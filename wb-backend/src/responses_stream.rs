//! SSE response stream for the OpenAI Responses API.

use std::sync::Arc;

use http_body_util::{BodyExt, StreamBody};
use hyper::body::{Bytes, Frame};
use hyper::StatusCode;
use serde_json::{json, Value};

use crate::responses::{envelope, message_item, usage_of};
use crate::upstream;
use crate::App;

pub(crate) async fn stream_response(
    app: &Arc<App>,
    chat_body: &Value,
    realm: &str,
    started: std::time::Instant,
    key_id: &str,
    session_key: Option<&str>,
) -> crate::http::Resp {
    let (cand, ctx, resp) = match upstream::open_upstream(app, chat_body, realm, session_key).await
    {
        Ok(v) => v,
        Err(resp) => return resp,
    };
    let id = format!("resp_{}", crate::sse::hex_token(32));
    let item_id = format!("msg_{}", crate::sse::hex_token(32));
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
        emit!(
            "response.output_item.added",
            json!({"type": "response.output_item.added", "output_index": 0,
                   "item": message_item(&item_id, "in_progress", "")})
        );
        emit!(
            "response.content_part.added",
            json!({"type": "response.content_part.added", "item_id": item_id,
                   "output_index": 0, "content_index": 0,
                   "part": {"type": "output_text", "text": "", "annotations": []}})
        );

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
                let Some(payload) = crate::stream::parse_frame(&line) else {
                    continue;
                };
                let Ok(frame) = serde_json::from_str::<Value>(payload) else {
                    continue;
                };
                crate::sse::absorb(&mut agg, &frame);
                let piece = frame
                    .get("choices")
                    .and_then(Value::as_array)
                    .and_then(|choices| choices.first())
                    .and_then(|choice| choice.get("delta"))
                    .and_then(|delta| delta.get("content"))
                    .and_then(Value::as_str)
                    .unwrap_or("");
                if !piece.is_empty() {
                    emit!(
                        "response.output_text.delta",
                        json!({
                            "type": "response.output_text.delta",
                            "item_id": item_id,
                            "output_index": 0,
                            "content_index": 0,
                            "delta": piece,
                        })
                    );
                }
            }
            if broken {
                break;
            }
        }
        let text = agg.content.clone();
        for (event, payload) in [
            (
                "response.output_text.done",
                json!({"type": "response.output_text.done", "item_id": item_id, "output_index": 0, "content_index": 0, "text": text}),
            ),
            (
                "response.content_part.done",
                json!({"type": "response.content_part.done", "item_id": item_id, "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": text, "annotations": []}}),
            ),
            (
                "response.output_item.done",
                json!({"type": "response.output_item.done", "output_index": 0, "item": message_item(&item_id, "completed", &text)}),
            ),
        ] {
            emit!(event, payload);
        }
        let usage = agg.client_usage();
        let final_payload = {
            let mut body = envelope(
                &id,
                created,
                "completed",
                &model,
                json!([message_item(&item_id, "completed", &text)]),
                &text,
                if usage.is_null() {
                    None
                } else {
                    Some(usage_of(&usage))
                },
            );
            body.as_object_mut()
                .map(|map| map.insert("type".into(), json!("response.completed")));
            json!({"type": "response.completed", "response": body})
        };
        emit!("response.completed", final_payload);

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
    headers.insert(
        hyper::header::CACHE_CONTROL,
        hyper::header::HeaderValue::from_static("no-cache"),
    );
    headers.insert(
        hyper::header::CONNECTION,
        hyper::header::HeaderValue::from_static("close"),
    );
    response
}
