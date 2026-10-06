"""Anthropic Messages API translation and streaming regression tests."""
import json
import os
import sys
import tempfile

_startup_dir = tempfile.TemporaryDirectory(prefix="anthropic-messages-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy as P

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (name, extra))


def parse_events(frames):
    out = []
    for frame in frames:
        text = frame.decode("utf-8")
        event = ""
        data = ""
        for line in text.splitlines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data += line[6:]
        if event:
            out.append((event, json.loads(data or "{}")))
    return out


print("[1] Messages request maps to chat completions")
request = {
    "model": "deepseek-v4.1-flash",
    "max_tokens": 128,
    "stream": True,
    "system": [{"type": "text", "text": "sys-a"},
               {"type": "text", "text": "sys-b"}],
    "messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aaa"}},
        ]},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "secret", "signature": "sig"},
            {"type": "text", "text": "ok"},
            {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"cmd": "ls"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "a.txt"},
            {"type": "text", "text": "next"},
        ]},
    ],
    "tools": [
        {"name": "Bash", "description": "run", "input_schema": {"type": "object"}},
        {"type": "web_search_20250305", "name": "web_search"},
    ],
    "tool_choice": {"type": "tool", "name": "Bash", "disable_parallel_tool_use": True},
    "stop_sequences": ["END"],
    "metadata": {"user_id": "u"},
    "thinking": {"type": "adaptive", "display": "summarized"},
    "output_config": {"effort": "high", "format": {"type": "json_schema"}},
    "context_management": {"edits": []},
    "top_k": 5,
}
chat = P.messages_to_chat(request)
check("model is preserved", chat.get("model") == "deepseek-v4.1-flash", chat.get("model"))
check("stream is preserved", chat.get("stream") is True, chat.get("stream"))
check("max_tokens is preserved", chat.get("max_tokens") == 128, chat.get("max_tokens"))
_sys = chat["messages"][0]["content"]
check("system blocks are joined and note appended",
      _sys.startswith("sys-a\nsys-b") and "web_search" in _sys, chat["messages"][0])
check("stop_sequences map to stop", chat.get("stop") == ["END"], chat.get("stop"))
check("metadata.user_id maps to user", chat.get("user") == "u", chat.get("user"))
check("parallel tool calls are disabled", chat.get("parallel_tool_calls") is False, chat.get("parallel_tool_calls"))
check("effort maps from output_config", chat.get("reasoning_effort") == "high", chat.get("reasoning_effort"))
check("top_k is dropped", "top_k" not in chat, chat)
check("thinking/output_config are not forwarded",
      "thinking" not in chat and "output_config" not in chat and "context_management" not in chat, chat)
check("only client tool with input_schema is forwarded",
      len(chat.get("tools") or []) == 1 and chat["tools"][0]["function"]["name"] == "Bash",
      chat.get("tools"))
user = chat["messages"][1]
img = user["content"][1]["image_url"]["url"]
check("base64 image becomes a data URL", img == "data:image/png;base64,aaa", img)
asst = chat["messages"][2]
check("thinking is not forwarded", "secret" not in json.dumps(asst, ensure_ascii=False), asst)
call = (asst.get("tool_calls") or [{}])[0]
check("tool_use maps to an OpenAI tool call",
      call.get("id") == "toolu_1" and call["function"]["name"] == "Bash"
      and call["function"]["arguments"] == '{"cmd":"ls"}',
      asst)
tool_msg = chat["messages"][3]
check("tool_result maps to a tool message",
      tool_msg.get("role") == "tool" and tool_msg.get("tool_call_id") == "toolu_1"
      and tool_msg.get("content") == "a.txt", tool_msg)

print()
print("[2] budget thinking maps to effort")
check("disabled -> none", P.anthropic_effort_from_messages({"thinking": {"type": "disabled"}}) == "none")
check("enabled low", P.anthropic_effort_from_messages({"thinking": {"type": "enabled", "budget_tokens": 1000}}) == "low")
check("enabled medium", P.anthropic_effort_from_messages({"thinking": {"type": "enabled", "budget_tokens": 9000}}) == "medium")
check("enabled high", P.anthropic_effort_from_messages({"thinking": {"type": "enabled", "budget_tokens": 17000}}) == "high")
check("enabled xhigh", P.anthropic_effort_from_messages({"thinking": {"type": "enabled", "budget_tokens": 40000}}) == "xhigh")

print()
print("[3] chat response maps to an Anthropic message")
chat_obj = {
    "id": "chatcmpl_1",
    "model": "deepseek-v4.1-flash",
    "choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "hello",
        "reasoning_content": "secret",
        "tool_calls": [{"id": "call_1", "type": "function", "function": {
            "name": "Bash", "arguments": '{"cmd":"ls"}'}}],
    }}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
              "prompt_cache_hit_tokens": 4, "prompt_cache_write_tokens": 2},
}
msg = P.chat_to_messages(chat_obj)
check("id is a message id", msg["id"].startswith("msg_"), msg["id"])
check("type/role are native", msg["type"] == "message" and msg["role"] == "assistant", msg)
check("text block is present", msg["content"][0] == {"type": "text", "text": "hello"}, msg["content"])
check("tool_use block is present",
      msg["content"][1]["type"] == "tool_use" and msg["content"][1]["input"] == {"cmd": "ls"}, msg["content"])
check("stop_reason maps tool_calls -> tool_use", msg["stop_reason"] == "tool_use", msg["stop_reason"])
check("usage maps cache fields",
      msg["usage"] == {"input_tokens": 10, "output_tokens": 5,
                       "cache_read_input_tokens": 4, "cache_creation_input_tokens": 2}, msg["usage"])
check("no unsigned thinking is replayed", "secret" not in json.dumps(msg, ensure_ascii=False), msg)

print()
print("[4] streaming text event order")
chunks = [
    b'data: {"id":"chatcmpl_2","model":"m","choices":[{"delta":{"content":"hel"}}]}\n\n',
    b'data: {"id":"chatcmpl_2","model":"m","choices":[{"delta":{"content":"lo"}}]}\n\n',
    b'data: {"id":"chatcmpl_2","model":"m","choices":[{"finish_reason":"stop","delta":{}}],"usage":{"prompt_tokens":2,"completion_tokens":2,"total_tokens":4}}\n\n',
    b'data: [DONE]\n\n',
]
events = parse_events(list(P.stream_messages_events(iter(chunks), "m")))
types = [e[0] for e in events]
check("starts with message_start", types and types[0] == "message_start", types)
check("ends with message_stop", types and types[-1] == "message_stop", types)
check("text block lifecycle is present",
      "content_block_start" in types and "content_block_delta" in types and "content_block_stop" in types, types)
text = "".join(e[1]["delta"]["text"] for e in events if e[0] == "content_block_delta")
check("text deltas combine", text == "hello", text)
final = [e[1] for e in events if e[0] == "message_delta"][0]
check("final stop reason is end_turn", final["delta"]["stop_reason"] == "end_turn", final)
check("final usage is mapped", final["usage"]["input_tokens"] == 2 and final["usage"]["output_tokens"] == 2, final)

print()
print("[5] streaming tool event order")
tool_chunks = [
    b'data: {"id":"chatcmpl_3","model":"m","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"Bash","arguments":"{\\"cmd\\":"}}]}}]}\n\n',
    b'data: {"id":"chatcmpl_3","model":"m","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"ls\\"}"}}]},"finish_reason":"tool_calls"}]}\n\n',
    b'data: [DONE]\n\n',
]
tool_events = parse_events(list(P.stream_messages_events(iter(tool_chunks), "m")))
tool_types = [e[0] for e in tool_events]
check("tool stream emits tool_use start", "content_block_start" in tool_types, tool_types)
check("tool stream emits input_json_delta", "content_block_delta" in tool_types, tool_types)
block = [e[1]["content_block"] for e in tool_events if e[0] == "content_block_start"][0]
check("tool block is native tool_use",
      block["type"] == "tool_use" and block["id"] == "call_1" and block["name"] == "Bash", block)
partial = "".join(e[1]["delta"]["partial_json"] for e in tool_events
                  if e[0] == "content_block_delta" and e[1]["delta"]["type"] == "input_json_delta")
check("tool JSON deltas combine", partial == '{"cmd":"ls"}', partial)
tool_final = [e[1] for e in tool_events if e[0] == "message_delta"][0]
check("tool stop reason is tool_use", tool_final["delta"]["stop_reason"] == "tool_use", tool_final)

print()
print("[6] streaming error event")
err_events = parse_events(list(P.stream_messages_events(iter([
    b'data: {"error":{"message":"boom"}}\n\n']), "m")))
check("error event is emitted", err_events and err_events[-1][0] == "error", err_events)
check("error payload has a native type",
      err_events[-1][1]["error"]["type"] == "api_error" if err_events else False, err_events)

print()
print("[7] count_tokens uses the native response shape")
count = P._anthropic_estimate_chat_tokens(P.messages_to_chat({"model": "m", "messages": [
    {"role": "user", "content": "你好 hello"}]}))
check("count is positive", count > 0, count)

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
