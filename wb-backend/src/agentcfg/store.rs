//! Atomic file writes, integration state, and byte-exact backups.

use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

pub(super) const STATE_FILE: &str = "integration-state.json";
const BACKUP_DIRNAME: &str = "agent-backups";
const WRITE_MODE: u32 = 0o600;

pub(super) fn read_limited(path: &Path) -> Result<Vec<u8>, String> {
    let limit = env_usize("WB_AGENT_CONFIG_MAX_BYTES", 8 * 1024 * 1024);
    match fs::read(path) {
        Ok(bytes) if bytes.len() > limit => Err(format!(
            "refusing to edit {}: file is {} bytes (limit {})",
            path.display(),
            bytes.len(),
            limit
        )),
        Ok(bytes) => Ok(bytes),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(Vec::new()),
        Err(err) => Err(format!("cannot read {}: {err}", path.display())),
    }
}

pub(super) fn read_text(path: &Path) -> Result<String, String> {
    String::from_utf8(read_limited(path)?)
        .map_err(|err| format!("{} is not valid UTF-8: {err}", path.display()))
}

pub(super) fn load_json(path: &Path, what: &str) -> Result<Map<String, Value>, String> {
    let text = read_text(path)?;
    if text.trim().is_empty() {
        return Ok(Map::new());
    }
    let value: Value = serde_json::from_str(&text)
        .map_err(|err| format!("{what} is not valid JSON ({}): {err}", path.display()))?;
    match value {
        Value::Object(map) => Ok(map),
        _ => Err(format!("{what} must be a JSON object: {}", path.display())),
    }
}

pub(super) fn dump_json(value: &Value) -> String {
    format!(
        "{}\n",
        serde_json::to_string_pretty(value).unwrap_or_else(|_| "null".into())
    )
}

pub(super) fn sha256_hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

pub(super) fn atomic_write(path: &Path, data: &[u8]) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|err| format!("cannot create {}: {err}", parent.display()))?;
    }
    let tmp = temp_path(path);
    let _ = fs::remove_file(&tmp);
    let mode = target_mode(path);
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(WRITE_MODE);
    }
    let result = (|| {
        let mut file = options
            .open(&tmp)
            .map_err(|err| format!("cannot create {}: {err}", tmp.display()))?;
        file.write_all(data)
            .and_then(|_| file.sync_all())
            .map_err(|err| format!("cannot write {}: {err}", tmp.display()))?;
        set_mode(&tmp, mode)?;
        fs::rename(&tmp, path).map_err(|err| format!("cannot replace {}: {err}", path.display()))
    })();
    if result.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    result
}

fn temp_path(path: &Path) -> PathBuf {
    let mut name = path.as_os_str().to_os_string();
    name.push(".tmp");
    PathBuf::from(name)
}

fn target_mode(path: &Path) -> u32 {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if let Ok(meta) = fs::metadata(path) {
            let current = meta.permissions().mode() & 0o777;
            if current & 0o077 == 0 && current & 0o400 != 0 {
                return current;
            }
        }
    }
    WRITE_MODE
}

fn set_mode(path: &Path, mode: u32) -> Result<(), String> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(mode))
            .map_err(|err| format!("cannot chmod {}: {err}", path.display()))?;
    }
    let _ = mode;
    Ok(())
}

pub(super) fn load_state(accounts_dir: &Path) -> Map<String, Value> {
    fs::read_to_string(accounts_dir.join(STATE_FILE))
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .and_then(|value| value.as_object().cloned())
        .unwrap_or_default()
}

pub(super) fn save_state(accounts_dir: &Path, state: &Map<String, Value>) -> Result<(), String> {
    fs::create_dir_all(accounts_dir)
        .map_err(|err| format!("cannot create {}: {err}", accounts_dir.display()))?;
    atomic_write(
        &accounts_dir.join(STATE_FILE),
        dump_json(&Value::Object(state.clone())).as_bytes(),
    )
}

pub(super) fn backup_file(
    accounts_dir: &Path,
    client_id: &str,
    path: &Path,
    original: &[u8],
) -> Result<PathBuf, String> {
    let directory = accounts_dir.join(BACKUP_DIRNAME).join(client_id);
    fs::create_dir_all(&directory)
        .map_err(|err| format!("cannot create {}: {err}", directory.display()))?;
    let stamp = crate::agentcfg::ops::backup_stamp();
    let base = path
        .file_name()
        .and_then(|name| name.to_str())
        .filter(|name| !name.is_empty())
        .unwrap_or("config");
    let name = format!("{stamp}-{base}");
    let mut target = directory.join(&name);
    let mut sequence = 1;
    while target.exists() {
        target = directory.join(format!("{name}-{sequence}"));
        sequence += 1;
    }
    atomic_write(&target, original)?;
    prune_backups(&directory);
    Ok(target)
}

pub(super) fn prune_backups(directory: &Path) {
    let keep = env_usize("WB_AGENT_BACKUP_KEEP", 10);
    let Ok(entries) = fs::read_dir(directory) else {
        return;
    };
    let mut names: Vec<String> = entries
        .filter_map(Result::ok)
        .filter_map(|entry| entry.file_name().into_string().ok())
        .collect();
    names.sort();
    let mut by_base: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for name in names {
        let base = backup_base(&name).unwrap_or(&name).to_string();
        by_base.entry(base).or_default().push(name);
    }
    for names in by_base.into_values() {
        let stale = names.len().saturating_sub(keep);
        for name in names.into_iter().take(stale) {
            let _ = fs::remove_file(directory.join(name));
        }
    }
}

fn backup_base(name: &str) -> Option<&str> {
    let bytes = name.as_bytes();
    if bytes.len() < 16
        || !bytes[..8].iter().all(u8::is_ascii_digit)
        || bytes[8] != b'-'
        || !bytes[9..15].iter().all(u8::is_ascii_digit)
        || bytes[15] != b'-'
    {
        return None;
    }
    Some(&name[16..])
}

pub(super) fn prune_empty_parents(path: &Path, home: &Path) {
    let Some(mut directory) = path.parent().map(Path::to_path_buf) else {
        return;
    };
    loop {
        if directory == home || !directory.starts_with(home) || fs::remove_dir(&directory).is_err()
        {
            return;
        }
        let Some(parent) = directory.parent().map(Path::to_path_buf) else {
            return;
        };
        directory = parent;
    }
}

fn env_usize(name: &str, default: usize) -> usize {
    std::env::var(name)
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(default)
}

#[cfg(test)]
mod tests {
    use std::io::Write;

    use super::*;

    #[test]
    fn backup_restores_original_bytes_exactly() {
        let root = std::env::temp_dir().join(format!("wb-agent-store-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let file = root.join("home/.codex/config.toml");
        let accounts = root.join("accounts");
        fs::create_dir_all(file.parent().unwrap()).unwrap();
        let original = b"\x00raw\tbytes\r\n\xff";
        fs::write(&file, original).unwrap();
        let backup = backup_file(&accounts, "codex", &file, original).unwrap();
        fs::write(&file, b"changed").unwrap();
        atomic_write(&file, &fs::read(&backup).unwrap()).unwrap();
        assert_eq!(fs::read(&file).unwrap(), original);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn state_round_trips_json_object() {
        let root = std::env::temp_dir().join(format!("wb-agent-state-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let mut state = Map::new();
        state.insert("codex".into(), Value::String("ok".into()));
        save_state(&root, &state).unwrap();
        assert_eq!(load_state(&root), state);
        let mut file = OpenOptions::new()
            .append(true)
            .open(root.join(STATE_FILE))
            .unwrap();
        file.write_all(b"broken").unwrap();
        assert!(load_state(&root).is_empty());
        let _ = fs::remove_dir_all(root);
    }
}
