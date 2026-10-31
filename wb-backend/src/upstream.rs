//! 上游调用（M1：非流式 chat）。
//!
//! 选号、body 改写、错误码映射都要和 Python 版对齐，细则见
//! specs/chat-nonstream.md。M1 只做「第一个 ready 账号 + 最小头 + body 改写」；
//! 身份指纹头族（wb_identity）与账号池（轮换/冷却/熔断）分别在 M2、M3。
//!
//! shortcut: 出站只带 Authorization/Content-Type/Accept —— 假上游不校验身份头，
//! 真上游要等 M2 把 wb_identity 的头族搬过来才通。

use std::sync::Arc;

use hyper::StatusCode;
use serde_json::{json, Map, Value};

use crate::record;
use crate::state::AccountFile;
use crate::App;

pub const DEFAULT_SYSTEM_PROMPT: &str = "You are a helpful assistant.";

pub fn chat_upstream_base(realm: &str) -> String {
    // 桌面/CLI 身分的 cn 对话端点可被 WB_CN_CHAT_UPSTREAM 改址；vscode 身分走
    // 自己硬编码的 www.workbuddy.cn，不吃这个变量（harness/README.md 有记录）。
    if realm == "cn" {
        if let Ok(v) = std::env::var("WB_CN_CHAT_UPSTREAM") {
            if !v.trim().is_empty() {
                return v.trim_end_matches('/').to_string();
            }
        }
        return env_else("WB_CN_UPSTREAM", "https://www.workbuddy.cn");
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

fn pick_account(app: &Arc<App>, realm: &str) -> Option<(String, String, String)> {
    let now = crate::state::now_epoch();
    app.state
        .accounts()
        .iter()
        .find(|a| a.realm() == realm && a.ready(now))
        .map(|a: &AccountFile| (a.uid(), a.realm(), a.str("accessToken").to_string()))
}

pub async fn chat_once(
    app: &Arc<App>,
    body: &Value,
    realm: &str,
    started: std::time::Instant,
) -> crate::http::Resp {
    let (uid, account_realm, token) = match pick_account(app, realm) {
        Some(picked) => picked,
        None => {
            return crate::http::error_resp(
                StatusCode::SERVICE_UNAVAILABLE,
                &format!("no usable account for realm {realm}"),
            )
        }
    };

    let forwarded = build_upstream_body(body);
    let model = forwarded
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let requested_model = body.get("model").and_then(Value::as_str).unwrap_or("").to_string();
    let effort = upstream_effort_of(body, &account_realm, &model);
    let fp = crate::fingerprint::prompt_fingerprint(
        forwarded
            .get("messages")
            .and_then(Value::as_array)
            .map(|m| m.as_slice())
            .unwrap_or(&[]),
    );

    let url = format!("{}{}", chat_upstream_base(&account_realm), crate::CHAT_PATH);
    // 上游要紧凑 JSON（与 Node JSON.stringify 同形态）；serde_json 默认就是紧凑形态。
    let encoded = serde_json::to_vec(&forwarded).unwrap_or_default();
    let resp = app
        .upstream
        .post(&url)
        .header("Authorization", format!("Bearer {token}"))
        .header("Content-Type", "application/json")
        .header("Accept", "text/event-stream")
        .body(encoded)
        .send()
        .await;

    let fail = |message: &str| {
        let elapsed = started.elapsed().as_millis() as i64;
        record::record(
            &app.recorder,
            record::Row {
                model: &model,
                stream: false,
                outcome: "failed",
                elapsed_ms: Some(elapsed),
                ttft_ms: None,
                gen_ms: None,
                requested_model: None,
                usage: None,
                fp: None,
                account: Some(&uid),
                realm: &account_realm,
                key: "",
                effort: None,
            },
        );
        crate::http::error_resp(StatusCode::BAD_GATEWAY, message)
    };

    let resp = match resp {
        Ok(r) => r,
        Err(e) => return fail(&format!("upstream unreachable: {e}")),
    };

    let status = resp.status();
    // 逐个 chunk 读而不是 text()：`ttft_ms` 量的是「首字节什么时候到」，读完再
    // 打时间戳会把首字延迟记成整个响应时间（gen_ms 恒为 0）。
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
            Err(e) => return fail(&format!("upstream stream error: {e}")),
        }
    }
    if !status.is_success() {
        // 错误码到报文的映射（11-128 / 11158 / 11140 / 11102 / 14220）在 M2。
        return fail(&format!(
            "upstream {}: {}",
            status.as_u16(),
            text.chars().take(600).collect::<String>()
        ));
    }

    let elapsed = started.elapsed().as_millis() as i64;
    let mut aggregated = match crate::sse::aggregate(&text) {
        Some(agg) => agg,
        None => return fail("upstream stream error"),
    };
    // 首块时刻以「真正收到第一个字节」为准，聚合器自己打的只是解析时刻。
    aggregated.first_chunk_at = first_chunk_at.or(aggregated.first_chunk_at);

    let started_at = crate::sse::epoch_now() - started.elapsed().as_secs_f64();
    let first_ms = aggregated.first_chunk_at.map(|at| ((at - started_at) * 1000.0) as i64);
    // 响应与 usage 行用的是同一份（已兜底/归一化的）usage，别各算一份。
    let usage = aggregated.client_usage();
    let usage_ref = if usage.is_null() { None } else { Some(&usage) };
    record::record(
        &app.recorder,
        record::Row {
            model: &model,
            stream: false,
            outcome: "completed",
            elapsed_ms: Some(elapsed),
            ttft_ms: first_ms,
            gen_ms: first_ms.map(|first| elapsed - first),
            requested_model: Some(&requested_model),
            usage: usage_ref,
            fp: Some(&fp),
            account: Some(&uid),
            realm: &account_realm,
            key: "",
            effort: effort.as_deref(),
        },
    );
    let payload = aggregated.into_payload(&json!(model), elapsed, usage);
    crate::http::json_resp(StatusCode::OK, &payload)
}
