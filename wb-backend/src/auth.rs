//! /v1 鉴权：面板管理的 api_keys（settings.json）+ 可选的启动参数 key。
//!
//! 两条非对称规则来自 Python 版（wb_proxy.py:550-571），照抄不解释：
//! 面板里只要有启用的 key，`--api-key` 就整体失效；开关 `auth_disabled` 一开就全放开。

use sha2::{Digest, Sha256};

use crate::state::State;

/// 定长比较：先各自摘要再比，长度和前缀都看不出差别。
fn ct_eq(a: &str, b: &str) -> bool {
    let digest = |v: &str| Sha256::digest(v.as_bytes()).to_vec();
    digest(a) == digest(b)
}

/// settings.json 里一条可用 key（enabled 且未删除）。
fn entries(state: &State) -> Vec<(String, String)> {
    let settings = state.settings();
    let settings = serde_json::Value::Object(settings);
    let mut out = Vec::new();
    if let Some(list) = settings.get("api_keys").and_then(serde_json::Value::as_array) {
        for entry in list {
            let key = entry.get("key").and_then(serde_json::Value::as_str).unwrap_or("").trim();
            let enabled = entry.get("enabled").and_then(serde_json::Value::as_bool).unwrap_or(false);
            // 面板写的是空串（不是 null）表示没删；两种都要当「未删除」。
            let deleted = match entry.get("deleted_at") {
                Some(serde_json::Value::String(text)) => !text.trim().is_empty(),
                Some(serde_json::Value::Null) | None => false,
                Some(_) => true,
            };
            if !key.is_empty() && enabled && !deleted {
                out.push((
                    entry.get("id").and_then(serde_json::Value::as_str).unwrap_or("").to_string(),
                    key.to_string(),
                ));
            }
        }
    }
    out
}

/// 一次请求带进来的凭据：`Authorization: Bearer` > `x-api-key` > `?key=`。
pub fn supplied(req_headers: &hyper::HeaderMap, query: Option<&str>) -> String {
    if let Some(v) = req_headers.get(hyper::header::AUTHORIZATION).and_then(|v| v.to_str().ok()) {
        if let Some(rest) = v.strip_prefix("Bearer ").or_else(|| v.strip_prefix("bearer ")) {
            return rest.trim().to_string();
        }
    }
    if let Some(v) = req_headers.get("x-api-key").and_then(|v| v.to_str().ok()) {
        return v.trim().to_string();
    }
    if let Some(q) = query {
        for pair in q.split('&') {
            if let Some(v) = pair.strip_prefix("key=") {
                return v.trim().to_string();
            }
        }
    }
    String::new()
}

/// 返回命中的 key id（写进 usage 行），空串表示「免鉴权放行」。
pub struct Auth {
    pub required: bool,
    pub key_id: String,
}

pub fn check(state: &State, supplied_key: &str) -> Result<Auth, ()> {
    let settings = state.settings();
    if settings.get("auth_disabled").and_then(serde_json::Value::as_bool) == Some(true) {
        return Ok(Auth { required: false, key_id: String::new() });
    }
    let keys = entries(state);
    let launcher = std::env::var("WB_API_KEY").unwrap_or_default();
    // 面板里有 key 时启动参数 key 一律失效（wb_proxy.py:570）。
    let extra: Vec<(String, String)> =
        if keys.is_empty() && !launcher.is_empty() {
            vec![("launcher".into(), launcher)]
        } else {
            Vec::new()
        };
    let required = !keys.is_empty() || !extra.is_empty();
    if !required {
        return Ok(Auth { required: false, key_id: String::new() });
    }
    if supplied_key.is_empty() {
        return Err(());
    }
    for (id, key) in keys.iter().chain(extra.iter()) {
        if ct_eq(supplied_key, key) {
            return Ok(Auth { required: true, key_id: id.clone() });
        }
    }
    Err(())
}
