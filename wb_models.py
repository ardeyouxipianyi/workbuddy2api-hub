"""Model catalogue: which models each realm serves and what the gateway lists.

Builds the per-realm model list behind /v1/models from, in order of trust:
the upstream remote product config (/v3/config), the desktop client's local
product config, the account's models endpoint, and the bundled catalogue
(wb_catalog). Each entry carries context / output limits, reasoning levels and
pricing tier (wb_tiers), with models.dev data filling gaps (wb_modelsdev).

Also owns the catalogue cache (memory + `catalog/` on disk), the daily remote
refresh loop, and `catalog_meta()` which tells the panel where a list came from.
Gateway state (pool, accounts dir, active realm, logging) is read through `_gw()`.
"""

import json
import os
import threading
import time
import urllib.request

import wb_accounts
import wb_catalog
import wb_modelsdev
import wb_probes
import wb_runtime
import wb_tiers

_gw = wb_runtime.gateway


_models_lock = threading.Lock()


_models_cache = {"intl": {"at": 0.0, "data": None}, "cn": {"at": 0.0, "data": None}}
#: 目录来源追踪（面板展示用）：每个 realm 最近一次构建/刷新清单的出处。
#: source 取值见 catalog_meta()；fetched_at 是网关拿到这批数据的时刻；
#: file_mtime 是桌面缓存文件自己的写入时刻（仅配置文件来源，可能远早于
#: fetched_at——那正是「清单是不是新鲜」的证据）。与 _models_cache 同批更新。
_catalog_meta = {}
#: 目录落盘存档：每次成功从上游 /v3/config 拉到清单就写一份响应体副本，纯留档
#: （网关自己不读它）。与 usage 一样运行时产生、在 .gitignore 里，不参与提交
#: 与更新；覆盖目录用 WB_PROXY_CATALOG_DIR。
CATALOG_CACHE_DIR = os.environ.get("WB_PROXY_CATALOG_DIR") \
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog")


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
    # The domestic exit's auto-router entry: the picker shows it, but it is
    # not a model a client can pin, so it stays out of the advertised list.
    "auto",
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
    "hy4-preview-f",
    "hy3",
    "deepseek-v4.1-flash",
    "gpt-6-astra",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "grok-4.7",
    "gemini-3.5-flash",
    "glm-5.3-flash",
    "glm-5.3",
    "glm-5.2",
    "kimi-k3",
    "kimi-k2.6",
    "kimi-k2.8-preview",
]
def merge_reasoning(base, live):
    """Field-level merge of one model's reasoning block.

    The live catalogue is the source of truth, but it does not always restate
    every field: the desktop endpoint sometimes gives a bare `effort` where the
    bundled table knows the level is selectable. A top-level update would drop
    `supportedEfforts`, and an `effort` without it reads as a pin - the model
    then looks unselectable in /v1/models and every request to it is reported at
    the wrong level. So the two blocks are merged field by field, and a block
    that ends up with `supportedEfforts` is selectable: its `effort` is the
    default restated, not a pin.
    """
    out = dict(base) if isinstance(base, dict) else {}
    if isinstance(live, dict):
        out.update(live)
    if out.get("supportedEfforts") and out.get("effort"):
        out.setdefault("defaultEffort", out["effort"])
        out.pop("effort", None)
    return out
def merge_catalog(primary, realm=None, extras=False):
    r = realm or _gw().CURRENT_REALM
    merged = {}
    # "all" is the union of both realms. The analytics dashboard lists every
    # model the gateway has served, so it must not drop the ones that only
    # one side's catalog knows about.
    if r == "all":
        source_static = list(wb_catalog.STATIC_INTL_MODELS) + list(wb_catalog.STATIC_CN_MODELS)
    else:
        source_static = getattr(wb_catalog, "STATIC_CN_MODELS" if r == "cn" else "STATIC_INTL_MODELS", wb_catalog.STATIC_MODELS)
    primary_ids = set(m for m, _ in (primary or []))
    for item in source_static:
        mid = item.get("id")
        if mid == "hy4-preview-f" and "hy4-preview" in primary_ids and "hy4-preview-f" not in primary_ids:
            continue
        if mid and is_chat_model(mid) and mid not in merged:
            merged[mid] = dict(item)
    for mid, meta in primary or []:
        if not is_chat_model(mid):
            continue
        if meta:
            base = merged.get(mid) or {}
            base_reasoning = base.get("reasoning")
            base.update(meta)
            if isinstance(meta.get("reasoning"), dict):
                base["reasoning"] = merge_reasoning(base_reasoning, meta["reasoning"])
            merged[mid] = base
        elif mid not in merged:
            merged[mid] = {}
    order = CN_UI_ORDER if r == "cn" else INTL_UI_ORDER
    if r == "all":
        order = list(INTL_UI_ORDER) + [m for m in CN_UI_ORDER if m not in INTL_UI_ORDER]
    if primary_ids:
        order = [("hy4-preview" if (m == "hy4-preview-f" and "hy4-preview" in primary_ids and "hy4-preview-f" not in primary_ids) else m) for m in order]
    out = []
    seen = set()
    for mid in order:
        if mid in merged and mid not in seen:
            seen.add(mid)
            out.append((mid, merged[mid]))
    if extras:
        # A model the curated table has never heard of still ships when the
        # *live* catalogue lists it - that is how a newly added upstream model
        # reaches /v1/models without a release. The bundled snapshot alone is
        # not enough: it also carries legacy entries the picker may not show.
        for mid, _meta in primary or []:
            if mid in merged and mid not in seen:
                item = merged[mid]
                # 无倍率模型隐藏：未配置有效 credits（内置表无、远程 meta 也无）不进入对外目录
                if not item.get("credits"):
                    continue
                seen.add(mid)
                out.append((mid, item))
    return out
_catalog_lock = threading.Lock()

_catalog_fallback_log = {}
def note_bundled_reasoning(realm, live, entries):
    """Say once when a model's reasoning controls come from the bundled table.

    The live catalogue is the source of truth and the snapshot is only the
    fallback, so values the snapshot alone carries can be no fresher than the
    snapshot. Two shapes put them there: a model the live catalogue gives no
    reasoning block at all, and one it gives a bare `effort` for where the
    snapshot knows the level is selectable. One line per change is enough.
    """
    live_meta = dict(live or [])

    def from_snapshot(mid, meta):
        reasoning = meta.get("reasoning") or {}
        if not reasoning:
            return False
        live_reasoning = (live_meta.get(mid) or {}).get("reasoning") or {}
        if not live_reasoning:
            return True
        return bool(reasoning.get("supportedEfforts")) \
            and not live_reasoning.get("supportedEfforts")

    missing = sorted(mid for mid, meta in entries if from_snapshot(mid, meta))
    if not missing:
        _catalog_fallback_log.pop(realm, None)
        return
    if _catalog_fallback_log.get(realm) == frozenset(missing):
        return
    _catalog_fallback_log[realm] = frozenset(missing)
    shown = ", ".join(missing[:6]) + (" ..." if len(missing) > 6 else "")
    _gw().log("catalog    : bundled table fills in reasoning controls the live "
        "catalogue omits for %d model(s): %s" % (len(missing), shown))
def catalog_meta(realm=None):
    """The provenance of the last list built for a realm, for the panel.

    {"source": "remote" | "endpoint" | "cache" | "legacy" | "bundled",
     "fetched_at": unix seconds when the gateway obtained the batch,
     "file_mtime": unix seconds of the desktop cache file's own write
                   (only for the config-file sources), else None}
    """
    r = realm or _gw().CURRENT_REALM
    with _models_lock:
        meta = _catalog_meta.get(r)
        return dict(meta) if meta else None


def models_available_for(model, realm):
    """Whether at least one account in the pool can serve this model.

    A PRO-only model needs a paid account; any other model only needs one
    enabled, credential-bearing account in the realm. Readiness (cooldowns,
    quotas) is deliberately not part of the test: a model vanishing because its
    only account is cooling for a minute would make auto-discovering clients
    re-read the catalogue over and over. With no pool at all this returns True,
    so a fresh install still advertises the full catalogue.
    """
    if _gw().POOL is None:
        return True
    usable = [a for a in _gw().POOL.accounts
              if a.realm == realm and a.enabled and a.access_token]
    if not usable:
        return False
    if wb_tiers.requires_pro(model, realm):
        return any(getattr(a, "is_paid_user", False) for a in usable)
    return True


def fetch_models(realm=None):
    r = realm or _gw().CURRENT_REALM
    with _models_lock:
        c = _models_cache.get(r) or {"at": 0.0, "data": None}
        if c["data"] and time.time() - c["at"] < 300:
            return c["data"]
    # One upstream walk per realm even when several callers miss the cache at
    # the same moment: a batch of /v1/models requests must not turn into a
    # batch of upstream requests.
    with _catalog_lock:
        with _models_lock:
            c = _models_cache.get(r) or {"at": 0.0, "data": None}
            if c["data"] and time.time() - c["at"] < 300:
                return c["data"]
        live, extras, source = curated_live_sources(r)
        if not live and r in ("intl", "all"):
            # The narrow endpoint is not the desktop catalogue, so it keeps the
            # old whitelist behaviour: only names the order table knows.
            live = [(m, {}) for m in fetch_endpoint_models()]
            extras = False
            if live:
                source = "endpoint"
        entries = merge_catalog(live, realm=r, extras=extras)
        note_bundled_reasoning(r, live, entries)
        if not live:
            # 没有任何活数据：清单完全来自内置快照（硬编码兜底）。
            source = "bundled"
        elif source in ("remote", "endpoint"):
            # 记下「这个 realm 今天已经从 API 拿到过目录」，每日兜底循环据此
            # 跳过（见 catalog_daily_pass）。
            note_remote_catalog(r)
        file_mtime = None
        if source in ("cache", "legacy") and r in ("intl", "cn"):
            try:
                file_mtime = os.path.getmtime(product_config_path(r))
            except OSError:
                file_mtime = None
        with _models_lock:
            _models_cache[r] = {"at": time.time(), "data": entries}
            _catalog_meta[r] = {"source": source, "fetched_at": time.time(),
                                "file_mtime": file_mtime}
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
    if mid == "glm-5.3-flash":
        # 强制修正：上游元数据虽宣告支持多模态，但后端实际未接入视觉通道（见 issue #315）
        vision = False
        item["vision_note"] = "上游宣告多模态但实际通道不支持读图，已修正为纯文本" 
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
    # Four-level lookup: remote > built-in knowledge table > local model.json
    # cache > models.dev (asynchronously warmed; never blocks). context_length
    # always has a value (1M when unknown - a high estimate is safer than a
    # low one); max_output_tokens is omitted when unknown.
    ctx_value, out_value, _limit_source = wb_modelsdev.lookup(
        mid, meta.get("maxInputTokens"), meta.get("maxOutputTokens"),
        directory=_gw().ACCOUNTS_DIR)
    item["context_length"] = ctx_value
    item["max_input_tokens"] = ctx_value
    if out_value:
        item["max_output_tokens"] = out_value
        item["max_completion_tokens"] = out_value
    # Annotate the measured upstream clamp (from output_probes.json) without
    # overriding the model's spec value; the panel shows "钳制 N×" beside it.
    clamp = wb_probes.clamp_for(_gw().ACCOUNTS_DIR, mid)
    if clamp:
        item["output_clamp"] = clamp
        item["max_output_tokens_clamped"] = clamp
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
    """Read the desktop app's cached catalog: [(id, meta), ...].

    "all" reads both apps when they are installed, so the combined view gets
    each side's metadata instead of only the domestic one.
    """
    r = realm or _gw().CURRENT_REALM
    if r == "all":
        out = _read_product_config_dir(".workbuddy-ai")
        seen = set(mid for mid, _ in out)
        for mid, meta in _read_product_config_dir(".workbuddy"):
            if mid not in seen:
                out.append((mid, meta))
        return out
    return _read_product_config_dir(".workbuddy-ai" if r == "intl" else ".workbuddy")


def _read_product_config_dir(cache_dir):
    home = os.path.expanduser("~")
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
#: The desktop client's own product-config endpoint. The cache file that
#: read_product_config_models() reads is this response written to disk, so
#: calling it directly is what lets a machine without the desktop app
#: (Docker, NAS, a headless server) advertise the live catalogue - live
#: multipliers included - instead of the narrower endpoint or the bundled
#: snapshot.
REMOTE_CONFIG_PATH = "/v3/config"

#: Suffixes that mark a variant of a name the catalogue already carries: the
#: regional build (deepseek-v4.1-flash-sg) and the experimental one (hy3-x).
#: Measured on both exits: the plain name is the free (x0.00) one and the
#: variant is the paid one, so the plain name is what gets advertised.
VARIANT_SUFFIXES = ("-sg", "-x")


def remote_config_headers(account, realm, ua=None):
    """Headers for the product-config call.

    The UA decides which catalogue comes back and only the desktop UA returns
    the full list (an unknown one is a hard 400, code 12403), so this uses a
    realm's fixed desktop UA rather than the account's current identity.
    """
    cfg = wb_accounts.get_realm_config(realm)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": ua or cfg["chat_ua"],
        "Origin": cfg["origin"],
        "Referer": cfg["origin"] + "/",
        "Authorization": "Bearer " + account.access_token,
        "X-User-Id": account.uid,
    }
    # 静态 API Key 必须额外带 X-API-Key：只发 Authorization 时上游下发 54 条
    # 而不是 56 条，且 HTTP 仍是 200、没有任何提示（实测）。少的四条正是
    # glm-5.2-aicc / glm-5.3-flashx / kimi-k3-2 / minimax-m3-pay。
    if getattr(account, "is_api_key", False):
        headers["X-API-Key"] = account.access_token
    return headers


def _agent_model_lists(payload):
    """Every agent's bare-string model list, cli-named agents first.

    The catalogue the picker shows rides in agents[].models. The endpoint
    answers with it under a "data" key while the desktop cache file is the
    same document written to disk without that envelope, so both are read.
    """
    roots = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        roots.append(data)
    cli, other = [], []
    for root in roots:
        agents = root.get("agents")
        if isinstance(agents, dict):
            entries = list(agents.items())
        elif isinstance(agents, list):
            entries = [((entry.get("name") if isinstance(entry, dict) else None),
                        entry) for entry in agents]
        else:
            continue
        for name, entry in entries:
            if not isinstance(entry, dict):
                continue
            models = entry.get("models")
            if not (isinstance(models, list) and models
                    and isinstance(models[0], str)):
                continue
            ids = [str(m).strip() for m in models if isinstance(m, str)]
            ids = [m for m in ids if m]
            if not ids:
                continue
            (cli if str(name or "").strip().lower() == "cli" else other).append(ids)
    return cli, other


def parse_remote_catalog(payload):
    """(ids, meta) from a /v3/config response, or None when it carries none.

    The picker's list rides in agents[].models as bare ids - under "data" in
    the endpoint's answer, at the top level in the desktop cache file. The
    per-model metadata (credits, limits, copy) lives in a separate models
    array. An unusable credential answers HTTP 200 with an *empty* list, so
    an empty catalogue is reported as None and the caller falls back instead
    of publishing "this exit has no models".
    """
    if not isinstance(payload, dict):
        return None
    cli_lists, other_lists = _agent_model_lists(payload)
    pool = cli_lists or other_lists
    best = max(pool, key=len) if pool else None
    ids, seen = [], set()
    for mid in best or []:
        if mid not in seen:
            seen.add(mid)
            ids.append(mid)
    if not ids:
        return None

    meta = {}

    def walk(node):
        if isinstance(node, dict):
            models = node.get("models")
            if isinstance(models, list) and models \
                and isinstance(models[0], dict) and models[0].get("id"):
                for item in models:
                    mid = str(item.get("id") or "").strip()
                    if mid:
                        meta.setdefault(mid, item)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return ids, meta


def snapshot_credits():
    """id -> credits from the bundled catalogue (both realms, intl first).

    Used to answer "is there a free sibling?" for a variant the remote lists
    but whose sibling it no longer does: the free hy4-preview-f, for example,
    is what the cn picker keeps while the remote only names the paid one.
    """
    out = {}
    for source in (getattr(wb_catalog, "STATIC_INTL_MODELS", []),
                           getattr(wb_catalog, "STATIC_CN_MODELS", [])):
        for item in source or []:
            mid = str(item.get("id") or "").strip()
            if mid:
                out.setdefault(mid, str(item.get("credits") or "").strip().lower())
    return out


def curate_remote_catalog(realm, ids, meta=None):
    """Trim a remote catalogue to the models the picker should offer.

      - virtual aliases (default-model ... auto) are not models;
      - "-sg" / "-x" builds are the paid variant of a name the list already
        carries;
      - when a free ("x0.00") sibling exists, the free one is the one the
        picker shows, so the paid sibling is dropped;
      - everything else keeps its upstream order. Names the upstream does not
        list at all stay available through the curated order tables and the
        bundled snapshot, which merge_catalog() keeps.
    """
    credits = snapshot_credits()
    for mid, item in (meta or {}).items():
        if isinstance(item, dict):
            credits[mid] = str(item.get("credits") or "").strip().lower()
    order = CN_UI_ORDER if realm == "cn" else INTL_UI_ORDER
    known = set(ids) | set(credits) | set(order)

    def free(mid):
        return credits.get(mid) in ("x0.00", "x0", "0", "0.00")

    out = []
    for mid in ids:
        if not is_chat_model(mid):
            continue
        if mid.endswith(VARIANT_SUFFIXES):
            continue
        if mid.endswith("-f"):
            base = mid[:-2]
            if base in ids and free(base) and not free(mid):
                continue
        elif (mid + "-f") in ids and free(mid + "-f") and not free(mid):
            continue
        out.append(mid)
    return out


def fetch_remote_product_config(realm):
    """(ids, meta) from the realm's own product-config endpoint, or None.

    At most two 10s attempts bound the wait: one per desktop UA, because the
    endpoint sits behind the WAF where a dropped connection is normal, and
    every caller has a fallback (the desktop cache file, the narrow model
    endpoint, the bundled snapshot).
    """
    if realm not in ("intl", "cn") or _gw().POOL is None:
        return None
    account = _gw().POOL.representative(realm=realm)
    if account is None or not account.access_token:
        _gw().log("remote catalog: no usable %s account, skipping" % realm)
        return None
    cfg = wb_accounts.get_realm_config(realm)
    url = cfg["chat_upstream"] + REMOTE_CONFIG_PATH
    # The chat UA is the desktop identity the rest of the gateway uses; the
    # plain app UA is the second try, for a build that answers only to it.
    uas = [cfg["chat_ua"]]
    if cfg.get("billing_ua") and cfg["billing_ua"] != cfg["chat_ua"]:
        uas.append(cfg["billing_ua"])
    last = None
    for ua in uas:
        headers = remote_config_headers(account, realm, ua)
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with wb_accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:
            last = exc
            continue
        parsed = parse_remote_catalog(payload)
        if parsed:
            # Keep the PRO-only map from the same payload; it is what the
            # scheduler and /v1/models use to keep free accounts off models
            # the upstream would answer 11158 for.
            wb_tiers.ingest(realm, payload)
            # 成功副本落盘留档（catalog/，纯存档；见 persist_remote_catalog）。
            persist_remote_catalog(realm, payload)
            return parsed
        last = "empty catalogue"
    _gw().log("remote catalog: %s fetch failed (%s)" % (realm, last))
    return None


#: 面板「刷新模型清单」按钮的服务端冷却：同一 realm 两次手动强制拉取的最短
#: 间隔。自动链本来就有 300s 缓存与桌面 UA 双尝试（最多两次请求），这个冷却
#: 再拦一层误连点，别让面板把 /v3/config 点成高频请求——上游有 WAF，连接被丢
#: 是常态，节制本身就是不触发风控的一部分。
CATALOG_REFRESH_COOLDOWN = 30.0
_catalog_refresh_at = {}


def refresh_catalog(realm):
    """手动刷新：立即向上游拉一次该 realm 的目录并发布到 /v1/models。

    与自动链（fetch_models 缓存过期后）共用同一条取数管线：
    fetch_remote_product_config 只挑该 realm 的代表账号、只打该 realm 的
    端点（realm 隔离由它内部的 POOL.representative(realm=realm) 保证，
    绝无跨区取数）；_catalog_lock 与自动链互斥，不会并发打到上游；失败
    时不清旧缓存——清单保持可用，等用户或自动链下次再拉。与自动链的一个
    差别：这里不读桌面缓存文件，拉不到就说拉不到（手动按钮要的是「这次
    真的从上游 API 取到了」的如实反馈）。

    返回面板可直接展示的 dict（任何情况都不抛异常）：
      {"ok": bool, "realm": str|None, "source": "remote"|None,
       "count": int, "cooldown": bool?, "retry_after": float?, "msg": str}
    """
    if realm not in ("intl", "cn"):
        return {"ok": False, "realm": None, "source": None, "count": 0,
                "msg": "realm 必须是 intl 或 cn"}
    now = time.time()
    with _models_lock:
        wait = CATALOG_REFRESH_COOLDOWN - (now - _catalog_refresh_at.get(realm, 0.0))
        if wait > 0:
            return {"ok": False, "realm": realm, "source": None, "count": 0,
                    "cooldown": True, "retry_after": round(wait, 1),
                    "msg": "刷新太频繁：约 %d 秒后可再试" % max(1, int(wait + 0.5))}
        # 先登记再拉取：失败也进冷却，连点的高频重试在这里就被拦住。
        _catalog_refresh_at[realm] = now
    with _catalog_lock:
        try:
            remote = fetch_remote_product_config(realm)
        except Exception as exc:
            _gw().log("catalog refresh: %s failed (%s)" % (realm, exc))
            remote = None
        if not remote:
            _gw().log("catalog refresh: %s kept the previous list (upstream gave none)" % realm)
            return {"ok": False, "realm": realm, "source": None, "count": 0,
                    "msg": "上游未返回可用目录（账号不可用或网络异常），已保留现有清单"}
        ids, meta = remote
        live = [(mid, meta.get(mid) or {})
                for mid in curate_remote_catalog(realm, ids, meta)]
        entries = merge_catalog(live, realm=realm, extras=True)
        note_bundled_reasoning(realm, live, entries)
        with _models_lock:
            _models_cache[realm] = {"at": time.time(), "data": entries}
            _catalog_meta[realm] = {"source": "remote", "fetched_at": time.time(),
                                    "file_mtime": None}
    _gw().log("catalog refresh: %s updated from the product-config endpoint (%d models)"
        % (realm, len(entries)))
    note_remote_catalog(realm)
    return {"ok": True, "realm": realm, "source": "remote", "count": len(entries),
            "msg": "已从上游 API 更新清单，共 %d 个模型" % len(entries)}


#: 目录的「每天至少从上游 API 取一次」兜底。活跃使用的部署里 fetch_models
#: 的 300s 缓存把数据刷得比这更新；这个后台循环只兜两种情况：长时间没人
#: 访问，或自动链一直回退到缓存文件/内置快照。上游有 WAF，频率上限就是
#: 每天每个 realm 一次——成功即打点（距上次成功满 24h 才再次触发）；失败
#: 不打扰成功记录，随循环每小时再试（拿不到新鲜数据时快一点重试是对的）。
CATALOG_DAILY_INTERVAL = 24 * 3600
_catalog_last_remote = {}


def note_remote_catalog(realm, when=None):
    """记下该 realm 最近一次成功从上游 API 拿到目录的时刻。"""
    if realm not in ("intl", "cn"):
        return
    with _models_lock:
        _catalog_last_remote[realm] = when if when is not None else time.time()


def catalog_daily_due(now=None):
    """到期的 realm 列表：距上次成功 API 获取 ≥ 24h（从未成功过也算到期）。"""
    now = now if now is not None else time.time()
    with _models_lock:
        return [r for r in ("intl", "cn")
                if now - _catalog_last_remote.get(r, 0.0) >= CATALOG_DAILY_INTERVAL]


def catalog_daily_pass(now=None):
    """每日兜底的一轮：只为到期的 realm 触发一次刷新。

    与手动刷新共用 refresh_catalog（同一把 _catalog_lock、同一个 30s 冷却、
    同样的 realm 隔离），失败保留旧数据。没有可用账号的 realm 静默跳过——
    不是错误，等有账号了下一轮再看，也不留日志噪声。
    """
    done = []
    for realm in catalog_daily_due(now):
        if _gw().POOL is None or _gw().POOL.representative(realm=realm) is None:
            continue
        out = refresh_catalog(realm)
        _gw().log("catalog daily: %s -> %s" % (realm, out.get("msg")))
        done.append((realm, out))
    return done


def catalog_daily_loop():
    # 先等启动流程跑完（账号池、服务就位），之后每小时看一眼。
    time.sleep(60)
    while True:
        try:
            catalog_daily_pass()
        except Exception as exc:
            _gw().log("catalog daily: pass failed (%s)" % exc)
        time.sleep(3600)


def start_catalog_daily():
    threading.Thread(target=catalog_daily_loop, name="catalog-daily",
                     daemon=True).start()


def product_config_path(realm):
    """The desktop cache file for a realm (the intl app writes its own)."""
    home = os.path.expanduser("~")
    cache_dir = ".workbuddy-ai" if realm == "intl" else ".workbuddy"
    return os.path.join(home, cache_dir, "cache", "acc-product-config-v3.json")


def catalog_cache_path(realm):
    """网关自己的落盘副本：最近一次成功拉取的 /v3/config 响应体。"""
    return os.path.join(CATALOG_CACHE_DIR, "product-config-%s.json" % realm)


def persist_remote_catalog(realm, payload):
    """把刚成功拉取的 /v3/config 响应体原样落盘一份（原子写；失败静默）。

    纯存档：网关自己不读它，只为留一份「最近一次 API 拿到的原始目录」——
    重启或排查时可直接查看文件。写盘失败不是取数失败，绝不能影响本次结果。
    """
    try:
        os.makedirs(CATALOG_CACHE_DIR, exist_ok=True)
        path = catalog_cache_path(realm)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as exc:
        _gw().log("catalog cache: %s persist failed (%s)" % (realm, exc))


def read_cached_remote_catalog(realm):
    """(ids, meta) from the desktop cache file, parsed like the remote."""
    if realm == "all":
        first = read_cached_remote_catalog("intl")
        second = read_cached_remote_catalog("cn")
        if not first:
            return second
        if not second:
            return first
        ids = list(first[0]) + [m for m in second[0] if m not in set(first[0])]
        meta = dict(second[1])
        meta.update(first[1])
        return ids, meta
    try:
        with open(product_config_path(realm), encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return None
    wb_tiers.ingest(realm, payload)
    return parse_remote_catalog(payload)


def curated_live_sources(realm):
    """(entries, extras, source) for the realm's live catalogue, already curated.

    Remote first, then the desktop cache file - the cache is this very
    response written to disk, so both go through the same parser and the same
    rules. `extras` says the entries came from the desktop catalogue, whose
    membership may add a model the curated tables have never seen; the legacy
    readers keep the old whitelist behaviour. `source` names where the batch
    came from ("remote" = 上游 /v3/config，"cache" = 同机的桌面缓存文件，
    "legacy" = 同一文件的宽松解析)，面板据此把「通过 API 获取」与「读取
    配置文件」分开说；None = 哪里都没拿到。
    """
    remote = None
    try:
        remote = fetch_remote_product_config(realm)
    except Exception as exc:
        _gw().log("remote catalog: %s failed (%s)" % (realm, exc))
    if remote:
        ids, meta = remote
        return ([(mid, meta.get(mid) or {})
                for mid in curate_remote_catalog(realm, ids, meta)], True, "remote")
    cached = read_cached_remote_catalog(realm)
    if cached:
        ids, meta = cached
        return ([(mid, meta.get(mid) or {})
                for mid in curate_remote_catalog(realm, ids, meta)], True, "cache")
    legacy = read_product_config_models(realm=realm)
    if legacy:
        ids = [mid for mid, _ in legacy]
        meta = dict((mid, m) for mid, m in legacy if isinstance(m, dict))
        keep = set(curate_remote_catalog(realm, ids, meta))
        return ([(mid, m) for mid, m in legacy if mid in keep], False, "legacy")
    return [], False, None


def fetch_endpoint_models():
    account = _gw().POOL.pick(realm="intl") if _gw().POOL else None
    if account is None:
        _gw().log("model discovery skipped: no usable account")
        cached = _models_cache.get("intl", {}).get("data")
        return [m for m, _ in (cached or [])]
    req = urllib.request.Request(_gw().UPSTREAM + _gw().MODELS_PATH, method="GET", headers=account.headers())
    try:
        with wb_accounts.urlopen(req, timeout=30, proxy=account.proxy) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        _gw().log(f"model discovery failed: {exc}")
        cached = _models_cache.get("intl", {}).get("data")
        return [m for m, _ in (cached or [])]
    ids, seen = [], set()
    for agent in (payload.get("data") or {}).get("agents") or []:
        for mid in agent.get("models") or []:
            if mid not in seen:
                seen.add(mid)
                ids.append(mid)
    return ids
