//! Low-level JSONL persistence and timestamp helpers for pricing.

use std::fs;
use std::io::Write;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};

use crate::pricing_cost::no_details;
use crate::pricing_policy::python_json;

#[derive(Clone, PartialEq, Eq)]
pub(super) struct FileKey {
    pub(super) exists: bool,
    modified_ns: u128,
    size: u64,
}

pub fn base_cost(_row: Value, disabled: bool, details: bool) -> Value {
    let mut out = Map::new();
    out.insert("cny".into(), json!(0.0));
    out.insert("known".into(), json!(false));
    out.insert("band".into(), Value::Null);
    out.insert("source".into(), json!("builtin"));
    out.insert("source_at".into(), Value::Null);
    out.insert("backfilled".into(), json!(false));
    if disabled {
        out.insert("disabled".into(), json!(true));
    }
    if details {
        out.extend(no_details());
    }
    Value::Object(out)
}

pub(super) fn add_source(
    cost: &mut Value,
    source: &str,
    source_at: Option<&Value>,
    backfilled: bool,
) {
    if let Some(map) = cost.as_object_mut() {
        map.insert("source".into(), json!(source));
        map.insert(
            "source_at".into(),
            source_at.cloned().unwrap_or(Value::Null),
        );
        map.insert("backfilled".into(), json!(backfilled));
    }
}

pub(super) fn add_markup(cost: &mut Value, markup: f64, details: bool) {
    if details && cost.get("known").and_then(Value::as_bool) == Some(true) {
        if let Some(map) = cost.as_object_mut() {
            map.insert("markup".into(), json!(markup));
            map.insert(
                "markup_from".into(),
                if markup == 1.0 {
                    Value::Null
                } else {
                    json!("pricing-markup.json")
                },
            );
        }
    }
}

pub(super) fn file_key(path: &Path) -> FileKey {
    match fs::metadata(path) {
        Ok(metadata) => FileKey {
            exists: true,
            modified_ns: metadata
                .modified()
                .ok()
                .and_then(|time| time.duration_since(UNIX_EPOCH).ok())
                .map(|duration| duration.as_nanos())
                .unwrap_or(0),
            size: metadata.len(),
        },
        Err(_) => FileKey {
            exists: false,
            modified_ns: 0,
            size: 0,
        },
    }
}

pub(super) fn read_jsonl(path: &Path) -> Vec<Value> {
    let Ok(text) = fs::read_to_string(path) else {
        return Vec::new();
    };
    text.lines()
        .filter_map(|line| serde_json::from_str::<Value>(line.trim()).ok())
        .filter(Value::is_object)
        .collect()
}

pub(super) fn append_jsonl(path: &Path, rows: &[Value]) {
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    let Ok(mut file) = fs::OpenOptions::new().create(true).append(true).open(path) else {
        return;
    };
    for row in rows {
        let line = python_json(row);
        let _ = writeln!(file, "{line}");
    }
}

pub fn now_epoch() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(0.0)
}

pub fn local_label(epoch: f64) -> String {
    unsafe {
        let time = epoch as libc::time_t;
        let mut value: libc::tm = std::mem::zeroed();
        if libc::localtime_r(&time, &mut value).is_null() {
            return String::new();
        }
        format!(
            "{:04}-{:02}-{:02} {:02}:{:02}",
            value.tm_year + 1900,
            value.tm_mon + 1,
            value.tm_mday,
            value.tm_hour,
            value.tm_min
        )
    }
}

pub(super) fn now_iso() -> String {
    crate::state::local_stamp().replace(' ', "T")
}
