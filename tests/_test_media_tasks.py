"""视频任务表（wb_media_tasks）的测试：状态机、落盘、签名、磁盘清理、重启语义。

这一套件**同步驱动** worker（`autostart=False` + 直接调 `_run_task`），所以没有线程
时序的偶然性；出站层与账号解析全部替身，一个字节不出网。

钉住的是几件容易写错、错了又要花钱才发现的事：

  - 提交后立刻是 `queued`，且**只留 prompt 头 80 字符**（不留全文）；
  - 轮询用提交它的那个账号，账号没了就记 `failed` 而不是换号重提交（那是再花一次钱）；
  - 下载失败时把上游已给的 URL 留在任务行里，线索不能丢；
  - 进程重启时 `queued`/`running` 一律变 `lost`——只报 failed 的话「钱花了没产物」查不到；
  - 下载令牌过期、篡改、索引不符全部拒绝；
  - 产物按 TTL 与磁盘总量清理，但**只删已完成任务**的 mp4。

Run with: python tests/_test_media_tasks.py
"""
import hmac
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_media as M
import wb_media_tasks as T

_TMP = tempfile.TemporaryDirectory(prefix="wb-media-tasks-")


class FakeAccount(object):
    def __init__(self, uid="u-1"):
        self.uid = uid


class FakeClient(object):
    def __init__(self, outcome=None, error=None, download_error=None, payload=b"MP4DATA"):
        self.outcome = outcome or {"status": "completed",
                                   "videos": ["https://cos/a.mp4"], "credit": 71.43}
        self.error = error
        self.download_error = download_error
        self.payload = payload
        self.polls = []
        self.downloads = []

    def poll_video_task(self, account, upstream_id):
        self.polls.append((account.uid, upstream_id))
        if self.error is not None:
            raise self.error
        return dict(self.outcome)

    def download_video(self, account, url, dest_dir, index=0):
        self.downloads.append((account.uid, url, index))
        if self.download_error is not None:
            raise self.download_error
        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, "video-test-%d.mp4" % index)
        with open(path, "wb") as handle:
            handle.write(self.payload)
        return path


_STORE_SEQ = [0]


def make_store(client=None, accounts=None, **kwargs):
    """每个 store 一个独立的产物目录。

    共用同一个目录会让上一条用例落盘的 tasks.json 被下一条读回来——`total` 变成
    历史累加值，测试变成互相依赖。一个用例一个目录，谁都不影响谁。
    """
    accounts = accounts if accounts is not None else {"u-1": FakeAccount()}
    kwargs.setdefault("autostart", False)
    submitter = kwargs.pop("submitter", None)
    _STORE_SEQ[0] += 1
    store = T.MediaTaskStore(
        out_dir=os.path.join(_TMP.name, "media-%d" % _STORE_SEQ[0]),
        client=client or FakeClient(),
        resolve_account=lambda uid: accounts.get(uid),
        **kwargs)
    if submitter is not None:
        store.set_submitter(submitter)
    return store


PARAMS = {"seconds": 5, "resolution": "720P", "aspect_ratio": "16:9",
          "enable_audio": True, "image_to_video": False, "warnings": []}
BODY = {"prompt": "海边日落" * 40, "model": "kling-video-v3", "seconds": 5}


class CreateTests(unittest.TestCase):
    def test_create_starts_queued_and_trims_prompt(self):
        store = make_store()
        task = store.create("kling-video-v3", PARAMS, "u-1", BODY, "up-1")
        self.assertEqual(task["status"], "queued")
        self.assertTrue(task["id"].startswith("vid_"))
        self.assertEqual(len(task["prompt_head"]), 80)
        self.assertEqual(task["prompt_len"], len(BODY["prompt"]))
        self.assertNotIn("prompt", task)        # 全文不落任务行
        store.stop()

    def test_pending_count_tracks_unfinished(self):
        store = make_store()
        store.create("m", PARAMS, "u-1", BODY, "up-1")
        store.create("m", PARAMS, "u-1", BODY, "up-2")
        self.assertEqual(store.pending_count(), 2)
        store.stop()

    def test_stats_shape(self):
        store = make_store()
        store.create("m", PARAMS, "u-1", BODY, "up-1")
        stats = store.stats()
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["pending"], 1)
        self.assertEqual(stats["by_status"]["queued"], 1)
        # 轮询线程数默认等于并发数（3）：单线程会被一条慢任务拖住，看起来像并发没生效。
        self.assertEqual(stats["workers"], store.workers)
        self.assertEqual(stats["workers"], T.DEFAULT_CONCURRENCY)
        self.assertEqual(stats["concurrency"], T.DEFAULT_CONCURRENCY)
        self.assertEqual(stats["active"], 1)
        self.assertEqual(stats["waiting"], 0)
        store.stop()


class QueueTests(unittest.TestCase):
    """并发闸 + 排队：超过并发数的提交**不进上游、不花钱**。"""

    def test_pending_task_is_not_submitted_yet(self):
        store = make_store(submitter=None)
        task = store.create_pending("minimax-video-h3", PARAMS, BODY)
        self.assertEqual(task["status"], "waiting")
        self.assertIsNone(task["upstream_id"])
        self.assertEqual(store.active_count(), 0)     # 没占用并发
        self.assertEqual(store.waiting_count(), 1)
        self.assertEqual(store.queue_position(task["id"]), 1)
        store.stop()

    def test_pump_submits_and_moves_to_queue(self):
        submitted = []

        def submitter(task):
            submitted.append(task["id"])
            return {"upstream_id": "up-9", "account": "u-9"}

        store = make_store(client=FakeClient(), submitter=submitter)
        task = store.create_pending("minimax-video-h3", PARAMS, BODY)
        store._pump()
        self.assertEqual(submitted, [task["id"]])
        row = store.get(task["id"])
        self.assertEqual(row["status"], "queued")     # 已受理，等轮询
        self.assertEqual(row["upstream_id"], "up-9")
        self.assertEqual(row["account"], "u-9")
        store.stop()

    def test_pump_respects_the_concurrency_cap(self):
        submitted = []

        def submitter(task):
            submitted.append(task["id"])
            return {"upstream_id": "up-%d" % len(submitted), "account": "u-1"}

        store = make_store(client=FakeClient(), workers=1, concurrency=2,
                           submitter=submitter)
        for _ in range(5):
            store.create_pending("minimax-video-h3", PARAMS, BODY)
        store._pump()
        # 起来 2 条到并发上限就停手；其余仍在排队。
        self.assertEqual(len(submitted), 2)
        self.assertEqual(store.waiting_count(), 3)
        store.stop()

    def test_rejected_submission_costs_nothing_and_is_retryable(self):
        def submitter(task):
            raise M.MediaError("REJECTED", "上游拒绝：seconds 越界")

        store = make_store(client=FakeClient(), submitter=submitter)
        task = store.create_pending("minimax-video-h3", PARAMS, BODY)
        store._pump()
        row = store.get(task["id"])
        # 未扣费 → rejected（对外 failed + invalid_request），不是 lost。
        self.assertEqual(row["status"], "rejected")
        self.assertIn("越界", row["error"])
        self.assertEqual(store.active_count(), 0)
        store.stop()

    def test_busy_accounts_reports_running_tasks(self):
        store = make_store(submitter=None)
        store.create("m", PARAMS, "u-1", BODY, "up-1")
        store.create("m", PARAMS, "u-2", BODY, "up-2")
        self.assertEqual(store.busy_accounts(), {"u-1", "u-2"})
        store.stop()

    def test_restart_marks_waiting_as_retryable_not_lost(self):
        """排队中的任务重启后是 failed（没花钱），不是 lost（钱可能花了）。"""
        # 两个 store 必须共用同一个产物目录，否则读回来的是空表（每个 make_store
        # 默认开新目录，见它的 docstring）。
        out_dir = os.path.join(_TMP.name, "media-restart")
        store = T.MediaTaskStore(out_dir=out_dir, client=FakeClient(),
                                 resolve_account=lambda uid: None, autostart=False)
        store.create_pending("minimax-video-h3", PARAMS, BODY)
        store.stop()

        reloaded = T.MediaTaskStore(out_dir=out_dir, client=FakeClient(),
                                    resolve_account=lambda uid: None, autostart=False)
        row = reloaded.list()[0]
        self.assertEqual(row["status"], "failed")
        self.assertIn("未扣费", row["error"])
        reloaded.stop()


class CancelTests(QueueTests):
    """取消的三种语义，区别就是钱。"""

    def test_cancel_waiting_task_costs_nothing_and_leaves_queue(self):
        store = make_store(submitter=lambda task: {"taskId": "up-1", "account": "u-1"})
        queued = store.create_pending("minimax-video-h3", PARAMS, BODY)
        self.assertEqual(queued["status"], "waiting")
        self.assertTrue(store.cancel(queued["id"]))
        task = store.get(queued["id"])
        self.assertEqual(task["status"], "failed")
        self.assertIn("未扣费", task["error"])
        # 摘掉后不该再被调度
        self.assertEqual(store.waiting_count(), 0)
        # 任务体也不能留着（否则 pump 之后还能拿到 prompt）
        self.assertNotIn(queued["id"], store._waiting_bodies)
        store.stop()

    def test_cancel_running_is_refused(self):
        store = make_store()
        task = store.create("kling-video-v3", PARAMS, "u-1", BODY, "up-1")
        with store._lock:
            store._tasks[task["id"]]["status"] = "running"
        self.assertFalse(store.cancel(task["id"]))
        self.assertEqual(store.get(task["id"])["status"], "running")
        store.stop()

    def test_wait_does_not_return_while_still_queued(self):
        """?wait=1 不能因为"还在本地排队"就立刻返回。"""
        store = make_store(submitter=None)
        task = store.create_pending("minimax-video-h3", PARAMS, BODY)
        seen = []
        def fake_sleep(_seconds):
            seen.append(1)
            with store._lock:               # 第二次"睡"的时候让它完成
                store._tasks[task["id"]]["status"] = "completed"
        result = store.wait(task["id"], timeout=30, interval=0, sleep=fake_sleep)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(seen)
        store.stop()


class WorkerTests(unittest.TestCase):
    def test_completes_and_downloads(self):
        client = FakeClient()
        store = make_store(client)
        task = store.create("kling-video-v3", PARAMS, "u-1", BODY, "up-1")
        store._run_task(task["id"])
        done = store.get(task["id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["credit"], 71.43)
        self.assertEqual(len(done["videos"]), 1)
        self.assertTrue(os.path.isfile(done["videos"][0]))
        # 轮询用的是提交它的那个账号，且用的是上游 task id
        self.assertEqual(client.polls, [("u-1", "up-1")])
        store.stop()

    def test_poll_failure_marks_failed_with_reason(self):
        client = FakeClient(error=M.MediaError("UPSTREAM", "任务失败：content rejected"))
        store = make_store(client)
        task = store.create("m", PARAMS, "u-1", BODY, "up-1")
        store._run_task(task["id"])
        done = store.get(task["id"])
        self.assertEqual(done["status"], "failed")
        self.assertIn("content rejected", done["error"])
        store.stop()

    def test_download_failure_keeps_upstream_urls(self):
        """产物生成了却下不下来：URL 必须留在任务行里，否则线索全丢。"""
        client = FakeClient(download_error=M.MediaError("UPSTREAM", "download boom"))
        store = make_store(client)
        task = store.create("m", PARAMS, "u-1", BODY, "up-1")
        store._run_task(task["id"])
        done = store.get(task["id"])
        self.assertEqual(done["status"], "failed")
        self.assertIn("download boom", done["error"])
        self.assertEqual(done["videos"], ["https://cos/a.mp4"])
        store.stop()

    def test_missing_account_marks_lost_not_failed(self):
        """账号取不到时标 `lost` 而不是 `failed`。

        task id 属于那个账号，换号轮询只会 404，所以要停；但上游任务**可能已经受理
        并扣了费**——记 failed 会让「钱花了没产物」查不到原因，而 lost 的语义正是
        「本地丢了线索、上游可能已花过钱」。
        """
        client = FakeClient()
        store = make_store(client, accounts={})
        task = store.create("m", PARAMS, "u-9", BODY, "up-1")
        store._run_task(task["id"])
        done = store.get(task["id"])
        self.assertEqual(done["status"], "lost")
        self.assertIn("可能仍在进行且已扣费", done["error"])
        self.assertEqual(client.polls, [])
        store.stop()

    def test_resolver_is_required_and_never_imports_wb_proxy(self):
        """没有注入 resolver 时返回 None，绝不 `import wb_proxy`。

        踩过的坑：wb_proxy 以脚本方式运行时模块名是 `__main__`，`import wb_proxy`
        会再执行一遍模块代码、拿到 `POOL is None` 的影子模块——任务提交成功却立刻
        「账号已不可用」。这条钉住的就是"再也不许自己 import"。
        """
        store = T.MediaTaskStore(out_dir=os.path.join(_TMP.name, "noresolver"),
                                 client=FakeClient(), autostart=False)
        self.assertIsNone(store._account_for("u-1"))
        store.stop()

    def test_worker_survives_client_exception(self):
        class Broken(FakeClient):
            def poll_video_task(self, account, upstream_id):
                raise RuntimeError("boom")

        store = make_store(Broken())
        task = store.create("m", PARAMS, "u-1", BODY, "up-1")
        store._run_task(task["id"])
        self.assertEqual(store.get(task["id"])["status"], "failed")
        store.stop()

    def test_running_task_is_not_rerun(self):
        client = FakeClient()
        store = make_store(client)
        task = store.create("m", PARAMS, "u-1", BODY, "up-1")
        store._update(task["id"], status="running")
        store._run_task(task["id"])
        self.assertEqual(client.polls, [])
        store.stop()


class CancelTests(unittest.TestCase):
    def test_cancel_queued(self):
        store = make_store()
        task = store.create("m", PARAMS, "u-1", BODY, "up-1")
        self.assertTrue(store.cancel(task["id"]))
        done = store.get(task["id"])
        # 取消用独立状态，不再混进 failed：对外 error.code 给出 cancelled，
        # 客户端据此知道"没扣费、可重试"；failed 意味着"上游可能已扣费"。
        self.assertEqual(done["status"], "cancelled")
        self.assertIn("cancelled", done["error"])
        store.stop()

    def test_cancel_running_is_refused(self):
        store = make_store()
        task = store.create("m", PARAMS, "u-1", BODY, "up-1")
        store._update(task["id"], status="running")
        self.assertFalse(store.cancel(task["id"]))
        store.stop()

    def test_cancel_unknown(self):
        store = make_store()
        self.assertFalse(store.cancel("nope"))
        store.stop()


class SignTests(unittest.TestCase):
    def test_round_trip(self):
        store = make_store()
        token = store.sign("vid_abc", 0)
        self.assertTrue(store.verify("vid_abc", 0, token))
        store.stop()

    def test_index_is_bound(self):
        store = make_store()
        token = store.sign("vid_abc", 0)
        self.assertFalse(store.verify("vid_abc", 1, token))
        store.stop()

    def test_task_is_bound(self):
        store = make_store()
        token = store.sign("vid_abc", 0)
        self.assertFalse(store.verify("vid_other", 0, token))
        store.stop()

    def test_expired(self):
        store = make_store(clock=lambda: 1000.0)
        token = store.sign("vid_abc", 0, ttl=10)
        self.assertTrue(store.verify("vid_abc", 0, token, now=1005))
        self.assertFalse(store.verify("vid_abc", 0, token, now=1011))
        store.stop()

    def test_tampered(self):
        store = make_store()
        token = store.sign("vid_abc", 0)
        exp, _, mac = token.partition(".")
        bad = "%s.%s" % (exp, "0" * len(mac) if mac != "0" * len(mac) else "1" * len(mac))
        self.assertFalse(store.verify("vid_abc", 0, bad))
        self.assertFalse(store.verify("vid_abc", 0, "garbage"))
        self.assertFalse(store.verify("vid_abc", 0, ""))
        store.stop()

    def test_tokens_differ_between_stores(self):
        """secret 是进程内的：换一个进程（或实例）旧链接即失效。"""
        first = make_store()
        second = make_store()
        token = first.sign("vid_abc", 0)
        self.assertFalse(second.verify("vid_abc", 0, token))
        first.stop()
        second.stop()


class ContentTests(unittest.TestCase):
    def test_content_path_after_download(self):
        client = FakeClient()
        store = make_store(client)
        task = store.create("m", PARAMS, "u-1", BODY, "up-1")
        store._run_task(task["id"])
        path = store.content_path(task["id"], 0)
        self.assertIsNotNone(path)
        self.assertTrue(os.path.isfile(path))
        self.assertIsNone(store.content_path(task["id"], 5))
        self.assertIsNone(store.content_path("nope", 0))
        store.stop()


class PersistenceTests(unittest.TestCase):
    def test_reload_marks_unfinished_as_lost(self):
        """重启时还在跑的任务必须变 lost：上游可能已经扣了费。"""
        out_dir = os.path.join(_TMP.name, "reload-media")
        store = T.MediaTaskStore(out_dir=out_dir, client=FakeClient(),
                                 resolve_account=lambda uid: FakeAccount(),
                                 autostart=False)
        done = store.create("m", PARAMS, "u-1", BODY, "up-done")
        store._run_task(done["id"])
        stuck = store.create("m", PARAMS, "u-1", BODY, "up-stuck")
        store._update(stuck["id"], status="running")
        store.stop()

        reopened = T.MediaTaskStore(out_dir=out_dir, client=FakeClient(),
                                    resolve_account=lambda uid: FakeAccount(),
                                    autostart=False)
        self.assertEqual(reopened.get(done["id"])["status"], "completed")
        lost = reopened.get(stuck["id"])
        self.assertEqual(lost["status"], "lost")
        self.assertIn("可能已扣费", lost["error"])
        reopened.stop()

    def test_state_file_is_written(self):
        out_dir = os.path.join(_TMP.name, "statefile-media")
        store = T.MediaTaskStore(out_dir=out_dir, client=FakeClient(),
                                 resolve_account=lambda uid: FakeAccount(),
                                 autostart=False)
        store.create("m", PARAMS, "u-1", BODY, "up-1")
        self.assertTrue(os.path.isfile(os.path.join(out_dir, "tasks.json")))
        store.stop()

    def test_corrupt_state_file_is_ignored(self):
        out_dir = os.path.join(_TMP.name, "corrupt-media")
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "tasks.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        store = T.MediaTaskStore(out_dir=out_dir, client=FakeClient(),
                                 resolve_account=lambda uid: FakeAccount(),
                                 autostart=False)
        self.assertEqual(store.list(), [])
        store.stop()


class PruneTests(unittest.TestCase):
    def test_expired_outputs_are_removed(self):
        now = [1000.0]
        store = make_store(clock=lambda: now[0], ttl_days=1)
        task = store.create("m", PARAMS, "u-1", BODY, "up-1")
        store._run_task(task["id"])
        path = store.get(task["id"])["videos"][0]
        self.assertTrue(os.path.isfile(path))
        now[0] += 2 * 86400
        store._prune_outputs()
        self.assertFalse(os.path.isfile(path))
        self.assertEqual(store.get(task["id"])["videos"], [])
        store.stop()

    def test_disk_cap_removes_oldest(self):
        store = make_store(max_disk_mb=0.0001)      # 约 104 字节
        first = store.create("m", PARAMS, "u-1", BODY, "up-1")
        store._run_task(first["id"])
        second = store.create("m", PARAMS, "u-1", BODY, "up-2")
        store._run_task(second["id"])               # 第二个完成时触发清理
        remaining = [task["id"] for task in store.list() if task["videos"]]
        self.assertLessEqual(len(remaining), 1)
        store.stop()

    def test_task_table_evicts_oldest_finished(self):
        store = make_store(max_tasks=2)
        ids = []
        for index in range(4):
            task = store.create("m", PARAMS, "u-1", BODY, "up-%d" % index)
            store._run_task(task["id"])
            ids.append(task["id"])
        self.assertLessEqual(len(store.list()), 2)
        self.assertIsNone(store.get(ids[0]))
        store.stop()


class WaitTests(unittest.TestCase):
    def test_wait_returns_when_finished(self):
        store = make_store()
        task = store.create("m", PARAMS, "u-1", BODY, "up-1")
        store._run_task(task["id"])
        done = store.wait(task["id"], timeout=1, sleep=lambda _: None)
        self.assertEqual(done["status"], "completed")
        store.stop()

    def test_wait_times_out_without_raising(self):
        store = make_store()
        task = store.create("m", PARAMS, "u-1", BODY, "up-1")
        state = {"t": 0.0}
        store._clock = lambda: state["t"]
        def tick(_seconds):
            state["t"] += 10
        result = store.wait(task["id"], timeout=5, sleep=tick)
        self.assertEqual(result["status"], "queued")
        store.stop()

    def test_wait_unknown_task(self):
        store = make_store()
        self.assertIsNone(store.wait("nope", timeout=0, sleep=lambda _: None))
        store.stop()


class DefaultDirTests(unittest.TestCase):
    def test_env_override_wins(self):
        saved = os.environ.get("WB_MEDIA_DIR")
        try:
            os.environ["WB_MEDIA_DIR"] = os.path.join(_TMP.name, "custom")
            self.assertEqual(T.default_out_dir(), os.path.join(_TMP.name, "custom"))
        finally:
            if saved is None:
                os.environ.pop("WB_MEDIA_DIR", None)
            else:
                os.environ["WB_MEDIA_DIR"] = saved

    def test_default_follows_code_dir(self):
        saved = os.environ.pop("WB_MEDIA_DIR", None)
        try:
            self.assertTrue(T.default_out_dir().endswith("media"))
            self.assertTrue(os.path.isabs(T.default_out_dir()))
        finally:
            if saved is not None:
                os.environ["WB_MEDIA_DIR"] = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
