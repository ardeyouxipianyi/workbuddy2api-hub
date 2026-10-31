use std::sync::Arc;

use serde_json::json;

use crate::update_backups::{
    max_keep_backups, start_rollback, valid_version, validate_target, view,
};
use crate::{update_config::Config, updates::Manager, App};

fn test_app(root: &std::path::Path) -> Arc<App> {
    let accounts = root.join("accounts");
    let usage = root.join("usage");
    std::fs::create_dir_all(&accounts).unwrap();
    std::fs::create_dir_all(&usage).unwrap();
    let mut config = Config::test_config(&accounts);
    config.backups_dir = root.join("updates");
    let state = crate::state::State::load(accounts.to_str().unwrap(), usage.to_str().unwrap());
    Arc::new(App {
        state,
        upstream: reqwest::Client::new(),
        started: std::time::Instant::now(),
        listen_port: 0,
        inflight: Default::default(),
        recorder: crate::usage::Recorder::new(usage.to_string_lossy().to_string()),
        pool: crate::pool::Pool::new(),
        panel: crate::panel::Panel::default(),
        pricing: crate::pricing::Pricing::new(&usage),
        scheduler: crate::scheduler::Scheduler::default(),
        updates: Arc::new(Manager::from_config(config)),
        logs: Default::default(),
        slots: crate::slots::Slots::from_env(0),
        accounts_ops: crate::accounts_ops::AccountsOps::new(),
    })
}

#[test]
fn accepts_only_three_numeric_parts() {
    assert!(valid_version("1.2.3"));
    assert!(valid_version("v1.2.3"));
    assert!(!valid_version("1.2"));
    assert!(!valid_version("1.2.x"));
    assert_eq!(validate_target("1.2"), Err("回退目标版本无效".into()));
}

#[test]
fn settings_keep_is_clamped_like_python() {
    let raw = [(-1, 0), (0, 0), (2, 2), (99, 3)];
    for (input, expected) in raw {
        assert_eq!(input.clamp(0, max_keep_backups()), expected);
    }
}

#[test]
fn backups_view_lists_python_compatible_folder_and_rollback_reaches_manager() {
    let root = std::env::temp_dir().join(format!(
        "wb-backups-{}-{}",
        std::process::id(),
        crate::sse::hex_token(8)
    ));
    std::fs::create_dir_all(&root).unwrap();
    let app = test_app(&root);
    let folder = app.updates.config.backups_dir.join("backup-9.9.9");
    std::fs::create_dir_all(&folder).unwrap();
    std::fs::write(folder.join("wb-backend"), b"previous").unwrap();
    std::fs::write(
        folder.join("wb_proxy.py"),
        "server_version = \"wb-proxy/1.9.2\"\n",
    )
    .unwrap();
    std::fs::write(
        folder.join("backup.json"),
        serde_json::to_vec(&json!({
            "version":"9.9.9","files":["wb-backend"],"saved":["wb-backend"],"absent":[]
        }))
        .unwrap(),
    )
    .unwrap();
    let payload = view(&app);
    assert_eq!(
        payload
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect::<Vec<_>>(),
        [
            "source",
            "backups",
            "current_version",
            "keep_backups",
            "keep_backups_max",
            "rollback_enabled"
        ]
    );
    assert_eq!(payload["backups"][0]["version"], "1.9.2");
    assert_eq!(payload["backups"][0]["complete"], true);
    assert_eq!(
        start_rollback(&app, "1.9.2", 0).unwrap_err(),
        "安装更新服务尚未配置"
    );
    let _ = std::fs::remove_dir_all(root);
}
