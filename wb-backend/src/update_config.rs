//! Environment-backed configuration for release checks and binary updates.

use std::path::{Path, PathBuf};

pub(crate) const DEFAULT_TARGET: &str = "x86_64-unknown-linux-musl";
const DEFAULT_REPO: &str = "ardeyouxipianyi/workbuddy2api-hub";
const DEFAULT_RELEASES_URL: &str = "https://api.github.com/repos/{repo}/releases";
const DEFAULT_LATEST_URL: &str = "https://github.com/{repo}/releases/latest";
const DEFAULT_MANIFEST_URL: &str =
    "https://github.com/{repo}/releases/download/{tag}/wb-backend-release-manifest.json";

#[derive(Clone)]
pub(crate) struct Config {
    pub(crate) accounts_dir: PathBuf,
    pub(crate) repository: String,
    pub(crate) releases_url: String,
    pub(crate) latest_url: String,
    pub(crate) manifest_url: String,
    pub(crate) target: String,
    pub(crate) release_hosts: Vec<String>,
    pub(crate) token_env: String,
    pub(crate) user_agent: String,
    pub(crate) interval_seconds: f64,
    pub(crate) poll_seconds: u64,
    pub(crate) first_delay_seconds: u64,
    pub(crate) startup_window_seconds: f64,
    pub(crate) startup_budget: usize,
    pub(crate) request_timeout_seconds: u64,
    pub(crate) max_body_bytes: usize,
    pub(crate) max_download_bytes: u64,
    pub(crate) settings_max_bytes: u64,
    pub(crate) status_max_bytes: u64,
    pub(crate) install_enabled: bool,
    pub(crate) binary_path: PathBuf,
    pub(crate) staging_dir: PathBuf,
    pub(crate) backups_dir: PathBuf,
    pub(crate) backup_path: PathBuf,
    pub(crate) status_path: PathBuf,
    pub(crate) helper_path: PathBuf,
    pub(crate) health_url: String,
    pub(crate) stop_timeout_seconds: u64,
    pub(crate) health_timeout_seconds: u64,
    pub(crate) health_poll_ms: u64,
    pub(crate) exit_delay_ms: u64,
}

impl Config {
    pub(crate) fn from_env(accounts_dir: impl AsRef<Path>) -> Self {
        let binary = env_path("WB_UPDATE_BINARY_PATH").unwrap_or_else(|| {
            std::env::current_exe().unwrap_or_else(|_| PathBuf::from("wb-backend"))
        });
        let sidecar = |suffix: &str| {
            let name = binary
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or("wb-backend");
            binary.with_file_name(format!("{name}.{suffix}"))
        };
        let target = env_string("WB_UPDATE_TARGET", DEFAULT_TARGET);
        let repository = env_string("WB_UPDATE_REPO", DEFAULT_REPO);
        Self {
            accounts_dir: accounts_dir.as_ref().to_path_buf(),
            releases_url: expand(
                &env_string("WB_UPDATE_RELEASES_URL", DEFAULT_RELEASES_URL),
                &repository,
                "",
                "",
                &target,
            ),
            latest_url: env_string("WB_UPDATE_LATEST_URL", DEFAULT_LATEST_URL)
                .replace("{repo}", &repository),
            manifest_url: env_string("WB_UPDATE_MANIFEST_URL", DEFAULT_MANIFEST_URL),
            target,
            release_hosts: csv_env("WB_UPDATE_RELEASE_HOSTS", "github.com"),
            token_env: env_string("WB_UPDATE_TOKEN_ENV", "GITHUB_TOKEN"),
            user_agent: env_string("WB_UPDATE_USER_AGENT", "wb-backend-updater"),
            interval_seconds: env_f64("WB_UPDATE_INTERVAL_SECONDS", 86_400.0).max(1.0),
            poll_seconds: env_u64("WB_UPDATE_POLL_SECONDS", 300).max(1),
            first_delay_seconds: env_u64("WB_UPDATE_FIRST_DELAY_SECONDS", 30),
            startup_window_seconds: env_f64("WB_UPDATE_STARTUP_WINDOW_SECONDS", 3600.0).max(1.0),
            startup_budget: env_u64("WB_UPDATE_STARTUP_BUDGET", 6).clamp(1, 100) as usize,
            request_timeout_seconds: env_u64("WB_UPDATE_REQUEST_TIMEOUT_SECONDS", 10).max(1),
            max_body_bytes: env_u64("WB_UPDATE_MAX_BODY_BYTES", 2 * 1024 * 1024) as usize,
            max_download_bytes: env_u64("WB_UPDATE_MAX_DOWNLOAD_BYTES", 256 * 1024 * 1024),
            settings_max_bytes: env_u64("WB_UPDATE_SETTINGS_MAX_BYTES", 4 * 1024 * 1024),
            status_max_bytes: env_u64("WB_UPDATE_STATUS_MAX_BYTES", 8192),
            install_enabled: env_string("WB_UPDATE_INSTALL_ENABLED", "") == "1",
            staging_dir: env_path("WB_UPDATE_STAGING_DIR")
                .unwrap_or_else(|| binary.parent().unwrap_or(Path::new(".")).to_path_buf()),
            backups_dir: env_path("WB_UPDATE_BACKUPS_DIR").unwrap_or_else(|| {
                binary
                    .parent()
                    .unwrap_or(Path::new("."))
                    .join("updates")
            }),
            backup_path: env_path("WB_UPDATE_BACKUP_PATH").unwrap_or_else(|| sidecar("previous")),
            status_path: env_path("WB_UPDATE_STATUS_PATH")
                .unwrap_or_else(|| sidecar("update-status.json")),
            helper_path: env_path("WB_UPDATE_HELPER_PATH").unwrap_or_else(|| binary.clone()),
            health_url: env_string("WB_UPDATE_HEALTH_URL", "http://127.0.0.1:{port}/health"),
            stop_timeout_seconds: env_u64("WB_UPDATE_STOP_TIMEOUT_SECONDS", 20),
            health_timeout_seconds: env_u64("WB_UPDATE_HEALTH_TIMEOUT_SECONDS", 45),
            health_poll_ms: env_u64("WB_UPDATE_HEALTH_POLL_MS", 500).max(50),
            exit_delay_ms: env_u64("WB_UPDATE_EXIT_DELAY_MS", 750),
            binary_path: binary,
            repository,
        }
    }

    pub(crate) fn install_supported(&self) -> bool {
        self.install_enabled
            && self.target == DEFAULT_TARGET
            && cfg!(all(
                target_os = "linux",
                target_arch = "x86_64",
                target_env = "musl"
            ))
    }

    pub(crate) fn manifest_for(&self, tag: &str, version: &str) -> String {
        expand(
            &self.manifest_url,
            &self.repository,
            tag,
            version,
            &self.target,
        )
    }

    pub(crate) fn health_for(&self, port: u16) -> String {
        self.health_url.replace("{port}", &port.to_string())
    }

    #[cfg(test)]
    pub(crate) fn test_config(accounts_dir: impl AsRef<Path>) -> Self {
        let root = accounts_dir.as_ref().to_path_buf();
        let binary = root.join("wb-backend");
        Self {
            accounts_dir: root.clone(),
            repository: "owner/repo".into(),
            releases_url: "http://127.0.0.1:1/releases".into(),
            latest_url: "http://127.0.0.1:1/latest".into(),
            manifest_url: "http://127.0.0.1:1/{tag}/manifest.json".into(),
            target: DEFAULT_TARGET.into(),
            release_hosts: vec!["github.com".into()],
            token_env: "WB_UPDATE_TEST_TOKEN".into(),
            user_agent: "wb-backend-test".into(),
            interval_seconds: 86_400.0,
            poll_seconds: 3600,
            first_delay_seconds: 3600,
            startup_window_seconds: 3600.0,
            startup_budget: 6,
            request_timeout_seconds: 2,
            max_body_bytes: 64 * 1024,
            max_download_bytes: 1024 * 1024,
            settings_max_bytes: 1024 * 1024,
            status_max_bytes: 8192,
            install_enabled: false,
            staging_dir: root.clone(),
            backups_dir: root.join("updates"),
            backup_path: binary.with_extension("previous"),
            status_path: binary.with_extension("update-status.json"),
            helper_path: binary.clone(),
            binary_path: binary,
            health_url: "http://127.0.0.1:{port}/health".into(),
            stop_timeout_seconds: 2,
            health_timeout_seconds: 2,
            health_poll_ms: 100,
            exit_delay_ms: 100,
        }
    }
}

pub(crate) fn env_string(name: &str, default: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| default.to_string())
}

fn env_path(name: &str) -> Option<PathBuf> {
    std::env::var(name)
        .ok()
        .filter(|s| !s.trim().is_empty())
        .map(PathBuf::from)
}

fn env_u64(name: &str, default: u64) -> u64 {
    std::env::var(name)
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(default)
}

fn env_f64(name: &str, default: f64) -> f64 {
    std::env::var(name)
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(default)
}

fn csv_env(name: &str, default: &str) -> Vec<String> {
    env_string(name, default)
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_ascii_lowercase)
        .collect()
}

fn expand(template: &str, repo: &str, tag: &str, version: &str, target: &str) -> String {
    template
        .replace("{repo}", repo)
        .replace("{tag}", tag)
        .replace("{version}", version)
        .replace("{target}", target)
}
