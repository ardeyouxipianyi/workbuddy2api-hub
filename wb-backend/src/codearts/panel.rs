//! `/codearts/*` 面板接口与 `/oauth/callback` 浏览器回跳。
//!
//! 对照 `wb_proxy.py` 的 `_get_codearts_overview`/`_get_codearts_accounts`/
//! `_handle_codearts` 与 `wb_codearts.py` 的 admin 函数。

use std::sync::Arc;

use hyper::body::Incoming;
use hyper::{Method, Request, StatusCode};
use serde_json::{json, Value};

use super::{admin, oauth, CodeArtsError};
use crate::http::{body_json, json_resp, Resp};
use crate::App;

fn error_resp(status: StatusCode, message: &str, kind: &str) -> Resp {
    json_resp(
        status,
        &json!({"error": {"message": message, "type": kind, "code": status.as_u16()}}),
    )
}

fn not_configured() -> Resp {
    error_resp(
        StatusCode::SERVICE_UNAVAILABLE,
        "CodeArts 出口未启用：请到设置页填写 Base URL 和 API Key。",
        "invalid_request_error",
    )
}

/// `_codearts_admin_error`：只有「account not found」才是 404，其余 NotFound 是版本过旧的 409。
fn admin_error(err: &CodeArtsError) -> Resp {
    match err {
        CodeArtsError::NotFound(message) => {
            if message.to_lowercase().contains("account not found") {
                error_resp(StatusCode::NOT_FOUND, message, "invalid_request_error")
            } else {
                error_resp(
                    StatusCode::CONFLICT,
                    "codearts2api 版本过旧，没有删除账号接口；请升级 apps/codearts 后重建容器。",
                    "invalid_request_error",
                )
            }
        }
        CodeArtsError::BadRequest(message) | CodeArtsError::Auth(message) => {
            error_resp(StatusCode::BAD_REQUEST, message, "invalid_request_error")
        }
        other => error_resp(StatusCode::BAD_GATEWAY, other.message(), "invalid_request_error"),
    }
}

pub async fn overview(app: &Arc<App>) -> Resp {
    let pool = super::pool_for(app);
    if !pool.config().enabled {
        return not_configured();
    }
    let mut data = admin::overview(&pool, false).await;
    let health = admin::health(&pool);
    if let Some(map) = data.as_object_mut() {
        map.insert("online".into(), health["ok"].clone());
        map.insert("health".into(), health);
        map.insert("base_url".into(), json!(super::snap_host()));
    }
    json_resp(StatusCode::OK, &data)
}

pub async fn accounts(app: &Arc<App>) -> Resp {
    let pool = super::pool_for(app);
    if !pool.config().enabled {
        return not_configured();
    }
    let data = admin::overview(&pool, false).await;
    json_resp(
        StatusCode::OK,
        &json!({
            "provider": "codearts",
            "accounts": data.get("accounts").cloned().unwrap_or(json!([])),
            "stats": data.get("stats").cloned().unwrap_or(json!({})),
            "models": data.get("models").cloned().unwrap_or(json!([])),
            "base_url": super::snap_host(),
        }),
    )
}

/// 面板 `/codearts/*`：GET 读视图，POST 走管理动作（都要面板会话）。
pub async fn handle(app: &Arc<App>, req: Request<Incoming>) -> Resp {
    if !crate::panel::session_ok(app, &req) {
        return error_resp(StatusCode::UNAUTHORIZED, "panel password required", "invalid_request_error");
    }
    let path = req.uri().path().to_string();
    if req.method() == Method::GET {
        return match path.as_str() {
            "/codearts/overview" => overview(app).await,
            "/codearts/accounts" => accounts(app).await,
            _ => error_resp(StatusCode::NOT_FOUND, "unknown codearts endpoint", "invalid_request_error"),
        };
    }
    let body = match body_json(req).await {
        Ok(value) => value,
        Err(resp) => return resp,
    };
    if !body.is_object() {
        return error_resp(StatusCode::BAD_REQUEST, "expected a JSON object", "invalid_request_error");
    }
    let pool = super::pool_for(app);
    if !pool.config().enabled {
        return not_configured();
    }
    let uid = body.get("uid").and_then(Value::as_str).unwrap_or("").trim().to_string();
    let result: Result<Value, CodeArtsError> = match path.as_str() {
        "/codearts/test" => {
            let health = admin::health(&pool);
            if !health["ok"].as_bool().unwrap_or(false) {
                return json_resp(StatusCode::BAD_GATEWAY, &json!({"ok": false, "health": health}));
            }
            let data = admin::overview(&pool, true).await;
            Ok(json!({
                "ok": true,
                "health": health,
                "stats": data.get("stats").cloned().unwrap_or(json!({})),
                "models": data.get("models").and_then(Value::as_array).map(|list| list.len()).unwrap_or(0),
            }))
        }
        "/codearts/accounts/oauth/start" => Ok(oauth::start(app.listen_port)),
        "/codearts/accounts/oauth/poll" => {
            let session_id = body.get("session_id").and_then(Value::as_str).unwrap_or("");
            oauth::poll(&pool, session_id).await
        }
        "/codearts/accounts/oauth/callback" => {
            let session_id = body.get("session_id").and_then(Value::as_str).unwrap_or("");
            let callback_url = body.get("callback_url").and_then(Value::as_str).unwrap_or("");
            oauth::callback(&pool, session_id, callback_url).await
        }
        "/codearts/keepalive" => Ok(admin::keepalive(&pool).await),
        "/codearts/models/refresh" => Ok(super::models::refresh_models(&pool).await),
        "/codearts/accounts/checkin" => admin::checkin(&pool, &uid).await,
        "/codearts/accounts/proxy" => {
            let slot = body.get("slot_id").and_then(Value::as_str).unwrap_or("");
            admin::set_proxy_slot(&pool, &uid, slot)
        }
        _ => {
            if uid.is_empty() {
                return error_resp(StatusCode::BAD_REQUEST, "uid required", "invalid_request_error");
            }
            match path.as_str() {
                "/codearts/accounts/enable" => Ok(if pool.enable(&uid) {
                    json!({"ok": true, "message": format!("已启用 {uid}")})
                } else {
                    return admin_error(&CodeArtsError::NotFound("account not found".into()));
                }),
                "/codearts/accounts/disable" => {
                    let reason = body.get("reason").and_then(Value::as_str).unwrap_or("");
                    Ok(if pool.disable(&uid, if reason.is_empty() { "manual disable" } else { reason }) {
                        json!({"ok": true, "message": format!("已停用 {uid}")})
                    } else {
                        return admin_error(&CodeArtsError::NotFound("account not found".into()));
                    })
                }
                "/codearts/accounts/delete" => Ok(if pool.remove(&uid) {
                    json!({"ok": true, "deleted": true, "message": format!("已删除 {uid}")})
                } else {
                    return admin_error(&CodeArtsError::NotFound("account not found".into()));
                }),
                "/codearts/accounts/clear-cooldown" => Ok(if pool.clear_cooldown(&uid) {
                    json!({"ok": true, "message": format!("已清冷却 {uid}")})
                } else {
                    return admin_error(&CodeArtsError::NotFound("account not found".into()));
                }),
                _ => return error_resp(StatusCode::NOT_FOUND, "unknown codearts endpoint", "invalid_request_error"),
            }
        }
    };
    match result {
        Ok(value) => json_resp(StatusCode::OK, &value),
        Err(err) => admin_error(&err),
    }
}

/// 浏览器 `GET /oauth/callback`：回 HTML（浏览器带不了面板 token，所以不鉴权）。
pub async fn callback_get(app: &Arc<App>, query: &[(String, String)]) -> Resp {
    let pool = super::pool_for(app);
    let (status, html) = oauth::callback_get(&pool, query).await;
    let status = StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_REQUEST);
    let mut response = hyper::Response::new(crate::http::full(hyper::body::Bytes::from(html)));
    *response.status_mut() = status;
    response.headers_mut().insert(
        hyper::header::CONTENT_TYPE,
        hyper::header::HeaderValue::from_static("text/html; charset=utf-8"),
    );
    response
}
