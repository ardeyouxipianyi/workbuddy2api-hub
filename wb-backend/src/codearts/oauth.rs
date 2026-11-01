//! CodeArts OAuth 登录：PKCE + DPoP 会话、浏览器回跳、身份补查、落账号。
//!
//! 对照 `wb_codearts.py` 的 `oauth_start`/`oauth_poll`/`oauth_callback`/
//! `oauth_callback_get`/`_save_login`/`lookup_identity`/`_find_session`/`_gc_oauth`。

use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};

use serde_json::{json, Value};

use super::crypto;
use super::{now_secs, CodeArtsError, Pool, Result, CLIENT_ID, OAUTH_TTL, PLUGIN_NAME,
            PLUGIN_VERSION, REDIRECT_PATH};

#[derive(Clone)]
struct OAuthSession {
    id: String,
    ticket_id: String,
    secret: String,
    verifier: String,
    dpop_jwk: Value,
    port: u16,
    created_at: f64,
    status: String,
    message: String,
    account: Option<Value>,
    ticket_fallback: bool,
    auth_url: String,
}

impl OAuthSession {
    fn public(&self) -> Value {
        let mut out = json!({
            "ok": self.status != "error",
            "status": self.status,
            "message": self.message,
            "session_id": self.id,
            "auth_url": self.auth_url,
        });
        if let Some(account) = &self.account {
            out["account"] = account.clone();
        }
        out
    }
}

fn sessions() -> &'static Mutex<HashMap<String, OAuthSession>> {
    static SESSIONS: OnceLock<Mutex<HashMap<String, OAuthSession>>> = OnceLock::new();
    SESSIONS.get_or_init(|| Mutex::new(HashMap::new()))
}

fn gc() {
    let now = now_secs();
    let mut map = sessions().lock().unwrap_or_else(|err| err.into_inner());
    map.retain(|_, session| now - session.created_at <= OAUTH_TTL);
}

fn find(session_id: Option<&str>, ticket_id: Option<&str>, secret: Option<&str>) -> Option<OAuthSession> {
    gc();
    let map = sessions().lock().unwrap_or_else(|err| err.into_inner());
    if let Some(id) = session_id.filter(|value| !value.is_empty()) {
        return map.get(id).cloned();
    }
    if let Some(secret) = secret.filter(|value| !value.is_empty()) {
        return map.values().find(|s| s.secret == secret && s.status == "pending").cloned();
    }
    if let Some(ticket) = ticket_id.filter(|value| !value.is_empty()) {
        return map.values().find(|s| s.ticket_id == ticket && s.status == "pending").cloned();
    }
    let mut pending: Vec<OAuthSession> = map.values().filter(|s| s.status == "pending").cloned().collect();
    pending.sort_by(|a, b| b.created_at.partial_cmp(&a.created_at).unwrap_or(std::cmp::Ordering::Equal));
    pending.into_iter().next()
}

fn store(session: OAuthSession) {
    sessions()
        .lock()
        .unwrap_or_else(|err| err.into_inner())
        .insert(session.id.clone(), session);
}

fn update<F: FnOnce(&mut OAuthSession)>(session_id: &str, apply: F) {
    let mut map = sessions().lock().unwrap_or_else(|err| err.into_inner());
    if let Some(session) = map.get_mut(session_id) {
        apply(session);
    }
}

/// `oauth_start`：生成会话与授权链接。
pub fn start(port: u16) -> Value {
    gc();
    let ticket_id = crypto::random_hex(16);
    let secret = crypto::random_hex(16);
    let (verifier, challenge) = crypto::pkce_pair();
    let dpop_jwk = crypto::KeyPair::generate().jwk();
    let session_id = crypto::random_hex(8);
    let query = super::client::query_string(&[
        ("theme", "2".to_string()),
        ("locale", "zh-cn".to_string()),
        ("uri_scheme", CLIENT_ID.to_string()),
        ("client_id", CLIENT_ID.to_string()),
        ("port", port.to_string()),
        ("code_challenge", challenge),
        ("code_challenge_method", "SHA-256".to_string()),
        ("ticket_id", ticket_id.clone()),
        ("plugin-name", PLUGIN_NAME.to_string()),
        ("plugin-version", PLUGIN_VERSION.to_string()),
    ]);
    let auth_url = format!("{}/authorize?{query}", super::portal_host());
    store(OAuthSession {
        id: session_id.clone(),
        ticket_id,
        secret,
        verifier,
        dpop_jwk,
        port,
        created_at: now_secs(),
        status: "pending".into(),
        message: "等待浏览器完成授权…".into(),
        account: None,
        ticket_fallback: false,
        auth_url: auth_url.clone(),
    });
    json!({
        "ok": true,
        "session_id": session_id,
        "auth_url": auth_url,
        "expires_in": OAUTH_TTL as i64,
        "message": "请在浏览器打开授权链接，完成后等待自动检测",
    })
}

/// `_save_login`：把 token 落成账号文件并进池。
async fn save_login(pool: &Pool, session: &OAuthSession, token: &Value) -> Result<Value> {
    let creds = token.get("credentials").cloned().unwrap_or(Value::Null);
    let mut uid = token.get("user_id").and_then(Value::as_str).unwrap_or("").to_string();
    let mut name = token.get("user_name").and_then(Value::as_str).unwrap_or("").to_string();
    let mut domain = token.get("domain_id").and_then(Value::as_str).unwrap_or("").to_string();
    if uid.is_empty() || name.is_empty() {
        let (looked_uid, looked_name, looked_domain) = lookup_identity(&creds).await;
        if uid.is_empty() {
            uid = looked_uid;
        }
        if name.is_empty() {
            name = looked_name;
        }
        if domain.is_empty() {
            domain = looked_domain;
        }
    }
    let refresh_token = token.get("refresh_token").and_then(Value::as_str).unwrap_or("");
    if uid.is_empty() {
        uid = identity_from_refresh_token(refresh_token);
    }
    if uid.is_empty() {
        // 最后兜底：refresh_token 的稳定哈希，避免写出 codearts-unknown.json。
        let digest = {
            use sha2::{Digest, Sha256};
            let mut hasher = Sha256::new();
            hasher.update(refresh_token.as_bytes());
            hasher.finalize().iter().map(|byte| format!("{byte:02x}")).collect::<String>()
        };
        uid = format!("codearts-{}", digest.chars().take(12).collect::<String>());
    }
    if name.is_empty() {
        name = uid.clone();
    }
    let auth = json!({
        "user_id": uid,
        "user_name": name,
        "domain_id": domain,
        "cloud_dragon_token": creds.get("security_token").cloned().unwrap_or(json!("")),
        "access_key_id": creds.get("access_key_id").cloned().unwrap_or(json!("")),
        "secret_access_key": creds.get("secret_access_key").cloned().unwrap_or(json!("")),
        "expiration": creds.get("expiration").cloned().unwrap_or(json!("")),
        "refresh_token": refresh_token,
        "code_verifier": session.verifier,
        "client_id": CLIENT_ID,
        "dpop_private_key": session.dpop_jwk,
        "updated_at": now_secs() as i64,
    });
    super::save_auth(pool.accounts_dir(), None, &auth)?;
    pool.add(auth);
    Ok(json!({"uid": uid, "nickname": name}))
}

/// `identity_from_refresh_token`：refresh_token 是 JWT 时取 sub。
pub fn identity_from_refresh_token(refresh_token: &str) -> String {
    let parts: Vec<&str> = refresh_token.split('.').collect();
    if parts.len() < 2 {
        return String::new();
    }
    let Some(raw) = crypto::b64url_decode(parts[1]) else { return String::new() };
    let Ok(payload) = serde_json::from_slice::<Value>(&raw) else { return String::new() };
    payload.get("sub").and_then(Value::as_str).unwrap_or("").to_string()
}

/// `lookup_identity`：caller-identity → current/user，补 uid/昵称/domain。
pub async fn lookup_identity(creds: &Value) -> (String, String, String) {
    let access = creds.get("access_key_id").and_then(Value::as_str).unwrap_or("");
    let secret = creds.get("secret_access_key").and_then(Value::as_str).unwrap_or("");
    let token = creds.get("security_token").and_then(Value::as_str).unwrap_or("");
    let (mut first_uid, mut first_name, mut first_domain) = (String::new(), String::new(), String::new());
    let sts_url = format!("{}/v5/caller-identity", super::sts_host());
    if let Ok(data) = super::chat::identity_holder_get(access, secret, token, &sts_url).await {
        let principal = data.get("principal_id").and_then(Value::as_str).unwrap_or("");
        first_uid = if principal.is_empty() {
            data.get("account_id").and_then(Value::as_str).unwrap_or("").to_string()
        } else {
            principal.to_string()
        };
        let urn = data.get("principal_urn").and_then(Value::as_str).unwrap_or("");
        first_name = if urn.contains('/') {
            urn.rsplit('/').next().unwrap_or("").to_string()
        } else {
            String::new()
        };
        first_domain = data.get("account_id").and_then(Value::as_str).unwrap_or("").to_string();
        if !first_uid.is_empty() && !first_name.is_empty() && first_name != first_uid {
            return (first_uid, first_name, first_domain);
        }
    }
    let current_url = format!("{}/v1/current/user", super::snap_manager());
    if let Ok(data) = super::chat::identity_holder_get(access, secret, token, &current_url).await {
        let uid = data.get("user_id").and_then(Value::as_str).unwrap_or("").to_string();
        let name = data.get("user_name").and_then(Value::as_str).unwrap_or("").to_string();
        let domain = data.get("domain_id").and_then(Value::as_str).unwrap_or("").to_string();
        if !uid.is_empty() || !first_uid.is_empty() {
            return (
                if first_uid.is_empty() { uid } else { first_uid },
                if name.is_empty() { first_name } else { name },
                if domain.is_empty() { first_domain } else { domain },
            );
        }
    }
    (first_uid, first_name, first_domain)
}

/// `oauth_poll`：前端轮询；ticket 兜底模式下顺便查一次 ticket。
pub async fn poll(pool: &Pool, session_id: &str) -> Result<Value> {
    let Some(session) = find(Some(session_id), None, None) else {
        return Err(CodeArtsError::NotFound("会话不存在或已过期，请重新发起授权".into()));
    };
    if session.status == "done" || session.status == "error" {
        return Ok(session.public());
    }
    if session.ticket_fallback {
        match super::client::poll_ticket(&session.ticket_id, &session.secret, 30.0).await {
            Ok(token) => {
                let uid = token.get("user_id").and_then(Value::as_str).unwrap_or("");
                let has_token = token["credentials"]["security_token"]
                    .as_str()
                    .map(|value| !value.is_empty())
                    .unwrap_or(false);
                if !uid.is_empty() && has_token {
                    let account = save_login(pool, &session, &token).await?;
                    update(&session.id, |s| {
                        s.status = "done".into();
                        s.message = format!("登录成功：{}", account["nickname"].as_str().unwrap_or(""));
                        s.account = Some(account.clone());
                    });
                } else {
                    update(&session.id, |s| s.message = "等待浏览器完成登录…".into());
                }
            }
            Err(err) => {
                update(&session.id, |s| {
                    s.status = "error".into();
                    s.message = err.message().to_string();
                });
            }
        }
    }
    Ok(find(Some(&session.id), None, None).map(|s| s.public()).unwrap_or(session.public()))
}

async fn exchange(pool: &Pool, session: &OAuthSession, code: &str) -> Result<Value> {
    let token = super::client::exchange_code(code, &session.verifier, session.port, &session.dpop_jwk).await?;
    save_login(pool, session, &token).await
}

fn loopback_host(host: &str) -> bool {
    matches!(host.to_lowercase().as_str(), "127.0.0.1" | "localhost" | "::1")
}

/// `_safe_oauth_redirect`：只放行 CodeArts 门户自己的地址。
fn safe_redirect(url: &str) -> bool {
    let Ok(target) = reqwest::Url::parse(url) else { return false };
    let Ok(portal) = reqwest::Url::parse(&super::portal_host()) else { return false };
    matches!(target.scheme(), "http" | "https")
        && target.scheme() == portal.scheme()
        && target.host_str() == portal.host_str()
        && target.port_or_known_default() == portal.port_or_known_default()
        && target.username().is_empty()
        && target.password().is_none()
}

fn query_value(url: &reqwest::Url, key: &str) -> String {
    url.query_pairs()
        .find(|(name, _)| name == key)
        .map(|(_, value)| value.to_string())
        .unwrap_or_default()
}

/// `oauth_callback`：面板里粘贴华为回调地址。
pub async fn callback(pool: &Pool, session_id: &str, callback_url: &str) -> Result<Value> {
    let Some(session) = find(Some(session_id), None, None) else {
        return Err(CodeArtsError::NotFound("会话不存在或已过期，请重新发起授权".into()));
    };
    let parsed = reqwest::Url::parse(callback_url.trim())
        .map_err(|_| CodeArtsError::BadRequest("只接受华为返回的 127.0.0.1 OAuth 回调地址".into()))?;
    if parsed.scheme() != "http"
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.path() != REDIRECT_PATH
        || !loopback_host(parsed.host_str().unwrap_or(""))
    {
        return Err(CodeArtsError::BadRequest("只接受华为返回的 127.0.0.1 OAuth 回调地址".into()));
    }
    if let Some(port) = parsed.port() {
        if session.port != 0 && port != session.port {
            return Err(CodeArtsError::BadRequest("回调端口与当前授权会话不匹配".into()));
        }
    }
    let code = query_value(&parsed, "code");
    let secret = query_value(&parsed, "secret");
    let redirect = query_value(&parsed, "redirect");
    if !code.is_empty() {
        let account = exchange(pool, &session, &code).await?;
        update(&session.id, |s| {
            s.status = "done".into();
            s.message = format!("登录成功：{}", account["nickname"].as_str().unwrap_or(""));
            s.account = Some(account.clone());
        });
        return Ok(find(Some(&session.id), None, None).map(|s| s.public()).unwrap_or(session.public()));
    }
    if !secret.is_empty() && !redirect.is_empty() {
        if !safe_redirect(&redirect) {
            return Err(CodeArtsError::BadRequest("授权跳转地址不属于 CodeArts 门户".into()));
        }
        update(&session.id, |s| {
            s.secret = secret;
            s.ticket_fallback = true;
            s.message = "回调已接收，继续等待授权结果…".into();
        });
        return Ok(json!({
            "ok": true, "status": "continue", "next_url": redirect,
            "message": "回调已接收，继续等待授权结果…",
        }));
    }
    Err(CodeArtsError::BadRequest("回调地址缺少 code 或 secret".into()))
}

/// `oauth_callback_get`：浏览器 GET `/oauth/callback`，回 HTML。
pub async fn callback_get(pool: &Pool, query: &[(String, String)]) -> (u16, String) {
    let value = |key: &str| -> String {
        query
            .iter()
            .find(|(name, _)| name == key)
            .map(|(_, value)| value.clone())
            .unwrap_or_default()
    };
    let ticket_id = value("ticket_id");
    let callback_secret = value("secret");
    if ticket_id.is_empty() && callback_secret.is_empty() {
        return (400, "<h3>回调缺少会话标识，请回到看板粘贴完整回调地址。</h3>".into());
    }
    let Some(session) = find(None, Some(&ticket_id), Some(&callback_secret)) else {
        return (400, "<h3>登录会话不存在或已过期，请回到看板重新发起。</h3>".into());
    };
    let code = value("code");
    let secret = value("secret");
    let redirect = value("redirect");
    if !code.is_empty() {
        match exchange(pool, &session, &code).await {
            Ok(_) => {
                update(&session.id, |s| {
                    s.status = "done".into();
                    s.message = "登录成功".into();
                });
                return (200, "<h3>CodeArts 登录成功，可以关闭此页面。</h3>".into());
            }
            Err(err) => {
                update(&session.id, |s| {
                    s.status = "error".into();
                    s.message = err.message().to_string();
                });
                return (400, format!("<h3>登录失败：{}</h3>", html_escape(err.message())));
            }
        }
    }
    if !secret.is_empty() && !redirect.is_empty() {
        if !safe_redirect(&redirect) {
            return (400, "<h3>登录失败：授权跳转地址不属于 CodeArts 门户</h3>".into());
        }
        update(&session.id, |s| {
            s.secret = secret;
            s.ticket_fallback = true;
            s.message = "回调已接收，继续等待授权结果…".into();
        });
        return (
            200,
            format!(
                "<meta http-equiv=\"refresh\" content=\"0;url={}\"><h3>正在继续华为云授权…</h3>",
                html_escape(&redirect)
            ),
        );
    }
    (400, "<h3>回调地址缺少 code 或 secret。</h3>".into())
}

fn html_escape(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identity_comes_from_the_jwt_subject() {
        // {"sub":"user-42"}
        let payload = "eyJzdWIiOiJ1c2VyLTQyIn0";
        assert_eq!(identity_from_refresh_token(&format!("h.{payload}.s")), "user-42");
        assert_eq!(identity_from_refresh_token("not-a-jwt"), "");
        assert_eq!(identity_from_refresh_token(""), "");
    }

    #[test]
    fn redirect_must_point_at_the_portal() {
        assert!(safe_redirect("https://codearts.huaweicloud.com/portal/next"));
        assert!(!safe_redirect("https://evil.example.com/portal"));
        assert!(!safe_redirect("http://codearts.huaweicloud.com/portal"));
        assert!(!safe_redirect("javascript:alert(1)"));
        let url = reqwest::Url::parse("http://127.0.0.1:8788/oauth/callback?code=abc").unwrap();
        assert_eq!(query_value(&url, "code"), "abc");
    }

    #[test]
    fn session_lookup_prefers_the_newest_pending_one() {
        let first = start(8788);
        let second = start(8788);
        let found = find(None, None, None).expect("应该能找到 pending 会话");
        assert_eq!(found.id, second["session_id"].as_str().unwrap());
        assert_ne!(first["session_id"], second["session_id"]);
        let by_id = find(Some(first["session_id"].as_str().unwrap()), None, None).unwrap();
        assert_eq!(by_id.id, first["session_id"].as_str().unwrap());
    }
}
