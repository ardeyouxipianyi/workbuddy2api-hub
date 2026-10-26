"""Web daily-activity conversations must release their upstream slot.

Upstream keeps at most 10 *active* (status=working) conversations per account and
refuses to create another one with HTTP 429 / code 10105. Every daily activity
opens a new conversation, so a version that never closed them leaked one per run
(three per run once retries kicked in) and left the account permanently unable to
check in. These cases pin the cleanup contract:

  - a finished turn stops its conversation (the record stays in the web history);
  - a failed turn stops and deletes it (nothing worth keeping);
  - cleanup happens even when the turn dies before the sandbox is reachable;
  - hitting 10105 prunes the oldest leaked conversations and retries once;
  - when nothing can be pruned the error is final, not retried three times;
  - pruning never touches a conversation that is not one of ours.
"""
import io
import json
import os
import sys
import time
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _isolated_dirs import isolated_data_dirs  # noqa: E402  (its own data root)

_TMP = isolated_data_dirs("wb-web-cleanup-")

import wb_accounts, wb_settings  # noqa: E402

UID = "c90a4e93-edad-42fe-aa3a-133fb27cf6b6"
CREATE_URL = "https://www.workbuddy.ai/console/as/conversations/"


class Response(object):
    def __init__(self, payload):
        self.payload = payload

    def read(self, *_args):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class WebStub(object):
    """A fake console API that records every request.

    `creates` is a queue of responses for POST /conversations/: a dict is returned
    as the body, an exception instance is raised. The queue's last item repeats.
    """

    def __init__(self, creates=None, conversations=None, session=None,
                 session_error=None, action_error=None):
        self.calls = []
        self.creates = list(creates or [{"code": 0, "msg": "OK",
                                         "data": {"id": "conv-1"}}])
        self.conversations = conversations or []
        self.session = session if session is not None else {
            "code": 0, "msg": "OK",
            "data": {"link": "https://box.example/acp", "token": "sandbox-token",
                     "sessionId": "conv-1", "cwd": "/workspace"}}
        self.session_error = session_error
        self.action_error = action_error

    def __call__(self, req, **_kwargs):
        self.calls.append(req)
        url, method = req.full_url, req.method
        if method == "POST" and url.endswith(("/stop", "/delete")):
            if self.action_error:
                raise self.action_error
            return Response({"code": 0, "msg": "OK", "data": {}})
        if method == "GET" and url.endswith("/session"):
            if self.session_error:
                raise self.session_error
            return Response(self.session)
        if method == "GET" and "/conversations/?" in url:
            return Response({"code": 0, "msg": "OK",
                             "data": {"conversations": self.conversations}})
        if method == "GET":
            return Response({"code": 0, "msg": "OK", "data": {"status": "completed"}})
        if url == CREATE_URL:
            item = self.creates.pop(0) if len(self.creates) > 1 else self.creates[0]
            if isinstance(item, Exception):
                raise item
            return Response(item)
        return Response({"code": 0, "msg": "OK", "data": {}})

    # -- assertions ------------------------------------------------------
    def actions(self, action):
        return [r for r in self.calls
                if r.full_url.endswith("/" + action) and r.method == "POST"]

    def created(self):
        return [r for r in self.calls
                if r.full_url == CREATE_URL and r.method == "POST"]


def active_limit_error(conversation_id="conv-1"):
    body = json.dumps({"code": 10105,
                       "msg": "CreateConversation: 10105:you have reached the "
                              "maximum number of active conversations(10), "
                              "please close some first"}).encode("utf-8")
    return urllib.error.HTTPError(CREATE_URL, 429, "Too Many Requests", {},
                                  io.BytesIO(body))


class CleanupTests(unittest.TestCase):
    def account(self, name=None, retry=None):
        acc = wb_accounts.Account({"uid": UID, "realm": "intl",
                                   "accessToken": "dummy-token",
                                   "lastDailyChat": None})
        if name is not None:
            directory = os.path.join(_TMP.name, name)
            os.makedirs(directory, exist_ok=True)
            acc.path = os.path.join(directory, "acc-intl.json")
        return acc

    def run_web(self, stub, turn=None, acc=None):
        acc = acc or self.account()
        real_urlopen = wb_accounts.urlopen
        real_turn = wb_accounts.wb_webagent.run_turn

        def fake_turn(*_args, **_kwargs):
            out = {"ok": True, "status": "completed", "events": 5, "chunks": 2,
                   "elapsed_ms": 1234, "error": ""}
            out.update(turn or {})
            return out

        wb_accounts.urlopen = stub
        wb_accounts.wb_webagent.run_turn = fake_turn
        try:
            return acc.daily_chat_web()
        finally:
            wb_accounts.urlopen = real_urlopen
            wb_accounts.wb_webagent.run_turn = real_turn

    # -- 正常一轮：收尾但不删 ------------------------------------------------
    def test_a_finished_turn_stops_the_conversation(self):
        stub = WebStub()
        res = self.run_web(stub)
        self.assertTrue(res.get("ok"), res)
        self.assertEqual([r.full_url for r in stub.actions("stop")],
                         [CREATE_URL + "conv-1/stop"])
        self.assertEqual(stub.actions("delete"), [],
                         "成功的一轮要留在网页端历史里")

    def test_the_stop_request_is_a_json_post_with_the_web_identity(self):
        stub = WebStub()
        self.run_web(stub)
        req = stub.actions("stop")[0]
        self.assertEqual(req.method, "POST")
        headers = {k.lower(): v for k, v in req.headers.items()}
        self.assertEqual(headers.get("authorization"), "Bearer dummy-token")
        self.assertEqual(headers.get("x-user-id"), UID)
        self.assertEqual(req.data, b"{}")

    # -- 失败一轮：停 + 删 ---------------------------------------------------
    def test_a_failed_turn_stops_and_deletes(self):
        stub = WebStub()
        acc = self.account("fail-once", retry=False)
        res = self.run_web(stub, turn={"ok": False, "status": "working",
                                       "chunks": 0,
                                       "error": "会话在 120s 内没有跑完（状态=working）"},
                           acc=acc)
        self.assertFalse(res.get("ok"))
        # 轮数由候选序列推出（至少两轮：第二轮留给「会话只是慢」），所以不再断言
        # 「恰好一轮」；改断言不变量——建出来的每一条会话都必须被收尾。
        # 失败的一轮是「先 stop 再 delete」，所以两者都该等于建出来的会话数。
        self.assertEqual(len(stub.actions("stop")), len(stub.created()),
                         "每建一条会话都必须 stop")
        self.assertEqual(len(stub.actions("delete")), len(stub.created()),
                         "失败的一轮 stop 之后还要 delete")

    def test_cleanup_runs_when_the_sandbox_lookup_fails(self):
        stub = WebStub(session_error=OSError("connection reset"))
        acc = self.account("session-error", retry=False)
        res = self.run_web(stub, acc=acc)
        self.assertFalse(res.get("ok"))
        self.assertIn("session 查询失败", res.get("error", ""))
        self.assertEqual(len(stub.actions("delete")), len(stub.created()),
                         "会话已经建出来了，失败也必须收尾")

    def test_cleanup_runs_when_the_sandbox_has_no_link(self):
        stub = WebStub(session={"code": 0, "msg": "OK", "data": {}})
        acc = self.account("no-link", retry=False)
        res = self.run_web(stub, acc=acc)
        self.assertFalse(res.get("ok"))
        self.assertIn("沙箱", res.get("error", ""))
        self.assertEqual(len(stub.actions("delete")), len(stub.created()),
                         "建完就崩的分支也要收尾")

    # -- 10105：先清理再重试 -------------------------------------------------
    def leaked(self, count=10, name="Hi"):
        return [{"id": "old-%02d" % i, "name": name, "status": "working",
                 "createdAt": "2026-10-%02dT09:00:00+08:00" % (i + 1)}
                for i in range(count)]

    def test_the_active_limit_prunes_old_conversations_and_retries(self):
        stub = WebStub(creates=[active_limit_error(),
                                {"code": 0, "msg": "OK", "data": {"id": "conv-new"}}],
                       conversations=self.leaked(10))
        res = self.run_web(stub)
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("conversation"), "conv-new")
        # 10 条活跃、保留 8 条 → 停掉最旧的 2 条。
        self.assertEqual([r.full_url for r in stub.actions("stop")[:2]],
                         [CREATE_URL + "old-00/stop", CREATE_URL + "old-01/stop"])
        self.assertEqual(len(stub.created()), 2, "清出名额后要再建一次")
        self.assertEqual(len(stub.actions("delete")), 0,
                         "成功的一轮只 stop，不删")

    def test_pruning_only_touches_daily_activity_conversations(self):
        conversations = self.leaked(10)
        conversations.insert(0, {"id": "user-1", "name": "季度报告",
                                 "status": "working",
                                 "createdAt": "2026-09-01T09:00:00+08:00"})
        stub = WebStub(creates=[active_limit_error(),
                                {"code": 0, "msg": "OK", "data": {"id": "conv-new"}}],
                       conversations=conversations)
        self.run_web(stub)
        stopped = [r.full_url for r in stub.actions("stop")]
        self.assertNotIn(CREATE_URL + "user-1/stop", stopped)
        self.assertIn(CREATE_URL + "old-00/stop", stopped)

    def test_pruning_does_nothing_below_the_limit(self):
        """prune 只在撞到上限时才动手；低于上限时由按年龄的清扫兜底。"""
        stub = WebStub(conversations=self.leaked(3))
        acc = self.account("prune-below")
        stopped = self._with_stub(stub, acc.web_conversation_prune)
        self.assertEqual(stopped, 0)
        self.assertEqual(stub.actions("stop"), [])

    def test_the_limit_error_is_final_when_nothing_can_be_freed(self):
        stub = WebStub(creates=[active_limit_error()],
                       conversations=[{"id": "user-1", "name": "季度报告",
                                       "status": "working",
                                       "createdAt": "2026-09-01T09:00:00+08:00"}]
                       + self.leaked(9, name="别人的会话"))
        res = self.run_web(stub)
        self.assertFalse(res.get("ok"))
        self.assertFalse(res.get("retryable"), "腾不出名额时不该再拿重试次数撞墙")
        self.assertIn("上限", res.get("error", ""))
        self.assertIn("workbuddy.ai", res.get("error", ""))

    def test_the_limit_error_is_not_retried_by_the_outer_loop(self):
        directory = os.path.join(_TMP.name, "limit-retry")
        os.makedirs(directory, exist_ok=True)
        acc = self.account()
        acc.path = os.path.join(directory, "acc-intl.json")
        stub = WebStub(creates=[active_limit_error()],
                       conversations=[{"id": "user-1", "name": "季度报告",
                                       "status": "working",
                                       "createdAt": "2026-09-01T09:00:00+08:00"}])
        res = self.run_web(stub, acc=acc)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("attempts"), 1)
        self.assertEqual(len(stub.created()), 1,
                         "一次 10105 只该建一次会话，不该再漏两条")

    # -- 错误可读性 ----------------------------------------------------------
    def test_the_limit_detector_matches_code_and_message(self):
        self.assertTrue(wb_accounts._active_conversation_limit(429, '{"code":10105}'))
        self.assertTrue(wb_accounts._active_conversation_limit(
            429, "you have reached the maximum number of active conversations(10)"))
        self.assertFalse(wb_accounts._active_conversation_limit(429, '{"code":14003}'))
        self.assertFalse(wb_accounts._active_conversation_limit(500, '{"code":10105}'))

    def test_a_successful_turn_is_not_reported_as_failed_by_cleanup(self):
        """收尾接口挂了也不能把已经跑完的一轮改判成失败。"""
        stub = WebStub(action_error=OSError("cleanup endpoint down"))
        res = self.run_web(stub)
        self.assertTrue(res.get("ok"), res)

    # -- 卡死的一轮：finally 跑不到，由别的线程代收尾 ------------------------
    def test_reclaim_releases_a_conversation_left_by_a_stuck_round(self):
        """一轮卡在 socket 上时 finally 永远跑不到，登记表让别的线程替它收尾。"""
        stub = WebStub()
        acc = self.account()
        wb_accounts.web_live_register(acc.uid, "conv-stuck", "m1")
        try:
            reclaimed = self._with_stub(stub, lambda: acc.web_conversation_reclaim(0))
            self.assertEqual(reclaimed, 1)
            self.assertEqual([r.full_url for r in stub.actions("stop")],
                             [CREATE_URL + "conv-stuck/stop"])
            self.assertEqual([r.full_url for r in stub.actions("delete")],
                             [CREATE_URL + "conv-stuck/delete"])
            self.assertEqual(wb_accounts.web_live_snapshot(acc.uid), [],
                             "收过尾的会话要从登记表里摘掉，别被反复重收")
        finally:
            wb_accounts.web_live_unregister(acc.uid, "conv-stuck")

    def test_failed_reclaim_keeps_the_entry_for_the_next_attempt(self):
        """收尾请求失败不能先摘登记：那会把唯一的回收线索丢掉。"""
        stub = WebStub(action_error=OSError("cleanup endpoint down"))
        acc = self.account()
        wb_accounts.web_live_register(acc.uid, "conv-retry", "m1")
        try:
            reclaimed = self._with_stub(stub, lambda: acc.web_conversation_reclaim(0))
            self.assertEqual(reclaimed, 0)
            self.assertEqual([i["conversation"] for i in
                              wb_accounts.web_live_snapshot(acc.uid)],
                             ["conv-retry"])

            stub.action_error = None
            reclaimed = self._with_stub(stub, lambda: acc.web_conversation_reclaim(0))
            self.assertEqual(reclaimed, 1)
            self.assertEqual(wb_accounts.web_live_snapshot(acc.uid), [])
        finally:
            wb_accounts.web_live_unregister(acc.uid, "conv-retry")

    def test_reclaim_ignores_a_round_that_is_still_within_its_budget(self):
        stub = WebStub()
        acc = self.account()
        wb_accounts.web_live_register(acc.uid, "conv-young", "m1")
        try:
            reclaimed = self._with_stub(stub, lambda: acc.web_conversation_reclaim(600))
            self.assertEqual(reclaimed, 0, "还没跑过头的一轮不能被抢收")
            self.assertEqual(stub.actions("stop"), [])
        finally:
            wb_accounts.web_live_unregister(acc.uid, "conv-young")

    def test_reclaim_only_touches_the_given_account(self):
        stub = WebStub()
        acc = self.account()
        wb_accounts.web_live_register("other-uid", "conv-other", "m1")
        try:
            reclaimed = self._with_stub(stub, lambda: acc.web_conversation_reclaim(0))
            self.assertEqual(reclaimed, 0)
            self.assertEqual(stub.calls, [])
        finally:
            wb_accounts.web_live_unregister("other-uid", "conv-other")

    def test_sweep_orphans_stops_only_old_daily_activity_conversations(self):
        """上一个进程留下的活跃会话：只停「我们建的」且够老的，别人的不碰。"""
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        old = (now - datetime.timedelta(hours=2)).isoformat()
        fresh = (now - datetime.timedelta(seconds=30)).isoformat()
        prompt = wb_accounts.DAILY_CHAT_WEB_PROMPT
        stub = WebStub(conversations=[
            {"id": "old-mine", "name": prompt, "status": "working", "createdAt": old},
            {"id": "fresh-mine", "name": prompt, "status": "working", "createdAt": fresh},
            {"id": "old-user", "name": "我的季度报告", "status": "working", "createdAt": old},
            {"id": "old-done", "name": prompt, "status": "completed", "createdAt": old},
            {"id": "no-date", "name": prompt, "status": "working"},
        ])
        acc = self.account("sweep")
        stopped = self._with_stub(stub, acc.web_conversation_sweep_orphans)
        self.assertEqual(stopped, 1)
        self.assertEqual([r.full_url for r in stub.actions("stop")],
                         [CREATE_URL + "old-mine/stop"],
                         "只停我们建的、够老的、还在跑的会话")

    def test_the_round_starts_by_reclaiming_what_the_last_one_left(self):
        """开跑前先收上一轮卡住留下的会话，别让它把活跃名额带走。"""
        stub = WebStub()
        acc = self.account("pre-reclaim")
        self.register_stale(acc.uid, "conv-left")
        try:
            res = self.run_web(stub, acc=acc)
        finally:
            wb_accounts.web_live_unregister(acc.uid, "conv-left")
        self.assertTrue(res.get("ok"), res)
        self.assertIn(CREATE_URL + "conv-left/stop",
                      [r.full_url for r in stub.actions("stop")])

    def register_stale(self, uid, conversation, age=3600):
        """登记一条「已经跑过头」的会话（模拟卡死那一路留下的登记）。"""
        wb_accounts.web_live_register(uid, conversation, "m1")
        with wb_accounts._WEB_LIVE_LOCK:
            wb_accounts._WEB_LIVE[(str(uid), str(conversation))]["started"] = time.time() - age

    def test_the_registry_cannot_grow_without_bound(self):
        """没人调 reclaim 的部署里，登记表也不能无限长。"""
        acc = self.account()
        try:
            self.register_stale(acc.uid, "conv-ancient",
                                age=wb_accounts.WEB_LIVE_REGISTRY_TTL + 60)
            wb_accounts.web_live_register(acc.uid, "conv-new", "m1")
            kept = [i["conversation"] for i in wb_accounts.web_live_snapshot(acc.uid)]
            self.assertEqual(kept, ["conv-new"],
                             "超过 TTL 的旧条目在注册新条目时被丢掉")

            for index in range(wb_accounts.WEB_LIVE_REGISTRY_MAX + 10):
                wb_accounts.web_live_register("bulk-uid", "conv-%03d" % index, "m1")
            self.assertLessEqual(len(wb_accounts.web_live_snapshot()),
                                 wb_accounts.WEB_LIVE_REGISTRY_MAX,
                                 "条数硬上限：再多也只留这么多")
        finally:
            with wb_accounts._WEB_LIVE_LOCK:
                wb_accounts._WEB_LIVE.clear()

    def _with_stub(self, stub, fn):
        real_urlopen = wb_accounts.urlopen
        wb_accounts.urlopen = stub
        try:
            return fn()
        finally:
            wb_accounts.urlopen = real_urlopen


if __name__ == "__main__":
    unittest.main()
