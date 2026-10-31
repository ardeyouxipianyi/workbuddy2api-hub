//! Per-realm limit publication and model-aware account eligibility.

use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{Map, Value};

use crate::daily_usage::{DailyUsage, DailyUsageCache};

#[derive(Clone, Copy, Default)]
pub struct Limits {
    pub reserve_credits: i64,
    pub daily_token_limit: i64,
    pub daily_credit_limit: i64,
    pub model_daily_token_limit: i64,
}

#[derive(Clone, Default)]
struct RealmLimits {
    global: Limits,
    intl: Limits,
    cn: Limits,
}

#[derive(Default)]
struct GuardState {
    limits: RealmLimits,
    usage: Option<DailyUsage>,
    token_published: bool,
    credit_published: bool,
    model_published: bool,
    free_intl: HashSet<String>,
    free_cn: HashSet<String>,
    free_at: f64,
}

#[derive(Clone, Default)]
pub struct AccountGuards {
    pub limits: Limits,
    pub daily_tokens_today: Option<i64>,
    pub daily_credits_today: Option<f64>,
    pub model_daily_tokens: Option<HashMap<String, i64>>,
    pub reserve_blocked: bool,
    pub daily_limit_blocked: bool,
    pub credit_limit_reached: bool,
}

pub struct Guards {
    state: Mutex<GuardState>,
    usage_cache: DailyUsageCache,
}

impl Default for Guards {
    fn default() -> Self {
        Self {
            state: Mutex::new(GuardState::default()),
            usage_cache: DailyUsageCache::default(),
        }
    }
}

impl Guards {
    pub fn refresh(
        &self,
        settings: &Map<String, Value>,
        catalog: &Value,
        usage_path: &Path,
        force: bool,
    ) {
        let limits = resolve_limits(settings);
        let token_on = limits.intl.daily_token_limit > 0 || limits.cn.daily_token_limit > 0;
        let credit_on = limits.intl.daily_credit_limit > 0 || limits.cn.daily_credit_limit > 0;
        let model_on =
            limits.intl.model_daily_token_limit > 0 || limits.cn.model_daily_token_limit > 0;
        let daily_on = token_on || credit_on || model_on;
        let usage = daily_on
            .then(|| self.usage_cache.stats(usage_path, force.then_some(0.0)))
            .flatten();
        let now = epoch();
        let refresh_free =
            force || now - self.state.lock().unwrap_or_else(|e| e.into_inner()).free_at >= 60.0;
        let free = refresh_free.then(|| {
            (
                crate::models::free_models("intl", catalog),
                crate::models::free_models("cn", catalog),
            )
        });
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        state.limits = limits;
        if usage.is_some() {
            state.usage = usage;
            state.token_published |= token_on || force;
            state.credit_published |= credit_on || force;
            state.model_published |= model_on || force;
        }
        if let Some((intl, cn)) = free {
            state.free_intl = intl;
            state.free_cn = cn;
            state.free_at = now;
        }
    }

    pub fn blocked(
        &self,
        uid: &str,
        realm: &str,
        credits: Option<&Value>,
        model: Option<&str>,
    ) -> bool {
        let state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        let limits = state.limits.for_realm(realm);
        let free = state.free_for(realm);
        if reserve_blocked(limits.reserve_credits, credits, model, free) {
            return true;
        }
        let Some(usage) = state.usage.as_ref() else {
            return false;
        };
        if limits.daily_token_limit > 0
            && usage.tokens.get(uid).copied().unwrap_or(0) >= limits.daily_token_limit
        {
            return true;
        }
        if limits.daily_credit_limit > 0
            && model
                .filter(|id| !id.is_empty())
                .map(|id| !free.contains(id))
                .unwrap_or(false)
            && usage.credits.get(uid).copied().unwrap_or(0.0) >= limits.daily_credit_limit as f64
        {
            return true;
        }
        model
            .filter(|id| !id.is_empty() && limits.model_daily_token_limit > 0)
            .and_then(|id| usage.models.get(uid).and_then(|models| models.get(id)))
            .map(|used| *used >= limits.model_daily_token_limit)
            .unwrap_or(false)
    }

    pub fn account(&self, uid: &str, realm: &str, credits: Option<&Value>) -> AccountGuards {
        let state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        let limits = state.limits.for_realm(realm);
        let free = state.free_for(realm);
        let daily_tokens = state
            .token_published
            .then(|| {
                state
                    .usage
                    .as_ref()
                    .map(|usage| usage.tokens.get(uid).copied().unwrap_or(0))
            })
            .flatten();
        let daily_credits = state
            .credit_published
            .then(|| {
                state
                    .usage
                    .as_ref()
                    .map(|usage| usage.credits.get(uid).copied().unwrap_or(0.0))
            })
            .flatten();
        let model_daily_tokens = state
            .model_published
            .then(|| {
                state
                    .usage
                    .as_ref()
                    .map(|usage| usage.models.get(uid).cloned().unwrap_or_default())
            })
            .flatten();
        let reserve = reserve_blocked(limits.reserve_credits, credits, None, free);
        let daily_limit =
            limits.daily_token_limit > 0 && daily_tokens.unwrap_or(0) >= limits.daily_token_limit;
        let credit_reached = limits.daily_credit_limit > 0
            && daily_credits.unwrap_or(0.0) >= limits.daily_credit_limit as f64;
        AccountGuards {
            limits,
            daily_tokens_today: daily_tokens,
            daily_credits_today: daily_credits,
            model_daily_tokens,
            reserve_blocked: reserve,
            daily_limit_blocked: daily_limit,
            credit_limit_reached: credit_reached,
        }
    }
}

impl RealmLimits {
    fn for_realm(&self, realm: &str) -> Limits {
        match realm {
            "intl" => self.intl,
            "cn" => self.cn,
            _ => self.global,
        }
    }
}

impl GuardState {
    fn free_for(&self, realm: &str) -> &HashSet<String> {
        if realm == "cn" {
            &self.free_cn
        } else {
            &self.free_intl
        }
    }
}

fn resolve_limits(settings: &Map<String, Value>) -> RealmLimits {
    let limits = crate::panel_settings::limits(settings);
    let values = |key: &str| {
        let entry = limits.get(key);
        let global = entry
            .and_then(|value| value.get("global"))
            .and_then(Value::as_i64)
            .unwrap_or(0)
            .max(0);
        let scoped = |realm: &str| {
            entry
                .and_then(|value| value.get(realm))
                .and_then(Value::as_i64)
                .filter(|value| *value >= 0)
                .unwrap_or(global)
        };
        (global, scoped("intl"), scoped("cn"))
    };
    let reserve = values("reserve_credits");
    let daily = values("daily_token_limit");
    let credit = values("daily_credit_limit");
    let model = values("model_daily_token_limit");
    let combine = |index: usize| {
        let pick = |entry: (i64, i64, i64)| match index {
            0 => entry.0,
            1 => entry.1,
            _ => entry.2,
        };
        Limits {
            reserve_credits: pick(reserve),
            daily_token_limit: pick(daily),
            daily_credit_limit: pick(credit),
            model_daily_token_limit: pick(model),
        }
    };
    RealmLimits {
        global: combine(0),
        intl: combine(1),
        cn: combine(2),
    }
}

fn reserve_blocked(
    reserve: i64,
    credits: Option<&Value>,
    model: Option<&str>,
    free: &HashSet<String>,
) -> bool {
    if reserve <= 0 || model.map(|id| free.contains(id)).unwrap_or(false) {
        return false;
    }
    let Some(remain) = credits
        .and_then(Value::as_object)
        .and_then(|credits| credits.get("remain"))
        .and_then(|value| {
            value
                .as_f64()
                .or_else(|| value.as_i64().map(|value| value as f64))
                .or_else(|| value.as_str().and_then(|text| text.trim().parse().ok()))
        })
    else {
        return false;
    };
    remain as i64 <= reserve
}

fn epoch() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(0.0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn grouped_limits_resolve_global_and_realm_overrides() {
        let settings = Map::from_iter([(
            "limits".to_string(),
            json!({
                "reserve_credits": {"global": 5, "intl": 2, "cn": null},
                "daily_token_limit": {"global": 100, "intl": 0, "cn": 50},
                "daily_credit_limit": {"global": 7, "intl": null, "cn": 3},
                "model_daily_token_limit": {"global": 20, "intl": null, "cn": null}
            }),
        )]);
        let limits = resolve_limits(&settings);
        assert_eq!(limits.intl.reserve_credits, 2);
        assert_eq!(limits.intl.daily_token_limit, 0);
        assert_eq!(limits.cn.daily_credit_limit, 3);
        assert_eq!(limits.cn.model_daily_token_limit, 20);
    }

    #[test]
    fn guard_exempts_free_models_and_fails_open_without_usage() {
        let guards = Guards::default();
        let free = HashSet::from(["free".to_string()]);
        let credits = json!({"remain": 0});
        assert!(!reserve_blocked(1, Some(&credits), Some("free"), &free));
        assert!(reserve_blocked(1, Some(&credits), Some("paid"), &free));
        assert!(reserve_blocked(1, Some(&credits), None, &free));
        assert!(!reserve_blocked(1, None, Some("paid"), &free));
        assert!(!guards.blocked("a", "intl", Some(&credits), Some("paid")));
    }

    #[test]
    fn published_usage_publishes_counts_and_model_specific_blocks() {
        let root = std::env::temp_dir().join(format!(
            "wb-guards-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&root).unwrap();
        let log = root.join("usage.jsonl");
        let now = epoch() as i64;
        std::fs::write(
            &log,
            format!(
                "{}\n{}\n",
                json!({"at": now, "account": "a", "model": "paid", "total_tokens": 150, "credit": 6.25}),
                json!({"at": now, "account": "a", "model": "gpt", "total_tokens": 200, "credit": 1.0})
            ),
        )
        .unwrap();
        let settings = Map::from_iter([(
            "limits".to_string(),
            json!({
                "reserve_credits": {"global": 5, "intl": null, "cn": null},
                "daily_token_limit": {"global": 0, "intl": null, "cn": null},
                "daily_credit_limit": {"global": 5, "intl": null, "cn": null},
                "model_daily_token_limit": {"global": 150, "intl": null, "cn": null}
            }),
        )]);
        let guards = Guards::default();
        guards.refresh(&settings, &Value::Null, &log, false);
        let credits = json!({"remain": 5});
        assert!(guards.blocked("a", "intl", Some(&credits), Some("paid")));
        assert!(!guards.blocked("a", "intl", Some(&credits), Some("hy4-preview-f")));
        let row = guards.account("a", "intl", Some(&credits));
        assert_eq!(row.daily_tokens_today, None);
        assert_eq!(row.daily_credits_today, Some(7.25));
        assert_eq!(row.model_daily_tokens.unwrap()["gpt"], 200);
        assert!(row.reserve_blocked);
        assert!(!row.daily_limit_blocked);
        assert!(row.credit_limit_reached);
        let token_only = Map::from_iter([(
            "limits".to_string(),
            json!({"daily_token_limit": {"global": 1, "intl": null, "cn": null}}),
        )]);
        guards.refresh(&token_only, &Value::Null, &log, false);
        assert!(guards.blocked("a", "intl", None, Some("hy4-preview-f")));
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn zero_and_unknown_usage_do_not_block() {
        let root = std::env::temp_dir().join(format!(
            "wb-guards-missing-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&root).unwrap();
        let settings = Map::from_iter([(
            "limits".to_string(),
            json!({"daily_token_limit": {"global": 1, "intl": null, "cn": null}}),
        )]);
        let guards = Guards::default();
        guards.refresh(&settings, &Value::Null, &root.join("missing.jsonl"), true);
        assert!(!guards.blocked("a", "intl", None, Some("gpt")));
        assert_eq!(guards.account("a", "intl", None).daily_tokens_today, None);

        let off = Map::from_iter([(
            "limits".to_string(),
            json!({"daily_token_limit": {"global": 0, "intl": null, "cn": null}}),
        )]);
        guards.refresh(&off, &Value::Null, &root.join("missing.jsonl"), true);
        assert!(!guards.blocked("a", "intl", None, Some("gpt")));
        let _ = std::fs::remove_dir_all(root);
    }
}
