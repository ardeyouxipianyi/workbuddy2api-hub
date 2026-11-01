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
        let mut empty = true;
        if let Some(choices) = out.get_mut("choices").and_then(Value::as_array_mut) {
            for choice in choices {
                if let Some(map) = choice.as_object_mut() {
                    let finish = map.get("finish_reason").and_then(Value::as_str).is_some()
                        || map.get("finish_reason").map(|v| !v.is_null()).unwrap_or(false);
                    // 空占位是噪音，而且会骗到客户端：每帧都带 `tool_calls: []` /
                    // `function_call: null` 时，界面上每个 token 都像「新开一段」，
                    // 思考内容会被拆成一屏一屏地重画。Python 侧逐帧剔过这些键
                    // （wb_proxy.py:11765 `_clean_frame`），这里照同样的规则。
                    if let Some(delta) = map.get_mut("delta").and_then(Value::as_object_mut) {
                        if delta.get("function_call").map(|v| match v {
                            Value::Object(o) => o.get("name").and_then(Value::as_str).unwrap_or("").is_empty(),
                            Value::Null => true,
                            other => other.as_str().map(str::is_empty).unwrap_or(false),
                        }) == Some(true) {
                            delta.remove("function_call");
                        }
                        if delta.get("tool_calls").and_then(Value::as_array).map(Vec::is_empty) == Some(true) {
                            delta.remove("tool_calls");
                        }
                        for noise in ["extra_fields", "refusal", "reasoning_content"] {
                            let drop_it = match delta.get(noise) {
                                Some(Value::String(text)) => text.is_empty(),
                                Some(Value::Null) | None => delta.contains_key(noise),
                                Some(_) => false,
                            };
                            if drop_it {
                                delta.remove(noise);
                            }
                        }
                        if delta.is_empty() {
                            if finish {
                                empty = false;
                            }
                        } else {
                            empty = false;
                        }
                    } else {
                        empty = false;
                    }
                    map.entry("finish_reason").or_insert(Value::Null);
                }
            }
        }
        if empty && out.get("choices").and_then(Value::as_array).map(Vec::is_empty) == Some(false) {
            return String::new();
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

    /// 流中途失败：状态码已经是 200，只能靠帧说明（specs/streaming-and-errors.md §1.11）。
    /// 之后**不发** finish/usage/`[DONE]` —— 客户端看到 `[DONE]` 或 finish 帧才算正常结束。
    pub fn error_frame(&self, message: &str) -> String {
        format!(
            "data: {}\n\n",
            json!({"error": {
                "message": message,
                "type": "upstream_error",
                "param": null,
                "code": "upstream_stream_error",
            }})
        )
    }

    pub fn done_frame(&self) -> String {
        "data: [DONE]\n\n".to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn emitter() -> Emitter {
        Emitter { id: "chatcmpl-wb-test".into(), model: "deepseek-v4.1-flash".into(), created: 1 }
    }

    /// 空占位必须剔掉：它们会让客户端把每个 token 当成「新开一段」。
    #[test]
    fn empty_placeholders_are_stripped() {
        let frame = json!({"choices": [{"index": 0, "delta": {
            "content": "", "reasoning_content": " mounts", "function_call": null,
            "refusal": "", "tool_calls": [], "extra_fields": null
        }}]});
        let out = emitter().rewrite(&frame);
        assert!(out.contains("\"reasoning_content\":\" mounts\""), "{out}");
        assert!(!out.contains("function_call"), "{out}");
        assert!(!out.contains("tool_calls"), "{out}");
        assert!(!out.contains("refusal"), "{out}");
        assert!(!out.contains("extra_fields"), "{out}");
    }

    /// 剔干净之后什么都没剩、又没有 finish_reason 的帧不发（Python 同样返回空串）。
    #[test]
    fn frames_left_empty_are_dropped_but_finish_frames_survive() {
        let noise = json!({"choices": [{"index": 0, "delta": {
            "reasoning_content": "", "function_call": null, "tool_calls": [], "extra_fields": null
        }}]});
        assert_eq!(emitter().rewrite(&noise), "");
        let finish = json!({"choices": [{"index": 0, "delta": {
            "reasoning_content": "", "tool_calls": []
        }, "finish_reason": "tool_calls"}]});
        let out = emitter().rewrite(&finish);
        assert!(out.contains("tool_calls"), "{out}");
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
