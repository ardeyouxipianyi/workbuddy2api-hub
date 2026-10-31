//! Anthropic content blocks and tool definitions converted for Chat Completions.

use serde_json::{json, Map, Value};

pub(super) fn text(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(value)) => value.clone(),
        Some(Value::Number(value)) => value.to_string(),
        Some(Value::Bool(value)) => value.to_string(),
        _ => String::new(),
    }
}

pub(super) fn json_text(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => "{}".into(),
        Some(Value::String(value)) => value.clone(),
        Some(value) => serde_json::to_string(value).unwrap_or_else(|_| "{}".into()),
    }
}

fn image_part(block: &Value) -> Value {
    let source = block.get("source").and_then(Value::as_object);
    let url = source
        .map(|source| match source.get("type").and_then(Value::as_str) {
            Some("base64") => format!(
                "data:{};base64,{}",
                source
                    .get("media_type")
                    .and_then(Value::as_str)
                    .unwrap_or("image/png"),
                text(source.get("data"))
            ),
            Some("url") => text(source.get("url")),
            Some("file") => {
                let file_id = text(source.get("file_id"));
                if file_id.is_empty() {
                    text(source.get("url"))
                } else {
                    file_id
                }
            }
            _ => String::new(),
        })
        .unwrap_or_default();
    json!({"type": "image_url", "image_url": {"url": url}})
}

fn document_text(block: &Value) -> String {
    let title = text(block.get("title"));
    let source = block.get("source").and_then(Value::as_object);
    let body = source
        .map(|source| match source.get("type").and_then(Value::as_str) {
            Some("text") => text(source.get("data")),
            Some("content") => content_to_text(source.get("content")),
            Some("base64") => format!(
                "[document {} omitted]",
                source
                    .get("media_type")
                    .and_then(Value::as_str)
                    .unwrap_or("application/octet-stream")
            ),
            Some("url") => text(source.get("url")),
            Some("file") => {
                let file_id = text(source.get("file_id"));
                if file_id.is_empty() {
                    text(source.get("url"))
                } else {
                    file_id
                }
            }
            _ => String::new(),
        })
        .unwrap_or_default();
    if title.is_empty() {
        body
    } else {
        format!("{title}\n{body}")
    }
}

fn block_text(block: &Value) -> String {
    if let Some(value) = block.get("text") {
        text(Some(value))
    } else if block.get("content").is_some() {
        content_to_text(block.get("content"))
    } else {
        String::new()
    }
}

pub(super) fn content_to_text(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(value)) => value.clone(),
        Some(Value::Array(parts)) => parts
            .iter()
            .map(|part| match part.get("type").and_then(Value::as_str) {
                Some("image") => image_part(part)
                    .pointer("/image_url/url")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
                Some("document") => document_text(part),
                _ => block_text(part),
            })
            .collect(),
        Some(Value::Object(block)) => block
            .get("text")
            .map(|value| text(Some(value)))
            .or_else(|| {
                block
                    .get("content")
                    .map(|value| content_to_text(Some(value)))
            })
            .unwrap_or_default(),
        Some(other) => text(Some(other)),
        None => String::new(),
    }
}

fn tool_result_content(value: Option<&Value>, is_error: bool) -> Value {
    let Some(Value::Array(blocks)) = value else {
        let content = content_to_text(value);
        return json!(if is_error {
            format!("error: {content}")
        } else {
            content
        });
    };
    let mut parts = Vec::new();
    let mut has_image = false;
    for block in blocks {
        if let Some(value) = block.as_str() {
            parts.push(json!({"type": "text", "text": value}));
            continue;
        }
        match block.get("type").and_then(Value::as_str) {
            Some("image") => {
                let part = image_part(block);
                if part
                    .pointer("/image_url/url")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .is_empty()
                {
                    parts.push(json!({"type": "text", "text": "[image omitted: no data]"}));
                } else {
                    has_image = true;
                    parts.push(part);
                }
            }
            Some("document") => parts.push(json!({"type": "text", "text": document_text(block)})),
            _ => {
                let value = block_text(block);
                if !value.is_empty() {
                    parts.push(json!({"type": "text", "text": value}));
                }
            }
        }
    }
    if !has_image {
        let joined = parts
            .iter()
            .filter(|part| part.get("type").and_then(Value::as_str) == Some("text"))
            .filter_map(|part| part.get("text").and_then(Value::as_str))
            .collect::<String>();
        return json!(if is_error {
            format!("error: {joined}")
        } else {
            joined
        });
    }
    if is_error {
        parts.insert(0, json!({"type": "text", "text": "error:"}));
    }
    Value::Array(parts)
}

#[derive(Default)]
struct BlockParts {
    calls: Vec<Value>,
    tools: Vec<Value>,
    texts: Vec<String>,
    content: Vec<Value>,
    multimodal: bool,
}

impl BlockParts {
    fn push(&mut self, block: &Value) {
        match block.get("type").and_then(Value::as_str) {
            Some("thinking" | "redacted_thinking") => {}
            Some("tool_use" | "server_tool_use") => self.calls.push(json!({
                "id": text(block.get("id")),
                "type": "function",
                "function": {
                    "name": text(block.get("name")),
                    "arguments": json_text(block.get("input")),
                }
            })),
            Some("tool_result") => self.tools.push(json!({
                "role": "tool",
                "tool_call_id": text(block.get("tool_use_id")),
                "content": tool_result_content(
                    block.get("content"),
                    block.get("is_error").and_then(Value::as_bool).unwrap_or(false),
                ),
            })),
            Some("image") => {
                let part = image_part(block);
                if !part
                    .pointer("/image_url/url")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .is_empty()
                {
                    self.multimodal = true;
                    self.content.push(part);
                }
            }
            Some("document") => {
                self.multimodal = true;
                self.content
                    .push(json!({"type": "text", "text": document_text(block)}));
            }
            _ => {
                let value = block_text(block);
                self.texts.push(value.clone());
                self.content.push(json!({"type": "text", "text": value}));
            }
        }
    }

    fn into_messages(self, role: &str) -> Vec<Value> {
        let mut out = self.tools;
        if self.calls.is_empty() && self.texts.is_empty() && !self.multimodal {
            return out;
        }
        let mut message = Map::new();
        message.insert("role".into(), json!(role));
        if !self.calls.is_empty() {
            message.insert("tool_calls".into(), Value::Array(self.calls));
        }
        if self.multimodal {
            message.insert("content".into(), Value::Array(self.content));
        } else {
            message.insert("content".into(), json!(self.texts.concat()));
        }
        out.push(Value::Object(message));
        out
    }
}

pub(super) fn blocks_to_messages(role: &str, blocks: &[Value]) -> Vec<Value> {
    let mut parts = BlockParts::default();
    for block in blocks {
        parts.push(block);
    }
    parts.into_messages(role)
}

pub(super) fn tools_to_chat(value: Option<&Value>) -> (Vec<Value>, Vec<String>) {
    let mut tools = Vec::new();
    let mut skipped = Vec::new();
    for tool in value.and_then(Value::as_array).into_iter().flatten() {
        if tool.get("function").and_then(Value::as_object).is_some() {
            tools.push(tool.clone());
            continue;
        }
        let name = {
            let name = text(tool.get("name"));
            if name.is_empty() {
                text(tool.get("type"))
            } else {
                name
            }
        };
        let schema = tool.get("input_schema").or_else(|| tool.get("parameters"));
        let Some(schema) = schema else {
            if !name.is_empty() {
                skipped.push(name);
            }
            continue;
        };
        let mut function = Map::new();
        function.insert("name".into(), json!(name));
        if let Some(description) = tool.get("description") {
            function.insert("description".into(), description.clone());
        }
        function.insert("parameters".into(), schema.clone());
        tools.push(json!({"type": "function", "function": function}));
    }
    (tools, skipped)
}

pub(super) fn tool_choice(value: Option<&Value>) -> Option<Value> {
    match value? {
        Value::String(value) => {
            let value = value.trim().to_ascii_lowercase();
            Some(json!(match value.as_str() {
                "any" => "required",
                other => other,
            }))
        }
        Value::Object(value) => match value.get("type").and_then(Value::as_str) {
            Some("auto") => Some(json!("auto")),
            Some("any") => Some(json!("required")),
            Some("none") => Some(json!("none")),
            Some("tool") => Some(json!({
                "type": "function",
                "function": {"name": text(value.get("name"))}
            })),
            _ => None,
        },
        _ => None,
    }
}

pub(super) fn effort(payload: &Value) -> Option<String> {
    let from_output = payload
        .pointer("/output_config/effort")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty());
    if let Some(value) = from_output {
        return Some(value.to_string());
    }
    let thinking = payload.get("thinking")?.as_object()?;
    match thinking
        .get("type")
        .and_then(Value::as_str)?
        .to_ascii_lowercase()
        .as_str()
    {
        "disabled" => Some("none".into()),
        "enabled" => {
            let budget = thinking
                .get("budget_tokens")
                .and_then(Value::as_i64)
                .unwrap_or(0);
            Some(
                match budget {
                    i64::MIN..=0 => return None,
                    32000.. => "xhigh",
                    16000.. => "high",
                    8000.. => "medium",
                    _ => "low",
                }
                .into(),
            )
        }
        _ => None,
    }
}
