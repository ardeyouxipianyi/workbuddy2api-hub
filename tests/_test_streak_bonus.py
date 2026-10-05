"""Streak manager (M3 C3) and the checkin read-back self-check (C6).

Run with: python _test_streak_bonus.py
No upstream credentials or outbound network are used.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="streak-bonus-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_scheduler
import wb_tasks


class FakeAccount(object):
    def __init__(self, uid="uid-1", nickname="One", realm="cn", enabled=True):
        self.uid = uid
        self.nickname = nickname
        self.realm = realm
        self.enabled = enabled
        self.access_token = "tok"
        self.expires_at = 0
        self.credits = {"remain": 123}
        self.checked_in = False

    def fetch_credits(self):
        return self.credits

    def can_checkin(self):
        return not self.checked_in

    def checkin(self):
        self.checked_in = True
        return {"ok": True, "msg": "ok"}


class FakePool(object):
    def __init__(self, accounts):
        self.accounts = accounts


class StreakApiTests(unittest.TestCase):
    def test_fetch_streak_days(self):
        with mock.patch.object(wb_tasks, "streak_full",
                               return_value=({"streak": {"days": 7}}, "")):
            self.assertEqual(wb_tasks.fetch_streak_days(FakeAccount()), 7)
        with mock.patch.object(wb_tasks, "streak_full",
                               return_value=(None, "boom")):
            self.assertIsNone(wb_tasks.fetch_streak_days(FakeAccount()))

    def test_heatmap_yesterday_missed(self):
        import datetime
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        with mock.patch.object(wb_tasks, "_growth_json",
                               return_value=({"cells": [{"date": yesterday, "score": 0}]}, "")):
            self.assertTrue(wb_tasks.heatmap_yesterday_missed(FakeAccount())[0])
        with mock.patch.object(wb_tasks, "_growth_json",
                               return_value=({"cells": [{"date": yesterday, "score": 5}]}, "")):
            self.assertFalse(wb_tasks.heatmap_yesterday_missed(FakeAccount())[0])
        with mock.patch.object(wb_tasks, "_growth_json",
                               return_value=({"cells": []}, "")):
            self.assertFalse(wb_tasks.heatmap_yesterday_missed(FakeAccount())[0])

    def test_redeem_tier_carries_an_idempotency_token(self):
        captured = {}

        def fake_growth(account, method, path, body=None, timeout=15):
            captured["method"] = method
            captured["path"] = path
            captured["body"] = body
            return {}, ""

        with mock.patch.object(wb_tasks, "_growth_json", side_effect=fake_growth):
            _data, err = wb_tasks.redeem_tier(FakeAccount(), "7d")
        self.assertEqual(err, "")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "/activity/growth/redeem")
        self.assertEqual(captured["body"]["tier"], "7d")
        self.assertTrue(captured["body"]["client_token"])

    def test_lottery_chances_and_draw(self):
        with mock.patch.object(wb_tasks, "_growth_json",
                               return_value=({"chances": 3}, "")):
            self.assertEqual(wb_tasks.lottery_chances(FakeAccount()), (3, ""))
        with mock.patch.object(wb_tasks, "_growth_json",
                               return_value=(None, "boom")):
            self.assertEqual(wb_tasks.lottery_chances(FakeAccount())[0], 0)
        with mock.patch.object(wb_tasks, "_growth_json",
                               return_value=({"prize": "p"}, "")):
            prize, err = wb_tasks.lottery_draw(FakeAccount())
        self.assertEqual(err, "")
        self.assertEqual(prize["prize"], "p")


class RunStreakBonusTests(unittest.TestCase):
    def setUp(self):
        self.sleep_patch = mock.patch.object(wb_tasks.time, "sleep", return_value=None)
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def test_non_cn_is_refused(self):
        result = wb_tasks.run_streak_bonus(FakeAccount(realm="intl"))
        self.assertFalse(result["ok"])

    def test_full_flow(self):
        account = FakeAccount()
        full = {
            "streak": {"days": 8},
            "makeup_cards": {"balance": 1},
            "redemption_status": {
                "tier_7d_status": "unlocked",
                "tier_14d_status": "locked",
                "tier_28d_status": "claimed",
                "tiers": [
                    {"tier": "7d", "credit": 100, "energy": 5, "cards": 1, "chances": 2},
                    {"tier": "14d", "credit": 200},
                    {"tier": "28d", "credit": 300},
                ],
            },
        }
        redeemed = []
        drawn = []

        with mock.patch.object(wb_tasks, "heatmap_yesterday_missed",
                               return_value=(True, "")), \
                mock.patch.object(wb_tasks, "streak_full",
                                  return_value=(full, "")), \
                mock.patch.object(wb_tasks, "use_makeup_card",
                                  return_value=True) as makeup, \
                mock.patch.object(wb_tasks, "claim_gift",
                                  return_value=(50, "")), \
                mock.patch.object(wb_tasks, "claim_compensation",
                                  return_value=(0, "none")), \
                mock.patch.object(wb_tasks, "redeem_tier",
                                  side_effect=lambda account, tier: redeemed.append(tier) or ({}, "")), \
                mock.patch.object(wb_tasks, "lottery_chances",
                                  return_value=(2, "")), \
                mock.patch.object(wb_tasks, "lottery_draw",
                                  side_effect=lambda account: drawn.append(1) or ({"prize": "p"}, "")):
            result = wb_tasks.run_streak_bonus(account)

        self.assertTrue(result["ok"])
        self.assertEqual(result["credit"], 50)
        self.assertEqual(redeemed, ["7d"])
        self.assertEqual(len(drawn), 2)
        makeup.assert_called_once()
        joined = "\n".join(result["logs"])
        self.assertIn("补签", joined)
        self.assertIn("新手礼包 +50", joined)
        self.assertIn("兑换 7d", joined)
        self.assertIn("抽奖完成 2 次", joined)

    def test_streak_query_failure_is_reported(self):
        with mock.patch.object(wb_tasks, "heatmap_yesterday_missed",
                               return_value=(False, "")), \
                mock.patch.object(wb_tasks, "claim_gift", return_value=(0, "none")), \
                mock.patch.object(wb_tasks, "claim_compensation", return_value=(0, "none")), \
                mock.patch.object(wb_tasks, "streak_full",
                                  return_value=(None, "boom")):
            result = wb_tasks.run_streak_bonus(FakeAccount())
        self.assertFalse(result["ok"])
        self.assertIn("连登状态查询失败", "\n".join(result["logs"]))


class SchedulerWiringTests(unittest.TestCase):
    def setUp(self):
        self.account = FakeAccount()
        self.scheduler = wb_scheduler.Scheduler(FakePool([self.account]))

    def tearDown(self):
        wb_tasks.set_logger(lambda msg: None)

    def test_bonus_runs_once_per_day(self):
        with mock.patch.object(wb_tasks, "run_streak_bonus",
                               return_value={"logs": ["bonus line"]}) as bonus:
            self.scheduler._maybe_streak_bonus(self.account, "uid12345")
            self.scheduler._maybe_streak_bonus(self.account, "uid12345")
        self.assertEqual(bonus.call_count, 1)
        self.assertTrue(any("bonus line" in line for line in self.scheduler.logs))

    def test_checkin_read_back_warns_when_the_streak_does_not_move(self):
        with mock.patch.object(wb_tasks, "fetch_streak_days",
                               side_effect=[5, 5]), \
                mock.patch.object(wb_tasks, "run_streak_bonus",
                                  return_value={"logs": ["bonus line"]}), \
                mock.patch.object(wb_scheduler, "do_cat_travel",
                                  return_value={"action": None, "msg": ""}), \
                mock.patch.object(wb_scheduler.time, "localtime",
                                  return_value=type("T", (), {"tm_hour": 9})()):
            self.scheduler._run_cycle("test")
        self.assertTrue(any("连登天数未增加" in line for line in self.scheduler.logs),
                        self.scheduler.logs)

    def test_checkin_read_back_is_quiet_when_the_streak_moves(self):
        with mock.patch.object(wb_tasks, "fetch_streak_days",
                               side_effect=[5, 6]), \
                mock.patch.object(wb_tasks, "run_streak_bonus",
                                  return_value={"logs": []}), \
                mock.patch.object(wb_scheduler, "do_cat_travel",
                                  return_value={"action": None, "msg": ""}), \
                mock.patch.object(wb_scheduler.time, "localtime",
                                  return_value=type("T", (), {"tm_hour": 9})()):
            self.scheduler._run_cycle("test")
        self.assertFalse(any("连登天数未增加" in line for line in self.scheduler.logs),
                         self.scheduler.logs)


if __name__ == "__main__":
    unittest.main()
