//! Usage totals, recent rows, and per-account totals.

use super::*;
use serde_json::Map;

fn overview_bucket(rows: &[&Value]) -> Value {
    let mut accounts: BTreeMap<String, i64> = BTreeMap::new();
    let mut out = json!({
        "requests": rows.len() as i64,
        "accounts": {},
        "cost_cny": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "total_tokens": 0,
        "credit": 0.0,
    });
    for row in rows {
        let account = row
            .get("account")
            .and_then(Value::as_str)
            .unwrap_or("(unattributed)");
        *accounts.entry(account.to_string()).or_default() += 1;
        for key in [
            "prompt_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "cached_tokens",
            "total_tokens",
        ] {
            out[key] = json!(integer(&out, key) + integer(row, key));
        }
        out["credit"] = json!(number(&out, "credit") + number(row, "credit"));
    }
    out["accounts"] = json!(accounts);
    out
}

fn group_overview_maps<'a>(
    rows: &[&'a Value],
    realms: &BTreeMap<String, String>,
) -> (
    BTreeMap<String, Vec<&'a Value>>,
    BTreeMap<String, BTreeMap<String, Vec<&'a Value>>>,
    BTreeMap<String, BTreeMap<String, BTreeMap<String, Vec<&'a Value>>>>,
) {
    let mut by_account: BTreeMap<(String, String, String), Vec<&'a Value>> = BTreeMap::new();
    let mut by_realm: BTreeMap<(String, String), Vec<&'a Value>> = BTreeMap::new();
    let mut by_model: BTreeMap<String, Vec<&'a Value>> = BTreeMap::new();
    for row in rows {
        let model = row
            .get("model")
            .and_then(Value::as_str)
            .unwrap_or("?")
            .to_string();
        let realm = row_realm(row, realms).to_string();
        let account = row
            .get("account")
            .and_then(Value::as_str)
            .unwrap_or("(unattributed)")
            .to_string();
        by_account
            .entry((model.clone(), realm.clone(), account))
            .or_default()
            .push(*row);
        by_realm
            .entry((model.clone(), realm))
            .or_default()
            .push(*row);
        by_model.entry(model).or_default().push(*row);
    }
    let mut realm_models: BTreeMap<String, BTreeMap<String, Vec<&'a Value>>> = BTreeMap::new();
    for ((model, realm), items) in by_realm {
        realm_models.entry(model).or_default().insert(realm, items);
    }
    let mut account_models: BTreeMap<String, BTreeMap<String, BTreeMap<String, Vec<&'a Value>>>> =
        BTreeMap::new();
    for ((model, realm, account), items) in by_account {
        account_models
            .entry(model)
            .or_default()
            .entry(realm)
            .or_default()
            .insert(account, items);
    }
    (by_model, realm_models, account_models)
}

fn overview_maps(rows: &[&Value], realms: &BTreeMap<String, String>) -> (Value, Value, Value) {
    let (by_model, realm_models, account_models) = group_overview_maps(rows, realms);
    let by_model: Map<String, Value> = by_model
        .into_iter()
        .map(|(model, items)| (model, overview_bucket(&items)))
        .collect();
    let by_realm = realm_models
        .into_iter()
        .map(|(model, realms)| {
            let realms: Map<String, Value> = realms
                .into_iter()
                .map(|(realm, items)| (realm, overview_bucket(&items)))
                .collect();
            (model, Value::Object(realms))
        })
        .collect();
    let by_account = account_models
        .into_iter()
        .map(|(model, realms)| {
            let realms = realms
                .into_iter()
                .map(|(realm, accounts)| {
                    let accounts: Map<String, Value> = accounts
                        .into_iter()
                        .map(|(account, items)| (account, overview_bucket(&items)))
                        .collect();
                    (realm, Value::Object(accounts))
                })
                .collect();
            (model, Value::Object(realms))
        })
        .collect();
    (
        Value::Object(by_model),
        Value::Object(by_realm),
        Value::Object(by_account),
    )
}

fn add_overview_totals(totals: &mut Value, selected: &[&Value]) {
    for row in selected {
        totals["requests"] = json!(integer(totals, "requests") + 1);
        for key in [
            "prompt_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "cached_tokens",
            "total_tokens",
        ] {
            totals[key] = json!(integer(totals, key) + integer(row, key));
        }
        if outcome(row) != "completed" {
            totals["errors"] = json!(integer(totals, "errors") + 1);
        }
        for (source, sum_key, count_key) in [
            ("ttft_ms", "ttft_ms_sum", "ttft_samples"),
            ("gen_ms", "gen_ms_sum", "gen_samples"),
            ("elapsed_ms", "wall_ms_sum", "wall_samples"),
        ] {
            if let Some(value) = row.get(source).and_then(Value::as_i64) {
                totals[sum_key] = json!(integer(totals, sum_key) + value);
                totals[count_key] = json!(integer(totals, count_key) + 1);
            }
        }
    }
}

fn overview_accounts(app: &App) -> Value {
    let accounts_map: Map<String, Value> = app
        .state
        .accounts_snapshot()
        .into_iter()
        .map(|account| {
            let uid = account.uid();
            let nickname = if account.str("nickname").is_empty() {
                uid.chars().take(8).collect::<String>()
            } else {
                account.str("nickname").to_string()
            };
            (uid, json!({"nickname": nickname, "realm": account.realm()}))
        })
        .collect();
    json!({
        "accounts_map": accounts_map,
        "accounts": app.state.accounts_snapshot().len(),
        "accounts_ready": app.state.ready_count(),
    })
}

fn representative_account(app: &App, realm: &str) -> Value {
    let rep = representative(app, realm);
    json!({
        "uid": rep.as_ref().map(|item| item.uid()).unwrap_or_default(),
        "domain": rep.as_ref().map(|item| item.domain()).unwrap_or_default(),
        "issuer": "",
        "credential_file": rep.as_ref().map(|item| item.file_name()).unwrap_or_default(),
        "expires_at": rep.as_ref().map(|item| item.expires_at()).unwrap_or(0),
    })
}

/// `/usage`: all-time totals for the selected realm plus pool metadata.
pub fn overview(app: &Arc<App>, req: &Request<Incoming>) -> Resp {
    let rows = read_rows(app);
    let realms = account_realms(app);
    let realm = requested_realm(req, app);
    let (since, until) = range_bounds(req);
    let selected: Vec<&Value> = rows_for_realm(&rows, &realm, &realms)
        .into_iter()
        .filter(|row| {
            if since.is_none() && until.is_none() {
                return true;
            }
            row_at(row).is_some_and(|at| {
                since.map(|lower| at >= lower).unwrap_or(true)
                    && until.map(|upper| at <= upper).unwrap_or(true)
            })
        })
        .collect();
    let mut totals = json!({
        "requests": 0, "errors": 0, "prompt_tokens": 0, "completion_tokens": 0,
        "reasoning_tokens": 0, "cached_tokens": 0, "total_tokens": 0,
        "credit": 0.0, "cost_cny": 0.0, "cost_missing": {},
        "started": crate::state::now_epoch() as f64,
        "by_model": {}, "by_model_realm": {}, "by_model_acct": {},
        "ttft_ms_sum": 0, "ttft_samples": 0, "gen_ms_sum": 0, "gen_samples": 0,
        "wall_ms_sum": 0, "wall_samples": 0,
    });
    add_overview_totals(&mut totals, &selected);
    let (by_model, by_model_realm, by_model_acct) = overview_maps(&selected, &realms);
    totals["by_model"] = by_model;
    totals["by_model_realm"] = by_model_realm;
    totals["by_model_acct"] = by_model_acct;
    let pool = overview_accounts(app);
    totals["since"] = json!(crate::state::local_stamp());
    totals["log_file"] = json!(app.recorder_path().display().to_string());
    totals["realm"] = json!(realm);
    totals["usd_cny"] = json!(7.1);
    totals["accounts_map"] = pool["accounts_map"].clone();
    let mut account = representative_account(app, &realm);
    account["accounts"] = pool["accounts"].clone();
    account["accounts_ready"] = pool["accounts_ready"].clone();
    totals["account"] = account;
    json_resp(StatusCode::OK, &totals)
}

pub fn by_account(app: &Arc<App>) -> Resp {
    let rows = read_rows(app);
    let mut grouped: BTreeMap<String, Vec<&Value>> = BTreeMap::new();
    for row in rows.iter().filter(|row| row.get("error").is_none()) {
        let account = row
            .get("account")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .unwrap_or("(unattributed)");
        grouped.entry(account.to_string()).or_default().push(row);
    }
    let mut accounts: Vec<Value> = grouped
        .into_iter()
        .map(|(account, items)| {
            let mut models: BTreeMap<String, i64> = BTreeMap::new();
            for row in &items {
                let model = row.get("model").and_then(Value::as_str).unwrap_or("?");
                *models.entry(model.to_string()).or_default() += 1;
            }
            let mut models: Vec<(String, i64)> = models.into_iter().collect();
            models.sort_by(|left, right| right.1.cmp(&left.1).then_with(|| left.0.cmp(&right.0)));
            json!({
                "account": account,
                "requests": items.len(),
                "prompt_tokens": items.iter().map(|row| integer(row, "prompt_tokens")).sum::<i64>(),
                "completion_tokens": items.iter().map(|row| integer(row, "completion_tokens")).sum::<i64>(),
                "reasoning_tokens": items.iter().map(|row| integer(row, "reasoning_tokens")).sum::<i64>(),
                "cached_tokens": items.iter().map(|row| integer(row, "cached_tokens")).sum::<i64>(),
                "total_tokens": items.iter().map(|row| integer(row, "total_tokens")).sum::<i64>(),
                "models": models.into_iter().take(5).collect::<Vec<_>>(),
            })
        })
        .collect();
    accounts
        .sort_by(|left, right| integer(right, "total_tokens").cmp(&integer(left, "total_tokens")));
    json_resp(StatusCode::OK, &json!({"accounts": accounts}))
}

fn recent_row(row: &Value) -> Value {
    let mut row = row.clone();
    if let Some(object) = row.as_object_mut() {
        let model = object
            .get("model")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        object.insert("key_name".into(), json!(""));
        object.insert("cost_cny".into(), json!(0.0));
        object.insert("cost_band".into(), Value::Null);
        object.insert("cost_source".into(), json!("builtin"));
        object.insert("cost_source_at".into(), Value::Null);
        object.insert("cost_backfilled".into(), json!(false));
        object.insert(
            "cost_rates".into(),
            json!({"input_cache_hit": 0.0, "input_cache_miss": 0.0, "output": 0.0}),
        );
        object.insert("cost_unit".into(), json!(1_000_000.0));
        object.insert("cost_currency".into(), json!("USD"));
        object.insert("cost_usd_cny".into(), json!(7.1));
        object.insert("cost_or_id".into(), json!(model));
        object.insert("cost_via".into(), json!("direct"));
        object.insert("cost_inherited_from".into(), Value::Null);
        object.insert("cost_override_from".into(), Value::Null);
        object.insert("cost_band_note".into(), Value::Null);
        object.insert("cost_via_derived".into(), json!(false));
    }
    row
}

pub fn recent(app: &Arc<App>, req: &Request<Incoming>) -> Resp {
    let limit = query(req, "limit")
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(100)
        .clamp(1, 1_000);
    let page = query(req, "page")
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(1)
        .max(1);
    let rows = read_rows(app);
    let realms = account_realms(app);
    let realm = requested_realm(req, app);
    let mut selected: Vec<&Value> = rows_for_realm(&rows, &realm, &realms);
    selected.reverse();
    let total = selected.len();
    let start = (page - 1).saturating_mul(limit);
    let page_rows: Vec<Value> = selected
        .into_iter()
        .skip(start)
        .take(limit)
        .map(recent_row)
        .collect();
    json_resp(
        StatusCode::OK,
        &json!({
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": if total == 0 { 1 } else { (total + limit - 1) / limit },
            "usd_cny": 7.1,
            "rows": page_rows,
        }),
    )
}
