"""用量聚合的两个热点：perf_stats 的窗口预过滤与 count_usage_rows 的增量计数。

两个接口都只读日志，但一个每次重建都要把采样的每一行重新解析（/usage/perf
是面板轮询的重头），另一个每次 /usage/recent 轮询都要全量重扫一遍日志只为
页数标签。各钉一组：

  1. perf_stats 的窗口预过滤（复用 _line_outside_window）必须是**语义中性**
     的：把预过滤关掉（monkeypatch 成恒 False，等价于改动前的路径）跑同一
     份日志，聚合结果必须与开着时逐字段相同；同时 json.loads 的调用次数要
     真的降下来（计数桩），证明省掉的解析真实存在、不是被悄悄退化。手改过
     的行——at 不是第一个键、重复 at、整数 at、坏 JSON、转义引号——全部
     混在日志里：预过滤只许丢「下面的窗口判断本来也会丢」的行，丢错任何
     一行，对拍立刻失败。
  2. count_usage_rows 的增量折叠与旧的全量扫描逐字对拍：参考实现（旧算法
     原样复制在本文件里）对每一个 realm 各扫一遍全文件，增量实现复用同一条
     进度；冷启动、追加、截断、同尺寸重写、换文件、文件消失，以及
     None/"intl"/"cn"/"all" 的组合，两边必须逐个相等。折叠推进到文件尾之后，
     0 新字节的调用一个 json.loads 都不许发生（增量成立的直接证据）。

唯一一处有意的口径收敛：日志末尾没有换行的半行不再计入总数（旧实现会把
正在写入的半行也数进去），与 _scan_usage_from / _scan_daily_usage 的整行
纪律一致——等它写完整再数，收敛后的数字与旧实现相同（下面单独钉住）。

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

_TMP = tempfile.mkdtemp(prefix="wb-hotspots-")
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
# 计数 TTL 归零：本套件每次都要走「未缓存」路径，TTL 行为另有专门断言。
U._COUNT_TTL = 0.0
# 关闭 checkpoint：本套件只测这两个读者，别让落盘状态掺进来。
os.environ["WB_USAGE_CACHE"] = "0"

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


def first_diff(a, b, span=70):
    if a == b:
        return ""
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return "@%d: %r vs %r" % (i, a[i:i + span], b[i:i + span])
    return "长度 %d vs %d" % (len(a), len(b))


# --------------------------------------------------------------------------
# 数据：一张带边界行的小日志
# --------------------------------------------------------------------------
_lt = time.localtime()
TODAY0 = time.mktime((_lt.tm_year, _lt.tm_mon, _lt.tm_mday, 0, 0, 0, 0, 0, -1))
YESTERDAY = TODAY0 - 86400


def row(at, model="m-a", account="u1", realm="intl", outcome="completed"):
    r = {"at": at, "model": model, "stream": True, "outcome": outcome,
         "elapsed_ms": 1000, "ttft_ms": 300, "gen_ms": 700,
         "prompt_tokens": 100, "completion_tokens": 50,
         "reasoning_tokens": 0, "cached_tokens": 0,
         "account": account, "tokens_per_sec": 90.0, "total_tokens": 150,
         "cache_hit_pct": 0.0}
    if realm is not None:
        r["realm"] = realm
    return r


def write_log(lines):
    with io.open(USAGE_LOG, "w", encoding="utf-8", newline="\n") as fh:
        for line in lines:
            fh.write(line + "\n")


def append_log(lines):
    with io.open(USAGE_LOG, "a", encoding="utf-8", newline="\n") as fh:
        for line in lines:
            fh.write(line + "\n")


def jline(row_obj):
    return json.dumps(row_obj, ensure_ascii=False)


# 手改过的行：预过滤读不精确的都必须照旧落到解析与 fold 上。
TRICKY = [
    # at 不是第一个键：预过滤认不出，只能解析（fold 再按窗口判断）
    jline({"model": "m-a", "at": YESTERDAY + 100, "realm": "intl",
           "outcome": "completed", "elapsed_ms": 900, "ttft_ms": 200,
           "gen_ms": 700, "tokens_per_sec": 80.0, "cache_hit_pct": 0.0,
           "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
    # 重复 at：第一个在窗外、最后一个在窗内。fold 保留（json.loads 取最后
    # 一个），预过滤不许因为第一个值就把它丢掉。
    '{"at": %d, "x": 1, "at": %d, "model": "m-b", "realm": "intl", '
    '"outcome": "completed", "elapsed_ms": 800, "ttft_ms": 100, "gen_ms": 700, '
    '"tokens_per_sec": 70.0, "prompt_tokens": 10, "completion_tokens": 5, '
    '"total_tokens": 15, "cache_hit_pct": 0.0}'
    % (YESTERDAY + 200, TODAY0 + 500),
    # 整数 at（json.loads 给 int）：预过滤不认，必须落到解析
    '{"at": %d, "model": "m-c", "realm": "cn", "outcome": "completed", '
    '"elapsed_ms": 700, "ttft_ms": 90, "gen_ms": 600, "tokens_per_sec": 60.0, '
    '"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, '
    '"cache_hit_pct": 0.0}' % int(TODAY0 + 700),
    # 字符串里的转义引号：不能拼出假的 "at" / "realm" 命中
    jline({"at": TODAY0 + 800, "note": 'say \\"at\\": \\"999\\" here',
           "model": "m-a", "realm": "intl", "outcome": "completed",
           "elapsed_ms": 600, "ttft_ms": 80, "gen_ms": 520,
           "tokens_per_sec": 50.0, "prompt_tokens": 10, "completion_tokens": 5,
           "total_tokens": 15, "cache_hit_pct": 0.0}),
    # 坏 JSON（非空行，总行数照数；任何 realm 都不算）
    '{"at": %d, "broken' % (TODAY0 + 900),
]


def make_log():
    lines = []
    # 昨天的行（today 窗口外）
    lines.append(jline(row(YESTERDAY + 3600, "m-a", "u1", "intl")))
    lines.append(jline(row(YESTERDAY + 7200, "m-b", "u2", "cn")))
    # 今天的行（窗口内），两个 realm 都有
    lines.append(jline(row(TODAY0 + 100, "m-a", "u1", "intl")))
    lines.append(jline(row(TODAY0 + 200, "m-b", "u2", "cn")))
    lines.append(jline(row(TODAY0 + 300, "m-a", "u2", "intl", "failed")))
    # 没有 realm 字段的老行（错误行）：走 row_realm 回退
    lines.append(jline(row(TODAY0 + 400, "gpt-5.6-luna", "u9", None, "failed")))
    lines.append(jline(row(TODAY0 + 450, "minimax-m3", "u9", None, "failed")))
    lines.extend(TRICKY)
    # 末尾再补一行今天的，保证 tail 签名落在窗口内
    lines.append(jline(row(TODAY0 + 1000, "m-a", "u1", "intl")))
    return lines


LOG_LINES = make_log()
write_log(LOG_LINES)

# --------------------------------------------------------------------------
# [1] perf_stats：窗口预过滤语义中性 + 真的少解析
# --------------------------------------------------------------------------
print("[1] perf_stats 窗口预过滤：与关掉预过滤（旧路径）逐字段对拍")

_real_json = P.json
_orig_line_outside = U._line_outside_window


class JsonCounter(object):
    """json 模块的计数桩：只数 loads，其余原样转发。

    _real 在构造时抓住**原始** loads：钩子挂在模块属性上，若转发时再读
    json.loads，读到的就是钩子自己（递归到 RecursionError），计数会变成
    「每行一次递归上限」的假数字。
    """

    def __init__(self):
        self.loads = 0
        self._real = _real_json.loads

    def __getattr__(self, name):
        return getattr(_real_json, name)

    def loads_hook(self, *args, **kwargs):
        self.loads += 1
        return self._real(*args, **kwargs)


def perf_with(sample, realm, since, until, prefilter=True):
    """(结果, json.loads 次数)。prefilter=False 即改动前的路径。"""
    counter = JsonCounter()
    old_loads = _real_json.loads
    _real_json.loads = counter.loads_hook
    try:
        if prefilter:
            U._line_outside_window = _orig_line_outside
        else:
            U._line_outside_window = lambda *a, **k: False
        try:
            out = U._perf_stats_uncached(sample, realm, since=since, until=until)
        finally:
            U._line_outside_window = _orig_line_outside
    finally:
        _real_json.loads = old_loads
    return out, counter.loads


# saved=True 的用例里日志确有几行能被预过滤证明在窗外；False 的用例里没有
# 可省的行（无窗口，或窗口盖住了每一行），解析次数必须持平。
CASES = [
    ("无窗口", 5000, None, None, None, False),
    ("today/all", 5000, None, TODAY0, None, True),
    ("today/intl", 5000, "intl", TODAY0, None, True),
    ("today/cn", 5000, "cn", TODAY0, None, True),
    ("全量窗口（下界 0 视为无界）", 5000, None, 0, None, False),
    ("收窄上界", 5000, None, None, TODAY0 + 400, True),
    ("两侧都钉", 5000, "intl", TODAY0 + 150, TODAY0 + 900, True),
    ("采样封顶 + 窗口", 3, None, TODAY0, None, False),
]
ok_same = True
ok_loads = True
for label, sample, realm, since, until, saved in CASES:
    with_pre, loads_pre = perf_with(sample, realm, since, until, True)
    without_pre, loads_plain = perf_with(sample, realm, since, until, False)
    same = (json.dumps(with_pre, ensure_ascii=False)
            == json.dumps(without_pre, ensure_ascii=False))
    ok_same = ok_same and same
    if not same:
        print("  [FAIL] 对拍：%s\n    %s" % (label, first_diff(
            json.dumps(without_pre, ensure_ascii=False),
            json.dumps(with_pre, ensure_ascii=False))))
    if saved:
        ok_loads = ok_loads and loads_pre < loads_plain
    else:
        ok_loads = ok_loads and loads_pre == loads_plain
    print("    %-24s json.loads：预过滤 %d 次 vs 旧路径 %d 次" %
          (label, loads_pre, loads_plain))
check("对拍：八个窗口/realm/采样组合全部逐字段一致", ok_same)
check("预过滤真的省解析（有可省行时严格变少，无省可省时持平）", ok_loads)

# 直接钉几个数字：预过滤不许把窗口内的行丢掉，也不许把窗外行算进来。
perf_today = U._perf_stats_uncached(5000, None, since=TODAY0, until=None)
perf_all = U._perf_stats_uncached(5000, None)
perf_cn_today = U._perf_stats_uncached(5000, "cn", since=TODAY0, until=None)
check("today 窗口只数窗口内（含重复 at 行的最后值、整数 at 行）",
      perf_today["sampled"] == 9, perf_today["sampled"])
check("all 数到全部可解析行", perf_all["sampled"] == len(LOG_LINES) - 1,
      perf_all["sampled"])
check("realm 过滤与窗口叠加（cn 的字段行 + 模型前缀归属的老行）",
      perf_cn_today["sampled"] == 3, perf_cn_today["sampled"])
check("重复 at 的行按解析结果（最后一个 at）进了窗口",
      "m-b" in perf_today["by_model"], list(perf_today["by_model"]))
check("整数 at 的行也在窗口里", "m-c" in perf_today["by_model"],
      list(perf_today["by_model"]))
check("坏 JSON 行既不在窗口也不在 all",
      perf_all["sampled"] == len(LOG_LINES) - 1, perf_all["sampled"])

# sample_from 的语义没有被动过：它取第一行解析出来的 at，即使那行在窗外。
check("sample_from 仍是第一行的 at（窗外的 yesterday 行）",
      perf_today["sample_from"] == YESTERDAY + 3600,
      perf_today["sample_from"])
check("sample_from 与关掉预过滤时一致",
      perf_today["sample_from"]
      == perf_with(5000, None, TODAY0, None, False)[0]["sample_from"])

# --------------------------------------------------------------------------
# [2] count_usage_rows：增量与旧全量扫描对拍
# --------------------------------------------------------------------------
print()
print("[2] count_usage_rows 增量折叠：与旧全量扫描（参考实现）对拍")


def ref_count(realm=None):
    """改动前 _count_usage_rows_uncached 的逐字复制（参考实现）。"""
    needles = ()
    if realm:
        needles = ('"realm": "%s"' % realm, '"realm":"%s"' % realm)
    n = 0
    try:
        with open(USAGE_LOG, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                if not needles:
                    n += 1
                    continue
                if any(x in line for x in needles):
                    n += 1
                    continue
                if '"realm"' in line:
                    continue          # realm 字段存在但值不同
                try:
                    if U.row_matches_realm(json.loads(line), realm):
                        n += 1
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return n


def count_via_uncached(realm):
    """走未缓存入口（含 realm_scope 归一化），绕开 TTL 缓存。"""
    return U._count_usage_rows_uncached(U.realm_scope(realm))


def compare_all(label):
    """四个 realm 全部与参考实现对拍；返回是否全等。"""
    detail = {}
    ok = True
    for realm in (None, "intl", "cn", "all"):
        got = count_via_uncached(realm)
        want = ref_count(U.realm_scope(realm))
        detail[realm] = (got, want)
        ok = ok and got == want
    check(label, ok, detail)
    return ok


compare_all("冷启动：四个 realm 的增量计数都等于全量扫描")
check("总数把坏 JSON 的非空行也算进去（与旧实现同口径）",
      count_via_uncached(None) == len(LOG_LINES), count_via_uncached(None))
check("没有 realm 字段的老行按模型前缀归属（gpt-* -> intl，minimax-* -> cn）",
      count_via_uncached("intl") == ref_count("intl")
      and count_via_uncached("cn") == ref_count("cn"),
      (count_via_uncached("intl"), ref_count("intl"),
       count_via_uncached("cn"), ref_count("cn")))
check("realm_scope('all') 与 None 同值（总数）",
      U.count_usage_rows("all") == U.count_usage_rows(None))

# 追加：增量只数新字节，结果与全量对拍
append_log([
    jline(row(TODAY0 + 1100, "m-a", "u1", "intl")),
    jline(row(TODAY0 + 1200, "m-b", "u2", "cn")),
    jline(row(TODAY0 + 1300, "gpt-5.6-luna", "u9", None, "failed")),
])
compare_all("追加后：增量结果仍与全量一致")
check("折叠推进到了文件尾（没有每次重扫全文件）",
      U._count_state["offset"] == os.path.getsize(USAGE_LOG),
      (U._count_state["offset"], os.path.getsize(USAGE_LOG)))

# 0 新字节：一个 json.loads 都不许发生
counter = JsonCounter()
old_loads = _real_json.loads
_real_json.loads = counter.loads_hook
try:
    n_none = U._count_usage_rows_uncached(None)
    n_cn = U._count_usage_rows_uncached("cn")
finally:
    _real_json.loads = old_loads
check("0 新字节的调用完全不解析日志（loads=0）", counter.loads == 0,
      counter.loads)
check("0 新字节的调用结果不变",
      n_none == ref_count(None) and n_cn == ref_count("cn"), (n_none, n_cn))
# 自检：桩本身是活的——折叠真的要解析时计数必须 > 0，否则上面的 0 无意义
append_log([jline(row(TODAY0 + 1050, "gpt-5.6-luna", "u9", None, "failed"))])
counter2 = JsonCounter()
old_loads = _real_json.loads
_real_json.loads = counter2.loads_hook
try:
    U._count_usage_rows_uncached(None)
finally:
    _real_json.loads = old_loads
check("计数桩自检：有新字节时确实解析了新增行（loads>0）",
      counter2.loads >= 1, counter2.loads)

# 截断（copytruncate）：位置校验必须作废重数
write_log(LOG_LINES[:4])
compare_all("日志被截短：增量结果与全量一致（状态重来）")

# 恢复原日志；同尺寸重写：tail 签名必须挡住旧进度
write_log(LOG_LINES)
count_via_uncached(None)            # 先把进度推到文件尾
size = os.path.getsize(USAGE_LOG)
text = io.open(USAGE_LOG, encoding="utf-8").read()
needle = '"realm": "intl"'
pos = text.rfind(needle)            # 最后一行的 realm 字段
check("变异点落在文件尾部（tail 签名看得到）",
      pos >= 0 and len(text) - pos < 64, (pos, len(text)))
# 等长替换：intl -> intx，这一行从 intl 桶里消失（任何查询都不再命中它）。
# 如果进度没有作废，intl 的计数会停在旧值——对拍立刻抓住。
mutated = text[:pos] + '"realm": "intx"' + text[pos + len(needle):]
check("变异保持字节数不变（size 校验对它是瞎的）",
      len(mutated.encode("utf-8")) == size,
      (len(mutated.encode("utf-8")), size))
io.open(USAGE_LOG, "w", encoding="utf-8", newline="\n").write(mutated)
compare_all("同尺寸重写：tail 签名挡住旧进度，结果与全量一致")

# 文件整个消失 / 重建
write_log(LOG_LINES)
count_via_uncached(None)
os.unlink(USAGE_LOG)
compare_all("日志消失：所有 realm 都是 0（与全量一致）")
write_log(LOG_LINES)
compare_all("日志重建：结果与全量一致")

# 半行（没有换行的尾巴）：留给下一趟，等写完整再数。旧实现会把正在写的半行
# 也数进总数，这是本改动有意收敛的一处 ≤1 行瞬时差异（与 _scan_usage_from
# 的整行纪律一致）；realm 计数两边本来就都不含它。
with io.open(USAGE_LOG, "a", encoding="utf-8", newline="\n") as fh:
    fh.write('{"at": %d, "model": "m-a"' % (TODAY0 + 1400))
check("半行不数：总数停在最后一个完整行", count_via_uncached(None) == len(LOG_LINES),
      (count_via_uncached(None), len(LOG_LINES)))
check("半行不数：realm 计数与全量一致（半行对 realm 无贡献）",
      all(count_via_uncached(r) == ref_count(U.realm_scope(r))
          for r in ("intl", "cn")),
      {r: (count_via_uncached(r), ref_count(U.realm_scope(r)))
       for r in ("intl", "cn")})
append_log([', "realm": "intl"}'])
check("半行补全后总数与全量一致（收敛到旧实现的数字）",
      count_via_uncached(None) == ref_count(None),
      (count_via_uncached(None), ref_count(None)))
check("半行补全后 realm 计数也一致",
      count_via_uncached("intl") == ref_count("intl"),
      (count_via_uncached("intl"), ref_count("intl")))

# 非法 UTF-8 的坏行：折叠停在它前面，这一次返回已数到的部分，下一次重数也
# 停在同一个位置（结果稳定）；坏行被截掉后折叠继续。旧实现的文本层按块解码，
# 坏行落在第一块里时整次报 0、落在后面时给部分数——新实现稳定地给出坏行之前
# 的行数，任何情况下都不会更差。
partial = count_via_uncached(None)
size_good = os.path.getsize(USAGE_LOG)
with open(USAGE_LOG, "ab") as fh:
    fh.write(b'{"at": 1, "model": "\xff\xfe bad"}\n')
check("非法 UTF-8 行：这一次返回已数到的部分（不报 0）",
      count_via_uncached(None) == partial,
      (count_via_uncached(None), partial))
check("非法 UTF-8 行：下一次重数停在同一个位置（结果稳定）",
      count_via_uncached(None) == partial,
      (count_via_uncached(None), partial))
with open(USAGE_LOG, "r+b") as fh:
    fh.truncate(size_good)
check("截回坏行之前：位置校验通过、结果不变",
      count_via_uncached(None) == partial, count_via_uncached(None))
append_log([jline(row(TODAY0 + 1600, "m-a", "u1", "intl"))])
check("截掉坏行后再追加：折叠继续（+1）",
      count_via_uncached(None) == partial + 1,
      (count_via_uncached(None), partial + 1))

# TTL 缓存：默认 TTL 内不重算，过期后重算并跟上新行
U._COUNT_TTL = 30.0
U._count_cache.clear()
first = U.count_usage_rows("cn")
append_log([jline(row(TODAY0 + 1500, "m-b", "u2", "cn"))])
check("TTL 内命中缓存（不重算）", U.count_usage_rows("cn") == first,
      (U.count_usage_rows("cn"), first))
U._COUNT_TTL = 0.0
U._count_cache.clear()
check("TTL 过期后重算并跟上新行",
      U.count_usage_rows("cn") == ref_count("cn"),
      (U.count_usage_rows("cn"), ref_count("cn")))

shutil.rmtree(_TMP, ignore_errors=True)
print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
