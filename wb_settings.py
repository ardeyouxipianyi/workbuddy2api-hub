"""Runtime settings for the gateway: panel password and API key override.

Everything lives in `accounts/settings.json` so a change made from the web
panel survives a restart without editing the launcher .bat files. The panel
password is never stored in clear text - only a PBKDF2-SHA256 digest.

Only the Python standard library is required.
"""

import copy
import fnmatch
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

import wb_pool

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
SETTINGS_CACHE_TTL = 5.0
# abspath -> (read_at, (mtime_ns, size), data). The chat path reads settings
# 3-4 times per request; the cache removes the repeated open+parse while the
# stamp check still notices an edit made outside save() (audit #6).
_settings_cache = {}


def settings_path(accounts_dir):
    return os.path.join(accounts_dir, "settings.json")


def _digest(password, salt_hex, rounds=PBKDF2_ROUNDS):
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), rounds
    ).hex()


def _settings_stamp(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def load(accounts_dir):
    """Return the persisted settings, or an empty dict on a fresh install.

    Cached for SETTINGS_CACHE_TTL seconds, keyed on the file's mtime+size so
    an edit made outside save() is still noticed; save() drops its own entry
    immediately (audit #6).
    """
    path = settings_path(accounts_dir)
    key = os.path.abspath(path)
    stamp = _settings_stamp(path)
    now = time.time()
    with _lock:
        hit = _settings_cache.get(key)
        if (hit and now - hit[0] < SETTINGS_CACHE_TTL
                and hit[1] == stamp):
            return copy.deepcopy(hit[2])
    data = {}
    try:
        with open(path, encoding="utf-8") as fh:
            parsed = json.load(fh)
        if isinstance(parsed, dict):
            data = parsed
    except FileNotFoundError:
        pass
    except Exception:
        pass
    with _lock:
        _settings_cache[key] = (now, stamp, data)
    return copy.deepcopy(data)


def save(accounts_dir, data):
    """Atomic write so a crash cannot leave a half-written settings file."""
    with _lock:
        os.makedirs(accounts_dir, exist_ok=True)
        path = settings_path(accounts_dir)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        _settings_cache.pop(os.path.abspath(path), None)
        return path


LOGGING_DEFAULTS = {
    # Client IP / UA are personal data: opt-in, off by default.
    "record_client_info": False,
    # Request archive retention window and size budget (panel reqlog parity).
    "retention_days": 7,
    "archive_max_mb": 100,
}


def validate_logging_patch(raw):
    """Strict validation for a panel-saved logging patch."""
    out = {}
    for key, value in (raw or {}).items():
        if key == "record_client_info":
            if not isinstance(value, bool):
                raise ValueError("record_client_info must be true or false")
            out[key] = value
        elif key in ("retention_days", "archive_max_mb"):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("%s must be a whole number" % key)
            if value < 1:
                raise ValueError("%s cannot be less than 1" % key)
            out[key] = value
        else:
            raise ValueError("unknown logging setting %r" % key)
    return out


def logging_config(accounts_dir):
    """Request-archive settings (lenient read, strict write)."""
    stored = load(accounts_dir).get("logging")
    stored = stored if isinstance(stored, dict) else {}
    out = {}
    for key, default in LOGGING_DEFAULTS.items():
        value = stored.get(key, default)
        if key == "record_client_info":
            out[key] = value if isinstance(value, bool) else default
        else:
            out[key] = (value if isinstance(value, int) and not isinstance(value, bool)
                        and value >= 1 else default)
    return out


def set_logging_config(accounts_dir, cfg):
    """Persist the logging settings. Returns the stored config."""
    current = logging_config(accounts_dir)
    if isinstance(cfg, dict):
        current.update({k: v for k, v in cfg.items() if k in LOGGING_DEFAULTS})
    clean = validate_logging_patch(current)
    with _lock:
        data = load(accounts_dir)
        data["logging"] = deep_merge(data.get("logging"), clean)
        save(accounts_dir, data)
    return clean


def deep_merge(base, patch):
    """Recursively merge patch into base; unknown sibling keys survive.

    The panel form only submits the keys it manages. Replacing a whole group
    would silently drop hand-written or future keys, so nested objects merge
    key by key (panel mergeConfigMaps semantics). Returns a new dict; inputs
    are not mutated.
    """
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return patch
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


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


def pool_config(accounts_dir):
    """Panel-parity pool rules (weighted picking + backoff windows).

    Stored as one `pool` object in settings.json. Unknown keys are ignored
    and missing keys fall back to the panel-verified defaults, so an
    install that never writes this object keeps its previous behaviour
    plus the new weighted picking.
    """
    stored = load(accounts_dir).get("pool")
    merged = dict(wb_pool.DEFAULTS)
    if isinstance(stored, dict):
        merged.update({k: v for k, v in stored.items() if k in wb_pool.DEFAULTS})
    return wb_pool.normalize(merged)


def set_pool_config(accounts_dir, cfg):
    """Persist the pool rules. Returns the normalized, stored config."""
    current = pool_config(accounts_dir)
    if isinstance(cfg, dict):
        current.update({k: v for k, v in cfg.items() if k in wb_pool.DEFAULTS})
    clean = wb_pool.normalize(current)
    with _lock:
        data = load(accounts_dir)
        data["pool"] = deep_merge(data.get("pool"), clean)
        save(accounts_dir, data)
    return clean


SCHEDULE_DEFAULTS = {
    "checkin_hours": [9, 21],
    "travel_hours": [9, 21],
    "keepalive_hours": [22],
    "cat_hours": [1, 23],
    "daily_chat_hours": [9, 21],
    "growth_hours": [1],
    "checkin_enabled": True,
    "travel_enabled": True,
    "keepalive_enabled": True,
    "cat_enabled": True,
    "daily_chat_enabled": True,
    "growth_enabled": True,
    # The gateway has always run scheduled tasks for disabled accounts;
    # this panel-parity switch lets an operator opt into skipping them.
    "include_disabled_in_tasks": True,
    "balance_refresh_enabled": False,
    "balance_refresh_minutes": 5,
}
_SCHEDULE_HOUR_KEYS = ("checkin_hours", "travel_hours", "keepalive_hours",
                       "cat_hours", "daily_chat_hours", "growth_hours")
_SCHEDULE_BOOL_KEYS = ("checkin_enabled", "travel_enabled", "keepalive_enabled",
                       "cat_enabled", "daily_chat_enabled", "growth_enabled",
                       "include_disabled_in_tasks", "balance_refresh_enabled")


def _clean_hours(value):
    """Validate an hours list (whole numbers 0-23, no bools), sorted."""
    if not isinstance(value, (list, tuple)):
        raise ValueError("hours must be a list of whole numbers 0-23")
    out = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError("hours must be whole numbers 0-23")
        if not 0 <= item <= 23:
            raise ValueError("hours must be between 0 and 23")
        if item not in out:
            out.append(item)
    return sorted(out)


def validate_schedule_patch(raw):
    """Strict validation for a panel-saved schedule patch."""
    out = {}
    for key, value in (raw or {}).items():
        if key in _SCHEDULE_HOUR_KEYS:
            out[key] = _clean_hours(value)
        elif key in _SCHEDULE_BOOL_KEYS:
            if not isinstance(value, bool):
                raise ValueError("%s must be true or false" % key)
            out[key] = value
        elif key == "balance_refresh_minutes":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("balance_refresh_minutes must be a whole number")
            if value < 1:
                raise ValueError("balance_refresh_minutes cannot be less than 1")
            out[key] = value
        else:
            raise ValueError("unknown schedule setting %r" % key)
    return out


def schedule_config(accounts_dir):
    """Panel-parity schedule: per-family hours, switches and balance scan."""
    stored = load(accounts_dir).get("schedule")
    stored = stored if isinstance(stored, dict) else {}
    out = {}
    for key, default in SCHEDULE_DEFAULTS.items():
        value = stored.get(key)
        if key in _SCHEDULE_HOUR_KEYS:
            try:
                cleaned = _clean_hours(value) if value is not None else None
            except ValueError:
                cleaned = None
            out[key] = cleaned if cleaned is not None else list(default)
        elif key in _SCHEDULE_BOOL_KEYS:
            out[key] = value if isinstance(value, bool) else default
        else:
            out[key] = (value if isinstance(value, int) and not isinstance(value, bool)
                        and value >= 1 else default)
    return out


def set_schedule_config(accounts_dir, cfg):
    """Persist the schedule. Returns the normalized, stored config."""
    current = schedule_config(accounts_dir)
    if isinstance(cfg, dict):
        current.update({k: v for k, v in cfg.items() if k in SCHEDULE_DEFAULTS})
    clean = {}
    for key, default in SCHEDULE_DEFAULTS.items():
        value = current.get(key, default)
        if key in _SCHEDULE_HOUR_KEYS:
            clean[key] = _clean_hours(value)
        elif key in _SCHEDULE_BOOL_KEYS:
            clean[key] = value if isinstance(value, bool) else default
        else:
            clean[key] = int(value)
    with _lock:
        data = load(accounts_dir)
        data["schedule"] = deep_merge(data.get("schedule"), clean)
        save(accounts_dir, data)
    return clean


REDIS_DEFAULTS = {
    "url": "",
    "token": "",
    "affinity_mirror": False,
    "ttl_seconds": 604800,
}


def validate_redis_patch(raw):
    """Strict validation for a panel-saved redis/upstash patch."""
    out = {}
    for key, value in (raw or {}).items():
        if key in ("url", "token"):
            if not isinstance(value, str):
                raise ValueError("%s must be a string" % key)
            out[key] = value.strip()
        elif key == "affinity_mirror":
            if not isinstance(value, bool):
                raise ValueError("affinity_mirror must be true or false")
            out[key] = value
        elif key == "ttl_seconds":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("ttl_seconds must be a whole number")
            if value < 60:
                raise ValueError("ttl_seconds cannot be less than 60")
            out[key] = value
        else:
            raise ValueError("unknown redis setting %r" % key)
    return out


def redis_config(accounts_dir):
    """Optional Upstash mirror for sticky sessions (off by default)."""
    stored = load(accounts_dir).get("redis")
    stored = stored if isinstance(stored, dict) else {}
    out = {}
    for key, default in REDIS_DEFAULTS.items():
        value = stored.get(key, default)
        if key in ("url", "token"):
            out[key] = value.strip() if isinstance(value, str) else default
        elif key == "affinity_mirror":
            out[key] = value if isinstance(value, bool) else default
        else:
            out[key] = (value if isinstance(value, int) and not isinstance(value, bool)
                        and value >= 60 else default)
    return out


def set_redis_config(accounts_dir, cfg):
    """Persist the redis mirror settings. Returns the stored config."""
    current = redis_config(accounts_dir)
    if isinstance(cfg, dict):
        current.update({k: v for k, v in cfg.items() if k in REDIS_DEFAULTS})
    clean = validate_redis_patch(current)
    with _lock:
        data = load(accounts_dir)
        data["redis"] = deep_merge(data.get("redis"), clean)
        save(accounts_dir, data)
    return clean


UPSTREAM_DEFAULTS = {
    "header_timeout_seconds": 120,
    "idle_timeout_seconds": 300,
    "device_token": "",
    "device_token_file": "",
}


def validate_upstream_patch(raw):
    """Strict validation for a panel-saved upstream patch."""
    out = {}
    for key, value in (raw or {}).items():
        if key in ("header_timeout_seconds", "idle_timeout_seconds"):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("%s must be a whole number of seconds" % key)
            if value < 1:
                raise ValueError("%s cannot be less than 1" % key)
            out[key] = value
        elif key in ("device_token", "device_token_file"):
            if not isinstance(value, str):
                raise ValueError("%s must be a string" % key)
            out[key] = value.strip()
        else:
            raise ValueError("unknown upstream setting %r" % key)
    return out


def upstream_config(accounts_dir):
    """Chat socket timeouts and the optional X-Device-Token source."""
    stored = load(accounts_dir).get("upstream")
    stored = stored if isinstance(stored, dict) else {}
    out = {}
    for key, default in UPSTREAM_DEFAULTS.items():
        value = stored.get(key, default)
        if key in ("header_timeout_seconds", "idle_timeout_seconds"):
            out[key] = (value if isinstance(value, int) and not isinstance(value, bool)
                        and value >= 1 else default)
        else:
            out[key] = value.strip() if isinstance(value, str) else default
    return out


def set_upstream_config(accounts_dir, cfg):
    """Persist the upstream settings. Returns the stored config."""
    current = upstream_config(accounts_dir)
    if isinstance(cfg, dict):
        current.update({k: v for k, v in cfg.items() if k in UPSTREAM_DEFAULTS})
    clean = validate_upstream_patch(current)
    with _lock:
        data = load(accounts_dir)
        data["upstream"] = deep_merge(data.get("upstream"), clean)
        save(accounts_dir, data)
    return clean


PROMPT_DEFAULTS = {
    "mode": "passthrough",
    "file": "",
}


def validate_prompt_patch(raw):
    """Strict validation for a panel-saved prompt patch."""
    out = {}
    for key, value in (raw or {}).items():
        if key == "mode":
            if not isinstance(value, str):
                raise ValueError("prompt mode must be a string")
            mode = value.strip().lower()
            if mode not in ("passthrough", "custom", "append"):
                raise ValueError("prompt mode must be passthrough, custom or append")
            out["mode"] = mode
        elif key == "file":
            if not isinstance(value, str):
                raise ValueError("prompt file must be a string")
            out["file"] = value.strip()
        else:
            raise ValueError("unknown prompt setting %r" % key)
    return out


def prompt_config(accounts_dir):
    """Gateway system-prompt mode (default passthrough = legacy behaviour)."""
    stored = load(accounts_dir).get("prompt")
    stored = stored if isinstance(stored, dict) else {}
    mode = stored.get("mode", PROMPT_DEFAULTS["mode"])
    if not isinstance(mode, str) or mode.strip().lower() not in (
            "passthrough", "custom", "append"):
        mode = PROMPT_DEFAULTS["mode"]
    else:
        mode = mode.strip().lower()
    file_path = stored.get("file", PROMPT_DEFAULTS["file"])
    if not isinstance(file_path, str):
        file_path = PROMPT_DEFAULTS["file"]
    return {"mode": mode, "file": file_path.strip()}


def set_prompt_config(accounts_dir, cfg):
    """Persist the prompt settings. Returns the stored config."""
    current = prompt_config(accounts_dir)
    if isinstance(cfg, dict):
        current.update({k: v for k, v in cfg.items() if k in PROMPT_DEFAULTS})
    clean = validate_prompt_patch(current)
    with _lock:
        data = load(accounts_dir)
        data["prompt"] = deep_merge(data.get("prompt"), clean)
        save(accounts_dir, data)
    return clean


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
