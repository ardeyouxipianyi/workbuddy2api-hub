# -*- coding: utf-8 -*-
"""wb_identity.py - 出站身分 (WorkBuddy 桌面端 / 官方 VSCode 插件 / 官方 CodeBuddy CLI)

支援三套出站身分:
  1. WorkBuddy 獨立桌面客戶端 (workbuddy / desktop / wb):
     - X-IDE-Type: WorkBuddy
     - X-IDE-Name: WorkBuddy
     - X-Product: WorkBuddy
     - 國際版端點: www.workbuddy.ai (UA: WorkBuddy/5.5.2 WorkBuddy AI/5.5.2 CLI/5.5.2)
     - 國內版端點: copilot.tencent.com (UA: WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1)

  2. 官方 VSCode 插件 (vscode / vsc):
     - X-IDE-Type: VSCode
     - X-IDE-Name: VSCode
     - X-Product: SaaS
     - UA: VSCode/<ver> WorkBuddy/<ver>
     - 國際版端點: www.workbuddy.ai，國內版端點: www.workbuddy.cn

  3. 官方 CodeBuddy CLI (cli):
     - X-IDE-Type: CLI
     - X-IDE-Name: CLI
     - X-Product: SaaS
     - UA: CLI/<ver> CodeBuddy/<ver>
     - 國際版端點: www.workbuddy.ai，國內版端點: copilot.tencent.com
"""

import hashlib
import uuid

DEFAULT_IDE_VERSION = "2.117.2"
DEFAULT_WORKBUDDY_IDE_VERSION = "1.119.0"
DEFAULT_WORKBUDDY_PRODUCT_VERSION = "4.9.29177644"

PRODUCT_DESKTOP = "workbuddy"
PRODUCT_VSCODE = "vscode"
PRODUCT_CLI = "cli"
VALID_PRODUCTS = (PRODUCT_DESKTOP, PRODUCT_VSCODE, PRODUCT_CLI)


def normalize_product(value):
    """把各種寫法收斂成 'workbuddy'、'vscode' 或 'cli'。"""
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
    """回傳 (chat base URL, X-Domain)。身分換了，端點也要跟著換。"""
    key = ("cn" if realm == "cn" else "intl", normalize_product(product))
    return _ENDPOINTS[key]


def domain_for_realm(realm):
    """X-Domain 的值（預設桌面端）。"""
    return endpoint_for(realm, PRODUCT_DESKTOP)[1]


# 進程啟動時的一次性鹽：讓由會話鍵/輪級鍵派生的聚合 ID 無法被外部按鍵內容預計算。
# 重啟換新不構成斷檔（重啟時舊會話/對話輪已結束），與 panel session/ids.go 同語義。
_DERIVE_SALT = uuid.uuid4().hex


def new_message_id():
    """32 位 hex 的消息/請求 ID（UUID4 去橫線），對齊官方 X-Request-ID 形態。"""
    return uuid.uuid4().hex


def stable_request_id(key):
    """由穩定鍵派生 32 hex 的 conversation-request ID。

    同 key 在進程內恆定；空 key 每次生成新值（無會話可聚合時保持原行為）。
    純派生、無快取，內存不隨會話/輪數增長。
    """
    key = str(key or "")
    if not key:
        return new_message_id()
    digest = hashlib.sha256((_DERIVE_SALT + "|" + key).encode("utf-8")).hexdigest()
    return digest[:32]


def valid_trace_id(value):
    """B3 TraceId 只認 16 或 32 位 hex。"""
    s = str(value or "")
    if len(s) not in (16, 32):
        return False
    return all(c in "0123456789abcdefABCDEF" for c in s)


def build_identity_headers(product, realm, uid, token,
                           conversation_id=None, enterprise_id="",
                           tenant_id="", department="",
                           conversation_request_id=None, trace_id=None):
    """回傳這一輪要用的身分標頭。product = 'workbuddy' (桌面端), 'vscode' 或 'cli'。

    conversation_request_id / trace_id 由請求層在輪轉循環外算好：同一輪的所有
    重試、換號、降級共用同一個聚合主鍵（panel issue #35 後台聚合語義）；缺省時
    自行生成，既有呼叫方零改動。
    """
    product = normalize_product(product)
    is_cn = (realm == "cn")

    conv = str(conversation_id or "").strip()
    msg_id = new_message_id()
    conv_req = str(conversation_request_id or "").strip() or new_message_id()
    trace = str(trace_id or "").strip() or conv_req
    # B3 只認 16/32 hex：入站聚合鍵形狀不合法時退回消息級 ID，避免破壞鏈路關聯。
    b3_trace = conv_req if valid_trace_id(conv_req) else msg_id
    family = {
        "X-Conversation-Request-ID": conv_req,
        "X-Conversation-Message-ID": msg_id,
        "X-Request-ID": msg_id,
        "X-Root-Request-ID": conv_req,
        "X-Trace-ID": trace,
        "X-B3-TraceId": b3_trace,
        "X-B3-SpanId": msg_id[:16],
        "X-B3-Sampled": "1",
    }

    if product == PRODUCT_DESKTOP:
        client_ver = "5.5.6" if is_cn else "5.5.2"
        chat_ua = ("WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1" if is_cn
                   else "WorkBuddy/5.5.2 WorkBuddy AI/5.5.2 CLI/5.5.2")
        headers = {
            "X-Agent-Purpose": "conversation",
            "X-IDE-Name": "WorkBuddy",
            "X-IDE-Type": "WorkBuddy",
            "X-IDE-Version": client_ver,
            "X-Product": "WorkBuddy",
            "X-Domain": endpoint_for(realm, product)[1],
            "User-Agent": chat_ua,
            "X-User-Id": str(uid or "anonymous"),
            "Authorization": "Bearer " + str(token or ""),
            "X-Request-ID": msg_id,
        }
        # X-Conversation-ID 透傳客戶端原值；客戶端沒給就不偽造（panel 同口徑）。
        if conv:
            headers["X-Conversation-ID"] = conv
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
            "X-Conversation-ID": conv or str(uuid.uuid4()).upper(),
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
            "X-Conversation-ID": conv or str(uuid.uuid4()).upper(),
        }

    headers.update(family)
    if enterprise_id:
        headers["X-Enterprise-Id"] = str(enterprise_id)
        headers["X-Tenant-Id"] = str(tenant_id or enterprise_id)
    if department:
        headers["X-Department-Info"] = str(department)

    return headers
