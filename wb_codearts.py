# -*- coding: utf-8 -*-
"""华为云 CodeArts（盘古助手/码道）原生客户端与账号池。

这是单进程版本：wb-hub 直接完成 OAuth 登录、token 续期、账号轮转、模型发现、
聊天 SSE 与签到，不再依赖独立的 codearts2api 服务。

协议来源：codearts2api 的 Go 实现与 `docs/reverse-engineering.md`。关键点：

- 聊天：`POST https://snap-access.cn-north-4.myhuaweicloud.com/api/v2/chat/completions`
  `x-auth-token` + 华为云 AK/SK `SDK-HMAC-SHA256` 签名；`stream=true`。
- 登录：华为云 CodeArts OAuth2（PKCE + DPoP）→ `sts.cn-north-4/v1/oauth2/tokens`
  换 STS 临时 AK/SK + security_token；refresh_token 与 client_id、DPoP 公钥绑定。
- 模型：agent-center / builtin / 限时福利三路合并，福利模型聊天时加
  `maas_type: benefit`。
- 签到：`/v1/ops/delivery|claim|confirm`，`Agent-Type: PromptCenter`。

依赖：Python 3.9+ 标准库；密码学原语在 `wb_codearts_crypto.py`。
"""

import json
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import wb_settings
import wb_codearts_crypto as crypto

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SNAP_HOST = os.environ.get("WB_CODEARTS_SNAP_HOST") or \
    "https://snap-access.cn-north-4.myhuaweicloud.com"
SNAP_MANAGER = os.environ.get("WB_CODEARTS_SNAP_MANAGER") or \
    SNAP_HOST + "/snap-manager"
PORTAL_HOST = os.environ.get("WB_CODEARTS_PORTAL_HOST") or \
    "https://codearts.huaweicloud.com/portal"
STS_HOST = os.environ.get("WB_CODEARTS_STS_HOST") or \
    "https://sts.cn-north-4.myhuaweicloud.com"
BENEFIT_HOST = os.environ.get("WB_CODEARTS_BENEFIT_HOST") or \
    "https://opengw.developer.huaweicloud.com"

CLIENT_ID = "codearts-agent"
PLUGIN_NAME = "snap_AIIDE"
PLUGIN_VERSION = "5.2.0"
REDIRECT_PATH = "/oauth/callback"

EP_CHAT = "/api/v2/chat/completions"
EP_OAUTH_TOKENS = "/v1/oauth2/tokens"
EP_LOGIN_TICKET = "/v1/login/ticket"
EP_MODEL_BUILTIN = "/v1/model/builtin"
EP_AGENT_LIST = "/v1/agent-center/agents/useragents"
EP_AGENT_DETAIL = "/v1/agent-center/agents/detail"
EP_BENEFIT_CONFIG = "/api/v1/gateway/config"
EP_BENEFIT_CLAIM = "/api/v1/benefit/claim"
EP_CREDIT_DELIVERY = "/v1/ops/delivery"
EP_CREDIT_CLAIM = "/v1/ops/claim"
EP_CREDIT_CONFIRM = "/v1/ops/confirm"
EP_PACKAGE_INFO = "/v1/statistics/plugin"

HEADER_MAAS_TYPE = "maas_type"
MAAS_BENEFIT = "benefit"

#: 冷启动种子：首次发现前也能列出/路由福利模型。
SEED_BENEFIT_MODELS = (
    "deepseek-v4-flash-0731", "deepseek-v4-pro-0813", "glm-5.3-flash",
)
SEED_KNOWN_MODELS = (
    "GLM-5.2", "GLM-5.2-ArkTS-SPARK", "GLM-5.1", "GLM-4.7",
    "OpenPangu-2.0-Pro", "OpenPangu-2.0-Flash",
    "Qwen3-VL-235B", "Qwen3.5-397B-A17B-VL", "Qwen3.6-27B-VL",
)
SEED_MODELS = tuple(SEED_KNOWN_MODELS) + tuple(SEED_BENEFIT_MODELS)

#: 模型目录缓存 1 小时；失败时保留上一轮。
CATALOG_TTL = 3600.0
#: 单个账号的模型发现最短间隔（避免每次 /v1/models 都打三路上游）。
CATALOG_MIN_INTERVAL = 60.0
#: 错误阈值与冷却。
ERR_THRESHOLD = 3
ERR_COOLDOWN = 600.0
SOFT_COOLDOWN = 60.0
#: token 提前刷新窗口。
REFRESH_SKEW = 1800.0
#: OAuth 会话 TTL。
OAUTH_TTL = 600.0
#: 错误体读取上限。
ERROR_BODY_MAX = 256 * 1024

DEFAULTS = {
    "enabled": False,
    "timeout_seconds": 30.0,
    "stream_timeout_seconds": 600.0,
    "models_ttl_seconds": 300.0,
    "max_concurrent": 1,
    "benefit_auto_claim": True,
    "credits_auto_claim": True,
}

_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class CodeArtsError(Exception):
    def __init__(self, message, status=None, code=None):
        super(CodeArtsError, self).__init__(message)
        self.status = status
        self.code = code


class CodeArtsUnavailable(CodeArtsError):
    pass


class CodeArtsAuthError(CodeArtsError):
    pass


class CodeArtsBadRequest(CodeArtsError):
    pass


class CodeArtsNotFound(CodeArtsError):
    pass


class CodeArtsConflict(CodeArtsError):
    pass


class CodeArtsUpstreamError(CodeArtsError):
    """上游 HTTP 200 但 SSE 内嵌业务错误（排队/TPM/模型未注册等）。"""


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def _positive_float(value, default, low, high):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float(default)
    return min(max(out, float(low)), float(high))


def _positive_int(value, default, low, high):
    try:
        out = int(value)
    except (TypeError, ValueError):
        return int(default)
    return min(max(out, int(low)), int(high))


def load_config(accounts_dir):
    """读取并收敛 CodeArts 配置；非法值回落默认，不阻塞网关启动。"""
    raw = wb_settings.load(accounts_dir).get("codearts")
    if not isinstance(raw, dict):
        raw = {}
    return {
        "enabled": raw.get("enabled") is True,
        "timeout_seconds": _positive_float(
            raw.get("timeout_seconds"), DEFAULTS["timeout_seconds"], 1.0, 600.0),
        "stream_timeout_seconds": _positive_float(
            raw.get("stream_timeout_seconds"),
            DEFAULTS["stream_timeout_seconds"], 1.0, 3600.0),
        "models_ttl_seconds": _positive_float(
            raw.get("models_ttl_seconds"), DEFAULTS["models_ttl_seconds"],
            60.0, 86400.0),
        "max_concurrent": _positive_int(
            raw.get("max_concurrent"), DEFAULTS["max_concurrent"], 1, 20),
        "benefit_auto_claim": raw.get("benefit_auto_claim", True) is not False,
        "credits_auto_claim": raw.get("credits_auto_claim", True) is not False,
        "accounts_dir": accounts_dir,
    }


def save_config(accounts_dir, patch):
    """保存配置；返回完整配置。非法值抛 ValueError。"""
    if not isinstance(patch, dict):
        raise ValueError("codearts settings must be an object")
    current = load_config(accounts_dir)
    merged = dict(current)
    for key in DEFAULTS:
        if key in patch:
            merged[key] = patch[key]
    merged["enabled"] = merged.get("enabled") is True
    merged["timeout_seconds"] = _positive_float(
        merged.get("timeout_seconds"), DEFAULTS["timeout_seconds"], 1.0, 600.0)
    merged["stream_timeout_seconds"] = _positive_float(
        merged.get("stream_timeout_seconds"),
        DEFAULTS["stream_timeout_seconds"], 1.0, 3600.0)
    merged["models_ttl_seconds"] = _positive_float(
        merged.get("models_ttl_seconds"), DEFAULTS["models_ttl_seconds"],
        60.0, 86400.0)
    merged["max_concurrent"] = _positive_int(
        merged.get("max_concurrent"), DEFAULTS["max_concurrent"], 1, 20)
    merged["benefit_auto_claim"] = merged.get("benefit_auto_claim", True) is not False
    merged["credits_auto_claim"] = merged.get("credits_auto_claim", True) is not False
    data = wb_settings.load(accounts_dir)
    data["codearts"] = {k: merged[k] for k in DEFAULTS}
    wb_settings.save(accounts_dir, data)
    return load_config(accounts_dir)


def configured(accounts_dir):
    return load_config(accounts_dir)["enabled"]


def masked_config(accounts_dir):
    cfg = load_config(accounts_dir)
    pool = pool_for(accounts_dir)
    out = {k: v for k, v in cfg.items() if k != "accounts_dir"}
    out["configured"] = bool(cfg["enabled"])
    out["accounts"] = len(pool.accounts) if pool else 0
    out["healthy"] = pool.stats()["healthy"] if pool else 0
    return out


# ---------------------------------------------------------------------------
# 账号文件与状态
# ---------------------------------------------------------------------------

def accounts_path(accounts_dir):
    return os.path.join(accounts_dir, "codearts")


def _auth_filename(uid):
    safe = str(uid or "").replace(os.sep, "_").replace("/", "_")
    return "codearts-%s.json" % (safe or "unknown")


def _atomic_write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_auths(accounts_dir):
    """读取 accounts/codearts/codearts-*.json。"""
    folder = accounts_path(accounts_dir)
    out = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return out
    for name in names:
        if not name.startswith("codearts-") or not name.endswith(".json"):
            continue
        path = os.path.join(folder, name)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if not data.get("cloud_dragon_token") or not data.get("user_id"):
            continue
        data["_path"] = path
        out.append(data)
    return out


def save_auth(accounts_dir, auth):
    path = auth.get("_path") or os.path.join(
        accounts_path(accounts_dir), _auth_filename(auth.get("user_id")))
    auth = dict(auth)
    auth.pop("_path", None)
    _atomic_write(path, auth)
    auth["_path"] = path
    return auth


def delete_auth(accounts_dir, uid):
    path = os.path.join(accounts_path(accounts_dir), _auth_filename(uid))
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False


def _parse_expiration(value):
    text = str(value or "").strip()
    if not text:
        return 0.0
    text = text.replace("Z", "+00:00")
    try:
        import datetime
        return datetime.datetime.fromisoformat(text).timestamp()
    except Exception:
        return 0.0


def _fmt_duration(seconds):
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, _ = divmod(rest, 60)
    if hours:
        return "%dh%dm" % (hours, minutes)
    return "%dm" % minutes


# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------

class CodeArtsAccount(object):
    """一个华为云 CodeArts 账号；也是 wb-hub 回程处理器需要的账号表面。"""

    uid = ""
    realm = "codearts"
    product = "codearts"
    proxy = None
    chat_path = EP_CHAT

    def __init__(self, auth, pool):
        self.auth = dict(auth)
        self.pool = pool
        self.uid = str(auth.get("user_id") or "")
        self.name = self.uid
        self.nickname = str(auth.get("user_name") or self.uid)
        # proxy_slot is the persisted binding; proxy is the runtime URL that
        # CodeArtsPool.apply_proxy_slots() derives from the current settings.
        self.proxy_slot = str(auth.get("proxySlot") or "").strip()
        self.proxy = ""
        self.disabled = False
        self.disabled_reason = ""
        self.cool_until = 0.0
        self.err_count = 0
        self.last_error = ""
        self.active_concurrent = 0
        self.max_concurrent = pool.config.get("max_concurrent", 1)
        self.last_used = 0.0
        self.models = []
        self.model_benefit = {}
        self.models_fetched_at = 0.0
        self.checkin = None
        self.info = None
        self.info_fetched_at = 0.0
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._models_lock = threading.Lock()
        self._info_lock = threading.Lock()

    def chat_base_url(self):
        return SNAP_HOST

    def model_disabled(self, mid):
        return False

    def is_cli_identity(self):
        return False

    # ---- 凭证 ----
    def credential(self):
        return {
            "access_key_id": self.auth.get("access_key_id") or "",
            "secret_access_key": self.auth.get("secret_access_key") or "",
            "security_token": self.auth.get("cloud_dragon_token") or "",
        }

    def expires_at(self):
        return _parse_expiration(self.auth.get("expiration"))

    def token_remaining(self):
        exp = self.expires_at()
        if not exp:
            return 0
        return max(0, exp - time.time())

    def expired(self):
        exp = self.expires_at()
        return bool(exp and exp <= time.time())

    def expiring_soon(self, skew=REFRESH_SKEW):
        exp = self.expires_at()
        return bool(exp and exp - time.time() <= skew)

    def update_auth(self, token):
        creds = token.get("credentials") or {}
        self.auth.update({
            "user_id": token.get("user_id") or self.uid,
            "user_name": token.get("user_name") or self.nickname,
            "domain_id": token.get("domain_id") or self.auth.get("domain_id") or "",
            "cloud_dragon_token": creds.get("security_token") or "",
            "access_key_id": creds.get("access_key_id") or "",
            "secret_access_key": creds.get("secret_access_key") or "",
            "expiration": creds.get("expiration") or "",
            "refresh_token": token.get("refresh_token") or self.auth.get("refresh_token") or "",
            "updated_at": int(time.time()),
        })
        self.auth = save_auth(self.pool.accounts_dir, self.auth)
        self.disabled = False
        self.disabled_reason = ""
        self.last_error = ""

    # ---- 状态快照 ----
    def view(self):
        now = time.time()
        cooling = (not self.disabled) and now < self.cool_until
        reason = self.disabled_reason or self.last_error
        info = self.info or {}
        credit_total = ((info.get("credits") or {}).get("total") or {})
        credits = None
        if credit_total:
            credits = {
                "remain": float(credit_total.get("remain") or 0),
                "amount": float(credit_total.get("amount") or 0),
                "used": float(credit_total.get("used") or 0),
                "expiring": float(credit_total.get("expiring") or 0),
                "unit": "credit",
                "package": info.get("package_name") or "",
                "end_date": info.get("end_date") or "",
            }
        return {
            "name": self.name,
            "uid": self.uid,
            "nickname": self.nickname,
            "user_name": self.nickname,
            "default_model": "",
            "credits": credits,
            "package": info.get("package_name") or "",
            "checkin": self.checkin,
            "disabled": self.disabled,
            "cooling": cooling,
            "until": (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.cool_until))
                      if cooling else ""),
            "err_count": self.err_count,
            "reason": reason,
            "last_error": self.last_error,
            "active_concurrent": self.active_concurrent,
            "max_concurrent": self.max_concurrent,
            "token_remaining": _fmt_duration(self.token_remaining()),
            "expires_at": (time.strftime("%Y-%m-%dT%H:%M:%S",
                                         time.localtime(self.expires_at()))
                           if self.expires_at() else ""),
            "models": len(self.models),
            "proxy_slot": self.proxy_slot,
            "proxy": self.proxy,
        }

    def healthy(self, now=None):
        """能否被选中。

        故意不检查 token 是否过期：过期账号仍要被选中，交给 `_ensure_token`
        刷新；把过期号直接过滤掉会让整池过期时一个号都选不到、也无法续期。
        """
        now = now or time.time()
        return (not self.disabled) and now >= self.cool_until

    def has_model(self, model):
        mid = str(model or "").lower()
        if not mid:
            return True
        if not self.models:
            return True  # 目录还没发现，先给它一次机会
        return any(str(m.get("id") or "").lower() == mid for m in self.models)

    def is_benefit(self, model):
        return bool(self.model_benefit.get(str(model or "").lower()))


# ---------------------------------------------------------------------------
# 账号池
# ---------------------------------------------------------------------------

class CodeArtsPool(object):
    def __init__(self, accounts_dir, config):
        self.accounts_dir = accounts_dir
        self.folder = accounts_path(accounts_dir)
        self.state_file = os.path.join(self.folder, "state.json")
        self.config = dict(config)
        self.accounts = []
        self._lock = threading.RLock()
        self.load()

    # ---- 持久化 ----
    def load(self):
        auths = load_auths(self.accounts_dir)
        state = self._load_state()
        accounts = []
        for auth in auths:
            acct = CodeArtsAccount(auth, self)
            entry = state.get(acct.uid) or {}
            acct.err_count = int(entry.get("err_count") or 0)
            acct.cool_until = float(entry.get("cool_until") or 0)
            acct.disabled = bool(entry.get("disabled"))
            acct.disabled_reason = str(entry.get("reason") or "")
            acct.last_error = str(entry.get("last_error") or "")
            if isinstance(entry.get("checkin"), dict):
                acct.checkin = entry["checkin"]
            accounts.append(acct)
        with self._lock:
            self.accounts = accounts
        self.apply_proxy_slots()

    def _load_state(self):
        try:
            with open(self.state_file, encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception:
            return {}
        if isinstance(raw, dict) and isinstance(raw.get("accounts"), list):
            raw = raw["accounts"]
        out = {}
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and item.get("name"):
                    out[str(item["name"])] = item
        return out

    def save_state(self):
        items = []
        with self._lock:
            accounts = list(self.accounts)
        for acct in accounts:
            items.append({
                "name": acct.uid,
                "err_count": acct.err_count,
                "cool_until": acct.cool_until,
                "disabled": acct.disabled,
                "reason": acct.disabled_reason,
                "last_error": acct.last_error,
                "checkin": acct.checkin,
            })
        try:
            _atomic_write(self.state_file, items)
        except Exception:
            pass

    # ---- 查询 ----
    def get(self, uid):
        with self._lock:
            for acct in self.accounts:
                if acct.uid == uid or acct.name == uid:
                    return acct
        return None

    def list(self):
        with self._lock:
            accounts = list(self.accounts)
        return [acct.view() for acct in sorted(accounts, key=lambda a: a.uid)]

    def apply_proxy_slots(self, slots=None):
        """Re-resolve every account's runtime `proxy` from its bound slot.

        Mirrors wb_accounts.AccountPool.apply_proxy_slots(): `proxySlot` is the
        persisted source of truth and `proxy` is derived here, so every outbound
        CodeArts call site can just use `acct.proxy`. A disabled or missing slot
        falls back to direct, while the binding stays on disk for re-enabling.
        """
        if slots is None:
            slots = wb_settings.proxy_slots(self.accounts_dir)
        by_id = {entry["id"]: entry for entry in slots if entry.get("enabled")}
        with self._lock:
            for acct in self.accounts:
                slot = by_id.get(acct.proxy_slot)
                acct.proxy = slot["url"] if slot else ""

    def stats(self):
        now = time.time()
        total = healthy = disabled = cooling = 0
        with self._lock:
            accounts = list(self.accounts)
        for acct in accounts:
            total += 1
            if acct.disabled:
                disabled += 1
            elif now < acct.cool_until:
                cooling += 1
            else:
                healthy += 1
        return {"total": total, "healthy": healthy, "disabled": disabled,
                "cooling": cooling}

    # ---- 增删启停 ----
    def add(self, auth):
        acct = CodeArtsAccount(auth, self)
        with self._lock:
            for index, existing in enumerate(self.accounts):
                if existing.uid == acct.uid:
                    # Re-login returns a fresh auth document without the
                    # panel-side binding. Keep the slot the operator chose.
                    if not acct.proxy_slot and existing.proxy_slot:
                        acct.proxy_slot = existing.proxy_slot
                    if acct.proxy_slot:
                        acct.auth["proxySlot"] = acct.proxy_slot
                    # _save_login() wrote the fresh token document before the
                    # pool saw it; persist the preserved binding here too.
                    acct.auth = save_auth(self.accounts_dir, acct.auth)
                    existing.auth = acct.auth
                    existing.proxy_slot = acct.proxy_slot
                    existing.nickname = acct.nickname
                    existing.disabled = False
                    existing.disabled_reason = ""
                    existing.last_error = ""
                    existing.err_count = 0
                    existing.cool_until = 0
                    existing.models_fetched_at = 0
                    self.accounts[index] = existing
                    self.save_state()
                    self.apply_proxy_slots()
                    return existing
            self.accounts.append(acct)
        self.save_state()
        self.apply_proxy_slots()
        return acct

    def remove(self, uid):
        acct = self.get(uid)
        if acct is None:
            return False
        with self._lock:
            self.accounts = [a for a in self.accounts if a.uid != uid]
        delete_auth(self.accounts_dir, uid)
        self.save_state()
        return True

    def enable(self, uid):
        acct = self.get(uid)
        if acct is None:
            return False
        acct.disabled = False
        acct.disabled_reason = ""
        acct.last_error = ""
        acct.err_count = 0
        acct.cool_until = 0
        self.save_state()
        return True

    def disable(self, uid, reason="manual disable"):
        acct = self.get(uid)
        if acct is None:
            return False
        acct.disabled = True
        acct.disabled_reason = reason
        acct.last_error = reason
        self.save_state()
        return True

    def clear_cooldown(self, uid):
        acct = self.get(uid)
        if acct is None:
            return False
        acct.cool_until = 0
        acct.err_count = 0
        self.save_state()
        return True

    # ---- 调度 ----
    def pick(self, model="", session_key=None, exclude=None, allowed_accounts=None):
        """按「能服务该模型 + 健康 + 并发未满」选一个账号。"""
        exclude = set(exclude or ())
        now = time.time()
        with self._lock:
            accounts = list(self.accounts)
        # 会话粘性：同一段对话优先回到上次的账号。
        if session_key:
            uid = self._affinity.get(session_key)
            if uid and uid not in exclude and (allowed_accounts is None or uid in allowed_accounts):
                acct = self.get(uid)
                if acct and acct.healthy(now) and acct.has_model(model) \
                        and acct.active_concurrent < acct.max_concurrent:
                    return acct
        candidates = [a for a in accounts
                      if a.uid not in exclude and (allowed_accounts is None or a.uid in allowed_accounts) and a.healthy(now)
                      and a.active_concurrent < a.max_concurrent]
        capable = [a for a in candidates if a.has_model(model)]
        pool = capable or candidates
        if not pool:
            return None
        pool.sort(key=lambda a: (a.active_concurrent, a.last_used or 0))
        return pool[0]

    _affinity = {}

    def bind_session(self, session_key, uid):
        if not session_key or not uid:
            return
        self._affinity[session_key] = uid
        if len(self._affinity) > 4096:
            # 简单上限：丢掉最早插入的一批。
            for key in list(self._affinity)[:1024]:
                self._affinity.pop(key, None)

    def acquire(self, acct):
        with acct._lock:
            if acct.active_concurrent >= acct.max_concurrent:
                return False
            acct.active_concurrent += 1
            acct.last_used = time.time()
            return True

    def release(self, acct):
        with acct._lock:
            if acct.active_concurrent > 0:
                acct.active_concurrent -= 1

    def note_success(self, acct):
        if acct.err_count or acct.cool_until or acct.last_error:
            acct.err_count = 0
            acct.cool_until = 0
            acct.last_error = ""
            self.save_state()

    def note_error(self, acct, message, soft=False, terminal=False):
        acct.last_error = str(message or "")[:200]
        if terminal:
            acct.disabled = True
            acct.disabled_reason = acct.last_error
        elif soft:
            acct.cool_until = time.time() + SOFT_COOLDOWN
        else:
            acct.err_count += 1
            if acct.err_count >= ERR_THRESHOLD:
                acct.cool_until = time.time() + ERR_COOLDOWN
                acct.err_count = 0
        self.save_state()


_POOLS = {}
_POOLS_LOCK = threading.Lock()


def pool_for(accounts_dir):
    """按 accounts_dir 取单例池；配置变化时刷新池配置。"""
    key = os.path.abspath(str(accounts_dir or "."))
    cfg = load_config(accounts_dir)
    with _POOLS_LOCK:
        pool = _POOLS.get(key)
        if pool is None:
            pool = CodeArtsPool(accounts_dir, cfg)
            _POOLS[key] = pool
        else:
            pool.config = cfg
            for acct in pool.accounts:
                acct.max_concurrent = cfg.get("max_concurrent", 1)
        return pool


def invalidate_pool(accounts_dir=None):
    with _POOLS_LOCK:
        if accounts_dir is None:
            _POOLS.clear()
        else:
            _POOLS.pop(os.path.abspath(str(accounts_dir)), None)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

_OPENER_CACHE = {}
_OPENER_LOCK = threading.Lock()
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _opener(proxy=""):
    """Build (and cache) an opener pinned to one proxy slot.

    Empty/blank means direct, and direct deliberately ignores the process
    environment proxies: a CodeArts account that is not bound to a slot must
    keep the pre-slot behaviour (no accidental HTTP_PROXY). One opener per
    proxy URL keeps each account's traffic on its own exit IP.
    """
    proxy = str(proxy or "").strip()
    if not proxy:
        return _DIRECT_OPENER
    with _OPENER_LOCK:
        opener = _OPENER_CACHE.get(proxy)
        if opener is None:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
            _OPENER_CACHE[proxy] = opener
        return opener


def _error_text(raw):
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return raw.decode("utf-8", "replace")[:500]
    if isinstance(parsed, dict):
        for key in ("error_msg", "message", "error_description", "error"):
            value = parsed.get(key)
            if isinstance(value, str) and value:
                return value[:500]
        code = parsed.get("error_code") or parsed.get("code")
        if code not in (None, "", 0, "0"):
            return "error_code=%s" % code
    return raw.decode("utf-8", "replace")[:500]


def _http(method, url, headers=None, body=None, timeout=30.0, proxy=""):
    """发一个普通 HTTP 请求，返回 (status, raw_bytes)。"""
    req = urllib.request.Request(url, data=body, headers=headers or {},
                                 method=method)
    try:
        with _opener(proxy).open(req, timeout=timeout) as resp:
            return resp.status, resp.read(ERROR_BODY_MAX * 4)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(ERROR_BODY_MAX)
        except Exception:
            raw = b""
        return exc.code, raw
    except (urllib.error.URLError, socket.timeout, TimeoutError,
            ConnectionError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise CodeArtsUnavailable(
            "连不上华为云 CodeArts：%s - 请检查本机网络或代理。" % reason)


def _json_http(method, url, headers=None, body=None, timeout=30.0, proxy=""):
    status, raw = _http(method, url, headers, body, timeout, proxy)
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        parsed = None
    if status >= 400:
        message = _error_text(raw)
        if status in (401, 403):
            raise CodeArtsAuthError(message, status=status)
        if status == 400:
            raise CodeArtsBadRequest(message, status=status)
        if status == 404:
            raise CodeArtsNotFound(message, status=status)
        raise CodeArtsError("华为云返回 HTTP %s：%s" % (status, message),
                            status=status)
    if not isinstance(parsed, dict):
        raise CodeArtsError("华为云返回了无法解析的响应：%s"
                            % raw[:200].decode("utf-8", "replace"))
    return parsed


def _normalize_token(payload):
    """把 oauth2/tokens 响应归一成统一结构。"""
    creds = payload.get("credentials") or {}
    legacy = payload.get("credential") or {}
    if not creds.get("security_token") and legacy.get("securitytoken"):
        creds = {
            "access_key_id": legacy.get("access") or "",
            "secret_access_key": legacy.get("secret") or "",
            "security_token": legacy.get("securitytoken") or "",
            "expiration": legacy.get("expires_at") or "",
        }
    return {
        "user_id": str(payload.get("user_id") or ""),
        "user_name": str(payload.get("user_name") or ""),
        "domain_id": str(payload.get("domain_id") or ""),
        "refresh_token": str(payload.get("refresh_token") or ""),
        "credentials": {
            "access_key_id": str(creds.get("access_key_id") or ""),
            "secret_access_key": str(creds.get("secret_access_key") or ""),
            "security_token": str(creds.get("security_token") or ""),
            "expiration": str(creds.get("expiration") or ""),
        },
    }


def request_token(form, dpop_jwk, timeout=30.0, proxy=""):
    url = STS_HOST + EP_OAUTH_TOKENS
    body = urllib.parse.urlencode(form).encode("utf-8")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "DPoP": crypto.dpop_proof(dpop_jwk, url),
    }
    payload = _json_http("POST", url, headers, body, timeout, proxy)
    token = _normalize_token(payload)
    if not token["credentials"]["security_token"]:
        raise CodeArtsError("登录响应缺少 security_token：%s"
                            % json.dumps(payload, ensure_ascii=False)[:300])
    return token


def exchange_code(code, verifier, port, dpop_jwk):
    form = {
        "client_id": CLIENT_ID,
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": "http://127.0.0.1:%d%s" % (port, REDIRECT_PATH),
    }
    return request_token(form, dpop_jwk)


def refresh_token_for(auth, timeout=30.0, proxy=""):
    jwk = auth.get("dpop_private_key")
    if not isinstance(jwk, dict) or not jwk.get("d"):
        jwk = crypto.jwk_from_keypair(*crypto.generate_keypair())
    form = {
        "client_id": str(auth.get("client_id") or CLIENT_ID),
        "code_verifier": str(auth.get("code_verifier") or ""),
        "grant_type": "refresh_token",
        "refresh_token": str(auth.get("refresh_token") or ""),
    }
    try:
        return request_token(form, jwk, timeout, proxy)
    except CodeArtsBadRequest as exc:
        message = str(exc)
        lowered = message.lower()
        if ("invalid_grant" in lowered or "expiredrefreshtoken" in lowered
                or "invaliddpopheader" in lowered
                or "invalid client id" in lowered
                or "has been used" in lowered):
            raise CodeArtsAuthError(
                "refresh_token 已失效或被拒，需要重新登录：%s" % message,
                status=exc.status)
        raise


def poll_ticket(ticket_id, secret, timeout=30.0):
    url = SNAP_MANAGER + EP_LOGIN_TICKET + "?" + urllib.parse.urlencode(
        {"ticket_id": ticket_id, "secret": secret})
    headers = {"plugin-name": PLUGIN_NAME, "plugin-version": PLUGIN_VERSION}
    payload = _json_http("GET", url, headers, None, timeout)
    return _normalize_token(payload)


# ---------------------------------------------------------------------------
# 华为云签名请求
# ---------------------------------------------------------------------------

def _signed_headers(acct, method, url, headers, body):
    cred = acct.credential()
    return crypto.sign_request(
        method, url, headers, body,
        cred["access_key_id"], cred["secret_access_key"],
        cred["security_token"])


def signed_get(acct, url, headers=None, timeout=30.0, agent_type=""):
    base = {"Content-Type": "application/json", "X-Language": "zh-cn"}
    if agent_type:
        base["Agent-Type"] = agent_type
    base.update(headers or {})
    signed = _signed_headers(acct, "GET", url, base, b"")
    return _json_http("GET", url, signed, None, timeout,
                      getattr(acct, "proxy", ""))


def signed_post(acct, url, payload, headers=None, timeout=30.0, agent_type=""):
    raw = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    base = {"Content-Type": "application/json", "X-Language": "zh-cn"}
    if agent_type:
        base["Agent-Type"] = agent_type
    base.update(headers or {})
    signed = _signed_headers(acct, "POST", url, base, raw)
    return _json_http("POST", url, signed, raw, timeout,
                      getattr(acct, "proxy", ""))


# ---------------------------------------------------------------------------
# 模型目录
# ---------------------------------------------------------------------------

def _canonical_model(acct, model):
    mid = str(model or "").strip()
    if not mid:
        return mid
    lower = mid.lower()
    for item in acct.models or ():
        if str(item.get("id") or "").lower() == lower:
            return str(item["id"])
    for seed in SEED_KNOWN_MODELS + SEED_BENEFIT_MODELS:
        if seed.lower() == lower:
            return seed
    static = {
        "snap-chat": "GLM-5.2", "glm-5.2": "GLM-5.2",
        "glm-5.1": "GLM-5.1", "glm-4.7": "GLM-4.7",
        "qwen3-vl-235b": "Qwen3-VL-235B",
        "qwen3.5-397b-a17b-vl": "Qwen3.5-397B-A17B-VL",
        "qwen3.6-27b-vl": "Qwen3.6-27B-VL",
    }
    return static.get(lower, mid)


def _merge_models(sets):
    merged = {}
    for source, items in sets:
        for item in items or []:
            mid = str(item.get("id") or "").strip()
            if not mid:
                continue
            key = mid.lower()
            current = merged.get(key)
            if current is None:
                merged[key] = {
                    "id": mid,
                    "name": str(item.get("name") or mid),
                    "context_window": int(item.get("context_window") or 0),
                    "max_tokens": int(item.get("max_tokens") or 0),
                    "desc": str(item.get("desc") or ""),
                    "benefit": bool(item.get("benefit")),
                }
                continue
            if not current.get("context_window") and item.get("context_window"):
                current["context_window"] = int(item.get("context_window"))
            if not current.get("max_tokens") and item.get("max_tokens"):
                current["max_tokens"] = int(item.get("max_tokens"))
            if item.get("benefit"):
                current["benefit"] = True
            if not current.get("desc") and item.get("desc"):
                current["desc"] = item["desc"]
    return list(merged.values())


def _fetch_agent_models(acct, timeout):
    url = SNAP_HOST + EP_AGENT_LIST + "?" + urllib.parse.urlencode(
        {"offset": 0, "limit": 100, "is_primary_agent": "true"})
    listing = signed_get(acct, url, timeout=timeout, agent_type="PromptCenter")
    agents = listing.get("agents") or []
    agent_id = ""
    for item in agents:
        alias = item.get("alias") or {}
        if (item.get("agent_name") == "CodeAgent"
                and alias.get("alias_zh_cn") == "智能体"
                and item.get("show_in_ide")):
            agent_id = str(item.get("agent_id") or "")
            break
    if not agent_id:
        for item in agents:
            if item.get("is_primary_agent"):
                agent_id = str(item.get("agent_id") or "")
                break
    if not agent_id and agents:
        agent_id = str(agents[0].get("agent_id") or "")
    if not agent_id:
        return []
    detail_url = SNAP_HOST + EP_AGENT_DETAIL + "?" + urllib.parse.urlencode(
        {"agent_id": agent_id})
    detail = signed_get(acct, detail_url, timeout=timeout,
                        agent_type="PromptCenter")
    models = ((detail.get("gpts") or {}).get("models") or [])
    out = []
    for item in models:
        params = item.get("model_parameters") or {}
        mid = (item.get("model_name") or item.get("model_id")
               or item.get("model_alias") or params.get("model_id") or "")
        if not mid:
            continue
        out.append({
            "id": str(mid),
            "name": str(mid),
            "context_window": int(params.get("context_window") or 0),
            "max_tokens": int(params.get("max_tokens") or 0),
            "desc": str(params.get("model_desc") or ""),
            "benefit": False,
        })
    return out


def _fetch_builtin_models(acct, timeout):
    data = signed_get(acct, SNAP_HOST + EP_MODEL_BUILTIN,
                      timeout=timeout, agent_type="PromptCenter")
    out = []
    for item in data.get("builtinModels") or []:
        mid = item.get("model_id") or item.get("model_name") or ""
        if not mid:
            continue
        out.append({
            "id": str(mid),
            "name": str(mid),
            "context_window": int(item.get("context_window") or 0),
            "max_tokens": int(item.get("max_tokens") or 0),
            "desc": str(item.get("model_desc") or ""),
            "benefit": False,
        })
    return out


def _claim_benefit(acct, timeout):
    try:
        signed_post(acct, BENEFIT_HOST + EP_BENEFIT_CLAIM, {},
                    timeout=timeout)
    except CodeArtsError:
        pass


def _fetch_benefit_models(acct, timeout, auto_claim=True):
    if auto_claim:
        _claim_benefit(acct, timeout)
    data = signed_get(acct, BENEFIT_HOST + EP_BENEFIT_CONFIG,
                      timeout=timeout)
    out = []
    result = data.get("result") or {}
    for item in result.get("models") or []:
        mid = item.get("model_id") or item.get("model_name") or ""
        if not mid:
            continue
        out.append({
            "id": str(mid),
            "name": str(item.get("model_name") or mid),
            "context_window": int(item.get("context_window") or 0),
            "max_tokens": int(item.get("max_tokens") or 0),
            "desc": str(item.get("model_desc") or ""),
            "benefit": True,
        })
    return out


def fetch_models(acct, timeout=20.0, auto_claim=True):
    """三路发现并写入账号目录；单路失败不影响其他路。

    三路互不依赖，并行跑：最坏等待从「3 × timeout」降到「1 × timeout」。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    sets = []
    errors = []
    tasks = {
        "agent": lambda: _fetch_agent_models(acct, timeout),
        "builtin": lambda: _fetch_builtin_models(acct, timeout),
        "benefit": lambda: _fetch_benefit_models(acct, timeout, auto_claim),
    }
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(fn): name for name, fn in tasks.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                items = future.result()
                if items:
                    sets.append((name, items))
            except CodeArtsError as exc:
                errors.append("%s: %s" % (name, exc))
            except Exception as exc:
                errors.append("%s: %s" % (name, exc))
    merged = _merge_models(sets)
    if not merged:
        # 全失败：保留旧目录；没有旧目录就用种子。
        if acct.models:
            return acct.models
        merged = _merge_models([("seed", [
            {"id": mid, "name": mid,
             "benefit": mid.lower() in {m.lower() for m in SEED_BENEFIT_MODELS}}
            for mid in SEED_MODELS])])
    acct.models = merged
    acct.model_benefit = {str(m["id"]).lower(): bool(m.get("benefit"))
                          for m in merged}
    acct.models_fetched_at = time.time()
    return merged


def ensure_models(acct, force=False, timeout=20.0):
    cfg = acct.pool.config
    ttl = float(cfg.get("models_ttl_seconds") or DEFAULTS["models_ttl_seconds"])
    with acct._models_lock:
        age = time.time() - (acct.models_fetched_at or 0)
        if acct.models and not force and age < min(ttl, CATALOG_TTL):
            return acct.models
        try:
            return fetch_models(acct, timeout=timeout,
                                auto_claim=cfg.get("benefit_auto_claim", True))
        except CodeArtsError:
            if acct.models:
                return acct.models
            raise


def model_entries(config):
    """所有账号目录的并集 + 种子；给 `/v1/models` 用。"""
    pool = pool_for(config["accounts_dir"])
    sets = []
    for acct in list(pool.accounts):
        try:
            _ensure_token(acct)
        except CodeArtsError:
            pass
        try:
            ensure_models(acct, timeout=8.0)
        except CodeArtsError:
            pass
        if acct.models:
            sets.append(("account", acct.models))
    merged = _merge_models(sets)
    if not merged:
        merged = _merge_models([("seed", [
            {"id": mid, "name": mid,
             "benefit": mid.lower() in {m.lower() for m in SEED_BENEFIT_MODELS}}
            for mid in SEED_MODELS])])
    now = int(time.time())
    out = []
    for item in merged:
        entry = {
            "id": item["id"],
            "object": "model",
            "created": now,
            "owned_by": "codearts",
            "provider": "codearts",
            "name": item.get("name") or item["id"],
        }
        if item.get("context_window"):
            entry["context_window"] = item["context_window"]
        if item.get("max_tokens"):
            entry["max_tokens"] = item["max_tokens"]
        if item.get("benefit"):
            entry["benefit"] = True
        out.append(entry)
    return out


def models_meta(config):
    pool = pool_for(config["accounts_dir"])
    stamps = [a.models_fetched_at for a in pool.accounts if a.models_fetched_at]
    return {
        "source": "codearts",
        "fetched_at": max(stamps) if stamps else None,
        "stale": False,
        "accounts": len(pool.accounts),
    }


def invalidate_models():
    with _POOLS_LOCK:
        pools = list(_POOLS.values())
    for pool in pools:
        for acct in pool.accounts:
            acct.models = []
            acct.model_benefit = {}
            acct.models_fetched_at = 0


def refresh_models(config):
    pool = pool_for(config["accounts_dir"])
    total = 0
    for acct in list(pool.accounts):
        try:
            ensure_models(acct, force=True, timeout=20.0)
            total += len(acct.models)
        except CodeArtsError:
            pass
    entries = model_entries(config)
    return {"ok": True, "models": len(entries),
            "catalog": models_meta(config)}


# ---------------------------------------------------------------------------
# 聊天
# ---------------------------------------------------------------------------

def _valid_chat_id(value):
    text = str(value or "").strip()
    return len(text) == 32 and all(c in "0123456789abcdefABCDEF" for c in text)


def _derive_chat_id(session_key, messages):
    import hashlib
    if session_key:
        return hashlib.sha256(str(session_key).encode("utf-8")).hexdigest()[:32]
    for item in messages or ():
        if isinstance(item, dict) and item.get("role") == "user":
            text = item.get("content")
            if isinstance(text, str) and text:
                return hashlib.sha256(("codearts:" + text).encode("utf-8")).hexdigest()[:32]
    return secrets.token_hex(16)


def _flatten_content(content):
    """把 OpenAI 内容分片压成纯文本；CodeArts 只消费 text 分片。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(part, dict) and (part.get("type") in ("image", "image_url", "input_image")
                                         or "image_url" in part) for part in content):
            raise CodeArtsBadRequest("CodeArts 出口暂不支持图片输入，请移除图片或选择其他出口")
        return "".join(str(part.get("text") or "")
                       for part in content if isinstance(part, dict))
    return str(content)


def build_chat_body(payload, session_key=None):
    """把客户端载荷收敛成 CodeArts `/api/v2/chat/completions` 的请求体。"""
    if not isinstance(payload, dict):
        raise CodeArtsBadRequest("request body must be a JSON object")
    from wb_proxy import (cleanup_orphan_tool_calls, merge_adjacent_tool_calls,
                          normalize_roles, repack_tool_result_blocks)
    # 只发 Go 版 chatBodyV2 会发的字段；客户端多出来的采样/格式字段一律丢掉，
    # 避免华为网关对未知字段返回 400。
    allowed = {
        "model", "messages", "temperature", "top_p", "max_tokens",
        "max_completion_tokens", "reasoning_effort", "tools", "tool_choice",
    }
    body = {str(k): v for k, v in payload.items() if str(k) in allowed}
    body["model"] = str(payload.get("model") or "GLM-5.2")
    messages = normalize_roles(payload.get("messages") or [])
    messages = [m for m in messages if isinstance(m, dict)]
    flattened = []
    for message in messages:
        if isinstance(message.get("content"), list):
            message = dict(message)
            message["content"] = _flatten_content(message.get("content"))
        flattened.append(message)
    messages = flattened
    messages, _ = merge_adjacent_tool_calls(messages)
    messages, _ = repack_tool_result_blocks(messages)
    messages, _ = cleanup_orphan_tool_calls(messages)
    if not messages:
        raise CodeArtsBadRequest("messages is empty")
    body["messages"] = messages
    # 指定函数名时 CodeArts 只认字符串 required：把工具清单收窄到该函数，
    # 再把 tool_choice 改成 required（与 Go 版 nativeTools 一致）。
    tools = body.get("tools")
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        function = choice.get("function") or {}
        name = function.get("name") if isinstance(function, dict) else ""
        if name and isinstance(tools, list):
            filtered = [tool for tool in tools
                        if isinstance(tool, dict)
                        and isinstance(tool.get("function"), dict)
                        and tool["function"].get("name") == name]
            if filtered:
                body["tools"] = filtered
                body["tool_choice"] = "required"
            else:
                body.pop("tool_choice", None)
        else:
            body.pop("tool_choice", None)
    if not body.get("tools"):
        body.pop("tool_choice", None)
    if body.get("max_completion_tokens") is not None and body.get("max_tokens") is None:
        body["max_tokens"] = body.pop("max_completion_tokens")
    chat_id = payload.get("conversation_id")
    if not _valid_chat_id(chat_id):
        chat_id = _derive_chat_id(session_key, messages)
    body["chat_id"] = chat_id
    body["prompt_cache_key"] = chat_id
    body["stream"] = True
    body["tool_stream"] = True
    return body


def _chat_headers(acct, chat_id, session_id, benefit):
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "x-auth-token": acct.auth.get("cloud_dragon_token") or "",
        "x-snap-traceid": secrets.token_hex(16),
        "X-Language": "zh-cn",
        "app-id": "CodeAgent3.0",
        "is_confidential": "false",
    }
    if benefit:
        headers[HEADER_MAAS_TYPE] = MAAS_BENEFIT
    return headers


class CodeArtsStream(object):
    """把华为云 SSE 包成 wb-hub 回程处理器能吃的行迭代器。

    wb-hub 的 `_chat_stream_response` / `aggregate_stream` 只认标准 OpenAI
    chunk（choices[].delta / finish_reason / usage）。这里把 CodeArts 的
    `text` 全文快照、`output` 终态和顶层 `delta` 都归一成标准 chunk。
    """

    def __init__(self, raw, model, acct, pool):
        self.raw = raw
        self.model = model
        self.acct = acct
        self.pool = pool
        self._wb_billed_model = model
        self._pending = b""
        self._done = False
        self._finish_sent = False
        self._last_text = ""
        self._usage = None
        self._created = int(time.time())

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def close(self):
        try:
            self.raw.close()
        except Exception:
            pass
        try:
            self.pool.release(self.acct)
        except Exception:
            pass

    def preflight(self):
        """读到首个 data 事件；HTTP 200 里内嵌的业务错误在这里抛出。"""
        while True:
            line = self.raw.readline()
            if not line:
                return
            stripped = line.strip()
            if not stripped.startswith(b"data:"):
                continue
            data = stripped[5:].strip()
            if not data:
                continue
            self._pending = line if line.endswith(b"\n") else line + b"\n"
            self._raise_embedded(data)
            return

    def _raise_embedded(self, data):
        text = data.decode("utf-8", "replace")
        if text == "[DONE]":
            return
        try:
            payload = json.loads(text)
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        code = payload.get("error_code") or payload.get("code")
        if code not in (None, "", 0, "0"):
            raise CodeArtsUpstreamError(
                str(payload.get("error_msg") or payload.get("message") or text)[:300],
                code=str(code))

    def _chunk(self, delta, finish=None):
        payload = {
            "id": "chatcmpl-codearts",
            "object": "chat.completion.chunk",
            "created": self._created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta or {},
                         "finish_reason": finish}],
        }
        return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")

    def _text_delta(self, text):
        if text is None:
            return b""
        text = str(text)
        if text == self._last_text:
            return b""
        if text.startswith(self._last_text):
            delta = text[len(self._last_text):]
        else:
            delta = text
        self._last_text = text
        return self._chunk({"content": delta}) if delta else b""

    def __iter__(self):
        while not self._done:
            if self._pending:
                line, self._pending = self._pending, b""
            else:
                line = self.raw.readline()
            if not line:
                break
            stripped = line.strip()
            if not stripped.startswith(b"data:"):
                continue
            data = stripped[5:].strip()
            if not data:
                continue
            text = data.decode("utf-8", "replace")
            if text == "[DONE]":
                self._done = True
                break
            self._raise_embedded(data)
            try:
                payload = json.loads(text)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            usage = payload.get("usage")
            if isinstance(usage, dict):
                self._usage = usage
            choices = payload.get("choices")
            if isinstance(choices, list) and choices:
                choice = choices[0] if isinstance(choices[0], dict) else {}
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    message = choice.get("message")
                    if isinstance(message, dict):
                        delta = {"role": message.get("role") or "assistant",
                                 "content": message.get("content")}
                        if message.get("tool_calls"):
                            delta["tool_calls"] = message["tool_calls"]
                    else:
                        delta = {}
                out = {
                    "id": payload.get("id") or "chatcmpl-codearts",
                    "object": "chat.completion.chunk",
                    "created": int(payload.get("created") or self._created),
                    "model": payload.get("model") or self.model,
                    "choices": [{
                        "index": choice.get("index", 0),
                        "delta": delta,
                        "finish_reason": choice.get("finish_reason"),
                    }],
                }
                yield ("data: " + json.dumps(out, ensure_ascii=False) + "\n\n").encode("utf-8")
                if choice.get("finish_reason"):
                    self._finish_sent = True
                continue
            # 顶层 delta（部分 CodeArts 事件）
            delta = payload.get("delta")
            if isinstance(delta, dict):
                yield self._chunk(delta)
                continue
            # 全文快照
            if "text" in payload:
                text_value = str(payload.get("text") or "")
                if text_value.strip() == "[DONE]" or text_value.strip().lower() == "done":
                    self._done = True
                    break
                chunk = self._text_delta(text_value)
                if chunk:
                    yield chunk
                continue
            # 终态 output 数组
            output = payload.get("output")
            if isinstance(output, list):
                text_out = "".join(
                    str(item.get("text") or "") for item in output
                    if isinstance(item, dict) and item.get("type") == "output_text")
                chunk = self._text_delta(text_out)
                if chunk:
                    yield chunk
        if not self._finish_sent:
            yield self._chunk({}, "stop")
        if self._usage:
            yield ("data: " + json.dumps({
                "id": "chatcmpl-codearts",
                "object": "chat.completion.chunk",
                "created": self._created,
                "model": self.model,
                "choices": [],
                "usage": self._usage,
            }, ensure_ascii=False) + "\n\n").encode("utf-8")
        yield b"data: [DONE]\n\n"


def _ensure_token(acct):
    if not acct.expiring_soon() and not acct.expired():
        return
    # refresh_token 是一次性的：同一账号并发刷新会让后一个请求拿到已消费的
    # token。串行化，并在拿到锁后重新判断（前一个请求可能已经刷好了）。
    with acct._refresh_lock:
        if not acct.expiring_soon() and not acct.expired():
            return
        try:
            token = refresh_token_for(acct.auth, proxy=acct.proxy)
        except CodeArtsAuthError as exc:
            acct.pool.note_error(acct, str(exc), terminal=True)
            raise
        except CodeArtsError:
            if acct.expired():
                raise
            return
        acct.update_auth(token)


def _is_concurrency_message(message):
    text = str(message or "").lower()
    return ("并发会话" in text or "tm.00001041" in text
            or "concurrent session" in text or "maximum number of active"
            in text)


def _client_chat(acct, body, session_key):
    model = _canonical_model(acct, body.get("model"))
    chat_id = body.get("chat_id") or _derive_chat_id(session_key, body.get("messages"))
    if not _valid_chat_id(chat_id):
        chat_id = _derive_chat_id(session_key, body.get("messages"))
    import hashlib
    session_id = hashlib.sha256(("codearts-session:" + chat_id).encode()).hexdigest()[:32]
    upstream_body = dict(body)
    upstream_body["model"] = model
    upstream_body["chat_id"] = chat_id
    upstream_body["prompt_cache_key"] = chat_id
    upstream_body["stream"] = True
    upstream_body["tool_stream"] = True
    raw = json.dumps(upstream_body, ensure_ascii=False).encode("utf-8")
    headers = _chat_headers(acct, chat_id, session_id, acct.is_benefit(model))
    headers = _signed_headers(acct, "POST", SNAP_HOST + EP_CHAT, headers, raw)
    # 与 Go 版一致：Chat-Id / Session-Id 在签名之后才加，不进入 SignedHeaders。
    headers["Chat-Id"] = chat_id
    headers["Session-Id"] = session_id
    timeout = float(acct.pool.config.get("stream_timeout_seconds")
                    or DEFAULTS["stream_timeout_seconds"])
    last_error = None
    for attempt in range(3):
        req = urllib.request.Request(SNAP_HOST + EP_CHAT, data=raw,
                                     headers=headers, method="POST")
        try:
            resp = _opener(acct.proxy).open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            try:
                err = exc.read(ERROR_BODY_MAX)
            except Exception:
                err = b""
            message = _error_text(err)
            if exc.code in (401, 403):
                raise CodeArtsAuthError(message, status=exc.code)
            if exc.code == 400 and _is_concurrency_message(message):
                last_error = CodeArtsUpstreamError(
                    message, status=429, code="TM.00001041")
                if attempt < 2:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise last_error
            if exc.code == 400:
                raise CodeArtsBadRequest(message, status=exc.code)
            if exc.code == 429:
                raise CodeArtsUpstreamError(message, status=exc.code, code="429")
            raise CodeArtsError("CodeArts 返回 HTTP %s：%s" % (exc.code, message),
                                status=exc.code)
        except (urllib.error.URLError, socket.timeout, TimeoutError,
                ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", None) or exc
            raise CodeArtsUnavailable("连不上 CodeArts 聊天接口：%s" % reason)
        stream = CodeArtsStream(resp, model, acct, acct.pool)
        try:
            stream.preflight()
        except CodeArtsUpstreamError as exc:
            stream.close()
            if _is_concurrency_message(str(exc)):
                last_error = exc
                if attempt < 2:
                    time.sleep(2.0 * (attempt + 1))
                    continue
            raise
        except Exception:
            stream.close()
            raise
        return stream, model
    raise last_error or CodeArtsUpstreamError("CodeArts 并发会话已满")


def open_chat(config, body, session_key=None, allowed_accounts=None):
    """选账号、续期、发现模型并打开 SSE；返回 (stream, account, model)。"""
    if not config.get("enabled"):
        raise CodeArtsBadRequest(
            "CodeArts 出口未启用：请到设置页打开「启用 CodeArts 出口」。")
    pool = pool_for(config["accounts_dir"])
    model = body.get("model") or ""
    exclude = set()
    last_error = None
    attempts = max(2, len(pool.accounts) + 1)
    for _ in range(attempts):
        acct = pool.pick(model, session_key=session_key, exclude=exclude,
                         allowed_accounts=allowed_accounts)
        if acct is None:
            break
        if not pool.acquire(acct):
            exclude.add(acct.uid)
            continue
        try:
            _ensure_token(acct)
            try:
                ensure_models(acct, timeout=8.0)
            except CodeArtsError:
                pass
            stream, used = _client_chat(acct, body, session_key)
            pool.note_success(acct)
            pool.bind_session(session_key, acct.uid)
            return stream, acct, used
        except CodeArtsBadRequest:
            pool.release(acct)
            raise
        except CodeArtsAuthError as exc:
            pool.note_error(acct, str(exc), terminal=True)
            pool.release(acct)
            exclude.add(acct.uid)
            last_error = exc
        except CodeArtsUpstreamError as exc:
            soft = (exc.status == 429 or "429" in str(exc.code or ""))
            pool.note_error(acct, str(exc), soft=soft)
            pool.release(acct)
            exclude.add(acct.uid)
            last_error = exc
        except CodeArtsUnavailable as exc:
            pool.note_error(acct, str(exc), soft=True)
            pool.release(acct)
            exclude.add(acct.uid)
            last_error = exc
        except CodeArtsError as exc:
            pool.note_error(acct, str(exc))
            pool.release(acct)
            exclude.add(acct.uid)
            last_error = exc
        except Exception:
            pool.release(acct)
            raise
    if last_error is not None:
        raise last_error
    reasons = []
    now = time.time()
    for acct in list(pool.accounts):
        if acct.disabled:
            reasons.append("%s 已停用：%s" % (acct.nickname, acct.disabled_reason or "手动停用"))
        elif acct.cool_until > now:
            reasons.append("%s 冷却中：%s" % (acct.nickname, acct.last_error or "稍后重试"))
    if reasons:
        raise CodeArtsUnavailable(
            "没有可用的 CodeArts 账号：" + "；".join(reasons[:2]))
    raise CodeArtsUnavailable(
        "没有可用的 CodeArts 账号：请在看板「账号」页添加并启用至少一个账号。")


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

_OAUTH_LOCK = threading.RLock()
_OAUTH_SESSIONS = {}
_OAUTH_TTL_SECONDS = OAUTH_TTL


class OAuthSession(object):
    def __init__(self, session_id, ticket_id, secret, verifier, dpop_jwk, port):
        self.id = session_id
        self.ticket_id = ticket_id
        self.secret = secret
        self.verifier = verifier
        self.dpop_jwk = dpop_jwk
        self.port = port
        self.created_at = time.time()
        self.status = "pending"
        self.message = "等待浏览器完成授权…"
        self.account = None
        self.ticket_fallback = False
        self.auth_url = ""

    def public(self):
        out = {
            "ok": self.status != "error",
            "status": self.status,
            "message": self.message,
            "session_id": self.id,
            "auth_url": self.auth_url,
        }
        if self.account:
            out["account"] = self.account
        return out


def _gc_oauth(now=None):
    now = now or time.time()
    for key, session in list(_OAUTH_SESSIONS.items()):
        if now - session.created_at > _OAUTH_TTL_SECONDS:
            _OAUTH_SESSIONS.pop(key, None)


def oauth_start(config, port=8788):
    _gc_oauth()
    ticket_id = crypto.random_hex(16)
    secret = crypto.random_hex(16)
    verifier, challenge = crypto.pkce_pair()
    dpop_jwk = crypto.jwk_from_keypair(*crypto.generate_keypair())
    session_id = secrets.token_hex(8)
    query = {
        "theme": "2",
        "locale": "zh-cn",
        "uri_scheme": CLIENT_ID,
        "client_id": CLIENT_ID,
        "port": str(int(port)),
        "code_challenge": challenge,
        "code_challenge_method": "SHA-256",
        "ticket_id": ticket_id,
        "plugin-name": PLUGIN_NAME,
        "plugin-version": PLUGIN_VERSION,
    }
    auth_url = PORTAL_HOST + "/authorize?" + urllib.parse.urlencode(query)
    session = OAuthSession(session_id, ticket_id, secret, verifier, dpop_jwk,
                           int(port))
    session.auth_url = auth_url
    with _OAUTH_LOCK:
        _OAUTH_SESSIONS[session_id] = session
    return {
        "ok": True,
        "session_id": session_id,
        "auth_url": auth_url,
        "expires_in": int(_OAUTH_TTL_SECONDS),
        "message": "请在浏览器打开授权链接，完成后等待自动检测",
    }


def _find_session(session_id=None, ticket_id=None, secret=None):
    _gc_oauth()
    with _OAUTH_LOCK:
        if session_id:
            return _OAUTH_SESSIONS.get(session_id)
        if secret:
            for session in _OAUTH_SESSIONS.values():
                if session.secret == secret and session.status == "pending":
                    return session
            return None
        if ticket_id:
            for session in _OAUTH_SESSIONS.values():
                if session.ticket_id == ticket_id and session.status == "pending":
                    return session
            return None
        pending = [s for s in _OAUTH_SESSIONS.values() if s.status == "pending"]
        pending.sort(key=lambda s: s.created_at, reverse=True)
        return pending[0] if pending else None


class _CredentialHolder(object):
    """只带 credential() 的轻量对象，给 signed_get 查身份用。"""

    def __init__(self, access_key_id, secret_access_key, security_token):
        self._cred = {
            "access_key_id": access_key_id or "",
            "secret_access_key": secret_access_key or "",
            "security_token": security_token or "",
        }

    def credential(self):
        return dict(self._cred)


def identity_from_refresh_token(refresh_token):
    """refresh_token 是 JWT 时取 sub；portal 回调不带身份时的兜底。"""
    parts = str(refresh_token or "").split(".")
    if len(parts) < 2:
        return ""
    try:
        payload = json.loads(crypto.b64url_decode(parts[1]).decode("utf-8"))
    except Exception:
        return ""
    return str(payload.get("sub") or "")


def lookup_identity(creds):
    """用刚拿到的 STS 凭证查身份：caller-identity → current/user。

    caller-identity 有时只给 principal_id/account_id，不给人名；此时继续查
    current/user 把昵称补上，避免账号表里显示一串 UID。
    """
    holder = _CredentialHolder(creds.get("access_key_id"),
                               creds.get("secret_access_key"),
                               creds.get("security_token"))
    first_uid = first_name = first_domain = ""
    try:
        data = signed_get(holder, STS_HOST + "/v5/caller-identity", timeout=10)
        first_uid = str(data.get("principal_id") or data.get("account_id") or "")
        urn = str(data.get("principal_urn") or "")
        first_name = urn.rsplit("/", 1)[-1] if "/" in urn else ""
        first_domain = str(data.get("account_id") or "")
        if first_uid and first_name and first_name != first_uid:
            return first_uid, first_name, first_domain
    except CodeArtsError:
        pass
    try:
        data = signed_get(holder, SNAP_MANAGER + "/v1/current/user", timeout=10)
        uid = str(data.get("user_id") or "")
        name = str(data.get("user_name") or "")
        domain = str(data.get("domain_id") or "")
        if uid or first_uid:
            return (first_uid or uid,
                    name or first_name,
                    domain or first_domain)
    except CodeArtsError:
        pass
    return first_uid, first_name, first_domain


def _save_login(config, session, token):
    uid = str(token.get("user_id") or "")
    name = str(token.get("user_name") or "")
    domain = str(token.get("domain_id") or "")
    creds = token.get("credentials") or {}
    if not uid or not name:
        # portal 回调只回 code，token 响应通常不带身份；用 STS 凭证补查。
        looked_uid, looked_name, looked_domain = lookup_identity(creds)
        uid = uid or looked_uid
        name = name or looked_name
        domain = domain or looked_domain
    if not uid:
        uid = identity_from_refresh_token(token.get("refresh_token") or "")
    if not uid:
        # 最后兜底：refresh_token 的稳定哈希，避免写出 codearts-unknown.json。
        import hashlib
        uid = "codearts-" + hashlib.sha256(
            str(token.get("refresh_token") or "").encode("utf-8")).hexdigest()[:12]
    if not name:
        name = uid
    auth = {
        "user_id": uid,
        "user_name": name,
        "domain_id": domain,
        "cloud_dragon_token": token["credentials"]["security_token"],
        "access_key_id": token["credentials"]["access_key_id"],
        "secret_access_key": token["credentials"]["secret_access_key"],
        "expiration": token["credentials"]["expiration"],
        "refresh_token": token.get("refresh_token") or "",
        "code_verifier": session.verifier,
        "client_id": CLIENT_ID,
        "dpop_private_key": session.dpop_jwk,
        "updated_at": int(time.time()),
    }
    auth = save_auth(config["accounts_dir"], auth)
    pool_for(config["accounts_dir"]).add(auth)
    session.status = "done"
    session.message = "登录成功：%s" % auth["user_name"]
    session.account = {"uid": uid, "nickname": auth["user_name"]}
    return auth


def _exchange_session(config, session, code):
    token = exchange_code(code, session.verifier, session.port,
                          session.dpop_jwk)
    return _save_login(config, session, token)


def oauth_poll(config, session_id):
    session = _find_session(session_id=session_id)
    if session is None:
        raise CodeArtsNotFound("会话不存在或已过期，请重新发起授权")
    if session.status in ("done", "error"):
        return session.public()
    if session.ticket_fallback:
        try:
            token = poll_ticket(session.ticket_id, session.secret)
        except CodeArtsError as exc:
            session.status = "error"
            session.message = str(exc)
            return session.public()
        if token.get("user_id") and token["credentials"]["security_token"]:
            _save_login(config, session, token)
        else:
            session.message = "等待浏览器完成登录…"
    return session.public()


def _loopback_host(host):
    return str(host or "").lower() in ("127.0.0.1", "localhost", "::1")


def _safe_oauth_redirect(url):
    try:
        target = urllib.parse.urlsplit(str(url or ""))
        portal = urllib.parse.urlsplit(PORTAL_HOST)
        return (target.scheme in ("http", "https") and target.scheme == portal.scheme
                and target.hostname == portal.hostname and target.port == portal.port
                and not target.username and not target.password)
    except ValueError:
        return False


def oauth_callback(config, session_id, callback_url):
    session = _find_session(session_id=session_id)
    if session is None:
        raise CodeArtsNotFound("会话不存在或已过期，请重新发起授权")
    parsed = urllib.parse.urlsplit(str(callback_url or "").strip())
    if (parsed.scheme != "http" or parsed.username or parsed.password
            or parsed.path != REDIRECT_PATH or not _loopback_host(parsed.hostname)):
        raise CodeArtsBadRequest("只接受华为返回的 127.0.0.1 OAuth 回调地址")
    if parsed.port and session.port and parsed.port != session.port:
        raise CodeArtsBadRequest("回调端口与当前授权会话不匹配")
    query = urllib.parse.parse_qs(parsed.query)
    code = (query.get("code") or [""])[0]
    secret = (query.get("secret") or [""])[0]
    redirect = (query.get("redirect") or [""])[0]
    if code:
        _exchange_session(config, session, code)
        return session.public()
    if secret and redirect:
        if not _safe_oauth_redirect(redirect):
            raise CodeArtsBadRequest("授权跳转地址不属于 CodeArts 门户")
        session.secret = secret
        session.ticket_fallback = True
        session.message = "回调已接收，继续等待授权结果…"
        return {
            "ok": True, "status": "continue", "next_url": redirect,
            "message": session.message,
        }
    raise CodeArtsBadRequest("回调地址缺少 code 或 secret")


def oauth_callback_get(config, query):
    """浏览器 GET /oauth/callback 的处理；返回 HTML。"""
    def value(key):
        raw = query.get(key)
        if isinstance(raw, list):
            return raw[0] if raw else ""
        return raw or ""
    ticket_id, callback_secret = value("ticket_id"), value("secret")
    if not ticket_id and not callback_secret:
        return 400, "<h3>回调缺少会话标识，请回到看板粘贴完整回调地址。</h3>"
    session = _find_session(ticket_id=ticket_id, secret=callback_secret)
    if session is None:
        return 400, "<h3>登录会话不存在或已过期，请回到看板重新发起。</h3>"
    code = value("code")
    secret = value("secret")
    redirect = value("redirect")
    try:
        if code:
            _exchange_session(config, session, code)
            return 200, "<h3>CodeArts 登录成功，可以关闭此页面。</h3>"
        if secret and redirect:
            if not _safe_oauth_redirect(redirect):
                raise CodeArtsBadRequest("授权跳转地址不属于 CodeArts 门户")
            session.secret = secret
            session.ticket_fallback = True
            session.message = "回调已接收，继续等待授权结果…"
            import html as html_module
            html = ('<meta http-equiv="refresh" content="0;url=%s">'
                    '<h3>正在继续华为云授权…</h3>' % html_module.escape(redirect, quote=True))
            return 200, html
    except CodeArtsError as exc:
        session.status = "error"
        session.message = str(exc)
        import html as html_module
        return 400, "<h3>登录失败：%s</h3>" % html_module.escape(str(exc))
    return 400, "<h3>回调地址缺少 code 或 secret。</h3>"


# ---------------------------------------------------------------------------
# 管理 API（本地账号池）
# ---------------------------------------------------------------------------

def _credit_metric(metrics, name):
    for item in metrics or ():
        if isinstance(item, dict) and item.get("name") == name:
            return {
                "amount": float(item.get("package_credit_amount") or 0),
                "used": float(item.get("package_credit_used") or 0),
                "remain": float(item.get("package_credit_remain") or 0),
                "expiring": float(item.get("package_credit_expiring_amount") or 0),
            }
    return None


def fetch_account_info(acct, timeout=15.0):
    """查账户/套餐/积分：`snap-manager/v1/statistics/plugin`。

    注意：`Agent-Type` / `X-Language` 必须在签名**之后**追加，不能进入
    SignedHeaders；否则华为网关会回 401 verify ak sk signature fail。
    """
    url = SNAP_MANAGER + EP_PACKAGE_INFO
    signed = _signed_headers(acct, "GET", url,
                             {"Content-Type": "application/json"}, b"")
    signed["Agent-Type"] = "PromptCenter"
    signed["X-Language"] = "zh-cn"
    data = _json_http("GET", url, signed, None, timeout, acct.proxy)
    package = data.get("package") or {}
    metrics = data.get("metrics") or []
    credits = {
        "total": _credit_metric(metrics, "usageTotalPackageCredit"),
        "basic": _credit_metric(metrics, "usageBasicPackageCredit"),
        "on_demand": _credit_metric(metrics, "usageOnDemandPackageCredit"),
        "bonus": _credit_metric(metrics, "usageBonusPackageCredit"),
        "unit": "credit",
    }
    models = []
    for item in data.get("model_metrics") or []:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("model_id") or "")
        if not mid:
            continue
        models.append({
            "id": mid,
            "name": str(item.get("model_name") or mid),
            "description": str(item.get("model_desc") or ""),
            "type": str(item.get("model_type") or ""),
            "enabled": item.get("enable") is not False,
            "status": str(item.get("status") or ""),
            "luxury": bool(item.get("is_luxury")),
        })
    info = {
        "is_credit_package": package.get("is_credit_package") is True,
        "is_token_package": package.get("is_token_package") is True,
        "spec_code": str(package.get("spec_code") or ""),
        "package_name": str(package.get("package_name_cn")
                            or package.get("package_name_en") or ""),
        "package_status": str(package.get("status") or ""),
        "start_date": str(data.get("start_date") or ""),
        "end_date": str(data.get("end_date") or ""),
        "credits": credits,
        "models": models,
        "fetched_at": time.time(),
    }
    acct.info = info
    acct.info_fetched_at = time.time()
    return info


def ensure_account_info(acct, force=False, timeout=15.0):
    if acct.info and not force and time.time() - acct.info_fetched_at < 60.0:
        return acct.info
    with acct._info_lock:
        if acct.info and not force and time.time() - acct.info_fetched_at < 60.0:
            return acct.info
        try:
            return fetch_account_info(acct, timeout=timeout)
        except CodeArtsError:
            if acct.info:
                return acct.info
            raise


_health_cache = {"at": 0.0, "value": {}}


def overview(config, force=False):
    pool = pool_for(config["accounts_dir"])
    for acct in list(pool.accounts):
        try:
            ensure_account_info(acct, timeout=8.0)
        except CodeArtsError:
            pass
    stats = pool.stats()
    credit_remain = credit_total = credit_used = 0.0
    for acct in pool.accounts:
        total = ((acct.info or {}).get("credits") or {}).get("total")
        if isinstance(total, dict):
            credit_remain += float(total.get("remain") or 0)
            credit_total += float(total.get("amount") or 0)
            credit_used += float(total.get("used") or 0)
    stats["credits"] = round(credit_remain, 2)
    stats["credits_total"] = round(credit_total, 2)
    stats["credits_used"] = round(credit_used, 2)
    accounts = pool.list()
    models = []
    seen = set()
    for acct in pool.accounts:
        infos = (acct.info or {}).get("models") or []
        if infos:
            for item in infos:
                mid = str(item.get("id") or "")
                key = mid.lower()
                if key and key not in seen:
                    seen.add(key)
                    models.append({
                        "id": mid,
                        "name": item.get("name") or mid,
                        "description": item.get("description") or "",
                    })
        else:
            for item in acct.models or ():
                mid = str(item.get("id") or "")
                key = mid.lower()
                if key and key not in seen:
                    seen.add(key)
                    models.append({"id": mid})
    if not models:
        models = [{"id": mid} for mid in SEED_MODELS]
    return {
        "service": "codearts",
        "region": "cn",
        "online": bool(config.get("enabled")),
        "base_url": SNAP_HOST,
        "stats": stats,
        "accounts": accounts,
        "models": models,
        "schedule": {"watch": {"enabled": False}},
    }


def health(config, force=False):
    now = time.time()
    if not force and now - _health_cache["at"] < 5.0:
        return dict(_health_cache["value"])
    pool = pool_for(config["accounts_dir"])
    stats = pool.stats()
    value = {
        "ok": bool(config.get("enabled")),
        "configured": bool(config.get("enabled")),
        "accounts": stats["total"],
        "healthy": stats["healthy"],
        "base_url": SNAP_HOST,
    }
    _health_cache.update({"at": now, "value": value})
    return dict(value)


def invalidate_overview():
    _health_cache["at"] = 0.0


def configured_config(config):
    return bool(config and config.get("enabled"))


def enable(config, uid):
    if not pool_for(config["accounts_dir"]).enable(uid):
        raise CodeArtsNotFound("account not found")
    invalidate_overview()
    return {"ok": True, "message": "已启用 %s" % uid}


def disable(config, uid, reason="manual disable"):
    if not pool_for(config["accounts_dir"]).disable(uid, reason):
        raise CodeArtsNotFound("account not found")
    invalidate_overview()
    return {"ok": True, "message": "已停用 %s" % uid}


def remove(config, uid):
    if not pool_for(config["accounts_dir"]).remove(uid):
        raise CodeArtsNotFound("account not found")
    invalidate_overview()
    return {"ok": True, "deleted": True, "message": "已删除 %s" % uid}


def clear_cooldown(config, uid):
    if not pool_for(config["accounts_dir"]).clear_cooldown(uid):
        raise CodeArtsNotFound("account not found")
    invalidate_overview()
    return {"ok": True, "message": "已清冷却 %s" % uid}


def apply_proxy_slots(accounts_dir, slots=None):
    """Re-resolve CodeArts account exits after the panel saves slots."""
    pool_for(accounts_dir).apply_proxy_slots(slots)


def set_proxy_slot(config, uid, slot_id):
    """Bind one CodeArts account to a proxy slot (empty string = direct)."""
    pool = pool_for(config["accounts_dir"])
    acct = pool.get(uid)
    if acct is None:
        raise CodeArtsNotFound("account not found")
    slot_id = str(slot_id or "").strip()
    acct.proxy_slot = slot_id
    acct.auth["proxySlot"] = slot_id
    acct.auth = save_auth(pool.accounts_dir, acct.auth)
    pool.apply_proxy_slots()
    invalidate_overview()
    return {
        "ok": True,
        "uid": acct.uid,
        "proxy_slot": acct.proxy_slot,
        "proxy": acct.proxy,
        "message": "出口已更新",
    }


def drop_missing_bindings(accounts_dir, slots):
    """Clear CodeArts bindings that point at a slot which no longer exists."""
    valid = {entry["id"] for entry in slots}
    pool = pool_for(accounts_dir)
    changed = 0
    for acct in list(pool.accounts):
        if acct.proxy_slot and acct.proxy_slot not in valid:
            acct.proxy_slot = ""
            acct.auth.pop("proxySlot", None)
            acct.auth = save_auth(accounts_dir, acct.auth)
            changed += 1
    # Always re-resolve: a surviving slot may have had its URL edited.
    pool.apply_proxy_slots(slots)
    return changed


def keepalive(config):
    pool = pool_for(config["accounts_dir"])
    refreshed = 0
    for acct in list(pool.accounts):
        if acct.disabled:
            continue
        try:
            if acct.expiring_soon() or acct.expired():
                _ensure_token(acct)
                refreshed += 1
        except CodeArtsError:
            pass
    return {"ok": True, "refreshed": refreshed,
            "message": "已检查 %d 个账号，续期 %d 个" % (len(pool.accounts), refreshed)}


def _credit_request(acct, method, path, payload=None, timeout=30.0):
    url = SNAP_HOST + path
    if method == "GET":
        return signed_get(acct, url, timeout=timeout, agent_type="PromptCenter")
    return signed_post(acct, url, payload or {}, timeout=timeout,
                       agent_type="PromptCenter")


def checkin_account(acct, timeout=30.0):
    data = _credit_request(acct, "GET",
                           EP_CREDIT_DELIVERY + "?channel=IDE",
                           timeout=timeout)
    items = ((data.get("data") or {}).get("items") or [])
    results = []
    total_credits = 0.0
    for item in items:
        campaign_id = item.get("campaignId")
        if campaign_id not in (1, 4):
            continue
        status = str(item.get("status") or "").strip()
        title = str(item.get("title") or ("积分活动 %s" % campaign_id))
        unit = item.get("benefitUnit")
        if unit and unit != "CREDIT":
            continue
        if status in ("CONFIRMED", "CONSUMED"):
            results.append({"campaign_id": campaign_id, "title": title,
                            "ok": True, "status": status, "message": "已领取"})
            continue
        if status == "ELIGIBLE":
            if not item.get("claimable"):
                results.append({"campaign_id": campaign_id, "title": title,
                                "ok": False, "status": status,
                                "message": "暂不可领取，稍后重试"})
                continue
            _credit_request(acct, "POST", EP_CREDIT_CLAIM, {
                "campaignId": campaign_id, "channel": "IDE",
                "idempotentKey": "claim_%s_%d" % (campaign_id, int(time.time() * 1000)),
            }, timeout=timeout)
            credits = float(item.get("pendingTotalAmount")
                            or item.get("benefitAmount") or 0)
            total_credits += credits
            status = "CLAIMED"
        if status in ("CLAIMED", "ELIGIBLE"):
            _credit_request(acct, "POST", EP_CREDIT_CONFIRM,
                            {"campaignId": campaign_id}, timeout=timeout)
            results.append({"campaign_id": campaign_id, "title": title,
                            "ok": True, "status": "CONFIRMED",
                            "credits": total_credits,
                            "message": "领取成功（%g 积分）" % total_credits
                            if total_credits else "已补确认"})
        elif status not in ("CONFIRMED", "CONSUMED"):
            results.append({"campaign_id": campaign_id, "title": title,
                            "ok": False, "status": status,
                            "message": "未获得活动资格"})
    message = "; ".join("%s: %s" % (r["title"], r["message"]) for r in results)
    if not message:
        message = "无可领取的签到或新手活动"
    result = {"ok": all(r.get("ok") for r in results) if results else True,
              "credits": total_credits, "message": message,
              "campaigns": results}
    acct.checkin = {"ok": result["ok"], "message": message,
                    "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    acct.info_fetched_at = 0.0
    acct.pool.save_state()
    return result


def checkin(config, uid=""):
    pool = pool_for(config["accounts_dir"])
    if uid:
        acct = pool.get(uid)
        if acct is None:
            raise CodeArtsNotFound("account not found")
        _ensure_token(acct)
        return checkin_account(acct)
    results = []
    for acct in list(pool.accounts):
        if acct.disabled:
            continue
        try:
            _ensure_token(acct)
            results.append({"uid": acct.uid, **checkin_account(acct)})
        except CodeArtsError as exc:
            results.append({"uid": acct.uid, "ok": False, "message": str(exc)})
    total = sum(float(r.get("credits") or 0) for r in results)
    return {"ok": all(r.get("ok") for r in results) if results else True,
            "credits": total,
            "message": "已处理 %d 个账号，共领取 %g 积分" % (len(results), total),
            "results": results}
