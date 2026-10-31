//! Missing account-management routes from `wb_proxy.py`.
//!
//! Route handlers stay here so `panel.rs` only owns authentication and dispatch.

mod config;
mod credits;
mod daily_web;
mod desktop;
mod device;
mod device_account;
mod device_http;
mod profile;
mod refresh;
mod test;
mod util;

#[cfg(test)]
mod tests;

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use hyper::body::Incoming;
use hyper::{Method, Request, StatusCode};
use serde_json::json;

use crate::http::{json_resp, Resp};
use crate::App;

#[derive(Clone)]
pub(crate) struct LoginSession {
    pub(crate) created: f64,
    pub(crate) platform: String,
    pub(crate) realm: String,
}

pub struct AccountsOps {
    logins: Mutex<HashMap<String, LoginSession>>,
    at_rest: desktop::AtRestState,
}

impl Default for AccountsOps {
    fn default() -> Self {
        Self::new()
    }
}

impl AccountsOps {
    pub fn new() -> Self {
        Self {
            logins: Mutex::new(HashMap::new()),
            at_rest: desktop::AtRestState::default(),
        }
    }

    pub(crate) fn put_login(&self, state: String, session: LoginSession) {
        if let Ok(mut logins) = self.logins.lock() {
            logins.insert(state, session);
        }
    }

    pub(crate) fn login(&self, state: &str) -> Option<LoginSession> {
        self.logins.lock().ok()?.get(state).cloned()
    }

    pub(crate) fn remove_login(&self, state: &str) -> bool {
        self.logins
            .lock()
            .map(|mut logins| logins.remove(state).is_some())
            .unwrap_or(false)
    }
}

pub(crate) fn handles(method: &Method, path: &str) -> bool {
    matches!(
        (method, path),
        (&Method::POST, "/accounts/login/start")
            | (&Method::GET, "/accounts/login/poll")
            | (&Method::POST, "/accounts/login/cancel")
            | (&Method::POST, "/accounts/refresh")
            | (&Method::POST, "/accounts/sync-profile")
            | (&Method::POST, "/accounts/test")
            | (&Method::GET, "/accounts/credits/detail")
            | (&Method::POST, "/accounts/credits/detail")
            | (&Method::POST, "/accounts/credits/fetch")
            | (&Method::POST, "/accounts/daily-chat-web")
            | (&Method::POST, "/accounts/import/desktop")
    )
}

pub async fn route(
    app: &Arc<App>,
    req: Request<Incoming>,
    method: &Method,
    path: &str,
) -> Option<Resp> {
    match (method, path) {
        (&Method::POST, "/accounts/login/start") => Some(device::start(app, req).await),
        (&Method::GET, "/accounts/login/poll") => Some(device::poll(app, &req).await),
        (&Method::POST, "/accounts/login/cancel") => Some(device::cancel(app, req).await),
        (&Method::POST, "/accounts/refresh") => Some(refresh::handle(app, req).await),
        (&Method::POST, "/accounts/sync-profile") => Some(profile::handle(app, req).await),
        (&Method::POST, "/accounts/test") => Some(test::handle(app, req).await),
        (&Method::GET, "/accounts/credits/detail") => Some(credits::detail_get(app, &req).await),
        (&Method::POST, "/accounts/credits/detail") => Some(credits::detail_post(app, req).await),
        (&Method::POST, "/accounts/credits/fetch") => Some(credits::fetch(app, req).await),
        (&Method::POST, "/accounts/daily-chat-web") => Some(daily_web::handle(app, req).await),
        (&Method::POST, "/accounts/import/desktop") => Some(desktop::handle(app, req).await),
        _ => None,
    }
}

pub(crate) fn unknown_endpoint(method: &Method) -> Resp {
    let message = if method == Method::POST {
        "unknown account endpoint"
    } else {
        "not found"
    };
    json_resp(
        StatusCode::NOT_FOUND,
        &json!({"error": {
            "message": message,
            "type": "invalid_request_error",
            "code": 404
        }}),
    )
}
