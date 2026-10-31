//! Anthropic Messages API routes.

use std::sync::atomic::Ordering;
use std::sync::Arc;

use http_body_util::BodyExt;
use hyper::body::Incoming;
use hyper::{Request, StatusCode};
use serde_json::{json, Value};

use crate::http::protocol::{self, BodyError, Identity};
use crate::http::{json_resp, Resp};
use crate::App;

const DISABLED: &str =
    "Anthropic Messages is disabled by default: this gateway translates requests to Chat \
     Completions and ignores cache_control. This can cause excessive credit consumption. \
     Enable it in the Settings panel or set WB_ENABLE_ANTHROPIC_MESSAGES=1 to opt in at your \
     own risk (see issue #367).";

pub fn error(status: StatusCode, message: &str, error_type: Option<&str>) -> Resp {
    let kind = error_type.unwrap_or_else(|| match status {
        StatusCode::BAD_REQUEST | StatusCode::UNPROCESSABLE_ENTITY => "invalid_request_error",
        StatusCode::UNAUTHORIZED => "authentication_error",
        StatusCode::FORBIDDEN => "permission_error",
        StatusCode::NOT_FOUND => "not_found_error",
        StatusCode::TOO_MANY_REQUESTS => "rate_limit_error",
        StatusCode::SERVICE_UNAVAILABLE => "overloaded_error",
        _ => "api_error",
    });
    json_resp(
        status,
        &json!({"type": "error", "error": {"type": kind, "message": message}}),
    )
}

fn enabled(app: &App) -> bool {
    app.state.setting_bool("enable_anthropic_messages", false)
        || std::env::var("WB_ENABLE_ANTHROPIC_MESSAGES")
            .ok()
            .map(|value| {
                matches!(
                    value.trim().to_ascii_lowercase().as_str(),
                    "1" | "true" | "yes" | "on"
                )
            })
            .unwrap_or(false)
}

async fn body(req: Request<Incoming>) -> Result<Value, Resp> {
    protocol::json_body(req)
        .await
        .map_err(|problem| match problem {
            BodyError::TooLarge { length, limit } => error(
                StatusCode::PAYLOAD_TOO_LARGE,
                &format!("payload too large ({length} bytes > {limit} limit)"),
                Some("invalid_request_error"),
            ),
            BodyError::Invalid => error(
                StatusCode::BAD_REQUEST,
                "invalid JSON body",
                Some("invalid_request_error"),
            ),
        })
}

async fn upstream_error(resp: Resp) -> Resp {
    let status = resp.status();
    let bytes = resp
        .into_body()
        .collect()
        .await
        .map(|body| body.to_bytes())
        .unwrap_or_default();
    let parsed: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    let message = parsed
        .pointer("/error/message")
        .and_then(Value::as_str)
        .unwrap_or("upstream error");
    if message.to_ascii_lowercase().contains("no usable account") {
        return error(
            StatusCode::SERVICE_UNAVAILABLE,
            &format!("{message} - add or enable one at the dashboard (/)"),
            None,
        );
    }
    error(status, message, None)
}

async fn messages(
    app: &Arc<App>,
    identity: Identity,
    realm: String,
    chat_body: Value,
    session: Option<String>,
    pin: Option<crate::keyref::PinnedAccount>,
) -> Resp {
    let started = std::time::Instant::now();
    let stream = chat_body
        .get("stream")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let model = chat_body
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    app.inflight.fetch_add(1, Ordering::Relaxed);
    let opened =
        crate::upstream::open_upstream(app, &chat_body, &realm, session.as_deref(), pin.as_ref())
            .await;
    let result = match opened {
        Err(resp) => upstream_error(resp).await,
        Ok((candidate, ctx, upstream)) if stream => crate::http::anthropic_stream::response(
            app.clone(),
            candidate,
            ctx,
            upstream,
            started,
            identity.key_id,
        ),
        Ok((candidate, ctx, upstream)) => {
            nonstream(
                app,
                candidate,
                ctx,
                upstream,
                started,
                &identity.key_id,
                &model,
            )
            .await
        }
    };
    app.inflight.fetch_sub(1, Ordering::Relaxed);
    result
}

async fn read_upstream(mut upstream: reqwest::Response) -> Result<(String, Option<f64>), Resp> {
    let mut raw = Vec::new();
    let mut first_at = None;
    loop {
        match upstream.chunk().await {
            Ok(Some(chunk)) => {
                first_at.get_or_insert_with(crate::sse::epoch_now);
                raw.extend_from_slice(&chunk);
            }
            Ok(None) => break,
            Err(failure) => {
                return Err(error(
                    StatusCode::BAD_GATEWAY,
                    &format!("upstream stream error: {failure}"),
                    None,
                ))
            }
        }
    }
    Ok((String::from_utf8_lossy(&raw).into_owned(), first_at))
}

fn record_completed(
    app: &App,
    candidate: &crate::pool::Candidate,
    ctx: &crate::upstream::Ctx,
    key_id: &str,
    elapsed: i64,
    first_ms: Option<i64>,
    usage: &Value,
) {
    crate::record::record(
        app,
        crate::record::Row {
            model: &ctx.model,
            stream: false,
            outcome: "completed",
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

async fn nonstream(
    app: &App,
    candidate: crate::pool::Candidate,
    ctx: crate::upstream::Ctx,
    upstream: reqwest::Response,
    started: std::time::Instant,
    key_id: &str,
    model: &str,
) -> Resp {
    let (text, first_at) = match read_upstream(upstream).await {
        Ok(value) => value,
        Err(resp) => return resp,
    };
    let Some(mut aggregate) = crate::sse::aggregate(&text) else {
        return error(StatusCode::BAD_GATEWAY, "upstream stream error", None);
    };
    aggregate.first_chunk_at = first_at.or(aggregate.first_chunk_at);
    let elapsed = started.elapsed().as_millis() as i64;
    let started_at = crate::sse::epoch_now() - started.elapsed().as_secs_f64();
    let first_ms = aggregate
        .first_chunk_at
        .map(|at| ((at - started_at) * 1000.0) as i64);
    let usage = aggregate.client_usage();
    let response_model = if aggregate.model.is_empty() {
        model.to_string()
    } else {
        aggregate.model.clone()
    };
    let chat = aggregate.into_payload(&json!(response_model), elapsed, usage.clone());
    record_completed(app, &candidate, &ctx, key_id, elapsed, first_ms, &usage);
    json_resp(
        StatusCode::OK,
        &crate::http::anthropic_translate::chat_to_messages(&chat, &response_model),
    )
}

pub async fn handle(app: &Arc<App>, req: Request<Incoming>) -> Resp {
    let pin = crate::keyref::pinned(&req);
    let identity = match protocol::authorize(app, &req) {
        Ok(identity) => identity,
        Err(()) => {
            return error(
                StatusCode::UNAUTHORIZED,
                "missing or invalid API key",
                Some("authentication_error"),
            )
        }
    };
    if !enabled(app) {
        return error(
            StatusCode::BAD_REQUEST,
            DISABLED,
            Some("invalid_request_error"),
        );
    }
    let is_count = req.uri().path().ends_with("/count_tokens");
    let realm = protocol::requested_realm(app, &req, &identity);
    let session = crate::http::session_key_header(req.headers());
    let payload = match body(req).await {
        Ok(payload) => payload,
        Err(resp) => return resp,
    };
    let chat = match crate::http::anthropic_translate::to_chat(&payload, &realm) {
        Ok(chat) => chat,
        Err(message) => {
            return error(
                StatusCode::BAD_REQUEST,
                &message,
                Some("invalid_request_error"),
            )
        }
    };
    if is_count {
        return json_resp(
            StatusCode::OK,
            &json!({"input_tokens": crate::http::anthropic_translate::count_chat_tokens(&chat)}),
        );
    }
    messages(app, identity, realm, chat, session, pin).await
}
