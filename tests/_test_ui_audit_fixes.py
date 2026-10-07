"""Regression tests for the 2026-10-06 UI audit fixes.

These cover the small backend/API contract changes that make the new
dashboard controls and status displays safe to use. They deliberately do not
touch a real deployment or the network.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="ui-audit-fixes-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_pool
import wb_proxy
import wb_settings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CaptureHandler(wb_proxy.Handler):
    def __init__(self):
        self.captured = None
        self.panel_ok = True

    def _json(self, code, obj):
        self.captured = (code, obj)

    def _error(self, code, message, err_type="server_error"):
        self.captured = (code, {"error": {
            "message": message, "type": err_type, "code": code}})

    def _panel_ok(self):
        return self.panel_ok


class BusyHandler(CaptureHandler):
    def __init__(self):
        super().__init__()
        self.path = "/v1/chat/completions"
        self.headers = {"X-Request-Id": "busy-test", "User-Agent": "unit-test"}
        self.client_address = ("127.0.0.1", 12345)
        self.payload = {"model": "busy-model", "stream": False, "messages": []}

    def _is_panel_route(self, path):
        return False

    def _authorized(self):
        return True

    def _payload_or_error(self, allow_list=False):
        return self.payload


class RouteContractTests(unittest.TestCase):
    def test_delete_missing_account_is_404(self):
        handler = CaptureHandler()
        pool = mock.Mock()
        pool.remove.return_value = False
        with mock.patch.object(wb_proxy, "POOL", pool):
            handler._route_accounts_delete({"uid": "missing"})
        self.assertEqual(handler.captured[0], 404)
        self.assertIn("no such account", handler.captured[1]["error"]["message"])

    def test_realm_rejects_unknown_value(self):
        handler = CaptureHandler()
        with mock.patch.object(wb_proxy, "save_persisted_realm") as save:
            handler._route_realm({"realm": "xx"})
        self.assertEqual(handler.captured[0], 400)
        save.assert_not_called()

    def test_realm_accepts_known_value_case_insensitively(self):
        handler = CaptureHandler()
        with mock.patch.object(wb_proxy, "save_persisted_realm") as save:
            handler._route_realm({"realm": "CN"})
        self.assertEqual(handler.captured[0], 200)
        save.assert_called_once_with("cn")

    def test_request_archive_requires_panel_auth(self):
        self.assertTrue(wb_proxy.Handler._is_panel_route("/requests"))
        self.assertTrue(wb_proxy.Handler._is_panel_route("/requests/metrics"))

    def test_requests_limit_is_strict(self):
        rows = [{"request_id": str(i), "at": float(i)} for i in range(10)]
        handler = CaptureHandler()
        with mock.patch.object(wb_proxy.wb_reqlog, "read_rows", return_value=rows), \
                mock.patch.object(wb_proxy.wb_reqlog, "filter_rows", return_value=rows):
            for bad in (0, -1, "abc", 100000, True):
                handler._route_requests({"limit": bad})
                self.assertEqual(handler.captured[0], 400, bad)
            handler._route_requests({"limit": 5})
            self.assertEqual(handler.captured[0], 200)
            self.assertEqual(len(handler.captured[1]["rows"]), 5)
            handler._route_requests({})
            self.assertEqual(handler.captured[0], 200)
            self.assertEqual(len(handler.captured[1]["rows"]), 10)

    def test_busy_hint_is_recorded(self):
        with mock.patch.object(wb_proxy, "_persist_usage") as persist:
            wb_proxy.record_error(
                "test-model", 503,
                "gateway is at its concurrent chat limit (32 in flight); retry shortly")
        row = persist.call_args[0][0]
        self.assertEqual(row["gateway_hint"],
                         "gateway is busy at its concurrency limit; retry shortly")

    def test_busy_slot_gate_records_before_replying(self):
        handler = BusyHandler()
        with mock.patch.object(wb_proxy._chat_slots, "acquire", return_value=False), \
                mock.patch.object(wb_proxy, "_persist_usage") as persist:
            handler.do_POST()
        self.assertEqual(handler.captured[0], 503)
        row = persist.call_args[0][0]
        self.assertEqual(row["status"], 503)
        self.assertEqual(row["gateway_hint"],
                         "gateway is busy at its concurrency limit; retry shortly")
        self.assertEqual(row["request_id"], "busy-test")

    def test_runtime_settings_returns_api_key_models_and_concurrency(self):
        entries = [{
            "id": "k1", "name": "main", "key": "secret-key", "realm": "cn",
            "enabled": True, "models": ["hy3", "deepseek-v4.1-flash"],
        }]
        with mock.patch.object(wb_proxy, "configured_keys", return_value=entries):
            view = wb_proxy.runtime_settings_view()
        self.assertEqual(view["api_keys"][0]["models"],
                         ["hy3", "deepseek-v4.1-flash"])
        self.assertIn("max_concurrent_chat", view)
        self.assertIn("chat_slot_wait_seconds", view)

    def test_public_includes_session_dead_state(self):
        account = wb_accounts.Account({"uid": "u1", "enabled": False})
        account.session_dead_fails = 2
        account.pool_cfg = dict(wb_pool.DEFAULTS)
        account.pool_cfg["session_dead_threshold"] = 3
        view = account.public()
        self.assertEqual(view["sessionDeadFails"], 2)
        self.assertEqual(view["sessionDeadThreshold"], 3)


class DashboardCoverageTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8") as fh:
            self.html = fh.read()

    def test_all_advanced_setting_keys_are_in_dashboard(self):
        keys = set(wb_pool.DEFAULTS)
        keys.update(wb_settings.SCHEDULE_DEFAULTS)
        keys.update(wb_settings.REDIS_DEFAULTS)
        keys.update(wb_settings.UPSTREAM_DEFAULTS)
        keys.update(wb_settings.PROMPT_DEFAULTS)
        keys.update(wb_settings.LOGGING_DEFAULTS)
        missing = sorted(key for key in keys if key not in self.html)
        self.assertEqual(missing, [], "advanced keys missing from dashboard: %s" % missing)

    def test_mobile_fixes_are_present(self):
        for marker in (
            "#logTagFilters{flex-wrap:wrap;max-width:100%}",
            "#pageSettings table:not(.data-cards){table-layout:fixed;width:100%}",
            "#accounts .id-btn{min-height:30px;min-width:32px}",
        ):
            self.assertIn(marker, self.html)

    def test_mobile_checker_uses_cross_platform_temp_paths(self):
        with open(os.path.join(ROOT, "tests", "_mobile_check.py"), encoding="utf-8") as fh:
            checker = fh.read()
        self.assertIn("tempfile.gettempdir()", checker)
        self.assertIn("WB_MOBILE_FIXTURES", checker)
        self.assertIn("WB_MOBILE_SHOTS", checker)
        self.assertNotIn('"/tmp/mobile-fixtures"', checker)
        self.assertNotIn('"/tmp/mobile-shots"', checker)

    def test_cloud_filters_and_governance_markers_are_present(self):
        for marker in (
            "reqFilterAccount", "reqFilterStatus", "reqFilterOutcome",
            "reqFilterPath", "reqFilterRequestId", "reqFilterSince",
            "reqFilterUntil", "错误 / 提示",
            "inFlight", "breakerFor", "degradeFor", "balanceCooledFor",
        ):
            self.assertIn(marker, self.html)
        self.assertNotIn("function filterRealm(", self.html)


if __name__ == "__main__":
    unittest.main()
