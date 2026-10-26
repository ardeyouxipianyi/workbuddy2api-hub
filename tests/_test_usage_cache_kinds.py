"""checkpoint 一类一份（v4）：按 kind 拆分的落盘、采纳与只读迁移。

旧布局把五份状态装在一个文件里（usage-aggregate-cache.json，schema 3）：
加载端要解析整份文件才能拿到自己那一类，写盘时任何一类推进都要把五类全部
重新序列化——真机上 remaining 一份就比其余四份加起来还大，只跑 /usage 的
进程也要为它付解析成本；而且写盘会把「当时不活跃的类」写成空段，把盘上的
旧数据清掉（重启后那条路径上的类只能冷重折）。

v4 起一类状态一份文件（usage-aggregate-cache-<kind>.json），每份自带
schema/kind 标签与位置、指纹校验。这个套件钉住拆分的承诺：

  * 单视图进程只解析自己那一类：计数桩证明只打开、只解析了要用的文件
    （其余文件即使是乱码也不影响采纳）；
  * 混合新旧：只有一类推进过（它的文件新），其余类按各自文件里的旧
    offset 续读——「一份新一份旧也各自成立」；
  * 写侧只重写有推进的类；不活跃的类旧文件不被清掉、之后仍能采纳；
  * 一类文件坏掉只作废那一类，其余类照常采纳（五类逐一验证）；
  * 旧单文件（schema 3）只读兼容：还没有自己文件的类从旧文件取它那一节，
    升级后的第一次刷新不必整段重折；类文件一旦存在就只认类文件；
  * 写侧原子性（tmp + os.replace，不排序键）与「某一类写失败不拖累其余类」。

No network: the usage log, the pricing snapshot and the settings file are
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

# 与 _test_usage_aggregate_cache.py 同一把冻结时钟：结果里有 time.time() 派生
# 的字段，不冻结就不可比。
FROZEN = 1791445200.0

_TMP = tempfile.mkdtemp(prefix="wb-cachekinds-")
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
import wb_accounts
import wb_pricing

P.ACCOUNTS_DIR = ACCOUNTS_DIR
wb_pricing.set_data_dir(USAGE_DIR)
wb_pricing.set_settings_dir(ACCOUNTS_DIR)

USAGE_LOG = P.USAGE_LOG
LEGACY = os.path.join(USAGE_DIR, "usage-aggregate-cache.json")
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
    """不排序的 JSON 文本：响应的字节里键序是可见的，比较一律用它。"""
    return json.dumps(obj, ensure_ascii=False)


def first_diff(a, b, span=70):
    if a == b:
        return ""
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return "@%d: %r vs %r" % (i, a[i:i + span], b[i:i + span])
    return "长度 %d vs %d" % (len(a), len(b))


# --------------------------------------------------------------------------
# 数据：一份覆盖五条折叠的日志
# --------------------------------------------------------------------------
def row(at, model, account, prompt, completion, realm="intl", key="k1",
        outcome="completed", credit=0.0):
    r = {"at": at, "model": model, "stream": True, "outcome": outcome,
         "elapsed_ms": 1000, "ttft_ms": 300, "gen_ms": 700,
         "prompt_tokens": prompt, "completion_tokens": completion,
         "reasoning_tokens": 0, "cached_tokens": 0,
         "account": account, "key": key, "tokens_per_sec": 90.0,
         "total_tokens": prompt + completion}
    if realm is not None:
        r["realm"] = realm
    if outcome != "completed":
        r["error"] = "boom"
    if credit:
        r["credit"] = credit
    return r


ROWS = [
    row(FROZEN - 5000, "m-a", "u1", 100, 50, credit=0.5),
    row(FROZEN - 4900, "m-b", "u2", 200, 20, realm="cn"),
    row(FROZEN - 4800, "m-a", "u1", 300, 30, key="k2"),
    row(FROZEN - 4700, "gpt-5.6-luna", "u3", 400, 40, realm=None),
    row(FROZEN - 4600, "m-b", "u2", 500, 50, realm="cn", outcome="failed"),
    row(FROZEN - 4500, "m-a", "u2", 600, 60, key=""),
    row(FROZEN - 4400, "m-c", "u1", 700, 70, outcome="client_aborted"),
    row(FROZEN - 4300, "m-a", "u1", 800, 80),
    row(FROZEN - 4200, "m-b", "u2", 900, 90, realm="cn", key="k2"),
    row(FROZEN - 4100, "gpt-5.6-luna", "u3", 110, 11, realm=None,
        outcome="failed"),
    row(FROZEN - 4000, "m-c", "u1", 220, 22),
    row(FROZEN - 3900, "m-a", "u2", 330, 33, key="k2"),
    row(FROZEN - 3800, "m-b", "u2", 440, 44, realm="cn"),
    row(FROZEN - 3700, "m-a", "u1", 550, 55, outcome="client_aborted"),
    row(FROZEN - 3600, "m-c", "u3", 660, 66, realm=None),
    row(FROZEN - 3500, "m-a", "u1", 770, 77),
    row(FROZEN - 3400, "m-b", "u2", 880, 88, realm="cn"),
    row(FROZEN - 3300, "m-a", "u1", 990, 99, key="k2"),
    row(FROZEN - 3100, "m-a", "u1", 123, 45),
]

PRICING = {
    "meta": {"usd_cny": 7.1},
    "models": {
        "m-a": {"unit": 1000000, "currency": "CNY",
                "flat": {"input_cache_hit": 0.5, "input_cache_miss": 1.0,
                         "output": 2.0}},
        "m-b": {"unit": 1000000, "currency": "CNY",
                "flat": {"input_cache_hit": 0.25, "input_cache_miss": 0.5,
                         "output": 1.0}},
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


def cache_env(enabled=None, min_bytes=None, min_seconds=None):
    for name, value in (("WB_USAGE_CACHE", enabled),
                        ("WB_USAGE_CACHE_MIN_BYTES", min_bytes),
                        ("WB_USAGE_CACHE_MIN_SECONDS", min_seconds)):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = str(value)


def cache_env_nowrite():
    cache_env(enabled=1, min_bytes=10 ** 9, min_seconds=10 ** 9)


def cache_env_force():
    cache_env(enabled=1, min_bytes=0, min_seconds=0)


def cache_env_off():
    cache_env(enabled=0, min_bytes=0, min_seconds=0)


# --------------------------------------------------------------------------
# 结果、状态清理与「模拟重启」
# --------------------------------------------------------------------------
def live_all(accounts=None):
    """五条折叠各走一遍：snapshot / by_account / analytics / series /
    remaining（three realms where the state is per-realm）。"""
    if accounts is None:
        accounts = [wb_accounts.Account({"uid": "u1", "nickname": "u1",
                                         "accessToken": "t", "realm": "intl"})]
    return {
        # 走公开入口（与面板同一条路）：realm="all" 折不限 realm 的那份，
        # realm=None 解析成 CURRENT_REALM（这里就是 intl）。
        "snap": U.usage_snapshot(realm="all", ttl=0),
        "snap_intl": U.usage_snapshot(realm=None, ttl=0),
        "snap_cn": U.usage_snapshot(realm="cn", ttl=0),
        "byacct": U.usage_by_account(ttl=0),
        "analytics": U.compute_usage_analytics(ttl=0),
        "analytics_intl": U.compute_usage_analytics(ttl=0, realm="intl"),
        "series": U.usage_timeseries(range="today", ttl=0),
        "remaining": U._remaining_payload(now=FROZEN, accounts=accounts),
    }


def live_all_canon(accounts=None):
    try:
        return canon(live_all(accounts)), ""
    except Exception as exc:                                  # noqa: BLE001
        return None, "%s: %s" % (type(exc).__name__, exc)


def restart():
    """清掉五份状态、TTL 缓存与「已读过 checkpoint」的记忆，模拟进程重启。"""
    U._usage_snap_state.clear()
    U._byacct_state.update({"buckets": None, "offset": 0, "key": None,
                            "tail": b""})
    U._analytics_state.clear()
    U._series_state.clear()
    with U._remaining_state_lock:
        U._remaining_state.clear()
        U._remaining_state.update({
            "events": [], "keys": set(), "usage": {}, "day": "",
            "log": {"offset": 0, "key": None, "tail": b""},
            "file": {"offset": 0, "key": None, "tail": b""},
        })
    U._snap_cache.clear()
    U._byacct_cache.update({"at": 0.0, "data": None})
    U._analytics_cache.clear()
    U._series_cache.clear()
    with U._remaining_cache_lock:
        U._remaining_cache.update({"at": 0.0, "built_at": 0.0, "data": None})
    U._usage_cache_forget()
    U._usage_cache_progress.clear()
    U._usage_cache_checkpointed.clear()
    U._usage_cache_last_attempt = time.time()
    U._usage_cache_digest_memo.clear()
    U._realm_disk_cache.update({"key": None, "map": None})


def cache_files():
    return {kind: path for kind, path in KIND_FILES.items()
            if os.path.exists(path)}


def clear_cache_files():
    for path in list(cache_files().values()):
        os.unlink(path)
    if os.path.exists(LEGACY):
        os.unlink(LEGACY)


def make_checkpoint():
    """在当前日志上把五条折叠都折一遍并强制落盘。"""
    cache_env_force()
    restart()
    live_all()
    return set(cache_files())


def kind_entries(kind):
    """某个类文件里的条目（文件不在返回 None）。"""
    path = KIND_FILES[kind]
    if not os.path.exists(path):
        return None
    with io.open(path, encoding="utf-8") as fh:
        return json.load(fh)["entries"]


def kind_raw(kind):
    with io.open(KIND_FILES[kind], encoding="utf-8") as fh:
        return json.load(fh)


def write_kind_raw(kind, data):
    io.open(KIND_FILES[kind], "w", encoding="utf-8", newline="\n").write(
        json.dumps(data, ensure_ascii=False))


def legacy_payload(entries):
    """按 v3 单文件的形状拼一份旧格式 checkpoint（一节一个 kind）。"""
    payload = {"schema": 3, "written_at": FROZEN}
    for kind in U._USAGE_CACHE_KINDS:
        payload[kind] = list(entries.get(kind) or [])
    return payload


def write_legacy(entries):
    io.open(LEGACY, "w", encoding="utf-8", newline="\n").write(
        json.dumps(legacy_payload(entries), ensure_ascii=False))


# --------------------------------------------------------------------------
# 基线
# --------------------------------------------------------------------------
write_pricing(PRICING)
write_log(ROWS)
cache_env_off()
restart()
GOLDEN_RAW = live_all()
GOLDEN = canon(GOLDEN_RAW)
GOLDEN_OBJ = json.loads(GOLDEN)
check("基线：五条折叠都有内容",
      GOLDEN_OBJ["snap"]["requests"] > 0 and GOLDEN_OBJ["byacct"]
      and GOLDEN_OBJ["analytics"]["accounts"]
      and GOLDEN_OBJ["series"]["series"] and GOLDEN_OBJ["remaining"]["rows"],
      (GOLDEN_OBJ["snap"]["requests"], len(GOLDEN_OBJ["byacct"]),
       len(GOLDEN_OBJ["series"]["series"]), len(GOLDEN_OBJ["remaining"]["rows"])))

print()
print("[1] 一类一份：五类都落盘、都能采纳，结果与冷折逐字节一致")
written = make_checkpoint()
check("五类各写出一份文件",
      written == set(U._USAGE_CACHE_KINDS), sorted(written))
check("文件名带 kind，且文件里的 kind 标签与文件名一致",
      all(kind_raw(k)["kind"] == k for k in U._USAGE_CACHE_KINDS),
      {k: kind_raw(k).get("kind") for k in U._USAGE_CACHE_KINDS})
check("旧单文件没有被写出来（只读迁移源）", not os.path.exists(LEGACY))
check("写盘是原子替换：没有留下临时文件",
      not [n for n in os.listdir(USAGE_DIR) if n.endswith(".tmp")],
      os.listdir(USAGE_DIR))
cache_env_nowrite()
restart()
LOADED_RAW, err = live_all_canon()
check("五类都被采纳（checkpointed 记到各自的位置）",
      set(U._usage_cache_checkpointed) == set(U._USAGE_CACHE_KINDS),
      U._usage_cache_checkpointed)
check("加载后结果与冷折逐字节一致", LOADED_RAW == GOLDEN,
      err or first_diff(GOLDEN, LOADED_RAW or ""))

print()
print("[2] 单视图进程只解析自己那一类（其余文件坏了也不影响）")
# 计数桩：记录真正被打开解析的类文件与字节数。
reads = []
real_read_kind = U._usage_cache_read_kind


def counting_read_kind(kind):
    reads.append((kind, os.path.getsize(KIND_FILES[kind])))
    return real_read_kind(kind)


legacy_reads = []
real_read_legacy = U._usage_cache_read_legacy_file


def counting_read_legacy():
    legacy_reads.append(1)
    return real_read_legacy()


U._usage_cache_read_kind = counting_read_kind
U._usage_cache_read_legacy_file = counting_read_legacy
try:
    cache_env_nowrite()
    restart()
    reads[:] = []
    legacy_reads[:] = []
    snap = U.usage_snapshot(realm=None, ttl=0)
    check("只跑 /usage 的进程只读了 snapshot 这一类",
          [kind for kind, _ in reads] == ["snapshot"], reads)
    check("没有去碰旧单文件", not legacy_reads, legacy_reads)
    check("采纳成功（结果来自 checkpoint，不是冷折）",
          "snapshot" in U._usage_cache_checkpointed
          and snap["requests"] == GOLDEN_OBJ["snap_intl"]["requests"],
          (dict(U._usage_cache_checkpointed), snap.get("requests")))
    parsed = sum(size for _, size in reads)
    total = sum(os.path.getsize(path) for path in cache_files().values())
    check("只解析了一个类文件、字节数少于全部 checkpoint",
          parsed == os.path.getsize(KIND_FILES["snapshot"]) and parsed < total,
          (parsed, total))
finally:
    U._usage_cache_read_kind = real_read_kind
    U._usage_cache_read_legacy_file = real_read_legacy

# 更强的一条：其余四类文件整份是乱码，单视图进程照样采纳。
cache_env_nowrite()
restart()
for kind in ("by_account", "analytics", "series", "remaining"):
    io.open(KIND_FILES[kind], "wb").write(b"{not json at all")
try:
    restart()
    snap = U.usage_snapshot(realm=None, ttl=0)
    check("其余四类文件是乱码时，/usage 仍从自己的文件采纳",
          "snapshot" in U._usage_cache_checkpointed
          and snap["requests"] == GOLDEN_OBJ["snap_intl"]["requests"],
          snap.get("requests"))
finally:
    make_checkpoint()

print()
print("[3] 混合新旧：只推进一类，其余类按各自文件里的旧 offset 续读")
append_log([row(FROZEN - 2000, "m-a", "u1", 2000, 200, key="k2"),
            row(FROZEN - 1900, "m-b", "u2", 100, 10, realm="cn"),
            row(FROZEN - 1800, "m-c", "u3", 300, 30, outcome="failed")])
cache_env_off()
restart()
GROWN = canon(live_all())
check("增长后的真值确实变了", GROWN != GOLDEN)
# 单视图进程：只折 series（写盘只该动 series 这一份）。
old_files = {kind: io.open(path, "rb").read()
             for kind, path in cache_files().items()}
cache_env_force()
restart()
U.usage_timeseries(range="today", ttl=0)
now_files = {kind: io.open(path, "rb").read()
             for kind, path in cache_files().items()}
check("只有推进过的那一类被重写（series 变了）",
      now_files["series"] != old_files["series"], sorted(cache_files()))
check("其余四类的旧文件一个字节都没动",
      all(now_files[k] == old_files[k] for k in old_files if k != "series"),
      sorted(k for k in old_files if now_files.get(k) != old_files[k]))
# 新进程把五类都折一遍：series 从新文件续读、其余四类从旧文件续读。
cache_env_nowrite()
restart()
MIXED_RAW, err = live_all_canon()
check("混合新旧下的结果与全量冷折逐字节一致",
      MIXED_RAW == GROWN, err or first_diff(GROWN, MIXED_RAW or ""))
check("每类各自记录自己的位置（series 在文件尾、其余在旧 offset）",
      U._usage_cache_checkpointed.get("series") == os.path.getsize(USAGE_LOG)
      and 0 < U._usage_cache_checkpointed.get("snapshot", 0)
      < os.path.getsize(USAGE_LOG),
      (dict(U._usage_cache_checkpointed), os.path.getsize(USAGE_LOG)))
write_log(ROWS)                       # 恢复基线日志，后续用例共用它

print()
print("[4] 写侧：不活跃的类不被清掉，之后仍能采纳")
# 协调者实测的破坏路径：一个进程里只有 remaining 在折（其余四类没有任何
# 状态），旧布局会把其余四类写成空段、整份覆盖——盘上那四类的旧数据被清掉。
written = make_checkpoint()
before_files = {kind: io.open(path, "rb").read()
                for kind, path in cache_files().items()}
append_log([row(FROZEN - 100, "m-a", "u1", 500, 50)])
cache_env_force()
restart()
U._remaining_payload(now=FROZEN, accounts=[wb_accounts.Account(
    {"uid": "u1", "nickname": "u1", "accessToken": "t", "realm": "intl"})])
after_files = {kind: io.open(path, "rb").read()
               for kind, path in cache_files().items()}
check("只有 remaining 被重写",
      after_files["remaining"] != before_files["remaining"]
      and all(after_files[k] == before_files[k]
              for k in before_files if k != "remaining"),
      sorted(k for k in before_files
             if after_files.get(k) != before_files[k]))
check("其余四类的旧文件仍在（没有被空段覆盖）",
      set(after_files) == set(U._USAGE_CACHE_KINDS), sorted(after_files))
# 重启：四类从各自的旧文件续读（只折新增的那一行），remaining 从新文件续读。
cache_env_nowrite()
restart()
AFTER_RAW, err = live_all_canon()
check("重启后四类仍然被采纳（旧文件没有被清掉）",
      set(U._usage_cache_checkpointed) == set(U._USAGE_CACHE_KINDS),
      U._usage_cache_checkpointed)
cache_env_off()
restart()
WANT = canon(live_all())
check("采纳后的结果与全量冷折一致",
      AFTER_RAW == WANT, err or first_diff(WANT, AFTER_RAW or ""))
write_log(ROWS)
make_checkpoint()

print()
print("[4.1] 同一类里不活跃的 realm 条目也不被清掉（真机实测的破坏路径）")
# 真机实测：重启后先折的 realm 触发一次写盘，盘上「还没被请求过」的另一个
# realm 条目被整份覆盖清掉——那个 realm 下次请求只能重折（intl 重折 15s）。
make_checkpoint()
def _snap_realms():
    return sorted("all" if e.get("realm") is None else e["realm"]
                  for e in kind_entries("snapshot"))
check("snapshot 文件里有三个 realm 条目（all + intl + cn）",
      _snap_realms() == ["all", "cn", "intl"], _snap_realms())
append_log([row(FROZEN - 100, "m-b", "u2", 700, 70)])
restart()
cache_env_force()
U.usage_snapshot(realm="all", ttl=0)          # 只请求 realm=all，不碰 intl/cn
check("只写 all 时 intl/cn 的旧条目仍在（没有被清掉）",
      _snap_realms() == ["all", "cn", "intl"], _snap_realms())
cache_env_nowrite()
restart()
U.usage_snapshot(realm=None, ttl=0)           # intl 之后仍能采纳（不重折）
check("intl 之后仍被采纳（snapshot 进了 checkpointed）",
      U._usage_cache_checkpointed.get("snapshot", 0) > 0,
      U._usage_cache_checkpointed)
cache_env_off()
restart()
write_log(ROWS)
make_checkpoint()

print()
print("[5] 一类坏掉只作废那一类，其余类照常采纳")
for kind in U._USAGE_CACHE_KINDS:
    # 每类各坏一种顶层形状：schema 改成未来版本 / kind 标签写错 / entries
    # 不是列表。坏法轮着来，覆盖面更大。
    make_checkpoint()
    raw = kind_raw(kind)
    if kind == "snapshot":
        raw["schema"] = U._USAGE_CACHE_SCHEMA + 1
        what = "schema 比当前新"
    elif kind == "by_account":
        raw["kind"] = "snapshot"
        what = "kind 标签与文件名不符"
    else:
        raw["entries"] = {}
        what = "entries 不是列表"
    write_kind_raw(kind, raw)
    cache_env_nowrite()
    restart()
    got, err = live_all_canon()
    check("%s 坏掉（%s）：结果回到真值" % (kind, what), got == GOLDEN,
          err or first_diff(GOLDEN, got or ""))
    check("%s 坏掉：其余四类照常采纳" % kind,
          set(U._usage_cache_checkpointed) == set(U._USAGE_CACHE_KINDS) - {kind},
          U._usage_cache_checkpointed)

print()
print("[6] 旧单文件（schema 3）只读兼容：升级后第一次刷新不必整段重折")
make_checkpoint()
entries = {kind: kind_entries(kind) for kind in U._USAGE_CACHE_KINDS}
clear_cache_files()
write_legacy(entries)
check("旧文件就位、五类文件都不在",
      os.path.exists(LEGACY) and not cache_files(), sorted(cache_files()))
cache_env_nowrite()
restart()
LEGACY_RAW, err = live_all_canon()
check("五类都从旧文件采纳（各取自己那一节）",
      set(U._usage_cache_checkpointed) == set(U._USAGE_CACHE_KINDS),
      U._usage_cache_checkpointed)
check("从旧文件采纳的结果与冷折逐字节一致",
      LEGACY_RAW == GOLDEN, err or first_diff(GOLDEN, LEGACY_RAW or ""))

# 类文件存在时旧文件不参与：把旧文件写成"有毒"的（requests +1000），新进程
# 只认类文件，结果必须仍是真值。
make_checkpoint()
entries = {kind: kind_entries(kind) for kind in U._USAGE_CACHE_KINDS}
poisoned = dict(entries)
poisoned["snapshot"] = [dict(e) for e in entries["snapshot"]]
poisoned["snapshot"][0] = json.loads(json.dumps(poisoned["snapshot"][0]))
poisoned["snapshot"][0]["snap"]["requests"] += 1000
write_legacy(poisoned)
cache_env_nowrite()
restart()
got, err = live_all_canon()
check("类文件存在时旧文件整份被忽略（带毒也不影响结果）", got == GOLDEN,
      err or first_diff(GOLDEN, got or ""))

# 类文件坏掉、旧文件在：退回旧文件采纳（更旧的 offset，结果仍正确）。
# 旧文件换回干净的那一份：带毒载荷的旧文件被采纳是既定的信任模型（位置/
# 指纹/形状过了就照用，载荷值不做校验），不属于「坏文件必须被拒」的范畴。
write_legacy(entries)
snapshot_raw = kind_raw("snapshot")
snapshot_raw["schema"] = U._USAGE_CACHE_SCHEMA + 1
write_kind_raw("snapshot", snapshot_raw)
cache_env_nowrite()
restart()
got, err = live_all_canon()
check("类文件坏掉时退回旧文件采纳（结果仍正确）", got == GOLDEN,
      err or first_diff(GOLDEN, got or ""))
check("这一类确实走了旧文件（采纳位置 = 旧文件里的 offset）",
      U._usage_cache_checkpointed.get("snapshot")
      == entries["snapshot"][0]["offset"],
      (U._usage_cache_checkpointed.get("snapshot"),
       entries["snapshot"][0]["offset"]))

# 旧文件的 schema 不对（2 / 4 / true）：整份作废，五类都冷折。
make_checkpoint()
entries = {kind: kind_entries(kind) for kind in U._USAGE_CACHE_KINDS}
for bad in (2, 4, True, "3", None):
    clear_cache_files()
    payload = legacy_payload(entries)
    if bad is None:
        payload.pop("schema")
    else:
        payload["schema"] = bad
    io.open(LEGACY, "w", encoding="utf-8", newline="\n").write(
        json.dumps(payload, ensure_ascii=False))
    cache_env_nowrite()
    restart()
    got, err = live_all_canon()
    check("旧文件 schema=%r：整份作废、结果回到真值" % (bad,), got == GOLDEN,
          err or first_diff(GOLDEN, got or ""))
    check("旧文件 schema=%r：一类都没有被采纳" % (bad,),
          not U._usage_cache_checkpointed, U._usage_cache_checkpointed)

print()
print("[7] 某一类写失败不拖累其余类；失败后退避不重试")
make_checkpoint()
clear_cache_files()
cache_env_force()
real_kind_path = U._usage_cache_kind_path
missing_dir = os.path.join(_TMP, "no-such-dir")
U._usage_cache_kind_path = lambda kind: (
    os.path.join(missing_dir, "usage-aggregate-cache-%s.json" % kind)
    if kind == "analytics" else real_kind_path(kind))
try:
    restart()
    live_all()
    written = set(cache_files())
    check("坏掉的那一类没写出来", "analytics" not in written, sorted(written))
    check("其余四类照常写出来",
          written == set(U._USAGE_CACHE_KINDS) - {"analytics"}, sorted(written))
    check("写失败的那一类没有被记成已落盘",
          "analytics" not in U._usage_cache_checkpointed,
          U._usage_cache_checkpointed)
finally:
    U._usage_cache_kind_path = real_kind_path
    if os.path.exists(missing_dir):
        shutil.rmtree(missing_dir, ignore_errors=True)
make_checkpoint()
cache_env_off()
clear_cache_files()
restart()
live_all()
check("开关关闭：一类文件都不写", not cache_files() and not os.path.exists(LEGACY),
      sorted(cache_files()))

shutil.rmtree(_TMP, ignore_errors=True)
print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
