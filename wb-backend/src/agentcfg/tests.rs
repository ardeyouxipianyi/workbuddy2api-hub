//! Write-surface integration tests. All client paths are under temporary homes.

use std::collections::BTreeMap;
use std::path::PathBuf;

use serde_json::{json, Value};

use super::ops::{integrate, restore};
use super::{clients, store};

fn temp_root(name: &str) -> PathBuf {
    let root = std::env::temp_dir().join(format!(
        "wb-agentcfg-{name}-{}-{:?}",
        std::process::id(),
        std::thread::current().id()
    ));
    let _ = std::fs::remove_dir_all(&root);
    root
}

#[test]
fn all_six_clients_apply_twice_and_restore_in_temp_home() {
    let root = temp_root("six");
    let home = root.join("home");
    let accounts = root.join("accounts");
    std::fs::create_dir_all(&home).unwrap();
    let models = vec![json!({
        "id": "model-x",
        "context_window": 1000,
        "max_output": 200
    })];
    for client in clients::known_clients() {
        let first = integrate(
            &accounts,
            &home,
            client,
            "http://127.0.0.1:8788",
            "sk-test",
            Some("model-x"),
            &models,
            false,
        )
        .unwrap();
        let first_bytes = snapshot(&first);
        let second = integrate(
            &accounts,
            &home,
            client,
            "http://127.0.0.1:8788",
            "sk-test",
            Some("model-x"),
            &models,
            false,
        )
        .unwrap();
        assert_eq!(first_bytes, snapshot(&second), "{client}");
        restore(&accounts, &home, client).unwrap();
    }
    let _ = std::fs::remove_dir_all(root);
}

#[test]
fn preexisting_bytes_survive_apply_then_restore() {
    let root = temp_root("bytes");
    let home = root.join("home");
    let accounts = root.join("accounts");
    let config = home.join(".codex/config.toml");
    let auth = home.join(".codex/auth.json");
    std::fs::create_dir_all(config.parent().unwrap()).unwrap();
    let original_config = b"# custom\nmodel = \"keep\"\n\xff";
    std::fs::write(&config, original_config).unwrap();
    std::fs::write(&auth, b"{\"user\": true}\n").unwrap();
    let error = integrate(
        &accounts,
        &home,
        "codex",
        "http://127.0.0.1:8788",
        "sk-test",
        Some("model-x"),
        &[json!({"id": "model-x"})],
        false,
    )
    .unwrap_err();
    assert!(error.contains("not valid UTF-8"));
    std::fs::write(&config, b"# custom\nmodel = \"keep\"\n").unwrap();
    integrate(
        &accounts,
        &home,
        "codex",
        "http://127.0.0.1:8788",
        "sk-test",
        Some("model-x"),
        &[json!({"id": "model-x"})],
        false,
    )
    .unwrap();
    std::fs::write(&config, b"edited after apply\n").unwrap();
    restore(&accounts, &home, "codex").unwrap();
    assert_eq!(
        std::fs::read(config).unwrap(),
        b"# custom\nmodel = \"keep\"\n"
    );
    assert_eq!(std::fs::read(auth).unwrap(), b"{\"user\": true}\n");
    let _ = std::fs::remove_dir_all(root);
}

#[test]
fn client_outputs_match_python_pure_function_fixtures() {
    let root = temp_root("python-parity");
    let home = root.join("home");
    let accounts = root.join("accounts");
    std::fs::create_dir_all(&home).unwrap();
    let models = vec![json!({
        "id": "model-x",
        "context_window": 1000,
        "max_output": 200
    })];
    let expected: BTreeMap<&str, BTreeMap<&str, &str>> = BTreeMap::from([
        (
            "claude-code",
            [(
                ".claude/settings.json",
                "7d78cc2619015a530825f0e933e43237441f212b483af5e38a022d1f9553bb10",
            )]
            .into(),
        ),
        (
            "codex",
            [
                (
                    ".codex/config.toml",
                    "e48e7010791e67c9dc4f3a885a1d362c6fe981bf3f58cd91e8127078b1ab25b1",
                ),
                (
                    ".codex/auth.json",
                    "920f276c69320edaf5682f5d4eae41509cda9de943052a0a559234dc956390db",
                ),
            ]
            .into(),
        ),
        (
            "crush",
            [(
                ".config/crush/crush.json",
                "6bb88a327086c0fd7b291fc52e323dd4e746cab7824d8b6d60a955ffab57a607",
            )]
            .into(),
        ),
        (
            "dsh",
            [
                (
                    ".dsh/.credentials.yaml",
                    "9d66458bd6b34dfdf808186e10585e17a6c7cd840271f8864011647c144a02e8",
                ),
                (
                    ".dsh/settings.yaml",
                    "d213520746a5f97e298e27c7ea2cadf6fd512c3016bc4722181536a55c6754be",
                ),
            ]
            .into(),
        ),
        (
            "opencode",
            [(
                ".config/opencode/opencode.json",
                "85726babd5c3327bce4d83182c6431dd0770ce0c13d4ce778e443d065e5f67bd",
            )]
            .into(),
        ),
        (
            "zcode",
            [(
                ".zcode/v2/provider_config.json",
                "501144bb9857fedf0cbc30c5f988e03fcbe328f7796d499cf12b820b8092176b",
            )]
            .into(),
        ),
    ]);
    for (client, files) in expected {
        integrate(
            &accounts,
            &home,
            client,
            "http://127.0.0.1:8788",
            "sk-test",
            Some("model-x"),
            &models,
            false,
        )
        .unwrap();
        for (relative, sha) in files {
            let bytes = std::fs::read(home.join(relative)).unwrap();
            assert_eq!(store::sha256_hex(&bytes), sha, "{client}: {relative}");
        }
        restore(&accounts, &home, client).unwrap();
    }
    let _ = std::fs::remove_dir_all(root);
}

#[test]
fn dsh_profile_patch_matches_python_pure_function_fixture() {
    let root = temp_root("dsh-profile");
    let home = root.join("home");
    let accounts = root.join("accounts");
    let dsh = home.join(".dsh");
    std::fs::create_dir_all(dsh.join("profiles/desktop")).unwrap();
    std::fs::create_dir_all(dsh.join("profiles/web")).unwrap();
    std::fs::write(dsh.join("profiles/desktop/cordis.patch.yml"), "[]\n").unwrap();
    std::fs::write(
            dsh.join("profiles/web/cordis.patch.yml"),
            "# web\n- id: llm-pi-ai\n  name: @deepseek-ai/dsh-llm-pi-ai\n  config:\n    other: kept\n    providers:\n      qwen:\n        baseURL: http://q\n- id: agent-default-model\n  config:\n    reasoningEffort: high\n",
        )
        .unwrap();
    std::fs::write(
        dsh.join("settings.yaml"),
        "agent-default-model: {\"reasoningEffort\": \"low\", \"keep\": 1}\nother: yes\n",
    )
    .unwrap();
    let models = vec![json!({
        "id": "vision-x",
        "context_window": 1000,
        "max_output": 200,
        "reasoning_efforts": ["low", "high"],
        "supports_vision": true
    })];
    integrate(
        &accounts,
        &home,
        "dsh",
        "http://127.0.0.1:8788",
        "sk-test",
        Some("vision-x"),
        &models,
        false,
    )
    .unwrap();
    let expected = [
        (
            ".dsh/.credentials.yaml",
            "9d66458bd6b34dfdf808186e10585e17a6c7cd840271f8864011647c144a02e8",
        ),
        (
            ".dsh/profiles/desktop/cordis.patch.yml",
            "b51be3a3c615c41645eec3aa1667832df5ab33d02240aeb43ab250ae53832fbc",
        ),
        (
            ".dsh/profiles/web/cordis.patch.yml",
            "a5b08b629dd9ab6a5658208a2ae88ea1b0f0c782ef05494a2abb4f8dccc66402",
        ),
        (
            ".dsh/settings.yaml",
            "5ca8ad12b4d9a6e669f09654e5054e23494d3943c1aaddc6f3f650e8ca574d9c",
        ),
    ];
    for (relative, sha) in expected {
        let bytes = std::fs::read(home.join(relative)).unwrap();
        assert_eq!(store::sha256_hex(&bytes), sha, "{relative}");
    }
    restore(&accounts, &home, "dsh").unwrap();
    let _ = std::fs::remove_dir_all(root);
}

fn snapshot(result: &Value) -> BTreeMap<String, String> {
    result["files"]
        .as_array()
        .unwrap()
        .iter()
        .map(|entry| {
            let path = entry["path"].as_str().unwrap();
            (
                path.into(),
                store::sha256_hex(&std::fs::read(path).unwrap()),
            )
        })
        .collect()
}
