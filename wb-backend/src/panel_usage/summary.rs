//! Usage totals, recent rows, and per-account totals.

use super::*;
use crate::pricing_store::RowCost;
use crate::usage_index::{Cell, CellKey, Index, Tokens};
use serde_json::Map;

fn overview_bucket(
    rows: &[&Row],
    costs: &[RowCost],
    indices: &[usize],
    current: &str,
) -> Value {
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
            .account
            .as_deref()
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
        // 桶里只累加能算出来的成本；「没价」只记在汇总里（Python 的 per 桶没有 cost_missing）。
        if costs[index].known {
            out["cost_cny"] = json!(number(&out, "cost_cny") + costs[index].cny);
        }
    }
    out["accounts"] = json!(accounts);
    out
}

/// Python 的 _fold_cost：可计价的累加金额，不可计价的按模型记一笔「没价」，
/// 主开关关掉的行不算「没价」（整块功能没开，不是缺价）。
fn fold_cost(bucket: &mut Value, row: &Row, cost: RowCost) {
    if cost.known {
        bucket["cost_cny"] = json!(number(bucket, "cost_cny") + cost.cny);
    } else if !cost.disabled {
        // 只在真遇到没价的行时才建这张表：Python 的桶里没有空 cost_missing。
        if !bucket.get("cost_missing").is_some_and(Value::is_object) {
            bucket["cost_missing"] = json!({});
        }
        let model = row.model.as_deref().unwrap_or("unknown");
        let current = bucket
            .get("cost_missing")
            .and_then(|value| value.get(model))
            .and_then(Value::as_i64)
            .unwrap_or(0);
        bucket["cost_missing"][model] = json!(current + 1);
    }
}

fn group_overview_maps(
    rows: &[&Row],
    realms: &BTreeMap<String, String>,
    current: &str,
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
        // 桶只为 completed 的行建：Python 的折叠在非 completed 分支就 return 了，
        // 否则「只有失败请求」的模型会在面板上长出一个全 0 的卡片。
        if outcome(row) != "completed" {
            continue;
        }
        let model = row.model.clone().unwrap_or_else(|| "?".to_string());
        let realm = row_realm(row, realms, current);
        let account = row
            .account
            .as_deref()
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
    rows: &[&Row],
    costs: &[RowCost],
    realms: &BTreeMap<String, String>,
    current: &str,
) -> (Value, Value, Value) {
    let (by_model, realm_models, account_models) = group_overview_maps(rows, realms, current);
    let by_model: Map<String, Value> = by_model
        .into_iter()
        .map(|(model, items)| (model, overview_bucket(rows, costs, &items, current)))
        .collect();
    let by_realm = realm_models
        .into_iter()
        .map(|(model, realms)| {
            let realms: Map<String, Value> = realms
                .into_iter()
                .map(|(realm, items)| (realm, overview_bucket(rows, costs, &items, current)))
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
                        .map(|(account, items)| (account, overview_bucket(rows, costs, &items, current)))
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

fn add_overview_totals(totals: &mut Value, selected: &[&Row], costs: &[RowCost]) {
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

/// /usage 的桶：completed 行的累计量 + 账号计数（by_* 桶里没有 errors/cost_missing）。
/// 与 `overview_bucket` 同口径，只是从小时桶来，不再逐行。
#[derive(Default)]
struct Fold {
    done: Tokens,
    failed: Tokens,
    aborted: i64,
    accounts: BTreeMap<String, i64>,
    missing: BTreeMap<String, i64>,
}

fn model_key(key: &CellKey) -> String {
    if key.model.is_empty() { "?".to_string() } else { key.model.clone() }
}

impl Fold {
    fn add(&mut self, key: &CellKey, cell: &Cell) {
        self.done.add(&cell.done);
        self.failed.add(&cell.failed);
        self.aborted += cell.aborted;
        if cell.done.rows > 0 {
            let account = if key.account.is_empty() { "(unattributed)" } else { key.account.as_str() };
            *self.accounts.entry(account.to_string()).or_default() += cell.done.rows;
        }
        // 「没价」按模型记：Python 的 cost_missing 缺 model 字段时记 unknown。
        let missing = cell.done.missing + cell.failed.missing;
        if missing > 0 {
            let model = if key.model.is_empty() { "unknown" } else { key.model.as_str() };
            *self.missing.entry(model.to_string()).or_default() += missing;
        }
    }

    fn bucket(&self) -> Value {
        json!({
            "requests": self.done.rows,
            "accounts": self.accounts,
            "cost_cny": self.done.cost,
            "prompt_tokens": self.done.prompt,
            "completion_tokens": self.done.completion,
            "reasoning_tokens": self.done.reasoning,
            "cached_tokens": self.done.cached,
            "total_tokens": self.done.total,
            "credit": self.done.credit,
        })
    }
}

/// 顶层的总量：requests/tokens 只算 completed；失败行只进 errors 和 credit
/// （客户端中断连 credit 都不算，只数 errors）。
fn apply_fold(totals: &mut Value, fold: &Fold) {
    totals["requests"] = json!(fold.done.rows);
    totals["errors"] = json!(fold.failed.rows + fold.aborted);
    totals["prompt_tokens"] = json!(fold.done.prompt);
    totals["completion_tokens"] = json!(fold.done.completion);
    totals["reasoning_tokens"] = json!(fold.done.reasoning);
    totals["cached_tokens"] = json!(fold.done.cached);
    totals["total_tokens"] = json!(fold.done.total);
    totals["credit"] = json!(fold.done.credit + fold.failed.credit);
    totals["cost_cny"] = json!(fold.done.cost + fold.failed.cost);
    if !fold.missing.is_empty() {
        totals["cost_missing"] = json!(fold.missing);
    }
}

/// 窗口内的三张明细图 + 顶层总量。桶只为 completed 的行建，否则「只有失败」的
/// 模型会在面板上长出一张全 0 的卡片（Python 的折叠在非 completed 分支就 return）。
fn index_maps(
    index: &Index,
    since: Option<f64>,
    until: Option<f64>,
    realm: &str,
) -> (Fold, Value, Value, Value) {
    let mut total = Fold::default();
    let mut by_model: BTreeMap<String, Fold> = BTreeMap::new();
    let mut by_realm: BTreeMap<(String, String), Fold> = BTreeMap::new();
    let mut by_acct: BTreeMap<(String, String, String), Fold> = BTreeMap::new();
    for (key, cell) in index.window(since, until, realm) {
        total.add(key, cell);
        if cell.done.rows == 0 {
            continue;
        }
        let model = model_key(key);
        let account = if key.account.is_empty() { "(unattributed)" } else { key.account.as_str() };
        by_model.entry(model.clone()).or_default().add(key, cell);
        by_realm
            .entry((model.clone(), key.realm.clone()))
            .or_default()
            .add(key, cell);
        by_acct
            .entry((model, key.realm.clone(), account.to_string()))
            .or_default()
            .add(key, cell);
    }
    let model_map: Map<String, Value> = by_model.into_iter()
        .map(|(model, fold)| (model, fold.bucket()))
        .collect();
    let mut realms: BTreeMap<String, Map<String, Value>> = BTreeMap::new();
    for ((model, realm), fold) in by_realm {
        realms.entry(model).or_default().insert(realm, fold.bucket());
    }
    let mut accounts: BTreeMap<String, BTreeMap<String, Map<String, Value>>> = BTreeMap::new();
    for ((model, realm, account), fold) in by_acct {
        accounts
            .entry(model)
            .or_default()
            .entry(realm)
            .or_default()
            .insert(account, fold.bucket());
    }
    let realms = realms.into_iter()
        .map(|(model, inner)| (model, Value::Object(inner)))
        .collect::<Map<String, Value>>();
    let accounts = accounts.into_iter()
        .map(|(model, inner)| {
            let inner: Map<String, Value> = inner
                .into_iter()
                .map(|(realm, rows)| (realm, Value::Object(rows)))
                .collect();
            (model, Value::Object(inner))
        })
        .collect::<Map<String, Value>>();
    (total, Value::Object(model_map), Value::Object(realms), Value::Object(accounts))
}

/// `/usage`: all-time totals for the selected realm plus pool metadata.
pub fn overview(app: &Arc<App>, req: &Request<Incoming>) -> Resp {
    let realms = account_realms(app);
    let realm = requested_realm(req, app);
    let (since, until) = range_bounds(req);
    let mut totals = json!({
        "requests": 0, "errors": 0, "prompt_tokens": 0, "completion_tokens": 0,
        "reasoning_tokens": 0, "cached_tokens": 0, "total_tokens": 0,
        "credit": 0.0, "cost_cny": 0.0, "cost_missing": {},
        "started": crate::state::now_epoch() as f64,
        "by_model": {}, "by_model_realm": {}, "by_model_acct": {},
        "ttft_ms_sum": 0, "ttft_samples": 0, "gen_ms_sum": 0, "gen_samples": 0,
        "wall_ms_sum": 0, "wall_samples": 0,
    });
    let (by_model, by_model_realm, by_model_acct) = if custom_range(req) {
        let rows = read_rows(app);
        let current = app.state.current_realm();
        let selected: Vec<&Row> = rows_for_realm(&rows, &realm, &realms, &current)
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
        let settings = app.state.settings_snapshot();
        let costs = app.pricing.cost_rows(&selected, &settings);
        add_overview_totals(&mut totals, &selected, &costs);
        overview_maps(&selected, &costs, &realms, &current)
    } else {
        let index = usage_index(app);
        let (fold, by_model, by_model_realm, by_model_acct) =
            index_maps(&index, since, until, &realm);
        apply_fold(&mut totals, &fold);
        (by_model, by_model_realm, by_model_acct)
    };
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
    let mut grouped: BTreeMap<String, Vec<&Row>> = BTreeMap::new();
    for row in rows.iter().filter(|row| row.error.is_none()) {
        let account = row
            .account
            .as_deref()
            .filter(|value| !value.is_empty())
            .unwrap_or("(unattributed)");
        grouped.entry(account.to_string()).or_default().push(row);
    }
    let mut accounts: Vec<Value> = grouped
        .into_iter()
        .map(|(account, items)| {
            // Python 是「按次数降序、次数相同保持首次出现顺序」（sorted 是稳定排序），
            // 所以这里不能用 BTreeMap 收集——那会把并列项按 id 排掉。
            let mut models: Vec<(String, i64)> = Vec::new();
            for row in &items {
                let model = row.model.clone().unwrap_or_else(|| "?".to_string());
                match models.iter_mut().find(|(name, _)| *name == model) {
                    Some((_, count)) => *count += 1,
                    None => models.push((model, 1)),
                }
            }
            models.sort_by(|left, right| right.1.cmp(&left.1));
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
fn recent_row(row: &Row, app: &App, settings: &Map<String, Value>) -> Value {
    let mut out = app.pricing.enrich_usage_row(&row.to_value(), settings);
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
    let current = app.state.current_realm();
    let mut selected: Vec<&Row> = rows_for_realm(&rows, &realm, &realms, &current);
    // 日志不是严格按时间追加的（多进程/补写），反转文件顺序取不到最新那一页；
    // Python 是按 at 倒序取页的。并列保持文件顺序（稳定排序）。
    selected.sort_by(|left, right| {
        number(right, "at")
            .partial_cmp(&number(left, "at"))
            .unwrap_or(std::cmp::Ordering::Equal)
    });
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
