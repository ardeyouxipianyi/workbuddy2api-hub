//! CodeArts 账号运维：账户/套餐信息、overview、health、keepalive、签到、代理槽。
//!
//! 对照 `wb_codearts.py` 的 `fetch_account_info`/`ensure_account_info`/`overview`/
//! `health`/`keepalive`/`checkin`/`set_proxy_slot`/`enable`/`disable`/`remove`。

use std::sync::Arc;

use serde_json::{json, Map, Value};

use super::client::{json_get_with, sign_headers};
use super::{now_secs, snap_host, Account, CodeArtsError, Config, Pool, Result, EP_CREDIT_CLAIM,
            EP_CREDIT_CONFIRM, EP_CREDIT_DELIVERY, EP_PACKAGE_INFO, SEED_BENEFIT_MODELS,
            SEED_KNOWN_MODELS};
use crate::state::write_json_atomic;

fn text(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(text)) => text.clone(),
        Some(Value::Number(number)) => number.to_string(),
        Some(Value::Null) | None => String::new(),
        Some(other) => other.to_string(),
    }
}

fn number(value: Option<&Value>) -> f64 {
    match value {
        Some(Value::Number(n)) => n.as_f64().unwrap_or(0.0),
        Some(Value::String(text)) => text.trim().parse().unwrap_or(0.0),
        _ => 0.0,
    }
}

fn credit_metric(metrics: &[Value], name: &str) -> Option<Value> {
    metrics.iter().find(|item| item.get("name").and_then(Value::as_str) == Some(name)).map(|item| {
        json!({
            "amount": number(item.get("package_credit_amount")),
            "used": number(item.get("package_credit_used")),
            "remain": number(item.get("package_credit_remain")),
            "expiring": number(item.get("package_credit_expiring_amount")),
        })
    })
}

/// `fetch_account_info`：`snap-manager/v1/statistics/plugin`。
///
/// `Agent-Type` / `X-Language` 必须在签名**之后**追加，否则华为网关回 401。
pub async fn fetch_account_info(account: &Account, timeout: f64) -> Result<Value> {
    let url = format!("{}{}", super::snap_manager(), EP_PACKAGE_INFO);
    let base = vec![("Content-Type".to_string(), "application/json".to_string())];
    let mut headers = sign_headers(&account.credential(), "GET", &url, &base, b"")?;
    headers.push(("Agent-Type".into(), "PromptCenter".into()));
    headers.push(("X-Language".into(), "zh-cn".into()));
    let data = json_get_with(&url, &headers, timeout, &account.proxy()).await?;

    let package = data.get("package").cloned().unwrap_or(Value::Null);
    let metrics = data.get("metrics").and_then(Value::as_array).cloned().unwrap_or_default();
    let credits = json!({
        "total": credit_metric(&metrics, "usageTotalPackageCredit"),
        "basic": credit_metric(&metrics, "usageBasicPackageCredit"),
        "on_demand": credit_metric(&metrics, "usageOnDemandPackageCredit"),
        "bonus": credit_metric(&metrics, "usageBonusPackageCredit"),
        "unit": "credit",
    });
    let mut models = Vec::new();
    for item in data.get("model_metrics").and_then(Value::as_array).cloned().unwrap_or_default() {
        let mid = text(item.get("model_id"));
        if mid.is_empty() {
            continue;
        }
        let name = {
            let name = text(item.get("model_name"));
            if name.is_empty() { mid.clone() } else { name }
        };
        models.push(json!({
            "id": mid,
            "name": name,
            "description": text(item.get("model_desc")),
            "type": text(item.get("model_type")),
            "enabled": item.get("enable").and_then(Value::as_bool).unwrap_or(true),
            "status": text(item.get("status")),
            "luxury": item.get("is_luxury").and_then(Value::as_bool).unwrap_or(false),
        }));
    }
    let package_name = {
        let cn = text(package.get("package_name_cn"));
        if cn.is_empty() { text(package.get("package_name_en")) } else { cn }
    };
    let info = json!({
        "is_credit_package": package.get("is_credit_package").and_then(Value::as_bool).unwrap_or(false),
        "is_token_package": package.get("is_token_package").and_then(Value::as_bool).unwrap_or(false),
        "spec_code": text(package.get("spec_code")),
        "package_name": package_name,
        "package_status": text(package.get("status")),
        "start_date": text(data.get("start_date")),
        "end_date": text(data.get("end_date")),
        "credits": credits,
        "models": models,
        "fetched_at": now_secs(),
    });
    account.set_info(info.clone());
    Ok(info)
}

/// `ensure_account_info`：60 秒缓存，失败保留旧值。
pub async fn ensure_account_info(account: &Account, force: bool, timeout: f64) -> Result<Value> {
    if let Some(info) = account.info() {
        if !force && now_secs() - account.info_fetched_at() < 60.0 {
            return Ok(info);
        }
    }
    match fetch_account_info(account, timeout).await {
        Ok(info) => Ok(info),
        Err(err) => match account.info() {
            Some(info) => Ok(info),
            None => Err(err),
        },
    }
}

/// `overview`：账号表 + 汇总积分 + 模型清单。
pub async fn overview(pool: &Pool, force: bool) -> Value {
    for account in pool.list_accounts() {
        let _ = ensure_account_info(&account, force, 8.0).await;
    }
    let mut stats = pool.stats();
    let (mut remain, mut total_amount, mut used) = (0.0, 0.0, 0.0);
    for account in pool.list_accounts() {
        let info = account.info().unwrap_or(Value::Null);
        if let Some(total) = info.get("credits").and_then(|c| c.get("total")).filter(|v| v.is_object()) {
            remain += number(total.get("remain"));
            total_amount += number(total.get("amount"));
            used += number(total.get("used"));
        }
    }
    if let Some(map) = stats.as_object_mut() {
        map.insert("credits".into(), json!((remain * 100.0).round() / 100.0));
        map.insert("credits_total".into(), json!((total_amount * 100.0).round() / 100.0));
        map.insert("credits_used".into(), json!((used * 100.0).round() / 100.0));
    }
    let accounts = pool.list();
    let mut models: Vec<Value> = Vec::new();
    let mut seen: Vec<String> = Vec::new();
    for account in pool.list_accounts() {
        let info = account.info().unwrap_or(Value::Null);
        let infos = info.get("models").and_then(Value::as_array).cloned().unwrap_or_default();
        if !infos.is_empty() {
            for item in infos {
                let mid = text(item.get("id"));
                let key = mid.to_lowercase();
                if key.is_empty() || seen.contains(&key) {
                    continue;
                }
                seen.push(key);
                let name = {
                    let name = text(item.get("name"));
                    if name.is_empty() { mid.clone() } else { name }
                };
                models.push(json!({"id": mid, "name": name, "description": text(item.get("description"))}));
            }
        } else {
            for item in account.models() {
                let mid = text(item.get("id"));
                let key = mid.to_lowercase();
                if key.is_empty() || seen.contains(&key) {
                    continue;
                }
                seen.push(key);
                models.push(json!({"id": mid}));
            }
        }
    }
    if models.is_empty() {
        models = SEED_KNOWN_MODELS
            .iter()
            .chain(SEED_BENEFIT_MODELS.iter())
            .map(|mid| json!({"id": mid}))
            .collect();
    }
    json!({
        "service": "codearts",
        "region": "cn",
        "online": pool.config().enabled,
        "base_url": snap_host(),
        "stats": stats,
        "accounts": accounts,
        "models": models,
        "schedule": {"watch": {"enabled": false}},
    })
}

/// `health`：面板 /health 的 codearts 子块。
pub fn health(pool: &Pool) -> Value {
    let stats = pool.stats();
    json!({
        "ok": pool.config().enabled,
        "configured": pool.config().enabled,
        "accounts": stats["total"],
        "healthy": stats["healthy"],
        "base_url": snap_host(),
    })
}

/// `keepalive`：把快过期/已过期的号续一遍。
pub async fn keepalive(pool: &Pool) -> Value {
    let mut refreshed = 0;
    let accounts = pool.list_accounts();
    for account in &accounts {
        let (disabled, _, _, _, _) = account.state_snapshot();
        if disabled {
            continue;
        }
        if account.expiring_soon(super::REFRESH_SKEW) || account.expired() {
            if super::client::ensure_token(account, pool).await.is_ok() {
                refreshed += 1;
            }
        }
    }
    json!({
        "ok": true,
        "refreshed": refreshed,
        "message": format!("已检查 {} 个账号，续期 {} 个", accounts.len(), refreshed),
    })
}

async fn credit_request(account: &Account, method: &str, path: &str, payload: Option<Value>, timeout: f64) -> Result<Map<String, Value>> {
    let url = format!("{}{}", snap_host(), path);
    match method {
        "GET" => super::client::signed_get(account, &url, &[], timeout, "PromptCenter").await,
        _ => super::client::signed_post(account, &url, &payload.unwrap_or_else(|| json!({})), &[], timeout, "PromptCenter").await,
    }
}

/// `checkin_account`：`/v1/ops/delivery|claim|confirm`（campaignId 1 和 4）。
pub async fn checkin_account(account: &Account, pool: &Pool, timeout: f64) -> Result<Value> {
    let data = credit_request(account, "GET", &format!("{EP_CREDIT_DELIVERY}?channel=IDE"), None, timeout).await?;
    let items = data
        .get("data")
        .and_then(|value| value.get("items"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut results: Vec<Value> = Vec::new();
    let mut total_credits = 0.0;
    for item in items {
        let campaign_id = item.get("campaignId").and_then(Value::as_i64).unwrap_or(0);
        if campaign_id != 1 && campaign_id != 4 {
            continue;
        }
        let mut status = text(item.get("status")).trim().to_string();
        let title = {
            let title = text(item.get("title"));
            if title.is_empty() { format!("积分活动 {campaign_id}") } else { title }
        };
        let unit = text(item.get("benefitUnit"));
        if !unit.is_empty() && unit != "CREDIT" {
            continue;
        }
        if status == "CONFIRMED" || status == "CONSUMED" {
            results.push(json!({"campaign_id": campaign_id, "title": title, "ok": true,
                                "status": status, "message": "已领取"}));
            continue;
        }
        if status == "ELIGIBLE" {
            if !item.get("claimable").and_then(Value::as_bool).unwrap_or(false) {
                results.push(json!({"campaign_id": campaign_id, "title": title, "ok": false,
                                    "status": status, "message": "暂不可领取，稍后重试"}));
                continue;
            }
            let key = format!("claim_{campaign_id}_{}", (now_secs() * 1000.0) as i64);
            credit_request(
                account,
                "POST",
                EP_CREDIT_CLAIM,
                Some(json!({"campaignId": campaign_id, "channel": "IDE", "idempotentKey": key})),
                timeout,
            )
            .await?;
            let credits = {
                let pending = number(item.get("pendingTotalAmount"));
                if pending > 0.0 { pending } else { number(item.get("benefitAmount")) }
            };
            total_credits += credits;
            status = "CLAIMED".into();
        }
        if status == "CLAIMED" || status == "ELIGIBLE" {
            credit_request(account, "POST", EP_CREDIT_CONFIRM, Some(json!({"campaignId": campaign_id})), timeout).await?;
            let message = if total_credits > 0.0 {
                format!("领取成功（{} 积分）", trim_number(total_credits))
            } else {
                "已补确认".to_string()
            };
            results.push(json!({"campaign_id": campaign_id, "title": title, "ok": true,
                                "status": "CONFIRMED", "credits": total_credits, "message": message}));
        } else if status != "CONFIRMED" && status != "CONSUMED" {
            results.push(json!({"campaign_id": campaign_id, "title": title, "ok": false,
                                "status": status, "message": "未获得活动资格"}));
        }
    }
    let mut message = results
        .iter()
        .map(|item| format!("{}: {}", text(item.get("title")), text(item.get("message"))))
        .collect::<Vec<_>>()
        .join("; ");
    if message.is_empty() {
        message = "无可领取的签到或新手活动".into();
    }
    let ok = if results.is_empty() {
        true
    } else {
        results.iter().all(|item| item.get("ok").and_then(Value::as_bool).unwrap_or(false))
    };
    let result = json!({"ok": ok, "credits": total_credits, "message": message, "campaigns": results});
    account.set_checkin(Some(json!({
        "ok": ok,
        "message": message,
        "checked_at": crate::state::local_stamp(),
    })));
    account.set_info_fetched_at_zero();
    pool.save_state();
    Ok(result)
}

fn trim_number(value: f64) -> String {
    if (value.fract()).abs() < 1e-9 {
        format!("{}", value as i64)
    } else {
        format!("{value}")
    }
}

/// `checkin`：uid 为空 = 全员签到。
pub async fn checkin(pool: &Pool, uid: &str) -> Result<Value> {
    if !uid.is_empty() {
        let Some(account) = pool.get(uid) else {
            return Err(CodeArtsError::NotFound("account not found".into()));
        };
        super::client::ensure_token(&account, pool).await?;
        return checkin_account(&account, pool, 30.0).await;
    }
    let mut results: Vec<Value> = Vec::new();
    for account in pool.list_accounts() {
        let (disabled, _, _, _, _) = account.state_snapshot();
        if disabled {
            continue;
        }
        match super::client::ensure_token(&account, pool).await {
            Ok(()) => match checkin_account(&account, pool, 30.0).await {
                Ok(mut value) => {
                    if let Some(map) = value.as_object_mut() {
                        map.insert("uid".into(), json!(account.uid));
                    }
                    results.push(value);
                }
                Err(err) => results.push(json!({"uid": account.uid, "ok": false, "message": err.message()})),
            },
            Err(err) => results.push(json!({"uid": account.uid, "ok": false, "message": err.message()})),
        }
    }
    let total: f64 = results.iter().map(|item| number(item.get("credits"))).sum();
    let ok = results.is_empty()
        || results.iter().all(|item| item.get("ok").and_then(Value::as_bool).unwrap_or(false));
    Ok(json!({
        "ok": ok,
        "credits": total,
        "message": format!("已处理 {} 个账号，共领取 {} 积分", results.len(), trim_number(total)),
        "results": results,
    }))
}

/// `set_proxy_slot`：把账号绑到一个代理槽（空串 = 直连）。
pub fn set_proxy_slot(pool: &Pool, uid: &str, slot_id: &str) -> Result<Value> {
    let Some(account) = pool.get(uid) else {
        return Err(CodeArtsError::NotFound("account not found".into()));
    };
    let slot = slot_id.trim().to_string();
    account.set_proxy_slot(&slot);
    let mut doc = account.auth();
    if let Some(map) = doc.as_object_mut() {
        if slot.is_empty() {
            map.remove("proxySlot");
        } else {
            map.insert("proxySlot".into(), json!(slot));
        }
    }
    super::save_auth(pool.accounts_dir(), account.path().as_deref(), &doc)?;
    pool.apply_proxy_slots(super::slots_for(pool.accounts_dir()));
    Ok(json!({
        "ok": true,
        "uid": account.uid,
        "proxy_slot": slot,
        "proxy": account.proxy(),
        "message": "出口已更新",
    }))
}

/// `drop_missing_bindings`：槽位被删后清掉账号上的悬空绑定。
pub fn drop_missing_bindings(pool: &Pool, slots: &[Value]) -> usize {
    let valid: Vec<String> = slots.iter().filter_map(|slot| slot.get("id").and_then(Value::as_str).map(|v| v.to_string())).collect();
    let mut changed = 0;
    for account in pool.list_accounts() {
        let bound = account.proxy_slot();
        if !bound.is_empty() && !valid.contains(&bound) {
            account.set_proxy_slot("");
            let mut doc = account.auth();
            if let Some(map) = doc.as_object_mut() {
                map.remove("proxySlot");
            }
            let _ = super::save_auth(pool.accounts_dir(), account.path().as_deref(), &doc);
            changed += 1;
        }
    }
    pool.apply_proxy_slots(slots.to_vec());
    changed
}

/// 面板动作的统一返回。
pub fn require_account<'a>(pool: &'a Pool, uid: &str) -> Result<Arc<Account>> {
    pool.get(uid).ok_or_else(|| CodeArtsError::NotFound("account not found".into()))
}

/// 保存设置时把 codearts 配置写回（面板 `/settings/save` 用）。
pub fn save_settings(app: &Arc<crate::App>, patch: &Map<String, Value>) -> Result<Config> {
    let mut current = super::config_from(&app.state.settings_snapshot()).to_json();
    if let Some(map) = current.as_object_mut() {
        for key in map.keys().cloned().collect::<Vec<_>>() {
            if let Some(value) = patch.get(&key) {
                map.insert(key, value.clone());
            }
        }
    }
    let mut holder = Map::new();
    holder.insert("codearts".into(), current);
    let config = super::config_from(&holder);
    let mut settings = app.state.settings_snapshot();
    settings.insert("codearts".into(), config.to_json());
    write_json_atomic(&app.state.accounts_dir.join("settings.json"), &Value::Object(settings))
        .map_err(|err| CodeArtsError::Other(format!("写入 settings.json 失败：{err}")))?;
    app.state.reload();
    Ok(config)
}
