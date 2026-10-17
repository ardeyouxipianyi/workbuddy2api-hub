"""Pin the egress-level transient backoff (issue #271).

Account-level governance punishes the *account* for three consecutive hard
failures (30 minutes, doubling to 6h). When the failure is a transport blip on
the outbound path, that accounting charges every account sharing the path: the
incident behind issue #271 saw 196 `URLError`s over four hours park 13 accounts
for up to ~3h. With the switch on, transport failures and 502/503/504 are
charged to the *egress* instead - one shared counter, seconds-level backoff,
cleared by a served request - and the account's fails/degrade counters are left
alone. A 500 stays on the account (it can be a real fault tied to the
credential); 401/403/429 already have their own paths.

No network: accounts are built from dicts, the upstream call is stubbed, and
time.sleep is recorded instead of slept.
"""
import gzip
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts as A
import wb_egress
import wb_proxy
import wb_settings
import wb_upstream_pool


def sent_body(req):
    """这次出站请求的 JSON body；桌面端身份是 gzip wire 形态，先解回来。"""
    data = req.data
    if (req.get_header("Content-encoding") or "").lower() == "gzip":
        data = gzip.decompress(data)
    return json.loads(data.decode("utf-8"))


class EgressKeyTests(unittest.TestCase):
    """The egress identity must match the connection pool's key exactly."""

    URL = "https://www.workbuddy.ai/v2/chat/completions"

    def test_direct_is_one_shared_egress(self):
        # No proxy: every account goes out the same way, so they share a key.
        self.assertEqual(wb_upstream_pool.egress_key(self.URL, ""),
                         ("https", "www.workbuddy.ai", 443, ""))

    def test_proxy_string_is_the_identity_not_the_slot(self):
        key = wb_upstream_pool.egress_key(self.URL, "http://10.0.0.9:8080")
        self.assertEqual(key, ("https", "www.workbuddy.ai", 443,
                               "http://10.0.0.9:8080"))

    def test_two_slots_pointing_at_one_address_share_a_bucket(self):
        # Two slots with the same address must not be split into two counters.
        self.assertEqual(
            wb_upstream_pool.egress_key(self.URL, "http://10.0.0.9:8080"),
            wb_upstream_pool.egress_key(self.URL, " http://10.0.0.9:8080 "))

    def test_bypassed_proxy_folds_to_direct(self):
        # The pool folds a system-bypassed proxy back to direct; accounting must
        # fold it the same way or one physical path becomes two counters.
        with mock.patch.object(wb_upstream_pool, "_proxy_bypassed",
                               return_value=True):
            key = wb_upstream_pool.egress_key(self.URL, "http://10.0.0.9:8080")
        self.assertEqual(key, ("https", "www.workbuddy.ai", 443, ""))

    def test_non_http_proxy_keeps_its_own_identity(self):
        # socks5 does not go through the pool, but it is still a distinct egress.
        key = wb_upstream_pool.egress_key(self.URL, "socks5://10.0.0.9:1080")
        self.assertEqual(key[3], "socks5://10.0.0.9:1080")


class EgressBackoffTests(unittest.TestCase):
    """The seconds-level ladder and the shared counter."""

    def setUp(self):
        wb_egress.reset()

    def test_ladder_is_seconds_and_capped(self):
        self.assertEqual(wb_egress.transient_backoff(1), 1.0)
        self.assertEqual(wb_egress.transient_backoff(2), 2.0)
        self.assertEqual(wb_egress.transient_backoff(3), 4.0)
        self.assertEqual(wb_egress.transient_backoff(9),
                         wb_egress.EGRESS_BACKOFF_MAX)

    def test_backoff_is_three_orders_below_the_account_ladder(self):
        # The account breaker starts at 30 minutes; this must start at seconds.
        self.assertLess(wb_egress.EGRESS_BACKOFF_MAX, A.BREAKER_COOLDOWN)

    def test_note_transient_grows_one_shared_counter(self):
        key = ("https", "www.workbuddy.ai", 443, "")
        first = wb_egress.note_transient(key)
        second = wb_egress.note_transient(key)
        self.assertEqual(wb_egress.streak(key), 2)
        # Jitter keeps the two apart, but the ladder underneath must have grown.
        self.assertGreaterEqual(first, 1.0 * wb_egress.EGRESS_JITTER_LOW)
        self.assertLessEqual(first, 1.0 * wb_egress.EGRESS_JITTER_HIGH)
        self.assertGreaterEqual(second, 2.0 * wb_egress.EGRESS_JITTER_LOW)
        self.assertLessEqual(second, 2.0 * wb_egress.EGRESS_JITTER_HIGH)

    def test_success_clears_the_streak(self):
        key = ("https", "www.workbuddy.ai", 443, "")
        wb_egress.note_transient(key)
        wb_egress.note_transient(key)
        self.assertEqual(wb_egress.streak(key), 2)
        wb_egress.note_success(key)
        self.assertEqual(wb_egress.streak(key), 0)
        # ...and the ladder restarts at its base, not at the old rung.
        wait = wb_egress.note_transient(key)
        self.assertLessEqual(wait, 1.0 * wb_egress.EGRESS_JITTER_HIGH)

    def test_quiet_window_starts_a_new_ladder(self):
        key = ("https", "www.workbuddy.ai", 443, "")
        now = 1000.0
        wb_egress.note_transient(key, now=now)
        wb_egress.note_transient(key, now=now + 1)
        self.assertEqual(wb_egress.streak(key, now=now + 1), 2)
        later = now + wb_egress.EGRESS_STREAK_RESET + 5
        self.assertEqual(wb_egress.streak(key, now=later), 0)


class _Sock(object):
    def settimeout(self, value):
        self.timeouts = getattr(self, "timeouts", []) + [value]


class _Response(object):
    """Just enough of the urllib response for open_upstream's success path."""

    def __init__(self, sock):
        self._sock = sock
        self.closed = False

    @property
    def fp(self):
        sock = self._sock

        class Raw(object):
            _sock = sock

        class Fp(object):
            raw = Raw()

        return Fp()

    def close(self):
        self.closed = True


class _OpenUpstreamHarness(unittest.TestCase):
    """Drive the real open_upstream() with a stubbed pool and upstream."""

    def setUp(self):
        wb_egress.reset()
        self.sleeps = []
        self._patches = [
            mock.patch.object(time, "sleep", side_effect=self.sleeps.append),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.accounts = []

    def add_account(self, uid, proxy=""):
        account = A.Account(
            {"uid": uid, "accessToken": "t", "realm": "intl",
             "proxy": proxy},
            os.path.join(self.dir, uid + ".json"))
        self.accounts.append(account)
        return account

    def _pool(self):
        accounts = self.accounts

        class Pool(object):
            affinity = type("Affinity", (), {
                "unbind": lambda self, key: None,
                "demote": lambda self, key, uid=None: None})()

            def count_ready(self, realm, model=None):
                return len(accounts)

            def pick_for_session(self, realm, session_key=None, exclude=(),
                                 model=None, page=None):
                for account in accounts:
                    if account.uid not in exclude:
                        return account
                return None

            def list_public(self):
                return []

            def apply_daily_token_limit(self, *a, **k):
                return 0

            def apply_daily_credit_limit(self, *a, **k):
                return 0

            def apply_model_daily_token_limit(self, *a, **k):
                return 0

            def apply_remaining_weights(self, weights=None):
                return weights or {}

        pool = Pool()
        pool.accounts = accounts
        return pool

    def run_request(self, urlopen, egress_backoff=None):
        """Call open_upstream once; return (raised, payload) or (None, result)."""
        if egress_backoff is not None:
            wb_settings.set_egress_backoff_enabled(self.dir, egress_backoff)
        old_pool, old_urlopen = wb_proxy.POOL, A.urlopen
        wb_proxy.POOL = self._pool()
        A.urlopen = urlopen
        try:
            with mock.patch.object(wb_proxy, "ACCOUNTS_DIR", self.dir):
                try:
                    result = wb_proxy.open_upstream(
                        {"model": "deepseek-v4.1-flash",
                         "messages": [{"role": "user", "content": "hi"}]},
                        target_realm="intl")
                except Exception as exc:  # noqa: BLE001 - the point is to inspect
                    return exc, None
                return None, result
        finally:
            wb_proxy.POOL, A.urlopen = old_pool, old_urlopen


class SwitchOnTests(_OpenUpstreamHarness):
    """With egress_backoff_enabled on, the path - not the account - is charged."""

    def test_transport_error_charges_the_egress_not_the_account(self):
        account = self.add_account("u-egress")

        def urlopen(req, timeout=30, proxy=""):
            raise urllib.error.URLError("The handshake operation timed out")

        raised, _result = self.run_request(urlopen, egress_backoff=True)
        self.assertIsNotNone(raised)
        # The account is untouched: no fails, no degrade, no breaker.
        self.assertEqual(account.fails, 0)
        self.assertEqual(account.degrade_count, 0)
        self.assertEqual(account.breaker_until, 0.0)
        self.assertEqual(account.degrade_until, 0.0)
        # The egress counter moved instead - exactly once, even though the
        # retry round re-picked the same account - and every sleep stayed
        # seconds-level.
        key = wb_upstream_pool.egress_key(
            account.chat_base_url() + wb_proxy.CHAT_PATH, account.proxy)
        self.assertEqual(wb_egress.streak(key), 1)
        self.assertTrue(self.sleeps)
        self.assertLessEqual(max(self.sleeps), wb_proxy.EGRESS_SLEEP_MAX)

    def test_one_request_charges_a_shared_egress_once(self):
        # tried.clear() re-picks the account after a transient, so without the
        # per-request guard a single client request would push the shared
        # counter up several rungs (the other half of issue #271's "one request
        # charges one account twice").
        account = self.add_account("u-once")

        def urlopen(req, timeout=30, proxy=""):
            raise urllib.error.URLError("The handshake operation timed out")

        self.run_request(urlopen, egress_backoff=True)
        key = wb_upstream_pool.egress_key(
            account.chat_base_url() + wb_proxy.CHAT_PATH, account.proxy)
        self.assertEqual(wb_egress.streak(key), 1)

    def test_free_model_retries_paid_sibling_only_after_6004_when_opted_in(self):
        account = self.add_account("u-credit-fallback")
        reset = time.time() + 600
        detail = ('{"code":6004,"msg":"usage exceeds frequency limit, '
                  'reset at 2026-10-16 23:00:00 UTC+8"}')
        error = urllib.error.HTTPError("https://upstream.invalid", 429,
                                       "rate limit", {}, io.BytesIO(detail.encode()))
        sent_models = []

        def urlopen(req, timeout=30, proxy=""):
            sent_models.append(sent_body(req)["model"])
            if len(sent_models) == 1:
                raise error
            return _Response(_Sock())

        with mock.patch.dict(os.environ, {"WB_ENABLE_FREE_MODEL_CREDIT_FALLBACK": "1"}), \
                mock.patch.object(wb_proxy, "parse_rate_limit_reset", return_value=reset), \
                mock.patch.object(wb_proxy, "apply_daily_guards", return_value=0), \
                mock.patch.object(wb_proxy, "remaining_priority_enabled", return_value=False), \
                mock.patch.object(wb_proxy, "upstream_timeouts", return_value=(2, 2)):
            raised, result = self.run_request(urlopen)

        self.assertIsNone(raised)
        self.assertEqual(sent_models, ["deepseek-v4.1-flash",
                                       "deepseek-v4.1-flash-sg"])
        self.assertEqual(result[0]._wb_billed_model, "deepseek-v4.1-flash-sg")
        self.assertGreater(account.cap_until("deepseek-v4.1-flash"), time.time())
        saved = []
        with mock.patch.object(wb_proxy, "_persist_usage",
                               side_effect=lambda row, _label: saved.append(dict(row))):
            wb_proxy.record_upstream_usage(
                result[0], "deepseek-v4.1-flash",
                {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})
        self.assertEqual(saved[0]["model"], "deepseek-v4.1-flash-sg")
        self.assertEqual(saved[0]["requested_model"], "deepseek-v4.1-flash")
        result[0].close()
        error.close()

    def test_paid_fallback_tries_each_account_before_product_rotation(self):
        first = self.add_account("u-paid-first")
        second = self.add_account("u-paid-second")
        reset = time.time() + 600
        for account in (first, second):
            account.note_model_cap("deepseek-v4.1-flash", reset)

        pool = self._pool()
        pool.count_ready = lambda realm, model=None: sum(
            a.ready(model=model) for a in self.accounts if a.realm == realm)
        pool.pick_for_session = lambda realm, session_key=None, exclude=(), model=None, page=None: next(
            (a for a in self.accounts
             if a.uid not in exclude and a.realm == realm and a.ready(model=model)), None)
        paid_cap = urllib.error.HTTPError(
            "https://upstream.invalid", 429, "rate limit", {},
            io.BytesIO(b'{"code":6004,"msg":"model limit"}'))
        sent = []
        switch_attempts = []

        def urlopen(req, timeout=30, proxy=""):
            model = sent_body(req)["model"]
            uid = dict((k.lower(), v) for k, v in req.header_items()).get("x-user-id")
            sent.append((model, uid))
            if uid == first.uid:
                raise paid_cap
            return _Response(_Sock())

        old_pool, old_urlopen = wb_proxy.POOL, A.urlopen
        wb_proxy.POOL, A.urlopen = pool, urlopen
        try:
            with mock.patch.dict(os.environ,
                                 {"WB_ENABLE_FREE_MODEL_CREDIT_FALLBACK": "1"}), \
                    mock.patch.object(wb_proxy, "apply_daily_guards", return_value=0), \
                    mock.patch.object(wb_proxy, "remaining_priority_enabled", return_value=False), \
                    mock.patch.object(wb_proxy, "upstream_timeouts", return_value=(2, 2)), \
                    mock.patch.object(wb_proxy, "auto_switch_product_enabled", return_value=True), \
                    mock.patch.object(wb_proxy, "_try_switch_product",
                                      side_effect=lambda account, model: switch_attempts.append(
                                          (account.uid, model)) or False):
                response, _account, _effort = wb_proxy.open_upstream(
                    {"model": "deepseek-v4.1-flash", "messages": [
                        {"role": "user", "content": "hi"}]}, target_realm="intl")
        finally:
            wb_proxy.POOL, A.urlopen = old_pool, old_urlopen
            paid_cap.close()

        self.assertEqual(sent, [
            ("deepseek-v4.1-flash-sg", first.uid),
            ("deepseek-v4.1-flash-sg", second.uid),
        ])
        self.assertEqual(switch_attempts, [])
        self.assertEqual(response._wb_billed_model, "deepseek-v4.1-flash-sg")
        response.close()

    def test_two_accounts_on_one_egress_share_the_counter(self):
        # The point of the whole change: N accounts behind one path must not
        # each carry their own punishment.
        first = self.add_account("u-a")
        self.add_account("u-b")

        def urlopen(req, timeout=30, proxy=""):
            raise urllib.error.URLError("connection reset by peer")

        self.run_request(urlopen, egress_backoff=True)
        key = wb_upstream_pool.egress_key(
            first.chat_base_url() + wb_proxy.CHAT_PATH, first.proxy)
        self.assertEqual(wb_egress.streak(key), 1)
        for account in self.accounts:
            self.assertEqual(account.fails, 0)
            self.assertEqual(account.breaker_until, 0.0)

    def test_gateway_5xx_charges_the_egress(self):
        account = self.add_account("u-gateway")

        def urlopen(req, timeout=30, proxy=""):
            raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", {}, None)

        raised, _result = self.run_request(urlopen, egress_backoff=True)
        self.assertIsNotNone(raised)
        self.assertEqual(account.fails, 0)
        key = wb_upstream_pool.egress_key(
            account.chat_base_url() + wb_proxy.CHAT_PATH, account.proxy)
        self.assertEqual(wb_egress.streak(key), 1)

    def test_500_stays_on_the_account(self):
        # 500 is deliberately left with the account until it gets another look.
        account = self.add_account("u-500")

        def urlopen(req, timeout=30, proxy=""):
            raise urllib.error.HTTPError(req.full_url, 500, "Server Error", {}, None)

        self.run_request(urlopen, egress_backoff=True)
        self.assertGreater(account.fails, 0)

    def test_a_served_request_clears_the_egress_streak(self):
        account = self.add_account("u-recover")
        sock = _Sock()
        calls = {"n": 0}

        def urlopen(req, timeout=30, proxy=""):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.URLError("connection reset by peer")
            return _Response(sock)

        raised, result = self.run_request(urlopen, egress_backoff=True)
        self.assertIsNone(raised)
        self.assertIsNotNone(result)
        self.assertEqual(account.fails, 0)
        key = wb_upstream_pool.egress_key(
            account.chat_base_url() + wb_proxy.CHAT_PATH, account.proxy)
        self.assertEqual(wb_egress.streak(key), 0)


class SwitchOffTests(_OpenUpstreamHarness):
    """The switch is off by default, so the old accounting must be untouched."""

    def test_transport_error_still_feeds_the_account(self):
        account = self.add_account("u-legacy")

        def urlopen(req, timeout=30, proxy=""):
            raise urllib.error.URLError("The handshake operation timed out")

        raised, _result = self.run_request(urlopen, egress_backoff=False)
        self.assertIsNotNone(raised)
        # Old behaviour: the transport error grows fails and degrade_count.
        self.assertGreater(account.fails, 0)
        self.assertGreater(account.degrade_count, 0)
        key = wb_upstream_pool.egress_key(
            account.chat_base_url() + wb_proxy.CHAT_PATH, account.proxy)
        self.assertEqual(wb_egress.streak(key), 0)

    def test_default_is_off(self):
        self.add_account("u-default")
        self.assertFalse(wb_settings.egress_backoff_enabled(self.dir))


if __name__ == "__main__":
    unittest.main()
