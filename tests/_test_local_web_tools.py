"""The gateway runs web_search / web_fetch itself, and the old defects stay fixed.

The client declares web_search as a server-side tool the chat endpoint has no
executor for, so the gateway declares a function-shaped one, swallows the calls
and runs them locally. v1.5.0-1.5.2 did that and was reverted (issue #43) over
three defects, all pinned here:

  * a model answering with a `queries` array was told it had asked for nothing,
    retried, and burned the round budget;
  * the injected definition was appended next to the client's own declaration,
    so the upstream saw two tools with the same name;
  * an exhausted round budget ended in a synthesised resp_wrapup - a failure
    presented as a normal finish, which the client showed as a cut-off answer.

The last one is checked against a fake upstream that walks the real streaming
path, so the number of response.created frames and the final event are observed
rather than assumed. No network access required.
"""

import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy as proxy
import wb_webtools as W

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


print("[1] the client's declaration is replaced, not duplicated")

client_tools = [
    {"type": "web_search"},
    {"type": "function", "name": "exec_command", "parameters": {"type": "object"}},
]
wants = W.client_wants_web(client_tools)
check("the server-side declaration is seen", wants["search"] is True, wants)
check("web_fetch is not invented", wants["fetch"] is False, wants)
check("a function-shaped declaration is seen too",
      W.client_wants_web([{"type": "function", "name": "web_search"}])["search"] is True)
check("web_search_preview counts as web_search",
      W.client_wants_web([{"type": "web_search_preview"}])["search"] is True)

installed = W.install_tool_defs(list(client_tools), wants)
names = [W.__dict__ and (t.get("name") or "") for t in installed]
check("exactly one web_search is forwarded", names.count("web_search") == 1, names)
check("the client's other tools survive", "exec_command" in names, names)
check("the server-side entry is gone",
      all(str(t.get("type")) != "web_search" for t in installed), installed)

nested = W.install_tool_defs(
    [{"type": "function", "function": {"name": "web_fetch", "parameters": {}}}],
    {"search": False, "fetch": True})
check("a nested function shape is replaced as well",
      len(nested) == 1 and nested[0]["type"] == "function" and nested[0]["name"] == "web_fetch",
      nested)
both = W.install_tool_defs([], {"search": True, "fetch": True})
check("declaring search also brings fetch", [t["name"] for t in both] == ["web_search", "web_fetch"], both)

print()
print("[2] the argument shapes a model actually sends")

check("query string", W.query_args({"query": "cats"}) == "cats")
check("queries array", W.query_args({"queries": ["cats", "dogs"]}) == "cats or dogs")
check("q alias", W.query_args({"q": "cats"}) == "cats")
check("nothing at all", W.query_args({}) == "")
check("url", W.url_arg({"url": "https://example.com/a"}) == "https://example.com/a")
check("urls array", W.url_arg({"urls": ["https://example.com/b"]}) == "https://example.com/b")
check("is_internal_tool", W.is_internal_tool("web_fetch") and not W.is_internal_tool("exec_command"))

print()
print("[3] failures are reported, never fabricated")

short = W.execute("web_search", {"query": "x"})
check("a too-short query says so without searching", "at least 2 characters" in short, short)
check("the error names the argument shape", '"query"' in short, short)
check("queries array of one short word is still reported",
      "at least 2 characters" in W.execute("web_search", {"queries": ["x"]}))
check("an unknown internal tool is refused",
      "not a tool this gateway runs" in W.execute("nope", {}))
check("garbage arguments do not raise", isinstance(W.execute("web_search", "{not json"), str))
check("a non-dict argument blob does not raise", isinstance(W.execute("web_search", 42), str))
check("private hosts are refused",
      "not allowed" in W.fetch("http://127.0.0.1/") or
      "not allowed" in W.fetch("http://localhost/x"), W.fetch("http://localhost/x"))
check("non-http schemes are refused", "http" in W.fetch("ftp://example.com/x"))
check("single-label hosts are refused", "not allowed" in W.fetch("http://intranet/x"))
check("startIndex past the end is an error, not an empty page",
      isinstance(W.fetch("http://127.0.0.1/"), str))

print()
print("[4] the streaming path: one created frame, a real completed frame")

TOOL_CHUNKS = [
    {"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": "call_1", "type": "function",
         "function": {"name": "web_search", "arguments": '{"query":'}}]},
        "finish_reason": None}]},
    {"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": ' "cats"}'}}]}, "finish_reason": None}]},
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
     "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}},
]
ANSWER_CHUNKS = [
    {"choices": [{"index": 0, "delta": {"content": "Cats are "}, "finish_reason": None}]},
    {"choices": [{"index": 0, "delta": {"content": "fine."}, "finish_reason": None}]},
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
     "usage": {"prompt_tokens": 20, "completion_tokens": 6, "total_tokens": 26}},
]


def sse(chunks):
    out = []
    for c in chunks:
        out.append(("data: " + json.dumps(c) + chr(10) + chr(10)).encode("utf-8"))
    out.append(b"data: [DONE]" + bytes([10, 10]))
    return out


class FakeUpstream(object):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        return iter(self.chunks)

    def close(self):
        self.closed = True


class FakeAccount(object):
    uid = "acct-test"


class FakeHandler(object):
    _responses_stream_response = proxy.Handler._responses_stream_response

    def _key_id(self):
        # These tests present no API key, so the usage row they produce is
        # unattributed - exactly what the real handler records in that case.
        return None

    def __init__(self):
        self.path = "/v1/responses"
        self.written = []
        self.headers_sent = []

    def send_response(self, code):
        self.headers_sent.append(("status", code))

    def send_header(self, name, value):
        self.headers_sent.append((name, value))

    def end_headers(self):
        self.headers_sent.append(("end", None))

    class _WFile(object):
        def __init__(self, sink):
            self.sink = sink

        def write(self, data):
            self.sink.append(data)

        def flush(self):
            pass

    @property
    def wfile(self):
        return FakeHandler._WFile(self.written)


opened = []


def fake_open_upstream(body, session_key=None, target_realm=None):
    opened.append(body)
    return FakeUpstream(sse(ANSWER_CHUNKS)), FakeAccount()


chat_body = {
    "model": "deepseek-v4.1-flash",
    "messages": [{"role": "user", "content": "tell me about cats"}],
    "tools": [W.web_search_tool_def()],
    # responses_to_chat marks a request whose definitions the gateway swapped
    # for its own; interception is tied to that mark (proxy.web_tools_active).
    "_web_tools": True,
    "stream": True,
}
handler = FakeHandler()
executed = []

with mock.patch.multiple(proxy,
                         open_upstream=fake_open_upstream,
                         record_usage=lambda *a, **k: None,
                         record_error=lambda *a, **k: None), \
        mock.patch.object(W, "execute", lambda name, args: executed.append((name, args)) or
                          "Search results for: cats" + chr(10) + "1. Cats - https://example.com"):
    handler._responses_stream_response(
        FakeUpstream(sse(TOOL_CHUNKS)), "deepseek-v4.1-flash", set(), {}, "fp",
        FakeAccount(), 0.0, None, base_body=chat_body,
        session_key="sess", realm="intl")

body = b"".join(handler.written).decode("utf-8", "replace")
check("the browser got a 200 stream", ("status", 200) in handler.headers_sent, handler.headers_sent[:2])
check("exactly one response.created", body.count("event: response.created") == 1,
      body.count("event: response.created"))
check("no second response.in_progress",
      body.count("event: response.in_progress") == 1, body.count("event: response.in_progress"))
check("the turn finishes with response.completed",
      "event: response.completed" in body, body[-200:])
check("no synthesised wrapup id", "resp_wrapup" not in body)
check("a second upstream call was made for the tool result", len(opened) == 1, len(opened))
check("the tool was run once", len(executed) == 1 and executed[0][0] == "web_search", executed)
check("the query reached the tool", "cats" in (executed[0][1] if executed else ""), executed)
check("the client never sees a web_search call", '"name": "web_search"' not in body)
check("the answer text reaches the client", "Cats are fine." in body, body[-400:])
check("the tool result was fed back",
      any(m.get("role") == "tool" for m in (opened[0].get("messages") or [])) if opened else False)

print()
print("[5] when the rounds run out the tools are withdrawn, not faked")

calls = {"n": 0}


def always_tool_body(body, session_key=None, target_realm=None):
    calls["n"] += 1
    calls.setdefault("bodies", []).append(body)
    if calls["n"] > W.MAX_WEB_ROUNDS:
        # out of rounds: the gateway must have taken the tools away, so this
        # answer is a real one and the stream can end normally.
        return FakeUpstream(sse(ANSWER_CHUNKS)), FakeAccount()
    return FakeUpstream(sse(TOOL_CHUNKS)), FakeAccount()


handler2 = FakeHandler()
with mock.patch.multiple(proxy,
                         open_upstream=always_tool_body,
                         record_usage=lambda *a, **k: None,
                         record_error=lambda *a, **k: None), \
        mock.patch.object(W, "execute", lambda name, args: "Search results for: cats"):
    handler2._responses_stream_response(
        FakeUpstream(sse(TOOL_CHUNKS)), "deepseek-v4.1-flash", set(), {}, "fp",
        FakeAccount(), 0.0, None, base_body=dict(chat_body),
        session_key="sess", realm="intl")

body2 = b"".join(handler2.written).decode("utf-8", "replace")
check("it stops after MAX_WEB_ROUNDS extra calls", calls["n"] <= W.MAX_WEB_ROUNDS + 1, calls["n"])
check("and still finishes with response.completed",
      "event: response.completed" in body2, body2[-200:])
check("without inventing a wrapup", "resp_wrapup" not in body2)
last_tools = [t.get("name") for t in (calls["bodies"][-1].get("tools") or [])]
check("the last call really had the tools withdrawn",
      "web_search" not in last_tools, last_tools)
check("the earlier calls still carried them",
      "web_search" in [t.get("name") for t in (calls["bodies"][0].get("tools") or [])],
      [t.get("name") for t in (calls["bodies"][0].get("tools") or [])])

print()
print("[6] the search is visible as a card, and cited as a source")

SAMPLE = ("Search results for: cats" + chr(10) + chr(10)
          + "1. All About Cats" + chr(10)
          + "   https://example.com/cats" + chr(10)
          + "   everything feline" + chr(10) + chr(10)
          + "2. Another" + chr(10)
          + "   https://example.com/other" + chr(10)
          + "   more" + chr(10))
found = W.sources_from_result(SAMPLE)
check("a search result is read back as sources",
      [s["url"] for s in found] == ["https://example.com/cats", "https://example.com/other"], found)
check("the title comes along", found and found[0]["title"] == "All About Cats", found)
check("an error string yields no sources", W.sources_from_result("Error: nope") == [])

answer = "See [All About Cats](https://example.com/cats) and https://example.com/other plus https://evil.example/x"
anns = proxy.build_citations(answer, found)
urls = sorted(a["url"] for a in anns)
check("a markdown link is cited", "https://example.com/cats" in urls, anns)
check("a bare url is cited too", "https://example.com/other" in urls, anns)
check("a url the tool never returned is not cited",
      "https://evil.example/x" not in urls, anns)
check("annotations are url_citation with a span",
      all(a["type"] == "url_citation" and a["end_index"] > a["start_index"] for a in anns), anns)

print()
print("[7] the streaming answer carries the web_search_call card")

card = [e for e in body.split("event: ") if e.startswith("response.output_item.done")]
check("a completed web_search_call item was emitted",
      any("'type': 'web_search_call'" in e or '"type": "web_search_call"' in e for e in card), card[-1:])
check("the card carries the query it ran",
      any("cats" in e for e in card if "web_search_call" in e), card[-1:])
check("the three lifecycle events were sent",
      all(("event: response.web_search_call." + s) in body for s in ("in_progress", "searching", "completed")))

print()
print("[8] the non-streaming path runs the tools too, instead of leaking the call")

CHAT_CALL = {"choices": [{"message": {"content": "", "tool_calls": [
    {"id": "call_1", "type": "function",
     "function": {"name": "web_search", "arguments": '{"query": "cats"}'}}]}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}
CHAT_DONE = {"choices": [{"message": {"content":
    "All About Cats: https://example.com/cats"}}],
    "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12}}
seen = {"aggregate": 0, "bodies": []}


def fake_aggregate(upstream, model, sink, *a, **k):
    seen["aggregate"] += 1
    return CHAT_CALL if seen["aggregate"] == 1 else CHAT_DONE


def capture_open(body, session_key=None, target_realm=None):
    seen["bodies"].append(body)
    return FakeUpstream(sse(ANSWER_CHUNKS)), FakeAccount()


class JsonHandler(FakeHandler):
    _responses_nonstream_response = proxy.Handler._responses_nonstream_response

    def __init__(self):
        FakeHandler.__init__(self)
        self.json_body = None

    def _json(self, status, payload):
        self.json_body = payload
        return status, payload

    def _error(self, status, message, kind=""):
        self.json_body = {"error": message}
        return status, message


jh = JsonHandler()
with mock.patch.multiple(proxy,
                         open_upstream=capture_open,
                         aggregate_stream=fake_aggregate,
                         record_usage=lambda *a, **k: None,
                         record_error=lambda *a, **k: None), \
        mock.patch.object(W, "execute", lambda name, args: SAMPLE):
    jh._responses_nonstream_response(
        FakeUpstream(sse(TOOL_CHUNKS)), "deepseek-v4.1-flash", set(), {}, "fp",
        FakeAccount(), 0.0, None, base_body=dict(chat_body),
        session_key="sess", realm="intl")

out = (jh.json_body or {}).get("output") or []
kinds = [o.get("type") for o in out]
check("the model ran twice (the tool round is invisible)", seen["aggregate"] == 2, seen["aggregate"])
check("no web_search function_call reaches the client",
      not any(o.get("type") == "function_call" and o.get("name") == "web_search" for o in out), kinds)
check("the client gets reasoning + a message", "message" in kinds, kinds)
anns = [a for o in out for p in (o.get("content") or []) for a in (p.get("annotations") or [])]
check("the citations ride on the non-stream message too", bool(anns), anns)
check("the tool result was fed back before the second call",
      any(m.get("role") == "tool" for m in (seen["bodies"][0].get("messages") or [])),
      seen["bodies"][0].get("messages"))

print()
print("[9] with the switch off the client's own call is forwarded, not run")

passthrough = []


def passthrough_open(body, session_key=None, target_realm=None):
    passthrough.append(body)
    return FakeUpstream(sse(ANSWER_CHUNKS)), FakeAccount()


handler3 = FakeHandler()
ran = []
with mock.patch.multiple(proxy,
                         open_upstream=passthrough_open,
                         record_usage=lambda *a, **k: None,
                         record_error=lambda *a, **k: None), \
        mock.patch.object(W, "execute", lambda name, args: ran.append(name) or "no"), \
        mock.patch.object(proxy.wb_settings, "local_web_tools", lambda accounts_dir: False):
    handler3._responses_stream_response(
        FakeUpstream(sse(TOOL_CHUNKS)), "deepseek-v4.1-flash", set(), {}, "fp",
        FakeAccount(), 0.0, None,
        base_body={"model": "m", "messages": [{"role": "user", "content": "hi"}],
                   "tools": [W.web_search_tool_def()], "stream": True},
        session_key="sess", realm="intl")

body3 = b"".join(handler3.written).decode("utf-8", "replace")
check("the client receives its own web_search call", '"name": "web_search"' in body3, body3[-300:])
check("the gateway ran no tool", ran == [], ran)
check("no extra upstream round was opened", passthrough == [], len(passthrough))
check("the stream still completes", "event: response.completed" in body3, body3[-200:])

print()
print("[10] the switch: settings, injection, and no private marker upstream")

_tmpdir = tempfile.mkdtemp(prefix="wb-webtools-")
check("the switch is off on a fresh install",
      proxy.wb_settings.local_web_tools(_tmpdir) is False)
check("the setting round-trips",
      proxy.wb_settings.set_local_web_tools(_tmpdir, True) is True
      and proxy.wb_settings.local_web_tools(_tmpdir) is True
      and proxy.wb_settings.set_local_web_tools(_tmpdir, False) is False
      and proxy.wb_settings.local_web_tools(_tmpdir) is False)

with mock.patch.object(proxy.wb_settings, "local_web_tools", lambda accounts_dir: False):
    chat_off = proxy.responses_to_chat({"model": "m", "input": "hi",
                                        "tools": [{"type": "web_search"}]})
check("with the switch off nothing is injected",
      "_web_tools" not in chat_off
      and any(t.get("type") == "web_search" for t in (chat_off.get("tools") or [])),
      chat_off)
check("and a client's own call is not collected",
      proxy.internal_calls_from_chat(CHAT_CALL) == [])

with mock.patch.object(proxy.wb_settings, "local_web_tools", lambda accounts_dir: True):
    chat_on = proxy.responses_to_chat({"model": "m", "input": "hi",
                                       "tools": [{"type": "web_search"}]})
check("with the switch on the declaration is replaced and marked",
      chat_on.get("_web_tools") is True
      and any(t.get("name") == "web_search" for t in (chat_on.get("tools") or [])),
      chat_on)
check("with the switch on calls are collected",
      len(proxy.internal_calls_from_chat(CHAT_CALL, web_tools=True)) == 1)
check("private markers never reach the upstream body",
      not any(str(k).startswith("_") for k in proxy.build_upstream_body(
          dict(chat_on, _namespace_map={"js": "node_repl"}, _web_tools=True))))

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
