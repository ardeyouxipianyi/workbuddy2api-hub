"""API-key row ids must be unique, or the copy button copies the wrong key.

The panel only ever draws a masked key, so the copy button asks
`/settings/reveal?id=...` for the clear-text value. That lookup returns the
first row whose id matches, which meant two rows sharing an id (rows used to
be identified by their position in the submitted list) made the copy button
on the second row hand out the first row's key.

These cases pin the fix: ids minted on write are unique, a settings file that
already holds duplicates is read back with distinct ids, and a reveal by id
resolves to exactly one row. No network access required.
"""

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


def write_settings(d, entries):
    S.save(d, {"api_keys": entries})


print("[1] rows submitted without an id get distinct ids")

d = tempfile.mkdtemp(prefix="wb-keyids-")
stored = S.set_api_keys(d, [
    {"id": "", "name": "123", "key": "k-881c", "realm": "cn"},
    {"id": "", "name": "other", "key": "k-2a1c", "realm": "intl"},
])
ids = [entry["id"] for entry in stored]
check("two blank ids do not collide", len(set(ids)) == 2, ids)
check("ids are non-empty", all(ids), ids)

print()
print("[2] a new row cannot reuse the id of a row deleted earlier")

# Simulate the original bug: rows 0..2 saved, row 1 deleted, a new row appended
# at index 1. Position-derived ids handed it the id of the deleted row.
S.set_api_keys(d, [
    {"id": "k0", "name": "a", "key": "k-a"},
    {"id": "k1", "name": "b", "key": "k-b"},
    {"id": "k2", "name": "c", "key": "k-c"},
])
S.set_api_keys(d, [
    {"id": "k0", "name": "a", "key": "k-a"},
    {"id": "k2", "name": "c", "key": "k-c"},
    {"id": "", "name": "123", "key": "k-881c"},
])
reloaded = S.api_keys(d)
ids = [entry["id"] for entry in reloaded]
check("all ids stay unique", len(set(ids)) == len(ids), ids)
check("existing rows keep their ids", ids[:2] == ["k0", "k2"], ids)

print()
print("[3] a legacy file with a duplicate id is read back with unique ids")

d2 = tempfile.mkdtemp(prefix="wb-keyids-")
write_settings(d2, [
    {"id": "k6", "name": "ChatGPT-HOME_Global", "key": "key-2a1c", "realm": "intl"},
    {"id": "k6", "name": "123", "key": "key-881c", "realm": "cn"},
])
entries = S.api_keys(d2)
check("both rows survive the read", len(entries) == 2, entries)
ids = [entry["id"] for entry in entries]
check("duplicate id was disambiguated", len(set(ids)) == 2, ids)
check("the first row keeps the original id", ids[0] == "k6", ids)
check("keys are not swapped", [e["key"] for e in entries] == ["key-2a1c", "key-881c"],
      [e["key"] for e in entries])

print()
print("[4] reveal-style lookup by id finds exactly one row")

by_id = {}
for entry in entries:
    by_id.setdefault(entry["id"], []).append(entry)
check("every id maps to a single row", all(len(v) == 1 for v in by_id.values()), by_id)
check("id of the 123 row resolves to its own key",
      by_id[ids[1]][0]["key"] == "key-881c", by_id[ids[1]])

print()
print("[5] the disambiguated id is what gets persisted")

# A save must not reintroduce the collision the read just repaired.
S.set_api_keys(d2, entries)
on_disk = json.loads(open(S.settings_path(d2), encoding="utf-8").read())["api_keys"]
disk_ids = [entry["id"] for entry in on_disk]
check("persisted ids are unique", len(set(disk_ids)) == len(disk_ids), disk_ids)
check("persisted ids match what was read", disk_ids == ids, (disk_ids, ids))
check("the 123 row still holds its own key",
      on_disk[1]["key"] == "key-881c", on_disk[1])

print()
print("[6] a blank id on a legacy row does not swallow the stored key")

d3 = tempfile.mkdtemp(prefix="wb-keyids-")
write_settings(d3, [{"id": "", "name": "solo", "key": "key-solo"}])
entries = S.api_keys(d3)
check("a blank id is filled in on read", entries[0]["id"] != "", entries)
check("the stored key is preserved", entries[0]["key"] == "key-solo", entries)

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
