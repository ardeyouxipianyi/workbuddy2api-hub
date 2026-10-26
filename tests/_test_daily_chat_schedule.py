"""Web-activity schedule: minute-level slots, independent of the checkin hours.

The intl daily activity used to run at the checkin hours (09:00/21:00). An
operator can now give it its own HH:MM slots; empty draws two random slots for
the day, 12 hours apart, to avoid the upstream's top-of-hour batch window.
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-daily-schedule-")

import wb_scheduler  # noqa: E402
import wb_settings  # noqa: E402
import wb_proxy  # noqa: E402

ACCOUNTS = os.path.join(_TMP.name, "accounts")


def at(hour, minute, day=20):
    return time.struct_time((2026, 10, day, hour, minute, 0, 0, 0, -1))


class TimeParsingTests(unittest.TestCase):
    def test_hh_mm_is_normalized_sorted_and_deduped(self):
        self.assertEqual(
            wb_settings._normalize_times("21:15, 9:05, 09:05, 21：30"),
            ["09:05", "21:15", "21:30"])

    def test_invalid_entries_are_dropped(self):
        self.assertEqual(
            wb_settings._normalize_times(["25:00", "12:60", "noon", "07:30"]),
            ["07:30"])

    def test_empty_is_none(self):
        self.assertIsNone(wb_settings._normalize_times(""))
        self.assertIsNone(wb_settings._normalize_times([]))

    def test_count_is_bounded(self):
        stored = wb_settings._normalize_times(
            ["%02d:%02d" % (h, m) for h in range(24) for m in (0, 30)])
        self.assertEqual(len(stored), wb_settings.DAILY_CHAT_TIMES_MAX)


class ScheduleSettingTests(unittest.TestCase):
    def setUp(self):
        wb_settings.set_daily_chat_times(ACCOUNTS, [])

    def test_default_is_empty(self):
        self.assertEqual(wb_settings.daily_chat_times(ACCOUNTS), [])

    def test_round_trip_and_clear(self):
        stored = wb_settings.set_daily_chat_times(ACCOUNTS, "09:30, 21:15")
        self.assertEqual(stored, ["09:30", "21:15"])
        self.assertEqual(wb_settings.daily_chat_times(ACCOUNTS),
                         ["09:30", "21:15"])
        self.assertEqual(wb_settings.set_daily_chat_times(ACCOUNTS, ""), [])
        self.assertEqual(wb_settings.daily_chat_times(ACCOUNTS), [])

    def test_invalid_non_empty_raises(self):
        with self.assertRaises(ValueError):
            wb_settings.set_daily_chat_times(ACCOUNTS, "not-a-time")

    def test_hourly_patch_keeps_the_daily_chat_slots(self):
        wb_settings.set_daily_chat_times(ACCOUNTS, ["09:30"])
        wb_settings.set_schedule_hours(ACCOUNTS, {"checkin": [8, 20]})
        self.assertEqual(wb_settings.daily_chat_times(ACCOUNTS), ["09:30"])
        self.assertEqual(wb_settings.schedule_hours(ACCOUNTS)["checkin"], [8, 20])


class DueSlotTests(unittest.TestCase):
    def setUp(self):
        self.sched = wb_scheduler.Scheduler(None)
        self.sched.daily_chat_times = ["09:30", "21:15"]

    def test_matching_minute_is_due_once(self):
        self.assertEqual(self.sched._due_daily_chat_slot(at(9, 30)), "09:30")
        self.assertEqual(self.sched._due_daily_chat_slot(at(9, 30)), "")
        self.assertEqual(self.sched._due_daily_chat_slot(at(9, 31)), "")
        self.assertEqual(self.sched._due_daily_chat_slot(at(21, 15)), "21:15")

    def test_next_day_resets_the_guard(self):
        self.assertEqual(self.sched._due_daily_chat_slot(at(9, 30, day=20)),
                         "09:30")
        self.assertEqual(self.sched._due_daily_chat_slot(at(9, 30, day=21)),
                         "09:30")

    def test_no_slots_means_never_due(self):
        self.sched.daily_chat_times = []
        self.assertEqual(self.sched._due_daily_chat_slot(at(9, 30)), "")


class CycleScopeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="daily-schedule-cycle-")
        wb_settings.set_daily_chat_times(self.dir, [])
        self.acc = mock.MagicMock()
        self.acc.uid = "u1"
        self.acc.realm = "intl"
        self.acc.restricted_until = 0.0
        self.acc.can_daily_chat.return_value = True
        self.acc.daily_chat.return_value = {"ok": True, "msg": "ok"}
        self.pool = mock.MagicMock()
        self.pool.dir = self.dir
        self.pool.accounts = [self.acc]
        self.sched = wb_scheduler.Scheduler(self.pool)
        self.sched.log = lambda *a, **k: None
        self.sched.daily_chat_times = []

    def run_cycle(self, **kwargs):
        with mock.patch.object(wb_proxy, "restriction_hold_remaining",
                               return_value=0.0), \
             mock.patch.object(wb_scheduler.time, "sleep", return_value=None), \
             mock.patch.object(wb_scheduler.time, "localtime",
                               return_value=at(9, 0)):
            self.sched._run_cycle(**kwargs)

    def test_daily_chat_only_runs_the_intl_activity(self):
        self.run_cycle(daily_chat_only=True)
        self.acc.daily_chat.assert_called_once_with(trigger="scheduler")
        self.acc.checkin.assert_not_called()
        self.acc.refresh.assert_not_called()

    def test_empty_slots_use_todays_random_slots(self):
        """时段留空时仍按当天随机时段运行，启动补跑先跑一轮。"""
        self.sched.daily_chat_random_slots = True
        self.run_cycle()
        self.acc.daily_chat.assert_called_once_with(trigger="scheduler")

    def test_explicit_slots_take_over_from_the_checkin_hour(self):
        """配了显式时段后，整点巡检不再顺带跑活跃；只有到点的时段才跑。

        第一轮是启动补跑（新进程第一次巡检会补当天还没活跃的账号），之后
        09:00 这个整点巡检不该再碰活跃。
        """
        self.sched.daily_chat_times = ["09:30"]
        self.run_cycle()
        self.acc.daily_chat.reset_mock()
        self.run_cycle()
        self.acc.daily_chat.assert_not_called()

    def test_startup_catchup_runs_once_and_respects_the_switch(self):
        """启动补跑只消费一次；关掉开关就一个都不补。"""
        self.sched.daily_chat_times = ["23:30"]      # 今天还没到点
        self.run_cycle()
        self.acc.daily_chat.assert_called_once_with(trigger="scheduler")
        self.acc.daily_chat.reset_mock()
        self.run_cycle()
        self.acc.daily_chat.assert_not_called()

        wb_settings.set_daily_chat_startup_catchup(self.dir, False)
        acc = mock.MagicMock()
        acc.uid = "u2"
        acc.realm = "intl"
        acc.restricted_until = 0.0
        acc.can_daily_chat.return_value = True
        acc.daily_chat.return_value = {"ok": True, "msg": "ok"}
        self.pool.accounts = [acc]
        self.sched = wb_scheduler.Scheduler(self.pool)
        self.sched.log = lambda *a, **k: None
        self.sched.daily_chat_times = ["23:30"]
        self.run_cycle()
        acc.daily_chat.assert_not_called()

class RandomSlotTests(unittest.TestCase):
    """面板留空时段时：每天随机抽两个、间隔 12 小时、当日不重抽。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="daily-schedule-random-")

    def test_two_slots_twelve_hours_apart(self):
        times = wb_settings.daily_chat_random_times(self.dir)
        self.assertEqual(len(times), 2)
        first = int(times[0][:2]) * 60 + int(times[0][3:])
        second = int(times[1][:2]) * 60 + int(times[1][3:])
        self.assertEqual((second - first) % (24 * 60), 12 * 60)

    def test_minutes_stay_off_the_top_of_the_hour(self):
        """14220 全落在整点批处理窗口，分钟避开 00–04 就绕开了那一段。"""
        for _ in range(20):
            times = wb_settings.daily_chat_random_times(self.dir)
            for slot in times:
                self.assertGreaterEqual(int(slot[3:]), 5)
                self.assertLessEqual(int(slot[3:]), 55)
            os.remove(wb_settings.settings_path(self.dir))

    def test_the_draw_is_stable_within_the_day(self):
        first = wb_settings.daily_chat_random_times(self.dir)
        self.assertEqual(wb_settings.daily_chat_random_times(self.dir), first)
        # 换一天才重抽
        tomorrow = time.time() + 86400
        again = wb_settings.daily_chat_random_times(self.dir, now=tomorrow)
        self.assertEqual(len(again), 2)

    def test_explicit_slots_win_over_the_random_ones(self):
        wb_settings.set_daily_chat_times(self.dir, ["07:15", "19:15"])
        self.assertEqual(wb_settings.daily_chat_effective_times(self.dir),
                         ["07:15", "19:15"])
        wb_settings.set_daily_chat_times(self.dir, [])
        self.assertEqual(len(wb_settings.daily_chat_effective_times(self.dir)), 2)


class DueWindowTests(unittest.TestCase):
    """时段按到期窗口判定：漏了 tick、巡检占锁、重启都要能补上。"""

    def setUp(self):
        pool = mock.MagicMock()
        pool.dir = ""
        pool.accounts = []
        self.sched = wb_scheduler.Scheduler(pool)
        self.sched.log = lambda *a, **k: None
        self.sched.daily_chat_times = ["09:37"]

    def at(self, hour, minute, day=21):
        return time.struct_time((2026, 10, day, hour, minute, 0, 2, 294, -1))

    def test_a_late_tick_still_fires_the_slot(self):
        self.assertEqual(self.sched._due_daily_chat_slot(self.at(9, 37)), "09:37")
        self.assertEqual(self.sched._due_daily_chat_slot(self.at(9, 37)), "")

    def test_a_missed_minute_is_caught_later(self):
        """09:37 那分钟没有 tick：10:05 醒来照样补这一次。"""
        self.assertEqual(self.sched._due_daily_chat_slot(self.at(10, 5)), "09:37")

    def test_a_slot_older_than_the_grace_window_is_skipped(self):
        self.assertEqual(self.sched._due_daily_chat_slot(self.at(22, 0)), "")

    def test_before_the_slot_nothing_fires(self):
        self.assertEqual(self.sched._due_daily_chat_slot(self.at(9, 36)), "")


class HangWatchdogTests(unittest.TestCase):
    """一个账号卡在网络上，不能把整轮巡检拖住，也不能把活跃名额带走。"""

    def setUp(self):
        pool = mock.MagicMock()
        pool.dir = ""
        pool.accounts = []
        self.sched = wb_scheduler.Scheduler(pool)
        self.logs = []
        self.sched.log = lambda msg, *a, **k: self.logs.append(msg)

    def account(self, hang=True):
        import threading
        acc = mock.MagicMock()
        acc.uid = "58eb0b70-0000-0000-0000-000000000001"
        acc.can_daily_chat.return_value = True
        release = threading.Event()
        self.release = release

        def daily_chat(trigger="unknown"):
            if hang:
                release.wait(30)          # 模拟卡在 socket 上：不返回
            return {"ok": True, "msg": "done"}

        acc.daily_chat.side_effect = daily_chat
        acc.web_conversation_reclaim.return_value = 2
        return acc

    def test_a_hung_account_is_abandoned_and_its_conversations_reclaimed(self):
        acc = self.account()
        started = time.time()
        try:
            ok = self.sched._run_one_daily_chat(acc, wait=0.2)
        finally:
            self.release.set()
        self.assertFalse(ok, "卡住的账号不能算成功")
        self.assertLess(time.time() - started, 5, "不能等它自己返回")
        acc.web_conversation_reclaim.assert_called_once_with(0)
        self.assertIn("仍未返回", " ".join(self.logs))
        self.assertEqual(self.sched._daily_chat_live, {}, "运行中状态要清掉")

    def test_the_abandoned_account_still_gets_an_activity_record(self):
        acc = self.account()
        try:
            with mock.patch("wb_activity.record_attempt") as record:
                self.sched._run_one_daily_chat(acc, wait=0.2)
        finally:
            self.release.set()
        self.assertTrue(record.called, "放弃也要落一条结论，面板上不能一片空白")
        res = record.call_args[0][3]
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("phase"), "hang")

    def test_a_normal_account_still_reports_success(self):
        acc = self.account(hang=False)
        self.assertTrue(self.sched._run_one_daily_chat(acc, wait=5))
        acc.web_conversation_reclaim.assert_not_called()

    def test_wait_zero_means_no_deadline(self):
        """预算 0（不限制）时不能自己造一个截止时间出来。"""
        acc = self.account(hang=False)
        with mock.patch.object(self.sched, "_daily_chat_budget", return_value=0):
            self.assertTrue(self.sched._run_one_daily_chat(acc))

    def test_abandoned_workers_are_bounded(self):
        """卡住的线程通常几十秒就结束；病态情况下也不能无限堆。"""
        import threading
        alive = [mock.MagicMock() for _ in range(wb_scheduler.DAILY_CHAT_ABANDONED_MAX)]
        for item in alive:
            item.is_alive.return_value = True
        self.sched._daily_chat_abandoned = list(alive)
        acc = self.account(hang=False)
        self.assertFalse(self.sched._run_one_daily_chat(acc, wait=5),
                         "线程堆到上限时这一轮跳过，而不是继续开新的")
        acc.daily_chat.assert_not_called()
        self.assertIn("跳过", " ".join(self.logs))

    def test_finished_abandoned_workers_are_forgotten(self):
        dead = mock.MagicMock()
        dead.is_alive.return_value = False
        self.sched._daily_chat_abandoned = [dead]
        acc = self.account(hang=False)
        self.assertTrue(self.sched._run_one_daily_chat(acc, wait=5))
        self.assertEqual(self.sched._daily_chat_abandoned, [],
                         "已经结束的线程要从计数里去掉，否则会一直占着上限")

    def test_a_hung_account_counts_against_the_cap(self):
        acc = self.account()
        try:
            self.sched._run_one_daily_chat(acc, wait=0.2)
        finally:
            self.release.set()
        self.assertEqual(len(self.sched._daily_chat_abandoned), 1)
        self.assertEqual(self.sched._daily_chat_status()["abandoned"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
