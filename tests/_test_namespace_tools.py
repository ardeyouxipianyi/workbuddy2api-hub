"""Deterministic tests for Codex App namespace tool support.

No network: the namespace expansion, the name resolution and the outbound
stamping are pure transformations, so every case feeds a synthetic payload and
asserts the shape that leaves for the upstream and the shape that returns to
the client.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ACCOUNTS_DIR",
                      os.path.join(os.path.dirname(os.path.abspath(__file__)), "_acc"))
os.environ.setdefault("USAGE_DIR",
                      os.path.join(os.path.dirname(os.path.abspath(__file__)), "_use"))

import wb_proxy as P

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


CUSTOM_TOOL = {
    "type": "custom",
    "name": "apply_patch",
    "description": "Use the patch format to edit files",
    "format": {"type": "grammar", "syntax": "lark", "definition": "start: /.*/s"},
}
FUNC_TOOL = {
    "type": "function",
    "name": "get_weather",
    "description": "weather",
    "parameters": {"type": "object", "properties": {}},
}
WEB_TOOL = {"type": "web_search"}
JS_TOOL = {
    "name": "js",
    "description": "run javascript",
    "parameters": {"type": "object", "properties": {"code": {"type": "string"}}},
}
SPAWN_TOOL = {
    "name": "spawn_agent",
    "parameters": {"type": "object", "properties": {"message": {"type": "string"}}},
}
PROFILE_TOOL = {
    "type": "function",
    "name": "_get_profile",
    "description": "github profile",
    "parameters": {"type": "object", "properties": {}},
}
NAMESPACES = [
    {"type": "namespace", "name": "node_repl", "tools": [JS_TOOL]},
    {"type": "namespace", "name": "multi_agent_v1", "children": [SPAWN_TOOL]},
    {"type": "namespace", "name": "codex_apps__github", "functions": [PROFILE_TOOL]},
]

print("[1] outbound: namespace tools are flattened, the rest is untouched")
flat, ns_map = P.expand_namespace_tools([CUSTOM_TOOL, WEB_TOOL, FUNC_TOOL] + NAMESPACES)
names = [t.get("name") or (t.get("function") or {}).get("name") for t in flat]
check("namespace children are pulled out as tools",
      {"js", "spawn_agent", "_get_profile"}.issubset(set(names)), names)
check("tools/children/functions are all recognised",
      len(ns_map) == 3, ns_map)
check("namespace mapping keeps the namespace name",
      ns_map.get("js") == "node_repl" and ns_map.get("spawn_agent") == "multi_agent_v1"
      and ns_map.get("_get_profile") == "codex_apps__github", ns_map)
check("custom tool survives the expansion",
      any(t.get("name") == "apply_patch" for t in flat), flat[:2])
check("ordinary function tool survives the expansion",
      any(t.get("name") == "get_weather" for t in flat))
check("non-function tool types are passed through untouched",
      any(t.get("type") == "web_search" for t in flat), flat)
check("top level order is preserved",
      names == ["apply_patch", None, "get_weather", "js", "spawn_agent", "_get_profile"],
      names)

print()
print("[2] outbound: duplicate tool names collapse to one entry")
flat_dup, _ = P.expand_namespace_tools([JS_TOOL] + NAMESPACES)
check("the top level definition wins",
      [t.get("name") for t in flat_dup].count("js") == 1, flat_dup)

print()
print("[3] outbound: the chat body carries the flattened tools")
chat = P.responses_to_chat({"model": "m", "input": "hi",
                            "tools": [CUSTOM_TOOL, WEB_TOOL, FUNC_TOOL] + NAMESPACES})
tools = chat["tools"]
by_name = {}
for t in tools:
    by_name[t.get("name") or (t.get("function") or {}).get("name")] = t
check("custom tool is downgraded to a function tool",
      by_name["apply_patch"]["type"] == "function"
      and "input" in by_name["apply_patch"]["parameters"]["properties"])
check("namespaced tool reaches the chat body",
      "js" in by_name and by_name["js"]["type"] == "function")
check("namespace map rides along for the return trip",
      chat.get("_namespace_map", {}).get("js") == "node_repl", chat.get("_namespace_map"))
check("web_search declaration is passed through untouched",
      any(t.get("type") == "web_search" for t in tools))
# The gateway used to turn this declaration into a function of its own and run
# it locally. It no longer does: the upstream has no server-side search tool
# (measured — declaring one leaves the model answering "I can't browse"), so
# the gateway forwards what the client sent instead of standing in for it.
check("the gateway no longer injects its own web_search function",
      not any((t.get("function") or {}).get("name") == "web_search" for t in tools),
      tools)
body = P.build_upstream_body(dict(chat))
up = {t.get("function", {}).get("name"): t for t in body["tools"] if isinstance(t, dict)}
check("upstream body wraps the flattened tools as chat functions",
      isinstance(up.get("js", {}).get("function"), dict)
      and isinstance(up["js"]["function"].get("parameters"), dict), up.get("js"))

print()
print("[4] return trip: the namespace is stamped back onto the tool call")
NS_MAP = {"js": "node_repl", "line": "codex_apps__github"}
for wire_name, bare, ns in (("js", "js", "node_repl"),
                            ("node_repl__js", "js", "node_repl"),
                            ("node_repl::js", "js", "node_repl"),
                            ("codex_apps__github__line", "line", "codex_apps__github"),
                            ("codex_apps__github::line", "line", "codex_apps__github"),
                            ("js2", "js2", "")):
    got_bare, got_ns = P.resolve_namespaced_name(wire_name, NS_MAP)
    check("%s -> (%s, %s)" % (wire_name, bare, ns or "no namespace"),
          (got_bare, got_ns) == (bare, ns), (got_bare, got_ns))
check("__ inside a namespace is not split at the wrong place",
      P.resolve_namespaced_name("codex_apps__github__line", {"line": "codex_apps__github"})
      == ("line", "codex_apps__github"))

print()
print("[5] return trip: function_call / custom_tool_call carry namespace + bare name")
items = [
    {"type": "function_call", "name": "js", "call_id": "c1"},
    {"type": "custom_tool_call", "name": "apply_patch", "call_id": "c2"},
    {"type": "function_call", "name": "node_repl__js", "call_id": "c3"},
    {"type": "message", "role": "assistant"},
]
fixed_items, fixed = P.apply_namespace_to_calls(items, {"js": "node_repl"})
check("only the namespaced calls are touched", fixed == 2, fixed)
check("bare call gets its namespace", fixed_items[0].get("namespace") == "node_repl"
      and fixed_items[0].get("name") == "js")
check("a non-namespaced tool is left alone", "namespace" not in fixed_items[1])
check("prefixed call is rewritten to the bare name",
      fixed_items[2].get("name") == "js" and fixed_items[2].get("namespace") == "node_repl",
      fixed_items[2])
already = [{"type": "function_call", "name": "js", "namespace": "node_repl", "call_id": "c9"}]
_, fixed_again = P.apply_namespace_to_calls(already, {"js": "node_repl"})
check("an already stamped call is not double counted", fixed_again == 0)

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
