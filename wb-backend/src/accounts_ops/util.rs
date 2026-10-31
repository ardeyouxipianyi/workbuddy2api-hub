use hyper::StatusCode;
use serde_json::{json, Map, Value};

use crate::http::{json_resp, Resp};
use crate::state::AccountFile;

pub(crate) fn error(status: StatusCode, message: impl Into<String>) -> Resp {
    let message = message.into();
    let error_type = if status.is_server_error() {
        "server_error"
    } else {
        "invalid_request_error"
    };
    json_resp(
        status,
        &json!({"error": {
            "message": message,
            "type": error_type,
            "code": status.as_u16()
        }}),
    )
}

pub(crate) fn query_param(query: &str, name: &str) -> String {
    query
        .split('&')
        .find_map(|pair| {
            let (key, value) = pair.split_once('=')?;
            (percent_decode(key) == name).then(|| percent_decode(value))
        })
        .unwrap_or_default()
}

fn percent_decode(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'+' => out.push(b' '),
            b'%' if index + 2 < bytes.len() => {
                let hex = std::str::from_utf8(&bytes[index + 1..index + 3]).unwrap_or("");
                if let Ok(value) = u8::from_str_radix(hex, 16) {
                    out.push(value);
                    index += 2;
                } else {
                    out.push(bytes[index]);
                }
            }
            byte => out.push(byte),
        }
        index += 1;
    }
    String::from_utf8(out).unwrap_or_else(|_| text.to_string())
}

pub(crate) fn encode_component(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for byte in text.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b'~') {
            out.push(byte as char);
        } else {
            out.push('%');
            out.push_str(&format!("{byte:02X}"));
        }
    }
    out
}

pub(crate) fn object_or_empty(value: Value) -> Map<String, Value> {
    match value {
        Value::Object(object) => object,
        _ => Map::new(),
    }
}

pub(crate) fn nickname(account: &AccountFile) -> String {
    let stored = account.str("nickname").trim();
    if stored.is_empty() {
        account.uid().chars().take(8).collect()
    } else {
        stored.to_string()
    }
}

pub(crate) fn safe_uid(uid: &str) -> bool {
    !uid.is_empty()
        && uid.len() <= 128
        && uid != "."
        && uid != ".."
        && uid
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
}

pub(crate) fn find_account(app: &crate::App, uid: &str) -> Option<AccountFile> {
    app.state
        .accounts_snapshot()
        .into_iter()
        .find(|account| account.uid() == uid)
}

pub(crate) fn target_accounts(
    app: &crate::App,
    uid: &str,
    realm: &str,
    require_tasks: bool,
) -> Vec<AccountFile> {
    app.state
        .accounts_snapshot()
        .into_iter()
        .filter(|account| {
            if !uid.is_empty() {
                account.uid() == uid
            } else if !realm.is_empty() && realm != "all" {
                account.realm() == realm
                    && (!require_tasks
                        || account
                            .data
                            .get("tasksEnabled")
                            .and_then(Value::as_bool)
                            .unwrap_or(true))
            } else {
                !require_tasks
                    || account
                        .data
                        .get("tasksEnabled")
                        .and_then(Value::as_bool)
                        .unwrap_or(true)
            }
        })
        .collect()
}
