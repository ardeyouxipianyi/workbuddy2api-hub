#!/usr/bin/env python3
"""Chat Completions must not lose a tool call the model printed as text.

Upstream sometimes answers a tools request with the call spelled out inside
`content` -- a full-width DSML block, or a hybrid of plain XML open tags and
DSML close tags -- instead of structured `tool_calls`. /v1/chat/completions
used to forward that text verbatim, so the client saw prose, found no call, and
ended the turn with nothing to run: reported as "it keeps stopping by itself".
The Responses path already parsed the DSML spelling; these cases pin the same
recovery on the Chat Completions path, streaming and not.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-dsml-chat-")

import wb_proxy as P  # noqa: E402
import wb_transforms as T  # noqa: E402

PASS = 0
FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   %s" % label)
    else:
        FAIL += 1
        print("  FAIL %s%s" % (label, (" | " + str(extra)) if extra else ""))


BAR = "\uff5c\uff5c"


def dsml_block(tool="run_shell", param="command", value="ls -l"):
    """The spelling the suite already pins for the Responses path."""
    return (
        "<%sDSML%s calls>"
        "<%sDSML%s invoke name=\"%s\">"
        "<%sDSML%s parameter name=\"%s\" string=\"true\">%s</%sDSML%s parameter>"
        "</%sDSML%s invoke></%sDSML%s calls>"
    ) % (BAR, BAR, BAR, BAR, tool, BAR, BAR, param, value,
         BAR, BAR, BAR, BAR, BAR, BAR)


# The shape observed in the wild: the opening tags are plain XML and the closing
# tags are DSML, and the first parameter's close tag is swallowed whole by the
# closing tags that follow it.
HYBRID = (
    "<run_code>"
    "<parameter name=\"code\">print(1)</parameter>\n"
    "<parameter name=\"description\">say hi</%sDSML%s parameter>\n"
    "</run_code></invoke></%sDSML%s parameter></%sDSML%s invoke></%sDSML%s calls>"
) % (BAR, BAR, BAR, BAR, BAR, BAR, BAR, BAR)

print("[1] parse_leaked_tool_calls recovers both spellings")
calls, clean = T.parse_leaked_tool_calls(HYBRID)
check("hybrid yields one call", calls is not None and len(calls) == 1, calls)
check("hybrid call name", bool(calls) and calls[0]["name"] == "run_code", calls)
args = json.loads(calls[0]["arguments"]) if calls else {}
check("hybrid code argument", args.get("code") == "print(1)", args)
check("hybrid description argument", args.get("description") == "say hi", args)
check("hybrid markup stripped",
      "<parameter" not in clean and "DSML" not in clean, clean)

calls2, clean2 = T.parse_leaked_tool_calls(dsml_block())
check("dsml yields one call", calls2 is not None and len(calls2) == 1, calls2)
check("dsml call name", bool(calls2) and calls2[0]["name"] == "run_shell", calls2)
check("dsml command argument",
      bool(calls2) and json.loads(calls2[0]["arguments"]).get("command") == "ls -l",
      calls2)
check("dsml markup stripped", "<" not in clean2, clean2)

print("[2] prose is left alone")
calls3, clean3 = T.parse_leaked_tool_calls("just a normal answer")
check("prose yields no call", calls3 is None, calls3)
check("prose unchanged", clean3 == "just a normal answer", clean3)
calls4, _ = T.parse_leaked_tool_calls("use a <parameter>less approach")
check("a bare word does not fabricate a call", calls4 is None, calls4)

print("[3] repair_leaked_tool_calls moves the call onto the message")
msg = {"role": "assistant", "content": "Here you go:\n" + HYBRID}
check("repair reports a change", P.repair_leaked_tool_calls(msg) is True)
check("content cleaned", "run_code>" not in msg["content"], msg["content"])
check("tool_calls written", len(msg.get("tool_calls") or []) == 1, msg)
check("tool_calls shape",
      bool(msg.get("tool_calls"))
      and msg["tool_calls"][0]["type"] == "function"
      and msg["tool_calls"][0]["function"]["name"] == "run_code",
      msg.get("tool_calls"))

msg2 = {"role": "assistant", "content": HYBRID,
        "tool_calls": [{"id": "call_x", "type": "function",
                        "function": {"name": "run_code", "arguments": "{}"}}]}
check("a real call wins", P.repair_leaked_tool_calls(msg2) is False, msg2)
check("a real call is untouched", msg2["tool_calls"][0]["id"] == "call_x", msg2)

msg3 = {"role": "assistant", "content": "plain prose"}
check("plain prose is untouched", P.repair_leaked_tool_calls(msg3) is False, msg3)

print("[4] the stream filter holds a printed call back")
f1 = P.LeakedToolCallStreamFilter()
out1 = f1.feed("Sure, running it now.")
check("prose passes straight through",
      out1 == [("text", "Sure, running it now.")], out1)

f2 = P.LeakedToolCallStreamFilter()
out2 = f2.feed("<run_code><parameter name=\"code\">print(1)</parameter>")
check("an opening call tag is held", out2 == [], out2)
out2b = f2.flush()
check("flush converts it", len(out2b) == 1 and out2b[0][0] == "calls", out2b)
check("flush keeps the call",
      bool(out2b) and out2b[0][1][0]["name"] == "run_code", out2b)

f3 = P.LeakedToolCallStreamFilter()
out3 = f3.feed("hello <di")
check("a partial unknown tag is not held", out3 == [("text", "hello <di")], out3)

f4 = P.LeakedToolCallStreamFilter()
out4a = f4.feed("text before ")
check("leading prose is released on its own", out4a == [("text", "text before ")], out4a)
out4 = f4.feed("<run_code><parameter name=\"code\">x</parameter>")
check("a following call is held", out4 == [], out4)
out4b = f4.flush()
check("the held call still converts", bool(out4b) and out4b[0][0] == "calls", out4b)

f5 = P.LeakedToolCallStreamFilter()
out5 = f5.feed("text before <run_code><parameter name=\"code\">x</parameter>")
check("prose ahead of a call in one delta is released",
      out5 == [("text", "text before ")], out5)
out5b = f5.flush()
check("that call converts too", bool(out5b) and out5b[0][0] == "calls", out5b)

print("[4b] a complete call is released without waiting for EOF (#549)")
f6 = P.LeakedToolCallStreamFilter()
out6 = f6.feed(HYBRID)
check("a complete call converts on arrival",
      len(out6) == 1 and out6[0][0] == "calls", out6)
check("nothing is left buffered", f6.buf == "", f6.buf)

f7 = P.LeakedToolCallStreamFilter()
out7 = f7.feed("look: " + HYBRID + " done")
check("prose before and after a complete call is kept",
      [a[0] for a in out7] == ["text", "calls", "text"], out7)
check("the trailing prose is intact",
      out7 and out7[-1] == ("text", " done"), out7)


def sse(payload):
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


def stream_of(text, finish="length", done=True):
    lines = [
        sse({"id": "cmpl-1", "model": "m",
             "choices": [{"index": 0, "delta": {"content": text},
                          "finish_reason": None}]}),
        sse({"id": "cmpl-1", "model": "m",
             "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}),
    ]
    if done:
        lines.append(b"data: [DONE]\n\n")
    return lines


print("[5] aggregate_stream recovers the call and fixes the terminator")
obj = P.aggregate_stream(iter(stream_of(HYBRID)), "m", "cmpl-1")
choice = (obj.get("choices") or [{}])[0]
message = choice.get("message") or {}
check("finish_reason is tool_calls",
      choice.get("finish_reason") == "tool_calls", choice.get("finish_reason"))
check("message carries the call",
      len(message.get("tool_calls") or []) == 1, message)
check("arguments survived",
      bool(message.get("tool_calls"))
      and json.loads(message["tool_calls"][0]["function"]["arguments"]).get("code")
      == "print(1)",
      message.get("tool_calls"))
check("content is no longer markup",
      "run_code>" not in (message.get("content") or ""), message.get("content"))

print("[6] an unusable or truncated block is not invented")
obj2 = P.aggregate_stream(
    iter(stream_of("<run_code><parameter name=\"code\">print(1)")), "m", "cmpl-2")
choice2 = (obj2.get("choices") or [{}])[0]
check("no call is fabricated",
      not ((choice2.get("message") or {}).get("tool_calls")),
      choice2.get("message"))
check("finish_reason stays length",
      choice2.get("finish_reason") == "length", choice2.get("finish_reason"))

print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
