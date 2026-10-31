//! Release discovery and panel update control.
//!
//! All values are environment-backed. Defaults and examples:
//! - releases: `WB_UPDATE_RELEASES_URL=https://api.github.com/repos/{repo}/releases`
//! - fallback: `WB_UPDATE_LATEST_URL=https://github.com/{repo}/releases/latest`
//! - repository/token: `WB_UPDATE_REPO=owner/repo`, `WB_UPDATE_TOKEN_ENV=GITHUB_TOKEN`
//! - cadence: `WB_UPDATE_INTERVAL_SECONDS=86400`, `WB_UPDATE_POLL_SECONDS=300`
//! - bounds: `WB_UPDATE_REQUEST_TIMEOUT_SECONDS=10`, `WB_UPDATE_MAX_BODY_BYTES=2097152`
//! - install: `WB_UPDATE_INSTALL_ENABLED=1`, `WB_UPDATE_BINARY_PATH=/opt/wb/wb-backend`
//! - backups: `WB_UPDATE_BACKUPS_DIR=updates`, `WB_UPDATE_REQUEST_DIR=/run/workbuddy2api-hub-updater`
//! - manifest: `WB_UPDATE_MANIFEST_URL=https://host/{repo}/{tag}/{target}.json`
//! - health: `WB_UPDATE_HEALTH_URL=http://127.0.0.1:{port}/health`

use std::sync::Arc;
use std::time::Duration;

use hyper::body::Incoming;
use hyper::{Request, StatusCode};
use serde_json::{json, Value};

use crate::http::{json_resp, Resp};
use crate::App;

pub use crate::update_manager::Manager;

fn authorized(app: &App, req: &Request<Incoming>) -> bool {
    app.panel.token_ok(
        req.headers()
            .get("x-panel-token")
            .and_then(|value| value.to_str().ok()),
    )
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

pub async fn get(app: &Arc<App>, req: &Request<Incoming>) -> Resp {
    if !authorized(app, req) {
        return unauthorized();
    }
    json_resp(StatusCode::OK, &app.updates.panel_status())
}

pub async fn check(app: &Arc<App>, req: &Request<Incoming>) -> Resp {
    if !authorized(app, req) {
        return unauthorized();
    }
    let mut status = app.updates.check(true, false).await;
    status["installation"] = app.updates.installation_status();
    status["install_enabled"] = json!(app.updates.config.install_supported());
    status["ok"] = json!(true);
    json_resp(StatusCode::OK, &status)
}

pub async fn install(app: &Arc<App>, req: &Request<Incoming>, port: u16) -> Resp {
    if !authorized(app, req) {
        return unauthorized();
    }
    match app.updates.start_install(port) {
        Ok(mut queued) => {
            queued["ok"] = json!(true);
            json_resp(StatusCode::ACCEPTED, &queued)
        }
        Err(message) if message == "安装更新服务尚未配置" => json_resp(
            StatusCode::SERVICE_UNAVAILABLE,
            &json!({"ok": false, "msg": message}),
        ),
        Err(message) => json_resp(StatusCode::CONFLICT, &json!({"ok": false, "msg": message})),
    }
}

pub async fn backups(app: &Arc<App>, req: &Request<Incoming>) -> Resp {
    if !authorized(app, req) {
        return unauthorized();
    }
    json_resp(StatusCode::OK, &crate::update_backups::view(app))
}

pub async fn rollback(app: &Arc<App>, req: Request<Incoming>, port: u16) -> Resp {
    if !authorized(app, &req) {
        return unauthorized();
    }
    let body = match crate::http::body_json(req).await {
        Ok(value) => value,
        Err(response) => return response,
    };
    let version = body
        .get("version")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    if version.is_empty() {
        return json_resp(
            StatusCode::CONFLICT,
            &json!({"ok": false, "msg": "请选择要回退的版本"}),
        );
    }
    match crate::update_backups::start_rollback(app, &version, port) {
        Ok(mut queued) => {
            queued["ok"] = json!(true);
            json_resp(StatusCode::ACCEPTED, &queued)
        }
        Err(message) => json_resp(
            StatusCode::CONFLICT,
            &json!({"ok": false, "msg": message}),
        ),
    }
}

pub fn spawn(app: Arc<App>) {
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_secs(app.updates.config.first_delay_seconds)).await;
        let mut startup = true;
        loop {
            let status = app.updates.check(false, startup).await;
            if let Some(error) = status.get("last_error").and_then(Value::as_str) {
                if !error.is_empty() {
                    app.log("update", "WARN", &format!("更新检查失败：{error}"));
                }
            }
            startup = false;
            tokio::time::sleep(Duration::from_secs(app.updates.config.poll_seconds)).await;
        }
    });
}

pub async fn run_helper_if_requested() -> bool {
    if !std::env::args().any(|arg| arg == "--wb-update-apply") {
        return false;
    }
    let _ = crate::update_apply::run_from_args().await;
    true
}
