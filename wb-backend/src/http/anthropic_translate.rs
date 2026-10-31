//! Anthropic Messages request and response translation.

use serde_json::{json, Map, Value};

use super::anthropic_content as content;

fn base_fields(chat: &mut Map<String, Value>, payload: &Value, realm: &str, model: &str) {
    chat.insert("model".into(), json!(model));
    for key in ["temperature", "top_p"] {
        if let Some(value) = payload.get(key).filter(|v| !v.is_null()) {
            chat.insert(key.into(), value.clone());
        }
    }
    if let Some(max_tokens) = payload.get("max_tokens").filter(|v| !v.is_null()) {
        chat.insert("max_tokens".into(), max_tokens.clone());
    } else if let Some(default) = crate::models::lookup(realm, model)
        .and_then(|entry| entry.get("max_output_tokens").and_then(Value::as_i64))
        .filter(|value| *value > 0)
    {
        chat.insert("max_tokens".into(), json!(default));
    }
    if let Some(stream) = payload.get("stream") {
        chat.insert("stream".into(), json!(stream.as_bool().unwrap_or(false)));
    }
    if let Some(stops) = payload
        .get("stop_sequences")
        .and_then(Value::as_array)
        .filter(|v| !v.is_empty())
    {
        chat.insert("stop".into(), Value::Array(stops.clone()));
    }
    if let Some(user) = payload
        .pointer("/metadata/user_id")
        .filter(|v| !v.is_null())
    {
        chat.insert("user".into(), user.clone());
    }
}

fn tool_fields(chat: &mut Map<String, Value>, payload: &Value, tools: Vec<Value>) {
    if !tools.is_empty() {
        chat.insert("tools".into(), Value::Array(tools));
    }
    if let Some(choice) = content::tool_choice(payload.get("tool_choice")) {
        chat.insert("tool_choice".into(), choice);
    }
    if payload
        .pointer("/tool_choice/disable_parallel_tool_use")
        .and_then(Value::as_bool)
        == Some(true)
    {
        chat.insert("parallel_tool_calls".into(), json!(false));
    }
    if let Some(effort) = content::effort(payload) {
        chat.insert("reasoning_effort".into(), json!(effort));
    }
}

fn system_text(payload: &Value, skipped: &[String]) -> String {
    let system = match payload.get("system") {
        Some(Value::String(value)) => value.clone(),
        Some(Value::Array(blocks)) => blocks
            .iter()
            .filter_map(|block| block.get("text").and_then(Value::as_str))
            .filter(|value| !value.is_empty())
            .collect::<Vec<_>>()
            .join("\n"),
        _ => String::new(),
    };
    if skipped.is_empty() {
        return system;
    }
    let note = format!(
        "These Anthropic server tools are not available here: {}. Do not call them.",
        skipped.join(", ")
    );
    if system.is_empty() {
        note
    } else {
        format!("{system}\n{note}")
    }
}

fn conversation_messages(payload: &Value, system: String) -> Result<Vec<Value>, String> {
    let messages_in = payload
        .get("messages")
        .and_then(Value::as_array)
        .ok_or("messages must be a list")?;
    let mut messages = Vec::new();
    if !system.is_empty() {
        messages.push(json!({"role": "system", "content": system}));
    }
    for (index, item) in messages_in.iter().enumerate() {
        let role = item
            .get("role")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        let role = if role.is_empty() { "user" } else { role };
        if matches!(role, "system" | "developer") {
            let value = content::content_to_text(item.get("content"));
            if !value.is_empty() {
                messages.push(json!({"role": "system", "content": value}));
            }
            continue;
        }
        if !matches!(role, "user" | "assistant") {
            return Err(format!(
                "messages[{index}].role {role:?} must be user or assistant; \
                 pass a system prompt in the top-level system field"
            ));
        }
        match item.get("content") {
            Some(Value::String(value)) => messages.push(json!({"role": role, "content": value})),
            Some(Value::Array(blocks)) => {
                messages.extend(content::blocks_to_messages(role, blocks))
            }
            Some(value) if !value.is_null() => {
                let value = content::text(Some(value));
                if !value.is_empty() {
                    messages.push(json!({"role": role, "content": value}));
                }
            }
            _ => {}
        }
    }
    Ok(messages)
}

pub fn to_chat(payload: &Value, realm: &str) -> Result<Value, String> {
    let model = payload
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if model.is_empty() {
        return Err("model is required".into());
    }
    if payload
        .get("messages")
        .and_then(Value::as_array)
        .map(Vec::is_empty)
        .unwrap_or(false)
    {
        return Err("messages must not be empty".into());
    }
    let mut chat = Map::new();
    base_fields(&mut chat, payload, realm, model);
    let (tools, skipped) = content::tools_to_chat(payload.get("tools"));
    tool_fields(&mut chat, payload, tools);
    let system = system_text(payload, &skipped);
    chat.insert(
        "messages".into(),
        Value::Array(conversation_messages(payload, system)?),
    );
    Ok(Value::Object(chat))
}

fn int(value: Option<&Value>) -> i64 {
    match value {
        Some(Value::Number(value)) => value
            .as_i64()
            .or_else(|| value.as_f64().map(|v| v as i64))
            .unwrap_or(0),
        Some(Value::String(value)) => value.parse().unwrap_or(0),
        _ => 0,
    }
}

pub fn usage(usage: Option<&Value>) -> Value {
    let Some(usage) = usage else {
        return json!({
            "input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 0,
        });
    };
    let prompt = int(usage.get("prompt_tokens"));
    let cache_read = match usage.get("prompt_cache_hit_tokens") {
        Some(Value::Null) | None => crate::record::best_cached_tokens(usage),
        Some(value) => int(Some(value)),
    };
    let cache_creation = int(usage.get("prompt_cache_write_tokens"));
    let mut out = Map::new();
    out.insert(
        "input_tokens".into(),
        json!((prompt - cache_read - cache_creation).max(0)),
    );
    out.insert("cache_creation_input_tokens".into(), json!(cache_creation));
    out.insert("cache_read_input_tokens".into(), json!(cache_read));
    out.insert(
        "output_tokens".into(),
        json!(int(usage.get("completion_tokens"))),
    );
    if let Some(tier) = usage.get("service_tier").filter(|value| match value {
        Value::Null => false,
        Value::String(value) => !value.is_empty(),
        _ => true,
    }) {
        out.insert("service_tier".into(), tier.clone());
    }
    Value::Object(out)
}

fn stop_reason(reason: Option<&str>) -> &'static str {
    match reason.unwrap_or("").trim().to_ascii_lowercase().as_str() {
        "length" => "max_tokens",
        "tool_calls" | "function_call" => "tool_use",
        "content_filter" | "refusal" => "refusal",
        _ => "end_turn",
    }
}

fn parse_tool_input(arguments: Option<&Value>) -> Value {
    match arguments {
        Some(Value::Object(_)) => arguments.unwrap().clone(),
        Some(Value::String(value)) if value.is_empty() => json!({}),
        Some(Value::String(value)) => {
            serde_json::from_str(value).unwrap_or_else(|_| json!({"raw": value}))
        }
        Some(value) => json!({"value": value}),
        None => json!({}),
    }
}

pub fn chat_to_messages(chat: &Value, model_hint: &str) -> Value {
    let choice = chat.pointer("/choices/0").unwrap_or(&Value::Null);
    let message = choice.get("message").unwrap_or(&Value::Null);
    let mut blocks = Vec::new();
    let reasoning = message
        .get("reasoning_content")
        .and_then(Value::as_str)
        .unwrap_or("");
    if !reasoning.is_empty() {
        blocks.push(json!({"type": "thinking", "thinking": reasoning}));
    }
    let body = content::content_to_text(message.get("content"));
    if !body.is_empty() {
        blocks.push(json!({"type": "text", "text": body}));
    }
    for call in message
        .get("tool_calls")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let function = call.get("function").unwrap_or(&Value::Null);
        blocks.push(json!({
            "type": "tool_use",
            "id": call.get("id").and_then(Value::as_str)
                .filter(|v| !v.is_empty()).map(str::to_string)
                .unwrap_or_else(|| format!("toolu_{}", crate::sse::hex_token(16))),
            "name": function.get("name").and_then(Value::as_str).unwrap_or(""),
            "input": parse_tool_input(function.get("arguments")),
        }));
    }
    let model = chat
        .get("model")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .unwrap_or(model_hint);
    let id = chat.get("id").and_then(Value::as_str).unwrap_or("");
    let id = if id.is_empty() {
        format!("msg_{}", crate::sse::hex_token(32))
    } else if id.starts_with("msg_") {
        id.to_string()
    } else {
        format!("msg_{id}")
    };
    json!({
        "id": id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": stop_reason(choice.get("finish_reason").and_then(Value::as_str)),
        "stop_sequence": null,
        "usage": usage(chat.get("usage")),
    })
}

fn estimate_tokens(value: &str) -> i64 {
    let cjk = value
        .chars()
        .filter(|c| matches!(*c as u32, 0x4e00..=0x9fff | 0x3400..=0x4dbf))
        .count() as i64;
    let other = value.chars().count() as i64 - cjk;
    cjk + if other == 0 {
        0
    } else {
        (other as f64 / 3.6).floor().max(1.0) as i64
    }
}

pub fn count_chat_tokens(chat: &Value) -> i64 {
    let mut values = Vec::new();
    for message in chat
        .get("messages")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        values.push(content::json_text(message.get("content")));
        for call in message
            .get("tool_calls")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            values.push(content::json_text(Some(call)));
        }
    }
    for tool in chat
        .get("tools")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        values.push(content::json_text(Some(tool)));
    }
    values
        .iter()
        .filter(|value| !value.is_empty())
        .map(|value| estimate_tokens(value))
        .sum()
}
