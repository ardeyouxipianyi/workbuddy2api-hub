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
# 国内版客户端没有对应抓包，维持原值，不凭猜测改。
DESKTOP_VERSION_CN = "5.5.6"
DESKTOP_UA_CN = "WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1"

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


def trace_headers():
    """5.8.4 对话请求带的链路头族（traceparent / b3 / X-B3-* / X-Trace-ID）。

    main 请求还带 X-B3-ParentSpanId，那需要真实的父子调用关系，网关没有，
    就不伪造了（辅助请求本来也不带）。
    """
    trace = new_message_id()
    span = secrets.token_hex(8)
    return {
        "X-Trace-ID": trace,
        "traceparent": "00-%s-%s-01" % (trace, span),
        "b3": "%s-%s-1" % (trace, span),
        "X-B3-TraceId": trace,
        "X-B3-SpanId": span,
        "X-B3-Sampled": "1",
    }


def build_identity_headers(product, realm, uid, token,
                           conversation_id=None, enterprise_id="",
                           tenant_id="", department=""):
    """回传这一轮要用的身分标头。product = 'workbuddy' (桌面端), 'vscode' 或 'cli'。

    conversation_id 只用于显式指定：留空（网关的常态）就每次请求新生成一个
    UUIDv7。上游不拿这个 id 存历史——抓包后的实测里，同一 id 连发两轮、第二轮
    只带 system+user，模型仍答不出上一轮；换个新 id 结果一样。所以网关不需要
    在会话维度上维护映射。
    """
    product = normalize_product(product)
    is_cn = (realm == "cn")

    conv = str(conversation_id or "").strip() or new_conversation_id()
    msg_id = new_message_id()
    # main 请求里 X-Conversation-Request-ID 与 X-Root-Request-ID 是同一个值，
    # 且早于消息 id —— 一次 user send 开始时生成、该轮所有请求复用。
    conv_req_id = new_message_id()

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
        headers.update(trace_headers())
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
