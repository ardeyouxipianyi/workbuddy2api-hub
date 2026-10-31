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

use hyper::body::Incoming;
use hyper::{Request, StatusCode};
use serde_json::{json, Map, Value};

use crate::http::{json_resp, Resp};
use crate::App;

const INTL: &str = include_str!("../assets/catalog-intl.json");
const CN: &str = include_str!("../assets/catalog-cn.json");

/// 目录里某个模型的元数据（effort 之类的判定要用）。
pub fn lookup(realm: &str, model: &str) -> Option<Value> {
    let raw = if realm == "cn" { CN } else { INTL };
    let items: Vec<Value> = serde_json::from_str(raw).ok()?;
    items.into_iter().find(|m| m.get("id").and_then(Value::as_str) == Some(model))
}

/// 全部模型 id（intl + cn 去重），智能体配置的模型选择器用。
pub fn flat_ids() -> Vec<String> {
    let mut seen = std::collections::BTreeSet::new();
    for realm in ["intl", "cn"] {
        for item in entries(realm) {
            if let Some(id) = item.get("id").and_then(Value::as_str) {
                seen.insert(id.to_string());
            }
        }
    }
    seen.into_iter().collect()
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
    let realm = crate::http::requested_realm(req, &app.state.current_realm());
    let data = entries(&realm);
    let mut payload = Map::new();
    payload.insert("object".into(), json!("list"));
    payload.insert("data".into(), Value::Array(data));
    payload.insert("realm".into(), json!(realm));
    payload.insert(
        "catalog".into(),
        json!({"source": "bundled", "fetched_at": now_float(), "file_mtime": Value::Null}),
    );
    json_resp(StatusCode::OK, &Value::Object(payload))
}

fn now_float() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}
