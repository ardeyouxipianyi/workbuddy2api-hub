//! CodeArts 出口（上游 v1.10.0 的第三个 realm）：配置、账号文件、账号池。
//!
//! 对照 `wb_codearts.py` 的 load_config/save_config/load_auths/save_auth/
//! CodeArtsAccount/CodeArtsPool 移植；聊天链路在 `client.rs`/`models.rs`/`stream.rs`，
//! 面板接口在 `panel.rs`。

pub mod admin;
pub mod chat;
pub mod client;
pub mod crypto;
pub mod models;
pub mod oauth;
pub mod panel;
pub mod stream;

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

use serde_json::{json, Map, Value};

use crate::state::write_json_atomic;

// ---------------------------------------------------------------------------
// 常量（wb_codearts.py 顶部）
// ---------------------------------------------------------------------------

pub fn snap_host() -> String {
    std::env::var("WB_CODEARTS_SNAP_HOST")
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| "https://snap-access.cn-north-4.myhuaweicloud.com".into())
}

pub fn snap_manager() -> String {
    std::env::var("WB_CODEARTS_SNAP_MANAGER")
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| format!("{}/snap-manager", snap_host()))
}

pub fn portal_host() -> String {
    std::env::var("WB_CODEARTS_PORTAL_HOST")
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| "https://codearts.huaweicloud.com/portal".into())
}

pub fn sts_host() -> String {
    std::env::var("WB_CODEARTS_STS_HOST")
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| "https://sts.cn-north-4.myhuaweicloud.com".into())
}

pub fn benefit_host() -> String {
    std::env::var("WB_CODEARTS_BENEFIT_HOST")
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| "https://opengw.developer.huaweicloud.com".into())
}

pub const CLIENT_ID: &str = "codearts-agent";
pub const PLUGIN_NAME: &str = "snap_AIIDE";
pub const PLUGIN_VERSION: &str = "5.2.0";
pub const REDIRECT_PATH: &str = "/oauth/callback";

pub const EP_CHAT: &str = "/api/v2/chat/completions";
pub const EP_OAUTH_TOKENS: &str = "/v1/oauth2/tokens";
pub const EP_LOGIN_TICKET: &str = "/v1/login/ticket";
pub const EP_MODEL_BUILTIN: &str = "/v1/model/builtin";
pub const EP_AGENT_LIST: &str = "/v1/agent-center/agents/useragents";
pub const EP_AGENT_DETAIL: &str = "/v1/agent-center/agents/detail";
pub const EP_BENEFIT_CONFIG: &str = "/api/v1/gateway/config";
pub const EP_BENEFIT_CLAIM: &str = "/api/v1/benefit/claim";
pub const EP_CREDIT_DELIVERY: &str = "/v1/ops/delivery";
pub const EP_CREDIT_CLAIM: &str = "/v1/ops/claim";
pub const EP_CREDIT_CONFIRM: &str = "/v1/ops/confirm";
pub const EP_PACKAGE_INFO: &str = "/v1/statistics/plugin";

pub const HEADER_MAAS_TYPE: &str = "maas_type";
pub const MAAS_BENEFIT: &str = "benefit";

pub const SEED_BENEFIT_MODELS: [&str; 3] =
    ["deepseek-v4-flash-0731", "deepseek-v4-pro-0813", "glm-5.3-flash"];
pub const SEED_KNOWN_MODELS: [&str; 9] = [
    "GLM-5.2",
    "GLM-5.2-ArkTS-SPARK",
    "GLM-5.1",
    "GLM-4.7",
    "OpenPangu-2.0-Pro",
    "OpenPangu-2.0-Flash",
    "Qwen3-VL-235B",
    "Qwen3.5-397B-A17B-VL",
    "Qwen3.6-27B-VL",
];

/// 目录缓存上限与单账号最短发现间隔。
pub const CATALOG_TTL: f64 = 3600.0;
pub const CATALOG_MIN_INTERVAL: f64 = 60.0;
/// 错误阈值与冷却。
pub const ERR_THRESHOLD: i64 = 3;
pub const ERR_COOLDOWN: f64 = 600.0;
pub const SOFT_COOLDOWN: f64 = 60.0;
/// token 提前刷新窗口。
pub const REFRESH_SKEW: f64 = 1800.0;
/// OAuth 会话 TTL。
pub const OAUTH_TTL: f64 = 600.0;
/// 错误体读取上限。
pub const ERROR_BODY_MAX: usize = 256 * 1024;

// ---------------------------------------------------------------------------
// 异常
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub enum CodeArtsError {
    Unavailable(String),
    Auth(String),
    BadRequest(String),
    NotFound(String),
    Conflict(String),
    Upstream(String),
    Other(String),
}

impl CodeArtsError {
    pub fn message(&self) -> &str {
        match self {
            CodeArtsError::Unavailable(m)
            | CodeArtsError::Auth(m)
            | CodeArtsError::BadRequest(m)
            | CodeArtsError::NotFound(m)
            | CodeArtsError::Conflict(m)
            | CodeArtsError::Upstream(m)
            | CodeArtsError::Other(m) => m,
        }
    }

    /// `_json_http` 与面板路由用的状态码映射。
    pub fn status(&self) -> u16 {
        match self {
            CodeArtsError::Unavailable(_) => 503,
            CodeArtsError::Auth(_) => 401,
            CodeArtsError::BadRequest(_) => 400,
            CodeArtsError::NotFound(_) => 404,
            CodeArtsError::Conflict(_) => 409,
            CodeArtsError::Upstream(_) => 502,
            CodeArtsError::Other(_) => 502,
        }
    }

    /// 面板/网关接受的错误类型（与 wb_proxy 的 `_error` 用的 type 一致）。
    pub fn error_type(&self) -> &'static str {
        match self {
            CodeArtsError::BadRequest(_) => "invalid_request_error",
            CodeArtsError::Auth(_) => "authentication_error",
            _ => "api_error",
        }
    }
}

impl std::fmt::Display for CodeArtsError {
    fn fmt(&self, fmt: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        fmt.write_str(self.message())
    }
}

pub type Result<T> = std::result::Result<T, CodeArtsError>;

// ---------------------------------------------------------------------------
// 配置
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
pub struct Config {
    pub enabled: bool,
    pub timeout_seconds: f64,
    pub stream_timeout_seconds: f64,
    pub models_ttl_seconds: f64,
    pub max_concurrent: i64,
    pub benefit_auto_claim: bool,
    pub credits_auto_claim: bool,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            enabled: false,
            timeout_seconds: 30.0,
            stream_timeout_seconds: 600.0,
            models_ttl_seconds: 300.0,
            max_concurrent: 1,
            benefit_auto_claim: true,
            credits_auto_claim: true,
        }
    }
}

fn positive_float(value: Option<&Value>, default: f64, low: f64, high: f64) -> f64 {
    let parsed = match value {
        Some(Value::Number(number)) => number.as_f64(),
        Some(Value::String(text)) => text.trim().parse::<f64>().ok(),
        Some(Value::Bool(flag)) => Some(if *flag { 1.0 } else { 0.0 }),
        _ => None,
    };
    parsed.filter(|value| value.is_finite()).unwrap_or(default).clamp(low, high)
}

fn positive_int(value: Option<&Value>, default: i64, low: i64, high: i64) -> i64 {
    let parsed = match value {
        Some(Value::Number(number)) => number.as_i64().or_else(|| number.as_f64().map(|v| v as i64)),
        Some(Value::String(text)) => text.trim().parse::<i64>().ok(),
        Some(Value::Bool(flag)) => Some(if *flag { 1 } else { 0 }),
        _ => None,
    };
    parsed.unwrap_or(default).clamp(low, high)
}

/// `load_config`：非法值一律回落默认，绝不因为配置坏了挡住网关启动。
pub fn config_from(settings: &Map<String, Value>) -> Config {
    let raw = settings.get("codearts");
    let field = |key: &str| raw.and_then(|value| value.get(key));
    let defaults = Config::default();
    Config {
        enabled: field("enabled").and_then(Value::as_bool).unwrap_or(false),
        timeout_seconds: positive_float(field("timeout_seconds"), defaults.timeout_seconds, 1.0, 600.0),
        stream_timeout_seconds: positive_float(
            field("stream_timeout_seconds"),
            defaults.stream_timeout_seconds,
            1.0,
            3600.0,
        ),
        models_ttl_seconds: positive_float(
            field("models_ttl_seconds"),
            defaults.models_ttl_seconds,
            60.0,
            86400.0,
        ),
        max_concurrent: positive_int(field("max_concurrent"), defaults.max_concurrent, 1, 20),
        benefit_auto_claim: field("benefit_auto_claim").and_then(Value::as_bool).unwrap_or(true),
        credits_auto_claim: field("credits_auto_claim").and_then(Value::as_bool).unwrap_or(true),
    }
}

impl Config {
    pub fn to_json(&self) -> Value {
        json!({
            "enabled": self.enabled,
            "timeout_seconds": self.timeout_seconds,
            "stream_timeout_seconds": self.stream_timeout_seconds,
            "models_ttl_seconds": self.models_ttl_seconds,
            "max_concurrent": self.max_concurrent,
            "benefit_auto_claim": self.benefit_auto_claim,
            "credits_auto_claim": self.credits_auto_claim,
        })
    }
}

/// `save_config`：只收 `codearts` 里已知的七个键，写回 settings.json。
pub fn save_config(app: &Arc<crate::App>, patch: &Map<String, Value>) -> Result<Config> {
    let mut merged = config_from(&app.state.settings_snapshot()).to_json();
    for key in merged.as_object().map(|map| map.keys().cloned().collect::<Vec<_>>()).unwrap_or_default() {
        if let Some(value) = patch.get(&key) {
            merged[&key] = value.clone();
        }
    }
    let before = "intl".to_string();
    let _ = before;
    // 收敛：把 patch 里的原值丢给 config_from 再规整一遍。
    let mut holder = Map::new();
    holder.insert("codearts".into(), merged);
    let config = config_from(&holder);

    let mut settings = app.state.settings_snapshot();
    settings.insert("codearts".into(), config.to_json());
    let path = app.state.accounts_dir.join("settings.json");
    write_json_atomic(&path, &Value::Object(settings))
        .map_err(|err| CodeArtsError::Other(format!("写入 settings.json 失败：{err}")))?;
    app.state.reload();
    Ok(config)
}

// ---------------------------------------------------------------------------
// 账号文件
// ---------------------------------------------------------------------------

pub fn accounts_path(accounts_dir: &Path) -> PathBuf {
    accounts_dir.join("codearts")
}

fn auth_filename(uid: &str) -> String {
    let safe = if uid.is_empty() { "unknown".to_string() } else { uid.replace('/', "_") };
    format!("codearts-{safe}.json")
}

/// `load_auths`：读 `accounts/codearts/codearts-*.json`，跳过坏文件。
pub fn load_auths(accounts_dir: &Path) -> Vec<(PathBuf, Value)> {
    let folder = accounts_path(accounts_dir);
    let mut out = Vec::new();
    let Ok(entries) = std::fs::read_dir(&folder) else { return out };
    let mut names: Vec<PathBuf> = entries.filter_map(|entry| entry.ok().map(|e| e.path())).collect();
    names.sort();
    for path in names {
        let name = path.file_name().and_then(|v| v.to_str()).unwrap_or("").to_string();
        if !name.starts_with("codearts-") || !name.ends_with(".json") {
            continue;
        }
        let Ok(raw) = std::fs::read_to_string(&path) else { continue };
        let Ok(data) = serde_json::from_str::<Value>(&raw) else { continue };
        if !data.is_object() {
            continue;
        }
        let token = data.get("cloud_dragon_token").and_then(Value::as_str).unwrap_or("");
        let uid = data.get("user_id").and_then(Value::as_str).unwrap_or("");
        if token.is_empty() || uid.is_empty() {
            continue;
        }
        out.push((path, data));
    }
    out
}

/// `save_auth`：原子写回；`_path` 由调用方给，缺省按 user_id 命名。
pub fn save_auth(accounts_dir: &Path, path: Option<&Path>, auth: &Value) -> Result<PathBuf> {
    let target = match path {
        Some(path) => path.to_path_buf(),
        None => {
            let uid = auth.get("user_id").and_then(Value::as_str).unwrap_or("");
            accounts_path(accounts_dir).join(auth_filename(uid))
        }
    };
    let mut doc = auth.clone();
    if let Some(map) = doc.as_object_mut() {
        map.remove("_path");
    }
    if let Some(parent) = target.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    write_json_atomic(&target, &doc)
        .map_err(|err| CodeArtsError::Other(format!("写入账号文件失败：{err}")))?;
    Ok(target)
}

pub fn delete_auth(accounts_dir: &Path, uid: &str) -> bool {
    std::fs::remove_file(accounts_path(accounts_dir).join(auth_filename(uid))).is_ok()
}

/// `_parse_expiration`：ISO8601（可带 Z）→ unix 秒；解析不了按 0。
pub fn parse_expiration(value: &str) -> f64 {
    let text = value.trim();
    if text.is_empty() {
        return 0.0;
    }
    let normalized = text.replace('Z', "+00:00");
    // 只要 `YYYY-MM-DDTHH:MM:SS` 前缀，时区偏移单独算。
    let (body, offset) = match normalized.char_indices().nth(19) {
        Some((index, _)) => normalized.split_at(index),
        None => (normalized.as_str(), ""),
    };
    let parsed = chrono_lite_parse(body);
    let Some(mut stamp) = parsed else { return 0.0 };
    stamp -= offset_seconds(offset);
    stamp
}

/// `YYYY-MM-DDTHH:MM:SS` → unix 秒（无时区）。只服务 expiration 字段。
fn chrono_lite_parse(body: &str) -> Option<f64> {
    let body = body.trim();
    let (date, time) = body.split_once('T').or_else(|| body.split_once(' '))?;
    let mut parts = date.split('-');
    let year: i64 = parts.next()?.parse().ok()?;
    let month: i64 = parts.next()?.parse().ok()?;
    let day: i64 = parts.next()?.parse().ok()?;
    let mut clock = time.split(':');
    let hour: i64 = clock.next()?.parse().ok()?;
    let minute: i64 = clock.next()?.parse().ok()?;
    let second: f64 = clock.next().unwrap_or("0").parse().ok()?;
    Some(days_from_civil(year, month, day) as f64 * 86400.0
        + hour as f64 * 3600.0
        + minute as f64 * 60.0
        + second)
}

/// Howard Hinnant 的 days_from_civil（与时区无关的 UTC 天数）。
fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let year = if month <= 2 { year - 1 } else { year };
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let yoe = year - era * 400;
    let mp = (month + 9) % 12;
    let doy = (153 * mp + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

/// unix 天数 → (year, month, day)，与 `days_from_civil` 互为逆运算。
pub fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let z = days + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (if m <= 2 { y + 1 } else { y }, m, d)
}

fn offset_seconds(offset: &str) -> f64 {
    let text = offset.trim();
    if text.is_empty() {
        return 0.0;
    }
    let sign = if text.starts_with('-') { -1.0 } else { 1.0 };
    let body = text.trim_start_matches(['+', '-']);
    let mut parts = body.split(':');
    let hours: f64 = parts.next().and_then(|v| v.parse().ok()).unwrap_or(0.0);
    let minutes: f64 = parts.next().and_then(|v| v.parse().ok()).unwrap_or(0.0);
    sign * (hours * 3600.0 + minutes * 60.0)
}

/// `_fmt_duration`：`1h20m` / `45m`。
pub fn fmt_duration(seconds: f64) -> String {
    let total = seconds.max(0.0) as i64;
    let hours = total / 3600;
    let minutes = (total % 3600) / 60;
    if hours > 0 {
        format!("{hours}h{minutes}m")
    } else {
        format!("{minutes}m")
    }
}

pub fn now_secs() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

// ---------------------------------------------------------------------------
// 账号
// ---------------------------------------------------------------------------

#[derive(Clone, Default)]
struct Inner {
    auth: Value,
    nickname: String,
    proxy_slot: String,
    proxy: String,
    disabled: bool,
    disabled_reason: String,
    cool_until: f64,
    err_count: i64,
    last_error: String,
    max_concurrent: i64,
    models: Vec<Value>,
    model_benefit: HashMap<String, bool>,
    models_fetched_at: f64,
    checkin: Option<Value>,
    info: Option<Value>,
    info_fetched_at: f64,
}

pub struct Account {
    pub uid: String,
    inner: Mutex<Inner>,
    pub active: AtomicUsize,
    pub last_used: Mutex<f64>,
    /// refresh_token 是一次性的：同一账号的续期必须串行（Python 用 _refresh_lock）。
    refresh: tokio::sync::Mutex<()>,
}

impl Inner {
    fn expires_at_hint_value(&self) -> f64 {
        parse_expiration(self.auth.get("expiration").and_then(Value::as_str).unwrap_or(""))
    }
}

impl Account {
    pub fn new(auth: Value, path: Option<PathBuf>, max_concurrent: i64) -> Self {
        let uid = auth.get("user_id").and_then(Value::as_str).unwrap_or("").to_string();
        let nickname = auth
            .get("user_name")
            .and_then(Value::as_str)
            .map(|v| v.to_string())
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| uid.clone());
        let proxy_slot = auth.get("proxySlot").and_then(Value::as_str).unwrap_or("").trim().to_string();
        let mut auth = auth;
        if let Some(map) = auth.as_object_mut() {
            map.remove("_path");
            if let Some(path) = path {
                map.insert("__path".into(), json!(path.display().to_string()));
            }
        }
        Self {
            inner: Mutex::new(Inner {
                auth,
                nickname,
                proxy_slot,
                max_concurrent,
                ..Inner::default()
            }),
            uid,
            active: AtomicUsize::new(0),
            last_used: Mutex::new(0.0),
            refresh: tokio::sync::Mutex::new(()),
        }
    }

    pub fn refresh_lock(&self) -> &tokio::sync::Mutex<()> {
        &self.refresh
    }

    fn inner(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(|err| err.into_inner())
    }

    pub fn path(&self) -> Option<PathBuf> {
        self.inner()
            .auth
            .get("__path")
            .and_then(Value::as_str)
            .map(PathBuf::from)
    }

    pub fn auth(&self) -> Value {
        let mut auth = self.inner().auth.clone();
        if let Some(map) = auth.as_object_mut() {
            map.remove("__path");
        }
        auth
    }

    pub fn nickname(&self) -> String {
        self.inner().nickname.clone()
    }

    pub fn proxy(&self) -> String {
        self.inner().proxy.clone()
    }

    pub fn proxy_slot(&self) -> String {
        self.inner().proxy_slot.clone()
    }

    pub fn set_proxy_slot(&self, slot: &str) {
        self.inner().proxy_slot = slot.trim().to_string();
    }

    /// 签到后让账户信息下一轮重新拉（Python 里把 info_fetched_at 清零）。
    pub fn set_info_fetched_at_zero(&self) {
        self.inner().info_fetched_at = 0.0;
    }

    pub fn max_concurrent(&self) -> i64 {
        self.inner().max_concurrent
    }

    pub fn set_max_concurrent(&self, value: i64) {
        self.inner().max_concurrent = value.max(1);
    }

    pub fn credential(&self) -> (String, String, String) {
        let inner = self.inner();
        let get = |key: &str| inner.auth.get(key).and_then(Value::as_str).unwrap_or("").to_string();
        (get("access_key_id"), get("secret_access_key"), get("cloud_dragon_token"))
    }

    pub fn expires_at(&self) -> f64 {
        let raw = self.inner().auth.get("expiration").and_then(Value::as_str).unwrap_or("").to_string();
        parse_expiration(&raw)
    }

    pub fn token_remaining(&self) -> f64 {
        let exp = self.expires_at();
        if exp <= 0.0 {
            return 0.0;
        }
        (exp - now_secs()).max(0.0)
    }

    pub fn expired(&self) -> bool {
        let exp = self.expires_at();
        exp > 0.0 && exp <= now_secs()
    }

    pub fn expiring_soon(&self, skew: f64) -> bool {
        let exp = self.expires_at();
        exp > 0.0 && exp - now_secs() <= skew
    }

    /// `healthy`：故意不看 token 是否过期（过期号需要被选中才能续期）。
    pub fn healthy(&self, now: f64) -> bool {
        let inner = self.inner();
        !inner.disabled && now >= inner.cool_until
    }

    pub fn models(&self) -> Vec<Value> {
        self.inner().models.clone()
    }

    pub fn models_fetched_at(&self) -> f64 {
        self.inner().models_fetched_at
    }

    pub fn set_models(&self, models: Vec<Value>, fetched_at: f64) {
        let mut inner = self.inner();
        inner.model_benefit = models
            .iter()
            .map(|item| {
                let id = item.get("id").and_then(Value::as_str).unwrap_or("").to_lowercase();
                let benefit = item.get("benefit").and_then(Value::as_bool).unwrap_or(false);
                (id, benefit)
            })
            .collect();
        inner.models = models;
        inner.models_fetched_at = fetched_at;
    }

    pub fn clear_models(&self) {
        let mut inner = self.inner();
        inner.models.clear();
        inner.model_benefit.clear();
        inner.models_fetched_at = 0.0;
    }

    pub fn has_model(&self, model: &str) -> bool {
        let mid = model.trim().to_lowercase();
        if mid.is_empty() {
            return true;
        }
        let inner = self.inner();
        if inner.models.is_empty() {
            return true;
        }
        inner
            .models
            .iter()
            .any(|item| item.get("id").and_then(Value::as_str).unwrap_or("").to_lowercase() == mid)
    }

    pub fn is_benefit(&self, model: &str) -> bool {
        let mid = model.trim().to_lowercase();
        self.inner().model_benefit.get(&mid).copied().unwrap_or(false)
    }

    pub fn info(&self) -> Option<Value> {
        self.inner().info.clone()
    }

    pub fn info_fetched_at(&self) -> f64 {
        self.inner().info_fetched_at
    }

    pub fn set_info(&self, info: Value) {
        let mut inner = self.inner();
        inner.info = Some(info);
        inner.info_fetched_at = now_secs();
    }

    pub fn checkin(&self) -> Option<Value> {
        self.inner().checkin.clone()
    }

    pub fn set_checkin(&self, checkin: Option<Value>) {
        self.inner().checkin = checkin;
    }

    pub fn state_snapshot(&self) -> (bool, String, f64, i64, String) {
        let inner = self.inner();
        (
            inner.disabled,
            inner.disabled_reason.clone(),
            inner.cool_until,
            inner.err_count,
            inner.last_error.clone(),
        )
    }

    pub fn set_state(&self, disabled: Option<bool>, reason: Option<&str>, cool_until: Option<f64>, err_count: Option<i64>, last_error: Option<&str>) {
        let mut inner = self.inner();
        if let Some(value) = disabled {
            inner.disabled = value;
        }
        if let Some(value) = reason {
            inner.disabled_reason = value.to_string();
        }
        if let Some(value) = cool_until {
            inner.cool_until = value;
        }
        if let Some(value) = err_count {
            inner.err_count = value;
        }
        if let Some(value) = last_error {
            inner.last_error = value.to_string();
        }
    }

    /// `update_auth`：登录/续期后把新凭证写回账号文件。
    pub fn update_auth(&self, pool: &Pool, token: &Value) -> Result<()> {
        let creds = token.get("credentials").cloned().unwrap_or(Value::Null);
        let current = self.auth();
        let text = |value: Option<&Value>, fallback: &str| -> String {
            value.and_then(Value::as_str).filter(|v| !v.is_empty()).unwrap_or(fallback).to_string()
        };
        let uid = text(token.get("user_id"), &self.uid);
        let mut doc = current.clone();
        let map = doc.as_object_mut().ok_or_else(|| CodeArtsError::Other("账号文件损坏".into()))?;
        map.insert("user_id".into(), json!(uid));
        map.insert("user_name".into(), json!(text(token.get("user_name"), &self.nickname())));
        let domain = text(token.get("domain_id"), current.get("domain_id").and_then(Value::as_str).unwrap_or(""));
        map.insert("domain_id".into(), json!(domain));
        map.insert("cloud_dragon_token".into(), json!(text(creds.get("security_token"), "")));
        map.insert("access_key_id".into(), json!(text(creds.get("access_key_id"), "")));
        map.insert("secret_access_key".into(), json!(text(creds.get("secret_access_key"), "")));
        map.insert("expiration".into(), json!(text(creds.get("expiration"), "")));
        let refresh = text(
            token.get("refresh_token"),
            current.get("refresh_token").and_then(Value::as_str).unwrap_or(""),
        );
        map.insert("refresh_token".into(), json!(refresh));
        map.insert("updated_at".into(), json!(now_secs() as i64));

        let path = self.path();
        let saved = save_auth(&pool.accounts_dir, path.as_deref(), &doc)?;
        {
            let mut inner = self.inner();
            let mut stored = doc.clone();
            if let Some(map) = stored.as_object_mut() {
                map.insert("__path".into(), json!(saved.display().to_string()));
            }
            inner.auth = stored;
            inner.disabled = false;
            inner.disabled_reason.clear();
            inner.last_error.clear();
        }
        Ok(())
    }

    /// `view`：面板 `/codearts/accounts` 的单行。
    pub fn view(&self) -> Value {
        let now = now_secs();
        let inner = self.inner();
        let cooling = !inner.disabled && now < inner.cool_until;
        let reason = if inner.disabled_reason.is_empty() { inner.last_error.clone() } else { inner.disabled_reason.clone() };
        let info = inner.info.clone().unwrap_or(Value::Null);
        let total = info
            .get("credits")
            .and_then(|v| v.get("total"))
            .cloned()
            .unwrap_or(Value::Null);
        let credits = if total.is_object() && total.as_object().map(|m| !m.is_empty()).unwrap_or(false) {
            let number = |key: &str| total.get(key).and_then(Value::as_f64).unwrap_or(0.0);
            json!({
                "remain": number("remain"),
                "amount": number("amount"),
                "used": number("used"),
                "expiring": number("expiring"),
                "unit": "credit",
                "package": info.get("package_name").and_then(Value::as_str).unwrap_or(""),
                "end_date": info.get("end_date").and_then(Value::as_str).unwrap_or(""),
            })
        } else {
            Value::Null
        };
        let remaining = {
            let exp = inner.expires_at_hint_value();
            if exp > 0.0 { (exp - now).max(0.0) } else { 0.0 }
        };
        let expires_at = {
            let exp = inner.expires_at_hint_value();
            if exp > 0.0 { local_stamp(exp) } else { String::new() }
        };
        let until = if cooling { local_stamp(inner.cool_until) } else { String::new() };
        json!({
            "name": self.uid,
            "uid": self.uid,
            "nickname": inner.nickname,
            "user_name": inner.nickname,
            "default_model": "",
            "credits": credits,
            "package": info.get("package_name").and_then(Value::as_str).unwrap_or(""),
            "checkin": inner.checkin.clone().unwrap_or(Value::Null),
            "disabled": inner.disabled,
            "cooling": cooling,
            "until": until,
            "err_count": inner.err_count,
            "reason": reason,
            "last_error": inner.last_error,
            "active_concurrent": self.active.load(Ordering::Relaxed),
            "max_concurrent": inner.max_concurrent,
            "token_remaining": fmt_duration(remaining),
            "expires_at": expires_at,
            "models": inner.models.len(),
            "proxy_slot": inner.proxy_slot,
            "proxy": inner.proxy,
        })
    }
}

fn local_stamp(stamp: f64) -> String {
    // 面板只要「本地时间 YYYY-MM-DDTHH:MM:SS」，与 Python 的 time.localtime 输出同形。
    crate::state::local_stamp_for(stamp as i64).replace('T', "T")
}

// ---------------------------------------------------------------------------
// 账号池
// ---------------------------------------------------------------------------

pub struct Pool {
    pub accounts_dir: PathBuf,
    config: Mutex<Config>,
    accounts: Mutex<Vec<Arc<Account>>>,
    affinity: Mutex<HashMap<String, String>>,
}

impl Pool {
    pub fn new(accounts_dir: &Path, config: Config) -> Self {
        let pool = Self {
            accounts_dir: accounts_dir.to_path_buf(),
            config: Mutex::new(config),
            accounts: Mutex::new(Vec::new()),
            affinity: Mutex::new(HashMap::new()),
        };
        pool.load();
        pool
    }

    pub fn config(&self) -> Config {
        self.config.lock().unwrap_or_else(|err| err.into_inner()).clone()
    }

    pub fn set_config(&self, config: Config) {
        let max_concurrent = config.max_concurrent;
        *self.config.lock().unwrap_or_else(|err| err.into_inner()) = config;
        for account in self.list_accounts() {
            account.set_max_concurrent(max_concurrent);
        }
    }

    pub fn accounts_dir(&self) -> &Path {
        &self.accounts_dir
    }

    pub fn state_file(&self) -> PathBuf {
        accounts_path(&self.accounts_dir).join("state.json")
    }

    pub fn list_accounts(&self) -> Vec<Arc<Account>> {
        self.accounts.lock().unwrap_or_else(|err| err.into_inner()).clone()
    }

    fn load(&self) {
        let config = self.config();
        let state = self.load_state();
        let mut accounts: Vec<Arc<Account>> = Vec::new();
        for (path, auth) in load_auths(&self.accounts_dir) {
            let account = Account::new(auth, Some(path), config.max_concurrent);
            if let Some(entry) = state.get(&account.uid).and_then(Value::as_object) {
                let number = |key: &str| entry.get(key).and_then(Value::as_f64).unwrap_or(0.0);
                let text = |key: &str| entry.get(key).and_then(Value::as_str).unwrap_or("").to_string();
                account.set_state(
                    Some(entry.get("disabled").and_then(Value::as_bool).unwrap_or(false)),
                    Some(&text("reason")),
                    Some(number("cool_until")),
                    Some(entry.get("err_count").and_then(Value::as_i64).unwrap_or(0)),
                    Some(&text("last_error")),
                );
                if let Some(checkin) = entry.get("checkin").filter(|v| v.is_object()) {
                    account.set_checkin(Some(checkin.clone()));
                }
            }
            accounts.push(Arc::new(account));
        }
        *self.accounts.lock().unwrap_or_else(|err| err.into_inner()) = accounts;
        self.apply_proxy_slots(load_slots(&self.accounts_dir));
    }

    fn load_state(&self) -> Map<String, Value> {
        let Ok(raw) = std::fs::read_to_string(self.state_file()) else { return Map::new() };
        let Ok(parsed) = serde_json::from_str::<Value>(&raw) else { return Map::new() };
        let items = match &parsed {
            Value::Object(map) => match map.get("accounts") {
                Some(list) if list.is_array() => list.clone(),
                _ => Value::Array(vec![]),
            },
            Value::Array(list) => Value::Array(list.clone()),
            _ => return Map::new(),
        };
        let mut out = Map::new();
        for item in items.as_array().cloned().unwrap_or_default() {
            if let Some(name) = item.get("name").and_then(Value::as_str) {
                out.insert(name.to_string(), item);
            }
        }
        out
    }

    pub fn save_state(&self) {
        let items: Vec<Value> = self
            .list_accounts()
            .iter()
            .map(|account| {
                let (disabled, reason, cool_until, err_count, last_error) = account.state_snapshot();
                json!({
                    "name": account.uid,
                    "err_count": err_count,
                    "cool_until": cool_until,
                    "disabled": disabled,
                    "reason": reason,
                    "last_error": last_error,
                    "checkin": account.checkin(),
                })
            })
            .collect();
        if let Some(parent) = self.state_file().parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        let _ = write_json_atomic(&self.state_file(), &Value::Array(items));
    }

    pub fn get(&self, uid: &str) -> Option<Arc<Account>> {
        self.list_accounts().into_iter().find(|account| account.uid == uid)
    }

    pub fn list(&self) -> Vec<Value> {
        let mut accounts = self.list_accounts();
        accounts.sort_by(|a, b| a.uid.cmp(&b.uid));
        accounts.iter().map(|account| account.view()).collect()
    }

    /// `apply_proxy_slots`：`proxySlot` 是持久绑定，`proxy` 由当前槽位解出来。
    pub fn apply_proxy_slots(&self, slots: Vec<Value>) {
        let by_id: HashMap<String, String> = slots
            .iter()
            .filter(|slot| slot.get("enabled").and_then(Value::as_bool).unwrap_or(false))
            .filter_map(|slot| {
                let id = slot.get("id").and_then(Value::as_str)?.to_string();
                let url = slot.get("url").and_then(Value::as_str)?.to_string();
                Some((id, url))
            })
            .collect();
        for account in self.list_accounts() {
            let slot = account.proxy_slot();
            let url = by_id.get(&slot).cloned().unwrap_or_default();
            account.inner().proxy = url;
        }
    }

    pub fn stats(&self) -> Value {
        let now = now_secs();
        let mut total = 0;
        let mut healthy = 0;
        let mut disabled = 0;
        let mut cooling = 0;
        for account in self.list_accounts() {
            total += 1;
            let (is_disabled, _, cool_until, _, _) = account.state_snapshot();
            if is_disabled {
                disabled += 1;
            } else if now < cool_until {
                cooling += 1;
            } else {
                healthy += 1;
            }
        }
        json!({"total": total, "healthy": healthy, "disabled": disabled, "cooling": cooling})
    }

    /// `add`：同一 uid 重新登录时保留运营手选的代理槽。
    pub fn add(&self, auth: Value) -> Arc<Account> {
        let config = self.config();
        let mut account = Account::new(auth, None, config.max_concurrent);
        let mut accounts = self.accounts.lock().unwrap_or_else(|err| err.into_inner());
        if let Some(index) = accounts.iter().position(|existing| existing.uid == account.uid) {
            let existing = accounts[index].clone();
            if account.proxy_slot().is_empty() && !existing.proxy_slot().is_empty() {
                account.inner().proxy_slot = existing.proxy_slot();
            }
            let slot = account.proxy_slot();
            if !slot.is_empty() {
                if let Some(map) = account.inner().auth.as_object_mut() {
                    map.insert("proxySlot".into(), json!(slot));
                }
            }
            let doc = account.auth();
            let saved = save_auth(&self.accounts_dir, existing.path().as_deref(), &doc).ok();
            {
                let mut inner = existing.inner();
                inner.auth = doc;
                if let Some(path) = saved {
                    if let Some(map) = inner.auth.as_object_mut() {
                        map.insert("__path".into(), json!(path.display().to_string()));
                    }
                }
                inner.proxy_slot = slot;
                inner.nickname = account.nickname();
                inner.disabled = false;
                inner.disabled_reason.clear();
                inner.last_error.clear();
                inner.err_count = 0;
                inner.cool_until = 0.0;
                inner.models_fetched_at = 0.0;
            }
            drop(accounts);
            self.save_state();
            self.apply_proxy_slots(load_slots(&self.accounts_dir));
            return existing;
        }
        let account = Arc::new(account);
        accounts.push(account.clone());
        drop(accounts);
        self.save_state();
        self.apply_proxy_slots(load_slots(&self.accounts_dir));
        account
    }

    pub fn remove(&self, uid: &str) -> bool {
        let Some(account) = self.get(uid) else { return false };
        {
            let mut accounts = self.accounts.lock().unwrap_or_else(|err| err.into_inner());
            accounts.retain(|item| item.uid != account.uid);
        }
        delete_auth(&self.accounts_dir, uid);
        self.save_state();
        true
    }

    pub fn enable(&self, uid: &str) -> bool {
        let Some(account) = self.get(uid) else { return false };
        account.set_state(Some(false), Some(""), Some(0.0), Some(0), Some(""));
        self.save_state();
        true
    }

    pub fn disable(&self, uid: &str, reason: &str) -> bool {
        let Some(account) = self.get(uid) else { return false };
        account.set_state(Some(true), Some(reason), None, None, Some(reason));
        self.save_state();
        true
    }

    pub fn clear_cooldown(&self, uid: &str) -> bool {
        let Some(account) = self.get(uid) else { return false };
        account.set_state(None, None, Some(0.0), Some(0), None);
        self.save_state();
        true
    }

    /// `pick`：能服务该模型 + 健康 + 并发未满；会话粘性优先。
    pub fn pick(&self, model: &str, session_key: Option<&str>, exclude: &[String], allowed: Option<&[String]>) -> Option<Arc<Account>> {
        let now = now_secs();
        let allowed_ok = |uid: &str| allowed.map(|list| list.iter().any(|item| item == uid)).unwrap_or(true);
        if let Some(key) = session_key {
            let affinity = self.affinity.lock().unwrap_or_else(|err| err.into_inner());
            if let Some(uid) = affinity.get(key) {
                if !exclude.contains(uid) && allowed_ok(uid) {
                    if let Some(account) = self.get(uid) {
                        if account.healthy(now)
                            && account.has_model(model)
                            && (account.active.load(Ordering::Relaxed) as i64) < account.max_concurrent()
                        {
                            return Some(account);
                        }
                    }
                }
            }
        }
        let candidates: Vec<Arc<Account>> = self
            .list_accounts()
            .into_iter()
            .filter(|account| !exclude.contains(&account.uid) && allowed_ok(&account.uid))
            .filter(|account| account.healthy(now))
            .filter(|account| (account.active.load(Ordering::Relaxed) as i64) < account.max_concurrent())
            .collect();
        let capable: Vec<Arc<Account>> = candidates.iter().filter(|account| account.has_model(model)).cloned().collect();
        let mut pool = if capable.is_empty() { candidates } else { capable };
        pool.sort_by(|left, right| {
            let la = left.active.load(Ordering::Relaxed);
            let ra = right.active.load(Ordering::Relaxed);
            la.cmp(&ra).then_with(|| {
                let ll = *left.last_used.lock().unwrap_or_else(|err| err.into_inner());
                let rl = *right.last_used.lock().unwrap_or_else(|err| err.into_inner());
                ll.partial_cmp(&rl).unwrap_or(std::cmp::Ordering::Equal)
            })
        });
        pool.into_iter().next()
    }

    pub fn bind_session(&self, session_key: &str, uid: &str) {
        if session_key.is_empty() || uid.is_empty() {
            return;
        }
        let mut affinity = self.affinity.lock().unwrap_or_else(|err| err.into_inner());
        affinity.insert(session_key.to_string(), uid.to_string());
        if affinity.len() > 4096 {
            for key in affinity.keys().take(1024).cloned().collect::<Vec<_>>() {
                affinity.remove(&key);
            }
        }
    }

    pub fn acquire(&self, account: &Account) -> bool {
        let max = account.max_concurrent();
        if account.active.load(Ordering::Relaxed) as i64 >= max {
            return false;
        }
        account.active.fetch_add(1, Ordering::Relaxed);
        *account.last_used.lock().unwrap_or_else(|err| err.into_inner()) = now_secs();
        true
    }

    pub fn release(&self, account: &Account) {
        let _ = account
            .active
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |value| value.checked_sub(1));
    }

    pub fn note_success(&self, account: &Account) {
        let (_, _, cool_until, err_count, last_error) = account.state_snapshot();
        if err_count != 0 || cool_until != 0.0 || !last_error.is_empty() {
            account.set_state(None, None, Some(0.0), Some(0), Some(""));
            self.save_state();
        }
    }

    pub fn note_error(&self, account: &Account, message: &str, soft: bool, terminal: bool) {
        let text: String = message.chars().take(200).collect();
        if terminal {
            account.set_state(Some(true), Some(&text), None, None, Some(&text));
        } else if soft {
            account.set_state(None, None, Some(now_secs() + SOFT_COOLDOWN), None, Some(&text));
        } else {
            let (_, _, _, err_count, _) = account.state_snapshot();
            let err_count = err_count + 1;
            if err_count >= ERR_THRESHOLD {
                account.set_state(None, None, Some(now_secs() + ERR_COOLDOWN), Some(0), Some(&text));
            } else {
                account.set_state(None, None, None, Some(err_count), Some(&text));
            }
        }
        self.save_state();
    }
}

/// 面板保存代理槽之后重新解绑/绑定用（Python `apply_proxy_slots`）。
pub fn slots_for(accounts_dir: &Path) -> Vec<Value> {
    load_slots(accounts_dir)
}

fn load_slots(accounts_dir: &Path) -> Vec<Value> {
    let raw = std::fs::read_to_string(accounts_dir.join("settings.json")).unwrap_or_default();
    match serde_json::from_str::<Value>(&raw) {
        Ok(Value::Object(settings)) => crate::proxy_slots::store::slots_from_settings(&settings),
        _ => Vec::new(),
    }
}

static POOLS: OnceLock<Mutex<HashMap<PathBuf, Arc<Pool>>>> = OnceLock::new();

fn pools() -> &'static Mutex<HashMap<PathBuf, Arc<Pool>>> {
    POOLS.get_or_init(|| Mutex::new(HashMap::new()))
}

/// `pool_for`：按 accounts_dir 取单例池，配置变化时就地刷新（含每账号并发）。
pub fn pool_for(app: &Arc<crate::App>) -> Arc<Pool> {
    let accounts_dir = app.state.accounts_dir.clone();
    let key = std::fs::canonicalize(&accounts_dir).unwrap_or_else(|_| accounts_dir.clone());
    let config = config_from(&app.state.settings_snapshot());
    let mut map = pools().lock().unwrap_or_else(|err| err.into_inner());
    if let Some(pool) = map.get(&key) {
        pool.set_config(config);
        return pool.clone();
    }
    let pool = Arc::new(Pool::new(&accounts_dir, config));
    map.insert(key, pool.clone());
    pool
}

/// `invalidate_pool`：账号文件被外部改动（例如 Python 侧登录）后重建。
pub fn invalidate_pool(app: &Arc<crate::App>) {
    let accounts_dir = app.state.accounts_dir.clone();
    let key = std::fs::canonicalize(&accounts_dir).unwrap_or(accounts_dir);
    pools().lock().unwrap_or_else(|err| err.into_inner()).remove(&key);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("wb-codearts-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn config_clamps_like_python() {
        let mut settings = Map::new();
        settings.insert(
            "codearts".into(),
            json!({"enabled": "yes", "timeout_seconds": 1000, "max_concurrent": 99,
                   "models_ttl_seconds": 5, "benefit_auto_claim": false}),
        );
        let config = config_from(&settings);
        // "yes" 不是 True → 关闭；数值越界收敛到上下限。
        assert!(!config.enabled);
        assert_eq!(config.timeout_seconds, 600.0);
        assert_eq!(config.max_concurrent, 20);
        assert_eq!(config.models_ttl_seconds, 60.0);
        assert!(!config.benefit_auto_claim);
        assert!(config.credits_auto_claim);
    }

    #[test]
    fn auth_files_round_trip_and_reject_incomplete_docs() {
        let dir = temp_dir("auth");
        let folder = accounts_path(&dir);
        std::fs::create_dir_all(&folder).unwrap();
        std::fs::write(
            folder.join("codearts-abc.json"),
            json!({"user_id": "abc", "user_name": "张三", "cloud_dragon_token": "T"}).to_string(),
        )
        .unwrap();
        // 缺 token 的档案要跳过，不能把坏账号带进池子。
        std::fs::write(folder.join("codearts-bad.json"), json!({"user_id": "bad"}).to_string()).unwrap();
        let auths = load_auths(&dir);
        assert_eq!(auths.len(), 1);
        assert_eq!(auths[0].1["user_id"], "abc");

        let path = save_auth(&dir, Some(&auths[0].0), &json!({"user_id": "abc", "x": 1})).unwrap();
        let saved: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(saved["x"], 1);
        assert_eq!(saved["user_id"], "abc");
        assert!(delete_auth(&dir, "abc"));
        assert!(load_auths(&dir).is_empty());
    }

    #[test]
    fn expiration_parses_iso8601_with_timezone() {
        // 2026-11-01T00:00:00Z
        assert_eq!(parse_expiration("2026-11-01T00:00:00Z"), 1793491200.0);
        assert_eq!(parse_expiration("2026-11-01T08:00:00+08:00"), 1793491200.0);
        assert_eq!(parse_expiration(""), 0.0);
        assert_eq!(parse_expiration("nonsense"), 0.0);
    }

    #[test]
    fn pool_picks_healthy_accounts_and_cools_down() {
        let dir = temp_dir("pool");
        let folder = accounts_path(&dir);
        std::fs::create_dir_all(&folder).unwrap();
        for uid in ["a", "b"] {
            std::fs::write(
                folder.join(format!("codearts-{uid}.json")),
                json!({"user_id": uid, "user_name": uid, "cloud_dragon_token": "T"}).to_string(),
            )
            .unwrap();
        }
        let pool = Pool::new(&dir, Config::default());
        assert_eq!(pool.list_accounts().len(), 2);
        let picked = pool.pick("", None, &[], None).expect("应该能选到账号");
        assert!(pool.acquire(&picked));
        // max_concurrent=1：同一个号并发已满，第二单必须换号。
        let second = pool.pick("", None, &[], None).expect("另一个号");
        assert_ne!(second.uid, picked.uid);
        pool.release(&picked);
        // 错误三次进冷却，就选不到它了。
        pool.note_error(&picked, "boom", false, false);
        pool.note_error(&picked, "boom", false, false);
        pool.note_error(&picked, "boom", false, false);
        assert!(!picked.healthy(now_secs()));
        let stats = pool.stats();
        assert_eq!(stats["cooling"], 1);
        pool.clear_cooldown(&picked.uid);
        assert!(picked.healthy(now_secs()));
        // 状态要落盘，重启后仍然在。
        let restored = Pool::new(&dir, Config::default());
        assert_eq!(restored.list_accounts().len(), 2);
    }

    #[test]
    fn fmt_duration_matches_python() {
        assert_eq!(fmt_duration(0.0), "0m");
        assert_eq!(fmt_duration(59.0), "0m");
        assert_eq!(fmt_duration(3600.0), "1h0m");
        assert_eq!(fmt_duration(7320.0), "2h2m");
    }
}
