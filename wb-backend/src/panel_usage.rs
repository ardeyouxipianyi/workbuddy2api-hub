//! Panel usage aggregates. The Python log is the single input to every view here.

use std::collections::BTreeMap;
use std::sync::Arc;

use hyper::body::Incoming;
use hyper::{Request, StatusCode};
use serde_json::{json, Value};

use crate::http::{json_resp, Resp};
use crate::App;

const DAY_SECONDS: f64 = 86_400.0;

fn read_rows(app: &App) -> Arc<Vec<Value>> {
    load_rows(&app.recorder_path())
}

/// 解析结果按 (路径, mtime, 长度) 缓存一次，所有面板视图共用同一份。
/// 以前每个请求都重读整份 usage.jsonl（16 MB → 约 55 MB 的 Value），面板每 5 秒
/// 并发打 /usage、/usage/perf、/usage/analytics，几次叠起来就是 500 MB ——
/// CT412 的内存就是这么爆的。
fn load_rows(path: &std::path::Path) -> Arc<Vec<Value>> {
    static CACHE: std::sync::OnceLock<std::sync::Mutex<(std::path::PathBuf, u64, u64, Arc<Vec<Value>>)>> =
        std::sync::OnceLock::new();
    let stamp = std::fs::metadata(path)
        .map(|meta| {
            let mtime = meta
                .modified()
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_millis() as u64)
                .unwrap_or(0);
            (mtime, meta.len())
        })
        .unwrap_or((0, 0));
    let cache = CACHE.get_or_init(|| {
        std::sync::Mutex::new((std::path::PathBuf::new(), 0, 0, Arc::new(Vec::new())))
    });
    let mut guard = cache.lock().unwrap_or_else(|error| error.into_inner());
    if guard.0 == path && guard.1 == stamp.0 && guard.2 == stamp.1 {
        return guard.3.clone();
    }
    let rows: Vec<Value> = std::fs::read_to_string(path)
        .map(|text| {
            text.lines()
                .filter(|line| !line.trim().is_empty())
                .filter_map(|line| serde_json::from_str(line).ok())
                .collect()
        })
        .unwrap_or_default();
    let shared = Arc::new(rows);
    *guard = (path.to_path_buf(), stamp.0, stamp.1, shared.clone());
    shared
}

fn query<'a>(req: &'a Request<Incoming>, name: &str) -> Option<&'a str> {
    req.uri()
        .query()
        .unwrap_or("")
        .split('&')
        .find_map(|pair| pair.strip_prefix(name)?.strip_prefix('='))
        .filter(|value| !value.is_empty())
}

fn requested_realm(req: &Request<Incoming>, app: &App) -> String {
    if let Some(value) = query(req, "realm") {
        if matches!(value, "cn" | "intl" | "all") {
            return value.to_string();
        }
    }
    if let Some(value) = req.headers().get("x-realm").and_then(|value| value.to_str().ok()) {
        if matches!(value, "cn" | "intl" | "all") {
            return value.to_string();
        }
    }
    app.state.current_realm()
}

fn local_midnight(days_back: i32) -> Option<f64> {
    let now = crate::state::now_epoch();
    let pointer = unsafe { libc::localtime(&now) };
    if pointer.is_null() {
        return None;
    }
    let mut calendar = unsafe { *pointer };
    calendar.tm_mday -= days_back;
    calendar.tm_hour = 0;
    calendar.tm_min = 0;
    calendar.tm_sec = 0;
    let result = unsafe { libc::mktime(&mut calendar) };
    (result >= 0).then_some(result as f64)
}

fn epoch_or_none(value: Option<&str>) -> Option<f64> {
    value
        .filter(|value| !value.is_empty())
        .and_then(|value| value.parse::<f64>().ok())
        .filter(|value| *value >= 0.0)
}

fn query_string_value<'a>(query: &'a str, name: &str) -> Option<&'a str> {
    query
        .split('&')
        .find_map(|pair| {
            let (key, value) = pair.split_once('=')?;
            (key == name).then_some(value)
        })
        .filter(|value| !value.is_empty())
}

fn python_weekday(libc_weekday: i32) -> i32 {
    (libc_weekday + 6) % 7
}

fn range_bounds_query(query: &str) -> (Option<f64>, Option<f64>) {
    let value = query_string_value(query, "range")
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    if matches!(value.as_str(), "today" | "day" | "1d") {
        return (local_midnight(0), None);
    }
    if matches!(value.as_str(), "week" | "w") {
        let now = crate::state::now_epoch();
        let pointer = unsafe { libc::localtime(&now) };
        let weekday = if pointer.is_null() {
            0
        } else {
            // libc uses Sunday=0; Python's tm_wday uses Monday=0.
            python_weekday(unsafe { (*pointer).tm_wday })
        };
        return (local_midnight(weekday), None);
    }
    if matches!(value.as_str(), "month" | "m") {
        let now = crate::state::now_epoch();
        let pointer = unsafe { libc::localtime(&now) };
        if pointer.is_null() {
            return (None, None);
        }
        let mut calendar = unsafe { *pointer };
        calendar.tm_mday = 1;
        calendar.tm_hour = 0;
        calendar.tm_min = 0;
        calendar.tm_sec = 0;
        let start = unsafe { libc::mktime(&mut calendar) };
        return ((start >= 0).then_some(start as f64), None);
    }
    if value == "custom" {
        let mut since = epoch_or_none(query_string_value(query, "since"));
        let mut until = epoch_or_none(query_string_value(query, "until"));
        if since.zip(until).is_some_and(|(low, high)| high < low) {
            std::mem::swap(&mut since, &mut until);
        }
        return (since, until);
    }
    (None, None)
}

fn range_bounds(req: &Request<Incoming>) -> (Option<f64>, Option<f64>) {
    range_bounds_query(req.uri().query().unwrap_or(""))
}

fn row_at(row: &Value) -> Option<f64> {
    row.get("at").and_then(|value| {
        value
            .as_f64()
            .or_else(|| value.as_str().and_then(|text| text.parse().ok()))
    })
}

fn row_realm<'a>(row: &'a Value, account_realms: &'a BTreeMap<String, String>) -> &'a str {
    row.get("realm")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .or_else(|| {
            row.get("account")
                .and_then(Value::as_str)
                .and_then(|uid| account_realms.get(uid).map(String::as_str))
        })
        .unwrap_or("")
}

fn account_realms(app: &App) -> BTreeMap<String, String> {
    app.state
        .accounts_snapshot()
        .into_iter()
        .map(|account| (account.uid(), account.realm()))
        .collect()
}

fn rows_for_realm<'a>(
    rows: &'a [Value],
    realm: &str,
    realms: &'a BTreeMap<String, String>,
) -> Vec<&'a Value> {
    rows.iter()
        .filter(|row| realm == "all" || row_realm(row, realms) == realm)
        .collect()
}

fn number(row: &Value, key: &str) -> f64 {
    row.get(key).and_then(Value::as_f64).unwrap_or(0.0)
}

fn integer(row: &Value, key: &str) -> i64 {
    row.get(key).and_then(Value::as_i64).unwrap_or(0)
}

fn epoch_now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(0.0)
}

fn outcome(row: &Value) -> &str {
    row.get("outcome").and_then(Value::as_str).unwrap_or("")
}

fn representative(app: &App, realm: &str) -> Option<crate::state::AccountFile> {
    let now = crate::state::now_epoch();
    let accounts = app.state.accounts_snapshot();
    accounts
        .iter()
        .find(|account| account.realm() == realm && account.ready(now))
        .or_else(|| accounts.iter().find(|account| account.realm() == realm))
        .cloned()
}

mod analytics;
mod perf;
mod remaining;
mod summary;
mod timeseries;

pub use analytics::analytics;
pub use perf::perf;
pub use remaining::remaining;
pub use summary::{by_account, overview, recent};
pub use timeseries::timeseries;

#[cfg(test)]
mod tests {
    use super::{load_rows, python_weekday, range_bounds_query};
    use std::sync::Arc;

    #[test]
    fn usage_rows_are_parsed_once_and_refreshed_on_change() {
        let dir = std::env::temp_dir().join(format!("wb-rows-{}", crate::sse::hex_token(8)));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("usage.jsonl");
        std::fs::write(&path, "{\"at\": 100}\n").unwrap();
        let first = load_rows(&path);
        let again = load_rows(&path);
        assert_eq!(first.len(), 1);
        assert!(Arc::ptr_eq(&first, &again), "缓存命中应复用同一份 Arc");
        // 文件长大之后必须重新解析，否则面板永远看不到新请求。
        std::thread::sleep(std::time::Duration::from_millis(20));
        std::fs::write(&path, "{\"at\": 100}\n{\"at\": 200}\n").unwrap();
        let refreshed = load_rows(&path);
        assert_eq!(refreshed.len(), 2);
        assert!(!Arc::ptr_eq(&first, &refreshed));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn custom_range_swaps_reversed_bounds_and_drops_invalid_values() {
        assert_eq!(
            range_bounds_query("range=custom&since=200&until=100"),
            (Some(100.0), Some(200.0))
        );
        assert_eq!(
            range_bounds_query("range=custom&since=-1&until=bad"),
            (None, None)
        );
    }

    #[test]
    fn unknown_range_keeps_the_all_time_snapshot() {
        assert_eq!(range_bounds_query("range=rolling-7d"), (None, None));
    }

    #[test]
    fn week_uses_python_monday_zero_even_when_libc_reports_sunday_zero() {
        assert_eq!(python_weekday(1), 0);
        assert_eq!(python_weekday(0), 6);
    }
}
