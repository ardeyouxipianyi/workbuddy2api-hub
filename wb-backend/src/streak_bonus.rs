//! `POST /tasks/streak-bonus`: makeup, gift claims, streak tiers and lottery.
//!
//! Bounds are environment-backed. Defaults/examples:
//! - `WB_STREAK_REQUEST_TIMEOUT_SECONDS=10`
//! - `WB_STREAK_DRAW_DELAY_MS=1000`, `WB_STREAK_ACCOUNT_DELAY_MS=1000`
//! - `WB_STREAK_MAX_DRAWS=100` per account, a fail-safe against bad upstream data

use std::sync::Arc;

use hyper::body::Incoming;
use hyper::{Request, StatusCode};
use serde_json::{json, Map, Value};

use crate::http::{json_resp, Resp};
use crate::state::AccountFile;
use crate::App;

fn env_u64(name: &str, default: u64) -> u64 {
    std::env::var(name)
        .ok()
        .and_then(|value| value.trim().parse().ok())
        .unwrap_or(default)
}

fn chat_base(account: &AccountFile) -> String {
    crate::upstream::chat_upstream_base(&account.realm(), &account.product())
}

fn headers(account: &AccountFile) -> Vec<(String, String)> {
    let data = Value::Object(account.data.clone());
    let enterprise = crate::identity::enterprise_of(&data);
    let department = crate::identity::department_of(&data);
    crate::identity::chat_headers(&crate::identity::Ctx {
        realm: "cn",
        product: &account.product(),
        uid: &account.uid(),
        token: &account.access_token(),
        enterprise_id: enterprise,
        department,
        session_key: None,
    })
}

async fn chat_get(app: &Arc<App>, account: &AccountFile, path: &str) -> Result<Value, String> {
    let mut request = app
        .upstream
        .get(format!("{}{path}", chat_base(account)))
        .timeout(std::time::Duration::from_secs(env_u64(
            "WB_STREAK_REQUEST_TIMEOUT_SECONDS",
            10,
        )));
    for (name, value) in headers(account) {
        request = request.header(name, value);
    }
    let response = request.send().await.map_err(|error| error.to_string())?;
    response
        .error_for_status()
        .map_err(|error| error.to_string())?
        .json::<Value>()
        .await
        .map_err(|error| error.to_string())
}

async fn chat_post(
    app: &Arc<App>,
    account: &AccountFile,
    path: &str,
    body: Value,
) -> Result<Value, String> {
    let mut request = app
        .upstream
        .post(format!("{}{path}", chat_base(account)))
        .timeout(std::time::Duration::from_secs(env_u64(
            "WB_STREAK_REQUEST_TIMEOUT_SECONDS",
            10,
        )))
        .json(&body);
    for (name, value) in headers(account) {
        request = request.header(name, value);
    }
    let response = request.send().await.map_err(|error| error.to_string())?;
    response
        .error_for_status()
        .map_err(|error| error.to_string())?
        .json::<Value>()
        .await
        .map_err(|error| error.to_string())
}

fn uuid_v4() -> String {
    let mut value: Vec<char> = crate::sse::hex_token(32).chars().collect();
    value[12] = '4';
    value[16] = '8';
    let text: String = value.into_iter().collect();
    format!(
        "{}-{}-{}-{}-{}",
        &text[0..8],
        &text[8..12],
        &text[12..16],
        &text[16..20],
        &text[20..32]
    )
}

async fn makeup_yesterday(app: &Arc<App>, account: &AccountFile, logs: &mut Vec<String>) {
    let Ok(heatmap) = chat_get(app, account, "/activity/growth/heatmap").await else {
        return;
    };
    let yesterday = crate::state::local_stamp_for(crate::state::now_epoch() - 86_400);
    let yesterday = &yesterday[..10];
    let missing = heatmap
        .pointer("/data/cells")
        .and_then(Value::as_array)
        .and_then(|cells| {
            cells
                .iter()
                .find(|cell| cell.get("date").and_then(Value::as_str) == Some(yesterday))
        })
        .and_then(|cell| cell.get("score"))
        .and_then(Value::as_f64)
        .unwrap_or(1.0)
        == 0.0;
    if !missing {
        return;
    }
    let Ok(streak) = chat_get(app, account, "/activity/growth/streak").await else {
        return;
    };
    let cards = streak
        .pointer("/data/makeup_cards/balance")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    if cards > 0
        && chat_post(
            app,
            account,
            "/activity/growth/makeup-cards/use",
            json!({"target_date": yesterday}),
        )
        .await
        .is_ok()
    {
        logs.push(format!("已消耗 1 张补签卡补签昨日 ({yesterday})"));
    }
}

async fn claim_gifts(app: &Arc<App>, account: &AccountFile, logs: &mut Vec<String>) {
    for (path, name) in [
        ("/billing/meter/claim-gift", "新手礼包"),
        ("/billing/meter/claim-compensation", "活动补偿"),
    ] {
        let Ok(value) = crate::credits::post(app, account, path, &json!({})).await else {
            continue;
        };
        if value.get("code").and_then(Value::as_i64) == Some(0) {
            let credit = value
                .pointer("/data/credit")
                .and_then(Value::as_i64)
                .unwrap_or(0);
            logs.push(format!("领取{name} +{credit}积分"));
        }
    }
}

async fn redeem_tiers(app: &Arc<App>, account: &AccountFile, logs: &mut Vec<String>) {
    let Ok(streak) = chat_get(app, account, "/activity/growth/streak").await else {
        return;
    };
    let Some(redemption) = streak.pointer("/data/redemption_status") else {
        return;
    };
    let statuses = [
        ("7d", "tier_7d_status"),
        ("14d", "tier_14d_status"),
        ("28d", "tier_28d_status"),
    ];
    for tier in redemption
        .get("tiers")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let Some(name) = tier.get("tier").and_then(Value::as_str) else {
            continue;
        };
        let status = statuses
            .iter()
            .find(|(key, _)| *key == name)
            .and_then(|(_, key)| redemption.get(*key))
            .and_then(Value::as_str)
            .unwrap_or("");
        if matches!(status, "locked" | "claimed") {
            continue;
        }
        let result = chat_post(
            app,
            account,
            "/activity/growth/redeem",
            json!({"tier": name, "client_token": uuid_v4()}),
        )
        .await;
        if result
            .ok()
            .and_then(|value| value.get("code").and_then(Value::as_i64))
            == Some(0)
        {
            logs.push(format!("成功兑换连登 {name} 档位"));
        }
    }
}

async fn draw_lottery(app: &Arc<App>, account: &AccountFile, logs: &mut Vec<String>) {
    let Ok(summary) = chat_get(app, account, "/activity/growth/lottery/summary").await else {
        return;
    };
    let chances = summary
        .pointer("/data/chances")
        .and_then(Value::as_u64)
        .unwrap_or(0)
        .min(env_u64("WB_STREAK_MAX_DRAWS", 100));
    let delay = env_u64("WB_STREAK_DRAW_DELAY_MS", 1_000);
    let mut drawn = 0;
    for index in 0..chances {
        let result = chat_post(
            app,
            account,
            "/activity/growth/lottery/draw",
            json!({"client_token": uuid_v4()}),
        )
        .await;
        if result
            .ok()
            .and_then(|value| value.get("code").and_then(Value::as_i64))
            != Some(0)
        {
            break;
        }
        drawn += 1;
        if index + 1 < chances {
            tokio::time::sleep(std::time::Duration::from_millis(delay)).await;
        }
    }
    if drawn > 0 {
        logs.push(format!("完成成长抽奖 {drawn} 次"));
    }
}

async fn run_account(app: &Arc<App>, account: &AccountFile) -> String {
    if account.realm() != "cn" {
        return "连登管家仅限国内版账号".to_string();
    }
    if crate::identity::enterprise_of(&Value::Object(account.data.clone())).is_some() {
        return "企业版账号无成长体系，跳过".to_string();
    }
    let mut logs = Vec::new();
    makeup_yesterday(app, account, &mut logs).await;
    claim_gifts(app, account, &mut logs).await;
    redeem_tiers(app, account, &mut logs).await;
    draw_lottery(app, account, &mut logs).await;
    if logs.is_empty() {
        "连登状态正常（无需兑换/无抽奖次数）".to_string()
    } else {
        logs.join("; ")
    }
}

fn targets(app: &App, body: &Map<String, Value>) -> Result<Vec<AccountFile>, String> {
    let uid = body.get("uid").and_then(Value::as_str).unwrap_or("");
    let all = app.state.accounts_snapshot();
    if uid.is_empty() || uid == "all" {
        let selected: Vec<_> = all
            .into_iter()
            .filter(|account| {
                account.realm() == "cn"
                    && account.enabled()
                    && account
                        .data
                        .get("tasksEnabled")
                        .and_then(Value::as_bool)
                        .unwrap_or(true)
            })
            .collect();
        return if selected.is_empty() {
            Err("未找到参与任务的国内版账号".into())
        } else {
            Ok(selected)
        };
    }
    all.into_iter()
        .find(|account| account.uid() == uid && account.realm() == "cn")
        .map(|account| vec![account])
        .ok_or_else(|| "未找到指定的国内版账号".into())
}

pub async fn handle(app: &Arc<App>, req: Request<Incoming>) -> Resp {
    let body = match crate::http::body_json(req).await {
        Ok(Value::Object(body)) => body,
        Ok(_) => Map::new(),
        Err(response) => return response,
    };
    let accounts = match targets(app, &body) {
        Ok(accounts) => accounts,
        Err(message) => return json_resp(StatusCode::OK, &json!({"ok": false, "msg": message})),
    };
    let delay = env_u64("WB_STREAK_ACCOUNT_DELAY_MS", 1_000);
    let mut results = Vec::with_capacity(accounts.len());
    for (index, account) in accounts.iter().enumerate() {
        let uid = account.uid();
        let nickname = account
            .data
            .get("nickname")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .unwrap_or(&uid[..uid.len().min(8)])
            .to_string();
        let message = run_account(app, account).await;
        app.log(
            "tasks",
            "INFO",
            &format!("账号 [{nickname}] 连登管家与抽奖: {message}"),
        );
        results.push(format!("{nickname}: {message}"));
        if index + 1 < accounts.len() {
            tokio::time::sleep(std::time::Duration::from_millis(delay)).await;
        }
    }
    json_resp(
        StatusCode::OK,
        &json!({"ok": true, "msg": results.join("\n")}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_pool_returns_python_handler_message() {
        let root = std::env::temp_dir().join(format!(
            "wb-streak-{}-{}",
            std::process::id(),
            crate::sse::hex_token(8)
        ));
        let accounts = root.join("accounts");
        let usage = root.join("usage");
        std::fs::create_dir_all(&accounts).unwrap();
        std::fs::create_dir_all(&usage).unwrap();
        let state = crate::state::State::load(accounts.to_str().unwrap(), usage.to_str().unwrap());
        let app = App {
            state,
            upstream: reqwest::Client::new(),
            started: std::time::Instant::now(),
            listen_port: 0,
            inflight: Default::default(),
            recorder: crate::usage::Recorder::new(usage.to_string_lossy().to_string()),
            pool: crate::pool::Pool::new(),
            panel: crate::panel::Panel::default(),
            pricing: crate::pricing::Pricing::new(&usage),
            scheduler: crate::scheduler::Scheduler::default(),
            updates: Arc::new(crate::updates::Manager::new(
                accounts.to_string_lossy().as_ref(),
            )),
            logs: Default::default(),
            slots: crate::slots::Slots::from_env(0),
        };
        let error = match targets(&app, &Map::new()) {
            Ok(_) => panic!("empty account pool unexpectedly had targets"),
            Err(error) => error,
        };
        assert_eq!(error, "未找到参与任务的国内版账号");
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn generated_compaction_style_uuid_is_v4() {
        let value = uuid_v4();
        assert_eq!(&value[14..15], "4");
        assert_eq!(&value[19..20], "8");
        assert_eq!(value.len(), 36);
    }
}
