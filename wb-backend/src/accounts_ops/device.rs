//! OAuth device-flow start/poll/cancel (`wb_accounts.AccountPool`).

use std::sync::Arc;

use hyper::body::Incoming;
use hyper::{Request, StatusCode};
use serde_json::{json, Map, Value};

use super::{config, util, LoginSession};
use crate::http::{body_json, json_resp, Resp};
use crate::App;

const AUTH_STATE_PATH: &str = "/v2/plugin/auth/state";
const AUTH_TOKEN_PATH: &str = "/v2/plugin/auth/token";
const LOGIN_ACCOUNT_PATH: &str = "/v2/plugin/login/account";
const LOGIN_PENDING: i64 = 11217;

fn request_headers(realm: &str) -> Vec<(&'static str, String)> {
    let origin = config::origin(realm);
    vec![
        ("Content-Type", "application/json".into()),
        ("Accept", "application/json, text/plain, */*".into()),
        ("X-Requested-With", "XMLHttpRequest".into()),
        ("User-Agent", config::billing_user_agent(realm)),
        ("Origin", origin.clone()),
        ("Referer", format!("{origin}/")),
    ]
}

fn apply_headers(
    mut req: reqwest::RequestBuilder,
    headers: impl IntoIterator<Item = (String, String)>,
) -> reqwest::RequestBuilder {
    for (key, value) in headers {
        req = req.header(key, value);
    }
    req
}

pub(super) async fn start(app: &Arc<App>, req: Request<Incoming>) -> Resp {
    let body = match body_json(req).await {
        Ok(value) => util::object_or_empty(value),
        Err(resp) => return resp,
    };
    start_with_body(app, &body).await
}

pub(super) async fn start_with_body(app: &Arc<App>, body: &Map<String, Value>) -> Resp {
    let platform = text(body, "platform", "CLI");
    let realm = body
        .get("realm")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .unwrap_or(&app.state.current_realm())
        .to_string();
    let payload = match start_request(app, &realm, &platform).await {
        Ok(payload) => payload,
        Err(error) => return util::error(StatusCode::BAD_GATEWAY, error),
    };
    let data = payload.get("data").and_then(Value::as_object);
    let state = data
        .and_then(|data| data.get("state"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let auth_url = data
        .and_then(|data| data.get("authUrl"))
        .and_then(Value::as_str)
        .unwrap_or("");
    if state.is_empty() || auth_url.is_empty() {
        return util::error(
            StatusCode::BAD_GATEWAY,
            format!("could not start login: auth/state returned no state/authUrl: {payload}"),
        );
    }
    begin_login(app, state, auth_url, &realm, &platform)
}

fn text(body: &Map<String, Value>, key: &str, default: &str) -> String {
    body.get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .unwrap_or(default)
        .to_string()
}

async fn start_request(app: &Arc<App>, realm: &str, platform: &str) -> Result<Value, String> {
    let url = format!(
        "{}{}?platform={}",
        config::chat_base(realm),
        AUTH_STATE_PATH,
        util::encode_component(platform)
    );
    let request = apply_headers(
        app.upstream.post(url).json(&json!({})),
        request_headers(realm)
            .into_iter()
            .map(|(key, value)| (key.to_string(), value)),
    );
    super::device_http::send_json(
        request,
        config::timeout("WB_ACCOUNT_LOGIN_TIMEOUT_SECONDS", 30),
        retry_attempts("WB_ACCOUNT_LOGIN_RETRIES", 3),
    )
    .await
    .map_err(|exc| format!("could not start login: {exc}"))
}

fn begin_login(app: &Arc<App>, state: &str, auth_url: &str, realm: &str, platform: &str) -> Resp {
    app.accounts_ops.put_login(
        state.to_string(),
        LoginSession {
            created: crate::state::now_epoch() as f64,
            platform: platform.to_string(),
            realm: realm.to_string(),
        },
    );
    app.log(
        "accounts",
        "INFO",
        &format!(
            "oauth login started (realm={realm}, platform={platform}, state={})",
            &state[..state.len().min(8)]
        ),
    );
    json_resp(
        StatusCode::OK,
        &json!({
            "state": state,
            "authUrl": auth_url,
            "realm": realm,
            "platform": platform
        }),
    )
}

pub(super) async fn poll(app: &Arc<App>, req: &Request<Incoming>) -> Resp {
    let state = util::query_param(req.uri().query().unwrap_or(""), "state")
        .trim()
        .to_string();
    let Some(session) = app.accounts_ops.login(&state) else {
        return json_resp(
            StatusCode::OK,
            &json!({
                "status": "unknown",
                "message": "state not recognised - start the login again"
            }),
        );
    };
    if crate::state::now_epoch() as f64 - session.created > config::login_ttl_secs() {
        app.accounts_ops.remove_login(&state);
        return json_resp(
            StatusCode::OK,
            &json!({
                "status": "expired",
                "message": "login window expired - start again"
            }),
        );
    }
    poll_session(app, &state, &session).await
}

pub(super) async fn poll_session(app: &Arc<App>, state: &str, session: &LoginSession) -> Resp {
    let payload = match fetch_poll(app, state, session).await {
        Ok(payload) => payload,
        Err(error) => return poll_pending(format!("poll error: {error}")),
    };
    let data = match classify_poll(&payload) {
        PollStep::Ready(data) => data,
        PollStep::Pending(message) => return poll_pending(message),
        PollStep::Error(message) => return poll_status("error", message),
        PollStep::Unknown(message) => return poll_pending(message),
    };
    let token = data
        .get("accessToken")
        .and_then(Value::as_str)
        .unwrap_or("");
    let uid = crate::state::jwt_sub(token).unwrap_or_default();
    if !util::safe_uid(&uid) {
        return poll_status("error", "accessToken contains no safe uid".into());
    }
    finish_login(app, state, session, &data, token, &uid).await
}

enum PollStep {
    Ready(Map<String, Value>),
    Pending(String),
    Error(String),
    Unknown(String),
}

fn classify_poll(payload: &Value) -> PollStep {
    let code = payload.get("code").and_then(Value::as_i64);
    if code == Some(LOGIN_PENDING) {
        return PollStep::Pending(
            payload
                .get("msg")
                .and_then(Value::as_str)
                .unwrap_or("waiting for browser login")
                .to_string(),
        );
    }
    if code != Some(0) {
        return PollStep::Error(format!(
            "code={} msg={}",
            code.unwrap_or(-1),
            payload.get("msg").and_then(Value::as_str).unwrap_or("")
        ));
    }
    let Some(data) = payload.get("data").and_then(Value::as_object) else {
        return PollStep::Unknown("waiting for token".into());
    };
    if data
        .get("accessToken")
        .and_then(Value::as_str)
        .unwrap_or("")
        .is_empty()
    {
        return PollStep::Unknown("waiting for token".into());
    }
    PollStep::Ready(data.clone())
}

async fn fetch_poll(app: &Arc<App>, state: &str, session: &LoginSession) -> Result<Value, String> {
    let url = format!(
        "{}{}?state={}",
        config::chat_base(&session.realm),
        AUTH_TOKEN_PATH,
        util::encode_component(state)
    );
    let request = apply_headers(
        app.upstream.get(url),
        request_headers(&session.realm)
            .into_iter()
            .map(|(key, value)| (key.to_string(), value)),
    );
    super::device_http::send_json(
        request,
        config::timeout("WB_ACCOUNT_LOGIN_POLL_TIMEOUT_SECONDS", 30),
        retry_attempts("WB_ACCOUNT_LOGIN_POLL_RETRIES", 2),
    )
    .await
}

fn retry_attempts(name: &str, default: usize) -> usize {
    std::env::var(name)
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(default)
        .max(1)
}

fn poll_pending(message: String) -> Resp {
    poll_status("pending", message)
}

fn poll_status(status: &str, message: String) -> Resp {
    json_resp(
        StatusCode::OK,
        &json!({"status": status, "message": message}),
    )
}

async fn finish_login(
    app: &Arc<App>,
    state: &str,
    session: &LoginSession,
    data: &Map<String, Value>,
    token: &str,
    uid: &str,
) -> Resp {
    let nickname = fetch_nickname(app, state, session, token).await;
    let account = super::device_account::create(session, data, token, uid, &nickname);
    if let Err(exc) = super::device_account::persist(app, uid, account) {
        return poll_status("error", format!("could not save account: {exc}"));
    }
    if session.realm == "cn" {
        if let Some(saved) = util::find_account(app, uid) {
            let _ = crate::credits::checkin(app, &saved).await;
        }
    }
    app.accounts_ops.remove_login(state);
    let account = util::find_account(app, uid)
        .map(|saved| crate::panel::account_view(app, &saved, crate::state::now_epoch()))
        .unwrap_or(Value::Null);
    json_resp(StatusCode::OK, &json!({"status": "ok", "account": account}))
}

async fn fetch_nickname(
    app: &Arc<App>,
    state: &str,
    session: &LoginSession,
    token: &str,
) -> String {
    let url = format!(
        "{}{}?state={}",
        config::chat_base(&session.realm),
        LOGIN_ACCOUNT_PATH,
        util::encode_component(state)
    );
    let headers = request_headers(&session.realm)
        .into_iter()
        .map(|(key, value)| (key.to_string(), value))
        .chain(std::iter::once((
            "Authorization".to_string(),
            format!("Bearer {token}"),
        )));
    let request = apply_headers(app.upstream.get(url), headers);
    let Ok(response) = request
        .timeout(config::timeout("WB_ACCOUNT_PROFILE_TIMEOUT_SECONDS", 15))
        .send()
        .await
    else {
        return String::new();
    };
    let Ok(payload) = response.json::<Value>().await else {
        return String::new();
    };
    payload
        .get("data")
        .and_then(|data| data.get("nickname"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string()
}

pub(super) async fn cancel(app: &Arc<App>, req: Request<Incoming>) -> Resp {
    let body = match body_json(req).await {
        Ok(value) => util::object_or_empty(value),
        Err(resp) => return resp,
    };
    let state = body.get("state").and_then(Value::as_str).unwrap_or("");
    let cancelled = app.accounts_ops.remove_login(state);
    json_resp(StatusCode::OK, &json!({"cancelled": cancelled}))
}
