"""国际版每日活跃的证书失败降级（issue #645）。

上游给网页通道分配的 ACP 沙箱域名（*.workbuddy.cloudagent-app.net）解析到腾讯云
EdgeOne，边缘节点下发的是 *.cdn.myqcloud.com 通配证书，Python ssl 必然是 Hostname
mismatch。这一轮重试永远不可能成功，而成长连登只需要一条 POST /v2/report 就能点亮。

这里钉四件事：
1. 国际版的活跃上报与连登查询都走它自己的 base（www.workbuddy.ai），不是国内 base；
2. 网页腿证书校验失败且开关打开时，改走活跃上报并把当天算完成；
3. 开关默认关，关着时行为与以前完全一致（仍如实报失败、不写 lastDailyChat）；
4. 非证书类失败一律不降级，避免把「真的没跑完」也算成完成。

Run with: python tests/run_all.py daily_chat_activity_fallback
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _isolated_dirs import isolated_data_dirs  # noqa: E402  (its own data root)
_TMP = isolated_data_dirs("wb-daily-fallback-")

import wb_accounts, wb_settings, wb_tasks  # noqa: E402

UID = "5f3d1c02-0a51-4a0e-9a5f-2f3d6cd0b7a1"
# 上游真实下发过的证书错误文本（见 issue #645 的报错原文）。
TLS_ERROR = ("SSLCertVerificationError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate "
             "verify failed: Hostname mismatch, certificate is not valid for "
             "'65225-2113853771288948736.workbuddy.cloudagent-app.net'. (_ssl.c:1016)")
NON_TLS_ERROR = "会话在 120s 内没有跑完（状态=working）"


def make_account(directory=None, realm="intl", uid=UID, **extra):
    data = {"uid": uid, "realm": realm, "accessToken": "dummy-token",
            "lastDailyChat": None}
    data.update(extra)
    acc = wb_accounts.Account(data)
    if directory:
        acc.path = os.path.join(directory, "acc-%s.json" % realm)
    return acc


def make_dir(name):
    directory = os.path.join(_TMP.name, name)
    os.makedirs(directory, exist_ok=True)
    return directory


class Response(object):
    """最小可用的 urlopen 返回值：网页通道的每一次 GET/POST 都用它。"""

    def __init__(self, payload):
        self.payload = payload

    def read(self, *_args):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def web_stub():
    """替换 urlopen：建会话 + 取沙箱 session + 查状态全部成功。"""
    create = {"code": 0, "msg": "OK", "data": {"id": "2102411494602919936"}}
    session = {"code": 0, "msg": "OK",
               "data": {"link": "https://box.example/acp",
                        "token": "sandbox-token",
                        "sessionId": "2102411494602919936",
                        "cwd": "/workspace"}}

    def fake(req, **_kwargs):
        if req.full_url.endswith("/session"):
            return Response(session)
        if req.full_url.endswith("/conversations/"):
            return Response(create)
        return Response({"code": 0, "msg": "OK",
                         "data": {"status": "completed"}})

    return fake


class StubCase(unittest.TestCase):
    """统一安装 urlopen / run_turn / 上报的打桩，测试结束自动还原。"""

    def install(self, turn_error, report_ok=True, streak=3):
        self.turns = []
        self.reported = []
        real_urlopen = wb_accounts.urlopen
        real_turn = wb_accounts.wb_webagent.run_turn
        real_events = wb_tasks.report_events
        real_streak = wb_tasks.fetch_streak_days

        def fake_turn(link, token, session_id, cwd, prompt, user_agent, **kwargs):
            self.turns.append(kwargs.get("wait_seconds"))
            return {"ok": False, "status": "working", "chunks": 0,
                    "elapsed_ms": 10, "error": turn_error}

        def fake_events(account, events, base=None):
            self.reported.append({"uid": account.uid, "base": base})
            return report_ok

        wb_accounts.urlopen = web_stub()
        wb_accounts.wb_webagent.run_turn = fake_turn
        wb_tasks.report_events = fake_events
        wb_tasks.fetch_streak_days = lambda account: streak
        self.addCleanup(setattr, wb_accounts, "urlopen", real_urlopen)
        self.addCleanup(setattr, wb_accounts.wb_webagent, "run_turn", real_turn)
        self.addCleanup(setattr, wb_tasks, "report_events", real_events)
        self.addCleanup(setattr, wb_tasks, "fetch_streak_days", real_streak)


class TlsTextTests(unittest.TestCase):
    def test_the_upstream_string_is_recognised(self):
        """判据吃的是字符串：run_turn 只回文本，异常对象早就丢了。"""
        self.assertTrue(wb_accounts.is_tls_verify_text(TLS_ERROR))

    def test_other_failures_are_not_certificate_failures(self):
        self.assertFalse(wb_accounts.is_tls_verify_text(NON_TLS_ERROR))
        self.assertFalse(wb_accounts.is_tls_verify_text(""))
        self.assertFalse(wb_accounts.is_tls_verify_text(None))

    def test_the_known_markers_all_match(self):
        for marker in ("certificate verify failed", "self-signed certificate",
                       "unable to get local issuer certificate",
                       "certificate has expired", "hostname mismatch"):
            self.assertTrue(wb_accounts.is_tls_verify_text("X: " + marker), marker)


class SwitchTests(unittest.TestCase):
    def test_off_by_default(self):
        """行为变更默认关：升级后线上语义不变。"""
        directory = make_dir("switch-default")
        self.assertFalse(wb_settings.daily_chat_web_fallback_activity(directory))

    def test_round_trip(self):
        directory = make_dir("switch-roundtrip")
        self.assertTrue(wb_settings.set_daily_chat_web_fallback_activity(directory, True))
        self.assertTrue(wb_settings.daily_chat_web_fallback_activity(directory))
        self.assertFalse(wb_settings.set_daily_chat_web_fallback_activity(directory, False))
        self.assertFalse(wb_settings.daily_chat_web_fallback_activity(directory))

    def test_a_hand_edited_json_does_not_turn_it_on(self):
        """只有字面 true 才算打开，字符串 "true" 之类的手改值不生效。"""
        directory = make_dir("switch-hand-edit")
        with open(wb_settings.settings_path(directory), "w", encoding="utf-8") as fh:
            json.dump({"daily_chat_web_fallback_activity": "true"}, fh)
        self.assertFalse(wb_settings.daily_chat_web_fallback_activity(directory))


class IntlBaseTests(unittest.TestCase):
    def test_streak_days_use_the_account_realm_base(self):
        """国际版必须查 www.workbuddy.ai 的 growth；打国内 base 只会拿不到天数。"""
        seen = []
        real = wb_accounts.urlopen

        def fake(req, **_kwargs):
            seen.append(req.full_url)
            return Response({"code": 0, "data": {"streak": {"days": 4}}})

        wb_accounts.urlopen = fake
        try:
            days = wb_tasks.fetch_streak_days(make_account(realm="intl"))
            self.assertEqual(days, 4)
            self.assertEqual(seen, ["https://www.workbuddy.ai/activity/growth/streak"])
            seen[:] = []
            days = wb_tasks.fetch_streak_days(make_account(realm="cn"))
            self.assertEqual(days, 4)
            self.assertTrue(seen[0].startswith(wb_tasks.CHAT_BASE), seen)
            self.assertTrue(seen[0].endswith("/activity/growth/streak"), seen)
        finally:
            wb_accounts.urlopen = real


class FallbackTests(StubCase):
    def test_a_certificate_failure_falls_back_when_the_switch_is_on(self):
        """开关打开：证书失败改走活跃上报，当天算完成，不再报整轮失败。"""
        directory = make_dir("fallback-on")
        wb_settings.set_daily_chat_web_fallback_activity(directory, True)
        acc = make_account(directory)
        self.install(TLS_ERROR)

        res = acc.daily_chat(web=True)

        self.assertTrue(res.get("ok"), res)
        self.assertNotIn("网页通道失败", res.get("error") or "")
        self.assertIn("回退", res.get("msg") or "")
        self.assertEqual(len(self.reported), 1)
        self.assertEqual(self.reported[0]["uid"], UID)
        self.assertIn("workbuddy.ai", self.reported[0]["base"])
        self.assertTrue(res.get("web_fallback", {}).get("ok"), res)
        self.assertTrue(acc.last_daily_chat)          # 当天算做过
        self.assertFalse(acc.can_daily_chat())        # 巡检不再重复跑

    def test_the_switch_being_off_keeps_the_old_verdict(self):
        """默认关：证书失败仍然如实报失败，也不写 lastDailyChat。"""
        directory = make_dir("fallback-off")
        acc = make_account(directory)
        self.install(TLS_ERROR)

        res = acc.daily_chat(web=True)

        self.assertFalse(res.get("ok"), res)
        self.assertIn("网页通道失败", res.get("msg") or "")
        self.assertIn("网页通道失败", res.get("error") or "")
        self.assertEqual(self.reported, [])
        self.assertNotIn("web_fallback", res)
        self.assertIsNone(acc.last_daily_chat)
        self.assertTrue(acc.can_daily_chat())         # 下一轮巡检还会重试

    def test_a_non_certificate_failure_never_falls_back(self):
        """开关打开也不该把「真的没跑完」当成完成：那会让 30 积分静默丢掉。"""
        directory = make_dir("fallback-non-tls")
        wb_settings.set_daily_chat_web_fallback_activity(directory, True)
        acc = make_account(directory)
        self.install(NON_TLS_ERROR)

        res = acc.daily_chat(web=True)

        self.assertFalse(res.get("ok"), res)
        self.assertIn("网页通道失败", res.get("msg") or "")
        self.assertEqual(self.reported, [])
        self.assertIsNone(acc.last_daily_chat)

    def test_a_rejected_report_leaves_the_day_open(self):
        """降级也要看结果：上报被上游拒绝时仍然算失败，好让下一轮重试。"""
        directory = make_dir("fallback-rejected")
        wb_settings.set_daily_chat_web_fallback_activity(directory, True)
        acc = make_account(directory)
        self.install(TLS_ERROR, report_ok=False)

        res = acc.daily_chat(web=True)

        self.assertFalse(res.get("ok"), res)
        self.assertIn("网页通道失败", res.get("msg") or "")
        self.assertEqual(len(self.reported), 1)
        self.assertIsNone(acc.last_daily_chat)
        self.assertTrue(acc.can_daily_chat())

    def test_a_cn_account_still_keeps_the_desktop_only_path(self):
        """国内版不走网页腿，这条升级对它没有影响。"""
        directory = make_dir("fallback-cn")
        wb_settings.set_daily_chat_web_fallback_activity(directory, True)
        acc = make_account(directory, realm="cn")
        self.install(TLS_ERROR)

        res = acc.daily_chat(web=True)

        self.assertFalse(res.get("ok"), res)
        self.assertIn("international", res.get("error") or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
