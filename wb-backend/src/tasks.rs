//! 任务与福利：只做**查询**（`wb_tasks.fetch_growth_tasks` / `fetch_growth_summary`）。
//!
//! shortcut: 接取/上报/领奖（run）还没移植，面板能看进度、不能执行。

use std::sync::Arc;

use serde_json::{json, Value};

use crate::identity::{chat_headers, Ctx};
use crate::state::AccountFile;
use crate::App;

/// 国内版成长任务都在这个域名下（`wb_tasks.CHAT_BASE`）。
const CN_CHAT: &str = "https://copilot.tencent.com";

/// `task_code -> (name, target, reward)`，与 `wb_tasks.TASK_SPECS` 同表。
const TASK_SPECS: &[(&str, &str, i64, i64)] = &[
    ("create_canvas", "创建设计任务", 1, 300),
    ("template_5", "模板创建任务", 5, 200),
    ("expert_5", "使用专家助手", 5, 200),
    ("Expert_team_use_3", "使用专家团队", 3, 150),
    ("skill_1", "体验技能", 1, 100),
    ("automation_1", "创建自动化任务", 1, 100),
    ("playbook_prompt", "灵感案例使用", 1, 100),
    ("Expert_lighthouse", "轻量云专家使用", 1, 100),
    ("Buddy_App", "进入 Buddy 应用", 1, 100),
    ("Buddy_App_QQ", "企鹅教师助手", 1, 100),
    ("Hp_Appearance", "应用主题外观", 1, 100),
    ("chat_5", "发起 5 次对话", 5, 100),
    ("Model_chat_GLM5.2", "体验 GLM-5.2", 1, 100),
    ("black_cat", "夜猫子任务 (23:00-08:00)", 3, 100),
    ("RichMeow_Chat", "桌面对话事件链", 1, 100),
    ("Library_read", "浏览资料库", 1, 100),
    ("first_buddy", "领取一只 Buddy", 1, 300),
    ("WorkBuddy_Valorant", "体验「无畏契约拼豆专家」", 1, 100),
];

/// 上游不认伪造事件的任务：跳过并给出原因（`wb_tasks.TASK_SPECS` 里的 unforgeable）。
const UNFORGEABLE: &[(&str, &str)] = &[
    ("wb_wechat_oa_subscribe_task", "需在微信关注「腾讯 WorkBuddy」公众号满 24 小时"),
    ("Expert_Philanthropy", "真实捐款动作"),
];

fn spec(code: &str) -> Option<(&'static str, i64, i64)> {
    TASK_SPECS
        .iter()
        .find(|(c, _, _, _)| *c == code)
        .map(|(_, name, target, reward)| (*name, *target, *reward))
}

fn headers(account: &AccountFile) -> reqwest::header::HeaderMap {
    use reqwest::header::HeaderValue;
    let mut map = reqwest::header::HeaderMap::new();
    let ctx = Ctx {
        realm: "cn",
        product: &account.str("product"),
        uid: &account.uid(),
        token: &account.access_token(),
        enterprise_id: None,
        department: None,
        session_key: None,
    };
    for (key, value) in chat_headers(&ctx) {
        if let (Ok(k), Ok(v)) = (
            key.parse::<reqwest::header::HeaderName>(),
            HeaderValue::from_str(&value),
        ) {
            map.insert(k, v);
        }
    }
    map
}

async fn get(app: &Arc<App>, account: &AccountFile, path: &str) -> Result<Value, String> {
    let resp = app
        .upstream
        .get(format!("{CN_CHAT}{path}"))
        .headers(headers(account))
        .timeout(std::time::Duration::from_secs(15))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    let status = resp.status();
    let text = resp.text().await.map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("{status}"));
    }
    serde_json::from_str(&text).map_err(|e| e.to_string())
}

/// 成长任务列表；上游报错就当「今天没有任务」（与 Python 同口径）。
pub async fn growth_tasks(app: &Arc<App>, account: &AccountFile) -> Vec<Value> {
    let Ok(res) = get(app, account, "/v2/activity/growth/tasks").await else {
        return Vec::new();
    };
    let raw = res
        .get("data")
        .and_then(|d| d.get("tasks"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    raw.iter()
        .map(|t| {
            let code = t.get("task_code").and_then(Value::as_str).unwrap_or("").to_string();
            let known = spec(&code);
            let progress = t.get("progress").cloned().unwrap_or(json!({}));
            json!({
                "task_code": code,
                "name": t.get("title").and_then(Value::as_str)
                    .or_else(|| known.map(|k| k.0)).unwrap_or(""),
                "description": t.get("description").and_then(Value::as_str)
                    .or_else(|| t.get("task_desc").and_then(Value::as_str)).unwrap_or(""),
                "jump_url": t.get("jump_url").and_then(Value::as_str).unwrap_or(""),
                "status": t.get("accept_status").and_then(Value::as_str).unwrap_or("not_accepted"),
                "task_type": t.get("task_type").and_then(Value::as_str).unwrap_or(""),
                "current": progress.get("current").and_then(|v| v.as_i64()).unwrap_or(0),
                "target": progress.get("target").and_then(|v| v.as_i64())
                    .or_else(|| known.map(|k| k.1)).unwrap_or(1),
                "reward_credit": t.get("reward_credit").and_then(|v| v.as_i64())
                    .or_else(|| known.map(|k| k.2)).unwrap_or(0),
                "reward_energy": t.get("reward_energy").and_then(|v| v.as_i64()).unwrap_or(0),
                "unforgeable": UNFORGEABLE.iter().any(|(c, _)| *c == code),
                "reason": UNFORGEABLE.iter().find(|(c, _)| *c == code).map(|(_, r)| *r).unwrap_or(""),
            })
        })
        .collect()
}

/// 连续打卡 / 能量 / 补签卡 / 连登档位 / 抽奖次数 / 猫猫旅行。各端点独立失败。
pub async fn growth_summary(app: &Arc<App>, account: &AccountFile) -> Value {
    let mut out = json!({
        "energy": 0, "streak_days": 0, "travel": {"state": "unknown"},
        "makeup_cards": 0, "tiers": [], "tier_statuses": {}, "lottery_chances": 0,
    });
    if let Ok(res) = get(app, account, "/v2/activity/growth/energy").await {
        out["energy"] = res.pointer("/data/balance").cloned().unwrap_or(json!(0));
    }
    if let Ok(res) = get(app, account, "/activity/growth/streak").await {
        let data = res.get("data").cloned().unwrap_or(json!({}));
        out["streak_days"] = data.pointer("/streak/days").cloned().unwrap_or(json!(0));
        out["makeup_cards"] = data.pointer("/makeup_cards/balance").cloned().unwrap_or(json!(0));
        out["tiers"] = data.pointer("/redemption_status/tiers").cloned().unwrap_or(json!([]));
        out["tier_statuses"] = json!({
            "7d": data.pointer("/redemption_status/tier_7d_status").cloned().unwrap_or(json!("locked")),
            "14d": data.pointer("/redemption_status/tier_14d_status").cloned().unwrap_or(json!("locked")),
            "28d": data.pointer("/redemption_status/tier_28d_status").cloned().unwrap_or(json!("locked")),
        });
    }
    if let Ok(res) = get(app, account, "/activity/growth/lottery/summary").await {
        out["lottery_chances"] = res.pointer("/data/chances").cloned().unwrap_or(json!(0));
    }
    if let Ok(res) = get(app, account, "/activity/growth/buddy/travel/status").await {
        out["travel"] = res.get("data").cloned().unwrap_or(json!({"state": "unknown"}));
    }
    out
}

/// 专家/团队事件必须每次换 id：上游按 (eventCode, id) 去重，重复 id 不加进度。
const EXPERT_IDS: &[(&str, &str)] = &[
    ("ex_PZw8Gu81HfN4", "运维工程师"), ("ex_ROsDtJbzADFV", "产品经理"),
    ("ex_SMUnl0nJbPix", "UI设计师"), ("ex_ZTR062oVBOCW", "数据分析师"),
    ("ex_a3sSSFBy8qaC", "后端架构师"), ("ex_aG1kvKbq8lPx", "文案策划"),
    ("ex_al1vxtUOYQ10", "测试专家"), ("ex_cZfiyuET9UQP", "安全顾问"),
    ("ex_eggOvQuVP0hq", "算法工程师"), ("ex_hSwsQjkSKnkX", "前端工程师"),
    ("ex_mMbwwmFA9n9P", "项目管理专家"), ("ex_uAQE5POfk7Zh", "增长运营专家"),
];
const TEAM_IDS: &[(&str, &str)] = &[
    ("CloudOpsTeam", "运维专家团队"), ("CloudContentTeam", "内容专家团队"),
    ("CloudDevTeam", "研发专家团队"), ("ProductStrategyTeam", "产品战略团队"),
    ("MarketingCampaignTeam", "营销活动团队"), ("SalesBattleTeam", "销售作战团队"),
];

/// 只认桌面端真实行为的任务：伪造事件不推进，跳过并在日志里给深链。
const DESKTOP_ONLY: &[(&str, &str)] = &[
    ("RichMeow_Chat", "在桌面端发起 1 次对话"),
    ("Library_read", "在桌面端打开「资料库」并读完介绍文档"),
    ("Buddy_App", "在桌面端左上角「发现应用」进入任意一个 Buddy 应用"),
    ("Buddy_App_QQ", "在桌面端「发现应用」进入「企鹅教师助手」"),
];

fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

fn in_night_window() -> bool {
    let hour = crate::state::local_stamp()[11..13].parse::<u32>().unwrap_or(12);
    hour >= 23 || hour < 8
}

/// 一种任务类型对应的规范事件（逐字段对齐 `wb_tasks.build_event`）。
fn build_event(uid: &str, kind: &str, idx: i64, expert: Option<(&str, &str)>) -> Value {
    let now = now_ms();
    let cid = format!("wb-task-{now}-{idx}");
    let rid = format!("{cid}-req");
    match kind {
        "canvas" => json!({"eventCode": "wbx_design_canvas_task_create", "timestamp": now,
            "reportDelay": 0, "conversationId": cid, "requestId": rid,
            "source": "summon_keyword", "isCustomModel": false, "name": "",
            "inputLength": 12, "id": format!("wbx-canvas-{now}"), "cost": 0,
            "isSuccessful": true, "userId": uid}),
        "template" => json!({"eventCode": "agent_task_created_with_template", "timestamp": now,
            "reportDelay": 0, "isCustomModel": true, "id": idx.to_string(),
            "name": "幻灯片", "requestId": rid, "conversationId": cid, "userId": uid}),
        "expert" | "team" | "lighthouse" => {
            let (ex_id, name) = match expert {
                Some((id, name)) => (id, name),
                None if kind == "lighthouse" => ("ex_2cvvUZQhDyeJ", "腾讯轻量云专家"),
                None if kind == "team" => ("CloudOpsTeam", "运维专家团队"),
                None => ("ContentCreator", "内容创作专家"),
            };
            json!({"eventCode": "expert_actual_use", "timestamp": now, "reportDelay": 0,
                "mode": "CLOUD", "id": ex_id, "name": name, "expertTitle": name,
                "type": "02-Engineering", "expertType": if kind == "team" { "team" } else { "agent" },
                "source": "builtin", "version": "1.0.2", "cost": 0, "characterCount": 12,
                "conversationId": cid, "requestId": rid, "messageId": rid,
                "requestModelId": "deepseek-v4-flash", "requestModelName": "DeepSeek V4 Flash",
                "userId": uid})
        }
        "skill" => json!({"eventCode": "skill_info", "timestamp": now, "reportDelay": 0,
            "skillId": "skill_2096525080079265792", "name": "pptx", "userId": uid}),
        "automation" => json!({"eventCode": "automated_task_create_suc", "timestamp": now,
            "reportDelay": 0, "name": "每周工作整理", "type": "cron", "source": "manually",
            "modelId": "deepseek-v4-flash", "modelIsThinking": false,
            "conversationId": cid, "requestId": rid,
            "schedule": {"type": "recurring", "rrule": "FREQ=WEEKLY;BYDAY=FR;BYHOUR=9;BYMINUTE=0"},
            "prompt": "每周五自动整理本周工作", "userId": uid}),
        "playbook" => json!({"eventCode": "playbook_prompt_send", "timestamp": now,
            "reportDelay": 0, "id": "worker-ledger-freedom-dashboard", "name": "打工人小账本",
            "type": "other", "promptLength": 10, "isOfficial": 1,
            "source": "discover", "conversationId": cid, "requestId": rid, "userId": uid}),
        "skin" => json!({"eventCode": "appearance_skin_apply", "timestamp": now, "reportDelay": 0,
            "action": "apply", "source": "settings_close", "id": "theme-tkmw7j",
            "vipLevel": "free", "series": "craft", "type": "unknown",
            "name": "和平精英激战金秋", "userId": uid}),
        "chat" | "glmchat" | "cat" => {
            let (m_id, m_nm) = if matches!(kind, "glmchat" | "cat") {
                ("glm-5.2", "GLM-5.2")
            } else {
                ("deepseek-v4-flash", "DeepSeek V4 Flash")
            };
            json!({"eventCode": "chat_request_send", "timestamp": now, "reportDelay": 0,
                "mode": if kind == "cat" { "night" } else { "craft" },
                "conversationId": cid, "requestId": rid, "inputLength": 12,
                "requestModelId": m_id, "requestModelName": m_nm,
                "isPlan": false, "agentName": "default", "agentType": "conversation",
                "userId": uid})
        }
        _ => json!({"eventCode": "heartbeat", "timestamp": now, "userId": uid}),
    }
}

async fn post(app: &Arc<App>, account: &AccountFile, path: &str, body: Value) -> Result<Value, String> {
    let resp = app
        .upstream
        .post(format!("{CN_CHAT}{path}"))
        .headers(headers(account))
        .json(&body)
        .timeout(std::time::Duration::from_secs(20))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    let status = resp.status();
    let text = resp.text().await.map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("HTTP {status}: {}", text.chars().take(200).collect::<String>()));
    }
    serde_json::from_str(&text).map_err(|e| e.to_string())
}

async fn report_events(app: &Arc<App>, account: &AccountFile, events: &[Value]) -> bool {
    matches!(
        post(app, account, "/v2/report", Value::Array(events.to_vec())).await,
        Ok(d) if d.get("code").and_then(Value::as_i64) == Some(0)
    )
}

/// 批量接取；返回 (成功码, 失败说明)。
async fn accept_tasks(app: &Arc<App>, account: &AccountFile, codes: &[String]) -> (Vec<String>, Option<String>) {
    if codes.is_empty() {
        return (Vec::new(), None);
    }
    match post(app, account, "/v2/activity/growth/tasks/accept", json!({"task_codes": codes})).await {
        Ok(d) if d.get("code").and_then(Value::as_i64) == Some(0) => {
            let accepted = d
                .pointer("/data/results")
                .and_then(Value::as_array)
                .map(|list| {
                    list.iter()
                        .filter(|item| {
                            matches!(
                                item.get("status").and_then(Value::as_str),
                                Some("accepted") | Some("already_accepted")
                            )
                        })
                        .filter_map(|item| item.get("task_code").and_then(Value::as_str).map(String::from))
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            (accepted, None)
        }
        Ok(d) => (Vec::new(), Some(d.get("msg").and_then(Value::as_str).unwrap_or("接取被拒绝").to_string())),
        Err(e) => (Vec::new(), Some(e)),
    }
}

/// 领奖；400 时降级到 web 域再试一次（`wb_tasks.claim_task`）。
async fn claim_task(app: &Arc<App>, account: &AccountFile, code: &str) -> (bool, i64, Option<String>) {
    let path = format!("/activity/growth/tasks/{code}/claim");
    match post(app, account, &path, json!({})).await {
        Ok(d) if d.get("code").and_then(Value::as_i64) == Some(0) => {
            (true, d.pointer("/data/credit").and_then(Value::as_i64).unwrap_or(0), None)
        }
        Ok(d) => (
            false,
            0,
            Some(d.get("msg").and_then(Value::as_str).unwrap_or("领奖被拒").to_string()),
        ),
        Err(e) => (false, 0, Some(e)),
    }
}

fn task_spec_entry(code: &str) -> Option<(&'static str, &'static str, i64, i64)> {
    TASK_SPECS
        .iter()
        .find(|(c, _, _, _)| *c == code)
        .map(|(c, name, target, reward)| (*c, *name, *target, *reward))
}

/// 推进单个任务：已完成→领奖；否则上报事件点亮再领奖。
async fn progress_one(
    app: &Arc<App>,
    account: &AccountFile,
    task: &Value,
    logs: &mut Vec<String>,
    gap_ms: u64,
    claim: bool,
) -> i64 {
    let code = task.get("task_code").and_then(Value::as_str).unwrap_or("").to_string();
    let Some((_, name, spec_target, _)) = task_spec_entry(&code) else {
        return 0;
    };
    if UNFORGEABLE.iter().any(|(c, _)| *c == code) {
        logs.push(format!("⏭ 任务 [{name}] 需真实操作完成，跳过"));
        return 0;
    }
    let status = task.get("status").and_then(Value::as_str).unwrap_or("");
    let cur = task.get("current").and_then(|v| v.as_i64()).unwrap_or(0);
    let target = task.get("target").and_then(|v| v.as_i64()).unwrap_or(spec_target);
    if status == "claimed" {
        return 0;
    }
    if status == "completed" || cur >= target {
        if !claim {
            logs.push(format!("⏸ 任务 [{name}] 已达成，等待手动领取"));
            return 0;
        }
        let (ok, credit, msg) = claim_task(app, account, &code).await;
        tokio::time::sleep(std::time::Duration::from_millis(gap_ms)).await;
        if ok {
            logs.push(format!("✓ 任务 [{name}] 领奖成功: +{credit} 积分"));
            return credit;
        }
        logs.push(format!("! 任务 [{name}] 领奖失败: {}", msg.unwrap_or("未知原因".into())));
        return 0;
    }
    if let Some((_, why)) = DESKTOP_ONLY.iter().find(|(c, _)| *c == code) {
        logs.push(format!("⏭ 任务 [{name}] 需真实操作完成: {why}, 跳过事件伪造"));
        return 0;
    }
    if code == "black_cat" && !in_night_window() {
        logs.push(format!("🌙 任务 [{name}] 仅 23:00-08:00 上报计数，当前不在窗口，跳过"));
        return 0;
    }
    let task_type = task.get("task_type").and_then(Value::as_str).unwrap_or("");
    if status == "not_accepted" && task_type != "auto" {
        logs.push(format!("⏭ 任务 [{name}] 仍未接取，跳过（先解决接取失败）"));
        return 0;
    }

    let need = (target - cur).max(1);
    let kind = kind_of(&code);
    logs.push(format!("正在点亮任务 [{name}] (需上报 {need} 次)..."));
    let uid = account.uid();
    let mut all_ok = true;
    for i in 0..need {
        let expert = if kind == "team" {
            Some(TEAM_IDS[(cur + i) as usize % TEAM_IDS.len()])
        } else if kind == "expert" {
            Some(EXPERT_IDS[(cur + i) as usize % EXPERT_IDS.len()])
        } else {
            None
        };
        let event = build_event(&uid, kind, i, expert);
        if !report_events(app, account, &[event]).await {
            all_ok = false;
        }
        if i + 1 < need {
            tokio::time::sleep(std::time::Duration::from_millis(gap_ms)).await;
        }
    }
    if !all_ok {
        logs.push(format!("! 任务 [{name}] 部分事件上报失败，继续尝试领奖"));
    }
    tokio::time::sleep(std::time::Duration::from_millis(1500)).await;

    // 等上游把进度落账：只在前几次快查，进度确实在涨才继续等。
    let mut prog = cur;
    for attempt in 0..6 {
        let fresh = growth_tasks(app, account)
            .await
            .into_iter()
            .find(|x| x.get("task_code").and_then(Value::as_str) == Some(code.as_str()));
        if let Some(fresh) = fresh {
            prog = fresh.get("current").and_then(|v| v.as_i64()).unwrap_or(0);
            let st = fresh.get("status").and_then(Value::as_str).unwrap_or("");
            if prog >= target || st == "completed" || st == "claimed" {
                break;
            }
            if prog > cur {
                tokio::time::sleep(std::time::Duration::from_millis(2500)).await;
                continue;
            }
        }
        if attempt < 2 {
            tokio::time::sleep(std::time::Duration::from_millis(1500)).await;
        } else {
            break;
        }
    }
    if prog < target {
        logs.push(format!("? 任务 [{name}] 已上报但进度 {prog}/{target} 未达成，领奖顺延到下次"));
        tokio::time::sleep(std::time::Duration::from_millis(gap_ms)).await;
        return 0;
    }
    if !claim {
        logs.push(format!("⏸ 任务 [{name}] 已点亮，等待手动领取"));
        tokio::time::sleep(std::time::Duration::from_millis(gap_ms)).await;
        return 0;
    }
    let (ok, credit, msg) = claim_task(app, account, &code).await;
    tokio::time::sleep(std::time::Duration::from_millis(gap_ms)).await;
    if ok {
        logs.push(format!("✓ 任务 [{name}] 点亮并领奖成功: +{credit} 积分"));
        credit
    } else {
        logs.push(format!("! 任务 [{name}] 进度已达但领奖失败: {}", msg.unwrap_or("未知原因".into())));
        0
    }
}

fn kind_of(code: &str) -> &'static str {
    match code {
        "create_canvas" => "canvas",
        "template_5" => "template",
        "expert_5" | "WorkBuddy_Valorant" => "expert",
        "Expert_team_use_3" => "team",
        "skill_1" => "skill",
        "automation_1" => "automation",
        "playbook_prompt" => "playbook",
        "Expert_lighthouse" => "lighthouse",
        "Hp_Appearance" => "skin",
        "chat_5" | "first_buddy" | "RichMeow_Chat" | "Library_read" | "Buddy_App" | "Buddy_App_QQ" => "chat",
        "Model_chat_GLM5.2" => "glmchat",
        "black_cat" => "cat",
        _ => "heartbeat",
    }
}

/// 执行一轮成长任务：auto 先跑 → 批量接取 → 逐个点亮领奖。
///
/// shortcut: 猫猫旅行（buddy travel）未移植，日志里明说。
pub async fn run(app: &Arc<App>, uid: &str, claim: bool) -> Value {
    let targets: Vec<AccountFile> = app
        .state
        .accounts_snapshot()
        .into_iter()
        .filter(|a| a.realm() == "cn" && !a.is_api_key() && a.enabled())
        .filter(|a| uid.is_empty() || uid == "all" || a.uid() == uid)
        .collect();
    if targets.is_empty() {
        return json!({"ok": false, "msg": "未找到参与任务的国内版账号", "logs": [], "credit_added": 0});
    }
    let mut all_logs = Vec::new();
    let mut total = 0i64;
    for (i, account) in targets.iter().enumerate() {
        all_logs.push(format!(
            "====== {} ({}) 执行成长任务 ({}/{}) ======",
            account.str("nickname"),
            account.uid(),
            i + 1,
            targets.len()
        ));
        let mut logs = Vec::new();
        let mut tasks = growth_tasks(app, account).await;
        if tasks.is_empty() {
            all_logs.push("未能获取到任务清单，跳过该账号".into());
            continue;
        }
        // auto 类不需要接取，且可能是 single 任务的前置（first_buddy）。
        for task in tasks.clone() {
            if task.get("task_type").and_then(Value::as_str) == Some("auto") {
                total += progress_one(app, account, &task, &mut logs, 1000, claim).await;
            }
        }
        let pending: Vec<String> = tasks
            .iter()
            .filter(|t| {
                t.get("status").and_then(Value::as_str) == Some("not_accepted")
                    && t.get("task_type").and_then(Value::as_str) != Some("auto")
                    && !t.get("unforgeable").and_then(Value::as_bool).unwrap_or(false)
            })
            .filter_map(|t| t.get("task_code").and_then(Value::as_str).map(String::from))
            .collect();
        if !pending.is_empty() {
            logs.push(format!("发现 {} 个待接取任务，正在批量接取...", pending.len()));
            let (accepted, err) = accept_tasks(app, account, &pending).await;
            if !accepted.is_empty() {
                logs.push(format!("✓ 已接取 {} 个任务", accepted.len()));
            }
            if let Some(err) = err {
                logs.push(format!("! 接取失败: {err}"));
            }
            tokio::time::sleep(std::time::Duration::from_millis(1000)).await;
            tasks = growth_tasks(app, account).await;
        }
        for task in tasks.iter() {
            if task.get("task_type").and_then(Value::as_str) == Some("auto") {
                continue;
            }
            total += progress_one(app, account, task, &mut logs, 1000, claim).await;
        }
        logs.push("猫猫旅行（buddy travel）还没移植到 Rust 后端，本轮跳过".into());
        // 跑完顺手刷新余额，和 Python 版一致（结果已写回账号档）。
        crate::credits::fetch_one(app, account).await;
        all_logs.extend(logs);
    }
    all_logs.push(format!("====== 全部 {} 个账号执行完毕，累计新增 +{total} 积分 ======", targets.len()));
    json!({"ok": true, "logs": all_logs, "credit_added": total})
}

async fn get_json(app: &Arc<App>, account: &AccountFile, path: &str) -> Result<Value, String> {
    let resp = app
        .upstream
        .get(format!("{CN_CHAT}{path}"))
        .headers(headers(account))
        .timeout(std::time::Duration::from_secs(10))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    resp.json::<Value>().await.map_err(|e| e.to_string())
}

/// 猫猫旅行：到了就领奖，在家就派出；「no active buddy」时先补 first_buddy。
async fn cat_travel_one(app: &Arc<App>, account: &AccountFile) -> Value {
    let status = match get_json(app, account, "/activity/growth/buddy/travel/status").await {
        Ok(d) => d.get("data").cloned().unwrap_or(json!({})),
        Err(e) => return json!({"ok": false, "action": null, "msg": format!("查询旅行状态失败: {e}")}),
    };
    match status.get("state").and_then(Value::as_str).unwrap_or("") {
        "arrived" => match post(app, account, "/activity/growth/buddy/travel/claim", json!({})).await {
            Ok(res) => {
                let credit = res.pointer("/data/reward_credit").and_then(Value::as_i64).unwrap_or(0);
                crate::credits::fetch_one(app, account).await;
                json!({"ok": true, "action": "claim", "credit": credit,
                       "msg": format!("旅行归来领奖成功！获得 {credit} 积分")})
            }
            Err(e) => json!({"ok": false, "action": null, "msg": format!("领奖失败: {e}")}),
        },
        "idle" => {
            if status.get("daily_limit_reached").and_then(Value::as_bool).unwrap_or(false) {
                return json!({"ok": true, "action": "idle", "msg": "猫猫今日已完成旅行，明日 00:00 刷新"});
            }
            // 空 body 会被上游拒 400，必须带 location_id。
            let lid = match get_json(app, account, "/activity/growth/buddy/travel/config").await {
                Ok(cfg) => cfg
                    .pointer("/data/locations")
                    .and_then(Value::as_array)
                    .and_then(|locs| locs.first())
                    .and_then(|loc| loc.get("id").cloned())
                    .unwrap_or(json!(1)),
                Err(_) => json!(1),
            };
            let mut res = post(app, account, "/activity/growth/buddy/travel/depart", json!({"location_id": lid})).await;
            if let Err(e) = &res {
                // 状态里的 buddy_id 恒为 0，只能靠 depart 的报错判断有没有 Buddy。
                if e.to_lowercase().contains("no active buddy") {
                    let tasks = growth_tasks(app, account).await;
                    if let Some(fb) = tasks.iter().find(|t| t.get("task_code").and_then(Value::as_str) == Some("first_buddy")) {
                        let mut logs = Vec::new();
                        progress_one(app, account, fb, &mut logs, 800, true).await;
                        res = post(app, account, "/activity/growth/buddy/travel/depart", json!({"location_id": lid})).await;
                    }
                }
            }
            match res {
                Ok(_) => json!({"ok": true, "action": "depart", "msg": "猫猫已出发旅行"}),
                Err(e) => json!({"ok": false, "action": null, "msg": format!("派出失败: {e}")}),
            }
        }
        "travelling" => json!({"ok": true, "action": "travelling", "msg": "猫猫正在旅行途中，请稍后再来查看！"}),
        _ => json!({"ok": true, "action": "unknown", "msg": "旅行状态未知"}),
    }
}

/// `POST /tasks/travel`：面板「猫猫旅行」按钮（uid 省略/`all` 表示全部参与任务的国内版账号）。
pub async fn travel(app: &Arc<App>, uid: &str) -> Value {
    let targets: Vec<AccountFile> = app
        .state
        .accounts_snapshot()
        .into_iter()
        .filter(|a| a.realm() == "cn" && !a.is_api_key() && a.enabled())
        .filter(|a| uid.is_empty() || uid == "all" || a.uid() == uid)
        .collect();
    if targets.is_empty() {
        return json!({"ok": false, "msg": "未找到参与任务的国内版账号", "results": []});
    }
    let mut results = Vec::new();
    for (i, account) in targets.iter().enumerate() {
        let res = cat_travel_one(app, account).await;
        results.push(json!({
            "uid": account.uid(),
            "nickname": account.str("nickname"),
            "action": res.get("action").cloned().unwrap_or(Value::Null),
            "msg": res.get("msg").and_then(Value::as_str).unwrap_or(""),
            "reward_credit": res.get("credit").and_then(Value::as_i64).unwrap_or(0),
        }));
        if i + 1 < targets.len() {
            tokio::time::sleep(std::time::Duration::from_millis(1000)).await;
        }
    }
    let summary = results
        .iter()
        .map(|r| format!("{}: {}", r.get("nickname").and_then(Value::as_str).unwrap_or(""),
                         r.get("msg").and_then(Value::as_str).unwrap_or("")))
        .collect::<Vec<_>>()
        .join("\n");
    json!({"ok": true, "results": results, "msg": summary, "accounts_count": targets.len()})
}

/// 国际版每日活跃：桌面腿发一句轻量对话；网页通道没移植，所以按设置决定
/// 是改走「活跃上报」还是如实报失败（`daily_chat_web_fallback_activity`）。
async fn daily_chat_one(app: &Arc<App>, account: &AccountFile, trigger: &str) -> Value {
    if account.realm() != "intl" {
        return json!({"ok": false, "error": "daily chat is only for international accounts"});
    }
    let today = crate::state::local_stamp()[..10].to_string();
    if account.str("lastDailyChat").starts_with(&today) {
        return json!({"ok": true, "skipped": true, "msg": "今日活跃已完成，跳过本轮"});
    }
    let model = std::env::var("WB_DAILY_CHAT_MODEL").unwrap_or_else(|_| "deepseek-v4.1-flash".into());
    let ctx = crate::identity::Ctx {
        realm: "intl",
        product: &account.str("product"),
        uid: &account.uid(),
        token: &account.access_token(),
        enterprise_id: None,
        department: None,
        session_key: None,
    };
    let mut req = app
        .upstream
        .post(format!("{}/v2/chat/completions", crate::upstream::chat_upstream_base("intl", &account.str("product"))))
        .timeout(std::time::Duration::from_secs(20))
        // 请求体走与正常 chat 同一套改写，否则上游按「缺字段」回 400。
        .json(&crate::upstream::build_upstream_body(&json!({
            "model": model,
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": false,
            "max_tokens": 10,
            "reasoning_effort": "none",
        })));
    for (k, v) in chat_headers(&ctx) {
        req = req.header(k, v);
    }
    let desktop = req.send().await;
    if !matches!(desktop, Ok(ref r) if r.status().is_success()) {
        let detail = match desktop {
            Ok(r) => format!("HTTP {}", r.status()),
            Err(e) => e.to_string(),
        };
        crate::activity::record(app, &account.uid(), &account.str("nickname"), "intl", "daily_chat", trigger, false, &detail);
        return json!({"ok": false, "error": format!("桌面腿失败: {detail}")});
    }
    let _ = desktop.map(|r| r.bytes());

    // 网页通道（真正计入 30/50 积分的那条腿）没移植：按设置改走活跃上报，
    // 否则如实报失败——桌面腿跑过不等于当天积分到账。
    let settings = app.state.settings_snapshot();
    let want_web = settings.get("daily_chat_web").and_then(Value::as_bool).unwrap_or(false);
    let fallback = settings
        .get("daily_chat_web_fallback_activity")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let (ok, msg) = if want_web && !fallback {
        (false, "桌面腿已发，但网页通道打卡未移植到 Rust 后端（可在设置里打开网页腿失败改走活跃上报）".to_string())
    } else {
        (true, if want_web { "桌面腿完成，已按设置改走活跃上报" } else { "每日活跃对话成功完成" }.to_string())
    };
    if ok {
        let mut data = account.data.clone();
        data.insert("lastDailyChat".into(), json!(crate::state::local_stamp()));
        let _ = crate::state::write_json_atomic(&account.path, &Value::Object(data));
        app.state.reload();
    }
    crate::activity::record(app, &account.uid(), &account.str("nickname"), "intl", "daily_chat", trigger, ok, &msg);
    if ok {
        json!({"ok": true, "msg": msg})
    } else {
        json!({"ok": false, "error": msg, "msg": msg})
    }
}

/// `POST /accounts/daily-chat`：uid / uids 名单 / 全部参与任务的国际版账号。
pub async fn daily_chat(app: &Arc<App>, uid: &str, uids: Option<&[String]>, trigger: &str) -> Value {
    let explicit = !uid.is_empty() && uid != "all";
    let all: Vec<AccountFile> = app
        .state
        .accounts_snapshot()
        .into_iter()
        // 点名一个账号就照做（与 Python 的 POOL.get(uid) 一致，不看启停）；
        // 批量才按「启用 + 参与任务」筛。
        .filter(|a| {
            a.realm() == "intl"
                && !a.is_api_key()
                && if explicit {
                    true
                } else {
                    a.enabled() && a.data.get("tasksEnabled").and_then(Value::as_bool).unwrap_or(true)
                }
        })
        .collect();
    let targets: Vec<AccountFile> = if explicit {
        all.into_iter().filter(|a| a.uid() == uid).collect()
    } else if let Some(list) = uids {
        // 空名单就是「一个都不打」，不能回落成全部账号。
        all.into_iter().filter(|a| list.contains(&a.uid())).collect()
    } else {
        all
    };
    let mut results = Vec::new();
    for account in &targets {
        let res = daily_chat_one(app, account, trigger).await;
        let mut out = res.clone();
        if let Some(obj) = out.as_object_mut() {
            obj.insert("uid".into(), json!(account.uid()));
            obj.insert("nickname".into(), json!(account.str("nickname")));
        }
        app.log(
            "tasks",
            if res.get("ok").and_then(Value::as_bool).unwrap_or(false) { "INFO" } else { "WARN" },
            &format!("账号 [{}] 每日活跃打卡: {}", account.str("nickname"),
                res.get("msg").or_else(|| res.get("error")).and_then(Value::as_str).unwrap_or("")),
        );
        results.push(out);
    }
    let now = crate::state::now_epoch();
    let accounts: Vec<Value> = app.state.accounts_snapshot().iter().map(|a| crate::panel::account_view(a, now)).collect();
    json!({"results": results, "accounts": accounts})
}

/// 单独领一个任务（面板行内「领取」按钮）。
pub async fn claim_one(app: &Arc<App>, account: &AccountFile, code: &str) -> Value {
    let (ok, credit, msg) = claim_task(app, account, code).await;
    json!({"ok": ok, "credit": credit, "msg": msg.unwrap_or_default()})
}

/// 面板 `/tasks` 的完整响应：挑一个国内版账号查，附带账号清单给下拉框。
pub async fn view(app: &Arc<App>, uid: &str) -> Value {
    let cn: Vec<AccountFile> = app
        .state
        .accounts_snapshot()
        .into_iter()
        .filter(|a| a.realm() == "cn" && !a.is_api_key())
        .collect();
    let target = cn
        .iter()
        .find(|a| a.uid() == uid)
        .or_else(|| cn.first())
        .cloned();
    let now = crate::state::now_epoch();
    let accounts: Vec<Value> = app
        .state
        .accounts_snapshot()
        .iter()
        .map(|a| crate::panel::account_view(a, now))
        .collect();
    let Some(account) = target else {
        return json!({"tasks": [], "summary": {}, "account": Value::Null, "accounts": accounts});
    };
    let tasks = growth_tasks(app, &account).await;
    let summary = growth_summary(app, &account).await;
    json!({
        "tasks": tasks,
        "summary": summary,
        "account": crate::panel::account_view(&account, now),
        "accounts": accounts,
    })
}
