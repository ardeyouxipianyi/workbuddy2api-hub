//! CodeArts 的 HTTP 层：普通请求、JSON 错误归一、OAuth token、华为云签名请求。
//!
//! 对照 `wb_codearts.py` 的 `_http`/`_json_http`/`_normalize_token`/`request_token`/
//! `exchange_code`/`refresh_token_for`/`poll_ticket`/`signed_get`/`signed_post`/
//! `_ensure_token`。

use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};

use serde_json::{json, Map, Value};

use super::crypto;
use super::{
    now_secs, Account, CodeArtsError, Pool, Result, CLIENT_ID, EP_LOGIN_TICKET, EP_OAUTH_TOKENS,
    ERROR_BODY_MAX, PLUGIN_NAME, PLUGIN_VERSION, REDIRECT_PATH, REFRESH_SKEW,
};

fn clients() -> &'static Mutex<HashMap<String, reqwest::Client>> {
    static CLIENTS: OnceLock<Mutex<HashMap<String, reqwest::Client>>> = OnceLock::new();
    CLIENTS.get_or_init(|| Mutex::new(HashMap::new()))
}

/// 一个代理 URL 一个 client（每个账号走自己的出口 IP）；空串=直连且忽略环境代理。
fn client_for(proxy: &str, timeout: f64) -> Result<reqwest::Client> {
    let proxy = proxy.trim();
    let key = format!("{proxy}|{timeout}");
    if let Some(client) = clients().lock().unwrap_or_else(|err| err.into_inner()).get(&key) {
        return Ok(client.clone());
    }
    let mut builder = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs_f64(timeout.max(1.0)))
        .connect_timeout(std::time::Duration::from_secs(20));
    if proxy.is_empty() {
        // 没绑槽位就直连，别被进程级 HTTP_PROXY 悄悄带偏（Python 同口径）。
        builder = builder.no_proxy();
    } else {
        builder = builder.proxy(
            reqwest::Proxy::all(proxy).map_err(|err| CodeArtsError::Unavailable(format!("代理槽位不可用：{err}")))?,
        );
    }
    let client = builder
        .build()
        .map_err(|err| CodeArtsError::Unavailable(format!("HTTP 客户端初始化失败：{err}")))?;
    clients()
        .lock()
        .unwrap_or_else(|err| err.into_inner())
        .insert(key, client.clone());
    Ok(client)
}

/// `_http`：返回 (status, body)；连不上统一成 `Unavailable`。
pub async fn http(
    method: &str,
    url: &str,
    headers: &[(String, String)],
    body: Option<Vec<u8>>,
    timeout: f64,
    proxy: &str,
) -> Result<(u16, Vec<u8>)> {
    let client = client_for(proxy, timeout)?;
    let verb = reqwest::Method::from_bytes(method.to_uppercase().as_bytes())
        .map_err(|err| CodeArtsError::Other(format!("非法方法：{err}")))?;
    let mut request = client.request(verb, url);
    for (key, value) in headers {
        request = request.header(key.as_str(), value.as_str());
    }
    if let Some(payload) = body {
        request = request.body(payload);
    }
    let response = request.send().await.map_err(|err| {
        CodeArtsError::Unavailable(format!("连不上华为云 CodeArts：{err} - 请检查本机网络或代理。"))
    })?;
    let status = response.status().as_u16();
    let bytes = response
        .bytes()
        .await
        .map_err(|err| CodeArtsError::Unavailable(format!("读取华为云响应失败：{err}")))?;
    let limit = ERROR_BODY_MAX * 4;
    Ok((status, bytes.get(..limit.min(bytes.len())).unwrap_or(&bytes).to_vec()))
}

/// `_error_text`：从华为云错误体里捞最能说明问题的一句。
pub fn error_text(raw: &[u8]) -> String {
    let text = String::from_utf8_lossy(raw).to_string();
    let parsed: Option<Value> = serde_json::from_str(&text).ok();
    if let Some(Value::Object(map)) = parsed {
        for key in ["error_msg", "message", "error_description", "error"] {
            if let Some(value) = map.get(key).and_then(Value::as_str) {
                if !value.is_empty() {
                    return value.chars().take(500).collect();
                }
            }
        }
        if let Some(code) = map.get("error_code").or_else(|| map.get("code")) {
            let empty = matches!(code, Value::Null) || code.as_str() == Some("") || code.as_i64() == Some(0);
            if !empty {
                let rendered = match code {
                    Value::String(text) => text.clone(),
                    other => other.to_string(),
                };
                return format!("error_code={rendered}");
            }
        }
    }
    text.chars().take(500).collect()
}

/// `_json_http`：非 2xx 按状态码映射成对应异常；2xx 但解析不出对象也算错。
pub async fn json_http(
    method: &str,
    url: &str,
    headers: &[(String, String)],
    body: Option<Vec<u8>>,
    timeout: f64,
    proxy: &str,
) -> Result<Map<String, Value>> {
    let (status, raw) = http(method, url, headers, body, timeout, proxy).await?;
    let parsed: Option<Value> = serde_json::from_str(&String::from_utf8_lossy(&raw)).ok();
    if status >= 400 {
        let message = error_text(&raw);
        return Err(match status {
            401 | 403 => CodeArtsError::Auth(message),
            400 => CodeArtsError::BadRequest(message),
            404 => CodeArtsError::NotFound(message),
            409 => CodeArtsError::Conflict(message),
            _ => CodeArtsError::Other(format!("华为云返回 HTTP {status}：{message}")),
        });
    }
    match parsed {
        Some(Value::Object(map)) => Ok(map),
        _ => {
            let head: String = String::from_utf8_lossy(&raw).chars().take(200).collect();
            Err(CodeArtsError::Other(format!("华为云返回了无法解析的响应：{head}")))
        }
    }
}

/// `_normalize_token`：把 oauth2/tokens 的两种历史形状收敛成统一结构。
pub fn normalize_token(payload: &Map<String, Value>) -> Value {
    let text = |value: Option<&Value>| -> String {
        match value {
            Some(Value::String(text)) => text.clone(),
            Some(Value::Number(number)) => number.to_string(),
            Some(Value::Null) | None => String::new(),
            Some(other) => other.to_string(),
        }
    };
    let legacy = payload.get("credential").and_then(Value::as_object);
    let mut creds = payload.get("credentials").and_then(Value::as_object).cloned().unwrap_or_default();
    let has_token = !text(creds.get("security_token")).is_empty();
    if !has_token {
        if let Some(legacy) = legacy {
            if !text(legacy.get("securitytoken")).is_empty() {
                creds = [
                    ("access_key_id".to_string(), json!(text(legacy.get("access")))),
                    ("secret_access_key".to_string(), json!(text(legacy.get("secret")))),
                    ("security_token".to_string(), json!(text(legacy.get("securitytoken")))),
                    ("expiration".to_string(), json!(text(legacy.get("expires_at")))),
                ]
                .into_iter()
                .collect();
            }
        }
    }
    json!({
        "user_id": text(payload.get("user_id")),
        "user_name": text(payload.get("user_name")),
        "domain_id": text(payload.get("domain_id")),
        "refresh_token": text(payload.get("refresh_token")),
        "credentials": {
            "access_key_id": text(creds.get("access_key_id")),
            "secret_access_key": text(creds.get("secret_access_key")),
            "security_token": text(creds.get("security_token")),
            "expiration": text(creds.get("expiration")),
        },
    })
}

fn form_body(form: &[(String, String)]) -> Vec<u8> {
    let encoded: Vec<String> = form
        .iter()
        .map(|(key, value)| {
            format!(
                "{}={}",
                urlencode(key),
                urlencode(value)
            )
        })
        .collect();
    encoded.join("&").into_bytes()
}

fn urlencode(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for byte in text.as_bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'*' => out.push(*byte as char),
            b' ' => out.push('+'),
            other => out.push_str(&format!("%{other:02X}")),
        }
    }
    out
}

pub fn query_string(pairs: &[(&str, String)]) -> String {
    pairs
        .iter()
        .map(|(key, value)| format!("{}={}", urlencode(key), urlencode(value)))
        .collect::<Vec<_>>()
        .join("&")
}

/// `request_token`：POST 到 STS，带 DPoP 证明。
pub async fn request_token(
    form: &[(String, String)],
    jwk: &Value,
    timeout: f64,
    proxy: &str,
) -> Result<Value> {
    let url = format!("{}{}", super::sts_host(), EP_OAUTH_TOKENS);
    let key = crypto::KeyPair::from_jwk(jwk).map_err(CodeArtsError::Auth)?;
    let headers = vec![
        ("Content-Type".to_string(), "application/x-www-form-urlencoded".to_string()),
        ("DPoP".to_string(), crypto::dpop_proof(&key, &url)),
    ];
    let payload = json_http("POST", &url, &headers, Some(form_body(form)), timeout, proxy).await?;
    let token = normalize_token(&payload);
    let security_token = token["credentials"]["security_token"].as_str().unwrap_or("");
    if security_token.is_empty() {
        let dump: String = serde_json::to_string(&payload).unwrap_or_default().chars().take(300).collect();
        return Err(CodeArtsError::Other(format!("登录响应缺少 security_token：{dump}")));
    }
    Ok(token)
}

pub async fn exchange_code(code: &str, verifier: &str, port: u16, jwk: &Value) -> Result<Value> {
    let form = vec![
        ("client_id".to_string(), CLIENT_ID.to_string()),
        ("code".to_string(), code.to_string()),
        ("code_verifier".to_string(), verifier.to_string()),
        ("grant_type".to_string(), "authorization_code".to_string()),
        ("redirect_uri".to_string(), format!("http://127.0.0.1:{port}{REDIRECT_PATH}")),
    ];
    request_token(&form, jwk, 30.0, "").await
}

/// `refresh_token_for`：refresh_token 失效时升级成 `Auth`（需要重新登录）。
pub async fn refresh_token_for(auth: &Value, timeout: f64, proxy: &str) -> Result<Value> {
    let jwk = auth
        .get("dpop_private_key")
        .filter(|value| value.get("d").is_some())
        .cloned()
        .unwrap_or_else(|| crypto::KeyPair::generate().jwk());
    let text = |key: &str, fallback: &str| -> String {
        auth.get(key)
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .unwrap_or(fallback)
            .to_string()
    };
    let form = vec![
        ("client_id".to_string(), text("client_id", CLIENT_ID)),
        ("code_verifier".to_string(), text("code_verifier", "")),
        ("grant_type".to_string(), "refresh_token".to_string()),
        ("refresh_token".to_string(), text("refresh_token", "")),
    ];
    match request_token(&form, &jwk, timeout, proxy).await {
        Ok(token) => Ok(token),
        Err(CodeArtsError::BadRequest(message)) => {
            let lowered = message.to_lowercase();
            let terminal = ["invalid_grant", "expiredrefreshtoken", "invaliddpopheader", "invalid client id", "has been used"]
                .iter()
                .any(|needle| lowered.contains(needle));
            if terminal {
                Err(CodeArtsError::Auth(format!("refresh_token 已失效或被拒，需要重新登录：{message}")))
            } else {
                Err(CodeArtsError::BadRequest(message))
            }
        }
        Err(other) => Err(other),
    }
}

pub async fn poll_ticket(ticket_id: &str, secret: &str, timeout: f64) -> Result<Value> {
    let url = format!(
        "{}{}?{}",
        super::snap_manager(),
        EP_LOGIN_TICKET,
        query_string(&[("ticket_id", ticket_id.to_string()), ("secret", secret.to_string())])
    );
    let headers = vec![
        ("plugin-name".to_string(), PLUGIN_NAME.to_string()),
        ("plugin-version".to_string(), PLUGIN_VERSION.to_string()),
    ];
    let payload = json_http("GET", &url, &headers, None, timeout, "").await?;
    Ok(normalize_token(&payload))
}

fn base_headers(agent_type: &str) -> Vec<(String, String)> {
    let mut headers = vec![
        ("Content-Type".to_string(), "application/json".to_string()),
        ("X-Language".to_string(), "zh-cn".to_string()),
    ];
    if !agent_type.is_empty() {
        headers.push(("Agent-Type".to_string(), agent_type.to_string()));
    }
    headers
}

fn signed_headers_for(account: &Account, method: &str, url: &str, headers: &[(String, String)], body: &[u8]) -> Result<Vec<(String, String)>> {
    let (access_key, secret_key, security_token) = account.credential();
    crypto::sign_request(method, url, headers, body, &access_key, &secret_key, &security_token, &utc_stamp())
        .map_err(CodeArtsError::Other)
}

/// `time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())`：签名必须用 UTC。
fn utc_stamp() -> String {
    let secs = super::now_secs() as i64;
    let days = secs.div_euclid(86400);
    let rest = secs.rem_euclid(86400);
    let (year, month, day) = super::civil_from_days(days);
    format!(
        "{year:04}{month:02}{day:02}T{:02}{:02}{:02}Z",
        rest / 3600,
        (rest % 3600) / 60,
        rest % 60
    )
}

pub async fn signed_get(
    account: &Account,
    url: &str,
    extra: &[(String, String)],
    timeout: f64,
    agent_type: &str,
) -> Result<Map<String, Value>> {
    let mut headers = base_headers(agent_type);
    headers.extend(extra.iter().cloned());
    let signed = signed_headers_for(account, "GET", url, &headers, b"")?;
    json_http("GET", url, &signed, None, timeout, &account.proxy()).await
}

pub async fn signed_post(
    account: &Account,
    url: &str,
    payload: &Value,
    extra: &[(String, String)],
    timeout: f64,
    agent_type: &str,
) -> Result<Map<String, Value>> {
    let raw = serde_json::to_vec(payload).unwrap_or_else(|_| b"{}".to_vec());
    let mut headers = base_headers(agent_type);
    headers.extend(extra.iter().cloned());
    let signed = signed_headers_for(account, "POST", url, &headers, &raw)?;
    json_http("POST", url, &signed, Some(raw), timeout, &account.proxy()).await
}

/// `_ensure_token`：快过期就续期。refresh_token 一次性，必须串行。
pub async fn ensure_token(account: &Account, pool: &Pool) -> Result<()> {
    if !account.expiring_soon(REFRESH_SKEW) && !account.expired() {
        return Ok(());
    }
    let _guard = account.refresh_lock().lock().await;
    if !account.expiring_soon(REFRESH_SKEW) && !account.expired() {
        return Ok(());
    }
    let auth = account.auth();
    match refresh_token_for(&auth, 30.0, &account.proxy()).await {
        Ok(token) => {
            account.update_auth(pool, &token)?;
            Ok(())
        }
        Err(CodeArtsError::Auth(message)) => {
            pool.note_error(account, &message, false, true);
            Err(CodeArtsError::Auth(message))
        }
        Err(other) => {
            if account.expired() {
                Err(other)
            } else {
                Ok(())
            }
        }
    }
}
