"""Anthropic Messages HTTP surface: routing, auth, translation, streaming.

Runs a real gateway process-in-thread against a mock WorkBuddy upstream so
the whole path is exercised - do_POST routing, the Anthropic error envelope,
request translation, the SSE event sequence and the usage rows - with no
network access and no real accounts.
"""
import base64
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import _lifecycle as life  # noqa: E402  (shared spare-port pick)

WORK = tempfile.mkdtemp(prefix="anthropic-http-")
ACCOUNTS = os.path.join(WORK, "accounts")
os.makedirs(ACCOUNTS, exist_ok=True)
os.environ["ACCOUNTS_DIR"] = ACCOUNTS
os.environ["WB_PROXY_USAGE_DIR"] = os.path.join(WORK, "usage")
os.makedirs(os.environ["WB_PROXY_USAGE_DIR"], exist_ok=True)
os.environ["HOME"] = WORK
os.environ["USERPROFILE"] = WORK
os.environ["WB_PROXY_KEY"] = "TESTKEY"


def jwt(uid="acct-http-1"):
    def seg(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return seg({"alg": "none", "typ": "JWT"}) + "." + seg(
        {"uid": uid, "exp": int(time.time()) + 86400 * 30}) + ".x"


with open(os.path.join(ACCOUNTS, "acct1.json"), "w", encoding="utf-8") as fh:
    json.dump({
        "uid": "acct-http-1",
        "accessToken": jwt(),
        "refreshToken": "",
        "expiresAt": int(time.time()) + 86400 * 30,
        "realm": "intl",
        "domain": "www.workbuddy.ai",
        "platform": "CLI",
        "product": "workbuddy",
        "enabled": True,
    }, fh)


UPSTREAM_PORT = life.free_port()
MOCK = {
    "stream": True,
    "chunks": [],
    "json": {},
    "requests": [],
}


def sse(chunks):
    out = []
    for c in chunks:
        out.append(("data: " + json.dumps(c) + "\n\n").encode("utf-8"))
    out.append(b"data: [DONE]\n\n")
    return b"".join(out)


class MockUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {"_raw": raw.decode("utf-8", "replace")}
        MOCK["requests"].append({"path": self.path, "body": body})
        if self.path.endswith("/chat/completions"):
            if MOCK["stream"]:
                payload = sse(MOCK["chunks"])
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                payload = json.dumps(MOCK["json"]).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            return
        payload = b'{"data":{}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        payload = b'{"data":{}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


upstream = ThreadingHTTPServer(("127.0.0.1", UPSTREAM_PORT), MockUpstream)
threading.Thread(target=upstream.serve_forever, daemon=True).start()

import wb_identity
import wb_proxy as P

_ENDPOINTS = dict(wb_identity._ENDPOINTS)
for key in list(_ENDPOINTS):
    _ENDPOINTS[key] = ("http://127.0.0.1:%d" % UPSTREAM_PORT, _ENDPOINTS[key][1])
wb_identity._ENDPOINTS.update(_ENDPOINTS)

GATEWAY_PORT = life.free_port()


class Args(object):
    host = "127.0.0.1"
    port = GATEWAY_PORT
    api_key = "TESTKEY"
    system_prompt = "You are a helpful assistant."
    user_agent = None
    usage_dir = os.environ["WB_PROXY_USAGE_DIR"]
    accounts_dir = ACCOUNTS
    import_desktop = False
    lan = False
    panel_password = None
    info = None


P._apply_cli_overrides(Args())
P._bootstrap_runtime(Args())
gateway = ThreadingHTTPServer(("127.0.0.1", GATEWAY_PORT), P.Handler)
threading.Thread(target=gateway.serve_forever, daemon=True).start()

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (label, detail))


def request(method, path, body=None, headers=None, timeout=30):
    url = "http://127.0.0.1:%d%s" % (GATEWAY_PORT, path)
    data = None if body is None else (
        body if isinstance(body, bytes) else json.dumps(body).encode("utf-8"))
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def wait_ready():
    for _ in range(60):
        try:
            code, _h, _b = request("GET", "/health", timeout=2)
            if code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def parse_sse(raw):
    events = []
    for block in raw.decode("utf-8", "replace").split("\n\n"):
        event = data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data += line[6:]
        if event:
            events.append((event, json.loads(data or "{}")))
    return events


TEXT_CHUNKS = [
    {"id": "chatcmpl_http_1", "model": "deepseek-v4.1-flash",
     "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]},
    {"id": "chatcmpl_http_1", "model": "deepseek-v4.1-flash",
     "choices": [{"index": 0, "delta": {"content": "Hello "}}]},
    {"id": "chatcmpl_http_1", "model": "deepseek-v4.1-flash",
     "choices": [{"index": 0, "delta": {"content": "world"}}]},
    {"id": "chatcmpl_http_1", "model": "deepseek-v4.1-flash",
     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
     "usage": {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13,
               "prompt_cache_hit_tokens": 3, "prompt_cache_write_tokens": 1}},
]
TOOL_CHUNKS = [
    {"id": "chatcmpl_http_2", "model": "deepseek-v4.1-flash",
     "choices": [{"index": 0, "delta": {"tool_calls": [
         {"index": 0, "id": "call_http_1", "type": "function",
          "function": {"name": "Bash", "arguments": "{\"cmd\":"}}]}}]},
    {"id": "chatcmpl_http_2", "model": "deepseek-v4.1-flash",
     "choices": [{"index": 0, "delta": {"tool_calls": [
         {"index": 0, "function": {"arguments": "\"ls\"}"}}]},
         "finish_reason": "tool_calls"}],
     "usage": {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11}},
]

try:
    if not wait_ready():
        print("  [FAIL] gateway did not start")
        sys.exit(1)

    print("[1] auth uses the Anthropic error envelope")
    code, headers, body = request(
        "POST", "/v1/messages",
        {"model": "deepseek-v4.1-flash", "max_tokens": 8,
         "messages": [{"role": "user", "content": "hi"}]})
    payload = json.loads(body or b"{}")
    check("missing key -> 401", code == 401, code)
    check("body is a native error envelope",
          payload.get("type") == "error"
          and (payload.get("error") or {}).get("type") == "authentication_error",
          payload)
    check("content-type is JSON",
          "application/json" in (headers.get("Content-Type") or ""), headers)

    code, _h, body = request(
        "POST", "/v1/messages",
        {"model": "deepseek-v4.1-flash", "max_tokens": 8,
         "messages": [{"role": "user", "content": "hi"}]},
        headers={"x-api-key": "WRONG"})
    payload = json.loads(body or b"{}")
    check("wrong x-api-key -> 401 authentication_error",
          code == 401
          and (payload.get("error") or {}).get("type") == "authentication_error",
          payload)

    code, _h, body = request(
        "POST", "/v1/messages",
        {"model": "deepseek-v4.1-flash", "max_tokens": 8,
         "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer TESTKEY"})
    check("Authorization: Bearer also authenticates",
          code != 401, (code, body[:120]))

    print()
    print("[2] request validation answers 400 invalid_request_error")
    for label, bad in (
        ("missing model", {"messages": [{"role": "user", "content": "hi"}]}),
        ("empty messages", {"model": "deepseek-v4.1-flash", "messages": []}),
        ("messages not a list", {"model": "deepseek-v4.1-flash", "messages": "x"}),
        ("unsupported role in messages",
         {"model": "deepseek-v4.1-flash",
          "messages": [{"role": "tool", "content": "s"}]}),
    ):
        code, _h, body = request("POST", "/v1/messages", bad,
                                 headers={"x-api-key": "TESTKEY"})
        payload = json.loads(body or b"{}")
        check("%s -> 400 invalid_request_error" % label,
              code == 400
              and (payload.get("error") or {}).get("type") == "invalid_request_error",
              (code, payload))

    code, _h, body = request("POST", "/v1/messages", b"{not json",
                             headers={"x-api-key": "TESTKEY"})
    payload = json.loads(body or b"{}")
    check("malformed JSON -> 400 native envelope",
          code == 400 and payload.get("type") == "error", (code, payload))

    print()
    print("[3] non-streaming translation round-trips")
    MOCK["stream"] = True
    MOCK["chunks"] = TEXT_CHUNKS
    MOCK["requests"] = []
    code, headers, body = request(
        "POST", "/v1/messages",
        {"model": "deepseek-v4.1-flash", "max_tokens": 64,
         "system": [{"type": "text", "text": "be brief"}],
         "messages": [
             {"role": "user", "content": [{"type": "text", "text": "say hi"}]},
             {"role": "system", "content": [{"type": "text", "text": "hook ctx"}]},
         ]},
        headers={"x-api-key": "TESTKEY"})
    msg = json.loads(body or b"{}")
    check("200 with JSON", code == 200
          and "application/json" in (headers.get("Content-Type") or ""), code)
    check("native message shape",
          msg.get("type") == "message" and msg.get("role") == "assistant",
          msg)
    check("text block carries the answer",
          msg.get("content") == [{"type": "text", "text": "Hello world"}], msg.get("content"))
    check("stop_reason is end_turn", msg.get("stop_reason") == "end_turn", msg.get("stop_reason"))
    check("usage maps input/output",
          (msg.get("usage") or {}).get("input_tokens") == 11
          and (msg.get("usage") or {}).get("output_tokens") == 2, msg.get("usage"))
    sent = (MOCK["requests"][0].get("body") or {}) if MOCK["requests"] else {}
    check("upstream saw the system prompt",
          any(m.get("role") == "system" and "be brief" in str(m.get("content"))
              for m in sent.get("messages") or []), sent.get("messages"))
    check("upstream saw the user text",
          any(m.get("role") == "user" and "say hi" in str(m.get("content"))
              for m in sent.get("messages") or []), sent.get("messages"))
    check("mid-conversation system role reaches upstream as a system message",
          any(m.get("role") == "system" and "hook ctx" in str(m.get("content"))
              for m in sent.get("messages") or []), sent.get("messages"))

    print()
    print("[4] streaming emits the native event sequence")
    MOCK["stream"] = True
    MOCK["chunks"] = TEXT_CHUNKS
    code, headers, body = request(
        "POST", "/v1/messages",
        {"model": "deepseek-v4.1-flash", "max_tokens": 64, "stream": True,
         "messages": [{"role": "user", "content": "say hi"}]},
        headers={"x-api-key": "TESTKEY"})
    events = parse_sse(body)
    types = [e[0] for e in events]
    check("200 SSE", code == 200
          and "text/event-stream" in (headers.get("Content-Type") or ""),
          (code, headers.get("Content-Type")))
    check("event order is message_start .. message_stop",
          types and types[0] == "message_start" and types[-1] == "message_stop", types)
    check("content block lifecycle",
          "content_block_start" in types and "content_block_delta" in types
          and "content_block_stop" in types, types)
    text = "".join(e[1]["delta"]["text"] for e in events
                   if e[0] == "content_block_delta" and e[1]["delta"]["type"] == "text_delta")
    check("text deltas combine", text == "Hello world", text)
    final = [e[1] for e in events if e[0] == "message_delta"][0]
    check("final delta carries stop_reason + usage",
          final["delta"]["stop_reason"] == "end_turn"
          and final["usage"]["input_tokens"] == 11
          and final["usage"]["output_tokens"] == 2, final)
    start = [e[1] for e in events if e[0] == "message_start"][0]
    check("message_start names the model",
          start["message"]["model"] == "deepseek-v4.1-flash", start)

    print()
    print("[5] tool_use round-trips through the stream")
    MOCK["stream"] = True
    MOCK["chunks"] = TOOL_CHUNKS
    MOCK["requests"] = []
    code, _h, body = request(
        "POST", "/v1/messages",
        {"model": "deepseek-v4.1-flash", "max_tokens": 64, "stream": True,
         "tools": [{"name": "Bash", "description": "run",
                    "input_schema": {"type": "object",
                                     "properties": {"cmd": {"type": "string"}}}}],
         "messages": [{"role": "user", "content": "list files"}]},
        headers={"x-api-key": "TESTKEY"})
    events = parse_sse(body)
    types = [e[0] for e in events]
    starts = [e[1] for e in events if e[0] == "content_block_start"]
    check("tool_use content_block_start",
          bool(starts) and starts[0]["content_block"]["type"] == "tool_use"
          and starts[0]["content_block"]["name"] == "Bash", starts)
    partial = "".join(e[1]["delta"]["partial_json"] for e in events
                      if e[0] == "content_block_delta"
                      and e[1]["delta"]["type"] == "input_json_delta")
    check("input_json_delta combines", partial == '{"cmd":"ls"}', partial)
    final = [e[1] for e in events if e[0] == "message_delta"][0]
    check("stop_reason is tool_use", final["delta"]["stop_reason"] == "tool_use", final)
    sent = (MOCK["requests"][0].get("body") or {}) if MOCK["requests"] else {}
    check("upstream received an OpenAI tool definition",
          bool(sent.get("tools"))
          and sent["tools"][0]["function"]["name"] == "Bash", sent.get("tools"))

    print()
    print("[6] count_tokens answers the native shape")
    code, _h, body = request(
        "POST", "/v1/messages/count_tokens",
        {"model": "deepseek-v4.1-flash",
         "messages": [{"role": "user", "content": "hello world"}]},
        headers={"x-api-key": "TESTKEY"})
    payload = json.loads(body or b"{}")
    check("count_tokens -> 200 with input_tokens",
          code == 200 and isinstance(payload.get("input_tokens"), int)
          and payload["input_tokens"] > 0, (code, payload))

    print()
    print("[7] usage rows are recorded for the messages route")
    usage_log = os.path.join(os.environ["WB_PROXY_USAGE_DIR"], "usage.jsonl")
    rows = []
    if os.path.exists(usage_log):
        with open(usage_log, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
    check("usage.jsonl has rows", len(rows) >= 2, len(rows))
    check("rows carry the model",
          any(r.get("model") == "deepseek-v4.1-flash" for r in rows), rows[-2:])
    stream_rows = [r for r in rows if r.get("stream") and r.get("outcome") == "completed"]
    check("streamed messages record upstream tokens",
          any((r.get("prompt_tokens") or 0) == 11 and (r.get("completion_tokens") or 0) == 2
              for r in stream_rows), stream_rows[-3:])
    check("streamed messages record cache hits",
          any((r.get("cached_tokens") or 0) == 3 for r in stream_rows), stream_rows[-3:])
finally:
    try:
        gateway.shutdown()
        gateway.server_close()
    except Exception:
        pass
    try:
        upstream.shutdown()
        upstream.server_close()
    except Exception:
        pass
    try:
        if P.SCHEDULER is not None:
            P.SCHEDULER.stop()
    except Exception:
        pass
    try:
        P.PRICING.stop()
    except Exception:
        pass

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
