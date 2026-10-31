//! Desktop credential scan/import (`wb_accounts.desktop_*`).

mod import;
mod key;
mod scan;

use std::path::Path;
use std::sync::{Arc, Mutex};

use hyper::body::Incoming;
use hyper::{Request, StatusCode};
use serde_json::{json, Value};

use super::util;
use crate::http::{body_json, json_resp, Resp};
use crate::App;

#[derive(Default)]
pub(crate) struct AtRestState {
    inner: Mutex<AtRestKey>,
}

#[derive(Default)]
struct AtRestKey {
    key: Option<Vec<u8>>,
    source: String,
}

impl AtRestState {
    pub(super) fn key_id(&self) -> String {
        self.inner
            .lock()
            .ok()
            .and_then(|state| state.key.as_ref().map(|key| key::derive_key_id(key)))
            .unwrap_or_default()
    }

    fn cached(&self) -> bool {
        self.inner
            .lock()
            .map(|state| state.key.is_some())
            .unwrap_or(false)
    }

    pub(super) fn adopt(&self, key: Vec<u8>) {
        if let Ok(mut state) = self.inner.lock() {
            state.key = Some(key);
            state.source = "manual".into();
        }
    }

    pub(super) fn forget(&self) {
        if let Ok(mut state) = self.inner.lock() {
            state.key = None;
            state.source.clear();
        }
    }

    pub(super) fn status(&self) -> Value {
        let keyblob = std::env::var("WB_ATREST_KEYBLOB").unwrap_or_default();
        let cached = self.cached();
        json!({
            "platform": std::env::consts::OS,
            "huntSupported": false,
            "huntHint": "encrypted desktop credentials are not decrypted by the Rust backend",
            "keyblob": keyblob,
            "keyblobFound": !keyblob.is_empty() && Path::new(&keyblob).is_file(),
            "keyId": self.key_id(),
            "keyCached": cached,
            "keySource": if cached { "manual" } else { "" },
            "error": ""
        })
    }
}

pub(super) async fn handle(app: &Arc<App>, req: Request<Incoming>) -> Resp {
    let body = match body_json(req).await {
        Ok(value) => util::object_or_empty(value),
        Err(resp) => return resp,
    };
    if body.get("recoverKey").and_then(Value::as_bool) == Some(true) {
        return key::recover(
            app,
            body.get("force").and_then(Value::as_bool) == Some(true),
        );
    }
    if let Some(text) = body.get("keyText").and_then(Value::as_str) {
        return key::import(app, text);
    }
    if body.get("forgetKey").and_then(Value::as_bool) == Some(true) {
        app.accounts_ops.at_rest.forget();
        return json_resp(
            StatusCode::OK,
            &json!({"ok": true, "atrest": app.accounts_ops.at_rest.status()}),
        );
    }
    if let Some(path) = body
        .get("path")
        .and_then(Value::as_str)
        .filter(|path| !path.is_empty())
    {
        return import::one_path(
            app,
            Path::new(path),
            body.get("realm").and_then(Value::as_str),
        )
        .await;
    }
    if body.get("all").and_then(Value::as_bool) == Some(true) {
        return import::all(app, body.get("realm").and_then(Value::as_str)).await;
    }
    json_resp(StatusCode::OK, &scan::payload(app))
}
