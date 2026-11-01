//! `POST /v1/responses`：Codex 走 `wire_api = "responses"`，所以这一层必须有。
//!
//! 形状是拿真进程（Python 版 + 真上游）抓出来的：非流式一个 `object:"response"`
//! 信封，流式是 `event:`/`data:` 成对的事件序列。**推理内容（reasoning item）不实现**：
//! 上游给 reasoning_content 时我们只把它当正文之外的东西丢掉，Codex 不接受
//! reasoning 项也能正常工作；缺它只是少了「思考过程」展示。
//!
//! shortcut: 只支持文本输入与 function 工具，多模态/内置工具（web_search 等）返回 400。

use std::sync::Arc;

use http_body_util::{BodyExt, StreamBody};
use hyper::body::{Bytes, Frame};
use hyper::StatusCode;
use serde_json::{json, Map, Value};

use crate::upstream;
use crate::App;

/// 生成 `resp_`/`msg_` 这类 id 的尾段。
fn tail(len: usize) -> String {
    crate::sse::hex_token(len)
}

/// Responses 请求 → Chat Completions 请求（`responses_to_chat`，wb_proxy.py:16756）。
pub fn to_chat(body: &Value) -> Result<Value, String> {
    let model = body.get("model").and_then(Value::as_str).unwrap_or("");
    let mut messages: Vec<Value> = Vec::new();
    if let Some(instructions) = body.get("instructions").and_then(Value::as_str) {
        if !instructions.trim().is_empty() {
            messages.push(json!({"role": "system", "content": instructions}));
        }
    }
    match body.get("input") {
        Some(Value::String(text)) => messages.push(json!({"role": "user", "content": text})),
        Some(Value::Array(items)) => {
            for item in items {
                // 已经是 chat 形状的直接搬；responses 的 message 项要拆 content 数组。
                let item_type = item.get("type").and_then(Value::as_str).unwrap_or("");
                if matches!(
                    item_type,
                    "compaction" | "compaction_summary" | "context_compaction"
                ) {
                    let summary = crate::responses_compact::decode_summary(
                        item.get("encrypted_content").and_then(Value::as_str).unwrap_or(""),
                    );
                    if !summary.trim().is_empty() {
                        messages.push(json!({
                            "role": "system",
                            "content": format!(
                                "Summary of the earlier conversation (context checkpoint):\n\n{}",
                                summary.trim()
                            ),
                        }));
                    }
                } else if let Some(role) = item.get("role").and_then(Value::as_str) {
                    messages.push(json!({"role": role, "content": text_of(item)}));
                } else if item.get("type").and_then(Value::as_str) == Some("function_call_output") {
                    messages.push(json!({
                        "role": "tool",
                        "tool_call_id": item.get("call_id").cloned().unwrap_or(Value::Null),
                        "content": item.get("output").cloned().unwrap_or(Value::Null),
                    }));
                }
            }
        }
        _ => return Err("input is required".into()),
    }
    let mut chat = Map::new();
    chat.insert("model".into(), json!(model));
    chat.insert("messages".into(), Value::Array(messages));
    if let Some(max) = body.get("max_output_tokens") {
        chat.insert("max_tokens".into(), max.clone());
    }
    for key in ["temperature", "top_p", "parallel_tool_calls", "reasoning_effort"] {
        if let Some(value) = body.get(key) {
            chat.insert(key.into(), value.clone());
        }
    }
    if let Some(tools) = body.get("tools").and_then(Value::as_array) {
        let mut out = Vec::new();
        for tool in tools {
            match tool.get("type").and_then(Value::as_str) {
                // responses 把函数展开在顶层，chat 要包在 "function" 里。
                Some("function") => out.push(json!({
                    "type": "function",
                    "function": {
                        "name": tool.get("name").cloned().unwrap_or(Value::Null),
                        "description": tool.get("description").cloned().unwrap_or(json!("")),
                        "parameters": tool.get("parameters").cloned().unwrap_or(json!({"type": "object"})),
                    }
                })),
                // Codex 0.164 会发 `namespace`/`custom`/`web_search` 这些非函数工具；
                // 上游 chat 协议里没有对应物，直接丢（Python 版同样不转发）。
                Some(_) => {}
                None => {}
            }
        }
        if !out.is_empty() {
            chat.insert("tools".into(), Value::Array(out));
        }
    }
    Ok(Value::Object(chat))
}

fn text_of(item: &Value) -> String {
    match item.get("content") {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Array(parts)) => parts
            .iter()
            .filter_map(|p| {
                p.get("text")
                    .and_then(Value::as_str)
                    .map(|s| s.to_string())
                    .or_else(|| p.as_str().map(|s| s.to_string()))
            })
            .collect::<Vec<_>>()
            .join(""),
        _ => String::new(),
    }
}

/// chat 的 usage → responses 的 usage（字段名不一样，别混用）。
pub(super) fn usage_of(chat_usage: &Value) -> Value {
    let input = chat_usage.get("prompt_tokens").and_then(Value::as_i64).unwrap_or(0);
    let output = chat_usage.get("completion_tokens").and_then(Value::as_i64).unwrap_or(0);
    let cached = crate::record::best_cached_tokens(chat_usage);
    let reasoning = chat_usage
        .get("completion_tokens_details")
        .and_then(|d| d.get("reasoning_tokens"))
        .and_then(Value::as_i64)
        .unwrap_or(0);
    json!({
        "input_tokens": input,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens": output,
        "output_tokens_details": {"reasoning_tokens": reasoning},
        "total_tokens": input + output,
    })
}

pub(super) fn envelope(
    id: &str,
    created: i64,
    status: &str,
    model: &str,
    output: Value,
    text: &str,
    usage: Option<Value>,
) -> Value {
    let mut out = Map::new();
    out.insert("id".into(), json!(id));
    out.insert("object".into(), json!("response"));
    out.insert("created_at".into(), json!(created));
    out.insert("status".into(), json!(status));
    out.insert("model".into(), json!(model));
    out.insert("output".into(), output);
    out.insert("output_text".into(), json!(text));
    out.insert("parallel_tool_calls".into(), json!(true));
    out.insert("tool_choice".into(), json!("auto"));
    out.insert("tools".into(), json!([]));
    out.insert("metadata".into(), json!({}));
    if let Some(usage) = usage {
        out.insert("usage".into(), usage);
    }
    Value::Object(out)
}

pub(super) fn message_item(id: &str, status: &str, text: &str) -> Value {
    json!({
        "id": id,
        "type": "message",
        "status": status,
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    })
}

pub async fn handle(
    app: &Arc<App>,
    req: hyper::Request<hyper::body::Incoming>,
    key_id: String,
) -> crate::http::Resp {
    let started = std::time::Instant::now();
    let path = req.uri().path().to_string();
    let pin = crate::keyref::pinned(&req);
    let realm = crate::http::requested_realm(&req, &app.state.current_realm());
    let session = crate::http::session_key_header(req.headers());
    let body = match crate::http::body_json(req).await {
        Ok(v) => v,
        Err(resp) => return resp,
    };
    let compact = crate::responses_compact::requested(&path, &body);
    let mut chat_body = match to_chat(&body) {
        Ok(v) => v,
        Err(message) => return crate::http::error_resp(StatusCode::BAD_REQUEST, &message),
    };
    if compact {
        crate::responses_compact::adjust_chat(&mut chat_body, &body);
    }
    let session = session.or_else(|| crate::http::session_key_body(&body));
    let session = session.as_deref();
    let stream = body.get("stream").and_then(Value::as_bool).unwrap_or(false);
    app.note(&format!(
        "responses: model={} stream={} msgs={} key={}",
        chat_body.get("model").and_then(Value::as_str).unwrap_or("-"),
        stream,
        chat_body.get("messages").and_then(Value::as_array).map_or(0, Vec::len),
        key_id.get(..8).unwrap_or(&key_id),
    ));
    app.inflight.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let result = if compact {
        // 压缩入口不认固定账号（Python 的 compact 分支也不看 X-Key-Ref）。
        crate::responses_compact::run(app, &chat_body, &realm, &key_id, session, started, stream)
            .await
    } else if stream {
        stream_response(app, &chat_body, &realm, started, &key_id, session, pin.as_ref()).await
    } else {
        once(app, &chat_body, &realm, started, &key_id, session, pin.as_ref()).await
    };
    app.inflight.fetch_sub(1, std::sync::atomic::Ordering::Relaxed);
    result
}

async fn once(
    app: &Arc<App>,
    chat_body: &Value,
    realm: &str,
    started: std::time::Instant,
    key_id: &str,
    session_key: Option<&str>,
    pin: Option<&crate::keyref::PinnedAccount>,
) -> crate::http::Resp {
    let (cand, ctx, resp) = match upstream::open_upstream(app, chat_body, realm, session_key, pin).await {
        Ok(v) => v,
        Err(resp) => return resp,
    };
    let mut text = String::new();
    let mut resp = resp;
    let mut first_chunk_at: Option<f64> = None;
    loop {
        match resp.chunk().await {
            Ok(Some(chunk)) => {
                if first_chunk_at.is_none() {
                    first_chunk_at = Some(crate::sse::epoch_now());
                }
                text.push_str(&String::from_utf8_lossy(&chunk));
            }
            Ok(None) => break,
            Err(e) => {
                return crate::http::error_resp(
                    StatusCode::BAD_GATEWAY,
                    &format!("upstream stream error: {e}"),
                )
            }
        }
    }
    let mut agg = match crate::sse::aggregate(&text) {
        Some(agg) => agg,
        None => {
            return crate::http::error_resp(StatusCode::BAD_GATEWAY, "upstream stream error")
        }
    };
    agg.first_chunk_at = first_chunk_at.or(agg.first_chunk_at);
    let elapsed = started.elapsed().as_millis() as i64;
    let started_at = crate::sse::epoch_now() - started.elapsed().as_secs_f64();
    let first_ms = agg.first_chunk_at.map(|at| ((at - started_at) * 1000.0) as i64);
    let usage = agg.client_usage();
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
            usage: if usage.is_null() { None } else { Some(&usage) },
            fp: Some(&ctx.fp),
            account: Some(&cand.uid),
            realm: &cand.realm,
            key: key_id,
            effort: ctx.effort.as_deref(),
        },
    );
    let id = format!("resp_{}", tail(32));
    let item_id = format!("msg_{}", tail(32));
    let text = agg.content.clone();
    let payload = envelope(
        &id,
        crate::state::now_epoch(),
        "completed",
        &ctx.model,
        json!([message_item(&item_id, "completed", &text)]),
        &text,
        if usage.is_null() { None } else { Some(usage_of(&usage)) },
    );
    crate::http::json_resp(StatusCode::OK, &payload)
}

async fn stream_response(
    app: &Arc<App>,
    chat_body: &Value,
    realm: &str,
    started: std::time::Instant,
    key_id: &str,
    session_key: Option<&str>,
    pin: Option<&crate::keyref::PinnedAccount>,
) -> crate::http::Resp {
    let (cand, ctx, resp) = match upstream::open_upstream(app, chat_body, realm, session_key, pin).await {
        Ok(v) => v,
        Err(resp) => return resp,
    };
    let id = format!("resp_{}", tail(32));
    let item_id = format!("msg_{}", tail(32));
    let created = crate::state::now_epoch();
    let model = ctx.model.clone();
    let app = app.clone();
    let key_id = key_id.to_string();
    let (tx, rx) = tokio::sync::mpsc::channel::<Result<Frame<Bytes>, std::convert::Infallible>>(32);

    tokio::spawn(async move {
        let mut seq: i64 = 0;
        macro_rules! emit {
            ($event:expr, $payload:expr) => {{
                seq += 1;
                let mut payload = $payload;
                if let Some(map) = payload.as_object_mut() {
                    map.insert("sequence_number".into(), json!(seq));
                }
                let text = format!("event: {}\ndata: {}\n\n", $event, payload);
                if tx.send(Ok(Frame::data(Bytes::from(text)))).await.is_err() {
                    return;
                }
            }};
        }

        emit!(
            "response.created",
            json!({"type": "response.created", "response": envelope(&id, created, "in_progress", &model, json!([]), "", None)})
        );
        emit!(
            "response.in_progress",
            json!({"type": "response.in_progress", "response": envelope(&id, created, "in_progress", &model, json!([]), "", None)})
        );
        seq += 1;
        let added_seq = seq;
        let text_added = format!(
            "event: response.output_item.added\ndata: {}\n\n",
            json!({"type": "response.output_item.added", "sequence_number": added_seq, "output_index": 0,
                   "item": message_item(&item_id, "in_progress", "")})
        );
        if tx.send(Ok(Frame::data(Bytes::from(text_added)))).await.is_err() {
            return;
        }
        seq += 1;
        let part_seq = seq;
        let text_part = format!(
            "event: response.content_part.added\ndata: {}\n\n",
            json!({"type": "response.content_part.added", "sequence_number": part_seq, "item_id": item_id,
                   "output_index": 0, "content_index": 0,
                   "part": {"type": "output_text", "text": "", "annotations": []}})
        );
        if tx.send(Ok(Frame::data(Bytes::from(text_part)))).await.is_err() {
            return;
        }

        let mut resp = resp;
        let mut buffer = String::new();
        let mut agg = crate::sse::Aggregated {
            id: id.clone(),
            model: model.clone(),
            created,
            content: String::new(),
            reasoning: String::new(),
            tool_calls: Vec::new(),
            usage: None,
            finish: None,
            first_chunk_at: None,
        };
        let mut first_chunk_at: Option<f64> = None;
        let mut broken = false;
        loop {
            let chunk = match resp.chunk().await {
                Ok(Some(chunk)) => chunk,
                Ok(None) => break,
                Err(_) => {
                    broken = true;
                    break;
                }
            };
            if first_chunk_at.is_none() {
                first_chunk_at = Some(crate::sse::epoch_now());
            }
            buffer.push_str(&String::from_utf8_lossy(&chunk));
            while let Some(idx) = buffer.find('\n') {
                let line: String = buffer.drain(..=idx).collect();
                let payload = match crate::stream::parse_frame(&line) {
                    Some(p) => p.to_string(),
                    None => continue,
                };
                let frame: Value = match serde_json::from_str(&payload) {
                    Ok(v) => v,
                    Err(_) => continue,
                };
                crate::sse::absorb(&mut agg, &frame);
                let delta = frame
                    .get("choices")
                    .and_then(Value::as_array)
                    .and_then(|c| c.first())
                    .and_then(|c| c.get("delta"));
                let piece = delta
                    .and_then(|d| d.get("content"))
                    .and_then(Value::as_str)
                    .unwrap_or("");
                if !piece.is_empty() {
                    seq += 1;
                    let payload = json!({
                        "type": "response.output_text.delta",
                        "sequence_number": seq,
                        "item_id": item_id,
                        "output_index": 0,
                        "content_index": 0,
                        "delta": piece,
                    });
                    let text = format!("event: response.output_text.delta\ndata: {payload}\n\n");
                    if tx.send(Ok(Frame::data(Bytes::from(text)))).await.is_err() {
                        return;
                    }
                }
            }
            if broken {
                break;
            }
        }
        let text = agg.content.clone();
        for (event, payload) in [
            ("response.output_text.done", json!({"type": "response.output_text.done", "item_id": item_id, "output_index": 0, "content_index": 0, "text": text})),
            ("response.content_part.done", json!({"type": "response.content_part.done", "item_id": item_id, "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": text, "annotations": []}})),
            ("response.output_item.done", json!({"type": "response.output_item.done", "output_index": 0, "item": message_item(&item_id, "completed", &text)})),
        ] {
            seq += 1;
            let mut payload = payload;
            if let Some(map) = payload.as_object_mut() {
                map.insert("sequence_number".into(), json!(seq));
            }
            let chunk = format!("event: {event}\ndata: {payload}\n\n");
            if tx.send(Ok(Frame::data(Bytes::from(chunk)))).await.is_err() {
                return;
            }
        }
        let usage = agg.client_usage();
        seq += 1;
        let final_payload = {
            let mut body = envelope(
                &id,
                created,
                "completed",
                &model,
                json!([message_item(&item_id, "completed", &text)]),
                &text,
                if usage.is_null() { None } else { Some(usage_of(&usage)) },
            );
            body.as_object_mut()
                .map(|m| m.insert("type".into(), json!("response.completed")));
            let mut wrapper = json!({"type": "response.completed", "sequence_number": seq, "response": body});
            wrapper.as_object_mut().unwrap();
            wrapper
        };
        let chunk = format!("event: response.completed\ndata: {final_payload}\n\n");
        let _ = tx.send(Ok(Frame::data(Bytes::from(chunk)))).await;

        let elapsed = started.elapsed().as_millis() as i64;
        let started_at = crate::sse::epoch_now() - started.elapsed().as_secs_f64();
        let first_ms = first_chunk_at.map(|at| ((at - started_at) * 1000.0) as i64);
        crate::record::record(
            app.as_ref(),
            crate::record::Row {
                model: &ctx.model,
                stream: true,
                outcome: if broken { "failed" } else { "completed" },
                elapsed_ms: Some(elapsed),
                ttft_ms: first_ms,
                gen_ms: first_ms.map(|first| elapsed - first),
                requested_model: Some(&ctx.requested_model),
                usage: if usage.is_null() { None } else { Some(&usage) },
                fp: Some(&ctx.fp),
                account: Some(&cand.uid),
                realm: &cand.realm,
                key: &key_id,
                effort: ctx.effort.as_deref(),
            },
        );
    });

    let body = StreamBody::new(tokio_stream::wrappers::ReceiverStream::new(rx));
    let mut response = hyper::Response::new(BodyExt::boxed(body));
    *response.status_mut() = StatusCode::OK;
    let headers = response.headers_mut();
    headers.insert(
        hyper::header::CONTENT_TYPE,
        hyper::header::HeaderValue::from_static("text/event-stream; charset=utf-8"),
    );
    headers.insert(hyper::header::CACHE_CONTROL, hyper::header::HeaderValue::from_static("no-cache"));
    headers.insert(hyper::header::CONNECTION, hyper::header::HeaderValue::from_static("close"));
    response
}
