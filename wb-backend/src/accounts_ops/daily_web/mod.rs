//! Web-console daily activity (`wb_accounts.daily_chat_web`).

mod acp;
mod conversation;

use std::sync::Arc;
use std::time::Duration;

use hyper::body::Incoming;
use hyper::{Request, StatusCode};
use serde_json::{json, Value};

use super::{config, util};
use crate::http::{body_json, json_resp, Resp};
use crate::state::AccountFile;
use crate::App;

pub(super) async fn handle(app: &Arc<App>, req: Request<Incoming>) -> Resp {
    let body = match body_json(req).await {
        Ok(value) => value,
        Err(resp) => return resp,
    };
    let body = util::object_or_empty(body);
    let uid = body.get("uid").and_then(Value::as_str).unwrap_or("");
    let targets = if uid.is_empty() {
        util::target_accounts(app, "", "intl", true)
            .into_iter()
            .filter(|account| account.realm() == "intl" && !account.is_api_key())
            .collect()
    } else {
        util::target_accounts(app, uid, "", false)
    };
    let prompt = config::daily_prompt();
    let models = configured_models(app);
    let mut results = Vec::new();
    for account in targets {
        let mut result = run_account(app, &account, &prompt, &models).await;
        if let Some(object) = result.as_object_mut() {
            object.insert("uid".into(), json!(account.uid()));
            object.insert("nickname".into(), json!(account.str("nickname")));
        }
        let ok = result.get("ok").and_then(Value::as_bool).unwrap_or(false);
        let detail = result
            .get("msg")
            .or_else(|| result.get("error"))
            .and_then(Value::as_str)
            .unwrap_or("");
        app.log(
            "tasks",
            if ok { "INFO" } else { "WARN" },
            &format!("账号 [{}] 网页通道打卡: {detail}", util::nickname(&account)),
        );
        results.push(result);
    }
    let now = crate::state::now_epoch();
    let accounts: Vec<Value> = app
        .state
        .accounts_snapshot()
        .iter()
        .map(|account| crate::panel::account_view(app, account, now))
        .collect();
    json_resp(
        StatusCode::OK,
        &json!({"results": results, "accounts": accounts}),
    )
}

fn configured_models(app: &App) -> Vec<String> {
    let configured = app
        .state
        .settings_snapshot()
        .get("daily_chat_models")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::trim)
                .filter(|item| !item.is_empty())
                .map(str::to_string)
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    if configured.is_empty() {
        vec![config::default_model()]
    } else {
        configured
    }
}

async fn run_account(
    app: &Arc<App>,
    account: &AccountFile,
    prompt: &str,
    models: &[String],
) -> Value {
    if account.realm() != "intl" {
        return json!({"ok": false, "error": "web daily chat is only for international accounts"});
    }
    if account
        .str("lastDailyChat")
        .starts_with(&crate::state::local_stamp()[..10])
    {
        return json!({"ok": true, "skipped": true, "msg": "今日活跃已完成，跳过网页活跃"});
    }
    let client = account_client(app, account);
    let result = run_attempts(&client, account, prompt, models, rounds(), wait()).await;
    let ok = result.get("ok").and_then(Value::as_bool) == Some(true);
    let message = if ok {
        result.get("msg").and_then(Value::as_str).unwrap_or("")
    } else {
        result
            .get("error")
            .and_then(Value::as_str)
            .unwrap_or("网页通道打卡失败")
    };
    record(app, account, ok, message);
    result
}

fn rounds() -> usize {
    std::env::var("WB_WEB_MIN_ROUNDS")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(2)
        .max(1)
}

fn wait() -> u64 {
    std::env::var("WB_WEB_TURN_TIMEOUT")
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .unwrap_or(120)
        .max(1)
}

async fn run_attempts(
    client: &reqwest::Client,
    account: &AccountFile,
    prompt: &str,
    models: &[String],
    rounds: usize,
    wait: u64,
) -> Value {
    let mut tried = Vec::new();
    let mut last = Value::Null;
    for index in 0..rounds {
        let model = models[index % models.len()].clone();
        tried.push(model.clone());
        let mut result = attempt(client, account, prompt, &model, wait).await;
        if let Some(object) = result.as_object_mut() {
            object.insert("attempts".into(), json!(index + 1));
            object.insert("models_tried".into(), json!(tried));
            object.insert("model".into(), json!(model));
        }
        if result.get("ok").and_then(Value::as_bool) == Some(true) {
            return result;
        }
        let retryable = result
            .get("retryable")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        last = result;
        if !retryable || index + 1 >= rounds {
            break;
        }
        tokio::time::sleep(retry_delay(index)).await;
    }
    last
}

fn retry_delay(round: usize) -> Duration {
    let base = std::env::var("WB_WEB_RETRY_BACKOFF")
        .ok()
        .and_then(|value| value.parse::<f64>().ok())
        .unwrap_or(5.0)
        .max(0.0);
    let cap = std::env::var("WB_WEB_RETRY_BACKOFF_MAX")
        .ok()
        .and_then(|value| value.parse::<f64>().ok())
        .unwrap_or(30.0)
        .max(base);
    Duration::from_secs_f64((base * 2f64.powi(round as i32)).min(cap))
}

fn record(app: &App, account: &AccountFile, ok: bool, message: &str) {
    crate::activity::record(
        app,
        &account.uid(),
        &util::nickname(account),
        "intl",
        "daily_chat",
        "manual",
        ok,
        message,
    );
}

fn account_client(app: &Arc<App>, account: &AccountFile) -> reqwest::Client {
    let proxy = account.str("proxy").trim();
    if proxy.is_empty() {
        return app.upstream.clone();
    }
    reqwest::Proxy::all(proxy)
        .ok()
        .and_then(|proxy| {
            reqwest::Client::builder()
                .proxy(proxy)
                .timeout(config::timeout("WB_WEB_REQUEST_TIMEOUT_SECONDS", 30))
                .build()
                .ok()
        })
        .unwrap_or_else(|| app.upstream.clone())
}

async fn attempt(
    client: &reqwest::Client,
    account: &AccountFile,
    prompt: &str,
    model: &str,
    wait: u64,
) -> Value {
    let conversation = match conversation::create(client, account, prompt, model).await {
        Ok(value) => value,
        Err(error) => return create_failure(error),
    };
    let session = match conversation::session(client, account, &conversation).await {
        Ok(session) => session,
        Err(error) => {
            conversation::release(client, account, &conversation, true).await;
            return session_failure(conversation, format!("session 查询失败: {error}"));
        }
    };
    let parts = match SessionParts::from(&session, &conversation) {
        Some(parts) => parts,
        None => {
            conversation::release(client, account, &conversation, true).await;
            return session_failure(conversation, "沙箱未就绪（没有 link/token）".into());
        }
    };
    let uid = account.uid();
    let result = acp::run_turn(
        client,
        &parts.link,
        &parts.token,
        &parts.session_id,
        &parts.cwd,
        prompt,
        model,
        &uid,
        wait,
        &conversation::url(&conversation),
    )
    .await;
    let ok = result.get("ok").and_then(Value::as_bool).unwrap_or(false);
    conversation::release(client, account, &conversation, !ok).await;
    finish_turn(result, conversation, model)
}

struct SessionParts {
    link: String,
    token: String,
    session_id: String,
    cwd: String,
}

impl SessionParts {
    fn from(session: &Value, conversation: &str) -> Option<Self> {
        let link = conversation::string_field(session, &["link", "endpoint"]);
        let token = conversation::string_field(session, &["token"]);
        if link.is_empty() || token.is_empty() {
            return None;
        }
        let session_id = conversation::string_field(session, &["sessionId", "session_id"]);
        let cwd = conversation::string_field(session, &["cwd"]);
        Some(Self {
            link,
            token,
            session_id: if session_id.is_empty() {
                conversation.to_string()
            } else {
                session_id
            },
            cwd: if cwd.is_empty() {
                config::web_default_cwd()
            } else {
                cwd
            },
        })
    }
}

fn create_failure(error: conversation::CreateError) -> Value {
    json!({
        "ok": false,
        "error": error.message,
        "retryable": error.retryable,
        "phase": "create"
    })
}

fn session_failure(conversation: String, error: String) -> Value {
    json!({
        "ok": false,
        "conversation": conversation,
        "error": error,
        "retryable": true,
        "phase": "session"
    })
}

fn finish_turn(mut result: Value, conversation: String, model: &str) -> Value {
    let ok = result.get("ok").and_then(Value::as_bool).unwrap_or(false);
    result["conversation"] = json!(conversation);
    result["model"] = json!(model);
    result["phase"] = json!("turn");
    if ok {
        result["msg"] = json!(format!(
            "网页通道会话跑完（{}）：{} 段输出，{} ms",
            model,
            result.get("chunks").and_then(Value::as_u64).unwrap_or(0),
            result
                .get("elapsed_ms")
                .and_then(Value::as_u64)
                .unwrap_or(0)
        ));
    } else {
        result["retryable"] = json!(true);
    }
    result
}
