"""GET /pricing belongs to the panel boundary, not the API-key boundary.

    python tests/_test_pricing_panel_auth.py

/pricing is the dashboard's read side for the pricing feature: it returns the
current policy document, the gap/mapping state, the pricing log and the resolved
project-local pricing file paths. None of that is part of the OpenAI-compatible
client surface, so an API key has no reason to reach it - which is exactly the
boundary the write side already uses for POST /pricing/refresh and
POST /pricing/mapping.

These tests drive the real do_GET dispatcher through the four credential tiers
the boundary has to tell apart: nothing at all, a valid API key, a wrong panel
token, and a real panel session. /v1/models is checked alongside them because it
is the endpoint that must *stay* readable with an API key alone.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _isolated_dirs import isolated_data_dirs  # noqa: E402  (its own data root)

import wb_proxy as proxy          # noqa: E402

import wb_models as MC  # noqa: E402
import wb_settings as settings    # noqa: E402

API_KEY = {"name": "client key", "key": "sk-client-abc", "enabled": True}

#: What PRICING.status() hands back; the panel must see exactly this.
PRICING_DOCUMENT = {
    "interval_minutes": 30.0,
    "enabled": True,
    "running": False,
    "policies": 3,
    "models": 7,
    "logs": [{"at": "2026-01-01 00:00:00", "msg": "refreshed"}],
    "gaps": [],
    "overrides_file": "/srv/workbuddy/pricing_overrides.json",
    "policies_file": "/srv/workbuddy/pricing_policies.json",
    "timeline": "/srv/workbuddy/pricing_timeline.json",
}


class FakePricing(object):
    def status(self):
        return dict(PRICING_DOCUMENT)


class Request(object):
    """Enough of a request object to run the real dispatcher.

    The boundary under test lives in do_GET/do_POST, so the methods are borrowed
    from the real Handler rather than re-implemented: only the response sink and
    the two socket helpers _error() needs are replaced.
    """

    _is_panel_route = staticmethod(proxy.Handler._is_panel_route)
    _panel_token = proxy.Handler._panel_token
    _panel_ok = proxy.Handler._panel_ok
    _key_ok = proxy.Handler._key_ok
    _key_realm = proxy.Handler._key_realm
    _authorized = proxy.Handler._authorized
    _supplied_key = proxy.Handler._supplied_key
    _request_realm = proxy.Handler._request_realm
    _strip_pinned_prefix_only = proxy.Handler._strip_pinned_prefix_only
    _apply_pinned_route = proxy.Handler._apply_pinned_route
    _error = proxy.Handler._error
    _get_pricing = proxy.Handler._get_pricing
    _get_v1_models = proxy.Handler._get_v1_models
    do_GET = proxy.Handler.do_GET
    do_POST = proxy.Handler.do_POST

    def __init__(self, path, headers=None):
        self.path = path
        self.headers = dict(headers or {})
        self.key_entry = None
        self.status = None
        self.body = None

    # A test request has no socket behind it.
    def _handle_expect_continue(self):
        pass

    def _discard_body(self):
        pass

    def _json(self, code, obj):
        self.status, self.body = code, obj


class PanelRouteBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="panel-route-")
        self.addCleanup(self._cleanup)
        patches = [
            mock.patch.multiple(proxy, ACCOUNTS_DIR=self.directory,
                                API_KEY=None, POOL=None),
            mock.patch.object(proxy, "PRICING", FakePricing()),
            mock.patch.object(proxy, "fetch_models",
                              lambda realm=None: [("gpt-4o", {})]),
            mock.patch.object(proxy.wb_modelsdev, "refresh_async",
                              lambda *a, **k: None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        # A configured, enabled key is what puts the deployment in
        # "auth required" mode, so the anonymous tier really is anonymous.
        settings.set_api_keys(self.directory, [API_KEY])

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.directory, ignore_errors=True)

    def request(self, path, key=None, panel=None):
        headers = {}
        if key:
            headers["Authorization"] = "Bearer " + key
        if panel:
            headers["X-Panel-Token"] = panel
        return Request(path, headers)

    def assert_panel_rejected(self, request):
        """401 from the panel boundary - not from the API-key check.

        The distinction is the whole point: a *valid* API key must still be
        turned away here, so the message has to name the panel, not the key.
        """
        request.do_GET()
        self.assertIsNotNone(request.body, "请求没有被派发处理")
        self.assertEqual(request.status, 401)
        message = request.body["error"]["message"]
        self.assertIn("panel password required", message)
        self.assertNotIn("api key", message)

    def test_anonymous_cannot_read_pricing(self):
        self.assert_panel_rejected(self.request("/pricing"))

    def test_a_valid_api_key_alone_cannot_read_pricing(self):
        self.assert_panel_rejected(self.request("/pricing", key=API_KEY["key"]))

    def test_a_wrong_panel_token_cannot_read_pricing(self):
        self.assert_panel_rejected(self.request("/pricing",
                                                panel="not-a-real-session"))

    def test_a_panel_session_still_reads_the_pricing_document(self):
        request = self.request("/pricing", panel=proxy.PANEL.create())
        request.do_GET()
        self.assertEqual(request.status, 200)
        self.assertEqual(request.body, PRICING_DOCUMENT)

    def test_a_panel_session_can_read_pricing_without_any_api_key(self):
        """The panel keeps working exactly as before - that is the other half."""
        request = self.request("/pricing", panel=proxy.PANEL.create())
        request.do_GET()
        self.assertEqual(request.status, 200)
        self.assertEqual(request.body["policies"], PRICING_DOCUMENT["policies"])

    def test_v1_models_stays_readable_with_an_api_key_alone(self):
        request = self.request("/v1/models", key=API_KEY["key"])
        request.do_GET()
        self.assertEqual(request.status, 200)
        self.assertEqual([entry["id"] for entry in request.body["data"]],
                         ["gpt-4o"])

    def test_v1_models_is_not_part_of_the_panel_boundary(self):
        request = self.request("/v1/models")
        request.do_GET()
        self.assertEqual(request.status, 401)
        self.assertIn("api key", request.body["error"]["message"])

    def test_pricing_writes_stay_panel_only(self):
        for path in ("/pricing/refresh", "/pricing/mapping"):
            with self.subTest(path=path):
                request = self.request(path, key=API_KEY["key"])
                request.do_POST()
                self.assertEqual(request.status, 401)
                self.assertIn("panel password required",
                              request.body["error"]["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
