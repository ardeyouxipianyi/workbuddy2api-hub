"""Protocol translation: Responses API and Anthropic Messages <-> Chat Completions.

The WorkBuddy upstream only speaks Chat Completions. Clients that speak OpenAI's
Responses API (Codex, Kelivo) or Anthropic Messages (Claude Code) are translated
down on the way in, and the reply - aggregated or streamed - is translated back
up into Responses objects / SSE events or Anthropic message events.

Also here: the gateway-run web tools (web_search / web_fetch) that a Responses
request can delegate, custom and namespace tools, and remote compaction.

The few calls back into the gateway (logging, opening another upstream turn,
the catalogue's max output tokens, the accounts directory) resolve the running
instance at call time through `_gw()`, so tests patching wb_proxy still apply.
"""

import base64
import json
import re
import socket
import time

import wb_runtime
import wb_settings
import wb_webtools
from wb_transforms import (DSML_CALLS_RE, is_truncated_arguments,
                           parse_dsml_tool_calls, preserve_request_contract_enabled)
from wb_wire import (_best_cached_tokens, _new_id, estimate_tokens, iter_sse_lines,
                     strip_data_prefix)

_gw = wb_runtime.gateway


# ---------------------------------------------------------------------------
# Responses API (/v1/responses) <-> Chat Completions translation
# ---------------------------------------------------------------------------
#
# Kelivo and other clients can speak OpenAI's newer Responses API. The upstream
# gateway only speaks Chat Completions, so those requests are translated down,
# and the reply is translated back up into Responses objects / SSE events.
def local_ip_addresses():
    """Every non-loopback IPv4 address this machine answers on."""
    found = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found and not ip.startswith("127."):
                found.append(ip)
    except Exception:
        pass
    if not found:
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("8.8.8.8", 80))
            found.append(probe.getsockname()[0])
            probe.close()
        except Exception:
            pass
    return found
def _flatten_content(content):
    """Flatten Responses-style content into text, or OpenAI vision parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    texts, parts = [], []
    for piece in content:
        if isinstance(piece, str):
            texts.append(piece)
            parts.append({"type": "text", "text": piece})
            continue
        if not isinstance(piece, dict):
            continue
        ptype = piece.get("type") or ""
        if ptype in ("input_text", "output_text", "text", "summary_text"):
            t = piece.get("text") or ""
            texts.append(t)
            parts.append({"type": "text", "text": t})
        elif ptype in ("input_image", "image_url", "image") or "image_url" in piece:
            url = piece.get("image_url") or piece.get("url")
            if isinstance(url, dict):
                url = url.get("url")
            if not url and piece.get("data"):
                mime = piece.get("mimeType") or piece.get("mime_type") or "image/png"
                url = f"data:{mime};base64," + piece["data"]
            if url:
                parts.append({"type": "image_url", "image_url": {"url": url}})
    if any(p.get("type") == "image_url" for p in parts):
        return parts          # multimodal: keep structured parts
    return chr(10).join(t for t in texts if t)


# ---------------------------------------------------------------------------
# Responses API "custom" (freeform) tools
#
# Some clients - most notably Codex 0.15x - declare their file-editing tool as a
# *custom* (freeform) tool rather than a JSON-schema function:
#
#     {"type": "custom", "name": "apply_patch", "format": {...grammar...}}
#
# and expect the model to answer with a custom_tool_call item carrying the raw
# payload in "input", then feed the result back as custom_tool_call_output.
#
# The upstream chat endpoint has no notion of custom tools, so we downgrade them
# to ordinary function tools with a single "input" string parameter on the way
# out, and re-inflate them to custom_tool_call on the way back. Without this the
# tool is silently ignored: the model emits the payload as ordinary prose and the
# client never sees a tool call (measured: 52 text deltas, 0 tool items).
# ---------------------------------------------------------------------------

CUSTOM_TOOL_HINT = (
    "This is a freeform tool. Put the COMPLETE raw payload into the single "
    "'input' string parameter, verbatim. Do not wrap it in JSON, do not wrap "
    "it in markdown code fences, do not add commentary."
)


# ---------------------------------------------------------------------------
# namespace 工具拒绝
#
# Codex 会把 MCP server / 外挂工具用 type="namespace" 的形式送出来。实测行为：
#   反代「接受」namespace 工具 -> app 把 MCP／外挂工具当成不可执行
#                                -> 每一次呼叫都回 "unsupported call"
#   反代「拒绝」namespace 工具 -> app 自动 fallback 成 flat function 清单
#                                -> 全部工具恢复正常
# （此行为在 Command Code proxy.mjs 的 CC_REJECT_NAMESPACE_TOOLS 实验里有记载，
#   Agent Router 也是靠直接拒绝这类请求才正常的。）
#
# 所以在这里主动回一个格式明确的 400，逼 app 走 fallback。
# 想还原成「照单全收」就把 REJECT_NAMESPACE_TOOLS 改成 False。
# ---------------------------------------------------------------------------

REJECT_NAMESPACE_TOOLS = False  # 保持关闭：正解是展开+还原 namespace

NAMESPACE_TOOL_MESSAGE = (
    'Unsupported tool type "namespace": this endpoint only supports flat '
    '"function" tools. Resend the tools as individual function entries.'
)


def find_namespace_tool(tools):
    """回传第一个 type=="namespace" 的工具名称，没有就回 None。"""
    for t in tools or []:
        if isinstance(t, dict) and str(t.get("type") or "").lower() == "namespace":
            return str(t.get("name") or t.get("server_label") or "(unnamed)")
    return None


def _is_custom_tool(tool):
    return isinstance(tool, dict) and str(tool.get("type") or "").lower() == "custom"


def custom_tool_names(tools, _depth=0):
    """Names of tools declared as freeform/custom in a Responses request.

    递归进 namespace：新版 Codex Desktop 把自由格式工具嵌在 functions 命名
    空间里（exec 的 type 就是 "custom"）。漏掉它的后果不是「少一个名字」，
    而是回程被判成 function_call，客户端按 (name, namespace, kind) 三元组匹
    配不到执行器，调用不执行、不返回，模型只能无限重试。namespace 只是客户
    端给工具分组的方式，不是停止查找的理由。
    """
    names = set()
    if _depth > NAMESPACE_MAX_DEPTH:
        return names
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if _is_custom_tool(t) and t.get("name"):
            names.add(str(t["name"]))
        elif str(t.get("type") or "").lower() == "namespace":
            subs = t.get("tools")
            if not isinstance(subs, list):
                subs = t.get("children")
            if not isinstance(subs, list):
                subs = t.get("functions")
            names |= custom_tool_names(subs, _depth + 1)
    return names


def _downgrade_custom_tool(tool):
    """Rewrite a Responses custom tool into a Chat function tool."""
    desc = tool.get("description") or ""
    fmt = tool.get("format") or {}
    extra = ""
    if isinstance(fmt, dict) and fmt.get("definition"):
        extra = chr(10) + chr(10) + "Grammar:" + chr(10) + str(fmt["definition"])
    return {
        "type": "function",
        "name": tool.get("name") or "",
        "description": (desc + chr(10) + chr(10) + CUSTOM_TOOL_HINT + extra).strip(),
        "parameters": {
            "type": "object",
            "properties": {
                "input": {
                    "type": "string",
                    "description": "Complete raw payload for this tool, verbatim.",
                }
            },
            "required": ["input"],
        },
    }


# ---------------------------------------------------------------------------
# namespace 工具：展开 + 还原
#
# 新版 Codex App 把 MCP／外挂工具用 namespace 形式送出：
#   {"type":"namespace","name":"codex_app","tools":[{name:"list_threads",...}]}
#
# 上游 Chat Completions 只认 flat function，看不懂 namespace。
# 但 App 回程是用 (name, namespace) 两个栏位找执行器 ——
# 只给 flat name，App 一律回 "unsupported call"（实测 js / list_threads 全灭）。
#
# 三件事：
#   1. Expand   送上游前把 namespace 展开成 flat function，记住 name -> namespace
#   2. Normalise 模型回传的 name 可能是 js / ns__js / ns::js，都要能解析
#   3. Restore   回程的 function_call / custom_tool_call 补上 namespace 栏位
#
# 参考：某开源 CodeBuddy/WorkBuddy 反向代理项目的 tool-namespaces 说明
# ---------------------------------------------------------------------------

NAMESPACE_MAX_DEPTH = 4
_NS_SEP = "__"


def expand_namespace_tools(tools, _depth=0):
    """把 namespace 展开成 flat function 清单，其余工具原样保留。

      * 子工具可能在 tools / children / functions 任一栏位
      * namespace 子工具常常没有 type 栏位，展开时补成上游认得的 flat function
      * custom / web_search 等非 function 项目原样留下，交给既有管线处理
      * 同名只留第一个
      * 回传 (flat_tools, name_to_namespace)
    """
    flat = []
    mapping = {}
    seen = set()
    max_depth = max(0, int(_depth) + NAMESPACE_MAX_DEPTH)

    def collect(entry, depth, ns_name):
        if not isinstance(entry, dict) or depth > max_depth:
            return
        etype = str(entry.get("type") or "").lower()
        if etype == "namespace":
            subs = entry.get("tools")
            if not isinstance(subs, list):
                subs = entry.get("children")
            if not isinstance(subs, list):
                subs = entry.get("functions")
            if not isinstance(subs, list):
                subs = []
            child_ns = str(entry.get("name") or ns_name or "")
            for sub in subs:
                collect(sub, depth + 1, child_ns)
            return
        if ns_name and etype in ("", "function"):
            fn = entry.get("function") if isinstance(entry.get("function"), dict) else None
            if fn is None:
                fn = {
                    "name": entry.get("name"),
                    "description": entry.get("description") or "",
                    "parameters": entry.get("parameters") or entry.get("input_schema")
                                  or {"type": "object", "properties": {}},
                }
                if preserve_request_contract_enabled() and "strict" in entry:
                    fn["strict"] = entry["strict"]
            name = str(fn.get("name") or "").strip()
            if not name or name in seen:
                return
            seen.add(name)
            mapping[name] = ns_name
            flat_fn = {
                "type": "function",
                "name": name,
                "description": fn.get("description") or "",
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            }
            if "strict" in fn:
                flat_fn["strict"] = fn["strict"]
            flat.append(flat_fn)
            return
        fn = entry.get("function") if isinstance(entry.get("function"), dict) else {}
        name = str(entry.get("name") or fn.get("name") or "").strip()
        if name:
            if name in seen:
                return
            seen.add(name)
            if ns_name:
                mapping[name] = ns_name
        flat.append(entry)

    for entry in tools or []:
        collect(entry, 0, "")
    return flat, mapping


def resolve_namespaced_name(name, mapping):
    """把模型回传的名字解析回 (bare_name, namespace)。接受 js / ns__js / ns::js。"""
    if not name:
        return name, ""
    name = str(name)
    if name in mapping:
        return name, mapping[name]
    if "::" in name:
        idx = name.find("::")
        if idx > 0:
            tail = name[idx + 2:]
            head = name[:idx]
            if tail in mapping:
                return tail, mapping[tail]
            return tail, head

    # ns__tool 用精确比对，避免 namespace 内含 '__'（如 codex_apps__github）时切错
    for tool, ns in mapping.items():
        if name == ns + _NS_SEP + tool:
            return tool, ns

    return name, ""


def stamp_namespace(item, mapping):
    """把模型回传的扁平工具名还原成 (name, namespace)。

    串流的 response.output_item.done 事件才是客户端派发工具呼叫的依据，
    所以每个 function_call / custom_tool_call 项目都要在送出前补上 namespace。
    """
    if not mapping or not isinstance(item, dict):
        return item
    bare, ns = resolve_namespaced_name(item.get("name"), mapping)
    if ns:
        item["name"] = bare
        item["namespace"] = ns
    return item


def apply_namespace_to_calls(output_items, mapping):
    """替 Responses 的 function_call / custom_tool_call 补上 namespace。"""
    if not mapping or not isinstance(output_items, list):
        return output_items, 0
    fixed = 0
    for item in output_items:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in ("function_call", "custom_tool_call"):
            continue
        if item.get("namespace"):
            continue
        bare, ns = resolve_namespaced_name(item.get("name"), mapping)
        if ns:
            item["name"] = bare
            item["namespace"] = ns
            fixed += 1
    return output_items, fixed


def additional_tools_from_input(payload):
    """从 input 数组里收集 additional_tools 条目的 tools。

    新版 Codex Desktop 0.162+ 把工具放在 input 内的
    {"type": "additional_tools", "tools": [...]} 条目里，顶层 tools 是空的。
    出站（responses_to_chat）与回程（custom_tool_names / request_meta.tools）
    的取值都必须走这里，否则两边看到的是不同的工具清单。
    """
    collected = []
    for item in payload.get("input") or []:
        if isinstance(item, dict) and item.get("type") == "additional_tools":
            sub_tools = item.get("tools")
            if isinstance(sub_tools, list):
                collected.extend(sub_tools)
    return collected


def _tools_for_chat(tools):
    """Downgrade custom tools; leave everything else untouched."""
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        out.append(_downgrade_custom_tool(t) if _is_custom_tool(t) else t)
    return out


def _unwrap_custom_input(args):
    """Pull the freeform string back out of an {"input": "..."} argument blob."""
    if not isinstance(args, str):
        return json.dumps(args or "", ensure_ascii=False)
    try:
        parsed = json.loads(args)
    except Exception:
        return args
    if isinstance(parsed, dict):
        val = parsed.get("input")
        if isinstance(val, str):
            return val
        if val is not None:
            return json.dumps(val, ensure_ascii=False)
    if isinstance(parsed, str):
        return parsed
    return args

# The gateway can run web_search / web_fetch itself; the panel switch decides.
#
# Some clients (Codex App and similar harnesses) declare web_search as a
# server-side tool, but the upstream has no executor for it: forwarding the
# declaration leaves the model answering as if no tool had been offered. With
# the switch on, the gateway swaps the declaration for a function of its own,
# swallows the calls and runs them locally (wb_webtools), then feeds the
# results back.
#
# Off by default: the declaration is forwarded untouched and a client that
# declares its own search tool receives the call - the behaviour since v1.5.3.
# Turning it on means the gateway itself fetches URLs a model asks for, so the
# egress policy is the operator's call.
def local_web_tools_enabled():
    """Panel switch: does this gateway run web_search / web_fetch itself?

    Read per request, so flipping the panel takes effect on the next one
    without a restart.
    """
    try:
        return wb_settings.local_web_tools(_gw().ACCOUNTS_DIR) is True
    except Exception:
        return False


def web_tools_active(body):
    """True when this request's tools were swapped for the gateway's own.

    Interception only applies to a request whose definitions the gateway
    injected: with the switch off, a client's own same-named function must be
    forwarded instead of being swallowed here.
    """
    return isinstance(body, dict) and body.get("_web_tools") is True


def sum_usage(total, part):
    """把一轮的 token 用量累加起来。

    代跑网路工具会多跑好几次上游，那些 token 是真的花掉的，所以记帐要加总，
    不能让最后一轮盖掉前面几轮。
    """
    if not isinstance(part, dict):
        return total
    if not isinstance(total, dict):
        total = {}
    for key, value in part.items():
        if isinstance(value, dict):
            total[key] = sum_usage(total.get(key), value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] = (total.get(key) or 0) + value
        elif key not in total:
            total[key] = value
    return total


_CITATION_MD_RE = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^)\s]+)\)")


def build_citations(text, sources):
    """把模型实际引用到的来源转成 url_citation annotations。

    只标注真的有出现在工具输出里的网址 —— 模型自己编的连结不会被当成引用。
    """
    text = str(text or "")
    if not text or not sources:
        return []
    by_url = {}
    for s in sources or []:
        if not isinstance(s, dict):
            continue
        url = str(s.get("url") or "").strip()
        if not url:
            continue
        by_url.setdefault(url, s)
        by_url.setdefault(url.rstrip("/"), s)

    anns = []
    seen = set()

    def add(url, title, start, end):
        key = (url, start, end)
        if key in seen or start < 0 or end <= start:
            return
        seen.add(key)
        anns.append({
            "type": "url_citation",
            "url": url,
            "title": title or url,
            "start_index": start,
            "end_index": end,
        })

    md_spans = []
    for m in _CITATION_MD_RE.finditer(text):
        url = m.group(2)
        src = by_url.get(url) or by_url.get(url.rstrip("/"))
        if not src:
            continue
        md_spans.append((m.start(0), m.end(0)))
        add(url, src.get("title") or m.group(1), m.start(0), m.end(0))

    for m in re.finditer(r"https?://[^\s<>()\[\]]+", text):
        if any(m.start(0) >= s and m.end(0) <= e for s, e in md_spans):
            continue
        url = m.group(0).rstrip(".,;:!?")
        src = by_url.get(url) or by_url.get(url.rstrip("/"))
        if not src:
            continue
        add(url, src.get("title"), m.start(0), m.start(0) + len(url))

    anns.sort(key=lambda a: (a["start_index"], a["end_index"]))
    return anns


def follow_up_with_tool_results(internal_calls, holder, model, session_key, t_start,
                                drop_tools=False, allowed_accounts=None):
    """执行反代自己代跑的网路工具，把结果喂回模型，回传新的上游连线。

    drop_tools=True 表示这是最后一轮：把网路工具从工具清单收回，模型没有东西
    可以再呼叫，只能用手上的结果把话讲完。旧版在回合用尽时合成一个
    resp_wrapup（status=completed、output=[]）收尾，那等于把失败伪装成正常
    结束，客户端看到的就是「讲到一半断掉」——issue #43。
    """
    convo = holder.get("convo_messages")
    if convo is None:
        convo = list(holder.get("base_messages") or [])
        holder["convo_messages"] = convo

    tool_calls = []
    for i, c in enumerate(internal_calls):
        tool_calls.append({
            "id": "call_web_%d_%d" % (int(t_start * 1000) % 1000000, i),
            "type": "function",
            "function": {"name": c["name"], "arguments": c.get("arguments") or "{}"},
        })
    convo.append({"role": "assistant", "content": None, "tool_calls": tool_calls})

    for tc in tool_calls:
        nm = tc["function"]["name"]
        result = wb_webtools.execute(nm, tc["function"]["arguments"])
        found = wb_webtools.sources_from_result(result)
        if found:
            holder.setdefault("web_sources", []).extend(found)
        _gw().log("web tool %s -> %d chars, %d citeable source(s)"
            % (nm, len(result or ""), len(found)), level="INFO")
        convo.append({
            "role": "tool",
            "tool_call_id": tc["id"],
            "name": nm,
            "content": result,
        })

    body = dict(holder.get("base_body") or {})
    if drop_tools:
        body["tools"] = [t for t in (body.get("tools") or [])
                         if not wb_webtools.is_internal_tool(tool_name_of(t))]
        convo.append({
            "role": "system",
            "content": ("The web tools are no longer available. Answer the user now with "
                        "what you already have. Do not say that you are searching again."),
        })
    body["messages"] = convo
    body["stream"] = True
    kwargs = dict(session_key=session_key, target_realm=holder.get("realm"))
    if allowed_accounts is not None:
        kwargs["allowed_accounts"] = allowed_accounts
    return _gw().open_upstream(body, **kwargs)


def internal_calls_from_chat(chat_obj, web_tools=False):
    """Calls in an aggregated chat completion the gateway runs itself.

    Only a request whose definitions the gateway injected can carry such a
    call; with the switch off a client's own same-named function stays the
    client's, so this answers empty.
    """
    message = ((chat_obj.get("choices") or [{}])[0] or {}).get("message") or {}
    out = []
    if not web_tools:
        return out
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        if wb_webtools.is_internal_tool(name):
            out.append({"name": name, "arguments": fn.get("arguments") or "{}"})
    return out


def tool_name_of(tool):
    """Tool name, whichever of the two shapes the entry uses."""
    if not isinstance(tool, dict):
        return ""
    if isinstance(tool.get("function"), dict):
        return str((tool.get("function") or {}).get("name") or "")
    return str(tool.get("name") or "")


# Codex remote compaction v2: the client sends the whole retained history plus a
# `{"type":"compaction_trigger"}` item and expects exactly one `compaction`
# output item back. The upstream Chat Completions API has no such concept, so
# the gateway performs the summarization itself and stores the summary in the
# item's opaque `encrypted_content` field. Codex replays that item verbatim on
# later turns; `_responses_input_to_messages` below turns it back into a normal
# context message, which is what makes a compacted thread continue correctly.
COMPACTION_PROMPT = (
    "You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff "
    "summary for another LLM that will resume the task.\n\n"
    "Include:\n"
    "- Current progress and key decisions made\n"
    "- Important context, constraints, or user preferences\n"
    "- What remains to be done (clear next steps)\n"
    "- Any critical data, examples, or references needed to continue\n\n"
    "Be concise, structured, and focused on helping the next LLM seamlessly "
    "continue the work. Return only the summary."
)

COMPACTION_ITEM_TYPES = ("compaction", "compaction_summary", "context_compaction")


def _has_compaction_trigger(payload):
    """True when a Responses request is Codex's native compaction turn."""
    if not isinstance(payload, dict):
        return False
    inp = payload.get("input")
    if isinstance(inp, str):
        return inp.strip() == "compaction_trigger"
    if isinstance(inp, list):
        return any(isinstance(item, dict) and item.get("type") == "compaction_trigger"
                   for item in inp)
    return False


def _encode_compaction_summary(text):
    """Pack a summary into the opaque string Codex stores and replays."""
    raw = json.dumps({"v": 1, "summary": str(text or "")},
                     ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _decode_compaction_summary(blob):
    """Best-effort decode of a compaction item's `encrypted_content`."""
    text = str(blob or "").strip()
    if not text:
        return ""
    try:
        padded = text + "=" * (-len(text) % 4)
        obj = json.loads(base64.b64decode(padded).decode("utf-8"))
        if isinstance(obj, dict) and isinstance(obj.get("summary"), str):
            return obj["summary"]
    except Exception:
        pass
    # A foreign provider may hand back plain text; keep it rather than drop the
    # only copy of the summary.
    return text


def _responses_input_to_messages(payload):
    """Turn the Responses input items into chat messages."""
    messages = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    inp = payload.get("input")
    pending_reasoning = ""
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype in (None, "message", "user"):
                body = _flatten_content(item.get("content"))
                if body:
                    role = item.get("role") or "user"
                    if role == "developer":
                        role = "system"
                    # If this is assistant text and the previous message is an assistant
                    # message (e.g. from an adjacent function_call), merge them so
                    # tool_calls and text stay in one message without breaking tool sequence.
                    if role == "assistant" and messages and messages[-1].get("role") == "assistant":
                        prev = messages[-1]
                        if prev.get("content"):
                            prev["content"] = str(prev["content"]) + chr(10) + str(body)
                        else:
                            prev["content"] = body
                        if pending_reasoning and "reasoning_content" not in prev:
                            prev["reasoning_content"] = pending_reasoning
                            pending_reasoning = ""
                    else:
                        msg_dict = {"role": role, "content": body}
                        if role == "assistant" and pending_reasoning:
                            msg_dict["reasoning_content"] = pending_reasoning
                            pending_reasoning = ""
                        messages.append(msg_dict)
            elif itype == "reasoning":
                # Reasoning item from previous assistant turn in Responses API.
                # In standard Chat Completions, reasoning is either backfilled into
                # the assistant message's reasoning_content or omitted.
                r_text = ""
                summ = item.get("summary")
                if isinstance(summ, list):
                    r_text = chr(10).join(
                        p.get("text", "") for p in summ if isinstance(p, dict) and p.get("text")
                    )
                elif isinstance(summ, str):
                    r_text = summ
                if not r_text:
                    cnt = item.get("content")
                    if isinstance(cnt, str):
                        r_text = cnt
                    elif isinstance(cnt, list):
                        r_text = _flatten_content(cnt)
                if r_text:
                    if messages and messages[-1].get("role") == "assistant":
                        messages[-1]["reasoning_content"] = r_text
                    else:
                        pending_reasoning = r_text
            elif itype == "function_call_output":
                raw_out = item.get("output")
                if not item.get("call_id"):
                    if isinstance(raw_out, list):
                        _txt = _flatten_content(raw_out)
                    elif isinstance(raw_out, dict):
                        _txt = json.dumps(raw_out, ensure_ascii=False)
                    else:
                        _txt = str(raw_out or "")
                    if isinstance(_txt, list):
                        # _flatten_content returns structured chat parts when
                        # the output carries images. Keep them as multimodal
                        # user content instead of crashing on .strip().
                        try:
                            _gw().log("[wb-proxy] orphan function_call_output kept as multimodal parts (%d)"
                                % len(_txt))
                        except Exception:
                            pass
                        messages.append({
                            "role": "user",
                            "content": ([{"type": "text", "text":
                                          "[Message from another task - treat this "
                                          "as a user instruction]"}] + _txt),
                        })
                        continue
                    _txt = (_txt or "").strip()
                    if _txt:
                        messages.append({
                            "role": "user",
                            "content": ("[Message from another task - treat this "
                                        "as a user instruction]" + chr(10) + chr(10) + _txt),
                        })
                        continue
                if isinstance(raw_out, list):
                    content = _flatten_content(raw_out)
                elif isinstance(raw_out, dict):
                    if raw_out.get("type") in ("input_image", "image_url", "image") or "image_url" in raw_out:
                        content = _flatten_content([raw_out])
                    else:
                        content = json.dumps(raw_out, ensure_ascii=False)
                elif isinstance(raw_out, str):
                    content = raw_out
                else:
                    content = str(raw_out)
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or "",
                    "content": content,
                })
            elif itype == "function_call":
                tc_item = {
                    "id": item.get("call_id") or item.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "{}",
                    },
                }
                # Merge into previous assistant message if adjacent
                if messages and messages[-1].get("role") == "assistant":
                    prev = messages[-1]
                    if "tool_calls" in prev:
                        prev["tool_calls"].append(tc_item)
                    else:
                        prev["tool_calls"] = [tc_item]
                    if pending_reasoning and "reasoning_content" not in prev:
                        prev["reasoning_content"] = pending_reasoning
                        pending_reasoning = ""
                else:
                    msg_dict = {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [tc_item],
                    }
                    if pending_reasoning:
                        msg_dict["reasoning_content"] = pending_reasoning
                        pending_reasoning = ""
                    messages.append(msg_dict)
            elif itype == "custom_tool_call":
                # Freeform tool call coming back as conversation history.
                raw_input = item.get("input")
                if isinstance(raw_input, (dict, list)):
                    raw_input = json.dumps(raw_input, ensure_ascii=False)
                if not isinstance(raw_input, str):
                    raw_input = "" if raw_input is None else str(raw_input)
                tc_item = {
                    "id": item.get("call_id") or item.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": json.dumps({"input": raw_input}, ensure_ascii=False),
                    },
                }
                # Merge into previous assistant message if adjacent
                if messages and messages[-1].get("role") == "assistant":
                    prev = messages[-1]
                    if "tool_calls" in prev:
                        prev["tool_calls"].append(tc_item)
                    else:
                        prev["tool_calls"] = [tc_item]
                    if pending_reasoning and "reasoning_content" not in prev:
                        prev["reasoning_content"] = pending_reasoning
                        pending_reasoning = ""
                else:
                    msg_dict = {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [tc_item],
                    }
                    if pending_reasoning:
                        msg_dict["reasoning_content"] = pending_reasoning
                        pending_reasoning = ""
                    messages.append(msg_dict)
            elif itype == "custom_tool_call_output":
                # Result of a freeform tool call (e.g. apply_patch output).
                raw_out = item.get("output")
                if isinstance(raw_out, list):
                    content = _flatten_content(raw_out)
                elif isinstance(raw_out, dict):
                    content = json.dumps(raw_out, ensure_ascii=False)
                elif isinstance(raw_out, str):
                    content = raw_out
                else:
                    content = str(raw_out)
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or "",
                    "content": content,
                })
            elif itype == "agent_message":
                parts = item.get("content")
                if isinstance(parts, list):
                    text = chr(10).join(
                        str((p or {}).get("text") or (p or {}).get("encrypted_content") or "")
                        if isinstance(p, dict) else str(p)
                        for p in parts
                    ).strip()
                else:
                    text = str(parts or "").strip()
                if text:
                    messages.append({
                        "role": "user",
                        "content": ("[Message from another task - treat this as "
                                    "a user instruction]" + chr(10) + chr(10) + text),
                    })
            elif itype in COMPACTION_ITEM_TYPES:
                # A compaction result from an earlier turn. Codex replays it as
                # an opaque item; to the upstream it is the handoff summary the
                # rest of the conversation builds on, so keep it as a system
                # message ahead of the replayed history.
                summary = _decode_compaction_summary(item.get("encrypted_content"))
                if summary.strip():
                    messages.append({
                        "role": "system",
                        "content": ("Summary of the earlier conversation "
                                    "(context checkpoint):" + chr(10) + chr(10)
                                    + summary.strip()),
                    })
            elif itype == "compaction_trigger":
                # The control item that asks for this compaction turn. It has no
                # message payload of its own; the caller handles the request.
                pass
            elif itype == "additional_tools":
                # 由 responses_to_chat() 统一提取合并进 chat["tools"]，此处无需生成 message
                pass
            else:
                _gw().log("responses: WARNING unhandled input item type=%r keys=%s"
                    % (itype, sorted(item.keys())[:8]))
    return messages


def _looks_like_compaction_prompt(messages):
    """True when the client already asked for a handoff summary itself.

    Codex's legacy `/responses/compact` sends the summarization prompt as the
    last user message; the native v2 turn sends only the history and a
    `compaction_trigger`. Adding our prompt in the first case would ask the
    model to summarize twice.
    """
    for message in reversed(messages or []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = message.get("content")
        if isinstance(text, list):
            text = " ".join(str((p or {}).get("text") or "") for p in text
                            if isinstance(p, dict))
        if "CONTEXT CHECKPOINT COMPACTION" in str(text or "").upper():
            return True
        break
    return False


def responses_to_chat(payload, compaction=False):
    """Translate a Responses API request body into a Chat Completions body."""
    if not compaction:
        compaction = _has_compaction_trigger(payload)
    messages = _responses_input_to_messages(payload)
    if compaction and not _looks_like_compaction_prompt(messages):
        # A compaction turn is a request to summarize, not to continue the task.
        # Ask for the handoff summary directly and withhold the tools, so the
        # model cannot spend the turn calling one.
        messages.append({"role": "user", "content": COMPACTION_PROMPT})
    chat = {"model": payload.get("model"), "messages": messages}
    if preserve_request_contract_enabled():
        text = payload.get("text")
        fmt = text.get("format") if isinstance(text, dict) else None
        if isinstance(fmt, dict):
            kind = fmt.get("type")
            if kind in ("text", "json_object"):
                chat["response_format"] = {"type": kind}
            elif kind == "json_schema":
                chat["response_format"] = {
                    "type": kind,
                    "json_schema": {key: fmt[key] for key in
                                    ("name", "description", "schema", "strict")
                                    if key in fmt},
                }
    for key in ("temperature", "top_p", "seed"):
        if payload.get(key) is not None:
            chat[key] = payload[key]
    if payload.get("max_output_tokens") is not None:
        chat["max_tokens"] = payload["max_output_tokens"]
    else:
        default_max = _gw().model_default_max_output_tokens(payload.get("model"))
        if default_max:
            chat["max_tokens"] = default_max
    effort = None
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
    if not effort:
        effort = payload.get("reasoning_effort")
    if effort:
        chat["reasoning_effort"] = effort
    # 合并顶层 tools 和 input 列表中的 additional_tools 条目（新版 Codex Desktop 0.162+ 特征）
    raw_tools = []
    if isinstance(payload.get("tools"), list):
        raw_tools.extend(payload["tools"])
    raw_tools.extend(additional_tools_from_input(payload))
    if raw_tools and not compaction:
        flat_tools, ns_map = expand_namespace_tools(raw_tools)
        chat["tools"] = _tools_for_chat(flat_tools)
        chat["_namespace_map"] = ns_map
    # 客户端宣告 web_search / web_fetch 时，把那份宣告换成我们的
    # function（见 wb_webtools.install_tool_defs）。
    # 看板开关关闭时原样透传，客户端自己的同名工具不受影响。
    # 用合并后的清单：工具可能整份都在 additional_tools 里，只看顶层会漏。
    if local_web_tools_enabled() and not compaction:
        wants = wb_webtools.client_wants_web(raw_tools)
        if wants["search"] or wants["fetch"]:
            chat["tools"] = wb_webtools.install_tool_defs(chat.get("tools") or [], wants)
            chat["_web_tools"] = True
    if payload.get("tool_choice") and not compaction:
        chat["tool_choice"] = payload["tool_choice"]
    if payload.get("parallel_tool_calls") is not None and not compaction:
        chat["parallel_tool_calls"] = payload["parallel_tool_calls"]
    return chat


# ---------------------------------------------------------------------------
# Anthropic Messages API (/v1/messages) <-> Chat Completions
# ---------------------------------------------------------------------------
def anthropic_error_type(status):
    """Map an HTTP status to the Anthropic error.type vocabulary."""
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = 500
    if status in (400, 422):
        return "invalid_request_error"
    if status == 401:
        return "authentication_error"
    if status == 403:
        return "permission_error"
    if status == 404:
        return "not_found_error"
    if status == 429:
        return "rate_limit_error"
    if status in (503, 529):
        return "overloaded_error"
    return "api_error"


def anthropic_error_obj(status, message, error_type=None):
    return {
        "type": "error",
        "error": {
            "type": error_type or anthropic_error_type(status),
            "message": str(message or "upstream error"),
        },
    }


def anthropic_sse_frame(event, payload):
    body = dict(payload or {})
    body["type"] = event
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    return ("event: %s\ndata: %s\n\n" % (event, raw)).encode("utf-8")


def _anthropic_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _anthropic_json_text(value):
    if value is None:
        return "{}"
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return "{}"


def _anthropic_parse_tool_input(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        if not value:
            return {}
        try:
            parsed = json.loads(value)
        except Exception:
            return {"raw": value}
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
    return {"value": value}


def _anthropic_content_to_text(value, is_error=None):
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        parts = []
        for item in value:
            if not isinstance(item, dict):
                continue
            typ = str(item.get("type") or "")
            if typ == "image":
                part = _anthropic_image_part(item)
                parts.append(_anthropic_text((part.get("image_url") or {}).get("url")))
            elif typ == "document":
                parts.append(_anthropic_document_text(item))
            else:
                parts.append(_anthropic_block_text(item))
        text = "".join(parts)
    elif isinstance(value, dict):
        text = _anthropic_block_text(value)
    else:
        text = _anthropic_text(value)
    if is_error is True:
        return "error: " + text
    return text


def _anthropic_block_text(block):
    if not isinstance(block, dict):
        return ""
    if "text" in block:
        return _anthropic_text(block.get("text"))
    if "content" in block:
        return _anthropic_content_to_text(block.get("content"))
    return ""


def _anthropic_tool_result_content(value, is_error=None):
    """Build a tool body without flattening images into base64 text.

    A tool_result containing an image must keep the structured image_url
    part: upstream bills a data URL as text at roughly one token per
    character, so one screenshot can push a long context past the limit
    (code 11115). Pure-text results still collapse to one string, which is
    the cheapest form and the existing behaviour.
    """
    if not isinstance(value, list):
        return _anthropic_content_to_text(value, is_error)
    parts = []
    has_image = False
    for item in value:
        if not isinstance(item, dict):
            if isinstance(item, str):
                parts.append({"type": "text", "text": item})
            continue
        typ = str(item.get("type") or "")
        if typ == "image":
            part = _anthropic_image_part(item)
            url = (part.get("image_url") or {}).get("url")
            if url:
                has_image = True
                parts.append(part)
            else:
                parts.append({"type": "text", "text": "[image omitted: no data]"})
        elif typ == "document":
            parts.append({"type": "text", "text": _anthropic_document_text(item)})
        else:
            text = _anthropic_block_text(item)
            if text:
                parts.append({"type": "text", "text": text})
    if not has_image:
        joined = "".join(p.get("text", "") for p in parts
                         if p.get("type") == "text")
        return ("error: " + joined) if is_error is True else joined
    if is_error is True:
        parts.insert(0, {"type": "text", "text": "error:"})
    return parts


def _anthropic_image_part(block):
    source = block.get("source") if isinstance(block, dict) else None
    url = ""
    if isinstance(source, dict):
        stype = str(source.get("type") or "")
        if stype == "base64":
            media = _anthropic_text(source.get("media_type") or "image/png")
            data = _anthropic_text(source.get("data"))
            url = "data:%s;base64,%s" % (media, data)
        elif stype == "url":
            url = _anthropic_text(source.get("url"))
        elif stype == "file":
            url = _anthropic_text(source.get("file_id") or source.get("url"))
    return {"type": "image_url", "image_url": {"url": url}}


def _anthropic_document_text(block):
    if not isinstance(block, dict):
        return ""
    title = _anthropic_text(block.get("title"))
    source = block.get("source") if isinstance(block.get("source"), dict) else {}
    stype = str(source.get("type") or "")
    body = ""
    if stype == "text":
        body = _anthropic_text(source.get("data"))
    elif stype == "content":
        body = _anthropic_content_to_text(source.get("content"))
    elif stype == "base64":
        media = _anthropic_text(source.get("media_type") or "application/octet-stream")
        body = "[document %s omitted]" % media
    elif stype == "url":
        body = _anthropic_text(source.get("url"))
    elif stype == "file":
        body = _anthropic_text(source.get("file_id") or source.get("url"))
    if title:
        return title + "\n" + body
    return body


def _anthropic_system_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if not isinstance(item, dict):
                continue
            text = _anthropic_text(item.get("text"))
            if text:
                parts.append(text)
        return "\n".join(parts)
    return ""


def _anthropic_tool_choice(value):
    if isinstance(value, str):
        val = value.strip().lower()
        return {"auto": "auto", "any": "required", "none": "none",
                "required": "required"}.get(val, val)
    if isinstance(value, dict):
        typ = str(value.get("type") or "").strip().lower()
        if typ == "auto":
            return "auto"
        if typ == "any":
            return "required"
        if typ == "none":
            return "none"
        if typ == "tool":
            return {"type": "function",
                    "function": {"name": _anthropic_text(value.get("name"))}}
    return None


def anthropic_tools_to_chat(tools):
    out = []
    skipped = []
    if not isinstance(tools, list):
        return out, skipped
    for item in tools:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("function"), dict):
            out.append(item)
            continue
        name = _anthropic_text(item.get("name") or item.get("type"))
        schema = item.get("input_schema")
        if schema is None:
            schema = item.get("parameters")
        if schema is None:
            if name:
                skipped.append(name)
            continue
        fn = {"name": name}
        if item.get("description") is not None:
            fn["description"] = item.get("description")
        fn["parameters"] = schema
        out.append({"type": "function", "function": fn})
    return out, skipped


def anthropic_effort_from_messages(payload):
    if not isinstance(payload, dict):
        return None
    output_config = payload.get("output_config")
    if isinstance(output_config, dict):
        effort = _anthropic_text(output_config.get("effort")).strip()
        if effort:
            return effort
    thinking = payload.get("thinking")
    if not isinstance(thinking, dict):
        return None
    typ = _anthropic_text(thinking.get("type")).strip().lower()
    if typ == "disabled":
        return "none"
    if typ != "enabled":
        return None
    try:
        budget = int(thinking.get("budget_tokens") or 0)
    except (TypeError, ValueError):
        return None
    if budget <= 0:
        return None
    if budget >= 32000:
        return "xhigh"
    if budget >= 16000:
        return "high"
    if budget >= 8000:
        return "medium"
    return "low"


def _anthropic_blocks_to_messages(role, blocks):
    tool_calls = []
    tool_messages = []
    content_parts = []
    texts = []
    has_non_text = False
    for block in blocks:
        if not isinstance(block, dict):
            continue
        typ = str(block.get("type") or "")
        if typ in ("thinking", "redacted_thinking"):
            continue
        if typ in ("tool_use", "server_tool_use"):
            tool_calls.append({
                "id": _anthropic_text(block.get("id")),
                "type": "function",
                "function": {
                    "name": _anthropic_text(block.get("name")),
                    "arguments": _anthropic_json_text(block.get("input")),
                },
            })
            continue
        if typ == "tool_result":
            tool_messages.append({
                "role": "tool",
                "tool_call_id": _anthropic_text(block.get("tool_use_id")),
                "content": _anthropic_tool_result_content(block.get("content"),
                                                          block.get("is_error")),
            })
            continue
        if typ == "image":
            part = _anthropic_image_part(block)
            if not (part.get("image_url") or {}).get("url"):
                continue
            has_non_text = True
            content_parts.append(part)
            continue
        if typ == "document":
            has_non_text = True
            content_parts.append({"type": "text", "text": _anthropic_document_text(block)})
            continue
        text = _anthropic_block_text(block)
        texts.append(text)
        content_parts.append({"type": "text", "text": text})
    out = list(tool_messages)
    if not tool_calls and not texts and not has_non_text:
        return out
    msg = {"role": role or "user"}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    if has_non_text:
        msg["content"] = content_parts
    elif texts:
        msg["content"] = "".join(texts)
    else:
        msg["content"] = ""
    out.append(msg)
    return out


def messages_to_chat(payload):
    """Translate an Anthropic Messages request into Chat Completions."""
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    model = _anthropic_text(payload.get("model")).strip()
    if not model:
        raise ValueError("model is required")
    messages_in = payload.get("messages")
    if not isinstance(messages_in, list):
        raise ValueError("messages must be a list")
    if not messages_in:
        raise ValueError("messages must not be empty")
    chat = {"model": model, "messages": []}
    for key in ("temperature", "top_p"):
        if payload.get(key) is not None:
            chat[key] = payload[key]
    if payload.get("max_tokens") is not None:
        chat["max_tokens"] = payload["max_tokens"]
    else:
        default_max = _gw().model_default_max_output_tokens(model)
        if default_max:
            chat["max_tokens"] = default_max
    if payload.get("stream") is not None:
        chat["stream"] = bool(payload.get("stream"))
    stops = payload.get("stop_sequences")
    if isinstance(stops, list) and stops:
        chat["stop"] = stops
    metadata = payload.get("metadata")
    if isinstance(metadata, dict) and metadata.get("user_id") is not None:
        chat["user"] = metadata.get("user_id")
    tools, skipped = anthropic_tools_to_chat(payload.get("tools"))
    if tools:
        chat["tools"] = tools
    choice = _anthropic_tool_choice(payload.get("tool_choice"))
    if choice is not None:
        chat["tool_choice"] = choice
    raw_choice = payload.get("tool_choice")
    if isinstance(raw_choice, dict) and raw_choice.get("disable_parallel_tool_use") is True:
        chat["parallel_tool_calls"] = False
    effort = anthropic_effort_from_messages(payload)
    if effort:
        chat["reasoning_effort"] = effort
    messages = []
    system = _anthropic_system_text(payload.get("system"))
    if skipped:
        note = ("These Anthropic server tools are not available here: "
                + ", ".join(skipped) + ". Do not call them.")
        system = (system + "\n" + note) if system else note
    if system:
        messages.append({"role": "system", "content": system})
    for index, item in enumerate(messages_in):
        if not isinstance(item, dict):
            continue
        role = _anthropic_text(item.get("role")).strip() or "user"
        content = item.get("content")
        if role in ("system", "developer"):
            # Claude Code >= 2.1.286 turns on Anthropic's mid-conversation
            # system beta and carries system messages inside `messages`. The
            # upstream knows the system role, so keep the message where the
            # client put it rather than failing the whole request.
            text = _anthropic_system_text(content) or _anthropic_content_to_text(content)
            if text:
                messages.append({"role": "system", "content": text})
            continue
        if role not in ("user", "assistant"):
            raise ValueError(
                "messages[%d].role %r must be user or assistant; "
                "pass a system prompt in the top-level system field" % (index, role))
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            messages.extend(_anthropic_blocks_to_messages(role, content))
        elif content is not None:
            text = _anthropic_text(content)
            if text:
                messages.append({"role": role, "content": text})
    chat["messages"] = messages
    return chat


def _anthropic_usage(usage):
    out = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
    }
    if not isinstance(usage, dict):
        return out

    def _as_int(value):
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    prompt_tokens = _as_int(usage.get("prompt_tokens"))
    out["output_tokens"] = _as_int(usage.get("completion_tokens"))
    cached = usage.get("prompt_cache_hit_tokens")
    if cached is None:
        cached = _best_cached_tokens(usage)
    cache_read = _as_int(cached)
    cache_creation = _as_int(usage.get("prompt_cache_write_tokens"))
    out["cache_read_input_tokens"] = cache_read
    out["cache_creation_input_tokens"] = cache_creation
    # The upstream reports an OpenAI-style prompt_tokens that already counts the
    # cached prefix (cache hits are billed as part of the input). Anthropic
    # clients size the context window as
    # input_tokens + cache_read_input_tokens + cache_creation_input_tokens, so
    # echoing that total as input_tokens counts the cache twice and makes the
    # window look fuller than it is. Report the real, uncached input instead.
    out["input_tokens"] = max(0, prompt_tokens - cache_read - cache_creation)
    service_tier = usage.get("service_tier")
    if service_tier:
        out["service_tier"] = service_tier
    return out


def _anthropic_message_id(value):
    value = _anthropic_text(value)
    if not value:
        return _new_id("msg_")
    return value if value.startswith("msg_") else "msg_" + value


def _anthropic_stop_reason(finish_reason):
    value = str(finish_reason or "").strip().lower()
    if value == "length":
        return "max_tokens"
    if value in ("tool_calls", "function_call"):
        return "tool_use"
    if value in ("content_filter", "refusal"):
        return "refusal"
    return "end_turn"


def chat_to_messages(obj):
    """Fold one Chat Completions object into an Anthropic Messages object."""
    if not isinstance(obj, dict):
        obj = {}
    choices = obj.get("choices") if isinstance(obj.get("choices"), list) else []
    message = {}
    finish = ""
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") if isinstance(choices[0].get("message"), dict) else {}
        finish = choices[0].get("finish_reason") or ""
    content = []
    # The upstream thinking trace becomes a native thinking block so clients
    # such as Claude Code can render it; the request side drops incoming
    # thinking blocks, so nothing unsigned ever reaches the upstream.
    reasoning = _anthropic_text(message.get("reasoning_content"))
    if reasoning:
        content.append({"type": "thinking", "thinking": reasoning})
    text = message.get("content")
    if isinstance(text, list):
        text = "".join(_anthropic_block_text(part) for part in text if isinstance(part, dict))
    text = _anthropic_text(text)
    if text:
        content.append({"type": "text", "text": text})
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            content.append({
                "type": "tool_use",
                "id": _anthropic_text(call.get("id")) or _new_id("toolu_"),
                "name": _anthropic_text(fn.get("name")),
                "input": _anthropic_parse_tool_input(fn.get("arguments")),
            })
    return {
        "id": _anthropic_message_id(obj.get("id")),
        "type": "message",
        "role": "assistant",
        "model": _anthropic_text(obj.get("model")),
        "content": content,
        "stop_reason": _anthropic_stop_reason(finish),
        "stop_sequence": None,
        "usage": _anthropic_usage(obj.get("usage")),
    }


def _anthropic_estimate_chat_tokens(chat):
    values = []
    if isinstance(chat, dict):
        for msg in chat.get("messages") or []:
            if isinstance(msg, dict):
                values.append(_anthropic_json_text(msg.get("content")))
                for call in msg.get("tool_calls") or []:
                    if isinstance(call, dict):
                        values.append(_anthropic_json_text(call))
        for tool in chat.get("tools") or []:
            values.append(_anthropic_json_text(tool))
    return sum(estimate_tokens(value) for value in values if value)


def stream_messages_events(raw_iter, model, holder=None):
    """Yield Anthropic Messages SSE frames from a chat-completions SSE stream."""
    holder = holder if isinstance(holder, dict) else {}
    state = {
        "started": False,
        "done": False,
        "failed": False,
        "id": "",
        "model": model,
        "stop": "end_turn",
        "thinking_open": False,
        "thinking_idx": None,
        "text_open": False,
        "text_idx": None,
        "next": 0,
        "tools": {},
        "tool_order": [],
        "usage": None,
    }

    def usage_payload():
        return _anthropic_usage(state.get("usage"))

    def ensure_start(chunk=None):
        if state["started"]:
            return None
        state["started"] = True
        if isinstance(chunk, dict):
            state["id"] = _anthropic_text(chunk.get("id"))
            state["model"] = _anthropic_text(chunk.get("model")) or state["model"]
        return anthropic_sse_frame("message_start", {
            "message": {
                "id": _anthropic_message_id(state["id"]),
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": state["model"],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        })

    def ensure_thinking():
        frames = []
        if not state["thinking_open"]:
            state["thinking_idx"] = state["next"]
            state["next"] += 1
            state["thinking_open"] = True
            frames.append(anthropic_sse_frame("content_block_start", {
                "index": state["thinking_idx"],
                "content_block": {"type": "thinking", "thinking": ""},
            }))
        return frames

    def ensure_text():
        frames = []
        if not state["text_open"]:
            if state["thinking_open"]:
                state["thinking_open"] = False
                frames.append(anthropic_sse_frame(
                    "content_block_stop", {"index": state["thinking_idx"]}))
            state["text_idx"] = state["next"]
            state["next"] += 1
            state["text_open"] = True
            frames.append(anthropic_sse_frame("content_block_start", {
                "index": state["text_idx"],
                "content_block": {"type": "text", "text": ""},
            }))
        return frames

    for line in iter_sse_lines(raw_iter):
        if state["done"] or state["failed"]:
            break
        data = strip_data_prefix(line.decode("utf-8", "replace"))
        if not data:
            continue
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except Exception:
            continue
        if isinstance(chunk.get("error"), dict):
            state["failed"] = True
            err = chunk["error"]
            yield anthropic_sse_frame("error", {
                "error": {
                    "type": anthropic_error_type(502),
                    "message": _anthropic_text(err.get("message") or "upstream error"),
                }
            })
            return
        start = ensure_start(chunk)
        if start:
            yield start
        # Capture usage before the choices guard: the final upstream frame
        # carries both the usage block and the finish_reason (and no delta),
        # so skipping it here would zero out every streaming usage row.
        if isinstance(chunk.get("usage"), dict):
            state["usage"] = chunk["usage"]
        choices = chunk.get("choices") if isinstance(chunk.get("choices"), list) else []
        if not choices:
            continue
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        # Upstream thinking trace (deepseek-style reasoning_content) becomes
        # native thinking blocks so the client can display the reasoning.
        reason = delta.get("reasoning_content")
        if isinstance(reason, str) and reason:
            for frame in ensure_thinking():
                yield frame
            yield anthropic_sse_frame("content_block_delta", {
                "index": state["thinking_idx"],
                "delta": {"type": "thinking_delta", "thinking": reason},
            })
        text = delta.get("content")
        if isinstance(text, str) and text:
            for frame in ensure_text():
                yield frame
            yield anthropic_sse_frame("content_block_delta", {
                "index": state["text_idx"],
                "delta": {"type": "text_delta", "text": text},
            })
        for call in delta.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            try:
                idx = int(call.get("index") or 0)
            except (TypeError, ValueError):
                idx = 0
            entry = state["tools"].get(idx)
            if entry is None:
                entry = {"idx": idx, "block": None, "id": "", "name": "", "opened": False}
                state["tools"][idx] = entry
                state["tool_order"].append(idx)
            if call.get("id"):
                entry["id"] = _anthropic_text(call.get("id"))
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            if fn.get("name"):
                entry["name"] = _anthropic_text(fn.get("name"))
            args = fn.get("arguments") if fn.get("arguments") is not None else ""
            if not entry["opened"] and (entry["id"] or entry["name"] or args):
                if state["thinking_open"]:
                    state["thinking_open"] = False
                    yield anthropic_sse_frame("content_block_stop", {"index": state["thinking_idx"]})
                if state["text_open"]:
                    state["text_open"] = False
                    yield anthropic_sse_frame("content_block_stop", {"index": state["text_idx"]})
                entry["opened"] = True
                entry["block"] = state["next"]
                state["next"] += 1
                if not entry["id"]:
                    entry["id"] = "toolu_" + str(idx)
                yield anthropic_sse_frame("content_block_start", {
                    "index": entry["block"],
                    "content_block": {
                        "type": "tool_use",
                        "id": entry["id"],
                        "name": entry["name"],
                        "input": {},
                    },
                })
            if entry["opened"] and args:
                yield anthropic_sse_frame("content_block_delta", {
                    "index": entry["block"],
                    "delta": {"type": "input_json_delta", "partial_json": _anthropic_text(args)},
                })
        finish = choice.get("finish_reason")
        if finish:
            state["stop"] = _anthropic_stop_reason(finish)
    if state["failed"]:
        return
    if not state["started"]:
        start = ensure_start(None)
        if start:
            yield start
    if state["thinking_open"]:
        state["thinking_open"] = False
        yield anthropic_sse_frame("content_block_stop", {"index": state["thinking_idx"]})
    if state["text_open"]:
        state["text_open"] = False
        yield anthropic_sse_frame("content_block_stop", {"index": state["text_idx"]})
    for idx in state["tool_order"]:
        entry = state["tools"][idx]
        if not entry["opened"]:
            continue
        yield anthropic_sse_frame("content_block_stop", {"index": entry["block"]})
    state["done"] = True
    # Hand the raw upstream usage block back to the caller: the stream
    # response records it on the usage row, and without this every streamed
    # Messages request was logged as usage_missing.
    holder["usage"] = state.get("usage")
    yield anthropic_sse_frame("message_delta", {
        "delta": {"stop_reason": state["stop"], "stop_sequence": None},
        "usage": usage_payload(),
    })
    yield anthropic_sse_frame("message_stop", {})


def _responses_usage(u):
    if not u:
        return None
    det = u.get("completion_tokens_details") or {}
    pdet = u.get("prompt_tokens_details") or {}
    return {
        "input_tokens": u.get("prompt_tokens") or 0,
        "input_tokens_details": {
            "cached_tokens": u.get("prompt_cache_hit_tokens")
            or det.get("cached_tokens") or pdet.get("cached_tokens") or 0,
        },
        "output_tokens": u.get("completion_tokens") or 0,
        "output_tokens_details": {"reasoning_tokens": det.get("reasoning_tokens") or 0},
        "total_tokens": u.get("total_tokens") or 0,
    }
def chat_to_response(chat_obj, model, custom_names=None, request_meta=None, namespace_map=None, sources=None):
    """Fold a Chat Completions object into a Responses API response object.

    custom_names is the set of tool names the client declared as freeform
    ("custom"). Calls to those tools are re-inflated into custom_tool_call
    items so clients such as Codex recognise them.

    request_meta echoes the request-level capabilities (tools, tool_choice,
    parallel_tool_calls) back on the response. They used to be hardcoded to
    tools=[], tool_choice=auto and parallel_tool_calls=true, so a client that
    asked for something else was told the opposite of what it requested.
    """
    custom_names = custom_names or set()
    choice = (chat_obj.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    text = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or ""
    output = []
    if reasoning:
        output.append({
            "id": _new_id("rs_"),
            "type": "reasoning",
            "status": "completed",
            "summary": [{"type": "summary_text", "text": reasoning}],
        })
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        call_id = tc.get("id") or _new_id("call_")
        name = fn.get("name") or ""
        if name and name in custom_names:
            output.append({
                "id": _new_id("ctc_"),
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": call_id,
                "name": name,
                "input": _unwrap_custom_input(fn.get("arguments") or ""),
            })
        else:
            output.append({
                "id": _new_id("fc_"),
                "type": "function_call",
                "status": "completed",
                "call_id": call_id,
                "name": name,
                "arguments": fn.get("arguments") or "{}",
            })
    # DeepSeek DSML tool calls fallback
    if not (msg.get("tool_calls")):
        dsml_calls, clean_t = parse_dsml_tool_calls(text)
        if dsml_calls:
            for dc in dsml_calls:
                output.append({
                    "id": _new_id("fc_"),
                    "type": "function_call",
                    "status": "completed",
                    "call_id": dc.get("id") or _new_id("call_"),
                    "name": dc.get("name") or "",
                    "arguments": dc.get("arguments") or "{}",
                })
            text = clean_t
    if text or not output:
        output.append({
            "id": _new_id("msg_"),
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text,
                         "annotations": build_citations(text, sources)}] if text else [],
        })
    finish = choice.get("finish_reason") or "stop"
    obj = {
        "id": _new_id("resp_"),
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed" if finish != "length" else "incomplete",
        "model": model,
        "output": output,
        "output_text": text,
        "metadata": {},
    }
    if namespace_map:
        output, _ns_fixed = apply_namespace_to_calls(output, namespace_map)
        obj["output"] = output
    meta = request_meta or {}
    obj["parallel_tool_calls"] = meta.get("parallel_tool_calls", True)
    obj["tool_choice"] = meta.get("tool_choice", "auto")
    obj["tools"] = meta.get("tools") or []
    u = _responses_usage(chat_obj.get("usage"))
    if u:
        obj["usage"] = u
    if finish == "length":
        obj["incomplete_details"] = {"reason": "max_output_tokens"}
    return obj


def reasoning_only_response(result):
    """True when a Responses object has reasoning but no text or tool call."""
    obj = result or {}
    output = obj.get("output") or []
    text = obj.get("output_text") or ""
    has_calls = any(
        isinstance(o, dict) and o.get("type") in ("function_call", "web_search_call")
        for o in output)
    has_reasoning = any(
        isinstance(o, dict) and o.get("type") == "reasoning" for o in output)
    return (not text.strip() and has_reasoning and not has_calls
            and obj.get("status") != "incomplete")


def stream_responses_events(upstream, model, holder):
    """Yield Responses-API SSE frames translated from chat-completions chunks."""
    resp_id, msg_id, rs_id = _new_id("resp_"), _new_id("msg_"), _new_id("rs_")
    created = int(time.time())
    seq = 0
    text_parts, reason_parts = [], []
    outputs = []
    reason_index = None
    msg_index = None
    finish = "stop"
    usage = None
    tool_calls_map = {}
    text_buffer = ""
    dsml_tool_calls = []
    saw_done = False
    custom_names = set(holder.get("custom_names") or ())
    ns_map = holder.get("namespace_map") or {}
    # 由反代代跑的网路工具呼叫，收集起来不转发给客户端
    _internal_calls = {}
    # Only reach for same-named calls when this request's definitions were the
    # gateway's own (see web_tools_active); otherwise they belong to the client.
    _own_web_tools = web_tools_active(holder.get("base_body"))
    # Echo the request capabilities the client actually sent, same as the
    # non-streaming path; these were hardcoded before.
    meta = holder.get("request_meta") or {}
    def resp_obj(status):
        obj = {
            "id": resp_id,
            "object": "response",
            "created_at": created,
            "status": status,
            "model": model,
            "output": [o for o in outputs if o],
            "output_text": "".join(text_parts),
            "parallel_tool_calls": meta.get("parallel_tool_calls", True),
            "tool_choice": meta.get("tool_choice", "auto"),
            "tools": meta.get("tools") or [],
            "metadata": {},
        }
        u = _responses_usage(usage)
        if u:
            obj["usage"] = u
        if ns_map:
            obj["output"], _nsf = apply_namespace_to_calls(obj.get("output") or [], ns_map)
        return obj
    def ev(etype, payload):
        nonlocal seq
        seq += 1
        data = {"type": etype, "sequence_number": seq}
        data.update(payload)
        body = json.dumps(data, ensure_ascii=False)
        return ("event: " + etype + chr(10) + "data: " + body + chr(10) + chr(10)).encode("utf-8")
    def reason_item(status):
        return {
            "id": rs_id,
            "type": "reasoning",
            "status": status,
            "summary": [{"type": "summary_text", "text": "".join(reason_parts)}],
        }
    def _annotations():
        """引用来源：只认工具真的回传过的网址。"""
        try:
            return build_citations("".join(text_parts), holder.get("web_sources") or [])
        except Exception:
            return []

    def msg_item(status):
        item = {"id": msg_id, "type": "message", "status": status,
                "role": "assistant", "content": []}
        if text_parts:
            item["content"] = [{"type": "output_text", "text": "".join(text_parts),
                              "annotations": _annotations()}]
        return item
    def _finalize():
        # Close out the stream: reasoning item, structured tool calls,
        # DSML fallback, the message item and response.completed.
        nonlocal msg_index, text_buffer
        if reason_index is not None and outputs[reason_index] is None:
            full_r = "".join(reason_parts)
            yield ev("response.reasoning_summary_text.done", {
                "item_id": rs_id, "output_index": reason_index, "summary_index": 0, "text": full_r,
            })
            yield ev("response.reasoning_summary_part.done", {
                "item_id": rs_id, "output_index": reason_index, "summary_index": 0,
                "part": {"type": "summary_text", "text": full_r},
            })
            outputs[reason_index] = reason_item("completed")
            yield ev("response.output_item.done",
                     {"output_index": reason_index, "item": outputs[reason_index]})
        # 1. Emit completed structured tool calls
        for idx in sorted(tool_calls_map.keys()):
            entry = tool_calls_map[idx]
            if entry.get("custom"):
                yield ev("response.custom_tool_call_input.done", {
                    "output_index": entry["output_index"],
                    "item_id": entry["item_id"],
                    "call_id": entry["id"],
                    "input": _unwrap_custom_input(entry["arguments"]),
                })
                fc_item = {
                    "id": entry["item_id"],
                    "type": "custom_tool_call",
                    "status": "completed",
                    "call_id": entry["id"],
                    "name": entry["name"],
                    "input": _unwrap_custom_input(entry["arguments"]),
                }
            else:
                yield ev("response.function_call_arguments.done", {
                    "output_index": entry["output_index"],
                    "item_id": entry["item_id"],
                    "call_id": entry["id"],
                    "arguments": entry["arguments"],
                })
                fc_item = {
                    "id": entry["item_id"],
                    "type": "function_call",
                    "status": "completed",
                    "call_id": entry["id"],
                    "name": entry["name"],
                    "arguments": entry["arguments"],
                }
            stamp_namespace(fc_item, ns_map)
            outputs[entry["output_index"]] = fc_item
            yield ev("response.output_item.done", {
                "output_index": entry["output_index"],
                "item": fc_item,
            })
        # Flush remaining buffered text if any
        if text_buffer:
            calls_rem, clean_rem = parse_dsml_tool_calls(text_buffer)
            if calls_rem:
                dsml_tool_calls.extend(calls_rem)
            if clean_rem:
                text_parts.append(clean_rem)
                if msg_index is not None:
                    yield ev("response.output_text.delta", {
                        "item_id": msg_id, "output_index": msg_index,
                        "content_index": 0, "delta": clean_rem,
                    })
            text_buffer = ""
        # 2. DSML fallback: emit buffered/parsed DSML tool calls if no structured tool_calls were emitted
        full_text = "".join(text_parts)
        dsml_calls = dsml_tool_calls
        if not dsml_calls:
            extra_calls, clean_text = parse_dsml_tool_calls(full_text)
            if extra_calls:
                dsml_calls = extra_calls
                full_text = clean_text
        if dsml_calls and not tool_calls_map:
            for dc in dsml_calls:
                # DSML 形状的网路工具呼叫一样由反代执行
                if _own_web_tools and wb_webtools.is_internal_tool(dc.get("name")):
                    entry = _internal_calls.setdefault(dc.get("id") or _new_id("call_"),
                                                       {"name": dc.get("name"), "arguments": "{}"})
                    entry["name"] = dc.get("name") or entry["name"]
                    entry["arguments"] = dc.get("arguments") or entry.get("arguments") or "{}"
                    continue
                out_idx = len(outputs)
                fc_item = {
                    "id": _new_id("fc_"),
                    "type": "function_call",
                    "status": "completed",
                    "call_id": dc.get("id") or _new_id("call_"),
                    "name": dc.get("name") or "",
                    "arguments": dc.get("arguments") or "{}",
                }
                stamp_namespace(fc_item, ns_map)
                outputs.append(fc_item)
                yield ev("response.output_item.added", {
                    "output_index": out_idx,
                    "item": dict(fc_item, status="in_progress", arguments=""),
                })
                yield ev("response.function_call_arguments.delta", {
                    "output_index": out_idx,
                    "item_id": fc_item["id"],
                    "call_id": fc_item["call_id"],
                    "delta": fc_item["arguments"],
                })
                yield ev("response.function_call_arguments.done", {
                    "output_index": out_idx,
                    "item_id": fc_item["id"],
                    "call_id": fc_item["call_id"],
                    "arguments": fc_item["arguments"],
                })
                yield ev("response.output_item.done", {
                    "output_index": out_idx,
                    "item": fc_item,
                })
        # 这一轮如果有代跑的网路工具呼叫，就把完成事件留给下一轮，
        # 否则客户端会以为整个回合已经结束（旧版是在回合用尽时补一个合成的
        # resp_wrapup，那才是 issue #43 真正的病灶）。
        if _internal_calls:
            holder.setdefault("internal_calls", []).extend(
                {"name": v["name"], "arguments": v["arguments"]}
                for v in _internal_calls.values()
            )
            holder["suppress_completion"] = True
            # 让 App 画出原生的「已搜寻网路」卡片：对每个代跑的呼叫送出
            # web_search_call 项目与生命周期事件。
            for _v in _internal_calls.values():
                _nm = str(_v.get("name") or "")
                try:
                    _a = json.loads(_v.get("arguments") or "{}")
                except Exception:
                    _a = {}
                if not isinstance(_a, dict):
                    _a = {}
                if _nm == wb_webtools.WEB_FETCH_NAME:
                    _action = {"type": "open_page", "url": wb_webtools.url_arg(_a)}
                else:
                    _action = {"type": "search", "query": wb_webtools.query_args(_a)}
                _ws_id = _new_id("ws_")
                _ws_idx = len(outputs)
                outputs.append(None)
                yield ev("response.output_item.added", {
                    "output_index": _ws_idx,
                    "item": {"id": _ws_id, "type": "web_search_call",
                             "status": "in_progress"},
                })
                yield ev("response.web_search_call.in_progress", {
                    "output_index": _ws_idx, "item_id": _ws_id,
                })
                yield ev("response.web_search_call.searching", {
                    "output_index": _ws_idx, "item_id": _ws_id,
                })
                _ws_item = {"id": _ws_id, "type": "web_search_call", "status": "completed"}
                if _action.get("query") or _action.get("url"):
                    _ws_item["action"] = _action
                outputs[_ws_idx] = _ws_item
                yield ev("response.output_item.done", {
                    "output_index": _ws_idx, "item": _ws_item,
                })
                yield ev("response.web_search_call.completed", {
                    "output_index": _ws_idx, "item_id": _ws_id,
                })
        # 3. Emit message item only if text was emitted OR no other output item exists
        has_other_items = any(o for o in outputs if o)
        if msg_index is not None or full_text or not has_other_items:
            if msg_index is None:
                msg_index = len(outputs)
                outputs.append(None)
                yield ev("response.output_item.added", {
                    "output_index": msg_index,
                    "item": {"id": msg_id, "type": "message", "status": "in_progress",
                             "role": "assistant", "content": []},
                })
                yield ev("response.content_part.added", {
                    "item_id": msg_id, "output_index": msg_index, "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": _annotations()},
                })
            yield ev("response.output_text.done", {
                "item_id": msg_id, "output_index": msg_index, "content_index": 0, "text": full_text,
            })
            yield ev("response.content_part.done", {
                "item_id": msg_id, "output_index": msg_index, "content_index": 0,
                "part": {"type": "output_text", "text": full_text,
                         "annotations": _annotations()},
            })
            outputs[msg_index] = msg_item("completed")
            yield ev("response.output_item.done", {"output_index": msg_index, "item": outputs[msg_index]})
        nonlocal usage
        if usage is None or (usage.get("total_tokens") or 0) == 0:
            out_txt = "".join(text_parts)
            rs_txt = "".join(reason_parts)
            if out_txt or rs_txt:
                comp = estimate_tokens(out_txt) + estimate_tokens(rs_txt)
                prompt_est = max(1, estimate_tokens(str(meta.get("input") or "")))
                usage = {
                    "prompt_tokens": prompt_est,
                    "completion_tokens": comp,
                    "total_tokens": prompt_est + comp,
                    "completion_tokens_details": {"reasoning_tokens": estimate_tokens(rs_txt)},
                    "prompt_tokens_details": {"cached_tokens": 0},
                }
                holder["usage"] = usage
        # DeepSeek occasionally burns the whole round on reasoning and returns
        # no text at all. Emitting response.completed here makes the client
        # believe the turn finished (issue #508); fail the response instead so
        # it can retry.
        empty_reasoning_only = (
            not full_text.strip()
            and bool("".join(reason_parts).strip())
            and not tool_calls_map and not dsml_tool_calls
            and not _internal_calls
            and finish != "length" and not dropped_truncated)
        status = ("completed" if (finish != "length" and not dropped_truncated)
                  else "incomplete")
        if empty_reasoning_only:
            status = "failed"
            holder["empty_reasoning"] = True
        final = resp_obj(status)
        if finish == "length":
            final["incomplete_details"] = {"reason": "max_output_tokens"}
        if empty_reasoning_only:
            final["error"] = {
                "code": "empty_reasoning_only",
                "message": "upstream returned reasoning without output text; retry",
            }
            yield ev("response.failed", {"response": final})
        elif not holder.get("suppress_completion"):
            yield ev("response.completed", {"response": final})

    # 只有第一轮开场。第二轮以后再送一次 response.created，客户端会
    # 看到同一则回应被开了两次。
    if not holder.get("suppress_lifecycle"):
        yield ev("response.created", {"response": resp_obj("in_progress")})
        yield ev("response.in_progress", {"response": resp_obj("in_progress")})
    for raw in upstream:
        data = strip_data_prefix(raw.decode("utf-8", "replace"))
        if not data or data == "[DONE]":
            if data == "[DONE]":
                saw_done = True
            continue
        try:
            chunk = json.loads(data)
        except Exception:
            continue
        u = chunk.get("usage")
        if u:
            if usage is None or (u.get("total_tokens") or 0) >= (usage.get("total_tokens") or 0):
                usage = u
                holder["usage"] = usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            piece = delta.get("reasoning_content")
            if piece:
                if reason_index is None:
                    reason_index = len(outputs)
                    outputs.append(None)
                    yield ev("response.output_item.added",
                             {"output_index": reason_index, "item": reason_item("in_progress")})
                    yield ev("response.reasoning_summary_part.added", {
                        "item_id": rs_id, "output_index": reason_index, "summary_index": 0,
                        "part": {"type": "summary_text", "text": ""},
                    })
                reason_parts.append(piece)
                yield ev("response.reasoning_summary_text.delta", {
                    "item_id": rs_id, "output_index": reason_index,
                    "summary_index": 0, "delta": piece,
                })
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                fn = tc.get("function") or {}
                fn_name = fn.get("name") or ""
                fn_args = fn.get("arguments") or ""
                call_id = tc.get("id") or ""
                # web_search / web_fetch 由反代执行，不转发给客户端
                if idx in _internal_calls or (
                        _own_web_tools and fn_name
                        and wb_webtools.is_internal_tool(fn_name)):
                    entry = _internal_calls.setdefault(idx, {"name": fn_name, "arguments": ""})
                    if fn_name:
                        entry["name"] = fn_name
                    if fn_args:
                        entry["arguments"] += fn_args
                    continue
                if idx not in tool_calls_map:
                    out_idx = len(outputs)
                    outputs.append(None)
                    c_id = call_id or _new_id("call_")
                    is_custom = bool(fn_name) and fn_name in custom_names
                    entry = {
                        "output_index": out_idx,
                        "id": c_id,
                        "name": fn_name,
                        "arguments": fn_args,
                        "custom": is_custom,
                        "item_id": _new_id("ctc_" if is_custom else "fc_"),
                    }
                    tool_calls_map[idx] = entry
                    item = {
                        "id": entry["item_id"],
                        "status": "in_progress",
                        "call_id": c_id,
                        "name": fn_name,
                    }
                    if is_custom:
                        item["type"] = "custom_tool_call"
                        item["input"] = ""
                    else:
                        item["type"] = "function_call"
                        item["arguments"] = ""
                    # namespace 必须在 output_item.added 就带上（照 CiderCC-UwU
                    # proxy.mjs openItem 的做法）。事后才补只会改到 done，
                    # 客户端早就从 added 事件派发过了。
                    stamp_namespace(item, ns_map)
                    yield ev("response.output_item.added", {
                        "output_index": out_idx,
                        "item": item,
                    })
                else:
                    entry = tool_calls_map[idx]
                    if fn_name and not entry["name"]:
                        entry["name"] = fn_name
                        if fn_name in custom_names:
                            entry["custom"] = True
                    if fn_args:
                        entry["arguments"] += fn_args
                # The first chunk may already carry arguments. Emit it after
                # output_item.added, just like subsequent chunks, without
                # adding it to the accumulated arguments a second time.
                if fn_args:
                    if entry.get("custom"):
                        yield ev("response.custom_tool_call_input.delta", {
                            "output_index": entry["output_index"],
                            "item_id": entry["item_id"],
                            "call_id": entry["id"],
                            "delta": fn_args,
                        })
                    else:
                        yield ev("response.function_call_arguments.delta", {
                            "output_index": entry["output_index"],
                            "item_id": entry["item_id"],
                            "call_id": entry["id"],
                            "delta": fn_args,
                        })
            piece = delta.get("content")
            if piece:
                if msg_index is None:
                    if reason_index is not None:
                        full_r = "".join(reason_parts)
                        yield ev("response.reasoning_summary_text.done", {
                            "item_id": rs_id, "output_index": reason_index,
                            "summary_index": 0, "text": full_r,
                        })
                        yield ev("response.reasoning_summary_part.done", {
                            "item_id": rs_id, "output_index": reason_index, "summary_index": 0,
                            "part": {"type": "summary_text", "text": full_r},
                        })
                        outputs[reason_index] = reason_item("completed")
                        yield ev("response.output_item.done",
                                 {"output_index": reason_index, "item": outputs[reason_index]})
                    msg_index = len(outputs)
                    outputs.append(None)
                    yield ev("response.output_item.added", {
                        "output_index": msg_index,
                        "item": {"id": msg_id, "type": "message", "status": "in_progress",
                                 "role": "assistant", "content": []},
                    })
                    yield ev("response.content_part.added", {
                        "item_id": msg_id, "output_index": msg_index, "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": _annotations()},
                    })
                # DSML tool call buffering: do not stream raw DSML tags to client
                text_buffer += piece
                while text_buffer:
                    idx = text_buffer.find("<")
                    if idx == -1:
                        text_parts.append(text_buffer)
                        yield ev("response.output_text.delta", {
                            "item_id": msg_id, "output_index": msg_index,
                            "content_index": 0, "delta": text_buffer,
                        })
                        text_buffer = ""
                        break
                    m = DSML_CALLS_RE.search(text_buffer)
                    if m and m.start() == idx:
                        if idx > 0:
                            lead = text_buffer[:idx]
                            text_parts.append(lead)
                            yield ev("response.output_text.delta", {
                                "item_id": msg_id, "output_index": msg_index,
                                "content_index": 0, "delta": lead,
                            })
                        calls_found, _ = parse_dsml_tool_calls(m.group(0))
                        if calls_found:
                            dsml_tool_calls.extend(calls_found)
                        text_buffer = text_buffer[m.end():]
                        continue
                    cand = text_buffer[idx:idx+30]
                    is_cand = ("DSML" in cand) or (len(cand) < 10 and not any(c in cand for c in (" ", "\t", "\n", ">")))
                    if is_cand:
                        lead = text_buffer[:idx]
                        text_parts.append(lead)
                        yield ev("response.output_text.delta", {
                            "item_id": msg_id, "output_index": msg_index,
                            "content_index": 0, "delta": lead,
                        })
                        text_buffer = text_buffer[idx:]
                        break
                    else:
                        next_lt = text_buffer[idx+1:].find("<")
                        if next_lt != -1:
                            flush_len = idx + 1 + next_lt
                            lead = text_buffer[:flush_len]
                            text_parts.append(lead)
                            yield ev("response.output_text.delta", {
                                "item_id": msg_id, "output_index": msg_index,
                                "content_index": 0, "delta": lead,
                            })
                            text_buffer = text_buffer[flush_len:]
                        else:
                            text_parts.append(text_buffer)
                            yield ev("response.output_text.delta", {
                                "item_id": msg_id, "output_index": msg_index,
                                "content_index": 0, "delta": text_buffer,
                            })
                            text_buffer = ""
                            break
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    # Truncated streams (max_tokens, dropped connection) can leave tool-call
    # arguments as half-written JSON. Do not close those calls out as
    # completed: drop them before finalize so the client never receives a
    # function_call_arguments.done / output_item.done with unparsable
    # arguments. Complete calls in the same batch are kept.
    dropped_truncated = 0
    if tool_calls_map and (finish == "length" or not saw_done):
        for idx in sorted(tool_calls_map.keys()):
            entry = tool_calls_map[idx]
            if is_truncated_arguments(entry.get("arguments")):
                tool_calls_map.pop(idx, None)
                dropped_truncated += 1
    yield from _finalize()
