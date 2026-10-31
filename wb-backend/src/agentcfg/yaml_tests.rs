//! YAML editor parity tests.

use serde_json::json;

use super::yaml::{yaml_refs_upsert, yaml_upsert_block, yaml_upsert_inline};

#[test]
fn yaml_inline_conversion_matches_python() {
    let input = "llm-pi-ai: {providers: {}, mode: normal}\nkeep: yes\n";
    let got = yaml_upsert_inline(input, "llm-pi-ai.providers.wb-proxy", &json!({"x": 1})).unwrap();
    assert_eq!(
        got,
        "llm-pi-ai:\n  providers:\n    wb-proxy: {\"x\":1}\n  mode: normal\nkeep: yes\n"
            .replace("{\"x\":1}", "{\"x\": 1}")
    );
}

#[test]
fn yaml_block_matches_python() {
    let input = "llm-pi-ai:\n  providers:\n    old:\n      x: 1\n# tail\n";
    let value = json!({"id": "m", "items": [{"a": 1, "b": "two words"}, "z"]});
    let got = yaml_upsert_block(input, "llm-pi-ai.providers.wb-proxy", &value).unwrap();
    assert_eq!(
            got,
            "llm-pi-ai:\n  providers:\n    old:\n      x: 1\n    wb-proxy:\n      id: m\n      items:\n        - a: 1\n          b: two words\n        - z\n# tail\n"
        );
}

#[test]
fn yaml_refs_matches_python() {
    let got =
        yaml_refs_upsert("refs:\n  A: old\nother: 1\n", "WB_PROXY_API_KEY", "sk-test").unwrap();
    assert_eq!(
        got,
        "refs:\n  A: old\n  WB_PROXY_API_KEY: sk-test\nother: 1\n"
    );
}
