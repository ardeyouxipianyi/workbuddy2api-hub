# -*- coding: utf-8 -*-
"""wb_desktop.py — 桌面端事件鏈上報（panel desktop.go 的 Python 移植）。

用於成長任務中「上游只認桌面客戶端行為」的判據事件。panel 2026-09-12 多帳號
實測：帶完整桌面指紋（extName=workbuddy-desktop、machineId、ideName…）向
copilot.tencent.com/v2/report 上報對應事件鏈即可點亮，無需真實桌面操作。

提供：
  - report_desktop_events()：桌面指紋 + /v2/report 批量上報；
  - report_web_event()：web 指紋 + www.workbuddy.cn/v2/report（Library_read）；
  - 各任務的事件鏈構造器（chat / buddyapp / template / playbook / canvas /
    automation / appearance）。

純標準庫，Python 3.9 兼容。所有函數冪等（事件內容不影響上游任務狀態）。
"""

import hashlib
import json
import time
import urllib.request
import uuid

import wb_accounts

DESKTOP_UA = "WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1"
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
DESKTOP_REPORT_PATH = "/v2/report"
APPEARANCE_SET_PATH = "/v2/user-asset/appearance/set"
# 小程序（mp）事件指紋常數（panel mpEventBase：appservice wQ()/Ao() 對齊）。
MP_IDE_TYPE = "WorkBuddy_MP"
MP_EXT_VERSION = "2.4.0"
MP_MACHINE_ID = "0655736a-607f-4d9d-b430-58176ee9a090"
# mp 上報走 billing 域（panel BillingBaseCN），與 web 事件（workbuddy.cn）不同域。
MP_REPORT_BASE_CN = "https://www.codebuddy.cn"
MP_REPORT_HEADERS = {
    "X-Client-Product": "workbuddy-mp",
    "X-Client-Version": MP_EXT_VERSION,
    "X-Client-Platform": "mp-weixin",
    "X-Platform": "wechatmp",
}
# Web 控制台 origin（panel webBase：CN = www.workbuddy.cn，國際 = www.workbuddy.ai）。
WEB_BASE_CN = "https://www.workbuddy.cn"
WEB_BASE_INTL = "https://www.workbuddy.ai"
# 桌面指紋常數（panel desktopFingerprint 的實測值）。
RELEASE_DATE = 1789036585355
COMMIT = "5f9692923c93033111c51ad7b003eb80204a9b75"
OS_VERSION = "10.0.26220"


def derive_desktop_id(account, salt):
    """由 uid 穩定派生 36 位 hex 設備標識（machineId / sessionId）。"""
    seed = "%s:%s" % (salt, account.uid)
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:36]


def desktop_fingerprint(account):
    """公共桌面指紋欄位（每個事件注入；業務欄位可覆蓋同名鍵）。"""
    now = int(time.time() * 1000)
    return {
        "timezone": "Asia/Shanghai",
        "reportDelay": 2000,
        "userId": account.uid,
        "username": account.nickname or "",
        "userNickname": account.nickname or "",
        "product": "SaaS",
        "releaseDate": RELEASE_DATE,
        "commit": COMMIT,
        "ideName": "WorkBuddy",
        "ideType": "WorkBuddy",
        "ideVersion": "5.5.6",
        "machineId": derive_desktop_id(account, "machine"),
        "sessionId": derive_desktop_id(account, "session"),
        "extName": "workbuddy-desktop",
        "extVersion": "5.5.6",
        "os": "win32",
        "arch": "x64",
        "osVersion": OS_VERSION,
        "cpuCores": 20,
        "memorySize": 24,
        "timestamp": now,
        "presentAt": now,
    }


def _post_report(account, url, payload, headers):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with wb_accounts.urlopen(req, timeout=15, proxy=account.proxy) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return isinstance(data, dict) and data.get("code") == 0
    except Exception:
        return False


def report_desktop_events(account, events):
    """以桌面指紋批量上報事件到 copilot.tencent.com/v2/report。"""
    if not events:
        return False
    fp = desktop_fingerprint(account)
    merged = []
    for event in events:
        item = dict(fp)
        item.update(event)
        merged.append(item)
    base = wb_accounts.get_realm_config(account.realm)["chat_upstream"]
    headers = {
        "Authorization": "Bearer " + str(account.access_token or ""),
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "User-Agent": DESKTOP_UA,
        "X-Domain": base,
        "X-Product": "SaaS",
        "X-Request-ID": derive_desktop_id(account, "req")
        + str(int(time.time() * 1000) % 1000000),
        "X-User-Id": account.uid,
    }
    return _post_report(account, base + DESKTOP_REPORT_PATH, merged, headers)


def report_web_event(account, event_code, page_url, element_id, element_name):
    """以 web 指紋上報單事件（www.workbuddy.cn/v2/report，Library_read 判據）。"""
    event = {
        "eventCode": event_code,
        "timestamp": int(time.time() * 1000),
        "reportDelay": 0,
        "pageURL": page_url,
        "elementId": element_id,
        "elementName": element_name,
        "os": "Win32",
        "arch": "",
        "osVersion": "10.0",
        "userAgent": WEB_UA,
        "machineId": derive_desktop_id(account, "webmachine"),
        "userId": account.uid,
        "userNickname": account.nickname or "",
        "enterpriseId": getattr(account, "enterprise_id", "") or "",
    }
    base = WEB_BASE_CN if account.realm == "cn" else WEB_BASE_INTL
    headers = {
        "Authorization": "Bearer " + str(account.access_token or ""),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-client-platform": "web",
        "Origin": base,
        "Referer": page_url,
        "User-Agent": WEB_UA,
        "X-User-Id": account.uid,
    }
    return _post_report(account, base + DESKTOP_REPORT_PATH, [event], headers)


def mp_event_base(account):
    """小程序埋點公共指紋（每個事件注入；業務欄位可覆蓋同名鍵）。"""
    return {
        "timestamp": int(time.time() * 1000),
        "ideType": MP_IDE_TYPE,
        "ideVersion": MP_EXT_VERSION,
        "extName": "workbuddy-mp",
        "extVersion": MP_EXT_VERSION,
        "product": "SaaS",
        "ideName": "wx_app_cloud",
        "platform": "mini_program",
        "os": "windows",
        "osVersion": "11",
        "arch": "x64",
        "machineId": MP_MACHINE_ID,
        "timezone": "Asia/Shanghai",
        "userId": account.uid,
        "userNickname": account.nickname or "",
    }


def report_mp_events(account, events):
    """以小程序指紋向 www.codebuddy.cn/v2/report 批量上報事件。"""
    if not events:
        return False
    base = mp_event_base(account)
    merged = []
    for event in events:
        item = dict(base)
        item.update(event)
        merged.append(item)
    headers = {
        "Authorization": "Bearer " + str(account.access_token or ""),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-User-Id": account.uid,
    }
    headers.update(MP_REPORT_HEADERS)
    return _post_report(account, MP_REPORT_BASE_CN + DESKTOP_REPORT_PATH, merged, headers)


def mp_chat_event(conversation_id, activity_id="", model_id="", model_name=""):
    """小程序 chat_request_send 事件（Sequential 對話族判據）。"""
    rid = "wb2api-" + uuid.uuid4().hex
    event = {
        "eventCode": "chat_request_send",
        "inputLength": 14, "isPlan": False, "isAutoExecuteTerminal": False,
        "isAutoModify": False, "codebaseEnable": False, "maxToken": 0,
        "maxSteps": 500, "temperature": 0, "maxRetries": 0,
        "mentionContexts": [], "knowledgeId": [], "knowledgeName": [],
        "codebaseId": "", "mentionContextCount": 0, "command": "",
        "recommendId": "", "skillId": "", "skillCount": 0, "totalCount": 0,
        "traceId": rid, "rootRequestId": rid,
        "parentConversationId": conversation_id,
        "conversationId": conversation_id,
        "messageId": "msg-" + rid[-8:],
        "agentName": "mp", "agentType": "main",
        "codebuddy.session_id": conversation_id,
        "codebuddy.conversation_request_id": rid,
    }
    if activity_id:
        event["activityId"] = activity_id
    if model_id:
        event["requestModelId"] = model_id
        event["requestModelName"] = model_name or model_id
    return event


def mp_expert_event(expert_id, expert_name, expert_type="agent"):
    """小程序 expert_actual_use 事件（Sequential_Tasks_2 判據）。"""
    return {
        "eventCode": "expert_actual_use", "reportDelay": 0,
        "extVersion": "2.2.8", "source": "mini_program",
        "id": expert_id, "name": expert_id,
        "expertTitle": expert_name or expert_id, "type": "send_message",
        "characterCount": 12, "expertType": expert_type or "agent",
    }


def mp_playbook_events(case_id, case_name):
    """小程序靈感事件組（Sequential_Tasks_7 備選判據）。"""
    base = {"id": case_id, "name": case_name, "type": "document",
            "categoryId": "", "categoryName": "", "skills": "",
            "skillNames": ""}
    cta = {"eventCode": "playbook_cta_click", "source": "discover",
           "position": 1, "extVersion": "2.2.8"}
    cta.update(base)
    send = {"eventCode": "playbook_prompt_send", "source": "discover",
            "promptLength": 96, "isOfficial": 1,
            "conversationId": "wb2api-mp-pb-" + uuid.uuid4().hex,
            "extVersion": "2.2.8"}
    send.update(base)
    return [cta, send]


def set_appearance_theme(account, resource_key):
    """POST /v2/user-asset/appearance/set {kind: theme, resource_key}。"""
    base = wb_accounts.get_realm_config(account.realm)["chat_upstream"]
    body = json.dumps({"kind": "theme", "resource_key": resource_key}).encode("utf-8")
    headers = {
        "Authorization": "Bearer " + str(account.access_token or ""),
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "User-Agent": DESKTOP_UA,
        "X-Product": "SaaS",
        "X-User-Id": account.uid,
    }
    req = urllib.request.Request(base + APPEARANCE_SET_PATH, data=body,
                                 method="POST", headers=headers)
    try:
        with wb_accounts.urlopen(req, timeout=15, proxy=account.proxy) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return isinstance(data, dict) and data.get("code") == 0
    except Exception:
        return False


def chat_sequence(conversation_id, request_id, message_id, model_id, model_name):
    """一次桌面端成功對話的完整事件鏈（實測點亮 RichMeow_Chat）。"""
    def mk(code, extra=None):
        event = {"eventCode": code}
        if extra:
            event.update(extra)
        return event

    rid = request_id
    return [
        mk("agent_task_created", {
            "source": "LOCAL", "name": "working", "task_target": "local",
            "mode": "craft", "requestModelId": model_id,
            "requestModelName": model_name, "has_repo": False, "repo_type": "none",
            "workspace_type": "empty", "has_connector": False,
            "connector_types": [], "has_mention": False, "mention_types": [],
            "has_template": False, "action": "", "template_name": "",
            "has_expert": False, "expert_id": "", "expert_name": "",
            "expert_industry_id": "", "has_skill": False, "skill_names": [],
            "conversationId": conversation_id, "messageId": message_id,
            "buddyId": "", "buddyName": "",
        }),
        mk("chat_message_send", {
            "messageId": message_id + "-assistant", "historyCount": 0,
            "isContextTruncated": False, "currentStepCount": 1, "traceId": rid,
            "rootRequestId": request_id, "parentConversationId": conversation_id,
            "agentName": "cli", "agentType": "main",
        }),
        mk("chat_request_send", {
            "inputLength": 24, "isPlan": False, "isAutoExecuteTerminal": False,
            "isAutoModify": False, "codebaseEnable": False, "maxToken": 0,
            "maxSteps": 500, "temperature": 0, "maxRetries": 0,
            "mentionContexts": [], "knowledgeId": [], "knowledgeName": [],
            "codebaseId": "", "mentionContextCount": 0, "command": "",
            "recommendId": "", "skillId": "", "skillCount": 0, "totalCount": 0,
            "traceId": rid, "rootRequestId": request_id,
            "parentConversationId": conversation_id, "agentName": "cli",
            "agentType": "main",
            "codebuddy.session_id": conversation_id,
            "codebuddy.conversation_request_id": request_id,
        }),
        mk("chat_message_response", {
            "messageId": message_id + "-assistant", "responseModelId": model_id,
            "inputToken": 120, "outputToken": 80, "totalToken": 200,
            "cachedTokens": 0, "cachedWriteTokens": 0, "cachedMissTokens": 0,
            "isSuccessful": True, "messageErrorCode": "", "finishReason": "stop",
            "firstTokenAt": int(time.time() * 1000), "traceId": rid,
            "conversationId": conversation_id, "rootRequestId": request_id,
            "parentConversationId": conversation_id, "agentName": "cli",
            "agentType": "main",
            "codebuddy.session_id": conversation_id,
            "codebuddy.conversation_request_id": request_id,
        }),
        mk("chat_message_status", {
            "messageId": message_id + "-assistant", "messageErrorCode": "0",
            "traceId": rid, "rootRequestId": request_id,
            "parentConversationId": conversation_id, "agentName": "cli",
            "agentType": "main",
        }),
        mk("chat_request_response", {
            "mode": "craft", "toolCallCount": 0, "inputToken": 120,
            "outputToken": 80, "totalToken": 200, "cachedTokens": 0,
            "cachedWriteTokens": 0, "cachedMissTokens": 0, "isSuccessful": True,
            "messageErrorCode": "", "finishReason": "stop",
            "rootRequestId": request_id, "parentConversationId": conversation_id,
        }),
    ]


def buddy_app_sequence(buddy_id="cb_y5Dy46tPQGGWtueMxXbe", buddy_name="企鹅教师助手"):
    """進入 Buddy 應用五連事件（實測點亮 Buddy_App 與 Buddy_App_QQ）。"""
    def mk(code, extra=None):
        event = {"eventCode": code, "mode": "LOCAL",
                 "buddyId": buddy_id, "buddyName": buddy_name}
        if extra:
            event.update(extra)
        return event

    return [
        mk("buddyapp_discover_click"),
        mk("buddyapp_show", {"elementId": buddy_id, "elementName": buddy_name,
                             "position": 2}),
        mk("buddyapp_enter_click", {"elementId": buddy_id, "elementName": buddy_name,
                                    "position": 2, "isFirstPage": "1"}),
        mk("buddyapp_auth_confirm_click", {"elementId": buddy_id,
                                           "elementName": buddy_name}),
        mk("buddyapp_bindaccount_skip_click", {"elementId": buddy_id,
                                               "elementName": buddy_name}),
    ]


def automation_create_event(name="wb2api 自动化"):
    """定時任務創建成功事件（實測點亮 automation_1）。"""
    return {
        "eventCode": "automated_task_create_suc", "name": name,
        "source": "manually", "modelId": "fast-model", "modelIsThinking": True,
        "connectorCount": 0, "skills": "", "skillCount": 0,
        "scheduleType": "once", "mode": "LOCAL",
    }


def template_use_sequence(conversation_id, request_id, template_id, template_name):
    """模板使用事件組（實測 5 組點亮 template_5）。"""
    events = chat_sequence(conversation_id, request_id, "msg-" + template_id,
                           "fast-model", "fast-model")
    events.append({"eventCode": "agent_task_created_with_template",
                   "mode": "working", "isCustomModel": False,
                   "id": template_id, "name": template_name,
                   "requestId": request_id})
    events.append({"eventCode": "template_used", "template_id": template_id,
                   "task_mode": "working"})
    return events


def playbook_prompt_sequence(conversation_id, request_id, case_id, case_name):
    """靈感案例做同款事件組（實測點亮 playbook_prompt）。"""
    events = chat_sequence(conversation_id, request_id, "msg-pb",
                           "fast-model", "fast-model")
    payload = {"id": case_id, "name": case_name, "type": "document",
               "categoryId": "", "categoryName": ""}
    cta = {"eventCode": "web_element_click", "pageName": "playbook_detail",
           "elementId": "playbook_ctaClick", "elementName": case_name,
           "source": "discover"}
    events.append(cta)
    click = {"eventCode": "playbook_cta_click", "source": "discover", "position": 0}
    click.update(payload)
    events.append(click)
    send = {"eventCode": "playbook_prompt_send", "conversationId": conversation_id,
            "requestId": request_id}
    send.update(payload)
    events.append(send)
    return events


def design_canvas_sequence(conversation_id, request_id):
    """設計創意畫布事件組（實測點亮 create_canvas，+300 分）。"""
    events = chat_sequence(conversation_id, request_id, "msg-canvas",
                           "fast-model", "fast-model")
    events.append({"eventCode": "wbx_design_canvas_task_create",
                   "conversationId": conversation_id, "requestId": request_id,
                   "source": "summon_keyword", "cost": 12000, "isSuccessful": True})
    events.append({"eventCode": "wbx_design_canvas_open",
                   "conversationId": conversation_id, "requestId": request_id,
                   "id": "ardot-file-" + request_id[-8:],
                   "source": "summon_keyword", "type": "page",
                   "cost": 13000, "isSuccessful": True})
    return events


def appearance_skin_event(theme_key="theme-tkmw7j"):
    """主題生效事件（panel 實測判據：settings_close 離開設置頁時上報）。"""
    return {"eventCode": "appearance_skin_apply", "action": "apply",
            "source": "settings_close", "id": theme_key, "vipLevel": 0,
            "series": "", "type": "unknown"}
