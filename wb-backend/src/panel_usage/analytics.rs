//! Usage analytics grouped by account, model, and API key.
//!
//! 口径全部对着 Python 的 `_feed_analytics` / `*_analytics_maps` 写：
//! client_aborted 整行跳过，其余行 requests/errors 按结果分，但 token/credit/成本
//! 照实累计（上游已经扣过的量不能因为失败就不算）；ttft/speed/elapsed 走真值判断。
//! 每个视图都有「全时段」和「窗口」两半，两半是同一趟折叠的两次输出。

use super::*;
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
    fn feed(&mut self, row: &Row, is_err: bool, cost: f64) {
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
            if let Some(value) = optional(row, key) {
                if value != 0.0 {
                    *sum += value;
                    *count += 1;
                }
            }
        }
    }

    /// 桶化的行：`done` 是成功行，`failed` 是其余非中断行。
    fn add_cell(&mut self, cell: &crate::usage_index::Cell) {
        self.requests += cell.done.rows;
        self.errors += cell.failed.rows;
        self.add_tokens(&cell.done);
        self.add_tokens(&cell.failed);
        self.ttft_sum += cell.ttft_sum;
        self.ttft_n += cell.ttft_n;
        self.speed_sum += cell.speed_sum;
        self.speed_n += cell.speed_n;
        self.elapsed_sum += cell.elapsed_sum;
        self.elapsed_n += cell.elapsed_n;
    }

    /// 只要成功行（账号/key 下的模型细分）。
    fn add_done(&mut self, cell: &crate::usage_index::Cell) {
        self.requests += cell.done.rows;
        self.add_tokens(&cell.done);
    }

    /// 只加数量：requests/errors 各自由调用方数（feed 里两者是同一行的二选一）。
    fn add_tokens(&mut self, tokens: &crate::usage_index::Tokens) {
        self.prompt += tokens.prompt;
        self.completion += tokens.completion;
        self.reasoning += tokens.reasoning;
        self.cached += tokens.cached;
        self.total += tokens.total;
        self.credit += tokens.credit;
        self.cost += tokens.cost;
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
    /// 首次出现顺序：Python 的排序是稳定排序，并列项按这个顺序出（不是按 id）。
    model_seq: BTreeMap<String, u64>,
    acct_seq: BTreeMap<String, u64>,
    /// 逐行路径的行序号（桶化路径直接用 cell.seq）。
    seen: u64,
}

fn model_of(row: &Row) -> String {
    row.model.as_deref().unwrap_or("(unknown)")
        .to_string()
}

fn key_id(row: &Row, known: &[String]) -> String {
    if row.key.is_none() {
        return "__before_keys__".into();
    }
    let id = row.key.as_deref().filter(|value| !value.is_empty()).unwrap_or("__no_key__");
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
        row: &Row,
        cost: f64,
        realms: &BTreeMap<String, String>,
        known_keys: &[String],
        current: &str,
    ) {
        if outcome(row) == "client_aborted" {
            return;
        }
        let is_err = outcome(row) != "completed";
        self.summary.feed(row, is_err, cost);
        let model = model_of(row);
        // 顶层的模型列表收所有行（含失败，python 的 tgt["models"] 就是这么折的）；
        // 账号/key 下的模型细分才只数成功请求。
        let seq = self.seen;
        self.seen += 1;
        self.model_seq.entry(model.clone()).or_insert(seq);
        self.models
            .entry(model.clone())
            .or_default()
            .feed(row, is_err, cost);

        let account = row.account.clone().unwrap_or_else(|| "(unattributed)".to_string());
        let realm = row_realm(row, realms, current);
        self.acct_seq.entry(account.clone()).or_insert(seq);
        let entry = self.accts.entry(account).or_default();
        if entry.realm.is_empty() {
            // python 记的是**行里的 realm 字段**（可能为空），池子里的账号随后按
            // 账号自己的 realm 覆盖；这里不能填推算出来的 realm。
            entry.realm = row.realm.clone().unwrap_or_default();
        }
        entry.stat.feed(row, is_err, cost);
        if !is_err {
            entry
                .models
                .entry(model.clone())
                .or_default()
                .feed(row, is_err, cost);
        }
        // realms 的键同样是行里的原值（空串也是键），cross_realm 靠它数「跨了几个出口」。
        let raw_realm = row.realm.clone().unwrap_or_default();
        *entry.realms.entry(raw_realm.clone()).or_default() += 1;
        entry.last_at = entry.last_at.max(number(row, "at"));

        let key = key_id(row, known_keys);
        let key_entry = self.keys.entry(key).or_default();
        key_entry.stat.feed(row, is_err, cost);
        if !is_err {
            key_entry.models.entry(model).or_default().feed(row, is_err, cost);
        }
        // key 的 realms 用**推算出来的** realm（python 的 k_realm = row_realm(row) or ""），
        // cross_realm 就是靠它数「这把 key 跨了几个出口」。
        *key_entry.realms.entry(realm).or_default() += 1;
        key_entry.last_at = key_entry.last_at.max(number(row, "at"));
    }

    /// 桶化的折叠：把「日 × 时 × 模型 × 账号 × key × realm」的累计量灌进这一半。
    /// 口径与 `feed` 逐条对齐，只是不再逐行。
    fn absorb(&mut self, key: &crate::usage_index::CellKey, cell: &crate::usage_index::Cell, known_keys: &[String]) {
        // 只有中断行的桶：feed 连账号/模型条目都不会建，这里同样跳过。
        if cell.done.rows == 0 && cell.failed.rows == 0 {
            return;
        }
        let model = if key.model.is_empty() { "(unknown)".to_string() } else { key.model.clone() };
        self.summary.add_cell(cell);
        self.model_seq.entry(model.clone()).or_insert(cell.seq);
        self.models.entry(model.clone()).or_default().add_cell(cell);

        let account = if key.account.is_empty() {
            "(unattributed)".to_string()
        } else {
            key.account.clone()
        };
        self.acct_seq.entry(account.clone()).or_insert(cell.seq);
        let entry = self.accts.entry(account.clone()).or_default();
        // 账号条目的 realm 取「本半里这个账号第一条带 realm 的行」（Python 的
        // `if not entry["realm"]` 只在空串时才补，所以按 seq 顺序喂进来才会一致）。
        if entry.realm.is_empty() {
            entry.realm = cell.first_raw_realm.clone();
        }
        entry.stat.add_cell(cell);
        if cell.done.rows > 0 {
            entry.models.entry(model.clone()).or_default().add_done(cell);
        }
        for (raw, count) in &cell.raw_realms {
            *entry.realms.entry(raw.clone()).or_default() += count;
        }
        entry.last_at = entry.last_at.max(cell.last_at);

        let key_id = if key.key == MISSING_KEY {
            "__before_keys__".to_string()
        } else if key.key.is_empty() {
            "__no_key__".to_string()
        } else if known_keys.iter().any(|entry| entry == &key.key) {
            key.key.clone()
        } else {
            "__unknown_key__".to_string()
        };
        let key_entry = self.keys.entry(key_id).or_default();
        key_entry.stat.add_cell(cell);
        if cell.done.rows > 0 {
            key_entry.models.entry(model).or_default().add_done(cell);
        }
        // key 的 realms 数的是行数，键是推算出来的 realm（`row_realm`）。
        *key_entry.realms.entry(key.realm.clone()).or_default() += cell.done.rows + cell.failed.rows;
        key_entry.last_at = key_entry.last_at.max(cell.last_at);
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
    // 账号按「窗口 tokens 降序、全时段 tokens 降序」排，并列按首次出现顺序
    //（Python 的 accts_list 是稳定排序）。
    let position = |row: &Value| {
        let uid = row.get("uid").and_then(Value::as_str).unwrap_or("");
        all.acct_seq.get(uid).copied().unwrap_or(u64::MAX)
    };
    let sort_key = |row: &Value| {
        (
            -integer(row.get("window").unwrap_or(&Value::Null), "total_tokens"),
            -integer(row.get("all_time").unwrap_or(&Value::Null), "total_tokens"),
            position(row),
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
        // 跨出口标记：合并后的 realms 才完整（配置里的 key 一开始是空的）。
        let multi = row
            .get("realms")
            .and_then(Value::as_object)
            .map(|realms| realms.keys().filter(|realm| !realm.is_empty()).count() > 1)
            .unwrap_or(false);
        row["cross_realm"] = json!(multi);
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

/// 按 seq（日志顺序）喂桶：账号条目的 realm 取「第一条带 realm 的行」，顺序错了就跟着错。
fn absorb_window(
    half: &mut Half,
    index: &crate::usage_index::Index,
    since: Option<f64>,
    until: Option<f64>,
    realm: &str,
    known_keys: &[String],
) {
    let mut cells: Vec<(&crate::usage_index::CellKey, &crate::usage_index::Cell)> =
        index.window(since, until, realm).collect();
    cells.sort_by_key(|(_, cell)| cell.seq);
    for (key, cell) in cells {
        half.absorb(key, cell, known_keys);
    }
}

/// `/usage/analytics`: 全时段 + 可选窗口。
pub fn analytics(app: &Arc<App>, req: &Request<Incoming>) -> Resp {
    let realm = query(req, "realm");
    let scope = realm.unwrap_or("all");
    // 面板发的是 range=today|week|month（区间按钮），只有自定义区间才带 since/until。
    let (range_since, range_until) = range_bounds(req);
    let explicit = query(req, "since").is_some() || query(req, "until").is_some();
    let since = query(req, "since")
        .and_then(|value| value.parse::<f64>().ok())
        .or(range_since);
    let until = query(req, "until")
        .and_then(|value| value.parse::<f64>().ok())
        .or(range_until);
    let settings = app.state.settings_snapshot();
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
    if explicit || custom_range(req) {
        // 任意 since/until：桶的粒度是整点，只能逐行。
        let all = read_rows(app);
        let realms = account_realms(app);
        let current = app.state.current_realm();
        let all_rows: Vec<&Row> = rows_for_realm(&all, scope, &realms, &current);
        let costs = app.pricing.cost_rows(&all_rows, &settings);
        for (index, row) in all_rows.iter().enumerate() {
            let cost = costs[index];
            let cost_value = if cost.known { cost.cny } else { 0.0 };
            all_half.feed(row, cost_value, &realms, &known_keys, &current);
            let at = number(row, "at");
            let in_window = since.map_or(true, |lower| at >= lower)
                && until.map_or(true, |upper| at <= upper);
            if in_window {
                window_half.feed(row, cost_value, &realms, &known_keys, &current);
            }
        }
    } else {
        let index = usage_index(app);
        absorb_window(&mut all_half, &index, None, None, scope, &known_keys);
        absorb_window(&mut window_half, &index, since, until, scope, &known_keys);
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
    let position = |row: &Value| {
        let model = row.get("model").and_then(Value::as_str).unwrap_or("");
        all_half.model_seq.get(model).copied().unwrap_or(u64::MAX)
    };
    let sort_key = |row: &Value| {
        (
            -integer(row.get("window").unwrap_or(&Value::Null), "total_tokens"),
            -integer(row.get("all_time").unwrap_or(&Value::Null), "total_tokens"),
            position(row),
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
