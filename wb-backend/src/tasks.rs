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
