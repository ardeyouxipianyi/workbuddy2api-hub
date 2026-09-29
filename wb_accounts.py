import re
import base64
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from wb_fingerprint import derive_id, generate_request_id
import wb_atrest
import wb_identity
import wb_settings
import wb_webagent

# ---------------------------------------------------------------------------
# 网页版通道（issue #75 / #59 / #90）
#
# 网页版 app 的「对话」不是 chat/completions，而是 console/as 下的 agent 会话。
# 这条链路只用 Authorization: Bearer <accessToken> 与 X-User-Id 两个凭据头，
# 没有桌面端的 X-IDE-* 指纹，所以网关手里同一份账号凭据可以直接用。
#
# 关键的一步（issue #90 实测）：建会话只是**排队**。agent 要等客户端接上这条
# 会话的沙箱（GET .../{id}/session 返回的 link + token）并请求这一轮才会跑，
# 否则会话永远停在 CREATING、没有任何输出，也就不算一次有效对话。网页端的顺序
# 是 建会话 → 取 session → ACP over HTTP+SSE 的 initialize / session/load /
# session/prompt（实现见 wb_webagent）。
#
# 每日活跃奖励认的是「跑完的 agent 会话」：桌面身分发 chat/completions 不计数
# （issue #75、#59 实测），只建会话不接沙箱同样不计数（#90 实测）。因此国际版
# 打卡在桌面端对话之外，再走一次这条网页通道，并且把它跑到 completed。
# ---------------------------------------------------------------------------
WEB_ORIGIN = "https://www.workbuddy.ai"
WEB_CONVERSATIONS_URL = WEB_ORIGIN + "/console/as/conversations/"
WEB_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0")
DAILY_CHAT_MODEL = "deepseek-v4.1-flash"
DAILY_CHAT_WEB_PROMPT = "Hi"
#: 网页通道一轮最多等多久（秒）。实测一次「Hi」十几秒就跑完，留足余量。
WEB_TURN_TIMEOUT = int(os.environ.get("WB_WEB_TURN_TIMEOUT") or "120")


def _retryable(exc):
    """Transient network faults worth another attempt (TLS resets, timeouts, 5xx)."""
    if isinstance(exc, ssl.SSLError):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, (TimeoutError, ConnectionResetError, ConnectionAbortedError, OSError)):
        return True
    return False


_OPENER_CACHE = {}
_OPENER_LOCK = threading.Lock()


def opener_for_proxy(proxy):
    """Build (and cache) a urllib opener bound to one outbound proxy.

    Empty/blank means "direct", signalled by None so callers can fall back to
    the process default opener. One opener per account keeps each account's
    traffic pinned to its own exit IP instead of sharing a rotating pool.
    """
    proxy = str(proxy or "").strip()
    if not proxy:
        return None
    with _OPENER_LOCK:
        opener = _OPENER_CACHE.get(proxy)
        if opener is None:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
            _OPENER_CACHE[proxy] = opener
        return opener


def urlopen(req, timeout=30, proxy=""):
    """urlopen honouring an optional per-account proxy."""
    opener = opener_for_proxy(proxy)
    if opener is None:
        return urllib.request.urlopen(req, timeout=timeout)
    return opener.open(req, timeout=timeout)


def http_json(url, data=None, method=None, headers=None, timeout=30,
              retries=3, backoff=1.0, log=None, proxy=""):
    """urlopen + json decode with retries.

    Chinese networks and CDN edges routinely drop a TLS handshake with
    "SSL: UNEXPECTED_EOF_WHILE_READING"; a single retry almost always
    succeeds, so every upstream call goes through here.
    """
    attempts = max(1, int(retries or 1))
    last = None
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
            url,
            data=data,
            method=method or ("POST" if data is not None else "GET"),
            headers=headers or {},
        )
        try:
            with urlopen(req, timeout=timeout, proxy=proxy) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            if attempt >= attempts or not _retryable(exc):
                break
            if log:
                log("network retry %d/%d after %s" % (attempt, attempts, exc))
            time.sleep(backoff * attempt)
    raise last

REALM_CONFIGS = {
    "intl": {
        "name": "国际版 (Global)",
        "chat_upstream": "https://www.workbuddy.ai",
        "billing_upstream": "https://www.workbuddy.ai",
        "origin": "https://www.workbuddy.ai",
        "domain": "www.workbuddy.ai",
        "chat_ua": "WorkBuddy/5.5.2 WorkBuddy AI/5.5.2 CLI/5.5.2",
        "billing_ua": "WorkBuddy/5.5.2",
        "info_filename": "workbuddy-desktop-ai.info",
        "cache_dir_name": ".workbuddy-ai",
        "has_checkin": False,
    },
    "cn": {
        "name": "国内版 (China)",
        "chat_upstream": "https://copilot.tencent.com",
        "billing_upstream": "https://www.codebuddy.cn",
        "origin": "https://www.codebuddy.cn",
        "domain": "copilot.tencent.com",
        "chat_ua": "WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1",
        "billing_ua": "WorkBuddy/5.5.6",
        "info_filename": "workbuddy-desktop.info",
        "cache_dir_name": ".workbuddy",
        "has_checkin": True,
    },
}

AUTH_STATE_PATH = "/v2/plugin/auth/state"
AUTH_TOKEN_PATH = "/v2/plugin/auth/token"
LOGIN_ACCOUNT_PATH = "/v2/plugin/login/account"
REFRESH_PATH = "/v2/plugin/auth/token/refresh"
CHECKIN_PATH = "/v2/billing/meter/daily-checkin"
GET_RESOURCE_PATH = "/v2/billing/meter/get-user-resource"

LOGIN_PENDING = 11217
LOGIN_TTL_SECONDS = 600
USER_AGENT = REALM_CONFIGS['intl']['chat_ua']
DEFAULT_UA_VERSION = '5.5.2'

def get_realm_config(realm):
    return REALM_CONFIGS.get(realm) or REALM_CONFIGS["intl"]

def _jwt_claims(token):
    try:
        segment = str(token).split(".")[1]
        segment += "=" * (-len(segment) % 4)
        return json.loads(base64.urlsafe_b64decode(segment))
    except Exception:
        return {}

def jwt_exp(token):
    try:
        return int(_jwt_claims(token).get("exp") or 0)
    except Exception:
        return 0

def jwt_uid(token):
    return str(_jwt_claims(token).get("sub") or "")

def jwt_issuer(token):
    return str(_jwt_claims(token).get("iss") or "")

def normalize_epoch(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if number > 1e11:
        number /= 1000.0
    return int(number)

#: token / 域名里能认出区域的字样。国内版历史上换过出口：copilot.tencent.com →
#: codebuddy.cn → workbuddy.cn（新版国内客户端的 JWT issuer 就是
#: https://www.workbuddy.cn/…），三个都得认；只认前两个会把 workbuddy.cn 的
#: 国内账号判成国际版——手动导入 JSON 时"明明是国内的却进了国际版"就是这么来的。
CN_REALM_MARKERS = ("copilot.tencent.com", "codebuddy.cn", "workbuddy.cn")
INTL_REALM_MARKERS = ("workbuddy.ai", "codebuddy.ai")
REALM_MARKERS = {"cn": CN_REALM_MARKERS, "intl": INTL_REALM_MARKERS}

def realm_evidence(token, domain=None):
    """从 token / 域名里看区域，看不出返回 None（不要瞎猜成 intl）。

    先看 token 自己的 issuer，再看域名：域名是客户端随手记下来的字段，可能过期
    或干脆是另一边的（同一台机器切区登录过就会这样），token 才是要拿去请求的
    那一串，冲突时以它为准。
    """
    iss = jwt_issuer(token).lower()
    if any(marker in iss for marker in CN_REALM_MARKERS):
        return "cn"
    if any(marker in iss for marker in INTL_REALM_MARKERS):
        return "intl"
    dom = str(domain or "").lower()
    if any(marker in dom for marker in CN_REALM_MARKERS):
        return "cn"
    if any(marker in dom for marker in INTL_REALM_MARKERS):
        return "intl"
    return None

def detect_realm_from_token(token, domain=None):
    return realm_evidence(token, domain) or "intl"

def domain_for_realm(realm, domain):
    """把域名对齐到区域：域名写着另一个区域时换成该区域的规范域名。

    出站请求的 X-Domain 头跟着它走，区域既然以 token 为准，就不能让一个过期
    域名再把请求带回另一边的出口。域名本身看不出区域时原样保留。
    """
    dom = str(domain or "").strip()
    other = "intl" if realm == "cn" else "cn"
    if dom and not any(marker in dom.lower() for marker in REALM_MARKERS[other]):
        return dom
    return get_realm_config(realm)["domain"]

def desktop_effective_realm(hint, token, domain=None):
    """桌面凭据归哪个区域：token / 域名说了算，文件名只作兜底。

    文件名（workbuddy-desktop.info → 国内、workbuddy-desktop-ai.info → 国际）
    只是客户端两套安装的默认约定：同一个文件里完全可能登录另一个区域的账号
    （切区登录就是这么用的）。按 token 判定才不会把国内账号塞进国际版列表——
    那种账号导入后每个请求都会打到错区域的出口上。
    """
    evidence = realm_evidence(token, domain)
    if evidence:
        return evidence
    return hint if hint in ("intl", "cn") else "intl"

class Account(object):
    def __init__(self, data, path=None):
        data = data or {}
        self.path = path
        token = str(data.get("accessToken") or "")
        self.uid = str(data.get("uid") or jwt_uid(token))
        # The CN desktop build stores its nickname as an encrypted envelope
        # ({"$wbEncrypted": ...}) rather than plain text. Stringifying that would
        # paint an entire dictionary into the account row, so anything that is not
        # a plain string is dropped and the uid prefix is shown instead.
        raw_nickname = data.get("nickname")
        if isinstance(raw_nickname, str) and "$wbEncrypted" not in raw_nickname:
            self.nickname = raw_nickname.strip()
        else:
            self.nickname = ""
        self.domain = str(data.get("domain") or "")
        self.realm = str(data.get("realm") or detect_realm_from_token(token, self.domain))
        if not self.domain:
            self.domain = get_realm_config(self.realm)["domain"]
        self.platform = str(data.get("platform") or "CLI")
        # 出站身分讀回憑證檔裡保存的值：面板手動切換與 429 自動切換都會經由
        # save() 寫進憑證檔（to_dict() 序列化的是當下身分），所以重啟後接著用
        # 上次實際生效的那條通道，而不是每次都回到預設。
        # 憑證檔沒有這個欄位、或值不合法時 normalize_product() 會回退到
        # WorkBuddy 獨立桌面端 (workbuddy)，升級前就已存在的帳號行為不變。
        self.product = wb_identity.normalize_product(data.get("product"))
        self.enterprise_id = str(data.get("enterpriseId") or "")
        self.access_token = token
        self.refresh_token = str(data.get("refreshToken") or "")
        self.expires_at = normalize_epoch(data.get("expiresAt")) or jwt_exp(token)
        self.added_at = data.get("addedAt") or time.time()
        self.source = str(data.get("source") or "oauth")
        self.proxy_slot = str(data.get("proxySlot") or "").strip()
        self.proxy_legacy = str(data.get("proxy") or "").strip()
        # Runtime-resolved value; recomputed by AccountPool.apply_proxy_slots().
        self.proxy = self.proxy_legacy
        self.enabled = data.get("enabled", True)
        self.last_error = str(data.get("lastError") or "")
        self.cooldown_until = float(data.get("cooldownUntil") or 0)
        # Per-model throttling. Upstream rate limits (code 6004 "usage exceeds
        # frequency limit") apply to ONE model for one account, not to the whole
        # account: other models keep working. Cooldown the offending model only,
        # otherwise a single throttled model blackholes every request on the pool.
        # Deliberately runtime-only (not persisted): see VOLATILE_FIELDS.
        self.model_cooldowns = {}
        self.credits = data.get("credits") or None
        self.last_checkin = data.get("lastCheckin") or None
        self.last_daily_chat = data.get("lastDailyChat") or None
        # Low-credit guard: once the balance reaches this level the account
        # stops being handed out, so it never drops to zero (a zero balance is
        # what makes the upstream start sending nagging SMS). Resolved from the
        # global setting by AccountPool.apply_reserve_credits(); 0 disables it.
        self.reserve_credits = 0
        # Daily token guard: an account that already burned this many tokens
        # today stops being handed out, so a client that would burn the rest
        # of the day's quota rotates to another account instead of hitting
        # the upstream wall. Resolved from the global setting by
        # AccountPool.apply_daily_token_limit(); 0 disables it.
        # daily_tokens_today stays None until the proxy has folded the usage
        # log at least once, so a fresh process never parks anyone on an
        # unknown count.
        self.daily_token_limit = 0
        self.daily_tokens_today = None
        # Serialise token refresh and file writes. Request threads, /health,
        # dashboard polls and the scheduler can all reach refresh()/save() for
        # the same account at once; without a lock the upstream rotates the
        # refresh token concurrently and the last writer wins, so a freshly
        # minted token can be overwritten by a stale snapshot.
        self._refresh_lock = threading.Lock()
        self._save_lock = threading.Lock()
        # The dashboard snapshots this state while request threads update it.
        # Keep it separate from _refresh_lock, which spans network requests.
        self._throttle_lock = threading.Lock()

    def to_dict(self):
        return {
            "uid": self.uid,
            "nickname": self.nickname,
            "domain": self.domain,
            "realm": self.realm,
            "platform": self.platform,
            "product": self.product,
            "enterpriseId": self.enterprise_id,
            "accessToken": self.access_token,
            "refreshToken": self.refresh_token,
            "expiresAt": self.expires_at,
            "addedAt": self.added_at,
            "source": self.source,
            "proxySlot": self.proxy_slot,
            "proxy": self.proxy_legacy,
            "enabled": self.enabled,
            "lastError": self.last_error,
            "cooldownUntil": self.cooldown_until,
            "credits": self.credits,
            "lastCheckin": self.last_checkin,
            "lastDailyChat": self.last_daily_chat,
        }

    def _throttle_snapshot(self, now):
        """Return one consistent view of account and per-model cooldowns."""
        with self._throttle_lock:
            error = self.last_error
            deadline = self.cooldown_until
            active = [(model, until) for model, until in self.model_cooldowns.items()
                      if until > now]
        active.sort(key=lambda pair: (pair[1], pair[0]))
        models = [{"model": model, "expiresAt": int(until)} for model, until in active]
        return error, deadline, models

    def model_cooldowns_snapshot(self):
        """Active model cooldowns, ordered by recovery time (epoch seconds)."""
        return self._throttle_snapshot(time.time())[2]

    def public(self):
        exp = self.expires_at or jwt_exp(self.access_token)
        now = time.time()
        last_error, deadline, models = self._throttle_snapshot(now)
        return {
            "uid": self.uid,
            "nickname": self.nickname or (self.uid[:8] if self.uid else "?"),
            "domain": self.domain,
            "realm": self.realm,
            "platform": self.platform,
            "product": self.product,
            "enterpriseId": self.enterprise_id,
            "enabled": bool(self.enabled),
            "source": self.source,
            "proxySlot": self.proxy_slot,
            "proxy": self.proxy,
            "expiresAt": exp,
            "expiresIn": _human_delta(exp - time.time()) if exp else None,
            "hasRefreshToken": bool(self.refresh_token),
            "lastError": last_error,
            "inCooldown": deadline > now,
            "cooldownFor": round(max(0.0, deadline - now)) or None,
            "modelCooldowns": models,
            "addedAt": self.added_at,
            "file": os.path.basename(self.path) if self.path else None,
            "credits": self.credits,
            "reserveCredits": int(self.reserve_credits or 0),
            "reserveBlocked": self.reserve_blocked(),
            "dailyTokenLimit": int(self.daily_token_limit or 0),
            "dailyTokensToday": (int(self.daily_tokens_today)
                                 if isinstance(self.daily_tokens_today, int)
                                 else None),
            "dailyLimitBlocked": self.daily_limit_blocked(),
            "lastCheckin": self.last_checkin,
            "lastDailyChat": self.last_daily_chat,
            "canCheckin": self.realm == "cn",
            "canDailyChat": self.realm == "intl",
            "machineId": derive_id(self.uid, "machine"),
            "sessionId": derive_id(self.uid, "session"),
        }

    def save(self, directory):
        os.makedirs(directory, exist_ok=True)
        safe_uid = re.sub(r"[^A-Za-z0-9_-]", "_", str(self.uid or "")).strip("_ ")
        name = (safe_uid or uuid.uuid4().hex) + ".json"
        path = os.path.abspath(os.path.join(directory, name))
        if not path.startswith(os.path.abspath(directory)):
            raise ValueError("invalid path for account save")
        # A unique temp name plus a per-account lock: two threads saving the
        # same account used to share "<uid>.json.tmp", so one could truncate
        # the file the other was still writing and the loser's os.replace()
        # then failed with ENOENT.
        with self._save_lock:
            tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
                raise
        self.path = path
        return path

    def delete(self):
        if self.path and os.path.exists(self.path):
            os.remove(self.path)

    def reserve_blocked(self):
        """True when the low-credit guard should keep this account idle.

        Only a *known* balance can block: an account whose credits were never
        fetched stays usable, otherwise a fresh install would look empty.
        """
        reserve = int(self.reserve_credits or 0)
        if reserve <= 0:
            return False
        credits = self.credits
        if not isinstance(credits, dict):
            return False
        remain = credits.get("remain")
        if remain is None:
            return False
        try:
            remain = int(remain)
        except (TypeError, ValueError):
            return False
        return remain <= reserve

    def daily_limit_blocked(self):
        """True when today's counted usage has reached the configured limit.

        Only a *counted* day can block: until the proxy has folded the usage
        log once, the count is None and the account stays usable.
        """
        try:
            limit = int(self.daily_token_limit or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            return False
        used = self.daily_tokens_today
        if used is None:
            return False
        try:
            return int(used) >= limit
        except (TypeError, ValueError):
            return False

    def ready(self, model=None):
        if not self.enabled or not self.access_token:
            return False
        if self.throttle_wait(model=model) > 0:
            return False
        # Parked below the reserve: serving a request here is what would push
        # the balance to zero and trigger the upstream reminder SMS.
        if self.reserve_blocked():
            return False
        # Today's token budget is spent: keep the seat for tomorrow instead
        # of letting the upstream answer 429 for the rest of the day.
        if self.daily_limit_blocked():
            return False
        exp = self.expires_at or jwt_exp(self.access_token)
        if not exp:
            return True
        remaining = exp - time.time()
        if remaining > 120:
            return True
        if remaining > 0:
            # Refresh is a last resort and its result decides availability.
            # Returning True unconditionally here kept handing out an account
            # whose token was about to expire, so requests went upstream with a
            # stale credential and came back 401/403.
            return self.refresh()
        return self.refresh()

    def headers(self, purpose="chat"):
        """組出這一輪的出站標頭。

        chat 用途走 wb_identity（CLI 頭 / WorkBuddy 頭，可切換）；
        billing 用途維持原本的輕量標頭，計費端點不吃那套身分。
        """
        cfg = get_realm_config(self.realm)

        if purpose != "chat":
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": cfg["billing_ua"],
                "Origin": cfg["origin"],
                "Referer": cfg["origin"] + "/",
                "Authorization": "Bearer " + self.access_token,
                "X-User-Id": self.uid,
                "X-Domain": self.domain or cfg["domain"],
                "X-CodeBuddy-Request": "1",
                "Accept-Language": "en-US" if self.realm == "intl" else "zh-CN",
                "X-Request-ID": generate_request_id(self.uid),
                "X-Machine-ID": derive_id(self.uid, "machine"),
                "X-Session-ID": derive_id(self.uid, "session"),
            }
            if self.enterprise_id:
                headers["X-Enterprise-Id"] = self.enterprise_id
                headers["X-Tenant-Id"] = self.enterprise_id
            else:
                headers["X-No-Enterprise-Id"] = "1"
            if self.realm == "cn":
                headers["X-Product"] = "SaaS"
            return headers

        identity = wb_identity.build_identity_headers(
            product=self.product,
            realm=self.realm,
            uid=self.uid,
            token=self.access_token,
            conversation_id=getattr(self, "conversation_id", None),
            enterprise_id=self.enterprise_id,
            tenant_id=self.enterprise_id,
        )
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": cfg["origin"],
            "Referer": cfg["origin"] + "/",
            "X-CodeBuddy-Request": "1",
            "Accept-Language": "en-US" if self.realm == "intl" else "zh-CN",
            "X-Machine-ID": derive_id(self.uid, "machine"),
            "X-Session-ID": derive_id(self.uid, "session"),
        }
        headers.update(identity)
        return headers

    def set_product(self, value):
        """切換出站身分（cli <-> workbuddy）。回傳 True 表示真的換了。

        身分會寫回憑證檔，重啟後仍然有效。save() 需要目錄參數。
        """
        new = wb_identity.normalize_product(value)
        if new == self.product:
            return False
        self.product = new
        try:
            self.clear_error()
        except Exception:
            pass
        return True

    def chat_base_url(self):
        """這個帳號目前身分該打的端點。"""
        return wb_identity.endpoint_for(self.realm, self.product)[0]

    def refresh(self):
        # Serialise refreshes per account, then re-check inside the lock: the
        # upstream rotates the refresh token, so two concurrent refreshes can
        # make the second one send a token that the first already consumed.
        with self._refresh_lock:
            return self._refresh_locked()

    def _refresh_locked(self):
        if not self.refresh_token:
            self._set_last_error("no refresh token; sign in again")
            return False
        cfg = get_realm_config(self.realm)
        url = cfg["chat_upstream"] + REFRESH_PATH
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": cfg["billing_ua"],
            "Origin": cfg["origin"],
            "Referer": cfg["origin"] + "/",
            "X-Refresh-Token": self.refresh_token,
            "X-Auth-Refresh-Source": "workbuddy" if self.realm == "cn" else "plugin",
            "X-User-Id": self.uid,
            "X-Domain": self.domain or cfg["domain"],
            "X-CodeBuddy-Request": "1",
            "Accept-Language": "en-US" if self.realm == "intl" else "zh-CN",
        }
        if self.enterprise_id:
            headers["X-Enterprise-Id"] = self.enterprise_id
        try:
            payload = http_json(url, data=b"{}", method="POST", headers=headers, timeout=30,
                                proxy=self.proxy)
        except Exception as exc:
            self._set_last_error("refresh failed: %s" % exc)
            return False
        data = (payload.get("data") or {})
        data = data.get("data") or data
        token = data.get("accessToken")
        if not token:
            self._set_last_error("refresh returned no token (%s)" % payload.get("msg"))
            return False
        self.access_token = token
        self.refresh_token = data.get("refreshToken") or self.refresh_token
        self.expires_at = jwt_exp(token) or self.expires_at
        with self._throttle_lock:
            self.last_error = ""
            self.cooldown_until = 0
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))
        return True

    def can_checkin(self):
        if self.realm != "cn":
            return False
        if not self.last_checkin:
            return True
        today_str = time.strftime("%Y-%m-%d")
        return not str(self.last_checkin).startswith(today_str)

    def can_daily_chat(self):
        if self.realm != "intl":
            return False
        if not self.last_daily_chat:
            return True
        today_str = time.strftime("%Y-%m-%d")
        return not str(self.last_daily_chat).startswith(today_str)

    def web_headers(self):
        """网页版 app 的出站头：只有 bearer 与 X-User-Id，没有桌面端指纹。"""
        return {
            "Authorization": "Bearer " + self.access_token,
            "X-User-Id": self.uid,
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": WEB_ORIGIN,
            "Referer": WEB_ORIGIN + "/app",
            "User-Agent": WEB_USER_AGENT,
        }

    def daily_chat_web(self, prompt=None):
        """网页通道的每日活跃会话（issue #75 / #59 / #90）。

        只建会话是不够的：agent 要等客户端接上沙箱并请求这一轮才会跑，否则会话
        永远停在 CREATING、没有任何输出，也就不算一次有效对话（#90 实测）。这里
        按网页端的顺序走完：建会话 → 取沙箱 link+token → ACP 的 initialize /
        session/load / session/prompt（见 wb_webagent）→ 轮询到 completed。

        返回 {"ok": True, "conversation": id, "status": "completed", "chunks": n,
        "elapsed_ms": n}，失败时 {"ok": False, "error": ...}（尽量带上会话 id）。
        """
        if self.realm != "intl":
            return {"ok": False, "error": "web daily chat is only for international accounts"}
        body = {
            "prompt": prompt or DAILY_CHAT_WEB_PROMPT,
            "model": DAILY_CHAT_MODEL,
            # 网页端建会话时固定带上这两项（抓包所得），保持请求形态一致。
            "conversationOrigin": "workbuddy-app",
            "plugins": [{"name": "weixinpay", "marketplace": "codebuddy-builtin"}],
        }
        req = urllib.request.Request(
            WEB_CONVERSATIONS_URL,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=self.web_headers(), method="POST")
        try:
            with urlopen(req, timeout=30, proxy=self.proxy) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:
                detail = str(exc)
            return {"ok": False, "error": "HTTP %d: %s" % (exc.code, detail[:120])}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if not isinstance(payload, dict):
            return {"ok": False, "error": "unexpected response"}
        conversation = (payload.get("data") or {}).get("id")
        if payload.get("code") not in (0, None) or not conversation:
            return {"ok": False, "error": "code=%s msg=%s"
                    % (payload.get("code"), payload.get("msg"))}

        # 建完只是排队：接上沙箱、请求这一轮，它才会真的跑起来（issue #90）。
        try:
            session = (self._web_conversation_get(conversation, "/session") or {}).get("data") or {}
        except Exception as exc:
            return {"ok": False, "conversation": conversation,
                    "error": "session 查询失败: %s" % exc}
        link = session.get("link") or session.get("endpoint") or ""
        token = session.get("token") or ""
        session_id = session.get("sessionId") or session.get("session_id") or conversation
        cwd = session.get("cwd") or "/workspace"
        if not link or not token:
            return {"ok": False, "conversation": conversation,
                    "error": "沙箱未就绪（没有 link/token）"}

        result = wb_webagent.run_turn(
            link, token, session_id, cwd, prompt or DAILY_CHAT_WEB_PROMPT,
            WEB_USER_AGENT,
            poll_status=lambda: self._web_conversation_status(conversation),
            wait_seconds=WEB_TURN_TIMEOUT, proxy=self.proxy)
        result["conversation"] = conversation
        if result.get("ok"):
            result["msg"] = "网页通道会话跑完：%d 段输出，%d ms" % (
                result.get("chunks") or 0, result.get("elapsed_ms") or 0)
        return result

    def _web_conversation_get(self, conversation, suffix=""):
        """读一条网页端会话（suffix 为空拿会话本身，"/session" 拿沙箱信息）。"""
        url = WEB_CONVERSATIONS_URL + urllib.parse.quote(str(conversation)) + suffix
        req = urllib.request.Request(url, headers=self.web_headers(), method="GET")
        with urlopen(req, timeout=30, proxy=self.proxy) as resp:
            return json.loads(resp.read().decode("utf-8", "replace") or "{}")

    def _web_conversation_status(self, conversation):
        """这条会话现在什么状态（completed 就是这一轮真的跑完了）。"""
        try:
            payload = self._web_conversation_get(conversation)
        except Exception:
            return ""
        data = payload.get("data") if isinstance(payload, dict) else None
        return str((data or {}).get("status") or "")

    def daily_chat(self, web=None):
        """国际版每日活跃对话（官方每日活跃 30/50 积分）。

        两步：桌面端身分的轻量对话（一直以来的做法），以及网页通道的会话
        （issue #75/#59/#90：算数的是「跑完的 agent 会话」）。web=None 时按
        settings.json 里的 daily_chat_web 决定，True/False 可显式指定。
        """
        if self.realm != "intl":
            return {"ok": False, "error": "daily chat is only for international accounts"}
        import wb_proxy
        url = self.chat_base_url() + wb_proxy.CHAT_PATH
        headers = self.headers("chat")
        body = {
            "model": DAILY_CHAT_MODEL,
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
            "max_tokens": 10,
            "reasoning_effort": "none",
        }
        req_body = wb_proxy.build_upstream_body(body)
        data = json.dumps(req_body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=20, proxy=self.proxy) as resp:
                _ = resp.read()
            self.last_daily_chat = time.strftime("%Y-%m-%d %H:%M:%S")
            if self.path and os.path.exists(os.path.dirname(self.path)):
                self.save(os.path.dirname(self.path))
            try:
                self.fetch_credits()
            except Exception:
                pass
            result = {"ok": True, "msg": "每日活跃对话成功完成"}
            if web is None:
                web = bool(self.path) and wb_settings.daily_chat_web(os.path.dirname(self.path))
            if web:
                res = self.daily_chat_web()
                result["web"] = res
                if res.get("ok"):
                    result["msg"] = ("每日活跃对话成功完成（网页通道 %s：%d 段输出，%d ms）"
                                     % (res.get("status") or "completed",
                                        res.get("chunks") or 0, res.get("elapsed_ms") or 0))
                else:
                    result["msg"] = ("每日活跃对话成功完成（网页通道失败：%s）"
                                     % res.get("error"))
            return result
        except urllib.error.HTTPError as exc:
            try:
                err = exc.read().decode("utf-8", "replace")
            except Exception:
                err = str(exc)
            return {"ok": False, "error": f"HTTP {exc.code}: {err[:100]}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def checkin(self):
        if self.realm != "cn":
            return {"ok": False, "error": "checkin is only available for CN realm accounts"}
        cfg = get_realm_config("cn")
        url = cfg["billing_upstream"] + CHECKIN_PATH
        headers = self.headers(purpose="billing")
        try:
            payload = http_json(url, data=b"{}", method="POST", headers=headers, timeout=15,
                                proxy=self.proxy)
            code = payload.get("code", -1)
            msg = payload.get("msg") or "ok"
            self.last_checkin = time.strftime("%Y-%m-%d %H:%M:%S")
            if self.path and os.path.exists(os.path.dirname(self.path)):
                self.save(os.path.dirname(self.path))
            return {"ok": (code == 0 or code == 10001), "code": code, "msg": msg, "data": payload.get("data")}
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8") or "{}")
                return {"ok": False, "error": body.get("msg") or ("HTTP %d" % exc.code)}
            except Exception:
                return {"ok": False, "error": "HTTP %d" % exc.code}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def fetch_credits(self):
        cfg = get_realm_config(self.realm)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        body = {
            "PageNumber": 1,
            "PageSize": 100,
            "ProductCode": "p_tcaca",
            "Status": [0, 3],
            "PackageEndTimeRangeBegin": now,
            "PackageEndTimeRangeEnd": "2036-01-01 00:00:00",
        }
        url = cfg["billing_upstream"] + GET_RESOURCE_PATH
        headers = self.headers(purpose="billing")
        try:
            res = http_json(url, data=json.dumps(body).encode(), method="POST", headers=headers,
                            timeout=30, proxy=self.proxy)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        data = res.get("data", {}).get("Response", {}).get("Data", {})
        accounts = data.get("Accounts") or []
        tot_remain, tot_used, tot_size = 0, 0, 0
        packages = []
        for a in accounts:
            pkg_name = a.get("PackageName") or "Package"
            if a.get("CycleCapacitySize", 0) > 0:
                remain = a.get("CycleCapacityRemain", 0)
                size = a.get("CycleCapacitySize", 0)
                used = max(0, size - remain)
                if a.get("CycleCapacityUsed", 0) > used:
                    used = a["CycleCapacityUsed"]
                    remain = max(0, size - used)
            else:
                remain = a.get("CapacityRemain", 0)
                used = a.get("CapacityUsed", 0)
                size = a.get("CapacitySize", 0)
            tot_remain += remain
            tot_used += used
            tot_size += size
            packages.append({"name": pkg_name, "remain": remain, "used": used, "size": size})
        self.credits = {
            "remain": tot_remain,
            "used": tot_used,
            "size": tot_size,
            "packages": packages,
            "updated_at": time.time(),
            "updated_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))
        return {"ok": True, "credits": self.credits}

    def _set_last_error(self, message):
        """Record refresh errors alongside the state shown in the panel."""
        with self._throttle_lock:
            self.last_error = str(message)[:200]

    def note_error(self, message, cooldown=60, single_account=False, model=None, until=None):
        with self._throttle_lock:
            self.last_error = str(message)[:200]
            if model:
                # Model-scoped throttle: keep the account usable for every other model.
                wait = max(1.0, float(until) - time.time()) if until else (
                    3.0 if single_account else float(cooldown))
                self.model_cooldowns[model] = time.time() + wait
                return
            actual_cooldown = 3 if single_account else cooldown
            self.cooldown_until = time.time() + actual_cooldown

    def throttle_wait(self, model=None):
        """Seconds until this account can serve `model` again (0 = right now)."""
        if not self.enabled or not self.access_token:
            return 0.0
        now = time.time()
        with self._throttle_lock:
            wait = max(0.0, self.cooldown_until - now)
            if model:
                wait = max(wait, max(0.0, self.model_cooldowns.get(model, 0.0) - now))
        return wait

    def clear_error(self, model=None):
        with self._throttle_lock:
            if model:
                self.model_cooldowns.pop(model, None)
            else:
                self.model_cooldowns.clear()
            if self.last_error or self.cooldown_until:
                self.last_error = ""
                self.cooldown_until = 0

def _human_delta(seconds):
    if seconds is None: return None
    if seconds <= 0: return "expired"
    days = seconds / 86400.0
    if days >= 1: return "%.0f days" % days
    hours = seconds / 3600.0
    if hours >= 1: return "%.1f hours" % hours
    return "%d min" % int(seconds / 60)

class SessionAffinity(object):
    def __init__(self, ttl=7200, max_entries=5000):
        self.ttl = ttl
        self.max_entries = max_entries
        self.bindings = {}
        self._lock = threading.Lock()
    def get(self, key):
        if not key: return None
        with self._lock:
            entry = self.bindings.get(key)
            if not entry: return None
            uid, exp = entry
            if time.time() > exp:
                self.bindings.pop(key, None)
                return None
            self.bindings[key] = (uid, time.time() + self.ttl)
            return uid
    def bind(self, key, uid):
        if not key or not uid: return
        with self._lock:
            if len(self.bindings) >= self.max_entries:
                now = time.time()
                self.bindings = {k: v for k, v in self.bindings.items() if v[1] > now}
            self.bindings[key] = (uid, time.time() + self.ttl)
    def unbind(self, key):
        if not key: return
        with self._lock:
            self.bindings.pop(key, None)

class AccountPool(object):
    def __init__(self, directory, log=None):
        self.dir = directory
        self.log = log or (lambda msg: None)
        self.accounts = []
        self.logins = {}
        self._lock = threading.RLock()
        self._cursor = 0
        self.affinity = SessionAffinity()

    def load(self):
        with self._lock:
            self.accounts = []
            if not os.path.isdir(self.dir): return self.accounts
            for name in sorted(os.listdir(self.dir)):
                if not name.endswith(".json"): continue
                path = os.path.join(self.dir, name)
                try:
                    with open(path, encoding="utf-8") as fh:
                        account = Account(json.load(fh), path)
                except Exception as exc:
                    self.log("account %s unreadable: %s" % (name, exc))
                    continue
                if account.uid:
                    self.accounts.append(account)
            self.apply_reserve_credits()
            return self.accounts

    def list_public(self, realm=None):
        with self._lock:
            accs = self.accounts if (not realm or realm == "all") else [a for a in self.accounts if a.realm == realm]
            return [a.public() for a in accs]

    def get(self, uid):
        with self._lock:
            for account in self.accounts:
                if account.uid == uid: return account
        return None

    def add(self, account):
        with self._lock:
            existing = self.get(account.uid)
            if existing is not None:
                account.added_at = existing.added_at
                account.path = existing.path
                if not account.credits and existing.credits:
                    account.credits = existing.credits
                if not account.last_checkin and existing.last_checkin:
                    account.last_checkin = existing.last_checkin
                if not account.last_daily_chat and existing.last_daily_chat:
                    account.last_daily_chat = existing.last_daily_chat
                if not account.proxy_slot and existing.proxy_slot:
                    account.proxy_slot = existing.proxy_slot
                if not account.proxy_legacy and existing.proxy_legacy:
                    account.proxy_legacy = existing.proxy_legacy
                self.accounts[self.accounts.index(existing)] = account
            else:
                self.accounts.append(account)
            account.save(self.dir)
            self.apply_proxy_slots()
            self.apply_reserve_credits()
            return account

    def remove(self, uid):
        with self._lock:
            account = self.get(uid)
            if account is None: return False
            account.delete()
            self.accounts.remove(account)
            return True

    def preview_import_rows(self, rows, realm=None, overwrite=False):
        """Report what import_rows() would do, without touching the pool.

        Shares the same accept/skip rules as import_rows() so a dry run cannot
        disagree with the real thing.
        """
        preview = {"added": [], "updated": [], "skipped": [], "invalid": []}
        known = {a.uid for a in self.accounts}
        seen = set()
        for index, row in enumerate(rows):
            try:
                kwargs = normalise_import_row(row, realm=realm)
            except Exception as exc:
                preview["invalid"].append({"index": index + 1, "reason": str(exc)})
                continue
            uid = kwargs["uid"]
            if uid in seen:
                preview["skipped"].append({"uid": uid, "reason": "duplicate inside the document"})
            elif uid in known and not overwrite:
                preview["skipped"].append({"uid": uid, "reason": "already exists"})
            elif uid in known:
                preview["updated"].append(uid)
            else:
                preview["added"].append(uid)
            seen.add(uid)
        return preview

    def import_rows(self, rows, realm=None, overwrite=False):
        """Add accounts from exported/foreign rows.

        Returns a report dict:
            added       - uids that were new to the pool
            updated     - uids that already existed and were replaced
            skipped     - [{"uid","reason"}] rows that were not imported
            invalid     - [{"index","reason"}] rows that could not be parsed

        Nothing is written until a row parses cleanly, so one bad entry does
        not abort the rest of the file.
        """
        added, updated, skipped, invalid = [], [], [], []
        seen = set()
        for index, row in enumerate(rows):
            try:
                kwargs = normalise_import_row(row, realm=realm)
            except Exception as exc:
                invalid.append({"index": index + 1, "reason": str(exc)})
                continue

            uid = kwargs["uid"]
            if uid in seen:
                skipped.append({"uid": uid, "reason": "duplicate inside the document"})
                continue
            seen.add(uid)

            existing = self.get(uid) is not None
            if existing and not overwrite:
                skipped.append({"uid": uid, "reason": "already exists"})
                continue

            try:
                self.add(Account(kwargs))
            except Exception as exc:
                invalid.append({"index": index + 1, "reason": str(exc)})
                continue

            (updated if existing else added).append(uid)

        if added or updated:
            self.apply_proxy_slots()
        return {
            "added": added,
            "updated": updated,
            "skipped": skipped,
            "invalid": invalid,
        }

    def set_enabled(self, uid, enabled):
        account = self.get(uid)
        if account is None: return None
        account.enabled = bool(enabled)
        if enabled:
            account.clear_error()
        account.save(self.dir)
        self.apply_proxy_slots()
        return account.public()

    def set_proxy(self, uid, proxy):
        account = self.get(uid)
        if account is None:
            return None
        account.proxy_legacy = str(proxy or "").strip()
        account.save(self.dir)
        self.apply_proxy_slots()
        return account.public()

    def apply_proxy_slots(self, slots=None):
        """Re-resolve every account's runtime `proxy` from its bound slot.

        `proxy_slot` is the persisted source of truth; `proxy` is a derived
        runtime value so the many outbound call sites need no change.
        """
        import wb_settings

        if slots is None:
            slots = wb_settings.proxy_slots(self.dir)
        by_id = {entry["id"]: entry for entry in slots if entry.get("enabled")}
        with self._lock:
            for account in self.accounts:
                slot = by_id.get(account.proxy_slot)
                if slot:
                    account.proxy = slot["url"]
                else:
                    account.proxy = account.proxy_legacy

    def apply_reserve_credits(self, value=None):
        """Re-resolve the low-credit guard for every account.

        Same shape as apply_proxy_slots(): settings.json is the source of
        truth and the per-account value is derived here, so the request path
        needs no extra settings lookup.
        """
        import wb_settings

        if value is None:
            value = wb_settings.reserve_credits(self.dir)
        try:
            value = max(0, int(value or 0))
        except (TypeError, ValueError):
            value = 0
        with self._lock:
            for account in self.accounts:
                account.reserve_credits = value
        return value

    def apply_daily_token_limit(self, value=None, usage=None):
        """Re-resolve the daily token guard for every account.

        Same shape as apply_reserve_credits(): settings.json holds the limit,
        while `usage` (uid -> tokens counted today) comes from the caller,
        because only the proxy reads the usage log. Passing None keeps the
        last known counts, so a settings change never turns them into
        "unknown".
        """
        import wb_settings

        if value is None:
            value = wb_settings.daily_token_limit(self.dir)
        try:
            value = max(0, int(value or 0))
        except (TypeError, ValueError):
            value = 0
        with self._lock:
            for account in self.accounts:
                was_blocked = account.daily_limit_blocked()
                account.daily_token_limit = value
                if usage is not None:
                    try:
                        account.daily_tokens_today = int(usage.get(account.uid, 0))
                    except (TypeError, ValueError):
                        account.daily_tokens_today = None
                now_blocked = account.daily_limit_blocked()
                if now_blocked != was_blocked:
                    if now_blocked:
                        self.log("account %s parked: daily token limit reached "
                                 "(%s/%s tokens today)"
                                 % (str(account.uid)[:8],
                                    account.daily_tokens_today, value))
                    else:
                        self.log("account %s resumed: daily token limit cleared"
                                 % str(account.uid)[:8])
        return value

    def set_proxy_slot(self, uid, slot_id):
        account = self.get(uid)
        if account is None:
            return None
        slot_id = str(slot_id or "").strip()
        account.proxy_slot = slot_id
        if not slot_id:
            # Selecting "direct" must mean direct. A stale legacy URL left in
            # place kept routing traffic through it, so the panel showed
            # direct while the account was still proxied.
            account.proxy_legacy = ""
        account.save(self.dir)
        self.apply_proxy_slots()
        return account.public()

    def set_all_enabled(self, enabled, realm=None):
        with self._lock:
            for account in self.accounts:
                if realm and account.realm != realm: continue
                account.enabled = bool(enabled)
                if enabled:
                    account.clear_error()
                account.save(self.dir)
        self.apply_proxy_slots()

    def count_ready(self, realm=None, model=None):
        with self._lock:
            snapshot = [a for a in self.accounts if not realm or a.realm == realm]
        return sum(1 for a in snapshot if a.enabled and a.access_token and a.ready(model=model))

    def pick_for_session(self, realm=None, session_key=None, exclude=None, model=None):
        exclude = exclude or set()
        if session_key:
            bound_uid = self.affinity.get(session_key)
            if bound_uid and bound_uid not in exclude:
                account = self.get(bound_uid)
                if account and account.realm == realm and account.ready(model=model):
                    return account
                self.affinity.unbind(session_key)
        account = self.pick(realm=realm, exclude=exclude, model=model)
        if account and session_key:
            self.affinity.bind(session_key, account.uid)
        return account

    def pick(self, realm=None, exclude=None, model=None):
        exclude = exclude or set()
        with self._lock:
            snapshot = [a for a in self.accounts if not realm or a.realm == realm]
            start = self._cursor
        total = len(snapshot)
        if total == 0: return None
        for offset in range(total):
            index = (start + offset) % total
            account = snapshot[index]
            if account.uid in exclude: continue
            if account.ready(model=model):
                with self._lock: self._cursor = (index + 1) % total
                return account
        return None

    def representative(self, realm=None):
        with self._lock:
            candidates = [a for a in self.accounts if not realm or a.realm == realm]
            for account in candidates:
                if account.access_token: return account
            return candidates[0] if candidates else None

    def start_login(self, realm="intl", platform="CLI"):
        cfg = get_realm_config(realm)
        url = "%s%s?platform=%s" % (cfg["chat_upstream"], AUTH_STATE_PATH, urllib.parse.quote(str(platform)))
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": cfg["billing_ua"],
            "Origin": cfg["origin"],
            "Referer": cfg["origin"] + "/",
        }
        payload = http_json(url, data=b"{}", method="POST", headers=headers,
                            timeout=30, retries=3, log=self.log)
        data = payload.get("data") or {}
        state = data.get("state")
        auth_url = data.get("authUrl")
        if not state or not auth_url:
            raise RuntimeError("auth/state returned no state/authUrl: %s" % payload)
        with self._lock:
            self.logins[state] = {"created": time.time(), "platform": platform, "realm": realm}
        return {"state": state, "authUrl": auth_url, "realm": realm, "platform": platform}

    def poll_login(self, state):
        state = str(state or "").strip()
        with self._lock:
            info = self.logins.get(state)
        if not info:
            return {"status": "unknown", "message": "state not recognised - start the login again"}
        if time.time() - info["created"] > LOGIN_TTL_SECONDS:
            with self._lock: self.logins.pop(state, None)
            return {"status": "expired", "message": "login window expired - start again"}
        realm = info.get("realm") or "intl"
        cfg = get_realm_config(realm)
        url = "%s%s?state=%s" % (cfg["chat_upstream"], AUTH_TOKEN_PATH, urllib.parse.quote(state))
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": cfg["billing_ua"],
            "Origin": cfg["origin"],
            "Referer": cfg["origin"] + "/",
        }
        try:
            payload = http_json(url, method="GET", headers=headers, timeout=30, retries=2)
        except Exception as exc:
            return {"status": "pending", "message": "poll error: %s" % exc}
        code = payload.get("code")
        if code == LOGIN_PENDING:
            return {"status": "pending", "message": payload.get("msg") or "waiting for browser login"}
        if code != 0:
            return {"status": "error", "message": "code=%s msg=%s" % (code, payload.get("msg"))}
        data = payload.get("data") or {}
        token = data.get("accessToken")
        if not token:
            return {"status": "pending", "message": "waiting for token"}
        uid = jwt_uid(token)
        nickname = ""
        try:
            acct_url = "%s%s?state=%s" % (cfg["chat_upstream"], LOGIN_ACCOUNT_PATH, urllib.parse.quote(state))
            acct_headers = dict(headers)
            acct_headers["Authorization"] = "Bearer " + token
            req_acct = urllib.request.Request(acct_url, method="GET", headers=acct_headers)
            with urllib.request.urlopen(req_acct, timeout=15) as resp_acct:
                profile = json.loads(resp_acct.read().decode("utf-8"))
                profile_data = profile.get("data") or {}
                nickname = str(profile_data.get("nickname") or "")
        except Exception: pass
        account = Account({
            "uid": uid,
            "nickname": nickname or uid[:8],
            "domain": data.get("domain") or cfg["domain"],
            "realm": realm,
            "platform": info["platform"],
            "accessToken": token,
            "refreshToken": data.get("refreshToken") or "",
            "expiresAt": normalize_epoch(data.get("expiresAt")) or jwt_exp(token),
            "source": "oauth",
            "enabled": True,
        })
        self.add(account)
        if realm == "cn":
            try: account.checkin()
            except Exception: pass
        with self._lock: self.logins.pop(state, None)
        return {"status": "ok", "account": account.public()}

    def cancel_login(self, state):
        with self._lock: return self.logins.pop(state, None) is not None

    def import_desktop_credential(self, path=None, realm=None, source="desktop-app"):
        if not path:
            found = []
            candidates = desktop_credential_candidates()
            for p, r in candidates:
                if realm and r != realm: continue
                try:
                    acc = self.import_desktop_credential(path=p, realm=r, source=source)
                    if acc: found.append(acc)
                except Exception: pass
            return found
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
        auth = blob.get("auth") or {}
        profile = blob.get("account") or {}
        token, key = desktop_decrypt_tokens(auth, log=self.log)
        if not token: raise RuntimeError("no accessToken in %s" % path)
        detected_realm = desktop_effective_realm(realm, token, auth.get("domain"))
        if realm and detected_realm != realm:
            self.log("desktop credential %s is a %s account (file hints %s) - importing as %s"
                     % (os.path.basename(path), detected_realm, realm, detected_realm))
        cfg = get_realm_config(detected_realm)
        nickname = profile.get("nickname")
        if key is not None and wb_atrest.is_envelope(nickname):
            # 国内版桌面端把昵称也一起加密了，能解就顺手解出来，省得只显示 UID 前缀
            nickname = wb_atrest.decrypt_field(key, nickname) or ""
        account = Account({
            "uid": profile.get("uid") or jwt_uid(token),
            "nickname": nickname if isinstance(nickname, str) else "",
            "domain": domain_for_realm(detected_realm, auth.get("domain")),
            "realm": detected_realm,
            "platform": "CLI",
            "enterpriseId": profile.get("enterpriseId") or "",
            "accessToken": token,
            "refreshToken": desktop_refresh_token(auth, key),
            "expiresAt": normalize_epoch(auth.get("expiresAt")) or jwt_exp(token),
            "source": source,
            "enabled": True,
        })
        self.add(account)
        if detected_realm == "cn":
            try: account.checkin()
            except Exception: pass
        return account

def desktop_atrest_key(force=False, log=None):
    """拿到桌面端 at-rest 密钥（内存里有就直接用，没有才去进程内存里找回）。

    只有真的遇到加密信封才会走到这里，所以旧客户端 / 明文凭据完全不受影响。
    回收失败（客户端没开、系统不是 Windows、权限不够）时抛 wb_atrest.AtRestError，
    由调用方翻译成给用户看的话。
    """
    return wb_atrest.recover_key(force=force, log=log or (lambda msg: None))


def desktop_decrypt_tokens(auth, log=None):
    """返回 (accessToken 明文, 密钥或 None)。

    桌面客户端 2026-09-24 之后把 token 存成 `$wbEncrypted` 信封，这里统一在
    读取处解密；仍然是明文的旧客户端原样返回、不去碰密钥。
    """
    raw = auth.get("accessToken")
    if not wb_atrest.is_envelope(raw):
        return str(raw or ""), None
    key = desktop_atrest_key(log=log)
    return wb_atrest.decrypt_field(key, raw) or "", key


def desktop_refresh_token(auth, key):
    """refreshToken 同样可能是信封；解不开时留空（有 accessToken 就还能跑）。"""
    raw = auth.get("refreshToken")
    if not wb_atrest.is_envelope(raw):
        return raw if isinstance(raw, str) else ""
    if key is None:
        return ""
    try:
        return wb_atrest.decrypt_field(key, raw) or ""
    except Exception:
        return ""


def desktop_auth_dirs():
    """Directories where the desktop client may keep its *.info credentials.

    Windows uses %LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth.
    macOS builds of the client keep the same layout under Application
    Support, so probe the plausible app names there too. Missing
    directories are harmless: callers only read the files that exist.
    """
    dirs = []
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            local = os.path.join(os.path.expanduser("~"), "AppData", "Local")
        dirs.append(os.path.join(local, "CodeBuddyExtension", "Data", "Public", "auth"))
    else:
        home = os.path.expanduser("~")
        base = (os.path.join(home, "Library", "Application Support")
                if sys.platform == "darwin"
                else os.path.join(home, ".local", "share"))
        for app in ("CodeBuddyExtension", "WorkBuddy", "CodeBuddy"):
            dirs.append(os.path.join(base, app, "Data", "Public", "auth"))
            dirs.append(os.path.join(base, app, "auth"))
    return dirs

def desktop_auth_dir():
    """First candidate credential directory (kept for older callers)."""
    return desktop_auth_dirs()[0]

def desktop_credential_candidates():
    out = []
    for base in desktop_auth_dirs():
        if not os.path.isdir(base): continue
        for name, realm in (("workbuddy-desktop-ai.info", "intl"),
                            ("workbuddy-desktop.info", "cn")):
            path = os.path.join(base, name)
            if os.path.isfile(path) and (path, realm) not in out:
                out.append((path, realm))
    return out


def scan_desktop_credentials():
    """Describe the desktop-app credentials found on this machine.

    Read-only: nothing is added to the pool. The dashboard shows the result
    and lets the user decide which ones to import, so the proxy never
    silently adopts the desktop client's login.

    桌面端加密之后（2026-09-24 起）扫描要多分辨一种情况：凭据是加密信封，
    得先把密钥回收回来才能读。这里只查内存里已有的密钥，不触发回收（回收要
    扫客户端进程内存，得由用户在弹窗里显式点一次），所以扫描始终是秒回的。
    """
    found = []
    key = None
    for path, realm in desktop_credential_candidates():
        cfg = get_realm_config(realm)
        item = {
            "path": path,
            "file": os.path.basename(path),
            "realm": realm,
            "realmName": cfg["name"],
            "domain": cfg["domain"],
            "readable": False,
            "valid": False,
            "encrypted": False,
            "needsKey": False,
            "uid": "",
            "nickname": "",
            "expiresAt": 0,
            "error": "",
        }
        try:
            with open(path, encoding="utf-8") as fh:
                blob = json.load(fh)
            auth = blob.get("auth") or {}
            profile = blob.get("account") or {}
            raw = auth.get("accessToken")
            item["readable"] = True
            if wb_atrest.is_envelope(raw):
                item["encrypted"] = True
                if key is None:
                    try:
                        key = wb_atrest.cached_key()
                    except Exception:
                        key = None
                if key is None:
                    item["needsKey"] = True
                    item["error"] = "凭据已加密：先点「回收密钥」，再回来导入"
                    found.append(item)
                    continue
                token = wb_atrest.decrypt_field(key, raw) or ""
            else:
                token = str(raw or "")
            if not token:
                item["error"] = "no accessToken inside the file"
                found.append(item)
                continue
            nickname = profile.get("nickname")
            if key is not None and wb_atrest.is_envelope(nickname):
                nickname = wb_atrest.decrypt_field(key, nickname)
            exp = normalize_epoch(auth.get("expiresAt")) or jwt_exp(token) or 0
            # 区域以 token 为准（文件名只兜底），跟导入时用的是同一套判断，
            # 免得列表里标着国内版、导入却跑进国际版
            item_realm = desktop_effective_realm(realm, token, auth.get("domain"))
            item_cfg = get_realm_config(item_realm)
            item.update({
                "valid": True,
                "uid": profile.get("uid") or jwt_uid(token),
                "nickname": nickname if isinstance(nickname, str) else "",
                "realm": item_realm,
                "realmName": item_cfg["name"],
                "realmHint": realm if item_realm != realm else "",
                "domain": domain_for_realm(item_realm, auth.get("domain")),
                "expiresAt": exp,
                "expiresIn": _human_delta(exp - time.time()) if exp else None,
            })
        except Exception as exc:
            item["error"] = str(exc)
        found.append(item)
    return found


# --------------------------------------------------------------- export / import
#
# Accounts travel as a single JSON document so a pool can be moved between
# machines (or backed up) without reaching into the accounts directory by hand.
# The shape is deliberately close to the per-account files on disk, so an
# exported document can be read by eye and hand-edited if needed.
#
# Two containers are accepted on import:
#   1. this module's own export  -> {"format": "workbuddy-accounts", "accounts": [...]}
#   2. a bare list               -> [ {...}, {...} ]           (hand-written)
#   3. a single account object   -> {...}                       (one-off paste)
# A desktop-app credential ({"auth": {...}, "account": {...}}) is also accepted,
# because that is what people usually have lying around.

EXPORT_FORMAT = "workbuddy-accounts"
EXPORT_VERSION = 1

# Fields that describe live state rather than the credential itself. They are
# exported for inspection but never trusted on import: a stale cooldown or a
# disabled flag from another machine would silently cripple the target pool.
VOLATILE_FIELDS = ("cooldownUntil", "lastError", "credits", "lastCheckin", "lastDailyChat")


def account_to_export(account):
    """Serialise one account for an export document."""
    data = account.to_dict()
    # Keep the credential and identity; drop nothing, but mark the file source
    # so a re-import on the same machine does not look like a desktop import.
    data.pop("path", None)
    return data


def build_export_document(accounts, realm=None, include_secrets=True, uids=None):
    """Wrap accounts in a self-describing export document.

    `uids` narrows the export to specific accounts (a single uid gives a
    one-account document). It is applied on top of the realm filter, so the
    caller can ask for "this account" and still get an empty document rather
    than a wrong one when the uid belongs to the other realm.
    """
    wanted = None
    if uids is not None:
        wanted = {str(u) for u in uids}
    rows = []
    for account in accounts:
        if realm and account.realm != realm:
            continue
        if wanted is not None and account.uid not in wanted:
            continue
        row = account_to_export(account)
        if not include_secrets:
            row.pop("accessToken", None)
            row.pop("refreshToken", None)
        rows.append(row)
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "exportedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(rows),
        "accounts": rows,
    }


def _coerce_account_rows(blob):
    """Normalise any accepted container into a list of account dicts.

    Returns (rows, error). Accepts the export document, a bare list, a single
    account object, or a desktop-app credential.
    """
    if isinstance(blob, list):
        rows = blob
    elif isinstance(blob, dict) and isinstance(blob.get("accounts"), list):
        # Our own export document (or any object carrying an accounts array).
        rows = blob["accounts"]
    elif isinstance(blob, dict):
        # A single account object, or a desktop-app credential
        # ({"account": {...}, "auth": {...}}). Anything else is a wrong shape
        # and must be reported rather than silently treated as one account.
        looks_like_account = (
            blob.get("accessToken")
            or isinstance(blob.get("auth"), dict)
            or isinstance(blob.get("account"), dict)
        )
        if not looks_like_account:
            keys = ", ".join(sorted(blob.keys())[:6]) or "none"
            return [], ("not an account document (expected an accounts array, "
                        "a list, or an account object; got keys: %s)" % keys)
        rows = [blob]
    else:
        return [], "expected an object or a list of accounts"

    out = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            return [], "account #%d is not an object" % (index + 1)
        out.append(row)
    if not out:
        return [], "no accounts found in the document"
    return out, ""


def normalise_import_row(row, realm=None):
    """Turn one exported/foreign row into Account kwargs.

    Accepts both the flat account shape and the nested desktop-credential shape
    so a file from either source imports cleanly. Raises ValueError when the
    row carries no usable credential.
    """
    auth = row.get("auth") if isinstance(row.get("auth"), dict) else None
    profile = row.get("account") if isinstance(row.get("account"), dict) else None

    def pick(key, default=None):
        """Read a field from whichever layer holds it (flat, auth, account)."""
        for layer in (row, auth, profile):
            if isinstance(layer, dict) and layer.get(key) not in (None, ""):
                return layer.get(key)
        return default

    token = str(pick("accessToken") or "").strip()
    if not token:
        raise ValueError("no accessToken")
    if token.count(".") != 2:
        raise ValueError("accessToken is not a JWT")

    # Where the account belongs. A realm forced by the caller still wins (that
    # is what the API option is for), otherwise the token/domain decides: a row
    # carrying a stale "realm" (an account once filed under the wrong region,
    # then exported again) must not keep dragging itself back there, and the
    # wrong region sends every request to the wrong upstream.
    forced = str(realm or "").strip().lower()
    evidence = realm_evidence(token, pick("domain"))
    row_realm = str(pick("realm") or "").strip().lower()
    if forced in ("intl", "cn"):
        detected = forced
    elif evidence:
        detected = evidence
    elif row_realm in ("intl", "cn"):
        detected = row_realm
    else:
        detected = detect_realm_from_token(token, pick("domain"))
    cfg = get_realm_config(detected)

    raw_uid = str(pick("uid") or "").strip() or jwt_uid(token)
    uid = re.sub(r"[^A-Za-z0-9_-]", "_", raw_uid).strip("_ ")
    if not uid:
        raise ValueError("cannot determine uid (no uid field and no sub claim)")

    return {
        "uid": uid,
        "nickname": str(pick("nickname") or ""),
        "domain": domain_for_realm(detected, pick("domain")),
        "realm": detected,
        "platform": str(pick("platform") or "CLI"),
        "enterpriseId": str(pick("enterpriseId") or ""),
        "accessToken": token,
        "refreshToken": str(pick("refreshToken") or ""),
        "expiresAt": normalize_epoch(pick("expiresAt")) or jwt_exp(token),
        "source": "import",
        "enabled": True,
        # Volatile state is intentionally reset - see VOLATILE_FIELDS.
        "lastError": "",
        "cooldownUntil": 0.0,
    }
