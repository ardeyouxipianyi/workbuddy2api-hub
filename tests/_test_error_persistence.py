"""A stale error label must not survive a restart.

An account's `lastError` / `cooldownUntil` are live state: they describe what
the upstream said on the last request, not the credential. They used to be
written into the account file and read back unconditionally, so an old
"HTTP 429 (model throttled)" label came back on the panel after every restart
even though the account had been serving fine for days - the in-memory clear
(successful request -> clear_error()) never reached the file, and the file was
only rewritten on unrelated events.

The fix makes them runtime-only for the local credential file, exactly like
`model_cooldowns` (see VOLATILE_FIELDS): __init__ ignores them, save() does not
write them. Export still carries them for inspection, because an export is a
snapshot of live state rather than a credential file.

No network: accounts are built from dicts and written to a temp directory.
"""
import base64
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-error-persistence-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_accounts as A

PASS = FAIL = 0

INTL_ISS = "https://www.workbuddy.ai/auth/realms/copilot"


def jwt(iss=INTL_ISS, sub="u-1"):
    def part(obj):
        raw = base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")
        return raw.rstrip("=")
    return "%s.%s.sig" % (part({"alg": "RS256", "typ": "JWT"}),
                          part({"iss": iss, "sub": sub, "exp": 4102444800}))


TOKEN = jwt()


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


def credential(uid="u-stale-1", **extra):
    data = {"uid": uid, "domain": "www.workbuddy.ai", "realm": "intl",
            "accessToken": TOKEN}
    data.update(extra)
    return data


def write_account(directory, data):
    path = os.path.join(directory, str(data["uid"]) + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    return path


def read_account(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


print("[1] a stale error on disk is not resurrected on load")
accounts_dir = os.path.join(_TMP, "restart")
os.makedirs(accounts_dir, exist_ok=True)
expired = time.time() - 3600
write_account(accounts_dir, credential(
    lastError="HTTP 429 (model throttled)",
    cooldownUntil=expired,
))
pool = A.AccountPool(accounts_dir)
loaded = pool.load()
check("the credential file loads", len(loaded) == 1, len(loaded))
state = loaded[0].public()
check("the stale 429 label is gone", state["lastError"] == "", state["lastError"])
check("no stale error detail is shown", state["lastErrorDetail"] is None,
      state["lastErrorDetail"])
check("an expired cooldown is not restored", state["inCooldown"] is False,
      state["inCooldown"])
check("the account is immediately selectable", loaded[0].ready() is True)

print("[2] a live error still shows while the process runs")
live = A.Account(credential(uid="u-live-1"))
live.note_error("HTTP 429 (model throttled)", cooldown=600)
live_state = live.public()
check("note_error() is visible right away",
      live_state["lastError"] == "HTTP 429 (model throttled)", live_state["lastError"])
check("note_error() starts a cooldown", live_state["inCooldown"] is True)
live.clear_error()
check("clear_error() clears it again", live.public()["lastError"] == "")

print("[3] save() keeps live error state out of the credential file")
save_dir = os.path.join(_TMP, "save")
os.makedirs(save_dir, exist_ok=True)
saved = A.Account(credential(uid="u-save-1"))
saved.note_error("HTTP 429 (model throttled)", cooldown=600)
path = saved.save(save_dir)
on_disk = read_account(path)
check("lastError is not written", "lastError" not in on_disk, on_disk.get("lastError"))
check("cooldownUntil is not written", "cooldownUntil" not in on_disk,
      on_disk.get("cooldownUntil"))
check("the credential itself is still written",
      on_disk.get("accessToken") == TOKEN, on_disk.get("accessToken"))

print("[4] a pre-existing file with the stale fields is cleaned up on save")
stale_dir = os.path.join(_TMP, "upgrade")
os.makedirs(stale_dir, exist_ok=True)
stale_path = write_account(stale_dir, credential(
    uid="u-upgrade-1",
    lastError="HTTP 429 (model throttled)",
    cooldownUntil=expired,
))
upgraded = A.AccountPool(stale_dir).load()[0]
check("the old file no longer poisons the loaded account",
      upgraded.public()["lastError"] == "")
upgraded.save(stale_dir)
check("the next save drops the stale fields",
      "lastError" not in read_account(stale_path)
      and "cooldownUntil" not in read_account(stale_path))

print("[5] export still carries live state for inspection")
exported = A.account_to_export(upgraded)
check("export keeps lastError", "lastError" in exported, sorted(exported.keys()))
check("export keeps cooldownUntil", "cooldownUntil" in exported)
exported_live = A.account_to_export(A.Account(credential(uid="u-export-1", **{
    "lastError": "ignored", "cooldownUntil": expired})))
check("export ignores the on-disk value it just loaded", exported_live["lastError"] == "",
      exported_live["lastError"])
mid_flight = A.Account(credential(uid="u-export-2"))
mid_flight.note_error("HTTP 429 (model throttled)", cooldown=600)
check("export reflects a live error",
      A.account_to_export(mid_flight)["lastError"] == "HTTP 429 (model throttled)")

print("[6] import still resets volatile state")
row = A.normalise_import_row(credential(
    uid="u-import-1",
    lastError="HTTP 429 (model throttled)",
    cooldownUntil=expired,
))
check("imported lastError is reset", row["lastError"] == "", row["lastError"])
check("imported cooldown is reset", row["cooldownUntil"] == 0.0, row["cooldownUntil"])

print()
print("passed %d, failed %d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
