"""Dashboard security headers and CSP (M4 D3, stage 2 nonce policy).

Run with: python _test_security_headers.py
No upstream credentials or outbound network are used.
"""
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="security-headers-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INLINE_HANDLER = re.compile(
    r'\son(?:click|change|input|submit|keydown|keyup|blur|focus|scroll)\s*=\s*"')


class CapturingHandler(wb_proxy.Handler):
    def __init__(self):
        self.status = None
        self.headers_sent = []
        self.written = b""

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.headers_sent.append((key, value))

    def end_headers(self):
        pass

    class _WFile(object):
        def __init__(self, owner):
            self.owner = owner

        def write(self, data):
            self.owner.written += data

        def flush(self):
            pass

    @property
    def wfile(self):
        return CapturingHandler._WFile(self)

    def headers_dict(self):
        return {key.lower(): value for key, value in self.headers_sent}


def render(page_markup):
    with tempfile.TemporaryDirectory() as directory:
        page = os.path.join(directory, "dashboard.html")
        with open(page, "w", encoding="utf-8") as fh:
            fh.write(page_markup)
        handler = CapturingHandler()
        with mock.patch.object(wb_proxy, "DASHBOARD_HTML", page):
            handler._dashboard()
    return handler


class DashboardHeaderTests(unittest.TestCase):
    def test_dashboard_carries_the_hardening_headers(self):
        handler = render("<html><script>var a=1;</script>"
                         "<script>var b=2;</script></html>")
        self.assertEqual(handler.status, 200)
        headers = handler.headers_dict()
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(headers.get("x-frame-options"), "DENY")
        self.assertEqual(headers.get("referrer-policy"), "no-referrer")
        self.assertIn("geolocation=()", headers.get("permissions-policy", ""))
        csp = headers.get("content-security-policy", "")
        self.assertIn("default-src 'self'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertIn("base-uri 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)

    def test_json_responses_are_nosniff(self):
        handler = CapturingHandler()
        handler._json(200, {"ok": True})
        headers = handler.headers_dict()
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")
        self.assertIn("application/json", headers.get("content-type", ""))


class DashboardNonceTests(unittest.TestCase):
    def test_two_inline_scripts_get_a_shared_nonce(self):
        handler = render("<html><script>var a=1;</script>"
                         "<script>var b=2;</script></html>")
        body = handler.written.decode("utf-8")
        nonces = re.findall(r'<script nonce="([^"]+)">', body)
        self.assertEqual(len(nonces), 2)
        self.assertEqual(nonces[0], nonces[1])
        csp = handler.headers_dict().get("content-security-policy", "")
        script_src = [part for part in csp.split(";") if "script-src" in part][0]
        self.assertIn("'self'", script_src)
        self.assertIn("'nonce-%s'" % nonces[0], script_src)
        self.assertNotIn("'unsafe-inline'", script_src)

    def test_unexpected_markup_keeps_the_inline_fallback(self):
        handler = render("<html><script>var a=1;</script></html>")
        body = handler.written.decode("utf-8")
        self.assertNotIn("nonce=", body)
        csp = handler.headers_dict().get("content-security-policy", "")
        script_src = [part for part in csp.split(";") if "script-src" in part][0]
        self.assertIn("'unsafe-inline'", script_src)

    def test_real_dashboard_matches_the_nonce_contract(self):
        with open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8") as fh:
            markup = fh.read()
        self.assertEqual(markup.count("<script>"), 2)
        self.assertEqual(len(INLINE_HANDLER.findall(markup)), 0)


if __name__ == "__main__":
    unittest.main()