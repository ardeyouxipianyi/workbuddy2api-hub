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

/// 面板登录限流：每客户端 IP 60 秒内 5 次失败（`wb_proxy.py` 的 `_login_attempts`）。
const LOGIN_WINDOW: i64 = 60;
const LOGIN_MAX: usize = 5;

#[derive(Default)]
pub struct Panel {
    tokens: Mutex<HashSet<String>>,
    password: Mutex<Option<String>>,
    /// ip -> 最近失败时刻（秒）。只在内存里，重启清空。
    attempts: Mutex<std::collections::HashMap<String, Vec<i64>>>,
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

    /// 返回到期等待秒数；`None` 表示还能试。
    fn login_blocked(&self, ip: &str, now: i64) -> Option<i64> {
        let mut map = self.attempts.lock().ok()?;
        let kept: Vec<i64> = map
            .get(ip)
            .map(|list| list.iter().copied().filter(|t| now - *t < LOGIN_WINDOW).collect())
            .unwrap_or_default();
        if kept.is_empty() {
            map.remove(ip);
        } else {
            map.insert(ip.to_string(), kept.clone());
        }
        if kept.len() >= LOGIN_MAX {
            return Some((LOGIN_WINDOW - (now - kept[0])).max(1));
        }
        None
    }

    fn note_login_failure(&self, ip: &str, now: i64) {
        if let Ok(mut map) = self.attempts.lock() {
            // 键是客户端地址，挡不住「拿一大片源地址来试」；这只是内存护栏。
            if map.len() > 1024 {
                map.clear();
            }
            map.entry(ip.to_string()).or_default().push(now);
        }
    }

    fn clear_login_failures(&self, ip: &str) {
        if let Ok(mut map) = self.attempts.lock() {
            map.remove(ip);
        }
    }
}

/// 登录限流用的客户端地址：反代部署下对端是 127.0.0.1，只有对端是回环时才读代理头，
/// 否则公网客户端伪造 `X-Real-IP` 就能换个桶继续猜（`wb_proxy.py` 的 login_rate_limit_key）。
///
/// shortcut: `WB_TRUSTED_PROXIES`（CIDR 白名单）没接，只信任回环对端；面板挂在
/// 非回环的反代后面时要补这张表。
/// 请求是不是本机发起的（没有对端信息时按本机算，与 Python 的默认一致）。
fn from_loopback<B>(req: &Request<B>) -> bool {
    req.extensions()
        .get::<crate::http::PeerAddr>()
        .map(|peer| peer.0.is_loopback())
        .unwrap_or(true)
}

fn client_ip<B>(req: &Request<B>) -> String {
    let peer = req
        .extensions()
        .get::<crate::http::PeerAddr>()
        .map(|p| p.0)
        .unwrap_or(std::net::IpAddr::V4(std::net::Ipv4Addr::LOCALHOST));
    // 公网客户端不能靠伪造头换桶：只有回环或 WB_TRUSTED_PROXIES 里的对端才配代表别人说话。
    if !peer_trusted(peer) {
        return peer.to_string();
    }
    let header = |name: &str| req.headers().get(name).and_then(|v| v.to_str().ok());
    if let Some(real) = header("x-real-ip").and_then(header_ip) {
        return real;
    }
    if let Some(first) = header("x-forwarded-for").and_then(|v| v.split(',').next()).and_then(header_ip) {
        return first;
    }
    peer.to_string()
}

/// `WB_TRUSTED_PROXIES`：逗号分隔的 IP 或 CIDR（`10.0.0.0/8,fd00::/8`），解析不了就丢。
fn trusted_nets() -> &'static Vec<(std::net::IpAddr, u8)> {
    static NETS: std::sync::OnceLock<Vec<(std::net::IpAddr, u8)>> = std::sync::OnceLock::new();
    NETS.get_or_init(|| parse_trusted(&std::env::var("WB_TRUSTED_PROXIES").unwrap_or_default()))
}

fn parse_trusted(raw: &str) -> Vec<(std::net::IpAddr, u8)> {
    raw.split(',')
        .filter_map(|item| {
            let item = item.trim();
            let (addr, prefix) = match item.split_once('/') {
                Some((a, p)) => (a.trim().parse().ok()?, p.trim().parse().ok()?),
                None => {
                    let addr: std::net::IpAddr = item.parse().ok()?;
                    (addr, if addr.is_ipv4() { 32 } else { 128 })
                }
            };
            let max = if addr.is_ipv4() { 32 } else { 128 };
            (prefix <= max).then_some((addr, prefix))
        })
        .collect()
}

fn peer_trusted(peer: std::net::IpAddr) -> bool {
    // Windows 双栈下连 127.0.0.1 的对端会显示成 ::ffff:127.0.0.1，也要算本机。
    let mapped_loopback = match peer {
        std::net::IpAddr::V6(v6) => v6.to_ipv4_mapped().map(|v4| v4.is_loopback()).unwrap_or(false),
        _ => false,
    };
    if peer.is_loopback() || mapped_loopback {
        return true;
    }
    trusted_nets().iter().any(|(net, prefix)| in_net(peer, *net, *prefix))
}

fn in_net(ip: std::net::IpAddr, net: std::net::IpAddr, prefix: u8) -> bool {
    match (ip, net) {
        (std::net::IpAddr::V4(a), std::net::IpAddr::V4(b)) => {
            let mask = if prefix == 0 { 0 } else { u32::MAX << (32 - prefix as u32) };
            (u32::from(a) & mask) == (u32::from(b) & mask)
        }
        (std::net::IpAddr::V6(a), std::net::IpAddr::V6(b)) => {
            let mask = if prefix == 0 { 0 } else { u128::MAX << (128 - prefix as u32) };
            (u128::from(a) & mask) == (u128::from(b) & mask)
        }
        _ => false,
    }
}

/// 代理头里的一个 IP：接受 `1.2.3.4`、`1.2.3.4:8080`、`[::1]`、`[::1]:80`；其余一律不认。
fn header_ip(raw: &str) -> Option<String> {
    let text = raw.trim();
    if text.is_empty() || text.len() > 64 || text.contains(',') {
        return None;
    }
    let text = if let Some(rest) = text.strip_prefix('[') {
        rest.split_once(']')?.0
    } else if text.matches(':').count() == 1 {
        let (host, port) = text.split_once(':')?;
        if port.chars().all(|c| c.is_ascii_digit()) && !port.is_empty() { host } else { return None }
    } else {
        text
    };
    text.parse::<std::net::IpAddr>().ok().map(|ip| ip.to_string())
}

fn token_of(req: &Request<Incoming>) -> Option<String> {
    req.headers()
        .get("x-panel-token")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string())
}

fn unauth() -> Resp {
    // 文案与 Python 一致（wb_proxy.py 的 panel 401）：客户端只认这句。
    json_resp(
        StatusCode::UNAUTHORIZED,
        &json!({"error": {"message": "panel password required", "type": "invalid_request_error", "code": 401}}),
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
        let ip = client_ip(&req);
        let now = crate::state::now_epoch();
        if let Some(wait) = app.panel.login_blocked(&ip, now) {
            return Some(json_resp(
                StatusCode::TOO_MANY_REQUESTS,
                &json!({"error": {
                    "message": format!("too many login attempts, please wait {wait}s"),
                    "type": "rate_limit_error", "code": 429}}),
            ));
        }
        let body = crate::http::body_json(req).await.ok()?;
        let given = body.get("password").and_then(Value::as_str).unwrap_or("");
        // Python 的口令在 settings.json（PBKDF2 哈希，缺省时是默认口令）；
        // WB_PANEL_PASSWORD 是部署时的额外口令（launcher 把 --panel-password 转成它）。
        // 两边都认，切后端才不会把人锁在外面。
        let env_ok = app.panel.password_required() && given == app.panel.password();
        if env_ok || crate::panel_password::verify_app(app, given) {
            app.panel.clear_login_failures(&ip);
            let token = crate::sse::hex_token(32);
            if let Ok(mut set) = app.panel.tokens.lock() {
                set.insert(token.clone());
            }
            return Some(json_resp(
                StatusCode::OK,
                &json!({"ok": true, "token": token, "using_default_password": false}),
            ));
        }
        app.panel.note_login_failure(&ip, now);
        // 与 Python 版同样的 0.5s 退避：挡住脚本化的猜口令。
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
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

    // `/realm` 在 Python 侧不算面板路由（wb_proxy.py 的 _is_panel_route），
    // 面板首屏在登录前就会读它，所以这里保持免 token。
    if (&method, path.as_str()) == (&Method::GET, "/realm") {
        return Some(json_resp(
            StatusCode::OK,
            &json!({"current": app.state.current_realm(), "options": ["intl", "cn"]}),
        ));
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
        (&Method::GET, "/admin/backend/status") => crate::launcher::status(app).await,
        (&Method::POST, "/admin/backend") => crate::launcher::switch(app, req).await,
        (&Method::GET, "/accounts") => crate::panel_read::accounts(app, &req),
        (&Method::GET, "/settings") => crate::panel_settings::view(app),
        (&Method::GET, "/usage") => crate::panel_usage::overview(app, &req),
        (&Method::GET, "/usage/timeseries") => crate::panel_usage::timeseries(app, &req),
        (&Method::GET, "/usage/perf") => crate::panel_usage::perf(app, &req),
        (&Method::GET, "/usage/remaining") => crate::panel_usage::remaining(app),
        (&Method::GET, "/usage/by-account") => crate::panel_usage::by_account(app),
        (&Method::GET, "/usage/recent") => crate::panel_usage::recent(app, &req),
        (&Method::GET, "/usage/analytics") => crate::panel_usage::analytics(app, &req),
        (&Method::GET, "/egress") | (&Method::POST, "/egress") => crate::egress::handle(app, req).await,
        (&Method::POST, "/catalog/refresh") => crate::catalog_refresh::handle(app, req).await,
        (&Method::POST, "/panel/password") => crate::panel_password::handle(app, req).await,
        (&Method::GET, "/accounts/credits") => accounts_credits(app, &req),
        (&Method::GET, "/accounts/credits/grants") => {
            json_resp(StatusCode::OK, &credit_grants(app))
        }
        (&Method::GET, "/logs") => {
            let ring = app.logs.lock().map(|r| r.iter().cloned().collect::<Vec<_>>()).unwrap_or_default();
            json_resp(StatusCode::OK, &json!({"total": ring.len(), "logs": ring}))
        }
        (&Method::GET, "/logs/export") => crate::misc_routes::logs_export(app),
        (&Method::POST, "/shutdown") => {
            // 先回 200，再让进程退：面板点「停止服务」期待立刻看到结果。
            tokio::spawn(async {
                tokio::time::sleep(std::time::Duration::from_millis(400)).await;
                std::process::exit(0);
            });
            json_resp(StatusCode::OK, &json!({"ok": true, "shutting_down": true}))
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
        (&Method::POST, "/quota-scan/run") => quota_scan_run(app).await,
        (&Method::POST, "/accounts/daily-chat") => daily_chat(app, req).await,
        (&Method::POST, "/tasks/travel") => tasks_travel(app, req).await,
        (&Method::POST, "/tasks/claim") => tasks_claim(app, req).await,
        (&Method::POST, "/tasks/streak-bonus") => crate::streak_bonus::handle(app, req).await,
        (&Method::POST, "/settings/save") => settings_save(app, req).await,
        (&Method::POST, "/realm") => realm_set(app, req).await,
        (&Method::GET, "/pricing")
        | (&Method::POST, "/pricing/refresh")
        | (&Method::POST, "/pricing/mapping") => {
            match crate::pricing_panel::handle(app, req).await {
                Some(resp) => resp,
                None => not_ported("/pricing"),
            }
        }
        (&Method::GET, "/tasks") => crate::panel_read::tasks(app, &req).await,
        (&Method::GET, "/activity") => {
            match crate::misc_routes::activity(app, req.uri().query().unwrap_or("")) {
                Ok(payload) => json_resp(StatusCode::OK, &payload),
                Err(message) => json_resp(
                    StatusCode::BAD_REQUEST,
                    &json!({"error": {"message": message, "type": "invalid_request_error", "code": 400}}),
                ),
            }
        }
        (&Method::GET, "/activity/history") => {
            let q = req.uri().query().unwrap_or("");
            let get = |name: &str| {
                q.split('&')
                    .find_map(|p| p.strip_prefix(name).and_then(|r| r.strip_prefix('=')))
                    .unwrap_or("")
            };
            match crate::activity::query(app, get("range"), get("uid"), get("task"), get("result"), get("limit")) {
                Ok(payload) => json_resp(StatusCode::OK, &payload),
                Err(e) => json_resp(StatusCode::BAD_REQUEST, &json!({"error": {"message": e, "code": 400}})),
            }
        }
        (&Method::GET, "/agents") => json_resp(StatusCode::OK, &crate::agents::overview(app, &req)),
        (&Method::GET, "/agents/available") => {
            json_resp(StatusCode::OK, &crate::agents::available(&req))
        }
        (&Method::GET, "/agents/model-selection") => {
            json_resp(StatusCode::OK, &crate::agents::model_selection(app))
        }
        (&Method::POST, "/agents/model-selection") => {
            let body = crate::http::body_json(req).await.unwrap_or(json!({}));
            json_resp(StatusCode::OK, &crate::agents::save_selection(app, &body))
        }
        // 一键配置会写本机客户端的配置文件（~/.codex、~/.claude…）：面板会话 +
        // 只允许本机来源，与 Python 的 client_allowed 一致。
        (&Method::POST, "/agents/apply") | (&Method::POST, "/agents/restore") => {
            if !app.panel.token_ok(token_of(&req).as_deref()) {
                json_resp(
                    StatusCode::UNAUTHORIZED,
                    &json!({"error": {"message": "panel password required",
                                      "type": "invalid_request_error", "code": 401}}),
                )
            } else if !from_loopback(&req) {
                json_resp(
                    StatusCode::FORBIDDEN,
                    &json!({"ok": false, "error": {
                        "message": "agent config is only available from the machine running the gateway",
                        "code": 403}}),
                )
            } else {
                crate::agentcfg::handle(app, req).await
            }
        }
        (&Method::GET, "/scheduler") => json_resp(StatusCode::OK, &app.scheduler.status(app)),
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
        (&Method::GET, path) | (&Method::POST, path) if path.starts_with("/proxy/") => {
            crate::proxy_slots::handle(app, req).await
        }
        (&Method::POST, _) => not_ported(&path),
        _ => json_resp(StatusCode::NOT_FOUND, &json!({"error": {"message": "not found", "code": 404}})),
    };
    Some(out)
}

/// 风控窗口剩余秒数；没有限制时回 null（面板按真假判断，`wb_accounts.py:1264`）。
fn restricted_for(account: &crate::state::AccountFile, now: f64) -> Value {
    let left = (account.restricted_until() - now).max(0.0).round();
    if left > 0.0 {
        json!(left)
    } else {
        Value::Null
    }
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
        "inCooldown": false,
        "cooldownFor": Value::Null,
        "restrictedFor": restricted_for(account, now as f64),
        "modelCooldowns": [],
        "softStreak": 0,
        "unscopedStreak": 0,
        "breakerFor": Value::Null,
        "degradeFor": Value::Null,
        "fails": 0,
        "failWindow": 600,
        "lastFailFor": Value::Null,
        "addedAt": raw.get("addedAt").cloned().unwrap_or(Value::Null),
        "file": account.file_name(),
        "credits": raw.get("credits").cloned().unwrap_or(Value::Null),
        "tier": raw.get("tier").filter(|value| value.is_object()).cloned().unwrap_or(Value::Null),
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
        "lastCheckin": raw.get("lastCheckin").filter(|value| !value.is_null() && value.as_i64() != Some(0)).cloned().unwrap_or(Value::Null),
        "lastDailyChat": raw.get("lastDailyChat").filter(|value| !value.is_null() && value.as_i64() != Some(0)).cloned().unwrap_or(Value::Null),
        "lastActivityReport": raw.get("lastActivityReport").filter(|value| !value.is_null() && value.as_i64() != Some(0)).cloned().unwrap_or(Value::Null),
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

/// `POST /quota-scan/run`：忽略查询间隔立刻查一轮。
/// 已登记 429 窗口的配额线仍然不动（那几条交给窗口探针）。
async fn quota_scan_run(app: &std::sync::Arc<App>) -> Resp {
    let sent = crate::quota::scan_due(app, true, crate::quota::BATCH_LIMIT).await;
    let msg = if sent > 0 {
        format!("已查询 {sent} 条配额线")
    } else {
        "没有需要查询的配额线（未勾选模型或都已到下一次查询时刻）".to_string()
    };
    json_resp(StatusCode::OK, &json!({"ok": true, "sent": sent, "msg": msg}))
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

/// `wb_settings._clean_quota_scan_models`：字符串按 `,;\n` 拆，列表逐项取字符串，
/// 去空白、去重、保序；超过 200 个或类型不对就整条拒绝（不静默截断）。
fn clean_models(value: &Value) -> Option<Vec<String>> {
    let raw: Vec<&str> = match value {
        Value::Array(items) => {
            let mut out = Vec::new();
            for item in items {
                out.push(item.as_str()?);
            }
            out
        }
        Value::String(text) => text.split([',', ';', '\n']).collect(),
        _ => return None,
    };
    let mut models: Vec<String> = Vec::new();
    for name in raw {
        let name = name.trim();
        if !name.is_empty() && !models.iter().any(|m| m == name) {
            models.push(name.to_string());
        }
    }
    if models.len() > 200 {
        return None;
    }
    Some(models)
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
        // 观察清单要先规整（去空白/去重/保序），与 Python 版落盘的口径一致。
        if key == "quota_scan_models" {
            let Some(models) = clean_models(value) else {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    &json!({"error": {"message": "quota_scan_models must be a list of model ids", "code": 400}}),
                );
            };
            settings.insert(key.clone(), json!(models));
            applied.push(key.clone());
            continue;
        }
        // 面板是 settings.json 的唯一写者；这里只认「已有键」和几个新键，
        // 免得把前端随手带的字段写进配置文件。
        let known = settings.contains_key(key)
            || matches!(
                key.as_str(),
                "api_keys" | "deleted_api_key_ids" | "panel_password" | "active_realm"
                    | "update_check_enabled"
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
/// 行里的 `action` 关联本机最近一次签到/活跃：发放时间前 2 小时内到之后 10 分钟的
/// 那条成功记录算「这次动作带来的」（上游记的发放时间常比我们的动作记录早几秒）。
fn credit_grants(app: &App) -> Value {
    let now = crate::state::now_epoch() as f64;
    let actions = crate::activity::grant_actions(app);
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
                "action": create_at
                    .and_then(|at| crate::activity::grant_action(&actions, &account.uid(), at))
                    .map(|(task, ts, ok)| json!({"task": task, "at": ts, "ok": ok}))
                    .unwrap_or(Value::Null),
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

/// 供活动历史复用（同 `stamp_epoch`，只是给别的模块一个入口）。
pub fn stamp_epoch_pub(text: &str) -> Option<f64> {
    stamp_epoch(text)
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

/// `POST /accounts/daily-chat`：国际版每日活跃（桌面腿 + 网页腿缺口如实回报）。
async fn daily_chat(app: &std::sync::Arc<App>, req: Request<Incoming>) -> Resp {
    let body = crate::http::body_json(req).await.unwrap_or(json!({}));
    let uid = body.get("uid").and_then(Value::as_str).unwrap_or("");
    // `uids` 存在但为空 = 一个都不打；不存在 = 走全部参与任务的账号。
    let uids: Option<Vec<String>> = body.get("uids").and_then(Value::as_array).map(|l| {
        l.iter().filter_map(Value::as_str).map(String::from).collect()
    });
    json_resp(
        StatusCode::OK,
        &crate::tasks::daily_chat(app, uid, uids.as_deref(), "manual").await,
    )
}

#[cfg(test)]
mod login_limit_tests {
    use super::*;

    #[test]
    fn fifth_failure_blocks_then_releases() {
        let panel = Panel::default();
        let t = 1_000;
        for i in 0..LOGIN_MAX {
            assert_eq!(panel.login_blocked("10.0.0.9", t + i as i64), None);
            panel.note_login_failure("10.0.0.9", t + i as i64);
        }
        // 第 5 次失败之后再试就是 429，剩余等待秒数是「最早那次 + 60」。
        assert_eq!(panel.login_blocked("10.0.0.9", t + 5), Some(55));
        // 窗口滑过就放行，别的来源从头就不受影响。
        assert_eq!(panel.login_blocked("10.0.0.9", t + 61), None);
        assert_eq!(panel.login_blocked("10.0.0.10", t + 5), None);
    }

    #[test]
    fn success_clears_the_bucket() {
        let panel = Panel::default();
        for i in 0..LOGIN_MAX {
            panel.note_login_failure("10.0.0.9", 2_000 + i as i64);
        }
        assert!(panel.login_blocked("10.0.0.9", 2_005).is_some());
        panel.clear_login_failures("10.0.0.9");
        assert_eq!(panel.login_blocked("10.0.0.9", 2_005), None);
    }

    #[test]
    fn forwarded_headers_only_trusted_from_loopback() {
        let mut req = Request::builder().header("x-real-ip", "203.0.113.7").body(()).unwrap();
        req.extensions_mut()
            .insert(crate::http::PeerAddr(std::net::IpAddr::from([10, 0, 0, 9])));
        assert_eq!(client_ip(&req), "10.0.0.9");
        let mut req = Request::builder().header("x-real-ip", "203.0.113.7").body(()).unwrap();
        req.extensions_mut()
            .insert(crate::http::PeerAddr(std::net::IpAddr::from([127, 0, 0, 1])));
        assert_eq!(client_ip(&req), "203.0.113.7");
    }

    #[test]
    fn cidrs_in_wb_trusted_proxies_may_speak_for_their_clients() {
        let nets = parse_trusted("10.0.0.0/8, fd00::/8, junk, 10.1.2.3/99, 192.168.1.5");
        assert_eq!(nets.len(), 3, "junk 与越界的 /99 要被丢掉，裸 IP 当 /32");
        assert!(in_net("10.9.8.7".parse().unwrap(), "10.0.0.0".parse().unwrap(), 8));
        assert!(!in_net("11.0.0.1".parse().unwrap(), "10.0.0.0".parse().unwrap(), 8));
        assert!(in_net("fd00::1".parse().unwrap(), "fd00::".parse().unwrap(), 8));
        assert!(!in_net("fe80::1".parse().unwrap(), "fd00::".parse().unwrap(), 8));
        assert!(in_net("192.168.1.5".parse().unwrap(), "192.168.1.5".parse().unwrap(), 32));
        assert!(!in_net("192.168.1.6".parse().unwrap(), "192.168.1.5".parse().unwrap(), 32));
    }

    #[test]
    fn proxy_headers_strip_ports_and_refuse_garbage() {
        assert_eq!(header_ip("203.0.113.7"), Some("203.0.113.7".into()));
        assert_eq!(header_ip("203.0.113.7:8080"), Some("203.0.113.7".into()));
        assert_eq!(header_ip("[2001:db8::1]:443"), Some("2001:db8::1".into()));
        assert_eq!(header_ip("[2001:db8::1]"), Some("2001:db8::1".into()));
        assert_eq!(header_ip("1.2.3.4, 5.6.7.8"), None);
        assert_eq!(header_ip("not-an-ip"), None);
        assert_eq!(header_ip(""), None);
        assert_eq!(header_ip(&"9".repeat(80)), None);
    }

    #[test]
    fn ipv4_mapped_loopback_counts_as_the_local_machine() {
        let mapped: std::net::IpAddr = "::ffff:127.0.0.1".parse().unwrap();
        assert!(peer_trusted(mapped));
    }
}
