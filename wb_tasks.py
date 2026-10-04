"""wb_tasks.py —— 国内版成长任务与日常福利全自动完成引擎

包含功能：
1. 成长任务查询、批量接取 (accept)、构造事件上报点亮 (report)、领奖入账 (claim)。
2. 连续打卡 (streak) 与能量 (energy) 余额查询。
3. 猫猫旅行 (buddy travel) 状态查询、自动派出与自动领奖。
4. 严格遵守 >= 1.0s 防风控间隔，并使用 wb_fingerprint 的稳定设备指纹。
"""
import datetime
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid

import wb_accounts as _accounts
import wb_desktop

CHAT_BASE = "https://copilot.tencent.com"
BILL_BASE = "https://www.codebuddy.cn"
WEB_BASE = "https://www.workbuddy.cn"
DESKTOP_UA = "WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1"
WEB_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"


_log = lambda msg: None


def set_logger(fn):
    """Route task diagnostics to the caller's logger.

    The growth endpoints swallow their errors so one dead endpoint cannot
    abort a whole cycle. Without a logger those failures are invisible, and an
    upstream change looks identical to "no tasks today".
    """
    global _log
    _log = fn or (lambda msg: None)

TASK_SPECS = {
    "create_canvas": {"kind": "canvas", "target": 1, "reward": 300, "name": "创建设计任务"},
    "template_5": {"kind": "template", "target": 5, "reward": 200, "name": "模板创建任务"},
    "expert_5": {"kind": "expert", "target": 5, "reward": 200, "name": "使用专家助手"},
    "Expert_team_use_3": {"kind": "team", "target": 3, "reward": 150, "name": "使用专家团队"},
    "skill_1": {"kind": "skill", "target": 1, "reward": 100, "name": "体验技能"},
    "automation_1": {"kind": "automation", "target": 1, "reward": 100, "name": "创建自动化任务"},
    "playbook_prompt": {"kind": "playbook", "target": 1, "reward": 100, "name": "灵感案例使用"},
    "Expert_lighthouse": {"kind": "lighthouse", "target": 1, "reward": 100, "name": "轻量云专家使用"},
    "Buddy_App": {"kind": "buddy5", "target": 1, "reward": 100, "name": "进入 Buddy 应用"},
    "Buddy_App_QQ": {"kind": "buddy5", "target": 1, "reward": 100, "name": "企鹅教师助手"},
    "Hp_Appearance": {"kind": "skin", "target": 1, "reward": 100, "name": "应用主题外观"},
    "chat_5": {"kind": "chat", "target": 5, "reward": 100, "name": "发起 5 次对话"},
    "Model_chat_GLM5.2": {"kind": "glmchat", "target": 1, "reward": 100, "name": "体验 GLM-5.2"},
    "black_cat": {"kind": "cat", "target": 3, "reward": 100, "name": "夜猫子任务 (23:00-08:00)"},
    "RichMeow_Chat": {"kind": "richmeow", "target": 1, "reward": 100, "name": "桌面对话事件链"},
    "Library_read": {"kind": "library", "target": 1, "reward": 100, "name": "浏览资料库"},
    "first_buddy": {"kind": "buddy_first", "target": 1, "reward": 0, "name": "领养首只猫猫"},
    "Expert_Philanthropy": {"unforgeable": True, "reason": "真实捐款动作", "reward": 0, "name": "公益爱心捐赠"},
    # 小程序（mp）口徑常態任務：默認列表不下發，需 X-Client-Platform: miniprogram。
    # school_season（校園日）活動已於 2026-09-24 結束，不登記（mp 列表出現也會被
    # pending() 過濾）。Sequential 族每日零點解鎖一環。
    "Sequential_Tasks_1": {"kind": "mpchat", "target": 1, "reward": 100, "name": "小程序首对话"},
    "Sequential_Tasks_2": {"kind": "mpexpert", "target": 1, "reward": 200, "name": "小程序选专家对话"},
    "Sequential_Tasks_3": {"kind": "mpchat", "target": 5, "reward": 300, "name": "小程序五次对话"},
    "Sequential_Tasks_4": {"kind": "mpauto", "target": 1, "reward": 100, "name": "小程序定时任务"},
    "Sequential_Tasks_5": {"kind": "mpmodel", "target": 1, "reward": 100, "name": "小程序使用 GLM5.2"},
    "Sequential_Tasks_6": {"kind": "mpchat", "target": 10, "reward": 500, "name": "小程序十次对话"},
    "Sequential_Tasks_7": {"kind": "mpplaybook", "target": 1, "reward": 500, "name": "小程序体验灵感"},
}

# ---------------------------------------------------------------------------
# 逆向修复常量 (2026-09 实测校准)
# ---------------------------------------------------------------------------
# 这些任务需要桌面客户端的真实行为信号。panel 2026-09-12 多账号实测后，下列
# 任务已能用带完整桌面指纹的事件链纯 API 点亮，见 DESKTOP_ACTIONS；此表仅保留
# 尚未破解、必须真实操作的条目（当前为空，保留供后续逆向使用）。
DESKTOP_ONLY_TASKS = {}

# 夜猫子任务只在 23:00-08:00 上报才计数, 且每天 1 次、累计 3 天。
NIGHT_TASK_CODES = {"black_cat"}

def in_night_window():
    h = time.localtime().tm_hour
    return h >= 23 or h < 8

# 专家/团队事件必须彼此不同: 桌面端 appendGrowthEvent 按 (eventCode, id) 去重,
# 上游同样只按不同 id 累加进度 —— 重复发同一个 id 进度永远不动。
# 下列 id 已在 2026-09 实测中验证可推进任务进度。
EXPERT_ID_POOL = [
    ("ex_PZw8Gu81HfN4", "运维工程师"), ("ex_ROsDtJbzADFV", "产品经理"),
    ("ex_SMUnl0nJbPix", "UI设计师"), ("ex_ZTR062oVBOCW", "数据分析师"),
    ("ex_a3sSSFBy8qaC", "后端架构师"), ("ex_aG1kvKbq8lPx", "文案策划"),
    ("ex_al1vxtUOYQ10", "测试专家"), ("ex_cZfiyuET9UQP", "安全顾问"),
    ("ex_eggOvQuVP0hq", "算法工程师"), ("ex_hSwsQjkSKnkX", "前端工程师"),
    ("ex_mMbwwmFA9n9P", "项目管理专家"), ("ex_uAQE5POfk7Zh", "增长运营专家"),
    ("ex_uZzSAScSy7FZ", "行业研究员"), ("ex_LHywGrZOtG7G", "数据分析师"),
    ("ex_NX5C8GBciVed", "测试架构师"), ("ex_DdCsaoq4AtcO", "云端运维专家"),
    ("ex_KzqKQguubrNQ", "内容创作专家"), ("ex_2cvvUZQhDyeJ", "腾讯轻量云专家"),
]
# 团队 id 来自官方专家清单 expert_center.json (expertType=team, 共 53 个),
# 前 3 个已在 2026-09 实测验证可推进 Expert_team_use_3。
TEAM_ID_POOL = [
    ("CloudOpsTeam", "运维专家团队"), ("CloudContentTeam", "内容专家团队"),
    ("CloudDevTeam", "研发专家团队"), ("ProductStrategyTeam", "产品战略团队"),
    ("MarketingCampaignTeam", "营销活动团队"), ("SalesBattleTeam", "销售作战团队"),
    ("DesignEngineTeam", "设计引擎团队"), ("HrOperationsTeam", "人力运营团队"),
]


def fetch_growth_tasks(account, mp=False):
    """查询成长任务列表及当前状态；mp=True 走小程序口径（mp 专属任务）。"""
    url = CHAT_BASE + "/v2/activity/growth/tasks"
    headers = account.headers("chat")
    if mp:
        headers = dict(headers)
        headers["X-Client-Platform"] = "miniprogram"
    req = urllib.request.Request(url, headers=headers)
    try:
        with _accounts.urlopen(req, timeout=15, proxy=account.proxy) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            raw_tasks = (data.get("data") or {}).get("tasks") or []
            tasks = []
            for t in raw_tasks:
                code = t.get("task_code") or ""
                spec = TASK_SPECS.get(code, {})
                prog = t.get("progress") or {}
                status = str(t.get("accept_status") or "not_accepted")
                tasks.append({
                    "task_code": code,
                    "name": t.get("title") or spec.get("name") or code,
                    "description": t.get("description") or t.get("task_desc") or "",
                    "jump_url": t.get("jump_url") or "",
                    "status": status,
                    "accept_status": status,
                    "upstream_status": str(t.get("status") or ""),
                    "locked": bool(t.get("locked")),
                    "claimable": bool(t.get("claimable")),
                    "claimed": bool(t.get("claimed") or status == "claimed"),
                    "current": prog.get("current", 0),
                    "target": prog.get("target", spec.get("target", 1)),
                    "reward_credit": t.get("reward_credit") or spec.get("reward", 0),
                    "reward_energy": t.get("reward_energy", 0),
                    "unforgeable": bool(spec.get("unforgeable")),
                    "reason": spec.get("reason", ""),
                })
            return tasks
    except Exception as exc:
        return []


def fetch_growth_summary(account):
    """查询连续打卡、猫猫旅行与能量余额。"""
    headers = account.headers("chat")
    out = {"energy": 0, "streak_days": 0, "travel": {"state": "unknown"}}
    # 1. 能量
    try:
        req = urllib.request.Request(CHAT_BASE + "/v2/activity/growth/energy", headers=headers)
        with _accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            out["energy"] = (d.get("data") or {}).get("balance", 0)
    except Exception as exc:
        _log(f"growth/energy query failed: {exc}")
    # 2. 连续打卡
    try:
        req = urllib.request.Request(CHAT_BASE + "/activity/growth/streak", headers=headers)
        with _accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            st = (d.get("data") or {}).get("streak") or {}
            out["streak_days"] = st.get("days", 0)
    except Exception as exc:
        _log(f"growth/streak query failed: {exc}")
    # 3. 猫猫旅行
    try:
        req = urllib.request.Request(CHAT_BASE + "/activity/growth/buddy/travel/status", headers=headers)
        with _accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            out["travel"] = d.get("data") or {}
    except Exception as exc:
        _log(f"buddy/travel/status query failed: {exc}")
    return out


def accept_tasks(account, codes, chunk=20, mp=False):
    """批量接取任务。

    上游按批返回 results, 单个任务可能 status=accepted / already_accepted /
    其它失败原因。过去这里只返回 bool 且吞掉异常, 接取失败时上层完全看不见,
    于是后续上报的事件全部作用在未接取的任务上 —— 进度永远 0, 领奖必然
    "task not completed", 表现就是"接取了一堆但一个都没点亮"。
    现在返回 {"ok": bool, "accepted": [...], "failed": [...], "msg": str}。
    """
    out = {"ok": True, "accepted": [], "failed": [], "msg": ""}
    if not codes:
        return out
    url = CHAT_BASE + "/v2/activity/growth/tasks/accept"
    for i in range(0, len(codes), max(1, chunk)):
        part = codes[i:i + max(1, chunk)]
        body = json.dumps({"task_codes": part}).encode("utf-8")
        headers = account.headers("chat")
        if mp:
            headers = dict(headers)
            headers["X-Client-Platform"] = "miniprogram"
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers=headers)
        try:
            with _accounts.urlopen(req, timeout=15, proxy=account.proxy) as resp:
                d = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(300).decode("utf-8", "replace")
            except Exception:
                pass
            out["ok"] = False
            out["msg"] = "HTTP %s: %s" % (exc.code, detail[:200])
            out["failed"].extend(part)
            _log(f"accept batch failed: {out['msg']}")
            continue
        except Exception as exc:
            out["ok"] = False
            out["msg"] = str(exc)[:200]
            out["failed"].extend(part)
            _log(f"accept batch failed: {exc}")
            continue
        if d.get("code") != 0:
            out["ok"] = False
            out["msg"] = d.get("msg") or ("code=%s" % d.get("code"))
            out["failed"].extend(part)
            _log(f"accept rejected: {out['msg']}")
            continue
        results = (d.get("data") or {}).get("results") or []
        seen = set()
        for item in results:
            code = item.get("task_code")
            st = str(item.get("status") or "")
            seen.add(code)
            if st in ("accepted", "already_accepted"):
                out["accepted"].append(code)
            else:
                out["failed"].append(code)
                _log(f"task {code} accept status={st}")
        for code in part:
            if code not in seen:
                out["failed"].append(code)
    return out


def claim_task(account, code, mp=False):
    """领取任务奖励。支持 copilot.tencent.com -> www.workbuddy.cn 自动降级。"""
    url = f"{CHAT_BASE}/activity/growth/tasks/{code}/claim"
    headers = account.headers("chat")
    if mp:
        headers = dict(headers)
        headers["X-Client-Platform"] = "miniprogram"
    req = urllib.request.Request(url, data=b"{}", method="POST", headers=headers)
    try:
        with _accounts.urlopen(req, timeout=15, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            if d.get("code") == 0:
                data = d.get("data") or {}
                return {"ok": True, "credit": data.get("credit", 0), "energy": data.get("energy", 0)}
            # 200 + 非0码 (典型: 400 task not completed —— 进度还没落账就来领奖)
            _log(f"task {code} claim rejected: {d.get('msg')}")
            return {"ok": False, "credit": 0, "energy": 0, "msg": d.get("msg") or f"code={d.get('code')}"}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if exc.fp else ""
        err_msg = f"HTTP {exc.code}"
        try:
            err_msg = json.loads(body).get("msg") or err_msg
        except Exception:
            pass
        if exc.code == 400:
            # 降级到 web 域领奖
            web_url = f"{WEB_BASE}/activity/growth/tasks/{code}/claim"
            web_hdrs = {
                "Authorization": "Bearer " + account.access_token,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": WEB_BASE,
                "Referer": f"{WEB_BASE}/profile/growth-center",
                "x-client-platform": "web",
                "User-Agent": WEB_UA,
                "X-User-Id": account.uid,
                "X-Domain": WEB_BASE,
            }
            try:
                req_web = urllib.request.Request(web_url, data=b"{}", method="POST", headers=web_hdrs)
                with _accounts.urlopen(req_web, timeout=15, proxy=account.proxy) as resp:
                    d = json.loads(resp.read().decode("utf-8"))
                    if d.get("code") == 0:
                        data = d.get("data") or {}
                        return {"ok": True, "credit": data.get("credit", 0), "energy": data.get("energy", 0)}
                    _log(f"task {code} web claim rejected: code={d.get('code')} msg={d.get('msg')}")
                    return {"ok": False, "credit": 0, "energy": 0, "msg": d.get("msg") or err_msg}
            except Exception as exc2:
                _log(f"task {code} web claim failed: {exc2}")
        _log(f"task {code} claim failed: {err_msg}")
        return {"ok": False, "credit": 0, "energy": 0, "msg": err_msg}
    except Exception as exc:
        _log(f"task {code} claim failed: {exc}")
    return {"ok": False, "credit": 0, "energy": 0}


def build_event(account, kind, idx=0, expert=None):
    """构造指定类型的真实规范事件数据。

    expert: (expert_id, expert_name) —— 专家/团队类事件必须每次使用不同的 id,
    上游按 (eventCode, id) 去重, 重复 id 不会推进任务进度。
    """
    now = int(time.time() * 1000)
    cid = f"wb-task-{now}-{idx}"
    rid = f"{cid}-req"
    uid = account.uid

    if kind == "canvas":
        return {"eventCode": "wbx_design_canvas_task_create", "timestamp": now,
                "reportDelay": 0, "conversationId": cid, "requestId": rid,
                "source": "summon_keyword", "isCustomModel": False, "name": "",
                "inputLength": 12, "id": f"wbx-canvas-{now}", "cost": 0,
                "isSuccessful": True, "userId": uid}
    if kind == "template":
        return {"eventCode": "agent_task_created_with_template", "timestamp": now,
                "reportDelay": 0, "isCustomModel": True, "id": str(idx),
                "name": "幻灯片", "requestId": rid, "conversationId": cid, "userId": uid}
    if kind in ("expert", "team", "lighthouse"):
        etype = "team" if kind == "team" else "agent"
        if expert:
            ex_id, name = expert
        elif kind == "lighthouse":
            ex_id, name = "ex_2cvvUZQhDyeJ", "腾讯轻量云专家"
        elif kind == "team":
            ex_id, name = "CloudOpsTeam", "运维专家团队"
        else:
            ex_id, name = "ContentCreator", "内容创作专家"
        return {"eventCode": "expert_actual_use", "timestamp": now, "reportDelay": 0,
                "mode": "CLOUD", "id": ex_id, "name": name, "expertTitle": name,
                "type": "02-Engineering", "expertType": etype, "source": "builtin",
                "version": "1.0.2", "cost": 0, "characterCount": 12, "conversationId": cid,
                "requestId": rid, "messageId": rid, "requestModelId": "deepseek-v4-flash",
                "requestModelName": "DeepSeek V4 Flash", "userId": uid}
    if kind == "skill":
        return {"eventCode": "skill_info", "timestamp": now, "reportDelay": 0,
                "skillId": "skill_2096525080079265792", "name": "pptx", "userId": uid}
    if kind == "automation":
        return {"eventCode": "automated_task_create_suc", "timestamp": now, "reportDelay": 0,
                "name": "每周工作整理", "type": "cron", "source": "manually",
                "modelId": "deepseek-v4-flash", "modelIsThinking": False,
                "conversationId": cid, "requestId": rid,
                "schedule": {"type": "recurring", "rrule": "FREQ=WEEKLY;BYDAY=FR;BYHOUR=9;BYMINUTE=0"},
                "prompt": "每周五自动整理本周工作", "userId": uid}
    if kind == "playbook":
        return {"eventCode": "playbook_prompt_send", "timestamp": now, "reportDelay": 0,
                "id": "worker-ledger-freedom-dashboard", "name": "打工人小账本",
                "type": "other", "promptLength": 10, "isOfficial": 1,
                "source": "discover", "conversationId": cid, "requestId": rid, "userId": uid}
    if kind == "skin":
        return {"eventCode": "appearance_skin_apply", "timestamp": now, "reportDelay": 0,
                "action": "apply", "source": "settings_close", "id": "theme-tkmw7j",
                "vipLevel": "free", "series": "craft", "type": "unknown",
                "name": "和平精英激战金秋", "userId": uid}
    if kind in ("chat", "glmchat", "cat"):
        m_id = "glm-5.2" if kind in ("glmchat", "cat") else "deepseek-v4-flash"
        m_nm = "GLM-5.2" if kind in ("glmchat", "cat") else "DeepSeek V4 Flash"
        mode = "night" if kind == "cat" else "craft"
        return {"eventCode": "chat_request_send", "timestamp": now, "reportDelay": 0,
                "mode": mode, "conversationId": cid, "requestId": rid,
                "inputLength": 12, "requestModelId": m_id, "requestModelName": m_nm,
                "isPlan": False, "agentName": "default", "agentType": "conversation",
                "userId": uid}
    return {"eventCode": "heartbeat", "timestamp": now, "userId": uid}


def report_events(account, events, base=None):
    """向上游上报事件数组。

    默认发 copilot.tencent.com (chat 侧) —— 与桌面客户端真实上报地址一致
    (桌面 NetLog: POST https://copilot.tencent.com/v2/report), 2026-09 实测
    该侧事件会实时推进成长任务进度。
    """
    if base is None:
        base = CHAT_BASE
    url = base + "/v2/report"
    headers = account.headers("billing" if base == BILL_BASE else "chat")
    body = json.dumps(events).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with _accounts.urlopen(req, timeout=15, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            return d.get("code") == 0
    except Exception:
        return False


def do_cat_travel(account):
    """检查并执行猫猫旅行 (领奖 / 派出)。"""
    headers = account.headers("chat")
    # 1. 查询状态
    try:
        req = urllib.request.Request(CHAT_BASE + "/activity/growth/buddy/travel/status", headers=headers)
        with _accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            st = d.get("data") or {}
    except Exception as exc:
        return {"ok": False, "msg": f"查询旅行状态失败: {exc}"}

    state = st.get("state")
    if state == "arrived":
        # 领奖 (官方前端: POST travel/claim body {})
        req_cl = urllib.request.Request(CHAT_BASE + "/activity/growth/buddy/travel/claim", data=b"{}", method="POST", headers=headers)
        try:
            with _accounts.urlopen(req_cl, timeout=10, proxy=account.proxy) as resp:
                c_res = json.loads(resp.read().decode("utf-8"))
                credit = (c_res.get("data") or {}).get("reward_credit", 0)
                account.fetch_credits()
                return {"ok": True, "action": "claim", "credit": credit, "msg": f"旅行归来领奖成功！获得 {credit} 积分"}
        except Exception as e:
            return {"ok": False, "msg": f"领奖失败: {e}"}

    if state == "idle":
        if st.get("daily_limit_reached"):
            return {"ok": True, "action": "idle", "msg": "猫猫今日已完成旅行，明日 00:00 刷新"}
        # 派出旅行: 官方前端要求 body 携带 {location_id} (目的地清单在
        # travel/config) —— 空 body 会被拒 400 "invalid request"。
        lid = None
        try:
            req_cfg = urllib.request.Request(CHAT_BASE + "/activity/growth/buddy/travel/config", headers=headers)
            with _accounts.urlopen(req_cfg, timeout=10, proxy=account.proxy) as resp:
                cfg = json.loads(resp.read().decode("utf-8"))
                locs = (cfg.get("data") or {}).get("locations") or []
            if locs:
                lid = locs[0].get("id")
        except Exception as exc:
            _log(f"buddy/travel/config query failed: {exc}")
        if lid is None:
            lid = 1
        dep_body = json.dumps({"location_id": lid}).encode("utf-8")
        req_dep = urllib.request.Request(CHAT_BASE + "/activity/growth/buddy/travel/depart", data=dep_body, method="POST", headers=headers)
        try:
            with _accounts.urlopen(req_dep, timeout=10, proxy=account.proxy) as resp:
                dep_res = json.loads(resp.read().decode("utf-8"))
                if dep_res.get("code") == 0:
                    loc = ((dep_res.get("data") or {}).get("location") or {}).get("name") or ""
                    return {"ok": True, "action": "depart", "msg": f"猫猫已出发前往「{loc}」，预计数小时后归来！"}
                return {"ok": False, "msg": f"派出旅行被上游拒绝: {dep_res.get('msg')}"}
        except Exception as e:
            return {"ok": False, "msg": f"派出旅行失败: {e}"}

    if state == "traveling":
        return {"ok": True, "action": "traveling", "msg": "猫猫正在旅行途中，请稍后再来查看！"}

    return {"ok": True, "action": state, "msg": f"当前状态: {state}"}


# ---------------------------------------------------------------------------
# 桌面事件链动作（wb_desktop，panel 2026-09-12 多账号实测判据）
# ---------------------------------------------------------------------------
def _desktop_chat_chain(account, need, prefix):
    ok_all = True
    for i in range(max(1, need)):
        ms = int(time.time() * 1000)
        conv = "wb2api-%s-%d-%d" % (prefix, ms, i)
        req = conv + "-req"
        events = wb_desktop.chat_sequence(conv, req,
                                          "msg-%s-%d" % (prefix, i),
                                          "fast-model", "fast-model")
        if not wb_desktop.report_desktop_events(account, events):
            ok_all = False
        time.sleep(0.3)
    return ok_all, "已上报桌面端完整对话事件链 ×%d" % max(1, need)


def run_desktop_richmeow(account, need):
    return _desktop_chat_chain(account, 1, "rm")


def run_desktop_buddy_app(account, need):
    ok = wb_desktop.report_desktop_events(account, wb_desktop.buddy_app_sequence())
    return ok, "已上报 buddyapp 进入五连事件（同时覆盖 Buddy_App 与 Buddy_App_QQ）"


def run_desktop_automation(account, need):
    ok = wb_desktop.report_desktop_events(
        account, [wb_desktop.automation_create_event()])
    return ok, "已上报定时任务创建事件"


def run_desktop_library(account, need):
    ok = wb_desktop.report_web_event(
        account, "web_element_click",
        "https://www.workbuddy.cn/space/d/o0KWYeynteVv06UnAZqIFm",
        "library_doc_intro_click", "WorkBuddy资料库介绍")
    return ok, "已上报资料库介绍阅读事件（web 指纹）"


_TEMPLATE_POOL = [("1", "深度研究"), ("2", "周报生成"), ("3", "竞品分析"),
                  ("4", "活动策划"), ("5", "代码评审")]


def run_desktop_template(account, need):
    ok_all = True
    count = max(1, need)
    for i in range(count):
        tid, tname = _TEMPLATE_POOL[i % len(_TEMPLATE_POOL)]
        ms = int(time.time() * 1000)
        conv = "wb2api-tpl-%d-%d" % (ms, i)
        req = conv + "-req"
        events = wb_desktop.template_use_sequence(conv, req, tid, tname)
        if not wb_desktop.report_desktop_events(account, events):
            ok_all = False
        time.sleep(0.3)
    return ok_all, "已上报 template_used ×%d" % count


def run_desktop_playbook(account, need):
    ms = int(time.time() * 1000)
    conv = "wb2api-pb-%d" % ms
    req = conv + "-req"
    events = wb_desktop.playbook_prompt_sequence(
        conv, req, "pm-gtm-launch-plan", "新产品上市 GTM 发布计划一页纸")
    ok = wb_desktop.report_desktop_events(account, events)
    return ok, "已上报 playbook_cta_click + playbook_prompt_send"


def run_desktop_canvas(account, need):
    ms = int(time.time() * 1000)
    conv = "wb2api-canvas-%d" % ms
    req = conv + "-req"
    events = wb_desktop.design_canvas_sequence(conv, req)
    ok = wb_desktop.report_desktop_events(account, events)
    return ok, "已上报 wbx_design_canvas_task_create/open"


def run_desktop_appearance(account, need):
    theme = "theme-tkmw7j"
    wb_desktop.set_appearance_theme(account, theme)
    time.sleep(2)
    ok = wb_desktop.report_desktop_events(
        account, [wb_desktop.appearance_skin_event(theme)])
    return ok, "已设置主题并上报皮肤生效事件"


DESKTOP_ACTIONS = {
    "RichMeow_Chat": run_desktop_richmeow,
    "Buddy_App": run_desktop_buddy_app,
    "Buddy_App_QQ": run_desktop_buddy_app,
    "automation_1": run_desktop_automation,
    "Library_read": run_desktop_library,
    "template_5": run_desktop_template,
    "playbook_prompt": run_desktop_playbook,
    "create_canvas": run_desktop_canvas,
    "Hp_Appearance": run_desktop_appearance,
}


# ---------------------------------------------------------------------------
# 連登管家（panel scheduler/streak.go + upstream/streak.go）
# ---------------------------------------------------------------------------
def _growth_json(account, method, path, body=None, timeout=15):
    """Growth-center call on the chat domain; returns (data, error)."""
    data = None
    headers = dict(account.headers("chat"))
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(CHAT_BASE + path, data=data, method=method,
                                 headers=headers)
    try:
        with _accounts.urlopen(req, timeout=timeout, proxy=account.proxy) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(300).decode("utf-8", "replace")
        except Exception:
            pass
        return None, "HTTP %s: %s" % (exc.code, detail[:200])
    except Exception as exc:
        return None, str(exc)[:200]
    if not isinstance(payload, dict):
        return None, "growth response is not an object"
    if payload.get("code") != 0:
        return None, str(payload.get("msg") or ("code=%s" % payload.get("code")))
    return payload.get("data") or {}, ""


def _billing_json(account, method, path, body=None, timeout=15):
    """Billing-domain call (www.codebuddy.cn for CN); returns (data, error)."""
    cfg = _accounts.get_realm_config(account.realm)
    url = cfg["billing_upstream"] + path
    headers = dict(account.headers("billing"))
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        with _accounts.urlopen(
                urllib.request.Request(url, data=data, method=method,
                                       headers=headers),
                timeout=timeout, proxy=account.proxy) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(300).decode("utf-8", "replace")
        except Exception:
            pass
        return None, "HTTP %s: %s" % (exc.code, detail[:200])
    except Exception as exc:
        return None, str(exc)[:200]
    if not isinstance(payload, dict) or payload.get("code") != 0:
        msg = payload.get("msg") if isinstance(payload, dict) else "bad response"
        return None, str(msg or "unknown error")
    return payload.get("data") or {}, ""


def streak_full(account):
    """GET /activity/growth/streak -> full streak/redemption payload."""
    return _growth_json(account, "GET", "/activity/growth/streak")


def fetch_streak_days(account):
    """Current consecutive-login day count, or None when unavailable."""
    data, err = streak_full(account)
    if err:
        return None
    try:
        return int(((data or {}).get("streak") or {}).get("days") or 0)
    except (TypeError, ValueError):
        return None


def redeem_tier(account, tier):
    """POST /activity/growth/redeem {tier, client_token} (idempotent token)."""
    return _growth_json(account, "POST", "/activity/growth/redeem",
                        {"tier": tier, "client_token": str(uuid.uuid4())})


def lottery_chances(account):
    """GET /activity/growth/lottery/summary -> (chances, error)."""
    data, err = _growth_json(account, "GET", "/activity/growth/lottery/summary")
    if err:
        return 0, err
    try:
        return int((data or {}).get("chances") or 0), ""
    except (TypeError, ValueError):
        return 0, "bad chances payload"


def lottery_draw(account):
    """POST /activity/growth/lottery/draw -> (prize, error)."""
    return _growth_json(account, "POST", "/activity/growth/lottery/draw",
                        {"client_token": str(uuid.uuid4())})


def heatmap_yesterday_missed(account):
    """True when yesterday's heatmap cell exists with score 0."""
    data, err = _growth_json(account, "GET", "/activity/growth/heatmap")
    if err:
        return False, err
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    for cell in (data or {}).get("cells") or []:
        if isinstance(cell, dict) and str(cell.get("date") or "")[:10] == yesterday:
            return (cell.get("score") or 0) == 0, ""
    return False, ""


def use_makeup_card(account, target_date):
    """POST /activity/growth/makeup-cards/use {target_date}."""
    _data, err = _growth_json(account, "POST", "/activity/growth/makeup-cards/use",
                              {"target_date": target_date})
    return err == ""


def claim_gift(account):
    """POST /billing/meter/claim-gift -> (credit, error)."""
    data, err = _billing_json(account, "POST", "/billing/meter/claim-gift", {})
    if err:
        return 0, err
    try:
        return int((data or {}).get("credit") or 0), ""
    except (TypeError, ValueError):
        return 0, "bad gift payload"


def claim_compensation(account):
    """POST /billing/meter/claim-compensation -> (credit, error)."""
    data, err = _billing_json(account, "POST", "/billing/meter/claim-compensation", {})
    if err:
        return 0, err
    try:
        return int((data or {}).get("credit") or 0), ""
    except (TypeError, ValueError):
        return 0, "bad compensation payload"


def run_streak_bonus(account):
    """連登管家：補簽 → 禮包/補償 → 兌換已解鎖檔位 → 抽完所有次數（冪等）。

    對應 panel scheduler/streak.go：未解鎖（403）與已領取檔位自動跳過；
    每次執行都不會重複消耗上游配額。
    """
    if account.realm != "cn":
        return {"ok": False, "logs": ["国际版不适用国内成长中心"], "credit": 0}
    logs = []
    credit_total = 0

    # 0. 補簽保連登（昨日漏簽且有補簽卡）。
    missed, _err = heatmap_yesterday_missed(account)
    if missed:
        full, _full_err = streak_full(account)
        cards = int(((full or {}).get("makeup_cards") or {}).get("balance") or 0)
        if cards > 0:
            yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
            if use_makeup_card(account, yesterday):
                logs.append("已用补签卡补签 %s（保连登）" % yesterday)
            else:
                logs.append("补签失败（上游拒绝）")
        else:
            logs.append("昨日漏签但没有补签卡")

    # 0.5 禮包/補償（每號一次；無則靜默）。
    gift, gift_err = claim_gift(account)
    if not gift_err and gift:
        credit_total += gift
        logs.append("新手礼包 +%s 积分" % gift)
    comp, comp_err = claim_compensation(account)
    if not comp_err and comp:
        credit_total += comp
        logs.append("补偿领取 +%s 积分" % comp)

    # 1. 兌換所有已解鎖檔位。
    full, err = streak_full(account)
    if err:
        logs.append("连登状态查询失败: %s" % err)
        return {"ok": False, "logs": logs, "credit": credit_total}
    status = (full or {}).get("redemption_status") or {}
    statuses = {
        "7d": status.get("tier_7d_status"),
        "14d": status.get("tier_14d_status"),
        "28d": status.get("tier_28d_status"),
    }
    for tier in status.get("tiers") or []:
        if not isinstance(tier, dict):
            continue
        name = str(tier.get("tier") or "")
        if statuses.get(name) in ("locked", "claimed"):
            continue
        _data, redeem_err = redeem_tier(account, name)
        if redeem_err:
            logs.append("兑换 %s 档跳过: %s" % (name, redeem_err))
            continue
        logs.append("兑换 %s 档（+%s积分 +%s能量 卡×%s 抽奖×%s）" % (
            name, tier.get("credit") or 0, tier.get("energy") or 0,
            tier.get("cards") or 0, tier.get("chances") or 0))
        time.sleep(0.5)

    # 2. 抽完所有次數。
    chances, chance_err = lottery_chances(account)
    if chance_err:
        logs.append("抽奖次数查询失败: %s" % chance_err)
    else:
        for i in range(chances):
            prize, draw_err = lottery_draw(account)
            if draw_err:
                logs.append("第%d抽失败: %s" % (i + 1, draw_err))
                break
            logs.append("第%d抽: %s" % (i + 1, json.dumps(
                prize, ensure_ascii=False)[:200]))
            time.sleep(0.5)
        if chances:
            logs.append("抽奖完成 %d 次" % chances)

    account.fetch_credits()
    return {"ok": True, "logs": logs, "credit": credit_total,
            "credits": account.credits}


# 小程序對話事件之間的真人節奏間隔：連發會被反作弊回滾（claim 400
# "task not completed"），panel 2026-09-26 實測 45s 間隔可穩定入賬。
MP_CHAT_GAP = float(os.environ.get("WB_MP_CHAT_GAP") or "45")
MP_SEQUENTIAL_CODES = ("Sequential_Tasks_1", "Sequential_Tasks_2",
                       "Sequential_Tasks_3", "Sequential_Tasks_4",
                       "Sequential_Tasks_5", "Sequential_Tasks_6",
                       "Sequential_Tasks_7")


def _report_sequential_events(account, code, need):
    """上報對應 Sequential 任務的判據事件（panel 各 runSequential*）。"""
    if code in ("Sequential_Tasks_1", "Sequential_Tasks_3", "Sequential_Tasks_6"):
        for i in range(need):
            conv = "wb2api-mp-%d-%d" % (int(time.time() * 1000), i)
            if not wb_desktop.report_mp_events(
                    account, [wb_desktop.mp_chat_event(conv)]):
                return False, "小程序对话事件上报失败"
            if i < need - 1:
                time.sleep(MP_CHAT_GAP)
        return True, "已上报小程序对话事件 ×%d（间隔 %.0fs 防回滚）" % (need, MP_CHAT_GAP)
    if code == "Sequential_Tasks_2":
        expert_id, expert_name = EXPERT_ID_POOL[0]
        if not wb_desktop.report_mp_events(
                account, [wb_desktop.mp_expert_event(expert_id, expert_name)]):
            return False, "小程序专家事件上报失败"
        return True, "已上报小程序专家使用事件（%s）" % expert_id
    if code == "Sequential_Tasks_4":
        if not wb_desktop.report_desktop_events(
                account, [wb_desktop.automation_create_event("wb2api 自动化")]):
            return False, "定时任务创建事件上报失败"
        return True, "已上报定时任务创建事件（PC 同源判据）"
    if code == "Sequential_Tasks_5":
        conv = "wb2api-mp-glm-%d" % int(time.time() * 1000)
        if not wb_desktop.report_mp_events(
                account, [wb_desktop.mp_chat_event(conv, model_id="glm-5.2",
                                                   model_name="GLM-5.2")]):
            return False, "小程序模型对话事件上报失败"
        return True, "已上报小程序 GLM-5.2 对话事件"
    if code == "Sequential_Tasks_7":
        ms = int(time.time() * 1000)
        conv = "wb2api-pb-%d" % ms
        req = conv + "-req"
        if wb_desktop.report_desktop_events(
                account, wb_desktop.playbook_prompt_sequence(
                    conv, req, "pm-gtm-launch-plan",
                    "新产品上市 GTM 发布计划一页纸")):
            return True, "已上报灵感事件组（PC 判据）"
        if wb_desktop.report_mp_events(
                account, wb_desktop.mp_playbook_events(
                    "pm-gtm-launch-plan", "新产品上市 GTM 发布计划一页纸")):
            return True, "已上报灵感事件组（mp 备选判据）"
        return False, "灵感事件组上报失败"
    return False, "未知 Sequential 任务 %s" % code


def run_sequential_task(account, code, gap=None):
    """Sequential 小程序任務通用骨架（panel runSequentialEventTask）。

    mp 查詢 → accept（mp 頭）→ 判據事件上報 → 回讀 → 達標領獎（mp 頭）。
    locked 期間 accept 不落賬，直接跳過等次日零點解鎖。
    """
    if account.realm != "cn":
        return False, "国际版不适用国内成长任务中心", 0
    if code not in MP_SEQUENTIAL_CODES:
        return False, "不是小程序 Sequential 任务", 0
    tasks = fetch_growth_tasks(account, mp=True)
    task = next((t for t in tasks if t["task_code"] == code), None)
    if task is None:
        return True, "mp 口径未下发该任务（前置未完成或活动未开始），跳过", 0
    if task.get("claimed") or task.get("status") == "claimed":
        return True, "已完成（已领取）", 0
    if task.get("locked"):
        return True, "任务未解锁（每日零点解锁一环），跳过", 0
    target = max(1, int(task.get("target") or 1))
    current = int(task.get("current") or 0)
    if task.get("claimable") or current >= target or task.get("status") == "completed":
        res = claim_task(account, code, mp=True)
        if res.get("ok"):
            credit = res.get("credit", 0) or 0
            return True, "已达标，领奖成功 +%s 积分" % credit, credit
        return False, "已达标但领奖失败: %s" % (res.get("msg") or "未知原因"), 0
    if task.get("status") == "not_accepted":
        accepted = accept_tasks(account, [code], mp=True)
        if code not in (accepted.get("accepted") or []):
            return True, "accept 未登记生效（可能处于每日锁定窗口），等下次调度", 0
        time.sleep(gap if gap is not None else 1.0)
        tasks = fetch_growth_tasks(account, mp=True)
        task = next((t for t in tasks if t["task_code"] == code), task)
        target = max(1, int(task.get("target") or target))
        current = int(task.get("current") or 0)
    need = max(1, target - current)
    ok, detail = _report_sequential_events(account, code, need)
    if not ok:
        return False, detail, 0
    prog = current
    for _round in range(3):
        time.sleep(3.0)
        fresh = next((t for t in fetch_growth_tasks(account, mp=True)
                      if t["task_code"] == code), None)
        if fresh:
            prog = int(fresh.get("current") or 0)
            if fresh.get("claimable") or fresh.get("claimed") or prog >= target:
                break
    if prog < target:
        return False, "%s；进度 %s/%s 未点亮（判据形态待校正，下次重试）" % (
            detail, prog, target), 0
    res = claim_task(account, code, mp=True)
    if res.get("ok"):
        credit = res.get("credit", 0) or 0
        return True, "%s；领奖成功 +%s 积分" % (detail, credit), credit
    return False, "进度已达但领奖失败: %s" % (res.get("msg") or "未知原因"), 0


def run_single_task(account, code, gap=1.0):
    """执行单个成长任务动作并自动领奖（panel runGrowthQueued 同语义）。

    回傳 (ok, message, earned_credit)。隊列與單任務入口共用本函數：先回讀任務
    狀態（已領取/未解鎖/不可自動化直接跳過），需要時補接取，再按 DESKTOP_ACTIONS
    或通用事件上報，等進度落賬後領獎。
    """
    if account.realm != "cn":
        return False, "国际版不适用国内成长任务中心", 0
    if code in MP_SEQUENTIAL_CODES:
        return run_sequential_task(account, code, gap=gap)
    tasks = fetch_growth_tasks(account)
    task = next((t for t in tasks if t["task_code"] == code), None)
    if task is None:
        return False, "该账号无此任务", 0
    if task.get("claimed") or task.get("status") == "claimed":
        return True, "已完成（已领取）", 0
    if task.get("unforgeable"):
        return False, "该任务不可自动化: %s" % (task.get("reason") or ""), 0
    if task.get("locked"):
        return True, "任务未解锁（上游锁定），跳过", 0
    if code in NIGHT_TASK_CODES and not in_night_window():
        return True, "夜猫子任务仅 23:00-08:00 计数，跳过", 0

    cur = int(task.get("current") or 0)
    tgt = int(task.get("target") or 1)
    if task.get("claimable") or cur >= tgt or task.get("upstream_status") == "complete":
        res = claim_task(account, code)
        if res.get("ok"):
            credit = res.get("credit", 0) or 0
            return True, "已达标，领奖成功 +%s 积分" % credit, credit
        return False, "已达标但领奖失败: %s" % (res.get("msg") or "未知原因"), 0

    if task.get("status") == "not_accepted":
        accepted = accept_tasks(account, [code])
        if code not in (accepted.get("accepted") or []):
            return False, "接取失败: %s" % (accepted.get("msg") or "上游拒绝"), 0
        time.sleep(gap)
        tasks = fetch_growth_tasks(account)
        task = next((t for t in tasks if t["task_code"] == code), task)
        cur = int(task.get("current") or 0)
        tgt = int(task.get("target") or tgt or 1)

    need = max(1, tgt - cur)
    action = DESKTOP_ACTIONS.get(code)
    if action:
        try:
            report_ok, detail = action(account, need)
        except Exception as exc:
            return False, "事件链异常: %s" % exc, 0
    else:
        spec = TASK_SPECS.get(code) or {}
        kind = spec.get("kind")
        report_ok = True
        detail = "已上报 %s 事件 ×%d" % (kind or "heartbeat", need)
        id_pool = None
        if kind in ("expert", "team"):
            id_pool = TEAM_ID_POOL if kind == "team" else EXPERT_ID_POOL
        for i in range(need):
            expert = None
            if id_pool:
                pid, pnm = id_pool[(cur + i) % len(id_pool)]
                expert = (pid, pnm)
            event = build_event(account, kind, idx=i, expert=expert)
            if not report_events(account, [event]):
                report_ok = False
            if i < need - 1:
                time.sleep(gap)
    if not report_ok:
        return False, "事件上报失败（上游拒绝）", 0

    # Wait for the upstream to post the progress (usually 1-3s).
    prog = cur
    for attempt in range(6):
        fresh = next((x for x in fetch_growth_tasks(account)
                      if x["task_code"] == code), None)
        if fresh:
            prog = int(fresh.get("current") or 0)
            if prog >= tgt or fresh.get("status") in ("completed", "claimed"):
                break
            if prog > cur:
                time.sleep(2.5)
                continue
        if attempt < 2:
            time.sleep(1.5)
        else:
            break
    if prog < tgt:
        return False, "%s；进度 %s/%s 未达成，领奖顺延" % (detail, prog, tgt), 0
    res = claim_task(account, code)
    if res.get("ok"):
        credit = res.get("credit", 0) or 0
        return True, "%s；领奖成功 +%s 积分" % (detail, credit), credit
    return False, "进度已达 %s/%s 但领奖失败: %s" % (prog, tgt, res.get("msg") or "未知原因"), 0


def run_growth_tasks(account, gap=1.0):
    """完整执行批量成长任务点亮与领奖。"""
    if account.realm != "cn":
        return {"ok": False, "msg": "国际版不适用国内成长任务中心", "logs": []}

    logs = []
    logs.append(f"开始为账号 {account.nickname or account.uid[:8]} 运行成长任务自动化...")
    tasks = fetch_growth_tasks(account)
    if not tasks:
        logs.append("未能获取到任务清单，请检查网络或账号状态")
        return {"ok": False, "logs": logs, "earned_credit": 0}

    # 1. 批量接取未接任务
    unaccepted = [t["task_code"] for t in tasks if t["status"] == "not_accepted" and not t.get("unforgeable")]
    if unaccepted:
        logs.append(f"发现 {len(unaccepted)} 个待接取任务，正在批量接取...")
        acc = accept_tasks(account, unaccepted)
        if acc.get("failed"):
            logs.append(f"! 接取未成功 {len(acc['failed'])} 个: {', '.join(acc['failed'][:5])}"
                        + (" ..." if len(acc["failed"]) > 5 else "")
                        + (f" ({acc['msg']})" if acc.get("msg") else ""))
        if acc.get("accepted"):
            logs.append(f"✓ 已接取 {len(acc['accepted'])} 个任务")
        time.sleep(gap)
        tasks = fetch_growth_tasks(account)
        # 复核一次: 上游偶尔会瞬时拒绝整个批次, 复查后仍处于未接取的再补一次,
        # 否则后面所有上报都作用在未接取的任务上 —— 进度全是 0。
        still = [t["task_code"] for t in tasks if t["status"] == "not_accepted"]
        if still:
            logs.append(f"仍有 {len(still)} 个未接取，重试接取一次...")
            retry = accept_tasks(account, still)
            if retry.get("accepted"):
                logs.append(f"✓ 重试接取成功 {len(retry['accepted'])} 个")
            time.sleep(gap)
            tasks = fetch_growth_tasks(account)
        if not tasks:
            logs.append("! 接取后无法获取任务清单，本轮中止")
            return {"ok": False, "logs": logs, "earned_credit": 0}
        still_pending = [t["task_code"] for t in tasks if t["status"] == "not_accepted"]
        if still_pending:
            logs.append(f"! 仍有 {len(still_pending)} 个任务处于未接取状态，"
                        f"对未接取任务上报事件不会计入进度，本轮跳过这些任务")

    total_earned = 0
    # 2. 处理每个任务
    for t in tasks:
        code = t["task_code"]
        spec = TASK_SPECS.get(code)
        if not spec or spec.get("unforgeable"):
            continue

        status = t["status"]
        cur = t.get("current", 0)
        tgt = t.get("target", 1)

        if status == "claimed":
            continue

        # 已完成的任务先领奖 —— 桌面端/夜间任务也可能被真实操作完成
        # (例如用户自己在桌面端用了一次, 或夜里调度器点亮了), 这类任务必须
        # 先结算, 不能因为"只能靠真实操作"就直接跳过丢掉奖励。
        if status == "completed" or cur >= tgt:
            res = claim_task(account, code)
            if res.get("ok"):
                cr = res.get("credit", 0)
                total_earned += cr
                logs.append(f"✓ 任务 [{spec['name']}] 领奖成功: +{cr} 积分")
            else:
                logs.append(f"! 任务 [{spec['name']}] 领奖失败: {res.get('msg') or '未知原因'}")
            time.sleep(gap)
            continue

        # 只认桌面端真实行为的任务: 伪造事件不会推进进度, 诚实跳过并给出深链。
        if code in DESKTOP_ONLY_TASKS:
            jump = t.get("jump_url") or "workbuddy://chat"
            logs.append(f"⏭ 任务 [{spec['name']}] 需真实操作完成: {DESKTOP_ONLY_TASKS[code]}"
                        f" (深链 {jump}), 跳过事件伪造")
            continue

        # 夜猫子任务只在 23:00-08:00 计数 (每日 01:00 调度器也会自动执行)
        if code in NIGHT_TASK_CODES and not in_night_window():
            logs.append(f"🌙 任务 [{spec['name']}] 仅 23:00-08:00 上报计数, 当前不在窗口, 跳过"
                        f" (每日 01:00 调度器自动执行, 累计 3 天)")
            continue

        if status == "not_accepted":
            # 接取没成功就上报是白费功夫: 上游只对已接取的任务累计进度。
            logs.append(f"⏭ 任务 [{spec['name']}] 仍未接取, 跳过 (先解决接取失败)")
            continue

        # 3. 需点亮上报 —— 专家/团队事件必须使用互不相同的 id, 否则上游按
        #    (eventCode, id) 去重, 进度永远不动。
        need = max(1, tgt - cur)
        kind = spec.get("kind")
        action = DESKTOP_ACTIONS.get(code)
        if action:
            logs.append(f"正在点亮任务 [{spec['name']}] (桌面事件链 ×{need})...")
            try:
                report_ok, detail = action(account, need)
            except Exception as exc:
                report_ok, detail = False, "事件链异常: %s" % exc
            logs.append(("✓ " if report_ok else "! ") + detail)
        else:
            logs.append(f"正在点亮任务 [{spec['name']}] (需上报 {need} 次)...")
            report_ok = True
            id_pool = None
            if kind in ("expert", "team"):
                id_pool = TEAM_ID_POOL if kind == "team" else EXPERT_ID_POOL
            for i in range(need):
                expert = None
                if id_pool:
                    pid, pnm = id_pool[(cur + i) % len(id_pool)]
                    expert = (pid, pnm)
                ev = build_event(account, kind, idx=i, expert=expert)
                if not report_events(account, [ev]):
                    report_ok = False
                if i < need - 1:
                    time.sleep(gap)
            if not report_ok:
                logs.append(f"! 任务 [{spec['name']}] 部分事件上报失败 (上游拒绝), 继续尝试领奖")
        time.sleep(1.5)

        # 等上游把进度落账再领奖。进度通常 1-3 秒就可见, 因此先快查几次;
        # 只有确实在动才继续等, 免得每个卡住的任务都空等 20 秒 (整轮要几分钟)。
        prog = cur
        for attempt in range(6):
            fresh = next((x for x in fetch_growth_tasks(account) if x["task_code"] == code), None)
            if fresh:
                prog = fresh.get("current", 0)
                if prog >= tgt or fresh.get("status") in ("completed", "claimed"):
                    break
                if prog > cur:
                    # 已经在涨了, 值得多等一会儿
                    time.sleep(2.5)
                    continue
            if attempt < 2:
                time.sleep(1.5)
            else:
                break
        if prog < tgt:
            logs.append(f"? 任务 [{spec['name']}] 已上报但进度 {prog}/{tgt} 未达成, 领奖顺延到下次运行")
            time.sleep(gap)
            continue

        # 领奖
        res = claim_task(account, code)
        if res.get("ok"):
            cr = res.get("credit", 0)
            total_earned += cr
            logs.append(f"✓ 任务 [{spec['name']}] 点亮并领奖成功: +{cr} 积分")
        else:
            logs.append(f"! 任务 [{spec['name']}] 进度已达 {prog}/{tgt} 但领奖失败: {res.get('msg') or '未知原因'}")
        time.sleep(gap)

    # 4. 顺手检查猫猫旅行
    tr_res = do_cat_travel(account)
    if tr_res.get("msg"):
        logs.append(f"猫猫日常: {tr_res.get('msg')}")
        if tr_res.get("credit"):
            total_earned += tr_res.get("credit", 0)

    # 5. 刷新积分余额
    account.fetch_credits()
    logs.append(f"🎉 全部完成！本次累计新增到账: +{total_earned} 积分，当前总剩余: {account.credits.get('remain', 0)} 积分")
    return {"ok": True, "logs": logs, "earned_credit": total_earned, "credits": account.credits}


def run_night_growth(account):
    """夜猫子任务 (black_cat): 每日 23:00-08:00 上报 1 次 GLM-5.2 夜间对话事件,
    每天计 1 次、累计 3 天后可领奖。白天调用会诚实跳过。"""
    if account.realm != "cn":
        return {"ok": False, "msg": "国际版不适用国内成长任务中心", "logs": [], "earned_credit": 0}
    logs = []
    name = account.nickname or account.uid[:8]
    if not in_night_window():
        return {"ok": True, "earned_credit": 0, "logs": [
            f"[{name}] 当前不在 23:00-08:00 夜间窗口, 夜猫子任务跳过 (每日 01:00 自动执行)"]}
    tasks = fetch_growth_tasks(account)
    t = next((x for x in tasks if x["task_code"] == "black_cat"), None)
    if not t:
        return {"ok": False, "earned_credit": 0, "logs": [f"[{name}] 未获取到夜猫子任务清单"]}
    if t["status"] == "claimed":
        return {"ok": True, "earned_credit": 0, "logs": [f"[{name}] 夜猫子任务已领奖"]}
    cur, tgt = t.get("current", 0), t.get("target", 3)
    if cur >= tgt:
        res = claim_task(account, "black_cat")
        cr = res.get("credit", 0) if res.get("ok") else 0
        account.fetch_credits()
        logs.append(f"[{name}] 夜猫子任务达标, 领奖 {'✓ +' + str(cr) + ' 积分' if res.get('ok') else '! 失败: ' + (res.get('msg') or '')}")
        return {"ok": res.get("ok", False), "earned_credit": cr, "logs": logs}
    ev = build_event(account, "cat", idx=0)
    ok = report_events(account, [ev])
    logs.append(f"[{name}] 上报夜间 GLM-5.2 对话事件: {'成功' if ok else '失败'} (进度 {cur}/{tgt})")
    fresh = None
    for _ in range(4):
        time.sleep(4)
        fresh = next((x for x in fetch_growth_tasks(account) if x["task_code"] == "black_cat"), None)
        if fresh and fresh.get("current", 0) > cur:
            break
    new_cur = fresh.get("current", cur) if fresh else cur
    if new_cur >= tgt:
        res = claim_task(account, "black_cat")
        cr = res.get("credit", 0) if res.get("ok") else 0
        logs.append(f"[{name}] 夜猫子任务完成 {new_cur}/{tgt}, 领奖 {'✓ +' + str(cr) + ' 积分' if res.get('ok') else '! 失败: ' + (res.get('msg') or '')}")
    elif new_cur > cur:
        logs.append(f"[{name}] 今晚 +1 ({new_cur}/{tgt}), 明晚继续, 累计 3 天可领奖")
    else:
        logs.append(f"[{name}] 已上报但进度暂未变化 ({new_cur}/{tgt}), 明晚调度器会继续累计")
    account.fetch_credits()
    earned = 0
    for line in logs:
        m = re.search(r"\+(\d+) 积分", line)
        if m:
            earned = int(m.group(1))
    return {"ok": True, "earned_credit": earned, "logs": logs}
