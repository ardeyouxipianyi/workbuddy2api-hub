//! Conservative text editors used by the client config writers.
//!
//! These mirror the Python implementation: only the addressed YAML/TOML/ENV
//! lines are touched, so comments and unrelated user formatting survive.

use serde_json::Value;

#[derive(Clone, Copy)]
pub(super) enum YamlValue<'a> {
    Json(&'a Value),
    Block(&'a Value),
}

pub(super) fn yaml_upsert_inline(
    text: &str,
    key_path: &str,
    value: &Value,
) -> Result<String, String> {
    yaml_upsert(text, key_path, YamlValue::Json(value))
}

#[allow(dead_code)] // DSH uses the lower-level block upsert; this is the public parity helper.
pub(super) fn yaml_upsert_block(
    text: &str,
    key_path: &str,
    value: &Value,
) -> Result<String, String> {
    yaml_upsert(text, key_path, YamlValue::Block(value))
}

fn yaml_upsert(text: &str, key_path: &str, value: YamlValue<'_>) -> Result<String, String> {
    let keys: Vec<&str> = key_path
        .split('.')
        .map(str::trim)
        .filter(|part| !part.is_empty())
        .collect();
    if keys.is_empty() {
        return Err("yaml upsert: empty key path".into());
    }
    let lines: Vec<String> = text.split('\n').map(str::to_string).collect();
    let end = lines.len();
    let lines = yaml_upsert_at(lines, 0, end, 0, &keys, value)?;
    Ok(lines.join("\n"))
}

pub(super) fn yaml_upsert_at(
    mut lines: Vec<String>,
    start: usize,
    end: usize,
    indent: usize,
    keys: &[&str],
    value: YamlValue<'_>,
) -> Result<Vec<String>, String> {
    let key = keys[0];
    for idx in start..end.min(lines.len()) {
        if yaml_key(&lines[idx]).as_deref() != Some(key) || yaml_indent(&lines[idx]) != indent {
            continue;
        }
        let after = after_colon(&lines[idx]).trim().to_string();
        let child_end = yaml_block_end(&lines, idx, indent);
        if keys.len() == 1 {
            replace_leaf(&mut lines, idx, child_end, indent, key, value)?;
            return Ok(lines);
        }
        if !after.is_empty() {
            yaml_inline_to_block(&mut lines, idx, key, &after)?;
        }
        let child_end = yaml_block_end(&lines, idx, indent);
        return yaml_upsert_at(lines, idx + 1, child_end, indent + 2, &keys[1..], value);
    }

    let insert = yaml_insert_point(&lines, start, end);
    let mut added = if keys.len() == 1 {
        render_leaf(indent, key, value)?
    } else {
        let mut out = vec![format!("{}{}:", " ".repeat(indent), key)];
        out.extend(yaml_upsert_at(
            Vec::new(),
            0,
            0,
            indent + 2,
            &keys[1..],
            value,
        )?);
        out
    };
    lines.splice(insert..insert, added.drain(..));
    Ok(lines)
}

fn replace_leaf(
    lines: &mut Vec<String>,
    idx: usize,
    child_end: usize,
    indent: usize,
    key: &str,
    value: YamlValue<'_>,
) -> Result<(), String> {
    let replacement = render_leaf(indent, key, value)?;
    lines.splice(idx..child_end, replacement);
    Ok(())
}

fn render_leaf(indent: usize, key: &str, value: YamlValue<'_>) -> Result<Vec<String>, String> {
    match value {
        YamlValue::Json(value) => Ok(vec![format!(
            "{}{}: {}",
            " ".repeat(indent),
            key,
            json_compact(value)
        )]),
        YamlValue::Block(value) => {
            let mut out = vec![format!("{}{}:", " ".repeat(indent), key)];
            out.extend(yaml_emit(value, indent + 2)?);
            Ok(out)
        }
    }
}

fn json_compact(value: &Value) -> String {
    match value {
        Value::Array(items) => format!(
            "[{}]",
            items
                .iter()
                .map(json_compact)
                .collect::<Vec<_>>()
                .join(", ")
        ),
        Value::Object(map) => format!(
            "{{{}}}",
            map.iter()
                .map(|(key, value)| format!(
                    "{}: {}",
                    serde_json::to_string(key).unwrap_or_default(),
                    json_compact(value)
                ))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        _ => serde_json::to_string(value).unwrap_or_default(),
    }
}

pub(super) fn yaml_refs_upsert(text: &str, key: &str, value: &str) -> Result<String, String> {
    let mut lines: Vec<String> = text.split('\n').map(str::to_string).collect();
    if let Some(idx) = lines
        .iter()
        .position(|line| yaml_indent(line) == 0 && yaml_key(line).as_deref() == Some("refs"))
    {
        let after = after_colon(&lines[idx]).trim();
        if !after.is_empty() {
            return Err(format!(
                "cannot upsert into `refs: {after}` - only a block-style refs: mapping is supported"
            ));
        }
        let end = yaml_block_end(&lines, idx, 0);
        if let Some(found) = (idx + 1..end)
            .find(|&j| yaml_indent(&lines[j]) == 2 && yaml_key(&lines[j]).as_deref() == Some(key))
        {
            lines[found] = format!("  {key}: {value}");
        } else {
            let insert = yaml_insert_point(&lines, idx + 1, end);
            lines.insert(insert, format!("  {key}: {value}"));
        }
        return Ok(lines.join("\n"));
    }
    while lines.last().is_some_and(|line| line.trim().is_empty()) {
        lines.pop();
    }
    lines.push("refs:".into());
    lines.push(format!("  {key}: {value}"));
    Ok(format!("{}\n", lines.join("\n")))
}

pub(super) fn yaml_emit(value: &Value, indent: usize) -> Result<Vec<String>, String> {
    match value {
        Value::Object(map) => {
            let mut out = Vec::new();
            for (key, item) in map {
                let name = yaml_scalar(&Value::String(key.clone()));
                match item {
                    Value::Object(_) => {
                        out.push(format!("{}{}:", " ".repeat(indent), name));
                        out.extend(yaml_emit(item, indent + 2)?);
                    }
                    Value::Array(_) => {
                        out.push(format!("{}{}:", " ".repeat(indent), name));
                        out.extend(yaml_emit_list(item, indent + 2)?);
                    }
                    _ => out.push(format!(
                        "{}{}: {}",
                        " ".repeat(indent),
                        name,
                        yaml_scalar(item)
                    )),
                }
            }
            Ok(out)
        }
        Value::Array(_) => yaml_emit_list(value, indent),
        _ => Ok(vec![format!(
            "{}{}",
            " ".repeat(indent),
            yaml_scalar(value)
        )]),
    }
}

fn yaml_emit_list(value: &Value, indent: usize) -> Result<Vec<String>, String> {
    let Value::Array(items) = value else {
        return yaml_emit(value, indent);
    };
    let pad = " ".repeat(indent);
    let mut out = Vec::new();
    for item in items {
        if let Value::Object(map) = item {
            for (position, (key, val)) in map.iter().enumerate() {
                let prefix = if position == 0 { "- " } else { "  " };
                let name = yaml_scalar(&Value::String(key.clone()));
                match val {
                    Value::Object(_) => {
                        out.push(format!("{pad}{prefix}{name}:"));
                        out.extend(yaml_emit(val, indent + 4)?);
                    }
                    Value::Array(_) => {
                        out.push(format!("{pad}{prefix}{name}:"));
                        out.extend(yaml_emit_list(val, indent + 4)?);
                    }
                    _ => out.push(format!("{pad}{prefix}{name}: {}", yaml_scalar(val))),
                }
            }
        } else if matches!(item, Value::Array(_)) {
            return Err("nested lists are outside the supported subset".into());
        } else {
            out.push(format!("{pad}- {}", yaml_scalar(item)));
        }
    }
    Ok(out)
}

pub(super) fn yaml_scalar(value: &Value) -> String {
    match value {
        Value::Bool(v) => v.to_string(),
        Value::Number(v) => v.to_string(),
        Value::Null => "null".into(),
        Value::String(text) if yaml_plain(text) => text.clone(),
        Value::String(text) => serde_json::to_string(text).unwrap_or_default(),
        _ => serde_json::to_string(value).unwrap_or_default(),
    }
}

fn yaml_plain(text: &str) -> bool {
    let mut chars = text.chars();
    matches!(chars.next(), Some(c) if c.is_ascii_alphanumeric() || c == '_')
        && !text.ends_with(' ')
        && !text.contains(": ")
        && !text.contains(" #")
        && chars.all(|c| {
            c.is_ascii_alphanumeric()
                || matches!(
                    c,
                    '_' | ' '
                        | '.'
                        | '/'
                        | ':'
                        | '@'
                        | '+'
                        | '('
                        | ')'
                        | '['
                        | ']'
                        | '{'
                        | '}'
                        | '<'
                        | '>'
                        | '~'
                        | '-'
                )
        })
}

fn yaml_indent(line: &str) -> usize {
    line.len() - line.trim_start_matches(' ').len()
}

fn yaml_key(line: &str) -> Option<String> {
    let stripped = line.trim();
    if stripped.is_empty()
        || stripped.starts_with('#')
        || stripped.starts_with('-')
        || yaml_indent(line) == 0 && stripped.starts_with(' ')
    {
        return None;
    }
    let bytes = line.trim_start_matches(' ').as_bytes();
    let first = *bytes.first()?;
    if !(first.is_ascii_alphanumeric() || first == b'_') {
        return None;
    }
    let mut colon = None;
    for (idx, &byte) in bytes.iter().enumerate() {
        if byte == b':' {
            colon = Some(idx);
            break;
        }
        if !(byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b'-') || byte == b' ') {
            return None;
        }
    }
    let colon = colon?;
    let tail = &line.trim_start_matches(' ')[colon + 1..];
    if !(tail.is_empty() || tail.starts_with(char::is_whitespace)) {
        return None;
    }
    let key = line.trim_start_matches(' ')[..colon].trim();
    (!key.is_empty()).then(|| key.to_string())
}

fn after_colon(line: &str) -> &str {
    line.split_once(':').map(|(_, tail)| tail).unwrap_or("")
}

fn yaml_block_end(lines: &[String], start: usize, indent: usize) -> usize {
    (start + 1..lines.len())
        .find(|&idx| {
            let stripped = lines[idx].trim();
            !stripped.is_empty() && yaml_indent(&lines[idx]) <= indent
        })
        .unwrap_or(lines.len())
}

fn yaml_insert_point(lines: &[String], start: usize, end: usize) -> usize {
    let mut idx = end.min(lines.len());
    while idx > start && lines[idx - 1].trim().is_empty() {
        idx -= 1;
    }
    if idx > start {
        idx
    } else {
        end.min(lines.len())
    }
}

fn yaml_inline_to_block(
    lines: &mut Vec<String>,
    idx: usize,
    key: &str,
    inline: &str,
) -> Result<(), String> {
    let value = inline.trim();
    if value.is_empty() {
        return Ok(());
    }
    if !(value.starts_with('{') && value.ends_with('}')) {
        return Err(format!(
            "cannot nest keys under `{key}: {inline}` - only an inline {{..}} mapping can be expanded"
        ));
    }
    let indent = yaml_indent(&lines[idx]);
    let inner = value[1..value.len() - 1].trim();
    let mut replacement = vec![format!("{}{}:", " ".repeat(indent), key)];
    if !inner.is_empty() {
        replacement.extend(
            inner
                .split(',')
                .map(str::trim)
                .filter(|part| !part.is_empty())
                .map(|part| format!("{}{}", " ".repeat(indent + 2), part)),
        );
    }
    lines.splice(idx..idx + 1, replacement);
    Ok(())
}
