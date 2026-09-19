"""wb_tasks.py —— 国内版成长任务与日常福利全自动完成引擎

包含功能：
1. 成长任务查询、批量接取 (accept)、构造事件上报点亮 (report)、领奖入账 (claim)。
2. 连续打卡 (streak) 与能量 (energy) 余额查询。
3. 猫猫旅行 (buddy travel) 状态查询、自动派出与自动领奖。
4. 严格遵守 >= 1.0s 防风控间隔，并使用 wb_fingerprint 的稳定设备指纹。
"""
import json
import re
import time
import urllib.error
import urllib.request

import wb_accounts as _accounts

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
}

# ---------------------------------------------------------------------------
# 逆向修复常量 (2026-09 实测校准)
# ---------------------------------------------------------------------------
# 这些任务上游只认桌面客户端的真实行为信号 (jump_url 均为 workbuddy:// 深链,
# 需要真实点击进入对应页面)。伪造 /v2/report 事件会被忽略或落到 heartbeat,
# 进度永远是 0/1, claim 必然返回 400 "task not completed"。诚实地跳过并给出深链。
DESKTOP_ONLY_TASKS = {
    "RichMeow_Chat": "在桌面端发起 1 次对话",
    "Library_read": "在桌面端打开「资料库」并读完介绍文档",
    "Buddy_App": "在桌面端左上角「发现应用」进入任意一个 Buddy 应用",
    "Buddy_App_QQ": "在桌面端「发现应用」进入「企鹅教师助手」",
}

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


def fetch_growth_tasks(account):
    """查询成长任务列表及当前状态。"""
    url = CHAT_BASE + "/v2/activity/growth/tasks"
    req = urllib.request.Request(url, headers=account.headers("chat"))
    try:
        with _accounts.urlopen(req, timeout=15, proxy=account.proxy) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            raw_tasks = (data.get("data") or {}).get("tasks") or []
            tasks = []
            for t in raw_tasks:
                code = t.get("task_code") or ""
                spec = TASK_SPECS.get(code, {})
                prog = t.get("progress") or {}
                tasks.append({
                    "task_code": code,
                    "name": t.get("title") or spec.get("name") or code,
                    "description": t.get("description") or t.get("task_desc") or "",
                    "jump_url": t.get("jump_url") or "",
                    "status": t.get("accept_status") or "not_accepted",
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


def accept_tasks(account, codes):
    """批量接取任务。"""
    if not codes: return True
    url = CHAT_BASE + "/v2/activity/growth/tasks/accept"
    body = json.dumps({"task_codes": codes}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers=account.headers("chat"))
    try:
        with _accounts.urlopen(req, timeout=15, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            return d.get("code") == 0
    except Exception:
        return False


def claim_task(account, code):
    """领取任务奖励。支持 copilot.tencent.com -> www.workbuddy.cn 自动降级。"""
    url = f"{CHAT_BASE}/activity/growth/tasks/{code}/claim"
    req = urllib.request.Request(url, data=b"{}", method="POST", headers=account.headers("chat"))
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
        accept_tasks(account, unaccepted)
        time.sleep(gap)
        tasks = fetch_growth_tasks(account)

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

        if status == "completed" or cur >= tgt:
            # 直接领奖
            res = claim_task(account, code)
            if res.get("ok"):
                cr = res.get("credit", 0)
                total_earned += cr
                logs.append(f"✓ 任务 [{spec['name']}] 领奖成功: +{cr} 积分")
            else:
                logs.append(f"! 任务 [{spec['name']}] 领奖失败: {res.get('msg') or '未知原因'}")
            time.sleep(gap)
            continue

        # 3. 需点亮上报 —— 专家/团队事件必须使用互不相同的 id, 否则上游按
        #    (eventCode, id) 去重, 进度永远不动。
        need = max(1, tgt - cur)
        kind = spec.get("kind")
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

        # 等上游把进度落账再领奖: 上报到进度可见存在秒级延迟, 立刻领奖会吃
        # 400 "task not completed", 表现为全部 +0。
        prog = cur
        for _ in range(5):
            fresh = next((x for x in fetch_growth_tasks(account) if x["task_code"] == code), None)
            if fresh:
                prog = fresh.get("current", 0)
                if prog >= tgt or fresh.get("status") in ("completed", "claimed"):
                    break
            time.sleep(4)
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
