use std::convert::Infallible;
use std::fs;
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use http_body_util::{BodyExt, Full};
use hyper::body::Bytes;
use hyper::service::service_fn;
use hyper::{Request, Response, StatusCode};
use hyper_util::rt::TokioIo;
use serde_json::{json, Map, Value};

use super::{device, refresh, LoginSession};
use crate::http::Resp;

static ENV_LOCK: Mutex<()> = Mutex::new(());
const LOGIN_TOKEN: &str =
    "eyJhbGciOiJub25lIn0.eyJzdWIiOiJmaXh0dXJlLWxvZ2luIiwiZXhwIjoyMDAwMDAwMDAwLCJpc3MiOiJodHRwczovL3d3dy53b3JrYnVkZHkuYWkifQ.signature";
const REFRESH_TOKEN: &str =
    "eyJhbGciOiJub25lIn0.eyJzdWIiOiJmaXh0dXJlLWFjY291bnQiLCJleHAiOjIwMDAwMDAwMDB9.signature";

#[derive(Clone, Copy)]
enum FakeKind {
    Device,
    Refresh,
    BrokenStart,
}

struct Fake {
    base: String,
    task: tokio::task::JoinHandle<()>,
}

impl Drop for Fake {
    fn drop(&mut self) {
        self.task.abort();
    }
}

async fn fake_upstream(kind: FakeKind) -> Fake {
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
        .await
        .unwrap();
    let address = listener.local_addr().unwrap();
    let token_calls = Arc::new(AtomicUsize::new(0));
    let task = tokio::spawn(async move {
        loop {
            let Ok((stream, _)) = listener.accept().await else {
                break;
            };
            let token_calls = token_calls.clone();
            tokio::spawn(async move {
                let service = service_fn(move |req| {
                    let token_calls = token_calls.clone();
                    async move { fake_response(kind, token_calls, req).await }
                });
                let _ = hyper::server::conn::http1::Builder::new()
                    .serve_connection(TokioIo::new(stream), service)
                    .await;
            });
        }
    });
    Fake {
        base: format!("http://{address}"),
        task,
    }
}

async fn fake_response(
    kind: FakeKind,
    token_calls: Arc<AtomicUsize>,
    req: Request<hyper::body::Incoming>,
) -> Result<Response<Full<Bytes>>, Infallible> {
    let path = req.uri().path().to_string();
    let _ = req.into_body().collect().await;
    let payload = match (kind, path.as_str()) {
        (FakeKind::BrokenStart, path) if path.ends_with("/auth/state") => {
            json!({"code": 0, "data": {}})
        }
        (_, path) if path.ends_with("/auth/state") => json!({
            "code": 0,
            "data": {
                "state": "state-python-fixture",
                "authUrl": "https://login.example/device"
            }
        }),
        (_, path) if path.ends_with("/auth/token") => {
            let call = token_calls.fetch_add(1, Ordering::SeqCst);
            if call == 0 {
                json!({"code": 11217, "msg": "waiting for browser login"})
            } else {
                json!({
                    "code": 0,
                    "data": {
                        "accessToken": LOGIN_TOKEN,
                        "refreshToken": "rotated-login-refresh",
                        "domain": "www.workbuddy.ai"
                    }
                })
            }
        }
        (_, path) if path.ends_with("/login/account") => {
            json!({"code": 0, "data": {"nickname": "Python Fixture"}})
        }
        (_, path) if path.ends_with("/auth/token/refresh") => json!({
            "code": 0,
            "data": {
                "accessToken": REFRESH_TOKEN,
                "refreshToken": "rotated-refresh-token"
            }
        }),
        _ => json!({"code": 404, "msg": path}),
    };
    let body = serde_json::to_vec(&payload).unwrap();
    Ok(Response::builder()
        .status(StatusCode::OK)
        .header("content-type", "application/json")
        .body(Full::new(Bytes::from(body)))
        .unwrap())
}

fn temp_dir(label: &str) -> PathBuf {
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let path = std::env::temp_dir().join(format!(
        "wb-account-ops-{label}-{}-{stamp}",
        std::process::id()
    ));
    fs::create_dir_all(path.join("accounts")).unwrap();
    fs::create_dir_all(path.join("usage")).unwrap();
    path
}

fn test_app(root: &PathBuf, account: Option<Value>) -> Arc<crate::App> {
    let accounts = root.join("accounts");
    let usage_dir = root.join("usage");
    fs::write(accounts.join("settings.json"), "{}").unwrap();
    if let Some(account) = account {
        let uid = account.get("uid").and_then(Value::as_str).unwrap();
        fs::write(
            accounts.join(format!("{uid}.json")),
            serde_json::to_vec_pretty(&account).unwrap(),
        )
        .unwrap();
    }
    let state = crate::state::State::load(accounts.to_str().unwrap(), usage_dir.to_str().unwrap());
    let updates_dir = state.accounts_dir.to_string_lossy().to_string();
    Arc::new(crate::App {
        state,
        upstream: reqwest::Client::new(),
        started: std::time::Instant::now(),
        listen_port: 0,
        inflight: Default::default(),
        recorder: crate::usage::Recorder::new(usage_dir.clone()),
        pool: crate::pool::Pool::new(),
        panel: crate::panel::Panel::default(),
        accounts_ops: super::AccountsOps::new(),
        pricing: crate::pricing::Pricing::new(&usage_dir),
        scheduler: crate::scheduler::Scheduler::default(),
        updates: Arc::new(crate::updates::Manager::new(&updates_dir)),
        logs: Default::default(),
    })
}

async fn response_json(resp: Resp) -> (StatusCode, Value) {
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (status, serde_json::from_slice(&bytes).unwrap())
}

fn body(value: Value) -> Map<String, Value> {
    value.as_object().unwrap().clone()
}

fn fixture_account(refresh_token: &str) -> Value {
    json!({
        "uid": "fixture-account",
        "nickname": "fixture",
        "domain": "copilot.tencent.com",
        "realm": "cn",
        "platform": "linux",
        "product": "workbuddy",
        "accessToken": "old-access-token",
        "refreshToken": refresh_token,
        "expiresAt": 1,
        "enabled": true,
        "tasksEnabled": false
    })
}

#[tokio::test]
async fn device_flow_matches_python_state_machine_and_persists_account() {
    let _guard = ENV_LOCK.lock().unwrap_or_else(|poison| poison.into_inner());
    let fake = fake_upstream(FakeKind::Device).await;
    std::env::set_var("WB_CN_CHAT_UPSTREAM", &fake.base);
    let root = temp_dir("device");
    let app = test_app(&root, None);
    app.accounts_ops.put_login(
        "state-python-fixture".into(),
        LoginSession {
            created: crate::state::now_epoch() as f64,
            platform: "CLI".into(),
            realm: "cn".into(),
        },
    );

    let (status, pending) =
        response_json(device::start_with_body(&app, &body(json!({"realm": "cn"}))).await).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        pending,
        json!({
            "state": "state-python-fixture",
            "authUrl": "https://login.example/device",
            "realm": "cn",
            "platform": "CLI"
        })
    );
    let session = app.accounts_ops.login("state-python-fixture").unwrap();
    let (status, first_poll) =
        response_json(device::poll_session(&app, "state-python-fixture", &session).await).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        first_poll,
        json!({"status": "pending", "message": "waiting for browser login"})
    );
    assert_cancel(&app);
    let (status, completed) =
        response_json(device::poll_session(&app, "state-python-fixture", &session).await).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(completed["status"], "ok");
    assert_eq!(completed["account"]["uid"], "fixture-login");
    assert_eq!(completed["account"]["nickname"], "Python Fixture");
    assert!(!app.accounts_ops.remove_login("state-python-fixture"));

    assert_saved_login(&root);
    std::env::remove_var("WB_CN_CHAT_UPSTREAM");
    drop(fake);
    let _ = fs::remove_dir_all(root);
}

fn assert_cancel(app: &Arc<crate::App>) {
    app.accounts_ops.put_login(
        "cancel-me".into(),
        LoginSession {
            created: crate::state::now_epoch() as f64,
            platform: "CLI".into(),
            realm: "cn".into(),
        },
    );
    assert!(app.accounts_ops.remove_login("cancel-me"));
}

fn assert_saved_login(root: &PathBuf) {
    let saved: Value =
        serde_json::from_slice(&fs::read(root.join("accounts/fixture-login.json")).unwrap())
            .unwrap();
    assert_eq!(saved["accessToken"], LOGIN_TOKEN);
    assert_eq!(saved["refreshToken"], "rotated-login-refresh");
    assert_eq!(saved["source"], "oauth");
    assert_eq!(saved["enabled"], true);
}

#[tokio::test]
async fn broken_device_start_returns_python_error_code_and_shape() {
    let _guard = ENV_LOCK.lock().unwrap_or_else(|poison| poison.into_inner());
    let fake = fake_upstream(FakeKind::BrokenStart).await;
    std::env::set_var("WB_CN_CHAT_UPSTREAM", &fake.base);
    let root = temp_dir("broken-start");
    let app = test_app(&root, None);
    let body = body(json!({"realm": "cn", "platform": "CLI"}));
    let (status, payload) = response_json(device::start_with_body(&app, &body).await).await;
    assert_eq!(status, StatusCode::BAD_GATEWAY);
    assert_eq!(payload["error"]["type"], "server_error");
    assert_eq!(payload["error"]["code"], 502);
    assert!(payload["error"]["message"]
        .as_str()
        .unwrap()
        .contains("auth/state returned no state/authUrl"));
    std::env::remove_var("WB_CN_CHAT_UPSTREAM");
    drop(fake);
    let _ = fs::remove_dir_all(root);
}

#[tokio::test]
async fn refresh_rotates_and_persists_tokens() {
    let _guard = ENV_LOCK.lock().unwrap_or_else(|poison| poison.into_inner());
    let fake = fake_upstream(FakeKind::Refresh).await;
    std::env::set_var("WB_CN_CHAT_UPSTREAM", &fake.base);
    let root = temp_dir("refresh");
    let app = test_app(&root, Some(fixture_account("old-refresh-token")));
    let account = app.state.accounts_snapshot().remove(0);
    let result = refresh::refresh_one(&app, &account).await;
    assert_eq!(
        result,
        json!({"uid": "fixture-account", "ok": true, "error": ""})
    );
    let saved: Value =
        serde_json::from_slice(&fs::read(root.join("accounts/fixture-account.json")).unwrap())
            .unwrap();
    assert_eq!(saved["accessToken"], REFRESH_TOKEN);
    assert_eq!(saved["refreshToken"], "rotated-refresh-token");
    assert_eq!(saved["expiresAt"], 2_000_000_000);

    let missing_refresh = fixture_account("");
    let path = root.join("accounts/missing-refresh.json");
    fs::write(&path, serde_json::to_vec(&missing_refresh).unwrap()).unwrap();
    app.state.reload();
    let missing = crate::state::AccountFile {
        path,
        name: "missing-refresh.json".into(),
        data: missing_refresh.as_object().unwrap().clone(),
    };
    assert_eq!(
        refresh::refresh_one(&app, &missing).await,
        json!({
            "uid": "fixture-account",
            "ok": false,
            "error": "no refresh token; sign in again"
        })
    );
    std::env::remove_var("WB_CN_CHAT_UPSTREAM");
    drop(fake);
    let _ = fs::remove_dir_all(root);
}
