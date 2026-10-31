//! 后端切换控制面：面板 → launcher（`accounts/launcher.sock`）。
//!
//! 协议是「一行 JSON 进、一行 JSON 出」，launcher 侧在 `wb_backend.py` 的
//! `serve_control()`，命令只有 `status` / `switch` / `shutdown`。
//! 为什么 Rust 侧也得有：切到 rust 之后面板还挂着，没有这两条路由就再也切不回 python，
//! 只能手改 `backend.json` + 重启。契约照 `wb_proxy.py` 的
//! `_route_backend_status` / `_route_backend_switch`。

use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::Duration;

use hyper::body::Incoming;
use hyper::{Request, StatusCode};
use serde_json::{json, Value};

use crate::http::json_resp;
use crate::App;

/// 一条命令超时（秒）；切换本身最长 180s（drain + 健康闸门），状态查询 5s 足够。
fn call(app: &Arc<App>, request: Value, secs: u64) -> impl std::future::Future<Output = Option<Value>> {
    let path = app.state.accounts_dir.join("launcher.sock");
    let mut payload = serde_json::to_vec(&request).unwrap_or_default();
    payload.push(b'\n');
    async move {
        tokio::task::spawn_blocking(move || {
            use std::io::{BufRead, BufReader, Write};
            let sock = std::os::unix::net::UnixStream::connect(&path).ok()?;
            sock.set_read_timeout(Some(Duration::from_secs(secs))).ok()?;
            sock.set_write_timeout(Some(Duration::from_secs(secs))).ok()?;
            let mut writer = sock.try_clone().ok()?;
            writer.write_all(&payload).ok()?;
            let mut line = String::new();
            BufReader::new(sock).read_line(&mut line).ok()?;
            serde_json::from_str(&line).ok()
        })
        .await
        .ok()?
    }
}

/// `GET /admin/backend/status`：launcher 的状态，或「没有 launcher」的降级答复。
pub async fn status(app: &Arc<App>) -> crate::http::Resp {
    if let Some(state) = call(app, json!({"cmd": "status"}), 5).await {
        return json_resp(StatusCode::OK, &state);
    }
    json_resp(
        StatusCode::OK,
        &json!({
            "supervised": false,
            "backend": "rust",
            "healthy": true,
            // 探针自己也算在飞：与 /health 一样减掉它。
            "inflight": app.inflight.load(Ordering::Relaxed).saturating_sub(1),
            "python": {"available": true},
            "rust": {"available": true, "missing": [], "binary": "wb-backend"},
            "detail": "没有 launcher：本进程自己占着端口，无法切换",
        }),
    )
}

/// `POST /admin/backend {backend|to}`：异步 202，真正的切换在后台做。
///
/// 不同步等：切换会杀掉正在服务这个请求的进程，同步等会把自己算进在飞数里，
/// launcher 的 drain 永远等不到 0（`wb_proxy.py` 同一处注释）。
pub async fn switch(app: &Arc<App>, req: Request<Incoming>) -> crate::http::Resp {
    let body = match crate::http::body_json(req).await {
        Ok(v) => v,
        Err(resp) => return resp,
    };
    let target = body
        .get("backend")
        .or_else(|| body.get("to"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    if target != "python" && target != "rust" {
        return crate::http::error_resp(StatusCode::BAD_REQUEST, "backend must be python or rust");
    }
    // capabilities 的「rust 是否就绪」在这里不用判：本进程就是 rust 后端，能服务即就绪。
    if call(app, json!({"cmd": "status"}), 5).await.is_none() {
        return crate::http::error_resp(
            StatusCode::SERVICE_UNAVAILABLE,
            "no launcher is supervising this process; start it with --supervise to switch backends",
        );
    }
    let app = app.clone();
    let to = target.clone();
    tokio::spawn(async move {
        call(&app, json!({"cmd": "switch", "to": to}), 180).await;
    });
    json_resp(
        StatusCode::ACCEPTED,
        &json!({"ok": true, "switching_to": target, "poll": "/admin/backend/status"}),
    )
}
