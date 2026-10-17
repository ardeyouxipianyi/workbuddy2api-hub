"""上游风控限制 403（code 11140）必须换号，而不是透传给客户端。

实测正文：{"code":11140,"msg":"request illegal",...,"displayMsg":{"en":"Your
account has been restricted due to unusual activity","zh":"账号存在异常行为，
已被限制使用"}}。这在重试循环里原本落进内容审核分支：降级提示词重试一次，
然后 break——被限制的账号既不冷却、也继续被选用，客户端一次次拿到 403，
对话就这样被打死（2026-10-11 生产实测）。

要钉住的行为：

  1. 风控 403 与内容审核 403 分流：前者冷却账号 2 小时并轮到下一个账号；
  2. 内容审核 403 的原路径不变（降级重试、透传，不给账号上长冷却）；
  3. 一切 >=500 的未知状态码（实测 550）也按瞬时故障轮转；
  4. 池里只有这一个账号时，仍把限制原因交给客户端（没有别的可试）；
  5. 换号重试时 CLI 身份的 X-Conversation-Request-ID 整轮复用、message id 每
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
    """pick_for_session 走真实的 ready() 语义：冷却中的账号不再被选中。"""

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

    def tearDown(self):
        P.POOL = self.old_pool

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
        return result, raised

    def test_restricted_account_is_cooled_and_next_account_serves(self):
        restricted = make_account("r1")
        healthy = make_account("h1")
        resp, raised = self._run(
            [restricted, healthy],
            [http_error(403, RESTRICT_BODY), FakeResp()])
        self.assertIsNone(raised)
        self.assertEqual(resp[1].uid, "h1")
        # 被限制的账号进 2 小时冷却，不再被选用。
        self.assertGreaterEqual(restricted.cooldown_until - time.time(), 7000)
        self.assertEqual(restricted.last_error, "HTTP 403 (account restricted)")
        self.assertIn("11140", restricted.last_error_detail)
        # 健康账号没有被牵连。
        self.assertEqual(healthy.cooldown_until, 0.0)

    def test_lone_restricted_account_still_reports_the_reason(self):
        restricted = make_account("r-only")
        resp, raised = self._run(
            [restricted],
            [http_error(403, RESTRICT_BODY), http_error(403, RESTRICT_BODY)])
        self.assertIsNone(resp)
        self.assertIsInstance(raised, P.ContentRejected)
        self.assertEqual(getattr(raised, "account_uid", None), "r-only")
        self.assertIn("11140", raised.detail)

    def test_content_403_keeps_its_old_path(self):
        a = make_account("c1")
        b = make_account("c2")
        resp, raised = self._run(
            [a, b],
            [http_error(403, CONTENT_BODY), http_error(403, CONTENT_BODY)])
        # 内容审核 403 不给账号上长冷却（这不是账号的坏）。
        self.assertEqual(a.cooldown_until, 0.0)
        self.assertEqual(b.cooldown_until, 0.0)
        self.assertIsNone(resp)
        self.assertIsInstance(raised, P.ContentRejected)

    def test_unknown_5xx_rotates_instead_of_passing_through(self):
        flaky = make_account("f1")
        healthy = make_account("f2")
        resp, raised = self._run(
            [flaky, healthy],
            [http_error(550, b""), FakeResp()])
        self.assertIsNone(raised)
        self.assertEqual(resp[1].uid, "f2")
        self.assertEqual(flaky.fails, 1)


class IdentityOnRetryTests(unittest.TestCase):
    """换号重试时的出站身份语义（官方 CLI 2.168.2 对齐）。"""

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


if __name__ == "__main__":
    unittest.main()
