"""T14：冷状态的窗口请求不付全量重折（改走既有的逐行冷扫描）。

现象（第二批清单 §T14）：价表**内容**变化（指纹是内容摘要，不是 mtime）会把
snapshot×3 + analytics×2 共 5 条状态判废，首个窗口请求要付一次全量重折——
真机实测 15.1s（预热器 ≤2 分钟才吸收，撞上就卡）。
修法：窗口请求发现状态冷（从没折过 / 采纳失败 / 刚被价表、realm、日志判废）
时不折状态，直接走既有的按日/按小时分组的冷扫描
（_scan_usage_snapshot_window_days/_hours、_scan_usage_analytics_window_*，
~1s 量级）；全量重折留给 all-time 视图（_usage_alltime_snapshot、analytics 的
all-time 半）与每 2 分钟一轮的预热器。

本套件钉住（T14 验收）：
  [1] 冷态窗口请求不折状态：计数桩数出的折行数 == 窗口内的行数（不是全量），
      状态 offset 仍是 0、日桶仍是空的；
  [2] 重折仍会发生：随后的 all-time 请求从零折到文件尾（计数桩 == 全量行数），
      状态转热，结果与「关掉日桶的逐行口径」逐字节一致；
  [3] 逐字节对拍：同一窗口「冷态走扫描」==「热态走桶」（日桶窗口、整点窗口、
      闭窗口各一条）；
  [4] 价表内容变化（T14 的真实触发）：判废后窗口请求不折，all-time 重折照发生；
  [5] analytics：窗口半同样先扫描；all-time 半照折（它本来就是载荷的一半）；
  [6] 与 T12 闭窗口闸门共存：闭窗口冷态扫描、热态走桶（T12 树），两者输出
      逐字节一致；闭窗口的冷态请求同样不折状态；
  [7] checkpoint 采纳不受影响：冷启动但 checkpoint 可用时窗口请求照旧采纳
      （计数桩 == 0，一行都不折）；
  [8] series 不在价表失效范围内（复核件 §2）：价表变化不动它，窗口行为不变。

计数桩的写法照 tests/_test_usage_agg_hotspots.py：把 _fold_usage_snapshot /
_fold_usage_analytics 换成计数器，断言「某些工作没做」。测试数据的价刻意用
二进制可精确求和的数，所以「按日/按小时分组再相加」与「逐行相加」逐字节
相同（见 _test_usage_day_buckets.py 的说明）。

No network: the usage log and the pricing table are synthesised in a temp
directory, and the clock is frozen.
"""
import io
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# 与 _test_usage_day_buckets.py / _test_usage_hour_buckets.py 同一把冻结时钟：
# 窗口边界（本地午夜/整点）与快照的 started/since 都从 time.time() 派生，
# 不冻结就不可比。
FROZEN = 1791445200.0

_TMP = tempfile.mkdtemp(prefix="wb-coldwin-")
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
PRICING_FILE = os.path.join(_TMP, "pricing.json")
os.environ["WB_PRICING_FILE"] = PRICING_FILE
_MTIME = [FROZEN - 1000000.0]

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
    """与其它套件同一套对拍口径：键排序、不转义。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def first_diff(want, got):
    if want == got:
        return ""
    for i, (a, b) in enumerate(zip(want, got)):
        if a != b:
            return "@%d: %r vs %r" % (i, want[max(0, i - 30):i + 30],
                                      got[max(0, i - 30):i + 30])
    return "长度不同：%d vs %d" % (len(want), len(got))


# 二进制可精确求和的价（0.25 / 0.5 / 0.125 与 2 的幂分母）：日/小时分组再相加
# 与逐行相加逐字节相同，对拍才能用 ==（同 _test_usage_day_buckets.py）。
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
# 换价（内容变、结构不变）：指纹是内容摘要，这一次改动必须判废那 5 条状态。
PRICING_B = {
    "meta": {"usd_cny": 7.4},
    "models": {
        "m-a": {"unit": 1, "currency": "CNY",
                "flat": {"input_cache_hit": 0.5, "input_cache_miss": 1.0,
                         "output": 0.25}},
        "m-b": {"unit": 1, "currency": "CNY",
                "flat": {"input_cache_hit": 0.25, "input_cache_miss": 0.5,
                         "output": 0.125}},
    },
}


def write_pricing(doc):
    io.open(PRICING_FILE, "w", encoding="utf-8").write(
        json.dumps(doc, ensure_ascii=False))
    # load_pricing() 按 (path, mtime) 缓存：两次写入必须落在不同的 mtime 上，
    # 否则文件内容变了、指纹还是旧的（同 _test_usage_hour_buckets.py）。
    _MTIME[0] += 3600.0
    os.utime(PRICING_FILE, (_MTIME[0], _MTIME[0]))


def row(at, model, account, prompt, completion, realm="intl", credit=0.0):
    return {
        "at": at, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at)),
        "model": model, "stream": True, "outcome": "completed",
        "elapsed_ms": 1200, "ttft_ms": 400, "gen_ms": 800,
        "prompt_tokens": prompt, "completion_tokens": completion,
        "reasoning_tokens": 0, "cached_tokens": 0,
        "total_tokens": prompt + completion, "credit": credit,
        "account": account, "realm": realm, "tokens_per_sec": 120.0,
        "cache_hit_pct": 0.0,
    }


def write_log(rows):
    with io.open(USAGE_LOG, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def cache_env(enabled=None, min_bytes=None, min_seconds=None, days=None):
    for name, value in (("WB_USAGE_CACHE", enabled),
                        ("WB_USAGE_CACHE_MIN_BYTES", min_bytes),
                        ("WB_USAGE_CACHE_MIN_SECONDS", min_seconds),
                        ("WB_USAGE_DAY_BUCKETS", days)):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = str(value)


def restart():
    """清掉所有状态与 TTL 缓存，模拟进程重启（checkpoint 文件不动）。"""
    U._usage_snap_state.clear()
    U._byacct_state.update({"buckets": None, "offset": 0, "key": None,
                            "tail": b""})
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


def snap_cold():
    """snapshot 状态「冷」的跨树判据：状态不存在（闭窗口不惊动状态）、或存在
    但没有可续折的数据（offset == 0 且没有日桶）。"""
    st = U._usage_snap_state.get(None)
    return st is None or (st["offset"] == 0 and not st["days"])


class FoldCounter(object):
    """把 P.<name> 换成计数器：calls == 折叠被调用的次数（= 折进去的行数）。"""

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.real = getattr(U, self.name)
        self.calls = 0

        def counting(*args, **kwargs):
            self.calls += 1
            return self.real(*args, **kwargs)

        setattr(U, self.name, counting)
        return self

    def __exit__(self, *exc):
        setattr(U, self.name, self.real)
        return False


# --------------------------------------------------------------------------
# 数据：最近四天各 9 行（午夜整点 1 行 + 每小时 1 行），今天 9 行
# --------------------------------------------------------------------------
DAYS = [U._local_midnight(FROZEN - back * 86400) for back in (3, 2, 1, 0)]
TODAY0 = DAYS[-1]
HOUR4 = TODAY0 + 4 * 3600
HOUR6 = TODAY0 + 6 * 3600

ROWS = []
for idx, day_at in enumerate(DAYS):
    # 午夜整点那行（边界行：既在「今天」窗口里，也在日桶边界上）
    ROWS.append(row(day_at, "m-a", "u1", 100, 10, credit=0.25))
    for h in range(1, 9):
        ROWS.append(row(day_at + h * 3600,
                        "m-b" if h % 3 == 0 else "m-a",
                        "u2" if h % 2 == 0 else "u1",
                        100 * h, 10 * h,
                        realm="cn" if (idx < 2 and h % 2 == 0) else "intl",
                        credit=0.5 * h))
TOTAL_ROWS = len(ROWS)          # 4 天 × 9 行 = 36
TODAY_ROWS = 9                  # 今天那 9 行
HOUR4_ROWS = 5                  # since=04:00 起的 5 行（h=4..8，含边界行）
CLOSED_ROWS = 3                 # 04:00-06:00 的 3 行（含两端边界行）

write_pricing(PRICING)
write_log(ROWS)

print("[0] 基线：关掉日桶的逐行口径（对照值），以及「状态是冷的」长什么样")
cache_env(enabled=0, min_bytes=0, min_seconds=0, days=0)
restart()
REF_TODAY = canon(U._usage_snapshot_uncached(None, since=TODAY0, until=None))
REF_HOUR4 = canon(U._usage_snapshot_uncached(None, since=HOUR4, until=None))
REF_CLOSED = canon(U._usage_snapshot_uncached(None, since=HOUR4, until=HOUR6))
REF_ALL = canon(U._usage_snapshot_uncached())
REF_AN_TODAY = canon(U._compute_usage_analytics_uncached(since=TODAY0,
                                                         until=None))
REF_AN_ALL = canon(U._compute_usage_analytics_uncached())
check("基线：今天窗口有内容，闭窗口是它的子集",
      json.loads(REF_TODAY)["requests"] == TODAY_ROWS
      and json.loads(REF_CLOSED)["requests"] == CLOSED_ROWS
      and json.loads(REF_ALL)["requests"] == TOTAL_ROWS,
      (json.loads(REF_TODAY)["requests"], json.loads(REF_CLOSED)["requests"],
       json.loads(REF_ALL)["requests"]))
check("基线（关掉日桶）：状态里一个日桶都没有",
      not (U._usage_snap_state.get(None) or {}).get("days")
      and not (U._analytics_state.get(None) or {}).get("days"),
      (U._usage_snap_state.get(None), U._analytics_state.get(None)))

print()
print("[1] 冷态窗口请求走扫描、不折状态（T14 的主修法）")
cache_env(enabled=0, min_bytes=0, min_seconds=0)
restart()
with FoldCounter("_fold_usage_snapshot") as c:
    got_today = U._usage_snapshot_uncached(None, since=TODAY0, until=None)
check("冷态今天窗口：折的行数 == 窗口内的行数（不是全量）",
      c.calls == TODAY_ROWS and c.calls < TOTAL_ROWS,
      (c.calls, TODAY_ROWS, TOTAL_ROWS))
check("冷态今天窗口：结果与逐行口径逐字节一致",
      canon(got_today) == REF_TODAY, first_diff(REF_TODAY, canon(got_today)))
state = U._usage_snap_state[None]
check("冷态今天窗口：状态一行都没折（offset 仍是 0、日桶仍是空的）",
      state["offset"] == 0 and not state["days"],
      (state["offset"], state["days"]))
with FoldCounter("_fold_usage_snapshot") as c:
    again = U._usage_snapshot_uncached(None, since=TODAY0, until=None)
check("再来一次窗口请求：还是不折（状态没有被冻成「折一半」）",
      c.calls == TODAY_ROWS and canon(again) == REF_TODAY,
      (c.calls, canon(again) == REF_TODAY))
with FoldCounter("_fold_usage_snapshot") as c:
    hour_cold = U._usage_snapshot_uncached(None, since=HOUR4, until=None)
check("冷态整点窗口：折的行数 == 窗口内的行数",
      c.calls == HOUR4_ROWS, (c.calls, HOUR4_ROWS))
check("冷态整点窗口：结果与逐行口径逐字节一致",
      canon(hour_cold) == REF_HOUR4, first_diff(REF_HOUR4, canon(hour_cold)))

print()
print("[2] 重折仍会发生：all-time 请求从零折到文件尾，状态转热")
with FoldCounter("_fold_usage_snapshot") as c:
    all_snap = U._usage_snapshot_uncached()
check("all-time 请求：折的行数 == 全量行数（重折确实发生了）",
      c.calls == TOTAL_ROWS, (c.calls, TOTAL_ROWS))
check("all-time 请求：结果与逐行口径逐字节一致",
      canon(all_snap) == REF_ALL, first_diff(REF_ALL, canon(all_snap)))
state = U._usage_snap_state[None]
check("all-time 之后状态转热（offset 到文件尾、日桶折出来了）",
      state["offset"] > 0 and len(state["days"]) == len(DAYS),
      (state["offset"], sorted(state["days"])))
with FoldCounter("_fold_usage_snapshot") as c:
    hot_today = U._usage_snapshot_uncached(None, since=TODAY0, until=None)
check("热态今天窗口：一行都不折（由存量日桶求和回答）", c.calls == 0, c.calls)
check("冷态扫描 == 热态桶求和（逐字节）",
      canon(hot_today) == canon(got_today),
      first_diff(canon(got_today), canon(hot_today)))
with FoldCounter("_fold_usage_snapshot") as c:
    hot_hour = U._usage_snapshot_uncached(None, since=HOUR4, until=None)
check("热态整点窗口：一行都不折（由存量小时桶求和回答）", c.calls == 0, c.calls)
check("整点窗口：冷态扫描 == 热态桶求和（逐字节）",
      canon(hot_hour) == canon(hour_cold),
      first_diff(canon(hour_cold), canon(hot_hour)))

print()
print("[3] 价表内容变化（T14 的真实触发）：窗口不折，all-time 重折")
write_pricing(PRICING_B)
cache_env(enabled=0, min_bytes=0, min_seconds=0, days=0)
restart()
REF_TODAY_B = canon(U._usage_snapshot_uncached(None, since=TODAY0, until=None))
REF_ALL_B = canon(U._usage_snapshot_uncached())
check("换了价表内容：真值确实变了（对照自检）",
      REF_TODAY_B != REF_TODAY and REF_ALL_B != REF_ALL)
write_pricing(PRICING_B)      # 再把 mtime 推一格，保证新内容被读到
cache_env(enabled=0, min_bytes=0, min_seconds=0)
restart()
with FoldCounter("_fold_usage_snapshot") as c:
    got_today_b = U._usage_snapshot_uncached(None, since=TODAY0, until=None)
check("价表判废后：窗口请求只折窗口内的行（不付全量重折）",
      c.calls == TODAY_ROWS and c.calls < TOTAL_ROWS,
      (c.calls, TODAY_ROWS, TOTAL_ROWS))
check("价表判废后：窗口结果按新价算，与逐行口径逐字节一致",
      canon(got_today_b) == REF_TODAY_B,
      first_diff(REF_TODAY_B, canon(got_today_b)))
state = U._usage_snap_state[None]
check("价表判废后：窗口请求之后状态仍是冷的（重折没被窗口偷做掉）",
      state["offset"] == 0 and not state["days"],
      (state["offset"], state["days"]))
with FoldCounter("_fold_usage_snapshot") as c:
    all_b = U._usage_snapshot_uncached()
check("价表判废后的 all-time 请求：从零折到文件尾（全量行数）",
      c.calls == TOTAL_ROWS, (c.calls, TOTAL_ROWS))
check("价表判废后的 all-time 结果与逐行口径逐字节一致",
      canon(all_b) == REF_ALL_B, first_diff(REF_ALL_B, canon(all_b)))
write_pricing(PRICING)
cache_env(enabled=0, min_bytes=0, min_seconds=0, days=0)
restart()
REF_TODAY = canon(U._usage_snapshot_uncached(None, since=TODAY0, until=None))
REF_HOUR4 = canon(U._usage_snapshot_uncached(None, since=HOUR4, until=None))
REF_CLOSED = canon(U._usage_snapshot_uncached(None, since=HOUR4, until=HOUR6))
REF_ALL = canon(U._usage_snapshot_uncached())

print()
print("[4] analytics：窗口半先扫描，all-time 半照折（它本来就是载荷的一半）")
cache_env(enabled=0, min_bytes=0, min_seconds=0)
restart()
pricing_on = P.wb_pricing.pricing_enabled()
with FoldCounter("_fold_usage_analytics") as c:
    an_cold = U._analytics_window(None, TODAY0, None, None, pricing_on)
check("冷态 analytics 窗口半：折的行数 == 窗口内的行数",
      c.calls == TODAY_ROWS and c.calls < TOTAL_ROWS,
      (c.calls, TODAY_ROWS, TOTAL_ROWS))
check("冷态 analytics 窗口半：状态一行都没折",
      U._analytics_state[None]["offset"] == 0
      and not U._analytics_state[None]["days"],
      (U._analytics_state[None]["offset"], U._analytics_state[None]["days"]))
with FoldCounter("_fold_usage_analytics") as c:
    an_payload = U._compute_usage_analytics_uncached(since=TODAY0, until=None)
check("analytics 端点：all-time 半照折（全量行数），窗口半由桶求和",
      c.calls == TOTAL_ROWS, (c.calls, TOTAL_ROWS))
check("analytics 端点：整包与逐行口径逐字节一致",
      canon(an_payload) == REF_AN_TODAY,
      first_diff(REF_AN_TODAY, canon(an_payload)))
with FoldCounter("_fold_usage_analytics") as c:
    an_hot = U._analytics_window(None, TODAY0, None,
                                 U._analytics_state[None]["offset"], pricing_on)
check("热态 analytics 窗口半：一行都不折", c.calls == 0, c.calls)
check("analytics 窗口半：冷态扫描 == 热态桶求和（逐字节）",
      canon(an_hot) == canon(an_cold),
      first_diff(canon(an_cold), canon(an_hot)))

print()
print("[5] 与 T12 闭窗口闸门共存：闭窗口冷态扫描、热态走桶，两者一致")
cache_env(enabled=0, min_bytes=0, min_seconds=0)
restart()
with FoldCounter("_fold_usage_snapshot") as c:
    closed_cold = U._usage_snapshot_uncached(None, since=HOUR4, until=HOUR6)
check("冷态闭窗口：一行都没折（状态不因闭窗口请求被重折）",
      c.calls == CLOSED_ROWS and snap_cold(),
      (c.calls, CLOSED_ROWS, U._usage_snap_state.get(None)))
check("冷态闭窗口：结果与逐行口径逐字节一致",
      canon(closed_cold) == REF_CLOSED,
      first_diff(REF_CLOSED, canon(closed_cold)))
U._usage_snapshot_uncached()      # 折到文件尾（重折照发生）
with FoldCounter("_fold_usage_snapshot") as c:
    closed_hot = U._usage_snapshot_uncached(None, since=HOUR4, until=HOUR6)
check("热态闭窗口：最多只折窗口内的行（T12 树走桶求和 == 0，本树整段扫描"
      " == 窗口行数），绝不重折全量",
      c.calls in (0, CLOSED_ROWS), c.calls)
check("闭窗口：冷态扫描 == 热态（T12 走桶 / 本树走扫）逐字节一致",
      canon(closed_hot) == canon(closed_cold),
      first_diff(canon(closed_cold), canon(closed_hot)))
cache_env(enabled=0, min_bytes=0, min_seconds=0)
restart()
an_closed_cold = U._analytics_window(None, HOUR4, HOUR6, None, pricing_on)
U._compute_usage_analytics_uncached()
an_closed_hot = U._analytics_window(None, HOUR4, HOUR6,
                                    U._analytics_state[None]["offset"],
                                    pricing_on)
check("analytics 闭窗口：冷态扫描 == 热态（T12 走桶 / 本树走扫）逐字节一致",
      canon(an_closed_cold) == canon(an_closed_hot),
      first_diff(canon(an_closed_cold), canon(an_closed_hot)))

print()
print("[6] checkpoint 采纳不受影响：冷启动但 checkpoint 可用时窗口照旧采纳")
cache_env(enabled=1, min_bytes=0, min_seconds=0)
restart()
U._usage_snapshot_uncached()          # 折一次并落盘
restart()                             # 模拟进程重启（状态清空，checkpoint 留下）
with FoldCounter("_fold_usage_snapshot") as c:
    adopted = U._usage_snapshot_uncached(None, since=TODAY0, until=None)
check("冷启动 + 可用 checkpoint：窗口请求走采纳（一行都不折）",
      c.calls == 0, c.calls)
check("冷启动 + 可用 checkpoint：结果与逐行口径逐字节一致",
      canon(adopted) == REF_TODAY, first_diff(REF_TODAY, canon(adopted)))
check("冷启动 + 可用 checkpoint：状态确实是被采纳出来的（offset > 0）",
      U._usage_snap_state[None]["offset"] > 0,
      U._usage_snap_state[None]["offset"])
cache_env(enabled=0, min_bytes=0, min_seconds=0)

print()
print("[7] series 不在价表失效范围内（复核件 §2）：价表变化不动它")
cache_env(enabled=0, min_bytes=0, min_seconds=0, days=None)
restart()
ts = U._usage_timeseries_uncached(None, TODAY0, FROZEN, 3600)
REF_TS = canon(U._usage_timeseries_scan(None, TODAY0, FROZEN, 3600))
check("series 窗口：层切片与整段扫描逐字节一致",
      canon(ts) == REF_TS, first_diff(REF_TS, canon(ts)))
series_off = U._series_state[None]["offset"]
check("series 窗口：冷态请求把状态折起来了（它没有 all-time 重建入口，"
      "冷态改走扫描会让状态永不重建——所以 T14 不动它）",
      series_off > 0, series_off)
write_pricing(PRICING_B)
cache_env(enabled=0, min_bytes=0, min_seconds=0, days=None)
ts_b = U._usage_timeseries_uncached(None, TODAY0, FROZEN, 3600)
check("价表内容变化：series 状态照旧热（折叠不算价，不被指纹判废）",
      U._series_state[None]["offset"] == series_off,
      (U._series_state[None]["offset"], series_off))
check("价表内容变化：series 窗口结果逐字节不变（口径不受价表影响）",
      canon(ts_b) == REF_TS, first_diff(REF_TS, canon(ts_b)))
write_pricing(PRICING)

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
