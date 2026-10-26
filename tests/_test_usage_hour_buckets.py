"""小时层：snapshot 的整点窗口（range=custom 起点落在本地整点时）。

面板的自定义区间起点是任意时刻，而 datetime-local 输入框给的是分钟粒度——
最常见的形态就是整点。改动前只有「起点是本地午夜」的窗口能走存量桶
（日桶，见 _test_usage_day_buckets.py），整点窗口一律整段扫描。这一套钉住
新增的小时层：

  * 切分：每行折进「它 at 所属那天、离本地午夜多少个 3600 秒」的那个小时
    桶（挂在日桶的 hours 子表里）；午夜整点的行落在第 0 个小时，迟写行落回
    它时间戳所属的那个小时；
  * 等价：整点窗口由小时桶相加，与改动前的逐行口径逐字节一致（测试数据的价
    刻意用二进制可精确求和的数，见 _test_usage_day_buckets.py 的说明）；跨
    午夜、跨天、同一天内、空窗口、realm 过滤、成本字段（cost_cny /
    cost_missing / by_model_acct）全部对齐；
  * 两条路径：小时层不可用时现折一份「按小时分组」的冷路径
    （_scan_usage_snapshot_window_hours），与存量桶给出的字节完全一致；
  * 封口：已经越过的小时不再重折（桶对象原样复用）；
  * checkpoint：小时表与 hours_floor_at 随状态落盘/加载，重启（含真·子进程）
    后窗口与冷折逐字节一致，且仍然走层（不退回扫描）；
  * 降级：坏小时表（形状/键/at/子桶嵌表）、坏 hours_floor_at、缺表、被摘过
    的表、剪枝线以外的窗口，一律退回冷路径，结果正确；
  * 保留：小时层只留最近几天（WB_USAGE_HOUR_KEEP_DAYS），剪枝线以外的小时
    表被置 None，迟写行不会把缺了其余小时的表重建出来；
  * 开关：WB_USAGE_DAY_BUCKETS=0（小时层挂在日桶上，同一个开关）时整点窗口
    与改动前逐字节一致（逐行口径）；
  * 夏令时：整点窗口在真实夏令时时区里跨切换日照样与逐行口径逐字节一致；
    格点被挪动（at 不落在 3600 的格点上）时求和侧放弃、退回冷路径。

降级用例沿用 _test_usage_aggregate_cache.py / _test_usage_day_buckets.py 的
写法：先给「应当被拒绝」的那份状态加一个一眼可见的标记（poison），再做变异。
标记没被采用就说明校验拦住了它；一旦被错误采用，结果里就会多出那个标记，
用例当场失败。

No network: the usage log, the pricing table and the settings file are
synthesised in a temp directory.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# 与 my_dump.py / _test_usage_day_buckets.py 同一把冻结时钟：窗口边界（本地
# 午夜/整点）与 started/since 都从 time.time() 派生，不冻结就不可比。
FROZEN = 1791445200.0

_TMP = tempfile.mkdtemp(prefix="wb-hourbucket-")
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
# checkpoint 在 v4 里一类一份文件（T8 的拆分）：这里给出合并视图与按类读写，
# 用例里 data["snapshot"] / data["series"] / data["analytics"] 的写法不用改。
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
# 数据：三个本地日 + 整点/午夜整点 + 同一个小时多行 + 空小时 + 迟写行
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
    # 前天：午夜整点（第 0 个小时）与第 1 个小时。
    row(DAY2, "m-a", "u1", 100, 10),
    row(DAY2 + 3600, "m-b", "u2", 200, 20, realm="cn"),
    # 昨天：00:00:00 与 23:59:59（都属于 DAY1，落进第 0 与第 23 个小时）。
    row(DAY1, "m-a", "u1", 300, 30, credit=0.5),
    row(DAY1 + 86399, "m-a", "u2", 400, 40, key="k2"),
    row(DAY1 + 3600, "m-c", "u1", 500, 50, outcome="failed", credit=0.25),
    # 今天：第 0 个小时两行（同一桶）、跨 realm、无价模型、客户端中止、
    # 无 key 字段的历史行、无 realm 字段的行。
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
# 今天第 5 个小时三行（同一小时多行）、第 7 个小时一行；第 6 个小时刻意留空
# （空小时不能影响求和，也不能让格点校验失败）。
ROWS.append(row(TODAY0 + 5 * 3600, "m-b", "u2", 1100, 110, realm="cn"))
ROWS.append(row(TODAY0 + 5 * 3600 + 1, "m-a", "u1", 1000, 100, credit=0.125))
ROWS.append(row(TODAY0 + 5 * 3600 + 1800, "m-a", "u2", 1200, 120))
ROWS.append(row(TODAY0 + 7 * 3600 + 61, "m-c", "u1", 1300, 130))

# 价目表刻意用二进制可精确表示的数（0.5 / 0.25 / 0.125）与 unit=1：
# 逐行累加与按小时累加因此都精确，逐字节比对成立（见文件头）。
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
# 开关（WB_USAGE_CACHE = checkpoint；WB_USAGE_DAY_BUCKETS = 日桶 + 小时层）
# --------------------------------------------------------------------------
def cache_env(enabled=None, min_bytes=None, min_seconds=None, days=None,
              keep=None, hour_keep=None):
    for name, value in (("WB_USAGE_CACHE", enabled),
                        ("WB_USAGE_CACHE_MIN_BYTES", min_bytes),
                        ("WB_USAGE_CACHE_MIN_SECONDS", min_seconds),
                        ("WB_USAGE_DAY_BUCKETS", days),
                        ("WB_USAGE_DAY_KEEP_DAYS", keep),
                        ("WB_USAGE_HOUR_KEEP_DAYS", hour_keep)):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = str(value)


def cache_env_nowrite():
    cache_env(enabled=1, min_bytes=10 ** 9, min_seconds=10 ** 9, days=None)


def cache_env_force():
    cache_env(enabled=1, min_bytes=0, min_seconds=0, days=None)


def cache_env_off():
    """改动前的口径：checkpoint 关掉、日桶/小时层关掉（= 逐行扫描 + 全量冷折）。"""
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


def hour_start(ts):
    """ts 所属本地整点的 epoch（与 U._local_hour_start 同一套）。"""
    lt = time.localtime(ts)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour,
                        0, 0, 0, 0, -1))


def hour_window(hours_back, minute_offset=0):
    """窗口起点：从今天零点往前 `hours_back` 个小时，再偏 `minute_offset` 分。"""
    return hour_start(TODAY0 - hours_back * 3600) + minute_offset * 60


def window_raw(since, until=None, realm=None):
    """窗口半的原始折叠（走层：小时层可用时由桶求和，否则冷路径）。"""
    return U._usage_snapshot_window(realm, since, until)


def window_public(since, until=None, realm=None):
    """公开入口的窗口结果（带 started / accounts_map 等装饰，时钟冻结后可比）。"""
    return U._usage_snapshot_uncached(realm, since=since, until=until)


def no_hour_cold_path():
    """把「按小时分组的冷路径」换成炸弹：走层时它不该被调用。"""

    def boom(*args, **kwargs):
        raise AssertionError("整点窗口走了冷路径（没有用上小时层）")

    U._scan_usage_snapshot_window_hours = boom


def restore_cold_path():
    U._scan_usage_snapshot_window_hours = _COLD_HOURS


_COLD_HOURS = U._scan_usage_snapshot_window_hours


def live():
    """公开入口的若干窗口（三个整点窗口 + 一个非整点 + 午夜 + all-time）。"""
    out = {}
    for label, since, until in WINDOWS:
        out["win_" + label] = window_public(since, until)
    out["snap_all"] = U._usage_snapshot_uncached()
    return out


def snapshot_entry(data, realm=None):
    for e in data["snapshot"]:
        if e.get("realm") == realm:
            return e
    raise AssertionError("checkpoint 里没有 realm=%r 的 snapshot 条目" % realm)


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


def make_checkpoint():
    """从零折一份干净的 checkpoint 并落盘。

    先删掉旧文件：上一条用例可能刚把一份「会被采纳的变异」写进盘（例如把
    今天的小时表摘掉），不删的话这一轮 restart() 会把它采纳回来、折叠不
    推进、也不会重写，变异就带到下一条用例去了。
    """
    if bool(cache_files()):
        clear_cache_files()
    cache_env_force()
    restart()
    live()
    return bool(cache_files())


# 窗口清单：(标签, 起点, 上界)。整点窗口走层；非整点/带上界的走逐行扫描。
WINDOWS = [
    ("today_h0", hour_window(15), None),            # 今天 00:00（也是午夜）
    ("today_h5", hour_window(10), None),            # 今天 05:00（跨过第 0 个小时）
    ("yest_h22", hour_window(26), None),            # 昨天 22:00，跨午夜到今天
    ("yest_h3", hour_window(45), None),             # 昨天 03:00
    ("odd_37", hour_window(10, minute_offset=37), None),   # 非整点：退回逐行
    ("until_open", hour_window(10), FROZEN),        # 带上界：退回逐行
    ("future", hour_window(-10), None),             # 空窗口（起点在未来）
]


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


def local_midnight(ts=None):
    lt = _t.localtime(FROZEN if ts is None else ts)
    return _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def hour_window(hours_back, minute_offset=0):
    lt = _t.localtime(local_midnight() - hours_back * 3600)
    base = _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour,
                      0, 0, 0, 0, -1))
    return base + minute_offset * 60


out = {}
for label, hours_back, minute_offset, until in __WINDOWS__:
    since = hour_window(hours_back, minute_offset)
    out["win_" + label] = U._usage_snapshot_uncached(None, since=since,
                                                     until=until)
out["snap_all"] = U._usage_snapshot_uncached()
with open(os.environ["WB_CHILD_OUT"], "w", encoding="utf-8") as fh:
    json.dump({"checkpointed": U._usage_cache_checkpointed, "results": out}, fh,
              ensure_ascii=False)
print("child ok")
'''
CHILD_SCRIPT = os.path.join(_TMP, "child_runner.py")
_CHILD_WINDOWS = [(label, 15 - (since - TODAY0) // 3600 if since is not None else 0,
                   0, until) for label, since, until in WINDOWS]
# 子进程里现算窗口：把「离今天零点多少小时」与「偏多少分」写死，避免依赖父
# 进程的临时目录布局。
_CHILD_WINDOWS = [
    ("today_h0", 15, 0, None),
    ("today_h5", 10, 0, None),
    ("yest_h22", 26, 0, None),
    ("yest_h3", 45, 0, None),
    ("odd_37", 10, 37, None),
    ("until_open", 10, 0, FROZEN),
    ("future", -10, 0, None),
]
io.open(CHILD_SCRIPT, "w", encoding="utf-8", newline="\n").write(
    RUNNER.replace("__FROZEN__", repr(FROZEN)).replace("__ROOT__", repr(ROOT))
    .replace("__WINDOWS__", repr(_CHILD_WINDOWS)))


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
check("基线：整点窗口有内容，窗口越窄覆盖的请求越少",
      GOLDEN_OBJ["win_today_h5"]["requests"] > 0
      and GOLDEN_OBJ["win_yest_h22"]["requests"]
      > GOLDEN_OBJ["win_today_h5"]["requests"],
      (GOLDEN_OBJ["win_today_h5"]["requests"],
       GOLDEN_OBJ["win_yest_h22"]["requests"]))
check("基线（关掉日桶/小时层）：状态里一个桶都没有",
      not U._usage_snap_state[None]["days"]
      and U._usage_snap_state[None].get("hours_floor_at") is None,
      U._usage_snap_state[None].get("days"))

print()
print("[1] 切分：每行折进它 at 所属那天、离午夜多少个 3600 秒的小时桶")
cache_env_nowrite()
restart()
live()
state = U._usage_snap_state[None]
days = state["days"]
today = days[TODAY_KEY]
hours = today["hours"]
check("小时子表挂在日桶里，键是数字字符串",
      isinstance(hours, dict) and hours and all(isinstance(h, str) for h in hours)
      and all(h.isdigit() for h in hours),
      sorted(hours, key=int))
check("小时桶的 at == 日桶 at + 序号*3600（格点）",
      all(int(hours[h]["at"]) == int(today["at"]) + int(h) * 3600
          for h in hours),
      {h: hours[h]["at"] for h in sorted(hours, key=int)})
check("午夜整点的行落在第 0 个小时",
      hours["0"]["requests"] == 5,
      hours["0"]["requests"])
check("同一个小时的多行折进同一个桶（今天第 5 个小时三行）",
      hours["5"]["requests"] == 3 and hours["5"]["total_tokens"] == 3630,
      (hours["5"]["requests"], hours["5"]["total_tokens"]))
check("空小时不建桶（今天第 6 个小时不在表里）",
      "6" not in hours, sorted(hours, key=int))
check("小时桶与日桶同源：成本与三层结构都在",
      all(k in hours["5"] for k in
          ("cost_cny", "cost_missing", "by_model", "by_model_realm",
           "by_model_acct")),
      sorted(hours["5"].keys())[:8])
check("昨天 23:59:59 的行落在昨天第 23 个小时（不跨天）",
      days[DAY1_KEY]["hours"]["23"]["requests"] == 1
      and days[DAY1_KEY]["hours"]["23"]["total_tokens"] == 440,
      days[DAY1_KEY]["hours"]["23"]["requests"])
# hours_floor_at 是剪枝线：最新那天往前保留小时层天数。
want_floor = U._day_key_start(TODAY_KEY) - U._usage_hour_keep_days() * 86400
check("hours_floor_at 落在剪枝线上（最新那天往前保留天数）",
      state["hours_floor_at"] == want_floor,
      (state["hours_floor_at"], want_floor))
# 迟写行：把一条 at 属于昨天第 4 个小时的行追加到今天，它必须落进那个小时。
append_log([row(DAY1 + 4 * 3600, "m-a", "u1", 1000, 100, credit=0.5)])
live()
days = U._usage_snap_state[None]["days"]
check("迟写行落进它时间戳所属的那个小时（不是折叠时刻那小时）",
      days[DAY1_KEY]["hours"]["4"]["requests"] == 1
      and days[TODAY_KEY]["hours"]["0"]["requests"] == 5,
      (days[DAY1_KEY]["hours"]["4"]["requests"],
       days[TODAY_KEY]["hours"]["0"]["requests"]))
write_log(ROWS)
cache_env_off()
restart()
GOLDEN_RAW = live()
GOLDEN = canon(GOLDEN_RAW)
GOLDEN_OBJ = json.loads(GOLDEN)

print()
print("[2] 等价：整点窗口 = 小时桶之和，与改动前的逐行口径逐字节一致")
# 测试数据的价是二进制精确值，所以整点窗口（含跨午夜、跨天）逐字节相同。
for label, since, until in WINDOWS:
    cache_env_off()
    restart()
    ref = canon(window_public(since, until))
    cache_env_nowrite()
    restart()
    got = canon(window_public(since, until))
    check("窗口 %s：走层/冷路径 == 逐行口径（整包逐字节）" % label, ref == got,
          first_diff(ref, got))
# 整点窗口确实走了小时层：把按小时分组的冷路径换成炸弹，结果必须不变。
cache_env_nowrite()
restart()
live()                       # 先把状态折到文件尾（含小时桶）
no_hour_cold_path()
try:
    layered = canon(window_public(WINDOWS[1][1]))
    check("整点窗口走了小时层（冷路径没被调用）",
          layered == canon(GOLDEN_OBJ["win_" + WINDOWS[1][0]]),
          first_diff(canon(GOLDEN_OBJ["win_" + WINDOWS[1][0]]), layered))
finally:
    restore_cold_path()
# 层给出的窗口半 == 按小时分组的冷路径（两条路径逐字节相同）。
for label, since, until in WINDOWS[:5]:
    cache_env_nowrite()
    restart()
    U._usage_snapshot_uncached()      # 折出小时桶
    layered = canon(window_raw(since, until))
    cold = canon(U._scan_usage_snapshot_window_hours(None, since, None))
    check("窗口 %s：小时桶之和 == 按小时分组的冷路径" % label, layered == cold,
          first_diff(cold, layered))
# realm 过滤：三个 realm 视角都要与逐行口径一致（小时桶按 realm 分开折）。
for realm in (None, "intl", "cn"):
    since = WINDOWS[1][1]
    cache_env_off()
    restart()
    ref = canon(window_raw(since, None, realm))
    cache_env_nowrite()
    restart()
    U._usage_snapshot_uncached(realm)
    got = canon(window_raw(since, None, realm))
    check("realm=%s：走层 == 逐行口径（整包逐字节）" % (realm or "all"),
          ref == got, first_diff(ref, got))
# 成本字段必须跟着小时桶走（不是留空、也不是标注不可用）。
cache_env_nowrite()
restart()
U._usage_snapshot_uncached()
snap = window_raw(WINDOWS[3][1])
check("整点窗口带成本字段：cost_cny / cost_missing / by_model_acct 都在",
      snap["cost_cny"] > 0 and isinstance(snap["cost_missing"], dict)
      and snap["by_model_acct"],
      (snap["cost_cny"], list(snap["by_model_acct"].keys())))
check("无价模型照常落进 cost_missing",
      snap["cost_missing"].get("no-price-model") == 1,
      snap["cost_missing"])
# 午夜起点仍然走日桶（小时层不抢它的活），且结果与逐行一致。
mid_since = TODAY0
cache_env_nowrite()
restart()
U._usage_snapshot_uncached()
mid = canon(window_raw(mid_since))
cache_env_off()
restart()
check("午夜起点：日桶路径 == 逐行口径（逐字节）",
      mid == canon(window_raw(mid_since)), "")

print()
print("[3] 封口：越过的小时不再重折（对象原样复用），只有今天在长")
cache_env_nowrite()
restart()
live()
before_objs = dict(U._usage_snap_state[None]["days"])
before = json.loads(canon(U._usage_snap_state[None]["days"]))
append_log([row(TODAY0 + 8 * 3600, "m-a", "u1", 2000, 200, credit=0.5)])
live()
after = U._usage_snap_state[None]["days"]
sealed = [k for k in before if k != TODAY_KEY]
check("旧日的小时表对象原样复用（同一个 dict，不是重建的）",
      all(after[k] is before_objs[k] for k in sealed),
      [k for k in sealed if after[k] is not before_objs[k]])
check("旧日的小时表内容一字未变",
      all(canon(after[k]) == canon(before[k]) for k in sealed),
      [k for k in sealed if canon(after[k]) != canon(before[k])])
check("今天第 8 个小时新建了桶，第 5 个小时没被动",
      "8" in after[TODAY_KEY]["hours"]
      and after[TODAY_KEY]["hours"]["5"]["requests"]
      == before[TODAY_KEY]["hours"]["5"]["requests"],
      sorted(after[TODAY_KEY]["hours"], key=int))
write_log(ROWS)
cache_env_off()
restart()
GOLDEN_RAW = live()
GOLDEN = canon(GOLDEN_RAW)

print()
print("[4] checkpoint：小时表与 hours_floor_at 随状态落盘/加载")
check("写盘成功", make_checkpoint())
stored = cache_data()
check("每个 kind 文件都是当前 schema 的 JSON 对象", all(json.load(io.open(KIND_FILES[k], encoding="utf-8"))["schema"] == U._USAGE_CACHE_SCHEMA for k in U._USAGE_CACHE_KINDS if os.path.exists(KIND_FILES[k])), cache_files())
entry = snapshot_entry(stored, None)
day = entry["days"][TODAY_KEY]
check("snapshot 条目里今天那天的 hours 子表落盘了",
      isinstance(day.get("hours"), dict) and day["hours"]
      and all(isinstance(h, str) for h in day["hours"]),
      sorted(day.get("hours") or {}, key=int))
check("小时桶带着格点 at 落盘",
      all(isinstance(sub.get("at"), (int, float))
          for sub in day["hours"].values()),
      list(day["hours"].values())[:1])
check("snapshot 条目带 hours_floor_at（小时层剪枝线）",
      entry.get("hours_floor_at") == want_floor,
      entry.get("hours_floor_at"))
hour_keys = list(day["hours"]["5"]["by_model"].keys())
check("小时桶里的键序是折叠插入顺序（不是字母序）",
      hour_keys and hour_keys != sorted(hour_keys), hour_keys[:5])

cache_env_nowrite()
restart()
LOADED_RAW = live()
ADOPTED = dict(U._usage_cache_checkpointed)
check("重启后加载到了 checkpoint（含 snapshot）",
      "snapshot" in ADOPTED, ADOPTED)
check("加载后窗口与冷折逐字节一致", canon(LOADED_RAW) == GOLDEN,
      first_diff(GOLDEN, canon(LOADED_RAW)))
# 加载回来的小时层必须照常服务：冷路径换成炸弹，整点窗口仍答得出来。
no_hour_cold_path()
try:
    got = canon(window_public(WINDOWS[1][1]))
    check("重启加载后整点窗口仍走层（冷路径没被调用）",
          got == canon(GOLDEN_OBJ["win_" + WINDOWS[1][0]]),
          first_diff(canon(GOLDEN_OBJ["win_" + WINDOWS[1][0]]), got))
finally:
    restore_cold_path()

print()
print("[5] 真·新进程：加载 checkpoint 后整点窗口一致，且确实走了加载路径")
child_raw, child_state = run_child()
check("新进程结果与冷折一致", canon(child_raw) == GOLDEN,
      first_diff(GOLDEN, canon(child_raw)))
check("新进程记录到了加载的 offset（含 snapshot）",
      "snapshot" in child_state, child_state)

print()
print("[6] 故障注入：坏小时表一律当作没有缓存，结果回到真值")


def mutate_hour(mutate_fn, label, since=None, win_label=None):
    """先给「窗口覆盖到的小时桶」加一个可见标记，再变异——结果必须回到真值。

    标记落在今天第 5 个小时（win_today_h5 / win_yest_h22 都覆盖它）：只要
    这份状态被采用，结果里就会多出 1000 次请求，用例当场失败。
    """
    make_checkpoint()
    data = cache_data()
    entry = snapshot_entry(data, None)
    hour = entry["days"][TODAY_KEY]["hours"]["5"]
    hour["requests"] += 1000
    mutate_fn(entry, hour)
    save_cache(data)
    cache_env_nowrite()
    restart()
    if since is None:
        since = WINDOWS[1][1]
    if win_label is None:
        win_label = "win_" + WINDOWS[1][0]
    got = canon(window_public(since))
    want = canon(GOLDEN_OBJ[win_label])
    check(label, got == want, first_diff(want, got))


def load_mutated(mutate_fn):
    """写一份带标记的 checkpoint、变异、重启加载；返回加载后的状态。

    与 mutate_hour 同一套准备，但断言方式不同：这几条要直接问「层还服务不
    服务」——层拒绝时结果由冷路径给出，标记本来就看不见。
    """
    make_checkpoint()
    data = cache_data()
    entry = snapshot_entry(data, None)
    entry["days"][TODAY_KEY]["hours"]["5"]["requests"] += 1000
    mutate_fn(entry)
    save_cache(data)
    cache_env_nowrite()
    restart()
    U._usage_snapshot_uncached()      # 采用（或拒绝）变异后的 checkpoint
    return U._usage_snap_state[None]


# 6.0 自检：形状合法、值被改过的小时桶会被采用（标记必须可见），否则下面的
# 用例可能什么都没测到。
make_checkpoint()
data = cache_data()
snapshot_entry(data, None)["days"][TODAY_KEY]["hours"]["5"]["requests"] += 1000
save_cache(data)
cache_env_nowrite()
restart()
POISONED = canon(window_public(WINDOWS[1][1]))
check("标记自检：形状合法的坏小时桶会被采用（标记出现在结果里）",
      POISONED != GOLDEN, "标记没生效，下面的用例可能什么都没测到")

mutate_hour(lambda entry, hour: entry["days"][TODAY_KEY].update({"hours": []}),
            "hours 不是对象")
mutate_hour(lambda entry, hour: hour.update({"requests": "1000"}),
            "小时桶缺数字字段")
mutate_hour(lambda entry, hour: hour.update({"by_model_acct": []}),
            "小时桶的 by_model_acct 不是对象")
mutate_hour(lambda entry, hour: hour.update({"at": "x"}),
            "小时桶的 at 不是数字")
mutate_hour(lambda entry, hour: hour.update({"hours": {}}),
            "小时桶里又嵌了一层 hours")


def _bad_key(entry, hour):
    hours = entry["days"][TODAY_KEY]["hours"]
    hours["x5"] = hours.pop("5")


mutate_hour(_bad_key, "小时键不是数字字符串")


def _bad_sub(entry, hour):
    hours = entry["days"][TODAY_KEY]["hours"]
    hours["5"] = {"requests": 1}


mutate_hour(_bad_sub, "小时桶缺字段（缺 cost/by_model 等）")


def _bad_floor(entry, hour):
    entry.update({"hours_floor_at": "x"})


mutate_hour(_bad_floor, "hours_floor_at 不是数字")

# 形状合法但「层不该服务」的几种状态：层必须拒绝（返回 None），窗口退回冷
# 路径。这几条直接问层函数，断言「拒绝」而不是「结果正确」——结果正确是
# 冷路径的功劳，标记看不见。
def _none_today(entry):
    entry["days"][TODAY_KEY]["hours"] = None


state = load_mutated(_none_today)
check("今天那天的表被摘过（None）：层拒绝服务",
      U._usage_snapshot_window_from_hours(None, WINDOWS[1][1], None, None) is None)
check("表被摘过时窗口退回冷路径，结果仍然正确",
      canon(window_public(WINDOWS[1][1]))
      == canon(GOLDEN_OBJ["win_" + WINDOWS[1][0]]))


def _drop_today(entry):
    del entry["days"][TODAY_KEY]["hours"]


state = load_mutated(_drop_today)
check("今天那天的表整个丢了：层拒绝服务",
      U._usage_snapshot_window_from_hours(None, WINDOWS[1][1], None, None) is None)


def _drop_yesterday(entry):
    del entry["days"][DAY1_KEY]["hours"]


state = load_mutated(_drop_yesterday)
check("窗口起点那天的表丢了：层拒绝服务（不能只跳过那一天）",
      U._usage_snapshot_window_from_hours(None, WINDOWS[2][1], None, None) is None)
check("起点那天丢表时窗口退回冷路径，结果仍然正确（没少算昨天）",
      canon(window_public(WINDOWS[2][1]))
      == canon(GOLDEN_OBJ["win_" + WINDOWS[2][0]]))


def _old_floor(entry):
    entry.update({"hours_floor_at": TODAY0 + 3600})


state = load_mutated(_old_floor)
check("hours_floor_at 比窗口起点还新：层拒绝服务",
      U._usage_snapshot_window_from_hours(None, WINDOWS[1][1], None, None) is None)


def _new_days_floor(entry):
    entry.update({"days_floor": TODAY_KEY})


state = load_mutated(_new_days_floor)
check("days_floor 比窗口起点还新：层拒绝服务",
      U._usage_snapshot_window_from_hours(None, WINDOWS[1][1], None, None) is None)


def _shifted_at(entry):
    entry["days"][TODAY_KEY]["hours"]["5"]["at"] += 60


state = load_mutated(_shifted_at)
check("小时桶 at 与日桶对不上：层拒绝服务（格点校验）",
      U._usage_snapshot_window_from_hours(None, WINDOWS[1][1], None, None) is None)


def _bad_day_at(entry):
    entry["days"][TODAY_KEY]["at"] += 1800


state = load_mutated(_bad_day_at)
check("日桶 at 被挪了半小时：层拒绝服务（格点校验）",
      U._usage_snapshot_window_from_hours(None, WINDOWS[1][1], None, None) is None)

print()
print("[7] 保留/剪枝：小时层只留最近几天，够不到的窗口退回冷路径")
# 删掉 checkpoint 再从零折：剪枝只在「新建日桶」时发生（与日桶的 days_floor
# 同一套节奏），采用回来的状态要等到第二天新建桶才会剪——这一节量的是剪枝
# 本身，所以从零折一份。
clear_cache_files()
cache_env(enabled=1, min_bytes=10 ** 9, min_seconds=10 ** 9, hour_keep=1)
restart()
live()
state = U._usage_snap_state[None]
floor = state["hours_floor_at"]
check("剪枝线随保留天数前移（keep=1）",
      floor == U._day_key_start(TODAY_KEY) - 86400,
      (floor, U._day_key_start(TODAY_KEY) - 86400))
check("剪枝线以外的天：小时表被置 None（留痕，不再重建）",
      state["days"][DAY2_KEY].get("hours", "missing") is None
      and state["days"][DAY1_KEY].get("hours"),
      (state["days"][DAY2_KEY].get("hours", "missing") is not None,
       state["days"][DAY1_KEY].get("hours") is not None))
check("剪枝线以内的小时表照常（今天）",
      state["days"][TODAY_KEY]["hours"]["5"]["requests"] == 3,
      state["days"][TODAY_KEY]["hours"]["5"]["requests"])
# 剪枝线以外的窗口：走冷路径，结果与关掉日桶/小时层时一致。
since_old = hour_window(45)          # 昨天 03:00（keep=1 时保留期是今天起）
cache_env(enabled=1, min_bytes=10 ** 9, min_seconds=10 ** 9, hour_keep=1)
restart()
got_old = canon(window_public(since_old))
cache_env_off()
restart()
want_old = canon(window_public(since_old))
check("剪枝线以外的整点窗口退回冷路径，结果与关掉开关一致",
      got_old == want_old, first_diff(want_old, got_old))
# 剪枝线以外的迟到行：不重建小时表（重建的表缺了其余小时，会少算）。
cache_env(enabled=1, min_bytes=10 ** 9, min_seconds=10 ** 9, hour_keep=1)
restart()
live()
append_log([row(DAY2 + 6 * 3600, "m-a", "u1", 1000, 100, credit=0.5)])
live()
state = U._usage_snap_state[None]
check("剪枝线以外的迟到行不重建小时表",
      state["days"][DAY2_KEY].get("hours", "missing") is None
      and state["days"][DAY2_KEY]["requests"] > 0,
      state["days"][DAY2_KEY].get("hours", "missing"))
got_late = canon(window_public(since_old))
cache_env_off()
restart()
want_late = canon(window_public(since_old))
check("迟到行之后，剪枝线以外的窗口仍然正确（退回逐行）",
      got_late == want_late, first_diff(want_late, got_late))
write_log(ROWS)
cache_env(hour_keep=None)
cache_env_off()
restart()
GOLDEN_RAW = live()
GOLDEN = canon(GOLDEN_RAW)

print()
print("[8] 开关：WB_USAGE_DAY_BUCKETS=0 时整点窗口与改动前逐字节一致")
cache_env_off()
restart()
ref = canon(window_public(WINDOWS[1][1]))
check("关掉开关：状态里没有日桶/小时桶（窗口路径压根不碰状态）",
      not (U._usage_snap_state.get(None) or {}).get("days"),
      (U._usage_snap_state.get(None) or {}).get("days"))
cache_env_nowrite()
restart()
# 折出一份 all-time（窗口请求本身不折——冷状态直接走扫描，见 T14 的
# _usage_snap_state_cold）：这条比的是「小时层服务」与「关掉开关的扫描」。
U._usage_snapshot_uncached()
got = canon(window_public(WINDOWS[1][1]))
check("开着开关：整点窗口结果与关掉时逐字节一致（精确价下）", ref == got,
      first_diff(ref, got))
check("开着开关：小时桶确实折了",
      U._usage_snap_state[None]["days"][TODAY_KEY]["hours"],
      "没有小时桶")

print()
print("[9] 夏令时：整点窗口跨切换日照样正确，格点被挪动时放弃")
if hasattr(time, "tzset"):
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
                  % ", ".join(ZONES))
        else:
            print("  [INFO] 夏令时用例使用 TZ=%s" % chosen)
            _time.time = lambda: time.mktime((2026, 10, 28, 12, 0, 0, 0, 0, -1))
            now = time.time()
            base = time.mktime((2026, 10, 22, 12, 0, 0, 0, 0, -1))
            dst_rows = [row(base + d * 86400 + h * 3600, "m-a", "u1", 100, 10)
                        for d in range(6) for h in (0, 6, 12, 18)]
            # 切换日（10-25）那一天多一小时：第 25 个小时的行也要进桶。
            dst_rows.append(row(time.mktime((2026, 10, 25, 23, 30, 0, 0, 0, -1)),
                                "m-b", "u2", 200, 20, realm="cn"))
            write_log(dst_rows)
            # 整点窗口：切换日当天与跨切换日各来一个，两个都必须与逐行一致。
            windows = [
                ("dst_day", time.mktime((2026, 10, 25, 0, 0, 0, 0, 0, -1))),
                ("across_dst", time.mktime((2026, 10, 24, 20, 0, 0, 0, 0, -1))),
                ("after_dst", time.mktime((2026, 10, 27, 6, 0, 0, 0, 0, -1))),
            ]
            for label, since in windows:
                cache_env_off()
                restart()
                ref = canon(U._usage_snapshot_uncached(None, since=since,
                                                       until=None))
                cache_env_nowrite()
                restart()
                got = canon(U._usage_snapshot_uncached(None, since=since,
                                                       until=None))
                check("夏令时 %s：整点窗口走层 == 逐行口径（逐字节）" % label,
                      ref == got, first_diff(ref, got))
            # 格点被挪动（夏令时若发生在小时边界以外）：求和侧放弃。
            since = windows[2][1]
            cache_env_nowrite()
            restart()
            # 折出一份 all-time（窗口请求本身不折——冷状态直接走扫描，
            # 见 T14 的 _usage_snap_state_cold），下面的层校验才有桶可看。
            U._usage_snapshot_uncached()
            key = U._local_day_key(since)
            state = U._usage_snap_state[None]
            check("对齐的整点窗口本来就走层",
                  U._usage_snapshot_window_from_hours(None, since, None, None)
                  is not None)
            # 取窗口起点之后的小时（起点那天的更早小时本来就会被跳过）。
            hours = state["days"][key]["hours"]
            h_key = sorted(hours, key=int)[-1]
            hours[h_key]["at"] += 1800
            check("小时桶 at 不落在 3600 格点上时求和放弃（退回冷路径）",
                  U._usage_snapshot_window_from_hours(None, since, None, None)
                  is None)
            hours[h_key]["at"] -= 1800
            check("恢复后求和又能用",
                  U._usage_snapshot_window_from_hours(None, since, None, None)
                  is not None)
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
cache_env_off()
restart()
GOLDEN_RAW = live()
GOLDEN = canon(GOLDEN_RAW)

print()
print("[10] 与日桶的边界：午夜窗口走日桶，整点窗口走小时层，两条路径不打架")
cache_env_nowrite()
restart()
live()
state = U._usage_snap_state[None]
check("午夜起点：_hour_window_key 也认它，但日桶优先（窗口结果仍由日桶给）",
      U._hour_window_key(TODAY0) == (TODAY_KEY, 0)
      and U._window_day_key(TODAY0) == TODAY_KEY,
      (U._hour_window_key(TODAY0), U._window_day_key(TODAY0)))
check("非整点起点：两条路都不认（退回逐行）",
      U._hour_window_key(TODAY0 + 61) is None
      and U._window_day_key(TODAY0 + 61) is None,
      (U._hour_window_key(TODAY0 + 61), U._window_day_key(TODAY0 + 61)))
check("空窗口（起点在未来）走层且全零",
      GOLDEN_OBJ["win_future"]["requests"] == 0
      and GOLDEN_OBJ["win_future"]["cost_cny"] == 0.0,
      GOLDEN_OBJ["win_future"]["requests"])
# 30 分钟偏移的时区语义（不是整点）：起点不是整点时一律逐行。
check("起点不是整点时 _usage_snapshot_window 走逐行路径",
      U._hour_window_key(TODAY0 + 1800) is None,
      U._hour_window_key(TODAY0 + 1800))

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
if FAIL:
    sys.exit(1)
