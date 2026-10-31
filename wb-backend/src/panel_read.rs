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

/// `?realm=` 原样放行：`all` 表示不过滤，未知值返回空表（Python 的 list_public 就这样）；
/// 没有这个参数才走 x-realm / 当前区域。共用的 `http::requested_realm` 只认 cn/intl，
/// 因为 chat/models 那边不该接受 all。
fn list_realm(query: Option<&str>, header: Option<&str>, current: &str) -> String {
    let asked = query
        .unwrap_or("")
        .split('&')
        .find_map(|pair| pair.strip_prefix("realm="))
        .filter(|value| !value.is_empty());
    match asked.or(header.filter(|value| matches!(*value, "cn" | "intl" | "all"))) {
        Some(value) => value.to_string(),
        None => current.to_string(),
    }
}

/// `GET /accounts`: list the selected realm, with `?realm=` taking precedence.
pub fn accounts(app: &Arc<App>, req: &Request<Incoming>) -> Resp {
    let realm = list_realm(
        req.uri().query(),
        req.headers().get("x-realm").and_then(|value| value.to_str().ok()),
        &app.state.current_realm(),
    );
    let now = crate::state::now_epoch();
    // Python 的 usable 是池子全局的（count_ready() 不按 ?realm= 过滤）。
    let usable = crate::pool::count_ready(app, "all", None);
    let list: Vec<Value> = app
        .state
        .accounts_snapshot()
        .iter()
        .filter(|account| realm == "all" || account.realm() == realm)
        .map(|account| crate::panel::account_view(app, account, now))
        .collect();
    json_resp(
        StatusCode::OK,
        &json!({
            "accounts": list,
            "storage": app.state.accounts_dir_name(),
            "usable": usable,
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
            "account": crate::panel::account_view(app, account, crate::state::now_epoch()),
            "accounts": account_list,
        }),
    )
}

#[cfg(test)]
mod tests {
    use super::list_realm;

    #[test]
    fn account_list_realm_keeps_all_but_chat_realm_does_not() {
        // 面板「全部出口」：realm=all 必须原样传下去，否则国际版账号在列表里消失。
        assert_eq!(list_realm(Some("realm=all"), None, "cn"), "all");
        assert_eq!(list_realm(Some("range=today&realm=intl"), None, "cn"), "intl");
        // 没有参数：x-realm 认 cn/intl/all，其余回落到当前区域（与 Python 的
        // query or CURRENT_REALM 同序）；未知 realm 由筛选阶段返回空表。
        assert_eq!(list_realm(None, Some("intl"), "cn"), "intl");
        assert_eq!(list_realm(Some("realm="), Some("intl"), "cn"), "intl");
        assert_eq!(list_realm(None, Some("bogus"), "cn"), "cn");
        assert_eq!(list_realm(None, None, "cn"), "cn");
    }
}
