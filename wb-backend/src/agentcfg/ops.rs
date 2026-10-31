//! Plan, transactionally write, and restore agent client files.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value};

use clients::{ClientPaths, Context};

use super::{clients, store};

struct FilePlan {
    path: PathBuf,
    content: String,
    existed: bool,
}

fn plan_client(
    home: &Path,
    client_id: &str,
    base_url: &str,
    api_key: &str,
    model: Option<&str>,
    models: &[Value],
    honor_env: bool,
) -> Result<(String, Vec<FilePlan>), String> {
    let label = clients::client_label(client_id).ok_or_else(|| {
        format!(
            "unknown client {client_id:?} (known: {})",
            clients::known_clients().join(", ")
        )
    })?;
    let paths = ClientPaths::resolve_with_env(client_id, home, honor_env)?;
    let base_url = base_url.trim().trim_end_matches('/').to_string();
    let base_url_v1 = if base_url.ends_with("/v1") {
        base_url.clone()
    } else {
        format!("{base_url}/v1")
    };
    let ctx = Context {
        base_url,
        base_url_v1,
        api_key: api_key.to_string(),
        model: model.map(str::to_string),
        models: models.to_vec(),
    };
    let contents: BTreeMap<PathBuf, String> = clients::apply_client(client_id, &paths, &ctx)?
        .into_iter()
        .collect();
    let files = paths
        .target_paths(client_id)
        .into_iter()
        .filter_map(|path| {
            contents.get(&path).map(|content| FilePlan {
                existed: path.exists(),
                path,
                content: content.clone(),
            })
        })
        .collect();
    Ok((label.to_string(), files))
}

pub(super) fn integrate(
    accounts_dir: &Path,
    home: &Path,
    client_id: &str,
    base_url: &str,
    api_key: &str,
    model: Option<&str>,
    models: &[Value],
    honor_env: bool,
) -> Result<Value, String> {
    let (label, files) = plan_client(home, client_id, base_url, api_key, model, models, honor_env)?;
    let mut state = store::load_state(accounts_dir);
    let previous = previous_files(state.get(client_id));
    let staged = stage_files(accounts_dir, client_id, files, &previous)?;
    let entries = write_staged(accounts_dir, client_id, &staged)?;
    let applied_at = crate::state::local_stamp();
    let record = json!({
        "applied_at": applied_at,
        "base_url": base_url.trim().trim_end_matches('/'),
        "model": model,
        "files": entries,
    });
    state.insert(client_id.into(), record);
    store::save_state(accounts_dir, &state)?;
    let record = state.get(client_id).cloned().unwrap_or(Value::Null);
    Ok(json!({
        "client": client_id,
        "label": label,
        "applied_at": record.get("applied_at"),
        "base_url": record.get("base_url"),
        "model": record.get("model"),
        "files": record.get("files"),
    }))
}

fn previous_files(record: Option<&Value>) -> Map<String, Value> {
    let mut out = Map::new();
    if let Some(files) = record
        .and_then(|record| record.get("files"))
        .and_then(Value::as_array)
    {
        for entry in files {
            if let Some(path) = entry.get("path").and_then(Value::as_str) {
                out.insert(path.into(), entry.clone());
            }
        }
    }
    out
}

struct Staged {
    path: PathBuf,
    content: String,
    existed: bool,
    backup: Option<String>,
}

fn stage_files(
    accounts_dir: &Path,
    client_id: &str,
    files: Vec<FilePlan>,
    previous: &Map<String, Value>,
) -> Result<Vec<Staged>, String> {
    let mut staged = Vec::new();
    for mut file in files {
        let prior = previous.get(&file.path.display().to_string());
        if prior.is_some_and(|entry| entry.get("existed").and_then(Value::as_bool) == Some(false))
            && file.existed
        {
            if let Ok(bytes) = store::read_limited(&file.path) {
                if prior
                    .and_then(|entry| entry.get("sha256"))
                    .and_then(Value::as_str)
                    == Some(store::sha256_hex(&bytes).as_str())
                {
                    file.existed = false;
                }
            }
        }
        let backup = if file.existed {
            match prior
                .and_then(|entry| entry.get("backup"))
                .and_then(Value::as_str)
                .filter(|path| !path.is_empty())
            {
                Some(path) => Some(path.to_string()),
                None => Some(store::read_limited(&file.path).and_then(|bytes| {
                    store::backup_file(accounts_dir, client_id, &file.path, &bytes)
                        .map(|path| path.display().to_string())
                })?),
            }
        } else {
            None
        };
        staged.push(Staged {
            path: file.path,
            content: file.content,
            existed: file.existed,
            backup,
        });
    }
    Ok(staged)
}

fn write_staged(
    _accounts_dir: &Path,
    client_id: &str,
    staged: &[Staged],
) -> Result<Vec<Value>, String> {
    let mut written = Vec::new();
    let mut entries = Vec::new();
    for file in staged {
        if let Err(err) = store::atomic_write(&file.path, file.content.as_bytes()) {
            rollback_written(&written);
            return Err(format!("failed to write {client_id} files: {err}"));
        }
        written.push(file);
        entries.push(json!({
            "path": file.path.display().to_string(),
            "existed": file.existed,
            "backup": file.backup,
            "sha256": store::sha256_hex(file.content.as_bytes()),
            "bytes": file.content.len(),
        }));
    }
    Ok(entries)
}

fn rollback_written(written: &[&Staged]) {
    for file in written.iter().rev() {
        let result = if file.existed {
            file.backup
                .as_ref()
                .map(Path::new)
                .map(store::read_limited)
                .transpose()
                .and_then(|bytes| {
                    bytes
                        .map(|bytes| store::atomic_write(&file.path, &bytes))
                        .unwrap_or(Ok(()))
                })
        } else {
            std::fs::remove_file(&file.path)
                .or_else(|err| {
                    if err.kind() == std::io::ErrorKind::NotFound {
                        Ok(())
                    } else {
                        Err(err)
                    }
                })
                .map_err(|err| err.to_string())
        };
        let _ = result;
    }
}

pub(super) fn restore(accounts_dir: &Path, home: &Path, client_id: &str) -> Result<Value, String> {
    let label = clients::client_label(client_id).ok_or_else(|| {
        format!(
            "unknown client {client_id:?} (known: {})",
            clients::known_clients().join(", ")
        )
    })?;
    let mut state = store::load_state(accounts_dir);
    let files = state
        .get(client_id)
        .and_then(|record| record.get("files"))
        .and_then(Value::as_array)
        .filter(|files| !files.is_empty())
        .cloned()
        .ok_or_else(|| format!("{label} has no recorded integration to restore"))?;
    let mut restored = Vec::new();
    for entry in files {
        let Some(path) = entry.get("path").and_then(Value::as_str) else {
            continue;
        };
        let path = PathBuf::from(path);
        if entry.get("existed").and_then(Value::as_bool) == Some(true)
            && entry
                .get("backup")
                .and_then(Value::as_str)
                .is_some_and(|v| !v.is_empty())
        {
            let backup = PathBuf::from(entry["backup"].as_str().unwrap_or(""));
            if !backup.exists() {
                return Err(format!(
                    "backup missing for {}: {}",
                    path.display(),
                    backup.display()
                ));
            }
            store::atomic_write(&path, &store::read_limited(&backup)?)?;
            restored.push(json!({"path": path.display().to_string(), "action": "restored"}));
        } else {
            let action = match std::fs::remove_file(&path) {
                Ok(()) => "deleted",
                Err(err) if err.kind() == std::io::ErrorKind::NotFound => "already-absent",
                Err(err) => return Err(format!("cannot remove {}: {err}", path.display())),
            };
            restored.push(json!({"path": path.display().to_string(), "action": action}));
            store::prune_empty_parents(&path, home);
        }
    }
    state.remove(client_id);
    store::save_state(accounts_dir, &state)?;
    Ok(json!({"client": client_id, "label": label, "restored": restored}))
}

pub(super) fn backup_stamp() -> String {
    let now = crate::state::now_epoch();
    let tm = unsafe { libc::localtime(&now as *const i64) };
    if tm.is_null() {
        return format!("{now:08}-000000");
    }
    let tm = unsafe { *tm };
    format!(
        "{:04}{:02}{:02}-{:02}{:02}{:02}",
        tm.tm_year + 1900,
        tm.tm_mon + 1,
        tm.tm_mday,
        tm.tm_hour,
        tm.tm_min,
        tm.tm_sec
    )
}
