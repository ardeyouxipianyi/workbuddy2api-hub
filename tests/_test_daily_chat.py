"""Unit tests for the international realm daily chat check-in.

Two channels are covered: the desktop-identity chat completion (always sent)
and the web conversation added for issue #75/#59, whose request shape is
pinned here so it cannot drift from what the web app actually sends.
"""
import io
import json
import tempfile
import urllib.error, os, sys, unittest, time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _isolated_dirs import isolated_data_dirs  # noqa: E402  (its own data root)
# Its own directories, removed when this process exits. This suite used to share
# tests/_acc_test and tests/_use_test with _test_sanitize_fingerprint.py and
# _test_tasks_cache.py, which made them unsafe to run at the same time and left
# directories in the repo after a run.
_TMP = isolated_data_dirs("wb-daily-chat-")

import wb_accounts, wb_settings

class DailyChatTests(unittest.TestCase):
    def test_daily_chat_eligibility(self):
        acc = wb_accounts.Account({
            "uid": "test_intl_1",
            "realm": "intl",
            "accessToken": "dummy",
            "lastDailyChat": None
        })
        # Fresh account can chat
        self.assertTrue(acc.can_daily_chat())
        self.assertFalse(acc.can_checkin()) # cn only

        # Already chatted today
        acc.last_daily_chat = time.strftime("%Y-%m-%d 10:00:00")
        self.assertFalse(acc.can_daily_chat())

        # Chatted yesterday
        acc.last_daily_chat = "2020-01-01 10:00:00"
        self.assertTrue(acc.can_daily_chat())

        # An upstream 11140 restriction suppresses later scheduler slots until
        # its persisted window expires.
        acc.restricted_until = time.time() + 3600
        self.assertFalse(acc.can_daily_chat())
        acc.restricted_until = time.time() - 1
        self.assertTrue(acc.can_daily_chat())

    def test_cn_account_ineligible(self):
        acc = wb_accounts.Account({
            "uid": "test_cn_1",
            "realm": "cn",
            "accessToken": "dummy"
        })
        self.assertFalse(acc.can_daily_chat())
        self.assertTrue(acc.can_checkin())

    def test_daily_chat_11140_persists_restriction_and_stops_retry(self):
        body = json.dumps({"code": 11140, "msg": "request illegal"}).encode("utf-8")
        with tempfile.TemporaryDirectory(prefix="daily-chat-11140-") as directory:
            acc = wb_accounts.Account({
                "uid": "test_intl_restricted",
                "realm": "intl",
                "accessToken": "dummy",
                "lastDailyChat": None,
            })
            acc.save(directory)
            err = urllib.error.HTTPError(
                "https://www.workbuddy.ai/chat", 403, "Forbidden", {}, io.BytesIO(body))
            before = time.time()
            with mock.patch.object(wb_accounts, "urlopen", side_effect=err):
                res = acc._daily_chat_upstream(web=False)

            self.assertFalse(res["ok"])
            self.assertTrue(res["restricted"])
            self.assertGreater(acc.restricted_until, before)
            self.assertFalse(acc.can_daily_chat())
            with open(acc.path, "r", encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual(saved["restrictedUntil"], acc.restricted_until)

            # A manual retry during the restriction window must not pretend the
            # day is complete and must not touch the upstream again.
            with mock.patch.object(acc, "_daily_chat_upstream",
                                   side_effect=AssertionError("must not retry")):
                skipped = acc.daily_chat(web=False, trigger="manual")
            self.assertFalse(skipped["ok"])
            self.assertTrue(skipped["skipped"])
            self.assertTrue(skipped["restricted"])

class WebChannelTests(unittest.TestCase):
    UID = "c90a4e93-edad-42fe-aa3a-133fb27cf6b6"

    def account(self, realm="intl"):
        return wb_accounts.Account({"uid": self.UID, "realm": realm,
                                    "accessToken": "dummy-token", "lastDailyChat": None})

    def stub(self, payload=None, session=None):
        calls = []
        create = payload if payload is not None else {
            "code": 0, "msg": "OK", "data": {"id": "2102411494602919936"}}
        if session is None:
            session = {
                "code": 0, "msg": "OK",
                "data": {"link": "https://box.example/acp", "token": "sandbox-token",
                         "sessionId": "2102411494602919936", "cwd": "/workspace"}}

        class Response(object):
            def __init__(self, payload):
                self.payload = payload

            def read(self, *_args):
                return json.dumps(self.payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def fake(req, **_kwargs):
            calls.append(req)
            if req.full_url.endswith("/session"):
                return Response(session)
            if req.full_url.endswith("/conversations/"):
                return Response(create)
            return Response({"code": 0, "msg": "OK",
                            "data": {"status": "completed"}})

        return calls, fake

    def stub_turn(self, result=None, sequence=None):
        """Replace the ACP drive with a recorder; returns the recorded calls.

        sequence 按轮次给出结果（最后一轮之后重复最后一个），用来测重试：例如
        [{"ok": False, "status": "working"}, {"ok": True}] 就是「先失败一次、第二次
        跑完」。result 则是覆盖每一轮的一次性结果。
        """
        seen = []
        real = wb_accounts.wb_webagent.run_turn
        pending = list(sequence or [])

        def fake(link, token, session_id, cwd, prompt, user_agent, **kwargs):
            seen.append({"link": link, "token": token, "session_id": session_id,
                         "cwd": cwd, "prompt": prompt,
                         "wait_seconds": kwargs.get("wait_seconds"),
                         "status": kwargs.get("poll_status")})
            out = {"ok": True, "status": "completed", "events": 5, "chunks": 2,
                   "elapsed_ms": 1234, "error": ""}
            if sequence:
                out.update(pending.pop(0) if len(pending) > 1 else pending[0])
            out.update(result or {})
            return out

        wb_accounts.wb_webagent.run_turn = fake
        self.addCleanup(setattr, wb_accounts.wb_webagent, "run_turn", real)
        return seen

    def test_web_conversation_request_shape(self):
        """POST /console/as/conversations/ with the web identity, no X-IDE-*."""
        acc = self.account()
        calls, fake = self.stub()
        turns = self.stub_turn()
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            res = acc.daily_chat_web()
        finally:
            wb_accounts.urlopen = old
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("conversation"), "2102411494602919936")
        # 开跑前会先 GET 一次会话列表清扫残留，所以按方法挑建会话那条。
        creates = [r for r in calls if r.method == "POST"
                   and r.full_url == "https://www.workbuddy.ai/console/as/conversations/"]
        self.assertEqual(len(creates), 1, [r.full_url for r in calls])
        req = creates[0]
        self.assertEqual(req.full_url, "https://www.workbuddy.ai/console/as/conversations/")
        headers = {k.lower(): v for k, v in req.headers.items()}
        self.assertEqual(headers.get("authorization"), "Bearer dummy-token")
        self.assertEqual(headers.get("x-user-id"), self.UID)
        self.assertEqual(headers.get("origin"), "https://www.workbuddy.ai")
        self.assertFalse([k for k in headers if k.startswith("x-ide")],
                         "the web channel must not carry desktop identity headers")
        body = json.loads(req.data.decode("utf-8"))
        self.assertTrue(body.get("prompt"))
        self.assertEqual(body.get("model"), wb_accounts.DAILY_CHAT_MODEL)
        self.assertEqual(body.get("conversationOrigin"), "workbuddy-app")
        # 建会话之后必须接沙箱、把这一轮跑起来，否则会话永远停在 CREATING。
        self.assertTrue(any(r.full_url.endswith("/session") for r in calls),
                        [r.full_url for r in calls])
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["link"], "https://box.example/acp")
        self.assertEqual(turns[0]["token"], "sandbox-token")
        self.assertEqual(turns[0]["session_id"], "2102411494602919936")
        self.assertEqual(turns[0]["cwd"], "/workspace")
        self.assertTrue(turns[0]["prompt"])
        self.assertEqual(res.get("status"), "completed")
        self.assertEqual(res.get("chunks"), 2)

    def test_web_conversation_reports_a_business_error(self):
        acc = self.account()
        _calls, fake = self.stub({"code": 12302, "msg": "activity is offline"})
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            res = acc.daily_chat_web()
        finally:
            wb_accounts.urlopen = old
        self.assertFalse(res.get("ok"))
        self.assertIn("activity is offline", res.get("error", ""))

    def test_web_conversation_is_intl_only(self):
        res = self.account(realm="cn").daily_chat_web()
        self.assertFalse(res.get("ok"))
        self.assertIn("international", res.get("error", ""))

    def test_daily_chat_runs_the_web_step_when_asked(self):
        acc = self.account()
        calls, fake = self.stub()
        self.stub_turn()
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            res = acc.daily_chat(web=True)
        finally:
            wb_accounts.urlopen = old
        self.assertTrue(res.get("ok"), res)
        self.assertTrue(res.get("web", {}).get("ok"), res)
        self.assertIn("网页通道", res.get("msg", ""))
        self.assertTrue(any(r.full_url.endswith("/console/as/conversations/") for r in calls),
                        [r.full_url for r in calls])

    def test_a_turn_that_never_finishes_is_reported(self):
        acc = self.account()
        calls, fake = self.stub()
        self.stub_turn({"ok": False, "status": "working", "chunks": 0,
                        "error": "会话在 120s 内没有跑完（状态=working）"})
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            res = acc.daily_chat_web()
        finally:
            wb_accounts.urlopen = old
        self.assertFalse(res.get("ok"), res)
        self.assertEqual(res.get("conversation"), "2102411494602919936")
        self.assertIn("没有跑完", res.get("error", ""))

    def test_a_missing_sandbox_is_reported(self):
        acc = self.account()
        calls, fake = self.stub(session={"code": 0, "msg": "OK", "data": {}})
        self.stub_turn()
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            res = acc.daily_chat_web()
        finally:
            wb_accounts.urlopen = old
        self.assertFalse(res.get("ok"), res)
        self.assertIn("沙箱", res.get("error", ""))

    def test_daily_chat_skips_the_web_step_when_off(self):
        acc = self.account()
        calls, fake = self.stub()
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            res = acc.daily_chat(web=False)
        finally:
            wb_accounts.urlopen = old
        self.assertTrue(res.get("ok"), res)
        self.assertNotIn("web", res)
        self.assertFalse(any("/console/as/" in r.full_url for r in calls),
                         [r.full_url for r in calls])

    def test_a_failed_web_step_is_reported_and_leaves_the_day_open(self):
        """网页腿失败要如实报失败，也不能写 lastDailyChat：否则 30 积分静默丢掉。"""
        acc = self.account()
        _calls, fake = self.stub()
        self.stub_turn({"ok": False, "status": "working", "chunks": 0,
                        "error": "会话在 120s 内没有跑完（状态=working）"})
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            res = acc.daily_chat(web=True)
        finally:
            wb_accounts.urlopen = old
        self.assertFalse(res.get("ok"), res)         # 只有网页腿算数，它失败就是失败
        self.assertFalse(res.get("web", {}).get("ok"), res)
        self.assertIn("网页通道失败", res.get("msg", ""))
        self.assertIn("网页通道失败", res.get("error", ""))
        self.assertIsNone(acc.last_daily_chat)       # 今天不算做过
        self.assertTrue(acc.can_daily_chat())        # 下一次巡检还会重试

    def test_a_finished_web_step_marks_the_day_done(self):
        acc = self.account()
        _calls, fake = self.stub()
        self.stub_turn()
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            res = acc.daily_chat(web=True)
        finally:
            wb_accounts.urlopen = old
        self.assertTrue(res.get("web", {}).get("ok"), res)
        self.assertTrue(acc.last_daily_chat)
        self.assertFalse(acc.can_daily_chat())

    def test_the_web_step_being_off_still_marks_the_day_done(self):
        acc = self.account()
        _calls, fake = self.stub()
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            res = acc.daily_chat(web=False)
        finally:
            wb_accounts.urlopen = old
        self.assertTrue(res.get("ok"), res)
        self.assertTrue(acc.last_daily_chat)
        self.assertFalse(acc.can_daily_chat())

    # ---- 失败重试 ----------------------------------------------------------
    # 一轮「会话还没跑完」往往只是慢，不是坏了。重试默认开，一共最多尝试 N 次
    # （含第一轮），每轮的等待在上一轮基础上增加一个首轮时长：默认 3 次 = 120 / 240 / 360。

    def account_in(self, name, models=None, budget=None, timeout=None):
        """一个带自己 settings.json 的账号，方便按设置页的口径测预算与候选序列。"""
        directory = os.path.join(_TMP.name, name)
        os.makedirs(directory, exist_ok=True)
        if models is not None:
            wb_settings.set_daily_chat_models(directory, models)
        if budget is not None:
            wb_settings.set_daily_chat_budget(directory, budget)
        if timeout is not None:
            wb_settings.set_daily_chat_web_timeout(directory, timeout)
        acc = wb_accounts.Account({"uid": self.UID, "realm": "intl",
                                   "accessToken": "dummy-token", "lastDailyChat": None})
        acc.path = os.path.join(directory, "acc-intl.json")
        return acc

    def run_web(self, acc):
        """跑一次网页腿，返回 (结果, 每轮的等待秒数)。"""
        _calls, fake = self.stub()
        turns = self.stub_turn({"ok": False, "status": "working", "chunks": 0,
                                "error": "会话在 120s 内没有跑完（状态=working）"})
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            res = acc.daily_chat_web()
        finally:
            wb_accounts.urlopen = old
        return res, [t["wait_seconds"] for t in turns]

    def test_a_slow_turn_is_retried_with_the_same_wait(self):
        """一轮没跑完只是慢：再等一轮。等待不再逐轮翻倍——总时长由预算管，
        每轮都拿满「单轮上限」，所以两轮都是 45 秒。"""
        acc = self.account_in("web-retry-same", timeout=45)
        _calls, fake = self.stub()
        turns = self.stub_turn(sequence=[{"ok": False, "status": "working", "chunks": 0,
                                          "error": "会话在 45s 内没有跑完（状态=working）"},
                                         {"ok": True, "status": "completed"}])
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            res = acc.daily_chat_web()
        finally:
            wb_accounts.urlopen = old
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("attempts"), 2)
        self.assertEqual([t["wait_seconds"] for t in turns], [45, 45])

    def test_the_rounds_follow_the_candidate_list(self):
        """候选 3 个 = 3 轮，按面板上的顺序换模型（旧实现会按倍率重排）。"""
        acc = self.account_in("web-candidates",
                              models=["glm-5.3-flash", "deepseek-v4.1-flash", "hy3"])
        res, waits = self.run_web(acc)
        self.assertFalse(res.get("ok"), res)
        self.assertEqual(res.get("attempts"), 3)
        self.assertEqual(res.get("models_tried"),
                         ["glm-5.3-flash", "deepseek-v4.1-flash", "hy3"])
        self.assertEqual(waits, [120, 120, 120])

    def test_at_least_two_rounds_without_a_candidate_list(self):
        """自动模式（没有候选列表）也至少跑两轮：第二轮留给「会话只是慢」。"""
        acc = self.account_in("web-auto-rounds")
        res, waits = self.run_web(acc)
        self.assertFalse(res.get("ok"), res)
        self.assertEqual(res.get("attempts"), wb_accounts.WEB_MIN_ROUNDS)
        self.assertEqual(waits, [120, 120])

    def test_a_tiny_budget_still_reports_a_verdict(self):
        """预算小到连一轮都放不下：不发请求，直接给「预算不足」的结论。

        这一条钉的是「必有结论」：旧实现里卡住的账号可以几小时不写一行，
        面板上就是一片空白。
        """
        acc = self.account_in("web-budget-tiny", budget=5, timeout=120)
        calls, fake = self.stub()
        turns = self.stub_turn()
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            res = acc.daily_chat_web()
        finally:
            wb_accounts.urlopen = old
        self.assertFalse(res.get("ok"), res)
        self.assertEqual(res.get("attempts"), 0)
        self.assertEqual(turns, [])
        self.assertEqual([r for r in calls if r.full_url.endswith("/conversations/")], [])
        self.assertEqual(res.get("phase"), "budget")
        self.assertTrue(res.get("budget_exhausted"))
        self.assertIn("预算", res.get("error") or "")

    def test_the_round_wait_is_clamped_by_the_budget(self):
        """单轮等待 = min(单轮上限, 剩余预算)，所以预算比上限小时每轮都按预算等。"""
        acc = self.account_in("web-budget-clamp", budget=60, timeout=120)
        res, waits = self.run_web(acc)
        self.assertFalse(res.get("ok"), res)
        self.assertEqual(waits, [60, 60])

    def test_a_business_rejection_is_not_retried(self):
        """建会话被业务拒绝（非 0 code）时重试没有意义，只跑一轮。"""
        acc = self.account_in("web-retry-reject")
        calls, fake = self.stub({"code": 12302, "msg": "activity is offline"})
        turns = self.stub_turn()
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            res = acc.daily_chat_web()
        finally:
            wb_accounts.urlopen = old
        self.assertFalse(res.get("ok"), res)
        self.assertEqual(res.get("attempts"), 1)
        self.assertEqual(turns, [])
        self.assertEqual(len([r for r in calls
                              if r.full_url.endswith("/conversations/")]), 1)

    def test_a_transient_create_failure_is_retried(self):
        """建会话时 503 是瞬时失败，值得再试一次（与业务拒绝区分开）。"""
        acc = self.account_in("web-retry-503")
        calls, fake = self.stub()
        turns = self.stub_turn()
        state = {"left": 1}
        real_fake = fake

        def flaky(req, **kwargs):
            if req.full_url.endswith("/conversations/") and state["left"] > 0:
                state["left"] -= 1
                raise urllib.error.HTTPError(req.full_url, 503, "Unavailable", {}, io.BytesIO(b""))
            return real_fake(req, **kwargs)

        old = wb_accounts.urlopen
        wb_accounts.urlopen = flaky
        try:
            res = acc.daily_chat_web()
        finally:
            wb_accounts.urlopen = old
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("attempts"), 2)
        self.assertEqual([t["wait_seconds"] for t in turns], [120])

    def test_the_env_timeout_survives_the_new_setting(self):
        """没在设置页改过时，WB_WEB_TURN_TIMEOUT 仍然说了算，不被 120 顶掉。"""
        acc = self.account_in("web-timeout-env")
        real = wb_accounts.WEB_TURN_TIMEOUT
        wb_accounts.WEB_TURN_TIMEOUT = 77
        try:
            self.assertEqual(acc.daily_chat_web_policy()[0], 77)
        finally:
            wb_accounts.WEB_TURN_TIMEOUT = real
        # 设置页改过就以设置页的为准。
        wb_settings.set_daily_chat_web_timeout(os.path.dirname(acc.path), 45)
        self.assertEqual(acc.daily_chat_web_policy()[0], 45)

    def test_the_budget_settings_default_and_round_trip(self):
        directory = os.path.join(_TMP.name, "web-budget-settings")
        # 默认：单账号预算 600 秒，候选序列为空（自动），至少两轮。
        self.assertEqual(wb_settings.daily_chat_budget(directory),
                         wb_settings.DAILY_CHAT_BUDGET_DEFAULT)
        self.assertEqual(wb_settings.daily_chat_models(directory), [])
        acc = self.account_in("web-budget-default")
        self.assertEqual(acc.daily_chat_budget(), wb_accounts.WEB_BUDGET_DEFAULT)
        self.assertEqual(acc.daily_chat_web_policy()[1], wb_accounts.WEB_MIN_ROUNDS)
        # 写读往返。
        self.assertEqual(wb_settings.set_daily_chat_budget(directory, 900), 900)
        self.assertEqual(wb_settings.daily_chat_budget(directory), 900)
        self.assertEqual(wb_settings.set_daily_chat_budget(directory, 0), 0)
        self.assertEqual(wb_settings.daily_chat_budget(directory), 0)
        self.assertEqual(wb_settings.set_daily_chat_models(
            directory, ["hy3", "hy3", " glm-5.3-flash "]), ["hy3", "glm-5.3-flash"])
        self.assertEqual(wb_settings.daily_chat_models(directory),
                         ["hy3", "glm-5.3-flash"])
        self.assertEqual(wb_settings.set_daily_chat_models(directory, []), [])
        self.assertEqual(wb_settings.daily_chat_models(directory), [])
        # 越界/非法值一律回落到默认，不让手改的 settings.json 把巡检拖住。
        for bad in (-1, wb_settings.DAILY_CHAT_BUDGET_MAX + 1, "600", True, None):
            with open(wb_settings.settings_path(directory), "w", encoding="utf-8") as fh:
                json.dump({"daily_chat_budget": bad}, fh)
            self.assertEqual(wb_settings.daily_chat_budget(directory),
                             wb_settings.DAILY_CHAT_BUDGET_DEFAULT, bad)
        for bad in (-1, wb_settings.DAILY_CHAT_BUDGET_MAX + 1, "600", True):
            with self.assertRaises(ValueError):
                wb_settings.set_daily_chat_budget(directory, bad)

    def test_legacy_model_and_fallback_settings_migrate_to_the_candidate_list(self):
        """升级不能把老部署选过的模型与回退组合静默丢掉。"""
        directory = os.path.join(_TMP.name, "web-legacy-candidates")
        os.makedirs(directory, exist_ok=True)
        with open(wb_settings.settings_path(directory), "w", encoding="utf-8") as fh:
            json.dump({
                "daily_chat_model": "hy3",
                "daily_chat_web_fallback": True,
                "daily_chat_web_fallback_models": ["glm-5.3-flash", "hy3"],
            }, fh)
        self.assertEqual(wb_settings.daily_chat_models(directory),
                         ["hy3", "glm-5.3-flash"])

        with open(wb_settings.settings_path(directory), "w", encoding="utf-8") as fh:
            json.dump({
                "daily_chat_model": "hy3",
                "daily_chat_web_fallback": False,
                "daily_chat_web_fallback_models": ["glm-5.3-flash"],
            }, fh)
        self.assertEqual(wb_settings.daily_chat_models(directory), ["hy3"])

    def test_backoff_is_clamped_to_the_remaining_budget(self):
        """建会话失败后的退避不能睡过单账号总预算。"""
        acc = self.account_in("web-backoff-budget", budget=600)
        deadline = time.time() + 2.0
        with mock.patch.object(wb_accounts.time, "sleep") as sleep:
            waited = acc._web_retry_pause(
                {"phase": "create"}, 5, deadline=deadline)
        self.assertGreater(waited, 0)
        self.assertLessEqual(waited, 2.0)
        sleep.assert_called_once()
        self.assertLessEqual(sleep.call_args[0][0], 2.0)

    def test_the_web_toggle_defaults_on_and_round_trips(self):
        directory = os.path.join(_TMP.name, "web-toggle")
        self.assertTrue(wb_settings.daily_chat_web(directory))
        self.assertFalse(wb_settings.set_daily_chat_web(directory, False))
        self.assertFalse(wb_settings.daily_chat_web(directory))
        self.assertTrue(wb_settings.set_daily_chat_web(directory, True))
        self.assertTrue(wb_settings.daily_chat_web(directory))
        with open(wb_settings.settings_path(directory), "w", encoding="utf-8") as fh:
            json.dump({"daily_chat_web": "false"}, fh)
        self.assertFalse(wb_settings.daily_chat_web(directory))





class CnCheckinTests(unittest.TestCase):
    def test_already_checked_in_is_treated_as_success(self):
        acc = wb_accounts.Account({"uid": "u-test-cn", "realm": "cn", "accessToken": "token"})
        def fake_http_json(url, data, method, headers, timeout, proxy=""):
            err_body = json.dumps({"code": 10001, "msg": "今天已签到，请明天再来"}).encode("utf-8")
            fp = io.BytesIO(err_body)
            raise urllib.error.HTTPError(url, 400, "Bad Request", {}, fp)

        real_http_json = wb_accounts.http_json
        wb_accounts.http_json = fake_http_json
        try:
            res = acc._checkin_upstream()
            self.assertTrue(res.get("ok"), res)
            self.assertEqual(res.get("code"), 10001)
            self.assertIn("今天已签到", res.get("msg"))
            self.assertTrue(acc.last_checkin)
        finally:
            wb_accounts.http_json = real_http_json



    def test_daily_chat_model_falls_back_when_preferred_is_cooling(self):
        acc = wb_accounts.Account({"uid": "u-fallback", "access_token": "t-fb", "realm": "intl"})
        acc.model_cooldowns["deepseek-v4.1-flash"] = time.time() + 600
        picked = acc.daily_chat_model()
        self.assertNotEqual(picked, "deepseek-v4.1-flash")
        self.assertIn(picked, ["hy3", "hy4-preview-f", "hy4-preview", "glm-5.3-flash"])


if __name__ == "__main__":
    unittest.main()
