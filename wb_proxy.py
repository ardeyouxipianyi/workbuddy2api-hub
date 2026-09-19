#!/usr/bin/env python3
"""WorkBuddy (workbuddy.ai) -> OpenAI-compatible reverse proxy.
Reuses the credentials the WorkBuddy desktop app already stored on this machine
(%%LOCALAPPDATA%%\\CodeBuddyExtension\\Data\\Public\\auth\\*.info), so no separate
login is needed. Exposes:
    GET  /v1/models
    POST /v1/chat/completions     (stream=true and stream=false)
    GET  /health
Only the Python standard library is required.
    python wb_proxy.py                    # bind 127.0.0.1:8788
    python wb_proxy.py --port 9000
    python wb_proxy.py --api-key sk-local # require a bearer token
"""
import argparse
import hashlib
from collections import deque
import re
import json
import os
MAX_PAYLOAD_BYTES = int(os.environ.get("WB_MAX_PAYLOAD_BYTES", 50 * 1024 * 1024))  # 50MB limit
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import wb_accounts
import wb_catalog
import wb_settings
CURRENT_REALM = os.environ.get("WB_PROXY_DEFAULT_REALM", "intl")
def detect_model_realm(model_id):
    if not model_id:
        return CURRENT_REALM
    m = str(model_id).lower()
    intl_only = {
        "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
        "gpt-5.5", "gpt-5.4", "gpt-5.3-codex", "gemini-3.5-flash"
    }
    if m in intl_only or any(m.startswith(p) for p in ("gpt-", "gemini-")):
        return "intl"
    cn_only = {
        "deepseek-v4-pro", "minimax-m3", "minimax-m2.7", "minimax-m2.5",
        "glm-5.3-flash", "glm-5.1", "glm-5.0-turbo", "glm-4.6v",
        "kimi-k3-1", "kimi-k2.8-preview", "kimi-k2.7", "kimi-k2-thinking",
        "hy3-x", "hy4-preview-dev", "hy4-preview-x"
    }
    if m in cn_only or any(m.startswith(p) for p in ("minimax-", "deepseek-v4-pro")):
        return "cn"
    return CURRENT_REALM
# Models that exist on one side only. Everything else (deepseek-v4.1-flash,
# hy3, glm-5.3 ...) is served by both exits, so it must not be treated as a
# conflict.
INTL_EXCLUSIVE_PREFIXES = ("gpt-", "gemini-")
CN_EXCLUSIVE_PREFIXES = ("minimax-", "deepseek-v4-pro")
INTL_EXCLUSIVE = {
    "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
    "gpt-5.5", "gpt-5.4", "gpt-5.3-codex", "gemini-3.5-flash",
}
CN_EXCLUSIVE = {
    "deepseek-v4-pro", "glm-5.3-flash", "glm-5.1", "glm-5v-turbo",
    "kimi-k3-1", "kimi-k2.8-preview", "kimi-k2.7", "minimax-m3",
    "hy3-x", "hy4-preview-dev", "hy4-preview-x",
}
def exclusive_realm(model_id):
    """"intl"/"cn" when only that exit serves the model, else ""."""
    if not model_id:
        return ""
    m = str(model_id).lower()
    if m in INTL_EXCLUSIVE or m.startswith(INTL_EXCLUSIVE_PREFIXES):
        return "intl"
    if m in CN_EXCLUSIVE or m.startswith(CN_EXCLUSIVE_PREFIXES):
        return "cn"
    return ""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
def install_console_close_handler():
    """Release the port when the console window is closed by the user.
    Windows does not kill child processes when a console window closes, so
    the proxy (started by the .bat as a child of cmd.exe) would survive and
    keep the port bound - the next launch then wrongly reports "another
    proxy is already running".
    Closing the window raises CTRL_CLOSE_EVENT in every process attached to
    that console, which is exactly the signal we want. Registering a handler
    for it is event-driven, so unlike polling a parent pid there is no
    chance of a false positive. Harmless when started without a console.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        PHANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        CTRL_CLOSE_EVENT = 2
        CTRL_LOGOFF_EVENT = 5
        CTRL_SHUTDOWN_EVENT = 6
        def _handler(event):
            if event in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
                try:
                    sys.stdout.flush()
                except Exception:
                    pass
                os._exit(0)
            return False
        handler = PHANDLER_ROUTINE(_handler)   # keep the callback referenced
        if not ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True):
            return None
        return handler
    except Exception:
        return None
UPSTREAM = "https://www.workbuddy.ai"
CHAT_PATH = "/v2/chat/completions"
MODELS_PATH = "/v2/enterprises/personal/models"
DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."
# The WorkBuddy AI desktop app caches its account product config here on every
# launch. That file carries the real model catalog the app shows in its picker
# (21 models, incl. deepseek-v4.1-flash / gpt-6-astra) - the CLI-facing
# /v2/enterprises/personal/models endpoint returns a narrower list, so prefer
# the cache and fall back to the endpoint.
PRODUCT_CONFIG_CACHE = os.path.join(os.path.expanduser("~"), ".workbuddy-ai", "cache", "acc-product-config-v3.json")
NOISE_KEYS = ("extra_fields", "refusal", "reasoning_content")
class BodyTooLarge(Exception):
    """Raised when a request body exceeds the configured cap."""
    def __init__(self, length):
        super(BodyTooLarge, self).__init__(length)
        self.length = length
class BadJSON(Exception):
    """Raised when a request body is present but not a JSON object."""
# CORS is only needed by browser-based chat clients that call the OpenAI-style
# API from another origin. Management routes (accounts, settings, usage,
# scheduler, panel) serve the dashboard, which is same-origin, so they get no
# ACAO header - that keeps a stray page on the LAN from reading their replies.
CORS_PATH_PREFIXES = ("/v1", "/chat", "/completions", "/models", "/responses")
# Management paths that happen to live under /v1 must not be treated as API:
# /v1/usage reports account-level spend and is gated by the panel session.
MANAGEMENT_PATH_PREFIXES = ("/v1/usage", "/usage", "/accounts", "/settings",
                            "/tasks", "/scheduler", "/panel", "/logs")
def cors_origin_allowed(path):
    """True when the OpenAI-style API path should advertise CORS."""
    path = (path or "").split("?")[0]
    if path.startswith(MANAGEMENT_PATH_PREFIXES):
        return False
    return path.startswith(CORS_PATH_PREFIXES)
_lock = threading.Lock()
_login_lock = threading.Lock()
_login_attempts = {}  # ip -> list of timestamp
def _prune_login_attempts(now=None, window=60):
    """Drop stale per-IP entries so the dict cannot grow without bound.
    Caller must hold _login_lock.
    """
    now = now or time.time()
    for ip in list(_login_attempts.keys()):
        recent = [t for t in _login_attempts[ip] if now - t < window]
        if recent:
            _login_attempts[ip] = recent
        else:
            del _login_attempts[ip]
_models_cache = {"intl": {"at": 0.0, "data": None}, "cn": {"at": 0.0, "data": None}}
# Usage accounting: every upstream response carries a usage block, and the
# proxy also records one JSONL line per request. Defaults to a folder next to
# this script; override with --usage-dir or WB_PROXY_USAGE_DIR.
USAGE_DIR = os.environ.get("WB_PROXY_USAGE_DIR") \
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage")
USAGE_LOG = os.path.join(USAGE_DIR, "usage.jsonl")
DASHBOARD_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "reasoning_tokens",
                "cached_tokens", "total_tokens", "credit")
# Web-panel access control. The panel is gated by its own password (default
# "admin"), independent of the /v1 API key. Sessions live in memory only, so a
# restart forces browsers to log in again.
PANEL = wb_settings.PanelSessions()
API_KEY_FILE_SET = False
def configured_keys():
    """Panel-managed API keys, always read fresh so panel edits apply at once."""
    try:
        return wb_settings.api_keys(ACCOUNTS_DIR)
    except Exception as exc:
        log("could not read api keys: %s" % exc)
        return []
def auth_required():
    """Whether /v1 calls must present a key at all."""
    if wb_settings.auth_disabled(ACCOUNTS_DIR):
        return False
    if any(entry.get("enabled") for entry in configured_keys()):
        return True
    return bool(API_KEY)
def identify_key(supplied):
    """Return the key entry a caller used, or None when nothing matches.
    Once the panel has at least one key, those keys are the only accepted
    credentials - otherwise a launcher key left in a .bat file would silently
    keep working after the panel was locked down.
    """
    extra = () if configured_keys() else (API_KEY,)
    return wb_settings.match_api_key(ACCOUNTS_DIR, supplied, extra_keys=extra)
def _empty_stats():
    return {"requests": 0, "errors": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "reasoning_tokens": 0, "cached_tokens": 0, "total_tokens": 0,
            "credit": 0.0, "started": time.time(), "by_model": {},
            # latency accumulators (averages; percentiles come from the JSONL)
            "ttft_ms_sum": 0, "ttft_samples": 0,
            "gen_ms_sum": 0, "gen_samples": 0,
            "wall_ms_sum": 0, "wall_samples": 0}
_usage = _empty_stats()
def _extract_usage(usage):
    """Normalize the upstream usage block into the fields we track."""
    if not usage:
        return {}
    details = usage.get("completion_tokens_details") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    return {
        "prompt_tokens": usage.get("prompt_tokens") or 0,
        "completion_tokens": usage.get("completion_tokens") or 0,
        "reasoning_tokens": details.get("reasoning_tokens") or 0,
        "cached_tokens": usage.get("prompt_cache_hit_tokens") or details.get("cached_tokens") \
            or prompt_details.get("cached_tokens") or 0,
        "total_tokens": usage.get("total_tokens") or 0,
        "credit": usage.get("credit") or 0,
    }
def row_matches_realm(row, realm):
    if not realm: return True
    r = row.get("realm")
    if r: return r == realm
    acct_uid = row.get("account")
    if acct_uid and POOL:
        acc = POOL.get(acct_uid)
        if acc: return acc.realm == realm
    model = row.get("model")
    if model: return detect_model_realm(model) == realm
    return realm == "intl"
def record_usage(model, usage, stream=None, elapsed_ms=None, ttft_ms=None, gen_ms=None, fp=None,
                account=None):
    """Accumulate stats, append a JSONL row, and persist the summary."""
    fields = _extract_usage(usage)
    if not fields:
        return None
    row = {
        "at": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": model,
        "stream": bool(stream),
        "elapsed_ms": elapsed_ms,
        "ttft_ms": ttft_ms,
        "gen_ms": gen_ms,
    }
    row.update(fields)
    if fp:
        row.update(fp)
    if account:
        row["account"] = account
    acc = POOL.get(account) if (account and POOL) else None
    row["realm"] = acc.realm if acc else CURRENT_REALM
    # Derived per-request rates (None-safe).
    if gen_ms and gen_ms > 0:
        row["tokens_per_sec"] = round(fields["completion_tokens"] / (gen_ms / 1000.0), 2)
    # Share the denominator with the aggregate view (compute_usage_analytics),
    # otherwise the per-request row and the rollup disagree on the same data.
    if fields["prompt_tokens"] > 0:
        row["cache_hit_pct"] = round(fields["cached_tokens"] * 100.0 / fields["prompt_tokens"], 1)
    with _lock:
        _usage["requests"] += 1
        for k in USAGE_FIELDS:
            if k in fields:
                _usage[k] += fields[k]
        if ttft_ms is not None:
            _usage["ttft_ms_sum"] += ttft_ms
            _usage["ttft_samples"] += 1
        if gen_ms is not None:
            _usage["gen_ms_sum"] += gen_ms
            _usage["gen_samples"] += 1
        if elapsed_ms is not None:
            _usage["wall_ms_sum"] += elapsed_ms
            _usage["wall_samples"] += 1
        per = _usage["by_model"].setdefault(model, {"requests": 0, **{k: 0 for k in USAGE_FIELDS}})
        per["requests"] += 1
        for k in USAGE_FIELDS:
            if k in fields:
                per[k] += fields[k]
    _persist_usage(row, "usage persist failed")
    try:
        t_tokens = fields.get("total_tokens", 0)
        dur = f" {elapsed_ms:.0f}ms" if elapsed_ms is not None else ""
        acc_tag = f" acct={account[:8]}" if account else ""
        speed_tag = f" {row.get('tokens_per_sec', 0)}t/s" if row.get("tokens_per_sec") else ""
        log(f"chat done: model={model}{acc_tag}{dur} tokens={t_tokens} (in={fields.get('prompt_tokens',0)} out={fields.get('completion_tokens',0)}){speed_tag}", tag="chat")
    except Exception:
        pass
    return row


def _persist_usage(row, fail_label):
    """Append one usage row as a JSONL line.

    usage-summary.json used to be rewritten on every single request - a full
    json.dumps of the running totals, a uniquely named temp file and an
    os.replace, plus the deep copy that fed it. Nothing in the tree ever
    loads that file (every aggregate re-reads usage.jsonl), so the work was
    pure overhead on the request path. One append per request now.
    """
    try:
        os.makedirs(USAGE_DIR, exist_ok=True)
        with open(USAGE_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:
        log("%s: %s" % (fail_label, exc))

def record_error(model, status, message, elapsed_ms=None, account=None):
    """Count a failed request and append it to the log so errors are visible.

    Passing the account uid records which account the request was bound to, so
    per-realm success rates attribute the failure by fact instead of falling
    back to guessing from the model name.
    """
    row = {
        "at": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": model,
        "error": True,
        "status": status,
        "message": str(message)[:200],
        "elapsed_ms": elapsed_ms,
    }
    if account:
        row["account"] = account
        acc = POOL.get(account) if POOL else None
        row["realm"] = acc.realm if acc else CURRENT_REALM
    with _lock:
        _usage["errors"] += 1
        if elapsed_ms is not None:
            _usage["wall_ms_sum"] += elapsed_ms
            _usage["wall_samples"] += 1
    _persist_usage(row, "error persist failed")
    dur = f" {elapsed_ms:.0f}ms" if elapsed_ms is not None else ""
    log(f"request error: model={model}{dur} status={status} msg={str(message)[:180]}", level="ERROR", tag="chat")
    return row
def _pct(values, q):
    """Nearest-rank percentile (no interpolation) - good enough for latency."""
    if not values:
        return None
    ordered = sorted(values)
    idx = int(round((q / 100.0) * (len(ordered) - 1)))
    return ordered[max(0, min(len(ordered) - 1, idx))]
_perf_cache = {}
_perf_lock = threading.Lock()


def perf_stats(sample=5000, realm=None, ttl=10):
    """Cached wrapper: parsing thousands of rows is CPU-heavy, and the
    dashboard polls this endpoint every few seconds."""
    r = realm or CURRENT_REALM
    try:
        key = (int(sample), r)
    except Exception:
        key = (5000, r)
    now = time.time()
    with _perf_lock:
        hit = _perf_cache.get(key)
        if hit is not None and (now - hit[0]) < ttl:
            return hit[1]
    data = _perf_stats_uncached(sample, realm)
    with _perf_lock:
        _perf_cache[key] = (time.time(), data)
    return data


def _perf_stats_uncached(sample=5000, realm=None):
    """Latency percentiles + derived rates, computed from the JSONL log."""
    ttfts, gens, walls, rates, hits, tok_rates = [], [], [], [], [], []
    total = ok = err = 0
    # 按模型聚合性能指标
    m_buckets = {}
    # 只读日志末尾 sample 行：原先 readlines() 会把整个日志读成字符串列表
    rows = [raw.decode("utf-8", "replace") for raw in _tail_lines(USAGE_LOG, sample)]
    for line in rows:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if realm and not row_matches_realm(r, realm):
            continue
        total += 1
        if r.get("error"):
            err += 1
            if r.get("elapsed_ms"):
                walls.append(r["elapsed_ms"])
            continue
        ok += 1
        if r.get("ttft_ms") is not None:
            ttfts.append(r["ttft_ms"])
        if r.get("gen_ms") is not None:
            gens.append(r["gen_ms"])
        if r.get("elapsed_ms") is not None:
            walls.append(r["elapsed_ms"])
        if r.get("tokens_per_sec"):
            tok_rates.append(r["tokens_per_sec"])
        if r.get("cache_hit_pct") is not None:
            hits.append(r["cache_hit_pct"])
        # 模型分桶记录
        m_id = r.get("model") or "unknown"
        mb = m_buckets.setdefault(m_id, {"total": 0, "ok": 0, "err": 0, "ttfts": [], "gens": [], "walls": [], "tok_rates": [], "hits": []})
        mb["total"] += 1
        if r.get("error"):
            mb["err"] += 1
        else:
            mb["ok"] += 1
        if r.get("ttft_ms") is not None: mb["ttfts"].append(r["ttft_ms"])
        if r.get("gen_ms") is not None: mb["gens"].append(r["gen_ms"])
        if r.get("elapsed_ms") is not None: mb["walls"].append(r["elapsed_ms"])
        if r.get("tokens_per_sec"): mb["tok_rates"].append(r["tokens_per_sec"])
        if r.get("cache_hit_pct") is not None: mb["hits"].append(r["cache_hit_pct"])
    def block(vals):
        if not vals:
            return None
        return {
            "avg": round(sum(vals) / len(vals), 1),
            "p50": _pct(vals, 50),
            "p90": _pct(vals, 90),
            "p99": _pct(vals, 99),
            "max": max(vals),
            "samples": len(vals),
        }
    return {
        "sampled": total,
        "success": ok,
        "errors": err,
        "success_rate_pct": round(ok * 100.0 / total, 1) if total else None,
        "ttft_ms": block(ttfts),
        "generation_ms": block(gens),
        "wall_ms": block(walls),
        "tokens_per_sec": block(tok_rates),
        "cache_hit_pct": block(hits),
        "by_model": {
            mid: {
                "requests": mb["total"],
                "errors": mb["err"],
                "success_rate_pct": round(mb["ok"] * 100.0 / mb["total"], 1) if mb["total"] else None,
                "ttft_ms": block(mb["ttfts"]),
                "generation_ms": block(mb["gens"]),
                "wall_ms": block(mb["walls"]),
                "tokens_per_sec": block(mb["tok_rates"]),
                "cache_hit_pct": block(mb["hits"]),
            } for mid, mb in m_buckets.items()
        }
    }
_snap_cache = {}
_snap_lock = threading.Lock()


def usage_snapshot(realm=None, ttl=10):
    """Cached wrapper: the dashboard polls this every few seconds."""
    r = realm or CURRENT_REALM
    now = time.time()
    with _snap_lock:
        hit = _snap_cache.get(r)
        if hit is not None and (now - hit[0]) < ttl:
            return hit[1]
    data = _usage_snapshot_uncached(r)
    with _snap_lock:
        _snap_cache[r] = (time.time(), data)
    return data


def _usage_snapshot_uncached(realm=None):
    r = realm or CURRENT_REALM
    rep = POOL.representative(realm=r) if POOL else current_account()
    snap = _empty_stats()
    snap["started"] = _usage.get("started", time.time())
    try:
        with open(USAGE_LOG, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if r and not row_matches_realm(row, r):
                    continue
                if row.get("error"):
                    snap["errors"] += 1
                else:
                    snap["requests"] += 1
                    for k in USAGE_FIELDS:
                        if k in row:
                            snap[k] += (row[k] or 0)
                    m = row.get("model") or "unknown"
                    per = snap["by_model"].setdefault(m, {"requests": 0, "accounts": {}, **{k: 0 for k in USAGE_FIELDS}})
                    per["requests"] += 1
                    for k in USAGE_FIELDS:
                        if k in row:
                            per[k] += (row[k] or 0)
                    acct_id = row.get("account")
                    if acct_id:
                        per.setdefault("accounts", {})
                        per["accounts"][acct_id] = per["accounts"].get(acct_id, 0) + 1
    except FileNotFoundError:
        pass
    except Exception as exc:
        log(f"usage snapshot read failed: {exc}")
    snap["since"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(snap.get("started", time.time())))
    snap["log_file"] = USAGE_LOG
    snap["realm"] = r
    snap["accounts_map"] = {a.uid: {"nickname": a.nickname, "realm": a.realm} for a in POOL.accounts} if POOL else {}
    snap["account"] = {
        "uid": (rep.uid if rep else ""),
        "domain": (rep.domain if rep else ""),
        "issuer": (wb_accounts.jwt_issuer(rep.access_token) if rep else ""),
        "credential_file": (os.path.basename(rep.path) if rep and rep.path else ""),
        "expires_at": (rep.expires_at if rep else 0),
        "accounts": (len(POOL.accounts) if POOL else 0),
        "accounts_ready": (POOL.count_ready() if POOL else 0),
    }
    return snap
def _tail_lines(path, max_lines, chunk=256 * 1024):
    """Return up to the last `max_lines` non-empty lines, oldest first.

    The usage log passes 20MB within a day. Scanning it end to end on every
    dashboard poll was the dominant cost behind slow /usage/* responses.
    """
    lines = []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            buf = b""
            while pos > 0 and len(lines) < max_lines:
                step = min(chunk, pos)
                pos -= step
                fh.seek(pos)
                buf = fh.read(step) + buf
                parts = buf.split(b"\n")
                buf = parts[0]
                for raw in reversed(parts[1:]):
                    if not raw.strip():
                        continue
                    lines.append(raw)
                    if len(lines) >= max_lines:
                        break
            if len(lines) < max_lines and buf.strip():
                lines.append(buf)
    except FileNotFoundError:
        return []
    except Exception as exc:
        log("tail read failed: %s" % exc)
        return []
    lines.reverse()
    return lines


def count_usage_rows(realm=None):
    """Cheap row count - substring match instead of a full JSON parse.

    Rows written before the `realm` field existed (they are all error rows)
    have to fall back to the account/model heuristic in row_matches_realm,
    so those few are still parsed properly.
    """
    needles = ()
    if realm:
        needles = ('"realm": "%s"' % realm, '"realm":"%s"' % realm)
    n = 0
    try:
        with open(USAGE_LOG, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                if not needles:
                    n += 1
                    continue
                if any(x in line for x in needles):
                    n += 1
                    continue
                if '"realm"' in line:
                    continue          # realm 字段存在但值不同
                try:
                    if row_matches_realm(json.loads(line), realm):
                        n += 1
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return n


def recent_usage(limit=100, realm=None, page=1):
    """Paginated rows from the tail of the log (page 1 is latest).

    Reading backward in chunks keeps this in the millisecond range while
    accurately fetching any requested page without missing rows across realms.
    """
    try:
        limit = max(1, int(limit))
    except Exception:
        limit = 100
    try:
        page = max(1, int(page))
    except Exception:
        page = 1
    total = count_usage_rows(realm)
    total_pages = max(1, (total + limit - 1) // limit) if total > 0 else 1
    page = min(page, total_pages)
    target_count = page * limit
    matching = []
    chunk = 256 * 1024
    try:
        with open(USAGE_LOG, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            buf = b""
            while pos > 0 and len(matching) < target_count:
                step = min(chunk, pos)
                pos -= step
                fh.seek(pos)
                buf = fh.read(step) + buf
                parts = buf.split(b"\n")
                buf = parts[0]
                for raw in reversed(parts[1:]):
                    st = raw.strip()
                    if not st:
                        continue
                    try:
                        item = json.loads(st.decode("utf-8", "replace"))
                    except Exception:
                        continue
                    if realm and not row_matches_realm(item, realm):
                        continue
                    matching.append(item)
                    if len(matching) >= target_count:
                        break
            if len(matching) < target_count and buf.strip():
                try:
                    item = json.loads(buf.strip().decode("utf-8", "replace"))
                    if not realm or row_matches_realm(item, realm):
                        matching.append(item)
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    except Exception as exc:
        log("recent_usage read failed: %s" % exc)
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    page_rows = matching[start_idx:end_idx]
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
        "rows": page_rows
    }
POOL = None
SCHEDULER = None
ACCOUNTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'accounts')
def realm_state_file():
    """Path of the persisted realm switch.

    Computed on every access rather than cached in a module constant: the
    constant was built from the default ACCOUNTS_DIR at import time, so a
    later --accounts-dir (or a Docker volume pointing somewhere else) still
    read and wrote the realm switch next to the script - the panel then
    reported an exit that did not match the configured account store.
    """
    return os.path.join(ACCOUNTS_DIR, "active_realm.json")


def load_persisted_realm():
    global CURRENT_REALM
    path = realm_state_file()
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
                r = d.get("realm")
                if r in ("intl", "cn"):
                    CURRENT_REALM = r
                    return CURRENT_REALM
        except Exception as e:
            log("could not load active realm: %s" % e)
    return CURRENT_REALM
def save_persisted_realm(realm):
    global CURRENT_REALM
    if realm in ("intl", "cn"):
        CURRENT_REALM = realm
        try:
            os.makedirs(ACCOUNTS_DIR, exist_ok=True)
            with open(realm_state_file(), "w", encoding="utf-8") as fh:
                json.dump({"realm": realm, "updated_at": time.time(), "updated_iso": time.strftime("%Y-%m-%d %H:%M:%S")}, fh, indent=2)
            log("persisted active realm '%s' to disk" % realm)
        except Exception as exc:
            log("failed to persist active realm: %s" % exc)
    return CURRENT_REALM
API_KEY = None
SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT
def import_desktop_accounts(realm=None):
    imported = []
    for p, r in wb_accounts.desktop_credential_candidates():
        if realm and r != realm:
            continue
        try:
            account = POOL.import_desktop_credential(path=p, realm=r)
            imported.append(account)
            log("imported %s (%s) from %s" % (account.uid[:8], account.realm, os.path.basename(p)))
        except Exception as exc:
            log("skip %s: %s" % (os.path.basename(p), exc))
    return imported
def desktop_credential_scan():
    """Read-only scan of the desktop client credentials on this machine."""
    return wb_accounts.scan_desktop_credentials()
def account_views(realm=None):
    """List view of every account, including a live readiness flag."""
    if not POOL:
        return []
    return POOL.list_public(realm=realm)


PROXY_DISCOVER_HOST = os.environ.get("WB_PROXY_DISCOVER_HOST") or "cli-proxy-mihomo"


def _proxy_port_range():
    raw = os.environ.get("WB_PROXY_DISCOVER_PORTS") or "17901-17910"
    if "-" in raw:
        lo, _, hi = raw.partition("-")
        if lo.strip().isdigit() and hi.strip().isdigit():
            return range(int(lo), int(hi) + 1)
    if raw.strip().isdigit():
        return [int(raw)]
    return range(17901, 17911)


def probe_proxy_exit(proxy_url, timeout=12):
    """Return (exit_ip, error) for one proxy URL."""
    try:
        opener = wb_accounts.opener_for_proxy(proxy_url)
        if opener is None:
            return "", "empty proxy url"
        req = urllib.request.Request("https://api.ipify.org", method="GET")
        with opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace").strip(), ""
    except Exception as exc:
        return "", str(exc)[:160]


def discover_proxy_slots():
    """Probe the configured mihomo host/ports and report reachable exits."""
    out = []
    for port in _proxy_port_range():
        url = "http://%s:%d" % (PROXY_DISCOVER_HOST, port)
        started = time.time()
        exit_ip, error = probe_proxy_exit(url)
        out.append(
            {
                "url": url,
                "reachable": not error,
                "exit_ip": exit_ip,
                "latency_ms": int((time.time() - started) * 1000),
                "error": error,
            }
        )
    return out


def proxy_slots_view():
    """Proxy slots plus how many enabled accounts are bound to each."""
    counts = {}
    if POOL:
        for account in POOL.accounts:
            slot_id = account.proxy_slot
            if slot_id and account.enabled:
                counts[slot_id] = counts.get(slot_id, 0) + 1
    out = []
    for entry in wb_settings.proxy_slots(ACCOUNTS_DIR):
        item = dict(entry)
        item["bound"] = counts.get(entry["id"], 0)
        out.append(item)
    return out


_byacct_cache = {"at": 0.0, "data": None}
_byacct_lock = threading.Lock()


def usage_by_account(ttl=10):
    """Cached wrapper: full aggregation over the whole log is expensive."""
    now = time.time()
    with _byacct_lock:
        if _byacct_cache["data"] is not None and (now - _byacct_cache["at"]) < ttl:
            return _byacct_cache["data"]
    data = _usage_by_account_uncached()
    with _byacct_lock:
        _byacct_cache["at"] = time.time()
        _byacct_cache["data"] = data
    return data


def _usage_by_account_uncached():
    """Aggregate the JSONL log per account id."""
    buckets = {}
    try:
        with open(USAGE_LOG, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("error"):
                    continue
                key = row.get("account") or "(unattributed)"
                bucket = buckets.setdefault(key, {
                    "account": key, "requests": 0, "prompt_tokens": 0,
                    "completion_tokens": 0, "reasoning_tokens": 0,
                    "cached_tokens": 0, "total_tokens": 0, "models": {},
                })
                bucket["requests"] += 1
                for field in ("prompt_tokens", "completion_tokens",
                              "reasoning_tokens", "cached_tokens", "total_tokens"):
                    bucket[field] += row.get(field) or 0
                model = row.get("model") or "?"
                bucket["models"][model] = bucket["models"].get(model, 0) + 1
    except FileNotFoundError:
        pass
    except Exception as exc:
        log("usage_by_account failed: %s" % exc)
    out = sorted(buckets.values(), key=lambda b: -b["total_tokens"])
    for item in out:
        item["models"] = sorted(item["models"].items(), key=lambda kv: -kv[1])[:5]
    return out
_analytics_cache = {"at": 0.0, "data": None}
_analytics_lock = threading.Lock()


def compute_usage_analytics(ttl=10):
    """Cached analytics payload.

    Unlike perf_stats/usage_snapshot/usage_by_account this used to run
    uncached, re-reading the whole JSONL on every call while the metrics tab
    polls it every 5 seconds. Same 10s TTL as its siblings now.
    """
    now = time.time()
    with _analytics_lock:
        hit = _analytics_cache["data"]
        if hit is not None and (now - _analytics_cache["at"]) < ttl:
            return hit
    data = _compute_usage_analytics_uncached()
    with _analytics_lock:
        _analytics_cache["at"] = time.time()
        _analytics_cache["data"] = data
    return data


def _compute_usage_analytics_uncached():
    """Detailed analytics for Token, Cache, and Reasoning metrics page."""
    now = time.localtime()
    today_ts = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))
    def new_stat():
        return {
            "requests": 0, "errors": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
            "cached_tokens": 0, "total_tokens": 0,
            "ttft_sum": 0.0, "ttft_n": 0,
            "speed_sum": 0.0, "speed_n": 0,
            "elapsed_sum": 0.0, "elapsed_n": 0,
        }
    all_summary = new_stat()
    today_summary = new_stat()
    acct_map = {}
    model_map = {}
    if os.path.exists(USAGE_LOG):
        try:
            with open(USAGE_LOG, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    is_err = bool(r.get("error"))
                    at = r.get("at", 0)
                    is_today = (at >= today_ts)
                    acct_uid = r.get("account") or "(unattributed)"
                    m_id = r.get("model") or "(unknown)"
                    def feed(stat_obj, is_error):
                        if is_error:
                            stat_obj["errors"] += 1
                        else:
                            stat_obj["requests"] += 1
                            stat_obj["prompt_tokens"] += (r.get("prompt_tokens") or 0)
                            stat_obj["completion_tokens"] += (r.get("completion_tokens") or 0)
                            stat_obj["reasoning_tokens"] += (r.get("reasoning_tokens") or 0)
                            stat_obj["cached_tokens"] += (r.get("cached_tokens") or 0)
                            stat_obj["total_tokens"] += (r.get("total_tokens") or 0)
                            if r.get("ttft_ms"):
                                stat_obj["ttft_sum"] += r["ttft_ms"]
                                stat_obj["ttft_n"] += 1
                            if r.get("tokens_per_sec"):
                                stat_obj["speed_sum"] += r["tokens_per_sec"]
                                stat_obj["speed_n"] += 1
                            if r.get("elapsed_ms"):
                                stat_obj["elapsed_sum"] += r["elapsed_ms"]
                                stat_obj["elapsed_n"] += 1
                    feed(all_summary, is_err)
                    if is_today:
                        feed(today_summary, is_err)
                    if acct_uid not in acct_map:
                        acct_map[acct_uid] = {
                            "uid": acct_uid,
                            "nickname": acct_uid,
                            "realm": r.get("realm", ""),
                            "domain": "",
                            "today": new_stat(),
                            "all_time": new_stat(),
                            "today_models": {},
                            "all_models": {},
                        }
                    feed(acct_map[acct_uid]["all_time"], is_err)
                    if is_today:
                        feed(acct_map[acct_uid]["today"], is_err)
                    if not is_err:
                        tm = acct_map[acct_uid]["all_models"].setdefault(m_id, {"requests": 0, "tokens": 0, "reasoning": 0})
                        tm["requests"] += 1
                        tm["tokens"] += (r.get("total_tokens") or 0)
                        tm["reasoning"] += (r.get("reasoning_tokens") or 0)
                        if is_today:
                            tdm = acct_map[acct_uid]["today_models"].setdefault(m_id, {"requests": 0, "tokens": 0, "reasoning": 0})
                            tdm["requests"] += 1
                            tdm["tokens"] += (r.get("total_tokens") or 0)
                            tdm["reasoning"] += (r.get("reasoning_tokens") or 0)
                    if m_id not in model_map:
                        model_map[m_id] = {"model": m_id, "today": new_stat(), "all_time": new_stat()}
                    feed(model_map[m_id]["all_time"], is_err)
                    if is_today:
                        feed(model_map[m_id]["today"], is_err)
        except Exception as exc:
            log("compute_usage_analytics failed: %s" % exc)
    if POOL:
        for a in POOL.accounts:
            if a.uid in acct_map:
                acct_map[a.uid]["nickname"] = a.nickname
                acct_map[a.uid]["realm"] = a.realm
                acct_map[a.uid]["domain"] = a.domain
                acct_map[a.uid]["credits"] = getattr(a, "credits", None) or {}
            else:
                acct_map[a.uid] = {
                    "uid": a.uid,
                    "nickname": a.nickname,
                    "realm": a.realm,
                    "domain": a.domain,
                    "credits": getattr(a, "credits", None) or {},
                    "today": new_stat(),
                    "all_time": new_stat(),
                    "today_models": {},
                    "all_models": {},
                }
    def finalize(stat_obj):
        p = stat_obj["prompt_tokens"]
        c = stat_obj["cached_tokens"]
        out = stat_obj["completion_tokens"]
        reas = stat_obj["reasoning_tokens"]
        stat_obj["cache_hit_pct"] = round((c / p * 100), 1) if p > 0 else 0.0
        stat_obj["reasoning_ratio"] = round((reas / out * 100), 1) if out > 0 else 0.0
        stat_obj["ttft_ms_avg"] = round(stat_obj["ttft_sum"] / stat_obj["ttft_n"]) if stat_obj["ttft_n"] > 0 else 0
        stat_obj["speed_avg"] = round(stat_obj["speed_sum"] / stat_obj["speed_n"], 1) if stat_obj["speed_n"] > 0 else 0.0
        stat_obj["elapsed_ms_avg"] = round(stat_obj["elapsed_sum"] / stat_obj["elapsed_n"]) if stat_obj["elapsed_n"] > 0 else 0
        return stat_obj
    finalize(all_summary)
    finalize(today_summary)
    for a in acct_map.values():
        finalize(a["today"])
        finalize(a["all_time"])
    for m in model_map.values():
        finalize(m["today"])
        finalize(m["all_time"])
    accts_list = sorted(acct_map.values(), key=lambda a: (-a["today"]["total_tokens"], -a["all_time"]["total_tokens"]))
    models_list = sorted(model_map.values(), key=lambda m: (-m["today"]["total_tokens"], -m["all_time"]["total_tokens"]))
    return {
        "today_ts": today_ts,
        "summary": {"today": today_summary, "all_time": all_summary},
        "accounts": accts_list,
        "models": models_list,
    }
def runtime_settings_view():
    """Current panel-visible settings (never returns the password or the key)."""
    key = API_KEY or ""
    if len(key) > 8:
        masked = key[:4] + "*" * 6 + key[-4:]
    else:
        masked = "*" * len(key)
    keys = []
    for entry in configured_keys():
        raw = entry.get("key") or ""
        keys.append({
            "id": entry.get("id") or "",
            "name": entry.get("name") or "",
            "realm": entry.get("realm") or "",
            "enabled": entry.get("enabled", True) is not False,
            "masked": (raw[:4] + "*" * 6 + raw[-4:]) if len(raw) > 8 else "*" * len(raw),
            "source": entry.get("source") or "panel",
            "created_at": entry.get("created_at") or "",
        })
    return {
        "panel_password_is_default": wb_settings.panel_password_is_default(ACCOUNTS_DIR),
        "api_key_set": bool(key),
        "api_key_set_by_panel": API_KEY_FILE_SET,
        "api_key_masked": masked,
        "auth_required": auth_required(),
        "api_keys": keys,
        "accounts_dir": ACCOUNTS_DIR,
        "usage_dir": USAGE_DIR,
        "settings_file": wb_settings.settings_path(ACCOUNTS_DIR),
        "version": "1.4.3",
    }
def current_account():
    """Account used for display purposes (health / usage summaries)."""
    return POOL.representative() if POOL else None
# ---------------------------------------------------------------------------
# Prefix-based session affinity (PATCHED-BY-OPS)
# ---------------------------------------------------------------------------
# 上游 prompt cache 是【账号级】的：只有同一个账号再次看到相同前缀才会命中。
# 实测证据（wk 实例 11 个号）：8 次完全相同的前缀请求被轮询分散到 8 个账号，
# 缓存率全部为 0%；而带上会话标识固定落到同一账号时，第 2 次起缓存率即 95.2%。
#
# sub2api / DSH 等客户端并不发送 X-Conversation-Id 之类的会话标识，
# 于是 hub 走纯轮询，同一对话每一轮都换账号，缓存必然归零。
#
# 这里在缺少显式会话键时，用【对话稳定前缀】派生亲和键：
# 取消息列表的前两条（system + 首条 user），它们在整段对话生命周期内不变，
# 因此同一对话的每一轮都会落到同一账号；而不同对话的首条 user 不同，
# 依旧会分散到各账号，负载均衡不受影响。
AFFINITY_BY_PREFIX = os.environ.get("WB_AFFINITY_BY_PREFIX", "1").lower() not in (
    "0", "false", "no", "off")
AFFINITY_DEBUG = os.environ.get("WB_AFFINITY_DEBUG", "0").lower() in (
    "1", "true", "yes", "on")
def derive_affinity_key(messages):
    """Derive a stable affinity key from a conversation's stable prefix.
    The first two messages (system + first user turn) stay byte-identical for
    the whole life of a conversation, so hashing them pins every later turn of
    that conversation to the same upstream account - exactly what prompt
    caching needs. Distinct conversations differ in their first user turn and
    therefore still spread across the pool.
    """
    if not AFFINITY_BY_PREFIX:
        return None
    try:
        msgs = messages or []
        if not msgs:
            return None
        head = msgs[:2]
        blob = json.dumps(head, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return "pfx-" + hashlib.sha256(blob).hexdigest()[:16]
    except Exception:
        return None
def prompt_fingerprint(messages):
    """Privacy-safe fingerprint of the outgoing prompt.
    Cache hits need a byte-identical prefix, so these hashes answer "is my
    prefix stable / is my conversation continuous?" without storing any text.
    """
    try:
        def h(obj):
            blob = json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
            return hashlib.sha256(blob).hexdigest()[:12]
        msgs = messages or []
        out = {"msgs_sha": h(msgs), "n_msgs": len(msgs)}
        if msgs:
            out["system_sha"] = h(msgs[0]) if msgs[0].get("role") == "system" else ""
            out["prefix_sha"] = h(msgs[:-1]) if len(msgs) > 1 else ""
        return out
    except Exception:
        return {}

LOG_BUFFER = deque(maxlen=2000)
_LOG_LOCK = threading.Lock()
_LOG_COUNTER = 0

def add_log_entry(msg, level=None, tag=None):
    global _LOG_COUNTER
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    t_short = time.strftime("%H:%M:%S")
    msg_str = str(msg).rstrip()
    if not level:
        lower = msg_str.lower()
        if any(k in lower for k in ("error", "exception", "failed", "11128", "11101", "11140", "traceback", "errno", "fatal")):
            level = "ERROR"
        elif any(k in lower for k in ("warn", "warning", "retry", "timeout")):
            level = "WARN"
        else:
            level = "INFO"
    if not tag:
        lower = msg_str.lower()
        if "chat:" in lower or "chat done" in lower or "/v1/chat" in lower or "/chat/completions" in lower or "responses" in lower:
            tag = "chat"
        elif "scheduler" in lower or "调度器" in lower:
            tag = "scheduler"
        elif "task" in lower or "任务" in lower or "打卡" in lower or "猫猫" in lower or "travel" in lower:
            tag = "tasks"
        elif "account" in lower or "账号" in lower or "pool" in lower or "imported" in lower:
            tag = "accounts"
        elif "model" in lower or "catalog" in lower or "模型" in lower:
            tag = "catalog"
        elif "auth" in lower or "token" in lower or "oauth" in lower:
            tag = "auth"
        elif "settings" in lower or "设置" in lower:
            tag = "settings"
        else:
            tag = "system"
    with _LOG_LOCK:
        _LOG_COUNTER += 1
        entry = {
            "id": _LOG_COUNTER,
            "ts": ts,
            "time": t_short,
            "level": level,
            "tag": tag,
            "msg": msg_str,
        }
        LOG_BUFFER.append(entry)
    return entry

def log(msg, level=None, tag=None):
    sys.stderr.write(f"[wb-proxy] {time.strftime('%H:%M:%S')} {msg}\n")
    sys.stderr.flush()
    add_log_entry(msg, level=level, tag=tag)

def get_logs(limit=200, level="", tag="", search="", since_id=0):
    with _LOG_LOCK:
        items = list(LOG_BUFFER)
    if since_id > 0:
        items = [x for x in items if x["id"] > since_id]
    if level:
        items = [x for x in items if x["level"] == level.upper()]
    if tag:
        items = [x for x in items if x["tag"].lower() == tag.lower()]
    if search:
        s = search.lower()
        items = [x for x in items if s in x["msg"].lower() or s in x["tag"].lower()]
    total = len(items)
    if limit and limit > 0 and since_id == 0:
        items = items[-limit:]
    max_id = items[-1]["id"] if items else since_id
    return {"total": total, "logs": items, "max_id": max_id}

def clear_logs():
    with _LOG_LOCK:
        LOG_BUFFER.clear()

# ---------------------------------------------------------------------------
# upstream helpers
# ---------------------------------------------------------------------------
#: Auxiliary models the API advertises but that are not usable for chat.
#: "lite" backs internal helpers (title generation, compaction) and upstream
#: rejects it with 11102; the codewise/completion entries are text-completion
#: or IDE-inline models, not chat models.
# Exclude WorkBuddy virtual aliases / quick presets
VIRTUAL_ALIAS_MODELS = {
    "default-model",
    "fast-model",
    "balanced-model",
    "primary-model",
    "deep-model",
}
NON_CHAT_MODELS = {"lite"} | VIRTUAL_ALIAS_MODELS
NON_CHAT_PREFIXES = ("codewise-", "completion-")
NON_CHAT_SUFFIXES = ("-image-alpha", "-image-alpha-edit", "-taco-completion")
def is_chat_model(mid):
    if not mid:
        return False
    if mid in NON_CHAT_MODELS:
        return False
    if mid.startswith(NON_CHAT_PREFIXES):
        return False
    if mid.endswith(NON_CHAT_SUFFIXES):
        return False
    return True
CN_UI_ORDER = [
    "hy4-preview-f",
    "hy3",
    "deepseek-v4.1-flash",
    "glm-5.3",
    "glm-5.3-flash",
    "glm-5.2",
    "glm-5.1",
    "glm-5v-turbo",
    "minimax-m3",
    "kimi-k3-1",
    "kimi-k2.8-preview",
    "kimi-k2.7",
    "kimi-k2.6",
    "deepseek-v4-pro",
]
INTL_UI_ORDER = [
    "deepseek-v4.1-flash",
    "gpt-6-astra",
    "hy4-preview-f",
    "hy4-preview",
    "hy3",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.3-codex",
    "gemini-3.5-flash",
    "glm-5.3",
    "glm-5.2",
    "kimi-k3",
    "kimi-k2.6",
]
def merge_catalog(primary, realm=None):
    r = realm or CURRENT_REALM
    merged = {}
    source_static = getattr(wb_catalog, "STATIC_CN_MODELS" if r == "cn" else "STATIC_INTL_MODELS", wb_catalog.STATIC_MODELS)
    for item in source_static:
        mid = item.get("id")
        if mid and is_chat_model(mid):
            merged[mid] = dict(item)
    for mid, meta in primary or []:
        if not is_chat_model(mid):
            continue
        if meta:
            base = merged.get(mid) or {}
            base.update(meta)
            merged[mid] = base
        elif mid not in merged:
            merged[mid] = {}
    order = CN_UI_ORDER if r == "cn" else INTL_UI_ORDER
    out = []
    for mid in order:
        if mid in merged:
            out.append((mid, merged[mid]))
    return out
def fetch_models(realm=None):
    r = realm or CURRENT_REALM
    with _lock:
        c = _models_cache.get(r) or {"at": 0.0, "data": None}
        if c["data"] and time.time() - c["at"] < 300:
            return c["data"]
    live = read_product_config_models(realm=r)
    if not live and r == "intl":
        live = [(m, {}) for m in fetch_endpoint_models()]
    entries = merge_catalog(live, realm=r)
    with _lock:
        _models_cache[r] = {"at": time.time(), "data": entries}
    return entries
def model_entry(mid, meta):
    """Build a rich /v1/models entry from the desktop app catalog metadata.
    The OpenAI spec only names id/object/created/owned_by, so capability data is
    convention-driven. Several shapes are emitted at once so that different
    clients (OpenRouter-style, LobeChat-style, plain-flag readers) all find
    what they look for.
    """
    meta = meta or {}
    item = {
        "id": mid,
        "object": "model",
        "created": int(time.time()),
        "owned_by": "workbuddy",
    }
    name = meta.get("name")
    if name:
        item["name"] = name
    desc = meta.get("descriptionEn") or meta.get("descriptionZh")
    if desc:
        item["description"] = desc
    # ---- modality / capability ----
    # disabledMultimodal explicitly turns image input off; absent means allowed.
    vision = bool(meta.get("supportsImages")) and not meta.get("disabledMultimodal")
    tools = bool(meta.get("supportsToolCall"))
    thinks = bool(meta.get("supportsReasoning"))
    inputs = ["text"] + (["image"] if vision else [])
    # Capability flags under every spelling the common clients look for.
    # /v1/models has no standard for this, so each convention is emitted at
    # once rather than guessing which one a given client reads:
    #   capabilities.vision      generic
    #   supports_vision/images   LobeChat-style flat flags
    #   vision                   Cherry Studio / NextChat style
    #   abilities.vision         LobeChat
    #   multimodal               misc
    #   *_modalities             OpenRouter
    item["capabilities"] = {
        "vision": vision,
        "tool_calls": tools,
        "reasoning": thinks,
    }
    item["supports_vision"] = vision
    item["supports_images"] = vision
    item["supports_tool_calls"] = tools
    item["supports_reasoning"] = thinks
    item["vision"] = vision
    item["multimodal"] = vision
    item["abilities"] = {
        "vision": vision,
        "functionCall": tools,
        "function_call": tools,
        "reasoning": thinks,
    }
    item["input_modalities"] = inputs
    item["output_modalities"] = ["text"]
    item["modalities"] = {"input": inputs, "output": ["text"]}
    # OpenRouter-shaped block, read by several multi-provider clients.
    item["architecture"] = {
        "input_modalities": inputs,
        "output_modalities": ["text"],
        "modality": "+".join(inputs) + "->text",
    }
    # ---- limits ----
    if meta.get("maxInputTokens"):
        item["context_length"] = meta["maxInputTokens"]
        item["max_input_tokens"] = meta["maxInputTokens"]
    if meta.get("maxOutputTokens"):
        item["max_output_tokens"] = meta["maxOutputTokens"]
        item["max_completion_tokens"] = meta["maxOutputTokens"]
    ctx = (meta.get("contextWindow") or {}).get("supportedLengths")
    if ctx:
        item["context_windows"] = ctx
    # ---- reasoning controls ----
    reasoning = meta.get("reasoning") or {}
    efforts = reasoning.get("supportedEfforts")
    if efforts:
        item["reasoning_efforts"] = efforts
    if reasoning.get("effort"):
        item["reasoning_fixed_effort"] = reasoning["effort"]
    if reasoning.get("defaultEffort"):
        item["reasoning_default_effort"] = reasoning["defaultEffort"]
    if reasoning.get("canDisableThinking") is not None:
        item["reasoning_can_disable"] = reasoning["canDisableThinking"]
    # DeepSeek 4.1 official supports low / high / max
    if mid == "deepseek-v4.1-flash":
        item["reasoning_efforts"] = ["low", "high", "max"]
        item["reasoning_default_effort"] = "high"
        item.pop("reasoning_fixed_effort", None)
    if meta.get("onlyReasoning") is not None:
        item["always_reasoning"] = bool(meta.get("onlyReasoning"))
    # ---- misc ----
    if meta.get("credits"):
        item["credits"] = meta["credits"]
    if meta.get("vendor"):
        item["vendor"] = meta["vendor"]
    if meta.get("temperature") is not None:
        item["temperature"] = meta["temperature"]
    if meta.get("top_p") is not None:
        item["top_p"] = meta["top_p"]
    if meta.get("isDefault"):
        item["is_default"] = True
    tags = [t for t in (meta.get("tags") or []) if isinstance(t, str) and not t.startswith("badge:")]
    if tags:
        item["tags"] = tags
    return item
def read_product_config_models(realm=None):
    """Read the desktop app's cached catalog: [(id, meta), ...]."""
    r = realm or CURRENT_REALM
    home = os.path.expanduser("~")
    cache_dir = ".workbuddy-ai" if r == "intl" else ".workbuddy"
    p = os.path.join(home, cache_dir, "cache", "acc-product-config-v3.json")
    try:
        with open(p, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception as exc:
        return []
    def find(node):
        if isinstance(node, dict):
            models = node.get("models")
            if isinstance(models, list) and models and isinstance(models[0], dict) and models[0].get("id"):
                return models
            for value in node.values():
                hit = find(value)
                if hit:
                    return hit
        return None
    models = find(cfg) or []
    out = []
    for m in models:
        mid = m.get("id")
        if isinstance(mid, str) and mid:
            out.append((mid, m))
    return out
def fetch_endpoint_models():
    account = POOL.pick(realm="intl") if POOL else None
    if account is None:
        log("model discovery skipped: no usable account")
        cached = _models_cache.get("intl", {}).get("data")
        return [m for m, _ in (cached or [])]
    req = urllib.request.Request(UPSTREAM + MODELS_PATH, method="GET", headers=account.headers())
    try:
        with wb_accounts.urlopen(req, timeout=30, proxy=account.proxy) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log(f"model discovery failed: {exc}")
        cached = _models_cache.get("intl", {}).get("data")
        return [m for m, _ in (cached or [])]
    ids, seen = [], set()
    for agent in (payload.get("data") or {}).get("agents") or []:
        for mid in agent.get("models") or []:
            if mid not in seen:
                seen.add(mid)
                ids.append(mid)
    return ids
def strip_data_prefix(line):
    line = line.strip()
    # SSE comment / heartbeat / keepalive / empty line
    if not line or line.startswith(":"):
        return ""
    while line.startswith("data:"):
        line = line[5:].strip()
    # Handle possible "data: : heartbeat"
    if not line or line.startswith(":"):
        return ""
    return line
def clean_chunk(raw):
    """Drop the empty noise fields the WorkBuddy gateway pads deltas with."""
    try:
        obj = json.loads(raw)
    except Exception:
        return raw
    changed = False
    for choice in obj.get("choices") or []:
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        # PATCHED-BY-OPS: 原判断 `if not delta.get("function_call")` 对
        # {"name":"","arguments":""} 为假（非空 dict 是真值），空占位删不掉。
        # 改为显式检查：name 与 arguments 均空才视为占位噪音。
        fc = delta.get("function_call")
        if fc is not None:
            fc_empty = False
            if isinstance(fc, dict):
                fc_empty = not fc.get("name")
            else:
                fc_empty = not fc
            if fc_empty:
                delta.pop("function_call", None)
                changed = True
        if isinstance(delta.get("tool_calls"), list) and not delta["tool_calls"]:
            delta.pop("tool_calls")
            changed = True
        for key in NOISE_KEYS:
            if key in delta and not delta.get(key):
                delta.pop(key)
                changed = True
        if not delta and not choice.get("finish_reason"):
            return ""
    return json.dumps(obj, ensure_ascii=False) if changed else raw
def _strip_empty_fc(obj):
    """PATCHED-BY-OPS: 递归剔除空 function_call 占位（Responses/chat 通用）。"""
    changed = False
    if isinstance(obj, dict):
        fc = obj.get("function_call")
        if isinstance(fc, dict) and not fc.get("name"):
            obj.pop("function_call", None)
            changed = True
        tc = obj.get("tool_calls")
        if isinstance(tc, list) and not tc:
            obj.pop("tool_calls", None)
            changed = True
        for v in list(obj.values()):
            if _strip_empty_fc(v):
                changed = True
    elif isinstance(obj, list):
        for v in obj:
            if _strip_empty_fc(v):
                changed = True
    return changed
def clean_responses_frame(frame):
    """PATCHED-BY-OPS: 清洗 Responses SSE 帧（bytes）。
    输入 b'event: x\ndata: {...}\n\n'；只改写 data: 行的 JSON，
    event: 行原样保留。解析失败原样返回（不破坏未知格式）。
    """
    if not frame:
        return frame
    try:
        text = frame.decode("utf-8")
    except Exception:
        return frame
    out, changed = [], False
    for line in text.splitlines():
        st = line.strip()
        if st.startswith("data:"):
            payload = st[5:].strip()
            if payload and payload != "[DONE]":
                try:
                    obj = json.loads(payload)
                    if _strip_empty_fc(obj):
                        line = "data: " + json.dumps(obj, ensure_ascii=False)
                        changed = True
                except Exception:
                    pass
        out.append(line)
    return ("\n".join(out) + "\n\n").encode("utf-8") if changed else frame
def normalize_roles(messages):
    """Map role names the upstream rejects onto ones it accepts.
    WorkBuddy only knows system / user / assistant / tool. OpenAI's newer
    "developer" role (used by the Codex CLI and current SDKs) is the same thing
    as "system", but sending it verbatim fails with code 11128.
    """
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            out.append(m)
            continue
        item = m
        if m.get("role") == "developer":
            item = dict(m)
            item["role"] = "system"
        out.append(item)
    return out
# ---------------------------------------------------------------------------
# Fingerprint Sanitization (immunizes against Codex / Claude Code WAF patterns)
# ---------------------------------------------------------------------------
SANITIZE_FEATURES = (
    "x-anthropic-billing-header",
    "cc_entrypoint=",
    "You are Claude Code",
    "Main branch (",
    "You are a coding agent running in the Codex CLI",
    "github.com/anthropics/",
    "11128",
)
SANITIZE_REWRITES = (
    ("You are Claude Code, Anthropic's official CLI for Claude",
     "You are Claude Code, Anthropic's official CLI tool for Claude"),
    ("Main branch (you will usually use this for PRs)",
     "Default branch (you will usually use this for PRs)"),
    ("You are a coding agent running in the Codex CLI, a terminal-based coding assistant.",
     "You are a coding agent running in the Codex CLI tool, a terminal-based coding assistant."),
    ("To give feedback, users should report the issue at https://github.com/anthropics/claude-code/issues",
     "To provide feedback, users should report the issue at https://github.com/anthropics/claude-code/issues"),
    ("11128", "11-128"),
)
SANITIZE_HDR_RE = re.compile(r"(?i)x-anthropic-billing-header:[^;\r\n]*;?\s*")
SANITIZE_BARE_HDR_RE = re.compile(r"(?i)x-anthropic-billing-header")
SANITIZE_KV_RE = re.compile(r"(?i)\bcc_[a-z0-9_]+=[^;\r\n]*;?\s*")
def has_fingerprint(text):
    if not isinstance(text, str) or not text:
        return False
    for f in SANITIZE_FEATURES:
        if f in text:
            return True
    return bool(SANITIZE_BARE_HDR_RE.search(text))
def sanitize_text(text):
    if not isinstance(text, str) or not text:
        return text
    if not has_fingerprint(text):
        return text
    for old, new in SANITIZE_REWRITES:
        text = text.replace(old, new)
    text = SANITIZE_HDR_RE.sub("", text)
    if "cc_" in text:
        prev = ""
        while prev != text:
            prev = text
            text = SANITIZE_KV_RE.sub("", text)
    text = SANITIZE_BARE_HDR_RE.sub("x-anthropic-billing-hdr", text)
    return text.strip()
def sanitize_content(content):
    if isinstance(content, str):
        return sanitize_text(content)
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and "text" in part:
                p = dict(part)
                p["text"] = sanitize_text(p["text"])
                out.append(p)
            else:
                out.append(part)
        return out
    return content
def sanitize_tool_calls(tool_calls):
    if not isinstance(tool_calls, list):
        return tool_calls
    out = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            item = dict(tc)
            fn = item.get("function")
            if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                fn = dict(fn)
                fn["arguments"] = sanitize_text(fn["arguments"])
                item["function"] = fn
            out.append(item)
        else:
            out.append(tc)
    return out
def sanitize_messages(messages):
    out = []
    for m in messages or []:
        if isinstance(m, dict):
            item = dict(m)
            if "content" in item:
                item["content"] = sanitize_content(item["content"])
            if isinstance(item.get("reasoning_content"), str):
                item["reasoning_content"] = sanitize_text(item["reasoning_content"])
            if "tool_calls" in item:
                item["tool_calls"] = sanitize_tool_calls(item["tool_calls"])
            out.append(item)
        else:
            out.append(m)
    return out
# ---------------------------------------------------------------------------
# DeepSeek Multi-turn Consistency: reasoning_content backfill
# ---------------------------------------------------------------------------
def backfill_reasoning_content(messages, model):
    if not model or not str(model).lower().startswith("deepseek"):
        return messages
    has_trace = False
    for m in messages:
        if isinstance(m, dict):
            if m.get("reasoning") or "reasoning_content" in m:
                has_trace = True
                break
    if not has_trace:
        return messages
    out = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant":
            item = dict(m)
            if "reasoning_content" not in item:
                if item.get("reasoning"):
                    item["reasoning_content"] = str(item["reasoning"])
                else:
                    item["reasoning_content"] = ""
            out.append(item)
        else:
            out.append(m)
    return out
# ---------------------------------------------------------------------------
# Tool & Tool Choice Normalization (avoids code 11101 on object tool_choice)
# ---------------------------------------------------------------------------
def normalize_tool_choice(obj):
    if "tool_choice" not in obj:
        return
    tc = obj["tool_choice"]
    if isinstance(tc, str):
        val = tc.strip().lower()
        if val == "none":
            obj.pop("tool_choice", None)
            obj.pop("tools", None)
            obj.pop("functions", None)
        return
    if isinstance(tc, dict):
        typ = (tc.get("type") or "").strip().lower()
        if typ == "none":
            obj.pop("tool_choice", None)
            obj.pop("tools", None)
            obj.pop("functions", None)
        elif typ in ("auto", "required"):
            obj["tool_choice"] = typ
        elif typ == "function":
            name = (tc.get("function") or {}).get("name") or tc.get("name") or ""
            obj["tool_choice"] = name.strip() or "auto"
        else:
            obj.pop("tool_choice", None)
    else:
        obj.pop("tool_choice", None)
def normalize_tools(obj):
    tools = obj.get("tools")
    if not tools or not isinstance(tools, list):
        return
    norm = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        # Wrap top-level name tool definition into Chat Completions function schema
        if "name" in t and "function" not in t and t.get("type") == "function":
            fn = {
                "name": t.get("name") or "",
                "description": t.get("description") or "",
                "parameters": t.get("parameters") or {},
            }
            if "strict" in t:
                fn["strict"] = t["strict"]
            norm.append({"type": "function", "function": fn})
        else:
            norm.append(t)
    obj["tools"] = norm
# ---------------------------------------------------------------------------
# DeepSeek DSML Tool Calls Fallback Parser
# ---------------------------------------------------------------------------
TAG_START = r"<[^>]*DSML[^>]*"
DSML_CALLS_RE = re.compile(TAG_START + r"calls>(.*?)</[^>]*DSML[^>]*calls>", re.DOTALL)
DSML_INVOKE_RE = re.compile(TAG_START + r"invoke\s+name=[\x22\x27]([^\x22\x27]+)[\x22\x27]>(.*?)</[^>]*invoke>", re.DOTALL)
DSML_PARAM_RE = re.compile(TAG_START + r"parameter\s+name=[\x22\x27]([^\x22\x27]+)[\x22\x27][^>]*>(.*?)</[^>]*parameter>", re.DOTALL)
def parse_dsml_tool_calls(text):
    if not text or "DSML" not in text:
        return None, text
    match = DSML_CALLS_RE.search(text)
    if not match:
        return None, text
    calls_block = match.group(1)
    tool_calls = []
    for inv_match in DSML_INVOKE_RE.finditer(calls_block):
        func_name = inv_match.group(1)
        params_block = inv_match.group(2)
        params = {}
        for p_match in DSML_PARAM_RE.finditer(params_block):
            p_name = p_match.group(1)
            p_val = p_match.group(2).strip()
            params[p_name] = p_val
        tool_calls.append({
            "id": _new_id("call_"),
            "name": func_name,
            "arguments": json.dumps(params, ensure_ascii=False),
        })
    clean = (text[:match.start()].strip() + " " + text[match.end():].strip()).strip()
    return tool_calls, clean
def translate_max_completion_tokens(obj):
    alias = obj.pop("max_completion_tokens", None)
    if alias is None:
        return
    if "max_tokens" in obj:
        return
    try:
        val = int(alias)
        if val > 0:
            obj["max_tokens"] = val
    except (TypeError, ValueError):
        pass
def build_upstream_body(payload):
    model = payload.get("model") or ""
    messages = normalize_roles(payload.get("messages") or [])
    messages = sanitize_messages(messages)
    messages = backfill_reasoning_content(messages, model)
    if not messages or (messages[0].get("role") != "system"):
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
    body = dict(payload)
    body["messages"] = messages
    translate_max_completion_tokens(body)
    normalize_tool_choice(body)
    normalize_tools(body)
    # Thinking injection for DeepSeek models
    if str(model).lower().startswith("deepseek"):
        if "thinking" not in body and body.get("reasoning_effort") != "none":
            body["thinking"] = {"type": "enabled"}
    body["stream"] = True
    if "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}
    return body
class RateLimited(Exception):
    """Upstream throttled this model (429 / code 6004). Distinct from a dead
    pool: the credential is fine, only the model is cooling down for a while."""

    def __init__(self, http_error=None, detail="", wait=60):
        self.http_error = http_error
        self.detail = detail or ""
        self.wait = max(1, int(wait or 60))
        super().__init__("upstream rate limit: %s" % (self.detail[:200] or "429"))


def retry_after_seconds(model, realm):
    """Shortest wait until any account of this realm can serve `model` again."""
    if not POOL:
        return 60
    waits = [a.throttle_wait(model=model) for a in POOL.accounts
             if a.realm == realm and a.enabled and a.access_token]
    active = [w for w in waits if w > 0]
    return int(min(active)) if active else 60


def realm_model_throttled(realm, model):
    """True when accounts exist and are healthy but all are cooling this model."""
    if not POOL:
        return (False, 0)
    existing = [a for a in POOL.accounts
                if a.realm == realm and a.enabled and a.access_token]
    if not existing:
        return (False, 0)
    waits = [a.throttle_wait(model=model) for a in existing]
    if waits and all(w > 0 for w in waits):
        return (True, int(min(waits)))
    return (False, 0)


def parse_rate_limit_reset(detail):
    """Pull the reset time out of an upstream 429 body, if it names one.

    Upstream answers code 6004 with "... your usage will reset at
    2026-09-19 18:29:03 UTC+8 ...". Returns an epoch or None. Kept tolerant on
    purpose: an unparseable body must not break the request path.
    """
    if not detail:
        return None
    m = re.search(r"reset at\s+(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", detail)
    if not m:
        return None
    stamp = m.group(1).replace("T", " ")
    tz = re.search(r"UTC([+-]\d{1,2})(?::?(\d{2}))?", detail)
    offset = 0
    if tz:
        hours = int(tz.group(1))
        minutes = int(tz.group(2) or 0)
        offset = hours * 3600 + (minutes * 60 if hours >= 0 else -minutes * 60)
    try:
        base = time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S")) - time.timezone
        return base - offset
    except Exception:
        return None


def open_upstream(payload, session_key=None, target_realm=None):
    realm = target_realm or detect_model_realm(payload.get("model")) or CURRENT_REALM
    model = str(payload.get("model") or "")
    upstream_body = build_upstream_body(payload)
    body = json.dumps(upstream_body, ensure_ascii=False).encode("utf-8")
    # PATCHED-BY-OPS: 客户端未提供会话标识时，用对话稳定前缀兜底。
    # 位置放在 build_upstream_body 之后，保证键与真正发往上游的消息一致
    # （该函数可能在最前面插入 SYSTEM_PROMPT）。
    if not session_key:
        session_key = derive_affinity_key(upstream_body.get("messages"))
        if session_key and AFFINITY_DEBUG:
            log("affinity: derived %s for %d msgs"
                % (session_key, len(upstream_body.get("messages") or [])))
    total = max(1, POOL.count_ready(realm, model=model)) if POOL else 1
    tried = set()
    last_error = None
    last_uid = None
    last_429 = None
    last_429_detail = ""
    for _ in range(total):
        account = POOL.pick_for_session(realm=realm, session_key=session_key,
                                        exclude=tried, model=model) if POOL else None
        if account is None:
            break
        if account.realm != realm:
            if session_key and POOL: POOL.affinity.unbind(session_key)
            continue
        tried.add(account.uid)
        last_uid = account.uid
        cfg = wb_accounts.get_realm_config(account.realm)
        chat_url = cfg["chat_upstream"] + CHAT_PATH
        req = urllib.request.Request(chat_url, data=body, method="POST",
                                     headers=account.headers(purpose="chat"))
        try:
            resp = wb_accounts.urlopen(req, timeout=600, proxy=account.proxy)
            account.clear_error(model=model)
            return resp, account
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                try:
                    detail = exc.read(600).decode("utf-8", "replace")
                except Exception:
                    detail = ""
                reset_at = parse_rate_limit_reset(detail)
                wait = max(1.0, reset_at - time.time()) if reset_at else 60.0
                # Model-scoped: only this model is throttled for this account,
                # so sibling models stay serviceable on the same credential.
                account.note_error("HTTP 429 (model throttled)", model=model, until=reset_at,
                                   cooldown=wait)
                log("account %s throttled on '%s' (429), retry in %ds"
                    % (account.uid[:8], model, int(wait)))
                if session_key and POOL:
                    POOL.affinity.unbind(session_key)
                last_error = exc
                last_429 = exc
                last_429_detail = detail
                continue
            if exc.code in (401, 403):
                log("account %s rejected (HTTP %s), rotating" % (account.uid[:8], exc.code))
                if session_key and POOL:
                    POOL.affinity.unbind(session_key)
                account.note_error("HTTP %s" % exc.code,
                                   cooldown=60,
                                   single_account=(total <= 1))
                last_error = exc
                continue
            raise
        except Exception as exc:
            if session_key and POOL:
                POOL.affinity.unbind(session_key)
            account.note_error(str(exc)[:120], cooldown=60, single_account=(total <= 1))
            last_error = exc
            continue
    if last_error is not None:
        # Carry the account that produced the failure out to the caller, so
        # the error row can name it even though the local variable that would
        # have held it was never assigned in the caller.
        try:
            last_error.account_uid = last_uid
        except Exception:
            pass
        if last_429 is not None:
            exc = RateLimited(last_429, last_429_detail,
                              wait=retry_after_seconds(model, realm))
            exc.account_uid = last_uid
            raise exc
        raise last_error
    throttled, wait = realm_model_throttled(realm, model)
    if throttled:
        raise RateLimited(None, "usage exceeds frequency limit", wait=wait)
    raise RuntimeError(f"no usable account for realm '{realm}': all are disabled, cooling down, or expired")
def extract_session_key(headers, payload):
    key = (
        headers.get("X-Conversation-Id") or
        headers.get("Conversation-Id") or
        headers.get("X-Session-Id") or
        headers.get("Session-Id") or
        payload.get("conversation_id") or
        payload.get("session_id") or
        (payload.get("metadata") or {}).get("conversation_id")
    )
    if key:
        return str(key).strip()
    return None
def aggregate_stream(raw_iter, model, resp_id):
    """Fold an SSE stream into one non-streaming chat.completion object."""
    content, reasoning, finish = [], [], "stop"
    tool_calls_map = {}
    usage = None
    started = time.time()
    first_chunk_at = None
    for line in raw_iter:
        data = strip_data_prefix(line.decode("utf-8", "replace"))
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except Exception:
            continue
        if first_chunk_at is None:
            first_chunk_at = time.time()
        if chunk.get("id"):
            resp_id = chunk["id"]
        if chunk.get("model"):
            model = chunk["model"]
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning.append(delta["reasoning_content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index")
                if idx is None:
                    idx = len(tool_calls_map)
                fn = tc.get("function") or {}
                call_id = tc.get("id")
                fn_name = fn.get("name") or ""
                fn_args = fn.get("arguments") or ""
                if idx not in tool_calls_map:
                    tool_calls_map[idx] = {
                        "id": call_id or _new_id("call_"),
                        "type": tc.get("type") or "function",
                        "function": {
                            "name": fn_name,
                            "arguments": fn_args,
                        }
                    }
                else:
                    entry = tool_calls_map[idx]
                    if call_id:
                        entry["id"] = call_id
                    if fn_name:
                        entry["function"]["name"] = (entry["function"]["name"] or "") + fn_name
                    if fn_args:
                        entry["function"]["arguments"] = (entry["function"]["arguments"] or "") + fn_args
            fc = delta.get("function_call")
            # PATCH2-BY-OPS: 上游会在流末尾发 function_call:{"name":"","arguments":""}
            # 占位。原判断对空 dict 成立，会凭空生成 tool_call 并伪造 id，
            # 导致 finish_reason 被改成 "tool_calls"（参数全空）→ 严格客户端死等。
            # 故：只要 name 为空即视为无效占位直接跳过。
            fc_is_empty = (not isinstance(fc, dict)) or (not fc.get("name"))
            if fc and isinstance(fc, dict) and not fc_is_empty:
                idx = 0
                if idx not in tool_calls_map:
                    tool_calls_map[idx] = {
                        "id": _new_id("call_"),
                        "type": "function",
                        "function": {
                            "name": fc.get("name") or "",
                            "arguments": fc.get("arguments") or "",
                        }
                    }
                else:
                    entry = tool_calls_map[idx]
                    if fc.get("name") and not entry["function"]["name"]:
                        entry["function"]["name"] = fc["name"]
                    if fc.get("arguments"):
                        entry["function"]["arguments"] += fc["arguments"]
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    message = {"role": "assistant", "content": "".join(content)}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    # PATCH2-BY-OPS: 二次防御——剔除「无函数名且无参数」的空 tool_call。
    # 即使上游以 tool_calls 数组形式发空占位，也不会泄漏给客户端。
    if tool_calls_map:
        tool_calls_map = {
            k: v for k, v in tool_calls_map.items()
            if (v.get("function") or {}).get("name")
        }
    if tool_calls_map:
        ordered_tcs = [tool_calls_map[k] for k in sorted(tool_calls_map.keys())]
        message["tool_calls"] = ordered_tcs
        if finish in ("stop", None):
            finish = "tool_calls"
    elif finish == "tool_calls":
        # 占位被全部过滤掉，无实际工具调用，降级为正常结束，防止客户端无限挂起等待
        finish = "stop"
    out = {
        "id": resp_id or "chatcmpl-wb",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }
    if usage:
        out["usage"] = usage
    out["elapsed_ms"] = int((time.time() - started) * 1000)
    out["first_chunk_at"] = first_chunk_at
    return out
# ---------------------------------------------------------------------------
# Responses API (/v1/responses) <-> Chat Completions translation
# ---------------------------------------------------------------------------
#
# Kelivo and other clients can speak OpenAI's newer Responses API. The upstream
# gateway only speaks Chat Completions, so those requests are translated down,
# and the reply is translated back up into Responses objects / SSE events.
def local_ip_addresses():
    """Every non-loopback IPv4 address this machine answers on."""
    found = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found and not ip.startswith("127."):
                found.append(ip)
    except Exception:
        pass
    if not found:
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("8.8.8.8", 80))
            found.append(probe.getsockname()[0])
            probe.close()
        except Exception:
            pass
    return found
def _new_id(prefix):
    return prefix + uuid.uuid4().hex
def _flatten_content(content):
    """Flatten Responses-style content into text, or OpenAI vision parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    texts, parts = [], []
    for piece in content:
        if isinstance(piece, str):
            texts.append(piece)
            parts.append({"type": "text", "text": piece})
            continue
        if not isinstance(piece, dict):
            continue
        ptype = piece.get("type") or ""
        if ptype in ("input_text", "output_text", "text", "summary_text"):
            t = piece.get("text") or ""
            texts.append(t)
            parts.append({"type": "text", "text": t})
        elif ptype in ("input_image", "image_url", "image") or "image_url" in piece:
            url = piece.get("image_url") or piece.get("url")
            if isinstance(url, dict):
                url = url.get("url")
            if not url and piece.get("data"):
                mime = piece.get("mimeType") or piece.get("mime_type") or "image/png"
                url = f"data:{mime};base64," + piece["data"]
            if url:
                parts.append({"type": "image_url", "image_url": {"url": url}})
    if any(p.get("type") == "image_url" for p in parts):
        return parts          # multimodal: keep structured parts
    return chr(10).join(t for t in texts if t)


# ---------------------------------------------------------------------------
# Responses API "custom" (freeform) tools
#
# Some clients - most notably Codex 0.15x - declare their file-editing tool as a
# *custom* (freeform) tool rather than a JSON-schema function:
#
#     {"type": "custom", "name": "apply_patch", "format": {...grammar...}}
#
# and expect the model to answer with a custom_tool_call item carrying the raw
# payload in "input", then feed the result back as custom_tool_call_output.
#
# The upstream chat endpoint has no notion of custom tools, so we downgrade them
# to ordinary function tools with a single "input" string parameter on the way
# out, and re-inflate them to custom_tool_call on the way back. Without this the
# tool is silently ignored: the model emits the payload as ordinary prose and the
# client never sees a tool call (measured: 52 text deltas, 0 tool items).
# ---------------------------------------------------------------------------

CUSTOM_TOOL_HINT = (
    "This is a freeform tool. Put the COMPLETE raw payload into the single "
    "'input' string parameter, verbatim. Do not wrap it in JSON, do not wrap "
    "it in markdown code fences, do not add commentary."
)


def _is_custom_tool(tool):
    return isinstance(tool, dict) and str(tool.get("type") or "").lower() == "custom"


def custom_tool_names(tools):
    """Names of tools declared as freeform/custom in a Responses request."""
    names = set()
    for t in tools or []:
        if _is_custom_tool(t) and t.get("name"):
            names.add(str(t["name"]))
    return names


def _downgrade_custom_tool(tool):
    """Rewrite a Responses custom tool into a Chat function tool."""
    desc = tool.get("description") or ""
    fmt = tool.get("format") or {}
    extra = ""
    if isinstance(fmt, dict) and fmt.get("definition"):
        extra = chr(10) + chr(10) + "Grammar:" + chr(10) + str(fmt["definition"])
    return {
        "type": "function",
        "name": tool.get("name") or "",
        "description": (desc + chr(10) + chr(10) + CUSTOM_TOOL_HINT + extra).strip(),
        "parameters": {
            "type": "object",
            "properties": {
                "input": {
                    "type": "string",
                    "description": "Complete raw payload for this tool, verbatim.",
                }
            },
            "required": ["input"],
        },
    }


def _tools_for_chat(tools):
    """Downgrade custom tools; leave everything else untouched."""
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        out.append(_downgrade_custom_tool(t) if _is_custom_tool(t) else t)
    return out


def _unwrap_custom_input(args):
    """Pull the freeform string back out of an {"input": "..."} argument blob."""
    if not isinstance(args, str):
        return json.dumps(args or "", ensure_ascii=False)
    try:
        parsed = json.loads(args)
    except Exception:
        return args
    if isinstance(parsed, dict):
        val = parsed.get("input")
        if isinstance(val, str):
            return val
        if val is not None:
            return json.dumps(val, ensure_ascii=False)
    if isinstance(parsed, str):
        return parsed
    return args

def responses_to_chat(payload):
    """Translate a Responses API request body into a Chat Completions body."""
    messages = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    inp = payload.get("input")
    pending_reasoning = ""
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype in (None, "message"):
                body = _flatten_content(item.get("content"))
                if body:
                    role = item.get("role") or "user"
                    if role == "developer":
                        role = "system"
                    # If this is assistant text and the previous message is an assistant
                    # message (e.g. from an adjacent function_call), merge them so
                    # tool_calls and text stay in one message without breaking tool sequence.
                    if role == "assistant" and messages and messages[-1].get("role") == "assistant":
                        prev = messages[-1]
                        if prev.get("content"):
                            prev["content"] = str(prev["content"]) + chr(10) + str(body)
                        else:
                            prev["content"] = body
                        if pending_reasoning and "reasoning_content" not in prev:
                            prev["reasoning_content"] = pending_reasoning
                            pending_reasoning = ""
                    else:
                        msg_dict = {"role": role, "content": body}
                        if role == "assistant" and pending_reasoning:
                            msg_dict["reasoning_content"] = pending_reasoning
                            pending_reasoning = ""
                        messages.append(msg_dict)
            elif itype == "reasoning":
                # Reasoning item from previous assistant turn in Responses API.
                # In standard Chat Completions, reasoning is either backfilled into
                # the assistant message's reasoning_content or omitted.
                r_text = ""
                summ = item.get("summary")
                if isinstance(summ, list):
                    r_text = chr(10).join(
                        p.get("text", "") for p in summ if isinstance(p, dict) and p.get("text")
                    )
                elif isinstance(summ, str):
                    r_text = summ
                if not r_text:
                    cnt = item.get("content")
                    if isinstance(cnt, str):
                        r_text = cnt
                    elif isinstance(cnt, list):
                        r_text = _flatten_content(cnt)
                if r_text:
                    if messages and messages[-1].get("role") == "assistant":
                        messages[-1]["reasoning_content"] = r_text
                    else:
                        pending_reasoning = r_text
            elif itype == "function_call_output":
                raw_out = item.get("output")
                if isinstance(raw_out, list):
                    content = _flatten_content(raw_out)
                elif isinstance(raw_out, dict):
                    if raw_out.get("type") in ("input_image", "image_url", "image") or "image_url" in raw_out:
                        content = _flatten_content([raw_out])
                    else:
                        content = json.dumps(raw_out, ensure_ascii=False)
                elif isinstance(raw_out, str):
                    content = raw_out
                else:
                    content = str(raw_out)
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or "",
                    "content": content,
                })
            elif itype == "function_call":
                tc_item = {
                    "id": item.get("call_id") or item.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "{}",
                    },
                }
                # Merge into previous assistant message if adjacent
                if messages and messages[-1].get("role") == "assistant":
                    prev = messages[-1]
                    if "tool_calls" in prev:
                        prev["tool_calls"].append(tc_item)
                    else:
                        prev["tool_calls"] = [tc_item]
                    if pending_reasoning and "reasoning_content" not in prev:
                        prev["reasoning_content"] = pending_reasoning
                        pending_reasoning = ""
                else:
                    msg_dict = {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [tc_item],
                    }
                    if pending_reasoning:
                        msg_dict["reasoning_content"] = pending_reasoning
                        pending_reasoning = ""
                    messages.append(msg_dict)
            elif itype == "custom_tool_call":
                # Freeform tool call coming back as conversation history.
                raw_input = item.get("input")
                if isinstance(raw_input, (dict, list)):
                    raw_input = json.dumps(raw_input, ensure_ascii=False)
                if not isinstance(raw_input, str):
                    raw_input = "" if raw_input is None else str(raw_input)
                tc_item = {
                    "id": item.get("call_id") or item.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": json.dumps({"input": raw_input}, ensure_ascii=False),
                    },
                }
                # Merge into previous assistant message if adjacent
                if messages and messages[-1].get("role") == "assistant":
                    prev = messages[-1]
                    if "tool_calls" in prev:
                        prev["tool_calls"].append(tc_item)
                    else:
                        prev["tool_calls"] = [tc_item]
                    if pending_reasoning and "reasoning_content" not in prev:
                        prev["reasoning_content"] = pending_reasoning
                        pending_reasoning = ""
                else:
                    msg_dict = {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [tc_item],
                    }
                    if pending_reasoning:
                        msg_dict["reasoning_content"] = pending_reasoning
                        pending_reasoning = ""
                    messages.append(msg_dict)
            elif itype == "custom_tool_call_output":
                # Result of a freeform tool call (e.g. apply_patch output).
                raw_out = item.get("output")
                if isinstance(raw_out, list):
                    content = _flatten_content(raw_out)
                elif isinstance(raw_out, dict):
                    content = json.dumps(raw_out, ensure_ascii=False)
                elif isinstance(raw_out, str):
                    content = raw_out
                else:
                    content = str(raw_out)
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or "",
                    "content": content,
                })
            else:
                # Never silently drop an unknown item: a dropped tool call or
                # tool result leaves the transcript inconsistent upstream.
                log("responses: WARNING unhandled input item type=%r keys=%s"
                    % (itype, sorted(item.keys())[:8]))
    chat = {"model": payload.get("model"), "messages": messages}
    for key in ("temperature", "top_p", "seed"):
        if payload.get(key) is not None:
            chat[key] = payload[key]
    if payload.get("max_output_tokens") is not None:
        chat["max_tokens"] = payload["max_output_tokens"]
    effort = None
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
    if not effort:
        effort = payload.get("reasoning_effort")
    if effort:
        chat["reasoning_effort"] = effort
    if payload.get("tools"):
        chat["tools"] = _tools_for_chat(payload["tools"])
    if payload.get("tool_choice"):
        chat["tool_choice"] = payload["tool_choice"]
    if payload.get("parallel_tool_calls") is not None:
        chat["parallel_tool_calls"] = payload["parallel_tool_calls"]
    return chat
def _responses_usage(u):
    if not u:
        return None
    det = u.get("completion_tokens_details") or {}
    pdet = u.get("prompt_tokens_details") or {}
    return {
        "input_tokens": u.get("prompt_tokens") or 0,
        "input_tokens_details": {
            "cached_tokens": u.get("prompt_cache_hit_tokens")
            or det.get("cached_tokens") or pdet.get("cached_tokens") or 0,
        },
        "output_tokens": u.get("completion_tokens") or 0,
        "output_tokens_details": {"reasoning_tokens": det.get("reasoning_tokens") or 0},
        "total_tokens": u.get("total_tokens") or 0,
    }
def chat_to_response(chat_obj, model, custom_names=None):
    """Fold a Chat Completions object into a Responses API response object.

    custom_names is the set of tool names the client declared as freeform
    ("custom"). Calls to those tools are re-inflated into custom_tool_call
    items so clients such as Codex recognise them.
    """
    custom_names = custom_names or set()
    choice = (chat_obj.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    text = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or ""
    output = []
    if reasoning:
        output.append({
            "id": _new_id("rs_"),
            "type": "reasoning",
            "status": "completed",
            "summary": [{"type": "summary_text", "text": reasoning}],
        })
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        call_id = tc.get("id") or _new_id("call_")
        name = fn.get("name") or ""
        if name and name in custom_names:
            output.append({
                "id": _new_id("ctc_"),
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": call_id,
                "name": name,
                "input": _unwrap_custom_input(fn.get("arguments") or ""),
            })
        else:
            output.append({
                "id": _new_id("fc_"),
                "type": "function_call",
                "status": "completed",
                "call_id": call_id,
                "name": name,
                "arguments": fn.get("arguments") or "{}",
            })
    # DeepSeek DSML tool calls fallback
    if not (msg.get("tool_calls")):
        dsml_calls, clean_t = parse_dsml_tool_calls(text)
        if dsml_calls:
            for dc in dsml_calls:
                output.append({
                    "id": _new_id("fc_"),
                    "type": "function_call",
                    "status": "completed",
                    "call_id": dc.get("id") or _new_id("call_"),
                    "name": dc.get("name") or "",
                    "arguments": dc.get("arguments") or "{}",
                })
            text = clean_t
    if text or not output:
        output.append({
            "id": _new_id("msg_"),
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}] if text else [],
        })
    finish = choice.get("finish_reason") or "stop"
    obj = {
        "id": _new_id("resp_"),
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed" if finish != "length" else "incomplete",
        "model": model,
        "output": output,
        "output_text": text,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "metadata": {},
    }
    u = _responses_usage(chat_obj.get("usage"))
    if u:
        obj["usage"] = u
    if finish == "length":
        obj["incomplete_details"] = {"reason": "max_output_tokens"}
    return obj
def stream_responses_events(upstream, model, holder):
    """Yield Responses-API SSE frames translated from chat-completions chunks."""
    resp_id, msg_id, rs_id = _new_id("resp_"), _new_id("msg_"), _new_id("rs_")
    created = int(time.time())
    seq = 0
    text_parts, reason_parts = [], []
    outputs = []
    reason_index = None
    msg_index = None
    finish = "stop"
    usage = None
    tool_calls_map = {}
    text_buffer = ""
    dsml_tool_calls = []
    custom_names = set(holder.get("custom_names") or ())
    def resp_obj(status):
        obj = {
            "id": resp_id,
            "object": "response",
            "created_at": created,
            "status": status,
            "model": model,
            "output": [o for o in outputs if o],
            "output_text": "".join(text_parts),
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "metadata": {},
        }
        u = _responses_usage(usage)
        if u:
            obj["usage"] = u
        return obj
    def ev(etype, payload):
        nonlocal seq
        seq += 1
        data = {"type": etype, "sequence_number": seq}
        data.update(payload)
        body = json.dumps(data, ensure_ascii=False)
        return ("event: " + etype + chr(10) + "data: " + body + chr(10) + chr(10)).encode("utf-8")
    def reason_item(status):
        return {
            "id": rs_id,
            "type": "reasoning",
            "status": status,
            "summary": [{"type": "summary_text", "text": "".join(reason_parts)}],
        }
    def msg_item(status):
        item = {"id": msg_id, "type": "message", "status": status,
                "role": "assistant", "content": []}
        if text_parts:
            item["content"] = [{"type": "output_text", "text": "".join(text_parts),
                              "annotations": []}]
        return item
    yield ev("response.created", {"response": resp_obj("in_progress")})
    yield ev("response.in_progress", {"response": resp_obj("in_progress")})
    for raw in upstream:
        data = strip_data_prefix(raw.decode("utf-8", "replace"))
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except Exception:
            continue
        if usage is None and chunk.get("usage"):
            usage = chunk["usage"]
            holder["usage"] = usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            piece = delta.get("reasoning_content")
            if piece:
                if reason_index is None:
                    reason_index = len(outputs)
                    outputs.append(None)
                    yield ev("response.output_item.added",
                             {"output_index": reason_index, "item": reason_item("in_progress")})
                    yield ev("response.reasoning_summary_part.added", {
                        "item_id": rs_id, "output_index": reason_index, "summary_index": 0,
                        "part": {"type": "summary_text", "text": ""},
                    })
                reason_parts.append(piece)
                yield ev("response.reasoning_summary_text.delta", {
                    "item_id": rs_id, "output_index": reason_index,
                    "summary_index": 0, "delta": piece,
                })
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                fn = tc.get("function") or {}
                fn_name = fn.get("name") or ""
                fn_args = fn.get("arguments") or ""
                call_id = tc.get("id") or ""
                if idx not in tool_calls_map:
                    out_idx = len(outputs)
                    outputs.append(None)
                    c_id = call_id or _new_id("call_")
                    is_custom = bool(fn_name) and fn_name in custom_names
                    entry = {
                        "output_index": out_idx,
                        "id": c_id,
                        "name": fn_name,
                        "arguments": fn_args,
                        "custom": is_custom,
                        "item_id": _new_id("ctc_" if is_custom else "fc_"),
                    }
                    tool_calls_map[idx] = entry
                    item = {
                        "id": entry["item_id"],
                        "status": "in_progress",
                        "call_id": c_id,
                        "name": fn_name,
                    }
                    if is_custom:
                        item["type"] = "custom_tool_call"
                        item["input"] = ""
                    else:
                        item["type"] = "function_call"
                        item["arguments"] = ""
                    yield ev("response.output_item.added", {
                        "output_index": out_idx,
                        "item": item,
                    })
                else:
                    entry = tool_calls_map[idx]
                    if fn_name and not entry["name"]:
                        entry["name"] = fn_name
                        if fn_name in custom_names:
                            entry["custom"] = True
                    if fn_args:
                        entry["arguments"] += fn_args
                        if entry.get("custom"):
                            yield ev("response.custom_tool_call_input.delta", {
                                "output_index": entry["output_index"],
                                "item_id": entry["item_id"],
                                "call_id": entry["id"],
                                "delta": fn_args,
                            })
                        else:
                            yield ev("response.function_call_arguments.delta", {
                                "output_index": entry["output_index"],
                                "call_id": entry["id"],
                                "delta": fn_args,
                            })
            piece = delta.get("content")
            if piece:
                if msg_index is None:
                    if reason_index is not None:
                        full_r = "".join(reason_parts)
                        yield ev("response.reasoning_summary_text.done", {
                            "item_id": rs_id, "output_index": reason_index,
                            "summary_index": 0, "text": full_r,
                        })
                        yield ev("response.reasoning_summary_part.done", {
                            "item_id": rs_id, "output_index": reason_index, "summary_index": 0,
                            "part": {"type": "summary_text", "text": full_r},
                        })
                        outputs[reason_index] = reason_item("completed")
                        yield ev("response.output_item.done",
                                 {"output_index": reason_index, "item": outputs[reason_index]})
                    msg_index = len(outputs)
                    outputs.append(None)
                    yield ev("response.output_item.added", {
                        "output_index": msg_index,
                        "item": {"id": msg_id, "type": "message", "status": "in_progress",
                                 "role": "assistant", "content": []},
                    })
                    yield ev("response.content_part.added", {
                        "item_id": msg_id, "output_index": msg_index, "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    })
                # DSML tool call buffering: do not stream raw DSML tags to client
                text_buffer += piece
                while text_buffer:
                    idx = text_buffer.find("<")
                    if idx == -1:
                        text_parts.append(text_buffer)
                        yield ev("response.output_text.delta", {
                            "item_id": msg_id, "output_index": msg_index,
                            "content_index": 0, "delta": text_buffer,
                        })
                        text_buffer = ""
                        break
                    m = DSML_CALLS_RE.search(text_buffer)
                    if m and m.start() == idx:
                        if idx > 0:
                            lead = text_buffer[:idx]
                            text_parts.append(lead)
                            yield ev("response.output_text.delta", {
                                "item_id": msg_id, "output_index": msg_index,
                                "content_index": 0, "delta": lead,
                            })
                        calls_found, _ = parse_dsml_tool_calls(m.group(0))
                        if calls_found:
                            dsml_tool_calls.extend(calls_found)
                        text_buffer = text_buffer[m.end():]
                        continue
                    cand = text_buffer[idx:idx+30]
                    is_cand = ("DSML" in cand) or (len(cand) < 10 and not any(c in cand for c in (" ", "\t", "\n", ">")))
                    if is_cand:
                        lead = text_buffer[:idx]
                        text_parts.append(lead)
                        yield ev("response.output_text.delta", {
                            "item_id": msg_id, "output_index": msg_index,
                            "content_index": 0, "delta": lead,
                        })
                        text_buffer = text_buffer[idx:]
                        break
                    else:
                        next_lt = text_buffer[idx+1:].find("<")
                        if next_lt != -1:
                            flush_len = idx + 1 + next_lt
                            lead = text_buffer[:flush_len]
                            text_parts.append(lead)
                            yield ev("response.output_text.delta", {
                                "item_id": msg_id, "output_index": msg_index,
                                "content_index": 0, "delta": lead,
                            })
                            text_buffer = text_buffer[flush_len:]
                        else:
                            text_parts.append(text_buffer)
                            yield ev("response.output_text.delta", {
                                "item_id": msg_id, "output_index": msg_index,
                                "content_index": 0, "delta": text_buffer,
                            })
                            text_buffer = ""
                            break
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    if reason_index is not None and outputs[reason_index] is None:
        full_r = "".join(reason_parts)
        yield ev("response.reasoning_summary_text.done", {
            "item_id": rs_id, "output_index": reason_index, "summary_index": 0, "text": full_r,
        })
        yield ev("response.reasoning_summary_part.done", {
            "item_id": rs_id, "output_index": reason_index, "summary_index": 0,
            "part": {"type": "summary_text", "text": full_r},
        })
        outputs[reason_index] = reason_item("completed")
        yield ev("response.output_item.done",
                 {"output_index": reason_index, "item": outputs[reason_index]})
    # 1. Emit completed structured tool calls
    for idx in sorted(tool_calls_map.keys()):
        entry = tool_calls_map[idx]
        if entry.get("custom"):
            yield ev("response.custom_tool_call_input.done", {
                "output_index": entry["output_index"],
                "item_id": entry["item_id"],
                "call_id": entry["id"],
                "input": _unwrap_custom_input(entry["arguments"]),
            })
            fc_item = {
                "id": entry["item_id"],
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": entry["id"],
                "name": entry["name"],
                "input": _unwrap_custom_input(entry["arguments"]),
            }
        else:
            yield ev("response.function_call_arguments.done", {
                "output_index": entry["output_index"],
                "call_id": entry["id"],
                "arguments": entry["arguments"],
            })
            fc_item = {
                "id": entry["item_id"],
                "type": "function_call",
                "status": "completed",
                "call_id": entry["id"],
                "name": entry["name"],
                "arguments": entry["arguments"],
            }
        outputs[entry["output_index"]] = fc_item
        yield ev("response.output_item.done", {
            "output_index": entry["output_index"],
            "item": fc_item,
        })
    # Flush remaining buffered text if any
    if text_buffer:
        calls_rem, clean_rem = parse_dsml_tool_calls(text_buffer)
        if calls_rem:
            dsml_tool_calls.extend(calls_rem)
        if clean_rem:
            text_parts.append(clean_rem)
            if msg_index is not None:
                yield ev("response.output_text.delta", {
                    "item_id": msg_id, "output_index": msg_index,
                    "content_index": 0, "delta": clean_rem,
                })
        text_buffer = ""
    # 2. DSML fallback: emit buffered/parsed DSML tool calls if no structured tool_calls were emitted
    full_text = "".join(text_parts)
    dsml_calls = dsml_tool_calls
    if not dsml_calls:
        extra_calls, clean_text = parse_dsml_tool_calls(full_text)
        if extra_calls:
            dsml_calls = extra_calls
            full_text = clean_text
    if dsml_calls and not tool_calls_map:
        for dc in dsml_calls:
            out_idx = len(outputs)
            fc_item = {
                "id": _new_id("fc_"),
                "type": "function_call",
                "status": "completed",
                "call_id": dc.get("id") or _new_id("call_"),
                "name": dc.get("name") or "",
                "arguments": dc.get("arguments") or "{}",
            }
            outputs.append(fc_item)
            yield ev("response.output_item.added", {
                "output_index": out_idx,
                "item": dict(fc_item, status="in_progress", arguments=""),
            })
            yield ev("response.function_call_arguments.delta", {
                "output_index": out_idx,
                "call_id": fc_item["call_id"],
                "delta": fc_item["arguments"],
            })
            yield ev("response.function_call_arguments.done", {
                "output_index": out_idx,
                "call_id": fc_item["call_id"],
                "arguments": fc_item["arguments"],
            })
            yield ev("response.output_item.done", {
                "output_index": out_idx,
                "item": fc_item,
            })
    # 3. Emit message item only if text was emitted OR no other output item exists
    has_other_items = any(o for o in outputs if o)
    if msg_index is not None or full_text or not has_other_items:
        if msg_index is None:
            msg_index = len(outputs)
            outputs.append(None)
            yield ev("response.output_item.added", {
                "output_index": msg_index,
                "item": {"id": msg_id, "type": "message", "status": "in_progress",
                         "role": "assistant", "content": []},
            })
            yield ev("response.content_part.added", {
                "item_id": msg_id, "output_index": msg_index, "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            })
        yield ev("response.output_text.done", {
            "item_id": msg_id, "output_index": msg_index, "content_index": 0, "text": full_text,
        })
        yield ev("response.content_part.done", {
            "item_id": msg_id, "output_index": msg_index, "content_index": 0,
            "part": {"type": "output_text", "text": full_text, "annotations": []},
        })
        outputs[msg_index] = msg_item("completed")
        yield ev("response.output_item.done", {"output_index": msg_index, "item": outputs[msg_index]})
    status = "completed" if finish != "length" else "incomplete"
    final = resp_obj(status)
    if finish == "length":
        final["incomplete_details"] = {"reason": "max_output_tokens"}
    yield ev("response.completed", {"response": final})
# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Which configured API key the caller used, set by _key_ok(). Its bound
    # realm decides the upstream exit for this request alone.
    key_entry = None
    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass
    def finish(self):
        try:
            super().finish()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass
    server_version = "wb-proxy/1.4.3"
    def log_message(self, fmt, *args):
        # 静默过滤前端看板高频定时心跳的正常 200 GET 请求（/logs、/usage、/accounts 轮询等）
        # 避免自增死循环刷屏与日志污染。遇 4xx/5xx 异常或所有非 GET 业务操作依然如实记录。
        try:
            status_code = int(args[1]) if len(args) > 1 and str(args[1]).isdigit() else 200
            if status_code < 400 and getattr(self, "command", "GET") == "GET":
                req_path = (getattr(self, "path", None) or (args[0] if args else "")).split("?")[0]
                quiet_prefixes = (
                    "/logs", "/usage", "/accounts", "/scheduler",
                    "/health", "/panel/status", "/realm", "/favicon.ico"
                )
                if any(req_path == p or req_path.startswith(p + "/") for p in quiet_prefixes):
                    return
        except Exception:
            pass
        log(fmt % args)
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
    def _error(self, code, message, err_type="server_error"):
        self._json(code, {"error": {"message": message, "type": err_type, "code": code}})
    def _rate_limited(self, exc):
        """429 with Retry-After, so clients back off instead of hammering.

        The upstream body names the reset time; when it does not, fall back to
        the shortest model cooldown we know about.
        """
        wait = max(1, int(getattr(exc, "wait", 60) or 60))
        body = json.dumps({
            "error": {
                "message": ("upstream rate limit reached for this model; retry in %ds"
                            % wait) + ((" - " + exc.detail[:200]) if exc.detail else ""),
                "type": "rate_limit_error",
                "code": 429,
                "retry_after": wait,
            }
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(429)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Retry-After", str(wait))
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
    def _download(self, filename, obj):
        """Send a JSON document as a browser download.
        Content-Disposition is quoted because the filename is generated from
        user-controlled parts (the realm filter) and could otherwise break the
        header or allow a response-splitting attempt.
        """
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        safe = re.sub(r'[^A-Za-z0-9._-]', "_", str(filename))[:120] or "export.json"
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % safe)
        self.send_header("Cache-Control", "no-store")
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
    def _supplied_key(self):
        """The key the caller presented, from the header or the ?key= query."""
        supplied = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        if supplied:
            return supplied
        # Browsers cannot set headers on a top-level navigation, so accept the
        # key as a query parameter too - the dashboard uses this when opened
        # from another device.
        try:
            query = parse_qs(urlparse(self.path).query)
            return (query.get("key") or [""])[0].strip()
        except Exception:
            return ""
    def _key_ok(self):
        """True when the request carries a right key (or no key is needed)."""
        # An authenticated panel session also unlocks the management APIs,
        # so the browser never has to keep the API key in localStorage.
        if self._panel_ok():
            return True
        self.key_entry = identify_key(self._supplied_key())
        if self.key_entry:
            return True
        if not auth_required():
            return True
        return False
    def _key_realm(self):
        """Realm bound to the key this request used, or "" when unbound."""
        return (self.key_entry or {}).get("realm") or ""
    def _cross_realm_error(self, model, realm):
        """Explain a model/exit mismatch instead of letting upstream reject it.
        Sending gpt-6-astra to the domestic exit (or deepseek-v4-pro to the
        international one) earns an opaque 403 from upstream, so catch it here
        and say which key is bound where.
        """
        if not realm or not model:
            return ""
        owner = exclusive_realm(model)
        if not owner or owner == realm:
            return ""
        name = (self.key_entry or {}).get("name") or "当前 Key"
        served = "国内版" if owner == "cn" else "国际版"
        used = "国内版" if realm == "cn" else "国际版"
        return ("模型 %s 只在%s提供，但「%s」绑定的是%s出口。"
                "请改用对应出口的 Key，或把该 Key 的出口改为「跟随面板切换」。"
                % (model, served, name, used))
    def _request_realm(self, explicit=None):
        """Pick the upstream exit for this request.
        Priority: an explicit ?realm= argument, then the realm bound to the
        API key, then the X-Realm header / ?realm= query, and finally the
        global switch. Returning None lets open_upstream() fall back to
        model-based detection.
        """
        if explicit:
            return explicit
        bound = self._key_realm()
        if bound:
            return bound
        header = self.headers.get("X-Realm")
        if header:
            return header
        try:
            return parse_qs(urlparse(self.path).query).get("realm", [None])[0]
        except Exception:
            return None
    def _authorized(self):
        if self._key_ok():
            return True
        self._error(401, "invalid api key", "invalid_request_error")
        return False
    # ---- web panel access ----
    def _panel_token(self):
        """Session token from the X-Panel-Token header.
        Deliberately header-only: a token in the query string leaks through
        browser history, the Referer header and any reverse-proxy access log.
        """
        token = (self.headers.get("X-Panel-Token") or "").strip()
        return token
    def _panel_ok(self):
        return PANEL.valid(self._panel_token())
    @staticmethod
    def _is_panel_route(path):
        """Management endpoints shown in the web panel.
        Model listings stay reachable with the API key alone so that plain
        OpenAI clients can keep discovering models.
        """
        if path.startswith("/accounts"):
            return True
        if path.startswith("/usage") or path.startswith("/v1/usage"):
            return True
        if path.startswith("/tasks") or path.startswith("/scheduler"):
            return True
        if path.startswith("/settings"):
            return True
        if path.startswith("/logs"):
            return True
        return False
    def do_OPTIONS(self):
        self.send_response(204)
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if self._is_panel_route(path) and not self._panel_ok():
            return self._error(401, "panel password required", "invalid_request_error")
        if path in ("/", "/dashboard", "/ui"):
            return self._dashboard()
        if path == "/panel/status":
            # Answer without a token: the dashboard needs to know whether to
            # show the login screen before it can hold a session.
            info = {
                "panel_password_required": True,
                "panel_password_is_default": wb_settings.panel_password_is_default(ACCOUNTS_DIR),
                "authenticated": self._panel_ok(),
            }
            # Whether a key exists is not a secret; its value never leaves the
            # process, and the settings endpoint only reports a masked form.
            info["api_key_set"] = bool(API_KEY)
            return self._json(200, info)
        if path == "/health":
            # Always answer (the launcher uses this to detect a running copy),
            # but only expose account identity to an authorised caller.
            rep = current_account()
            info = {
                "ok": True,
                # Report the realm actually in use; this used to be the
                # literal "intl" and drifted from the panel switch.
                "realm": CURRENT_REALM,
                "accounts": len(POOL.accounts) if POOL else 0,
                "accounts_ready": POOL.count_ready() if POOL else 0,
                "api_key_required": bool(API_KEY),
            }
            if self._key_ok():
                info.update({
                    "uid": rep.uid if rep else None,
                    "domain": rep.domain if rep else None,
                    "issuer": wb_accounts.jwt_issuer(rep.access_token) if rep else None,
                    "credential_file": os.path.basename(rep.path) if rep and rep.path else None,
                    "expires_at": rep.expires_at if rep else None,
                })
            return self._json(200, info)
        # Accept the conventional /v1 prefix and the bare path, because clients
        # differ in whether they append "/v1" themselves.
        if path == "/realm":
            return self._json(200, {"current": CURRENT_REALM, "options": ["intl", "cn"]})
        if path in ("/v1/models", "/models"):
            if not self._authorized():
                return
            req_realm = self._request_realm() or CURRENT_REALM
            try:
                entries = fetch_models(realm=req_realm)
            except Exception as exc:
                return self._error(502, str(exc))
            data = [model_entry(mid, meta) for mid, meta in entries]
            return self._json(200, {"object": "list", "data": data, "realm": req_realm or CURRENT_REALM})
        if path in ("/usage", "/v1/usage"):
            if not self._authorized():
                return
            req_realm = query.get('realm', [None])[0] or self.headers.get('X-Realm') or CURRENT_REALM
            return self._json(200, usage_snapshot(realm=req_realm))
        if path == "/usage/recent":
            if not self._authorized():
                return
            try:
                limit = max(1, min(1000, int((query.get("limit") or ["100"])[0])))
            except ValueError:
                limit = 100
            try:
                page = max(1, int((query.get("page") or ["1"])[0]))
            except ValueError:
                page = 1
            req_realm = query.get('realm', [None])[0] or self.headers.get('X-Realm') or CURRENT_REALM
            return self._json(200, recent_usage(limit, realm=req_realm, page=page))
        if path == "/accounts/credits":
            if not self._authorized():
                return
            # Refresh credits for all accounts
            for a in (POOL.accounts if POOL else []):
                a.fetch_credits()
            return self._json(200, {"accounts": account_views()})
        if path == "/accounts":
            if not self._authorized():
                return
            return self._json(200, {
                "accounts": account_views(realm=query.get('realm', [None])[0] or CURRENT_REALM),
                "storage": ACCOUNTS_DIR,
                "usable": POOL.count_ready() if POOL else 0,
            })
        if path == "/accounts/export":
            if not self._authorized():
                return
            # ?download=1 makes the browser save it as a file; without it the
            # document is returned inline so the dashboard can show a summary.
            # ?uid= narrows it to specific accounts (repeatable, comma-joined),
            # which is how the per-row "export" button works.
            realm = (query.get("realm") or [None])[0] or None
            if realm not in ("intl", "cn"):
                realm = None
            include_secrets = (query.get("secrets") or ["1"])[0] not in ("0", "false", "no")
            uids = []
            for raw in query.get("uid") or []:
                uids.extend(part.strip() for part in str(raw).split(",") if part.strip())
            if uids:
                known = {a.uid for a in (POOL.accounts if POOL else [])}
                missing = [u for u in uids if u not in known]
                if missing:
                    return self._error(404, "no such account: %s" % ", ".join(missing[:5]),
                                       "invalid_request_error")
            doc = wb_accounts.build_export_document(
                POOL.accounts if POOL else [],
                realm=realm,
                include_secrets=include_secrets,
                uids=uids or None,
            )
            if (query.get("download") or ["0"])[0] in ("1", "true", "yes"):
                stamp = time.strftime("%Y%m%d-%H%M%S")
                if len(uids) == 1:
                    # Name a single-account export after the account, so a
                    # folder of them stays readable.
                    label = uids[0][:8]
                else:
                    label = realm + "-" if realm else ""
                name = "workbuddy-accounts-%s%s.json" % (label, stamp)
                return self._download(name, doc)
            return self._json(200, doc)
        if path == "/accounts/login/poll":
            if not self._authorized():
                return
            state = (query.get("state") or [""])[0]
            return self._json(200, POOL.poll_login(state))
        if path == "/usage/analytics":
            if not self._authorized():
                return
            return self._json(200, compute_usage_analytics())
        if path == "/usage/by-account":
            if not self._authorized():
                return
            return self._json(200, {"accounts": usage_by_account()})
        if path == "/usage/perf":
            if not self._authorized():
                return
            try:
                sample = max(10, min(20000, int((query.get("sample") or ["5000"])[0])))
            except ValueError:
                sample = 5000
            req_realm = query.get('realm', [None])[0] or self.headers.get('X-Realm') or CURRENT_REALM
            return self._json(200, perf_stats(sample, realm=req_realm))
        if path == "/tasks":
            if not self._authorized():
                return
            cn_accounts = [a for a in (POOL.accounts if POOL else []) if a.realm == "cn" and a.enabled]
            if not cn_accounts:
                return self._json(200, {"tasks": [], "summary": {}, "accounts": [], "msg": "未找到可用的国内版账号"})
            uid = (query.get("uid") or [None])[0]
            acc = None
            if uid and uid != "all":
                target = POOL.get(uid) if POOL else None
                if target and target.realm == "cn":
                    acc = target
            if not acc:
                acc = cn_accounts[0]
            from wb_tasks import fetch_growth_tasks, fetch_growth_summary
            tasks = fetch_growth_tasks(acc)
            summary = fetch_growth_summary(acc)
            acct_list = [{"uid": a.uid, "nickname": a.nickname or a.uid[:8]} for a in cn_accounts]
            return self._json(200, {
                "tasks": tasks,
                "summary": summary,
                "account": acc.public(),
                "accounts": acct_list,
            })
        if path == "/scheduler":
            if not self._authorized():
                return
            return self._json(200, SCHEDULER.status() if SCHEDULER else {"enabled": False, "msg": "未运行"})
        if path == "/settings":
            if not self._authorized():
                return
            return self._json(200, runtime_settings_view())
        if path == "/proxy/slots":
            if not self._panel_ok():
                return self._error(
                    401, "panel password required", "invalid_request_error"
                )
            return self._json(200, {"slots": proxy_slots_view()})
        if path == "/logs":
            if not self._authorized():
                return
            try:
                limit = int(query.get("limit", ["200"])[0])
            except (ValueError, TypeError):
                limit = 200
            level = query.get("level", [""])[0]
            tag = query.get("tag", [""])[0]
            search = query.get("search", [""])[0]
            try:
                since_id = int(query.get("since_id", ["0"])[0])
            except (ValueError, TypeError):
                since_id = 0
            return self._json(200, get_logs(limit=limit, level=level, tag=tag, search=search, since_id=since_id))
        if path == "/logs/export":
            if not self._authorized():
                return
            log_data = get_logs(limit=5000)
            lines = [f"[{item['ts']}] [{item['level']}] [{item['tag']}] {item['msg']}" for item in log_data["logs"]]
            text_content = "\n".join(lines).encode("utf-8")
            filename = f"wb-proxy-{time.strftime('%Y%m%d-%H%M%S')}.log"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(text_content)))
            if cors_origin_allowed(self.path):
                self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(text_content)
            return
        if path == "/settings/reveal":
            # The panel only ever draws masked keys, so copying one needs an
            # explicit request. Panel session required, API key is not enough.
            if not self._panel_ok():
                return self._error(401, "panel password required", "invalid_request_error")
            wanted = (query.get("id") or [""])[0]
            for entry in configured_keys():
                if entry.get("id") == wanted:
                    return self._json(200, {"id": wanted, "key": entry.get("key") or ""})
            return self._error(404, "no such key", "invalid_request_error")
        return self._error(404, "not found", "invalid_request_error")
    def _dashboard(self):
        try:
            with open(DASHBOARD_HTML, "rb") as fh:
                body = fh.read()
        except Exception as exc:
            return self._error(500, f"dashboard.html unavailable: {exc}")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
    def _read_payload(self, max_bytes=MAX_PAYLOAD_BYTES, allow_list=False):
        """Parse the request body into a dict (or a list when allow_list).
        Raises BodyTooLarge / BadJSON so every caller handles both cases the
        same way instead of each remembering to check for None.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length > max_bytes:
            raise BodyTooLarge(length)
        if length < 0:
            raise BadJSON()
        try:
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            data = json.loads(raw or "{}")
        except Exception:
            raise BadJSON()
        if isinstance(data, dict):
            return data
        if allow_list and isinstance(data, list):
            # The account-import endpoint accepts a bare array of accounts,
            # which is the most natural shape for a hand-written file.
            return data
        return {}
    def _payload_or_error(self, allow_list=False):
        """Read the body, replying with the right error and returning None."""
        try:
            return self._read_payload(allow_list=allow_list)
        except BodyTooLarge as exc:
            self._error(413, "payload too large (%d bytes > %d limit)"
                        % (exc.length, MAX_PAYLOAD_BYTES), "invalid_request_error")
            return None
        except BadJSON:
            self._error(400, "invalid JSON body", "invalid_request_error")
            return None
    def _handle_settings_save(self):
        """Persist panel-managed settings from the web settings tab."""
        payload = self._payload_or_error()
        if payload is None:
            return
        reply = {}
        if "api_keys" in payload:
            raw = payload.get("api_keys")
            if not isinstance(raw, list):
                return self._error(400, "api_keys must be a list", "invalid_request_error")
            # The panel only ever shows a masked key, so a blank value means
            # "keep what is stored" for that row rather than "clear it".
            existing = {entry.get("id"): entry for entry in configured_keys()}
            cleaned = []
            for index, item in enumerate(raw):
                if not isinstance(item, dict):
                    return self._error(400, "each api key must be an object",
                                       "invalid_request_error")
                entry_id = str(item.get("id") or "").strip()
                value = str(item.get("key") or "").strip()
                if not value and entry_id and entry_id in existing:
                    value = existing[entry_id].get("key") or ""
                if not entry_id:
                    entry_id = "k%d" % index
                if value and len(value) < 4:
                    return self._error(400, "api key must be at least 4 characters",
                                       "invalid_request_error")
                if not value:
                    return self._error(400, "a key entry is empty - fill it in or remove the row",
                                       "invalid_request_error")
                realm = str(item.get("realm") or "").strip().lower()
                if realm not in ("", "intl", "cn"):
                    return self._error(400, "realm must be intl, cn or empty",
                                       "invalid_request_error")
                created_at = item.get("created_at") or (existing.get(entry_id, {}).get("created_at") if entry_id in existing else None) or time.strftime("%Y/%m/%d %H:%M")
                cleaned.append({
                    "id": entry_id,
                    "name": str(item.get("name") or "").strip(),
                    "key": value,
                    "realm": realm,
                    "enabled": item.get("enabled", True) is not False,
                    "created_at": created_at,
                })
            wb_settings.set_api_keys(ACCOUNTS_DIR, cleaned)
            reply["api_keys_saved"] = len(cleaned)
        if "auth_disabled" in payload:
            wb_settings.set_auth_disabled(ACCOUNTS_DIR, payload.get("auth_disabled"))
            reply["auth_disabled"] = bool(payload.get("auth_disabled"))
        new_key = payload.get("api_key")
        if new_key is not None:
            new_key = str(new_key).strip()
            if new_key and len(new_key) < 4:
                return self._error(400, "api key must be at least 4 characters",
                                   "invalid_request_error")
            global API_KEY, API_KEY_FILE_SET
            wb_settings.set_api_key(ACCOUNTS_DIR, new_key)
            API_KEY = new_key
            API_KEY_FILE_SET = True
            reply["api_key_set"] = bool(new_key)
        if payload.get("restart_scheduler"):
            if SCHEDULER:
                SCHEDULER.stop()
                SCHEDULER.start()
            reply["scheduler"] = "restarted"
        reply.update(runtime_settings_view())
        return self._json(200, reply)

    def _handle_proxy_slots(self, path, payload):
        """Proxy-slot management (panel-authenticated)."""
        if path == "/proxy/slots":
            return self._json(200, {"slots": proxy_slots_view()})
        if path == "/proxy/slots/save":
            raw = payload.get("slots")
            if not isinstance(raw, list):
                return self._error(400, "slots must be a list", "invalid_request_error")
            cleaned = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "").strip()
                if not url:
                    continue
                cleaned.append(
                    {
                        "id": str(item.get("id") or "").strip(),
                        "name": str(item.get("name") or "").strip(),
                        "url": url,
                        "enabled": item.get("enabled", True) is not False,
                    }
                )
            saved = wb_settings.set_proxy_slots(ACCOUNTS_DIR, cleaned)
            if POOL:
                POOL.apply_proxy_slots(saved)
            log("proxy slots saved: %d slot(s)" % len(saved))
            return self._json(200, {"slots": proxy_slots_view()})
        if path == "/proxy/slots/test":
            slot_id = str(payload.get("id") or "").strip()
            slot = wb_settings.find_proxy_slot(ACCOUNTS_DIR, slot_id)
            if slot is None:
                return self._error(404, "no such proxy slot")
            started = time.time()
            exit_ip, error = probe_proxy_exit(slot["url"])
            return self._json(
                200,
                {
                    "ok": not error,
                    "id": slot_id,
                    "exit_ip": exit_ip,
                    "latency_ms": int((time.time() - started) * 1000),
                    "error": error,
                },
            )
        if path == "/proxy/discover":
            return self._json(200, {"candidates": discover_proxy_slots()})
        return self._error(404, "not found", "invalid_request_error")

    def _handle_panel(self, path):
        """Panel login, logout and the settings screen (password + API key)."""
        payload = self._payload_or_error()
        if payload is None:
            return
        if path == "/panel/login":
            client_ip = self.client_address[0] if hasattr(self, "client_address") and self.client_address else "127.0.0.1"
            now = time.time()
            with _login_lock:
                _prune_login_attempts(now)
                attempts = [t for t in _login_attempts.get(client_ip, []) if now - t < 60]
                _login_attempts[client_ip] = attempts
                if len(attempts) >= 5:
                    wait_sec = int(60 - (now - attempts[0]))
                    return self._error(429, f"too many login attempts, please wait {max(1, wait_sec)}s", "rate_limit_error")
            password = str(payload.get("password") or "")
            if not wb_settings.verify_panel_password(ACCOUNTS_DIR, password):
                with _login_lock:
                    _login_attempts.setdefault(client_ip, []).append(now)
                # Small backoff delay to mitigate automated brute force
                time.sleep(0.5)
                return self._error(401, "invalid panel password", "invalid_request_error")
            with _login_lock:
                _login_attempts.pop(client_ip, None)
            token = PANEL.create()
            return self._json(200, {
                "ok": True,
                "token": token,
                "using_default_password": wb_settings.panel_password_is_default(ACCOUNTS_DIR),
            })
        if path == "/panel/logout":
            PANEL.revoke(self._panel_token())
            return self._json(200, {"ok": True})
        # Everything past this point requires an authenticated panel session.
        if not self._panel_ok():
            return self._error(401, "panel password required", "invalid_request_error")
        if path == "/panel/password":
            current = str(payload.get("current") or "")
            new = str(payload.get("new") or "")
            if not wb_settings.verify_panel_password(ACCOUNTS_DIR, current):
                return self._error(401, "current password is wrong", "invalid_request_error")
            if len(new) < 4:
                return self._error(400, "new password must be at least 4 characters", "invalid_request_error")
            wb_settings.set_panel_password(ACCOUNTS_DIR, new)
            if new != wb_settings.DEFAULT_PANEL_PASSWORD:
                # Rotating the password invalidates every other browser session.
                PANEL.revoke_all()
            token = PANEL.create()
            return self._json(200, {"ok": True, "token": token})
        return self._error(404, "not found", "invalid_request_error")
    def _handle_accounts(self, path, payload):
        """Account-management endpoints (dashboard uses these)."""
        if POOL is None:
            return self._error(503, "account pool unavailable")
        if path == "/accounts/import" and isinstance(payload, list):
            # A bare array is only meaningful for import; wrap it so the rest
            # of this handler can keep assuming a dict.
            payload = {"data": payload}
        if not isinstance(payload, dict):
            return self._error(400, "expected a JSON object", "invalid_request_error")
        if path in ("/accounts/credits", "/accounts/credits/fetch"):
            uid = payload.get("uid")
            realm = payload.get("realm")
            if uid:
                targets = [POOL.get(uid)]
            elif realm and realm != "all":
                targets = [a for a in POOL.accounts if a.realm == realm]
            else:
                targets = list(POOL.accounts)
            results = []
            for account in targets:
                if account is None:
                    continue
                res = account.fetch_credits()
                results.append({"uid": account.uid, "ok": res.get("ok", False),
                                "credits": account.credits, "error": res.get("error", "")})
            return self._json(200, {"results": results, "accounts": account_views()})
        if path == "/tasks/run":
            if not POOL:
                return self._json(200, {"ok": False, "msg": "账号池不可用"})
            uid = payload.get("uid")
            if uid and uid != "all":
                target = POOL.get(uid)
                if not target or target.realm != "cn":
                    return self._json(200, {"ok": False, "msg": "未找到指定的国内版账号"})
                targets = [target]
            else:
                targets = [a for a in POOL.accounts if a.realm == "cn" and a.enabled]
            if not targets:
                return self._json(200, {"ok": False, "msg": "未找到已启用的国内版账号"})
            from wb_tasks import run_growth_tasks
            combined_logs = []
            total_credit = 0
            for i, acc in enumerate(targets):
                uid_str = acc.uid[:8] if acc.uid else "?"
                nick = acc.nickname or uid_str
                combined_logs.append(f"====== 正在为账号 [{nick} ({acc.uid})] 执行全自动成长任务 ({i+1}/{len(targets)}) ======")
                res = run_growth_tasks(acc, gap=1.0)
                # run_growth_tasks() reports its total as "earned_credit";
                # reading the old "credit_added" name silently summed zeros
                # and the dashboard always showed "+0 积分".
                total_credit += res.get("earned_credit") or 0
                for l in res.get("logs") or []:
                    combined_logs.append(f"  {l}")
                if i < len(targets) - 1:
                    time.sleep(1.5)
            combined_logs.append(f"====== 全部 {len(targets)} 个账号任务执行完毕，累计新增积分: +{total_credit} ======")
            return self._json(200, {
                "ok": True,
                "credit_added": total_credit,
                "logs": combined_logs,
                "accounts_count": len(targets)
            })
        if path == "/tasks/travel":
            if not POOL:
                return self._json(200, {"ok": False, "msg": "账号池不可用"})
            uid = payload.get("uid")
            if uid and uid != "all":
                target = POOL.get(uid)
                if not target or target.realm != "cn":
                    return self._json(200, {"ok": False, "msg": "未找到指定的国内版账号"})
                targets = [target]
            else:
                targets = [a for a in POOL.accounts if a.realm == "cn" and a.enabled]
            if not targets:
                return self._json(200, {"ok": False, "msg": "未找到已启用的国内版账号"})
            from wb_tasks import do_cat_travel
            results = []
            for i, acc in enumerate(targets):
                uid_str = acc.uid[:8] if acc.uid else "?"
                nick = acc.nickname or uid_str
                res = do_cat_travel(acc)
                results.append({
                    "uid": acc.uid,
                    "nickname": nick,
                    "action": res.get("action"),
                    "msg": res.get("msg") or "",
                    # do_cat_travel() returns the amount as "credit".
                    "reward_credit": res.get("credit", 0)
                })
                if i < len(targets) - 1:
                    time.sleep(1.0)
            summary_msg = chr(10).join([f"{r['nickname']}: {r['msg']}" for r in results])
            return self._json(200, {
                "ok": True,
                "results": results,
                "msg": summary_msg,
                "accounts_count": len(targets)
            })
        if path == "/scheduler/trigger":
            if SCHEDULER:
                return self._json(200, SCHEDULER.trigger_now())
            return self._json(200, {"ok": False, "msg": "调度器未初始化"})
        if path == "/scheduler/toggle":
            if SCHEDULER:
                SCHEDULER.enabled = not SCHEDULER.enabled
                SCHEDULER.log(f"用户切换调度器状态为: {'启用' if SCHEDULER.enabled else '暂停'}")
                return self._json(200, SCHEDULER.status())
            return self._json(200, {"ok": False, "msg": "调度器未初始化"})
        if path == "/logs/clear":
            clear_logs()
            return self._json(200, {"ok": True})
        if path == "/realm":
            new_realm = payload.get("realm")
            if new_realm in ("intl", "cn"):
                save_persisted_realm(new_realm)
            return self._json(200, {"ok": True, "current": CURRENT_REALM, "persisted": True})
        if path == "/accounts/checkin":
            uid = payload.get("uid")
            targets = [POOL.get(uid)] if uid else [a for a in (POOL.accounts if POOL else []) if a.realm == "cn"]
            results = []
            for account in targets:
                if account is None:
                    continue
                res = account.checkin()
                results.append({"uid": account.uid, "nickname": account.nickname, **res})
            return self._json(200, {"results": results, "accounts": account_views()})
        if path == "/accounts/login/start":
            platform = payload.get("platform") or "CLI"
            target_realm = payload.get("realm") or CURRENT_REALM
            try:
                started = POOL.start_login(realm=target_realm, platform=platform)
            except Exception as exc:
                return self._error(502, "could not start login: %s" % exc)
            log("oauth login started (realm=%s, platform=%s, state=%s)" % (target_realm, platform, started["state"][:8]))
            return self._json(200, started)
        if path == "/accounts/login/cancel":
            state = payload.get("state") or ""
            return self._json(200, {"cancelled": POOL.cancel_login(state)})
        if path == "/accounts/import/desktop":
            # Two ways to call this:
            #   {}                     -> scan only (read-only, nothing imported)
            #   {"path": "..."}        -> import that credential
            #   {"all": true}          -> import everything the scan found
            target_path = payload.get("path")
            if target_path:
                realm = payload.get("realm")
                try:
                    account = POOL.import_desktop_credential(
                        path=target_path, realm=realm, source="desktop-app")
                except Exception as exc:
                    return self._error(400, "import failed: %s" % exc)
                log("imported %s from %s (user confirmed)" % (account.uid[:8], os.path.basename(target_path)))
                return self._json(200, {
                    "imported": [account.public()],
                    "accounts": account_views(),
                })
            if payload.get("all"):
                imported = import_desktop_accounts(payload.get("realm"))
                return self._json(200, {
                    "imported": [a.public() for a in imported],
                    "accounts": account_views(),
                })
            return self._json(200, {
                "detected": desktop_credential_scan(),
                "accounts": account_views(),
                "pool_uids": [a.uid for a in POOL.accounts],
            })
        if path == "/accounts/refresh":
            uid = payload.get("uid")
            targets = [POOL.get(uid)] if uid else list(POOL.accounts)
            results = []
            for account in targets:
                if account is None:
                    continue
                ok = account.refresh()
                account.save(ACCOUNTS_DIR)
                results.append({"uid": account.uid, "ok": ok, "error": account.last_error})
            return self._json(200, {"results": results})
        if path == "/accounts/test":
            uid = payload.get("uid")
            if not uid:
                return self._error(400, "uid required")
            account = POOL.get(uid)
            if not account:
                return self._error(404, "no such account")
            test_model = payload.get("model") or "deepseek-v4.1-flash"
            cfg = wb_accounts.get_realm_config(account.realm)
            chat_url = cfg["chat_upstream"] + CHAT_PATH
            test_body = {
                "model": test_model,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            }
            forwarded = build_upstream_body(test_body)
            body = json.dumps(forwarded, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                chat_url, data=body, method="POST",
                headers=account.headers(purpose="chat")
            )
            t0 = time.time()
            try:
                with wb_accounts.urlopen(req, timeout=30, proxy=account.proxy) as resp:
                    chat_obj = aggregate_stream(resp, test_model, None)
                    wall_ms = int((time.time() - t0) * 1000)
                    choices = chat_obj.get("choices") or []
                    msg = (choices[0].get("message") or {}) if choices else {}
                    reply_text = (msg.get("content") or msg.get("reasoning_content") or "OK").strip()
                    if len(reply_text) > 80:
                        reply_text = reply_text[:77] + "..."
                    account.clear_error()
                    log(f"account test: uid={account.uid[:8]} model={test_model} wall={wall_ms}ms ok=True", tag="accounts")
                    return self._json(200, {
                        "ok": True,
                        "uid": account.uid,
                        "model": test_model,
                        "elapsed_ms": wall_ms,
                        "reply": reply_text,
                    })
            except urllib.error.HTTPError as exc:
                wall_ms = int((time.time() - t0) * 1000)
                detail = exc.read(400).decode("utf-8", "replace")
                account.note_error(f"HTTP {exc.code}: {detail[:80]}", cooldown=60)
                log(f"account test: uid={account.uid[:8]} model={test_model} wall={wall_ms}ms error={exc.code}", level="WARN", tag="accounts")
                return self._json(200, {
                    "ok": False,
                    "uid": account.uid,
                    "status": exc.code,
                    "error": f"HTTP {exc.code}: {detail[:150]}",
                    "elapsed_ms": wall_ms,
                })
            except Exception as exc:
                wall_ms = int((time.time() - t0) * 1000)
                account.note_error(str(exc)[:80], cooldown=60)
                log(f"account test: uid={account.uid[:8]} model={test_model} wall={wall_ms}ms exc={exc}", level="WARN", tag="accounts")
                return self._json(200, {
                    "ok": False,
                    "uid": account.uid,
                    "status": 500,
                    "error": str(exc),
                    "elapsed_ms": wall_ms,
                })
        if path == "/accounts/set":
            uid = payload.get("uid")
            if not uid:
                return self._error(400, "uid required")
            updated = None
            if "proxySlot" in payload:
                updated = POOL.set_proxy_slot(uid, payload.get("proxySlot"))
                if updated is None:
                    return self._error(404, "no such account")
                log(
                    "account %s proxy slot set to %s"
                    % (uid[:8], updated.get("proxySlot") or "(direct)")
                )
            if "proxy" in payload:
                updated = POOL.set_proxy(uid, payload.get("proxy"))
                if updated is None:
                    return self._error(404, "no such account")
                log(
                    "account %s proxy set to %s"
                    % (uid[:8], updated.get("proxy") or "(direct)")
                )
            if "enabled" in payload:
                updated = POOL.set_enabled(uid, bool(payload.get("enabled")))
                if updated is None:
                    return self._error(404, "no such account")
                log(
                    "account %s %s"
                    % (uid[:8], "enabled" if payload.get("enabled") else "disabled")
                )
            if updated is None:
                return self._error(
                    400, "nothing to update: pass 'enabled', 'proxy' or 'proxySlot'"
                )
            return self._json(200, {"account": updated})
        if path == "/accounts/set-all":
            POOL.set_all_enabled(bool(payload.get("enabled")))
            return self._json(200, {"accounts": account_views()})
        if path == "/accounts/delete":
            uid = payload.get("uid")
            if not uid:
                return self._error(400, "uid required")
            removed = POOL.remove(uid)
            log("account %s deleted" % uid[:8])
            return self._json(200, {"deleted": removed, "accounts": account_views()})
        if path == "/accounts/import":
            # Import a previously exported document (or any hand-written list
            # of accounts). Body shapes accepted, see wb_accounts._coerce_account_rows:
            #   {"format":"workbuddy-accounts","accounts":[...]}   <- our export
            #   [...]                                              <- bare list
            #   {"accessToken": ...}                               <- single account
            #   {"account":{...},"auth":{...}}                     <- desktop credential
            #
            # Options:
            #   dryRun    (bool) - validate and report, write nothing
            #   overwrite (bool) - replace accounts whose uid already exists
            #   realm     ("intl"|"cn") - force a realm instead of detecting it
            #
            # `data` carries the document. It is preferred over the bare body so
            # the body can also hold the options above.
            blob = payload.get("data") if "data" in payload else payload
            if not isinstance(blob, (dict, list)):
                return self._error(400, "the document must be a JSON object or array",
                                   "invalid_request_error")
            rows, problem = wb_accounts._coerce_account_rows(blob)
            if problem:
                return self._error(400, "cannot read the document: %s" % problem,
                                   "invalid_request_error")
            dry_run = bool(payload.get("dryRun"))
            overwrite = bool(payload.get("overwrite"))
            forced_realm = (payload.get("realm") or "").strip().lower() or None
            if forced_realm and forced_realm not in ("intl", "cn"):
                return self._error(400, "realm must be intl or cn", "invalid_request_error")
            if dry_run:
                # Validate every row without touching the pool so the caller can
                # see exactly what an import would do before committing to it.
                # Shares its rules with the real import, so the preview cannot
                # disagree with what would actually happen.
                return self._json(200, {
                    "dryRun": True,
                    "count": len(rows),
                    "result": POOL.preview_import_rows(rows, realm=forced_realm, overwrite=overwrite),
                    "accounts": account_views(),
                })
            report = POOL.import_rows(rows, realm=forced_realm, overwrite=overwrite)
            log("account import: %d added, %d updated, %d skipped, %d invalid"
                % (len(report["added"]), len(report["updated"]),
                   len(report["skipped"]), len(report["invalid"])))
            return self._json(200, {
                "count": len(rows),
                "result": report,
                "accounts": account_views(),
            })
        return self._error(404, "unknown account endpoint", "invalid_request_error")
    def _handle_responses(self, payload):
        """Serve /v1/responses by translating to chat completions upstream."""
        session_key = extract_session_key(self.headers, payload)
        custom_names = custom_tool_names(payload.get("tools"))
        chat_req = responses_to_chat(payload)
        model = payload.get("model") or "deepseek-v4.1-flash"
        want_stream = bool(payload.get("stream"))
        t_start = time.time()
        fp = prompt_fingerprint(chat_req.get("messages"))
        log(
            "responses: model=%s stream=%s msgs=%d effort=%r custom_tools=%s"
            % (model, want_stream, len(chat_req.get("messages") or []),
               chat_req.get("reasoning_effort"),
               sorted(custom_names) or "-")
        )
        try:
            req_realm = self._request_realm() or CURRENT_REALM
            blocked = self._cross_realm_error(chat_req.get("model"), req_realm)
            if blocked:
                return self._error(400, blocked, "invalid_request_error")
            upstream, account = open_upstream(chat_req, session_key=session_key, target_realm=req_realm)
        except RateLimited as exc:
            t = time.time() - t_start
            record_error(model, 429, exc.detail[:200], elapsed_ms=int(t * 1000),
                         account=getattr(exc, "account_uid", None))
            return self._rate_limited(exc)
        except urllib.error.HTTPError as exc:
            detail = exc.read(600).decode("utf-8", "replace")
            record_error(model, exc.code, detail,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None))
            return self._error(exc.code, f"upstream {exc.code}: {detail}")
        except Exception as exc:
            message = str(exc)
            record_error(model, 502, message,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None))
            if message.startswith("no usable account"):
                return self._error(503, message +
                                   " - add or enable one at the dashboard (/)")
            return self._error(502, f"upstream unreachable: {exc}")
        with upstream:
            if want_stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                if cors_origin_allowed(self.path):
                    self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                holder = {"usage": None, "custom_names": custom_names}
                first_ms = None
                try:
                    for frame in stream_responses_events(upstream, model, holder):
                        if first_ms is None:
                            first_ms = int((time.time() - t_start) * 1000)
                        # PATCHED-BY-OPS: 与 chat completions 路径对齐，清洗噪音帧
                        # （空 function_call 占位会让 sub2api 等严格解析器卡在
                        #  legacy 工具调用分支，报 "no terminal response event"）
                        self.wfile.write(clean_responses_frame(frame))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    wall = int((time.time() - t_start) * 1000)
                    record_usage(model, holder.get("usage"), stream=True, elapsed_ms=wall,
                                 ttft_ms=first_ms,
                                 gen_ms=(wall - first_ms) if first_ms is not None else None,
                                 fp=fp, account=account.uid)
                    return
                except Exception as exc:
                    # Upstream died mid-stream (timeout, incomplete read, ...).
                    # Without this the traceback escapes to the HTTP layer and
                    # the client is left holding a half-finished stream with no
                    # terminal event.
                    wall = int((time.time() - t_start) * 1000)
                    record_error(model, 502, "stream aborted: %s" % exc,
                                 elapsed_ms=wall, account=account.uid)
                    record_usage(model, holder.get("usage"), stream=True, elapsed_ms=wall,
                                 ttft_ms=first_ms,
                                 gen_ms=(wall - first_ms) if first_ms is not None else None,
                                 fp=fp, account=account.uid)
                    try:
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                    except Exception:
                        pass
                    return
                wall = int((time.time() - t_start) * 1000)
                record_usage(model, holder.get("usage"), stream=True, elapsed_ms=wall,
                             ttft_ms=first_ms,
                             gen_ms=(wall - first_ms) if first_ms is not None else None,
                             fp=fp, account=account.uid)
                return
            try:
                chat_obj = aggregate_stream(upstream, model, None)
            except Exception as exc:
                record_error(model, 502, str(exc),
                             elapsed_ms=int((time.time() - t_start) * 1000),
                             account=account.uid)
                return self._error(502, f"upstream stream error: {exc}")
            wall = int((time.time() - t_start) * 1000)
            result = chat_to_response(chat_obj, model, custom_names)
            record_usage(model, chat_obj.get("usage"), stream=False, elapsed_ms=wall, fp=fp,
                         account=account.uid)
            return self._json(200, result)
    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/settings/save":
            if not self._panel_ok():
                return self._error(401, "panel password required", "invalid_request_error")
            return self._handle_settings_save()
        if path.startswith("/proxy/"):
            if not self._panel_ok():
                return self._error(
                    401, "panel password required", "invalid_request_error"
                )
            payload = self._payload_or_error()
            if payload is None:
                return
            return self._handle_proxy_slots(path, payload)
        if path in ("/panel/login", "/panel/logout", "/panel/password"):
            return self._handle_panel(path)
        if self._is_panel_route(path) and not self._panel_ok():
            return self._error(401, "panel password required", "invalid_request_error")
        is_account_route = (
            path.startswith("/accounts/")
            or path == "/realm"
            or path.startswith("/tasks")
            or path.startswith("/scheduler")
            or path.startswith("/logs")
        )
        if not is_account_route and path not in ("/v1/chat/completions", "/chat/completions",
                                                "/v1/completions", "/completions",
                                                "/v1/responses", "/responses"):
            return self._error(404, "not found", "invalid_request_error")
        if not self._authorized():
            return
        payload = self._payload_or_error(allow_list=(path == "/accounts/import"))
        if payload is None:
            return
        if is_account_route:
            return self._handle_accounts(path, payload)
        if path in ("/v1/responses", "/responses"):
            return self._handle_responses(payload)
        # Diagnostics: what the client actually asked for, and what we forward.
        # Only the knobs that change behaviour are logged - never message text.
        forwarded = build_upstream_body(payload)
        given = payload.get("reasoning_effort") or payload.get("reasoning") \
            or payload.get("thinking") or payload.get("enable_thinking")
        log(
            "chat: model=%s client_effort=%r -> upstream_effort=%r stream=%s msgs=%d"
            % (
                payload.get("model"),
                given,
                forwarded.get("reasoning_effort"),
                bool(payload.get("stream")),
                len(forwarded.get("messages") or []),
            )
        )
        session_key = extract_session_key(self.headers, payload)
        fp = prompt_fingerprint(forwarded.get("messages"))
        want_stream = bool(payload.get("stream"))
        model = payload.get("model") or "hy4-preview"
        t_start = time.time()
        try:
            req_realm = self._request_realm() or CURRENT_REALM
            blocked = self._cross_realm_error(payload.get("model"), req_realm)
            if blocked:
                return self._error(400, blocked, "invalid_request_error")
            upstream, account = open_upstream(payload, session_key=session_key, target_realm=req_realm)
        except RateLimited as exc:
            record_error(model, 429, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None))
            return self._rate_limited(exc)
        except urllib.error.HTTPError as exc:
            detail = exc.read(600).decode("utf-8", "replace")
            record_error(model, exc.code, detail,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None))
            return self._error(exc.code, f"upstream {exc.code}: {detail}")
        except Exception as exc:
            message = str(exc)
            record_error(model, 502, message, elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None))
            if message.startswith("no usable account"):
                # Only a genuinely empty/cooling pool is a 503. A throttled model
                # is reported as 429 by _rate_limited above instead.
                return self._error(503, message +
                                   " - add or enable one at the dashboard (/)")
            return self._error(502, f"upstream unreachable: {exc}")
        with upstream:
            if want_stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                if cors_origin_allowed(self.path):
                    self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                emitted = False
                last_usage = None
                first_ms = None
                try:
                    for line in upstream:
                        data = strip_data_prefix(line.decode("utf-8", "replace"))
                        if not data or data == "[DONE]" or data.startswith(":"):
                            continue
                        try:
                            maybe = json.loads(data)
                            if maybe.get("usage"):
                                last_usage = maybe["usage"]
                        except Exception:
                            pass
                        cleaned = clean_chunk(data)
                        if not cleaned:
                            continue
                        if first_ms is None:
                            first_ms = int((time.time() - t_start) * 1000)
                        emitted = True
                        self.wfile.write(f"data: {cleaned}\n\n".encode("utf-8"))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    # Client hung up; still account for what upstream produced.
                    wall = int((time.time() - t_start) * 1000)
                    record_usage(model, last_usage, stream=True,
                                 elapsed_ms=wall, ttft_ms=first_ms,
                                 gen_ms=(wall - first_ms) if first_ms is not None else None,
                                 fp=fp, account=account.uid)
                    return
                except Exception as exc:
                    # Upstream quit mid-stream (timeout, incomplete read, ...).
                    # The client would otherwise get a truncated stream with no
                    # terminal marker, and the traceback reached the HTTP layer.
                    wall = int((time.time() - t_start) * 1000)
                    record_error(model, 502, "stream aborted: %s" % exc,
                                 elapsed_ms=wall, account=account.uid)
                    record_usage(model, last_usage, stream=True,
                                 elapsed_ms=wall, ttft_ms=first_ms,
                                 gen_ms=(wall - first_ms) if first_ms is not None else None,
                                 fp=fp, account=account.uid)
                    try:
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                    except Exception:
                        pass
                    return
                if not emitted:
                    err = json.dumps({"error": {"message": "empty upstream stream", "type": "server_error"}})
                    self.wfile.write(f"data: {err}\n\n".encode("utf-8"))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                wall = int((time.time() - t_start) * 1000)
                record_usage(model, last_usage, stream=True,
                             elapsed_ms=wall, ttft_ms=first_ms,
                             gen_ms=(wall - first_ms) if first_ms is not None else None,
                             fp=fp, account=account.uid)
                return
            try:
                result = aggregate_stream(upstream, model, None)
            except Exception as exc:
                record_error(model, 502, str(exc), elapsed_ms=int((time.time() - t_start) * 1000),
                             account=account.uid)
                return self._error(502, f"upstream stream error: {exc}")
            wall = int((time.time() - t_start) * 1000)
            first_at = result.get("first_chunk_at")
            # Measured from request arrival so streaming and non-streaming are comparable.
            first_ms = int((first_at - t_start) * 1000) if first_at else None
            record_usage(model, result.get("usage"), stream=False,
                         elapsed_ms=wall, ttft_ms=first_ms,
                         gen_ms=(wall - first_ms) if first_ms is not None else None,
                         fp=fp, account=account.uid)
            return self._json(200, result)
def main():
    global POOL, ACCOUNTS_DIR, API_KEY, SYSTEM_PROMPT, USAGE_DIR, USAGE_LOG
    API_KEY_GENERATED = False
    ap = argparse.ArgumentParser(description="WorkBuddy (workbuddy.ai) -> OpenAI-compatible proxy")
    ap.add_argument("--info", help="path to the WorkBuddy *.info credential file")
    ap.add_argument("--host", default=os.environ.get("HOST") or "127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT") or "8788"))
    ap.add_argument("--lan", action="store_true",
                    help="listen on every interface so other devices on the LAN can "
                         "reach it (implies --host 0.0.0.0 and forces an api key)")
    ap.add_argument("--api-key", default=os.environ.get("API_KEY") or os.environ.get("WB_PROXY_KEY") or None,
                    help="require this bearer token on /v1/* (optional)")
    ap.add_argument("--system-prompt", default=DEFAULT_SYSTEM_PROMPT,
                    help="system message injected when the request has none (required upstream)")
    ap.add_argument("--user-agent", default=None,
                    help="override the upstream User-Agent (default: mirror the official "
                         "WorkBuddy AI client)")
    ap.add_argument("--usage-dir", default=None,
                    help="where to store usage.jsonl (default: ./usage)")
    ap.add_argument("--accounts-dir", default=os.environ.get("ACCOUNTS_DIR") or None,
                    help="where the per-account credential files live (default: ./accounts)")
    ap.add_argument("--import-desktop", action="store_true",
                    help="import the desktop app credential as an account, then exit")
    ap.add_argument("--panel-password", default=None,
                    help="set the web panel password on startup (default: admin)")
    args = ap.parse_args()
    # LAN mode binds every interface. The key is generated below, once
    # ACCOUNTS_DIR is resolved, so it can be persisted and reused.
    if args.lan and args.host == "127.0.0.1":
        args.host = "0.0.0.0"
    if args.user_agent:
        wb_accounts.USER_AGENT = args.user_agent.strip()
        log("user-agent : %s (override)" % wb_accounts.USER_AGENT)
    if args.usage_dir:
        USAGE_DIR = os.path.abspath(args.usage_dir)
        USAGE_LOG = os.path.join(USAGE_DIR, "usage.jsonl")
    # Refuse to start a second copy. On Windows SO_REUSEADDR lets two sockets
    # bind the same port, which silently splits incoming connections between
    # them - confusing and hard to diagnose.
    try:
        probe = urllib.request.urlopen(
            f"http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{args.port}/health",
            timeout=2,
        )
        existing = json.loads(probe.read().decode("utf-8"))
    except Exception:
        existing = None  # nothing answering /health - let the bind below decide
    if isinstance(existing, dict):
        # Only OUR /health carries the account-pool fields ("accounts"). Other
        # services can occupy the same port and also answer /health with JSON
        # (a dev proxy, another gateway); treating that as "already running" made this
        # launcher exit silently while the port belonged to someone else - the
        # dashboard then showed a foreign UI and API calls failed with 401/404.
        foreign = existing.get("service") or "accounts" not in existing
        if foreign:
            who = existing.get("service") or "an unknown HTTP service"
            print()
            print(f"  [ERROR] port {args.port} is already taken by another program: {who}")
            print("          wb-proxy itself is NOT running - nothing was started.")
            print()
            print("  Fix: start wb-proxy on a different port, e.g.")
            print(f"          start-wb-proxy.bat {args.port + 1}")
            print(f"          python wb_proxy.py --port {args.port + 1}")
            print()
            print("  Check who owns the port:  netstat -ano | findstr :%d" % args.port)
            print()
            raise SystemExit(1)
        print()
        print(f"  [已有一个反代在 {args.port} 端口运行，无需重复启动]")
        print(f"  账号: {existing.get('uid', '?')} @ {existing.get('domain', '?')}")
        print(f"  看板: http://127.0.0.1:{args.port}/")
        print()
        print("  如果要重启: 先把原来那个窗口关掉（或结束 python 进程），再运行本程序。")
        print()
        return
    API_KEY = args.api_key
    SYSTEM_PROMPT = args.system_prompt
    if args.accounts_dir:
        ACCOUNTS_DIR = os.path.abspath(args.accounts_dir)
    # LAN mode must not ship a known key: the gateway spends the account's own
    # upstream quota, so a guessable default lets anyone on the network drain
    # it. Generate one on first use, persist it, and reuse it afterwards.
    if args.lan and not API_KEY:
        API_KEY, API_KEY_GENERATED = wb_settings.ensure_launcher_key(ACCOUNTS_DIR)
    # A key saved from the panel wins over an auto-generated LAN key so a
    # change made in the browser survives a restart of the .bat file. An
    # explicit --api-key on the command line still takes precedence.
    global API_KEY_FILE_SET
    saved_key, key_from_panel = wb_settings.api_key_override(ACCOUNTS_DIR)
    if key_from_panel and not args.api_key:
        API_KEY = saved_key
        API_KEY_FILE_SET = True
    if args.panel_password:
        wb_settings.set_panel_password(ACCOUNTS_DIR, args.panel_password)
        log("panel      : password set from --panel-password")
    elif wb_settings.panel_password_is_default(ACCOUNTS_DIR):
        log("panel      : password is still the default 'admin' - change it in the panel")
    POOL = wb_accounts.AccountPool(ACCOUNTS_DIR, log=log)
    POOL.load()
    POOL.apply_proxy_slots()
    load_persisted_realm()
    global SCHEDULER
    from wb_scheduler import Scheduler
    SCHEDULER = Scheduler(POOL)
    SCHEDULER.start()
    if args.info:
        account = POOL.import_desktop_credential(args.info, source="file")
        log("imported account %s from %s" % (account.uid[:8], args.info))
    first_run = not POOL.accounts
    if first_run:
        # Never adopt the desktop client's login silently: just report what is
        # available and let the user import it from the dashboard.
        detected = desktop_credential_scan()
        usable = [d for d in detected if d.get("valid")]
        if usable:
            log("no accounts yet - detected %d desktop credential(s), NOT importing" % len(usable))
            for d in usable:
                log("  available: %s  %s  %s" % (
                    (d.get("uid") or "?")[:8], d.get("nickname") or "(no name)",
                    d.get("realmName") or d.get("realm")))
            log("open the dashboard and click [Scan desktop app] to import")
        else:
            log("no accounts yet - no desktop credentials found on this machine")
    if first_run and not POOL.accounts:
        # Do NOT exit here: the dashboard has to stay reachable so a new
        # account can be added through the browser login flow.
        log("still no accounts - starting anyway so you can log in via the dashboard")
    if args.import_desktop:
        for account in POOL.accounts:
            print("  %s  %s  %s" % (account.uid[:8], account.nickname, account.domain))
        return
    rep = current_account()
    log("accounts   : %d total, %d usable" % (len(POOL.accounts), POOL.count_ready()))
    for account in POOL.accounts:
        log("  - %s  %s  %s  %s" % (account.uid[:8], account.nickname or "(no name)",
                                    account.domain, wb_accounts._human_delta(
                                        (account.expires_at or 0) - time.time()) or "?"))
    log("store      : %s" % ACCOUNTS_DIR)
    log(f"credential : {rep.path if rep else chr(45)}")
    if os.path.exists(PRODUCT_CONFIG_CACHE):
        log(f"catalog    : {PRODUCT_CONFIG_CACHE}")
    else:
        log("catalog    : app cache not found - will use the model API instead")
    log(f"account    : {rep.uid if rep else chr(45)} @ {rep.domain if rep else chr(45)}")
    log(f"issuer     : {wb_accounts.jwt_issuer(rep.access_token) if rep else chr(45)}")
    log("realm      : %s (%s)" % (
        CURRENT_REALM,
        "www.workbuddy.ai" if CURRENT_REALM == "intl" else "copilot.tencent.com"))
    log("user-agent : %s" % wb_accounts.USER_AGENT)
    if args.host == "0.0.0.0":
        ips = local_ip_addresses() or ["<this-pc-ip>"]
        print()
        print("  " + "=" * 62)
        print("  LAN MODE - reachable from other devices")
        print()
        for ip in ips:
            print("    API       : http://%s:%s/v1" % (ip, args.port))
            print("    Dashboard : http://%s:%s/" % (ip, args.port))
        print()
        print("    API Key   : %s" % API_KEY)
        if API_KEY_GENERATED:
            print("                (newly generated & saved to accounts/settings.json)")
        else:
            print("                (reused from accounts/settings.json)")
        print()
        print("    Open the dashboard (key already included):")
        print("      http://%s:%s/?key=%s" % (ips[0], args.port, API_KEY))
        print()
        print("    Clients: Base URL = the API address above, then paste the key.")
        print()
        print("    If nothing can connect, allow python through the")
        print("    firewall: run allow-firewall.bat once as administrator.")
        print("  " + "=" * 62)
        print()
        sys.stdout.flush()
    if not POOL.accounts:
        print()
        print("  " + "=" * 62)
        print("  NO ACCOUNTS YET")
        print()
        print("  Open the dashboard and click [Login new account]:")
        print("      http://127.0.0.1:%s/" % args.port)
        print()
        print("  The browser flow adds the account automatically.")
        print("  This window must stay open.")
        print("  " + "=" * 62)
        print()
        sys.stdout.flush()
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        # Port stolen between the probe above and this bind, or held by
        # something that does not answer /health: report it in plain words
        # instead of dumping a raw socketserver traceback.
        print()
        print(f"  [ERROR] failed to listen on {args.host}:{args.port} - {exc}")
        print("          the port is reserved or held by another program;")
        print("          wb-proxy did NOT start.")
        print()
        print("  Fix: stop the program holding the port, or pick another port:")
        print(f"          netstat -ano | findstr :{args.port}")
        print(f"          start-wb-proxy.bat {args.port + 1}")
        print()
        raise SystemExit(1)
    # Only claim the address once the socket really exists, so a failed bind
    # never prints a "listening" line that contradicts the error below.
    log(f"listening  : http://{args.host}:{args.port}/v1  (api key: {'on' if API_KEY else 'off'})")
    log(f"dashboard  : http://{args.host}:{args.port}/")
    # Keep the handler referenced for the process lifetime: SetConsoleCtrlHandler
    # stores a raw pointer, so a collected callback would crash on close.
    _ctrl_handler = install_console_close_handler()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("bye")
    finally:
        try:
            server.server_close()
        except Exception:
            pass
if __name__ == "__main__":
    try:
        # Keep console output readable regardless of the active code page.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
