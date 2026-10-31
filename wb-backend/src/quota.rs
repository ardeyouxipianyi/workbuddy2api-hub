//! 模型额度窗口探针 + 额度主动查询（`wb_window_probe.py` / `wb_quota_scan.py` 的 Rust 版）。
//!
//! 两条配额线共用同一个「(账号, 模型) 打满后上游给了重置时刻」的事实：
//! 窗口探针到点补一个最小请求，把新一轮窗口的起点钉在重置那一刻；额度扫描则在
//! 还没打满时按 6 小时主动查一次。状态各落一个文件，格式与 Python 版逐键一致
//! （`accounts/model_windows.json`、`accounts/quota_scan.json`），两个后端互相接手
//! 时记录照旧有效。
//!
//! shortcut: 模型级冷却没接（池只有账号级冷却），已登记窗口的配额线靠窗口记录拦住
//! 重复查询；真实请求仍可能再撞一次同一条已满的线，撞上后走既有的 60s 账号冷却。
//! shortcut: 巡检循环 60s 一跳，窗口探针最多比重置时刻晚 60s 发出（Python 版 30s）。

use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Map, Value};

use crate::state::{now_epoch, AccountFile};
use crate::App;

const WINDOW_FILE: &str = "model_windows.json";
const SCAN_FILE: &str = "quota_scan.json";
const STATE_VERSION: i64 = 1;

// ---- 额度扫描（wb_quota_scan 的常量，名字与值都照抄）
const SCAN_INTERVAL: f64 = 6.0 * 3600.0;
const RETRY_DELAY: f64 = 15.0 * 60.0;
const MAX_ATTEMPTS: i64 = 6;
const FIRST_DELAY: f64 = 5.0;
const FIRST_STAGGER: f64 = 15.0;
const FIRST_DELAY_MAX: f64 = 3600.0;
pub const BATCH_LIMIT: usize = 6;
const SCAN_GAP: Duration = Duration::from_millis(400);
const MAX_AGE: f64 = 30.0 * 86400.0;

/// 面板说明文字用的查询周期（小时）。
pub fn scan_interval_hours() -> i64 {
    (SCAN_INTERVAL / 3600.0) as i64
}

// ---- 窗口探针（wb_window_probe 的常量）
const PROBE_PROMPT: &str = "Hi";
const PROBE_MAX_TOKENS: i64 = 16;
const PROBE_TIMEOUT: u64 = 60;
const WINDOW_MAX_ATTEMPTS: i64 = 20;
const WINDOW_RETRY: f64 = 15.0;
const FRESH_WINDOW_MIN: f64 = 30.0;
const WINDOW_MAX_AGE: f64 = 7.0 * 86400.0;
const WINDOW_BATCH: usize = 8;
const PROBE_GAP: Duration = Duration::from_millis(400);

/// 两个状态文件的读-改-写锁；面板「立即查询」与巡检线程会同时进来。
static FILE_LOCK: Mutex<()> = Mutex::new(());
static SCAN_BUSY: AtomicBool = AtomicBool::new(false);
static WINDOW_BUSY: AtomicBool = AtomicBool::new(false);

/// 重入保护：同一时刻只跑一轮（Python 版是两个 `_run_lock`）。
struct Guard(&'static AtomicBool);

impl Guard {
    fn take(flag: &'static AtomicBool) -> Option<Guard> {
        if flag
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .is_ok()
        {
            Some(Guard(flag))
        } else {
            None
        }
    }
}

impl Drop for Guard {
    fn drop(&mut self) {
        self.0.store(false, Ordering::SeqCst);
    }
}

// --------------------------------------------------------------- 小工具

fn key(uid: &str, model: &str) -> String {
    format!("{uid}\u{0}{model}")
}

fn row_num(row: &Map<String, Value>, name: &str) -> f64 {
    match row.get(name) {
        Some(Value::Number(n)) => n.as_f64().unwrap_or(0.0),
        Some(Value::String(s)) => s.trim().parse().unwrap_or(0.0),
        _ => 0.0,
    }
}

fn row_int(row: &Map<String, Value>, name: &str) -> i64 {
    row_num(row, name) as i64
}

fn row_str(row: &Map<String, Value>, name: &str) -> String {
    row.get(name)
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string()
}

fn clip(text: &str, limit: usize) -> String {
    text.chars().take(limit).collect()
}

/// `wb_settings.window_probe`：缺省开；只认真 `true`（手写 "true" 不算）。
pub fn window_probe_enabled(app: &Arc<App>) -> bool {
    match app.state.settings_snapshot().get("window_probe") {
        None => true,
        Some(Value::Bool(v)) => *v,
        _ => false,
    }
}

// --------------------------------------------------------------- 状态存取

/// 读一个状态文件。兼容三种形状：`{"<field>": [...]}`、`{"<field>": {...}}`、裸数组；
/// 带 BOM 的手改文件也认（Python 用 utf-8-sig）。坏文件当作没有记录。
fn read_rows(path: &Path, field: &str) -> Vec<Map<String, Value>> {
    let text = match std::fs::read_to_string(path) {
        Ok(t) => t,
        Err(_) => return Vec::new(),
    };
    let text = text.strip_prefix('\u{feff}').unwrap_or(&text);
    let raw: Value = match serde_json::from_str(text) {
        Ok(v) => v,
        Err(_) => return Vec::new(),
    };
    let stored = match &raw {
        Value::Object(map) => map.get(field).cloned().unwrap_or(Value::Null),
        other => other.clone(),
    };
    match stored {
        Value::Array(items) => items
            .into_iter()
            .filter_map(|v| v.as_object().cloned())
            .collect(),
        Value::Object(map) => map
            .into_iter()
            .filter_map(|(_, v)| v.as_object().cloned())
            .collect(),
        _ => Vec::new(),
    }
}

fn save_rows(dir: &Path, file: &str, field: &str, rows: Vec<Value>, extra: Map<String, Value>) {
    let mut payload = Map::new();
    payload.insert("version".into(), json!(STATE_VERSION));
    payload.insert("saved_at".into(), json!(crate::state::local_stamp()));
    for (k, v) in extra {
        payload.insert(k, v);
    }
    payload.insert(field.into(), Value::Array(rows));
    let _ = std::fs::create_dir_all(dir);
    // 写失败只丢持久化，不影响本轮查询/探针（与 Python 版同一个取向）。
    let _ = crate::state::write_json_atomic(&dir.join(file), &Value::Object(payload));
}

fn load_windows(dir: &Path, now: f64) -> Map<String, Value> {
    let mut out = Map::new();
    for row in read_rows(&dir.join(WINDOW_FILE), "windows") {
        let uid = row_str(&row, "uid");
        let model = row_str(&row, "model");
        let reset_at = row_num(&row, "reset_at");
        if uid.is_empty() || model.is_empty() || reset_at <= 0.0 {
            continue;
        }
        if reset_at < now - WINDOW_MAX_AGE {
            continue;
        }
        let k = key(&uid, &model);
        out.insert(
            k,
            json!({
                "uid": uid,
                "model": model,
                "reset_at": reset_at,
                "attempts": row_int(&row, "attempts"),
                "last_try": row_num(&row, "last_try"),
                "last_result": clip(&row_str(&row, "last_result"), 200),
                "cap": row.get("cap").and_then(Value::as_bool).unwrap_or(false),
                "cap_at": row_num(&row, "cap_at"),
            }),
        );
    }
    out
}

fn save_windows(dir: &Path, windows: &Map<String, Value>) {
    save_rows(
        dir,
        WINDOW_FILE,
        "windows",
        windows.values().cloned().collect(),
        Map::new(),
    );
}

fn load_scans(dir: &Path, now: f64) -> Map<String, Value> {
    let mut out = Map::new();
    for row in read_rows(&dir.join(SCAN_FILE), "scans") {
        let uid = row_str(&row, "uid");
        let model = row_str(&row, "model");
        if uid.is_empty() || model.is_empty() {
            continue;
        }
        let stamp = {
            let last_try = row_num(&row, "last_try");
            if last_try > 0.0 {
                last_try
            } else {
                row_num(&row, "seen_at")
            }
        };
        if stamp > 0.0 && stamp < now - MAX_AGE {
            continue;
        }
        let k = key(&uid, &model);
        out.insert(
            k,
            json!({
                "uid": uid,
                "model": model,
                "last_scan": row_num(&row, "last_scan"),
                "next_at": row_num(&row, "next_at"),
                "seen_at": row_num(&row, "seen_at"),
                "attempts": row_int(&row, "attempts"),
                "last_try": row_num(&row, "last_try"),
                "last_result": clip(&row_str(&row, "last_result"), 40),
                "last_detail": clip(&row_str(&row, "last_detail"), 200),
            }),
        );
    }
    out
}

fn save_scans(dir: &Path, scans: &Map<String, Value>) {
    let mut extra = Map::new();
    extra.insert("interval_hours".into(), json!(SCAN_INTERVAL / 3600.0));
    save_rows(
        dir,
        SCAN_FILE,
        "scans",
        scans.values().cloned().collect(),
        extra,
    );
}

// --------------------------------------------------------------- 窗口登记

/// 登记一个额度窗口（`wb_window_probe.schedule`）。重复 429 时间戳不变就不重写。
/// Rust 侧目前没有「配额用尽(6004)」的调用方，所以 cap 恒为 false。
pub fn schedule(app: &Arc<App>, uid: &str, model: &str, reset_at: f64) -> bool {
    let now = now_epoch() as f64;
    if uid.is_empty() || model.is_empty() || reset_at <= now {
        return false;
    }
    if !window_probe_enabled(app) {
        return false;
    }
    let dir = app.state.accounts_dir.clone();
    let _guard = FILE_LOCK.lock().unwrap();
    let mut windows = load_windows(&dir, now);
    let k = key(uid, model);
    if let Some(row) = windows.get(&k).and_then(Value::as_object) {
        if (row_num(row, "reset_at") - reset_at).abs() < 1.0 {
            return false;
        }
    }
    windows.insert(
        k,
        json!({
            "uid": uid,
            "model": model,
            "reset_at": reset_at,
            "attempts": 0,
            "last_try": 0.0,
            "last_result": "",
            "cap": false,
            "cap_at": 0.0,
        }),
    );
    save_windows(&dir, &windows);
    true
}

fn drop_window(dir: &Path, uid: &str, model: &str) -> bool {
    let now = now_epoch() as f64;
    let _guard = FILE_LOCK.lock().unwrap();
    let mut windows = load_windows(dir, now);
    if windows.remove(&key(uid, model)).is_some() {
        save_windows(dir, &windows);
        true
    } else {
        false
    }
}

fn update_window(
    dir: &Path,
    uid: &str,
    model: &str,
    reset_at: f64,
    attempts: i64,
    detail: &str,
) -> bool {
    let now = now_epoch() as f64;
    let _guard = FILE_LOCK.lock().unwrap();
    let mut windows = load_windows(dir, now);
    let k = key(uid, model);
    let Some(row) = windows.get(&k).and_then(Value::as_object).cloned() else {
        return false;
    };
    let mut row = row;
    row.insert("reset_at".into(), json!(reset_at));
    row.insert("attempts".into(), json!(attempts));
    row.insert("last_try".into(), json!(now_epoch() as f64));
    row.insert("last_result".into(), json!(clip(detail, 200)));
    windows.insert(k, Value::Object(row));
    save_windows(dir, &windows);
    true
}

/// 面板调度条用的窗口快照（按到点时刻升序）。
pub fn window_snapshot(app: &Arc<App>, limit: usize) -> Vec<Value> {
    let now = now_epoch() as f64;
    let windows = load_windows(&app.state.accounts_dir, now);
    let mut rows: Vec<Value> = windows
        .values()
        .filter_map(Value::as_object)
        .map(|row| {
            let uid = row_str(row, "uid");
            let reset_at = row_num(row, "reset_at");
            json!({
                "uid": uid,
                "uid8": clip(&uid, 8),
                "model": row_str(row, "model"),
                "reset_at": reset_at,
                "reset_in": (reset_at - now).max(0.0) as i64,
                "attempts": row_int(row, "attempts"),
                "last_result": row_str(row, "last_result"),
                "cap": row.get("cap").and_then(Value::as_bool).unwrap_or(false),
                "cap_at": row_num(row, "cap_at"),
            })
        })
        .collect();
    rows.sort_by(|a, b| {
        let x = a.get("reset_at").and_then(Value::as_f64).unwrap_or(0.0);
        let y = b.get("reset_at").and_then(Value::as_f64).unwrap_or(0.0);
        x.partial_cmp(&y).unwrap_or(std::cmp::Ordering::Equal)
    });
    rows.truncate(limit.max(1));
    rows
}

// --------------------------------------------------------------- 探针请求

/// 给 (账号, 模型) 发一个最小对话请求，返回 (通了, 上游给的重置时刻, 失败原因)。
async fn probe_request(
    app: &Arc<App>,
    account: &AccountFile,
    model: &str,
) -> (bool, Option<f64>, String) {
    let app = app.clone();
    let account = account.clone();
    let model = model.to_string();
    let uid = account.uid();
    let mut token = account.access_token();
    // Token 快过期就先刷新，否则探针只会拿回 401，白跑一趟。
    let exp = if account.expires_at() > 0 {
        account.expires_at()
    } else {
        crate::state::jwt_exp(&token).unwrap_or(0)
    };
    if exp > 0 && exp - now_epoch() < 120 {
        if let Some(fresh) = crate::pool::refresh(&app, &uid).await {
            token = fresh;
        }
    }
    let realm = account.realm();
    let product = account.product();
    let url = format!(
        "{}{}",
        crate::upstream::chat_upstream_base(&realm, &product),
        crate::CHAT_PATH
    );
    let body = crate::upstream::build_upstream_body(&json!({
        "model": model,
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
        "stream": true,
        "max_tokens": PROBE_MAX_TOKENS,
        "reasoning_effort": "none",
    }));
    let headers = crate::identity::chat_headers(&crate::identity::Ctx {
        realm: &realm,
        product: &product,
        uid: &uid,
        token: &token,
        enterprise_id: None,
        department: None,
        session_key: None,
    });
    let mut builder = app
        .upstream
        .post(&url)
        .timeout(Duration::from_secs(PROBE_TIMEOUT))
        .body(serde_json::to_vec(&body).unwrap_or_default());
    for (name, value) in headers {
        builder = builder.header(name, value);
    }
    let resp = match builder.send().await {
        Ok(r) => r,
        Err(e) => return (false, None, clip(&e.to_string(), 200)),
    };
    let status = resp.status();
    if status.is_success() {
        let _ = resp.bytes().await;
        return (true, None, String::new());
    }
    let detail = resp.text().await.unwrap_or_default();
    if status.as_u16() == 429 {
        return (false, parse_reset(&detail), clip(&detail, 200));
    }
    (
        false,
        None,
        format!("HTTP {}: {}", status.as_u16(), clip(&detail, 120)),
    )
}

/// 还原 JSON 转义过的响应体里的 `\uXXXX`（`\\u5c06` -> `将`）。
/// Python 版用 `unicode_escape`，那会把本来就正常的中文 UTF-8 解成乱码；这里只认 `\u`。
fn unescape(text: &str) -> String {
    if !text.contains("\\u") {
        return text.to_string();
    }
    let chars: Vec<char> = text.chars().collect();
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    while i < chars.len() {
        if chars[i] == '\\' && i + 5 < chars.len() && chars[i + 1] == 'u' {
            let hex: String = chars[i + 2..i + 6].iter().collect();
            if let Ok(code) = u32::from_str_radix(&hex, 16) {
                if let Some(ch) = char::from_u32(code) {
                    out.push(ch);
                    i += 6;
                    continue;
                }
            }
        }
        out.push(chars[i]);
        i += 1;
    }
    out
}

/// 从 429 响应体里抠出重置时刻（`wb_proxy.parse_rate_limit_reset`）。
///
/// 上游给两种措辞：`... will reset at 2026-09-19 18:29:03 UTC+8 ...` 与
/// `... 将在 2026-10-02 16:00:34 UTC+8 重置 ...`。时间戳按体里自带的时区换算成 epoch
/// （用 UTC 口径算，不能用本机时区，否则不在 UTC+8 的机器会差几小时）。
pub fn parse_reset(detail: &str) -> Option<f64> {
    if detail.is_empty() {
        return None;
    }
    let text = unescape(detail);
    let bytes = text.as_bytes();
    let keyword = ["reset at", "将在"]
        .iter()
        .filter_map(|kw| text.find(kw))
        .min();
    let stamp_at = match keyword {
        Some(at) => find_stamp(bytes, &text, at),
        // 措辞不认识时的兜底：认「YYYY-MM-DD HH:MM:SS UTC±N」这个搭配。
        None => find_stamp_utc(bytes, &text),
    }?;
    let stamp = text.get(stamp_at..stamp_at + 19)?;
    let field = |a: usize, b: usize| stamp.get(a..b).and_then(|s| s.parse::<i64>().ok());
    let (year, month, day) = (field(0, 4)?, field(5, 7)?, field(8, 10)?);
    let (hour, minute, second) = (field(11, 13)?, field(14, 16)?, field(17, 19)?);
    let offset = utc_offset(bytes);
    Some((civil_to_epoch(year, month, day, hour, minute, second) - offset) as f64)
}

fn is_stamp_at(bytes: &[u8], i: usize) -> bool {
    let digit = |k: usize| {
        bytes
            .get(i + k)
            .map(|b| b.is_ascii_digit())
            .unwrap_or(false)
    };
    digit(0)
        && digit(1)
        && digit(2)
        && digit(3)
        && bytes.get(i + 4) == Some(&b'-')
        && digit(5)
        && digit(6)
        && bytes.get(i + 7) == Some(&b'-')
        && digit(8)
        && digit(9)
        && matches!(bytes.get(i + 10), Some(b' ') | Some(b'T'))
        && digit(11)
        && digit(12)
        && bytes.get(i + 13) == Some(&b':')
        && digit(14)
        && digit(15)
        && bytes.get(i + 16) == Some(&b':')
        && digit(17)
        && digit(18)
}

fn find_stamp(bytes: &[u8], text: &str, from: usize) -> Option<usize> {
    (from..bytes.len())
        .find(|i| text.is_char_boundary(*i) && i + 19 <= bytes.len() && is_stamp_at(bytes, *i))
}

/// 兜底：只认后面紧跟 `UTC±N` 的时间戳。
fn find_stamp_utc(bytes: &[u8], text: &str) -> Option<usize> {
    let mut i = 0;
    while i + 19 <= bytes.len() {
        if text.is_char_boundary(i) && is_stamp_at(bytes, i) {
            let rest = text.get(i + 19..).unwrap_or("");
            let rest = rest.strip_prefix(' ').unwrap_or(rest);
            if rest.starts_with("UTC+") || rest.starts_with("UTC-") {
                return Some(i);
            }
        }
        i += 1;
    }
    None
}

fn utc_offset(bytes: &[u8]) -> i64 {
    let mut i = 0;
    while i + 3 < bytes.len() {
        if &bytes[i..i + 3] == b"UTC" && (bytes[i + 3] == b'+' || bytes[i + 3] == b'-') {
            let sign = if bytes[i + 3] == b'-' { -1 } else { 1 };
            let mut j = i + 4;
            let mut hours: i64 = 0;
            let mut digits = 0;
            while j < bytes.len() && bytes[j].is_ascii_digit() && digits < 2 {
                hours = hours * 10 + (bytes[j] - b'0') as i64;
                j += 1;
                digits += 1;
            }
            if digits == 0 {
                i += 1;
                continue;
            }
            let mut minutes: i64 = 0;
            if bytes.get(j) == Some(&b':') {
                j += 1;
            }
            let mut mdigits = 0;
            while j < bytes.len() && bytes[j].is_ascii_digit() && mdigits < 2 {
                minutes = minutes * 10 + (bytes[j] - b'0') as i64;
                j += 1;
                mdigits += 1;
            }
            return sign * (hours * 3600 + minutes * 60);
        }
        i += 1;
    }
    0
}

/// `YYYY-MM-DD HH:MM:SS`（UTC 口径）→ epoch。
fn civil_to_epoch(year: i64, month: i64, day: i64, hour: i64, minute: i64, second: i64) -> i64 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let mp = (month + 9) % 12;
    let doy = (153 * mp + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146097 + doe - 719468;
    days * 86400 + hour * 3600 + minute * 60 + second
}

// --------------------------------------------------------------- 窗口探针

async fn fire_due(app: &Arc<App>) -> usize {
    let Some(_guard) = Guard::take(&WINDOW_BUSY) else {
        return 0;
    };
    if !window_probe_enabled(app) {
        return 0;
    }
    let dir = app.state.accounts_dir.clone();
    let now = now_epoch() as f64;
    let windows = load_windows(&dir, now);
    let mut due: Vec<Map<String, Value>> = windows
        .values()
        .filter_map(Value::as_object)
        .filter(|row| row_num(row, "reset_at") <= now)
        .cloned()
        .collect();
    due.sort_by(|a, b| {
        row_num(a, "reset_at")
            .partial_cmp(&row_num(b, "reset_at"))
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    let mut sent = 0;
    for row in due.into_iter().take(WINDOW_BATCH) {
        let uid = row_str(&row, "uid");
        let model = row_str(&row, "model");
        let account = app
            .state
            .accounts()
            .iter()
            .find(|a| a.uid() == uid)
            .cloned();
        let Some(account) = account else {
            app.log(
                "scheduler",
                "WARN",
                &format!(
                    "[窗口探针] 账号 [{}] 已不在池中，丢弃 {} 的窗口记录",
                    clip(&uid, 8),
                    model
                ),
            );
            drop_window(&dir, &uid, &model);
            continue;
        };
        // 停用或无凭证的账号不该被自动唤醒：保留记录，等它被重新启用。
        if !account.enabled() || account.access_token().is_empty() {
            continue;
        }
        sent += 1;
        fire(app, &account, &model).await;
        tokio::time::sleep(PROBE_GAP).await;
    }
    sent
}

async fn fire(app: &Arc<App>, account: &AccountFile, model: &str) {
    let dir = app.state.accounts_dir.clone();
    let uid = account.uid();
    let uid8 = clip(&uid, 8);
    let (ok, reset_at, detail) = probe_request(app, account, model).await;
    if ok {
        // 探针通了：这条配额线已经重置，立刻放回可用状态。
        drop_window(&dir, &uid, model);
        app.log(
            "scheduler",
            "INFO",
            &format!("[窗口探针] ✓ 账号 [{uid8}] 的 {model} 已开始新一轮计时（探针请求成功）"),
        );
        return;
    }
    let now = now_epoch() as f64;
    if let Some(reset) = reset_at {
        if reset > now + FRESH_WINDOW_MIN {
            // 上游给了更晚的重置点：这条线确实进入了新的等待期。
            update_window(&dir, &uid, model, reset, 0, &detail);
            let at = crate::state::local_stamp_for(reset as i64);
            app.log(
                "scheduler",
                "INFO",
                &format!("[窗口探针] 账号 [{uid8}] 的 {model} 仍在限流，下一轮重置 {at}"),
            );
            return;
        }
    }
    let attempts = load_windows(&dir, now)
        .get(&key(&uid, model))
        .and_then(Value::as_object)
        .map(|row| row_int(row, "attempts"))
        .unwrap_or(0)
        + 1;
    if attempts >= WINDOW_MAX_ATTEMPTS {
        drop_window(&dir, &uid, model);
        app.log(
            "scheduler",
            "WARN",
            &format!(
                "[窗口探针] 账号 [{uid8}] 的 {model} 连续 {attempts} 次未重置，放弃（下一次 429 会重新登记）"
            ),
        );
        return;
    }
    update_window(&dir, &uid, model, now + WINDOW_RETRY, attempts, &detail);
    app.log(
        "scheduler",
        "INFO",
        &format!(
            "[窗口探针] 账号 [{uid8}] 的 {model} 尚未重置（{}），{} 秒后重试 ({attempts}/{WINDOW_MAX_ATTEMPTS})",
            if detail.is_empty() { "无详情" } else { &detail },
            WINDOW_RETRY as i64
        ),
    );
}

// --------------------------------------------------------------- 额度扫描

/// 面板勾选的模型清单（`wb_settings._clean_quota_scan_models`）：去空白、去重、保序；
/// 手改的 settings.json 里可能是逗号/分号/换行分隔的字符串。
pub fn watched_models(app: &Arc<App>) -> Vec<String> {
    let settings = app.state.settings_snapshot();
    let mut out: Vec<String> = Vec::new();
    let mut push = |item: &str| {
        let name = item.trim();
        if !name.is_empty() && !out.iter().any(|x| x == name) {
            out.push(name.to_string());
        }
    };
    match settings.get("quota_scan_models") {
        Some(Value::Array(items)) => {
            for item in items {
                if let Some(s) = item.as_str() {
                    push(s);
                }
            }
        }
        Some(Value::String(text)) => {
            for item in text.split([',', ';', '\n']) {
                push(item);
            }
        }
        _ => {}
    }
    out
}

/// 这条配额线下一次该查的时刻。`last_scan=0` 表示从没查过，不能用它推导
/// （0 + 间隔是 1970 年，会混进 max() 里）。
fn due_at(row: &Map<String, Value>) -> f64 {
    let last_scan = row_num(row, "last_scan");
    let base = if last_scan > 0.0 {
        last_scan + SCAN_INTERVAL
    } else {
        0.0
    };
    base.max(row_num(row, "next_at"))
}

/// 一条从未查过的占位记录（`wb_quota_scan._new_row`）。`last_scan=0` 表示还没正经查过。
fn new_scan_row(uid: &str, model: &str, now: f64) -> Map<String, Value> {
    match json!({
        "uid": uid,
        "model": model,
        "last_scan": 0.0,
        "next_at": 0.0,
        "seen_at": now,
        "attempts": 0,
        "last_try": 0.0,
        "last_result": "",
        "last_detail": "",
    }) {
        Value::Object(map) => map,
        _ => Map::new(),
    }
}

/// 给从未登记过的组合写一条占位，并把首次查询按登记顺序排开
/// （刚勾上一堆模型时别在一瞬间把所有请求打上去）。
fn seed(scans: &mut Map<String, Value>, wanted: &[(String, String)], now: f64) {
    let mut delay = FIRST_DELAY;
    for (uid, model) in wanted {
        let k = key(uid, model);
        if scans.contains_key(&k) {
            continue;
        }
        let mut row = new_scan_row(uid, model, now);
        row.insert("next_at".into(), json!(now + delay));
        scans.insert(k, Value::Object(row));
        delay = (delay + FIRST_STAGGER).min(FIRST_DELAY_MAX);
    }
}

/// 查一轮到点的配额线，返回实际发出的请求数。
/// `force=true` 供面板「立即查询」：只跳过查询间隔，已经登记 429 窗口的线照样不动。
pub async fn scan_due(app: &Arc<App>, force: bool, limit: usize) -> usize {
    let Some(_guard) = Guard::take(&SCAN_BUSY) else {
        return 0;
    };
    let dir = app.state.accounts_dir.clone();
    let models = watched_models(app);
    if models.is_empty() {
        return 0;
    }
    let now = now_epoch() as f64;
    let pool_accounts: Vec<AccountFile> = app
        .state
        .accounts()
        .iter()
        .filter(|a| a.enabled() && !a.access_token().is_empty())
        .cloned()
        .collect();
    if pool_accounts.is_empty() {
        return 0;
    }

    let wanted: Vec<(String, String)> = models
        .iter()
        .flat_map(|m| pool_accounts.iter().map(move |a| (a.uid(), m.clone())))
        .collect();
    {
        let _lock = FILE_LOCK.lock().unwrap();
        let mut scans = load_scans(&dir, now);
        let before = scans.len();
        seed(&mut scans, &wanted, now);
        if scans.len() != before {
            save_scans(&dir, &scans);
        }
    }

    let windows = load_windows(&dir, now);
    let scans = load_scans(&dir, now);
    let mut due: Vec<(f64, String, AccountFile)> = Vec::new();
    for row in scans.values().filter_map(Value::as_object) {
        let model = row_str(row, "model");
        if !models.iter().any(|m| *m == model) {
            continue;
        }
        let uid = row_str(row, "uid");
        let Some(account) = pool_accounts.iter().find(|a| a.uid() == uid) else {
            continue; // 账号已不在池中：留着记录，等它回来
        };
        if windows.contains_key(&key(&uid, &model)) {
            continue; // 已有 429 记录 -> 交给窗口探针
        }
        if app.pool.cooling(&uid) {
            continue; // 正在冷却
        }
        let when = due_at(row);
        if !force && now < when {
            continue;
        }
        due.push((when, model, account.clone()));
    }
    due.sort_by(|a, b| {
        a.0.partial_cmp(&b.0)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.1.cmp(&b.1))
    });

    let mut sent = 0;
    for (_, model, account) in due.into_iter().take(limit.max(1)) {
        sent += 1;
        scan_one(app, &account, &model, now).await;
        tokio::time::sleep(SCAN_GAP).await;
    }
    sent
}

async fn scan_one(app: &Arc<App>, account: &AccountFile, model: &str, now: f64) {
    let dir = app.state.accounts_dir.clone();
    let uid = account.uid();
    let uid8 = clip(&uid, 8);
    let (ok, reset_at, detail) = probe_request(app, account, model).await;

    if ok {
        record(&dir, &uid, model, now, Some(now), 0.0, Some(0), "ok", "");
        app.log(
            "scheduler",
            "INFO",
            &format!("[额度查询] ✓ 账号 [{uid8}] 的 {model} 正常（未达限额）"),
        );
        return;
    }

    if let Some(reset) = reset_at {
        if reset > now + 5.0 {
            // 这条线已经打满了：登记窗口，之后本模块不再查它。
            let scheduled = schedule(app, &uid, model, reset);
            record(
                &dir,
                &uid,
                model,
                now,
                Some(now),
                0.0,
                Some(0),
                "throttled",
                &detail,
            );
            let at = crate::state::local_stamp_for(reset as i64);
            let tail = if scheduled {
                format!("已登记窗口（{at} 到点）")
            } else {
                format!("冷却至 {at}")
            };
            app.log(
                "scheduler",
                "WARN",
                &format!("[额度查询] 账号 [{uid8}] 的 {model} 已达限额，{tail}"),
            );
            return;
        }
    }

    let attempts = load_scans(&dir, now)
        .get(&key(&uid, model))
        .and_then(Value::as_object)
        .map(|row| row_int(row, "attempts"))
        .unwrap_or(0)
        + 1;
    let result = if detail.contains("429") {
        "throttled"
    } else {
        "error"
    };
    if attempts >= MAX_ATTEMPTS {
        // 连续失败说明不是一次抖动：推迟到下一个周期，别一直重试。
        record(
            &dir,
            &uid,
            model,
            now,
            Some(now),
            0.0,
            Some(0),
            result,
            &detail,
        );
        app.log(
            "scheduler",
            "WARN",
            &format!(
                "[额度查询] 账号 [{uid8}] 的 {model} 连续 {attempts} 次失败，推迟到下一个周期（{}）",
                if detail.is_empty() { "无详情" } else { &detail }
            ),
        );
        return;
    }
    record(
        &dir,
        &uid,
        model,
        now,
        None,
        now + RETRY_DELAY,
        Some(attempts),
        result,
        &detail,
    );
    app.log(
        "scheduler",
        "INFO",
        &format!(
            "[额度查询] 账号 [{uid8}] 的 {model} 未完成检查（{}），{} 秒后重试 ({attempts}/{MAX_ATTEMPTS})",
            if detail.is_empty() { "无详情" } else { &detail },
            RETRY_DELAY as i64
        ),
    );
}

/// 写回一条记录。`last_scan` 为空表示这次不算「查过」（失败只排重试时刻）。
#[allow(clippy::too_many_arguments)]
fn record(
    dir: &Path,
    uid: &str,
    model: &str,
    now: f64,
    last_scan: Option<f64>,
    next_at: f64,
    attempts: Option<i64>,
    result: &str,
    detail: &str,
) -> Map<String, Value> {
    let _guard = FILE_LOCK.lock().unwrap();
    let mut scans = load_scans(dir, now);
    let k = key(uid, model);
    let mut row = match scans.get(&k).and_then(Value::as_object).cloned() {
        Some(row) => row,
        None => new_scan_row(uid, model, now),
    };
    if let Some(stamp) = last_scan {
        row.insert(
            "last_scan".into(),
            json!(row_num(&row, "last_scan").max(stamp)),
        );
    }
    row.insert("next_at".into(), json!(next_at));
    let attempts = match attempts {
        Some(n) => n,
        None => row_int(&row, "attempts"),
    };
    row.insert("attempts".into(), json!(attempts));
    row.insert("last_try".into(), json!(now));
    row.insert("last_result".into(), json!(clip(result, 40)));
    row.insert("last_detail".into(), json!(clip(detail, 200)));
    scans.insert(k, Value::Object(row.clone()));
    save_scans(dir, &scans);
    row
}

/// 面板「模型额度查询」与调度条用的快照（按下一次应查时刻升序）。
pub fn scan_snapshot(app: &Arc<App>, limit: usize) -> Vec<Value> {
    let dir = app.state.accounts_dir.clone();
    let now = now_epoch() as f64;
    let watched = watched_models(app);
    let windows = load_windows(&dir, now);
    let scans = load_scans(&dir, now);
    let mut out: Vec<Value> = scans
        .values()
        .filter_map(Value::as_object)
        .map(|row| {
            let uid = row_str(row, "uid");
            let model = row_str(row, "model");
            json!({
                "uid": uid,
                "uid8": clip(&uid, 8),
                "model": model,
                "watched": watched.iter().any(|m| *m == model),
                "pending": windows.contains_key(&key(&uid, &model)),
                "last_scan": row_num(row, "last_scan"),
                "last_result": row_str(row, "last_result"),
                "last_detail": row_str(row, "last_detail"),
                "due_in": (due_at(row) - now).max(0.0) as i64,
            })
        })
        .collect();
    out.sort_by(|a, b| {
        let pick = |v: &Value, k: &str| {
            v.get(k)
                .and_then(Value::as_i64)
                .or_else(|| v.get(k).and_then(Value::as_str).map(|_| 0))
                .unwrap_or(0)
        };
        pick(a, "due_in")
            .cmp(&pick(b, "due_in"))
            .then_with(|| {
                let x = a.get("model").and_then(Value::as_str).unwrap_or("");
                let y = b.get("model").and_then(Value::as_str).unwrap_or("");
                x.cmp(y)
            })
            .then_with(|| {
                let x = a.get("uid").and_then(Value::as_str).unwrap_or("");
                let y = b.get("uid").and_then(Value::as_str).unwrap_or("");
                x.cmp(y)
            })
    });
    out.truncate(limit.max(1));
    out
}

/// 巡检循环每跳调一次：先跑到点的窗口探针，再查一轮到点的配额线。
pub async fn tick(app: Arc<App>) {
    fire_due(&app).await;
    scan_due(&app, false, BATCH_LIMIT).await;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reset_time_is_read_from_both_upstream_phrasings() {
        // 上游原文：2026-09-19 18:29:03 UTC+8 / 2026-10-02 16:00:34 UTC+8
        assert_eq!(
            parse_reset("your usage will reset at 2026-09-19 18:29:03 UTC+8"),
            Some(1789813743.0)
        );
        assert_eq!(
            parse_reset("\"您的使用量已超出频率限制，将在 2026-10-02 16:00:34 UTC+8 重置\""),
            Some(1790928034.0)
        );
        // 转义过的体也要认：\u5c06\u5728 = 将在
        assert_eq!(
            parse_reset("\\u5c06\\u5728 2026-10-02 16:00:34 UTC+8 \\u91cd\\u7f6e"),
            Some(1790928034.0)
        );
        assert_eq!(parse_reset("no timestamp here"), None);
    }

    #[test]
    fn due_time_ignores_never_scanned_rows() {
        let mut row = Map::new();
        row.insert("last_scan".into(), json!(0.0));
        row.insert("next_at".into(), json!(500.0));
        assert_eq!(due_at(&row), 500.0);
        row.insert("last_scan".into(), json!(1000.0));
        assert_eq!(due_at(&row), 1000.0 + SCAN_INTERVAL);
    }

    #[test]
    fn seeded_rows_stagger_and_survive_a_round_trip() {
        let dir = std::env::temp_dir().join(format!("wb-quota-{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let now = 1_000_000.0;
        let wanted: Vec<(String, String)> = (0..3)
            .map(|i| (format!("uid{i}"), "m".to_string()))
            .collect();
        let wait = |scans: &Map<String, Value>, uid: &str| {
            row_num(
                scans.get(&key(uid, "m")).unwrap().as_object().unwrap(),
                "next_at",
            ) - now
        };
        let mut scans = Map::new();
        seed(&mut scans, &wanted, now);
        assert_eq!(wait(&scans, "uid0"), FIRST_DELAY);
        assert_eq!(wait(&scans, "uid1"), FIRST_DELAY + FIRST_STAGGER);
        // 再 seed 一次不覆盖已有记录（否则每次调用都把首次查询推后）
        seed(&mut scans, &wanted, now + 60.0);
        assert_eq!(wait(&scans, "uid0"), FIRST_DELAY);
        save_scans(&dir, &scans);
        let back = load_scans(&dir, now);
        assert_eq!(back.len(), 3);
        assert_eq!(
            row_str(
                back.get(&key("uid0", "m")).unwrap().as_object().unwrap(),
                "model"
            ),
            "m"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
}
