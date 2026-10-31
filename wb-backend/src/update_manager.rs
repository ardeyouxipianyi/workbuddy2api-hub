//! In-memory check/install state machine backed by narrow settings bookkeeping.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

use crate::update_config::Config;
use crate::update_release::{fetch_releases, is_newer, parse_version, pick_latest, Release};
use crate::update_state::{self as stored, StoredCheck};
use crate::VERSION;

#[derive(Default)]
struct Runtime {
    latest: Option<Release>,
    last_attempt: f64,
    last_success: f64,
    last_error: String,
    startup_checks: Vec<f64>,
}

impl Runtime {
    fn from_stored(stored: StoredCheck) -> Self {
        let latest = parse_version(&stored.latest_version).map(|_| Release {
            version: stored.latest_version,
            tag: String::new(),
            url: String::new(),
            published_at: String::new(),
        });
        Self {
            latest,
            last_attempt: stored.last_attempt,
            last_success: stored.last_success,
            last_error: String::new(),
            startup_checks: stored.startup_checks,
        }
    }
}

pub struct Manager {
    pub(crate) config: Config,
    client: reqwest::Client,
    runtime: Mutex<Runtime>,
    checking: AtomicBool,
    pub(crate) install_reserved: AtomicBool,
}

impl Manager {
    pub fn new(accounts_dir: &str) -> Self {
        Self::from_config(Config::from_env(accounts_dir))
    }

    pub(crate) fn from_config(config: Config) -> Self {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(config.request_timeout_seconds))
            .build()
            .expect("build update client");
        let runtime = Runtime::from_stored(stored::load_check(&config));
        Self {
            config,
            client,
            runtime: Mutex::new(runtime),
            checking: AtomicBool::new(false),
            install_reserved: AtomicBool::new(false),
        }
    }

    pub async fn check(&self, manual: bool, startup: bool) -> Value {
        if !manual {
            if !stored::enabled(&self.config) {
                return skipped(self.status(), "disabled");
            }
            if startup && !self.startup_due() {
                return skipped(self.status(), "startup budget spent");
            }
            if !startup && !self.due() {
                return skipped(self.status(), "not due yet");
            }
            if startup {
                self.note_startup_check();
            }
        }
        if self.checking.swap(true, Ordering::AcqRel) {
            return skipped(self.status(), "already running");
        }
        let now = unix_now();
        self.begin_attempt(now);
        match fetch_releases(&self.config, &self.client).await {
            Ok(document) => self.finish_success(&document, now),
            Err(error) => self.finish_failure(error),
        }
        self.checking.store(false, Ordering::Release);
        self.status()
    }

    fn begin_attempt(&self, now: f64) {
        let mut runtime = self.lock_runtime();
        runtime.last_attempt = now;
        runtime.last_error.clear();
        drop(runtime);
        stored::persist_check(&self.config, false, "", now);
    }

    fn finish_success(&self, document: &[Value], now: f64) {
        let picked = pick_latest(document, &self.config.release_hosts);
        let version = picked
            .as_ref()
            .map(|release| release.version.clone())
            .unwrap_or_default();
        let mut runtime = self.lock_runtime();
        runtime.latest = picked;
        runtime.last_success = now;
        drop(runtime);
        stored::persist_check(&self.config, true, &version, now);
    }

    fn finish_failure(&self, error: String) {
        self.lock_runtime().last_error = error;
    }

    fn lock_runtime(&self) -> std::sync::MutexGuard<'_, Runtime> {
        self.runtime
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    pub fn status(&self) -> Value {
        let runtime = self.lock_runtime();
        let latest = runtime.latest.as_ref();
        let version = latest.map(|release| release.version.clone());
        json!({
            "current_version": VERSION,
            "latest_version": version,
            "latest_tag": latest.map(|release| release.tag.clone()).unwrap_or_default(),
            "update_available": version.as_deref().map(|value| is_newer(value, VERSION)).unwrap_or(false),
            "enabled": stored::enabled(&self.config),
            "checking": self.checking.load(Ordering::Relaxed),
            "last_attempt": timestamp(runtime.last_attempt),
            "last_success": timestamp(runtime.last_success),
            "last_error": runtime.last_error,
            "release_url": latest.map(|release| release.url.clone()).unwrap_or_default(),
            "published_at": latest.map(|release| release.published_at.clone()).unwrap_or_default(),
        })
    }

    pub fn panel_status(&self) -> Value {
        let mut payload = self.status();
        payload["install_enabled"] = json!(self.config.install_supported());
        payload["installation"] = self.installation_status();
        payload
    }

    pub(crate) fn installation_status(&self) -> Value {
        stored::read_installation(&self.config.status_path, self.config.status_max_bytes)
    }

    fn due(&self) -> bool {
        let last = self.lock_runtime().last_attempt;
        last <= 0.0 || unix_now() - last >= self.config.interval_seconds
    }

    fn startup_due(&self) -> bool {
        let cutoff = unix_now() - self.config.startup_window_seconds;
        let runtime = self.lock_runtime();
        runtime
            .startup_checks
            .iter()
            .filter(|stamp| **stamp > cutoff)
            .count()
            < self.config.startup_budget
    }

    fn note_startup_check(&self) {
        let now = unix_now();
        let mut runtime = self.lock_runtime();
        runtime.startup_checks.push(now);
        runtime.startup_checks.sort_by(f64::total_cmp);
        if runtime.startup_checks.len() > 12 {
            let excess = runtime.startup_checks.len() - 12;
            runtime.startup_checks.drain(..excess);
        }
        drop(runtime);
        stored::persist_startup(&self.config, now);
    }

    fn preflight_install(&self) -> Result<String, String> {
        if !self.config.install_supported() {
            return Err("安装更新服务尚未配置".into());
        }
        let status = self.status();
        if !status
            .get("last_error")
            .and_then(Value::as_str)
            .unwrap_or("")
            .is_empty()
        {
            return Err("请先成功检查更新".into());
        }
        if !status
            .get("update_available")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            return Err("当前没有可安装的新版本".into());
        }
        let tag = status
            .get("latest_tag")
            .and_then(Value::as_str)
            .unwrap_or("");
        let url = status
            .get("release_url")
            .and_then(Value::as_str)
            .unwrap_or("");
        if tag.is_empty() || url.is_empty() {
            return Err("请先点击立即检查更新".into());
        }
        Ok(tag.to_string())
    }

    pub(crate) fn start_install(self: &Arc<Self>, port: u16) -> Result<Value, String> {
        let tag = self.preflight_install()?;
        let version = parse_version(&tag)
            .map(|value| format!("{}.{}.{}", value.0, value.1, value.2))
            .ok_or_else(|| "目标版本无效或不高于当前版本".to_string())?;
        if stored::is_active_installation(&self.installation_status())
            || self.install_reserved.swap(true, Ordering::AcqRel)
        {
            return Err("已有更新任务正在运行".into());
        }
        stored::write_installation(&self.config, "queued", &version, "更新任务已排队");
        let manager = self.clone();
        let task_version = version.clone();
        tokio::spawn(async move {
            if let Err(error) = manager.run_install(port, &tag, &task_version).await {
                stored::write_installation(&manager.config, "failed", &task_version, &error);
                manager.install_reserved.store(false, Ordering::Release);
            }
        });
        Ok(json!({"state": "queued", "version": version, "message": "更新任务已排队"}))
    }

    async fn run_install(&self, port: u16, tag: &str, version: &str) -> Result<(), String> {
        stored::write_installation(
            &self.config,
            "downloading",
            version,
            &format!("正在下载 {tag}"),
        );
        std::fs::create_dir_all(&self.config.staging_dir)
            .map_err(|error| format!("应用目录不可写: {error}"))?;
        let asset = crate::update_download::resolve_asset(
            &self.client,
            &self.config.manifest_for(tag, version),
            &self.config.target,
            version,
            self.config.max_body_bytes,
        )
        .await?;
        stored::write_installation(&self.config, "verifying", version, "正在校验更新包");
        let staged = self.config.staging_dir.join(format!(
            ".{}-update-{}-{}",
            self.config
                .binary_path
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("wb-backend"),
            std::process::id(),
            unix_nanos(),
        ));
        crate::update_download::download_verified(
            &self.client,
            &asset,
            &staged,
            self.config.max_download_bytes,
        )
        .await?;
        crate::update_download::make_executable(&staged)?;
        stored::write_installation(&self.config, "stopping", version, "正在停止网关");
        let backup_dir = self
            .config
            .backups_dir
            .join(format!("backup-{version}"));
        let binary_name = self
            .config
            .binary_path
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("wb-backend");
        std::fs::create_dir_all(&backup_dir)
            .map_err(|error| format!("无法创建回退备份目录: {error}"))?;
        crate::state::write_json_atomic(
            &backup_dir.join("backup.json"),
            &json!({
                "version": VERSION,
                "files": [binary_name],
                "saved": [binary_name],
                "absent": [],
            }),
        )
        .map_err(|error| format!("无法写入回退备份清单: {error}"))?;
        let backup = backup_dir.join(binary_name);
        crate::update_apply::spawn_helper(crate::update_apply::HelperSpec {
            helper: self.config.helper_path.clone(),
            binary: self.config.binary_path.clone(),
            staged,
            backup,
            status: self.config.status_path.clone(),
            version: version.to_string(),
            health_url: self.config.health_for(port),
            parent_pid: std::process::id(),
            stop_timeout_seconds: self.config.stop_timeout_seconds,
            health_timeout_seconds: self.config.health_timeout_seconds,
            health_poll_ms: self.config.health_poll_ms,
        })?;
        let delay = self.config.exit_delay_ms;
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(delay)).await;
            std::process::exit(0);
        });
        Ok(())
    }

}

fn unix_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|time| time.as_secs_f64())
        .unwrap_or(0.0)
}

fn unix_nanos() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|time| time.as_nanos())
        .unwrap_or(0)
}

fn timestamp(value: f64) -> Option<String> {
    (value > 0.0).then(|| crate::state::local_stamp_for(value as i64))
}

fn skipped(mut status: Value, reason: &str) -> Value {
    status["skipped"] = json!(reason);
    status
}
