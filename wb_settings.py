"""Runtime settings for the gateway: panel password and API key override.

Everything lives in `accounts/settings.json` so a change made from the web
panel survives a restart without editing the launcher .bat files. The panel
password is never stored in clear text - only a PBKDF2-SHA256 digest.

Only the Python standard library is required.
"""

import fnmatch
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

DEFAULT_PANEL_PASSWORD = "admin"
PBKDF2_ROUNDS = 120_000
SESSION_TTL = 7 * 24 * 3600

_lock = threading.RLock()


def settings_path(accounts_dir):
    return os.path.join(accounts_dir, "settings.json")


def _digest(password, salt_hex, rounds=PBKDF2_ROUNDS):
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), rounds
    ).hex()


def load(accounts_dir):
    """Return the persisted settings, or an empty dict on a fresh install."""
    path = settings_path(accounts_dir)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return {}


def save(accounts_dir, data):
    """Atomic write so a crash cannot leave a half-written settings file."""
    with _lock:
        os.makedirs(accounts_dir, exist_ok=True)
        path = settings_path(accounts_dir)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return path


def panel_password_is_default(accounts_dir):
    data = load(accounts_dir)
    if not data.get("panel_password_hash"):
        return True
    return data.get("panel_password_default") is True


def verify_panel_password(accounts_dir, password):
    """True when `password` opens the web panel."""
    password = password or ""
    data = load(accounts_dir)
    stored = data.get("panel_password_hash")
    if not stored:
        return password == DEFAULT_PANEL_PASSWORD
    if data.get("panel_password_default") is True:
        return password == DEFAULT_PANEL_PASSWORD
    salt = data.get("panel_password_salt")
    if not salt:
        return False
    rounds = int(data.get("panel_password_rounds") or PBKDF2_ROUNDS)
    try:
        given = _digest(password, salt, rounds)
    except Exception:
        return False
    return hmac.compare_digest(given, stored)


def set_panel_password(accounts_dir, password):
    with _lock:
        data = load(accounts_dir)
        if password == DEFAULT_PANEL_PASSWORD:
            data.pop("panel_password_salt", None)
            data.pop("panel_password_rounds", None)
            data["panel_password_hash"] = ""
            data["panel_password_default"] = True
        else:
            salt = secrets.token_hex(16)
            data["panel_password_salt"] = salt
            data["panel_password_rounds"] = PBKDF2_ROUNDS
            data["panel_password_hash"] = _digest(password, salt)
            data["panel_password_default"] = False
        save(accounts_dir, data)


def api_key_override(accounts_dir):
    """Return (key, is_set). `is_set` means the panel manages the key."""
    data = load(accounts_dir)
    if not data.get("api_key_set"):
        return None, False
    return str(data.get("api_key") or ""), True


def set_api_key(accounts_dir, key):
    with _lock:
        data = load(accounts_dir)
        data["api_key"] = key or ""
        data["api_key_set"] = True
        save(accounts_dir, data)


def ensure_launcher_key(accounts_dir):
    """Return the persisted LAN key, creating one on first use.

    LAN mode must never ship a well-known default: the gateway spends the
    account's own upstream quota, so anyone on the same network could drain it.
    The value is generated once and stored so clients keep working across
    restarts. Returns (key, created) so the caller can tell the user whether
    this run minted a fresh credential.
    """
    with _lock:
        data = load(accounts_dir)
        existing = str(data.get("launcher_key") or "").strip()
        if existing:
            return existing, False
        key = "wb-" + secrets.token_urlsafe(24)
        data["launcher_key"] = key
        save(accounts_dir, data)
        return key, True


# --------------------------------------------------------------- API keys
# Each key can be bound to one upstream realm, so several clients can hit
# different exits at the same time instead of sharing the global switch.

REALMS = ("", "intl", "cn")


def _clean_model_patterns(value):
    """Normalize one key's model allow-list into a list of lowercase patterns.

    The panel posts a list; a hand-edited settings.json tends to hold a
    comma-separated string, so both shapes are accepted. Matching is done with
    fnmatch, which makes an exact name (`gpt-6-astra`) and a wildcard
    (`deepseek/*`) behave the same way. An empty result means "no restriction",
    which is what every key written before this field existed reads back as -
    an upgrade therefore keeps behaving exactly as before.
    """
    if isinstance(value, str):
        raw = [part for part in re.split(r"[,;\n]", value)]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        return []
    out = []
    for item in raw:
        pattern = str(item or "").strip().lower()
        if pattern and pattern not in out:
            out.append(pattern)
    return out


def key_allows_model(entry, model):
    """True when `entry` places no model restriction, or `model` matches it.

    A key with an empty list stays unrestricted, so nothing changes for
    installs that never touch this field.
    """
    patterns = _clean_model_patterns((entry or {}).get("models"))
    if not patterns:
        return True
    name = str(model or "").strip().lower()
    if not name:
        return True
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def _clean_key_entry(entry):
    """Normalize one stored key entry; returns None when unusable."""
    if not isinstance(entry, dict):
        return None
    key = str(entry.get("key") or "").strip()
    if not key:
        return None
    realm = str(entry.get("realm") or "").strip().lower()
    if realm not in REALMS:
        realm = ""
    return {
        "id": str(entry.get("id") or secrets.token_hex(6)),
        "name": str(entry.get("name") or "").strip() or "未命名",
        "key": key,
        "realm": realm,
        "models": _clean_model_patterns(entry.get("models")),
        "enabled": entry.get("enabled", True) is not False,
        "created_at": entry.get("created_at") or time.strftime("%Y/%m/%d %H:%M"),
    }


def _unique_key_id(candidate, used):
    """Return `candidate`, or a variant that is not already in `used`.

    Ids used to be minted from the row's index in the submitted list, so a
    settings file written by an older build can hold two rows carrying the same
    id. `/settings/reveal` then answered with whichever row came first, which
    made the copy button on the other row hand out a different key. Later
    duplicates get a numeric suffix: the suffix is deterministic, so the id a
    `/settings` read just returned still resolves on the follow-up reveal.
    """
    candidate = str(candidate or "").strip()
    if candidate and candidate not in used:
        return candidate
    if candidate:
        suffix = 2
        while True:
            alt = "%s-%d" % (candidate, suffix)
            if alt not in used:
                return alt
            suffix += 1
    while True:
        alt = secrets.token_hex(6)
        if alt not in used:
            return alt


def api_keys(accounts_dir):
    """Every configured key, newest shape first.

    A settings file written by an older build only has the single
    `api_key`/`api_key_set` pair; that is surfaced as one unbound entry so
    upgrades keep working without a migration step. Ids are made unique here
    as well as on write, so a file that already holds a duplicate (and no
    longer has to be saved before it behaves) reads back as distinct rows.
    """
    data = load(accounts_dir)
    stored = data.get("api_keys")
    if isinstance(stored, list):
        out = []
        seen = set()
        seen_ids = set()
        for raw in stored:
            entry = _clean_key_entry(raw)
            if entry and entry["key"] not in seen:
                seen.add(entry["key"])
                entry["id"] = _unique_key_id(entry["id"], seen_ids)
                seen_ids.add(entry["id"])
                out.append(entry)
        return out

    if data.get("api_key_set"):
        legacy = str(data.get("api_key") or "").strip()
        if legacy:
            return [{
                "id": "legacy",
                "name": "默认（跟随面板切换）",
                "key": legacy,
                "realm": "",
                "models": [],
                "enabled": True,
            }]
    return []


def set_api_keys(accounts_dir, keys):
    """Replace the whole key list. Returns the stored list."""
    with _lock:
        cleaned = []
        seen = set()
        seen_ids = set()
        for raw in keys or []:
            entry = _clean_key_entry(raw)
            if entry and entry["key"] not in seen:
                seen.add(entry["key"])
                entry["id"] = _unique_key_id(entry["id"], seen_ids)
                seen_ids.add(entry["id"])
                cleaned.append(entry)
        data = load(accounts_dir)
        data["api_keys"] = cleaned
        # The single-key fields are now derived; drop them so there is one
        # source of truth and the list survives a restart.
        data.pop("api_key", None)
        data.pop("api_key_set", None)
        save(accounts_dir, data)
        return cleaned


def match_api_key(accounts_dir, supplied, extra_keys=()):
    """Find which configured key a request presented, if any.

    Returns a copy of the entry (with a `source` field) so the caller can read
    the bound realm, or None when nothing matches.
    """
    supplied = (supplied or "").strip()
    if not supplied:
        return None
    for entry in api_keys(accounts_dir):
        if entry["enabled"] and hmac.compare_digest(supplied, entry["key"]):
            out = dict(entry)
            out["source"] = "panel"
            return out
    for candidate in extra_keys:
        candidate = (candidate or "").strip()
        if candidate and hmac.compare_digest(supplied, candidate):
            return {
                "id": "launcher",
                "name": "启动参数",
                "key": candidate,
                "realm": "",
                "models": [],
                "enabled": True,
                "source": "launcher",
            }
    return None


def auth_disabled(accounts_dir):
    """True when the operator switched API-key checking off entirely."""
    return load(accounts_dir).get("auth_disabled") is True


def set_auth_disabled(accounts_dir, disabled):
    with _lock:
        data = load(accounts_dir)
        data["auth_disabled"] = bool(disabled)
        save(accounts_dir, data)


def reserve_credits(accounts_dir):
    """Global low-credit guard: an account at or below this balance stays idle.

    Zero disables the guard, which keeps installs that predate the setting
    behaving exactly as before.
    """
    try:
        value = int(load(accounts_dir).get("reserve_credits") or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def set_reserve_credits(accounts_dir, value):
    """Persist the guard threshold. Returns the stored value."""
    try:
        value = int(value or 0)
    except (TypeError, ValueError):
        value = 0
    value = max(0, value)
    with _lock:
        data = load(accounts_dir)
        data["reserve_credits"] = value
        save(accounts_dir, data)
    return value


def daily_token_limit(accounts_dir):
    """Global daily guard: an account that already burned this many tokens
    today stays idle until local midnight.

    Zero disables the guard, which keeps installs that predate the setting
    behaving exactly as before.
    """
    try:
        value = int(load(accounts_dir).get("daily_token_limit") or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def set_daily_token_limit(accounts_dir, value):
    """Persist the daily token threshold. Returns the stored value."""
    try:
        value = int(value or 0)
    except (TypeError, ValueError):
        value = 0
    value = max(0, value)
    with _lock:
        data = load(accounts_dir)
        data["daily_token_limit"] = value
        save(accounts_dir, data)
    return value


def daily_credit_limit(accounts_dir):
    """Daily credit guard: an account that already spent this many credits
    today serves free models only until local midnight, so a client that
    would keep burning credits on paid models rotates to another account
    instead of spending the whole balance.

    Zero disables the guard, which keeps installs that predate the setting
    behaving exactly as before.
    """
    try:
        value = int(load(accounts_dir).get("daily_credit_limit") or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def set_daily_credit_limit(accounts_dir, value):
    """Persist the daily credit threshold. Returns the stored value."""
    try:
        value = int(value or 0)
    except (TypeError, ValueError):
        value = 0
    value = max(0, value)
    with _lock:
        data = load(accounts_dir)
        data["daily_credit_limit"] = value
        save(accounts_dir, data)
    return value


def model_daily_token_limit(accounts_dir):
    """Per-model daily guard: an account that already burned this many
    tokens today on ONE model stops being handed out for that model until
    local midnight, while every other model keeps working.

    Zero disables the guard, which keeps installs that predate the setting
    behaving exactly as before.
    """
    try:
        value = int(load(accounts_dir).get("model_daily_token_limit") or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def set_model_daily_token_limit(accounts_dir, value):
    """Persist the per-model daily token threshold. Returns the stored value."""
    try:
        value = int(value or 0)
    except (TypeError, ValueError):
        value = 0
    value = max(0, value)
    with _lock:
        data = load(accounts_dir)
        data["model_daily_token_limit"] = value
        save(accounts_dir, data)
    return value


def auto_switch_product(accounts_dir):
    """Whether an upstream 429 may rotate an account's outbound identity.

    Off unless the operator turns it on. Rotating identity spends the request's
    retry budget and leaves the account on a channel nobody picked, so the
    gateway does not decide that on its own - and an install that predates the
    setting keeps behaving exactly as it did.
    """
    return load(accounts_dir).get("auto_switch_product") is True


def set_auto_switch_product(accounts_dir, enabled):
    """Persist the auto-switch toggle. Returns the stored boolean."""
    enabled = bool(enabled)
    with _lock:
        data = load(accounts_dir)
        data["auto_switch_product"] = enabled
        save(accounts_dir, data)
    return enabled


def daily_chat_web(accounts_dir):
    """Whether the intl daily check-in also opens a web-channel conversation.

    On unless the operator turns it off: the desktop-identity chat completion
    this automation used to send does not register the daily activity, while a
    web conversation does (issues #75, #59). An install that never touched the
    setting keeps the web step, because that is the behaviour that earns the
    credits; the toggle exists so a deployment can opt back into the old
    single-request check-in.
    """
    value = load(accounts_dir).get("daily_chat_web")
    return True if value is None else value is True


def set_daily_chat_web(accounts_dir, enabled):
    """Persist the web-channel toggle. Returns the stored boolean."""
    enabled = bool(enabled)
    with _lock:
        data = load(accounts_dir)
        data["daily_chat_web"] = enabled
        save(accounts_dir, data)
    return enabled
def local_web_tools(accounts_dir):
    """Whether the gateway runs web_search / web_fetch calls itself.

    Off unless the operator turns it on. Forwarding the client's declaration
    untouched is what this gateway has done since v1.5.3, and it is what an
    install that never touched the switch keeps doing: the upstream has no
    server-side search tool, so a client declaring one runs it in its own
    process. Turning the switch on swaps the declaration for the gateway's own
    function and executes the calls locally (wb_webtools), which also means the
    gateway itself fetches the URLs a model asks for - hence opt-in only.
    """
    return load(accounts_dir).get("local_web_tools") is True


def set_local_web_tools(accounts_dir, enabled):
    """Persist the local web-tools switch. Returns the stored boolean."""
    enabled = bool(enabled)
    with _lock:
        data = load(accounts_dir)
        data["local_web_tools"] = enabled
        save(accounts_dir, data)
    return enabled


_SLOT_ID_RE = re.compile(r"^slot-(\d+)$")


def _clean_slot_entry(entry, fallback_id=None):
    """Normalize one stored slot; returns None when unusable."""
    if not isinstance(entry, dict):
        return None
    url = str(entry.get("url") or "").strip()
    if not url:
        return None
    slot_id = str(entry.get("id") or "").strip()
    if not slot_id:
        slot_id = fallback_id or ""
    return {
        "id": slot_id,
        "name": str(entry.get("name") or "").strip() or slot_id,
        "url": url,
        "enabled": entry.get("enabled", True) is not False,
    }


def _next_slot_id(existing):
    """Next unused `slot-<n>` id for a legacy list stored without ids."""
    highest = 0
    for entry in existing:
        match = _SLOT_ID_RE.match(str(entry.get("id") or ""))
        if match:
            highest = max(highest, int(match.group(1)))
    return "slot-%d" % (highest + 1)


def _slot_seq(data):
    """Highest slot number ever issued in this store."""
    try:
        return int(data.get("proxy_slot_seq") or 0)
    except Exception:
        return 0


def proxy_slots(accounts_dir):
    """Every configured proxy slot, in stored order."""
    data = load(accounts_dir)
    stored = data.get("proxy_slots")
    if not isinstance(stored, list):
        return []
    out, seen = [], set()
    for raw in stored:
        entry = _clean_slot_entry(raw)
        if entry and entry["url"] not in seen:
            seen.add(entry["url"])
            if not entry["id"]:
                entry["id"] = _next_slot_id(out)
            out.append(entry)
    return out


def set_proxy_slots(accounts_dir, slots):
    """Replace the whole slot list. Returns the stored list.

    Ids are drawn from a counter that only ever grows. Accounts persist the
    id they are bound to, so recycling a freed id would silently re-point an
    existing account at a newly added slot's exit IP.
    """
    with _lock:
        data = load(accounts_dir)
        stored = data.get("proxy_slots")
        stored = stored if isinstance(stored, list) else []

        seq = _slot_seq(data)
        # Seed from both the incoming and the outgoing list, so an id that is
        # being removed in this very save can never be handed to a new entry.
        for entry in list(stored) + list(slots or []):
            if not isinstance(entry, dict):
                continue
            match = _SLOT_ID_RE.match(str(entry.get("id") or ""))
            if match:
                seq = max(seq, int(match.group(1)))
        # A legacy list stored without ids is displayed as slot-1..slot-N, so
        # keep the counter above those too.
        seq = max(seq, len(stored))

        cleaned, seen = [], set()
        for raw in slots or []:
            entry = _clean_slot_entry(raw)
            if not entry or entry["url"] in seen:
                continue
            seen.add(entry["url"])
            if not entry["id"] or any(e["id"] == entry["id"] for e in cleaned):
                seq += 1
                entry["id"] = "slot-%d" % seq
            cleaned.append(entry)
        data["proxy_slots"] = cleaned
        data["proxy_slot_seq"] = seq
        save(accounts_dir, data)
        return cleaned


def drop_missing_bindings(pool, slots):
    """Clear bindings that point at a slot which no longer exists.

    Without this the stale id stays on the account, and a later slot that
    happens to receive that id would capture the account.
    """
    valid = {entry["id"] for entry in slots}
    changed = 0
    for account in list(getattr(pool, "accounts", []) or []):
        if account.proxy_slot and account.proxy_slot not in valid:
            account.proxy_slot = ""
            try:
                account.save(pool.dir)
            except Exception:
                pass
            changed += 1
    if changed:
        pool.apply_proxy_slots(slots)
    return changed


def find_proxy_slot(accounts_dir, slot_id):
    slot_id = str(slot_id or "").strip()
    if not slot_id:
        return None
    for entry in proxy_slots(accounts_dir):
        if entry["id"] == slot_id:
            return entry
    return None


class PanelSessions(object):
    """In-memory bearer tokens handed out after a successful panel login.

    Deliberately not persisted: restarting the gateway logs browsers out, which
    is the safer default for a LAN tool that people expose behind a port map.
    """

    def __init__(self, ttl=SESSION_TTL):
        self.ttl = ttl
        self._tokens = {}
        self._lock = threading.RLock()

    def create(self):
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._tokens[token] = time.time() + self.ttl
        return token

    def valid(self, token):
        if not token:
            return False
        with self._lock:
            expiry = self._tokens.get(token)
            if not expiry:
                return False
            if expiry < time.time():
                self._tokens.pop(token, None)
                return False
            return True

    def revoke(self, token):
        if not token:
            return
        with self._lock:
            self._tokens.pop(token, None)

    def revoke_all(self):
        with self._lock:
            self._tokens.clear()
