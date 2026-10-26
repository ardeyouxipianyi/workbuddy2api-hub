"""analytics 小时层：自定义区间窗口的就近取值。

日桶只拆得开「起点是本地午夜 + until 开放」的窗口；面板自定义区间的起点是
任意时刻，那条路径一直整段扫描（路由器上冷 49.4s）。analytics 的 token 口径
含失败行（`_feed_analytics`），与 snapshot/series 不同，借不了别人的层——所以
这一套钉住 analytics 自己的小时子表：

  * 切分：每行折进「它 at 所属那天的那一小时」，小时键是「离本地午夜多少
    秒」（字符串），迟写（at 早于折叠时刻）的行也落回它时间戳所属的那天那
    小时；realm 不匹配的行不建桶；
  * 等价：起点整小时的自定义窗口由小时桶相加，与改动前的逐行口径一致。
    测试数据的价刻意用二进制可精确求和的数（0.5/0.25/0.125）与 unit=1，所以
    连 cost_cny 都能逐字节比；价不是精确值时只有浮点求和顺序的差异，这一条
    单独钉在 [8]；
  * 逐字节：同一日志同一窗口，「走层」与「强制整段扫（现折小时桶）」逐字节
    一致；省下的工作用计数桩证明（窗口回答不再付 cost_for_row）；
  * 覆盖不到就退回扫描：非整小时的起点、until 切在桶中间、小时被剪掉、
    days_floor 以下——一律退回，结果与改动前的逐行口径一致；
  * checkpoint：小时子表与 max_at 随状态落盘/加载，重启（含真·子进程）后
    与冷折逐字节一致；旧格式的日桶（没有 at）整体拒收 → 重折一次；而旧天
    被剪掉的小时表（hours 键缺席或为 null）**不算损坏**，状态照常采用（#26，采纳用例见 _test_usage_aggregate_cache.py）；
  * 降级：坏小时子表（形状/小时键/at/max_at）一律当作没有缓存，结果回到真值；
  * 保留：WB_USAGE_HOUR_KEEP_HOURS 决定小时子表的保留线，剪掉的天退回扫描；
  * 开关：WB_USAGE_DAY_BUCKETS=0 时与改动前逐字节一致（旧口径）。

降级用例沿用 _test_usage_day_buckets.py 的写法：先给「应当被拒绝」的那份状态
加一个一眼可见的标记（poison），再做变异。标记没被采用就说明校验拦住了它；
一旦被错误采用，结果里就会多出那个标记，用例当场失败。

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

# 与 _test_usage_day_buckets.py 同一把冻结时钟：窗口边界（本地午夜/整点）与
# started/since 都从 time.time() 派生，不冻结就不可比。
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
# 数据：三个本地日 + 整点/半点行 + 迟写行 + 无价模型 + 失败/中止 + 双 realm
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
# 旗舰窗口：昨天 12:00 起（整点、不是午夜——日桶拆不开的那种）。
NOON1 = DAY1 + 12 * 3600
# 非整点起点：昨天 12:30。
NOON30 = NOON1 + 1800


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
    # 前天（DAY2）：两个整点小时，外加一行在午夜整点（属于 DAY2 的 0 点）。
    row(DAY2, "m-a", "u1", 100, 10),
    row(DAY2 + 3600, "m-b", "u2", 200, 20, realm="cn"),
    row(DAY2 + 12 * 3600, "m-a", "u1", 150, 15),
    # 昨天（DAY1）：11/12/23 点，含 12:00 整点与 12:30 半点。
    row(DAY1 + 11 * 3600, "m-a", "u1", 300, 30, credit=0.5),
    row(NOON1, "m-a", "u2", 400, 40, key="k2"),
    row(NOON30, "m-c", "u1", 500, 50, outcome="failed", credit=0.25),
    row(DAY1 + 23 * 3600, "m-b", "u2", 350, 35, realm="cn", speed=90.0),
    # 今天（TODAY0）：0 点与 13 点，含无价模型、客户端中止、无 key 字段的老行。
    row(TODAY0, "m-a", "u1", 600, 60, credit=1.0),
    row(TODAY0 + 1800, "m-b", "u2", 700, 70, realm="cn", speed=90.0),
    row(TODAY0 + 13 * 3600, "m-a", "u2", 800, 80, outcome="client_aborted"),
    row(TODAY0 + 13 * 3600 + 60, "no-price-model", "u1", 900, 90),
]
_pre_key = row(TODAY0 + 13 * 3600 + 120, "m-c", "u1", 110, 11)
del _pre_key["key"]
ROWS.append(_pre_key)
_no_realm = row(TODAY0 + 13 * 3600 + 180, "m-c", "u2", 120, 12, realm=None)
ROWS.append(_no_realm)

# 价目表刻意用二进制可精确表示的数（0.5 / 0.25 / 0.125）与 unit=1：
# 逐行累加与按小时累加因此都精确，自定义窗口也能逐字节比对（见文件头）。
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
# 开关与重启（同 _test_usage_day_buckets.py，多一个 WB_USAGE_HOUR_KEEP_HOURS）
# --------------------------------------------------------------------------
def cache_env(enabled=None, min_bytes=None, min_seconds=None, days=None,
              keep=None, hours=None):
    for name, value in (("WB_USAGE_CACHE", enabled),
                        ("WB_USAGE_CACHE_MIN_BYTES", min_bytes),
                        ("WB_USAGE_CACHE_MIN_SECONDS", min_seconds),
                        ("WB_USAGE_DAY_BUCKETS", days),
                        ("WB_USAGE_DAY_KEEP_DAYS", keep),
                        ("WB_USAGE_HOUR_KEEP_HOURS", hours)):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = str(value)


def cache_env_nowrite():
    cache_env(enabled=1, min_bytes=10 ** 9, min_seconds=10 ** 9, days=None)


def cache_env_force():
    cache_env(enabled=1, min_bytes=0, min_seconds=0, days=None)


def cache_env_off():
    """改动前的口径：checkpoint 关掉、日桶/小时桶关掉（= 逐行扫描）。"""
    cache_env(enabled=0, min_bytes=0, min_seconds=0, days=0)


def restart():
    """清掉所有状态、TTL 缓存与「已读过 checkpoint」的记忆，模拟进程重启。"""
    U._analytics_state.clear()
    U._analytics_cache.clear()
    U._snap_cache.clear()
    U._usage_snap_state.clear()
    U._usage_cache_forget()
    U._usage_cache_progress.clear()
    U._usage_cache_checkpointed.clear()
    U._usage_cache_last_attempt = time.time()
    U._usage_cache_digest_memo.clear()


def win(lo, hi=None, realm=None, ttl=0):
    """公开入口的窗口结果（analytics）。"""
    return U._compute_usage_analytics_uncached(realm=realm, since=lo, until=hi)


def live():
    """几个关键窗口 + 全量：跨路径比较都从这一份取。"""
    out = {}
    for name, lo, hi in (("noon1", NOON1, None),
                         ("noon1_until", NOON1, FROZEN),
                         ("noon30", NOON30, None),
                         ("today_until", TODAY0, FROZEN),
                         ("all", None, None)):
        out["an_" + name] = win(lo, hi)
    out["an_noon1_cn"] = win(NOON1, None, realm="cn")
    return out


def deep(obj):
    """一份结构深拷贝（比较「改动前」的结果用；活状态会被就地改）。"""
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

TODAY0 = U._local_midnight(FROZEN)
DAY1 = TODAY0 - 86400
NOON1 = DAY1 + 12 * 3600
out = {}
for name, lo, hi in (("noon1", NOON1, None),
                     ("noon1_until", NOON1, FROZEN),
                     ("noon30", NOON1 + 1800, None),
                     ("today_until", TODAY0, FROZEN),
                     ("all", None, None)):
    out["an_" + name] = U._compute_usage_analytics_uncached(since=lo, until=hi)
out["an_noon1_cn"] = U._compute_usage_analytics_uncached(realm="cn", since=NOON1,
                                                         until=None)
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


def clean_checkpoint():
    """删掉盘上的 checkpoint：让接下来的状态从零折起（不带上一段的标记）。"""
    try:
        clear_cache_files()
    except OSError:
        pass


def make_checkpoint():
    cache_env_force()
    restart()
    live()
    return bool(cache_files())


def an_entry(data, realm=None):
    for e in data["analytics"]:
        if e.get("realm") == realm:
            return e
    raise AssertionError("没有 analytics/%r" % (realm,))


def newest_day(data, realm=None):
    days = an_entry(data, realm)["days"]
    return days[sorted(days)[-1]]


def newest_hour(data, realm=None):
    """最新那天最新的那个小时桶（键与桶一起）。"""
    hours = newest_day(data, realm)["hours"]
    key = sorted(hours, key=int)[-1]
    return key, hours[key]


# --------------------------------------------------------------------------
# 计数桩：证明「走层」时窗口回答不再付计价/扫描
# --------------------------------------------------------------------------
COST_CALLS = {"n": 0}
_REAL_COST_FOR_ROW = wb_pricing.cost_for_row


def counting_cost_for_row(*args, **kwargs):
    COST_CALLS["n"] += 1
    return _REAL_COST_FOR_ROW(*args, **kwargs)


wb_pricing.cost_for_row = counting_cost_for_row


def cost_calls(fn):
    """计价计数：先清行级记忆（定价表），否则同一批行重复折会被记忆挡住、
    数不到「这一遍真的付了多少计价」。"""
    U._row_day_memo.clear()
    U._row_cost_memo.clear()
    COST_CALLS["n"] = 0
    out = fn()
    return out, COST_CALLS["n"]


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
# 开放窗口（until=None）没有上界，是带上界窗口的超集：夹具里「今天 13:00」的
# 行在部分时区（CI 的 UTC 07:40）晚于冻结时刻，只有开放窗口会把它们算进来。
check("基线（改动前口径）：旗舰窗口有内容，开放窗口覆盖带上界窗口的超集",
      GOLDEN_OBJ["an_noon1"]["summary"]["window"]["total_tokens"] > 0
      and GOLDEN_OBJ["an_noon1"]["summary"]["window"]["total_tokens"]
      >= GOLDEN_OBJ["an_noon1_until"]["summary"]["window"]["total_tokens"],
      (GOLDEN_OBJ["an_noon1"]["summary"]["window"]["total_tokens"],
       GOLDEN_OBJ["an_noon1_until"]["summary"]["window"]["total_tokens"]))
check("基线（关掉日桶/小时桶）：状态里没有日桶、也没有小时子表",
      not U._analytics_state[None]["days"],
      U._analytics_state[None]["days"])
check("基线：失败行的 token 计进窗口、requests 不计（口径自检）",
      GOLDEN_OBJ["an_noon1"]["summary"]["window"]["errors"] == 1
      and GOLDEN_OBJ["an_noon1"]["summary"]["window"]["total_tokens"] >= 500 + 50,
      GOLDEN_OBJ["an_noon1"]["summary"]["window"])
check("基线：客户端中止行整行不进 analytics（口径自检）",
      "m-a" in {m["model"] for m in GOLDEN_OBJ["an_noon1"]["models"]}
      and 800 not in {m["window"]["total_tokens"]
                      for m in GOLDEN_OBJ["an_noon1"]["models"]},
      [m["model"] for m in GOLDEN_OBJ["an_noon1"]["models"]])

print()
print("[1] 切分：每行折进它 at 所属的本地日与小时（整点、半点、迟写、跨 realm）")
cache_env_nowrite()
restart()
live()
days = U._analytics_state[None]["days"]
check("日桶键 == 日志里出现过的本地日",
      sorted(days) == [DAY2_KEY, DAY1_KEY, TODAY_KEY], sorted(days))
check("日桶带 at（本地午夜）与 hours 子表",
      all(isinstance(b.get("at"), (int, float)) and isinstance(b.get("hours"), dict)
          for b in days.values()),
      [(k, b.get("at"), type(b.get("hours")).__name__)
       for k, b in sorted(days.items())])
hours1 = days[DAY1_KEY]["hours"]
check("小时键是「离本地午夜多少秒」的字符串",
      sorted(hours1, key=int) == ["11", "12", "23"],
      sorted(hours1, key=int))
check("小时子桶的 at == 日桶 at + 小时*3600",
      all(int(hours1[h]["at"]) == int(days[DAY1_KEY]["at"]) + int(h) * 3600
          for h in hours1),
      {h: hours1[h]["at"] for h in sorted(hours1, key=int)})
# 11 点那行（300+30）在 11 桶；12:00 与 12:30 两行（400+40 与 500+50）在 12 桶。
check("整点行与半点行落进同一个 12 点桶",
      hours1["12"]["summary"]["total_tokens"] == 400 + 40 + 500 + 50,
      hours1["12"]["summary"]["total_tokens"])
check("11 点桶只装 11 点那行",
      hours1["11"]["summary"]["total_tokens"] == 300 + 30,
      hours1["11"]["summary"]["total_tokens"])
check("12 点桶里的失败行：tokens 计入、errors 计 1、requests 不计",
      hours1["12"]["summary"]["errors"] == 1
      and hours1["12"]["summary"]["requests"] == 1,
      hours1["12"]["summary"])
check("不限 realm 的折叠里 cn 行照常进桶",
      hours1["23"]["summary"]["requests"] == 1
      and hours1["23"]["accts"]["u2"]["window"]["requests"] == 1,
      hours1["23"]["accts"].keys())
# realm=cn 是另一份状态：只有 cn 的行。
U._compute_usage_analytics_uncached(realm="cn", since=NOON1, until=None)
cn_days = U._analytics_state["cn"]["days"]
check("realm=cn 的折叠独立成一份状态，只装 cn 的行",
      sorted(cn_days[DAY1_KEY]["hours"], key=int) == ["23"]
      and sorted(days[DAY1_KEY]["hours"], key=int) == ["11", "12", "23"],
      (sorted(cn_days[DAY1_KEY]["hours"], key=int),
       sorted(days[DAY1_KEY]["hours"], key=int)))
check("max_at 是折过的最大 at（含 realm 不匹配的行）",
      U._analytics_state[None]["max_at"] == TODAY0 + 13 * 3600 + 180,
      (U._analytics_state[None]["max_at"], TODAY0 + 13 * 3600 + 180))
# 迟写行：把一条 at 属于昨天 12:15 的行追加到今天，它必须落进昨天 12 点桶。
append_log([row(NOON1 + 900, "m-a", "u1", 1000, 100, credit=0.5)])
live()
late = U._analytics_state[None]["days"][DAY1_KEY]["hours"]["12"]
check("迟写行落进它时间戳所属的那天那小时（不是折叠时刻那小时）",
      late["summary"]["total_tokens"] == 400 + 40 + 500 + 50 + 1000 + 100
      and U._analytics_state[None]["days"][TODAY_KEY]["hours"]["13"]
          ["summary"]["total_tokens"] == 900 + 90 + 110 + 11 + 120 + 12,
      late["summary"]["total_tokens"])
write_log(ROWS)
cache_env_off()
restart()
GOLDEN_RAW = live()
GOLDEN = canon(GOLDEN_RAW)
GOLDEN_OBJ = json.loads(GOLDEN)

print()
print("[2] 等价：整点起点的自定义窗口 = 小时桶之和，与改动前逐行口径一致")
# 测试数据的价是二进制精确值，所以整点窗口也能逐字节相同。
for name, lo, hi in (("昨天 12:00 起（旗舰）", NOON1, None),
                     ("昨天 12:00 ~ 现在", NOON1, FROZEN),
                     ("今天 00:00 ~ 现在（整点 + 带 until）", TODAY0, FROZEN),
                     ("前天 12:00 起", DAY2 + 12 * 3600, None)):
    cache_env_off()
    restart()
    ref = canon(win(lo, hi))
    cache_env_nowrite()
    restart()
    got = canon(win(lo, hi))
    check("窗口「%s」：小时层 == 逐行口径（整包逐字节）" % name, ref == got,
          first_diff(ref, got))
# realm 过滤：同一窗口换 realm 也要一致。
cache_env_off()
restart()
ref = canon(win(NOON1, None, realm="cn"))
cache_env_nowrite()
restart()
check("窗口「昨天 12:00 起」realm=cn：小时层 == 逐行口径",
      canon(win(NOON1, None, realm="cn")) == ref,
      first_diff(ref, canon(win(NOON1, None, realm="cn"))))
# 空窗口（起点是整点、落在最后一行之后）：层给空，与逐行口径一致。
cache_env_off()
restart()
ref = canon(win(TODAY0 + 20 * 3600, None))
cache_env_nowrite()
restart()
check("空窗口（起点在最后一行之后的整点）：层 == 逐行口径",
      canon(win(TODAY0 + 20 * 3600, None)) == ref,
      first_diff(ref, canon(win(TODAY0 + 20 * 3600, None))))
# 起点是本地午夜（整点）但带 until：日桶那条路被 until 挡住，走小时层。
cache_env_off()
restart()
ref = canon(win(TODAY0, FROZEN))
cache_env_nowrite()
restart()
got = canon(win(TODAY0, FROZEN))
check("午夜起点 + until：小时层 == 逐行口径（逐字节）", ref == got,
      first_diff(ref, got))

print()
print("[3] 逐字节：同一窗口「走层」vs「强制整段扫（现折小时桶）」+ 计数桩")
cache_env_nowrite()
restart()
pricing_on = P.wb_pricing.pricing_enabled()
# 先折一次（走层），再从状态里现折一份小时桶，两份必须逐字节一致。
_, calls_cold = cost_calls(lambda: win(NOON1, None))
layer = U._analytics_window_from_hours(None, NOON1, None, pricing_on)
cold = U._scan_usage_analytics_window_hours(None, NOON1, None)
check("走层 vs 现折小时桶：窗口半 maps 逐字节一致",
      canon(layer) == canon(cold), first_diff(canon(layer), canon(cold)))
check("冷折确实付了计价（计数桩自检）", calls_cold > 0, calls_cold)
# 层已经热了：再来一次窗口回答，一行都不该重折（计数桩归零）。
_, calls_warm = cost_calls(lambda: win(NOON1, None))
check("走层：窗口回答不再付计价（计数桩 == 0）", calls_warm == 0, calls_warm)
# 强制整段扫（逐行口径）确实付了计价——省下的工作真实存在。
_, calls_scan = cost_calls(lambda: U._scan_usage_analytics_window(None, NOON1, None))
check("逐行口径的整段扫确实付了计价（计数桩 > 0）", calls_scan > 0, calls_scan)
# 窗口内 9 行里有 1 行是客户端中止（折叠在算价之前就丢掉它），其余 8 行各付一次。
check("逐行口径的计价次数 == 窗口内非中止行数（8）", calls_scan == 8, calls_scan)
# 午夜窗口：小时层与日桶层给出同一份窗口半（两条对齐路径互相印证）。
restart()
by_hours = U._analytics_window_from_hours(None, TODAY0, None, pricing_on)
by_days = U._analytics_window_from_days(None, TODAY_KEY, None, pricing_on)
check("午夜窗口：小时层与日桶层一致（逐字节）",
      canon(by_hours) == canon(by_days), first_diff(canon(by_days), canon(by_hours)))

print()
print("[4] 覆盖不到：一律退回扫描，结果与改动前逐行口径一致")
# 4.1 起点不是整点：不进小时层（计数桩证明它真的扫了）。
cache_env_off()
restart()
ref = canon(win(NOON30, None))
cache_env_nowrite()
restart()
_, calls = cost_calls(lambda: win(NOON30, None))
got = canon(win(NOON30, None))
check("非整点起点（12:30）：退回扫描、结果一致", got == ref, first_diff(ref, got))
check("非整点起点：确实走了整段扫描（计数桩 > 0）", calls > 0, calls)
# 4.2 until 切进折过的数据里（早于折过的最大 at）：落小时格点、那一刻没有
#     整秒行时由小时层按「at < until 的桶」回答（闭窗口）；不落格点（切在
#     桶中间）时退回扫描。两种情形结果都必须与逐行口径一致。
cache_env_off()
restart()
ref = canon(win(NOON1, NOON1 + 7200))
cache_env_nowrite()
restart()
got = canon(win(NOON1, NOON1 + 7200))
check("until 切进数据（落小时格点）：结果与逐行口径一致",
      got == ref, first_diff(ref, got))
check("until 切进数据（落小时格点）：小时层按闭窗口回答（计数桩 == 0）",
      cost_calls(lambda: win(NOON1, NOON1 + 7200))[1] == 0)
cache_env_off()
restart()
ref_mid = canon(win(NOON1, NOON1 + 7200 + 1800))
cache_env_nowrite()
restart()
got_mid = canon(win(NOON1, NOON1 + 7200 + 1800))
check("until 切在桶中间（不落格点）：退回扫描、结果一致",
      got_mid == ref_mid, first_diff(ref_mid, got_mid))
check("until 切在桶中间：走的是现折小时桶那条冷路径（计数桩 > 0）",
      cost_calls(lambda: win(NOON1, NOON1 + 7200 + 1800))[1] > 0)
# 4.3 小时子表被剪掉的天：该天的窗口退回扫描、结果一致。
cache_env(enabled=1, min_bytes=10 ** 9, min_seconds=10 ** 9, hours=1)
restart()
live()
days = U._analytics_state[None]["days"]
check("保留 1 小时：更早的天丢了 hours 子表、日桶还在",
      "hours" not in days[DAY2_KEY] and "hours" in days[TODAY_KEY]
      and "hours" in days[DAY1_KEY],
      {k: ("hours" in b) for k, b in sorted(days.items())})
cache_env_off()
restart()
ref = canon(win(DAY2 + 12 * 3600, None))
cache_env(enabled=1, min_bytes=10 ** 9, min_seconds=10 ** 9, hours=1)
restart()
got = canon(win(DAY2 + 12 * 3600, None))
check("剪掉小时的天：窗口退回扫描、结果一致", got == ref, first_diff(ref, got))
# 4.4 days_floor 以下：整条窗口退回扫描、结果一致。
cache_env(enabled=1, min_bytes=10 ** 9, min_seconds=10 ** 9, keep=1)
restart()
live()
lo = DAY2 + 12 * 3600
cache_env_off()
restart()
ref = canon(win(lo, None))
cache_env(enabled=1, min_bytes=10 ** 9, min_seconds=10 ** 9, keep=1)
restart()
got = canon(win(lo, None))
check("days_floor 以下：退回扫描、结果一致", got == ref, first_diff(ref, got))
cache_env(keep=None)
# 4.5 位置校验失效（日志被换/截断）：状态整个重折，窗口结果仍与基线一致。
cache_env_nowrite()
restart()
live()
U._analytics_state[None]["key"] = (0, 0, 0)
got = canon(win(NOON1, None))
check("状态的位置校验失效：重折后窗口仍与基线一致",
      got == canon(GOLDEN_RAW["an_noon1"]),
      first_diff(canon(GOLDEN_RAW["an_noon1"]), got))

print()
print("[5] checkpoint：小时子表与 max_at 随状态落盘/加载，重启后逐字节一致")
check("写盘成功", make_checkpoint())
stored = cache_data()
COLD_DAYS = canon(U._analytics_state[None]["days"])
COLD_MAX_AT = U._analytics_state[None]["max_at"]
check("每个 kind 文件都是当前 schema 的 JSON 对象", all(json.load(io.open(KIND_FILES[k], encoding="utf-8"))["schema"] == U._USAGE_CACHE_SCHEMA for k in U._USAGE_CACHE_KINDS if os.path.exists(KIND_FILES[k])), cache_files())
entry = an_entry(stored, None)
check("analytics 条目带 max_at（数字）",
      isinstance(entry.get("max_at"), (int, float))
      and not isinstance(entry.get("max_at"), bool),
      entry.get("max_at"))
check("max_at 与折叠状态一致（不排序的 JSON 往返）",
      entry["max_at"] == COLD_MAX_AT,
      (entry.get("max_at"), COLD_MAX_AT))
h_key, h_bucket = newest_hour(stored)
check("落盘的日桶带 at 与 hours（小时键是数字字符串）",
      isinstance(newest_day(stored)["at"], (int, float))
      and h_key.isdigit() and isinstance(h_bucket.get("at"), (int, float)),
      (h_key, h_bucket.get("at")))
check("落盘的小时桶是窗口形状（window / window_models，没有 all_time）",
      all("window" in e and "all_time" not in e
          for e in h_bucket["models"].values()),
      list(h_bucket["models"].values())[:1])
check("落盘的小时桶键序是折叠插入顺序（不是字母序）",
      list(h_bucket.keys())[:4] == ["summary", "accts", "models", "keys"],
      list(h_bucket.keys())[:6])
# 冷折出来的日桶（含小时子表、at、键序）与落盘的一份逐字节一致。
check("冷折状态里的日桶 == 落盘的日桶（逐字节，含 at/hours 键序）",
      canon(entry["days"]) == COLD_DAYS,
      first_diff(COLD_DAYS, canon(entry["days"])))
cache_env_nowrite()
restart()
LOADED_RAW = live()
ADOPTED = dict(U._usage_cache_checkpointed)
check("重启后加载到了 checkpoint（含 analytics）", "analytics" in ADOPTED, ADOPTED)
check("加载后的日桶 == 冷折的日桶（逐字节，含 at/hours 键序）",
      canon(U._analytics_state[None]["days"]) == COLD_DAYS,
      first_diff(COLD_DAYS, canon(U._analytics_state[None]["days"])))
check("加载后所有窗口与冷折逐字节一致", canon(LOADED_RAW) == GOLDEN,
      first_diff(GOLDEN, canon(LOADED_RAW)))
# 重启后走层的窗口回答不该重折（小时子表是从 checkpoint 来的）。
_, calls = cost_calls(lambda: win(NOON1, None))
check("重启加载后：窗口回答仍不付计价（计数桩 == 0）", calls == 0, calls)

print()
print("[6] 真·新进程：加载 checkpoint 后窗口一致，且确实走了加载路径")
child_raw, child_state = run_child()
check("新进程结果与冷折一致", canon(child_raw) == GOLDEN,
      first_diff(GOLDEN, canon(child_raw)))
check("新进程记录到了加载的 offset（含 analytics）",
      "analytics" in child_state, child_state)

print()
print("[7] 降级：坏小时子表/旧格式一律当作没有缓存（另有用例钉旧天的小时表缺席不算损坏）")


def poison(data, realm=None):
    """给「最新那天最新那个小时桶」加一个一眼可见的标记。

    标记落在今天最新那个小时桶：旗舰窗口（昨天 12:00 起）包含它，只要这份
    状态被采用、且小时层真的用了它，结果里就会多出 1000 次请求。
    """
    newest_hour(data, realm)[1]["summary"]["requests"] += 1000


def mutate_hour(data, mutate_fn, realm=None):
    """对「最新那天最新那个小时桶」做变异。"""
    hours = newest_day(data, realm)["hours"]
    key = sorted(hours, key=int)[-1]
    mutate_fn(hours, key)


def mutate(mutate_fn, label, realm=None):
    """先给该 entry 加标记，再变异，然后从它加载——结果必须回到真值。"""
    make_checkpoint()
    data = cache_data()
    poison(data, realm)
    mutate_fn(data)
    save_cache(data)
    cache_env_nowrite()
    restart()
    got = canon(live())
    check(label, got == GOLDEN, first_diff(GOLDEN, got))


# 7.0 自检：形状合法、值被改过的小时桶会被采用（标记必须可见），否则下面的
# 用例可能什么都没测到。
make_checkpoint()
data = cache_data()
poison(data)
save_cache(data)
cache_env_nowrite()
restart()
check("标记自检：形状合法的坏小时桶会被采用（标记出现在结果里）",
      canon(live()) != GOLDEN, "结果没有变化")

mutate(lambda d: an_entry(d).pop("max_at"), "entry 没有 max_at（旧格式）")
mutate(lambda d: an_entry(d).update({"max_at": "now"}), "max_at 不是数字")
mutate(lambda d: an_entry(d).update({"max_at": True}), "max_at 写成 true")
mutate(lambda d: newest_day(d).pop("at"), "日桶没有 at（旧格式）")
mutate(lambda d: newest_day(d).update({"at": "midnight"}), "日桶的 at 不是数字")
# 注意：这里**没有**「日桶没有 hours」那条用例。剪枝线摘掉旧天的小时表是被认可的
# 常态（#26），采纳覆盖钉在 _test_usage_aggregate_cache.py（标记落在 all-time
# 的 maps 上，一定可见，不受夹具/时区影响）。
mutate(lambda d: newest_day(d).update({"hours": []}), "hours 不是对象")
mutate(lambda d: mutate_hour(d, lambda h, k: h.update({"x": {}})),
       "小时键不是数字")
mutate(lambda d: mutate_hour(d, lambda h, k: h.__setitem__(k, {})),
       "小时桶形状不对（空对象）")
mutate(lambda d: mutate_hour(d, lambda h, k: h.__setitem__(k, dict(h[k], at="x"))),
       "小时桶的 at 不是数字")
mutate(lambda d: mutate_hour(d, lambda h, k: h.__setitem__(k, dict(h[k], hours={}))),
       "小时桶里又套了一层 hours")
mutate(lambda d: mutate_hour(
    d, lambda h, k: h.__setitem__(k, dict(h[k], summary={"requests": 1}))),
    "小时桶的 summary 形状不对")
mutate(lambda d: mutate_hour(
    d, lambda h, k: h.__setitem__(
        k, dict(h[k], summary=dict(h[k]["summary"], requests=None)))),
    "小时桶的字段类型不对")
# 形状合法但格点对不上：会被采用，查询侧按格点校验放弃、退回扫描，结果仍正确。
mutate(lambda d: mutate_hour(
    d, lambda h, k: h.__setitem__(k, dict(h[k], at=int(h[k]["at"]) + 60))),
    "小时桶的 at 偏离格点：被采用但查询侧退回扫描，结果仍正确")
mutate(lambda d: mutate_hour(
    d, lambda h, k: h.__setitem__("25", dict(h[k], at=int(h[k]["at"]) + 3600))),
    "小时键与 at 对不上（at != 日桶 at + 小时*3600）：退回扫描，结果仍正确")

print()
print("[8] 浮点口径：价不是二进制精确值时，层与现折小时桶仍逐字节一致")
clean_checkpoint()
write_pricing({**PRICING, "models": {
    **PRICING["models"],
    "m-a": {"unit": 3, "currency": "CNY",
            "flat": {"input_cache_hit": 0.1, "input_cache_miss": 0.3,
                     "output": 0.7}},
    "m-b": {"unit": 7, "currency": "CNY",
            "flat": {"input_cache_hit": 0.13, "input_cache_miss": 0.29,
                     "output": 0.71}},
}})
cache_env_nowrite()
restart()
got = win(NOON1, None)
layer = U._analytics_window_from_hours(None, NOON1, None,
                                       P.wb_pricing.pricing_enabled())
cold = U._scan_usage_analytics_window_hours(None, NOON1, None)
check("层与现折小时桶逐字节一致（非精确价）",
      canon(layer) == canon(cold), first_diff(canon(cold), canon(layer)))
cache_env_off()
restart()
ref = win(NOON1, None)
diffs = float_diffs(ref, got, tol=1e-9)
check("层 vs 逐行口径：结构与整数逐字段相同，浮点差异 <=1e-9 相对误差",
      not diffs, diffs[:6])
check("两种口径算的是同一批行（requests 相同）",
      ref["summary"]["window"]["requests"] == got["summary"]["window"]["requests"],
      (ref["summary"]["window"]["requests"],
       got["summary"]["window"]["requests"]))
write_pricing(PRICING)

print()
print("[9] 开关：WB_USAGE_DAY_BUCKETS=0 时与改动前逐字节一致")
clean_checkpoint()
cache_env(enabled=0, min_bytes=0, min_seconds=0, days=0)
restart()
live()
check("关掉日桶/小时桶：状态里一个日桶、一个小时子表都没有",
      not U._analytics_state[None]["days"],
      U._analytics_state[None]["days"])
check("关掉日桶/小时桶：结果与基线一致", canon(live()) == GOLDEN,
      first_diff(GOLDEN, canon(live())))
# 打开小时层但 checkpoint 关掉（WB_USAGE_CACHE=0）：小时层仍然工作、结果与
# 基线一致——两个开关互不依赖。
cache_env(enabled=0, min_bytes=0, min_seconds=0, days=None)
restart()
check("checkpoint 关掉但小时层开着：结果与基线一致", canon(live()) == GOLDEN)
check("checkpoint 关掉但小时层开着：小时子表确实折了",
      bool(U._analytics_state[None]["days"])
      and any(b.get("hours") for b in U._analytics_state[None]["days"].values()))
cache_env()

print()
print("[10] 格点被挪动（夏令时/日边界不规则）：层自己退回去")
clean_checkpoint()
cache_env_nowrite()
restart()
live()
pricing_on = P.wb_pricing.pricing_enabled()
state = U._analytics_state[None]
check("对齐窗口本来就走小时层（不是退回）",
      U._analytics_window_from_hours(None, NOON1, None, pricing_on) is not None)
day_at = state["days"][TODAY_KEY]["at"]
state["days"][TODAY_KEY]["at"] = day_at + 3600   # 假装这一天少了/多了 3600 秒
check("日桶 at 被挪动（小时桶与日桶对不上）：小时层放弃、退回扫描",
      U._analytics_window_from_hours(None, NOON1, None, pricing_on) is None)
state["days"][TODAY_KEY]["at"] = day_at
hours = state["days"][DAY1_KEY]["hours"]
h_key = sorted(hours, key=int)[0]
h_at = hours[h_key]["at"]
hours[h_key]["at"] = h_at + 60
check("小时桶 at 偏离格点：小时层放弃、退回扫描",
      U._analytics_window_from_hours(None, NOON1, None, pricing_on) is None)
hours[h_key]["at"] = h_at
check("恢复后小时层又能用",
      U._analytics_window_from_hours(None, NOON1, None, pricing_on) is not None)
# 格点被挪动时窗口结果仍然正确（退回扫描兜住）。
cache_env_off()
restart()
ref = canon(win(NOON1, None))
cache_env_nowrite()
restart()
live()
U._analytics_state[None]["days"][TODAY_KEY]["at"] += 3600
got = canon(win(NOON1, None))
check("日桶 at 被挪动时窗口仍与逐行口径一致（退回扫描兜住）",
      got == ref, first_diff(ref, got))
clean_checkpoint()

print()
print("[11] 夏令时时区（真实 TZ）：整点窗口与日桶/逐行口径一致")
clean_checkpoint()
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
                  % (", ".join(ZONES)))
        else:
            print("  [INFO] 夏令时用例使用 TZ=%s" % chosen)
            # 冻结时钟挪到切换日之后（10-28 12:00），造一批横跨 10-25（25
            # 小时那天）的整点行；窗口起点取 10-25 01:00（切换发生在当天
            # 03:00，窗口因此横跨它），落在小时子表的保留线以内（默认 48h，
            # 最新那天是 10-27）。
            _time.time = lambda: time.mktime((2026, 10, 28, 12, 0, 0, 0, 0, -1))
            base = time.mktime((2026, 10, 22, 12, 0, 0, 0, 0, -1))
            dst_rows = [row(base + d * 86400 + h * 3600, "m-a", "u1", 100, 10)
                        for d in range(5) for h in (0, 6, 12, 18)]
            write_log(dst_rows)
            lo = time.mktime((2026, 10, 25, 1, 0, 0, 0, 0, -1))
            cache_env_off()
            restart()
            ref = canon(U._compute_usage_analytics_uncached(since=lo, until=None))
            cache_env_nowrite()
            restart()
            got = canon(U._compute_usage_analytics_uncached(since=lo, until=None))
            check("夏令时时区：横跨切换日的整点自定义窗口 == 逐行口径（逐字节）",
                  got == ref, first_diff(ref, got))
            cache_env_nowrite()
            restart()
            U._compute_usage_analytics_uncached(since=lo, until=None)
            pricing_on = P.wb_pricing.pricing_enabled()
            layer = U._analytics_window_from_hours(None, lo, None, pricing_on)
            cold = U._scan_usage_analytics_window_hours(None, lo, None)
            check("夏令时时区：层确实接住了这个窗口（没有退回扫描）",
                  layer is not None)
            check("夏令时时区：层 == 现折小时桶（逐字节）",
                  layer is not None and canon(layer) == canon(cold),
                  first_diff(canon(cold), canon(layer) if layer else {}))
            # 起点是切换日当天的本地午夜（25 小时那天）：日桶那条路（until
            # 开放）照常可用，与小时层给出同一份窗口半。
            lo2 = time.mktime((2026, 10, 25, 0, 0, 0, 0, 0, -1))
            key2 = time.strftime("%Y-%m-%d", time.localtime(lo2))
            by_hours = U._analytics_window_from_hours(None, lo2, None, pricing_on)
            by_days = U._analytics_window_from_days(None, key2, None, pricing_on)
            check("夏令时切换日：小时层与日桶层一致（逐字节）",
                  by_hours is not None and by_days is not None
                  and canon(by_hours) == canon(by_days),
                  first_diff(canon(by_days) if by_days else {},
                             canon(by_hours) if by_hours else {}))
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

shutil.rmtree(_TMP, ignore_errors=True)
print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
