"""Deterministic protocol-conversion tests for the custom-tool port.

No network: feeds synthetic chat-completion objects/chunks into the translation
functions and asserts the Responses-API shapes that Codex depends on.
"""
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _isolated_dirs import isolated_data_dirs  # noqa: E402  (its own data root)
# Its own directories, removed when this process exits. This suite used to share
# tests/_acc and tests/_use with _test_tool_choice_none.py, which made the two
# unsafe to run at the same time and left directories in the repo after a run.
_TMP = isolated_data_dirs("wb-custom-tools-")

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
normal_fc_events = [json.loads(l[6:]) for l in raw2.splitlines()
                    if l.startswith("data: ") and '"response.function_call_arguments.' in l]
check("ordinary function events carry item_id",
      normal_fc_events and all(e.get("item_id") for e in normal_fc_events), normal_fc_events)
check("no custom events for a normal tool", "custom_tool_call_input" not in raw2)

print()
print("[7.1] streaming: preserve arguments included in the first tool-call chunk")
for first_args, later_args in [
    ('{"city":"', ['Beijing"}']),
    ('{"city":"Beijing"}', []),
    ('', ['{"city":"Beijing"}']),
]:
    chunks = [chunk({"tool_calls": [{
        "index": 0, "id": "call_first", "type": "function",
        "function": {"name": "get_weather", "arguments": first_args},
    }]})]
    chunks.extend(chunk({"tool_calls": [{"index": 0, "function": {"arguments": part}}]})
                  for part in later_args)
    chunks.extend([chunk({}, "tool_calls"), b"data: [DONE]\n\n"])
    events = [json.loads(line[6:]) for line in b"".join(P.stream_responses_events(
        iter(chunks), "m", {"usage": None})).decode().splitlines()
        if line.startswith("data: ")]
    deltas = [e for e in events if e["type"] == "response.function_call_arguments.delta"]
    done = [e for e in events if e["type"] == "response.function_call_arguments.done"]
    prefix = "first arguments %r" % first_args
    check(prefix + ": deltas reconstruct the complete JSON exactly once",
          "".join(e["delta"] for e in deltas) == '{"city":"Beijing"}', deltas)
    check(prefix + ": delta arguments match the final event",
          len(done) == 1 and done[0]["arguments"] == "".join(e["delta"] for e in deltas), done)
    check(prefix + ": no empty argument deltas",
          all(e["delta"] for e in deltas), deltas)
    added_pos = next(i for i, e in enumerate(events)
                     if e["type"] == "response.output_item.added")
    check(prefix + ": tool item starts before its argument deltas",
          all(i > added_pos for i, e in enumerate(events)
              if e["type"] == "response.function_call_arguments.delta"))

print()
print("[7.2] streaming: interleaved tools keep their own initial argument segments")
interleaved = [
    chunk({"tool_calls": [
        {"index": 0, "id": "call_a", "function": {"name": "get_weather", "arguments": '{"city":"'}},
        {"index": 1, "id": "call_b", "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}},
    ]}),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'Beijing"}'}}]}),
    chunk({}, "tool_calls"), b"data: [DONE]\n\n",
]
events = [json.loads(line[6:]) for line in b"".join(P.stream_responses_events(
    iter(interleaved), "m", {"usage": None})).decode().splitlines()
    if line.startswith("data: ")]
for index, expected in [(0, '{"city":"Beijing"}'), (1, '{"city":"Paris"}')]:
    actual = "".join(e["delta"] for e in events
                     if e["type"] == "response.function_call_arguments.delta"
                     and e["output_index"] == index)
    check("interleaved tool %d: arguments preserved without duplication" % index,
          actual == expected, actual)

print()
print("[7.3] streaming: custom tools preserve their first argument segment too")
custom_first = [
    chunk({"tool_calls": [{"index": 0, "id": "call_custom_first",
                           "function": {"name": "apply_patch", "arguments": '{"input":"'}}]}),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'pwd"}'}}]}),
    chunk({}, "tool_calls"), b"data: [DONE]\n\n",
]
events = [json.loads(line[6:]) for line in b"".join(P.stream_responses_events(
    iter(custom_first), "m", {"usage": None, "custom_names": {"apply_patch"}})).decode().splitlines()
    if line.startswith("data: ")]
custom_delta = "".join(e["delta"] for e in events
                       if e["type"] == "response.custom_tool_call_input.delta")
check("custom tool: first segment reaches the existing custom delta path",
      custom_delta == '{"input":"pwd"}', custom_delta)
check("custom tool: final input remains unwrapped",
      any(e["type"] == "response.custom_tool_call_input.done" and e["input"] == "pwd"
          for e in events))
check("custom tool: no function argument events emitted",
      not any(e["type"].startswith("response.function_call_arguments.") for e in events))

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




print("[11] additional_tools item in responses input is merged into chat tools")
resp_additional = {
    "model": "gpt-6.1-sol",
    "input": [
        {
            "type": "additional_tools",
            "tools": [{"type": "function", "name": "exec_command", "description": "run command"}]
        },
        {"role": "user", "content": "run ls"}
    ]
}
chat_additional = P.responses_to_chat(resp_additional)
check("additional_tools merged into chat tools",
      any(t.get("name") == "exec_command" for t in chat_additional.get("tools", [])),
      chat_additional.get("tools"))
check("additional_tools not leaking into chat messages",
      len(chat_additional.get("messages", [])) == 1
      and chat_additional["messages"][0]["role"] == "user",
      chat_additional.get("messages"))


print("[12] finish_reason empty string in stream chunk normalized to None")
chunk_with_empty_finish = '{"choices":[{"delta":{"content":"chunk"},"finish_reason":""}]}'
cleaned_chunk = json.loads(P.clean_chunk(chunk_with_empty_finish))
check("finish_reason normalized to None",
      cleaned_chunk["choices"][0]["finish_reason"] is None,
      cleaned_chunk["choices"][0])

print()
print("[13] regression: custom tool nested in a namespace is collected")
CUSTOM_EXEC = {
    "type": "custom", "name": "exec",
    "format": {"type": "grammar", "syntax": "lark", "definition": "start: /.*/s"},
}
FUNCS_NS = {
    "type": "namespace", "name": "functions",
    "tools": [CUSTOM_EXEC,
              {"type": "function", "name": "wait",
               "parameters": {"type": "object", "properties": {}}}],
}
check("[13] nested *custom* tool is collected",
      P.custom_tool_names([FUNCS_NS]) == {"exec"}, P.custom_tool_names([FUNCS_NS]))
check("[13] plain nested function is not",
      "wait" not in P.custom_tool_names([FUNCS_NS]), P.custom_tool_names([FUNCS_NS]))
check("[13] flat custom still collected",
      P.custom_tool_names([CUSTOM_TOOL]) == {"apply_patch"}, P.custom_tool_names([CUSTOM_TOOL]))
# 子工具也常放在 children / functions 栏位；两处都要能递归到。
for _field in ("children", "functions"):
    _ns = {"type": "namespace", "name": "functions", _field: [CUSTOM_EXEC]}
    check("[13] nested under %r also collected" % _field,
          P.custom_tool_names([_ns]) == {"exec"}, P.custom_tool_names([_ns]))

print()
print("[14] regression: namespaced custom tool round-trips as custom_tool_call")
ns_map = P.responses_to_chat({"model": "m", "input": "hi", "tools": [FUNCS_NS]})["_namespace_map"]
chat_exec = {"choices": [{"finish_reason": "tool_calls", "message": {
    "role": "assistant", "content": "",
    "tool_calls": [{"id": "call_e", "type": "function", "function": {
        "name": "exec", "arguments": json.dumps({"input": "pwd"})}}]}}]}
r_exec = P.chat_to_response(chat_exec, "m", P.custom_tool_names([FUNCS_NS]), namespace_map=ns_map)
item_exec = r_exec["output"][0]
check("[14] exec emitted as custom_tool_call",
      item_exec["type"] == "custom_tool_call", item_exec["type"])
check("[14] namespace stamped", item_exec.get("namespace") == "functions", item_exec.get("namespace"))
check("[14] input unwrapped, not JSON", item_exec.get("input") == "pwd", item_exec.get("input"))

# 反面对照：namespace 里的普通 function 仍走 function_call，参数原样透传。
chat_wait = {"choices": [{"finish_reason": "tool_calls", "message": {
    "role": "assistant", "content": "",
    "tool_calls": [{"id": "call_w", "type": "function", "function": {
        "name": "wait", "arguments": '{"ms":100}'}}]}}]}
r_wait = P.chat_to_response(chat_wait, "m", P.custom_tool_names([FUNCS_NS]), namespace_map=ns_map)
item_wait = r_wait["output"][0]
check("[14] plain function stays function_call",
      item_wait["type"] == "function_call", item_wait["type"])
check("[14] its namespace is stamped too",
      item_wait.get("namespace") == "functions", item_wait.get("namespace"))
check("[14] arguments untouched", item_wait.get("arguments") == '{"ms":100}')

print()
print("[15] regression: tools delivered entirely inside additional_tools reach the return path")
resp_ns_additional = {
    "model": "m",
    "input": [
        {"type": "additional_tools", "tools": [FUNCS_NS]},
        {"role": "user", "content": "read a file"},
    ],
}
# 出站：必须合并进 chat tools，且 custom 被降级、namespace 被展开
chat_ns_additional = P.responses_to_chat(resp_ns_additional)
flat = chat_ns_additional.get("tools", [])
check("[15] namespaced custom tool reached chat tools",
      any(t.get("name") == "exec" for t in flat), [t.get("name") for t in flat])
check("[15] and was downgraded to function",
      all(t.get("type") == "function" for t in flat), [t.get("type") for t in flat])
check("[15] namespace map recorded",
      chat_ns_additional.get("_namespace_map", {}).get("exec") == "functions",
      chat_ns_additional.get("_namespace_map"))
# 回程：_handle_responses 用的是同一份合并清单（这里手工复算那个合并口径）
merged = list(resp_ns_additional.get("tools") or []) + P.additional_tools_from_input(resp_ns_additional)
check("[15] merged list yields the custom name",
      P.custom_tool_names(merged) == {"exec"}, P.custom_tool_names(merged))

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
