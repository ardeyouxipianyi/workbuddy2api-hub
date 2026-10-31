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
    /// 客户端看到的 usage（也是 usage 行里记的那份，与 Python 一致）。
    pub fn client_usage(&self) -> Value {
        usage_for_client(self.usage.clone(), &self.content, &self.reasoning)
    }

    pub fn into_payload(self, request_model: &Value, elapsed_ms: i64, usage: Value) -> Value {
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
        out.insert("usage".into(), usage);
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
        absorb(&mut agg, &chunk);
    }
    if !saw_any {
        return None;
    }
    Some(agg)
}

/// 把上游的一帧折进聚合结果。流式与非流式（先整个读完再聚合）共用这一份，
/// 免得两条路径对「usage 取哪一份」「delta 怎么累」各写一遍。
pub fn absorb(agg: &mut Aggregated, chunk: &Value) {
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
    if let Some(usage) = chunk.get("usage").filter(|u| !u.is_null()) {
        // 上游可能在中途和末帧各报一次；要的是 total_tokens 最大的那个，
        // 不是最后出现的那个（末帧有时只补 completion_tokens）。
        let total = |v: &Value| v.get("total_tokens").and_then(Value::as_i64).unwrap_or(0);
        let better = agg.usage.as_ref().map(|cur| total(usage) >= total(cur)).unwrap_or(true);
        if better {
            agg.usage = Some(usage.clone());
        }
    }
    let Some(choices) = chunk.get("choices").and_then(Value::as_array) else {
        return;
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

pub fn epoch_now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// 客户端看到的 usage：上游没给或全 0 时按字面估一条，命中缓存时补齐别名。
/// （`aggregate_stream` 尾部，wb_proxy.py:15846-15867）
fn usage_for_client(usage: Option<Value>, content: &str, reasoning: &str) -> Value {
    let total = |v: &Value| v.get("total_tokens").and_then(Value::as_i64).unwrap_or(0);
    let mut usage = match usage {
        Some(u) if total(&u) != 0 => u,
        other => {
            if content.is_empty() && reasoning.is_empty() {
                return other.unwrap_or(Value::Null);
            }
            let comp = estimate_tokens(content) + estimate_tokens(reasoning);
            let prompt = std::cmp::max(1, comp / 2);
            json!({
                "prompt_tokens": prompt,
                "completion_tokens": comp,
                "total_tokens": prompt + comp,
                "completion_tokens_details": {"reasoning_tokens": estimate_tokens(reasoning)},
                "prompt_tokens_details": {"cached_tokens": 0},
            })
        }
    };
    // 归一化：有命中就把四个别名一起补齐，客户端读哪个都对。
    let best = crate::record::best_cached_tokens(&usage);
    if best > 0 {
        if let Some(map) = usage.as_object_mut() {
            map.insert("cache_read_input_tokens".into(), json!(best));
            map.insert("cached_tokens".into(), json!(best));
            map.insert("prompt_cache_hit_tokens".into(), json!(best));
            if let Some(Value::Object(details)) = map.get_mut("prompt_tokens_details") {
                details.insert("cached_tokens".into(), json!(best));
            } else {
                map.insert("prompt_tokens_details".into(), json!({"cached_tokens": best}));
            }
            if let Some(Value::Object(details)) = map.get_mut("input_tokens_details") {
                details.insert("cached_tokens".into(), json!(best));
            }
        }
    }
    usage
}

/// `estimate_tokens`（wb_proxy.py:15595）：CJK 按字算，其余按 3.6 字/token。
fn estimate_tokens(text: &str) -> i64 {
    if text.is_empty() {
        return 0;
    }
    let cjk = text
        .chars()
        .filter(|c| matches!(*c as u32, 0x4e00..=0x9fff | 0x3400..=0x4dbf))
        .count() as i64;
    let other = text.chars().count() as i64 - cjk;
    if other == 0 {
        cjk
    } else {
        cjk + std::cmp::max(1, (other as f64 / 3.6) as i64)
    }
}

/// 16 个 hex 字符的随机 id 尾（`chatcmpl-wb-<16 hex>` 用）。
pub fn hex_token(len: usize) -> String {
    use std::io::Read;
    let mut raw = vec![0u8; len];
    let _ = std::fs::File::open("/dev/urandom").and_then(|mut f| f.read_exact(&mut raw));
    let alphabet = b"0123456789abcdef";
    raw.iter().map(|b| alphabet[*b as usize % 16] as char).collect()
}
