//! `GET /v1/models`：内置目录 + 运行时过滤。
//!
//! 目录内容（顺序、capabilities、limits）来自 `assets/catalog-<realm>.json`，
//! 由 `assets/build-catalog.py` 从 Python 的 `merge_catalog`/`model_entry` 导出
//! ——目录只有一份真源。这里只做三件运行时的事：`created` 取当前时间、按
//! realm 选表、打「PRO 才能用」的单向标注。
//!
//! shortcut: 活数据源（remote / 桌面缓存 / models.dev）与 pinned/key/available
//! 过滤还没搬，M4 搬面板时一起做；在那之前这里只服务内置目录。

use std::sync::Arc;
use std::sync::OnceLock;

use hyper::body::Incoming;
use hyper::{Request, StatusCode};
use serde_json::{json, Map, Value};

use crate::http::{json_resp, Resp};
use crate::App;

const INTL: &str = include_str!("../assets/catalog-intl.json");
const CN: &str = include_str!("../assets/catalog-cn.json");

fn bundled_free(realm: &str) -> &'static std::collections::HashSet<String> {
    static FREE: OnceLock<(
        std::collections::HashSet<String>,
        std::collections::HashSet<String>,
    )> = OnceLock::new();
    let free = FREE.get_or_init(|| {
        let collect = |raw: &str| {
            serde_json::from_str::<Vec<Value>>(raw)
                .unwrap_or_default()
                .into_iter()
                .filter(|item| {
                    matches!(
                        item.get("credits")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .trim()
                            .to_lowercase()
                            .as_str(),
                        "x0.00" | "x0" | "0" | "0.00"
                    )
                })
                .filter_map(|item| {
                    item.get("id")
                        .and_then(Value::as_str)
                        .map(str::to_string)
                })
                .collect()
        };
        (collect(INTL), collect(CN))
    });
    if realm == "cn" {
        &free.1
    } else {
        &free.0
    }
}

/// Free model ids from the bundled catalogue plus the runtime remote cache.
pub fn free_models(realm: &str, cached: &Value) -> std::collections::HashSet<String> {
    let mut free = bundled_free(realm).clone();
    let entries = cached
        .get(realm)
        .and_then(|entry| entry.get("data"))
        .and_then(Value::as_array);
    for item in entries.into_iter().flatten() {
        let credits = item
            .get("credits")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_lowercase();
        if matches!(credits.as_str(), "x0.00" | "x0" | "0" | "0.00") {
            if let Some(id) = item.get("id").and_then(Value::as_str) {
                free.insert(id.to_string());
            }
        }
    }
    free
}

/// 目录里某个模型的元数据（effort 之类的判定要用）。
pub fn lookup(realm: &str, model: &str) -> Option<Value> {
    let raw = if realm == "cn" { CN } else { INTL };
    let items: Vec<Value> = serde_json::from_str(raw).ok()?;
    items.into_iter().find(|m| m.get("id").and_then(Value::as_str) == Some(model))
}

/// Model descriptors for the agent-config picker. Objects carry the catalog
/// limits and capabilities clients use; duplicate ids across realms keep the
/// first (international) entry.
pub fn agent_models() -> Vec<Value> {
    let mut seen = std::collections::BTreeSet::new();
    let mut out = Vec::new();
    for realm in ["intl", "cn"] {
        for item in entries(realm) {
            let Some(id) = item.get("id").and_then(Value::as_str) else {
                continue;
            };
            if !seen.insert(id.to_string()) {
                continue;
            }
            let mut model = Map::new();
            if let Some(value) = item
                .get("max_input_tokens")
                .or_else(|| item.get("context_length"))
                .and_then(Value::as_i64)
            {
                model.insert("context_window".into(), json!(value));
            }
            if let Some(value) = item
                .get("max_output_tokens")
                .or_else(|| item.get("max_completion_tokens"))
                .and_then(Value::as_i64)
            {
                model.insert("max_output".into(), json!(value));
            }
            if let Some(value) = item.get("name").and_then(Value::as_str) {
                model.insert("name".into(), json!(value));
            }
            if item
                .get("supports_vision")
                .or_else(|| item.get("supports_images"))
                .and_then(Value::as_bool)
                .unwrap_or(false)
            {
                model.insert("supports_vision".into(), json!(true));
            }
            if let Some(efforts) = item.get("reasoning_efforts").and_then(Value::as_array) {
                let efforts: Vec<Value> = efforts.iter().filter(|value| value.is_string()).cloned().collect();
                if !efforts.is_empty() {
                    model.insert("reasoning_efforts".into(), Value::Array(efforts));
                }
            }
            out.push(Value::Object(model));
        }
    }
    out
}

fn entries(realm: &str) -> Vec<Value> {
    let raw = if realm == "cn" { CN } else { INTL };
    let mut items: Vec<Value> = serde_json::from_str(raw).unwrap_or_default();
    let now = crate::state::now_epoch();
    for item in items.iter_mut() {
        if let Value::Object(map) = item {
            // created 在资产里被剔掉了：它是「本次响应的时间」，不是模型属性。
            let mut with_created = Map::new();
            with_created.insert("id".into(), map.get("id").cloned().unwrap_or(Value::Null));
            with_created.insert("object".into(), json!("model"));
            with_created.insert("created".into(), json!(now));
            with_created.insert("owned_by".into(), json!("workbuddy"));
            for (k, v) in map.iter() {
                if k != "id" {
                    with_created.insert(k.clone(), v.clone());
                }
            }
            *item = Value::Object(with_created);
        }
    }
    items
}

pub async fn list(app: &Arc<App>, req: &Request<Incoming>) -> Resp {
    let pinned = crate::keyref::pinned(req).and_then(|pin| {
        app.state
            .accounts_snapshot()
            .into_iter()
            .find(|account| account.uid() == pin.uid)
    });
    let realm = pinned
        .as_ref()
        .map(crate::state::AccountFile::realm)
        .unwrap_or_else(|| crate::http::requested_realm(req, &app.state.current_realm(), ""));
    // CodeArts 是活目录（三路上游 + 种子兜底），不走内置快照。
    if realm == "codearts" {
        return crate::codearts::chat::models_response(app).await;
    }
    // 走运行时目录：面板「刷新模型清单」写的是它，这里若只读内置快照，
    // 刷新成功也看不见新模型（cn 一直停在 14 个）。
    let (mut data, catalog) = crate::catalog_refresh::entries_for(&realm);
    if let Some(account) = pinned.as_ref() {
        let disabled = crate::keyref::disabled_models(account);
        if !disabled.is_empty() {
            let disabled: std::collections::HashSet<&str> =
                disabled.iter().map(String::as_str).collect();
            data.retain(|item| {
                item.get("id")
                    .and_then(Value::as_str)
                    .map(|id| !disabled.contains(id))
                    .unwrap_or(true)
            });
        }
    }
    let mut payload = Map::new();
    payload.insert("object".into(), json!("list"));
    payload.insert("data".into(), Value::Array(data));
    payload.insert("realm".into(), json!(realm));
    payload.insert("catalog".into(), catalog);
    json_resp(StatusCode::OK, &Value::Object(payload))
}