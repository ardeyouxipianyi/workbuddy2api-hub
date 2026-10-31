//! Static defaults shared with the Python panel implementation.

use serde_json::{json, Map, Value};

const BUILTIN_PROMPTS: &str = include_str!("../assets/webprompts-zh.json");

pub(crate) fn builtin_prompts() -> Vec<String> {
    serde_json::from_str(BUILTIN_PROMPTS).unwrap_or_default()
}

pub(crate) fn identity_version_defaults() -> Value {
    json!({
        "desktop_intl": "5.8.4",
        "desktop_cn": "5.8.3",
        "desktop_cli": "2.161.0",
        "vscode_product": "4.12.39997873",
        "vscode_ide": "1.119.0",
        "cli": "2.168.2",
        "sdk": "6.25.0",
        "node": "v22.21.1",
    })
}

pub(crate) fn mapping(settings: &Map<String, Value>, key: &str, defaults: Value) -> Value {
    let Some(stored) = settings.get(key).and_then(Value::as_object) else {
        return defaults;
    };
    let mut out = defaults.as_object().cloned().unwrap_or_default();
    for (name, value) in stored {
        if out.contains_key(name) {
            out.insert(name.clone(), value.clone());
        }
    }
    Value::Object(out)
}

pub(crate) fn identity(settings: &Map<String, Value>) -> Value {
    let stored = settings.get("identity").and_then(Value::as_object);
    let private_data = stored
        .and_then(|value| value.get("private_data"))
        .and_then(Value::as_str)
        .filter(|value| matches!(*value, "auto" | "true" | "false"))
        .unwrap_or("auto");
    let versions = stored
        .and_then(|value| value.get("versions"))
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    json!({"private_data": private_data, "versions": versions})
}

pub(crate) fn null_or_i64(value: Option<&Value>) -> Value {
    value
        .filter(|value| value.is_i64())
        .cloned()
        .unwrap_or(Value::Null)
}
