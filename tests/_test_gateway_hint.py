"""Pin the gateway_hint contract: an additive note beside the upstream error.

The hint must never replace or rewrite the upstream message (clients keep
parsing the same envelope), must only appear when the gateway actually
classified the failure, and unknown failures must stay hint-less rather than
guess. Run with the bundled Python:

    python tests/_test_gateway_hint.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy as proxy


class HintClassificationTests(unittest.TestCase):
    def test_known_shapes_get_a_hint(self):
        cases = [
            (503, "concurrent chat limit reached", "concurrency"),
            (400, "11133 model_param_invalid: bad field", "parameters"),
            (400, "11135 invalid_image_data", "image"),
            (503, "no usable account for realm 'intl'", "no healthy account"),
            (400, "maximum context length exceeded", "context"),
            (429, "rate limit reached for model", "rate limited"),
            (402, "insufficient credit balance", "credits"),
            (401, "session not found, please login", "session expired"),
            (400, "content rejected by policy", "content policy"),
            (404, "no such model: foo-bar", "no such model"),
            (403, "request blocked by WAF", "WAF"),
        ]
        for status, message, needle in cases:
            hint = proxy.gateway_hint(status, message)
            self.assertIn(needle, hint, (status, message, hint))

    def test_unknown_failures_get_no_hint(self):
        for status, message in ((500, "internal error"), (400, ""),
                                (400, "something we have never seen")):
            self.assertEqual("", proxy.gateway_hint(status, message),
                             (status, message))

    def test_hint_never_replaces_the_message(self):
        # The hint is additive; the upstream wording stays verbatim.
        message = "rate limit reached for model deepseek-v4.1-flash"
        self.assertEqual(message, str(message))
        hint = proxy.gateway_hint(429, message)
        self.assertTrue(hint)
        self.assertNotIn(hint, message)


class _Stub(object):
    _error = proxy.Handler._error

    def __init__(self):
        self.status = None
        self.payload = None

    def _handle_expect_continue(self):
        pass

    def _discard_body(self):
        pass

    def _json(self, code, obj):
        self.status = code
        self.payload = obj
        return code, obj


class ErrorEnvelopeTests(unittest.TestCase):
    def test_envelope_carries_the_hint_when_present(self):
        stub = _Stub()
        stub._error(429, "rate limit reached for model x", "rate_limit_error")
        self.assertEqual(429, stub.status)
        error = stub.payload["error"]
        self.assertEqual("rate limit reached for model x", error["message"])
        self.assertEqual("rate_limit_error", error["type"])
        self.assertEqual(429, error["code"])
        self.assertEqual("rate limited by upstream; retry after reset",
                         error["gateway_hint"])

    def test_envelope_omits_the_hint_when_unknown(self):
        stub = _Stub()
        stub._error(500, "internal error")
        error = stub.payload["error"]
        self.assertNotIn("gateway_hint", error)
        self.assertEqual("internal error", error["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
