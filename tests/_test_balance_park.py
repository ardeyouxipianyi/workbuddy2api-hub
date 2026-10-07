"""Pin the 402 balance park: an out-of-credits account waits for 04:00.

A 402 (no credits) used to get the same short cooldown as any other error, so
the pool handed the empty account out again minutes later and every request to
it failed the same way - for the rest of the day on a single-account install.
The park holds the account until the next local 04:00 (the wall the daily
reset refills behind) and lifts early when a balance refresh shows credits
again. Rate-limit and model cooldowns are untouched.

No network: accounts are built from dicts and written to a temp directory, and
the credits refresh is stubbed.
"""
import base64
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-balance-park-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_accounts as A

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


INTL_ISS = "https://www.workbuddy.ai/auth/realms/copilot"


def jwt(iss=INTL_ISS, sub="u-1"):
    def part(obj):
        raw = base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")
        return raw.rstrip("=")
    return "%s.%s.sig" % (part({"alg": "RS256", "typ": "JWT"}),
                          part({"iss": iss, "sub": sub, "exp": 4102444800}))


TOKEN = jwt()


def credential(uid="u-402-1", **extra):
    data = {"uid": uid, "domain": "www.workbuddy.ai", "realm": "intl",
            "accessToken": TOKEN}
    data.update(extra)
    return data


def load_account(data, tag="park"):
    directory = os.path.join(_TMP, tag)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, str(data["uid"]) + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    pool = A.AccountPool(directory)
    accounts = pool.load()
    check("credential loads", len(accounts) == 1, len(accounts))
    return accounts[0]


def local_stamp(y, m, d, hh, mm=0):
    return time.mktime((y, m, d, hh, mm, 0, 0, 0, -1))


print("[1] next_local_4am rolls to the next 04:00")
before = local_stamp(2026, 3, 1, 3, 30)
check("before 04:00 -> same day",
      abs(A.next_local_4am(before) - local_stamp(2026, 3, 1, 4, 0)) < 1,
      A.next_local_4am(before) - before)
after = local_stamp(2026, 3, 1, 5, 0)
check("after 04:00 -> next day",
      abs(A.next_local_4am(after) - local_stamp(2026, 3, 2, 4, 0)) < 1,
      A.next_local_4am(after) - after)
exact = local_stamp(2026, 3, 1, 4, 0)
check("exactly 04:00 -> next day (a reached deadline is not reused)",
      abs(A.next_local_4am(exact) - local_stamp(2026, 3, 2, 4, 0)) < 1)

print("[2] note_balance_cooled parks the account until 04:00")
account = load_account(credential())
now = time.time()
until = account.note_balance_cooled("HTTP 402 (insufficient credits)")
check("deadline is in the future", until > now, until - now)
check("deadline is within 24h", until - now <= 24 * 3600, until - now)
check("throttle_wait blocks", account.throttle_wait() > 0, account.throttle_wait())
view = account.public()
check("the panel shows the park as a cooldown", view["inCooldown"] is True)
check("cooldownFor is set", (view["cooldownFor"] or 0) > 0, view["cooldownFor"])
check("lastError keeps the upstream wording",
      "402" in (view["lastError"] or ""), view["lastError"])
check("per-model cooldowns are untouched", view["modelCooldowns"] == [])
check("second call never shortens the deadline",
      account.note_balance_cooled() >= until)

print("[3] revive only when a refresh shows credits")
account.credits = {"remain": 0}
check("zero balance does not revive", account.revive_balance_cooldown() is False)
check("still parked", account.throttle_wait() > 0)
account.credits = {"remain": 5}
check("credits revive the park", account.revive_balance_cooldown() is True)
check("throttle_wait is clear again", account.throttle_wait() == 0)
check("the error label is cleared", account.public()["lastError"] == "")

print("[4] fetch_credits lifts the park after a successful refresh")
account2 = load_account(credential(uid="u-402-2"), tag="park2")
account2.note_balance_cooled("HTTP 402 (insufficient credits)")
account2.credits = {"remain": 9}
account2._fetch_credits_raw = lambda: {"ok": True, "credits": account2.credits}
res = account2.fetch_credits()
check("refresh reports ok", isinstance(res, dict) and res.get("ok") is True)
check("park lifted by the refresh", account2.throttle_wait() == 0)

print("[5] a plain clear_error() also resets the park")
account3 = load_account(credential(uid="u-402-3"), tag="park3")
account3.note_balance_cooled("HTTP 402 (insufficient credits)")
check("parked first", account3.throttle_wait() > 0)
account3.clear_error()
check("clear_error clears the park", account3.throttle_wait() == 0)

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
