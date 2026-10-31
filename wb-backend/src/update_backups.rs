//! Local retained-backup inventory and rollback entry point.

use std::path::{Path, PathBuf};
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::UNIX_EPOCH;

use serde_json::{json, Value};

use crate::App;

const DEFAULT_MAX_KEEP_BACKUPS: i64 = 3;
const DEFAULT_MAX_VISIBLE_BACKUPS: usize = 8;

struct Entry {
    path: Option<PathBuf>,
    value: Value,
}

fn setting_keep(app: &App) -> i64 {
    let path = app.state.accounts_dir.join("settings.json");
    std::fs::read(path)
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        .and_then(|value| value.get("keep_backups").and_then(Value::as_i64))
        .unwrap_or(1)
        .clamp(0, max_keep_backups())
}

pub(crate) fn max_keep_backups() -> i64 {
    std::env::var("WB_UPDATE_MAX_KEEP_BACKUPS")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(DEFAULT_MAX_KEEP_BACKUPS)
        .max(0)
}

fn max_visible_backups() -> usize {
    std::env::var("WB_UPDATE_MAX_VISIBLE_BACKUPS")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(DEFAULT_MAX_VISIBLE_BACKUPS)
        .max(1)
}

pub(crate) fn valid_version(value: &str) -> bool {
    let parts: Vec<&str> = value.trim().trim_start_matches('v').split('.').collect();
    parts.len() == 3
        && parts
            .iter()
            .all(|part| !part.is_empty() && part.bytes().all(|byte| byte.is_ascii_digit()))
}

fn backup_version(path: &Path) -> String {
    let Some(text) = std::fs::read_to_string(path.join("wb_proxy.py")).ok() else {
        return String::new();
    };
    let marker = "server_version = \"wb-proxy/";
    text.find(marker)
        .and_then(|at| {
            let rest = &text[at + marker.len()..];
            rest.find('"').map(|end| rest[..end].to_string())
        })
        .filter(|value| valid_version(value))
        .unwrap_or_default()
}

fn metadata_version(path: &Path) -> String {
    std::fs::read(path.join("backup.json"))
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        .and_then(|value| {
            value
                .get("version")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .filter(|value| valid_version(value))
        .unwrap_or_default()
}

fn metadata_files(path: &Path) -> (Vec<String>, Vec<String>) {
    let value = std::fs::read(path.join("backup.json"))
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok());
    let list = |name: &str| {
        value
            .as_ref()
            .and_then(|root| root.get(name))
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .filter_map(Value::as_str)
                    .filter(|item| !item.is_empty())
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default()
    };
    (list("saved"), list("absent"))
}

fn metadata_valid(path: &Path) -> bool {
    std::fs::read(path.join("backup.json"))
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        .is_some_and(|value| {
            ["saved", "absent"].iter().all(|name| {
                value
                    .get(*name)
                    .and_then(Value::as_array)
                    .is_some_and(|items| {
                        items
                            .iter()
                            .all(|item| item.as_str().is_some_and(|text| !text.is_empty()))
                    })
            })
        })
}

fn folder_size(path: &Path) -> u64 {
    let Ok(entries) = std::fs::read_dir(path) else {
        return 0;
    };
    entries
        .filter_map(Result::ok)
        .map(|entry| {
            let path = entry.path();
            if path.is_dir() {
                folder_size(&path)
            } else {
                entry.metadata().map(|meta| meta.len()).unwrap_or(0)
            }
        })
        .sum()
}

fn local_inventory(app: &App) -> Vec<Entry> {
    let root = &app.updates.config.backups_dir;
    let Ok(entries) = std::fs::read_dir(root) else {
        return Vec::new();
    };
    let mut out = Vec::new();
    for entry in entries.filter_map(Result::ok) {
        let name = entry.file_name().to_string_lossy().to_string();
        let dir_version = name.trim_start_matches("backup-");
        if !name.starts_with("backup-") || !valid_version(dir_version) || !entry.path().is_dir() {
            continue;
        }
        let path = entry.path();
        let (saved, absent) = metadata_files(&path);
        let missing = saved.iter().find(|relative| {
            path.join(relative)
                .metadata()
                .map(|meta| !meta.is_file() || meta.len() == 0)
                .unwrap_or(true)
        });
        let version = {
            let value = backup_version(&path);
            if value.is_empty() {
                metadata_version(&path)
            } else {
                value
            }
        };
        let mut reason = if metadata_valid(&path) {
            missing
                .map(|relative| format!("备份缺少文件 {relative}"))
                .unwrap_or_default()
        } else {
            "备份缺少可解析的 backup.json".into()
        };
        if reason.is_empty() && !valid_version(&version) {
            reason = "备份里没有可识别的版本".into();
        }
        let created_at = entry
            .metadata()
            .and_then(|meta| meta.modified())
            .ok()
            .and_then(|stamp| stamp.duration_since(UNIX_EPOCH).ok())
            .map(|stamp| crate::state::local_stamp_for(stamp.as_secs() as i64).replace('T', " "))
            .unwrap_or_default();
        let files = saved.len() + absent.len();
        let complete = reason.is_empty();
        out.push(Entry {
            path: Some(path),
            value: json!({
                "version": version,
                "dir_version": dir_version,
                "created_at": created_at,
                "files": files,
                "complete": complete,
                "reason": reason,
                "size_mb": (folder_size(&entry.path()) as f64 / (1024.0 * 1024.0) * 10.0).round() / 10.0,
                "source": "local",
            }),
        });
    }
    out.sort_by(|left, right| {
        right.value["created_at"]
            .as_str()
            .cmp(&left.value["created_at"].as_str())
    });
    out.truncate(max_visible_backups());
    out
}

fn root_inventory(app: &App) -> Vec<Entry> {
    let _ = app;
    let request_dir = std::env::var("WB_UPDATE_REQUEST_DIR")
        .unwrap_or_else(|_| "/run/workbuddy2api-hub-updater".to_string());
    let Ok(bytes) = std::fs::read(Path::new(&request_dir).join("backups.json")) else {
        return Vec::new();
    };
    let Some(items) = serde_json::from_slice::<Value>(&bytes)
        .ok()
        .and_then(|value| value.get("backups").and_then(Value::as_array).cloned())
    else {
        return Vec::new();
    };
    items
        .into_iter()
        .take(max_visible_backups())
        .filter_map(|item| {
            let version = item.get("version").and_then(Value::as_str)?;
            if !valid_version(version) {
                return None;
            }
            Some(Entry {
                path: None,
                value: json!({
                    "version": version,
                    "created_at": item.get("created_at").and_then(Value::as_str).unwrap_or("").chars().take(32).collect::<String>(),
                    "files": item.get("files").and_then(Value::as_i64).unwrap_or(0),
                    "complete": item.get("complete").and_then(Value::as_bool).unwrap_or(false),
                    "reason": item.get("reason").and_then(Value::as_str).unwrap_or("").chars().take(120).collect::<String>(),
                    "size_mb": item.get("size_mb").and_then(Value::as_i64).unwrap_or(0),
                    "source": "systemd",
                }),
            })
        })
        .collect()
}

fn inventory(app: &App) -> (String, Vec<Entry>) {
    let root = root_inventory(app);
    if !root.is_empty() {
        ("systemd".into(), root)
    } else {
        let local = local_inventory(app);
        let source =
            if app.updates.config.install_supported() || app.updates.config.backups_dir.exists() {
                "local"
            } else {
                ""
            };
        (source.into(), local)
    }
}

pub fn view(app: &Arc<App>) -> Value {
    let (source, entries) = inventory(app);
    json!({
        "source": source,
        "backups": entries.into_iter().map(|entry| entry.value).collect::<Vec<_>>(),
        "current_version": crate::VERSION,
        "keep_backups": setting_keep(app),
        "keep_backups_max": max_keep_backups(),
        "rollback_enabled": app.updates.config.install_supported() || crate::update_requests::enabled(),
    })
}

pub fn start_rollback(app: &Arc<App>, version: &str, port: u16) -> Result<Value, String> {
    let normalized = validate_target(version)?;
    let entry = inventory(app)
        .1
        .into_iter()
        .find(|entry| {
            entry.value.get("version").and_then(Value::as_str) == Some(normalized.as_str())
                && entry.value.get("complete").and_then(Value::as_bool) == Some(true)
        })
        .ok_or_else(|| format!("没有可回退到 {normalized} 的完整备份"))?;
    let Some(path) = entry.path else {
        return crate::update_requests::queue_rollback(&normalized, port);
    };
    let (saved, _) = metadata_files(&path);
    let binary_name = app
        .updates
        .config
        .binary_path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("wb-backend");
    let backup = if path.join(binary_name).exists() {
        path.join(binary_name)
    } else if saved.is_empty() {
        return Err(format!("没有可回退到 {normalized} 的完整备份"));
    } else {
        path.join(&saved[0])
    };
    if app.updates.config.install_supported() {
        app.updates.start_rollback(&normalized, &backup, port)
    } else {
        crate::update_requests::queue_rollback(&normalized, port)
    }
}

impl crate::updates::Manager {
    pub(crate) fn start_rollback(
        self: &Arc<Self>,
        version: &str,
        backup: &Path,
        port: u16,
    ) -> Result<Value, String> {
        if !self.config.install_supported() {
            return Err("安装更新服务尚未配置".into());
        }
        if crate::update_state::is_active_installation(&self.installation_status())
            || self.install_reserved.swap(true, Ordering::AcqRel)
        {
            return Err("已有更新任务正在运行".into());
        }
        std::fs::create_dir_all(&self.config.staging_dir)
            .map_err(|error| format!("应用目录不可写: {error}"))?;
        let staged = self.config.staging_dir.join(format!(
            ".{}-rollback-{}-{}",
            self.config
                .binary_path
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("wb-backend"),
            std::process::id(),
            crate::sse::hex_token(8),
        ));
        if let Err(error) = std::fs::copy(backup, &staged) {
            self.install_reserved.store(false, Ordering::Release);
            return Err(format!("无法读取回退备份: {error}"));
        }
        if let Err(error) = crate::update_download::make_executable(&staged) {
            let _ = std::fs::remove_file(&staged);
            self.install_reserved.store(false, Ordering::Release);
            return Err(error);
        }
        crate::update_state::write_installation(&self.config, "queued", version, "回退任务已排队");
        if let Err(error) = crate::update_apply::spawn_helper(crate::update_apply::HelperSpec {
            helper: self.config.helper_path.clone(),
            binary: self.config.binary_path.clone(),
            staged,
            backup: self.config.backup_path.clone(),
            status: self.config.status_path.clone(),
            version: version.to_string(),
            health_url: self.config.health_for(port),
            parent_pid: std::process::id(),
            stop_timeout_seconds: self.config.stop_timeout_seconds,
            health_timeout_seconds: self.config.health_timeout_seconds,
            health_poll_ms: self.config.health_poll_ms,
        }) {
            self.install_reserved.store(false, Ordering::Release);
            crate::update_state::write_installation(&self.config, "failed", version, &error);
            return Err(error);
        }
        let delay = self.config.exit_delay_ms;
        tokio::spawn(async move {
            tokio::time::sleep(std::time::Duration::from_millis(delay)).await;
            std::process::exit(0);
        });
        Ok(json!({"state": "queued", "version": version, "message": "回退任务已排队"}))
    }
}

pub(crate) fn validate_target(version: &str) -> Result<String, String> {
    if !valid_version(version) {
        return Err("回退目标版本无效".into());
    }
    let normalized = version.trim().trim_start_matches('v');
    if normalized == crate::VERSION {
        return Err("回退目标与当前版本相同".into());
    }
    Ok(normalized.to_string())
}
