"""Per-API-key attribution on the metrics page.

Every usage row now records which configured key the caller presented, and
/usage/analytics folds the same rows a second time by that key. This pins the
parts that are easy to get subtly wrong:

  * the unattributed buckets are told apart. A row written before the field
    existed, a row from a deployment that never configured a key, and an id
    the roster no longer knows are three different things: the first can only
    shrink, the second keeps growing for as long as the deployment runs
    keyless, and the third is a broken settings file. Merging them would hide
    a growing leak behind a shrinking legacy tail;
  * the table adds up. The per-key rows and the per-account rows are two
    views of one log, so their request counts must match exactly - that is
    the whole point of showing a spend breakdown;
  * a key the panel can still see gets a row even with no traffic (otherwise
    "no usage" and "no such key" look identical), while a disabled key that
    saw nothing in the window does not;
  * the per-key model list is capped, because a 50-key deployment would
    otherwise ship thousands of pills to a page that repaints every 5s;
  * deleting a key in the panel is a soft delete: the secret is wiped, the id
    survives, and the history stays readable.

No network: the usage log and the settings file are synthesised in a temp
directory.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-bykey-")
# WB_PROXY_USAGE_DIR is read at import, but ACCOUNTS_DIR is not configurable
# that way - wb_proxy hard-codes it next to the script - so setting the
# environment variable for it does nothing and it has to be rebound below.
# This test writes settings, so getting that wrong would drop its fixtures
# into the real accounts/settings.json, where the panel would show them.
os.environ["WB_PROXY_USAGE_DIR"] = _TMP

import wb_proxy as P
import wb_settings as S

P.ACCOUNTS_DIR = os.path.join(_TMP, "accounts")
os.makedirs(P.ACCOUNTS_DIR, exist_ok=True)

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


# --------------------------------------------------------------------------
# 1. The writers put the key id on the row, always.
# --------------------------------------------------------------------------
P.record_usage("m-1", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
               key="k-writer")
P.record_usage("m-1", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
P.record_error("m-1", 500, "boom", key="k-writer")
P.record_error("m-1", 500, "boom")

with io.open(P.USAGE_LOG, encoding="utf-8") as fh:
    written = [json.loads(line) for line in fh if line.strip()]

check("a successful row records the key that called", written[0].get("key") == "k-writer")
check("a keyless success still writes the field", "key" in written[1] and written[1]["key"] == "")
check("a failed row records the key too", written[2].get("key") == "k-writer")
check("a keyless failure still writes the field", "key" in written[3] and written[3]["key"] == "")
check("the new field is appended, so older readers keep working",
      list(written[0].keys()).index("key") > list(written[0].keys()).index("realm"))


# --------------------------------------------------------------------------
# 2. Soft delete in the settings layer.
# --------------------------------------------------------------------------
_d = os.path.join(_TMP, "settings-accounts")
os.makedirs(_d, exist_ok=True)
S.set_api_keys(_d, [
    {"id": "k1", "name": "甲", "key": "key-alpha", "realm": "cn"},
    {"id": "k2", "name": "乙", "key": "key-beta", "realm": ""},
])
S.set_api_keys(_d, [{"id": "k1", "name": "甲改名", "key": "key-alpha", "realm": "cn"}])

live = S.api_keys(_d)
check("a key removed from the list stops being live", [e["id"] for e in live] == ["k1"])
check("renaming keeps the id, so the history stays attached",
      live[0]["name"] == "甲改名" and live[0]["id"] == "k1")

everything = S.api_keys(_d, include_deleted=True)
tomb = [e for e in everything if e["id"] == "k2"]
check("the removed key keeps a row", len(tomb) == 1, everything)
check("its name and creation date survive",
      bool(tomb) and tomb[0]["name"] == "乙" and tomb[0]["created_at"])
check("its secret is wiped", bool(tomb) and tomb[0]["key"] == "")
check("it is marked disabled and dated",
      bool(tomb) and tomb[0]["enabled"] is False and bool(tomb[0]["deleted_at"]))
check("a deleted key cannot authenticate", S.match_api_key(_d, "key-beta") is None)
check("a live key still can", (S.match_api_key(_d, "key-alpha") or {}).get("id") == "k1")
check("two tombstones do not collapse into one",
      len(S.api_keys(_d, include_deleted=True)) == 2)
check("saving again does not resurrect the tombstone",
      [e["id"] for e in S.set_api_keys(_d, [{"id": "k1", "name": "甲改名",
                                             "key": "key-alpha", "realm": "cn"}])] == ["k1"])


# --------------------------------------------------------------------------
# 3. The analytics payload.
# --------------------------------------------------------------------------
NOW = time.time()


def row(account, key, model="m-1", prompt=100, completion=50, realm="intl", tokens=None):
    r = {
        "at": NOW, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW)),
        "model": model, "stream": True, "outcome": "completed",
        "elapsed_ms": 1000, "ttft_ms": 300, "gen_ms": 700,
        "prompt_tokens": prompt, "completion_tokens": completion,
        "reasoning_tokens": 0, "cached_tokens": 0,
        "total_tokens": tokens if tokens is not None else prompt + completion,
        "credit": 1.5, "account": account, "realm": realm,
    }
    if key is not None:
        r["key"] = key
    return r


rows = [
    row("acct-A", "k1"),
    row("acct-A", "k1", model="m-2"),
    row("acct-B", "k2"),                      # configured but disabled
    row("acct-B", ""),                        # keyless deployment
    row("acct-B", None),                      # written before the field existed
    row("acct-B", "ghost"),                   # id the roster does not know
    row("acct-A", "launcher"),                # the --api-key launcher key
    row("acct-A", "", realm="cn"),            # keyless, other exit
]
# A key that spans both exits, and one with more models than the cap.
rows += [row("acct-C", "k-follow", realm="intl"), row("acct-C", "k-follow", realm="cn")]
rows += [row("acct-D", "k-wide", model="m-%d" % i, tokens=(900 - i * 100)) for i in range(1, 8)]

with io.open(P.USAGE_LOG, "w", encoding="utf-8") as fh:
    for r in rows:
        fh.write(json.dumps(r, ensure_ascii=False) + "\n")

S.set_api_keys(P.ACCOUNTS_DIR, [
    {"id": "k1", "name": "甲", "key": "key-alpha", "realm": "cn", "enabled": True},
    {"id": "k2", "name": "乙", "key": "key-beta", "realm": "", "enabled": False},
    {"id": "k-idle", "name": "闲", "key": "key-idle", "realm": "intl", "enabled": True},
    {"id": "k-follow", "name": "跟", "key": "key-follow", "realm": "", "enabled": True},
    {"id": "k-wide", "name": "宽", "key": "key-wide", "realm": "", "enabled": True},
])
P.API_KEY = "launcher-secret"

data = P._compute_usage_analytics_uncached()
by_key = {k["key"]: k for k in data["keys"]}

check("the payload carries a key axis", "keys" in data and len(data["keys"]) == len(by_key))
check("a used key is attributed", by_key["k1"]["window"]["requests"] == 2)
check("its tokens follow", by_key["k1"]["window"]["total_tokens"] == 300)
check("its model breakdown is split per model",
      sorted(m["model"] for m in by_key["k1"]["models"]) == ["m-1", "m-2"])
check("a configured key with no traffic still gets a row (0 is not 'missing')",
      by_key["k-idle"]["window"]["requests"] == 0 and by_key["k-idle"]["name"] == "闲")
check("a disabled key with traffic in the window is kept",
      by_key["k2"]["window"]["requests"] == 1 and by_key["k2"]["enabled"] is False)
check("rows written before the field existed are their own bucket",
      by_key[P.KEY_BUCKET_BEFORE]["window"]["requests"] == 1)
check("a keyless deployment is its own bucket, not the legacy one",
      by_key[P.KEY_BUCKET_ANON]["window"]["requests"] == 2)
check("an id the roster cannot name is folded into one row",
      by_key[P.KEY_BUCKET_UNKNOWN]["window"]["requests"] == 1)
check("the launcher key gets a row when it is configured",
      by_key["launcher"]["window"]["requests"] == 1 and by_key["launcher"]["name"] == "启动参数")
check("a key used on both exits is flagged",
      by_key["k-follow"]["cross_realm"] is True
      and sorted(by_key["k-follow"]["realms"]) == ["cn", "intl"])
check("a key that only ever used one exit is not",
      by_key["k1"]["cross_realm"] is False)
check("the model list is capped", len(by_key["k-wide"]["models"]) == P.KEY_MODEL_TOP_N)
check("the cap is accounted for rather than dropped",
      by_key["k-wide"]["models_other"]["models"] == 2
      and by_key["k-wide"]["models_other"]["tokens"] == 300 + 200)
check("the busiest models survive the cap",
      [m["model"] for m in by_key["k-wide"]["models"]] == ["m-1", "m-2", "m-3", "m-4", "m-5"])

key_reqs = sum(k["window"]["requests"] for k in data["keys"])
acct_reqs = sum(a["window"]["requests"] for a in data["accounts"])
check("the per-key table adds up to the per-account table", key_reqs == acct_reqs,
      (key_reqs, acct_reqs))
check("and to the window summary itself", key_reqs == data["summary"]["window"]["requests"])
check("every row the page renders is named", all(k.get("name") for k in data["keys"]))
# The handler serialises this straight to the wire; a non-serialisable value
# (a set, a datetime) would turn the whole metrics page into a 500.
_wire = json.dumps(data, ensure_ascii=False)
check("the payload survives the trip to the browser",
      len(json.loads(_wire)["keys"]) == len(data["keys"]))

# The realm view must not invent a row for a key bound to the other exit.
only_cn = P._compute_usage_analytics_uncached(realm="cn")
check("a key bound to the other exit is not listed in this view",
      "k-idle" not in [k["key"] for k in only_cn["keys"]])
check("a key with no binding is still listed, it can serve this exit",
      "k-follow" in [k["key"] for k in only_cn["keys"]])

# An older panel that never asks about keys must keep getting a valid payload.
legacy_view = {k: v for k, v in data.items() if k != "keys"}
check("dropping the new axis leaves the old payload intact",
      set(legacy_view) == {"window", "realm", "summary", "accounts", "models"})

shutil.rmtree(_TMP, ignore_errors=True)
print("\n  SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
