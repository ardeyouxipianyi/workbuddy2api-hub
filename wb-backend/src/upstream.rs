//! 上游调用（M1：非流式 chat）。
//!
//! 选号、body 改写、错误码映射都要和 Python 版对齐，细则见
//! specs/chat-nonstream.md。M1 只做「第一个 ready 账号 + 最小头 + body 改写」；
//! 身份指纹头族（wb_identity）与账号池（轮换/冷却/熔断）分别在 M2、M3。
//!
//! shortcut: 出站只带 Authorization/Content-Type/Accept —— 假上游不校验身份头，
//! 真上游要等 M2 把 wb_identity 的头族搬过来才通。

use std::sync::Arc;

use http_body_util::{BodyExt, StreamBody};
use hyper::body::{Bytes, Frame};
use hyper::StatusCode;
use serde_json::{json, Map, Value};

use crate::record;
use crate::state::AccountFile;
use crate::App;

pub const DEFAULT_SYSTEM_PROMPT: &str = "You are a helpful assistant.";

/// 对话端点：`endpoint_for(realm, product)[0]`（wb_identity.py:298-315 的 `_ENDPOINTS`）。
/// cn 的 desktop/cli 走 `CN_CHAT_BASE`（可用 WB_CN_CHAT_UPSTREAM 改址），
/// vscode 身分写死 `www.workbuddy.cn` 且不吃这个变量。
pub fn chat_upstream_base(realm: &str, product: &str) -> String {
    if realm == "cn" {
        if product == "vscode" {
            return "https://www.workbuddy.cn".to_string();
        }
        if let Ok(v) = std::env::var("WB_CN_CHAT_UPSTREAM") {
            let v = v.trim().trim_end_matches('/');
            if v.starts_with("http://") || v.starts_with("https://") {
                return v.to_string();
            }
        }
        return "https://copilot.tencent.com".to_string();
    }
    env_else("WB_UPSTREAM", "https://www.workbuddy.ai")
}

fn env_else(name: &str, default: &str) -> String {
    std::env::var(name)
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| default.to_string())
}

/// `build_upstream_body`（wb_proxy.py:13331-13425）的 M1 子集：
/// developer→system、注入 system、去掉 `_` 前缀键、max_completion_tokens 改名、
/// 强制 stream + stream_options.include_usage。工具配对修复与 thinking 注入留给 M2。
pub fn build_upstream_body(payload: &Value) -> Value {
    let mut body: Map<String, Value> = match payload {
        Value::Object(map) => map.clone(),
        _ => Map::new(),
    };
    let system_prompt = env_else("WB_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT);

    let mut messages: Vec<Value> = body
        .get("messages")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    for message in messages.iter_mut() {
        if let Some(map) = message.as_object_mut() {
            if map.get("role").and_then(Value::as_str) == Some("developer") {
                map.insert("role".into(), json!("system"));
            }
        }
    }
    // 首条不是 system 就在最前面补一条：指纹（n_msgs/msgs_sha）全靠这一步。
    let has_system = messages
        .first()
        .and_then(|m| m.get("role"))
        .and_then(Value::as_str)
        == Some("system");
    if !has_system {
        messages.insert(0, json!({"role": "system", "content": system_prompt}));
    }
    body.insert("messages".into(), Value::Array(messages));

    // 以 `_` 开头的键是网关内部标记，绝不外发。
    body.retain(|k, _| !k.starts_with('_'));
    if !body.contains_key("max_tokens") {
        if let Some(value) = body.get("max_completion_tokens").cloned() {
            body.insert("max_tokens".into(), value);
        }
    }
    body.remove("max_completion_tokens");
    body.insert("stream".into(), json!(true));
    if !body.contains_key("stream_options") {
        body.insert("stream_options".into(), json!({"include_usage": true}));
    }
    Value::Object(body)
}

/// `upstream_effort_of`（wb_proxy.py:13221）：这一请求实际跑的推理档位。
/// 目录把它钉死到某一档的模型（只有一种 effort）以目录为准；否则客户端的值优先；
/// 再否则用目录默认值。行里没有这个字段就等于「这模型没有推理控制」。
pub fn upstream_effort_of(body: &Value, realm: &str, model: &str) -> Option<String> {
    let entry = crate::models::lookup(realm, model);
    if let Some(efforts) = entry
        .as_ref()
        .and_then(|m| m.get("reasoning_efforts"))
        .and_then(Value::as_array)
    {
        if efforts.len() == 1 {
            return efforts[0].as_str().map(|s| s.to_string());
        }
    }
    let thinking_disabled = body
        .get("thinking")
        .and_then(|t| t.get("type"))
        .and_then(Value::as_str)
        .map(|t| t.trim().eq_ignore_ascii_case("disabled"))
        .unwrap_or(false);
    let given = body
        .get("reasoning_effort")
        .or_else(|| body.get("reasoningEffort"))
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty());
    if thinking_disabled {
        return Some("none".into());
    }
    if let Some(value) = given {
        if value.trim().eq_ignore_ascii_case("none") {
            return Some("none".into());
        }
        return Some(value.to_string());
    }
    entry
        .as_ref()
        .and_then(|m| m.get("reasoning_default_effort"))
        .and_then(Value::as_str)
        .map(|s| s.to_string())
}

/// 一次请求的公共上下文（两个入口都要，且都要 owned 交给 spawn）。
pub(crate) struct Ctx {
    pub(crate) model: String,
    pub(crate) requested_model: String,
    pub(crate) effort: Option<String>,
    pub(crate) fp: crate::fingerprint::Fingerprint,
}

/// 选号 → 取 token → 打上游，带 401 刷新重试与 429/5xx 换号（specs/account-pool.md §4）。
/// 成功返回 (账号, 上下文, 上游响应)；失败返回已经准备好的错误响应。
pub(crate) async fn open_upstream(
    app: &Arc<App>,
    body: &Value,
    realm: &str,
    session_key: Option<&str>,
) -> Result<(crate::pool::Candidate, Ctx, reqwest::Response), crate::http::Resp> {
    let forwarded = build_upstream_body(body);
    let model = forwarded.get("model").and_then(Value::as_str).unwrap_or("").to_string();
    let ctx = Ctx {
        effort: upstream_effort_of(body, realm, &model),
        requested_model: body.get("model").and_then(Value::as_str).unwrap_or("").to_string(),
        fp: crate::fingerprint::prompt_fingerprint(
            forwarded.get("messages").and_then(Value::as_array).map(|m| m.as_slice()).unwrap_or(&[]),
        ),
        model,
    };
    let encoded = serde_json::to_vec(&forwarded).unwrap_or_default();
    let mut tried: Vec<String> = Vec::new();
    let mut last = "no usable account".to_string();
    // 最多换三个号：再多就是上游整体挂了，换也没用。
    for _ in 0..3 {
        let cand = match crate::pool::candidates(app, realm, &tried).into_iter().next() {
            Some(c) => c,
            None => break,
        };
        tried.push(cand.uid.clone());
        let token = crate::pool::ensure_fresh(app, &cand).await;
        let url = format!(
            "{}{}",
            chat_upstream_base(&cand.realm, &cand.product),
            crate::CHAT_PATH
        );
        // 身份头族：上游按它判渠道合法性，缺了会被 11-128 拦（大 payload 才触发）。
        let headers = crate::identity::chat_headers(&crate::identity::Ctx {
            realm: &cand.realm,
            product: &cand.product,
            uid: &cand.uid,
            token: &token,
            enterprise_id: None,
            department: None,
            session_key,
        });
        // 调试用：WB_DUMP_BODY=/path 时把最后一次上游请求原样落盘（默认关）。
        if let Ok(path) = std::env::var("WB_DUMP_BODY") {
            let _ = std::fs::write(&path, &encoded);
        }
        let mut builder = app.upstream.post(&url);
        for (name, value) in headers {
            builder = builder.header(name, value);
        }
        let sent = builder.body(encoded.clone()).send().await;
        let resp = match sent {
            Ok(r) => r,
            Err(e) => {
                last = format!("upstream unreachable: {e}");
                continue;
            }
        };
        let status = resp.status();
        if status.is_success() {
            app.pool.clear(&cand.uid);
            return Ok((cand, ctx, resp));
        }
        let code = status.as_u16();
        let text = resp.text().await.unwrap_or_default();
        last = format!("upstream {code}: {}", text.chars().take(600).collect::<String>());
        if code == 401 {
            // 每号每请求最多刷一次（Python 的 refreshed 集合语义），刷完下次循环换号或重试。
            if crate::pool::refresh(app, &cand.uid).await.is_some() {
                tried.pop();
            }
            continue;
        }
        if code == 429 || code >= 500 {
            crate::pool::cooldown(app, &cand.uid, 60);
            continue;
        }
        break; // 其它 4xx 是请求本身的问题，换号无意义
    }
    Err(crate::http::error_resp(StatusCode::BAD_GATEWAY, &last))
}

/// 流式：先拿到上游响应头再交回 SSE 响应体（这样上游立刻失败还能回正常 JSON 错误）。
pub async fn chat_stream(
    app: &Arc<App>,
    body: &Value,
    realm: &str,
    started: std::time::Instant,
    key_id: &str,
    session_key: Option<&str>,
) -> crate::http::Resp {
    let (cand, ctx, resp) =
        match open_upstream(app, body, realm, session_key).await {
            Ok(v) => v,
            Err(resp) => return resp,
        };
    let emitter = crate::stream::Emitter::new(&ctx.model);
    let app = app.clone();
    let key_id = key_id.to_string();
    let (tx, rx) = tokio::sync::mpsc::channel::<Result<Frame<Bytes>, std::convert::Infallible>>(32);
    tokio::spawn(async move {
        let mut resp = resp;
        let mut buffer = String::new();
        let mut agg = crate::sse::Aggregated {
            id: emitter.id.clone(),
            model: emitter.model.clone(),
            created: emitter.created,
            content: String::new(),
            reasoning: String::new(),
            tool_calls: Vec::new(),
            usage: None,
            finish: None,
            first_chunk_at: None,
        };
        let mut first_chunk_at: Option<f64> = None;
        let mut broken: Option<String> = None;
        loop {
            let chunk = match resp.chunk().await {
                Ok(Some(chunk)) => chunk,
                Ok(None) => break,
                Err(e) => {
                    broken = Some(format!("upstream stream aborted: {e}"));
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
                if let Some(err) = frame.get("error") {
                    let message = err
                        .get("message")
                        .and_then(Value::as_str)
                        .unwrap_or("upstream error")
                        .to_string();
                    broken = Some(message);
                    break;
                }
                crate::sse::absorb(&mut agg, &frame);
                let has_choices = frame
                    .get("choices")
                    .and_then(Value::as_array)
                    .map(|c| !c.is_empty())
                    .unwrap_or(false);
                if has_choices {
                    let out = emitter.rewrite(&frame);
                    if !out.is_empty()
                        && tx.send(Ok(Frame::data(Bytes::from(out)))).await.is_err()
                    {
                        return;
                    }
                }
            }
            if broken.is_some() {
                break;
            }
        }
        agg.first_chunk_at = first_chunk_at;
        let elapsed = started.elapsed().as_millis() as i64;
        let first_ms = first_chunk_at.map(|at| {
            let start = crate::sse::epoch_now() - started.elapsed().as_secs_f64();
            ((at - start) * 1000.0) as i64
        });
        let usage = agg.client_usage();
        let mut tail = String::new();
        match broken.as_deref() {
            // 坏流：只发一条 error 帧，不发 finish/usage/[DONE]。
            Some(message) => tail.push_str(&emitter.error_frame(message)),
            None => {
                if !usage.is_null() {
                    tail.push_str(&emitter.usage_frame(&usage));
                }
                tail.push_str(&emitter.done_frame());
            }
        }
        let _ = tx.send(Ok(Frame::data(Bytes::from(tail)))).await;
        record::record(
            &app.recorder,
            record::Row {
                model: &ctx.model,
                stream: true,
                outcome: if broken.is_some() { "failed" } else { "completed" },
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
    headers.insert(
        hyper::header::ACCESS_CONTROL_ALLOW_ORIGIN,
        hyper::header::HeaderValue::from_static("*"),
    );
    response
}

/// 非流式：内部仍按流式取上游，读完再聚合成普通 JSON。
pub async fn chat_once(
    app: &Arc<App>,
    body: &Value,
    realm: &str,
    started: std::time::Instant,
    key_id: &str,
    session_key: Option<&str>,
) -> crate::http::Resp {
    let (cand, ctx, resp) = match open_upstream(app, body, realm, session_key).await {
        Ok(v) => v,
        Err(resp) => return resp,
    };
    let mut text = String::new();
    let mut first_chunk_at: Option<f64> = None;
    let mut resp = resp;
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
                return record_failure(app, &cand, &ctx, key_id, started, &format!("upstream stream error: {e}"))
            }
        }
    }
    let elapsed = started.elapsed().as_millis() as i64;
    let mut aggregated = match crate::sse::aggregate(&text) {
        Some(agg) => agg,
        None => return record_failure(app, &cand, &ctx, key_id, started, "upstream stream error"),
    };
    // 首块时刻以「真正收到第一个字节」为准，聚合器自己打的只是解析时刻。
    aggregated.first_chunk_at = first_chunk_at.or(aggregated.first_chunk_at);
    let started_at = crate::sse::epoch_now() - started.elapsed().as_secs_f64();
    let first_ms = aggregated.first_chunk_at.map(|at| ((at - started_at) * 1000.0) as i64);
    let usage = aggregated.client_usage();
    record::record(
        &app.recorder,
        record::Row {
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
    let payload = aggregated.into_payload(&json!(ctx.model), elapsed, usage);
    crate::http::json_resp(StatusCode::OK, &payload)
}

fn record_failure(
    app: &Arc<App>,
    cand: &crate::pool::Candidate,
    ctx: &Ctx,
    key_id: &str,
    started: std::time::Instant,
    message: &str,
) -> crate::http::Resp {
    record::record(
        &app.recorder,
        record::Row {
            model: &ctx.model,
            stream: false,
            outcome: "failed",
            elapsed_ms: Some(started.elapsed().as_millis() as i64),
            ttft_ms: None,
            gen_ms: None,
            requested_model: None,
            usage: None,
            fp: None,
            account: Some(&cand.uid),
            realm: &cand.realm,
            key: key_id,
            effort: None,
        },
    );
    crate::http::error_resp(StatusCode::BAD_GATEWAY, message)
}
