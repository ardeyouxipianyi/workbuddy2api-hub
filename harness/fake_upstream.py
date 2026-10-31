#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A local stand-in for the WorkBuddy upstream.

Purpose: let the two backends (Python / Rust) answer the *same* request twice
without spending a single credit, so their responses can be compared byte for
byte. Nothing here talks to the network.

    python3 harness/fake_upstream.py --port 9444 --fixtures harness/fixtures

Routing is by path only; a chat request is answered from
``fixtures/<key>.json`` where ``key`` = first 16 hex of sha256 over
``model + "\\n" + canonical(messages)``. A missing fixture is not an error: the
upstream synthesises a minimal OpenAI-shaped answer (and a matching SSE stream),
so a new case only needs a fixture when its *content* matters for the diff.

The account bootstrap (``--bootstrap DIR``) writes one cn account plus the
matching settings into ``DIR``, which is what makes a throwaway gateway able to
reach this server with ``WB_CN_CHAT_UPSTREAM=http://127.0.0.1:<port>``.

The account's product must be ``workbuddy`` (desktop): the vscode identity has
its own hardcoded cn endpoint and ignores WB_CN_CHAT_UPSTREAM, so a vscode
fixture would quietly call the real ``www.workbuddy.cn`` instead of this file.
"""
import argparse
import hashlib
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))


def fixture_key(model, messages):
    payload = json.dumps({"model": model, "messages": messages}, sort_keys=True,
                         ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def completion_body(model, text="OK", usage=None):
    return {
        "id": "chatcmpl-fixture",
        "object": "chat.completion",
        "created": 1730000000,
        "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}],
        "usage": usage or {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13},
    }


def _marker(messages):
    """读请求里的标记词，让同一条 case 能要到不同的上游行为（无 usage / 有缓存命中）。"""
    for message in messages or []:
        text = message.get("content")
        if isinstance(text, str):
            if "NOUSAGE" in text:
                return "nousage"
            if "WITHCACHE" in text:
                return "cache"
    return ""


def sse_frames(model, text="OK", marker=""):
    """The upstream's own stream shape: chunks carrying choices, usage last."""
    frames = []
    for piece in (text[:1] or "O", text[1:] or "K"):
        frames.append({"choices": [{"index": 0, "delta": {"content": piece}}]})
    frames.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    if marker != "nousage":
        usage = {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13}
        if marker == "cache":
            usage["prompt_tokens_details"] = {"cached_tokens": 7}
        frames[-1]["usage"] = usage
    out = []
    for frame in frames:
        frame.update({"id": "chatcmpl-fixture", "object": "chat.completion.chunk",
                      "created": 1730000000, "model": model})
        out.append("data: " + json.dumps(frame, ensure_ascii=False) + "\n\n")
    out.append("data: [DONE]\n\n")
    return "".join(out).encode("utf-8")


def sse_parts(model, text="OK", marker=""):
    """SSE 按帧切片；由 handler 逐片 flush，客户端才能观察到「首字节早于末字节」。"""
    return [frame + b"\n\n"
            for frame in sse_frames(model, text, marker).split(b"\n\n") if frame.strip()]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    fixtures_dir = None
    seen = []

    def log_message(self, *args):
        pass

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fixture(self, model, messages):
        if not self.fixtures_dir:
            return None
        path = os.path.join(self.fixtures_dir, fixture_key(model, messages) + ".json")
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/v3/config":
            return self._json(200, {"agents": {"cli": {"models": [
                {"name": "deepseek-v4.1-flash", "rate": 0.0, "contextLength": 1000000},
                {"name": "glm-5.3", "rate": 1.0, "contextLength": 200000},
            ]}}, "modelTiers": []})
        if path.startswith("/billing/meter/") or path.startswith("/v2/billing/meter/"):
            return self._json(200, {"code": 0, "data": {"totalCredits": 1000,
                                                        "cycleStartTime": 1730000000,
                                                        "cycleEndTime": 1730086400}})
        return self._json(404, {"code": 404, "msg": "no fixture for %s" % path})

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._json(400, {"code": 400, "msg": "bad json"})
        if path == "/v2/chat/completions":
            model = body.get("model") or "unknown"
            messages = body.get("messages") or []
            key = fixture_key(model, messages)
            self.seen.append(key)
            fixture = self._fixture(model, messages)
            usage = (fixture or {}).get("usage")
            marker = _marker(messages)
            if body.get("stream"):
                payload = (fixture or {}).get("sse")
                parts = ([payload.encode("utf-8")] if isinstance(payload, str) else
                         sse_parts(model, (fixture or {}).get("text", "OK"), marker))
                data_len = sum(len(p) for p in parts)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(data_len))
                self.end_headers()
                # 逐片 flush + 20ms：一次性写到底的话 `ttft_ms`/`gen_ms` 会退化成
                # 「谁先读完谁先记」，`tokens_per_sec` 有无就成了噪声；20ms 让
                # gen_ms 在毫秒粒度上稳定 > 0。
                for part in parts:
                    self.wfile.write(part)
                    self.wfile.flush()
                    time.sleep(0.02)
                return None
            return self._json(200, fixture["body"] if fixture and fixture.get("body")
                              else completion_body(model, (fixture or {}).get("text", "OK"), usage))
        if path.endswith("/auth/token/refresh") or path.endswith("/auth/state") or \
                path.endswith("/login/account"):
            return self._json(200, {"code": 0, "data": {"accessToken": "fixture-token",
                                                        "expiresIn": 7200}})
        if path.endswith("/daily-checkin") or "/meter/" in path:
            return self._json(200, {"code": 0, "data": {"totalCredits": 1000}})
        return self._json(404, {"code": 404, "msg": "no fixture for %s" % path})


def bootstrap(directory):
    """One cn account + settings that point the gateway at a local upstream."""
    accounts = os.path.join(directory, "accounts")
    usage = os.path.join(directory, "usage")
    os.makedirs(accounts, exist_ok=True)
    os.makedirs(usage, exist_ok=True)
    uid = "fixture-0000-0000-0000-000000000001"
    account = {
        "uid": uid, "nickname": "fixture", "domain": "copilot.tencent.com",
        "realm": "cn", "platform": "linux", "product": "workbuddy",
        "enterpriseId": "", "accessToken": "fixture-token",
        "refreshToken": "", "expiresAt": int(time.time()) + 86400,
        "addedAt": int(time.time()), "source": "fixture", "apiKey": "",
        "proxySlot": "", "proxy": "", "enabled": True, "tasksEnabled": False,
        "weight": 1, "note": "harness", "disabledModels": [],
        "restrictedUntil": 0,
        "credits": {"totalCredits": 1000, "isPaidUser": False, "fetchedAt": int(time.time())},
        "tier": "free", "lastCheckin": 0, "lastDailyChat": 0, "lastActivityReport": 0,
        "modelCaps": {},
    }
    with open(os.path.join(accounts, uid + ".json"), "w", encoding="utf-8") as fh:
        json.dump(account, fh, ensure_ascii=False, indent=2)
    settings = {"api_keys": [], "panel_password_default": True,
                "log_db_enabled": False, "update_check_enabled": False,
                # Anthropic 兼容面默认是关的；对拍要真走一次翻译，所以这里打开。
                "enable_anthropic_messages": True,
                "daily_chat_web": False, "schedule": {"checkin": [23], "travel": [23],
                                                      "keepalive": [23], "cat": [23]}}
    with open(os.path.join(accounts, "settings.json"), "w", encoding="utf-8") as fh:
        json.dump(settings, fh, ensure_ascii=False, indent=2)
    return accounts, usage


def serve(port, fixtures_dir):
    Handler.fixtures_dir = fixtures_dir
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return httpd


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=9444)
    ap.add_argument("--fixtures", default=os.path.join(HERE, "fixtures"))
    ap.add_argument("--bootstrap", metavar="DIR", help="write a fixture account+settings into DIR")
    args = ap.parse_args(argv)
    if args.bootstrap:
        accounts, usage = bootstrap(args.bootstrap)
        print("bootstrapped %s" % accounts)
        print("run the gateway with:")
        print("  ACCOUNTS_DIR=%s WB_PROXY_USAGE_DIR=%s WB_CN_CHAT_UPSTREAM=http://127.0.0.1:%d \\\n"
              "  python3 wb_proxy.py --port 8090 --serve" % (accounts, usage, args.port))
        return 0
    httpd = serve(args.port, args.fixtures)
    print("fake upstream on http://127.0.0.1:%d (fixtures: %s)" % (args.port, args.fixtures))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
