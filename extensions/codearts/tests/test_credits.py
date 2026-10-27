"""Unit tests for the local CodeArts credit ledger (no network, no Hub).

The ledger prices `codeart/*` turns on the multiplier table the CodeArts model
picker publishes and keeps one balance per Hub API key. Run with the current
interpreter:

    python -m unittest discover -s extensions/codearts/tests -v
"""

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from extensions.codearts import credits as C
from extensions.codearts.bridge import BridgeRuntime
from extensions.codearts.config import BridgeConfig


def local_ts(hour, minute=0):
    """Local wall clock on a fixed date, as an epoch the module can inspect."""
    return time.mktime((2026, 10, 15, hour, minute, 0, 0, 0, -1))


class _Handler:
    """The only seam the balance floor reads from a Hub request handler."""

    def __init__(self, key_id):
        self.key_id = key_id

    def _key_id(self):
        return self.key_id


class MultiplierTests(unittest.TestCase):
    WINDOWS = C.parse_windows("23:00-08:00")

    def test_prefix_and_case_are_normalised(self):
        self.assertEqual(C.canonical_model("codeart/DeepSeek-V4.1-Flash"),
                         "deepseek-v4.1-flash")
        self.assertEqual(C.canonical_model("codearts/GLM-5.3"), "glm-5.3")
        self.assertEqual(C.canonical_model("  openpangu-2.0-pro  "), "openpangu-2.0-pro")
        self.assertEqual(C.canonical_model(None), "")

    def test_published_rates_are_the_default_table(self):
        table = C.effective_multipliers()
        self.assertEqual(table["glm-5.2"], 0.70)
        self.assertEqual(table["glm-5.2-arkts-spark"], 0.70)
        self.assertEqual(table["openpangu-2.0-pro"], 0.70)
        self.assertEqual(table["openpangu-2.0-flash"], 0.32)
        self.assertEqual(table["glm-5.3"], 0.79)
        self.assertEqual(table["deepseek-v4.1-flash"], 0.18)
        self.assertEqual(table["kimi-k3"], 2.38)

    def test_day_rate_outside_the_night_window(self):
        quote = C.resolve_multiplier("codeart/deepseek-v4.1-flash",
                                     windows=self.WINDOWS, at=local_ts(12))
        self.assertEqual(quote["multiplier"], 0.18)
        self.assertFalse(quote["night_rate"])
        self.assertFalse(quote["in_night_window"])

    def test_night_rate_inside_the_night_window(self):
        for hour, minute in ((23, 0), (23, 59), (0, 0), (7, 59)):
            quote = C.resolve_multiplier("codeart/deepseek-v4.1-flash",
                                         windows=self.WINDOWS, at=local_ts(hour, minute))
            self.assertEqual(quote["multiplier"], 0.10, (hour, minute))
            self.assertTrue(quote["night_rate"], (hour, minute))
            self.assertTrue(quote["in_night_window"], (hour, minute))

    def test_night_window_edges_are_exclusive_at_the_end(self):
        self.assertEqual(C.resolve_multiplier(
            "deepseek-v4.1-flash", windows=self.WINDOWS,
            at=local_ts(22, 59))["multiplier"], 0.18)
        quote = C.resolve_multiplier("deepseek-v4.1-flash", windows=self.WINDOWS,
                                     at=local_ts(8, 0))
        self.assertEqual(quote["multiplier"], 0.18)
        self.assertFalse(quote["in_night_window"])

    def test_other_models_keep_their_day_rate_at_night(self):
        quote = C.resolve_multiplier("codeart/GLM-5.3", windows=self.WINDOWS,
                                     at=local_ts(1))
        self.assertEqual(quote["multiplier"], 0.79)
        self.assertTrue(quote["in_night_window"])
        self.assertFalse(quote["night_rate"])

    def test_variants_inherit_their_longest_base_id(self):
        quote = C.resolve_multiplier("codeart/deepseek-v4.1-flash-1017",
                                     windows=self.WINDOWS, at=local_ts(12))
        self.assertEqual(quote["key"], "deepseek-v4.1-flash")
        self.assertEqual(quote["multiplier"], 0.18)

    def test_unknown_model_is_charged_the_default_rate(self):
        quote = C.resolve_multiplier("codeart/qwen3-vl-235b")
        self.assertIsNone(quote["key"])
        self.assertEqual(quote["multiplier"], C.DEFAULT_MULTIPLIER)

    def test_overrides_replace_builtin_rates(self):
        table = C.effective_multipliers({"GLM-5.2": 0.5})
        self.assertEqual(table["glm-5.2"], 0.5)
        night = C.effective_night_multipliers({"deepseek-v4.1-flash": 0.2})
        self.assertEqual(night["deepseek-v4.1-flash"], 0.2)


class WindowParsingTests(unittest.TestCase):
    def test_parse_one_and_many_windows(self):
        self.assertEqual(C.parse_windows("23:00-08:00"), [(1380, 480)])
        self.assertEqual(C.parse_windows("23:00-08:00, 12:00-13:00"),
                         [(1380, 480), (720, 780)])
        self.assertEqual(C.parse_windows(""), [])
        self.assertEqual(C.parse_windows(None), [])

    def test_bad_windows_raise(self):
        for text in ("23:00", "24:00-08:00", "23:60-08:00", "23:00-23:00",
                     "23:00-08:00-01:00", "abc"):
            with self.assertRaises(ValueError, msg=text):
                C.parse_windows(text)

    def test_a_trailing_comma_is_tolerated(self):
        # An operator editing "23:00-08:00," by hand should not lose the window.
        self.assertEqual(C.parse_windows("23:00-08:00,"), [(1380, 480)])

    def test_windows_format_back(self):
        self.assertEqual(C.format_windows([(1380, 480)]), "23:00-08:00")

    def test_windows_that_do_not_cross_midnight(self):
        windows = C.parse_windows("12:00-13:00")
        self.assertTrue(C.in_window(windows, at=local_ts(12, 30)))
        self.assertFalse(C.in_window(windows, at=local_ts(13, 0)))
        self.assertFalse(C.in_window(windows, at=local_ts(11, 59)))


class CreditMathTests(unittest.TestCase):
    def test_tokens_rate_and_base_price(self):
        self.assertEqual(C.credits_for(900, 100, 0.18, 1.0), 0.18)
        self.assertEqual(C.credits_for(900, 100, 0.18, 2.0), 0.36)
        self.assertEqual(C.credits_for(0, 0, 0.18, 1.0), 0.0)
        self.assertEqual(C.credits_for(None, None, 0.18, 1.0), 0.0)

    def test_night_rate_is_cheaper_than_day_rate(self):
        night = C.credits_for(900, 100, 0.10, 1.0)
        day = C.credits_for(900, 100, 0.18, 1.0)
        self.assertLess(night, day)

    def test_bad_numbers_are_rejected(self):
        with self.assertRaises(ValueError):
            C.credits_for(10, 10, float("nan"), 1.0)
        with self.assertRaises(ValueError):
            C.credits_for(10, 10, -1, 1.0)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "credits.json"
        self.ledger = C.CreditLedger(self.path)

    def test_unknown_key_starts_at_zero(self):
        entry = self.ledger.entry("k-new")
        self.assertEqual(entry["balance"], 0.0)
        self.assertEqual(entry["requests"], 0)
        self.assertEqual(self.ledger.balance("k-new"), 0.0)

    def test_grant_and_charge_persist(self):
        self.ledger.grant("k0", 100.0, note="首充")
        entry = self.ledger.charge("k0", 0.18, {
            "model": "codeart/deepseek-v4.1-flash", "tokens": 1000,
            "multiplier": 0.18, "night": False, "base_per_1k": 1.0})
        self.assertEqual(entry["balance"], 99.82)
        self.assertEqual(entry["spent"], 0.18)
        self.assertEqual(entry["requests"], 1)
        reopened = C.CreditLedger(self.path)
        self.assertEqual(reopened.balance("k0"), 99.82)
        event = reopened.events(1)[0]
        self.assertEqual(event["type"], "charge")
        self.assertEqual(event["multiplier"], 0.18)
        self.assertEqual(event["tokens"], 1000)

    def test_negative_grant_is_allowed_and_logged(self):
        self.ledger.grant("k0", 10.0)
        entry = self.ledger.grant("k0", -2.5, note="纠正")
        self.assertEqual(entry["balance"], 7.5)
        self.assertEqual(entry["granted"], 7.5)
        self.assertEqual(self.ledger.events(1)[0]["note"], "纠正")

    def test_charge_may_take_the_balance_negative(self):
        # The token count is only known after the turn, so one turn can end
        # below zero; the floor is checked before the *next* turn starts.
        entry = self.ledger.charge("k0", 0.5)
        self.assertEqual(entry["balance"], -0.5)
        self.assertEqual(self.ledger.balance("k0"), -0.5)

    def test_zero_and_bad_amounts_are_rejected(self):
        with self.assertRaises(ValueError):
            self.ledger.grant("k0", 0)
        with self.assertRaises(ValueError):
            self.ledger.charge("k0", 0)
        with self.assertRaises(ValueError):
            self.ledger.charge("k0", -1)
        with self.assertRaises(ValueError):
            self.ledger.grant("k0", "10")
        with self.assertRaises(ValueError):
            self.ledger.grant("k0", float("inf"))

    def test_key_ids_are_validated(self):
        for key_id in ("", "  ", "a\nb", "k" * 200, None):
            with self.assertRaises(ValueError, msg=repr(key_id)):
                self.ledger.grant(key_id, 1.0)

    def test_corrupt_file_is_moved_aside_not_overwritten(self):
        self.path.write_text("{not json", encoding="utf-8")
        ledger = C.CreditLedger(self.path)
        self.assertEqual(ledger.entries(), {})
        self.assertTrue(ledger.recovered_backup)
        backup = Path(ledger.recovered_backup)
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_text(encoding="utf-8"), "{not json")
        ledger.grant("k0", 5.0)
        self.assertEqual(C.CreditLedger(self.path).balance("k0"), 5.0)

    def test_event_log_rotates_once(self):
        events = self.ledger.events_path
        events.write_text("", encoding="utf-8")
        old = C.EVENTS_BACKUP_BYTES
        C.EVENTS_BACKUP_BYTES = 10
        try:
            self.ledger.grant("k0", 1.0)
            self.ledger.grant("k0", 1.0)
            self.assertTrue(events.with_name(events.name + ".1").exists())
        finally:
            C.EVENTS_BACKUP_BYTES = old


class PanelViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = C.CreditLedger(Path(self.tmp.name) / "credits.json")
        self.config = {"credits_enabled": True, "credits_enforce": True,
                       "credits_base_per_1k": 1.0,
                       "credits_night_window": "23:00-08:00",
                       "credits_multipliers": {"GLM-5.2": 0.5},
                       "credits_night_multipliers": {}}

    def test_view_lists_keys_rates_and_totals(self):
        self.ledger.grant("k0", 100.0)
        self.ledger.charge("k0", 0.18, {"model": "codeart/deepseek-v4.1-flash",
                                        "tokens": 1000, "multiplier": 0.18})
        view = C.panel_view(self.config, self.ledger,
                            key_names={"k0": "团队 A", "k1": "团队 B"},
                            now=local_ts(23, 30))
        self.assertTrue(view["night_now"])
        self.assertEqual(view["local_time"], "23:30")
        self.assertEqual(view["night_window"], "23:00-08:00")
        keys = {item["id"]: item for item in view["keys"]}
        self.assertEqual(keys["k0"]["name"], "团队 A")
        self.assertEqual(keys["k0"]["balance"], 99.82)
        self.assertEqual(keys["k1"]["balance"], 0.0)      # configured, not funded
        self.assertFalse(keys["k1"]["billed"])
        self.assertEqual(view["totals"]["requests"], 1)
        rates = {row["model"]: row for row in view["multipliers"]}
        self.assertEqual(rates["deepseek-v4.1-flash"]["night"], 0.10)
        self.assertEqual(rates["deepseek-v4.1-flash"]["source"], "builtin")
        self.assertEqual(rates["glm-5.2"]["day"], 0.5)
        self.assertEqual(rates["glm-5.2"]["source"], "override")
        self.assertEqual(view["events"][0]["type"], "charge")


class CredentialSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "bridge.json"
        self.config = BridgeConfig(self.path)

    def test_credit_defaults_are_off_and_conservative(self):
        view = self.config.view()
        self.assertFalse(view["credits_enabled"])
        self.assertTrue(view["credits_enforce"])
        self.assertEqual(view["credits_base_per_1k"], 1.0)
        self.assertEqual(view["credits_night_window"], "23:00-08:00")
        self.assertEqual(view["credits_multipliers"], {})
        self.assertEqual(view["credits_night_multipliers"], {})

    def test_credit_settings_round_trip_and_canonicalise(self):
        view = self.config.update({"credits_enabled": True,
                                   "credits_base_per_1k": 2.5,
                                   "credits_night_window": "22:00-06:00",
                                   "credits_multipliers": {"CodeArt/GLM-5.2": 0.5}})
        self.assertTrue(view["credits_enabled"])
        self.assertEqual(view["credits_base_per_1k"], 2.5)
        self.assertEqual(view["credits_night_window"], "22:00-06:00")
        self.assertEqual(view["credits_multipliers"], {"glm-5.2": 0.5})
        self.assertEqual(self.config.view()["credits_night_window"], "22:00-06:00")

    def test_bad_credit_settings_are_rejected(self):
        cases = (
            {"credits_enabled": "yes"},
            {"credits_enforce": 1},
            {"credits_base_per_1k": 0},
            {"credits_base_per_1k": "1"},
            {"credits_base_per_1k": float("nan")},
            {"credits_night_window": "23:00"},
            {"credits_night_window": 2300},
            {"credits_multipliers": []},
            {"credits_multipliers": {"": 1.0}},
            {"credits_multipliers": {"glm-5.2": -1}},
            {"credits_night_multipliers": {"glm-5.3": "0.1"}},
        )
        for patch in cases:
            with self.assertRaises(ValueError, msg=repr(patch)):
                self.config.update(patch)


class RuntimeBillingTests(unittest.TestCase):
    """The bridge seam: quote, charge and the balance floor."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "bridge.json"
        self.config = BridgeConfig(self.path)
        self.config.update({"enabled": True, "api_key": "test-key",
                            "credits_enabled": True})
        self.runtime = BridgeRuntime(None, self.path)
        self.runtime.local.handler = _Handler("k0")

    def row(self, at=None, **extra):
        row = {"key": "k0", "model": "codeart/deepseek-v4.1-flash",
               "prompt_tokens": 900, "completion_tokens": 100,
               "total_tokens": 1000, "at": local_ts(12) if at is None else at}
        row.update(extra)
        return row

    def test_day_turn_is_charged_at_the_published_rate(self):
        entry = self.runtime.charge_usage_row(self.row())
        self.assertEqual(entry["spent"], 0.18)
        self.assertEqual(self.runtime.ledger.balance("k0"), -0.18)

    def test_night_turn_uses_the_night_rate(self):
        entry = self.runtime.charge_usage_row(self.row(at=local_ts(23, 30)))
        self.assertEqual(entry["spent"], 0.10)

    def test_base_price_scales_the_charge(self):
        self.config.update({"credits_base_per_1k": 2.0})
        entry = self.runtime.charge_usage_row(self.row())
        self.assertEqual(entry["spent"], 0.36)

    def test_unbillable_rows_are_skipped(self):
        self.assertIsNone(self.runtime.charge_usage_row(self.row(key="")))
        self.assertIsNone(self.runtime.charge_usage_row(self.row(error=True)))
        self.assertIsNone(self.runtime.charge_usage_row(self.row(total_tokens=0)))
        self.config.update({"credits_enabled": False})
        self.assertIsNone(self.runtime.charge_usage_row(self.row()))

    def test_balance_floor_blocks_then_allows(self):
        self.assertTrue(self.runtime.credits_block_reason())
        self.runtime.ledger.grant("k0", 1.0)
        self.assertEqual(self.runtime.credits_block_reason(), "")
        self.config.update({"credits_enforce": False})
        self.assertEqual(self.runtime.credits_block_reason(), "")
        self.config.update({"credits_enforce": True, "credits_enabled": False})
        self.assertEqual(self.runtime.credits_block_reason(), "")

    def test_panel_requests_without_a_key_are_never_blocked(self):
        self.runtime.local.handler = _Handler(None)
        self.assertEqual(self.runtime.credits_block_reason(), "")

    def test_quote_reports_the_night_rate(self):
        quote = self.runtime.credit_quote("codeart/deepseek-v4.1-flash",
                                          at=local_ts(1))
        self.assertEqual(quote["multiplier"], 0.10)
        self.assertTrue(quote["night_rate"])

    def test_grant_from_the_panel_returns_the_view(self):
        view = self.runtime.grant_credits({"key_id": "k0", "amount": 12.5,
                                           "note": "测试充值"})
        self.assertEqual({item["id"]: item for item in view["keys"]}["k0"]["balance"], 12.5)
        self.assertEqual(view["events"][0]["note"], "测试充值")

    def test_bad_grant_payloads_are_rejected(self):
        for payload in ({"key_id": "k0", "amount": 0},
                        {"key_id": "", "amount": 1},
                        {"key_id": "k0", "amount": "1"},
                        {"key_id": "k0", "amount": True},
                        {"key_id": "k0", "amount": 1, "note": "x" * 201},
                        {"key_id": "k0", "amount": 1e18},
                        "not-an-object"):
            with self.assertRaises(ValueError, msg=repr(payload)):
                self.runtime.grant_credits(payload)


if __name__ == "__main__":
    unittest.main()
