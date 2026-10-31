//! 面板「智能体一键配置」的读面：各客户端的安装/配置现状 + 可勾选的模型清单。
//!
//! 配置文件名与 `wb_agents.CLIENTS` 一一对应（含 CLAUDE_CONFIG_DIR / CODEX_HOME
//! 覆盖）。写面（apply/restore，会改本机客户端配置并做备份/回滚）还没移植，
//! Rust 侧如实回 501，不假装配置成功。
//!
//! shortcut: 只有 4 个客户端会把模型清单写进自身配置（见 MODEL_EMBEDDING_CLIENTS），
//! 这里照抄；DSH 的 profile 补丁层没细看，config_paths 只报静态两处。

use serde_json::{json, Value};

use crate::App;

/// (id, label, desc, protocol, 配置文件)
const CLIENTS: &[(&str, &str, &str, &str)] = &[
    ("claude-code", "Claude Code", "Anthropic CLI；把 ANTHROPIC_BASE_URL 指向本网关。", "anthropic"),
    ("codex", "Codex CLI", "OpenAI 兼容 CLI；写 model_provider 与 model。", "openai"),
    ("opencode", "OpenCode", "OpenCode；写 provider 配置。", "openai"),
    ("dsh", "DeepSeek Harness", "DSH；在 profile 补丁层加 wb-proxy provider。", "openai"),
    ("crush", "Crush", "Charm Crush；加一条 wb-proxy provider。", "openai"),
    ("zcode", "ZCode", "ZCode v2；写 provider_config。", "openai"),
];

const MODEL_EMBEDDING_CLIENTS: &[&str] = &["opencode", "dsh", "crush", "zcode"];

fn home() -> std::path::PathBuf {
    std::env::var("HOME").map(std::path::PathBuf::from).unwrap_or_else(|_| ".".into())
}

fn env_path(var: &str, fallback: &str) -> std::path::PathBuf {
    match std::env::var(var) {
        Ok(v) if !v.is_empty() => std::path::PathBuf::from(v),
        _ => home().join(fallback),
    }
}

fn config_paths(id: &str) -> Vec<std::path::PathBuf> {
    let h = home();
    match id {
        "claude-code" => vec![env_path("CLAUDE_CONFIG_DIR", ".claude").join("settings.json")],
        "codex" => {
            let dir = env_path("CODEX_HOME", ".codex");
            vec![dir.join("config.toml"), dir.join("auth.json")]
        }
        "opencode" => vec![h.join(".config/opencode/opencode.json")],
        "dsh" => vec![h.join(".dsh/.credentials.yaml"), h.join(".dsh/settings.yaml")],
        "crush" => vec![h.join(".config/crush/crush.json")],
        "zcode" => vec![h.join(".zcode/v2/provider_config.json")],
        _ => Vec::new(),
    }
}

/// 客户端是否装过：配置文件存在，或 PATH 里有同名命令。
fn installed(id: &str, paths: &[std::path::PathBuf]) -> bool {
    if paths.iter().any(|p| p.exists()) {
        return true;
    }
    let bin = match id {
        "claude-code" => "claude",
        "codex" => "codex",
        "dsh" => "dsh",
        _ => return false,
    };
    std::env::var("PATH")
        .map(|path| path.split(':').any(|dir| !dir.is_empty() && std::path::Path::new(dir).join(bin).exists()))
        .unwrap_or(false)
}

fn integration_state(app: &App) -> Value {
    std::fs::read(app.state.accounts_dir.join("integration-state.json"))
        .ok()
        .and_then(|raw| serde_json::from_slice::<Value>(&raw).ok())
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({}))
}

fn sha256_hex(path: &std::path::Path) -> Option<String> {
    use sha2::{Digest, Sha256};
    let bytes = std::fs::read(path).ok()?;
    Some(Sha256::digest(&bytes).iter().map(|b| format!("{b:02x}")).collect())
}

/// 客户端该填的网关地址：0.0.0.0 绑定时回环才对客户端可用（同 Python 的 base_url_hint）。
fn gateway_hint() -> String {
    let port = std::env::var("PORT").ok().and_then(|p| p.parse::<u16>().ok()).unwrap_or(8788);
    format!("http://127.0.0.1:{port}")
}

/// `GET /agents`
pub fn overview(app: &App) -> Value {
    let state = integration_state(app);
    let clients: Vec<Value> = CLIENTS
        .iter()
        .map(|(id, label, desc, protocol)| {
            let paths = config_paths(id);
            let record = state.get(id).filter(|v| v.is_object());
            // 记录里每个文件记着上次写入的 sha256：对不上就是被手工改过。
            let external_change = record
                .and_then(|r| r.get("files"))
                .and_then(Value::as_array)
                .map(|files| {
                    files.iter().any(|entry| {
                        let Some(path) = entry.get("path").and_then(Value::as_str) else {
                            return true;
                        };
                        match (sha256_hex(std::path::Path::new(path)), entry.get("sha256").and_then(Value::as_str)) {
                            (Some(now), Some(want)) => now != want,
                            _ => true,
                        }
                    })
                })
                .unwrap_or(false);
            let applied = record.map(|r| {
                json!({
                    "at": r.get("applied_at").cloned().unwrap_or(Value::Null),
                    "base_url": r.get("base_url").cloned().unwrap_or(Value::Null),
                    "model": r.get("model").cloned().unwrap_or(Value::Null),
                    "files": r.get("files").and_then(Value::as_array).map(|f| f.len()).unwrap_or(0),
                    "external_change": external_change,
                })
            });
            json!({
                "id": id,
                "label": label,
                "desc": desc,
                "protocol": protocol,
                "installed": installed(id, &paths),
                "configured": record.is_some(),
                "config_paths": paths.iter().map(|p| p.display().to_string()).collect::<Vec<_>>(),
                "applied": applied.unwrap_or(Value::Null),
            })
        })
        .collect();

    let settings = app.state.settings_snapshot();
    let selection = settings.get("agent_model_selection").and_then(Value::as_array);
    let keys: Vec<Value> = settings
        .get("api_keys")
        .and_then(Value::as_array)
        .map(|list| {
            list.iter()
                .filter(|k| k.get("enabled").and_then(Value::as_bool).unwrap_or(false))
                .map(|k| {
                    json!({
                        "id": k.get("id").cloned().unwrap_or(Value::Null),
                        "name": k.get("name").and_then(Value::as_str).unwrap_or(""),
                        "enabled": true,
                    })
                })
                .collect()
        })
        .unwrap_or_default();

    json!({
        "enabled": true,
        "clients": clients,
        "models": crate::models::flat_ids(),
        "selected_models": selection.cloned().unwrap_or_default(),
        "models_configured": selection.is_some(),
        "model_embedding_clients": MODEL_EMBEDDING_CLIENTS,
        "keys": keys,
        "global_key_set": !keys.is_empty(),
        "auth_required": settings.get("auth_required").and_then(Value::as_bool).unwrap_or(true),
        "gateway": {"base_url": gateway_hint()},
    })
}

/// `POST /agents/model-selection`：只存勾选清单，不碰任何客户端配置文件。
pub fn save_selection(app: &App, body: &Value) -> Value {
    let list: Vec<String> = body
        .get("models")
        .or_else(|| body.get("selected_models"))
        .and_then(Value::as_array)
        .map(|list| {
            list.iter()
                .filter_map(Value::as_str)
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .collect()
        })
        .unwrap_or_default();
    let mut settings = app.state.settings_snapshot();
    settings.insert("agent_model_selection".into(), json!(list));
    let path = app.state.accounts_dir.join("settings.json");
    match crate::state::write_json_atomic(&path, &Value::Object(settings)) {
        Ok(()) => {
            app.state.reload();
            json!({"ok": true, "configured": true, "selected_models": list})
        }
        Err(e) => json!({"ok": false, "error": format!("cannot save selection: {e}")}),
    }
}
