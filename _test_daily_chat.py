"""Unit tests for international realm daily chat check-in feature."""
import json, os, sys, unittest, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("ACCOUNTS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "_acc_test"))
os.environ.setdefault("USAGE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "_use_test"))

import wb_accounts, wb_settings

class DailyChatTests(unittest.TestCase):
    def test_daily_chat_eligibility(self):
        acc = wb_accounts.Account({
            "uid": "test_intl_1",
            "realm": "intl",
            "accessToken": "dummy",
            "lastDailyChat": None
        })
        # Fresh account can chat
        self.assertTrue(acc.can_daily_chat())
        self.assertFalse(acc.can_checkin()) # cn only

        # Already chatted today
        acc.last_daily_chat = time.strftime("%Y-%m-%d 10:00:00")
        self.assertFalse(acc.can_daily_chat())

        # Chatted yesterday
        acc.last_daily_chat = "2020-01-01 10:00:00"
        self.assertTrue(acc.can_daily_chat())

    def test_cn_account_ineligible(self):
        acc = wb_accounts.Account({
            "uid": "test_cn_1",
            "realm": "cn",
            "accessToken": "dummy"
        })
        self.assertFalse(acc.can_daily_chat())
        self.assertTrue(acc.can_checkin())

if __name__ == "__main__":
    unittest.main()

