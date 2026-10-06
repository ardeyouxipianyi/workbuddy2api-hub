"""Panel-parity upstream timeouts (B1) and the optional X-Device-Token (B4).

Run with: python _test_upstream_timeouts.py
No upstream credentials or outbound network are used.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="upstream-timeouts-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_proxy
import wb_settings


def clear_device_token_cache():
    with wb_accounts._DEVICE_TOKEN_LOCK:
        wb_accounts._DEVICE_TOKEN_CACHE.update({"key": None, "token": "", "at": 0.0})


class UpstreamSettingsTests(unittest.TestCase):
    def test_defaults_match_the_panel_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = wb_settings.upstream_config(directory)
        self.assertEqual(cfg, {
            "header_timeout_seconds": 120,
            "idle_timeout_seconds": 300,
            "device_token": "",
            "device_token_file": "",
        })

    def test_partial_patch_merges_and_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            wb_settings.set_upstream_config(directory, {"header_timeout_seconds": 45})
            cfg = wb_settings.upstream_config(directory)
            self.assertEqual(cfg["header_timeout_seconds"], 45)
            self.assertEqual(cfg["idle_timeout_seconds"], 300)
            wb_settings.set_upstream_config(directory, {"idle_timeout_seconds": 600})
            cfg = wb_settings.upstream_config(directory)
            self.assertEqual(cfg["header_timeout_seconds"], 45)
            self.assertEqual(cfg["idle_timeout_seconds"], 600)
            wb_settings.set_upstream_config(directory, {"device_token_file": " C:/tmp/tok "})
            self.assertEqual(wb_settings.upstream_config(directory)["device_token_file"],
                             "C:/tmp/tok")

    def test_validate_rejects_bad_shapes(self):
        for bad in ({"header_timeout_seconds": True},
                    {"header_timeout_seconds": "120"},
                    {"header_timeout_seconds": 0},
                    {"idle_timeout_seconds": -1},
                    {"device_token": 7},
                    {"device_token_file": None},
                    {"unknown_setting": 1}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                wb_settings.validate_upstream_patch(bad)
        self.assertEqual(wb_settings.validate_upstream_patch({"device_token": " tok "}),
                         {"device_token": "tok"})

    def test_lenient_load_ignores_corrupt_and_legacy_values(self):
        with tempfile.TemporaryDirectory() as directory:
            with open(wb_settings.settings_path(directory), "w", encoding="utf-8") as fh:
                json.dump({"upstream": {"header_timeout_seconds": "nope",
                                        "idle_timeout_seconds": -3,
                                        "device_token": 7,
                                        "device_token_file": None}}, fh)
            cfg = wb_settings.upstream_config(directory)
            self.assertEqual(cfg["header_timeout_seconds"], 120)
            self.assertEqual(cfg["idle_timeout_seconds"], 300)
            self.assertEqual(cfg["device_token"], "")
            self.assertEqual(cfg["device_token_file"], "")
        with tempfile.TemporaryDirectory() as directory:
            with open(wb_settings.settings_path(directory), "w", encoding="utf-8") as fh:
                json.dump({"upstream": [], "reserve_credits": 5}, fh)
            self.assertEqual(wb_settings.upstream_config(directory)["idle_timeout_seconds"],
                             300)
        with tempfile.TemporaryDirectory() as directory:
            with open(wb_settings.settings_path(directory), "w", encoding="utf-8") as fh:
                fh.write("{not json")
            self.assertEqual(wb_settings.upstream_config(directory)["header_timeout_seconds"],
                             120)


class DeviceTokenTests(unittest.TestCase):
    def setUp(self):
        clear_device_token_cache()

    def make_account(self, directory, uid="uid-device"):
        return wb_accounts.Account({"uid": uid, "accessToken": "t", "realm": "intl"},
                                   os.path.join(directory, uid + ".json"))

    def test_no_configuration_injects_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            account = self.make_account(directory)
            self.assertEqual(wb_accounts.resolve_device_token(directory), "")
            self.assertNotIn("X-Device-Token", account.headers("chat"))
            self.assertNotIn("X-Device-Token", account.headers("billing"))

    def test_literal_token_is_injected_on_chat_and_billing(self):
        with tempfile.TemporaryDirectory() as directory:
            wb_settings.set_upstream_config(directory, {"device_token": "tok-literal"})
            account = self.make_account(directory)
            self.assertEqual(wb_accounts.resolve_device_token(directory), "tok-literal")
            self.assertEqual(account.headers("chat")["X-Device-Token"], "tok-literal")
            self.assertEqual(account.headers("billing")["X-Device-Token"], "tok-literal")

    def test_file_token_is_read_trimmed_and_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = os.path.join(directory, "device_token")
            with open(token_file, "w", encoding="utf-8") as fh:
                fh.write("  tok-file\n")
            wb_settings.set_upstream_config(directory, {"device_token_file": token_file})
            with mock.patch.object(wb_accounts.time, "time", return_value=1000.0):
                self.assertEqual(wb_accounts.resolve_device_token(directory), "tok-file")
            with open(token_file, "w", encoding="utf-8") as fh:
                fh.write("tok-rotated")
            with mock.patch.object(wb_accounts.time, "time", return_value=1299.0):
                self.assertEqual(wb_accounts.resolve_device_token(directory), "tok-file")
            with mock.patch.object(wb_accounts.time, "time", return_value=1301.0):
                self.assertEqual(wb_accounts.resolve_device_token(directory), "tok-rotated")

    def test_literal_wins_over_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = os.path.join(directory, "device_token")
            with open(token_file, "w", encoding="utf-8") as fh:
                fh.write("tok-file")
            wb_settings.set_upstream_config(directory, {"device_token": "tok-literal",
                                                        "device_token_file": token_file})
            self.assertEqual(wb_accounts.resolve_device_token(directory), "tok-literal")

    def test_oversized_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = os.path.join(directory, "device_token")
            with open(token_file, "w", encoding="utf-8") as fh:
                fh.write("x" * 1025)
            wb_settings.set_upstream_config(directory, {"device_token_file": token_file})
            self.assertEqual(wb_accounts.resolve_device_token(directory), "")
            self.assertNotIn("X-Device-Token", self.make_account(directory).headers("chat"))

    def test_missing_file_degrades_to_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            wb_settings.set_upstream_config(directory, {
                "device_token_file": os.path.join(directory, "does-not-exist")})
            self.assertEqual(wb_accounts.resolve_device_token(directory), "")
            self.assertNotIn("X-Device-Token", self.make_account(directory).headers("chat"))

    def test_key_change_bypasses_the_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = os.path.join(directory, "device_token")
            with open(token_file, "w", encoding="utf-8") as fh:
                fh.write("tok-file")
            wb_settings.set_upstream_config(directory, {"device_token_file": token_file})
            with mock.patch.object(wb_accounts.time, "time", return_value=1000.0):
                self.assertEqual(wb_accounts.resolve_device_token(directory), "tok-file")
            wb_settings.set_upstream_config(directory, {"device_token": "tok-literal"})
            with mock.patch.object(wb_accounts.time, "time", return_value=1001.0):
                self.assertEqual(wb_accounts.resolve_device_token(directory), "tok-literal")

    def test_account_without_a_directory_injects_nothing(self):
        account = wb_accounts.Account({"uid": "uid-bare", "accessToken": "t"})
        self.assertNotIn("X-Device-Token", account.headers("chat"))


class StreamTimeoutTests(unittest.TestCase):
    def test_upstream_timeouts_default_and_override(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(wb_proxy, "ACCOUNTS_DIR", directory):
                self.assertEqual(wb_proxy.upstream_timeouts(), (120.0, 300.0))
                wb_settings.set_upstream_config(directory, {
                    "header_timeout_seconds": 45, "idle_timeout_seconds": 600})
                self.assertEqual(wb_proxy.upstream_timeouts(), (45.0, 600.0))

    def test_apply_stream_idle_timeout_sets_the_socket(self):
        class Sock(object):
            def __init__(self):
                self.timeouts = []

            def settimeout(self, value):
                self.timeouts.append(value)

        sock = Sock()

        class Raw(object):
            _sock = sock

        class Fp(object):
            raw = Raw()

        class Response(object):
            fp = Fp()

        self.assertTrue(wb_proxy._apply_stream_idle_timeout(Response(), 300))
        self.assertEqual(sock.timeouts, [300.0])

        class FpFallback(object):
            _sock = sock

        class ResponseFallback(object):
            fp = FpFallback()

        self.assertTrue(wb_proxy._apply_stream_idle_timeout(ResponseFallback(), 12))
        self.assertEqual(sock.timeouts, [300.0, 12.0])

    def test_apply_stream_idle_timeout_degrades_cleanly(self):
        class Response(object):
            pass

        self.assertFalse(wb_proxy._apply_stream_idle_timeout(Response(), 300))
        self.assertFalse(wb_proxy._apply_stream_idle_timeout(Response(), 0))
        self.assertFalse(wb_proxy._apply_stream_idle_timeout(Response(), "nope"))

        class Sock(object):
            def settimeout(self, value):
                raise OSError("gone")

        class Fp(object):
            _sock = Sock()

        class ResponseWithSock(object):
            fp = Fp()

        self.assertFalse(wb_proxy._apply_stream_idle_timeout(ResponseWithSock(), 300))

    def test_open_upstream_applies_header_timeout_and_idle_timeout(self):
        class Sock(object):
            def __init__(self):
                self.timeouts = []

            def settimeout(self, value):
                self.timeouts.append(value)

        class Response(object):
            def __init__(self, sock):
                self._sock = sock
                self.closed = False

            @property
            def fp(self):
                class Raw(object):
                    _sock = self._sock

                class Fp(object):
                    raw = Raw()

                return Fp()

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            wb_settings.set_upstream_config(directory, {
                "header_timeout_seconds": 45, "idle_timeout_seconds": 600})
            account = wb_accounts.Account(
                {"uid": "uid-b1", "accessToken": "t", "realm": "intl"},
                os.path.join(directory, "uid-b1.json"))
            sock = Sock()
            response = Response(sock)
            calls = {}

            def fake_urlopen(req, timeout=30, proxy=""):
                calls["timeout"] = timeout
                calls["proxy"] = proxy
                return response

            class Pool(object):
                accounts = [account]
                affinity = type("Affinity", (), {"unbind": lambda self, key: None})()

                def count_ready(self, realm, model=None):
                    return 1

                def pick_for_session(self, realm, session_key=None, exclude=(),
                                     model=None):
                    return account

                def list_public(self):
                    return []

                def apply_daily_token_limit(self, value=None, usage=None):
                    return value or 0

                def apply_daily_credit_limit(self, value=None, credits=None, free_models=None):
                    return value or 0

                def apply_model_daily_token_limit(self, value=None, per_model=None):
                    return value or 0

            old_pool, old_urlopen = wb_proxy.POOL, wb_accounts.urlopen
            wb_proxy.POOL = Pool()
            wb_accounts.urlopen = fake_urlopen
            try:
                with mock.patch.object(wb_proxy, "ACCOUNTS_DIR", directory):
                    upstream, picked, _effort = wb_proxy.open_upstream(
                        {"model": "deepseek-v4.1-flash",
                         "messages": [{"role": "user", "content": "hi"}]},
                        target_realm="intl")
                try:
                    self.assertEqual(picked.uid, "uid-b1")
                    self.assertEqual(calls["timeout"], 45.0)
                    self.assertEqual(sock.timeouts, [600.0])
                finally:
                    upstream.close()
                self.assertTrue(response.closed)
                self.assertEqual(account.in_flight, 0)
            finally:
                wb_proxy.POOL = old_pool
                wb_accounts.urlopen = old_urlopen

    def test_runtime_settings_view_exposes_upstream(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(wb_proxy, "ACCOUNTS_DIR", directory):
                view = wb_proxy.runtime_settings_view()
        self.assertIn("upstream", view)
        self.assertEqual(view["upstream"]["idle_timeout_seconds"], 300)


if __name__ == "__main__":
    unittest.main()
