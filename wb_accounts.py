import re
import base64
import datetime
import json
import os
import random
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from wb_fingerprint import derive_id, generate_request_id, session_id
import wb_activity
import wb_atrest
import wb_catalog
import wb_identity
import wb_settings
import wb_tiers
import wb_upstream_pool
import wb_webagent
import wb_webprompts

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
WEB_ORIGIN_CN = "https://www.workbuddy.cn"
WEB_CONVERSATIONS_URL = WEB_ORIGIN + "/console/as/conversations/"
PROFILE_PATH = "/console/account"
WEB_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0")
DAILY_CHAT_MODEL = "deepseek-v4.1-flash"
DAILY_CHAT_WEB_PROMPT = "Hi"
#: 网页通道一轮最多等多久（秒）。实测一次「Hi」十几秒就跑完，留足余量。
#: 没配 settings 的账号（如单测里手工构造的）用它兜底。
WEB_TURN_TIMEOUT = int(os.environ.get("WB_WEB_TURN_TIMEOUT") or "120")
#: 网页通道至少跑几轮：第二轮留给「会话只是慢」（状态=working），一轮没跑完
#: 不代表坏了。候选序列更长时按候选数量走（每个候选一轮）。
WEB_MIN_ROUNDS = 2
#: 兼容旧调用方：早期版本用它当「总尝试次数」。现在轮数由候选序列推出。
WEB_TURN_ATTEMPTS = 3
#: 单账号总预算（秒）的默认值；0 = 不限制。与 wb_settings 同值：没配 settings
#: 的账号（单测里手工构造的）按它走。
WEB_BUDGET_DEFAULT = 600
#: 剩余预算低于这个值就不再开新的一轮：一轮至少要能等到会话真的跑起来。
WEB_BUDGET_MIN_ROUND = 30
#: 桌面腿（轻量对话）在首选模型冷却时顺延用的免费模型。网页腿不看这份名单 ——
#: 那边的回退就是面板上的候选序列。
DAILY_CHAT_DESKTOP_FALLBACKS = ("hy3", "hy4-preview-f", "hy4-preview",
                                "glm-5.3-flash")
#: 还没跑到 turn 就失败（建会话 5xx、取沙箱失败）时的退避基数（秒）。
#: 那几轮只花掉几百毫秒、不消耗任何等待预算，背靠背重试等于把全部尝试挤进
#: 同一个上游故障窗口（issue #558：8 个账号 × 6 次尝试全落进约 100 秒的抖动）。
#: 设 0 关闭退避，恢复旧行为。
WEB_RETRY_BACKOFF = float(os.environ.get("WB_WEB_RETRY_BACKOFF") or "5")
#: 单次退避上限（秒）：指数增长到它就封顶，避免把整批巡检拖太久。
WEB_RETRY_BACKOFF_MAX = float(os.environ.get("WB_WEB_RETRY_BACKOFF_MAX") or "30")


def web_retry_backoff(round_index, base=None, cap=None):
    """第 round_index 轮（从 0 起）失败后要等多久，秒；0 表示不退避。

    base 默认取 WB_RETRY_BACKOFF（设置页可改，见 wb_settings.
    daily_chat_web_retry_backoff）；cap 默认取 WB_RETRY_BACKOFF_MAX，但不会小于
    base —— 否则操作者把基数调大后会被一个更小的上限悄悄压回去。
    """
    base = WEB_RETRY_BACKOFF if base is None else base
    cap = WEB_RETRY_BACKOFF_MAX if cap is None else cap
    if base <= 0:
        return 0.0
    return min(base * (2 ** round_index), max(cap, base))
#: 上游对单个账号同时存在的「活跃会话」（status=working）有硬上限，超过就拒绝
#: 建会话：HTTP 429 / code 10105 "you have reached the maximum number of active
#: conversations(10)"。每条会话跑完必须收尾，否则每天漏一条，十天后这个账号
#: 就再也建不出会话（面板上表现为「每日活跃打卡」全部失败）。
WEB_CONVERSATION_ACTIVE_LIMIT = 10
#: 撞到上限时自动停掉的旧会话条数上限之外保留的活跃条数。留一点余量，别把用户
#: 正在网页端使用的会话也停掉。
WEB_CONVERSATION_PRUNE_KEEP = 8
#: 上游把「还在跑」的会话记成这些状态；只有它们占活跃名额。
WEB_CONVERSATION_ACTIVE_STATUSES = ("working", "creating", "pending")
#: 本进程记着的「已建未收尾」网页会话，超过这个时长就当跑过头了，由别的线程代收尾。
#: 取值是单轮上限的两倍——正常一轮最多 turn_cap 秒，翻倍后仍没注销就一定是卡住了。
WEB_LIVE_STALE_FACTOR = 2
#: 清扫「上一个进程留下的活跃会话」时只动创建超过这么久的：一轮最多几分钟，
#: 半小时还没结束的一定不是正在跑的本轮（也避免误杀另一个网关实例正在跑的会话）。
WEB_ORPHAN_SWEEP_SECONDS = 1800
#: 登记表里的条目最多留这么久。正常路径会注销，只有卡死的才会留到现在；万一某个
#: 部署根本不跑调度器（没人调 reclaim），注册新条目时按这个年龄把老条目丢掉，
#: 保证这张表不会无限长。
WEB_LIVE_REGISTRY_TTL = 24 * 3600
#: 登记表的硬上限（条）。TTL 之外的兜底：病态情况下也不会堆到几十万条。
WEB_LIVE_REGISTRY_MAX = 128


#: 本进程里「已经建出来、还没收尾」的网页会话：(uid, 会话 id) -> 登记信息。
#:
#: 正常路径靠 _daily_chat_web_once 的 finally 注销；但 finally 只有在那一轮**能
#: 返回**时才跑得掉。getaddrinfo 不吃 socket 超时，服务端也可以一点一点滴数据，
#: 于是存在「线程卡在 socket 上、永远走不到 finally」的窗口——那条会话就会一直
#: 占着上游的活跃名额，直到进程重启。这张表让**别的线程**能在超时之后替它收尾：
#: 收尾不能依赖那个卡住的线程本身。
_WEB_LIVE = {}
_WEB_LIVE_LOCK = threading.Lock()


def web_live_register(uid, conversation, model=""):
    """记下一条刚建出来、还没收尾的网页会话（供别的线程代收尾）。"""
    if not uid or not conversation:
        return
    now = time.time()
    with _WEB_LIVE_LOCK:
        # 这张表不能无限长：正常路径会注销，卡死的条目按年龄和条数兜底清掉。
        for key, item in list(_WEB_LIVE.items()):
            if now - item.get("started", 0) > WEB_LIVE_REGISTRY_TTL:
                _WEB_LIVE.pop(key, None)
        while len(_WEB_LIVE) >= WEB_LIVE_REGISTRY_MAX:
            oldest = min(_WEB_LIVE, key=lambda k: _WEB_LIVE[k].get("started", 0))
            _WEB_LIVE.pop(oldest, None)
        _WEB_LIVE[(str(uid), str(conversation))] = {
            "uid": str(uid), "conversation": str(conversation),
            "model": model or "", "started": now,
        }


def web_live_unregister(uid, conversation):
    """这条会话已经收尾了，从登记表里去掉。"""
    with _WEB_LIVE_LOCK:
        _WEB_LIVE.pop((str(uid), str(conversation)), None)


def web_live_snapshot(uid=None, min_age=0.0):
    """还记着的未收尾会话；给了 uid 只看这个账号，min_age 只挑跑过头的。"""
    now = time.time()
    with _WEB_LIVE_LOCK:
        items = [dict(item) for item in _WEB_LIVE.values()]
    out = []
    for item in items:
        if uid and item["uid"] != str(uid):
            continue
        if min_age and now - item["started"] < min_age:
            continue
        out.append(item)
    return out



class TLSVerifyError(RuntimeError):
    """The upstream certificate chain is not trusted by this process."""


TLS_VERIFY_HINT = (
    "TLS 证书校验失败：上游证书链里有本机不信任的证书。常见于公司代理、"
    "Clash/Surge 等 HTTPS 解密，或 macOS 上 Python 没读到系统钥匙串里的根证书。"
    "把签发该证书的根证书导出为 PEM，设置环境变量 "
    "WB_CA_BUNDLE=/path/to/ca.pem 后重启网关；也可以设置 SSL_CERT_FILE。"
)


def is_tls_verify_error(exc):
    """True for certificate/hostname verification failures.

    These are configuration errors, not transient network faults: retrying the
    same request produces the same result and hides the real cause behind a
    502. The check looks at URLError.reason as well, because urllib wraps the
    original SSLCertVerificationError there.
    """
    reason = getattr(exc, "reason", None)
    text = "%s %s %s" % (type(exc).__name__, exc, "" if reason is None else reason)
    lower = text.lower()
    return any(marker in lower for marker in (
        "certificate_verify_failed",
        "certificate verify failed",
        "self-signed certificate",
        "self signed certificate",
        "unable to get local issuer certificate",
        "unable to get issuer certificate",
        "certificate has expired",
        "hostname mismatch",
    ))


def tls_verify_hint(exc=None):
    if exc is None:
        return TLS_VERIFY_HINT
    return "%s 原始错误：%s" % (TLS_VERIFY_HINT, exc)


def is_tls_verify_text(text):
    """True when an already-stringified error carries a certificate failure.

    wb_webagent.run_turn 把 live 的异常折成「<类型>: <消息>」文本，调用方拿不到
    异常对象，所以 is_tls_verify_error 够不着它。标记表是同一份，只是换成判
    字符串：网页通道沙箱入口证书挂掉（#645）走的正是这条路径。
    """
    if not text:
        return False
    return is_tls_verify_error(TLSVerifyError(str(text)))


def _retryable(exc):
    """Transient network faults worth another attempt (TLS resets, timeouts, 5xx)."""
    if is_tls_verify_error(exc):
        return False
    if isinstance(exc, ssl.SSLError):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, (TimeoutError, ConnectionResetError, ConnectionAbortedError, OSError)):
        return True
    return False


def web_origin_for(realm):
    """Web-console origin per realm (CN vs international)."""
    return WEB_ORIGIN_CN if str(realm or "").lower() == "cn" else WEB_ORIGIN


def fetch_account_profile(account, timeout=15):
    """Web-console account profile -> (uid, nickname), nothing else.

    The endpoint also returns phoneNumber and other personal fields; this
    function parses only uid and nickname, so sensitive values never enter
    logs, responses or storage. A uid mismatch against the credential raises
    (wrong-account guard).
    """
    origin = web_origin_for(getattr(account, "realm", ""))
    req = urllib.request.Request(
        origin + PROFILE_PATH, method="GET",
        headers={
            "Authorization": "Bearer " + str(account.access_token or ""),
            "Accept": "application/json, text/plain, */*",
            "x-client-platform": "web",
            "Origin": WEB_ORIGIN_CN,
            "Referer": WEB_ORIGIN_CN + "/profile/account-settings",
            "User-Agent": WEB_USER_AGENT,
        },
    )
    with urlopen(req, timeout=timeout, proxy=getattr(account, "proxy", "")) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("profile response is not an object")
    uid = str(data.get("uid") or "")
    if uid and account.uid and uid != account.uid:
        raise RuntimeError("profile uid mismatch")
    nickname = data.get("nickname")
    return uid, nickname.strip() if isinstance(nickname, str) else ""


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
    """urlopen honouring an optional per-account proxy, over a keep-alive pool.

    urllib 的 AbstractHTTPHandler.do_open 会写死 Connection: close，每个请求都得
    重新 TCP+TLS 握手（真机实测 118.0ms，是请求路径上最大的一笔开销）。默认改走
    wb_upstream_pool：按（目标, 代理串, 凭据摘要）分池复用连接——不同账号不共用
    同一条 TCP/TLS 连接，同一账号仍复用。不能逐字节对齐 urllib 语义的请求（非
    http(s) 目标、非 HTTP 代理、需要跟随的 3xx 重定向）由 PoolBypass 回退到下面
    这条原来的路，行为与改动前一致；WB_UPSTREAM_KEEPALIVE=0 时整条路径都与改动
    前相同。

    头名保真：调用方若把有序 [(name, value)] 挂在 req.wb_exact_headers 上，
    池路径会原样发这些头名（见 wb_proxy.open_upstream）；没挂的请求（billing /
    登录 / 签到等）走原来的 .title() 路径，行为不变。
    """
    if wb_upstream_pool.enabled():
        try:
            return wb_upstream_pool.urlopen(req, timeout=timeout, proxy=proxy)
        except wb_upstream_pool.PoolBypass:
            pass
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
    if last is not None and is_tls_verify_error(last):
        raise TLSVerifyError(tls_verify_hint(last)) from last
    raise last

REALM_CONFIGS = {
    "intl": {
        "name": "国际版 (Global)",
        "chat_upstream": "https://www.workbuddy.ai",
        "billing_upstream": "https://www.workbuddy.ai",
        "origin": "https://www.workbuddy.ai",
        "domain": "www.workbuddy.ai",
        # 5.8.4 实测（2026-10-16 抓包）：桌面端 UA 是三段式，末段是客户端内置
        # CLI 的版本；取值与 wb_identity.DESKTOP_UA_INTL 保持同一来源。
        "chat_ua": wb_identity.DESKTOP_UA_INTL,
        "billing_ua": "WorkBuddy/%s" % wb_identity.DESKTOP_VERSION_INTL,
        "info_filename": "workbuddy-desktop-ai.info",
        "cache_dir_name": ".workbuddy-ai",
        "has_checkin": False,
    },
    "cn": {
        "name": "国内版 (China)",
        # 默认 https://copilot.tencent.com；WB_CN_CHAT_UPSTREAM 可覆盖（见 wb_identity.CN_CHAT_BASE）。
        # domain 不随之改：它是 X-Domain 的兜底值，请求优先用账号自己的 domain。
        "chat_upstream": wb_identity.CN_CHAT_BASE,
        "billing_upstream": "https://www.codebuddy.cn",
        "origin": "https://www.codebuddy.cn",
        "domain": "copilot.tencent.com",
        # 国内版没有对应抓包，取值维持原样，但同样从 wb_identity 取，
        # 免得两处各写一份（国际版 5.8.4 对齐时就漏改过 intl 那一份）。
        "chat_ua": wb_identity.DESKTOP_UA_CN,
        "billing_ua": "WorkBuddy/%s" % wb_identity.DESKTOP_VERSION_CN,
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
RESOURCE_SUMMARY_PATH = "/billing/meter/get-user-resource-summary"
RESOURCE_FREE_PACKAGES_PATH = "/billing/meter/get-user-resource-free-packages"
RESOURCE_PAID_PACKAGES_PATH = "/billing/meter/get-user-resource-paid-packages"
CHECKIN_STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
ENTERPRISE_USAGE_PATH = "/billing/meter/get-enterprise-user-usage"
#: 静态 API Key 上企业用量走 /v2 前缀（实测）：不带 /v2 的同一路径被 APISIX
#: 直接回 401 的 HTML 页，带 /v2 才进业务层。OAuth 账号维持原路径不动——
#: 那一条已经在生产跑着，没有理由陪着改。
ENTERPRISE_USAGE_PATH_V2 = "/v2/billing/meter/get-enterprise-user-usage"

#: 静态 API Key 的前缀。CodeBuddy 平台签发的 key 形如 ck_<id>.<secret>；
#: pt_ 是同一套签发体系里见过的另一种前缀。这类凭证不是 JWT：不能刷新、
#: 没有过期时间、也没有登录账号的个人身份，额度走企业口径端点。
API_KEY_PREFIXES = ("ck_", "pt_")

#: 静态 API Key 账号的 source 标记。与 OAuth / cockpit / import / desktop-app
#: 并列，落盘可读，导出导入照常携带。
SOURCE_API_KEY = "api-key"


def api_key_id(token):
    """从 ck_<id>.<secret> 里取 <id> 段（剥前缀、截到首个点之前）。

    只保留 [A-Za-z0-9_-]：这个值会拼进凭证文件名，净化是路径穿越的防线。
    """
    text = str(token or "")
    for prefix in API_KEY_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    if "." in text:
        text = text.split(".", 1)[0]
    return re.sub(r"[^A-Za-z0-9_-]", "", text)


def is_api_key_token(token):
    """该凭证是不是静态 API Key（ck_/pt_ 前缀）。

    判据只有前缀：静态 Key 没有本地可校验的签名，唯一可靠的验证是拿它打一次
    上游（见 Account.probe_api_key）。
    """
    return str(token or "").startswith(API_KEY_PREFIXES)

#: 账号档位（付费 / 免费）的有效期。计费摘要里的 `IsPaidUser` 只在真正
#: 刷新时读一次，一天一次足够：档位不会分钟级变化，而这是国际版唯一一次
#: 额外的计费请求（国内版的详细积分路径本来就在打同一个接口，见 #505）。
TIER_TTL_SECONDS = 24 * 3600.0

LOGIN_PENDING = 11217
LOGIN_TTL_SECONDS = 600
USER_AGENT = REALM_CONFIGS['intl']['chat_ua']

#: 签到文案判据。10001 在本仓库**不是**「签到成功」专属码：活动补偿的
#: 「每人限领一次，您已领取过」与 content rejected 都用它，上游还会在
#: code=0 下回「签到失败，已签到天数不足 7 天」。原先 `code == 0 or
#: code == 10001` 一律当成功，把「请求处理中，请勿重复操作」这种明确没签成
#: 的回执也记成了成功（实测一轮 27 个账号里 4 条，当天真签成 8 个）。
CHECKIN_PENDING_HINTS = ("处理中", "请勿重复操作", "系统繁忙", "稍后再试")
CHECKIN_ALREADY_HINTS = ("已签到",)
CHECKIN_FAILURE_HINTS = ("失败",)


def checkin_state(code, msg):
    """把签到回执归成 checked / already / pending / failed。

    只有 checked / already 能写 last_checkin：写早了（旧实现拿到 HTTP 200 就
    先写盘）会让当天 can_checkin() 恒为 False，这次失败被永久放弃，9 点与
    21 点两轮再也不会重试，面板还显示「今日已领」。
    """
    text = str(msg or "")
    if any(k in text for k in CHECKIN_PENDING_HINTS):
        return "pending"
    if code == 0:
        # 「失败」是唯一的硬词：CHECKIN_FAILURE_HINTS 之外的口径太宽，
        # 「签到成功，距离 7 天奖励还不足」里的「不足」会把成功判成失败。
        return "failed" if any(k in text for k in CHECKIN_FAILURE_HINTS) else "checked"
    if code == 10001:
        return "already" if any(k in text for k in CHECKIN_ALREADY_HINTS) else "failed"
    return "failed"


#: 国际版桌面端的版本号。以前这里硬编码 5.5.2，和 wb_identity 的
#: DESKTOP_VERSION_INTL 各写一份，改版本时容易只改一处（5.8.4 对齐时
#: 就漏了这个常量）。现在统一从 wb_identity 取，保持单一来源。
DEFAULT_UA_VERSION = wb_identity.DESKTOP_VERSION_INTL

def get_realm_config(realm):
    return REALM_CONFIGS.get(realm) or REALM_CONFIGS["intl"]


def tier_from_summary(data):
    """Normalise one billing-summary payload into a tier record (#505).

    Both realms answer the same fields on `/billing/meter/get-user-resource-summary`;
    intl omits `ProTrialStatus`. `SubscriptionPackageCode` was empty on every
    account measured, so the package *name* is what gets displayed.

    Returns None when the payload carries no `IsPaidUser`: "no answer" must
    never be recorded as "free".
    """
    if not isinstance(data, dict) or "IsPaidUser" not in data:
        return None
    try:
        trial = int(data.get("ProTrialStatus") or 0)
    except (TypeError, ValueError):
        trial = 0
    return {
        "paid": bool(data.get("IsPaidUser")),
        "package": str(data.get("SubscriptionPackageName") or ""),
        "package_code": str(data.get("SubscriptionPackageCode") or ""),
        "protected_price": bool(data.get("IsProtectedPriceUser")),
        "trial": trial,
        "checked_at": time.time(),
        "source": "billing",
    }


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

#: 「限额联动」只覆盖免费模型（用户口径，2026-10-10）：只有这些模型的 6004
#: 撞线才做「记录 → 跨重启禁用」；付费模型不参与——它们的消耗按积分计费，
#: 不在这套 24h 窗口配额里。键与 Account.realm 的取值一致（intl / cn），
#: 国内版的 deepseek-v4.1-flash 是收费模型（x0.11），不在名单内。
FREE_CAP_MODELS = {
    "intl": frozenset({"hy4-preview-f", "hy3", "deepseek-v4.1-flash"}),
    "cn": frozenset({"hy4-preview-f", "hy3"}),
}


def normalise_disabled_models(raw):
    """把「人工禁用的模型名单」归一成干净的 list[str]。

    供账号档加载与面板写入两条路共用，保证「内存里的判断」和「落盘的内容」
    口径一致：保留非空字符串、按小写归一（与 is_model_banned 同口径）、去重、
    保持输入顺序。非列表输入一律当作「没配」返回空列表——坏数据不至于让账号
    加载失败。
    """
    out = []
    if not isinstance(raw, (list, tuple)):
        return out
    seen = set()
    for mid in raw:
        if not isinstance(mid, str):
            continue
        m = mid.strip().lower()
        if not m or m in seen:
            continue
        seen.add(m)
        out.append(m)
    return out


def is_free_cap_model(realm, model):
    """该「账号区域 × 模型」是否在限额联动的范围内。

    未知区域或空模型一律不在：范围判断必须失败关闭，宁可漏记一次也不把
    付费模型拉进持久化禁用。
    """
    return bool(model) and str(model) in FREE_CAP_MODELS.get(str(realm or ""), ())

#: 上游用一个远超任何真实计费周期的抵扣截止时间表示「不会过期」。实测
#: （2026-10-08，31 行真实包数据）Free Plan Subscription 与个人体验版的
#: DeductionEndTime 落在 2034/2035 年，而真实包周期是 14 天或 1 个月。超过
#: 这个天数就按「不过期」处理，不再参与到期倒计时与临期判定。
EXPIRY_SENTINEL_DAYS = 730


def deduction_end_text(acc):
    """包的「抵扣截止时间」——积分到这个点就不能再抵扣，即作废时刻。

    取上游的 DeductionEndTime（epoch 毫秒）。上游没给就返回空串，调用方
    回退到 CycleEndTime。实测 Bonus Pack / 裂变包 / 体验版的 CycleEndTime
    与 DeductionEndTime 完全一致，但免费包/体验版这类包的 CycleEndTime 只是
    每月的计费周期边界，两者能差 8 年——所以到期要认这个字段。
    """
    raw = acc.get("DeductionEndTime")
    if raw in (None, "", 0, "0"):
        return ""
    try:
        stamp = float(raw)
    except (TypeError, ValueError):
        return ""
    if stamp > 1e11:
        stamp /= 1000.0
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))
    except (OSError, ValueError):
        return ""


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

_DEVICE_TOKEN_CACHE = {"key": None, "token": "", "at": 0.0}
_DEVICE_TOKEN_LOCK = threading.Lock()
_DEVICE_TOKEN_TTL = 300
_DEVICE_TOKEN_MAX_BYTES = 1024


def resolve_device_token(accounts_dir):
    """X-Device-Token fallback: a literal value, or a desktop token file.

    Mirrors the panel's device_token.go: the desktop client writes its
    token to a file, the gateway reads it (max 1KB, trimmed) and caches
    the result for five minutes so the header hot path stays cheap.
    Failures degrade to an empty token instead of breaking a request.
    """
    try:
        cfg = wb_settings.upstream_config(accounts_dir)
    except Exception:
        return ""
    literal = str(cfg.get("device_token") or "").strip()
    path = str(cfg.get("device_token_file") or "").strip()
    key = literal or path
    if not key:
        return ""
    now = time.time()
    with _DEVICE_TOKEN_LOCK:
        if (_DEVICE_TOKEN_CACHE["key"] == key
                and now - _DEVICE_TOKEN_CACHE["at"] < _DEVICE_TOKEN_TTL):
            return _DEVICE_TOKEN_CACHE["token"]
    token = literal
    if not token and path:
        try:
            if os.path.getsize(path) <= _DEVICE_TOKEN_MAX_BYTES:
                with open(path, encoding="utf-8") as fh:
                    token = fh.read().strip()
        except Exception:
            token = ""
    with _DEVICE_TOKEN_LOCK:
        _DEVICE_TOKEN_CACHE.update({"key": key, "token": token, "at": now})
    return token


def next_local_4am(now=None):
    """Epoch of the next local 04:00.

    The daily reset that puts CN credits back happens overnight, so the
    04:00 wall is what an out-of-credits park waits for. A timestamp already
    past 04:00 rolls to tomorrow, so a deadline is never reused in place.
    """
    now = time.time() if now is None else now
    lt = time.localtime(now)
    stamp = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                         4, 0, 0, 0, 0, -1))
    if stamp <= now:
        stamp += 86400
    return stamp


# Account-level failure governance. A repeatedly soft-limited credential
# backs off exponentially instead of being retried on every request; a
# credential that keeps failing hard trips a breaker; unknown failures degrade
# it for a while. The counters live on Account, these helpers only turn a
# count into seconds (panel project's pool semantics).
SOFT_RATE_BASE = 600.0          # first verified account-level 429: 10 minutes
SOFT_RATE_MAX = 7200.0          # ... doubling up to 2 hours
# An *unscoped* 429 - the body names no reset clock, so nothing in it says
# whether the limit is on the credential or on one model - buys a window on that
# one model instead, shorter and capped at the credential ladder's first tier.
# The bare "usage exceeds frequency limit" body is the most common 429 the
# upstream sends (229 of the 263 in the incident environment), so it must not be
# able to park a credential for two hours.
SOFT_RATE_UNVERIFIED_BASE = 60.0
SOFT_RATE_UNVERIFIED_MAX = 600.0
BREAKER_THRESHOLD = 3           # consecutive hard failures before the breaker
BREAKER_COOLDOWN = 1800.0       # first breaker window: 30 minutes
BREAKER_COOLDOWN_MAX = 21600.0  # ... doubling up to 6 hours
# 上面那个「连续」必须有时间含义：老实现只认次数、不认间隔，fails 只有成功才
# 归零，于是同一条凭据几小时前的一次 502 + 几天前的一次超时 + 刚才一次抖动就凑成
# 「连续 3 次」，把账号关 30 分钟——而且关得越久下次越容易再凑满（1800 → 3600 →
# 7200…）。生产上的表现就是「明明测了能用，它还熔断」。现在两次失败间隔超过这个
# 窗口就重新从 1 数起，连续才真的是连续。可用 WB_FAIL_WINDOW 覆盖，设 0 关闭衰减
# （回到旧的累计行为）。
try:
    FAIL_WINDOW = max(0.0, float(os.environ.get("WB_FAIL_WINDOW") or 600.0))
except (TypeError, ValueError):
    FAIL_WINDOW = 600.0
DEGRADE_THRESHOLD = 5           # consecutive unknown failures before degrading
DEGRADE_COOLDOWN = 600.0        # first degrade window: 10 minutes
DEGRADE_COOLDOWN_MAX = 7200.0   # ... doubling up to 2 hours
# 上游账号级风控限制（HTTP 403 / code 11140「账号存在异常行为，已被限制使用」）
# 的冷却时长。上游按小时到工作日计：社区复盘里官方工单给的口径是「临时风控
# 1-3 个工作日自动解除」，期间官方客户端同样报这个错，所以这不是内容审核、
# 也不能靠换提示词或换号解决。取 2 小时作第一档，到期再试一次即可。
# 与其它冷却不同，这个期限是**持久化**的（见 note_restricted）。
RESTRICTED_ACCOUNT_COOLDOWN = 7200.0


def _exponential_backoff(count, base, cap, offset):
    """base * 2 ** (count - offset), clamped to cap and to 20 steps."""
    step = max(0, int(count) - int(offset))
    return min(float(base) * (2 ** min(step, 20)), float(cap))


def soft_backoff(streak):
    """Cooldown seconds for `streak` consecutive account-level soft limits."""
    return _exponential_backoff(streak, SOFT_RATE_BASE, SOFT_RATE_MAX, 1)


def unverified_soft_backoff(streak):
    """Cooldown for `streak` consecutive *unscoped* soft limits, on one model.

    Repetition still escalates, but from 60s and only up to the credential
    ladder's first tier (600s) - see SOFT_RATE_UNVERIFIED_MAX.
    """
    return _exponential_backoff(streak, SOFT_RATE_UNVERIFIED_BASE,
                                SOFT_RATE_UNVERIFIED_MAX, 1)


def breaker_backoff(fails):
    """Breaker cooldown for `fails` consecutive hard failures."""
    return _exponential_backoff(fails, BREAKER_COOLDOWN, BREAKER_COOLDOWN_MAX,
                                BREAKER_THRESHOLD)


def degrade_backoff(fails):
    """Degrade window for `fails` consecutive unknown failures."""
    return _exponential_backoff(fails, DEGRADE_COOLDOWN, DEGRADE_COOLDOWN_MAX,
                                DEGRADE_THRESHOLD)


def _clean_candidates(models):
    """Order-preserving de-duplication of a candidate list.

    Both the panel's list and the automatic one can contain the same id twice
    (the preferred model is also a free model), and an empty id must never
    reach the upstream. Kept separate from wb_settings._clean_models because
    this one must not truncate: the caller decides how many rounds it walks.
    """
    seen = set()
    ordered = []
    for model in models or []:
        name = str(model or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    return ordered


def _model_credit_rank(model, realm):
    """The catalogue multiplier as a number; unknown models sort last.

    "x0.00" is the free tier, "x0.29" a cheap paid model, "x6.67" an expensive
    one. The live catalogue is not available here (wb_accounts must not import
    wb_proxy), so this reads the bundled table: every model the fallback
    combination can name is in it, and an unknown id still gets a stable slot
    after the known ones instead of being dropped.
    """
    table = (wb_catalog.STATIC_CN_MODELS if realm == "cn"
             else wb_catalog.STATIC_INTL_MODELS)
    for item in table:
        if item.get("id") != model:
            continue
        raw = str(item.get("credits") or "").strip().lower()
        if raw.startswith("x"):
            try:
                return float(raw[1:])
            except ValueError:
                pass
        break
    return float("inf")


def _parse_created_at(value):
    """把上游的 createdAt 解析成 epoch 秒；解析不出来返回 0.0。

    上游给的是带时区的 ISO 8601（如 2026-10-22T09:00:00+08:00）。解析不出来时
    返回 0.0，调用方一律当「判断不了年龄」跳过——宁可漏收一条，也不要误停用户
    或另一个实例正在跑的会话（漏掉的由撞 10105 时的 web_conversation_prune 兜）。
    """
    text = str(value or "").strip()
    if not text:
        return 0.0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp.timestamp()


def _active_conversation_limit(status, detail):
    """Whether an upstream rejection means "no free active-conversation slot".

    The console API answers HTTP 429 with code 10105 and a message like
    "CreateConversation: 10105:you have reached the maximum number of active
    conversations(10), please c...". Matching the code alone is enough, but the
    message is checked too so a proxy that rewrites the body still gets the
    recovery path instead of a bare 429.
    """
    if status != 429:
        return False
    text = str(detail or "")
    return ("10105" in text
            or "maximum number of active conversations" in text.lower())


class Account(object):
    def __init__(self, data, path=None):
        data = data or {}
        self.path = path
        # Directory of the credential file, so header-time helpers (the
        # optional X-Device-Token) can resolve settings without a pool lookup.
        self.accounts_dir = os.path.dirname(path) if path else ""
        token = str(data.get("accessToken") or "")
        # 静态 API Key 与 OAuth token 是两条鉴权路径：前者不是 JWT，没有
        # refreshToken / 过期时间 / 个人身份，也不该走任何刷新与签到逻辑。
        # 先定这个标志，后面所有「是不是 JWT 账号」的分支都读它。
        self.is_api_key = is_api_key_token(token)
        # 静态 Key 解析不出 sub 声明，uid 只能来自落盘字段或 key 的 <id> 段
        # （ck_<id>.<secret> → apikey-<id>，与 Go 版面板同一口径）。
        uid = str(data.get("uid") or "").strip()
        if not uid and self.is_api_key:
            derived = api_key_id(token)
            uid = ("apikey-" + derived) if derived else ""
        self.uid = uid or jwt_uid(token)
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
        if self.is_api_key:
            # 静态 Key 只在国内版出口有效（国际版不认这套签发），落盘写着
            # intl 也不能照单全收——那会让每个请求打到不支持它的上游。
            self.realm = "cn"
        else:
            self.realm = str(data.get("realm") or detect_realm_from_token(token, self.domain))
        if not self.domain:
            self.domain = get_realm_config(self.realm)["domain"]
        self.platform = str(data.get("platform") or "CLI")
        # 出站身分读回凭证档里保存的值：面板手动切换与 429 自动切换都会经由
        # save() 写进凭证档（to_dict() 序列化的是当下身分），所以重启后接着用
        # 上次实际生效的那条通道，而不是每次都回到预设。
        # 凭证档没有这个栏位、或值不合法时 normalize_product() 会回退到
        # WorkBuddy 独立桌面端 (workbuddy)，升级前就已存在的帐号行为不变。
        self.product = wb_identity.normalize_product(data.get("product"))
        self.enterprise_id = str(data.get("enterpriseId") or "")
        self.access_token = token
        self.refresh_token = str(data.get("refreshToken") or "")
        # 静态 Key 永不过期（expiresAt 恒 0）。写 0 而不是「解 JWT 得到的 0」
        # 是有意的：ready() 里 exp 为 0 表示「不按时间淘汰」。
        self.expires_at = (0 if self.is_api_key
                           else normalize_epoch(data.get("expiresAt")) or jwt_exp(token))
        # 企业账号登录时切到企业空间（token_source=enterprise_switch），企业 id
        # 只写在 token 里，凭据文件的 enterpriseId 往往是空的。不回填的话每个
        # 请求都会带上 X-No-Enterprise-Id: 1，企业目录/配额分支走错。
        if not self.enterprise_id:
            self.enterprise_id = self.jwt_enterprise_id(token)
        self.added_at = data.get("addedAt") or time.time()
        self.source = str(data.get("source")
                          or (SOURCE_API_KEY if self.is_api_key else "oauth"))
        self.proxy_slot = str(data.get("proxySlot") or "").strip()
        self.proxy_legacy = str(data.get("proxy") or "").strip()
        # Runtime-resolved value; recomputed by AccountPool.apply_proxy_slots().
        self.proxy = self.proxy_legacy
        self.enabled = data.get("enabled", True)
        # Task participation is independent of API dispatch: an account can be
        # disabled for traffic but still run check-in / growth / travel / daily
        # activity (issue #521). Accounts written before this field existed
        # follow `enabled`, so a disabled account stays fully off until the
        # operator opts it back in.
        raw_tasks_enabled = data.get("tasksEnabled")
        self.tasks_enabled = (bool(raw_tasks_enabled)
                              if raw_tasks_enabled is not None
                              else bool(self.enabled))
        # 手动调度权重（面板按账号设置，1 = 不干预）。随凭证档持久化：这是
        # 用户的明确分派意图，不该在重启后悄悄回到默认。非法值一律回落 1，
        # 坏数据不至于让账号加载失败。
        try:
            self.weight = max(1, min(100, int(data.get("weight") or 1)))
        except (TypeError, ValueError):
            self.weight = 1
        # 账号备注（面板自填，比如「批量」/「自用」）：随凭证档持久化，导出
        # 与导入照常携带。只做展示，不参与任何调度判断；非字符串一律视为没
        # 写过，超长截断，别让一条长备注把账号行撑爆。
        raw_note = data.get("note")
        self.note = (raw_note if isinstance(raw_note, str) else "").strip()[:200]
        # 该账号**人工**禁用的模型名单（面板 / 接口配置）。与 weight、note 同款：
        # 随凭证档持久化 —— 这是使用者的明确意图，不该在重启后悄悄回到默认。
        #
        # 与 model_cooldowns / model_caps 那些**自动**节流区分开：那些表达「暂时
        # 不该用」（限流或额度撞线的恢复窗口），这里表达「这个号就别碰这几个
        # 模型」。典型用途：某个号在某些模型上有授权/合规限制，但同一个模型还想
        # 走别的号——那就禁这一对组合，而不是把整个号停掉。
        #
        # 模型 id 统一**小写**存储与比较（与 is_model_banned 的口径一致），
        # 只做精确匹配不做前缀：行为可预期，不会误伤同前缀的其他模型。
        raw_deny = data.get("disabledModels")
        self.disabled_models = normalise_disabled_models(raw_deny)
        # Live error state, never restored from the credential file: the label
        # describes the last upstream answer, not the credential. It used to be
        # persisted and read back, so a 429 that had long since recovered came
        # back on the panel after every restart - a successful request only
        # clears it in memory, and the file is rewritten on unrelated events, so
        # a stale value could sit on disk for days. Runtime-only, like
        # model_cooldowns below; see RUNTIME_ONLY_FIELDS and VOLATILE_FIELDS.
        self.last_error = ""
        # Full upstream body behind `last_error`, for the dashboard tooltip. Kept
        # in its own runtime-only field so the visible label keeps the truncation
        # it has always had; never persisted, never exported.
        self.last_error_detail = ""
        self.cooldown_until = 0.0
        # 运行时标记：上游返回 401 或 token 为空时置为 True，提示需重新登录
        self.token_expired = bool(not self.access_token)
        # 上游账号级风控限制（403 / code 11140「账号存在异常行为，已被限制
        # 使用」）的解除时刻。与上面那些节流窗口不同，这一条**持久化**：上游的
        # 限制按小时到工作日计，网关重启 / 换身分 / 刷新 token 都不能让它重新
        # 入池——否则同一台设备上的账号会被一个个送进风控（见 note_restricted
        # 与社区复盘「解封前严禁换号继续打同一设备」）。
        self.restricted_until = 0.0
        try:
            self.restricted_until = max(0.0, float(data.get("restrictedUntil") or 0))
        except (TypeError, ValueError):
            self.restricted_until = 0.0
        # Per-model throttling. Upstream rate limits (code 6004 "usage exceeds
        # frequency limit") apply to ONE model for one account, not to the whole
        # account: other models keep working. Cooldown the offending model only,
        # otherwise a single throttled model blackholes every request on the pool.
        # Deliberately runtime-only (not persisted): see VOLATILE_FIELDS.
        self.model_cooldowns = {}
        # 每个模型冷却窗口是**什么时候挂上的**（model -> epoch）。model_cooldowns
        # 只留恢复时刻，起始时刻以前被丢掉：面板因此无法如实说「限流时刻」，只能
        # 用「—」占位，把 60s 的短时 parking 和几小时的 6004 撞线画成一样。与
        # model_cooldowns 同款运行时态：重启即清零（6004 的撞线时刻另有持久化的
        # model_caps["at"]）。
        self.model_cooldowns_at = {}
# 402 (out of credits): a hard park until the next local 04:00,
        # instead of a short cooldown that would retry an empty account all
        # day. Runtime-only, like the other throttle windows; a balance
        # refresh that shows credits again lifts it early.
        self.balance_until = 0.0
# Account-level failure streaks; see the *_backoff() helpers above.
        # All runtime-only, like the other throttle windows: a restart clears
        # them and the account gets a clean slate.
        self.soft_streak = 0
        # Unscoped soft 429s keep their own counter: the two ladders must not
        # feed each other (see note_unscoped_rate).
        self.unscoped_streak = 0
        self.fails = 0
        self.degrade_count = 0
        self.breaker_until = 0.0
        self.degrade_until = 0.0
        # 最近一次记失败的时刻：FAIL_WINDOW 用它判断「连续」是否还成立。
        self.last_fail_at = 0.0
        # 最近一次软限流（429）的时刻：两条软限流阶梯用同一个时间窗。
        self.last_soft_at = 0.0
        self.credits = data.get("credits") or None
        # 账号档位，来自计费摘要（#505）：{paid, package, package_code,
        # protected_price, trial, checked_at, source}。企业账号不写——上游对
        # 它们的 IsPaidUser 语义还没验证过（见 _fetch_tier_summary）。
        self.tier = data.get("tier") if isinstance(data.get("tier"), dict) else None
        # Whether the upstream says this is a paid (PRO) account. The billing
        # summary is authoritative once it has answered (#505); otherwise fall
        # back to the last credits snapshot. An account that never refreshed is
        # treated as free, so a PRO-only model is not handed to it before the
        # tier is known.
        paid = self.tier_paid()
        if paid is None:
            paid = bool(self.credits.get("is_paid_user")
                        if isinstance(self.credits, dict) else False)
        self.is_paid_user = paid
        self.last_checkin = data.get("lastCheckin") or None
        self.last_daily_chat = data.get("lastDailyChat") or None
        # 国内版每天一次的「对话活跃上报」（点亮官方 growth 连登/热力墙）上次
        # 成功的时刻；与 lastCheckin 同款：只按「今天成功过没有」做闸门。
        self.last_activity_report = data.get("lastActivityReport") or None
        # 上游「窗口额度用满」的记录（429 / code 6004）：model -> {"at": 撞线时刻,
        # "reset": 上游给的恢复时刻}。**持久化**（与其它节流窗口不同）：重启后
        # 仍然知道这个组合在恢复时间内，不用等下一次撞线才重新发现。
        # 只有带重置时刻的 6004 才记——那才是「额度用满」的可判定证据；
        # 其余 429（无重置时刻的软限流）不在这里。记录范围限免费模型
        # （FREE_CAP_MODELS，见 note_model_cap）。
        self.model_caps = {}
        for mid, entry in (data.get("modelCaps") or {}).items():
            if not isinstance(entry, dict):
                continue
            # 范围外的模型不得被记 cap：旧版本存下的（或手改的）范围外条目
            # 在加载时就丢掉，免得 restore_model_caps() 把它们又挂成禁用。
            if not is_free_cap_model(self.realm, mid):
                continue
            try:
                at = float(entry.get("at") or 0)
                reset = float(entry.get("reset") or 0)
            except (TypeError, ValueError):
                continue
            if at > 0 and reset > 0:
                self.model_caps[str(mid)] = {"at": at, "reset": reset}
        # Low-credit guard: once the balance reaches this level the account
        # stops being handed out, so it never drops to zero (a zero balance is
        # what makes the upstream start sending nagging SMS). Resolved from the
        # global setting by AccountPool.apply_reserve_credits(); 0 disables it.
        self.reserve_credits = 0
        # Expiring-credits preference: while this account's soonest-expiring
        # credit package is inside this many days, the pool hands the account
        # out before accounts whose credits lapse later, so credits about to
        # be written off get spent first. Resolved from the global setting by
        # AccountPool.apply_expiring_window(); 0 disables the preference.
        self.expiring_window_days = 0
        # Daily token guard: an account that already burned this many tokens
        # today stops being handed out, so a client that would burn the rest
        # of the day's quota rotates to another account instead of hitting
        # the upstream wall. Resolved from the global setting by
        # AccountPool.apply_daily_token_limit(); 0 disables the guard.
        # daily_tokens_today stays None until the proxy has folded the usage
        # log at least once, so a fresh process never parks anyone on an
        # unknown count.
        self.daily_token_limit = 0
        self.daily_tokens_today = None
        # Daily credit guard: paid models only. Once today's counted spend
        # reaches the limit the account keeps serving models the catalogue
        # marks free ("x0.00") and is skipped for everything else, so the
        # free tier never goes dark just because the balance is capped.
        # Resolved from the global setting by
        # AccountPool.apply_daily_credit_limit(); 0 disables the guard.
        self.daily_credit_limit = 0
        self.daily_credits_today = None
        # The realm's free model ids, pushed by the same apply call. Empty
        # until then, and an unknown model counts as paid, so the guard
        # fails closed instead of leaking spend through unseen names.
        self.free_models = frozenset()
        # Per-model daily guard: once ONE model burned the configured tokens
        # today the account stops being handed out for that model only -
        # every other model keeps working. Resolved from the global setting
        # by AccountPool.apply_model_daily_token_limit(); 0 disables it.
        self.model_daily_token_limit = 0
        self.model_daily_tokens = None
        # Serialise token refresh and file writes. Request threads, /health,
        # dashboard polls and the scheduler can all reach refresh()/save() for
        # the same account at once; without a lock the upstream rotates the
        # refresh token concurrently and the last writer wins, so a freshly
        # minted token can be overwritten by a stale snapshot.
        self._refresh_lock = threading.Lock()
        self._save_lock = threading.Lock()
        # Guards fetch_credits(). The background refresher and the sign-in /
        # daily-activity tasks all call it, and two overlapping fetches would
        # each spend an upstream billing call and then race to publish the
        # result. Separate from _refresh_lock (token refresh) and _save_lock
        # (file write) so the three never nest into each other.
        self._credits_lock = threading.Lock()
        # Guards the local estimate written by consume_credit(). Kept apart
        # from _credits_lock because that one is held across a network call:
        # a request finishing while a refresh is in flight must not wait for
        # the upstream round-trip just to subtract its own spend.
        self._balance_lock = threading.Lock()
        # The dashboard snapshots this state while request threads update it.
        # Keep it separate from _refresh_lock, which spans network requests.
        self._throttle_lock = threading.Lock()

    def to_dict(self):
        """Full view of the account, including live state.

        This is what an export is built from, so it carries the live error and
        cooldown for inspection. The local credential file must not: save()
        strips RUNTIME_ONLY_FIELDS before writing.
        """
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
            # 静态 API Key 账号：面板据此渲染「API Key」标签并隐藏签到/任务，
            # 导入导出也靠它把账号还原成同一类型。
            "apiKey": bool(self.is_api_key),
            "proxySlot": self.proxy_slot,
            "proxy": self.proxy_legacy,
            "enabled": self.enabled,
            "tasksEnabled": bool(self.tasks_enabled),
            "weight": self.weight,
            "note": self.note,
            "disabledModels": list(self.disabled_models),
            "lastError": self.last_error,
            "cooldownUntil": self.cooldown_until,
            # 持久化：上游风控限制按小时到工作日计，重启后仍然成立（见
            # note_restricted）。导出的文档会带上它供查看，导入侧
            # normalise_import_row() 不认这个字段，换机器不会把账号一起停掉。
            "restrictedUntil": self.restricted_until,
            "credits": self.credits,
            # 账号档位（#505）。持久化：一天一次的计费摘要结论，重启后仍然
            # 有效；导入到另一台机器时按 VOLATILE_FIELDS 丢掉，不把上一台
            # 机器的档位当成结论。
            "tier": self.tier,
            "lastCheckin": self.last_checkin,
            "lastDailyChat": self.last_daily_chat,
            "lastActivityReport": self.last_activity_report,
            # 持久化：6004 的恢复时间是上游给的事实，重启后仍然成立（见 __init__）。
            "modelCaps": {mid: dict(entry) for mid, entry in self.model_caps.items()},
        }

    def _throttle_snapshot(self, now):
        """Return one consistent view of account and per-model cooldowns."""
        with self._throttle_lock:
            error = self.last_error
            detail = self.last_error_detail
            deadline = max(self.cooldown_until, self.balance_until,
                           self.breaker_until, self.degrade_until,
                           self.restricted_until)
            restricted = self.restricted_until
            active = [(model, until) for model, until in self.model_cooldowns.items()
                      if until > now]
        active.sort(key=lambda pair: (pair[1], pair[0]))
        models = []
        for model, until in active:
            entry = {"model": model, "expiresAt": int(until)}
            # 这个窗口是何时挂上的：面板据此把「限流时刻」如实画出来，而不是拿
            # 「—」占位（临时 429 与 6004 撞线都由此有真实的时间语义）。
            started = self.model_cooldowns_at.get(model)
            if started:
                entry["startedAt"] = int(started)
            # 撞线时间只在 cap 还在恢复期内时带上：它同时说明这次禁用是
            # 「额度用满」（6004）而不是一次临时限流，面板据此显示两段时间。
            if self.cap_until(model, now) > 0:
                entry["cappedAt"] = int(self.model_caps[model]["at"])
            models.append(entry)
        return error, deadline, models, detail, restricted

    def model_cooldowns_snapshot(self):
        """Active model cooldowns, ordered by recovery time (epoch seconds)."""
        return self._throttle_snapshot(time.time())[2]

    def public(self):
        exp = self.expires_at or jwt_exp(self.access_token)
        now = time.time()
        is_token_expired = bool(getattr(self, "token_expired", False) or (exp and exp <= now) or not self.access_token)
        last_error, deadline, models, last_error_detail, restricted = \
            self._throttle_snapshot(now)
        return {
            "uid": self.uid,
            "nickname": self.nickname or (self.uid[:8] if self.uid else "?"),
            "domain": self.domain,
            "realm": self.realm,
            "platform": self.platform,
            "product": self.product,
            "enterpriseId": self.enterprise_id,
            "enabled": bool(self.enabled),
            "tasksEnabled": bool(self.tasks_enabled),
            "weight": int(self.weight),
            "note": self.note,
            "disabledModels": list(self.disabled_models),
            "source": self.source,
            "apiKey": bool(self.is_api_key),
            "credentialMask": self.credential_mask(),
            "proxySlot": self.proxy_slot,
            "proxy": self.proxy,
            "expiresAt": exp,
            "expiresIn": "expired" if is_token_expired else (_human_delta(exp - time.time()) if exp else None),
            "tokenExpired": is_token_expired,
            "needRelogin": is_token_expired,
            "hasRefreshToken": bool(self.refresh_token),
            "lastError": last_error,
            "lastErrorDetail": last_error_detail or None,
            "inCooldown": deadline > now,
            "cooldownFor": round(max(0.0, deadline - now)) or None,
            # 上游账号级风控限制（403 / 11140）：面板据此把账号标成「风控限制」
            # 而不是普通冷却，并提示去官方渠道申诉而不是换号重试。
            "restrictedFor": round(max(0.0, restricted - now)) or None,
            "modelCooldowns": models,
            "softStreak": int(self.soft_streak),
            "unscopedStreak": int(self.unscoped_streak),
            "breakerFor": round(max(0.0, self.breaker_until - now)) or None,
            "degradeFor": round(max(0.0, self.degrade_until - now)) or None,
            # 连续失败计数（离熔断还差几次）与最近一次失败的时间：面板用它解释
            # 「为什么这个账号被关了」，也是「一键测试」判定成功/失败的依据。
            "fails": int(self.fails),
            "failWindow": int(FAIL_WINDOW),
            "lastFailFor": (round(max(0.0, now - self.last_fail_at)) if self.last_fail_at else None),
            "addedAt": self.added_at,
            "file": os.path.basename(self.path) if self.path else None,
            "credits": self.credits,
            # 账号档位（#505）：面板在「会员类型」旁显示包名与读取时刻。
            "tier": self.tier,
            "reserveCredits": int(self.reserve_credits or 0),
            "reserveBlocked": self.reserve_blocked(),
            "dailyTokenLimit": int(self.daily_token_limit or 0),
            "dailyTokensToday": (int(self.daily_tokens_today)
                                 if isinstance(self.daily_tokens_today, int)
                                 else None),
            "dailyLimitBlocked": self.daily_limit_blocked(),
            "dailyCreditLimit": int(self.daily_credit_limit or 0),
            "dailyCreditsToday": (round(float(self.daily_credits_today), 2)
                                  if isinstance(self.daily_credits_today, (int, float))
                                  else None),
            "creditLimitReached": self.credit_limit_reached(),
            "modelDailyTokenLimit": int(self.model_daily_token_limit or 0),
            "modelDailyTokens": ({str(k): int(v)
                                  for k, v in self.model_daily_tokens.items()}
                                 if isinstance(self.model_daily_tokens, dict)
                                 else None),
            "lastCheckin": self.last_checkin,
            "lastDailyChat": self.last_daily_chat,
            "lastActivityReport": self.last_activity_report,
            "canCheckin": self.realm == "cn" and not self.uses_enterprise_billing(),
            "canDailyChat": self.realm == "intl",
            "canReportActivity": (self.realm in ("cn", "intl")
                                  and not self.uses_enterprise_billing()),
            "machineId": derive_id(self.uid, "machine"),
            "sessionId": session_id(self.uid),
        }

    def credential_mask(self):
        """脱敏后的凭证：静态 Key 显示 `ck_<id>.***`，OAuth token 不回显。

        OAuth token 是一串完整可用的凭证，面板从来没有展示过它，这里继续
        返回空串而不是「前四位 + 后四位」——那已经足够在日志里定位，也足够
        让人误以为面板可以拿回原值。
        """
        if not self.is_api_key:
            return ""
        token = str(self.access_token or "")
        head = token.split(".", 1)[0]
        return (head + ".***") if head else "***"

    def probe_api_key(self, timeout=15):
        """用企业额度端点验一次这个 Key 是否有效。

        静态 Key 没有任何本地可校验的签名字段，唯一可靠的验证就是拿它打一次
        真实端点。加账号时必须先探再落盘：探测失败的 key 进了池子，选号侧会
        反复选中它、每次失败都要冷却换号，表象是「网关时好时坏」。
        """
        if not self.is_api_key:
            return {"ok": False, "error": "not an API key"}
        url = get_realm_config("cn")["billing_upstream"] + ENTERPRISE_USAGE_PATH_V2
        headers = {
            "Authorization": "Bearer " + str(self.access_token or ""),
            # 官方 CLI 两个头都发；这里同样两个都给，省得探测通过、真跑却
            # 因为少了另一个头被判成不同的调用方。
            "X-API-Key": str(self.access_token or ""),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": get_realm_config("cn")["chat_ua"],
        }
        try:
            res = http_json(url, data=b"{}", method="POST", headers=headers,
                            timeout=timeout, proxy=self.proxy)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if not isinstance(res, dict) or res.get("code") != 0:
            return {"ok": False,
                    "error": "code %s: %s" % (res.get("code"), res.get("msg"))}
        data = res.get("data") or {}
        return {"ok": True, "used": data.get("credit"),
                "limit": data.get("limitNum"),
                "cycle_end": data.get("cycleEndTime")}

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
                payload = self.to_dict()
                # Live error/cooldown state stays out of the credential file;
                # writing it is what made a long-recovered 429 label reappear on
                # every restart (see RUNTIME_ONLY_FIELDS).
                for field in RUNTIME_ONLY_FIELDS:
                    payload.pop(field, None)
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=2)
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

    def consume_credit(self, amount):
        """Subtract what this request actually cost from the cached balance.

        The background refresher is the source of truth, but it visits one
        account per tick (30 minutes by default). On a large pool a balance
        can therefore be hours old, and reserve_blocked() would keep handing
        the account out until it reached zero (#442). Every finished request
        carries the upstream's own ``credit``, so the guard can stay honest
        between refreshes; the next fetch overwrites this estimate with the
        real number.

        Never raises: a missing/unknown balance simply stays unknown, and the
        result is clamped at zero so a single expensive request cannot make
        the panel show a negative remainder.
        """
        try:
            amount = float(amount or 0)
        except (TypeError, ValueError):
            return
        if amount <= 0:
            return
        with self._balance_lock:
            credits = self.credits
            if not isinstance(credits, dict):
                return
            try:
                remain = float(credits.get("remain"))
            except (TypeError, ValueError):
                return
            try:
                used = float(credits.get("used") or 0)
            except (TypeError, ValueError):
                used = 0.0
            try:
                size = float(credits.get("size") or 0)
            except (TypeError, ValueError):
                size = 0.0
            remain = max(0.0, remain - amount)
            used += amount
            credits["remain"] = round(remain, 2)
            credits["used"] = round(used, 2)
            if size > 0:
                credits["remain_percent"] = "%.1f%%" % (remain / size * 100.0)
                credits["used_percent"] = "%.1f%%" % (min(used, size) / size * 100.0)

    def soonest_expiring_days(self):
        """Days until the earliest credit package that still has credits left.

        None when nothing can be said: no credit data, no package with a
        usable remainder, or an end date the upstream never gave.

        Packages the upstream never really expires are skipped. The enterprise
        quota resets its allowance rather than voiding it, and the free plan /
        trial packs carry a deduction deadline years out, so both are marked
        `no_expiry` where they are built. Treating either as an expiry would
        make those accounts look permanently "about to lapse" - the free plan
        would even look urgent at every month end, when its billing cycle rolls
        over but its credits keep working.
        """
        credits = self.credits
        if not isinstance(credits, dict):
            return None
        soonest = None
        for package in (credits.get("packages") or []):
            if not isinstance(package, dict):
                continue
            # `no_expiry` is the general rule; the package_code check is a
            # belt-and-braces guard for credit blobs written before the flag
            # existed, which are still on disk until the next refresh.
            if package.get("no_expiry"):
                continue
            if package.get("package_code") == "enterprise":
                continue
            if package.get("is_expired"):
                continue
            days = package.get("days_left")
            remain = package.get("remain")
            if not isinstance(days, (int, float)) or days < 0:
                continue
            if not isinstance(remain, (int, float)) or remain <= 0:
                continue
            if soonest is None or days < soonest:
                soonest = float(days)
        return soonest

    def in_expiring_window(self):
        """True when this account should jump the dispatch queue.

        Unknown or missing credit data is never a preference: an account the
        gateway cannot judge keeps its plain round-robin turn instead of being
        pushed to the front on a guess.
        """
        try:
            window = int(self.expiring_window_days or 0)
        except (TypeError, ValueError):
            window = 0
        if window <= 0:
            return False
        soonest = self.soonest_expiring_days()
        return soonest is not None and soonest <= window

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

    def model_is_free(self, model):
        """True when the realm catalogue marks `model` as a free one."""
        return bool(model) and model in (self.free_models or ())

    def credit_limit_reached(self):
        """True when today's counted spend reached the daily credit limit.

        The account-level fact, independent of which model is asked for:
        ready() pairs it with model_is_free() so free models keep serving,
        and the dashboard shows it as the guard's state.
        """
        try:
            limit = int(self.daily_credit_limit or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            return False
        used = self.daily_credits_today
        if used is None:
            return False
        try:
            return float(used) >= limit
        except (TypeError, ValueError):
            return False

    def credit_limit_blocked(self, model=None):
        """True when the credit guard should keep this account off `model`.

        A free model stays available even after the cap is reached - that is
        the point of the guard. Without a model to judge (model is None) the
        account is not blocked here, because the caller cannot know whether
        the request would spend anything.
        """
        if not model or self.model_is_free(model):
            return False
        return self.credit_limit_reached()

    def note_model_cap(self, model, reset_at, now=None):
        """记下「这个模型的窗口额度被上游判定用满」（429 / code 6004）。

        持久化：6004 带的重置时刻是上游给的事实，跨重启仍然成立——否则重启后
        要等到下一次撞线才重新发现，而那个组合本来就在恢复时间内。返回是否新记
        （重复撞同一个窗口不算新，避免每次都重写凭证档）。

        只记免费模型（FREE_CAP_MODELS）：限额联动仅覆盖它们，范围外的模型
        即使收到 6004 也不写 cap、不跨重启禁用，只保留调用方给的运行时冷却。
        """
        if not model or not reset_at:
            return False
        if not is_free_cap_model(self.realm, model):
            return False
        now = time.time() if now is None else now
        try:
            reset = float(reset_at)
        except (TypeError, ValueError):
            return False
        with self._throttle_lock:
            old = self.model_caps.get(model)
            if old is not None and float(old.get("reset") or 0) == reset:
                # 同一个窗口的重复撞线：不用重写凭证档，但冷却要重新挂上——
                # 中间可能被换身分之类的清理顺手清掉过；挂不回来，这个窗口
                # 就一直不会被禁用（真机实测）。
                if self.model_cooldowns.get(model, 0.0) < reset:
                    self.model_cooldowns[model] = reset
                return False
            self.model_caps[model] = {"at": now, "reset": reset}
            # 恢复时间之前这个模型接不了单：挂上模型冷却，分派与账号行因此
            # 立刻反映（重启后由 restore_model_caps() 重新挂）。
            self.model_cooldowns[model] = max(self.model_cooldowns.get(model, 0.0), reset)
        return True

    def cap_until(self, model, now=None):
        """这个模型处于「上游判定额度用满」状态的截止时刻；不在其中返回 0。"""
        entry = self.model_caps.get(model)
        if not isinstance(entry, dict):
            return 0.0
        now = time.time() if now is None else now
        try:
            reset = float(entry.get("reset") or 0)
        except (TypeError, ValueError):
            return 0.0
        return reset if reset > now else 0.0

    def capped_model_names(self, now=None):
        """此刻仍在恢复时间内的模型（上游判定额度用满）。"""
        now = time.time() if now is None else now
        return sorted(mid for mid in self.model_caps
                      if self.cap_until(mid, now) > 0)

    def restore_model_caps(self, now=None):
        """重启后把仍在恢复时间内的 6004 重新挂成模型冷却。

        6004 的恢复时间跨重启有效，而模型冷却是纯内存的——不挂回来的话，重启
        会让这些组合重新接单，直到下一次撞 429 才又停。范围检查再过一道：
        加载端已丢弃范围外的 cap，这里兜住直接写 model_caps 的调用方。
        """
        now = time.time() if now is None else now
        restored = []
        with self._throttle_lock:
            for mid, entry in self.model_caps.items():
                if not is_free_cap_model(self.realm, mid):
                    continue
                try:
                    reset = float(entry.get("reset") or 0)
                except (TypeError, ValueError):
                    continue
                if reset <= now:
                    continue
                if self.model_cooldowns.get(mid, 0.0) < reset:
                    self.model_cooldowns[mid] = reset
                    restored.append(mid)
        return sorted(restored)

    def model_token_limit_blocked(self, model=None):
        """True when `model` already burned its daily token budget today.

        Only the named model is refused; the account's other models keep
        working, and an uncounted model never blocks.
        """
        try:
            limit = int(self.model_daily_token_limit or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0 or not model:
            return False
        per = self.model_daily_tokens
        if not isinstance(per, dict):
            return False
        used = per.get(model)
        if used is None:
            return False
        try:
            return int(used) >= limit
        except (TypeError, ValueError):
            return False

    def blocked_model_names(self):
        """The models currently out of budget, as a set.

        Used by AccountPool.apply_model_daily_token_limit() to log only the
        models whose state actually changed instead of one line per model
        on every refresh.
        """
        per = self.model_daily_tokens
        if not isinstance(per, dict):
            return set()
        out = set()
        for mid, used in per.items():
            if self.model_token_limit_blocked(mid):
                out.add(mid)
        return out

    def unavailable_reason(self, model=None):
        """这个账号此刻为什么接不了单；可用时返回空字符串。

        `ready()` 是个布尔判断：池子被抽干的时候它只能说「不行」，说不清有几个
        账号、各自卡在哪一条。生产上出现「明明有 9 个账号却报没有可用账号」时，
        缺的就是这句话——判定顺序与 ready() 保持一致，所以报出来的原因就是它
        拒绝的原因（唯一不覆盖的是临期凭证那条：它会真的发起刷新，诊断路径上
        不该顺带打上游）。
        """
        if not self.enabled:
            return "已停用"
        if not self.access_token:
            return "无凭证"
        if getattr(self, "token_expired", False):
            return "凭证已过期"
        # 与 ready() 同序：人工黑名单排在所有「暂时不可用」之前，所以
        # 「所有账号都禁用了这个模型」时，池子抽干的报错里会逐个号写明原因。
        if self.model_disabled(model):
            return "该模型已被人工禁用"
        if (model and not getattr(self, "is_paid_user", False)
                and wb_tiers.requires_pro(model, self.realm)):
            return "该模型仅限 PRO 账号"
        now = time.time()
        # 四种惩罚的到期时间各走各的，谁把时间推得最远就报谁：只报「冷却」
        # 会让人往 429 的方向查，而实际可能是上游连续断连触发的熔断（30 分钟
        # 起，与账号本身无关），或者余额保护这类完全不同的原因。
        penalties = [("上游风控限制", self.restricted_until),
                     ("熔断", self.breaker_until), ("降权", self.degrade_until),
                     ("余额保护", self.balance_until), ("软限流冷却", self.cooldown_until)]
        if model:
            penalties.append(("模型 %s 冷却" % model, self.model_cooldowns.get(model, 0.0)))
        label, until = max(penalties, key=lambda item: item[1])
        if until > now:
            # 剩余时间要精确到秒：还剩 42 秒时写「0 min」，读起来就是「已经该能用
            # 了却还被挡着」——生产日志里那串「软限流冷却 剩余 0 min」就是这么
            # 来的。连续次数一并带上，才看得出是在升级还是刚开始。
            streak = ""
            if label == "软限流冷却" and self.soft_streak > 1:
                streak = "（连续第 %d 次）" % self.soft_streak
            elif label.startswith("模型") and self.unscoped_streak > 1:
                streak = "（连续第 %d 次）" % self.unscoped_streak
            return "%s 剩余 %s%s" % (label, _human_left(until - now), streak)
        if self.reserve_blocked():
            return "余额低于保留线"
        if self.daily_limit_blocked():
            return "今日 Token 额度用尽"
        if self.credit_limit_blocked(model):
            return "今日积分额度用尽"
        if self.model_token_limit_blocked(model):
            return "该模型今日额度用尽"
        exp = self.expires_at or jwt_exp(self.access_token)
        if exp and exp - now <= 120:
            return "凭证已过期"
        return ""

    def model_disabled(self, model):
        """这个账号被人工禁用了该模型？空名单恒为 False。

        与自动节流区分：`model_cooldowns` / `model_caps` 表达「暂时不该用」，
        这里是使用者明确指定的「这个号就别碰这几个模型」。精确匹配（名单在
        加载时已按小写归一），不做前缀匹配。
        """
        if not model or not self.disabled_models:
            return False
        return str(model).strip().lower() in self.disabled_models

    def ready(self, model=None):
        if not self.enabled or not self.access_token or getattr(self, "token_expired", False):
            return False
        # 人工黑名单排在最前：它是静态配置（最便宜），而且在语义上优先于所有
        # 「暂时不可用」的判断 —— 被禁用的模型不该因为账号恰好恢复了就漏进来。
        if self.model_disabled(model):
            return False
        # A PRO-only model must not be served by a free account: the upstream
        # answers 11158, which used to be discovered only after spending the
        # request. An unknown tier map returns False, so this is a no-op until
        # the catalogue has delivered modelTiers.
        if (model and not getattr(self, "is_paid_user", False)
                and wb_tiers.requires_pro(model, self.realm)):
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
        # The day's credit spend reached the cap: paid models stop, free
        # ones keep serving (see credit_limit_blocked).
        if self.credit_limit_blocked(model):
            return False
        # One model out of budget does not take the account with it: only
        # that model is refused here.
        if self.model_token_limit_blocked(model):
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

    def is_cli_identity(self):
        """出站身份是不是官方 CodeBuddy CLI（决定 chat 的 wire 形态）。"""
        return self.product == wb_identity.PRODUCT_CLI

    def headers(self, purpose="chat", session_key=None, turn_request_id=None,
                model=None, conversation_id=None):
        """组出这一轮的出站标头。

        chat 用途走 wb_identity（CLI 头 / WorkBuddy 头 / VSCode 头，可切换）；
        billing 用途维持原本的轻量标头，计费端点不吃那套身分。

        `model` / `conversation_id` 只对 chat 用途有意义：官方 VSCode 扩展在
        model 请求上带 `X-Model-ID`（值就是本次 model），`X-Conversation-ID`
        按会话稳定；计费端点不吃这套身分，两个参数在 billing 路径上被忽略。

        chat 标头按 5.8.4 抓包对齐：官方对话请求不带 X-Machine-ID /
        X-Session-ID（那两个只用于反馈上报），所以这里也不再发——之前发的
        是每个账号恒定的派生值，反而是一处「网关特征」。

        CLI 身份另按官方 CodeBuddy CLI 2.168.2 对齐：它的 chat 请求由 OpenAI
        Node SDK 发出，没有 Origin / Referer / X-CodeBuddy-Request /
        Accept-Language 这些浏览器头，Accept 就是 application/json；CLI 包里
        也没有设备 token 逻辑，所以 CLI 身份不发 X-Device-Token。

        session_key / turn_request_id 只影响 CLI 身份（官方 CLI 语义：会话 id
        按会话稳定、X-Conversation-Request-ID 一轮 user send 内复用），由
        open_upstream 传进来；桌面端 / VSCode 分支不吃这两个参数，行为不变。

        X-Device-Token 只在**国内版**发送：国内版客户端确实带真实的 Turing
        Shield 设备 token，国际版客户端则没有这个头（三轮抓包与 5.7.6 / 5.8.4
        二进制构造器都不含它）。发给国际版账号既是伪造头，也是把多个账号串到
        同一台设备的关联键（#448）。CLI 身份无论哪个区域都不发。

        billing 标头仍带 X-Machine-ID / X-Session-ID，但两个都必须按账号派生：
        网关是一个进程冒充几十台设备，一旦取进程级的值，全部账号就会共用同一个
        session id，那是一条把它们串起来的关联键（与每账号一台设备的隔离口径
        相悖）。见 wb_fingerprint.session_id。
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
                "X-Session-ID": session_id(self.uid),
            }
            # 静态 API Key：与 chat 链路口径一致，两个鉴权头都发。计费端点
            # 本身任一个都认，但统一形态省得两条路径各写一套。
            if self.is_api_key:
                headers["X-API-Key"] = self.access_token
            if self.enterprise_id:
                headers["X-Enterprise-Id"] = self.enterprise_id
                headers["X-Tenant-Id"] = self.enterprise_id
            else:
                headers["X-No-Enterprise-Id"] = "1"
            if self.realm == "cn":
                headers["X-Product"] = "SaaS"
            # 官方 CLI 全包没有设备 token 逻辑，CLI 身份一律不发。
            device_token = self._device_token() if not self.is_cli_identity() else ""
            if device_token:
                headers["X-Device-Token"] = device_token
            return headers

        identity = wb_identity.build_identity_headers(
            product=self.product,
            realm=self.realm,
            uid=self.uid,
            token=self.access_token,
            conversation_id=conversation_id,
            enterprise_id=self.enterprise_id,
            tenant_id=self.enterprise_id,
            session_key=session_key,
            turn_request_id=turn_request_id,
            model=model,
            private_data=(wb_settings.identity_private_data(
                os.path.dirname(self.path)) if self.path else "auto"),
        )
        if self.is_cli_identity() or (
                self.product == wb_identity.PRODUCT_DESKTOP
                and wb_identity.desktop_chat_compat_enabled()):
            # Opt-in desktop chat uses the SDK profile, not the billing web profile.
            # 官方 CLI 的 chat 请求由 OpenAI Node SDK 发出：浏览器头一个都没有
            # （X-Requested-With 也没有），Accept 就是 application/json。
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        else:
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": cfg["origin"],
                "Referer": cfg["origin"] + "/",
                "X-CodeBuddy-Request": "1",
                "Accept-Language": "en-US" if self.realm == "intl" else "zh-CN",
            }
        headers.update(identity)
        # 国际版不发 X-Device-Token（#448）；国内版保留真实 token 语义。
        # VSCode 非企业号仍按官方 token || error 的失败态发占位（#431）。
        device_token = (self._device_token()
                        if self.realm == "cn" and not self.is_cli_identity()
                        else "")
        if device_token:
            headers["X-Device-Token"] = device_token
        elif (self.product == wb_identity.PRODUCT_VSCODE
              and not self.enterprise_id):
            # 官方非企业号一定发 token 或 error 二选一（源码实锤：
            # AgentContextBuilderImpl / buildAuthHeaders 里 token || error）。
            # hub 没有本机 QIMEI/TuringShield SDK，生成不了真实 token，按官方
            # 的失败态发 error 占位，而不是两者都不发（那是官方不存在的状态）。
            headers["X-Device-Token-Error"] = "sdk_not_initialized"
        return headers

    def _device_token(self):
        """Optional X-Device-Token from the configured literal or file."""
        directory = getattr(self, "accounts_dir", "")
        if not directory:
            return ""
        return resolve_device_token(directory)

    def sync_nickname(self):
        """Manual web-console nickname refresh.

        Only called from the explicit dashboard/API action - never from the
        balance scheduler - and persists the new nickname immediately.
        """
        _uid, nickname = fetch_account_profile(self)
        if nickname and nickname != self.nickname:
            self.nickname = nickname
            directory = getattr(self, "accounts_dir", "")
            if not directory and self.path:
                directory = os.path.dirname(self.path)
            if directory:
                self.save(directory)
        return nickname

    def set_product(self, value):
        """切换出站身分（cli <-> workbuddy）。回传 True 表示真的换了。

        身分会写回凭证档，重启后仍然有效。save() 需要目录参数。
        """
        new = wb_identity.normalize_product(value)
        if new == self.product:
            return False
        self.product = new
        try:
            # 清的是限流状态；上游判定额度用满的禁用（仍在恢复期内的
            # model_caps）跨身分成立，不能被这次切换顺手清掉。
            self.clear_error(keep_caps=True)
        except Exception:
            pass
        return True

    def chat_base_url(self):
        """这个帐号目前身分该打的端点。"""
        return wb_identity.endpoint_for(self.realm, self.product)[0]

    def refresh(self):
        # Serialise refreshes per account, then re-check inside the lock: the
        # upstream rotates the refresh token, so two concurrent refreshes can
        # make the second one send a token that the first already consumed.
        with self._refresh_lock:
            return self._refresh_locked()

    def _refresh_locked(self):
        if self.is_api_key:
            # 静态 Key 无法 refresh（refresh 端点只认 OAuth 设备流）。返回
            # False 而不是把 token_expired 置 True：这类账号按定义就是长期
            # 有效，标成「过期」会把一个健康的号踢出池子并要求重新登录。
            return False
        if not self.refresh_token:
            self.token_expired = True
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
            err_msg = str(exc)
            if "401" in err_msg or "Authorization" in err_msg or "Unauthorized" in err_msg:
                self.token_expired = True
                self._set_last_error("登录已过期 (HTTP 401)，请重新登录")
            else:
                self._set_last_error("refresh failed: %s" % exc)
            return False
        data = (payload.get("data") or {})
        data = data.get("data") or data
        token = data.get("accessToken")
        if not token:
            self.token_expired = True
            self._set_last_error("refresh returned no token (%s)" % payload.get("msg"))
            return False
        self.access_token = token
        self.refresh_token = data.get("refreshToken") or self.refresh_token
        self.expires_at = jwt_exp(token) or self.expires_at
        self.token_expired = False
        with self._throttle_lock:
            self.last_error = ""
            self.cooldown_until = 0
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))
        return True

    def task_active(self):
        """Whether scheduled / manual tasks may touch this account.

        Independent of `enabled`: `enabled` only controls API dispatch.
        """
        return bool(self.tasks_enabled)

    def can_checkin(self):
        if self.realm != "cn":
            return False
        # 无个人成长体系：企业号上游直接拒，静态 Key 连个人身份都没有。
        if self.uses_enterprise_billing():
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

    def can_report_activity(self):
        """每天一次的对话活跃上报（点亮 growth 连登）是否还没做过。

        国内版与国际版都有这套成长体系，只是上报端点不同（见 report_activity）。
        """
        if self.realm not in ("cn", "intl"):
            return False
        if self.uses_enterprise_billing():
            return False
        if not self.last_activity_report:
            return True
        today_str = time.strftime("%Y-%m-%d")
        return not str(self.last_activity_report).startswith(today_str)

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

    def daily_chat_model(self):
        """Which model the desktop-identity leg of the activity runs on.

        Shares the candidate list with the web leg (`daily_chat_models`, first
        entry preferred): one setting now drives both legs, and a model that is
        cooling down or capped falls through to the next candidate instead of
        failing the activity.
        """
        candidates = self.web_fallback_models() or [DAILY_CHAT_MODEL]
        if len(candidates) < 2:
            # 桌面腿历史上会在首选模型冷却 / 达限时顺延到其它免费模型。候选序列
            # 只配了一个（或没配）时保留这份顺延；网页腿的轮数仍只看候选序列。
            candidates = candidates + [model for model in DAILY_CHAT_DESKTOP_FALLBACKS
                                       if model not in candidates]
        now = time.time()
        with self._throttle_lock:
            for cand in candidates:
                # Check model-level cooldown or cap directly
                cooldown_until = self.model_cooldowns.get(cand, 0.0)
                if cooldown_until <= now and self.cap_until(cand, now) <= 0.0:
                    return cand
        return candidates[0]

    def web_fallback_models(self):
        """The ordered model candidates the web leg walks through.

        An explicit list in the panel wins and is used **in that order** - it is
        the operator's priority, not a suggestion. Only the automatic mode (no
        list configured) sorts by catalogue multiplier, so a free model runs
        before a cheap paid one and that one before an expensive one.

        This replaces the old pair of settings (`daily_chat_model` for the
        preferred model plus a separate fallback switch and combination): with
        both set, the fallback branch took every model from the combination and
        the preferred model never ran at all.
        """
        configured = []
        if self.path:
            configured = wb_settings.daily_chat_models(
                os.path.dirname(self.path))
        if configured:
            return _clean_candidates(configured)
        # 自动模式只有内置的免费模型一个候选：候选序列就是回退，没配就是不回退。
        # 旧实现在这里额外塞进各区域的免费模型，只因为那套「回退组合」必须有人填。
        return [DAILY_CHAT_MODEL]

    def daily_chat_web_prompt(self):
        """The prompt this turn sends on the web channel.

        With the random-prompt setting off (the default) it is the same "Hi"
        the activity has always sent. With it on, one line is drawn from the
        built-in 300-line pool plus whatever the operator added, so the daily
        activity does not look like the same canned request every day.
        """
        if not self.path:
            return DAILY_CHAT_WEB_PROMPT
        directory = os.path.dirname(self.path)
        if not wb_settings.daily_chat_web_random_prompt(directory):
            return DAILY_CHAT_WEB_PROMPT
        # The stored list is the full pool the operator picked (builtin lines
        # and custom lines together). Empty means "never configured", which
        # keeps the original behaviour of drawing from all 300 builtin lines.
        pool = wb_settings.daily_chat_web_prompts(directory)
        if not pool:
            pool = wb_webprompts.builtin_prompts()
        return random.choice(pool) if pool else DAILY_CHAT_WEB_PROMPT
    def daily_chat_web(self, prompt=None, trigger="unknown"):
        """网页通道的每日活跃会话（issue #75 / #59 / #90）。

        只建会话是不够的：agent 要等客户端接上沙箱并请求这一轮才会跑，否则会话
        永远停在 CREATING、没有任何输出，也就不算一次有效对话（#90 实测）。这里
        按网页端的顺序走完：建会话 → 取沙箱 link+token → ACP 的 initialize /
        session/load / session/prompt（见 wb_webagent）→ 轮询到 completed。

        返回 {"ok": True, "conversation": id, "status": "completed", "chunks": n,
        "elapsed_ms": n}，失败时 {"ok": False, "error": ...}（尽量带上会话 id）。

        trigger 只用于活动历史的来源标注；trigger=None 表示这一步算在外层尝试
        里、不单独记一行 —— daily_chat() 内部就是这么调的。
        """
        if self.realm != "intl":
            return {"ok": False, "error": "web daily chat is only for international accounts"}
        # 当日已经成功过一次就不再开新会话：多个定时点、手动触发与并发调度
        # 可能在同一账号上重叠，重复跑只会多消耗一次网页活跃额度。
        if not self.can_daily_chat():
            return {"ok": True, "skipped": True,
                    "msg": "今日活跃已完成，跳过网页活跃"}
        res = self._daily_chat_web_upstream(prompt)
        wb_activity.record_attempt(self, wb_activity.TASK_DAILY_CHAT, trigger, res)
        return res

    def _daily_chat_web_upstream(self, prompt=None):
        """网页通道的实际流程；返回形状见 daily_chat_web。

        一次打卡 = **一个预算 + 一个候选序列**：按候选顺序一轮一轮试，每轮最多等到
        「单轮上限」与「剩余预算」里较小的那个；建会话 / 取沙箱失败后的退避也从同
        一个预算里扣。预算用尽必定给出结论（phase="budget"），不再有「跑了几个小
        时没动静、面板一片空白」这种状态。

        轮数由候选序列推出（至少两轮：第二轮留给「会话只是慢」），所以面板上不再
        需要「最多尝试次数」这个与候选数量互相覆盖的旋钮——旧实现里它被
        `max(总次数, 候选数)` 顶掉，设 1 次也照样跑 6 次。

        业务拒绝（建会话返回非 0 code）不再试：同一个被拒的请求再发一次也一样被拒。

        返回里带 attempts（实际跑了几轮）、models_tried、model，其余字段与
        _daily_chat_web_once 一致。
        """
        models = self.web_fallback_models() or [self.daily_chat_model()]
        turn_cap, rounds = self.daily_chat_web_policy()
        budget = self.daily_chat_budget()
        deadline = time.time() + budget if budget > 0 else None
        tried = []
        last = None
        budget_hit = False
        # 开跑之前先把「上一次跑剩下的」收干净，两层：
        # 1. 本进程登记着、却已经超过两轮上限还没注销的会话——上一轮卡在 socket 上，
        #    它的 finally 永远跑不到，只能由本线程代收；
        # 2. 上一个进程留下的（登记表跟着进程一起没了，只能按上游会话列表兜底）。
        # 任何一步失败都不影响这一轮：名额真不够时建会话会走 10105 的清理重试。
        try:
            self.web_conversation_reclaim(max_age=turn_cap * WEB_LIVE_STALE_FACTOR)
            self.web_conversation_sweep_orphans()
        except Exception:
            pass
        for index in range(rounds):
            wait = turn_cap
            if deadline is not None:
                remaining = deadline - time.time()
                if remaining < WEB_BUDGET_MIN_ROUND:
                    budget_hit = True
                    break
                # 向上取整：面板上「单轮最长 120 秒」就该等满 120 秒，
                # 别因为几毫秒的已用时间把它变成 119。
                wait = min(turn_cap, int(remaining + 0.999))
            model = models[index % len(models)]
            res = self._daily_chat_web_once(prompt, wait_seconds=wait, model=model)
            res["attempts"] = index + 1
            res["models_tried"] = tried + [model]
            res["model"] = model
            if res.get("ok"):
                return res
            last = res
            if not res.get("retryable"):
                return res
            tried.append(model)
            if index + 1 >= rounds:
                break
            self._web_retry_pause(res, index, deadline=deadline)
            if deadline is not None and deadline - time.time() < WEB_BUDGET_MIN_ROUND:
                budget_hit = True
                break
        if last is None:
            # 预算小到连第一轮都放不下：仍然要给一个明确结论，不能返回空。
            return {"ok": False, "phase": "budget", "budget_exhausted": True,
                    "attempts": 0,
                    "error": "单账号预算 %ds 不足一轮（至少需要 %ds 秒）"
                             % (budget, WEB_BUDGET_MIN_ROUND)}
        if budget_hit:
            last = dict(last)
            last["phase"] = "budget"
            last["budget_exhausted"] = True
            last["error"] = ("单账号预算 %ds 用尽（尝试 %d 轮，最后状态 %s）：%s"
                             % (budget, len(tried), last.get("status") or "未知",
                                last.get("error") or "会话没有跑完"))
        return last

    def _web_retry_pause(self, res, round_index, deadline=None):
        """还没跑到 turn 就失败时补一段退避，返回实际等了多少秒。

        跑过 turn 的轮次已经把等待预算（wait_seconds）花掉了，再睡一次就是双重
        惩罚；而建会话 / 取沙箱失败只用掉几百毫秒，背靠背重试会把所有尝试挤进
        同一个上游故障窗口。phase 缺失时按「已跑到 turn」处理，旧调用方和测试
        桩返回的结果形状因此保持原样。给了 deadline 时，退避也会被剩余预算钳住，
        不能睡过总预算。
        """
        if res.get("phase") not in ("create", "session"):
            return 0.0
        delay = web_retry_backoff(round_index,
                                  base=self.daily_chat_web_retry_backoff())
        if deadline is not None:
            delay = min(delay, max(0.0, deadline - time.time()))
        if delay > 0:
            time.sleep(delay)
        return delay

    def daily_chat_web_retry_backoff(self):
        """建会话 / 取沙箱阶段失败后的退避基数（秒）。设置页改过就按设置页的来。

        没配过时用内置默认 WEB_RETRY_BACKOFF —— 它吃 WB_WEB_RETRY_BACKOFF
        环境变量，别让这个新设置项把它悄悄顶掉。没配 settings 的账号（单测里
        手工构造的）同样按内置默认走。0 = 不退避（旧行为）。
        """
        if not self.path:
            return WEB_RETRY_BACKOFF
        return wb_settings.daily_chat_web_retry_backoff(
            os.path.dirname(self.path), default=WEB_RETRY_BACKOFF)

    def daily_chat_budget(self):
        """单账号跑完网页活跃的总预算（秒）；0 = 不限制。

        这是这套流程里唯一的时间上限：每轮等待、建会话失败后的退避都从它里面扣，
        所以「一次打卡最长占住调度器多久」是一个可预测的数（默认 600 秒）。旧实现
        没有这个上限，一个卡在「会话永远 working」的账号能把整轮巡检拖住数小时。
        """
        if not self.path:
            return WEB_BUDGET_DEFAULT
        return wb_settings.daily_chat_budget(
            os.path.dirname(self.path), default=WEB_BUDGET_DEFAULT)

    def daily_chat_web_policy(self):
        """(单轮最长等待秒数, 轮数)。设置页改过就按设置页的来。

        轮数 = max(WEB_MIN_ROUNDS, 候选数)：候选序列每多一个模型就多一轮，最少两轮
        —— 第二轮留给「会话只是慢」（状态=working），一轮没跑完不代表坏了。总时长由
        单账号预算兜底（见 daily_chat_budget），所以这里不需要「最多尝试次数」这个
        与候选数量互相覆盖的旋钮。

        等待时间没设过时用 WEB_TURN_TIMEOUT —— 它吃 WB_WEB_TURN_TIMEOUT 环境变量，
        别让这个设置项把它悄悄顶掉。没配 settings 的账号（单测里手工构造的）同样按
        内置默认走。
        """
        rounds = max(WEB_MIN_ROUNDS, len(self.web_fallback_models()))
        if not self.path:
            return WEB_TURN_TIMEOUT, rounds
        directory = os.path.dirname(self.path)
        return (wb_settings.daily_chat_web_timeout(directory, default=WEB_TURN_TIMEOUT),
                rounds)

    def _daily_chat_web_once(self, prompt=None, wait_seconds=None, model=None):
        """网页通道的一轮尝试；返回形状见 daily_chat_web。

        结果里的 retryable 说明这一轮的失败值不值得再试一次（重试策略见
        _daily_chat_web_upstream）。model 为空时按账号首选模型；回退模式显式
        传入当前候选，保证建会话与 session/set_model 用的是同一个模型。

        phase 标出失败停在哪一步：create（建会话）/ session（取沙箱）/ turn
        （接上沙箱跑这一轮）。重试方按它判断这一轮到底有没有花掉等待预算。
        """
        if wait_seconds is None:
            wait_seconds = WEB_TURN_TIMEOUT
        model = model or self.daily_chat_model()
        chosen_prompt = prompt or self.daily_chat_web_prompt()
        body = {
            "prompt": chosen_prompt,
            "model": model,
            # 网页端建会话时固定带上这两项（抓包所得），保持请求形态一致。
            "conversationOrigin": "workbuddy-app",
            "plugins": [{"name": "weixinpay", "marketplace": "codebuddy-builtin"}],
        }
        conversation, failure = self._web_conversation_create(body)
        if failure and failure.get("limit"):
            # 账号攒满了活跃会话：老版本每跑一次就漏一条、从不清尾，十天后连建都
            # 建不出来。先停掉最旧的本网关会话把名额腾出来，再建一次；腾不出来就
            # 直接告诉用户去网页端收尾，不再拿重试次数去撞同一堵墙。
            freed = self.web_conversation_prune()
            if freed:
                conversation, failure = self._web_conversation_create(body)
            if failure:
                hint = ("已自动结束 %d 条旧会话" % freed) if freed \
                    else "没有可自动结束的旧会话"
                return {"ok": False,
                        "error": "活跃会话已达上限（%d）：%s；请到 %s 网页端结束或删除"
                                 "旧对话后重试"
                                 % (WEB_CONVERSATION_ACTIVE_LIMIT, hint,
                                    WEB_ORIGIN.replace("https://", "")),
                        "retryable": False}
        if failure:
            return {"ok": False, "error": failure.get("message") or "建会话失败",
                    "retryable": bool(failure.get("retryable")),
                    "phase": "create"}

        keep = False
        # 建出来就先登记：万一这一轮卡在 socket 上走不到 finally，别的线程还能
        # 按这张表替它收尾（见 web_conversation_reclaim）。
        web_live_register(self.uid, conversation, model)
        try:
            # 建完只是排队：接上沙箱、请求这一轮，它才会真的跑起来（issue #90）。
            try:
                session = (self._web_conversation_get(conversation, "/session") or {}).get("data") or {}
            except Exception as exc:
                return {"ok": False, "conversation": conversation,
                        "error": "session 查询失败: %s" % exc, "retryable": True,
                        "phase": "session"}
            link = session.get("link") or session.get("endpoint") or ""
            token = session.get("token") or ""
            session_id = session.get("sessionId") or session.get("session_id") or conversation
            cwd = session.get("cwd") or "/workspace"
            if not link or not token:
                return {"ok": False, "conversation": conversation,
                        "error": "沙箱未就绪（没有 link/token）", "retryable": True,
                        "phase": "session"}

            result = wb_webagent.run_turn(
                link, token, session_id, cwd, chosen_prompt,
                WEB_USER_AGENT,
                poll_status=lambda: self._web_conversation_status(conversation),
                wait_seconds=wait_seconds, proxy=self.proxy,
                model=model)
            result["conversation"] = conversation
            result["model"] = model
            result["phase"] = "turn"
            if result.get("ok"):
                keep = True
                result["msg"] = "网页通道会话跑完（%s）：%d 段输出，%d ms" % (
                    model, result.get("chunks") or 0, result.get("elapsed_ms") or 0)
            else:
                result["retryable"] = True
            return result
        finally:
            # 收尾必须发生：不 stop 的话这条会话会一直占着上游的活跃名额，跑够十次
            # 这个账号就再也建不出会话（HTTP 429 / 10105）。成功的一轮保留记录
            # （stop 只把状态改成 completed），失败的一轮直接删掉，免得网页端历史
            # 里堆满「Hi」。
            try:
                self.web_conversation_release(conversation, delete=not keep)
            finally:
                web_live_unregister(self.uid, conversation)

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

    # ---- 网页会话的收尾与名额回收 -----------------------------------------
    # 上游对每个账号同时存在的活跃会话（status=working）有硬上限 10 条，而每条
    # 每日活跃都会新建一条。只建不收尾的话，每天漏一条、每次重试再漏一条，攒满
    # 十条之后 CreateConversation 直接 429 / 10105，打卡就再也做不成了。

    def _web_conversation_call(self, conversation, action):
        """对一条网页会话执行动作（stop / delete），返回上游是否接受。

        清理是收尾动作，任何异常都不能冒泡：一轮已经跑完的活跃不能因为收尾失败
        被改判成失败。
        """
        if not conversation:
            return False
        url = (WEB_CONVERSATIONS_URL + urllib.parse.quote(str(conversation))
               + "/" + action)
        req = urllib.request.Request(url, data=b"{}",
                                     headers=self.web_headers(), method="POST")
        try:
            with urlopen(req, timeout=20, proxy=self.proxy) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except Exception:
            return False
        return isinstance(payload, dict) and payload.get("code") in (0, None)

    def web_conversation_release(self, conversation, delete=False):
        """结束一条网页会话，释放它占的活跃名额。

        默认只 stop：会话留在网页端历史里，状态从 working 变成 completed，名额
        随即释放。delete=True 时再删掉它——失败轮次没有保留价值，删掉免得网页端
        塞满「Hi」。
        """
        if not conversation:
            return False
        stopped = self._web_conversation_call(conversation, "stop")
        if delete:
            return self._web_conversation_call(conversation, "delete") or stopped
        return stopped

    def web_conversation_list(self, page_size=50):
        """读账号的网页会话列表（上游按创建时间倒序返回）。"""
        url = WEB_CONVERSATIONS_URL + "?page=1&pageSize=%d" % max(1, int(page_size))
        req = urllib.request.Request(url, headers=self.web_headers(), method="GET")
        with urlopen(req, timeout=20, proxy=self.proxy) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        data = (payload if isinstance(payload, dict) else {}).get("data") or {}
        conversations = data.get("conversations")
        return conversations if isinstance(conversations, list) else []

    def _daily_activity_prompt_pool(self):
        """本网关可能用作会话名的提示词（内置池 + 操作者自定义）。"""
        pool = set(wb_webprompts.builtin_prompts())
        try:
            if self.path:
                pool.update(wb_settings.daily_chat_web_prompts(
                    os.path.dirname(self.path)))
        except Exception:
            pass
        pool.add(DAILY_CHAT_WEB_PROMPT)
        return pool

    def web_conversation_prune(self, keep=WEB_CONVERSATION_PRUNE_KEEP):
        """停掉最旧的活跃会话，把账号拉回上限以下；返回停掉的条数。

        只在撞到 10105 时调用，用来收拾老版本漏下的会话。按创建时间从旧到新停，
        且只动名字属于每日活跃提示词池的会话——用户自己在网页端的真实对话不碰。
        stop 不是删除：会话仍在网页端历史里，只是不再占活跃名额。
        """
        try:
            conversations = self.web_conversation_list()
        except Exception:
            return 0
        active = [c for c in conversations
                  if str((c or {}).get("status") or "").lower()
                  in WEB_CONVERSATION_ACTIVE_STATUSES]
        if len(active) < WEB_CONVERSATION_ACTIVE_LIMIT:
            return 0
        active.sort(key=lambda c: str((c or {}).get("createdAt") or ""))
        pool = self._daily_activity_prompt_pool()
        stopped = 0
        for conversation in active:
            if len(active) - stopped <= keep:
                break
            if str((conversation or {}).get("name") or "").strip() not in pool:
                continue
            if self._web_conversation_call(conversation.get("id"), "stop"):
                stopped += 1
        return stopped

    def web_conversation_reclaim(self, max_age=0.0):
        """替**别的线程**收尾它建出来、却还没注销的网页会话；返回收掉的条数。

        为什么需要它：_daily_chat_web_once 的 finally 只在这一轮能返回时才跑得掉，
        而 socket 层存在走不到返回的窗口（getaddrinfo 不吃 socket 超时、服务端可
        以一点一点滴数据）。那一路卡住时，那条会话会一直占着上游的活跃名额，攒够
        十条这个账号就再也建不出会话——和 #623 修的是同一个坑，只是那条路径靠
        finally 兜不住。

        max_age=0 表示「这个账号登记着的一律收掉」；给了秒数就只收超过这个时长的
        （正常一轮的 finally 会自己注销，只有跑过头的才会被收）。收尾用本线程自己
        的短超时连接，与卡住的那条线程无关，所以它卡着也照样能收。
        """
        reclaimed = 0
        for item in web_live_snapshot(self.uid, min_age=max_age):
            conversation = item.get("conversation")
            if self.web_conversation_release(conversation, delete=True):
                # 收尾成功后才摘掉登记：失败时留着重试，否则这个活跃名额会永久
                # 丢失（进程内登记表是它唯一的回收线索）。
                web_live_unregister(self.uid, conversation)
                reclaimed += 1
        return reclaimed

    def web_conversation_sweep_orphans(self, older_than=WEB_ORPHAN_SWEEP_SECONDS):
        """清扫**上一个进程**留下的活跃会话；返回停掉的条数。

        进程被 kill 或崩掉时登记表跟着没了，靠 web_conversation_reclaim 收不到；
        这里按上游的会话列表兜底：只停「名字来自每日活跃提示词池」且创建超过
        older_than 秒、仍处于活跃状态的会话（用户自己在网页端的对话不碰）。

        阈值默认 30 分钟——一轮最多几分钟，半小时还没结束的一定不是正在跑的本轮，
        也避免误杀另一个网关实例正在跑的会话。只 stop 不 delete：历史留着。
        """
        try:
            conversations = self.web_conversation_list()
        except Exception:
            return 0
        cutoff = time.time() - max(0, older_than)
        pool = self._daily_activity_prompt_pool()
        stopped = 0
        for conversation in conversations:
            data = conversation or {}
            if str(data.get("status") or "").lower() not in WEB_CONVERSATION_ACTIVE_STATUSES:
                continue
            if str(data.get("name") or "").strip() not in pool:
                continue
            created = _parse_created_at(data.get("createdAt"))
            if not created or created > cutoff:
                continue
            if self._web_conversation_call(data.get("id"), "stop"):
                stopped += 1
        return stopped

    def _web_conversation_create(self, body):
        """建一条网页会话；返回 (会话 id, 失败信息)。

        失败信息是 {"message": ..., "retryable": bool, "limit": bool}，limit 表示
        「活跃会话已满」——调用方会先清理再重试一次。
        """
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
            return None, {"message": "HTTP %d: %s" % (exc.code, detail[:120]),
                          "retryable": exc.code >= 500 or exc.code in (408, 429),
                          "limit": _active_conversation_limit(exc.code, detail)}
        except Exception as exc:
            return None, {"message": str(exc), "retryable": True, "limit": False}
        if not isinstance(payload, dict):
            return None, {"message": "unexpected response", "retryable": True,
                          "limit": False}
        conversation = (payload.get("data") or {}).get("id")
        if payload.get("code") not in (0, None) or not conversation:
            return None, {"message": "code=%s msg=%s" % (payload.get("code"),
                                                         payload.get("msg")),
                          "retryable": False, "limit": False}
        return conversation, None

    def daily_chat(self, web=None, trigger="unknown"):
        """国际版每日活跃对话（官方每日活跃 30/50 积分）。

        两步：桌面端身分的轻量对话（一直以来的做法），以及网页通道的会话
        （issue #75/#59/#90：算数的是「跑完的 agent 会话」）。web=None 时按
        settings.json 里的 daily_chat_web 决定，True/False 可显式指定。

        trigger 只用于活动历史的来源标注；整次尝试记一行，网页通道那一步的结果
        并进同一行的说明里，不再多记一行。ok 以真正计入积分的网页腿为准：网页腿
        开着且失败时整次算失败（桌面腿跑过也不算数）。
        """
        if self.realm != "intl":
            return {"ok": False, "error": "daily chat is only for international accounts"}
        # 当日活跃成功后再触发（下一个定时点、手动补跑、并发重复调度）直接
        # 跳过，连桌面腿也不发：这一天的 30/50 积分已经拿到了。
        if not self.can_daily_chat():
            return {"ok": True, "skipped": True,
                    "msg": "今日活跃已完成，跳过本轮"}
        res = self._daily_chat_upstream(web)
        wb_activity.record_attempt(self, wb_activity.TASK_DAILY_CHAT, trigger, res)
        return res

    def _daily_chat_upstream(self, web=None):
        """每日活跃对话的实际流程；返回形状见 daily_chat。"""
        import wb_proxy
        url = self.chat_base_url() + wb_proxy.CHAT_PATH
        model = self.daily_chat_model()
        headers = self.headers("chat", model=model)
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
            "max_tokens": 10,
            "reasoning_effort": "none",
        }
        req_body = wb_proxy.build_upstream_body(body)
        # 官方 CLI 的 chat 请求体：gzip + cache_retention（紧凑 JSON 由传输层统一
        # 处理，本 PR 不重复实现）；桌面端 / VSCode 维持原请求体（VSCode 的
        # RequestBodyGzip 有 feature 开关、主 chat 是否压仍待抓包确认）。
        cli_identity = self.is_cli_identity()
        if cli_identity:
            req_body = {k: v for k, v in req_body.items()
                        if k not in wb_identity.CLI_BODY_DROP_FIELDS}
            req_body["cache_retention"] = wb_identity.CLI_CACHE_RETENTION
            headers["Content-Encoding"] = wb_identity.REQUEST_CONTENT_ENCODING
        data = json.dumps(req_body, ensure_ascii=False).encode("utf-8")
        if cli_identity:
            data = wb_identity.gzip_body(data)
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        # 与 open_upstream 的 chat 路径同口径：头名原样上线（Request.add_header 会
        # 先 capitalize，所以有序原名单独挂在请求上给池路径用）。
        req.wb_exact_headers = list(headers.items())
        try:
            with urlopen(req, timeout=20, proxy=self.proxy) as resp:
                _ = resp.read()
            try:
                self.fetch_credits()
            except Exception:
                pass
            result = {"ok": True, "msg": "每日活跃对话成功完成"}
            web_ok = True
            if web is None:
                web = bool(self.path) and wb_settings.daily_chat_web(os.path.dirname(self.path))
            if web:
                res = self.daily_chat_web(trigger=None)
                result["web"] = res
                web_ok = bool(res.get("ok"))
                if web_ok:
                    # 重试过就把轮数带上：光看「成功」分不清是首轮就跑完，还是
                    # 前几轮超时、多等了一轮才跑完（后者是慢，不是没跑）。
                    tries = res.get("attempts") or 1
                    retried = "（重试 %d 次后）" % (tries - 1) if tries > 1 else ""
                    result["msg"] = ("每日活跃对话成功完成（网页通道%s %s：%d 段输出，%d ms）"
                                     % (retried, res.get("status") or "completed",
                                        res.get("chunks") or 0, res.get("elapsed_ms") or 0))
                else:
                    # 桌面腿成功不等于当天打卡完成：计入官方「每日活跃」的只有网页腿
                    # （#75/#59/#90）。面板与活动历史按 ok 画成功/失败，网页腿失败却
                    # 报成功的话，「今天这 30/50 积分没到账」在界面上一直看不见。
                    tries = res.get("attempts") or 1
                    suffix = "（已重试 %d 次）" % (tries - 1) if tries > 1 else ""
                    result["ok"] = False
                    result["error"] = "网页通道失败：%s%s" % (res.get("error"), suffix)
                    result["msg"] = "每日活跃对话未完成（网页通道失败：%s%s）" % (
                        res.get("error"), suffix)
                    # 沙箱入口证书校验失败（#645）不属于「这一轮没跑完」，重试同一轮
                    # 永远不会成功；而成长连登只需要一条 POST /v2/report 就能点亮。
                    # 开关打开时改走活跃上报，把当天算完成（默认关，行为不变）。
                    fallback_on = (bool(self.path)
                                   and wb_settings.daily_chat_web_fallback_activity(
                                       os.path.dirname(self.path)))
                    if fallback_on and is_tls_verify_text(res.get("error")):
                        fb = self.report_activity()
                        result["web_fallback"] = fb
                        if fb.get("ok"):
                            web_ok = True
                            result["ok"] = True
                            result.pop("error", None)
                            result["msg"] = (
                                "每日活跃对话：网页通道证书校验失败，已回退对话活跃上报（%s）"
                                % (fb.get("msg") or ""))
            # 只有真正计入官方「每日活跃」的网页通道成功了才算今天做过：网页腿失败时
            # 若照写 lastDailyChat，当天后续巡检会直接跳过该账号，那天的 30/50 积分静默
            # 丢掉且不再重试。web=False（没开网页通道）时维持原语义：桌面腿成功即算完成。
            if web_ok:
                self.last_daily_chat = time.strftime("%Y-%m-%d %H:%M:%S")
                if self.path and os.path.exists(os.path.dirname(self.path)):
                    self.save(os.path.dirname(self.path))
            return result
        except urllib.error.HTTPError as exc:
            try:
                err = exc.read().decode("utf-8", "replace")
            except Exception:
                err = str(exc)
            return {"ok": False, "error": f"HTTP {exc.code}: {err[:100]}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def checkin(self, trigger="unknown"):
        """国内版每日签到。

        trigger 只用于活动历史的来源标注（scheduler / manual / account_add /
        account_import）；记录写失败不会改变签到结果。
        """
        if self.realm != "cn":
            return {"ok": False, "error": "checkin is only available for CN realm accounts"}
        if self.is_api_key:
            return {"ok": False, "error": "API key accounts have no checkin programme"}
        if self.is_enterprise():
            return {"ok": False, "error": "enterprise accounts do not support checkin"}
        res = self._checkin_upstream()
        wb_activity.record_attempt(self, wb_activity.TASK_CHECKIN, trigger, res)
        return res

    def _checkin_upstream(self):
        """签到的实际请求；返回形状见 checkin。"""
        cfg = get_realm_config("cn")
        url = cfg["billing_upstream"] + CHECKIN_PATH
        headers = self.headers(purpose="billing")
        try:
            payload = http_json(url, data=b"{}", method="POST", headers=headers, timeout=15,
                                proxy=self.proxy)
            code = payload.get("code", -1)
            msg = payload.get("msg") or "ok"
            state = checkin_state(code, msg)
            if state in ("checked", "already"):
                self.last_checkin = time.strftime("%Y-%m-%d %H:%M:%S")
                if self.path and os.path.exists(os.path.dirname(self.path)):
                    self.save(os.path.dirname(self.path))
                return {"ok": True, "state": state, "code": code, "msg": msg,
                        "data": payload.get("data")}
            return {"ok": False, "state": state, "code": code, "msg": msg, "error": msg}
        except urllib.error.HTTPError as exc:
            try:
                raw_b = exc.read().decode("utf-8") or "{}"
                body = json.loads(raw_b)
                code = body.get("code")
                msg = body.get("msg") or ("HTTP %d" % exc.code)
                # 上游如果在今日已签到时返回 HTTP 400 且文案写「今天已签到」，
                # 说明今日事实上已处于签到完成状态，按成功处理并记录时间。
                state = checkin_state(code, msg)
                if state == "already":
                    self.last_checkin = time.strftime("%Y-%m-%d %H:%M:%S")
                    if self.path and os.path.exists(os.path.dirname(self.path)):
                        self.save(os.path.dirname(self.path))
                    return {"ok": True, "state": state, "code": code, "msg": msg,
                            "data": body.get("data")}
                return {"ok": False, "state": state, "code": code, "msg": msg, "error": msg}
            except Exception:
                return {"ok": False, "error": "HTTP %d" % exc.code}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def report_activity(self):
        """国内版对话活跃上报：一条 /v2/report 点亮官方 growth 连登。

        这是客户端 chat_request_send 事件的复刻（事件形状见 wb_tasks.build_event），
        上游口径：必须带 userId，否则服务端 200 但静默丢弃；每号每天一次即可，
        不做多时点高频上报（风控口径）。成功后记 lastActivityReport 并落盘，再读
        一次 /activity/growth/streak 把连登天数带回去——读失败不影响上报本身的
        结论。国内版与国际版都有这套成长体系，只是 base 不同：国际版走
        chat_base_url()（www.workbuddy.ai），国内版走 wb_tasks 的默认国内 base。
        """
        if self.realm not in ("cn", "intl"):
            return {"ok": False, "error": "activity report is only for CN/Intl realm accounts"}
        if self.is_api_key:
            return {"ok": False, "error": "API key accounts have no activity programme"}
        if self.is_enterprise():
            return {"ok": False, "error": "enterprise accounts do not support activity report"}
        import wb_tasks
        event = wb_tasks.build_event(self, "chat")
        base = None if self.realm == "cn" else self.chat_base_url()
        if not wb_tasks.report_events(self, [event], base=base):
            return {"ok": False, "error": "活跃上报被上游拒绝（未返回 code=0）"}
        self.last_activity_report = time.strftime("%Y-%m-%d %H:%M:%S")
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))
        days = wb_tasks.fetch_streak_days(self)
        if days is None:
            return {"ok": True, "msg": "对话活跃上报成功（连登天数未知）"}
        if days <= 0:
            return {"ok": True, "streak_days": 0,
                    "msg": "对话活跃上报成功，但连登仍是 0 天（可能被上游静默丢弃）"}
        return {"ok": True, "streak_days": days,
                "msg": "对话活跃上报成功（连续打卡 %d 天）" % days}

    def _parse_package_account(self, acc):
        pkg_name = acc.get("PackageName") or "Package"
        pkg_code = acc.get("PackageCode") or ""

        def _val(*keys):
            for k in keys:
                v = acc.get(k)
                if v is not None and v != "":
                    try:
                        return float(v)
                    except (ValueError, TypeError):
                        pass
            return 0.0

        size = _val("CycleCapacitySizePrecise", "CycleCapacitySize", "CapacitySizePrecise", "CapacitySize")
        remain = _val("CycleCapacityRemainPrecise", "CycleCapacityRemain", "CapacityRemainPrecise", "CapacityRemain")
        used = _val("CycleCapacityUsedPrecise", "CycleCapacityUsed", "CapacityUsedPrecise", "CapacityUsed")

        if size > 0 and used <= 0 and remain <= size:
            used = max(0.0, size - remain)
        elif size > 0 and remain <= 0 and used < size:
            remain = max(0.0, size - used)

        # 提取发放原因（如“官方活动发放”、“拉新奖励”等）
        grant_reason = ""
        for attr in (acc.get("AccountAttributes") or []):
            if isinstance(attr, dict) and attr.get("Key") == "grantReason":
                grant_reason = str(attr.get("Value") or "")
                break

        # 提取创建时间（毫秒时间戳转换）
        create_time = ""
        raw_create = acc.get("CreateTime")
        if raw_create:
            try:
                ts = float(raw_create)
                if ts > 1e11:
                    ts /= 1000.0
                create_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
            except Exception:
                create_time = str(raw_create)

        end_time_str = acc.get("CycleEndTime") or acc.get("ExpiredTime") or ""
        # 到期认「抵扣截止时间」而不是「周期结束时间」：周期结束只是计费周期
        # 的边界，积分未必跟着作废。免费包/体验版这两个字段能差 8 年，只认
        # CycleEndTime 会让这类账号每到月底都被误判成「即将到期」。
        expire_time_str = deduction_end_text(acc) or end_time_str
        days_left = None
        is_expired = False
        no_expiry = False
        if expire_time_str:
            try:
                clean_time = expire_time_str.replace("T", " ")[:19]
                end_ts = time.mktime(time.strptime(clean_time, "%Y-%m-%d %H:%M:%S"))
                diff_sec = end_ts - time.time()
                if diff_sec > EXPIRY_SENTINEL_DAYS * 86400.0:
                    no_expiry = True
                else:
                    days_left = round(diff_sec / 86400.0, 1)
                    is_expired = diff_sec < 0
            except Exception:
                pass

        in_usage = bool(acc.get("InUsage"))
        if not in_usage and not is_expired and remain > 0 and used > 0:
            in_usage = True

        return {
            "name": pkg_name,
            "package_code": pkg_code,
            "product_name": acc.get("ProductName") or "",
            "sub_product_name": acc.get("SubProductName") or "",
            "grant_reason": grant_reason,
            "resource_id": acc.get("ResourceId") or "",
            "deal_name": acc.get("DealName") or "",
            "create_time": create_time,
            "remain": round(remain, 2),
            "used": round(used, 2),
            "size": round(size, 2),
            "unit": acc.get("CapacityUnit") or "credits",
            "in_usage": in_usage,
            "auto_renew": bool(acc.get("AutoRenewFlag") or acc.get("SupportAutoRenew")),
            "cycle_start_time": acc.get("CycleStartTime") or "",
            "cycle_end_time": end_time_str,
            "expire_time": expire_time_str,
            "no_expiry": no_expiry,
            "days_left": days_left,
            "is_expired": is_expired,
            "status": acc.get("Status", 0),
        }

    def _fetch_credits_cn_detailed(self):
        cfg = get_realm_config(self.realm)
        headers = self.headers(purpose="billing")
        headers["X-Client-Platform"] = "web"
        base_billing = cfg["billing_upstream"]

        # 1. 套餐概览
        summary_url = base_billing + RESOURCE_SUMMARY_PATH
        try:
            s_res = http_json(summary_url, data=b"{}", method="POST", headers=headers,
                              timeout=15, proxy=self.proxy)
        except Exception as exc:
            err_str = str(exc)
            if "401" in err_str or "Authorization" in err_str or "Unauthorized" in err_str:
                if self.refresh_token and self.refresh():
                    headers = self.headers(purpose="billing")
                    headers["X-Client-Platform"] = "web"
                    try:
                        s_res = http_json(summary_url, data=b"{}", method="POST", headers=headers,
                                          timeout=15, proxy=self.proxy)
                    except Exception:
                        self.token_expired = True
                        self._set_last_error("登录已过期，请重新登录")
                        return {"ok": False, "error": "登录已过期，请在账号管理页重新扫码登录", "need_relogin": True}
                else:
                    self.token_expired = True
                    self._set_last_error("登录已过期，请重新登录")
                    return {"ok": False, "error": "登录已过期，请在账号管理页重新扫码登录", "need_relogin": True}
            else:
                raise
        if s_res.get("code") != 0:
            return {"ok": False, "error": s_res.get("msg") or f"code {s_res.get('code')}"}
        s_data = s_res.get("data") or {}
        # 同一份摘要顺带给出档位（#505）：国内版的详细积分路径本来就在打这个
        # 接口，不额外花请求。企业账号跳过——上游对它们的 IsPaidUser 语义
        # 还没验证过，写进去会把「企业额度」覆盖成「免费」。
        if not self.is_enterprise():
            tier = tier_from_summary(s_data)
            if tier:
                self.apply_tier(tier)
        paid_flag = self.tier_paid()
        if paid_flag is None:
            paid_flag = bool(s_data.get("IsPaidUser"))
        summary_pkgs = s_data.get("Packages") or []
        pkg_codes = [p.get("PackageCode") for p in summary_pkgs if p.get("PackageCode")]

        # 2. 免费包
        free_accs = []
        if pkg_codes:
            free_url = base_billing + RESOURCE_FREE_PACKAGES_PATH
            body = {"PackageCodes": pkg_codes, "PageNumber": 1, "PageSize": 200, "Status": [0]}
            try:
                f_res = http_json(free_url, data=json.dumps(body).encode(), method="POST",
                                  headers=headers, timeout=15, proxy=self.proxy)
                free_accs = (f_res.get("data") or {}).get("Accounts") or []
            except Exception:
                pass

        # 3. 付费包
        paid_accs = []
        if pkg_codes:
            paid_url = base_billing + RESOURCE_PAID_PACKAGES_PATH
            body = {"PackageCodes": pkg_codes, "PageNumber": 1, "PageSize": 200, "Status": [0, 3], "NeedRenewInfo": True}
            try:
                p_res = http_json(paid_url, data=json.dumps(body).encode(), method="POST",
                                  headers=headers, timeout=15, proxy=self.proxy)
                paid_accs = (p_res.get("data") or {}).get("Accounts") or []
            except Exception:
                pass

        # 4. 每日签到状态
        checkin_info = None
        try:
            checkin_url = cfg.get("chat_upstream", "https://copilot.tencent.com") + CHECKIN_STATUS_PATH
            c_res = http_json(checkin_url, data=b"{}", method="POST", headers=headers,
                              timeout=10, proxy=self.proxy)
            c_data = c_res.get("data") or {}
            if c_data:
                checkin_info = {
                    "today_checked_in": c_data.get("today_checked_in", False),
                    "streak_days": c_data.get("streak_days", 0),
                    "daily_credit": c_data.get("daily_credit", 0),
                    "today_credit": c_data.get("today_credit", 0),
                    "active": c_data.get("active", True),
                }
        except Exception:
            pass

        packages = []
        seen_account_ids = set()
        for raw_acc in (free_accs + paid_accs):
            acc_id = raw_acc.get("AccountId")
            if acc_id and acc_id in seen_account_ids:
                continue
            if acc_id:
                seen_account_ids.add(acc_id)
            packages.append(self._parse_package_account(raw_acc))

        tot_remain = sum(p["remain"] for p in packages)
        tot_used = sum(p["used"] for p in packages)
        tot_size = sum(p["size"] for p in packages)

        # 若具体包为空且 summary 含有汇总容量，则使用 summaryPkgs
        if not packages and summary_pkgs:
            for sp in summary_pkgs:
                tot_size += float(sp.get("CycleTotalCapacity") or 0)
                tot_remain += float(sp.get("CycleRemainCapacity") or 0)
                tot_used += float(sp.get("CycleUsedCapacity") or 0)

        tot_remain = round(tot_remain, 2)
        tot_used = round(tot_used, 2)
        tot_size = round(tot_size, 2)

        # 排序：使用中优先 -> 未过期中按到期时间升序 -> 已过期排最后
        def _pkg_sort_key(p):
            in_use_score = 0 if p.get("in_usage") else 1
            expired_score = 1 if p.get("is_expired") else 0
            days = p.get("days_left") if p.get("days_left") is not None else 99999
            if days < 0:
                days = 99999 + abs(days)
            return (in_use_score, expired_score, days, -p.get("remain", 0))

        packages.sort(key=_pkg_sort_key)

        # 查找最早到期的有效包（有剩余积分且未过期）
        active_pkgs = [p for p in packages if p.get("remain", 0) > 0 and not p.get("is_expired") and p.get("days_left") is not None]
        active_pkgs.sort(key=lambda p: p["days_left"])
        earliest_expiring = None
        if active_pkgs:
            ep = active_pkgs[0]
            earliest_expiring = {
                "name": ep["name"],
                "package_code": ep.get("package_code", ""),
                "remain": ep["remain"],
                "cycle_end_time": ep["cycle_end_time"],
                "days_left": ep["days_left"],
            }

        self.credits = {
            "remain": tot_remain,
            "used": tot_used,
            "size": tot_size,
            "used_percent": f"{(tot_used / tot_size * 100):.1f}%" if tot_size > 0 else "0.0%",
            "remain_percent": f"{(tot_remain / tot_size * 100):.1f}%" if tot_size > 0 else "100.0%",
            "is_paid_user": paid_flag,
            "checkin": checkin_info,
            "earliest_expiring": earliest_expiring,
            "packages": packages,
            "updated_at": time.time(),
            "updated_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))
        return {"ok": True, "credits": self.credits}

    def _fetch_credits_fallback(self):
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
            err_str = str(exc)
            if "401" in err_str or "Authorization" in err_str or "Unauthorized" in err_str:
                if self.refresh_token and self.refresh():
                    try:
                        headers = self.headers(purpose="billing")
                        res = http_json(url, data=json.dumps(body).encode(), method="POST", headers=headers,
                                        timeout=30, proxy=self.proxy)
                    except Exception:
                        self.token_expired = True
                        self._set_last_error("登录已过期，请重新登录")
                        return {"ok": False, "error": "登录已过期，请在账号管理页重新扫码登录", "need_relogin": True}
                else:
                    self.token_expired = True
                    self._set_last_error("登录已过期，请重新登录")
                    return {"ok": False, "error": "登录已过期，请在账号管理页重新扫码登录", "need_relogin": True}
            return {"ok": False, "error": err_str}
        data = res.get("data", {}).get("Response", {}).get("Data", {})
        accounts = data.get("Accounts") or []
        packages = []
        for a in accounts:
            packages.append(self._parse_package_account(a))

        tot_remain = round(sum(p["remain"] for p in packages), 2)
        tot_used = round(sum(p["used"] for p in packages), 2)
        tot_size = round(sum(p["size"] for p in packages), 2)

        def _pkg_sort_key(p):
            in_use_score = 0 if p.get("in_usage") else 1
            expired_score = 1 if p.get("is_expired") else 0
            days = p.get("days_left") if p.get("days_left") is not None else 99999
            if days < 0:
                days = 99999 + abs(days)
            return (in_use_score, expired_score, days, -p.get("remain", 0))

        packages.sort(key=_pkg_sort_key)

        active_pkgs = [p for p in packages if p.get("remain", 0) > 0 and not p.get("is_expired") and p.get("days_left") is not None]
        active_pkgs.sort(key=lambda p: p["days_left"])
        earliest_expiring = None
        if active_pkgs:
            ep = active_pkgs[0]
            earliest_expiring = {
                "name": ep["name"],
                "package_code": ep.get("package_code", ""),
                "remain": ep["remain"],
                "cycle_end_time": ep["cycle_end_time"],
                "days_left": ep["days_left"],
            }

        self.credits = {
            "remain": tot_remain,
            "used": tot_used,
            "size": tot_size,
            "used_percent": f"{(tot_used / tot_size * 100):.1f}%" if tot_size > 0 else "0.0%",
            "remain_percent": f"{(tot_remain / tot_size * 100):.1f}%" if tot_size > 0 else "100.0%",
            # 国际版的 `get-user-resource` 没有档位字段，这里用计费摘要的结论
            # （#505）；摘要还没跑过时按免费处理，与旧行为一致。
            "is_paid_user": bool(self.tier_paid()),
            "checkin": getattr(self, "credits", {}).get("checkin") if isinstance(getattr(self, "credits", None), dict) else None,
            "earliest_expiring": earliest_expiring,
            "packages": packages,
            "updated_at": time.time(),
            "updated_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))
        return {"ok": True, "credits": self.credits}

    def jwt_enterprise_id(self, token=None):
        """企业空间 id，取自 token 自身（或已存的 accessToken）。

        企业账号是登录时切到企业空间换来的 token（token_source=enterprise_switch），
        账号文件里的 enterpriseId 常常是空的——只有 token 里有。拿不到就返回 ""。
        """
        return str(_jwt_claims(token or self.access_token).get("enterprise_id") or "")

    def is_enterprise(self):
        """True when this account belongs to an enterprise (team) space.

        Enterprise members have no personal resource packages, so the personal
        billing endpoint always answers TotalCount=0 and the panel shows 0/0.
        Their credit lives on /billing/meter/get-enterprise-user-usage instead.
        """
        return bool(self.enterprise_id or self.jwt_enterprise_id())

    def uses_enterprise_billing(self):
        """额度是不是该走企业口径端点。

        静态 API Key 也走这一条：它没有个人账号身份，个人
        get-user-resource 对它返回 200 但 Accounts 为空（实测），复用个人
        逻辑的话余额恒 0，积分保底会把可用号当成耗尽号挡死。
        """
        return bool(self.is_api_key or self.is_enterprise())

    def _fetch_credits_enterprise(self):
        """企业账号积分：周期内已用 + 周期额度，剩余 = 额度 - 已用。

        上游只回 4 个字段：credit（本周期已消耗，实测随时间单调递增）、
        limitNum（周期额度）、cycleStartTime / cycleEndTime。没有包列表，
        所以合成一个包条目，让看板的明细弹窗与到期提示照常工作。
        """
        cfg = get_realm_config(self.realm)
        # 静态 Key 必须走 /v2 前缀（见 ENTERPRISE_USAGE_PATH_V2 的注释）。
        path = (ENTERPRISE_USAGE_PATH_V2 if self.is_api_key
                else ENTERPRISE_USAGE_PATH)
        url = cfg["billing_upstream"] + path
        headers = self.headers(purpose="billing")
        headers["X-Client-Platform"] = "web"
        try:
            res = http_json(url, data=b"{}", method="POST", headers=headers,
                            timeout=15, proxy=self.proxy)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        data = res.get("data") or {}
        try:
            used = round(float(data.get("credit") or 0), 2)
        except (TypeError, ValueError):
            used = 0.0
        try:
            size = round(float(data.get("limitNum") or 0), 2)
        except (TypeError, ValueError):
            size = 0.0
        # 上游只给额度与已用；已用超过额度（或额度缺失）时不要算出负余额。
        remain = round(max(0.0, size - used), 2) if size > 0 else 0.0

        cycle_start = str(data.get("cycleStartTime") or "")
        cycle_end = str(data.get("cycleEndTime") or data.get("cycleResetTime") or "")
        # 企业额度按周期重置（额度回满），不是到期作废：cycleEndTime 到期后
        # 额度是回满而不是清零。所以它不参与到期倒计时——把它算成「即将到期」
        # 会让企业账号永远落在临期窗口里。
        days_left = None
        is_expired = False
        no_expiry = True

        package = {
            "name": "企业额度" if size else "企业周期额度",
            "package_code": "enterprise",
            "product_name": "",
            "sub_product_name": "",
            "grant_reason": "企业空间发放",
            "resource_id": "",
            "deal_name": "",
            "create_time": cycle_start,
            "remain": remain,
            "used": used,
            "size": size,
            "unit": "credits",
            "in_usage": bool(used > 0),
            "auto_renew": True,
            "cycle_start_time": cycle_start,
            "cycle_end_time": cycle_end,
            "expire_time": "",
            "no_expiry": no_expiry,
            "days_left": days_left,
            "is_expired": is_expired,
            "status": 0,
        }
        self.credits = {
            "remain": remain,
            "used": used,
            "size": size,
            "used_percent": ("%.1f%%" % (used / size * 100)) if size > 0 else "0.0%",
            "remain_percent": ("%.1f%%" % (remain / size * 100)) if size > 0 else "100.0%",
            "is_paid_user": True,
            "is_enterprise": True,
            "checkin": None,
            "earliest_expiring": None,
            "packages": [package] if size > 0 else [],
            "updated_at": time.time(),
            "updated_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))
        return {"ok": True, "credits": self.credits}

    def tier_paid(self):
        """True / False once the billing summary has answered, None when unknown.

        `None` is not "free": a summary that failed or never ran must not
        quietly withhold PRO models from a paying account. Callers fall back to
        the last credits snapshot instead (see __init__).
        """
        tier = self.tier if isinstance(self.tier, dict) else None
        if not tier:
            return None
        paid = tier.get("paid")
        return bool(paid) if paid is not None else None

    def tier_is_fresh(self, now=None):
        """True while the stored tier is inside TIER_TTL_SECONDS."""
        tier = self.tier if isinstance(self.tier, dict) else None
        if not tier or tier.get("paid") is None:
            return False
        try:
            checked = float(tier.get("checked_at") or 0)
        except (TypeError, ValueError):
            return False
        now = time.time() if now is None else now
        # `0 < delta` 会漏掉「刚读完」那一瞬间：同一毫秒内 delta 就是 0.0，
        # 于是第二次调用又去请求一次。只要求时间戳有效、且没超过 TTL。
        return checked > 0 and (now - checked) < TIER_TTL_SECONDS

    def apply_tier(self, info):
        """Record a billing-summary answer and mirror it into the credits view.

        The panel reads `credits.is_paid_user`; keeping the two in step is what
        makes the badge and the model gate agree. A partial answer is ignored
        rather than stored, so a malformed response cannot erase a known tier.
        """
        if not isinstance(info, dict) or info.get("paid") is None:
            return False
        self.tier = dict(info)
        self.is_paid_user = bool(info.get("paid"))
        if isinstance(self.credits, dict):
            self.credits["is_paid_user"] = self.is_paid_user
        return True

    def _fetch_tier_summary(self, headers=None):
        """Read the tier from the billing summary (#505).

        CN's detailed balance path already calls this endpoint, so it hands the
        parsed answer to apply_tier() instead of paying for a second request.
        The intl balance path (`get-user-resource`) carries neither
        `IsPaidUser` nor `SubscriptionPackageName`, so there this is one extra
        request per TIER_TTL_SECONDS.

        Returns None on any failure. "No answer" must never become a tier
        conclusion: 401 / non-zero code / network error leaves the previous
        value and its timestamp exactly as they were.
        """
        if self.uses_enterprise_billing():
            # 企业空间的 IsPaidUser 语义未验证（#505），先按「未知」处理。
            # 静态 Key 的个人计费摘要端点直接 401，同样按未知处理。
            return None
        if self.restricted_until > time.time():
            # 账号正被上游限制：这时再打计费接口只会继续喂风控。
            return None
        if headers is None:
            headers = self.headers(purpose="billing")
        headers = dict(headers)
        headers["X-Client-Platform"] = "web"
        url = get_realm_config(self.realm)["billing_upstream"] + RESOURCE_SUMMARY_PATH
        try:
            res = http_json(url, data=b"{}", method="POST", headers=headers,
                            timeout=15, proxy=self.proxy)
        except Exception:
            return None
        if not isinstance(res, dict) or res.get("code") != 0:
            return None
        return tier_from_summary(res.get("data"))

    def _refresh_tier_if_due(self):
        """Refresh the tier at most once per TIER_TTL_SECONDS."""
        if self.tier_is_fresh():
            return None
        info = self._fetch_tier_summary()
        if not info or not self.apply_tier(info):
            return None
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))
        return info

    def fetch_credits(self):
        """Refresh the balance; a refresh that shows credits again also lifts
        an early 402 park (revive_balance_cooldown).

        Serialised per account: the background refresher and the sign-in /
        daily-activity tasks all land here, and without the lock two
        overlapping calls would each spend an upstream billing request and
        then race to publish whichever answer came back second.
        """
        with self._credits_lock:
            res = self._fetch_credits_raw()
            if isinstance(res, dict) and res.get("ok"):
                self.revive_balance_cooldown()
                # 档位跟着积分刷新走（#505）：同一处节流、同一个后台循环，
                # 不新开定时器；失败不写结论，旧值和时间戳原样保留。
                tier = self._refresh_tier_if_due()
                if tier:
                    res = dict(res)
                    res["tier"] = tier
            return res

    def _fetch_credits_raw(self):
        # 静态 API Key 没有套餐概览/个人资源包：/billing/meter/get-user-resource-summary
        # 对它直接 401（实测，APISIX 层挡掉），个人 get-user-resource 返回空
        # Accounts。所以不进个人链路，直接问企业口径。
        if self.is_api_key:
            ent = self._fetch_credits_enterprise()
            if ent.get("ok"):
                return ent
            return ent
        if self.realm == "cn":
            try:
                res = self._fetch_credits_cn_detailed()
                # 企业账号在个人计费接口上没有资源包：返回 ok 但全是 0。
                # 这时改问企业用量接口，否则看板永远是 0/0。
                if res.get("ok") and not (self.credits or {}).get("size"):
                    if self.is_enterprise():
                        ent = self._fetch_credits_enterprise()
                        if ent.get("ok"):
                            return ent
                if res.get("ok"):
                    return res
                # 若详细接口明确返回认证失败，直接上报，不再尝试 fallback
                # （fallback 使用相同 token，会得到同样的 401）。
                err = res.get("error", "")
                if err and (_auth_failure_text(err) or "登录" in err):
                    self.token_expired = True
                    self._set_last_error("登录已过期，请重新登录")
                    return res
            except Exception:
                pass
            if self.is_enterprise():
                try:
                    ent = self._fetch_credits_enterprise()
                    if ent.get("ok"):
                        return ent
                except Exception:
                    pass
        return self._fetch_credits_fallback()

    def _set_last_error(self, message):
        """Record refresh errors alongside the state shown in the panel."""
        with self._throttle_lock:
            self.last_error = str(message)[:200]
            self.last_error_detail = ""

    def note_error(self, message, cooldown=60, single_account=False, model=None, until=None,
                   detail=None):
        with self._throttle_lock:
            self.last_error = str(message)[:200]
            # The visible label keeps its existing truncation; `detail` carries the
            # untouched upstream body so the panel can show it on hover.
            self.last_error_detail = str(detail)[:2000] if detail else ""
            if _auth_failure_text(message):
                self.token_expired = True
            if model:
                # Model-scoped throttle: keep the account usable for every other model.
                wait = max(1.0, float(until) - time.time()) if until else (
                    3.0 if single_account else float(cooldown))
                now = time.time()
                self.model_cooldowns[model] = now + wait
                # 起始时刻：面板要如实说「限流时刻 + 恢复时间」，而恢复时刻减不出
                # 起始时刻（wait 来自 60s→10m 的阶梯，不落库）。记这一次就够了。
                self.model_cooldowns_at[model] = now
                return
            actual_cooldown = 3 if single_account else cooldown
            self.cooldown_until = time.time() + actual_cooldown

    def note_restricted(self, detail="", cooldown=None):
        """上游账号级风控限制（403 / code 11140）：按小时冷却，且不随重启消失。

        实测正文：{"code":11140,"msg":"request illegal",...,"displayMsg":
        「账号存在异常行为，已被限制使用」}。官方客户端同样报这个错，说明是
        账号级标记而不是请求内容（magpie #1849 的对照：同一请求同一套头，有的
        账号 200、有的 403）。社区复盘（Zhengyuuuui/workbuddy2api 封禁事件）
        给出的处置口径是：看到 11140 立即停手，解封前严禁换号继续打同一台设备，
        否则设备指纹会连坐。

        与普通冷却的区别：
        - 固定 2 小时（可用 cooldown 覆盖），不吃 single_account 的 3 秒兜底——
          把被限制的账号每 3 秒再试一次，只会继续喂上游风控；
        - 持久化到凭据档（restrictedUntil）：重启 / 刷新 token / 换身分 /
          「一键测试」都不会把它清掉，只有真正成功一次（说明上游已解除）才清。
        """
        now = time.time()
        wait = RESTRICTED_ACCOUNT_COOLDOWN if cooldown is None else float(cooldown)
        with self._throttle_lock:
            self.last_error = "HTTP 403 (account restricted)"
            self.last_error_detail = str(detail)[:2000] if detail else ""
            # 第一次命中才起算：反复探测/重试不该把 2 小时越推越长。
            if self.restricted_until <= now:
                self.restricted_until = now + max(1.0, wait)
        return self.restricted_until

    def note_balance_cooled(self, message="insufficient credits"):
        """402 / out of credits: park until the next local 04:00.

        A short cooldown would hand the empty account out again minutes
        later, and every request to it fails the same way; the daily reset
        is when credits come back, so that is the wall clock this waits for.
        A refresh that shows credits again can lift it early - see
        revive_balance_cooldown().
        """
        until = next_local_4am()
        with self._throttle_lock:
            self.last_error = str(message)[:200]
            self.balance_until = max(self.balance_until, until)
        return until

    def revive_balance_cooldown(self):
        """A balance refresh that shows credits again lifts the 402 park only.

        Rate-limit and model cooldowns stay exactly as they are; this is not
        a general clear_error().
        """
        if not self.balance_until:
            return False
        credits = self.credits if isinstance(self.credits, dict) else {}
        try:
            remain = int(credits.get("remain"))
        except (TypeError, ValueError):
            remain = None
        if remain is None or remain <= 0:
            return False
        with self._throttle_lock:
            self.balance_until = 0.0
            self.last_error = ""
            self.last_error_detail = ""
        return True

    def note_soft_rate(self, message):
        """Account-level rate limit: soft cooldown with exponential backoff."""
        with self._throttle_lock:
            self._decay_soft_streak()
            self.soft_streak += 1
            wait = soft_backoff(self.soft_streak)
            self.last_error = str(message)[:200]
            self.cooldown_until = max(self.cooldown_until, time.time() + wait)
        return wait

    def _decay_soft_streak(self):
        """软限流阶梯也要有时间窗（调用方已持有 _throttle_lock）。

        这两条阶梯原先只加不减、只有成功才归零：昨天被限过 3 次，今天第一个
        429 就直接从 2400s 起步，后天起步更高，直到 7200s 封顶——账号明明已经
        恢复正常（面板测试通过），却动不动被按几十分钟到两小时，这正是「不断
        软限流」的另一半。窗口内才算连续，隔得久就回到第一档（600s / 60s）。
        """
        now = time.time()
        if FAIL_WINDOW and self.last_soft_at and now - self.last_soft_at > FAIL_WINDOW:
            self.soft_streak = 0
            self.unscoped_streak = 0
        self.last_soft_at = now

    def note_unscoped_rate(self, model):
        """An unscoped soft 429: grow its own streak, return this model's window.

        Counting is deliberately separate from soft_streak. Sharing one counter
        let the ladders feed each other: four unscoped 429s pushed the first
        genuinely credential-scoped one straight to the 7200s ceiling instead of
        starting at 600s, and a credential streak made the next unscoped window
        start high as well. Each scope now has its own count, and a served
        request clears both through note_success().
        """
        with self._throttle_lock:
            self._decay_soft_streak()
            self.unscoped_streak += 1
            return unverified_soft_backoff(self.unscoped_streak)

    def note_failure(self, message):
        """5xx / transport failure: feed the breaker counter."""
        with self._throttle_lock:
            self.last_error = str(message)[:200]
            self._decay_strikes()
            self.fails += 1
            if self.fails >= BREAKER_THRESHOLD:
                wait = breaker_backoff(self.fails)
                self.breaker_until = max(self.breaker_until, time.time() + wait)
        return self.breaker_until

    def _decay_strikes(self):
        """两次失败隔得太久就不再算「连续」，从头计数。

        调用方必须已持有 _throttle_lock。窗口设 0 时保持旧的累计语义。
        """
        now = time.time()
        if FAIL_WINDOW and self.last_fail_at and now - self.last_fail_at > FAIL_WINDOW:
            self.fails = 0
            self.degrade_count = 0
        self.last_fail_at = now

    def note_unknown_failure(self, message):
        """Unknown error: degrade counter plus the shared breaker counter."""
        with self._throttle_lock:
            self.last_error = str(message)[:200]
            self._decay_strikes()
            self.degrade_count += 1
            self.fails += 1
            now = time.time()
            if self.degrade_count >= DEGRADE_THRESHOLD:
                wait = degrade_backoff(self.degrade_count)
                self.degrade_until = max(self.degrade_until, now + wait)
            if self.fails >= BREAKER_THRESHOLD:
                wait = breaker_backoff(self.fails)
                self.breaker_until = max(self.breaker_until, now + wait)
        return self.degrade_until

    def note_success(self, model=None):
        """A served request clears every account-level penalty.

        That includes a persisted upstream restriction: a request that actually
        succeeded is the one piece of evidence that the vendor lifted it. (In
        normal dispatch a restricted account is never picked, so this only fires
        from a manual test.)

        It also lifts the `token_expired` park. A response the upstream really
        served is direct proof that the credential still works, yet that flag is
        process-local and only ever cleared by a successful refresh, a re-login
        or a restart. So an account parked by one credential-shaped 401 stayed
        unusable for the rest of the process lifetime: the panel's "test" would
        return a real model reply and still render 「凭证失效」, because
        `ready()` kept the account out of rotation. Nothing re-tests the token
        in between, so it never came back on its own.
        """
        with self._throttle_lock:
            if model:
                self.model_cooldowns.pop(model, None)
                self.model_cooldowns_at.pop(model, None)
            self.soft_streak = 0
            self.unscoped_streak = 0
            self.fails = 0
            self.degrade_count = 0
            self.last_fail_at = 0.0
            self.last_soft_at = 0.0
            self.breaker_until = 0.0
            self.degrade_until = 0.0
            self.restricted_until = 0.0
            self.last_error = ""
            self.last_error_detail = ""
            self.cooldown_until = 0.0
            self.token_expired = False

    def throttle_wait(self, model=None):
        """Seconds until this account can serve `model` again (0 = right now)."""
        if not self.enabled or not self.access_token:
            return 0.0
        now = time.time()
        with self._throttle_lock:
            wait = max(0.0, self.cooldown_until - now,
                       self.balance_until - now, self.breaker_until - now,
                       self.degrade_until - now, self.restricted_until - now)
            if model:
                wait = max(wait, max(0.0, self.model_cooldowns.get(model, 0.0) - now))
        return wait

    def clear_error(self, model=None, keep_caps=False):
        """清掉限流状态；keep_caps 保留「上游判定额度用满」的禁用。

        换身分/重试要清的是限流窗口，而 6004 的禁用是上游给的窗口事实（恢复
        时刻之前这个组合就是不可用），不能顺手清掉——真机实测（2026-10-10）：
        auto-switch 开着时它会把刚记下的 cap 冷却一起清掉，6004 之后账号立刻
        又可用，禁用形同不存在。
        """
        with self._throttle_lock:
            if model:
                if not (keep_caps and self.cap_until(model) > 0):
                    self.model_cooldowns.pop(model, None)
                    self.model_cooldowns_at.pop(model, None)
            elif keep_caps:
                now = time.time()
                for mid in [m for m in self.model_cooldowns
                            if self.cap_until(m, now) <= 0]:
                    self.model_cooldowns.pop(mid, None)
                    self.model_cooldowns_at.pop(mid, None)
            else:
                self.model_cooldowns.clear()
                self.model_cooldowns_at.clear()
            if (self.last_error or self.cooldown_until
                    or self.last_error_detail or self.balance_until):
                self.last_error = ""
                self.last_error_detail = ""
                self.cooldown_until = 0
                self.balance_until = 0.0
            # 上游账号级风控限制（restricted_until）故意不在这里清：它不是本机
            # 记的限流窗口，而是上游对账号的标记；换身分 / 重试 / 刷新都不该把
            # 它放回池子（见 note_restricted）。

    def clear_model_cooldown(self, model):
        """只解除某个模型的限流，账号级状态原样保留。

        额度窗口探针（wb_window_probe）用它：探针证明该模型这条配额线已经
        重置，但这不等于账号本身恢复——一次网络故障留下的 cooldown_until
        不该被顺手清掉，否则 60 秒的账号冷却会被探针提前解除。
        """
        if not model:
            return
        with self._throttle_lock:
            self.model_cooldowns.pop(model, None)
            # 起始时刻一起清：留着它，同一个模型下次再被限流时，面板会显示
            # 上一次的开始时间（早于本次限流），把「刚开始 2 分钟」说成「很久了」。
            self.model_cooldowns_at.pop(model, None)

    def arm_model_cooldown(self, model, until, started_at=None):
        """把一条**已知**的限流窗口挂成模型冷却（重启后恢复用）。

        与 note_error 的分工：那个是「刚撞到」的现场记录，会一起写 last_error
        与 last_error_detail；这个是从盘上读回来的恢复，只补冷却，不改错误文案
        ——重启不该把「上次的报错」重新贴到账号行上。

        返回是否真的挂上了（窗口已过期或已有更晚的冷却时返回 False）。
        """
        if not model:
            return False
        try:
            until = float(until)
        except (TypeError, ValueError):
            return False
        now = time.time()
        if until <= now:
            return False
        with self._throttle_lock:
            if self.model_cooldowns.get(model, 0.0) >= until:
                return False
            self.model_cooldowns[model] = until
            try:
                started = float(started_at or 0)
            except (TypeError, ValueError):
                started = 0.0
            if started:
                self.model_cooldowns_at.setdefault(model, started)
        return True

def _human_delta(seconds):
    if seconds is None: return None
    if seconds <= 0: return "expired"
    days = seconds / 86400.0
    if days >= 1: return "%.0f days" % days
    hours = seconds / 3600.0
    if hours >= 1: return "%.1f hours" % hours
    return "%d min" % int(seconds / 60)


def _auth_failure_text(text):
    """True for a credential-shaped 401, not an intermediate proxy page.

    A bare 401 from openresty / nginx (an HTML error page) is not evidence
    that the token is dead: parking the account on it would take a healthy
    credential out of rotation until a manual re-login (#435). Only mark the
    account when the failure itself names authorization.
    """
    lower = str(text or "").lower()
    if "<html" in lower:
        return False
    return ("401" in lower or "unauthorized" in lower or "authorization" in lower
            or "token expired" in lower or "invalid_token" in lower)


def _human_left(seconds):
    """还剩多久，精确到秒（对外解释「为什么现在不能用」时用）。

    `_human_delta` 是给凭据有效期那种以天计的场景用的，小于一分钟一律写
    「0 min」；而冷却/熔断的剩余时间经常就是几十秒，写成 0 分钟会让人以为
    账号已经可用、网关还在挡它。
    """
    secs = max(0, int(round(seconds)))
    if secs < 60:
        return "%ds" % secs
    if secs < 3600:
        m, s = divmod(secs, 60)
        return "%dm%02ds" % (m, s) if s else "%dm" % m
    if secs < 86400:
        h, rem = divmod(secs, 3600)
        m = rem // 60
        return "%dh%02dm" % (h, m) if m else "%dh" % h
    return "%.1f days" % (secs / 86400.0)

class SessionAffinity(object):
    """Which upstream account served a conversation, so the next turn can go
    back to it.

    Each conversation keeps a short most-recently-used list rather than a single
    account. The front entry is what `get()` returns - the account the last turn
    went to. The entries behind it are the accounts that served the conversation
    before that, most recent first: when the front one is unusable (cooling,
    parked, disabled) the next pick prefers an account this conversation has
    already warmed upstream over a cold one, so the prompt cache survives the
    move instead of being rebuilt from scratch. Every entry expires on its own
    clock, so an account the conversation has not visited for a while drops out
    by itself.

    The history is small on purpose (`history`, 3): it is a list of accounts
    that may still hold the conversation's prefix, not a load-balancing device.
    """

    def __init__(self, ttl=7200, max_entries=5000, history=3):
        self.ttl = ttl
        self.max_entries = max_entries
        self.history = max(1, int(history))
        self.bindings = {}
        self._lock = threading.Lock()
    def _live(self, key, now):
        """The conversation's unexpired entries, dropping the rest in place."""
        entries = self.bindings.get(key) or []
        live = [entry for entry in entries if entry[1] > now]
        if live:
            self.bindings[key] = live
        else:
            self.bindings.pop(key, None)
        return live
    def get(self, key):
        """The account the last turn went to (slides its expiry), or None."""
        if not key: return None
        with self._lock:
            now = time.time()
            live = self._live(key, now)
            if not live: return None
            uid, _exp = live[0]
            live[0] = (uid, now + self.ttl)
            return uid
    def recent(self, key):
        """Every unexpired account of this conversation, most recent first."""
        if not key: return []
        with self._lock:
            return [uid for uid, _exp in self._live(key, time.time())]
    def bind(self, key, uid):
        """Make `uid` the front entry, deduped and capped at `history`."""
        if not key or not uid: return
        now = time.time()
        with self._lock:
            if len(self.bindings) >= self.max_entries:
                self.bindings = {k: v for k, v in self.bindings.items()
                                 if v and v[0][1] > now}
            entries = [entry for entry in (self.bindings.get(key) or [])
                       if entry[0] != uid]
            entries.insert(0, (uid, now + self.ttl))
            self.bindings[key] = entries[:self.history]
    def demote(self, key, uid=None):
        """Drop one account from the conversation, keeping the others.

        The failure paths use this instead of `unbind`: the account that just
        failed (429 / 402 / 401 / 5xx / dropped connection) must not be handed
        the next attempt, but the accounts before it may still hold this
        conversation's prefix upstream - the next pick should try them before
        falling back to a cold one. Dropping the front entry is the default;
        pass `uid` to drop a specific one.
        """
        if not key: return
        with self._lock:
            entries = self.bindings.get(key) or []
            if uid is None:
                entries = entries[1:]
            else:
                entries = [entry for entry in entries if entry[0] != uid]
            if entries:
                self.bindings[key] = entries
            else:
                self.bindings.pop(key, None)
    def unbind(self, key):
        if not key: return
        with self._lock:
            self.bindings.pop(key, None)

def _realm_limit(values, realm):
    """One guard's value for an account's realm.

    `values` is the map wb_settings.limit_values() hands over -
    {"global": g, "intl": ..., "cn": ...}. A blank or unrecognised realm falls
    back to the global default, so an account whose realm could not be detected
    is still guarded by the global number.
    """
    if isinstance(values, dict):
        value = values.get(realm)
        if value is None:
            value = values.get("global")
    else:
        value = values
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


class CreditsRefresher(threading.Thread):
    """Keep account credit balances fresh, off the request path.

    The dispatch preference reads `account.credits`, which only the sign-in and
    daily-activity tasks used to refresh: an account could go days without an
    update, and a restart inherited whatever balance was on disk. Every tick
    this refreshes the single stalest account that is past the TTL, so the
    upstream billing calls stay bounded - one per tick - and land spread across
    the pool instead of arriving as a burst. A failed account is parked for six
    hours so a broken credential cannot be retried on every tick.

    The request volume is capped by the tick, not by the pool size: at the
    default half-hour tick that is at most 48 billing calls a day however many
    accounts there are. With a 12-hour TTL a 28-account pool wants 56 calls a
    day, so the tick - not the TTL - is the binding limit and the effective
    refresh age settles a little above the TTL. That is the intended trade:
    the preference is measured in days, so a few extra hours of drift is worth
    far less than the calls it saves.
    """

    def __init__(self, pool, interval_seconds=1800):
        super().__init__(daemon=True, name="credits-refresher")
        self.pool = pool
        self.interval_seconds = max(60.0, float(interval_seconds))
        self.last_run = None
        self.last_uid = None
        self.last_error = None
        self.logs = []
        self._stop_event = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._parked = {}          # uid -> epoch until which we skip it
        self._failed_cooldown = 6 * 3600.0

    def log(self, msg):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self.logs.append("[%s] %s" % (stamp, msg))
        if len(self.logs) > 40:
            self.logs = self.logs[-40:]

    def stop(self):
        self._stop_event.set()
        self._wake.set()

    def wake(self):
        self._wake.set()

    def ttl_seconds(self):
        """The current TTL in seconds - the one place the unit is applied."""
        try:
            import wb_settings
            hours = wb_settings.credits_refresh_hours(self.pool.dir)
        except Exception:
            return 0.0
        try:
            return max(0.0, float(hours)) * 3600.0
        except (TypeError, ValueError):
            return 0.0

    def stalest_account(self, ttl_seconds, now=None):
        """The account whose balance is oldest and past the TTL, or None.

        A balance that was never fetched counts as infinitely old, so a fresh
        install fills in its accounts before any timed-out one is revisited.
        """
        now = time.time() if now is None else now
        with self.pool._lock:
            accounts = list(self.pool.accounts)
        chosen = None
        chosen_age = None
        for account in accounts:
            if not account.enabled or not account.access_token:
                continue
            if self._parked.get(account.uid, 0) > now:
                continue
            credits = account.credits
            updated = credits.get("updated_at") if isinstance(credits, dict) else None
            if isinstance(updated, (int, float)):
                age = now - updated
            else:
                age = float("inf")
            if age < ttl_seconds:
                continue
            if chosen_age is None or age > chosen_age:
                chosen, chosen_age = account, age
        return chosen

    def refresh_once(self, now=None):
        """Refresh one stale account; returns its uid, or None when none was due."""
        ttl = self.ttl_seconds()
        if ttl <= 0:
            return None
        with self._lock:
            account = self.stalest_account(ttl, now=now)
            if account is None:
                return None
            try:
                result = account.fetch_credits()
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            if not isinstance(result, dict) or not result.get("ok"):
                error = (result or {}).get("error") or "unknown error"
                self._parked[account.uid] = \
                    (time.time() if now is None else now) + self._failed_cooldown
                self.last_error = str(error)
                self.log("刷新 %s 积分失败，暂缓一小时: %s"
                         % (account.uid[:8], error))
                return None
            self.last_run = time.time() if now is None else now
            self.last_uid = account.uid
            self.last_error = None
            self.log("已刷新 %s 的积分（%s）"
                     % (account.uid[:8], (account.credits or {}).get("updated_iso")))
            tier = result.get("tier") if isinstance(result, dict) else None
            if isinstance(tier, dict):
                self.log("已读取 %s 的账号档位：%s%s"
                         % (account.uid[:8],
                            "付费" if tier.get("paid") else "免费",
                            (" · " + tier["package"]) if tier.get("package") else ""))
            return account.uid

    def run(self):
        while not self._stop_event.is_set():
            self._wake.wait(timeout=self.interval_seconds)
            if self._stop_event.is_set():
                break
            self._wake.clear()
            try:
                self.refresh_once()
            except Exception as exc:
                self.last_error = str(exc)
                self.log("刷新循环异常: %s" % exc)

    def status(self):
        return {
            "ttl_hours": round(self.ttl_seconds() / 3600.0, 2),
            "interval_seconds": self.interval_seconds,
            "last_run": self.last_run,
            "last_uid": self.last_uid,
            "last_error": self.last_error,
            "logs": self.logs[-10:],
        }

# 和账号凭证放在同一个目录、但本身不是账号的文件。跳过它们，免得每次启动
# 都被当成「损坏的账号」解析并打一条 error 日志（settings.json 是面板设置，
# active_realm.json 是区域开关）。
NON_ACCOUNT_FILES = ("settings.json", "active_realm.json")


# 和账号凭证放在同一个目录、但本身不是账号的文件。跳过它们，免得每次启动
# 都被当成「损坏的账号」解析并打一条 error 日志（settings.json 是面板设置，
# active_realm.json 是区域开关，model_windows.json 是额度窗口记录，
# quota_scan.json 是主动额度查询记录）。
NON_ACCOUNT_FILES = ("settings.json", "active_realm.json", "model_windows.json", "quota_scan.json")


class AccountPool(object):
    def __init__(self, directory, log=None):
        self.dir = directory
        self.log = log or (lambda msg: None)
        self.accounts = []
        self.logins = {}
        self._lock = threading.RLock()
        self._cursor = 0
        # Smooth weighted round-robin state for the expiring-credits window,
        # keyed by uid (see _weighted_pick). Runtime-only: a restart just
        # restarts the rotation.
        self._expiry_weights = {}
        # 剩余用量优先调度：wb_proxy 每请求推来的 {(uid, model): 权重}（只有它
        # 读得到剩余估算）。空表 = 不启用——开关关着、或没有任何组合接近估计
        # 限额——选择就是原来的纯轮询。纯派生值，不落盘。
        self._remaining_weights = {}
        # 这条偏好自己的平滑加权轮询累计值，键是 (uid, model)：与临期的
        # _expiry_weights 分开，两个偏好各自记各自的轮次。
        self._remaining_pick_state = {}
        # 手动权重优先（面板按账号设置，weight > 1 才生效）的平滑加权轮询
        # 累计值，键是 uid：与前两条偏好各自的轮次分开。派生值，不落盘。
        self._manual_weights = {}
        # 页内轮转（超限对话）的每页游标，键是 (页号, 页大小)；整池轮转仍用
        # _cursor，两条轮转互不挪动对方的位置。派生值，不落盘。
        self._page_cursors = {}
        # uid 排序表的缓存：池成员没变就直接复用，变了重建（见 _page_ranks）。
        self._page_order_cache = None
        self.affinity = SessionAffinity()

    def load(self):
        with self._lock:
            self.accounts = []
            if not os.path.isdir(self.dir): return self.accounts
            for name in sorted(os.listdir(self.dir)):
                if not name.endswith(".json") or name in NON_ACCOUNT_FILES: continue
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
            self.apply_expiring_window()
            # 6004 的恢复时间跨重启有效：把还在恢复时间内的组合重新挂成模型
            # 冷却，否则重启会把它们重新放出去，直到下次撞 429 才又停。
            self.restore_model_caps()
            # 普通 429（非 6004，比如国内版的频率限制）带的重置时刻由窗口探针
            # 落在 model_windows.json 里，跨重启有效；但 restore_model_caps 只读
            # 凭证档的 modelCaps，读不到它。不补这一次，重启后这些窗口就只对
            # 探针可见：池子照常派单（每单再吃一次 429），面板「当前禁用」也空着。
            self.restore_window_cooldowns()
            return self.accounts

    def restore_model_caps(self, now=None):
        """把仍在恢复时间内的 6004 挂回每个账号的模型冷却上。"""
        now = time.time() if now is None else now
        restored = {}
        with self._lock:
            for account in self.accounts:
                mids = account.restore_model_caps(now)
                if mids:
                    restored[account.uid] = mids
        for uid, mids in sorted(restored.items()):
            self.log("account %s: 模型 %s 仍在额度恢复期内（至上游给定时刻）"
                     % (uid[:8], ", ".join(mids)))
        return restored

    def restore_window_cooldowns(self, now=None):
        """把 wb_window_probe 登记的限流窗口挂回模型冷却（重启后恢复）。

        与 restore_model_caps 的分工：那个管 6004（配额用满，来源是凭证档的
        modelCaps），这个管「429 带重置时刻」的普通限流（来源是
        accounts/model_windows.json）。两者都只在 reset_at 还没到时才挂，到点由
        窗口探针清掉（_fire 成功后调 clear_model_cooldown），不会永久钉住账号。

        读不到探针状态（模块缺失、文件坏）只写日志，不影响加载：宁可这次不恢复，
        也不能因为一个附属文件让整个账号池加载失败。
        """
        now = time.time() if now is None else now
        directory = getattr(self, "dir", "") or ""
        if not directory:
            return {}
        try:
            import wb_window_probe
            windows = wb_window_probe.snapshot(directory, now)
        except Exception as exc:
            self.log("窗口探针状态读取失败（跳过限流窗口恢复）: %s" % exc)
            return {}
        restored = {}
        with self._lock:
            by_uid = {a.uid: a for a in self.accounts}
            for row in windows or ():
                uid = str(row.get("uid") or "")
                model = str(row.get("model") or "")
                account = by_uid.get(uid)
                if account is None or not model:
                    continue
                if account.arm_model_cooldown(model, row.get("reset_at")):
                    restored.setdefault(uid, []).append(model)
        for uid, mids in sorted(restored.items()):
            self.log("account %s: 模型 %s 仍在上游限流窗口内（重启后按窗口恢复）"
                     % (uid[:8], ", ".join(mids)))
        return restored

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
                # 重新登录 / 扫码换 token 不该丢掉已经读到的档位（#505）。
                if not account.tier and existing.tier:
                    account.apply_tier(existing.tier)
                if not account.last_checkin and existing.last_checkin:
                    account.last_checkin = existing.last_checkin
                if not account.last_daily_chat and existing.last_daily_chat:
                    account.last_daily_chat = existing.last_daily_chat
                if not account.proxy_slot and existing.proxy_slot:
                    account.proxy_slot = existing.proxy_slot
                if not account.proxy_legacy and existing.proxy_legacy:
                    account.proxy_legacy = existing.proxy_legacy
                account.token_expired = False
                with account._throttle_lock:
                    account.last_error = ""
                    account.last_error_detail = ""
                self.accounts[self.accounts.index(existing)] = account
            else:
                self.accounts.append(account)
            account.save(self.dir)
            self.apply_proxy_slots()
            self.apply_reserve_credits()
            self.apply_expiring_window()
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

    def set_enabled(self, uid, enabled, tasks_enabled=None):
        account = self.get(uid)
        if account is None: return None
        account.enabled = bool(enabled)
        # Disabling stops tasks too unless the caller names the task flag
        # explicitly; the panel sends both when it wants API-only or
        # tasks-only behaviour.
        account.tasks_enabled = (bool(tasks_enabled)
                                 if tasks_enabled is not None
                                 else bool(enabled))
        if enabled:
            account.clear_error()
        account.save(self.dir)
        self.apply_proxy_slots()
        return account.public()

    def set_tasks_enabled(self, uid, tasks_enabled):
        """Toggle task participation without changing API dispatch."""
        account = self.get(uid)
        if account is None:
            return None
        account.tasks_enabled = bool(tasks_enabled)
        account.save(self.dir)
        return account.public()

    def set_weight(self, uid, weight):
        """设置账号的手动调度权重（1-100，1 = 不干预），落盘并回公共视图。"""
        account = self.get(uid)
        if account is None:
            return None
        try:
            account.weight = max(1, min(100, int(weight)))
        except (TypeError, ValueError):
            return None
        account.save(self.dir)
        return account.public()

    def set_note(self, uid, note):
        """设置账号备注（面板标记「批量」/「自用」等），落盘并回公共视图。

        空串 = 清除备注；与调度无关，纯展示。
        """
        account = self.get(uid)
        if account is None:
            return None
        account.note = str(note or "").strip()[:200]
        account.save(self.dir)
        return account.public()

    def set_disabled_models(self, uid, models):
        """设置该账号**人工**禁用的模型名单，落盘并回公共视图。

        语义：这些模型不再派给这个账号。不影响该账号的其他模型，也不影响其他
        账号对同一模型的可用性 —— 请求会自动落到池子里别的号上。传空列表 =
        清空禁用、恢复原行为。

        名单按 normalise_disabled_models 归一；坏输入退化成空列表（= 不禁用）
        而不是抛错：面板传进来什么形状都不该让一个账号加载失败。
        """
        account = self.get(uid)
        if account is None:
            return None
        account.disabled_models = normalise_disabled_models(models)
        account.save(self.dir)
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

    def apply_reserve_credits(self, values=None):
        """Re-resolve the low-credit guard for every account.

        Same shape as apply_proxy_slots(): settings.json is the source of
        truth and the per-account value is derived here, so the request path
        needs no extra settings lookup. `values` is the guard resolved per
        realm ({"global": g, "intl": ..., "cn": ...}); each account picks its
        own realm, so a per-realm override lands only on that realm's accounts
        while the global default keeps covering both.
        """
        import wb_settings

        if values is None:
            values = wb_settings.limit_values(self.dir, "reserve_credits")
        with self._lock:
            for account in self.accounts:
                account.reserve_credits = _realm_limit(values, account.realm)
        return values

    def apply_expiring_window(self, values=None):
        """Re-resolve the expiring-credits window for every account.

        Same shape as apply_reserve_credits(): settings.json holds the window
        per realm (global by default) and each account reads its own, so the
        request path needs no extra settings lookup. The window is a dispatch
        preference, not a guard: it never makes an account unavailable, it
        only decides who is handed out first.
        """
        import wb_settings

        if values is None:
            values = wb_settings.limit_values(self.dir, "expiring_window_days")
        with self._lock:
            for account in self.accounts:
                account.expiring_window_days = _realm_limit(values, account.realm)
        return values

    def apply_remaining_weights(self, weights):
        """换上 wb_proxy 算好的 (账号, 模型) 权重表（剩余用量优先调度）。

        数据方向与 apply_daily_* 同款：只有 wb_proxy 读得到剩余估算（折叠、
        预算与载荷都在那边），池不反向依赖它，只存派生值。None / 空表表示
        「不启用」——开关关着，或这一刻没有组合接近估计限额——把上次的权重
        清掉，选择立即回到纯轮询（开关关时行为与从前逐字节一致）。
        权重 <= 1 的条目没有偏好意义，直接丢弃。返回存下来的表（测试用）。
        """
        table = {}
        for pair, weight in (weights or {}).items():
            try:
                uid, model = pair
                weight = int(weight)
            except (TypeError, ValueError):
                continue
            if weight > 1:
                table[(str(uid), str(model))] = weight
        with self._lock:
            self._remaining_weights = table
        return table

    def apply_daily_token_limit(self, values=None, usage=None):
        """Re-resolve the daily token guard for every account.

        Same shape as apply_reserve_credits(): settings.json holds the limit
        per realm, while `usage` (uid -> tokens counted today) comes from the
        caller, because only the proxy reads the usage log. Passing None keeps
        the last known counts, so a settings change never turns them into
        "unknown".
        """
        import wb_settings

        if values is None:
            values = wb_settings.limit_values(self.dir, "daily_token_limit")
        with self._lock:
            for account in self.accounts:
                limit = _realm_limit(values, account.realm)
                was_blocked = account.daily_limit_blocked()
                account.daily_token_limit = limit
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
                                    account.daily_tokens_today, limit))
                    else:
                        self.log("account %s resumed: daily token limit cleared"
                                 % str(account.uid)[:8])
        return values

    def apply_daily_credit_limit(self, values=None, credits=None, free_models=None):
        """Re-resolve the daily credit guard for every account.

        Same shape as apply_daily_token_limit(): settings.json holds the
        limit per realm, while `credits` (uid -> spent today) and `free_models`
        (realm -> free model ids) come from the caller, because only the
        proxy reads the usage log and the model catalogue. Passing None
        keeps the last known values, so a settings change never turns them
        into "unknown".
        """
        import wb_settings

        if values is None:
            values = wb_settings.limit_values(self.dir, "daily_credit_limit")
        with self._lock:
            for account in self.accounts:
                limit = _realm_limit(values, account.realm)
                was_blocked = account.credit_limit_reached()
                account.daily_credit_limit = limit
                if credits is not None:
                    try:
                        account.daily_credits_today = float(
                            credits.get(account.uid, 0) or 0)
                    except (TypeError, ValueError):
                        account.daily_credits_today = None
                if free_models is not None:
                    account.free_models = frozenset(
                        free_models.get(account.realm) or ())
                now_blocked = account.credit_limit_reached()
                if now_blocked != was_blocked:
                    if now_blocked:
                        self.log("account %s capped: daily credit limit reached "
                                 "(%s/%s credits today), free models only"
                                 % (str(account.uid)[:8],
                                    account.daily_credits_today, limit))
                    else:
                        self.log("account %s resumed: daily credit limit cleared"
                                 % str(account.uid)[:8])
        return values

    def apply_model_daily_token_limit(self, values=None, per_model=None):
        """Re-resolve the per-model daily token guard for every account.

        `per_model` is uid -> {model: tokens counted today}; None keeps the
        last known counts. A blocked model never takes the whole account
        with it - ready(model) refuses exactly the models that are out of
        budget, and only their state changes are logged.
        """
        import wb_settings

        if values is None:
            values = wb_settings.limit_values(self.dir, "model_daily_token_limit")
        with self._lock:
            for account in self.accounts:
                limit = _realm_limit(values, account.realm)
                was_blocked = account.blocked_model_names()
                account.model_daily_token_limit = limit
                if per_model is not None:
                    raw = per_model.get(account.uid) or {}
                    try:
                        account.model_daily_tokens = {str(k): int(v)
                                                      for k, v in raw.items()}
                    except (TypeError, ValueError, AttributeError):
                        account.model_daily_tokens = None
                now_blocked = account.blocked_model_names()
                for mid in sorted(now_blocked - was_blocked):
                    self.log("account %s model %s parked: daily token limit "
                             "reached (%s/%s tokens today)"
                             % (str(account.uid)[:8], mid,
                                (account.model_daily_tokens or {}).get(mid), limit))
                for mid in sorted(was_blocked - now_blocked):
                    self.log("account %s model %s resumed: daily token limit "
                             "cleared" % (str(account.uid)[:8], mid))
        return values

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
                account.tasks_enabled = bool(enabled)
                if enabled:
                    account.clear_error()
                account.save(self.dir)
        self.apply_proxy_slots()

    def task_accounts(self, realm=None):
        """Accounts that participate in scheduled / manual tasks.

        Kept separate from the API pool: `enabled` controls dispatch, while
        `task_active()` controls check-in, growth, travel and daily activity
        (issue #521).
        """
        with self._lock:
            return [a for a in self.accounts
                    if (not realm or a.realm == realm) and a.task_active()]

    def count_ready(self, realm=None, model=None, allowed=None):
        with self._lock:
            snapshot = [a for a in self.accounts if not realm or a.realm == realm]
        return sum(1 for a in snapshot
                   if a.enabled and a.access_token and a.ready(model=model)
                   and (allowed is None or a.uid in allowed))

    def pick_for_session(self, realm=None, session_key=None, exclude=None, model=None,
                         page=None, allowed=None):
        """Pick the account for one turn of a conversation.

        Three shapes, in order of preference:

        * A bound conversation (the normal case) keeps the account the last turn
          went to.
        * When that account is unusable right now (cooling, parked, disabled),
          the conversation's other warm accounts are tried before the pool's own
          preferences - they may still hold its prefix upstream (see
          SessionAffinity). The account that serves the turn becomes the new
          front entry, and the one it replaced stays behind it as a fallback.
        * `page` marks a conversation that is past the affinity length cap: it
          does not pin, it rotates over one page of the pool (an (index, size)
          pair), so every account of the page keeps a warm copy without piling
          every oversized body onto a single account.
        """
        exclude = exclude or set()
        if session_key and page is None:
            for uid in self.affinity.recent(session_key):
                if uid in exclude:
                    continue
                # A bound conversation must not route around the key's account
                # restriction: the binding was made by a request that was
                # allowed to use it, but this one may not be.
                if allowed is not None and uid not in allowed:
                    continue
                account = self.get(uid)
                if account and account.realm == realm and account.ready(model=model):
                    self.affinity.bind(session_key, uid)
                    return account
        account = self.pick(realm=realm, exclude=exclude, model=model, page=page,
                            allowed=allowed)
        if account is None and page is not None:
            # The page has nobody usable right now. Serving the conversation
            # from outside it beats not serving it at all.
            account = self.pick(realm=realm, exclude=exclude, model=model,
                                allowed=allowed)
        # A page conversation is not bound on purpose: its rotation is what keeps
        # every account of the page warm, and a binding would pin it back to one
        # (the state it was released from). The page cursor carries its rotation.
        if account and session_key and page is None:
            self.affinity.bind(session_key, account.uid)
        return account

    def pick(self, realm=None, exclude=None, model=None, page=None, allowed=None):
        exclude = exclude or set()
        with self._lock:
            snapshot = [a for a in self.accounts if not realm or a.realm == realm]
        if allowed is not None:
            snapshot = [a for a in snapshot if a.uid in allowed]
        if page is not None:
            snapshot = self._page_slice(snapshot, page)
        if not snapshot:
            return None
        # Expiring-credits preference: accounts whose soonest-expiring credit
        # package is inside the window are served first, so credits about to
        # lapse get spent. It only reorders who is offered; an account the
        # window covers is still skipped when it is cooling down, over a daily
        # limit, or excluded. When the window covers nobody - or everyone it
        # covers is unavailable right now - the pool falls back to the plain
        # round-robin it always had, so a single lapsed account can never wedge
        # the realm.
        # 手动权重优先：面板给账号设了 >1 的权重时，这些账号先于一切自动
        # 偏好接单，彼此之间按权重平滑加权轮询——权重高的先被选中、分到的
        # 流量也多，但同层之内仍有轮转，谁也不独占。它们此刻都不可用（冷却、
        # 限额、被排除）时照旧落回下面的自动偏好与轮询，一条偏好卡不住池子。
        # 全部权重都是 1（默认）时这一层直接返回 None，分派与从前完全一致。
        account = self._pick_weighted_first(snapshot, exclude, model)
        if account is not None:
            return account
        account = self._pick_expiring_first(snapshot, exclude, model)
        if account is not None:
            return account
        # 剩余用量优先调度（开关默认关）：估计剩余接近耗尽的 (账号, 模型) 接着
        # 被优先交出（见 _pick_remaining_first）。权重表为空时这条路径直接
        # 返回 None，落到与从前完全相同的纯轮询上。
        account = self._pick_remaining_first(snapshot, exclude, model)
        if account is not None:
            return account
        return self._rotate_pick(snapshot, exclude, model, cursor_key=page)

    def _pick_weighted_first(self, snapshot, exclude, model):
        """手动权重 > 1 的账号优先接单，或 None（没有可交的）。

        与临期积分那条同款的两层结构，只是这层的权重来自用户在面板上的明确
        设置，优先级也最高：权重是「我就想让这个号多用」的直接表达，不该被
        自动偏好覆盖。同层之间按权重平滑加权轮询——两个高权重账号按权重比
        分流量，它们与权重 1 的账号之间则是「先用完我再轮别人」。权重 1 的
        账号完全不进这一层：临期积分、剩余用量这些自动子权重照旧在它们身上
        生效，一行都不变。
        """
        ready = []
        for account in snapshot:
            if account.uid in exclude:
                continue
            if int(getattr(account, "weight", 1) or 1) <= 1:
                continue
            if not account.ready(model=model):
                continue
            ready.append((account, account.uid, int(account.weight)))
        if not ready:
            return None
        return self._smooth_weighted_pick(ready, "_manual_weights")

    def _pick_expiring_first(self, snapshot, exclude, model):
        """Serve the in-window accounts first, or None when none can.

        Within the window the pick is a weighted round-robin: an account's
        share grows the closer its credits are to lapsing, but no account is
        ever handed the whole window. A strict soonest-first order would put
        every request on the one account expiring tomorrow until the upstream
        rate-limited it, which is exactly the burst this spreads out; weighting
        keeps the preference while giving the accounts expiring later a real
        share. Accounts that lapse on the same day carry the same weight, so
        they keep taking turns.
        """
        urgent = [a for a in snapshot if a.in_expiring_window()]
        if not urgent:
            return None
        ready = [a for a in urgent if a.uid not in exclude and a.ready(model=model)]
        if not ready:
            return None
        return self._weighted_pick(ready)

    def _expiry_weight(self, account):
        """How much of the window's traffic this account should take.

        Linear in how close the credits are to lapsing, floored at 1 so an
        account at the far edge of the window still gets served rather than
        starved until the others run dry.
        """
        try:
            window = int(account.expiring_window_days or 0)
        except (TypeError, ValueError):
            window = 0
        days = account.soonest_expiring_days()
        if days is None:
            return 1
        return max(1, int(round(window - days)) + 1)

    def _pick_remaining_first(self, snapshot, exclude, model):
        """剩余估算接近耗尽的 (账号, 模型) 优先接单，或 None（没有可交的）。

        与临期积分那条同款的两层结构：只有权重 > 1（快达到估计限额）的组合
        进入这一层，它们之间按剩余多少平滑加权轮询——越接近耗尽分到的流量
        越多，但同层之内没有谁独占。没有加权的可选项，或它们此刻都不可用
        （冷却、限额、被排除）时落回纯轮询，一个快耗尽的组合不会把整个模型
        卡死。剩余为 0 与没有预算样本的组合由 wb_proxy 侧就不进表：前者没有
        「先用掉」的意义（冷却中的本来也不可用），后者没有可比较的尺度。
        """
        if not model:
            return None
        table = self._remaining_weights
        if not table:
            return None
        ready = []
        for account in snapshot:
            if account.uid in exclude:
                continue
            weight = table.get((account.uid, model))
            if weight is None or weight <= 1:
                continue
            if not account.ready(model=model):
                continue
            ready.append((account, weight))
        if not ready:
            return None
        return self._smooth_weighted_pick(
            [(account, (account.uid, model), weight) for account, weight in ready],
            "_remaining_pick_state",
            keep_uids=set(a.uid for a in snapshot))

    def _weighted_pick(self, ready):
        """临期积分那条的入口：在窗口内的账号之间平滑加权轮询。"""
        return self._smooth_weighted_pick(
            [(account, account.uid, self._expiry_weight(account))
             for account in ready], "_expiry_weights")

    def _smooth_weighted_pick(self, entries, state_name, keep_uids=None):
        """Smooth weighted round-robin over (account, key, weight) triples.

        nginx's algorithm: every entry accrues its weight each pick, the
        largest running total wins, and the winner pays back the whole round's
        weight. Equal weights degenerate to a plain rotation, so same-weight
        entries keep taking turns.

        `key` is what the rotation state is kept under - a uid for the
        expiring-credits window, a (uid, model) pair for the remaining-usage
        preference - and `state_name` names the pool attribute holding that
        state, so the two preferences never share a rotation.

        Without `keep_uids` the state is pruned to exactly the entries taking
        part this pick (the expiring window's behaviour). The remaining-usage
        preference passes every account still in the pool instead: pruning by
        the current pick alone would drop another model's (or another realm's)
        rotation every time it is someone else's turn, and a rotation that
        restarts every pick collapses the weights into "always the first
        entry". Entries of accounts that really left the pool are dropped.

        The rotation state is shared by every request thread, so the
        read-modify-write runs under the pool lock. The `ready()` checks that
        feed this do NOT: those can reach the network (a token refresh), and
        holding the lock across one expiring credential would serialise every
        dispatch in the realm behind it.
        """
        keys = set(key for _account, key, _weight in entries)
        with self._lock:
            stored = getattr(self, state_name)
            if keep_uids is None:
                state = dict((key, value) for key, value in stored.items()
                             if key in keys)
            else:
                state = dict((key, value) for key, value in stored.items()
                             if key[0] in keep_uids)
            total = 0
            chosen = None
            for account, key, weight in entries:
                state[key] = state.get(key, 0) + weight
                total += weight
                if chosen is None or state[key] > state[chosen[1]]:
                    chosen = (account, key)
            if chosen is None:
                return None
            state[chosen[1]] -= total
            setattr(self, state_name, state)
        return chosen[0]

    def _page_slice(self, snapshot, page):
        """The accounts on one page of the pool.

        A page is a fixed slot: an account's rank among the realm's accounts
        (sorted by uid) modulo the page size. Ranking inside the realm - not
        across the whole pool, and not by load order or file name - is what
        keeps a page the size it says it is: with a mixed-realm pool, ranking
        globally would leave a page with one usable account for this realm and
        pin its conversations, the state the length cap exists to leave. Uid
        order also keeps a conversation's page stable across restarts and
        reloads; adding or removing an account reshuffles at most one slot per
        page.

        A page cannot be larger than the realm: a size that covers every
        account (or more) means the page is the realm's whole pool.
        """
        try:
            index, size = int(page[0]), int(page[1])
        except (TypeError, ValueError, IndexError):
            return snapshot
        ranks = self._page_ranks(snapshot)
        if size <= 1 or size >= len(ranks):
            return snapshot
        out = []
        for account in snapshot:
            rank = ranks.get(account.uid)
            if rank is not None and rank % size == index % size:
                out.append(account)
        return out

    def _page_ranks(self, snapshot):
        """uid -> rank among the snapshot's accounts, cached until it changes.

        The snapshot is the realm-filtered pool (readiness is not part of it),
        so the ranks - and therefore the pages - only move when an account is
        added to or removed from that realm.
        """
        uids = tuple(sorted(a.uid for a in snapshot if a.uid))
        cached = self._page_order_cache
        if cached is not None and cached[0] == uids:
            return cached[1]
        ranks = dict((uid, rank) for rank, uid in enumerate(uids))
        self._page_order_cache = (uids, ranks)
        return ranks

    def _rotate_pick(self, group, exclude, model, cursor_key=None):
        """Round-robin one group of accounts, advancing its cursor.

        The whole-pool rotation keeps the single `_cursor` it always had. A
        page rotation keeps its own cursor per page, so page picks and plain
        picks never move each other's position.
        """
        total = len(group)
        if total == 0:
            return None
        with self._lock:
            start = (self._page_cursors.get(cursor_key, 0) if cursor_key is not None
                     else self._cursor)
        for offset in range(total):
            index = (start + offset) % total
            account = group[index]
            if account.uid in exclude:
                continue
            if account.ready(model=model):
                with self._lock:
                    if cursor_key is not None:
                        self._page_cursors[cursor_key] = (index + 1) % total
                    else:
                        self._cursor = (index + 1) % total
                return account
        return None

    def representative(self, realm=None):
        with self._lock:
            candidates = [a for a in self.accounts if not realm or a.realm == realm]
            for account in candidates:
                if account.access_token: return account
            return candidates[0] if candidates else None

    def find_unique(self, ref):
        """Resolve a pinned-route ref to exactly one account.

        Order: exact uid, unique nickname, unique uid prefix. A tier with more
        than one match is ambiguous; the caller must report it, never guess.
        Returns (account, reason) where reason is "" on success or a short
        message fragment such as "was not found" / "is ambiguous".
        """
        ref = str(ref or "").strip()
        if not ref:
            return None, "was not found"
        with self._lock:
            accounts = list(self.accounts)
        exact = [a for a in accounts if str(a.uid or "") == ref]
        if len(exact) == 1:
            return exact[0], ""
        if len(exact) > 1:
            return None, "is ambiguous"
        named = [a for a in accounts if str(a.nickname or "").strip() == ref]
        if len(named) == 1:
            return named[0], ""
        if len(named) > 1:
            return None, "is ambiguous"
        prefixed = [a for a in accounts if str(a.uid or "").startswith(ref)]
        if len(prefixed) == 1:
            return prefixed[0], ""
        if len(prefixed) > 1:
            return None, "is ambiguous"
        return None, "was not found"

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
            try: account.checkin(trigger="account_add")
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
            try: account.checkin(trigger="account_import")
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
                    item["error"] = ("凭据已加密：先点「回收密钥」，再回来导入"
                                     if wb_atrest.hunt_supported()
                                     else "凭据已加密：" + wb_atrest.hunt_hint())
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
VOLATILE_FIELDS = ("cooldownUntil", "lastError", "credits", "lastCheckin",
                   "lastDailyChat", "lastActivityReport", "modelCaps", "tier")

# The subset of VOLATILE_FIELDS that must not round-trip through the local
# credential file at all: they are not trusted on load and not written by save(),
# exactly like model_cooldowns. An export still carries them (see to_dict()).
RUNTIME_ONLY_FIELDS = ("lastError", "cooldownUntil")


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
            or blob.get("access_token")
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
    # cockpit tools exports a bare array of snake_case OAuth rows. Map it onto
    # the flat shape the rest of this function already understands; expires_at
    # is milliseconds, which normalize_epoch() below converts to seconds.
    if (isinstance(row, dict) and "access_token" in row
            and "accessToken" not in row):
        row = {
            "uid": row.get("uid"),
            "nickname": row.get("nickname") or row.get("email") or "",
            "domain": row.get("domain"),
            "accessToken": row.get("access_token"),
            "refreshToken": row.get("refresh_token"),
            "expiresAt": row.get("expires_at"),
            "source": "cockpit",
        }
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
    api_key = is_api_key_token(token)
    # 静态 API Key 不是 JWT，不能按 JWT 校验；其余形态仍要求三段式，
    # 那是「这串东西确实是 OAuth token」唯一可靠的本地判据。
    if not api_key and token.count(".") != 2:
        raise ValueError("accessToken is not a JWT")

    # Where the account belongs. A realm forced by the caller still wins (that
    # is what the API option is for), otherwise the token/domain decides: a row
    # carrying a stale "realm" (an account once filed under the wrong region,
    # then exported again) must not keep dragging itself back there, and the
    # wrong region sends every request to the wrong upstream.
    forced = str(realm or "").strip().lower()
    if api_key:
        # 静态 Key 只在国内版出口有效。强制成 cn，忽略调用方的 realm 参数与
        # 行里的旧值——把这类账号放进国际版列表，每个请求都会打空。
        forced = "cn"
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
    if not raw_uid and api_key:
        # 静态 Key 从自己的 <id> 段派生 uid：ck_<id>.<secret> → apikey-<id>。
        # 与 Go 版面板同一口径，两边导出的文件可以互认。
        raw_uid = "apikey-" + api_key_id(token)
    uid = re.sub(r"[^A-Za-z0-9_-]", "_", raw_uid).strip("_ ")
    if not uid:
        raise ValueError("cannot determine uid (no uid field and no sub claim)")

    tasks_enabled = pick("tasksEnabled")
    return {
        "uid": uid,
        "nickname": str(pick("nickname") or ""),
        "domain": domain_for_realm(detected, pick("domain")),
        "realm": detected,
        "platform": str(pick("platform") or "CLI"),
        "enterpriseId": str(pick("enterpriseId") or ""),
        "accessToken": token,
        "refreshToken": str(pick("refreshToken") or ""),
        "expiresAt": (0 if api_key
                      else normalize_epoch(pick("expiresAt")) or jwt_exp(token)),
        "source": str(pick("source") or (SOURCE_API_KEY if api_key else "import")),
        "enabled": True,
        # Imported accounts start with tasks on (they are new to this pool);
        # an export that carries the flag keeps the operator's choice.
        "tasksEnabled": (bool(tasks_enabled)
                         if tasks_enabled is not None else True),
        # Volatile state is intentionally reset - see VOLATILE_FIELDS.
        "lastError": "",
        "cooldownUntil": 0.0,
    }
