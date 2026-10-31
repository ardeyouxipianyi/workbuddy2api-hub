//! Queue rollback work for a deployment-provided update executor.
//!
//! Defaults/examples:
//! - `WB_UPDATE_REQUEST_DIR=/run/workbuddy2api-hub-updater`
//! - `WB_UPDATE_EXECUTOR=external` (or `executor.json` in that directory)
//! - `WB_UPDATE_PATH_UNIT_FILE=/etc/systemd/system/workbuddy2api-hub-updater.path`
//! - `WB_UPDATE_ENABLED_UNIT_LINK=/etc/systemd/system/multi-user.target.wants/workbuddy2api-hub-updater.path`

use std::io::Write;
use std::path::{Path, PathBuf};

use serde_json::{json, Value};

fn env_path(name: &str, default: &str) -> PathBuf {
    std::env::var(name)
        .ok()
        .filter(|value| !value.trim().is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(default))
}

fn request_dir() -> PathBuf {
    env_path("WB_UPDATE_REQUEST_DIR", "/run/workbuddy2api-hub-updater")
}

fn install_flag() -> bool {
    std::env::var("WB_UPDATE_INSTALL_ENABLED").as_deref() == Ok("1")
}

fn systemd_enabled(dir: &Path) -> bool {
    install_flag()
        && dir.is_dir()
        && env_path(
            "WB_UPDATE_PATH_UNIT_FILE",
            "/etc/systemd/system/workbuddy2api-hub-updater.path",
        )
        .exists()
        && env_path(
            "WB_UPDATE_ENABLED_UNIT_LINK",
            "/etc/systemd/system/multi-user.target.wants/workbuddy2api-hub-updater.path",
        )
        .exists()
}

fn external_enabled(dir: &Path) -> bool {
    install_flag()
        && dir.is_dir()
        && (std::env::var("WB_UPDATE_EXECUTOR")
            .map(|value| value.eq_ignore_ascii_case("external"))
            .unwrap_or(false)
            || dir.join("executor.json").exists())
}

pub fn enabled() -> bool {
    let dir = request_dir();
    systemd_enabled(&dir) || external_enabled(&dir)
}

fn active_state(path: &Path) -> bool {
    std::fs::read(path)
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        .is_some_and(|value| crate::update_state::is_active_installation(&value))
}

fn write_request(dir: &Path, payload: &Value) -> Result<(), String> {
    let path = dir.join("request.json");
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = match options.open(&path) {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
            return Err("已有更新任务正在运行".into())
        }
        Err(_) => return Err("无法提交更新任务，请检查更新服务状态".into()),
    };
    let body = serde_json::to_vec(payload).map_err(|_| "提交更新任务失败".to_string())?;
    if file.write_all(&body).and_then(|_| file.sync_all()).is_err() {
        let _ = std::fs::remove_file(path);
        return Err("提交更新任务失败".into());
    }
    Ok(())
}

pub(crate) fn queue_in(dir: &Path, version: &str, port: u16, root: &Path) -> Result<Value, String> {
    if active_state(&dir.join("status.json")) {
        return Err("已有更新任务正在运行".into());
    }
    let payload = json!({
        "version": version,
        "requested_at": crate::state::local_stamp(),
        "pid": std::process::id(),
        "port": port,
        "root": root.display().to_string(),
        "action": "rollback",
        "tag": format!("v{version}"),
    });
    write_request(dir, &payload)?;
    Ok(json!({
        "state": "queued",
        "version": version,
        "message": "回退任务已排队",
    }))
}

pub(crate) fn queue_rollback(version: &str, port: u16) -> Result<Value, String> {
    if !enabled() {
        return Err("安装更新服务尚未配置".into());
    }
    let root = std::env::current_exe()
        .ok()
        .and_then(|path| path.parent().map(Path::to_path_buf))
        .unwrap_or_else(|| PathBuf::from("."));
    queue_in(&request_dir(), version, port, &root)
}

#[cfg(test)]
mod tests {
    use super::queue_in;

    #[test]
    fn queues_one_external_rollback_atomically() {
        let root = std::env::temp_dir().join(format!(
            "wb-update-request-{}-{}",
            std::process::id(),
            crate::sse::hex_token(8)
        ));
        std::fs::create_dir_all(&root).unwrap();
        let queued = queue_in(&root, "1.9.2", 8799, &root).unwrap();
        assert_eq!(queued["state"], "queued");
        let payload: serde_json::Value =
            serde_json::from_slice(&std::fs::read(root.join("request.json")).unwrap()).unwrap();
        assert_eq!(payload["action"], "rollback");
        assert_eq!(payload["tag"], "v1.9.2");
        assert_eq!(
            queue_in(&root, "1.9.2", 8799, &root).unwrap_err(),
            "已有更新任务正在运行"
        );
        let _ = std::fs::remove_dir_all(root);
    }
}
