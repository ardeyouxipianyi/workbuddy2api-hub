"""11-128 出站取证开关：默认关、只抓 400/11-128、摘要不含明文。

这个开关存在的唯一目的是让报告者把「真正发往上游的 body」带回来，而不是
再贴一段截断的 600 字节错误体。测试钉住四件事：

  1. 未设置 / 非 11-128 / 非 400-403 时不落盘；
  2. 摘要模式不含任何提示词明文，Authorization / API Key 一律脱敏；
  3. full 模式写出实际出站字节（gzip 会先解压），可直接做逐字节对比；
  4. 同一 body 的重试只写一份，避免客户端重试刷出几十个文件。

Run with: python tests/_test_dump_11_128.py
"""
import gzip
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy as P
import wb_accounts


SYSTEM_TEXT = ("You are Claude Code, Anthropic's official CLI tool for Claude.\n"
               "TOPSECRET-SYSTEM")
DETAIL = json.dumps({
    "code": "11-128",
    "msg": "Illegal API invocation from an unapproved channel",
    "requestId": "req-abc-123",
})


def make_body():
    return {
        "model": "deepseek-v4.1-flash",
        "stream": True,
        "messages": [
            {"role": "system", "content": SYSTEM_TEXT},
            {"role": "user", "content": "TOPSECRET-PROMPT"},
        ],
        "tools": [{
            "type": "function",
            "function": {"name": "Read", "parameters": {"type": "object"}},
        }],
    }


def make_exc(body, encoding="identity", status=400, detail=DETAIL):
    exc = urllib.error.HTTPError(
        "https://upstream.invalid/v1/chat/completions?token=query-secret",
        status, "Bad Request", {"x-request-id": "resp-123"}, io.BytesIO(b""))
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    exc.wb_outbound_body = gzip.compress(raw) if encoding == "gzip" else raw
    exc.wb_outbound_encoding = encoding
    exc.wb_outbound_headers = {
        "Authorization": "Bearer upstream-secret",
        "X-Api-Key": "api-secret",
        "User-Agent": "claude-cli/2.1.0",
        "x-stainless-os": "Windows",
        "X-Conversation-ID": "conv-abcdef",
    }
    exc.wb_outbound_url = ("https://upstream.invalid/v1/chat/completions"
                           "?token=query-secret")
    exc.wb_outbound_product = "cli"
    exc.wb_outbound_realm = "intl"
    exc.account_uid = "abcdef1234567890"
    exc.proxy_slot = "slot-1"
    exc.proxy_exit = "127.0.0.1:7890"
    return exc


class Dump11128Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_usage = P.USAGE_DIR
        self._old_env = os.environ.get("WB_DUMP_11_128")
        P.USAGE_DIR = self.tmp.name
        P._DUMP_11_128_SEEN.clear()

    def tearDown(self):
        P.USAGE_DIR = self._old_usage
        if self._old_env is None:
            os.environ.pop("WB_DUMP_11_128", None)
        else:
            os.environ["WB_DUMP_11_128"] = self._old_env
        P._DUMP_11_128_SEEN.clear()
        self.tmp.cleanup()

    def _call(self, exc, detail=DETAIL, endpoint="messages"):
        try:
            return P.maybe_dump_11_128(
                exc, detail, endpoint, "deepseek-v4.1-flash",
                fp=P.prompt_fingerprint(make_body()["messages"]), stream=True,
                client_headers={"Authorization": "Bearer client-secret",
                                "User-Agent": "claude-cli/2.1.0"})
        finally:
            exc.close()

    def _files(self):
        path = os.path.join(self.tmp.name, P.DUMP_11_128_DIR)
        return sorted(os.listdir(path)) if os.path.isdir(path) else []

    def test_disabled_writes_nothing(self):
        os.environ.pop("WB_DUMP_11_128", None)
        self.assertEqual(self._call(make_exc(make_body())), "")
        self.assertEqual(self._files(), [])

    def test_only_channel_rejection_is_dumped(self):
        os.environ["WB_DUMP_11_128"] = "1"
        other = json.dumps({"code": "11140", "msg": "account restricted"})
        self.assertEqual(self._call(make_exc(make_body()), detail=other), "")
        self.assertEqual(self._call(make_exc(make_body(), status=500)), "")
        self.assertEqual(self._files(), [])

    def test_summary_redacts_and_keeps_prompt_text_out(self):
        os.environ["WB_DUMP_11_128"] = "1"
        path = self._call(make_exc(make_body()))
        self.assertTrue(path)
        names = self._files()
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].endswith(".json"))
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
        summary = json.loads(raw)
        self.assertEqual(summary["schema"], 1)
        self.assertEqual(summary["endpoint"], "messages")
        self.assertEqual(summary["request_id"], "req-abc-123")
        self.assertEqual(summary["account"]["uid8"], "abcdef12")
        self.assertEqual(summary["upstream_url"],
                         "https://upstream.invalid/v1/chat/completions")
        self.assertTrue(summary["body"]["available"])
        self.assertEqual(summary["body"]["n_messages"], 2)
        self.assertEqual(summary["body"]["tool_names"], ["Read"])
        self.assertTrue(summary["body"]["fingerprint_hits"])
        self.assertEqual(summary["headers"]["Authorization"], "<redacted>")
        self.assertEqual(summary["headers"]["X-Api-Key"], "<redacted>")
        self.assertEqual(summary["headers"]["User-Agent"], "claude-cli/2.1.0")
        self.assertEqual(summary["client_headers"]["Authorization"],
                         "<redacted>")
        self.assertNotIn("TOPSECRET-SYSTEM", raw)
        self.assertNotIn("TOPSECRET-PROMPT", raw)
        self.assertNotIn("Bearer upstream-secret", raw)
        self.assertNotIn("query-secret", raw)
        self.assertFalse(any(name.endswith((".body.json", ".body.bin"))
                             for name in names))

    def test_full_mode_writes_exact_decompressed_body(self):
        os.environ["WB_DUMP_11_128"] = "full"
        body = make_body()
        path = self._call(make_exc(body, encoding="gzip"))
        self.assertTrue(path)
        with open(path, encoding="utf-8") as handle:
            summary = json.load(handle)
        body_name = summary["body"]["file"]
        self.assertTrue(body_name.endswith(".body.json"))
        body_path = os.path.join(self.tmp.name, P.DUMP_11_128_DIR, body_name)
        with open(body_path, "rb") as handle:
            dumped = handle.read()
        self.assertEqual(dumped,
                         json.dumps(body, ensure_ascii=False).encode("utf-8"))
        self.assertIn(b"TOPSECRET-PROMPT", dumped)

    def test_same_body_is_dumped_once(self):
        os.environ["WB_DUMP_11_128"] = "1"
        self.assertTrue(self._call(make_exc(make_body())))
        self.assertEqual(self._call(make_exc(make_body())), "")
        self.assertEqual(len(self._files()), 1)

    def test_channel_rejection_detection(self):
        self.assertTrue(P.is_channel_rejection(400, DETAIL))
        self.assertTrue(P.is_channel_rejection(403, DETAIL))
        self.assertFalse(P.is_channel_rejection(500, DETAIL))
        self.assertFalse(P.is_channel_rejection(
            400, json.dumps({"code": "11140"})))


class OpenUpstreamAttachTests(unittest.TestCase):
    """最终出站字节必须挂到异常上，而不是翻译前的 chat body。"""

    class Pool(object):
        def __init__(self, accounts):
            self.accounts = accounts
            self.affinity = wb_accounts.SessionAffinity()

        def count_ready(self, realm, model=None):
            return sum(1 for account in self.accounts
                       if account.realm == realm and account.ready(model=model))

        def pick_for_session(self, realm=None, session_key=None, exclude=(),
                             model=None, page=None):
            for account in self.accounts:
                if account.uid in exclude or account.realm != realm:
                    continue
                if account.ready(model=model):
                    return account
            return None

        def apply_remaining_weights(self, weights=None):
            return None

    def setUp(self):
        self._old_pool = P.POOL
        self._old_urlopen = wb_accounts.urlopen
        self._old_env = os.environ.get("WB_DUMP_11_128")
        os.environ["WB_DUMP_11_128"] = "1"

    def tearDown(self):
        P.POOL = self._old_pool
        wb_accounts.urlopen = self._old_urlopen
        if self._old_env is None:
            os.environ.pop("WB_DUMP_11_128", None)
        else:
            os.environ["WB_DUMP_11_128"] = self._old_env

    def test_http_error_carries_final_outbound_body(self):
        account = wb_accounts.Account({
            "uid": "dump-attach-1", "accessToken": "t-attach",
            "realm": "intl",
        })
        P.POOL = self.Pool([account])
        detail = json.dumps({"code": "11-128", "msg": "unapproved channel"})

        def reject(req, timeout=None, proxy=None):
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", {}, io.BytesIO(detail.encode()))

        wb_accounts.urlopen = reject
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            P.open_upstream({
                "model": "deepseek-v4.1-flash",
                "messages": [{"role": "user", "content": "attach-me"}],
            }, target_realm="intl")
        exc = ctx.exception
        self.assertTrue(exc.wb_outbound_body)
        self.assertEqual(exc.wb_outbound_encoding, "identity")
        self.assertIn("Authorization", exc.wb_outbound_headers)
        self.assertTrue(exc.wb_outbound_url.endswith("/chat/completions"))
        self.assertEqual(exc.wb_outbound_product, "workbuddy")
        self.assertEqual(exc.wb_outbound_realm, "intl")
        exc.close()

    def test_cli_identity_keeps_gzip_body(self):
        account = wb_accounts.Account({
            "uid": "dump-attach-cli", "accessToken": "t-attach-cli",
            "realm": "intl", "product": "cli",
        })
        P.POOL = self.Pool([account])
        detail = json.dumps({"code": "11-128", "msg": "unapproved channel"})

        def reject(req, timeout=None, proxy=None):
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", {}, io.BytesIO(detail.encode()))

        wb_accounts.urlopen = reject
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            P.open_upstream({
                "model": "deepseek-v4.1-flash",
                "messages": [{"role": "user", "content": "attach-cli"}],
            }, target_realm="intl")
        exc = ctx.exception
        self.assertEqual(exc.wb_outbound_encoding, "gzip")
        self.assertIn(b"deepseek-v4.1-flash", gzip.decompress(exc.wb_outbound_body))
        exc.close()


if __name__ == "__main__":
    unittest.main()
