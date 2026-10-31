//! Desktop credential discovery, scanning, and plaintext import.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use hyper::StatusCode;
use serde_json::{json, Map, Value};

use super::super::{config, util};
use crate::http::{json_resp, Resp};
use crate::App;

pub(super) async fn all(app: &Arc<App>, realm: Option<&str>) -> Resp {
    let mut imported = Vec::new();
    for (path, hint) in candidates() {
        if realm.filter(|want| *want != hint).is_some() {
            continue;
        }
        if let Ok(account) = import_file(app, &path, realm.or(Some(hint))).await {
            imported.push(crate::panel::account_view(
                app,
                &account,
                crate::state::now_epoch(),
            ));
        }
    }
    json_resp(
        StatusCode::OK,
        &json!({"imported": imported, "accounts": account_views(app)}),
    )
}

pub(super) async fn one_path(app: &Arc<App>, path: &Path, realm: Option<&str>) -> Resp {
    match import_file(app, path, realm).await {
        Ok(account) => json_resp(
            StatusCode::OK,
            &json!({
                "imported": [crate::panel::account_view(app, &account, crate::state::now_epoch())],
                "accounts": account_views(app)
            }),
        ),
        Err(error) => util::error(StatusCode::BAD_REQUEST, format!("import failed: {error}")),
    }
}

async fn import_file(
    app: &Arc<App>,
    path: &Path,
    realm_hint: Option<&str>,
) -> Result<crate::state::AccountFile, String> {
    let (blob, auth, profile, token) = read_credential(path)?;
    let domain_hint = auth.get("domain").and_then(Value::as_str).unwrap_or("");
    let realm = credential_realm(&token, domain_hint, realm_hint);
    let uid = credential_uid(&profile, &token)?;
    let data = account_data(&blob, &token, &realm, &uid)?;
    let account_path = app.state.accounts_dir.join(format!("{uid}.json"));
    crate::state::write_json_atomic(&account_path, &data).map_err(|exc| exc.to_string())?;
    app.state.reload();
    let account =
        util::find_account(app, &uid).ok_or_else(|| "saved account is unreadable".to_string())?;
    if account.realm() == "cn" {
        let _ = crate::credits::checkin(app, &account).await;
    }
    app.log(
        "accounts",
        "INFO",
        &format!(
            "imported {} from {} (user confirmed)",
            uid.chars().take(8).collect::<String>(),
            path.file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("")
        ),
    );
    Ok(util::find_account(app, &uid).unwrap_or(account))
}

fn read_credential(
    path: &Path,
) -> Result<(Value, Map<String, Value>, Map<String, Value>, String), String> {
    let raw = std::fs::read_to_string(path).map_err(|exc| exc.to_string())?;
    let blob = serde_json::from_str::<Value>(&raw).map_err(|exc| exc.to_string())?;
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
    let auth_value = blob.get("auth").cloned().unwrap_or_else(|| json!({}));
    let token = match auth_value.get("accessToken").and_then(Value::as_str) {
        Some(token) if !token.is_empty() => token.to_string(),
        _ => String::new(),
    };
    if token.is_empty() {
        return Err(format!("no accessToken in {}", path.display()));
    }
    if is_encrypted(&token) {
        return Err("encrypted desktop credentials are not supported by the Rust backend".into());
    }
    Ok((blob, auth, profile, token))
}

fn credential_uid(profile: &Map<String, Value>, token: &str) -> Result<String, String> {
    let uid = profile
        .get("uid")
        .and_then(Value::as_str)
        .filter(|uid| !uid.is_empty())
        .map(str::to_string)
        .or_else(|| crate::state::jwt_sub(token))
        .ok_or_else(|| "credential has no uid".to_string())?;
    if !util::safe_uid(&uid) {
        return Err("credential uid contains unsafe characters".into());
    }
    Ok(uid)
}

fn account_data(blob: &Value, token: &str, realm: &str, uid: &str) -> Result<Value, String> {
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
    let refresh = auth
        .get("refreshToken")
        .and_then(Value::as_str)
        .unwrap_or("");
    let expires = normalize_epoch(auth.get("expiresAt"))
        .or_else(|| crate::state::jwt_exp(token))
        .unwrap_or(0);
    Ok(json!({
        "uid": uid,
        "nickname": profile.get("nickname").and_then(Value::as_str).unwrap_or(""),
        "domain": aligned_domain(realm, auth.get("domain").and_then(Value::as_str).unwrap_or("")),
        "realm": realm,
        "platform": "CLI",
        "product": "workbuddy",
        "enterpriseId": profile.get("enterpriseId").and_then(Value::as_str).unwrap_or(""),
        "accessToken": token,
        "refreshToken": refresh,
        "expiresAt": expires,
        "source": "desktop-app",
        "enabled": true,
        "tasksEnabled": true,
        "addedAt": crate::state::now_epoch()
    }))
}

pub(super) fn candidates() -> Vec<(PathBuf, &'static str)> {
    let intl_name = std::env::var("WB_DESKTOP_INTL_FILENAME")
        .unwrap_or_else(|_| "workbuddy-desktop-ai.info".into());
    let cn_name =
        std::env::var("WB_DESKTOP_CN_FILENAME").unwrap_or_else(|_| "workbuddy-desktop.info".into());
    let mut out = Vec::new();
    for directory in auth_dirs() {
        for (name, realm) in [(&intl_name, "intl"), (&cn_name, "cn")] {
            let path = directory.join(name);
            if path.is_file() && !out.iter().any(|(seen, _)| seen == &path) {
                out.push((path, realm));
            }
        }
    }
    out
}

fn auth_dirs() -> Vec<PathBuf> {
    if let Ok(paths) = std::env::var("WB_DESKTOP_AUTH_DIRS") {
        return std::env::split_paths(&paths).collect();
    }
    let Some(home) = std::env::var_os("HOME").map(PathBuf::from) else {
        return Vec::new();
    };
    if cfg!(windows) {
        let local = std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join("AppData").join("Local"));
        return vec![local.join("CodeBuddyExtension/Data/Public/auth")];
    }
    let base = if cfg!(target_os = "macos") {
        home.join("Library/Application Support")
    } else {
        home.join(".local/share")
    };
    ["CodeBuddyExtension", "WorkBuddy", "CodeBuddy"]
        .into_iter()
        .flat_map(|app| {
            [
                base.join(app).join("Data/Public/auth"),
                base.join(app).join("auth"),
            ]
        })
        .collect()
}

pub(super) fn credential_realm(token: &str, domain: &str, hint: Option<&str>) -> String {
    detect_realm(token, domain)
        .or_else(|| hint.filter(|realm| *realm == "cn" || *realm == "intl"))
        .unwrap_or("intl")
        .to_string()
}

fn detect_realm(token: &str, domain: &str) -> Option<&'static str> {
    let issuer = jwt_issuer(token).to_lowercase();
    let domain = domain.to_lowercase();
    if ["copilot.tencent.com", "codebuddy.cn", "workbuddy.cn"]
        .iter()
        .any(|marker| issuer.contains(marker) || domain.contains(marker))
    {
        Some("cn")
    } else if ["workbuddy.ai", "codebuddy.ai"]
        .iter()
        .any(|marker| issuer.contains(marker) || domain.contains(marker))
    {
        Some("intl")
    } else {
        None
    }
}

fn jwt_issuer(token: &str) -> String {
    let Some(payload) = token.split('.').nth(1) else {
        return String::new();
    };
    let normalized = payload.replace('-', "+").replace('_', "/");
    let Some(bytes) = decode_base64(&normalized) else {
        return String::new();
    };
    serde_json::from_slice::<Value>(&bytes)
        .ok()
        .and_then(|value| value.get("iss").and_then(Value::as_str).map(str::to_string))
        .unwrap_or_default()
}

fn decode_base64(text: &str) -> Option<Vec<u8>> {
    let mut out = Vec::new();
    let mut accumulator = 0u32;
    let mut bits = 0u8;
    for byte in text.bytes().filter(|byte| *byte != b'=') {
        let value = match byte {
            b'A'..=b'Z' => byte - b'A',
            b'a'..=b'z' => byte - b'a' + 26,
            b'0'..=b'9' => byte - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => return None,
        } as u32;
        accumulator = (accumulator << 6) | value;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((accumulator >> bits) as u8);
            accumulator &= (1 << bits) - 1;
        }
    }
    Some(out)
}

pub(super) fn aligned_domain(realm: &str, domain: &str) -> String {
    let other_markers: &[&str] = if realm == "cn" {
        &["workbuddy.ai", "codebuddy.ai"]
    } else {
        &["copilot.tencent.com", "codebuddy.cn", "workbuddy.cn"]
    };
    if domain.trim().is_empty()
        || other_markers
            .iter()
            .any(|marker| domain.to_lowercase().contains(marker))
    {
        config::domain(realm)
    } else {
        domain.trim().to_string()
    }
}

pub(super) fn normalize_epoch(value: Option<&Value>) -> Option<i64> {
    let raw = match value? {
        Value::Number(number) => number.as_f64()?,
        Value::String(text) => text.trim().parse().ok()?,
        _ => return None,
    };
    let seconds = if raw > 1e11 { raw / 1000.0 } else { raw };
    (seconds > 0.0).then_some(seconds as i64)
}

pub(super) fn is_encrypted(value: &str) -> bool {
    value.trim_start().starts_with("{") && value.contains("$wbEncrypted")
}

pub(super) fn account_views(app: &Arc<App>) -> Vec<Value> {
    let now = crate::state::now_epoch();
    app.state
        .accounts_snapshot()
        .iter()
        .map(|account| crate::panel::account_view(app, account, now))
        .collect()
}
