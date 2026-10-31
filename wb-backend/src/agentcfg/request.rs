//! HTTP request contract and key/model selection.

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use hyper::body::Incoming;
use hyper::{Request, StatusCode};
use serde_json::{json, Map, Value};

use crate::http::{body_json, error_resp, json_resp, Resp};
use crate::App;

use super::clients;
use super::model_picker::{clean_models, configured_models_or_all, enrich_models};
use super::ops::{integrate, restore};

static WRITE_LOCK: Mutex<()> = Mutex::new(());

/// Export point for `POST /agents/apply` and `POST /agents/restore`.
///
/// The enclosing router should call this only after its panel-session and
/// loopback checks; the request itself is consumed here so keep-alive bodies
/// are always drained.
pub async fn handle(app: &App, req: Request<Incoming>) -> Resp {
    let path = req.uri().path().to_string();
    if req.method() != hyper::Method::POST {
        return error_resp(StatusCode::METHOD_NOT_ALLOWED, "method not allowed");
    }
    let result = match path.as_str() {
        "/agents/apply" => {
            let payload = match body_json(req).await {
                Ok(value) => value,
                Err(resp) => return resp,
            };
            let _guard = WRITE_LOCK.lock().unwrap_or_else(|err| err.into_inner());
            apply_payload(app, &payload)
        }
        "/agents/restore" => {
            let payload = match body_json(req).await {
                Ok(value) => value,
                Err(resp) => return resp,
            };
            let _guard = WRITE_LOCK.lock().unwrap_or_else(|err| err.into_inner());
            restore_payload(app, &payload)
        }
        _ => return error_resp(StatusCode::NOT_FOUND, "not found"),
    };
    match result {
        Ok(value) => json_resp(StatusCode::OK, &value),
        Err(message) => json_resp(
            StatusCode::BAD_REQUEST,
            &json!({"error": {
                "message": message,
                "type": "invalid_request_error",
                "code": 400
            }}),
        ),
    }
}

fn apply_payload(app: &App, payload: &Value) -> Result<Value, String> {
    let request = prepare_apply(app, payload)?;
    let mut result = integrate(
        &app.state.accounts_dir,
        &resolve_home(None),
        &request.client_id,
        &request.base_url,
        &request.api_key,
        request.model.as_deref(),
        &request.models,
        true,
    )?;
    let files = result
        .get("files")
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0);
    if let Some(object) = result.as_object_mut() {
        object.insert("warnings".into(), json!(request.warnings));
    }
    app.log(
        "agents",
        "info",
        &format!(
            "agents apply: client={} base_url={} model={} files={files}",
            request.client_id,
            request.base_url,
            request.model.as_deref().unwrap_or("-")
        ),
    );
    Ok(result)
}

struct ApplyRequest {
    client_id: String,
    base_url: String,
    model: Option<String>,
    models: Vec<Value>,
    api_key: String,
    warnings: Vec<String>,
}

fn prepare_apply(app: &App, payload: &Value) -> Result<ApplyRequest, String> {
    let Some(payload) = payload.as_object() else {
        return Err("request body must be a JSON object".into());
    };
    let client_id = text_field(payload, &["client", "client_id"])
        .trim()
        .to_string();
    if client_id.is_empty() {
        return Err("client is required".into());
    }
    let base_url = text_field(payload, &["base_url"]).trim().to_string();
    if !(base_url.starts_with("http://") || base_url.starts_with("https://")) {
        return Err("base_url must start with http:// or https://".into());
    }
    let model = text_field(payload, &["model"]);
    let model = (!model.trim().is_empty()).then(|| model.trim().to_string());
    let raw_models = match payload.get("models") {
        Some(Value::Array(items)) => items.clone(),
        Some(_) => return Err("models must be a list".into()),
        None => configured_models_or_all(app),
    };
    let mut models = clean_models(&raw_models);
    enrich_models(&mut models);
    if models.is_empty() && clients::model_embedding(&client_id) {
        return Err(
            "请先在「网关模型清单」中勾选至少一个模型：只有勾选的模型才会写入客户端配置".into(),
        );
    }
    let (api_key, warnings) = apply_key(app, payload)?;
    Ok(ApplyRequest {
        client_id,
        base_url,
        model,
        models,
        api_key,
        warnings,
    })
}

fn apply_key(app: &App, payload: &Map<String, Value>) -> Result<(String, Vec<String>), String> {
    let mut warnings = Vec::new();
    let key = resolve_key(app, payload, &mut warnings)?;
    let api_key = match key {
        Some(key) => key,
        None if auth_required(app) => {
            return Err("no gateway key available - 请先配置 API Key".into())
        }
        None => {
            warnings.push(
                "gateway has no key configured; wrote placeholder 'wb-local' \
                 (auth is off, so any value works)"
                    .into(),
            );
            "wb-local".into()
        }
    };
    Ok((api_key, warnings))
}

fn restore_payload(app: &App, payload: &Value) -> Result<Value, String> {
    let Some(payload) = payload.as_object() else {
        return Err("request body must be a JSON object".into());
    };
    let client_id = text_field(payload, &["client", "client_id"])
        .trim()
        .to_string();
    if client_id.is_empty() {
        return Err("client is required".into());
    }
    let result = restore(&app.state.accounts_dir, &resolve_home(None), &client_id)?;
    let count = result
        .get("restored")
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0);
    app.log(
        "agents",
        "info",
        &format!("agents restore: client={client_id} files={count}"),
    );
    Ok(result)
}

fn text_field(payload: &Map<String, Value>, names: &[&str]) -> String {
    names
        .iter()
        .find_map(|name| payload.get(*name))
        .map(|value| {
            if value_truthy(value) {
                python_text(value)
            } else {
                String::new()
            }
        })
        .unwrap_or_default()
}

fn value_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().unwrap_or(0.0) != 0.0,
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn python_text(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::String(text) => text.clone(),
        Value::Bool(v) => {
            if *v {
                "True".into()
            } else {
                "False".into()
            }
        }
        Value::Number(v) => v.to_string(),
        _ => String::new(),
    }
}

fn resolve_key(
    app: &App,
    payload: &Map<String, Value>,
    _warnings: &mut Vec<String>,
) -> Result<Option<String>, String> {
    let key_id = text_field(payload, &["key_id"]);
    let global = std::env::var("WB_API_KEY").ok().filter(|v| !v.is_empty());
    if key_id == "__global" {
        return Ok(global);
    }
    let keys = app
        .state
        .settings_snapshot()
        .get("api_keys")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if !key_id.is_empty() {
        return keys
            .iter()
            .find(|key| key.get("id").map(python_text) == Some(key_id.clone()))
            .and_then(|key| key.get("key").and_then(Value::as_str))
            .filter(|key| !key.is_empty())
            .map(str::to_string)
            .map(Some)
            .ok_or_else(|| format!("no configured key with id {key_id:?}"));
    }
    if global.is_some() {
        return Ok(global);
    }
    Ok(keys.into_iter().find_map(|key| {
        (key.get("enabled").and_then(Value::as_bool) == Some(true))
            .then(|| key.get("key").and_then(Value::as_str))
            .flatten()
            .filter(|key| !key.is_empty())
            .map(str::to_string)
    }))
}

fn auth_required(app: &App) -> bool {
    if app
        .state
        .settings_snapshot()
        .get("auth_disabled")
        .and_then(Value::as_bool)
        == Some(true)
    {
        return false;
    }
    let has_panel_key = app
        .state
        .settings_snapshot()
        .get("api_keys")
        .and_then(Value::as_array)
        .is_some_and(|keys| {
            keys.iter().any(|key| {
                key.get("enabled").and_then(Value::as_bool) == Some(true)
                    && key
                        .get("key")
                        .and_then(Value::as_str)
                        .is_some_and(|text| !text.is_empty())
            })
        });
    has_panel_key
        || std::env::var("WB_API_KEY")
            .map(|key| !key.is_empty())
            .unwrap_or(false)
}

fn resolve_home(home: Option<&Path>) -> PathBuf {
    if let Some(home) = home {
        return absolute(home);
    }
    let home = std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    absolute(&home)
}

fn absolute(path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(path)
    }
}
