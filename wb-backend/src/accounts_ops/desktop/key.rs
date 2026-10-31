//! In-memory at-rest key import and status.

use std::sync::Arc;

use hyper::StatusCode;
use serde_json::json;
use sha2::{Digest, Sha256};

use crate::http::{json_resp, Resp};
use crate::App;

pub(super) fn derive_key_id(key: &[u8]) -> String {
    format!("{:x}", Sha256::digest(key))[..16].to_string()
}

pub(super) fn recover(app: &Arc<App>, force: bool) -> Resp {
    if !force && app.accounts_ops.at_rest.cached() {
        return json_resp(
            StatusCode::OK,
            &json!({
                "ok": true,
                "keyId": app.accounts_ops.at_rest.key_id(),
                "msg": "已从桌面客户端进程回收密钥（只留在内存里）",
                "atrest": app.accounts_ops.at_rest.status()
            }),
        );
    }
    json_resp(
        StatusCode::OK,
        &json!({
            "ok": false,
            "msg": "desktop process-memory key recovery is not available in the Rust backend",
            "atrest": app.accounts_ops.at_rest.status()
        }),
    )
}

pub(super) fn import(app: &Arc<App>, text: &str) -> Resp {
    let Some(key) = parse_key_text(text) else {
        return json_resp(
            StatusCode::OK,
            &json!({
                "ok": false,
                "msg": "认不出密钥格式：需要 64 位 hex 保护密钥，或 44 字符 base64 的 atRestSecretKey",
                "atrest": app.accounts_ops.at_rest.status()
            }),
        );
    };
    app.accounts_ops.at_rest.adopt(key);
    json_resp(
        StatusCode::OK,
        &json!({
            "ok": true,
            "keyId": app.accounts_ops.at_rest.key_id(),
            "msg": "已导入解码密钥（只留在内存里）",
            "atrest": app.accounts_ops.at_rest.status()
        }),
    )
}

fn parse_key_text(text: &str) -> Option<Vec<u8>> {
    let trimmed = text.trim();
    if trimmed.len() == 64 {
        let mut bytes = Vec::with_capacity(32);
        for index in (0..trimmed.len()).step_by(2) {
            bytes.push(u8::from_str_radix(&trimmed[index..index + 2], 16).ok()?);
        }
        return Some(bytes);
    }
    if trimmed.len() >= 40 {
        let bytes = decode_standard_base64(trimmed)?;
        return (bytes.len() == 32).then_some(bytes);
    }
    None
}

fn decode_standard_base64(text: &str) -> Option<Vec<u8>> {
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
