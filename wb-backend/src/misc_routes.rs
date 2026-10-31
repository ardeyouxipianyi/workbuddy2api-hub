//! Small panel/read routes that do not own state of their own.

use std::path::{Path, PathBuf};

use http_body_util::{BodyExt, Full};
use hyper::body::Bytes;
use hyper::header::{HeaderValue, CONTENT_DISPOSITION, CONTENT_TYPE};
use hyper::{Response, StatusCode};
use serde_json::{json, Value};

use crate::http::{json_resp, Resp};
use crate::App;

fn query_value<'a>(query: &'a str, name: &str) -> &'a str {
    query
        .split('&')
        .find_map(|pair| {
            let (key, value) = pair.split_once('=')?;
            (key == name).then_some(value)
        })
        .unwrap_or("")
}

fn phone_start(bytes: &[u8], start: usize) -> Option<usize> {
    if start > 0 && (bytes[start - 1].is_ascii_alphanumeric() || bytes[start - 1] == b'_') {
        return None;
    }
    let separators = |byte: u8| matches!(byte, b' ' | b'\t' | b'-' | b'.' | 0xe2);
    let mut at = start;
    for (index, group) in [3usize, 4, 4].into_iter().enumerate() {
        for _ in 0..group {
            if !bytes.get(at).is_some_and(u8::is_ascii_digit) {
                return None;
            }
            at += 1;
        }
        if index < 2 {
            if bytes.get(at).is_some_and(|byte| separators(*byte)) {
                if *bytes.get(at).unwrap() == 0xe2
                    && bytes.get(at + 1..at + 3) != Some(&[0x80, 0x99][..])
                {
                    return None;
                }
                at += if bytes[at] == 0xe2 { 3 } else { 1 };
            }
        }
    }
    if bytes
        .get(at)
        .is_some_and(|byte| byte.is_ascii_alphanumeric() || *byte == b'_')
    {
        None
    } else {
        Some(at)
    }
}

/// Python `_mask_text` 的号码脱敏规则，只处理三个 3/4/4 数字组。
pub(crate) fn mask_phones(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = String::with_capacity(text.len());
    let mut copied = 0;
    let mut at = 0;
    while at < bytes.len() {
        if bytes[at].is_ascii_digit() {
            if let Some(end) = phone_start(bytes, at) {
                let digits: Vec<u8> = bytes[at..end]
                    .iter()
                    .copied()
                    .filter(u8::is_ascii_digit)
                    .collect();
                if digits[0] != b'1' || !matches!(digits[1], b'3'..=b'9') {
                    at += 1;
                    continue;
                }
                out.push_str(&text[copied..at]);
                out.push_str(std::str::from_utf8(&digits[..3]).unwrap_or(""));
                out.push_str("****");
                out.push_str(std::str::from_utf8(&digits[7..]).unwrap_or(""));
                copied = end;
                at = end;
                continue;
            }
        }
        at += 1;
    }
    out.push_str(&text[copied..]);
    out
}

fn export_filename(stamp: &str) -> String {
    let digits: String = stamp
        .chars()
        .filter(char::is_ascii_digit)
        .take(14)
        .collect();
    format!("wb-proxy-{digits}.log")
}

fn log_line(entry: &Value) -> String {
    let ts = entry
        .get("ts")
        .and_then(Value::as_str)
        .map(str::to_string)
        .or_else(|| {
            entry
                .get("ts")
                .and_then(Value::as_i64)
                .map(|stamp| crate::state::local_stamp_for(stamp).replace('T', " "))
        })
        .unwrap_or_default();
    format!(
        "[{}] [{}] [{}] {}",
        ts,
        entry.get("level").and_then(Value::as_str).unwrap_or(""),
        entry.get("tag").and_then(Value::as_str).unwrap_or(""),
        mask_phones(entry.get("msg").and_then(Value::as_str).unwrap_or("")),
    )
}

fn file_response(body: Vec<u8>, content_type: &'static str, disposition: String) -> Resp {
    let mut response = Response::new(Full::new(Bytes::from(body)).boxed());
    *response.status_mut() = StatusCode::OK;
    response
        .headers_mut()
        .insert(CONTENT_TYPE, HeaderValue::from_static(content_type));
    if let Ok(value) = HeaderValue::from_str(&disposition) {
        response.headers_mut().insert(CONTENT_DISPOSITION, value);
    }
    response
}

/// `GET /logs/export`: the current in-memory ring as a plain-text attachment.
pub fn logs_export(app: &App) -> Resp {
    let ring = app
        .logs
        .lock()
        .map(|items| items.iter().map(log_line).collect::<Vec<_>>())
        .unwrap_or_default();
    let body = ring.join("\n").into_bytes();
    let filename = export_filename(&crate::state::local_stamp());
    file_response(
        body,
        "text/plain; charset=utf-8",
        format!("attachment; filename=\"{filename}\""),
    )
}

/// `GET /activity`: compact account/activity snapshot. Python exposes the
/// detailed rows at `/activity/history`; this endpoint keeps the same filters
/// and returns that projection under a dedicated `activity` key.
pub fn activity(app: &App, query: &str) -> Result<Value, String> {
    let rows = crate::activity::query(
        app,
        query_value(query, "range"),
        query_value(query, "uid"),
        query_value(query, "task"),
        query_value(query, "result"),
        query_value(query, "limit"),
    )?;
    Ok(activity_payload(rows))
}

fn activity_payload(rows: Value) -> Value {
    json!({
        "activity": rows.get("rows").cloned().unwrap_or_else(|| json!([])),
        "total": rows.get("total").cloned().unwrap_or(json!(0)),
        "limit": rows.get("limit").cloned().unwrap_or(json!(100)),
        "range": rows.get("range").cloned().unwrap_or(json!("7d")),
        "filters": rows.get("filters").cloned().unwrap_or_else(|| json!({})),
    })
}

fn agents_cli_path() -> PathBuf {
    std::env::var("WB_AGENTS_CLI_PATH")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| Path::new("wb_agents.py").to_path_buf())
}

/// `GET /agents/cli` is public by contract so another machine can fetch the
/// standalone configurator. Default: `WB_AGENTS_CLI_PATH=wb_agents.py`.
pub fn agents_cli() -> Resp {
    agents_cli_from(&agents_cli_path())
}

fn agents_cli_from(path: &Path) -> Resp {
    match std::fs::read(path) {
        Ok(body) => file_response(
            body,
            "text/plain; charset=utf-8",
            "inline; filename=\"wb_agents.py\"".to_string(),
        ),
        Err(error) => json_resp(
            StatusCode::INTERNAL_SERVER_ERROR,
            &json!({"error": {
                "message": format!("wb_agents.py unavailable: {error}"),
                "type": "invalid_request_error",
                "code": 500,
            }}),
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn masks_phone_numbers_like_python_exports() {
        assert_eq!(
            mask_phones("老王(13812345678) 138-1234-5678 139 1234 5678"),
            "老王(138****5678) 138****5678 139****5678"
        );
        assert_eq!(mask_phones("uid=13812345678"), "uid=138****5678");
        assert_eq!(
            mask_phones("6ef20bf4-76fa-4085-b179-97517476cc1e"),
            "6ef20bf4-76fa-4085-b179-97517476cc1e"
        );
        assert_eq!(mask_phones("138123456780"), "138123456780");
        assert_eq!(
            mask_phones("账号179-97517476已停用"),
            "账号179****7476已停用"
        );
    }

    #[test]
    fn export_line_and_filename_match_python_shape() {
        let line = log_line(&json!({
            "ts":"2026-10-31 10:11:12","level":"WARN","tag":"tasks",
            "msg":"nick 13812345678 failed"
        }));
        assert_eq!(
            line,
            "[2026-10-31 10:11:12] [WARN] [tasks] nick 138****5678 failed"
        );
        assert_eq!(
            export_filename("2026-10-31 10:11:12"),
            "wb-proxy-20261031101112.log"
        );
        let numeric = log_line(&json!({
            "ts":1_793_440_842_i64,"level":"INFO","tag":"system","msg":"ok"
        }));
        assert!(!numeric.contains('T'));
    }

    #[test]
    fn activity_keeps_python_filter_envelope() {
        let payload = activity_payload(json!({
            "rows":[], "total":0, "limit":100, "range":"7d",
            "filters":{"uid":"","task":"","result":""}
        }));
        assert_eq!(
            payload
                .as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .collect::<Vec<_>>(),
            ["activity", "total", "limit", "range", "filters"]
        );
        assert_eq!(payload["filters"]["task"], "");
    }

    #[tokio::test]
    async fn agents_cli_serves_bytes_with_python_headers() {
        let path = std::env::temp_dir().join(format!(
            "wb-agent-cli-{}-{}",
            std::process::id(),
            crate::sse::hex_token(8)
        ));
        std::fs::write(&path, b"#!/usr/bin/env python3\nprint('ok')\n").unwrap();
        let response = agents_cli_from(&path);
        let _ = std::fs::remove_file(path);
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response.headers()[CONTENT_TYPE],
            "text/plain; charset=utf-8"
        );
        assert_eq!(
            response.headers()[CONTENT_DISPOSITION],
            "inline; filename=\"wb_agents.py\""
        );
        let body = response.into_body().collect().await.unwrap().to_bytes();
        assert_eq!(&body[..], b"#!/usr/bin/env python3\nprint('ok')\n");
    }
}
