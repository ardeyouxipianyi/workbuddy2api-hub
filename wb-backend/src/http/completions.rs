//! `POST /v1/completions`.
//!
//! Python currently accepts the legacy prompt shape but returns the chat
//! completion envelope from the shared chat pipeline. The prompt is still
//! normalized to one user message so it reaches the upstream correctly.

use std::sync::Arc;

use hyper::body::Incoming;
use hyper::{Request, StatusCode};
use serde_json::{json, Map, Value};

use crate::http::protocol::{self, Identity};
use crate::http::{json_resp, Resp};
use crate::App;

fn prompt_text(prompt: &Value) -> Result<String, &'static str> {
    match prompt {
        Value::String(text) => Ok(text.clone()),
        Value::Array(items) if items.iter().all(Value::is_string) => Ok(items
            .iter()
            .filter_map(Value::as_str)
            .collect::<Vec<_>>()
            .join("\n")),
        _ => Err("prompt must be a string or an array of strings"),
    }
}

pub fn to_chat(body: &Value) -> Result<Value, &'static str> {
    let model = body
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if model.is_empty() {
        return Err("model is required");
    }
    let prompt = prompt_text(body.get("prompt").ok_or("prompt is required")?)?;
    let mut chat = Map::new();
    chat.insert("model".into(), json!(model));
    chat.insert(
        "messages".into(),
        json!([{"role": "user", "content": prompt}]),
    );
    for key in [
        "max_tokens",
        "temperature",
        "top_p",
        "n",
        "stream",
        "stop",
        "presence_penalty",
        "frequency_penalty",
        "user",
    ] {
        if let Some(value) = body.get(key) {
            chat.insert(key.into(), value.clone());
        }
    }
    Ok(Value::Object(chat))
}

pub async fn handle(app: &Arc<App>, req: Request<Incoming>, identity: Identity) -> Resp {
    let started = std::time::Instant::now();
    let realm = protocol::requested_realm(app, &req, &identity);
    let session = crate::http::session_key_header(req.headers());
    let body = match protocol::json_body(req).await {
        Ok(body) => body,
        Err(protocol::BodyError::TooLarge { length, limit }) => {
            return json_resp(
                StatusCode::PAYLOAD_TOO_LARGE,
                &json!({"error": {
                    "message": format!("payload too large ({length} bytes > {limit} limit)"),
                    "type": "invalid_request_error",
                    "code": 413,
                }}),
            )
        }
        Err(protocol::BodyError::Invalid) => {
            return crate::http::error_resp(StatusCode::BAD_REQUEST, "invalid JSON body")
        }
    };
    let chat_body = match to_chat(&body) {
        Ok(body) => body,
        Err(message) => return crate::http::error_resp(StatusCode::BAD_REQUEST, message),
    };
    app.inflight
        .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let result = if body.get("stream").and_then(Value::as_bool).unwrap_or(false) {
        crate::upstream::chat_stream(
            app,
            &chat_body,
            &realm,
            started,
            &identity.key_id,
            session.as_deref(),
        )
        .await
    } else {
        crate::upstream::chat_once(
            app,
            &chat_body,
            &realm,
            started,
            &identity.key_id,
            session.as_deref(),
        )
        .await
    };
    app.inflight
        .fetch_sub(1, std::sync::atomic::Ordering::Relaxed);
    result
}
