use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::update_config::Config;
use crate::update_release::{parse_version, pick_latest};
use crate::updates::Manager;

struct TestDir(PathBuf);

impl TestDir {
    fn new(name: &str) -> Self {
        let path = std::env::temp_dir().join(format!(
            "wb-backend-{name}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos()
        ));
        std::fs::create_dir_all(&path).unwrap();
        Self(path)
    }

    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TestDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn local_server(bodies: Vec<Vec<u8>>) -> (u16, std::thread::JoinHandle<()>) {
    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let port = listener.local_addr().unwrap().port();
    let handle = std::thread::spawn(move || {
        for body in bodies {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0u8; 4096];
            let _ = stream.read(&mut request);
            let header = format!(
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            );
            stream.write_all(header.as_bytes()).unwrap();
            stream.write_all(&body).unwrap();
        }
    });
    (port, handle)
}

fn sha256(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

#[test]
fn stable_versions_are_sorted_and_prereleases_are_ignored() {
    assert_eq!(parse_version("v12.3.4"), Some((12, 3, 4)));
    assert_eq!(parse_version("1.2"), None);
    let releases = vec![
        json!({"tag_name":"v9.9.8","draft":false,"prerelease":false,
            "html_url":"https://github.com/o/r/releases/tag/v9.9.8"}),
        json!({"tag_name":"v9.9.10","draft":false,"prerelease":true,
            "html_url":"https://github.com/o/r/releases/tag/v9.9.10"}),
        json!({"tag_name":"v9.9.9","draft":false,"prerelease":false,
            "html_url":"https://github.com/o/r/releases/tag/v9.9.9"}),
    ];
    let picked = pick_latest(&releases, &["github.com".into()]).unwrap();
    assert_eq!(picked.version, "9.9.9");
    assert_eq!(picked.tag, "v9.9.9");
}

#[tokio::test]
async fn check_uses_loopback_and_persists_python_state_keys() {
    let dir = TestDir::new("check");
    let accounts = dir.path().join("accounts");
    std::fs::create_dir_all(&accounts).unwrap();
    let settings = json!({
        "update_check_enabled": true,
        "kept": "unchanged",
        "update_check": {"last_attempt": 12.0, "startup_checks": [1.0]}
    });
    crate::state::write_json_atomic(&accounts.join("settings.json"), &settings).unwrap();
    let release = json!([{
        "tag_name": "v9.9.9",
        "draft": false,
        "prerelease": false,
        "html_url": "https://github.com/o/r/releases/tag/v9.9.9",
        "published_at": "2026-10-31T00:00:00Z"
    }]);
    let (port, server) = local_server(vec![serde_json::to_vec(&release).unwrap()]);
    let mut config = Config::test_config(&accounts);
    config.releases_url = format!("http://127.0.0.1:{port}/releases");
    config.latest_url = format!("http://127.0.0.1:{port}/latest");
    let manager = Manager::from_config(config);

    let status = manager.check(true, false).await;
    server.join().unwrap();

    assert_eq!(status["latest_version"], "9.9.9");
    assert_eq!(status["latest_tag"], "v9.9.9");
    assert_eq!(status["update_available"], true);
    let stored: Value =
        serde_json::from_slice(&std::fs::read(accounts.join("settings.json")).unwrap()).unwrap();
    assert_eq!(stored["kept"], "unchanged");
    assert_eq!(stored["update_check"]["latest_version"], "9.9.9");
    assert!(stored["update_check"]["last_success"].as_f64().unwrap() > 0.0);
    assert_eq!(stored["update_check"]["startup_checks"], json!([1.0]));
}

#[tokio::test]
async fn download_verify_replace_and_rollback_use_only_temp_files() {
    let dir = TestDir::new("apply");
    let new_binary = b"new-rust-binary".to_vec();
    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let port = listener.local_addr().unwrap().port();
    let manifest = serde_json::to_vec(&json!({
        "version": "9.9.9",
        "targets": {
            "x86_64-unknown-linux-musl": {
                "url": format!("http://127.0.0.1:{port}/wb-backend"),
                "sha256": sha256(&new_binary),
            }
        }
    }))
    .unwrap();
    let binary_body = new_binary.clone();
    let server = std::thread::spawn(move || {
        for body in [manifest, binary_body] {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0u8; 4096];
            let _ = stream.read(&mut request);
            let header = format!(
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            );
            stream.write_all(header.as_bytes()).unwrap();
            stream.write_all(&body).unwrap();
        }
    });

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(2))
        .build()
        .unwrap();
    let asset = crate::update_download::resolve_asset(
        &client,
        &format!("http://127.0.0.1:{port}/manifest.json"),
        "x86_64-unknown-linux-musl",
        "9.9.9",
        64 * 1024,
    )
    .await
    .unwrap();
    let current = dir.path().join("wb-backend");
    let staged = dir.path().join("staged");
    let backup = dir.path().join("previous");
    std::fs::write(&current, b"old-rust-binary").unwrap();
    crate::update_download::download_verified(&client, &asset, &staged, 1024 * 1024)
        .await
        .unwrap();
    server.join().unwrap();

    crate::update_apply::replace_with_backup(&current, &staged, &backup).unwrap();
    assert_eq!(std::fs::read(&current).unwrap(), new_binary);
    assert_eq!(std::fs::read(&backup).unwrap(), b"old-rust-binary");
    crate::update_apply::rollback_binary(&current, &backup).unwrap();
    assert_eq!(std::fs::read(&current).unwrap(), b"old-rust-binary");
    assert_eq!(std::fs::read(&backup).unwrap(), b"old-rust-binary");
}

#[tokio::test]
async fn bad_sha_is_rejected_before_the_current_binary_changes() {
    let dir = TestDir::new("bad-sha");
    let binary = b"tampered".to_vec();
    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let port = listener.local_addr().unwrap().port();
    let manifest = serde_json::to_vec(&json!({
        "version": "9.9.9",
        "targets": {
            "x86_64-unknown-linux-musl": {
                "url": format!("http://127.0.0.1:{port}/wb-backend"),
                "sha256": "0".repeat(64),
            }
        }
    }))
    .unwrap();
    let server = std::thread::spawn(move || {
        for body in [manifest, binary] {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0u8; 4096];
            let _ = stream.read(&mut request);
            let header = format!(
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            );
            stream.write_all(header.as_bytes()).unwrap();
            stream.write_all(&body).unwrap();
        }
    });
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(2))
        .build()
        .unwrap();
    let asset = crate::update_download::resolve_asset(
        &client,
        &format!("http://127.0.0.1:{port}/manifest.json"),
        "x86_64-unknown-linux-musl",
        "9.9.9",
        64 * 1024,
    )
    .await
    .unwrap();
    let current = dir.path().join("wb-backend");
    let staged = dir.path().join("staged");
    std::fs::write(&current, b"old-rust-binary").unwrap();
    let error = crate::update_download::download_verified(&client, &asset, &staged, 1024)
        .await
        .unwrap_err();
    server.join().unwrap();
    assert_eq!(error, "SHA-256 verification failed");
    assert_eq!(std::fs::read(&current).unwrap(), b"old-rust-binary");
    assert!(!staged.exists());
}
