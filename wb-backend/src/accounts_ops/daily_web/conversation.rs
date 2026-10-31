//! Web-console conversation create/session/release calls.

use serde_json::{json, Value};

use super::super::{config, util};
use crate::state::AccountFile;

pub(super) struct CreateError {
    pub(super) message: String,
    pub(super) retryable: bool,
}

pub(super) async fn create(
    client: &reqwest::Client,
    account: &AccountFile,
    prompt: &str,
    model: &str,
) -> Result<String, CreateError> {
    let response = client
        .post(conversations_url())
        .headers(web_headers(account))
        .json(&json!({
            "prompt": prompt,
            "model": model,
            "conversationOrigin": "workbuddy-app",
            "plugins": [{"name": "weixinpay", "marketplace": "codebuddy-builtin"}]
        }))
        .timeout(config::timeout("WB_WEB_REQUEST_TIMEOUT_SECONDS", 30))
        .send()
        .await;
    let response = match response {
        Ok(response) => response,
        Err(exc) => {
            return Err(CreateError {
                message: exc.to_string(),
                retryable: true,
            })
        }
    };
    let status = response.status();
    let payload = response.json::<Value>().await.unwrap_or_else(|_| json!({}));
    let conversation = payload
        .pointer("/data/id")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    if created(status.is_success(), &payload, &conversation) {
        return Ok(conversation);
    }
    Err(CreateError {
        message: create_error(&status, &payload),
        retryable: status.is_server_error() || matches!(status.as_u16(), 408 | 429),
    })
}

fn created(success: bool, payload: &Value, conversation: &str) -> bool {
    success
        && payload
            .get("code")
            .and_then(Value::as_i64)
            .map(|code| code == 0)
            .unwrap_or(true)
        && !conversation.is_empty()
}

fn create_error(status: &reqwest::StatusCode, payload: &Value) -> String {
    format!(
        "HTTP {}: code={} msg={}",
        status.as_u16(),
        payload.get("code").and_then(Value::as_i64).unwrap_or(-1),
        payload
            .get("msg")
            .and_then(Value::as_str)
            .unwrap_or("unexpected response")
    )
}

pub(super) async fn session(
    client: &reqwest::Client,
    account: &AccountFile,
    conversation: &str,
) -> Result<Value, String> {
    let payload = client
        .get(format!("{}/session", url(conversation)))
        .headers(web_headers(account))
        .timeout(config::timeout("WB_WEB_REQUEST_TIMEOUT_SECONDS", 30))
        .send()
        .await
        .map_err(|exc| exc.to_string())?
        .json::<Value>()
        .await
        .map_err(|exc| exc.to_string())?;
    Ok(payload.get("data").cloned().unwrap_or_else(|| json!({})))
}

pub(super) async fn release(
    client: &reqwest::Client,
    account: &AccountFile,
    conversation: &str,
    delete: bool,
) {
    let base = url(conversation);
    let _ = client
        .post(format!("{base}/stop"))
        .headers(web_headers(account))
        .json(&json!({}))
        .timeout(config::timeout("WB_WEB_RELEASE_TIMEOUT_SECONDS", 20))
        .send()
        .await;
    if delete {
        let _ = client
            .post(format!("{base}/delete"))
            .headers(web_headers(account))
            .json(&json!({}))
            .timeout(config::timeout("WB_WEB_RELEASE_TIMEOUT_SECONDS", 20))
            .send()
            .await;
    }
}

fn web_headers(account: &AccountFile) -> reqwest::header::HeaderMap {
    use reqwest::header::{
        HeaderMap, HeaderValue, ACCEPT, AUTHORIZATION, CONTENT_TYPE, ORIGIN, REFERER, USER_AGENT,
    };
    let mut headers = HeaderMap::new();
    let origin = config::web_origin("intl");
    for (key, value) in [
        (AUTHORIZATION, format!("Bearer {}", account.access_token())),
        (CONTENT_TYPE, "application/json".into()),
        (ACCEPT, "application/json, text/plain, */*".into()),
        (ORIGIN, origin.clone()),
        (REFERER, format!("{origin}/app")),
        (USER_AGENT, config::web_user_agent()),
    ] {
        if let Ok(value) = HeaderValue::from_str(&value) {
            headers.insert(key, value);
        }
    }
    if let Ok(value) = HeaderValue::from_str(&account.uid()) {
        headers.insert("x-user-id", value);
    }
    headers
}

fn conversations_url() -> String {
    format!("{}/console/as/conversations/", config::web_origin("intl"))
}

pub(super) fn url(conversation: &str) -> String {
    format!(
        "{}{}",
        conversations_url(),
        util::encode_component(conversation)
    )
}

pub(super) fn string_field(value: &Value, keys: &[&str]) -> String {
    keys.iter()
        .find_map(|key| value.get(*key).and_then(Value::as_str))
        .unwrap_or("")
        .to_string()
}
