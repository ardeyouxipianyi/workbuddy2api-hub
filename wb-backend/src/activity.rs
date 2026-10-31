//! 账号每日活动历史（签到 / 国际活跃）：`usage/activity_history.jsonl` 追加写。
//!
//! 面板「签到与活跃记录」表和积分获取历史的 `action` 关联都读这里。读取路径
//! 一律先投影到允许字段——文件可能被别的写入方或手工改过，一行合法 JSON 也
//! 可能夹带 accessToken 之类的键，不能原样交出去。
//!
//! shortcut: 只记 checkin 与 daily_chat 两类（Python 的 TASKS 也是这两类），
//! 自动整理（compact，5000 行/90 天）没移植，文件会一直长。

use serde_json::{json, Value};

use crate::App;

const HISTORY_NAME: &str = "activity_history.jsonl";
const TASKS: &[&str] = &["checkin", "daily_chat"];
const TRIGGERS: &[&str] = &["scheduler", "manual", "account_add", "account_import", "unknown"];
const RANGES: &[&str] = &["today", "1d", "7d", "30d", "90d", "all"];

fn history_path(app: &App) -> std::path::PathBuf {
    app.state.usage_dir.join(HISTORY_NAME)
}

/// 本地时间带偏移量（`2026-10-31T16:04:35+08:00`），面板直接按本地显示。
fn timestamp() -> String {
    let epoch = crate::state::now_epoch();
    let offset = unsafe {
        let tm = libc::localtime(&epoch as *const i64);
        if tm.is_null() {
            0
        } else {
            (*tm).tm_gmtoff
        }
    };
    let sign = if offset < 0 { '-' } else { '+' };
    let offset = offset.abs();
    format!(
        "{}{}{:02}:{:02}",
        crate::state::local_stamp().replace(' ', "T"),
        sign,
        offset / 3600,
        (offset % 3600) / 60
    )
}

/// 追加一次尝试。写不进去只记日志，绝不把一次成功的签到变成失败。
pub fn record(app: &App, uid: &str, nickname: &str, realm: &str, task: &str, trigger: &str, ok: bool, message: &str) {
    use std::io::Write;
    let row = json!({
        "ts": timestamp(),
        "uid": uid,
        "nickname": nickname,
        "realm": if realm == "cn" || realm == "intl" { realm } else { "" },
        "task": if TASKS.contains(&task) { task } else { "checkin" },
        "trigger": if TRIGGERS.contains(&trigger) { trigger } else { "unknown" },
        "ok": ok,
        "message": message.chars().take(200).collect::<String>(),
    });
    let path = history_path(app);
    let write = || -> std::io::Result<()> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let mut file = std::fs::OpenOptions::new().create(true).append(true).open(&path)?;
        writeln!(file, "{}", serde_json::to_string(&row).unwrap_or_default())
    };
    if let Err(e) = write() {
        app.log("activity", "WARN", &format!("活动历史写入失败: {e}"));
    }
}

/// `today` 走本地日历零点，其余是滚动天数，`all` 不设下界。
fn range_cutoff(range: &str) -> Result<Option<f64>, String> {
    if !RANGES.contains(&range) {
        return Err(format!("range must be one of: {}", RANGES.join(", ")));
    }
    if range == "all" {
        return Ok(None);
    }
    if range == "today" {
        let midnight = format!("{} 00:00:00", &crate::state::local_stamp()[..10]);
        return Ok(crate::panel::stamp_epoch_pub(&midnight));
    }
    let days: f64 = match range {
        "1d" => 1.0,
        "7d" => 7.0,
        "30d" => 30.0,
        _ => 90.0,
    };
    Ok(Some(crate::state::now_epoch() as f64 - days * 86400.0))
}

fn allowed(row: &Value) -> Value {
    let boolish = |v: Option<&Value>| match v {
        Some(Value::Bool(b)) => *b,
        Some(Value::String(s)) => matches!(s.as_str(), "1" | "true" | "yes" | "ok"),
        Some(Value::Number(n)) => n.as_f64().unwrap_or(0.0) != 0.0,
        _ => false,
    };
    let text = |key: &str, max: usize| -> String {
        row.get(key).and_then(Value::as_str).unwrap_or("").chars().take(max).collect()
    };
    let realm = text("realm", 8);
    let task = text("task", 16);
    let trigger = text("trigger", 20);
    json!({
        "ts": text("ts", 40),
        "uid": text("uid", 80),
        "nickname": text("nickname", 120),
        "realm": if realm == "cn" || realm == "intl" { realm } else { String::new() },
        "task": if TASKS.contains(&task.as_str()) { task } else { String::new() },
        "trigger": if TRIGGERS.contains(&trigger.as_str()) { trigger } else { "unknown".to_string() },
        "ok": boolish(row.get("ok")),
        "message": text("message", 200),
    })
}

/// `GET /activity/history`：最新在前，筛选在服务端做完。
pub fn query(app: &App, range: &str, uid: &str, task: &str, result: &str, limit: &str) -> Result<Value, String> {
    let range = if range.trim().is_empty() { "7d" } else { range.trim() };
    let cutoff = range_cutoff(range)?;
    if !task.is_empty() && !TASKS.contains(&task) {
        return Err(format!("task must be one of: {}", TASKS.join(", ")));
    }
    let result = result.trim().to_lowercase();
    let want_ok = match result.as_str() {
        "" => None,
        "ok" | "success" | "succeeded" => Some(true),
        "failed" | "fail" | "error" => Some(false),
        other => return Err(format!("result must be one of: ok, failed (got {other})")),
    };
    let limit: usize = limit.trim().parse().unwrap_or(100).clamp(1, 1000);

    let raw = std::fs::read_to_string(history_path(app)).unwrap_or_default();
    let mut matched: Vec<Value> = Vec::new();
    // 追加序即时间序，倒过来就是最新在前。
    for line in raw.lines().rev() {
        let Ok(row) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        let row = allowed(&row);
        if !uid.is_empty() && row["uid"].as_str() != Some(uid) {
            continue;
        }
        if !task.is_empty() && row["task"].as_str() != Some(task) {
            continue;
        }
        if let Some(want) = want_ok {
            if row["ok"].as_bool() != Some(want) {
                continue;
            }
        }
        if let Some(cutoff) = cutoff {
            // 读不出时间就不能算「在区间内」，否则一条坏行能绕过下界。
            let ts = crate::panel::stamp_epoch_pub(row["ts"].as_str().unwrap_or(""));
            if ts.map(|t| t < cutoff).unwrap_or(true) {
                continue;
            }
        }
        matched.push(row);
    }
    let total = matched.len();
    matched.truncate(limit);
    Ok(json!({
        "rows": matched,
        "total": total,
        "limit": limit,
        "range": range,
        "filters": {"uid": uid, "task": task, "result": result},
    }))
}

/// uid → [(epoch, task, ok)]，只含签到与每日活跃的真实尝试（按时间升序）。
pub fn grant_actions(app: &App) -> std::collections::HashMap<String, Vec<(f64, String, bool)>> {
    let raw = std::fs::read_to_string(history_path(app)).unwrap_or_default();
    let mut out: std::collections::HashMap<String, Vec<(f64, String, bool)>> = Default::default();
    for line in raw.lines() {
        let Ok(row) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        let row = allowed(&row);
        let Some(at) = crate::panel::stamp_epoch_pub(row["ts"].as_str().unwrap_or("")) else {
            continue;
        };
        let uid = row["uid"].as_str().unwrap_or("");
        if uid.is_empty() {
            continue;
        }
        out.entry(uid.to_string()).or_default().push((
            at,
            row["task"].as_str().unwrap_or("").to_string(),
            row["ok"].as_bool().unwrap_or(false),
        ));
    }
    for list in out.values_mut() {
        list.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
    }
    out
}

/// 发放时刻对应的那次动作；窗口与 Python 的 GRANT_ACTION_BEFORE/AFTER 一致。
pub fn grant_action(
    actions: &std::collections::HashMap<String, Vec<(f64, String, bool)>>,
    uid: &str,
    created_at: f64,
) -> Option<(String, String, bool)> {
    let list = actions.get(uid)?;
    let mut best: Option<&(f64, String, bool)> = None;
    for entry in list {
        let delta = entry.0 - created_at;
        if delta >= -600.0 && delta <= 2.0 * 3600.0 && entry.2 {
            best = Some(entry);
        }
        if entry.0 > created_at + 600.0 {
            break;
        }
    }
    let entry = best?;
    Some((entry.1.clone(), format_stamp_iso(entry.0 as i64), entry.2))
}

fn format_stamp_iso(epoch: i64) -> String {
    let offset = unsafe {
        let tm = libc::localtime(&epoch as *const i64);
        if tm.is_null() { 0 } else { (*tm).tm_gmtoff }
    };
    let sign = if offset < 0 { '-' } else { '+' };
    let offset = offset.abs();
    format!(
        "{}{}{:02}:{:02}",
        crate::state::local_stamp_for(epoch),
        sign,
        offset / 3600,
        (offset % 3600) / 60
    )
}
