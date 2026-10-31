//! 上游 SSE 的聚合（Python 版 `aggregate_stream`，wb_proxy.py:15722-15871）。
//!
//! 上游一律按流式回（`body["stream"]=true`），非流式客户端要的是拼好的一条
//! message。哪些字段原样透传、哪些由网关补，等于协议本身，改之前先看 spec。

use serde_json::{json, Map, Value};

pub struct Aggregated {
    pub id: String,
    pub model: String,
    pub created: i64,
    pub content: String,
    pub reasoning: String,
    pub tool_calls: Vec<Value>,
    pub usage: Option<Value>,
    pub finish: Option<String>,
    pub first_chunk_at: Option<f64>,
}

impl Aggregated {
    pub fn into_payload(self, request_model: &Value, elapsed_ms: i64) -> Value {
        let mut message = Map::new();
        message.insert("role".into(), json!("assistant"));
        message.insert("content".into(), json!(self.content));
        if !self.reasoning.is_empty() {
            message.insert("reasoning_content".into(), json!(self.reasoning));
        }
        if !self.tool_calls.is_empty() {
            message.insert("tool_calls".into(), Value::Array(self.tool_calls));
        }
        let mut out = Map::new();
        out.insert("id".into(), json!(self.id));
        out.insert("object".into(), json!("chat.completion"));
        out.insert("created".into(), json!(self.created));
        out.insert(
            "model".into(),
            if self.model.is_empty() { request_model.clone() } else { json!(self.model) },
        );
        out.insert(
            "choices".into(),
            json!([{
                "index": 0,
                "message": Value::Object(message),
                "finish_reason": self.finish.unwrap_or_else(|| "stop".to_string()),
            }]),
        );
        out.insert("usage".into(), self.usage.unwrap_or(Value::Null));
        out.insert("elapsed_ms".into(), json!(elapsed_ms));
        out.insert("first_chunk_at".into(), json!(self.first_chunk_at.unwrap_or(0.0)));
        Value::Object(out)
    }
}

/// 解析 SSE 文本。`[DONE]` 结束；usage 取最后一个非空帧（上游通常在末帧补）。
pub fn aggregate(text: &str) -> Option<Aggregated> {
    let mut agg = Aggregated {
        id: String::new(),
        model: String::new(),
        created: 0,
        content: String::new(),
        reasoning: String::new(),
        tool_calls: Vec::new(),
        usage: None,
        finish: None,
        first_chunk_at: None,
    };
    let mut saw_any = false;
    for line in text.lines() {
        let payload = match line.strip_prefix("data:") {
            Some(rest) => rest.trim(),
            None => continue,
        };
        if payload.is_empty() {
            continue;
        }
        if payload == "[DONE]" {
            break;
        }
        let chunk: Value = match serde_json::from_str(payload) {
            Ok(v) => v,
            Err(_) => continue,
        };
        saw_any = true;
        if agg.first_chunk_at.is_none() {
            agg.first_chunk_at = Some(epoch_now());
        }
        if agg.id.is_empty() {
            if let Some(s) = chunk.get("id").and_then(Value::as_str) {
                agg.id = s.to_string();
            }
        }
        if agg.model.is_empty() {
            if let Some(s) = chunk.get("model").and_then(Value::as_str) {
                agg.model = s.to_string();
            }
        }
        if agg.created == 0 {
            if let Some(n) = chunk.get("created").and_then(Value::as_i64) {
                agg.created = n;
            }
        }
        if chunk.get("usage").map(|u| !u.is_null()).unwrap_or(false) {
            agg.usage = chunk.get("usage").cloned();
        }
        let Some(choices) = chunk.get("choices").and_then(Value::as_array) else {
            continue;
        };
        for choice in choices {
            if let Some(reason) = choice.get("finish_reason").and_then(Value::as_str) {
                agg.finish = Some(reason.to_string());
            }
            let Some(delta) = choice.get("delta") else { continue };
            if let Some(s) = delta.get("content").and_then(Value::as_str) {
                agg.content.push_str(s);
            }
            if let Some(s) = delta.get("reasoning_content").and_then(Value::as_str) {
                agg.reasoning.push_str(s);
            }
            if let Some(calls) = delta.get("tool_calls").and_then(Value::as_array) {
                for call in calls {
                    agg.tool_calls.push(call.clone());
                }
            }
        }
    }
    if !saw_any {
        return None;
    }
    Some(agg)
}

pub fn epoch_now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}
