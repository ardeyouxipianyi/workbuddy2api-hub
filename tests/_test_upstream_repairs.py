"""Deterministic tests for the three upstream-request repairs.

No network and no upstream account needed: the functions under test are pure
body transformations, so each case feeds a synthetic request and asserts the
shape that leaves for the upstream.

  1. prompt_cache_key injection  - reuse the upstream prefix cache, account scoped
  2. deepseek thinking + effort  - thinking.type alone does not enable the trace
  3. tool-call pairing repair    - orphaned calls / split results kill a session
"""
import copy
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


def assistant_with_calls(*ids):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": i, "type": "function",
                            "function": {"name": "f", "arguments": "{}"}} for i in ids]}


def tool_result(tid):
    return {"role": "tool", "tool_call_id": tid, "content": "ok"}


print("[1] prompt_cache_key: account scoped, stable per session (opt-in helper)")

body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
out = P.inject_prompt_cache_key(body, "abcdef1234567890", "conv-1")
key = out.get("prompt_cache_key")
check("key is injected", isinstance(key, str) and key, key)
check("key carries the account prefix", key.startswith("wb2a-abcdef12-"), key)
check("source body is not mutated", "prompt_cache_key" not in body)

same = P.inject_prompt_cache_key(body, "abcdef1234567890", "conv-1")
check("same account + same session is stable",
      same["prompt_cache_key"] == key)

other_conv = P.inject_prompt_cache_key(body, "abcdef1234567890", "conv-2")
check("different session -> different key",
      other_conv["prompt_cache_key"] != key)

other_uid = P.inject_prompt_cache_key(body, "9999999999999999", "conv-1")
check("different account -> different key (no cross-account cache reads)",
      other_uid["prompt_cache_key"] != key)
check("different account -> different prefix",
      other_uid["prompt_cache_key"].startswith("wb2a-99999999-"),
      other_uid["prompt_cache_key"])

client_key = {"model": "m", "prompt_cache_key": "client-chose-this",
              "messages": [{"role": "user", "content": "hi"}]}
kept = P.inject_prompt_cache_key(client_key, "abcdef1234567890", "conv-1")
check("an explicit client key is never overwritten",
      kept["prompt_cache_key"] == "client-chose-this")

blank_key = {"model": "m", "prompt_cache_key": "   ",
             "messages": [{"role": "user", "content": "hi"}]}
filled = P.inject_prompt_cache_key(blank_key, "abcdef1234567890", "conv-1")
check("a blank client key is treated as absent",
      filled["prompt_cache_key"] == key)

by_conv_id = {"model": "m", "conversation_id": "from-body",
              "messages": [{"role": "user", "content": "hi"}]}
via_body = P.inject_prompt_cache_key(by_conv_id, "abcdef1234567890", "conv-1")
via_arg = P.inject_prompt_cache_key(
    {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    "abcdef1234567890", "from-body")
check("body conversation_id outranks the session key argument",
      via_body["prompt_cache_key"] == via_arg["prompt_cache_key"])

camel = P.inject_prompt_cache_key(
    {"model": "m", "conversationId": "from-body",
     "messages": [{"role": "user", "content": "hi"}]},
    "abcdef1234567890", "conv-1")
check("camelCase conversationId is recognised too",
      camel["prompt_cache_key"] == via_arg["prompt_cache_key"])

empty_uid = P.inject_prompt_cache_key(body, "", "")
check("an empty account still yields a key with a placeholder prefix",
      empty_uid["prompt_cache_key"].startswith("wb2a---"), empty_uid["prompt_cache_key"])

print()
print("[2] deepseek thinking: effort is filled in, explicit choices are honoured")


def deepseek_body(model="deepseek-v4.1-flash", **extra):
    payload = {"model": model,
               "messages": [{"role": "user", "content": "hi"}]}
    payload.update(extra)
    return P.build_upstream_body(payload)


bare = deepseek_body()
check("thinking is enabled", (bare.get("thinking") or {}).get("type") == "enabled",
      bare.get("thinking"))
check("an effort level is filled in alongside it",
      isinstance(bare.get("reasoning_effort"), str) and bare["reasoning_effort"],
      bare.get("reasoning_effort"))
check("the filled level matches the catalog default",
      bare.get("reasoning_effort") == (P.model_default_effort("deepseek-v4.1-flash") or "high"),
      bare.get("reasoning_effort"))

explicit = deepseek_body(reasoning_effort="low")
check("an explicit effort is left alone", explicit["reasoning_effort"] == "low")

camel_explicit = deepseek_body(reasoningEffort="max")
check("a camelCase explicit effort is left alone",
      camel_explicit.get("reasoningEffort") == "max"
      and "reasoning_effort" not in camel_explicit, camel_explicit.get("reasoning_effort"))

disabled = deepseek_body(thinking={"type": "disabled"})
check("thinking disabled is not re-enabled",
      disabled["thinking"] == {"type": "disabled"}, disabled.get("thinking"))
check("thinking disabled does not gain an effort",
      not disabled.get("reasoning_effort"), disabled.get("reasoning_effort"))

none_effort = deepseek_body(reasoning_effort="none")
check("effort 'none' is not overridden",
      none_effort["reasoning_effort"] == "none", none_effort.get("reasoning_effort"))

enabled_no_effort = deepseek_body(thinking={"type": "enabled"})
check("an enabled-but-effortless request gains the effort",
      enabled_no_effort.get("reasoning_effort"), enabled_no_effort)

non_deepseek = deepseek_body(model="glm-5.3")
check("non-deepseek models are untouched",
      "thinking" not in non_deepseek and "reasoning_effort" not in non_deepseek,
      (non_deepseek.get("thinking"), non_deepseek.get("reasoning_effort")))

print()
print("[3] tool pairing: repack results, drop orphans")

split = [{"role": "user", "content": "go"},
         assistant_with_calls("c0", "c1"),
         tool_result("c0"),
         {"role": "developer", "content": "<image_resize_notice>"},
         tool_result("c1")]
fixed, changed = P.repack_tool_result_blocks(copy.deepcopy(split))
check("an interleaved message is reported as a change", changed)
check("results are now adjacent",
      [m.get("role") for m in fixed[1:5]] == ["assistant", "tool", "tool", "developer"],
      [m.get("role") for m in fixed[1:5]])
check("result order is preserved",
      [m.get("tool_call_id") for m in fixed if m.get("role") == "tool"] == ["c0", "c1"])
check("the intruder is kept, only moved",
      any(m.get("content") == "<image_resize_notice>" for m in fixed))

adjacent = [{"role": "user", "content": "go"},
            assistant_with_calls("c0"),
            tool_result("c0")]
unchanged, changed2 = P.repack_tool_result_blocks(copy.deepcopy(adjacent))
check("an already-correct batch is left alone", not changed2)
check("and returned as the same object", unchanged is not None)

two_batches = [assistant_with_calls("a0"),
               tool_result("a0"),
               assistant_with_calls("b0"),
               tool_result("b0")]
kept_batches, changed3 = P.repack_tool_result_blocks(copy.deepcopy(two_batches))
check("a second batch is not swallowed as an intruder", not changed3)
check("both batches survive",
      len([m for m in kept_batches if m.get("role") == "assistant"]) == 2)

orphan_call = [{"role": "user", "content": "go"},
               assistant_with_calls("c0", "c1"),
               tool_result("c0")]
cleaned, changed4 = P.cleanup_orphan_tool_calls(copy.deepcopy(orphan_call))
check("an unanswered call is reported as a change", changed4)
check("the unanswered call is dropped",
      [tc["id"] for tc in cleaned[1]["tool_calls"]] == ["c0"],
      cleaned[1].get("tool_calls"))
check("the answered call survives", cleaned[2]["tool_call_id"] == "c0")

orphan_result = [{"role": "user", "content": "go"},
                 {"role": "assistant", "content": "done"},
                 tool_result("ghost")]
cleaned2, changed5 = P.cleanup_orphan_tool_calls(copy.deepcopy(orphan_result))
check("a result with no call is reported as a change", changed5)
check("the orphan result is removed",
      [m.get("role") for m in cleaned2] == ["user", "assistant"],
      [m.get("role") for m in cleaned2])

all_orphan = [{"role": "user", "content": "go"},
              assistant_with_calls("c0"),
              {"role": "user", "content": "next"}]
cleaned3, changed6 = P.cleanup_orphan_tool_calls(copy.deepcopy(all_orphan))
check("a fully unanswered batch is reported as a change", changed6)
check("the empty tool_calls key is removed entirely",
      "tool_calls" not in cleaned3[1], cleaned3[1])
check("the rest of the conversation is intact", len(cleaned3) == 3)

no_tools = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"}]
cleaned4, changed7 = P.cleanup_orphan_tool_calls(copy.deepcopy(no_tools))
check("a tool-free conversation is untouched", not changed7)
check("and keeps its length", len(cleaned4) == 2)

print()
print("[3b] split parallel batches merge; truncated arguments are dropped")

split_batch = [{"role": "user", "content": "go"},
               assistant_with_calls("c0"),
               assistant_with_calls("c1"),
               tool_result("c0"),
               tool_result("c1")]
merged, changed8 = P.merge_adjacent_tool_calls(copy.deepcopy(split_batch))
check("back-to-back tool_calls batches are reported as a change", changed8)
check("both calls now ride on one assistant",
      [tc["id"] for tc in merged[1].get("tool_calls", [])] == ["c0", "c1"],
      merged[1].get("tool_calls"))
check("the duplicate assistant is gone",
      len([m for m in merged if m.get("role") == "assistant"]) == 1)

empty_array = [assistant_with_calls("c0"),
               {"role": "assistant", "content": [],
                "tool_calls": assistant_with_calls("c1")["tool_calls"]},
               tool_result("c0"),
               tool_result("c1")]
merged_arr, changed9 = P.merge_adjacent_tool_calls(copy.deepcopy(empty_array))
check("content: [] counts as empty and merges", changed9)
check("empty-array batch keeps both calls",
      [tc["id"] for tc in merged_arr[0].get("tool_calls", [])] == ["c0", "c1"])

reasoned = [{"role": "assistant", "content": None, "reasoning_content": "first",
             "tool_calls": assistant_with_calls("c0")["tool_calls"]},
            {"role": "assistant", "content": "",
             "reasoning_content": "second",
             "tool_calls": assistant_with_calls("c1")["tool_calls"]}]
merged_r, changed10 = P.merge_adjacent_tool_calls(copy.deepcopy(reasoned))
check("reasoning traces are preserved on merge",
      merged_r[0].get("reasoning_content") == "first\nsecond",
      merged_r[0].get("reasoning_content"))

text_then_calls = [{"role": "assistant", "content": "",
                    "tool_calls": assistant_with_calls("c0")["tool_calls"]},
                   {"role": "assistant", "content": "done"}]
merged_t, changed11 = P.merge_adjacent_tool_calls(copy.deepcopy(text_then_calls))
check("a trailing plain assistant folds into the content-less call message",
      changed11 and merged_t[0].get("content") == "done" and len(merged_t) == 1)

not_empty = [assistant_with_calls("c0"),
             {"role": "assistant", "content": "kept",
              "tool_calls": assistant_with_calls("c1")["tool_calls"]}]
_, changed12 = P.merge_adjacent_tool_calls(copy.deepcopy(not_empty))
check("a trailing batch with real content is not merged", not changed12)

separated = [assistant_with_calls("c0"),
             {"role": "user", "content": "hi"},
             assistant_with_calls("c1")]
_, changed13 = P.merge_adjacent_tool_calls(copy.deepcopy(separated))
check("non-adjacent batches are never merged", not changed13)

check("an empty string is a legal no-argument call",
      not P.is_truncated_arguments(""))
check("whitespace is a legal no-argument call",
      not P.is_truncated_arguments("   "))
check("valid JSON object is complete", not P.is_truncated_arguments('{"a":1}'))
check("valid JSON scalar is complete", not P.is_truncated_arguments("null"))
check("half-written JSON is truncated", P.is_truncated_arguments('{"a":'))
check("plain text is not silently accepted", P.is_truncated_arguments("not json"))

mixed_calls = [{"id": "ok", "function": {"name": "f", "arguments": '{"a":1}'}},
               {"id": "cut", "function": {"name": "f", "arguments": '{"a":'}},
               {"id": "noargs", "function": {"name": "f", "arguments": ""}}]
kept_calls = P.drop_truncated_tool_calls(mixed_calls)
check("only the truncated call is dropped",
      [c["id"] for c in kept_calls] == ["ok", "noargs"])

def sse_line(obj):
    return ("data: " + json.dumps(obj) + "\n\n").encode("utf-8")


truncated_stream = [
    sse_line({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "call_t", "function": {"name": "f",
                                                  "arguments": '{"a":'}}]},
        "finish_reason": "length"}]}),
]
agg = P.aggregate_stream(iter(truncated_stream), "m", None)
check("aggregate_stream drops a truncated call at max_tokens",
      "tool_calls" not in agg["choices"][0]["message"])
check("aggregate_stream keeps the length finish reason",
      agg["choices"][0]["finish_reason"] == "length")

complete_stream = [
    sse_line({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "call_c", "function": {"name": "f",
                                                  "arguments": '{"a":1}'}}]},
        "finish_reason": "length"}]}),
]
agg2 = P.aggregate_stream(iter(complete_stream), "m", None)
check("aggregate_stream keeps a complete call even at max_tokens",
      [tc["id"] for tc in agg2["choices"][0]["message"].get("tool_calls", [])] == ["call_c"])

eof_stream = [
    sse_line({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "call_e", "function": {"name": "f",
                                                  "arguments": '{"a":'}}]},
        "finish_reason": "tool_calls"}]}),
]
agg3 = P.aggregate_stream(iter(eof_stream), "m", None)
check("aggregate_stream drops a truncated call on EOF without [DONE]",
      "tool_calls" not in agg3["choices"][0]["message"])

resp_stream = [
    sse_line({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "call_r", "function": {"name": "f",
                                                  "arguments": '{"a":'}}]},
        "finish_reason": "tool_calls"}]}),
]
resp_raw = b"".join(P.stream_responses_events(
    iter(resp_stream), "m", {"usage": None})).decode("utf-8")
check("stream_responses_events drops the truncated done event",
      "function_call_arguments.done" not in resp_raw, resp_raw[-400:])
check("stream_responses_events marks the response incomplete",
      '"status": "incomplete"' in resp_raw)

print()
print("[4] integration: build_upstream_body applies the repairs")

broken = {"model": "deepseek-v4.1-flash",
          "messages": [{"role": "user", "content": "go"},
                       assistant_with_calls("c0", "c1"),
                       tool_result("c0")]}
built = P.build_upstream_body(broken)
check("the orphaned call is gone from the outbound body",
      [tc["id"] for tc in built["messages"][2]["tool_calls"]] == ["c0"],
      built["messages"][2].get("tool_calls"))
check("the deepseek effort rides along", built.get("reasoning_effort"),
      built.get("reasoning_effort"))
check("stream is still forced", built.get("stream") is True)
check("usage reporting is still requested",
      (built.get("stream_options") or {}).get("include_usage") is True)
check("no cache key yet: that is account scoped and added per candidate",
      "prompt_cache_key" not in built)
check("the caller's payload is not mutated",
      len(broken["messages"][1]["tool_calls"]) == 2)

split_body = {"model": "deepseek-v4.1-flash",
              "messages": [{"role": "user", "content": "go"},
                           assistant_with_calls("c0"),
                           assistant_with_calls("c1"),
                           tool_result("c0"),
                           tool_result("c1")]}
built_split = P.build_upstream_body(split_body)
split_assistants = [m for m in built_split["messages"] if m.get("role") == "assistant"]
check("a split parallel batch leaves as one assistant",
      len(split_assistants) == 1, len(split_assistants))
check("both calls survive the merge",
      [tc["id"] for tc in split_assistants[0].get("tool_calls", [])] == ["c0", "c1"],
      split_assistants[0].get("tool_calls"))

print()
print("[5] prompt_cache_key is off by default")

check("the injection is opt-in, not always-on",
      P.prompt_cache_key_enabled() is False,
      P.prompt_cache_key_enabled())

import os as _os
_os.environ["WB_PROMPT_CACHE_KEY"] = "1"
check("it can be switched on with WB_PROMPT_CACHE_KEY=1",
      P.prompt_cache_key_enabled() is True)
_os.environ["WB_PROMPT_CACHE_KEY"] = "0"
check("and switched back off", P.prompt_cache_key_enabled() is False)
del _os.environ["WB_PROMPT_CACHE_KEY"]

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
