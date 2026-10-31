//! Download validation, atomic binary switching, and the one-shot update helper.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

pub(crate) struct HelperSpec {
    pub(crate) helper: PathBuf,
    pub(crate) binary: PathBuf,
    pub(crate) staged: PathBuf,
    pub(crate) backup: PathBuf,
    pub(crate) status: PathBuf,
    pub(crate) version: String,
    pub(crate) health_url: String,
    pub(crate) parent_pid: u32,
    pub(crate) stop_timeout_seconds: u64,
    pub(crate) health_timeout_seconds: u64,
    pub(crate) health_poll_ms: u64,
}

pub(crate) fn spawn_helper(spec: HelperSpec) -> Result<(), String> {
    let mut command = Command::new(&spec.helper);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    command
        .arg("--wb-update-apply")
        .arg("--binary")
        .arg(&spec.binary)
        .arg("--staged")
        .arg(&spec.staged)
        .arg("--backup")
        .arg(&spec.backup)
        .arg("--status")
        .arg(&spec.status)
        .arg("--version")
        .arg(&spec.version)
        .arg("--health-url")
        .arg(&spec.health_url)
        .arg("--parent-pid")
        .arg(spec.parent_pid.to_string())
        .arg("--stop-timeout")
        .arg(spec.stop_timeout_seconds.to_string())
        .arg("--health-timeout")
        .arg(spec.health_timeout_seconds.to_string())
        .arg("--health-poll-ms")
        .arg(spec.health_poll_ms.to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map(|_| ())
        .map_err(|error| format!("无法启动更新助手: {error}"))
}

struct ApplyRequest {
    binary: PathBuf,
    staged: PathBuf,
    backup: PathBuf,
    status: PathBuf,
    version: String,
    health_url: String,
    parent_pid: u32,
    stop_timeout: Duration,
    health_timeout: Duration,
    health_poll: Duration,
}

impl ApplyRequest {
    fn parse() -> Result<Self, String> {
        let mut values = std::collections::BTreeMap::new();
        let mut args = std::env::args()
            .skip_while(|arg| arg != "--wb-update-apply")
            .skip(1);
        while let Some(flag) = args.next() {
            let name = flag
                .strip_prefix("--")
                .ok_or_else(|| "invalid helper argument".to_string())?;
            let value = args
                .next()
                .ok_or_else(|| format!("missing value for --{name}"))?;
            values.insert(name.to_string(), value);
        }
        let path = |name: &str| {
            values
                .get(name)
                .cloned()
                .map(PathBuf::from)
                .ok_or_else(|| format!("missing --{name}"))
        };
        let number = |name: &str| {
            values
                .get(name)
                .ok_or_else(|| format!("missing --{name}"))?
                .parse::<u64>()
                .map_err(|_| format!("invalid --{name}"))
        };
        Ok(Self {
            binary: path("binary")?,
            staged: path("staged")?,
            backup: path("backup")?,
            status: path("status")?,
            version: values
                .get("version")
                .cloned()
                .ok_or_else(|| "missing --version".to_string())?,
            health_url: values
                .get("health-url")
                .cloned()
                .ok_or_else(|| "missing --health-url".to_string())?,
            parent_pid: number("parent-pid")? as u32,
            stop_timeout: Duration::from_secs(number("stop-timeout")?),
            health_timeout: Duration::from_secs(number("health-timeout")?),
            health_poll: Duration::from_millis(number("health-poll-ms")?),
        })
    }
}

pub(crate) async fn run_from_args() -> Result<(), String> {
    run(ApplyRequest::parse()?).await
}

async fn run(request: ApplyRequest) -> Result<(), String> {
    write_status(
        &request.status,
        "stopping",
        &request.version,
        "正在停止网关",
    );
    if !wait_for_exit(request.parent_pid, request.stop_timeout) {
        return fail(&request, "网关未在超时内退出");
    }
    write_status(
        &request.status,
        "applying",
        &request.version,
        "正在安装程序",
    );
    if let Err(error) = replace_with_backup(&request.binary, &request.staged, &request.backup) {
        return fail(&request, &error);
    }
    write_status(
        &request.status,
        "restarting",
        &request.version,
        "正在重启网关",
    );
    match wait_health(
        &request.health_url,
        &request.version,
        request.health_timeout,
        request.health_poll,
    )
    .await
    {
        Ok(_) => {
            write_status(
                &request.status,
                "succeeded",
                &request.version,
                &format!("已更新到 {}", request.version),
            );
            Ok(())
        }
        Err(observed_pid) => rollback(&request, observed_pid).await,
    }
}

async fn rollback(request: &ApplyRequest, observed_pid: Option<u32>) -> Result<(), String> {
    write_status(
        &request.status,
        "rolling_back",
        &request.version,
        "新版本未在超时内就绪",
    );
    terminate_observed(observed_pid);
    if let Err(error) = rollback_binary(&request.binary, &request.backup) {
        return fail(request, &error);
    }
    let healthy = wait_health(
        &request.health_url,
        "",
        request.health_timeout,
        request.health_poll,
    )
    .await
    .is_ok();
    let (state, message) = if healthy {
        ("rolled_back", "新版本启动失败，已回滚")
    } else {
        ("failed", "已恢复旧二进制，但网关未重新就绪")
    };
    write_status(&request.status, state, &request.version, message);
    Ok(())
}

fn fail(request: &ApplyRequest, message: &str) -> Result<(), String> {
    write_status(&request.status, "failed", &request.version, message);
    let _ = std::fs::remove_file(&request.staged);
    Err(message.to_string())
}

pub(crate) fn replace_with_backup(
    binary: &Path,
    staged: &Path,
    backup: &Path,
) -> Result<(), String> {
    if let Some(parent) = backup.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|error| format!("cannot create backup directory: {error}"))?;
    }
    snapshot_file(binary, backup)
        .map_err(|error| format!("cannot back up current binary: {error}"))?;
    if let Err(error) = std::fs::rename(staged, binary) {
        return Err(format!("cannot install new binary: {error}"));
    }
    sync_parent(binary);
    Ok(())
}

pub(crate) fn rollback_binary(binary: &Path, backup: &Path) -> Result<(), String> {
    let failed = binary.with_extension("failed");
    let restore = binary.with_extension("rollback");
    let _ = std::fs::remove_file(&failed);
    let _ = std::fs::remove_file(&restore);
    snapshot_file(binary, &failed)
        .map_err(|error| format!("cannot preserve failed binary: {error}"))?;
    if let Err(error) = snapshot_file(backup, &restore) {
        let _ = std::fs::remove_file(&failed);
        return Err(format!("cannot prepare previous binary: {error}"));
    }
    if let Err(error) = std::fs::rename(&restore, binary) {
        let _ = std::fs::remove_file(&failed);
        let _ = std::fs::remove_file(&restore);
        return Err(format!("cannot restore previous binary: {error}"));
    }
    let _ = std::fs::remove_file(failed);
    sync_parent(binary);
    Ok(())
}

fn snapshot_file(source: &Path, destination: &Path) -> std::io::Result<()> {
    match std::fs::remove_file(destination) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    if std::fs::hard_link(source, destination).is_err() {
        std::fs::copy(source, destination)?;
    }
    std::fs::File::open(destination)?.sync_all()
}

fn sync_parent(path: &Path) {
    if let Some(parent) = path.parent() {
        if let Ok(directory) = std::fs::File::open(parent) {
            let _ = directory.sync_all();
        }
    }
}

fn wait_for_exit(pid: u32, timeout: Duration) -> bool {
    #[cfg(unix)]
    {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            if unsafe { libc::kill(pid as libc::pid_t, 0) } != 0 {
                return true;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        false
    }
    #[cfg(not(unix))]
    {
        let _ = (pid, timeout);
        false
    }
}

async fn wait_health(
    url: &str,
    expected: &str,
    timeout: Duration,
    poll: Duration,
) -> Result<Value, Option<u32>> {
    let client = reqwest::Client::builder()
        .timeout(poll.max(Duration::from_millis(500)))
        .build()
        .map_err(|_| None)?;
    let deadline = Instant::now() + timeout;
    let mut observed_pid = None;
    while Instant::now() < deadline {
        if let Ok(response) = client.get(url).send().await {
            if let Ok(value) = response.json::<Value>().await {
                observed_pid = value
                    .get("pid")
                    .and_then(Value::as_u64)
                    .map(|pid| pid as u32)
                    .or(observed_pid);
                let version = value.get("version").and_then(Value::as_str).unwrap_or("");
                if value.get("ok").and_then(Value::as_bool) == Some(true)
                    && (expected.is_empty() || version == expected)
                {
                    return Ok(value);
                }
            }
        }
        tokio::time::sleep(poll).await;
    }
    Err(observed_pid)
}

fn terminate_observed(pid: Option<u32>) {
    #[cfg(unix)]
    if let Some(pid) = pid {
        unsafe {
            libc::kill(pid as libc::pid_t, libc::SIGTERM);
        }
    }
    #[cfg(not(unix))]
    let _ = pid;
}

fn write_status(path: &Path, state: &str, version: &str, message: &str) {
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|time| time.as_secs() as i64)
        .unwrap_or(0);
    let payload = json!({
        "state": state,
        "version": version,
        "message": message.chars().take(240).collect::<String>(),
        "updated_at": crate::state::local_stamp_for(stamp),
    });
    let _ = crate::state::write_json_atomic(path, &payload);
}
