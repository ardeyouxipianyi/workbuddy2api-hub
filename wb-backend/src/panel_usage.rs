//! Panel usage aggregates. The Python log is the single input to every view here.

use std::collections::BTreeMap;
use std::sync::Arc;

use hyper::body::Incoming;
use hyper::{Request, StatusCode};
use serde_json::{json, Value};

use crate::http::{json_resp, Resp};
use crate::App;

const DAY_SECONDS: f64 = 86_400.0;

fn read_rows(app: &App) -> Arc<Vec<Row>> {
    load_rows(&app.recorder_path())
}

/// 一行日志在内存里的紧凑形态。
///
/// 面板要整份日志（全时段/跨月），而 serde_json::Value 每行要 ~4 KB：2.6 万行就是
/// 115 MB，还随日志线性长（CT412 上一次内存爆掉就是这么来的）。这里只留各视图真正
/// 读的字段，一级对象每行 ~200 B；`raw` 存原行，给「最近请求」这种要原样输出的
/// 视图用（它只有一页几十行，重新解析很便宜）。
#[derive(serde::Deserialize, Default)]
#[serde(default)]
pub struct Row {
    pub at: f64,
    pub model: Option<String>,
    pub account: Option<String>,
    pub realm: Option<String>,
    pub key: Option<String>,
    pub outcome: Option<String>,
    pub iso: Option<String>,
    pub credit: f64,
    pub prompt_tokens: f64,
    pub completion_tokens: f64,
    pub reasoning_tokens: f64,
    pub cached_tokens: f64,
    pub total_tokens: f64,
    pub ttft_ms: Option<f64>,
    pub gen_ms: Option<f64>,
    pub elapsed_ms: Option<f64>,
    pub tokens_per_sec: Option<f64>,
    pub cache_hit_pct: Option<f64>,
    pub error: Option<Value>,
    pub cost_policy: Option<Value>,
    /// 原行（不含换行）。只给「最近请求」原样输出用，**只有尾部**几行留原文：
    /// 整份日志每行都留一份要 ~600 B/行（2.7 万行 ≈ 17 MB，10 万行 ≈ 60 MB）。
    /// 更早的行 `to_value()` 用解析出的字段重建，面板也不会展示它们。
    #[serde(skip)]
    pub raw: Box<str>,
}

impl Row {
    pub fn str(&self, key: &str) -> Option<&str> {
        match key {
            "model" => self.model.as_deref(),
            "account" => self.account.as_deref(),
            "realm" => self.realm.as_deref(),
            "key" => self.key.as_deref(),
            "outcome" => self.outcome.as_deref(),
            "iso" => self.iso.as_deref(),
            _ => None,
        }
    }

    pub fn has(&self, key: &str) -> bool {
        match key {
            "key" => self.key.is_some(),
            "realm" => self.realm.is_some(),
            "error" => self.error.is_some(),
            _ => self.str(key).is_some(),
        }
    }

    /// 解析一行日志（也供测试构造行用）。
    pub fn parse(line: &str) -> Option<Row> {
        let mut row: Row = serde_json::from_str(line).ok()?;
        row.raw = line.into();
        Some(row)
    }

    /// 原行的 Value 形态（只给「最近请求」这类要整行输出的视图）。
    /// 尾部保留原文的行原样返回；更早的行按字段重建（面板不会展示它们）。
    pub fn to_value(&self) -> Value {
        if !self.raw.is_empty() {
            if let Ok(value) = serde_json::from_str(&self.raw) {
                return value;
            }
        }
        let mut map = serde_json::Map::new();
        map.insert("at".into(), json!(self.at));
        for (key, value) in [
            ("model", &self.model),
            ("account", &self.account),
            ("realm", &self.realm),
            ("key", &self.key),
            ("outcome", &self.outcome),
            ("iso", &self.iso),
        ] {
            if let Some(value) = value {
                map.insert(key.into(), json!(value));
            }
        }
        for (key, value) in [
            ("credit", self.credit),
            ("prompt_tokens", self.prompt_tokens),
            ("completion_tokens", self.completion_tokens),
            ("reasoning_tokens", self.reasoning_tokens),
            ("cached_tokens", self.cached_tokens),
            ("total_tokens", self.total_tokens),
        ] {
            if value != 0.0 {
                map.insert(key.into(), json!(value));
            }
        }
        for (key, value) in [
            ("ttft_ms", self.ttft_ms),
            ("gen_ms", self.gen_ms),
            ("elapsed_ms", self.elapsed_ms),
            ("tokens_per_sec", self.tokens_per_sec),
            ("cache_hit_pct", self.cache_hit_pct),
        ] {
            if let Some(value) = value {
                map.insert(key.into(), json!(value));
            }
        }
        if let Some(error) = &self.error {
            map.insert("error".into(), error.clone());
        }
        if let Some(policy) = &self.cost_policy {
            map.insert("cost_policy".into(), policy.clone());
        }
        Value::Object(map)
    }
}

/// 逐行读 + 解析；只有尾部 `RAW_TAIL` 行保留原文（见 `Row::raw`）。
fn parse_rows_streaming(path: &std::path::Path) -> Vec<Row> {
    use std::io::BufRead;
    const RAW_TAIL: usize = 256;
    let Ok(file) = std::fs::File::open(path) else { return Vec::new() };
    let meta_len = file.metadata().map(|meta| meta.len()).unwrap_or(0);
    // 每行约 600 B 估个容量，省掉十几次 realloc（不会超额：只是上界估计）。
    let mut rows: Vec<Row> = Vec::with_capacity((meta_len / 600).max(64) as usize);
    let reader = std::io::BufReader::with_capacity(256 * 1024, file);
    let mut handle = reader;
    let mut line = String::new();
    loop {
        line.clear();
        match handle.read_line(&mut line) {
            Ok(0) => break,
            Ok(_) => {
                if line.trim().is_empty() {
                    continue;
                }
                if let Some(row) = Row::parse(&line) {
                    rows.push(row);
                }
            }
            Err(_) => break,
        }
    }
    // 只留尾部原文：更早的行在「最近请求」里根本不会出现。
    let cutoff = rows.len().saturating_sub(RAW_TAIL);
    for row in rows.iter_mut().take(cutoff) {
        row.raw = "".into();
    }
    rows
}

/// 解析结果按 (路径, mtime, 长度) 缓存一次，所有面板视图共用同一份。
/// 以前每个请求都重读整份 usage.jsonl（16 MB → 约 55 MB 的 Value），面板每 5 秒
/// 并发打 /usage、/usage/perf、/usage/analytics，几次叠起来就是 500 MB ——
/// CT412 的内存就是这么爆的。
fn load_rows(path: &std::path::Path) -> Arc<Vec<Row>> {
    static CACHE: std::sync::OnceLock<std::sync::Mutex<(std::path::PathBuf, u64, u64, Arc<Vec<Row>>)>> =
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
    // 边读边解析：不再把整份 16 MB 文本读进内存（解析期间原文与结果双份驻留，
    // 那就是 CT412 内存峰值的大头）。
    let rows = parse_rows_streaming(path);
    let shared = Arc::new(rows);
    // 大块重建之后把空闲页还给系统：glibc 默认留着 arena，RSS 会一直悬在高位。
    crate::state::trim_malloc();
    *guard = (path.to_path_buf(), stamp.0, stamp.1, shared.clone());
    shared
}

/// 行里没有 key 字段（不是空串）在桶里的标记：JSON 里不会出现 NUL，足够区分。
pub const MISSING_KEY: &str = "\u{0}";

/// 增量聚合缓存：新追加的行折进小时桶，窗口视图只加桶。
///
/// 判定很简单——文件变长就只读尾部、折新增的行；文件变短/换文件、计价输入变了、
/// 账号出口表变了，就整份重建（一次约 150 ms，正常一天也碰不到几次）。
struct IndexCache {
    path: std::path::PathBuf,
    offset: u64,
    pricing: String,
    realms: String,
    /// 用 Arc 是为了让请求线程拿着快照慢慢算，不长期占锁；写时复制只在新数据到达
    /// 且正好有人在读时发生。
    index: std::sync::Arc<crate::usage_index::Index>,
}

static INDEX_CACHE: std::sync::OnceLock<std::sync::Mutex<IndexCache>> = std::sync::OnceLock::new();

fn realms_stamp(app: &App) -> String {
    let mut parts: Vec<String> = account_realms(app)
        .into_iter()
        .map(|(uid, realm)| format!("{uid}={realm}"))
        .collect();
    parts.push(app.state.current_realm());
    parts.join(";")
}

/// 本地小时桶的起点与日键（日键给窗口排序，起点给窗口过滤）。
fn local_hour(at: f64) -> (i32, i32, f64) {
    let stamp = at as libc::time_t;
    let mut value: libc::tm = unsafe { std::mem::zeroed() };
    if unsafe { libc::localtime_r(&stamp, &mut value) }.is_null() {
        return (0, 0, at);
    }
    let day = (value.tm_year + 1900) * 10_000 + (value.tm_mon + 1) * 100 + value.tm_mday;
    let hour = value.tm_hour;
    value.tm_min = 0;
    value.tm_sec = 0;
    let start = unsafe { libc::mktime(&mut value) };
    (day, hour, if start > 0 { start as f64 } else { at })
}

fn advance_index(cache: &mut IndexCache, app: &Arc<App>) {
    let path = app.recorder_path();
    let settings = app.state.settings_snapshot();
    let pricing = app.pricing.stamp(&settings);
    let realms = realms_stamp(app);
    let length = std::fs::metadata(&path).map(|meta| meta.len()).unwrap_or(0);
    if path != cache.path || pricing != cache.pricing || realms != cache.realms || length < cache.offset {
        cache.path = path.clone();
        cache.offset = 0;
        cache.index = std::sync::Arc::new(crate::usage_index::Index::default());
    }
    if length == cache.offset {
        cache.pricing = pricing;
        cache.realms = realms;
        return;
    }
    let mut tail: Vec<u8> = Vec::new();
    if let Ok(mut file) = std::fs::File::open(&path) {
        use std::io::{Read, Seek, SeekFrom};
        if file.seek(SeekFrom::Start(cache.offset)).is_ok() && file.read_to_end(&mut tail).is_err() {
            tail.clear();
        }
    }
    // 只折完整的行：末半行留给下次（offset 停在最后一个换行之后）。
    let complete = tail
        .iter()
        .rposition(|byte| *byte == b'\n')
        .map(|index| index + 1)
        .unwrap_or(0);
    let text = String::from_utf8_lossy(&tail[..complete]).to_string();
    let rows: Vec<Row> = text
        .lines()
        .filter(|line| !line.trim().is_empty())
        .filter_map(Row::parse)
        .collect();
    let refs: Vec<&Row> = rows.iter().collect();
    let costs = app.pricing.cost_rows(&refs, &settings);
    let realms_map = account_realms(app);
    let current = app.state.current_realm();
    let index = std::sync::Arc::make_mut(&mut cache.index);
    for (offset, row) in rows.iter().enumerate() {
        let (day, hour, start) = local_hour(row.at);
        let realm = row_realm(row, &realms_map, &current);
        let key = crate::usage_index::CellKey {
            model: row.model.clone().unwrap_or_default(),
            account: row.account.clone().unwrap_or_default(),
            // 没有 key 字段和 key 为空串是两回事：analytics 把前者归到
            // __before_keys__、后者归到 __no_key__（Python 的 row["key"] 判定）。
            key: row.key.clone().unwrap_or_else(|| MISSING_KEY.to_string()),
            realm,
        };
        let seq = index.rows;
        index.rows += 1;
        index
            .hours
            .entry((day, hour))
            .or_default()
            .entry(key)
            .or_default()
            .add_row(row, costs[offset], seq, start);
    }
    cache.offset += complete as u64;
    cache.pricing = pricing;
    cache.realms = realms;
}

fn usage_index(app: &Arc<App>) -> std::sync::Arc<crate::usage_index::Index> {
    let cache = INDEX_CACHE.get_or_init(|| {
        std::sync::Mutex::new(IndexCache {
            path: std::path::PathBuf::new(),
            offset: 0,
            pricing: String::new(),
            realms: String::new(),
            index: std::sync::Arc::new(crate::usage_index::Index::default()),
        })
    });
    let mut guard = cache.lock().unwrap_or_else(|error| error.into_inner());
    advance_index(&mut guard, app);
    guard.index.clone()
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

/// 自定义区间（range=custom）的边界是任意时刻，桶的粒度是整点：只有它必须逐行。
fn custom_range(req: &Request<Incoming>) -> bool {
    query_string_value(req.uri().query().unwrap_or(""), "range")
        .map(|value| value.trim().eq_ignore_ascii_case("custom"))
        .unwrap_or(false)
}

fn row_at(row: &Row) -> Option<f64> {
    (row.at > 0.0).then_some(row.at)
}

/// 行的 realm：字段 → 账号 → 模型归属，推不出来时归到当前 realm。
/// 与 Python 的 row_realm 同序（它最后还会按模型的前缀表猜一次，见 detect_model_realm）。
fn row_realm(row: &Row, account_realms: &BTreeMap<String, String>, current: &str) -> String {
    if let Some(value) = row
        .realm
        .as_deref()
        .filter(|value| !value.is_empty())
    {
        return value.to_string();
    }
    if let Some(realm) = row
        .account
        .as_deref()
        .and_then(|uid| account_realms.get(uid))
    {
        return realm.clone();
    }
    match row.model.as_deref() {
        Some(model) if !model.is_empty() => detect_model_realm(model, current),
        _ => current.to_string(),
    }
}

/// Python 的 detect_model_realm：只有明确属于某个 exit 的模型才被判定，
/// 其余一律算当前 realm（否则老行会因为推不出归属而整条消失）。
fn detect_model_realm(model: &str, current: &str) -> String {
    let m = model.to_lowercase();
    const INTL_ONLY: [&str; 9] = [
        "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5",
        "gpt-5.4", "gpt-5.3-codex", "gemini-3.5-flash", "grok-4.7",
    ];
    if INTL_ONLY.contains(&m.as_str()) || m.starts_with("gpt-") || m.starts_with("gemini-") {
        return "intl".into();
    }
    const CN_ONLY: [&str; 13] = [
        "deepseek-v4-pro", "minimax-m3", "minimax-m2.7", "minimax-m2.5", "glm-5.1",
        "glm-5.0-turbo", "glm-4.6v", "kimi-k3-1", "kimi-k2.7", "kimi-k2-thinking",
        "hy3-x", "hy4-preview-dev", "hy4-preview-x",
    ];
    if CN_ONLY.contains(&m.as_str()) || m.starts_with("minimax-") || m.starts_with("deepseek-v4-pro") {
        return "cn".into();
    }
    current.to_string()
}

fn account_realms(app: &App) -> BTreeMap<String, String> {
    app.state
        .accounts_snapshot()
        .into_iter()
        .map(|account| (account.uid(), account.realm()))
        .collect()
}

fn rows_for_realm<'a>(
    rows: &'a [Row],
    realm: &str,
    realms: &BTreeMap<String, String>,
    current: &str,
) -> Vec<&'a Row> {
    rows.iter()
        .filter(|row| realm == "all" || row_realm(row, realms, current) == realm)
        .collect()
}

/// 数值读取对两种行都成立：日志行（Row）和视图自己拼的输出对象（Value）。
/// 输出对象只用来排序/汇总，没必要为它再造一个类型。
trait NumField {
    fn num(&self, key: &str) -> Option<f64>;
}

impl NumField for Row {
    fn num(&self, key: &str) -> Option<f64> {
        optional(self, key)
    }
}

impl<T: NumField + ?Sized> NumField for &T {
    fn num(&self, key: &str) -> Option<f64> {
        (**self).num(key)
    }
}

impl NumField for Value {
    fn num(&self, key: &str) -> Option<f64> {
        self.get(key).and_then(Value::as_f64)
    }
}

fn number<T: NumField>(row: &T, key: &str) -> f64 {
    row.num(key).unwrap_or(0.0)
}

/// 可能缺失的数值：视图要区分「没有这个字段」和「值是 0」。
fn optional(row: &Row, key: &str) -> Option<f64> {
    match key {
        "at" => Some(row.at),
        "credit" => Some(row.credit),
        "prompt_tokens" => Some(row.prompt_tokens),
        "completion_tokens" => Some(row.completion_tokens),
        "reasoning_tokens" => Some(row.reasoning_tokens),
        "cached_tokens" => Some(row.cached_tokens),
        "total_tokens" => Some(row.total_tokens),
        "ttft_ms" => row.ttft_ms,
        "gen_ms" => row.gen_ms,
        "elapsed_ms" => row.elapsed_ms,
        "tokens_per_sec" => row.tokens_per_sec,
        "cache_hit_pct" => row.cache_hit_pct,
        _ => None,
    }
}

fn integer<T: NumField>(row: &T, key: &str) -> i64 {
    row.num(key).map(|value| value as i64).unwrap_or(0)
}

fn epoch_now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(0.0)
}

fn outcome(row: &Row) -> &str {
    row.outcome.as_deref().unwrap_or("")
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
    use super::{detect_model_realm, load_rows, python_weekday, range_bounds_query, row_realm, Row};
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
    fn row_realm_falls_back_to_model_then_current_realm() {
        // 老行没有 realm 字段：先看账号，再看模型前缀表，最后归到当前 realm。
        // 最后一步是关键：推不出归属就返回空串会让这些行在按 realm 过滤时整条消失。
        let mut realms = std::collections::BTreeMap::new();
        realms.insert("acct-1".to_string(), "intl".to_string());
        let with_account = Row::parse(r#"{"account": "acct-1", "model": "hy3"}"#).unwrap();
        assert_eq!(row_realm(&with_account, &realms, "cn"), "intl");
        let cn_model = Row::parse(r#"{"model": "minimax-m3"}"#).unwrap();
        assert_eq!(row_realm(&cn_model, &realms, "intl"), "cn");
        let intl_model = Row::parse(r#"{"model": "gpt-5.5"}"#).unwrap();
        assert_eq!(row_realm(&intl_model, &realms, "cn"), "intl");
        let unknown = Row::parse(r#"{"model": "some-new-model"}"#).unwrap();
        assert_eq!(row_realm(&unknown, &realms, "cn"), "cn");
        assert_eq!(detect_model_realm("", "intl"), "intl");
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
