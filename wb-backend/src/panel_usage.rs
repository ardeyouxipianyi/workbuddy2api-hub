//! Panel usage aggregates. The Python log is the single input to every view here.

use std::collections::BTreeMap;
use std::sync::Arc;

use hyper::body::Incoming;
use hyper::{Request, StatusCode};
use serde_json::{json, Value};

use crate::http::{json_resp, Resp};
use crate::App;

const DAY_SECONDS: f64 = 86_400.0;

fn read_rows(app: &App) -> Vec<Value> {
    let Ok(text) = std::fs::read_to_string(app.recorder_path()) else {
        return Vec::new();
    };
    text.lines()
        .filter(|line| !line.trim().is_empty())
        .filter_map(|line| serde_json::from_str(line).ok())
        .collect()
}

fn query<'a>(req: &'a Request<Incoming>, name: &str) -> Option<&'a str> {
    req.uri()
        .query()
        .unwrap_or("")
        .split('&')
        .find_map(|pair| pair.strip_prefix(name)?.strip_prefix('='))
        .filter(|value| !value.is_empty())
}

fn requested_realm(req: &Request<Incoming>, app: &App) -> String {
    crate::http::requested_realm(req, &app.state.current_realm())
}

fn row_realm<'a>(row: &'a Value, account_realms: &'a BTreeMap<String, String>) -> &'a str {
    row.get("realm")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .or_else(|| {
            row.get("account")
                .and_then(Value::as_str)
                .and_then(|uid| account_realms.get(uid).map(String::as_str))
        })
        .unwrap_or("")
}

fn account_realms(app: &App) -> BTreeMap<String, String> {
    app.state
        .accounts_snapshot()
        .into_iter()
        .map(|account| (account.uid(), account.realm()))
        .collect()
}

fn rows_for_realm<'a>(
    rows: &'a [Value],
    realm: &str,
    realms: &'a BTreeMap<String, String>,
) -> Vec<&'a Value> {
    rows.iter()
        .filter(|row| realm == "all" || row_realm(row, realms) == realm)
        .collect()
}

fn number(row: &Value, key: &str) -> f64 {
    row.get(key).and_then(Value::as_f64).unwrap_or(0.0)
}

fn integer(row: &Value, key: &str) -> i64 {
    row.get(key).and_then(Value::as_i64).unwrap_or(0)
}

fn epoch_now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(0.0)
}

fn outcome(row: &Value) -> &str {
    row.get("outcome").and_then(Value::as_str).unwrap_or("")
}

fn representative(app: &App, realm: &str) -> Option<crate::state::AccountFile> {
    let now = crate::state::now_epoch();
    let accounts = app.state.accounts_snapshot();
    accounts
        .iter()
        .find(|account| account.realm() == realm && account.ready(now))
        .or_else(|| accounts.iter().find(|account| account.realm() == realm))
        .cloned()
}

mod analytics;
mod perf;
mod remaining;
mod summary;
mod timeseries;

pub use analytics::analytics;
pub use perf::perf;
pub use remaining::remaining;
pub use summary::{by_account, overview, recent};
pub use timeseries::timeseries;
