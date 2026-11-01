//! 把 CodeArts 的 SSE 事件归一成标准 OpenAI chunk（`CodeArtsStream` 的移植）。
//!
//! 上游事件有四种形状：标准 `choices[].delta`、顶层 `delta`、全文快照 `text`
//! （每次都发整段，需要做差分）、终态 `output[]`。下游只认标准 chunk，这里统一
//! 成 `data: {...}\n\n` 字符串。

use serde_json::{json, Value};

pub struct CodeArtsStream {
    model: String,
    created: i64,
    done: bool,
    finish_sent: bool,
    last_text: String,
    usage: Option<Value>,
}

impl CodeArtsStream {
    pub fn new(model: &str) -> Self {
        Self {
            model: model.to_string(),
            created: super::now_secs() as i64,
            done: false,
            finish_sent: false,
            last_text: String::new(),
            usage: None,
        }
    }

    pub fn usage(&self) -> Option<Value> {
        self.usage.clone()
    }

    pub fn done(&self) -> bool {
        self.done
    }

    fn chunk(&self, delta: Value, finish: Value) -> String {
        json_line(&json!({
            "id": "chatcmpl-codearts",
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }))
    }

    /// 全文快照 → 增量；重复时吞掉，回退时按 Python 的规则整段重发。
    fn text_delta(&mut self, text: &str) -> Option<String> {
        if text == self.last_text {
            return None;
        }
        let delta = match text.strip_prefix(self.last_text.as_str()) {
            Some(rest) => rest.to_string(),
            None => text.to_string(),
        };
        self.last_text = text.to_string();
        if delta.is_empty() {
            None
        } else {
            Some(self.chunk(json!({"content": delta}), Value::Null))
        }
    }

    /// `_raise_embedded`：HTTP 200 但事件里带业务错误（排队/TPM/模型未注册）。
    fn embedded_error(data: &str) -> Option<super::CodeArtsError> {
        let payload: Value = serde_json::from_str(data).ok()?;
        if !payload.is_object() {
            return None;
        }
        let code = payload.get("error_code").or_else(|| payload.get("code"));
        let empty = match code {
            None | Some(Value::Null) => true,
            Some(Value::String(text)) => text.is_empty() || text == "0",
            Some(Value::Number(number)) => number.as_i64() == Some(0),
            _ => false,
        };
        if empty {
            return None;
        }
        let message = payload
            .get("error_msg")
            .or_else(|| payload.get("message"))
            .and_then(Value::as_str)
            .unwrap_or(data);
        let message: String = message.chars().take(300).collect();
        Some(super::CodeArtsError::Upstream(message))
    }

    /// 喂一行原始 SSE 文本；返回要写给客户端的 chunk（可能为空）。
    pub fn feed(&mut self, line: &str) -> super::Result<Vec<String>> {
        let mut out = Vec::new();
        if self.done {
            return Ok(out);
        }
        let Some(data) = sse_data(line) else { return Ok(out) };
        if data == "[DONE]" {
            self.done = true;
            return Ok(out);
        }
        if let Some(err) = Self::embedded_error(data) {
            return Err(err);
        }
        let Ok(payload) = serde_json::from_str::<Value>(data) else {
            return Ok(out);
        };
        if !payload.is_object() {
            return Ok(out);
        }
        if let Some(usage) = payload.get("usage").filter(|value| value.is_object()) {
            self.usage = Some(usage.clone());
        }
        if let Some(choice) = payload.get("choices").and_then(Value::as_array).and_then(|list| list.first()) {
            let mut delta = choice.get("delta").cloned().unwrap_or(Value::Null);
            if !delta.is_object() {
                delta = match choice.get("message") {
                    Some(message) if message.is_object() => {
                        let mut built = json!({
                            "role": message.get("role").and_then(Value::as_str).unwrap_or("assistant"),
                            "content": message.get("content").cloned().unwrap_or(Value::Null),
                        });
                        if let Some(calls) = message.get("tool_calls").filter(|value| !value.is_null()) {
                            built["tool_calls"] = calls.clone();
                        }
                        built
                    }
                    _ => json!({}),
                };
            }
            let finish = choice.get("finish_reason").cloned().unwrap_or(Value::Null);
            if finish.is_string() {
                self.finish_sent = true;
            }
            out.push(json_line(&json!({
                "id": payload.get("id").cloned().unwrap_or(json!("chatcmpl-codearts")),
                "object": "chat.completion.chunk",
                "created": payload.get("created").and_then(Value::as_i64).unwrap_or(self.created),
                "model": payload
                    .get("model")
                    .and_then(Value::as_str)
                    .map(|value| value.to_string())
                    .unwrap_or_else(|| self.model.clone()),
                "choices": [{
                    "index": choice.get("index").and_then(Value::as_i64).unwrap_or(0),
                    "delta": delta,
                    "finish_reason": finish,
                }],
            })));
            return Ok(out);
        }
        if let Some(delta) = payload.get("delta").filter(|value| value.is_object()) {
            out.push(self.chunk(delta.clone(), Value::Null));
            return Ok(out);
        }
        if let Some(text_value) = payload.get("text") {
            let text = match text_value {
                Value::String(text) => text.clone(),
                other => other.to_string(),
            };
            let trimmed = text.trim();
            if trimmed == "[DONE]" || trimmed.eq_ignore_ascii_case("done") {
                self.done = true;
                return Ok(out);
            }
            if let Some(chunk) = self.text_delta(&text) {
                out.push(chunk);
            }
            return Ok(out);
        }
        if let Some(output) = payload.get("output").and_then(Value::as_array) {
            let text_out: String = output
                .iter()
                .filter(|item| item.get("type").and_then(Value::as_str) == Some("output_text"))
                .map(|item| item.get("text").and_then(Value::as_str).unwrap_or("").to_string())
                .collect();
            if let Some(chunk) = self.text_delta(&text_out) {
                out.push(chunk);
            }
        }
        Ok(out)
    }

    /// 收尾：补 finish_reason、usage 帧、`[DONE]`（与 Python `__iter__` 尾部一致）。
    pub fn tail(&mut self) -> Vec<String> {
        let mut out = Vec::new();
        if !self.finish_sent {
            out.push(self.chunk(json!({}), json!("stop")));
            self.finish_sent = true;
        }
        if let Some(usage) = self.usage.clone() {
            out.push(json_line(&json!({
                "id": "chatcmpl-codearts",
                "object": "chat.completion.chunk",
                "created": self.created,
                "model": self.model,
                "choices": [],
                "usage": usage,
            })));
        }
        out.push("data: [DONE]\n\n".to_string());
        out
    }
}

fn sse_data(line: &str) -> Option<&str> {
    let trimmed = line.trim();
    let payload = trimmed.strip_prefix("data:")?.trim();
    if payload.is_empty() {
        None
    } else {
        Some(payload)
    }
}

fn json_line(value: &Value) -> String {
    format!("data: {}\n\n", serde_json::to_string(value).unwrap_or_default())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn text_snapshots_become_deltas() {
        let mut stream = CodeArtsStream::new("GLM-5.2");
        assert!(stream.feed("event: message").unwrap().is_empty());
        let first = stream.feed(r#"data: {"text":"你好"}"#).unwrap();
        assert_eq!(first.len(), 1);
        assert!(first[0].contains("\"content\":\"你好\""));
        // 同一段再来一次：不重复发。
        assert!(stream.feed(r#"data: {"text":"你好"}"#).unwrap().is_empty());
        // 扩展到「你好世界」：只发增量「世界」。
        let second = stream.feed(r#"data: {"text":"你好世界"}"#).unwrap();
        assert!(second[0].contains("世界"));
        assert!(!second[0].contains("你好"));
    }

    #[test]
    fn standard_choices_pass_through_with_usage_tail() {
        let mut stream = CodeArtsStream::new("GLM-5.2");
        let chunks = stream
            .feed(r#"data: {"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}],"usage":{"total_tokens":7}}"#)
            .unwrap();
        assert!(chunks[0].contains("\"content\":\"hi\""));
        assert_eq!(stream.usage().unwrap()["total_tokens"], 7);
        let tail = stream.tail();
        assert!(tail[0].contains("\"finish_reason\":\"stop\""));
        assert!(tail[1].contains("\"usage\""));
        assert_eq!(tail.last().unwrap(), "data: [DONE]\n\n");
    }

    #[test]
    fn upstream_finish_reason_suppresses_synthetic_stop() {
        let mut stream = CodeArtsStream::new("GLM-5.2");
        stream
            .feed(r#"data: {"choices":[{"index":0,"delta":{"content":"x"},"finish_reason":"length"}]}"#)
            .unwrap();
        let tail = stream.tail();
        assert_eq!(tail.len(), 1, "上游给过 finish_reason，不再补一条");
        assert_eq!(tail[0], "data: [DONE]\n\n");
    }

    #[test]
    fn embedded_business_errors_surface_as_upstream_errors() {
        let mut stream = CodeArtsStream::new("GLM-5.2");
        let err = stream
            .feed(r#"data: {"error_code":"TM.00001041","message":"并发会话数已达上限"}"#)
            .unwrap_err();
        assert!(err.message().contains("并发会话"));
        assert_eq!(err.status(), 502);
    }

    #[test]
    fn done_marker_and_output_arrays_are_handled() {
        let mut stream = CodeArtsStream::new("GLM-5.2");
        let chunks = stream.feed(r#"data: {"output":[{"type":"output_text","text":"结果"}]}"#).unwrap();
        assert!(chunks[0].contains("结果"));
        stream.feed("data: [DONE]").unwrap();
        assert!(stream.done());
        assert!(stream.feed(r#"data: {"text":"之后的不该再发"}"#).unwrap().is_empty());
    }
}
