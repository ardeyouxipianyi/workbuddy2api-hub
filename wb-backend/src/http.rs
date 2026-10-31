//! 路由与鉴权（M1 子集）。
//!
//! 越界返回 404 而不是猜一个 handler：M2 起逐条搬，未搬的路由返回 501 并在
//! capabilities 里保持 `ported: false`，面板据此显示「不可用」。

use std::sync::Arc;

use http_body_util::combinators::BoxBody;
use http_body_util::{BodyExt, Full};
use hyper::body::{Bytes, Incoming};
use hyper::{Method, Request, Response, StatusCode};
use serde_json::{json, Value};

use crate::{auth, health, models, App};

/// Client address retained past hyper's connection layer for local-only routes.
#[derive(Clone, Copy)]
pub struct PeerAddr(pub std::net::IpAddr);

/// 流式与非流式共用一种响应体：M1 的 `Full` 装不下 SSE 的增量写。
pub type Resp = Response<BoxBody<Bytes, std::convert::Infallible>>;

fn full(bytes: Bytes) -> BoxBody<Bytes, std::convert::Infallible> {
    Full::new(bytes).map_err(|e| match e {}).boxed()
}

pub fn json_resp(code: StatusCode, payload: &Value) -> Resp {
    let body = serde_json::to_vec(payload).unwrap_or_else(|_| b"{}".to_vec());
    let mut resp = Response::new(full(Bytes::from(body)));
    *resp.status_mut() = code;
    resp.headers_mut().insert(
        hyper::header::CONTENT_TYPE,
        hyper::header::HeaderValue::from_static("application/json"),
    );
    resp
}

pub fn text_resp(code: StatusCode, text: &str) -> Resp {
    let mut resp = Response::new(full(Bytes::from(text.to_string())));
    *resp.status_mut() = code;
    resp
}

/// Python 版所有错误都是 `{"error": {...}}`；M1 只用到这一种信封。
pub fn error_resp(code: StatusCode, message: &str) -> Resp {
    json_resp(code, &json!({"error": {"message": message, "type": "invalid_request_error"}}))
}

/// `/v1` 的鉴权闸门：返回写进 usage 行的 key id，失败时返回 401 响应。
/// 报文与 Python 版（wb_proxy.py:18866-18881）逐字一致，客户端看到的是同一句提示。
pub fn require_auth(app: &App, req: &Request<Incoming>) -> Result<String, Resp> {
    let supplied = auth::supplied(req.headers(), req.uri().query());
    match auth::check(&app.state, &supplied) {
        Ok(passed) if !passed.required => Ok(String::new()),
        Ok(passed) => Ok(passed.key_id),
        Err(()) => {
            let has_key = app
                .state
                .settings()
                .get("api_keys")
                .and_then(Value::as_array)
                .map(|keys| {
                    keys.iter().any(|k| k.get("enabled").and_then(Value::as_bool).unwrap_or(false))
                })
                .unwrap_or(false)
                || std::env::var("WB_API_KEY").map(|v| !v.is_empty()).unwrap_or(false);
            let hint = if has_key {
                "send it as 'Authorization: Bearer ***' or '?key=<key>'; \
                 copy the value from the panel's 设置 page"
            } else {
                "no key is configured - open the dashboard and add one, \
                 or restart with --api-key"
            };
            Err(error_resp(
                StatusCode::UNAUTHORIZED,
                &format!("invalid api key - {hint}"),
            ))
        }
    }
}

pub async fn route(app: Arc<App>, req: Request<Incoming>) -> Resp {
    let path = req.uri().path().to_string();
    let method = req.method().clone();
    // 账号与设置按 mtime 重读：面板改 key、切区域、换号都不用重启。
    app.state.refresh();
    match (&method, path.as_str()) {
        (&Method::GET, "/health") => health::get(&app).await,
        (&Method::GET, "/v1/models") | (&Method::GET, "/models") => models::list(&app, &req).await,
        (&Method::POST, "/v1/chat/completions") | (&Method::POST, "/chat/completions") => {
            match require_auth(&app, &req) {
                Ok(key_id) => crate::chat::completions(&app, req, key_id).await,
                Err(resp) => resp,
            }
        }
        (&Method::POST, "/v1/responses") | (&Method::POST, "/responses") => {
            match require_auth(&app, &req) {
                Ok(key_id) => crate::responses::handle(&app, req, key_id).await,
                Err(resp) => resp,
            }
        }
        (&Method::OPTIONS, _) => text_resp(StatusCode::NO_CONTENT, ""),
        (_, "/health") | (_, "/v1/models") | (_, "/v1/chat/completions") | (_, "/v1/responses") => {
            error_resp(StatusCode::METHOD_NOT_ALLOWED, "method not allowed")
        }
        _ => match crate::panel::handle(&app, req).await {
            Some(resp) => resp,
            None => error_resp(StatusCode::NOT_FOUND, "not found"),
        },
    }
}

/// `?realm=` > `X-Realm` 头 > 当前区域（wb_proxy.py:19224 同一优先级）。
/// 会话键（specs/identity-upstream.md §1.6）：头优先，再 body 三个位置。
/// 都没有就返回 None，由 identity 层每请求新生成（上游按它做会话亲和）。
pub fn session_key_header(headers: &hyper::HeaderMap) -> Option<String> {
    for name in ["x-conversation-id", "conversation-id", "x-session-id", "session-id"] {
        if let Some(v) = headers.get(name).and_then(|v| v.to_str().ok()) {
            if !v.trim().is_empty() {
                return Some(v.trim().to_string());
            }
        }
    }
    None
}

pub fn session_key_body(body: &Value) -> Option<String> {
    let candidates = [
        body.get("conversation_id"),
        body.get("session_id"),
        body.get("metadata").and_then(|m| m.get("conversation_id")),
    ];
    for value in candidates.into_iter().flatten() {
        if let Some(s) = value.as_str() {
            if !s.trim().is_empty() {
                return Some(s.trim().to_string());
            }
        }
    }
    None
}

pub fn requested_realm(req: &Request<Incoming>, current: &str) -> String {
    if let Some(q) = req.uri().query() {
        for pair in q.split('&') {
            if let Some(v) = pair.strip_prefix("realm=") {
                if v == "cn" || v == "intl" {
                    return v.to_string();
                }
            }
        }
    }
    if let Some(v) = req.headers().get("x-realm").and_then(|v| v.to_str().ok()) {
        if v == "cn" || v == "intl" {
            return v.to_string();
        }
    }
    current.to_string()
}

/// 请求体上限：`WB_MAX_PAYLOAD_BYTES`，默认 16 MiB（wb_proxy.py:42）。
fn payload_limit() -> usize {
    std::env::var("WB_MAX_PAYLOAD_BYTES")
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .unwrap_or(16 * 1024 * 1024)
}

/// 超限报文的 `gateway_hint`（wb_proxy.py:13722）——客户端/人靠它知道去改哪个开关。
fn gateway_hint_413() -> &'static str {
    "single request body exceeds the gateway cap (WB_MAX_PAYLOAD_BYTES, default 16MB); \
     raise it and restart wb-proxy"
}

/// 读整个请求体；超限时回 Python 版同一条 413 报文。
pub async fn body_json(req: Request<Incoming>) -> Result<Value, Resp> {
    let limit = payload_limit();
    let bytes = http_body_util::BodyExt::collect(req.into_body())
        .await
        .map_err(|e| {
            error_resp(StatusCode::BAD_REQUEST, &format!("invalid request body: {e}"))
        })?
        .to_bytes();
    if bytes.len() > limit {
        return Err(json_resp(
            StatusCode::PAYLOAD_TOO_LARGE,
            &json!({"error": {
                "message": format!("payload too large ({} bytes > {} limit)", bytes.len(), limit),
                "type": "invalid_request_error",
                "code": 413,
                "gateway_hint": gateway_hint_413(),
            }}),
        ));
    }
    serde_json::from_slice(&bytes).map_err(|e| {
        error_resp(StatusCode::BAD_REQUEST, &format!("invalid json: {e}"))
    })
}
