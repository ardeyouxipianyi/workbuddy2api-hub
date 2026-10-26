"""「用到的价」状态指纹（T15）：收窄判定、作废矩阵、checkpoint 往返。

价表内容一变（哪怕只是新增一个无关模型的价格），带价格指纹的 5 条状态
（snapshot×3 + analytics×2）此前全部判废、各重折 12-15s；但已折行的成本在
设计上就是历史价（带 cost_policy 的行按不可变策略计价，无引用老行由时间线
解析），所以绝大多数重折是白付。T15 把状态指纹从「三张表的对象身份」收窄成
「折叠行真正用到的价」的一份记录（refs / timeline 投影 / timeline_at /
timeline_models / unresolved / backfilled / builtin），判定侧从当前价表重算
同一份记录逐项比对。

这个套件钉住四件事（对应 T15_DESIGN.md §4 的测试矩阵）：

  [1] 对拍：同一份日志（带引用行、无引用行、回填行、内置快照行、错误行、
      未知模型行、坏 JSON 行）在「收窄前判定（每次都判废）」与「收窄后判定」
      下折出的快照/analytics 载荷**逐字节相同**——作废只影响「要不要重折」，
      不影响数值；
  [2] 作废矩阵 ①-⑦：追加无关模型的价格、追加被引用模型的新价、上界之外的
      追加都不判废；时间线前缀被改、被引用策略内容被改、内置价目表变化、
      回填行存在时时间线任意追加都判废；每条都断言「状态是否被判废」（计数
      桩：零重折 / 有重折）与「重折后的输出与冷折逐字节一致」；
  [3] checkpoint 往返：新记录落盘 → 采纳 → 判定一致（零重折）；旧 schema
      （4）的文件与旧形状的 pricing 字段一律被拒 → 一次性重折；
  [4] 计数桩：不作废的用例里一行都没有重折（折叠函数调用数为 0）。

另外钉两条收窄判定的边界（都写进报告）：总开关翻转必须判废；无引用行解析
不到 pid 的模型（时间线里一行都没提到）之后被补了一行也必须判废。

No network: 策略表、时间线、内置价目表与日志全部在临时目录里现造。
"""
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FROZEN = 1791445200.0
T0 = FROZEN - 100000.0          # 日志行的基准时刻

_TMP = tempfile.mkdtemp(prefix="wb-priced-fp-")
USAGE_DIR = os.path.join(_TMP, "usage")
ACCOUNTS_DIR = os.path.join(_TMP, "accounts")
os.makedirs(USAGE_DIR, exist_ok=True)
os.makedirs(ACCOUNTS_DIR, exist_ok=True)
os.environ["WB_PROXY_USAGE_DIR"] = USAGE_DIR
os.environ["ACCOUNTS_DIR"] = ACCOUNTS_DIR

import wb_pricing                                                   # noqa: E402
import wb_proxy as P                                                # noqa: E402
import wb_usage as U  # noqa: E402
import wb_settings                                                  # noqa: E402

wb_pricing.set_data_dir(USAGE_DIR)
wb_pricing.set_settings_dir(ACCOUNTS_DIR)
P.ACCOUNTS_DIR = ACCOUNTS_DIR
USAGE_LOG = P.USAGE_LOG
POLICIES_FILE = wb_pricing.policies_path()
TIMELINE_FILE = wb_pricing.timeline_path()
PRICING_FILE = os.path.join(_TMP, "pricing.json")
os.environ["WB_PRICING_FILE"] = PRICING_FILE
# 快照载荷里带着进程自己的 _usage["started"]：冻结它让各次折叠可比。
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
    """载荷的可比形态：不排序键（键顺序也是输出的一部分）。"""
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
# 环境：文件读写、缓存作废、状态清零
# --------------------------------------------------------------------------
_MTIME = [FROZEN - 1000000.0]


def _bump(path):
    _MTIME[0] += 3600.0
    os.utime(path, (_MTIME[0], _MTIME[0]))


def forget_file_keys():
    """让下一次 load_pricing/policies/timeline 重新 stat 文件。

    _file_key 有 1 秒 TTL（本进程之外写下的变更最迟 1 秒内可见），测试不等
    这一秒，直接清掉缓存——语义与「等过了 TTL」一致。
    """
    wb_pricing._file_key_cache.clear()
    wb_pricing._file_cache.update({"key": None, "data": None})


def write_text(path, text):
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    _bump(path)
    forget_file_keys()


def write_pricing(doc):
    write_text(PRICING_FILE, json.dumps(doc, ensure_ascii=False))


def write_log(rows):
    """写 usage.jsonl；rows 里的字符串按原样写（坏行用得上）。"""
    lines = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
             for r in rows]
    write_text(USAGE_LOG, "\n".join(lines) + "\n")


def register(doc, at):
    """把一份价目表登记进策略表与时间线（与定价刷新器同一条路）。"""
    _fresh, _changed, assignment = wb_pricing.record_policies(doc, at=at)
    forget_file_keys()
    return assignment


def rewrite_timeline(rows):
    write_text(TIMELINE_FILE,
               "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def rewrite_policies(rows):
    write_text(POLICIES_FILE,
               "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def clear_states():
    """清掉五个聚合的状态、TTL 缓存与 checkpoint 读记忆（模拟一次重启）。"""
    U._usage_snap_state.clear()
    U._snap_cache.clear()
    U._analytics_state.clear()
    U._analytics_cache.clear()
    U._series_cache.clear()
    U._series_state.clear()
    U._perf_cache.clear()
    U._count_cache.clear()
    U._byacct_cache.update({"at": 0.0, "data": None})
    U._byacct_state.update({"buckets": None, "offset": 0, "key": None,
                            "tail": b""})
    U._row_day_memo.clear()
    U._row_cost_memo.clear()
    U._usage_cache_forget()
    U._usage_cache_progress.clear()
    U._usage_cache_checkpointed.clear()


def clear_cache_files():
    for kind in U._USAGE_CACHE_KINDS:
        try:
            os.unlink(U._usage_cache_kind_path(kind))
        except OSError:
            pass


def fresh_scenario():
    """下一个用例的干净起点：文件删掉、状态清掉、checkpoint 不留。"""
    for path in (PRICING_FILE, POLICIES_FILE, TIMELINE_FILE):
        try:
            os.unlink(path)
        except OSError:
            pass
    write_text(USAGE_LOG, "")
    clear_cache_files()
    clear_states()
    forget_file_keys()


# --------------------------------------------------------------------------
# 数据：覆盖各条定价分支的小日志
# --------------------------------------------------------------------------
BUILTIN_DOC = {
    "meta": {"usd_cny": 7.1},
    "models": {
        "m-builtin": {"unit": 1000000, "currency": "CNY",
                      "flat": {"input_cache_hit": 0.5,
                               "input_cache_miss": 1.0, "output": 2.0}},
    },
}


def price_doc(models, usd=7.1):
    out = {}
    for name, rate in models.items():
        out[name] = {"unit": 1000000, "currency": "USD", "or_id": "v/" + name,
                     "flat": {"input_cache_hit": rate[0],
                              "input_cache_miss": rate[1], "output": rate[2]}}
    return {"meta": {"usd_cny": usd}, "models": out}


RATE_REF = (1.0, 2.0, 4.0)
RATE_OLD = (0.5, 1.5, 3.0)
RATE_BF = (0.25, 0.75, 1.5)
RATE_NEW = (9.0, 18.0, 36.0)          # 「同 id 换价」用例用


def row(at, model, account="u1", prompt=1000, completion=200,
        outcome="completed", policy=None, key="k1"):
    r = {"at": at, "model": model, "stream": True, "outcome": outcome,
         "elapsed_ms": 1000, "ttft_ms": 300, "gen_ms": 700,
         "prompt_tokens": prompt, "completion_tokens": completion,
         "reasoning_tokens": 0, "cached_tokens": 0,
         "account": account, "key": key, "realm": "intl",
         "tokens_per_sec": 90.0, "total_tokens": prompt + completion}
    if outcome != "completed":
        r["error"] = "boom"
    if policy:
        r["cost_policy"] = policy
    return r


def base_log(pid_ref, bf_backfilled=False):
    """一份带各条分支的日志。

    行序即折序：带引用行（m-ref）、无引用老行（m-old，时间线解析）、回填行
    （m-bf，只在后面的时间线行里出现过）、内置快照行（m-builtin，从未进过
    时间线）、失败行（无 token → 成本 0）、未知模型行（没价 → cost_missing）、
    中止行（整行跳过）、坏 JSON 行（解析失败跳过）。
    """
    rows = [
        row(T0 + 100, "m-ref", "u1", 4000, 1000, policy=pid_ref),
        row(T0 + 200, "m-old", "u1", 3000, 600),
        row(T0 + 300, "m-old", "u2", 2000, 400),
        row(T0 + 400, "m-bf", "u2", 1500, 300),
        row(T0 + 500, "m-builtin", "u1", 1000, 200),
        row(T0 + 600, "m-old", "u1", 0, 0, outcome="failed"),
        row(T0 + 700, "m-no-such", "u2", 700, 100),
        row(T0 + 800, "m-ref", "u1", 900, 100, policy=pid_ref,
            outcome="client_aborted"),
        "{not json at all",
    ]
    return rows


def build_tables(bf_at=T0 + 5000, include_bf=True):
    """策略表 + 时间线 + 内置价目表；返回 (pid_ref, pid_old, pid_bf)。

    第一条时间线行在 T0-5000（m-ref / m-old）；m-bf 只在 T0+5000 那一行出现
    ——无引用行在它之前，解析必然回填（backfilled=True 的依赖）。
    """
    write_pricing(BUILTIN_DOC)
    doc1 = price_doc({"m-ref": RATE_REF, "m-old": RATE_OLD})
    assignment = register(doc1, T0 - 5000)
    pid_ref, pid_old = assignment["m-ref"], assignment["m-old"]
    pid_bf = None
    if include_bf:
        assignment2 = register(price_doc({"m-bf": RATE_BF}), bf_at)
        pid_bf = assignment2["m-bf"]
    return pid_ref, pid_old, pid_bf


# --------------------------------------------------------------------------
# 视图与计数桩
# --------------------------------------------------------------------------
def snap_payload():
    """快照载荷（三个 realm 视图都覆盖；都是无窗口的 all-time 折叠）。"""
    return {"snap": U._usage_snapshot_uncached(),
            "snap_intl": U._usage_snapshot_uncached(realm="intl"),
            "snap_cn": U._usage_snapshot_uncached(realm="cn")}


def analytics_payload():
    return {"analytics": U._compute_usage_analytics_uncached(),
            "analytics_intl": U._compute_usage_analytics_uncached(realm="intl")}


def payload():
    out = snap_payload()
    out.update(analytics_payload())
    return out


def cold_payload():
    """冷折叠：状态全清后折一遍（对拍的真值）。"""
    clear_states()
    return payload()


class Counters(object):
    """三处计数桩：快照折叠 / analytics 折叠 / 取价。

    「判废」= 折叠从文件头重来：折叠函数按行被调用、取价每行一次；「没判废」
    = 状态还在文件尾，一行都不折（折叠函数调用数为 0）。取价计数另外钉住
    「价表换代 → 行级记忆整体作废」这件事。
    """

    def __init__(self):
        self.counts = {"fold_snap": 0, "fold_an": 0, "cost": 0}
        self._saved = None

    def __enter__(self):
        real_snap = U._fold_usage_snapshot
        real_an = U._fold_usage_analytics
        real_cost = wb_pricing.cost_for_row
        counts = self.counts

        def fold_snap(*args, **kwargs):
            counts["fold_snap"] += 1
            return real_snap(*args, **kwargs)

        def fold_an(*args, **kwargs):
            counts["fold_an"] += 1
            return real_an(*args, **kwargs)

        def cost(*args, **kwargs):
            counts["cost"] += 1
            return real_cost(*args, **kwargs)

        self._saved = (real_snap, real_an, real_cost)
        U._fold_usage_snapshot = fold_snap
        U._fold_usage_analytics = fold_an
        wb_pricing.cost_for_row = cost
        return self

    def __exit__(self, *exc):
        U._fold_usage_snapshot, U._fold_usage_analytics, wb_pricing.cost_for_row = self._saved
        return False

    def reset(self):
        for key in self.counts:
            self.counts[key] = 0


def state_of(realm=None):
    return U._usage_snap_state[realm]["pricing"]


def refolded(counters):
    counts = counters.counts if isinstance(counters, Counters) else counters
    return counts["fold_snap"] > 0 or counts["fold_an"] > 0


# --------------------------------------------------------------------------
# [1] 对拍：收窄前（每次都判废）与收窄后判定折出的载荷逐字节相同
# --------------------------------------------------------------------------
print()
print("[1] 对拍：收窄前后折出的载荷逐字节相同（作废只影响要不要重折）")
fresh_scenario()
PID_REF, PID_OLD, PID_BF = build_tables()
write_log(base_log(PID_REF))
KEPT = canon(payload())
CHECKPOINT_RECORD = state_of()
check("收窄后判定：记录是七字段的新形状",
      isinstance(CHECKPOINT_RECORD, dict)
      and set(CHECKPOINT_RECORD) == set(U._PRICED_FIELDS), CHECKPOINT_RECORD)
check("记录里 refs 收下了解析出来的 pid（显式引用 + 时间线解析）",
      set(CHECKPOINT_RECORD["refs"]) == {PID_REF, PID_OLD, PID_BF},
      CHECKPOINT_RECORD["refs"])
check("记录里时间线依赖的模型集正确（无引用非零成本行）",
      CHECKPOINT_RECORD["timeline_models"] == ["m-bf", "m-builtin", "m-old"]
      and CHECKPOINT_RECORD["backfilled"] is True,
      CHECKPOINT_RECORD)
check("记录里 unresolved 只收「整条时间线都没提到」的模型",
      CHECKPOINT_RECORD["unresolved"] == ["m-builtin"],
      CHECKPOINT_RECORD["unresolved"])
check("记录里 builtin 有内置价目表的摘要",
      isinstance(CHECKPOINT_RECORD["builtin"], str)
      and CHECKPOINT_RECORD["timeline"] is not None, CHECKPOINT_RECORD)

# 收窄前的判定：任何一次请求都当价表变了（最激进的判废），数值必须一字不差。
_real_used = U._pricing_used_unchanged
U._pricing_used_unchanged = lambda record, pricing: False
try:
    REFOLDED = canon(payload())
finally:
    U._pricing_used_unchanged = _real_used
check("每次都判废（重折）时折出的载荷与保留状态时逐字节相同",
      KEPT == REFOLDED, first_diff(KEPT, REFOLDED))

# 判废真的发生了（不是桩没生效）：收窄判定下同一份日志再折一遍是零重折。
with Counters() as counters:
    again = canon(payload())
check("收窄后判定：同一份日志再请求一行都不折（零重折）",
      not refolded(counters) and counters.counts["cost"] == 0
      and again == KEPT, (counters.counts, first_diff(KEPT, again)))

# --------------------------------------------------------------------------
# [2] 作废矩阵 ①-⑦
# --------------------------------------------------------------------------
print()
print("[2] 作废矩阵：用到的价变没变 = 状态该不该判废")


def case(name, mutate, expect_refold, note=""):
    """一个矩阵用例：冷折一遍把状态热起来 → 变异 → 再请求 → 断言判废与数值。

    expect_refold=True 时要求这次请求发生了重折（折叠函数被调用）；False 时
    要求零重折。两种情况下载荷都必须与「按新价表冷折」逐字节一致。
    """
    clear_states()               # 起点干净，不受上一个个案的状态影响
    with Counters() as counters:
        canon(payload())         # 冷折一次，把状态热起来
        counters.reset()
        mutate()
        got = canon(payload())
        counts = dict(counters.counts)
    did = counts["fold_snap"] > 0 or counts["fold_an"] > 0
    check("%s：%s" % (name, "判废重折" if expect_refold else "不判废"),
          did == expect_refold, (counts, note))
    want = canon(cold_payload())
    check("%s：输出与冷折逐字节一致" % name, got == want,
          first_diff(want, got))


# ① 追加一个无关模型的策略 + 时间线行 → 不作废（T15 的核心收益）
fresh_scenario()
PID_REF, PID_OLD, PID_BF = build_tables(bf_at=T0 + 9000, include_bf=False)
write_log(base_log(PID_REF))
case("① 追加无关模型的价（策略 + 时间线行）",
     lambda: register(price_doc({"m-unrelated": RATE_NEW}), T0 + 6000),
     False)

# ② 追加一个被引用模型的新价（新 pid + 时间线行）→ 不作废（历史价口径）
fresh_scenario()
PID_REF, PID_OLD, PID_BF = build_tables(bf_at=T0 + 9000, include_bf=False)
write_log(base_log(PID_REF))
case("② 追加被引用模型的新价（新 pid + 时间线行）",
     lambda: register(price_doc({"m-ref": RATE_NEW}), T0 + 7000),
     False)

# ③ 时间线上界之外的追加（无关模型，at > timeline_at）→ 不作废
fresh_scenario()
PID_REF, PID_OLD, PID_BF = build_tables(bf_at=T0 + 9000, include_bf=False)
write_log(base_log(PID_REF))
case("③ 上界之外的时间线追加（at > timeline_at）",
     lambda: register(price_doc({"m-later": RATE_NEW}), T0 + 20000),
     False)

# ④ 改动无引用行依赖的时间线前缀（插入一条更早的行）→ 判废
fresh_scenario()
PID_REF, PID_OLD, PID_BF = build_tables(bf_at=T0 + 9000, include_bf=False)
write_log(base_log(PID_REF))
_tl = [json.loads(line) for line in
       io.open(TIMELINE_FILE, encoding="utf-8") if line.strip()]
_tl.insert(1, {"at": T0 + 150, "policies": {"m-old": PID_REF}})
case("④ 改动时间线前缀（给 m-old 插一条更早的行使解析换价）",
     lambda: rewrite_timeline(_tl), True)

# ⑤ 改一个被引用策略的内容（同 id 换价）→ 判废
fresh_scenario()
PID_REF, PID_OLD, PID_BF = build_tables(bf_at=T0 + 9000, include_bf=False)
write_log(base_log(PID_REF))
_pols = [json.loads(line) for line in
         io.open(POLICIES_FILE, encoding="utf-8") if line.strip()]
for _row in _pols:
    if _row.get("id") == PID_REF:
        _row["flat"] = {"input_cache_hit": RATE_NEW[0],
                        "input_cache_miss": RATE_NEW[1], "output": RATE_NEW[2]}
case("⑤ 被引用策略的内容被改（同 id 换价）",
     lambda: rewrite_policies(_pols), True)

# ⑥ 内置价目表变化（用到 builtin 的日志）→ 判废
fresh_scenario()
PID_REF, PID_OLD, PID_BF = build_tables(bf_at=T0 + 9000, include_bf=False)
write_log(base_log(PID_REF))
case("⑥ 内置价目表被改（用到 builtin 的行跟着变）",
     lambda: write_pricing({"meta": {"usd_cny": 7.4},
                            "models": {"m-builtin": {
                                "unit": 1000000, "currency": "CNY",
                                "flat": {"input_cache_hit": 0.9,
                                         "input_cache_miss": 1.8,
                                         "output": 3.6}}}}),
     True)

# ⑦ backfilled 为真时：时间线任意追加 → 判废（保守）
fresh_scenario()
PID_REF, PID_OLD, PID_BF = build_tables(bf_at=T0 + 5000, include_bf=True)
write_log(base_log(PID_REF))
payload()                            # 先折一遍，状态里才有记录
check("⑦ 的起点：记录里 backfilled 为真（m-bf 靠后面的行回填）",
      state_of()["backfilled"] is True, state_of())
case("⑦ 回填依赖下时间线任意追加",
     lambda: register(price_doc({"m-unrelated": RATE_NEW}), T0 + 6000),
     True)

# 收窄判定的边界：总开关翻转、解析不到 pid 的模型被补行
print()
print("[2b] 边界：开关翻转与「没解析出 pid」的模型补行")

fresh_scenario()
PID_REF, PID_OLD, PID_BF = build_tables(bf_at=T0 + 9000, include_bf=False)
write_log(base_log(PID_REF))
with Counters() as counters:
    base = canon(payload())
    counters.reset()
    wb_settings.set_pricing_enabled(ACCOUNTS_DIR, False)
    off = canon(payload())
    off_counts = dict(counters.counts)
    counters.reset()
    wb_settings.set_pricing_enabled(ACCOUNTS_DIR, True)
    back = canon(payload())
    on_counts = dict(counters.counts)
check("总开关关掉：判废重折（成本全部停摆）",
      off_counts["fold_snap"] > 0 and off != base, (off_counts,))
check("总开关再打开：又判废重折，载荷回到原值",
      on_counts["fold_snap"] > 0 and back == base, (on_counts,))

# 内置快照行（m-builtin）整条时间线都没提到：之后补一行必须判废
fresh_scenario()
PID_REF, PID_OLD, PID_BF = build_tables(bf_at=T0 + 9000, include_bf=False)
write_log(base_log(PID_REF))
payload()                            # 先折一遍，状态里才有记录
check("起点：m-builtin 记在 unresolved 里（时间线一行都没提到它）",
      "m-builtin" in state_of()["unresolved"], state_of()["unresolved"])
with Counters() as counters:
    canon(payload())
    counters.reset()
    register(price_doc({"m-builtin": RATE_NEW}), T0 + 8000)
    after = canon(payload())
    counts = dict(counters.counts)
check("给「没解析出 pid」的模型补一行：判废重折",
      counts["fold_snap"] > 0, counts)
check("补行后的输出与冷折逐字节一致", after == canon(cold_payload()),
      first_diff(after, canon(cold_payload())))

# --------------------------------------------------------------------------
# [3] checkpoint 往返与旧格式被拒
# --------------------------------------------------------------------------
print()
print("[3] checkpoint：新记录往返一致；旧 schema / 旧形状被拒（一次性重折）")


def cache_env_nowrite():
    os.environ["WB_USAGE_CACHE"] = "1"
    os.environ["WB_USAGE_CACHE_MIN_BYTES"] = "0"
    os.environ["WB_USAGE_CACHE_MIN_SECONDS"] = "0"


def kind_entry(kind, realm=None):
    with io.open(U._usage_cache_kind_path(kind), encoding="utf-8") as fh:
        data = json.load(fh)
    for entry in data["entries"]:
        if entry.get("realm") == realm:
            return data, entry
    raise AssertionError("没有 %s/%r 条目" % (kind, realm))


def save_kind(kind, data):
    with io.open(U._usage_cache_kind_path(kind), "w", encoding="utf-8",
                 newline="\n") as fh:
        fh.write(json.dumps(data, ensure_ascii=False))


fresh_scenario()
PID_REF, PID_OLD, PID_BF = build_tables(bf_at=T0 + 9000, include_bf=False)
write_log(base_log(PID_REF))
cache_env_nowrite()
GOLDEN = canon(payload())
_snap_file, _snap_entry = kind_entry("snapshot", None)
_an_file, _an_entry = kind_entry("analytics", None)
check("落盘的 snapshot 条目带新形状的 pricing 记录",
      U._pricing_record_ok(_snap_entry["pricing"]), _snap_entry["pricing"])
check("落盘的 analytics 条目带新形状的 pricing 记录",
      U._pricing_record_ok(_an_entry["pricing"]), _an_entry["pricing"])
check("新记录随状态一起过判定（不改磁盘也能通过）",
      U._pricing_used_unchanged(
          _snap_entry["pricing"], U._pricing_inputs(True)), None)
check("顶层 schema 是 5", _snap_file.get("schema") == 5, _snap_file.get("schema"))
check("记录很小（几十个 pid + 几个模型名，序列化后 < 1KB）",
      len(json.dumps(_snap_entry["pricing"], ensure_ascii=False)) < 1024,
      len(json.dumps(_snap_entry["pricing"], ensure_ascii=False)))

# 往返：模拟重启 → 采纳 → 零重折且载荷一致
clear_states()
with Counters() as counters:
    got = canon(payload())
check("重启后采纳 checkpoint：零重折",
      not refolded(counters) and counters.counts["cost"] == 0,
      counters.counts)
check("采纳后的载荷与冷折逐字节一致", got == GOLDEN, first_diff(GOLDEN, got))
check("五类状态里的 snapshot / analytics 确实被采用",
      U._usage_cache_checkpointed.get("snapshot", 0) > 0
      and U._usage_cache_checkpointed.get("analytics", 0) > 0,
      dict(U._usage_cache_checkpointed))

# 旧形状的 pricing 字段（v4 的 ["on", d1, d2, d3]）→ 拒
_snap_file, _snap_entry = kind_entry("snapshot", None)
_snap_entry["pricing"] = ["on", "x", "y", "z"]
save_kind("snapshot", _snap_file)
clear_states()
with Counters() as counters:
    got = canon(payload())
check("旧形状的 pricing 字段被拒：判废重折",
      counters.counts["fold_snap"] > 0, counters.counts)
check("被拒后的载荷与冷折逐字节一致", got == GOLDEN, first_diff(GOLDEN, got))

# 旧 schema 的文件（4）→ 顶层直接拒
_snap_file["schema"] = 4
save_kind("snapshot", _snap_file)
clear_states()
with Counters() as counters:
    got = canon(payload())
check("schema=4 的旧文件被拒：判废重折",
      counters.counts["fold_snap"] > 0, counters.counts)
check("schema=4 被拒后的载荷与冷折逐字节一致", got == GOLDEN,
      first_diff(GOLDEN, got))

# --------------------------------------------------------------------------
# [4] 计数桩总收口：不作废的用例里一行都没折过
# --------------------------------------------------------------------------
print()
print("[4] 计数桩：不作废的用例（①②③）零重折")
fresh_scenario()
PID_REF, PID_OLD, PID_BF = build_tables(bf_at=T0 + 9000, include_bf=False)
write_log(base_log(PID_REF))
with Counters() as counters:
    canon(payload())                     # 第一次冷折
    counters.reset()
    register(price_doc({"m-unrelated": RATE_NEW}), T0 + 6000)   # ①
    canon(payload())
    register(price_doc({"m-ref": RATE_NEW}), T0 + 7000)         # ②
    canon(payload())
    register(price_doc({"m-later": RATE_NEW}), T0 + 20000)      # ③
    canon(payload())
check("三次价格追加后折叠函数调用数仍为 0（没发生重折）",
      counters.counts["fold_snap"] == 0 and counters.counts["fold_an"] == 0
      and counters.counts["cost"] == 0, counters.counts)

# 追加一行日志（带引用）：只有那一行被折（增量续折，不是重折）
with Counters() as counters:
    with io.open(USAGE_LOG, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row(T0 + 900, "m-ref", "u1", 100, 10,
                                policy=PID_REF), ensure_ascii=False) + "\n")
    grew = canon(payload())
    counts = dict(counters.counts)
check("追加一行日志：只折那一行（三个 realm 的快照各折 1 行、两个 analytics 各 1 行，"
      "定价被行级记忆跨视图共享只算 1 次）",
      counts["fold_snap"] == 3 and counts["fold_an"] == 2
      and counts["cost"] == 1, counts)
check("增量续折后的载荷与冷折逐字节一致", grew == canon(cold_payload()),
      first_diff(canon(cold_payload()), grew))

os.environ.pop("WB_USAGE_CACHE_MIN_BYTES", None)
os.environ.pop("WB_USAGE_CACHE_MIN_SECONDS", None)
shutil.rmtree(_TMP, ignore_errors=True)
print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
