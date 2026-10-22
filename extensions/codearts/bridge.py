"""Guarded, process-local Hub adapters. Original launchers never import this file."""
from contextlib import contextmanager
import copy
import hashlib
import inspect
import json
from pathlib import Path
import threading
import urllib.error
from urllib.parse import parse_qs, urlsplit

from .client import ADMIN_ACTIONS, CodeArtsClient, PREFIX, is_codearts
from .config import BridgeConfig


class ServicePool:
    """An upstream service handle, never inserted into WorkBuddy's account pool."""
    uid = "codearts:pool"
    realm = "codearts"
    proxy = ""

    def __init__(self, client):
        self.client = client

    def chat_base_url(self):
        return self.client.config.snapshot()["base_url"]


class BridgeRuntime:
    def __init__(self, hub, path):
        self.hub = hub
        self.config = BridgeConfig(path)
        self.config.view()  # Invalid stored settings fail before the listener starts.
        self.client = CodeArtsClient(self.config)
        self.service_pool = ServicePool(self.client)
        self.local = threading.local()

    @contextmanager
    def request(self, handler):
        previous = getattr(self.local, "handler", None)
        self.local.handler = handler
        try:
            yield
        finally:
            self.local.handler = previous

    def open(self, payload, session_key=None, **kwargs):
        hub = self.hub
        try:
            self.config.ready()
        except ValueError as exc:
            raise hub.PinnedAccountError(503, str(exc)) from None
        if hub.pinned_account() is not None or kwargs.get("pin_account") not in (None, self.service_pool):
            raise hub.PinnedAccountError(403, "CodeArts cannot use a pinned WorkBuddy account")
        if kwargs.get("allowed_accounts") is not None:
            raise hub.PinnedAccountError(403, "WorkBuddy account-restricted keys cannot access the CodeArts service pool")
        handler = getattr(self.local, "handler", None)
        if handler and handler._key_realm():
            raise hub.PinnedAccountError(403, "WorkBuddy realm-bound keys cannot access CodeArts")
        conversation = payload.get("conversation_id") or session_key
        if conversation:
            scope = (handler._key_id() or "panel") if handler else "internal"
            conversation = "hub-ca-" + hashlib.sha256((scope + ":" + str(conversation)).encode()).hexdigest()
        response = self.client.chat(payload, conversation)
        return response, self.service_pool, payload.get("reasoning_effort")

    def summary(self):
        rows = []
        for line in self.hub._tail_lines(self.hub.USAGE_LOG, 5000):
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            if row.get("provider") == "codearts" or is_codearts(row.get("model")):
                rows.append(row)
        return {
            "sample_limit": 5000,
            "requests": len(rows),
            "errors": sum(bool(r.get("error")) for r in rows),
            "total_tokens": sum(int(r.get("total_tokens") or 0) for r in rows),
            "recent": list(reversed(rows[-20:])),
        }


def install(hub, config_path):
    """Install once before starting Hub; fail explicitly when its seams change."""
    if hasattr(hub, "_codearts_bridge"):
        raise RuntimeError("CodeArts bridge is already installed in this process")
    for name in ("Handler", "open_upstream", "open_upstream_with_fallback", "build_upstream_body", "_persist_usage", "_tail_lines"):
        if not callable(getattr(hub, name, None)):
            raise RuntimeError("incompatible Hub: missing " + name)
    if "allowed_accounts" not in inspect.signature(hub.open_upstream).parameters:
        raise RuntimeError("incompatible Hub: open_upstream account restrictions are unavailable")
    runtime = BridgeRuntime(hub, config_path)
    original_open = hub.open_upstream
    original_fallback = hub.open_upstream_with_fallback
    original_body = hub.build_upstream_body
    original_persist = hub._persist_usage
    native_handler = hub.Handler

    def open_upstream(payload, *args, **kwargs):
        if not is_codearts(payload.get("model")):
            return original_open(payload, *args, **kwargs)
        return runtime.open(payload, *args, **kwargs)

    def open_with_fallback(payload, *args, **kwargs):
        if not is_codearts(payload.get("model")):
            return original_fallback(payload, *args, **kwargs)
        return runtime.open(payload, *args, **kwargs)

    def build_body(payload):
        if not is_codearts(payload.get("model")):
            return original_body(payload)
        body = copy.deepcopy(payload)
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        return body

    def persist(row, fail_label):
        if is_codearts(row.get("model")):
            row["provider"] = "codearts"
            row["realm"] = "codearts"
            row["upstream_model"] = row["model"][len(PREFIX):]
            row["account"] = "codearts:pool"
        return original_persist(row, fail_label)

    class CodeArtsHandler(native_handler):
        def _cross_realm_error(self, model, realm):
            if not is_codearts(model):
                return super()._cross_realm_error(model, realm)
            if hub.pinned_account() is not None:
                return "CodeArts cannot use a pinned WorkBuddy account"
            explicit = super()._request_realm()
            if explicit and explicit not in ("codearts", "all"):
                return "CodeArts cannot use a WorkBuddy realm-bound key or explicit WorkBuddy exit"
            return ""

        def _dispatch_chat_post(self, path, payload):
            with runtime.request(self):
                return super()._dispatch_chat_post(path, payload)

        def _handle_responses(self, payload):
            with runtime.request(self):
                return super()._handle_responses(payload)

        def _handle_messages(self, payload):
            with runtime.request(self):
                return super()._handle_messages(payload)

        def _get_v1_models(self):
            if self._request_realm() == "codearts":
                if not self._authorized():
                    return
                if self._key_realm() or self._key_allowed_accounts() is not None or hub.pinned_account():
                    return self._error(403, "this key or pinned account cannot access CodeArts")
                try:
                    data = [m for m in runtime.client.models()
                            if hub.wb_settings.key_allows_model(self.key_entry, m["id"])]
                    return super()._json(200, {"object": "list", "data": data, "realm": "codearts"})
                except Exception as exc:
                    return self._bridge_error(exc)
            return super()._get_v1_models()

        def _json(self, code, obj):
            path = urlsplit(getattr(self, "path", "")).path
            explicit = self._request_realm() if path in ("/v1/models", "/models") else None
            if (code == 200 and path in ("/v1/models", "/models") and
                    isinstance(obj, dict) and isinstance(obj.get("data"), list) and
                    explicit in (None, "all") and not self._key_realm() and
                    self._key_allowed_accounts() is None and not hub.pinned_account()):
                try:
                    extra = runtime.client.models()
                except Exception as exc:
                    extra = []
                    runtime.client.last_error = type(exc).__name__
                known = {m.get("id") for m in obj["data"]}
                extra = [m for m in extra if m["id"] not in known and
                         hub.wb_settings.key_allows_model(self.key_entry, m["id"])]
                obj = {**obj, "data": obj["data"] + extra}
            return super()._json(code, obj)

        def _send_asset(self, body, content_type, etag=None):
            if etag and hub._if_none_match_hit(self.headers.get("If-None-Match", ""), etag):
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache" if etag else "no-store")
            if etag:
                self.send_header("ETag", etag)
            self.end_headers()
            self.wfile.write(body)

        def _dashboard(self):
            body = Path(hub.DASHBOARD_HTML).read_bytes()
            language = hub.wb_settings.ui_language(hub.ACCOUNTS_DIR)
            body = body.replace(b'data-ui-language="__WB_UI_LANGUAGE__"',
                                ('data-ui-language="%s"' % language).encode())
            # Capture before native early-paint validation discards an unknown
            # tab. The deferred extension later registers its own tab.
            early = (b'<script>try{window.__CODEARTS_REQUESTED__='
                     b'new URL(location.href).searchParams.get("tab")==="codearts"||'
                     b'(!new URL(location.href).searchParams.has("tab")&&'
                     b'localStorage.getItem("wb-proxy-main-tab")==="codearts");}catch(e){}</script>')
            body = body.replace(b"<head>", b"<head>" + early, 1)
            body = body.replace(b"</body>", b'<script src="/codearts/dashboard.js" defer></script></body>')
            etag = '"codearts-%s"' % hashlib.sha256(body).hexdigest()[:24]
            return self._send_asset(body, "text/html; charset=utf-8", etag)

        def _bridge_error(self, exc):
            status = exc.code if isinstance(exc, urllib.error.HTTPError) else (503 if isinstance(exc, ValueError) else 502)
            # Do not relay arbitrary backend error bodies or credential-bearing configuration.
            message = str(exc) if isinstance(exc, ValueError) else "CodeArts backend request failed (%s)" % status
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            return self._error(status, message, "invalid_request_error")

        def _same_origin(self):
            if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
                return False
            origin = self.headers.get("Origin")
            if not origin:
                return True  # Header-only panel token also supports local API clients.
            parsed = urlsplit(origin)
            return parsed.scheme in ("http", "https") and parsed.netloc.lower() == self.headers.get("Host", "").lower()

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/codearts/dashboard.js":
                body = (Path(__file__).parent / "dashboard.js").read_bytes()
                return self._send_asset(body, "text/javascript; charset=utf-8")
            if not path.startswith("/codearts/"):
                return super().do_GET()
            if not self._panel_ok():
                return self._error(401, "panel password required", "invalid_request_error")
            try:
                if path == "/codearts/config":
                    return self._json(200, runtime.config.view())
                if path == "/codearts/overview":
                    return self._json(200, runtime.client.overview())
                if path == "/codearts/models":
                    return self._json(200, {"data": runtime.client.models()})
                if path == "/codearts/usage":
                    return self._json(200, runtime.summary())
            except Exception as exc:
                return self._bridge_error(exc)
            return self._error(404, "not found", "invalid_request_error")

        def do_POST(self):
            path = urlsplit(self.path).path
            if not path.startswith("/codearts/"):
                return super().do_POST()
            if not self._panel_ok():
                return self._error(401, "panel password required", "invalid_request_error")
            if not self._same_origin():
                return self._error(403, "cross-origin CodeArts management is forbidden", "invalid_request_error")
            action = path[len("/codearts/admin/"):] if path.startswith("/codearts/admin/") else None
            if path not in ("/codearts/config", "/codearts/models/refresh") and action not in ADMIN_ACTIONS:
                return self._error(404, "not found", "invalid_request_error")
            payload = self._payload_or_error()
            if payload is None:
                return
            try:
                if path == "/codearts/config":
                    result = runtime.config.update(payload)
                    runtime.client.invalidate()
                elif path == "/codearts/models/refresh":
                    runtime.client.invalidate()
                    result = {"data": runtime.client.models()}
                else:
                    result = runtime.client.admin(action, payload)
                return self._json(200, result)
            except Exception as exc:
                return self._bridge_error(exc)

    hub.open_upstream = open_upstream
    hub.open_upstream_with_fallback = open_with_fallback
    hub.build_upstream_body = build_body
    hub._persist_usage = persist
    hub.Handler = CodeArtsHandler
    hub._codearts_bridge = runtime
    return runtime
