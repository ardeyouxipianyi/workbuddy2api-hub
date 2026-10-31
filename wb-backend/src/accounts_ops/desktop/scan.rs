//! Read-only desktop credential scan.

use std::path::Path;
use std::sync::Arc;

use serde_json::{json, Value};

use super::super::config;
use super::import;
use crate::App;

pub(super) fn payload(app: &Arc<App>) -> Value {
    let detected: Vec<Value> = import::candidates()
        .into_iter()
        .map(|(path, hint)| item(&path, hint))
        .collect();
    json!({
        "detected": detected,
        "accounts": import::account_views(app),
        "pool_uids": app.state.accounts_snapshot()
            .into_iter().map(|account| account.uid()).collect::<Vec<_>>(),
        "atrest": app.accounts_ops.at_rest.status()
    })
}

fn item(path: &Path, realm_hint: &str) -> Value {
    let mut item = base_item(path, realm_hint);
    let raw = match std::fs::read_to_string(path) {
        Ok(raw) => raw,
        Err(error) => {
            item["error"] = json!(error.to_string());
            return item;
        }
    };
    item["readable"] = json!(true);
    let blob = match serde_json::from_str::<Value>(&raw) {
        Ok(blob) => blob,
        Err(error) => {
            item["error"] = json!(error.to_string());
            return item;
        }
    };
    fill_item(&mut item, &blob, realm_hint);
    item
}

fn base_item(path: &Path, realm_hint: &str) -> Value {
    json!({
        "path": path,
        "file": path.file_name().and_then(|name| name.to_str()).unwrap_or(""),
        "realm": realm_hint,
        "realmName": realm_name(realm_hint),
        "domain": config::domain(realm_hint),
        "readable": false,
        "valid": false,
        "encrypted": false,
        "needsKey": false,
        "uid": "",
        "nickname": "",
        "expiresAt": 0,
        "error": ""
    })
}

fn fill_item(item: &mut Value, blob: &Value, realm_hint: &str) {
    let auth = blob
        .get("auth")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let profile = blob
        .get("account")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let token = auth
        .get("accessToken")
        .and_then(Value::as_str)
        .unwrap_or("");
    if import::is_encrypted(token) {
        item["encrypted"] = json!(true);
        item["needsKey"] = json!(true);
        item["error"] =
            json!("encrypted desktop credentials are not supported by the Rust backend");
        return;
    }
    if token.is_empty() {
        item["error"] = json!("no accessToken inside the file");
        return;
    }
    let domain = auth.get("domain").and_then(Value::as_str).unwrap_or("");
    let realm = import::credential_realm(token, domain, Some(realm_hint));
    item["valid"] = json!(true);
    item["uid"] = json!(profile
        .get("uid")
        .and_then(Value::as_str)
        .filter(|uid| !uid.is_empty())
        .map(str::to_string)
        .or_else(|| crate::state::jwt_sub(token))
        .unwrap_or_default());
    item["nickname"] = json!(profile
        .get("nickname")
        .and_then(Value::as_str)
        .unwrap_or(""));
    item["realm"] = json!(realm);
    item["realmName"] = json!(realm_name(&realm));
    item["domain"] = json!(import::aligned_domain(&realm, domain));
    item["expiresAt"] = json!(import::normalize_epoch(auth.get("expiresAt"))
        .or_else(|| crate::state::jwt_exp(token))
        .unwrap_or(0));
}

fn realm_name(realm: &str) -> &'static str {
    if realm == "cn" {
        "国内版 (China)"
    } else {
        "国际版 (Global)"
    }
}
