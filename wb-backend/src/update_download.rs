//! Release manifest resolution, bounded download, and SHA-256 verification.
//!
//! Manifest contract:
//! `{"version":"1.2.3","targets":{"x86_64-unknown-linux-musl":`
//! `{"url":"https://host/wb-backend","sha256":"<64 hex characters>"}}}`

use std::io::Write;
#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;
use std::path::Path;

use serde::Deserialize;
use sha2::{Digest, Sha256};

use crate::update_release::parse_version;

#[derive(Clone, Debug)]
pub(crate) struct ReleaseAsset {
    pub(crate) url: String,
    pub(crate) sha256: String,
}

#[derive(Deserialize)]
struct Manifest {
    version: String,
    targets: std::collections::BTreeMap<String, ManifestTarget>,
}

#[derive(Deserialize)]
struct ManifestTarget {
    url: String,
    sha256: String,
}

pub(crate) async fn resolve_asset(
    client: &reqwest::Client,
    manifest_url: &str,
    target: &str,
    expected_version: &str,
    max_bytes: usize,
) -> Result<ReleaseAsset, String> {
    validate_update_url(manifest_url)?;
    let response = client
        .get(manifest_url)
        .header(reqwest::header::USER_AGENT, "wb-backend-updater")
        .send()
        .await
        .map_err(network_error)?
        .error_for_status()
        .map_err(network_error)?;
    validate_update_url(response.url().as_str())?;
    if response
        .content_length()
        .map(|length| length > max_bytes as u64)
        .unwrap_or(false)
    {
        return Err("release manifest too large".into());
    }
    let mut response = response;
    let mut body = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(network_error)? {
        if body.len().saturating_add(chunk.len()) > max_bytes {
            return Err("release manifest too large".into());
        }
        body.extend_from_slice(&chunk);
    }
    let manifest: Manifest =
        serde_json::from_slice(&body).map_err(|_| "invalid release manifest".to_string())?;
    if manifest.version != expected_version || parse_version(&manifest.version).is_none() {
        return Err("release manifest version mismatch".into());
    }
    let entry = manifest
        .targets
        .get(target)
        .ok_or_else(|| format!("release manifest has no {target} target"))?;
    validate_update_url(&entry.url)?;
    let sha256 = entry.sha256.trim().to_ascii_lowercase();
    if sha256.len() != 64 || !sha256.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err("release manifest has invalid sha256".into());
    }
    Ok(ReleaseAsset {
        url: entry.url.clone(),
        sha256,
    })
}

pub(crate) async fn download_verified(
    client: &reqwest::Client,
    asset: &ReleaseAsset,
    destination: &Path,
    max_bytes: u64,
) -> Result<(), String> {
    if let Some(parent) = destination.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|error| format!("cannot create staging directory: {error}"))?;
    }
    let mut response = client
        .get(&asset.url)
        .header(reqwest::header::USER_AGENT, "wb-backend-updater")
        .send()
        .await
        .map_err(network_error)?
        .error_for_status()
        .map_err(network_error)?;
    validate_update_url(response.url().as_str())?;
    if response
        .content_length()
        .map(|length| length > max_bytes)
        .unwrap_or(false)
    {
        return Err("download too large".into());
    }
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(destination)
        .map_err(|error| format!("cannot create staged binary: {error}"))?;
    let mut downloaded = 0u64;
    while let Some(chunk) = response.chunk().await.map_err(network_error)? {
        downloaded = downloaded.saturating_add(chunk.len() as u64);
        if downloaded > max_bytes {
            drop(file);
            let _ = std::fs::remove_file(destination);
            return Err("download too large".into());
        }
        if let Err(error) = file.write_all(&chunk) {
            drop(file);
            let _ = std::fs::remove_file(destination);
            return Err(format!("cannot write staged binary: {error}"));
        }
    }
    if let Err(error) = file.sync_all() {
        drop(file);
        let _ = std::fs::remove_file(destination);
        return Err(format!("cannot sync staged binary: {error}"));
    }
    let actual = match sha256_file(destination) {
        Ok(actual) => actual,
        Err(error) => {
            let _ = std::fs::remove_file(destination);
            return Err(error);
        }
    };
    if actual != asset.sha256 {
        let _ = std::fs::remove_file(destination);
        return Err("SHA-256 verification failed".into());
    }
    Ok(())
}

pub(crate) fn make_executable(path: &Path) -> Result<(), String> {
    #[cfg(not(unix))]
    {
        let _ = path;
        return Ok(());
    }
    #[cfg(unix)]
    {
        let mut permissions = std::fs::metadata(path)
            .map_err(|error| format!("cannot inspect staged binary: {error}"))?
            .permissions();
        permissions.set_mode(0o755);
        std::fs::set_permissions(path, permissions)
            .map_err(|error| format!("cannot chmod staged binary: {error}"))
    }
}

pub(crate) fn sha256_file(path: &Path) -> Result<String, String> {
    use std::io::Read;
    let mut file =
        std::fs::File::open(path).map_err(|error| format!("cannot read staged binary: {error}"))?;
    let mut hasher = Sha256::new();
    let mut buffer = [0u8; 64 * 1024];
    loop {
        let count = file
            .read(&mut buffer)
            .map_err(|error| format!("cannot read staged binary: {error}"))?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

fn validate_update_url(value: &str) -> Result<(), String> {
    let url = reqwest::Url::parse(value).map_err(|_| "invalid update URL".to_string())?;
    let loopback = matches!(url.host_str(), Some("127.0.0.1" | "::1" | "localhost"));
    if url.scheme() == "https" || (url.scheme() == "http" && loopback) {
        Ok(())
    } else {
        Err("update URL must use HTTPS (HTTP is allowed only on loopback)".into())
    }
}

fn network_error(error: reqwest::Error) -> String {
    error
        .status()
        .map(|status| format!("HTTP {}", status.as_u16()))
        .unwrap_or_else(|| "network error: request failed".to_string())
}
