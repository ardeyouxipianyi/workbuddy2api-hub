# -*- coding: utf-8 -*-
"""Field-by-field parity: standalone wb-agg (C) vs the Python folds.

Runs on the router (Linux).  The C tool's JSON is compared against the pure
Python folds over the same log; both sides read the same pricing tables.
"""
import argparse
import json
import os
import subprocess
import sys
import time

RAW_STAT = ("requests", "errors", "prompt_tokens", "completion_tokens",
            "reasoning_tokens", "cached_tokens", "total_tokens", "credit",
            "cost_cny", "ttft_sum", "ttft_n", "speed_sum", "speed_n",
            "elapsed_sum", "elapsed_n")


def deep_compare(path, py, cc, report, limit=40):
    """Collect mismatches; dicts are compared by key, lists elementwise."""
    if isinstance(py, dict) and isinstance(cc, dict):
        for key in sorted(set(py) | set(cc)):
            if key not in py or key not in cc:
                report["mismatch"] += 1
                if len(report["examples"]) < limit:
                    report["examples"].append(
                        "%s.%s: %s" % (path, key,
                                       "missing in C" if key not in cc else "missing in Python"))
                continue
            deep_compare(path + "." + str(key), py[key], cc[key], report, limit)
        return
    if isinstance(py, list) and isinstance(cc, list):
        if len(py) != len(cc):
            report["mismatch"] += 1
            if len(report["examples"]) < limit:
                report["examples"].append("%s: length %d vs %d" % (path, len(py), len(cc)))
            return
        for index, (a, b) in enumerate(zip(py, cc)):
            deep_compare("%s[%d]" % (path, index), a, b, report, limit)
        return
    report["leaf"] += 1
    if isinstance(py, bool) or isinstance(cc, bool):
        same = py is cc
    elif isinstance(py, (int, float)) and isinstance(cc, (int, float)):
        same = float(py) == float(cc)
    else:
        same = py == cc
    if not same:
        report["mismatch"] += 1
        if len(report["examples"]) < limit:
            report["examples"].append("%s: py=%r c=%r" % (path, py, cc))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tree", required=True)
    ap.add_argument("--data", required=True, help="directory holding usage.jsonl")
    ap.add_argument("--bin", required=True)
    ap.add_argument("--accounts", default=None)
    ap.add_argument("--daily-midnight", type=float, required=True)
    ap.add_argument("--series-lo", type=float, default=0.0)
    ap.add_argument("--series-hi", type=float, default=1e18)
    ap.add_argument("--series-step", type=int, default=86400)
    ap.add_argument("--pricing", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--perf-sample", type=int, default=5000)
    ap.add_argument("--now", type=float, default=0.0,
                    help="build time for the remaining payload (0 = read meta.now)")
    ap.add_argument("--with-remaining", action="store_true")
    ap.add_argument("--with-perf", action="store_true")
    ap.add_argument("--dump-py", default=None,
                    help="write the Python-side section payloads here for debugging")
    args = ap.parse_args()

    log = os.path.join(args.data, "usage.jsonl")
    command = [args.bin, "--log", log, "--daily-midnight", repr(args.daily_midnight),
               "--since", repr(args.series_lo), "--until", repr(args.series_hi),
               "--step", str(args.series_step)]
    if args.pricing:
        command += ["--pricing", args.pricing]
    if args.accounts:
        command += ["--accounts", args.accounts]
    if args.with_perf:
        command += ["--perf-sample", str(args.perf_sample)]
    if args.with_remaining:
        command += ["--now", repr(args.now)]
    started = time.perf_counter()
    proc = subprocess.run(command, capture_output=True, text=True, timeout=900)
    c_seconds = time.perf_counter() - started
    if proc.returncode != 0:
        print("C TOOL FAILED rc=%d\n%s" % (proc.returncode, proc.stderr[-2000:]))
        return 1
    c = json.loads(proc.stdout)

    os.environ["WB_PROXY_USAGE_DIR"] = args.data
    os.environ["WB_USAGE_CACHE"] = "0"
    os.environ["WB_NATIVE_CORE"] = "0"
    os.environ.setdefault("WB_PROXY_DEFAULT_REALM", "intl")
    if args.accounts:
        os.environ["ACCOUNTS_DIR"] = args.accounts
    sys.path.insert(0, args.tree)
    sys.dont_write_bytecode = True
    import wb_proxy as P
    P.wb_pricing.set_data_dir(args.data)
    P.wb_pricing.set_settings_dir(args.accounts or os.path.join(args.tree, "accounts"))
    # The abi3 experiment tree carries wb_native_core; a plain main tree does
    # not. When it is there, make sure it is switched off so the comparison
    # really is against the pure-Python folds.
    _core = getattr(P, "wb_native_core", None)
    if _core is not None:
        assert _core.module() is None

    py = {}
    snap = P.usage_snapshot(realm="all", ttl=0)
    py["snapshot"] = {k: snap[k] for k in
                      ("requests", "errors", "prompt_tokens", "completion_tokens",
                       "reasoning_tokens", "cached_tokens", "total_tokens",
                       "credit", "cost_cny", "cost_missing",
                       "by_model", "by_model_realm", "by_model_acct")}
    # The raw fold, not the decorated endpoint payload: the payload merges
    # unknown key ids and renames nothing else, and the C engine emits the
    # fold itself.
    rows = []
    with open(log, encoding="utf-8") as fh:
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
    maps = P._new_analytics_maps()
    pricing_on = P.wb_pricing.pricing_enabled()
    for row in rows:
        P._fold_usage_analytics(row, None, maps, P._ANALYTICS_ALL, pricing_on)
    py["analytics"] = maps
    P._byacct_state.update({"buckets": None, "offset": 0, "key": None, "tail": b""})
    py["by_account"] = P._usage_by_account_buckets()
    P._count_state.update({"offset": 0, "key": None, "tail": b"", "n": 0, "n_realm": {}})
    P._count_usage_rows_uncached(None)
    py["count"] = {"n": P._count_state["n"], "n_realm": dict(P._count_state["n_realm"])}
    scan = P._usage_timeseries_scan(None, args.series_lo, args.series_hi, args.series_step)
    py["series"] = {"bucket_seconds": scan["bucket_seconds"], "since": scan["since"],
                    "until": scan["until"], "series": scan["series"],
                    "credits": scan["credits"]}
    # The daily section needs a fixed "today" midnight; keep the patch
    # days_back-aware so the remaining payload's three-day keys stay distinct
    # (a constant would collapse them onto one day).
    P._local_midnight = (lambda ts=None, days_back=0:
                         args.daily_midnight - days_back * 86400.0)
    state = {"tokens": {}, "credits": {}, "models": {}, "offset": 0}
    state, _ = P._scan_daily_usage(0, state)
    py["daily"] = {"tokens": state["tokens"], "credits": state["credits"],
                   "models": state["models"]}

    if args.with_perf:
        py["perf"] = P._perf_stats_uncached(sample=args.perf_sample, realm=None,
                                            since=None, until=None)

    if args.with_remaining:
        class FakeAcct(object):
            def __init__(self, uid, nickname, realm, enabled, caps):
                self.uid = uid
                self.nickname = nickname
                self.realm = realm
                self.enabled = enabled
                self.model_caps = caps

        accounts = []
        if args.accounts:
            for name in sorted(os.listdir(args.accounts)):
                if not name.endswith(".json"):
                    continue
                with open(os.path.join(args.accounts, name), encoding="utf-8") as fh:
                    doc = json.load(fh)
                caps = {}
                for mid, entry in (doc.get("modelCaps") or {}).items():
                    if isinstance(entry, dict):
                        caps[str(mid)] = {"at": float(entry.get("at") or 0),
                                          "reset": float(entry.get("reset") or 0)}
                accounts.append(FakeAcct(
                    str(doc.get("uid") or name[:-5]),
                    str(doc.get("nickname") or ""),
                    str(doc.get("realm") or ""),
                    doc.get("enabled", True) is not False, caps))
        rem_state = {"events": [], "keys": set(), "usage": {}}
        for row in rows:
            P._fold_remaining(row, rem_state)
        events_path = os.path.join(args.data, "limit-events.jsonl")
        if os.path.isfile(events_path):
            with open(events_path, encoding="utf-8") as fh:
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
        realm_of = {a.uid: a.realm for a in accounts}
        daily_view = P._estimate_daily_view(P._estimate_daily_read(), None)
        py["remaining"] = P._remaining_payload_locked(rem_state, args.now,
                                                      accounts, realm_of, daily_view)

    wanted = ["snapshot", "analytics", "series", "by_account", "count", "daily",
              "perf", "remaining"]
    if args.dump_py:
        with open(args.dump_py, "w", encoding="utf-8") as fh:
            json.dump({name: py.get(name) for name in wanted}, fh,
                      ensure_ascii=False, indent=1)
    report = {"c_seconds": round(c_seconds, 3), "mismatch": 0, "leaf": 0, "examples": [],
              "sections": {}}
    for name in wanted:
        if name not in py or name not in c:
            continue
        section = {"mismatch": 0, "leaf": 0, "examples": []}
        deep_compare(name, py[name], c[name], section)
        report["sections"][name] = section
        report["mismatch"] += section["mismatch"]
        report["leaf"] += section["leaf"]
        report["examples"].extend(section["examples"][:3])
    print(json.dumps({k: v for k, v in report.items() if k != "examples"}, indent=2))
    if report["examples"]:
        print("first examples:")
        for line in report["examples"][:12]:
            print("  " + line)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    return 0 if report["mismatch"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
