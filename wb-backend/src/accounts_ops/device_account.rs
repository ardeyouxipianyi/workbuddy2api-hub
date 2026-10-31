//! Device-login success persistence.

use std::sync::Arc;

use serde_json::{json, Map, Value};

use super::{config, LoginSession};
use crate::App;

pub(super) fn create(
    session: &LoginSession,
    data: &Map<String, Value>,
    token: &str,
    uid: &str,
    nickname: &str,
) -> Value {
    let expires = normalize_epoch(data.get("expiresAt"))
        .or_else(|| crate::state::jwt_exp(token))
        .unwrap_or(0);
    let domain = data
        .get("domain")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
        .unwrap_or_else(|| config::domain(&session.realm));
    json!({
        "uid": uid,
        "nickname": if nickname.is_empty() {
            uid.chars().take(8).collect::<String>()
        } else {
            nickname.to_string()
        },
        "domain": domain,
        "realm": session.realm,
        "platform": session.platform,
        "product": "workbuddy",
        "accessToken": token,
        "refreshToken": data.get("refreshToken").and_then(Value::as_str).unwrap_or(""),
        "expiresAt": expires,
        "source": "oauth",
        "enabled": true,
        "tasksEnabled": true,
        "weight": 1,
        "note": "",
        "disabledModels": [],
        "proxySlot": "",
        "proxy": "",
        "addedAt": crate::state::now_epoch()
    })
}

fn normalize_epoch(value: Option<&Value>) -> Option<i64> {
    let raw = match value? {
        Value::Number(number) => number.as_f64()?,
        Value::String(text) => text.trim().parse().ok()?,
        _ => return None,
    };
    let seconds = if raw > 1e11 { raw / 1000.0 } else { raw };
    (seconds > 0.0).then_some(seconds as i64)
}

pub(super) fn persist(app: &Arc<App>, uid: &str, account: Value) -> std::io::Result<()> {
    let path = app.state.accounts_dir.join(format!("{uid}.json"));
    crate::state::write_json_atomic(&path, &account)?;
    app.state.reload();
    Ok(())
}
