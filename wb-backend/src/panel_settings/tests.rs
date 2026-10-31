use super::*;

#[test]
fn key_masking_never_returns_a_short_secret() {
    assert_eq!(mask("12345678"), "********");
    assert_eq!(mask("123456789"), "1234******6789");
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
