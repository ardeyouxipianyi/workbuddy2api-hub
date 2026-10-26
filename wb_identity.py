# -*- coding: utf-8 -*-
"""wb_identity.py - 出站身分 (WorkBuddy 桌面端 / 官方 VSCode 插件 / 官方 CodeBuddy CLI)

支援三套出站身分:
  1. WorkBuddy 独立桌面客户端 (workbuddy / desktop / wb):
     - X-IDE-Type / X-IDE-Name: WorkBuddy
     - X-Product: SaaS
     - X-Agent-Intent: craft、X-Agent-Purpose: conversation、X-Agent-Type: main
     - 国际版端点: www.workbuddy.ai
       实测 5.8.4（2026-10-16 mitmproxy 抓包，账号 uid 已脱敏）:
         X-IDE-Version: 5.8.4
         User-Agent: WorkBuddy/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0
     - 国内版端点: copilot.tencent.com（无对应抓包，维持 5.5.6 与原 UA）

  2. 官方 VSCode 扩展 (vscode / vsc):
     - X-IDE-Type / X-IDE-Name: VSCode
     - X-Product: SaaS
     - UA: VSCode/<宿主版本> CodeBuddy/<扩展版本>
     - 国际版端点: www.workbuddy.ai，国内版端点: www.workbuddy.cn
     - 版本与头族取自官方扩展 4.12.39997873（Open VSX
       `Tencent-Cloud.coding-copilot` 解包源码）：product.json 里
       productName=CodeBuddy、platform=VSCode、deploymentType=SaaS、
       productVersion=4.12.39997873；UA 由
       UserAgentHttpInterceptor.buildUserAgent 拼成
       `<platform>/<platformVersion> <productName>/<productVersion>`，
       platformVersion 是宿主的 VSCode 版本（扩展从 vscode.env 取），
       hub 没有宿主，默认 1.119.0，可用 WB_VSCODE_IDE_VERSION 覆盖。

  3. 官方 CodeBuddy CLI (cli):
     - X-IDE-Type / X-IDE-Name: CLI、X-IDE-Version: 2.168.2
     - X-Product: SaaS
     - X-Agent-Intent: craft、X-Agent-Purpose: conversation、X-Agent-Type: main、
       X-Agent-Runtime: other
     - UA: CLI/2.168.2 CodeBuddy/2.168.2（官方 buildUserAgent：platform 段 +
       productName 段；adapter 先删掉 SDK 的 user-agent，CLI 也没有
       userAgentExtension，所以没有第三段）
     - OpenAI Node SDK 6.25.0 的 x-stainless-* 指纹头（见
       cli_stainless_headers()；OS/arch/node 版本默认取宿主，可用
       WB_IDENTITY_OS / WB_IDENTITY_ARCH / WB_IDENTITY_NODE_VERSION 覆盖）
     - X-Private-Data：个人号 false、企业号 true（官方 getCustomHeaders 默认值）
     - chat 请求体：JSON.stringify 紧凑形态 + gzip（productFeatures.RequestBodyGzip）
       + cache_retention=long；不带 metadata / conversation_id /
       previous_response_id；X-Root-Request-ID 主对话会发，值 = 本轮
       X-Conversation-Request-ID
     - 国际版端点: www.workbuddy.ai（官方 product.json 是 www.codebuddy.ai；本网关
       因解析不了 codebuddy.ai 走 workbuddy.ai，属已知 endpoint 差异），国内版
       端点: copilot.tencent.com
     取值来源：官方 npm 包 @tencent-ai/codebuddy-code 2.168.2 解包核对
     （product.json 的产品常量、dist 里的 header 常量 / buildUserAgent /
     请求头组装 / 请求体处理器）。

三套身分都带官方客户端的会话头族（X-Conversation-ID / -Request-ID /
-Message-ID / -Root-Request-ID）：缺了它会触发上游的渠道校验，回 400 /
code 11-128（`conversation id missing`）。ID 形态按 5.8.4 实测：会话 id 是带
连字符的 UUID，请求/消息 id 是 32 位无连字符 hex（UUIDv7），见 uuid7。
"""

import gzip
import hashlib
import io
import os
import platform
import secrets
import threading
import time
import uuid

# 5.8.4 实测（国际版桌面客户端抓包）: 对话请求的版本号与 UA。
# UA 末段 CLI/2.161.0 是客户端内置 CLI 的版本，不是桌面端自己的版本。
DESKTOP_VERSION_INTL = "5.8.4"
# 对话请求由客户端内置 CLI 子进程发出，该进程带 CLIENT_INFO_IDE_TYPE=WorkBuddy
# （sidecar-manager.js 硬编码），buildUserAgent 里 platform 段取它、application
# 段取 product.json 的 applicationName（国际版为 workbuddy-ai），所以首段是大写
# WorkBuddy、次段是小写 workbuddy-ai。
# 注意 Electron 主进程自己发的旁路请求（msg-center / conversations）UA 是
# workbuddy-ai/... workbuddy-ai/...，两者不是同一条链路，别照抄错。
DESKTOP_UA_INTL = "WorkBuddy/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0"
# 国内版桌面端：2026-10-18 两区桌面端实跑（Go 版抓包记录）为 5.8.3、
# 内置 CLI 2.161.0；第二段按国内版是 WorkBuddy（国际版是 workbuddy-ai）。
DESKTOP_VERSION_CN = "5.8.3"
DESKTOP_UA_CN = "WorkBuddy/5.8.3 WorkBuddy/5.8.3 CLI/2.161.0"

#: 官方 CodeBuddy CLI 的版本号：npm 包 @tencent-ai/codebuddy-code 的 package
#: version。product.json 没有覆盖 platformVersion / productVersion，所以
#: buildUserAgent 的两段（CLI / CodeBuddy）都用它。2.168.2 为官方包解包核对值。
DEFAULT_IDE_VERSION = "2.168.2"
#: 官方 CLI 的 UA 公式（dist 里的 buildUserAgent）：
#:   <platform>/<platformVersion> + <applicationName|productName>/<productVersion>
#:   + 可选 userAgentExtension
#: CLI 的 product.json：platform=CLI、productName=CodeBuddy、applicationName
#: 为空；CLI 没有 CLIENT_INFO_USER_AGENT_EXTENSION，adapter 又会先删掉 SDK
#: 的 `user-agent` 头（SDK 的 Headers 实例键已小写化），所以 buildUserAgent
#: 的入参是空串，最终只有两段。
#: 官方 CLI 的 chat 请求由 OpenAI Node SDK 6.25.0 发出，SDK 自带的一组
#: X-Stainless-* 指纹头。值取自 SDK 6.25.0 的默认值；OS / Arch / Runtime-Version
#: 本来由 SDK 按宿主机生成，这里默认取网关宿主（与 TLS/网络栈一致），可用
#: WB_IDENTITY_OS / WB_IDENTITY_ARCH / WB_IDENTITY_NODE_VERSION 覆盖。
#: 头名按官方 wire 小写（SDK 的 Headers 迭代时键已小写化）。
def _identity_os():
    override = str(os.environ.get("WB_IDENTITY_OS") or "").strip()
    if override:
        return override
    system = platform.system()
    return {"Windows": "Windows", "Darwin": "MacOS", "Linux": "Linux"}.get(
        system, system or "Linux")


def _identity_arch():
    override = str(os.environ.get("WB_IDENTITY_ARCH") or "").strip()
    if override:
        return override
    machine = (platform.machine() or "").lower()
    return {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64",
            "arm64": "arm64", "i386": "ia32", "i686": "ia32"}.get(
        machine, machine or "x64")


def desktop_chat_compat_enabled():
    """Opt in to desktop chat headers without changing billing or other products."""
    return os.environ.get("WB_DESKTOP_CHAT_COMPAT", "").strip() == "1"


def cli_stainless_headers():
    """官方 OpenAI Node SDK 6.25.0 的 x-stainless-* 指纹头。"""
    return {
        "x-stainless-lang": "js",
        "x-stainless-package-version": sdk_version(),
        "x-stainless-os": _identity_os(),
        "x-stainless-arch": _identity_arch(),
        "x-stainless-runtime": "node",
        "x-stainless-runtime-version": (
            str(_VERSION_OVERRIDES.get("node") or "").strip()
            or str(os.environ.get("WB_IDENTITY_NODE_VERSION") or "").strip()
            or node_version()),
        "x-stainless-retry-count": "0",
        "x-stainless-timeout": "1200",
    }
#: X-Agent-Runtime：官方枚举 AGENT_RUNTIMES = local-agent / cloud-agent /
#: agent-agent；解析函数对未知/缺省值回 "other"（默认 other，可由 env 或
#: session meta 覆盖）。主对话没有覆盖时就是 other。
CLI_AGENT_RUNTIME = "other"
#: 官方 product.json 的 productFeatures.RequestBodyGzip = true → 官方 CLI 的
#: chat 请求体默认 gzip 压缩，带这个请求头。JSON 仍是 JSON.stringify 的紧凑形态。
REQUEST_CONTENT_ENCODING = "gzip"
#: 官方 CLI 在 chat 请求体里注入的字段：模型请求处理器看到 body 没有
#: cache_retention 就补上（resolveCacheRetention；默认 "long"，只有设了
#: CODEBUDDY_DISABLE_PROMPT_CACHING_1H 才 "short"）。
CLI_CACHE_RETENTION = "long"
#: 官方 CLI 的 chat 请求体不带这几个字段（Responses API 的字段 / 客户端自定义
#: metadata）。网关会把客户端 payload 原样带出去，CLI 身份下要剔掉。
CLI_BODY_DROP_FIELDS = ("metadata", "conversation_id", "previous_response_id")

# 官方 VSCode 扩展 4.12.39997873（Open VSX `Tencent-Cloud.coding-copilot`，
# 解包源码的 product.json）：productName=CodeBuddy、platform=VSCode、
# deploymentType=SaaS、productVersion=4.12.39997873。
# UA 由 UserAgentHttpInterceptor.buildUserAgent 拼成
# `<platform>/<platformVersion> <productName>/<productVersion>`；其中
# platformVersion 是宿主的 VSCode 版本（扩展从 vscode.env 取），hub 没有宿主，
# 默认 1.119.0，可用 WB_VSCODE_IDE_VERSION 覆盖。
VSCODE_PRODUCT_VERSION = "4.12.39997873"
VSCODE_IDE_VERSION = "1.119.0"
VSCODE_PLATFORM = "VSCode"
VSCODE_PRODUCT_NAME = "CodeBuddy"


def vscode_ide_version():
    """宿主 VSCode 版本（官方取 vscode.env；hub 没有宿主，默认 1.119.0）。

    可用 WB_VSCODE_IDE_VERSION 覆盖，跟你的实际宿主 IDE 版本对齐。
    """
    return (str(_VERSION_OVERRIDES.get("vscode_ide") or "").strip()
            or str(os.environ.get("WB_VSCODE_IDE_VERSION") or "").strip()
            or VSCODE_IDE_VERSION)


def vscode_ua():
    return "%s/%s %s/%s" % (VSCODE_PLATFORM, vscode_ide_version(),
                            VSCODE_PRODUCT_NAME, vscode_product_version())
# 官方 HttpEnvRouteInterceptor：developerMode 关闭时固定 production。
VSCODE_ENV_ID = "production"

# 旧名字（VSCode 分支最初把宿主版本/扩展版本叫成 WorkBuddy_*）保留成别名，
# 免得外部脚本按老名字取值时断掉。
DEFAULT_WORKBUDDY_IDE_VERSION = VSCODE_IDE_VERSION
DEFAULT_WORKBUDDY_PRODUCT_VERSION = VSCODE_PRODUCT_VERSION

#: 默认版本号：抓包 / 官方包解包核对过的值。面板上「出站身份版本号」列出的
#: 就是这一组；只有这组值有对应的头族与请求体配套。
_VERSION_DEFAULTS = {
    "desktop_intl": DESKTOP_VERSION_INTL,
    "desktop_cn": DESKTOP_VERSION_CN,
    "desktop_cli": "2.161.0",
    "vscode_product": VSCODE_PRODUCT_VERSION,
    "vscode_ide": VSCODE_IDE_VERSION,
    "cli": DEFAULT_IDE_VERSION,
    "sdk": "6.25.0",
    "node": "v22.21.1",
}
#: 面板改过的版本号（settings.json 的 identity_versions）。只改发出去的字符串，
#: 不改头族、请求体与 ID 形态——那些是每个版本单独核对过的。
_VERSION_OVERRIDES = {}


def default_versions():
    """A copy of the verified default version numbers."""
    return dict(_VERSION_DEFAULTS)


def configure_versions(overrides):
    """Apply panel-provided version overrides (empty/unknown keys are ignored)."""
    global _VERSION_OVERRIDES
    clean = {}
    for key in _VERSION_DEFAULTS:
        value = str((overrides or {}).get(key) or "").strip()
        if value:
            clean[key] = value
    _VERSION_OVERRIDES = clean
    return dict(clean)


def _version(key):
    return str(_VERSION_OVERRIDES.get(key) or _VERSION_DEFAULTS[key])


def desktop_version(realm):
    return _version("desktop_cn" if realm == "cn" else "desktop_intl")


def desktop_ua(realm):
    app = desktop_version(realm)
    cli = _version("desktop_cli")
    product = "WorkBuddy" if realm == "cn" else "workbuddy-ai"
    return "WorkBuddy/%s %s/%s CLI/%s" % (app, product, app, cli)


def vscode_product_version():
    return _version("vscode_product")


def cli_version():
    return _version("cli")


def sdk_version():
    return _version("sdk")


def node_version():
    return _version("node")

PRODUCT_DESKTOP = "workbuddy"
PRODUCT_VSCODE = "vscode"
PRODUCT_CLI = "cli"
VALID_PRODUCTS = (PRODUCT_DESKTOP, PRODUCT_VSCODE, PRODUCT_CLI)


def normalize_product(value):
    """把各种写法收敛成 'workbuddy'、'vscode' 或 'cli'。"""
    v = str(value or "").strip().lower()
    if v in ("workbuddy", "wb", "desktop"):
        return PRODUCT_DESKTOP
    if v in ("vscode", "vsc", "ide"):
        return PRODUCT_VSCODE
    if v in ("cli", "codebuddy"):
        return PRODUCT_CLI
    return PRODUCT_DESKTOP


#: 国内版对话侧上游的默认 base。
CN_CHAT_BASE_DEFAULT = "https://copilot.tencent.com"


def _cn_chat_base_from_env():
    """读环境变量 WB_CN_CHAT_UPSTREAM（只在导入时读一次，改了要重启）。

    返回 (生效的 base, 被忽略的原始值)。没设或设成空串 = 默认地址，老部署行为不变；
    不是 http(s) 地址时忽略并回落默认，由启动日志提示（见 wb_proxy main）。

    为什么需要：copilot.tencent.com 从海外出口（如代理软件的全局模式）连接时 TLS 握手
    会被复位，国内版的对话、刷新 token、模型目录全部失败；www.workbuddy.cn 提供同一套
    接口且海外可达（官方桌面端当前即以它为 endpoint，VSCode 身分本来也走它）。
    """
    raw = (os.environ.get("WB_CN_CHAT_UPSTREAM") or "").strip()
    if not raw:
        return CN_CHAT_BASE_DEFAULT, ""
    base = raw.rstrip("/")
    scheme, sep, host = base.partition("://")
    if scheme not in ("http", "https") or not sep or not host:
        return CN_CHAT_BASE_DEFAULT, raw
    return base, ""


#: 国内版对话侧上游的生效 base；wb_accounts.REALM_CONFIGS["cn"] 与 wb_tasks.CHAT_BASE 都取它。
CN_CHAT_BASE, CN_CHAT_BASE_IGNORED = _cn_chat_base_from_env()

# 覆盖国内版 base 时 X-Domain（第二项）不跟着改：请求头优先用账号登录时记下的 domain，
# 这里只是兜底；换的只是"往哪发"。
_ENDPOINTS = {
    ("intl", PRODUCT_DESKTOP): ("https://www.workbuddy.ai", "www.workbuddy.ai"),
    ("intl", PRODUCT_VSCODE): ("https://www.workbuddy.ai", "www.workbuddy.ai"),
    # www.codebuddy.ai does not resolve on the networks this was measured on
    # (getaddrinfo fails; the system resolver hands back the 0.0.0.1 sinkhole),
    # so the international CLI identity rides the workbuddy.ai edge, which
    # accepts CLI headers and answers normally.
    ("intl", PRODUCT_CLI): ("https://www.workbuddy.ai", "www.workbuddy.ai"),
    ("cn", PRODUCT_DESKTOP): (CN_CHAT_BASE, "copilot.tencent.com"),
    ("cn", PRODUCT_VSCODE): ("https://www.workbuddy.cn", "www.workbuddy.cn"),
    ("cn", PRODUCT_CLI): (CN_CHAT_BASE, "copilot.tencent.com"),
}


def endpoint_for(realm, product):
    """回传 (chat base URL, X-Domain)。身分换了，端点也要跟着换。"""
    key = ("cn" if realm == "cn" else "intl", normalize_product(product))
    return _ENDPOINTS[key]


def domain_for_realm(realm):
    """X-Domain 的值（预设桌面端）。"""
    return endpoint_for(realm, PRODUCT_DESKTOP)[1]


def uuid7():
    """RFC 9562 UUIDv7：48 位毫秒时间戳打头、后面是随机位，时间有序。

    5.8.4 客户端的请求/消息 id 就是这个形态（去连字符即 32hex）。上游不校验
    版本号：抓包里 main 对话的 X-Conversation-ID 实际是 v4、辅助请求是 v7，
    两种都被正常接受（200），所以形态只求贴近，不承担语义。
    """
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    return uuid.UUID(int=(ms << 80) | (0x7 << 76) | (rand_a << 64)
                     | (0b10 << 62) | rand_b)


def new_conversation_id():
    """X-Conversation-ID：带连字符的 UUID（5.8.4 实测形态）。"""
    return str(uuid7())


def conversation_id_for(uid, session_key):
    """按「账号 + 会话键」稳定派生一个会话 id（带连字符的 UUID）。

    官方客户端在同一个会话里始终复用同一个 X-Conversation-ID（抓包里主对话
    是 UUIDv4、辅助请求是 v7，两种都被接受）；网关以前每个请求都现生成一个，
    等于把一次多轮对话拆成上千个「一次性会话」，与人类客户端的画像不符。
    这里按账号 + 会话键做稳定派生：同一账号的同一会话多轮复用同一个 id，
    不同账号 / 不同会话互不相同——既贴近官方语义，又不会成为跨账号关联键
    （把同一段会话的 id 发给多个账号才是关联键）。

    没有会话键（客户端没带）时返回空串，调用方保持原来的「每请求新生成」。
    """
    key = str(session_key or "").strip()
    if not key:
        return ""
    digest = hashlib.sha256(("%s|%s" % (uid or "", key)).encode("utf-8")).hexdigest()
    h = list(digest[:32])
    h[12] = "4"          # UUID 版本位
    h[16] = "a"          # RFC 4122 变体位
    h = "".join(h)
    return "%s-%s-%s-%s-%s" % (h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])


def new_message_id():
    """X-Request-ID / X-Conversation-Message-ID / -Request-ID / -Root-Request-ID：
    32 位无连字符 hex（5.8.4 实测形态）。"""
    return uuid7().hex


def new_turn_request_id():
    """一轮 user send 的 X-Conversation-Request-ID。

    官方 CLI 的语义（getEffectiveConversationRequestId）：这个 id 在「一次 user
    send」开始时生成，这一轮里所有请求（含重试）复用它；只有 message id
    （X-Request-ID = X-Conversation-Message-ID）是每请求新生成的。所以它必须由
    调用方在重试循环外生成一次再传进 build_identity_headers（见
    wb_proxy.open_upstream），不能放在身份构造里每请求现生成。
    """
    return new_message_id()


#: 官方 CLI 的 X-Conversation-ID 就是会话的 session.id：同一个会话里恒定，换
#: 会话换新值（进程重启换新也符合「每进程一个会话」的官方语义）。网关按
#: (账号 uid, 会话键) 映射成同一个 id，进程内缓存复用——绝不能每请求现生成，
#: 那会把每次请求都变成一次「一次性会话」，是反作弊眼里最明显的非人类形态。
def gzip_body(data):
    """把已经序列化好的 chat 请求体 gzip 掉（官方 RequestBodyGzip 行为）。

    productFeatures.RequestBodyGzip = true → 官方 CLI 的 chat 请求体默认 gzip，
    并带 Content-Encoding: gzip（见 REQUEST_CONTENT_ENCODING）。gzip 头里的
    mtime 固定成 0，同一请求体每次压出同样的字节（便于对拍/测试，不影响解码）。
    请求体的 JSON 序列化本身不在本函数里：紧凑形态由传输层统一处理。
    """
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as fh:
        fh.write(data)
    return buf.getvalue()


def trace_headers(with_parent=False):
    """对话请求带的链路头族（traceparent / b3 / X-B3-* / X-Trace-ID）。

    桌面端 5.8.4 抓包里 main 请求还带 X-B3-ParentSpanId（模型调用是会话根
    span 的子 span），辅助请求不带；网关没有真实父子关系，桌面端维持原样
    （with_parent=False）。官方 CLI 的对话请求是子 span，按官方结构补齐父
    span 与 b3 第四段：with_parent=True。
    """
    trace = new_message_id()
    span = secrets.token_hex(8)
    headers = {
        "X-Trace-ID": trace,
        "traceparent": "00-%s-%s-01" % (trace, span),
        "b3": "%s-%s-1" % (trace, span),
        "X-B3-TraceId": trace,
        "X-B3-SpanId": span,
        "X-B3-Sampled": "1",
    }
    if with_parent:
        parent = secrets.token_hex(8)
        headers["X-B3-ParentSpanId"] = parent
        headers["b3"] = "%s-%s-1-%s" % (trace, span, parent)
    return headers


def new_request_trace_id():
    """`X-Request-Trace-Id`：官方 CustomHeadersHttpProxyInterceptor 每个请求新生成
    一个 `generateUUUID()`（模块 338803：优先 `crypto.randomUUID()`，返回**带连字符**
    的 UUID），直接写进头里、没有去连字符。这里保持同一形态。
    """
    return str(uuid.uuid4())


def vscode_trace_headers():
    """官方 VSCode 扩展 4.12.39997873 的链路头族（B3，无 traceparent）。

    官方 model-provider 的 trace span 头（`OpenTelemetrySpan.requestHeaders`）：
    `X-Trace-ID` = `X-B3-TraceId` = traceId；`X-B3-SpanId` 是每请求随机的
    16 hex；`X-B3-ParentSpanId` 来自父 span；`b3` 是
    `<traceId>-<spanId>-1-<parentSpanId>`（四段）；`X-B3-Sampled` 固定 1。
    官方自身代码里没有 `traceparent`（只有第三方埋点库会发），所以这里不加。
    网关没有真实的父子 span：合成一个父 span id，既保证 b3 恒为四段，又保证
    b3 尾段与 `X-B3-ParentSpanId` 同源。
    """
    trace = new_message_id()
    span = secrets.token_hex(8)
    parent = secrets.token_hex(8)
    return {
        "X-Trace-ID": trace,
        "b3": "%s-%s-1-%s" % (trace, span, parent),
        "X-B3-TraceId": trace,
        "X-B3-ParentSpanId": parent,
        "X-B3-SpanId": span,
        "X-B3-Sampled": "1",
    }


def build_identity_headers(product, realm, uid, token,
                           conversation_id=None, enterprise_id="",
                           tenant_id="", department="",
                           model=None, session_key=None, turn_request_id=None,
                           private_data=None):
    """回传这一轮要用的身分标头。product = 'workbuddy' (桌面端), 'vscode' 或 'cli'。

    conversation_id 由调用方显式指定时优先使用；留空时按身分处理：
      - CLI 身份按 (uid, session_key) 稳定派生（conversation_id_for），官方
        客户端同一会话复用同一个 id；
      - 桌面端 / VSCode 每次请求新生成（桌面端 UUIDv7，VSCode 是 32 位无连
        字符 hex，官方 BaseAgent 的 `uuidv4().replace(/-/g, "")`）。
    上游不拿这个 id 存历史——抓包后的实测里，同一 id 连发两轮、第二轮只带
    system+user，模型仍答不出上一轮；换个新 id 结果一样。所以稳定与否不影响
    模型行为，只影响出站请求「像不像官方客户端」。

    CLI 身份按官方语义处理（session_key / turn_request_id 只对它生效，桌面端 /
    VSCode 分支不受影响）：
      - X-Conversation-ID 按 (uid, session_key) 稳定，见 conversation_id_for；
      - X-Conversation-Request-ID / X-Root-Request-ID 取调用方给的一轮 request
        id（open_upstream 在重试循环外生成一次），整轮所有请求/重试复用；
      - X-Request-ID = X-Conversation-Message-ID，每请求新生成。

    model 只在 VSCode 身分上生效：官方扩展的 model-provider 在 model 请求上带
    `X-Model-ID`（值就是本次请求的 model）与两个 monitor 时间戳。桌面端 / CLI
    分支不受影响。
    """
    product = normalize_product(product)
    is_cn = (realm == "cn")

    explicit_conv = str(conversation_id or "").strip()
    conv = explicit_conv or (new_message_id() if product == PRODUCT_VSCODE
                             else new_conversation_id())
    msg_id = new_message_id()
    # main 请求里 X-Conversation-Request-ID 与 X-Root-Request-ID 是同一个值，
    # 且早于消息 id —— 一次 user send 开始时生成、该轮所有请求复用。
    conv_req_id = new_message_id()
    if product == PRODUCT_CLI:
        # 官方 CLI 的两处会话标识（见 docstring）。桌面端 / VSCode 不吃这两个
        # 参数，保持它们原有的「每请求现生成」行为。
        if not str(conversation_id or "").strip():
            derived = conversation_id_for(uid, session_key)
            if derived:
                conv = derived
        conv_req_id = str(turn_request_id or "").strip() or new_message_id()

    if product == PRODUCT_DESKTOP:
        client_ver = desktop_version(realm)
        chat_ua = desktop_ua(realm)
        headers = {
            "X-Agent-Intent": "craft",
            "X-Agent-Purpose": "conversation",
            "X-Agent-Type": "main",
            "X-IDE-Name": "WorkBuddy",
            "X-IDE-Type": "WorkBuddy",
            "X-IDE-Version": client_ver,
            "X-Private-Data": "true",
            "X-Product": "SaaS",
            "X-Domain": endpoint_for(realm, product)[1],
            "User-Agent": chat_ua,
            "X-User-Id": str(uid or "anonymous"),
            "Authorization": "Bearer " + str(token or ""),
            "X-Conversation-ID": conv,
            "X-Conversation-Request-ID": conv_req_id,
            "X-Root-Request-ID": conv_req_id,
            "X-Conversation-Message-ID": msg_id,
            "X-Request-ID": msg_id,
        }
        compat = desktop_chat_compat_enabled()
        headers.update(trace_headers(with_parent=compat))
        if compat:
            # Desktop profile reported by workbuddy2api-panel's headers.go
            # (CN 5.8.3 / intl 5.8.4); shared SDK values remain configurable.
            # Not a claim that Python's TLS stack matches the official client.
            sdk_headers = cli_stainless_headers()
            # Desktop request timeout is not verified; do not copy CLI's budget.
            sdk_headers.pop("x-stainless-timeout", None)
            headers.update(sdk_headers)
    elif product == PRODUCT_VSCODE:
        # 官方 VSCode 扩展 4.12.39997873 的 chat 请求头（解包源码实锤）：
        #   - 全局拦截器：UA / X-IDE-* / X-Product-Version（UserAgent +
        #     CustomHeadersHttpProxyInterceptor）、X-Env-ID（HttpEnvRoute）、
        #     X-Request-Trace-Id（CustomHeadersHttpProxyInterceptor，每请求一个
        #     带连字符的 generateUUUID()）；
        #   - model-provider（buildModelRequestHeaders）：X-Agent-Intent、
        #     X-Conversation-* 家族、X-Model-ID；agentType 在主对话是 main；
        #   - CraftInvokableAgent 拦截器：X-Private-Data（仅非国内站）；
        #   - trace span：X-Trace-ID / b3 / X-B3-*（见 vscode_trace_headers）。
        # id 关系按官方：X-Request-ID 与 X-Conversation-Request-ID 是同一个本轮
        # requestId（BaseAgent 的 requestId = uuidv4 去连字符，32hex）；
        # X-Conversation-ID 是 32hex；X-Conversation-Message-ID 是独立的随机值。
        # 主对话（primary agent）不发 X-Root-Request-ID / X-Parent-Conversation-ID
        # ——官方只在 subagent 上带 rootRequestId。
        headers = {
            "X-Agent-Intent": "craft",
            "X-Agent-Type": "main",
            "X-IDE-Type": VSCODE_PLATFORM,
            "X-IDE-Name": VSCODE_PLATFORM,
            "X-IDE-Version": vscode_ide_version(),
            "X-Product-Version": vscode_product_version(),
            "X-Env-ID": VSCODE_ENV_ID,
            # 官方主 chat 代码路径没有 X-Product（只有 codewiki 路径有）。本 PR
            # 保留现状，避免误删；「主 chat 是否发 X-Product」待 5.8.x 抓包确认。
            "X-Product": "SaaS",
            "X-Domain": endpoint_for(realm, product)[1],
            "User-Agent": vscode_ua(),
            "X-User-Id": str(uid or "anonymous"),
            "Authorization": "Bearer " + str(token or ""),
            "X-Request-Trace-Id": new_request_trace_id(),
            "X-Conversation-ID": conv,
            "X-Conversation-Request-ID": conv_req_id,
            "X-Conversation-Message-ID": msg_id,
            "X-Request-ID": conv_req_id,
        }
        if model:
            now_ms = str(int(time.time() * 1000))
            headers["X-Model-ID"] = str(model)
            # 官方 BaseAgent 在 _preRequest / _executeSingleStep 里把这两个毫秒
            # 时间戳塞进 customHeaders（axios 会把数字转成字符串）；只出现在
            # model 请求上，非 model 端点不发。
            headers["monitor_promptPrepareStartTime"] = now_ms
            headers["monitor_httpSendTime"] = now_ms
        # 官方按 endpoint 判断：只有国内站（copilot.tencent.com）不发
        # X-Private-Data；国际站默认 true（enableModelOptimization 打开时才 false，
        # 网关没有这个开关，取默认 true）。
        if not is_cn:
            headers["X-Private-Data"] = "true"
        headers.update(vscode_trace_headers())
        # X-Device-Token：官方由本机 QIMEI/TuringShield native SDK 生成真实设备
        # token（仅非企业号），且 token 或 error 必发其一；hub 没有本机 SDK，
        # 生成不了也不伪造，由 Account.headers 按官方失败态发
        # X-Device-Token-Error: sdk_not_initialized。运维配置的真实 token 仍优先。
        #
        # gzip：官方主 chat 默认压缩（RequestBodyGzip=true → executeByMode 置
        # enableRequestBodyGzip → createCustomFetch 压缩），由 wb_proxy 统一做。
    else:  # PRODUCT_CLI
        ide_version = cli_version()
        headers = {
            "X-Agent-Intent": "craft",
            "X-Agent-Purpose": "conversation",
            "X-Agent-Runtime": CLI_AGENT_RUNTIME,
            "X-Agent-Type": "main",
            "X-IDE-Type": "CLI",
            "X-IDE-Name": "CLI",
            "X-IDE-Version": ide_version,
            "X-Product": "SaaS",
            "X-Domain": endpoint_for(realm, product)[1],
            "User-Agent": "CLI/%s CodeBuddy/%s" % (ide_version, ide_version),
            "X-User-Id": str(uid or "anonymous"),
            "Authorization": "Bearer " + str(token or ""),
            "X-Conversation-ID": conv,
            "X-Conversation-Request-ID": conv_req_id,
            "X-Root-Request-ID": conv_req_id,
            "X-Conversation-Message-ID": msg_id,
            "X-Request-ID": msg_id,
        }
        # X-Private-Data：官方 getCustomHeaders 先按模型优化开关写 true/false
        # （个人号默认 false、企业号 true），随后策略只在自定义模型/skip 时删头、
        # sticky 会话强制 true。这里按账号是否企业号取对应的默认值。
        headers["X-Private-Data"] = "true" if enterprise_id else "false"
        headers.update(cli_stainless_headers())
        headers.update(trace_headers(with_parent=True))

    # 静态 API Key（ck_/pt_）：官方 CLI 两个头都发（Authorization 与 X-API-Key
    # 任一均可鉴权）。只发 Authorization 时，/v3/config 的模型清单只下发 54 条
    # 而不是 56 条——差的 4 个（glm-5.2-aicc / glm-5.3-flashx / kimi-k3-2 /
    # minimax-m3-pay）会静默消失，所以两头都发。
    if str(token or "").startswith(("ck_", "pt_")):
        headers["X-API-Key"] = str(token)

    if enterprise_id:
        headers["X-Enterprise-Id"] = str(enterprise_id)
        headers["X-Tenant-Id"] = str(tenant_id or enterprise_id)
    if department:
        headers["X-Department-Info"] = str(department)

    # Issue #516: let the operator declare the data-usage intent instead of
    # always inheriting the per-identity default. "auto" (or None) changes
    # nothing; true/false is sent verbatim, including on VSCode CN where the
    # auto branch omits the header.
    if private_data in ("true", "false"):
        headers["X-Private-Data"] = private_data

    return headers
