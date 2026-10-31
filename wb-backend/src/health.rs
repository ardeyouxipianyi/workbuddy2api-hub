//! `/health`：launcher 就靠这里自报身份，键与顺序必须和 Python 版一致
//! （黄金样本见 specs/golden/health.json）。
//!
//! 注意 `inflight`：launcher 用它判断「还有没有请求在飞」。这里报的是真实
//! 在飞数，health 请求本身不进计数——Python 版要 `max(0, n-1)` 是因为它把
//! 自己也算进去了。

use std::sync::atomic::Ordering;
use std::sync::Arc;

use hyper::StatusCode;
use serde_json::{json, Map, Value};

use crate::http::{json_resp, Resp};
use crate::App;

pub async fn get(app: &Arc<App>) -> Resp {
    let state = &app.state;
    let realm = state.current_realm();
    let accounts = state.accounts();
    let now = crate::state::now_epoch();

    let mut by_realm: Map<String, Value> = Map::new();
    let mut ready_by_realm: Map<String, Value> = Map::new();
    let mut ready_current = 0usize;
    for a in accounts.iter() {
        let r = a.realm();
        *by_realm.entry(r.clone()).or_insert_with(|| json!(0)) =
            json!(by_realm.get(&r).and_then(Value::as_u64).unwrap_or(0) + 1);
        if a.ready(now) {
            *ready_by_realm.entry(r.clone()).or_insert_with(|| json!(0)) =
                json!(ready_by_realm.get(&r).and_then(Value::as_u64).unwrap_or(0) + 1);
            if r == realm {
                ready_current += 1;
            }
        }
    }

    let mut payload = Map::new();
    payload.insert("ok".into(), json!(true));
    payload.insert("realm".into(), json!(realm));
    // Python 版用它表示「加密模块可用」（Windows 上取密钥用）；Rust 后端不做
    // 进程内取密钥，恒定 true 才和 Linux 上的 Python 版一致。
    payload.insert("crypto_ok".into(), json!(true));
    payload.insert("backend".into(), json!("rust"));
    payload.insert("version".into(), json!(crate::VERSION));
    payload.insert("inflight".into(), json!(app.inflight.load(Ordering::Relaxed)));
    payload.insert("pid".into(), json!(std::process::id()));
    payload.insert("accounts".into(), json!(accounts.len()));
    payload.insert("accounts_ready".into(), json!(state.ready_count()));
    payload.insert("accounts_by_realm".into(), Value::Object(by_realm));
    payload.insert("accounts_ready_by_realm".into(), Value::Object(ready_by_realm));
    payload.insert("accounts_ready_current_realm".into(), json!(ready_current));
    payload.insert("models_ready".into(), json!({}));
    payload.insert("api_key_required".into(), json!(state.api_key_required()));

    // 账号身份只给授权调用方看（Python 版同一口径）；M1 的鉴权在 M2 一起接。
    match accounts.iter().find(|a| a.ready(now)) {
        Some(a) => {
            payload.insert("uid".into(), json!(a.uid()));
            payload.insert("domain".into(), json!(a.str("domain")));
            payload.insert("issuer".into(), json!(""));
            payload.insert("credential_file".into(), json!(a.name));
            payload.insert("expires_at".into(), json!(a.expires_at()));
        }
        None => {
            payload.insert("uid".into(), Value::Null);
            payload.insert("domain".into(), Value::Null);
            payload.insert("issuer".into(), Value::Null);
            payload.insert("credential_file".into(), Value::Null);
            payload.insert("expires_at".into(), json!(0));
        }
    }
    payload.insert("key_accounts_ready".into(), json!(0));
    payload.insert("key_accounts_bound".into(), json!(0));

    json_resp(StatusCode::OK, &Value::Object(payload))
}
