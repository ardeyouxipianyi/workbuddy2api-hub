#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Scriptable stand-in upstream for the streaming / error contract probes.

The first user message selects the behaviour, so one server answers every case
in `specs/streaming-and-errors.md` without touching fixtures:

    PROBE:base | role | reasoning | tools | usage_on_choice | nousage
          | nodone | empty | abort | keepalive | slow
    ERR:<status>:<json>        e.g. ERR:403:{"code":11140,"msg":"request illegal"}

Anything else gets the two-frame "OK" stream, same as fake_upstream's fallback.
Every chat request (headers + body) is appended to --log as one JSON line, so a
probe can assert what the gateway actually sent upstream.

    python3 harness/probe_upstream.py --port 9555 --log /tmp/probe.jsonl
"""
import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USAGE = {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13}


def _marker(messages):
    for message in messages or []:
        text = message.get("content")
        if isinstance(text, str) and (text.startswith("PROBE:") or text.startswith("ERR:")):
            return text.split("\n")[0].strip()
    return ""


def _frame(model, delta, finish=None, **extra):
    obj = {"id": "chatcmpl-probe", "object": "chat.completion.chunk",
           "created": 1730000000, "model": model,
           "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    obj.update(extra)
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n"


def _usage_frame(model, usage=None):
    return b"data: " + json.dumps({
        "id": "chatcmpl-probe", "object": "chat.completion.chunk",
        "created": 1730000000, "model": model, "choices": [],
        "usage": usage or USAGE}, ensure_ascii=False).encode("utf-8") + b"\n\n"


def stream_parts(case, model):
    """[(bytes, delay_before_write)] for the requested case."""
    done = (b"data: [DONE]\n\n", 0)
    if case == "role":
        return [(_frame(model, {"role": "assistant", "content": ""}), 0),
                (_frame(model, {"content": "He"}), 0),
                (_frame(model, {"content": "llo"}, "stop"), 0),
                (_usage_frame(model), 0), done]
    if case == "reasoning":
        return [(_frame(model, {"reasoning_content": "think1"}), 0),
                (_frame(model, {"reasoning_content": "think2", "content": "A"}), 0),
                (_frame(model, {"content": "B"}, "stop"), 0),
                (_usage_frame(model), 0), done]
    if case == "tools":
        return [(_frame(model, {"tool_calls": [{"index": 0, "id": "call_a",
                                               "type": "function",
                                               "function": {"name": "f", "arguments": '{"x"'}}]}), 0),
                (_frame(model, {"tool_calls": [{"index": 0, "function": {"arguments": ":1}"}}]}), 0),
                (_frame(model, {}, "tool_calls"), 0),
                (_usage_frame(model), 0), done]
    if case == "usage_on_choice":
        return [(_frame(model, {"content": "OK"}, "stop", usage=USAGE), 0), done]
    if case == "nousage":
        return [(_frame(model, {"content": "OK"}, "stop"), 0), done]
    if case == "nodone":
        return [(_frame(model, {"content": "OK"}, "stop"), 0), (_usage_frame(model), 0)]
    if case == "empty":
        return [done]
    if case == "abort":
        return [(_frame(model, {"content": "half"}, None), 0)]      # then the socket dies
    if case == "abort0":
        return [(_frame(model, {"content": ""}, None), 0)]         # dies before any text
    if case == "keepalive":
        return [(b": ping\n\n", 0), (_frame(model, {"content": "OK"}, "stop"), 0),
                (_usage_frame(model), 0), done]
    if case == "raw":
        # No `id`, and an upstream `event:` line the gateway does not model.
        return [(b"event: message\n\n", 0),
                (b'data: {"choices":[{"index":0,"delta":{"content":"X","role":"assistant"},"finish_reason":"stop"}],'
                 b'"object":"chat.completion.chunk","system_fingerprint":"fp_x"}\n\n', 0),
                done]
    if case == "slow":
        return [(_frame(model, {"content": "A"}, None), 0),
                (_frame(model, {"content": "B"}, "stop"), 18.0),
                (_usage_frame(model), 0), done]
    return [(_frame(model, {"content": "O"}, None), 0),
            (_frame(model, {"content": "K"}, "stop"), 0),
            (_usage_frame(model), 0), done]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    log_path = None

    def log_message(self, *args):
        pass

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        return self._json(404, {"code": 404, "msg": "no route %s" % self.path})

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._json(400, {"code": 400, "msg": "bad json"})
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"path": path, "headers": dict(self.headers),
                                     "body": body}, ensure_ascii=False) + "\n")
        if path != "/v2/chat/completions":
            return self._json(404, {"code": 404, "msg": "no route %s" % path})
        model = body.get("model") or "unknown"
        marker = _marker(body.get("messages"))
        if marker.startswith("ERR:"):
            _, status, payload = marker.split(":", 2)
            try:
                return self._json(int(status), json.loads(payload))
            except ValueError:
                return self._json(int(status), {"code": int(status), "msg": payload})
        case = marker[len("PROBE:"):] if marker.startswith("PROBE:") else ""
        parts = stream_parts(case, model)
        promised = sum(len(p) for p, _ in parts)
        if case in ("abort", "abort0"):
            # Promise more than is written: the peer sees an incomplete read,
            # exactly like a stream that dies mid-body.
            promised += 4096
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(promised))
        self.end_headers()
        for part, delay in parts:
            if delay:
                time.sleep(delay)
            self.wfile.write(part)
            self.wfile.flush()
            time.sleep(0.02)
        if case in ("abort", "abort0"):
            self.close_connection = True
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=9555)
    ap.add_argument("--log", default="")
    args = ap.parse_args(argv)
    Handler.log_path = args.log or None
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("probe upstream on http://127.0.0.1:%d (log: %s)"
          % (args.port, args.log or "-"))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
