//! `POST /v1/chat/completions`（非流式 + 流式）。
//!
//! 契约见 specs/chat-nonstream.md 与 specs/streaming-and-errors.md。流式的帧形状
//! 是拿真进程抓出来的（不是照上游原样转发），见 stream.rs。

use std::sync::atomic::Ordering;
use std::sync::Arc;

use hyper::body::Incoming;
use hyper::Request;
use serde_json::Value;

use crate::http::{body_json, Resp};
use crate::App;

pub async fn completions(app: &Arc<App>, req: Request<Incoming>, key_id: String) -> Resp {
    let started = std::time::Instant::now();
    let pin = crate::keyref::pinned(&req);
    let realm = crate::http::requested_realm(&req, &app.state.current_realm());
    let session = crate::http::session_key_header(req.headers());
    let body = match body_json(req).await {
        Ok(v) => v,
        Err(resp) => return resp,
    };
    let session = session.or_else(|| crate::http::session_key_body(&body));
    let session = session.as_deref();
    let stream = body.get("stream").and_then(Value::as_bool).unwrap_or(false);
    // Python 的 `chat: model=… client_effort=%r -> upstream_effort=%r …`：面板日志页的主要来源。
    // 客户端那栏按 Python 的四种拼法取，实际档位交给 upstream_effort_of 判。
    let model = body.get("model").and_then(Value::as_str).unwrap_or("-");
    let given = ["reasoning_effort", "reasoningEffort", "reasoning", "thinking", "enable_thinking"]
        .iter()
        .find_map(|key| body.get(*key).filter(|value| !value.is_null()));
    let effort = crate::upstream::upstream_effort_of(&body, &realm, model);
    let effort = effort.as_deref().map(Value::from);
    // msgs 数的是**发出去的**消息：build_upstream_body 会在首条不是 system 时补一条。
    let messages = body.get("messages").and_then(Value::as_array).map_or(&[][..], Vec::as_slice);
    let injected = !matches!(
        messages.first().and_then(|m| m.get("role")).and_then(Value::as_str),
        Some("system")
    );
    app.note(&format!(
        "chat: model={model} client_effort={} -> upstream_effort={} stream={} msgs={} key={}",
        crate::logs::show(given),
        crate::logs::show(effort.as_ref()),
        crate::logs::yn(stream),
        messages.len() + usize::from(injected),
        key_id.get(..8).unwrap_or(&key_id),
    ));
    app.inflight.fetch_add(1, Ordering::Relaxed);
    let result = if stream {
        crate::upstream::chat_stream(
            app,
            &body,
            &realm,
            started,
            &key_id,
            session,
            pin.as_ref(),
        )
        .await
    } else {
        crate::upstream::chat_once(
            app,
            &body,
            &realm,
            started,
            &key_id,
            session,
            pin.as_ref(),
        )
        .await
    };
    app.inflight.fetch_sub(1, Ordering::Relaxed);
    result
}
