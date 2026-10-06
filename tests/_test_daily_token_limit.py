"""The daily token guard parks an account once today's usage reaches the limit.

The upstream caps a free window at a fixed token budget (code 6004), and by the
time it answers 429 the day is already spent. This guard lets the operator park
an account at a local threshold instead, so the next request rotates away
before the upstream has to refuse. Two things matter and are pinned here: the
counter folds usage.jsonl incrementally (only rows at/after local midnight,
skipping client cancellations) and an account only blocks on a *counted* day -
an unknown count must never park anyone.

No network: the usage log is synthesised in a temp directory.
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-daily-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_accounts
import wb_settings
import wb_proxy as P


def row(at, account, total, outcome="completed"):
    return {
        "at": at, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at)),
        "model": "deepseek-v4.1-flash", "stream": True, "outcome": outcome,
        "total_tokens": total, "account": account, "realm": "intl",
    }


def reset_cache():
    with P._daily_usage_lock:
        P._daily_usage.update({"day": "", "totals": None, "offset": 0, "at": 0.0})


class DailyTokenLimitTests(unittest.TestCase):
    def setUp(self):
        reset_cache()

    def test_setting_round_trip(self):
        with tempfile.TemporaryDirectory(prefix="daily-set-") as directory:
            self.assertEqual(wb_settings.daily_token_limit(directory), 0)
            self.assertEqual(
                wb_settings.set_daily_token_limit(directory, 200000000), 200000000)
            self.assertEqual(wb_settings.daily_token_limit(directory), 200000000)
            # Garbage and negatives collapse to "off" instead of raising.
            self.assertEqual(wb_settings.set_daily_token_limit(directory, -5), 0)
            self.assertEqual(wb_settings.set_daily_token_limit(directory, "abc"), 0)

    def test_scan_counts_today_only_and_resumes_incrementally(self):
        midnight = P._local_midnight()
        rows = [
            row(midnight - 3600, "acct-A", 999999),   # yesterday: ignored
            row(midnight + 60, "acct-A", 1200),       # today
            row(midnight + 120, "acct-A", 800),       # today
            row(midnight + 180, "acct-B", 300),       # today
            row(midnight + 240, "acct-A", 7777, outcome="client_aborted"),
        ]
        with io.open(P.USAGE_LOG, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.assertEqual(P.daily_tokens_by_account(ttl=0),
                    {"acct-A": 2000, "acct-B": 300})

        # The counter resumes from its byte offset: a row appended after the
        # first scan is folded in without recounting the file.
        with io.open(P.USAGE_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row(midnight + 300, "acct-B", 100)) + "\n")
        self.assertEqual(P.daily_tokens_by_account(ttl=0),
                    {"acct-A": 2000, "acct-B": 400})

        # A row still being written (no trailing newline) is left for the next
        # scan instead of being half-counted.
        with io.open(P.USAGE_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row(midnight + 400, "acct-A", 50)))
        self.assertEqual(P.daily_tokens_by_account(ttl=0),
                    {"acct-A": 2000, "acct-B": 400})
        with io.open(P.USAGE_LOG, "a", encoding="utf-8") as fh:
            fh.write("\n")
        self.assertEqual(P.daily_tokens_by_account(ttl=0),
                    {"acct-A": 2050, "acct-B": 400})

    def test_guard_blocks_at_threshold_and_needs_a_count(self):
        account = wb_accounts.Account({"uid": "uid-a", "accessToken": "t"})
        account.daily_token_limit = 1000
        self.assertFalse(account.daily_limit_blocked())  # not counted yet
        self.assertTrue(account.ready())
        account.daily_tokens_today = 999
        self.assertFalse(account.daily_limit_blocked())
        self.assertTrue(account.ready())
        account.daily_tokens_today = 1000
        self.assertTrue(account.daily_limit_blocked())
        self.assertFalse(account.ready())
        account.daily_token_limit = 0            # guard off
        self.assertFalse(account.daily_limit_blocked())
        self.assertTrue(account.ready())

    def test_pool_skips_parked_account_and_publishes_state(self):
        with tempfile.TemporaryDirectory(prefix="daily-pool-") as directory:
            pool = wb_accounts.AccountPool(directory)
            pool.accounts = [
                wb_accounts.Account({"uid": "uid-spent", "accessToken": "t",
                               "realm": "intl"}),
                wb_accounts.Account({"uid": "uid-fresh", "accessToken": "t",
                               "realm": "intl"}),
            ]
            pool.apply_daily_token_limit(1000, {"uid-spent": 1000, "uid-fresh": 10})
            self.assertTrue(pool.accounts[0].daily_limit_blocked())
            self.assertEqual({pool.pick(realm="intl").uid for _ in range(4)},
                    {"uid-fresh"})
            self.assertEqual(pool.count_ready(realm="intl"), 1)
            row = [a for a in pool.list_public(realm="intl")
                    if a["uid"] == "uid-spent"][0]
            self.assertTrue(row["dailyLimitBlocked"])
            self.assertEqual(row["dailyTokensToday"], 1000)
            self.assertEqual(row["dailyTokenLimit"], 1000)

    def test_settings_value_reaches_the_pool(self):
        with tempfile.TemporaryDirectory(prefix="daily-reload-") as directory:
            wb_settings.set_daily_token_limit(directory, 500)
            pool = wb_accounts.AccountPool(directory)
            account = wb_accounts.Account({"uid": "uid-one", "accessToken": "t"})
            account.daily_tokens_today = 600
            pool.accounts = [account]
            self.assertFalse(account.daily_limit_blocked())  # limit not applied yet
            pool.apply_daily_token_limit()
            self.assertEqual(account.daily_token_limit, 500)
            self.assertTrue(account.daily_limit_blocked())

            wb_settings.set_daily_token_limit(directory, 0)
            pool.apply_daily_token_limit()
            self.assertFalse(account.daily_limit_blocked())


    def test_pool_wide_park_answers_429_with_its_own_message(self):
        account = wb_accounts.Account({"uid": "uid-spent", "accessToken": "t",
                                      "realm": "intl"})
        account.daily_token_limit = 1000
        account.daily_tokens_today = 1000
        wb_settings.set_daily_token_limit(os.environ["ACCOUNTS_DIR"], 1000)

        class Pool(object):
            accounts = [account]

            def count_ready(self, realm, model=None):
                return sum(a.ready(model=model) for a in self.accounts)

            def pick_for_session(self, realm, session_key=None, exclude=(), model=None):
                return next((a for a in self.accounts if a.uid not in exclude
                            and a.realm == realm
                            and a.ready(model=model)), None)

            def apply_daily_token_limit(self, value=None, usage=None):
                return value or 0

            def apply_daily_credit_limit(self, value=None, credits=None,
                                         free_models=None):
                return value or 0

            def apply_model_daily_token_limit(self, value=None, per_model=None):
                return value or 0

        old_pool = P.POOL
        P.POOL = Pool()
        try:
            with self.assertRaises(P.RateLimited) as caught:
                P.open_upstream(
                    {"model": "deepseek-v4.1-flash",
                     "messages": [{"role": "user", "content": "hi"}]},
                    target_realm="intl")
        finally:
            P.POOL = old_pool
            wb_settings.set_daily_token_limit(os.environ["ACCOUNTS_DIR"], 0)
        self.assertIn("token limit", str(caught.exception.message))
        self.assertIn("local midnight", str(caught.exception.message))
        self.assertGreaterEqual(caught.exception.wait, 60)

if __name__ == "__main__":
    unittest.main()
