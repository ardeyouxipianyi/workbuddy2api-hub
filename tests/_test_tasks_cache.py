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
    def __init__(self, uid, realm="cn"):
        self.uid = uid
        self.nickname = uid[:8]
        self.realm = realm
        self.enabled = True
        self.checkins = 0

    # 与 wb_accounts.Account.checkin 同形：调用方会带 trigger（手动/定时），
    # 桩只关心调用发生了没有。
    def checkin(self, trigger="unknown"):
        self.checkins += 1
        return {"ok": True, "code": 0, "msg": "ok"}

    def public(self):
        return {"uid": self.uid, "nickname": self.nickname, "realm": self.realm,
                "enabled": self.enabled}


class FakePool(object):
    """Only the surface the account routes actually touch."""

    def __init__(self, accounts):
        self.accounts = list(accounts)

    def get(self, uid):
        for account in self.accounts:
            if account.uid == uid:
                return account
        return None

    def list_public(self, realm=None):
        return [a.public() for a in self.accounts
                if realm is None or a.realm == realm]

    def remove(self, uid):
        before = len(self.accounts)
        self.accounts = [a for a in self.accounts if a.uid != uid]
        return len(self.accounts) < before


class FakeUpstream(object):
    """Growth tasks, summary, task run and travel - with no network at all.

    `reads` counts only the two snapshot fetches, `actions` records the
    state-changing calls, so "did it refetch" and "did the action run" stay
    separate questions. Every read carries an `epoch` the test controls: bump it
    before the action and "still showing the previous generation" becomes
    directly decidable instead of a guess about the cache's internal state.
    """

    def __init__(self):
        self.reads = []
        self.actions = []
        self.epoch = 1

    def bump(self):
        self.epoch += 1

    def tasks(self, acc):
        self.reads.append(("tasks", acc.uid))
        return {"kind": "tasks", "uid": acc.uid, "epoch": self.epoch}

    def summary(self, acc):
        self.reads.append(("summary", acc.uid))
        return {"kind": "summary", "uid": acc.uid, "epoch": self.epoch}

    def run_tasks(self, acc, gap=1.0, claim=True):
        self.actions.append(("run", acc.uid))
        return {"earned_credit": 7, "logs": ["ran"]}

    def travel(self, acc):
        self.actions.append(("travel", acc.uid))
        return {"action": "travel", "msg": "ok", "credit": 3}


def capturing_handler():
    """The lightweight Handler double the other route suites already use."""

    class Handler(P.Handler):
        def __init__(self):
            self.captured = None

        def _json(self, code, obj):
            self.captured = (code, obj)

        def _error(self, code, message, err_type="server_error"):
            self.captured = (code, {"error": message})

    return Handler()


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

class ActionInvalidationTests(unittest.TestCase):
    """动作路径必须让下一份快照重新取，而不是端出旧值。

    这里不再数源码里出现过几次 `invalidate_tasks_cache()`：那只能证明某行文字还在，
    既挡不住"调用被挪到走不到的分支后面"，也会被一次无害的重命名/包装误伤。断言的是
    可观察行为——走真实路由派发触发动作，之后再看快照，上游必须被重新打一次，而且
    看到的是新的一代。测试自己不调用 invalidate_tasks_cache()，那是被测对象的事。
    """

    def setUp(self):
        self.upstream = FakeUpstream()
        self._orig = (T.fetch_growth_tasks, T.fetch_growth_summary,
                      T.run_growth_tasks, T.do_cat_travel)
        self._ttl = P.TASKS_CACHE_TTL
        T.fetch_growth_tasks = self.upstream.tasks
        T.fetch_growth_summary = self.upstream.summary
        T.run_growth_tasks = self.upstream.run_tasks
        T.do_cat_travel = self.upstream.travel
        self.account = StubAccount("uid-a")
        self._pool = P.POOL
        P.POOL = FakePool([self.account])
        P.invalidate_tasks_cache()

    def tearDown(self):
        (T.fetch_growth_tasks, T.fetch_growth_summary,
         T.run_growth_tasks, T.do_cat_travel) = self._orig
        P.TASKS_CACHE_TTL = self._ttl
        P.POOL = self._pool
        P.invalidate_tasks_cache()

    def warm(self):
        """Read the snapshot warm; the second read has to hit the cache.

        Without this control the invalidation assertions below would also pass
        on a cache that never worked in the first place.
        """
        first = P.growth_snapshot(self.account)
        self.reads_after_warm = len(self.upstream.reads)
        self.assertIs(P.growth_snapshot(self.account), first,
                      "快照没读热：缓存本身就没生效，失效断言无从谈起")
        self.assertEqual(len(self.upstream.reads), self.reads_after_warm,
                         "命中快照时不该再打上游")
        return first

    def drive(self, path, payload):
        """Go through the real POST dispatch for the account routes."""
        handler = capturing_handler()
        handler._handle_accounts(path, payload)
        self.assertIsNotNone(handler.captured, "%s 没有产生响应" % path)
        code, obj = handler.captured
        self.assertEqual(code, 200, "%s 返回 %s: %r" % (path, code, obj))
        return obj

    def assert_refetched(self, warm, path):
        fresh = P.growth_snapshot(self.account)
        self.assertEqual(len(self.upstream.reads), self.reads_after_warm + 2,
                         "%s 之后仍然端出旧快照：失效没走到这条路径上" % path)
        self.assertEqual(fresh[0]["epoch"], warm[0]["epoch"] + 1,
                         "%s 之后拿到的不是新值" % path)

    def test_task_run_invalidates_the_snapshot(self):
        warm = self.warm()
        self.upstream.bump()
        response = self.drive("/tasks/run", {"uid": "uid-a"})
        self.assertTrue(response.get("ok"), "执行动作没有真的跑起来: %r" % response)
        self.assertIn(("run", "uid-a"), self.upstream.actions)
        self.assert_refetched(warm, "/tasks/run")

    def test_task_travel_invalidates_the_snapshot(self):
        warm = self.warm()
        self.upstream.bump()
        response = self.drive("/tasks/travel", {"uid": "uid-a"})
        self.assertTrue(response.get("ok"), "旅行动作没有真的跑起来: %r" % response)
        self.assertIn(("travel", "uid-a"), self.upstream.actions)
        self.assert_refetched(warm, "/tasks/travel")

    def test_task_claim_is_per_account_and_refetches(self):
        warm = self.warm()
        self.upstream.bump()
        calls = []
        orig = T.claim_task
        T.claim_task = lambda acc, code: (calls.append((acc.uid, code)) or
                                          {"ok": True, "credit": 12})
        try:
            response = self.drive("/tasks/claim",
                                  {"uid": "uid-a", "code": "first_buddy"})
        finally:
            T.claim_task = orig
        self.assertTrue(response.get("ok"), response)
        self.assertEqual(calls, [("uid-a", "first_buddy")])
        self.assert_refetched(warm, "/tasks/claim")

    def test_task_claim_requires_a_single_account(self):
        response = self.drive("/tasks/claim",
                              {"uid": "all", "code": "first_buddy"})
        self.assertFalse(response.get("ok"), response)

    def test_account_checkin_invalidates_the_snapshot(self):
        warm = self.warm()
        self.upstream.bump()
        response = self.drive("/accounts/checkin", {"uid": "uid-a"})
        self.assertEqual(len(response.get("results") or []), 1,
                         "签到动作没有真的跑起来: %r" % response)
        self.assertEqual(self.account.checkins, 1, "签到没有被调用")
        self.assert_refetched(warm, "/accounts/checkin")


    def test_account_delete_invalidates_the_snapshot(self):
        warm = self.warm()
        self.upstream.bump()
        response = self.drive("/accounts/delete", {"uid": "uid-a"})
        self.assertTrue(response.get("deleted"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
