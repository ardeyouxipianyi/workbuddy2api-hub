"""Regression tests for the tool_choice="none" do-it-forever loop.

Reported symptom: with deepseek-v4.1-flash driving an agent client, the model
kept replying "I'll do it" without ever emitting a tool call, appending two
messages per turn until the context reached ~297k tokens and the user had to
abort the request by hand (usage.jsonl recorded outcome=client_aborted,
gen_ms=128262, usage_missing=true).

Root cause: normalize_tool_choice() deleted the whole `tools` declaration when
the client sent tool_choice="none". Without the function schema the model could
not use the structured tool channel, so it downgraded the call into DSML /
pseudo-JSON text inside `content` (tool_calls empty, finish_reason=stop). The
client parsed no call, asked again, and the model repeated itself forever.

These tests pin the outbound shape so the deletion cannot come back.
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

TOOLS = [{
    "type": "function",
    "function": {
        "name": "run_shell",
        "description": "Run a shell command",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string"}},
                       "required": ["command"]},
    },
}]

def norm(tool_choice):
    obj = {"model": "deepseek-v4.1-flash", "tools": json.loads(json.dumps(TOOLS))}
    if tool_choice is not None:
        obj["tool_choice"] = tool_choice
    P.normalize_tool_choice(obj)
    return obj

print("[1] tool_choice='none' must NOT delete the tools declaration")
o = norm("none")
check("tools survive", "tools" in o and len(o["tools"]) == 1, o.get("tools"))
check("function name intact", o["tools"][0]["function"]["name"] == "run_shell")
check("tool_choice kept as the string upstream accepts",
      o.get("tool_choice") == "none", o.get("tool_choice"))

print()
print("[2] upstream declares tool_choice as a string; an object form 11101s")
o = norm({"type": "none"})
check("object form downgraded to string", o.get("tool_choice") == "none", o.get("tool_choice"))
check("tools still present", "tools" in o)
check("tool_choice is never a dict",
      not isinstance(o.get("tool_choice"), dict), o.get("tool_choice"))

print()
print("[3] the other spellings keep working")
check("'auto' passes through", norm("auto").get("tool_choice") == "auto")
check("{type:auto} normalises to 'auto'",
      norm({"type": "auto"}).get("tool_choice") == "auto")
check("'required' passes through", norm("required").get("tool_choice") == "required")
named = norm({"type": "function", "function": {"name": "run_shell"}})
check("named function choice becomes the bare name",
      named.get("tool_choice") == "run_shell", named.get("tool_choice"))
check("named choice keeps tools", "tools" in named)

print()
print("[4] a body with no tool_choice is left alone")
o = {"model": "m", "tools": json.loads(json.dumps(TOOLS))}
P.normalize_tool_choice(o)
check("no tool_choice invented", "tool_choice" not in o, o.get("tool_choice"))
check("tools untouched", len(o["tools"]) == 1)

print()
print("[5] build_upstream_body keeps tools end to end under tool_choice=none")
body = P.build_upstream_body({
    "model": "deepseek-v4.1-flash",
    "messages": [{"role": "user", "content": "do the thing"}],
    "tools": json.loads(json.dumps(TOOLS)),
    "tool_choice": "none",
})
check("full pipeline retains tools", bool(body.get("tools")), body.get("tools"))
check("full pipeline keeps a string tool_choice",
      body.get("tool_choice") == "none", body.get("tool_choice"))

print()
print("[6] DSML text fallback still parses upstream's real full-width syntax")
FS = "\uff5c\uff5c"
block = (
    "<" + FS + "DSML" + FS + " calls>" + chr(10) +
    "<" + FS + "DSML" + FS + ' invoke name="run_shell">' + chr(10) +
    "<" + FS + "DSML" + FS + ' parameter name="command" string="true">ls -l' +
    "</" + FS + "DSML" + FS + " parameter>" + chr(10) +
    "</" + FS + "DSML" + FS + " invoke>" + chr(10) +
    "</" + FS + "DSML" + FS + " calls>"
)
calls, clean = P.parse_dsml_tool_calls(block)
check("DSML call recovered from text", bool(calls), calls)
if calls:
    check("recovered the function name", calls[0]["name"] == "run_shell", calls[0])
    check("recovered the arguments",
          json.loads(calls[0]["arguments"]).get("command") == "ls -l", calls[0])
check("DSML tags stripped from visible text", "DSML" not in clean, repr(clean[:80]))

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
