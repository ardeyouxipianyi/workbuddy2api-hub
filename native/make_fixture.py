# -*- coding: utf-8 -*-
"""Build the deterministic perf/remaining fixture on the router (or locally).

usage: python3 make_fixture.py <outdir>

Creates:
  <outdir>/usage/usage.jsonl          synthetic rows (normal/error/aborted/429,
                                      one out-of-order pair), ~3 local days
  <outdir>/usage/limit-events.jsonl   two events (one duplicate reset)
  <outdir>/usage/estimate-daily.json  yesterday's archived budgets
  <outdir>/accounts/*.json            three accounts with modelCaps
The timeline is anchored to NOW (see NOW below) so both sides agree.
"""
import io
import json
import os
import sys

NOW = 1791806400.0                      # 2026-10-12 12:00:00 +08 (TZ=Asia/Shanghai)
DAY = 86400.0
OUT = sys.argv[1] if len(sys.argv) > 1 else "fixture"

# local day keys used by the fixture (Asia/Shanghai): 10-12 is "today", 10-11 yesterday
TODAY = "2026-10-12"
YESTERDAY = "2026-10-11"


def iso(at):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at))


def row(at, uid, model, realm, tokens, **extra):
    r = {"at": at, "iso": iso(at), "model": model, "account": uid, "realm": realm,
         "outcome": "completed", "total_tokens": tokens, "prompt_tokens": tokens // 2,
         "completion_tokens": tokens - tokens // 2, "credit": extra.pop("credit", 0)}
    r.update(extra)
    return r


def main():
    usage_dir = os.path.join(OUT, "usage")
    acct_dir = os.path.join(OUT, "accounts")
    os.makedirs(usage_dir, exist_ok=True)
    os.makedirs(acct_dir, exist_ok=True)
    lines = []

    # normal traffic for three pairs across ~3 days
    pairs = [("acct-a", "model-x", "intl"), ("acct-b", "model-y", "cn"),
             ("acct-a", "model-y", "cn"), ("acct-c", "model-x", "intl")]
    for step in range(0, 3 * 24 * 2):                    # every 30 min, 3 days
        at = NOW - 3 * DAY + step * 1800 + 7.25
        uid, model, realm = pairs[step % len(pairs)]
        lines.append(row(at, uid, model, realm, 100 + (step * 37) % 900,
                         credit=0.25 if step % 5 == 0 else 0))
    # latency fields so perf has samples
    for index, line in enumerate(lines):
        line["elapsed_ms"] = 800 + (index * 13) % 1500
        line["ttft_ms"] = 200 + (index * 7) % 500
        line["gen_ms"] = line["elapsed_ms"] - line["ttft_ms"]
        line["tokens_per_sec"] = 40.0 + (index % 60)
        line["cache_hit_pct"] = float((index * 3) % 100)
    # an out-of-order pair (completion clock regression, like the real log)
    lines.append(row(NOW - 3600.5, "acct-a", "model-x", "intl", 777))
    lines.append(row(NOW - 3600.5 + 0.00496, "acct-a", "model-x", "intl", 778))
    # rows the remaining fold skips
    lines.append({"at": NOW - 2000, "model": "model-x", "account": "acct-a",
                  "realm": "intl", "outcome": "completed", "total_tokens": 999,
                  "error": "boom"})
    lines.append({"at": NOW - 1900, "model": "model-x", "account": "acct-a",
                  "realm": "intl", "outcome": "completed", "total_tokens": 999,
                  "usage_missing": True})
    lines.append({"at": NOW - 1800, "model": "model-x", "account": "acct-a",
                  "realm": "intl", "outcome": "client_aborted", "total_tokens": 999})
    # 429 rows: cooling now / cooling long gone / just reset
    lines.append({"at": NOW - 2 * 3600, "iso": iso(NOW - 2 * 3600), "model": "model-x",
                  "account": "acct-a", "realm": "intl", "status": 429, "outcome": "failed",
                  "error": "HTTP 429", "message": "your usage will reset at 2026-10-12 14:00:00 UTC+8"})
    lines.append({"at": NOW - 26 * 3600, "iso": iso(NOW - 26 * 3600), "model": "model-y",
                  "account": "acct-b", "realm": "cn", "status": 429, "outcome": "failed",
                  "error": "HTTP 429", "message": "将在 2026-10-12 10:00:00 UTC+8 重置"})
    lines.append({"at": NOW - 50 * 3600, "iso": iso(NOW - 50 * 3600), "model": "model-y",
                  "account": "acct-a", "realm": "cn", "status": 429, "outcome": "failed",
                  "error": "HTTP 429", "message": "将在 2026-10-11 10:00:00 UTC+8 重置"})
    lines.sort(key=lambda item: item["at"])
    with io.open(os.path.join(usage_dir, "usage.jsonl"), "w", encoding="utf-8",
                 newline="\n") as fh:
        for line in lines:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")

    # event journal: one real event + its duplicate reset (dedup) + a second pair
    events = [
        {"at": NOW - 26 * 3600, "reset": NOW - 2 * 3600, "account": "acct-b",
         "model": "model-y", "realm": "cn", "sample": 450000},
        {"at": NOW - 25.5 * 3600, "reset": NOW - 2 * 3600, "account": "acct-b",
         "model": "model-y", "realm": "cn", "sample": 460000},
        {"at": NOW - 5 * 3600, "reset": NOW + 6 * 3600, "account": "acct-c",
         "model": "model-x", "realm": "intl", "sample": 310000},
    ]
    with io.open(os.path.join(usage_dir, "limit-events.jsonl"), "w", encoding="utf-8",
                 newline="\n") as fh:
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    daily = {"schema": 1, "days": {
        YESTERDAY: {"budgets": {
            "cn\x1fmodel-y": {"avg": 520000, "min": 480000, "max": 560000, "n": 2},
            "intl\x1fmodel-x": {"avg": 300000, "min": 290000, "max": 310000, "n": 1},
        }, "segments": {}},
    }}
    with io.open(os.path.join(usage_dir, "estimate-daily.json"), "w", encoding="utf-8",
                 newline="\n") as fh:
        fh.write(json.dumps(daily, ensure_ascii=False))

    accounts = [
        {"uid": "acct-a", "nickname": "Alpha", "realm": "intl", "enabled": True,
         "modelCaps": {"model-x": {"at": NOW - 2 * 3600, "reset": NOW + 2 * 3600}}},
        {"uid": "acct-b", "nickname": "Beta", "realm": "cn", "enabled": True,
         "modelCaps": {"model-y": {"at": NOW - 26 * 3600, "reset": NOW - 2 * 3600}}},
        {"uid": "acct-c", "nickname": "Gamma", "realm": "intl", "enabled": True},
    ]
    for account in accounts:
        with io.open(os.path.join(acct_dir, account["uid"] + ".json"), "w",
                     encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(account, ensure_ascii=False, indent=1))
    print(json.dumps({"out": OUT, "rows": len(lines), "now": NOW,
                      "today": TODAY, "yesterday": YESTERDAY}))


if __name__ == "__main__":
    main()
