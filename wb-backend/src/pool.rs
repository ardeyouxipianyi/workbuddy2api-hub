//! 账号池：选号、冷却、令牌刷新。
//!
//! 契约见 specs/account-pool.md（选号顺序 §3、401/429 分支 §4、刷新请求 §5）。
//! 与 Python 版的差异是刻意的：Python 在内存里维护账号对象，Rust 每次都从文件读，
//! 只有「冷却到什么时候」放内存（重启即清空，代价是重启后可能再撞一次 429）。

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};

use crate::state::{now_epoch, AccountFile};
use crate::App;

/// 一个可用候选（选号结果）。
pub struct Candidate {
    pub uid: String,
    pub token: String,
    pub realm: String,
    pub domain: String,
    pub product: String,
    pub expires_at: i64,
    pub is_static_key: bool,
}

pub struct Pool {
    /// uid -> 冷却到什么时候（epoch 秒）。
    cooldown: Mutex<HashMap<String, i64>>,
}

impl Pool {
    pub fn new() -> Pool {
        Pool { cooldown: Mutex::new(HashMap::new()) }
    }

    pub fn cooling(&self, uid: &str) -> bool {
        let map = self.cooldown.lock().unwrap();
        map.get(uid).map(|until| *until > now_epoch()).unwrap_or(false)
    }

    pub fn cooldown(&self, uid: &str, secs: i64) {
        self.cooldown.lock().unwrap().insert(uid.to_string(), now_epoch() + secs);
    }

    pub fn clear(&self, uid: &str) {
        self.cooldown.lock().unwrap().remove(uid);
    }
}

/// 给某个账号记冷却（模块级入口，调用方不用先拿到 Pool）。
pub fn cooldown(app: &Arc<App>, uid: &str, secs: i64) {
    app.pool.cooldown(uid, secs);
}

/// 候选：区域相同、enabled、有 token、未过期太久、不在冷却。
/// 返回顺序 = 账号文件名排序（与 Python 的 `sorted(os.listdir)` 一致）。
pub fn candidates(app: &Arc<App>, realm: &str, tried: &[String]) -> Vec<Candidate> {
    let now = now_epoch();
    app.state
        .accounts()
        .iter()
        .filter(|a| a.realm() == realm)
        .filter(|a| !tried.contains(&a.uid()))
        .filter(|a| !app.pool.cooling(&a.uid()))
        .filter(|a| a.enabled())
        .filter(|a| !a.str("accessToken").is_empty())
        .filter(|a| a.expires_at() == 0 || a.expires_at() > now - 3600)
        .map(|a: &AccountFile| Candidate {
            uid: a.uid(),
            token: a.str("accessToken").to_string(),
            realm: a.realm(),
            domain: a.domain(),
            product: a.product(),
            expires_at: a.expires_at(),
            is_static_key: a.is_api_key(),
        })
        .collect()
}

/// `ready()` 的临期分支：`0 < exp-now <= 120` 或已过期就刷新（wb_accounts.py:1835-1848）。
/// 返回本次请求该用的 token（刷新失败就沿用旧的，让上游去判 401）。
pub async fn ensure_fresh(app: &Arc<App>, cand: &Candidate) -> String {
    if cand.is_static_key || cand.expires_at == 0 {
        return cand.token.clone();
    }
    if cand.expires_at - now_epoch() > 120 {
        return cand.token.clone();
    }
    match refresh(app, &cand.uid).await {
        Some(token) => token,
        None => cand.token.clone(),
    }
}

fn realm_base(realm: &str) -> (String, String, String, String, String) {
    // (chat_upstream, origin, domain, chat_ua, billing_ua) —— REALM_CONFIGS 的 Rust 版。
    if realm == "cn" {
        (
            std::env::var("WB_CN_CHAT_UPSTREAM")
                .ok()
                .filter(|v| {
                    (v.starts_with("http://") || v.starts_with("https://")) && !v.trim().is_empty()
                })
                .unwrap_or_else(|| "https://copilot.tencent.com".into()),
            "https://www.codebuddy.cn".into(),
            "copilot.tencent.com".into(),
            "WorkBuddy/5.8.3 WorkBuddy/5.8.3 CLI/2.161.0".into(),
            "WorkBuddy/5.8.3".into(),
        )
    } else {
        (
            "https://www.workbuddy.ai".into(),
            "https://www.workbuddy.ai".into(),
            "www.workbuddy.ai".into(),
            "WorkBuddy/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0".into(),
            "WorkBuddy/5.8.4".into(),
        )
    }
}

/// 刷新令牌：`POST {chat_upstream}/v2/plugin/auth/token/refresh`（wb_accounts.py:2019-2081）。
/// 成功返回新 accessToken，失败返回 None（错误留在账号文件的 lastError 里）。
pub async fn refresh(app: &Arc<App>, uid: &str) -> Option<String> {
    let account = {
        let accounts = app.state.accounts();
        accounts.iter().find(|a| a.uid() == uid).cloned()?
    };
    let refresh_token = account.str("refreshToken").to_string();
    if refresh_token.is_empty() {
        // 静态 key 不能刷；没有 refreshToken 的号也刷不了（wb_accounts.py:2032-2035）。
        return None;
    }
    let realm = account.realm();
    let (base, origin, domain, _chat_ua, billing_ua) = realm_base(&realm);
    let url = format!("{base}/v2/plugin/auth/token/refresh");
    let mut req = app
        .upstream
        .post(&url)
        .header("Content-Type", "application/json")
        .header("Accept", "application/json, text/plain, */*")
        .header("X-Requested-With", "XMLHttpRequest")
        .header("User-Agent", billing_ua)
        .header("Origin", origin.clone())
        .header("Referer", format!("{origin}/"))
        .header("X-Refresh-Token", &refresh_token)
        .header("X-Auth-Refresh-Source", if realm == "cn" { "workbuddy" } else { "plugin" })
        .header("X-User-Id", uid)
        .header(
            "X-Domain",
            if account.domain().is_empty() { domain } else { account.domain() },
        )
        .header("X-CodeBuddy-Request", "1")
        .header("Accept-Language", if realm == "intl" { "en-US" } else { "zh-CN" })
        .body("{}")
        .timeout(Duration::from_secs(30));
    let enterprise = account.str("enterpriseId").to_string();
    if !enterprise.is_empty() {
        req = req.header("X-Enterprise-Id", enterprise);
    }
    let payload: Value = match req.send().await {
        Ok(resp) => match resp.json().await {
            Ok(v) => v,
            Err(e) => {
                eprintln!("wb-backend: refresh returned non-json: {e}");
                return None;
            }
        },
        Err(e) => {
            eprintln!("wb-backend: refresh failed: {e}");
            return None;
        }
    };
    // 两层 data 兼容：{"data":{"data":{...}}} 与 {"data":{...}}（wb_accounts.py:2065-2066）。
    let data = payload.get("data").cloned().unwrap_or(json!({}));
    let data = data.get("data").cloned().unwrap_or(data);
    let token = match data.get("accessToken").and_then(Value::as_str) {
        Some(t) if !t.is_empty() => t.to_string(),
        _ => {
            eprintln!("wb-backend: refresh returned no token: {}", payload.get("msg").unwrap_or(&Value::Null));
            return None;
        }
    };
    let new_refresh = data
        .get("refreshToken")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .unwrap_or(refresh_token);
    let expires = jwt_exp(&token).unwrap_or(account.expires_at());

    if let Err(e) = save_credentials(&account, &token, &new_refresh, expires) {
        eprintln!("wb-backend: could not persist refreshed token for {uid}: {e}");
        // 落盘失败仍返回 token：本次请求能用，下次重启前靠旧文件兜底。
    }
    app.state.refresh();
    Some(token)
}

/// 只改凭证四个键，其余原样保留（账号文件里还有 quota/权重等一堆字段）。
/// 原子写：临时文件 + rename，避免半截文件把账号搞坏。
fn save_credentials(
    account: &AccountFile,
    access: &str,
    refresh_token: &str,
    expires_at: i64,
) -> std::io::Result<()> {
    let text = std::fs::read_to_string(&account.path)?;
    let mut data: serde_json::Map<String, Value> = match serde_json::from_str(&text) {
        Ok(Value::Object(map)) => map,
        _ => return Err(std::io::Error::new(std::io::ErrorKind::InvalidData, "account not an object")),
    };
    data.insert("accessToken".into(), json!(access));
    data.insert("refreshToken".into(), json!(refresh_token));
    data.insert("expiresAt".into(), json!(expires_at));
    let tmp = account.path.with_extension("json.tmp");
    let body = serde_json::to_vec_pretty(&Value::Object(data))?;
    std::fs::write(&tmp, body)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600));
    }
    std::fs::rename(&tmp, &account.path)
}

/// JWT 的 `exp`（不验签，只读载荷）。取不到就 None。
fn jwt_exp(token: &str) -> Option<i64> {
    let payload = token.split('.').nth(1)?;
    let bytes = b64url_decode(payload)?;
    let value: Value = serde_json::from_slice(&bytes).ok()?;
    value.get("exp").and_then(Value::as_i64)
}

fn b64url_decode(text: &str) -> Option<Vec<u8>> {
    const TABLE: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = Vec::new();
    let mut acc: u32 = 0;
    let mut bits = 0;
    for byte in text.bytes() {
        let value = match TABLE.iter().position(|c| *c == byte) {
            Some(v) => v as u32,
            None => continue,
        };
        acc = (acc << 6) | value;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((acc >> bits) as u8);
        }
    }
    Some(out)
}
