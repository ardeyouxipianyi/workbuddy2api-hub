"""Tests for gateway_hint 413, GPT max_tokens clamping and CodeArts 429 mapping."""
import io
import json
import time
import unittest
import sys
import os
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wb_proxy
import wb_codearts

class GatewayFixesTest(unittest.TestCase):
    def test_gateway_hint_413(self):
        hint = wb_proxy.gateway_hint(413, "payload too large (20375735 bytes > 16777216 limit)")
        self.assertIn("WB_MAX_PAYLOAD_BYTES", hint)
        self.assertIn("panel settings", hint)

    def test_gpt_max_tokens_clamping(self):
        body = {"model": "gpt-6-luna", "messages": [], "max_tokens": 1}
        upstream = wb_proxy.build_upstream_body(body)
        self.assertEqual(upstream["max_tokens"], 16)

        body_ds = {"model": "deepseek-v4.1-flash", "messages": [], "max_tokens": 1}
        upstream_ds = wb_proxy.build_upstream_body(body_ds)
        self.assertEqual(upstream_ds["max_tokens"], 1)

        body_ok = {"model": "gpt-6.1-sol", "messages": [], "max_tokens": 32}
        upstream_ok = wb_proxy.build_upstream_body(body_ok)
        self.assertEqual(upstream_ok["max_tokens"], 32)

class CodeArts429Tests(unittest.TestCase):
    """A CodeArts upstream 429 keeps its status and reset instant in the OpenAI envelope."""

    class _Handler(wb_proxy.Handler):
        def __init__(self):
            self.path = "/v1/chat/completions"
            self.status = None
            self.sent_headers = []
            self.wfile = io.BytesIO()
            self._json_etag = None
            self._json_extra_headers = None

        def send_response(self, code, message=None):
            self.status = code

        def send_header(self, key, value):
            self.sent_headers.append((key, value))

        def end_headers(self):
            pass

        def _handle_expect_continue(self):
            pass

        def _discard_body(self):
            pass

        def _key_id(self):
            return None

    def test_codearts_429_maps_to_rate_limit(self):
        reset_text = time.strftime("%Y-%m-%d %H:%M:%S UTC",
                                   time.gmtime(time.time() + 3600))
        exc = wb_codearts.CodeArtsUpstreamError(
            f"usage exceeds frequency limit, reset at {reset_text}",
            status=429)
        handler = self._Handler()
        with mock.patch.object(wb_proxy, "record_error", lambda *a, **k: None):
            handler._codearts_error("glm-5.2", exc, time.time(), "codearts")
        self.assertEqual(handler.status, 429)
        headers = dict(handler.sent_headers)
        self.assertIn("X-RateLimit-Reset", headers)
        self.assertIn("X-RateLimit-Reset-At", headers)
        body = json.loads(handler.wfile.getvalue())
        self.assertEqual(body["error"]["type"], "rate_limit_error")
        self.assertIn("reset_at", body["error"])

if __name__ == "__main__":
    unittest.main()
