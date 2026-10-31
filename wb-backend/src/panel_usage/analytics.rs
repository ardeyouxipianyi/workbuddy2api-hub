//! Usage analytics grouped by account, model, and API key.

use super::*;
use serde_json::Map;

fn feed_analytics(out: &mut Value, row: &Value) {
    if outcome(row) == "client_aborted" {
        return;
    }
    if outcome(row) == "completed" {
        out["requests"] = json!(integer(out, "requests") + 1);
    } else {
        out["errors"] = json!(integer(out, "errors") + 1);
    }
    for key in [
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "total_tokens",
    ] {
        out[key] = json!(integer(out, key) + integer(row, key));
    }
    out["credit"] = json!(number(out, "credit") + number(row, "credit"));
    for (source, sum, count) in [
        ("ttft_ms", "ttft_sum", "ttft_n"),
        ("tokens_per_sec", "speed_sum", "speed_n"),
        ("elapsed_ms", "elapsed_sum", "elapsed_n"),
    ] {
        if let Some(value) = row.get(source).and_then(Value::as_f64) {
            out[sum] = json!(number(out, sum) + value);
            out[count] = json!(integer(out, count) + 1);
        }
    }
}

fn finalize_analytics(out: &mut Value) {
    let prompt = integer(&out, "prompt_tokens");
    let cached = integer(&out, "cached_tokens");
    let completion = integer(&out, "completion_tokens");
    let reasoning = integer(&out, "reasoning_tokens");
    out["cache_hit_pct"] = json!(if prompt > 0 {
        ((cached as f64 * 1000.0 / prompt as f64).round()) / 10.0
    } else {
        0.0
    });
    out["reasoning_ratio"] = json!(if completion > 0 {
        ((reasoning as f64 * 1000.0 / completion as f64).round()) / 10.0
    } else {
        0.0
    });
    for (sum, count, avg, integer_avg) in [
        ("ttft_sum", "ttft_n", "ttft_ms_avg", true),
        ("speed_sum", "speed_n", "speed_avg", false),
        ("elapsed_sum", "elapsed_n", "elapsed_ms_avg", true),
    ] {
        let n = integer(&out, count);
        let value = number(&out, sum);
        out[avg] = if n == 0 {
            if integer_avg {
                json!(0)
            } else {
                json!(0.0)
            }
        } else if integer_avg {
            json!((value / n as f64).round() as i64)
        } else {
            json!((value * 10.0 / n as f64).round() / 10.0)
        };
    }
}

fn bucket(rows: &[&Value]) -> Value {
    let mut out = json!({
        "requests": 0, "errors": 0, "prompt_tokens": 0, "completion_tokens": 0,
        "reasoning_tokens": 0, "cached_tokens": 0, "total_tokens": 0,
        "credit": 0.0, "cost_cny": 0.0, "ttft_sum": 0.0, "ttft_n": 0,
        "speed_sum": 0.0, "speed_n": 0, "elapsed_sum": 0.0, "elapsed_n": 0,
        "cache_hit_pct": 0.0, "reasoning_ratio": 0.0,
        "ttft_ms_avg": 0, "speed_avg": 0.0, "elapsed_ms_avg": 0,
    });
    for row in rows {
        feed_analytics(&mut out, row);
    }
    finalize_analytics(&mut out);
    out
}

fn model_breakdown(rows: &[&Value]) -> Map<String, Value> {
    let mut grouped: BTreeMap<String, Vec<&Value>> = BTreeMap::new();
    for row in rows
        .iter()
        .copied()
        .filter(|row| outcome(row) == "completed")
    {
        let model = row
            .get("model")
            .and_then(Value::as_str)
            .unwrap_or("(unknown)");
        grouped.entry(model.to_string()).or_default().push(row);
    }
    grouped
        .into_iter()
        .map(|(model, items)| {
            let requests = items.len() as i64;
            let tokens: i64 = items.iter().map(|row| integer(row, "total_tokens")).sum();
            let reasoning: i64 = items
                .iter()
                .map(|row| integer(row, "reasoning_tokens"))
                .sum();
            (
                model,
                json!({
                    "requests": requests,
                    "tokens": tokens,
                    "reasoning": reasoning,
                    "cost_cny": 0.0,
                }),
            )
        })
        .collect()
}

fn top_models(models: &Map<String, Value>) -> (Vec<Value>, Value) {
    let mut rows: Vec<(&String, &Value)> = models.iter().collect();
    rows.sort_by(|left, right| {
        integer(right.1, "tokens")
            .cmp(&integer(left.1, "tokens"))
            .then_with(|| integer(right.1, "requests").cmp(&integer(left.1, "requests")))
            .then_with(|| left.0.cmp(right.0))
    });
    let head = rows
        .iter()
        .take(5)
        .map(|(model, stat)| {
            json!({
                "model": model,
                "requests": integer(stat, "requests"),
                "tokens": integer(stat, "tokens"),
                "reasoning": integer(stat, "reasoning"),
            })
        })
        .collect();
    (head, Value::Null)
}

fn key_id(row: &Value) -> String {
    if row.get("key").is_none() {
        "__before_keys__".into()
    } else {
        row.get("key")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .unwrap_or("__no_key__")
            .to_string()
    }
}

fn key_rows(rows: &[&Value]) -> Vec<Value> {
    let mut grouped: BTreeMap<String, Vec<&Value>> = BTreeMap::new();
    for row in rows {
        grouped.entry(key_id(row)).or_default().push(*row);
    }
    grouped
        .into_iter()
        .map(|(key, items)| {
            let (name, source) = match key.as_str() {
                "__before_keys__" => ("(切换前)", "bucket"),
                "__no_key__" => ("(无 key)", "bucket"),
                _ => ("(未知 key)", "bucket"),
            };
            let mut realms: BTreeMap<String, i64> = BTreeMap::new();
            let mut last_at: f64 = 0.0;
            for row in &items {
                let realm = row.get("realm").and_then(Value::as_str).unwrap_or("");
                *realms.entry(realm.to_string()).or_default() += 1;
                last_at = last_at.max(number(row, "at"));
            }
            let breakdown = model_breakdown(&items);
            let (models, models_other) = top_models(&breakdown);
            json!({
                "key": key,
                "name": name,
                "realm": "",
                "enabled": false,
                "source": source,
                "window": bucket(&items),
                "all_time": bucket(&items),
                "window_models": breakdown,
                "all_models": breakdown,
                "realms": realms,
                "last_at": last_at,
                "models": models,
                "models_other": models_other,
                "cross_realm": realms.keys().filter(|realm| !realm.is_empty()).count() > 1,
            })
        })
        .collect()
}

fn analytics_accounts(app: &App, rows: &[&Value], realm: Option<&str>) -> Vec<Value> {
    let mut grouped: BTreeMap<String, Vec<&Value>> = BTreeMap::new();
    for row in rows {
        let uid = row
            .get("account")
            .and_then(Value::as_str)
            .unwrap_or("(unattributed)");
        grouped.entry(uid.to_string()).or_default().push(*row);
    }
    let accounts = app.state.accounts_snapshot();
    let ids: std::collections::BTreeSet<String> = accounts
        .iter()
        .filter(|account| realm.is_none() || realm == Some(account.realm().as_str()))
        .map(|account| account.uid())
        .chain(grouped.keys().cloned())
        .collect();
    ids.into_iter()
        .map(|uid| {
            let account = accounts.iter().find(|account| account.uid() == uid);
            let items = grouped.get(&uid).map(Vec::as_slice).unwrap_or(&[]);
            let models = model_breakdown(items);
            json!({
                "uid": uid,
                "nickname": account
                    .map(|value| value.str("nickname").to_string())
                    .filter(|value| !value.is_empty())
                    .unwrap_or_else(|| uid.chars().take(8).collect::<String>()),
                "realm": account.map(|value| value.realm()).unwrap_or_default(),
                "domain": account.map(|value| value.domain()).unwrap_or_default(),
                "credits": account
                    .and_then(|value| value.data.get("credits"))
                    .cloned()
                    .unwrap_or_else(|| json!({})),
                "window": bucket(items),
                "all_time": bucket(items),
                "window_models": models,
                "all_models": models,
            })
        })
        .collect()
}

fn analytics_models(rows: &[&Value]) -> Vec<Value> {
    let mut grouped: BTreeMap<String, Vec<&Value>> = BTreeMap::new();
    for row in rows {
        let model = row
            .get("model")
            .and_then(Value::as_str)
            .unwrap_or("(unknown)");
        grouped.entry(model.to_string()).or_default().push(*row);
    }
    grouped
        .into_iter()
        .map(|(model, items)| {
            json!({
                "model": model,
                "window": bucket(&items),
                "all_time": bucket(&items),
            })
        })
        .collect()
}

/// `/usage/analytics`: all-time plus an optional explicit window.
pub fn analytics(app: &Arc<App>, req: &Request<Incoming>) -> Resp {
    let all = read_rows(app);
    let realms = account_realms(app);
    let realm = query(req, "realm");
    let all_rows = rows_for_realm(&all, realm.unwrap_or("all"), &realms);
    // 面板发的是 range=today|week|month（区间按钮），只有自定义区间才带 since/until。
    // 只读 since/until 会让所有区间都落进「全时段」，就是「今天显示出全部数据」。
    let (range_since, range_until) = range_bounds(req);
    let since = query(req, "since")
        .and_then(|value| value.parse::<f64>().ok())
        .or(range_since);
    let until = query(req, "until")
        .and_then(|value| value.parse::<f64>().ok())
        .or(range_until);
    let window_rows: Vec<&Value> = match (since, until) {
        (None, None) => all_rows.clone(),
        _ => all_rows
            .iter()
            .copied()
            .filter(|row| number(row, "at") >= since.unwrap_or(f64::NEG_INFINITY))
            .filter(|row| number(row, "at") <= until.unwrap_or(f64::INFINITY))
            .collect(),
    };
    let models = analytics_models(&window_rows);
    let keys = key_rows(&window_rows);
    let accounts = analytics_accounts(app, &window_rows, realm);
    json_resp(
        StatusCode::OK,
        &json!({
            "window": {
                "since": since.map_or(Value::Null, |value| json!(value)),
                "until": until.map_or(Value::Null, |value| json!(value)),
            },
            "realm": realm.unwrap_or("all"),
            "usd_cny": 7.1,
            "summary": {
                "window": bucket(&window_rows),
                "all_time": bucket(&all_rows),
            },
            "accounts": accounts,
            "models": models,
            "keys": keys,
        }),
    )
}
