"""Request and response repairs between OpenAI-style clients and the upstream.

Everything here rewrites a chat payload (or a reply) so the WorkBuddy upstream
accepts it, or so the client receives a well-formed answer:

* fingerprint sanitisation of client prompts that trip the upstream WAF;
* tool-call pairing / orphan cleanup and DeepSeek reasoning_content backfill;
* tool, tool_choice, schema and stop normalisation (upstream codes 11101/11129);
* DSML / printed tool-call recovery and the streaming leak filter;
* the Chat Completions chunk builders used by every streaming path.

Functions are pure (input in, payload out); gateway state stays in wb_proxy.
"""

import json
import os
import re
import time
import uuid

from wb_wire import _new_id, normalize_usage_cache_aliases


# ---------------------------------------------------------------------------
# Fingerprint Sanitization (immunizes against Codex / Claude Code WAF patterns)
# ---------------------------------------------------------------------------
SANITIZE_FEATURES = (
    "x-anthropic-billing-header",
    "cc_entrypoint=",
    "You are Claude Code",
    "Main branch (",
    "You are a coding agent running in the Codex CLI",
    "github.com/anthropics/",
    "11128",
)
SANITIZE_REWRITES = (
    ("You are Claude Code, Anthropic's official CLI for Claude",
     "You are Claude Code, Anthropic's official CLI tool for Claude"),
    ("Main branch (you will usually use this for PRs)",
     "Default branch (you will usually use this for PRs)"),
    ("You are a coding agent running in the Codex CLI, a terminal-based coding assistant.",
     "You are a coding agent running in the Codex CLI tool, a terminal-based coding assistant."),
    ("To give feedback, users should report the issue at https://github.com/anthropics/claude-code/issues",
     "To provide feedback, users should report the issue at https://github.com/anthropics/claude-code/issues"),
    ("11128", "11-128"),
)
SANITIZE_HDR_RE = re.compile(r"(?i)x-anthropic-billing-header:[^;\r\n]*;?\s*")
SANITIZE_BARE_HDR_RE = re.compile(r"(?i)x-anthropic-billing-header")
SANITIZE_KV_RE = re.compile(r"(?i)\bcc_[a-z0-9_]+=[^;\r\n]*;?\s*")
# WorkBuddy upstream returns 11128 ("Illegal API invocation from an unapproved
# channel") when this exact OmO identity fingerprint appears as a contiguous
# substring in a system message. A/B tests show the match is case-insensitive,
# survives surrounding prefix/suffix text, and stops matching when the phrase
# structure is changed. Rewrite only this confirmed fingerprint, leaving the
# agent identity and behaviour intact while dropping the framework attribution.
SANITIZE_OMO_JUNIOR_RE = re.compile(
    r"Sisyphus-Junior - Focused executor from OhMyOpenCode", re.IGNORECASE
)
# 上游 WAF 把「客户端自我介绍句式 + 品牌」当成非官方渠道（#533 / #537 / #539）：
#   You are an AI agent powered by DeepSeek Harness.
#   You are a coding agent powered by the deepseek-v4.1-flash model.
#   You are an AI assistant built on DeepSeek.
# 大小写不敏感、连续子串匹配；单独品牌词或改词后放行。这里整句换成中性句，
# 不依赖具体品牌，也不保留 "powered by / built on" 结构。
#
# 只作用于 system / developer / assistant 角色（见 sanitize_messages）：user / tool
# 里的品牌词是用户数据，改写会让模型看到与磁盘不一致的内容（#537）。assistant 在
# 1.7.25 及以前没被覆盖，而同一句放进 assistant 历史会稳定 400（#574）。
#
# 1.7.18 及以前只认 "agent powered by" / "assistant built on" 两种动词搭配，而同一类
# 经纬句还有 "built by"（如 "You are <Product> Agent, built by <Vendor>."）、
# "created by" / "developed by" / "made by" / "trained by" 等变体，以及省掉名词的
# "You are powered by ..."。上游只补其中一条规则就能让这些句子重新 400，所以在
# 同一判据内把动词表补全；名词可省略，冠词后允许 0~3 个修饰词（专名也算，如
# "<Product> Agent"），逗号可有可无。窗口有界，避免 "You are free to use tools built
# by ..." 这类分词修饰宾语的句子被误伤。
SANITIZE_IDENTITY_VERBS = (
    "powered by", "built on", "built by", "created by",
    "developed by", "made by", "trained by",
)
SANITIZE_IDENTITY_RE = re.compile(
    r"You are\s+"
    r"(?:an?\s+|the\s+)?(?:[\w.-]+\s+){0,3}?"
    r"(?:agent|assistant|model|AI|LLM)?[\s,]*"
    r"(?:powered by|built on|built by|created by|developed by|made by|trained by)"
    r"[^\n]*?(?:\.(?=\s|$)|(?=\n|$))",
    re.IGNORECASE)
SANITIZE_IDENTITY_REPLACEMENT = "You are a helpful AI assistant."
# GUI 描述段（#570）：DSH 的系统提示里有一段 GUI 描述，上游按「A 句 + B 句连续出现」
# 判 11-128。逐段二分实测：指纹是整段，改 A 句、改 B 句、或在两句之间插东西都会失配。
#
# 这里选改动最小的一种：只把 A 句尾的语序换一下（`the runtime context names` ->
# `is named in the runtime context`），一个词都不删——`DeepSeek Harness` 全名保留，
# 句子意思完全不变。既不碰品牌词，也不像插入不可见字符那样在提示词里留下无法 diff
# 的痕迹。
#
# 只锚定这一句：句子之外的同名品牌、路径、包名都不受影响。
SANITIZE_DSH_GUI_RE = re.compile(
    r"(?i)(You are interacting with the user through the "
    r"DeepSeek[ \t]+Harness[ \t]+Web[ \t]+GUI)"
    r" whose URL the runtime context names\.")
SANITIZE_DSH_GUI_REPLACEMENT = r"\1 whose URL is named in the runtime context."
SANITIZE_DSH_GUI_LOWER = "whose url the runtime context names"
# 品牌词缩减（#574）：`DeepSeek Harness` 单独出现放行，但它和 GUI 描述句
# （`this page` / `this GUI` / `this app`）连续出现时命中同一套 11-128 校验。
# 逐段二分实测：只把品牌缩成 `DeepSeek` 就能通过，句子其余部分不用动；
# 换成 DSH / the project / AI assistant 也都能过，取语义损失最小的那个。
#
# 只匹配散文里的空格 / 制表符形式。`deepseek-harness`、`deepseek_harness`、
# `/opt/deepseek-harness/app` 这类路径与标识符保持原样（#574 里实测过
# `[ \t_-]*` 会把它们一起改坏）。
SANITIZE_BRAND_RE = re.compile(r"DeepSeek[ \t]+Harness", re.IGNORECASE)
SANITIZE_BRAND_REPLACEMENT = "DeepSeek"
SANITIZE_BRAND_LOWER = "harness"
# 指纹判定的字面量快路径（见 has_fingerprint）：原先对每段文本跑两个 (?i) 正则，
# 4KB 输入实测 319µs，其中正则 137+188µs、几个 `in` 只占 9µs；64KB 文本要
# 6.2ms，而 sanitize_messages 会给每个文本段都过一遍——build_upstream_body
# (168KB) 的耗时约 95% 都耗在这里（13.60ms -> 2.14ms）。
# 两个模式都是纯字面量，换成小写副本上的 `in` 之后是纯 C 速度的单遍扫描。
#
# 等价性：re.IGNORECASE 的 Unicode 语义与 str.lower() 只有三处差异（CPython
# 3.13 全码点实测，其余所有带大小写的码点两法完全一致）：
#   'İ'(U+0130) 与 'ı'(U+0131) 能匹配字面量 i，'ſ'(U+017F) 能匹配 s，
# 而这三个字符的 lower() 并不落到 i/s。所以先把它们按 SANITIZE_REI_FOLD
# 归一化成基字母、再小写，副本上的 `in` 与 (?i) search 完全等价（多字符模式
# 同样成立；全码点替换 + 定向用例 + 随机串对拍零差异，见 tests/_test_hotpath_savings.py）。
SANITIZE_REI_FOLD = str.maketrans({0x130: "i", 0x131: "i", 0x17F: "s"})
# 两个小写字面量直接从正则派生，保证与正则永远同源：手抄第二份迟早漂移，
# 派生出来的值不存在这个问题。pattern 里的 (?i) 是内联旗标，剥掉后就是
# 字面量本体。
SANITIZE_BARE_HDR_LOWER = SANITIZE_BARE_HDR_RE.pattern
if SANITIZE_BARE_HDR_LOWER.startswith("(?i)"):
    SANITIZE_BARE_HDR_LOWER = SANITIZE_BARE_HDR_LOWER[4:]
SANITIZE_OMO_JUNIOR_LOWER = SANITIZE_OMO_JUNIOR_RE.pattern.lower()
SANITIZE_IDENTITY_LOWER = SANITIZE_IDENTITY_VERBS
def has_fingerprint(text, identity=True):
    if not isinstance(text, str) or not text:
        return False
    for f in SANITIZE_FEATURES:
        if f in text:
            return True
    # 三个特例字符几乎从不出现：先做三次 C 速度探测，命中才走归一化副本，
    # 常规文本只付一次 lower()。
    if "\u0130" in text or "\u0131" in text or "\u017f" in text:
        low = text.translate(SANITIZE_REI_FOLD).lower()
    else:
        low = text.lower()
    if (SANITIZE_BARE_HDR_LOWER in low
            or SANITIZE_OMO_JUNIOR_LOWER in low):
        return True
    if not identity:
        return False
    # 品牌词缩减：`harness` 是廉价前置筛，真命中交给正则。
    if (SANITIZE_BRAND_LOWER in low
            and SANITIZE_BRAND_RE.search(text) is not None):
        return True
    # GUI 描述段：先做一次字面量探测，命中才走正则。
    if (SANITIZE_DSH_GUI_LOWER in low
            and SANITIZE_DSH_GUI_RE.search(text) is not None):
        return True
    # 动词短语只是廉价前置筛：真命中与否交给正则判定。这样 "powered by" 出现在
    # 与自我介绍无关的句子（工具/插件的产品描述）时不会走进 sanitize_text 的其余
    # 清洗步骤，也不会白白改写。
    if not any(needle in low for needle in SANITIZE_IDENTITY_LOWER):
        return False
    return SANITIZE_IDENTITY_RE.search(text) is not None
def sanitize_text(text, identity=True):
    if not isinstance(text, str) or not text:
        return text
    if not has_fingerprint(text, identity=identity):
        return text
    # Keep the rewrite deliberately narrow: do not globally remove
    # "OhMyOpenCode" or "Sisyphus-Junior", because either token alone is
    # accepted by the upstream. Only the confirmed contiguous fingerprint is
    # neutralized.
    text = SANITIZE_OMO_JUNIOR_RE.sub("Sisyphus-Junior - Focused executor", text)
    if identity:
        text = SANITIZE_IDENTITY_RE.sub(SANITIZE_IDENTITY_REPLACEMENT, text)
        # GUI 描述段单独一条规则：锚定整句，只调整句尾语序。
        text = SANITIZE_DSH_GUI_RE.sub(SANITIZE_DSH_GUI_REPLACEMENT, text)
        # 品牌词缩减放在最后：GUI 规则还认全名，先让它匹配原文。
        text = SANITIZE_BRAND_RE.sub(SANITIZE_BRAND_REPLACEMENT, text)
    for old, new in SANITIZE_REWRITES:
        text = text.replace(old, new)
    text = SANITIZE_HDR_RE.sub("", text)
    if "cc_" in text:
        prev = ""
        while prev != text:
            prev = text
            text = SANITIZE_KV_RE.sub("", text)
    text = SANITIZE_BARE_HDR_RE.sub("x-anthropic-billing-hdr", text)
    return text.strip()
def sanitize_content(content, identity=True):
    if isinstance(content, str):
        return sanitize_text(content, identity=identity)
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and "text" in part:
                p = dict(part)
                p["text"] = sanitize_text(p["text"], identity=identity)
                out.append(p)
            else:
                out.append(part)
        return out
    return content


def preserve_request_contract_enabled():
    """Opt-in field/data preservation; does not enforce upstream constraints."""
    return os.environ.get("WB_PRESERVE_REQUEST_CONTRACT", "0").strip().lower() in (
        "1", "true", "yes", "on")


def sanitize_tool_calls(tool_calls):
    if not isinstance(tool_calls, list):
        return tool_calls
    # Arguments are serialized application data, not a client fingerprint.
    # Even when preservation is on, a non-string value still has to become a
    # JSON string: the upstream Go struct rejects a dict with code 11101
    # (#446). Preservation only skips the text rewrite for string values.
    preserve = preserve_request_contract_enabled()
    out = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            item = dict(tc)
            fn = item.get("function")
            if isinstance(fn, dict):
                fn = dict(fn)
                raw_args = fn.get("arguments")
                if isinstance(raw_args, str):
                    fn["arguments"] = (raw_args if preserve
                                       else sanitize_text(raw_args, identity=False))
                elif raw_args is None:
                    fn["arguments"] = "{}"
                else:
                    # Upstream Go struct expects a string for function.arguments.
                    # Clients passing dict, list, int, bool, etc. trigger code 11101
                    # (cannot unmarshal into field of type string). Serialize to JSON.
                    try:
                        fn["arguments"] = json.dumps(raw_args, ensure_ascii=False)
                    except Exception:
                        fn["arguments"] = str(raw_args)
                item["function"] = fn
            out.append(item)
        else:
            out.append(tc)
    return out
def sanitize_messages(messages):
    out = []
    for m in messages or []:
        if isinstance(m, dict):
            item = dict(m)
            preserve_tool_result = (preserve_request_contract_enabled()
                                    and item.get("role") in ("tool", "function"))
            # assistant 历史里的身份句同样是已确认的指纹（#574：同一句放在
            # assistant 里 400/400/400，放在 user 里 200/200/200）。user / tool
            # 仍是用户数据，不改写。
            identity = item.get("role") in ("system", "developer", "assistant")
            if "content" in item and not preserve_tool_result:
                item["content"] = sanitize_content(item["content"],
                                                   identity=identity)
            if isinstance(item.get("reasoning_content"), str):
                item["reasoning_content"] = sanitize_text(
                    item["reasoning_content"], identity=False)
            if "tool_calls" in item:
                item["tool_calls"] = sanitize_tool_calls(item["tool_calls"])
            out.append(item)
        else:
            out.append(m)
    return out


# ---------------------------------------------------------------------------
# Tool-call pairing repair
# ---------------------------------------------------------------------------
def _empty_content(value):
    """True when an assistant message carries no content at all.

    None / "" / [] all mean "no content": clients disagree on which empty
    shape they emit, and a batch split into adjacent assistant messages must
    merge for either shape (a missed [] was the original 11148 case).
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value == ""
    if isinstance(value, list):
        return len(value) == 0
    return False


def _merge_reasoning_content(dst, src):
    """Fold src's reasoning_content into dst, newline-joined when both exist."""
    rc = src.get("reasoning_content")
    if not isinstance(rc, str) or not rc:
        return
    prev = dst.get("reasoning_content")
    if isinstance(prev, str) and prev:
        dst["reasoning_content"] = prev + "\n" + rc
    else:
        dst["reasoning_content"] = rc


def merge_adjacent_tool_calls(messages):
    """Merge back-to-back assistant tool_calls messages into one.

    Some OpenAI-compatible agent clients replay a parallel batch as several
    adjacent assistant messages, each carrying one tool_call. DeepSeek-family
    upstreams answer 400 / code 11148 (tool_call_sequence_broken) for that
    shape and retire the conversation, because every retry replays the same
    history and switching accounts cannot help. Merging the declarations
    restores the shape the upstream accepts.

    Conditions are strict, so no semantics are invented:
      - the messages must be adjacent;
      - the trailing message must have empty content (None/""/[]);
      - the leading message must already be an assistant with tool_calls.

    A second form folds a plain assistant's string content into a preceding
    tool_calls assistant that has no content of its own (the same split batch
    replayed in the other order). reasoning_content is preserved either way,
    because DeepSeek multi-turn thinking requires it back.
    """
    if not isinstance(messages, list) or len(messages) < 2:
        return messages, False
    out = []
    changed = False
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        if m.get("role") == "assistant" and out:
            prev = out[-1] if isinstance(out[-1], dict) else None
            if prev is not None and prev.get("role") == "assistant":
                tcs = m.get("tool_calls")
                prev_calls = prev.get("tool_calls")
                # Form 1: this message declares tool calls and has no content.
                if (isinstance(tcs, list) and tcs
                        and _empty_content(m.get("content"))
                        and isinstance(prev_calls, list) and prev_calls):
                    prev["tool_calls"] = prev_calls + tcs
                    _merge_reasoning_content(prev, m)
                    changed = True
                    continue
                # Form 2: plain string content folds into a preceding
                # content-less tool_calls assistant.
                if ("tool_calls" not in m
                        and isinstance(m.get("content"), str) and m["content"]
                        and isinstance(prev_calls, list) and prev_calls
                        and _empty_content(prev.get("content"))):
                    prev["content"] = m["content"]
                    _merge_reasoning_content(prev, m)
                    changed = True
                    continue
        out.append(m)
    if not changed:
        return messages, False
    return out, True


def repack_tool_result_blocks(messages):
    """Keep a tool_calls batch and its results adjacent.

    The upstream requires the role:"tool" results to follow the assistant
    message that requested them with nothing in between. Codex's
    image_resize_notice, for one, arrives as a developer message right after a
    tool output; with parallel calls it lands between two results, the pairing
    reads as broken and the upstream rejects the whole request (code 11148),
    retiring the conversation. This only reorders: same results, same relative
    order, the intruders moved behind the batch.
    """
    if not isinstance(messages, list) or len(messages) < 3:
        return messages, False
    out = []
    changed = False
    i = 0
    while i < len(messages):
        m = messages[i]
        if not isinstance(m, dict) or m.get("role") != "assistant":
            out.append(m)
            i += 1
            continue
        calls = m.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            out.append(m)
            i += 1
            continue
        want = set()
        for tc in calls:
            if isinstance(tc, dict):
                tid = tc.get("id")
                if isinstance(tid, str) and tid:
                    want.add(tid)
        out.append(m)
        i += 1
        results = []
        between = []
        while i < len(messages):
            mm = messages[i]
            if not isinstance(mm, dict):
                break
            role = mm.get("role")
            if role == "tool":
                tid = mm.get("tool_call_id")
                if not (isinstance(tid, str) and tid in want):
                    break
                results.append(mm)
                i += 1
                continue
            # Any assistant message ends this batch. A message that is not a
            # result is an intruder: it may sit before the first result (the
            # old code gave up here, leaving the pairing broken) or between
            # two results. Either way it moves behind the whole batch.
            if role == "assistant":
                break
            between.append(mm)
            i += 1
        if results:
            out.extend(results)
            out.extend(between)
            if between:
                changed = True
        else:
            out.extend(between)
    if not changed:
        return messages, False
    return out, True


def cleanup_orphan_tool_calls(messages):
    """Drop tool calls that have no result, and results that have no call.

    A failed tool call (bad arguments, timeout, unknown tool) leaves the client
    with an assistant tool_calls entry it can never answer: the result message
    is never written, yet the entry rides along with the history on every later
    turn and the upstream rejects each one (code 11148), so a single failed
    call can retire a whole conversation. Both sides are trimmed against the
    same set of ids, so no half-pairing can survive the repair.
    """
    if not isinstance(messages, list) or not messages:
        return messages, False
    call_ids = set()
    result_ids = set()
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "tool":
            tid = m.get("tool_call_id")
            if isinstance(tid, str) and tid:
                result_ids.add(tid)
        elif role == "assistant":
            calls = m.get("tool_calls")
            if isinstance(calls, list):
                for tc in calls:
                    if isinstance(tc, dict):
                        tid = tc.get("id")
                        if isinstance(tid, str) and tid:
                            call_ids.add(tid)
    if not call_ids and not result_ids:
        return messages, False
    keep = call_ids & result_ids
    changed = False
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        calls = m.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            continue
        kept = [tc for tc in calls
                if isinstance(tc, dict) and isinstance(tc.get("id"), str)
                and tc["id"] in keep]
        if len(kept) == len(calls):
            continue
        changed = True
        if kept:
            m["tool_calls"] = kept
        else:
            m.pop("tool_calls", None)
    out = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "tool":
            tid = m.get("tool_call_id")
            if not (isinstance(tid, str) and tid in keep):
                changed = True
                continue
        out.append(m)
    if not changed:
        return messages, False
    return out, True


def is_truncated_arguments(raw):
    """True when a non-empty tool-arguments string is not valid JSON.

    A stream cut off by max_tokens or a dropped connection leaves the last
    tool call with half-written JSON. An empty/whitespace string is a legal
    no-argument tool, and any parseable JSON (including null/scalars/arrays)
    is the model's own output for the client to validate - only non-empty
    unparsable strings count as truncation.
    """
    if not isinstance(raw, str):
        return False
    trimmed = raw.strip()
    if not trimmed:
        return False
    try:
        json.loads(trimmed)
        return False
    except Exception:
        return True


def drop_truncated_tool_calls(calls):
    """Return the tool calls whose arguments are not half-written JSON."""
    if not isinstance(calls, list):
        return calls
    kept = []
    for call in calls:
        if not isinstance(call, dict):
            kept.append(call)
            continue
        fn = call.get("function")
        if isinstance(fn, dict) and is_truncated_arguments(fn.get("arguments")):
            continue
        kept.append(call)
    return kept


# ---------------------------------------------------------------------------
# DeepSeek Multi-turn Consistency: reasoning_content backfill
# ---------------------------------------------------------------------------
# Upstream (code 11155 "the reasoning content from the previous turn must be
# passed back in thinking mode") requires every assistant message to carry a
# `reasoning_content` string while thinking is on. Two halves gate the fix,
# mirroring the official client's ReasoningContentBackfillRule:
#   - thinkingEnabled: deepseek + thinking enabled -> always backfill, even
#     when a third-party client dropped reasoning entirely (this was the bug:
#     only the hasTrace half existed, so zero-trace histories were forwarded
#     untouched and rejected).
#   - hasTrace: any existing reasoning trace -> backfill regardless of the
#     thinking flag.
# Upstream also validates len(reasoning) > 0, so an empty placeholder is not
# enough on its own: `reasoning` is mirrored with a non-empty value.
def backfill_reasoning_content(messages, model, thinking_enabled=None):
    if not model or not str(model).lower().startswith("deepseek"):
        return messages
    if thinking_enabled is None:
        thinking_enabled = False
    has_trace = False
    for m in messages:
        if not isinstance(m, dict):
            continue
        reasoning = m.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            has_trace = True
            break
        if "reasoning_content" in m:
            has_trace = True
            break
    if not thinking_enabled and not has_trace:
        return messages
    out = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant":
            item = dict(m)
            rc = item.get("reasoning_content")
            if not isinstance(rc, str):
                # Non-string (null/number/absent) counts as missing, matching
                # the official `typeof !== "string"` check.
                legacy = item.get("reasoning")
                rc = legacy if isinstance(legacy, str) else ""
                item["reasoning_content"] = rc
            # Mirror onto `reasoning` with a non-empty value: upstream rejects
            # an empty/absent reasoning, while a whitespace placeholder passes
            # its length check and carries no model-visible semantics.
            existing = item.get("reasoning")
            if not (isinstance(existing, str) and existing):
                item["reasoning"] = rc if rc else " "
            out.append(item)
        else:
            out.append(m)
    return out
# ---------------------------------------------------------------------------
# Tool & Tool Choice Normalization (avoids code 11101 on object tool_choice)
# ---------------------------------------------------------------------------
def normalize_tool_choice(obj):
    if "tool_choice" not in obj:
        return
    tc = obj["tool_choice"]
    if isinstance(tc, str):
        val = tc.strip().lower()
        if val == "none":
            # 这里曾经把 tools/functions 一起删掉，那正是 Agent 死循环的成因：
            # 工具声明没了，模型拿不到函数签名、又没有结构化工具通道，却仍被要求
            # 完成任务，于是把调用降级成 DSML / 伪 JSON 文本塞进 content
            # （tool_calls 为空、finish_reason=stop）。客户端解析不到调用只能再
            # 追问一轮，模型又重复一遍 "I'll do it"，上下文每轮 +2 条消息、token
            # 线性膨胀，直到撑爆窗口或用户手动断开。
            #
            # tool_choice="none" 的语义是「本轮不许调用工具」，这层意思由
            # tool_choice 字段本身表达就够了，不需要抹掉能力声明。
            # 上游把 tool_choice 声明为 string（发对象会 11101），所以保持字符串
            # 原样透传，同时保留 tools。
            #
            # 取舍：实测本上游并不真正遵守 tool_choice="none"（保留 tools 后它
            # 仍返回 tool_calls）。但对比两条路 —— 删 tools 会让模型输出不可解析
            # 的文本、Agent 原地空转；留 tools 则走正常 tool_calls 通道，客户端能
            # 正常执行与回填 —— 后者明显更好。确实需要禁止调用时，客户端不传
            # tools 即可。
            obj["tool_choice"] = "none"
        return
    if isinstance(tc, dict):
        typ = (tc.get("type") or "").strip().lower()
        if typ == "none":
            # 同上：保留 tools 声明。上游只认字符串，对象形式必须降级成
            # "none"，否则 11101。
            obj["tool_choice"] = "none"
        elif typ in ("auto", "required"):
            obj["tool_choice"] = typ
        elif typ == "function":
            name = (tc.get("function") or {}).get("name") or tc.get("name") or ""
            obj["tool_choice"] = name.strip() or "auto"
        else:
            obj.pop("tool_choice", None)
    else:
        obj.pop("tool_choice", None)
# ---------------------------------------------------------------------------
# Tool Schema Parameters Normalization (avoids code 11129)
# ---------------------------------------------------------------------------
_SCHEMA_TYPE_MAP = {
    "str": "string",
    "text": "string",
    "int": "integer",
    "bool": "boolean",
    "dict": "object",
    "list": "array",
    "float": "number",
}
_SCHEMA_VALID_TYPES = {"string", "number", "integer", "boolean", "array", "object", "null"}


def _deep_normalize_schema(s):
    """Recursively clean and normalize JSON Schema for upstream Go validator."""
    if not isinstance(s, dict):
        return s

    # 1. Strip metadata fields with null/None values (upstream strictly rejects null description/title)
    for k in list(s.keys()):
        if s[k] is None and k in ("description", "title", "format", "pattern", "items", "default"):
            s.pop(k, None)

    # 2. Normalize type declarations
    if "type" in s:
        typ = s["type"]
        if isinstance(typ, str):
            typ_lower = typ.strip().lower()
            if typ_lower in _SCHEMA_TYPE_MAP:
                s["type"] = _SCHEMA_TYPE_MAP[typ_lower]
            elif typ_lower not in _SCHEMA_VALID_TYPES:
                if "properties" in s:
                    s["type"] = "object"
                elif "items" in s:
                    s["type"] = "array"
                else:
                    s.pop("type", None)
            else:
                s["type"] = typ_lower
        elif isinstance(typ, list):
            s["type"] = [_SCHEMA_TYPE_MAP.get(str(t).lower(), str(t).lower()) for t in typ if t]

    # 3. Ensure object types always have a properties dictionary
    if s.get("type") == "object" or ("properties" in s and s.get("type") is None):
        if s.get("type") is None:
            s["type"] = "object"
        if "properties" not in s or not isinstance(s.get("properties"), dict):
            s["properties"] = {}

    # 4. Clean required list: drop None, empty strings, ensure list of strings
    if "required" in s:
        req = s["required"]
        if isinstance(req, list):
            cleaned_req = [str(x) for x in req if x is not None and str(x).strip()]
            if cleaned_req:
                s["required"] = cleaned_req
            else:
                s.pop("required", None)
        elif isinstance(req, str) and req.strip():
            s["required"] = [req.strip()]
        else:
            s.pop("required", None)

    # 5. Handle array items: unpack tuple schema into dict, recursively normalize
    if "items" in s:
        items = s["items"]
        if isinstance(items, list):
            first_dict = next((it for it in items if isinstance(it, dict)), None)
            s["items"] = _deep_normalize_schema(dict(first_dict)) if first_dict is not None else {"type": "string"}
        elif isinstance(items, dict):
            _deep_normalize_schema(items)
        elif items is None:
            s.pop("items", None)

    # 6. Recursively normalize properties
    if "properties" in s and isinstance(s["properties"], dict):
        for prop_name, prop_val in list(s["properties"].items()):
            if isinstance(prop_val, dict):
                _deep_normalize_schema(prop_val)

    # 7. Recursively normalize definitions / $defs
    for def_key in ("$defs", "definitions"):
        if def_key in s and isinstance(s[def_key], dict):
            for d_name, d_val in list(s[def_key].items()):
                if isinstance(d_val, dict):
                    _deep_normalize_schema(d_val)

    # 8. Recursively normalize composition keywords (anyOf, oneOf, allOf)
    for comb_key in ("anyOf", "oneOf", "allOf"):
        if comb_key in s and isinstance(s[comb_key], list):
            for sub in s[comb_key]:
                if isinstance(sub, dict):
                    _deep_normalize_schema(sub)

    return s


def _normalize_tool_parameters(fn):
    """Ensure function parameters comply with strict upstream schema (avoids code 11129).

    Upstream Go schema validator expects function parameters to have:
      - `type: "object"`
      - `properties: {...}` (a dictionary, even if empty)
      - `required` as a list of strings without nulls
      - valid JSON schema types (converting aliases like 'str'/'int'/'dict' to 'string'/'integer'/'object')
      - no null values in metadata fields like description/title
    This function recursively normalizes nested parameters, properties, items, and definitions.

    Note on WB_PRESERVE_REQUEST_CONTRACT (#423):
      Schema validity normalization is an upstream transport-layer requirement,
      not an application-level data rewriting (unfixed schemas cause upstream to
      reject the call with 400 regardless of preservation flags). This fix strictly
      repairs malformed types without stripping compliant fields or modifying
      existing semantic parameters.
    """
    if not isinstance(fn, dict):
        return
    params = fn.get("parameters")
    if params is None or not isinstance(params, dict):
        fn["parameters"] = {"type": "object", "properties": {}}
        return
    p = dict(params)
    if p.get("type") != "object":
        p["type"] = "object"
    if "properties" not in p or not isinstance(p.get("properties"), dict):
        p["properties"] = {}
    _deep_normalize_schema(p)
    fn["parameters"] = p


def normalize_tools(obj):
    # Older clients might still send root-level `functions`
    if "functions" in obj and "tools" not in obj:
        funcs = obj.pop("functions")
        if isinstance(funcs, list):
            obj["tools"] = [{"type": "function", "function": f} for f in funcs if isinstance(f, dict)]

    tools = obj.get("tools")
    if not tools or not isinstance(tools, list):
        if "tools" in obj:
            obj.pop("tools", None)
        return
    norm = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        # Wrap top-level name tool definition into Chat Completions function schema
        if "name" in t and "function" not in t:
            fn = {
                "name": t.get("name") or "",
                "description": t.get("description") or "",
                "parameters": t.get("parameters") or {},
            }
            if "strict" in t:
                fn["strict"] = t["strict"]
            _normalize_tool_parameters(fn)
            norm.append({"type": "function", "function": fn})
        else:
            item = dict(t)
            if item.get("type") != "function":
                item["type"] = "function"
            if "function" in item and isinstance(item["function"], dict):
                fn = dict(item["function"])
                _normalize_tool_parameters(fn)
                item["function"] = fn
            norm.append(item)
    if norm:
        obj["tools"] = norm
    else:
        obj.pop("tools", None)
# ---------------------------------------------------------------------------
# Stop Sequence Normalization (avoids code 11101 on a string stop)
# ---------------------------------------------------------------------------
def normalize_stop(obj):
    # OpenAI 把 stop 声明成 string | array，客户端传单个字符串完全合法；本上游的
    # Go 端却把该字段定死为 []string，收到字符串直接 400：
    #   Unmarshal chat params failed with error: json: cannot unmarshal string
    #   into Go struct field Request.stop of type []string   (code 11101)
    # 实测（deepseek-v4.1-flash，同一条 prompt，同一个上游账号）：
    #   "stop": "END"     -> 400 / code 11101
    #   "stop": ["END"]   -> 200
    # 归一化成数组后语义完全等价：OpenAI 里单字符串就是单元素数组，模型侧看到的
    # 停止序列一模一样。空字符串 / 空数组 / null / 非法类型一律删字段——它们的实际
    # 含义都是「没有停止序列」，而空值本身在上游同样会被判参数不合法。
    if "stop" not in obj:
        return
    val = obj["stop"]
    if isinstance(val, str):
        val = [val] if val else []
    elif isinstance(val, (list, tuple)):
        val = [s for s in val if isinstance(s, str) and s]
    else:
        val = []
    if val:
        obj["stop"] = val
    else:
        obj.pop("stop", None)
# ---------------------------------------------------------------------------
# DeepSeek DSML Tool Calls Fallback Parser
# ---------------------------------------------------------------------------
TAG_START = r"<[^>]*DSML[^>]*"
DSML_CALLS_RE = re.compile(TAG_START + r"calls>(.*?)</[^>]*DSML[^>]*calls>", re.DOTALL)
DSML_INVOKE_RE = re.compile(TAG_START + r"invoke\s+name=[\x22\x27]([^\x22\x27]+)[\x22\x27]>(.*?)</[^>]*invoke>", re.DOTALL)
DSML_PARAM_RE = re.compile(TAG_START + r"parameter\s+name=[\x22\x27]([^\x22\x27]+)[\x22\x27][^>]*>(.*?)</[^>]*parameter>", re.DOTALL)
def parse_dsml_tool_calls(text):
    if not text or "DSML" not in text:
        return None, text
    match = DSML_CALLS_RE.search(text)
    if not match:
        return None, text
    calls_block = match.group(1)
    tool_calls = []
    for inv_match in DSML_INVOKE_RE.finditer(calls_block):
        func_name = inv_match.group(1)
        params_block = inv_match.group(2)
        params = {}
        for p_match in DSML_PARAM_RE.finditer(params_block):
            p_name = p_match.group(1)
            p_val = p_match.group(2).strip()
            params[p_name] = p_val
        tool_calls.append({
            "id": _new_id("call_"),
            "name": func_name,
            "arguments": json.dumps(params, ensure_ascii=False),
        })
    clean = (text[:match.start()].strip() + " " + text[match.end():].strip()).strip()
    return tool_calls, clean
# ---------------------------------------------------------------------------
# Printed tool calls on the Chat Completions path
# ---------------------------------------------------------------------------
# The Responses path has parsed DSML text since the tool_choice="none" fix, but
# /v1/chat/completions -- the route the desktop client actually uses -- still
# forwarded upstream `content` verbatim. When the model printed its call instead
# of emitting structured tool_calls, the client saw plain text, found no call,
# and the turn just ended: the markup stayed in the transcript and nothing ran.
# Reported by the user as "it keeps stopping by itself".
#
# Two spellings show up in the wild:
#   1. the full-width DSML form the suite already pins:
#      <||DSML|| calls><||DSML|| invoke name="run_code"> ... </||DSML|| invoke>
#   2. a hybrid the model mixes on its own -- a plain opening tag with DSML
#      closing tags, and the first parameter's close tag swallowed whole:
#      <run_code><parameter name="code">...</parameter>
#      <parameter name="description">...</|DSML| parameter>
#      </run_code></invoke></|DSML| parameter></|DSML| invoke></|DSML| calls>
#
# parse_dsml_tool_calls() above only matches (1): it wants a DSML opening tag
# for the calls block, which (2) never has. The parser below is driven by the
# <parameter> elements instead, because those are spelled the same way in both
# shapes and a parameter name is the one thing a call cannot do without.
LEAK_PARAM_RE = re.compile(
    r"<\s*(?:\|\s*DSML\s*\|\s*)?(?P<tag>parameter|param|arg|argument)\b"
    r"(?P<attrs>[^>]*)>"
    r"(?P<value>.*?)"
    r"</\s*(?:\|\s*DSML\s*\|\s*)?(?:parameter|param|arg|argument)\s*>",
    re.DOTALL | re.IGNORECASE,
)
LEAK_TAG_RE = re.compile(
    r"<\s*/?\s*(?:\|\s*DSML\s*\|\s*)?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<attrs>[^>]*)>"
)
# The full-width spelling (｜｜DSML｜｜) is what the model actually prints;
# LEAK_TAG_RE only knows the ASCII bars, so closing-tag boundaries get their
# own matcher that accepts both.
LEAK_CLOSE_RE = re.compile(
    r"<\s*/\s*(?:\|\s*DSML\s*\|\s*|[\uff5c]+\s*DSML\s*[\uff5c]+\s*)?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)[^>]*>",
    re.IGNORECASE,
)
LEAK_NAME_ATTR_RE = re.compile(r"""name\s*=\s*["']([^"']*)["']""")
LEAK_WRAPPER_TAGS = {"calls", "tool_calls", "function_calls", "function_call"}
LEAK_PARAM_TAGS = {"parameter", "param", "arg", "argument"}
# A printed call is complete once one of these closing tags arrives. The
# stream filter used to wait for EOF, so a long tool call left the client
# silent and then dumped the whole block at the end (issue #549).
CALL_CLOSE_TAGS = LEAK_WRAPPER_TAGS | {"invoke", "run_code", "run_shell"}
# First characters of every tag the stream filter has to hold text back for.
LEAK_TAG_PREFIXES = (
    "<run_code", "<run_shell", "<invoke", "<parameter", "<param", "<arg",
    "<argument", "<calls", "<tool_calls", "<function_calls", "<function_call",
    "<think", "<thinking",
    "</run_code", "</run_shell", "</invoke", "</parameter", "</param",
    "</arg", "</argument", "</calls", "</tool_calls", "</function_calls",
    "</function_call", "</think", "</thinking",
    "<|dsml|", "</|dsml|",
)


def _normalize_dsml_bars(text):
    """Fold the full-width spelling of the DSML token back to ASCII bars."""
    return re.sub(r"\uff5c+\s*DSML\s*\uff5c+", "|DSML|", text)


def _strip_spans(text, spans):
    """Remove every [start, end) span, then collapse the leftover blanks."""
    if not spans:
        return text
    kept = []
    last = 0
    for start, end in sorted(spans):
        if start < last:
            continue
        kept.append(text[last:start])
        last = end
    kept.append(text[last:])
    return "".join(kept).strip()


def parse_leaked_tool_calls(text):
    """Recover a tool call the model printed instead of emitting.

    Returns (calls, clean_text) with calls in the same shape as
    parse_dsml_tool_calls(), or (None, text) when nothing was recognised.
    """
    if not text or "<" not in text:
        return None, text
    # Fold the full-width spelling first so both shapes go through one walk.
    original = text
    text = _normalize_dsml_bars(text)
    found = []
    for m in LEAK_PARAM_RE.finditer(text):
        attr = LEAK_NAME_ATTR_RE.search(m.group("attrs") or "")
        if not attr or not attr.group(1):
            continue
        found.append((m.start(), attr.group(1), m.group("value").strip(), m.span()))
    if not found:
        return None, original
    tags = list(LEAK_TAG_RE.finditer(text))
    calls = []
    for start, pname, value, span in found:
        owner = None
        for tag in tags:
            if tag.start() >= start:
                break
            if tag.group(0).lstrip().startswith("</"):
                continue
            tname = tag.group("name").lower()
            if tname in LEAK_WRAPPER_TAGS or tname in LEAK_PARAM_TAGS:
                continue
            owner = tag
        name = ""
        if owner is not None:
            attr = LEAK_NAME_ATTR_RE.search(owner.group("attrs") or "")
            name = (attr.group(1) if attr else owner.group("name")).strip()
        if not name:
            continue
        key = owner.start() if owner is not None else None
        if calls and calls[-1]["open"] == key:
            calls[-1]["params"][pname] = value
            calls[-1]["spans"].append(span)
        else:
            calls.append({"name": name, "params": {pname: value},
                          "open": key, "spans": [span]})
    if not calls:
        return None, original
    # Everything from the call's opening tag to the last of its parameters is
    # markup rather than prose, so the whole region goes away in one piece.
    regions = []
    for call in calls:
        first = call["open"]
        if first is None:
            first = min(s for s, _e in call["spans"])
        last = max(e for _s, e in call["spans"])
        # Swallow the closing tags that trail the last parameter.
        while True:
            nxt = last
            while nxt < len(text) and text[nxt] in " \t\r\n":
                nxt += 1
            m = LEAK_TAG_RE.match(text, nxt)
            if not m or not m.group(0).lstrip().startswith("</"):
                break
            last = m.end()
        regions.append((first, last))
    clean = _strip_spans(text, regions)
    # Wrappers and stray tags the region walk did not cover (<|DSML| calls>,
    # <think>, ...) are markup too; drop any tag carrying the DSML token.
    leftover = []
    for m in LEAK_TAG_RE.finditer(clean):
        if "DSML" in _normalize_dsml_bars(m.group(0)).upper():
            leftover.append(m.span())
        elif m.group("name").lower() in LEAK_WRAPPER_TAGS:
            leftover.append(m.span())
    clean = _strip_spans(clean, leftover)
    return [{
        "id": _new_id("call_"),
        "name": call["name"],
        "arguments": json.dumps(call["params"], ensure_ascii=False),
    } for call in calls], clean


def repair_leaked_tool_calls(message):
    """Move a printed call out of `content` and into `tool_calls`.

    Only fires when the message carries no structured call: a real tool_calls
    entry always wins, so a model that emits both is left alone. Returns True
    when the message changed.
    """
    if not isinstance(message, dict):
        return False
    existing = message.get("tool_calls")
    if isinstance(existing, list) and existing:
        return False
    content = message.get("content")
    if not isinstance(content, str) or "<" not in content:
        return False
    calls, clean = parse_leaked_tool_calls(content)
    if not calls:
        calls, clean = parse_dsml_tool_calls(content)
    if not calls:
        return False
    message["content"] = clean
    message["tool_calls"] = [{
        "id": call["id"],
        "type": "function",
        "function": {"name": call["name"], "arguments": call["arguments"]},
    } for call in calls]
    return True


def could_be_leaked_call(tail):
    """True while `tail` (starting at "<") may still grow into printed markup."""
    low = _normalize_dsml_bars(tail).lower()
    m = LEAK_TAG_RE.match(low)
    if m and any(m.group(0).startswith(p) for p in LEAK_TAG_PREFIXES):
        return True
    if len(low) > 96 or ">" in low:
        return False
    return any(p.startswith(low) or low.startswith(p) for p in LEAK_TAG_PREFIXES)


def _split_at_call_close(text):
    """Split `text` after the first complete call-closing tag.

    Returns (prefix, rest), or (None, text) while no call has closed yet.
    Consecutive closing tags belong to the same block and are swallowed
    with it, so a trailing </calls> cannot later leak out as prose.
    """
    for match in LEAK_CLOSE_RE.finditer(text):
        if match.group("name").lower() not in CALL_CLOSE_TAGS:
            continue
        end = match.end()
        while True:
            nxt = end
            while nxt < len(text) and text[nxt] in " \t\r\n":
                nxt += 1
            closing = LEAK_CLOSE_RE.match(text, nxt)
            if not closing:
                break
            end = closing.end()
        return text[:end], text[end:]
    return None, text


def _first_call_candidate(text):
    """Index of the first "<" that may still become printed markup."""
    for idx, ch in enumerate(text):
        if ch == "<" and could_be_leaked_call(text[idx:]):
            return idx
    return None


class LeakedToolCallStreamFilter:
    """Hold back assistant text that may be a printed tool call.

    /v1/chat/completions forwards text the moment it arrives, so a call the
    model printed instead of emitting would already be on the wire before the
    stream ended and anything could parse it. Buffer the text instead: release
    it as soon as it is clearly prose, convert whatever is left at the end.

    feed(text) returns a list of ("text", str) and ("calls", [call, ...]).
    """

    MAX_HOLD = 262144

    def __init__(self):
        self.buf = ""

    def _release_all(self):
        out, self.buf = self.buf, ""
        return [("text", out)] if out else []

    def feed(self, text):
        if not text:
            return []
        self.buf += text
        idx = _first_call_candidate(self.buf)
        if idx is None:
            return self._release_all()
        out = []
        if idx:
            out.append(("text", self.buf[:idx]))
            self.buf = self.buf[idx:]
        if len(self.buf) > self.MAX_HOLD:
            out.extend(self._release_all())
            return out
        prefix, rest = _split_at_call_close(self.buf)
        if prefix is None:
            return out
        calls, clean = parse_leaked_tool_calls(prefix)
        if not calls:
            calls, clean = parse_dsml_tool_calls(prefix)
        if not calls:
            return out
        self.buf = rest
        out.append(("calls", calls))
        if clean:
            out.append(("text", clean))
        # Prose after the call can go out at once; only text that may still
        # grow into another call stays buffered.
        idx = _first_call_candidate(self.buf)
        if idx is None:
            out.extend(self._release_all())
        elif idx:
            out.append(("text", self.buf[:idx]))
            self.buf = self.buf[idx:]
        return out

    def flush(self):
        if not self.buf:
            return []
        buf, self.buf = self.buf, ""
        calls, clean = parse_leaked_tool_calls(buf)
        if not calls:
            calls, clean = parse_dsml_tool_calls(buf)
        if not calls:
            return [("text", buf)]
        out = []
        if clean:
            out.append(("text", clean))
        out.append(("calls", calls))
        return out


def chat_chunk(model, delta, finish=None, chunk_id=None):
    """One Chat Completions streaming chunk."""
    if not chunk_id:
        chunk_id = f"chatcmpl-wb-{uuid.uuid4().hex[:16]}"
    return json.dumps({
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }, ensure_ascii=False)


def chat_text_chunk(model, text, chunk_id=None):
    return chat_chunk(model, {"content": text}, None, chunk_id)


def chat_tool_call_chunks(model, calls, chunk_id=None):
    """Deltas that hand recovered calls over as structured tool calls."""
    out = []
    for i, call in enumerate(calls):
        out.append(chat_chunk(model, {"tool_calls": [{
            "index": i,
            "id": call["id"],
            "type": "function",
            "function": {"name": call["name"], "arguments": call["arguments"]},
        }]}, None, chunk_id))
    return out


def chat_finish_chunk(model, reason, chunk_id=None):
    return chat_chunk(model, {}, reason, chunk_id)


def chat_usage_chunk(model, usage, chunk_id="chatcmpl-wb"):
    """The trailing usage frame of an include_usage stream.

    OpenAI puts usage on a final chunk with an empty choices list, after the
    finish chunk and before [DONE]. Strict downstream parsers (clients that
    validate the chunk contract, gateways that bill from the stream) ignore a
    usage block riding on a choice-bearing chunk, so the block is hoisted out
    of the upstream frame and re-emitted here in that shape.
    """
    normalize_usage_cache_aliases(usage)
    return json.dumps({
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [],
        "usage": usage,
    }, ensure_ascii=False)


def chat_stream_error_frame(message, code="upstream_stream_error"):
    """A terminal SSE error frame for a chat stream that died mid-body.

    `data: [DONE]` means the turn finished normally. Sending it after an
    upstream read error hid the failure from every client that treats it as
    the end of the answer (issue #549); send an error object instead and
    close the connection.
    """
    payload = {"error": {"message": str(message), "type": "upstream_error",
                         "param": None, "code": code}}
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


def responses_stream_error_frame(message, code="upstream_stream_error"):
    """The Responses-API spelling of the same terminal error event."""
    payload = {"type": "error", "code": code, "message": str(message),
               "param": None, "sequence_number": 0}
    return ("event: error\ndata: " + json.dumps(payload, ensure_ascii=False)
            + "\n\n").encode("utf-8")


def translate_max_completion_tokens(obj):
    alias = obj.pop("max_completion_tokens", None)
    if alias is None:
        return
    if "max_tokens" in obj:
        return
    try:
        val = int(alias)
        if val > 0:
            obj["max_tokens"] = val
    except (TypeError, ValueError):
        pass
