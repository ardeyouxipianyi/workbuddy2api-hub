//! Release API/fallback discovery and stable-version comparison.

use serde_json::{json, Value};

use crate::update_config::Config;

#[derive(Clone, Debug)]
pub(crate) struct Release {
    pub(crate) version: String,
    pub(crate) tag: String,
    pub(crate) url: String,
    pub(crate) published_at: String,
}

pub(crate) async fn fetch_releases(
    config: &Config,
    client: &reqwest::Client,
) -> Result<Vec<Value>, String> {
    match fetch_api(config, client).await {
        Ok(value) => Ok(value),
        Err(api_error) => match fetch_latest(config, client).await {
            Ok(release) => Ok(vec![release]),
            Err(_) => Err(api_error),
        },
    }
}

async fn fetch_api(config: &Config, client: &reqwest::Client) -> Result<Vec<Value>, String> {
    let mut request = client
        .get(&config.releases_url)
        .header(reqwest::header::USER_AGENT, &config.user_agent)
        .header(reqwest::header::ACCEPT, "application/vnd.github+json");
    if let Ok(token) = std::env::var(&config.token_env) {
        if !token.trim().is_empty() {
            request = request.bearer_auth(token);
        }
    }
    let response = request.send().await.map_err(network_error)?;
    let response = response.error_for_status().map_err(http_error)?;
    let body = read_limited(response, config.max_body_bytes).await?;
    let value: Value = serde_json::from_slice(&body).map_err(|_| "invalid JSON".to_string())?;
    value
        .as_array()
        .cloned()
        .ok_or_else(|| "unexpected release document".into())
}

async fn fetch_latest(config: &Config, client: &reqwest::Client) -> Result<Value, String> {
    let response = client
        .get(&config.latest_url)
        .header(reqwest::header::USER_AGENT, &config.user_agent)
        .send()
        .await
        .map_err(network_error)?;
    let response = response.error_for_status().map_err(http_error)?;
    let final_url = response.url().as_str();
    let tag = final_url
        .split_once("/releases/tag/")
        .map(|(_, tail)| tail)
        .and_then(|tail| tail.split(['/', '?', '#']).next())
        .filter(|tag| !tag.is_empty())
        .ok_or_else(|| "unexpected release document".to_string())?;
    Ok(json!({
        "tag_name": tag,
        "draft": false,
        "prerelease": false,
        "html_url": final_url,
        "published_at": "",
    }))
}

async fn read_limited(mut response: reqwest::Response, max: usize) -> Result<Vec<u8>, String> {
    if response
        .content_length()
        .map(|length| length > max as u64)
        .unwrap_or(false)
    {
        return Err("response too large".into());
    }
    let mut body = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(network_error)? {
        if body.len().saturating_add(chunk.len()) > max {
            return Err("response too large".into());
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

fn network_error(error: reqwest::Error) -> String {
    if error.is_timeout() {
        "network error: timeout".into()
    } else {
        "network error: request failed".into()
    }
}

fn http_error(error: reqwest::Error) -> String {
    error
        .status()
        .map(|status| format!("HTTP {}", status.as_u16()))
        .unwrap_or_else(|| network_error(error))
}

pub(crate) fn pick_latest(releases: &[Value], hosts: &[String]) -> Option<Release> {
    let mut best: Option<((u64, u64, u64), Release)> = None;
    for entry in releases {
        if entry.get("draft").and_then(Value::as_bool) == Some(true)
            || entry.get("prerelease").and_then(Value::as_bool) == Some(true)
        {
            continue;
        }
        let tag = entry.get("tag_name").and_then(Value::as_str).unwrap_or("");
        let Some(version) = parse_version(tag) else {
            continue;
        };
        if best
            .as_ref()
            .map(|(seen, _)| version <= *seen)
            .unwrap_or(false)
        {
            continue;
        }
        best = Some((
            version,
            Release {
                version: format!("{}.{}.{}", version.0, version.1, version.2),
                tag: tag.trim().chars().take(64).collect(),
                url: safe_release_url(
                    entry.get("html_url").and_then(Value::as_str).unwrap_or(""),
                    hosts,
                ),
                published_at: entry
                    .get("published_at")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .chars()
                    .take(32)
                    .collect(),
            },
        ));
    }
    best.map(|(_, release)| release)
}

fn safe_release_url(value: &str, hosts: &[String]) -> String {
    let Ok(url) = reqwest::Url::parse(value.trim()) else {
        return String::new();
    };
    let allowed = url.scheme() == "https"
        && url
            .host_str()
            .map(|host| {
                hosts
                    .iter()
                    .any(|allowed| allowed == &host.to_ascii_lowercase())
            })
            .unwrap_or(false);
    if allowed {
        value.trim().chars().take(300).collect()
    } else {
        String::new()
    }
}

pub(crate) fn parse_version(value: &str) -> Option<(u64, u64, u64)> {
    let text = value.trim().strip_prefix('v').unwrap_or(value.trim());
    if text.is_empty() {
        return None;
    }
    let mut parts = text.split('.');
    let major = parts.next()?.parse().ok()?;
    let minor = parts.next()?.parse().ok()?;
    let patch = parts.next()?.parse().ok()?;
    (parts.next().is_none()).then_some((major, minor, patch))
}

pub(crate) fn is_newer(candidate: &str, current: &str) -> bool {
    matches!(
        (parse_version(candidate), parse_version(current)),
        (Some(left), Some(right)) if left > right
    )
}
