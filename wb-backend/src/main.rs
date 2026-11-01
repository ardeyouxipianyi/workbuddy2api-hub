#![recursion_limit = "512"]
//! wb-backend —— workbuddy2api-hub 的可选 Rust 后端。
//!
//! M1 只做：`/health`、`/v1/models`、非流式 chat、账号/settings 读、usage 追加。
//! 契约来源是 Python 版（wb_proxy.py），逐字节对齐由 harness/compare.py 判定。

mod auth;
mod accounts_ops;
mod catalog_refresh;
mod chat;
mod codearts;
mod daily_usage;
mod egress;
mod fingerprint;
mod health;
mod credits;
mod identity;
mod http;
mod keyref;
mod launcher;
mod models;
mod activity;
pub mod agentcfg;
mod agents;
mod logs;
mod misc_routes;
mod panel;
mod panel_account;
mod panel_defaults;
mod panel_limits;
mod panel_password;
mod panel_read;
mod panel_settings;
mod panel_usage;
mod usage_index;
mod pricing;
mod pricing_catalog;
mod pricing_cost;
mod pricing_engine;
mod pricing_io;
mod pricing_panel;
mod pricing_policy;
mod pricing_settings;
mod pricing_store;
mod rate_limit;
#[cfg(test)]
mod pricing_tests;
mod pricing_view;
mod proxy_slots;
mod scheduler;
mod pool;
mod pool_breaker;
mod pool_guards;
mod quota;
mod record;
mod responses;
mod responses_compact;
#[cfg(test)]
mod responses_compact_tests;
mod slots;
mod sse;
mod state;
mod stream;
mod streak_bonus;
mod tasks;
mod update_backups;
mod update_apply;
#[cfg(test)]
mod update_backups_tests;
mod update_config;
mod update_download;
mod update_manager;
mod update_release;
mod update_requests;
mod update_state;
#[cfg(test)]
mod update_tests;
mod updates;
mod upstream;
mod usage;
mod v1_compat;

use std::sync::Arc;

use hyper::body::Incoming;
use hyper::service::service_fn;
use hyper::Request;
use hyper_util::rt::TokioIo;
use tokio::net::TcpListener;

/// 与上游约定的对话路径（wb_proxy.py:292）。
pub const CHAT_PATH: &str = "/v2/chat/completions";
/// 发布版由 CI 用 `WB_BUILD_VERSION=<tag>` 注入，让 `/health` 和 release 上的
/// 二进制版本对得上；本地构建回落到 Cargo.toml 的版本。
pub const VERSION: &str = match option_env!("WB_BUILD_VERSION") {
    Some(version) => version,
    None => env!("CARGO_PKG_VERSION"),
};

/// 进程级只读状态 + 写者。端口唯一属主是子进程自己（launcher 不 bind）。
pub struct App {
    pub state: state::State,
    pub upstream: reqwest::Client,
    pub started: std::time::Instant,
    /// The port the update helper probes after the supervisor restarts us.
    pub listen_port: u16,
    /// 在飞请求数：`/health` 会报给 launcher，切换时 launcher 等它归零。
    pub inflight: std::sync::atomic::AtomicUsize,
    /// usage.jsonl 的写者（单进程串行追加）。
    pub recorder: usage::Recorder,
    /// 账号冷却表（内存态，重启清空）。
    pub pool: pool::Pool,
    /// 面板会话（X-Panel-Token）与口令。
    pub panel: panel::Panel,
    /// OAuth device-flow sessions and desktop import scratch state.
    pub(crate) accounts_ops: accounts_ops::AccountsOps,
    /// 价估算策略、按行回算与手动刷新状态。
    pub pricing: pricing::Pricing,
    pub scheduler: scheduler::Scheduler,
    pub updates: std::sync::Arc<updates::Manager>,
    /// 面板「运行日志」页：内存环形缓冲 + 可选 SQLite 落库（见 logs.rs）。
    pub logs: logs::Logs,
    /// 对话并发闸门（Python 的 `_chat_slots`）：许可 drop 即释放。
    pub slots: slots::Slots,
}

impl App {
    /// 记一条运行日志（面板「运行日志」页用；同一条也给 stderr）。
    pub fn log(&self, tag: &str, level: &str, msg: &str) {
        eprintln!("[{tag}] {msg}");
        self.logs.add(msg, Some(level), Some(tag));
    }

    /// 同 `log`，但不指定级别/模块，交给关键字推断（Python 的 `log()` 默认行为：
    /// 访问日志、chat 起止这些行本来就只有一个自由文本）。
    pub fn note(&self, msg: &str) {
        eprintln!("{msg}");
        self.logs.add(msg, None, None);
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
            "--version" | "-V" => {
                // 发布流水线用它核对二进制自带的版本号。
                println!("wb-backend {VERSION}");
                std::process::exit(0);
            }
            // launcher 会带 --serve/--supervise 之外的后端通用参数，未知参数按 Python 版忽略。
            other => eprintln!("wb-backend: ignoring unknown argument {other}"),
        }
        i += 1;
    }
    Args { host, port, accounts_dir, usage_dir }
}

#[tokio::main]
async fn main() -> std::io::Result<()> {
    if updates::run_helper_if_requested().await {
        return Ok(());
    }
    let args = parse_args();
    let state = state::State::load(&args.accounts_dir, &args.usage_dir);
    let pricing = pricing::Pricing::new(std::path::Path::new(&args.usage_dir));
    // shortcut: 上游连接不做池化调优，M1 只要求功能等价，压测在 M7 前再看
    let upstream = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(600))
        .build()
        .expect("build http client");
    let ready_accounts = state.ready_count();
    let app = Arc::new(App {
        state,
        upstream,
        started: std::time::Instant::now(),
        listen_port: args.port,
        inflight: std::sync::atomic::AtomicUsize::new(0),
        recorder: usage::Recorder::new(args.usage_dir.clone()),
        pool: pool::Pool::new(),
        panel: panel::Panel::default(),
        accounts_ops: accounts_ops::AccountsOps::new(),
        pricing,
        scheduler: scheduler::Scheduler::default(),
        updates: std::sync::Arc::new(updates::Manager::new(&args.accounts_dir)),
        logs: logs::Logs::new(args.usage_dir.clone()),
        // `auto` 的上限按就绪账号数算，所以要在状态装好之后才建。
        slots: slots::Slots::from_env(ready_accounts),
    });
    app.pool.refresh_guards(&app.state, false);
    // 日志落库开关与保留上限来自面板设置（Python 在启动后与每次保存设置时各调一次）。
    app.logs.configure_from(&app.state.settings_snapshot());
    crate::http::apply_payload_limit(&app.state.settings_snapshot());

    scheduler::spawn(app.clone());
    updates::spawn(app.clone());

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
                // 访问日志要在 route 之前取：req 会被交给 handler 吃掉。
                let method = req.method().as_str().to_string();
                let path = req
                    .uri()
                    .path_and_query()
                    .map(|p| p.as_str().to_string())
                    .unwrap_or_default();
                // 对端地址只有 accept 时拿得到：面板登录限流按它分桶，/agents 的
                // 本机限定也按它判断回环。
                req.extensions_mut().insert(http::PeerAddr(peer.ip()));
                async move {
                    let resp = http::route(app.clone(), req).await;
                    // Python 的 log_message：一行一个请求，面板高频轮询的 200 GET 不记。
                    let stamp = state::local_stamp();
                    if let Some(line) = logs::access_line(
                        &peer.ip().to_string(),
                        &method,
                        &path,
                        resp.status().as_u16(),
                        &stamp,
                    ) {
                        app.note(&line);
                    }
                    Ok::<_, std::convert::Infallible>(resp)
                }
            });
            let _ = hyper::server::conn::http1::Builder::new()
                .serve_connection(io, service)
                .await;
        });
    }
}
