//! Runtime settings view with the same stable shape as `wb_settings.runtime_settings_view`.

use std::sync::Arc;

use hyper::StatusCode;
use serde_json::{json, Map, Value};

use crate::http::{json_resp, Resp};
use crate::App;

const DEFAULT_SCHEDULE_CHECKIN: &[i64] = &[9, 21];
const DEFAULT_SCHEDULE_TRAVEL: &[i64] = &[9, 21];
const DEFAULT_SCHEDULE_KEEPALIVE: &[i64] = &[22];
const DEFAULT_SCHEDULE_CAT: &[i64] = &[1];
const DEFAULT_DAILY_CHAT_TIMES: &[&str] = &["06:05", "18:05"];
fn int_setting(settings: &Map<String, Value>, key: &str, default: i64, max: i64) -> i64 {
    settings
        .get(key)
        .and_then(Value::as_i64)
        .filter(|value| *value >= 0)
        .unwrap_or(default)
        .min(max)
}

fn number_setting(settings: &Map<String, Value>, key: &str, default: f64, max: f64) -> f64 {
    settings
        .get(key)
        .and_then(Value::as_f64)
        .filter(|value| *value >= 0.0)
        .unwrap_or(default)
        .min(max)
}

fn bool_setting(settings: &Map<String, Value>, key: &str, default: bool) -> bool {
    settings
        .get(key)
        .and_then(Value::as_bool)
        .unwrap_or(default)
}

fn string_setting<'a>(settings: &'a Map<String, Value>, key: &str, default: &'a str) -> &'a str {
    settings.get(key).and_then(Value::as_str).unwrap_or(default)
}

fn mask(value: &str) -> String {
    if value.len() > 8 {
        format!("{}******{}", &value[..4], &value[value.len() - 4..])
    } else {
        "*".repeat(value.len())
    }
}

/// 面板写 `deleted_at: ""` 表示没删（不是 null、也不是缺字段）；三种都是「未删除」。
/// 只判 `is_some()` 会把生产里每一条活着的 key 都算成已删除 —— CT412 实测面板
/// 「设置」页直接变空，看着像 key 丢了。
fn is_deleted(entry: &Value) -> bool {
    match entry.get("deleted_at") {
        Some(Value::String(text)) => !text.trim().is_empty(),
        Some(Value::Null) | None => false,
        Some(_) => true,
    }
}

fn api_keys(settings: &Map<String, Value>, deleted: bool) -> Vec<Value> {
    settings
        .get("api_keys")
        .and_then(Value::as_array)
        .map(|rows| {
            rows.iter()
                .filter(|entry| is_deleted(entry) == deleted)
                .map(|entry| {
                    let raw = entry.get("key").and_then(Value::as_str).unwrap_or("");
                    if deleted {
                        json!({
                            "id": entry.get("id").and_then(Value::as_str).unwrap_or(""),
                            "name": entry.get("name").and_then(Value::as_str).unwrap_or(""),
                            "realm": entry.get("realm").and_then(Value::as_str).unwrap_or(""),
                            "created_at": entry.get("created_at").and_then(Value::as_str).unwrap_or(""),
                            "deleted_at": entry.get("deleted_at").and_then(Value::as_str).unwrap_or(""),
                        })
                    } else {
                        json!({
                            "id": entry.get("id").and_then(Value::as_str).unwrap_or(""),
                            "name": entry.get("name").and_then(Value::as_str).unwrap_or(""),
                            "realm": entry.get("realm").and_then(Value::as_str).unwrap_or(""),
                            "models": string_list(entry.get("models"), usize::MAX),
                            "accounts": string_list(entry.get("accounts"), usize::MAX),
                            "enabled": entry.get("enabled").and_then(Value::as_bool).unwrap_or(true),
                            "masked": mask(raw),
                            "source": entry.get("source").and_then(Value::as_str).unwrap_or("panel"),
                            "created_at": entry.get("created_at").and_then(Value::as_str).unwrap_or(""),
                        })
                    }
                })
                .collect()
        })
        .unwrap_or_default()
}

/// 本地面板显示用的时间戳（与 Python 的 `time.strftime("%Y/%m/%d %H:%M")` 同格式）。
fn now_label() -> String {
    let stamp = crate::state::now_epoch() as libc::time_t;
    let mut value: libc::tm = unsafe { std::mem::zeroed() };
    if unsafe { libc::localtime_r(&stamp, &mut value) }.is_null() {
        return String::new();
    }
    format!(
        "{:04}/{:02}/{:02} {:02}:{:02}",
        value.tm_year + 1900,
        value.tm_mon + 1,
        value.tm_mday,
        value.tm_hour,
        value.tm_min
    )
}

fn unique_id(candidate: String, used: &[String]) -> String {
    if !candidate.is_empty() && !used.iter().any(|id| id == &candidate) {
        return candidate;
    }
    let mut suffix = 2;
    loop {
        let alt = format!("{candidate}-{suffix}");
        if !used.iter().any(|id| id == &alt) {
            return alt;
        }
        suffix += 1;
    }
}

/// 保存面板提交的 API Key 列表（`POST /settings/save` 的 `api_keys` 字段）。
///
/// 三条与 Python 一致的规则，缺一条就会丢用户的密钥：
///
/// 1. 面板只画打码后的 key，所以这一行的 `key` 留空 = **沿用已存的密钥**，
///    而不是「把它清空」。早期 Rust 版把提交原样落盘，于是每次在面板上改点
///    别的设置都会把所有密钥写成空串。
/// 2. 只有 `deleted_api_key_ids` 里点名的 id 才软删除（保留 id 与 created_at，
///    清掉密钥、置 `enabled: false`），其余已存的键一律保留——提交的列表可能
///    是脏的（第二个标签页、保存前的旧快照），不能当成「以这份为准」。
/// 3. 提交里没有 id 的行是新行，由服务端生成 id；已有 id 的行沿用原 id。
///
/// 返回落盘后的行数。
pub(crate) fn merge_api_keys(
    settings: &mut Map<String, Value>,
    submitted: &Value,
    delete_ids: Option<&Value>,
) -> Result<usize, String> {
    let Some(rows) = submitted.as_array() else {
        return Err("api_keys must be a list".to_string());
    };
    let stored: Vec<Value> = settings
        .get("api_keys")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let text = |value: &Value, key: &str| -> String {
        value
            .get(key)
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string()
    };
    let stored_by_id = |id: &str| -> Option<Value> {
        stored
            .iter()
            .find(|entry| text(entry, "id") == id)
            .cloned()
    };
    let drop: Vec<String> = delete_ids
        .and_then(Value::as_array)
        .map(|items| items.iter().filter_map(|item| item.as_str().map(str::to_string)).collect())
        .unwrap_or_default();
    let stamp = now_label();
    let mut out: Vec<Value> = Vec::new();
    let mut used: Vec<String> = Vec::new();
    for row in rows {
        let Some(item) = row.as_object() else {
            return Err("each api key must be an object".to_string());
        };
        let id = item.get("id").and_then(Value::as_str).unwrap_or("").trim().to_string();
        let old = stored_by_id(&id);
        let mut secret = item.get("key").and_then(Value::as_str).unwrap_or("").trim().to_string();
        if secret.is_empty() {
            secret = old
                .as_ref()
                .map(|entry| text(entry, "key"))
                .unwrap_or_default();
        }
        if secret.is_empty() {
            return Err("a key entry is empty - fill it in or remove the row".to_string());
        }
        // 提交里没提到的字段沿用已存值：旧面板不认识新字段时不能把它清掉。
        let field = |name: &str| -> Option<Value> {
            item.get(name)
                .cloned()
                .or_else(|| old.as_ref().and_then(|entry| entry.get(name).cloned()))
        };
        let final_id = if id.is_empty() {
            crate::sse::hex_token(6)
        } else {
            id.clone()
        };
        let final_id = unique_id(final_id, &used);
        used.push(final_id.clone());
        let realm = field("realm")
            .and_then(|value| value.as_str().map(|text| text.trim().to_lowercase()))
            .unwrap_or_default();
        let mut entry = Map::new();
        entry.insert("id".into(), json!(final_id));
        entry.insert("name".into(), field("name").unwrap_or_else(|| json!("")));
        entry.insert("key".into(), json!(secret));
        entry.insert("realm".into(), json!(realm));
        entry.insert(
            "models".into(),
            field("models").unwrap_or_else(|| json!(Vec::<String>::new())),
        );
        entry.insert(
            "accounts".into(),
            field("accounts").unwrap_or_else(|| json!(Vec::<String>::new())),
        );
        entry.insert(
            "concurrency_limit".into(),
            field("concurrency_limit").unwrap_or(json!(0)),
        );
        entry.insert(
            "enabled".into(),
            field("enabled").unwrap_or(json!(true)),
        );
        let created = old
            .as_ref()
            .map(|old_entry| text(old_entry, "created_at"))
            .filter(|text| !text.is_empty())
            .unwrap_or_else(|| stamp.clone());
        entry.insert("created_at".into(), json!(created));
        out.push(Value::Object(entry));
    }
    // 已存但这次没提交的键：点名的软删除，没点名的一字不动。
    for old in &stored {
        let id = text(old, "id");
        if used.iter().any(|seen| seen == &id) {
            continue;
        }
        used.push(id.clone());
        if drop.iter().any(|dropped| dropped == &id) && !is_deleted(old) {
            let mut retired = Map::new();
            retired.insert("id".into(), json!(id));
            retired.insert("name".into(), json!(text(old, "name")));
            retired.insert("key".into(), json!(""));
            retired.insert("realm".into(), json!(text(old, "realm")));
            retired.insert(
                "models".into(),
                old.get("models").cloned().unwrap_or_else(|| json!(Vec::<String>::new())),
            );
            retired.insert("enabled".into(), json!(false));
            retired.insert("created_at".into(), json!(text(old, "created_at")));
            retired.insert("deleted_at".into(), json!(stamp));
            out.push(Value::Object(retired));
            continue;
        }
        out.push(old.clone());
    }
    let live = out
        .iter()
        .filter(|entry| !is_deleted(entry) && !text(entry, "key").is_empty())
        .count();
    settings.insert("api_keys".into(), Value::Array(out));
    Ok(live)
}

/// `GET /settings/reveal?id=<id>`：面板要「复制」就只能拿回明文密钥。
/// 只在面板会话下可用（外层已校验 token），与 Python 的 `_get_settings_reveal` 同形。
pub(crate) fn reveal(app: &Arc<App>, query: Option<&str>) -> Resp {
    let wanted = query
        .unwrap_or("")
        .split('&')
        .find_map(|pair| pair.strip_prefix("id="))
        .map(|value| urldecode(value))
        .unwrap_or_default();
    let settings = app.state.settings_snapshot();
    let found = settings
        .get("api_keys")
        .and_then(Value::as_array)
        .and_then(|rows| {
            rows.iter()
                .find(|entry| {
                    entry.get("id").and_then(Value::as_str) == Some(wanted.as_str())
                })
                .map(|entry| entry.get("key").and_then(Value::as_str).unwrap_or("").to_string())
        });
    match found {
        Some(key) => json_resp(StatusCode::OK, &json!({"id": wanted, "key": key})),
        None => json_resp(
            StatusCode::NOT_FOUND,
            &json!({"error": {"message": "no such key", "code": 404}}),
        ),
    }
}

fn urldecode(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'%' if i + 2 < bytes.len() => {
                let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).unwrap_or("");
                match u8::from_str_radix(hex, 16) {
                    Ok(byte) => {
                        out.push(byte);
                        i += 3;
                    }
                    Err(_) => {
                        out.push(b'%');
                        i += 1;
                    }
                }
            }
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            byte => {
                out.push(byte);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).to_string()
}

pub(crate) fn string_list(value: Option<&Value>, limit: usize) -> Vec<String> {
    let Some(items) = value.and_then(Value::as_array) else {
        return Vec::new();
    };
    let mut out = Vec::new();
    for item in items {
        let Some(text) = item.as_str().map(str::trim).filter(|text| !text.is_empty()) else {
            continue;
        };
        if !out.iter().any(|seen| seen == text) {
            out.push(text.to_string());
            if out.len() >= limit {
                break;
            }
        }
    }
    out
}

fn hours(settings: &Map<String, Value>, key: &str, default: &[i64]) -> Vec<i64> {
    let value = settings
        .get("schedule")
        .and_then(Value::as_object)
        .and_then(|schedule| schedule.get(key))
        .and_then(Value::as_array);
    let mut out: Vec<i64> = value
        .into_iter()
        .flatten()
        .filter_map(Value::as_i64)
        .filter(|hour| (0..=23).contains(hour))
        .collect();
    out.sort_unstable();
    out.dedup();
    if out.is_empty() {
        default.to_vec()
    } else {
        out
    }
}

pub(crate) fn daily_chat_times(app: &App) -> Vec<String> {
    let settings = app.state.settings_snapshot();
    string_list(
        settings
            .get("schedule")
            .and_then(Value::as_object)
            .and_then(|schedule| schedule.get("daily_chat")),
        24,
    )
}

pub(crate) fn daily_chat_effective_times(app: &App) -> Vec<String> {
    let explicit = daily_chat_times(app);
    if !explicit.is_empty() {
        return explicit;
    }
    let settings = app.state.settings_snapshot();
    let stored = string_list(
        settings
            .get("daily_chat_random")
            .and_then(Value::as_object)
            .and_then(|random| random.get("times")),
        2,
    );
    if stored.is_empty() {
        DEFAULT_DAILY_CHAT_TIMES
            .iter()
            .map(|time| (*time).to_string())
            .collect()
    } else {
        stored
    }
}

fn schedule(settings: &Map<String, Value>, app: &App) -> Value {
    let explicit = daily_chat_times(app);
    let effective = daily_chat_effective_times(app);
    json!({
        "checkin": hours(settings, "checkin", DEFAULT_SCHEDULE_CHECKIN),
        "travel": hours(settings, "travel", DEFAULT_SCHEDULE_TRAVEL),
        "keepalive": hours(settings, "keepalive", DEFAULT_SCHEDULE_KEEPALIVE),
        "cat": hours(settings, "cat", DEFAULT_SCHEDULE_CAT),
        "daily_chat": explicit,
        "daily_chat_effective": effective,
        "daily_chat_random": settings
            .get("schedule")
            .and_then(Value::as_object)
            .and_then(|schedule| schedule.get("daily_chat"))
            .and_then(Value::as_array)
            .map(|items| items.is_empty())
            .unwrap_or(true),
    })
}

pub(crate) fn limits(settings: &Map<String, Value>) -> Map<String, Value> {
    let stored = settings.get("limits").and_then(Value::as_object);
    let mut out = Map::new();
    for (key, default) in [
        ("reserve_credits", 0),
        ("daily_token_limit", 0),
        ("daily_credit_limit", 0),
        ("model_daily_token_limit", 0),
        ("expiring_window_days", 7),
    ] {
        let entry = stored
            .and_then(|values| values.get(key))
            .and_then(Value::as_object);
        let global = entry
            .and_then(|value| value.get("global"))
            .and_then(limit_number)
            .or_else(|| settings.get(key).and_then(limit_number))
            .unwrap_or(default)
            .max(0);
        let realm = |name: &str| {
            entry
                .and_then(|value| value.get(name))
                .and_then(limit_number)
                .map_or(Value::Null, |value| json!(value))
        };
        out.insert(
            key.to_string(),
            json!({"global": global.max(if key == "expiring_window_days" { 0 } else { 0 }), "intl": realm("intl"), "cn": realm("cn")}),
        );
    }
    out
}

fn limit_number(value: &Value) -> Option<i64> {
    if value.is_boolean() || value.is_null() {
        return None;
    }
    value
        .as_i64()
        .or_else(|| value.as_f64().map(|number| number as i64))
        .or_else(|| value.as_str().and_then(|text| text.trim().parse().ok()))
        .filter(|number| *number >= 0)
}

fn clean_products(settings: &Map<String, Value>) -> Vec<String> {
    let values = string_list(settings.get("auto_switch_products"), 3);
    let allowed = ["workbuddy", "vscode", "cli"];
    if values.is_empty() {
        allowed.iter().map(|value| (*value).to_string()).collect()
    } else {
        values
            .into_iter()
            .filter(|value| allowed.contains(&value.as_str()))
            .collect()
    }
}

fn web_prompts(settings: &Map<String, Value>) -> Vec<String> {
    string_list(settings.get("daily_chat_web_prompts"), 500)
}

fn merge(out: &mut Map<String, Value>, group: Value) {
    if let Value::Object(values) = group {
        out.extend(values);
    }
}

fn access_settings(settings: &Map<String, Value>) -> Value {
    let live_keys = api_keys(&settings, false);
    let deleted_keys = api_keys(&settings, true);
    let global_key = std::env::var("WB_API_KEY").unwrap_or_default();
    json!({
        "panel_password_is_default": !settings.contains_key("panel_password_hash"),
        "api_key_set": !global_key.is_empty(),
        "api_key_set_by_panel": !live_keys.is_empty(),
        "api_key_masked": mask(&global_key),
        "auth_required": !global_key.is_empty() || !live_keys.is_empty(),
        "api_keys": live_keys,
        "deleted_api_keys": deleted_keys,
    })
}

fn global_limit(limits: &Map<String, Value>, name: &str) -> Value {
    limits
        .get(name)
        .and_then(|value| value.get("global"))
        .cloned()
        .unwrap_or(json!(0))
}

fn policy_settings(settings: &Map<String, Value>, app: &App) -> Value {
    let limit_view = limits(settings);
    json!({
        "limits": limit_view,
        "reserve_credits": global_limit(&limit_view, "reserve_credits"),
        "daily_token_limit": global_limit(&limit_view, "daily_token_limit"),
        "daily_credit_limit": global_limit(&limit_view, "daily_credit_limit"),
        "model_daily_token_limit": global_limit(&limit_view, "model_daily_token_limit"),
        "pricing_refresh_minutes": number_setting(&settings, "pricing_refresh_minutes", 5.0, 43_200.0),
        "credits_refresh_hours": number_setting(&settings, "credits_refresh_hours", 12.0, 720.0),
        "section_max_height": int_setting(&settings, "section_max_height", 640, 4_000),
        "schedule": schedule(&settings, app),
        "ui_language": string_setting(&settings, "ui_language", "zh"),
        "pricing_variant_inherit": bool_setting(&settings, "pricing_variant_inherit", true),
        "pricing_enabled": bool_setting(&settings, "pricing_enabled", true),
        "auto_switch_product": bool_setting(&settings, "auto_switch_product", false),
        "auto_switch_products": clean_products(&settings),
        "free_model_credit_fallback": bool_setting(&settings, "free_model_credit_fallback", false),
        "enable_anthropic_messages": bool_setting(&settings, "enable_anthropic_messages", false),
        "filter_models_by_key": bool_setting(&settings, "filter_models_by_key", false),
        "reasoning_effort_map": bool_setting(&settings, "reasoning_effort_map", true),
        "models_only_available": bool_setting(&settings, "models_only_available", false),
        "identity": crate::panel_defaults::identity(&settings),
        "identity_version_defaults": crate::panel_defaults::identity_version_defaults(),
    })
}

fn daily_chat_settings(settings: &Map<String, Value>) -> Value {
    json!({
        "daily_chat_web": bool_setting(&settings, "daily_chat_web", true),
        "daily_chat_web_timeout": int_setting(&settings, "daily_chat_web_timeout", 120, 3_600),
        "daily_chat_web_timeout_default": 120,
        "daily_chat_web_timeout_max": 3_600,
        "daily_chat_web_retry_backoff": number_setting(&settings, "daily_chat_web_retry_backoff", 5.0, 60.0),
        "daily_chat_web_retry_backoff_default": 5.0,
        "daily_chat_web_retry_backoff_max": 60,
        "daily_chat_web_random_prompt": bool_setting(&settings, "daily_chat_web_random_prompt", false),
        "daily_chat_web_prompts": web_prompts(&settings),
        "daily_chat_web_builtin_prompts": crate::panel_defaults::builtin_prompts(),
        "daily_chat_web_prompts_max": 500,
        "daily_chat_models": string_list(settings.get("daily_chat_models"), 8),
        "daily_chat_models_max": 8,
        "daily_chat_budget": int_setting(&settings, "daily_chat_budget", 600, 7_200),
        "daily_chat_budget_default": 600,
        "daily_chat_budget_max": 7_200,
        "daily_chat_startup_catchup": bool_setting(&settings, "daily_chat_startup_catchup", true),
        "daily_chat_web_fallback_activity": bool_setting(&settings, "daily_chat_web_fallback_activity", false),
    })
}

fn runtime_settings(settings: &Map<String, Value>, app: &Arc<App>) -> Value {
    let prompt_defaults = json!({"mode": "passthrough", "file": "", "text": ""});
    let upstream_defaults = json!({
        "header_timeout_seconds": 120,
        "idle_timeout_seconds": 300,
        "device_token": "",
        "device_token_file": "",
    });
    let usage_defaults = json!({"hour_keep_hours": 48, "day_keep_days": 32});
    json!({
        "local_web_tools": bool_setting(&settings, "local_web_tools", false),
        "remaining_priority_enabled": bool_setting(&settings, "remaining_priority_enabled", false),
        "egress_backoff_enabled": bool_setting(&settings, "egress_backoff_enabled", false),
        "realm_fallback_enabled": bool_setting(&settings, "realm_fallback_enabled", false),
        "account_max_concurrent": int_setting(&settings, "account_max_concurrent", 0, 10),
        "account_max_concurrent_max": 10,
        "account_inflight": {},
        "affinity_stable_key_enabled": bool_setting(&settings, "affinity_stable_key_enabled", false),
        "log_db_enabled": bool_setting(&settings, "log_db_enabled", false),
        "log_db_max_rows": crate::panel_defaults::null_or_i64(settings.get("log_db_max_rows")),
        "log_db_max_days": settings.get("log_db_max_days").and_then(Value::as_i64).unwrap_or(7),
        "log_db_max_bytes": crate::panel_defaults::null_or_i64(settings.get("log_db_max_bytes")),
        "accounts_collapsed": bool_setting(&settings, "accounts_collapsed", false),
        "accounts_separate_tab": bool_setting(&settings, "accounts_separate_tab", false),
        "key_before_hidden": bool_setting(&settings, "key_before_hidden", false),
        "hidden_pages": string_list(settings.get("hidden_pages"), 32),
        "update_check_enabled": bool_setting(&settings, "update_check_enabled", false),
        "keep_backups": int_setting(&settings, "keep_backups", 1, 3),
        "keep_backups_max": 3,
        "upstream": crate::panel_defaults::mapping(&settings, "upstream", upstream_defaults),
        // v1.10.0：单请求体上限可在面板里 1–256MB 调整，没设就是环境变量/16MB。
        "max_payload_mb": crate::panel_defaults::null_or_i64(settings.get("max_payload_mb")),
        "max_payload_bytes_effective": crate::http::payload_bytes_effective(&settings),
        // CodeArts 出口（wb_codearts.masked_config）：rust 侧没有账号池，只回配置与 0 计数。
        "codearts": codearts_masked(&settings),
        "usage": crate::panel_defaults::mapping(&settings, "usage", usage_defaults),
        "prompt": crate::panel_defaults::mapping(&settings, "prompt", prompt_defaults),
        "window_probe": bool_setting(&settings, "window_probe", true),
        "quota_scan_models": crate::quota::watched_models(app),
        "quota_scan_interval_hours": crate::quota::scan_interval_hours(),
        "quota_scans": crate::quota::scan_snapshot(app, 12),
        "accounts_dir": app.state.accounts_dir_name(),
        "usage_dir": app.state.usage_dir.display().to_string(),
        "settings_file": app.state.accounts_dir.join("settings.json").display().to_string(),
        "version": crate::VERSION,
    })
}

/// `wb_codearts.SNAP_HOST`：面板/健康检查里展示的 CodeArts 入口。
pub const CODEARTS_SNAP_HOST: &str = "https://snap-access.cn-north-4.myhuaweicloud.com";

/// `wb_codearts.configured_config`：只认 settings 里 `codearts.enabled is True`。
pub fn codearts_configured(settings: &Map<String, Value>) -> bool {
    settings
        .get("codearts")
        .and_then(|value| value.get("enabled"))
        .and_then(Value::as_bool)
        .unwrap_or(false)
}

/// `wb_codearts.load_config` + `masked_config` 的形状（面板设置页直接读这些键）。
pub fn codearts_masked(settings: &Map<String, Value>) -> Value {
    fn number(raw: Option<&Value>, default: f64, low: f64, high: f64) -> Value {
        let value = raw
            .and_then(|value| value.as_f64())
            .filter(|value| value.is_finite())
            .unwrap_or(default)
            .clamp(low, high);
        // Python 侧这些字段有的存成整数（max_concurrent），照原类型回。
        if value.fract() == 0.0 {
            json!(value as i64)
        } else {
            json!(value)
        }
    }
    let raw = settings.get("codearts");
    let field = |key: &str| raw.and_then(|value| value.get(key));
    let enabled = field("enabled").and_then(Value::as_bool).unwrap_or(false);
    json!({
        "enabled": enabled,
        "timeout_seconds": number(field("timeout_seconds"), 30.0, 1.0, 600.0),
        "stream_timeout_seconds": number(field("stream_timeout_seconds"), 600.0, 1.0, 3600.0),
        "models_ttl_seconds": number(field("models_ttl_seconds"), 300.0, 60.0, 86400.0),
        "max_concurrent": number(field("max_concurrent"), 1.0, 1.0, 20.0),
        "benefit_auto_claim": field("benefit_auto_claim").and_then(Value::as_bool).unwrap_or(true),
        "credits_auto_claim": field("credits_auto_claim").and_then(Value::as_bool).unwrap_or(true),
        "configured": enabled,
        "accounts": 0,
        "healthy": 0,
    })
}

pub fn view(app: &Arc<App>) -> Resp {
    let settings = app.state.settings_snapshot();
    let daily_chat = daily_chat_times(app);
    let effective = daily_chat_effective_times(app);
    let mut output = Map::new();
    merge(&mut output, access_settings(&settings));
    merge(&mut output, policy_settings(&settings, app));
    merge(&mut output, daily_chat_settings(&settings));
    merge(&mut output, runtime_settings(&settings, app));
    if let Some(schedule) = output.get_mut("schedule").and_then(Value::as_object_mut) {
        schedule.insert("daily_chat".into(), json!(daily_chat));
        schedule.insert("daily_chat_effective".into(), json!(effective));
        schedule.insert("daily_chat_random".into(), json!(daily_chat.is_empty()));
    }
    json_resp(StatusCode::OK, &Value::Object(output))
}

#[cfg(test)]
mod tests;
