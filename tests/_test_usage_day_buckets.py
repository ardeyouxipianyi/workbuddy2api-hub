"""按日分桶：窗口半（range=today/week/month）的增量。

PR #189 把 all-time 半变成按字节偏移的增量折叠，落盘 checkpoint 解决了重启
后的冷扫；但窗口半仍是「从头扫到窗口」——窗口越宽扫得越多。这一套钉住按日
分桶这条新路径：

  * 切分：每行折进「它自己 at 所属的本地日」，午夜整点的行归新的一天，
    迟写（at 早于折叠时刻）的行也落回它所属的那天；
  * 等价：对齐窗口（today/week/month）由日桶相加，结果与改动前的逐行口径
    一致。测试数据的价刻意用二进制可精确求和的数（0.5/0.25/0.125）与
    unit=1，所以连 cost_cny 都能逐字节比；价不是精确值时只有浮点求和顺序
    的差异，这一条单独钉在 [10]；
  * 封口：已经越过的那天不再重折（桶对象原样复用，只有今天在长）；
  * checkpoint：日桶随状态一起落盘/加载，重启（含真·子进程）后窗口与冷折
    逐字节一致；schema 2 的旧文件整体作废；
  * 降级：坏日桶（形状/日键/floor/credits/hours）、剪枝线以下的天、折叠
    出错，一律退回整段扫描，结果正确；
  * 时间序列：日桶/小时桶切片与整段扫描逐字节一致（连 credits 前 50 条
    也一样）；夏令时让日长不再是 86400 秒时自动退回扫描；
  * 开关：WB_USAGE_DAY_BUCKETS=0 时窗口路径与改动前逐字节一致（旧口径）。

降级用例沿用 _test_usage_aggregate_cache.py 的写法：先给「应当被拒绝」的
那份状态加一个一眼可见的标记（poison），再做变异。标记没被采用就说明校验
拦住了它；一旦被错误采用，结果里就会多出那个标记，用例当场失败。

No network: the usage log, the pricing table and the settings file are
synthesised in a temp directory.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# 与 my_dump.py / _test_usage_aggregate_cache.py 同一把冻结时钟：窗口边界
# （本地午夜）与 started/since 都从 time.time() 派生，不冻结就不可比。
FROZEN = 1791445200.0

_TMP = tempfile.mkdtemp(prefix="wb-daybucket-")
USAGE_DIR = os.path.join(_TMP, "usage")
ACCOUNTS_DIR = os.path.join(_TMP, "accounts")
os.makedirs(USAGE_DIR, exist_ok=True)
os.makedirs(ACCOUNTS_DIR, exist_ok=True)
os.environ["WB_PROXY_USAGE_DIR"] = USAGE_DIR
os.environ["ACCOUNTS_DIR"] = ACCOUNTS_DIR

import time as _time
_time.time = lambda: FROZEN

import wb_proxy as P

import wb_usage as U
import wb_pricing

P.ACCOUNTS_DIR = ACCOUNTS_DIR
wb_pricing.set_data_dir(USAGE_DIR)
wb_pricing.set_settings_dir(ACCOUNTS_DIR)

USAGE_LOG = P.USAGE_LOG
# v4 布局：一类状态一份文件（usage-aggregate-cache-<kind>.json）。
KIND_FILES = {kind: U._usage_cache_kind_path(kind)
              for kind in U._USAGE_CACHE_KINDS}
U._usage["started"] = FROZEN

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


def canon(obj):
    """不排序键：响应的 JSON 字节里键序是可见的，比较一律用不排序的文本。"""
    return json.dumps(obj, ensure_ascii=False)


def first_diff(a, b, span=60):
    if a == b:
        return ""
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return "@%d: %r vs %r" % (i, a[i:i + span], b[i:i + span])
    return "长度 %d vs %d" % (len(a), len(b))


def float_diffs(a, b, path="", out=None, tol=1e-9):
    """收集超出相对误差的浮点差异；整数字段与结构必须完全相同。"""
    if out is None:
        out = []
    if isinstance(a, dict) and isinstance(b, dict):
        if list(a.keys()) != list(b.keys()):
            out.append((path, "键不同", "键不同"))
            return out
        for k in a:
            float_diffs(a[k], b[k], "%s.%s" % (path, k), out, tol)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append((path, "长度 %d" % len(a), "长度 %d" % len(b)))
            return out
        for i, (x, y) in enumerate(zip(a, b)):
            float_diffs(x, y, "%s[%d]" % (path, i), out, tol)
    elif isinstance(a, bool) or isinstance(b, bool):
        if a != b:
            out.append((path, repr(a), repr(b)))
    elif isinstance(a, float) or isinstance(b, float):
        if isinstance(a, int) and isinstance(b, int):
            if a != b:
                out.append((path, repr(a), repr(b)))
            return out
        scale = max(1.0, abs(a or 0.0), abs(b or 0.0))
        if abs((a or 0.0) - (b or 0.0)) > tol * scale:
            out.append((path, repr(a), repr(b)))
    else:
        if a != b:
            out.append((path, repr(a), repr(b)))
    return out


# --------------------------------------------------------------------------
# 数据：三个本地日 + 午夜整点 + 迟写行 + 无价模型 + 失败/中止
# --------------------------------------------------------------------------
def local_midnight(ts=None, days_back=0):
    lt = time.localtime(FROZEN if ts is None else ts)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - days_back,
                        0, 0, 0, 0, 0, -1))


TODAY0 = local_midnight()
DAY1 = TODAY0 - 86400
DAY2 = TODAY0 - 2 * 86400
TODAY_KEY = time.strftime("%Y-%m-%d", time.localtime(FROZEN))
DAY1_KEY = time.strftime("%Y-%m-%d", time.localtime(DAY1))
DAY2_KEY = time.strftime("%Y-%m-%d", time.localtime(DAY2))


def row(at, model, account, prompt, completion, realm="intl", key="k1",
        outcome="completed", credit=0.0, speed=120.0):
    r = {"at": at, "model": model, "stream": True, "outcome": outcome,
         "elapsed_ms": 1000, "ttft_ms": 300, "gen_ms": 700,
         "prompt_tokens": prompt, "completion_tokens": completion,
         "reasoning_tokens": 0, "cached_tokens": 0,
         "account": account, "key": key, "tokens_per_sec": speed,
         "credit": credit}
    if realm is not None:
        r["realm"] = realm
    if outcome != "completed":
        r["error"] = "boom"
    r["total_tokens"] = prompt + completion
    return r


ROWS = [
    # 前天（DAY2）：一行就在午夜整点——它属于 DAY2 而不是 DAY1。
    row(DAY2, "m-a", "u1", 100, 10),
    row(DAY2 + 3600, "m-b", "u2", 200, 20, realm="cn"),
    # 昨天（DAY1）：含 00:00:00 与 23:59:59 两行（都属于 DAY1）。
    row(DAY1, "m-a", "u1", 300, 30, credit=0.5),
    row(DAY1 + 86399, "m-a", "u2", 400, 40, key="k2"),
    row(DAY1 + 3600, "m-c", "u1", 500, 50, outcome="failed", credit=0.25),
    # 今天（TODAY0）：跨 realm、无价模型、客户端中止、无 key 字段的历史行。
    row(TODAY0, "m-a", "u1", 600, 60, credit=1.0),
    row(TODAY0 + 100, "m-b", "u2", 700, 70, realm="cn", speed=90.0),
    row(TODAY0 + 200, "m-a", "u2", 800, 80, outcome="client_aborted"),
    row(TODAY0 + 300, "no-price-model", "u1", 900, 90),
]
_pre_key = row(TODAY0 + 400, "m-c", "u1", 110, 11)
del _pre_key["key"]
ROWS.append(_pre_key)
_no_realm = row(TODAY0 + 500, "m-c", "u2", 120, 12, realm=None)
ROWS.append(_no_realm)

# 价目表刻意用二进制可精确表示的数（0.5 / 0.25 / 0.125）与 unit=1：
# 逐行累加与按日累加因此都精确，多天窗口也能逐字节比对（见文件头）。
PRICING = {
    "meta": {"usd_cny": 7.1},
    "models": {
        "m-a": {"unit": 1, "currency": "CNY",
                "flat": {"input_cache_hit": 0.25, "input_cache_miss": 0.5,
                         "output": 0.125}},
        "m-b": {"unit": 1, "currency": "CNY",
                "flat": {"input_cache_hit": 0.125, "input_cache_miss": 0.25,
                         "output": 0.0625}},
    },
}
PRICING_FILE = os.path.join(_TMP, "pricing.json")
os.environ["WB_PRICING_FILE"] = PRICING_FILE
_MTIME = [FROZEN - 1000000.0]


def write_pricing(doc):
    io.open(PRICING_FILE, "w", encoding="utf-8").write(
        json.dumps(doc, ensure_ascii=False))
    _MTIME[0] += 3600.0
    os.utime(PRICING_FILE, (_MTIME[0], _MTIME[0]))


def write_log(rows):
    with io.open(USAGE_LOG, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def append_log(rows):
    with io.open(USAGE_LOG, "a", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------
# 开关（WB_USAGE_CACHE = checkpoint；WB_USAGE_DAY_BUCKETS = 日桶）
# --------------------------------------------------------------------------
def cache_env(enabled=None, min_bytes=None, min_seconds=None, days=None,
              keep=None):
    for name, value in (("WB_USAGE_CACHE", enabled),
                        ("WB_USAGE_CACHE_MIN_BYTES", min_bytes),
                        ("WB_USAGE_CACHE_MIN_SECONDS", min_seconds),
                        ("WB_USAGE_DAY_BUCKETS", days),
                        ("WB_USAGE_DAY_KEEP_DAYS", keep)):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = str(value)


def cache_env_nowrite():
    cache_env(enabled=1, min_bytes=10 ** 9, min_seconds=10 ** 9, days=None)


def cache_env_force():
    cache_env(enabled=1, min_bytes=0, min_seconds=0, days=None)


def cache_env_off():
    """改动前的口径：checkpoint 关掉、日桶关掉（= 逐行扫描 + 全量冷折）。"""
    cache_env(enabled=0, min_bytes=0, min_seconds=0, days=0)


def restart():
    """清掉所有状态、TTL 缓存与「已读过 checkpoint」的记忆，模拟进程重启。"""
    U._usage_snap_state.clear()
    U._byacct_state.update({"buckets": None, "offset": 0, "key": None, "tail": b""})
    U._analytics_state.clear()
    U._series_state.clear()
    U._snap_cache.clear()
    U._byacct_cache.update({"at": 0.0, "data": None})
    U._analytics_cache.clear()
    U._series_cache.clear()
    U._usage_cache_forget()
    U._usage_cache_progress.clear()
    U._usage_cache_checkpointed.clear()
    U._usage_cache_last_attempt = time.time()
    U._usage_cache_digest_memo.clear()


def window_start(name, now=None):
    """按 range_window 的规则取窗口起点，但锚在给定时刻（默认冻结时钟）。

    range_window 的 week/month 读的是**系统时钟**：`time.localtime()` 不带参数
    走的是 C 的 time()，补 time.time 改不到它，week 于是等于「冻结日期减真实
    星期几」、month 等于「当前真实月份」。套件把时间冻在 2026-10-08、真实日期
    却是跑的那天，直接调 range_window 会让窗口随真实日期漂移（跑到 11 月、
    或恰好在周一导致 week 与 today 重合），断言就会时灵时不灵。这里按同一套
    规则、用冻结时钟现算，窗口因此是确定的。
    """
    if now is None:
        now = FROZEN
    if name == "today":
        return U._local_midnight(now)
    lt = time.localtime(now)
    if name == "week":
        return U._local_midnight(now, days_back=lt.tm_wday)
    return time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))


def window_series(name, now=None, **kwargs):
    """窗口序列：走公开入口，窗口由 window_start() 现算后显式传入。

    usage_timeseries 的 range 别名同样依赖系统时钟，所以这里用 range="custom"
    + since（until 缺省）——与别名等价（起点是本地午夜），但确定。
    """
    lo = window_start(name, now)
    return U.usage_timeseries(realm="all", range="custom", since=lo, until=None,
                              ttl=0, **kwargs)


def live():
    """公开入口的窗口结果（三个窗口的 snapshot/analytics/timeseries + all）。"""
    out = {}
    for name in ("today", "week", "month"):
        lo = window_start(name)
        out["snap_" + name] = U._usage_snapshot_uncached(since=lo, until=None)
        out["an_" + name] = U._compute_usage_analytics_uncached(since=lo,
                                                               until=None)
        out["ts_" + name] = window_series(name)
    out["snap_all"] = U._usage_snapshot_uncached()
    out["an_all"] = U._compute_usage_analytics_uncached()
    out["byacct"] = U._usage_by_account_uncached()
    return out


def deep(obj):
    """一份结构深拷贝（比较「改动前」的快照用；活状态会被就地改）。"""
    return json.loads(canon(obj))


RUNNER = r'''
import json, os, sys
import time as _t
FROZEN = __FROZEN__
_t.time = lambda: FROZEN
sys.path.insert(0, __ROOT__)
import wb_proxy as P
import wb_usage as U
import wb_pricing
wb_pricing.set_data_dir(P.USAGE_DIR)
wb_pricing.set_settings_dir(P.ACCOUNTS_DIR)


def window_start(name):
    """与父进程同一套窗口算法（range_window 的 week/month 读系统时钟，
    子进程里也一样漂；这里锚在冻结时钟上）。"""
    if name == "today":
        return U._local_midnight(FROZEN)
    lt = _t.localtime(FROZEN)
    if name == "week":
        return U._local_midnight(FROZEN, days_back=lt.tm_wday)
    return _t.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))


out = {}
for name in ("today", "week", "month"):
    lo = window_start(name)
    out["snap_" + name] = U._usage_snapshot_uncached(since=lo, until=None)
    out["an_" + name] = U._compute_usage_analytics_uncached(since=lo, until=None)
    out["ts_" + name] = U.usage_timeseries(realm="all", range="custom", since=lo,
                                           until=None, ttl=0)
out["snap_all"] = U._usage_snapshot_uncached()
out["an_all"] = U._compute_usage_analytics_uncached()
out["byacct"] = U._usage_by_account_uncached()
with open(os.environ["WB_CHILD_OUT"], "w", encoding="utf-8") as fh:
    json.dump({"checkpointed": U._usage_cache_checkpointed, "results": out}, fh,
              ensure_ascii=False)
print("child ok")
'''
CHILD_SCRIPT = os.path.join(_TMP, "child_runner.py")
io.open(CHILD_SCRIPT, "w", encoding="utf-8", newline="\n").write(
    RUNNER.replace("__FROZEN__", repr(FROZEN)).replace("__ROOT__", repr(ROOT)))


def run_child(extra_env=None):
    dest = os.path.join(_TMP, "child_out.json")
    if os.path.exists(dest):
        os.unlink(dest)
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["WB_CHILD_OUT"] = dest
    env.update(extra_env or {})
    done = subprocess.run([sys.executable, CHILD_SCRIPT], cwd=ROOT, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=300)
    out = done.stdout.decode("utf-8", "replace")
    if done.returncode != 0 or not os.path.exists(dest):
        raise AssertionError("子进程失败 rc=%s\n%s" % (done.returncode, out[-2000:]))
    with io.open(dest, encoding="utf-8") as fh:
        payload = json.load(fh)
    return payload["results"], payload["checkpointed"]


def cache_data():
    """五个 kind 文件的合并视图：{kind: entries}（缺失/读不出的当空表）。

    与 v3 的单文件视图同形，用例里 data[kind] / days_path(...) 的写法不用改。
    """
    merged = {}
    for kind in U._USAGE_CACHE_KINDS:
        try:
            with io.open(KIND_FILES[kind], encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            merged[kind] = []
            continue
        entries = data.get("entries") if isinstance(data, dict) else None
        merged[kind] = entries if isinstance(entries, list) else []
    return merged


def save_cache(data):
    """把合并视图写回 kind 文件；空表不写文件（与生产侧写盘一致）。"""
    for kind in U._USAGE_CACHE_KINDS:
        entries = data.get(kind) or []
        if not entries:
            continue
        payload = {"schema": U._USAGE_CACHE_SCHEMA, "kind": kind,
                   "written_at": FROZEN, "entries": entries}
        io.open(KIND_FILES[kind], "w", encoding="utf-8", newline="\n").write(
            json.dumps(payload, ensure_ascii=False))


def make_checkpoint():
    cache_env_force()
    restart()
    live()
    return any(os.path.exists(path) for path in KIND_FILES.values())


def days_path(data, kind, realm=None):
    for e in data[kind]:
        if kind == "by_account" or e.get("realm") == realm:
            return e
    raise AssertionError("没有 %s/%r" % (kind, realm))


def newest_day(data, kind, realm=None):
    """该 entry 里最新的那个日桶（= 今天那一桶；窗口一定包含它）。"""
    days = days_path(data, kind, realm)["days"]
    return days[sorted(days)[-1]]


# --------------------------------------------------------------------------
# 基线
# --------------------------------------------------------------------------
write_pricing(PRICING)
write_log(ROWS)
cache_env_off()
restart()
GOLDEN_RAW = live()
GOLDEN = canon(GOLDEN_RAW)
GOLDEN_OBJ = json.loads(GOLDEN)
# 用 >= 而不是 >：三个窗口都由 window_start() 锚在冻结时钟上现算，而冻结那天
# 本身可能是周一——那时 week 与 today 天然重合，不该让用例在这一天挂。
check("基线：三个窗口都有内容，窗口越宽覆盖的行只多不少",
      GOLDEN_OBJ["snap_today"]["requests"] > 0
      and GOLDEN_OBJ["snap_week"]["requests"] >= GOLDEN_OBJ["snap_today"]["requests"]
      and GOLDEN_OBJ["an_month"]["summary"]["window"]["total_tokens"]
      >= GOLDEN_OBJ["an_week"]["summary"]["window"]["total_tokens"] > 0,
      (GOLDEN_OBJ["snap_today"]["requests"], GOLDEN_OBJ["snap_week"]["requests"],
       GOLDEN_OBJ["an_month"]["summary"]["window"]["total_tokens"],
       GOLDEN_OBJ["an_week"]["summary"]["window"]["total_tokens"]))
check("基线（关掉日桶）：状态里一个日桶都没有",
      not U._usage_snap_state[None]["days"]
      and not U._analytics_state[None]["days"]
      and not U._series_state.get(None, {}).get("days"),
      (U._usage_snap_state[None]["days"], U._analytics_state[None]["days"]))

print()
print("[1] 切分：每行折进它 at 所属的本地日（午夜整点、迟写行、无价行）")
cache_env_nowrite()
restart()
live()
snap_days = deep(U._usage_snap_state[None]["days"])
an_days = U._analytics_state[None]["days"]
ser_days = U._series_state[None]["days"]
check("snapshot 的日桶键 == 日志里出现过的本地日",
      sorted(snap_days) == [DAY2_KEY, DAY1_KEY, TODAY_KEY], sorted(snap_days))
check("analytics / series 的日桶键一致",
      sorted(an_days) == sorted(snap_days) == sorted(ser_days),
      (sorted(an_days), sorted(ser_days)))
# days_floor 是剪枝线：最新那天往前保留天数；更早的窗口退回扫描。
want_floor = time.strftime("%Y-%m-%d",
                           time.localtime(TODAY0 - U._usage_day_keep_days() * 86400))
check("days_floor 落在剪枝线上（最新那天往前保留天数）",
      U._usage_snap_state[None]["days_floor"] == want_floor,
      (U._usage_snap_state[None]["days_floor"], want_floor))
# 午夜整点的行归新的一天：DAY2 00:00:00 在 DAY2 桶里，不在 DAY1。
check("午夜整点的行属于新的一天",
      snap_days[DAY2_KEY]["requests"] == 2
      and snap_days[DAY1_KEY]["requests"] == 2,
      (snap_days[DAY2_KEY]["requests"], snap_days[DAY1_KEY]["requests"]))
# 每桶令牌数 == 手工按日分组（completed-only，与折叠口径一致）
want = {DAY2_KEY: 100 + 10 + 200 + 20, DAY1_KEY: 300 + 30 + 400 + 40}
check("每天桶的 total_tokens 与手工分组一致",
      {DAY2_KEY: snap_days[DAY2_KEY]["total_tokens"],
       DAY1_KEY: snap_days[DAY1_KEY]["total_tokens"]} == want,
      ({DAY2_KEY: snap_days[DAY2_KEY]["total_tokens"],
        DAY1_KEY: snap_days[DAY1_KEY]["total_tokens"]}, want))
# 小时子桶：TODAY0 + 500 落在第 0 个小时（TODAY0 起 500 秒 < 3600）。
hours = ser_days[TODAY_KEY]["hours"]
check("小时子桶按「离本地午夜多少秒」切（键是字符串）",
      sorted(hours, key=int)[0] == "0" and all(isinstance(h, str) for h in hours),
      sorted(hours, key=int))
check("小时子桶的 at 与日桶对齐（日桶 at + 小时*3600）",
      all(int(hours[h]["at"]) == int(ser_days[TODAY_KEY]["at"]) + int(h) * 3600
          for h in hours), {h: hours[h]["at"] for h in sorted(hours, key=int)})
# 迟写行：把一条 at 属于昨天的行追加到今天，它必须落进昨天的桶。
append_log([row(DAY1 + 7200, "m-a", "u1", 1000, 100, credit=0.5)])
live()
late_days = U._usage_snap_state[None]["days"]
check("迟写行落进它时间戳所属的那天（不是折叠时刻那天）",
      late_days[DAY1_KEY]["requests"] == snap_days[DAY1_KEY]["requests"] + 1
      and late_days[TODAY_KEY]["requests"] == snap_days[TODAY_KEY]["requests"],
      (late_days[DAY1_KEY]["requests"], snap_days[DAY1_KEY]["requests"]))
write_log(ROWS)
cache_env_off()
restart()
GOLDEN_RAW = live()
GOLDEN = canon(GOLDEN_RAW)
GOLDEN_OBJ = json.loads(GOLDEN)

print()
print("[2] 等价：窗口 = 日桶之和，与改动前的逐行口径逐字段一致")
# 测试数据的价是二进制精确值，所以多天窗口也逐字节相同。
for name in ("today", "week", "month"):
    cache_env_off()
    restart()
    ref = canon(live())
    cache_env_nowrite()
    restart()
    got = canon(live())
    check("窗口 %s：日桶路径 == 逐行口径（整包逐字节）" % name, ref == got,
          first_diff(ref, got))
# 日桶状态不可用时现折的那份（_scan_*_window_days）也必须与有日桶时一致：
# 清掉状态后先折出一份 all-time（窗口请求本身不折——冷状态直接走扫描，
# 见 T14 的 _usage_snap_state_cold），再把结果与有日桶时的窗口比。
lo = window_start("week")
day_key = time.strftime("%Y-%m-%d", time.localtime(lo))
cache_env_nowrite()
restart()
U._usage_snapshot_uncached()
from_state = U._empty_stats()
for k in sorted(U._usage_snap_state[None]["days"]):
    if k >= day_key:
        U._add_usage_snapshot(from_state, U._usage_snap_state[None]["days"][k])
COLD_SNAP = U._scan_usage_snapshot_window_days(None, lo, None)
check("冷路径（现折日桶）的 snapshot == 有日桶时相加的结果",
      canon(COLD_SNAP) == canon(from_state),
      first_diff(canon(from_state), canon(COLD_SNAP)))
cache_env_nowrite()
restart()
U._compute_usage_analytics_uncached(since=lo, until=None)
from_state = U._new_analytics_maps()
for k in sorted(U._analytics_state[None]["days"]):
    if k >= day_key:
        U._add_analytics_window_maps(from_state, U._analytics_state[None]["days"][k])
check("冷路径（现折日桶）的 analytics 窗口半 == 有日桶时相加的结果",
      canon(U._scan_usage_analytics_window_days(None, lo, None)) == canon(from_state),
      first_diff(canon(from_state),
                 canon(U._scan_usage_analytics_window_days(None, lo, None))))

print()
print("[3] 封口：越过的那天不再重折（对象原样复用），只有今天在长")
cache_env_nowrite()
restart()
live()
before_objs = dict(U._usage_snap_state[None]["days"])
before = deep(U._usage_snap_state[None]["days"])
append_log([row(TODAY0 + 600, "m-a", "u1", 2000, 200, credit=0.5)])
live()
after = U._usage_snap_state[None]["days"]
sealed = [k for k in before if k != TODAY_KEY]
check("旧日的桶对象原样复用（同一个 dict，不是重建的）",
      all(after[k] is before_objs[k] for k in sealed),
      [k for k in sealed if after[k] is not before_objs[k]])
check("旧日的桶内容一字未变",
      all(canon(after[k]) == canon(before[k]) for k in sealed),
      [k for k in sealed if canon(after[k]) != canon(before[k])])
check("今天那一桶确实长了",
      after[TODAY_KEY]["requests"] == before[TODAY_KEY]["requests"] + 1,
      (after[TODAY_KEY]["requests"], before[TODAY_KEY]["requests"]))
write_log(ROWS)
cache_env_off()
restart()
GOLDEN_RAW = live()
GOLDEN = canon(GOLDEN_RAW)

print()
print("[4] checkpoint：日桶随状态落盘/加载，重启后逐字节一致")
check("写盘成功", make_checkpoint())
stored = cache_data()
check("每个 kind 文件都是 schema=当前版本（v5）的 JSON 对象",
      all(json.load(io.open(KIND_FILES[k], encoding="utf-8"))["schema"]
          == U._USAGE_CACHE_SCHEMA and U._USAGE_CACHE_SCHEMA == 5
          for k in U._USAGE_CACHE_KINDS if os.path.exists(KIND_FILES[k])),
      U._USAGE_CACHE_SCHEMA)
check("四个 kinds 都在",
      all(len(stored[k]) >= 1 for k in
          ("snapshot", "analytics", "series", "by_account")),
      {k: len(v) for k, v in stored.items() if isinstance(v, list)})
snap_entry = days_path(stored, "snapshot", None)
check("snapshot 条目带日桶与 days_floor（剪枝线）",
      isinstance(snap_entry.get("days"), dict) and snap_entry["days"]
      and snap_entry.get("days_floor") == want_floor,
      (sorted((snap_entry.get("days") or {}).keys()), snap_entry.get("days_floor")))
an_day = newest_day(stored, "analytics", None)
check("analytics 的日桶是窗口形状（window / window_models，没有 all_time）",
      all("window" in e and "all_time" not in e
          for e in an_day["models"].values()),
      list(an_day["models"].values())[:1])
ser_entry = days_path(stored, "series", None)
check("series 条目带 max_at / credits / 小时子桶",
      isinstance(ser_entry.get("max_at"), (int, float)) and ser_entry["credits"]
      and all(isinstance(h, str) for h in newest_day(stored, "series")["hours"]),
      (ser_entry.get("max_at"), len(ser_entry.get("credits") or [])))
day_keys = list(newest_day(stored, "snapshot")["by_model"].keys())
check("日桶里的键序是折叠插入顺序（不是字母序）",
      day_keys and day_keys != sorted(day_keys), day_keys[:5])

cache_env_nowrite()
restart()
LOADED_RAW = live()
ADOPTED = dict(U._usage_cache_checkpointed)
check("重启后加载到了 checkpoint（含 series）",
      set(ADOPTED) >= {"snapshot", "analytics", "series"}, ADOPTED)
check("加载后窗口与冷折逐字节一致", canon(LOADED_RAW) == GOLDEN,
      first_diff(GOLDEN, canon(LOADED_RAW)))

print()
print("[5] 真·新进程：加载 checkpoint 后窗口一致，且确实走了加载路径")
child_raw, child_state = run_child()
check("新进程结果与冷折一致", canon(child_raw) == GOLDEN,
      first_diff(GOLDEN, canon(child_raw)))
check("新进程记录到了加载的 offset（含 series）",
      set(child_state) >= {"snapshot", "analytics", "series"}, child_state)

print()
print("[6] 故障注入：坏日桶一律当作没有缓存，结果回到真值")


def mutate(mutate_fn, label, kind="snapshot"):
    """先给该 entry 最新的日桶加一个可见标记，再变异——结果必须回到真值。

    标记落在今天那一桶：三个窗口都包含它，只要这份状态被采用，结果里就会
    多出 1000 次请求，用例当场失败。
    """
    make_checkpoint()
    data = cache_data()
    day = newest_day(data, kind)
    if kind == "analytics":
        day["summary"]["requests"] += 1000
    else:
        day["requests"] += 1000
    mutate_fn(data)
    save_cache(data)
    cache_env_nowrite()
    restart()
    got = canon(live())
    check(label, got == GOLDEN, first_diff(GOLDEN, got))


def mutate_schema(mutate_fn, label):
    """把每个 kind 文件的顶层形状改坏（v4 之前是「旧文件整体作废」）。

    标记照旧落在 snapshot 今天的日桶里：文件被拒 → 结果回到真值；万一被
    放行，结果里就会多出 1000 次请求。改坏的是文件顶层（schema 等），不是
    载荷，所以这一份必须整体不被采用。
    """
    make_checkpoint()
    data = cache_data()
    newest_day(data, "snapshot")["requests"] += 1000
    save_cache(data)
    for kind in U._USAGE_CACHE_KINDS:
        path = KIND_FILES[kind]
        if not os.path.exists(path):
            continue
        with io.open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        mutate_fn(raw)
        io.open(path, "w", encoding="utf-8", newline="\n").write(
            json.dumps(raw, ensure_ascii=False))
    cache_env_nowrite()
    restart()
    got = canon(live())
    check(label, got == GOLDEN, first_diff(GOLDEN, got))


# 6.0 自检：形状合法、值被改过的日桶会被采用（标记必须可见），否则下面的
# 用例可能什么都没测到。
make_checkpoint()
data = cache_data()
newest_day(data, "snapshot")["requests"] += 1000
save_cache(data)
cache_env_nowrite()
restart()
check("标记自检：形状合法的坏日桶会被采用（标记出现在结果里）",
      canon(live()) != GOLDEN, "结果没有变化")

# 6.1 形状被改坏
mutate(lambda d: newest_day(d, "snapshot").update({"requests": "很多"}),
       "日桶的数字字段被写成字符串")
mutate(lambda d: newest_day(d, "snapshot").pop("by_model"),
       "日桶少了 by_model")
mutate(lambda d: newest_day(d, "snapshot").update({"by_model": []}),
       "日桶的 by_model 不是对象")
mutate(lambda d: days_path(d, "snapshot", None).update({"days": []}),
       "days 不是对象")
mutate(lambda d: days_path(d, "snapshot", None).pop("days"),
       "旧格式 entry（没有 days）")
mutate(lambda d: days_path(d, "snapshot", None)["days"].update({"2026-13-99": {}}),
       "日键不是合法日期")
mutate(lambda d: days_path(d, "snapshot", None)["days"].update({"20261008": {}}),
       "日键长度不对")
mutate(lambda d: days_path(d, "snapshot", None).update({"days_floor": 5}),
       "days_floor 不是日键")
mutate(lambda d: days_path(d, "snapshot", None).update(
    {"days_floor": "2099-01-01"}),
    "days_floor 是合法日键：被采用，早于它的窗口退回扫描，结果仍正确")
mutate(lambda d: newest_day(d, "analytics").update(
    {"summary": {"requests": 1}}),
    "analytics 日桶的 summary 形状不对", kind="analytics")
mutate(lambda d: newest_day(d, "analytics")["summary"].update({"requests": None}),
       "analytics 日桶的字段类型不对", kind="analytics")
mutate(lambda d: newest_day(d, "series").update({"hours": []}),
       "series 日桶的 hours 不是对象", kind="series")
mutate(lambda d: newest_day(d, "series")["hours"].update({"x": {}}),
       "series 的小时键不是数字", kind="series")
mutate(lambda d: days_path(d, "series", None).update({"max_at": "now"}),
       "series 的 max_at 不是数字", kind="series")
mutate(lambda d: days_path(d, "series", None).update({"credits": "x"}),
       "series 的 credits 不是列表", kind="series")
mutate(lambda d: days_path(d, "series", None)["credits"].__setitem__(
    0, {"at": "x", "iso": "", "model": "", "account": "", "credit": 1,
        "total_tokens": 1}),
    "series 的 credit 条目形状不对", kind="series")
mutate(lambda d: newest_day(d, "series")["hours"].__setitem__(
    "0", dict(newest_day(d, "series")["hours"]["0"], at="x")),
    "series 的小时子桶 at 不是数字", kind="series")

# 6.2 schema：旧文件整体作废（一次性全量重扫，结果正确）
mutate_schema(lambda d: d.update({"schema": 2}), "旧 schema（v2，没有日桶）")
mutate_schema(lambda d: d.update({"schema": 4}), "旧 schema（v4，价格指纹是旧形状）")
mutate_schema(lambda d: d.update({"schema": 6}), "未来的 schema")
mutate_schema(lambda d: d.update({"schema": True}), "schema 写成 true")

print()
print("[7] 剪枝：保留天数以外的天被丢掉，够不到的窗口退回扫描")
# keep=2：剪枝线 = 最新那天往前 2 天，线上的那天保留（实际留 3 天）。
cache_env(enabled=1, min_bytes=10 ** 9, min_seconds=10 ** 9, keep=2)
restart()
live()
days = U._usage_snap_state[None]["days"]
keep_floor = time.strftime("%Y-%m-%d",
                           time.localtime(TODAY0 - 2 * 86400))
check("保留 2 天：剪枝线以下的天被丢掉（线上的那天保留）",
      sorted(days) == [DAY2_KEY, DAY1_KEY, TODAY_KEY]
      and keep_floor == DAY2_KEY, (sorted(days), keep_floor))
check("days_floor 抬到剪枝线",
      U._usage_snap_state[None]["days_floor"] == keep_floor,
      U._usage_snap_state[None]["days_floor"])
# 一条远古的补写行（剪枝线以外）：折不进去，但也不影响窗口。
append_log([row(DAY2 - 10 * 86400, "m-a", "u1", 5000, 500)])
live()
check("剪枝线以外的迟到行被丢掉（不建桶、不改 days_floor）",
      sorted(U._usage_snap_state[None]["days"])
      == [DAY2_KEY, DAY1_KEY, TODAY_KEY],
      sorted(U._usage_snap_state[None]["days"]))
write_log(ROWS)
lo = window_start("month")
got_month = canon(U._usage_snapshot_uncached(since=lo, until=None))
cache_env_off()
restart()
want_month = canon(U._usage_snapshot_uncached(since=lo, until=None))
check("剪枝线以外的窗口退回扫描，结果与关掉日桶时一致",
      got_month == want_month, first_diff(want_month, got_month))
cache_env(enabled=1, min_bytes=10 ** 9, min_seconds=10 ** 9, keep=2)
restart()
today = U._usage_snapshot_uncached(since=window_start("today"), until=None)
check("剪枝线以内的窗口仍由日桶服务（今天）",
      today["requests"] == GOLDEN_OBJ["snap_today"]["requests"],
      (today["requests"], GOLDEN_OBJ["snap_today"]["requests"]))
cache_env(keep=None)

print()
print("[8] 时间序列：日/小时桶切片 == 改动前的整段扫描（逐字节）")
# 60 条 credit 行：缓冲要裁到 50，且与扫描的选择逐条一致。
credit_rows = [row(TODAY0 + 1000 + i * 10, "m-a", "u1", 10, 1,
                   credit=0.5 + i * 0.25) for i in range(60)]
credit_rows.append(row(DAY1 + 5000, "m-a", "u1", 10, 1, credit=9.5))
credit_rows.append(row(DAY2 + 5000, "m-a", "u1", 10, 1, credit=9.75))
write_log(ROWS + credit_rows)
SCAN = {}
for name in ("today", "week", "month"):
    cache_env_off()
    restart()
    SCAN[name] = canon(window_series(name))
cache_env_nowrite()
restart()
for name in ("today", "week", "month"):
    got = canon(window_series(name))
    check("timeseries %s：切片 == 整段扫描（逐字节）" % name,
          got == SCAN[name], first_diff(SCAN[name], got))
got = window_series("week")
check("credits 恰好 50 条（缓冲裁过）", len(got["credits"]) == 50,
      len(got["credits"]))
check("credits 与扫描的前 50 条逐条相同（含 at 相同的稳定顺序）",
      canon(got["credits"]) == canon(json.loads(SCAN["week"])["credits"]),
      first_diff(canon(json.loads(SCAN["week"])["credits"]),
                 canon(got["credits"])))
# 自定义窗口（起点不是本地午夜）、显式桶宽、窗口上界早于折过的最大 at：
# 三条都必须退回整段扫描，结果与扫描一致。
for label, kwargs in (("自定义窗口（起点非午夜）",
                       dict(range="custom", since=TODAY0 + 100, until=FROZEN)),
                      ("显式桶宽（120s）",
                       dict(range="custom", since=window_start("today"),
                            bucket_seconds=120))):
    cache_env_off()
    restart()
    ref = canon(U.usage_timeseries(realm="all", ttl=0, **kwargs))
    cache_env_nowrite()
    restart()
    check(label + "：退回扫描，结果一致",
          canon(U.usage_timeseries(realm="all", ttl=0, **kwargs)) == ref)
append_log([row(FROZEN + 100000, "m-a", "u1", 10, 1)])
cache_env_off()
restart()
FUT = canon(window_series("today"))
cache_env_nowrite()
restart()
check("有未来行（hi < 折过的最大 at）时退回扫描，结果一致",
      canon(window_series("today")) == FUT)
write_log(ROWS + credit_rows)

print()
print("[9] 夏令时 / 日边界不规则：切片自己退回去")
cache_env_nowrite()
restart()
window_series("today")
state = U._series_state[None]
lo = window_start("today")
check("对齐窗口本来就走切片（不是退回）",
      U._series_slice(state, None, lo, FROZEN, 3600, TODAY_KEY) is not None)
state["days"][TODAY_KEY]["at"] += 3600        # 假装这一天少了/多了 3600 秒
check("日桶 at 不落在 86400 格点上时切片放弃（退回扫描）",
      U._series_slice(state, None, lo, FROZEN, 3600, TODAY_KEY) is None)
state["days"][TODAY_KEY]["at"] -= 3600
hours = state["days"][TODAY_KEY]["hours"]
h_key = sorted(hours, key=int)[0]
hours[h_key]["at"] += 60
check("小时子桶 at 与日桶对不上时切片放弃",
      U._series_slice(state, None, lo, FROZEN, 3600, TODAY_KEY) is None)
hours[h_key]["at"] -= 60
check("恢复后切片又能用",
      U._series_slice(state, None, lo, FROZEN, 3600, TODAY_KEY) is not None)
if hasattr(time, "tzset"):
    # 真实夏令时时区（Europe/Berlin 2026-10-25 结束夏令时，那天 25 小时）：
    # 冻结时钟挪到切换之后（10-28 12:00，晚于最后一行），造一批横跨切换日的
    # 行，再跑 today/week/month。
    #
    # 三条纪律，别把它们混成一条：
    #   * 与「整段扫描」比的是 timeseries 的**整份载荷**（公开入口给的就是
    #     这个形状），日桶开/关两条路径必须逐字节一致；
    #   * snapshot 比的是**同一种东西**——公开入口对公开入口（日桶开 vs 关）。
    #     拿 `_scan_usage_snapshot_window()` 的原始折叠去比公开入口是错的：
    #     后者还会补 started/since/log_file/realm/usd_cny/accounts_map/account
    #     这几个字段，任何时区、任何日期都比不平（Linux 上这段真的会跑，
    #     Windows 没有 tzset 所以一直没暴露）。
    #   * 先确认「本机真的被切到了柏林」再断言：有 zoneinfo 的系统认 IANA 名，
    #     只有 POSIX TZ 支持的系统（musl/OpenWrt 路由器这类没装 zoneinfo 的
    #     镜像）会把它悄悄降级成 UTC——那时日边界不再被挪动，断言就会以
    #     「切片竟然成立」这种误导性的方式失败。用 10-25 那天是不是 25 小时
    #     来验证，两个 TZ 值都做不到就跳过这段。
    ZONES = ("Europe/Berlin", "CET-1CEST,M3.5.0,M10.5.0/3")
    old_tz = os.environ.get("TZ")
    chosen = None
    for zone in ZONES:
        os.environ["TZ"] = zone
        time.tzset()
        if (time.mktime((2026, 10, 26, 0, 0, 0, 0, 0, -1))
                - time.mktime((2026, 10, 25, 0, 0, 0, 0, 0, -1))) == 90000:
            chosen = zone
            break
    try:
        if chosen is None:
            print("  [SKIP] 本机无法把进程切到柏林夏令时（%s 都不生效）"
                  % (", ".join(ZONES)))
        else:
            print("  [INFO] 夏令时用例使用 TZ=%s" % chosen)
            _time.time = lambda: time.mktime((2026, 10, 28, 12, 0, 0, 0, 0, -1))
            base = time.mktime((2026, 10, 22, 12, 0, 0, 0, 0, -1))
            dst_rows = [row(base + d * 86400 + h * 3600, "m-a", "u1", 100, 10)
                        for d in range(6) for h in (0, 6, 12, 18)]
            write_log(dst_rows)
            for name in ("today", "week", "month"):
                lo = window_start(name, time.time())
                step = 86400 if name == "month" else 3600
                scan = canon(U._usage_timeseries_scan(None, lo, time.time(), step))
                got = canon(window_series(name, time.time()))
                check("夏令时时区（%s）timeseries %s 与扫描一致" % (chosen, name),
                      got == scan, first_diff(scan, got))
                cache_env(enabled=1, min_bytes=10 ** 9, min_seconds=10 ** 9,
                          days=0)
                restart()
                ref = canon(U._usage_snapshot_uncached(since=lo, until=None))
                cache_env(enabled=1, min_bytes=10 ** 9, min_seconds=10 ** 9)
                restart()
                got_snap = canon(U._usage_snapshot_uncached(since=lo, until=None))
                check("夏令时时区 snapshot %s：日桶路径 == 逐行口径（逐字节）" % name,
                      got_snap == ref, first_diff(ref, got_snap))
            # 10-25 是切换日（那天 25 小时）：month（10-01 起）一定含它，
            # 日边界被挪过 3600 秒，切片必须放弃；today（10-28，切换日之后的
            # 普通一天）没有这个问题，切片照常可用。
            #
            # week 的起点随「这一周从哪天开始」变（见 window_start），所以期望
            # 值按 lo 现算：周起点落在 10-26 及以后时窗口内全是规则日边界
            # （切片可用），落在 10-25 及以前就含切换日（切片放弃）。
            regular_from = time.mktime((2026, 10, 26, 0, 0, 0, 0, 0, -1))
            week_lo = window_start("week", time.time())
            for name, expect_taken in (("month", False),
                                       ("week", week_lo >= regular_from),
                                       ("today", True)):
                lo = window_start(name, time.time())
                step = 86400 if name == "month" else 3600
                key = time.strftime("%Y-%m-%d", time.localtime(lo))
                cache_env(enabled=1, min_bytes=10 ** 9, min_seconds=10 ** 9)
                restart()
                window_series(name, time.time())
                sliced = U._series_slice(U._series_state[None], None, lo,
                                         time.time(), step, key)
                check("夏令时 %s：切片%s" % (name, "照常可用" if expect_taken
                                            else "放弃、退回扫描"),
                      (sliced is not None) == expect_taken,
                      None if sliced is None else "切片竟然成立了")
    finally:
        _time.time = lambda: FROZEN
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()
else:
    print("  [SKIP] 本平台没有 time.tzset，跳过真实夏令时时区用例")
# 恢复基线日志并重算基线（后面的用例都在这份日志上做）
write_log(ROWS)
cache_env_off()
restart()
GOLDEN_RAW = live()
GOLDEN = canon(GOLDEN_RAW)
GOLDEN_OBJ = json.loads(GOLDEN)

print()
print("[10] 浮点口径：价不是二进制精确值时，只有浮点求和顺序的差异")
# 0.1 这类价不是二进制精确数：多天窗口「按日相加」与「逐行相加」会有末位
# 差。这里把它钉成「只有浮点累加器允许 <=1e-9 相对误差，其余逐字节相同」
# ——这是日桶推导的固有代价（逐行顺序无法从日聚合还原），见 wb_proxy.py
# 里「按日分桶」一节的说明。
write_pricing({**PRICING, "models": {
    **PRICING["models"],
    "m-a": {"unit": 3, "currency": "CNY",
            "flat": {"input_cache_hit": 0.1, "input_cache_miss": 0.3,
                     "output": 0.7}},
    "m-b": {"unit": 7, "currency": "CNY",
            "flat": {"input_cache_hit": 0.13, "input_cache_miss": 0.29,
                     "output": 0.71}},
}})
lo = window_start("week")
cache_env_off()
restart()
ref = U._usage_snapshot_uncached(since=lo, until=None)
cache_env_nowrite()
restart()
got = U._usage_snapshot_uncached(since=lo, until=None)
diffs = float_diffs(ref, got, tol=1e-9)
check("多天窗口：结构与整数逐字段相同，浮点差异 <=1e-9 相对误差",
      not diffs, diffs[:6])
check("两种口径算的是同一批行（requests 相同）",
      ref["requests"] == got["requests"], (ref["requests"], got["requests"]))
lo = window_start("today")
cache_env_off()
restart()
ref1 = canon(U._usage_snapshot_uncached(since=lo, until=None))
cache_env_nowrite()
restart()
got1 = canon(U._usage_snapshot_uncached(since=lo, until=None))
check("单天窗口：非精确价也逐字节一致（桶只有一个，求和顺序相同）",
      ref1 == got1, first_diff(ref1, got1))
write_pricing(PRICING)

print()
print("[11] 开关：WB_USAGE_DAY_BUCKETS=0 时与改动前逐字节一致")
cache_env(enabled=1, min_bytes=10 ** 9, min_seconds=10 ** 9, days=0)
restart()
live()
check("关掉日桶：状态里一个日桶都不折",
      not U._usage_snap_state[None]["days"]
      and not U._analytics_state[None]["days"]
      and not U._series_state.get(None, {}).get("days"),
      (len(U._usage_snap_state[None]["days"]),
       len(U._analytics_state[None]["days"]),
       len(U._series_state.get(None, {}).get("days") or {})))
cache_env_off()
restart()
check("关掉日桶：结果与基线一致", canon(live()) == GOLDEN,
      first_diff(GOLDEN, canon(live())))
cache_env(days=None)
# 打开日桶但 checkpoint 关掉（WB_USAGE_CACHE=0）：日桶仍然工作、结果与基线
# 一致——两个开关互不依赖。
cache_env(enabled=0, min_bytes=0, min_seconds=0)
restart()
check("checkpoint 关掉但日桶开着：结果与基线一致",
      canon(live()) == GOLDEN)
check("checkpoint 关掉但日桶开着：日桶确实折了",
      bool(U._usage_snap_state[None]["days"]))
cache_env()

shutil.rmtree(_TMP, ignore_errors=True)
print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
