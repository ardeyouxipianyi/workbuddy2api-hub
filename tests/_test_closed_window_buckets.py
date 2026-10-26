"""闭窗口对齐走桶：两端都含的窗口（since/until 都给出）不再整段扫描。

改动前只有 until 开放的窗口能由存量桶回答：until 一给出，快照/analytics/
时间序列三条路都退回整段扫描（真机闭 30 天 13.3s、闭整点 1.07s、闭窗口
timeseries 1.1s）。闭窗口 [since, until] 的两端都含，而桶按 at 严格切分
（见 _usage_hour_bucket），所以「at < until 的那批桶」只差「恰好落在 until
上的行」——这一套钉住补上的边沿账（hits/hits_overflow）与它支撑的三条路：

  * 边沿账：折叠时凡 at 恰好落在整秒上的行进账（真实日志几乎为零，at 是
    time.time() 的浮点秒）；直到 until 没有整秒行时桶的和 = 闭窗口，真有一
    行落在 until 上（或账记满 256 条）就退回整段扫描，结果与改动前逐字节
    一致；
  * 对齐闭窗口走桶：日格点（两端都是本地午夜）、小时格点（两端都是本地
    整点）、分钟格点（step=60 的窗口）三档都由存量桶/环回答，计数桩证明
    一次都不进扫描路径；
  * 逐字节对拍：桶路径 vs 按日/按小时分组的冷路径 vs 改动前的整段口径
    （关掉日桶），三份逐字节相同；
  * 边界行：at 恰好 = since（左闭，必须计入）、= 整点/午夜（窗口内，照常
    计入）、= until（右闭，必须计入且闸门必须拦住桶路径、退回扫描）；
  * 未对齐：起点或上界不落格点 → 退回扫描，结果与对照一致；
  * 账溢出：整秒行超过 256 条 → 闭窗口全部退回扫描（正确性优先）；
  * checkpoint：hits/hits_overflow 随三份状态落盘/加载，重启采用后闭窗口
    结果不变、账在；旧格式条目（没有这两个键）被拒 → 升级后一次性重折；
    坏形状（非列表/浮点/布尔/int 溢出标记）一律拒绝；
  * 夏令时假时区：参照 _test_usage_day_buckets.py §[9] 的做法（TZ=Berlin +
    tzset，本机做不到就 SKIP）；另外用「把日桶 at 挪 3600 秒」的变异在所有
    平台钉住「格点被挪动 → 切片自己退回去」。

No network: the usage log, the pricing table and the settings file are
synthesised in a temp directory.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# 与 _test_usage_day_buckets.py / _test_usage_hour_buckets.py 同一把冻结时钟：
# 窗口边界（本地午夜/整点）与 started/since 都从 time.time() 派生，不冻结就
# 不可比。本地时间是 2026-10-08 15:40。
FROZEN = 1791445200.0

_TMP = tempfile.mkdtemp(prefix="wb-closed-")
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
# checkpoint 在 v4 里一类一份文件：这里给出合并视图与按类读写，用例里
# data["snapshot"] / data["series"] 的写法不用改。
KIND_FILES = {kind: U._usage_cache_kind_path(kind) for kind in U._USAGE_CACHE_KINDS}


def cache_files():
    return [p for p in KIND_FILES.values() if os.path.exists(p)]


def clear_cache_files():
    for path in KIND_FILES.values():
        if os.path.exists(path):
            os.unlink(path)
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


# --------------------------------------------------------------------------
# 数据：三个本地日 + 三个整秒边界行（= 窗口起点 / 小时窗口起点 / 窗口内整点）
# 普通行的 at 都带小数（+0.25/+0.5/+0.75）：边沿账只收整秒行，普通行不该
# 进账（进了账也只是让更多窗口退回扫描，但这里的用例要钉住账的内容）。
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

# 窗口（都是本地格点；MIN_* 是分钟格点，step=60 的窗口）：
DAY_LO, DAY_HI = DAY1, TODAY0                 # 两个本地午夜
DAY2_LO = DAY2                                # 两天窗（窗口内含一个午夜）
HOUR_LO = TODAY0 + 5 * 3600                   # 05:00（本地整点）
HOUR_HI = TODAY0 + 7 * 3600                   # 07:00
MIN_LO = TODAY0 + 5 * 3600 + 1800             # 05:30（60 的整数倍）
MIN_HI = TODAY0 + 6 * 3600 + 1800             # 06:30
MIN_HI_EDGE = TODAY0 + 6 * 3600               # 06:00：日志里有整秒行 = until


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
    row(DAY2 + 3600 + 0.25, "m-a", "u1", 100, 10, credit=0.5),
    row(DAY1 + 1800 + 0.5, "m-a", "u1", 200, 20),
    # 边界：at 恰好 = 日窗口起点（本地午夜）——左闭，必须计入。
    row(DAY1, "m-b", "u2", 300, 30, realm="cn", credit=0.25),
    row(DAY1 + 7200 + 0.75, "m-b", "u2", 400, 40, realm="cn"),
    row(DAY1 + 23 * 3600 + 0.25, "m-a", "u1", 500, 50, credit=0.125),
    # 边界：at 恰好 = 小时窗口起点（本地整点）——左闭，必须计入。
    row(HOUR_LO, "m-a", "u1", 600, 60, credit=0.5),
    row(HOUR_LO + 600 + 0.25, "m-b", "u2", 700, 70, realm="cn"),
    row(HOUR_LO + 1800 + 0.5, "m-a", "u2", 800, 80),
    # 边界：窗口内的本地整点（06:00）——照常计入，同时把「= until」的用例
    # 钉在它身上（MIN_HI_EDGE 就是 06:00）。
    row(MIN_HI_EDGE, "m-b", "u1", 900, 90, credit=0.25),
    row(TODAY0 + 6 * 3600 + 61 + 0.25, "m-a", "u1", 110, 11),
    row(TODAY0 + 6 * 3600 + 1800 + 0.75, "m-c", "u2", 120, 12),
    row(HOUR_HI - 0.25, "m-c", "u1", 130, 13),
]
# 整秒行（按日志顺序）：边沿账的内容就是这三个。
INT_ROWS = [DAY1, HOUR_LO, MIN_HI_EDGE]

# 价目表刻意用二进制可精确表示的数（0.5 / 0.25 / 0.125）与 unit=1：
# 逐行累加与按桶累加因此都精确，逐字节比对成立（见文件头）。
PRICING = {
    "meta": {"usd_cny": 7.1},
    "models": {
        "m-a": {"unit": 1, "currency": "CNY",
                "flat": {"input_cache_hit": 0.25, "input_cache_miss": 0.5,
                         "output": 0.125}},
        "m-b": {"unit": 1, "currency": "CNY",
                "flat": {"input_cache_hit": 0.125, "input_cache_miss": 0.25,
                         "output": 0.0625}},
        "m-c": {"unit": 1, "currency": "CNY",
                "flat": {"input_cache_hit": 0.5, "input_cache_miss": 1.0,
                         "output": 0.25}},
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
# 开关与重启
# --------------------------------------------------------------------------
def cache_env(enabled=None, min_bytes=None, min_seconds=None, days=None):
    for name, value in (("WB_USAGE_CACHE", enabled),
                        ("WB_USAGE_CACHE_MIN_BYTES", min_bytes),
                        ("WB_USAGE_CACHE_MIN_SECONDS", min_seconds),
                        ("WB_USAGE_DAY_BUCKETS", days)):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = str(value)


def cache_env_nowrite():
    """日桶/环开着、checkpoint 关掉（状态每轮重折，不落盘）。"""
    cache_env(enabled=0, min_bytes=0, min_seconds=0, days=None)


def cache_env_force():
    cache_env(enabled=1, min_bytes=0, min_seconds=0, days=None)


def cache_env_read():
    """checkpoint 可读（采用路径）但写盘被节流挡住：文件保持手写的那一份。"""
    cache_env(enabled=1, min_bytes=10 ** 9, min_seconds=10 ** 9, days=None)


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


# --------------------------------------------------------------------------
# 三个视图的取窗口入口
# --------------------------------------------------------------------------
def window_raw(since, until=None, realm=None):
    """窗口半的原始折叠（走桶：两端落格点时由存量桶求和，否则冷路径）。"""
    return U._usage_snapshot_window(realm, since, until)


def snap_window(since, until=None, realm=None):
    """公开入口的窗口结果（带 started / accounts_map 等装饰，冻结时钟后可比）。"""
    return U._usage_snapshot_uncached(realm, since=since, until=until)


def series_window(since, until=None, bucket=None, realm="all"):
    return U.usage_timeseries(realm=realm, range="custom", since=since,
                              until=until, bucket_seconds=bucket, ttl=0)


def an_window(since, until=None, realm=None):
    return U._compute_usage_analytics_uncached(realm, since=since, until=until)


def ref_window(since, until=None, realm=None):
    """改动前的口径（日桶关掉 = 整段扫描）下同一窗口的窗口半。"""
    cache_env_off()
    restart()
    out = window_raw(since, until, realm)
    cache_env_nowrite()
    restart()
    return out


def ref_series(since, until=None, bucket=None, realm="all"):
    cache_env_off()
    restart()
    out = series_window(since, until, bucket, realm)
    cache_env_nowrite()
    restart()
    return out


def ref_an(since, until=None, realm=None):
    cache_env_off()
    restart()
    out = an_window(since, until, realm)
    cache_env_nowrite()
    restart()
    return out


class ScanCounter(object):
    """计数桩：包住四条扫描路径，数它们被调用了几次。

    对齐的闭窗口一次都不该进这几条路（桶/环直接回答）；未对齐、边界行或
    旧 checkpoint 回退时必须进（省下的工作真实存在，反过来也要钉住）。
    """

    NAMES = ("_scan_usage_snapshot_window",
             "_scan_usage_snapshot_window_days",
             "_scan_usage_snapshot_window_hours",
             "_scan_usage_analytics_window",
             "_scan_usage_analytics_window_days",
             "_scan_usage_analytics_window_hours",
             "_usage_timeseries_scan")

    def __init__(self):
        self.calls = 0
        self.per = {}
        self.original = {}

    def __enter__(self):
        def make(name, original):
            def counting(*args, **kwargs):
                self.calls += 1
                self.per[name] = self.per.get(name, 0) + 1
                return original(*args, **kwargs)
            return counting
        for name in self.NAMES:
            self.original[name] = getattr(U, name)
            setattr(U, name, make(name, self.original[name]))
        return self

    def __exit__(self, *exc):
        for name, fn in self.original.items():
            setattr(U, name, fn)
        return False


# --------------------------------------------------------------------------
# checkpoint 助手
# --------------------------------------------------------------------------
def cache_data():
    """五个 kind 文件的合并视图：{kind: entries}（缺失/读不出的当空表）。"""
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
    """把合并视图写回 kind 文件（保持 v4 的包装形状；空表不写）。"""
    for kind in U._USAGE_CACHE_KINDS:
        entries = data.get(kind) or []
        if not entries:
            continue
        payload = {"schema": U._USAGE_CACHE_SCHEMA, "kind": kind,
                   "written_at": time.time(), "entries": entries}
        io.open(KIND_FILES[kind], "w", encoding="utf-8", newline="\n").write(
            json.dumps(payload, ensure_ascii=False))


def snapshot_entry(data, realm=None):
    for e in data["snapshot"]:
        if e.get("realm") == realm:
            return e
    raise AssertionError("checkpoint 里没有 realm=%r 的 snapshot 条目" % realm)


def series_entry(data, realm=None):
    for e in data["series"]:
        if e.get("realm") == realm:
            return e
    raise AssertionError("checkpoint 里没有 realm=%r 的 series 条目" % realm)


def analytics_entry(data, realm=None):
    for e in data["analytics"]:
        if e.get("realm") == realm:
            return e
    raise AssertionError("checkpoint 里没有 realm=%r 的 analytics 条目" % realm)


def make_checkpoint():
    """从零折一份干净的 checkpoint 并落盘（先删旧文件，免得带到下一条用例）。"""
    if bool(cache_files()):
        clear_cache_files()
    cache_env_force()
    restart()
    live()
    return bool(cache_files())


def live():
    """把三份状态都折到文件尾（快照 + analytics + 时间序列）。"""
    U._usage_alltime_snapshot(None)
    U._compute_usage_analytics_uncached(None)
    series_window(DAY_LO, DAY_HI)


def direct_adopt_snapshot():
    """直接调 adopt（不经过窗口）：返回 (ok, state)。

    state 是 adopt 要求的那种全新状态；restart() 先清掉「已读过 checkpoint」
    的记忆，adopt 才会重新从盘上取条目。
    """
    restart()
    state = {"snap": None, "offset": 0, "key": None, "pricing": None,
             "realm": None, "tail": b"", "days": {}, "days_floor": None,
             "hours_floor_at": None, "hits": [], "hits_overflow": False}
    with U._usage_snap_state_lock:
        ok = U._usage_cache_adopt_snapshot(
            None, state, U._usage_log_key(),
            U._pricing_inputs(P.wb_pricing.pricing_enabled()), U._realm_inputs())
    return ok, state


# --------------------------------------------------------------------------
# 基线：改动前口径与走桶口径逐字节一致
# --------------------------------------------------------------------------
write_pricing(PRICING)
write_log(ROWS)

cache_env_nowrite()
restart()
BASE_DAY = canon(window_raw(DAY_LO, DAY_HI))
BASE_HOUR = canon(window_raw(HOUR_LO, HOUR_HI))
BASE_SER_HOUR = canon(series_window(DAY_LO, DAY_HI))
BASE_SER_DAY = canon(series_window(DAY2_LO, DAY_HI, bucket=86400))
BASE_AN_DAY = canon(an_window(DAY_LO, DAY_HI))
BASE_AN_HOUR = canon(an_window(HOUR_LO, HOUR_HI))

print()
print("[0] 基线：三个视图的闭窗口都有内容（不是空窗口对空窗口）")
DAY_OBJ = json.loads(BASE_DAY)
check("日格点闭窗口：4 行、1540 令牌（含 at == since 的边界行）",
      DAY_OBJ["requests"] == 4 and DAY_OBJ["total_tokens"] == 1540,
      (DAY_OBJ["requests"], DAY_OBJ["total_tokens"]))
HOUR_OBJ = json.loads(BASE_HOUR)
check("小时格点闭窗口：7 行、3696 令牌",
      HOUR_OBJ["requests"] == 7 and HOUR_OBJ["total_tokens"] == 3696,
      (HOUR_OBJ["requests"], HOUR_OBJ["total_tokens"]))
check("时间序列小时闭窗口有桶", len(json.loads(BASE_SER_HOUR)["series"]) > 0)
check("analytics 闭窗口的窗口半有内容",
      json.loads(BASE_AN_DAY)["summary"]["window"]["requests"] == 4,
      json.loads(BASE_AN_DAY)["summary"]["window"]["requests"])

print()
print("[1] 边沿账：只记整秒行；闸门按 until 放行/拦下")
cache_env_nowrite()
restart()
U._usage_alltime_snapshot(None)
state = U._usage_snap_state[None]
check("账里就是三个整秒行（日志顺序）",
      state["hits"] == [int(x) for x in INT_ROWS], state["hits"])
check("账没溢出（hits_overflow False）", state["hits_overflow"] is False)
check("闸门：until 不在账里（TODAY0）→ 放行",
      U._closed_window_edge_ok(state, DAY_HI) is True)
check("闸门：until 恰好在账里（HOUR_LO）→ 拦下",
      U._closed_window_edge_ok(state, HOUR_LO) is False)
check("闸门：账溢出 → 一律拦下",
      U._closed_window_edge_ok({"hits": [], "hits_overflow": True}, DAY_HI)
      is False)

print()
print("[2] 对齐闭窗口走桶：日/小时/分钟三档，计数桩证明不进扫描路径")
REF_DAY = canon(ref_window(DAY_LO, DAY_HI))
cache_env_nowrite()
restart()
# 在 #301 之后，冷状态下的窗口请求改走冷扫描以避免全量重折阻塞；
# 先发一次 all-time 请求让状态与日/小时桶转热，后续窗口请求才能走桶！
U._usage_alltime_snapshot(None)
U._compute_usage_analytics_uncached(None)
with ScanCounter() as counter:
    got = canon(window_raw(DAY_LO, DAY_HI))
check("日格点闭窗口：桶路径回答、扫描 0 次、与改动前口径逐字节一致",
      got == REF_DAY and counter.calls == 0,
      (first_diff(REF_DAY, got), counter.calls, counter.per))
cold_days = canon(U._scan_usage_snapshot_window_days(None, DAY_LO, DAY_HI))
check("日格点闭窗口：桶路径 == 按日分组冷路径（逐字节）",
      got == cold_days, first_diff(cold_days, got))
REF_HOUR = canon(ref_window(HOUR_LO, HOUR_HI))
cache_env_nowrite()
restart()
U._usage_alltime_snapshot(None)
with ScanCounter() as counter:
    got = canon(window_raw(HOUR_LO, HOUR_HI))
check("小时格点闭窗口：桶路径回答、扫描 0 次、与改动前口径逐字节一致",
      got == REF_HOUR and counter.calls == 0,
      (first_diff(REF_HOUR, got), counter.calls, counter.per))
cold_hours = canon(U._scan_usage_snapshot_window_hours(None, HOUR_LO, HOUR_HI))
check("小时格点闭窗口：桶路径 == 按小时分组冷路径（逐字节）",
      got == cold_hours, first_diff(cold_hours, got))
REF_AN = canon(ref_an(DAY_LO, DAY_HI))
cache_env_nowrite()
restart()
with ScanCounter() as counter:
    got = canon(an_window(DAY_LO, DAY_HI))
check("analytics 日格点闭窗口：日桶回答、扫描 0 次、逐字节一致",
      got == REF_AN and counter.calls == 0,
      (first_diff(REF_AN, got), counter.calls, counter.per))
REF_AN_HOUR = canon(ref_an(HOUR_LO, HOUR_HI))
cache_env_nowrite()
restart()
with ScanCounter() as counter:
    got = canon(an_window(HOUR_LO, HOUR_HI))
check("analytics 小时格点闭窗口：小时层回答、扫描 0 次、逐字节一致",
      got == REF_AN_HOUR and counter.calls == 0,
      (first_diff(REF_AN_HOUR, got), counter.calls, counter.per))
REF_MIN = canon(ref_series(MIN_LO, MIN_HI))
cache_env_nowrite()
restart()
with ScanCounter() as counter:
    got = canon(series_window(MIN_LO, MIN_HI))
check("分钟格点闭窗口（step=60）：分钟环回答、扫描 0 次、逐字节一致",
      got == REF_MIN and counter.calls == 0,
      (first_diff(REF_MIN, got), counter.calls, counter.per))
MIN_OBJ = json.loads(got)
check("分钟闭窗口：桶都 < until（06:30 那一桶不进结果）",
      all(b["at"] < MIN_HI for b in MIN_OBJ["series"]),
      [b["at"] for b in MIN_OBJ["series"]])
# 公开入口（带装饰）也要逐字节一致：走桶与改动前口径是同一份响应。
cache_env_nowrite()
restart()
got_public = canon(snap_window(DAY_LO, DAY_HI))
cache_env_off()
restart()
ref_public = canon(snap_window(DAY_LO, DAY_HI))
check("公开入口：日格点闭窗口走桶 == 改动前口径（整包逐字节）",
      got_public == ref_public, first_diff(ref_public, got_public))

print()
print("[3] 边界行 = since / 窗口内整点：必须计入（左闭与桶内整秒）")
# at == since 的行（DAY1）已经算进基线数字（4 行 1540 令牌）；这里再从
# 「去掉那行会怎样」反向钉一次：少掉它，令牌数就不对了。
cache_env_nowrite()
restart()
U._usage_alltime_snapshot(None)
window_raw(DAY_LO, DAY_HI)
no_left = U._empty_stats()
for key in sorted(U._usage_snap_state[None]["days"]):
    if key >= DAY1_KEY and key < TODAY_KEY:
        U._add_usage_snapshot(no_left, U._usage_snap_state[None]["days"][key])
check("左闭：at == since 的行在日桶里（含它就多 330 令牌）",
      no_left["total_tokens"] == 1540
      and DAY_OBJ["total_tokens"] == 1540,
      (no_left["total_tokens"], DAY_OBJ["total_tokens"]))
# 窗口内的本地整点（06:00，整秒行）必须进小时桶、也进分钟桶。
cache_env_nowrite()
restart()
hour_obj = window_raw(HOUR_LO, HOUR_HI)
check("窗口内整点（06:00）计入小时窗口",
      hour_obj["total_tokens"] == 3696, hour_obj["total_tokens"])
min_obj = json.loads(canon(series_window(MIN_LO, MIN_HI)))
check("窗口内整点（06:00）计入分钟窗口（有自己的桶）",
      any(b["at"] == MIN_HI_EDGE and b["requests"] == 1
          and b["total_tokens"] == 990 for b in min_obj["series"]),
      [(b["at"], b["requests"]) for b in min_obj["series"]])
# 窗口内的本地午夜（DAY1，多天窗口里）同样照常计入。
cache_env_nowrite()
restart()
span = window_raw(DAY2_LO, DAY_HI)
check("多天窗口：窗口内的午夜行（DAY1）照常计入（两天 5 行 1650 令牌）",
      span["requests"] == 5 and span["total_tokens"] == 1650,
      (span["requests"], span["total_tokens"]))

print()
print("[4] 边界行 = until：右闭必须计入，闸门必须拦住桶路径（退回扫描）")
# 日格点：给 TODAY0（= until）补一行整秒行。
extra_day = row(TODAY0, "m-a", "u1", 1000, 100)
write_log(ROWS + [extra_day])
REF = canon(ref_window(DAY_LO, DAY_HI))
cache_env_nowrite()
restart()
# 在 #301 之后，冷状态下的窗口请求改走冷扫描以避免全量重折阻塞；
# 先发一次 all-time 请求让状态与日/小时桶转热，后续窗口请求才能走桶！
U._usage_alltime_snapshot(None)
U._compute_usage_analytics_uncached(None)
with ScanCounter() as counter:
    got = canon(window_raw(DAY_LO, DAY_HI))
check("at == until（午夜整秒行）：闸门拦住桶路径、退回扫描、逐字节一致",
      got == REF and counter.calls >= 1,
      (first_diff(REF, got), counter.calls, counter.per))
got_obj = json.loads(got)
check("右闭：at == until 的行计入（4+1 行、1540+1100 令牌）",
      got_obj["requests"] == 5 and got_obj["total_tokens"] == 2640,
      (got_obj["requests"], got_obj["total_tokens"]))
# 小时格点：给 HOUR_HI（= until）补一行整秒行（日窗口那行也留着）。
extra_hour = row(HOUR_HI, "m-b", "u1", 2000, 200)
write_log(ROWS + [extra_day, extra_hour])
REF = canon(ref_window(HOUR_LO, HOUR_HI))
cache_env_nowrite()
restart()
with ScanCounter() as counter:
    got = canon(window_raw(HOUR_LO, HOUR_HI))
check("at == until（整点整秒行）：闸门拦住小时层、退回扫描、逐字节一致",
      got == REF and counter.calls >= 1,
      (first_diff(REF, got), counter.calls, counter.per))
got_obj = json.loads(got)
check("右闭：at == until 的整点行计入（7+1 行、3696+2200 令牌）",
      got_obj["requests"] == 8 and got_obj["total_tokens"] == 5896,
      (got_obj["requests"], got_obj["total_tokens"]))
write_log(ROWS)
# 分钟格点：日志里本来就有 at == MIN_HI_EDGE（06:00）的整秒行。
REF = canon(ref_series(MIN_LO, MIN_HI_EDGE))
cache_env_nowrite()
restart()
with ScanCounter() as counter:
    got = canon(series_window(MIN_LO, MIN_HI_EDGE))
check("分钟窗口的 until 有整秒行：闸门拦住切片、退回扫描、逐字节一致",
      got == REF and counter.calls >= 1,
      (first_diff(REF, got), counter.calls, counter.per))
min_edge = json.loads(got)
check("右闭：at == until 的行进了最后一个桶（06:00 桶 1 行 990 令牌）",
      min_edge["series"] and min_edge["series"][-1]["at"] == MIN_HI_EDGE
      and min_edge["series"][-1]["requests"] == 1
      and min_edge["series"][-1]["total_tokens"] == 990,
      min_edge["series"][-1:])
# analytics 也钉一遍：at == until（午夜整秒行）→ 日桶闸门拦下、逐字节一致。
write_log(ROWS + [extra_day])
REF = canon(ref_an(DAY_LO, DAY_HI))
cache_env_nowrite()
restart()
with ScanCounter() as counter:
    got = canon(an_window(DAY_LO, DAY_HI))
check("analytics：at == until → 日桶闸门拦下、退回扫描、逐字节一致",
      got == REF and counter.calls >= 1,
      (first_diff(REF, got), counter.calls, counter.per))
write_log(ROWS)

print()
print("[5] 未对齐：起点/上界不落格点 → 退回扫描，结果与改动前一致")
fallback = [
    ("起点不是本地午夜（00:30）", dict(since=DAY_LO + 1800, until=DAY_HI)),
    ("上界不是格点（次日 00:30）", dict(since=DAY_LO, until=DAY_HI + 1800)),
    ("小时级上界不落格点（07:30）", dict(since=HOUR_LO, until=HOUR_HI + 1800)),
    ("起点不是整点（05:30，落分钟格点）", dict(since=MIN_LO, until=MIN_HI)),
]
for label, kwargs in fallback:
    ref = canon(ref_window(**kwargs))
    cache_env_nowrite()
    restart()
    with ScanCounter() as counter:
        got = canon(window_raw(**kwargs))
    check(label + "：退回扫描、结果一致（扫描 >= 1 次）",
          got == ref and counter.calls >= 1,
          (first_diff(ref, got), counter.calls, counter.per))
# 时间序列：起点带 30 秒（不落分钟格点）→ 连状态都不惊动，直接扫描。
ref = canon(ref_series(MIN_LO + 30, MIN_HI))
cache_env_nowrite()
restart()
with ScanCounter() as counter:
    got = canon(series_window(MIN_LO + 30, MIN_HI))
check("分钟起点 +30 秒：退回扫描、结果一致（扫描 >= 1 次）",
      got == ref and counter.calls >= 1,
      (first_diff(ref, got), counter.calls, counter.per))

print()
print("[6] 账溢出：整秒行超过上限（256）→ 闭窗口全部退回扫描")
# 溢出行用 m-a：两个冷路径（整段扫描 vs 按小时分组）对聚合表的键序只在
# 「迟写行把某个 (model, realm) 的首次出现挪进更早的小时桶」时才可能不同，
# 那是两条冷路径的既有性质，不是这一批改动引入的；这里要钉的是回退后的
# 数字与改动前逐字节一致，所以让数据不踩那个键序边角。
overflow_rows = [row(HOUR_LO + 30 + i, "m-a", "u1", 10, 1)
                 for i in range(300)]
write_log(ROWS + overflow_rows)
cache_env_nowrite()
restart()
U._usage_alltime_snapshot(None)
state = U._usage_snap_state[None]
check("账溢出标记亮起（记满后停记）", state["hits_overflow"] is True
      and len(state["hits"]) == U._CLOSED_WINDOW_HITS_MAX,
      (state["hits_overflow"], len(state["hits"])))
for label, kwargs in (("日格点", dict(since=DAY_LO, until=DAY_HI)),
                      ("小时格点", dict(since=HOUR_LO, until=HOUR_HI))):
    ref = canon(ref_window(**kwargs))
    cache_env_nowrite()
    restart()
    with ScanCounter() as counter:
        got = canon(window_raw(**kwargs))
    check("溢出后 %s 闭窗口退回扫描、结果一致" % label,
          got == ref and counter.calls >= 1,
          (first_diff(ref, got), counter.calls, counter.per))
ref = canon(ref_series(MIN_LO, MIN_HI))
cache_env_nowrite()
restart()
with ScanCounter() as counter:
    got = canon(series_window(MIN_LO, MIN_HI))
check("溢出后分钟闭窗口退回扫描、结果一致",
      got == ref and counter.calls >= 1,
      (first_diff(ref, got), counter.calls, counter.per))
write_log(ROWS)

print()
print("[7] checkpoint：hits 落盘/采纳，旧格式与坏形状一律被拒")
check("写盘成功", make_checkpoint())
data = cache_data()
entry = snapshot_entry(data, None)
check("snapshot 条目带 hits（整秒行）",
      entry.get("hits") == [int(x) for x in INT_ROWS], entry.get("hits"))
check("snapshot 条目 hits_overflow 是 False",
      entry.get("hits_overflow") is False, entry.get("hits_overflow"))
check("series 条目带 hits（同一份账）",
      series_entry(data, None).get("hits") == [int(x) for x in INT_ROWS],
      series_entry(data, None).get("hits"))
check("analytics 条目带 hits（同一份账）",
      analytics_entry(data, None).get("hits") == [int(x) for x in INT_ROWS],
      analytics_entry(data, None).get("hits"))
check("_usage_cache_hits_ok：合法形状通过",
      U._usage_cache_hits_ok([], False) and U._usage_cache_hits_ok([1, 2], True))
check("_usage_cache_hits_ok：缺键/非列表/非整数/非严格 bool 一律拒",
      not U._usage_cache_hits_ok(None, False)
      and not U._usage_cache_hits_ok([], None)
      and not U._usage_cache_hits_ok("x", False)
      and not U._usage_cache_hits_ok([1.5], False)
      and not U._usage_cache_hits_ok([True], False)
      and not U._usage_cache_hits_ok([], 1)
      and not U._usage_cache_hits_ok([], 0))
# 采用：合法条目 accept，hits 装进状态。
ok, adopted_state = direct_adopt_snapshot()
check("合法条目：adopt 返回 True", ok is True)
check("采纳后的 hits 与落盘一致",
      adopted_state["hits"] == [int(x) for x in INT_ROWS]
      and adopted_state["hits_overflow"] is False,
      (adopted_state["hits"], adopted_state["hits_overflow"]))
# 重启后走采用路径取闭窗口：结果与冷折逐字节一致，且确实走了加载。
cache_env_read()
restart()
got = canon(window_raw(DAY_LO, DAY_HI))
check("重启（采用 checkpoint）后闭窗口与冷折逐字节一致",
      got == BASE_DAY and "snapshot" in U._usage_cache_checkpointed,
      (first_diff(BASE_DAY, got), sorted(U._usage_cache_checkpointed)))
# 采纳回来的账参与闸门：把 until 加进账里 → 该窗口必须退回扫描。
make_checkpoint()
data = cache_data()
entry = snapshot_entry(data, None)
entry["hits"] = sorted(set(entry["hits"]) | {int(TODAY0)})
save_cache(data)
cache_env_read()
restart()
with ScanCounter() as counter:
    got = canon(window_raw(DAY_LO, DAY_HI))
check("采纳后的 hits 参与闸门（until 在账里 → 退回扫描）且结果一致",
      got == BASE_DAY and counter.calls >= 1,
      (first_diff(BASE_DAY, got), counter.calls, counter.per))
# 溢出标记也一样参与：hits_overflow True → 一律退回。
make_checkpoint()
data = cache_data()
snapshot_entry(data, None)["hits_overflow"] = True
save_cache(data)
cache_env_read()
restart()
with ScanCounter() as counter:
    got = canon(window_raw(HOUR_LO, HOUR_HI))
check("采纳后的 hits_overflow=True → 闭窗口退回扫描且结果一致",
      got == BASE_HOUR and counter.calls >= 1,
      (first_diff(BASE_HOUR, got), counter.calls, counter.per))
# 旧格式条目（没有 hits 键）必须被拒 → 升级后一次性重折。
make_checkpoint()
data = cache_data()
entry = snapshot_entry(data, None)
entry.pop("hits")
entry.pop("hits_overflow")
save_cache(data)
ok, rejected_state = direct_adopt_snapshot()
check("旧格式（缺 hits 键）：adopt 返回 False", ok is False)
check("被拒时状态没有被改动（hits 仍是空的）", rejected_state["hits"] == [])
cache_env_read()
restart()
got = canon(window_raw(DAY_LO, DAY_HI))
check("旧格式被拒后一次性重折：闭窗口结果与冷折一致",
      got == BASE_DAY and "snapshot" not in U._usage_cache_checkpointed,
      (first_diff(BASE_DAY, got), sorted(U._usage_cache_checkpointed)))
# series 与 analytics 的旧格式条目同样被拒（两份都要重折一次）。
make_checkpoint()
data = cache_data()
series_entry(data, None).pop("hits")
series_entry(data, None).pop("hits_overflow")
analytics_entry(data, None).pop("hits")
analytics_entry(data, None).pop("hits_overflow")
save_cache(data)
cache_env_read()
restart()
got_series = canon(series_window(DAY_LO, DAY_HI))
got_an = canon(an_window(DAY_LO, DAY_HI))
check("旧格式：series 条目被拒（没进已采纳表）",
      "series" not in U._usage_cache_checkpointed,
      sorted(U._usage_cache_checkpointed))
check("旧格式：analytics 条目被拒（没进已采纳表）",
      "analytics" not in U._usage_cache_checkpointed,
      sorted(U._usage_cache_checkpointed))
check("旧格式被拒后：series 闭窗口与冷折一致", got_series == BASE_SER_HOUR,
      first_diff(BASE_SER_HOUR, got_series))
check("旧格式被拒后：analytics 闭窗口与冷折一致", got_an == BASE_AN_DAY,
      first_diff(BASE_AN_DAY, got_an))
# 坏形状（合法 JSON、非法语义）一律拒绝。
bad_shapes = [
    ("hits 不是列表", lambda e: e.update({"hits": "x"})),
    ("hits 里混进浮点", lambda e: e.update({"hits": [1.5]})),
    ("hits 里混进布尔", lambda e: e.update({"hits": [True]})),
    ("hits_overflow 是 1（int 不是 bool）", lambda e: e.update({"hits_overflow": 1})),
    ("hits_overflow 缺失", lambda e: e.pop("hits_overflow")),
]
for label, mutate in bad_shapes:
    make_checkpoint()
    data = cache_data()
    mutate(snapshot_entry(data, None))
    save_cache(data)
    ok, _ = direct_adopt_snapshot()
    check("坏形状（%s）：adopt 拒绝" % label, ok is False, ok)

print()
print("[8] series 侧：step=60/3600/86400 的对齐闭窗口走切片（计数桩 0 扫描）")
cases = [
    ("step=60（05:30..06:30）", dict(since=MIN_LO, until=MIN_HI)),
    ("step=3600（午夜..午夜）", dict(since=DAY_LO, until=DAY_HI)),
    ("step=86400（两天，显式桶宽）",
     dict(since=DAY2_LO, until=DAY_HI, bucket=86400)),
]
for label, kwargs in cases:
    ref = canon(ref_series(**kwargs))
    cache_env_nowrite()
    restart()
    with ScanCounter() as counter:
        got = canon(series_window(**kwargs))
    check(label + "：切片回答、扫描 0 次、与整段扫描逐字节一致",
          got == ref and counter.calls == 0,
          (first_diff(ref, got), counter.calls, counter.per))
# 切片与整段扫描直接对拍（同一组边界；桶 at 的类型跟着 lo 走，两边都传
# 公开入口归一化后的浮点，切片的 at 来自状态，与扫描的 lo + k*step 同型）。
cache_env_nowrite()
restart()
series_window(DAY_LO, DAY_HI)
state = U._series_state[None]
float_slice = U._series_slice(state, None, DAY_LO, DAY_HI, 3600, DAY1_KEY)
float_scan = U._usage_timeseries_scan(None, DAY_LO, DAY_HI, 3600)
check("step=3600：切片直接调用与整段扫描逐字节一致",
      float_slice is not None and canon(float_slice) == canon(float_scan),
      first_diff(canon(float_scan), canon(float_slice or {})))
day_slice = U._series_slice(state, None, DAY2_LO, DAY_HI, 86400, DAY2_KEY)
day_scan = U._usage_timeseries_scan(None, DAY2_LO, DAY_HI, 86400)
check("step=86400：切片直接调用与整段扫描逐字节一致",
      day_slice is not None and canon(day_slice) == canon(day_scan),
      first_diff(canon(day_scan), canon(day_slice or {})))
# 切片不吐窗口外的桶：step=3600 的窗口里不该有 today 的任何桶。
check("切片只吐窗口内的桶（今天的小时桶不在结果里）",
      all(b["at"] < DAY_HI for b in float_slice["series"]),
      [b["at"] for b in float_slice["series"] if b["at"] >= DAY_HI])
# 闭窗口上界切进缓冲里：窗口之外的计费行（今天 05:00/06:00 两条 credit 行）
# 不许出现在切片的 credit 历史里。
check("切片不吐窗口外的 credit 行（上界之后的两条 credit 不在）",
      all((c.get("at") or 0) <= DAY_HI for c in float_slice["credits"])
      and len(float_slice["credits"]) == 2,
      float_slice["credits"])
# 一分钟切片：桶序与扫描一致、上界之后的桶不进结果。
cache_env_nowrite()
restart()
series_window(MIN_LO, MIN_HI)
min_slice = U._series_slice(U._series_state[None], None, MIN_LO, MIN_HI, 60,
                            TODAY_KEY)
min_scan = U._usage_timeseries_scan(None, MIN_LO, MIN_HI, 60)
check("step=60：切片直接调用与整段扫描逐字节一致",
      min_slice is not None and canon(min_slice) == canon(min_scan),
      first_diff(canon(min_scan), canon(min_slice or {})))

print()
print("[9] 开关：WB_USAGE_DAY_BUCKETS=0 时闭窗口与改动前逐字节一致")
cache_env_off()
restart()
check("关掉日桶：日/小时闭窗口与基线一致",
      canon(window_raw(DAY_LO, DAY_HI)) == BASE_DAY
      and canon(window_raw(HOUR_LO, HOUR_HI)) == BASE_HOUR)
check("关掉日桶：series 与 analytics 闭窗口与基线一致",
      canon(series_window(DAY_LO, DAY_HI)) == BASE_SER_HOUR
      and canon(an_window(DAY_LO, DAY_HI)) == BASE_AN_DAY)
cache_env_nowrite()
restart()
check("日桶关掉时窗口路径连状态都不碰（没有可用的层）",
      not U._usage_snap_state and not U._series_state)
cache_env()

print()
print("[10] 夏令时假时区：闭窗口跨切换日照样正确，格点被挪动时退回")
# 无 tzset 的平台（Windows）用「把日桶 at 挪 3600 秒」的变异钉住格点校验；
# 有 tzset 的平台（CI 的 Linux）再叠一组真实柏林夏令时用例。
cache_env_nowrite()
restart()
series_window(DAY_LO, DAY_HI)
state = U._series_state[None]
check("对齐闭窗口本来就走切片",
      U._series_slice(state, None, DAY_LO, DAY_HI, 3600, DAY1_KEY) is not None)
state["days"][TODAY_KEY]["at"] += 3600        # 假装这一天少了/多了 3600 秒
check("日桶 at 不落在 86400 格点上时闭窗口切片放弃（退回扫描）",
      U._series_slice(state, None, DAY_LO, DAY_HI, 3600, DAY1_KEY) is None)
state["days"][TODAY_KEY]["at"] -= 3600
hours = state["days"][TODAY_KEY]["hours"]
h_key = sorted(hours, key=int)[0]
hours[h_key]["at"] += 60
check("小时子桶 at 与日桶对不上时切片放弃",
      U._series_slice(state, None, DAY_LO, DAY_HI, 3600, DAY1_KEY) is None)
hours[h_key]["at"] -= 60
check("恢复后切片又能用",
      U._series_slice(state, None, DAY_LO, DAY_HI, 3600, DAY1_KEY) is not None)
state["hits"].append(int(DAY_HI))
check("账里补进 until（整秒行）→ 闭窗口切片放弃（闸门）",
      U._series_slice(state, None, DAY_LO, DAY_HI, 3600, DAY1_KEY) is None)
state["hits"].remove(int(DAY_HI))
check("账里去掉 until 后切片又能用",
      U._series_slice(state, None, DAY_LO, DAY_HI, 3600, DAY1_KEY) is not None)
# snapshot 侧同一条：把今天的小时桶 at 挪 1800 秒 → 小时层拒绝服务。
cache_env_nowrite()
restart()
U._usage_alltime_snapshot(None)
window_raw(HOUR_LO, HOUR_HI)
snap_state = U._usage_snap_state[None]
check("对齐的小时闭窗口本来就走层",
      U._usage_snapshot_window_from_hours(None, HOUR_LO, HOUR_HI, None)
      is not None)
snap_state["days"][TODAY_KEY]["at"] += 1800
check("小时桶 at 与日桶对不上时小时层拒绝（退回冷路径）",
      U._usage_snapshot_window_from_hours(None, HOUR_LO, HOUR_HI, None) is None)
snap_state["days"][TODAY_KEY]["at"] -= 1800
check("恢复后小时层又能用",
      U._usage_snapshot_window_from_hours(None, HOUR_LO, HOUR_HI, None)
      is not None)

if hasattr(time, "tzset"):
    # 真实夏令时时区（Europe/Berlin 2026-10-25 结束夏令时，那天 25 小时）：
    # 冻结时钟挪到切换之后（10-28 12:00，晚于最后一行），造一批横跨切换日
    # 的行，再跑闭窗口。
    #
    # 三条纪律（与 _test_usage_day_buckets.py §[9] 同一套）：
    #   * 与「整段扫描」比的是同一种东西——公开入口对公开入口；
    #   * 先确认「本机真的被切到了柏林」再断言：只有 POSIX TZ 支持的系统会
    #     把 IANA 名悄悄降级成 UTC，那时日边界不再被挪动，断言会以误导性的
    #     方式失败。用 10-25 那天是不是 25 小时来验证，两个 TZ 值都做不到就
    #     跳过这段；
    #   * 行都带 +0.25，普通行不落整秒（边沿账只收整秒行）。
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
            dst_rows = [row(base + d * 86400 + h * 3600 + 0.25, "m-a", "u1",
                            10, 1)
                        for d in range(6) for h in (0, 6, 12, 18)]
            write_log(dst_rows)
            M24 = time.mktime((2026, 10, 24, 0, 0, 0, 0, 0, -1))
            M27 = time.mktime((2026, 10, 27, 0, 0, 0, 0, 0, -1))
            M28 = time.mktime((2026, 10, 28, 0, 0, 0, 0, 0, -1))
            # (a) 切换之后的规则日：日格点闭窗口走日桶，与逐行口径逐字节一致。
            ref = canon(ref_window(M27, M28))
            cache_env_nowrite()
            restart()
            U._usage_alltime_snapshot(None)
            with ScanCounter() as counter:
                got = canon(window_raw(M27, M28))
            check("夏令时：切换后规则日闭窗口走日桶（扫描 0 次）、逐字节一致",
                  got == ref and counter.calls == 0,
                  (first_diff(ref, got), counter.calls, counter.per))
            # (b) 横跨 25 小时那天的日格点闭窗口：日键求和与逐行口径一致。
            ref = canon(ref_window(M24, M27))
            cache_env_nowrite()
            restart()
            got = canon(window_raw(M24, M27))
            check("夏令时：横跨切换日的日格点闭窗口与逐行口径逐字节一致",
                  got == ref, first_diff(ref, got))
            # (c) 切换日的小时级闭窗口（05:00-07:00 墙钟）：层或冷路径都要
            # 与逐行口径逐字节一致（层够不到保留线时由冷路径回答）。
            h_lo = time.mktime((2026, 10, 25, 5, 0, 0, 0, 0, -1))
            h_hi = time.mktime((2026, 10, 25, 7, 0, 0, 0, 0, -1))
            ref = canon(ref_window(h_lo, h_hi))
            cache_env_nowrite()
            restart()
            got = canon(window_raw(h_lo, h_hi))
            check("夏令时：切换日的小时级闭窗口与逐行口径逐字节一致",
                  got == ref, first_diff(ref, got))
            # (d) 日边界被挪动：横跨切换日的切片必须放弃（退回扫描），结果一致。
            cache_env_nowrite()
            restart()
            series_window(M24, M28, bucket=86400)
            sliced = U._series_slice(U._series_state[None], None, M24, M28,
                                     86400,
                                     time.strftime("%Y-%m-%d",
                                                   time.localtime(M24)))
            check("夏令时：横跨切换日的日桶切片放弃（格点被挪动）",
                  sliced is None, None if sliced is None else "切片竟然成立了")
            ref = canon(ref_series(M24, M28, bucket=86400))
            got = canon(series_window(M24, M28, bucket=86400))
            check("夏令时：放弃后整段扫描的结果与逐行口径一致",
                  got == ref, first_diff(ref, got))
    finally:
        _time.time = lambda: FROZEN
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()
else:
    print("  [SKIP] 本平台没有 time.tzset，跳过真实夏令时时区用例")
write_log(ROWS)
cache_env_nowrite()
restart()

shutil.rmtree(_TMP, ignore_errors=True)
print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
