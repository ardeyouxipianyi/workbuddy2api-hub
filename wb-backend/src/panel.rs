//! 面板：原版 `dashboard.html` + 它调用的那批 JSON 接口。
//!
//! 面板鉴权不是 cookie，而是 `POST /panel/login` 换一个 `X-Panel-Token` 头
//! （`dashboard.html` 的 `authHeaders()` 就是这么发的）。
//!
//! shortcut: 目前只做到「能登录、能看到账号/用量/设置」。写入面（/settings/save、
//! /accounts/set、导入导出、登录流程）、任务/调度、自更新还没移植，命中时返回
//! `{"ok":false,"error":"not ported yet"}` 而不是假装成功。

use std::collections::HashSet;
use std::sync::Mutex;

use hyper::body::Incoming;
use hyper::{Method, Request, StatusCode};
use serde_json::{json, Map, Value};

use crate::http::{json_resp, Resp};
use crate::App;

const DASHBOARD: &str = include_str!("../../dashboard.html");

#[derive(Default)]
pub struct Panel {
    tokens: Mutex<HashSet<String>>,
    password: Mutex<Option<String>>,
}

impl Panel {
    pub fn token_ok(&self, token: Option<&str>) -> bool {
        match token {
            Some(t) => self.tokens.lock().map(|s| s.contains(t)).unwrap_or(false),
            None => false,
        }
    }

    fn password(&self) -> String {
        if let Some(p) = self.password.lock().ok().and_then(|g| g.clone()) {
            return p;
        }
        std::env::var("WB_PANEL_PASSWORD").unwrap_or_default()
    }

    fn password_required(&self) -> bool {
        !self.password().is_empty()
    }
}

fn token_of(req: &Request<Incoming>) -> Option<String> {
    req.headers()
        .get("x-panel-token")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string())
}

fn unauth() -> Resp {
    json_resp(
        StatusCode::UNAUTHORIZED,
        &json!({"error": {"message": "unauthorized", "type": "invalid_request_error", "code": 401}}),
    )
}

fn not_ported(what: &str) -> Resp {
    json_resp(
        StatusCode::NOT_IMPLEMENTED,
        &json!({"ok": false, "error": format!("{what} 还没移植到 Rust 后端")}),
    )
}

/// 面板路由统一入口；不是面板路由返回 None。
pub async fn handle(app: &std::sync::Arc<App>, req: Request<Incoming>) -> Option<Resp> {
    let path = req.uri().path().to_string();
    let method = req.method().clone();

    // 静态：原版 dashboard.html（内嵌，保持单文件交付）。
    if (&method, path.as_str()) == (&Method::GET, "/") || (&method, path.as_str()) == (&Method::GET, "/dashboard") {
        let mut resp = crate::http::text_resp(StatusCode::OK, DASHBOARD);
        resp.headers_mut().insert(
            hyper::header::CONTENT_TYPE,
            hyper::header::HeaderValue::from_static("text/html; charset=utf-8"),
        );
        return Some(resp);
    }

    // 登录/状态：不需要 token。
    if (&method, path.as_str()) == (&Method::POST, "/panel/login") {
        let body = crate::http::body_json(req).await.ok()?;
        let given = body.get("password").and_then(Value::as_str).unwrap_or("");
        if !app.panel.password_required() || given == app.panel.password() {
            let token = crate::sse::hex_token(32);
            if let Ok(mut set) = app.panel.tokens.lock() {
                set.insert(token.clone());
            }
            return Some(json_resp(
                StatusCode::OK,
                &json!({"ok": true, "token": token, "using_default_password": false}),
            ));
        }
        return Some(json_resp(
            StatusCode::UNAUTHORIZED,
            &json!({"error": {"message": "invalid panel password", "type": "invalid_request_error", "code": 401}}),
        ));
    }
    if (&method, path.as_str()) == (&Method::GET, "/panel/status") {
        return Some(json_resp(
            StatusCode::OK,
            &json!({
                "panel_password_required": app.panel.password_required(),
                "panel_password_is_default": false,
                "authenticated": app.panel.token_ok(token_of(&req).as_deref()),
                "crypto_ok": true,
                "api_key_set": false,
            }),
        ));
    }
    if (&method, path.as_str()) == (&Method::POST, "/panel/logout") {
        if let Some(t) = token_of(&req) {
            if let Ok(mut set) = app.panel.tokens.lock() {
                set.remove(&t);
            }
        }
        return Some(json_resp(StatusCode::OK, &json!({"ok": true})));
    }

    // 其余都要 token。
    if !app.panel.token_ok(token_of(&req).as_deref()) {
        return Some(unauth());
    }

    let out = match (&method, path.as_str()) {
        (&Method::GET, "/realm") => json_resp(
            StatusCode::OK,
            &json!({"current": app.state.current_realm(), "options": ["intl", "cn"]}),
        ),
        (&Method::GET, "/accounts") => accounts(app),
        (&Method::GET, "/settings") => settings(app),
        (&Method::GET, "/usage") => usage(app),
        (&Method::GET, "/usage/timeseries") => usage_timeseries(app, &req),
        (&Method::GET, "/usage/perf") => usage_perf(app),
        (&Method::GET, "/usage/remaining") => json_resp(
            StatusCode::OK,
            &json!({"window_seconds": 86400, "budgets": [], "models": [], "rows": []}),
        ),
        (&Method::GET, "/usage/by-account") => usage_by_account(app),
        (&Method::GET, "/usage/recent") => usage_recent(app, &req),
        (&Method::GET, "/usage/analytics") => usage_analytics(app, &req),
        (&Method::GET, "/accounts/credits") => accounts_credits(app, &req),
        (&Method::GET, "/accounts/credits/grants") => {
            json_resp(StatusCode::OK, &credit_grants(app))
        }
        (&Method::GET, "/logs") => {
            let ring = app.logs.lock().map(|r| r.iter().cloned().collect::<Vec<_>>()).unwrap_or_default();
            json_resp(StatusCode::OK, &json!({"total": ring.len(), "logs": ring}))
        }
        (&Method::POST, "/logs/clear") => {
            if let Ok(mut ring) = app.logs.lock() {
                ring.clear();
            }
            json_resp(StatusCode::OK, &json!({"ok": true}))
        }
        (&Method::GET, "/settings/reveal") => {
            let id = req.uri().query().unwrap_or("").split('&')
                .find_map(|p| p.strip_prefix("id=")).unwrap_or("");
            let settings = app.state.settings_snapshot();
            let key = settings.get("api_keys").and_then(Value::as_array).and_then(|list| {
                list.iter()
                    .find(|k| k.get("id").and_then(Value::as_str) == Some(id))
                    .and_then(|k| k.get("key").and_then(Value::as_str))
            });
            match key {
                Some(key) => json_resp(StatusCode::OK, &json!({"key": key})),
                None => json_resp(StatusCode::NOT_FOUND, &json!({"error": {"message": "no such key", "code": 404}})),
            }
        }
        (&Method::POST, "/accounts/set") => accounts_set(app, req).await,
        (&Method::POST, "/accounts/set-all") => accounts_set_all(app, req).await,
        (&Method::POST, "/accounts/add-api-key") => add_api_key(app, req).await,
        (&Method::POST, "/accounts/import") => accounts_import(app, req).await,
        (&Method::POST, "/accounts/delete") => accounts_delete(app, req).await,
        (&Method::POST, "/accounts/export") | (&Method::GET, "/accounts/export") => {
            accounts_export(app, &req)
        }
        (&Method::POST, "/accounts/credits") => accounts_credits_fetch(app, req).await,
        (&Method::POST, "/accounts/product") => accounts_product(app, req).await,
        (&Method::POST, "/accounts/checkin") => checkin(app, req).await,
        (&Method::POST, "/scheduler/toggle") => {
            let on = app.scheduler.enabled();
            app.scheduler.set_enabled(!on);
            json_resp(StatusCode::OK, &json!({"ok": true, "enabled": !on,
                "msg": if on { "调度器已暂停" } else { "调度器已恢复" }}))
        }
        (&Method::POST, "/scheduler/trigger") => {
            let out = crate::scheduler::daily(app).await;
            json_resp(StatusCode::OK, &json!({"ok": true, "msg": "已触发巡检保活", "result": out}))
        }
        (&Method::POST, "/tasks/run") => tasks_run(app, req).await,
        (&Method::POST, "/tasks/travel") => tasks_travel(app, req).await,
        (&Method::POST, "/tasks/claim") => tasks_claim(app, req).await,
        (&Method::POST, "/settings/save") => settings_save(app, req).await,
        (&Method::POST, "/realm") => realm_set(app, req).await,
        (&Method::POST, "/panel/password") => not_ported("/panel/password"),
        (&Method::GET, "/pricing") => json_resp(
            StatusCode::OK,
            &json!({
                "interval_minutes": 0, "enabled": false, "master_enabled": false,
                "running": false, "last_run": Value::Null, "last_run_label": "",
                "next_run": Value::Null, "next_run_label": "", "last_error": "",
                "last_models": [], "last_extra": {}, "policies": [], "variants": [],
                // 价格刷新属于 tasks/ops 组，还没移植：这里如实报告「未启用」，
                // 而不是伪造一份看起来正常的价格表。
                "ported": false,
            }),
        ),
        (&Method::GET, "/tasks") => {
            let uid = req
                .uri()
                .query()
                .unwrap_or("")
                .split('&')
                .find_map(|p| p.strip_prefix("uid="))
                .unwrap_or("");
            json_resp(StatusCode::OK, &crate::tasks::view(app, uid).await)
        }
        (&Method::GET, "/agents") => json_resp(StatusCode::OK, &crate::agents::overview(app)),
        (&Method::GET, "/agents/available") => {
            json_resp(StatusCode::OK, &json!({"models": crate::models::flat_ids()}))
        }
        (&Method::POST, "/agents/model-selection") => {
            let body = crate::http::body_json(req).await.unwrap_or(json!({}));
            json_resp(StatusCode::OK, &crate::agents::save_selection(app, &body))
        }
        (&Method::POST, "/agents/apply") | (&Method::POST, "/agents/restore") => json_resp(
            StatusCode::NOT_IMPLEMENTED,
            &json!({"ok": false, "error": {
                "message": "一键配置（写客户端配置并做备份/回滚）还没移植到 Rust 后端；勾选清单可以保存，但不会改本机客户端配置",
                "code": 501}}),
        ),
        (&Method::GET, "/scheduler") => json_resp(StatusCode::OK, &app.scheduler.status()),
        (&Method::GET, "/updates") => json_resp(
            StatusCode::OK,
            &json!({
                "current_version": env!("CARGO_PKG_VERSION"),
                "latest_version": env!("CARGO_PKG_VERSION"),
                "latest_tag": "", "update_available": false,
                "enabled": false, "checking": false,
                "last_attempt": Value::Null, "last_success": Value::Null, "last_error": "",
                "release_url": "", "published_at": "", "install_enabled": false,
                "ported": false,
            }),
        ),
        (&Method::GET, "/proxy/slots") => json_resp(StatusCode::OK, &json!({"slots": []})),
        (&Method::GET, "/agents") | (&Method::GET, "/agents/available") => not_ported("/agents"),
        (&Method::POST, _) => not_ported(&path),
        _ => json_resp(StatusCode::NOT_FOUND, &json!({"error": {"message": "not found", "code": 404}})),
    };
    Some(out)
}

/// `/accounts`：把账号档拍平成面板要的 58 个字段（`wb_accounts.py:1229 public()`）。
fn accounts(app: &std::sync::Arc<App>) -> Resp {
    let now = crate::state::now_epoch();
    let mut list = Vec::new();
    for account in app.state.accounts_snapshot() {
        list.push(account_view(&account, now));
    }
    let usable = list
        .iter()
        .filter(|a| a.get("enabled").and_then(Value::as_bool).unwrap_or(false)
            && !a.get("tokenExpired").and_then(Value::as_bool).unwrap_or(true))
        .count();
    json_resp(
        StatusCode::OK,
        &json!({"accounts": list, "storage": app.state.accounts_dir_name(), "usable": usable}),
    )
}

pub(crate) fn account_view(account: &crate::state::AccountFile, now: i64) -> Value {
    let raw = &account.data;
    let uid = account.uid();
    let exp = match account.expires_at() {
        0 => crate::state::jwt_exp(&account.access_token()),
        v => Some(v),
    };
    let token_expired = exp.map(|e| e <= now).unwrap_or(true) || account.access_token().is_empty();
    let enabled = account.enabled();
    json!({
        "uid": uid,
        "nickname": raw.get("nickname").and_then(Value::as_str).filter(|s| !s.is_empty())
            .map(|s| s.to_string())
            .unwrap_or_else(|| uid.chars().take(8).collect()),
        "domain": account.domain(),
        "realm": account.realm(),
        "platform": raw.get("platform").and_then(Value::as_str).unwrap_or(""),
        "product": account.product(),
        "enterpriseId": raw.get("enterpriseId").and_then(Value::as_str).unwrap_or(""),
        "enabled": enabled,
        "tasksEnabled": raw.get("tasksEnabled").and_then(Value::as_bool).unwrap_or(true),
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
        "lastError": Value::Null,
        "lastErrorDetail": Value::Null,
        "inCooldown": false,
        "cooldownFor": Value::Null,
        "restrictedFor": Value::Null,
        "modelCooldowns": [],
        "softStreak": 0,
        "unscopedStreak": 0,
        "breakerFor": Value::Null,
        "degradeFor": Value::Null,
        "fails": 0,
        "failWindow": 1800,
        "lastFailFor": Value::Null,
        "addedAt": raw.get("addedAt").cloned().unwrap_or(Value::Null),
        "file": account.file_name(),
        "credits": raw.get("credits").cloned().unwrap_or(Value::Null),
        "tier": raw.get("tier").cloned().unwrap_or(Value::Null),
        "reserveCredits": raw.get("reserveCredits").and_then(Value::as_i64).unwrap_or(0),
        "reserveBlocked": false,
        "dailyTokenLimit": raw.get("dailyTokenLimit").and_then(Value::as_i64).unwrap_or(0),
        "dailyTokensToday": Value::Null,
        "dailyLimitBlocked": false,
        "dailyCreditLimit": raw.get("dailyCreditLimit").and_then(Value::as_i64).unwrap_or(0),
        "dailyCreditsToday": Value::Null,
        "creditLimitReached": false,
        "modelDailyTokenLimit": raw.get("modelDailyTokenLimit").and_then(Value::as_i64).unwrap_or(0),
        "modelDailyTokens": Value::Null,
        "lastCheckin": raw.get("lastCheckin").cloned().unwrap_or(Value::Null),
        "lastDailyChat": raw.get("lastDailyChat").cloned().unwrap_or(Value::Null),
        "lastActivityReport": raw.get("lastActivityReport").cloned().unwrap_or(Value::Null),
        "canCheckin": account.realm() == "cn",
        "canDailyChat": account.realm() == "intl",
        "canReportActivity": account.realm() == "cn" || account.realm() == "intl",
        "machineId": crate::state::derive_id(&uid, "machine"),
        "sessionId": crate::state::derive_id(&uid, "session"),
    })
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

/// `/settings`：settings.json 原样 + 面板要的几个派生键。
fn settings(app: &std::sync::Arc<App>) -> Resp {
    let state = app.state.settings_snapshot();
    let mut out = state;
    let keys = out.get("api_keys").and_then(Value::as_array).cloned().unwrap_or_default();
    let live: Vec<Value> = keys
        .iter()
        .filter(|k| k.get("enabled").and_then(Value::as_bool).unwrap_or(true))
        .cloned()
        .collect();
    out.insert("api_key_set".into(), json!(!live.is_empty()));
    out.insert("api_key_set_by_panel".into(), json!(!live.is_empty()));
    out.insert("api_key_masked".into(), json!(""));
    out.insert("auth_required".into(), json!(!live.is_empty()));
    out.insert("panel_password_is_default".into(), json!(false));
    out.insert("realm".into(), json!(app.state.current_realm()));
    out.insert("usd_cny".into(), json!(7.1));
    json_resp(StatusCode::OK, &Value::Object(out))
}

fn read_usage(path: &std::path::Path) -> Vec<Value> {
    let Ok(text) = std::fs::read_to_string(path) else {
        return Vec::new();
    };
    text.lines()
        .filter(|l| !l.trim().is_empty())
        .filter_map(|l| serde_json::from_str::<Value>(l).ok())
        .collect()
}

fn usage_rows(app: &std::sync::Arc<App>) -> Vec<Value> {
    read_usage(&app.recorder_path())
}

/// `/usage`：面板顶部的总量卡片（26 个键，见 golden/panel/usage.json）。
fn usage(app: &std::sync::Arc<App>) -> Resp {
    let rows = usage_rows(app);
    let mut totals = json!({
        "requests": 0, "errors": 0, "prompt_tokens": 0, "completion_tokens": 0,
        "reasoning_tokens": 0, "cached_tokens": 0, "total_tokens": 0,
        "credit": 0.0, "cost_cny": 0.0, "cost_missing": {},
        "started": crate::state::now_epoch(),
        "by_model": {}, "by_model_realm": {}, "by_model_acct": {},
        "ttft_ms_sum": 0, "ttft_samples": 0, "gen_ms_sum": 0, "gen_samples": 0,
        "wall_ms_sum": 0, "wall_samples": 0,
    });
    let mut accounts_map = Map::new();
    for row in &rows {
        let n = |k: &str| row.get(k).and_then(|v| v.as_i64()).unwrap_or(0);
        for key in ["prompt_tokens", "completion_tokens", "reasoning_tokens", "cached_tokens", "total_tokens"] {
            let v = totals[key].as_i64().unwrap_or(0) + n(key);
            totals[key] = json!(v);
        }
        totals["requests"] = json!(totals["requests"].as_i64().unwrap_or(0) + 1);
        if row.get("outcome").and_then(Value::as_str) != Some("completed") {
            totals["errors"] = json!(totals["errors"].as_i64().unwrap_or(0) + 1);
        }
        if let Some(t) = row.get("ttft_ms").and_then(|v| v.as_i64()) {
            totals["ttft_ms_sum"] = json!(totals["ttft_ms_sum"].as_i64().unwrap_or(0) + t);
            totals["ttft_samples"] = json!(totals["ttft_samples"].as_i64().unwrap_or(0) + 1);
        }
        if let Some(t) = row.get("gen_ms").and_then(|v| v.as_i64()) {
            totals["gen_ms_sum"] = json!(totals["gen_ms_sum"].as_i64().unwrap_or(0) + t);
            totals["gen_samples"] = json!(totals["gen_samples"].as_i64().unwrap_or(0) + 1);
        }
        if let Some(t) = row.get("elapsed_ms").and_then(|v| v.as_i64()) {
            totals["wall_ms_sum"] = json!(totals["wall_ms_sum"].as_i64().unwrap_or(0) + t);
            totals["wall_samples"] = json!(totals["wall_samples"].as_i64().unwrap_or(0) + 1);
        }
        if let Some(account) = row.get("account").and_then(Value::as_str) {
            let realm = row.get("realm").and_then(Value::as_str).unwrap_or("");
            accounts_map.insert(account.to_string(), json!({"nickname": account, "realm": realm}));
        }
    }
    totals["since"] = json!(rows
        .first()
        .and_then(|r| r.get("iso").and_then(Value::as_str))
        .unwrap_or(""));
    totals["log_file"] = json!(app.recorder_path().display().to_string());
    totals["realm"] = json!(app.state.current_realm());
    totals["usd_cny"] = json!(7.1);
    totals["accounts_map"] = Value::Object(accounts_map);
    json_resp(StatusCode::OK, &totals)
}

/// `/usage/timeseries`：按小时桶聚合（形状见 golden/panel/usage-timeseries.json）。
fn usage_timeseries(app: &std::sync::Arc<App>, req: &Request<Incoming>) -> Resp {
    let query = req.uri().query().unwrap_or("");
    let mut bucket: i64 = 3600;
    for pair in query.split('&') {
        if let Some(v) = pair.strip_prefix("bucket=") {
            bucket = v.parse().unwrap_or(3600).max(60);
        }
    }
    let rows = usage_rows(app);
    let until = crate::state::now_epoch();
    let since = until as f64 - 24.0 * 3600.0;
    let mut buckets: std::collections::BTreeMap<i64, (i64, i64)> = Default::default();
    for row in &rows {
        let at = row.get("at").and_then(Value::as_f64).unwrap_or(0.0);
        if at < since {
            continue;
        }
        let slot = ((at / bucket as f64).floor() as i64) * bucket;
        let entry = buckets.entry(slot).or_insert((0, 0));
        entry.0 += 1;
        entry.1 += row.get("total_tokens").and_then(Value::as_i64).unwrap_or(0);
    }
    let series: Vec<Value> = buckets
        .into_iter()
        .map(|(at, (requests, tokens))| json!({"at": at, "requests": requests, "total_tokens": tokens}))
        .collect();
    json_resp(
        StatusCode::OK,
        &json!({
            "ok": true,
            "realm": app.state.current_realm(),
            "bucket_seconds": bucket,
            "since": since,
            "until": until,
            "series": series,
            "credits": [],
        }),
    )
}

/// `/usage/perf`：延迟/成功率（15 个键）。
fn usage_perf(app: &std::sync::Arc<App>) -> Resp {
    let rows = usage_rows(app);
    let sampled = rows.len() as i64;
    let mut success = 0;
    let mut errors = 0;
    let mut aborted = 0;
    let mut ttft = Vec::new();
    let mut gen = Vec::new();
    let mut wall = Vec::new();
    for row in &rows {
        match row.get("outcome").and_then(Value::as_str) {
            Some("completed") => success += 1,
            Some("client_aborted") => aborted += 1,
            _ => errors += 1,
        }
        for (key, bucket) in [("ttft_ms", &mut ttft), ("gen_ms", &mut gen), ("elapsed_ms", &mut wall)] {
            if let Some(v) = row.get(key).and_then(|v| v.as_i64()) {
                bucket.push(v as f64);
            }
        }
    }
    let avg = |v: &Vec<f64>| {
        if v.is_empty() {
            Value::Null
        } else {
            json!((v.iter().sum::<f64>() / v.len() as f64).round())
        }
    };
    json_resp(
        StatusCode::OK,
        &json!({
            "sampled": sampled,
            "sample_from": rows.first().and_then(|r| r.get("iso").cloned()).unwrap_or(Value::Null),
            "sample_capped": false,
            "success": success,
            "errors": errors,
            "client_aborted": aborted,
            "success_rate_pct": if sampled > 0 { json!((success as f64 * 100.0 / sampled as f64).round()) } else { Value::Null },
            "ttft_ms": avg(&ttft),
            "generation_ms": avg(&gen),
            "wall_ms": avg(&wall),
            "tokens_per_sec": Value::Null,
            "cache_hit_pct": Value::Null,
            "by_model": {},
            "by_model_realm": {},
            "by_model_acct": {},
        }),
    )
}

fn usage_by_account(app: &std::sync::Arc<App>) -> Resp {
    let rows = usage_rows(app);
    let mut map: std::collections::BTreeMap<String, Value> = Default::default();
    for row in &rows {
        let Some(account) = row.get("account").and_then(Value::as_str) else { continue };
        let entry = map.entry(account.to_string()).or_insert_with(|| json!({
            "requests": 0, "total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0,
        }));
        if let Some(obj) = entry.as_object_mut() {
            for key in ["requests", "total_tokens", "prompt_tokens", "completion_tokens"] {
                let add = if key == "requests" {
                    1
                } else {
                    row.get(key).and_then(Value::as_i64).unwrap_or(0)
                };
                let current = obj.get(key).and_then(Value::as_i64).unwrap_or(0);
                obj.insert(key.into(), json!(current + add));
            }
        }
    }
    // 面板要的是**数组**（`(byAcct.accounts||[]).forEach(a => USAGE_BY_ACCOUNT[a.account]=a)`），
    // 返回 map 会在前端抛 TypeError，而那行被 try/catch 包着，表现就是「账号列表 0/0」。
    let accounts: Vec<Value> = map
        .into_iter()
        .map(|(account, stats)| {
            let mut obj = match stats {
                Value::Object(o) => o,
                _ => Map::new(),
            };
            obj.insert("account".into(), json!(account));
            Value::Object(obj)
        })
        .collect();
    json_resp(StatusCode::OK, &json!({"accounts": accounts}))
}

/// `/usage/recent`：最近请求表（`{total,page,limit,total_pages,usd_cny,rows}`）。
fn usage_recent(app: &std::sync::Arc<App>, req: &Request<Incoming>) -> Resp {
    let query = req.uri().query().unwrap_or("");
    let mut limit = 20usize;
    let mut page = 1usize;
    for pair in query.split('&') {
        if let Some(v) = pair.strip_prefix("limit=") {
            limit = v.parse().unwrap_or(20).clamp(1, 500);
        }
        if let Some(v) = pair.strip_prefix("page=") {
            page = v.parse().unwrap_or(1).max(1);
        }
    }
    let mut rows = usage_rows(app);
    rows.reverse();
    let total = rows.len();
    let start = (page - 1) * limit;
    let slice: Vec<Value> = rows.into_iter().skip(start).take(limit).collect();
    json_resp(
        StatusCode::OK,
        &json!({
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": if total == 0 { 1 } else { (total + limit - 1) / limit },
            "usd_cny": 7.1,
            "rows": slice,
        }),
    )
}

/// 一段时间的汇总（`/usage/analytics` 的 summary.window 与 all_time 用同一套键）。
fn bucket_of(rows: &[&Value]) -> Value {
    let sum = |key: &str| -> i64 { rows.iter().map(|r| r.get(key).and_then(|v| v.as_i64()).unwrap_or(0)).sum() };
    let requests = rows.len() as i64;
    let errors = rows.iter().filter(|r| r.get("outcome").and_then(Value::as_str) != Some("completed")).count() as i64;
    let prompt = sum("prompt_tokens");
    let completion = sum("completion_tokens");
    let total = sum("total_tokens");
    let cached = sum("cached_tokens");
    let ttft_n = rows.iter().filter(|r| r.get("ttft_ms").is_some()).count() as i64;
    let ttft_sum: i64 = rows.iter().filter_map(|r| r.get("ttft_ms").and_then(|v| v.as_i64())).sum();
    let elapsed_n = rows.iter().filter(|r| r.get("elapsed_ms").is_some()).count() as i64;
    let elapsed_sum: i64 = rows.iter().filter_map(|r| r.get("elapsed_ms").and_then(|v| v.as_i64())).sum();
    let speed_n = rows.iter().filter(|r| r.get("tokens_per_sec").is_some()).count() as i64;
    let speed_sum: f64 = rows.iter().filter_map(|r| r.get("tokens_per_sec").and_then(|v| v.as_f64())).sum();
    json!({
        "requests": requests,
        "errors": errors,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "reasoning_tokens": sum("reasoning_tokens"),
        "cached_tokens": cached,
        "total_tokens": total,
        "credit": 0.0,
        "cost_cny": 0.0,
        "ttft_sum": ttft_sum as f64,
        "ttft_n": ttft_n,
        "speed_sum": speed_sum,
        "speed_n": speed_n,
        "elapsed_sum": elapsed_sum as f64,
        "elapsed_n": elapsed_n,
        "cache_hit_pct": if total > 0 { (cached as f64 * 100.0 / total as f64 * 10.0).round() / 10.0 } else { 0.0 },
        "reasoning_ratio": 0.0,
        "ttft_ms_avg": if ttft_n > 0 { ttft_sum / ttft_n } else { 0 },
        "speed_avg": if speed_n > 0 { (speed_sum / speed_n as f64 * 10.0).round() / 10.0 } else { 0.0 },
        "elapsed_ms_avg": if elapsed_n > 0 { elapsed_sum / elapsed_n } else { 0 },
    })
}

/// `/usage/analytics`：summary + 按账号/模型/Key 拆分。
fn usage_analytics(app: &std::sync::Arc<App>, req: &Request<Incoming>) -> Resp {
    let query = req.uri().query().unwrap_or("");
    let realm_filter: Option<&str> = query
        .split('&')
        .find_map(|p| p.strip_prefix("realm="))
        .filter(|v| !v.is_empty());
    let all = usage_rows(app);
    let until = crate::state::now_epoch() as f64;
    let since = until - 24.0 * 3600.0;
    let pick = |rows: &[Value], from: f64| -> Vec<Value> {
        rows.iter()
            .filter(|r| r.get("at").and_then(|v| v.as_f64()).unwrap_or(0.0) >= from)
            .filter(|r| match realm_filter {
                Some(want) => r.get("realm").and_then(Value::as_str) == Some(want),
                None => true,
            })
            .cloned()
            .collect()
    };
    let window_rows = pick(&all, since);
    let all_rows = pick(&all, 0.0);
    let win_refs: Vec<&Value> = window_rows.iter().collect();
    let all_refs: Vec<&Value> = all_rows.iter().collect();

    let accounts_meta = app.state.accounts_snapshot();
    let mut accounts_out = Vec::new();
    for account in &accounts_meta {
        let uid = account.uid();
        let mine: Vec<&Value> = win_refs
            .iter()
            .copied()
            .filter(|r| r.get("account").and_then(Value::as_str) == Some(uid.as_str()))
            .collect();
        let mine_all: Vec<&Value> = all_refs
            .iter()
            .copied()
            .filter(|r| r.get("account").and_then(Value::as_str) == Some(uid.as_str()))
            .collect();
        accounts_out.push(json!({
            "uid": uid,
            "nickname": account.data.get("nickname").cloned().unwrap_or(Value::Null),
            "realm": account.realm(),
            "domain": account.domain(),
            "credits": account.data.get("credits").cloned().unwrap_or(Value::Null),
            "window": bucket_of(&mine),
            "all_time": bucket_of(&mine_all),
            "window_models": [],
            "all_models": [],
        }));
    }

    let settings = app.state.settings_snapshot();
    let mut keys_out = Vec::new();
    if let Some(keys) = settings.get("api_keys").and_then(Value::as_array) {
        for key in keys {
            let id = key.get("id").and_then(Value::as_str).unwrap_or("");
            let short: String = id.chars().take(12).collect();
            let mine: Vec<&Value> = win_refs
                .iter()
                .copied()
                .filter(|r| r.get("key").and_then(Value::as_str) == Some(id))
                .collect();
            let mine_all: Vec<&Value> = all_refs
                .iter()
                .copied()
                .filter(|r| r.get("key").and_then(Value::as_str) == Some(id))
                .collect();
            keys_out.push(json!({
                "key": short,
                "name": key.get("name").cloned().unwrap_or(Value::Null),
                "realm": key.get("realm").and_then(Value::as_str).unwrap_or(""),
                "enabled": key.get("enabled").and_then(Value::as_bool).unwrap_or(true),
                "source": key.get("source").and_then(Value::as_str).unwrap_or("panel"),
                "window": bucket_of(&mine),
                "all_time": bucket_of(&mine_all),
            }));
        }
    }

    json_resp(
        StatusCode::OK,
        &json!({
            "window": {"since": since, "until": until},
            "realm": realm_filter.unwrap_or(""),
            "usd_cny": 7.1,
            "summary": {"window": bucket_of(&win_refs), "all_time": bucket_of(&all_refs)},
            "accounts": accounts_out,
            "models": [],
            "keys": keys_out,
        }),
    )
}

/// `/accounts/credits`：账号档 + `credits`（积分形状与 panel 一致，值来自账号文件）。
fn accounts_credits(app: &std::sync::Arc<App>, req: &Request<Incoming>) -> Resp {
    let query = req.uri().query().unwrap_or("");
    let realm_filter: Option<&str> = query.split('&').find_map(|p| p.strip_prefix("realm="));
    let now = crate::state::now_epoch();
    let mut list = Vec::new();
    for account in app.state.accounts_snapshot() {
        let realm = account.realm();
        if let Some(want) = realm_filter {
            if !want.is_empty() && want != realm {
                continue;
            }
        }
        let mut view = account_view(&account, now);
        let credits = account.data.get("credits").cloned().unwrap_or(Value::Null);
        if let Some(obj) = view.as_object_mut() {
            obj.insert("credits".into(), credits);
        }
        list.push(view);
    }
    json_resp(StatusCode::OK, &json!({"accounts": list, "busy": false}))
}

/// 找到 uid 对应的账号文件并改一个字段，返回改完的公开视图。
fn mutate_account(
    app: &std::sync::Arc<App>,
    uid: &str,
    apply: impl FnOnce(&mut Map<String, Value>),
) -> Option<Value> {
    let account = app.state.accounts_snapshot().into_iter().find(|a| a.uid() == uid)?;
    let mut data = account.data.clone();
    apply(&mut data);
    let value = Value::Object(data);
    if crate::state::write_json_atomic(&account.path, &value).is_err() {
        return None;
    }
    app.state.reload();
    let fresh = account_view(&account_with(app, uid)?, crate::state::now_epoch());
    Some(fresh)
}

fn account_with(app: &std::sync::Arc<App>, uid: &str) -> Option<crate::state::AccountFile> {
    app.state.accounts_snapshot().into_iter().find(|a| a.uid() == uid)
}

/// `POST /accounts/set`：逐字段生效（`wb_proxy.py:_route_accounts_set`）。
async fn accounts_set(app: &std::sync::Arc<App>, req: Request<Incoming>) -> Resp {
    let body = match crate::http::body_json(req).await {
        Ok(v) => v,
        Err(resp) => return resp,
    };
    let Some(uid) = body.get("uid").and_then(Value::as_str) else {
        return json_resp(StatusCode::BAD_REQUEST, &json!({"error": {"message": "uid required", "code": 400}}));
    };
    let mut updated: Option<Value> = None;
    let fields = [
        ("proxySlot", "proxySlot"),
        ("proxy", "proxy"),
        ("enabled", "enabled"),
        ("tasksEnabled", "tasksEnabled"),
        ("note", "note"),
        ("disabledModels", "disabledModels"),
    ];
    for (key, stored) in fields {
        if let Some(value) = body.get(key) {
            updated = mutate_account(app, uid, |data| {
                data.insert(stored.to_string(), value.clone());
            });
            if updated.is_none() {
                return json_resp(StatusCode::NOT_FOUND, &json!({"error": {"message": "no such account", "code": 404}}));
            }
        }
    }
    if let Some(weight) = body.get("weight") {
        let weight = weight.as_i64().unwrap_or(0);
        if !(1..=100).contains(&weight) {
            return json_resp(StatusCode::BAD_REQUEST, &json!({"error": {"message": "weight out of range (1-100)", "code": 400}}));
        }
        updated = mutate_account(app, uid, |data| {
            data.insert("weight".into(), json!(weight));
        });
    }
    match updated {
        Some(account) => {
            app.log("accounts", "INFO", &format!("账号 {} 已更新 {}", &uid[..uid.len().min(8)], body));
            json_resp(StatusCode::OK, &json!({"account": account}))
        }
        None => json_resp(
            StatusCode::BAD_REQUEST,
            &json!({"error": {"message": "nothing to update: pass 'enabled', 'tasksEnabled', 'proxy', 'proxySlot', 'weight' or 'note'", "code": 400}}),
        ),
    }
}

/// `POST /accounts/set-all`：整池启用/停用。
async fn accounts_set_all(app: &std::sync::Arc<App>, req: Request<Incoming>) -> Resp {
    let body = match crate::http::body_json(req).await {
        Ok(v) => v,
        Err(resp) => return resp,
    };
    let enabled = body.get("enabled").and_then(Value::as_bool).unwrap_or(false);
    let realm = body.get("realm").and_then(Value::as_str).unwrap_or("");
    let uids: Vec<String> = app
        .state
        .accounts_snapshot()
        .into_iter()
        .filter(|a| realm.is_empty() || realm == "all" || a.realm() == realm)
        .map(|a| a.uid())
        .collect();
    for uid in &uids {
        mutate_account(app, uid, |data| {
            data.insert("enabled".into(), json!(enabled));
        });
    }
    json_resp(StatusCode::OK, &json!({"ok": true, "changed": uids.len()}))
}

/// `POST /accounts/delete`：删除账号文件。
async fn accounts_delete(app: &std::sync::Arc<App>, req: Request<Incoming>) -> Resp {
    let body = match crate::http::body_json(req).await {
        Ok(v) => v,
        Err(resp) => return resp,
    };
    let Some(uid) = body.get("uid").and_then(Value::as_str) else {
        return json_resp(StatusCode::BAD_REQUEST, &json!({"error": {"message": "uid required", "code": 400}}));
    };
    let Some(account) = account_with(app, uid) else {
        return json_resp(StatusCode::NOT_FOUND, &json!({"error": {"message": "no such account", "code": 404}}));
    };
    let removed = std::fs::remove_file(&account.path).is_ok();
    app.log("accounts", "WARN", &format!("账号 {} 已删除", &uid[..uid.len().min(8)]));
    app.state.reload();
    let now = crate::state::now_epoch();
    let list: Vec<Value> = app.state.accounts_snapshot().iter().map(|a| account_view(a, now)).collect();
    json_resp(StatusCode::OK, &json!({"deleted": removed, "accounts": list}))
}

/// `GET /accounts/export`：自描述导出文档（`wb_accounts.build_export_document`）。
fn accounts_export(app: &std::sync::Arc<App>, req: &Request<Incoming>) -> Resp {
    let query = req.uri().query().unwrap_or("");
    let realm = query
        .split('&')
        .find_map(|p| p.strip_prefix("realm="))
        .filter(|v| *v == "cn" || *v == "intl");
    let secrets = !query.split('&').any(|p| p == "secrets=0" || p == "secrets=false");
    let uids: Vec<String> = query
        .split('&')
        .filter_map(|p| p.strip_prefix("uid="))
        .flat_map(|v| v.split(',').map(|s| s.trim().to_string()))
        .filter(|s| !s.is_empty())
        .collect();
    let mut rows = Vec::new();
    for account in app.state.accounts_snapshot() {
        if let Some(want) = realm {
            if account.realm() != want {
                continue;
            }
        }
        if !uids.is_empty() && !uids.contains(&account.uid()) {
            continue;
        }
        let mut row = Value::Object(account.data.clone());
        if !secrets {
            if let Some(obj) = row.as_object_mut() {
                obj.remove("accessToken");
                obj.remove("refreshToken");
            }
        }
        rows.push(row);
    }
    json_resp(
        StatusCode::OK,
        &json!({
            "format": "workbuddy-accounts",
            "version": 1,
            "exportedAt": crate::state::local_stamp(),
            "count": rows.len(),
            "accounts": rows,
        }),
    )
}

/// `POST /settings/save`：只接受面板管理的键；未知键原样忽略（不写脏数据）。
async fn settings_save(app: &std::sync::Arc<App>, req: Request<Incoming>) -> Resp {
    let body = match crate::http::body_json(req).await {
        Ok(v) => v,
        Err(resp) => return resp,
    };
    let Some(patch) = body.as_object() else {
        return json_resp(StatusCode::BAD_REQUEST, &json!({"error": {"message": "body must be an object", "code": 400}}));
    };
    let mut settings = app.state.settings_snapshot();
    let mut applied = Vec::new();
    for (key, value) in patch {
        // 面板是 settings.json 的唯一写者；这里只认「已有键」和几个新键，
        // 免得把前端随手带的字段写进配置文件。
        let known = settings.contains_key(key)
            || matches!(
                key.as_str(),
                "api_keys" | "deleted_api_key_ids" | "panel_password" | "active_realm"
            );
        if !known {
            continue;
        }
        settings.insert(key.clone(), value.clone());
        applied.push(key.clone());
    }
    let path = app.state.accounts_dir.join("settings.json");
    let value = Value::Object(settings);
    match crate::state::write_json_atomic(&path, &value) {
        Ok(()) => {
            app.state.reload();
            app.log("settings", "INFO", &format!("设置已保存: {}", applied.join(", ")));
            json_resp(StatusCode::OK, &json!({"ok": true, "applied": applied}))
        }
        Err(e) => json_resp(
            StatusCode::INTERNAL_SERVER_ERROR,
            &json!({"error": {"message": format!("cannot save settings: {e}"), "code": 500}}),
        ),
    }
}

/// `POST /realm`：切默认出口（写 active_realm.json）。
async fn realm_set(app: &std::sync::Arc<App>, req: Request<Incoming>) -> Resp {
    let body = match crate::http::body_json(req).await {
        Ok(v) => v,
        Err(resp) => return resp,
    };
    let realm = body.get("realm").and_then(Value::as_str).unwrap_or("");
    if realm != "cn" && realm != "intl" {
        return json_resp(StatusCode::BAD_REQUEST, &json!({"error": {"message": "realm must be cn or intl", "code": 400}}));
    }
    let path = app.state.accounts_dir.join("active_realm.json");
    let _ = crate::state::write_json_atomic(&path, &json!({"realm": realm}));
    app.state.reload();
    json_resp(StatusCode::OK, &json!({"ok": true, "current": realm, "options": ["intl", "cn"]}))
}

/// `POST /accounts/product`：切换出站身分（cli / workbuddy / vscode）。
/// 官方两条配额线，某条满了可以换另一条；本机只改文件，不等别的路径顺手存档。
async fn accounts_product(app: &std::sync::Arc<App>, req: Request<Incoming>) -> Resp {
    let body = match crate::http::body_json(req).await {
        Ok(v) => v,
        Err(resp) => return resp,
    };
    let product = body.get("product").and_then(Value::as_str).unwrap_or("").trim().to_lowercase();
    if !matches!(product.as_str(), "workbuddy" | "vscode" | "cli") {
        return json_resp(
            StatusCode::BAD_REQUEST,
            &json!({"error": {"message": "product must be 'workbuddy', 'vscode', or 'cli'", "code": 400}}),
        );
    }
    let uid = body.get("uid").and_then(Value::as_str).unwrap_or("");
    let realm = body.get("realm").and_then(Value::as_str).unwrap_or("");
    let targets: Vec<String> = app
        .state
        .accounts_snapshot()
        .into_iter()
        .filter(|a| {
            if !uid.is_empty() {
                a.uid() == uid
            } else if !realm.is_empty() && realm != "all" {
                a.realm() == realm
            } else {
                true
            }
        })
        .map(|a| a.uid())
        .collect();
    let mut changed = Vec::new();
    for target in &targets {
        if mutate_account(app, target, |data| {
            data.insert("product".into(), json!(product));
        })
        .is_some()
        {
            changed.push(target.chars().take(8).collect::<String>());
        }
    }
    let now = crate::state::now_epoch();
    let accounts: Vec<Value> = app.state.accounts_snapshot().iter().map(|a| account_view(a, now)).collect();
    json_resp(StatusCode::OK, &json!({"ok": true, "product": product, "changed": changed, "accounts": accounts}))
}

/// `POST /accounts/credits`：打上游刷新积分（uid / realm / 整池）。
async fn accounts_credits_fetch(app: &std::sync::Arc<App>, req: Request<Incoming>) -> Resp {
    let body = match crate::http::body_json(req).await {
        Ok(v) => v,
        Err(resp) => return resp,
    };
    let uid = body.get("uid").and_then(Value::as_str).unwrap_or("");
    let realm = body.get("realm").and_then(Value::as_str).unwrap_or("");
    let out = crate::credits::sweep(app, uid, realm).await;
    app.log("credits", "INFO", &format!("刷新积分 uid={uid} realm={realm}"));
    let ok = out
        .get("results")
        .and_then(Value::as_array)
        .map(|r| r.iter().filter(|x| x.get("ok").and_then(Value::as_bool).unwrap_or(false)).count())
        .unwrap_or(0);
    let mut obj = match out {
        Value::Object(o) => o,
        _ => Map::new(),
    };
    obj.insert("refreshed".into(), json!(ok));
    json_resp(StatusCode::OK, &Value::Object(obj))
}

/// `POST /tasks/run`：跑一轮成长任务（接取 → 点亮 → 领奖）。
async fn tasks_run(app: &std::sync::Arc<App>, req: Request<Incoming>) -> Resp {
    let body = crate::http::body_json(req).await.unwrap_or(json!({}));
    let uid = body.get("uid").and_then(Value::as_str).unwrap_or("");
    let claim = body.get("claim").and_then(Value::as_bool).unwrap_or(true);
    let out = crate::tasks::run(app, uid, claim).await;
    app.log("tasks", "INFO", &format!("成长任务执行完毕，新增 +{} 积分", out.get("credit_added").and_then(Value::as_i64).unwrap_or(0)));
    json_resp(StatusCode::OK, &out)
}

/// `POST /tasks/claim`：单独领一个任务的奖励。
async fn tasks_claim(app: &std::sync::Arc<App>, req: Request<Incoming>) -> Resp {
    let body = match crate::http::body_json(req).await {
        Ok(v) => v,
        Err(resp) => return resp,
    };
    let uid = body.get("uid").and_then(Value::as_str).unwrap_or("");
    let code = body.get("code").and_then(Value::as_str).unwrap_or("");
    let Some(account) = app.state.accounts_snapshot().into_iter().find(|a| a.uid() == uid) else {
        return json_resp(StatusCode::NOT_FOUND, &json!({"ok": false, "error": "未找到指定账号"}));
    };
    let out = crate::tasks::claim_one(app, &account, code).await;
    json_resp(StatusCode::OK, &out)
}

/// `POST /accounts/checkin`：国内版每日签到。
async fn checkin(app: &std::sync::Arc<App>, req: Request<Incoming>) -> Resp {
    let body = crate::http::body_json(req).await.unwrap_or(json!({}));
    let uid = body.get("uid").and_then(Value::as_str).unwrap_or("");
    let out = crate::credits::checkin_all(app, uid).await;
    let ok = out.get("results").and_then(Value::as_array)
        .map(|r| r.iter().filter(|x| x.get("ok").and_then(Value::as_bool).unwrap_or(false)).count())
        .unwrap_or(0);
    app.log("tasks", "INFO", &format!("签到完成 {ok} 个账号"));
    json_resp(StatusCode::OK, &out)
}

/// `POST /accounts/import`：导入导出文档 / 裸数组 / 单个账号对象。
///
/// 选项与 Python 版一致：`data`（把文档包一层，好跟选项分开放）、`dryRun`（只报
/// 结果不写盘）、`overwrite`（uid 已存在就替换）、`realm`（强制区域）。
///
/// shortcut: 桌面客户端凭据扫描与 at-rest 解密没移植。
async fn accounts_import(app: &std::sync::Arc<App>, req: Request<Incoming>) -> Resp {
    let body = match crate::http::body_json(req).await {
        Ok(v) => v,
        Err(resp) => return resp,
    };
    let doc = body.get("data").unwrap_or(&body);
    let dry_run = body.get("dryRun").and_then(Value::as_bool).unwrap_or(false);
    let overwrite = body.get("overwrite").and_then(Value::as_bool).unwrap_or(false);
    let forced = body.get("realm").and_then(Value::as_str).unwrap_or("").to_lowercase();
    if !forced.is_empty() && forced != "cn" && forced != "intl" {
        return json_resp(StatusCode::BAD_REQUEST, &json!({"error": {"message": "realm must be intl or cn", "code": 400}}));
    }
    let rows: Vec<Value> = match doc {
        Value::Array(list) => list.clone(),
        Value::Object(obj) => match obj.get("accounts").and_then(Value::as_array) {
            Some(list) => list.clone(),
            None if obj.contains_key("accessToken") || obj.contains_key("access_token") => vec![doc.clone()],
            None => {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    &json!({"error": {"message": "not an account document: expected an accounts array, a list, or an account object", "code": 400}}),
                )
            }
        },
        _ => {
            return json_resp(
                StatusCode::BAD_REQUEST,
                &json!({"error": {"message": "the document must be a JSON object or array", "code": 400}}),
            )
        }
    };
    let mut imported = Vec::new();
    let mut skipped = Vec::new();
    let mut overwritten = Vec::new();
    let mut failed = Vec::new();
    for row in rows {
        let Some(obj) = row.as_object() else {
            failed.push(json!({"error": "account row is not an object"}));
            continue;
        };
        let token = obj
            .get("accessToken")
            .or_else(|| obj.get("access_token"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        if token.is_empty() {
            failed.push(json!({"error": "row has no accessToken"}));
            continue;
        }
        let uid = obj
            .get("uid")
            .and_then(Value::as_str)
            .map(|s| s.to_string())
            .or_else(|| crate::state::jwt_sub(&token))
            .unwrap_or_default();
        if uid.is_empty() {
            failed.push(json!({"error": "row has no uid and the token carries no sub"}));
            continue;
        }
        let path = app.state.accounts_dir.join(format!("{uid}.json"));
        let exists = path.exists();
        if exists && !overwrite {
            skipped.push(json!(uid));
            continue;
        }
        if dry_run {
            if exists {
                overwritten.push(json!(uid));
            } else {
                imported.push(json!(uid));
            }
            continue;
        }
        let mut data = obj.clone();
        data.insert("uid".into(), json!(uid));
        data.insert("accessToken".into(), json!(token));
        if !forced.is_empty() {
            data.insert("realm".into(), json!(forced));
        }
        data.entry("enabled").or_insert(json!(true));
        data.entry("tasksEnabled").or_insert(json!(true));
        match crate::state::write_json_atomic(&path, &Value::Object(data)) {
            Ok(()) => {
                if exists {
                    overwritten.push(json!(uid));
                } else {
                    imported.push(json!(uid));
                }
            }
            Err(e) => failed.push(json!({"uid": uid, "error": e.to_string()})),
        }
    }
    if dry_run {
        let now = crate::state::now_epoch();
        let accounts: Vec<Value> = app.state.accounts_snapshot().iter().map(|a| account_view(a, now)).collect();
        return json_resp(
            StatusCode::OK,
            &json!({"dryRun": true, "imported": imported, "overwritten": overwritten,
                    "skipped": skipped, "failed": failed, "accounts": accounts}),
        );
    }
    app.state.reload();
    let now = crate::state::now_epoch();
    let accounts: Vec<Value> = app.state.accounts_snapshot().iter().map(|a| account_view(a, now)).collect();
    app.log(
        "accounts",
        "INFO",
        &format!(
            "导入完成: 新增 {} 覆盖 {} 跳过 {} 失败 {}",
            imported.len(), overwritten.len(), skipped.len(), failed.len()
        ),
    );
    json_resp(
        StatusCode::OK,
        &json!({"imported": imported, "overwritten": overwritten, "skipped": skipped,
                "failed": failed, "accounts": accounts}),
    )
}

/// `POST /accounts/add-api-key`：先拿企业额度端点探一次，探通了才落盘。
async fn add_api_key(app: &std::sync::Arc<App>, req: Request<Incoming>) -> Resp {
    let body = match crate::http::body_json(req).await {
        Ok(v) => v,
        Err(resp) => return resp,
    };
    let key = body
        .get("key")
        .or_else(|| body.get("accessToken"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    if key.is_empty() {
        return json_resp(StatusCode::BAD_REQUEST, &json!({"error": {"message": "key is required", "code": 400}}));
    }
    if !crate::credits::is_api_key_token(&key) {
        return json_resp(
            StatusCode::BAD_REQUEST,
            &json!({"error": {"message": "this is not a static API key: expected the ck_/pt_ prefix issued by the CodeBuddy API console", "code": 400}}),
        );
    }
    let probe = crate::credits::probe_api_key(app, &key).await;
    if probe.get("ok").and_then(Value::as_bool) != Some(true) {
        app.log("accounts", "WARN", &format!("API Key 探测失败，未落盘: {}", probe.get("error").and_then(Value::as_str).unwrap_or("")));
        return json_resp(
            StatusCode::BAD_REQUEST,
            &json!({"error": {"message": format!("probe failed: {}", probe.get("error").and_then(Value::as_str).unwrap_or("unknown")), "code": 400}}),
        );
    }
    let uid = body
        .get("uid")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .unwrap_or_else(|| crate::credits::api_key_id(&key));
    if uid.is_empty() {
        return json_resp(StatusCode::BAD_REQUEST, &json!({"error": {"message": "cannot derive uid from this key", "code": 400}}));
    }
    let path = app.state.accounts_dir.join(format!("{uid}.json"));
    let existed = path.exists();
    let nickname = body.get("nickname").and_then(Value::as_str).unwrap_or("").to_string();
    let account = json!({
        "uid": uid,
        "nickname": nickname,
        "accessToken": key,
        "realm": "cn",
        "platform": "CLI",
        "source": "api-key",
        "enabled": true,
        "tasksEnabled": false,
    });
    if let Err(e) = crate::state::write_json_atomic(&path, &account) {
        return json_resp(
            StatusCode::INTERNAL_SERVER_ERROR,
            &json!({"error": {"message": format!("cannot save account: {e}"), "code": 500}}),
        );
    }
    app.state.reload();
    app.log("accounts", "INFO", &format!("加 API Key: uid={} probe ok({}/{})",
        &uid[..uid.len().min(8)],
        probe.get("used").and_then(Value::as_i64).unwrap_or(0),
        probe.get("limit").and_then(Value::as_i64).unwrap_or(0)));
    let view = app
        .state
        .accounts_snapshot()
        .into_iter()
        .find(|a| a.uid() == uid)
        .map(|a| account_view(&a, crate::state::now_epoch()))
        .unwrap_or(Value::Null);
    json_resp(
        StatusCode::OK,
        &json!({"ok": true, "uid": uid, "nickname": nickname, "updated": existed, "probe": probe, "account": view}),
    )
}

/// `POST /tasks/travel`：猫猫旅行（到站领奖 / 在家派出）。
async fn tasks_travel(app: &std::sync::Arc<App>, req: Request<Incoming>) -> Resp {
    let body = crate::http::body_json(req).await.unwrap_or(json!({}));
    let uid = body.get("uid").and_then(Value::as_str).unwrap_or("");
    let out = crate::tasks::travel(app, uid).await;
    app.log("tasks", "INFO", &format!("猫猫旅行: {}", out.get("msg").and_then(Value::as_str).unwrap_or("")));
    json_resp(StatusCode::OK, &out)
}

/// 积分包快照摊平成一张表（`credit_grants`）：只读账号档，不发上游请求。
///
/// shortcut: 行里的 `action`（这笔发放对应本机哪次签到/活跃）恒为 null——活动历史
/// 表还没移植；其余字段口径与 Python 一致。
fn credit_grants(app: &App) -> Value {
    let now = crate::state::now_epoch() as f64;
    let mut rows: Vec<Value> = Vec::new();
    let mut fetched = 0f64;
    for account in app.state.accounts_snapshot() {
        let credits = account.data.get("credits").cloned().unwrap_or(json!({}));
        if let Some(v) = credits.get("updated_at").and_then(Value::as_f64) {
            fetched = fetched.max(v);
        }
        let packages = credits.get("packages").and_then(Value::as_array).cloned().unwrap_or_default();
        for pkg in packages.iter().filter(|p| p.is_object()) {
            let num = |key: &str| pkg.get(key).and_then(Value::as_f64).unwrap_or(0.0);
            let expire_iso = pkg
                .get("expire_time")
                .or_else(|| pkg.get("cycle_end_time"))
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let create_iso = pkg.get("create_time").and_then(Value::as_str).unwrap_or("").to_string();
            let create_at = stamp_epoch(&create_iso);
            let expire_at = stamp_epoch(&expire_iso);
            let remain = num("remain");
            let expired = pkg.get("is_expired").and_then(Value::as_bool).unwrap_or(false)
                || expire_at.map(|t| t <= now).unwrap_or(false);
            let status = if expired {
                "expired"
            } else if remain <= 0.0 {
                "used_up"
            } else if pkg.get("in_usage").and_then(Value::as_bool).unwrap_or(false) {
                "active"
            } else {
                "available"
            };
            let text = |key: &str| pkg.get(key).and_then(Value::as_str).map(String::from);
            rows.push(json!({
                "uid": account.uid(),
                "nickname": if account.str("nickname").is_empty() {
                    account.uid().chars().take(8).collect::<String>()
                } else { account.str("nickname").to_string() },
                "realm": account.realm(),
                "name": text("name").or_else(|| text("package_code")).unwrap_or_else(|| "Package".into()),
                "product": text("sub_product_name").or_else(|| text("product_name")).unwrap_or_default(),
                "grant_reason": text("grant_reason").unwrap_or_default(),
                "size": num("size"),
                "remain": remain,
                "used": num("used"),
                "unit": text("unit").unwrap_or_else(|| "credit".into()),
                "create_at": create_at,
                "create_iso": create_iso,
                "expire_at": expire_at,
                "expire_iso": expire_iso,
                "no_expiry": pkg.get("no_expiry").and_then(Value::as_bool).unwrap_or(false),
                "days_left": pkg.get("days_left").cloned().unwrap_or(Value::Null),
                "status": status,
                "action": Value::Null,
            }));
        }
    }
    // 新的在前；同一时刻按账号、包名稳定排序，避免每次刷新顺序跳。
    rows.sort_by(|a, b| {
        let key = |r: &Value| {
            (
                r.get("create_at").and_then(Value::as_f64).unwrap_or(0.0) as i64,
                r.get("nickname").and_then(Value::as_str).unwrap_or("").to_string(),
                r.get("name").and_then(Value::as_str).unwrap_or("").to_string(),
            )
        };
        let (ta, na, pa) = key(a);
        let (tb, nb, pb) = key(b);
        tb.cmp(&ta).then(na.cmp(&nb)).then(pa.cmp(&pb))
    });
    let sum = |field: &str| rows.iter().map(|r| r.get(field).and_then(Value::as_f64).unwrap_or(0.0)).sum::<f64>();
    let accounts: std::collections::BTreeSet<String> = rows
        .iter()
        .filter_map(|r| r.get("uid").and_then(Value::as_str).map(String::from))
        .collect();
    json!({
        "ok": true,
        "generated_at": now,
        "generated_iso": crate::state::local_stamp(),
        "fetched_at": if fetched > 0.0 { json!(fetched) } else { Value::Null },
        "fetched_iso": if fetched > 0.0 { json!(format_stamp(fetched as i64)) } else { Value::Null },
        "rows": rows,
        "summary": {
            "count": rows.len(),
            "size": (sum("size") * 100.0).round() / 100.0,
            "remain": (sum("remain") * 100.0).round() / 100.0,
            "used": (sum("used") * 100.0).round() / 100.0,
            "accounts": accounts.len(),
        },
    })
}

/// `'2026-10-10 00:42:45'` → epoch（本地时区）；认不出来回 None。
fn stamp_epoch(text: &str) -> Option<f64> {
    if text.len() < 19 {
        return None;
    }
    let tm = libc::tm {
        tm_sec: text.get(17..19)?.parse().ok()?,
        tm_min: text.get(14..16)?.parse().ok()?,
        tm_hour: text.get(11..13)?.parse().ok()?,
        tm_mday: text.get(8..10)?.parse().ok()?,
        tm_mon: text.get(5..7)?.parse::<i32>().ok()? - 1,
        tm_year: text.get(..4)?.parse::<i32>().ok()? - 1900,
        tm_wday: 0,
        tm_yday: 0,
        tm_isdst: -1,
        tm_gmtoff: 0,
        tm_zone: std::ptr::null(),
    };
    let epoch = unsafe { libc::mktime(&tm as *const libc::tm as *mut libc::tm) };
    if epoch < 0 {
        None
    } else {
        Some(epoch as f64)
    }
}

fn format_stamp(epoch: i64) -> String {
    let tm = unsafe { libc::localtime(&epoch as *const i64) };
    if tm.is_null() {
        return String::new();
    }
    let tm = unsafe { *tm };
    format!(
        "{:04}-{:02}-{:02} {:02}:{:02}:{:02}",
        tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec
    )
}
