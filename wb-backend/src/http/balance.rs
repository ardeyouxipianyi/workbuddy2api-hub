//! `GET /v1/balance-summary`: aggregate cached credits without upstream traffic.

use hyper::body::Incoming;
use hyper::{Request, StatusCode};
use serde_json::{json, Map, Value};

use crate::http::protocol::Identity;
use crate::http::{json_resp, Resp};
use crate::App;

fn number(value: Option<&Value>) -> Option<f64> {
    let parsed = match value? {
        Value::Number(number) => number.as_f64()?,
        Value::String(text) => text.trim().parse::<f64>().ok()?,
        _ => return None,
    };
    parsed.is_finite().then_some(parsed)
}

pub fn summary(app: &App, req: &Request<Incoming>, identity: &Identity) -> Resp {
    let realm = crate::http::protocol::requested_realm(app, req, identity);
    let accounts = app.state.accounts();
    let enabled: Vec<_> = accounts
        .iter()
        .filter(|account| account.enabled())
        .filter(|account| realm == "all" || account.realm() == realm)
        .collect();

    let mut total = 0.0;
    let mut known = 0usize;
    let mut updated: Option<f64> = None;
    for account in &enabled {
        let Some(credits) = account.data.get("credits").and_then(Value::as_object) else {
            continue;
        };
        let Some(remain) = number(credits.get("remain")).filter(|v| *v >= 0.0) else {
            continue;
        };
        total += remain;
        known += 1;
        if let Some(stamp) = number(credits.get("updated_at")) {
            updated = Some(updated.map_or(stamp, |old| old.max(stamp)));
        }
    }

    let mut payload = Map::new();
    payload.insert(
        "balance".into(),
        if known == 0 {
            Value::Null
        } else {
            json!(((total * 100.0).round() / 100.0))
        },
    );
    payload.insert("unit".into(), json!("credit"));
    payload.insert("realm".into(), json!(realm));
    payload.insert("accounts_enabled".into(), json!(enabled.len()));
    payload.insert("accounts_with_credit_data".into(), json!(known));
    payload.insert(
        "updated_at".into(),
        updated.map_or(Value::Null, |v| json!(v)),
    );
    json_resp(StatusCode::OK, &Value::Object(payload))
}
