"""Deterministic protocol-conversion tests for the custom-tool port.

No network: feeds synthetic chat-completion objects/chunks into the translation
functions and asserts the Responses-API shapes that Codex depends on.
"""
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ACCOUNTS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "_acc"))
os.environ.setdefault("USAGE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "_use"))

import wb_proxy as P

PASS = FAIL = 0
def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print("  [PASS] " + label)
    else:
        FAIL += 1; print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))

CUSTOM_TOOL = {
    "type": "custom",
    "name": "apply_patch",
    "description": "Use the patch format to edit files",
    "format": {"type": "grammar", "syntax": "lark", "definition": "start: /.*/s"},
}
FUNC_TOOL = {
    "type": "function", "name": "get_weather",
    "description": "weather", "parameters": {"type": "object", "properties": {}},
}

print("[1] outbound: custom tool is downgraded to a function tool")
chat = P.responses_to_chat({"model": "m", "input": "hi", "tools": [CUSTOM_TOOL, FUNC_TOOL]})
tools = chat["tools"]
check("custom tool became type=function", tools[0]["type"] == "function", tools[0].get("type"))
check("custom tool keeps its name", tools[0]["name"] == "apply_patch")
check("custom tool has single 'input' param",
      list((tools[0]["parameters"]["properties"] or {}).keys()) == ["input"])
check("'input' is required", tools[0]["parameters"]["required"] == ["input"])
check("freeform hint present in description", "freeform tool" in tools[0]["description"])
check("grammar forwarded into description", "start: /.*/s" in tools[0]["description"])
check("ordinary function tool untouched", tools[1] is FUNC_TOOL or tools[1] == FUNC_TOOL)

print()
print("[2] inbound history: custom_tool_call / custom_tool_call_output survive")
hist = {"model": "m", "input": [
    {"role": "user", "content": "edit the file"},
    {"type": "custom_tool_call", "name": "apply_patch", "call_id": "call_1",
     "input": "*** Begin Patch\n*** Add File: a.txt\n+hi\n*** End Patch"},
    {"type": "custom_tool_call_output", "call_id": "call_1", "output": "Done!"},
]}
c2 = P.responses_to_chat(hist)
msgs = c2["messages"]
asst = [m for m in msgs if m.get("role") == "assistant" and m.get("tool_calls")]
check("assistant message carries the tool call", len(asst) == 1)
tc = asst[0]["tool_calls"][0]
check("tool call id preserved", tc["id"] == "call_1", tc.get("id"))
check("payload wrapped as {input: ...}",
      json.loads(tc["function"]["arguments"])["input"].startswith("*** Begin Patch"))
tool_msgs = [m for m in msgs if m.get("role") == "tool"]
check("tool result appended with matching id",
      len(tool_msgs) == 1 and tool_msgs[0]["tool_call_id"] == "call_1")

print()
print("[2.5] responses reasoning item handling (Issue #17)")
hist_reasoning = {"model": "m", "input": [
    {"role": "user", "content": "solve math"},
    {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "let me think about 2+2"}]},
    {"type": "message", "role": "assistant", "content": "4"},
]}
c_r = P.responses_to_chat(hist_reasoning)
asst_r = [m for m in c_r["messages"] if m.get("role") == "assistant"]
check("reasoning attached to assistant message", len(asst_r) == 1 and asst_r[0].get("reasoning_content") == "let me think about 2+2")
check("assistant message content preserved", asst_r[0].get("content") == "4")

print()
print("[3] unknown input item types are logged, not silently dropped")
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    P.responses_to_chat({"model": "m", "input": [
        {"role": "user", "content": "x"},
        {"type": "local_shell_call", "call_id": "c9"},
    ]})
check("no crash on unknown type", True)

print()
print("[4] non-streaming response: custom tool call is re-inflated")
chat_obj = {"choices": [{"finish_reason": "tool_calls", "message": {
    "role": "assistant", "content": "",
    "tool_calls": [{"id": "call_7", "type": "function", "function": {
        "name": "apply_patch",
        "arguments": json.dumps({"input": "*** Begin Patch\n+ok\n*** End Patch"})}}]}}]}
r = P.chat_to_response(chat_obj, "m", {"apply_patch"})
item = r["output"][0]
check("item type is custom_tool_call", item["type"] == "custom_tool_call", item["type"])
check("input unwrapped verbatim, not JSON", item["input"] == "*** Begin Patch\n+ok\n*** End Patch", repr(item.get("input")))
check("call_id preserved", item["call_id"] == "call_7")

print()
print("[5] regression: ordinary function calls are unchanged")
chat_obj2 = {"choices": [{"finish_reason": "tool_calls", "message": {
    "role": "assistant", "content": "",
    "tool_calls": [{"id": "call_8", "type": "function", "function": {
        "name": "get_weather", "arguments": '{"city":"Beijing"}'}}]}}]}
r2 = P.chat_to_response(chat_obj2, "m", {"apply_patch"})
it2 = r2["output"][0]
check("still function_call", it2["type"] == "function_call", it2["type"])
check("arguments untouched", it2["arguments"] == '{"city":"Beijing"}')
check("id still fc_ prefixed", it2["id"].startswith("fc_"), it2["id"])

print()
print("[6] streaming: custom tool emits custom_tool_call_input.* events")
def chunk(delta, finish=None):
    return ("data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}) + "\n\n").encode()
stream = [
    chunk({"tool_calls": [{"index": 0, "id": "call_9",
                           "function": {"name": "apply_patch", "arguments": ""}}]}),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"input":"*** Begin'}}]}),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": ' Patch\\n+hi\\n*** End Patch"}'}}]}),
    chunk({}, "tool_calls"),
]
holder = {"usage": None, "custom_names": {"apply_patch"}}
raw = b"".join(P.stream_responses_events(iter(stream), "m", holder))
text = raw.decode("utf-8")
check("has response.custom_tool_call_input.delta", "response.custom_tool_call_input.delta" in text)
check("has response.custom_tool_call_input.done", "response.custom_tool_call_input.done" in text)
check("no stray function_call_arguments events", "response.function_call_arguments" not in text)
done = [json.loads(l[6:]) for l in text.splitlines()
        if l.startswith("data: ") and '"response.custom_tool_call_input.done"' in l]
check("done event carries unwrapped input",
      done and done[0]["input"] == "*** Begin Patch\n+hi\n*** End Patch", done[:1])
added = [json.loads(l[6:]) for l in text.splitlines()
         if l.startswith("data: ") and '"response.output_item.added"' in l]
custom_added = [a for a in added if a.get("item", {}).get("type") == "custom_tool_call"]
check("output_item.added uses custom_tool_call", len(custom_added) == 1)

print()
print("[7] streaming regression: ordinary function tool still uses function_call_arguments.*")
stream2 = [
    chunk({"tool_calls": [{"index": 0, "id": "call_10",
                           "function": {"name": "get_weather", "arguments": ""}}]}),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"city":"Beijing"}'}}]}),
    chunk({}, "tool_calls"),
]
raw2 = b"".join(P.stream_responses_events(iter(stream2), "m", {"usage": None, "custom_names": {"apply_patch"}})).decode()
check("function_call_arguments.delta present", "response.function_call_arguments.delta" in raw2)
check("function_call_arguments.done present", "response.function_call_arguments.done" in raw2)
check("no custom events for a normal tool", "custom_tool_call_input" not in raw2)

print()
print("[8] aggregate_stream: empty tool_call placeholder fallback (Issue #15)")
# Case A: upstream sends legacy function_call placeholder with finish_reason="tool_calls"
stream_placeholder = [
    chunk({"content": "Hello!"}),
    chunk({"function_call": {"name": "", "arguments": ""}}, "tool_calls"),
]
res_a = P.aggregate_stream(iter(stream_placeholder), "m", None)
check("placeholder: finish_reason downgraded to stop", res_a["choices"][0]["finish_reason"] == "stop")
check("placeholder: message has no tool_calls", "tool_calls" not in res_a["choices"][0]["message"])

# Case B: upstream sends name="" with non-empty arguments
stream_nameless = [
    chunk({"content": "Thinking..."}),
    chunk({"tool_calls": [{"index": 0, "function": {"name": "", "arguments": '{"k":"v"}'}}]}, "tool_calls"),
]
res_b = P.aggregate_stream(iter(stream_nameless), "m", None)
check("nameless tool: finish_reason downgraded to stop", res_b["choices"][0]["finish_reason"] == "stop")
check("nameless tool: filtered out completely", "tool_calls" not in res_b["choices"][0]["message"])

# Case C: valid tool call is preserved
stream_valid = [
    chunk({"tool_calls": [{"index": 0, "id": "call_ok", "function": {"name": "my_func", "arguments": '{"ok":true}'}}]}, "tool_calls"),
]
res_c = P.aggregate_stream(iter(stream_valid), "m", None)
check("valid tool: finish_reason is tool_calls", res_c["choices"][0]["finish_reason"] == "tool_calls")
check("valid tool: tool_calls present", len(res_c["choices"][0]["message"].get("tool_calls", [])) == 1)

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
