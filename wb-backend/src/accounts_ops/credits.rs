//! Credit detail and fetch aliases used by the panel.

use std::sync::Arc;

use hyper::body::Incoming;
use hyper::{Request, StatusCode};
use serde_json::{json, Value};

use super::util;
use crate::http::{body_json, json_resp, Resp};
use crate::state::AccountFile;
use crate::App;

pub(super) async fn detail_get(app: &Arc<App>, req: &Request<Incoming>) -> Resp {
    let query = req.uri().query().unwrap_or("");
    let uid = util::query_param(query, "uid");
    let refresh = matches!(
        util::query_param(query, "refresh").as_str(),
        "1" | "true" | "yes"
    );
    let account = if uid.is_empty() {
        app.state.accounts_snapshot().into_iter().next()
    } else {
        util::find_account(app, &uid)
    };
    match account {
        Some(account) => resolve_detail(app, account, refresh).await,
        None => json_resp(
            StatusCode::OK,
            &json!({"ok": false, "error": "未找到指定账号"}),
        ),
    }
}

pub(super) async fn detail_post(app: &Arc<App>, req: Request<Incoming>) -> Resp {
    let body = match body_json(req).await {
        Ok(value) => value,
        Err(resp) => return resp,
    };
    let body = util::object_or_empty(body);
    let uid = body.get("uid").and_then(Value::as_str).unwrap_or("");
    let account = util::find_account(app, uid);
    let refresh = body.get("refresh").and_then(Value::as_bool).unwrap_or(true);
    match account {
        Some(account) => resolve_detail(app, account, refresh).await,
        None => json_resp(
            StatusCode::OK,
            &json!({"ok": false, "error": "未找到指定账号"}),
        ),
    }
}

async fn resolve_detail(app: &Arc<App>, account: AccountFile, force: bool) -> Resp {
    let missing_packages = account
        .data
        .get("credits")
        .and_then(|credits| credits.get("packages"))
        .and_then(Value::as_array)
        .map(Vec::is_empty)
        .unwrap_or(true);
    if force || missing_packages {
        let result = crate::credits::fetch_one(app, &account).await;
        if result.get("ok").and_then(Value::as_bool) != Some(true) {
            let fresh = util::find_account(app, &account.uid()).unwrap_or(account);
            return json_resp(
                StatusCode::OK,
                &json!({
                    "ok": false,
                    "uid": fresh.uid(),
                    "nickname": fresh.str("nickname"),
                    "realm": fresh.realm(),
                    "credits": fresh.data.get("credits").cloned().unwrap_or(Value::Null),
                    "error": result.get("error").and_then(Value::as_str)
                        .unwrap_or("获取积分明细失败")
                }),
            );
        }
    }
    let fresh = util::find_account(app, &account.uid()).unwrap_or(account);
    ok_detail(&fresh)
}

fn ok_detail(account: &AccountFile) -> Resp {
    json_resp(
        StatusCode::OK,
        &json!({
            "ok": true,
            "uid": account.uid(),
            "nickname": account.str("nickname"),
            "realm": account.realm(),
            "credits": account.data.get("credits").cloned().unwrap_or(Value::Null)
        }),
    )
}

pub(super) async fn fetch(app: &Arc<App>, req: Request<Incoming>) -> Resp {
    let body = match body_json(req).await {
        Ok(value) => value,
        Err(resp) => return resp,
    };
    let body = util::object_or_empty(body);
    let uid = body.get("uid").and_then(Value::as_str).unwrap_or("");
    let realm = body.get("realm").and_then(Value::as_str).unwrap_or("");
    let out = crate::credits::sweep(app, uid, realm).await;
    json_resp(StatusCode::OK, &out)
}
