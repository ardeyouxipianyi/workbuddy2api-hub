"""聚合状态落盘：往返、降级与节流。

PR #189 把面板的 all-time 聚合改成按字节偏移增量折叠，但状态只在进程内存
里：服务一重启，第一次刷新仍是全量扫描，而日志每天涨一万多行，冷扫描成本
跟着线性上升。这一套把几份状态（snapshot / by_account / analytics 的
all-time 折叠）连同位置信息落盘到数据目录，重启后第一次刷新只读新增字节。

v4 起一类状态一份文件（usage-aggregate-cache-<kind>.json）：这一套按新的
文件布局读写；升级前单文件（v3）的只读兼容由 _test_usage_cache_kinds.py
钉住。

这个套件钉住的是「checkpoint 只是加速」这条底线：

  * 往返：折叠 → 写盘 → 模拟重启加载 → 聚合结果与不落盘时逐字节一致
    （**包括 JSON 键顺序**：响应字节里键序是可见的，比较一律用不排序的
    文本，并单独断言键序树一致——v1 用 sort_keys 写盘让加载后变成字母序，
    正是这一类断言漏掉之后才暴露的问题）；
  * 真·新进程：子进程加载 checkpoint，结果一致且确实走了加载路径；
  * 续扫：checkpoint 之后日志增长，新进程只折增量，结果与全量一致；
  * 降级：损坏 JSON / 截断 / schema 不符 / 日志被替换、截断或同尺寸重写 /
    tail 签名不符 / 位置或类型不合法 / 目录不可写，一律当作没有缓存、走
    全量，结果正确且不抛错；
  * 采纳：剪枝线摘掉旧天的小时表（小时层比日桶短命）不算损坏，analytics
    状态照常采用——误拒就会天天全量重折（#26）；
  * 指纹：价格表或 realm 归属一变，checkpoint 必须作废；
  * 节流：默认阈值下连续刷新不写文件，推进到阈值才写；
  * 开关：WB_USAGE_CACHE=0 时行为与 #189 原状一致（不读也不写）。

降级用例的写法：先给「应当被拒绝」的那份状态加一个一眼可见的载荷标记
（poison），再做变异。标记没被采用就说明校验拦住了它；一旦被错误采用，结果
里就会多出那个标记，用例当场失败——比只看「有没有加载」更直接。

No network: the usage log, the pricing snapshot and the settings file are
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

# 与 my_dump.py 同一把冻结时钟：聚合结果里有 time.time() 派生的字段，不冻结
# 的话「父进程算的」和「子进程算的」必然不同——那是墙钟差异，不是行为差异。
FROZEN = 1791445200.0

_TMP = tempfile.mkdtemp(prefix="wb-aggcache-")
USAGE_DIR = os.path.join(_TMP, "usage")
ACCOUNTS_DIR = os.path.join(_TMP, "accounts")
os.makedirs(USAGE_DIR, exist_ok=True)
os.makedirs(ACCOUNTS_DIR, exist_ok=True)
os.environ["WB_PROXY_USAGE_DIR"] = USAGE_DIR
os.environ["ACCOUNTS_DIR"] = ACCOUNTS_DIR

import wb_proxy as P

import wb_usage as U
import wb_accounts
import wb_pricing

# 这个套件不碰真 accounts/ 目录：ACCOUNTS_DIR 是脚本旁边的硬编码路径，只能
# 在导入后重绑（_test_analytics_by_key.py 里记过同样的坑）。
P.ACCOUNTS_DIR = ACCOUNTS_DIR
wb_pricing.set_data_dir(USAGE_DIR)
wb_pricing.set_settings_dir(ACCOUNTS_DIR)

USAGE_LOG = P.USAGE_LOG
# v4 布局：一类状态一份文件。套件按 kind 读写这些文件，个别用例还会直接
# 改坏某一类的文件来验证「一类坏不影响其余类」。
KIND_FILES = {kind: U._usage_cache_kind_path(kind)
              for kind in U._USAGE_CACHE_KINDS}
# snapshot 的结果里带着进程自己的 _usage["started"]，冻结它让父进程与子进程
# 的输出可比。
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
    """两份结果的可比形态：同一份 JSON 文本。

    不排序键：响应的 JSON 字节里键序是可见的，checkpoint 加载回来的状态必须
    连键顺序都和冷折叠一致（v1 的 sort_keys 写盘让加载后变成字母序，正是这个
    函数当初用 sort_keys 比较时漏掉的那一类差异）。比较一律用不排序的文本。
    """
    return json.dumps(obj, ensure_ascii=False)


def first_diff(a, b, span=70):
    if a == b:
        return ""
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return "@%d: %r vs %r" % (i, a[i:i + span], b[i:i + span])
    return "长度 %d vs %d" % (len(a), len(b))


def key_order(obj):
    """一份 JSON 结构里的键顺序（值只留形状）。

    用 list 承载 dict 的键序，这样 == 比较本身就对顺序敏感。tuple 和 list 在
    JSON 里都是数组（父进程的结果里有 tuple，子进程的是 list），按序列同等
    对待，键序比较才不会把「经过一次 JSON 往返」误判成差异。
    """
    if isinstance(obj, dict):
        return [(k, key_order(v)) for k, v in obj.items()]
    if isinstance(obj, (list, tuple)):
        return [key_order(v) for v in obj]
    return None


def first_order_path(a, b, path=""):
    """两棵键序树的第一个差异路径，用于失败时指出具体是哪个 dict。"""
    if isinstance(a, dict) and isinstance(b, dict):
        if list(a.keys()) != list(b.keys()):
            return "%s: %s vs %s" % (path or "$", list(a.keys()), list(b.keys()))
        for k in a:
            hit = first_order_path(a[k], b[k], "%s.%s" % (path, k))
            if hit:
                return hit
        return ""
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        for i, (x, y) in enumerate(zip(a, b)):
            hit = first_order_path(x, y, "%s[%d]" % (path, i))
            if hit:
                return hit
        return ""
    return ""


def adopted_all(state):
    """三份状态是否都被采用（by_account 没有指纹，它单独为真说明不了问题）。"""
    return (state.get("snapshot", 0) > 0 and state.get("analytics", 0) > 0
            and state.get("by_account", 0) > 0)


def check_same_shape(label, cold, loaded):
    """值 + 键序都要一致（冷折叠结果 vs 加载 checkpoint 后的结果）。"""
    check(label + "：值一致", canon(cold) == canon(loaded),
          first_diff(canon(cold), canon(loaded)))
    check(label + "：键顺序一致", key_order(cold) == key_order(loaded),
          first_order_path(cold, loaded))


# --------------------------------------------------------------------------
# 数据：一张覆盖各条折叠分支的小日志
# --------------------------------------------------------------------------
def row(at, model, account, prompt, completion, realm="intl", key="k1",
        outcome="completed"):
    r = {"at": at, "model": model, "stream": True, "outcome": outcome,
         "elapsed_ms": 1000, "ttft_ms": 300, "gen_ms": 700,
         "prompt_tokens": prompt, "completion_tokens": completion,
         "reasoning_tokens": 0, "cached_tokens": 0,
         "account": account, "key": key, "tokens_per_sec": 90.0}
    if realm is not None:
        r["realm"] = realm
    if outcome != "completed":
        r["error"] = "boom"
    # 放在最后：tail 签名的变异测试要改的就是这个字段。
    r["total_tokens"] = prompt + completion
    return r


ROWS = [
    row(FROZEN - 5000, "m-a", "u1", 100, 50),
    row(FROZEN - 4900, "m-b", "u2", 200, 20, realm="cn"),
    row(FROZEN - 4800, "m-a", "u1", 300, 30, key="k2"),
    # 没有 realm 字段、模型只认前缀（gpt-）的行：归属与 CURRENT_REALM 无关
    row(FROZEN - 4700, "gpt-5.6-luna", "u3", 400, 40, realm=None),
    row(FROZEN - 4600, "m-b", "u2", 500, 50, realm="cn", outcome="failed"),
    row(FROZEN - 4500, "m-a", "u2", 600, 60, key=""),
    row(FROZEN - 4400, "m-c", "u1", 700, 70, outcome="client_aborted"),
    row(FROZEN - 4300, "m-a", "u1", 800, 80),
    row(FROZEN - 4200, "m-b", "u2", 900, 90, realm="cn", key="k2"),
    row(FROZEN - 4100, "gpt-5.6-luna", "u3", 110, 11, realm=None, outcome="failed"),
    row(FROZEN - 4000, "m-c", "u1", 220, 22),
    row(FROZEN - 3900, "m-a", "u2", 330, 33, key="k2"),
    row(FROZEN - 3800, "m-b", "u2", 440, 44, realm="cn"),
    row(FROZEN - 3700, "m-a", "u1", 550, 55, outcome="client_aborted"),
    # 没有 realm 字段、模型也不认前缀（m-c）：归属跟着 CURRENT_REALM 走
    row(FROZEN - 3600, "m-c", "u3", 660, 66, realm=None),
    row(FROZEN - 3500, "m-a", "u1", 770, 77),
    row(FROZEN - 3400, "m-b", "u2", 880, 88, realm="cn"),
    row(FROZEN - 3300, "m-a", "u1", 990, 99, key="k2"),
]
# 一条没有 `key` 字段的行（keys 功能之前写下的行）→ __before_keys__ 桶。
_pre_key = row(FROZEN - 3200, "m-c", "u1", 111, 11)
del _pre_key["key"]
ROWS.append(_pre_key)
# 最后一行以 total_tokens 结尾：tail 变异测试改的就是它。
ROWS.append(row(FROZEN - 3100, "m-a", "u1", 123, 45))

# 一份最小价目表：部分行有价、部分行没有，两条路径都要覆盖。
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
    # load_pricing() 按 (path, mtime) 缓存：两次写入落在同一个 mtime 刻度上
    # 会拿不到新内容（Windows 上 time.time() 只有 ~15ms 精度），把 mtime 拨
    # 到一把确定会前进的整数秒上。
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
    """把三个开关写进环境（None = 删除该变量，用默认值）。"""
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
# 结果与「模拟重启」
# --------------------------------------------------------------------------
def live():
    """五个入口的原始结果（三份状态、三个 realm 视图都覆盖）。"""
    return {
        "snap": U._usage_snapshot_uncached(),
        "snap_intl": U._usage_snapshot_uncached(realm="intl"),
        "snap_cn": U._usage_snapshot_uncached(realm="cn"),
        "byacct": U._usage_by_account_uncached(),
        "analytics": U._compute_usage_analytics_uncached(),
        "analytics_intl": U._compute_usage_analytics_uncached(realm="intl"),
        "analytics_cn": U._compute_usage_analytics_uncached(realm="cn"),
    }


def live_raw():
    """(原始结果, 异常描述)。异常本身就是「校验没拦住」的一种表现。"""
    try:
        return live(), ""
    except Exception as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)


def live_canon():
    """(canon(结果), 异常描述)。"""
    obj, err = live_raw()
    return (canon(obj) if obj is not None else None), err


def restart():
    """清掉三份状态、TTL 缓存与「已读过 checkpoint」的记忆，模拟一次进程重启。"""
    U._usage_snap_state.clear()
    U._byacct_state.update({"buckets": None, "offset": 0, "key": None, "tail": b""})
    U._analytics_state.clear()
    U._snap_cache.clear()
    U._byacct_cache.update({"at": 0.0, "data": None})
    U._analytics_cache.clear()
    U._usage_cache_forget()
    U._usage_cache_progress.clear()
    U._usage_cache_checkpointed.clear()
    U._usage_cache_last_attempt = time.time()
    U._usage_cache_digest_memo.clear()
    U._realm_disk_cache.update({"key": None, "map": None})


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
out = {
    "snap": U._usage_snapshot_uncached(),
    "snap_intl": U._usage_snapshot_uncached(realm="intl"),
    "snap_cn": U._usage_snapshot_uncached(realm="cn"),
    "byacct": U._usage_by_account_uncached(),
    "analytics": U._compute_usage_analytics_uncached(),
    "analytics_intl": U._compute_usage_analytics_uncached(realm="intl"),
    "analytics_cn": U._compute_usage_analytics_uncached(realm="cn"),
}
with open(os.environ["WB_CHILD_OUT"], "w", encoding="utf-8") as fh:
    # 不排序：键序本身就是要比的东西（父进程会连键序一起比对）。
    json.dump({"checkpointed": U._usage_cache_checkpointed, "results": out}, fh,
              ensure_ascii=False)
print("child ok")
'''
CHILD_SCRIPT = os.path.join(_TMP, "child_runner.py")
io.open(CHILD_SCRIPT, "w", encoding="utf-8", newline="\n").write(
    RUNNER.replace("__FROZEN__", repr(FROZEN)).replace("__ROOT__", repr(ROOT)))


def run_child(extra_env=None):
    """真·新进程：跑同一组入口，返回 (原始结果, checkpointed)。"""
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


def cache_files():
    """当前存在的 kind 文件路径列表。"""
    return [path for path in KIND_FILES.values() if os.path.exists(path)]


def no_cache_files():
    """一个 kind 文件都没有（等价于「没有 checkpoint」）。"""
    return not cache_files()


def make_checkpoint():
    """在当前日志上折一遍并强制落盘。"""
    cache_env_force()
    restart()
    live()
    return bool(cache_files())


def cache_data():
    """五个 kind 文件的合并视图：{kind: entries}（缺失/读不出的当空表）。

    与 v3 的单文件视图同形，用例里 _entry(data, kind, realm) 的写法不用改。
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
    """把合并视图写回 kind 文件（保持 v4 的包装形状）。

    空表不写文件——与生产侧的写盘一致（没内容的类不落盘）。
    """
    for kind in U._USAGE_CACHE_KINDS:
        entries = data.get(kind) or []
        if not entries:
            continue
        payload = {"schema": U._USAGE_CACHE_SCHEMA, "kind": kind,
                   "written_at": FROZEN, "entries": entries}
        io.open(KIND_FILES[kind], "w", encoding="utf-8", newline="\n").write(
            json.dumps(payload, ensure_ascii=False))


def kind_file_data(kind):
    """某个 kind 文件的原始 JSON（顶层形状校验用）。"""
    with io.open(KIND_FILES[kind], encoding="utf-8") as fh:
        return json.load(fh)


def save_kind_file(kind, data):
    io.open(KIND_FILES[kind], "w", encoding="utf-8", newline="\n").write(
        json.dumps(data, ensure_ascii=False))


def clear_cache_files():
    for path in cache_files():
        os.unlink(path)


def _entry(data, kind, realm=None):
    """checkpoint 里某一份状态（by_account 只有一份；其余按 realm 匹配）。"""
    for entry in data[kind]:
        if kind == "by_account" or entry.get("realm") == realm:
            return entry
    raise AssertionError("没有 %s/%r 条目" % (kind, realm))


# --------------------------------------------------------------------------
# 基线：不落盘的真值
# --------------------------------------------------------------------------
write_pricing(PRICING)
write_log(ROWS)
cache_env_off()
restart()
GOLDEN_RAW = live()
GOLDEN = canon(GOLDEN_RAW)
GOLDEN_OBJ = json.loads(GOLDEN)
check("基线：无 checkpoint 时三份聚合都有内容",
      GOLDEN_OBJ["snap"]["requests"] > 0 and GOLDEN_OBJ["byacct"]
      and GOLDEN_OBJ["analytics"]["accounts"],
      (GOLDEN_OBJ["snap"]["requests"], len(GOLDEN_OBJ["byacct"])))
check("基线：有价与无价两条路径都被折到",
      GOLDEN_OBJ["snap"]["cost_missing"] and GOLDEN_OBJ["snap"]["cost_cny"] > 0,
      (GOLDEN_OBJ["snap"]["cost_missing"], GOLDEN_OBJ["snap"]["cost_cny"]))
check("基线：realm 过滤真的在起作用（intl/cn 的请求数不同）",
      GOLDEN_OBJ["snap_intl"]["requests"] != GOLDEN_OBJ["snap_cn"]["requests"],
      (GOLDEN_OBJ["snap_intl"]["requests"], GOLDEN_OBJ["snap_cn"]["requests"]))
check("基线：关掉开关时连文件都不写",
      no_cache_files(), os.listdir(USAGE_DIR))

print()
print("[1] 往返：写盘 → 模拟重启加载 → 与不落盘逐字节一致（含键顺序）")
check("写盘成功", make_checkpoint())
stored = cache_data()
check("每个写出的 kind 文件都是 schema=当前版本、kind 标签正确的 JSON 对象",
      all(kind_file_data(k)["schema"] == U._USAGE_CACHE_SCHEMA
          and kind_file_data(k)["kind"] == k
          and isinstance(kind_file_data(k)["entries"], list)
          for k in U._USAGE_CACHE_KINDS if os.path.exists(KIND_FILES[k])),
      {k: (kind_file_data(k).get("schema"), kind_file_data(k).get("kind"))
       for k in U._USAGE_CACHE_KINDS if os.path.exists(KIND_FILES[k])})
check("schema 版本是 v5（一类一份的布局 + 「用到的价」指纹）",
      U._USAGE_CACHE_SCHEMA == 5, U._USAGE_CACHE_SCHEMA)
check("三份状态都写进去了（每个 realm 一份）",
      len(stored["snapshot"]) == 3 and len(stored["by_account"]) == 1
      and len(stored["analytics"]) == 3,
      {k: len(v) for k, v in stored.items()})
check("没折过的两类（series / remaining）不写文件",
      not os.path.exists(KIND_FILES["series"])
      and not os.path.exists(KIND_FILES["remaining"]),
      cache_files())
check("realm 标签跟着状态一起落盘（null 表示不限 realm）",
      sorted([e["realm"] if e["realm"] is not None else "all"
              for e in stored["snapshot"]]) == ["all", "cn", "intl"],
      [e["realm"] for e in stored["snapshot"]])
check("by_account 那份不带 realm 轴", "realm" not in stored["by_account"][0])
check("写盘是原子替换：没有留下临时文件",
      not [n for n in os.listdir(USAGE_DIR) if n.endswith(".tmp")],
      os.listdir(USAGE_DIR))
# 落盘时不能排序：文件里的键序必须是折叠的插入顺序。排序（v1 的 sort_keys）
# 会让加载回来的一切变成字母序，响应字节随之与冷启动不同。
stored_keys = list(_entry(stored, "snapshot", None)["snap"].keys())
fold_keys = list(U._usage_snap_state[None]["snap"].keys())
check("落盘的键顺序就是折叠的插入顺序（不是字母序）",
      stored_keys == fold_keys and stored_keys != sorted(stored_keys),
      (stored_keys[:6], sorted(stored_keys)[:6]))

cache_env_nowrite()
restart()
LOADED_RAW, err = live_raw()
ADOPTED = dict(U._usage_cache_checkpointed)
check("模拟重启后加载到了 checkpoint（三份都被采用）", adopted_all(ADOPTED), ADOPTED)
check("加载后结果与不落盘逐字节一致", canon(LOADED_RAW or {}) == GOLDEN,
      err or first_diff(GOLDEN, canon(LOADED_RAW or {})))
# 这是 v1 漏掉的那一类断言：值相等还不够，响应的 JSON 字节里键序是可见的。
check_same_shape("加载后 vs 冷折叠（模拟重启）", GOLDEN_RAW, LOADED_RAW)
check("键序自检：冷折叠的顶层 summary 不是字母序（否则这条断言测不出东西）",
      list(GOLDEN_RAW["analytics"]["summary"]["all_time"].keys())[0] != "cached_tokens",
      list(GOLDEN_RAW["analytics"]["summary"]["all_time"].keys())[:4])

print()
print("[2] 真·新进程：加载 checkpoint 后结果一致（含键顺序），且确实走了加载路径")
child_raw, child_state = run_child()
check("新进程结果与不落盘一致", canon(child_raw) == GOLDEN,
      first_diff(GOLDEN, canon(child_raw)))
check_same_shape("新进程 vs 冷折叠（真·新进程）", GOLDEN_RAW, child_raw)
check("新进程记录到了加载的 offset（三份都被采用）",
      adopted_all(child_state), child_state)
child_off, child_state_off = run_child({"WB_USAGE_CACHE": "0"})
check("新进程 + 关掉开关：结果一致", canon(child_off) == GOLDEN,
      first_diff(GOLDEN, canon(child_off)))
check_same_shape("新进程（关掉开关）vs 冷折叠", GOLDEN_RAW, child_off)
check("新进程 + 关掉开关：不读 checkpoint", child_state_off == {}, child_state_off)

print()
print("[3] 续扫：checkpoint 之后日志增长，只折增量（含键顺序）")
append_log([
    row(FROZEN - 2000, "m-a", "u1", 2000, 200, key="k2"),
    row(FROZEN - 1900, "m-b", "u2", 100, 10, realm="cn"),
    row(FROZEN - 1800, "m-c", "u3", 300, 30, outcome="failed"),
])
cache_env_off()
restart()
GROWN_RAW = live()
GROWN = canon(GROWN_RAW)
check("增长后的真值确实变了", GROWN != GOLDEN)
cache_env_nowrite()
restart()
LOADED_GROWN_RAW, err = live_raw()
ADOPTED_GROWN = dict(U._usage_cache_checkpointed)
check("加载旧 checkpoint 后只折增量，结果等于全量", canon(LOADED_GROWN_RAW or {}) == GROWN,
      err or first_diff(GROWN, canon(LOADED_GROWN_RAW or {})))
check_same_shape("续扫（增量折叠）vs 全量冷折叠", GROWN_RAW, LOADED_GROWN_RAW)
check("加载的 offset 是旧 checkpoint 的位置（不是文件尾）",
      bool(ADOPTED_GROWN)
      and ADOPTED_GROWN.get("snapshot", 0) < os.path.getsize(USAGE_LOG),
      (ADOPTED_GROWN, os.path.getsize(USAGE_LOG)))
child_grown, _ = run_child()
check("真·新进程同样只折增量", canon(child_grown) == GROWN,
      first_diff(GROWN, canon(child_grown)))
check_same_shape("真·新进程（续扫）vs 全量冷折叠", GROWN_RAW, child_grown)
# 恢复成基线日志，后面的用例都在这份日志上做
write_log(ROWS)
cache_env_off()
restart()
GOLDEN_RAW = live()
GOLDEN = canon(GOLDEN_RAW)
GOLDEN_OBJ = json.loads(GOLDEN)

print()
print("[4] 降级：坏 checkpoint 一律当作没有缓存")


def poison(data, kind, realm=None):
    """给一份状态加一个一眼可见的载荷标记。

    校验放行它 → 结果里就会多出这个标记 → 用例当场失败；校验拦住了它 →
    结果与真值逐字节一致。比只看「加载了没有」更直接。
    """
    entry = _entry(data, kind, realm)
    if kind == "snapshot":
        entry["snap"]["requests"] += 1000
    elif kind == "by_account":
        entry["buckets"]["__poison__"] = {
            "account": "__poison__", "requests": 1000, "prompt_tokens": 0,
            "completion_tokens": 0, "reasoning_tokens": 0, "cached_tokens": 0,
            "total_tokens": 0, "models": {}}
    else:
        entry["maps"]["summary"]["requests"] += 1000


# 先证明标记本身是可见的：三份都带上标记、都照常被采用时，结果必须明显
# 不同——否则下面的降级用例可能什么都没测到。
make_checkpoint()
data = cache_data()
poison(data, "snapshot", None)
poison(data, "by_account")
poison(data, "analytics", None)
save_cache(data)
cache_env_nowrite()
restart()
got, err = live_canon()
check("标记机制自检：三份状态都被采用时标记会出现在结果里",
      got is not None and got != GOLDEN, err or "结果没有变化")


def mutate_cache(mutate, label, kind="snapshot", realm=None):
    """先给要变异的那份状态加标记，再变异，然后从它加载——结果必须回到真值。"""
    make_checkpoint()
    data = cache_data()
    poison(data, kind, realm)
    mutate(data)
    save_cache(data)
    cache_env_nowrite()
    restart()
    got, err = live_canon()
    check(label, got == GOLDEN, err or first_diff(GOLDEN, got or ""))


def mutate_kind_file(kind, mutate, label, others=("analytics", "by_account")):
    """改坏某个 kind 文件的顶层形状，再从它加载。

    该类的状态必须被拒、结果回到真值，而其余类照常被采用——一类文件坏了不
    拖累其余类，这是 v4 拆文件的直接承诺。
    """
    make_checkpoint()
    data = kind_file_data(kind)
    mutate(data)
    save_kind_file(kind, data)
    cache_env_nowrite()
    restart()
    got, err = live_canon()
    check(label, got == GOLDEN, err or first_diff(GOLDEN, got or ""))
    if others:
        check(label + "：其余类照常采用",
              all(k in U._usage_cache_checkpointed for k in others),
              U._usage_cache_checkpointed)


# 4.1 文件本身读不出来（五个 kind 文件一起改坏：等于「没有任何 checkpoint」）
make_checkpoint()
for path in cache_files():
    io.open(path, "wb").write(b"{this is not json")
cache_env_nowrite()
restart()
check("损坏 JSON：结果回到真值", canon(live()) == GOLDEN)
make_checkpoint()
for path in cache_files():
    raw = io.open(path, "rb").read()
    io.open(path, "wb").write(raw[:40] + b"\x00\xff\xfe" + raw[60:])
restart()
check("二进制乱码：结果回到真值", canon(live()) == GOLDEN)
make_checkpoint()
for path in cache_files():
    text = io.open(path, encoding="utf-8").read()
    io.open(path, "w", encoding="utf-8").write(text[:len(text) // 2])
restart()
check("截断文件：结果回到真值", canon(live()) == GOLDEN)
make_checkpoint()
for path in cache_files():
    io.open(path, "w", encoding="utf-8").write("[1, 2, 3]")
restart()
check("顶层不是对象：结果回到真值", canon(live()) == GOLDEN)
make_checkpoint()
for path in cache_files():
    io.open(path, "w", encoding="utf-8").write("")
restart()
check("空文件：结果回到真值", canon(live()) == GOLDEN)
clear_cache_files()
restart()
check("文件不存在：结果回到真值", canon(live()) == GOLDEN)

# 4.2 schema 与顶层形状（按类分别校验：坏一份只作废一份）
mutate_kind_file("snapshot", lambda d: d.update({"schema": 6}),
                 "schema 版本不符（比当前新）")
mutate_kind_file("snapshot", lambda d: d.update({"schema": 4}),
                 "旧 schema（v4 的价格指纹是旧形状，条目结构相同也不放行）")
mutate_kind_file("snapshot", lambda d: d.update({"schema": 3}),
                 "旧 schema（v3 单文件布局，条目结构相同也不放行）")
mutate_kind_file("snapshot", lambda d: d.update({"schema": 2}),
                 "旧 schema（v2 没有按日分桶）")
mutate_kind_file("snapshot", lambda d: d.update({"schema": True}),
                 "schema 写成 true（Python 里 True == 1）")
mutate_kind_file("snapshot", lambda d: d.pop("schema"), "schema 缺失")
mutate_kind_file("snapshot", lambda d: d.update({"schema": "5"}),
                 "schema 写成字符串")
mutate_kind_file("snapshot", lambda d: d.update({"kind": "analytics"}),
                 "kind 标签与文件名不符")
mutate_kind_file("snapshot", lambda d: d.pop("kind"), "kind 标签缺失")
mutate_kind_file("snapshot", lambda d: d.update({"entries": {}}),
                 "entries 不是列表")
mutate_kind_file("snapshot", lambda d: d.pop("entries"), "entries 缺失")

# 4.3 位置：offset / key / tail
mutate_cache(lambda d: _entry(d, "snapshot").update({"offset": 10 ** 9}),
             "offset 超出文件长度")
mutate_cache(lambda d: _entry(d, "snapshot").update({"offset": -1}),
             "offset 为负")
mutate_cache(lambda d: _entry(d, "snapshot").update(
    {"offset": float(_entry(d, "snapshot")["offset"])}),
    "offset 写成浮点数（类型不对）")
mutate_cache(lambda d: _entry(d, "snapshot").update(
    {"offset": _entry(d, "snapshot")["offset"] + 1}),
    "offset 挪了一字节、tail 还是旧的")
mutate_cache(lambda d: _entry(d, "snapshot")["key"].__setitem__(2, 987654321),
             "key 里的 inode 对不上")
mutate_cache(lambda d: _entry(d, "snapshot")["key"].__setitem__(1, 987654321),
             "key 里的 dev 对不上")
mutate_cache(lambda d: _entry(d, "snapshot").update({"key": [1, 2]}),
             "key 长度不对")
mutate_cache(lambda d: _entry(d, "snapshot").update({"tail": "zz"}),
             "tail 不是合法十六进制")
mutate_cache(lambda d: _entry(d, "snapshot").update({"tail": 12345}),
             "tail 不是字符串")
mutate_cache(lambda d: _entry(d, "snapshot").update({"tail": ""}),
             "tail 为空串（签名对不上）")
mutate_cache(lambda d: _entry(d, "by_account").update({"offset": 10 ** 9}),
             "by_account 的 offset 超出文件长度", kind="by_account")
mutate_cache(lambda d: _entry(d, "analytics").update({"offset": 10 ** 9}),
             "analytics 的 offset 超出文件长度", kind="analytics")

# 4.4 载荷形状（手工改坏字段类型）
mutate_cache(lambda d: _entry(d, "snapshot")["snap"].update({"requests": "很多"}),
             "snapshot 的数字字段被写成字符串")
mutate_cache(lambda d: _entry(d, "snapshot")["snap"].pop("by_model"),
             "snapshot 少了 by_model")
mutate_cache(lambda d: _entry(d, "snapshot")["snap"].update({"by_model": []}),
             "snapshot 的 by_model 不是对象")
mutate_cache(lambda d: _entry(d, "snapshot")["snap"].update({"cost_missing": 7}),
             "cost_missing 不是对象")
mutate_cache(lambda d: list(_entry(d, "snapshot")["snap"]["by_model"].values())[0]
             .update({"accounts": "x"}),
             "by_model 桶的 accounts 不是对象")
mutate_cache(lambda d: _entry(d, "snapshot")["snap"].pop("ttft_ms_sum"),
             "snapshot 少了折叠要累加的字段")
mutate_cache(lambda d: _entry(d, "by_account")["buckets"].update({"u1": "x"}),
             "by_account 的桶不是对象", kind="by_account")
mutate_cache(lambda d: _entry(d, "by_account")["buckets"]["u1"].pop("models"),
             "by_account 的桶少了 models", kind="by_account")
mutate_cache(lambda d: _entry(d, "analytics")["maps"].pop("keys"),
             "analytics 少了 keys", kind="analytics")
mutate_cache(lambda d: _entry(d, "analytics")["maps"]["summary"].update(
    {"requests": None}),
    "analytics summary 的字段类型不对", kind="analytics")
mutate_cache(lambda d: _entry(d, "analytics")["maps"]["keys"]["__before_keys__"]
             .update({"last_at": "昨天"}),
             "analytics 的 last_at 不是数字", kind="analytics")
mutate_cache(lambda d: _entry(d, "analytics")["maps"]["models"].update({"m-a": 5}),
             "analytics 的模型条目不是对象", kind="analytics")

# 4.5 realm 字段缺失/写错（缺失的条目绝不能被 realm=None 匹配走）
mutate_cache(lambda d: _entry(d, "snapshot", None).pop("realm"),
             "条目没有 realm 字段")
mutate_cache(lambda d: _entry(d, "snapshot", None).update({"realm": 5}),
             "realm 字段类型不对")

# 4.6 采纳：剪枝线摘掉旧天的小时表不算损坏（#26）
#
# _prune_day_buckets 的 hours_keep 一段会把保留窗口之外的旧天的小时表摘掉
# （pop 掉键；午夜兜底那天则置 None）。摘掉之后这份状态仍然可用：按日对齐的
# 窗口照旧由日桶自身的 maps 回答，整点窗口本来就按 hours is None 退回整段
# 扫描。曾经 analytics 的日桶判据要求 hours 必须是 dict，于是「只要还在折新行，
# 剪枝线天天摘、整份状态天天被判废」，每次进程启动都要付一次全量重折（44MB
# 副本上实测 ~3s）。下面反过来钉：这样的状态**必须被采纳**。


def strip_day_hours(entry, mode="pop"):
    """模拟剪枝线：把日桶的 hours 子表摘掉。返回摘掉的天数。"""
    days = entry.get("days") or {}
    stripped = 0
    for bucket in days.values():
        if not isinstance(bucket, dict) or "hours" not in bucket:
            continue
        if mode == "pop":
            bucket.pop("hours", None)
        else:
            bucket["hours"] = None
        stripped += 1
    return stripped


def accept_cache(mutate, label, kind="analytics", realm=None):
    """加标记 + 变异后加载——标记必须仍出现在结果里（= 状态被采纳）。

    与 mutate_cache 相反：那边钉「坏状态必须被拒」，这边钉「合法状态不许被
    误拒」。被误拒时走全量重折，标记消失、结果等于真值，用例当场失败。

    每轮先清掉盘上的 checkpoint 从干净的一份重来：与降级用例不同，这里标记
    会被真的采用，采用后的状态可能落回盘上，不清就会把 +1000 带进后面的用例。
    """
    clear_cache_files()
    make_checkpoint()
    data = cache_data()
    poison(data, kind, realm)
    mutate(data)
    save_cache(data)
    cache_env_nowrite()
    restart()
    got, err = live_canon()
    check(label, got is not None and got != GOLDEN,
          err or "状态被误拒（标记没出现，说明整份重折了）")


_state = {}
accept_cache(lambda d: _state.update(
    n=strip_day_hours(_entry(d, "analytics", None))),
    "旧天没有 hours 键的 analytics 状态仍被采纳（#26）")
check("用例有实义：确实摘掉了至少一个日桶的 hours 键",
      _state.get("n", 0) > 0, "夹具里没有带 hours 的日桶")

_state = {}
accept_cache(lambda d: _state.update(
    n=strip_day_hours(_entry(d, "analytics", None), "none")),
    "旧天的 hours 为 null 的 analytics 状态仍被采纳（#26）")
check("用例有实义：确实把至少一个日桶的 hours 置成了 null",
      _state.get("n", 0) > 0, "夹具里没有带 hours 的日桶")

# 收尾：把带标记的状态从盘上清掉、重建一份干净的 checkpoint，免得 +1000
# 顺着盘上的文件漏进后面的指纹用例。
clear_cache_files()
make_checkpoint()

# 4.7 指纹
mutate_cache(lambda d: _entry(d, "snapshot").update({"pricing": ["on", "x", "y", "z"]}),
             "价格指纹被改（旧形状的列表）")
mutate_cache(lambda d: _entry(d, "snapshot").update({"pricing": None}),
             "价格指纹缺失")
mutate_cache(lambda d: _entry(d, "snapshot")["pricing"].update({"builtin": "0" * 40}),
             "用到的价：内置价目表摘要被改")
mutate_cache(lambda d: _entry(d, "snapshot")["pricing"].update({"timeline_models": []}),
             "用到的价：时间线依赖被抹掉（投影摘要对不上）")
mutate_cache(lambda d: _entry(d, "snapshot").update({"realm_inputs": ["cn", []]}),
             "realm 指纹被改")
mutate_cache(lambda d: _entry(d, "analytics").update({"pricing": ["off"]}),
             "analytics 的价格指纹被改", kind="analytics")
mutate_cache(lambda d: _entry(d, "analytics")["pricing"].update({"refs": {"ghost": "0" * 40}}),
             "analytics 的用到的价：多出一个查不到的 pid", kind="analytics")
mutate_cache(lambda d: _entry(d, "analytics").update({"realm_inputs": ["cn", []]}),
             "analytics 的 realm 指纹被改", kind="analytics")

# 4.8 真实变化：价格表 / realm 归属
make_checkpoint()
write_pricing({**PRICING, "meta": {"usd_cny": 7.4}, "models": {
    **PRICING["models"],
    "m-a": {"unit": 1000000, "currency": "CNY",
            "flat": {"input_cache_hit": 0.9, "input_cache_miss": 1.8,
                     "output": 3.6}},
}})
cache_env_off()
restart()
GOLDEN_PRICE = canon(live())
check("价格表变了：真值确实变了", GOLDEN_PRICE != GOLDEN)
cache_env_nowrite()
restart()
got, err = live_canon()
check("价格表变了：checkpoint 作废、结果按新价重算", got == GOLDEN_PRICE,
      err or first_diff(GOLDEN_PRICE, got or ""))
check("价格表变了：带指纹的两份确实没有被采用",
      "snapshot" not in U._usage_cache_checkpointed
      and "analytics" not in U._usage_cache_checkpointed,
      U._usage_cache_checkpointed)

write_pricing(PRICING)
make_checkpoint()
OLD_REALM = P.CURRENT_REALM
P.CURRENT_REALM = "cn"
cache_env_off()
restart()
GOLDEN_REALM = canon(live())
check("CURRENT_REALM 变了：真值确实变了", GOLDEN_REALM != GOLDEN)
cache_env_nowrite()
restart()
got, err = live_canon()
check("CURRENT_REALM 变了：checkpoint 作废", got == GOLDEN_REALM,
      err or first_diff(GOLDEN_REALM, got or ""))
check("CURRENT_REALM 变了：带指纹的两份确实没有被采用",
      "snapshot" not in U._usage_cache_checkpointed
      and "analytics" not in U._usage_cache_checkpointed,
      U._usage_cache_checkpointed)
P.CURRENT_REALM = OLD_REALM

# 4.8 日志被截断 / 截断后重写 / 被替换
make_checkpoint()
io.open(USAGE_LOG, "w").close()          # copytruncate：截到 0
cache_env_off()
restart()
EMPTY = canon(live())
cache_env_nowrite()
restart()
got, err = live_canon()
check("日志被截到 0：offset 落在文件外，走全量（结果为空）", got == EMPTY,
      err or first_diff(EMPTY, got or ""))
check("日志被截到 0：确实没有被采用", not U._usage_cache_checkpointed)

write_log(ROWS)
make_checkpoint()
cache_size = os.path.getsize(USAGE_LOG)
text = io.open(USAGE_LOG, encoding="utf-8").read()
needle = '"total_tokens": %d' % (123 + 45)      # 最后一行的值
pos = text.rfind(needle)
check("变异点落在文件尾部（tail 签名看得到）",
      pos >= 0 and len(text) - pos < 64, (pos, len(text)))
mutated = text[:pos] + '"total_tokens": %d' % (268) + text[pos + len(needle):]
io.open(USAGE_LOG, "w", encoding="utf-8", newline="\n").write(mutated)
check("变异保持了日志字节数不变（size 校验对它是瞎的）",
      os.path.getsize(USAGE_LOG) == cache_size,
      (os.path.getsize(USAGE_LOG), cache_size))
cache_env_off()
restart()
REWRITTEN = canon(live())
check("同尺寸重写确实改变了聚合结果（否则这个用例测不出东西）",
      REWRITTEN != GOLDEN)
cache_env_nowrite()
restart()
got, err = live_canon()
check("同尺寸重写：tail 签名挡住 checkpoint，结果按新内容重算",
      got == REWRITTEN, err or first_diff(REWRITTEN, got or ""))
check("同尺寸重写：确实没有被采用", not U._usage_cache_checkpointed)

write_log(ROWS)
make_checkpoint()
# 整个文件被换掉（新文件对象）：dev/ino 变了，或内容一致——接受或拒绝都
# 允许，结果必须永远正确。
os.unlink(USAGE_LOG)
write_log(ROWS)
cache_env_nowrite()
restart()
got, err = live_canon()
check("日志文件被替换：结果正确（接受或拒绝都行）", got == GOLDEN,
      err or first_diff(GOLDEN, got or ""))

# 4.9 日志整个消失
make_checkpoint()
os.unlink(USAGE_LOG)
cache_env_off()
restart()
NO_LOG = canon(live())
cache_env_nowrite()
restart()
got, err = live_canon()
check("日志不存在：checkpoint 作废，结果为空", got == NO_LOG,
      err or first_diff(NO_LOG, got or ""))
check("日志不存在：确实没有被采用", not U._usage_cache_checkpointed)
write_log(ROWS)

print()
print("[5] 目录不可写 / 被占位：不能崩，结果正确")
cache_env_force()
restart()
real_kind_path = U._usage_cache_kind_path
real_path = U._usage_cache_path
missing_dir = os.path.join(_TMP, "no-such-dir")


def _missing_kind_path(kind):
    return os.path.join(missing_dir, "usage-aggregate-cache-%s.json" % kind)


U._usage_cache_kind_path = _missing_kind_path
U._usage_cache_path = lambda: os.path.join(missing_dir,
                                           "usage-aggregate-cache.json")
try:
    got, err = live_canon()
    check("写盘路径不可用：不抛错、结果正确", got == GOLDEN,
          err or first_diff(GOLDEN, got or ""))
    check("写盘路径不可用：没有创建任何文件", not os.path.exists(missing_dir))
finally:
    U._usage_cache_kind_path = real_kind_path
    U._usage_cache_path = real_path
# 读路径被目录占位（open 会抛 OSError，必须被吞掉）
make_checkpoint()
clear_cache_files()
os.makedirs(KIND_FILES["snapshot"])
os.makedirs(KIND_FILES["analytics"])
try:
    cache_env_force()
    restart()
    got, err = live_canon()
    check("读盘路径被目录占位：不抛错、结果正确", got == GOLDEN,
          err or first_diff(GOLDEN, got or ""))
finally:
    os.rmdir(KIND_FILES["snapshot"])
    os.rmdir(KIND_FILES["analytics"])
make_checkpoint()          # 恢复一份干净 checkpoint

print()
print("[6] 开关：关掉后与 #189 原状一致")
data = cache_data()
_entry(data, "snapshot", None)["snap"]["requests"] += 7
save_cache(data)
cache_env_nowrite()
restart()
poisoned, err = live_canon()
poisoned = json.loads(poisoned) if poisoned else {}
check("打开开关：手工改过的 checkpoint 会被采用（证明加载路径是活的）",
      poisoned.get("snap", {}).get("requests")
      == GOLDEN_OBJ["snap"]["requests"] + 7,
      (poisoned.get("snap", {}).get("requests"), GOLDEN_OBJ["snap"]["requests"]))
cache_env_off()
restart()
got, err = live_canon()
got = json.loads(got) if got else {}
check("关掉开关：被改过的 checkpoint 完全不被读，结果回到真值",
      got.get("snap", {}).get("requests") == GOLDEN_OBJ["snap"]["requests"],
      (got.get("snap", {}).get("requests"), GOLDEN_OBJ["snap"]["requests"]))
check("关掉开关：也不会写盘",
      not [n for n in os.listdir(USAGE_DIR) if n.endswith(".tmp")])

print()
print("[7] 节流：默认阈值下连续刷新不写文件")
clear_cache_files()
cache_env(enabled=1)                     # 默认 1MB / 900s
restart()
live()
check("默认阈值 + 首折（不到 1MB）：不写", no_cache_files())
for _ in range(3):
    live()
check("默认阈值下连续刷新：一直不写", no_cache_files())
cache_env(enabled=1, min_bytes=100, min_seconds=10 ** 9)
restart()
live()
check("推进超过 100 字节：写一次", bool(cache_files()))
clear_cache_files()
live()
check("没有新字节：不再写（文件被删掉也不补写）", no_cache_files())
cache_env(enabled=1, min_bytes=10 ** 9, min_seconds=0)
restart()
live()
check("计时器到点 + 从零折满（确有推进）：写一次", bool(cache_files()))
check("写出来的还是完整的 checkpoint（每份自带 schema 与 kind 标签）",
      all(kind_file_data(k)["schema"] == U._USAGE_CACHE_SCHEMA
          and kind_file_data(k)["kind"] == k
          for k in U._USAGE_CACHE_KINDS if os.path.exists(KIND_FILES[k])))

# 空闲不重写（本节的靶子）：进度已对齐到 checkpoint，等满一个时间窗口后再
# 刷新，日志没有新字节 → 不许再写。旧行为：计时器到点就无条件写一份逐字节
# 相同的 checkpoint，面板开着等于每 900s 白写一份 120KB（纯写放大）。
#
# 判据用写盘调用计数，不用文件时间或内容：exFAT 的 mtime 粒度粗，
# 而一份内容相同的重写与「没有写」在文件上也看不出区别。
snapshot_writes = []
real_write_kind = U._usage_cache_write_kind


def counting_write_kind(kind, entries):
    if kind == "snapshot":
        snapshot_writes.append(entries)
    return real_write_kind(kind, entries)


U._usage_cache_write_kind = counting_write_kind
try:
    cache_env(enabled=1, min_bytes=10 ** 9, min_seconds=900)
    U._usage_cache_last_attempt -= 901
    live()
    check("计时器到点 + 无推进：不写（空闲不重写）", snapshot_writes == [])
    # 同一件事从真实调用路径再走一遍：无新字节的 usage_snapshot(range="today")
    # 是面板轮询每几秒就碰一次的路径（日桶半 → _usage_cache_maybe_save）。
    U._usage_cache_last_attempt -= 901
    U.usage_snapshot(range="today", ttl=0)
    check("无新字节的 usage_snapshot(range=today) 不重写 checkpoint",
          snapshot_writes == [])
    # 有推进（不足字节阈值）：计时器分支照常写——修的是空闲重写，不是计时器
    append_log([row(FROZEN - 100, "m-a", "u1", 500, 50)])
    U._usage_cache_last_attempt -= 901
    live()
    check("计时器到点 + 有新字节（不足字节阈值）：写一次",
          len(snapshot_writes) == 1)
    write_log(ROWS)                          # 恢复基线日志，后续用例共用它

    # 加载之后不产生「白写」：采用 checkpoint 会把节流基线对齐到加载的 offset
    cache_env(enabled=1)                     # 默认阈值
    snapshot_writes.clear()
    restart()
    live()
    check("重启加载后（默认阈值）不会重写一份内容相同的文件",
          snapshot_writes == [])
    cache_env_off()
    clear_cache_files()
    restart()
    live()
    check("开关关闭 + 阈值全 0：仍然不写", no_cache_files())
finally:
    U._usage_cache_write_kind = real_write_kind

print()
print("[8] 幂等：同一份 checkpoint 只被采用一次")
cache_env_nowrite()
make_checkpoint()
restart()
first = canon(live())
second = canon(live())
check("同一个进程里重复刷新结果不变", first == second, first_diff(first, second))
restart()
third = canon(live())
check("再模拟一次重启结果仍不变", third == GOLDEN, first_diff(GOLDEN, third))

print()
print("[9] 指纹必须与「账号池加载到哪一步」无关（重启后第一刻就要命中）")
# 这一节用一份自己的账号目录与日志：
#   * 账号文件放在磁盘上（指纹的来源）；
#   * 日志每行都带 realm 字段，归属完全由字段决定——这样「池在不在」不影响
#     任何数字，断言「命中」时不必纠缠归属口径。
REAL_ACCOUNTS = os.path.join(_TMP, "accounts-real")
os.makedirs(REAL_ACCOUNTS, exist_ok=True)
_ACCT_MTIME = [FROZEN - 2000000.0]


def write_account(uid, realm):
    path = os.path.join(REAL_ACCOUNTS, uid + ".json")
    io.open(path, "w", encoding="utf-8").write(json.dumps(
        {"uid": uid, "realm": realm, "accessToken": "", "nickname": uid}))
    # 指纹的磁盘缓存键含 (size, mtime)：两次写入落在同一个 mtime 刻度上会
    # 读不到新内容（Windows 的 time.time() 只有 ~15ms 精度），拨到确定前进
    # 的整数秒上。
    _ACCT_MTIME[0] += 3600.0
    os.utime(path, (_ACCT_MTIME[0], _ACCT_MTIME[0]))


write_account("u-1", "intl")
write_account("u-2", "cn")
SAVED_DIR, SAVED_POOL = P.ACCOUNTS_DIR, P.POOL
P.ACCOUNTS_DIR = REAL_ACCOUNTS
try:
    write_log([
        row(FROZEN - 5000, "m-a", "u-1", 100, 50),
        row(FROZEN - 4900, "m-b", "u-2", 200, 20, realm="cn"),
        row(FROZEN - 4800, "m-a", "u-2", 300, 30, realm="cn"),
    ])
    cache_env_off()
    P.POOL = None
    restart()
    NINE_RAW = live()
    NINE = canon(NINE_RAW)

    # 判据：同一份磁盘 + 同一份配置，三种启动状态算出的指纹必须一模一样。
    fp_none = U._realm_inputs()
    P.POOL = wb_accounts.AccountPool(REAL_ACCOUNTS)      # 建好了，还没 load()
    fp_unloaded = U._realm_inputs()
    P.POOL.load()                                        # 加载完成
    fp_loaded = U._realm_inputs()
    check("指纹与池的加载状态无关（未建/未加载/已加载都相同）",
          fp_none == fp_unloaded == fp_loaded, (fp_none, fp_unloaded, fp_loaded))
    check("指纹就是磁盘上的 uid→realm",
          fp_loaded == (P.CURRENT_REALM, (("u-1", "intl"), ("u-2", "cn"))), fp_loaded)

    # 先写一份 checkpoint（池就绪），再用「池未就绪」的状态加载：必须命中。
    check("池就绪时写盘成功", make_checkpoint())
    stored = cache_data()
    check("落盘的 realm 指纹就是磁盘那份",
          _entry(stored, "snapshot", None)["realm_inputs"]
          == U._realm_inputs_key(fp_loaded),
          _entry(stored, "snapshot", None)["realm_inputs"])
    P.POOL = wb_accounts.AccountPool(REAL_ACCOUNTS)      # 重启：池还没 load()
    cache_env_nowrite()
    restart()
    got, err = live_canon()
    check("池未就绪的进程仍然命中 checkpoint（含带 realm 指纹的两份）",
          adopted_all(U._usage_cache_checkpointed), U._usage_cache_checkpointed)
    check("池未就绪时的结果与冷折叠一致", got == NINE, err or first_diff(NINE, got or ""))
    # 池加载完之后：载荷里的 accounts_map/account 由活池派生，数字部分必须仍
    # 然与全量折叠一致（这一段就是「运行 10 分钟后」的那一态）。
    P.POOL.load()
    cache_env_off()
    restart()
    NINE_LOADED = canon(live())
    cache_env_nowrite()
    restart()
    got, err = live_canon()
    check("池加载完之后同样命中且一致", got == NINE_LOADED,
          err or first_diff(NINE_LOADED, got or ""))

    # 磁盘变了（realm 改了）：指纹必须跟着变，且池没跟上时不许写盘。
    write_account("u-2", "intl")
    check("账号文件改了，指纹跟着变",
          U._realm_inputs() != fp_loaded, U._realm_inputs())
    check("池还停在旧归属（磁盘与池不一致）",
          not U._realm_fold_reproducible(U._account_realm_map()))
    clear_cache_files()
    cache_env_force()
    restart()
    live()
    written = cache_data()
    check("池没跟上磁盘时 snapshot/analytics 不写（by_account 照写，它不读归属）",
          written["snapshot"] == [] and written["analytics"] == [],
          {k: len(v) for k, v in written.items()})
    write_account("u-2", "cn")          # 改回来
    cache_env_off()
    restart()
    check("改回来之后指纹回到原值", U._realm_inputs() == fp_loaded, U._realm_inputs())

    # 池整个不存在而磁盘上有账号：折叠根本不查账号映射，这种折叠冷进程复现
    # 不了 —— 宁可不信任、不写，等池就绪后再用。
    P.POOL = None
    clear_cache_files()
    cache_env_force()
    restart()
    live()
    written = cache_data()
    check("没有池而磁盘上有账号：snapshot/analytics 不写",
          written["snapshot"] == [] and written["analytics"] == [],
          {k: len(v) for k, v in written.items()})
finally:
    P.ACCOUNTS_DIR = SAVED_DIR
    P.POOL = SAVED_POOL
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
