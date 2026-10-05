"""Deep-merge settings writes (M4 D6): unknown keys survive at every level.

Run with: python _test_settings_merge.py
No upstream credentials or outbound network are used.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="settings-merge-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_settings


class DeepMergeTests(unittest.TestCase):
    def test_nested_merge_keeps_siblings(self):
        base = {"a": 1, "nested": {"x": 1, "y": 2}, "keep": {"deep": True}}
        patch = {"a": 2, "nested": {"x": 9}, "new": 3}
        out = wb_settings.deep_merge(base, patch)
        self.assertEqual(out, {"a": 2, "nested": {"x": 9, "y": 2},
                               "keep": {"deep": True}, "new": 3})
        self.assertEqual(base["a"], 1, "base must not be mutated")

    def test_non_dict_values_replace(self):
        self.assertEqual(wb_settings.deep_merge({"a": [1, 2]}, {"a": [3]}), {"a": [3]})
        self.assertEqual(wb_settings.deep_merge("x", {"a": 1}), {"a": 1})


class SettingsPreservationTests(unittest.TestCase):
    def write_unknowns(self, directory):
        data = {
            "user_custom_top": {"keep": 1},
            "pool": {"unknown_pool_key": "keep-me", "nested": {"deep": 7}},
            "schedule": {"unknown_schedule_key": True},
            "redis": {"unknown_redis_key": "keep"},
            "upstream": {"unknown_upstream_key": 42},
            "prompt": {"unknown_prompt_key": "keep"},
        }
        with open(wb_settings.settings_path(directory), "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def read(self, directory):
        with open(wb_settings.settings_path(directory), "r", encoding="utf-8") as fh:
            return json.load(fh)

    def test_every_group_setter_preserves_unknown_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_unknowns(directory)
            wb_settings.set_pool_config(directory, {"weighted_pick": False})
            wb_settings.set_schedule_config(directory, {"checkin_hours": [3]})
            wb_settings.set_redis_config(directory, {"enabled": False})
            wb_settings.set_upstream_config(directory, {"idle_timeout_seconds": 600})
            wb_settings.set_prompt_config(directory, {"mode": "append"})
            stored = self.read(directory)
        self.assertEqual(stored["user_custom_top"], {"keep": 1})
        self.assertEqual(stored["pool"]["unknown_pool_key"], "keep-me")
        self.assertEqual(stored["pool"]["nested"], {"deep": 7})
        self.assertEqual(stored["schedule"]["unknown_schedule_key"], True)
        self.assertEqual(stored["redis"]["unknown_redis_key"], "keep")
        self.assertEqual(stored["upstream"]["unknown_upstream_key"], 42)
        self.assertEqual(stored["prompt"]["unknown_prompt_key"], "keep")

    def test_known_values_still_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_unknowns(directory)
            wb_settings.set_schedule_config(directory, {"checkin_hours": [3, 15]})
            wb_settings.set_upstream_config(directory, {"header_timeout_seconds": 45})
            wb_settings.set_prompt_config(directory, {"mode": "custom", "file": "x.txt"})
            stored = self.read(directory)
            schedule = wb_settings.schedule_config(directory)
            upstream = wb_settings.upstream_config(directory)
            prompt = wb_settings.prompt_config(directory)
        self.assertEqual(schedule["checkin_hours"], [3, 15])
        self.assertEqual(upstream["header_timeout_seconds"], 45)
        self.assertEqual(prompt["mode"], "custom")
        self.assertEqual(prompt["file"], "x.txt")
        self.assertEqual(stored["schedule"]["unknown_schedule_key"], True)

    def test_top_level_unknown_keys_survive_scalar_setters(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_unknowns(directory)
            wb_settings.set_reserve_credits(directory, 10)
            stored = self.read(directory)
        self.assertEqual(stored["user_custom_top"], {"keep": 1})
        self.assertEqual(stored["reserve_credits"], 10)


class SettingsCacheTests(unittest.TestCase):
    def test_load_reuses_the_parse_until_the_file_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            wb_settings.save(directory, {"a": 1})
            real_load = json.load
            calls = {"n": 0}

            def counting_load(fh):
                calls["n"] += 1
                return real_load(fh)

            with mock.patch.object(wb_settings.json, "load",
                                   side_effect=counting_load):
                first = wb_settings.load(directory)
                second = wb_settings.load(directory)
            self.assertEqual(calls["n"], 1)
            self.assertEqual(first, {"a": 1})
            self.assertEqual(second, {"a": 1})
            # save() invalidates the entry, so a panel change lands at once.
            wb_settings.save(directory, {"a": 2})
            self.assertEqual(wb_settings.load(directory)["a"], 2)

    def test_load_notices_an_external_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            wb_settings.save(directory, {"a": 1})
            self.assertEqual(wb_settings.load(directory)["a"], 1)
            with open(wb_settings.settings_path(directory), "w",
                      encoding="utf-8") as fh:
                json.dump({"a": 222}, fh)
            self.assertEqual(wb_settings.load(directory)["a"], 222)


if __name__ == "__main__":
    unittest.main()
