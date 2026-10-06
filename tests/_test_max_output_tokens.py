"""Tests for defaulting max_tokens from model catalog (issue #121)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wb_proxy as P


class MaxOutputTokensTests(unittest.TestCase):
    def test_responses_to_chat_defaults_max_output_tokens(self):
        # When Codex / Responses API omits max_output_tokens, deepseek-v4.1-flash
        # gets 128000 from the catalog instead of letting upstream truncate at 32k.
        payload = {"model": "deepseek-v4.1-flash", "input": "hello"}
        chat = P.responses_to_chat(payload)
        self.assertEqual(chat.get("max_tokens"), 128000)

    def test_responses_to_chat_honors_explicit_max_output_tokens(self):
        payload = {
            "model": "deepseek-v4.1-flash",
            "input": "hello",
            "max_output_tokens": 4096,
        }
        chat = P.responses_to_chat(payload)
        self.assertEqual(chat.get("max_tokens"), 4096)

    def test_build_upstream_body_defaults_max_tokens(self):
        # Standard Chat Completions request without max_tokens also receives
        # the catalog default maxOutputTokens.
        chat = {
            "model": "deepseek-v4.1-flash",
            "messages": [{"role": "user", "content": "hello"}],
        }
        body = P.build_upstream_body(chat)
        self.assertEqual(body.get("max_tokens"), 128000)

    def test_build_upstream_body_honors_explicit_max_tokens(self):
        chat = {
            "model": "deepseek-v4.1-flash",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 2048,
        }
        body = P.build_upstream_body(chat)
        self.assertEqual(body.get("max_tokens"), 2048)

    def test_build_upstream_body_honors_max_completion_tokens(self):
        chat = {
            "model": "deepseek-v4.1-flash",
            "messages": [{"role": "user", "content": "hello"}],
            "max_completion_tokens": 8192,
        }
        body = P.build_upstream_body(chat)
        self.assertEqual(body.get("max_tokens"), 8192)

    def test_unknown_model_leaves_max_tokens_unset(self):
        chat = {
            "model": "unrecognized-custom-model",
            "messages": [{"role": "user", "content": "hello"}],
        }
        body = P.build_upstream_body(chat)
        self.assertNotIn("max_tokens", body)

    def test_model_default_max_output_tokens_lookup(self):
        self.assertEqual(
            P.model_default_max_output_tokens("deepseek-v4.1-flash"), 128000
        )
        self.assertEqual(P.model_default_max_output_tokens("gpt-6-astra"), 128000)
        self.assertIsNone(P.model_default_max_output_tokens("non-existent-model"))


if __name__ == "__main__":
    unittest.main()

