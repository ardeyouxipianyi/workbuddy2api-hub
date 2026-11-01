//! Panel-managed proxy slots and their discovery/probe endpoints.

mod probe;
pub mod store;

use std::sync::Arc;

use hyper::body::Incoming;
use hyper::{Method, Request, StatusCode};
use serde_json::{json, Map, Value};

use crate::http::{body_json, error_resp, json_resp, Resp};
use crate::App;

pub async fn handle(app: &Arc<App>, req: Request<Incoming>) -> Resp {
    let path = req.uri().path().to_string();
    match (req.method(), path.as_str()) {
        (&Method::GET, "/proxy/slots") | (&Method::POST, "/proxy/slots") => slots_view(app),
        (&Method::POST, "/proxy/slots/save") => save(app, req).await,
        (&Method::POST, "/proxy/slots/test") => test(app, req).await,
        (&Method::GET, "/proxy/discover") | (&Method::POST, "/proxy/discover") => {
            probe::discover(app).await
        }
        _ => error_resp(StatusCode::NOT_FOUND, "not found"),
    }
}

fn slots_view(app: &App) -> Resp {
    let slots = store::slots_from_settings(&app.state.settings_snapshot());
    let accounts = app.state.accounts_snapshot();
    json_resp(
        StatusCode::OK,
        &json!({"slots": store::with_runtime_fields(&slots, &accounts)}),
    )
}

async fn save(app: &Arc<App>, req: Request<Incoming>) -> Resp {
    let payload = match body_json(req).await {
        Ok(value) => value,
        Err(resp) => return resp,
    };
    let Some(raw) = payload.get("slots").and_then(Value::as_array) else {
        return error_resp(StatusCode::BAD_REQUEST, "slots must be a list");
    };
    let saved = match store::store_slots(&app.state.accounts_dir, raw) {
        Ok(saved) => saved,
        Err(error) => return save_error(&error),
    };
    let dropped = store::drop_missing_bindings(app, &saved);
    app.state.refresh();
    if dropped > 0 {
        app.log(
            "proxy",
            "INFO",
            &format!("proxy slots: unbound {dropped} account(s) from removed slots"),
        );
    }
    app.log(
        "proxy",
        "INFO",
        &format!("proxy slots saved: {} slot(s)", saved.len()),
    );
    let accounts = app.state.accounts_snapshot();
    json_resp(
        StatusCode::OK,
        &json!({"slots": store::with_runtime_fields(&saved, &accounts)}),
    )
}

async fn test(app: &Arc<App>, req: Request<Incoming>) -> Resp {
    let payload = match body_json(req).await {
        Ok(value) => value,
        Err(resp) => return resp,
    };
    let slot_id = payload
        .get("id")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    let Some(slot) = store::slots_from_settings(&app.state.settings_snapshot())
        .into_iter()
        .find(|slot| slot.get("id").and_then(Value::as_str) == Some(slot_id))
    else {
        return error_resp(StatusCode::NOT_FOUND, "no such proxy slot");
    };
    let url = slot.get("url").and_then(Value::as_str).unwrap_or("");
    let outcome = probe::probe(app, url).await;
    let mut reply = probe::probe_json(&outcome);
    reply["id"] = json!(slot_id);
    reply["slot"] = Value::Null;
    if outcome.ok {
        let mut fields = Map::new();
        fields.insert("ip".into(), json!(outcome.exit_ip));
        fields.insert("probed_at".into(), json!(crate::state::now_epoch()));
        if outcome.intel_ok {
            for (key, value) in [
                ("country", &outcome.country),
                ("country_code", &outcome.country_code),
                ("ip_type", &outcome.ip_type),
                ("isp", &outcome.isp),
                ("asn", &outcome.asn),
            ] {
                fields.insert(key.into(), json!(value));
            }
        }
        let updated = match store::update_slot(&app.state.accounts_dir, slot_id, &fields) {
            Ok(Some(slot)) => slot,
            Ok(None) => return error_resp(StatusCode::NOT_FOUND, "no such proxy slot"),
            Err(error) => return save_error(&error),
        };
        reply["slot"] = updated.clone();
        reply["name"] = updated.get("name").cloned().unwrap_or(json!(""));
    }
    json_resp(StatusCode::OK, &reply)
}

fn save_error(error: &std::io::Error) -> Resp {
    json_resp(
        StatusCode::INTERNAL_SERVER_ERROR,
        &json!({"error": {
            "message": format!("cannot save settings: {error}"),
            "type": "server_error",
            "code": 500
        }}),
    )
}
