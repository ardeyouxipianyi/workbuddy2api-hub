//! Latency, throughput, and success-rate statistics.

use super::*;
use serde_json::Map;

#[derive(Default)]
struct Perf {
    total: i64,
    ok: i64,
    errors: i64,
    aborted: i64,
    ttft: Vec<f64>,
    generation: Vec<f64>,
    wall: Vec<f64>,
    rates: Vec<f64>,
    cache: Vec<f64>,
}

fn feed_perf(bucket: &mut Perf, row: &Value) {
    bucket.total += 1;
    match outcome(row) {
        "completed" => bucket.ok += 1,
        "client_aborted" => bucket.aborted += 1,
        _ => bucket.errors += 1,
    }
    if outcome(row) == "completed" {
        for (key, target) in [
            ("ttft_ms", &mut bucket.ttft),
            ("gen_ms", &mut bucket.generation),
            ("tokens_per_sec", &mut bucket.rates),
            ("cache_hit_pct", &mut bucket.cache),
        ] {
            if let Some(value) = row.get(key).and_then(Value::as_f64) {
                target.push(value);
            }
        }
    }
    if outcome(row) != "client_aborted" {
        if let Some(value) = row.get("elapsed_ms").and_then(Value::as_f64) {
            bucket.wall.push(value);
        }
    } else if let Some(value) = row.get("elapsed_ms").and_then(Value::as_f64) {
        bucket.wall.push(value);
    }
}

fn metric(values: &[f64]) -> Value {
    if values.is_empty() {
        return Value::Null;
    }
    let mut sorted = values.to_vec();
    sorted.sort_by(f64::total_cmp);
    let rank = |percent: f64| {
        let index = (percent / 100.0 * (sorted.len() - 1) as f64).round() as usize;
        sorted[index.min(sorted.len() - 1)]
    };
    json!({
        "avg": ((values.iter().sum::<f64>() / values.len() as f64) * 10.0).round() / 10.0,
        "p50": rank(50.0),
        "p90": rank(90.0),
        "p99": rank(99.0),
        "max": sorted[sorted.len() - 1],
        "samples": values.len(),
    })
}

fn percent(ok: i64, errors: i64) -> Value {
    if ok + errors == 0 {
        Value::Null
    } else {
        json!((ok as f64 * 1000.0 / (ok + errors) as f64).round() / 10.0)
    }
}

fn perf_entry(bucket: &Perf) -> Value {
    json!({
        "requests": bucket.total,
        "errors": bucket.errors,
        "client_aborted": bucket.aborted,
        "success_rate_pct": percent(bucket.ok, bucket.errors),
        "ttft_ms": metric(&bucket.ttft),
        "generation_ms": metric(&bucket.generation),
        "wall_ms": metric(&bucket.wall),
        "tokens_per_sec": metric(&bucket.rates),
        "cache_hit_pct": metric(&bucket.cache),
    })
}

struct PerfResults {
    total: Perf,
    by_model: BTreeMap<String, Perf>,
    by_realm: BTreeMap<String, BTreeMap<String, Perf>>,
    by_account: BTreeMap<String, BTreeMap<String, BTreeMap<String, Perf>>>,
}

fn aggregate_perf(selected: Vec<&Value>, realms: &BTreeMap<String, String>) -> PerfResults {
    let mut result = PerfResults {
        total: Perf::default(),
        by_model: BTreeMap::new(),
        by_realm: BTreeMap::new(),
        by_account: BTreeMap::new(),
    };
    for row in selected {
        feed_perf(&mut result.total, row);
        let model = row
            .get("model")
            .and_then(Value::as_str)
            .unwrap_or("unknown")
            .to_string();
        feed_perf(result.by_model.entry(model.clone()).or_default(), row);
        let realm = row_realm(row, realms).to_string();
        feed_perf(
            result
                .by_realm
                .entry(model.clone())
                .or_default()
                .entry(realm.clone())
                .or_default(),
            row,
        );
        let account = row
            .get("account")
            .and_then(Value::as_str)
            .unwrap_or("(unattributed)")
            .to_string();
        feed_perf(
            result
                .by_account
                .entry(model)
                .or_default()
                .entry(realm)
                .or_default()
                .entry(account)
                .or_default(),
            row,
        );
    }
    result
}

fn model_map(buckets: &BTreeMap<String, Perf>) -> Map<String, Value> {
    buckets
        .iter()
        .map(|(key, value)| (key.clone(), perf_entry(value)))
        .collect()
}

fn realm_map(buckets: &BTreeMap<String, BTreeMap<String, Perf>>) -> Map<String, Value> {
    buckets
        .iter()
        .map(|(model, realms)| {
            let nested: Map<String, Value> = realms
                .iter()
                .map(|(realm, value)| (realm.clone(), perf_entry(value)))
                .collect();
            (model.clone(), Value::Object(nested))
        })
        .collect()
}

fn account_map(
    buckets: &BTreeMap<String, BTreeMap<String, BTreeMap<String, Perf>>>,
) -> Map<String, Value> {
    buckets
        .iter()
        .map(|(model, realms)| {
            let nested: Map<String, Value> = realms
                .iter()
                .map(|(realm, accounts)| {
                    let rows: Map<String, Value> = accounts
                        .iter()
                        .map(|(uid, value)| (uid.clone(), perf_entry(value)))
                        .collect();
                    (realm.clone(), Value::Object(rows))
                })
                .collect();
            (model.clone(), Value::Object(nested))
        })
        .collect()
}

/// `/usage/perf`: latest N rows for the selected realm.
pub fn perf(app: &Arc<App>, req: &Request<Incoming>) -> Resp {
    let sample = query(req, "sample")
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(5_000)
        .clamp(10, 20_000);
    let rows = read_rows(app);
    let sample_from = rows
        .first()
        .and_then(|row| row.get("at"))
        .cloned()
        .unwrap_or(Value::Null);
    let capped = rows.len() >= sample;
    let start = rows.len().saturating_sub(sample);
    let sampled = &rows[start..];
    let realms = account_realms(app);
    let realm = requested_realm(req, app);
    let selected = rows_for_realm(sampled, &realm, &realms);
    let result = aggregate_perf(selected, &realms);
    let total = result.total;
    json_resp(
        StatusCode::OK,
        &json!({
            "sampled": total.total,
            "sample_from": sample_from,
            "sample_capped": capped,
            "success": total.ok,
            "errors": total.errors,
            "client_aborted": total.aborted,
            "success_rate_pct": percent(total.ok, total.errors),
            "ttft_ms": metric(&total.ttft),
            "generation_ms": metric(&total.generation),
            "wall_ms": metric(&total.wall),
            "tokens_per_sec": metric(&total.rates),
            "cache_hit_pct": metric(&total.cache),
            "by_model": model_map(&result.by_model),
            "by_model_realm": realm_map(&result.by_realm),
            "by_model_acct": account_map(&result.by_account),
        }),
    )
}
