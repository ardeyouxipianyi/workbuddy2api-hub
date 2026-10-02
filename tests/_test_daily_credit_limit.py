"""Two more daily guards: the credit cap and the per-model token cap.

Both fold usage.jsonl the same way the daily token guard folds tokens - rows
at/after local midnight, cancellations skipped - but they refuse less than
the whole account:

  - the credit guard caps *paid* models once the day's spend is reached,
    while the models the catalogue marks free ("x0.00") keep serving;
  - the per-model token guard caps exactly the one model that burned its
    budget, leaving every other model on that account working.

The free/paid split depends on the account's realm (the same id can be free
on one exit and paid on the other), which is why the pool receives a
realm -> free-ids view rather than a flat list.

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
_TMP = tempfile.mkdtemp(prefix="wb-credit-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_accounts
import wb_settings
import wb_proxy as P


def row(at, account, model, tokens, credit=0.0, outcome="completed"):
    return {
        "at": at, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at)),
        "model": model, "stream": True, "outcome": outcome,
        "total_tokens": tokens, "credit": credit, "account": account, "realm": "intl",
    }


def reset_cache():
    with P._daily_usage_lock:
        P._daily_usage.update({"day": "", "totals": None, "credits": None,
                               "models": None, "offset": 0, "at": 0.0})


class CreditGuardSettingsTests(unittest.TestCase):
    def test_daily_credit_is_off_until_set(self):
        with tempfile.TemporaryDirectory(prefix="credit-set-") as directory:
            # Off by default, exactly like the other guards here: an install
            # that upgrades into this feature keeps behaving as before.
            self.assertEqual(wb_settings.daily_credit_limit(directory), 0)
            self.assertEqual(wb_settings.set_daily_credit_limit(directory, 80), 80)
            self.assertEqual(wb_settings.daily_credit_limit(directory), 80)
            self.assertEqual(wb_settings.set_daily_credit_limit(directory, 0), 0)
            self.assertEqual(wb_settings.daily_credit_limit(directory), 0)
            # Garbage and negatives collapse to "off" instead of raising.
            self.assertEqual(wb_settings.set_daily_credit_limit(directory, -5), 0)
            self.assertEqual(wb_settings.set_daily_credit_limit(directory, "abc"), 0)
            # A corrupt stored value reads back as off too.
            with open(wb_settings.settings_path(directory), "w", encoding="utf-8") as fh:
                json.dump({"daily_credit_limit": "lots"}, fh)
            self.assertEqual(wb_settings.daily_credit_limit(directory), 0)

    def test_model_daily_token_defaults_to_unlimited(self):
        with tempfile.TemporaryDirectory(prefix="credit-set-") as directory:
            self.assertEqual(wb_settings.model_daily_token_limit(directory), 0)
            self.assertEqual(
                wb_settings.set_model_daily_token_limit(directory, 200000000), 200000000)
            self.assertEqual(
                wb_settings.model_daily_token_limit(directory), 200000000)
            self.assertEqual(wb_settings.set_model_daily_token_limit(directory, -5), 0)
            self.assertEqual(wb_settings.set_model_daily_token_limit(directory, "abc"), 0)


class DailyUsageFoldTests(unittest.TestCase):
    def setUp(self):
        reset_cache()

    def test_scan_folds_credits_and_per_model_tokens(self):
        midnight = P._local_midnight()
        rows = [
            row(midnight - 3600, "acct-A", "gpt-6-astra", 1000, credit=99.0),
            row(midnight + 60, "acct-A", "gpt-6-astra", 1000, credit=30.5),
            row(midnight + 120, "acct-A", "deepseek-v4.1-flash", 500, credit=0),
            row(midnight + 180, "acct-B", "hy4-preview", 700, credit=2.5),
            row(midnight + 240, "acct-A", "gpt-6-astra", 100, credit=1.0,
                outcome="client_aborted"),
        ]
        with io.open(P.USAGE_LOG, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        stats = P.daily_usage_stats(ttl=0)
        # Yesterday's rows stay out, and a cancellation is not a consumed
        # request - neither its tokens nor its credit count.
        self.assertEqual(stats["tokens"], {"acct-A": 1500, "acct-B": 700})
        self.assertEqual(stats["credits"], {"acct-A": 30.5, "acct-B": 2.5})
        self.assertEqual(stats["models"], {
            "acct-A": {"gpt-6-astra": 1000, "deepseek-v4.1-flash": 500},
            "acct-B": {"hy4-preview": 700},
        })
        # The original reader keeps its shape for existing callers.
        self.assertEqual(P.daily_tokens_by_account(ttl=0),
                         {"acct-A": 1500, "acct-B": 700})

        # The fold resumes from its byte offset: a row appended after the
        # first scan is folded into every view without recounting the file.
        with io.open(P.USAGE_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row(midnight + 300, "acct-B", "glm-5.3", 100,
                                    credit=1.5)) + "\n")
        stats = P.daily_usage_stats(ttl=0)
        self.assertEqual(stats["tokens"]["acct-B"], 800)
        self.assertEqual(stats["credits"]["acct-B"], 4.0)
        self.assertEqual(stats["models"]["acct-B"], {"hy4-preview": 700, "glm-5.3": 100})


class CreditGuardAccountTests(unittest.TestCase):
    def test_guard_blocks_paid_models_and_keeps_free_ones(self):
        account = wb_accounts.Account({"uid": "uid-a", "accessToken": "t",
                                      "realm": "intl"})
        account.daily_credit_limit = 50
        account.free_models = frozenset({"deepseek-v4.1-flash"})
        # Not counted yet: nothing blocks.
        self.assertFalse(account.credit_limit_reached())
        self.assertFalse(account.credit_limit_blocked("gpt-6-astra"))
        self.assertTrue(account.ready("gpt-6-astra"))
        account.daily_credits_today = 49.9
        self.assertFalse(account.credit_limit_reached())
        account.daily_credits_today = 50.0
        self.assertTrue(account.credit_limit_reached())
        self.assertTrue(account.credit_limit_blocked("gpt-6-astra"))
        self.assertFalse(account.ready("gpt-6-astra"))
        # The free model keeps serving after the cap is reached.
        self.assertFalse(account.credit_limit_blocked("deepseek-v4.1-flash"))
        self.assertTrue(account.ready("deepseek-v4.1-flash"))
        # Unknown model (None): the caller cannot tell whether this request
        # would spend anything, so it is not blocked here.
        self.assertFalse(account.credit_limit_blocked(None))
        self.assertTrue(account.ready())
        # An unknown id is treated as paid: the guard fails closed.
        self.assertTrue(account.credit_limit_blocked("brand-new-model"))
        # Turning the guard off restores everything at once.
        account.daily_credit_limit = 0
        self.assertTrue(account.ready("gpt-6-astra"))

    def test_model_guard_blocks_only_that_model(self):
        account = wb_accounts.Account({"uid": "uid-b", "accessToken": "t"})
        account.model_daily_token_limit = 200000000
        self.assertFalse(account.model_token_limit_blocked("hy4-preview"))
        account.model_daily_tokens = {"hy4-preview": 199999999,
                                      "glm-5.3": 300000000}
        self.assertFalse(account.model_token_limit_blocked("hy4-preview"))
        self.assertTrue(account.ready("hy4-preview"))
        self.assertTrue(account.model_token_limit_blocked("glm-5.3"))
        self.assertFalse(account.ready("glm-5.3"))
        account.model_daily_tokens["hy4-preview"] = 200000000
        self.assertTrue(account.model_token_limit_blocked("hy4-preview"))
        self.assertFalse(account.ready("hy4-preview"))
        # Other models on the same account are untouched.
        self.assertTrue(account.ready("kimi-k3"))
        self.assertEqual(account.blocked_model_names(),
                         {"hy4-preview", "glm-5.3"})
        # No model to judge: never blocked by this guard.
        self.assertFalse(account.model_token_limit_blocked(None))
        account.model_daily_token_limit = 0
        self.assertTrue(account.ready("glm-5.3"))


class CreditGuardPoolTests(unittest.TestCase):
    def test_pool_caps_credit_per_account_and_publishes_state(self):
        with tempfile.TemporaryDirectory(prefix="credit-pool-") as directory:
            spent = wb_accounts.Account({"uid": "uid-spent", "accessToken": "t",
                                         "realm": "intl"})
            fresh = wb_accounts.Account({"uid": "uid-fresh", "accessToken": "t",
                                         "realm": "intl"})
            pool = wb_accounts.AccountPool(directory, log=lambda _m: None)
            pool.accounts = [spent, fresh]
            pool.apply_daily_credit_limit(
                50, {"uid-spent": 60.0, "uid-fresh": 10.0},
                {"intl": {"deepseek-v4.1-flash"}})
            # A paid model rotates away from the capped account...
            for _ in range(3):
                self.assertEqual(pool.pick(realm="intl", model="gpt-6-astra").uid,
                                 "uid-fresh")
            self.assertEqual(pool.count_ready(realm="intl", model="gpt-6-astra"), 1)
            # ...while the free one still reaches both.
            self.assertEqual(pool.count_ready(realm="intl",
                                              model="deepseek-v4.1-flash"), 2)
            row = [a for a in pool.list_public(realm="intl")
                   if a["uid"] == "uid-spent"][0]
            self.assertTrue(row["creditLimitReached"])
            self.assertEqual(row["dailyCreditsToday"], 60.0)
            self.assertEqual(row["dailyCreditLimit"], 50)
            # None keeps the last known counts when only the setting changes.
            pool.apply_daily_credit_limit(0)
            self.assertFalse(pool.pick(realm="intl", model="gpt-6-astra") is None)
            self.assertEqual(spent.daily_credits_today, 60.0)

    def test_pool_caps_one_model_per_account(self):
        with tempfile.TemporaryDirectory(prefix="credit-model-") as directory:
            a = wb_accounts.Account({"uid": "uid-a", "accessToken": "t",
                                     "realm": "intl"})
            b = wb_accounts.Account({"uid": "uid-b", "accessToken": "t",
                                     "realm": "intl"})
            pool = wb_accounts.AccountPool(directory, log=lambda _m: None)
            pool.accounts = [a, b]
            pool.apply_model_daily_token_limit(1000, {"uid-a": {"hy4-preview": 1500}})
            self.assertTrue(a.model_token_limit_blocked("hy4-preview"))
            # Another account's budget is its own.
            self.assertFalse(b.model_token_limit_blocked("hy4-preview"))
            self.assertTrue(b.ready("hy4-preview"))
            self.assertEqual(pool.count_ready(realm="intl", model="hy4-preview"), 1)
            # The capped account keeps its other models.
            self.assertTrue(a.ready("glm-5.3"))
            row = [a for a in pool.list_public(realm="intl")
                   if a["uid"] == "uid-a"][0]
            self.assertEqual(row["modelDailyTokenLimit"], 1000)
            self.assertEqual(row["modelDailyTokens"], {"hy4-preview": 1500})

    def _stub_pool(self, accounts):
        class Pool(object):
            def __init__(self):
                self.accounts = accounts

            def count_ready(self, realm, model=None):
                return sum(a.ready(model=model) for a in accounts)

            def pick_for_session(self, realm, session_key=None, exclude=(),
                                 model=None):
                return next((a for a in accounts if a.uid not in exclude
                             and a.realm == realm and a.ready(model=model)), None)

            def apply_daily_token_limit(self, value=None, usage=None):
                return value or 0

            def apply_daily_credit_limit(self, value=None, credits=None,
                                         free_models=None):
                return value or 0

            def apply_model_daily_token_limit(self, value=None, per_model=None):
                return value or 0
        return Pool()

    def test_pool_wide_credit_cap_answers_429_with_its_own_message(self):
        account = wb_accounts.Account({"uid": "uid-cap", "accessToken": "t",
                                      "realm": "intl"})
        account.daily_credit_limit = 50
        account.daily_credits_today = 50.0

        old_pool = P.POOL
        P.POOL = self._stub_pool([account])
        try:
            with self.assertRaises(P.RateLimited) as caught:
                P.open_upstream(
                    {"model": "gpt-6-astra",
                     "messages": [{"role": "user", "content": "hi"}]},
                    target_realm="intl")
        finally:
            P.POOL = old_pool
        self.assertIn("credit limit", str(caught.exception.message))
        self.assertIn("free models", str(caught.exception.message))
        self.assertGreaterEqual(caught.exception.wait, 60)

    def test_pool_wide_model_cap_answers_429_with_its_own_message(self):
        account = wb_accounts.Account({"uid": "uid-model", "accessToken": "t",
                                      "realm": "intl"})
        account.model_daily_token_limit = 1000
        account.model_daily_tokens = {"gpt-6-astra": 1000}

        old_pool = P.POOL
        P.POOL = self._stub_pool([account])
        try:
            with self.assertRaises(P.RateLimited) as caught:
                P.open_upstream(
                    {"model": "gpt-6-astra",
                     "messages": [{"role": "user", "content": "hi"}]},
                    target_realm="intl")
        finally:
            P.POOL = old_pool
        self.assertIn("token limit for gpt-6-astra", str(caught.exception.message))
        self.assertGreaterEqual(caught.exception.wait, 60)


class FreeModelViewTests(unittest.TestCase):
    def test_free_table_is_built_per_realm_from_the_catalogue(self):
        view = P.free_models_by_realm()
        # The bundled snapshot marks this one free on the international exit;
        # the domestic catalogue carries it as paid, so it must not appear
        # there - the same id, two answers.
        self.assertIn("deepseek-v4.1-flash", view["intl"])
        self.assertNotIn("deepseek-v4.1-flash", view["cn"])
        self.assertIn("hy4-preview-f", view["intl"])
        self.assertIn("hy4-preview-f", view["cn"])
        # A paid id is in neither table.
        self.assertNotIn("gpt-6-astra", view["intl"])
        self.assertTrue(P.credits_is_free("x0.00"))
        self.assertTrue(P.credits_is_free(" x0.00 "))
        self.assertFalse(P.credits_is_free("x0.34 credits"))
        self.assertFalse(P.credits_is_free(""))
        self.assertFalse(P.credits_is_free(None))


if __name__ == "__main__":
    unittest.main()
