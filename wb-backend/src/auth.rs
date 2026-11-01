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
        let value = v.trim();
        let mut parts = value.splitn(2, char::is_whitespace);
        let scheme = parts.next().unwrap_or("");
        let mut supplied = if scheme.eq_ignore_ascii_case("bearer") {
            parts.next().unwrap_or("").trim().to_string()
        } else {
            value.to_string()
        };
        if supplied.len() >= 2 {
            let first = supplied.as_bytes()[0] as char;
            let last = supplied.as_bytes()[supplied.len() - 1] as char;
            if first == last && matches!(first, '\'' | '"') {
                supplied = supplied[1..supplied.len() - 1].trim().to_string();
            }
        }
        if !supplied.is_empty() {
            return supplied;
        }
    }
    for name in ["x-api-key", "api-key", "x-auth-token"] {
        if let Some(v) = req_headers.get(name).and_then(|v| v.to_str().ok()) {
            if !v.trim().is_empty() {
                return v.trim().to_string();
            }
        }
    }
    if let Some(q) = query {
        for pair in q.split('&') {
            let Some((name, value)) = pair.split_once('=') else {
                continue;
            };
            if matches!(name, "key" | "api_key" | "api-key") && !value.trim().is_empty() {
                return value.trim().to_string();
            }
        }
    }
    String::new()
}

/// 某个 Key 绑定的出口（`""` = 跟随面板切换）。路由时 Key 的绑定优先于全局开关，
/// 与 Python `_resolve_realm` 同序（wb_proxy.py:18915：显式 ?realm= > Key > X-Realm > 全局）。
pub fn bound_realm(state: &State, key_id: &str) -> String {
    if key_id.is_empty() {
        return String::new();
    }
    let settings = state.settings();
    settings
        .get("api_keys")
        .and_then(serde_json::Value::as_array)
        .and_then(|list| {
            list.iter()
                .find(|entry| entry.get("id").and_then(serde_json::Value::as_str) == Some(key_id))
                .and_then(|entry| entry.get("realm").and_then(serde_json::Value::as_str))
                .map(|realm| realm.trim().to_lowercase())
        })
        .unwrap_or_default()
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

#[cfg(test)]
mod tests {
    use super::supplied;

    #[test]
    fn accepts_python_query_and_quote_spellings() {
        assert_eq!(supplied(&hyper::HeaderMap::new(), Some("api_key=sk-test")), "sk-test");
        let mut headers = hyper::HeaderMap::new();
        headers.insert(
            hyper::header::AUTHORIZATION,
            hyper::header::HeaderValue::from_static("BEARER \"sk-quoted\""),
        );
        assert_eq!(supplied(&headers, None), "sk-quoted");
    }
}
