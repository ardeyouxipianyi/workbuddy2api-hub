//! Minimal ACP-over-streamable-HTTP transport used by the web daily activity.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Instant;

use serde_json::{json, Value};

pub(super) async fn run_turn(
    client: &reqwest::Client,
    link: &str,
    token: &str,
    session_id: &str,
    cwd: &str,
    prompt: &str,
    model: &str,
    uid: &str,
    wait_seconds: u64,
    conversation_url: &str,
) -> Value {
    let started = Instant::now();
    let channel = match Channel::open(client, link, token).await {
        Ok(channel) => channel,
        Err(error) => return result(false, "", 0, 0, started, &error),
    };
    if let Err(error) = initialize(&channel, session_id, cwd, prompt, model).await {
        channel.close();
        return result(
            false,
            "",
            channel.events(),
            channel.chunks(),
            started,
            &error,
        );
    }
    let (status, error) =
        wait_for_completion(client, token, uid, conversation_url, wait_seconds).await;
    let finished = status == "completed" && error.is_empty();
    channel.close();
    let fallback = if finished {
        ""
    } else if error.is_empty() {
        "会话没有跑完"
    } else {
        &error
    };
    result(
        finished,
        &status,
        channel.events(),
        channel.chunks(),
        started,
        fallback,
    )
}

async fn initialize(
    channel: &Channel,
    session_id: &str,
    cwd: &str,
    prompt: &str,
    model: &str,
) -> Result<(), String> {
    channel
        .request(
            "initialize",
            json!({
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": false, "writeTextFile": false},
                    "terminal": false
                }
            }),
            1,
        )
        .await?;
    channel
        .request(
            "session/load",
            json!({"sessionId": session_id, "cwd": cwd, "mcpServers": []}),
            2,
        )
        .await?;
    let _ = channel
        .request(
            "session/set_model",
            json!({"sessionId": session_id, "modelId": model}),
            3,
        )
        .await;
    channel
        .request(
            "session/prompt",
            json!({"sessionId": session_id, "prompt": [{"type": "text", "text": prompt}]}),
            4,
        )
        .await
}

async fn wait_for_completion(
    client: &reqwest::Client,
    token: &str,
    uid: &str,
    url: &str,
    wait_seconds: u64,
) -> (String, String) {
    let interval_ms = std::env::var("WB_WEB_POLL_INTERVAL_MS")
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .unwrap_or(3000)
        .max(10);
    let deadline = Instant::now() + std::time::Duration::from_secs(wait_seconds.max(1));
    loop {
        let status = status(client, token, uid, url).await;
        if status == "completed" {
            return (status, String::new());
        }
        if matches!(status.as_str(), "failed" | "error") {
            return (status.clone(), format!("会话状态={status}"));
        }
        if Instant::now() >= deadline {
            return (status, format!("会话在 {wait_seconds}s 内没有跑完"));
        }
        tokio::time::sleep(std::time::Duration::from_millis(interval_ms)).await;
    }
}

async fn status(client: &reqwest::Client, token: &str, uid: &str, url: &str) -> String {
    let origin = super::config::web_origin("intl");
    let response = client
        .get(url)
        .bearer_auth(token)
        .header("Accept", "application/json, text/plain, */*")
        .header("X-User-Id", uid)
        .header("Origin", &origin)
        .header("Referer", format!("{origin}/app"))
        .header("User-Agent", super::config::web_user_agent())
        .send()
        .await;
    let Ok(response) = response else {
        return String::new();
    };
    let Ok(payload) = response.json::<Value>().await else {
        return String::new();
    };
    payload
        .pointer("/data/status")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string()
}

struct Channel {
    client: reqwest::Client,
    link: String,
    token: String,
    connection_id: String,
    events: Arc<AtomicU64>,
    chunks: Arc<AtomicU64>,
    reader: tokio::task::JoinHandle<()>,
}

impl Channel {
    async fn open(client: &reqwest::Client, link: &str, token: &str) -> Result<Self, String> {
        let response = client
            .get(link)
            .bearer_auth(token)
            .header("Accept", "text/event-stream")
            .header("User-Agent", super::config::web_user_agent())
            .send()
            .await
            .map_err(|exc| exc.to_string())?;
        if !response.status().is_success() {
            return Err(format!("SSE 通道返回 HTTP {}", response.status()));
        }
        let connection_id = response
            .headers()
            .get("acp-connection-id")
            .and_then(|value| value.to_str().ok())
            .unwrap_or("")
            .to_string();
        if connection_id.is_empty() {
            return Err("SSE 通道没有返回 Acp-Connection-Id".into());
        }
        let events = Arc::new(AtomicU64::new(0));
        let chunks = Arc::new(AtomicU64::new(0));
        let reader = tokio::spawn(read_events(response, events.clone(), chunks.clone()));
        Ok(Self {
            client: client.clone(),
            link: link.to_string(),
            token: token.to_string(),
            connection_id,
            events,
            chunks,
            reader,
        })
    }

    async fn request(&self, method: &str, params: Value, id: u64) -> Result<(), String> {
        let response = self
            .client
            .post(&self.link)
            .bearer_auth(&self.token)
            .header("Acp-Connection-Id", &self.connection_id)
            .header("Accept", "application/json, text/event-stream")
            .header("User-Agent", super::config::web_user_agent())
            .json(&json!({
                "jsonrpc": "2.0",
                "id": id,
                "method": method,
                "params": params
            }))
            .send()
            .await
            .map_err(|exc| exc.to_string())?;
        if matches!(response.status().as_u16(), 200 | 202) {
            Ok(())
        } else {
            Err(format!("{method} 返回 HTTP {}", response.status()))
        }
    }

    fn events(&self) -> u64 {
        self.events.load(Ordering::Relaxed)
    }

    fn chunks(&self) -> u64 {
        self.chunks.load(Ordering::Relaxed)
    }

    fn close(&self) {
        self.reader.abort();
    }
}

async fn read_events(response: reqwest::Response, events: Arc<AtomicU64>, chunks: Arc<AtomicU64>) {
    let mut stream = response.bytes_stream();
    let mut buffer = String::new();
    while let Some(Ok(bytes)) = tokio_stream::StreamExt::next(&mut stream).await {
        buffer.push_str(&String::from_utf8_lossy(&bytes));
        while let Some(end) = buffer.find("\n\n") {
            let event: String = buffer.drain(..end + 2).collect();
            for line in event.lines() {
                let Some(raw) = line.strip_prefix("data:") else {
                    continue;
                };
                let Ok(payload) = serde_json::from_str::<Value>(raw.trim()) else {
                    continue;
                };
                if payload.get("method").and_then(Value::as_str) == Some("session/update") {
                    events.fetch_add(1, Ordering::Relaxed);
                    if payload
                        .pointer("/params/update/sessionUpdate")
                        .and_then(Value::as_str)
                        == Some("agent_message_chunk")
                    {
                        chunks.fetch_add(1, Ordering::Relaxed);
                    }
                }
            }
        }
    }
}

fn result(
    ok: bool,
    status: &str,
    events: u64,
    chunks: u64,
    started: Instant,
    error: &str,
) -> Value {
    json!({
        "ok": ok,
        "status": status,
        "events": events,
        "chunks": chunks,
        "elapsed_ms": started.elapsed().as_millis() as u64,
        "error": error
    })
}
