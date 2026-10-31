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

/// 读整个请求体。M1 的上限沿用 Python 版（WB_MAX_BODY_MB，默认 64）。
pub async fn body_json(req: Request<Incoming>) -> Result<Value, String> {
    let limit = std::env::var("WB_MAX_BODY_MB")
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .unwrap_or(64)
        * 1024
        * 1024;
    let bytes = http_body_util::BodyExt::collect(req.into_body())
        .await
        .map_err(|e| e.to_string())?
        .to_bytes();
    if bytes.len() > limit {
        return Err("request body too large".into());
    }
    serde_json::from_slice(&bytes).map_err(|e| format!("invalid json: {e}"))
}
