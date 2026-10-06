"""The four guards share one grouped shape: a global default that covers both
realms, plus an optional per-realm override where an empty value means
"inherit the global".

Pinned here: the grouped store and the one-time fold of the flat pre-grouping
keys, the values the pool hands each account, and the two shapes
/settings/save accepts. An override of 0 is *not* the same as an empty one -
0 turns the guard off for that realm, empty keeps following the global - so
both directions are asserted. No network access required.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_settings
import wb_proxy as proxy


def account(uid, realm):
    return wb_accounts.Account(
        {"uid": uid, "accessToken": "t-" + uid, "realm": realm})


class LimitStorageTests(unittest.TestCase):
    def test_global_default_covers_both_realms(self):
        with tempfile.TemporaryDirectory(prefix="limits-") as d:
            wb_settings.set_daily_token_limit(d, 1000)
            self.assertEqual(wb_settings.daily_token_limit(d), 1000)
            self.assertEqual(wb_settings.daily_token_limit(d, "intl"), 1000)
            self.assertEqual(wb_settings.daily_token_limit(d, "cn"), 1000)
            self.assertEqual(wb_settings.limit_values(d, "daily_token_limit"),
                             {"global": 1000, "intl": 1000, "cn": 1000})

    def test_override_wins_for_its_realm_only(self):
        with tempfile.TemporaryDirectory(prefix="limits-") as d:
            wb_settings.set_limit(d, "daily_token_limit", "global", 1000)
            wb_settings.set_limit(d, "daily_token_limit", "cn", 50)
            self.assertEqual(wb_settings.daily_token_limit(d, "intl"), 1000)
            self.assertEqual(wb_settings.daily_token_limit(d, "cn"), 50)
            self.assertEqual(wb_settings.limit_values(d, "daily_token_limit"),
                             {"global": 1000, "intl": 1000, "cn": 50})

    def test_zero_override_is_distinct_from_inherit(self):
        with tempfile.TemporaryDirectory(prefix="limits-") as d:
            wb_settings.set_limit(d, "reserve_credits", "global", 10)
            wb_settings.set_limit(d, "reserve_credits", "cn", 0)
            self.assertEqual(wb_settings.reserve_credits(d, "cn"), 0)
            self.assertEqual(wb_settings.reserve_credits(d, "intl"), 10)
            self.assertEqual(wb_settings.limit_values(d, "reserve_credits"),
                             {"global": 10, "intl": 10, "cn": 0})

    def test_clearing_an_override_falls_back_to_the_global(self):
        with tempfile.TemporaryDirectory(prefix="limits-") as d:
            wb_settings.set_limit(d, "reserve_credits", "global", 10)
            wb_settings.set_limit(d, "reserve_credits", "intl", 3)
            self.assertEqual(wb_settings.reserve_credits(d, "intl"), 3)
            wb_settings.set_limit(d, "reserve_credits", "intl", None)
            self.assertEqual(wb_settings.reserve_credits(d, "intl"), 10)
            wb_settings.set_limit(d, "reserve_credits", "cn", "")
            self.assertEqual(wb_settings.reserve_credits(d, "cn"), 10)
            snapshot = wb_settings.limits_snapshot(d)
            self.assertIsNone(snapshot["reserve_credits"]["intl"])
            self.assertIsNone(snapshot["reserve_credits"]["cn"])
            self.assertEqual(snapshot["reserve_credits"]["global"], 10)

    def test_snapshot_covers_every_guard_and_scope(self):
        with tempfile.TemporaryDirectory(prefix="limits-") as d:
            snapshot = wb_settings.limits_snapshot(d)
            self.assertEqual(set(snapshot), set(wb_settings.LIMIT_KEYS))
            for entry in snapshot.values():
                self.assertEqual(set(entry), {"global", "intl", "cn"})

    def test_junk_and_negatives_collapse(self):
        with tempfile.TemporaryDirectory(prefix="limits-") as d:
            stored = wb_settings.set_limit(d, "reserve_credits", "global", "abc")
            self.assertEqual(stored["global"], 0)
            stored = wb_settings.set_limit(d, "reserve_credits", "global", -5)
            self.assertEqual(stored["global"], 0)
            # An override that cannot be read becomes "inherit", not "off".
            wb_settings.set_limit(d, "reserve_credits", "global", 7)
            wb_settings.set_limit(d, "reserve_credits", "intl", "abc")
            self.assertIsNone(
                wb_settings.limits_snapshot(d)["reserve_credits"]["intl"])
            self.assertEqual(wb_settings.reserve_credits(d, "intl"), 7)

    def test_unknown_key_or_scope_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="limits-") as d:
            with self.assertRaises(ValueError):
                wb_settings.set_limit(d, "nope", "global", 1)
            with self.assertRaises(ValueError):
                wb_settings.set_limit(d, "reserve_credits", "eu", 1)

    def test_legacy_flat_keys_fold_into_limits_once(self):
        with tempfile.TemporaryDirectory(prefix="limits-") as d:
            wb_settings.save(d, {
                "panel_password_hash": "x",
                "reserve_credits": 7,
                "daily_token_limit": 1000,
                "daily_credit_limit": 3,
                "model_daily_token_limit": 500,
            })
            self.assertEqual(wb_settings.reserve_credits(d), 7)
            self.assertEqual(wb_settings.daily_token_limit(d, "cn"), 1000)
            stored = wb_settings.load(d)
            # The flat keys are folded away, so a rollback cannot read a stale
            # number from beside the grouped copy.
            for key in wb_settings.LIMIT_KEYS:
                self.assertNotIn(key, stored)
            self.assertEqual(stored["panel_password_hash"], "x")
            self.assertEqual(stored["limits"]["reserve_credits"]["global"], 7)
            self.assertEqual(stored["limits"]["daily_credit_limit"]["global"], 3)
            self.assertIsNone(stored["limits"]["daily_token_limit"]["cn"])

    def test_a_file_without_any_guard_is_not_rewritten(self):
        with tempfile.TemporaryDirectory(prefix="limits-") as d:
            wb_settings.save(d, {"panel_password_hash": "x"})
            self.assertEqual(wb_settings.reserve_credits(d), 0)
            self.assertNotIn("limits", wb_settings.load(d))

    def test_flat_setter_writes_the_global_slot(self):
        with tempfile.TemporaryDirectory(prefix="limits-") as d:
            self.assertEqual(wb_settings.set_reserve_credits(d, 12), 12)
            self.assertEqual(wb_settings.reserve_credits(d), 12)
            self.assertEqual(wb_settings.reserve_credits(d, "intl"), 12)
            self.assertEqual(
                wb_settings.limits_snapshot(d)["reserve_credits"]["global"], 12)


class LimitPoolTests(unittest.TestCase):
    def _pool(self, directory, realms):
        pool = wb_accounts.AccountPool(directory, log=lambda _m: None)
        accounts = []
        for realm in realms:
            entry = account("uid-" + (realm or "none"), realm)
            # Account() falls back to "intl" when it cannot detect a realm, so
            # pin the realm this case is actually about.
            entry.realm = realm
            accounts.append(entry)
        pool.accounts = accounts
        return pool

    def test_pool_distributes_per_realm(self):
        with tempfile.TemporaryDirectory(prefix="limits-") as d:
            wb_settings.set_limit(d, "daily_token_limit", "global", 100)
            wb_settings.set_limit(d, "daily_token_limit", "cn", 40)
            pool = self._pool(d, ["intl", "cn", ""])
            pool.apply_daily_token_limit()
            got = {a.realm: a.daily_token_limit for a in pool.accounts}
            self.assertEqual(got["intl"], 100)
            self.assertEqual(got["cn"], 40)
            # An account whose realm could not be detected follows the global.
            self.assertEqual(got[""], 100)

    def test_pool_still_accepts_one_number_for_everyone(self):
        with tempfile.TemporaryDirectory(prefix="limits-") as d:
            pool = self._pool(d, ["intl", "cn"])
            pool.apply_daily_token_limit(250, {"uid-intl": 0, "uid-cn": 0})
            self.assertEqual({a.daily_token_limit for a in pool.accounts}, {250})

    def test_each_guard_lands_on_its_own_field(self):
        with tempfile.TemporaryDirectory(prefix="limits-") as d:
            wb_settings.set_limit(d, "reserve_credits", "global", 11)
            wb_settings.set_limit(d, "daily_credit_limit", "intl", 22)
            wb_settings.set_limit(d, "model_daily_token_limit", "cn", 33)
            pool = self._pool(d, ["intl", "cn"])
            pool.apply_reserve_credits()
            pool.apply_daily_credit_limit()
            pool.apply_model_daily_token_limit()
            intl, cn = pool.accounts
            self.assertEqual((intl.reserve_credits, cn.reserve_credits), (11, 11))
            self.assertEqual((intl.daily_credit_limit, cn.daily_credit_limit), (22, 0))
            self.assertEqual(
                (intl.model_daily_token_limit, cn.model_daily_token_limit), (0, 33))

    def test_override_parks_only_that_realms_account(self):
        with tempfile.TemporaryDirectory(prefix="limits-") as d:
            wb_settings.set_limit(d, "daily_token_limit", "global", 1000)
            wb_settings.set_limit(d, "daily_token_limit", "cn", 100)
            pool = self._pool(d, ["intl", "cn"])
            pool.apply_daily_token_limit(None, {"uid-intl": 500, "uid-cn": 500})
            intl, cn = pool.accounts
            self.assertFalse(intl.daily_limit_blocked())
            self.assertTrue(cn.daily_limit_blocked())


class SettingsSaveTests(unittest.TestCase):
    class Request(object):
        _handle_settings_save = proxy.Handler._handle_settings_save

        def __init__(self, payload):
            self.payload = payload
            self.status = None
            self.reply = None

        def _payload_or_error(self, allow_list=False):
            return self.payload

        def _error(self, status, message, kind=""):
            self.status = status
            self.reply = message
            return status, message

        def _json(self, status, payload):
            self.status = status
            self.reply = payload
            return status, payload

    def _save(self, directory, payload):
        request = self.Request(payload)
        with mock.patch.multiple(proxy, ACCOUNTS_DIR=directory, POOL=None,
                                 SCHEDULER=None, PRICING=None):
            request._handle_settings_save()
        return request

    def test_grouped_payload_round_trips(self):
        with tempfile.TemporaryDirectory(prefix="limits-") as d:
            request = self._save(d, {"limits": {
                "reserve_credits": {"global": 5, "intl": 2, "cn": None},
                "daily_token_limit": {"global": 1000},
            }})
            self.assertEqual(request.status, 200)
            self.assertEqual(wb_settings.reserve_credits(d), 5)
            self.assertEqual(wb_settings.reserve_credits(d, "intl"), 2)
            self.assertEqual(wb_settings.reserve_credits(d, "cn"), 5)
            self.assertEqual(wb_settings.daily_token_limit(d), 1000)
            self.assertEqual(request.reply["reserve_credits"], 5)
            self.assertEqual(
                request.reply["limits"]["reserve_credits"],
                {"global": 5, "intl": 2, "cn": None})

    def test_flat_payload_still_sets_the_global(self):
        with tempfile.TemporaryDirectory(prefix="limits-") as d:
            request = self._save(d, {"daily_credit_limit": 42})
            self.assertEqual(request.status, 200)
            self.assertEqual(wb_settings.daily_credit_limit(d), 42)
            self.assertEqual(wb_settings.daily_credit_limit(d, "cn"), 42)

    def test_blank_override_clears_a_stored_one(self):
        with tempfile.TemporaryDirectory(prefix="limits-") as d:
            wb_settings.set_limit(d, "reserve_credits", "global", 9)
            wb_settings.set_limit(d, "reserve_credits", "cn", 1)
            self._save(d, {"limits": {
                "reserve_credits": {"global": 9, "cn": ""}}})
            self.assertEqual(wb_settings.reserve_credits(d, "cn"), 9)
            self.assertIsNone(
                wb_settings.limits_snapshot(d)["reserve_credits"]["cn"])

    def test_bad_values_are_refused(self):
        with tempfile.TemporaryDirectory(prefix="limits-") as d:
            cases = [
                {"limits": []},
                {"limits": {"reserve_credits": {"global": "abc"}}},
                {"limits": {"reserve_credits": {"global": -1}}},
                {"limits": {"reserve_credits": {"intl": "abc"}}},
                {"limits": {"reserve_credits": {"intl": -1}}},
                {"limits": {"reserve_credits": {"global": True}}},
                {"daily_token_limit": "abc"},
            ]
            for payload in cases:
                request = self._save(d, payload)
                self.assertEqual(request.status, 400, payload)
            # Nothing was written by the refused payloads.
            self.assertEqual(wb_settings.reserve_credits(d), 0)
            self.assertEqual(wb_settings.daily_token_limit(d), 0)

    def test_unknown_guard_in_the_payload_is_ignored(self):
        with tempfile.TemporaryDirectory(prefix="limits-") as d:
            request = self._save(d, {"limits": {"not_a_guard": {"global": 5}}})
            self.assertEqual(request.status, 200)
            self.assertEqual(wb_settings.reserve_credits(d), 0)

    def test_settings_view_exposes_the_grouped_map(self):
        with tempfile.TemporaryDirectory(prefix="limits-") as d:
            wb_settings.set_limit(d, "daily_token_limit", "global", 7)
            wb_settings.set_limit(d, "daily_token_limit", "intl", 3)
            with mock.patch.object(proxy, "ACCOUNTS_DIR", d):
                view = proxy.runtime_settings_view()
            self.assertEqual(view["limits"]["daily_token_limit"],
                             {"global": 7, "intl": 3, "cn": None})
            # The flat keys stay for older panel builds; they read the global.
            self.assertEqual(view["daily_token_limit"], 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
