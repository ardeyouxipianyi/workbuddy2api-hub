//! `POST /accounts/refresh`: rotate one account or the whole pool explicitly.

use std::sync::Arc;

use hyper::body::Incoming;
use hyper::{Request, StatusCode};
use serde_json::{json, Value};

use super::util;
use crate::http::{body_json, json_resp, Resp};
use crate::state::AccountFile;
use crate::App;

pub(super) async fn handle(app: &Arc<App>, req: Request<Incoming>) -> Resp {
    let body = match body_json(req).await {
        Ok(value) => value,
        Err(resp) => return resp,
    };
    let body = util::object_or_empty(body);
    let uid = body.get("uid").and_then(Value::as_str).unwrap_or("");
    let targets = util::target_accounts(app, uid, "", false);
    let mut results = Vec::with_capacity(targets.len());
    for account in targets {
        results.push(refresh_one(app, &account).await);
    }
    json_resp(StatusCode::OK, &json!({"results": results}))
}

pub(super) async fn refresh_one(app: &Arc<App>, account: &AccountFile) -> Value {
    if account.is_api_key() {
        return json!({"uid": account.uid(), "ok": false, "error": ""});
    }
    if account.refresh_token().is_empty() {
        let message = "no refresh token; sign in again";
        persist_error(account, message);
        return json!({"uid": account.uid(), "ok": false, "error": message});
    }
    let ok = crate::pool::refresh(app, &account.uid()).await.is_some();
    let error = if ok {
        String::new()
    } else {
        util::find_account(app, &account.uid())
            .map(|fresh| fresh.str("lastError").to_string())
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| "refresh failed".to_string())
    };
    json!({"uid": account.uid(), "ok": ok, "error": error})
}

fn persist_error(account: &AccountFile, message: &str) {
    let mut data = account.data.clone();
    data.insert("lastError".into(), json!(message));
    let _ = crate::state::write_json_atomic(&account.path, &Value::Object(data));
}
