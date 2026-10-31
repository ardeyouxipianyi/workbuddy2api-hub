use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::PathBuf;

use serde_json::{json, Map, Value};

use crate::pricing_catalog::build_snapshot;
use crate::pricing_engine::{compute_row, Defaults};
use crate::pricing_policy::policy_id;
use crate::pricing_store::PricingStore;
use crate::{panel, pool, pricing, record, scheduler, state, usage};

fn temp_dir(label: &str) -> PathBuf {
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let path = std::env::temp_dir().join(format!("wb-rust-{label}-{}-{stamp}", std::process::id()));
    fs::create_dir_all(&path).unwrap();
    path
}

fn bundled_pricing() -> Value {
    serde_json::from_str(include_str!("../assets/pricing.json")).unwrap()
}

#[test]
fn python_cost_samples_match() {
    let pricing = bundled_pricing();
    let at = 1_790_908_275.0;
    let samples = [
        (
            json!({"model":"deepseek-v4.1-flash","at":at,"prompt_tokens":1000,"cached_tokens":200,"completion_tokens":300}),
            None,
            0.0012495999999999998,
        ),
        (
            json!({"model":"gpt-6-astra","at":at,"prompt_tokens":272000,"cached_tokens":2000,"completion_tokens":300}),
            Some(0),
            38.52815,
        ),
        (
            json!({"model":"hy4-preview-f","at":at,"prompt_tokens":1000,"cached_tokens":0,"completion_tokens":300}),
            Some(0),
            0.011248529999999998,
        ),
    ];
    for (row, band, expected) in samples {
        let cost = compute_row(&row, &pricing, 1.0);
        assert!(cost.known);
        assert_eq!(cost.band, band);
        assert!(
            (cost.cny - expected).abs() < 1e-12,
            "{} != {expected}",
            cost.cny
        );
    }
    let unknown = compute_row(
        &json!({"model":"no-such-model","at":at,"prompt_tokens":1}),
        &pricing,
        1.0,
    );
    assert!(!unknown.known);
    assert_eq!(unknown.cny, 0.0);
}

#[test]
fn python_policy_ids_match() {
    let flat = json!({"input_cache_hit":0.01,"input_cache_miss":0.03,"output":0.5});
    assert_eq!(
        policy_id("deepseek-v4.1-flash", &flat, &Value::Null, 7.1),
        "85b188e975e1"
    );
    let flat = json!({"input_cache_hit":1.0,"input_cache_miss":10.0,"output":50.0});
    let bands = json!([{
        "flat":{"input_cache_hit":2.0,"input_cache_miss":20.0,"output":75.0},
        "min_prompt_tokens":272000
    }]);
    assert_eq!(policy_id("gpt-6-astra", &flat, &bands, 7.1), "7014b582a4ad");
}

#[test]
fn every_live_policy_fixture_id_recomputes_identically() {
    let rows = include_str!("../../tests/fixtures/pricing_tooltip/policies_live_before.jsonl");
    let mut count = 0;
    for line in rows.lines().filter(|line| !line.trim().is_empty()) {
        let row: Value = serde_json::from_str(line).unwrap();
        let model = row["model"].as_str().unwrap();
        let flat = row.get("flat").unwrap_or(&Value::Null);
        let bands = row.get("bands").unwrap_or(&Value::Null);
        let usd = row["usd_cny"].as_f64().unwrap();
        assert_eq!(policy_id(model, flat, bands, usd), row["id"]);
        count += 1;
    }
    assert_eq!(count, 52);
}

#[test]
fn policy_table_and_timeline_match_python_jsonl() {
    let dir = temp_dir("policy-format");
    let store = PricingStore::new(dir.clone(), None);
    let doc = json!({
        "meta":{"base_currency":"CNY","usd_cny":7.1},
        "models":{"m":{
            "display":"m","source":"openrouter","currency":"USD","unit":1000000,
            "or_id":"vendor/m",
            "flat":{"input_cache_hit":0.01,"input_cache_miss":0.03,"output":0.5}
        }}
    });
    let result = store.record_policies(&doc, Some(1_790_908_275.0), false);
    assert_eq!(result.added, 1);
    assert!(result.changed);
    assert_eq!(
        fs::read_to_string(store.policies_path()).unwrap(),
        "{\"id\": \"2f8b4af16737\", \"model\": \"m\", \"or_id\": \"vendor/m\", \"currency\": \"USD\", \"unit\": 1000000, \"usd_cny\": 7.1, \"flat\": {\"input_cache_hit\": 0.01, \"input_cache_miss\": 0.03, \"output\": 0.5}, \"bands\": null, \"via\": \"direct\", \"inherited_from\": null, \"override_from\": null, \"first_seen\": 1790908275.0}\n"
    );
    assert_eq!(
        fs::read_to_string(store.timeline_path()).unwrap(),
        "{\"at\": 1790908275.0, \"policies\": {\"m\": \"2f8b4af16737\"}}\n"
    );
    let _ = fs::remove_dir_all(dir);
}

#[test]
fn referenced_policy_survives_newer_price() {
    let dir = temp_dir("policy-history");
    let store = PricingStore::new(dir.clone(), None);
    let entry = |rate| {
        json!({
            "currency":"USD","unit":1000000,"or_id":"vendor/m",
            "flat":{"input_cache_hit":rate,"input_cache_miss":rate,"output":rate}
        })
    };
    let first = store.record_policies(
        &json!({"meta":{"usd_cny":7.1},"models":{"m":entry(1.0)}}),
        Some(100.0),
        false,
    );
    store.record_policies(
        &json!({"meta":{"usd_cny":7.1},"models":{"m":entry(2.0)}}),
        Some(200.0),
        false,
    );
    let old = first.assignment["m"].clone();
    let row = json!({
        "model":"m","at":250.0,"cost_policy":old,
        "prompt_tokens":1000000,"cached_tokens":0,"completion_tokens":0
    });
    let cost = store.cost_for_row(&row, true, true);
    assert_eq!(cost["source"], old);
    assert!((cost["cny"].as_f64().unwrap() - 7.1).abs() < 1e-12);
    let _ = fs::remove_dir_all(dir);
}

#[test]
fn fixed_openrouter_sample_builds_python_shaped_snapshot() {
    let defaults = Defaults::parse(include_str!("../assets/pricing-defaults.json")).unwrap();
    let or_models: Map<String, Value> = serde_json::from_value(json!({
        "deepseek/deepseek-v4.1-flash":{
            "prompt":"0.00000003","completion":"0.0000005","input_cache_read":"0.00000001"
        },
        "z-ai/glm-5.3-flashx":{
            "prompt":"0.0000002","completion":"0.0000007","input_cache_read":"0.00000004"
        }
    }))
    .unwrap();
    let ids = vec![
        "deepseek-v4.1-flash".to_string(),
        "glm-5.3-flashx".to_string(),
    ];
    let (doc, unpriced, _) = build_snapshot(
        &or_models,
        &ids,
        &BTreeMap::new(),
        &defaults,
        true,
        "2026-10-31T00:00:00",
        "https://openrouter.ai/api/v1/models",
    );
    assert!(unpriced.is_empty());
    assert_eq!(
        doc["models"]["deepseek-v4.1-flash"]["flat"]["input_cache_miss"],
        0.03
    );
    assert_eq!(
        doc["models"]["glm-5.3-flashx"]["flat"]["input_cache_miss"],
        0.2
    );
    assert_eq!(doc["meta"]["usd_cny"], 7.1);
}

#[test]
fn settings_normalize_like_python() {
    let mut settings = Map::new();
    assert_eq!(
        pricing::apply_setting(&mut settings, "pricing_enabled", &json!(false)),
        Some(Ok("pricing_enabled".into()))
    );
    assert_eq!(settings["pricing_enabled"], false);
    assert_eq!(
        pricing::apply_setting(&mut settings, "pricing_refresh_hours", &json!(6)),
        Some(Ok("pricing_refresh_minutes".into()))
    );
    assert_eq!(settings["pricing_refresh_minutes"], 360.0);
    assert!(!settings.contains_key("pricing_refresh_hours"));
    assert!(
        pricing::apply_setting(&mut settings, "pricing_enabled", &json!("false"))
            .unwrap()
            .is_err()
    );
    let mut legacy = Map::from_iter([("pricing_refresh_hours".to_string(), json!(6))]);
    assert!(pricing::normalize_refresh_setting(&mut legacy));
    assert_eq!(legacy["pricing_refresh_minutes"], 360.0);
    assert!(!legacy.contains_key("pricing_refresh_hours"));
}

#[test]
fn pricing_status_has_python_key_set() {
    let dir = temp_dir("status");
    let pricing = pricing::Pricing::new(&dir);
    let status = pricing.status(&Map::new());
    let expected: BTreeSet<&str> = [
        "interval_minutes",
        "enabled",
        "master_enabled",
        "running",
        "last_run",
        "last_run_label",
        "next_run",
        "next_run_label",
        "last_error",
        "last_models",
        "last_extra",
        "policies",
        "models",
        "current_at",
        "current_at_label",
        "current",
        "gaps",
        "gap_summary",
        "variant_inherit",
        "overrides_file",
        "policies_file",
        "timeline",
        "logs",
    ]
    .into_iter()
    .collect();
    let actual: BTreeSet<&str> = status
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    assert_eq!(actual, expected);
    let _ = fs::remove_dir_all(dir);
}

#[test]
fn recent_usage_gets_python_prefixed_cost_fields() {
    let dir = temp_dir("recent-cost");
    let pricing = pricing::Pricing::new(&dir);
    let row = json!({
        "model":"deepseek-v4.1-flash","at":1790908275.0,
        "prompt_tokens":1000,"cached_tokens":200,"completion_tokens":300
    });
    let enriched = pricing.enrich_usage_row(&row, &Map::new());
    assert_eq!(enriched["cost_cny"], 0.00125);
    assert_eq!(enriched["cost_source"], "builtin");
    assert_eq!(enriched["cost_via"], "direct");
    assert_eq!(enriched["cost_currency"], "USD");
    let disabled = Map::from_iter([("pricing_enabled".to_string(), json!(false))]);
    let off = pricing.enrich_usage_row(&row, &disabled);
    assert_eq!(off["cost_cny"], Value::Null);
    assert_eq!(off["cost_source"], Value::Null);
    assert_eq!(off["cost_backfilled"], false);
    let _ = fs::remove_dir_all(dir);
}

#[test]
fn usage_record_stamps_current_policy() {
    let root = temp_dir("record");
    let accounts = root.join("accounts");
    let usage_dir = root.join("usage");
    fs::create_dir_all(&accounts).unwrap();
    fs::create_dir_all(&usage_dir).unwrap();
    let state = state::State::load(accounts.to_str().unwrap(), usage_dir.to_str().unwrap());
    let pricing = pricing::Pricing::new(&usage_dir);
    let entry = json!({
        "currency":"USD","unit":1000000,"or_id":"vendor/m",
        "flat":{"input_cache_hit":0.01,"input_cache_miss":0.03,"output":0.5}
    });
    let result = pricing.store.record_policies(
        &json!({"meta":{"usd_cny":7.1},"models":{"m":entry}}),
        Some(100.0),
        false,
    );
    let updates_dir = state.accounts_dir.to_string_lossy().to_string();
    let app = crate::App {
        state,
        upstream: reqwest::Client::new(),
        started: std::time::Instant::now(),
        listen_port: 0,
        updates: std::sync::Arc::new(crate::updates::Manager::new(&updates_dir)),
        inflight: Default::default(),
        recorder: usage::Recorder::new(usage_dir.clone()),
        pool: pool::Pool::new(),
        panel: panel::Panel::default(),
        pricing,
        scheduler: scheduler::Scheduler::default(),
        logs: Default::default(),
    };
    let model = "m";
    let credited = json!({"prompt_tokens":1,"completion_tokens":1,"total_tokens":2,"credit":0.5});
    record::record(
        &app,
        record::Row {
            model,
            stream: false,
            outcome: "completed",
            elapsed_ms: Some(1),
            ttft_ms: None,
            gen_ms: None,
            requested_model: None,
            usage: Some(&credited),
            fp: None,
            account: None,
            realm: "intl",
            key: "test",
            effort: None,
        },
    );
    let row: Value =
        serde_json::from_str(fs::read_to_string(app.recorder_path()).unwrap().trim()).unwrap();
    assert_eq!(row["cost_policy"], result.assignment["m"]);
    assert_eq!(row["credit"], 0.5);
    let _ = fs::remove_dir_all(root);
}
