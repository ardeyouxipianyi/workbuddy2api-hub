//! 客户端 SSE 的帧构造（形状拿真进程实测出来的，不是照上游原样转发）。
//!
//! 实测结论（Python 版 + 假上游 + 真上游各跑一遍）：
//! 1. 上游的 delta 帧原样转发，只把 `id`/`created`/`model` 换成网关自己的；
//! 2. 帧上的 `usage` 摘掉，改成收尾时单独发一条 `choices: []` 的 usage 帧
//!    （那条里的 usage 是归一化后的，带别名）；
//! 3. 最后 `data: [DONE]`。上游若自带一条 usage-only 帧，不转发。
//!
//! 早先的实现自己拼了 role 帧、补 logprobs/usage 字段 —— 那是照着真上游的帧
//! 「反推」的，夹具一跑就露馅：多出来的帧和字段 Python 都不会发。

use serde_json::{json, Value};

pub struct Emitter {
    pub id: String,
    pub model: String,
    pub created: i64,
}

impl Emitter {
    pub fn new(model: &str) -> Emitter {
        Emitter {
            id: format!("chatcmpl-wb-{}", crate::sse::hex_token(16)),
            model: model.to_string(),
            created: crate::state::now_epoch(),
        }
    }

    /// 转发一帧：换信封字段（同名替换，位置不变），摘掉 usage。
    pub fn rewrite(&self, frame: &Value) -> String {
        let mut out = match frame.as_object().cloned() {
            Some(map) => map,
            None => return String::new(),
        };
        out.insert("id".into(), json!(self.id));
        out.insert("model".into(), json!(self.model));
        out.insert("created".into(), json!(self.created));
        out.remove("usage");
        // 每个 choice 都要有 finish_reason（哪怕是 null）：Python 版逐帧补齐，
        // 严格的客户端（openai SDK 的 pydantic 模型）缺这个键会直接抛。
        if let Some(choices) = out.get_mut("choices").and_then(Value::as_array_mut) {
            for choice in choices {
                if let Some(map) = choice.as_object_mut() {
                    map.entry("finish_reason").or_insert(Value::Null);
                }
            }
        }
        format!("data: {}\n\n", Value::Object(out))
    }

    /// 收尾的 usage 帧：`choices` 为空，usage 与 usage 行同源。
    pub fn usage_frame(&self, usage: &Value) -> String {
        let mut out = serde_json::Map::new();
        out.insert("id".into(), json!(self.id));
        out.insert("object".into(), json!("chat.completion.chunk"));
        out.insert("created".into(), json!(self.created));
        out.insert("model".into(), json!(self.model));
        out.insert("choices".into(), json!([]));
        out.insert("usage".into(), usage.clone());
        format!("data: {}\n\n", Value::Object(out))
    }

    pub fn done_frame(&self) -> String {
        "data: [DONE]\n\n".to_string()
    }
}

/// 从上游 SSE 行里切出 `data:` 负载（跳过空行、注释与 `[DONE]`）。
pub fn parse_frame(line: &str) -> Option<&str> {
    let line = line.trim_end_matches(['\r', '\n']);
    let payload = line.strip_prefix("data:")?.trim();
    if payload.is_empty() || payload == "[DONE]" {
        None
    } else {
        Some(payload)
    }
}
