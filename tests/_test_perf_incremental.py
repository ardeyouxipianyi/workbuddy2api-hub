"""perf 的增量样本（T2）与 _tail_lines 的逐块读取（T3）。

/usage/perf 是面板轮询里唯一没有增量的聚合：每次 TTL 过期都重读日志末尾
sample 行、逐行 json.loads、把每一行的五个数值往全局与三层模型桶各 append
一次，再对每个桶排序求分位。真机 558ms/次，TTL 15 秒——面板开着就每 15 秒
付一次。本套件钉住三件事：

  1. _tail_lines 只对"本块 + 上一轮遗留的前缀片段"做 split（不重切已读的
     块）：用 open 计数桩证明读进来的字节数就是尾部那一段、split 的块数就是
     ceil(字节/chunk)，同时与旧实现的逐字复制（参考实现）在十来种边界日志上
     逐字节对拍——行集合与顺序一个字节都不许变。
  2. perf 的增量环：同一份日志按脚本演进（追加 / 半行写一半又写完整 / 空行 /
     坏 JSON / 没有 realm 的老行 / client_aborted / 截断 / 同尺寸重写 / 删除 /
     重建 / 超过 sample），每一步的输出都与参考实现（旧 _perf_stats_uncached
     的逐字复制）逐字节相同；sample_from / sample_capped / sampled / 三层桶的
     顺序与内容全部一致。
  3. 增量真的省了工作：0 新字节的 TTL 重算一个 json.loads 都不发生、也不重读
     日志；N 行新日志只解析 N 行（计数桩）。

唯一一处有意的口径收敛（下面单独钉住）：窗口预过滤跳过的行不解析，但旧实现
里"样本最老的那一行"无论窗口如何都会被解析，它的 at 就是 sample_from。增量
实现给被预过滤的行也留一份它声明的 at（完整行才走这条），所以两者只在"坏
JSON、以 } 结尾、带可解析的 {"at": <float>, 前缀、且是样本最老的一行"时不同：
新实现报那一行声明的 at，旧实现报下一行的 at；两者只差 sample_from 一个字段，
且那一行滑出样本后立刻重新一致。

无网络：日志在临时目录里现造。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="wb-perfinc-")
USAGE_DIR = os.path.join(_TMP, "usage")
ACCOUNTS_DIR = os.path.join(_TMP, "accounts")
os.makedirs(USAGE_DIR, exist_ok=True)
os.makedirs(ACCOUNTS_DIR, exist_ok=True)
os.environ["WB_PROXY_USAGE_DIR"] = USAGE_DIR
os.environ["ACCOUNTS_DIR"] = ACCOUNTS_DIR

import wb_proxy as P

import wb_usage as U

P.ACCOUNTS_DIR = ACCOUNTS_DIR
P.POOL = None                     # 归属只走行自己的字段/模型前缀，可确定
P.CURRENT_REALM = "intl"
os.environ["WB_USAGE_CACHE"] = "0"    # 本套件只测这两个读者，别让落盘掺进来

LOG = P.USAGE_LOG
PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


def first_diff(a, b, span=70):
    if a == b:
        return ""
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return "@%d: %r vs %r" % (i, a[i:i + span], b[i:i + span])
    return "长度 %d vs %d" % (len(a), len(b))


def text(obj):
    return json.dumps(obj, ensure_ascii=False)


# --------------------------------------------------------------------------
# 参考实现：改动前 _tail_lines 与 _perf_stats_uncached 的逐字复制
# --------------------------------------------------------------------------
def ref_tail_lines(path, max_lines, chunk=256 * 1024):
    lines = []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            buf = b""
            while pos > 0 and len(lines) < max_lines:
                step = min(chunk, pos)
                pos -= step
                fh.seek(pos)
                buf = fh.read(step) + buf
                parts = buf.split(b"\n")
                buf = parts[0]
                for raw in reversed(parts[1:]):
                    if not raw.strip():
                        continue
                    lines.append(raw)
                    if len(lines) >= max_lines:
                        break
            if len(lines) < max_lines and buf.strip():
                lines.append(buf)
    except FileNotFoundError:
        return []
    except Exception:
        return []
    lines.reverse()
    return lines


def ref_perf_stats(sample=5000, realm=None, since=None, until=None):
    """改动前 _perf_stats_uncached 的逐字复制（走参考 _tail_lines）。"""
    ttfts, gens, walls, rates, hits, tok_rates = [], [], [], [], [], []
    total = ok = err = aborted = 0
    m_buckets = {}
    mr_buckets = {}
    ma_buckets = {}
    rows = [raw.decode("utf-8", "replace") for raw in ref_tail_lines(LOG, sample)]
    sample_capped = len(rows) >= sample
    sample_from = None
    for line in rows:
        line = line.strip()
        if not line:
            continue
        if (since or until) and sample_from is not None and \
                U._line_outside_window(line, since or None, until or None):
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if sample_from is None:
            sample_from = r.get("at")
        if realm and not U.row_matches_realm(r, realm):
            continue
        at = r.get("at") or 0
        if since and at < since:
            continue
        if until and at > until:
            continue
        total += 1
        outcome = U.row_outcome(r)
        m_id = r.get("model") or "unknown"
        r_realm = U.row_realm(r)
        mb = m_buckets.setdefault(m_id, {"total": 0, "ok": 0, "err": 0, "aborted": 0,
                                         "ttfts": [], "gens": [], "walls": [],
                                         "tok_rates": [], "hits": []})
        rb = mr_buckets.setdefault(m_id, {}).setdefault(
            r_realm, {"total": 0, "ok": 0, "err": 0, "aborted": 0,
                      "ttfts": [], "gens": [], "walls": [],
                      "tok_rates": [], "hits": []})
        ab = ma_buckets.setdefault(m_id, {}).setdefault(r_realm, {}).setdefault(
            r.get("account") or "(unattributed)",
            {"total": 0, "ok": 0, "err": 0, "aborted": 0,
             "ttfts": [], "gens": [], "walls": [],
             "tok_rates": [], "hits": []})
        mb["total"] += 1
        rb["total"] += 1
        ab["total"] += 1
        if outcome == "client_aborted":
            aborted += 1
            for b in (mb, rb, ab):
                b["aborted"] += 1
                if r.get("elapsed_ms"):
                    b["walls"].append(r["elapsed_ms"])
            if r.get("elapsed_ms"):
                walls.append(r["elapsed_ms"])
            continue
        if outcome != "completed":
            err += 1
            for b in (mb, rb, ab):
                b["err"] += 1
                if r.get("elapsed_ms"):
                    b["walls"].append(r["elapsed_ms"])
            if r.get("elapsed_ms"):
                walls.append(r["elapsed_ms"])
            continue
        ok += 1
        for b in (mb, rb, ab):
            b["ok"] += 1
        if r.get("ttft_ms") is not None:
            ttfts.append(r["ttft_ms"])
            for b in (mb, rb, ab):
                b["ttfts"].append(r["ttft_ms"])
        if r.get("gen_ms") is not None:
            gens.append(r["gen_ms"])
            for b in (mb, rb, ab):
                b["gens"].append(r["gen_ms"])
        if r.get("elapsed_ms") is not None:
            walls.append(r["elapsed_ms"])
            for b in (mb, rb, ab):
                b["walls"].append(r["elapsed_ms"])
        if r.get("tokens_per_sec"):
            tok_rates.append(r["tokens_per_sec"])
            for b in (mb, rb, ab):
                b["tok_rates"].append(r["tokens_per_sec"])
        if r.get("cache_hit_pct") is not None:
            hits.append(r["cache_hit_pct"])
            for b in (mb, rb, ab):
                b["hits"].append(r["cache_hit_pct"])

    def block(vals):
        if not vals:
            return None
        return {
            "avg": round(sum(vals) / len(vals), 1),
            "p50": U._pct(vals, 50),
            "p90": U._pct(vals, 90),
            "p99": U._pct(vals, 99),
            "max": max(vals),
            "samples": len(vals),
        }

    return {
        "sampled": total,
        "sample_from": sample_from,
        "sample_capped": sample_capped,
        "success": ok,
        "errors": err,
        "client_aborted": aborted,
        "success_rate_pct": round(ok * 100.0 / (ok + err), 1) if (ok + err) else None,
        "ttft_ms": block(ttfts),
        "generation_ms": block(gens),
        "wall_ms": block(walls),
        "tokens_per_sec": block(tok_rates),
        "cache_hit_pct": block(hits),
        "by_model": {
            mid: {
                "requests": mb["total"],
                "errors": mb["err"],
                "client_aborted": mb.get("aborted", 0),
                "success_rate_pct": round(mb["ok"] * 100.0 / (mb["ok"] + mb["err"]), 1)
                                     if (mb["ok"] + mb["err"]) else None,
                "ttft_ms": block(mb["ttfts"]),
                "generation_ms": block(mb["gens"]),
                "wall_ms": block(mb["walls"]),
                "tokens_per_sec": block(mb["tok_rates"]),
                "cache_hit_pct": block(mb["hits"]),
            } for mid, mb in m_buckets.items()
        },
        "by_model_realm": {
            mid: {
                realm: {
                    "requests": rb["total"],
                    "errors": rb["err"],
                    "client_aborted": rb.get("aborted", 0),
                    "success_rate_pct": round(rb["ok"] * 100.0 / (rb["ok"] + rb["err"]), 1)
                                         if (rb["ok"] + rb["err"]) else None,
                    "ttft_ms": block(rb["ttfts"]),
                    "generation_ms": block(rb["gens"]),
                    "wall_ms": block(rb["walls"]),
                    "tokens_per_sec": block(rb["tok_rates"]),
                    "cache_hit_pct": block(rb["hits"]),
                } for realm, rb in realms.items()
            } for mid, realms in mr_buckets.items()
        },
        "by_model_acct": {
            mid: {
                realm: {
                    acct: {
                        "requests": ab["total"],
                        "errors": ab["err"],
                        "client_aborted": ab.get("aborted", 0),
                        "success_rate_pct": round(ab["ok"] * 100.0 / (ab["ok"] + ab["err"]), 1)
                                             if (ab["ok"] + ab["err"]) else None,
                        "ttft_ms": block(ab["ttfts"]),
                        "generation_ms": block(ab["gens"]),
                        "wall_ms": block(ab["walls"]),
                        "tokens_per_sec": block(ab["tok_rates"]),
                        "cache_hit_pct": block(ab["hits"]),
                    } for acct, ab in accts.items()
                } for realm, accts in realms.items()
            } for mid, realms in ma_buckets.items()
        }
    }


# --------------------------------------------------------------------------
# 数据工具
# --------------------------------------------------------------------------
_lt = time.localtime()
TODAY0 = time.mktime((_lt.tm_year, _lt.tm_mon, _lt.tm_mday, 0, 0, 0, 0, 0, -1))
ANCHOR = TODAY0 + 600.0            # 今天 00:10，测试窗口都围着它取


def row(at, model="m-a", account="u1", realm="intl", outcome="completed",
        elapsed=1000, ttft=300, gen=700, tps=90.0, hit=0.0):
    r = {"at": at, "iso": "x", "model": model, "stream": True,
         "outcome": outcome, "elapsed_ms": elapsed, "ttft_ms": ttft,
         "gen_ms": gen, "prompt_tokens": 100, "completion_tokens": 50,
         "total_tokens": 150, "tokens_per_sec": tps, "cache_hit_pct": hit,
         "account": account}
    if realm is not None:
        r["realm"] = realm
    return json.dumps(r, ensure_ascii=False)


def write_log(lines):
    with io.open(LOG, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(l + "\n" for l in lines))


def write_raw(data):
    with open(LOG, "wb") as fh:
        fh.write(data)


def append_lines(lines):
    with io.open(LOG, "a", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(l + "\n" for l in lines))


def reset_state():
    """清掉增量环与载荷缓存（本套件按用例从零起）。"""
    try:
        U._perf_state.clear()
    except AttributeError:
        pass
    U._perf_cache.clear()


def inc(sample, realm=None, rng=None, since=None, until=None):
    """走增量入口（ttl=0 每次都是 TTL 过期后的重建）。"""
    return U.perf_stats(sample, realm=realm, ttl=0, range=rng,
                        since=since, until=until)


def ref(sample, realm=None, rng=None, since=None, until=None):
    """参考实现，用同一套窗口解析。"""
    r = U.realm_scope(realm, P.CURRENT_REALM)
    lo, hi = U.range_window(rng, since, until)
    return ref_perf_stats(sample, r, since=lo, until=hi)


def compare(label, sample, realm=None, rng=None, since=None, until=None):
    a = text(inc(sample, realm, rng, since, until))
    b = text(ref(sample, realm, rng, since, until))
    ok = a == b
    check(label, ok, first_diff(b, a))
    return ok


# --------------------------------------------------------------------------
# [1] _tail_lines：与参考实现逐字节一致 + 读入的字节数有界（不重切已读块）
# --------------------------------------------------------------------------
print("[1] _tail_lines：逐字节对拍 + 读取字节数有界")

CASES = {
    # 正常行 + 末尾没有换行的完整行（_tail_lines 把它当一行）
    "no_final_newline": ("".join(row(ANCHOR + i * 60) + "\n" for i in range(20))[:-1]).encode(),
    # 末尾是写了一半的 JSON（没有换行）
    "partial_tail": ("".join(row(ANCHOR + i * 60) + "\n" for i in range(20))
                     + '{"at": 1, "mod').encode(),
    # 末尾换行之后只剩空白
    "blank_tail": ("".join(row(ANCHOR + i * 60) + "\n" for i in range(20)) + "  \n").encode(),
    # 空行与只有空白字符的行穿插
    "blank_lines": "".join(
        (row(ANCHOR + i * 60) + "\n") + ("\n" if i % 3 == 0 else "")
        + ("   \n" if i % 5 == 0 else "") for i in range(30)).encode(),
    # 整个文件没有一个换行
    "no_newline": row(ANCHOR).encode(),
    # 行数少于 sample
    "few_lines": "".join(row(ANCHOR + i * 60) + "\n" for i in range(5)).encode(),
    # 空文件
    "empty": b"",
    # 坏 JSON 混在窗口边界上
    "bad_json": "".join(
        (('{"at": %r, "broken\n' % (ANCHOR + i * 60)) if i % 4 == 0
         else (row(ANCHOR + i * 60) + "\n")) for i in range(40)).encode(),
    # 重复 at / 整数 at / at 不是第一个键
    "tricky_at": (
        '{"at": %r, "x": 1, "at": %r, "model": "m-b", "realm": "intl", '
        '"outcome": "completed", "elapsed_ms": 800, "ttft_ms": 100, '
        '"gen_ms": 700, "tokens_per_sec": 70.0, "cache_hit_pct": 0.0, '
        '"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}\n'
        % (ANCHOR, ANCHOR + 300)
        + '{"at": %d, "model": "m-c", "realm": "cn", "outcome": "completed", '
          '"elapsed_ms": 700, "ttft_ms": 90, "gen_ms": 600, "tokens_per_sec": 60.0, '
          '"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, '
          '"cache_hit_pct": 0.0}\n' % int(ANCHOR + 700)
        + json.dumps({"model": "m-a", "at": ANCHOR + 100, "realm": "intl",
                      "outcome": "completed", "elapsed_ms": 900, "ttft_ms": 200,
                      "gen_ms": 700, "tokens_per_sec": 80.0, "cache_hit_pct": 0.0,
                      "prompt_tokens": 10, "completion_tokens": 5,
                      "total_tokens": 15}, ensure_ascii=False) + "\n"
        + row(ANCHOR + 200) + "\n").encode(),
}
# 一行比 chunk 还长：跨块边界的那一行必须照样拼起来
CASES["huge_line"] = (row(ANCHOR) + "\n"
                      + json.dumps({"at": ANCHOR + 1, "pad": "x" * (300 * 1024)},
                                   ensure_ascii=False) + "\n"
                      + row(ANCHOR + 2) + "\n").encode()
# 20000+ 行（跨很多块）
CASES["big"] = "".join(row(ANCHOR + i * 3, "m-%d" % (i % 5), "u%d" % (i % 3),
                           "intl" if i % 2 else "cn") + "\n"
                      for i in range(25000)).encode()

ok_all = True
for name in sorted(CASES):
    write_raw(CASES[name])
    for sample in (1, 3, 10, 5000, 20000):
        got = U._tail_lines(LOG, sample)
        want = ref_tail_lines(LOG, sample)
        if got != want:
            ok_all = False
            print("  [FAIL] %s sample=%s: %s" % (name, sample, first_diff(
                repr(want)[:200], repr(got)[:200])))
check("_tail_lines 与参考实现逐字节一致（%d 种日志 × 5 个 sample）"
      % len(CASES), ok_all)

# 读入的字节数有界：open 计数桩只数 read()，证明没有把已读的块再切一遍
_real_open = open


class ReadCounter(object):
    def __init__(self, fh):
        self._fh = fh
        self.reads = 0
        self.bytes = 0

    def __getattr__(self, name):
        return getattr(self._fh, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._fh.__exit__(*exc)

    def read(self, *args):
        data = self._fh.read(*args)
        self.reads += 1
        self.bytes += len(data)
        return data


def tail_with_counter(path, sample, chunk=256 * 1024):
    counter = {}

    def fake_open(*args, **kwargs):
        counter["fh"] = ReadCounter(_real_open(*args, **kwargs))
        return counter["fh"]

    U.open = fake_open
    try:
        lines = U._tail_lines(path, sample, chunk=chunk)
    finally:
        del U.open
    return lines, counter["fh"].reads, counter["fh"].bytes


write_raw(CASES["big"])
size = os.path.getsize(LOG)
lines, reads, nbytes = tail_with_counter(LOG, 20000)
span = sum(len(l) + 1 for l in lines)
check("sample=20000：读进来的字节数 ≤ 尾部那一段 + 一个 chunk 余量",
      0 < nbytes <= span + 2 * 256 * 1024, (nbytes, span))
check("sample=20000：read() 次数 = ceil(读入字节/chunk)",
      reads == -(-nbytes // (256 * 1024)), (reads, nbytes))
lines5, reads5, nbytes5 = tail_with_counter(LOG, 5000)
check("sample=5000 读入的字节数随 sample 线性缩小（不是整段重读）",
      nbytes5 < nbytes, (nbytes5, nbytes))
check("sample=5000 的行数与参考一致", len(lines5) == 5000, len(lines5))

# 计时只打印不断言（CI 机器负载不可控；结构性证据在上面两条）
t0 = time.perf_counter()
U._tail_lines(LOG, 20000)
t20 = (time.perf_counter() - t0) * 1000.0
t0 = time.perf_counter()
U._tail_lines(LOG, 5000)
t5 = (time.perf_counter() - t0) * 1000.0
print("    计时参考（25000 行日志）：sample=5000 %.2fms，sample=20000 %.2fms，"
      "比值 %.2f（读入的字节/行数就是 4 倍关系，线性读取器不可能更小）"
      % (t5, t20, (t20 / t5) if t5 else 0))

# --------------------------------------------------------------------------
# [2] 冷路径：_perf_stats_uncached（重构后的渲染）与参考实现逐字节一致
# --------------------------------------------------------------------------
print()
print("[2] 冷路径渲染与参考实现逐字节一致")

write_raw(CASES["tricky_at"])
for sample, realm, rng, since, until in (
        (5000, None, None, None, None),
        (5000, "all", None, None, None),
        (3, "cn", None, None, None),
        (5000, None, "today", None, None),
        (5000, None, "custom", ANCHOR + 100, ANCHOR + 600),
        (10, "intl", "custom", ANCHOR + 150, None)):
    reset_state()
    a = text(U._perf_stats_uncached(sample, U.realm_scope(realm, P.CURRENT_REALM),
                                    since=U.range_window(rng, since, until)[0],
                                    until=U.range_window(rng, since, until)[1]))
    b = text(ref(sample, realm, rng, since, until))
    check("冷路径对拍 sample=%s realm=%s range=%s" % (sample, realm, rng), a == b,
          first_diff(b, a))

# --------------------------------------------------------------------------
# [3] 增量环：按脚本演进，每一步都与参考实现逐字节一致
# --------------------------------------------------------------------------
print()
print("[3] 增量环：逐步演进对拍（追加 / 半行 / 截断 / 重写 / 删除）")

KEYS = [
    (5000, "all", None, None, None),
    (5000, "intl", "today", None, None),
    (5, "all", None, None, None),
    (3, "all", "custom", ANCHOR + 100, ANCHOR + 3600),
    (10, "cn", None, None, None),
    (5000, "all", "custom", None, ANCHOR + 700),
]
base = [row(ANCHOR + i * 60, "m-%d" % (i % 3), "u%d" % (i % 4),
            "intl" if i % 2 else "cn",
            "completed" if i % 7 else "failed",
            elapsed=None if i % 11 == 0 else 1000 + i,
            ttft=None if i % 5 == 0 else 200 + i,
            gen=None if i % 9 == 0 else 500 + i,
            tps=None if i % 13 == 0 else 80.0 + i,
            hit=None if i % 6 == 0 else float(i % 100))
        for i in range(30)]


def step_compare(label):
    ok = True
    for sample, realm, rng, since, until in KEYS:
        a = text(inc(sample, realm, rng, since, until))
        b = text(ref(sample, realm, rng, since, until))
        if a != b:
            ok = False
            print("    %s | sample=%s realm=%s range=%s\n      %s"
                  % (label, sample, realm, rng, first_diff(b, a)))
    return ok


reset_state()
ok_steps = True
write_log(base)
ok_steps = step_compare("01 初始 30 行") and ok_steps
append_lines([row(ANCHOR + 2000 + i * 60, "m-a", "u1") for i in range(5)])
ok_steps = step_compare("02 追加 5 行") and ok_steps
# 半行：追加到文件尾（没有换行），下一次再写完整
with io.open(LOG, "a", encoding="utf-8", newline="\n") as fh:
    fh.write('{"at": %r, "model": "m-b"' % (ANCHOR + 3000))
ok_steps = step_compare("03 追加半行") and ok_steps
with io.open(LOG, "a", encoding="utf-8", newline="\n") as fh:
    fh.write(', "realm": "cn", "outcome": "completed", "elapsed_ms": 900, '
             '"ttft_ms": 300, "gen_ms": 600, "tokens_per_sec": 70.0, '
             '"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, '
             '"cache_hit_pct": 1.0}\n')
append_lines([row(ANCHOR + 3100 + i * 60, "m-c", "u2", "intl") for i in range(3)])
ok_steps = step_compare("04 半行写完整 + 3 行") and ok_steps
with io.open(LOG, "a", encoding="utf-8", newline="\n") as fh:
    fh.write("   \n")
ok_steps = step_compare("05 末尾只剩空白") and ok_steps
with io.open(LOG, "a", encoding="utf-8", newline="\n") as fh:
    fh.write("".join(l + "\n" for l in [row(ANCHOR + 4000, "m-a", "u1"), "",
                                        "  ", row(ANCHOR + 4060, "m-b", "u2", "cn")]))
ok_steps = step_compare("06 追加 2 行 + 空行 + 空白行") and ok_steps
append_lines(['{"at": %r, "broken' % (ANCHOR + 5000)])
ok_steps = step_compare("07 追加坏 JSON 行") and ok_steps
append_lines([json.dumps({"at": ANCHOR + 5060, "model": "gpt-5.6-luna",
                          "error": True, "elapsed_ms": 500}, ensure_ascii=False)])
ok_steps = step_compare("08 追加没有 realm 的老行") and ok_steps
append_lines([row(ANCHOR + 5120, "m-a", "u3", "intl", "client_aborted",
                  elapsed=120, ttft=None, gen=None, tps=None, hit=None)])
ok_steps = step_compare("09 追加 client_aborted 行") and ok_steps
write_log(base[:20])
ok_steps = step_compare("10 截断到前 20 行（位置校验作废→重建）") and ok_steps
append_lines([row(ANCHOR + 6000 + i * 60, "m-b", "u2") for i in range(4)])
ok_steps = step_compare("11 截断后再追加 4 行") and ok_steps
# 同尺寸重写：把尾部的 realm 值改一个字母，字节数不变
data = io.open(LOG, encoding="utf-8").read()
pos = data.rfind('"realm": "intl"')
needle = '"realm": "intl"'
check("同尺寸重写点落在文件尾部（tail 签名看得到）",
      pos >= 0 and len(data) - pos < 64, (pos, len(data)))
io.open(LOG, "w", encoding="utf-8", newline="\n").write(
    data[:pos] + '"realm": "intx"' + data[pos + len(needle):])
ok_steps = step_compare("12 同尺寸重写（tail 签名挡住旧进度）") and ok_steps
os.unlink(LOG)
ok_steps = step_compare("13 删除日志") and ok_steps
write_log(base[:12])
ok_steps = step_compare("14 重建 12 行") and ok_steps
append_lines([row(ANCHOR + 7000 + i * 60, "m-%d" % (i % 2), "u1") for i in range(8)])
ok_steps = step_compare("15 小 sample 溢出（滑出旧行）") and ok_steps
append_lines([row(ANCHOR + 8000 + i * 60, "m-%d" % (i % 3), "u%d" % (i % 2),
                  "intl" if i % 2 else "cn") for i in range(40)])
ok_steps = step_compare("16 追加 40 行（超过 sample=3/5/10）") and ok_steps
# 尾部无换行的完整行（_tail_lines 把它算一行；offset 停在它前面）
data = io.open(LOG, encoding="utf-8", newline="").read()
io.open(LOG, "w", encoding="utf-8", newline="").write(data.rstrip("\n"))
ok_steps = step_compare("17 尾部无换行的完整行") and ok_steps
with io.open(LOG, "a", encoding="utf-8", newline="\n") as fh:
    fh.write("\n")
ok_steps = step_compare("18 把无换行的尾巴补上换行") and ok_steps
append_lines([row(ANCHOR + 9000 + i * 60, "m-e", "u9") for i in range(6)])
ok_steps = step_compare("19 补换行后再追加") and ok_steps
check("增量环 19 步演进、每步 6 个 (sample,realm,窗口) 组合全部与参考逐字节一致",
      ok_steps)

# --------------------------------------------------------------------------
# [4] 计数桩：增量真的只做新行、0 新字节什么都不做
# --------------------------------------------------------------------------
print()
print("[4] 计数桩：0 新字节不解析不重读；N 行新日志只解析 N 行")


class JsonCounter(object):
    def __init__(self):
        self.loads = 0
        self._real = json.loads

    def hook(self, *args, **kwargs):
        self.loads += 1
        return self._real(*args, **kwargs)


def count_loads(fn):
    counter = JsonCounter()
    old = json.loads
    P.json.loads = counter.hook
    try:
        fn()
    finally:
        P.json.loads = old
    return counter.loads


reset_state()
write_log(base)
inc(5000, "all")
U._perf_cache.clear()
n0 = count_loads(lambda: inc(5000, "all"))
check("0 新字节的 TTL 重算：一个 json.loads 都不发生", n0 == 0, n0)
# 自检：桩是活的
append_lines([row(ANCHOR + 9500, "m-a", "u1")])
U._perf_cache.clear()
n1 = count_loads(lambda: inc(5000, "all"))
check("计数桩自检：追加 1 行时恰好解析 1 行", n1 == 1, n1)
append_lines([row(ANCHOR + 9560 + i * 60, "m-b", "u2") for i in range(5)])
U._perf_cache.clear()
n5 = count_loads(lambda: inc(5000, "all"))
check("追加 5 行只解析 5 行（不是整段 5000 行）", n5 == 5, n5)
# 0 新字节也不重读日志
_real_open2 = open


def count_reads(fn):
    box = {}

    def fake_open(*args, **kwargs):
        box["fh"] = ReadCounter(_real_open2(*args, **kwargs))
        return box["fh"]

    U.open = fake_open
    try:
        fn()
    finally:
        del U.open
    return box["fh"].reads, box["fh"].bytes


U._perf_cache.clear()
reads, nbytes = count_reads(lambda: inc(5000, "all"))
check("0 新字节的 TTL 重算：日志只被读 64B 的 tail 签名（不重读整段）",
      reads <= 2 and nbytes <= 128, (reads, nbytes))
U._perf_cache.clear()
reads, nbytes = count_reads(lambda: inc(5000, "all"))
check("样本没变时连渲染都省掉（载荷直接复用）",
      U._perf_state[(5000, "all", -1, -1)]["dirty"] is False)

# --------------------------------------------------------------------------
# [5] 口径钉住：sample_from / sample_capped / 桶顺序 / TTL
# --------------------------------------------------------------------------
print()
print("[5] 口径钉住")

reset_state()
lines = []
for i in range(12):
    lines.append(row(ANCHOR + i * 60, "m-%d" % (i % 3), "u%d" % (i % 4),
                     "intl" if i % 2 else "cn"))
write_log(lines)
reset_state()
small = inc(3, "all")
big = inc(5000, "all")
check("sample=3：sample_capped 为真", small["sample_capped"] is True,
      small["sample_capped"])
check("sample=5000：sample_capped 为假", big["sample_capped"] is False,
      big["sample_capped"])
check("sample=3 的 sample_from = 样本最老一行的 at",
      small["sample_from"] == ANCHOR + 9 * 60, small["sample_from"])
check("sample=3 的 sampled 就是 3", small["sampled"] == 3, small["sampled"])
check("sample=5000 的 sampled 是全部 12 行", big["sampled"] == 12, big["sampled"])
check("sample_from 与参考一致", small["sample_from"] == ref(3, "all")["sample_from"])
check("三层桶的键顺序与参考一致",
      (list(big["by_model"]) == list(ref(5000, "all")["by_model"])
       and list(big["by_model_realm"]) == list(ref(5000, "all")["by_model_realm"])
       and list(big["by_model_acct"]) == list(ref(5000, "all")["by_model_acct"])),
      (list(big["by_model"]), list(ref(5000, "all")["by_model"])))

# 桶删空后重现：位置必须回到"样本里第一次出现"的地方（重建时的顺序）
reset_state()
write_log([row(ANCHOR, "m-x", "u1"), row(ANCHOR + 60, "m-y", "u1"),
           row(ANCHOR + 120, "m-x", "u1"), row(ANCHOR + 180, "m-z", "u1")])
inc(3, "all")                       # 样本 = [m-y, m-x, m-z]
append_lines([row(ANCHOR + 240, "m-x", "u1")])   # m-y 滑出 → 样本 = [m-x, m-z, m-x]
got = inc(3, "all")
want = ref(3, "all")
check("滑出后桶顺序与重建一致（不是简单挪到末尾）",
      list(got["by_model"]) == list(want["by_model"]),
      (list(got["by_model"]), list(want["by_model"])))

# TTL：默认 TTL 内命中缓存，过期后重建并跟上新行
reset_state()
U._perf_cache.clear()
U._STATS_TTL = 60.0
first = U.perf_stats(5000, realm="all")
append_lines([row(ANCHOR + 5000, "m-q", "u1")])
check("TTL 内命中缓存（不重算）", U.perf_stats(5000, realm="all") == first)
U._STATS_TTL = 0.0
check("TTL 过期后重建并跟上新行",
      U.perf_stats(5000, realm="all")["sampled"] == ref(5000, "all")["sampled"],
      U.perf_stats(5000, realm="all")["sampled"])
# ETag 仍然只认缓存条目的构建时刻
U._perf_cache.clear()
U._STATS_TTL = 60.0
U.perf_stats(5000, realm="all")
tag1 = U.perf_stats_etag(5000, realm="all")
check("缓存命中时 ETag 不变", U.perf_stats_etag(5000, realm="all") == tag1,
      tag1)
U._STATS_TTL = 0.0
# Windows 的 time.time() 粒度约 15.6ms：两次重建若落在同一个 tick，
# _cache_etag 的微秒戳会相同、tag 也就「没变」（CI windows-3.12 腿实测过）。
# 睡过一个 tick，保证测的是「重建换了构建时刻」这件事本身。
time.sleep(0.05)
U.perf_stats(5000, realm="all")
tag2 = U.perf_stats_etag(5000, realm="all")
check("重建后 ETag 变、缓存命中时不变", tag1 and tag2 and tag1 != tag2,
      (tag1, tag2))
U._STATS_TTL = float(os.environ.get("WB_STATS_TTL", 15))

# 状态数有上限（用户来回切窗口不会无限堆积）
reset_state()
write_log(base)
for i in range(12):
    inc(5000, "all", "custom", ANCHOR + i * 10, ANCHOR + 4000)
check("增量状态数不超过上限", len(U._perf_state) <= U._PERF_STATE_MAX,
      len(U._perf_state))
check("淘汰后仍与参考一致",
      text(inc(5000, "all", "custom", ANCHOR, ANCHOR + 4000))
      == text(ref(5000, "all", "custom", ANCHOR, ANCHOR + 4000)))

# --------------------------------------------------------------------------
# [6] 有意收敛：坏 JSON + 可解析 at 前缀 + 窗外 + 样本最老行 → sample_from
# --------------------------------------------------------------------------
print()
print("[6] 有意收敛（单列）：坏 JSON 的 sample_from")
lo, hi = ANCHOR + 50, ANCHOR + 1000
BROKEN = '{"at": %r, "x": }' % (ANCHOR - 5000)

# 冷路径：坏行就是样本第一行 → 旧实现也不预过滤它（开关还没打开），两边一致
reset_state()
write_log([BROKEN, row(ANCHOR + 60, "m-a", "u1"), row(ANCHOR + 120, "m-a", "u1")])
check("冷路径：坏 JSON 行是样本第一行时两边一致（都不预过滤它）",
      text(inc(3, "all", "custom", lo, hi)) == text(ref(3, "all", "custom", lo, hi)))

# 增量路径：先折进带 at 的行（开关打开），坏行被预过滤；随后旧行滑出，
# 坏行成了样本最老的一行——旧实现这时会解析它并失败，转而取下一行的 at。
reset_state()
write_log([row(ANCHOR + 60, "m-a", "u1"), row(ANCHOR + 120, "m-a", "u1")])
inc(3, "all", "custom", lo, hi)
append_lines([BROKEN])
inc(3, "all", "custom", lo, hi)          # 样本 = [A, B, 坏行]
append_lines([row(ANCHOR + 180, "m-a", "u1")])
inc(3, "all", "custom", lo, hi)          # 样本 = [B, 坏行, C]
append_lines([row(ANCHOR + 240, "m-a", "u1")])
got = inc(3, "all", "custom", lo, hi)    # 样本 = [坏行, C, D]
want = ref(3, "all", "custom", lo, hi)
check("坏行被预过滤后又成了样本最老行：sample_from 取它声明的 at（收敛点）",
      got["sample_from"] == ANCHOR - 5000 and want["sample_from"] == ANCHOR + 180,
      (got["sample_from"], want["sample_from"]))
check("收敛只影响 sample_from 一个字段（其余逐字节一致）",
      {k: v for k, v in got.items() if k != "sample_from"}
      == {k: v for k, v in want.items() if k != "sample_from"})
append_lines([row(ANCHOR + 300, "m-a", "u1")])
check("那一行滑出样本后立刻重新一致",
      text(inc(3, "all", "custom", lo, hi)) == text(ref(3, "all", "custom", lo, hi)))

shutil.rmtree(_TMP, ignore_errors=True)
print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
