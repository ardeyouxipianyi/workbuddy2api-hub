//! DSH profile patch and legacy YAML writers.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value};

use super::transform::truthy;
use super::{Context, CREDENTIAL_REF, PROVIDER_ID, PROVIDER_NAME};
use crate::agentcfg::store;
use crate::agentcfg::yaml::{self, YamlValue};

pub(super) struct Paths {
    pub credentials: PathBuf,
    pub settings: PathBuf,
    pub patches: Vec<PathBuf>,
}

impl Paths {
    pub(super) fn resolve(home: &Path) -> Self {
        let root = home.join(".dsh");
        let settings = root.join("settings.yaml");
        let mut patches = profile_patches(home);
        let include_settings = patches.is_empty() || settings.is_file();
        if !include_settings {
            patches.retain(|path| path != &settings);
        }
        Self {
            credentials: root.join(".credentials.yaml"),
            settings,
            patches,
        }
    }

    pub(super) fn target_paths(&self) -> Vec<PathBuf> {
        let mut paths = vec![self.credentials.clone()];
        paths.extend(self.patches.iter().cloned());
        if self.patches.is_empty() || self.settings.is_file() {
            paths.push(self.settings.clone());
        }
        paths
    }
}

pub(super) fn apply(paths: &Paths, ctx: &Context) -> Result<Vec<(PathBuf, String)>, String> {
    let provider = provider(ctx);
    let mut contents = BTreeMap::new();
    for path in &paths.patches {
        contents.insert(
            path.clone(),
            patch_upsert(&store::read_text(path)?, &provider, ctx.model.as_deref())?,
        );
    }
    if paths.patches.is_empty() || paths.settings.is_file() {
        let mut text = yaml::yaml_upsert_inline(
            &store::read_text(&paths.settings)?,
            &format!("llm-pi-ai.providers.{PROVIDER_ID}"),
            &provider,
        )?;
        if let Some(model) = &ctx.model {
            let default = default_model(&text, PROVIDER_ID, model);
            text = yaml::yaml_upsert_inline(&text, "agent-default-model", &default)?;
        }
        contents.insert(paths.settings.clone(), text);
    }
    let credentials = yaml::yaml_refs_upsert(
        &store::read_text(&paths.credentials)?,
        CREDENTIAL_REF,
        &ctx.api_key,
    )?;
    contents.insert(paths.credentials.clone(), credentials);
    Ok(contents.into_iter().collect())
}

fn profile_patches(home: &Path) -> Vec<PathBuf> {
    let root = home.join(".dsh/profiles");
    let Ok(entries) = std::fs::read_dir(root) else {
        return Vec::new();
    };
    let mut names: Vec<_> = entries
        .filter_map(Result::ok)
        .filter(|entry| entry.path().is_dir())
        .filter_map(|entry| entry.file_name().into_string().ok())
        .collect();
    names.sort();
    names
        .into_iter()
        .map(|name| {
            home.join(".dsh/profiles")
                .join(name)
                .join("cordis.patch.yml")
        })
        .filter(|path| path.is_file())
        .collect()
}

fn provider(ctx: &Context) -> Value {
    let mut provider = Map::new();
    provider.insert("displayName".into(), json!(PROVIDER_NAME));
    provider.insert("apiKeyEnv".into(), json!(CREDENTIAL_REF));
    provider.insert("api".into(), json!("openai-completions"));
    provider.insert("baseURL".into(), json!(ctx.base_url_v1));
    provider.insert(
        "compat".into(),
        json!({"thinkingFormat": "openai", "supportsReasoningEffort": true}),
    );
    provider.insert(
        "retryPolicy".into(),
        json!({
            "mode": "normal",
            "maxRetries": 8,
            "backoff": {
                "initialDelayMs": 2000,
                "maxDelayMs": 30000,
                "jitterRatio": 0.1
            }
        }),
    );
    let models: Vec<Value> = ctx.models.iter().filter_map(model).collect();
    if !models.is_empty() {
        provider.insert("models".into(), json!(models));
    }
    Value::Object(provider)
}

fn model(entry: &Value) -> Option<Value> {
    let id = entry
        .get("id")
        .and_then(Value::as_str)
        .filter(|v| !v.is_empty())?;
    let mut item = Map::new();
    item.insert("id".into(), json!(id));
    if let Some(value) = entry.get("context_window").filter(|v| truthy(v)) {
        item.insert("contextWindow".into(), value.clone());
    }
    if let Some(value) = entry.get("max_output").filter(|v| truthy(v)) {
        if let Some(value) = integer(value) {
            item.insert("maxTokens".into(), json!(value));
        }
    }
    if let Some(levels) = reasoning_levels(entry.get("reasoning_efforts")) {
        item.insert("reasoningEfforts".into(), levels);
    }
    if entry.get("supports_vision").is_some_and(truthy)
        || entry.get("supportsVision").is_some_and(truthy)
    {
        item.insert("input".into(), json!(["text", "image"]));
    }
    Some(Value::Object(item))
}

fn integer(value: &Value) -> Option<i64> {
    match value {
        Value::Number(number) => number.as_i64(),
        Value::String(text) => text.parse().ok(),
        Value::Bool(v) => Some(i64::from(*v)),
        _ => None,
    }
}

fn reasoning_levels(raw: Option<&Value>) -> Option<Value> {
    let mut out = Map::new();
    match raw? {
        Value::Object(pairs) => {
            for (level, wire) in pairs {
                insert_level(&mut out, level, wire);
            }
        }
        Value::Array(levels) => {
            for level in levels {
                insert_level(&mut out, &scalar(level), &json!(scalar(level)));
            }
        }
        _ => return None,
    }
    (!out.is_empty()).then_some(Value::Object(out))
}

fn insert_level(out: &mut Map<String, Value>, level: &str, wire: &Value) {
    let name = level.trim();
    if name.is_empty() {
        return;
    }
    let wire = scalar(wire);
    out.insert(
        name.to_string(),
        json!(if wire.is_empty() { name } else { &wire }),
    );
}

fn scalar(value: &Value) -> String {
    match value {
        Value::Null => String::new(),
        Value::String(v) => v.trim().to_string(),
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

fn default_model(text: &str, provider: &str, model: &str) -> Value {
    let mut out = Map::new();
    out.insert("provider".into(), json!(provider));
    out.insert("model".into(), json!(model));
    for line in text.split('\n') {
        let Some(raw) = line.trim().strip_prefix("agent-default-model:") else {
            continue;
        };
        if raw.trim().starts_with('{') {
            if let Ok(Value::Object(mut current)) = serde_json::from_str::<Value>(raw.trim()) {
                current.insert("provider".into(), json!(provider));
                current.insert("model".into(), json!(model));
                return Value::Object(current);
            }
        }
        break;
    }
    Value::Object(out)
}

fn patch_upsert(text: &str, provider: &Value, model: Option<&str>) -> Result<String, String> {
    let mut lines: Vec<String> = text.split('\n').map(str::to_string).collect();
    if let Some(idx) = lines.iter().position(|line| line.trim() == "[]") {
        lines.remove(idx);
    }
    if lines.last().is_some_and(String::is_empty) {
        lines.pop();
    }
    if let Some((start, end)) = find_item(&lines, "llm-pi-ai") {
        lines = yaml::yaml_upsert_at(
            lines,
            start + 1,
            end,
            2,
            &["config", "providers", PROVIDER_ID],
            YamlValue::Block(provider),
        )?;
    } else {
        lines.extend(provider_entry(provider));
    }
    if let Some(model) = model {
        if let Some((start, end)) = find_item(&lines, "agent-default-model") {
            lines = yaml::yaml_upsert_at(
                lines,
                start + 1,
                end,
                2,
                &["config", "provider"],
                YamlValue::Json(&json!(PROVIDER_ID)),
            )?;
            let (start, end) = find_item(&lines, "agent-default-model")
                .ok_or_else(|| "agent-default-model entry disappeared".to_string())?;
            lines = yaml::yaml_upsert_at(
                lines,
                start + 1,
                end,
                2,
                &["config", "model"],
                YamlValue::Json(&json!(model)),
            )?;
        } else {
            lines.extend([
                "- id: agent-default-model".into(),
                "  config:".into(),
                format!("    provider: {}", yaml::yaml_scalar(&json!(PROVIDER_ID))),
                format!("    model: {}", yaml::yaml_scalar(&json!(model))),
            ]);
        }
    }
    Ok(format!("{}\n", lines.join("\n")))
}

fn provider_entry(provider: &Value) -> Vec<String> {
    let mut out = vec![
        "- id: llm-pi-ai".into(),
        format!(
            "  name: {}",
            yaml::yaml_scalar(&json!("@deepseek-ai/dsh-llm-pi-ai"))
        ),
        "  config:".into(),
        "    providers:".into(),
    ];
    let mut wrapper = Map::new();
    wrapper.insert(PROVIDER_ID.into(), provider.clone());
    out.extend(yaml::yaml_emit(&Value::Object(wrapper), 6).unwrap_or_default());
    out
}

type Range = (usize, usize);

fn patch_items(lines: &[String]) -> Vec<Range> {
    let starts: Vec<usize> = lines
        .iter()
        .enumerate()
        .filter(|(_, line)| line.starts_with("- ") || line.trim_end() == "-")
        .map(|(idx, _)| idx)
        .collect();
    starts
        .iter()
        .enumerate()
        .map(|(pos, &start)| (start, starts.get(pos + 1).copied().unwrap_or(lines.len())))
        .collect()
}

fn find_item(lines: &[String], wanted: &str) -> Option<Range> {
    patch_items(lines)
        .into_iter()
        .find(|&(start, end)| item_id(lines, start, end) == Some(wanted))
}

fn item_id(lines: &[String], start: usize, end: usize) -> Option<&str> {
    if let Some(raw) = lines[start].strip_prefix("- id:") {
        return Some(unquote(raw));
    }
    (start + 1..end).find_map(|idx| {
        if lines[idx].len() - lines[idx].trim_start_matches(' ').len() != 2 {
            return None;
        }
        let line = lines[idx].trim();
        line.strip_prefix("id:").map(unquote)
    })
}

fn unquote(raw: &str) -> &str {
    let raw = raw.trim();
    if raw.len() >= 2 {
        let first = raw.as_bytes()[0];
        if (first == b'"' || first == b'\'') && raw.as_bytes().last() == Some(&first) {
            return &raw[1..raw.len() - 1];
        }
    }
    raw
}
