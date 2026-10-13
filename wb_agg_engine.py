# -*- coding: utf-8 -*-
"""可选 C 聚合引擎的接缝：冷窗口快照 + 序列整段扫描。

默认关：只有 `WB_AGG_ENGINE` 取真值（1/on/true/yes）且 `wb_native_agg` 能
加载库时才用引擎；库缺失、开关关着、归属输入对不上、调用失败、返回形状不
对——一律返回 None，调用方照旧跑自己的 Python 全扫。**本模块绝不抛异常**：
它接的是请求路径，加速件不能把网关带崩。

为什么只接这两条路
==================
引擎是「一次全量扫描出结果」的批处理；网关里绝大多数聚合是**增量**的
（all-time 快照、按账号、计数、按天都带 offset/tail 状态与 checkpoint），
拿 C 全量替换反而比 Python 的增量慢。真正「每次请求都要全量重扫」的只有
两条兜底路径：

  * 窗口快照的兜底扫描：日/小时桶切片不成立时（自定义区间、状态冷、
    桶切不出那一刻）——见 wb_proxy._scan_usage_snapshot_window
  * 序列的整段扫描：桶切片不成立时——见 wb_proxy._usage_timeseries_scan

真机 48k 行实测（x86_64 软路由）：这两条 Python 全扫约 13 s / 17 s，
C 引擎 1.0–1.2 s；见 native/README.md。

归属口径的先决条件
==================
引擎按账号目录里每个 `*.json` 的 `realm` 字段做行归属，Python 的
`row_realm()` 还会在没有 `realm` 时从 token 推导（wb_accounts.Account），
并且会跳过没有 uid 的文件。两者对「某个账号属于哪个 realm」必须逐账号一致，
否则 realm 过滤/分组会分叉——所以 `_attribution_inputs_ok()` 先核对：
目录里每个账号文件都有非空 uid 与非空 realm，且 uid 集合与账号池一致。
对不上就回退 Python（宁可不加速）。
"""
import json
import os
import threading

_TRUE = ("1", "on", "true", "yes")
_ENV = "WB_AGG_ENGINE"

# 引擎每次调用都要把整份日志读进来（峰值几十 MB 量级），而这两条接缝在
# 请求线程里跑：并发窗口请求各跑一次会让峰值翻倍。串行化调用把内存钉在
# 「一次一份」上，代价是并发时排队（每次 1 s 量级）。
_CALL_LOCK = threading.Lock()

# 引擎默认归属：与 wb_proxy.row_realm() 的最后一档一致（模型未知时回落 "intl"）。
_DEFAULT_REALM = "intl"


def enabled():
    """开关打开且库可用。绝不抛异常。"""
    try:
        if os.environ.get(_ENV, "").strip().lower() not in _TRUE:
            return False
        import wb_native_agg
        return bool(wb_native_agg.available())
    except Exception:
        return False


def _attribution_inputs_ok(accounts_dir, pool):
    """账号归属输入是否与 Python 侧逐账号一致（见模块头）。绝不抛异常。"""
    try:
        if not accounts_dir or not os.path.isdir(accounts_dir):
            # 没有目录：Python 侧也不该有账号（池为空），否则对不上。
            return pool is None or not pool.accounts
        uids = []
        for name in sorted(os.listdir(accounts_dir)):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(accounts_dir, name), encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                return False
            uid = data.get("uid")
            realm = data.get("realm")
            if not uid or not realm:
                # Python 会跳过没有 uid 的文件、没有 realm 时从 token 推导；
                # 引擎两条都不做，出现这种文件就不加速。
                return False
            uids.append(str(uid))
        pool_uids = sorted(a.uid for a in (pool.accounts if pool else []))
        return sorted(uids) == pool_uids
    except Exception:
        return False


def _pricing_paths(pricing_on):
    """总开关开着时返回 {doc, policies, timeline}；关着返回 None。"""
    import wb_pricing
    if pricing_on is None:
        pricing_on = wb_pricing.pricing_enabled()
    if not pricing_on:
        return None
    doc = wb_pricing._candidate_file()
    policies, timeline = wb_pricing._data_paths()[:2]
    return {
        "pricing": doc,
        "policies": policies if policies and os.path.isfile(policies) else None,
        "timeline": timeline if timeline and os.path.isfile(timeline) else None,
    }


def _run(log, usage_log, sections, accounts_dir, pool, pricing_on, need_pricing=True,
         **params):
    """跑一次引擎，返回解析后的 dict；任何一环不成立返回 None。绝不抛异常。"""
    try:
        if not enabled():
            return None
        if not _attribution_inputs_ok(accounts_dir, pool):
            return None
        request = {
            "log": usage_log,
            "sections": list(sections),
            "realm": "all",
            "default_realm": _DEFAULT_REALM,
            "accounts": accounts_dir,
        }
        if need_pricing:
            pricing = _pricing_paths(pricing_on)
            if pricing is None:
                # 引擎总是算价；总开关关着时 Python 的折叠不算，硬用会多出
                # 成本数字——这条路留给 Python。
                return None
            request.update(pricing)
        import wb_native_agg
        request.update(params)
        with _CALL_LOCK:
            out = wb_native_agg.run(request)
        if not isinstance(out, dict):
            return None
        return out
    except Exception as exc:
        try:
            log("agg engine call failed: %s" % exc)
        except Exception:
            pass
        return None


# 引擎用 %.17g 打浮点：0.0 会打成 `0`，而 Python 那边同一个字段可能是 float
# （`0.0`）。值相同、文本不同就没法声称「逐字节相同」，所以按 Python 的**初始
# 类型**逐字段归一——不是「看着像浮点就转」：
#   * 顶层 credit / cost_cny 与每个桶的 cost_cny：Python 初始成 0.0（float）
#   * 桶的 credit：Python 来自 USAGE_FIELDS 的 0（int），只有真加上小数额度才变
#     float，所以**不能**碰
#   * 序列段的 since/until 与每个桶的 at/credit：Python 初始成 float
def _coerce_float(mapping, key):
    """mapping[key] 若是 int 就转 float（bool 除外，它不是数字字段）。"""
    if isinstance(mapping, dict) and key in mapping:
        value = mapping[key]
        if isinstance(value, int) and not isinstance(value, bool):
            mapping[key] = float(value)


def _normalise_snapshot(snap):
    """把 snapshot 段的浮点字段按 Python 的初始类型转回 float（就地）。"""
    _coerce_float(snap, "credit")
    _coerce_float(snap, "cost_cny")
    for bucket in (snap.get("by_model") or {}).values():
        _coerce_float(bucket, "cost_cny")
    for realms in (snap.get("by_model_realm") or {}).values():
        for bucket in (realms or {}).values():
            _coerce_float(bucket, "cost_cny")
    for realms in (snap.get("by_model_acct") or {}).values():
        for accts in (realms or {}).values():
            for bucket in (accts or {}).values():
                _coerce_float(bucket, "cost_cny")


def _normalise_series(data):
    """把序列段的浮点字段按 Python 的初始类型转回 float（就地）。"""
    _coerce_float(data, "since")
    _coerce_float(data, "until")
    for bucket in data.get("series") or ():
        _coerce_float(bucket, "at")
        _coerce_float(bucket, "credit")
    for entry in data.get("credits") or ():
        _coerce_float(entry, "at")


def snapshot_window(log, usage_log, accounts_dir, realm, since, until,
                    pool=None, pricing_on=None):
    """窗口快照的兜底扫描（wb_proxy._scan_usage_snapshot_window 的替身）。

    返回引擎 snapshot 段（13 个累计键）的 dict，或 None。调用方按
    `_empty_stats()` 打底再覆盖，形状就与 Python 的折叠逐键一致。
    """
    out = _run(log, usage_log, ["snapshot"], accounts_dir, pool, pricing_on,
               realm=realm or "all",
               since=float(since or 0.0), until=float(until or 1e18),
               has_since=bool(since), has_until=bool(until))
    if out is None:
        return None
    snap = out.get("snapshot")
    if not isinstance(snap, dict) or "requests" not in snap:
        return None
    _normalise_snapshot(snap)
    return snap


def series(log, usage_log, accounts_dir, realm, lo, hi, step, pool=None,
           pricing_on=None):
    """序列的整段扫描（wb_proxy._usage_timeseries_scan 的替身）。

    引擎的 series 段少 Python 返回里的 `ok` 与 `realm` 两个键，这里补上；
    其余键（bucket_seconds/since/until/series/credits）逐键来自引擎。

    数值形态也对齐：Python 那边 since/until 与每个桶/明细的 `at`、`credit`
    都是 float（JSON 里带 `.0`），引擎用 `%.17g` 打出来是整数文本。值相同、
    文本不同会让「逐字节相同」这条验收标准失效，所以逐字段转 float。
    """
    out = _run(log, usage_log, ["series"], accounts_dir, pool, pricing_on,
               need_pricing=False,
               realm="all", series_realm=realm or "all",
               since=float(lo), until=float(hi), has_since=True, has_until=True,
               step=int(step))
    if out is None:
        return None
    data = out.get("series")
    if not isinstance(data, dict) or "series" not in data:
        return None
    result = {"ok": True, "realm": realm or "all"}
    result.update(data)
    _normalise_series(result)
    return result
