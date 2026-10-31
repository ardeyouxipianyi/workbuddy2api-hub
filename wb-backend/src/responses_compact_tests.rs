use serde_json::json;

use crate::responses_compact::{
    adjust_chat, decode_summary, encode_summary, nonstream_body, requested, COMPACTION_PROMPT,
};

#[test]
fn compaction_path_and_trigger_are_detected() {
    assert!(requested("/v1/responses/compact", &json!({})));
    assert!(requested(
        "/v1/responses",
        &json!({"input":[{"type":"compaction_trigger"}]})
    ));
    assert!(!requested("/v1/responses", &json!({"input":"hello"})));
}

#[test]
fn summary_codec_round_trips_and_keeps_plain_text() {
    let encoded = encode_summary("the summary");
    assert_eq!(decode_summary(&encoded), "the summary");
    assert_eq!(decode_summary("plain text"), "plain text");
}

#[test]
fn legacy_and_native_requests_get_one_compaction_prompt_without_tools() {
    let mut legacy =
        json!({"model":"m","messages":[{"role":"user","content":"continue"}],"tools":[{}]});
    adjust_chat(&mut legacy, &json!({"input":"continue"}));
    assert!(legacy.get("tools").is_none());
    assert_eq!(legacy["messages"].as_array().unwrap().len(), 2);

    let mut native =
        json!({"model":"m","messages":[{"role":"user","content":"history"}],"tools":[{}]});
    adjust_chat(
        &mut native,
        &json!({"input":[{"type":"compaction_trigger"}]}),
    );
    assert_eq!(native["messages"].as_array().unwrap().len(), 2);

    let mut supplied = json!({
        "model":"m",
        "messages":[{"role":"user","content":COMPACTION_PROMPT}],
        "tools":[{}]
    });
    adjust_chat(&mut supplied, &json!({"input":COMPACTION_PROMPT}));
    let prompts = supplied["messages"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|message| {
            message["content"]
                .as_str()
                .is_some_and(|text| text.contains("CONTEXT CHECKPOINT COMPACTION"))
        })
        .count();
    assert_eq!(prompts, 1);
}

#[test]
fn nonstream_response_keys_match_python() {
    let value = nonstream_body("resp_x", 1, "m", json!({}), json!({}));
    let keys: Vec<&str> = value
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    assert_eq!(
        keys,
        [
            "id",
            "object",
            "created_at",
            "status",
            "model",
            "output",
            "output_text",
            "metadata",
            "usage"
        ]
    );
}
