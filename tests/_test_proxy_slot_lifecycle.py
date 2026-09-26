"""Slot ids must never be recycled onto a different slot.

Accounts persist the slot id they are bound to. If a removed id could be
handed to a newly added slot, that account would silently start using the new
slot's exit IP - the panel would show a fresh slot while an existing account's
route changed underneath it. Nothing would look broken, which is what makes
it worth a test.
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_settings

PASS = 0
FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (label, detail))


def make_store():
    return tempfile.mkdtemp(prefix="slotlife_")


def account(uid, slot):
    return wb_accounts.Account({
        "uid": uid, "nickname": uid, "domain": "www.workbuddy.ai",
        "realm": "intl", "accessToken": "a.b.c", "refreshToken": "r",
        "expiresAt": 4102444800, "enabled": True, "proxySlot": slot,
    })


# ---- [1] deleting a slot releases the accounts bound to it --------------
print("[1] deleting a slot unbinds its accounts")
work = make_store()
slots = wb_settings.set_proxy_slots(work, [
    {"id": "", "name": "A", "url": "http://10.0.0.1:17901", "enabled": True},
    {"id": "", "name": "B", "url": "http://10.0.0.2:17902", "enabled": True},
])
ids = [s["id"] for s in slots]
check("ids were assigned", ids == ["slot-1", "slot-2"], repr(ids))

pool = wb_accounts.AccountPool(work)
pool.add(account("u1", "slot-2"))
pool.apply_proxy_slots(slots)
check("account bound to slot-2 uses its url",
      pool.accounts[0].proxy == "http://10.0.0.2:17902", pool.accounts[0].proxy)

remaining = wb_settings.set_proxy_slots(work, [slots[0]])
dropped = wb_settings.drop_missing_bindings(pool, remaining)
pool.apply_proxy_slots(remaining)
check("the stale binding was dropped", dropped == 1, str(dropped))
check("account fell back to direct", pool.accounts[0].proxy == "", pool.accounts[0].proxy)
check("binding cleared on disk", pool.accounts[0].proxy_slot == "")
shutil.rmtree(work, ignore_errors=True)

# ---- [2] a removed id is never reused, even within one save -------------
print("[2] a freed id is not reused by a newly added slot")
work = make_store()
wb_settings.set_proxy_slots(work, [
    {"id": "", "name": "A", "url": "http://10.0.0.1:17901", "enabled": True},
    {"id": "", "name": "B", "url": "http://10.0.0.2:17902", "enabled": True},
])
pool = wb_accounts.AccountPool(work)
pool.add(account("u1", "slot-2"))

# Drop slot-2 and add a replacement in the same save - the case where
# "highest surviving + 1" handed slot-2 back out.
new_list = wb_settings.set_proxy_slots(work, [
    {"id": "slot-1", "name": "A", "url": "http://10.0.0.1:17901", "enabled": True},
    {"id": "", "name": "C", "url": "http://10.0.0.9:17909", "enabled": True},
])
new_ids = [s["id"] for s in new_list]
check("new slot did not take the freed id", "slot-2" not in new_ids, repr(new_ids))

dropped = wb_settings.drop_missing_bindings(pool, new_list)
pool.apply_proxy_slots(new_list)
check("the stale binding was dropped", dropped == 1, str(dropped))
check("account did not adopt the new slot",
      pool.accounts[0].proxy != "http://10.0.0.9:17909", pool.accounts[0].proxy)
check("account is direct", pool.accounts[0].proxy == "", pool.accounts[0].proxy)
shutil.rmtree(work, ignore_errors=True)

# ---- [3] the counter survives a reload and keeps growing ----------------
print("[3] the counter is persisted and only grows")
work = make_store()
first = wb_settings.set_proxy_slots(work, [
    {"id": "", "name": "A", "url": "http://10.0.0.1:17901", "enabled": True},
])
check("first id is slot-1", first[0]["id"] == "slot-1", first[0]["id"])
seen = {first[0]["id"]}
for n in range(3):
    again = wb_settings.set_proxy_slots(work, [
        {"id": "", "name": "N%d" % n,
         "url": "http://10.0.0.%d:17999" % (n + 5), "enabled": True},
    ])
    seen.add(again[0]["id"])
check("every generation got a distinct id", len(seen) == 4, repr(sorted(seen)))
shutil.rmtree(work, ignore_errors=True)

# ---- [4] bulk disable releases slots, like single disable ----------------
print("[4] set-all-disabled releases slots")
work = make_store()
slots = wb_settings.set_proxy_slots(work, [
    {"id": "", "name": "A", "url": "http://10.0.0.1:17901", "enabled": True},
])
pool = wb_accounts.AccountPool(work)
pool.add(account("u1", slots[0]["id"]))
pool.add(account("u2", slots[0]["id"]))
pool.apply_proxy_slots(slots)
check("both accounts use the slot",
      all(a.proxy == "http://10.0.0.1:17901" for a in pool.accounts))

pool.set_all_enabled(False)
check("all slots released on bulk disable",
      all(a.proxy_slot == "" for a in pool.accounts),
      repr([a.proxy_slot for a in pool.accounts]))
check("runtime proxy cleared too",
      all(a.proxy == "" for a in pool.accounts),
      repr([a.proxy for a in pool.accounts]))
shutil.rmtree(work, ignore_errors=True)

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
