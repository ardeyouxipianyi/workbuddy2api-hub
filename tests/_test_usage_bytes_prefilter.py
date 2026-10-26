"""未对齐窗口的 bytes 级预过滤（_line_outside_window_bytes）。

未对齐窗口（自定义区间）走 _scan_usage_snapshot_window 一族：整段读日志、
逐行 decode + strip，再做窗口判定（_line_outside_window）。窗外的行只是不折，
decode 照付；而窄窗口在几万行的日志上把绝大部分行都判在窗外，decode 就是这
条路径上唯一能整段省掉的开销。

本改动把同一道判定搬到**未解码的行**上（_line_outside_window_bytes）：能在
字节上读准、且整行 ASCII（decode 必然成功）的窗外行连 decode 都不再付；读
不准的行——非首键 at、重复 at、整数 token、转义引号、坏字节、非 ASCII——一律
返回 False，照旧解码后交给文本判定。保守语义一条不改：字节版说"窗外"的行，
文本版也必须说"窗外"（反过来不成立只许出现在"字节版认不出"那一侧）。

套件钉四件事：

  1. 判定层：TRICKY 形状逐条对拍两条判定（ASCII 形状上必须逐条相同；非 ASCII
     形状只许更保守）；
  2. 扫描层：六个窗口读取器 + 公开端点在"预过滤开/关"（关 = monkeypatch 成恒
     False，等价于改动前的路径）下输出逐字节一致；
  3. 计数桩：跳过的行真的不再 decode（bytes 子类记 decode 次数），
     json.loads 次数一个不变，预过滤命中数就是"省下的 decode 数"；
  4. 坏字节 / 半行 / 跨块行 / 窗口边界：坏行照旧让整趟扫描停在原地（它后面的
     行两个模式都不折），半行留给下一趟，跨块的长行照折，恰好落在 lo/hi 上的
     行照留。

No network: the log is synthesised in a temp directory.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-bytesgate-")
USAGE_DIR = os.path.join(_TMP, "usage")
ACCOUNTS_DIR = os.path.join(_TMP, "accounts")
os.makedirs(USAGE_DIR, exist_ok=True)
os.makedirs(ACCOUNTS_DIR, exist_ok=True)
os.environ["WB_PROXY_USAGE_DIR"] = USAGE_DIR
os.environ["ACCOUNTS_DIR"] = ACCOUNTS_DIR
# 不写 checkpoint：本套件只测读取路径，别让落盘状态掺进来。
os.environ["WB_USAGE_CACHE"] = "0"

import wb_proxy as P

import wb_usage as U

P.ACCOUNTS_DIR = ACCOUNTS_DIR
P.POOL = None                     # 归属只走行自己的字段，可确定
P.CURRENT_REALM = "intl"

USAGE_LOG = P.USAGE_LOG
PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


# --------------------------------------------------------------------------
# 数据：一个不落格点的窄窗口（T13 的目标形状），外加各种手改行
# --------------------------------------------------------------------------
# lo 既不在本地午夜/整点格点上，也不在分钟格点上：窗口一定会走整段扫描，
# 而不是日桶/小时桶/分钟环的切片。
LO = 1790000000.0
HI = LO + 600.0


def row_bytes(at, model="m-a", account="u1", realm="intl", outcome="completed",
              note=None):
    r = {"at": at, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at)),
         "model": model, "stream": True, "outcome": outcome,
         "elapsed_ms": 1200, "ttft_ms": 400, "gen_ms": 800,
         "prompt_tokens": 100, "completion_tokens": 50,
         "reasoning_tokens": 0, "cached_tokens": 0, "total_tokens": 150,
         "account": account, "realm": realm, "tokens_per_sec": 120.0,
         "cache_hit_pct": 0.0}
    if note is not None:
        r["note"] = note
    return json.dumps(r, ensure_ascii=False).encode("utf-8") + b"\n"


# 判定层样本：每条 (标签, 行, 期望的 (bytes 版, 文本版))。
# 文本版对坏字节行给 "decode-error"（它在 decode 那一步就抛了，根本没到判定）。
SHAPES = [
    ("规范行 · 窗外",
     row_bytes(LO - 3600), (True, True)),
    ("规范行 · 窗内",
     row_bytes(LO + 60), (False, False)),
    ("at 恰好 = lo（下界归窗口）",
     row_bytes(LO), (False, False)),
    ("at 恰好 = hi（上界归窗口）",
     row_bytes(HI), (False, False)),
    ("at 差一丁点在 lo 之下",
     row_bytes(LO - 1e-6), (True, True)),
    ("at 差一丁点在 hi 之上",
     row_bytes(HI + 1e-6), (True, True)),
    ("at 不是第一个键",
     b'{"model": "m-a", "at": 1789999000.5, "realm": "intl"}\n', (False, False)),
    ("重复 at：第一个窗外、最后一个窗内",
     b'{"at": 1789999000.5, "x": 1, "at": 1790000060.0, "model": "m-b"}\n',
     (False, False)),
    ("重复 at：两个都在窗外",
     b'{"at": 1789999000.5, "x": 1, "at": 1789999100.5, "model": "m-b"}\n',
     (False, False)),
    ("整数 at",
     b'{"at": 1789999000, "model": "m-a", "realm": "intl"}\n', (False, False)),
    ("负整数 at",
     b'{"at": -5, "model": "m-a", "realm": "intl"}\n', (False, False)),
    ("字符串里的转义引号（拼不出 \"at\" 的收尾引号）",
     b'{"at": 1789999000.5, "note": "say \\"at\\": \\"x\\"", "model": "m-a"}\n',
     (True, True)),
    ("嵌套对象里的第二个 at 键（真的出现 \"at\"）",
     b'{"at": 1789999000.5, "x": {"at": 1789999100.5}, "model": "m-a"}\n',
     (False, False)),
    ("值后没有逗号",
     b'{"at": 1789999000.5}\n', (False, False)),
    ("形状对但 JSON 是坏的（窗外）",
     b'{"at": 1789999000.5, "broken\n', (True, True)),
    ("at 是字符串字面量",
     b'{"at": "1789999000.5", "model": "m-a"}\n', (False, False)),
    ("指数记法（小写 e）",
     b'{"at": 1.789999e9, "model": "m-a"}\n', (True, True)),
    ("指数记法（大写 E）",
     b'{"at": 1.789999E9, "model": "m-a"}\n', (True, True)),
    ("CRLF 行尾（窗外）",
     b'{"at": 1789999000.5, "model": "m-a"}\r\n', (True, True)),
    ("坏 UTF-8 字节（形状是窗外）",
     b'{"at": 1789999000.5, "model": "\xff\xfe bad"}\n', (False, "decode-error")),
    ("合法非 ASCII（窗外）",
     row_bytes(LO - 120, model="\u4e2d\u6587\u6a21\u578b"), (False, True)),
    ("合法非 ASCII（窗内）",
     row_bytes(LO + 180, model="\u4e2d\u6587\u6a21\u578b"), (False, False)),
    ("前缀前有空格（窗外）",
     b'  {"at": 1789999000.5, "model": "m-a"}\n', (False, True)),
    ("前缀前有空格（窗内）",
     b'  {"at": 1790000060.0, "model": "m-a"}\n', (False, False)),
    ("at 值里带下划线（窗外）",
     b'{"at": 1_789999000.5, "model": "m-a"}\n', (True, True)),
    ("超长行（跨读块，窗外）",
     row_bytes(LO - 60, note="x" * 9000), (True, True)),
    ("超长行（跨读块，窗内）",
     row_bytes(LO + 420, note="x" * 9000), (False, False)),
]

print("[1] 判定层：字节版 vs 文本版，TRICKY 形状逐条")
ok_imply = True
ok_same = True
for label, raw, (want_b, want_t) in SHAPES:
    body = raw.rstrip(b"\n")          # 判定看的是不含换行的那段（扫描里也一样）
    got_b = U._line_outside_window_bytes(body, LO, HI)
    try:
        got_t = U._line_outside_window(body.decode("utf-8").strip(), LO, HI)
    except UnicodeDecodeError:
        got_t = "decode-error"
    if got_b and got_t is not True:
        ok_imply = False
        print("  [FAIL] %s：字节版说窗外（True），文本版却 %r" % (label, got_t))
    if (got_b, got_t) != (want_b, want_t):
        ok_same = False
        print("  [FAIL] %-34s 拿到 %r/%r，期望 %r/%r"
              % (label, got_b, got_t, want_b, want_t))
    else:
        print("    %-34s 字节版=%-5s 文本版=%s" % (label, got_b, got_t))
check("字节版说'窗外'的行，文本版也必须说'窗外'（保守方向不许破）", ok_imply)
check("27 条 TRICKY 形状逐条与期望一致", ok_same)

# 护栏单独钉：坏字节与非 ASCII 必须落到 decode 上（不是被跳过）。
check("坏 UTF-8 行不被字节预过滤跳过（跳过它就绕开了 decode 报错）",
      U._line_outside_window_bytes(
          b'{"at": 1789999000.5, "model": "\xff\xfe bad"}', LO, HI) is False)
check("合法非 ASCII 行同样不跳过（整行 ASCII 才可能返回 True）",
      U._line_outside_window_bytes(
          row_bytes(LO - 120, model="\u4e2d\u6587").rstrip(b"\n"), LO, HI) is False)


# --------------------------------------------------------------------------
# 日志：干净的一份（无坏字节），窗内/窗外/边界/半行/跨块都在
# --------------------------------------------------------------------------
# (标签, 行, 是否折进窗口, 字节预过滤是否该跳过它)
CLEAN_LINES = [
    ("窗外 · 普通", row_bytes(LO - 7200), False, True),
    ("窗内 · 第一条", row_bytes(LO + 60), True, False),
    ("窗内 · 另一账号", row_bytes(LO + 120, model="m-b", account="u2",
                                   realm="cn"), True, False),
    ("窗外 · 坏 JSON（形状对）", b'{"at": 1789999000.5, "broken\n', False, True),
    ("窗外 · 整数 at", b'{"at": 1789999000, "model": "m-a"}\n', False, False),
    ("窗外 · 重复 at（末值窗内）",
     b'{"at": 1789999000.5, "x": 1, "at": 1790000060.0, "model": "m-b"}\n',
     True, False),
    ("窗外 · 转义引号（拼不出 \"at\"）",
     b'{"at": 1789999000.5, "note": "say \\"at\\": \\"x\\"", "model": "m-a"}\n',
     False, True),
    ("窗内 · 边界 lo", row_bytes(LO), True, False),
    ("窗外 · lo 之下一点点", row_bytes(LO - 1e-6), False, True),
    ("窗内 · 边界 hi", row_bytes(HI), True, False),
    ("窗外 · hi 之上一点点", row_bytes(HI + 1e-6), False, True),
    ("窗内 · 跨块长行", row_bytes(LO + 420, note="x" * 9000), True, False),
    ("窗外 · 跨块长行", row_bytes(LO - 60, note="y" * 9000), False, True),
    ("窗内 · 最后一条完整行", row_bytes(LO + 500), True, False),
]
EXPECT_ROWS = sum(1 for _l, _r, folded, _h in CLEAN_LINES if folded)
EXPECT_HITS = sum(1 for _l, _r, _f, hit in CLEAN_LINES if hit)
# 半行（没有换行）：留给下一趟；它的 at 在窗内，补全后应当多折一条。
HALF_LINE = b'{"at": 1790000300.0, "model": "m-a"'


def write_log(chunks):
    with open(USAGE_LOG, "wb") as fh:
        for chunk in chunks:
            fh.write(chunk)


def clean_log():
    write_log([raw for _l, raw, _f, _h in CLEAN_LINES] + [HALF_LINE])


clean_log()


def reset_states():
    """把各聚合的状态清干净（等价于进程刚起、无 checkpoint）。"""
    for name in ("_snap_cache", "_count_cache", "_series_cache", "_series_state",
                 "_analytics_cache", "_analytics_state", "_usage_snap_state",
                 "_perf_cache"):
        obj = getattr(U, name, None)
        if isinstance(obj, dict):
            obj.clear()
    U._byacct_cache.update({"at": 0.0, "data": None})
    U._byacct_state.update({"buckets": None, "offset": 0, "key": None, "tail": b""})
    U._count_state.update({"offset": 0, "key": None, "tail": b"", "n": 0,
                           "n_realm": {}})
    U._remaining_cache.update({"at": 0.0, "built_at": 0.0, "data": None})
    U._daily_usage.update({"day": "", "totals": None, "credits": None,
                           "models": None, "offset": 0, "at": 0.0})
    try:
        U._remaining_state["events"] = []
        U._remaining_state["loaded"] = False
        U._remaining_state["offset"] = 0
        U._remaining_state["key"] = None
    except Exception:
        pass


_real_prefilter = U._line_outside_window_bytes


def dump(value):
    """两种序列化都算：raw 抓键序差异，sorted 只比内容。"""
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")))


def run_with_prefilter(on, fn):
    """跑一次 fn：on=False 时把字节预过滤关掉（等价于改动前的路径）。"""
    reset_states()
    if not on:
        U._line_outside_window_bytes = lambda raw, lo, hi: False
    try:
        try:
            out = fn()
        except Exception as exc:                # 两个模式都得同样地炸
            out = "ERR: %s: %s" % (type(exc).__name__, exc)
    finally:
        U._line_outside_window_bytes = _real_prefilter
    return out


# 冻结时钟：载荷里有几个"现在"字段（快照的 started、remaining 的
# generated_at），不冻的话开/关两次调用必然差在这几个字上。口径同
# bytegate：只冻无参调用，带参的 localtime/gmtime 照旧（日键算的是行自己的 at）。
_NOW = LO + 3600.0
_real_time, _real_lt, _real_gm = time.time, time.localtime, time.gmtime
time.time = lambda: _NOW
time.localtime = lambda secs=None: _real_lt(_NOW if secs is None else secs)
time.gmtime = lambda secs=None: _real_gm(_NOW if secs is None else secs)


# --------------------------------------------------------------------------
# [2] 扫描层：预过滤开/关，输出逐字节一致
# --------------------------------------------------------------------------
print()
print("[2] 扫描层：六个窗口读取器 + 公开端点，预过滤开/关逐字节对拍")

READERS = [
    ("_scan_usage_snapshot_window",
     lambda: U._scan_usage_snapshot_window(None, LO, HI)),
    ("_scan_usage_snapshot_window_days",
     lambda: U._scan_usage_snapshot_window_days(None, LO)),
    ("_scan_usage_snapshot_window_hours",
     lambda: U._scan_usage_snapshot_window_hours(None, LO)),
    ("_scan_usage_analytics_window",
     lambda: U._scan_usage_analytics_window(None, LO, HI)),
    ("_scan_usage_analytics_window_days",
     lambda: U._scan_usage_analytics_window_days(None, LO)),
    ("_scan_usage_analytics_window_hours",
     lambda: U._scan_usage_analytics_window_hours(None, LO, HI)),
    ("_usage_timeseries_scan",
     lambda: U._usage_timeseries_scan(None, LO, HI, 60)),
    ("usage_snapshot(custom)",
     lambda: U.usage_snapshot(realm=None, range="custom", since=LO, until=HI)),
    ("compute_usage_analytics(custom)",
     lambda: U.compute_usage_analytics(range="custom", since=LO, until=HI)),
    ("usage_timeseries(custom)",
     lambda: U.usage_timeseries(range="custom", since=LO, until=HI)),
    ("remaining_usage",
     lambda: U.remaining_usage()),
    ("count_usage_rows",
     lambda: U.count_usage_rows(realm=None)),
    ("usage_by_account",
     lambda: U.usage_by_account()),
]

ok_same = True
for label, fn in READERS:
    on = run_with_prefilter(True, fn)
    off = run_with_prefilter(False, fn)
    same = dump(on) == dump(off)
    ok_same = ok_same and same
    if not same:
        print("  [FAIL] %s：开/关输出不同" % label)
        a, b = dump(on)[1], dump(off)[1]
        for i in range(min(len(a), len(b))):
            if a[i] != b[i]:
                print("    开: %s" % a[max(0, i - 80):i + 120])
                print("    关: %s" % b[max(0, i - 80):i + 120])
                break
        else:
            print("    长度 %d vs %d" % (len(a), len(b)))
    else:
        print("    %-34s 一致（raw=%d 字节）" % (label, len(dump(on)[0])))
check("十三个读取器在预过滤开/关下输出逐字节一致（raw 与 sorted 都比）",
      ok_same)

# 窗口口径本身也直接钉一遍：折进窗口的正好是 EXPECT_ROWS 条。
snap_on = run_with_prefilter(True, READERS[0][1])
snap_off = run_with_prefilter(False, READERS[0][1])
check("窗内行数正确（恰好 lo / 恰好 hi 保留，外面一点点丢掉，半行不数）",
      snap_on["requests"] == EXPECT_ROWS, (snap_on["requests"], EXPECT_ROWS))
check("关掉预过滤时窗内行数相同",
      snap_off["requests"] == EXPECT_ROWS, snap_off["requests"])

# 无 skip 的调用方（all-time 折叠等）本来就与预过滤无关：关掉也不许变。
alltime_on = run_with_prefilter(True, lambda: U._scan_usage_snapshot_window(None, LO, None))
alltime_off = run_with_prefilter(False, lambda: U._scan_usage_snapshot_window(None, LO, None))
check("只有下界的窗口在开/关下一致（同一个 skip 路径的另一种边界）",
      dump(alltime_on) == dump(alltime_off))


# --------------------------------------------------------------------------
# [3] 计数桩：跳过的行不再 decode；json.loads 次数不变
# --------------------------------------------------------------------------
print()
print("[3] 计数桩：decode 次数（bytes 子类）与 json.loads 次数")


class _CountedBytes(bytes):
    """bytes 子类：每 decode 一次记一笔（扫描 decode 的正是 readline 的返回）。"""

    decodes = 0

    def decode(self, *args, **kwargs):
        _CountedBytes.decodes += 1
        return bytes.decode(self, *args, **kwargs)


class _CountedReader(object):
    """包一层 readline：每行都换成 _CountedBytes。其余属性原样转发。"""

    def __init__(self, fh):
        self._fh = fh

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._fh.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._fh, name)

    def readline(self):
        raw = self._fh.readline()
        return _CountedBytes(raw) if raw else raw


_real_open = open


def _counted_open(path, mode="r", *args, **kwargs):
    fh = _real_open(path, mode, *args, **kwargs)
    if "b" in str(mode) and os.path.abspath(str(path)) == os.path.abspath(USAGE_LOG):
        return _CountedReader(fh)
    return fh


def scan_counted(prefilter):
    """跑一次窗口扫描，返回 (结果, {decode 次数, loads 次数, 预过滤命中数})。"""
    counts = {"decodes": 0, "loads": 0, "hits": 0}
    real_loads = P.json.loads

    def loads_hook(*args, **kwargs):
        counts["loads"] += 1
        return real_loads(*args, **kwargs)

    def bytes_hook(raw, lo, hi):
        out = _real_prefilter(raw, lo, hi)
        if out:
            counts["hits"] += 1
        return out

    _CountedBytes.decodes = 0
    U.open = _counted_open
    P.json.loads = loads_hook
    U._line_outside_window_bytes = (bytes_hook if prefilter
                                    else (lambda raw, lo, hi: False))
    try:
        snap = U._scan_usage_snapshot_window(None, LO, HI)
    finally:
        U.__dict__.pop("open", None)
        P.json.loads = real_loads
        U._line_outside_window_bytes = _real_prefilter
    counts["decodes"] = _CountedBytes.decodes
    return snap, counts


total_lines = len(CLEAN_LINES)            # 半行不算（扫描留给下一趟）
snap_pre, c_pre = scan_counted(True)
snap_plain, c_plain = scan_counted(False)
print("    预过滤开：decode=%d  json.loads=%d  命中=%d"
      % (c_pre["decodes"], c_pre["loads"], c_pre["hits"]))
print("    预过滤关：decode=%d  json.loads=%d" % (c_plain["decodes"], c_plain["loads"]))
check("关掉预过滤时每一整行都 decode（旧路径）",
      c_plain["decodes"] == total_lines, (c_plain["decodes"], total_lines))
check("开着预过滤时命中的行一条都不 decode（decode = 总行数 - 命中数）",
      c_pre["hits"] == EXPECT_HITS
      and c_pre["decodes"] == total_lines - EXPECT_HITS,
      (c_pre["decodes"], c_pre["hits"], total_lines, EXPECT_HITS))
check("跳过的行真的不再 decode（decode 次数严格变少）",
      c_pre["decodes"] < c_plain["decodes"], (c_pre["decodes"], c_plain["decodes"]))
check("json.loads 次数一个不变，且等于旧路径（省的是 decode，不是 loads）",
      c_pre["loads"] == c_plain["loads"] == total_lines - EXPECT_HITS,
      (c_pre["loads"], c_plain["loads"], total_lines - EXPECT_HITS))
check("计数桩下输出仍逐字节一致", dump(snap_pre) == dump(snap_plain))
check("桩自检：计数确实挂在扫描用的文件对象上（关模式 decode 数 = 整行数）",
      c_plain["decodes"] == total_lines)


# --------------------------------------------------------------------------
# [4] 坏字节：照旧落到 decode，扫描停在原地，后面的行两个模式都不折
# --------------------------------------------------------------------------
print()
print("[4] 坏 UTF-8：预过滤不许把它跳过（跳过它就绕开了 decode 报错）")

BAD_LINES = [
    ("窗外 · 普通（预过滤可跳）", row_bytes(LO - 3600), False),
    ("窗内 · 坏行之前", row_bytes(LO + 60), True),
    ("窗外 · 合法非 ASCII（字节版落到 decode，文本版跳过）",
     row_bytes(LO - 60, model="\u4e2d\u6587\u6a21\u578b"), False),
    ("坏 UTF-8（形状是窗外）",
     b'{"at": 1789999000.5, "model": "\xff\xfe bad"}\n', "stop"),
    ("窗内 · 坏行之后（两个模式都不折）", row_bytes(LO + 120), False),
]
write_log([raw for _l, raw, _f in BAD_LINES])
EXPECT_BAD_ROWS = 1                       # 只有"坏行之前"那一条

# 这一段扫描会往 stderr 报一次 "usage snapshot read failed"（坏行让 pass 停在
# 原地，正是被钉住的行为），预期输出，别让它混进矩阵日志。
_real_log = P.log
P.log = lambda *args, **kwargs: None
try:
    snap_bad_on, cb_on = scan_counted(True)
    snap_bad_off, cb_off = scan_counted(False)
finally:
    P.log = _real_log
print("    预过滤开：decode=%d  json.loads=%d  命中=%d"
      % (cb_on["decodes"], cb_on["loads"], cb_on["hits"]))
print("    预过滤关：decode=%d  json.loads=%d" % (cb_off["decodes"], cb_off["loads"]))
check("坏行让整趟扫描停在原地：它后面的窗内行两个模式都不折",
      snap_bad_on["requests"] == EXPECT_BAD_ROWS
      and snap_bad_off["requests"] == EXPECT_BAD_ROWS,
      (snap_bad_on["requests"], snap_bad_off["requests"], EXPECT_BAD_ROWS))
check("坏字节场景下开/关输出仍逐字节一致",
      dump(snap_bad_on) == dump(snap_bad_off))
check("坏行被 decode（护栏生效：开模式 decode 含坏行本身）",
      cb_on["decodes"] == 3 and cb_off["decodes"] == 4,
      (cb_on["decodes"], cb_off["decodes"]))
check("非 ASCII 的合法行没让扫描提前停（它落在 decode 上、被文本版跳过）",
      cb_on["loads"] == 1 and cb_on["hits"] == 1,
      (cb_on["loads"], cb_on["hits"]))

# 坏行被截掉之后，后面的行照折（两个模式都恢复）。
write_log([raw for _l, raw, _f in BAD_LINES if _f != "stop"])
snap_fixed_on = run_with_prefilter(True, READERS[0][1])
snap_fixed_off = run_with_prefilter(False, READERS[0][1])
check("截掉坏行后：窗内行恢复（两个模式一致）",
      dump(snap_fixed_on) == dump(snap_fixed_off)
      and snap_fixed_on["requests"] == 2, snap_fixed_on["requests"])


# --------------------------------------------------------------------------
# [5] 半行与跨块行：整行纪律不变
# --------------------------------------------------------------------------
print()
print("[5] 半行 / 跨块行")

clean_log()
check("半行不折（计数停在最后一条完整行）",
      run_with_prefilter(True, READERS[0][1])["requests"] == EXPECT_ROWS)
with open(USAGE_LOG, "ab") as fh:
    fh.write(b'}\n')
half_done = run_with_prefilter(True, READERS[0][1])
check("半行补全后多折一条（补的这条 at 在窗内）",
      half_done["requests"] == EXPECT_ROWS + 1, half_done["requests"])
check("补全后开/关仍逐字节一致",
      dump(half_done) == dump(run_with_prefilter(False, READERS[0][1])))

clean_log()
check("跨块长行（窗外）字节版判为窗外",
      U._line_outside_window_bytes(
          row_bytes(LO - 60, note="z" * 9000).rstrip(b"\n"), LO, HI) is True)
check("跨块长行（窗内）字节版判为窗内（留给 decode 与折叠）",
      U._line_outside_window_bytes(
          row_bytes(LO + 420, note="z" * 9000).rstrip(b"\n"), LO, HI) is False)
check("跨块行两个模式都折进窗口",
      run_with_prefilter(True, READERS[0][1])["requests"] == EXPECT_ROWS)

# --------------------------------------------------------------------------
time.time, time.localtime, time.gmtime = _real_time, _real_lt, _real_gm
shutil.rmtree(_TMP, ignore_errors=True)
print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
