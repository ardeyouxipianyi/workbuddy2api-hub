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

fn feed_perf(bucket: &mut Perf, row: &Row) {
    bucket.total += 1;
    match outcome(row) {
        "completed" => bucket.ok += 1,
        "client_aborted" => bucket.aborted += 1,
        _ => bucket.errors += 1,
    }
    if outcome(row) == "completed" {
        // Python 的判定不统一，照抄：ttft/gen/cache_hit 看「不是 None」（0 也收），
        // tokens_per_sec 看真值（0 不收）；wall 三种结果都收，但也看真值。
        for (key, target) in [
            ("ttft_ms", &mut bucket.ttft),
            ("gen_ms", &mut bucket.generation),
            ("cache_hit_pct", &mut bucket.cache),
        ] {
            if let Some(value) = optional(row, key) {
                target.push(value);
            }
        }
        if let Some(value) = optional(row, "tokens_per_sec") {
            if value != 0.0 {
                bucket.rates.push(value);
            }
        }
    }
    if let Some(value) = optional(row, "elapsed_ms") {
        if value != 0.0 {
            bucket.wall.push(value);
        }
    }
}

fn metric(values: &[f64]) -> Value {
    if values.is_empty() {
        return Value::Null;
    }
    let mut sorted = values.to_vec();
    sorted.sort_by(f64::total_cmp);
    // Python 的 round() 是「五取偶」：n=2 时 p50 落在 0.5 上取 0（取最小值而不是最大值）。
    // 用四舍五入会整整差一个样本，面板上就是另一个数。
    let rank = |percent: f64| {
        let index = (percent / 100.0 * (sorted.len() - 1) as f64).round_ties_even() as usize;
        sorted[index.min(sorted.len() - 1)]
    };
    json!({
        "avg": ((values.iter().sum::<f64>() / values.len() as f64) * 10.0).round_ties_even() / 10.0,
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

fn aggregate_perf(
    selected: Vec<&Row>,
    realms: &BTreeMap<String, String>,
    current: &str,
) -> PerfResults {
    let mut result = PerfResults {
        total: Perf::default(),
        by_model: BTreeMap::new(),
        by_realm: BTreeMap::new(),
        by_account: BTreeMap::new(),
    };
    for row in selected {
        feed_perf(&mut result.total, row);
        let model = row.model.clone().unwrap_or_else(|| "unknown".to_string());
        feed_perf(result.by_model.entry(model.clone()).or_default(), row);
        let realm = row_realm(row, realms, current);
        feed_perf(
            result
                .by_realm
                .entry(model.clone())
                .or_default()
                .entry(realm.clone())
                .or_default(),
            row,
        );
        let account = row.account.clone().unwrap_or_else(|| "(unattributed)".to_string());
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
    let capped = rows.len() >= sample;

    let start = rows.len().saturating_sub(sample);
    let sampled = &rows[start..];
    let realms = account_realms(app);
    let realm = requested_realm(req, app);
    // 区间必须过滤：不然「今日」把整份样本都算进去了（Python 是按窗口收样的）。
    let (since, until) = range_bounds(req);
    let in_window = |row: &&Row| {
        let at = number(row, "at");
        since.map_or(true, |lower| at >= lower) && until.map_or(true, |upper| at <= upper)
    };
    let current = app.state.current_realm();
    let selected: Vec<&Row> = rows_for_realm(sampled, &realm, &realms, &current)
        .into_iter()
        .filter(in_window)
        .collect();
    // shortcut: sample_from 取「样本切片的第一行」。Python 的环是增量裁剪的，
    // 上限 5000 行时首行落在哪一份窗口里取决于它的环形缓冲实现，这里没有逐字复刻
    // （见 KNOWN_DIFFS 的 sample_from）；升级时机＝有人真的用这个字段做判断。
    let sample_from = sampled
        .first()
        .map(|row| json!(row.at))
        .unwrap_or(Value::Null);
    let result = aggregate_perf(selected, &realms, &current);
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
