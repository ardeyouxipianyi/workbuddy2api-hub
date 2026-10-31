//! Panel-shaped usage row enrichment.

use serde_json::{json, Map, Value};

use crate::pricing::Pricing;

impl Pricing {
    pub fn enrich_usage_row(&self, row: &Value, settings: &Map<String, Value>) -> Value {
        let cost = self.cost_for_row(row, settings, true);
        let mut out = row.as_object().cloned().unwrap_or_default();
        let known = cost.get("known").and_then(Value::as_bool).unwrap_or(false);
        let cny = cost
            .get("cny")
            .and_then(Value::as_f64)
            .map(round_six)
            .filter(|_| known);
        out.insert(
            "cost_cny".into(),
            cny.map_or(Value::Null, |value| json!(value)),
        );
        for (source, target) in [
            ("band", "cost_band"),
            ("source", "cost_source"),
            ("source_at", "cost_source_at"),
            ("backfilled", "cost_backfilled"),
            ("rates", "cost_rates"),
            ("unit", "cost_unit"),
            ("currency", "cost_currency"),
            ("usd_cny", "cost_usd_cny"),
            ("or_id", "cost_or_id"),
            ("via", "cost_via"),
            ("inherited_from", "cost_inherited_from"),
            ("override_from", "cost_override_from"),
            ("band_note", "cost_band_note"),
            ("via_derived", "cost_via_derived"),
        ] {
            let value = if known {
                cost.get(source).cloned().unwrap_or(Value::Null)
            } else if target == "cost_backfilled" {
                json!(false)
            } else {
                Value::Null
            };
            out.insert(target.into(), value);
        }
        Value::Object(out)
    }
}

fn round_six(value: f64) -> f64 {
    (value * 1_000_000.0).round() / 1_000_000.0
}
