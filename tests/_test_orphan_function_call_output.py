"""Tests for orphan function_call_output items whose output carries images.

An output list with image parts makes _flatten_content() return structured
chat parts (a list). The orphan (no call_id) branch used to call .strip()
on that list and raise AttributeError, which socketserver swallows and the
connection is dropped without a response (issue observed with Codex
Desktop: every /v1/responses retry died as a bare 502).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wb_proxy as P


class OrphanFunctionCallOutputTests(unittest.TestCase):
    def test_orphan_output_with_image_parts_becomes_multimodal_user_message(self):
        # The orphan branch must keep the structured parts instead of calling
        # .strip() on the list _flatten_content() returns for multimodal input.
        payload = {"model": "deepseek-v4.1-flash", "input": [
            {"type": "function_call_output",
             "output": [{"type": "input_image",
                         "image_url": "data:image/png;base64,iVBORw0KGgo="}]},
        ]}
        chat = P.responses_to_chat(payload)  # raised AttributeError before the fix
        messages = chat["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "user")
        parts = messages[0]["content"]
        self.assertIsInstance(parts, list)
        self.assertTrue(str(parts[0].get("text", "")).startswith("[Message from another task"))
        self.assertEqual(parts[1].get("type"), "image_url")

    def test_orphan_output_text_only_still_flattens_to_text(self):
        payload = {"model": "deepseek-v4.1-flash", "input": [
            {"type": "function_call_output",
             "output": [{"type": "output_text", "text": "done"}]},
        ]}
        chat = P.responses_to_chat(payload)
        messages = chat["messages"]
        self.assertEqual(messages[0]["role"], "user")
        self.assertIn("done", messages[0]["content"])

    def test_orphan_output_dict_still_serializes(self):
        payload = {"model": "deepseek-v4.1-flash", "input": [
            {"type": "function_call_output",
             "output": {"type": "output_text", "text": "done"}},
        ]}
        chat = P.responses_to_chat(payload)
        messages = chat["messages"]
        self.assertIn("done", messages[0]["content"])

    def test_function_call_output_with_call_id_unchanged(self):
        payload = {"model": "deepseek-v4.1-flash", "input": [
            {"type": "function_call", "call_id": "call_1", "name": "shell",
             "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1",
             "output": [{"type": "output_text", "text": "done"}]},
        ]}
        chat = P.responses_to_chat(payload)
        roles = [m["role"] for m in chat["messages"]]
        self.assertEqual(roles, ["assistant", "tool"])


if __name__ == "__main__":
    unittest.main()
