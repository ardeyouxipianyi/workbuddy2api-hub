"""上游风控限制 403（code 11140）：冷却、持久化，且不再换号连坐。

实测正文：{"code":11140,"msg":"request illegal",...,"displayMsg":{"en":"Your
account has been restricted due to unusual activity","zh":"账号存在异常行为，
已被限制使用"}}。官方客户端同样报这个错，说明是账号级标记而不是请求内容
（yetone/magpie #1849 的对照：同一请求、同一套头、同一模型，有的账号 200、
有的 403）。

社区复盘（Zhengyuuuui/workbuddy2api《封禁事件复盘与改进执行清单》）给出的
处置红线是：看到 11140 立即停手，解封前严禁换号继续打同一台设备——设备指纹
会连坐。旧实现在 11140 上「冷却该号、立刻换下一个号继续同一请求」，一个请求
能遍历整个池子；#422 用户实测 7 个号在一次任务里全被封。

要钉住的行为：

  1. 风控 403 与内容审核 403 分流：前者冷却账号 2 小时并**持久化**，默认
     **不再换号重试**（一个请求只打一个号就停手，下一次请求自然会落到别的号）；
  2. 同一窗口内多个账号被限制 → 整池暂停（连坐保护），暂停期间一个上游请求
     都不发；WB_RESTRICTION_HOLD_SECONDS=0 可关闭暂停；
  3. WB_RESTRICTED_403_ROTATE_MAX>0 可恢复旧的透明换号行为；
  4. 内容审核 403 的原路径不变（降级重试、透传，不给账号上长冷却）；
  5. 一切 >=500 的未知状态码（实测 550）也按瞬时故障轮转；
  6. 池里只有这一个账号时，仍把限制原因交给客户端。
  7. 换号重试时 CLI 身份的 X-Conversation-Request-ID 整轮复用、message id 每
     请求新生成，桌面端身份保持原样（官方 CLI 2.168.2 对齐的回归钉）。

Run with the current interpreter (python tests/run_all.py restricted).
"""
import io
import gzip
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error

_TMP = tempfile.mkdtemp(prefix="wb-restrict403-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_proxy as P


RESTRICT_BODY = json.dumps({
    "code": 11140, "msg": "request illegal", "requestId": "1" * 32,
    "displayMsg": {
        "en": "Your account has been restricted due to unusual activity",
        "zh": "账号存在异常行为，已被限制使用",
    },
}).encode("utf-8")

CONTENT_BODY = json.dumps({
    "code": 10001, "msg": "content rejected",
}).encode("utf-8")


def http_error(code, body):
    return urllib.error.HTTPError("https://upstream/chat", code, "err", {},
                                  io.BytesIO(body))


class FakeResp(object):
    pass


def make_account(uid):
    return wb_accounts.Account({"uid": uid, "accessToken": "t-" + uid,
                                "realm": "intl"})


def make_cli_account(uid):
    """官方 CLI 身份的账号（product=cli）。"""
    return wb_accounts.Account({"uid": uid, "accessToken": "t-" + uid,
                                "realm": "intl", "product": "cli"})


def request_headers(req):
    """urllib 会把头名 capitalize，比较时统一小写。"""
    return {str(k).lower(): v for k, v in req.headers.items()}


def run_switch(accounts, script):
    """一次 open_upstream：第一个账号 403 11140，第二个账号正常应答。

    返回 (upstream, result, raised)；upstream.calls 是两次出站请求。
    """
    old_pool = P.POOL
    P.POOL = StubPool(accounts)
    upstream = ScriptedUpstream(script)
    real_urlopen = wb_accounts.urlopen
    wb_accounts.urlopen = upstream
    try:
        result = P.open_upstream(dict(PAYLOAD), target_realm="intl")
        raised = None
    except Exception as exc:
        result = None
        raised = exc
    finally:
        wb_accounts.urlopen = real_urlopen
        P.POOL = old_pool
    return upstream, result, raised


class StubPool(object):
    """pick_for_session 走真实的 ready() 语义：冷却/受限中的账号不再被选中。"""

    def __init__(self, accounts):
        self.accounts = accounts
        self.affinity = wb_accounts.SessionAffinity()

    def count_ready(self, realm, model=None):
        return sum(1 for a in self.accounts
                   if a.realm == realm and a.ready(model=model))

    def pick_for_session(self, realm=None, session_key=None, exclude=(),
                         model=None, page=None):
        for a in self.accounts:
            if a.uid in exclude:
                continue
            if realm and a.realm != realm:
                continue
            if a.ready(model=model):
                return a
        return None

    def apply_remaining_weights(self, weights=None):
        return None

    def apply_daily_token_limit(self, value=None, usage=None):
        return value or 0

    def apply_daily_credit_limit(self, value=None, credits=None, free_models=None):
        return value or 0

    def apply_model_daily_token_limit(self, value=None, per_model=None):
        return value or 0


class ScriptedUpstream(object):
    """按尝试顺序吐预设结果：异常抛出、FakeResp 返回。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, req, timeout=None, proxy=None):
        self.calls.append(req)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


PAYLOAD = {"model": "deepseek-v4.1-flash",
           "messages": [{"role": "user", "content": "hi"}]}


class Restricted403DetectionTests(unittest.TestCase):
    def test_real_restriction_body_matches(self):
        self.assertTrue(P.restriction_is_account_level(RESTRICT_BODY.decode("utf-8")))
        # 400 字节截断（旧读法）也能命中 code 与 en 措辞。
        self.assertTrue(P.restriction_is_account_level(
            RESTRICT_BODY.decode("utf-8")[:400]))
        # code 写成字符串、带空格的变体。
        self.assertTrue(P.restriction_is_account_level('{"code": "11140"}'))

    def test_content_and_junk_bodies_do_not_match(self):
        self.assertFalse(P.restriction_is_account_level(CONTENT_BODY.decode("utf-8")))
        self.assertFalse(P.restriction_is_account_level(""))
        self.assertFalse(P.restriction_is_account_level(None))
        # hex requestId 里可能出现 "11140" 子串，裸字符串不算数。
        self.assertFalse(P.restriction_is_account_level(
            '{"msg":"x","requestId":"11140abcdef"}'))


class Restricted403DispatchTests(unittest.TestCase):
    def setUp(self):
        self.old_pool = P.POOL
        self.old_rotate = P.RESTRICTED_403_ROTATE_MAX
        self.old_min = P.RESTRICTION_WAVE_MIN
        self.old_window = P.RESTRICTION_WAVE_WINDOW
        self.old_hold = P.RESTRICTION_HOLD_SECONDS
        P.RESTRICTED_403_ROTATE_MAX = 0
        P.RESTRICTION_WAVE_MIN = 2
        P.RESTRICTION_WAVE_WINDOW = 900.0
        P.RESTRICTION_HOLD_SECONDS = 1800.0
        P.reset_restriction_state()

    def tearDown(self):
        P.POOL = self.old_pool
        P.RESTRICTED_403_ROTATE_MAX = self.old_rotate
        P.RESTRICTION_WAVE_MIN = self.old_min
        P.RESTRICTION_WAVE_WINDOW = self.old_window
        P.RESTRICTION_HOLD_SECONDS = self.old_hold
        P.reset_restriction_state()

    def _run(self, accounts, script):
        P.POOL = StubPool(accounts)
        upstream = ScriptedUpstream(script)
        self.upstream = upstream
        real_urlopen = wb_accounts.urlopen
        wb_accounts.urlopen = upstream
        try:
            result = P.open_upstream(dict(PAYLOAD), target_realm="intl")
            raised = None
        except Exception as exc:
            result = None
            raised = exc
        finally:
            wb_accounts.urlopen = real_urlopen
        return result, raised, upstream

    def test_restricted_account_stops_the_request(self):
        restricted = make_account("r1")
        healthy = make_account("h1")
        resp, raised, upstream = self._run(
            [restricted, healthy],
            [http_error(403, RESTRICT_BODY), FakeResp()])
        # 默认不换号：一条请求只打了一个号就停手，健康账号没被牵连。
        self.assertEqual(len(upstream.calls), 1)
        self.assertIsNone(resp)
        self.assertIsInstance(raised, P.ContentRejected)
        self.assertEqual(getattr(raised, "account_uid", None), "r1")
        self.assertIn("11140", raised.detail)
        self.assertGreaterEqual(restricted.restricted_until - time.time(), 7000)
        self.assertEqual(restricted.last_error, "HTTP 403 (account restricted)")
        self.assertIn("11140", restricted.last_error_detail)
        self.assertEqual(healthy.cooldown_until, 0.0)
        self.assertEqual(healthy.restricted_until, 0.0)
        # 下一次请求会落到健康账号：受限账号不再是候选。
        self.assertFalse(restricted.ready())

    def test_rotate_switch_restores_transparent_rotation(self):
        P.RESTRICTED_403_ROTATE_MAX = 1
        restricted = make_account("r1")
        healthy = make_account("h1")
        resp, raised, upstream = self._run(
            [restricted, healthy],
            [http_error(403, RESTRICT_BODY), FakeResp()])
        self.assertEqual(len(upstream.calls), 2)
        self.assertIsNone(raised)
        self.assertEqual(resp[1].uid, "h1")
        self.assertGreaterEqual(restricted.restricted_until - time.time(), 7000)
        self.assertEqual(healthy.cooldown_until, 0.0)

    def test_lone_restricted_account_still_reports_the_reason(self):
        restricted = make_account("r-only")
        resp, raised, upstream = self._run(
            [restricted], [http_error(403, RESTRICT_BODY)])
        self.assertEqual(len(upstream.calls), 1)
        self.assertIsNone(resp)
        self.assertIsInstance(raised, P.ContentRejected)
        self.assertEqual(getattr(raised, "account_uid", None), "r-only")
        self.assertIn("11140", raised.detail)
        # 单号场景不再退化成 3 秒冷却：2 小时内不会再打这个号。
        self.assertGreaterEqual(restricted.restricted_until - time.time(), 7000)

    def test_two_restrictions_pause_the_whole_pool(self):
        first = make_account("a1")
        second = make_account("b1")
        P.POOL = StubPool([first, second])
        upstream = ScriptedUpstream([http_error(403, RESTRICT_BODY),
                                     http_error(403, RESTRICT_BODY)])
        real_urlopen = wb_accounts.urlopen
        wb_accounts.urlopen = upstream
        try:
            with self.assertRaises(P.ContentRejected):
                P.open_upstream(dict(PAYLOAD), target_realm="intl")
            with self.assertRaises(P.ContentRejected):
                P.open_upstream(dict(PAYLOAD), target_realm="intl")
            self.assertEqual(len(upstream.calls), 2)
            self.assertGreater(P.restriction_hold_remaining("intl"), 1700)
            # 暂停期间：一个上游请求都不发，直接 429 + 说明。
            with self.assertRaises(P.RateLimited) as ctx:
                P.open_upstream(dict(PAYLOAD), target_realm="intl")
            self.assertEqual(len(upstream.calls), 2)
            self.assertIn("暂停", ctx.exception.message)
        finally:
            wb_accounts.urlopen = real_urlopen

    def test_hold_switch_can_disable_the_pause(self):
        P.RESTRICTION_HOLD_SECONDS = 0.0
        first = make_account("a1")
        second = make_account("b1")
        P.POOL = StubPool([first, second])
        upstream = ScriptedUpstream([http_error(403, RESTRICT_BODY),
                                     http_error(403, RESTRICT_BODY)])
        real_urlopen = wb_accounts.urlopen
        wb_accounts.urlopen = upstream
        try:
            for _ in range(2):
                with self.assertRaises(P.ContentRejected):
                    P.open_upstream(dict(PAYLOAD), target_realm="intl")
            self.assertEqual(P.restriction_hold_remaining("intl"), 0.0)
        finally:
            wb_accounts.urlopen = real_urlopen

    def test_content_403_keeps_its_old_path(self):
        a = make_account("c1")
        b = make_account("c2")
        resp, raised, upstream = self._run(
            [a, b],
            [http_error(403, CONTENT_BODY), http_error(403, CONTENT_BODY)])
        # 内容审核 403 不给账号上长冷却（这不是账号的坏）。
        self.assertEqual(a.cooldown_until, 0.0)
        self.assertEqual(a.restricted_until, 0.0)
        self.assertEqual(b.cooldown_until, 0.0)
        self.assertIsNone(resp)
        self.assertIsInstance(raised, P.ContentRejected)

    def test_unknown_5xx_rotates_instead_of_passing_through(self):
        flaky = make_account("f1")
        healthy = make_account("f2")
        resp, raised, upstream = self._run(
            [flaky, healthy],
            [http_error(550, b""), FakeResp()])
        self.assertIsNone(raised)
        self.assertEqual(resp[1].uid, "f2")
        self.assertEqual(flaky.fails, 1)


class ChannelGuardAttributionTests(unittest.TestCase):
    """400 透传也要写明用了哪个账号、哪个代理出口（#743）。

    400 在 open_upstream() 里直接 raise（不轮转、不改正文），旧实现因此
    绕过了函数末尾的 account_uid 附加，错误行只能记成 unattributed。
    """

    def setUp(self):
        self._old_pool = P.POOL
        self._old_rotate = P.RESTRICTED_403_ROTATE_MAX
        P.RESTRICTED_403_ROTATE_MAX = 0

    def tearDown(self):
        P.POOL = self._old_pool
        P.RESTRICTED_403_ROTATE_MAX = self._old_rotate

    def _run(self, scripts):
        account = wb_accounts.Account({
            "uid": "400-user-1", "accessToken": "t-800",
            "realm": "intl", "proxy": "http://user:pass@127.0.0.1:17913",
            "proxySlot": "slot-8",
        })
        P.POOL = StubPool([account])
        upstream = ScriptedUpstream(scripts)
        real_urlopen = wb_accounts.urlopen
        wb_accounts.urlopen = upstream
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                P.open_upstream(dict(PAYLOAD), target_realm="intl")
        finally:
            wb_accounts.urlopen = real_urlopen
        return ctx.exception, upstream

    def test_400_carries_account_and_egress(self):
        body = json.dumps({
            "code": "11-128",
            "msg": "Illegal API invocation from an unapproved channel",
        }).encode("utf-8")
        raised, upstream = self._run([http_error(400, body)])
        self.assertIsInstance(raised, urllib.error.HTTPError)
        self.assertEqual(len(upstream.calls), 1)
        self.assertEqual(getattr(raised, "account_uid", None), "400-user-1")
        self.assertEqual(getattr(raised, "proxy_slot", None), "slot-8")
        self.assertEqual(getattr(raised, "proxy_exit", None), "127.0.0.1:17913")

    def test_hundreds_of_400s_keep_the_attribution(self):
        body = b'{"code":11-128,"msg":"Illegal API invocation"}'
        for _ in range(22):
            raised, _upstream = self._run([http_error(400, body)])
            self.assertEqual(getattr(raised, "account_uid", None), "400-user-1")
            self.assertEqual(getattr(raised, "proxy_slot", None), "slot-8")
            self.assertEqual(getattr(raised, "proxy_exit", None),
                             "127.0.0.1:17913")

    def test_egress_exit_never_leaks_proxy_credentials(self):
        self.assertEqual(P.egress_exit(""), "direct")
        self.assertEqual(P.egress_exit("http://user:secret@10.0.0.9:8080"),
                         "10.0.0.9:8080")
        self.assertEqual(P.egress_exit("10.0.0.9:8080"), "10.0.0.9:8080")


class IdentityOnRetryTests(unittest.TestCase):
    """换号重试时的出站身份语义（官方 CLI 2.168.2 对齐）。"""

    def setUp(self):
        # 默认已改为「11140 不换号」；这组用例只验证换号后的身份形态，
        # 显式打开旧的透明轮转开关。
        self._old_rotate = P.RESTRICTED_403_ROTATE_MAX
        P.RESTRICTED_403_ROTATE_MAX = 1

    def tearDown(self):
        P.RESTRICTED_403_ROTATE_MAX = self._old_rotate

    def test_cli_turn_request_id_survives_the_account_switch(self):
        upstream, resp, raised = run_switch(
            [make_cli_account("r1"), make_cli_account("h1")],
            [http_error(403, RESTRICT_BODY), FakeResp()])
        self.assertIsNone(raised)
        self.assertEqual(resp[1].uid, "h1")
        self.assertEqual(len(upstream.calls), 2)
        first = request_headers(upstream.calls[0])
        second = request_headers(upstream.calls[1])
        # 一轮 user send 的 request id 在整轮（含换号重试）复用同一个值。
        self.assertTrue(first["x-conversation-request-id"])
        self.assertEqual(first["x-conversation-request-id"],
                         second["x-conversation-request-id"])
        # 主对话也发 X-Root-Request-ID，值 = 本轮 conversation request id。
        self.assertEqual(first["x-root-request-id"],
                         first["x-conversation-request-id"])
        # message id 每请求新生成；会话 id 按账号隔离（换号就换会话）。
        self.assertEqual(first["x-request-id"], first["x-conversation-message-id"])
        self.assertNotEqual(first["x-request-id"], second["x-request-id"])
        self.assertNotEqual(first["x-conversation-id"],
                            second["x-conversation-id"])
        # 官方 CLI 2.168.2 的身份头（含 OpenAI Node SDK 6.25.0 的指纹头）。
        self.assertEqual(first["user-agent"],
                         "CLI/2.168.2 CodeBuddy/2.168.2")
        self.assertEqual(first["x-ide-version"], "2.168.2")
        self.assertEqual(first["x-agent-runtime"], "other")
        self.assertEqual(first["x-private-data"], "false")
        self.assertEqual(first["x-stainless-lang"], "js")
        self.assertEqual(first["x-stainless-package-version"], "6.25.0")
        self.assertEqual(first["x-stainless-runtime-version"], "v22.21.1")
        self.assertTrue(first["x-b3-parentspanid"])
        # 官方 CLI 的 chat 没有浏览器头，Accept 就是 application/json。
        self.assertEqual(first["accept"], "application/json")
        for gone in ("origin", "referer", "x-codebuddy-request",
                     "accept-language", "x-device-token", "x-requested-with"):
            self.assertNotIn(gone, first)
        # 请求体：gzip + cache_retention=long，不带 metadata。
        # （紧凑 JSON 由 transport PR #429 统一钉，本 PR 不重复实现。）
        self.assertEqual(first["content-encoding"], "gzip")
        self.assertEqual(upstream.calls[0].data[:2], b"\x1f\x8b")
        body = json.loads(gzip.decompress(upstream.calls[0].data).decode("utf-8"))
        self.assertEqual(body.get("cache_retention"), "long")
        self.assertNotIn("metadata", body)
        self.assertNotIn("conversation_id", body)

    def test_desktop_identity_is_unchanged_by_the_cli_alignment(self):
        upstream, resp, raised = run_switch(
            [make_account("d1"), make_account("d2")],
            [http_error(403, RESTRICT_BODY), FakeResp()])
        self.assertIsNone(raised)
        self.assertEqual(resp[1].uid, "d2")
        first = request_headers(upstream.calls[0])
        second = request_headers(upstream.calls[1])
        # 桌面端仍是每请求现生成会话 request id（本 PR 不改它的行为）。
        self.assertNotEqual(first["x-conversation-request-id"],
                            second["x-conversation-request-id"])
        self.assertNotIn("x-b3-parentspanid", first)
        self.assertEqual(first["user-agent"],
                         "WorkBuddy/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0")
        # 桌面端的请求体与浏览器头都保持原样（不 gzip）。
        self.assertNotIn("content-encoding", first)
        self.assertNotEqual(upstream.calls[0].data[:2], b"\x1f\x8b")
        self.assertEqual(first["accept"], "application/json, text/plain, */*")
        self.assertIn("origin", first)
        self.assertIn("referer", first)
        self.assertIn("x-codebuddy-request", first)
        self.assertIn("accept-language", first)
class RestrictionPersistenceTests(unittest.TestCase):
    """限制期限是账号状态：落盘、重启/清冷却/测试都不能把它放回池子。"""

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="wb-restrict-persist-")

    def _restricted(self, uid="p1"):
        account = wb_accounts.Account({"uid": uid, "accessToken": "t-" + uid,
                                       "realm": "intl"})
        account.note_restricted(detail='{"code":11140}')
        return account

    def test_restriction_survives_save_and_reload(self):
        account = self._restricted()
        account.save(self.directory)
        with open(account.path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertGreater(data.get("restrictedUntil") or 0, time.time() + 7000)
        reloaded = wb_accounts.Account(data, account.path)
        self.assertGreater(reloaded.restricted_until, time.time() + 7000)
        self.assertFalse(reloaded.ready())
        self.assertIn("上游风控限制", reloaded.unavailable_reason())

    def test_clear_error_keeps_the_restriction(self):
        account = self._restricted()
        account.clear_error()
        self.assertGreater(account.restricted_until, time.time() + 7000)
        account.clear_error(keep_caps=True)
        self.assertGreater(account.restricted_until, time.time() + 7000)

    def test_a_successful_request_lifts_the_restriction(self):
        account = self._restricted()
        account.note_success()
        self.assertEqual(account.restricted_until, 0.0)
        self.assertTrue(account.ready())

    def test_restriction_expires_on_its_own(self):
        account = self._restricted()
        account.restricted_until = time.time() - 1
        self.assertTrue(account.ready())
        self.assertEqual(account.throttle_wait(), 0.0)

    def test_repeated_detection_does_not_extend_the_window(self):
        account = self._restricted()
        first = account.restricted_until
        account.note_restricted(detail='{"code":11140}')
        self.assertAlmostEqual(account.restricted_until, first, delta=0.01)


if __name__ == "__main__":
    unittest.main()
