//! 出口级瞬时失败退避（`wb_egress.py` 的 Rust 版）。
//!
//! 同一物理出口共享一个秒级计数器；任一成功清空它。开关沿用 Python 的
//! `egress_backoff_enabled` 设置，默认关闭。

#![allow(dead_code)] // 父进程尚未统一接线；handler 是稳定入口。

use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

use hyper::body::Incoming;
use hyper::{Method, Request, StatusCode};
use serde_json::{json, Value};

use crate::http::{body_json, json_resp, Resp};
use crate::App;

#[derive(Clone, Copy, Debug)]
pub struct Config {
    pub base: f64,
    pub max: f64,
    pub streak_reset: f64,
    pub jitter_low: f64,
    pub jitter_high: f64,
}

impl Default for Config {
    fn default() -> Self {
        let num = |name: &str, default: f64| {
            std::env::var(name)
                .ok()
                .and_then(|v| v.parse::<f64>().ok())
                .filter(|v| v.is_finite() && *v >= 0.0)
                .unwrap_or(default)
        };
        Self {
            base: num("WB_EGRESS_BACKOFF_BASE", 1.0),
            max: num("WB_EGRESS_BACKOFF_MAX", 15.0),
            streak_reset: num("WB_EGRESS_STREAK_RESET", 60.0),
            jitter_low: num("WB_EGRESS_JITTER_LOW", 0.75),
            jitter_high: num("WB_EGRESS_JITTER_HIGH", 1.25),
        }
    }
}

#[derive(Clone, Copy)]
struct Entry {
    streak: u32,
    last_hit: f64,
}

fn state() -> &'static Mutex<HashMap<String, Entry>> {
    static STATE: OnceLock<Mutex<HashMap<String, Entry>>> = OnceLock::new();
    STATE.get_or_init(|| Mutex::new(HashMap::new()))
}

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// 连续 `streak` 次抖动后的未加抖动退避秒数。
pub fn transient_backoff(streak: u32, config: Config) -> f64 {
    let step = streak.saturating_sub(1).min(20);
    (config.base * 2_f64.powi(step as i32)).min(config.max)
}

fn jitter(wait: f64, config: Config, key: &str, streak: u32, at: f64) -> f64 {
    use sha2::{Digest, Sha256};

    if config.jitter_high <= config.jitter_low {
        return wait * config.jitter_low;
    }
    let digest = Sha256::digest(format!("{key}|{streak}|{at:.9}").as_bytes());
    let raw = u64::from_be_bytes(digest[..8].try_into().unwrap_or([0; 8]));
    let unit = raw as f64 / u64::MAX as f64;
    wait * (config.jitter_low + (config.jitter_high - config.jitter_low) * unit)
}

/// 记一次出口级瞬时失败，返回本次带抖动的退避秒数。
pub fn note_transient(key: &str) -> f64 {
    note_transient_at(key, now(), Config::default())
}

fn note_transient_at(key: &str, at: f64, config: Config) -> f64 {
    let mut all = state().lock().unwrap_or_else(|e| e.into_inner());
    let mut entry = all.get(key).copied().unwrap_or(Entry {
        streak: 0,
        last_hit: at,
    });
    if at - entry.last_hit > config.streak_reset {
        entry.streak = 0;
    }
    entry.streak = entry.streak.saturating_add(1);
    entry.last_hit = at;
    all.insert(key.to_string(), entry);
    jitter(
        transient_backoff(entry.streak, config),
        config,
        key,
        entry.streak,
        at,
    )
}

/// 这一段出口成功一次，清空连续计数。
pub fn note_success(key: &str) {
    state()
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .remove(key);
}

/// 当前连续抖动次数；超过静默窗口后读作 0。
pub fn streak(key: &str) -> u32 {
    streak_at(key, now(), Config::default())
}

fn streak_at(key: &str, at: f64, config: Config) -> u32 {
    state()
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .get(key)
        .filter(|entry| at - entry.last_hit <= config.streak_reset)
        .map(|entry| entry.streak)
        .unwrap_or(0)
}

pub fn reset() {
    state().lock().unwrap_or_else(|e| e.into_inner()).clear();
}

fn token_of(req: &Request<Incoming>) -> Option<&str> {
    req.headers()
        .get("x-panel-token")
        .and_then(|v| v.to_str().ok())
}

fn unauthorized() -> Resp {
    json_resp(
        StatusCode::UNAUTHORIZED,
        &json!({"error": {
            "message": "panel password required",
            "type": "invalid_request_error",
            "code": 401
        }}),
    )
}

/// `/egress` 面板路由：GET 读开关，POST 只接受 Python settings 的同形字段。
///
/// POST payload: `{"egress_backoff_enabled": true|false}`。
pub async fn handle(app: &std::sync::Arc<App>, req: Request<Incoming>) -> Resp {
    if !app.panel.token_ok(token_of(&req)) {
        return unauthorized();
    }
    match *req.method() {
        Method::GET => get_setting(app),
        Method::POST => update_setting(app, req).await,
        _ => json_resp(
            StatusCode::METHOD_NOT_ALLOWED,
            &json!({"error": {
                "message": "method not allowed",
                "type": "invalid_request_error",
                "code": 405
            }}),
        ),
    }
}

fn get_setting(app: &App) -> Resp {
    json_resp(
        StatusCode::OK,
        &json!({
            "egress_backoff_enabled":
                app.state.setting_bool("egress_backoff_enabled", false)
        }),
    )
}

async fn update_setting(app: &std::sync::Arc<App>, req: Request<Incoming>) -> Resp {
    let body = match body_json(req).await {
        Ok(v) => v,
        Err(resp) => return resp,
    };
    let Some(payload) = body.as_object() else {
        return json_resp(
            StatusCode::BAD_REQUEST,
            &json!({"error": {
                "message": "expected a JSON object",
                "type": "invalid_request_error",
                "code": 400
            }}),
        );
    };
    let Some(enabled) = payload.get("egress_backoff_enabled") else {
        return json_resp(StatusCode::OK, &json!({"ok": true}));
    };
    let Some(enabled) = enabled.as_bool() else {
        return json_resp(
            StatusCode::BAD_REQUEST,
            &json!({"error": {
                "message": "egress_backoff_enabled must be true or false",
                "type": "invalid_request_error",
                "code": 400
            }}),
        );
    };
    let mut settings = app.state.settings_snapshot();
    settings.insert("egress_backoff_enabled".into(), json!(enabled));
    let path = app.state.accounts_dir.join("settings.json");
    if let Some(parent) = path.parent() {
        if let Err(e) = std::fs::create_dir_all(parent) {
            return save_error(&e);
        }
    }
    match crate::state::write_json_atomic(&path, &Value::Object(settings)) {
        Ok(()) => {
            app.state.refresh();
            json_resp(
                StatusCode::OK,
                &json!({"ok": true, "egress_backoff_enabled": enabled}),
            )
        }
        Err(e) => save_error(&e),
    }
}

fn save_error(error: &std::io::Error) -> Resp {
    json_resp(
        StatusCode::INTERNAL_SERVER_ERROR,
        &json!({"error": {
            "message": format!("cannot save settings: {error}"),
            "type": "server_error",
            "code": 500
        }}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config() -> Config {
        Config {
            base: 1.0,
            max: 15.0,
            streak_reset: 60.0,
            jitter_low: 0.75,
            jitter_high: 1.25,
        }
    }

    #[test]
    fn backoff_ladder_matches_python_output() {
        // Python: transient_backoff(1/2/3/9) == 1.0/2.0/4.0/15.0
        let c = config();
        assert_eq!(transient_backoff(1, c), 1.0);
        assert_eq!(transient_backoff(2, c), 2.0);
        assert_eq!(transient_backoff(3, c), 4.0);
        assert_eq!(transient_backoff(9, c), 15.0);
    }

    #[test]
    fn shared_counter_success_and_quiet_window_match_python() {
        reset();
        let c = config();
        let key = "https|www.workbuddy.ai|443|";
        let at = 1000.0;
        let first = note_transient_at(key, at, c);
        assert!((0.75..=1.25).contains(&first));
        let second = note_transient_at(key, at + 1.0, c);
        assert!((1.5..=2.5).contains(&second));
        assert_eq!(streak_at(key, at + 1.0, c), 2);
        assert_eq!(streak_at(key, at + 65.0, c), 0);
        note_success(key);
        assert_eq!(streak_at(key, at + 1.0, c), 0);
        reset();
    }
}
