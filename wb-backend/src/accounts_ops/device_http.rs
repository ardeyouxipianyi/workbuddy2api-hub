//! Retry policy shared by device-flow upstream calls.

use std::time::Duration;

use serde_json::Value;

pub(super) async fn send_json(
    request: reqwest::RequestBuilder,
    timeout: Duration,
    attempts: usize,
) -> Result<Value, String> {
    let attempts = attempts.max(1);
    let mut last = "upstream request failed".to_string();
    for attempt in 1..=attempts {
        let Some(current) = request.try_clone() else {
            return Err("upstream request cannot be retried".into());
        };
        match current.timeout(timeout).send().await {
            Ok(response) if response.status().is_success() => {
                return response
                    .json::<Value>()
                    .await
                    .map_err(|exc| exc.to_string())
            }
            Ok(response) => {
                let status = response.status();
                let body = response.text().await.unwrap_or_default();
                last = format!(
                    "HTTP {}: {}",
                    status.as_u16(),
                    body.chars().take(200).collect::<String>()
                );
                if attempt == attempts || !retryable(status.as_u16()) {
                    return Err(last);
                }
            }
            Err(exc) => {
                last = exc.to_string();
                if attempt == attempts {
                    return Err(last);
                }
            }
        }
        tokio::time::sleep(backoff(attempt)).await;
    }
    Err(last)
}

fn retryable(status: u16) -> bool {
    status == 408 || status == 429 || status >= 500
}

fn backoff(attempt: usize) -> Duration {
    let millis = std::env::var("WB_ACCOUNT_UPSTREAM_RETRY_MS")
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .unwrap_or(250);
    Duration::from_millis(millis.saturating_mul(attempt as u64))
}
