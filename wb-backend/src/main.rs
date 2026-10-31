#![recursion_limit = "512"]
//! wb-backend —— workbuddy2api-hub 的可选 Rust 后端。
//!
//! M1 只做：`/health`、`/v1/models`、非流式 chat、账号/settings 读、usage 追加。
//! 契约来源是 Python 版（wb_proxy.py），逐字节对齐由 harness/compare.py 判定。

mod auth;
mod chat;
mod fingerprint;
mod health;
mod credits;
mod identity;
mod http;
mod launcher;
mod models;
mod activity;
mod agents;
mod panel;
mod panel_defaults;
mod panel_read;
mod panel_settings;
mod panel_usage;
mod scheduler;
mod pool;
mod quota;
mod record;
mod responses;
mod sse;
mod state;
mod stream;
mod tasks;
mod upstream;
mod usage;

use std::sync::Arc;

use hyper::body::Incoming;
use hyper::service::service_fn;
use hyper::Request;
use hyper_util::rt::TokioIo;
use tokio::net::TcpListener;

/// 与上游约定的对话路径（wb_proxy.py:292）。
pub const CHAT_PATH: &str = "/v2/chat/completions";
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// 进程级只读状态 + 写者。端口唯一属主是子进程自己（launcher 不 bind）。
pub struct App {
    pub state: state::State,
    pub upstream: reqwest::Client,
    pub started: std::time::Instant,
    /// 在飞请求数：`/health` 会报给 launcher，切换时 launcher 等它归零。
    pub inflight: std::sync::atomic::AtomicUsize,
    /// usage.jsonl 的写者（单进程串行追加）。
    pub recorder: usage::Recorder,
    /// 账号冷却表（内存态，重启清空）。
    pub pool: pool::Pool,
    /// 面板会话（X-Panel-Token）与口令。
    pub panel: panel::Panel,
    pub scheduler: scheduler::Scheduler,
    /// 最近 500 条面板/后端事件，`GET /logs` 直接读它（面板「运行日志」页）。
    pub logs: std::sync::Mutex<std::collections::VecDeque<serde_json::Value>>,
}

impl App {
    /// 记一条运行日志（面板「运行日志」页用；同一条也给 stderr）。
    pub fn log(&self, tag: &str, level: &str, msg: &str) {
        eprintln!("[{tag}] {msg}");
        let entry = serde_json::json!({
            "id": crate::sse::hex_token(8),
            "ts": crate::state::now_epoch(),
            "time": crate::state::local_stamp(),
            "level": level,
            "tag": tag,
            "msg": msg,
        });
        if let Ok(mut ring) = self.logs.lock() {
            ring.push_back(entry);
            while ring.len() > 500 {
                ring.pop_front();
            }
        }
    }

    pub fn recorder_path(&self) -> std::path::PathBuf {
        self.recorder.path()
    }
}

fn env_or(name: &str, default: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| default.to_string())
}

struct Args {
    host: String,
    port: u16,
    accounts_dir: String,
    usage_dir: String,
}

fn parse_args() -> Args {
    let mut host = env_or("WB_HOST", "0.0.0.0");
    let mut port: u16 = env_or("WB_PORT", "8788").parse().unwrap_or(8788);
    let mut accounts_dir = env_or("ACCOUNTS_DIR", "accounts");
    let mut usage_dir = env_or("WB_PROXY_USAGE_DIR", "usage");
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let mut i = 0;
    while i < argv.len() {
        let take = |i: &mut usize| -> String {
            *i += 1;
            argv.get(*i).cloned().unwrap_or_default()
        };
        match argv[i].as_str() {
            "--host" => host = take(&mut i),
            "--port" => port = take(&mut i).parse().unwrap_or(port),
            "--accounts-dir" => accounts_dir = take(&mut i),
            "--usage-dir" => usage_dir = take(&mut i),
            // launcher 会带 --serve/--supervise 之外的后端通用参数，未知参数按 Python 版忽略。
            other => eprintln!("wb-backend: ignoring unknown argument {other}"),
        }
        i += 1;
    }
    Args { host, port, accounts_dir, usage_dir }
}

#[tokio::main]
async fn main() -> std::io::Result<()> {
    let args = parse_args();
    let state = state::State::load(&args.accounts_dir, &args.usage_dir);
    // shortcut: 上游连接不做池化调优，M1 只要求功能等价，压测在 M7 前再看
    let upstream = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(600))
        .build()
        .expect("build http client");
    let app = Arc::new(App {
        state,
        upstream,
        started: std::time::Instant::now(),
        inflight: std::sync::atomic::AtomicUsize::new(0),
        recorder: usage::Recorder::new(args.usage_dir.clone()),
        pool: pool::Pool::new(),
        panel: panel::Panel::default(),
        scheduler: scheduler::Scheduler::default(),
        logs: std::sync::Mutex::new(std::collections::VecDeque::new()),
    });

    scheduler::spawn(app.clone());

    let listener = TcpListener::bind((args.host.as_str(), args.port)).await?;
    eprintln!(
        "wb-backend listening on {}:{} (accounts={} usage={})",
        args.host, args.port, args.accounts_dir, args.usage_dir
    );

    loop {
        let (stream, peer) = match listener.accept().await {
            Ok(pair) => pair,
            Err(_) => continue,
        };
        let app = app.clone();
        tokio::spawn(async move {
            let io = TokioIo::new(stream);
            let service = service_fn(move |mut req: Request<Incoming>| {
                let app = app.clone();
                // 对端地址只有 accept 时拿得到：面板登录限流按它分桶，/agents 的
                // 本机限定也按它判断回环。
                req.extensions_mut().insert(http::PeerAddr(peer.ip()));
                async move { Ok::<_, std::convert::Infallible>(http::route(app, req).await) }
            });
            let _ = hyper::server::conn::http1::Builder::new()
                .serve_connection(io, service)
                .await;
        });
    }
}
