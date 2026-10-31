//! `POST /accounts/sync-profile`: refresh only uid/nickname from the web console.

use std::sync::Arc;

use hyper::body::Incoming;
use hyper::{Request, StatusCode};
use serde_json::{json, Value};

use super::{config, util};
use crate::http::{body_json, json_resp, Resp};
use crate::state::AccountFile;
use crate::App;

pub(super) async fn handle(app: &Arc<App>, req: Request<Incoming>) -> Resp {
    let body = match body_json(req).await {
        Ok(value) => value,
        Err(resp) => return resp,
    };
    let body = util::object_or_empty(body);
    let uid = body.get("uid").and_then(Value::as_str).unwrap_or("").trim();
    let realm = body.get("realm").and_then(Value::as_str).unwrap_or("");
    let targets = util::target_accounts(app, uid, realm, false);
    let mut updated = Vec::new();
    let mut failed = Vec::new();
    for account in targets {
        match sync_one(app, &account).await {
            Ok(nickname) => {
                updated.push(json!({"uid": account.uid(), "nickname": nickname}));
                app.log(
                    "accounts",
                    "INFO",
                    &format!(
                        "account {}: nickname synced from the web console",
                        account.uid().chars().take(8).collect::<String>()
                    ),
                );
            }
            Err(error) => {
                failed.push(json!({
                    "uid": account.uid(),
                    "error": error.chars().take(200).collect::<String>()
                }));
            }
        }
    }
    let now = crate::state::now_epoch();
    let accounts: Vec<Value> = app
        .state
        .accounts_snapshot()
        .iter()
        .map(|account| crate::panel::account_view(account, now))
        .collect();
    json_resp(
        StatusCode::OK,
        &json!({"updated": updated, "failed": failed, "accounts": accounts}),
    )
}

async fn sync_one(app: &Arc<App>, account: &AccountFile) -> Result<String, String> {
    let origin = config::web_origin(&account.realm());
    let response = app
        .upstream
        .get(format!("{origin}/console/account"))
        .header(
            "Authorization",
            format!("Bearer {}", account.access_token()),
        )
        .header("Accept", "application/json, text/plain, */*")
        .header("x-client-platform", "web")
        .header("Origin", config::web_origin("cn"))
        .header(
            "Referer",
            format!("{}/profile/account-settings", config::web_origin("cn")),
        )
        .header("User-Agent", config::web_user_agent())
        .timeout(config::timeout("WB_ACCOUNT_PROFILE_TIMEOUT_SECONDS", 15))
        .send()
        .await
        .map_err(|exc| exc.to_string())?;
    if !response.status().is_success() {
        return Err(format!("HTTP {}", response.status()));
    }
    let payload = response
        .json::<Value>()
        .await
        .map_err(|exc| format!("profile response is not JSON: {exc}"))?;
    let upstream_uid = payload.get("uid").and_then(Value::as_str).unwrap_or("");
    if !upstream_uid.is_empty() && upstream_uid != account.uid() {
        return Err("profile uid mismatch".into());
    }
    let nickname = payload
        .get("nickname")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    if !nickname.is_empty() && nickname != account.str("nickname") {
        let mut data = account.data.clone();
        data.insert("nickname".into(), json!(nickname));
        crate::state::write_json_atomic(&account.path, &Value::Object(data))
            .map_err(|exc| format!("could not save nickname: {exc}"))?;
        app.state.reload();
    }
    Ok(nickname)
}
