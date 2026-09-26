"""Deterministic tests for proxy-slot definitions (no network)."""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_settings as S

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


print("[1] proxy_slots: empty by default, cleaned on read")
d = tempfile.mkdtemp(prefix="wb-slots-")
check("no key -> empty list", S.proxy_slots(d) == [])

S.save(
    d,
    {
        "proxy_slots": [
            {"id": "slot-1", "name": "槽 1", "url": "http://a:1", "enabled": True},
            {"id": "", "name": "auto", "url": "http://b:2"},
            {"id": "slot-2", "url": "  "},  # no url -> dropped
            {"id": "slot-3", "name": "", "url": "socks5://c:3", "enabled": False},
            "junk",  # not a dict -> dropped
        ]
    },
)
slots = S.proxy_slots(d)
check(
    "junk/empty-url entries dropped",
    [s["url"] for s in slots] == ["http://a:1", "http://b:2", "socks5://c:3"],
    slots,
)
check("blank id auto-assigned", slots[1]["id"] == "slot-2", slots[1]["id"])
check("missing enabled defaults True", slots[1]["enabled"] is True)
check("name preserved when present", slots[1]["name"] == "auto", slots[1]["name"])
check("name defaults to id when blank", slots[2]["name"] == "slot-3", slots[2]["name"])
check("enabled False preserved", slots[2]["enabled"] is False)
check("url trimmed", slots[0]["url"] == "http://a:1")

print()
print("[2] set_proxy_slots: assigns ids, dedupes, persists")
saved = S.set_proxy_slots(
    d,
    [
        {"name": "one", "url": "http://x:1"},
        {"name": "two", "url": "http://x:2"},
        {"name": "dup", "url": "http://x:1"},
    ],
)
check("two unique slots kept", len(saved) == 2, saved)
check("ids auto-assigned", all(s["id"].startswith("slot-") for s in saved), saved)
check("ids unique", len({s["id"] for s in saved}) == 2, saved)
again = S.proxy_slots(d)
check(
    "persisted and reloaded",
    [s["url"] for s in again] == ["http://x:1", "http://x:2"],
    again,
)
check("names preserved", [s["name"] for s in again] == ["one", "two"], again)

print()
print("[3] new ids never collide with existing ones")
saved2 = S.set_proxy_slots(d, again + [{"name": "three", "url": "http://x:3"}])
ids = [s["id"] for s in saved2]
check("existing ids stable", ids[0] == again[0]["id"] and ids[1] == again[1]["id"], ids)
check("new id is fresh", len(set(ids)) == 3, ids)

print()
print("[4] find_proxy_slot")
check(
    "finds by id",
    (S.find_proxy_slot(d, saved2[0]["id"]) or {}).get("url") == "http://x:1",
)
check("unknown id -> None", S.find_proxy_slot(d, "slot-999") is None)
check("empty id -> None", S.find_proxy_slot(d, "") is None)

print()
print("[5] Account: proxySlot persisted, proxy is resolved runtime value")
import wb_accounts as A

d2 = tempfile.mkdtemp(prefix="wb-slots-acct-")
S.set_proxy_slots(d2, [{"id": "slot-1", "name": "one", "url": "http://slot:1"}])

acc = A.Account(
    {
        "uid": "u-1",
        "domain": "www.workbuddy.ai",
        "realm": "intl",
        "proxySlot": "slot-1",
        "proxy": "http://legacy:9",
    }
)
check("proxy_slot loaded", acc.proxy_slot == "slot-1", acc.proxy_slot)
check("proxy_legacy loaded", acc.proxy_legacy == "http://legacy:9", acc.proxy_legacy)
check("proxy starts as legacy before apply", acc.proxy == "http://legacy:9", acc.proxy)

pool = A.AccountPool(d2, log=lambda m: None)
pool.add(acc)
pool.apply_proxy_slots()
check("apply resolves slot url", acc.proxy == "http://slot:1", acc.proxy)
view = acc.public()
check(
    "public exposes proxySlot", view.get("proxySlot") == "slot-1", view.get("proxySlot")
)
check(
    "public exposes resolved proxy",
    view.get("proxy") == "http://slot:1",
    view.get("proxy"),
)

print()
print("[6] resolution priority: enabled slot > legacy > direct")
S.set_proxy_slots(
    d2, [{"id": "slot-1", "name": "one", "url": "http://slot:1", "enabled": False}]
)
pool.apply_proxy_slots()
check("disabled slot falls back to legacy", acc.proxy == "http://legacy:9", acc.proxy)

S.set_proxy_slots(
    d2, [{"id": "slot-1", "name": "one", "url": "http://slot:1", "enabled": True}]
)
pool.apply_proxy_slots()
check("re-enabled slot wins again", acc.proxy == "http://slot:1", acc.proxy)

acc_nolegacy = A.Account(
    {
        "uid": "u-2",
        "domain": "www.workbuddy.ai",
        "realm": "intl",
        "proxySlot": "slot-1",
    }
)
pool.add(acc_nolegacy)
S.set_proxy_slots(d2, [])
pool.apply_proxy_slots()
check("no slot + no legacy -> direct", acc_nolegacy.proxy == "", acc_nolegacy.proxy)
check("legacy-only account keeps legacy", acc.proxy == "http://legacy:9", acc.proxy)

print()
print("[7] set_proxy_slot persists and re-resolves")
S.set_proxy_slots(d2, [{"id": "slot-2", "name": "two", "url": "http://slot:2"}])
view2 = pool.set_proxy_slot("u-2", "slot-2")
check("set_proxy_slot returns view", (view2 or {}).get("proxySlot") == "slot-2", view2)
check(
    "runtime proxy updated", acc_nolegacy.proxy == "http://slot:2", acc_nolegacy.proxy
)
reloaded = A.AccountPool(d2, log=lambda m: None)
reloaded.load()
reloaded.apply_proxy_slots()
r2 = reloaded.get("u-2")
check(
    "proxySlot survives reload",
    r2 and r2.proxy_slot == "slot-2",
    r2.proxy_slot if r2 else None,
)
check(
    "resolved after reload",
    r2 and r2.proxy == "http://slot:2",
    r2.proxy if r2 else None,
)
check("unknown uid -> None", pool.set_proxy_slot("nope", "slot-2") is None)
check(
    "empty slot id clears binding",
    (pool.set_proxy_slot("u-2", "") or {}).get("proxySlot") == "",
)

print()
print("[8] proxy_slots_view counts only enabled accounts")
import importlib

os.environ.setdefault("ACCOUNTS_DIR", d2)
os.environ.setdefault("USAGE_DIR", tempfile.mkdtemp(prefix="wb-slots-use-"))
P = importlib.import_module("wb_proxy")
P.ACCOUNTS_DIR = d2
P.POOL = pool
S.set_proxy_slots(d2, [{"id": "slot-2", "name": "two", "url": "http://slot:2"}])
pool.set_proxy_slot("u-2", "slot-2")
pool.set_proxy_slot("u-1", "slot-2")
view = P.proxy_slots_view()
by_id = {v["id"]: v for v in view}
check("view lists slot-2", "slot-2" in by_id, list(by_id))
check(
    "bound count reflects two accounts", by_id["slot-2"]["bound"] == 2, by_id["slot-2"]
)
check("view has url", by_id["slot-2"]["url"] == "http://slot:2")

print()
print("[9] disabling an account releases its slot")
pool.set_enabled("u-1", False)
u1 = pool.get("u-1")
check("disabled account proxySlot cleared", u1.proxy_slot == "", repr(u1.proxy_slot))
check(
    "disabled account falls back to legacy (not slot)",
    u1.proxy == "http://legacy:9",
    repr(u1.proxy),
)
check(
    "disabled account no longer counted",
    {v["id"]: v for v in P.proxy_slots_view()}["slot-2"]["bound"] == 1,
    P.proxy_slots_view(),
)
check("enabled account keeps its slot", pool.get("u-2").proxy_slot == "slot-2")

pool.set_enabled("u-1", True)
check(
    "re-enabled account starts unbound",
    pool.get("u-1").proxy_slot == "",
    pool.get("u-1").proxy_slot,
)

print()
print("[10] load migrates: disabled account holding a slot releases it")
d3 = tempfile.mkdtemp(prefix="wb-slots-migrate-")
S.set_proxy_slots(d3, [{"id": "slot-1", "name": "one", "url": "http://slot:1"}])
mig = A.Account(
    {
        "uid": "u-mig",
        "domain": "www.workbuddy.ai",
        "realm": "intl",
        "proxySlot": "slot-1",
        "enabled": False,
    }
)
mig.save(d3)
pool3 = A.AccountPool(d3, log=lambda m: None)
pool3.load()
pool3.apply_proxy_slots()
m3 = pool3.get("u-mig")
check("stale slot cleared on load", m3.proxy_slot == "", repr(m3.proxy_slot))
check("disabled account uses no slot proxy", m3.proxy == "", repr(m3.proxy))
reloaded3 = A.AccountPool(d3, log=lambda m: None)
reloaded3.load()
check(
    "cleanup persisted to disk",
    reloaded3.get("u-mig").proxy_slot == "",
    reloaded3.get("u-mig").proxy_slot,
)

print()
print("[11] selecting direct clears a stale legacy proxy")
d4 = tempfile.mkdtemp(prefix="wb-slots-direct-")
S.set_proxy_slots(d4, [{"id": "slot-1", "name": "one", "url": "http://slot:1"}])
stale = A.Account({
    "uid": "u-stale", "domain": "www.workbuddy.ai", "realm": "intl",
    "proxy": "http://stale-host:17901",
})
pool4 = A.AccountPool(d4, log=lambda m: None)
pool4.add(stale)
check("legacy proxy is active before", stale.proxy == "http://stale-host:17901", stale.proxy)
pool4.set_proxy_slot("u-stale", "")
check("explicit direct clears legacy proxy", stale.proxy_legacy == "", repr(stale.proxy_legacy))
check("runtime proxy becomes direct", stale.proxy == "", repr(stale.proxy))
reloaded4 = A.AccountPool(d4, log=lambda m: None)
reloaded4.load()
reloaded4.apply_proxy_slots()
check(
    "cleanup persisted to disk",
    reloaded4.get("u-stale").proxy_legacy == "",
    reloaded4.get("u-stale").proxy_legacy,
)

print()
print("[12] binding a slot keeps the legacy proxy as a fallback")
fresh = A.Account({
    "uid": "u-fallback", "domain": "www.workbuddy.ai", "realm": "intl",
    "proxy": "http://fallback:9",
})
pool4.add(fresh)
pool4.set_proxy_slot("u-fallback", "slot-1")
check("slot wins while enabled", fresh.proxy == "http://slot:1", fresh.proxy)
check("legacy kept for fallback", fresh.proxy_legacy == "http://fallback:9", fresh.proxy_legacy)
S.set_proxy_slots(d4, [{"id": "slot-1", "name": "one", "url": "http://slot:1", "enabled": False}])
pool4.apply_proxy_slots()
check("disabled slot falls back to legacy", fresh.proxy == "http://fallback:9", fresh.proxy)

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
