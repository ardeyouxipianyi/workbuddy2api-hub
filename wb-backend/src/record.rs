//! usage 行（wb_proxy.py:847-931 `record_usage`）。
//!
//! 字段顺序就是写入顺序，面板/聚合都按名字读，但逐行比对（harness）看顺序，
//! 所以这里用有序 Map 按原文顺序插入，不要改成 struct + serde（那样顺序会被
//! 字段声明绑死，也难对齐「条件字段」）。
//!
//! shortcut: `cost_policy` 恒为 null —— 计价总开关默认关，开启后由 wb_pricing
//! 铸策略 id，那是 M4 面板/计价一起做的活。

use serde_json::{json, Map, Value};

use crate::fingerprint::Fingerprint;
use crate::usage::Recorder;

pub struct Row<'a> {
    pub model: &'a str,
    pub stream: bool,
    pub outcome: &'a str,
    pub elapsed_ms: Option<i64>,
    pub ttft_ms: Option<i64>,
    pub gen_ms: Option<i64>,
    pub requested_model: Option<&'a str>,
    pub usage: Option<&'a Value>,
    pub fp: Option<&'a Fingerprint>,
    pub account: Option<&'a str>,
    pub realm: &'a str,
    pub key: &'a str,
    pub effort: Option<&'a str>,
}

fn int_of(v: Option<&Value>) -> i64 {
    v.and_then(Value::as_i64).unwrap_or(0)
}

/// `_best_cached_tokens`（wb_proxy.py:817-844）：六个别名里第一个正数。
pub fn best_cached_tokens(usage: &Value) -> i64 {
    let pick = |path: &[&str]| -> i64 {
        let mut cur = usage;
        for key in path {
            match cur.get(*key) {
                Some(next) => cur = next,
                None => return 0,
            }
        }
        cur.as_i64().or_else(|| cur.as_f64().map(|f| f as i64)).unwrap_or(0)
    };
    for value in [
        pick(&["prompt_tokens_details", "cached_tokens"]),
        pick(&["prompt_cache_hit_tokens"]),
        pick(&["cache_read_input_tokens"]),
        pick(&["cached_tokens"]),
        pick(&["input_tokens_details", "cached_tokens"]),
        pick(&["completion_tokens_details", "cached_tokens"]),
    ] {
        if value > 0 {
            return value;
        }
    }
    0
}

pub fn record(recorder: &Recorder, row: Row<'_>) {
    let mut out = Map::new();
    // 1-2：完成时刻（本地时区，无 TZ 后缀，与 Python time.strftime 一致）。
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    out.insert("at".into(), json!(now.as_secs_f64()));
    out.insert("iso".into(), json!(local_iso(now.as_secs() as i64)));
    // 3-8
    out.insert("model".into(), json!(row.model));
    out.insert("stream".into(), json!(row.stream));
    out.insert("outcome".into(), json!(row.outcome));
    out.insert("elapsed_ms".into(), json!(row.elapsed_ms));
    out.insert("ttft_ms".into(), json!(row.ttft_ms));
    out.insert("gen_ms".into(), json!(row.gen_ms));
    if let Some(requested) = row.requested_model {
        if requested != row.model {
            out.insert("requested_model".into(), json!(requested));
        }
    }

    let fields = row.usage;
    let mut cached = 0;
    let mut prompt_tokens = 0;
    let mut completion_tokens = 0;
    match fields {
        Some(usage) => {
            prompt_tokens = int_of(usage.get("prompt_tokens"));
            completion_tokens = int_of(usage.get("completion_tokens"));
            cached = best_cached_tokens(usage);
            out.insert("prompt_tokens".into(), json!(prompt_tokens));
            out.insert("completion_tokens".into(), json!(completion_tokens));
            out.insert(
                "reasoning_tokens".into(),
                json!(int_of(usage.pointer("/completion_tokens_details/reasoning_tokens"))),
            );
            out.insert("cached_tokens".into(), json!(cached));
            out.insert("total_tokens".into(), json!(int_of(usage.get("total_tokens"))));
            out.insert("credit".into(), usage.get("credit").cloned().unwrap_or(json!(0)));
        }
        None => {
            out.insert("usage_missing".into(), json!(true));
        }
    }

    if let Some(fp) = row.fp {
        out.insert("msgs_sha".into(), json!(fp.msgs_sha));
        out.insert("n_msgs".into(), json!(fp.n_msgs));
        if let Some(system_sha) = &fp.system_sha {
            out.insert("system_sha".into(), json!(system_sha));
        }
        if let Some(prefix_sha) = &fp.prefix_sha {
            out.insert("prefix_sha".into(), json!(prefix_sha));
        }
    }
    if let Some(account) = row.account {
        out.insert("account".into(), json!(account));
    }
    out.insert("realm".into(), json!(row.realm));
    out.insert("key".into(), json!(row.key));
    out.insert("cost_policy".into(), Value::Null);
    if let Some(effort) = row.effort {
        out.insert("reasoning_effort".into(), json!(effort));
    }
    if let Some(gen_ms) = row.gen_ms {
        if gen_ms > 0 {
            let tps = (completion_tokens as f64 / (gen_ms as f64 / 1000.0) * 100.0).round() / 100.0;
            out.insert("tokens_per_sec".into(), json!(tps));
        }
    }
    if prompt_tokens > 0 {
        let pct = (cached as f64 * 100.0 / prompt_tokens as f64 * 10.0).round() / 10.0;
        out.insert("cache_hit_pct".into(), json!(pct));
    }
    recorder.append(&Value::Object(out));
}

/// `time.strftime("%Y-%m-%dT%H:%M:%S")` 的本地时间，不依赖 libc 的 TZ 处理。
fn local_iso(epoch: i64) -> String {
    let offset = local_offset_seconds(epoch);
    let t = epoch + offset;
    let (y, m, d) = civil_from_days(t.div_euclid(86400));
    let secs = t.rem_euclid(86400);
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}",
        secs / 3600,
        (secs % 3600) / 60,
        secs % 60
    )
}

/// 本地时区偏移（秒）。usage 行的 `iso` 是本地时间，和 Python
/// `time.strftime` 同一口径。
fn local_offset_seconds(epoch: i64) -> i64 {
    // SAFETY: localtime_r 只写我们自己栈上的 tm，可重入，不碰全局状态。
    unsafe {
        let t = epoch as libc::time_t;
        let mut tm: libc::tm = std::mem::zeroed();
        if libc::localtime_r(&t, &mut tm).is_null() {
            return 0;
        }
        tm.tm_gmtoff
    }
}

/// Howard Hinnant 的 civil_from_days：把天数换成 (y, m, d)，不引入 chrono。
fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (if m <= 2 { y + 1 } else { y }, m, d)
}
