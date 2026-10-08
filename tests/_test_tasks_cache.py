"""GET /tasks 的短 TTL 快照：命中不重复打上游，动作类接口要能立刻失效。

    python tests/_test_tasks_cache.py

背景：上游那两个接口（成长任务 + 汇总）实测各约 1 秒，看板切区域、切账号、切页面
各打一次，用户侧就是"点了要等几秒"。快照只在只读路径生效，且任何会改变任务状态的
动作（执行/旅行/签到）都会先清掉它——这条用测试钉住，别哪天又退化成"点完执行看到的
还是旧状态"。
"""
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _isolated_dirs import isolated_data_dirs  # noqa: E402  (its own data root)
# Its own directories, removed when this process exits. This suite used to share
# tests/_acc_test and tests/_use_test with _test_daily_chat.py and
# _test_sanitize_fingerprint.py, which made them unsafe to run at the same time
# and left directories in the repo after a run.
_TMP = isolated_data_dirs("wb-tasks-cache-")

import wb_proxy as P        # noqa: E402
import wb_tasks as T        # noqa: E402


class StubAccount(object):
    def __init__(self, uid):
        self.uid = uid
        self.nickname = uid[:8]


class TasksCacheTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._orig = (T.fetch_growth_tasks, T.fetch_growth_summary)
        self._ttl = P.TASKS_CACHE_TTL

        def tasks(acc):
            self.calls.append(("tasks", acc.uid))
            return {"tasks": [], "for": acc.uid}

        def summary(acc):
            self.calls.append(("summary", acc.uid))
            return {"summary": {}, "for": acc.uid}

        self._stub = tasks
        T.fetch_growth_tasks = tasks
        T.fetch_growth_summary = summary
        P.invalidate_tasks_cache()

    def tearDown(self):
        T.fetch_growth_tasks, T.fetch_growth_summary = self._orig
        P.TASKS_CACHE_TTL = self._ttl
        P.invalidate_tasks_cache()

    def test_repeat_read_is_served_from_snapshot(self):
        acc = StubAccount("uid-a")
        first = P.growth_snapshot(acc)
        second = P.growth_snapshot(acc)
        self.assertEqual(len(self.calls), 2, "第二次读不该再打上游")
        self.assertEqual(first, second)
        self.assertIs(first[0], second[0], "应复用同一份对象")

    def test_invalidate_forces_a_refetch(self):
        acc = StubAccount("uid-a")
        P.growth_snapshot(acc)
        P.invalidate_tasks_cache()
        P.growth_snapshot(acc)
        self.assertEqual(len(self.calls), 4, "失效后必须重新取一次")

    def test_snapshot_is_per_account(self):
        a, b = StubAccount("uid-a"), StubAccount("uid-b")
        P.growth_snapshot(a)
        P.growth_snapshot(b)
        P.growth_snapshot(a)          # 命中 a 的快照
        self.assertEqual(self.calls, [("tasks", "uid-a"), ("summary", "uid-a"),
                                      ("tasks", "uid-b"), ("summary", "uid-b")])

    def test_ttl_expiry(self):
        acc = StubAccount("uid-a")
        P.TASKS_CACHE_TTL = 0.05
        P.growth_snapshot(acc)
        time.sleep(0.12)
        P.growth_snapshot(acc)
        self.assertEqual(len(self.calls), 4, "过期后应重新取")

    def test_ttl_zero_disables_cache(self):
        acc = StubAccount("uid-a")
        P.TASKS_CACHE_TTL = 0
        P.growth_snapshot(acc)
        P.growth_snapshot(acc)
        self.assertEqual(len(self.calls), 4)

    def test_failure_is_not_cached(self):
        acc = StubAccount("uid-a")
        T.fetch_growth_tasks = lambda a: (_ for _ in ()).throw(RuntimeError("upstream down"))
        with self.assertRaises(RuntimeError):
            P.growth_snapshot(acc)
        T.fetch_growth_tasks = self._stub
        P.growth_snapshot(acc)
        self.assertTrue(self.calls, "失败不该被缓存成快照")

    def test_action_routes_invalidate(self):
        """执行/旅行/签到三条动作路径都必须清快照（源码级断言，防止漏改）。"""
        import inspect
        src = inspect.getsource(P.Handler._route_tasks_run) + \
            inspect.getsource(P.Handler._route_tasks_travel) + \
            inspect.getsource(P.Handler._route_accounts_checkin)
        self.assertEqual(src.count("invalidate_tasks_cache()"), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
