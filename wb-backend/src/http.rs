//! 路由与鉴权（M1 子集）。
//!
//! 越界返回 404 而不是猜一个 handler：M2 起逐条搬，未搬的路由返回 501 并在
//! capabilities 里保持 `ported: false`，面板据此显示「不可用」。

use std::sync::Arc;

use http_body_util::Full;
use hyper::body::{Bytes, Incoming};
use hyper::{Method, Request, Response, StatusCode};
use serde_json::{json, Value};

use crate::{health, models, App};

pub type Resp = Response<Full<Bytes>>;

pub fn json_resp(code: StatusCode, payload: &Value) -> Resp {
    let body = serde_json::to_vec(payload).unwrap_or_else(|_| b"{}".to_vec());
    let mut resp = Response::new(Full::new(Bytes::from(body)));
    *resp.status_mut() = code;
    resp.headers_mut().insert(
        hyper::header::CONTENT_TYPE,
        hyper::header::HeaderValue::from_static("application/json"),
    );
    resp
}

fn text_resp(code: StatusCode, text: &str) -> Resp {
    let mut resp = Response::new(Full::new(Bytes::from(text.to_string())));
    *resp.status_mut() = code;
    resp
}

/// Python 版所有错误都是 `{"error": {...}}`；M1 只用到这一种信封。
pub fn error_resp(code: StatusCode, message: &str) -> Resp {
    json_resp(code, &json!({"error": {"message": message, "type": "invalid_request_error"}}))
}

pub async fn route(app: Arc<App>, req: Request<Incoming>) -> Resp {
    let path = req.uri().path().to_string();
    let method = req.method().clone();
    match (&method, path.as_str()) {
        (&Method::GET, "/health") => health::get(&app).await,
        (&Method::GET, "/v1/models") | (&Method::GET, "/models") => models::list(&app, &req).await,
        (&Method::POST, "/v1/chat/completions") | (&Method::POST, "/chat/completions") => {
            crate::chat::completions(&app, req).await
        }
        (&Method::OPTIONS, _) => text_resp(StatusCode::NO_CONTENT, ""),
        (_, "/health") | (_, "/v1/models") | (_, "/v1/chat/completions") => {
            error_resp(StatusCode::METHOD_NOT_ALLOWED, "method not allowed")
        }
        _ => error_resp(StatusCode::NOT_FOUND, "not found"),
    }
}

/// `?realm=` > `X-Realm` 头 > 当前区域（wb_proxy.py:19224 同一优先级）。
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
