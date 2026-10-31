//! Model selection cleanup and catalog enrichment for apply requests.

use std::collections::BTreeMap;

use serde_json::{json, Map, Value};

use crate::App;

const INTL_CATALOG: &str = include_str!("../../assets/catalog-intl.json");
const CN_CATALOG: &str = include_str!("../../assets/catalog-cn.json");

pub(super) fn configured_models_or_all(app: &App) -> Vec<Value> {
    match app
        .state
        .settings_snapshot()
        .get("agent_model_selection")
        .and_then(Value::as_array)
    {
        Some(selected) => selected
            .iter()
            .filter_map(Value::as_str)
            .map(|id| json!({"id": id.trim()}))
            .filter(|entry| !entry["id"].as_str().unwrap_or("").is_empty())
            .collect(),
        None => model_specs(),
    }
}

pub(super) fn clean_models(raw: &[Value]) -> Vec<Value> {
    let limit = std::env::var("WB_AGENT_MODEL_LIMIT")
        .ok()
        .and_then(|text| text.parse::<usize>().ok())
        .unwrap_or(80);
    raw.iter()
        .take(limit)
        .filter_map(|item| match item {
            Value::Object(map) if truthy(map.get("id")) => Some(item.clone()),
            Value::String(text) if !text.trim().is_empty() => Some(json!({"id": text.trim()})),
            _ => None,
        })
        .collect()
}

fn truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(v)) => *v,
        Some(Value::Number(v)) => v.as_f64().unwrap_or(0.0) != 0.0,
        Some(Value::String(v)) => !v.is_empty(),
        Some(Value::Array(v)) => !v.is_empty(),
        Some(Value::Object(v)) => !v.is_empty(),
    }
}

pub(super) fn enrich_models(models: &mut [Value]) {
    let mut specs = BTreeMap::new();
    for spec in model_specs() {
        if let Some(id) = spec.get("id").and_then(Value::as_str) {
            specs.insert(id.to_string(), spec);
        }
    }
    for item in models {
        let Some(id) = item.get("id").and_then(Value::as_str) else {
            continue;
        };
        let Some(spec) = specs.get(id) else {
            continue;
        };
        let Some(overrides) = item.as_object() else {
            continue;
        };
        let mut merged = spec.as_object().cloned().unwrap_or_default();
        for (key, value) in overrides {
            if !value.is_null() && value.as_str() != Some("") {
                merged.insert(key.clone(), value.clone());
            }
        }
        *item = Value::Object(merged);
    }
}

fn model_specs() -> Vec<Value> {
    let mut seen = std::collections::HashSet::new();
    let mut out = Vec::new();
    for raw in [INTL_CATALOG, CN_CATALOG] {
        let Ok(items) = serde_json::from_str::<Vec<Value>>(raw) else {
            continue;
        };
        for meta in items {
            let Some(id) = meta.get("id").and_then(Value::as_str) else {
                continue;
            };
            if id.is_empty() || !seen.insert(id.to_string()) {
                continue;
            }
            out.push(model_spec(id, &meta));
        }
    }
    out
}

fn model_spec(id: &str, meta: &Value) -> Value {
    let mut out = Map::new();
    out.insert("id".into(), json!(id));
    if let Some(value) = meta
        .get("context_length")
        .or_else(|| meta.get("max_input_tokens"))
        .filter(|value| truthy(Some(value)))
    {
        out.insert("context_window".into(), value.clone());
    }
    if let Some(value) = meta
        .get("max_output_tokens")
        .filter(|value| truthy(Some(value)))
    {
        out.insert("max_output".into(), value.clone());
    }
    if let Some(value) = meta.get("name").filter(|value| truthy(Some(value))) {
        out.insert("name".into(), value.clone());
    }
    if meta.get("supports_vision").and_then(Value::as_bool) == Some(true) {
        out.insert("supports_vision".into(), json!(true));
    }
    if let Some(levels) = meta
        .get("reasoning_efforts")
        .and_then(Value::as_array)
        .filter(|levels| !levels.is_empty())
    {
        out.insert("reasoning_efforts".into(), Value::Array(levels.clone()));
    }
    Value::Object(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fallback_specs_keep_python_catalog_order_and_deduplicate() {
        let specs = model_specs();
        let ids: Vec<&str> = specs
            .iter()
            .filter_map(|spec| spec.get("id").and_then(Value::as_str))
            .collect();
        assert_eq!(&ids[..3], ["hy4-preview-f", "hy3", "deepseek-v4.1-flash"]);
        let unique: std::collections::HashSet<_> = ids.iter().collect();
        assert_eq!(unique.len(), ids.len());
    }
}
