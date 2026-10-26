"""Outbound identity versions: verified defaults, panel overrides, password gate.

The version numbers are part of the wire fingerprint. The defaults are the
values verified against captures and unpacked official packages; a panel-set
override changes only the string, not the header family or request body.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_identity
import wb_accounts
import wb_proxy
import wb_settings


class DefaultVersionTests(unittest.TestCase):
    def setUp(self):
        wb_identity.configure_versions({})

    def tearDown(self):
        wb_identity.configure_versions({})

    def test_verified_defaults(self):
        self.assertEqual(wb_identity.desktop_ua("intl"),
                         "WorkBuddy/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0")
        self.assertEqual(wb_identity.desktop_ua("cn"),
                         "WorkBuddy/5.8.3 WorkBuddy/5.8.3 CLI/2.161.0")
        self.assertEqual(wb_identity.vscode_ua(),
                         "VSCode/1.119.0 CodeBuddy/4.12.39997873")
        self.assertEqual(wb_identity.cli_version(), "2.168.2")
        self.assertEqual(wb_identity.sdk_version(), "6.25.0")
        self.assertEqual(wb_identity.node_version(), "v22.21.1")

    def test_desktop_override_changes_both_version_and_ua(self):
        wb_identity.configure_versions({"desktop_cn": "9.9.9"})
        self.assertEqual(wb_identity.desktop_version("cn"), "9.9.9")
        self.assertIn("WorkBuddy/9.9.9 WorkBuddy/9.9.9",
                      wb_identity.desktop_ua("cn"))
        # The other realm keeps its default.
        self.assertEqual(wb_identity.desktop_version("intl"), "5.8.4")

    def test_vscode_and_cli_overrides(self):
        wb_identity.configure_versions({"vscode_product": "1.2.3",
                                        "vscode_ide": "2.3.4",
                                        "cli": "9.8.7",
                                        "sdk": "1.0.0",
                                        "node": "v1.0.0"})
        self.assertEqual(wb_identity.vscode_ua(),
                         "VSCode/2.3.4 CodeBuddy/1.2.3")
        self.assertEqual(wb_identity.cli_version(), "9.8.7")
        headers = wb_identity.cli_stainless_headers()
        self.assertEqual(headers["x-stainless-package-version"], "1.0.0")
        self.assertEqual(headers["x-stainless-runtime-version"], "v1.0.0")

    def test_clearing_restores_defaults(self):
        wb_identity.configure_versions({"desktop_intl": "9.9.9"})
        wb_identity.configure_versions({})
        self.assertEqual(wb_identity.desktop_version("intl"), "5.8.4")


class SettingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="identity-versions-")

    def test_default_is_empty(self):
        self.assertEqual(wb_settings.identity_versions(self.dir), {})

    def test_round_trip_and_clear(self):
        stored = wb_settings.set_identity_versions(
            self.dir, {"desktop_cn": "9.9.9", "cli": "2.0.0"})
        self.assertEqual(stored, {"desktop_cn": "9.9.9", "cli": "2.0.0"})
        self.assertEqual(wb_settings.identity_versions(self.dir), stored)
        self.assertEqual(wb_settings.set_identity_versions(self.dir, {}), {})
        self.assertEqual(wb_settings.identity_versions(self.dir), {})

    def test_invalid_versions_are_rejected(self):
        with self.assertRaises(ValueError):
            wb_settings.set_identity_versions(self.dir,
                                              {"desktop_cn": "9 9"})
        with self.assertRaises(ValueError):
            wb_settings.set_identity_versions(self.dir,
                                              {"desktop_cn": "x" * 80})


class FakeHandler(object):
    _handle_settings_save = wb_proxy.Handler._handle_settings_save

    def __init__(self, payload):
        self.payload = payload
        self.reply = None

    def _payload_or_error(self, allow_list=False):
        return self.payload

    def _body_consumed(self):
        pass

    def _json(self, status, payload):
        self.reply = (status, payload)
        return status, payload

    def _error(self, status, message, kind=""):
        self.reply = (status, message)
        return status, message


class PasswordGateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="identity-gate-")
        self.old = wb_proxy.ACCOUNTS_DIR
        wb_proxy.ACCOUNTS_DIR = self.dir
        wb_identity.configure_versions({})

    def tearDown(self):
        wb_proxy.ACCOUNTS_DIR = self.old
        wb_identity.configure_versions({})

    def save(self, password, versions):
        handler = FakeHandler({"identity_versions": versions,
                               "panel_password": password})
        wb_proxy.Handler._handle_settings_save(handler)
        return handler.reply

    def test_wrong_password_is_refused(self):
        status, _body = self.save("wrong", {"desktop_cn": "9.9.9"})
        self.assertEqual(status, 401)
        self.assertEqual(wb_settings.identity_versions(self.dir), {})
        self.assertEqual(wb_identity.desktop_version("cn"), "5.8.3")

    def test_default_panel_password_saves_and_applies(self):
        status, body = self.save("admin", {"desktop_cn": "9.9.9"})
        self.assertEqual(status, 200)
        self.assertEqual(body["identity_versions"], {"desktop_cn": "9.9.9"})
        self.assertEqual(wb_settings.identity_versions(self.dir),
                         {"desktop_cn": "9.9.9"})
        self.assertEqual(wb_identity.desktop_version("cn"), "9.9.9")


class DesktopChatProfileTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("WB_DESKTOP_CHAT_COMPAT", None)
        wb_identity.configure_versions({})
        self.token = mock.patch.object(wb_accounts.Account, "_device_token", return_value="")
        self.token.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.token.stop)
        self.addCleanup(wb_identity.configure_versions, {})

    def account(self, product="workbuddy", realm="cn"):
        return wb_accounts.Account({"uid": "desktop-fixture", "accessToken": "fixture",
                                    "product": product, "realm": realm}, path=None)

    def test_default_retains_legacy_profile(self):
        headers = self.account().headers()
        self.assertIn("Origin", headers)
        self.assertNotIn("x-stainless-runtime", headers)
        self.assertNotIn("X-B3-ParentSpanId", headers)

    def test_only_explicit_one_enables_profile(self):
        for value in ("0", "false", "true", "yes", ""):
            with self.subTest(value=value):
                os.environ["WB_DESKTOP_CHAT_COMPAT"] = value
                self.assertFalse(wb_identity.desktop_chat_compat_enabled())
        os.environ["WB_DESKTOP_CHAT_COMPAT"] = "1"
        self.assertTrue(wb_identity.desktop_chat_compat_enabled())

    def test_desktop_realms_use_sdk_not_browser_headers(self):
        os.environ["WB_DESKTOP_CHAT_COMPAT"] = "1"
        for realm in ("cn", "intl"):
            with self.subTest(realm=realm):
                headers = self.account(realm=realm).headers()
                self.assertEqual(headers["Accept"], "application/json")
                self.assertEqual(headers["Authorization"], "Bearer fixture")
                self.assertEqual(headers["X-User-Id"], "desktop-fixture")
                for name in ("Origin", "Referer", "Accept-Language", "X-Requested-With",
                             "X-CodeBuddy-Request", "X-Machine-ID", "X-Session-ID"):
                    self.assertNotIn(name, headers)

    def test_sdk_headers_respect_existing_runtime_overrides(self):
        os.environ.update(WB_DESKTOP_CHAT_COMPAT="1", WB_IDENTITY_OS="Linux",
                          WB_IDENTITY_ARCH="arm64")
        wb_identity.configure_versions({"sdk": "9.1.0", "node": "v23.0.0"})
        headers = self.account().headers()
        for name, value in {"lang": "js", "package-version": "9.1.0", "os": "Linux",
                            "arch": "arm64", "runtime": "node", "runtime-version": "v23.0.0",
                            "retry-count": "0"}.items():
            self.assertEqual(headers["x-stainless-" + name], value)
        self.assertNotIn("x-stainless-timeout", headers)

    def test_b3_relationships_and_parent_are_consistent(self):
        os.environ["WB_DESKTOP_CHAT_COMPAT"] = "1"
        headers = self.account().headers()
        trace, span, sampled, parent = headers["b3"].split("-")
        self.assertEqual(headers["X-B3-TraceId"], trace)
        self.assertEqual(headers["X-B3-SpanId"], span)
        self.assertEqual(headers["X-B3-ParentSpanId"], parent)
        self.assertEqual((len(trace), len(span), len(parent), sampled), (32, 16, 16, "1"))
        self.assertEqual(headers["traceparent"], "00-" + trace + "-" + span + "-01")

    def test_other_products_and_billing_are_unchanged(self):
        stable = ("Accept", "Origin", "Referer", "Accept-Language", "X-Requested-With",
                  "User-Agent", "x-stainless-runtime", "x-stainless-timeout", "X-IDE-Type")
        for product, purpose in (("cli", "chat"), ("vscode", "chat"), ("workbuddy", "billing")):
            with self.subTest(product=product, purpose=purpose):
                acc = self.account(product)
                os.environ.pop("WB_DESKTOP_CHAT_COMPAT", None)
                before = acc.headers(purpose=purpose)
                os.environ["WB_DESKTOP_CHAT_COMPAT"] = "1"
                after = acc.headers(purpose=purpose)
                self.assertEqual({k: before.get(k) for k in stable},
                                 {k: after.get(k) for k in stable})

    def test_missing_device_token_is_not_invented(self):
        os.environ["WB_DESKTOP_CHAT_COMPAT"] = "1"
        self.assertNotIn("X-Device-Token", self.account().headers())
        self.assertNotIn("X-Device-Token-Error", self.account().headers())

    def test_real_device_token_preserves_realm_boundary(self):
        os.environ["WB_DESKTOP_CHAT_COMPAT"] = "1"
        with mock.patch.object(wb_accounts.Account, "_device_token", return_value="real-fixture"):
            self.assertEqual(self.account().headers()["X-Device-Token"], "real-fixture")
            self.assertNotIn("X-Device-Token", self.account(realm="intl").headers())


if __name__ == "__main__":
    unittest.main(verbosity=2)
