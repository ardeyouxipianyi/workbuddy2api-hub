//! Policy serialization, hashing, timeline lookup, and cost-detail fields.

use std::collections::{BTreeMap, HashMap};

use serde_json::{Map, Value};

use crate::pricing_engine::as_f64;

pub struct RecordResult {
    pub added: usize,
    pub changed: bool,
    pub assignment: BTreeMap<String, String>,
}

pub fn python_json(value: &Value) -> String {
    match value {
        Value::Null => "null".into(),
        Value::Bool(value) => value.to_string(),
        Value::Number(value) => value.to_string(),
        Value::String(value) => serde_json::to_string(value).unwrap_or_else(|_| "\"\"".into()),
        Value::Array(values) => {
            let body = values
                .iter()
                .map(python_json)
                .collect::<Vec<_>>()
                .join(", ");
            format!("[{body}]")
        }
        Value::Object(map) => python_object(map, false),
    }
}

pub fn python_json_sorted(value: &Value) -> String {
    match value {
        Value::Object(map) => python_object(map, true),
        Value::Array(values) => {
            let body = values
                .iter()
                .map(python_json_sorted)
                .collect::<Vec<_>>()
                .join(", ");
            format!("[{body}]")
        }
        _ => python_json(value),
    }
}

fn python_object(map: &Map<String, Value>, sorted: bool) -> String {
    let mut entries: Vec<_> = map.iter().collect();
    if sorted {
        entries.sort_by(|left, right| left.0.cmp(right.0));
    }
    let body = entries
        .into_iter()
        .map(|(key, value)| {
            let key = serde_json::to_string(key).unwrap_or_else(|_| "\"\"".into());
            let value = if sorted {
                python_json_sorted(value)
            } else {
                python_json(value)
            };
            format!("{key}: {value}")
        })
        .collect::<Vec<_>>()
        .join(", ");
    format!("{{{body}}}")
}

pub fn policy_id(model: &str, flat: &Value, bands: &Value, usd_cny: f64) -> String {
    let bands = if bands.as_array().is_some_and(Vec::is_empty) {
        Value::Null
    } else {
        bands.clone()
    };
    let payload = serde_json::json!({
        "model": model,
        "flat": flat,
        "bands": bands,
        "usd_cny": (usd_cny * 1_000_000.0).round() / 1_000_000.0,
    });
    sha1_hex(python_json_sorted(&payload).as_bytes())[..12].to_string()
}

fn sha1_hex(input: &[u8]) -> String {
    let mut data = input.to_vec();
    let bit_len = (data.len() as u64).wrapping_mul(8);
    data.push(0x80);
    while data.len() % 64 != 56 {
        data.push(0);
    }
    data.extend_from_slice(&bit_len.to_be_bytes());
    let mut h = [
        0x6745_2301u32,
        0xefcd_ab89,
        0x98ba_dcfe,
        0x1032_5476,
        0xc3d2_e1f0,
    ];
    for chunk in data.chunks_exact(64) {
        let mut words = [0u32; 80];
        for (index, bytes) in chunk.chunks_exact(4).enumerate() {
            words[index] = u32::from_be_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]);
        }
        for index in 16..80 {
            words[index] =
                (words[index - 3] ^ words[index - 8] ^ words[index - 14] ^ words[index - 16])
                    .rotate_left(1);
        }
        let (mut a, mut b, mut c, mut d, mut e) = (h[0], h[1], h[2], h[3], h[4]);
        for (index, word) in words.iter().enumerate() {
            let (f, k) = match index {
                0..=19 => ((b & c) | (!b & d), 0x5a82_7999),
                20..=39 => (b ^ c ^ d, 0x6ed9_eba1),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8f1b_bcdc),
                _ => (b ^ c ^ d, 0xca62_c1d6),
            };
            let next = a
                .rotate_left(5)
                .wrapping_add(f)
                .wrapping_add(e)
                .wrapping_add(k)
                .wrapping_add(*word);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = next;
        }
        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
    }
    h.iter().map(|word| format!("{word:08x}")).collect()
}

pub fn policy_record(model: &str, entry: &Value, usd_cny: f64, first_seen: f64) -> Value {
    let mut record = Map::new();
    let flat = entry
        .get("flat")
        .cloned()
        .unwrap_or_else(|| serde_json::json!({}));
    let bands = entry.get("bands").cloned().unwrap_or(Value::Null);
    record.insert(
        "id".into(),
        serde_json::json!(policy_id(model, &flat, &bands, usd_cny)),
    );
    record.insert("model".into(), serde_json::json!(model));
    record.insert(
        "or_id".into(),
        entry.get("or_id").cloned().unwrap_or(Value::Null),
    );
    record.insert(
        "currency".into(),
        entry
            .get("currency")
            .cloned()
            .unwrap_or_else(|| serde_json::json!("USD")),
    );
    record.insert(
        "unit".into(),
        entry
            .get("unit")
            .cloned()
            .unwrap_or_else(|| serde_json::json!(1_000_000)),
    );
    record.insert("usd_cny".into(), serde_json::json!(usd_cny));
    record.insert("flat".into(), flat);
    record.insert("bands".into(), bands);
    record.insert(
        "via".into(),
        entry
            .get("via")
            .cloned()
            .unwrap_or_else(|| serde_json::json!("direct")),
    );
    record.insert(
        "inherited_from".into(),
        entry.get("inherited_from").cloned().unwrap_or(Value::Null),
    );
    record.insert(
        "override_from".into(),
        entry.get("override_from").cloned().unwrap_or(Value::Null),
    );
    record.insert("first_seen".into(), serde_json::json!(first_seen));
    Value::Object(record)
}

pub fn policy_entry(policy: &Value) -> Value {
    let mut entry = Map::new();
    entry.insert(
        "display".into(),
        policy.get("model").cloned().unwrap_or(Value::Null),
    );
    entry.insert("source".into(), serde_json::json!("openrouter"));
    entry.insert(
        "currency".into(),
        policy
            .get("currency")
            .cloned()
            .unwrap_or_else(|| serde_json::json!("USD")),
    );
    entry.insert(
        "unit".into(),
        policy
            .get("unit")
            .cloned()
            .filter(|value| as_f64(Some(value)).unwrap_or(0.0) != 0.0)
            .unwrap_or_else(|| serde_json::json!(1_000_000)),
    );
    entry.insert(
        "or_id".into(),
        policy.get("or_id").cloned().unwrap_or(Value::Null),
    );
    entry.insert(
        "flat".into(),
        policy
            .get("flat")
            .cloned()
            .unwrap_or_else(|| serde_json::json!({})),
    );
    for key in ["via", "inherited_from", "override_from"] {
        if let Some(value) = policy.get(key) {
            entry.insert(key.into(), value.clone());
        }
    }
    if let Some(bands) = policy
        .get("bands")
        .filter(|value| value.as_array().is_some_and(|a| !a.is_empty()))
    {
        entry.insert("bands".into(), bands.clone());
    }
    Value::Object(entry)
}

pub fn doc_for_policy(policy: &Value, model: &str) -> Value {
    let mut models = Map::new();
    models.insert(model.to_string(), policy_entry(policy));
    serde_json::json!({
        "meta": {
            "base_currency": "CNY",
            "usd_cny": as_f64(policy.get("usd_cny")).unwrap_or(crate::pricing_engine::USD_CNY),
        },
        "models": models,
    })
}

pub fn load_policy_map(rows: Vec<Value>) -> HashMap<String, Value> {
    rows.into_iter()
        .filter_map(|row| Some((row.get("id")?.as_str()?.to_string(), row)))
        .collect()
}

pub fn current_assignment(timeline: &[Value]) -> (Option<f64>, BTreeMap<String, String>) {
    let Some(last) = timeline.last() else {
        return (None, BTreeMap::new());
    };
    let at = as_f64(last.get("at"));
    let policies = assignment_of(last);
    (at, policies)
}

fn assignment_of(row: &Value) -> BTreeMap<String, String> {
    row.get("policies")
        .and_then(Value::as_object)
        .into_iter()
        .flat_map(|map| map.iter())
        .filter_map(|(model, id)| Some((model.clone(), id.as_str()?.to_string())))
        .collect()
}

/// 时间线预处理：批量计价时，每行都重建 ats 数组和整张 assignment 太贵
/// （2.7 万行约 250 ms）。这里一次摊平，之后按行二分查。
pub struct PolicyHistory {
    ats: Vec<f64>,
    assignments: Vec<BTreeMap<String, String>>,
}

impl PolicyHistory {
    pub fn new(timeline: &[Value]) -> Self {
        Self {
            ats: timeline
                .iter()
                .map(|row| as_f64(row.get("at")).unwrap_or(0.0))
                .collect(),
            assignments: timeline.iter().map(assignment_of).collect(),
        }
    }

    pub fn resolve(&self, epoch_sec: f64, model: &str) -> (Option<String>, bool) {
        if self.ats.is_empty() {
            return (None, false);
        }
        let found = self.ats.partition_point(|at| *at <= epoch_sec);
        let index = found.saturating_sub(1);
        if let Some(id) = self.assignments[index].get(model) {
            return (Some(id.clone()), false);
        }
        for (at, assignment) in self.ats.iter().zip(&self.assignments) {
            if *at < epoch_sec {
                continue;
            }
            if let Some(id) = assignment.get(model) {
                return (Some(id.clone()), true);
            }
        }
        for assignment in self.assignments[..=index].iter().rev() {
            if let Some(id) = assignment.get(model) {
                return (Some(id.clone()), true);
            }
        }
        (None, false)
    }
}

pub fn policy_ref_at(timeline: &[Value], epoch_sec: f64, model: &str) -> (Option<String>, bool) {
    PolicyHistory::new(timeline).resolve(epoch_sec, model)
}
