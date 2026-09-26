"""Account and panel state after a model-scoped upstream 429.

Run with: python _test_model_cooldowns.py
No upstream credentials or outbound network are used.
"""
import atexit
import io
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
import urllib.error

_startup_dir = tempfile.TemporaryDirectory(prefix="model-cooldowns-")
atexit.register(_startup_dir.cleanup)
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts as accounts
import wb_proxy as proxy


class ModelCooldownTests(unittest.TestCase):
    def account(self):
        return accounts.Account({"uid": "synthetic-cn", "realm": "cn", "accessToken": "token"})

    def test_account_snapshot_and_selection(self):
        account = self.account()
        now = time.time()
        account.note_error("429", model="glm-5.3", until=now + 600)
        account.note_error("429", model="glm-5.2", until=now + 60)
        state = account.public()

        self.assertEqual([item["model"] for item in state["modelCooldowns"]],
                         ["glm-5.2", "glm-5.3"])
        self.assertTrue(all(isinstance(item["expiresAt"], int)
                            for item in state["modelCooldowns"]))
        self.assertFalse(state["inCooldown"])
        self.assertTrue(account.ready(model="another-model"))
        self.assertFalse(account.ready(model="glm-5.3"))

        account.clear_error(model="glm-5.3")
        self.assertEqual([item["model"] for item in account.public()["modelCooldowns"]],
                         ["glm-5.2"])
        with account._throttle_lock:
            account.model_cooldowns["glm-5.2"] = time.time() - 1
        self.assertEqual(account.model_cooldowns_snapshot(), [])

        with account._throttle_lock:
            account.cooldown_until = 1000.0
        with mock.patch.object(accounts.time, "time", return_value=1000.0):
            at_deadline = account.public()
        self.assertFalse(at_deadline["inCooldown"])
        self.assertIsNone(at_deadline["cooldownFor"])

    def test_snapshot_does_not_wait_for_token_refresh(self):
        account = self.account()
        account.note_error("429", model="glm-5.3", until=time.time() + 60)
        with account._refresh_lock:
            start = time.monotonic()
            self.assertEqual(account.public()["modelCooldowns"][0]["model"], "glm-5.3")
            self.assertLess(time.monotonic() - start, 1)

    def test_concurrent_updates_and_panel_reads(self):
        account = self.account()
        stop = threading.Event()
        failures = []

        def writer():
            n = 0
            while not stop.is_set():
                account.note_error("429", model="m%d" % (n % 20), until=time.time() + 5)
                account.clear_error(model="m%d" % ((n + 1) % 20))
                n += 1

        def reader():
            try:
                while not stop.is_set():
                    account.public()
            except Exception as exc:
                failures.append(exc)

        threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader)
                                                      for _ in range(2)]
        for thread in threads:
            thread.start()
        try:
            time.sleep(0.5)
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=2)
        self.assertFalse(any(thread.is_alive() for thread in threads), "worker did not stop")
        self.assertEqual(failures, [])

    def test_upstream_429_reaches_accounts_payload(self):
        account = self.account()
        reset = time.time() + 600
        detail = "usage exceeds frequency limit"
        error = urllib.error.HTTPError("https://upstream.invalid", 429, "rate limit", {},
                                      io.BytesIO(detail.encode("utf-8")))
        class Pool(object):
            accounts = [account]
            affinity = types.SimpleNamespace(unbind=lambda _key: None)
            def count_ready(self, realm, model=None):
                return sum(a.ready(model=model) for a in self.accounts)
            def pick_for_session(self, realm, session_key=None, exclude=(), model=None):
                return next((a for a in self.accounts if a.uid not in exclude
                             and a.realm == realm and a.ready(model=model)), None)
            def list_public(self):
                return [a.public() for a in self.accounts]

        old_pool, old_urlopen = proxy.POOL, accounts.urlopen
        old_parser = proxy.parse_rate_limit_reset
        proxy.POOL = Pool()
        accounts.urlopen = lambda *args, **kwargs: (_ for _ in ()).throw(error)
        # Keep this test on the 429 -> account-list path, independent of the
        # existing parser's timezone handling.
        proxy.parse_rate_limit_reset = lambda _detail: reset
        try:
            with self.assertRaises(proxy.RateLimited):
                proxy.open_upstream({"model": "glm-5.3", "messages": [
                    {"role": "user", "content": "hello"}]}, target_realm="cn")
            row = proxy.POOL.list_public()[0]
            self.assertFalse(row["inCooldown"])
            self.assertEqual(row["modelCooldowns"][0]["model"], "glm-5.3")
            self.assertLess(abs(row["modelCooldowns"][0]["expiresAt"] - reset), 2)
            self.assertTrue(account.ready(model="another-model"))
        finally:
            proxy.POOL, accounts.urlopen = old_pool, old_urlopen
            proxy.parse_rate_limit_reset = old_parser
            error.close()


if __name__ == "__main__":
    unittest.main()
