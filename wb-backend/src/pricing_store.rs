//! Durable pricing history and cached runtime reads.

use std::collections::{BTreeMap, HashMap};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, RwLock};

use serde_json::{json, Map, Value};

use crate::pricing_catalog::{build_snapshot, index_openrouter, resolve};
use crate::pricing_cost::{price_row_with_doc, price_row_with_policy};
use crate::pricing_engine::{as_f64, Defaults, USD_CNY};
use crate::pricing_io::{
    add_markup, add_source, append_jsonl, base_cost, file_key, now_epoch, now_iso, read_jsonl,
    FileKey,
};
use crate::pricing_policy::{current_assignment, load_policy_map, policy_record, RecordResult};

const BUNDLED_PRICING: &str = include_str!("../assets/pricing.json");
const BUNDLED_DEFAULTS: &str = include_str!("../assets/pricing-defaults.json");

#[derive(Clone)]
struct LiveIndex {
    models: Map<String, Value>,
    by_norm: BTreeMap<String, Vec<String>>,
}

pub struct PricingStore {
    data_dir: PathBuf,
    configured_file: Option<PathBuf>,
    bundled: Value,
    defaults: Defaults,
    pricing_cache: Mutex<Option<(FileKey, std::sync::Arc<Value>)>>,
    policies_cache: Mutex<Option<(FileKey, std::sync::Arc<HashMap<String, Value>>)>>,
    timeline_cache: Mutex<Option<(FileKey, std::sync::Arc<Vec<Value>>)>>,
    override_cache: Mutex<Option<(FileKey, std::sync::Arc<BTreeMap<String, String>>)>>,
    markup_cache: Mutex<Option<(FileKey, std::sync::Arc<BTreeMap<String, f64>>)>>,
    live_index: RwLock<Option<LiveIndex>>,
    write_lock: Mutex<()>,
}

/// 一行计价要读的全部输入（一次扫描只取一份）。
struct Inputs {
    policies: std::sync::Arc<HashMap<String, Value>>,
    timeline: std::sync::Arc<Vec<Value>>,
    doc: std::sync::Arc<Value>,
    overrides: std::sync::Arc<BTreeMap<String, String>>,
    markup: std::sync::Arc<BTreeMap<String, f64>>,
}

/// 批量计价的结果：面板聚合只关心这三个量。
#[derive(Clone, Copy)]
pub struct RowCost {
    pub cny: f64,
    pub known: bool,
    pub disabled: bool,
}

impl PricingStore {
    pub fn new(data_dir: PathBuf, configured_file: Option<PathBuf>) -> Self {
        let bundled = serde_json::from_str(BUNDLED_PRICING).unwrap_or_else(|_| json!({}));
        let defaults = Defaults::parse(BUNDLED_DEFAULTS).unwrap_or(Defaults {
            model_ids: Vec::new(),
            overrides: BTreeMap::new(),
            aliases: Default::default(),
            variant_suffixes: Vec::new(),
        });
        Self {
            data_dir,
            configured_file,
            bundled,
            defaults,
            pricing_cache: Mutex::new(None),
            policies_cache: Mutex::new(None),
            timeline_cache: Mutex::new(None),
            override_cache: Mutex::new(None),
            markup_cache: Mutex::new(None),
            live_index: RwLock::new(None),
            write_lock: Mutex::new(()),
        }
    }

    pub fn defaults(&self) -> &Defaults {
        &self.defaults
    }

    pub fn data_dir(&self) -> &Path {
        &self.data_dir
    }

    pub fn policies_path(&self) -> PathBuf {
        self.data_dir.join("pricing-policies.jsonl")
    }

    pub fn timeline_path(&self) -> PathBuf {
        self.data_dir.join("pricing-timeline.jsonl")
    }

    pub fn overrides_path(&self) -> PathBuf {
        self.data_dir.join("pricing-overrides.json")
    }

    pub fn markup_path(&self) -> PathBuf {
        self.data_dir.join("pricing-markup.json")
    }

    pub fn load_pricing(&self) -> std::sync::Arc<Value> {
        let path = self
            .configured_file
            .clone()
            .or_else(|| Some(PathBuf::from("pricing/pricing.json")));
        let Some(path) = path else {
            return std::sync::Arc::new(self.bundled.clone());
        };
        let key = file_key(&path);
        if let Some((cached_key, value)) = self.pricing_cache.lock().unwrap().as_ref() {
            if cached_key == &key {
                return value.clone();
            }
        }
        let value = if key.exists {
            fs::read_to_string(&path)
                .ok()
                .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
                .filter(|value| value.get("models").is_some_and(Value::is_object))
                .unwrap_or_else(|| self.bundled.clone())
        } else {
            self.bundled.clone()
        };
        let value = std::sync::Arc::new(value);
        *self.pricing_cache.lock().unwrap() = Some((key, value.clone()));
        value
    }

    pub fn usd_cny(&self) -> f64 {
        as_f64(self.load_pricing().pointer("/meta/usd_cny"))
            .filter(|value| *value != 0.0)
            .unwrap_or(1.0)
    }

    pub fn load_policies(&self) -> std::sync::Arc<HashMap<String, Value>> {
        let path = self.policies_path();
        let key = file_key(&path);
        if let Some((cached_key, value)) = self.policies_cache.lock().unwrap().as_ref() {
            if cached_key == &key {
                return value.clone();
            }
        }
        let rows = read_jsonl(&path);
        let value = load_policy_map(rows);
        let value = std::sync::Arc::new(value);
        *self.policies_cache.lock().unwrap() = Some((key, value.clone()));
        value
    }

    pub fn load_timeline(&self) -> std::sync::Arc<Vec<Value>> {
        let path = self.timeline_path();
        let key = file_key(&path);
        if let Some((cached_key, value)) = self.timeline_cache.lock().unwrap().as_ref() {
            if cached_key == &key {
                return value.clone();
            }
        }
        let mut rows: Vec<Value> = read_jsonl(&path)
            .into_iter()
            .filter(|row| row.get("policies").is_some_and(Value::is_object))
            .collect();
        rows.sort_by(|left, right| {
            as_f64(left.get("at"))
                .unwrap_or(0.0)
                .total_cmp(&as_f64(right.get("at")).unwrap_or(0.0))
        });
        let rows = std::sync::Arc::new(rows);
        *self.timeline_cache.lock().unwrap() = Some((key, rows.clone()));
        rows
    }

    pub fn current_assignment(&self) -> (Option<f64>, BTreeMap<String, String>) {
        current_assignment(&self.load_timeline())
    }

    /// 原样返回一份可写的副本（只有写路径要改它，读路径走 effective_overrides）。
    pub fn runtime_overrides(&self) -> BTreeMap<String, String> {
        let path = self.overrides_path();
        let key = file_key(&path);
        if let Some((cached_key, value)) = self.override_cache.lock().unwrap().as_ref() {
            if cached_key == &key {
                return (**value).clone();
            }
        }
        let mut value = BTreeMap::new();
        if key.exists {
            if let Some(Value::Object(map)) = fs::read_to_string(&path)
                .ok()
                .and_then(|raw| serde_json::from_str(&raw).ok())
            {
                for (model, reference) in map {
                    if let Some(reference) =
                        reference.as_str().map(str::trim).filter(|s| !s.is_empty())
                    {
                        let model = model.trim();
                        if !model.is_empty() {
                            value.insert(model.to_string(), reference.to_string());
                        }
                    }
                }
            }
        }
        let value = std::sync::Arc::new(value);
        *self.override_cache.lock().unwrap() = Some((key, value.clone()));
        (*value).clone()
    }

    pub fn effective_overrides(&self) -> std::sync::Arc<BTreeMap<String, String>> {
        let mut value = self.defaults.overrides.clone();
        value.extend(self.runtime_overrides());
        std::sync::Arc::new(value)
    }

    pub fn save_runtime_override(&self, model: &str, reference: &str) -> Result<(), String> {
        let _write = self
            .write_lock
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let mut values = self.runtime_overrides();
        if reference.is_empty() {
            values.remove(model);
        } else {
            values.insert(model.to_string(), reference.to_string());
        }
        let path = self.overrides_path();
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).map_err(|error| error.to_string())?;
        }
        crate::state::write_json_atomic(&path, &serde_json::to_value(&values).unwrap_or_default())
            .map_err(|error| error.to_string())?;
        *self.override_cache.lock().unwrap() = Some((file_key(&path), std::sync::Arc::new(values)));
        Ok(())
    }

    pub fn markup_for(&self, model: &str) -> f64 {
        self.markup_map().get(model).copied().unwrap_or(1.0)
    }

    fn markup_map(&self) -> std::sync::Arc<BTreeMap<String, f64>> {
        let path = self.markup_path();
        let key = file_key(&path);
        if let Some((cached_key, value)) = self.markup_cache.lock().unwrap().as_ref() {
            if cached_key == &key {
                return value.clone();
            }
        }
        let mut value = BTreeMap::new();
        if key.exists {
            if let Some(Value::Object(map)) = fs::read_to_string(&path)
                .ok()
                .and_then(|raw| serde_json::from_str(&raw).ok())
            {
                for (model, factor) in map {
                    if let Some(factor) = as_f64(Some(&factor)).filter(|value| *value > 0.0) {
                        value.insert(model.trim().to_string(), factor);
                    }
                }
            }
        }
        let value = std::sync::Arc::new(value);
        *self.markup_cache.lock().unwrap() = Some((key, value.clone()));
        value
    }

    pub fn current_policy_id(&self, model: &str, enabled: bool, variants: bool) -> Option<String> {
        if !enabled || model.trim().is_empty() {
            return None;
        }
        let (_at, assignment) = self.current_assignment();
        if let Some(id) = assignment.get(model) {
            return Some(id.clone());
        }
        self.ensure_policy(model, variants)
    }

    pub fn ensure_policy(&self, model: &str, variants: bool) -> Option<String> {
        let live = self.live_index.read().ok()?.clone()?;
        let overrides = self.effective_overrides();
        let (reference, overridden, inherited) = resolve(
            model,
            &live.models,
            &live.by_norm,
            &overrides,
            &self.defaults,
            variants,
        );
        let reference = reference?;
        let (doc, _unpriced, _overridden) = build_snapshot(
            &live.models,
            &[model.to_string()],
            &overrides,
            &self.defaults,
            variants,
            &now_iso(),
            "",
        );
        let mut entry = doc.get("models")?.get(model)?.clone();
        if inherited.is_none() && !overridden {
            if let Value::Object(map) = &mut entry {
                map.remove("via");
                map.remove("override_from");
            }
        }
        if entry.get("or_id").and_then(Value::as_str) != Some(reference.as_str()) {
            return None;
        }
        let single = json!({
            "meta": {"base_currency": "CNY", "usd_cny": USD_CNY},
            "models": {model: entry},
        });
        self.record_policies(&single, Some(now_epoch()), true)
            .assignment
            .get(model)
            .cloned()
    }

    pub fn remember_live_index(&self, models: Map<String, Value>) {
        if models.is_empty() {
            return;
        }
        let by_norm = index_openrouter(&models);
        if let Ok(mut slot) = self.live_index.write() {
            *slot = Some(LiveIndex { models, by_norm });
        }
    }

    pub fn live_models(&self) -> Option<Map<String, Value>> {
        self.live_index
            .read()
            .ok()?
            .as_ref()
            .map(|live| live.models.clone())
    }

    pub fn record_policies(&self, doc: &Value, at: Option<f64>, merge: bool) -> RecordResult {
        let _write = self
            .write_lock
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let stamp = at.unwrap_or_else(now_epoch);
        let usd = as_f64(doc.pointer("/meta/usd_cny")).unwrap_or(USD_CNY);
        let models = doc
            .get("models")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        let mut known = (*self.load_policies()).clone();
        let (_previous_at, previous) = self.current_assignment();
        let mut assignment = if merge {
            previous.clone()
        } else {
            BTreeMap::new()
        };
        let mut fresh = Vec::new();
        for (model, entry) in &models {
            let record = policy_record(model, entry, usd, stamp);
            let id = record
                .get("id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            assignment.insert(model.clone(), id.clone());
            if !known.contains_key(&id) {
                known.insert(id, record.clone());
                fresh.push(record);
            }
        }
        if !fresh.is_empty() {
            append_jsonl(&self.policies_path(), &fresh);
            *self.policies_cache.lock().unwrap() = None;
        }
        let changed = !assignment.is_empty() && assignment != previous;
        if changed {
            let timeline = json!({"at": stamp, "policies": assignment});
            append_jsonl(&self.timeline_path(), &[timeline]);
            *self.timeline_cache.lock().unwrap() = None;
        }
        RecordResult {
            added: fresh.len(),
            changed,
            assignment,
        }
    }

    pub fn cost_for_row(&self, row: &Value, enabled: bool, details: bool) -> Value {
        if row.is_null() {
            return base_cost(Value::Null, false, false);
        }
        if !enabled {
            return base_cost(Value::Null, true, details);
        }
        self.cost_from(row, &self.inputs(), details)
    }

    /// 一次扫描里每行都要计价时用这个：策略/时间线/价表/覆盖/加价各只读一次。
    /// 逐行调 cost_for_row 会把这些表克隆 16k 次，面板一刷新就把内存顶上去
    /// （Python 也是整扫复用同一份 _pricing_inputs）。
    /// 整包计价：各表只读一次（逐行调 cost_for_row 会把它们克隆上万次）。
    /// 计价链是 Value 形状的，这里给每行拼一个只有计价字段的小对象；成本只取
    /// cny/known 两个数，不拼明细——否则 2.7 万行要 400 ms，面板切区间一眼就卡。
    pub fn cost_rows(&self, rows: &[&crate::panel_usage::Row], enabled: bool) -> Vec<RowCost> {
        if !enabled {
            return vec![RowCost { cny: 0.0, known: false, disabled: true }; rows.len()];
        }
        let inputs = self.inputs();
        // 政策条目按 id 只拼一次（同一套政策的 2 万行不必各拼一份）。
        let mut entries: HashMap<String, (Value, f64)> = HashMap::new();
        let history = crate::pricing_policy::PolicyHistory::new(&inputs.timeline);
        rows.iter()
            .map(|row| {
                let Some(model) = row.model.as_deref() else {
                    return RowCost { cny: 0.0, known: false, disabled: false };
                };
                let mut view = Map::new();
                view.insert("model".into(), json!(model));
                view.insert("at".into(), json!(row.at));
                view.insert("prompt_tokens".into(), json!(row.prompt_tokens));
                view.insert("cached_tokens".into(), json!(row.cached_tokens));
                view.insert("completion_tokens".into(), json!(row.completion_tokens));
                if let Some(policy) = &row.cost_policy {
                    view.insert("cost_policy".into(), policy.clone());
                }
                let view = Value::Object(view);
                let markup = inputs.markup.get(model).copied().unwrap_or(1.0);
                let mut policy_id = row
                    .cost_policy
                    .as_ref()
                    .and_then(Value::as_str)
                    .map(str::to_string);
                if policy_id.is_none() {
                    (policy_id, _) = history.resolve(row.at, model);
                }
                let cost = match policy_id.as_deref().filter(|id| inputs.policies.contains_key(*id)) {
                    Some(id) => {
                        let (entry, usd_cny) = entries.entry(id.to_string()).or_insert_with(|| {
                            let policy = &inputs.policies[id];
                            (
                                crate::pricing_policy::policy_entry(policy),
                                as_f64(policy.get("usd_cny"))
                                    .unwrap_or(crate::pricing_engine::USD_CNY),
                            )
                        });
                        crate::pricing_engine::compute_entry(entry, *usd_cny, &view, markup)
                    }
                    None => {
                        let doc = &inputs.doc;
                        let entry = doc.pointer("/models").and_then(|models| models.get(model));
                        let usd_cny = as_f64(doc.pointer("/meta/usd_cny"))
                            .filter(|value| *value != 0.0)
                            .unwrap_or(1.0);
                        match entry {
                            Some(entry) => crate::pricing_engine::compute_entry(entry, usd_cny, &view, markup),
                            None => return RowCost { cny: 0.0, known: false, disabled: false },
                        }
                    }
                };
                RowCost { cny: cost.cny, known: cost.known, disabled: false }
            })
            .collect()
    }

    fn inputs(&self) -> Inputs {
        Inputs {
            policies: self.load_policies(),
            timeline: self.load_timeline(),
            doc: self.load_pricing(),
            overrides: self.effective_overrides(),
            markup: self.markup_map(),
        }
    }

    fn cost_from(&self, row: &Value, inputs: &Inputs, details: bool) -> Value {
        let Some(model) = row.get("model").and_then(Value::as_str) else {
            return base_cost(Value::Null, false, details);
        };
        let markup = inputs.markup.get(model).copied().unwrap_or(1.0);
        let overrides = &inputs.overrides;
        let policies = &inputs.policies;
        let timeline = &inputs.timeline;
        let mut backfilled = false;
        let mut policy_id = row
            .get("cost_policy")
            .and_then(Value::as_str)
            .map(str::to_string);
        if policy_id.is_none() {
            (policy_id, backfilled) = crate::pricing_policy::policy_ref_at(
                &timeline,
                as_f64(row.get("at")).unwrap_or(0.0),
                model,
            );
        }
        if let Some(policy) = policy_id.as_deref().and_then(|id| policies.get(id)) {
            let mut cost = price_row_with_policy(row, policy, model, overrides, markup, details);
            add_source(
                &mut cost,
                policy_id.as_deref().unwrap_or(""),
                policy.get("first_seen"),
                backfilled,
            );
            add_markup(&mut cost, markup, details);
            return cost;
        }
        let mut cost = price_row_with_doc(row, &inputs.doc, model, overrides, markup, details);
        add_source(&mut cost, "builtin", None, false);
        add_markup(&mut cost, markup, details);
        cost
    }
}
