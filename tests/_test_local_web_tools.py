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
rather than assumed.

Sections [12]-[17] cover the multi-engine rotation and fetch hardening:
invisible failover across the backend chain, labelled non-standard sources,
engine cooldown, cache and throttle, failure classification, the text-content
gate and the DNS / IP guards. Engine runs and HTTP responses are injected, so
the file stays offline. No network access required.
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
print("[4] redirects cannot bypass the URL guard")

with mock.patch.object(W, "_resolve_host_public", lambda host: (True, "")):
    redirect_handler = W._SafeRedirectHandler()
    public_request = W.urllib.request.Request("https://example.com/start")
    headers = {"Location": "/next"}
    safe_redirect = redirect_handler.redirect_request(
        public_request, None, 302, "Found", headers, "/next")
check("relative public redirects are resolved and allowed",
      safe_redirect.full_url == "https://example.com/next", safe_redirect.full_url)

blocked = []
for destination in (
        "http://127.0.0.1/admin", "http://10.0.0.5/", "http://192.168.1.1/",
        "http://169.254.169.254/", "http://172.16.0.1/", "http://localhost/"):
    try:
        redirect_handler.redirect_request(
            public_request, None, 302, "Found", {"Location": destination}, destination)
    except W._UnsafeRedirectError:
        blocked.append(True)
    else:
        blocked.append(False)
check("loopback, private, link-local and local-name redirect targets are refused",
      all(blocked), blocked)

class RedirectingOpener(object):
    def __init__(self, handler):
        self.handler = handler

    def open(self, req, timeout=None):
        return self.handler.redirect_request(
            req, None, 302, "Found", {"Location": "http://127.0.0.1/admin"},
            "http://127.0.0.1/admin")

def build_redirecting_opener(*handlers):
    safe_handlers = [h for h in handlers if isinstance(h, W._SafeRedirectHandler)]
    check("HTTP requests install the safe redirect handler", len(safe_handlers) == 1)
    return RedirectingOpener(safe_handlers[0])

with mock.patch.object(W.urllib.request, "build_opener", side_effect=build_redirecting_opener), \
        mock.patch.object(W, "_resolve_host_public", lambda host: (True, "")):
    redirect_result = W.fetch("https://example.com/start")
check("fetch reports a blocked redirect without opening its target",
      "redirect blocked" in redirect_result, redirect_result)

print()
print("[5] the streaming path: one created frame, a real completed frame")

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
    # The real one returns (response, account, effort); the follow-up callers
    # record the effort on the usage row and ignore it here.
    return FakeUpstream(sse(ANSWER_CHUNKS)), FakeAccount(), body.get("reasoning_effort")


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
        mock.patch.object(W, "execute", lambda name, args, **kwargs: executed.append((name, args)) or
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
print("[6] when the rounds run out the tools are withdrawn, not faked")

calls = {"n": 0}


def always_tool_body(body, session_key=None, target_realm=None):
    calls["n"] += 1
    calls.setdefault("bodies", []).append(body)
    if calls["n"] > W.MAX_WEB_ROUNDS:
        # out of rounds: the gateway must have taken the tools away, so this
        # answer is a real one and the stream can end normally.
        return FakeUpstream(sse(ANSWER_CHUNKS)), FakeAccount(), body.get("reasoning_effort")
    return FakeUpstream(sse(TOOL_CHUNKS)), FakeAccount(), body.get("reasoning_effort")


handler2 = FakeHandler()
with mock.patch.multiple(proxy,
                         open_upstream=always_tool_body,
                         record_usage=lambda *a, **k: None,
                         record_error=lambda *a, **k: None), \
        mock.patch.object(W, "execute", lambda name, args, **kwargs: "Search results for: cats"):
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
print("[7] the search is visible as a card, and cited as a source")

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
print("[8] the streaming answer carries the web_search_call card")

card = [e for e in body.split("event: ") if e.startswith("response.output_item.done")]
check("a completed web_search_call item was emitted",
      any("'type': 'web_search_call'" in e or '"type": "web_search_call"' in e for e in card), card[-1:])
check("the card carries the query it ran",
      any("cats" in e for e in card if "web_search_call" in e), card[-1:])
check("the three lifecycle events were sent",
      all(("event: response.web_search_call." + s) in body for s in ("in_progress", "searching", "completed")))

print()
print("[9] the non-streaming path runs the tools too, instead of leaking the call")

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
    return FakeUpstream(sse(ANSWER_CHUNKS)), FakeAccount(), body.get("reasoning_effort")


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
        mock.patch.object(W, "execute", lambda name, args, **kwargs: SAMPLE):
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
print("[10] with the switch off the client's own call is forwarded, not run")

passthrough = []


def passthrough_open(body, session_key=None, target_realm=None):
    passthrough.append(body)
    return FakeUpstream(sse(ANSWER_CHUNKS)), FakeAccount(), body.get("reasoning_effort")


handler3 = FakeHandler()
ran = []
with mock.patch.multiple(proxy,
                         open_upstream=passthrough_open,
                         record_usage=lambda *a, **k: None,
                         record_error=lambda *a, **k: None), \
        mock.patch.object(W, "execute", lambda name, args, **kwargs: ran.append(name) or "no"), \
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
print("[11] the switch: settings, injection, and no private marker upstream")

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
print("[12] multi-engine rotation: a blocked engine is invisible to the model")


def make_engine(table, log):
    def _run(name, query, n, timeout):
        log.append(name)
        outcome = table.get(name) or {"state": "empty", "results": [], "detail": "no results"}
        result = dict(outcome)
        result["results"] = list(outcome.get("results") or [])
        return result
    return _run


W.reset_state()
runs = []
table = {
    "duckduckgo": {"state": "blocked", "results": [], "detail": "HTTP 202"},
    "bing": {"state": "ok", "results": [
        {"title": "Cats", "url": "https://example.com/cats", "snippet": "feline things"}]},
}
with mock.patch.object(W, "_run_engine", make_engine(table, runs)):
    text = W.search("fallback probe alpha 1", 5)
check("the blocked engine is tried first", runs[:2] == ["duckduckgo", "bing"], runs)
check("the healthy engine answers in the normal format",
      text.startswith("Search results for:") and "https://example.com/cats" in text, text)
check("nothing in the answer hints at the rotation",
      "bing" not in text.lower() and "unavailable" not in text.lower(), text)
check("the blocked engine went into cooldown", W._cooling("duckduckgo") is True)

runs2 = []
with mock.patch.object(W, "_run_engine", make_engine(table, runs2)):
    W.search("fallback probe alpha 2", 5)
check("a cooling engine is skipped while another is healthy", runs2[0] == "bing", runs2)
check("the default chain carries no chinese engines",
      "baidu" not in W.engine_names() and W.engine_names()[0] == "duckduckgo",
      W.engine_names())

print()
print("[12b] non-standard backends are named in the result")

W.reset_state()
runs3 = []
table3 = dict((name, {"state": "blocked", "results": [], "detail": "HTTP 403"})
              for name in W.engine_names())
table3["wikipedia"] = {"state": "ok", "results": [
    {"title": "Linux", "url": "https://en.wikipedia.org/wiki/Linux", "snippet": "kernel"}]}
with mock.patch.object(W, "_run_engine", make_engine(table3, runs3)):
    text3 = W.search("encyclopaedia probe bravo 3", 5)
check("the wikipedia fallback still returns the numbered list",
      text3.startswith("Search results for:") and "wikipedia.org" in text3, text3)
check("the model is told the backend is Wikipedia",
      "Wikipedia" in text3 and "tell the user" in text3.lower(), text3)
check("the fallback note does not break the sources parser",
      W.sources_from_result(text3) == [
          {"title": "Linux", "url": "https://en.wikipedia.org/wiki/Linux"}],
      W.sources_from_result(text3))

W.reset_state()
so_table = dict((name, {"state": "blocked", "results": [], "detail": "HTTP 403"})
                for name in W.engine_names())
so_table["stackoverflow"] = {"state": "ok", "results": [
    {"title": "How to asyncio?", "url": "https://stackoverflow.com/q/1", "snippet": "Q&A"}]}
with mock.patch.object(W, "_run_engine", make_engine(so_table, [])):
    so_text = W.search("asyncio probe bravo 4", 5)
check("stack overflow fallbacks are labelled too",
      "Stack Overflow" in so_text and "tell the user" in so_text.lower(), so_text)

print()
print("[13] a total outage is reported; an honest empty set keeps the old wording")

W.reset_state()
runs4 = []
blocked_all = dict((name, {"state": "blocked", "results": [], "detail": "HTTP 403"})
                   for name in W.engine_names())
with mock.patch.object(W, "_run_engine", make_engine(blocked_all, runs4)):
    text4 = W.search("outage probe charlie 5", 5)
check("every engine was tried before giving up", runs4 == list(W.engine_names()), runs4)
check("the model is told search is temporarily unavailable",
      text4.startswith("Error: web search is temporarily unavailable"), text4)
check("a failure is not dressed up as no results", "No results found" not in text4, text4)
check("the model is told not to retry the same query",
      "do not retry" in text4.lower(), text4)

W.reset_state()
empty_all = dict((name, {"state": "empty", "results": [], "detail": "no results"})
                 for name in W.engine_names())
with mock.patch.object(W, "_run_engine", make_engine(empty_all, [])):
    text5 = W.search("nothing matches this probe delta 6", 5)
check("a genuinely empty search keeps the old wording",
      text5.startswith("No results found for:") and "Error" not in text5, text5)

W.reset_state()
mixed = dict((name, {"state": "blocked", "results": [], "detail": "HTTP 429"})
             for name in W.engine_names())
mixed["news"] = {"state": "empty", "results": [], "detail": "no results"}
with mock.patch.object(W, "_run_engine", make_engine(mixed, [])):
    text6 = W.search("mixed probe echo 7", 5)
check("one honest empty answer beats a blanket outage claim",
      text6.startswith("No results found for:"), text6)

print()
print("[14] cache and throttle keep repeated work off the wire")

W.reset_state()
runs5 = []
table5 = {"duckduckgo": {"state": "ok", "results": [
    {"title": "T", "url": "https://example.com/t", "snippet": "s"}]}}
with mock.patch.object(W, "_run_engine", make_engine(table5, runs5)):
    first = W.search("cached probe foxtrot 8", 5)
    second = W.search("cached probe foxtrot 8", 5)
check("the second identical search hits the cache", runs5 == ["duckduckgo"], runs5)
check("the cached answer is identical", first == second)

W.reset_state()
runs6 = []
with mock.patch.object(W, "CACHE_TTL", 0.0), \
        mock.patch.object(W, "_run_engine", make_engine(table5, runs6)):
    W.search("uncached probe golf 9", 5)
    W.search("uncached probe golf 9", 5)
check("with the TTL off both searches hit the engines",
      runs6 == ["duckduckgo", "duckduckgo"], runs6)

sleeps = []
with mock.patch.object(W, "MIN_INTERVAL", 5.0), \
        mock.patch.object(W.time, "monotonic", side_effect=[100.0, 100.0]), \
        mock.patch.object(W.time, "sleep", lambda s: sleeps.append(s)):
    W._THROTTLE_LAST[0] = 0.0
    W._throttle()
    W._throttle()
check("back-to-back requests are spaced by the interval",
      len(sleeps) == 1 and sleeps[0] >= 4.9, sleeps)
print()
print("[15] failure classification: anti-bot pages, 202 and HTTP errors")

import urllib.error as _urlerr

DDG_BLOCK_PAGE = ('<html><body><div class="anomaly-modal">Unfortunately, bots use '
                  'DuckDuckGo too. If this error persists, contact us.</div></body></html>')
check("a duckduckgo anti-bot page is recognised",
      W._page_looks_blocked(DDG_BLOCK_PAGE, W._ENGINES["duckduckgo"]) is True)
check("an ordinary result page is not flagged",
      W._page_looks_blocked("<html><body>fresh web results about cats</body></html>",
                            W._ENGINES["duckduckgo"]) is False)


def fake_open(response):
    def _open(url, timeout=0, max_bytes=0, **kw):
        return response
    return _open


with mock.patch.object(W, "_open_url", fake_open(
        (DDG_BLOCK_PAGE.encode("utf-8"), 202, "text/html", "utf-8"))):
    blocked = W._run_engine("duckduckgo", "cats", 5, 5)
check("202 plus an anti-bot page classifies as blocked",
      blocked["state"] == "blocked", blocked)

with mock.patch.object(W, "_open_url", fake_open(
        (b"<html><body>nothing here</body></html>", 200, "text/html", "utf-8"))):
    empty = W._run_engine("duckduckgo", "cats", 5, 5)
check("a clean page with no blocks stays empty, not blocked",
      empty["state"] == "empty", empty)

with mock.patch.object(W, "_open_url", side_effect=_urlerr.HTTPError(
        "https://html.duckduckgo.com/html/", 429, "Too Many Requests", {}, None)):
    limited = W._run_engine("duckduckgo", "cats", 5, 5)
check("HTTP 429 classifies as blocked",
      limited["state"] == "blocked" and "429" in limited["detail"], limited)

with mock.patch.object(W, "_open_url", side_effect=_urlerr.HTTPError(
        "https://www.bing.com/search", 500, "Server Error", {}, None)):
    broken = W._run_engine("bing", "cats", 5, 5)
check("HTTP 500 classifies as an error",
      broken["state"] == "error" and "500" in broken["detail"], broken)

print()
print("[15b] the engine parsers read the shapes the live sites return")

import base64 as _b64

_bing_url = "https://example.com/bing-target"
_wrapped = "a1" + _b64.urlsafe_b64encode(_bing_url.encode("utf-8")).decode("ascii").rstrip("=")
bing_page = ('<ol id="b_results"><li class="b_algo"><h2><a href="https://www.bing.com/ck/a?x=1&amp;u='
             + _wrapped + '">Bing Title &amp; Co</a></h2><p>Bing snippet.</p></li></ol>')
parsed_bing = W._parse_bing(bing_page, 5, "q")
check("bing result blocks are parsed", len(parsed_bing) == 1, parsed_bing)
check("bing redirect links are unwrapped",
      parsed_bing and parsed_bing[0]["url"] == _bing_url, parsed_bing)
check("bing titles and snippets survive entities",
      parsed_bing and parsed_bing[0]["title"] == "Bing Title & Co"
      and parsed_bing[0]["snippet"] == "Bing snippet.", parsed_bing)

ddg_page = ('<div class="result"><div class="links_main result__body">'
            '<h2 class="result__title"><a class="result__a" rel="nofollow" '
            'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fddg&amp;rut=1">'
            'DDG Title</a></h2><a class="result__snippet">DDG snippet.</a></div></div>')
parsed_ddg = W._parse_duckduckgo(ddg_page, 5, "q")
check("duckduckgo redirect links are unwrapped",
      parsed_ddg and parsed_ddg[0]["url"] == "https://example.com/ddg", parsed_ddg)

wiki_page = json.dumps({"query": {"search": [
    {"title": "Linux", "snippet": '<span class="searchmatch">Linux</span> is a kernel'}]}})
parsed_wiki = W._parse_wikipedia(wiki_page, 5, "linux")
check("wikipedia hits become article urls",
      parsed_wiki and parsed_wiki[0]["url"] == "https://en.wikipedia.org/wiki/Linux", parsed_wiki)
parsed_wiki_zh = W._parse_wikipedia(wiki_page, 5, u"核心")
check("cjk queries read the chinese wikipedia",
      parsed_wiki_zh and parsed_wiki_zh[0]["url"].startswith("https://zh.wikipedia.org/"),
      parsed_wiki_zh)
check("wiki snippets lose their markup",
      parsed_wiki and "<span" not in parsed_wiki[0]["snippet"], parsed_wiki)

so_page = json.dumps({"items": [
    {"title": "How do I use asyncio?", "link": "https://stackoverflow.com/q/1",
     "score": 42, "answer_count": 3, "tags": ["python", "asyncio"]}]})
parsed_so = W._parse_stackoverflow(so_page, 5, "asyncio")
check("stack overflow items carry link and stats",
      parsed_so and parsed_so[0]["url"] == "https://stackoverflow.com/q/1"
      and "score 42" in parsed_so[0]["snippet"] and "asyncio" in parsed_so[0]["snippet"],
      parsed_so)

news_page = ("<rss><channel><item><title>Big News - Example</title>"
             "<link>https://news.example.com/a</link>"
             "<pubDate>Thu, 08 Oct 2026 21:53:08 GMT</pubDate></item></channel></rss>")
parsed_news = W._parse_news(news_page, 5, "news")
check("news items parse with their date",
      parsed_news and parsed_news[0]["url"] == "https://news.example.com/a"
      and "2026" in parsed_news[0]["snippet"], parsed_news)

print()
print("[16] web_fetch: text gate, gzip, charset and the DNS guard")

import gzip as _gzip


class FakeResponse(object):
    def __init__(self, body, ctype="text/html; charset=utf-8", encoding="", status=200):
        self._body = body
        self.status = status
        self.headers = {"Content-Type": ctype}
        if encoding:
            self.headers["Content-Encoding"] = encoding

    def read(self, n=-1):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener(object):
    def __init__(self, response):
        self.response = response

    def open(self, req, timeout=None):
        return self.response


W.reset_state()
page_body = "<html><body>Hello <b>World</b></body></html>".encode("utf-8")
with mock.patch.object(W.urllib.request, "build_opener",
                       lambda *handlers: FakeOpener(FakeResponse(
                           _gzip.compress(page_body), "text/html; charset=utf-8", "gzip"))), \
        mock.patch.object(W, "_resolve_host_public", lambda host: (True, "")):
    gz_out = W.fetch("https://example.com/gz-page")
check("gzip bodies are decompressed before reading",
      "Hello" in gz_out and "URL: https://example.com/gz-page" in gz_out, gz_out)

with mock.patch.object(W.urllib.request, "build_opener",
                       lambda *handlers: FakeOpener(FakeResponse(b"%PDF-1.7 fake", "application/pdf"))), \
        mock.patch.object(W, "_resolve_host_public", lambda host: (True, "")):
    pdf_out = W.fetch("https://example.com/file.pdf")
check("binary payloads are refused with a readable error",
      pdf_out.startswith("Error:") and "application/pdf" in pdf_out, pdf_out)

meta = '<html><head><meta charset="big5"></head><body>中文</body></html>'.encode("big5")
with mock.patch.object(W.urllib.request, "build_opener",
                       lambda *handlers: FakeOpener(FakeResponse(meta, "text/html"))), \
        mock.patch.object(W, "_resolve_host_public", lambda host: (True, "")):
    meta_out = W.fetch("https://example.com/big5-page")
check("the charset falls back to the meta tag", u"中文" in meta_out, meta_out)


def fake_gai_loopback(host, port, *args, **kwargs):
    return [(2, 1, 6, "", ("127.0.0.1", 0))]


with mock.patch.object(W.socket, "getaddrinfo", fake_gai_loopback):
    _u, problem = W._guard_url("http://rebind.example/x", resolve=True)
check("a host that resolves into loopback is refused", bool(problem), problem)
with mock.patch.object(W.socket, "getaddrinfo",
                       lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]):
    _u, problem = W._guard_url("http://public.example/x", resolve=True)
check("a public resolution passes the guard", problem == "", problem)

check("ipv6 loopback is not public", W._ip_is_public("::1") is False)
check("ipv4-mapped loopback is not public", W._ip_is_public("::ffff:127.0.0.1") is False)
check("telecom CGNAT is not public", W._ip_is_public("100.64.0.1") is False)
check("a public resolver address is public", W._ip_is_public("2001:4860:4860::8888") is True)

print()
print("[17] the web tools egress proxy is configurable")

with mock.patch.dict(os.environ, {"WB_WEB_TOOLS_PROXY": "user:pass@127.0.0.1:8080"}):
    check("a scheme-less proxy value is normalised",
          W._egress_proxy() == "http://user:pass@127.0.0.1:8080", W._egress_proxy())
with mock.patch.dict(os.environ, {"WB_WEB_TOOLS_PROXY": "http://proxy.example:3128"}):
    check("a full proxy URL is kept as given",
          W._egress_proxy() == "http://proxy.example:3128", W._egress_proxy())
with mock.patch.dict(os.environ, {}, clear=False):
    os.environ.pop("WB_WEB_TOOLS_PROXY", None)
    check("no proxy configured means direct", W._egress_proxy() == "", W._egress_proxy())

captured = {}


def fake_build(*handlers):
    captured["handlers"] = handlers
    return FakeOpener(FakeResponse(b"<html>ok</html>"))


W.reset_state()
with mock.patch.object(W.urllib.request, "build_opener", fake_build), \
        mock.patch.dict(os.environ, {"WB_WEB_TOOLS_PROXY": "http://proxy.example:3128"}), \
        mock.patch.object(W, "_resolve_host_public", lambda host: (True, "")):
    W.fetch("https://example.com/proxy-check")
kinds = [type(h).__name__ for h in captured.get("handlers") or ()]
check("the proxy handler is installed for web tools",
      "ProxyHandler" in kinds and "_SafeRedirectHandler" in kinds, kinds)
print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
