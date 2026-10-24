"""每日对话活跃上报与连登（issue #250 / 连续打卡，issue #645 加入国际版）。

包含行为级测试：
1. can_report_activity 判定：cn 与 intl 都适用，按本地日期闸门（当天做过则不重复）。
2. report_activity 行为：
   - 非法 realm 直接拒绝；国际版按自己的 base（chat_base_url）上报；
   - 构造 chat 事件上报并校验上游 code=0；
   - 上报成功后更新 last_activity_report 并落盘；
   - 只读读取 streak days 带回 msg / streak_days 结构；
   - 模拟上游静默丢弃 (streak days<=0) 的提示与正常递增提示。
3. 调度器巡检链路：
   - 针对 cn 账号执行活跃上报；
   - 第二轮不重复上报；
   - 计数器与汇总日志正确统计上报个数。

Run with: python tests/run_all.py streak
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_scheduler
import wb_tasks


def make_account(uid="u1", realm="cn", **extra):
    data = {
        "uid": uid,
        "realm": realm,
        "accessToken": "token-" + uid,
        "nickname": "test-" + uid,
        "expiresAt": int(time.time()) + 3600,
    }
    data.update(extra)
    return wb_accounts.Account(data)


class CanReportActivityTests(unittest.TestCase):
    def test_can_checkin_skips_enterprise(self):
        acc = make_account(uid="ent-chk", realm="cn", enterpriseId="ent-1")
        self.assertFalse(acc.can_checkin())
        res = acc.checkin()
        self.assertFalse(res.get("ok"))
        self.assertIn("enterprise", res.get("error", ""))

    def test_can_report_activity_skips_enterprise(self):
        acc = make_account(uid="ent-rep", realm="cn", enterpriseId="ent-1")
        self.assertFalse(acc.can_report_activity())
        res = acc.report_activity()
        self.assertFalse(res.get("ok"))
        self.assertIn("enterprise", res.get("error", ""))

    def test_intl_account_can_report(self):
        # issue #645：国际版也有 growth 连登（www.workbuddy.ai 的 /activity/growth/streak），
        # 只是上报端点不同，所以它同样算「今天还没做过活跃上报」。
        acc = make_account(realm="intl")
        self.assertTrue(acc.can_report_activity(), acc.realm)
        acc.last_activity_report = time.strftime("%Y-%m-%d %H:%M:%S")
        self.assertFalse(acc.can_report_activity())

    def test_cn_account_without_record_can_report(self):
        acc = make_account(realm="cn")
        self.assertTrue(acc.can_report_activity())

    def test_cn_account_with_today_record_cannot_report(self):
        acc = make_account(realm="cn", lastActivityReport=time.strftime("%Y-%m-%d %H:%M:%S"))
        self.assertFalse(acc.can_report_activity())

    def test_cn_account_with_yesterday_record_can_report(self):
        acc = make_account(realm="cn", lastActivityReport="2026-10-09 10:00:00")
        self.assertTrue(acc.can_report_activity())


class ReportActivityTests(unittest.TestCase):
    def test_rejects_unknown_realm(self):
        acc = make_account(realm="other")
        res = acc.report_activity()
        self.assertFalse(res.get("ok"))
        self.assertIn("only for CN", res.get("error", ""))

    def test_intl_reports_against_its_own_base(self):
        """国际版必须打到 www.workbuddy.ai；用国内 base 上报只会静默失败。"""
        acc = make_account(uid="intl-base", realm="intl")
        seen = []

        def fake_report_events(account, events, base=None):
            seen.append((account.uid, base))
            return True

        with mock.patch.object(wb_tasks, "report_events", fake_report_events), \
             mock.patch.object(wb_tasks, "fetch_streak_days", return_value=3):
            res = acc.report_activity()
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(seen, [("intl-base", acc.chat_base_url())])
        self.assertIn("workbuddy.ai", seen[0][1])

    def test_cn_keeps_the_default_base(self):
        acc = make_account(uid="cn-base", realm="cn")
        seen = []

        def fake_report_events(account, events, base=None):
            seen.append(base)
            return True

        with mock.patch.object(wb_tasks, "report_events", fake_report_events), \
             mock.patch.object(wb_tasks, "fetch_streak_days", return_value=None):
            res = acc.report_activity()
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(seen, [None])  # None 让 wb_tasks 走默认国内 base

    def test_fails_when_report_events_rejected(self):
        acc = make_account(realm="cn")
        with mock.patch.object(wb_tasks, "report_events", return_value=False):
            res = acc.report_activity()
            self.assertFalse(res.get("ok"))
            self.assertIn("拒绝", res.get("error", ""))
            self.assertIsNone(acc.last_activity_report)

    def test_success_with_positive_streak_days(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            acc = make_account(realm="cn")
            acc.path = acc.save(tmpdir)

            with mock.patch.object(wb_tasks, "report_events", return_value=True), \
                 mock.patch.object(wb_tasks, "fetch_streak_days", return_value=5):
                res = acc.report_activity()
                self.assertTrue(res.get("ok"))
                self.assertEqual(res.get("streak_days"), 5)
                self.assertIn("连续打卡 5 天", res.get("msg", ""))
                self.assertIsNotNone(acc.last_activity_report)

                # 确认持久化落盘
                with open(acc.path, encoding="utf-8") as f:
                    reloaded = wb_accounts.Account(json.load(f), acc.path)
                self.assertEqual(reloaded.last_activity_report, acc.last_activity_report)

    def test_success_with_unknown_streak_days(self):
        acc = make_account(realm="cn")
        with mock.patch.object(wb_tasks, "report_events", return_value=True), \
             mock.patch.object(wb_tasks, "fetch_streak_days", return_value=None):
            res = acc.report_activity()
            self.assertTrue(res.get("ok"))
            self.assertIn("连登天数未知", res.get("msg", ""))

    def test_success_with_zero_streak_days_warns(self):
        acc = make_account(realm="cn")
        with mock.patch.object(wb_tasks, "report_events", return_value=True), \
             mock.patch.object(wb_tasks, "fetch_streak_days", return_value=0):
            res = acc.report_activity()
            self.assertTrue(res.get("ok"))
            self.assertEqual(res.get("streak_days"), 0)
            self.assertIn("连登仍是 0 天", res.get("msg", ""))


class SchedulerActivityReportTests(unittest.TestCase):
    def test_scheduler_runs_report_for_cn_accounts(self):
        cn_acc = make_account(uid="cn-due", realm="cn")
        intl_acc = make_account(uid="intl-due", realm="intl")
        intl_acc.last_daily_chat = time.strftime("%Y-%m-%d %H:%M:%S")
        cn_acc.last_checkin = time.strftime("%Y-%m-%d %H:%M:%S")

        pool = type("Pool", (), {"accounts": [cn_acc, intl_acc]})()
        scheduler = wb_scheduler.Scheduler(pool)
        scheduler.cat_hours = []

        report_calls = []

        def fake_report(self):
            report_calls.append(self.uid)
            self.last_activity_report = time.strftime("%Y-%m-%d %H:%M:%S")
            return {"ok": True, "streak_days": 2, "msg": "对话活跃上报成功（连续打卡 2 天）"}

        with mock.patch.object(wb_accounts.Account, "report_activity", fake_report), \
             mock.patch.object(wb_scheduler, "do_cat_travel", lambda acc: {"action": None}), \
             mock.patch.object(wb_scheduler.time, "sleep", lambda *_: None):
            scheduler._run_cycle("测试第一轮", force=True)
            self.assertEqual(report_calls, ["cn-due"])
            self.assertTrue(any("活跃上报 1 个" in log for log in scheduler.logs))

            # 第二轮不重复跑
            scheduler._run_cycle("测试第二轮", force=True)
            self.assertEqual(report_calls, ["cn-due"])
            self.assertTrue(any("活跃上报 0 个" in log for log in scheduler.logs))





class StreakBonusTests(unittest.TestCase):
    def test_skips_intl_account(self):
        acc = make_account(uid="intl-bonus", realm="intl")
        res = wb_tasks.run_streak_bonus(acc)
        self.assertFalse(res.get("ok"))
        self.assertIn("仅限国内版", res.get("msg"))

    def test_skips_enterprise_account(self):
        acc = make_account(uid="ent-bonus", realm="cn", enterpriseId="ent-123")
        res = wb_tasks.run_streak_bonus(acc)
        self.assertTrue(res.get("ok"))
        self.assertIn("企业版", res.get("msg"))

    def test_streak_bonus_executes_safely(self):
        acc = make_account(uid="cn-bonus", realm="cn")
        with mock.patch.object(wb_tasks._accounts, "urlopen") as mock_url:
            mock_resp = mock.MagicMock()
            mock_resp.read.return_value = json.dumps({"code": 0, "data": {}}).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_url.return_value = mock_resp

            res = wb_tasks.run_streak_bonus(acc)
            self.assertTrue(res.get("ok"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

