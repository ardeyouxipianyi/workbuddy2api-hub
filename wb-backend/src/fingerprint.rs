//! 提示指纹：`msgs_sha` / `system_sha` / `prefix_sha`（wb_proxy.py:10414-10430）。
//!
//! 哈希输入是 `json.dumps(obj, sort_keys=True, ensure_ascii=False)` 的**默认分隔符**
//! 形态（`", "` / `": "`），不是紧凑形态。用 serde_json 直接序列化会得到完全
//! 不同的 sha，所以这里手写一个只服务于指纹的规范化序列化器。

use serde_json::Value;
use sha2::{Digest, Sha256};

pub struct Fingerprint {
    pub msgs_sha: String,
    pub n_msgs: usize,
    pub system_sha: Option<String>,
    pub prefix_sha: Option<String>,
}

pub fn prompt_fingerprint(messages: &[Value]) -> Fingerprint {
    let msgs = Value::Array(messages.to_vec());
    let mut fp = Fingerprint {
        msgs_sha: hash12(&msgs),
        n_msgs: messages.len(),
        system_sha: None,
        prefix_sha: None,
    };
    if let Some(first) = messages.first() {
        fp.system_sha = Some(if first.get("role").and_then(Value::as_str) == Some("system") {
            hash12(first)
        } else {
            String::new()
        });
        fp.prefix_sha = Some(if messages.len() > 1 {
            hash12(&Value::Array(messages[..messages.len() - 1].to_vec()))
        } else {
            String::new()
        });
    }
    fp
}

fn hash12(value: &Value) -> String {
    let mut hasher = Sha256::new();
    hasher.update(canonical(value).as_bytes());
    let digest = hasher.finalize();
    let mut out = String::with_capacity(12);
    for byte in digest.iter().take(6) {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}

/// Python `json.dumps(sort_keys=True, ensure_ascii=False)` 的等价输出。
pub fn canonical(value: &Value) -> String {
    let mut out = String::new();
    write_value(value, &mut out);
    out
}

fn write_value(value: &Value, out: &mut String) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(n) => out.push_str(&number_repr(n)),
        Value::String(s) => write_string(s, out),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write_value(item, out);
            }
            out.push(']');
        }
        Value::Object(map) => {
            // sort_keys=True：按键的码点升序；serde_json 的 preserve_order 是插入序，
            // 所以这里必须自己排序。
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            out.push('{');
            for (i, key) in keys.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write_string(key, out);
                out.push_str(": ");
                write_value(&map[*key], out);
            }
            out.push('}');
        }
    }
}

fn number_repr(n: &serde_json::Number) -> String {
    if let Some(i) = n.as_i64() {
        return i.to_string();
    }
    if let Some(u) = n.as_u64() {
        return u.to_string();
    }
    let f = n.as_f64().unwrap_or(0.0);
    // Python 把整数值的 float 写成 "1.0"，Rust 的 `{}` 写 "1"。
    if f.fract() == 0.0 && f.abs() < 1e16 {
        format!("{f:.1}")
    } else {
        format!("{f}")
    }
}

fn write_string(s: &str, out: &mut String) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_python_dumps() {
        // 期望值来自 Python: json.dumps(obj, sort_keys=True, ensure_ascii=False)
        let value: Value = serde_json::from_str(r#"{"role":"system","content":"hi"}"#).unwrap();
        assert_eq!(canonical(&value), r#"{"content": "hi", "role": "system"}"#);
        assert_eq!(hash12(&value), "141f34976eff");
        assert_eq!(hash12(&Value::Array(vec![])), "4f53cda18c2b");
        let nested: Value = serde_json::from_str(r#"{"b":[1,2],"a":{"z":true,"y":null}}"#).unwrap();
        assert_eq!(canonical(&nested), r#"{"a": {"y": null, "z": true}, "b": [1, 2]}"#);
        let floatish: Value = serde_json::from_str(r#"{"x":1.0,"y":0.5}"#).unwrap();
        assert_eq!(canonical(&floatish), r#"{"x": 1.0, "y": 0.5}"#);
    }
}
