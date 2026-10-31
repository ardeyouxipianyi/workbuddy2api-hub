//! 积分刷新：打上游计费端点，把结果写回账号档的 `credits`。
//!
//! 三条路，按 `wb_accounts._fetch_credits_raw` 的顺序选：
//! 企业/静态 Key → 企业口径；国内版 → 套餐概览 + 免费包 + 付费包 + 签到状态；
//! 其余（国际版）→ `get-user-resource` 兜底。
//!
//! shortcut: 没做档位（tier）同步与后台定时刷新，只有面板点「刷新积分」时才跑。

use std::collections::HashSet;
use std::sync::Arc;

use serde_json::{json, Map, Value};

use crate::state::AccountFile;
use crate::App;

const SUMMARY: &str = "/billing/meter/get-user-resource-summary";
const FREE_PKGS: &str = "/billing/meter/get-user-resource-free-packages";
const PAID_PKGS: &str = "/billing/meter/get-user-resource-paid-packages";
const CHECKIN: &str = "/v2/billing/meter/checkin-activity-status";
const GET_RESOURCE: &str = "/v2/billing/meter/get-user-resource";
const ENT_USAGE: &str = "/billing/meter/get-enterprise-user-usage";
const ENT_USAGE_V2: &str = "/v2/billing/meter/get-enterprise-user-usage";

/// 超过这个天数的到期时间当成「永久」（免费包的周期边界能到 8 年后）。
const EXPIRY_SENTINEL_DAYS: f64 = 730.0;

fn billing_base(realm: &str) -> &'static str {
    match realm {
        "cn" => "https://www.codebuddy.cn",
        _ => "https://www.workbuddy.ai",
    }
}

fn origin(realm: &str) -> &'static str {
    billing_base(realm)
}

fn headers(account: &AccountFile) -> reqwest::header::HeaderMap {
    use reqwest::header::HeaderValue;
    let realm = account.realm();
    let mut map = reqwest::header::HeaderMap::new();
    let mut put = |key: &'static str, value: String| {
        if let Ok(v) = HeaderValue::from_str(&value) {
            map.insert(key, v);
        }
    };
    let uid = account.uid();
    put("content-type", "application/json".into());
    put("accept", "application/json, text/plain, */*".into());
    put("x-requested-with", "XMLHttpRequest".into());
    put(
        "user-agent",
        format!("WorkBuddy/{}", crate::identity::desktop_version(&realm)),
    );
    put("origin", origin(&realm).into());
    put("referer", format!("{}/", origin(&realm)));
    put("authorization", format!("Bearer {}", account.access_token()));
    put("x-user-id", uid.clone());
    let domain = match account.domain() {
        d if !d.is_empty() => d,
        _ => if realm == "cn" { "copilot.tencent.com".into() } else { "www.workbuddy.ai".into() },
    };
    put("x-domain", domain);
    put("x-codebuddy-request", "1".into());
    put("accept-language", if realm == "intl" { "en-US".into() } else { "zh-CN".into() });
    put("x-machine-id", crate::state::derive_id(&uid, "machine"));
    put("x-session-id", crate::state::derive_id(&uid, "session"));
    if account.is_api_key() {
        put("x-api-key", account.access_token());
    }
    let enterprise = account.str("enterpriseId").to_string();
    if !enterprise.is_empty() {
        put("x-enterprise-id", enterprise.clone());
        put("x-tenant-id", enterprise);
    } else {
        put("x-no-enterprise-id", "1".into());
    }
    if realm == "cn" {
        put("x-product", "SaaS".into());
    }
    map
}

async fn post(app: &Arc<App>, account: &AccountFile, path: &str, body: &Value) -> Result<Value, String> {
    let url = format!("{}{}", billing_base(&account.realm()), path);
    let resp = app
        .upstream
        .post(&url)
        .headers(headers(account))
        .json(body)
        .timeout(std::time::Duration::from_secs(20))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    let status = resp.status();
    let text = resp.text().await.map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("{status} {}", text.chars().take(200).collect::<String>()));
    }
    serde_json::from_str(&text).map_err(|e| format!("bad json: {e}"))
}

fn num(value: Option<&Value>) -> f64 {
    match value {
        Some(Value::Number(n)) => n.as_f64().unwrap_or(0.0),
        Some(Value::String(s)) => s.trim().parse().unwrap_or(0.0),
        _ => 0.0,
    }
}

/// 取第一个非空字段的数值（上游同一含义有好几个精度不同的键）。
fn val_of(acc: &Value, keys: &[&str]) -> f64 {
    for key in keys {
        let v = acc.get(*key);
        match v {
            None | Some(Value::Null) => continue,
            Some(Value::String(t)) if t.is_empty() => continue,
            _ => {}
        }
        let n = num(v);
        if n != 0.0 {
            return n;
        }
    }
    0.0
}

fn local_iso(epoch_seconds: f64) -> String {
    let secs = epoch_seconds as i64;
    let tm = unsafe { libc::localtime(&secs as *const i64) };
    if tm.is_null() {
        return String::new();
    }
    let tm = unsafe { *tm };
    format!(
        "{:04}-{:02}-{:02} {:02}:{:02}:{:02}",
        tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec
    )
}

/// 包的「抵扣截止时间」：认 DeductionEndTime（epoch 毫秒），没有就回退周期结束时间。
fn deduction_end_text(acc: &Value) -> String {
    let raw = acc.get("DeductionEndTime");
    let mut stamp = num(raw);
    if stamp <= 0.0 {
        return String::new();
    }
    if stamp > 1e11 {
        stamp /= 1000.0;
    }
    local_iso(stamp)
}

fn parse_time(text: &str) -> Option<f64> {
    let clean: String = text.replace('T', " ").chars().take(19).collect();
    let mut it = clean.split(|c| c == '-' || c == ' ' || c == ':');
    let y: i32 = it.next()?.parse().ok()?;
    let mo: i32 = it.next()?.parse().ok()?;
    let d: i32 = it.next()?.parse().ok()?;
    let h: i32 = it.next()?.parse().ok()?;
    let mi: i32 = it.next()?.parse().ok()?;
    let s: i32 = it.next()?.parse().ok()?;
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    tm.tm_year = y - 1900;
    tm.tm_mon = mo - 1;
    tm.tm_mday = d;
    tm.tm_hour = h;
    tm.tm_min = mi;
    tm.tm_sec = s;
    tm.tm_isdst = -1;
    let ts = unsafe { libc::mktime(&mut tm) };
    if ts < 0 {
        None
    } else {
        Some(ts as f64)
    }
}

/// 一个资源包 → 面板明细里的一行。
fn parse_package(acc: &Value) -> Value {
    let mut size = val_of(acc, &["CycleCapacitySizePrecise", "CycleCapacitySize", "CapacitySizePrecise", "CapacitySize"]);
    let mut remain = val_of(acc, &["CycleCapacityRemainPrecise", "CycleCapacityRemain", "CapacityRemainPrecise", "CapacityRemain"]);
    let mut used = val_of(acc, &["CycleCapacityUsedPrecise", "CycleCapacityUsed", "CapacityUsedPrecise", "CapacityUsed"]);
    if size > 0.0 && used <= 0.0 && remain <= size {
        used = (size - remain).max(0.0);
    } else if size > 0.0 && remain <= 0.0 && used < size {
        remain = (size - used).max(0.0);
    }
    if size <= 0.0 {
        size = used + remain;
    }

    let grant_reason = acc
        .get("AccountAttributes")
        .and_then(Value::as_array)
        .and_then(|list| {
            list.iter()
                .find(|a| a.get("Key").and_then(Value::as_str) == Some("grantReason"))
                .and_then(|a| a.get("Value").and_then(Value::as_str))
        })
        .unwrap_or("")
        .to_string();

    let create_time = match acc.get("CreateTime") {
        Some(v) if num(Some(v)) > 0.0 => {
            let mut ts = num(Some(v));
            if ts > 1e11 {
                ts /= 1000.0;
            }
            local_iso(ts)
        }
        Some(Value::String(s)) if !s.is_empty() => s.clone(),
        _ => String::new(),
    };

    let cycle_end = acc.get("CycleEndTime").and_then(Value::as_str).unwrap_or("").to_string();
    let expire = {
        let d = deduction_end_text(acc);
        if d.is_empty() { cycle_end.clone() } else { d }
    };
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    let (mut days_left, mut is_expired, mut no_expiry) = (Value::Null, false, false);
    if !expire.is_empty() {
        if let Some(end) = parse_time(&expire) {
            let diff = end - now;
            if diff > EXPIRY_SENTINEL_DAYS * 86400.0 {
                no_expiry = true;
            } else {
                days_left = json!((diff / 86400.0 * 10.0).round() / 10.0);
                is_expired = diff < 0.0;
            }
        }
    }

    json!({
        "name": acc.get("PackageName").and_then(Value::as_str).unwrap_or("Package"),
        "package_code": acc.get("PackageCode").and_then(Value::as_str).unwrap_or(""),
        "size": (size * 100.0).round() / 100.0,
        "used": (used * 100.0).round() / 100.0,
        "remain": (remain * 100.0).round() / 100.0,
        "in_usage": acc.get("InUsage").and_then(Value::as_bool).unwrap_or(false),
        "is_expired": is_expired,
        "no_expiry": no_expiry,
        "days_left": days_left,
        "cycle_start_time": acc.get("CycleStartTime").and_then(Value::as_str).unwrap_or(""),
        "cycle_end_time": cycle_end,
        "expire_time": expire,
        "create_time": create_time,
        "grant_reason": grant_reason,
        "status": acc.get("Status").cloned().unwrap_or(Value::Null),
    })
}

fn totals(packages: &mut Vec<Value>) -> (f64, f64, f64, Option<Value>) {
    let get = |p: &Value, key: &str| p.get(key).and_then(Value::as_f64).unwrap_or(0.0);
    packages.sort_by(|a, b| {
        let key = |p: &Value| {
            let in_use = if p.get("in_usage").and_then(Value::as_bool).unwrap_or(false) { 0 } else { 1 };
            let expired = if p.get("is_expired").and_then(Value::as_bool).unwrap_or(false) { 1 } else { 0 };
            let days = p
                .get("days_left")
                .and_then(Value::as_f64)
                .map(|d| if d < 0.0 { 99999.0 + d.abs() } else { d })
                .unwrap_or(99999.0);
            (in_use, expired, (days * 10.0) as i64, -((get(p, "remain") * 100.0) as i64))
        };
        key(a).cmp(&key(b))
    });
    let sum = |key: &str| packages.iter().map(|p| get(p, key)).sum::<f64>();
    let (size, used, remain) = (sum("size"), sum("used"), sum("remain"));
    let earliest = packages
        .iter()
        .filter(|p| {
            get(p, "remain") > 0.0
                && !p.get("is_expired").and_then(Value::as_bool).unwrap_or(false)
                && p.get("days_left").map(|d| !d.is_null()).unwrap_or(false)
        })
        .min_by(|a, b| {
            get(a, "days_left").partial_cmp(&get(b, "days_left")).unwrap_or(std::cmp::Ordering::Equal)
        })
        .map(|p| {
            json!({
                "name": p.get("name").cloned().unwrap_or(Value::Null),
                "package_code": p.get("package_code").cloned().unwrap_or(Value::Null),
                "remain": p.get("remain").cloned().unwrap_or(Value::Null),
                "cycle_end_time": p.get("cycle_end_time").cloned().unwrap_or(Value::Null),
                "days_left": p.get("days_left").cloned().unwrap_or(Value::Null),
            })
        });
    (size, used, remain, earliest)
}

fn credits_doc(size: f64, used: f64, remain: f64, paid: bool, checkin: Value, earliest: Option<Value>, packages: Vec<Value>) -> Value {
    let pct = |n: f64, fallback: &str| {
        if size > 0.0 { format!("{:.1}%", n / size * 100.0) } else { fallback.to_string() }
    };
    json!({
        "remain": (remain * 100.0).round() / 100.0,
        "used": (used * 100.0).round() / 100.0,
        "size": (size * 100.0).round() / 100.0,
        "used_percent": pct(used, "0.0%"),
        "remain_percent": pct(remain, "100.0%"),
        "is_paid_user": paid,
        "checkin": checkin,
        "earliest_expiring": earliest,
        "packages": packages,
        "updated_at": std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0),
        "updated_iso": crate::state::local_stamp(),
    })
}

/// 单个账号刷新一次积分；失败回 `{ok:false,error}` 而不是抛。
pub async fn fetch_one(app: &Arc<App>, account: &AccountFile) -> Value {
    if account.access_token().is_empty() {
        return json!({"ok": false, "error": "没有 accessToken"});
    }
    let result = if account.is_api_key() || !account.str("enterpriseId").is_empty() {
        enterprise(app, account).await
    } else if account.realm() == "cn" {
        match cn_detailed(app, account).await {
            Ok(v) => Ok(v),
            Err(e) => fallback(app, account).await.map_err(|_| e),
        }
    } else {
        fallback(app, account).await
    };
    match result {
        Ok(credits) => {
            let mut data = account.data.clone();
            data.insert("credits".into(), credits.clone());
            if let Err(e) = crate::state::write_json_atomic(&account.path, &Value::Object(data)) {
                return json!({"ok": false, "error": format!("积分已取到但写盘失败: {e}")});
            }
            app.state.reload();
            json!({"ok": true, "uid": account.uid(), "credits": credits})
        }
        Err(e) => json!({"ok": false, "uid": account.uid(), "error": e}),
    }
}

async fn cn_detailed(app: &Arc<App>, account: &AccountFile) -> Result<Value, String> {
    let summary = post(app, account, SUMMARY, &json!({})).await?;
    if summary.get("code").and_then(Value::as_i64).unwrap_or(-1) != 0 {
        return Err(summary
            .get("msg")
            .and_then(Value::as_str)
            .unwrap_or("套餐概览失败")
            .to_string());
    }
    let data = summary.get("data").cloned().unwrap_or(json!({}));
    let paid = data.get("IsPaidUser").and_then(Value::as_bool).unwrap_or(false);
    let summary_pkgs = data.get("Packages").and_then(Value::as_array).cloned().unwrap_or_default();
    let codes: Vec<Value> = summary_pkgs
        .iter()
        .filter_map(|p| p.get("PackageCode").cloned())
        .filter(|c| c.as_str().map(|s| !s.is_empty()).unwrap_or(false))
        .collect();

    let mut packages = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    if !codes.is_empty() {
        for (path, body) in [
            (FREE_PKGS, json!({"PackageCodes": codes, "PageNumber": 1, "PageSize": 200, "Status": [0]})),
            (
                PAID_PKGS,
                json!({"PackageCodes": codes, "PageNumber": 1, "PageSize": 200, "Status": [0, 3], "NeedRenewInfo": true}),
            ),
        ] {
            if let Ok(res) = post(app, account, path, &body).await {
                if let Some(list) = res
                    .get("data")
                    .and_then(|d| d.get("Accounts"))
                    .and_then(Value::as_array)
                {
                    for raw in list {
                        let id = raw.get("AccountId").and_then(Value::as_str).unwrap_or("").to_string();
                        if !id.is_empty() && !seen.insert(id) {
                            continue;
                        }
                        packages.push(parse_package(raw));
                    }
                }
            }
        }
    }

    // 包列表为空时退回概览里的周期汇总，至少让总数不显示 0/0。
    if packages.is_empty() && !summary_pkgs.is_empty() {
        let mut size = 0.0;
        let mut remain = 0.0;
        let mut used = 0.0;
        for sp in &summary_pkgs {
            size += num(sp.get("CycleTotalCapacity"));
            remain += num(sp.get("CycleRemainCapacity"));
            used += num(sp.get("CycleUsedCapacity"));
        }
        if size > 0.0 || remain > 0.0 {
            packages.push(json!({
                "name": summary_pkgs[0].get("PackageName").and_then(Value::as_str).unwrap_or("Package"),
                "package_code": summary_pkgs[0].get("PackageCode").cloned().unwrap_or(Value::Null),
                "size": size, "used": used, "remain": remain,
                "in_usage": true, "is_expired": false, "no_expiry": false,
                "days_left": Value::Null, "cycle_start_time": "", "cycle_end_time": "",
                "expire_time": "", "create_time": "", "grant_reason": "",
            }));
        }
    }

    let checkin = match post(app, account, CHECKIN, &json!({})).await {
        Ok(res) => match res.get("data") {
            Some(d) if d.as_object().map(|o| !o.is_empty()).unwrap_or(false) => json!({
                "today_checked_in": d.get("today_checked_in").cloned().unwrap_or(json!(false)),
                "streak_days": d.get("streak_days").cloned().unwrap_or(json!(0)),
                "daily_credit": d.get("daily_credit").cloned().unwrap_or(json!(0)),
                "today_credit": d.get("today_credit").cloned().unwrap_or(json!(0)),
                "active": d.get("active").cloned().unwrap_or(json!(true)),
            }),
            _ => Value::Null,
        },
        Err(_) => Value::Null,
    };

    let (size, used, remain, earliest) = totals(&mut packages);
    Ok(credits_doc(size, used, remain, paid, checkin, earliest, packages))
}

async fn fallback(app: &Arc<App>, account: &AccountFile) -> Result<Value, String> {
    let body = json!({
        "PageNumber": 1,
        "PageSize": 100,
        "ProductCode": "p_tcaca",
        "Status": [0, 3],
        "PackageEndTimeRangeBegin": crate::state::local_stamp(),
        "PackageEndTimeRangeEnd": "2036-01-01 00:00:00",
    });
    let res = post(app, account, GET_RESOURCE, &body).await?;
    let list = res
        .get("data")
        .and_then(|d| d.get("Response"))
        .and_then(|d| d.get("Data"))
        .and_then(|d| d.get("Accounts"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut packages: Vec<Value> = list.iter().map(parse_package).collect();
    let (size, used, remain, earliest) = totals(&mut packages);
    Ok(credits_doc(size, used, remain, false, Value::Null, earliest, packages))
}

/// 企业口径：只有周期已用与额度，合成一个包让看板明细照常工作。
async fn enterprise(app: &Arc<App>, account: &AccountFile) -> Result<Value, String> {
    let path = if account.is_api_key() { ENT_USAGE_V2 } else { ENT_USAGE };
    let res = post(app, account, path, &json!({})).await?;
    let data = res.get("data").cloned().unwrap_or(json!({}));
    let used = num(data.get("credit"));
    let limit = num(data.get("limitNum"));
    let remain = (limit - used).max(0.0);
    let cycle_end = data.get("cycleEndTime").and_then(Value::as_str).unwrap_or("").to_string();
    let mut packages = vec![json!({
        "name": "企业额度",
        "package_code": "enterprise",
        "size": limit, "used": used, "remain": remain,
        "in_usage": true, "is_expired": false, "no_expiry": false,
        "days_left": Value::Null,
        "cycle_start_time": data.get("cycleStartTime").and_then(Value::as_str).unwrap_or(""),
        "cycle_end_time": cycle_end,
        "expire_time": cycle_end,
        "create_time": "", "grant_reason": "企业额度",
    })];
    let (size, used_total, remain_total, earliest) = totals(&mut packages);
    Ok(credits_doc(size, used_total, remain_total, true, Value::Null, earliest, packages))
}

/// 面板的「一键刷新积分」：按 uid / realm / 整池挑目标，逐个刷新。
pub async fn sweep(app: &Arc<App>, uid: &str, realm: &str) -> Value {
    let targets: Vec<AccountFile> = app
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
        .collect();
    let mut results = Vec::new();
    for account in &targets {
        results.push(fetch_one(app, account).await);
        // 多账号时拉开间隔，别把上游计费端点打成一股突发。
        if targets.len() > 1 {
            tokio::time::sleep(std::time::Duration::from_millis(300)).await;
        }
    }
    let now = crate::state::now_epoch();
    let accounts: Vec<Value> = app
        .state
        .accounts_snapshot()
        .iter()
        .map(|a| crate::panel::account_view(a, now))
        .collect();
    let mut out = Map::new();
    out.insert("results".into(), Value::Array(results));
    out.insert("accounts".into(), Value::Array(accounts));
    Value::Object(out)
}

/// 签到回执归一：`pending` 不写 lastCheckin（写早了当天就不再重试）。
fn checkin_state(code: i64, msg: &str) -> &'static str {
    let pending = ["处理中", "请勿重复操作", "系统繁忙", "稍后再试"];
    if pending.iter().any(|k| msg.contains(k)) {
        return "pending";
    }
    match code {
        0 => {
            if msg.contains("失败") {
                "failed"
            } else {
                "checked"
            }
        }
        10001 => {
            if msg.contains("已签到") {
                "already"
            } else {
                "failed"
            }
        }
        _ => "failed",
    }
}

/// 国内版每日签到（`wb_accounts.checkin`）。
pub async fn checkin(app: &Arc<App>, account: &AccountFile) -> Value {
    if account.realm() != "cn" || account.is_api_key() || !account.str("enterpriseId").is_empty() {
        return json!({"ok": false, "error": "这个账号没有个人成长体系（仅国内版 OAuth 账号可签到）"});
    }
    // 「今天已签到」是 HTTP 400 + code 10001，所以这里不能把 4xx 直接当失败。
    let payload = match app
        .upstream
        .post(format!("{}/v2/billing/meter/daily-checkin", billing_base("cn")))
        .headers(headers(account))
        .json(&json!({}))
        .timeout(std::time::Duration::from_secs(15))
        .send()
        .await
    {
        Ok(resp) => resp.text().await.ok().and_then(|t| serde_json::from_str::<Value>(&t).ok()),
        Err(_) => None,
    };
    let Some(payload) = payload else {
        return json!({"ok": false, "state": "failed", "error": "签到请求失败"});
    };
    let code = payload.get("code").and_then(Value::as_i64).unwrap_or(-1);
    let msg = payload.get("msg").and_then(Value::as_str).unwrap_or("ok").to_string();
    let state = checkin_state(code, &msg);
    if state == "checked" || state == "already" {
        let mut data = account.data.clone();
        data.insert("lastCheckin".into(), json!(crate::state::local_stamp()));
        let _ = crate::state::write_json_atomic(&account.path, &Value::Object(data));
        app.state.reload();
        json!({"ok": true, "state": state, "code": code, "msg": msg,
               "data": payload.get("data").cloned().unwrap_or(Value::Null)})
    } else {
        json!({"ok": false, "state": state, "code": code, "msg": msg, "error": msg})
    }
}

/// 面板「国内版签到」：按 uid 或全部参与任务的国内版账号逐个签到。
pub async fn checkin_all(app: &Arc<App>, uid: &str) -> Value {
    let targets: Vec<AccountFile> = app
        .state
        .accounts_snapshot()
        .into_iter()
        .filter(|a| {
            a.realm() == "cn"
                && !a.is_api_key()
                && a.str("enterpriseId").is_empty()
                && (!uid.is_empty() && uid != "all" && a.uid() == uid
                    || (uid.is_empty() || uid == "all")
                        && a.data.get("tasksEnabled").and_then(Value::as_bool).unwrap_or(true))
        })
        .collect();
    let mut results = Vec::new();
    for account in &targets {
        let mut out = checkin(app, account).await;
        if let Some(obj) = out.as_object_mut() {
            obj.insert("uid".into(), json!(account.uid()));
            obj.insert("nickname".into(), json!(account.str("nickname")));
        }
        results.push(out);
    }
    let now = crate::state::now_epoch();
    let accounts: Vec<Value> = app
        .state
        .accounts_snapshot()
        .iter()
        .map(|a| crate::panel::account_view(a, now))
        .collect();
    json!({"results": results, "accounts": accounts})
}

/// `ck_<id>.<secret>` 里取 `<id>`（只留 [A-Za-z0-9_-]，会拼进文件名）。
pub fn api_key_id(token: &str) -> String {
    let mut text = token;
    for prefix in ["ck_", "pt_"] {
        if let Some(rest) = text.strip_prefix(prefix) {
            text = rest;
            break;
        }
    }
    let head = text.split('.').next().unwrap_or("");
    head.chars().filter(|c| c.is_ascii_alphanumeric() || *c == '_' || *c == '-').collect()
}

pub fn is_api_key_token(token: &str) -> bool {
    token.starts_with("ck_") || token.starts_with("pt_")
}

/// 用企业额度端点验一把静态 Key（`Account.probe_api_key`）——Key 没有任何本地
/// 校验手段，坏 Key 进了池会让选号侧反复选中它，表象是「网关时好时坏」。
pub async fn probe_api_key(app: &Arc<App>, token: &str) -> Value {
    let resp = app
        .upstream
        .post(format!("{}{}", billing_base("cn"), ENT_USAGE_V2))
        .header("authorization", format!("Bearer {token}"))
        .header("x-api-key", token)
        .header("content-type", "application/json")
        .header("accept", "application/json")
        .header("user-agent", format!("WorkBuddy/{}", crate::identity::desktop_version("cn")))
        .json(&json!({}))
        .timeout(std::time::Duration::from_secs(15))
        .send()
        .await;
    let Ok(resp) = resp else {
        return json!({"ok": false, "error": "探测请求失败"});
    };
    let Ok(body) = resp.json::<Value>().await else {
        return json!({"ok": false, "error": "上游回的不是 JSON"});
    };
    if body.get("code").and_then(Value::as_i64) != Some(0) {
        return json!({"ok": false, "error": format!(
            "code {}: {}", body.get("code").and_then(Value::as_i64).unwrap_or(-1),
            body.get("msg").and_then(Value::as_str).unwrap_or(""))});
    }
    let data = body.get("data").cloned().unwrap_or(json!({}));
    json!({"ok": true, "used": data.get("credit").cloned().unwrap_or(Value::Null),
           "limit": data.get("limitNum").cloned().unwrap_or(Value::Null),
           "cycle_end": data.get("cycleEndTime").cloned().unwrap_or(Value::Null)})
}
