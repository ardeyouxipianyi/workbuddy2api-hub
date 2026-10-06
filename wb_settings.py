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
# How often the gateway pulls fresh prices, in minutes; keep in step with
# wb_pricing.DEFAULT_REFRESH_MINUTES.
DEFAULT_PRICING_REFRESH_MINUTES = 5.0
# The setting used to be counted in hours and stored under this key. It is read
# once on upgrade (x60) and rewritten under the new key, so 6 hours can never
# come back as 6 minutes.
LEGACY_PRICING_REFRESH_HOURS_KEY = "pricing_refresh_hours"
PRICING_REFRESH_MINUTES_KEY = "pricing_refresh_minutes"
# A month, the old cap converted: 720 hours = 43200 minutes.
MAX_PRICING_REFRESH_MINUTES = 24 * 30 * 60
# Whether a model name may inherit its price from a suffix-stripped base
# (deepseek-r1-0528-lkeap → deepseek-r1-0528). Missing key reads as on.
PRICING_VARIANT_INHERIT_KEY = "pricing_variant_inherit"

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
    installs that never touch this field. A restricted key that names no model
    is refused instead of waved through: nothing in the request says it is
    asking for something the key may use, and forwarding it only reaches the
    upstream carrying an empty model, spending an attempt on a call that cannot
    succeed.
    """
    patterns = _clean_model_patterns((entry or {}).get("models"))
    if not patterns:
        return True
    name = str(model or "").strip().lower()
    if not name:
        return False
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def _clean_key_entry(entry):
    """Normalize one stored key entry; returns None when unusable.

    A deleted entry keeps its row even though its secret is gone: the id is
    what the usage log records, so dropping the row would make every past
    request of that key unattributable. Deletion is one-way - the secret is
    never stored again and the entry can only ever be read, not re-enabled.
    """
    if not isinstance(entry, dict):
        return None
    key = str(entry.get("key") or "").strip()
    deleted_at = str(entry.get("deleted_at") or "").strip()
    if not key and not deleted_at:
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
        "enabled": False if deleted_at else entry.get("enabled", True) is not False,
        "created_at": entry.get("created_at") or time.strftime("%Y/%m/%d %H:%M"),
        "deleted_at": deleted_at,
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


def api_keys(accounts_dir, include_deleted=False):
    """Every configured key, newest shape first.

    A settings file written by an older build only has the single
    `api_key`/`api_key_set` pair; that is surfaced as one unbound entry so
    upgrades keep working without a migration step. Ids are made unique here
    as well as on write, so a file that already holds a duplicate (and no
    longer has to be saved before it behaves) reads back as distinct rows.

    Deleted entries are hidden by default: they are gone as credentials, and
    match_api_key must never see them. Their rows stay on disk (and come back
    with include_deleted) because the usage log names a key by id, and an id
    with no name is not a table anyone can read.
    """
    data = load(accounts_dir)
    stored = data.get("api_keys")
    if isinstance(stored, list):
        out = []
        seen = set()
        seen_ids = set()
        for raw in stored:
            entry = _clean_key_entry(raw)
            if entry is None:
                continue
            # Deduplicate on the secret, and only when there is one: every
            # deleted entry has an empty key, and those are distinct rows.
            if entry["key"]:
                if entry["key"] in seen:
                    continue
                seen.add(entry["key"])
            entry["id"] = _unique_key_id(entry["id"], seen_ids)
            seen_ids.add(entry["id"])
            out.append(entry)
        if not include_deleted:
            out = [e for e in out if not e.get("deleted_at")]
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
                "created_at": "",
                "deleted_at": "",
            }]
    return []


def set_api_keys(accounts_dir, keys):
    """Replace the whole key list. Returns the saved (live) list.

    Removal is a soft delete. The caller is the panel, which can only submit
    the rows it can see and cannot see deleted ones, so an id that vanishes
    from the submission is marked deleted instead of dropped: the usage log
    attributes spend by id, and losing the id would dump a key's whole history
    into "(未知 key)". The secret is wiped at that same moment, so a deleted
    key can never authenticate again.
    """
    with _lock:
        previous = api_keys(accounts_dir, include_deleted=True)
        cleaned = []
        seen = set()
        seen_ids = set()
        for raw in keys or []:
            entry = _clean_key_entry(raw)
            if entry is None:
                continue
            if entry["key"]:
                if entry["key"] in seen:
                    continue
                seen.add(entry["key"])
            entry["id"] = _unique_key_id(entry["id"], seen_ids)
            seen_ids.add(entry["id"])
            cleaned.append(entry)
        for old in previous:
            if old["id"] in seen_ids:
                continue
            seen_ids.add(old["id"])
            cleaned.append({
                "id": old["id"],
                "name": old["name"],
                "key": "",
                "realm": old.get("realm") or "",
                "models": old.get("models") or [],
                "enabled": False,
                "created_at": old.get("created_at") or "",
                "deleted_at": old.get("deleted_at") or time.strftime("%Y/%m/%d %H:%M"),
            })
        data = load(accounts_dir)
        data["api_keys"] = cleaned
        # The single-key fields are now derived; drop them so there is one
        # source of truth and the list survives a restart.
        data.pop("api_key", None)
        data.pop("api_key_set", None)
        save(accounts_dir, data)
        return [e for e in cleaned if not e.get("deleted_at")]


def match_api_key(accounts_dir, supplied, extra_keys=()):
    """Find which configured key a request presented, if any.

    Returns a copy of the entry (with a `source` field) so the caller can read
    the bound realm, or None when nothing matches.
    """
    supplied = (supplied or "").strip()
    if not supplied:
        return None
    for entry in api_keys(accounts_dir):
        # A deleted entry is already stored with enabled=False; the explicit
        # check keeps a hand-edited settings file from reviving one.
        if entry["enabled"] and not entry.get("deleted_at") \
                and hmac.compare_digest(supplied, entry["key"]):
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


# ------------------------------------------------------------------ limits
# The four guards share one shape: a global default that covers both realms,
# plus an optional per-realm override. An override left empty inherits the
# global value, so an install that never touches it behaves exactly as before,
# and one that does only ever has to reason about a single number per guard.
LIMIT_KEYS = ("reserve_credits", "daily_token_limit",
              "daily_credit_limit", "model_daily_token_limit")
LIMIT_REALMS = ("intl", "cn")
LIMIT_SCOPES = ("global",) + LIMIT_REALMS
LIMITS_KEY = "limits"


def _empty_limit_entry():
    """One guard: a global default plus a slot per realm (None = inherit)."""
    return {"global": 0, "intl": None, "cn": None}


def _coerce_global(value):
    """A global threshold. Junk and negatives collapse to 0 (off), which is
    also what every install predating the setting reads as."""
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        number = 0
    return max(0, number)


def _coerce_override(value):
    """A per-realm override. None (or a blank form field) means "inherit the
    global default", kept distinct from an explicit 0 ("off for this realm")."""
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, number)


def _fold_legacy_limits(data):
    """Move the four flat top-level guards into `limits`.

    Returns (changed, limits). The old keys are dropped as they are folded in,
    so a later rollback cannot resurrect a stale limit from beside the grouped
    copy.
    """
    limits = {}
    changed = False
    for key in LIMIT_KEYS:
        entry = _empty_limit_entry()
        if key in data:
            entry["global"] = _coerce_global(data.pop(key))
            changed = True
        limits[key] = entry
    return changed, limits


def _normalize_limits(raw):
    """A copy of the stored map with every key and scope filled in, so a
    partial or hand-edited settings.json still reads as a complete shape."""
    limits = {}
    for key in LIMIT_KEYS:
        entry = raw.get(key)
        if not isinstance(entry, dict):
            entry = {}
        limits[key] = {
            "global": _coerce_global(entry.get("global")),
            "intl": _coerce_override(entry.get("intl")),
            "cn": _coerce_override(entry.get("cn")),
        }
    return limits


def limits_data(accounts_dir):
    """The grouped limits map, migrating a flat pre-grouping file on first read.

    Reading is what upgrades: the first look at an old settings.json folds the
    four flat keys into `limits` and rewrites the file, so the rest of the
    gateway only ever sees one shape.
    """
    with _lock:
        data = load(accounts_dir)
        raw = data.get(LIMITS_KEY)
        if isinstance(raw, dict):
            return _normalize_limits(raw)
        changed, limits = _fold_legacy_limits(data)
        if changed:
            data[LIMITS_KEY] = limits
            try:
                save(accounts_dir, data)
            except Exception:
                # A read-only accounts dir must not take the panel down; the
                # folded values are still returned and the next read retries.
                pass
        return limits


def limits_snapshot(accounts_dir):
    """The panel's view of every guard, keyed by limit then by scope."""
    return limits_data(accounts_dir)


def limit_value(accounts_dir, key, realm=None):
    """One guard's effective value for a realm.

    realm None (or "") returns the global default; an intl/cn override wins
    when it is set. An unknown key reads as 0 (off), so a typo cannot wedge
    the request path.
    """
    entry = limits_data(accounts_dir).get(key) or _empty_limit_entry()
    if realm in LIMIT_REALMS:
        override = entry.get(realm)
        if override is not None:
            return override
    return entry.get("global") or 0


def limit_values(accounts_dir, key):
    """One guard resolved for every realm plus the global it inherits.

    {"global": g, "intl": ..., "cn": ...}. The intl/cn entries already carry
    the global value when no override is set, so the pool can hand each
    account its own realm without a second settings lookup.
    """
    entry = limits_data(accounts_dir).get(key) or _empty_limit_entry()
    global_value = entry.get("global") or 0
    values = {"global": global_value}
    for realm in LIMIT_REALMS:
        override = entry.get(realm)
        values[realm] = global_value if override is None else override
    return values


def set_limit(accounts_dir, key, scope, value):
    """Persist one guard at one scope. Returns the stored entry.

    scope is "global", "intl" or "cn"; a None/blank value clears an intl/cn
    override back to "inherit", while the global slot always stores a number.
    """
    if key not in LIMIT_KEYS:
        raise ValueError("unknown limit: %s" % key)
    if scope not in LIMIT_SCOPES:
        raise ValueError("unknown scope: %s" % scope)
    with _lock:
        data = load(accounts_dir)
        raw = data.get(LIMITS_KEY)
        if isinstance(raw, dict):
            limits = _normalize_limits(raw)
        else:
            _changed, limits = _fold_legacy_limits(data)
        entry = limits[key]
        if scope == "global":
            entry["global"] = _coerce_global(value)
        else:
            entry[scope] = _coerce_override(value)
        data[LIMITS_KEY] = limits
        save(accounts_dir, data)
    return entry


def reserve_credits(accounts_dir, realm=None):
    """Global low-credit guard: an account at or below this balance stays idle.

    Zero disables the guard, which keeps installs that predate the setting
    behaving exactly as before.
    """
    return limit_value(accounts_dir, "reserve_credits", realm)


def set_reserve_credits(accounts_dir, value):
    """Persist the guard threshold. Returns the stored value."""
    return set_limit(accounts_dir, "reserve_credits", "global", value)["global"]


def daily_token_limit(accounts_dir, realm=None):
    """Global daily guard: an account that already burned this many tokens
    today stays idle until local midnight.

    Zero disables the guard, which keeps installs that predate the setting
    behaving exactly as before.
    """
    return limit_value(accounts_dir, "daily_token_limit", realm)


def set_daily_token_limit(accounts_dir, value):
    """Persist the daily token threshold. Returns the stored value."""
    return set_limit(accounts_dir, "daily_token_limit", "global", value)["global"]


def daily_credit_limit(accounts_dir, realm=None):
    """Daily credit guard: an account that already spent this many credits
    today serves free models only until local midnight, so a client that
    would keep burning credits on paid models rotates to another account
    instead of spending the whole balance.

    Zero disables the guard, which keeps installs that predate the setting
    behaving exactly as before.
    """
    return limit_value(accounts_dir, "daily_credit_limit", realm)


def set_daily_credit_limit(accounts_dir, value):
    """Persist the daily credit threshold. Returns the stored value."""
    return set_limit(accounts_dir, "daily_credit_limit", "global", value)["global"]


def model_daily_token_limit(accounts_dir, realm=None):
    """Per-model daily guard: an account that already burned this many
    tokens today on ONE model stops being handed out for that model until
    local midnight, while every other model keeps working.

    Zero disables the guard, which keeps installs that predate the setting
    behaving exactly as before.
    """
    return limit_value(accounts_dir, "model_daily_token_limit", realm)


def set_model_daily_token_limit(accounts_dir, value):
    """Persist the per-model daily token threshold. Returns the stored value."""
    return set_limit(accounts_dir, "model_daily_token_limit", "global", value)["global"]


def _clamp_refresh_minutes(value):
    """A month is well past "often enough"; the cap keeps a typo from parking
    the next refresh beyond any horizon the panel can show."""
    return max(0.0, min(MAX_PRICING_REFRESH_MINUTES, value))


def _migrate_pricing_refresh(data, accounts_dir):
    """(found, minutes) - convert a pre-minutes settings.json in place.

    `pricing_refresh_hours` used to hold hours. Reading it as minutes would
    turn 6 hours into 6 minutes, so the value is multiplied by 60 here, written
    under the new key and the old key dropped - one time, on the first read
    after the upgrade. A value that is not a number just loses the stale key
    and falls back to the default; a legacy 0 still means "off".
    """
    legacy = data.pop(LEGACY_PRICING_REFRESH_HOURS_KEY, None)
    if legacy is None:
        return False, None
    try:
        minutes = _clamp_refresh_minutes(float(legacy) * 60.0)
    except (TypeError, ValueError):
        minutes = None
    else:
        data[PRICING_REFRESH_MINUTES_KEY] = minutes
    try:
        save(accounts_dir, data)
    except Exception:
        # A read-only accounts dir must not take the panel down; the converted
        # value is still returned, and the next read simply migrates again.
        pass
    return True, minutes


def pricing_refresh_minutes(accounts_dir):
    """How often the gateway refreshes the OpenRouter price history, in minutes.

    Zero disables the refresh, which keeps installs that predate the setting
    on the bundled snapshot. A settings.json written before the unit changed
    carries `pricing_refresh_hours`, migrated here on first read (see
    `_migrate_pricing_refresh`). Anything not a number falls back to the
    default, so a hand-edited settings.json cannot wedge the refresh loop.
    """
    with _lock:
        data = load(accounts_dir)
        raw = data.get(PRICING_REFRESH_MINUTES_KEY)
        if raw is None:
            found, minutes = _migrate_pricing_refresh(data, accounts_dir)
            if not found:
                return DEFAULT_PRICING_REFRESH_MINUTES
            if minutes is None:
                return DEFAULT_PRICING_REFRESH_MINUTES
            return minutes
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_PRICING_REFRESH_MINUTES
    return value if value > 0 else 0.0


def set_pricing_refresh_minutes(accounts_dir, value):
    """Persist the refresh interval in minutes. Returns the stored value."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return pricing_refresh_minutes(accounts_dir)
    value = _clamp_refresh_minutes(value)
    with _lock:
        data = load(accounts_dir)
        data[PRICING_REFRESH_MINUTES_KEY] = value
        # The minutes key is the one in force; a leftover legacy key would only
        # confuse a later rollback into reading a stale interval.
        data.pop(LEGACY_PRICING_REFRESH_HOURS_KEY, None)
        save(accounts_dir, data)
    return value


def pricing_variant_inherit(accounts_dir):
    """Whether a model name may inherit its price from a suffix-stripped base.

    On unless the operator turns it off: a hub model carrying a channel suffix
    (`deepseek-r1-0528-lkeap`) is the same entity as its base model upstream,
    and without this the gateway would show "未定价" for a model it can price
    exactly. Off restores the previous behaviour - only the override table and
    an exact name match can price a model - so an install that wants the
    strictest possible rule keeps it. A settings.json that predates the key
    reads back as on, which is the default this ships with.
    """
    value = load(accounts_dir).get(PRICING_VARIANT_INHERIT_KEY)
    return True if value is None else value is True


def set_pricing_variant_inherit(accounts_dir, enabled):
    """Persist the variant-inheritance switch. Returns the stored boolean."""
    enabled = bool(enabled)
    with _lock:
        data = load(accounts_dir)
        data[PRICING_VARIANT_INHERIT_KEY] = enabled
        save(accounts_dir, data)
    return enabled


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
    """Normalize one stored slot; returns None when unusable.

    The exit fields are written by a probe and stay empty until one runs. An
    empty name means "label this slot by its exit", which is what the panel
    shows; it is never silently replaced by the id here, or the operator could
    not tell an auto-named slot from a renamed one.
    """
    if not isinstance(entry, dict):
        return None
    url = str(entry.get("url") or "").strip()
    if not url:
        return None
    slot_id = str(entry.get("id") or "").strip()
    if not slot_id:
        slot_id = fallback_id or ""
    try:
        probed_at = int(entry.get("probed_at") or 0)
    except (TypeError, ValueError):
        probed_at = 0
    return {
        "id": slot_id,
        "name": str(entry.get("name") or "").strip(),
        "url": url,
        "enabled": entry.get("enabled", True) is not False,
        "ip": str(entry.get("ip") or "").strip(),
        "country": str(entry.get("country") or "").strip(),
        "country_code": str(entry.get("country_code") or "").strip().upper(),
        "ip_type": str(entry.get("ip_type") or "").strip().lower(),
        "isp": str(entry.get("isp") or "").strip(),
        "asn": str(entry.get("asn") or "").strip(),
        "probed_at": probed_at,
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


def update_proxy_slot(accounts_dir, slot_id, fields, defaults=None):
    """Merge `fields` into one stored slot; returns the merged copy, or None.

    Both the read and the write happen under the store lock. A plain
    read-modify-write from the caller would race with a panel save and write
    back a list that no longer matches what is on disk, silently reverting
    whatever the other writer changed.

    `defaults` are applied only to fields that are still empty at write time,
    so a value the operator typed while the caller was working is never
    overwritten by a derived one.
    """
    slot_id = str(slot_id or "").strip()
    if not slot_id:
        return None
    with _lock:
        out, found = [], None
        for entry in proxy_slots(accounts_dir):
            if entry["id"] == slot_id and found is None:
                entry = dict(entry)
                entry.update(fields)
                for key, value in (defaults or {}).items():
                    if not entry.get(key):
                        entry[key] = value
                found = entry
            out.append(entry)
        if found is None:
            return None
        data = load(accounts_dir)
        data["proxy_slots"] = out
        save(accounts_dir, data)
        return found


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
