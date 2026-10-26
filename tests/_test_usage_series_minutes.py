"""时间序列的分钟环：step=60 的窗口（span<=6h）不再整段扫描。

usage_timeseries() 的桶宽由窗口跨度决定：span<=6h 给 step=60（自定义区间，
面板 datetime-local 给的起点就是整分钟）。日桶/小时桶都锚在本地午夜上，
答不了这种任意分钟格点的窗口，于是这一档一直退回整段扫描（真机 6h 窗口
881ms 级，每次 TTL 过期都要重扫一遍）。这一套钉住补上的「最近 6 小时分钟环」：

  * 切分：每行折进「它 at 所属的 epoch 分钟」，与日/小时桶同一 realm 过滤、
    同一折叠口径（completed 计令牌、client_aborted 不计 credit）；
  * 保留：环只留最近 6 小时（与选择器 span<=6h 一一对应），保留线以下的桶
    不建也不留；更早的窗口一律退回整段扫描（正确性优先）；
  * 等价：step=60 的窗口在环可用时与整段扫描逐字节一致（跨午夜、跨小时、
    空窗口、realm 过滤、显式 bucket_seconds=60 与自动选择两条路径、起点是
    int 或 float 都钉住）；环不可用的窗口（起点不在分钟格点、超出 6h、
    hi < max_at、旧 checkpoint 的完整线之前）退回扫描，结果同样一致；
  * checkpoint：环随 series 状态落盘（紧凑的位置数组），新进程采用后窗口与
    冷折逐字节一致；坏环（形状/类型/乱序）整份拒绝并退回扫描；旧版
    checkpoint（没有 minutes 键）按「环只从采用点之后完整」处理；
  * 计数桩：环服务的窗口一次都不碰 _usage_timeseries_scan（省下的工作真实
    存在），退回扫描的窗口必须碰它；
  * 开关：WB_USAGE_DAY_BUCKETS=0 时不折环、窗口与改动前逐字节一致。

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

# 与 _test_usage_day_buckets.py 同一把冻结时钟（窗口边界与 started 都从
# time.time() 派生，不冻结就不可比）。本地时间是 2026-10-08 15:40。
FROZEN = 1791445200.0

_TMP = tempfile.mkdtemp(prefix="wb-minutes-")
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
# 数据：三天 + 午夜前后的密集分钟区 + 跨 realm + 迟写/乱序行
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

# 密集区：TODAY0 前 1 小时到后 1 小时，每 120 秒一行（跨午夜、跨小时）。
# 区间收在 TODAY0 + 3600：max_at 就是 MAIN_HI，分钟窗口的上界条件
# （hi >= max_at）才有解——否则它属于「上界切进桶里」的降级路径。
DENSE_LO = TODAY0 - 3600
DENSE_HI = TODAY0 + 3600
DENSE = list(range(int(DENSE_LO), int(DENSE_HI) + 1, 120))
# 保留线的边界行：正好在「max_at - 6h」的格点上。
EDGE_AT = DENSE_HI - 6 * 3600


def row(at, prompt=10, completion=5, realm="intl", credit=0.0,
        outcome="completed", account="u1"):
    r = {"at": at, "model": "m-a", "stream": True, "outcome": outcome,
         "elapsed_ms": 1000, "ttft_ms": 300, "gen_ms": 700,
         "prompt_tokens": prompt, "completion_tokens": completion,
         "reasoning_tokens": 0, "cached_tokens": 0,
         "account": account, "key": "k1", "tokens_per_sec": 120.0,
         "credit": credit, "total_tokens": prompt + completion}
    if realm is not None:
        r["realm"] = realm
    if outcome != "completed":
        r["error"] = "boom"
    return r


ROWS = []
# 前天/昨天：给日桶、小时桶与 realm 过滤留旧数据（分钟环够不到它们）。
ROWS.append(row(DAY2, 100, 10, realm="cn"))
ROWS.append(row(DAY1 + 3600, 200, 20, credit=0.5))
ROWS.append(row(DAY1 + 7200, 300, 30, realm="cn", credit=0.25))
# 密集区：每 5 分钟一行换 realm，credit 只在部分行上（credits 选择要能对上）。
for index, at in enumerate(DENSE):
    ROWS.append(row(at, 10 + index, 5 + index,
                    realm="cn" if index % 5 == 0 else "intl",
                    credit=0.5 if index % 7 == 0 else 0.0,
                    account="u1" if index % 3 else "u2"))
# 保留线的边界行（正好在 max_at - 6h 的格点上；它让 max_at 落在整分钟上）。
ROWS.append(row(EDGE_AT, 77, 7))
# 失败行与客户端中止：折叠口径要和整段扫描一致。
ROWS.append(row(TODAY0 + 1200, 40, 4, outcome="failed", credit=0.5))
ROWS.append(row(TODAY0 + 1260, 41, 4, outcome="client_aborted", credit=9.0))

PRICING = {
    "meta": {"usd_cny": 7.1},
    "models": {
        "m-a": {"unit": 1, "currency": "CNY",
                "flat": {"input_cache_hit": 0.25, "input_cache_miss": 0.5,
                         "output": 0.125}},
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


def grid(ts):
    """ts 所在（或紧邻的下一个）分钟格点。"""
    return int(ts // 60) * 60


MAX_AT = max(r["at"] for r in ROWS)
MAIN_LO = grid(TODAY0 - 1800)          # 23:30，跨午夜窗口的起点
MAIN_HI = grid(TODAY0 + 3600)          # 01:00
NOW_LO = grid(FROZEN) - 2 * 3600       # 13:40，空窗口（日志里没有这个区间的行）
EDGE_LO = EDGE_AT                      # max_at - 6h，保留线的边界窗口
EDGE_HI = DENSE_HI                     # = max_at


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


def cache_env_off():
    """改动前的口径：checkpoint 关掉、日桶关掉（= 逐行扫描 + 全量冷折）。"""
    cache_env(enabled=0, min_bytes=0, min_seconds=0, days=0)


def cache_env_nowrite():
    """日桶与分钟环开着、checkpoint 关掉（状态每轮重折，不落盘）。"""
    cache_env(enabled=0, min_bytes=0, min_seconds=0, days=None)


def cache_env_read():
    """checkpoint 可读（采用路径）但写盘被节流挡住：文件保持手写的那一份。"""
    cache_env(enabled=1, min_bytes=10 ** 9, min_seconds=10 ** 9, days=None)


def cache_env_force():
    cache_env(enabled=1, min_bytes=0, min_seconds=0, days=None)


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


def window(since, until=None, bucket=None, realm="all", ttl=0):
    return U.usage_timeseries(realm=realm, range="custom", since=since,
                              until=until, bucket_seconds=bucket, ttl=ttl)


def scan_ref(since, until=None, bucket=None, realm="all"):
    """整段扫描的参照：关掉日桶（连环一起关）后同一窗口的输出。"""
    cache_env_off()
    restart()
    out = window(since, until, bucket, realm)
    cache_env_nowrite()
    restart()
    return out


def series_state():
    """realm=all 的窗口折出来的那份状态（状态键是 None）。"""
    return U._series_state.get(None)


def ring(state):
    return state.get("minutes") or {}


class ScanCounter(object):
    """计数桩：包住 _usage_timeseries_scan，数它被调用了几次。"""

    def __init__(self):
        self.calls = 0
        self.original = U._usage_timeseries_scan

    def __enter__(self):
        def counting(*args, **kwargs):
            self.calls += 1
            return self.original(*args, **kwargs)
        U._usage_timeseries_scan = counting
        return self

    def __exit__(self, *exc):
        U._usage_timeseries_scan = self.original
        return False


# --------------------------------------------------------------------------
# 子进程：采用 checkpoint 后取窗口（真·新进程）
# --------------------------------------------------------------------------
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


def win(since, until=None, bucket=None, realm="all"):
    return U.usage_timeseries(realm=realm, range="custom", since=since,
                              until=until, bucket_seconds=bucket, ttl=0)


out = {"main": win(__MAIN_LO__, __MAIN_HI__),
       "edge": win(__EDGE_LO__, __EDGE_HI__),
       "now": win(__NOW_LO__),
       "cn": win(__MAIN_LO__, __MAIN_HI__, realm="cn")}
state = U._series_state.get(None) or {}
out["ring_keys"] = sorted(state.get("minutes") or {})
out["minutes_from"] = state.get("minutes_from")
with open(os.environ["WB_CHILD_OUT"], "w", encoding="utf-8") as fh:
    json.dump({"checkpointed": U._usage_cache_checkpointed, "results": out}, fh,
              ensure_ascii=False)
print("child ok")
'''
CHILD_SCRIPT = os.path.join(_TMP, "child_runner.py")
io.open(CHILD_SCRIPT, "w", encoding="utf-8", newline="\n").write(
    RUNNER.replace("__FROZEN__", repr(FROZEN)).replace("__ROOT__", repr(ROOT))
    .replace("__MAIN_LO__", repr(MAIN_LO)).replace("__MAIN_HI__", repr(MAIN_HI))
    .replace("__EDGE_LO__", repr(EDGE_LO)).replace("__EDGE_HI__", repr(EDGE_HI))
    .replace("__NOW_LO__", repr(NOW_LO)))


def run_child():
    dest = os.path.join(_TMP, "child_out.json")
    if os.path.exists(dest):
        os.unlink(dest)
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["WB_CHILD_OUT"] = dest
    # 子进程必须真的走采用路径：cache 打开、写盘节流挡住（文件保持父进程
    # 写的那一份，后面的用例接着用它）。
    env["WB_USAGE_CACHE"] = "1"
    env["WB_USAGE_CACHE_MIN_BYTES"] = str(10 ** 9)
    env["WB_USAGE_CACHE_MIN_SECONDS"] = str(10 ** 9)
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


def series_entry(data, realm=None):
    for e in data["series"]:
        if e.get("realm") == realm:
            return e
    raise AssertionError("没有 series/%r" % (realm,))


def make_checkpoint():
    """从零折一遍再落盘：删掉旧文件，免得这一轮的状态是从上一份采用来的。"""
    cache_env_force()
    if bool(cache_files()):
        clear_cache_files()
    restart()
    window(MAIN_LO, MAIN_HI)
    window(NOW_LO)
    return bool(cache_files())


# --------------------------------------------------------------------------
# [0] 基线（日桶/小时桶开着，分钟环也折）
# --------------------------------------------------------------------------
write_pricing(PRICING)
write_log(ROWS)
cache_env_off()
restart()
BASE_MAIN = canon(window(MAIN_LO, MAIN_HI))
BASE_NOW = canon(window(NOW_LO))
BASE_CN = canon(window(MAIN_LO, MAIN_HI, realm="cn"))
BASE_EDGE = canon(window(EDGE_LO, EDGE_HI))
check("基线：跨午夜窗口有内容且桶宽是 60",
      json.loads(BASE_MAIN)["bucket_seconds"] == 60
      and len(json.loads(BASE_MAIN)["series"]) > 10,
      len(json.loads(BASE_MAIN)["series"]))
check("基线：空窗口（日志里没有这段）是空序列",
      json.loads(BASE_NOW)["series"] == []
      and json.loads(BASE_NOW)["credits"] == [])
check("基线：realm 过滤下 cn 的令牌数少于 all",
      sum(b["total_tokens"] for b in json.loads(BASE_CN)["series"])
      < sum(b["total_tokens"] for b in json.loads(BASE_MAIN)["series"]))
check("基线：边界窗口（max_at - 6h .. max_at）有内容",
      len(json.loads(BASE_EDGE)["series"]) > 1,
      len(json.loads(BASE_EDGE)["series"]))

print()
print("[1] 切分与保留：每行折进它 at 所属的 epoch 分钟，环只留最近 6 小时")
cache_env_nowrite()
restart()
window(MAIN_LO, MAIN_HI)
state = series_state()
minutes = ring(state)
check("环的键是 epoch 分钟整数，at == 键*60",
      minutes and all(isinstance(k, int) for k in minutes)
      and all(int(b["at"]) == k * 60 for k, b in minutes.items()),
      sorted(minutes)[:3])
check("环里没有超过 max_at 的桶",
      all(k * 60 <= state["max_at"] for k in minutes), max(minutes))
check("保留线：最早的桶不早于 max_at - 6h",
      min(minutes) * 60 >= state["max_at"] - U._SERIES_MINUTE_WINDOW,
      (min(minutes) * 60, state["max_at"] - U._SERIES_MINUTE_WINDOW))
check("前天/昨天的行不建桶（环只留最近 6 小时）",
      (DAY1 + 3600) // 60 not in minutes and (DAY2 // 60) not in minutes)
check("环的桶数与密集区行数相符（每 120 秒一行 => 约一半分钟被占）",
      20 <= len(minutes) <= 120, len(minutes))
# 边界行（正好在 max_at - 6h 的格点上）必须在环里：窗口 [max_at-6h, max_at]
# 要能服务得动。
check("边界行（max_at - 6h 整点）在环里",
      EDGE_AT // 60 in minutes, sorted(minutes)[:2])
# 折叠途中的上限：环不会无限长（冷折时先涨后剪）。
check("折叠途中的环有上限（<= 上限值）",
      len(minutes) <= U._SERIES_MINUTE_RING_CAP, len(minutes))

print()
print("[2] 等价：step=60 的窗口在环可用时与整段扫描逐字节一致")
cases = [
    ("跨午夜窗口（23:30 .. 01:00）", dict(since=MAIN_LO, until=MAIN_HI)),
    ("空窗口（最近 2h，日志里没有行）", dict(since=NOW_LO)),
    ("realm=cn 的跨午夜窗口",
     dict(since=MAIN_LO, until=MAIN_HI, realm="cn")),
    ("保留线边界窗口（max_at - 6h .. max_at）",
     dict(since=EDGE_LO, until=EDGE_HI)),
    ("显式 bucket_seconds=60（同跨午夜窗口）",
     dict(since=MAIN_LO, until=MAIN_HI, bucket=60)),
    ("起点偏移 7 分钟（不落整点，仍在格点上）",
     dict(since=MAIN_LO + 420, until=MAIN_HI)),
]
for label, kwargs in cases:
    ref = canon(scan_ref(**kwargs))
    with ScanCounter() as counter:
        cache_env_nowrite()
        restart()
        got = canon(window(**kwargs))
    check(label + "：环服务、与整段扫描逐字节一致（扫描 0 次）",
          got == ref and counter.calls == 0,
          (first_diff(ref, got), counter.calls))
# 起点是 int / float 两条：公开入口把 since 归一成 float（_epoch_or_none），
# 所以 int 那条要直接调切片与扫描来钉（整段扫描的桶 at 是 lo + k*60，类型
# 跟着 lo 走，切片必须对齐）。
cache_env_nowrite()
restart()
window(MAIN_LO, MAIN_HI)
state = series_state()
int_slice = U._series_slice(state, None, int(MAIN_LO), int(MAIN_HI), 60, TODAY_KEY)
int_scan = U._usage_timeseries_scan(None, int(MAIN_LO), int(MAIN_HI), 60)
check("int 起点：切片服务，且与整段扫描逐字节一致",
      int_slice is not None and canon(int_slice) == canon(int_scan),
      first_diff(canon(int_scan), canon(int_slice or {})))
check("int 起点的桶 at 是整数（与整段扫描同型）",
      all(isinstance(b["at"], int) for b in int_slice["series"]),
      int_slice["series"][0]["at"])
float_out = window(float(MAIN_LO), float(MAIN_HI))
check("float 起点的桶 at 是浮点（与整段扫描同型）",
      all(isinstance(b["at"], float) for b in float_out["series"]),
      float_out["series"][0]["at"])
check("两种起点的桶值相同（只有 at 的写法不同）",
      canon([{k: v for k, v in b.items() if k != "at"}
             for b in int_slice["series"]])
      == canon([{k: v for k, v in b.items() if k != "at"}
                for b in float_out["series"]]))

# credits：窗口内的 credit 行按 at 倒序取前 50，与扫描逐条一致。
credit_rows = [row(TODAY0 - 1000 + i * 5, 10, 1, credit=0.25)
               for i in range(60)]
write_log(ROWS + credit_rows)
cache_env_nowrite()
restart()
window(MAIN_LO, MAIN_HI)
got = window(MAIN_LO, MAIN_HI)
ref_credits = scan_ref(MAIN_LO, MAIN_HI)["credits"]
check("credits 恰好 50 条（缓冲裁过）", len(got["credits"]) == 50,
      len(got["credits"]))
check("credits 与整段扫描的前 50 条逐条相同",
      canon(got["credits"]) == canon(ref_credits),
      first_diff(canon(ref_credits), canon(got["credits"])))
write_log(ROWS)

print()
print("[3] 降级：环答不了的窗口一律退回整段扫描，结果照样一致")
fallback = [
    ("起点不在分钟格点（+30 秒）",
     dict(since=MAIN_LO + 30, until=MAIN_HI)),
    ("起点超出保留线（max_at - 6h - 60）",
     dict(since=EDGE_LO - 60, until=EDGE_HI, bucket=60)),
    ("窗口上界早于折过的最大 at",
     dict(since=MAIN_LO, until=MAIN_HI - 600)),
    ("显式桶宽 90（不是分钟环的格点）",
     dict(since=MAIN_LO, until=MAIN_HI, bucket=90)),
    ("显式桶宽 120（同上）",
     dict(since=MAIN_LO, until=MAIN_HI, bucket=120)),
    ("起点大得离谱（手写 URL，6e18 的整分钟）：不抛错、退回扫描",
     dict(since=6e18, bucket=60)),
]
for label, kwargs in fallback:
    ref = canon(scan_ref(**kwargs))
    with ScanCounter() as counter:
        cache_env_nowrite()
        restart()
        window(MAIN_LO, MAIN_HI)          # 先把状态折起来
        got = canon(window(**kwargs))
    check(label + "：退回扫描、结果一致（扫描 >=1 次）",
          got == ref and counter.calls >= 1,
          (first_diff(ref, got), counter.calls))
# 直接用 _series_slice 钉住拒绝条件（不看输出，看它自己返回 None）。
cache_env_nowrite()
restart()
window(MAIN_LO, MAIN_HI)
state = series_state()
check("_series_slice 对非格点起点返回 None",
      U._series_slice(state, None, MAIN_LO + 30, MAIN_HI, 60, TODAY_KEY) is None)
check("_series_slice 对超出保留线的起点返回 None",
      U._series_slice(state, None, EDGE_LO - 60, EDGE_HI, 60, TODAY_KEY) is None)
check("_series_slice 对 hi < max_at 返回 None",
      U._series_slice(state, None, MAIN_LO, MAIN_HI - 600, 60, TODAY_KEY) is None)
check("_series_slice 对格点窗口返回切片",
      U._series_slice(state, None, MAIN_LO, MAIN_HI, 60, TODAY_KEY) is not None)
check("_series_minute_grid_ok 只放行 60 的整数倍",
      U._series_minute_grid_ok(MAIN_LO) and U._series_minute_grid_ok(float(MAIN_LO))
      and not U._series_minute_grid_ok(MAIN_LO + 30)
      and not U._series_minute_grid_ok(MAIN_LO + 0.5))

print()
print("[4] 迟写行：保留线以内的补写折进环，线以外的丢掉")
# 保留线以内（max_at - 2h）的迟到行：必须在环里，窗口两边都要看见它。
late_in = DENSE_HI - 7200 + 60
append_log([row(late_in, 500, 50, credit=0.5)])
cache_env_nowrite()
restart()
got_late = window(DENSE_HI - 3 * 3600, DENSE_HI)
check("保留线以内的迟到行进了环",
      (late_in // 60) in ring(series_state()), sorted(ring(series_state()))[:3])
late_bucket = ring(series_state()).get(late_in // 60)
check("迟到行的令牌进了它自己那一分钟的桶",
      late_bucket is not None and late_bucket["requests"] == 1
      and late_bucket["total_tokens"] == 550
      and late_bucket["credit"] == 0.5, late_bucket)
ref_late = canon(scan_ref(DENSE_HI - 3 * 3600, DENSE_HI))
check("迟到行进环后窗口仍与整段扫描一致",
      canon(got_late) == ref_late,
      first_diff(ref_late, canon(got_late)))
write_log(ROWS)
# 保留线以外（max_at - 7h）的迟到行：不建桶；含它的窗口本来就服务不了，
# 退回扫描后结果照样一致。
late_out = DENSE_HI - 7 * 3600
append_log([row(late_out, 900, 90)])
cache_env_nowrite()
restart()
window(MAIN_LO, MAIN_HI)
check("保留线以外的迟到行不建桶",
      (late_out // 60) not in ring(series_state()),
      sorted(ring(series_state()))[:3])
check("含线外迟到行的窗口（显式 60s、跨度 >6h）退回扫描、结果一致",
      canon(window(late_out - 60, DENSE_HI, bucket=60))
      == canon(scan_ref(late_out - 60, DENSE_HI, bucket=60)))
write_log(ROWS)

print()
print("[5] checkpoint：环随 series 状态落盘/加载，重启后逐字节一致")
check("写盘成功", make_checkpoint())
stored = cache_data()
entry = series_entry(stored, None)
minutes_rows = entry.get("minutes")
check("series 条目带 minutes（升序的位置数组）",
      isinstance(minutes_rows, list) and len(minutes_rows) > 10
      and all(isinstance(r, list) and len(r) == 1 + len(U._SERIES_MINUTE_FIELDS)
              for r in minutes_rows),
      (type(minutes_rows).__name__,
       len(minutes_rows) if isinstance(minutes_rows, list) else None))
check("落盘行的分钟严格升序",
      all(minutes_rows[i][0] < minutes_rows[i + 1][0]
          for i in range(len(minutes_rows) - 1)))
check("落盘行就是内存环的紧凑形态（键+8 个数字）",
      len(minutes_rows) == len(ring(series_state()))
      and minutes_rows[0][0] == min(ring(series_state())),
      (len(minutes_rows), len(ring(series_state()))))
# 体积预算：满环（6 小时最多 361 个桶）也要压在 16KB 以内——series 已经是
# checkpoint 最大的一份，字典形状（每桶 9 个字段名）会平白多出几十 KB。
# 位置数组下满环约 14.4KB（每桶 ~40 字节），相对真机整份 checkpoint
# （541-675KB）只有 2~3%。
full_rows = [[k, 14, 0, 840, 420, 140, 70, 1470, 0.35]
             for k in range(30000000, 30000000 + 361)]
full_bytes = len(json.dumps(full_rows, separators=(",", ":")))
check("满环的落盘体积 <= 16KB（体积预算）", full_bytes <= 16000, full_bytes)
check("落盘体积比字典形状小得多（同一批桶）",
      full_bytes < len(json.dumps([dict(zip(
          ("at",) + U._SERIES_MINUTE_FIELDS,
          [k * 60, 14, 0, 840, 420, 140, 70, 1470, 0.35]))
          for k in range(30000000, 30000000 + 361)],
          separators=(",", ":"))))
check("minutes_from 是 None（从零折起的环对整份日志完整）",
      entry.get("minutes_from") is None, entry.get("minutes_from"))

cache_env_read()
restart()
LOADED_MAIN = canon(window(MAIN_LO, MAIN_HI))
check("重启（本进程，采用 checkpoint）后窗口与冷折逐字节一致",
      LOADED_MAIN == BASE_MAIN and "series" in U._usage_cache_checkpointed,
      (first_diff(BASE_MAIN, LOADED_MAIN), sorted(U._usage_cache_checkpointed)))
child, adopted = run_child()
check("新进程加载到了 checkpoint（含 series）",
      "series" in adopted, sorted(adopted))
check("新进程的跨午夜窗口与冷折逐字节一致",
      canon(child["main"]) == BASE_MAIN, first_diff(BASE_MAIN, canon(child["main"])))
check("新进程的空窗口与冷折逐字节一致", canon(child["now"]) == BASE_NOW)
check("新进程的 realm 窗口与冷折逐字节一致", canon(child["cn"]) == BASE_CN)
check("新进程的边界窗口与冷折逐字节一致", canon(child["edge"]) == BASE_EDGE)
check("新进程的环与冷折的环同一批键",
      child["ring_keys"] == sorted(ring(series_state())),
      (child["ring_keys"][:3], sorted(ring(series_state()))[:3]))

print()
print("[6] 旧版 checkpoint（没有 minutes 键）：环只从采用点之后完整")
make_checkpoint()
data = cache_data()
entry = series_entry(data, None)
entry.pop("minutes")
entry.pop("minutes_from")
save_cache(data)
cache_env_read()
restart()
window(MAIN_LO, MAIN_HI)              # 采用旧条目 + 折到文件尾
state = series_state()
check("采用旧条目后环是空的、minutes_from 记成采用时的 max_at",
      ring(state) == {} and state.get("minutes_from") == MAX_AT,
      (ring(state), state.get("minutes_from")))
check("旧条目采用后，早于完整线的窗口退回扫描、结果一致",
      canon(window(MAIN_LO, MAIN_HI)) == BASE_MAIN)
# 采用点之后追加一行（at 晚于 minutes_from）：起点晚于完整线的窗口由环服务。
append_log([row(DENSE_HI + 180, 600, 60, credit=0.5)])
new_at = DENSE_HI + 180
with ScanCounter() as counter:
    got = canon(window(new_at, DENSE_HI + 600))
check("新行确实进了环",
      (new_at // 60) in ring(series_state()))
ref_new = canon(scan_ref(new_at, DENSE_HI + 600))
check("完整线之后的窗口由环服务（扫描 0 次）、结果一致",
      got == ref_new and counter.calls == 0,
      (first_diff(ref_new, got), counter.calls))
write_log(ROWS)

print()
print("[7] 坏环：形状/类型/乱序一律整份拒绝，结果回到真值")


def poison(entry):
    """给环里最新那一桶加 1000 次请求；被采用时窗口数字会当场变样。"""
    rows = entry.get("minutes")
    if isinstance(rows, list) and rows:
        rows[-1][1] += 1000


def mutate(mutate_fn, label):
    """poison + 变异：整份 series 条目必须被拒绝，窗口退回扫描、结果回到真值。"""
    make_checkpoint()
    data = cache_data()
    entry = series_entry(data, None)
    poison(entry)
    mutate_fn(entry)
    save_cache(data)
    cache_env_read()
    restart()
    got = canon(window(MAIN_LO, MAIN_HI))
    check(label, got == BASE_MAIN, first_diff(BASE_MAIN, got))


# 7.0 自检：合法形状 + 改过的值必须被采用（poison 可见），否则下面的用例
# 可能什么都没测到。
make_checkpoint()
data = cache_data()
poison(series_entry(data, None))
save_cache(data)
cache_env_read()
restart()
check("poison 自检：形状合法的坏环会被采用（标记出现在结果里）",
      canon(window(MAIN_LO, MAIN_HI)) != BASE_MAIN, "结果没有变化")

mutate(lambda e: e.update({"minutes": "x"}), "minutes 不是列表")
mutate(lambda e: e.update({"minutes": []}),
       "minutes 是空列表（环该有的桶没了）：退回扫描、结果仍正确")
mutate(lambda e: e["minutes"][-1].__setitem__(0, e["minutes"][-1][0] + 0.5),
       "分钟不是整数")
mutate(lambda e: e["minutes"][-1].pop(), "行短了一个字段")
mutate(lambda e: e["minutes"][-1].append(1), "行长了一个字段")
mutate(lambda e: e["minutes"][-1].__setitem__(1, "多"),
       "行里的数字字段被写成字符串")
mutate(lambda e: e["minutes"].__setitem__(-1, list(reversed(e["minutes"][-1]))),
       "行的字段顺序被打乱（分钟跑到最后）")
mutate(lambda e: e["minutes"].reverse(), "行序倒过来（分钟不再升序）")
mutate(lambda e: e["minutes"].__setitem__(1, list(e["minutes"][0])),
       "两行同一个分钟（重复）")
mutate(lambda e: e.update({"minutes_from": "now"}), "minutes_from 不是数字")
mutate(lambda e: e.update({"minutes_from": MAX_AT + 86400}),
       "minutes_from 是数字但很晚：被采用，早于它的窗口退回扫描，结果仍正确")

print()
print("[8] 开关：WB_USAGE_DAY_BUCKETS=0 时不折环、窗口与改动前逐字节一致")
cache_env(enabled=1, min_bytes=10 ** 9, min_seconds=10 ** 9, days=0)
restart()
window(MAIN_LO, MAIN_HI)
state = series_state()
check("关掉日桶：状态里一个日桶、一个分钟桶都没有",
      not (state or {}).get("days") and not ring(state or {}),
      ((state or {}).get("days"), ring(state or {})))
check("关掉日桶：窗口退回扫描、结果与基线一致",
      canon(window(MAIN_LO, MAIN_HI)) == BASE_MAIN)
# 落盘也得空着：这一轮不许 make_checkpoint（它会强制打开日桶）。日桶关掉时
# 窗口路径连 series 状态都不碰，所以连 checkpoint 都不会写出来。
cache_env(enabled=1, min_bytes=0, min_seconds=0, days=0)
clear_cache_files()
restart()
window(MAIN_LO, MAIN_HI)
check("关掉日桶：series 状态根本不折（没有 checkpoint 可写）",
      not bool(cache_files()))
cache_env(days=None)
# 关掉 checkpoint 但环开着：环仍然工作、结果与基线一致。
cache_env(enabled=0, min_bytes=0, min_seconds=0)
restart()
check("checkpoint 关掉但环开着：结果与基线一致",
      canon(window(MAIN_LO, MAIN_HI)) == BASE_MAIN)
check("checkpoint 关掉但环开着：环确实折了", bool(ring(series_state())))
cache_env()

shutil.rmtree(_TMP, ignore_errors=True)
print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
