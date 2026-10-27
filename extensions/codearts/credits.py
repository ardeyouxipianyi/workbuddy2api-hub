"""Local credit ledger for CodeArts turns (opt-in, per Hub API key).

The CodeArts backend bills Huawei credits upstream, but the usage row the Hub
writes for a `codeart/*` turn carries `credit: 0`: nothing on the gateway says
what that turn cost. This module prices those turns against the multiplier
table the CodeArts model picker publishes - GLM-5.2 0.70x, OpenPangu-2.0-Pro
0.70x, OpenPangu-2.0-Flash 0.32x, GLM-5.3 0.79x, DeepSeek-V4.1-Flash 0.18x,
Kimi-K3 2.38x - lets the operator sell credits to Hub API keys, and (when
enforcement is on) refuses to start a turn once a key's balance is used up.

Credits for one turn: tokens / 1000 * rate * base_per_1k. `rate` is the model's
published day rate, replaced by its night rate inside the configured night
window (default 23:00-08:00; DeepSeek-V4.1-Flash 0.10 instead of 0.18). A model
that is not in the table is charged at 1.00x rather than nothing, so a newly
listed model cannot silently bill zero. A variant such as
`deepseek-v4.1-flash-1017` inherits the rate of its longest matching base id.

Everything here is local to the extension: the Hub's own settings, usage log and
account pool are not touched. Balances live in `credits.json` next to the
bridge configuration (rewritten atomically, one balance per key) and every
charge or adjustment appends one line to `credits-events.jsonl` (rotated once
at 8 MiB). A malformed `credits.json` is moved aside instead of overwritten, so
a hand edit can never destroy the balances silently.

Only the Python standard library is required.
"""

import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time

#: Credit multipliers published by the CodeArts model picker (day rate).
BUILTIN_MULTIPLIERS = {
    "glm-5.2": 0.70,
    "glm-5.2-arkts-spark": 0.70,
    "openpangu-2.0-pro": 0.70,
    "openpangu-2.0-flash": 0.32,
    "glm-5.3": 0.79,
    "deepseek-v4.1-flash": 0.18,
    "kimi-k3": 2.38,
}

#: Night rates that replace the day rate while the night window is open.
BUILTIN_NIGHT_MULTIPLIERS = {
    "deepseek-v4.1-flash": 0.10,
}

DEFAULT_NIGHT_WINDOW = "23:00-08:00"
#: Rate for a model the tables do not name. 1.00 charges the base price rather
#: than nothing, and is visible in the panel as the table's fallback.
DEFAULT_MULTIPLIER = 1.0
EVENTS_BACKUP_BYTES = 8 * 1024 * 1024
_KEY_MAX = 128

_WINDOW_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def canonical_model(model):
    """The table key for one model name: no `codeart/` prefix, lower case."""
    name = str(model or "").strip()
    for prefix in ("codeart/", "codearts/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name.strip().lower()


def parse_windows(text):
    """Parse `23:00-08:00` / `23:00-08:00,12:00-13:00` into minute pairs.

    Empty text means "no night window" and returns an empty list. Windows that
    cross midnight are allowed and handled by :func:`in_window`.
    """
    raw = str(text or "").strip()
    if not raw:
        return []
    windows = []
    for part in raw.split(","):
        chunk = part.strip()
        if not chunk:
            continue
        pair = chunk.split("-")
        if len(pair) != 2:
            raise ValueError("must look like 23:00-08:00")
        start = _clock(pair[0])
        end = _clock(pair[1])
        if start == end:
            raise ValueError("must not start and end at the same time")
        windows.append((start, end))
    return windows


def _clock(text):
    match = _WINDOW_RE.match(str(text or "").strip())
    if match is None:
        raise ValueError("must look like 23:00-08:00")
    return int(match.group(1)) * 60 + int(match.group(2))


def format_windows(windows):
    """Render parsed windows back as `23:00-08:00` (panel output)."""
    return ", ".join("%02d:%02d-%02d:%02d" % (s // 60, s % 60, e // 60, e % 60)
                     for s, e in windows)


def in_window(windows, at=None):
    """True when the local wall clock of `at` falls inside any window."""
    if not windows:
        return False
    moment = time.localtime(time.time() if at is None else float(at))
    minute = moment.tm_hour * 60 + moment.tm_min
    for start, end in windows:
        if start < end:
            if start <= minute < end:
                return True
        elif minute >= start or minute < end:
            # Crosses local midnight: 23:00-08:00 is 23:00..23:59 plus 00:00..07:59.
            return True
    return False


def effective_multipliers(overrides=None):
    """The built-in day table with the operator's overrides applied."""
    table = dict(BUILTIN_MULTIPLIERS)
    table.update(clean_multiplier_table(overrides))
    return table


def effective_night_multipliers(overrides=None):
    """The built-in night table with the operator's overrides applied."""
    table = dict(BUILTIN_NIGHT_MULTIPLIERS)
    table.update(clean_multiplier_table(overrides))
    return table


def clean_multiplier_table(table, label="multipliers"):
    """Validate and canonicalise one `{model: rate}` table from the settings."""
    if table is None:
        return {}
    if not isinstance(table, dict):
        raise ValueError(label + " must be a JSON object of model id to rate")
    out = {}
    for raw_name, raw_rate in table.items():
        if not isinstance(raw_name, str):
            raise ValueError(label + " keys must be model ids")
        name = canonical_model(raw_name)
        if not name:
            raise ValueError(label + " keys must be non-empty model ids")
        rate = _number(raw_rate, label + " rates")
        if rate < 0 or rate > 1000:
            raise ValueError(label + " rates must be between 0 and 1000")
        out[name] = float(rate)
    return out


def _match(name, table):
    """Exact id first, then the longest prefix (`...-1017` inherits its base)."""
    if not name:
        return None
    if name in table:
        return name
    best = None
    for key in table:
        if name.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    return best


def resolve_multiplier(model, multipliers=None, night_multipliers=None,
                       windows=None, at=None):
    """The rate that applies to `model` right now.

    Returns `{model, key, multiplier, night_rate, in_night_window}`; `night_rate`
    says the night table actually replaced the day rate, which is what the panel
    and the event log show. A model missing from both tables gets
    `DEFAULT_MULTIPLIER`.
    """
    name = canonical_model(model)
    day_table = dict(BUILTIN_MULTIPLIERS if multipliers is None else multipliers)
    night_table = dict(BUILTIN_NIGHT_MULTIPLIERS
                       if night_multipliers is None else night_multipliers)
    windows = list(windows or ())
    day_key = _match(name, day_table)
    night_key = _match(name, night_table)
    in_night = in_window(windows, at)
    multiplier = day_table[day_key] if day_key else DEFAULT_MULTIPLIER
    applied = False
    if in_night and night_key:
        multiplier = night_table[night_key]
        applied = True
    return {
        "model": name,
        "key": day_key or night_key,
        "multiplier": round(float(multiplier), 4),
        "night_rate": applied,
        "in_night_window": bool(in_night),
    }


def credits_for(prompt_tokens, completion_tokens, multiplier, base_per_1k):
    """Credits one turn costs: tokens / 1000 * rate * base price."""
    tokens = max(0, int(prompt_tokens or 0)) + max(0, int(completion_tokens or 0))
    rate = _number(multiplier, "multiplier")
    base = _number(base_per_1k, "base_per_1k")
    if rate < 0 or base < 0:
        raise ValueError("rate and base price must not be negative")
    return round(tokens / 1000.0 * rate * base, 4)


def _number(value, label="credit amount", default=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        if default is not None:
            return default
        raise ValueError(label + " must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(label + " must be finite")
    return number


def _iso(at=None):
    return time.strftime("%Y-%m-%d %H:%M:%S",
                         time.localtime(time.time() if at is None else float(at)))


def _empty_entry():
    return {"balance": 0.0, "spent": 0.0, "granted": 0.0,
            "requests": 0, "updated_at": ""}


def _clean_entry(entry):
    """One stored balance; anything unreadable becomes an empty one."""
    if not isinstance(entry, dict):
        return _empty_entry()
    out = _empty_entry()
    for name in ("balance", "spent", "granted"):
        out[name] = round(_number(entry.get(name), "credits.json " + name, 0.0), 4)
    requests = entry.get("requests")
    if isinstance(requests, int) and not isinstance(requests, bool) and requests >= 0:
        out["requests"] = requests
    out["updated_at"] = str(entry.get("updated_at") or "")[:32]
    return out


def _clean_key_id(key_id):
    if not isinstance(key_id, str):
        raise ValueError("credit key id must be a string")
    key = key_id.strip()
    if not key or len(key) > _KEY_MAX or any(c in key for c in "\r\n\x00"):
        raise ValueError("credit key id must be a non-empty single-line string")
    return key


class CreditLedger:
    """One balance per Hub API key plus an append-only event log."""

    VERSION = 1

    def __init__(self, path, events_path=None):
        self.path = Path(path)
        self.events_path = (Path(events_path) if events_path
                            else self.path.with_name("credits-events.jsonl"))
        self.lock = threading.RLock()
        self._state = None
        #: Path of the file moved aside when credits.json could not be parsed.
        self.recovered_backup = ""

    # -- stored state ---------------------------------------------------
    def _load(self):
        if self._state is not None:
            return self._state
        state = {"version": self.VERSION, "keys": {}}
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            self._state = state
            return state
        try:
            data = json.loads(raw.decode("utf-8-sig"))
            keys = data.get("keys") if isinstance(data, dict) else None
            if not isinstance(keys, dict):
                raise ValueError("credits.json has no keys map")
            for key_id, entry in keys.items():
                state["keys"][str(key_id)] = _clean_entry(entry)
        except (ValueError, TypeError, AttributeError):
            # Never overwrite a file we could not read: move it aside so the
            # operator can still recover it by hand, and start empty.
            backup = self.path.with_name("credits.corrupt-%d.json" % int(time.time()))
            try:
                os.replace(self.path, backup)
                self.recovered_backup = str(backup)
            except OSError:
                pass
            state["keys"] = {}
        self._state = state
        return state

    def _save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".codearts-credits-",
                                         dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump({"version": self.VERSION, "keys": state["keys"]},
                          handle, ensure_ascii=False, indent=1, sort_keys=True)
                handle.write("\n")
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _append_event(self, event):
        try:
            if self.events_path.stat().st_size > EVENTS_BACKUP_BYTES:
                os.replace(self.events_path,
                           self.events_path.with_name(self.events_path.name + ".1"))
        except FileNotFoundError:
            pass
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    # -- reads ----------------------------------------------------------
    def entries(self):
        """A copy of every stored balance, keyed by API key id."""
        with self.lock:
            return {key: dict(entry) for key, entry in self._load()["keys"].items()}

    def entry(self, key_id):
        key = _clean_key_id(key_id)
        with self.lock:
            stored = self._load()["keys"].get(key)
            return dict(stored) if stored else _empty_entry()

    def balance(self, key_id):
        return float(self.entry(key_id)["balance"])

    def events(self, limit=30):
        """The newest `limit` events, newest first."""
        limit = max(1, int(limit or 1))
        try:
            size = self.events_path.stat().st_size
            with self.events_path.open("rb") as handle:
                # Read the tail only: the log grows for the life of the install.
                if size > 256 * 1024:
                    handle.seek(-256 * 1024, os.SEEK_END)
                    handle.readline()          # drop the half line the seek cut
                raw = handle.read()
        except FileNotFoundError:
            return []
        out = []
        for line in reversed(raw.decode("utf-8", "replace").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                out.append(event)
                if len(out) >= limit:
                    break
        return out

    # -- writes ---------------------------------------------------------
    def grant(self, key_id, amount, note=""):
        """Add `amount` credits (a negative amount debits) and log it."""
        key = _clean_key_id(key_id)
        amount = _number(amount)
        if amount == 0:
            raise ValueError("credit amount must not be zero")
        note = str(note or "")[:200]
        with self.lock:
            state = self._load()
            entry = state["keys"].setdefault(key, _empty_entry())
            entry["balance"] = round(float(entry["balance"]) + amount, 4)
            entry["granted"] = round(float(entry["granted"]) + amount, 4)
            entry["updated_at"] = _iso()
            self._save(state)
            self._append_event({"type": "grant", "key": key, "credits": amount,
                                "balance": entry["balance"], "note": note,
                                "at": entry["updated_at"]})
            return dict(entry)

    def charge(self, key_id, amount, meta=None):
        """Deduct one turn's credits and log it with the pricing evidence."""
        key = _clean_key_id(key_id)
        amount = _number(amount)
        if amount <= 0:
            raise ValueError("credit charge must be positive")
        meta = meta if isinstance(meta, dict) else {}
        with self.lock:
            state = self._load()
            entry = state["keys"].setdefault(key, _empty_entry())
            entry["balance"] = round(float(entry["balance"]) - amount, 4)
            entry["spent"] = round(float(entry["spent"]) + amount, 4)
            entry["requests"] = int(entry["requests"]) + 1
            entry["updated_at"] = _iso()
            self._save(state)
            event = {"type": "charge", "key": key, "credits": amount,
                     "balance": entry["balance"], "at": entry["updated_at"]}
            for name in ("model", "upstream_model", "tokens", "multiplier",
                         "night", "base_per_1k"):
                value = meta.get(name)
                if value is not None:
                    event[name] = value
            self._append_event(event)
            return dict(entry)


def panel_view(config, ledger, key_names=None, now=None):
    """Everything the panel's credits section renders, from local state only.

    `key_names` maps key ids to the names configured in the Hub, so a balance
    row still says which key it belongs to after a rename; a key with no
    balance yet is listed too (balance 0), because funding it is the first
    step of turning enforcement on.
    """
    windows = parse_windows(config.get("credits_night_window"))
    day = effective_multipliers(config.get("credits_multipliers"))
    night = effective_night_multipliers(config.get("credits_night_multipliers"))
    overridden = (set(clean_multiplier_table(config.get("credits_multipliers")))
                  | set(clean_multiplier_table(config.get("credits_night_multipliers"))))
    names = dict(key_names or {})
    entries = ledger.entries()

    keys = []
    totals = {"balance": 0.0, "spent": 0.0, "granted": 0.0, "requests": 0}
    for key_id in sorted(set(names) | set(entries)):
        stored = entries.get(key_id) or _empty_entry()
        item = {
            "id": key_id,
            "name": names.get(key_id) or key_id,
            "balance": round(float(stored["balance"]), 4),
            "spent": round(float(stored["spent"]), 4),
            "granted": round(float(stored["granted"]), 4),
            "requests": int(stored["requests"]),
            "updated_at": stored["updated_at"],
            "billed": key_id in entries,
        }
        totals["balance"] = round(totals["balance"] + item["balance"], 4)
        totals["spent"] = round(totals["spent"] + item["spent"], 4)
        totals["granted"] = round(totals["granted"] + item["granted"], 4)
        totals["requests"] += item["requests"]
        keys.append(item)

    models = []
    for name in sorted(set(day) | set(night)):
        models.append({
            "model": name,
            "day": round(float(day.get(name, DEFAULT_MULTIPLIER)), 4),
            "night": (round(float(night[name]), 4) if name in night else None),
            "source": "override" if name in overridden else "builtin",
        })

    moment = time.time() if now is None else float(now)
    return {
        "enabled": bool(config.get("credits_enabled")),
        "enforce": bool(config.get("credits_enforce")),
        "base_per_1k": round(_number(config.get("credits_base_per_1k"), "credits_base_per_1k", 1.0), 4),
        "night_window": str(config.get("credits_night_window") or ""),
        "windows": [{"start": "%02d:%02d" % (s // 60, s % 60),
                     "end": "%02d:%02d" % (e // 60, e % 60)} for s, e in windows],
        "night_now": in_window(windows, moment),
        "local_time": time.strftime("%H:%M", time.localtime(moment)),
        "default_multiplier": DEFAULT_MULTIPLIER,
        "multipliers": models,
        "keys": keys,
        "totals": totals,
        "events": ledger.events(30),
        "recovered_from": ledger.recovered_backup,
    }
