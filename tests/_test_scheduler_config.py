"""Schedule configuration plumbing for the panel-parity scheduler.

Run with: python _test_scheduler_config.py
No upstream credentials or outbound network are used.
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="scheduler-config-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_scheduler
import wb_settings
import wb_tasks


class ScheduleSettingsTests(unittest.TestCase):
    def test_defaults_match_the_legacy_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = wb_settings.schedule_config(directory)
        self.assertEqual(cfg["checkin_hours"], [9, 21])
        self.assertEqual(cfg["travel_hours"], [9, 21])
        self.assertEqual(cfg["keepalive_hours"], [22])
        self.assertEqual(cfg["cat_hours"], [1, 23])
        self.assertEqual(cfg["daily_chat_hours"], [9, 21])
        self.assertEqual(cfg["growth_hours"], [1])
        self.assertTrue(cfg["growth_enabled"])
        self.assertTrue(cfg["include_disabled_in_tasks"])
        self.assertFalse(cfg["balance_refresh_enabled"])
        self.assertEqual(cfg["balance_refresh_minutes"], 5)

    def test_roundtrip_and_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            wb_settings.set_schedule_config(directory, {"checkin_hours": [3, 15],
                                                        "include_disabled_in_tasks": False})
            cfg = wb_settings.schedule_config(directory)
            self.assertEqual(cfg["checkin_hours"], [3, 15])
            self.assertFalse(cfg["include_disabled_in_tasks"])
            self.assertEqual(cfg["keepalive_hours"], [22])

    def test_validate_rejects_bad_shapes(self):
        with self.assertRaises(ValueError):
            wb_settings.validate_schedule_patch({"checkin_hours": [24]})
        with self.assertRaises(ValueError):
            wb_settings.validate_schedule_patch({"checkin_hours": [True]})
        with self.assertRaises(ValueError):
            wb_settings.validate_schedule_patch({"checkin_hours": "9,21"})
        with self.assertRaises(ValueError):
            wb_settings.validate_schedule_patch({"include_disabled_in_tasks": "no"})
        with self.assertRaises(ValueError):
            wb_settings.validate_schedule_patch({"balance_refresh_minutes": 0})
        with self.assertRaises(ValueError):
            wb_settings.validate_schedule_patch({"unknown_setting": 1})
        self.assertEqual(wb_settings.validate_schedule_patch({"cat_hours": []}),
                         {"cat_hours": []})


class SchedulerConfigTests(unittest.TestCase):
    def make_scheduler(self, directory, cfg=None):
        if cfg:
            wb_settings.set_schedule_config(directory, cfg)
        pool = wb_accounts.AccountPool(directory)
        return wb_scheduler.Scheduler(pool)

    def test_apply_settings_recomputes_all_hours(self):
        with tempfile.TemporaryDirectory() as directory:
            sched = self.make_scheduler(directory, {"checkin_hours": [3],
                                                    "travel_hours": [4],
                                                    "keepalive_hours": [5],
                                                    "cat_hours": [6],
                                                    "daily_chat_hours": [7],
                                                    "growth_hours": [8]})
        self.assertEqual(sched.all_hours, [3, 4, 5, 6, 7, 8])

    def test_disabling_every_family_leaves_no_fire_hours(self):
        with tempfile.TemporaryDirectory() as directory:
            sched = self.make_scheduler(directory, {"checkin_enabled": False,
                                                    "travel_enabled": False,
                                                    "keepalive_enabled": False,
                                                    "cat_enabled": False,
                                                    "daily_chat_enabled": False,
                                                    "growth_enabled": False})
        self.assertEqual(sched.all_hours, [])
        self.assertIsNone(sched.next_run_time)

    def test_account_eligibility_follows_include_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            sched = self.make_scheduler(directory, {"include_disabled_in_tasks": True})
        account = wb_accounts.Account({"uid": "u1", "realm": "cn",
                                       "accessToken": "t", "enabled": False})
        self.assertTrue(sched._account_eligible(account))
        sched.include_disabled_in_tasks = False
        self.assertFalse(sched._account_eligible(account))

    def test_balance_refresh_due_and_counting(self):
        with tempfile.TemporaryDirectory() as directory:
            sched = self.make_scheduler(directory, {"balance_refresh_enabled": True,
                                                    "balance_refresh_minutes": 5})
            calls = []

            class FakeAccount(object):
                uid = "acct"

                def fetch_credits(self):
                    calls.append(self.uid)
                    return {"ok": True}

            sched.pool.accounts = [FakeAccount()]
            sched._last_balance_refresh = -1000.0
            with mock.patch.object(wb_scheduler.time, "time", return_value=10.0):
                self.assertTrue(sched._maybe_refresh_balances())
            self.assertEqual(calls, ["acct"])
            with mock.patch.object(wb_scheduler.time, "time", return_value=70.0):
                self.assertFalse(sched._maybe_refresh_balances())
            self.assertEqual(calls, ["acct"])

    def test_growth_queue_fires_at_the_configured_hour(self):
        class FakeQueue(object):
            def __init__(self):
                self.calls = 0

            def start(self):
                self.calls += 1
                return {"started": True, "total": 2}

        class FakeAccount(object):
            uid = "u1"
            realm = "cn"
            enabled = True
            nickname = "N"
            expires_at = 0

            def can_checkin(self):
                return False

        with tempfile.TemporaryDirectory() as directory:
            sched = self.make_scheduler(directory, {"growth_hours": [5],
                                                    "growth_enabled": True})
            sched.pool.accounts = [FakeAccount()]
            queue = FakeQueue()
            sched.task_queue = queue
            hour5 = type("T", (), {"tm_hour": 5})()
            hour6 = type("T", (), {"tm_hour": 6})()
            with mock.patch.object(wb_scheduler.time, "sleep", return_value=None), \
                    mock.patch.object(wb_scheduler.time, "localtime",
                                      return_value=hour5), \
                    mock.patch.object(wb_scheduler, "do_cat_travel",
                                      return_value={"action": None, "msg": ""}), \
                    mock.patch.object(wb_tasks, "run_streak_bonus",
                                      return_value={"logs": []}):
                sched._run_cycle("test")
            self.assertEqual(queue.calls, 1)
            with mock.patch.object(wb_scheduler.time, "sleep", return_value=None), \
                    mock.patch.object(wb_scheduler.time, "localtime",
                                      return_value=hour6), \
                    mock.patch.object(wb_scheduler, "do_cat_travel",
                                      return_value={"action": None, "msg": ""}), \
                    mock.patch.object(wb_tasks, "run_streak_bonus",
                                      return_value={"logs": []}):
                sched._run_cycle("test")
            self.assertEqual(queue.calls, 1)

    def test_each_family_only_runs_in_its_own_hours(self):
        events = []

        class FakeQueue(object):
            def start(self):
                events.append("growth")
                return {"started": True, "total": 1}

        class CN(object):
            uid = "cn-1"
            realm = "cn"
            enabled = True
            nickname = "CN"

            def __init__(self):
                self.expires_at = time.time() + 60

            def refresh(self):
                events.append("refresh")
                return True

            def can_checkin(self):
                return True

            def checkin(self):
                events.append("checkin")
                return {"ok": True, "msg": "ok"}

        class INTL(object):
            uid = "intl-1"
            realm = "intl"
            enabled = True
            nickname = "INTL"

            def __init__(self):
                self.expires_at = time.time() + 60

            def refresh(self):
                events.append("refresh")
                return True

            def can_daily_chat(self):
                return True

            def daily_chat(self):
                events.append("daily_chat")
                return {"ok": True}

        def travel(acc):
            events.append("travel")
            return {"action": None, "msg": ""}

        def streak(acc):
            events.append("streak")
            return {"logs": []}

        def night(acc):
            events.append("night")
            return {"logs": []}

        with tempfile.TemporaryDirectory() as directory:
            sched = self.make_scheduler(directory, {
                "checkin_hours": [9], "travel_hours": [10],
                "keepalive_hours": [11], "cat_hours": [12],
                "daily_chat_hours": [13], "growth_hours": [14]})
            sched.pool.accounts = [CN(), INTL()]
            sched.task_queue = FakeQueue()
            for hour, expected in ((9, {"checkin", "streak"}),
                                   (11, {"refresh"}),
                                   (13, {"daily_chat"}),
                                   (14, {"growth"})):
                events.clear()
                stamp = type("T", (), {"tm_hour": hour})()
                with mock.patch.object(wb_scheduler.time, "sleep", return_value=None), \
                        mock.patch.object(wb_scheduler.time, "localtime",
                                          return_value=stamp), \
                        mock.patch.object(wb_scheduler, "do_cat_travel", side_effect=travel), \
                        mock.patch.object(wb_tasks, "run_streak_bonus", side_effect=streak), \
                        mock.patch.object(wb_tasks, "fetch_streak_days", return_value=None), \
                        mock.patch.object(wb_tasks, "run_night_growth", side_effect=night):
                    sched._run_cycle("test")
                self.assertEqual(set(events), expected,
                                 "hour %d -> %r" % (hour, events))

    def test_status_exposes_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            sched = self.make_scheduler(directory, {"checkin_hours": [8]})
        status = sched.status()
        self.assertEqual(status["checkin_hours"], [8])
        self.assertIn("balance_refresh", status)


if __name__ == "__main__":
    unittest.main(verbosity=1)
