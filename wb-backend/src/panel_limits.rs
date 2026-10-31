//! Validation and persistence shape for dashboard limit settings.

use serde_json::{json, Map, Value};

const LIMIT_KEYS: &[&str] = &[
    "reserve_credits",
    "daily_token_limit",
    "daily_credit_limit",
    "model_daily_token_limit",
    "expiring_window_days",
];

pub fn apply(
    settings: &mut Map<String, Value>,
    patch: &Map<String, Value>,
) -> Result<Vec<String>, String> {
    let grouped = patch.get("limits");
    if grouped.is_some_and(|value| !value.is_object()) {
        return Err("limits must be an object".into());
    }
    let mut touched = Vec::new();
    for key in LIMIT_KEYS {
        let scoped = grouped
            .and_then(Value::as_object)
            .and_then(|limits| limits.get(*key))
            .filter(|value| value.is_object());
        let flat = patch.get(*key);
        if scoped.is_none() && flat.is_none() {
            continue;
        }
        let entry = ensure_grouped(settings, key)?;
        if let Some(scoped) = scoped.and_then(Value::as_object) {
            for scope in ["global", "intl", "cn"] {
                if let Some(value) = scoped.get(scope) {
                    entry.insert(scope.to_string(), coerce(scope, value, key)?);
                }
            }
        } else if let Some(value) = flat {
            entry.insert("global".into(), coerce("global", value, key)?);
        }
        touched.push((*key).to_string());
    }
    Ok(touched)
}

fn ensure_grouped<'a>(
    settings: &'a mut Map<String, Value>,
    key: &str,
) -> Result<&'a mut Map<String, Value>, String> {
    if !settings.get("limits").is_some_and(Value::is_object) {
        settings.insert("limits".into(), json!({}));
    }
    let missing = !settings
        .get("limits")
        .and_then(Value::as_object)
        .is_some_and(|limits| limits.get(key).is_some_and(Value::is_object));
    let flat = settings
        .get(key)
        .and_then(Value::as_i64)
        .filter(|value| *value >= 0)
        .unwrap_or(if key == "expiring_window_days" { 7 } else { 0 });
    let limits = settings
        .get_mut("limits")
        .and_then(Value::as_object_mut)
        .ok_or_else(|| "limits must be an object".to_string())?;
    if missing {
        limits.insert(
            key.to_string(),
            json!({"global": flat, "intl": Value::Null, "cn": Value::Null}),
        );
    }
    limits
        .get_mut(key)
        .and_then(Value::as_object_mut)
        .ok_or_else(|| format!("{key} must be an object"))
}

fn coerce(scope: &str, value: &Value, key: &str) -> Result<Value, String> {
    if scope != "global" && (value.is_null() || value.as_str() == Some("")) {
        return Ok(Value::Null);
    }
    let number = if value.is_boolean() || value.is_null() {
        None
    } else {
        value
            .as_i64()
            .or_else(|| value.as_f64().map(|number| number as i64))
            .or_else(|| value.as_str().and_then(|text| text.trim().parse().ok()))
    }
    .ok_or_else(|| {
        if scope == "global" {
            format!("{key} must be a whole number")
        } else {
            format!("{key} must be a whole number or blank")
        }
    })?;
    if number < 0 {
        return Err(format!("{key} cannot be negative"));
    }
    Ok(json!(number))
}

pub fn is_limit_key(key: &str) -> bool {
    LIMIT_KEYS.contains(&key)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn grouped_patch_preserves_other_keys_and_applies_realm_overrides() {
        let mut settings = Map::from_iter([(
            "limits".to_string(),
            json!({"daily_credit_limit": {"global": 3, "intl": 2, "cn": null}}),
        )]);
        let patch = json!({"limits": {
            "daily_token_limit": {"global": 100, "intl": 0, "cn": null}
        }});
        let touched = apply(&mut settings, patch.as_object().unwrap()).unwrap();
        assert_eq!(touched, vec!["daily_token_limit"]);
        assert_eq!(settings["limits"]["daily_credit_limit"]["global"], 3);
        assert_eq!(settings["limits"]["daily_token_limit"]["global"], 100);
        assert_eq!(settings["limits"]["daily_token_limit"]["intl"], 0);
    }

    #[test]
    fn flat_legacy_values_and_blank_overrides_keep_python_semantics() {
        let mut settings = Map::new();
        let patch = json!({"daily_token_limit": 42, "limits": {
            "reserve_credits": {"intl": null, "cn": 7}
        }});
        apply(&mut settings, patch.as_object().unwrap()).unwrap();
        assert_eq!(settings["limits"]["daily_token_limit"]["global"], 42);
        assert_eq!(settings["limits"]["reserve_credits"]["intl"], Value::Null);
        assert_eq!(settings["limits"]["reserve_credits"]["cn"], 7);
    }

    #[test]
    fn invalid_values_are_rejected() {
        let mut settings = Map::new();
        for value in [json!(true), json!("x"), json!(-1)] {
            let patch = json!({"limits": {"daily_token_limit": {"global": value}}});
            assert!(apply(&mut settings, patch.as_object().unwrap()).is_err());
        }
    }

    #[test]
    fn numeric_strings_and_integral_floats_match_python_int_coercion() {
        let mut settings = Map::new();
        let patch = json!({"limits": {
            "daily_token_limit": {"global": "42"},
            "daily_credit_limit": {"global": 3.5}
        }});
        apply(&mut settings, patch.as_object().unwrap()).unwrap();
        assert_eq!(settings["limits"]["daily_token_limit"]["global"], 42);
        assert_eq!(settings["limits"]["daily_credit_limit"]["global"], 3);
    }
}
