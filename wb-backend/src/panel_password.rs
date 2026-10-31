//! 面板口令：与 `wb_settings.py` 相同的 PBKDF2-SHA256 文件格式与校验规则。
//! 这里用项目已有的 `sha2` 实现 HMAC/PBKDF2；Unix/Windows 分别取系统随机 salt。

#![allow(dead_code)] // 父进程尚未统一接线；登录与改密共用这些入口。

use std::sync::{Mutex, OnceLock};

use hyper::body::Incoming;
use hyper::{Request, StatusCode};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::http::{body_json, json_resp, Resp};
use crate::App;

fn operation_lock() -> &'static Mutex<()> {
    static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    LOCK.get_or_init(|| Mutex::new(()))
}

fn default_password() -> String {
    std::env::var("WB_DEFAULT_PANEL_PASSWORD")
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| "admin".into())
}

fn rounds() -> u32 {
    std::env::var("WB_PANEL_PBKDF2_ROUNDS")
        .ok()
        .and_then(|v| v.parse::<u32>().ok())
        .filter(|v| *v > 0)
        .unwrap_or(120_000)
}

fn hmac_sha256_parts(key: &[u8], first: &[u8], second: &[u8]) -> [u8; 32] {
    let mut block = [0_u8; 64];
    if key.len() > 64 {
        block[..32].copy_from_slice(&Sha256::digest(key));
    } else {
        block[..key.len()].copy_from_slice(key);
    }
    let mut inner_pad = [0_u8; 64];
    let mut outer_pad = [0_u8; 64];
    for (index, byte) in block.into_iter().enumerate() {
        inner_pad[index] = byte ^ 0x36;
        outer_pad[index] = byte ^ 0x5c;
    }
    let mut inner = Sha256::new();
    inner.update(inner_pad);
    inner.update(first);
    inner.update(second);
    let inner = inner.finalize();
    let mut outer = Sha256::new();
    outer.update(outer_pad);
    outer.update(inner);
    outer.finalize().into()
}

fn hmac_sha256(key: &[u8], message: &[u8]) -> [u8; 32] {
    hmac_sha256_parts(key, message, &[])
}

fn pbkdf2_sha256(password: &[u8], salt: &[u8], rounds: u32) -> [u8; 32] {
    let block_index = 1_u32.to_be_bytes();
    let mut u = hmac_sha256_parts(password, salt, &block_index);
    let mut out = u;
    for _ in 1..rounds {
        u = hmac_sha256(password, &u);
        for (dst, src) in out.iter_mut().zip(u) {
            *dst ^= src;
        }
    }
    out
}

fn hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(DIGITS[(byte >> 4) as usize] as char);
        out.push(DIGITS[(byte & 0x0f) as usize] as char);
    }
    out
}

fn unhex(text: &str) -> Option<Vec<u8>> {
    if text.len() % 2 != 0 {
        return None;
    }
    fn digit(byte: u8) -> Option<u8> {
        match byte {
            b'0'..=b'9' => Some(byte - b'0'),
            b'a'..=b'f' => Some(byte - b'a' + 10),
            b'A'..=b'F' => Some(byte - b'A' + 10),
            _ => None,
        }
    }
    text.as_bytes()
        .chunks_exact(2)
        .map(|pair| Some((digit(pair[0])? << 4) | digit(pair[1])?))
        .collect()
}

fn equal(left: &str, right: &str) -> bool {
    let left = left.as_bytes();
    let right = right.as_bytes();
    let mut diff = left.len() ^ right.len();
    for index in 0..left.len().max(right.len()) {
        diff |= left.get(index).copied().unwrap_or(0) as usize
            ^ right.get(index).copied().unwrap_or(0) as usize;
    }
    diff == 0
}

fn digest(password: &str, salt: &str, rounds: u32) -> Option<String> {
    let salt = unhex(salt)?;
    Some(hex(&pbkdf2_sha256(password.as_bytes(), &salt, rounds)))
}

fn settings_path(app: &App) -> std::path::PathBuf {
    app.state.accounts_dir.join("settings.json")
}

fn load_settings(app: &App) -> Map<String, Value> {
    std::fs::read(settings_path(app))
        .ok()
        .and_then(|raw| serde_json::from_slice::<Value>(&raw).ok())
        .and_then(|value| value.as_object().cloned())
        .unwrap_or_default()
}

pub fn verify(password: &str, settings: &Map<String, Value>) -> bool {
    let default = default_password();
    let Some(stored) = settings.get("panel_password_hash").and_then(Value::as_str) else {
        return password == default;
    };
    if stored.is_empty() {
        return password == default;
    }
    if settings
        .get("panel_password_default")
        .and_then(Value::as_bool)
        == Some(true)
    {
        return password == default;
    }
    let Some(salt) = settings.get("panel_password_salt").and_then(Value::as_str) else {
        return false;
    };
    if salt.is_empty() {
        return false;
    }
    let rounds = match settings.get("panel_password_rounds") {
        Some(Value::Number(v)) => v
            .as_u64()
            .filter(|v| *v > 0)
            .map(|v| v.min(u32::MAX as u64) as u32),
        Some(Value::String(v)) => v.trim().parse::<u32>().ok().filter(|v| *v > 0),
        _ => None,
    }
    .unwrap_or_else(rounds);
    digest(password, salt, rounds)
        .map(|given| equal(&given, stored))
        .unwrap_or(false)
}

/// 验证当前 app 的账号目录里的口令；登录接线也直接复用此入口。
pub fn verify_app(app: &App, password: &str) -> bool {
    verify(password, &load_settings(app))
}

pub fn is_default(settings: &Map<String, Value>) -> bool {
    let hash = settings
        .get("panel_password_hash")
        .and_then(Value::as_str)
        .unwrap_or("");
    hash.is_empty()
        || settings
            .get("panel_password_default")
            .and_then(Value::as_bool)
            == Some(true)
}

fn random_salt() -> [u8; 16] {
    let mut salt = [0_u8; 16];
    #[cfg(unix)]
    unsafe {
        use std::ffi::c_void;
        let ptr = salt.as_mut_ptr() as *mut c_void;
        let got = libc::getrandom(ptr, salt.len(), 0);
        if got == salt.len() as isize {
            return salt;
        }
    }
    #[cfg(windows)]
    unsafe {
        #[link(name = "bcrypt")]
        extern "system" {
            fn BCryptGenRandom(
                algorithm: *mut std::ffi::c_void,
                buffer: *mut u8,
                len: u32,
                flags: u32,
            ) -> i32;
        }
        const BCRYPT_USE_SYSTEM_PREFERRED_RNG: u32 = 0x0000_0002;
        if BCryptGenRandom(
            std::ptr::null_mut(),
            salt.as_mut_ptr(),
            salt.len() as u32,
            BCRYPT_USE_SYSTEM_PREFERRED_RNG,
        ) == 0
        {
            return salt;
        }
    }
    // 系统随机源不可用时仍不落明文；时间+PID+地址只作为最后兜底。
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let seed = format!("{stamp}|{}|{:p}", std::process::id(), &salt);
    salt.copy_from_slice(&Sha256::digest(seed.as_bytes())[..16]);
    salt
}

fn apply_password(
    settings: &mut Map<String, Value>,
    password: &str,
    salt_override: Option<[u8; 16]>,
) {
    if password == default_password() {
        settings.remove("panel_password_salt");
        settings.remove("panel_password_rounds");
        settings.insert("panel_password_hash".into(), json!(""));
        settings.insert("panel_password_default".into(), json!(true));
        return;
    }
    let salt = salt_override.unwrap_or_else(random_salt);
    let salt_hex = hex(&salt);
    let rounds = rounds();
    settings.insert("panel_password_salt".into(), json!(salt_hex));
    settings.insert("panel_password_rounds".into(), json!(rounds));
    settings.insert(
        "panel_password_hash".into(),
        json!(hex(&pbkdf2_sha256(password.as_bytes(), &salt, rounds))),
    );
    settings.insert("panel_password_default".into(), json!(false));
}

fn save(app: &App, settings: &Map<String, Value>) -> std::io::Result<()> {
    write_settings(&app.state.accounts_dir, settings)?;
    app.state.refresh();
    Ok(())
}

fn write_settings(
    accounts_dir: &std::path::Path,
    settings: &Map<String, Value>,
) -> std::io::Result<()> {
    std::fs::create_dir_all(accounts_dir)?;
    crate::state::write_json_atomic(
        &accounts_dir.join("settings.json"),
        &Value::Object(settings.clone()),
    )
}

fn token_of(req: &Request<Incoming>) -> Option<&str> {
    req.headers()
        .get("x-panel-token")
        .and_then(|v| v.to_str().ok())
}

fn py_text(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(v)) => v.clone(),
        Some(Value::Bool(true)) => "True".into(),
        Some(Value::Bool(false)) => String::new(),
        Some(Value::Number(v)) if v.as_f64() == Some(0.0) => String::new(),
        Some(v) => v.to_string(),
    }
}

fn error(code: StatusCode, message: &str, kind: &str) -> Resp {
    json_resp(
        code,
        &json!({"error": {
            "message": message,
            "type": kind,
            "code": code.as_u16()
        }}),
    )
}

/// `POST /panel/password` handler；成功时沿用当前已认证会话 token。
pub async fn handle(app: &std::sync::Arc<App>, req: Request<Incoming>) -> Resp {
    let token = match token_of(&req) {
        Some(token) if app.panel.token_ok(Some(token)) => token.to_string(),
        _ => {
            return error(
                StatusCode::UNAUTHORIZED,
                "panel password required",
                "invalid_request_error",
            )
        }
    };
    let body = match body_json(req).await {
        Ok(v) => v,
        Err(resp) => return resp,
    };
    let current = py_text(body.get("current"));
    let new = py_text(body.get("new"));
    let _guard = operation_lock().lock().unwrap_or_else(|e| e.into_inner());
    let mut settings = load_settings(app);
    if !verify(&current, &settings) {
        return error(
            StatusCode::UNAUTHORIZED,
            "current password is wrong",
            "invalid_request_error",
        );
    }
    if new.chars().count() < 4 {
        return error(
            StatusCode::BAD_REQUEST,
            "new password must be at least 4 characters",
            "invalid_request_error",
        );
    }
    apply_password(&mut settings, &new, None);
    if let Err(e) = save(app, &settings) {
        return error(
            StatusCode::INTERNAL_SERVER_ERROR,
            &format!("cannot save settings: {e}"),
            "server_error",
        );
    }
    app.log("panel", "INFO", "password changed");
    // shortcut: Panel 尚未暴露 revoke_all/create；父层接线时补全会话轮换。
    json_resp(
        StatusCode::OK,
        &json!({
            "ok": true,
            "token": token,
            "using_default_password": new == default_password()
        }),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn password_file_fields_and_default_round_trip_match_python() {
        let mut settings = Map::new();
        settings.insert("hand_written".into(), json!(true));
        apply_password(
            &mut settings,
            "secret123",
            Some([
                0x00, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88, 0x99, 0xaa, 0xbb, 0xcc, 0xdd,
                0xee, 0xff,
            ]),
        );
        let dir = std::env::temp_dir().join(format!(
            "wb-rust-panel-password-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ));
        write_settings(&dir, &settings).unwrap();
        let raw = std::fs::read_to_string(dir.join("settings.json")).unwrap();
        assert_eq!(
            raw,
            "{\n  \"hand_written\": true,\n  \"panel_password_salt\": \"00112233445566778899aabbccddeeff\",\n  \"panel_password_rounds\": 120000,\n  \"panel_password_hash\": \"ad2ec8bc24c820757ea314c265c46ae1762a256e680ab4e72054c60642e07ff0\",\n  \"panel_password_default\": false\n}"
        );
        let _ = std::fs::remove_dir_all(dir);
        assert!(verify("secret123", &settings));
        assert!(!verify("admin", &settings));
        apply_password(&mut settings, "admin", None);
        assert_eq!(
            settings.get("panel_password_hash").and_then(Value::as_str),
            Some("")
        );
        assert_eq!(
            settings
                .get("panel_password_default")
                .and_then(Value::as_bool),
            Some(true)
        );
        assert!(!settings.contains_key("panel_password_salt"));
        assert!(!settings.contains_key("panel_password_rounds"));
        assert!(verify("admin", &settings));
        assert!(settings.contains_key("hand_written"));
    }
}
