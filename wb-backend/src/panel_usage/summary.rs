//! Usage totals, recent rows, and per-account totals.

use super::*;
use crate::pricing_store::RowCost;
use serde_json::Map;

fn overview_bucket(rows: &[&Value], costs: &[RowCost], indices: &[usize]) -> Value {
    let mut accounts: BTreeMap<String, i64> = BTreeMap::new();
    let mut out = json!({
        "requests": 0,
        "accounts": {},
        "cost_cny": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "total_tokens": 0,
        "credit": 0.0,
    });
    for &index in indices {
        let row = rows[index];
        // 与 Python 的折叠规则一致：requests/tokens/账号计数/成本只算 completed。
        if outcome(row) != "completed" {
            continue;
        }
        let account = row
            .get("account")
            .and_then(Value::as_str)
            .unwrap_or("(unattributed)");
        *accounts.entry(account.to_string()).or_default() += 1;
        out["requests"] = json!(integer(&out, "requests") + 1);
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
        fold_cost(&mut out, row, costs[index]);
    }
    out["accounts"] = json!(accounts);
    out
}

/// Python 的 _fold_cost：可计价的累加金额，不可计价的按模型记一笔「没价」，
/// 主开关关掉的行不算「没价」（整块功能没开，不是缺价）。
fn fold_cost(bucket: &mut Value, row: &Value, cost: RowCost) {
    if cost.known {
        bucket["cost_cny"] = json!(number(bucket, "cost_cny") + cost.cny);
    } else if !cost.disabled {
        // 只在真遇到没价的行时才建这张表：Python 的桶里没有空 cost_missing。
        if !bucket.get("cost_missing").is_some_and(Value::is_object) {
            bucket["cost_missing"] = json!({});
        }
        let model = row.get("model").and_then(Value::as_str).unwrap_or("unknown");
        let current = bucket
            .get("cost_missing")
            .and_then(|value| value.get(model))
            .and_then(Value::as_i64)
            .unwrap_or(0);
        bucket["cost_missing"][model] = json!(current + 1);
    }
}

fn group_overview_maps(
    rows: &[&Value],
    realms: &BTreeMap<String, String>,
) -> (
    BTreeMap<String, Vec<usize>>,
    BTreeMap<String, BTreeMap<String, Vec<usize>>>,
    BTreeMap<String, BTreeMap<String, BTreeMap<String, Vec<usize>>>>,
) {
    // 按行下标分组：成本是逐行算的（costs 与 rows 同序），存引用就拿不到它。
    let mut by_account: BTreeMap<(String, String, String), Vec<usize>> = BTreeMap::new();
    let mut by_realm: BTreeMap<(String, String), Vec<usize>> = BTreeMap::new();
    let mut by_model: BTreeMap<String, Vec<usize>> = BTreeMap::new();
    for (index, row) in rows.iter().enumerate() {
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
            .push(index);
        by_realm
            .entry((model.clone(), realm))
            .or_default()
            .push(index);
        by_model.entry(model).or_default().push(index);
    }
    let mut realm_models: BTreeMap<String, BTreeMap<String, Vec<usize>>> = BTreeMap::new();
    for ((model, realm), items) in by_realm {
        realm_models.entry(model).or_default().insert(realm, items);
    }
    let mut account_models: BTreeMap<String, BTreeMap<String, BTreeMap<String, Vec<usize>>>> =
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

fn overview_maps(
    rows: &[&Value],
    costs: &[RowCost],
    realms: &BTreeMap<String, String>,
) -> (Value, Value, Value) {
    let (by_model, realm_models, account_models) = group_overview_maps(rows, realms);
    let by_model: Map<String, Value> = by_model
        .into_iter()
        .map(|(model, items)| (model, overview_bucket(rows, costs, &items)))
        .collect();
    let by_realm = realm_models
        .into_iter()
        .map(|(model, realms)| {
            let realms: Map<String, Value> = realms
                .into_iter()
                .map(|(realm, items)| (realm, overview_bucket(rows, costs, &items)))
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
                        .map(|(account, items)| (account, overview_bucket(rows, costs, &items)))
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

fn add_overview_totals(totals: &mut Value, selected: &[&Value], costs: &[RowCost]) {
    for (index, row) in selected.iter().enumerate() {
        // Python 的口径：requests/tokens 只算 completed；失败行进 errors，但
        // 只要上游已经扣过费（不是客户端中断）就要计入 credit —— 否则面板
        // 「今日」的额度花销永远是 0。
        if outcome(row) == "completed" {
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
            totals["credit"] = json!(number(totals, "credit") + number(row, "credit"));
            fold_cost(totals, row, costs[index]);
        } else {
            totals["errors"] = json!(integer(totals, "errors") + 1);
            if outcome(row) != "client_aborted" {
                totals["credit"] = json!(number(totals, "credit") + number(row, "credit"));
                // 客户端中断的行连成本也不能算：用的是不完整的 usage 块。
                fold_cost(totals, row, costs[index]);
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
    let settings = app.state.settings_snapshot();
    let costs = app.pricing.cost_rows(&selected, &settings);
    add_overview_totals(&mut totals, &selected, &costs);
    let (by_model, by_model_realm, by_model_acct) = overview_maps(&selected, &costs, &realms);
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

/// 面板「最近请求」的显示名：settings 里 key 的 id -> name，name 空就回退 id，
/// 未知 id 原样返回，空 id 返回空串（与 Python 的 key_label 一致）。
fn key_label(settings: &Map<String, Value>, key_id: &str) -> String {
    if key_id.is_empty() {
        return String::new();
    }
    if let Some(Value::Array(keys)) = settings.get("api_keys") {
        for entry in keys {
            let id = entry.get("id").and_then(Value::as_str).unwrap_or("");
            if id == key_id {
                let name = entry.get("name").and_then(Value::as_str).unwrap_or("").trim();
                return if name.is_empty() { id.to_string() } else { name.to_string() };
            }
        }
    }
    key_id.to_string()
}

/// 每行补面板要的成本明细（cost_cny/band/rates/…）。这是 Python 的
/// cost_for_row 展示路径：details=true 才有 rates、band_note 这些悬停卡片字段。
fn recent_row(row: &Value, app: &App, settings: &Map<String, Value>) -> Value {
    let mut out = app.pricing.enrich_usage_row(row, settings);
    let key_id = out.get("key").and_then(Value::as_str).unwrap_or("").to_string();
    if let Some(object) = out.as_object_mut() {
        object.insert("key_name".into(), json!(key_label(settings, &key_id)));
    }
    out
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
    let settings = app.state.settings_snapshot();
    let page_rows: Vec<Value> = selected
        .into_iter()
        .skip(start)
        .take(limit)
        .map(|row| recent_row(row, app, &settings))
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
