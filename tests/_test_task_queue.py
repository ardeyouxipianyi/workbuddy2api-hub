"""Task center queue (M3 C1) and the single-task runner.

Run with: python _test_task_queue.py
No upstream credentials or outbound network are used.
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="task-queue-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_taskqueue
import wb_tasks


class FakeAccount(object):
    def __init__(self, uid, nickname="", realm="cn", enabled=True):
        self.uid = uid
        self.nickname = nickname
        self.realm = realm
        self.enabled = enabled


class FakePool(object):
    def __init__(self, accounts):
        self.accounts = accounts

    def get(self, uid):
        return next((a for a in self.accounts if a.uid == uid), None)


def task(code, status="accepted", current=0, target=1, **extra):
    item = {"task_code": code, "name": code, "status": status,
            "accept_status": status, "current": current, "target": target,
            "locked": False, "claimable": False, "claimed": False,
            "unforgeable": False}
    item.update(extra)
    return item


class PendingFilterTests(unittest.TestCase):
    def test_pending_rules(self):
        self.assertTrue(wb_taskqueue.TaskQueue.pending(task("chat_5", current=0, target=5)))
        self.assertFalse(wb_taskqueue.TaskQueue.pending(task("chat_5", claimed=True)))
        self.assertFalse(wb_taskqueue.TaskQueue.pending(task("chat_5", status="claimed")))
        self.assertFalse(wb_taskqueue.TaskQueue.pending(task("chat_5", locked=True)))
        self.assertFalse(wb_taskqueue.TaskQueue.pending(task("chat_5", unforgeable=True)))
        self.assertFalse(wb_taskqueue.TaskQueue.pending(task("unknown_code")))
        self.assertFalse(wb_taskqueue.TaskQueue.pending(
            {"task_code": "Expert_Philanthropy", "unforgeable": True}))

    def test_night_task_needs_the_window(self):
        with mock.patch.object(wb_tasks, "in_night_window", return_value=False):
            self.assertFalse(wb_taskqueue.TaskQueue.pending(task("black_cat", target=3)))
        with mock.patch.object(wb_tasks, "in_night_window", return_value=True):
            self.assertTrue(wb_taskqueue.TaskQueue.pending(task("black_cat", target=3)))

    def test_concurrency_is_clamped(self):
        self.assertEqual(wb_taskqueue.TaskQueue.clamp_concurrency(0), 1)
        self.assertEqual(wb_taskqueue.TaskQueue.clamp_concurrency(2), 2)
        self.assertEqual(wb_taskqueue.TaskQueue.clamp_concurrency(99), 3)
        self.assertEqual(wb_taskqueue.TaskQueue.clamp_concurrency("x"), 1)


class ScanTests(unittest.TestCase):
    def test_scan_filters_and_counts(self):
        accounts = [FakeAccount("uid-1", "One"), FakeAccount("uid-2", "Two"),
                    FakeAccount("uid-intl", "Intl", realm="intl"),
                    FakeAccount("uid-off", "Off", enabled=False)]
        queue = wb_taskqueue.TaskQueue(FakePool(accounts), runner=lambda a, c: (True, "", 0))

        def fake_fetch(account):
            if account.uid == "uid-1":
                return [task("chat_5", current=0, target=5),
                        task("template_5", claimed=True),
                        task("unknown_code")]
            return [task("create_canvas", locked=True),
                    task("automation_1")]

        with mock.patch.object(wb_taskqueue.wb_tasks, "fetch_growth_tasks",
                               side_effect=fake_fetch):
            result = queue.scan()
        self.assertTrue(result["ok"])
        self.assertEqual([i["uid"] for i in result["accounts"]], ["uid-1", "uid-2"])
        self.assertEqual(result["pending_count"], 2)
        self.assertEqual([t["task_code"] for t in result["accounts"][0]["growth"]],
                         ["chat_5"])
        self.assertEqual([t["task_code"] for t in result["accounts"][1]["growth"]],
                         ["automation_1"])


class ScanTimeoutTests(unittest.TestCase):
    def test_late_worker_results_do_not_reach_the_returned_scan(self):
        accounts = [FakeAccount("uid-1", "One")]
        release = threading.Event()
        started = threading.Event()

        def slow_fetch(account, mp=False):
            started.set()
            release.wait(timeout=5)
            return [task("chat_5")]

        class NoWaitThread(threading.Thread):
            def join(self, timeout=None):
                # Simulate the 60s join window elapsing while the worker runs.
                return None

        queue = wb_taskqueue.TaskQueue(FakePool(accounts),
                                       runner=lambda a, c: (True, "ok", 0))
        with mock.patch.object(wb_taskqueue.wb_tasks, "fetch_growth_tasks",
                               side_effect=slow_fetch), \
                mock.patch.object(wb_taskqueue.threading, "Thread", NoWaitThread):
            result = queue.scan()
        self.assertTrue(started.wait(timeout=2), "the worker never started")
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["accounts"], [])
        release.set()
        time.sleep(0.3)  # let the late worker try to write its result
        self.assertEqual(result["accounts"], [],
                         "a late worker mutated the returned scan")


class QueueRunTests(unittest.TestCase):
    def make_queue(self, accounts, runner, concurrency=1):
        return wb_taskqueue.TaskQueue(FakePool(accounts), runner=runner,
                                      concurrency=concurrency)

    def tasks_for(self, account):
        if account.uid == "uid-1":
            return [task("chat_5", current=0, target=5), task("template_5")]
        return [task("create_canvas")]

    def test_queue_runs_every_item_with_per_account_serialisation(self):
        accounts = [FakeAccount("uid-1", "One"), FakeAccount("uid-2", "Two")]
        active = {}
        max_global = {"n": 0}
        lock = threading.Lock()
        seen = []

        def runner(account, code):
            with lock:
                active[account.uid] = active.get(account.uid, 0) + 1
                seen.append((account.uid, code))
                self.assertLessEqual(active[account.uid], 1)
                max_global["n"] = max(max_global["n"], sum(active.values()))
            time.sleep(0.02)
            with lock:
                active[account.uid] -= 1
            return True, "done " + code, 0

        queue = self.make_queue(accounts, runner, concurrency=1)
        with mock.patch.object(wb_taskqueue.wb_tasks, "fetch_growth_tasks",
                               side_effect=self.tasks_for):
            result = queue.start()
            self.assertTrue(result["started"])
            self.assertEqual(result["total"], 3)
            self.assertTrue(queue.wait(timeout=10))
        status = queue.status()
        self.assertFalse(status["running"])
        self.assertEqual(status["counts"].get("done"), 3)
        self.assertLessEqual(max_global["n"], 1)
        self.assertEqual(len(seen), 3)

    def test_queue_respects_concurrency_across_accounts(self):
        accounts = [FakeAccount("uid-%d" % i) for i in range(1, 4)]
        active = {"n": 0}
        max_active = {"n": 0}
        lock = threading.Lock()

        def runner(account, code):
            with lock:
                active["n"] += 1
                max_active["n"] = max(max_active["n"], active["n"])
            time.sleep(0.05)
            with lock:
                active["n"] -= 1
            return True, "ok", 0

        queue = self.make_queue(accounts, runner, concurrency=2)

        def fetch(account):
            return [task("chat_5", target=1)]

        with mock.patch.object(wb_taskqueue.wb_tasks, "fetch_growth_tasks",
                               side_effect=fetch):
            result = queue.start()
            self.assertTrue(result["started"])
            self.assertEqual(result["concurrency"], 2)
            self.assertTrue(queue.wait(timeout=10))
        self.assertEqual(max_active["n"], 2)

    def test_second_start_is_rejected_while_running(self):
        accounts = [FakeAccount("uid-1")]
        release = threading.Event()

        def runner(account, code):
            release.wait(timeout=5)
            return True, "ok", 0

        queue = self.make_queue(accounts, runner)
        with mock.patch.object(wb_taskqueue.wb_tasks, "fetch_growth_tasks",
                               return_value=[task("chat_5")]):
            first = queue.start()
            self.assertTrue(first["started"])
            second = queue.start()
            self.assertFalse(second["started"])
            release.set()
            self.assertTrue(queue.wait(timeout=10))

    def test_concurrent_start_calls_only_one_wins(self):
        """BUG-2: the scan window must not let a second start through."""
        accounts = [FakeAccount("uid-1")]
        release = threading.Event()
        scan_started = threading.Event()

        def slow_fetch(account):
            scan_started.set()
            release.wait(timeout=5)
            return [task("chat_5")]

        def runner(account, code):
            return True, "ok", 0

        queue = self.make_queue(accounts, runner)
        results = []
        with mock.patch.object(wb_taskqueue.wb_tasks, "fetch_growth_tasks",
                               side_effect=slow_fetch):
            first = threading.Thread(target=lambda: results.append(queue.start()))
            second = threading.Thread(target=lambda: results.append(queue.start()))
            first.start()
            self.assertTrue(scan_started.wait(timeout=5),
                            "the first scan never started")
            second.start()
            time.sleep(0.3)  # let the second caller reach the guard
            release.set()
            first.join(timeout=10)
            second.join(timeout=10)
            self.assertTrue(queue.wait(timeout=10))
        started = [r for r in results if r.get("started")]
        self.assertEqual(len(started), 1, results)

    def test_no_pending_items_reports_cleanly(self):
        queue = self.make_queue([FakeAccount("uid-1")], lambda a, c: (True, "", 0))
        with mock.patch.object(wb_taskqueue.wb_tasks, "fetch_growth_tasks",
                               return_value=[]):
            result = queue.start()
        self.assertFalse(result["started"])
        self.assertEqual(result["total"], 0)

    def test_runner_errors_become_error_items(self):
        accounts = [FakeAccount("uid-1")]

        def runner(account, code):
            if code == "template_5":
                raise RuntimeError("boom")
            return False, "rejected", 0

        queue = self.make_queue(accounts, runner)
        with mock.patch.object(wb_taskqueue.wb_tasks, "fetch_growth_tasks",
                               return_value=[task("chat_5"), task("template_5")]):
            queue.start()
            self.assertTrue(queue.wait(timeout=10))
        status = queue.status()
        self.assertEqual(status["counts"].get("error"), 2)
        messages = [item["message"] for item in status["items"]]
        self.assertIn("rejected", messages)
        self.assertIn("boom", messages)

    def test_account_lock_skips_a_busy_account(self):
        accounts = [FakeAccount("uid-1")]
        queue = self.make_queue(accounts, lambda a, c: (True, "ok", 0))
        lock = queue.account_lock("uid-1")
        lock.acquire()
        try:
            with mock.patch.object(wb_taskqueue.wb_tasks, "fetch_growth_tasks",
                                   return_value=[task("chat_5")]):
                queue.start()
                self.assertTrue(queue.wait(timeout=10))
        finally:
            lock.release()
        status = queue.status()
        self.assertEqual(status["counts"].get("skipped"), 1)


class SingleTaskRunnerTests(unittest.TestCase):
    def setUp(self):
        self.account = FakeAccount("uid-1", "One")
        self.sleep_patch = mock.patch.object(wb_tasks.time, "sleep", return_value=None)
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def test_non_cn_account_is_refused(self):
        ok, message, _credit = wb_tasks.run_single_task(FakeAccount("u", realm="intl"), "chat_5")
        self.assertFalse(ok)
        self.assertIn("国际版", message)

    def test_missing_task(self):
        with mock.patch.object(wb_tasks, "fetch_growth_tasks", return_value=[]):
            ok, message, _credit = wb_tasks.run_single_task(self.account, "chat_5")
        self.assertFalse(ok)
        self.assertIn("无此任务", message)

    def test_claimed_task_skips(self):
        with mock.patch.object(wb_tasks, "fetch_growth_tasks",
                               return_value=[task("chat_5", claimed=True)]):
            ok, message, credit = wb_tasks.run_single_task(self.account, "chat_5")
        self.assertTrue(ok)
        self.assertIn("已领取", message)
        self.assertEqual(credit, 0)

    def test_locked_task_skips(self):
        with mock.patch.object(wb_tasks, "fetch_growth_tasks",
                               return_value=[task("chat_5", locked=True)]):
            ok, message, _credit = wb_tasks.run_single_task(self.account, "chat_5")
        self.assertTrue(ok)
        self.assertIn("未解锁", message)

    def test_unforgeable_task_is_refused(self):
        with mock.patch.object(wb_tasks, "fetch_growth_tasks",
                               return_value=[task("Expert_Philanthropy", unforgeable=True)]):
            ok, message, _credit = wb_tasks.run_single_task(
                self.account, "Expert_Philanthropy")
        self.assertFalse(ok)
        self.assertIn("不可自动化", message)

    def test_claimable_task_is_claimed(self):
        with mock.patch.object(wb_tasks, "fetch_growth_tasks",
                               return_value=[task("chat_5", claimable=True)]), \
                mock.patch.object(wb_tasks, "claim_task",
                                  return_value={"ok": True, "credit": 100}):
            ok, message, credit = wb_tasks.run_single_task(self.account, "chat_5")
        self.assertTrue(ok)
        self.assertEqual(credit, 100)
        self.assertIn("领奖成功", message)

    def test_full_action_flow(self):
        calls = {"fetch": 0, "claim": 0}

        def fetch(account):
            calls["fetch"] += 1
            if calls["fetch"] == 1:
                return [task("chat_5", status="not_accepted", current=0, target=5)]
            return [task("chat_5", status="accepted", current=5, target=5)]

        def claim(account, code):
            calls["claim"] += 1
            return {"ok": True, "credit": 100}

        with mock.patch.object(wb_tasks, "fetch_growth_tasks", side_effect=fetch), \
                mock.patch.object(wb_tasks, "accept_tasks",
                                  return_value={"accepted": ["chat_5"], "failed": []}), \
                mock.patch.object(wb_tasks, "claim_task", side_effect=claim), \
                mock.patch.dict(wb_tasks.DESKTOP_ACTIONS,
                                {"chat_5": lambda account, need: (True, "chain sent")}):
            ok, message, credit = wb_tasks.run_single_task(self.account, "chat_5")
        self.assertTrue(ok)
        self.assertEqual(credit, 100)
        self.assertIn("chain sent", message)
        self.assertEqual(calls["claim"], 1)

    def test_progress_not_reached_reports_and_skips_claim(self):
        with mock.patch.object(wb_tasks, "fetch_growth_tasks",
                               return_value=[task("chat_5", current=0, target=5)]), \
                mock.patch.object(wb_tasks, "claim_task") as claim, \
                mock.patch.dict(wb_tasks.DESKTOP_ACTIONS,
                                {"chat_5": lambda account, need: (True, "chain sent")}):
            ok, message, _credit = wb_tasks.run_single_task(self.account, "chat_5")
        self.assertFalse(ok)
        self.assertIn("未达成", message)
        claim.assert_not_called()


if __name__ == "__main__":
    unittest.main()
