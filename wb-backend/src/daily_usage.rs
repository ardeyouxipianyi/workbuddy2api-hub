//! Incremental fold of today's usage log (`wb_proxy.daily_usage_stats`).

use std::collections::HashMap;
use std::fs::File;
use std::io::{BufRead, BufReader, Seek, SeekFrom};
use std::path::Path;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::Value;

#[derive(Clone, Debug, Default, PartialEq)]
pub struct DailyUsage {
    pub tokens: HashMap<String, i64>,
    pub credits: HashMap<String, f64>,
    pub models: HashMap<String, HashMap<String, i64>>,
}

#[derive(Default)]
struct CacheState {
    day: String,
    totals: Option<DailyUsage>,
    offset: u64,
    at: f64,
}

pub struct DailyUsageCache {
    state: Mutex<CacheState>,
    ttl: f64,
}

impl Default for DailyUsageCache {
    fn default() -> Self {
        Self {
            state: Mutex::new(CacheState::default()),
            ttl: std::env::var("WB_STATS_TTL")
                .ok()
                .and_then(|text| text.trim().parse::<f64>().ok())
                .filter(|ttl| *ttl >= 0.0)
                .unwrap_or(15.0),
        }
    }
}

impl DailyUsageCache {
    /// `ttl=0` forces a fold, matching `daily_usage_stats(ttl=0)`.
    pub fn stats(&self, path: &Path, ttl: Option<f64>) -> Option<DailyUsage> {
        let now = epoch();
        let Some(day) = local_day(now) else {
            return None;
        };
        let midnight = local_midnight(now)?;
        let mut cache = self.state.lock().unwrap_or_else(|error| error.into_inner());
        let ttl = ttl.unwrap_or(self.ttl);
        if cache.day == day && cache.totals.is_some() && now - cache.at < ttl {
            return cache.totals.clone();
        }

        let mut totals = if cache.day == day {
            cache.totals.clone().unwrap_or_default()
        } else {
            DailyUsage::default()
        };
        let mut offset = cache.offset;
        let size = std::fs::metadata(path).map(|meta| meta.len()).unwrap_or(0);
        if offset > size {
            totals = DailyUsage::default();
            offset = 0;
        }

        match scan(path, offset, midnight, totals) {
            Ok((totals, offset)) => {
                *cache = CacheState {
                    day,
                    totals: Some(totals.clone()),
                    offset,
                    at: now,
                };
                Some(totals)
            }
            Err(error) => {
                *cache = CacheState {
                    day,
                    totals: None,
                    offset: 0,
                    at: now,
                };
                eprintln!("wb-backend: daily usage scan failed: {error}");
                None
            }
        }
    }
}

fn scan(
    path: &Path,
    mut offset: u64,
    midnight: f64,
    mut totals: DailyUsage,
) -> std::io::Result<(DailyUsage, u64)> {
    let mut reader = BufReader::new(File::open(path)?);
    reader.seek(SeekFrom::Start(offset))?;
    let mut line = String::new();
    loop {
        line.clear();
        let read = reader.read_line(&mut line)?;
        if read == 0 {
            break;
        }
        if !line.ends_with('\n') {
            break;
        }
        offset += read as u64;
        let text = line.trim();
        if text.is_empty() {
            continue;
        }
        let Ok(row) = serde_json::from_str::<Value>(text) else {
            continue;
        };
        let at = number(row.get("at")).unwrap_or(0.0);
        if at < midnight {
            continue;
        }
        if row.get("outcome").and_then(Value::as_str) == Some("client_aborted") {
            continue;
        }
        let Some(uid) = row
            .get("account")
            .and_then(Value::as_str)
            .filter(|uid| !uid.is_empty())
        else {
            continue;
        };
        let tokens = number(row.get("total_tokens")).unwrap_or(0.0) as i64;
        *totals.tokens.entry(uid.to_string()).or_default() += tokens;
        let credit = number(row.get("credit")).unwrap_or(0.0);
        if credit != 0.0 {
            *totals.credits.entry(uid.to_string()).or_default() += credit;
        }
        if let Some(model) = row
            .get("model")
            .and_then(Value::as_str)
            .filter(|model| !model.is_empty())
        {
            *totals
                .models
                .entry(uid.to_string())
                .or_default()
                .entry(model.to_string())
                .or_default() += tokens;
        }
    }
    Ok((totals, offset))
}

fn number(value: Option<&Value>) -> Option<f64> {
    value.and_then(|value| {
        value
            .as_f64()
            .or_else(|| value.as_i64().map(|number| number as f64))
    })
}

fn epoch() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(0.0)
}

fn local_tm(now: f64) -> Option<libc::tm> {
    let stamp = now as libc::time_t;
    let mut value = std::mem::MaybeUninit::<libc::tm>::uninit();
    let result = unsafe { libc::localtime_r(&stamp, value.as_mut_ptr()) };
    if result.is_null() {
        None
    } else {
        Some(unsafe { value.assume_init() })
    }
}

fn local_day(now: f64) -> Option<String> {
    let tm = local_tm(now)?;
    Some(format!(
        "{:04}-{:02}-{:02}",
        tm.tm_year + 1900,
        tm.tm_mon + 1,
        tm.tm_mday
    ))
}

fn local_midnight(now: f64) -> Option<f64> {
    let mut tm = local_tm(now)?;
    tm.tm_hour = 0;
    tm.tm_min = 0;
    tm.tm_sec = 0;
    tm.tm_isdst = -1;
    let stamp = unsafe { libc::mktime(&mut tm) };
    (stamp >= 0).then_some(stamp as f64)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn temp_path(name: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!(
            "wb-daily-{name}-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ))
    }

    #[test]
    fn fold_matches_python_boundaries_and_resumes_from_offset() {
        let path = temp_path("fold.jsonl");
        let now = epoch();
        let midnight = local_midnight(now).unwrap() as i64;
        let rows = [
            format!(
                r#"{{"at":{},"account":"a","model":"gpt","total_tokens":100,"credit":5}}"#,
                midnight - 1
            ),
            format!(
                r#"{{"at":{},"account":"a","model":"gpt","total_tokens":50,"credit":1.5}}"#,
                midnight
            ),
            format!(
                r#"{{"at":{},"account":"b","model":"free","total_tokens":25,"credit":0}}"#,
                midnight + 1
            ),
            format!(
                r#"{{"at":{},"account":"a","model":"gpt","total_tokens":99,"credit":9,"outcome":"client_aborted"}}"#,
                midnight + 2
            ),
        ];
        std::fs::write(&path, rows.join("\n") + "\n").unwrap();
        let cache = DailyUsageCache::default();
        let first = cache.stats(&path, Some(0.0)).unwrap();
        assert_eq!(first.tokens.get("a"), Some(&50));
        assert_eq!(first.tokens.get("b"), Some(&25));
        assert_eq!(first.credits.get("a"), Some(&1.5));
        assert_eq!(first.models["a"]["gpt"], 50);

        let mut file = std::fs::OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap();
        writeln!(
            file,
            r#"{{"at":{},"account":"b","model":"paid","total_tokens":10,"credit":2}}"#,
            midnight + 3
        )
        .unwrap();
        let second = cache.stats(&path, Some(0.0)).unwrap();
        assert_eq!(second.tokens.get("b"), Some(&35));
        assert_eq!(second.credits.get("b"), Some(&2.0));
        assert_eq!(second.models["b"]["paid"], 10);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn partial_tail_is_not_counted_or_consumed() {
        let path = temp_path("partial.jsonl");
        let midnight = local_midnight(epoch()).unwrap() as i64;
        let complete = format!(
            r#"{{"at":{},"account":"a","model":"gpt","total_tokens":4}}"#,
            midnight
        );
        std::fs::write(&path, format!("{complete}\n")).unwrap();
        let mut file = std::fs::OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap();
        write!(file, r#"{{"at":{},"account":"a""#, midnight).unwrap();
        let cache = DailyUsageCache::default();
        assert_eq!(cache.stats(&path, Some(0.0)).unwrap().tokens["a"], 4);
        file.write_all(b",\"model\":\"gpt\",\"total_tokens\":6}\n")
            .unwrap();
        assert_eq!(cache.stats(&path, Some(0.0)).unwrap().tokens["a"], 10);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn missing_log_is_unknown_not_zero() {
        let path = temp_path("missing.jsonl");
        assert_eq!(DailyUsageCache::default().stats(&path, Some(0.0)), None);
    }
}
