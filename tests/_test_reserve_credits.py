"""The low-credit guard parks an account before its balance reaches zero."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_settings


def make_account(uid, remain):
    return wb_accounts.Account({
        "uid": uid,
        "accessToken": "token-%s" % uid,
        "realm": "cn",
        "credits": {"remain": remain, "used": 0, "size": 100},
    })


class ReserveCreditsTests(unittest.TestCase):
    def test_setting_round_trip(self):
        with tempfile.TemporaryDirectory(prefix="reserve-") as directory:
            self.assertEqual(wb_settings.reserve_credits(directory), 0)
            self.assertEqual(wb_settings.set_reserve_credits(directory, 10), 10)
            self.assertEqual(wb_settings.reserve_credits(directory), 10)
            # Garbage and negatives collapse to "off" instead of raising.
            self.assertEqual(wb_settings.set_reserve_credits(directory, -5), 0)
            self.assertEqual(wb_settings.set_reserve_credits(directory, "abc"), 0)

    def test_guard_parks_account_at_or_below_threshold(self):
        above = make_account("uid-above", 11)
        above.reserve_credits = 10
        self.assertFalse(above.reserve_blocked())
        self.assertTrue(above.ready())

        exact = make_account("uid-exact", 10)
        exact.reserve_credits = 10
        self.assertTrue(exact.reserve_blocked())
        self.assertFalse(exact.ready())

        below = make_account("uid-below", 3)
        below.reserve_credits = 10
        self.assertTrue(below.reserve_blocked())
        self.assertFalse(below.ready())

    def test_guard_off_and_unknown_balance_stay_usable(self):
        off = make_account("uid-off", 1)
        self.assertFalse(off.reserve_blocked())  # reserve 0 disables the guard
        self.assertTrue(off.ready())

        unknown = wb_accounts.Account({"uid": "uid-unknown", "accessToken": "t"})
        unknown.reserve_credits = 10
        self.assertFalse(unknown.reserve_blocked())
        self.assertTrue(unknown.ready())

    def test_pool_skips_parked_account_and_publishes_state(self):
        with tempfile.TemporaryDirectory(prefix="reserve-pool-") as directory:
            wb_settings.set_reserve_credits(directory, 10)
            pool = wb_accounts.AccountPool(directory)
            pool.accounts = [make_account("uid-rich", 90), make_account("uid-poor", 4)]
            pool.apply_reserve_credits()
            self.assertEqual(pool.accounts[0].reserve_credits, 10)
            self.assertTrue(pool.accounts[1].reserve_blocked())
            self.assertEqual({pool.pick(realm="cn").uid for _ in range(4)}, {"uid-rich"})
            self.assertEqual(pool.count_ready(realm="cn"), 1)
            row = [a for a in pool.list_public(realm="cn") if a["uid"] == "uid-poor"][0]
            self.assertTrue(row["reserveBlocked"])
            self.assertEqual(row["reserveCredits"], 10)

    def test_settings_change_reaches_the_pool(self):
        with tempfile.TemporaryDirectory(prefix="reserve-reload-") as directory:
            pool = wb_accounts.AccountPool(directory)
            account = make_account("uid-one", 8)
            pool.accounts = [account]
            self.assertFalse(account.reserve_blocked())

            wb_settings.set_reserve_credits(directory, 10)
            pool.apply_reserve_credits()
            self.assertTrue(account.reserve_blocked())

            wb_settings.set_reserve_credits(directory, 0)
            pool.apply_reserve_credits()
            self.assertFalse(account.reserve_blocked())


if __name__ == "__main__":
    unittest.main()

