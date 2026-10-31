//! Pricing switches and refresh-interval normalization.

use serde_json::{json, Map, Value};

use crate::pricing::Pricing;
use crate::pricing_engine::as_f64;

const DEFAULT_REFRESH_MINUTES: f64 = 5.0;
const MAX_REFRESH_MINUTES: f64 = 43_200.0;

pub struct Settings {
    pub enabled: bool,
    pub variant_inherit: bool,
    pub refresh_minutes: f64,
}

pub fn read(settings: &Map<String, Value>) -> Settings {
    Settings {
        enabled: bool_setting(settings, "pricing_enabled", true),
        variant_inherit: bool_setting(settings, "pricing_variant_inherit", true),
        refresh_minutes: refresh_minutes(settings),
    }
}

pub fn enrich_settings(settings: &mut Map<String, Value>, pricing: &Pricing) {
    normalize_refresh_setting(settings);
    let values = pricing.settings(settings);
    settings.insert("pricing_enabled".into(), json!(values.enabled));
    settings.insert(
        "pricing_variant_inherit".into(),
        json!(values.variant_inherit),
    );
    settings.insert(
        "pricing_refresh_minutes".into(),
        json!(values.refresh_minutes),
    );
    settings.insert("usd_cny".into(), json!(pricing.usd_cny()));
}

pub fn normalize_refresh_setting(settings: &mut Map<String, Value>) -> bool {
    if settings.contains_key("pricing_refresh_minutes") {
        return false;
    }
    let Some(raw) = settings.remove("pricing_refresh_hours") else {
        return false;
    };
    if let Some(hours) = numeric_setting(&raw) {
        let minutes = (hours * 60.0).clamp(0.0, MAX_REFRESH_MINUTES);
        settings.insert("pricing_refresh_minutes".into(), json!(minutes));
    }
    true
}

pub fn apply_setting(
    settings: &mut Map<String, Value>,
    key: &str,
    value: &Value,
) -> Option<Result<String, String>> {
    match key {
        "pricing_enabled" | "pricing_variant_inherit" => {
            let Some(enabled) = value.as_bool() else {
                return Some(Err(format!("{key} must be true or false")));
            };
            settings.insert(key.to_string(), json!(enabled));
            Some(Ok(key.to_string()))
        }
        "pricing_refresh_minutes" | "pricing_refresh_hours" => {
            let Some(mut minutes) = numeric_setting(value) else {
                return Some(Err(format!("{key} must be a number")));
            };
            if minutes < 0.0 {
                return Some(Err("pricing_refresh_minutes cannot be negative".into()));
            }
            if key == "pricing_refresh_hours" {
                minutes *= 60.0;
            }
            minutes = minutes.clamp(0.0, MAX_REFRESH_MINUTES);
            settings.remove("pricing_refresh_hours");
            settings.insert("pricing_refresh_minutes".into(), json!(minutes));
            Some(Ok("pricing_refresh_minutes".into()))
        }
        _ => None,
    }
}

fn bool_setting(settings: &Map<String, Value>, key: &str, default: bool) -> bool {
    match settings.get(key) {
        None => default,
        Some(Value::Bool(value)) => *value,
        Some(_) => false,
    }
}

fn refresh_minutes(settings: &Map<String, Value>) -> f64 {
    if let Some(value) = settings.get("pricing_refresh_minutes") {
        return numeric_setting(value)
            .map(|minutes| minutes.max(0.0).min(MAX_REFRESH_MINUTES))
            .unwrap_or(DEFAULT_REFRESH_MINUTES);
    }
    if let Some(value) = settings.get("pricing_refresh_hours") {
        return numeric_setting(value)
            .map(|hours| (hours * 60.0).max(0.0).min(MAX_REFRESH_MINUTES))
            .unwrap_or(DEFAULT_REFRESH_MINUTES);
    }
    DEFAULT_REFRESH_MINUTES
}

fn numeric_setting(value: &Value) -> Option<f64> {
    if value.is_boolean() {
        return None;
    }
    as_f64(Some(value)).filter(|value| value.is_finite())
}
