//! Runtime facade for pricing status, manual refresh, and record stamping.
//!
//! Python's background `PriceRefresher` is intentionally not a resident thread
//! here: the parent backend can schedule [`Pricing::run_refresh`] at its own
//! cadence. The panel's explicit refresh path is available now.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Map, Value};

use crate::pricing_catalog::{build_snapshot, index_openrouter, resolve};
use crate::pricing_engine::as_f64;
use crate::pricing_io::{local_label, now_epoch};
use crate::pricing_panel::fetch_openrouter;
use crate::pricing_store::PricingStore;
use crate::App;

#[derive(Clone)]
pub struct Config {
    pub url: String,
    pub timeout: Duration,
    pub max_models: usize,
    pub max_response_bytes: usize,
}

#[derive(Default)]
struct Runtime {
    running: bool,
    last_run: Option<f64>,
    last_error: Option<String>,
    last_models: usize,
    last_extra: usize,
    logs: Vec<String>,
}

#[allow(unused_imports)] // The parent wires this handler into panel::handle.
pub use crate::pricing_panel::handle;
pub use crate::pricing_settings::{
    apply_setting, enrich_settings, normalize_refresh_setting, Settings,
};

pub struct Pricing {
    pub store: PricingStore,
    pub(super) config: Config,
    runtime: Mutex<Runtime>,
}

impl Pricing {
    pub fn new(usage_dir: &Path) -> Self {
        // Examples: WB_PRICING_FILE=pricing/pricing.json,
        // WB_PRICING_DIR=usage, WB_PRICING_URL=https://openrouter.ai/api/v1/models.
        let configured = std::env::var_os("WB_PRICING_FILE").map(PathBuf::from);
        let data_dir = std::env::var_os("WB_PRICING_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| usage_dir.to_path_buf());
        let store = PricingStore::new(data_dir, configured);
        let url = std::env::var("WB_PRICING_URL").unwrap_or_else(|_| {
            store
                .load_pricing()
                .pointer("/meta/sources/openrouter")
                .and_then(Value::as_str)
                .unwrap_or("https://openrouter.ai/api/v1/models")
                .to_string()
        });
        let timeout = env_usize("WB_PRICING_TIMEOUT_SECS", 45).max(1) as u64;
        let max_models = env_usize("WB_PRICING_MAX_MODELS", 10_000).max(1);
        let max_response_bytes = env_usize("WB_PRICING_MAX_BYTES", 8 * 1024 * 1024).max(1024);
        Self {
            store,
            config: Config {
                url,
                timeout: Duration::from_secs(timeout),
                max_models,
                max_response_bytes,
            },
            runtime: Mutex::new(Runtime::default()),
        }
    }

    pub fn usd_cny(&self) -> f64 {
        self.store.usd_cny()
    }

    pub fn settings(&self, settings: &Map<String, Value>) -> Settings {
        crate::pricing_settings::read(settings)
    }

    pub fn current_policy_id(&self, model: &str, settings: &Map<String, Value>) -> Option<String> {
        let values = self.settings(settings);
        self.store
            .current_policy_id(model, values.enabled, values.variant_inherit)
    }

    pub fn cost_for_row(&self, row: &Value, settings: &Map<String, Value>, details: bool) -> Value {
        self.store
            .cost_for_row(row, self.settings(settings).enabled, details)
    }

    /// 整扫一次的行级成本：面板聚合（/usage、perf、analytics、recent）都走这里，
    /// 避免逐行克隆价表。
    pub fn cost_rows(
        &self,
        rows: &[&crate::panel_usage::Row],
        settings: &Map<String, Value>,
    ) -> Vec<crate::pricing_store::RowCost> {
        self.store.cost_rows(rows, self.settings(settings).enabled)
    }

    pub fn status(&self, settings: &Map<String, Value>) -> Value {
        let values = self.settings(settings);
        let policies = self.store.load_policies();
        let (at, assignment) = self.store.current_assignment();
        let runtime = self
            .runtime
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let current = current_map(&policies, &assignment);
        let (gaps, gap_summary) = self.gap_report(&policies, &assignment, values.variant_inherit);
        json!({
            "interval_minutes": values.refresh_minutes,
            "enabled": values.enabled && values.refresh_minutes > 0.0,
            "master_enabled": values.enabled,
            "running": runtime.running,
            "last_run": runtime.last_run,
            "last_run_label": runtime.last_run.map(local_label),
            "next_run": Value::Null,
            "next_run_label": Value::Null,
            "last_error": runtime.last_error,
            "last_models": runtime.last_models,
            "last_extra": runtime.last_extra,
            "policies": policies.len(),
            "models": assignment.len(),
            "current_at": at,
            "current_at_label": at.map(local_label),
            "current": current,
            "gaps": gaps,
            "gap_summary": gap_summary,
            "variant_inherit": values.variant_inherit,
            "overrides_file": self.store.overrides_path().display().to_string(),
            "policies_file": self.store.policies_path().display().to_string(),
            "timeline": self.store.timeline_path().display().to_string(),
            "logs": runtime.logs,
        })
    }

    pub fn begin_refresh(&self) -> bool {
        let mut runtime = self
            .runtime
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if runtime.running {
            return false;
        }
        runtime.running = true;
        true
    }

    pub async fn run_refresh(&self, app: Arc<App>) {
        if !self.begin_refresh() {
            return;
        }
        let settings = app.state.settings_snapshot();
        let values = self.settings(&settings);
        if !values.enabled {
            self.finish_error("价估算已关闭".to_string());
            return;
        }
        match fetch_openrouter(&self.config).await {
            Ok(or_models) => self.commit_refresh(&or_models, values.variant_inherit),
            Err(error) => self.finish_error(error),
        }
    }

    fn commit_refresh(&self, or_models: &Map<String, Value>, variants: bool) {
        let extra_ids: Vec<String> = Vec::new();
        let overrides = self.store.effective_overrides();
        let generated_at = crate::state::local_stamp().replace(' ', "T");
        let (doc, unpriced, overridden) = build_snapshot(
            or_models,
            &self.store.defaults().model_ids,
            &overrides,
            self.store.defaults(),
            variants,
            &generated_at,
            &self.config.url,
        );
        if doc
            .get("models")
            .and_then(Value::as_object)
            .is_none_or(Map::is_empty)
        {
            self.finish_error("抓到了模型列表，但一个都没匹配上".to_string());
            return;
        }
        let stamp = now_epoch();
        let result = self.store.record_policies(&doc, Some(stamp), false);
        self.store.remember_live_index(or_models.clone());
        let models = doc
            .get("models")
            .and_then(Value::as_object)
            .map(Map::len)
            .unwrap_or(0);
        let mut runtime = self
            .runtime
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        runtime.running = false;
        runtime.last_run = Some(stamp);
        runtime.last_error = None;
        runtime.last_models = models;
        runtime.last_extra = extra_ids.len();
        push_log(
            &mut runtime,
            format!(
                "已取价：{models} 个模型（新增策略 {}，人工覆盖 {}，未定价 {}）",
                result.added,
                overridden.len(),
                unpriced.len()
            ),
        );
    }

    fn finish_error(&self, error: String) {
        let mut runtime = self
            .runtime
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        runtime.running = false;
        if !error.is_empty() {
            push_log(&mut runtime, error.clone());
        }
        runtime.last_error = Some(error);
    }

    fn gap_report(
        &self,
        policies: &std::collections::HashMap<String, Value>,
        assignment: &BTreeMap<String, String>,
        variants: bool,
    ) -> (Vec<Value>, Value) {
        let known = known_models(&self.store, policies, assignment);
        let live = self.store.live_models().unwrap_or_default();
        let by_norm = index_openrouter(&live);
        let overrides = self.store.effective_overrides();
        let defaults = self.store.defaults();
        let mut items = Vec::new();
        let mut counts = BTreeMap::from([
            ("alias", 0usize),
            ("or_missing", 0usize),
            ("variant_unmatched", 0usize),
        ]);
        for model in &defaults.model_ids {
            if known.contains(model) {
                continue;
            }
            if !live.is_empty()
                && resolve(model, &live, &by_norm, &overrides, defaults, variants)
                    .0
                    .is_some()
            {
                continue;
            }
            let reason = if defaults.aliases.contains(model) {
                "alias"
            } else if variant_suffix(model, defaults).is_some() {
                "variant_unmatched"
            } else {
                "or_missing"
            };
            *counts.get_mut(reason).unwrap() += 1;
            items.push(gap_item(model, reason));
        }
        let summary = json!({
            "total": counts["or_missing"] + counts["variant_unmatched"],
            "or_missing": counts["or_missing"],
            "variant_unmatched": counts["variant_unmatched"],
            "aliases": counts["alias"],
            "variants_enabled": variants,
        });
        (items, summary)
    }
}

impl Pricing {
    #[allow(dead_code)] // Called by the parent wired manual-refresh handler.
    pub(super) async fn run_refresh_staged(&self, app: Arc<App>) {
        let settings = app.state.settings_snapshot();
        if !self.settings(&settings).enabled {
            self.finish_error("价估算已关闭".to_string());
            return;
        }
        match fetch_openrouter(&self.config).await {
            Ok(models) => self.commit_refresh(&models, self.settings(&settings).variant_inherit),
            Err(error) => self.finish_error(error),
        }
    }
}

fn known_models(
    store: &PricingStore,
    policies: &std::collections::HashMap<String, Value>,
    assignment: &BTreeMap<String, String>,
) -> BTreeSet<String> {
    let pricing = store.load_pricing();
    let mut known: BTreeSet<String> = pricing
        .get("models")
        .and_then(Value::as_object)
        .into_iter()
        .flat_map(Map::keys)
        .cloned()
        .collect();
    known.extend(assignment.keys().cloned());
    known.extend(policies.values().filter_map(|policy| {
        policy
            .get("model")
            .and_then(Value::as_str)
            .map(str::to_string)
    }));
    known
}

fn gap_item(model: &str, reason: &str) -> Value {
    let mut item = Map::new();
    item.insert("model".into(), json!(model));
    item.insert("reason".into(), json!(reason));
    if reason != "alias" {
        item.insert("candidates".into(), json!([]));
    }
    Value::Object(item)
}

fn current_map(
    policies: &std::collections::HashMap<String, Value>,
    assignment: &BTreeMap<String, String>,
) -> Value {
    let mut current = Map::new();
    for (model, id) in assignment {
        let policy = policies.get(id);
        current.insert(
            model.clone(),
            json!({
                "id": id,
                "label": policy
                    .and_then(|row| as_f64(row.get("first_seen")))
                    .map(local_label),
                "via": policy
                    .and_then(|row| row.get("via"))
                    .and_then(Value::as_str)
                    .unwrap_or("direct"),
                "inherited_from": policy
                    .and_then(|row| row.get("inherited_from"))
                    .cloned()
                    .unwrap_or(Value::Null),
            }),
        );
    }
    Value::Object(current)
}

fn push_log(runtime: &mut Runtime, message: String) {
    runtime
        .logs
        .push(format!("[{}] {message}", crate::state::local_stamp()));
    if runtime.logs.len() > 40 {
        runtime.logs.remove(0);
    }
}

fn variant_suffix<'a>(
    model: &str,
    defaults: &'a crate::pricing_engine::Defaults,
) -> Option<&'a str> {
    defaults
        .variant_suffixes
        .iter()
        .find_map(|(suffix, enabled)| {
            (*enabled && model.ends_with(suffix)).then_some(suffix.as_str())
        })
}

fn env_usize(name: &str, default: usize) -> usize {
    std::env::var(name)
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(default)
}
