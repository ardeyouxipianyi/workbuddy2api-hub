//! CodeArts 聊天链路：请求体收敛、选号、打开 SSE、流式/非流式回包。
//!
//! 对照 `wb_codearts.build_chat_body`/`_chat_headers`/`_client_chat`/`open_chat`
//! 与 `wb_proxy._handle_codearts_chat`。

use std::sync::Arc;

use hyper::body::Bytes;
use http_body_util::BodyExt;
use hyper::StatusCode;
use serde_json::{json, Map, Value};

use super::stream::CodeArtsStream;
use super::{
    now_secs, snap_host, Account, CodeArtsError, Pool, Result, EP_CHAT, HEADER_MAAS_TYPE, MAAS_BENEFIT,
};
use crate::http::{json_resp, Resp};
use crate::App;

/// `build_chat_body`：只发 Go 版 `chatBodyV2` 会发的字段。
pub fn build_chat_body(payload: &Value, session_key: Option<&str>) -> Result<Value> {
    let Some(source) = payload.as_object() else {
        return Err(CodeArtsError::BadRequest("request body must be a JSON object".into()));
    };
    let allowed = [
        "model",
        "messages",
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "reasoning_effort",
        "tools",
        "tool_choice",
    ];
    let mut body: Map<String, Value> = source
        .iter()
        .filter(|(key, _)| allowed.contains(&key.as_str()))
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect();
    let model = source
        .get("model")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .unwrap_or("GLM-5.2")
        .to_string();
    body.insert("model".into(), json!(model));

    let raw_messages = source.get("messages").and_then(Value::as_array).cloned().unwrap_or_default();
    let mut messages: Vec<Value> = Vec::with_capacity(raw_messages.len());
    for mut message in raw_messages {
        if !message.is_object() {
            continue;
        }
        if message.get("role").and_then(Value::as_str) == Some("developer") {
            message["role"] = json!("system");
        }
        if let Some(content) = message.get("content") {
            if content.is_array() {
                message["content"] = json!(flatten_content(content)?);
            }
        }
        messages.push(message);
    }
    // shortcut: 不做 Python 的 merge_adjacent_tool_calls / repack_tool_result_blocks /
    // cleanup_orphan_tool_calls；多轮工具场景下孤立的 tool 消息会原样发给 CodeArts。
    if messages.is_empty() {
        return Err(CodeArtsError::BadRequest("messages is empty".into()));
    }
    body.insert("messages".into(), Value::Array(messages.clone()));

    let tools = body.get("tools").cloned();
    if let Some(choice) = body.get("tool_choice").cloned() {
        if choice.is_object() {
            let name = choice
                .get("function")
                .and_then(|function| function.get("name"))
                .and_then(Value::as_str)
                .unwrap_or("");
            if !name.is_empty() {
                if let Some(Value::Array(list)) = tools.clone() {
                    let filtered: Vec<Value> = list
                        .into_iter()
                        .filter(|tool| {
                            tool.get("function").and_then(|f| f.get("name")).and_then(Value::as_str) == Some(name)
                        })
                        .collect();
                    if filtered.is_empty() {
                        body.remove("tool_choice");
                    } else {
                        body.insert("tools".into(), Value::Array(filtered));
                        body.insert("tool_choice".into(), json!("required"));
                    }
                } else {
                    body.remove("tool_choice");
                }
            } else {
                body.remove("tool_choice");
            }
        }
    }
    let has_tools = body.get("tools").and_then(Value::as_array).map(|list| !list.is_empty()).unwrap_or(false);
    if !has_tools {
        body.remove("tool_choice");
    }
    if body.get("max_completion_tokens").is_some() && body.get("max_tokens").is_none() {
        if let Some(value) = body.remove("max_completion_tokens") {
            body.insert("max_tokens".into(), value);
        }
    }
    let mut chat_id = payload
        .get("conversation_id")
        .and_then(Value::as_str)
        .filter(|value| valid_chat_id(value))
        .map(|value| value.to_string())
        .unwrap_or_default();
    if chat_id.is_empty() {
        chat_id = derive_chat_id(session_key, &messages);
    }
    body.insert("chat_id".into(), json!(chat_id));
    body.insert("prompt_cache_key".into(), json!(chat_id));
    body.insert("stream".into(), json!(true));
    body.insert("tool_stream".into(), json!(true));
    Ok(Value::Object(body))
}

/// CodeArts 只消费 text 分片；带图片直接 400（与 Python 同一条文案）。
fn flatten_content(content: &Value) -> Result<String> {
    if let Some(text) = content.as_str() {
        return Ok(text.to_string());
    }
    let Some(parts) = content.as_array() else {
        return Ok(content.to_string());
    };
    let has_image = parts.iter().any(|part| {
        let kind = part.get("type").and_then(Value::as_str).unwrap_or("");
        matches!(kind, "image" | "image_url" | "input_image") || part.get("image_url").is_some()
    });
    if has_image {
        return Err(CodeArtsError::BadRequest(
            "CodeArts 出口暂不支持图片输入，请移除图片或选择其他出口".into(),
        ));
    }
    Ok(parts
        .iter()
        .map(|part| part.get("text").and_then(Value::as_str).unwrap_or("").to_string())
        .collect())
}

fn valid_chat_id(value: &str) -> bool {
    value.len() == 32 && value.chars().all(|c| c.is_ascii_hexdigit())
}

fn sha256_hex(text: &str) -> String {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(text.as_bytes());
    hasher.finalize().iter().map(|byte| format!("{byte:02x}")).collect()
}

fn derive_chat_id(session_key: Option<&str>, messages: &[Value]) -> String {
    if let Some(key) = session_key.filter(|value| !value.is_empty()) {
        return sha256_hex(key).chars().take(32).collect();
    }
    for message in messages {
        if message.get("role").and_then(Value::as_str) != Some("user") {
            continue;
        }
        if let Some(text) = message.get("content").and_then(Value::as_str) {
            if !text.is_empty() {
                return sha256_hex(&format!("codearts:{text}")).chars().take(32).collect();
            }
        }
    }
    super::crypto::random_hex(16)
}

pub struct OpenedChat {
    pub response: reqwest::Response,
    pub account: Arc<Account>,
    pub model: String,
    pub chat_id: String,
}

fn is_concurrency_message(message: &str) -> bool {
    let text = message.to_lowercase();
    text.contains("并发会话")
        || text.contains("tm.00001041")
        || text.contains("concurrent session")
        || text.contains("maximum number of active")
}

/// `_client_chat`：签名 → 加 Chat-Id/Session-Id（不进 SignedHeaders）→ 3 次重试。
async fn client_chat(account: &Account, body: &Value, session_key: Option<&str>) -> Result<(SyntheticResponse, String, String)> {
    let model = super::models::canonical_model(account, body.get("model").and_then(Value::as_str).unwrap_or(""));
    let messages = body.get("messages").and_then(Value::as_array).cloned().unwrap_or_default();
    let mut chat_id = body.get("chat_id").and_then(Value::as_str).unwrap_or("").to_string();
    if !valid_chat_id(&chat_id) {
        chat_id = derive_chat_id(session_key, &messages);
    }
    let session_id: String = sha256_hex(&format!("codearts-session:{chat_id}")).chars().take(32).collect();

    let mut upstream = body.clone();
    let map = upstream.as_object_mut().ok_or_else(|| CodeArtsError::Other("bad body".into()))?;
    map.insert("model".into(), json!(model));
    map.insert("chat_id".into(), json!(chat_id));
    map.insert("prompt_cache_key".into(), json!(chat_id));
    map.insert("stream".into(), json!(true));
    map.insert("tool_stream".into(), json!(true));
    let raw = serde_json::to_vec(&upstream).unwrap_or_default();

    let mut headers: Vec<(String, String)> = vec![
        ("Content-Type".into(), "application/json".into()),
        ("Accept".into(), "text/event-stream".into()),
        (
            "x-auth-token".into(),
            account.auth().get("cloud_dragon_token").and_then(Value::as_str).unwrap_or("").to_string(),
        ),
        ("x-snap-traceid".into(), super::crypto::random_hex(16)),
        ("X-Language".into(), "zh-cn".into()),
        ("app-id".into(), "CodeAgent3.0".into()),
        ("is_confidential".into(), "false".into()),
    ];
    if account.is_benefit(&model) {
        headers.push((HEADER_MAAS_TYPE.into(), MAAS_BENEFIT.into()));
    }
    let (access_key, secret_key, security_token) = account.credential();
    let signed = super::crypto::sign_request(
        "POST",
        &format!("{}{}", snap_host(), EP_CHAT),
        &headers,
        &raw,
        &access_key,
        &secret_key,
        &security_token,
        &super::client::utc_stamp(),
    )
    .map_err(CodeArtsError::Other)?;
    let mut signed = signed;
    // 与 Go 版一致：Chat-Id / Session-Id 在签名之后才加。
    signed.push(("Chat-Id".into(), chat_id.clone()));
    signed.push(("Session-Id".into(), session_id));

    let url = format!("{}{}", snap_host(), EP_CHAT);
    let timeout = 600.0;
    let mut last_error: Option<CodeArtsError> = None;
    for attempt in 0..3 {
        let (status, text) = match super::client::http("POST", &url, &signed, Some(raw.clone()), timeout, &account.proxy()).await {
            Ok((status, bytes)) => (status, String::from_utf8_lossy(&bytes).to_string()),
            Err(err) => return Err(err),
        };
        if status >= 400 {
            let message = super::client::error_text(text.as_bytes());
            if status == 401 || status == 403 {
                return Err(CodeArtsError::Auth(message));
            }
            if status == 400 && is_concurrency_message(&message) {
                last_error = Some(CodeArtsError::Upstream(message));
                if attempt < 2 {
                    tokio::time::sleep(std::time::Duration::from_secs_f64(2.0 * (attempt as f64 + 1.0))).await;
                    continue;
                }
                return Err(last_error.unwrap());
            }
            if status == 400 {
                return Err(CodeArtsError::BadRequest(message));
            }
            if status == 429 {
                return Err(CodeArtsError::Upstream(message));
            }
            return Err(CodeArtsError::Other(format!("CodeArts 返回 HTTP {status}：{message}")));
        }
        // 2xx：把上面读到的字节重新包成一次请求是浪费；直接返回文本，
        // 由调用方按行喂给 CodeArtsStream（SSE 就是按行解析）。
        return Ok((SyntheticResponse { text }, model, chat_id));
    }
    Err(last_error.unwrap_or_else(|| CodeArtsError::Upstream("CodeArts 并发会话已满".into())))
}

/// 已经把整段 SSE 读进内存的响应（CodeArts 的事件都是按行解析，不需要真流）。
pub struct SyntheticResponse {
    pub text: String,
}

/// `open_chat`：选号 → 续期 → 目录 → 打开流；失败按 Python 的规则退号/冷却。
pub async fn open_chat(
    pool: &Pool,
    body: &Value,
    session_key: Option<&str>,
    allowed: Option<&[String]>,
) -> Result<(SyntheticResponse, Arc<Account>, String, String)> {
    let model = body.get("model").and_then(Value::as_str).unwrap_or("").to_string();
    let mut exclude: Vec<String> = Vec::new();
    let mut last_error: Option<CodeArtsError> = None;
    let attempts = (pool.list_accounts().len() + 1).max(2);
    for _ in 0..attempts {
        let Some(account) = pool.pick(&model, session_key, &exclude, allowed) else { break };
        if !pool.acquire(&account) {
            exclude.push(account.uid.clone());
            continue;
        }
        let outcome = async {
            super::client::ensure_token(&account, pool).await?;
            let _ = super::models::ensure_models(&account, pool, false, 8.0).await;
            client_chat(&account, body, session_key).await
        }
        .await;
        match outcome {
            Ok((response, used_model, chat_id)) => {
                pool.note_success(&account);
                if let Some(key) = session_key {
                    pool.bind_session(key, &account.uid);
                }
                return Ok((response, account, used_model, chat_id));
            }
            Err(CodeArtsError::BadRequest(message)) => {
                pool.release(&account);
                return Err(CodeArtsError::BadRequest(message));
            }
            Err(err) => {
                let soft = matches!(err, CodeArtsError::Upstream(_) | CodeArtsError::Unavailable(_));
                let terminal = matches!(err, CodeArtsError::Auth(_));
                pool.note_error(&account, err.message(), soft, terminal);
                pool.release(&account);
                exclude.push(account.uid.clone());
                last_error = Some(err);
            }
        }
    }
    if let Some(err) = last_error {
        return Err(err);
    }
    // 一个号都没选到：把停用/冷却原因带出去，面板能直接看懂。
    let now = now_secs();
    let reasons: Vec<String> = pool
        .list_accounts()
        .iter()
        .filter_map(|account| {
            let (disabled, reason, cool_until, _, last_error) = account.state_snapshot();
            if disabled {
                Some(format!("{} 已停用：{}", account.nickname(), if reason.is_empty() { "手动停用".into() } else { reason }))
            } else if cool_until > now {
                Some(format!("{} 冷却中：{}", account.nickname(), if last_error.is_empty() { "稍后重试".into() } else { last_error }))
            } else {
                None
            }
        })
        .take(2)
        .collect();
    if !reasons.is_empty() {
        return Err(CodeArtsError::Unavailable(format!("没有可用的 CodeArts 账号：{}", reasons.join("；"))));
    }
    Err(CodeArtsError::Unavailable(
        "没有可用的 CodeArts 账号：请在看板「账号」页添加并启用至少一个账号。".into(),
    ))
}

fn error_response(app: &Arc<App>, model: &str, account: Option<&str>, key_id: &str, started: std::time::Instant, err: &CodeArtsError) -> Resp {
    let status = match err {
        CodeArtsError::BadRequest(_) => StatusCode::BAD_REQUEST,
        CodeArtsError::NotFound(_) | CodeArtsError::Conflict(_) => StatusCode::CONFLICT,
        _ => StatusCode::BAD_GATEWAY,
    };
    crate::record::record(
        app,
        crate::record::Row {
            model,
            stream: false,
            outcome: "failed",
            elapsed_ms: Some(started.elapsed().as_millis() as i64),
            ttft_ms: None,
            gen_ms: None,
            requested_model: None,
            usage: None,
            fp: None,
            account,
            realm: "codearts",
            key: key_id,
            effort: None,
        },
    );
    json_resp(
        status,
        &json!({"error": {"message": err.message(), "type": err.error_type(), "code": status.as_u16()}}),
    )
}

/// `/v1/chat/completions` 的 CodeArts 分支（流式）。
pub async fn chat_stream(
    app: &Arc<App>,
    body: &Value,
    session_key: Option<&str>,
    allowed: Option<&[String]>,
    started: std::time::Instant,
    key_id: &str,
) -> Resp {
    let pool = super::pool_for(app);
    if !pool.config().enabled {
        return error_response(app, body.get("model").and_then(Value::as_str).unwrap_or("-"), None, key_id, started,
            &CodeArtsError::BadRequest("CodeArts 出口未启用：请到设置页打开「启用 CodeArts 出口」。".into()));
    }
    let request = match build_chat_body(body, session_key) {
        Ok(value) => value,
        Err(err) => return error_response(app, body.get("model").and_then(Value::as_str).unwrap_or("-"), None, key_id, started, &err),
    };
    let (response, account, model, _chat_id) = match open_chat(&pool, &request, session_key, allowed).await {
        Ok(value) => value,
        Err(err) => {
            let requested = body.get("model").and_then(Value::as_str).unwrap_or("-").to_string();
            return error_response(app, &requested, None, key_id, started, &err);
        }
    };
    app.note(&format!(
        "codearts chat: model={model} stream=True msgs={} key={}",
        body.get("messages").and_then(Value::as_array).map(|m| m.len()).unwrap_or(0),
        key_id.get(..8).unwrap_or(key_id)
    ));

    use http_body_util::StreamBody;
    use hyper::body::Frame;
    let (tx, rx) = tokio::sync::mpsc::channel::<std::result::Result<Frame<Bytes>, std::convert::Infallible>>(32);
    let app = app.clone();
    let key_id = key_id.to_string();
    let uid = account.uid.clone();
    tokio::spawn(async move {
        let mut stream = CodeArtsStream::new(&model);
        let mut failure: Option<String> = None;
        let mut collected = String::new();
        for line in response.text.lines() {
            match stream.feed(line) {
                Ok(chunks) => {
                    for chunk in chunks {
                        collected.push_str(&chunk);
                        if tx.send(Ok(Frame::data(Bytes::from(chunk)))).await.is_err() {
                            pool_release(&account);
                            return;
                        }
                    }
                }
                Err(err) => {
                    failure = Some(err.message().to_string());
                    break;
                }
            }
            if stream.done() {
                break;
            }
        }
        if failure.is_none() {
            for chunk in stream.tail() {
                collected.push_str(&chunk);
                let _ = tx.send(Ok(Frame::data(Bytes::from(chunk)))).await;
            }
        }
        let elapsed = started.elapsed().as_millis() as i64;
        let aggregated = crate::sse::aggregate(&collected);
        let usage = aggregated.as_ref().map(|agg| agg.client_usage()).unwrap_or(Value::Null);
        crate::record::record(
            app.as_ref(),
            crate::record::Row {
                model: &model,
                stream: true,
                outcome: if failure.is_some() { "failed" } else { "completed" },
                elapsed_ms: Some(elapsed),
                ttft_ms: aggregated.as_ref().and_then(|agg| {
                    let started_at = crate::sse::epoch_now() - started.elapsed().as_secs_f64();
                    agg.first_chunk_at.map(|at| ((at - started_at) * 1000.0) as i64)
                }),
                gen_ms: None,
                requested_model: None,
                usage: if usage.is_null() { None } else { Some(&usage) },
                fp: None,
                account: Some(&uid),
                realm: "codearts",
                key: &key_id,
                effort: None,
            },
        );
        // 流结束（正常/中断/客户端断开）都要把并发名额还回去，否则该号一直占满。
        pool_release(&account);
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

fn pool_release(account: &Arc<Account>) {
    let _ = account
        .active
        .fetch_update(std::sync::atomic::Ordering::Relaxed, std::sync::atomic::Ordering::Relaxed, |value| value.checked_sub(1));
}

/// `/v1/chat/completions` 的 CodeArts 分支（非流式）。
pub async fn chat_once(
    app: &Arc<App>,
    body: &Value,
    session_key: Option<&str>,
    allowed: Option<&[String]>,
    started: std::time::Instant,
    key_id: &str,
) -> Resp {
    let pool = super::pool_for(app);
    let requested = body.get("model").and_then(Value::as_str).unwrap_or("-").to_string();
    if !pool.config().enabled {
        return error_response(app, &requested, None, key_id, started,
            &CodeArtsError::BadRequest("CodeArts 出口未启用：请到设置页打开「启用 CodeArts 出口」。".into()));
    }
    let request = match build_chat_body(body, session_key) {
        Ok(value) => value,
        Err(err) => return error_response(app, &requested, None, key_id, started, &err),
    };
    let (response, account, model, _chat_id) = match open_chat(&pool, &request, session_key, allowed).await {
        Ok(value) => value,
        Err(err) => return error_response(app, &requested, None, key_id, started, &err),
    };
    let mut stream = CodeArtsStream::new(&model);
    let mut collected = String::new();
    let mut failure: Option<CodeArtsError> = None;
    for line in response.text.lines() {
        match stream.feed(line) {
            Ok(chunks) => collected.push_str(&chunks.concat()),
            Err(err) => {
                failure = Some(err);
                break;
            }
        }
        if stream.done() {
            break;
        }
    }
    if failure.is_none() {
        collected.push_str(&stream.tail().concat());
    }
    pool_release(&account);
    if let Some(err) = failure {
        return error_response(app, &model, Some(&account.uid), key_id, started, &err);
    }
    let elapsed = started.elapsed().as_millis() as i64;
    let aggregated = match crate::sse::aggregate(&collected) {
        Some(agg) => agg,
        None => {
            return error_response(app, &model, Some(&account.uid), key_id, started,
                &CodeArtsError::Upstream("CodeArts 没有返回可解析的内容".into()))
        }
    };
    let usage = aggregated.client_usage();
    crate::record::record(
        app,
        crate::record::Row {
            model: &model,
            stream: false,
            outcome: "completed",
            elapsed_ms: Some(elapsed),
            ttft_ms: None,
            gen_ms: None,
            requested_model: None,
            usage: if usage.is_null() { None } else { Some(&usage) },
            fp: None,
            account: Some(&account.uid),
            realm: "codearts",
            key: key_id,
            effort: None,
        },
    );
    let payload = aggregated.into_payload(&json!(model), elapsed, usage);
    json_resp(StatusCode::OK, &payload)
}

/// `/v1/models?realm=codearts`（Python `_get_codearts_models`）。
pub async fn models_response(app: &Arc<App>) -> Resp {
    let pool = super::pool_for(app);
    if !pool.config().enabled {
        return json_resp(
            StatusCode::SERVICE_UNAVAILABLE,
            &json!({"error": {"message": "CodeArts 出口未启用：请到设置页填写 Base URL 和 API Key。",
                              "type": "invalid_request_error", "code": 503}}),
        );
    }
    let entries = super::models::model_entries(&pool).await;
    let data: Vec<Value> = entries;
    json_resp(
        StatusCode::OK,
        &json!({"object": "list", "data": data, "realm": "codearts",
                "catalog": super::models::models_meta(&pool)}),
    )
}

/// `signed_get` 的薄封装，admin/oauth 侧查身份用。
pub async fn identity_holder_get(access_key: &str, secret_key: &str, token: &str, url: &str) -> Result<Map<String, Value>> {
    let holder = super::client::CredentialHolder::new(access_key, secret_key, token);
    holder.signed_get(url, &[], 10.0, "").await
}
