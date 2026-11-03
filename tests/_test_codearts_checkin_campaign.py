"""CodeArts credit campaigns: pick the daily check-in by type, not by id.

The daily check-in is a `USER_LOGIN` campaign whose `campaignId` changes
between seasons (34 at the time of writing, 1/4 historically). A hard-coded
`(1, 4)` whitelist silently skips it and reports the new-user campaign as
already claimed, so the panel says "checked in" while nothing was credited.
"""
import os
import sys
import tempfile
import unittest

_startup_dir = tempfile.TemporaryDirectory(prefix="codearts-checkin-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_codearts


class CheckinCampaignTest(unittest.TestCase):
    def test_daily_login_by_type(self):
        self.assertTrue(wb_codearts._is_checkin_campaign({
            "campaignId": 34, "type": "USER_LOGIN",
        }))

    def test_historical_ids_still_work(self):
        self.assertTrue(wb_codearts._is_checkin_campaign({"campaignId": 4}))

    def test_trigger_event_is_enough(self):
        self.assertTrue(wb_codearts._is_checkin_campaign({
            "campaignId": 99,
            "type": "MANUAL_CLAIM",
            "extra": {"triggerEvent": "user.login"},
        }))

    def test_invite_campaign_is_not_checkin(self):
        self.assertFalse(wb_codearts._is_checkin_campaign({
            "campaignId": 3, "type": "INVITE_USER",
        }))

    def test_unknown_campaign_is_not_checkin(self):
        self.assertFalse(wb_codearts._is_checkin_campaign({
            "campaignId": 77, "type": "SOMETHING_ELSE",
        }))


if __name__ == "__main__":
    unittest.main()
