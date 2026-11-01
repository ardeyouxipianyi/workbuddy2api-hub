"""Tests for gateway_hint 413 and GPT max_tokens clamping."""
import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wb_proxy

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

if __name__ == "__main__":
    unittest.main()
