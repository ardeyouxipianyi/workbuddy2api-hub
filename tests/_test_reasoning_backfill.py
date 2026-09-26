"""Deterministic tests for DeepSeek reasoning_content backfill.

Root cause of upstream 11155 ("the reasoning content from the previous turn
must be passed back in thinking mode"): the gateway only backfilled when the
history already contained a reasoning trace (hasTrace half). When a third-party
client drops reasoning entirely, every assistant message was forwarded without
`reasoning_content` and upstream rejected the request.

Upstream also validates len(reasoning) > 0, so a bare empty string is not
enough - the field must be mirrored onto `reasoning` with a non-empty value.

No network: pure payload transformation tests.
"""

import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault(
    "ACCOUNTS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "_acc_rc")
)
os.environ.setdefault(
    "USAGE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "_use_rc")
)

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


def asst(out):
    return [m for m in out if isinstance(m, dict) and m.get("role") == "assistant"]


print("[1] thinking enabled + no reasoning trace: every assistant gets the field")
msgs = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "q1"},
    {"role": "assistant", "content": "a1"},
    {"role": "user", "content": "q2"},
    {"role": "assistant", "content": "a2"},
    {"role": "user", "content": "q3"},
]
out = P.backfill_reasoning_content(
    copy.deepcopy(msgs), "deepseek-v4.1-flash", thinking_enabled=True
)
a = asst(out)
check(
    "all assistant messages carry reasoning_content",
    all("reasoning_content" in m for m in a),
    [sorted(m) for m in a],
)
check(
    "reasoning_content is a string",
    all(isinstance(m.get("reasoning_content"), str) for m in a),
)
check(
    "reasoning mirrored and non-empty",
    all(isinstance(m.get("reasoning"), str) and m.get("reasoning") for m in a),
    [m.get("reasoning") for m in a],
)
check(
    "no reasoning trace -> placeholder, not content invention",
    all(m["reasoning_content"] == "" for m in a),
    [m["reasoning_content"] for m in a],
)
check(
    "reasoning placeholder is a single space",
    all(m["reasoning"] == " " for m in a),
    [repr(m["reasoning"]) for m in a],
)

print()
print("[2] real reasoning_content is preserved, never overwritten")
msgs = [
    {"role": "user", "content": "q1"},
    {"role": "assistant", "content": "a1", "reasoning_content": "real trace"},
    {"role": "user", "content": "q2"},
]
out = P.backfill_reasoning_content(
    copy.deepcopy(msgs), "deepseek-v4.1-flash", thinking_enabled=True
)
a = asst(out)[0]
check(
    "existing reasoning_content kept",
    a["reasoning_content"] == "real trace",
    a["reasoning_content"],
)
check(
    "reasoning mirrored from reasoning_content",
    a["reasoning"] == "real trace",
    a.get("reasoning"),
)

print()
print("[3] legacy `reasoning` field is copied into reasoning_content")
msgs = [
    {"role": "user", "content": "q1"},
    {"role": "assistant", "content": "a1", "reasoning": "legacy trace"},
    {"role": "user", "content": "q2"},
]
out = P.backfill_reasoning_content(
    copy.deepcopy(msgs), "deepseek-v4.1-flash", thinking_enabled=True
)
a = asst(out)[0]
check(
    "reasoning copied to reasoning_content",
    a["reasoning_content"] == "legacy trace",
    a.get("reasoning_content"),
)
check(
    "legacy reasoning untouched", a["reasoning"] == "legacy trace", a.get("reasoning")
)

print()
print("[4] non-string reasoning_content (null / number) counts as missing")
for bad in (None, 123, {"x": 1}, ["y"]):
    msgs = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1", "reasoning_content": bad},
        {"role": "user", "content": "q2"},
    ]
    out = P.backfill_reasoning_content(
        copy.deepcopy(msgs), "deepseek-v4.1-flash", thinking_enabled=True
    )
    a = asst(out)[0]
    check(
        f"non-string {type(bad).__name__} replaced with string",
        isinstance(a.get("reasoning_content"), str),
        a.get("reasoning_content"),
    )

print()
print("[5] empty-string reasoning_content is normalized (mirror reasoning non-empty)")
msgs = [
    {"role": "user", "content": "q1"},
    {"role": "assistant", "content": "a1", "reasoning_content": ""},
    {"role": "user", "content": "q2"},
]
out = P.backfill_reasoning_content(
    copy.deepcopy(msgs), "deepseek-v4.1-flash", thinking_enabled=True
)
a = asst(out)[0]
check("reasoning_content stays a string", isinstance(a.get("reasoning_content"), str))
check(
    "reasoning mirror is non-empty",
    a.get("reasoning") not in (None, ""),
    repr(a.get("reasoning")),
)

print()
print("[6] thinking disabled + no trace: untouched")
msgs = [
    {"role": "user", "content": "q1"},
    {"role": "assistant", "content": "a1"},
    {"role": "user", "content": "q2"},
]
out = P.backfill_reasoning_content(
    copy.deepcopy(msgs), "deepseek-v4.1-flash", thinking_enabled=False
)
a = asst(out)[0]
check(
    "no field injected when thinking disabled and no trace",
    "reasoning_content" not in a,
    sorted(a),
)

print()
print("[7] thinking disabled + existing trace: still backfilled (hasTrace half)")
msgs = [
    {"role": "user", "content": "q1"},
    {"role": "assistant", "content": "a1", "reasoning_content": "trace"},
    {"role": "user", "content": "q2"},
    {"role": "assistant", "content": "a2"},
]
out = P.backfill_reasoning_content(
    copy.deepcopy(msgs), "deepseek-v4.1-flash", thinking_enabled=False
)
a = asst(out)
check("traced assistant keeps field", a[0]["reasoning_content"] == "trace")
check("untraced assistant gains the field", "reasoning_content" in a[1], sorted(a[1]))

print()
print("[8] non-deepseek model: zero changes")
msgs = [
    {"role": "user", "content": "q1"},
    {"role": "assistant", "content": "a1"},
    {"role": "user", "content": "q2"},
]
for model in ("glm-5.3", "kimi-k3", "gpt-5.6-sol", "hy3"):
    out = P.backfill_reasoning_content(
        copy.deepcopy(msgs), model, thinking_enabled=True
    )
    a = asst(out)[0]
    check(f"{model} untouched", "reasoning_content" not in a, sorted(a))

print()
print("[9] non-dict entries survive untouched")
msgs = [
    {"role": "user", "content": "q1"},
    "junk",
    {"role": "assistant", "content": "a1"},
]
out = P.backfill_reasoning_content(
    copy.deepcopy(msgs), "deepseek-v4.1-flash", thinking_enabled=True
)
check("junk entry preserved", "junk" in out, out)

print()
print("[10] build_upstream_body wires thinking_enabled through")
payload = {
    "model": "deepseek-v4.1-flash",
    "messages": [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ],
}
body = P.build_upstream_body(copy.deepcopy(payload))
a = [m for m in body["messages"] if m.get("role") == "assistant"][0]
check(
    "deepseek payload backfilled end to end",
    isinstance(a.get("reasoning_content"), str),
    sorted(a),
)
check(
    "thinking enabled injected",
    (body.get("thinking") or {}).get("type") == "enabled",
    body.get("thinking"),
)

payload2 = copy.deepcopy(payload)
payload2["thinking"] = {"type": "disabled"}
body2 = P.build_upstream_body(payload2)
a2 = [m for m in body2["messages"] if m.get("role") == "assistant"][0]
check(
    "explicit thinking disabled is respected", "reasoning_content" not in a2, sorted(a2)
)

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
