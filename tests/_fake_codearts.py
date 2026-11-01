# -*- coding: utf-8 -*-
"""测试用的假华为云 CodeArts 上游。

模拟单进程版本用到的端点：OAuth 换 token、聊天 SSE、模型发现、福利领取、
每日签到。测试把 `wb_codearts.SNAP_HOST` / `STS_HOST` / `BENEFIT_HOST`
指到本服务，不碰外网、不碰真实账号。

文件名不以 `_test_` 开头，`tests/run_all.py` 不会把它当套件跑。
"""

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NOW = int(time.time())


class FakeState(object):
    def __init__(self):
        self.token = "TOKEN"
        self.refresh_token = "refresh-1"
        self.api_key = "AK"
        self.secret_key = "SK"
        self.user_id = "u1"
        self.user_name = "账号一"
        self.domain_id = "d1"
        self.expiration = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        self.requests = []
        self.last_headers = None
        self.last_body = None
        self.chat_text = "你好"
        self.chat_mode = "delta"  # delta | text
        self.chat_error = None    # (status, body_dict) 或 None
        self.chat_error_remaining = -1  # -1 = 一直失败；N = 失败 N 次后恢复
        self.embedded_error = None  # {"error_code": ..., "error_msg": ...}
        self.require_benefit_header = False
        self.omit_identity = False
        self.builtin = [
            {"model_id": "GLM-5.2", "model_name": "GLM-5.2",
             "context_window": 131072, "max_tokens": 8192},
            {"model_id": "GLM-4.7", "model_name": "GLM-4.7"},
        ]
        self.agent_models = [
            {"model_name": "GLM-5.2", "model_id": "GLM-5.2",
             "model_parameters": {"context_window": 131072, "max_tokens": 8192}},
        ]
        self.benefit_models = [
            {"model_id": "deepseek-v4-pro-0813", "model_name": "deepseek-v4-pro-0813",
             "context_window": 65536, "max_tokens": 4096},
        ]
        self.campaigns = [{
            "campaignId": 1, "title": "每日签到", "status": "ELIGIBLE",
            "claimable": True, "benefitUnit": "CREDIT",
            "pendingTotalAmount": 1000,
            "extra": {"startTime": "2026-01-01T00:00:00Z",
                      "endTime": "2026-12-31T00:00:00Z"},
        }]
        self.token_calls = 0
        self.model_calls = 0
        self.package = {
            "spec_code": "codearts.agent.individual.trial",
            "status": "normal",
            "package_name_cn": "体验版",
            "package_name_en": "Trial",
            "is_token_package": False,
            "is_credit_package": True,
        }
        self.credit_metrics = [
            {"name": "usageTotalPackageCredit", "package_credit_amount": 4500.0,
             "package_credit_used": 0.81, "package_credit_remain": 4499.19,
             "package_credit_expiring_amount": 0},
            {"name": "usageBasicPackageCredit", "package_credit_amount": 500.0,
             "package_credit_used": 0.0, "package_credit_remain": 500.0,
             "package_credit_expiring_amount": 0},
            {"name": "usageOnDemandPackageCredit", "package_credit_amount": 0,
             "package_credit_used": 0, "package_credit_remain": 0,
             "package_credit_expiring_amount": 0},
            {"name": "usageBonusPackageCredit", "package_credit_amount": 4000.0,
             "package_credit_used": 0.81, "package_credit_remain": 3999.19,
             "package_credit_expiring_amount": 0},
        ]
        self.model_metrics = [
            {"model_id": "GLM-5.2", "model_name": "GLM-5.2",
             "model_desc": "最新旗舰模型", "model_type": "system",
             "enable": True, "status": "normal", "is_luxury": False},
        ]

    def reset_requests(self):
        self.requests = []
        self.last_headers = None
        self.last_body = None


def _sse_chunks(state, model):
    created = int(time.time())
    if state.chat_mode == "text":
        events = [
            {"text": "你", "prompt_tokens": 3, "completion_tokens": 1},
            {"text": "你好", "prompt_tokens": 3, "completion_tokens": 2},
            {"text": "[DONE]", "error_code": "0"},
        ]
        out = b""
        for event in events:
            out += ("data: " + json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        return out
    chunks = [
        {"id": "chatcmpl-fake", "object": "chat.completion.chunk",
         "created": created, "model": model,
         "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                      "finish_reason": None}]},
        {"id": "chatcmpl-fake", "object": "chat.completion.chunk",
         "created": created, "model": model,
         "choices": [{"index": 0, "delta": {"content": state.chat_text},
                      "finish_reason": None}]},
        {"id": "chatcmpl-fake", "object": "chat.completion.chunk",
         "created": created, "model": model,
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"id": "chatcmpl-fake", "object": "chat.completion.chunk",
         "created": created, "model": model, "choices": [],
         "usage": {"prompt_tokens": 3, "completion_tokens": 2,
                   "total_tokens": 5}},
    ]
    out = b""
    for chunk in chunks:
        out += ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n").encode("utf-8")
    out += b"data: [DONE]\n"
    return out


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state = None

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, status, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def _send_sse(self, raw):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def _read(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _token_payload(self):
        state = self.state
        payload = {
            "refresh_token": state.refresh_token,
            "credentials": {
                "access_key_id": state.api_key,
                "secret_access_key": state.secret_key,
                "security_token": state.token,
                "expiration": state.expiration,
            },
        }
        if not state.omit_identity:
            payload.update({
                "user_id": state.user_id,
                "user_name": state.user_name,
                "domain_id": state.domain_id,
            })
        return payload

    def do_POST(self):
        path = self.path.split("?")[0]
        raw = self._read()
        state = self.state
        state.requests.append({"path": path, "body": raw.decode("utf-8", "replace"),
                               "headers": {k.lower(): v for k, v in self.headers.items()}})
        state.last_headers = {k.lower(): v for k, v in self.headers.items()}
        try:
            state.last_body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            state.last_body = {}
        if path == "/v1/oauth2/tokens":
            state.token_calls += 1
            form = urllib.parse.parse_qs(raw.decode("utf-8"))
            if form.get("grant_type", [""])[0] == "refresh_token":
                if not self.headers.get("DPoP"):
                    self._send_json(400, {"error": "invalid_grant",
                                          "error_msg": "missing DPoP"})
                    return
            self._send_json(200, self._token_payload())
            return
        if path == "/api/v2/chat/completions":
            if self.headers.get("x-auth-token") != state.token:
                self._send_json(401, {"error_msg": "bad token"})
                return
            if state.require_benefit_header and self.headers.get("maas_type") != "benefit":
                self._send_json(200, {"error_code": "InferHub.002002009.404",
                                      "error_msg": "model is not registered"})
                return
            if state.chat_error:
                if state.chat_error_remaining == 0:
                    state.chat_error = None
                else:
                    if state.chat_error_remaining > 0:
                        state.chat_error_remaining -= 1
                    self._send_json(*state.chat_error)
                    return
            if state.embedded_error:
                raw_sse = ("data: " + json.dumps(state.embedded_error) + "\n").encode()
                self._send_sse(raw_sse)
                return
            self._send_sse(_sse_chunks(state, state.last_body.get("model") or "GLM-5.2"))
            return
        if path == "/api/v1/benefit/claim":
            self._send_json(200, {"error_code": "0000", "error_msg": ""})
            return
        if path == "/v1/ops/claim":
            self._send_json(200, {"code": 0, "message": "ok", "data": {}})
            return
        if path == "/v1/ops/confirm":
            self._send_json(200, {"code": 0, "message": "ok", "data": {}})
            return
        self._send_json(404, {"error_msg": "not found"})

    def do_GET(self):
        path = self.path.split("?")[0]
        state = self.state
        state.requests.append({"path": path, "body": "",
                               "headers": {k.lower(): v for k, v in self.headers.items()}})
        state.last_headers = {k.lower(): v for k, v in self.headers.items()}
        if path == "/v1/login/ticket":
            self._send_json(200, self._token_payload())
            return
        if path == "/v1/model/builtin":
            state.model_calls += 1
            self._send_json(200, {"builtinModels": state.builtin})
            return
        if path == "/v1/agent-center/agents/useragents":
            self._send_json(200, {"agents": [{
                "agent_id": "agent-1", "agent_name": "CodeAgent",
                "is_primary_agent": True, "show_in_ide": True,
                "alias": {"alias_zh_cn": "智能体"},
            }]})
            return
        if path == "/v1/agent-center/agents/detail":
            self._send_json(200, {"gpts": {"models": state.agent_models}})
            return
        if path == "/api/v1/gateway/config":
            self._send_json(200, {"error_code": "0000", "result": {
                "base_url": "https://example.test", "models": state.benefit_models}})
            return
        if path == "/snap-manager/v1/statistics/plugin":
            self._send_json(200, {
                "metrics": state.credit_metrics,
                "model_metrics": state.model_metrics,
                "package": state.package,
                "start_date": "2026-10-31",
                "end_date": "2026-12-01",
            })
            return
        if path == "/v1/ops/delivery":
            self._send_json(200, {"code": 0, "message": "ok",
                                  "data": {"items": state.campaigns}})
            return
        if path == "/v5/caller-identity":
            self._send_json(200, {"account_id": state.domain_id,
                                  "principal_id": state.user_id,
                                  "principal_urn": "iam::%s:user/%s" % (
                                      state.domain_id, state.user_name)})
            return
        if path == "/v1/current/user":
            self._send_json(200, {"user_id": state.user_id,
                                  "user_name": state.user_name,
                                  "domain_id": state.domain_id})
            return
        self._send_json(404, {"error_msg": "not found"})


def start_server(state=None):
    state = state or FakeState()
    handler = type("FakeHuawei", (_Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    return server, base, state


def stop_server(server):
    try:
        server.shutdown()
        server.server_close()
    except Exception:
        pass
