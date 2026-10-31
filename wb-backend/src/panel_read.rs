//! Panel read views whose selection rules differ from the core account/task paths.

use std::sync::Arc;

use hyper::body::Incoming;
use hyper::{Request, StatusCode};
use serde_json::{json, Value};

use crate::http::{json_resp, Resp};
use crate::state::AccountFile;
use crate::App;

fn query_value(req: &Request<Incoming>, name: &str) -> String {
    req.uri()
        .query()
        .unwrap_or("")
        .split('&')
        .find_map(|pair| pair.strip_prefix(name).and_then(|v| v.strip_prefix('=')))
        .unwrap_or("")
        .to_string()
}

/// `GET /accounts`: list the selected realm, with `?realm=` taking precedence.
pub fn accounts(app: &Arc<App>, req: &Request<Incoming>) -> Resp {
    let realm = crate::http::requested_realm(req, &app.state.current_realm());
    let now = crate::state::now_epoch();
    let list: Vec<Value> = app
        .state
        .accounts_snapshot()
        .iter()
        .filter(|account| realm == "all" || account.realm() == realm)
        .map(|account| crate::panel::account_view(account, now))
        .collect();
    json_resp(
        StatusCode::OK,
        &json!({
            "accounts": list,
            "storage": app.state.accounts_dir_name(),
            "usable": app.state.ready_count(),
        }),
    )
}

fn tasks_enabled(account: &AccountFile) -> bool {
    account
        .data
        .get("tasksEnabled")
        .and_then(Value::as_bool)
        .unwrap_or_else(|| account.enabled())
}

/// `GET /tasks`: only domestic accounts participating in scheduled tasks.
pub async fn tasks(app: &Arc<App>, req: &Request<Incoming>) -> Resp {
    let accounts: Vec<AccountFile> = app
        .state
        .accounts_snapshot()
        .into_iter()
        .filter(|account| account.realm() == "cn" && tasks_enabled(account))
        .collect();
    if accounts.is_empty() {
        return json_resp(
            StatusCode::OK,
            &json!({
                "tasks": [],
                "summary": {},
                "accounts": [],
                "msg": "未找到参与任务的国内版账号",
            }),
        );
    }

    let requested = query_value(req, "uid");
    let account = accounts
        .iter()
        .find(|account| !requested.is_empty() && requested != "all" && account.uid() == requested)
        .unwrap_or(&accounts[0]);
    let tasks = crate::tasks::growth_tasks(app, account).await;
    let summary = crate::tasks::growth_summary(app, account).await;
    let account_list: Vec<Value> = accounts
        .iter()
        .map(|item| {
            let uid = item.uid();
            json!({
                "uid": uid,
                "nickname": if item.str("nickname").is_empty() {
                    uid.chars().take(8).collect::<String>()
                } else {
                    item.str("nickname").to_string()
                },
            })
        })
        .collect();
    json_resp(
        StatusCode::OK,
        &json!({
            "tasks": tasks,
            "summary": summary,
            "account": crate::panel::account_view(account, crate::state::now_epoch()),
            "accounts": account_list,
        }),
    )
}
