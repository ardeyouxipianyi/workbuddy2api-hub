//! Narrow `settings.json` bookkeeping and installation status persistence.

use std::path::Path;

use serde_json::{json, Map, Value};

use crate::update_config::Config;

#[derive(Default)]
pub(crate) struct StoredCheck {
    pub(crate) last_attempt: f64,
    pub(crate) last_success: f64,
    pub(crate) latest_version: String,
    pub(crate) startup_checks: Vec<f64>,
}

pub(crate) fn load_check(config: &Config) -> StoredCheck {
    let settings = read_settings(config);
    let Some(state) = settings.get("update_check").and_then(Value::as_object) else {
        return StoredCheck::default();
    };
    let mut startup_checks: Vec<f64> = state
        .get("startup_checks")
        .and_then(Value::as_array)
        .map(|items| items.iter().filter_map(positive_f64).collect())
        .unwrap_or_default();
    startup_checks.sort_by(f64::total_cmp);
    StoredCheck {
        last_attempt: state
            .get("last_attempt")
            .and_then(positive_f64)
            .unwrap_or(0.0),
        last_success: state
            .get("last_success")
            .and_then(positive_f64)
            .unwrap_or(0.0),
        latest_version: state
            .get("latest_version")
            .and_then(Value::as_str)
            .unwrap_or("")
            .chars()
            .take(32)
            .collect(),
        startup_checks,
    }
}

pub(crate) fn enabled(config: &Config) -> bool {
    read_settings(config)
        .get("update_check_enabled")
        .and_then(Value::as_bool)
        == Some(true)
}

pub(crate) fn persist_check(config: &Config, success: bool, version: &str, at: f64) {
    update_check(config, |state| {
        state.insert("last_attempt".into(), json!(at));
        if success {
            state.insert("last_success".into(), json!(at));
        }
        if !version.is_empty() {
            state.insert(
                "latest_version".into(),
                json!(version.chars().take(32).collect::<String>()),
            );
        }
    });
}

pub(crate) fn persist_startup(config: &Config, at: f64) {
    update_check(config, |state| {
        let mut stamps: Vec<f64> = state
            .get("startup_checks")
            .and_then(Value::as_array)
            .map(|items| items.iter().filter_map(positive_f64).collect())
            .unwrap_or_default();
        stamps.push(at);
        stamps.sort_by(f64::total_cmp);
        if stamps.len() > 12 {
            stamps.drain(..stamps.len() - 12);
        }
        state.insert("startup_checks".into(), json!(stamps));
    });
}

pub(crate) fn read_installation(path: &Path, max_bytes: u64) -> Value {
    let unavailable = || json!({"state": "unavailable", "version": "", "message": ""});
    let Ok(bytes) = std::fs::read(path) else {
        return json!({"state": "idle", "version": "", "message": ""});
    };
    if bytes.len() as u64 > max_bytes {
        return unavailable();
    }
    let Ok(Value::Object(mut payload)) = serde_json::from_slice::<Value>(&bytes) else {
        return unavailable();
    };
    if !is_visible_state(payload.get("state").and_then(Value::as_str).unwrap_or("")) {
        return unavailable();
    }
    for key in ["version", "message", "updated_at"] {
        if let Some(value) = payload.get_mut(key) {
            let limit = if key == "message" { 240 } else { 32 };
            let text: String = value.as_str().unwrap_or("").chars().take(limit).collect();
            *value = json!(text);
        }
    }
    Value::Object(payload)
}

pub(crate) fn write_installation(config: &Config, state: &str, version: &str, message: &str) {
    if let Some(parent) = config.status_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let payload = json!({
        "state": state,
        "version": version,
        "message": message.chars().take(240).collect::<String>(),
        "updated_at": crate::state::local_stamp(),
    });
    let _ = crate::state::write_json_atomic(&config.status_path, &payload);
}

fn read_settings(config: &Config) -> Map<String, Value> {
    let path = config.accounts_dir.join("settings.json");
    let Ok(bytes) = std::fs::read(path) else {
        return Map::new();
    };
    if bytes.len() as u64 > config.settings_max_bytes {
        return Map::new();
    }
    serde_json::from_slice::<Value>(&bytes)
        .ok()
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default()
}

fn update_check(config: &Config, mutate: impl FnOnce(&mut Map<String, Value>)) {
    let path = config.accounts_dir.join("settings.json");
    let mut root = read_settings(config);
    let state = root.entry("update_check").or_insert_with(|| json!({}));
    if !state.is_object() {
        *state = json!({});
    }
    mutate(state.as_object_mut().expect("update_check is object"));
    let _ = crate::state::write_json_atomic(&path, &Value::Object(root));
}

fn positive_f64(value: &Value) -> Option<f64> {
    value.as_f64().filter(|value| *value > 0.0)
}

fn is_visible_state(state: &str) -> bool {
    matches!(
        state,
        "idle"
            | "queued"
            | "downloading"
            | "verifying"
            | "backing_up"
            | "stopping"
            | "applying"
            | "restoring"
            | "restarting"
            | "checking"
            | "succeeded"
            | "restored"
            | "rolling_back"
            | "rolled_back"
            | "failed"
            | "unavailable"
    )
}

pub(crate) fn is_active_installation(value: &Value) -> bool {
    matches!(
        value.get("state").and_then(Value::as_str),
        Some(
            "queued"
                | "downloading"
                | "verifying"
                | "backing_up"
                | "stopping"
                | "applying"
                | "restoring"
                | "restarting"
                | "checking"
                | "rolling_back"
        )
    )
}
