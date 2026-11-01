//! 路由与鉴权（M1 子集）。
//!
//! 越界返回 404 而不是猜一个 handler：M2 起逐条搬，未搬的路由返回 501 并在
//! capabilities 里保持 `ported: false`，面板据此显示「不可用」。

use std::sync::Arc;

use http_body_util::combinators::BoxBody;
use http_body_util::{BodyExt, Full};
use hyper::body::{Bytes, Incoming};
use hyper::{Method, Request, Response, StatusCode};
use serde_json::{json, Value};

use crate::{auth, health, models, App};

mod anthropic;
mod anthropic_content;
mod anthropic_sse;
mod anthropic_stream;
mod anthropic_translate;
mod balance;
mod completions;
mod protocol;

/// Client address retained past hyper's connection layer for local-only routes.
#[derive(Clone, Copy)]
pub struct PeerAddr(pub std::net::IpAddr);

/// 流式与非流式共用一种响应体：M1 的 `Full` 装不下 SSE 的增量写。
pub type Resp = Response<BoxBody<Bytes, std::convert::Infallible>>;

fn full(bytes: Bytes) -> BoxBody<Bytes, std::convert::Infallible> {
    Full::new(bytes).map_err(|e| match e {}).boxed()
}

pub fn json_resp(code: StatusCode, payload: &Value) -> Resp {
    let body = serde_json::to_vec(payload).unwrap_or_else(|_| b"{}".to_vec());
    let mut resp = Response::new(full(Bytes::from(body)));
    *resp.status_mut() = code;
    resp.headers_mut().insert(
        hyper::header::CONTENT_TYPE,
        hyper::header::HeaderValue::from_static("application/json; charset=utf-8"),
    );
    resp
}

pub fn text_resp(code: StatusCode, text: &str) -> Resp {
    let mut resp = Response::new(full(Bytes::from(text.to_string())));
    *resp.status_mut() = code;
    resp
}

/// Python 版所有错误都是 `{"error": {...}}`；M1 只用到这一种信封。
/// 并发闸门排不上队时的 503（OpenAI 信封；`/v1/messages` 用 anthropic::error）。
fn busy(app: &App) -> Resp {
    json_resp(
        StatusCode::SERVICE_UNAVAILABLE,
        &json!({"error": {
            "message": app.slots.busy_message(),
            "type": "server_error",
            "code": 503,
            "gateway_hint": crate::slots::Slots::BUSY_HINT,
        }}),
    )
}

pub fn error_resp(code: StatusCode, message: &str) -> Resp {
    json_resp(
        code,
        &json!({"error": {
            "message": message,
            "type": "invalid_request_error",
            "code": code.as_u16(),
        }}),
    )
}

fn auth_hint(has_key: bool) -> &'static str {
    if has_key {
        "send it as 'Authorization: Bearer <key>' or '?key=<key>'; \
         copy the value from the panel's 设置 page"
    } else {
        "no key is configured - open the dashboard and add one, \
         or restart with --api-key"
    }
}

/// `/v1` 的鉴权闸门：返回写进 usage 行的 key id，失败时返回 401 响应。
/// 报文与 Python 版（wb_proxy.py:18866-18881）逐字一致，客户端看到的是同一句提示。
pub fn require_auth(app: &App, req: &Request<Incoming>) -> Result<String, Resp> {
    // 面板会话也算「已授权」：浏览器只带 X-Panel-Token 去读 /v1/models 这类管理接口，
    // 不该把 API key 放进 localStorage（Python 的 _key_ok 同一条规则）。
    if crate::panel::session_ok(app, req) {
        return Ok(String::new());
    }
    let supplied = auth::supplied(req.headers(), req.uri().query());
    match auth::check(&app.state, &supplied) {
        Ok(passed) if !passed.required => Ok(String::new()),
        Ok(passed) => Ok(passed.key_id),
        Err(()) => {
            let has_key = app
                .state
                .settings()
                .get("api_keys")
                .and_then(Value::as_array)
                .map(|keys| {
                    keys.iter().any(|k| k.get("enabled").and_then(Value::as_bool).unwrap_or(false))
                })
                .unwrap_or(false)
                || std::env::var("WB_API_KEY").map(|v| !v.is_empty()).unwrap_or(false);
            let hint = auth_hint(has_key);
            Err(error_resp(
                StatusCode::UNAUTHORIZED,
                &format!("invalid api key - {hint}"),
            ))
        }
    }
}

pub async fn route(app: Arc<App>, mut req: Request<Incoming>) -> Resp {
    if let Err(resp) = crate::keyref::prepare(&app, &mut req) {
        return resp;
    }
    let path = req.uri().path().to_string();
    let method = req.method().clone();
    // 账号与设置按 mtime 重读：面板改 key、切区域、换号都不用重启。
    app.state.refresh();
    match (&method, path.as_str()) {
        (&Method::GET, "/health") => health::get(&app).await,
        // 模型目录不是「公开发现」：Python 的 _get_v1_models 也先 _authorized()，
        // 没 key 就 401。少了这一层，切到 rust 之后整个目录对外裸奔。
        (&Method::GET, "/v1/models") | (&Method::GET, "/models") => match require_auth(&app, &req) {
            Ok(_) => models::list(&app, &req).await,
            Err(resp) => resp,
        },
        (&Method::GET, "/v1/balance-summary") => match protocol::authorize(&app, &req) {
            Ok(identity) => balance::summary(&app, &req, &identity),
            Err(()) => protocol::openai_auth_error(&app),
        },
        (&Method::POST, "/v1/chat/completions") | (&Method::POST, "/chat/completions") => {
            let _slot = match app.slots.acquire().await {
                Some(permit) => permit,
                None => return busy(&app),
            };
            match require_auth(&app, &req) {
                Ok(key_id) => crate::chat::completions(&app, req, key_id).await,
                Err(resp) => resp,
            }
        }
        (&Method::POST, "/v1/completions") => {
            let _slot = match app.slots.acquire().await {
                Some(permit) => permit,
                None => return busy(&app),
            };
            match protocol::authorize(&app, &req) {
                Ok(identity) => completions::handle(&app, req, identity).await,
                Err(()) => protocol::openai_auth_error(&app),
            }
        }
        // count_tokens 不占对话槽（Python 里它走的是另一条分支）。
        (&Method::POST, "/v1/messages/count_tokens") => anthropic::handle(&app, req).await,
        (&Method::POST, "/v1/messages") => {
            let _slot = match app.slots.acquire().await {
                Some(permit) => permit,
                None => {
                    return anthropic::error(
                        StatusCode::SERVICE_UNAVAILABLE,
                        &app.slots.busy_message(),
                        Some("overloaded_error"),
                    )
                }
            };
            anthropic::handle(&app, req).await
        }
        (&Method::POST, "/v1/responses") | (&Method::POST, "/responses") => {
            let _slot = match app.slots.acquire().await {
                Some(permit) => permit,
                None => return busy(&app),
            };
            match require_auth(&app, &req) {
                Ok(key_id) => crate::responses::handle(&app, req, key_id).await,
                Err(resp) => resp,
            }
        }
        (&Method::POST, "/v1/responses/compact") | (&Method::POST, "/responses/compact") => {
            match require_auth(&app, &req) {
                Ok(key_id) => crate::responses::handle(&app, req, key_id).await,
                Err(resp) => resp,
            }
        }
        (&Method::GET, "/v1/usage") => crate::v1_compat::usage(&app, &req),
        (&Method::GET, "/agents/cli") => crate::misc_routes::agents_cli(),
        (&Method::GET, "/updates") => crate::updates::get(&app, &req).await,
        (&Method::GET, "/updates/backups") => crate::updates::backups(&app, &req).await,
        (&Method::POST, "/updates/rollback") => {
            crate::updates::rollback(&app, req, app.listen_port).await
        }
        (&Method::POST, "/updates/check") => crate::updates::check(&app, &req).await,
        (&Method::POST, "/updates/install") => {
            crate::updates::install(&app, &req, app.listen_port).await
        }
        (&Method::OPTIONS, _) => text_resp(StatusCode::NO_CONTENT, ""),
        (_, "/health")
        | (_, "/v1/models")
        | (_, "/v1/balance-summary")
        | (_, "/v1/chat/completions")
        | (_, "/v1/completions")
        | (_, "/v1/messages")
        | (_, "/v1/messages/count_tokens")
        | (_, "/v1/responses")
        | (_, "/v1/responses/compact")
        | (_, "/v1/usage")
        | (_, "/agents/cli")
        | (_, "/updates/backups")
        | (_, "/updates/rollback") => {
            error_resp(StatusCode::METHOD_NOT_ALLOWED, "method not allowed")
        }
        _ => match crate::panel::handle(&app, req).await {
            Some(resp) => resp,
            None => error_resp(StatusCode::NOT_FOUND, "not found"),
        },
    }
}

/// `?realm=` > `X-Realm` 头 > 当前区域（wb_proxy.py:19224 同一优先级）。
/// 会话键（specs/identity-upstream.md §1.6）：头优先，再 body 三个位置。
/// 都没有就返回 None，由 identity 层每请求新生成（上游按它做会话亲和）。
pub fn session_key_header(headers: &hyper::HeaderMap) -> Option<String> {
    for name in ["x-conversation-id", "conversation-id", "x-session-id", "session-id"] {
        if let Some(v) = headers.get(name).and_then(|v| v.to_str().ok()) {
            if !v.trim().is_empty() {
                return Some(v.trim().to_string());
            }
        }
    }
    None
}

pub fn session_key_body(body: &Value) -> Option<String> {
    let candidates = [
        body.get("conversation_id"),
        body.get("session_id"),
        body.get("metadata").and_then(|m| m.get("conversation_id")),
    ];
    for value in candidates.into_iter().flatten() {
        if let Some(s) = value.as_str() {
            if !s.trim().is_empty() {
                return Some(s.trim().to_string());
            }
        }
    }
    None
}

pub fn requested_realm(req: &Request<Incoming>, current: &str) -> String {
    // `codearts` 也原样放行：rust 还没实现这个出口，明确回「池子里没有可用账号」
    // 比悄悄按当前出口派单诚实（v1.10.0 的第三个 realm）。
    fn accept(value: &str) -> Option<String> {
        matches!(value, "cn" | "intl" | "codearts").then(|| value.to_string())
    }
    if let Some(q) = req.uri().query() {
        for pair in q.split('&') {
            if let Some(v) = pair.strip_prefix("realm=") {
                if let Some(realm) = accept(v) {
                    return realm;
                }
            }
        }
    }
    if let Some(v) = req.headers().get("x-realm").and_then(|v| v.to_str().ok()) {
        if let Some(realm) = accept(v) {
            return realm;
        }
    }
    current.to_string()
}

/// 面板设置 `max_payload_mb` 生效后的上限；0 = 还没配置过，回落环境变量。
static PAYLOAD_LIMIT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

/// 请求体上限：`WB_MAX_PAYLOAD_BYTES`，默认 16 MiB（wb_proxy.py:42）。
fn env_payload_limit() -> usize {
    std::env::var("WB_MAX_PAYLOAD_BYTES")
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .unwrap_or(16 * 1024 * 1024)
}

/// 面板的 `max_payload_mb`（1..=256）优先，没设就用环境变量/默认值。
pub fn payload_bytes_effective(settings: &serde_json::Map<String, Value>) -> usize {
    settings
        .get("max_payload_mb")
        .and_then(Value::as_i64)
        .filter(|mb| (1..=256).contains(mb))
        .map(|mb| mb as usize * 1024 * 1024)
        .unwrap_or_else(env_payload_limit)
}

/// 启动时与每次保存设置都调一次：改完立刻生效，不用重启（v1.10.0 的设置项）。
pub fn apply_payload_limit(settings: &serde_json::Map<String, Value>) {
    PAYLOAD_LIMIT.store(payload_bytes_effective(settings), std::sync::atomic::Ordering::Relaxed);
}

fn payload_limit() -> usize {
    match PAYLOAD_LIMIT.load(std::sync::atomic::Ordering::Relaxed) {
        0 => env_payload_limit(),
        limit => limit,
    }
}

/// 超限报文的 `gateway_hint`（wb_proxy.py:13722）——客户端/人靠它知道去改哪个开关。
fn gateway_hint_413() -> &'static str {
    "single request body exceeds the gateway cap; reduce the request or \
     increase the limit in panel settings (or WB_MAX_PAYLOAD_BYTES before startup)"
}

/// 读整个请求体；超限时回 Python 版同一条 413 报文。
pub async fn body_json(req: Request<Incoming>) -> Result<Value, Resp> {
    let limit = payload_limit();
    let bytes = http_body_util::BodyExt::collect(req.into_body())
        .await
        .map_err(|e| {
            error_resp(StatusCode::BAD_REQUEST, &format!("invalid request body: {e}"))
        })?
        .to_bytes();
    if bytes.len() > limit {
        return Err(json_resp(
            StatusCode::PAYLOAD_TOO_LARGE,
            &json!({"error": {
                "message": format!("payload too large ({} bytes > {} limit)", bytes.len(), limit),
                "type": "invalid_request_error",
                "code": 413,
                "gateway_hint": gateway_hint_413(),
            }}),
        ));
    }
    serde_json::from_slice(&bytes)
        .map_err(|_| error_resp(StatusCode::BAD_REQUEST, "invalid JSON body"))
}

#[cfg(test)]
mod tests {
    use http_body_util::BodyExt;

    use super::*;

    #[test]
    fn payload_limit_follows_the_panel_setting() {
        let empty = serde_json::Map::new();
        assert_eq!(payload_bytes_effective(&empty), 16 * 1024 * 1024);
        let mut settings = serde_json::Map::new();
        settings.insert("max_payload_mb".into(), json!(3));
        assert_eq!(payload_bytes_effective(&settings), 3 * 1024 * 1024);
        // 越界值不生效（保存时本就拦掉了，这里防手工改坏 settings.json）。
        for bad in [0, 257, -5] {
            settings.insert("max_payload_mb".into(), json!(bad));
            assert_eq!(payload_bytes_effective(&settings), 16 * 1024 * 1024);
        }
    }

    #[tokio::test]
    async fn api_error_envelope_has_python_code_field() {
        let response = error_resp(StatusCode::UNAUTHORIZED, "invalid api key - test");
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
        let body = response.into_body().collect().await.unwrap().to_bytes();
        assert_eq!(
            serde_json::from_slice::<Value>(&body).unwrap(),
            json!({"error": {
                "message": "invalid api key - test",
                "type": "invalid_request_error",
                "code": 401
            }})
        );
    }

    #[test]
    fn key_hint_is_byte_for_byte_python_text() {
        assert_eq!(
            auth_hint(true),
            "send it as 'Authorization: Bearer <key>' or '?key=<key>'; copy the value from the panel's 设置 page"
        );
    }
}
