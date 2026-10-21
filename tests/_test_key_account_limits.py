"""Per-key account and model restrictions.

A key may name the accounts it is allowed to use, in addition to the model
patterns it already had. An empty list keeps the key unrestricted, so installs
that never touch the field keep their exact behaviour. The account filter is
enforced in the scheduler, including the conversation-affinity path: a bound
conversation must not route around a restriction the current request carries.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_proxy as proxy
import wb_settings


def make_account(uid, realm="intl"):
    return wb_accounts.Account(
        {"uid": uid, "realm": realm, "accessToken": "t", "enabled": True})


class AccountListTests(unittest.TestCase):
    def test_comma_string_list_and_dedupe(self):
        self.assertEqual(
            wb_settings._clean_account_refs("a, b; a\nc"),
            ["a", "b", "c"])
        self.assertEqual(
            wb_settings._clean_account_refs(["a", " a ", "", None, "b"]),
            ["a", "b"])

    def test_length_and_count_are_bounded(self):
        long_ref = "x" * (wb_settings.KEY_ACCOUNT_MAX_LEN + 1)
        stored = wb_settings._clean_account_refs(
            [long_ref] + ["u%d" % i for i in range(wb_settings.KEY_ACCOUNTS_MAX + 5)])
        self.assertNotIn(long_ref, stored)
        self.assertEqual(len(stored), wb_settings.KEY_ACCOUNTS_MAX)

    def test_empty_means_unrestricted(self):
        entry = {"accounts": []}
        self.assertIsNone(wb_settings.key_allowed_accounts(entry))
        self.assertTrue(wb_settings.key_allows_account(entry, "any"))
        self.assertIsNone(wb_settings.key_allowed_accounts(None))

    def test_matching_is_exact(self):
        entry = {"accounts": ["uid-1", "uid-2"]}
        self.assertTrue(wb_settings.key_allows_account(entry, "uid-1"))
        self.assertFalse(wb_settings.key_allows_account(entry, "uid-"))
        self.assertFalse(wb_settings.key_allows_account(entry, "UID-1"))
        self.assertEqual(wb_settings.key_allowed_accounts(entry),
                         {"uid-1", "uid-2"})

    def test_legacy_entry_without_accounts_stays_unrestricted(self):
        cleaned = wb_settings._clean_key_entry(
            {"id": "k1", "name": "old", "key": "sk-1"})
        self.assertEqual(cleaned["accounts"], [])
        self.assertIsNone(wb_settings.key_allowed_accounts(cleaned))

    def test_clean_entry_keeps_the_list(self):
        cleaned = wb_settings._clean_key_entry(
            {"id": "k1", "name": "new", "key": "sk-1",
             "accounts": ["uid-1", "uid-1", "uid-2"]})
        self.assertEqual(cleaned["accounts"], ["uid-1", "uid-2"])


class SchedulerFilterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="key-accounts-")
        self.pool = wb_accounts.AccountPool(self.directory, log=lambda _m: None)
        self.pool.accounts = [make_account("uid-a"), make_account("uid-b"),
                              make_account("uid-c")]

    def test_pick_only_returns_allowed_accounts(self):
        for _ in range(6):
            account = self.pool.pick(realm="intl", allowed={"uid-b"})
            self.assertIsNotNone(account)
            self.assertEqual(account.uid, "uid-b")

    def test_pick_without_restriction_still_rotates(self):
        seen = set()
        for _ in range(9):
            account = self.pool.pick(realm="intl")
            self.assertIsNotNone(account)
            seen.add(account.uid)
        self.assertEqual(seen, {"uid-a", "uid-b", "uid-c"})

    def test_pick_returns_none_when_nobody_is_allowed(self):
        self.assertIsNone(self.pool.pick(realm="intl", allowed={"nobody"}))

    def test_count_ready_honours_the_filter(self):
        self.assertEqual(self.pool.count_ready("intl"), 3)
        self.assertEqual(self.pool.count_ready("intl", allowed={"uid-a", "uid-b"}), 2)
        self.assertEqual(self.pool.count_ready("intl", allowed={"nobody"}), 0)

    def test_affinity_does_not_route_around_the_filter(self):
        # A conversation bound to uid-a must not keep serving uid-a when the
        # current request's key only allows uid-b.
        self.pool.affinity.bind("conv-1", "uid-a")
        account = self.pool.pick_for_session(
            realm="intl", session_key="conv-1", allowed={"uid-b"})
        self.assertIsNotNone(account)
        self.assertEqual(account.uid, "uid-b")
        self.assertEqual(self.pool.affinity.recent("conv-1")[0], "uid-b")


class FakeRequest(object):
    """Just enough of the handler for a /settings/save round-trip."""

    _handle_settings_save = proxy.Handler._handle_settings_save

    def __init__(self, payload):
        self.payload = payload
        self.answering = []

    def _payload_or_error(self, allow_list=False):
        return self.payload

    def _error(self, status, message, kind=""):
        self.answering.append(("error", status, message))
        return status, message

    def _json(self, status, payload):
        self.answering.append(("json", status, payload))
        return status, payload


class SettingsSaveKeepsKeyFieldsTests(unittest.TestCase):
    """/settings/save must not rebuild a key without its new fields.

    2026-10-21: the handler assembled each key from a fixed field list and left
    out `accounts` and `concurrency_limit`, so every save silently erased an
    account whitelist and reset the per-key concurrency limit to 0 - while
    dashboard.html kept posting both fields (13300-13301) and reading the limit
    back for its input (11209). The panel therefore showed 0 for a key that had
    one, and saving anything wrote that 0 through.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="key-save-fields-")
        wb_settings.set_api_keys(self.directory, [{
            "id": "k1", "name": "bound", "key": "sk-bound-secret",
            "realm": "intl", "models": ["deepseek*"],
            "accounts": ["uid-1", "uid-2"], "concurrency_limit": 4,
        }])
        patcher = mock.patch.multiple(
            proxy, ACCOUNTS_DIR=self.directory, POOL=None, SCHEDULER=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def save(self, row):
        proxy.Handler._handle_settings_save(FakeRequest({"api_keys": [row]}))
        return wb_settings.api_keys(self.directory)[0]

    def test_a_row_that_omits_both_fields_keeps_them(self):
        kept = self.save({"id": "k1", "name": "bound", "key": "", "realm": "intl"})
        self.assertEqual(kept["accounts"], ["uid-1", "uid-2"])
        self.assertEqual(kept["concurrency_limit"], 4)
        self.assertEqual(kept["key"], "sk-bound-secret")

    def test_an_explicit_list_replaces_the_whitelist(self):
        kept = self.save({"id": "k1", "key": "", "accounts": ["uid-3"]})
        self.assertEqual(kept["accounts"], ["uid-3"])

    def test_an_explicit_empty_list_removes_the_whitelist(self):
        kept = self.save({"id": "k1", "key": "", "accounts": []})
        self.assertEqual(kept["accounts"], [])
        self.assertIsNone(wb_settings.key_allowed_accounts(kept))

    def test_an_explicit_limit_replaces_the_stored_one(self):
        kept = self.save({"id": "k1", "key": "", "concurrency_limit": 7})
        self.assertEqual(kept["concurrency_limit"], 7)

    def test_an_explicit_zero_removes_the_limit(self):
        kept = self.save({"id": "k1", "key": "", "concurrency_limit": 0})
        self.assertEqual(kept["concurrency_limit"], 0)

    def test_the_view_returns_both_fields(self):
        row = proxy.runtime_settings_view()["api_keys"][0]
        self.assertEqual(row["accounts"], ["uid-1", "uid-2"])
        self.assertEqual(row["concurrency_limit"], 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
