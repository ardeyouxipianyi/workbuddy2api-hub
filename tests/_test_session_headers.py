"""Session header family (B5) and gateway_hint error notes.

Run with: python _test_session_headers.py
No upstream credentials or outbound network are used.
"""
import json
import os
import sys
import tempfile
import unittest

_startup_dir = tempfile.TemporaryDirectory(prefix="session-headers-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_identity
import wb_proxy


def header_of(request, name):
    wanted = name.lower()
    for key, value in request.headers.items():
        if key.lower() == wanted:
            return value
    return None


class SessionIdTests(unittest.TestCase):
    def test_stable_request_id_is_derived_not_random(self):
        first = wb_identity.stable_request_id("conv-1:u0:hello")
        second = wb_identity.stable_request_id("conv-1:u0:hello")
        other = wb_identity.stable_request_id("conv-1:u0:other")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        for value in (first, other):
            self.assertEqual(len(value), 32)
            int(value, 16)

    def test_empty_key_stays_random(self):
        one = wb_identity.stable_request_id("")
        two = wb_identity.stable_request_id("")
        self.assertNotEqual(one, two)
        self.assertEqual(len(one), 32)

    def test_valid_trace_id(self):
        self.assertTrue(wb_identity.valid_trace_id("a" * 32))
        self.assertTrue(wb_identity.valid_trace_id("A" * 16))
        self.assertFalse(wb_identity.valid_trace_id("a" * 31))
        self.assertFalse(wb_identity.valid_trace_id("g" * 32))
        self.assertFalse(wb_identity.valid_trace_id(""))

    def test_identity_family_for_every_product(self):
        conv_req = "a" * 32
        for product in ("workbuddy", "vscode", "cli"):
            headers = wb_identity.build_identity_headers(
                product, "cn", "uid-1", "tok",
                conversation_id="conv-1",
                conversation_request_id=conv_req)
            self.assertEqual(headers["X-Conversation-Request-ID"], conv_req, product)
            self.assertEqual(headers["X-Root-Request-ID"], conv_req, product)
            self.assertEqual(headers["X-Trace-ID"], conv_req, product)
            self.assertEqual(headers["X-B3-TraceId"], conv_req, product)
            self.assertEqual(headers["X-B3-Sampled"], "1", product)
            self.assertEqual(headers["X-Conversation-ID"], "conv-1", product)
            message_id = headers["X-Conversation-Message-ID"]
            self.assertEqual(message_id, headers["X-Request-ID"], product)
            self.assertEqual(len(message_id), 32, product)
            self.assertEqual(headers["X-B3-SpanId"], message_id[:16], product)

    def test_conversation_id_is_not_fabricated_for_desktop(self):
        headers = wb_identity.build_identity_headers(
            "workbuddy", "cn", "uid-1", "tok",
            conversation_request_id="b" * 32)
        self.assertNotIn("X-Conversation-ID", headers)
        for product in ("vscode", "cli"):
            fabricated = wb_identity.build_identity_headers(
                product, "cn", "uid-1", "tok",
                conversation_request_id="b" * 32)
            self.assertIn("X-Conversation-ID", fabricated)
            self.assertEqual(len(fabricated["X-Conversation-ID"]), 36)

    def test_invalid_aggregation_id_falls_back_for_b3_only(self):
        headers = wb_identity.build_identity_headers(
            "cli", "cn", "uid-1", "tok",
            conversation_request_id="not-hex")
        self.assertEqual(headers["X-Conversation-Request-ID"], "not-hex")
        self.assertEqual(headers["X-B3-TraceId"], headers["X-Request-ID"])

    def test_inbound_trace_id_is_passed_through(self):
        headers = wb_identity.build_identity_headers(
            "cli", "cn", "uid-1", "tok",
            conversation_request_id="c" * 32,
            trace_id="client-trace")
        self.assertEqual(headers["X-Trace-ID"], "client-trace")
        self.assertEqual(headers["X-B3-TraceId"], "c" * 32)


class TurnKeyTests(unittest.TestCase):
    def test_last_user_message_wins(self):
        messages = [{"role": "user", "content": "first"},
                    {"role": "assistant", "content": "ok"},
                    {"role": "user", "content": "second"}]
        self.assertEqual(wb_proxy.turn_key_from_messages(messages), "u2:second")

    def test_same_text_at_a_different_index_is_a_different_turn(self):
        one = wb_proxy.turn_key_from_messages([{"role": "user", "content": "continue"}])
        two = wb_proxy.turn_key_from_messages([
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "continue"}])
        self.assertNotEqual(one, two)

    def test_multimodal_parts_are_signed_without_the_raw_data(self):
        key = wb_proxy.turn_key_from_messages([{"role": "user", "content": [
            {"type": "text", "text": "look at this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 5000}},
        ]}])
        self.assertIn("look at this", key)
        self.assertIn("[image_url:", key)
        self.assertNotIn("A" * 100, key)

    def test_no_user_message_has_no_key(self):
        self.assertEqual(wb_proxy.turn_key_from_messages(
            [{"role": "system", "content": "sys"}]), "")
        self.assertEqual(wb_proxy.turn_key_from_messages(
            [{"role": "user", "content": ""}]), "")


class SessionMetaTests(unittest.TestCase):
    def test_turn_key_is_stable_within_a_turn(self):
        payload = {"messages": [{"role": "user", "content": "hello"}]}
        one = wb_proxy.session_meta_for(payload, session_key="conv-1")
        two = wb_proxy.session_meta_for(payload, session_key="conv-1")
        self.assertEqual(one["conversation_request_id"],
                         two["conversation_request_id"])
        self.assertEqual(one["conversation_request_id"],
                         wb_identity.stable_request_id("conv-1:u0:hello"))
        self.assertEqual(len(one["conversation_request_id"]), 32)

    def test_next_user_message_changes_the_turn(self):
        first = wb_proxy.session_meta_for(
            {"messages": [{"role": "user", "content": "hello"}]},
            session_key="conv-1")
        second = wb_proxy.session_meta_for(
            {"messages": [{"role": "user", "content": "hello"},
                          {"role": "assistant", "content": "hi"},
                          {"role": "user", "content": "again"}]},
            session_key="conv-1")
        self.assertNotEqual(first["conversation_request_id"],
                            second["conversation_request_id"])

    def test_inbound_aggregation_id_wins(self):
        meta = wb_proxy.session_meta_for(
            {"messages": [{"role": "user", "content": "hello"}]},
            session_key="conv-1", inbound_request_id="client-supplied",
            trace_id="client-trace")
        self.assertEqual(meta["conversation_request_id"], "client-supplied")
        self.assertEqual(meta["trace_id"], "client-trace")

    def test_no_session_and_no_turn_is_random(self):
        payload = {"messages": [{"role": "assistant", "content": "hi"}]}
        one = wb_proxy.session_meta_for(payload)
        two = wb_proxy.session_meta_for(payload)
        self.assertNotEqual(one["conversation_request_id"],
                            two["conversation_request_id"])

    def test_resolve_conversation_id(self):
        self.assertEqual(wb_proxy.resolve_conversation_id(
            {"metadata": {"conversation_id": "meta-snake"}}), "meta-snake")
        self.assertEqual(wb_proxy.resolve_conversation_id(
            {"metadata": {"conversationId": "meta-camel"}}), "meta-camel")
        self.assertEqual(wb_proxy.resolve_conversation_id(
            {"conversation_id": "top-snake"}), "top-snake")
        self.assertEqual(wb_proxy.resolve_conversation_id(
            {"conversationId": "top-camel"}), "top-camel")
        self.assertEqual(wb_proxy.resolve_conversation_id({}), "")


class OpenUpstreamHeaderTests(unittest.TestCase):
    def make_pool(self, account):
        class Pool(object):
            accounts = [account]
            affinity = type("Affinity", (), {"unbind": lambda self, key: None})()

            def count_ready(self, realm, model=None):
                return 1

            def pick_for_session(self, realm, session_key=None, exclude=(),
                                 model=None):
                return account

            def list_public(self):
                return []

            def apply_daily_token_limit(self, value=None, usage=None):
                return value or 0

        return Pool()

    def test_retries_reuse_one_conversation_request_id(self):
        with tempfile.TemporaryDirectory() as directory:
            account = wb_accounts.Account(
                {"uid": "uid-b5", "accessToken": "t", "realm": "intl"},
                os.path.join(directory, "uid-b5.json"))
            captured = []

            class Response(object):
                def close(self):
                    pass

            def fake_urlopen(req, timeout=30, proxy=""):
                captured.append(req)
                return Response()

            old_pool, old_urlopen = wb_proxy.POOL, wb_accounts.urlopen
            wb_proxy.POOL = self.make_pool(account)
            wb_accounts.urlopen = fake_urlopen
            try:
                payload = {"model": "deepseek-v4.1-flash",
                           "messages": [{"role": "user", "content": "hello"}]}
                for _ in range(2):
                    upstream, _account = wb_proxy.open_upstream(
                        payload, session_key="conv-1", target_realm="intl")
                    upstream.close()
            finally:
                wb_proxy.POOL = old_pool
                wb_accounts.urlopen = old_urlopen

            self.assertEqual(len(captured), 2)
            first_id = header_of(captured[0], "X-Conversation-Request-ID")
            second_id = header_of(captured[1], "X-Conversation-Request-ID")
            self.assertEqual(first_id, second_id)
            self.assertEqual(first_id,
                             wb_identity.stable_request_id("conv-1:u0:hello"))
            self.assertNotEqual(
                header_of(captured[0], "X-Request-ID"),
                header_of(captured[1], "X-Request-ID"))
            self.assertEqual(header_of(captured[0], "X-Root-Request-ID"), first_id)
            self.assertEqual(header_of(captured[0], "X-B3-Sampled"), "1")

    def test_inbound_id_is_passed_through_to_the_upstream(self):
        with tempfile.TemporaryDirectory() as directory:
            account = wb_accounts.Account(
                {"uid": "uid-b5b", "accessToken": "t", "realm": "intl"},
                os.path.join(directory, "uid-b5b.json"))
            captured = []

            class Response(object):
                def close(self):
                    pass

            def fake_urlopen(req, timeout=30, proxy=""):
                captured.append(req)
                return Response()

            old_pool, old_urlopen = wb_proxy.POOL, wb_accounts.urlopen
            wb_proxy.POOL = self.make_pool(account)
            wb_accounts.urlopen = fake_urlopen
            try:
                upstream, _account = wb_proxy.open_upstream(
                    {"model": "deepseek-v4.1-flash",
                     "messages": [{"role": "user", "content": "hello"}]},
                    session_key="conv-1", target_realm="intl",
                    inbound_request_id="client-req-1", trace_id="client-trace-1")
                upstream.close()
            finally:
                wb_proxy.POOL = old_pool
                wb_accounts.urlopen = old_urlopen

            self.assertEqual(header_of(captured[0], "X-Conversation-Request-ID"),
                             "client-req-1")
            self.assertEqual(header_of(captured[0], "X-Trace-ID"), "client-trace-1")


class GatewayHintTests(unittest.TestCase):
    def test_model_param_invalid_family(self):
        hint = wb_proxy.gateway_hint(
            400, 'upstream 400: {"code":11133,"msg":"Invalid request parameters"}')
        self.assertIn("request parameters were rejected", hint)

    def test_image_data_family(self):
        hint = wb_proxy.gateway_hint(400, "code 11135 invalid_image_data")
        self.assertIn("image data rejected", hint)

    def test_no_healthy_account(self):
        hint = wb_proxy.gateway_hint(503, "no usable account for model X")
        self.assertIn("no healthy account", hint)

    def test_rate_limit(self):
        self.assertIn("rate limited", wb_proxy.gateway_hint(429, "too many requests"))

    def test_context_length(self):
        self.assertIn("context exceeds",
                      wb_proxy.gateway_hint(400, "maximum context length exceeded"))

    def test_unknown_shape_has_no_hint(self):
        self.assertEqual(wb_proxy.gateway_hint(500, "boom"), "")

    def test_error_response_carries_the_hint(self):
        class Handler(wb_proxy.Handler):
            def __init__(self):
                self.captured = None

            def _handle_expect_continue(self):
                pass

            def _discard_body(self):
                pass

            def _json(self, code, obj):
                self.captured = (code, obj)

        handler = Handler()
        handler._error(400, 'upstream 400: {"code":11133}')
        code, obj = handler.captured
        self.assertEqual(code, 400)
        self.assertIn("gateway_hint", obj["error"])

        handler._error(500, "boom")
        _code, obj = handler.captured
        self.assertNotIn("gateway_hint", obj["error"])


if __name__ == "__main__":
    unittest.main()
