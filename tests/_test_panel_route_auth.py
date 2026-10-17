"""Management routes answer to the panel session, not to an API key.

A management endpoint drawn in the web panel is reached with a panel session.
An API key is the credential for the model APIs; holding one is not a substitute
for the panel password. The two boundaries are separate, and the dispatcher is
where they are enforced - which is exactly why the two halves of one surface can
drift apart. The update-status read used to answer to `_authorized()` while
POST /updates/check was already panel-only, so a caller holding nothing but a
key could read the running version and the release status.

This suite drives the real do_GET/do_POST over an in-memory socket, so what it
pins is the dispatcher's behaviour rather than any handler's internals, and it
covers the management surface as a whole rather than the one route that was
fixed. Every route in the tables below must give the same four answers: an
anonymous caller is refused, an API key alone is refused, a wrong panel token
is refused, and a real panel session gets through.

The pricing table is the other half of that story: its writes were always
panel-only while the read answered to `_authorized()`, so an API key alone
could read the estimate and the resolved project-local file paths behind it.
The read is on the panel boundary now, and it is covered below like the rest.

/agents is in the tables for a different reason: that read carries no check of
its own, neither key nor session, and is protected only by the dispatcher's
prefix. The contract is the boundary the dispatcher enforces, not the check a
handler remembers to add.

No network: the model listing and the release checker are both injected.

    python tests/_test_panel_route_auth.py
"""
import email
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _isolated_dirs import isolated_data_dirs  # noqa: E402  (its own data root)

_TMP = isolated_data_dirs("wb-panel-route-auth-")

import wb_proxy as proxy  # noqa: E402
import wb_settings  # noqa: E402

KEY = "panel-route-key"
PANEL_ONLY_MESSAGE = "panel password required"
MODEL_ID = "injected-panel-route-model"
LATEST = "1.6.18"


def injected_models(realm=None):
    """The model listing, without walking anything upstream."""
    return [(MODEL_ID, {"name": "Injected"})]


class StubUpdates(object):
    """Stands in for the release checker: the boundary is what is under test."""

    def __init__(self):
        self.manual_checks = 0
        self.last = {"current_version": proxy.running_version(),
                     "latest_version": None, "update_available": False}

    def check(self, manual=False):
        self.manual_checks += 1
        self.last = {"current_version": proxy.running_version(),
                     "latest_version": LATEST, "update_available": True}
        return dict(self.last)

    def status(self):
        return dict(self.last)


class DispatchRequest(proxy.Handler):
    """A real Handler driven over an in-memory socket.

    Which boundary refuses a management route is a property of the dispatcher,
    not of the endpoint method, so the request has to go through the real
    do_GET/do_POST for the test to mean anything.
    """

    def __init__(self, path, headers=None, command="GET", body=b""):
        self.path = path
        self.command = command
        self.request_version = "HTTP/1.1"
        self.close_connection = True
        raw = "".join("%s: %s\r\n" % item for item in (headers or {}).items())
        self.headers = email.message_from_string(raw)
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = None
        # 内存 socket 的对端就是回环；一键配置的可用性判定读的正是这个地址
        # （issue #246），远程那一侧在 RemoteAgentsTests 里改写它。
        self.client_address = ("127.0.0.1", 0)

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass

    def log_message(self, fmt, *args):
        pass

    def dispatch(self):
        """Run the real dispatcher and hand back status, JSON body, raw body."""
        getattr(self, "do_" + self.command)()
        raw = self.wfile.getvalue().decode("utf-8")
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None      # the log export is plain text, not a document
        return self.status, payload, raw


class BoundaryCase(unittest.TestCase):
    """One management surface, one isolated settings directory per test."""

    # Management reads the panel draws, none of which is a model API.
    PANEL_ONLY_GET = ("/settings", "/updates", "/logs", "/tasks", "/scheduler",
                      "/accounts", "/usage", "/pricing", "/proxy/slots",
                      "/activity/history", "/agents", "/accounts/credits/grants")
    # The same boundary on a reply that is a file rather than a document.
    PANEL_ONLY_DOWNLOAD = ("/logs/export",)
    # The management writes, each guarded by its own panel check in do_POST.
    # /updates/check is the half of the update pair that was already correct;
    # the two pricing writes are the ones the panel's own buttons post to.
    PANEL_ONLY_POST = ("/updates/check", "/pricing/refresh",
                       "/pricing/mapping", "/settings/save",
                       "/catalog/refresh")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="panel-route-")
        self.addCleanup(self._tmp.cleanup)
        self.directory = self._tmp.name
        wb_settings.set_api_keys(self.directory, [
            {"id": "k1", "name": "k", "key": KEY, "enabled": True}])
        self.updates = StubUpdates()

    def dispatch(self, path, headers=None, command="GET", body=b"", pool=None):
        request = DispatchRequest(path, headers=headers, command=command, body=body)
        with mock.patch.multiple(proxy, ACCOUNTS_DIR=self.directory, API_KEY=None,
                                 POOL=pool, PRICING=None, UPDATES=self.updates,
                                 SCHEDULER=None, fetch_models=injected_models):
            return request.dispatch()

    def assert_refused(self, path, headers, command="GET", body=b""):
        status, payload, _raw = self.dispatch(path, headers, command, body)
        self.assertEqual(status, 401, "%s %s answered %s" % (command, path, status))
        self.assertIn(PANEL_ONLY_MESSAGE, payload["error"]["message"])
        # The refusal is the whole reply: nothing the route would have reported
        # may ride along with it.
        self.assertEqual(list(payload), ["error"],
                         "%s %s leaked a payload with its refusal" % (command, path))
        return payload

    def assert_reached(self, path, command="GET", body=b""):
        status, payload, raw = self.dispatch(path, self.session(), command, body)
        self.assertEqual(status, 200, "%s %s answered %s" % (command, path, status))
        self.assertNotIn(PANEL_ONLY_MESSAGE, raw)
        if payload is not None:
            self.assertNotIn("error", payload)
        return payload

    def session(self):
        return {"X-Panel-Token": proxy.PANEL.create()}

    def key_only(self):
        return {"Authorization": "Bearer " + KEY}


class PanelOnlyGetTests(BoundaryCase):
    """Every management read is closed to everything but a panel session."""

    def test_an_anonymous_caller_is_refused(self):
        for path in self.PANEL_ONLY_GET:
            with self.subTest(path=path):
                self.assert_refused(path, {})

    def test_an_api_key_alone_is_refused(self):
        for path in self.PANEL_ONLY_GET:
            with self.subTest(path=path):
                self.assert_refused(path, self.key_only())

    def test_a_wrong_panel_token_is_refused(self):
        for path in self.PANEL_ONLY_GET:
            with self.subTest(path=path):
                self.assert_refused(path, {"X-Panel-Token": "not-a-session"})

    def test_a_panel_session_reaches_every_route(self):
        for path in self.PANEL_ONLY_GET:
            with self.subTest(path=path):
                self.assert_reached(path)

    def test_the_log_export_is_on_the_same_boundary(self):
        # A reply that is a file rather than a document still carries the same
        # four answers.
        path = self.PANEL_ONLY_DOWNLOAD[0]
        self.assert_refused(path, {})
        self.assert_refused(path, self.key_only())
        self.assert_refused(path, {"X-Panel-Token": "not-a-session"})
        self.assert_reached(path)

    def test_the_update_status_is_not_readable_with_a_key(self):
        # The incident this contract comes from, asserted on the payload rather
        # than the code: the release status must not appear in a refusal.
        payload = self.assert_refused("/updates", self.key_only())
        self.assertNotIn("current_version", json.dumps(payload))
        body = self.assert_reached("/updates")
        self.assertEqual(body["current_version"], proxy.running_version())


class PanelOnlyPostTests(BoundaryCase):
    """The management writes are on the same boundary, not a special case."""

    def test_an_anonymous_caller_is_refused(self):
        for path in self.PANEL_ONLY_POST:
            with self.subTest(path=path):
                self.assert_refused(path, {}, command="POST", body=b"{}")

    def test_an_api_key_alone_is_refused(self):
        for path in self.PANEL_ONLY_POST:
            with self.subTest(path=path):
                self.assert_refused(path, self.key_only(), command="POST", body=b"{}")

    def test_a_wrong_panel_token_is_refused(self):
        for path in self.PANEL_ONLY_POST:
            with self.subTest(path=path):
                self.assert_refused(path, {"X-Panel-Token": "not-a-session"},
                                    command="POST", body=b"{}")

    def test_a_panel_session_reaches_every_route(self):
        for path in self.PANEL_ONLY_POST:
            with self.subTest(path=path):
                self.assert_reached(path, command="POST", body=b"{}")

    def test_the_manual_update_check_is_panel_only_too(self):
        # The other half of the update pair. It takes no body, so it is also the
        # case that proves the refusal happens before any body is read.
        self.assert_refused("/updates/check", self.key_only(), command="POST")
        body = self.assert_reached("/updates/check", command="POST")
        self.assertIs(body["ok"], True)
        self.assertEqual(body["latest_version"], LATEST)
        self.assertEqual(self.updates.manual_checks, 1,
                         "only the session may spend the check")


class RemoteAgentsTests(BoundaryCase):
    """一键配置只在「浏览器和网关同一台机器」时可用（issue #246）。

    判据是请求来源地址：远程看板（手机 / 另一台电脑 / 容器端口映射）即使拿着
    面板会话，也读不到客户端清单、更写不了网关所在机器的客户端配置。环境标记
    （容器 / OpenWrt）另算一道，命中时连回环来源也不放行。
    """

    def dispatch_remote(self, path, headers=None, command="GET", body=b"",
                        deployment=None):
        request = DispatchRequest(path, headers=headers, command=command, body=body)
        request.client_address = ("192.168.1.20", 40000)
        with mock.patch.multiple(proxy, ACCOUNTS_DIR=self.directory, API_KEY=None,
                                 POOL=None, PRICING=None, UPDATES=self.updates,
                                 SCHEDULER=None, fetch_models=injected_models,
                                 _SERVER_DEPLOYMENT=deployment):
            return request.dispatch()

    def test_a_remote_panel_reads_enabled_false(self):
        status, payload, _raw = self.dispatch_remote("/agents", self.session())
        self.assertEqual(status, 200)
        self.assertIs(payload.get("enabled"), False, payload)
        self.assertNotIn("clients", payload)
        self.assertNotIn("models", payload)

    def test_a_remote_panel_cannot_apply_or_restore(self):
        for path in ("/agents/apply", "/agents/restore"):
            with self.subTest(path=path):
                status, payload, _raw = self.dispatch_remote(
                    path, self.session(), command="POST",
                    body=b'{"client": "claude-code"}')
                self.assertEqual(status, 403, payload)
                self.assertIn("only available from the machine",
                              payload["error"]["message"])

    def test_the_capability_probe_answers_for_both_sides(self):
        # 看板启动时问的就是它：本机 true、远程 false，两侧都不 import
        # wb_agents、不做任何探测。
        request = DispatchRequest("/agents/available", headers=self.session())
        with mock.patch.multiple(proxy, ACCOUNTS_DIR=self.directory, API_KEY=None,
                                 POOL=None, PRICING=None, UPDATES=self.updates,
                                 SCHEDULER=None, fetch_models=injected_models,
                                 _SERVER_DEPLOYMENT=False):
            status, payload, _raw = request.dispatch()
        self.assertEqual((status, payload), (200, {"enabled": True}))
        status, payload, _raw = self.dispatch_remote("/agents/available",
                                                    self.session(),
                                                    deployment=False)
        self.assertEqual((status, payload), (200, {"enabled": False}))

    def test_a_container_marker_disables_it_even_from_loopback(self):
        # --network host 的容器里，来源就是 127.0.0.1，但写进去的是容器里的
        # 配置目录；环境标记必须压过来源判定。
        request = DispatchRequest("/agents/available", headers=self.session())
        with mock.patch.multiple(proxy, ACCOUNTS_DIR=self.directory, API_KEY=None,
                                 POOL=None, PRICING=None, UPDATES=self.updates,
                                 SCHEDULER=None, fetch_models=injected_models,
                                 _SERVER_DEPLOYMENT=True):
            status, payload, _raw = request.dispatch()
        self.assertEqual((status, payload), (200, {"enabled": False}))


class ApiKeySurfaceTests(BoundaryCase):
    """The key boundary is unchanged, and it is a different check."""

    def test_an_api_key_still_reaches_the_model_listing(self):
        status, body, _raw = self.dispatch("/v1/models", self.key_only())
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in body["data"]], [MODEL_ID])

    def test_an_anonymous_api_call_is_refused_as_a_key_problem(self):
        # Not the panel message: the model APIs never moved onto the session
        # boundary, and a caller who forgot a key must be told that.
        status, body, _raw = self.dispatch("/v1/models")
        self.assertEqual(status, 401)
        self.assertIn("invalid api key", body["error"]["message"])
        self.assertNotIn(PANEL_ONLY_MESSAGE, body["error"]["message"])

    def test_a_panel_session_also_satisfies_the_key_check(self):
        # Why the panel boundary cannot be `_authorized()`: a session passes that
        # check too, so it cannot tell a session from a key. This is the property
        # that made the update-status mistake possible, and it is documented
        # behaviour rather than something this suite introduces.
        status, _body, _raw = self.dispatch("/v1/models", self.session())
        self.assertEqual(status, 200)


class SummaryAccount(object):
    """Minimal account surface used by the cached balance aggregation."""

    def __init__(self, remain, realm="cn", enabled=True, updated_at=None):
        self.uid = "private-account-uid"
        self.nickname = "private-account-name"
        self.realm = realm
        self.enabled = enabled
        self.credits = None if remain is None else {
            "remain": remain,
            "updated_at": updated_at,
        }

    def fetch_credits(self):
        raise AssertionError("balance summary must use cached credit snapshots")


class SummaryPool(object):
    def __init__(self, accounts):
        self.accounts = accounts


class BalanceSummaryTests(BoundaryCase):
    """The compact balance read is key-accessible and does not leak accounts."""

    def test_anonymous_call_is_refused_as_a_key_problem(self):
        status, body, _raw = self.dispatch("/v1/balance-summary")
        self.assertEqual(status, 401)
        self.assertIn("invalid api key", body["error"]["message"])
        self.assertNotIn(PANEL_ONLY_MESSAGE, body["error"]["message"])

    def test_key_reads_only_the_bound_realm_and_never_refreshes_upstream(self):
        pool = SummaryPool([
            SummaryAccount(10.25, updated_at=100),
            SummaryAccount(2.5, updated_at=200),
            SummaryAccount(100, enabled=False),
            SummaryAccount(50, realm="intl"),
        ])
        headers = dict(self.key_only(), **{"X-Realm": "cn"})
        status, body, raw = self.dispatch("/v1/balance-summary", headers, pool=pool)
        self.assertEqual(status, 200)
        self.assertEqual(body, {
            "balance": 12.75,
            "unit": "credit",
            "realm": "cn",
            "accounts_enabled": 2,
            "accounts_with_credit_data": 2,
            "updated_at": 200.0,
        })
        self.assertNotIn("private-account-uid", raw)
        self.assertNotIn("private-account-name", raw)

    def test_unknown_credit_snapshots_do_not_become_zero(self):
        pool = SummaryPool([
            SummaryAccount(3),
            SummaryAccount(None),
        ])
        headers = dict(self.key_only(), **{"X-Realm": "cn"})
        status, body, _raw = self.dispatch(
            "/v1/balance-summary", headers, pool=pool)
        self.assertEqual(status, 200)
        self.assertEqual(body["balance"], 3.0)
        self.assertEqual(body["accounts_enabled"], 2)
        self.assertEqual(body["accounts_with_credit_data"], 1)

        status, body, _raw = self.dispatch(
            "/v1/balance-summary", headers,
            pool=SummaryPool([SummaryAccount(None)]))
        self.assertEqual(status, 200)
        self.assertIsNone(body["balance"])
        self.assertEqual(body["accounts_with_credit_data"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
