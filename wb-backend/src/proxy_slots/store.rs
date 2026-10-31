//! `settings.json.proxy_slots`: normalization, stable ids, bindings, and views.

use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::{Mutex, OnceLock};

use serde_json::{json, Map, Value};

use crate::state::AccountFile;
use crate::App;

const SLOT_PREFIX: &str = "slot-";

fn store_lock() -> &'static Mutex<()> {
    static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    LOCK.get_or_init(|| Mutex::new(()))
}

pub fn slots_from_settings(settings: &Map<String, Value>) -> Vec<Value> {
    let Some(stored) = settings.get("proxy_slots").and_then(Value::as_array) else {
        return Vec::new();
    };
    let mut out = Vec::with_capacity(stored.len());
    let mut seen_urls = HashSet::with_capacity(stored.len());
    for raw in stored {
        let Some(mut entry) = clean_slot(raw) else {
            continue;
        };
        let url = entry.get("url").and_then(Value::as_str).unwrap_or("");
        if !seen_urls.insert(url.to_string()) {
            continue;
        }
        if entry
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .is_empty()
        {
            entry["id"] = json!(next_id(&out));
        }
        out.push(entry);
    }
    out
}

pub fn clean_slot(value: &Value) -> Option<Value> {
    let object = value.as_object()?;
    let url = text(object.get("url"));
    if url.is_empty() {
        return None;
    }
    Some(json!({
        "id": text(object.get("id")),
        "name": text(object.get("name")),
        "url": url,
        "enabled": object.get("enabled") != Some(&Value::Bool(false)),
        "ip": text(object.get("ip")),
        "country": text(object.get("country")),
        "country_code": text(object.get("country_code")).to_uppercase(),
        "ip_type": text(object.get("ip_type")).to_lowercase(),
        "isp": text(object.get("isp")),
        "asn": text(object.get("asn")),
        "probed_at": integer(object.get("probed_at")),
    }))
}

fn text(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(text)) => text.trim().to_string(),
        Some(Value::Number(number)) => number.to_string(),
        _ => String::new(),
    }
}

fn integer(value: Option<&Value>) -> i64 {
    match value {
        Some(Value::Number(number)) => number.as_i64().unwrap_or(0),
        Some(Value::String(text)) => text.trim().parse().unwrap_or(0),
        _ => 0,
    }
}

fn next_id(existing: &[Value]) -> String {
    let highest = existing
        .iter()
        .filter_map(|entry| slot_number(entry.get("id").and_then(Value::as_str).unwrap_or("")))
        .max()
        .unwrap_or(0);
    format!("{SLOT_PREFIX}{}", highest + 1)
}

fn slot_number(id: &str) -> Option<u64> {
    id.strip_prefix(SLOT_PREFIX)?.parse().ok()
}

pub fn store_slots(dir: &Path, slots: &[Value]) -> Result<Vec<Value>, std::io::Error> {
    let _guard = store_lock().lock().unwrap();
    let path = dir.join("settings.json");
    let mut settings = read_settings(&path);
    let stored = settings.get("proxy_slots").and_then(Value::as_array);
    let mut sequence = settings
        .get("proxy_slot_seq")
        .and_then(|value| value.as_u64().or_else(|| value.as_str()?.parse().ok()))
        .unwrap_or(0);
    sequence = sequence.max(stored.map_or(0, |items| items.len() as u64));
    for entry in stored.into_iter().flatten().chain(slots) {
        if let Some(number) = entry
            .get("id")
            .and_then(Value::as_str)
            .and_then(slot_number)
        {
            sequence = sequence.max(number);
        }
    }

    let mut cleaned = Vec::with_capacity(slots.len());
    let mut seen_urls = HashSet::with_capacity(slots.len());
    let mut seen_ids = HashSet::with_capacity(slots.len());
    for raw in slots {
        let Some(mut entry) = clean_slot(raw) else {
            continue;
        };
        let url = entry.get("url").and_then(Value::as_str).unwrap_or("");
        if !seen_urls.insert(url.to_string()) {
            continue;
        }
        let id = entry.get("id").and_then(Value::as_str).unwrap_or("");
        if id.is_empty() || !seen_ids.insert(id.to_string()) {
            sequence += 1;
            let fresh = format!("{SLOT_PREFIX}{sequence}");
            entry["id"] = json!(fresh.clone());
            seen_ids.insert(fresh);
        }
        cleaned.push(entry);
    }
    settings.insert("proxy_slots".into(), Value::Array(cleaned.clone()));
    settings.insert("proxy_slot_seq".into(), json!(sequence));
    crate::state::write_json_atomic(&path, &Value::Object(settings))?;
    Ok(cleaned)
}

fn read_settings(path: &Path) -> Map<String, Value> {
    std::fs::read_to_string(path)
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .and_then(|value| value.as_object().cloned())
        .unwrap_or_default()
}

pub fn update_slot(
    dir: &Path,
    slot_id: &str,
    fields: &Map<String, Value>,
) -> Result<Option<Value>, std::io::Error> {
    let _guard = store_lock().lock().unwrap();
    let path = dir.join("settings.json");
    let mut settings = read_settings(&path);
    let slots = settings
        .get("proxy_slots")
        .and_then(Value::as_array)
        .cloned();
    let Some(slots) = slots else {
        return Ok(None);
    };
    let mut found = None;
    let mut updated = Vec::with_capacity(slots.len());
    for raw in slots {
        let mut entry = clean_slot(&raw).unwrap_or(raw);
        if found.is_none() && entry.get("id").and_then(Value::as_str) == Some(slot_id) {
            if let Some(object) = entry.as_object_mut() {
                for (key, value) in fields {
                    object.insert(key.clone(), value.clone());
                }
            }
            found = Some(entry.clone());
        }
        updated.push(entry);
    }
    if found.is_some() {
        settings.insert("proxy_slots".into(), Value::Array(updated));
        crate::state::write_json_atomic(&path, &Value::Object(settings))?;
    }
    Ok(found)
}

pub fn drop_missing_bindings(app: &App, slots: &[Value]) -> usize {
    let valid: HashSet<&str> = slots
        .iter()
        .filter_map(|slot| slot.get("id").and_then(Value::as_str))
        .collect();
    let mut changed = 0;
    for account in app.state.accounts_snapshot() {
        let Some(slot) = account.data.get("proxySlot").and_then(Value::as_str) else {
            continue;
        };
        if slot.is_empty() || valid.contains(slot) {
            continue;
        }
        let mut data = account.data.clone();
        data.insert("proxySlot".into(), json!(""));
        if crate::state::write_json_atomic(&account.path, &Value::Object(data)).is_ok() {
            changed += 1;
        }
    }
    changed
}

pub fn with_runtime_fields(slots: &[Value], accounts: &[AccountFile]) -> Vec<Value> {
    let mut counts = HashMap::<String, usize>::new();
    for account in accounts.iter().filter(|account| account.enabled()) {
        if let Some(slot) = account.data.get("proxySlot").and_then(Value::as_str) {
            if !slot.is_empty() {
                *counts.entry(slot.to_string()).or_default() += 1;
            }
        }
    }
    slots
        .iter()
        .cloned()
        .map(|mut slot| {
            let id = slot.get("id").and_then(Value::as_str).unwrap_or("");
            slot["bound"] = json!(counts.get(id).copied().unwrap_or(0));
            slot["label"] = json!(slot_label(&slot));
            slot
        })
        .collect()
}

fn slot_label(slot: &Value) -> String {
    let name = slot
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if !name.is_empty() {
        return name.to_string();
    }
    let country = slot
        .get("country")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if country.is_empty() {
        return slot
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
    }
    match slot.get("ip_type").and_then(Value::as_str).unwrap_or("") {
        "residential" => format!("{country} 住宅"),
        "datacenter" => format!("{country} 机房"),
        _ => country.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    fn temp_dir() -> std::path::PathBuf {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let path = std::env::temp_dir().join(format!(
            "wb-slots-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir_all(&path).unwrap();
        path
    }

    #[test]
    fn slot_round_trip_normalizes_fields_and_keeps_probe_data() {
        let dir = temp_dir();
        let cleaned = store_slots(
            &dir,
            &[json!({
                "name": "  edge  ",
                "url": " http://127.0.0.1:17901 ",
                "country_code": "us",
                "ip_type": "RESIDENTIAL",
                "ip": "1.2.3.4",
                "probed_at": "42",
            })],
        )
        .unwrap();
        assert_eq!(cleaned[0]["id"], "slot-1");
        assert_eq!(cleaned[0]["name"], "edge");
        assert_eq!(cleaned[0]["url"], "http://127.0.0.1:17901");
        assert_eq!(cleaned[0]["country_code"], "US");
        assert_eq!(cleaned[0]["ip_type"], "residential");
        assert_eq!(cleaned[0]["probed_at"], 42);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn save_validation_drops_invalid_and_duplicate_urls_and_never_reuses_ids() {
        let dir = temp_dir();
        store_slots(
            &dir,
            &[json!({"id": "slot-7", "url": "http://127.0.0.1:1"})],
        )
        .unwrap();
        let saved = store_slots(
            &dir,
            &[
                json!({"name": "no url"}),
                json!({"url": "http://127.0.0.1:2"}),
                json!({"url": "http://127.0.0.1:2"}),
            ],
        )
        .unwrap();
        assert_eq!(saved.len(), 1);
        assert_eq!(saved[0]["id"], "slot-8");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn labels_use_name_then_exit_then_id() {
        assert_eq!(
            slot_label(
                &json!({"id":"slot-1","name":"香港","country":"美国","ip_type":"residential"})
            ),
            "香港"
        );
        assert_eq!(
            slot_label(&json!({"id":"slot-1","name":"","country":"美国","ip_type":"datacenter"})),
            "美国 机房"
        );
        assert_eq!(slot_label(&json!({"id":"slot-1"})), "slot-1");
    }
}
