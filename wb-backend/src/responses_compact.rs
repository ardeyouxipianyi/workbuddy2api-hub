//! Codex's legacy Responses compaction envelope.

use std::sync::Arc;

use http_body_util::BodyExt;
use hyper::body::Bytes;
use hyper::{header, StatusCode};
use serde_json::{json, Value};

use crate::upstream;
use crate::App;

pub(crate) const COMPACTION_PROMPT: &str = "You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task.\n\nInclude:\n- Current progress and key decisions made\n- Important context, constraints, or user preferences\n- What remains to be done (clear next steps)\n- Any critical data, examples, or references needed to continue\n\nBe concise, structured, and focused on helping the next LLM seamlessly continue the work. Return only the summary.";

fn default_model() -> String {
    std::env::var("WB_COMPACTION_MODEL")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| "deepseek-v4.1-flash".to_string())
}

fn has_trigger(body: &Value) -> bool {
    match body.get("input") {
        Some(Value::String(value)) => value.trim() == "compaction_trigger",
        Some(Value::Array(items)) => items
            .iter()
            .any(|item| item.get("type").and_then(Value::as_str) == Some("compaction_trigger")),
        _ => false,
    }
}

pub(crate) fn requested(path: &str, body: &Value) -> bool {
    path.trim_end_matches('/').ends_with("/responses/compact") || has_trigger(body)
}

fn looks_like_prompt(chat: &Value) -> bool {
    chat.get("messages")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .rev()
        .find(|message| message.get("role").and_then(Value::as_str) == Some("user"))
        .and_then(|message| message.get("content"))
        .is_some_and(|content| {
            content
                .to_string()
                .to_ascii_uppercase()
                .contains("CONTEXT CHECKPOINT COMPACTION")
        })
}

pub(crate) fn adjust_chat(chat: &mut Value, original: &Value) {
    let needs_prompt = !looks_like_prompt(chat);
    let Some(object) = chat.as_object_mut() else {
        return;
    };
    if object
        .get("model")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .is_none()
    {
        object.insert("model".into(), json!(default_model()));
    }
    object.remove("tools");
    object.remove("tool_choice");
    object.remove("parallel_tool_calls");
    if let Some(Value::String(value)) = original.get("input") {
        if value.trim() == "compaction_trigger" {
            if let Some(messages) = object.get_mut("messages").and_then(Value::as_array_mut) {
                messages.retain(|message| {
                    message.get("content").and_then(Value::as_str) != Some("compaction_trigger")
                });
            }
        }
    }
    if needs_prompt {
        object
            .entry("messages")
            .or_insert_with(|| json!([]))
            .as_array_mut()
            .unwrap()
            .push(json!({"role": "user", "content": COMPACTION_PROMPT}));
    }
}

fn base64_alphabet(index: usize) -> u8 {
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"[index]
}

pub(crate) fn encode_summary(summary: &str) -> String {
    let raw = serde_json::to_vec(&json!({"v": 1, "summary": summary})).unwrap_or_default();
    let mut out = String::with_capacity((raw.len() + 2) / 3 * 4);
    for chunk in raw.chunks(3) {
        let value = (chunk[0] as u32) << 16
            | (chunk.get(1).copied().unwrap_or(0) as u32) << 8
            | chunk.get(2).copied().unwrap_or(0) as u32;
        out.push(base64_alphabet(((value >> 18) & 63) as usize) as char);
        out.push(base64_alphabet(((value >> 12) & 63) as usize) as char);
        out.push(if chunk.len() > 1 {
            base64_alphabet(((value >> 6) & 63) as usize) as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            base64_alphabet((value & 63) as usize) as char
        } else {
            '='
        });
    }
    out
}

pub(crate) fn decode_summary(blob: &str) -> String {
    let text = blob.trim();
    if text.is_empty() {
        return String::new();
    }
    let decode = |byte: u8| match byte {
        b'A'..=b'Z' => Some(byte - b'A'),
        b'a'..=b'z' => Some(byte - b'a' + 26),
        b'0'..=b'9' => Some(byte - b'0' + 52),
        b'+' => Some(62),
        b'/' => Some(63),
        _ => None,
    };
    let mut bytes = Vec::new();
    for chunk in text.as_bytes().chunks(4) {
        let values: Vec<u8> = chunk.iter().filter_map(|byte| decode(*byte)).collect();
        if values.len() < 2 {
            return text.to_string();
        }
        bytes.push((values[0] << 2) | (values[1] >> 4));
        if values.len() > 2 {
            bytes.push((values[1] << 4) | (values[2] >> 2));
        }
        if values.len() > 3 {
            bytes.push((values[2] << 6) | values[3]);
        }
    }
    serde_json::from_slice::<Value>(&bytes)
        .ok()
        .and_then(|value| {
            value
                .get("summary")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .unwrap_or_else(|| text.to_string())
}

fn compaction_item(summary: &str) -> Value {
    json!({
        "id": format!("cmp_{}", crate::sse::hex_token(32)),
        "type": "compaction",
        "encrypted_content": encode_summary(summary),
    })
}

async fn collect_summary(
    app: &Arc<App>,
    chat: &Value,
    realm: &str,
    key_id: &str,
    session: Option<&str>,
    started: std::time::Instant,
    stream: bool,
) -> Result<(String, Value), crate::http::Resp> {
    let (candidate, context, response) = upstream::open_upstream(app, chat, realm, session).await?;
    let mut response = response;
    let mut text = String::new();
    while let Some(chunk) = response.chunk().await.map_err(|error| {
        crate::http::error_resp(
            StatusCode::BAD_GATEWAY,
            &format!("upstream stream error: {error}"),
        )
    })? {
        text.push_str(&String::from_utf8_lossy(&chunk));
    }
    let aggregate = crate::sse::aggregate(&text)
        .ok_or_else(|| crate::http::error_resp(StatusCode::BAD_GATEWAY, "upstream stream error"))?;
    let summary = aggregate.content.trim().to_string();
    if summary.is_empty() {
        return Err(crate::http::error_resp(
            StatusCode::BAD_GATEWAY,
            "compaction produced no summary (the model returned no text); retry",
        ));
    }
    let usage = aggregate.client_usage();
    crate::record::record(
        app,
        crate::record::Row {
            model: &context.model,
            stream,
            outcome: "completed",
            elapsed_ms: Some(started.elapsed().as_millis() as i64),
            ttft_ms: None,
            gen_ms: None,
            requested_model: Some(&context.requested_model),
            usage: if usage.is_null() { None } else { Some(&usage) },
            fp: Some(&context.fp),
            account: Some(&candidate.uid),
            realm: &candidate.realm,
            key: key_id,
            effort: context.effort.as_deref(),
        },
    );
    Ok((summary, usage))
}

fn response_usage(chat: &Value) -> Value {
    crate::responses::usage_of(chat)
}

pub(crate) fn nonstream_body(
    id: &str,
    created: i64,
    model: &str,
    item: Value,
    usage: Value,
) -> Value {
    json!({
        "id": id,
        "object": "response",
        "created_at": created,
        "status": "completed",
        "model": model,
        "output": [item],
        "output_text": "",
        "metadata": {},
        "usage": usage,
    })
}

pub(crate) async fn run(
    app: &Arc<App>,
    chat: &Value,
    realm: &str,
    key_id: &str,
    session: Option<&str>,
    started: std::time::Instant,
    stream: bool,
) -> crate::http::Resp {
    let configured_model;
    let model = match chat.get("model").and_then(Value::as_str) {
        Some(value) if !value.is_empty() => value,
        _ => {
            configured_model = default_model();
            configured_model.as_str()
        }
    };
    let (summary, raw_usage) =
        match collect_summary(app, chat, realm, key_id, session, started, stream).await {
            Ok(value) => value,
            Err(response) => return response,
        };
    let item = compaction_item(&summary);
    let id = format!("resp_{}", crate::sse::hex_token(32));
    let created = crate::state::now_epoch();
    let usage = response_usage(&raw_usage);
    if !stream {
        return crate::http::json_resp(
            StatusCode::OK,
            &nonstream_body(&id, created, model, item, usage),
        );
    }
    let base = json!({
        "id": id,
        "object": "response",
        "created_at": created,
        "model": model,
        "output": [item],
        "output_text": "",
        "metadata": {},
    });
    let mut created_response = base.clone();
    created_response["status"] = json!("in_progress");
    created_response["output"] = json!([]);
    let mut completed = base;
    completed["status"] = json!("completed");
    completed["usage"] = usage;
    let frames = [
        (
            "response.created",
            json!({"type":"response.created","sequence_number":1,"response":created_response}),
        ),
        (
            "response.in_progress",
            json!({"type":"response.in_progress","sequence_number":2,"response":created_response}),
        ),
        (
            "response.output_item.done",
            json!({"type":"response.output_item.done","sequence_number":3,"output_index":0,"item":item}),
        ),
        (
            "response.completed",
            json!({"type":"response.completed","sequence_number":4,"response":completed}),
        ),
    ];
    let body = frames
        .into_iter()
        .map(|(event, data)| format!("event: {event}\ndata: {data}\n\n"))
        .collect::<String>();
    let mut response =
        hyper::Response::new(BodyExt::boxed(http_body_util::Full::new(Bytes::from(body))));
    *response.status_mut() = StatusCode::OK;
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        header::HeaderValue::from_static("text/event-stream; charset=utf-8"),
    );
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        header::HeaderValue::from_static("no-cache"),
    );
    response.headers_mut().insert(
        header::CONNECTION,
        header::HeaderValue::from_static("close"),
    );
    response.headers_mut().insert(
        header::ACCESS_CONTROL_ALLOW_ORIGIN,
        header::HeaderValue::from_static("*"),
    );
    response
}
