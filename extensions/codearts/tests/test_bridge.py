"""Wire tests: real Hub HTTP handlers against a controlled CodeArts endpoint."""
import base64
import concurrent.futures
import gzip
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from extensions.codearts.config import BridgeConfig
from extensions.codearts.client import CodeArtsClient
from extensions.codearts.bridge import install


class Backend(BaseHTTPRequestHandler):
    requests = []
    status = 200
    delay = 0
    stream_error = False

    def log_message(self, *args):
        pass

    def respond(self, status, body, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.requests.append((self.path, dict(self.headers), None))
        if self.path == "/redirect/v1/models":
            self.send_response(302)
            self.send_header("Location", "/credential-leak")
            self.end_headers()
            return
        if self.status != 200:
            return self.respond(self.status, b'{"error":{"message":"backend unavailable"}}')
        obj = {"data": [{"id": "GLM-5.2", "object": "model", "owned_by": "codearts"}]}
        if self.path == "/admin/api/overview":
            obj = {"service": "codearts2api", "stats": {"total": 1, "healthy": 1},
                   "accounts": [{"uid": "huawei-1", "nickname": "Huawei", "disabled": False}]}
        self.respond(200, json.dumps(obj).encode())

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        body = json.loads(raw)
        self.requests.append((self.path, dict(self.headers), body))
        if self.status != 200:
            return self.respond(self.status, b'{"error":{"message":"backend unavailable"}}')
        time.sleep(self.delay)
        if self.path == "/admin/api/oauth/start":
            return self.respond(200, b'{"ok":true,"session_id":"oauth-test","auth_url":"https://auth.example.test/authorize"}')
        if self.path.startswith("/admin/"):
            return self.respond(200, b'{"ok":true,"status":"pending"}')
        if self.stream_error:
            return self.respond(200, b'event: error\ndata: {"error":{"code":"CODEARTS_STREAM_ERROR","message":"upstream rejected"}}\n\ndata: [DONE]\n\n', "text/event-stream")
        model = body["model"]
        delta = {"content": "bridge-ok"}
        finish = "stop"
        if body.get("tools"):
            delta = {"tool_calls": [{"index": 0, "id": "call-1", "type": "function",
                      "function": {"name": "weather", "arguments": '{"city":"Taipei"}'}}]}
            finish = "tool_calls"
        chunks = [
            {"id": "chatcmpl-backend", "object": "chat.completion.chunk", "model": model,
             "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {"id": "chatcmpl-backend", "object": "chat.completion.chunk", "model": model,
             "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
            {"id": "chatcmpl-backend", "object": "chat.completion.chunk", "model": model,
             "choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}
        ]
        wire = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
        self.respond(200, wire.encode(), "text/event-stream")


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = BridgeConfig(Path(self.tmp.name) / "config.json")

    def test_new_install_is_disabled(self):
        self.assertFalse(self.config.view()["enabled"])
        self.assertEqual(self.config.view()["model_prefix"], "codeart/")
        with self.assertRaises(ValueError):
            self.config.ready()

    def test_saved_secret_never_appears_in_view_and_blank_preserves_it(self):
        self.config.update({"api_key": "secret-123", "base_url": "http://localhost:7866/v1/"})
        self.config.update({"api_key": "", "enabled": True})
        self.assertEqual(self.config.ready()["api_key"], "secret-123")
        self.assertEqual(self.config.view()["base_url"], "http://localhost:7866")
        self.assertNotIn("secret-123", json.dumps(self.config.view()))

    def test_clear_secret_is_explicit(self):
        self.config.update({"api_key": "old"})
        self.config.update({"clear_api_key": True})
        self.assertFalse(self.config.view()["has_api_key"])

    def test_environment_secret_is_not_persisted(self):
        with patch.dict(os.environ, {"CA_TEST_KEY": "env-only"}):
            self.config.update({"enabled": True, "api_key_env": "CA_TEST_KEY"})
            self.assertEqual(self.config.ready()["api_key"], "env-only")
            self.assertNotIn("env-only", self.config.path.read_text())

    def test_reject_unsafe_or_malformed_configuration(self):
        for payload in [{"enabled": "true"}, {"base_url": "file:///tmp/x"},
                        {"base_url": "http://user:pass@localhost"},
                        {"base_url": "http://localhost/?key=secret"},
                        {"timeout_seconds": 0}, {"timeout_seconds": True}, {"unknown": 1}]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.config.update(payload)


class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.accounts = Path(cls.tmp.name) / "accounts"
        cls.accounts.mkdir()
        cls.usage = Path(cls.tmp.name) / "usage"
        cls.usage.mkdir()
        os.environ["ACCOUNTS_DIR"] = str(cls.accounts)
        os.environ["WB_PROXY_USAGE_DIR"] = str(cls.usage)
        os.environ["WB_PROXY_CATALOG_DIR"] = str(Path(cls.tmp.name) / "catalog")
        import wb_proxy as hub
        cls.hub = hub
        cls.backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        threading.Thread(target=cls.backend.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.backend.server_port
        cls.config = BridgeConfig(cls.accounts / "codearts" / "bridge.json")
        cls.config.update({"enabled": True, "api_key": "backend-test-key", "base_url": cls.base})
        cls.runtime = install(hub, cls.config.path)
        hub.API_KEY = "gateway-test-key"
        hub.SYSTEM_PROMPT = "native-workbuddy-system"
        hub.POOL = hub.wb_accounts.AccountPool(str(cls.accounts), log=lambda *a, **kw: None)
        claims = base64.urlsafe_b64encode(json.dumps({"uid": "wb-test", "exp": int(time.time()) + 86400}).encode()).decode().rstrip("=")
        (cls.accounts / "native.json").write_text(json.dumps({"uid": "wb-test", "accessToken": "x." + claims + ".x",
            "enabled": True, "realm": "intl", "product": "workbuddy", "platform": "CLI",
            "expiresAt": int(time.time()) + 86400}), encoding="utf-8")
        hub.POOL.load()
        cls.native = hub.POOL.get("wb-test")
        cls.native.chat_base_url = lambda: cls.base
        cls.native.note_success = lambda **kw: None
        cls.native.fetch_credits = lambda *a, **kw: None
        hub._models_cache["intl"] = {"at": time.time(), "data": [("native-model", {})]}
        cls.server = hub.GatewayServer(("127.0.0.1", 0), hub.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.panel = hub.PANEL.create()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.backend.shutdown()
        cls.backend.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        Backend.requests.clear()
        Backend.status = 200
        Backend.delay = 0
        Backend.stream_error = False
        self.config.update({"enabled": True, "base_url": self.base, "api_key": "backend-test-key"})
        self.runtime.client.invalidate()
        self.hub.wb_settings.set_api_keys(str(self.accounts), [])

    def request(self, method, path, body=None, key="gateway-test-key", panel=False, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=8)
        h = {"Content-Type": "application/json"}
        if key:
            h["Authorization"] = "Bearer " + key
        if panel:
            h["X-Panel-Token"] = self.panel
        h.update(headers or {})
        conn.request(method, path, None if body is None else json.dumps(body), h)
        resp = conn.getresponse()
        raw = resp.read()
        result = (resp.status, dict(resp.getheaders()), raw)
        conn.close()
        return result

    def chat(self, **fields):
        body = {"model": "codearts/GLM-5.2", "messages": [{"role": "user", "content": "hello"}]}
        body.update(fields)
        return self.request("POST", "/v1/chat/completions", body)

    def test_chat_nonstream_keeps_alias_usage_and_message(self):
        messages = [{"role": "system", "content": "DeepSeek Harness precise identity"},
                    {"role": "user", "content": "hello"}]
        status, _, raw = self.chat(messages=messages)
        self.assertEqual(status, 200, raw)
        obj = json.loads(raw)
        self.assertEqual(obj["choices"][0]["message"]["content"], "bridge-ok")
        self.assertEqual(obj["model"], "codearts/GLM-5.2")
        self.assertEqual(obj["usage"]["total_tokens"], 18)
        path, headers, body = Backend.requests[-1]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(body["model"], "GLM-5.2")
        self.assertEqual(body["messages"], messages)
        self.assertEqual(headers["Authorization"], "Bearer backend-test-key")
        self.assertNotIn("X-User-Id", headers)

    def test_stream_keeps_alias_and_trailing_usage(self):
        status, _, raw = self.chat(stream=True, stream_options={"include_usage": True})
        self.assertEqual(status, 200, raw)
        frames = [json.loads(x[6:]) for x in raw.decode().splitlines() if x.startswith("data: {")]
        self.assertTrue(frames)
        self.assertTrue(all(f["model"] == "codearts/GLM-5.2" for f in frames))
        self.assertEqual(frames[-1]["choices"], [])
        self.assertEqual(frames[-1]["usage"]["total_tokens"], 18)
        self.assertTrue(raw.rstrip().endswith(b"data: [DONE]"))

    def test_function_calls_preserve_name_and_arguments(self):
        tools = [{"type": "function", "function": {"name": "weather", "parameters": {"type": "object"}}}]
        status, _, raw = self.chat(tools=tools)
        self.assertEqual(status, 200, raw)
        call = json.loads(raw)["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(call["function"], {"name": "weather", "arguments": '{"city":"Taipei"}'})

    def test_responses_reuses_native_translation(self):
        status, _, raw = self.request("POST", "/v1/responses", {"model": "codearts/GLM-5.2", "input": "hello"})
        self.assertEqual(status, 200, raw)
        obj = json.loads(raw)
        self.assertEqual(obj["object"], "response")
        self.assertEqual(obj["output"][0]["content"][0]["text"], "bridge-ok")

    def test_responses_stream_completes_instead_of_iteration_error(self):
        status, _, raw = self.request("POST", "/v1/responses", {"model": "codearts/GLM-5.2", "input": "hello", "stream": True})
        self.assertEqual(status, 200, raw)
        self.assertIn(b'event: response.completed', raw)
        self.assertIn(b'bridge-ok', raw)
        self.assertNotIn(b'not iterable', raw)

    def test_responses_function_tools_and_named_choice_use_chat_schema(self):
        status, _, raw = self.request("POST", "/v1/responses", {
            "model": "codearts/GLM-5.2", "input": "weather?",
            "tools": [{"type": "function", "name": "weather", "parameters": {"type": "object"}}],
            "tool_choice": {"type": "function", "name": "weather"}})
        self.assertEqual(status, 200, raw)
        body = Backend.requests[-1][2]
        self.assertEqual(body["tools"], [{"type": "function", "function": {"name": "weather", "parameters": {"type": "object"}}}])
        self.assertEqual(body["tool_choice"], {"type": "function", "function": {"name": "weather"}})

    def test_real_codearts_sse_error_is_never_completed_success(self):
        Backend.stream_error = True
        for route, payload in (("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}),
                               ("/v1/responses", {"input": "hi"})):
            for stream in (False, True):
                status, _, raw = self.request("POST", route, {"model": "codearts/GLM-5.2", "stream": stream, **payload})
                if not stream:
                    self.assertEqual(status, 502, raw)
                else:
                    self.assertIn(b'error', raw)
                    self.assertNotIn(b'event: response.completed', raw)
                row = json.loads(Path(self.hub.USAGE_LOG).read_text().splitlines()[-1])
                self.assertTrue(row.get("error"), row)
                self.assertNotEqual(row["outcome"], "completed")

    def test_pinned_workbuddy_route_cannot_access_codearts(self):
        status, _, raw = self.request("POST", "/u/wb-test/v1/chat/completions", {
            "model": "codearts/GLM-5.2", "messages": [{"role": "user", "content": "hi"}]})
        self.assertIn(status, (400, 403), raw)
        self.assertEqual(Backend.requests, [])

    def test_conversation_ids_are_isolated_between_gateway_keys(self):
        self.hub.wb_settings.set_api_keys(str(self.accounts), [
            {"id": "key-a", "key": "a", "enabled": True}, {"id": "key-b", "key": "b", "enabled": True}])
        for key in ("a", "b"):
            status, _, raw = self.request("POST", "/v1/chat/completions", {
                "model": "codearts/GLM-5.2", "conversation_id": "same-client-value",
                "messages": [{"role": "user", "content": "hi"}]}, key=key)
            self.assertEqual(status, 200, raw)
        self.assertNotEqual(Backend.requests[0][2]["conversation_id"], Backend.requests[1][2]["conversation_id"])

    def test_disabled_does_not_dispatch_or_fallback(self):
        self.config.update({"enabled": False})
        status, _, raw = self.chat()
        self.assertEqual(status, 503, raw)
        self.assertEqual(Backend.requests, [])

    def test_backend_error_is_returned_without_workbuddy_fallback(self):
        Backend.status = 429
        status, _, raw = self.chat()
        self.assertEqual(status, 429, raw)
        self.assertEqual(len(Backend.requests), 1)

    def test_gateway_auth_rejected_before_backend(self):
        status, _, _ = self.request("POST", "/v1/chat/completions", {"model": "codearts/GLM-5.2"}, key="wrong")
        self.assertEqual(status, 401)
        self.assertEqual(Backend.requests, [])

    def test_key_realm_account_and_model_restrictions(self):
        for restriction in ({"realm": "intl"}, {"accounts": ["wb-test"]}, {"models": ["native-*"]}):
            self.hub.wb_settings.set_api_keys(str(self.accounts), [{"id": "restricted", "key": "limited", "enabled": True, **restriction}])
            status, _, raw = self.request("POST", "/v1/chat/completions", {"model": "codearts/GLM-5.2", "messages": [{"role": "user", "content": "hi"}]}, key="limited")
            self.assertIn(status, (400, 403), raw)
        self.assertEqual(Backend.requests, [])

    def test_restricted_concurrency_uses_native_slots(self):
        self.hub.wb_settings.set_api_keys(str(self.accounts), [{"id": "one", "key": "limited", "enabled": True, "concurrency_limit": 1}])
        Backend.delay = 0.3
        body = {"model": "codearts/GLM-5.2", "messages": [{"role": "user", "content": "hi"}]}
        with concurrent.futures.ThreadPoolExecutor(2) as executor:
            jobs = [executor.submit(self.request, "POST", "/v1/chat/completions", body, "limited") for _ in range(2)]
        self.assertEqual(sorted(j.result()[0] for j in jobs), [200, 429])

    def test_catalog_merges_and_backend_outage_preserves_native_models(self):
        status, _, raw = self.request("GET", "/v1/models")
        self.assertEqual(status, 200, raw)
        self.assertTrue({"native-model", "codeart/GLM-5.2"}.issubset({m["id"] for m in json.loads(raw)["data"]}))
        Backend.status = 503
        self.runtime.client.invalidate()
        status, _, raw = self.request("GET", "/v1/models")
        self.assertEqual(status, 200, raw)
        self.assertIn("native-model", {m["id"] for m in json.loads(raw)["data"]})

    def test_codeart_prefix_and_legacy_alias_share_one_listener(self):
        for model in ("codeart/GLM-5.2", "codearts/GLM-5.2"):
            status, _, raw = self.chat(model=model)
            self.assertEqual(status, 200, raw)
            self.assertEqual(json.loads(raw)["model"], model)
            self.assertEqual(Backend.requests[-1][2]["model"], "GLM-5.2")
            row = json.loads(Path(self.hub.USAGE_LOG).read_text().splitlines()[-1])
            self.assertEqual(row["upstream_model"], "GLM-5.2")
            self.assertEqual(row["realm"], "codearts")

    def test_native_panel_and_agent_picker_include_namespaced_models(self):
        status, _, raw = self.request("GET", "/v1/models?realm=intl", panel=True)
        self.assertEqual(status, 200, raw)
        ids = {m["id"] for m in json.loads(raw)["data"]}
        self.assertIn("native-model", ids)
        self.assertIn("codeart/GLM-5.2", ids)
        handler = self.hub.Handler.__new__(self.hub.Handler)
        self.assertIn("codeart/GLM-5.2", {m["id"] for m in handler._agents_models()})

    def test_explicit_workbuddy_client_view_does_not_bypass_key_scope(self):
        status, _, raw = self.request("GET", "/v1/models?realm=intl")
        self.assertEqual(status, 200, raw)
        self.assertNotIn("codeart/GLM-5.2", {m["id"] for m in json.loads(raw)["data"]})

    def test_oauth_start_is_returned_through_same_panel_port(self):
        status, _, raw = self.request("POST", "/codearts/admin/oauth/start", {}, panel=True)
        self.assertEqual(status, 200, raw)
        obj = json.loads(raw)
        self.assertEqual(obj["auth_url"], "https://auth.example.test/authorize")
        self.assertEqual(obj["session_id"], "oauth-test")

    def test_native_request_uses_original_v2_path(self):
        status, _, raw = self.chat(model="native-model")
        self.assertEqual(status, 200, raw)
        self.assertEqual(Backend.requests[-1][0], "/v2/chat/completions")
        self.assertEqual(Backend.requests[-1][2]["messages"][0]["content"], "native-workbuddy-system")

    def test_concurrent_provider_rows_never_use_the_other_realm(self):
        with concurrent.futures.ThreadPoolExecutor(2) as executor:
            jobs = [executor.submit(self.chat, model=m) for m in ("native-model", "codearts/GLM-5.2")]
        for job in jobs:
            self.assertEqual(job.result()[0], 200, job.result()[2])
        rows = [json.loads(x) for x in Path(self.hub.USAGE_LOG).read_text().splitlines()][-2:]
        self.assertEqual({r["model"]: r["realm"] for r in rows}, {"native-model": "intl", "codearts/GLM-5.2": "codearts"})

    def test_management_requires_panel_session_and_masks_secrets(self):
        self.assertEqual(self.request("GET", "/codearts/config")[0], 401)
        status, _, raw = self.request("GET", "/codearts/config", panel=True)
        self.assertEqual(status, 200, raw)
        self.assertNotIn(b"backend-test-key", raw)
        self.assertTrue(json.loads(raw)["has_api_key"])

    def test_cross_origin_config_update_is_rejected(self):
        status, _, _ = self.request("POST", "/codearts/config", {"enabled": False}, panel=True, headers={"Origin": "https://evil.invalid"})
        self.assertEqual(status, 403)
        self.assertTrue(self.config.view()["enabled"])

    def test_management_allowlist_and_actions(self):
        self.assertEqual(self.request("POST", "/codearts/admin/not-a-route", {}, panel=True)[0], 404)
        status, _, raw = self.request("POST", "/codearts/admin/checkin", {"uid": "huawei-1"}, panel=True)
        self.assertEqual(status, 200, raw)
        self.assertEqual(Backend.requests[-1][0], "/admin/api/checkin")

    def test_redirect_does_not_forward_credentials(self):
        self.config.update({"base_url": self.base + "/redirect"})
        with self.assertRaises(Exception):
            CodeArtsClient(self.config).models()
        self.assertEqual([r[0] for r in Backend.requests], ["/redirect/v1/models"])

    def test_dashboard_injects_extension_without_cache_collision(self):
        status, headers, raw = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'/codearts/dashboard.js', raw)
        self.assertIn("codearts", headers.get("ETag", ""))
        status, _, raw = self.request("GET", "/codearts/dashboard.js")
        self.assertEqual(status, 200)
        self.assertNotIn(b"backend-test-key", raw)


if __name__ == "__main__":
    unittest.main()
