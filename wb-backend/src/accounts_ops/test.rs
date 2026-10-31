//! `POST /accounts/test`: one bounded upstream request per selected account.

use std::net::IpAddr;
use std::sync::Arc;
use std::time::Instant;

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
    let uid = body.get("uid").and_then(Value::as_str).unwrap_or("");
    let model = body
        .get("model")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
        .unwrap_or_else(config::default_model);
    if !uid.is_empty() {
        return test_one(app, uid, &model).await;
    }
    test_all(app, &body, model).await
}

async fn test_one(app: &Arc<App>, uid: &str, model: &str) -> Resp {
    let Some(account) = util::find_account(app, uid) else {
        return util::error(StatusCode::NOT_FOUND, "no such account");
    };
    let row = probe_account(app.clone(), account, model.to_string()).await;
    let mut result = json!({
        "ok": row["ok"],
        "uid": row["uid"],
        "model": row["model"],
        "elapsed_ms": row["elapsed_ms"]
    });
    if row["ok"].as_bool() == Some(true) {
        result["reply"] = row["reply"].clone();
    } else {
        result["status"] = if row["status"].is_null() {
            json!(500)
        } else {
            row["status"].clone()
        };
        result["error"] = row["error"].clone();
        if row["network"].as_bool() == Some(true) {
            result["network"] = json!(true);
        }
    }
    json_resp(StatusCode::OK, &result)
}

async fn test_all(app: &Arc<App>, body: &serde_json::Map<String, Value>, model: String) -> Resp {
    let mut realm = body
        .get("realm")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_lowercase();
    if realm != "intl" && realm != "cn" {
        realm = app.state.current_realm();
    }
    let targets = targets_for(app, &realm);
    let started = Instant::now();
    let network = probe_network(&realm).await;
    let rows = probe_all(app, targets, &model).await;
    let passed = rows
        .iter()
        .filter(|row| row.get("ok").and_then(Value::as_bool) == Some(true))
        .count();
    let failed = rows.len().saturating_sub(passed);
    let summary = summary(&realm, &model, rows.len(), passed, &network, started);
    app.log(
        "accounts",
        "INFO",
        &format!(
            "test all: realm={realm} model={model} tested={} passed={passed} failed={failed}",
            rows.len()
        ),
    );
    let now = crate::state::now_epoch();
    let accounts: Vec<Value> = app
        .state
        .accounts_snapshot()
        .iter()
        .map(|account| crate::panel::account_view(account, now))
        .collect();
    json_resp(
        StatusCode::OK,
        &json!({
            "ok": true,
            "summary": summary,
            "network": network,
            "results": rows,
            "accounts": accounts
        }),
    )
}

fn targets_for(app: &Arc<App>, realm: &str) -> Vec<AccountFile> {
    app.state
        .accounts_snapshot()
        .into_iter()
        .filter(|account| account.realm() == realm && account.enabled())
        .collect()
}

fn summary(
    realm: &str,
    model: &str,
    tested: usize,
    passed: usize,
    network: &Value,
    started: Instant,
) -> Value {
    json!({
        "realm": realm,
        "model": model,
        "tested": tested,
        "passed": passed,
        "failed": tested.saturating_sub(passed),
        "networkDown": network.get("ok").and_then(Value::as_bool) != Some(true),
        "elapsed_ms": started.elapsed().as_millis() as u64
    })
}

async fn probe_all(app: &Arc<App>, targets: Vec<AccountFile>, model: &str) -> Vec<Value> {
    let concurrency = std::env::var("WB_ACCOUNT_TEST_CONCURRENCY")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(4)
        .clamp(1, 32);
    let mut rows = Vec::with_capacity(targets.len());
    for chunk in targets.chunks(concurrency) {
        let mut handles = Vec::with_capacity(chunk.len());
        for account in chunk {
            let app = app.clone();
            let account = account.clone();
            let model = model.to_string();
            handles.push(tokio::spawn(async move {
                probe_account(app, account, model).await
            }));
        }
        for handle in handles {
            if let Ok(row) = handle.await {
                rows.push(row);
            }
        }
    }
    rows
}

async fn probe_account(app: Arc<App>, account: AccountFile, model: String) -> Value {
    let started = Instant::now();
    let (status, text) = match send_probe(&app, &account, &model).await {
        Ok(reply) => reply,
        Err(exc) => {
            return probe_row(
                &account,
                &model,
                started,
                false,
                None,
                "",
                &format!("本机网络不可用：{exc}"),
                true,
            );
        }
    };
    if !(200..300).contains(&status) {
        return probe_row(
            &account,
            &model,
            started,
            false,
            Some(status),
            "",
            &text.chars().take(150).collect::<String>(),
            false,
        );
    }
    app.pool.clear(&account.uid());
    probe_row(
        &account,
        &model,
        started,
        true,
        None,
        &extract_reply(&text),
        "",
        false,
    )
}

async fn send_probe(
    app: &Arc<App>,
    account: &AccountFile,
    model: &str,
) -> Result<(u16, String), reqwest::Error> {
    let body = crate::upstream::build_upstream_body(&json!({
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": false
    }));
    let account_data = Value::Object(account.data.clone());
    let realm = account.realm();
    let product = account.product();
    let uid = account.uid();
    let token = account.access_token();
    let ctx = crate::identity::Ctx {
        realm: &realm,
        product: &product,
        uid: &uid,
        token: &token,
        enterprise_id: crate::identity::enterprise_of(&account_data),
        department: crate::identity::department_of(&account_data),
        session_key: None,
    };
    let url = format!(
        "{}{}",
        crate::upstream::chat_upstream_base(&realm, &product),
        crate::CHAT_PATH
    );
    let mut request = app
        .upstream
        .post(url)
        .json(&body)
        .timeout(config::timeout("WB_ACCOUNT_TEST_TIMEOUT_SECONDS", 30));
    for (key, value) in crate::identity::chat_headers(&ctx) {
        request = request.header(key, value);
    }
    let response = request.send().await?;
    let status = response.status();
    let text = response.text().await.unwrap_or_default();
    Ok((status.as_u16(), text))
}

fn extract_reply(text: &str) -> String {
    if let Ok(payload) = serde_json::from_str::<Value>(text) {
        if let Some(message) = payload
            .pointer("/choices/0/message")
            .and_then(Value::as_object)
        {
            let reply = message
                .get("content")
                .or_else(|| message.get("reasoning_content"))
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim();
            return truncate_reply(if reply.is_empty() { "OK" } else { reply });
        }
    }
    let mut reply = String::new();
    for line in text.lines() {
        let Some(raw) = line.strip_prefix("data:") else {
            continue;
        };
        let Ok(payload) = serde_json::from_str::<Value>(raw.trim()) else {
            continue;
        };
        if let Some(piece) = payload
            .pointer("/choices/0/delta/content")
            .and_then(Value::as_str)
        {
            reply.push_str(piece);
        }
    }
    truncate_reply(if reply.is_empty() { "OK" } else { &reply })
}

fn truncate_reply(reply: &str) -> String {
    if reply.chars().count() > 80 {
        format!("{}...", reply.chars().take(77).collect::<String>())
    } else {
        reply.to_string()
    }
}

fn probe_row(
    account: &AccountFile,
    model: &str,
    started: Instant,
    ok: bool,
    status: Option<u16>,
    reply: &str,
    error: &str,
    network: bool,
) -> Value {
    json!({
        "uid": account.uid(),
        "nickname": util::nickname(account),
        "realm": account.realm(),
        "model": model,
        "ok": ok,
        "elapsed_ms": started.elapsed().as_millis() as u64,
        "status": status,
        "reply": reply,
        "error": error,
        "network": network
    })
}

async fn probe_network(realm: &str) -> Value {
    let url = config::chat_base(realm);
    let parsed = match reqwest::Url::parse(&url) {
        Ok(parsed) => parsed,
        Err(exc) => return network_error(realm, &url, "", "", &format!("URL 解析失败：{exc}")),
    };
    if parsed.scheme() != "https" {
        return network_error(
            realm,
            &url,
            "",
            "",
            &format!("上游地址必须是 https + 主机名：{url}"),
        );
    }
    let Some(host) = parsed.host_str() else {
        return network_error(realm, &url, "", "", "上游地址缺少主机名");
    };
    let port = parsed.port_or_known_default().unwrap_or(443);
    let dns_started = Instant::now();
    let addresses = match tokio::time::timeout(
        config::timeout("WB_ACCOUNT_TEST_DNS_TIMEOUT_SECONDS", 5),
        tokio::net::lookup_host((host, port)),
    )
    .await
    {
        Ok(Ok(addresses)) => addresses.collect::<Vec<_>>(),
        Ok(Err(exc)) => {
            return network_error(realm, &url, host, "", &format!("DNS 解析失败：{exc}"))
        }
        Err(_) => return network_error(realm, &url, host, "", "DNS 解析超时"),
    };
    let dns_ms = dns_started.elapsed().as_millis() as u64;
    let Some(address) = addresses
        .into_iter()
        .find(|address| public_ip(address.ip()))
    else {
        return network_error(realm, &url, host, "", "没有可用的公网解析结果");
    };
    let tcp_started = Instant::now();
    let connected = tokio::time::timeout(
        config::timeout("WB_ACCOUNT_TEST_TCP_TIMEOUT_SECONDS", 5),
        tokio::net::TcpStream::connect(address),
    )
    .await;
    if !matches!(connected, Ok(Ok(_))) {
        return network_error(realm, &url, host, &address.ip().to_string(), "TCP 连接失败");
    }
    json!({
        "realm": realm,
        "url": url,
        "host": host,
        "ip": address.ip().to_string(),
        "ok": true,
        "dns_ms": dns_ms,
        "tcp_ms": tcp_started.elapsed().as_millis() as u64,
        "error": ""
    })
}

fn network_error(realm: &str, url: &str, host: &str, ip: &str, error: &str) -> Value {
    json!({
        "realm": realm,
        "url": url,
        "host": host,
        "ip": ip,
        "ok": false,
        "dns_ms": Value::Null,
        "tcp_ms": Value::Null,
        "error": error
    })
}

fn public_ip(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(ip) => {
            !(ip.is_private()
                || ip.is_loopback()
                || ip.is_link_local()
                || ip.is_broadcast()
                || ip.is_documentation()
                || ip.is_unspecified()
                || ip.is_multicast())
        }
        IpAddr::V6(ip) => !(ip.is_loopback() || ip.is_unspecified() || ip.is_multicast()),
    }
}
