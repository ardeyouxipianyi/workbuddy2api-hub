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
     - chat 请求由客户端内置 CLI 子进程经 OpenAI Node SDK 6.25.0 发出（见
       DESKTOP_STAINLESS_HEADERS），请求体 gzip（Content-Encoding: gzip），
       main 请求带 X-B3-ParentSpanId、b3 为四段；桌面 daemon 还会随请求上行
       一个本地 ACP 连接 id（acp-connection-id，会话级稳定，见
       acp_connection_id_for）。
     - 国内版端点: copilot.tencent.com
       实测 5.6.2（2026-09-29 官方 CLI 钩子全量抓包）: UA 中段仍是
       WorkBuddy、X-IDE-Version: 5.6.2、X-Device-Token 每次对话必带（真实
       Turing Shield token，网关无法生成；本模块只在配置了真实 token 时才
       发，见 wb_accounts）、body 带 agent:"cli" + conversationRequestId
       （见 desktop_body_fields）。

  2. 官方 VSCode 插件 (vscode / vsc):
     - X-IDE-Type / X-IDE-Name: VSCode
     - X-Product: SaaS
     - UA: VSCode/<ver> WorkBuddy/<ver>
     - 国际版端点: www.workbuddy.ai，国内版端点: www.workbuddy.cn
     （无对应抓包，取值维持原样）

  3. 官方 CodeBuddy CLI (cli):
     - X-IDE-Type / X-IDE-Name: CLI
     - X-Product: SaaS
     - UA: CLI/<ver> CodeBuddy/<ver>
     - 国际版端点: www.workbuddy.ai，国内版端点: copilot.tencent.com
     （版本号取 5.8.4 内置 CLI 的 2.161.0）

三套身分都带官方客户端的会话头族（X-Conversation-ID / -Request-ID /
-Message-ID / -Root-Request-ID）：缺了它会触发上游的渠道校验，回 400 /
code 11-128（`conversation id missing`）。ID 形态按 5.8.4 实测：会话 id 是带
连字符的 UUID，请求/消息 id 是 32 位无连字符 hex（UUIDv7），见 uuid7。
"""

import gzip
import hashlib
import io
import secrets
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
# 国内版：官方 5.6.2 的 CLI 钩子全量抓包（2026-09-29）是现有最完整的国内版
# 证据：X-IDE-Version 5.6.2、UA 中段仍是 WorkBuddy（不是 workbuddy-ai）、
# body 带 agent:"cli" + conversationRequestId、每次对话带真实 X-Device-Token。
# 5.8.2/5.8.3 只有 changelog、没有完整抓包，先钉 5.6.2，等抓包再升。
# UA 的版本段有两处静态实锤（同一份 5.6.2 客户端解包）：
#   - 前两段 5.6.2 = 安装清单 re/install-manifest.json 的 appVersion；
#   - 末段 CLI/2.147.0 = 内置 CLI 的 package.json publishConfig.customPackage.version。
# 整条 UA 仍无 5.6.2 抓包原值，但两段版本号不再是猜测。
DESKTOP_VERSION_CN = "5.6.2"
DESKTOP_UA_CN = "WorkBuddy/5.6.2 WorkBuddy/5.6.2 CLI/2.147.0"

# 国际版 chat 请求由内置 CLI 子进程的 OpenAI Node SDK 6.25.0 发出，SDK 自带
# 这组 x-stainless-* 指纹头（官方 5.2.7 原始报文实测；5.8.4 抓包未逐项记录
# 值，但同源：桌面 5.8.4 内置 Node v22.21.1 + openai-node 6.25.0）。
# 值口径：lang/package/runtime/retry-count 是 SDK 常量；arch/os 由 SDK 按宿主
# 机 process.arch/platform 生成——网关冒充的是一台 Windows x64 桌面机。
# 头名按官方 wire 原样小写（5.2.7 原始报文：x-stainless-arch 等）。
DESKTOP_STAINLESS_HEADERS = {
    "x-stainless-arch": "x64",
    "x-stainless-lang": "js",
    "x-stainless-os": "Windows",
    "x-stainless-package-version": "6.25.0",
    "x-stainless-retry-count": "0",
    "x-stainless-runtime": "node",
    "x-stainless-runtime-version": "v22.21.1",
}

# 官方桌面端 chat 请求体 gzip（5.2.7 原始报文 / 5.6.2 抓包 / 5.8.4 抓包都有
# Content-Encoding: gzip）；纯标准库 gzip，无第三方依赖。
REQUEST_CONTENT_ENCODING = "gzip"

DEFAULT_IDE_VERSION = "2.161.0"
DEFAULT_WORKBUDDY_IDE_VERSION = "1.119.0"
DEFAULT_WORKBUDDY_PRODUCT_VERSION = "4.9.29177644"

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


_ENDPOINTS = {
    ("intl", PRODUCT_DESKTOP): ("https://www.workbuddy.ai", "www.workbuddy.ai"),
    ("intl", PRODUCT_VSCODE): ("https://www.workbuddy.ai", "www.workbuddy.ai"),
    # www.codebuddy.ai does not resolve on the networks this was measured on
    # (getaddrinfo fails; the system resolver hands back the 0.0.0.1 sinkhole),
    # so the international CLI identity rides the workbuddy.ai edge, which
    # accepts CLI headers and answers normally.
    ("intl", PRODUCT_CLI): ("https://www.workbuddy.ai", "www.workbuddy.ai"),
    ("cn", PRODUCT_DESKTOP): ("https://copilot.tencent.com", "copilot.tencent.com"),
    ("cn", PRODUCT_VSCODE): ("https://www.workbuddy.cn", "www.workbuddy.cn"),
    ("cn", PRODUCT_CLI): ("https://copilot.tencent.com", "copilot.tencent.com"),
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


def new_message_id():
    """X-Request-ID / X-Conversation-Message-ID / -Request-ID / -Root-Request-ID：
    32 位无连字符 hex（5.8.4 实测形态）。"""
    return uuid7().hex


def new_turn_request_id():
    """一轮 user send 的 X-Conversation-Request-ID / X-Root-Request-ID。

    官方桌面端语义：这个 id 在「一次 user send」开始时生成，这一轮里所有
    请求（含重试/换号）复用它；只有 message id（X-Request-ID =
    X-Conversation-Message-ID）是每请求新生成的。所以它必须由调用方在重试
    循环外生成一次再传进 build_identity_headers（见 wb_proxy.open_upstream）。
    """
    return new_message_id()


def gzip_body(data):
    """把已经序列化好的 chat 请求体 gzip 掉（官方 RequestBodyGzip 行为）。

    官方桌面端 chat 请求体带 Content-Encoding: gzip（5.2.7 原始报文 / 5.6.2
    抓包 / 5.8.4 抓包均实证）。gzip 头里的 mtime 固定成 0，同一请求体每次压
    出同样的字节（便于对拍/测试，不影响解码）。JSON 序列化本身不在这里做。
    """
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as fh:
        fh.write(data)
    return buf.getvalue()


def acp_connection_id_for(uid, session_key=None):
    """acp-connection-id：桌面 daemon 与内置 CLI 之间的本地 ACP 连接 id。

    官方 5.2.7 原始报文与 5.6.2 抓包都是带连字符的 UUID，且语义是「会话级
    元数据」——同一个会话内稳定、换会话换新。这个 id 只在客户端本地有意义，
    上游无从校验其真实性，所以按 (账号, 会话键) 稳定派生：同会话同值、
    跨账号/跨会话互不相同；没有会话键（探活等单发请求）时每次新 UUID。
    """
    key = str(session_key or "").strip()
    if not key:
        return str(uuid.uuid4())
    digest = hashlib.sha256(
        ("acp|%s|%s" % (uid or "anonymous", key)).encode("utf-8")).hexdigest()
    h = list(digest[:32])
    h[12] = "4"      # UUID 版本位（官方实测形态）
    h[16] = "a"      # RFC 4122 变体位
    h = "".join(h)
    return "%s-%s-%s-%s-%s" % (h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])


def trace_headers(with_parent=False):
    """对话请求带的链路头族（traceparent / b3 / X-B3-* / X-Trace-ID）。

    官方桌面端 5.8.4 抓包里 main 请求还带 X-B3-ParentSpanId，b3 是四段
    （`<trace>-<span>-1-<parent>`）：模型调用是会话根 span 的子 span。网关没有
    真实父子关系，但官方客户端自己也是随机生成一个 parentSpanId（5.6.2 的
    app.asar RE：`crypto.randomBytes(8)`），所以桌面端按官方结构补齐
    （with_parent=True）；辅助请求 / 其它身份维持三段 b3。
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


def desktop_body_fields(realm, turn_request_id):
    """桌面端 chat 请求体里官方额外带的字段（按 realm 分支）。

    国内版 5.6.2 的 CLI 钩子抓包 body 末尾有 `agent:"cli"` 与
    `conversationRequestId`（32hex，与该轮 X-Conversation-Request-ID 同源）；
    国际版 5.2.7 原始报文没有这两个字段（body 里是 extra_vars.growthEvent），
    所以 intl 不加。返回空 dict 表示「不追加任何字段」。
    """
    if str(realm or "").lower() != "cn":
        return {}
    return {
        "agent": "cli",
        "conversationRequestId": str(turn_request_id or ""),
    }


def build_identity_headers(product, realm, uid, token,
                           conversation_id=None, enterprise_id="",
                           tenant_id="", department="",
                           session_key=None, turn_request_id=None):
    """回传这一轮要用的身分标头。product = 'workbuddy' (桌面端), 'vscode' 或 'cli'。

    conversation_id 只用于显式指定：留空（网关的常态）就每次请求新生成一个
    UUIDv7。上游不拿这个 id 存历史——抓包后的实测里，同一 id 连发两轮、第二轮
    只带 system+user，模型仍答不出上一轮；换个新 id 结果一样。所以网关不需要
    在会话维度上维护映射。

    session_key / turn_request_id 供桌面端身份使用（官方语义）：
      - acp-connection-id 按 (uid, session_key) 稳定派生（会话级元数据）；
      - X-Conversation-Request-ID / X-Root-Request-ID 取调用方给的一轮 request
        id（open_upstream 在重试循环外生成一次），整轮所有请求/重试复用；
      - X-Request-ID = X-Conversation-Message-ID，每请求新生成。
    其它身份分支不受这两个参数影响。
    """
    product = normalize_product(product)
    is_cn = (realm == "cn")

    conv = str(conversation_id or "").strip() or new_conversation_id()
    msg_id = new_message_id()
    # main 请求里 X-Conversation-Request-ID 与 X-Root-Request-ID 是同一个值，
    # 且早于消息 id —— 一次 user send 开始时生成、该轮所有请求复用。
    conv_req_id = str(turn_request_id or "").strip() or new_message_id()

    if product == PRODUCT_DESKTOP:
        client_ver = DESKTOP_VERSION_CN if is_cn else DESKTOP_VERSION_INTL
        chat_ua = DESKTOP_UA_CN if is_cn else DESKTOP_UA_INTL
        headers = {
            "X-Agent-Intent": "craft",
            "X-Agent-Purpose": "conversation",
            "X-Agent-Type": "main",
            "X-IDE-Name": "WorkBuddy",
            "X-IDE-Type": "WorkBuddy",
            "X-IDE-Version": client_ver,
            # 官方策略：sticky 会话才发（值恒 true）；桌面 daemon 通常置 sticky，
            # 这里按 sticky 处理。
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
            "acp-connection-id": acp_connection_id_for(uid, session_key),
        }
        # OpenAI Node SDK 6.25.0 的指纹头（官方桌面端 chat 实发）。
        headers.update(DESKTOP_STAINLESS_HEADERS)
        # main 请求的链路头族：带 X-B3-ParentSpanId，b3 四段。
        headers.update(trace_headers(with_parent=True))
    elif product == PRODUCT_VSCODE:
        headers = {
            "X-Agent-Intent": "craft",
            "X-IDE-Type": "VSCode",
            "X-IDE-Name": "VSCode",
            "X-IDE-Version": DEFAULT_WORKBUDDY_IDE_VERSION,
            "X-Product-Version": DEFAULT_WORKBUDDY_PRODUCT_VERSION,
            "X-Env-ID": "production",
            "X-Product": "SaaS",
            "X-Domain": endpoint_for(realm, product)[1],
            "User-Agent": "VSCode/%s WorkBuddy/%s" % (
                DEFAULT_WORKBUDDY_IDE_VERSION, DEFAULT_WORKBUDDY_PRODUCT_VERSION),
            "X-User-Id": str(uid or "anonymous"),
            "Authorization": "Bearer " + str(token or ""),
            "X-Conversation-ID": conv,
            "X-Conversation-Request-ID": conv_req_id,
            "X-Root-Request-ID": conv_req_id,
            "X-Conversation-Message-ID": msg_id,
            "X-Request-ID": msg_id,
        }
    else:  # PRODUCT_CLI
        ide_version = DEFAULT_IDE_VERSION
        headers = {
            "X-Agent-Intent": "craft",
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

    if enterprise_id:
        headers["X-Enterprise-Id"] = str(enterprise_id)
        headers["X-Tenant-Id"] = str(tenant_id or enterprise_id)
    if department:
        headers["X-Department-Info"] = str(department)

    return headers
