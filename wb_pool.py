"""Panel-parity pool governance rules.

Pure decision logic ported from the panel project's internal/pool package
(weighted picking plus soft-rate / breaker / degrade backoff maths) into this
gateway's Python shape. Per-account state lives on wb_accounts.Account; this
module never touches the network or disk.

Defaults mirror the panel project so the behaviour the operator gets is the
one that was exercised there. Every value can be overridden from
accounts/settings.json ("pool" object) and reads back with the same meaning
on upgrade: an install that never writes the object keeps its previous
behaviour plus the new weighted picking.
"""
import random

DEFAULTS = {
    "weighted_pick": True,
    "soft_rate": 600.0,
    "soft_rate_max": 7200.0,
    "breaker_threshold": 3,
    "breaker_cooldown": 1800.0,
    "breaker_cooldown_max": 21600.0,
    "degrade_threshold": 5,
    "degrade_cooldown": 600.0,
    "degrade_cooldown_max": 7200.0,
    "idle_weight_per_hour": 0.5,
    "idle_weight_max": 5.0,
    "max_in_flight": 3,
    "max_in_flight_global": 2,
    "top_n": 5,
    "cost_ledger_ttl": 21600,
    "cost_explore_interval": 1800.0,
    "credit_floor": 0,
    "min_pick_gap": 0.1,
    "affinity_ttl": 7200,
    "affinity_max_entries": 5000,
    "session_dead_threshold": 3,
}

_BOOL_KEYS = ("weighted_pick",)
_INT_MIN = {
    "breaker_threshold": 1,
    "degrade_threshold": 1,
    "max_in_flight": 0,
    "max_in_flight_global": 0,
    "top_n": 1,
    "affinity_ttl": 60,
    "affinity_max_entries": 100,
    "session_dead_threshold": 1,
    "cost_ledger_ttl": 60,
    "credit_floor": 0,
}
_FLOAT_KEYS = (
    "soft_rate",
    "soft_rate_max",
    "breaker_cooldown",
    "breaker_cooldown_max",
    "degrade_cooldown",
    "degrade_cooldown_max",
    "idle_weight_per_hour",
    "idle_weight_max",
    "min_pick_gap",
    "cost_explore_interval",
)


def _positive_int(value, default, minimum):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= minimum else default


def _positive_float(value, default, minimum=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number >= minimum else default


def normalize(cfg):
    """Return a complete, type-checked pool config.

    Unknown keys are dropped and missing keys fall back to the defaults, so a
    hand-edited settings.json cannot leave the pool half-configured. Numbers
    outside a sane range fall back to the default: a negative soft rate would park accounts
    forever, a zero top_n would make the shortlist empty.
    """
    raw = dict(DEFAULTS)
    if isinstance(cfg, dict):
        raw.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    out = dict(DEFAULTS)
    for key in _BOOL_KEYS:
        value = raw.get(key)
        if isinstance(value, bool):
            out[key] = value
        elif isinstance(value, int) and value in (0, 1):
            # A hand-edited 0/1 means off/on; the old `is not False` test
            # turned 0 into True - the opposite intent (audit #11).
            out[key] = bool(value)
        else:
            out[key] = DEFAULTS[key]
    for key, minimum in _INT_MIN.items():
        out[key] = _positive_int(raw.get(key), DEFAULTS[key], minimum)
    for key in _FLOAT_KEYS:
        out[key] = _positive_float(raw.get(key), DEFAULTS[key], 0.0)
    return out


def validate_patch(raw):
    """Validate a settings.json patch. Raises ValueError on a bad value.

    Only known keys are accepted, so a typo in the panel cannot silently
    create a second, ignored setting. bool is rejected where an integer is
    expected ("true" for a threshold is the classic truthy-string bug).
    """
    out = {}
    for key, value in (raw or {}).items():
        if key not in DEFAULTS:
            raise ValueError("unknown pool setting %r" % key)
        if key in _BOOL_KEYS:
            if not isinstance(value, bool):
                raise ValueError("%s must be true or false" % key)
        elif key in _INT_MIN:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("%s must be a whole number" % key)
            if value < _INT_MIN[key]:
                raise ValueError("%s cannot be less than %d" % (key, _INT_MIN[key]))
        elif key in _FLOAT_KEYS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("%s must be a number" % key)
            if float(value) < 0:
                raise ValueError("%s cannot be negative" % key)
        out[key] = value
    return out


def next_local_4am(now=None):
    """Epoch of the next local 04:00 (panel's balance-recovery wall).

    The panel project parks an out-of-credits account until 04:00 local
    so the 09:00/21:00 check-ins can revive it; the same wall clock is
    used here. A timestamp already past 04:00 rolls to tomorrow.
    """
    import time as _time
    now = _time.time() if now is None else now
    lt = _time.localtime(now)
    stamp = _time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                          4, 0, 0, 0, 0, -1))
    if stamp <= now:
        stamp += 86400
    return stamp


def soft_backoff(streak, base, cap):
    """Cooldown seconds for `streak` consecutive account-level soft limits."""
    step = max(0, int(streak) - 1)
    return min(float(base) * (2 ** min(step, 20)), float(cap))


def breaker_backoff(fails, threshold, base, cap):
    """Breaker cooldown for `fails` consecutive failures (base at threshold)."""
    step = max(0, int(fails) - int(threshold))
    return min(float(base) * (2 ** min(step, 20)), float(cap))


def degrade_backoff(fails, threshold, base, cap):
    """Degrade window for `fails` consecutive unknown failures."""
    return breaker_backoff(fails, threshold, base, cap)


def credits_remain(account):
    """Known remaining credits, or None when the balance was never fetched."""
    credits = getattr(account, "credits", None)
    if not isinstance(credits, dict):
        return None
    try:
        return int(credits.get("remain"))
    except (TypeError, ValueError):
        return None


def account_weight(account, max_remain, now, cfg):
    """Panel-parity weight: credits share * 10 + idle compensation.

    An account whose balance was never fetched counts as full share instead of
    zero, so a fresh install does not starve the accounts it has not measured
    yet. "Never used" earns the full idle bonus, which is what makes a newly
    added credential get picked.
    """
    remain = credits_remain(account)
    if remain is None or not max_remain or max_remain <= 0:
        ratio = 1.0
    else:
        ratio = min(1.0, max(0.0, float(remain) / float(max_remain)))
    last = float(getattr(account, "last_used_at", 0.0) or 0.0)
    if last <= 0:
        idle = float(cfg["idle_weight_max"])
    else:
        idle = min((now - last) / 3600.0 * float(cfg["idle_weight_per_hour"]),
                   float(cfg["idle_weight_max"]))
    return ratio * 10.0 + idle


def weights_for(candidates, cfg, now):
    remains = [credits_remain(a) for a in candidates]
    known = [r for r in remains if r is not None]
    max_remain = max(known) if known else 0
    return [account_weight(a, max_remain, now, cfg) for a in candidates]


def choose(candidates, cfg, now=None, rng=None):
    """Pick one account with the panel's shortlist + weighted-random rule.

    Equal-weight candidates are shuffled before the weight sort, so the Top-5
    cut never starves the same uid; a candidate picked within min_pick_gap is
    skipped unless every shortlisted candidate was used that recently.
    """
    import time as _time
    rng = rng or random
    now = _time.time() if now is None else now
    candidates = list(candidates)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    weights = weights_for(candidates, cfg, now)
    order = list(candidates)
    rng.shuffle(order)
    weight_of = {id(a): w for a, w in zip(candidates, weights)}
    order.sort(key=lambda a: -weight_of[id(a)])
    top_n = max(1, int(cfg.get("top_n") or 5))
    shortlist = order[:top_n]
    gap = max(0.0, float(cfg.get("min_pick_gap") or 0.0))
    if gap:
        fresh = [a for a in shortlist
                 if now - float(getattr(a, "last_used_at", 0.0) or 0.0) >= gap]
        if fresh:
            shortlist = fresh
    pool_weights = [max(1e-6, weight_of[id(a)]) for a in shortlist]
    return rng.choices(shortlist, weights=pool_weights, k=1)[0]
