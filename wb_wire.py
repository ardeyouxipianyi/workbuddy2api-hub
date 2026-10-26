"""Wire-format primitives shared by every protocol path.

Reading upstream SSE streams, normalising the usage block's cache-hit aliases,
rough token estimates and response ids. Pure functions with no gateway state:
the request paths (wb_proxy), the message repairs (wb_transforms) and the
protocol translators (wb_translate) all build on these.
"""

import http.client
import re
import uuid


def normalize_usage_cache_aliases(usage):
    """Write the best cache-hit value into every alias.

    Some responses carry the real hit in prompt_tokens_details.cached_tokens
    while also emitting cache_read_input_tokens: 0 / cached_tokens: 0
    compatibility aliases; strict downstream parsers may prefer the zero
    aliases and lose the hit. Mutates and returns the usage dict.
    """
    if not isinstance(usage, dict):
        return usage
    best = _best_cached_tokens(usage)
    if best <= 0:
        return usage
    usage["cache_read_input_tokens"] = best
    usage["cached_tokens"] = best
    usage["prompt_cache_hit_tokens"] = best
    prompt_details = dict(usage.get("prompt_tokens_details") or {})
    prompt_details["cached_tokens"] = best
    usage["prompt_tokens_details"] = prompt_details
    # Also ensure input_tokens_details carries it when present
    if isinstance(usage.get("input_tokens_details"), dict):
        input_details = dict(usage["input_tokens_details"])
        input_details["cached_tokens"] = best
        usage["input_tokens_details"] = input_details
    return usage
    if isinstance(usage.get("input_tokens_details"), dict):
        input_details = dict(usage["input_tokens_details"])
        input_details["cached_tokens"] = best
        usage["input_tokens_details"] = input_details
    return usage


def _best_cached_tokens(usage):
    """Best cache-hit value across every alias the upstreams emit (E3).

    Order mirrors the panel: prompt_tokens_details.cached_tokens >
    prompt_cache_hit_tokens > cache_read_input_tokens > cached_tokens >
    input_tokens_details.cached_tokens > completion_tokens_details.
    First positive value wins; zero aliases never shadow a real hit.
    """
    if not isinstance(usage, dict):
        return 0
    prompt_details = usage.get("prompt_tokens_details") or {}
    input_details = usage.get("input_tokens_details") or {}
    details = usage.get("completion_tokens_details") or {}
    for value in (prompt_details.get("cached_tokens"),
                  usage.get("prompt_cache_hit_tokens"),
                  usage.get("cache_read_input_tokens"),
                  usage.get("cached_tokens"),
                  input_details.get("cached_tokens"),
                  details.get("cached_tokens")):
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            continue
        if number > 0:
            # Token counts must stay integers: the Codex client parses
            # response.completed strictly and rejects 123.0 (invalid number).
            return int(number)
    return 0


def strip_data_prefix(line):
    line = line.strip()
    # SSE comment / heartbeat / keepalive / empty line
    if not line or line.startswith(":"):
        return ""
    while line.startswith("data:"):
        line = line[5:].strip()
    # Handle possible "data: : heartbeat"
    if not line or line.startswith(":"):
        return ""
    return line


# CJK 计数走单遍 C 扫描：先 sub 掉 CJK 再按长度差计数，与原来的逐字符 Python
# 循环逐值相等（64KB ASCII 实测 18.7ms -> 1.25ms）。别换 findall，实测更慢。
# 调用点：aggregate_stream、_chat_stream_response 的 usage 兜底、
# /v1/messages/count_tokens。
CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
def estimate_tokens(text):
    if not text:
        return 0
    if not isinstance(text, str):
        text = str(text)
    cjk = len(text) - len(CJK_RE.sub("", text))
    other = len(text) - cjk
    return cjk + max(1, int(other / 3.6)) if text else 0

# 每次向流式响应要多少字节。64 KiB 远大于一条 SSE 事件（典型几百字节），
# 又远小于 http.client 单次 read 的上限，按它切块能把「一行一次 syscall」
# 摊成「一块一次」，同时不会让首字节延迟变差（见 iter_sse_lines）。
SSE_READ_BLOCK = 64 * 1024

def iter_sse_lines(source, block_size=SSE_READ_BLOCK, before_read=None):
    """按块读取流式响应并逐行产出，把逐行 read 的 syscall 开销摊掉。

    直接 `for line in resp` 迭代 http.client.HTTPResponse 时，每一行都要走
    一次 read() syscall，而每次 syscall 都要释放并重新抢回 GIL。一轮流式回复
    动辄几千条事件，于是进程大部分时间花在二十来个 handler 线程之间来回交接
    GIL 上，而不是在干活。4 vCPU 真机实测（v1.7.2，15~20 req/s）：10 秒内
    106 214 次 futex，占全部 syscall 时间的 71%，其中约三分之二来自这条逐行
    读取路径。

    read1() 每次最多一次底层 syscall，所以按块取回来再在内存里切行，能把
    syscall 数降一到两个数量级。延迟不变：read1() 一有字节就返回，完整的行
    立刻产出；只有跨块的那一行需要等下一块。

    产出的行**带换行符**，与直接迭代 response 的形状完全一致，调用方一行都
    不用改。没有 read1 的对象（列表、文本模式的文件……）退回普通迭代。

    `before_read` 在每个**可能阻塞的 read1 之前**调一次，调用方可以在这里把
    攒下的写出掉：一个读取块里的事件因此合成一次 write，而下一个字节一到就
    立刻产出——延迟与逐块写完全一样，只是不再一个 chunk 一次 sendall。
    """
    read1 = getattr(source, "read1", None)
    if read1 is None:
        for line in source:
            if before_read is not None:
                before_read()
            yield line
        return
    buf = b""
    while True:
        if before_read is not None:
            before_read()
        chunk = read1(block_size)
        if not chunk:
            # http.client.read1() returns b"" when the peer closes early and
            # does not raise for a Content-Length response; without this check
            # a truncated stream looks like a clean EOF and the retry path in
            # _chat_stream_response never sees it (issue #522).
            remaining = getattr(source, "length", 0) or 0
            if remaining > 0:
                raise http.client.IncompleteRead(b"", remaining)
            break
        buf += chunk
        if b"\n" not in buf:
            continue
        head, _, buf = buf.rpartition(b"\n")
        for line in head.split(b"\n"):
            yield line + b"\n"
    if buf:
        # 上游没以换行收尾时，最后一行照旧要交出去（迭代 response 也是这个形状）。
        yield buf


def _new_id(prefix):
    return prefix + uuid.uuid4().hex
