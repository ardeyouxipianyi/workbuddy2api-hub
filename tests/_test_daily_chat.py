"""Unit tests for the international realm daily chat check-in.

Two channels are covered: the desktop-identity chat completion (always sent)
and the web conversation added for issue #75/#59, whose request shape is
pinned here so it cannot drift from what the web app actually sends.
"""
import json, os, sys, unittest, time

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

    def test_cn_account_ineligible(self):
        acc = wb_accounts.Account({
            "uid": "test_cn_1",
            "realm": "cn",
            "accessToken": "dummy"
        })
        self.assertFalse(acc.can_daily_chat())
        self.assertTrue(acc.can_checkin())

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

    def stub_turn(self, result=None):
        """Replace the ACP drive with a recorder; returns the recorded calls."""
        seen = []
        real = wb_accounts.wb_webagent.run_turn

        def fake(link, token, session_id, cwd, prompt, user_agent, **kwargs):
            seen.append({"link": link, "token": token, "session_id": session_id,
                         "cwd": cwd, "prompt": prompt,
                         "status": kwargs.get("poll_status")})
            out = {"ok": True, "status": "completed", "events": 5, "chunks": 2,
                   "elapsed_ms": 1234, "error": ""}
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
        req = calls[0]
        self.assertEqual(req.method, "POST")
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

if __name__ == "__main__":
    unittest.main()

