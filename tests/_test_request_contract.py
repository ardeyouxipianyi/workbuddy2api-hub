"""Opt-in request fidelity, not upstream constraint enforcement (no network)."""
import copy
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _isolated_dirs import isolated_data_dirs

_TMP = isolated_data_dirs("wb-request-contract-")
import wb_proxy as P

FLAG = "WB_PRESERVE_REQUEST_CONTRACT"
SCHEMA = {"type": "object", "properties": {"code": {"type": "integer"}},
          "required": ["code"], "additionalProperties": False}
DATA = '{"code":11128,"literal":"11128"}'


class RequestContractTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {FLAG: "1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def convert(self, fmt):
        return P.responses_to_chat({"model": "deepseek-v4.1-flash", "input": "hi",
                                    "text": {"format": fmt}})

    def namespace(self, strict=True, nested=False):
        fn = {"name": "lookup_fixture", "parameters": copy.deepcopy(SCHEMA),
              "strict": strict}
        entry = {"type": "function", "function": fn} if nested else dict(fn, type="function")
        return [{"type": "namespace", "name": "fixture", "tools": [entry]}]

    def tool_turn(self):
        return [{"role": "user", "content": "Run lookup_fixture."},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_fixture", "type": "function", "function": {
                        "name": "lookup_fixture", "arguments": DATA}}]},
                {"role": "tool", "tool_call_id": "call_fixture", "content": DATA}]

    def test_default_is_off(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(P.preserve_request_contract_enabled())

    def test_flag_spellings(self):
        for value in ("1", "true", "YES", " on "):
            with self.subTest(value=value), mock.patch.dict(os.environ, {FLAG: value}):
                self.assertTrue(P.preserve_request_contract_enabled())
        for value in ("0", "false", "", "no", "typo"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {FLAG: value}):
                self.assertFalse(P.preserve_request_contract_enabled())

    def test_json_schema_mapping_and_wire_body(self):
        fmt = {"type": "json_schema", "name": "fixture", "description": "fixture schema",
               "schema": copy.deepcopy(SCHEMA), "strict": True}
        expected = {"type": "json_schema", "json_schema": {
            key: value for key, value in fmt.items() if key != "type"}}
        chat = self.convert(fmt)
        self.assertEqual(chat["response_format"], expected)
        self.assertEqual(P.build_upstream_body(chat)["response_format"], expected)

    def test_json_schema_false_and_omitted_optional_fields(self):
        fmt = {"type": "json_schema", "name": "fixture", "schema": SCHEMA, "strict": False}
        out = self.convert(fmt)["response_format"]["json_schema"]
        self.assertIs(out["strict"], False)
        self.assertNotIn("description", out)
        del fmt["strict"]
        self.assertNotIn("strict", self.convert(fmt)["response_format"]["json_schema"])

    def test_text_and_json_object_mapping(self):
        for kind in ("text", "json_object"):
            with self.subTest(kind=kind):
                chat = self.convert({"type": kind})
                self.assertEqual(P.build_upstream_body(chat)["response_format"], {"type": kind})

    def test_bad_or_absent_format_does_not_crash(self):
        for text in (None, "text", {}, {"format": None}, {"format": "json"},
                     {"format": {}}, {"format": {"type": "unknown"}}):
            with self.subTest(text=text):
                out = P.responses_to_chat({"model": "fixture", "input": "hi", "text": text})
                self.assertNotIn("response_format", out)

    def test_format_mapping_does_not_mutate_request(self):
        payload = {"model": "fixture", "input": "hi", "text": {"format": {
            "type": "json_schema", "name": "fixture", "schema": SCHEMA, "strict": True}}}
        original = copy.deepcopy(payload)
        P.build_upstream_body(P.responses_to_chat(payload))
        self.assertEqual(payload, original)

    def test_legacy_format_mapping_is_unchanged(self):
        with mock.patch.dict(os.environ, {FLAG: "0"}):
            self.assertNotIn("response_format", self.convert({"type": "json_object"}))

    def test_namespace_flat_strict_true_false_reaches_wire(self):
        for strict in (True, False):
            with self.subTest(strict=strict):
                chat = P.responses_to_chat({"model": "fixture", "input": "hi",
                                            "tools": self.namespace(strict)})
                wire = P.build_upstream_body(chat)
                self.assertIs(wire["tools"][0]["function"]["strict"], strict)

    def test_nested_namespace_strict_is_already_preserved(self):
        for value in ("0", "1"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {FLAG: value}):
                flat, mapping = P.expand_namespace_tools(self.namespace(False, nested=True))
                self.assertIs(flat[0]["strict"], False)
                self.assertEqual(mapping, {"lookup_fixture": "fixture"})

    def test_namespace_missing_strict_stays_missing(self):
        tools = self.namespace()
        del tools[0]["tools"][0]["strict"]
        self.assertNotIn("strict", P.expand_namespace_tools(tools)[0][0])

    def test_namespace_does_not_mutate_request(self):
        tools = self.namespace()
        before = copy.deepcopy(tools)
        P.expand_namespace_tools(tools)
        self.assertEqual(tools, before)

    def test_legacy_namespace_flat_strict_is_unchanged(self):
        with mock.patch.dict(os.environ, {FLAG: "0"}):
            self.assertNotIn("strict", P.expand_namespace_tools(self.namespace())[0][0])

    def test_tool_arguments_and_results_are_exact(self):
        messages = self.tool_turn()
        before = copy.deepcopy(messages)
        out = P.sanitize_messages(messages)
        self.assertEqual(out[1]["tool_calls"][0]["function"]["arguments"], DATA)
        self.assertEqual(out[2]["content"], DATA)
        self.assertEqual(json.loads(out[2]["content"])["code"], 11128)
        self.assertEqual(messages, before)

    def test_tool_turn_wire_preserves_data(self):
        body = P.build_upstream_body({"model": "deepseek-v4.1-flash",
                                      "messages": self.tool_turn()})
        assistant = next(m for m in body["messages"] if m.get("tool_calls"))
        tool = next(m for m in body["messages"] if m.get("role") == "tool")
        self.assertEqual(assistant["tool_calls"][0]["function"]["arguments"], DATA)
        self.assertEqual(tool["content"], DATA)

    def test_legacy_function_result_preserved(self):
        out = P.sanitize_messages([{"role": "function", "name": "fixture", "content": DATA}])
        self.assertEqual(out[0]["content"], DATA)

    def test_tool_content_parts_preserved(self):
        parts = [{"type": "text", "text": DATA}]
        self.assertEqual(P.sanitize_messages([{"role": "tool", "content": parts}])[0]["content"], parts)

    def test_other_role_fingerprint_rules_are_unchanged(self):
        for role in ("system", "user", "assistant"):
            with self.subTest(role=role):
                out = P.sanitize_messages([{"role": role, "content": "11128"}])
                self.assertEqual(out[0]["content"], "11-128")

    def test_legacy_tool_sanitization_is_unchanged(self):
        with mock.patch.dict(os.environ, {FLAG: "0"}):
            out = P.sanitize_messages(self.tool_turn())
            self.assertEqual(out[2]["content"], DATA.replace("11128", "11-128"))
            self.assertEqual(out[1]["tool_calls"][0]["function"]["arguments"],
                             DATA.replace("11128", "11-128"))

    def test_malformed_tool_calls_remain_tolerated(self):
        for calls in (None, [], "fixture", [None, {}]):
            self.assertEqual(P.sanitize_tool_calls(calls), calls)


if __name__ == "__main__":
    unittest.main()
