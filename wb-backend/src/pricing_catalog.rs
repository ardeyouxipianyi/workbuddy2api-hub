//! OpenRouter catalogue matching and snapshot construction.

use std::collections::BTreeMap;

use serde_json::{json, Map, Value};

use crate::pricing_engine::{bands_from_overrides, rates_from, Defaults, USD_CNY};

pub fn normalize(name: &str) -> String {
    name.chars()
        .filter(|ch| ch.is_ascii_alphanumeric())
        .flat_map(char::to_lowercase)
        .collect()
}

pub fn index_openrouter(or_models: &Map<String, Value>) -> BTreeMap<String, Vec<String>> {
    let mut by_norm: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for id in or_models.keys() {
        if id.contains(':') {
            continue;
        }
        let leaf = id.rsplit('/').next().unwrap_or(id);
        by_norm.entry(normalize(leaf)).or_default().push(id.clone());
    }
    by_norm
}

fn variant_bases(model: &str, defaults: &Defaults) -> Vec<String> {
    let mut out = Vec::new();
    for (suffix, enabled) in &defaults.variant_suffixes {
        if !enabled || !model.ends_with(suffix) {
            continue;
        }
        let base = model[..model.len() - suffix.len()].trim();
        if !base.is_empty() {
            out.push(base.to_string());
        }
    }
    out
}

fn resolve_once(
    model: &str,
    or_models: &Map<String, Value>,
    by_norm: &BTreeMap<String, Vec<String>>,
    overrides: &BTreeMap<String, String>,
    defaults: &Defaults,
) -> (Option<String>, bool) {
    for table in [overrides, &defaults.overrides] {
        if let Some(reference) = table.get(model) {
            if or_models.contains_key(reference) {
                return (Some(reference.clone()), true);
            }
        }
    }
    let leaf = model.rsplit('/').next().unwrap_or(model);
    match by_norm.get(&normalize(leaf)).map(Vec::as_slice) {
        Some([only]) => (Some(only.clone()), false),
        _ => (None, false),
    }
}

pub fn resolve(
    model: &str,
    or_models: &Map<String, Value>,
    by_norm: &BTreeMap<String, Vec<String>>,
    overrides: &BTreeMap<String, String>,
    defaults: &Defaults,
    variants: bool,
) -> (Option<String>, bool, Option<String>) {
    let (direct, overridden) = resolve_once(model, or_models, by_norm, overrides, defaults);
    if direct.is_some() || !variants {
        return (direct, overridden, None);
    }
    for base in variant_bases(model, defaults) {
        let (reference, _) = resolve_once(&base, or_models, by_norm, overrides, defaults);
        if reference.is_some() {
            return (reference, true, Some(base));
        }
    }
    (None, false, None)
}

pub fn build_snapshot(
    or_models: &Map<String, Value>,
    model_ids: &[String],
    overrides: &BTreeMap<String, String>,
    defaults: &Defaults,
    variants: bool,
    generated_at: &str,
    source_url: &str,
) -> (Value, Vec<String>, Vec<String>) {
    let by_norm = index_openrouter(or_models);
    let mut models = Map::new();
    let mut unpriced = Vec::new();
    let mut overridden_models = Vec::new();
    for model in model_ids {
        let (reference, overridden, inherited) =
            resolve(model, or_models, &by_norm, overrides, defaults, variants);
        let Some(reference) = reference else {
            unpriced.push(model.clone());
            continue;
        };
        if overridden {
            overridden_models.push(model.clone());
        }
        let source = or_models
            .get(&reference)
            .cloned()
            .unwrap_or_else(|| json!({}));
        models.insert(
            model.clone(),
            entry_for(model, &reference, &source, inherited.as_deref(), overridden),
        );
    }
    let doc = json!({
        "meta": {
            "generated_at": generated_at,
            "base_currency": "CNY",
            "usd_cny": USD_CNY,
            "sources": {"openrouter": source_url},
            "note": "单价均为「每百万 token」的美元价，取自 OpenRouter 公布的模型级价（OpenRouter 定义为该模型 top provider 的价，多 provider 模型通常接近最低档；它是参考价，不等于 WorkBuddy 官方价或结算价）；按 usd_cny 折算成人民币；按条件定价的模型（输入长度阈值或 UTC 时段）另带 bands，按每条请求的输入长度与时间取档。OpenRouter 的缓存写入价与音频价，本地 usage 日志没有对应的 token 数，未计入。",
        },
        "models": models,
    });
    (doc, unpriced, overridden_models)
}

fn entry_for(
    model: &str,
    reference: &str,
    source: &Value,
    inherited: Option<&str>,
    overridden: bool,
) -> Value {
    let mut entry = Map::new();
    entry.insert("display".into(), json!(model));
    entry.insert("source".into(), json!("openrouter"));
    entry.insert("currency".into(), json!("USD"));
    entry.insert("unit".into(), json!(1_000_000));
    entry.insert("or_id".into(), json!(reference));
    entry.insert("flat".into(), rates_from(source));
    let bands = bands_from_overrides(source);
    if !bands.is_empty() {
        entry.insert("bands".into(), Value::Array(bands));
    }
    if let Some(base) = inherited {
        entry.insert("via".into(), json!("variant"));
        entry.insert("inherited_from".into(), json!(base));
    } else if overridden {
        entry.insert("via".into(), json!("override"));
        entry.insert("override_from".into(), json!(model));
    }
    Value::Object(entry)
}
