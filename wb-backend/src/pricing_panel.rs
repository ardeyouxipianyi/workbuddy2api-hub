//! Panel-facing pricing handler and explicit OpenRouter fetch.

use std::sync::Arc;

use hyper::body::Incoming;
use hyper::{Method, Request, StatusCode};
use serde_json::{json, Map, Value};

use crate::http::{json_resp, Resp};
use crate::pricing::Config;
use crate::App;

#[allow(dead_code)] // The parent backend owns route-table wiring.
pub async fn handle(app: &Arc<App>, req: Request<Incoming>) -> Option<Resp> {
    let method = req.method().clone();
    let path = req.uri().path();
    if !matches!(
        (&method, path),
        (&Method::GET, "/pricing") | (&Method::POST, "/pricing/refresh")
    ) {
        return None;
    }
    if !app.panel.token_ok(
        req.headers()
            .get("x-panel-token")
            .and_then(|value| value.to_str().ok()),
    ) {
        return Some(json_resp(
            StatusCode::UNAUTHORIZED,
            &json!({"error": {"message": "unauthorized", "type": "invalid_request_error", "code": 401}}),
        ));
    }
    if method == Method::GET {
        return Some(json_resp(
            StatusCode::OK,
            &app.pricing.status(&app.state.settings_snapshot()),
        ));
    }
    Some(refresh_response(app))
}

fn refresh_response(app: &Arc<App>) -> Resp {
    let settings = app.state.settings_snapshot();
    if !app.pricing.settings(&settings).enabled {
        return json_resp(
            StatusCode::OK,
            &json!({"ok": false, "msg": "价估算已关闭，请先启用"}),
        );
    }
    if app.pricing.begin_refresh() {
        let task_app = app.clone();
        tokio::spawn(async move {
            task_app.pricing.run_refresh_staged(task_app.clone()).await;
        });
    }
    let mut status = app.pricing.status(&settings);
    if let Some(map) = status.as_object_mut() {
        map.insert("ok".into(), json!(true));
        map.insert("msg".into(), json!("已开始抓取，稍候刷新查看结果"));
    }
    json_resp(StatusCode::OK, &status)
}

pub async fn fetch_openrouter(config: &Config) -> Result<Map<String, Value>, String> {
    let client = reqwest::Client::builder()
        .timeout(config.timeout)
        .build()
        .map_err(|error| error.to_string())?;
    let mut response = client
        .get(&config.url)
        .header("User-Agent", "workbuddy2api-hub-pricing/1.0")
        .send()
        .await
        .map_err(|error| error.to_string())?
        .error_for_status()
        .map_err(|error| error.to_string())?;
    let mut body = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(|error| error.to_string())? {
        if body.len().saturating_add(chunk.len()) > config.max_response_bytes {
            return Err(format!(
                "pricing response exceeded {} bytes",
                config.max_response_bytes
            ));
        }
        body.extend_from_slice(&chunk);
    }
    let payload: Value = serde_json::from_slice(&body).map_err(|error| error.to_string())?;
    let mut out = Map::new();
    for item in payload
        .get("data")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        if out.len() >= config.max_models {
            return Err(format!(
                "pricing response exceeded {} models",
                config.max_models
            ));
        }
        if let Some(id) = item.get("id").and_then(Value::as_str) {
            out.insert(
                id.to_string(),
                item.get("pricing").cloned().unwrap_or_else(|| json!({})),
            );
        }
    }
    Ok(out)
}
