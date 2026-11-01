//! CodeArts 模型目录：三路发现（agent-center / builtin / 限时福利）、合并、缓存。
//!
//! 对照 `wb_codearts.py` 的 `_canonical_model`/`_merge_models`/`_fetch_*`/
//! `fetch_models`/`ensure_models`/`model_entries`/`models_meta`/`refresh_models`。

use serde_json::{json, Map, Value};

use super::client::{query_string, signed_get, signed_post};
use super::{
    benefit_host, now_secs, snap_host, Account, Pool, Result, CATALOG_TTL, EP_AGENT_DETAIL,
    EP_AGENT_LIST, EP_BENEFIT_CLAIM, EP_BENEFIT_CONFIG, EP_MODEL_BUILTIN, SEED_BENEFIT_MODELS,
    SEED_KNOWN_MODELS,
};

fn text(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(text)) => text.clone(),
        Some(Value::Number(number)) => number.to_string(),
        Some(Value::Null) | None => String::new(),
        Some(other) => other.to_string(),
    }
}

fn number(value: Option<&Value>) -> i64 {
    match value {
        Some(Value::Number(n)) => n.as_i64().or_else(|| n.as_f64().map(|v| v as i64)).unwrap_or(0),
        Some(Value::String(text)) => text.trim().parse().unwrap_or(0),
        _ => 0,
    }
}

fn seed_models() -> Vec<Value> {
    let benefit: Vec<String> = SEED_BENEFIT_MODELS.iter().map(|m| m.to_lowercase()).collect();
    SEED_KNOWN_MODELS
        .iter()
        .chain(SEED_BENEFIT_MODELS.iter())
        .map(|mid| {
            json!({
                "id": mid,
                "name": mid,
                "benefit": benefit.contains(&mid.to_lowercase()),
            })
        })
        .collect()
}

/// `_canonical_model`：把客户端写的别名收敛成目录里的正式 id。
pub fn canonical_model(account: &Account, model: &str) -> String {
    let mid = model.trim();
    if mid.is_empty() {
        return mid.to_string();
    }
    let lower = mid.to_lowercase();
    for item in account.models() {
        if text(item.get("id")).to_lowercase() == lower {
            return text(item.get("id"));
        }
    }
    for seed in SEED_KNOWN_MODELS.iter().chain(SEED_BENEFIT_MODELS.iter()) {
        if seed.to_lowercase() == lower {
            return (*seed).to_string();
        }
    }
    match lower.as_str() {
        "snap-chat" | "glm-5.2" => "GLM-5.2".into(),
        "glm-5.1" => "GLM-5.1".into(),
        "glm-4.7" => "GLM-4.7".into(),
        "qwen3-vl-235b" => "Qwen3-VL-235B".into(),
        "qwen3.5-397b-a17b-vl" => "Qwen3.5-397B-A17B-VL".into(),
        "qwen3.6-27b-vl" => "Qwen3.6-27B-VL".into(),
        _ => mid.to_string(),
    }
}

/// `_merge_models`：按 id（小写）合并，后到的只补空缺字段，benefit 取或。
pub fn merge_models(sets: &[Vec<Value>]) -> Vec<Value> {
    let mut merged: Vec<(String, Value)> = Vec::new();
    for items in sets {
        for item in items {
            let mid = text(item.get("id")).trim().to_string();
            if mid.is_empty() {
                continue;
            }
            let key = mid.to_lowercase();
            if let Some((_, current)) = merged.iter_mut().find(|(existing, _)| *existing == key) {
                if number(current.get("context_window")) == 0 && number(item.get("context_window")) > 0 {
                    current["context_window"] = json!(number(item.get("context_window")));
                }
                if number(current.get("max_tokens")) == 0 && number(item.get("max_tokens")) > 0 {
                    current["max_tokens"] = json!(number(item.get("max_tokens")));
                }
                if item.get("benefit").and_then(Value::as_bool).unwrap_or(false) {
                    current["benefit"] = json!(true);
                }
                if text(current.get("desc")).is_empty() && !text(item.get("desc")).is_empty() {
                    current["desc"] = item["desc"].clone();
                }
                continue;
            }
            let name = {
                let name = text(item.get("name"));
                if name.is_empty() { mid.clone() } else { name }
            };
            merged.push((
                key,
                json!({
                    "id": mid,
                    "name": name,
                    "context_window": number(item.get("context_window")),
                    "max_tokens": number(item.get("max_tokens")),
                    "desc": text(item.get("desc")),
                    "benefit": item.get("benefit").and_then(Value::as_bool).unwrap_or(false),
                }),
            ));
        }
    }
    merged.into_iter().map(|(_, value)| value).collect()
}

async fn fetch_agent_models(account: &Account, timeout: f64) -> Result<Vec<Value>> {
    let list_url = format!(
        "{}{}?{}",
        snap_host(),
        EP_AGENT_LIST,
        query_string(&[
            ("offset", "0".to_string()),
            ("limit", "100".to_string()),
            ("is_primary_agent", "true".to_string()),
        ])
    );
    let listing = signed_get(account, &list_url, &[], timeout, "PromptCenter").await?;
    let agents = listing.get("agents").and_then(Value::as_array).cloned().unwrap_or_default();
    let mut agent_id = String::new();
    for item in &agents {
        let alias_zh = item
            .get("alias")
            .and_then(|alias| alias.get("alias_zh_cn"))
            .and_then(Value::as_str)
            .unwrap_or("");
        if item.get("agent_name").and_then(Value::as_str) == Some("CodeAgent")
            && alias_zh == "智能体"
            && item.get("show_in_ide").and_then(Value::as_bool).unwrap_or(false)
        {
            agent_id = text(item.get("agent_id"));
            break;
        }
    }
    if agent_id.is_empty() {
        if let Some(item) = agents
            .iter()
            .find(|item| item.get("is_primary_agent").and_then(Value::as_bool).unwrap_or(false))
        {
            agent_id = text(item.get("agent_id"));
        }
    }
    if agent_id.is_empty() {
        if let Some(item) = agents.first() {
            agent_id = text(item.get("agent_id"));
        }
    }
    if agent_id.is_empty() {
        return Ok(Vec::new());
    }
    let detail_url = format!(
        "{}{}?{}",
        snap_host(),
        EP_AGENT_DETAIL,
        query_string(&[("agent_id", agent_id)])
    );
    let detail = signed_get(account, &detail_url, &[], timeout, "PromptCenter").await?;
    let models = detail
        .get("gpts")
        .and_then(|gpts| gpts.get("models"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut out = Vec::new();
    for item in models {
        let params = item.get("model_parameters").cloned().unwrap_or(Value::Null);
        let mut mid = text(item.get("model_name"));
        if mid.is_empty() {
            mid = text(item.get("model_id"));
        }
        if mid.is_empty() {
            mid = text(item.get("model_alias"));
        }
        if mid.is_empty() {
            mid = text(params.get("model_id"));
        }
        if mid.is_empty() {
            continue;
        }
        out.push(json!({
            "id": mid,
            "name": mid,
            "context_window": number(params.get("context_window")),
            "max_tokens": number(params.get("max_tokens")),
            "desc": text(params.get("model_desc")),
            "benefit": false,
        }));
    }
    Ok(out)
}

async fn fetch_builtin_models(account: &Account, timeout: f64) -> Result<Vec<Value>> {
    let data = signed_get(
        account,
        &format!("{}{}", snap_host(), EP_MODEL_BUILTIN),
        &[],
        timeout,
        "PromptCenter",
    )
    .await?;
    let mut out = Vec::new();
    for item in data.get("builtinModels").and_then(Value::as_array).cloned().unwrap_or_default() {
        let mut mid = text(item.get("model_id"));
        if mid.is_empty() {
            mid = text(item.get("model_name"));
        }
        if mid.is_empty() {
            continue;
        }
        out.push(json!({
            "id": mid,
            "name": mid,
            "context_window": number(item.get("context_window")),
            "max_tokens": number(item.get("max_tokens")),
            "desc": text(item.get("model_desc")),
            "benefit": false,
        }));
    }
    Ok(out)
}

async fn claim_benefit(account: &Account, timeout: f64) {
    // 领取失败不影响目录（Python 同样吞掉异常）。
    let _ = signed_post(
        account,
        &format!("{}{}", benefit_host(), EP_BENEFIT_CLAIM),
        &json!({}),
        &[],
        timeout,
        "",
    )
    .await;
}

async fn fetch_benefit_models(account: &Account, timeout: f64, auto_claim: bool) -> Result<Vec<Value>> {
    if auto_claim {
        claim_benefit(account, timeout).await;
    }
    let data = signed_get(account, &format!("{}{}", benefit_host(), EP_BENEFIT_CONFIG), &[], timeout, "").await?;
    let mut out = Vec::new();
    let models = data
        .get("result")
        .and_then(|result| result.get("models"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    for item in models {
        let mut mid = text(item.get("model_id"));
        if mid.is_empty() {
            mid = text(item.get("model_name"));
        }
        if mid.is_empty() {
            continue;
        }
        let name = {
            let name = text(item.get("model_name"));
            if name.is_empty() { mid.clone() } else { name }
        };
        out.push(json!({
            "id": mid,
            "name": name,
            "context_window": number(item.get("context_window")),
            "max_tokens": number(item.get("max_tokens")),
            "desc": text(item.get("model_desc")),
            "benefit": true,
        }));
    }
    Ok(out)
}

/// `fetch_models`：三路并行，单路失败不影响其他路；全失败保留旧目录/种子。
pub async fn fetch_models(account: &Account, timeout: f64, auto_claim: bool) -> Result<Vec<Value>> {
    let (agent, builtin, benefit) = tokio::join!(
        fetch_agent_models(account, timeout),
        fetch_builtin_models(account, timeout),
        fetch_benefit_models(account, timeout, auto_claim),
    );
    let mut sets: Vec<Vec<Value>> = Vec::new();
    for outcome in [agent, builtin, benefit] {
        if let Ok(items) = outcome {
            if !items.is_empty() {
                sets.push(items);
            }
        }
    }
    let mut merged = merge_models(&sets);
    if merged.is_empty() {
        let existing = account.models();
        if !existing.is_empty() {
            return Ok(existing);
        }
        merged = merge_models(&[seed_models()]);
    }
    account.set_models(merged.clone(), now_secs());
    Ok(merged)
}

/// `ensure_models`：TTL 内直接用缓存；失败保留旧目录。
pub async fn ensure_models(account: &Account, pool: &Pool, force: bool, timeout: f64) -> Result<Vec<Value>> {
    let config = pool.config();
    let age = now_secs() - account.models_fetched_at();
    let existing = account.models();
    if !existing.is_empty() && !force && age < config.models_ttl_seconds.min(CATALOG_TTL) {
        return Ok(existing);
    }
    match fetch_models(account, timeout, config.benefit_auto_claim).await {
        Ok(models) => Ok(models),
        Err(err) => {
            let existing = account.models();
            if existing.is_empty() {
                Err(err)
            } else {
                Ok(existing)
            }
        }
    }
}

/// `model_entries`：所有账号目录并集 + 种子，给 `/v1/models?realm=codearts`。
pub async fn model_entries(pool: &Pool) -> Vec<Value> {
    let mut sets: Vec<Vec<Value>> = Vec::new();
    for account in pool.list_accounts() {
        let _ = super::client::ensure_token(&account, pool).await;
        let _ = ensure_models(&account, pool, false, 8.0).await;
        let models = account.models();
        if !models.is_empty() {
            sets.push(models);
        }
    }
    let mut merged = merge_models(&sets);
    if merged.is_empty() {
        merged = merge_models(&[seed_models()]);
    }
    let now = now_secs() as i64;
    merged
        .into_iter()
        .map(|item| {
            let id = text(item.get("id"));
            let mut entry = Map::new();
            entry.insert("id".into(), json!(id));
            entry.insert("object".into(), json!("model"));
            entry.insert("created".into(), json!(now));
            entry.insert("owned_by".into(), json!("codearts"));
            entry.insert("provider".into(), json!("codearts"));
            let name = {
                let name = text(item.get("name"));
                if name.is_empty() { id.clone() } else { name }
            };
            entry.insert("name".into(), json!(name));
            if number(item.get("context_window")) > 0 {
                entry.insert("context_window".into(), json!(number(item.get("context_window"))));
            }
            if number(item.get("max_tokens")) > 0 {
                entry.insert("max_tokens".into(), json!(number(item.get("max_tokens"))));
            }
            if item.get("benefit").and_then(Value::as_bool).unwrap_or(false) {
                entry.insert("benefit".into(), json!(true));
            }
            Value::Object(entry)
        })
        .collect()
}

pub fn models_meta(pool: &Pool) -> Value {
    let stamps: Vec<f64> = pool
        .list_accounts()
        .iter()
        .map(|account| account.models_fetched_at())
        .filter(|stamp| *stamp > 0.0)
        .collect();
    let fetched_at = if stamps.is_empty() {
        Value::Null
    } else {
        json!(stamps.iter().cloned().fold(f64::MIN, f64::max))
    };
    json!({
        "source": "codearts",
        "fetched_at": fetched_at,
        "stale": false,
        "accounts": pool.list_accounts().len(),
    })
}

/// `refresh_models`：强制刷新每个账号，再回一次目录大小。
pub async fn refresh_models(pool: &Pool) -> Value {
    for account in pool.list_accounts() {
        let _ = ensure_models(&account, pool, true, 20.0).await;
    }
    let entries = model_entries(pool).await;
    json!({"ok": true, "models": entries.len(), "catalog": models_meta(pool)})
}

/// `invalidate_models`：清掉所有账号的目录缓存。
pub fn invalidate_models(pool: &Pool) {
    for account in pool.list_accounts() {
        account.clear_models();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn item(id: &str, extra: Value) -> Value {
        let mut base = json!({"id": id, "name": id, "benefit": false});
        if let (Some(map), Some(extra)) = (base.as_object_mut(), extra.as_object()) {
            for (key, value) in extra {
                map.insert(key.clone(), value.clone());
            }
        }
        base
    }

    #[test]
    fn merge_models_fills_gaps_and_keeps_benefit() {
        let merged = merge_models(&[
            vec![item("GLM-5.2", json!({"context_window": 128000}))],
            vec![item("glm-5.2", json!({"max_tokens": 8192, "benefit": true, "desc": "x"}))],
            vec![item("", json!({})), item("GLM-5.2", json!({"desc": "later"}))],
        ]);
        assert_eq!(merged.len(), 1, "同 id 大小写不同也只留一条");
        assert_eq!(merged[0]["id"], "GLM-5.2");
        assert_eq!(merged[0]["context_window"], 128000);
        assert_eq!(merged[0]["max_tokens"], 8192);
        assert_eq!(merged[0]["benefit"], true);
        // 先到的 desc 优先（Python 只补空缺）。
        assert_eq!(merged[0]["desc"], "x");
    }

    #[test]
    fn seeds_carry_the_benefit_flag() {
        let seeds = seed_models();
        assert!(seeds.iter().any(|m| m["id"] == "GLM-5.2" && m["benefit"] == false));
        assert!(seeds.iter().any(|m| m["id"] == "glm-5.3-flash" && m["benefit"] == true));
    }

    #[test]
    fn models_meta_reports_null_when_nothing_fetched() {
        let dir = std::env::temp_dir().join(format!("wb-ca-meta-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let pool = Pool::new(&dir, super::super::Config::default());
        let meta = models_meta(&pool);
        assert_eq!(meta["source"], "codearts");
        assert!(meta["fetched_at"].is_null());
        assert_eq!(meta["accounts"], 0);
    }
}
