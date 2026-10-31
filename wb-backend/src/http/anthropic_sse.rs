//! State machine that maps chat-completion chunks to Anthropic SSE payloads.

use std::collections::HashMap;

use serde_json::{json, Value};

pub(super) fn event(event: &str, mut payload: Value) -> String {
    if let Some(map) = payload.as_object_mut() {
        map.insert("type".into(), json!(event));
    }
    format!("event: {event}\ndata: {payload}\n\n")
}

struct Tool {
    block: i64,
}

pub(super) struct StreamState {
    started: bool,
    id: String,
    model: String,
    stop: &'static str,
    next: i64,
    thinking: Option<i64>,
    text: Option<i64>,
    tools: HashMap<i64, Tool>,
    tool_order: Vec<i64>,
}

impl StreamState {
    pub(super) fn new(model: &str) -> StreamState {
        StreamState {
            started: false,
            id: String::new(),
            model: model.to_string(),
            stop: "end_turn",
            next: 0,
            thinking: None,
            text: None,
            tools: HashMap::new(),
            tool_order: Vec::new(),
        }
    }

    fn message_id(&self) -> String {
        if self.id.is_empty() {
            format!("msg_{}", crate::sse::hex_token(32))
        } else if self.id.starts_with("msg_") {
            self.id.clone()
        } else {
            format!("msg_{}", self.id)
        }
    }

    pub(super) fn start(&mut self, chunk: Option<&Value>) -> Option<String> {
        if self.started {
            return None;
        }
        self.started = true;
        if let Some(chunk) = chunk {
            self.id = chunk
                .get("id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            if let Some(model) = chunk
                .get("model")
                .and_then(Value::as_str)
                .filter(|v| !v.is_empty())
            {
                self.model = model.to_string();
            }
        }
        Some(event(
            "message_start",
            json!({
                "message": {
                    "id": self.message_id(),
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": self.model,
                    "stop_reason": null,
                    "stop_sequence": null,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }
            }),
        ))
    }

    fn open_thinking(&mut self) -> String {
        let index = self.next;
        self.next += 1;
        self.thinking = Some(index);
        event(
            "content_block_start",
            json!({
                "index": index,
                "content_block": {"type": "thinking", "thinking": ""},
            }),
        )
    }

    fn open_text(&mut self, out: &mut String) {
        if let Some(index) = self.thinking.take() {
            out.push_str(&event("content_block_stop", json!({"index": index})));
        }
        let index = self.next;
        self.next += 1;
        self.text = Some(index);
        out.push_str(&event(
            "content_block_start",
            json!({
                "index": index,
                "content_block": {"type": "text", "text": ""},
            }),
        ));
    }

    fn open_tool(&mut self, index: i64, call: &Value, out: &mut String) -> i64 {
        if let Some(index) = self.thinking.take() {
            out.push_str(&event("content_block_stop", json!({"index": index})));
        }
        if let Some(index) = self.text.take() {
            out.push_str(&event("content_block_stop", json!({"index": index})));
        }
        if let Some(tool) = self.tools.get(&index) {
            return tool.block;
        }
        let block = self.next;
        self.next += 1;
        let function = call.get("function").unwrap_or(&Value::Null);
        let id = call
            .get("id")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .map(str::to_string)
            .unwrap_or_else(|| format!("toolu_{index}"));
        out.push_str(&event(
            "content_block_start",
            json!({
                "index": block,
                "content_block": {
                    "type": "tool_use",
                    "id": id,
                    "name": function.get("name").and_then(Value::as_str).unwrap_or(""),
                    "input": {},
                }
            }),
        ));
        self.tool_order.push(index);
        self.tools.insert(index, Tool { block });
        block
    }

    pub(super) fn finish_blocks(&mut self) -> String {
        let mut out = String::new();
        if let Some(index) = self.thinking.take() {
            out.push_str(&event("content_block_stop", json!({"index": index})));
        }
        if let Some(index) = self.text.take() {
            out.push_str(&event("content_block_stop", json!({"index": index})));
        }
        for index in &self.tool_order {
            if let Some(tool) = self.tools.get(index) {
                out.push_str(&event("content_block_stop", json!({"index": tool.block})));
            }
        }
        out
    }

    pub(super) fn started(&self) -> bool {
        self.started
    }

    pub(super) fn stop(&self) -> &'static str {
        self.stop
    }
}

fn push_reasoning(state: &mut StreamState, delta: &Value, out: &mut String) {
    if let Some(reasoning) = delta
        .get("reasoning_content")
        .and_then(Value::as_str)
        .filter(|v| !v.is_empty())
    {
        if state.thinking.is_none() {
            out.push_str(&state.open_thinking());
        }
        out.push_str(&event(
            "content_block_delta",
            json!({
                "index": state.thinking.unwrap(),
                "delta": {"type": "thinking_delta", "thinking": reasoning},
            }),
        ));
    }
}

fn push_text(state: &mut StreamState, delta: &Value, out: &mut String) {
    if let Some(text) = delta
        .get("content")
        .and_then(Value::as_str)
        .filter(|v| !v.is_empty())
    {
        if state.text.is_none() {
            state.open_text(out);
        }
        out.push_str(&event(
            "content_block_delta",
            json!({
                "index": state.text.unwrap(),
                "delta": {"type": "text_delta", "text": text},
            }),
        ));
    }
}

fn push_tools(state: &mut StreamState, delta: &Value, out: &mut String) {
    for call in delta
        .get("tool_calls")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let index = call.get("index").and_then(Value::as_i64).unwrap_or(0);
        let block = state.open_tool(index, call, out);
        let arguments = call
            .pointer("/function/arguments")
            .and_then(Value::as_str)
            .unwrap_or("");
        if !arguments.is_empty() {
            out.push_str(&event(
                "content_block_delta",
                json!({
                    "index": block,
                    "delta": {"type": "input_json_delta", "partial_json": arguments},
                }),
            ));
        }
    }
}

pub(super) fn process_frame(
    state: &mut StreamState,
    aggregate: &mut crate::sse::Aggregated,
    chunk: &Value,
) -> Result<String, String> {
    if let Some(error) = chunk.get("error") {
        return Err(error
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or("upstream error")
            .to_string());
    }
    let mut out = state.start(Some(chunk)).unwrap_or_default();
    crate::sse::absorb(aggregate, chunk);
    let choice = chunk.pointer("/choices/0").unwrap_or(&Value::Null);
    let delta = choice.get("delta").unwrap_or(&Value::Null);
    push_reasoning(state, delta, &mut out);
    push_text(state, delta, &mut out);
    push_tools(state, delta, &mut out);
    if let Some(reason) = choice.get("finish_reason").and_then(Value::as_str) {
        state.stop = match reason.to_ascii_lowercase().as_str() {
            "length" => "max_tokens",
            "tool_calls" | "function_call" => "tool_use",
            "content_filter" | "refusal" => "refusal",
            _ => "end_turn",
        };
    }
    Ok(out)
}
