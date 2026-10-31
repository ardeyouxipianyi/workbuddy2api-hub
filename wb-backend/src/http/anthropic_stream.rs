//! HTTP streaming transport for Anthropic Messages SSE.

use std::sync::Arc;

use http_body_util::{BodyExt, StreamBody};
use hyper::body::{Bytes, Frame};
use hyper::StatusCode;
use serde_json::{json, Value};

use super::anthropic_sse::{event, process_frame, StreamState};
use crate::http::anthropic_translate;
use crate::App;

type Tx = tokio::sync::mpsc::Sender<Result<Frame<Bytes>, std::convert::Infallible>>;

async fn pump(
    upstream: &mut reqwest::Response,
    tx: &Tx,
    state: &mut StreamState,
    aggregate: &mut crate::sse::Aggregated,
) -> (Option<f64>, Option<String>) {
    let mut first_at = None;
    let mut buffer = String::new();
    loop {
        let chunk = match upstream.chunk().await {
            Ok(Some(chunk)) => chunk,
            Ok(None) => return (first_at, None),
            Err(error) => return (first_at, Some(format!("upstream stream aborted: {error}"))),
        };
        first_at.get_or_insert_with(crate::sse::epoch_now);
        buffer.push_str(&String::from_utf8_lossy(&chunk));
        while let Some(pos) = buffer.find('\n') {
            let line: String = buffer.drain(..=pos).collect();
            if line.trim() == "data: [DONE]" {
                return (first_at, None);
            }
            let Some(raw) = crate::stream::parse_frame(&line) else {
                continue;
            };
            let Ok(chunk) = serde_json::from_str::<Value>(raw) else {
                continue;
            };
            let out = match process_frame(state, aggregate, &chunk) {
                Ok(out) => out,
                Err(error) => return (first_at, Some(error)),
            };
            if !out.is_empty() && tx.send(Ok(Frame::data(Bytes::from(out)))).await.is_err() {
                return (first_at, Some("client disconnected".into()));
            }
        }
    }
}

fn finish(state: &mut StreamState, usage: &Value, broken: Option<&str>) -> String {
    if let Some(message) = broken {
        return event(
            "error",
            json!({
                "error": {"type": "api_error", "message": message}
            }),
        );
    }
    let mut tail = if state.started() {
        String::new()
    } else {
        state.start(None).unwrap_or_default()
    };
    tail.push_str(&state.finish_blocks());
    tail.push_str(&event(
        "message_delta",
        json!({
            "delta": {"stop_reason": state.stop(), "stop_sequence": null},
            "usage": anthropic_translate::usage(if usage.is_null() { None } else { Some(usage) }),
        }),
    ));
    tail.push_str(&event("message_stop", json!({})));
    tail
}

fn record(
    app: &App,
    candidate: &crate::pool::Candidate,
    ctx: &crate::upstream::Ctx,
    key_id: &str,
    started: std::time::Instant,
    first_at: Option<f64>,
    usage: &Value,
    broken: bool,
) {
    let elapsed = started.elapsed().as_millis() as i64;
    let started_at = crate::sse::epoch_now() - started.elapsed().as_secs_f64();
    let first_ms = first_at.map(|at| ((at - started_at) * 1000.0) as i64);
    crate::record::record(
        &app.recorder,
        crate::record::Row {
            model: &ctx.model,
            stream: true,
            outcome: if broken { "failed" } else { "completed" },
            elapsed_ms: Some(elapsed),
            ttft_ms: first_ms,
            gen_ms: first_ms.map(|first| elapsed - first),
            requested_model: Some(&ctx.requested_model),
            usage: if usage.is_null() { None } else { Some(usage) },
            fp: Some(&ctx.fp),
            account: Some(&candidate.uid),
            realm: &candidate.realm,
            key: key_id,
            effort: ctx.effort.as_deref(),
        },
    );
}

fn sse_stream(
    app: Arc<App>,
    candidate: crate::pool::Candidate,
    ctx: crate::upstream::Ctx,
    key_id: String,
    started: std::time::Instant,
    mut upstream: reqwest::Response,
) -> tokio::sync::mpsc::Receiver<Result<Frame<Bytes>, std::convert::Infallible>> {
    let (tx, rx) = tokio::sync::mpsc::channel(32);
    tokio::spawn(async move {
        let mut state = StreamState::new(&ctx.model);
        let mut aggregate = crate::sse::Aggregated {
            id: String::new(),
            model: ctx.model.clone(),
            created: 0,
            content: String::new(),
            reasoning: String::new(),
            tool_calls: Vec::new(),
            usage: None,
            finish: None,
            first_chunk_at: None,
        };
        let (first_at, broken) = pump(&mut upstream, &tx, &mut state, &mut aggregate).await;
        aggregate.first_chunk_at = first_at;
        let usage = aggregate.client_usage();
        let tail = finish(&mut state, &usage, broken.as_deref());
        let _ = tx.send(Ok(Frame::data(Bytes::from(tail)))).await;
        record(
            &app,
            &candidate,
            &ctx,
            &key_id,
            started,
            first_at,
            &usage,
            broken.is_some(),
        );
    });
    rx
}

pub fn response(
    app: Arc<App>,
    candidate: crate::pool::Candidate,
    ctx: crate::upstream::Ctx,
    upstream: reqwest::Response,
    started: std::time::Instant,
    key_id: String,
) -> crate::http::Resp {
    let rx = sse_stream(app, candidate, ctx, key_id, started, upstream);
    let body = StreamBody::new(tokio_stream::wrappers::ReceiverStream::new(rx));
    let mut response = hyper::Response::new(BodyExt::boxed(body));
    *response.status_mut() = StatusCode::OK;
    let headers = response.headers_mut();
    headers.insert(
        hyper::header::CONTENT_TYPE,
        hyper::header::HeaderValue::from_static("text/event-stream; charset=utf-8"),
    );
    headers.insert(
        hyper::header::CACHE_CONTROL,
        hyper::header::HeaderValue::from_static("no-cache"),
    );
    headers.insert(
        hyper::header::CONNECTION,
        hyper::header::HeaderValue::from_static("close"),
    );
    headers.insert(
        hyper::header::ACCESS_CONTROL_ALLOW_ORIGIN,
        hyper::header::HeaderValue::from_static("*"),
    );
    response
}
