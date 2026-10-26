"""Deterministic tests for the dashboard time-range filter (issues #39, #68).

The metrics page reports the selected window and all time side by side. The
range selector used to drive only the KPI cards, so the model matrix kept
showing all-time figures while the page claimed today; /usage and /usage/perf
now accept a range parameter and this pins its semantics.

The selector also grew past "today / all time" (issue #68): this week, this
month and a custom interval. Two things matter for those and are pinned here:
the bounds are calendar windows anchored to local midnight, and the resolved
bounds - not a today/all flag - key the caches, because this week and this
month overlap and one entry cannot describe both.

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

import wb_usage as U

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

print("[1] range_window: today/week/month are calendar windows, the rest is history")
check("today maps to local midnight", U.range_window("today") == (TODAY0, None),
      (U.range_window("today"), (TODAY0, None)))
check("Today is accepted case-insensitively", U.range_window("TODAY")[0] == TODAY0)
check("1d is an alias", U.range_window("1d")[0] == TODAY0)
check("week starts on Monday at local midnight",
      time.localtime(U.range_window("week")[0]).tm_wday == 0
      and time.localtime(U.range_window("week")[0]).tm_hour == 0,
      time.strftime("%Y-%m-%d %H:%M", time.localtime(U.range_window("week")[0])))
check("the week window starts no later than today", U.range_window("week")[0] <= TODAY0,
      (U.range_window("week")[0], TODAY0))
check("month starts on the 1st at local midnight",
      time.localtime(U.range_window("month")[0]).tm_mday == 1
      and time.localtime(U.range_window("month")[0]).tm_hour == 0,
      time.strftime("%Y-%m-%d %H:%M", time.localtime(U.range_window("month")[0])))
check("the month window starts no later than today", U.range_window("month")[0] <= TODAY0,
      (U.range_window("month")[0], TODAY0))
check("an open-ended window has no upper bound", U.range_window("week")[1] is None)
check("all disables the filter", U.range_window("all") == (None, None))
check("an empty value disables the filter", U.range_window("") == (None, None))
check("a missing value disables the filter", U.range_window(None) == (None, None))
check("an unknown value disables the filter", U.range_window("last-week") == (None, None))
check("7d is not silently read as this week", U.range_window("7d") == (None, None))
check("custom takes both bounds", U.range_window("custom", 100, 200) == (100, 200))
check("custom accepts one open side", U.range_window("custom", 100, None) == (100, None))
check("custom swaps reversed bounds", U.range_window("custom", 200, 100) == (100, 200))
check("custom drops an unparseable bound", U.range_window("custom", "abc", 200) == (None, 200))
check("custom drops a negative bound", U.range_window("custom", -5, None) == (None, None))

print()
print("[2] /usage: the window applies to every total it reports")
allr = U.usage_snapshot(realm="all", ttl=0)
check("no range still reports the whole log", allr["requests"] == 4, allr["requests"])
check("no range totals 8300 tokens", allr["total_tokens"] == 8300, allr["total_tokens"])
check("no range sees both models",
      {"deepseek-v4.1-flash", "glm-5.3"} <= set(allr["by_model"]), list(allr["by_model"]))

today = U.usage_snapshot(realm="all", ttl=0, range="today")
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
perf_all = U.perf_stats(5000, realm="all", ttl=0)
perf_today = U.perf_stats(5000, realm="all", ttl=0, range="today")
check("unfiltered perf samples the whole log", perf_all["sampled"] == 4,
      perf_all["sampled"])
check("today perf samples only today", perf_today["sampled"] == 2,
      perf_today["sampled"])
check("today perf drops yesterday-only models",
      "glm-5.3" not in (perf_today.get("by_model") or {}),
      list(perf_today.get("by_model") or {}))

print()
print("[4] the two ranges are cached separately")
U.usage_snapshot(realm="all", ttl=60)
a = U.usage_snapshot(realm="all", ttl=60)
b = U.usage_snapshot(realm="all", ttl=60, range="today")
check("a cached all-range read is not served for today", a["requests"] != b["requests"],
      (a["requests"], b["requests"]))
check("the cached entries kept their own values",
      a["requests"] == 4 and b["requests"] == 2, (a["requests"], b["requests"]))

print()
print("[5] a custom window is inclusive on both ends")
pair = U.usage_snapshot(realm="all", ttl=0, range="custom",
                        since=TODAY0 + 3600, until=TODAY0 + 7200)
check("both ends are inside the window", pair["requests"] == 2, pair["requests"])
check("the lower bound is inclusive",
      U.usage_snapshot(realm="all", ttl=0, range="custom",
                       since=TODAY0 + 3600)["requests"] == 2)
check("a single instant matches only the row at that instant",
      U.usage_snapshot(realm="all", ttl=0, range="custom",
                       since=TODAY0 + 3600, until=TODAY0 + 3600)["requests"] == 1)
check("a window past every row counts nothing",
      U.usage_snapshot(realm="all", ttl=0, range="custom",
                       since=TODAY0 + 99999)["requests"] == 0)
check("an open start reaches back to the log's first row",
      U.usage_snapshot(realm="all", ttl=0, range="custom",
                       until=TODAY0 + 3600)["requests"] == 3)

print()
print("[6] week and month filter the same way, and do not share a cache entry")
WEEK0 = U.range_window("week")[0]
MONTH0 = U.range_window("month")[0]
want_week = sum(1 for r in rows if r["at"] >= WEEK0)
want_month = sum(1 for r in rows if r["at"] >= MONTH0)
week = U.usage_snapshot(realm="all", ttl=0, range="week")
month = U.usage_snapshot(realm="all", ttl=0, range="month")
check("week counts every row from its Monday on", week["requests"] == want_week,
      (week["requests"], want_week))
check("month counts every row from the 1st on", month["requests"] == want_month,
      (month["requests"], want_month))
# Overlapping windows are the trap the old today/all cache key could not
# express: a shared entry would serve one window's totals under the other's
# label, and the numbers below would come out identical.
cached_week = U.usage_snapshot(realm="all", ttl=60, range="week")
cached_month = U.usage_snapshot(realm="all", ttl=60, range="month")
check("a cached week read is not served for month",
      cached_week["requests"] == want_week and cached_month["requests"] == want_month,
      (cached_week["requests"], want_week, cached_month["requests"], want_month))

print()
print("[7] the analytics payload labels its first column with the selected window")
an = U.compute_usage_analytics(ttl=0, range="today")
an_week = U.compute_usage_analytics(ttl=0, range="week")
an_all = U.compute_usage_analytics(ttl=0, range="all")
check("the payload reports the bounds it applied",
      an_week["window"] == {"since": WEEK0, "until": None}, an_week["window"])
check("analytics window matches the range filter",
      an["summary"]["window"]["total_tokens"] == 800,
      an["summary"]["window"]["total_tokens"])
check("analytics all_time matches the unfiltered snapshot",
      an["summary"]["all_time"]["total_tokens"] == 8300,
      an["summary"]["all_time"]["total_tokens"])
check("the week bucket follows the week window",
      an_week["summary"]["window"]["total_tokens"]
      == sum(r["total_tokens"] for r in rows if r["at"] >= WEEK0),
      an_week["summary"]["window"]["total_tokens"])
check("all_time is the same figure in every window",
      an_week["summary"]["all_time"]["total_tokens"]
      == an_all["summary"]["all_time"]["total_tokens"] == 8300,
      (an_week["summary"]["all_time"]["total_tokens"], an_all["summary"]["all_time"]["total_tokens"]))
check("accounts carry a window bucket",
      all("window" in a and "window_models" in a for a in an_week["accounts"]),
      [sorted(a.keys()) for a in an_week["accounts"]][:1])
check("the old today bucket is gone from accounts",
      all("today" not in a for a in an_week["accounts"]))

print()
print("[8] perf reports how far its sample reaches into the window")
perf_small = U.perf_stats(3, realm="all", ttl=0)
perf_big = U.perf_stats(5000, realm="all", ttl=0)
check("a capped sample says so", perf_small["sample_capped"] is True,
      perf_small["sample_capped"])
check("a capped sample reports where it starts", perf_small["sample_from"] is not None,
      perf_small["sample_from"])
check("an uncapped sample is not flagged", perf_big["sample_capped"] is False,
      perf_big["sample_capped"])
check("the sample still describes the window",
      U.perf_stats(3, realm="all", ttl=0, range="today")["sampled"] == 2,
      U.perf_stats(3, realm="all", ttl=0, range="today")["sampled"])

shutil.rmtree(_TMP, ignore_errors=True)
print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
