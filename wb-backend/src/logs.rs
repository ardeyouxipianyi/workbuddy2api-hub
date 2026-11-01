//! 运行日志：内存环形缓冲 + 可选 SQLite 落库。
//!
//! 对照 `wb_proxy.py` 的 `add_log_entry` / `get_logs` 与 `wb_logstore.py`：
//! 级别与模块按关键字推断、号码脱敏后再入缓冲、2000 条上限、`since_id` 增量拉取；
//! 落库开关与三条保留上限（rows / days / bytes）来自面板设置，默认关。
//!
//! 落库走单独线程 + 单连接（Python 的 LogStore 形状）：请求线程只做 mpsc send，
//! 一批一个事务，读也走同一个队列，所以不会有两个写者抢库，也不会每行一次 fsync。

use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::mpsc::{channel, Receiver, RecvTimeoutError, Sender};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use rusqlite::Connection;
use serde_json::{json, Value};

const RING_MAX: usize = 2000;
/// 一条日志的正文上限：整段响应体被误打进日志时别把库撑爆（Python 同值）。
const MSG_MAX: usize = 64 * 1024;
const BATCH_MAX: usize = 256;
const TRIM_INTERVAL: Duration = Duration::from_secs(5);
const TRIM_BYTES_INTERVAL: Duration = Duration::from_secs(30);
const IDLE_TICK: Duration = Duration::from_secs(5);

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS logs (
    id    INTEGER PRIMARY KEY,
    ts    TEXT NOT NULL,
    time  TEXT NOT NULL,
    level TEXT NOT NULL,
    tag   TEXT NOT NULL,
    msg   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level);
CREATE INDEX IF NOT EXISTS idx_logs_tag   ON logs(tag);
";

/// 级别推断关键字：命中即 ERROR 优先于 WARN。
const ERROR_HINTS: &[&str] = &[
    "error", "exception", "failed", "fail:", "reject", "traceback", "errno", "fatal", "11-128",
    "11101", "11140", "失败", "异常", "错误", "拒绝", "不可用", "无效",
];
const WARN_HINTS: &[&str] = &[
    "warn", "warning", "retry", "timeout", "throttl", "重试", "超时", "告警", "警告", "降级",
    "限流",
];
/// 模块推断：顺序即优先级，越靠前越先判定（与 Python 同序）。
const TAG_HINTS: &[(&str, &[&str])] = &[
    ("chat", &["chat:", "chat done", "request error", "/v1/chat", "/chat/completions", "responses"]),
    ("scheduler", &["scheduler", "调度器"]),
    ("tasks", &["task", "任务", "打卡", "签到", "猫猫", "travel", "growth"]),
    ("accounts", &["account", "账号", "pool", "imported"]),
    ("pricing", &["pricing", "定价", "取价", "价格", "price", "抓取"]),
    ("egress", &["egress", "出口", "槽位", "proxy slot", "mihomo"]),
    ("upstream", &["upstream", "上游"]),
    ("webtools", &["webtool", "web tool", "本地网络工具"]),
    ("catalog", &["model", "catalog", "模型"]),
    ("auth", &["auth", "token", "oauth"]),
    ("settings", &["settings", "设置"]),
    ("update", &["更新", "release"]),
    ("usage", &["usage", "用量"]),
    ("agents", &["agents", "智能体"]),
];

/// 面板日志页的高频轮询：正常的 GET(<400) 不记账，否则两秒一条刷屏。
const QUIET: &[&str] = &[
    "/logs", "/usage", "/accounts", "/scheduler", "/health", "/panel/status", "/realm",
    "/favicon.ico",
];

#[derive(Default, Clone, Copy)]
pub struct Caps {
    pub rows: Option<i64>,
    pub days: Option<i64>,
    pub bytes: Option<i64>,
}

#[derive(Default, Clone)]
pub struct Query {
    pub limit: usize,
    pub level: String,
    pub tag: String,
    pub search: String,
    pub since_id: i64,
}

struct Entry {
    id: i64,
    ts: String,
    time: String,
    level: String,
    tag: String,
    msg: String,
}

enum Job {
    Append(Entry),
    Query(Query, Sender<Value>),
    Stats(Sender<Value>),
    Clear(Sender<Value>),
}

pub struct Logs {
    ring: Mutex<VecDeque<Value>>,
    next_id: AtomicI64,
    path: PathBuf,
    worker: Mutex<Option<Worker>>,
}

struct Worker {
    tx: Sender<Job>,
    caps: Caps,
}

impl Default for Logs {
    /// 测试夹具（`logs: Default::default()`）用：落库开关默认关，只写内存。
    fn default() -> Self {
        Logs::new(std::env::temp_dir())
    }
}

impl Logs {
    pub fn new(usage_dir: impl Into<PathBuf>) -> Logs {
        Logs {
            ring: Mutex::new(VecDeque::new()),
            next_id: AtomicI64::new(0),
            path: usage_dir.into().join("logs.sqlite3"),
            worker: Mutex::new(None),
        }
    }

    /// 一条日志：`level`/`tag` 省略时按关键字推断（Python 的 add_log_entry）。
    pub fn add(&self, msg: &str, level: Option<&str>, tag: Option<&str>) -> Value {
        let raw = msg.trim_end();
        let lower = raw.to_lowercase();
        let level = level.map(str::to_string).unwrap_or_else(|| {
            if ERROR_HINTS.iter().any(|h| lower.contains(h)) {
                "ERROR".to_string()
            } else if WARN_HINTS.iter().any(|h| lower.contains(h)) {
                "WARN".to_string()
            } else {
                "INFO".to_string()
            }
        });
        let tag = tag
            .map(str::to_string)
            .unwrap_or_else(|| {
                TAG_HINTS
                    .iter()
                    .find(|(_, hints)| hints.iter().any(|h| lower.contains(h)))
                    .map(|(name, _)| (*name).to_string())
                    .unwrap_or_else(|| "system".to_string())
            });
        // 掩码在推断之后：推断用原始串，否则含号码的行分类会变（Python 同）。
        let safe = crate::misc_routes::mask_phones(raw);
        let stamp = crate::state::local_stamp();
        let entry = Entry {
            id: self.next_id.fetch_add(1, Ordering::Relaxed) + 1,
            ts: stamp.clone(),
            time: stamp.get(11..19).unwrap_or("").to_string(),
            level,
            tag,
            msg: safe.chars().take(MSG_MAX).collect(),
        };
        let value = entry.value();
        if let Ok(mut ring) = self.ring.lock() {
            ring.push_back(value.clone());
            while ring.len() > RING_MAX {
                ring.pop_front();
            }
        }
        if let Ok(guard) = self.worker.lock() {
            if let Some(worker) = guard.as_ref() {
                // 只是入队：落库慢不该让请求线程等。
                let _ = worker.tx.send(Job::Append(entry));
            }
        }
        value
    }

    pub fn mode(&self) -> &'static str {
        if self.db_active() {
            "db"
        } else {
            "memory"
        }
    }

    pub fn db_active(&self) -> bool {
        self.worker.lock().map(|w| w.is_some()).unwrap_or(false)
    }

    /// 面板筛选后的日志。落库开启时以库为准（它是完整历史），查询失败退回内存。
    pub fn query(&self, q: &Query) -> Value {
        if let Some(value) = self.ask(|tx| Job::Query(q.clone(), tx)) {
            return value;
        }
        self.query_ring(q)
    }

    fn query_ring(&self, q: &Query) -> Value {
        let guard = match self.ring.lock() {
            Ok(guard) => guard,
            Err(_) => return json!({ "total": 0, "logs": [], "max_id": q.since_id }),
        };
        let needle = q.search.to_lowercase();
        let level = q.level.to_uppercase();
        let mut items: Vec<Value> = guard
            .iter()
            .filter(|item| item["id"].as_i64().unwrap_or(0) > q.since_id)
            .filter(|item| level.is_empty() || item["level"].as_str().unwrap_or("") == level)
            .filter(|item| {
                q.tag.is_empty()
                    || item["tag"].as_str().unwrap_or("").eq_ignore_ascii_case(&q.tag)
            })
            .filter(|item| {
                needle.is_empty()
                    || item["msg"].as_str().unwrap_or("").to_lowercase().contains(&needle)
                    || item["tag"].as_str().unwrap_or("").to_lowercase().contains(&needle)
            })
            .cloned()
            .collect();
        let total = items.len();
        if q.limit > 0 && q.since_id == 0 && items.len() > q.limit {
            items = items.split_off(items.len() - q.limit);
        }
        let max_id = items
            .last()
            .and_then(|item| item["id"].as_i64())
            .unwrap_or(q.since_id);
        json!({ "total": total, "logs": items, "max_id": max_id })
    }

    pub fn stats(&self) -> Option<Value> {
        self.ask(Job::Stats)
    }

    pub fn clear(&self) {
        if let Ok(mut ring) = self.ring.lock() {
            ring.clear();
        }
        let (tx, rx) = channel();
        if let Ok(guard) = self.worker.lock() {
            if let Some(worker) = guard.as_ref() {
                let _ = worker.tx.send(Job::Clear(tx));
            }
        }
        let _ = rx.recv_timeout(Duration::from_secs(8));
    }

    fn ask<F>(&self, build: F) -> Option<Value>
    where
        F: FnOnce(Sender<Value>) -> Job,
    {
        let (tx, rx) = channel();
        {
            let guard = self.worker.lock().ok()?;
            let worker = guard.as_ref()?;
            worker.tx.send(build(tx)).ok()?;
        }
        rx.recv_timeout(Duration::from_secs(8)).ok()
    }

    /// 读面板设置：`log_db_enabled` 与三条上限（留空 / 0 = 该轴不限制）。
    pub fn configure_from(&self, settings: &serde_json::Map<String, Value>) {
        fn cap(settings: &serde_json::Map<String, Value>, key: &str) -> Option<i64> {
            let value = settings.get(key)?;
            value
                .as_i64()
                .or_else(|| value.as_str().and_then(|text| text.trim().parse().ok()))
                .filter(|value| *value > 0)
        }
        self.configure(
            settings
                .get("log_db_enabled")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            Caps {
                rows: cap(settings, "log_db_max_rows"),
                days: cap(settings, "log_db_max_days"),
                bytes: cap(settings, "log_db_max_bytes"),
            },
        );
    }

    /// 面板「日志落库」开关与三条上限；启动时与每次保存设置都调一次。
    pub fn configure(&self, enabled: bool, caps: Caps) {
        let mut guard = match self.worker.lock() {
            Ok(guard) => guard,
            Err(_) => return,
        };
        if enabled {
            if let Some(worker) = guard.as_mut() {
                worker.caps = caps;
                return;
            }
            // 库打不开就别启用：否则每个面板查询都要干等到 8 秒超时才回退内存。
            if connect(&self.path).is_none() {
                return;
            }
            let (tx, rx) = channel();
            let path = self.path.clone();
            std::thread::Builder::new()
                .name("logstore".to_string())
                .spawn(move || run_store(path, caps, rx))
                .ok();
            *guard = Some(Worker { tx, caps });
            // id 接着库里最大的续号，否则面板的 since_id 会重复拉旧行。
            if let Some(highest) = max_id(&self.path) {
                self.next_id.fetch_max(highest, Ordering::Relaxed);
            }
        } else {
            // 关掉只是停写，库与历史原样留着（下次打开接着记）。
            *guard = None;
        }
    }
}

impl Entry {
    fn value(&self) -> Value {
        json!({
            "id": self.id, "ts": self.ts, "time": self.time,
            "level": self.level, "tag": self.tag, "msg": self.msg,
        })
    }
}

fn run_store(path: PathBuf, caps: Caps, rx: Receiver<Job>) {
    let conn = match connect(&path) {
        Some(conn) => conn,
        None => return,
    };
    let mut last_trim = Instant::now();
    let mut last_bytes_trim = Instant::now();
    loop {
        let first = match rx.recv_timeout(IDLE_TICK) {
            Ok(job) => job,
            Err(RecvTimeoutError::Timeout) => {
                // 空闲也要按节奏裁剪：没有新日志不代表旧日志可以一直留着。
                maybe_trim(&conn, &caps, &path, &mut last_trim, &mut last_bytes_trim);
                continue;
            }
            Err(RecvTimeoutError::Disconnected) => break,
        };
        let mut batch = vec![first];
        while batch.len() < BATCH_MAX {
            match rx.try_recv() {
                Ok(job) => batch.push(job),
                Err(_) => break,
            }
        }
        // 一批一个事务：突发流量只付一次 fsync。
        let tx = match conn.unchecked_transaction() {
            Ok(tx) => tx,
            Err(_) => continue,
        };
        let mut wrote = false;
        for job in batch {
            match job {
                Job::Append(entry) => {
                    let _ = tx.execute(
                        "INSERT OR REPLACE INTO logs (id, ts, time, level, tag, msg) \
                         VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                        rusqlite::params![
                            entry.id,
                            entry.ts,
                            entry.time,
                            entry.level,
                            entry.tag,
                            entry.msg
                        ],
                    );
                    wrote = true;
                }
                Job::Query(q, tx_reply) => {
                    let _ = tx_reply.send(query_db(&tx, &q));
                }
                Job::Stats(tx_reply) => {
                    let _ = tx_reply.send(stats_db(&tx, &path));
                }
                Job::Clear(tx_reply) => {
                    let _ = tx.execute("DELETE FROM logs", []);
                    wrote = true;
                    let _ = tx_reply.send(json!({ "ok": true }));
                }
            }
        }
        let _ = tx.commit();
        if wrote {
            maybe_trim(&conn, &caps, &path, &mut last_trim, &mut last_bytes_trim);
        }
    }
}

fn connect(path: &Path) -> Option<Connection> {
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let conn = Connection::open(path).ok()?;
    // 与 Python 的 sqlite3 timeout=10 对齐：切换后端时两个进程可能短暂同时持有库，
    // busy_timeout=0 会让这一瞬间的 INSERT 直接报 BUSY 然后被丢掉。
    let _ = conn.busy_timeout(Duration::from_secs(10));
    // journal_mode=DELETE：文件大小就是操作者设上限的那个文件（WAL 的新行会停在
    // 副文件里，那个上限永远收不到它们）。
    let _ = conn.execute_batch("PRAGMA journal_mode=DELETE; PRAGMA synchronous=NORMAL;");
    conn.execute_batch(SCHEMA).ok()?;
    Some(conn)
}

fn query_db(conn: &Connection, q: &Query) -> Value {
    let mut where_parts: Vec<String> = Vec::new();
    let mut args: Vec<rusqlite::types::Value> = Vec::new();
    if q.since_id > 0 {
        where_parts.push("id > ?".to_string());
        args.push(rusqlite::types::Value::Integer(q.since_id));
    }
    if !q.level.is_empty() {
        where_parts.push("level = ?".to_string());
        args.push(rusqlite::types::Value::Text(q.level.to_uppercase()));
    }
    if !q.tag.is_empty() {
        where_parts.push("LOWER(tag) = ?".to_string());
        args.push(rusqlite::types::Value::Text(q.tag.to_lowercase()));
    }
    if !q.search.is_empty() {
        where_parts.push("(instr(LOWER(msg), ?) > 0 OR instr(LOWER(tag), ?) > 0)".to_string());
        let needle = rusqlite::types::Value::Text(q.search.to_lowercase());
        args.push(needle.clone());
        args.push(needle);
    }
    let clause = if where_parts.is_empty() {
        String::new()
    } else {
        format!(" WHERE {}", where_parts.join(" AND "))
    };
    let total: i64 = conn
        .query_row(
            &format!("SELECT COUNT(*) FROM logs{clause}"),
            rusqlite::params_from_iter(args.clone()),
            |row| row.get(0),
        )
        .unwrap_or(0);
    let paged = q.limit > 0 && q.since_id == 0;
    let sql = if paged {
        format!("SELECT id, ts, time, level, tag, msg FROM logs{clause} ORDER BY id DESC LIMIT ?")
    } else {
        format!("SELECT id, ts, time, level, tag, msg FROM logs{clause} ORDER BY id")
    };
    let mut select_args = args;
    if paged {
        select_args.push(rusqlite::types::Value::Integer(q.limit as i64));
    }
    let mut rows: Vec<Value> = Vec::new();
    if let Ok(mut stmt) = conn.prepare(&sql) {
        let mapped = stmt.query_map(rusqlite::params_from_iter(select_args), |row| {
            Ok(json!({
                "id": row.get::<_, i64>(0)?,
                "ts": row.get::<_, String>(1)?,
                "time": row.get::<_, String>(2)?,
                "level": row.get::<_, String>(3)?,
                "tag": row.get::<_, String>(4)?,
                "msg": row.get::<_, String>(5)?,
            }))
        });
        if let Ok(iter) = mapped {
            rows = iter.filter_map(Result::ok).collect();
        }
    }
    if paged {
        rows.reverse();
    }
    let max_id = rows
        .last()
        .and_then(|item| item["id"].as_i64())
        .unwrap_or(if q.since_id > 0 { q.since_id } else { 0 });
    json!({ "total": total, "logs": rows, "max_id": max_id })
}

fn stats_db(conn: &Connection, path: &Path) -> Value {
    let (count, oldest, newest): (i64, Option<String>, Option<String>) = conn
        .query_row("SELECT COUNT(*), MIN(ts), MAX(ts) FROM logs", [], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?))
        })
        .unwrap_or((0, None, None));
    json!({
        "rows": count,
        "oldest": oldest.unwrap_or_default(),
        "newest": newest.unwrap_or_default(),
        "bytes": std::fs::metadata(path).map(|m| m.len()).unwrap_or(0),
        "path": path.to_string_lossy(),
    })
}

fn max_id(path: &Path) -> Option<i64> {
    if !path.exists() {
        return None;
    }
    let conn = Connection::open(path).ok()?;
    conn.query_row("SELECT MAX(id) FROM logs", [], |row| row.get::<_, Option<i64>>(0))
        .ok()
        .flatten()
}

fn maybe_trim(
    conn: &Connection,
    caps: &Caps,
    path: &Path,
    last_trim: &mut Instant,
    last_bytes_trim: &mut Instant,
) {
    if last_trim.elapsed() >= TRIM_INTERVAL {
        *last_trim = Instant::now();
        trim_rows(conn, caps);
    }
    if last_bytes_trim.elapsed() >= TRIM_BYTES_INTERVAL {
        *last_bytes_trim = Instant::now();
        if let Some(cap) = caps.bytes {
            trim_bytes(conn, path, cap);
        }
    }
}

fn trim_rows(conn: &Connection, caps: &Caps) {
    if let Some(days) = caps.days {
        // local_stamp_for 带 'T'，库里的 ts 是空格分隔（Python 的 strftime 口径）：
        // 不换掉的话边界上的行会被误删。
        let cutoff = crate::state::local_stamp_for(now_epoch() - days * 86_400).replace('T', " ");
        let _ = conn.execute("DELETE FROM logs WHERE ts < ?1", rusqlite::params![cutoff]);
    }
    if let Some(rows) = caps.rows {
        let _ = conn.execute(
            "DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT ?1)",
            rusqlite::params![rows],
        );
    }
}

/// 从最旧删起再重建，直到文件落回上限的 85%：按平均行长估算要删多少行，一次
/// VACUUM 收尾，而不是一页一页缩（那个规模要几千轮）。
fn trim_bytes(conn: &Connection, path: &Path, cap: i64) {
    let mut size = file_size(path);
    if size <= cap {
        return;
    }
    let target = (cap * 85 / 100).max(1);
    for _ in 0..4 {
        let total: i64 = conn
            .query_row("SELECT COUNT(*) FROM logs", [], |row| row.get(0))
            .unwrap_or(0);
        if total <= 0 {
            break;
        }
        let average = (size as f64 / total as f64).max(1.0);
        let keep = ((target as f64 / average) as i64).max(1);
        let drop_count = total - keep;
        if drop_count <= 0 {
            break;
        }
        let _ = conn.execute(
            "DELETE FROM logs WHERE id IN (SELECT id FROM logs ORDER BY id ASC LIMIT ?1)",
            rusqlite::params![drop_count],
        );
        if conn.execute_batch("VACUUM").is_err() {
            break;
        }
        let new_size = file_size(path);
        if new_size >= size {
            break;
        }
        size = new_size;
        if size <= cap {
            break;
        }
    }
}

fn file_size(path: &Path) -> i64 {
    std::fs::metadata(path).map(|m| m.len() as i64).unwrap_or(0)
}

fn now_epoch() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// `?since_id=&limit=&level=&tag=&search=`：面板日志页与筛选框用的那组参数。
pub fn parse_query(raw: &str) -> Query {
    let mut parsed = Query { limit: 200, ..Default::default() };
    for pair in raw.split('&') {
        let (key, value) = match pair.split_once('=') {
            Some(pair) => pair,
            None => continue,
        };
        let value = url_decode(value);
        match key {
            "limit" => parsed.limit = value.trim().parse().unwrap_or(200),
            "level" => parsed.level = value,
            "tag" => parsed.tag = value,
            "search" => parsed.search = value,
            "since_id" => parsed.since_id = value.trim().parse().unwrap_or(0),
            _ => {}
        }
    }
    parsed
}

/// 查询串里只有 `%XX` 与 `+` 两种转义，不值得为此引一个依赖。
fn url_decode(value: &str) -> String {
    let bytes = value.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'%' if index + 2 < bytes.len() => {
                let hex = std::str::from_utf8(&bytes[index + 1..index + 3]).ok();
                match hex.and_then(|hex| u8::from_str_radix(hex, 16).ok()) {
                    Some(byte) => {
                        out.push(byte);
                        index += 3;
                    }
                    None => {
                        out.push(bytes[index]);
                        index += 1;
                    }
                }
            }
            b'+' => {
                out.push(b' ');
                index += 1;
            }
            byte => {
                out.push(byte);
                index += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).to_string()
}

/// Python `log_message` 那行访问日志：正常的高频 GET 不记（会自增刷屏）。
pub fn access_line(ip: &str, method: &str, path: &str, status: u16, stamp: &str) -> Option<String> {
    let bare = path.split('?').next().unwrap_or("");
    if status < 400
        && method == "GET"
        && QUIET
            .iter()
            .any(|p| bare == *p || bare.starts_with(&format!("{p}/")))
    {
        return None;
    }
    let target = if path.is_empty() { "/" } else { path };
    Some(format!(
        "{ip} - - [{}] \"{method} {target} HTTP/1.1\" {status}",
        access_date(stamp)
    ))
}

/// `01/Nov/2026 12:34:56`：Python `log_date_time_string()` 的格式。
fn access_date(stamp: &str) -> String {
    const MONTHS: [&str; 12] = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    let parts: Vec<&str> = stamp.split(['-', ' ', ':']).collect();
    if parts.len() < 6 {
        return stamp.to_string();
    }
    let month = parts[1].parse::<usize>().unwrap_or(1).clamp(1, 12);
    format!(
        "{}/{}/{} {}:{}:{}",
        parts[2],
        MONTHS[month - 1],
        parts[0],
        parts[3],
        parts[4],
        parts[5]
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 每个用例一个独立目录：落库用例写库，别互相踩。
    fn workdir() -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let path = std::env::temp_dir().join(format!("wb-logs-{}-{nanos}", std::process::id()));
        std::fs::create_dir_all(&path).expect("temp dir");
        path
    }

    #[test]
    fn infers_level_and_tag_like_python() {
        let logs = Logs::new(workdir());
        let error = logs.add("chat: model=x 失败: 上游返回 502", None, None);
        assert_eq!(error["level"], "ERROR");
        assert_eq!(error["tag"], "chat");
        let warn = logs.add("上游 timeout，准备重试", None, None);
        assert_eq!(warn["level"], "WARN");
        assert_eq!(warn["tag"], "upstream");
        let info = logs.add("catalog refreshed", None, None);
        assert_eq!(info["level"], "INFO");
        assert_eq!(info["tag"], "catalog");
        // 显式 tag/level 优先，不再推断。
        let explicit = logs.add("whatever failed", Some("INFO"), Some("custom"));
        assert_eq!(explicit["level"], "INFO");
        assert_eq!(explicit["tag"], "custom");
    }

    #[test]
    fn ring_keeps_newest_and_since_id_walks_forward() {
        let logs = Logs::new(workdir());
        for i in 0..RING_MAX + 50 {
            logs.add(&format!("line {i}"), None, Some("system"));
        }
        let all = logs.query(&Query { limit: 0, ..Default::default() });
        assert_eq!(all["total"], RING_MAX);
        assert_eq!(all["logs"][0]["msg"], "line 50");
        let last_id = all["max_id"].as_i64().unwrap();
        let tail = logs.query(&Query { limit: 10, ..Default::default() });
        assert_eq!(tail["logs"].as_array().unwrap().len(), 10);
        assert_eq!(tail["max_id"].as_i64().unwrap(), last_id);
        // since_id 是增量拉取：只要更新的行，且不被 limit 截断。
        let fresh = logs.query(&Query { since_id: last_id, limit: 10, ..Default::default() });
        assert!(fresh["logs"].as_array().unwrap().is_empty());
        logs.add("after", None, Some("system"));
        let fresh = logs.query(&Query { since_id: last_id, limit: 10, ..Default::default() });
        assert_eq!(fresh["logs"].as_array().unwrap().len(), 1);
        assert_eq!(fresh["logs"][0]["msg"], "after");
    }

    #[test]
    fn filters_are_applied_to_the_ring() {
        let logs = Logs::new(workdir());
        logs.add("chat done: model=a", None, Some("chat"));
        logs.add("pricing refreshed", None, Some("pricing"));
        let by_level = logs.query(&Query { level: "error".into(), ..Default::default() });
        assert_eq!(by_level["total"], 0);
        let by_tag = logs.query(&Query { tag: "CHAT".into(), ..Default::default() });
        assert_eq!(by_tag["logs"].as_array().unwrap().len(), 1);
        let by_search = logs.query(&Query { search: "PRICING".into(), ..Default::default() });
        assert_eq!(by_search["logs"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn database_mode_persists_and_resumes_numbering() {
        let dir = workdir();
        let logs = Logs::new(dir.clone());
        logs.configure(true, Caps { rows: None, days: Some(7), bytes: None });
        assert_eq!(logs.mode(), "db");
        logs.add("first", None, Some("system"));
        logs.add("second", None, Some("system"));
        // 落库是异步批量的：等一拍再读。
        let deadline = Instant::now() + Duration::from_secs(5);
        let mut stored = 0;
        while Instant::now() < deadline {
            stored = logs.stats().map(|s| s["rows"].as_i64().unwrap_or(0)).unwrap_or(0);
            if stored == 2 {
                break;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        assert_eq!(stored, 2);
        let from_db = logs.query(&Query { limit: 10, ..Default::default() });
        assert_eq!(from_db["logs"].as_array().unwrap().len(), 2);
        assert_eq!(from_db["logs"][1]["msg"], "second");

        // 新进程（新 Logs）接着库里最大 id 续号。
        let reopened = Logs::new(dir);
        reopened.configure(true, Caps::default());
        let entry = reopened.add("third", None, Some("system"));
        assert_eq!(entry["id"].as_i64().unwrap(), 3);
        reopened.configure(false, Caps::default());
        logs.configure(false, Caps::default());
    }

    #[test]
    fn days_cap_keeps_rows_inside_the_window() {
        let dir = workdir();
        let path = dir.join("logs.sqlite3");
        let conn = connect(&path).expect("open");
        // 正好卡在边界上的那一行必须留下：cutoff 与写入口径都是空格分隔的本地时间。
        let edge = crate::state::local_stamp_for(now_epoch() - 7 * 86_400);
        for (id, ts) in [(1, edge.replace('T', " ")), (2, "2020-01-01 00:00:00".to_string())] {
            conn.execute(
                "INSERT INTO logs (id, ts, time, level, tag, msg) VALUES (?1, ?2, '00:00:00', 'INFO', 'system', 'x')",
                rusqlite::params![id, ts],
            )
            .expect("insert");
        }
        trim_rows(&conn, &Caps { rows: None, days: Some(7), bytes: None });
        let left: i64 = conn.query_row("SELECT COUNT(*) FROM logs", [], |row| row.get(0)).unwrap();
        assert_eq!(left, 1);
    }

    #[test]
    fn access_lines_skip_panel_polling() {
        let stamp = "2026-11-01 12:34:56";
        assert!(access_line("10.0.0.2", "GET", "/logs?since_id=5&limit=200", 200, stamp).is_none());
        assert!(access_line("10.0.0.2", "GET", "/usage?range=today", 200, stamp).is_none());
        // 4xx 照样记，业务端点也照样记。
        let bad = access_line("10.0.0.2", "GET", "/usage", 500, stamp).unwrap();
        assert_eq!(bad, "10.0.0.2 - - [01/Nov/2026 12:34:56] \"GET /usage HTTP/1.1\" 500");
        let chat = access_line("10.0.0.2", "POST", "/v1/chat/completions", 200, stamp).unwrap();
        assert!(chat.contains("\"POST /v1/chat/completions HTTP/1.1\" 200"));
    }
}
