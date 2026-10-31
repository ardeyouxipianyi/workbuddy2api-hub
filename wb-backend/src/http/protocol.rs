//! Shared request parsing and API-key checks for protocol routes.

use hyper::body::Incoming;
use hyper::{Request, StatusCode};
use serde_json::{json, Value};

use crate::http::{json_resp, Resp};
use crate::App;

pub struct Identity {
    pub key_id: String,
    pub realm: String,
}

fn supplied(req: &Request<Incoming>) -> String {
    if let Some(value) = req
        .headers()
        .get("authorization")
        .and_then(|v| v.to_str().ok())
    {
        let value = value.trim();
        let mut parts = value.splitn(2, ' ');
        let first = parts.next().unwrap_or("");
        let supplied = if first.eq_ignore_ascii_case("bearer") {
            parts.next().unwrap_or("").trim()
        } else {
            value
        };
        let supplied = supplied
            .strip_prefix(['"', '\''])
            .and_then(|value| value.strip_suffix(['"', '\'']))
            .unwrap_or(supplied)
            .trim();
        if !supplied.is_empty() {
            return supplied.to_string();
        }
    }
    for name in ["x-api-key", "api-key", "x-auth-token"] {
        if let Some(value) = req.headers().get(name).and_then(|v| v.to_str().ok()) {
            if !value.trim().is_empty() {
                return value.trim().to_string();
            }
        }
    }
    req.uri()
        .query()
        .into_iter()
        .flat_map(|query| query.split('&'))
        .find_map(|pair| {
            let (name, value) = pair.split_once('=')?;
            matches!(name, "key" | "api_key" | "api-key")
                .then(|| value.trim().to_string())
                .filter(|value| !value.is_empty())
        })
        .unwrap_or_default()
}

/// `_key_ok`: a valid panel session or API key, or keyless mode.
pub fn authorize(app: &App, req: &Request<Incoming>) -> Result<Identity, ()> {
    let panel_ok = req
        .headers()
        .get("x-panel-token")
        .and_then(|v| v.to_str().ok())
        .map(|token| app.panel.token_ok(Some(token)))
        .unwrap_or(false);
    if panel_ok {
        return Ok(Identity {
            key_id: String::new(),
            realm: String::new(),
        });
    }

    let supplied = supplied(req);
    let checked = crate::auth::check(&app.state, &supplied)?;
    let realm = app
        .state
        .settings()
        .get("api_keys")
        .and_then(Value::as_array)
        .and_then(|keys| {
            keys.iter()
                .find(|entry| entry.get("id").and_then(Value::as_str) == Some(&checked.key_id))
        })
        .and_then(|entry| entry.get("realm"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    Ok(Identity {
        key_id: checked.key_id,
        realm,
    })
}

fn has_configured_key(app: &App) -> bool {
    app.state
        .settings()
        .get("api_keys")
        .and_then(Value::as_array)
        .map(|keys| {
            keys.iter().any(|key| {
                key.get("enabled").and_then(Value::as_bool).unwrap_or(false)
                    && key
                        .get("key")
                        .and_then(Value::as_str)
                        .map(|v| !v.is_empty())
                        .unwrap_or(false)
            })
        })
        .unwrap_or(false)
        || std::env::var("WB_API_KEY")
            .map(|v| !v.is_empty())
            .unwrap_or(false)
}

pub fn openai_auth_error(app: &App) -> Resp {
    let message = if has_configured_key(app) {
        "invalid api key - send it as 'Authorization: Bearer <key>' or '?key=<key>'; \
         copy the value from the panel's 设置 page"
    } else {
        "invalid api key - no key is configured - open the dashboard and add one, \
         or restart with --api-key"
    };
    json_resp(
        StatusCode::UNAUTHORIZED,
        &json!({"error": {"message": message, "type": "invalid_request_error", "code": 401}}),
    )
}

/// Mirrors `_request_realm`: key binding, X-Realm, query, then the current realm.
pub fn requested_realm(app: &App, req: &Request<Incoming>, identity: &Identity) -> String {
    if matches!(identity.realm.as_str(), "cn" | "intl" | "all") {
        return identity.realm.clone();
    }
    if let Some(realm) = req.headers().get("x-realm").and_then(|v| v.to_str().ok()) {
        if matches!(realm, "cn" | "intl" | "all") {
            return realm.to_string();
        }
    }
    if let Some(query) = req.uri().query() {
        for pair in query.split('&') {
            if let Some(realm) = pair.strip_prefix("realm=") {
                if matches!(realm, "cn" | "intl" | "all") {
                    return realm.to_string();
                }
            }
        }
    }
    app.state.current_realm()
}

pub enum BodyError {
    TooLarge { length: usize, limit: usize },
    Invalid,
}

/// Read a JSON object while retaining the size test used by Python.
pub async fn json_body(req: Request<Incoming>) -> Result<Value, BodyError> {
    let limit = std::env::var("WB_MAX_PAYLOAD_BYTES")
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .unwrap_or(16 * 1024 * 1024);
    let bytes = http_body_util::BodyExt::collect(req.into_body())
        .await
        .map_err(|_| BodyError::Invalid)?
        .to_bytes();
    if bytes.len() > limit {
        return Err(BodyError::TooLarge {
            length: bytes.len(),
            limit,
        });
    }
    let payload: Value = serde_json::from_slice(&bytes).map_err(|_| BodyError::Invalid)?;
    Ok(if payload.is_object() {
        payload
    } else {
        json!({})
    })
}
