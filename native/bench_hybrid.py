# -*- coding: utf-8 -*-
"""bench_hybrid —— 混合版（进程内 libwb_agg.so）四路计时与对拍。

在路由器命令行执行（SSH 由主控负责）；本脚本只测量、只打印，不写任何数据文件。

    python3 bench_hybrid.py --lib <libwb_agg.so> --bin <wb-agg> --tree <native-full 树> \\
        --data <usage 目录> [--data2 <另一个>] [--accounts ...] [--pricing ...] \\
        [--now X] [--daily-midnight X] [--perf-sample N] [--rounds 3]

八段全开 = snapshot / analytics / series / by_account / count / daily / perf / remaining。
四路：

  1. CLI 冷调：subprocess 跑 wb-agg（八段全开），每轮一个全新进程；
     墙钟 + 从 meta 读 cpu_seconds。
  2. 库调用冷调：每轮一个全新 python 子进程；子进程内第一次
     wb_native_agg.run(request)（八段全开）记冷调（含懒加载 dlopen），
     紧接着同进程第二次调用（价表缓存命中）单独计时——混合版的关键数字。
  3. Python 纯折叠对照：--tree 里的 wb_proxy 直调（_fold_usage_snapshot 全量 /
     _fold_usage_analytics 全量 / _perf_stats_uncached / _usage_timeseries_scan /
     by_account / count / daily / remaining），冷态（WB_USAGE_CACHE=0、TTL=0、
     WB_NATIVE_CORE=0），每轮一个全新子进程；单轮超过 300s 只跑 1 轮。
     只作速度对照，不与 C 逐字节比较。
  4. 结果一致性：库调用 vs CLI 的 JSON（同参数同数据）。先剔除 meta 里的计时
     字段（*_seconds / peak_rss_kib），其余按 canonical JSON（sort_keys、紧凑
     分隔、ensure_ascii=False）逐字节比较；打印 equal=True/False，不相等时
     附前几条路径级差异。注意：同一份请求在两侧都显式传值（见输出里的
     cli_command 与 request 两份参数，保证"同参数"）。

TZ：脚本不设 TZ（由调用方决定）；输出里带上 os.environ.get("TZ") 供主控核对。
--daily-midnight / --now 缺省时由 Python 按本机时区现算并同时喂给两侧；
要跨 TZ 复现请显式传值（建议 TZ=UTC 跑）。

最后一行打印单行 JSON 汇总，便于主控收集。
仅标准库；Python 3.12 / 3.13 均可运行。
"""

import argparse
import json
import os
import subprocess
import sys
import time

ALL_SECTIONS = ("snapshot", "analytics", "series", "by_account", "count",
                "daily", "perf", "remaining")
# meta 里每次运行必然不同的字段：对拍前剔除（*_seconds 后缀 + 峰值内存）。
VOLATILE_META = ("c_seconds", "cpu_seconds", "load_seconds", "parse_seconds",
                 "fold_seconds", "peak_rss_kib")
PY_ROUND_BUDGET_S = 300.0      # Python 对照单轮超过它就只跑 1 轮
CLI_TIMEOUT_S = 900
LIB_CHILD_TIMEOUT_S = 900
PY_CHILD_TIMEOUT_S = 3600
DIFF_LIMIT = 20

_MISSING = object()


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

class _Series(object):
    """一路测量的若干轮样本：保留全部 runs，汇总报最小值。"""

    def __init__(self):
        self.runs = []

    def add(self, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        self.runs.append(float(value))

    def min(self):
        return min(self.runs) if self.runs else None

    def as_dict(self):
        return {"min": self.min(), "runs": self.runs}


def _jsonable(value):
    """把 _Series 换成可序列化 dict（递归）。"""
    if isinstance(value, _Series):
        return value.as_dict()
    if isinstance(value, dict):
        return dict((k, _jsonable(v)) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _fmt(value, unit="s"):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "n/a"
    return "%.3f%s" % (value, unit)


def _last_json_line(raw):
    """从子进程 stdout 里取最后一行可解析为 dict 的 JSON。"""
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:
        return None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except Exception:
            continue
        if isinstance(value, dict):
            return value
    return None


def _local_midnight_of(now):
    """now 所在本地日的 00:00（与 wb-agg main.c 的 local_midnight_now 同式）。"""
    lt = time.localtime(now)
    return float(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


def _native_module():
    """导入 wb_native_agg：先看脚本同目录，再看仓库根（native/ 的上一层）。

    失败返回 None（不抛）——库侧拿不到就整段跳过，基准照跑 CLI 与 Python 两路。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (here, os.path.dirname(here)):
        if candidate and candidate not in sys.path:
            sys.path.append(candidate)
    try:
        import wb_native_agg
    except Exception:
        return None
    return wb_native_agg


# ---------------------------------------------------------------------------
# 请求 / 命令行：两侧参数出自同一份取值，保证"同参数同数据"
# ---------------------------------------------------------------------------

def _optional_paths(ctx, data):
    """两侧都显式传的兄弟文件：存在才传，不存在时两侧都走各自的缺省。"""
    optional = {}
    if ctx["pricing"]:
        optional["pricing"] = ctx["pricing"]
    if ctx["accounts"]:
        optional["accounts"] = ctx["accounts"]
    for key, name in (("policies", "pricing-policies.jsonl"),
                      ("timeline", "pricing-timeline.jsonl"),
                      ("events", "limit-events.jsonl"),
                      ("estimate_daily", "estimate-daily.json")):
        path = os.path.join(data, name)
        if os.path.isfile(path):
            optional[key] = path
    return optional


def _build_request(ctx, log, now, midnight, optional):
    request = {
        "log": log,
        "sections": list(ALL_SECTIONS),
        "realm": "all",
        "default_realm": ctx["default_realm"],
        "series_realm": "all",
        "perf_realm": "all",
        "since": ctx["series_lo"],
        "until": ctx["series_hi"],
        "has_since": True,
        "has_until": True,
        "step": ctx["series_step"],
        "daily_midnight": midnight,
        "perf_sample": ctx["perf_sample"],
        "now": now,
    }
    request.update(optional)
    return request


def _build_cli(ctx, log, now, midnight, optional):
    cmd = [ctx["binary"], "--log", log,
           "--realm", "all", "--series-realm", "all", "--perf-realm", "all",
           "--default-realm", ctx["default_realm"],
           "--daily-midnight", repr(midnight),
           "--since", repr(ctx["series_lo"]),
           "--until", repr(ctx["series_hi"]),
           "--step", str(ctx["series_step"]),
           "--perf-sample", str(ctx["perf_sample"]),
           "--now", repr(now)]
    if "pricing" in optional:
        cmd += ["--pricing", optional["pricing"]]
    if "accounts" in optional:
        cmd += ["--accounts", optional["accounts"]]
    if "policies" in optional:
        cmd += ["--policies", optional["policies"]]
    if "timeline" in optional:
        cmd += ["--timeline", optional["timeline"]]
    if "events" in optional:
        cmd += ["--events", optional["events"]]
    if "estimate_daily" in optional:
        cmd += ["--estimate-daily", optional["estimate_daily"]]
    return cmd


# ---------------------------------------------------------------------------
# 子进程模式（--internal-*）：被父进程按轮拉起，各跑一次，打印一行 JSON
# ---------------------------------------------------------------------------

def _child_lib_run(request_text):
    """一个全新进程里：run#1（冷，含懒加载）计时；run#2（价表缓存命中）单独计时。"""
    request = json.loads(request_text)
    wb_native_agg = _native_module()
    if wb_native_agg is None:
        print(json.dumps({"ok": False, "available": False,
                          "reason": "import wb_native_agg failed (native/ or repo root)"}))
        return 1

    def _call():
        t0 = time.perf_counter()
        before = os.times()
        result = wb_native_agg.run(request)
        wall = time.perf_counter() - t0
        after = os.times()
        proc_cpu = (after.user - before.user) + (after.system - before.system)
        meta_cpu = None
        if isinstance(result, dict):
            meta = result.get("meta")
            if isinstance(meta, dict):
                meta_cpu = meta.get("cpu_seconds")
        return {"wall_s": wall, "meta_cpu_s": meta_cpu,
                "proc_cpu_s": proc_cpu, "ok": result is not None}

    cold = _call()
    out = {"ok": bool(cold["ok"]), "cold": cold, "warm": None, "available": None,
           "version": None, "path": None, "reason": None}
    if cold["ok"]:
        out["warm"] = _call()
    out["available"] = wb_native_agg.available()
    out["version"] = wb_native_agg.version()
    out["path"] = wb_native_agg.library_path()
    if not out["ok"]:
        if not out["available"]:
            out["reason"] = "lib unavailable (WB_AGG_LIB=%r)" % os.environ.get("WB_AGG_LIB")
        else:
            out["reason"] = wb_native_agg.last_error() or "run returned None"
    print(json.dumps(out, ensure_ascii=True))
    return 0 if out["ok"] else 1


class _FakeAcct(object):
    """parity.py 同款：剩余用量载荷读账号视图用的最小账号对象。"""

    def __init__(self, uid, nickname, realm, enabled, caps):
        self.uid = uid
        self.nickname = nickname
        self.realm = realm
        self.enabled = enabled
        self.model_caps = caps


def _load_accounts_for_remaining(accounts_dir):
    """读 --accounts 目录，构造 (_FakeAcct 列表, uid -> realm)。"""
    accounts = []
    realm_of = {}
    if not accounts_dir or not os.path.isdir(accounts_dir):
        return accounts, realm_of
    for name in sorted(os.listdir(accounts_dir)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(accounts_dir, name), encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        caps = {}
        for mid, entry in (doc.get("modelCaps") or {}).items():
            if isinstance(entry, dict):
                caps[str(mid)] = {"at": float(entry.get("at") or 0),
                                  "reset": float(entry.get("reset") or 0)}
        acct = _FakeAcct(str(doc.get("uid") or name[:-5]),
                         str(doc.get("nickname") or ""),
                         str(doc.get("realm") or ""),
                         doc.get("enabled", True) is not False, caps)
        accounts.append(acct)
        realm_of[acct.uid] = acct.realm
    return accounts, realm_of


def _run_py_folds(spec, out):
    """纯 Python 冷折一套（八条路径），把每项耗时写进 out["folds"]。"""
    os.environ["WB_PROXY_USAGE_DIR"] = spec["data"]
    os.environ["WB_USAGE_CACHE"] = "0"
    os.environ["WB_NATIVE_CORE"] = "0"
    os.environ["WB_STATS_TTL"] = "0"
    os.environ["WB_COUNT_TTL"] = "0"
    os.environ["WB_REMAINING_TTL"] = "0"
    # 与 C 侧 --default-realm / --accounts 强制对齐：继承来的环境变量不能让
    # 纯 Python 对照读成另一份 realm 归属。
    os.environ["WB_PROXY_DEFAULT_REALM"] = spec.get("default_realm") or "intl"
    accounts_dir = spec.get("accounts")
    if accounts_dir:
        os.environ["ACCOUNTS_DIR"] = accounts_dir
    else:
        os.environ.pop("ACCOUNTS_DIR", None)
    sys.dont_write_bytecode = True
    tree = spec["tree"]
    if tree not in sys.path:
        sys.path.insert(0, tree)

    t0 = time.perf_counter()
    import wb_proxy as P
    out["import_s"] = time.perf_counter() - t0

    P.wb_pricing.set_data_dir(spec["data"])
    P.wb_pricing.set_settings_dir(accounts_dir or os.path.join(tree, "accounts"))
    # Only the abi3 experiment tree has wb_native_core; a plain main tree does
    # not. If it is there it must be off, so this really times pure Python.
    _core = getattr(P, "wb_native_core", None)
    if _core is not None and _core.module() is not None:
        raise RuntimeError("wb_native_core loaded; expected pure Python (WB_NATIVE_CORE=0)")

    def timed(name, func):
        started = time.perf_counter()
        value = func()
        out["folds"][name] = time.perf_counter() - started
        return value

    # 全量行先解析一次：三条手工折叠共用同一批行（与 parity.py 同法）。
    log = os.path.join(spec["data"], "usage.jsonl")
    started = time.perf_counter()
    rows = []
    with open(log, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    out["parse_rows_s"] = time.perf_counter() - started
    out["rows"] = len(rows)

    pricing_on = P.wb_pricing.pricing_enabled()

    def fold_snapshot():
        snap = P._empty_stats()
        pricing = P._pricing_inputs(pricing_on)
        for row in rows:
            P._fold_usage_snapshot(row, snap, None, None, None, pricing_on,
                                   pricing=pricing)
        return snap

    timed("snapshot_fold", fold_snapshot)

    def fold_analytics():
        maps = P._new_analytics_maps()
        for row in rows:
            P._fold_usage_analytics(row, None, maps, P._ANALYTICS_ALL, pricing_on)
        return maps

    timed("analytics_fold", fold_analytics)

    timed("perf_stats", lambda: P._perf_stats_uncached(
        sample=spec["perf_sample"], realm=None, since=None, until=None))

    timed("series_scan", lambda: P._usage_timeseries_scan(
        None, spec["series_lo"], spec["series_hi"], spec["series_step"]))

    def fold_by_account():
        P._byacct_state.update({"buckets": None, "offset": 0, "key": None, "tail": b""})
        return P._usage_by_account_buckets()

    timed("by_account", fold_by_account)

    def fold_count():
        P._count_state.update({"offset": 0, "key": None, "tail": b"",
                               "n": 0, "n_realm": {}})
        return P._count_usage_rows_uncached(None)

    timed("count", fold_count)

    def fold_daily():
        # 与 C 侧 --daily-midnight 对齐（parity.py 同款补丁，days_back 保持可用）。
        P._local_midnight = (lambda ts=None, days_back=0:
                             spec["daily_midnight"] - days_back * 86400.0)
        state = {"tokens": {}, "credits": {}, "models": {}, "offset": 0}
        state, _ = P._scan_daily_usage(0, state)
        return state

    timed("daily_scan", fold_daily)

    def fold_remaining():
        rem_state = {"events": [], "keys": set(), "usage": {}}
        for row in rows:
            P._fold_remaining(row, rem_state)
        events_path = os.path.join(spec["data"], "limit-events.jsonl")
        if os.path.isfile(events_path):
            with open(events_path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(event, dict):
                        P._merge_event(rem_state, event, "file")
        daily_view = P._estimate_daily_view(P._estimate_daily_read(), None)
        accounts, realm_of = _load_accounts_for_remaining(spec.get("accounts"))
        return P._remaining_payload_locked(rem_state, spec["now"], accounts,
                                           realm_of, daily_view)

    timed("remaining_fold", fold_remaining)

    out["total_s"] = (out["parse_rows_s"] or 0.0) + sum(out["folds"].values())


def _child_py_run(spec_text):
    """一个全新进程里跑完整套纯 Python 冷折，打印一行 JSON。"""
    spec = json.loads(spec_text)
    out = {"ok": False, "error": None, "import_s": None, "parse_rows_s": None,
           "folds": {}, "total_s": None, "rows": 0}
    try:
        _run_py_folds(spec, out)
        out["ok"] = True
    except Exception as exc:
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    print(json.dumps(out, ensure_ascii=True))
    return 0 if out["ok"] else 1


# ---------------------------------------------------------------------------
# 四路（父进程侧）
# ---------------------------------------------------------------------------

def _route_cli(ctx, cmd):
    """第 1 路：CLI 冷调，rounds 轮，每轮一个全新进程。返回 (最后一轮文本, entry)。"""
    wall = _Series()
    cpu = _Series()
    entry = {"wall": wall, "cpu": cpu, "rc": [], "runs": [], "error": None}
    text = None
    for round_index in range(ctx["rounds"]):
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=CLI_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            entry["error"] = "timeout after %ds" % CLI_TIMEOUT_S
            print("  [cli] round%d TIMEOUT after %ds" % (round_index + 1, CLI_TIMEOUT_S))
            break
        except OSError as exc:
            entry["error"] = "spawn failed: %s" % exc
            print("  [cli] round%d spawn failed: %s" % (round_index + 1, exc))
            break
        elapsed = time.perf_counter() - t0
        ok = False
        meta = None
        if proc.returncode == 0:
            try:
                doc = json.loads(proc.stdout.decode("utf-8", "replace"))
            except Exception:
                doc = None
            if isinstance(doc, dict):
                meta = doc.get("meta")
                if isinstance(meta, dict):
                    ok = True
                    text = proc.stdout.decode("utf-8", "replace")
        cpu_value = meta.get("cpu_seconds") if isinstance(meta, dict) else None
        wall.add(elapsed)
        cpu.add(cpu_value)
        entry["rc"].append(proc.returncode)
        entry["runs"].append({"wall_s": elapsed, "cpu_s": cpu_value,
                              "rc": proc.returncode})
        print("  [cli] round%d wall=%s cpu=%s rc=%d"
              % (round_index + 1, _fmt(elapsed), _fmt(cpu_value), proc.returncode))
        if not ok:
            entry["error"] = ("rc=%d" % proc.returncode) if proc.returncode \
                else "stdout not JSON"
            tail = proc.stderr.decode("utf-8", "replace").strip()[-400:]
            if tail:
                print("  [cli] stderr tail: %s" % tail)
            break
    if entry["runs"]:
        print("  [cli] min wall=%s cpu=%s (%d run%s)"
              % (_fmt(wall.min()), _fmt(cpu.min()), len(entry["runs"]),
                 "" if len(entry["runs"]) == 1 else "s"))
    if entry["error"]:
        print("  [cli] error: %s" % entry["error"])
    return text, entry


def _route_lib(ctx, request):
    """第 2 路：库调用。每轮一个全新 python 子进程：冷调 + 第二次（缓存命中）。"""
    entry = {"available": None, "version": None, "path": None, "error": None,
             "cold": {"wall": _Series(), "cpu": _Series(), "proc_cpu": _Series()},
             "warm": {"wall": _Series(), "cpu": _Series(), "proc_cpu": _Series()},
             "runs": []}
    request_text = json.dumps(request, ensure_ascii=True)
    for round_index in range(ctx["rounds"]):
        cmd = [ctx["python"], "-B", ctx["script"], "--internal-lib-run", request_text]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=ctx["child_env"], timeout=LIB_CHILD_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            entry["error"] = "child timeout after %ds" % LIB_CHILD_TIMEOUT_S
            break
        except OSError as exc:
            entry["error"] = "child spawn failed: %s" % exc
            break
        payload = _last_json_line(proc.stdout)
        if payload is None:
            entry["error"] = "child produced no JSON (rc=%d): %s" % (
                proc.returncode, proc.stderr.decode("utf-8", "replace").strip()[-300:])
            break
        entry["available"] = payload.get("available")
        entry["version"] = payload.get("version")
        entry["path"] = payload.get("path")
        if not payload.get("ok"):
            entry["error"] = payload.get("reason") or "lib run failed"
            break
        cold = payload.get("cold") or {}
        warm = payload.get("warm") or {}
        entry["cold"]["wall"].add(cold.get("wall_s"))
        entry["cold"]["cpu"].add(cold.get("meta_cpu_s"))
        entry["cold"]["proc_cpu"].add(cold.get("proc_cpu_s"))
        entry["warm"]["wall"].add(warm.get("wall_s"))
        entry["warm"]["cpu"].add(warm.get("meta_cpu_s"))
        entry["warm"]["proc_cpu"].add(warm.get("proc_cpu_s"))
        entry["runs"].append({"cold": cold, "warm": warm})
        print("  [lib] round%d cold wall=%s cpu=%s | warm#2 wall=%s cpu=%s"
              % (round_index + 1, _fmt(cold.get("wall_s")), _fmt(cold.get("meta_cpu_s")),
                 _fmt(warm.get("wall_s")), _fmt(warm.get("meta_cpu_s"))))
    if entry["runs"]:
        print("  [lib] cold min wall=%s cpu=%s | warm#2 min wall=%s cpu=%s (path=%s version=%s)"
              % (_fmt(entry["cold"]["wall"].min()), _fmt(entry["cold"]["cpu"].min()),
                 _fmt(entry["warm"]["wall"].min()), _fmt(entry["warm"]["cpu"].min()),
                 entry["path"], entry["version"]))
    if entry["error"]:
        print("  [lib] unavailable/failed: %s" % entry["error"])
    return entry


def _route_python(ctx, data, now, midnight):
    """第 3 路：纯 Python 冷折对照。每轮一个全新子进程；单轮 >300s 只跑 1 轮。"""
    entry = {"rounds_run": 0, "error": None, "rows": None,
             "import": _Series(), "parse_rows": _Series(),
             "total": _Series(), "folds": {}}
    spec = {"tree": ctx["tree"], "data": data, "accounts": ctx["accounts"],
            "default_realm": ctx["default_realm"], "daily_midnight": midnight,
            "now": now, "perf_sample": ctx["perf_sample"],
            "series_lo": ctx["series_lo"], "series_hi": ctx["series_hi"],
            "series_step": ctx["series_step"]}
    spec_text = json.dumps(spec, ensure_ascii=True)
    for round_index in range(ctx["rounds"]):
        cmd = [ctx["python"], "-B", ctx["script"], "--internal-py-run", spec_text]
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=ctx["child_env"], timeout=PY_CHILD_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            entry["error"] = "child timeout after %ds" % PY_CHILD_TIMEOUT_S
            break
        except OSError as exc:
            entry["error"] = "child spawn failed: %s" % exc
            break
        child_wall = time.perf_counter() - t0
        payload = _last_json_line(proc.stdout)
        if payload is None:
            entry["error"] = "child produced no JSON (rc=%d): %s" % (
                proc.returncode, proc.stderr.decode("utf-8", "replace").strip()[-300:])
            break
        if not payload.get("ok"):
            entry["error"] = payload.get("error") or "python folds failed"
            break
        entry["rounds_run"] = round_index + 1
        entry["rows"] = payload.get("rows")
        entry["import"].add(payload.get("import_s"))
        entry["parse_rows"].add(payload.get("parse_rows_s"))
        folds = payload.get("folds") or {}
        for name, seconds in folds.items():
            entry["folds"].setdefault(name, _Series()).add(seconds)
        entry["total"].add(payload.get("total_s"))
        print("  [py ] round%d wall=%s import=%s parse_rows=%s total=%s rows=%s"
              % (round_index + 1, _fmt(child_wall), _fmt(payload.get("import_s")),
                 _fmt(payload.get("parse_rows_s")), _fmt(payload.get("total_s")),
                 payload.get("rows")))
        print("  [py ]   folds: %s" % " ".join(
            "%s=%s" % (name, _fmt(seconds)) for name, seconds in sorted(folds.items())))
        total = payload.get("total_s")
        if isinstance(total, (int, float)) and not isinstance(total, bool) \
                and total > PY_ROUND_BUDGET_S:
            print("  [py ] total > %.0fs; stopping after 1 round" % PY_ROUND_BUDGET_S)
            break
    if entry["rounds_run"]:
        print("  [py ] min total=%s (rounds=%d)"
              % (_fmt(entry["total"].min()), entry["rounds_run"]))
    elif entry["error"]:
        print("  [py ] error: %s" % entry["error"])
    return entry


# ---------------------------------------------------------------------------
# 第 4 路：结果一致性（库 vs CLI，canonical 字节比较）
# ---------------------------------------------------------------------------

def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _strip_volatile_meta(meta):
    if not isinstance(meta, dict):
        return meta
    return dict((k, v) for k, v in meta.items()
                if k not in VOLATILE_META and not k.endswith("_seconds"))


def _diff_paths(a, b, path, diffs, limit=DIFF_LIMIT):
    """收集路径级差异（最多 limit 条）；数值按 float 相等比较。"""
    if len(diffs) >= limit:
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if len(diffs) >= limit:
                return
            if key not in a or key not in b:
                diffs.append("%s.%s: %s" % (path, key, "missing in lib" if key not in b
                                            else "missing in cli"))
                continue
            _diff_paths(a[key], b[key], path + "." + str(key), diffs, limit)
        return
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            diffs.append("%s: length cli=%d lib=%d" % (path, len(a), len(b)))
            return
        for index, (x, y) in enumerate(zip(a, b)):
            if len(diffs) >= limit:
                return
            _diff_paths(x, y, "%s[%d]" % (path, index), diffs, limit)
        return
    if isinstance(a, bool) or isinstance(b, bool):
        same = a is b
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
        same = float(a) == float(b)
    else:
        same = a == b
    if not same:
        diffs.append("%s: cli=%r lib=%r" % (path, a, b))


def _compare_documents(cli_doc, lib_doc):
    """八段 + 稳定 meta 的 canonical 字节比较；逐段释放以控内存。"""
    report = {"equal": None, "sections": {}, "meta_stable": None,
              "sizes": {}, "diffs": [],
              "method": "canonical JSON bytes (sort_keys, compact, ensure_ascii=False); "
                        "meta timing fields stripped"}
    if not isinstance(cli_doc, dict) or not isinstance(lib_doc, dict):
        report["equal"] = False
        report["diffs"].append("top-level not a JSON object (cli=%s lib=%s)"
                               % (type(cli_doc).__name__, type(lib_doc).__name__))
        return report
    equal = True
    for name in ALL_SECTIONS:
        in_cli = name in cli_doc
        in_lib = name in lib_doc
        if in_cli and in_lib:
            bytes_cli = _canonical(cli_doc[name])
            bytes_lib = _canonical(lib_doc[name])
            same = bytes_cli == bytes_lib
            report["sizes"][name] = len(bytes_lib)
            if not same:
                _diff_paths(cli_doc[name], lib_doc[name], name, report["diffs"])
        else:
            # 本 bench 固定八段全开：缺任何一段都是异常，必须判不等。
            same = False
            if in_cli or in_lib:
                report["diffs"].append("%s: present cli=%s lib=%s"
                                       % (name, in_cli, in_lib))
            else:
                report["diffs"].append("%s: absent on both sides" % name)
        report["sections"][name] = same
        if not same:
            equal = False
        cli_doc.pop(name, None)
        lib_doc.pop(name, None)
    meta_cli = _strip_volatile_meta(cli_doc.pop("meta", None))
    meta_lib = _strip_volatile_meta(lib_doc.pop("meta", None))
    if isinstance(meta_cli, dict) and isinstance(meta_lib, dict):
        meta_same = _canonical(meta_cli) == _canonical(meta_lib)
        if not meta_same:
            _diff_paths(meta_cli, meta_lib, "meta", report["diffs"])
    else:
        meta_same = meta_cli == meta_lib
    report["meta_stable"] = meta_same
    if not meta_same:
        equal = False
    # 顶层余键（理论上没有；有就必须一致）。
    if cli_doc or lib_doc:
        rest_same = _canonical(cli_doc) == _canonical(lib_doc)
        if not rest_same:
            equal = False
            _diff_paths(cli_doc, lib_doc, "<top>", report["diffs"])
    report["equal"] = equal
    return report


def _route_equality(ctx, request, cli_text):
    """第 4 路：库调用 vs CLI（同参数同数据）。"""
    entry = {"equal": None, "sections": {}, "meta_stable": None, "sizes": {},
             "diffs": [], "method": None, "reason": None}
    if cli_text is None:
        entry["reason"] = "no usable CLI output"
        print("  [eq ] skipped: no usable CLI output")
        return entry
    module = _native_module()
    if module is None or not module.available():
        entry["reason"] = "lib unavailable"
        print("  [eq ] skipped: lib unavailable")
        return entry
    lib_doc = module.run(request)
    if lib_doc is None:
        entry["reason"] = "lib run returned None: %s" % (module.last_error() or "?")
        print("  [eq ] skipped: %s" % entry["reason"])
        return entry
    try:
        cli_doc = json.loads(cli_text)
    except Exception as exc:
        entry["reason"] = "CLI JSON parse failed: %s" % exc
        print("  [eq ] skipped: %s" % entry["reason"])
        return entry
    report = _compare_documents(cli_doc, lib_doc)
    entry.update(report)
    good = sum(1 for value in report["sections"].values() if value)
    print("  [eq ] equal=%s sections=%d/%d meta_stable=%s"
          % (report["equal"], good, len(ALL_SECTIONS), report["meta_stable"]))
    for line in report["diffs"][:8]:
        print("  [eq ]   diff: %s" % line)
    return entry


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _build_parser():
    parser = argparse.ArgumentParser(
        description="混合版四路计时与对拍（CLI 冷调 / 库冷调+二次 / Python 纯折对照 / 结果一致性）")
    parser.add_argument("--lib", default=None,
                        help="libwb_agg.so 路径（经 WB_AGG_LIB 传给 wb_native_agg；缺省走其定位顺序）")
    parser.add_argument("--bin", dest="binary", default=None, help="wb-agg 可执行文件路径")
    parser.add_argument("--tree", default=None, help="native-full 树（含 wb_proxy.py）")
    parser.add_argument("--data", default=None, help="usage 目录（含 usage.jsonl）")
    parser.add_argument("--data2", default=None, help="第二个 usage 目录（可选）")
    parser.add_argument("--accounts", default=None, help="账号目录（两侧都显式传）")
    parser.add_argument("--pricing", default=None, help="pricing.json（两侧都显式传）")
    parser.add_argument("--now", type=float, default=None,
                        help="remaining 用的 now（缺省 = 本脚本开跑时的整数秒）")
    parser.add_argument("--daily-midnight", type=float, default=None,
                        help="daily 的本地午夜（缺省按 now 的本机时区现算）")
    parser.add_argument("--perf-sample", type=int, default=5000)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--series-lo", type=float, default=0.0)
    parser.add_argument("--series-hi", type=float, default=1e18)
    parser.add_argument("--series-step", type=int, default=86400)
    parser.add_argument("--default-realm", default="intl")
    parser.add_argument("--internal-lib-run", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--internal-py-run", default=None, help=argparse.SUPPRESS)
    return parser


def _run_dataset(ctx, data, index, total, now, midnight):
    print("")
    print("== dataset %d/%d: %s ==" % (index, total, data))
    log = os.path.join(data, "usage.jsonl")
    entry = {"data": data, "log": log, "tz": os.environ.get("TZ"),
             "now": now, "daily_midnight": midnight}
    if not os.path.isfile(log):
        print("  [err] usage.jsonl not found: %s" % log)
        entry["error"] = "usage.jsonl not found"
        return entry
    optional = _optional_paths(ctx, data)
    request = _build_request(ctx, log, now, midnight, optional)
    cmd = _build_cli(ctx, log, now, midnight, optional)
    entry["cli_command"] = cmd
    entry["request"] = request
    print("  log=%s" % log)
    print("  tz=%s rounds=%d sections=%d"
          % (os.environ.get("TZ"), ctx["rounds"], len(ALL_SECTIONS)))

    cli_text, cli_entry = _route_cli(ctx, cmd)
    entry["cli"] = cli_entry
    entry["lib"] = _route_lib(ctx, request)
    entry["python"] = _route_python(ctx, data, now, midnight)
    entry["equality"] = _route_equality(ctx, request, cli_text)
    return entry


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.internal_lib_run:
        return _child_lib_run(args.internal_lib_run)
    if args.internal_py_run:
        return _child_py_run(args.internal_py_run)

    missing = [flag for flag, value in (("--bin", args.binary), ("--tree", args.tree),
                                        ("--data", args.data)) if not value]
    if missing:
        parser.error("missing required arguments: " + ", ".join(missing))
    rounds = args.rounds if args.rounds >= 1 else 1

    if args.lib:
        os.environ["WB_AGG_LIB"] = args.lib
    ctx = {
        "binary": args.binary,
        "tree": os.path.abspath(args.tree),
        "script": os.path.abspath(__file__),
        "python": sys.executable or "python3",
        "accounts": args.accounts,
        "pricing": args.pricing,
        "default_realm": args.default_realm,
        "perf_sample": args.perf_sample,
        "series_lo": args.series_lo,
        "series_hi": args.series_hi,
        "series_step": args.series_step,
        "rounds": rounds,
    }
    child_env = dict(os.environ)
    child_env["PYTHONIOENCODING"] = "utf-8"
    ctx["child_env"] = child_env

    now = args.now if args.now is not None else float(int(time.time()))
    midnight = args.daily_midnight
    if midnight is None:
        midnight = _local_midnight_of(now)
        print("[warn] --daily-midnight not given; computed in Python (TZ=%s): %r"
              % (os.environ.get("TZ"), midnight))
        print("[warn] pass --daily-midnight explicitly to pin it across TZ differences")

    datasets = [args.data] + ([args.data2] if args.data2 else [])
    summary = {
        "bench": "bench_hybrid",
        "tz": os.environ.get("TZ"),
        "rounds": rounds,
        "bin": args.binary,
        "tree": ctx["tree"],
        "lib_arg": args.lib,
        "now": now,
        "daily_midnight": midnight,
        "perf_sample": args.perf_sample,
        "series": {"lo": args.series_lo, "hi": args.series_hi,
                   "step": args.series_step},
        "default_realm": args.default_realm,
        "sections": list(ALL_SECTIONS),
        "datasets": [],
    }
    for index, data in enumerate(datasets):
        summary["datasets"].append(
            _run_dataset(ctx, data, index + 1, len(datasets), now, midnight))
    print("")
    print(json.dumps(_jsonable(summary), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
