//! Cost payloads and the human-readable explanation of a row's price.

use std::collections::BTreeMap;

use serde_json::{Map, Value};

use crate::pricing_engine::{as_f64, as_i64, compute_row, ComputedCost};
use crate::pricing_policy::{doc_for_policy, policy_entry};

pub fn cost_value(
    cost: &ComputedCost,
    entry: Option<&Value>,
    meta: &Value,
    model: &str,
    overrides: &BTreeMap<String, String>,
    details: bool,
) -> Value {
    let mut value = Map::new();
    value.insert("cny".into(), serde_json::json!(cost.cny));
    value.insert("known".into(), serde_json::json!(cost.known));
    value.insert("band".into(), serde_json::json!(cost.band));
    value.insert(
        "rates".into(),
        if cost.known {
            cost.rates.clone()
        } else {
            Value::Null
        },
    );
    if details {
        let detail = if cost.known {
            details_for(entry, meta, model, cost, overrides)
        } else {
            no_details()
        };
        for (key, detail_value) in detail {
            value.insert(key, detail_value);
        }
    }
    Value::Object(value)
}

pub fn no_details() -> Map<String, Value> {
    [
        "rates",
        "unit",
        "currency",
        "usd_cny",
        "or_id",
        "via",
        "inherited_from",
        "override_from",
        "band_note",
        "via_derived",
        "markup",
        "markup_from",
    ]
    .into_iter()
    .map(|key| (key.to_string(), Value::Null))
    .collect()
}

fn details_for(
    entry: Option<&Value>,
    meta: &Value,
    model: &str,
    cost: &ComputedCost,
    overrides: &BTreeMap<String, String>,
) -> Map<String, Value> {
    let Some(entry) = entry else {
        return no_details();
    };
    let (via, inherited, override_from, derived) = match_source(entry, model, overrides);
    let mut out = Map::new();
    out.insert("rates".into(), cost.rates.clone());
    out.insert(
        "unit".into(),
        serde_json::json!(as_f64(entry.get("unit"))
            .filter(|n| *n != 0.0)
            .unwrap_or(1_000_000.0)),
    );
    out.insert(
        "currency".into(),
        entry
            .get("currency")
            .cloned()
            .unwrap_or_else(|| serde_json::json!("USD")),
    );
    out.insert(
        "usd_cny".into(),
        serde_json::json!(as_f64(meta.get("usd_cny")).unwrap_or(0.0)),
    );
    out.insert(
        "or_id".into(),
        entry.get("or_id").cloned().unwrap_or(Value::Null),
    );
    out.insert("via".into(), serde_json::json!(via));
    out.insert(
        "inherited_from".into(),
        inherited.map_or(Value::Null, Value::String),
    );
    out.insert(
        "override_from".into(),
        override_from.map_or(Value::Null, Value::String),
    );
    out.insert(
        "band_note".into(),
        band_note(entry, cost.band).map_or(Value::Null, Value::String),
    );
    out.insert("via_derived".into(), serde_json::json!(derived));
    out
}

pub fn match_source(
    entry: &Value,
    model: &str,
    overrides: &BTreeMap<String, String>,
) -> (&'static str, Option<String>, Option<String>, bool) {
    if let Some(via) = entry.get("via").and_then(Value::as_str) {
        if ["variant", "override", "direct"].contains(&via) {
            return (
                match via {
                    "variant" => "variant",
                    "override" => "override",
                    _ => "direct",
                },
                entry
                    .get("inherited_from")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                entry
                    .get("override_from")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                false,
            );
        }
    }
    if overrides.get(model).map(String::as_str) == entry.get("or_id").and_then(Value::as_str) {
        return ("override", None, Some(model.to_string()), true);
    }
    ("direct", None, None, false)
}

pub fn band_note(entry: &Value, index: Option<usize>) -> Option<String> {
    let band = entry.get("bands")?.as_array()?.get(index?)?.as_object()?;
    if let Some(threshold) = band.get("min_prompt_tokens") {
        let threshold = as_i64(Some(threshold)).unwrap_or(0);
        return Some(format!("输入长度 ≥ {} tokens", comma_number(threshold)));
    }
    let days: Vec<&str> = band
        .get("days")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .filter_map(weekday_cn)
        .collect();
    let start = hhmm(as_f64(band.get("start")).unwrap_or(0.0));
    let end = hhmm(as_f64(band.get("end")).unwrap_or(2400.0));
    if days.is_empty() {
        Some(format!("每天 {start}–{end} UTC"))
    } else {
        Some(format!("每周 {} {start}–{end} UTC", days.join("/")))
    }
}

fn weekday_cn(day: &str) -> Option<&'static str> {
    match day {
        "mon" => Some("周一"),
        "tue" => Some("周二"),
        "wed" => Some("周三"),
        "thu" => Some("周四"),
        "fri" => Some("周五"),
        "sat" => Some("周六"),
        "sun" => Some("周日"),
        _ => None,
    }
}

fn hhmm(number: f64) -> String {
    let number = number as i64;
    format!("{:02}:{:02}", number / 100, number % 100)
}

fn comma_number(number: i64) -> String {
    let raw = number.to_string();
    let mut out = String::new();
    for (index, ch) in raw.chars().rev().enumerate() {
        if index > 0 && index % 3 == 0 {
            out.push(',');
        }
        out.push(ch);
    }
    out.chars().rev().collect()
}

pub fn price_row_with_policy(
    row: &Value,
    policy: &Value,
    model: &str,
    overrides: &BTreeMap<String, String>,
    markup: f64,
    details: bool,
) -> Value {
    let doc = doc_for_policy(policy, model);
    let cost = compute_row(row, &doc, markup);
    let meta = doc.get("meta").unwrap_or(&Value::Null);
    let entry = policy_entry(policy);
    cost_value(&cost, Some(&entry), meta, model, overrides, details)
}

pub fn price_row_with_doc(
    row: &Value,
    doc: &Value,
    model: &str,
    overrides: &BTreeMap<String, String>,
    markup: f64,
    details: bool,
) -> Value {
    let cost = compute_row(row, doc, markup);
    let meta = doc.get("meta").unwrap_or(&Value::Null);
    let entry = doc.get("models").and_then(|models| models.get(model));
    cost_value(&cost, entry, meta, model, overrides, details)
}
