//! `POST /v1/chat/completions`（非流式 + 流式）。
//!
//! 契约见 specs/chat-nonstream.md 与 specs/streaming-and-errors.md。流式的帧形状
//! 是拿真进程抓出来的（不是照上游原样转发），见 stream.rs。

use std::sync::atomic::Ordering;
use std::sync::Arc;

use hyper::body::Incoming;
use hyper::{Request, StatusCode};
use serde_json::Value;

use crate::http::{body_json, error_resp, Resp};
use crate::App;

pub async fn completions(app: &Arc<App>, req: Request<Incoming>, key_id: String) -> Resp {
    let started = std::time::Instant::now();
    let realm = crate::http::requested_realm(&req, &app.state.current_realm());
    let session = crate::http::session_key_header(req.headers());
    let body = match body_json(req).await {
        Ok(v) => v,
        Err(resp) => return resp,
    };
    let session = session.or_else(|| crate::http::session_key_body(&body));
    let session = session.as_deref();
    let stream = body.get("stream").and_then(Value::as_bool).unwrap_or(false);
    app.inflight.fetch_add(1, Ordering::Relaxed);
    let result = if stream {
        crate::upstream::chat_stream(app, &body, &realm, started, &key_id, session).await
    } else {
        crate::upstream::chat_once(app, &body, &realm, started, &key_id, session).await
    };
    app.inflight.fetch_sub(1, Ordering::Relaxed);
    result
}
