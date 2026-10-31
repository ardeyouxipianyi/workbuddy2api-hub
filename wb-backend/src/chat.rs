//! `POST /v1/chat/completions`（M1：非流式）。
//!
//! 契约见 specs/chat-nonstream.md，字节对齐由 harness 判定。流式（`stream:true`）
//! 在 M2 接，现在显式报 501 而不是假装成功。

use std::sync::atomic::Ordering;
use std::sync::Arc;

use hyper::body::Incoming;
use hyper::{Request, StatusCode};
use serde_json::Value;

use crate::http::{body_json, error_resp, Resp};
use crate::App;

pub async fn completions(app: &Arc<App>, req: Request<Incoming>) -> Resp {
    let started = std::time::Instant::now();
    let realm = crate::http::requested_realm(&req, &app.state.current_realm());
    let body = match body_json(req).await {
        Ok(v) => v,
        Err(e) => return error_resp(StatusCode::BAD_REQUEST, &e),
    };
    let stream = body.get("stream").and_then(Value::as_bool).unwrap_or(false);
    app.inflight.fetch_add(1, Ordering::Relaxed);
    let result = if stream {
        // M2：SSE。现在返回 501，避免「静默降级成非流式」骗过客户端。
        error_resp(
            StatusCode::NOT_IMPLEMENTED,
            "this backend has not ported streaming yet (capabilities: protocols.ported=false)",
        )
    } else {
        crate::upstream::chat_once(app, &body, &realm, started).await
    };
    app.inflight.fetch_sub(1, Ordering::Relaxed);
    result
}
