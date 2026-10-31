//! Conservative TOML and dotenv text editors.

use serde_json::Value;

pub(super) fn toml_upsert_top_key(text: &str, key: &str, value: &Value) -> Result<String, String> {
    let rendered = format!("{key} = {}", toml_value(value)?);
    let mut lines: Vec<String> = text.split('\n').map(str::to_string).collect();
    let mut last_top = None;
    for idx in 0..lines.len() {
        if toml_table(&lines[idx]).is_some() {
            break;
        }
        if let Some(found) = toml_key(&lines[idx]) {
            last_top = Some(idx);
            if found == key {
                lines[idx] = rendered;
                return Ok(lines.join("\n"));
            }
        }
    }
    lines.insert(last_top.map_or(0, |idx| idx + 1), rendered);
    Ok(lines.join("\n"))
}

pub(super) fn toml_upsert_table(
    text: &str,
    table: &str,
    mapping: &[(&str, Value)],
) -> Result<String, String> {
    let mut lines: Vec<String> = text.split('\n').map(str::to_string).collect();
    if let Some(start) = lines
        .iter()
        .position(|line| toml_table(line).as_deref() == Some(table))
    {
        let end = table_end(&lines, start);
        return toml_upsert_range(lines, start + 1, end, mapping);
    }
    while lines.last().is_some_and(|line| line.trim().is_empty()) {
        lines.pop();
    }
    if !lines.is_empty() {
        lines.push(String::new());
    }
    lines.push(format!("[{table}]"));
    for (key, value) in mapping {
        lines.push(format!("{key} = {}", toml_value(value)?));
    }
    Ok(format!("{}\n", lines.join("\n")))
}

fn toml_upsert_range(
    mut lines: Vec<String>,
    start: usize,
    end: usize,
    mapping: &[(&str, Value)],
) -> Result<String, String> {
    let mut remaining: Vec<(&str, &Value)> = mapping.iter().map(|(k, v)| (*k, v)).collect();
    let mut last_content = start;
    for idx in start..end {
        let stripped = lines[idx].trim();
        if stripped.is_empty() || stripped.starts_with('#') {
            continue;
        }
        last_content = idx + 1;
        let Some(key) = toml_key(&lines[idx]) else {
            continue;
        };
        if let Some(pos) = remaining.iter().position(|(name, _)| *name == key) {
            let (_, value) = remaining.remove(pos);
            lines[idx] = format!("{key} = {}", toml_value(value)?);
        }
    }
    let mut insert = last_content;
    for (key, value) in remaining {
        lines.insert(insert, format!("{key} = {}", toml_value(value)?));
        insert += 1;
    }
    Ok(lines.join("\n"))
}

pub(super) fn toml_drop_table_keys(text: &str, table: &str, keys: &[&str]) -> String {
    let mut lines: Vec<String> = text.split('\n').map(str::to_string).collect();
    let Some(start) = lines
        .iter()
        .position(|line| toml_table(line).as_deref() == Some(table))
    else {
        return text.to_string();
    };
    let end = table_end(&lines, start);
    let stale: Vec<usize> = (start + 1..end)
        .filter(|&idx| toml_key(&lines[idx]).is_some_and(|key| keys.contains(&key.as_str())))
        .collect();
    for idx in stale.into_iter().rev() {
        lines.remove(idx);
    }
    lines.join("\n")
}

fn toml_value(value: &Value) -> Result<String, String> {
    if value.is_null() {
        return Err("TOML has no null value".into());
    }
    serde_json::to_string(value).map_err(|e| e.to_string())
}

fn toml_table(line: &str) -> Option<String> {
    let rest = line.trim_start();
    let end = rest.find(']')?;
    let inner = &rest[1..end];
    let tail = rest[end + 1..].trim();
    if !rest.starts_with('[')
        || !(tail.is_empty() || tail.starts_with('#'))
        || inner.is_empty()
        || !inner
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-'))
    {
        return None;
    }
    Some(inner.to_string())
}

fn toml_key(line: &str) -> Option<String> {
    let rest = line.trim_start();
    let eq = rest.find('=')?;
    let key = rest[..eq].trim();
    if key.is_empty()
        || !key
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'))
    {
        return None;
    }
    Some(key.to_string())
}

fn table_end(lines: &[String], start: usize) -> usize {
    (start + 1..lines.len())
        .find(|&idx| toml_table(&lines[idx]).is_some())
        .unwrap_or(lines.len())
}

#[allow(dead_code)] // wb_agents exposes this helper; none of the current six clients consumes dotenv.
pub(super) fn env_upsert(text: &str, mapping: &[(&str, &str)]) -> String {
    let mut lines: Vec<String> = text.split('\n').map(str::to_string).collect();
    let mut remaining: Vec<(&str, &str)> = mapping.to_vec();
    for line in &mut lines {
        let Some(key) = env_key(line) else {
            continue;
        };
        if let Some(pos) = remaining.iter().position(|(name, _)| *name == key) {
            let (_, value) = remaining.remove(pos);
            *line = format!("{key}={value}");
        }
    }
    if !remaining.is_empty() {
        while lines.last().is_some_and(|line| line.trim().is_empty()) {
            lines.pop();
        }
        lines.extend(
            remaining
                .into_iter()
                .map(|(key, value)| format!("{key}={value}")),
        );
        lines.push(String::new());
    }
    lines.join("\n")
}

#[allow(dead_code)]
fn env_key(line: &str) -> Option<String> {
    let rest = line.trim_start();
    let eq = rest.find('=')?;
    let key = &rest[..eq];
    let mut chars = key.chars();
    let first = chars.next()?;
    if !(first.is_ascii_alphabetic() || first == '_')
        || !chars.all(|c| c.is_ascii_alphanumeric() || c == '_')
    {
        return None;
    }
    Some(key.to_string())
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn toml_top_key_matches_python() {
        let got = toml_upsert_top_key("[table]\nx = 1\n", "model", &json!("gpt-x")).unwrap();
        assert_eq!(got, "model = \"gpt-x\"\n[table]\nx = 1\n");
    }

    #[test]
    fn toml_table_and_drop_match_python() {
        let mapping = [
            ("name", json!("WB Proxy")),
            ("base_url", json!("http://127.0.0.1:8788/v1")),
        ];
        let got = toml_upsert_table(
            "model = \"a\"\n\n[model_providers.wb-proxy]\nname = \"old\"\nenv_key = \"X\"\n\n[other]\na=1\n",
            "model_providers.wb-proxy",
            &mapping,
        )
        .unwrap();
        assert_eq!(
            got,
            "model = \"a\"\n\n[model_providers.wb-proxy]\nname = \"WB Proxy\"\nenv_key = \"X\"\nbase_url = \"http://127.0.0.1:8788/v1\"\n\n[other]\na=1\n"
        );
        let dropped = toml_drop_table_keys(
            &got,
            "model_providers.wb-proxy",
            &["env_key", "env_key_instructions"],
        );
        assert_eq!(
            dropped,
            "model = \"a\"\n\n[model_providers.wb-proxy]\nname = \"WB Proxy\"\nbase_url = \"http://127.0.0.1:8788/v1\"\n\n[other]\na=1\n"
        );
    }

    #[test]
    fn env_upsert_matches_python() {
        let got = env_upsert(
            "OLD=1\n# keep\nOPENAI_API_KEY=old\n\n",
            &[
                ("OPENAI_API_KEY", "new"),
                ("OPENAI_BASE_URL", "http://x/v1"),
            ],
        );
        assert_eq!(
            got,
            "OLD=1\n# keep\nOPENAI_API_KEY=new\nOPENAI_BASE_URL=http://x/v1\n"
        );
    }
}
