"""Regression tests for function parameters schema normalization (code 11129)
and tool_calls arguments serialization (code 11101).

Reported symptom:
Clients (e.g. Hermes Agent, OpenAI-compatible SDKs) defining zero-argument tools
emit `"parameters": {}` (missing `type: "object"`). The upstream Go struct validator
strictly rejects the request with HTTP 400:
    upstream 400: {"code":11129,"msg":"invalid function call parameters"}
    extError: {"code":"invalid_function_parameters", ...}
    displayMsg: {"zh":"工具定义不合规，请新建任务重试"}

Similar failures occur when:
  - parameters is empty dict or None
  - parameters specifies properties but omits `type: "object"`
  - required is None or a non-list string
  - history tool_calls function arguments is a dict instead of string

These tests verify:
  1. Parameters dictionary is normalized to have `type: "object"` and a valid `properties` dict.
  2. `required` field is validated into a clean list of strings or dropped.
  3. Empty `tools` array is stripped to avoid upstream rejecting empty tool list.
  4. Root-level legacy `functions` parameter is mapped to `tools`.
  5. In-history `tool_calls[].function.arguments` as a dict or None is properly serialized to JSON string.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _isolated_dirs import isolated_data_dirs  # noqa: E402
_TMP = isolated_data_dirs("wb-tool-params-norm-")

import wb_proxy as P
import wb_transforms as T

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


print("[1] Zero-argument tool parameters: {} gain type: object and properties: {}")
fn = {"name": "get_time", "description": "time", "parameters": {}}
T._normalize_tool_parameters(fn)
check("type is object", fn["parameters"].get("type") == "object", fn["parameters"])
check("properties is dict", isinstance(fn["parameters"].get("properties"), dict), fn["parameters"])

print()
print("[2] Missing type with properties gains type: object")
fn = {"name": "search", "parameters": {"properties": {"q": {"type": "string"}}}}
T._normalize_tool_parameters(fn)
check("type added", fn["parameters"].get("type") == "object", fn["parameters"])
check("properties preserved", "q" in fn["parameters"].get("properties", {}), fn["parameters"])

print()
print("[3] None or non-dict parameters normalized to valid object schema")
for val in (None, [], "str", True):
    fn = {"name": "test", "parameters": val}
    T._normalize_tool_parameters(fn)
    check("val=%r -> valid schema" % (val,),
          fn["parameters"] == {"type": "object", "properties": {}}, fn["parameters"])

print()
print("[4] required field cleaned up")
fn = {"name": "t1", "parameters": {"type": "object", "properties": {"a": {}}, "required": None}}
T._normalize_tool_parameters(fn)
check("required None dropped", "required" not in fn["parameters"], fn["parameters"])

fn = {"name": "t2", "parameters": {"type": "object", "properties": {"a": {}}, "required": "a"}}
T._normalize_tool_parameters(fn)
check("required string wrapped into list", fn["parameters"].get("required") == ["a"], fn["parameters"])

fn = {"name": "t3", "parameters": {"type": "object", "properties": {"a": {}, "b": {}}, "required": ["a", None, "b"]}}
T._normalize_tool_parameters(fn)
check("required list filtered None", fn["parameters"].get("required") == ["a", "b"], fn["parameters"])

print()
print("[5] normalize_tools end-to-end on payload")
obj = {
    "tools": [
        {"type": "function", "function": {"name": "fn1", "parameters": {}}},
        {"type": "function", "name": "fn2", "parameters": {}}
    ]
}
P.normalize_tools(obj)
check("fn1 normalized", obj["tools"][0]["function"]["parameters"] == {"type": "object", "properties": {}}, obj)
check("fn2 wrapped & normalized", obj["tools"][1]["function"]["parameters"] == {"type": "object", "properties": {}}, obj)

print()
print("[6] Empty tools list stripped")
obj = {"tools": []}
P.normalize_tools(obj)
check("empty tools popped", "tools" not in obj, obj)

print()
print("[7] Legacy functions mapped to tools")
obj = {
    "functions": [
        {"name": "legacy_fn", "parameters": {}}
    ]
}
P.normalize_tools(obj)
check("functions popped", "functions" not in obj, obj)
check("tools gained", "tools" in obj and len(obj["tools"]) == 1, obj)
check("legacy_fn parameters normalized", obj["tools"][0]["function"]["parameters"] == {"type": "object", "properties": {}}, obj)

print()
print("[8] sanitize_tool_calls: non-string arguments (dict, list, int, bool, None) serialized to string")
tool_calls = [
    {
        "id": "call_1",
        "type": "function",
        "function": {"name": "fn1", "arguments": {"city": "Beijing"}}
    },
    {
        "id": "call_2",
        "type": "function",
        "function": {"name": "fn2", "arguments": None}
    },
    {
        "id": "call_3",
        "type": "function",
        "function": {"name": "fn3", "arguments": "{\"city\":\"Shanghai\"}"}
    },
    {
        "id": "call_4",
        "type": "function",
        "function": {"name": "fn4", "arguments": [1, 2, "a"]}
    },
    {
        "id": "call_5",
        "type": "function",
        "function": {"name": "fn5", "arguments": 123}
    },
    {
        "id": "call_6",
        "type": "function",
        "function": {"name": "fn6", "arguments": True}
    }
]
res = T.sanitize_tool_calls(tool_calls)
check("dict arguments converted to JSON string", res[0]["function"]["arguments"] == '{"city": "Beijing"}', res[0]["function"]["arguments"])
check("None arguments converted to empty JSON object", res[1]["function"]["arguments"] == "{}", res[1]["function"]["arguments"])
check("string arguments untouched", res[2]["function"]["arguments"] == '{"city":"Shanghai"}', res[2]["function"]["arguments"])
check("list arguments converted to JSON string", res[3]["function"]["arguments"] == '[1, 2, "a"]', res[3]["function"]["arguments"])
check("int arguments converted to JSON string", res[4]["function"]["arguments"] == '123', res[4]["function"]["arguments"])
check("bool arguments converted to JSON string", res[5]["function"]["arguments"] == 'true', res[5]["function"]["arguments"])

print()
print("[9] Deep recursive normalization: python type aliases mapped to json schema types")
fn = {
    "name": "calc",
    "parameters": {
        "type": "dict",
        "properties": {
            "a": {"type": "int"},
            "b": {"type": "float"},
            "c": {"type": "str"},
            "d": {"type": "bool"},
            "items": {"type": "list", "items": {"type": "str"}}
        }
    }
}
T._normalize_tool_parameters(fn)
p = fn["parameters"]
check("dict mapped to object", p.get("type") == "object", p)
check("int mapped to integer", p["properties"]["a"].get("type") == "integer", p)
check("float mapped to number", p["properties"]["b"].get("type") == "number", p)
check("str mapped to string", p["properties"]["c"].get("type") == "string", p)
check("bool mapped to boolean", p["properties"]["d"].get("type") == "boolean", p)
check("list mapped to array and inner str to string", p["properties"]["items"].get("type") == "array" and p["properties"]["items"]["items"].get("type") == "string", p)

print()
print("[10] Deep recursive normalization: null description / title / format stripped")
fn = {
    "name": "nested_clean",
    "parameters": {
        "type": "object",
        "properties": {
            "top_val": {"type": "string", "description": None, "title": None},
            "sub": {
                "type": "object",
                "properties": {
                    "inner": {"type": "string", "description": None}
                }
            },
            "tags": {
                "type": "array",
                "items": {"type": "string", "description": None}
            }
        }
    }
}
T._normalize_tool_parameters(fn)
p = fn["parameters"]
check("top_val description stripped", "description" not in p["properties"]["top_val"], p)
check("top_val title stripped", "title" not in p["properties"]["top_val"], p)
check("sub.inner description stripped", "description" not in p["properties"]["sub"]["properties"]["inner"], p)
check("tags items description stripped", "description" not in p["properties"]["tags"]["items"], p)

print()
print("[11] Tuple items array unpacked into single dictionary")
fn = {
    "name": "tuple_tool",
    "parameters": {
        "type": "object",
        "properties": {
            "coords": {
                "type": "array",
                "items": [{"type": "number", "description": None}, {"type": "number"}]
            }
        }
    }
}
T._normalize_tool_parameters(fn)
p = fn["parameters"]
check("items converted to dict", isinstance(p["properties"]["coords"]["items"], dict), p)
check("items type preserved", p["properties"]["coords"]["items"].get("type") == "number", p)
check("items inner null description stripped", "description" not in p["properties"]["coords"]["items"], p)

print()
print("[12] Definitions / $defs and anyOf / oneOf recursively normalized")
fn = {
    "name": "defs_tool",
    "parameters": {
        "type": "object",
        "$defs": {
            "Config": {"type": "dict", "properties": {"timeout": {"type": "int", "description": None}}}
        },
        "properties": {
            "mode": {
                "anyOf": [
                    {"type": "str", "description": None},
                    {"type": "int"}
                ]
            }
        }
    }
}
T._normalize_tool_parameters(fn)
p = fn["parameters"]
check("$defs Config type mapped to object", p["$defs"]["Config"].get("type") == "object", p)
check("$defs timeout type mapped to integer", p["$defs"]["Config"]["properties"]["timeout"].get("type") == "integer", p)
check("$defs timeout null description stripped", "description" not in p["$defs"]["Config"]["properties"]["timeout"], p)
check("anyOf branch 0 mapped to string", p["properties"]["mode"]["anyOf"][0].get("type") == "string", p)
check("anyOf branch 0 null description stripped", "description" not in p["properties"]["mode"]["anyOf"][0], p)
check("anyOf branch 1 mapped to integer", p["properties"]["mode"]["anyOf"][1].get("type") == "integer", p)

print()
print("[13] Tools wrapping without type='function'")
obj = {
    "tools": [
        {"name": "bare_tool", "parameters": {"type": "object"}}
    ]
}
P.normalize_tools(obj)
check("bare_tool wrapped with function", "function" in obj["tools"][0], obj)
check("bare_tool type set to function", obj["tools"][0].get("type") == "function", obj)
check("bare_tool parameters normalized", obj["tools"][0]["function"]["parameters"].get("properties") == {}, obj)

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
