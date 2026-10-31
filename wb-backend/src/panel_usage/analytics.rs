//! Usage analytics grouped by account, model, and API key.
//!
//! 口径全部对着 Python 的 `_feed_analytics` / `*_analytics_maps` 写：
//! client_aborted 整行跳过，其余行 requests/errors 按结果分，但 token/credit/成本
//! 照实累计（上游已经扣过的量不能因为失败就不算）；ttft/speed/elapsed 走真值判断。
//! 每个视图都有「全时段」和「窗口」两半，两半是同一趟折叠的两次输出。

use super::*;
use crate::pricing_store::RowCost;
use serde_json::Map;

#[derive(Default, Clone)]
struct Stat {
    requests: i64,
    errors: i64,
    prompt: i64,
    completion: i64,
    reasoning: i64,
    cached: i64,
    total: i64,
    credit: f64,
    cost: f64,
    ttft_sum: f64,
    ttft_n: i64,
    speed_sum: f64,
    speed_n: i64,
    elapsed_sum: f64,
    elapsed_n: i64,
}

impl Stat {
    fn feed(&mut self, row: &Value, is_err: bool, cost: f64) {
        if is_err {
            self.errors += 1;
        } else {
            self.requests += 1;
        }
        self.prompt += integer(row, "prompt_tokens");
        self.completion += integer(row, "completion_tokens");
        self.reasoning += integer(row, "reasoning_tokens");
        self.cached += integer(row, "cached_tokens");
        self.total += integer(row, "total_tokens");
        self.credit += number(row, "credit");
        self.cost += cost;
        // 真值判断：0 与缺字段都不进样本（Python 的 `if vals[6]:`）。
        for (key, sum, count) in [
            ("ttft_ms", &mut self.ttft_sum, &mut self.ttft_n),
            ("tokens_per_sec", &mut self.speed_sum, &mut self.speed_n),
            ("elapsed_ms", &mut self.elapsed_sum, &mut self.elapsed_n),
        ] {
            if let Some(value) = row.get(key).and_then(Value::as_f64) {
                if value != 0.0 {
                    *sum += value;
                    *count += 1;
                }
            }
        }
    }

    fn json(&self) -> Value {
        let cache_hit = if self.prompt > 0 {
            round_one(self.cached as f64 * 100.0 / self.prompt as f64)
        } else {
            0.0
        };
        let reasoning_ratio = if self.completion > 0 {
            round_one(self.reasoning as f64 * 100.0 / self.completion as f64)
        } else {
            0.0
        };
        json!({
            "requests": self.requests,
            "errors": self.errors,
            "prompt_tokens": self.prompt,
            "completion_tokens": self.completion,
            "reasoning_tokens": self.reasoning,
            "cached_tokens": self.cached,
            "total_tokens": self.total,
            "credit": self.credit,
            "cost_cny": self.cost,
            "ttft_sum": self.ttft_sum,
            "ttft_n": self.ttft_n,
            "speed_sum": self.speed_sum,
            "speed_n": self.speed_n,
            "elapsed_sum": self.elapsed_sum,
            "elapsed_n": self.elapsed_n,
            "cache_hit_pct": cache_hit,
            "reasoning_ratio": reasoning_ratio,
            "ttft_ms_avg": if self.ttft_n > 0 {
                json!((self.ttft_sum / self.ttft_n as f64).round_ties_even() as i64)
            } else {
                json!(0)
            },
            "speed_avg": if self.speed_n > 0 {
                json!(round_one(self.speed_sum / self.speed_n as f64))
            } else {
                json!(0.0)
            },
            "elapsed_ms_avg": if self.elapsed_n > 0 {
                json!((self.elapsed_sum / self.elapsed_n as f64).round_ties_even() as i64)
            } else {
                json!(0)
            },
        })
    }
}

fn round_one(value: f64) -> f64 {
    (value * 10.0).round_ties_even() / 10.0
}

/// 一个账号 / 一把 key 在某一半里的条目：自己的统计 + 按模型的细分。
#[derive(Default, Clone)]
struct Entry {
    stat: Stat,
    models: BTreeMap<String, Stat>,
    realms: BTreeMap<String, i64>,
    last_at: f64,
    realm: String,
}

#[derive(Default)]
struct Half {
    summary: Stat,
    accts: BTreeMap<String, Entry>,
    models: BTreeMap<String, Stat>,
    keys: BTreeMap<String, Entry>,
}

fn model_of(row: &Value) -> String {
    row.get("model")
        .and_then(Value::as_str)
        .unwrap_or("(unknown)")
        .to_string()
}

fn key_id(row: &Value, known: &[String]) -> String {
    if row.get("key").is_none() {
        return "__before_keys__".into();
    }
    let id = row
        .get("key")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .unwrap_or("__no_key__");
    // settings 里查不到的 key 归到 __unknown_key__ 桶（Python 是这么归的），
    // 否则同一个「日志里有、配置里没有」的 id 会在面板上冒充一个真实 key。
    if id != "__no_key__" && !known.iter().any(|entry| entry == id) {
        return "__unknown_key__".into();
    }
    id.to_string()
}

impl Half {
    fn feed(
        &mut self,
        row: &Value,
        cost: f64,
        realms: &BTreeMap<String, String>,
        known_keys: &[String],
    ) {
        if outcome(row) == "client_aborted" {
            return;
        }
        let is_err = outcome(row) != "completed";
        self.summary.feed(row, is_err, cost);
        let model = model_of(row);
        self.models
            .entry(model.clone())
            .or_default()
            .feed(row, is_err, cost);

        let account = row
            .get("account")
            .and_then(Value::as_str)
            .unwrap_or("(unattributed)")
            .to_string();
        let realm = row_realm(row, realms).to_string();
        let entry = self.accts.entry(account).or_default();
        if entry.realm.is_empty() {
            entry.realm = realm.clone();
        }
        entry.stat.feed(row, is_err, cost);
        entry
            .models
            .entry(model.clone())
            .or_default()
            .feed(row, is_err, cost);
        *entry.realms.entry(realm.clone()).or_default() += 1;
        entry.last_at = entry.last_at.max(number(row, "at"));

        let key = key_id(row, known_keys);
        let key_entry = self.keys.entry(key).or_default();
        key_entry.stat.feed(row, is_err, cost);
        key_entry.models.entry(model).or_default().feed(row, is_err, cost);
        *key_entry.realms.entry(realm).or_default() += 1;
        key_entry.last_at = key_entry.last_at.max(number(row, "at"));
    }
}

/// 每个模型一行的细分。Python 的模型细分只数 completed（失败行进账号/汇总的
/// errors，不进模型行），账号视图带 cost_cny、key 视图不带。
fn models_json(models: &BTreeMap<String, Stat>, with_cost: bool) -> Value {
    let map: Map<String, Value> = models
        .iter()
        .map(|(model, stat)| {
            let mut row = json!({
                "requests": stat.requests,
                "tokens": stat.total,
                "reasoning": stat.reasoning,
            });
            if with_cost {
                row["cost_cny"] = json!(stat.cost);
            }
            (model.clone(), row)
        })
        .collect();
    Value::Object(map)
}

/// 前 5 个模型 + 一个「其他」尾巴（Python 的 _top_models：按 tokens、requests、id 排）。
fn top_models(models: &BTreeMap<String, Stat>) -> (Vec<Value>, Value) {
    let mut rows: Vec<(&String, &Stat)> = models.iter().collect();
    rows.sort_by(|left, right| {
        right
            .1
            .total
            .cmp(&left.1.total)
            .then_with(|| right.1.requests.cmp(&left.1.requests))
            .then_with(|| left.0.cmp(right.0))
    });
    let head = rows
        .iter()
        .take(5)
        .map(|(model, stat)| {
            json!({
                "model": model,
                "requests": stat.requests,
                "tokens": stat.total,
                "reasoning": stat.reasoning,
            })
        })
        .collect();
    let rest = &rows[rows.len().min(5)..];
    let other = if rest.is_empty() {
        Value::Null
    } else {
        json!({
            "model": "(其他)",
            "other": true,
            "models": rest.len(),
            "requests": rest.iter().map(|(_, stat)| stat.requests).sum::<i64>(),
            "tokens": rest.iter().map(|(_, stat)| stat.total).sum::<i64>(),
            "reasoning": rest.iter().map(|(_, stat)| stat.reasoning).sum::<i64>(),
        })
    };
    (head, other)
}

fn key_label(settings: &Map<String, Value>, key: &str) -> String {
    match key {
        "__before_keys__" => return "(切换前)".into(),
        "__no_key__" => return "(无 key)".into(),
        _ => {}
    }
    if let Some(Value::Array(keys)) = settings.get("api_keys") {
        for entry in keys {
            if entry.get("id").and_then(Value::as_str) == Some(key) {
                let name = entry.get("name").and_then(Value::as_str).unwrap_or("").trim();
                return if name.is_empty() { key.to_string() } else { name.to_string() };
            }
        }
    }
    "(未知 key)".into()
}

fn accounts_json(
    app: &App,
    all: &Half,
    window: &Half,
    settings: &Map<String, Value>,
    realm: Option<&str>,
) -> Vec<Value> {
    let accounts = app.state.accounts_snapshot();
    let mut ids: Vec<String> = accounts
        .iter()
        .filter(|account| realm.is_none() || realm == Some(account.realm().as_str()))
        .map(|account| account.uid())
        .collect();
    for uid in all.accts.keys() {
        if !ids.contains(uid) {
            ids.push(uid.clone());
        }
    }
    let _ = settings;
    let mut list: Vec<Value> = ids.into_iter()
        .map(|uid| {
            let account = accounts.iter().find(|account| account.uid() == uid);
            let empty = Entry::default();
            let all_entry = all.accts.get(&uid).unwrap_or(&empty);
            let win_entry = window.accts.get(&uid).unwrap_or(&empty);
            let mut out = json!({
                "uid": uid,
                // 账号池里没有的 uid，Python 直接拿整串 uid 当昵称（不截断）。
                "nickname": account
                    .map(|value| value.str("nickname").to_string())
                    .filter(|value| !value.is_empty())
                    .unwrap_or_else(|| uid.clone()),
                "realm": account
                    .map(|value| value.realm())
                    .filter(|value| !value.is_empty())
                    .unwrap_or_else(|| all_entry.realm.clone()),
                "domain": account.map(|value| value.domain()).unwrap_or_default(),
                "window": win_entry.stat.json(),
                "all_time": all_entry.stat.json(),
                "window_models": models_json(&win_entry.models, true),
                "all_models": models_json(&all_entry.models, true),
            });
            // Python 只有账号文件里真有 credits 才输出这个键。
            if let Some(credits) = account.and_then(|value| value.data.get("credits")) {
                out["credits"] = credits.clone();
            }
            out
        })
        .collect();
    // 账号按「窗口 tokens 降序、全时段 tokens 降序」排（Python 的 accts_list）。
    let sort_key = |row: &Value| {
        (
            -integer(row.get("window").unwrap_or(&Value::Null), "total_tokens"),
            -integer(row.get("all_time").unwrap_or(&Value::Null), "total_tokens"),
        )
    };
    list.sort_by(|left, right| sort_key(left).cmp(&sort_key(right)));
    list
}

fn keys_json(all: &Half, window: &Half, settings: &Map<String, Value>) -> Vec<Value> {
    // 配置里的 key 先各占一行（哪怕这段时间没流量），日志里见到的 key 再并进来：
    // 配置里没有的 id 一律并进 __unknown_key__（Python 的 _build_key_rows）。
    let mut rows: Vec<Value> = Vec::new();
    let mut declared: Vec<String> = Vec::new();
    if let Some(Value::Array(keys)) = settings.get("api_keys") {
        for entry in keys {
            let id = entry.get("id").and_then(Value::as_str).unwrap_or("");
            if id.is_empty() {
                continue;
            }
            declared.push(id.to_string());
            let name = entry
                .get("name")
                .and_then(Value::as_str)
                .filter(|value| !value.trim().is_empty())
                .unwrap_or(id);
            rows.push(json!({
                "key": id,
                "name": name,
                "realm": entry.get("realm").and_then(Value::as_str).unwrap_or(""),
                "enabled": entry.get("enabled").and_then(Value::as_bool).unwrap_or(true),
                "source": "panel",
                "window": Stat::default().json(),
                "all_time": Stat::default().json(),
                "window_models": Value::Object(Map::new()),
                "all_models": Value::Object(Map::new()),
                "realms": Value::Object(Map::new()),
                "last_at": 0.0,
                "models": Vec::<Value>::new(),
                "models_other": Value::Null,
                "cross_realm": false,
            }));
        }
    }
    let index_of = |rows: &[Value], key: &str| {
        rows.iter()
            .position(|row| row.get("key").and_then(Value::as_str) == Some(key))
    };
    for (key, all_entry) in all.keys.iter() {
        let empty = Entry::default();
        let win_entry = window.keys.get(key).unwrap_or(&empty);
        let (models, models_other) = top_models(&win_entry.models);
        let target = if key == "__before_keys__" || key == "__no_key__" {
            index_of(&rows, key)
        } else if declared.iter().any(|entry| entry == key) {
            index_of(&rows, key)
        } else {
            index_of(&rows, "__unknown_key__")
        };
        match target {
            Some(index) => {
                rows[index]["window"] = win_entry.stat.json();
                rows[index]["all_time"] = all_entry.stat.json();
                // __unknown_key__ 是多个日志 key 合并出来的，Python 合并时只留
                // requests/tokens/reasoning，成本会被丢掉；其余 key 带 cost_cny。
                let with_cost = key != "__unknown_key__";
                rows[index]["window_models"] = models_json(&win_entry.models, with_cost);
                rows[index]["all_models"] = models_json(&all_entry.models, with_cost);
                rows[index]["realms"] = json!(all_entry.realms);
                rows[index]["last_at"] = json!(all_entry.last_at);
                if key == "__unknown_key__" || !declared.iter().any(|entry| entry == key) {
                    rows[index]["models"] = json!(models);
                    rows[index]["models_other"] = models_other;
                }
            }
            None => {
                let bucket_key = if key == "__before_keys__" || key == "__no_key__" {
                    key.clone()
                } else {
                    "__unknown_key__".to_string()
                };
                rows.push(json!({
                    "key": bucket_key,
                    "name": key_label(settings, &bucket_key),
                    "realm": "",
                    "enabled": false,
                    "source": "bucket",
                    "window": win_entry.stat.json(),
                    "all_time": all_entry.stat.json(),
                    "window_models": models_json(&win_entry.models, bucket_key != "__unknown_key__"),
                    "all_models": models_json(&all_entry.models, bucket_key != "__unknown_key__"),
                    "realms": json!(all_entry.realms),
                    "last_at": json!(all_entry.last_at),
                    "models": json!(models),
                    "models_other": models_other,
                    "cross_realm": all_entry.realms.keys().filter(|realm| !realm.is_empty()).count() > 1,
                }));
            }
        }
    }
    // 配置里 key 的 top-models 用窗口那一半算（Python 也是从 window_models 取的）。
    for row in rows.iter_mut() {
        if row.get("source").and_then(Value::as_str) == Some("panel") {
            let window_models = row.get("window_models").cloned().unwrap_or_else(|| json!({}));
            let mut stats: BTreeMap<String, Stat> = BTreeMap::new();
            if let Value::Object(map) = window_models {
                for (model, value) in map {
                    let mut stat = Stat::default();
                    stat.requests = integer(&value, "requests");
                    stat.total = integer(&value, "tokens");
                    stat.reasoning = integer(&value, "reasoning");
                    stats.insert(model, stat);
                }
            }
            let (models, models_other) = top_models(&stats);
            row["models"] = json!(models);
            row["models_other"] = models_other;
        }
        let idle_disabled = row.get("source").and_then(Value::as_str) == Some("panel")
            && row.get("enabled").and_then(Value::as_bool) == Some(false)
            && integer(row.get("window").unwrap_or(&Value::Null), "requests") == 0
            && integer(row.get("window").unwrap_or(&Value::Null), "errors") == 0;
        row["_drop"] = json!(idle_disabled);
    }
    rows.retain(|row| row.get("_drop").and_then(Value::as_bool) != Some(true));
    for row in rows.iter_mut() {
        if let Some(object) = row.as_object_mut() {
            object.remove("_drop");
        }
    }
    // 排序键：窗口 tokens 降序、全时段 tokens 降序、name（Python 的 _build_key_rows）。
    let sort_key = |row: &Value| {
        (
            -integer(row.get("window").unwrap_or(&Value::Null), "total_tokens"),
            -integer(row.get("all_time").unwrap_or(&Value::Null), "total_tokens"),
            row.get("name").and_then(Value::as_str).unwrap_or("").to_string(),
        )
    };
    rows.sort_by(|left, right| sort_key(left).cmp(&sort_key(right)));
    rows
}

/// `/usage/analytics`: 全时段 + 可选窗口。
pub fn analytics(app: &Arc<App>, req: &Request<Incoming>) -> Resp {
    let all = read_rows(app);
    let realms = account_realms(app);
    let realm = query(req, "realm");
    let all_rows: Vec<&Value> = rows_for_realm(&all, realm.unwrap_or("all"), &realms);
    // 面板发的是 range=today|week|month（区间按钮），只有自定义区间才带 since/until。
    let (range_since, range_until) = range_bounds(req);
    let since = query(req, "since")
        .and_then(|value| value.parse::<f64>().ok())
        .or(range_since);
    let until = query(req, "until")
        .and_then(|value| value.parse::<f64>().ok())
        .or(range_until);
    let settings = app.state.settings_snapshot();
    let costs = app.pricing.cost_rows(&all_rows, &settings);

    let known_keys: Vec<String> = match settings.get("api_keys") {
        Some(Value::Array(keys)) => keys
            .iter()
            .filter_map(|entry| entry.get("id").and_then(Value::as_str))
            .map(str::to_string)
            .collect(),
        _ => Vec::new(),
    };
    let mut all_half = Half::default();
    let mut window_half = Half::default();
    for (index, row) in all_rows.iter().enumerate() {
        let cost = costs[index];
        let cost_value = if cost.known { cost.cny } else { 0.0 };
        all_half.feed(row, cost_value, &realms, &known_keys);
        let at = number(row, "at");
        let in_window = since.map_or(true, |lower| at >= lower)
            && until.map_or(true, |upper| at <= upper);
        if in_window {
            window_half.feed(row, cost_value, &realms, &known_keys);
        }
    }

    let mut models: Vec<Value> = all_half
        .models
        .iter()
        .map(|(model, all_stat)| {
            let window_stat = window_half.models.get(model);
            json!({
                "model": model,
                "window": window_stat.map(Stat::json).unwrap_or_else(|| Stat::default().json()),
                "all_time": all_stat.json(),
            })
        })
        .collect();
    let sort_key = |row: &Value| {
        (
            -integer(row.get("window").unwrap_or(&Value::Null), "total_tokens"),
            -integer(row.get("all_time").unwrap_or(&Value::Null), "total_tokens"),
        )
    };
    models.sort_by(|left, right| sort_key(left).cmp(&sort_key(right)));

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
                "window": window_half.summary.json(),
                "all_time": all_half.summary.json(),
            },
            "accounts": accounts_json(app, &all_half, &window_half, &settings, realm),
            "models": models,
            "keys": keys_json(&all_half, &window_half, &settings),
        }),
    )
}
