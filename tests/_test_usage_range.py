"""Deterministic tests for the dashboard time-range filter (issue #39).

The metrics page reports "today" and "all time" side by side. The range
selector used to drive only the KPI cards, so the model matrix kept showing
all-time figures while the page claimed today; /usage and /usage/perf now
accept a range parameter and this pins its semantics.

No network: the usage log is synthesised in a temp directory.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-range-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_proxy as P

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


def row(at, model, account, prompt, completion, realm="intl"):
    return {
        "at": at, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at)),
        "model": model, "stream": True, "outcome": "completed",
        "elapsed_ms": 1200, "ttft_ms": 400, "gen_ms": 800,
        "prompt_tokens": prompt, "completion_tokens": completion,
        "reasoning_tokens": 0, "cached_tokens": 0,
        "total_tokens": prompt + completion, "credit": 0,
        "account": account, "realm": realm, "tokens_per_sec": 120.0,
        "cache_hit_pct": 0.0,
    }


now = time.time()
_lt = time.localtime(now)
TODAY0 = time.mktime((_lt.tm_year, _lt.tm_mon, _lt.tm_mday, 0, 0, 0, 0, 0, -1))
YESTERDAY = TODAY0 - 86400

rows = [
    row(YESTERDAY + 3600, "deepseek-v4.1-flash", "acct-A", 4000, 1000),
    row(YESTERDAY + 7200, "glm-5.3", "acct-B", 2000, 500),
    row(TODAY0 + 3600, "deepseek-v4.1-flash", "acct-A", 300, 100),
    row(TODAY0 + 7200, "deepseek-v4.1-flash", "acct-A", 300, 100),
]
with io.open(P.USAGE_LOG, "w", encoding="utf-8") as fh:
    for r in rows:
        fh.write(json.dumps(r, ensure_ascii=False) + chr(10))

print("[1] range_since: only today is a filter, everything else is history")
check("today maps to local midnight", P.range_since("today") == TODAY0,
      (P.range_since("today"), TODAY0))
check("Today is accepted case-insensitively", P.range_since("TODAY") == TODAY0)
check("1d is an alias", P.range_since("1d") == TODAY0)
check("all disables the filter", P.range_since("all") is None)
check("an empty value disables the filter", P.range_since("") is None)
check("a missing value disables the filter", P.range_since(None) is None)
check("an unknown value disables the filter", P.range_since("last-week") is None)

print()
print("[2] /usage: the window applies to every total it reports")
allr = P.usage_snapshot(realm="all", ttl=0)
check("no range still reports the whole log", allr["requests"] == 4, allr["requests"])
check("no range totals 8300 tokens", allr["total_tokens"] == 8300, allr["total_tokens"])
check("no range sees both models",
      {"deepseek-v4.1-flash", "glm-5.3"} <= set(allr["by_model"]), list(allr["by_model"]))

today = P.usage_snapshot(realm="all", ttl=0, range="today")
check("today counts only today's requests", today["requests"] == 2, today["requests"])
check("today totals only today's tokens", today["total_tokens"] == 800,
      today["total_tokens"])
check("today drops the model that only ran yesterday",
      "glm-5.3" not in today["by_model"], list(today["by_model"]))
check("today keeps the model that ran today",
      "deepseek-v4.1-flash" in today["by_model"])
check("today drops the account that only ran yesterday",
      not any("acct-B" in r for r in (today.get("by_model_acct") or {}).values()),
      today.get("by_model_acct"))

print()
print("[3] /usage/perf: latency and speed describe the same window")
perf_all = P.perf_stats(5000, realm="all", ttl=0)
perf_today = P.perf_stats(5000, realm="all", ttl=0, range="today")
check("unfiltered perf samples the whole log", perf_all["sampled"] == 4,
      perf_all["sampled"])
check("today perf samples only today", perf_today["sampled"] == 2,
      perf_today["sampled"])
check("today perf drops yesterday-only models",
      "glm-5.3" not in (perf_today.get("by_model") or {}),
      list(perf_today.get("by_model") or {}))

print()
print("[4] the two ranges are cached separately")
P.usage_snapshot(realm="all", ttl=60)
a = P.usage_snapshot(realm="all", ttl=60)
b = P.usage_snapshot(realm="all", ttl=60, range="today")
check("a cached all-range read is not served for today", a["requests"] != b["requests"],
      (a["requests"], b["requests"]))
check("the cached entries kept their own values",
      a["requests"] == 4 and b["requests"] == 2, (a["requests"], b["requests"]))

print()
print("[5] the analytics payload keeps its own today/all split")
an = P.compute_usage_analytics(ttl=0)
check("analytics today matches the range filter",
      an["summary"]["today"]["total_tokens"] == 800,
      an["summary"]["today"]["total_tokens"])
check("analytics all_time matches the unfiltered snapshot",
      an["summary"]["all_time"]["total_tokens"] == 8300,
      an["summary"]["all_time"]["total_tokens"])

shutil.rmtree(_TMP, ignore_errors=True)
print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
