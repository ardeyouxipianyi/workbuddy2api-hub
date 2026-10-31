//! 账号池：选号、冷却、令牌刷新。
//!
//! 契约见 specs/account-pool.md（选号顺序 §3、401/429 分支 §4、刷新请求 §5）。
//! 与 Python 版的差异是刻意的：Python 在内存里维护账号对象，Rust 每次都从文件读，
//! 只有「冷却到什么时候」放内存（重启即清空，代价是重启后可能再撞一次 429）。

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};

use crate::pool_breaker::{BreakerView, Breakers, FailureKind};
use crate::pool_guards::{AccountGuards, Guards};
use crate::state::{now_epoch, AccountFile, State};
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
    /// uid -> explicit cooldown deadline (epoch seconds).
    cooldown: Mutex<HashMap<String, f64>>,
    /// uid -> 账号级软限流连击（撞线时刻 + 次数）。窗口外就重新从 1 数起。
    soft: Mutex<HashMap<String, Streak>>,
    /// (uid, model) -> 未定域软限流连击。
    unscoped: Mutex<HashMap<(String, String), Streak>>,
    /// (uid, model) -> 这个模型在这个账号上的冷却截止时刻。
    model_cooldown: Mutex<HashMap<(String, String), f64>>,
    breakers: Breakers,
    guards: Guards,
    /// Refresh tokens rotate upstream, so requests for one account must run
    /// serially and re-read the file after the preceding rotation.
    refreshes: Mutex<HashMap<String, Arc<tokio::sync::Mutex<()>>>>,
}

/// 软限流的连击计数：Python 的 `_decay_soft_streak` 要求窗口内才算连续。
#[derive(Default, Clone, Copy)]
struct Streak {
    count: i64,
    last_at: f64,
}

/// 两次软限流间隔超过它，连击归零（`WB_FAIL_WINDOW` 的默认值）。
const SOFT_WINDOW: f64 = 600.0;

impl Pool {
    pub fn new() -> Pool {
        Pool {
            cooldown: Mutex::new(HashMap::new()),
            soft: Mutex::new(HashMap::new()),
            unscoped: Mutex::new(HashMap::new()),
            model_cooldown: Mutex::new(HashMap::new()),
            breakers: Breakers::default(),
            guards: Guards::default(),
            refreshes: Mutex::new(HashMap::new()),
        }
    }

    pub fn cooling(&self, uid: &str) -> bool {
        let now = now_epoch() as f64;
        self.cooldown_until(uid) > now || self.breakers.cooling(uid, now)
    }

    pub fn cooldown(&self, uid: &str, secs: i64) {
        self.cooldown
            .lock()
            .unwrap()
            .insert(uid.to_string(), now_epoch() as f64 + secs as f64);
    }

    pub fn clear(&self, uid: &str) {
        self.cooldown.lock().unwrap().remove(uid);
        // 一次真的被上游服务的请求，说明软限流也过去了（Python 的 note_success）。
        self.soft.lock().unwrap().remove(uid);
        self.unscoped.lock().unwrap().retain(|(owner, _), _| owner != uid);
        self.model_cooldown.lock().unwrap().retain(|(owner, _), _| owner != uid);
        self.breakers.success(uid);
    }

    /// 账号级软限流：600s 起，连击翻倍，2h 封顶。返回这次要冷却多久。
    pub fn note_soft_rate(&self, uid: &str) -> f64 {
        let wait = {
            let now = now_epoch() as f64;
            let mut map = self.soft.lock().unwrap();
            let streak = map.entry(uid.to_string()).or_default();
            if streak.last_at > 0.0 && now - streak.last_at > SOFT_WINDOW {
                streak.count = 0;
            }
            streak.count += 1;
            streak.last_at = now;
            crate::rate_limit::soft_backoff(streak.count)
        };
        self.cooldown(uid, wait as i64);
        wait
    }

    /// 未定域的软限流：只关这个账号的这个模型，60s 起翻倍，10min 封顶。
    pub fn note_unscoped_rate(&self, uid: &str, model: &str) -> f64 {
        let wait = {
            let now = now_epoch() as f64;
            let mut map = self.unscoped.lock().unwrap();
            let streak = map.entry((uid.to_string(), model.to_string())).or_default();
            if streak.last_at > 0.0 && now - streak.last_at > SOFT_WINDOW {
                streak.count = 0;
            }
            streak.count += 1;
            streak.last_at = now;
            crate::rate_limit::unscoped_backoff(streak.count)
        };
        self.cool_model(uid, model, wait);
        wait
    }

    pub fn cool_model(&self, uid: &str, model: &str, secs: f64) {
        self.model_cooldown
            .lock()
            .unwrap()
            .insert((uid.to_string(), model.to_string()), now_epoch() as f64 + secs);
    }

    pub fn model_cooling(&self, uid: &str, model: &str) -> bool {
        let until = self
            .model_cooldown
            .lock()
            .unwrap()
            .get(&(uid.to_string(), model.to_string()))
            .copied()
            .unwrap_or(0.0);
        until > now_epoch() as f64
    }

    pub fn hard_failure(&self, uid: &str) {
        self.breakers
            .failure(uid, FailureKind::Hard, now_epoch() as f64);
    }

    pub fn unknown_failure(&self, uid: &str) {
        self.breakers
            .failure(uid, FailureKind::Unknown, now_epoch() as f64);
    }

    pub fn breaker_view(&self, uid: &str) -> BreakerView {
        self.breakers.view(uid, now_epoch() as f64)
    }

    pub fn fail_window(&self) -> i64 {
        self.breakers.fail_window() as i64
    }

    pub fn cooldown_for(&self, uid: &str) -> Option<i64> {
        let left = (self.cooldown_until(uid) - now_epoch() as f64).round() as i64;
        (left > 0).then_some(left)
    }

    pub fn account_guards(
        &self,
        uid: &str,
        realm: &str,
        credits: Option<&Value>,
    ) -> AccountGuards {
        self.guards.account(uid, realm, credits)
    }

    pub fn refresh_guards(&self, state: &State, force: bool) {
        state.refresh();
        let settings = state.settings_snapshot();
        let catalog = state
            .model_catalog
            .read()
            .unwrap_or_else(|error| error.into_inner())
            .clone();
        self.guards
            .refresh(&settings, &catalog, &state.usage_dir.join("usage.jsonl"), force);
    }

    fn cooldown_until(&self, uid: &str) -> f64 {
        self.cooldown
            .lock()
            .unwrap()
            .get(uid)
            .copied()
            .unwrap_or(0.0)
    }

    fn refresh_lock(&self, uid: &str) -> Arc<tokio::sync::Mutex<()>> {
        let mut locks = self.refreshes.lock().unwrap();
        locks
            .entry(uid.to_string())
            .or_insert_with(|| Arc::new(tokio::sync::Mutex::new(())))
            .clone()
    }
}

/// 给某个账号记冷却（模块级入口，调用方不用先拿到 Pool）。
pub fn cooldown(app: &Arc<App>, uid: &str, secs: i64) {
    app.pool.cooldown(uid, secs);
}

/// 候选：区域相同、enabled、有 token、未过期太久、不在冷却。
/// 返回顺序 = 账号文件名排序（与 Python 的 `sorted(os.listdir)` 一致）。
pub fn candidates(
    app: &Arc<App>,
    realm: &str,
    tried: &[String],
    model: Option<&str>,
) -> Vec<Candidate> {
    let now = now_epoch();
    app.pool.refresh_guards(&app.state, false);
    app.state
        .accounts()
        .iter()
        .filter_map(|account: &AccountFile| {
            let uid = account.uid();
            if account.realm() != realm
                || tried.contains(&uid)
                || app.pool.cooling(&uid)
                // 上游风控窗口（`wb_accounts.py:989` 的 restrictedUntil）：
                // 窗口期内的号选了也是 403，直接跳过。
                || account.restricted_until() > now as f64
                // 只被「某模型」限流的号：别的模型还能用，整条拔掉是旧的过度冷却。
                || model.is_some_and(|name| app.pool.model_cooling(&uid, name))
                || !account.enabled()
                || account.str("accessToken").is_empty()
                || (account.expires_at() != 0 && account.expires_at() <= now - 3600)
                || app.pool.guards.blocked(
                    &uid,
                    &realm,
                    account.data.get("credits"),
                    model,
                )
            {
                return None;
            }
            Some(Candidate {
                uid,
                token: account.str("accessToken").to_string(),
                realm: account.realm(),
                domain: account.domain(),
                product: account.product(),
                expires_at: account.expires_at(),
                is_static_key: account.is_api_key(),
            })
        })
        .collect()
}

pub fn count_ready(app: &Arc<App>, realm: &str, model: Option<&str>) -> usize {
    if realm == "all" {
        candidates(app, "intl", &[], model).len() + candidates(app, "cn", &[], model).len()
    } else {
        candidates(app, realm, &[], model).len()
    }
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
        let base = std::env::var("WB_CN_CHAT_UPSTREAM")
            .ok()
            .filter(|v| {
                (v.starts_with("http://") || v.starts_with("https://")) && !v.trim().is_empty()
            })
            .unwrap_or_else(|| "https://copilot.tencent.com".into());
        (
            base,
            std::env::var("WB_CN_ORIGIN")
                .unwrap_or_else(|_| "https://www.codebuddy.cn".into()),
            std::env::var("WB_CN_DOMAIN").unwrap_or_else(|_| "copilot.tencent.com".into()),
            "WorkBuddy/5.8.3 WorkBuddy/5.8.3 CLI/2.161.0".into(),
            "WorkBuddy/5.8.3".into(),
        )
    } else {
        (
            crate::upstream::chat_upstream_base("intl", "workbuddy"),
            std::env::var("WB_INTL_ORIGIN")
                .unwrap_or_else(|_| "https://www.workbuddy.ai".into()),
            std::env::var("WB_INTL_DOMAIN").unwrap_or_else(|_| "www.workbuddy.ai".into()),
            "WorkBuddy/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0".into(),
            "WorkBuddy/5.8.4".into(),
        )
    }
}

/// 刷新令牌：`POST {chat_upstream}/v2/plugin/auth/token/refresh`（wb_accounts.py:2019-2081）。
/// 成功返回新 accessToken，失败返回 None（错误留在账号文件的 lastError 里）。
pub async fn refresh(app: &Arc<App>, uid: &str) -> Option<String> {
    let refresh_lock = app.pool.refresh_lock(uid);
    let _guard = refresh_lock.lock().await;
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
        .timeout(refresh_timeout());
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

fn refresh_timeout() -> Duration {
    let seconds = std::env::var("WB_REFRESH_TIMEOUT_SECONDS")
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .unwrap_or(30)
        .max(1);
    Duration::from_secs(seconds)
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn soft_rate_ladder_escalates_and_success_clears() {
        // 账号级软限流 600s 起翻倍（Python 的 soft_backoff），一次成功就归零。
        let pool = Pool::new();
        assert_eq!(pool.note_soft_rate("u1"), 600.0);
        assert_eq!(pool.note_soft_rate("u1"), 1200.0);
        assert!(pool.cooling("u1"));
        pool.clear("u1");
        assert!(!pool.cooling("u1"));
        assert_eq!(pool.note_soft_rate("u1"), 600.0);
    }

    #[test]
    fn unscoped_rate_parks_only_that_model() {
        // 没定域的 429 只关这个账号的这个模型，账号本身还能跑别的模型。
        let pool = Pool::new();
        assert_eq!(pool.note_unscoped_rate("u1", "glm-5.1"), 60.0);
        assert_eq!(pool.note_unscoped_rate("u1", "glm-5.1"), 120.0);
        assert!(pool.model_cooling("u1", "glm-5.1"));
        assert!(!pool.model_cooling("u1", "deepseek-v4-pro"));
        assert!(!pool.cooling("u1"));
    }
}
