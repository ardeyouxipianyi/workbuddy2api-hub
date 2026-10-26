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
    vision_reject = False
    force_406 = False

    def log_message(self, *args):
        pass

    def respond(self, status, body, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _has_image(body):
        for message in body.get("messages") or []:
            content = message.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and (
                            part.get("type") in ("image_url", "input_image", "image")
                            or "image_url" in part):
                        return True
        return False

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
        if self.path == "/v1/chat/completions" and (
                self.force_406 or (self.vision_reject and self._has_image(body))):
            return self.respond(400, b'{"error":{"code":"InferHub.001001020.406","message":"The request model is not multimodal"}}')
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

    def test_catalog_selection_is_exact_and_can_change_without_stale_cache(self):
        self.config.update({'enabled': True, 'api_key': 'test', 'catalog_models': ['GLM-5.2']})
        client = CodeArtsClient(self.config)
        data = {'data': [{'id': 'GLM-5.2'}, {'id': 'glm-5.2'}, {'id': 'Kimi-K3'}]}
        with patch.object(client, '_json', return_value=data):
            self.assertEqual([m['id'] for m in client.models()], ['codeart/GLM-5.2'])
            self.config.update({'catalog_models': ['Kimi-K3']})
            self.assertEqual([m['id'] for m in client.models()], ['codeart/Kimi-K3'])
            self.config.update({'catalog_models': []})
            self.assertEqual(len(client.models()), 3)
            self.config.update({'catalog_models': ['Not-Listed']})
            selected = client.models()
            self.assertEqual(selected[0]['id'], 'codeart/Not-Listed')
            self.assertFalse(selected[0]['backend_listed'])
            self.assertNotIn('context_length', selected[0])

    def test_image_catalog_requires_explicit_boolean_and_deduplicates_aliases(self):
        self.config.update({'enabled': True, 'api_key': 'test', 'catalog_models': ['GLM-5.2']})
        client = CodeArtsClient(self.config)
        data = {'data': [{'id': 'GLM-5.2'},
                         {'id': 'Vision Name', 'registered_id': 'vision-real', 'supports_images': True},
                         {'id': 'vision-real', 'registered_id': 'vision-real', 'supports_images': True},
                         {'id': 'text', 'supports_images': False}, {'id': 'unknown'},
                         {'id': 'string-true', 'supports_images': 'true'},
                         {'id': 'conflict', 'supports_images': True, 'capability_conflict': True}]}
        with patch.object(client, '_json', return_value=data):
            self.assertEqual([m['id'] for m in client.models()], ['codeart/GLM-5.2'])
            self.config.update({'catalog_include_images': True})
            models = client.models()
            self.assertEqual([m['id'] for m in models], ['codeart/GLM-5.2', 'codeart/vision-real'])
            self.assertTrue(models[-1]['supports_images'])
            self.config.update({'catalog_include_images': False})
            self.assertEqual(len(client.models()), 1)
        with self.assertRaises(ValueError):
            self.config.update({'catalog_include_images': 'true'})

    def test_catalog_deduplicates_registered_id_aliases(self):
        self.config.update({'enabled': True, 'api_key': 'test', 'catalog_models': []})
        client = CodeArtsClient(self.config)
        data = {'data': [
            {'id': 'deepseek-v4.1-flash', 'name': 'DeepSeek-V4.1-Flash', 'registered_id': 'deepseek-v4.1-flash'},
            {'id': 'DeepSeek-V4.1-Flash', 'name': 'DeepSeek-V4.1-Flash', 'registered_id': 'deepseek-v4.1-flash'},
            {'id': 'glm-5.2', 'name': 'GLM-5.2', 'registered_id': 'GLM-5.2'},
            {'id': 'GLM-5.2', 'name': 'GLM-5.2', 'registered_id': 'GLM-5.2'}]}
        with patch.object(client, '_json', return_value=data):
            models = client.models()
        self.assertEqual([m['id'] for m in models],
                         ['codeart/deepseek-v4.1-flash', 'codeart/GLM-5.2'])
        self.assertEqual(models[0]['name'], 'DeepSeek-V4.1-Flash')
        self.assertEqual(models[0]['registered_id'], 'deepseek-v4.1-flash')

    def test_catalog_selection_resolves_display_name(self):
        self.config.update({'enabled': True, 'api_key': 'test', 'catalog_models': ['DeepSeek-V4.1-Flash']})
        client = CodeArtsClient(self.config)
        data = {'data': [
            {'id': 'deepseek-v4.1-flash', 'name': 'DeepSeek-V4.1-Flash', 'registered_id': 'deepseek-v4.1-flash'},
            {'id': 'DeepSeek-V4.1-Flash', 'name': 'DeepSeek-V4.1-Flash', 'registered_id': 'deepseek-v4.1-flash'}]}
        with patch.object(client, '_json', return_value=data):
            selected = client.models()
        self.assertEqual([m['id'] for m in selected], ['codeart/deepseek-v4.1-flash'])
        # 命中后端条目而非占位：占位条 backend_listed 为 False 且带 availability_note
        self.assertTrue(selected[0].get('backend_listed', True))
        self.assertEqual(selected[0]['name'], 'DeepSeek-V4.1-Flash')

    def test_catalog_tolerates_non_string_registered_id(self):
        # 上游畸形数据：registered_id 为非字符串 truthy 时不得抛 TypeError，应回退到 id。
        self.config.update({'enabled': True, 'api_key': 'test', 'catalog_models': []})
        client = CodeArtsClient(self.config)
        data = {'data': [
            {'id': 'weird', 'registered_id': 123},
            {'id': 'listy', 'registered_id': ['x']},
            {'id': 'blanky', 'registered_id': ''}]}
        with patch.object(client, '_json', return_value=data):
            models = client.models()
        self.assertEqual([m['id'] for m in models],
                         ['codeart/weird', 'codeart/listy', 'codeart/blanky'])
        self.assertEqual(models[0]['registered_id'], 'weird')

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

    def test_vision_fallback_config_validation(self):
        for payload in [{"vision_fallback_model": "codeart/X"},
                        {"vision_fallback_model": "codearts/X"},
                        {"vision_fallback_model": ""},
                        {"vision_fallback_model": " X "},
                        {"vision_fallback_model": 123},
                        {"vision_fallback_enabled": "true"}]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.config.update(payload)
        view = self.config.view()
        self.assertFalse(view["vision_fallback_enabled"])  # 行为变更默认关（CONTRIBUTING 第 42 行）
        self.assertEqual(view["vision_fallback_model"], "deepseek-v4.1-flash")
        self.config.update({"vision_fallback_enabled": False, "vision_fallback_model": "other-model"})
        view = self.config.view()
        self.assertFalse(view["vision_fallback_enabled"])
        self.assertEqual(view["vision_fallback_model"], "other-model")


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
        Backend.vision_reject = False
        Backend.force_406 = False
        self.config.update({"enabled": True, "base_url": self.base, "api_key": "backend-test-key",
                            "vision_fallback_enabled": True, "vision_fallback_model": "deepseek-v4.1-flash"})
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

    def test_messages_stream_failure_keeps_terminal_error_after_heartbeat_close(self):
        Backend.stream_error = True
        with patch.object(self.hub, "_anthropic_messages_enabled", return_value=True):
            status, _, raw = self.request("POST", "/v1/messages", {
                "model": "codeart/GLM-5.2", "max_tokens": 32, "stream": True,
                "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(status, 200)
        self.assertIn(b"event: error", raw)
        self.assertNotIn(b"event: message_stop", raw)
        row = json.loads(Path(self.hub.USAGE_LOG).read_text().splitlines()[-1])
        self.assertEqual(row["outcome"], "upstream_aborted")

    def test_images_survive_chat_responses_and_anthropic_boundaries(self):
        url = 'data:image/png;base64,aGVsbG8='
        image = {'type': 'image_url', 'image_url': {'url': url, 'detail': 'high'}}
        cases = [('/v1/chat/completions', {'model': 'codeart/GLM-5.2', 'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'colour?'}, image]}]}),
                 ('/v1/responses', {'model': 'codeart/GLM-5.2', 'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'colour?'}, {'type': 'input_image', 'image_url': url, 'detail': 'high'}]}]}),
                 ('/v1/messages', {'model': 'codeart/GLM-5.2', 'max_tokens': 32, 'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'colour?'}, {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'aGVsbG8='}}]}]})]
        with patch.object(self.hub, '_anthropic_messages_enabled', return_value=True):
            for path, payload in cases:
                with self.subTest(path=path):
                    status, _, raw = self.request('POST', path, payload)
                    self.assertEqual(status, 200, raw)
                    body = [b for p, h, b in Backend.requests if p == '/v1/chat/completions'][-1]
                    self.assertEqual(body['model'], 'GLM-5.2')
                    parts = body['messages'][-1]['content']
                    got = next(p for p in parts if p.get('type') == 'image_url')
                    self.assertEqual(got['image_url']['url'], url)
                    if path != '/v1/messages':
                        self.assertEqual(got['image_url']['detail'], 'high')

    def test_unsupported_media_rejected_before_outbound(self):
        with patch.object(self.hub, '_anthropic_messages_enabled', return_value=True):
            for path in ('/v1/chat/completions', '/v1/responses', '/v1/messages'):
                content = [{'type': 'input_audio', 'input_audio': {'data': 'x', 'format': 'wav'}}]
                payload = {'model': 'codeart/GLM-5.2', 'max_tokens': 32}
                payload['input' if path.endswith('responses') else 'messages'] = [{'role': 'user', 'content': content}]
                before = len(Backend.requests)
                status, _, raw = self.request('POST', path, payload)
                self.assertEqual(status, 400, raw)
                self.assertEqual(len(Backend.requests), before)

    def test_responses_tool_output_media_is_validated(self):
        for kind in ('input_audio', 'input_video', 'input_file'):
            payload = {'model': 'codeart/GLM-5.2', 'input': [
                {'role': 'user', 'content': 'inspect result'},
                {'type': 'function_call', 'call_id': 'call-1', 'name': 'inspect', 'arguments': '{}'},
                {'type': 'function_call_output', 'call_id': 'call-1', 'output': [{'type': kind}]}]}
            before = len(Backend.requests)
            status, _, raw = self.request('POST', '/v1/responses', payload)
            self.assertEqual(status, 400, raw)
            self.assertEqual(len(Backend.requests), before)

    def test_terminal_heartbeat_fix_is_scoped_to_codearts(self):
        runtime = self.hub._codearts_bridge
        writes = []
        runtime.local.codearts_stream = True
        heartbeat = self.hub.SSEHeartbeat(writes.append, interval=3600)
        runtime.local.codearts_stream = False
        heartbeat.write(b'data: normal\n\n')
        heartbeat.close()
        heartbeat.write(b'data: after-close\n\n')
        heartbeat.write(b'event: error\ndata: {}\n\n')
        heartbeat.write(b'event: error\ndata: {}\n\n')
        self.assertEqual(len(writes), 2)
        self.assertTrue(writes[-1].startswith(b'event: error'))
        native = self.hub.SSEHeartbeat(writes.append, interval=3600)
        native.close()
        native.write(b'event: error\ndata: {}\n\n')
        self.assertEqual(len(writes), 2, 'WorkBuddy post-close behavior must stay unchanged')

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

    def test_vision_rejection_falls_back_with_header_and_usage_row(self):
        url = 'data:image/png;base64,aGVsbG8='
        image = {'type': 'image_url', 'image_url': {'url': url, 'detail': 'high'}}
        cases = [
            ('/v1/chat/completions', {'model': 'codeart/GLM-5.2', 'messages': [
                {'role': 'user', 'content': [{'type': 'text', 'text': 'colour?'}, image]}]}),
            ('/v1/responses', {'model': 'codeart/GLM-5.2', 'input': [
                {'role': 'user', 'content': [{'type': 'input_text', 'text': 'colour?'},
                                             {'type': 'input_image', 'image_url': url, 'detail': 'high'}]}]}),
            ('/v1/messages', {'model': 'codeart/GLM-5.2', 'max_tokens': 32, 'messages': [
                {'role': 'user', 'content': [{'type': 'text', 'text': 'colour?'},
                    {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'aGVsbG8='}}]}]}),
        ]
        Backend.vision_reject = True
        with patch.object(self.hub, '_anthropic_messages_enabled', return_value=True):
            for path, payload in cases:
                with self.subTest(path=path):
                    Backend.requests.clear()
                    status, headers, raw = self.request('POST', path, payload)
                    self.assertEqual(status, 200, raw)
                    mark = headers.get('X-WB-Vision-Fallback', '')
                    self.assertIn('codeart/GLM-5.2', mark)
                    self.assertIn('deepseek-v4.1-flash', mark)
                    paths = [p for p, h, b in Backend.requests]
                    self.assertIn('/v1/chat/completions', paths)
                    fallback = [b for p, h, b in Backend.requests if p == '/v2/chat/completions']
                    self.assertTrue(fallback, 'workbuddy fallback channel was not called')
                    self.assertEqual(fallback[-1]['model'], 'deepseek-v4.1-flash')
                    row = json.loads(Path(self.hub.USAGE_LOG).read_text(encoding="utf-8").splitlines()[-1])
                    self.assertEqual(row['model'], 'deepseek-v4.1-flash')
                    self.assertEqual(row['requested_model'], 'codeart/GLM-5.2')
                    self.assertTrue(row['vision_fallback'])
                    self.assertNotEqual(row.get('provider'), 'codearts')

    def test_vision_rejection_stream_keeps_alias_and_header(self):
        url = 'data:image/png;base64,aGVsbG8='
        image = {'type': 'image_url', 'image_url': {'url': url, 'detail': 'high'}}
        payload = {'model': 'codeart/GLM-5.2', 'stream': True, 'messages': [
            {'role': 'user', 'content': [{'type': 'text', 'text': 'colour?'}, image]}]}
        Backend.vision_reject = True
        status, headers, raw = self.request('POST', '/v1/chat/completions', payload)
        self.assertEqual(status, 200, raw)
        self.assertIn('deepseek-v4.1-flash', headers.get('X-WB-Vision-Fallback', ''))
        frames = [json.loads(x[6:]) for x in raw.decode().splitlines() if x.startswith('data: {')]
        self.assertTrue(frames)
        self.assertTrue(all(f['model'] == 'codeart/GLM-5.2' for f in frames))
        self.assertTrue(raw.rstrip().endswith(b'data: [DONE]'))
        self.assertTrue(any(p == '/v2/chat/completions' for p, h, b in Backend.requests))

    def test_vision_rejection_without_image_passes_through(self):
        Backend.force_406 = True
        status, headers, raw = self.chat()
        self.assertEqual(status, 400, raw)
        self.assertIn(b'001001020.406', raw)
        self.assertNotIn('X-WB-Vision-Fallback', headers)
        self.assertFalse(any(p == '/v2/chat/completions' for p, h, b in Backend.requests))

    def test_other_400_with_image_passes_through(self):
        url = 'data:image/png;base64,aGVsbG8='
        image = {'type': 'image_url', 'image_url': {'url': url, 'detail': 'high'}}
        payload = {'model': 'codeart/GLM-5.2', 'messages': [
            {'role': 'user', 'content': [{'type': 'text', 'text': 'x'}, image]}]}
        Backend.status = 400
        status, headers, raw = self.request('POST', '/v1/chat/completions', payload)
        self.assertEqual(status, 400, raw)
        self.assertFalse(any(p == '/v2/chat/completions' for p, h, b in Backend.requests))

    def test_vision_fallback_disabled_by_config(self):
        self.config.update({'vision_fallback_enabled': False})
        url = 'data:image/png;base64,aGVsbG8='
        image = {'type': 'image_url', 'image_url': {'url': url, 'detail': 'high'}}
        payload = {'model': 'codeart/GLM-5.2', 'messages': [
            {'role': 'user', 'content': [{'type': 'text', 'text': 'x'}, image]}]}
        Backend.vision_reject = True
        status, headers, raw = self.request('POST', '/v1/chat/completions', payload)
        self.assertEqual(status, 400, raw)
        self.assertNotIn('X-WB-Vision-Fallback', headers)
        self.assertFalse(any(p == '/v2/chat/completions' for p, h, b in Backend.requests))

    def test_responses_stream_falls_back_on_vision_rejection(self):
        url = 'data:image/png;base64,aGVsbG8='
        payload = {'model': 'codeart/GLM-5.2', 'stream': True, 'input': [
            {'role': 'user', 'content': [{'type': 'input_text', 'text': 'colour?'},
                                         {'type': 'input_image', 'image_url': url, 'detail': 'high'}]}]}
        Backend.vision_reject = True
        status, headers, raw = self.request('POST', '/v1/responses', payload)
        self.assertEqual(status, 200, raw)
        self.assertIn('deepseek-v4.1-flash', headers.get('X-WB-Vision-Fallback', ''))
        self.assertIn(b'event: response.completed', raw)
        self.assertIn(b'bridge-ok', raw)
        self.assertNotIn(b'not iterable', raw)
        self.assertTrue(any(p == '/v2/chat/completions' for p, h, b in Backend.requests))

    def test_messages_stream_falls_back_on_vision_rejection(self):
        payload = {'model': 'codeart/GLM-5.2', 'max_tokens': 32, 'stream': True, 'messages': [
            {'role': 'user', 'content': [{'type': 'text', 'text': 'colour?'},
                {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'aGVsbG8='}}]}]}
        Backend.vision_reject = True
        with patch.object(self.hub, '_anthropic_messages_enabled', return_value=True):
            status, headers, raw = self.request('POST', '/v1/messages', payload)
        self.assertEqual(status, 200, raw)
        self.assertIn('deepseek-v4.1-flash', headers.get('X-WB-Vision-Fallback', ''))
        self.assertIn(b'event: message_start', raw)
        self.assertIn(b'event: message_stop', raw)
        self.assertIn(b'codeart/GLM-5.2', raw)
        self.assertTrue(any(p == '/v2/chat/completions' for p, h, b in Backend.requests))

    def test_reopen_after_vision_fallback_short_circuits_pin_check(self):
        self.runtime.local.vision_fallback = {"from": "codeart/GLM-5.2", "to": "deepseek-v4.1-flash"}
        self.addCleanup(setattr, self.runtime.local, "vision_fallback", None)
        payload = {"model": "codeart/GLM-5.2", "messages": [{"role": "user", "content": "hi"}]}
        upstream, account, _ = self.runtime.open(dict(payload), pin_account=self.native)
        with upstream:
            upstream.read1(65536)
        self.assertEqual(account.uid, "wb-test")
        self.assertEqual([p for p, h, b in Backend.requests], ["/v2/chat/completions"])

    def test_vision_fallback_header_rejects_crlf_injection(self):
        url = 'data:image/png;base64,aGVsbG8='
        image = {'type': 'image_url', 'image_url': {'url': url, 'detail': 'high'}}
        payload = {'model': 'codeart/GLM-5.2\r\nX-Evil: pwned', 'messages': [
            {'role': 'user', 'content': [{'type': 'text', 'text': 'x'}, image]}]}
        Backend.vision_reject = True
        status, headers, raw = self.request('POST', '/v1/chat/completions', payload)
        self.assertEqual(status, 200, raw)
        self.assertNotIn('X-Evil', headers)
        mark = headers.get('X-WB-Vision-Fallback', '')
        self.assertEqual(mark, 'codeart/GLM-5.2;via=workbuddy:deepseek-v4.1-flash')
        self.assertNotIn('\r', mark)
        self.assertNotIn('\n', mark)

    def test_vision_fallback_header_is_ascii_for_cjk_model(self):
        url = 'data:image/png;base64,aGVsbG8='
        image = {'type': 'image_url', 'image_url': {'url': url, 'detail': 'high'}}
        payload = {'model': 'codeart/模型', 'messages': [
            {'role': 'user', 'content': [{'type': 'text', 'text': 'x'}, image]}]}
        Backend.vision_reject = True
        status, headers, raw = self.request('POST', '/v1/chat/completions', payload)
        self.assertEqual(status, 200, raw)
        self.assertIn('X-WB-Vision-Fallback', headers)
        mark = headers['X-WB-Vision-Fallback']
        self.assertTrue(mark.isascii(), mark)
        self.assertNotIn('模型', mark)
        self.assertNotIn('\r', mark)
        self.assertNotIn('\n', mark)
        self.assertTrue(any(p == '/v2/chat/completions' for p, h, b in Backend.requests))

    def test_vision_fallback_header_is_ascii_for_emoji_model(self):
        url = 'data:image/png;base64,aGVsbG8='
        image = {'type': 'image_url', 'image_url': {'url': url, 'detail': 'high'}}
        payload = {'model': 'codeart/🚀', 'messages': [
            {'role': 'user', 'content': [{'type': 'text', 'text': 'x'}, image]}]}
        Backend.vision_reject = True
        status, headers, raw = self.request('POST', '/v1/chat/completions', payload)
        self.assertEqual(status, 200, raw)
        self.assertIn('X-WB-Vision-Fallback', headers)
        mark = headers['X-WB-Vision-Fallback']
        self.assertTrue(mark.isascii(), mark)
        self.assertNotIn('🚀', mark)
        self.assertNotIn('\r', mark)
        self.assertNotIn('\n', mark)
        self.assertTrue(any(p == '/v2/chat/completions' for p, h, b in Backend.requests))

    def test_safe_fallback_token_is_ascii_only(self):
        from extensions.codearts.bridge import _safe_fallback_token

        cases = {"codeart/模型": "codeart/", "codeart/\r\nX": "codeart/",
                 "codeart/🚀": "codeart/", "\r\n": "unknown", "": "unknown"}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                token = _safe_fallback_token(raw)
                self.assertEqual(token, expected)
                self.assertTrue(token.isascii())
                token.encode("latin-1")  # 绝不触发 http.server 的 latin-1 编码错误

    def test_alias_stream_length_tracks_underlying_response(self):
        from extensions.codearts.bridge import _AliasStream

        class FakeResponse:
            def __init__(self, blocks):
                self.blocks = list(blocks)
                self.length = sum(len(b) for b in blocks)
                self.headers = {}

            def read1(self, size=-1):
                if not self.blocks:
                    return b""
                block = self.blocks.pop(0)
                self.length -= len(block)
                return block

            def close(self):
                pass

        response = FakeResponse([b'data: {"model":"x"}\n\n'])
        stream = _AliasStream(response, "codeart/GLM-5.2")
        self.assertEqual(stream.length, response.length)
        stream.read1(65536)
        # 必须动态跟随底层 length；快照实现会停在初值，导致完整流被误判为截断。
        self.assertEqual(stream.length, 0)
        self.assertEqual(stream.length, response.length)

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
        self.assertIn(b'/codearts/dashboard.css', raw)
        self.assertIn("codearts", headers.get("ETag", ""))
        status, _, raw = self.request("GET", "/codearts/dashboard.js")
        self.assertEqual(status, 200)
        self.assertNotIn(b"backend-test-key", raw)
        status, headers, css = self.request("GET", "/codearts/dashboard.css")
        self.assertEqual(status, 200)
        self.assertTrue(headers.get("Content-Type", "").startswith("text/css"))
        self.assertIn(b"#pageCodearts", css)
        self.assertNotIn(b"backend-test-key", css)



if __name__ == "__main__":
    unittest.main()
