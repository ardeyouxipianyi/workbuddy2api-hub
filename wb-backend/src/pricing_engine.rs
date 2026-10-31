//! Pure pricing and catalogue-matching helpers.
//!
//! This module mirrors the row-pricing half of `wb_pricing.py`. It performs no
//! I/O, so callers can test it with fixed OpenRouter samples.

use std::collections::{BTreeMap, BTreeSet};

use serde_json::{json, Map, Value};

pub const USD_CNY: f64 = 7.10;

pub fn as_f64(value: Option<&Value>) -> Option<f64> {
    match value? {
        Value::Number(number) => number.as_f64(),
        Value::String(text) => text.trim().parse::<f64>().ok(),
        _ => None,
    }
}

pub fn as_i64(value: Option<&Value>) -> Option<i64> {
    match value? {
        Value::Number(number) => number
            .as_i64()
            .or_else(|| number.as_f64().map(|n| n as i64)),
        Value::String(text) => text.trim().parse::<i64>().ok(),
        _ => None,
    }
}

#[derive(Clone, Debug)]
pub struct Defaults {
    pub model_ids: Vec<String>,
    pub overrides: BTreeMap<String, String>,
    pub aliases: BTreeSet<String>,
    pub variant_suffixes: Vec<(String, bool)>,
}

impl Defaults {
    pub fn parse(raw: &str) -> Result<Self, String> {
        let value: Value = serde_json::from_str(raw).map_err(|e| e.to_string())?;
        let model_ids = value
            .get("model_ids")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .map(str::to_string)
            .collect();
        let overrides = value
            .get("overrides")
            .and_then(Value::as_object)
            .into_iter()
            .flat_map(|map| map.iter())
            .filter_map(|(key, val)| Some((key.clone(), val.as_str()?.to_string())))
            .collect();
        let aliases = value
            .get("aliases")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .map(str::to_string)
            .collect();
        let variant_suffixes = value
            .get("variant_suffixes")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|item| Some((item.get(0)?.as_str()?.to_string(), item.get(1)?.as_bool()?)))
            .collect();
        Ok(Self {
            model_ids,
            overrides,
            aliases,
            variant_suffixes,
        })
    }
}

#[derive(Clone, Debug)]
pub struct ComputedCost {
    pub cny: f64,
    pub known: bool,
    pub band: Option<usize>,
    pub rates: Value,
}

impl ComputedCost {
    pub fn unknown() -> Self {
        Self {
            cny: 0.0,
            known: false,
            band: None,
            rates: Value::Null,
        }
    }
}

pub fn compute_row(row: &Value, pricing: &Value, markup: f64) -> ComputedCost {
    let Some(model) = row.get("model").and_then(Value::as_str) else {
        return ComputedCost::unknown();
    };
    let Some(entry) = pricing.pointer("/models").and_then(|m| m.get(model)) else {
        return ComputedCost::unknown();
    };
    let meta = pricing.get("meta").unwrap_or(&Value::Null);
    let unit = as_f64(entry.get("unit"))
        .filter(|n| *n != 0.0)
        .unwrap_or(1_000_000.0);
    let prompt = as_f64(row.get("prompt_tokens")).unwrap_or(0.0);
    let (rate, band) = band_for(entry, as_f64(row.get("at")).unwrap_or(0.0), prompt);
    let factor = if entry
        .get("currency")
        .and_then(Value::as_str)
        .unwrap_or("")
        .eq_ignore_ascii_case("USD")
    {
        as_f64(meta.get("usd_cny"))
            .filter(|n| *n != 0.0)
            .unwrap_or(1.0)
    } else {
        1.0
    };
    sum_rate(
        &rate,
        row,
        prompt,
        unit,
        factor,
        markup,
        &Value::Object(Map::new()),
        band,
    )
}

fn sum_rate(
    rate: &Map<String, Value>,
    row: &Value,
    prompt: f64,
    unit: f64,
    factor: f64,
    markup: f64,
    raw_rates: &Value,
    band: Option<usize>,
) -> ComputedCost {
    let cached = as_f64(row.get("cached_tokens")).unwrap_or(0.0).min(prompt);
    let uncached = prompt - cached;
    let output = as_f64(row.get("completion_tokens")).unwrap_or(0.0);
    let miss = rate_number(rate, "input_cache_miss", None);
    let hit = rate_number(rate, "input_cache_hit", Some("input_cache_miss"));
    let output_rate = rate_number(rate, "output", None);
    let cny = (uncached * miss + cached * hit + output * output_rate) / unit * factor * markup;
    ComputedCost {
        cny,
        known: true,
        band,
        rates: if raw_rates.is_null() {
            Value::Object(rate.clone())
        } else {
            raw_rates.clone()
        },
    }
}

fn rate_number(rate: &Map<String, Value>, key: &str, fallback: Option<&str>) -> f64 {
    as_f64(rate.get(key))
        .or_else(|| fallback.and_then(|name| as_f64(rate.get(name))))
        .unwrap_or(0.0)
}

pub fn band_for(
    entry: &Value,
    epoch_sec: f64,
    prompt_tokens: f64,
) -> (Map<String, Value>, Option<usize>) {
    let flat = entry
        .get("flat")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let Some(bands) = entry.get("bands").and_then(Value::as_array) else {
        return (flat, None);
    };
    if bands.is_empty() {
        return (flat, None);
    }
    let (day, hhmm) = utc_parts(epoch_sec);
    let mut hit: Option<(Map<String, Value>, usize)> = None;
    for (index, band) in bands.iter().enumerate() {
        let Some(band_obj) = band.as_object() else {
            continue;
        };
        if !band_applies(band_obj, prompt_tokens, &day, hhmm) {
            continue;
        }
        if let Some(table) = band_obj.get("flat").and_then(Value::as_object) {
            hit = Some((table.clone(), index));
        }
    }
    hit.map_or((flat, None), |(table, index)| (table, Some(index)))
}

fn band_applies(band: &Map<String, Value>, prompt_tokens: f64, day: &str, hhmm: i64) -> bool {
    if let Some(threshold) = band.get("min_prompt_tokens") {
        return prompt_tokens >= as_f64(Some(threshold)).unwrap_or(0.0);
    }
    if let Some(days) = band.get("days").and_then(Value::as_array) {
        if !days.is_empty() && !days.iter().any(|value| value.as_str() == Some(day)) {
            return false;
        }
    }
    let start = as_f64(band.get("start")).unwrap_or(0.0);
    let end = as_f64(band.get("end"))
        .filter(|n| *n != 0.0)
        .unwrap_or(2400.0);
    start <= hhmm as f64 && (hhmm as f64) < end
}

fn utc_parts(epoch_sec: f64) -> (String, i64) {
    const DAYS: [&str; 7] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
    let epoch = epoch_sec as i64;
    let days = epoch.div_euclid(86_400);
    let weekday = (days + 3).rem_euclid(7) as usize;
    let secs = epoch.rem_euclid(86_400);
    (
        DAYS[weekday].to_string(),
        (secs / 3600) * 100 + (secs % 3600) / 60,
    )
}

pub fn rates_from(item: &Value) -> Value {
    json!({
        "input_cache_hit": per_million(item.get("input_cache_read")),
        "input_cache_miss": per_million(item.get("prompt")),
        "output": per_million(item.get("completion")),
    })
}

fn per_million(value: Option<&Value>) -> Value {
    match as_f64(value) {
        Some(number) => json!((number * 1_000_000.0 * 1_000_000.0).round() / 1_000_000.0),
        None => Value::Null,
    }
}

pub fn bands_from_overrides(item: &Value) -> Vec<Value> {
    let mut out = Vec::new();
    let Some(items) = item.get("overrides").and_then(Value::as_array) else {
        return out;
    };
    for override_item in items {
        let Some(source) = override_item.as_object() else {
            continue;
        };
        let mut band = Map::new();
        band.insert("flat".into(), rates_from(override_item));
        if let Some(threshold) = source.get("min_prompt_tokens") {
            band.insert(
                "min_prompt_tokens".into(),
                json!(as_f64(Some(threshold)).unwrap_or(0.0) as i64),
            );
        } else if source.keys().any(|key| key.starts_with("utc_")) {
            let days = utc_days(source);
            let end = as_f64(source.get("utc_end"))
                .filter(|n| *n != 0.0)
                .unwrap_or(2400.0);
            band.insert(
                "days".into(),
                if days.is_empty() {
                    Value::Null
                } else {
                    Value::Array(days)
                },
            );
            band.insert(
                "start".into(),
                json!(as_f64(source.get("utc_start")).unwrap_or(0.0) as i64),
            );
            band.insert("end".into(), json!(end as i64));
        } else {
            continue;
        }
        out.push(Value::Object(band));
    }
    out
}

fn utc_days(source: &Map<String, Value>) -> Vec<Value> {
    const LONG: [&str; 7] = [
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ];
    const SHORT: [&str; 7] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
    source
        .get("utc_days")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|day| {
            let lower = day.as_str()?.to_ascii_lowercase();
            LONG.iter()
                .position(|name| *name == lower)
                .map(|index| json!(SHORT[index]))
        })
        .collect()
}
