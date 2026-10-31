//! Client-specific JSON/TOML transforms.

use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value};

use super::Context;
use super::{PROVIDER_ID, PROVIDER_NAME};
use crate::agentcfg::{store, toml_env};

pub(super) fn apply_claude(path: &Path, ctx: &Context) -> Result<Vec<(PathBuf, String)>, String> {
    let mut data = store::load_json(path, "claude settings")?;
    let mut env = match data.get("env") {
        None | Some(Value::Null) => Map::new(),
        Some(Value::Object(map)) => map.clone(),
        Some(_) => return Err("claude settings env must be an object".into()),
    };
    let base = ctx
        .base_url
        .strip_suffix("/v1/")
        .or_else(|| ctx.base_url.strip_suffix("/v1"))
        .unwrap_or(&ctx.base_url);
    env.insert("ANTHROPIC_BASE_URL".into(), json!(base));
    env.insert("ANTHROPIC_AUTH_TOKEN".into(), json!(ctx.api_key));
    if let Some(model) = &ctx.model {
        for key in [
            "ANTHROPIC_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        ] {
            env.insert(key.into(), json!(model));
        }
    }
    data.insert("env".into(), Value::Object(env));
    Ok(vec![(
        path.to_path_buf(),
        store::dump_json(&Value::Object(data)),
    )])
}

pub(super) fn apply_codex(
    config_path: &Path,
    auth_path: &Path,
    ctx: &Context,
) -> Result<Vec<(PathBuf, String)>, String> {
    let mut text = store::read_text(config_path)?;
    text = toml_env::toml_upsert_top_key(&text, "model_provider", &json!(PROVIDER_ID))?;
    if let Some(model) = &ctx.model {
        text = toml_env::toml_upsert_top_key(&text, "model", &json!(model))?;
    }
    let table = format!("model_providers.{PROVIDER_ID}");
    let mapping = [
        ("name", json!(PROVIDER_NAME)),
        ("base_url", json!(ctx.base_url_v1)),
        ("wire_api", json!("responses")),
        ("experimental_bearer_token", json!(ctx.api_key)),
    ];
    text = toml_env::toml_upsert_table(&text, &table, &mapping)?;
    text = toml_env::toml_drop_table_keys(&text, &table, &["env_key", "env_key_instructions"]);
    let mut auth = store::load_json(auth_path, "codex auth")?;
    auth.insert("OPENAI_API_KEY".into(), json!(ctx.api_key));
    Ok(vec![
        (config_path.to_path_buf(), text),
        (
            auth_path.to_path_buf(),
            store::dump_json(&Value::Object(auth)),
        ),
    ])
}

pub(super) fn apply_opencode(path: &Path, ctx: &Context) -> Result<Vec<(PathBuf, String)>, String> {
    let mut data = store::load_json(path, "opencode config")?;
    data.entry("$schema")
        .or_insert_with(|| json!("https://opencode.ai/config.json"));
    let models = opencode_models(&ctx.models);
    let mut options = Map::new();
    options.insert("baseURL".into(), json!(ctx.base_url_v1));
    options.insert("apiKey".into(), json!(ctx.api_key));
    let mut provider = Map::new();
    provider.insert("npm".into(), json!("@ai-sdk/openai-compatible"));
    provider.insert("name".into(), json!(PROVIDER_NAME));
    provider.insert("options".into(), Value::Object(options));
    if !models.is_empty() {
        provider.insert("models".into(), Value::Object(models.clone()));
    }
    let mut provider_patch = Map::new();
    provider_patch.insert(PROVIDER_ID.into(), Value::Object(provider));
    let mut patch = Map::new();
    patch.insert("provider".into(), Value::Object(provider_patch));
    data = deep_merge(data, patch);
    if let Some(entry) = data
        .get_mut("provider")
        .and_then(Value::as_object_mut)
        .and_then(|providers| providers.get_mut(PROVIDER_ID))
        .and_then(Value::as_object_mut)
    {
        if models.is_empty() {
            entry.remove("models");
        } else {
            entry.insert("models".into(), Value::Object(models));
        }
    }
    if let Some(model) = &ctx.model {
        data.insert("model".into(), json!(format!("{PROVIDER_ID}/{model}")));
    }
    Ok(vec![(
        path.to_path_buf(),
        store::dump_json(&Value::Object(data)),
    )])
}

fn opencode_models(items: &[Value]) -> Map<String, Value> {
    let mut models = Map::new();
    for item in items {
        let Some(id) = item
            .get("id")
            .and_then(Value::as_str)
            .filter(|v| !v.is_empty())
        else {
            continue;
        };
        let context = positive_int(item.get("context_window"));
        let output = positive_int(item.get("max_output"));
        let mut limit = Map::new();
        if let Some(value) = context {
            limit.insert("context".into(), json!(value));
        }
        if let Some(value) = output {
            limit.insert("output".into(), json!(value));
        }
        let entry = if limit.is_empty() {
            json!({})
        } else {
            json!({"limit": Value::Object(limit)})
        };
        models.insert(id.into(), entry);
    }
    models
}

fn positive_int(value: Option<&Value>) -> Option<i64> {
    let value = value?;
    let parsed = match value {
        Value::Number(number) => number.as_i64(),
        Value::String(text) => text.parse::<i64>().ok(),
        _ => None,
    }?;
    (parsed > 0).then_some(parsed)
}

pub(super) fn apply_crush(path: &Path, ctx: &Context) -> Result<Vec<(PathBuf, String)>, String> {
    let data = store::load_json(path, "crush config")?;
    let models = ctx
        .models
        .iter()
        .filter_map(crush_model)
        .collect::<Vec<_>>();
    let mut provider_map = Map::new();
    provider_map.insert("name".into(), json!(PROVIDER_NAME));
    provider_map.insert("type".into(), json!("openai"));
    provider_map.insert("base_url".into(), json!(ctx.base_url_v1));
    provider_map.insert("api_key".into(), json!(ctx.api_key));
    provider_map.insert("models".into(), json!(models));
    let provider = Value::Object(provider_map);
    let mut providers = Map::new();
    providers.insert(PROVIDER_ID.into(), provider);
    let mut patch = Map::new();
    patch.insert("providers".into(), Value::Object(providers));
    let merged = deep_merge(data, patch);
    Ok(vec![(
        path.to_path_buf(),
        store::dump_json(&Value::Object(merged)),
    )])
}

fn crush_model(item: &Value) -> Option<Value> {
    let id = item
        .get("id")
        .and_then(Value::as_str)
        .filter(|v| !v.is_empty())?;
    let mut out = Map::new();
    out.insert("id".into(), json!(id));
    out.insert("name".into(), json!(id));
    if let Some(value) = item.get("context_window").filter(|v| truthy(v)) {
        out.insert("context_window".into(), value.clone());
    }
    if let Some(value) = item.get("max_output").filter(|v| truthy(v)) {
        out.insert("default_max_tokens".into(), value.clone());
    }
    Some(Value::Object(out))
}

pub(super) fn apply_zcode(path: &Path, ctx: &Context) -> Result<Vec<(PathBuf, String)>, String> {
    let original = store::load_json(path, "zcode provider config")?;
    let mut data = zcode_document(original);
    let rule = zcode_rule(ctx, &zcode_model_ids(&ctx.models));
    let config = data
        .get_mut("config")
        .and_then(Value::as_object_mut)
        .ok_or_else(|| {
            format!(
                "zcode provider config has no config object: {}",
                path.display()
            )
        })?;
    let holder = config
        .get_mut("providerConfigRules")
        .and_then(Value::as_object_mut)
        .ok_or_else(|| {
            format!(
                "zcode provider config has no providerConfigRules: {}",
                path.display()
            )
        })?;
    let rules = holder
        .get_mut("providerRules")
        .and_then(Value::as_array_mut)
        .ok_or_else(|| {
            format!(
                "zcode providerConfigRules has no providerRules list: {}",
                path.display()
            )
        })?;
    upsert_zcode_rule(rules, rule);
    ensure_zcode_order(config);
    Ok(vec![(
        path.to_path_buf(),
        store::dump_json(&Value::Object(data)),
    )])
}

fn zcode_document(original: Map<String, Value>) -> Map<String, Value> {
    if !original.is_empty() {
        return original;
    }
    json!({
        "schemaVersion": 1,
        "config": {
            "providerOrder": [],
            "providerConfigRules": {"providerRules": []}
        }
    })
    .as_object()
    .cloned()
    .unwrap_or_default()
}

fn upsert_zcode_rule(rules: &mut Vec<Value>, rule: Value) {
    let mut replaced = false;
    for existing in rules.iter_mut() {
        if existing.get("providerId").and_then(Value::as_str) == Some(PROVIDER_ID) {
            if let (Some(base), Some(patch)) = (existing.as_object_mut(), rule.as_object()) {
                *base = deep_merge(base.clone(), patch.clone());
            }
            replaced = true;
            break;
        }
    }
    if !replaced {
        rules.push(rule);
    }
}

fn ensure_zcode_order(config: &mut Map<String, Value>) {
    let order = config.entry("providerOrder").or_insert_with(|| json!([]));
    if !order.is_array() {
        *order = json!([]);
    }
    let order = order.as_array_mut().expect("providerOrder array");
    if !order
        .iter()
        .any(|value| value.as_str() == Some(PROVIDER_ID))
    {
        order.push(json!(PROVIDER_ID));
    }
}

fn zcode_model_ids(items: &[Value]) -> Vec<String> {
    let mut ids = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for item in items {
        let raw = item.get("id").unwrap_or(item);
        let id = raw
            .as_str()
            .map(str::trim)
            .map(str::to_string)
            .unwrap_or_else(|| scalar_text(raw));
        if !id.is_empty() && seen.insert(id.clone()) {
            ids.push(id);
        }
    }
    ids
}

fn zcode_rule(ctx: &Context, ids: &[String]) -> Value {
    let mut config = Map::new();
    config.insert("group".into(), json!("standard-personal"));
    config.insert(
        "access".into(),
        json!({"type": "api-key", "apiKey": ctx.api_key}),
    );
    config.insert(
        "api".into(),
        json!({"type": "openai-chat-completions", "baseUrl": ctx.base_url_v1}),
    );
    if !ids.is_empty() {
        config.insert("personalModelIds".into(), json!(ids));
        config.insert("modelOrder".into(), json!(ids));
    }
    json!({
        "providerId": PROVIDER_ID,
        "providerName": PROVIDER_NAME,
        "config": config,
    })
}

fn deep_merge(base: Map<String, Value>, patch: Map<String, Value>) -> Map<String, Value> {
    let mut out = base;
    for (key, value) in patch {
        match (out.get_mut(&key), value) {
            (Some(Value::Object(base)), Value::Object(patch)) => {
                *base = deep_merge(std::mem::take(base), patch);
            }
            (Some(slot), value) => *slot = value,
            (None, value) => {
                out.insert(key, value);
            }
        }
    }
    out
}

pub(super) fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(v) => *v,
        Value::Number(v) => v.as_f64().unwrap_or(0.0) != 0.0,
        Value::String(v) => !v.is_empty(),
        Value::Array(v) => !v.is_empty(),
        Value::Object(v) => !v.is_empty(),
    }
}

fn scalar_text(value: &Value) -> String {
    match value {
        Value::Null => String::new(),
        Value::String(text) => text.clone(),
        Value::Bool(v) => {
            if *v {
                "True".into()
            } else {
                "False".into()
            }
        }
        Value::Number(v) => v.to_string(),
        _ => String::new(),
    }
}
