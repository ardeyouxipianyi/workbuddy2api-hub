//! `/accounts` row shape with live pool and daily-usage state.

use serde_json::{json, Value};

/// 风控窗口剩余秒数；没有限制时回 null（`wb_accounts.py:1264`）。
fn restricted_for(account: &crate::state::AccountFile, now: f64) -> Value {
    let left = (account.restricted_until() - now).max(0.0).round();
    (left > 0.0)
        .then_some(left)
        .map_or(Value::Null, |left| json!(left))
}

fn runtime_fields(app: &crate::App, account: &crate::state::AccountFile, now: i64) -> Value {
    let uid = account.uid();
    let breaker = app.pool.breaker_view(&uid);
    let simple_cooldown = app.pool.cooldown_for(&uid).unwrap_or(0);
    let restricted = (account.restricted_until() - now as f64).max(0.0).round() as i64;
    let breaker_left = (breaker.breaker_until - now as f64).max(0.0).round() as i64;
    let degrade_left = (breaker.degrade_until - now as f64).max(0.0).round() as i64;
    let cooldown_left = simple_cooldown
        .max(restricted)
        .max(breaker_left)
        .max(degrade_left);
    let last_fail_for = (breaker.last_fail_at > 0.0)
        .then_some((now as f64 - breaker.last_fail_at).max(0.0).round() as i64);
    json!({
        "inCooldown": cooldown_left > 0,
        "cooldownFor": (cooldown_left > 0).then_some(cooldown_left),
        "restrictedFor": restricted_for(account, now as f64),
        "breakerFor": (breaker_left > 0).then_some(breaker_left),
        "degradeFor": (degrade_left > 0).then_some(degrade_left),
        "fails": breaker.fails,
        "failWindow": app.pool.fail_window(),
        "lastFailFor": last_fail_for,
    })
}

fn guard_fields(app: &crate::App, account: &crate::state::AccountFile) -> Value {
    let guards = app.pool.account_guards(
        &account.uid(),
        &account.realm(),
        account.data.get("credits"),
    );
    let daily_credits = guards
        .daily_credits_today
        .map(|credits| (credits * 100.0).round() / 100.0);
    json!({
        "reserveCredits": guards.limits.reserve_credits,
        "reserveBlocked": guards.reserve_blocked,
        "dailyTokenLimit": guards.limits.daily_token_limit,
        "dailyTokensToday": guards.daily_tokens_today,
        "dailyLimitBlocked": guards.daily_limit_blocked,
        "dailyCreditLimit": guards.limits.daily_credit_limit,
        "dailyCreditsToday": daily_credits,
        "creditLimitReached": guards.credit_limit_reached,
        "modelDailyTokenLimit": guards.limits.model_daily_token_limit,
        "modelDailyTokens": guards.model_daily_tokens,
    })
}

fn merge(view: &mut Value, extra: Value) {
    if let (Some(view), Value::Object(extra)) = (view.as_object_mut(), extra) {
        view.extend(extra);
    }
}

fn core_fields(account: &crate::state::AccountFile, now: i64) -> Value {
    let raw = &account.data;
    let uid = account.uid();
    let realm = account.realm();
    let exp = match account.expires_at() {
        0 => crate::state::jwt_exp(&account.access_token()),
        value => Some(value),
    };
    let token_expired =
        exp.map(|expires| expires <= now).unwrap_or(true) || account.access_token().is_empty();
    let enabled = account.enabled();
    json!({
        "uid": uid,
        "nickname": raw.get("nickname").and_then(Value::as_str).filter(|s| !s.is_empty())
            .map(|s| s.to_string())
            .unwrap_or_else(|| uid.chars().take(8).collect()),
        "domain": account.domain(),
        "realm": realm,
        "platform": raw.get("platform").and_then(Value::as_str).unwrap_or(""),
        "product": account.product(),
        "enterpriseId": raw.get("enterpriseId").and_then(Value::as_str).unwrap_or(""),
        "enabled": enabled,
        "tasksEnabled": raw.get("tasksEnabled").and_then(Value::as_bool).unwrap_or(enabled),
        "weight": raw.get("weight").and_then(Value::as_i64).unwrap_or(1),
        "note": raw.get("note").and_then(Value::as_str).unwrap_or(""),
        "disabledModels": raw.get("disabledModels").cloned().unwrap_or(json!([])),
        "source": raw.get("source").and_then(Value::as_str).unwrap_or("oauth"),
        "apiKey": account.is_api_key(),
        "credentialMask": "",
        "proxySlot": raw.get("proxySlot").and_then(Value::as_str).unwrap_or(""),
        "proxy": raw.get("proxy").and_then(Value::as_str).unwrap_or(""),
        "expiresAt": exp,
        "expiresIn": if token_expired { json!("expired") } else { json!(human_delta(exp.unwrap_or(0) - now)) },
        "tokenExpired": token_expired,
        "needRelogin": token_expired,
        "hasRefreshToken": !account.refresh_token().is_empty(),
        "lastError": "",
        "lastErrorDetail": Value::Null,
        "modelCooldowns": [],
        "softStreak": 0,
        "unscopedStreak": 0,
    })
}

fn metadata_fields(account: &crate::state::AccountFile) -> Value {
    let raw = &account.data;
    let uid = account.uid();
    let realm = account.realm();
    json!({
        "addedAt": raw.get("addedAt").cloned().unwrap_or(Value::Null),
        "file": account.file_name(),
        "credits": raw.get("credits").cloned().unwrap_or(Value::Null),
        "tier": raw.get("tier").filter(|value| value.is_object()).cloned().unwrap_or(Value::Null),
        "lastCheckin": raw.get("lastCheckin").filter(|value| !value.is_null() && value.as_i64() != Some(0)).cloned().unwrap_or(Value::Null),
        "lastDailyChat": raw.get("lastDailyChat").filter(|value| !value.is_null() && value.as_i64() != Some(0)).cloned().unwrap_or(Value::Null),
        "lastActivityReport": raw.get("lastActivityReport").filter(|value| !value.is_null() && value.as_i64() != Some(0)).cloned().unwrap_or(Value::Null),
        "canCheckin": realm == "cn",
        "canDailyChat": realm == "intl",
        "canReportActivity": realm == "cn" || realm == "intl",
        "machineId": crate::state::derive_id(&uid, "machine"),
        "sessionId": crate::state::derive_id(&uid, "session"),
    })
}

pub(crate) fn account_view(
    app: &crate::App,
    account: &crate::state::AccountFile,
    now: i64,
) -> Value {
    let mut view = core_fields(account, now);
    merge(&mut view, runtime_fields(app, account, now));
    merge(&mut view, guard_fields(app, account));
    merge(&mut view, metadata_fields(account));
    view
}

fn human_delta(seconds: i64) -> String {
    if seconds <= 0 {
        return "expired".into();
    }
    let days = seconds as f64 / 86400.0;
    if days >= 1.0 {
        return format!("{days:.0} days");
    }
    let hours = seconds as f64 / 3600.0;
    if hours >= 1.0 {
        return format!("{hours:.1} hours");
    }
    format!("{} min", seconds / 60)
}
