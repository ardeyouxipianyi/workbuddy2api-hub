use super::*;

#[test]
fn key_masking_never_returns_a_short_secret() {
    assert_eq!(mask("12345678"), "********");
    assert_eq!(mask("123456789"), "1234******6789");
}

/// 面板只画打码后的 key：提交时留空必须沿用已存密钥，否则一次「保存设置」
/// 就会把所有密钥清成空串（真丢过一次）。
#[test]
fn blank_key_keeps_the_stored_secret() {
    let mut settings = Map::new();
    settings.insert(
        "api_keys".into(),
        json!([{"id": "aaa", "name": "Hermes", "key": "secret-1", "realm": "intl",
                "enabled": true, "created_at": "2026/10/01 10:00"}]),
    );
    let submitted = json!([{"id": "aaa", "name": "Hermes", "key": "", "realm": "intl",
                            "enabled": true}]);
    let live = merge_api_keys(&mut settings, &submitted, Some(&json!([]))).unwrap();
    assert_eq!(live, 1);
    let rows = settings["api_keys"].as_array().unwrap();
    assert_eq!(rows[0]["key"], json!("secret-1"));
    assert_eq!(rows[0]["created_at"], json!("2026/10/01 10:00"));
}

/// 只有点名的 id 才退掉，提交里没提到的键原样保留（提交可能是旧快照）。
#[test]
fn only_named_ids_are_retired() {
    let mut settings = Map::new();
    settings.insert(
        "api_keys".into(),
        json!([
            {"id": "aaa", "name": "Hermes", "key": "secret-1"},
            {"id": "bbb", "name": "公司", "key": "secret-2"},
        ]),
    );
    let submitted = json!([{"id": "aaa", "name": "Hermes", "key": ""}]);
    let live = merge_api_keys(&mut settings, &submitted, Some(&json!(["bbb"]))).unwrap();
    assert_eq!(live, 1);
    let rows = settings["api_keys"].as_array().unwrap();
    assert_eq!(rows.len(), 2);
    let retired = rows.iter().find(|row| row["id"] == json!("bbb")).unwrap();
    assert_eq!(retired["key"], json!(""));
    assert_eq!(retired["enabled"], json!(false));
    assert!(retired["deleted_at"].as_str().unwrap().len() >= 10);
}

/// 新行由服务端发 id；同一份提交里重复的新行不共享 id。
#[test]
fn new_rows_get_ids() {
    let mut settings = Map::new();
    let submitted = json!([
        {"id": "", "name": "A", "key": "secret-a"},
        {"id": "", "name": "B", "key": "secret-b"},
    ]);
    merge_api_keys(&mut settings, &submitted, Some(&json!([]))).unwrap();
    let rows = settings["api_keys"].as_array().unwrap();
    let ids: Vec<&str> = rows.iter().map(|row| row["id"].as_str().unwrap()).collect();
    assert!(ids.iter().all(|id| !id.is_empty()));
    assert_ne!(ids[0], ids[1]);
    // 空密钥的行必须被拒，而不是静默落成空串。
    assert!(merge_api_keys(&mut settings, &json!([{"id": "", "name": "C", "key": ""}]), None).is_err());
}

#[test]
fn limits_fill_every_scope() {
    let mut settings = Map::new();
    settings.insert("daily_token_limit".into(), json!(42));
    let view = limits(&settings);
    assert_eq!(view["daily_token_limit"]["global"], json!(42));
    assert_eq!(view["daily_token_limit"]["intl"], Value::Null);
    assert_eq!(view["expiring_window_days"]["global"], json!(7));
}

#[test]
fn limits_coerce_strings_and_groups_like_python() {
    let settings = Map::from_iter([(
        "limits".to_string(),
        json!({"daily_credit_limit": {"global": "12", "intl": 3.0, "cn": null}}),
    )]);
    let view = limits(&settings);
    assert_eq!(view["daily_credit_limit"]["global"], json!(12));
    assert_eq!(view["daily_credit_limit"]["intl"], json!(3));
    assert_eq!(view["daily_credit_limit"]["cn"], Value::Null);
}

#[test]
fn string_lists_are_trimmed_deduplicated_and_bounded() {
    assert_eq!(
        string_list(Some(&json!([" a ", "a", "", "b", "c"])), 2),
        vec!["a".to_string(), "b".to_string()]
    );
}
