"""Optional Upstash mirror for sticky sessions (panel parity).

Run with: python _test_redis_mirror.py
No outbound network is used (urlopen is stubbed).
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_redisstore
import wb_settings


class FakeMirror(object):
    def __init__(self):
        self.data = {}
        self.sets = []
        self.deletes = []

    def enabled(self):
        return True

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ttl):
        self.data[key] = value
        self.sets.append((key, value, ttl))
        return True

    def delete(self, key):
        existed = key in self.data
        self.data.pop(key, None)
        self.deletes.append(key)
        return existed


class AffinityMirrorTests(unittest.TestCase):
    def test_bind_mirrors_and_restart_rehydrates(self):
        mirror = FakeMirror()
        first = wb_accounts.SessionAffinity(ttl=60, mirror=mirror, mirror_ttl=120)
        first.bind("s1", "uid-1")
        self.assertEqual(mirror.data["wbaff:s1"], "uid-1")
        self.assertEqual(mirror.sets[0][2], 120)
        second = wb_accounts.SessionAffinity(ttl=60, mirror=mirror, mirror_ttl=120)
        self.assertEqual(second.get("s1"), "uid-1")
        self.assertIn("s1", second.bindings)
        second.unbind("s1")
        self.assertNotIn("wbaff:s1", mirror.data)

    def test_no_mirror_keeps_memory_only(self):
        affinity = wb_accounts.SessionAffinity(ttl=60)
        affinity.bind("s1", "uid-1")
        self.assertEqual(affinity.get("s1"), "uid-1")
        self.assertIsNone(affinity.mirror)

    def test_mirror_errors_are_swallowed(self):
        class Broken(object):
            def get(self, key):
                raise RuntimeError("down")

            def set(self, key, value, ttl):
                raise RuntimeError("down")

            def delete(self, key):
                raise RuntimeError("down")

        affinity = wb_accounts.SessionAffinity(ttl=60, mirror=Broken())
        affinity.bind("s1", "uid-1")
        self.assertEqual(affinity.get("s1"), "uid-1")
        affinity.unbind("s1")
        self.assertIsNone(affinity.get("s1"))


class UpstashStoreTests(unittest.TestCase):
    def test_commands_shape(self):
        captured = []

        class FakeResp(object):
            def __init__(self, payload):
                self.payload = payload

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        def fake_urlopen(req, timeout=None):
            captured.append((req.full_url, req.data, dict(req.header_items()), timeout))
            return FakeResp({"result": "uid-9"})

        with mock.patch.object(wb_redisstore.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            store = wb_redisstore.UpstashStore("redis.example.com", "tok", timeout=1.5)
            self.assertEqual(store.get("k"), "uid-9")
            self.assertTrue(store.set("k", "v", 60))
            self.assertTrue(store.delete("k"))

        self.assertEqual(captured[0][0], "https://redis.example.com")
        self.assertEqual(json.loads(captured[0][1].decode("utf-8")), ["GET", "k"])
        self.assertEqual(json.loads(captured[1][1].decode("utf-8")),
                         ["SET", "k", "v", "EX", 60])
        self.assertEqual(json.loads(captured[2][1].decode("utf-8")), ["DEL", "k"])
        self.assertEqual(captured[0][3], 1.5)
        self.assertIn("Authorization", {k: v for k, v in captured[0][2].items()})

    def test_errors_degrade_to_miss(self):
        with mock.patch.object(wb_redisstore.urllib.request, "urlopen",
                               side_effect=RuntimeError("boom")):
            store = wb_redisstore.UpstashStore("redis.example.com", "tok")
            self.assertIsNone(store.get("k"))
            self.assertFalse(store.set("k", "v", 60))
            self.assertFalse(store.delete("k"))
        self.assertFalse(wb_redisstore.build_mirror("").enabled())


class RedisSettingsTests(unittest.TestCase):
    def test_defaults_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = wb_settings.redis_config(directory)
            self.assertEqual(cfg["url"], "")
            self.assertFalse(cfg["affinity_mirror"])
            self.assertEqual(cfg["ttl_seconds"], 604800)
            wb_settings.set_redis_config(directory, {"url": "example.com",
                                                     "affinity_mirror": True,
                                                     "ttl_seconds": 120})
            again = wb_settings.redis_config(directory)
            self.assertEqual(again["url"], "example.com")
            self.assertTrue(again["affinity_mirror"])
            self.assertEqual(again["ttl_seconds"], 120)

    def test_validate_rejects_bad_shapes(self):
        with self.assertRaises(ValueError):
            wb_settings.validate_redis_patch({"affinity_mirror": "yes"})
        with self.assertRaises(ValueError):
            wb_settings.validate_redis_patch({"ttl_seconds": 10})
        with self.assertRaises(ValueError):
            wb_settings.validate_redis_patch({"url": 5})
        with self.assertRaises(ValueError):
            wb_settings.validate_redis_patch({"unknown_setting": 1})

    def test_pool_config_builds_the_mirror(self):
        with tempfile.TemporaryDirectory() as directory:
            wb_settings.set_redis_config(directory, {"url": "https://example.com",
                                                     "token": "tok",
                                                     "affinity_mirror": True,
                                                     "ttl_seconds": 120})
            pool = wb_accounts.AccountPool(directory)
            pool.accounts = []
            pool.apply_pool_config()
            self.assertIsNotNone(pool.affinity.mirror)
            self.assertTrue(pool.affinity.mirror.enabled())
            self.assertEqual(pool.affinity.mirror_ttl, 120)


if __name__ == "__main__":
    unittest.main(verbosity=1)
