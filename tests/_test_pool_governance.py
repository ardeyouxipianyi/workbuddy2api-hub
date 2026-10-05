"""Panel-parity pool governance: weighted picking + backoff state machine.

Run with: python _test_pool_governance.py
No upstream credentials or outbound network are used.
"""
import os
import random
import sys
import tempfile
import time
import unittest
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="pool-governance-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_pool
import wb_settings


def make_account(uid, realm="cn", credits=None):
    data = {"uid": uid, "realm": realm, "accessToken": "token"}
    if credits is not None:
        data["credits"] = {"remain": credits}
    return wb_accounts.Account(data)


class BackoffMathTests(unittest.TestCase):
    def test_soft_backoff_grows_then_caps(self):
        self.assertEqual(wb_pool.soft_backoff(1, 600, 7200), 600)
        self.assertEqual(wb_pool.soft_backoff(2, 600, 7200), 1200)
        self.assertEqual(wb_pool.soft_backoff(4, 600, 7200), 4800)
        self.assertEqual(wb_pool.soft_backoff(9, 600, 7200), 7200)

    def test_breaker_backoff_starts_at_threshold_and_caps(self):
        self.assertEqual(wb_pool.breaker_backoff(3, 3, 1800, 21600), 1800)
        self.assertEqual(wb_pool.breaker_backoff(4, 3, 1800, 21600), 3600)
        self.assertEqual(wb_pool.breaker_backoff(99, 3, 1800, 21600), 21600)

    def test_degrade_uses_same_shape(self):
        self.assertEqual(wb_pool.degrade_backoff(5, 5, 600, 7200), 600)
        self.assertEqual(wb_pool.degrade_backoff(6, 5, 600, 7200), 1200)

    def test_normalize_clamps_and_drops_unknown(self):
        cfg = wb_pool.normalize({"soft_rate": -5, "top_n": 0,
                                 "weighted_pick": False, "bogus": 1})
        self.assertEqual(cfg["soft_rate"], wb_pool.DEFAULTS["soft_rate"])
        self.assertEqual(cfg["top_n"], wb_pool.DEFAULTS["top_n"])
        self.assertFalse(cfg["weighted_pick"])
        self.assertNotIn("bogus", cfg)
        self.assertEqual(cfg["breaker_threshold"],
                         wb_pool.DEFAULTS["breaker_threshold"])
        # Audit #11: 0/1 are honoured, other non-bools fall back to default.
        self.assertFalse(wb_pool.normalize({"weighted_pick": 0})["weighted_pick"])
        self.assertTrue(wb_pool.normalize({"weighted_pick": 1})["weighted_pick"])
        self.assertTrue(wb_pool.normalize(
            {"weighted_pick": "no"})["weighted_pick"])

    def test_validate_patch_rejects_bad_shapes(self):
        with self.assertRaises(ValueError):
            wb_pool.validate_patch({"weighted_pick": "yes"})
        with self.assertRaises(ValueError):
            wb_pool.validate_patch({"breaker_threshold": True})
        with self.assertRaises(ValueError):
            wb_pool.validate_patch({"soft_rate": -1})
        with self.assertRaises(ValueError):
            wb_pool.validate_patch({"unknown_setting": 1})
        self.assertEqual(wb_pool.validate_patch({"weighted_pick": False,
                                                 "soft_rate": 30}),
                         {"weighted_pick": False, "soft_rate": 30})


class AccountGovernanceTests(unittest.TestCase):
    def test_soft_rate_streak_grows_and_success_resets(self):
        account = make_account("soft")
        with mock.patch.object(wb_accounts.time, "time", return_value=1000.0):
            first = account.note_soft_rate("429")
            second = account.note_soft_rate("429")
            self.assertEqual((first, second), (600.0, 1200.0))
            self.assertEqual(account.soft_streak, 2)
            self.assertFalse(account.ready())
            self.assertEqual(account.public()["softStreak"], 2)
        account.note_success()
        self.assertEqual(account.soft_streak, 0)
        self.assertTrue(account.ready())

    def test_breaker_trips_at_threshold_and_expires(self):
        account = make_account("brk")
        now = [1000.0]
        with mock.patch.object(wb_accounts.time, "time",
                               side_effect=lambda: now[0]):
            account.note_failure("500")
            account.note_failure("502")
            self.assertTrue(account.ready())
            account.note_failure("503")
            self.assertFalse(account.ready())
            self.assertAlmostEqual(account.breaker_until - now[0], 1800.0, places=3)
            now[0] += 2000.0
            self.assertTrue(account.ready())
        account.note_success()
        self.assertEqual(account.fails, 0)
        self.assertEqual(account.breaker_until, 0.0)

    def test_degrade_window_parks_unknown_failures(self):
        account = make_account("degrade")
        account.pool_cfg = wb_pool.normalize({"breaker_threshold": 99,
                                              "degrade_threshold": 3,
                                              "degrade_cooldown": 120})
        now = [2000.0]
        with mock.patch.object(wb_accounts.time, "time",
                               side_effect=lambda: now[0]):
            account.note_unknown_failure("boom")
            account.note_unknown_failure("boom")
            self.assertTrue(account.ready())
            account.note_unknown_failure("boom")
            self.assertFalse(account.ready())
            self.assertAlmostEqual(account.degrade_until - now[0], 120.0, places=3)
        account.note_success()
        self.assertEqual(account.degrade_count, 0)
        self.assertEqual(account.degrade_until, 0.0)

    def test_in_flight_cap_parks_and_releases(self):
        account = make_account("lease")
        account.max_in_flight = 2
        self.assertTrue(account.acquire())
        self.assertTrue(account.acquire())
        self.assertFalse(account.acquire())
        self.assertFalse(account.ready())
        self.assertEqual(account.public()["inFlight"], 2)
        account.release()
        self.assertTrue(account.ready())
        self.assertEqual(account.public()["inFlight"], 1)


class WeightedPickTests(unittest.TestCase):
    def test_idle_weight_prefers_never_used(self):
        idle = make_account("idle", credits=100)
        fresh = make_account("fresh", credits=100)
        idle.last_used_at = time.time() - 3600
        fresh.last_used_at = 0.0
        weights = wb_pool.weights_for([idle, fresh], wb_pool.normalize(None),
                                      time.time())
        self.assertGreater(weights[1], weights[0])

    def test_unknown_credits_are_not_starved(self):
        known = make_account("known", credits=1000)
        unknown = make_account("unknown")
        weights = wb_pool.weights_for([known, unknown], wb_pool.normalize(None),
                                      time.time())
        self.assertGreaterEqual(weights[1], weights[0])

    def test_credits_share_drives_the_draw(self):
        rich = make_account("rich", credits=1000)
        poor = make_account("poor", credits=100)
        cfg = wb_pool.normalize(None)
        rng = random.Random(7)
        now = time.time()
        counts = {"rich": 0, "poor": 0}
        for _ in range(400):
            counts[wb_pool.choose([rich, poor], cfg, now=now, rng=rng).uid] += 1
        self.assertGreater(counts["rich"], counts["poor"] * 2)

    def test_min_pick_gap_skips_just_used_candidate(self):
        accounts = [make_account("a%d" % i, credits=100) for i in range(5)]
        now = time.time()
        accounts[0].last_used_at = now
        cfg = wb_pool.normalize({"min_pick_gap": 0.1})
        rng = random.Random(3)
        for _ in range(50):
            picked = wb_pool.choose(accounts, cfg, now=now, rng=rng)
            self.assertNotEqual(picked.uid, "a0")

    def test_pool_pick_uses_weighted_choice_and_stamps_idle(self):
        pool = wb_accounts.AccountPool(_startup_dir.name)
        first = make_account("first", credits=100)
        second = make_account("second", credits=100)
        pool.accounts = [first, second]
        pool.apply_pool_config(wb_pool.normalize(None))
        with mock.patch.object(wb_accounts.wb_pool, "choose",
                               return_value=second) as choose:
            picked = pool.pick()
        self.assertIs(picked, second)
        self.assertGreater(second.last_used_at, 0.0)
        choose.assert_called_once()

    def test_weighted_off_restores_cursor_round_robin(self):
        pool = wb_accounts.AccountPool(_startup_dir.name)
        first = make_account("first")
        second = make_account("second")
        pool.accounts = [first, second]
        pool.apply_pool_config(wb_pool.normalize({"weighted_pick": False}))
        self.assertEqual(pool.pick().uid, "first")
        self.assertEqual(pool.pick().uid, "second")
        self.assertEqual(pool.pick().uid, "first")


class PoolSettingsTests(unittest.TestCase):
    def test_pool_config_persists_and_merges(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = wb_settings.pool_config(directory)
            self.assertTrue(cfg["weighted_pick"])
            wb_settings.set_pool_config(directory,
                                        {"soft_rate": 30, "weighted_pick": False})
            again = wb_settings.pool_config(directory)
            self.assertEqual(again["soft_rate"], 30.0)
            self.assertFalse(again["weighted_pick"])
            self.assertEqual(again["breaker_threshold"],
                             wb_pool.DEFAULTS["breaker_threshold"])

    def test_apply_pool_config_sets_realm_caps(self):
        pool = wb_accounts.AccountPool(_startup_dir.name)
        cn = make_account("cn", realm="cn")
        intl = make_account("intl", realm="intl")
        pool.accounts = [cn, intl]
        pool.apply_pool_config(wb_pool.normalize({"max_in_flight": 3,
                                                  "max_in_flight_global": 2}))
        self.assertEqual(cn.max_in_flight, 3)
        self.assertEqual(intl.max_in_flight, 2)
        self.assertEqual(pool.affinity.ttl, 7200)
        # Audit #9: global=0 falls back to the per-account cap; only
        # max_in_flight=0 means unlimited (leases off).
        pool.apply_pool_config(wb_pool.normalize({"max_in_flight": 4,
                                                  "max_in_flight_global": 0}))
        self.assertEqual(cn.max_in_flight, 4)
        self.assertEqual(intl.max_in_flight, 4)
        pool.apply_pool_config(wb_pool.normalize({"max_in_flight": 0,
                                                  "max_in_flight_global": 0}))
        self.assertEqual(cn.max_in_flight, 0)
        self.assertEqual(intl.max_in_flight, 0)


class LeaseWrapperTests(unittest.TestCase):
    def test_lease_released_when_wrapped_response_closes(self):
        import wb_proxy

        account = make_account("wrapped")
        account.max_in_flight = 1
        self.assertTrue(account.acquire())

        class FakeResponse(object):
            def __init__(self):
                self.closed = False
                self.lines = [b"data: {}\n"]

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                self.close()
                return False

            def __iter__(self):
                return iter(self.lines)

            def close(self):
                self.closed = True

        fake = FakeResponse()
        wrapped = wb_proxy._LeasedResponse(fake, account)
        with wrapped:
            for _line in wrapped:
                pass
        self.assertTrue(fake.closed)
        self.assertEqual(account.in_flight, 0)
        self.assertTrue(account.acquire())

    def test_close_without_context_still_releases(self):
        import wb_proxy

        account = make_account("closed-directly")
        self.assertTrue(account.acquire())

        class FakeResponse(object):
            def close(self):
                pass

        wrapped = wb_proxy._LeasedResponse(FakeResponse(), account)
        wrapped.close()
        wrapped.close()
        self.assertEqual(account.in_flight, 0)


class RateLimitClassifierTests(unittest.TestCase):
    def test_reset_time_means_model_scoped(self):
        import wb_proxy

        self.assertFalse(wb_proxy.rate_limit_is_account_level("usage exceeds frequency limit",
                                                              time.time() + 60))

    def test_missing_reset_time_means_account_soft_limit(self):
        import wb_proxy

        self.assertTrue(wb_proxy.rate_limit_is_account_level("usage exceeds frequency limit",
                                                             None))
        self.assertTrue(wb_proxy.rate_limit_is_account_level("slow down", None))


class NextLocal4amTests(unittest.TestCase):
    def test_returns_the_next_local_four_am(self):
        lt = time.localtime()
        now = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 3, 0, 0, 0, 0, -1))
        until = wb_pool.next_local_4am(now)
        self.assertGreater(until, now)
        self.assertLessEqual(until - now, 24 * 3600)
        self.assertEqual(time.localtime(until).tm_hour, 4)
        self.assertAlmostEqual(until - now, 3600, delta=2)


class BalanceCooldownTests(unittest.TestCase):
    def test_402_parks_until_a_balance_refresh_shows_credits(self):
        account = make_account("balance")
        account.note_balance_cooled("HTTP 402")
        self.assertFalse(account.ready())
        self.assertIsNotNone(account.public()["balanceCooledFor"])
        account.credits = {"remain": 0}
        self.assertFalse(account.revive_balance_cooldown())
        self.assertFalse(account.ready())
        account.credits = {"remain": 500}
        self.assertTrue(account.revive_balance_cooldown())
        self.assertTrue(account.ready())
        self.assertIsNone(account.public()["balanceCooledFor"])


class SessionDeadTests(unittest.TestCase):
    def test_three_consecutive_reports_disable_the_account(self):
        account = make_account("dead")
        account.pool_cfg = wb_pool.normalize({"session_dead_threshold": 3})
        self.assertFalse(account.note_session_dead("12153"))
        self.assertFalse(account.note_session_dead("12153"))
        self.assertTrue(account.note_session_dead("12153"))
        self.assertFalse(account.enabled)
        account.enabled = True
        account.mark_manual_revive()
        self.assertEqual(account.session_dead_fails, 0)

    def test_success_clears_the_session_dead_counter(self):
        account = make_account("recovered")
        account.pool_cfg = wb_pool.normalize({"session_dead_threshold": 3})
        self.assertFalse(account.note_session_dead("12153"))
        account.note_success()
        self.assertEqual(account.session_dead_fails, 0)
        self.assertFalse(account.note_session_dead("12153"))


class CostLedgerTests(unittest.TestCase):
    def make_pool(self, *accounts):
        pool = wb_accounts.AccountPool(_startup_dir.name)
        pool.accounts = list(accounts)
        pool.apply_pool_config(wb_pool.normalize(None))
        return pool

    def test_note_model_cost_marks_free_and_paid(self):
        pool = self.make_pool()
        now = [5000.0]
        with mock.patch.object(wb_accounts.time, "time",
                               side_effect=lambda: now[0]):
            pool.note_model_cost("a", "m-free", 0)
            pool.note_model_cost("a", "m-paid", 0.35)
        self.assertEqual(pool.cost_ledger[("a", "m-free")]["tier"], 0)
        self.assertEqual(pool.cost_ledger[("a", "m-paid")]["tier"], 2)

    def test_cost_layer_prefers_free_then_unknown(self):
        free = make_account("free")
        paid = make_account("paid")
        unknown = make_account("unknown")
        pool = self.make_pool(free, paid, unknown)
        cfg = wb_pool.normalize({"cost_explore_interval": 0})
        now = [5000.0]
        with mock.patch.object(wb_accounts.time, "time",
                               side_effect=lambda: now[0]):
            pool.note_model_cost("free", "m", 0)
        chosen = pool._apply_cost_layer([free, paid, unknown], "m", cfg, now[0])
        self.assertEqual([a.uid for a in chosen], ["free"])
        # Keep the paid observation fresh while the free one ages out: the
        # unknown layer is then preferred over the measured-paid one.
        now[0] += cfg["cost_ledger_ttl"] - 10
        with mock.patch.object(wb_accounts.time, "time",
                               side_effect=lambda: now[0]):
            pool.note_model_cost("paid", "m", 0.2)
        now[0] += 11
        chosen = pool._apply_cost_layer([free, paid, unknown], "m", cfg, now[0])
        self.assertEqual(sorted(a.uid for a in chosen), ["free", "unknown"])

    def test_exploration_routes_to_unknown_then_back(self):
        free = make_account("free")
        unknown = make_account("unknown")
        pool = self.make_pool(free, unknown)
        cfg = wb_pool.normalize({"cost_explore_interval": 100})
        now = [9000.0]
        with mock.patch.object(wb_accounts.time, "time",
                               side_effect=lambda: now[0]):
            pool.note_model_cost("free", "m", 0)
        first = pool._apply_cost_layer([free, unknown], "m", cfg, now[0])
        self.assertEqual([a.uid for a in first], ["unknown"])
        second = pool._apply_cost_layer([free, unknown], "m", cfg, now[0] + 10)
        self.assertEqual([a.uid for a in second], ["free"])
        third = pool._apply_cost_layer([free, unknown], "m", cfg, now[0] + 101)
        self.assertEqual([a.uid for a in third], ["unknown"])

    def test_credit_floor_only_blocks_measured_paid_below_floor(self):
        free = make_account("free", credits=10)
        paid = make_account("paid", credits=50)
        rich = make_account("rich", credits=500)
        unknown = make_account("unknown")
        pool = self.make_pool(free, paid, rich, unknown)
        cfg = wb_pool.normalize({"credit_floor": 100})
        now = [7000.0]
        with mock.patch.object(wb_accounts.time, "time",
                               side_effect=lambda: now[0]):
            pool.note_model_cost("free", "m", 0)
            pool.note_model_cost("paid", "m", 0.2)
            pool.note_model_cost("rich", "m", 0.2)
        kept = pool._apply_credit_floor([free, paid, rich, unknown], "m",
                                        cfg, now[0])
        self.assertEqual(sorted(a.uid for a in kept), ["free", "rich", "unknown"])

    def test_pick_returns_none_when_every_candidate_is_floor_blocked(self):
        paid = make_account("paid", credits=1)
        pool = self.make_pool(paid)
        pool.apply_pool_config(wb_pool.normalize({"credit_floor": 100}))
        now = [8000.0]
        with mock.patch.object(wb_accounts.time, "time",
                               side_effect=lambda: now[0]):
            pool.note_model_cost("paid", "m", 0.5)
            picked = pool.pick(model="m")
        self.assertIsNone(picked)


class CostRecordingTests(unittest.TestCase):
    def test_record_usage_feeds_the_cost_ledger(self):
        import wb_proxy

        seen = []

        class StubPool(object):
            def get(self, uid):
                return None

            def note_model_cost(self, uid, model, credit):
                seen.append((uid, model, credit))

        old = wb_proxy.POOL
        wb_proxy.POOL = StubPool()
        try:
            wb_proxy.record_usage("m", {"total_tokens": 10, "credit": 0.25},
                                  account="acct-1")
        finally:
            wb_proxy.POOL = old
        self.assertEqual(seen, [("acct-1", "m", 0.25)])

    def test_missing_credit_stays_unknown_not_free(self):
        """BUG-4: no credit field -> do not write tier 0."""
        import wb_proxy

        seen = []

        class StubPool(object):
            def get(self, uid):
                return None

            def note_model_cost(self, uid, model, credit):
                seen.append((uid, model, credit))

        old = wb_proxy.POOL
        wb_proxy.POOL = StubPool()
        try:
            wb_proxy.record_usage("m", {"total_tokens": 10}, account="acct-1")
        finally:
            wb_proxy.POOL = old
        self.assertEqual(seen, [])

    def test_explicit_zero_credit_still_records_free(self):
        import wb_proxy

        seen = []

        class StubPool(object):
            def get(self, uid):
                return None

            def note_model_cost(self, uid, model, credit):
                seen.append((uid, model, credit))

        old = wb_proxy.POOL
        wb_proxy.POOL = StubPool()
        try:
            wb_proxy.record_usage("m", {"total_tokens": 10, "credit": 0},
                                  account="acct-1")
        finally:
            wb_proxy.POOL = old
        self.assertEqual(seen, [("acct-1", "m", 0)])


class PoolExhaustionReportingTests(unittest.TestCase):
    def test_status_mapping_agrees_between_row_and_client(self):
        import wb_proxy

        self.assertEqual(wb_proxy.upstream_error_status(
            "no usable account for realm 'cn': all are busy"), 503)
        self.assertEqual(wb_proxy.upstream_error_status("connection reset"), 502)
        self.assertEqual(wb_proxy.upstream_error_status(""), 502)

    def test_busy_accounts_are_named_in_the_reason(self):
        import wb_proxy

        class Fake(object):
            pass

        busy = Fake()
        busy.max_in_flight = 3
        busy.in_flight = 3
        idle = Fake()
        idle.max_in_flight = 3
        idle.in_flight = 0
        busy_msg = wb_proxy.no_usable_account_message("cn", [busy])
        idle_msg = wb_proxy.no_usable_account_message("cn", [idle])
        self.assertIn("busy", busy_msg)
        self.assertIn("in-flight cap", busy_msg)
        self.assertNotIn("busy", idle_msg)
        self.assertIn("disabled", idle_msg)

    def test_handler_records_and_answers_with_the_same_status(self):
        import wb_proxy

        calls = []

        class Handler(wb_proxy.Handler):
            def __init__(self):
                pass

            def _error(self, status, message, kind=""):
                return status, message

        handler = Handler()
        with mock.patch.object(wb_proxy, "record_error",
                               side_effect=lambda *a, **k: calls.append((a, k))):
            code, _message = handler._open_upstream_error(
                RuntimeError("no usable account for realm 'cn': all are busy"),
                "m", 0.0)
        self.assertEqual(code, 503)
        self.assertEqual(calls[0][0][1], 503)
        calls.clear()
        with mock.patch.object(wb_proxy, "record_error",
                               side_effect=lambda *a, **k: calls.append((a, k))):
            code, _message = handler._open_upstream_error(
                RuntimeError("connection reset"), "m", 0.0)
        self.assertEqual(code, 502)
        self.assertEqual(calls[0][0][1], 502)


if __name__ == "__main__":
    unittest.main(verbosity=1)
