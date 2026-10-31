//! `/v1` routes with management semantics.

use std::sync::Arc;

use hyper::body::Incoming;
use hyper::{Request, StatusCode};
use serde_json::json;

use crate::http::{json_resp, require_auth, Resp};
use crate::App;

fn panel_required() -> Resp {
    json_resp(
        StatusCode::UNAUTHORIZED,
        &json!({"error": {
            "message": "panel password required",
            "type": "invalid_request_error",
            "code": 401,
        }}),
    )
}

pub fn usage(app: &Arc<App>, req: &Request<Incoming>) -> Resp {
    let supplied = crate::auth::supplied(req.headers(), req.uri().query());
    match crate::auth::check(&app.state, &supplied) {
        Ok(passed) if passed.required => crate::panel_usage::overview(app, req),
        // Python's GET dispatcher applies the panel boundary before the handler
        // when a deployment has no API key configured.
        Ok(_) => panel_required(),
        Err(_) => require_auth(app, req).err().unwrap_or_else(|| {
            json_resp(
                StatusCode::UNAUTHORIZED,
                &json!({"error": {
                    "message": "invalid api key",
                    "type": "invalid_request_error",
                    "code": 401,
                }}),
            )
        }),
    }
}

#[cfg(test)]
mod tests {
    use http_body_util::BodyExt;

    use super::panel_required;

    #[tokio::test]
    async fn panel_error_shape_matches_python() {
        let response = panel_required();
        let body = response.into_body().collect().await.unwrap().to_bytes();
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&body).unwrap(),
            serde_json::json!({"error": {
                "message": "panel password required",
                "type": "invalid_request_error",
                "code": 401,
            }})
        );
    }
}
