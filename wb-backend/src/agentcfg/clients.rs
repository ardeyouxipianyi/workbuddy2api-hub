//! Per-client config transforms for the clients present in `wb_agents.CLIENTS`.

mod dsh;
mod transform;

use std::path::{Path, PathBuf};

use serde_json::Value;

pub(super) const PROVIDER_ID: &str = "wb-proxy";
pub(super) const PROVIDER_NAME: &str = "WB Proxy";
pub(super) const CREDENTIAL_REF: &str = "WB_PROXY_API_KEY";

pub(super) struct Context {
    pub base_url: String,
    pub base_url_v1: String,
    pub api_key: String,
    pub model: Option<String>,
    pub models: Vec<Value>,
}

pub(super) fn client_label(id: &str) -> Option<&'static str> {
    match id {
        "claude-code" => Some("Claude Code"),
        "codex" => Some("Codex CLI"),
        "opencode" => Some("OpenCode"),
        "dsh" => Some("DeepSeek Harness"),
        "crush" => Some("Crush"),
        "zcode" => Some("ZCode"),
        _ => None,
    }
}

pub(super) fn known_clients() -> &'static [&'static str] {
    &["claude-code", "codex", "dsh", "opencode", "crush", "zcode"]
}

pub(super) fn model_embedding(id: &str) -> bool {
    matches!(id, "opencode" | "dsh" | "crush" | "zcode")
}

pub(super) fn apply_client(
    id: &str,
    paths: &ClientPaths,
    ctx: &Context,
) -> Result<Vec<(PathBuf, String)>, String> {
    let result = match id {
        "claude-code" => transform::apply_claude(&paths.settings, ctx),
        "codex" => transform::apply_codex(&paths.config, &paths.auth, ctx),
        "opencode" => transform::apply_opencode(&paths.config, ctx),
        "dsh" => dsh::apply(&paths.dsh, ctx),
        "crush" => transform::apply_crush(&paths.config, ctx),
        "zcode" => transform::apply_zcode(&paths.config, ctx),
        _ => Err(format!(
            "unknown client {id:?} (known: {})",
            known_clients().join(", ")
        )),
    };
    result.map_err(|err| {
        if err.starts_with("failed to build ") || err.starts_with("unknown client") {
            err
        } else {
            format!("failed to build {id} config: {err}")
        }
    })
}

pub(super) struct ClientPaths {
    settings: PathBuf,
    config: PathBuf,
    auth: PathBuf,
    dsh: dsh::Paths,
}

impl ClientPaths {
    pub(super) fn resolve_with_env(id: &str, home: &Path, honor_env: bool) -> Result<Self, String> {
        let unused = home.join("__wb_agent_unused__");
        let mut paths = Self {
            settings: unused.clone(),
            config: unused.clone(),
            auth: unused.clone(),
            dsh: dsh::Paths {
                credentials: unused.clone(),
                settings: unused,
                patches: Vec::new(),
            },
        };
        match id {
            "claude-code" => {
                let dir = if honor_env {
                    std::env::var("CLAUDE_CONFIG_DIR")
                        .ok()
                        .filter(|v| !v.is_empty())
                        .map(PathBuf::from)
                } else {
                    None
                }
                .unwrap_or_else(|| home.join(".claude"));
                paths.settings = dir.join("settings.json");
            }
            "codex" => {
                let dir = if honor_env {
                    codex_home(home)
                } else {
                    home.join(".codex")
                };
                paths.config = dir.join("config.toml");
                paths.auth = dir.join("auth.json");
            }
            "opencode" => paths.config = home.join(".config/opencode/opencode.json"),
            "dsh" => paths.dsh = dsh::Paths::resolve(home),
            "crush" => paths.config = home.join(".config/crush/crush.json"),
            "zcode" => paths.config = home.join(".zcode/v2/provider_config.json"),
            _ => return Err(apply_client(id, &paths, &empty_context(home)).unwrap_err()),
        }
        Ok(paths)
    }

    pub(super) fn target_paths(&self, id: &str) -> Vec<PathBuf> {
        match id {
            "claude-code" => vec![self.settings.clone()],
            "codex" => vec![self.config.clone(), self.auth.clone()],
            "dsh" => self.dsh.target_paths(),
            "opencode" | "crush" | "zcode" => vec![self.config.clone()],
            _ => Vec::new(),
        }
    }
}

fn empty_context(_home: &Path) -> Context {
    Context {
        base_url: String::new(),
        base_url_v1: String::new(),
        api_key: String::new(),
        model: None,
        models: Vec::new(),
    }
}

fn codex_home(home: &Path) -> PathBuf {
    std::env::var("CODEX_HOME")
        .ok()
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".codex"))
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn all_six_registered_clients_have_a_label() {
        assert_eq!(known_clients().len(), 6);
        assert!(known_clients().iter().all(|id| client_label(id).is_some()));
    }

    #[test]
    fn opencode_models_snapshot_replaces_old_entries() {
        let mut ctx = empty_context(Path::new("/tmp/home"));
        ctx.base_url_v1 = "http://gateway/v1".into();
        ctx.api_key = "sk".into();
        ctx.models = vec![json!({"id": "new", "context_window": "1000", "max_output": 200})];
        let root = std::env::temp_dir().join(format!("wb-opencode-{}", std::process::id()));
        let path = root.join("opencode.json");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(
            &path,
            "{\"provider\":{\"wb-proxy\":{\"models\":{\"old\":{}},\"keep\":true}}}",
        )
        .unwrap();
        let (_, text) = transform::apply_opencode(&path, &ctx).unwrap().remove(0);
        let value: Value = serde_json::from_str(&text).unwrap();
        let provider = &value["provider"]["wb-proxy"];
        assert!(provider["models"].get("old").is_none());
        assert_eq!(provider["models"]["new"]["limit"]["context"], 1000);
        assert_eq!(provider["keep"], true);
        let _ = std::fs::remove_dir_all(root);
    }
}
