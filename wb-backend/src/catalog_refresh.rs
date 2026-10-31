//! 面板 `POST /catalog/refresh`：从当前 realm 的上游 `/v3/config` 取目录，
//! 发布到进程内运行时目录，并把原始响应原子落盘。
//!
//! 路由接线由父进程负责；`entries_for()` 是该模块给 `/v1/models` 的运行时入口。

#![allow(dead_code)] // 父进程尚未统一接线；handler/entries_for 是稳定入口。

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock, RwLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use hyper::body::Incoming;
use hyper::{Request, StatusCode};
use serde_json::{json, Map, Value};

use crate::http::{body_json, json_resp, Resp};
use crate::App;

const INTL: &str = include_str!("../assets/catalog-intl.json");
const CN: &str = include_str!("../assets/catalog-cn.json");

#[derive(Clone, Debug)]
struct Config {
    timeout: Duration,
    cooldown: f64,
    cache_dir: PathBuf,
    remote_path: String,
    virtual_aliases: HashSet<String>,
    non_chat_prefixes: Vec<String>,
    non_chat_suffixes: Vec<String>,
    variant_suffixes: Vec<String>,
}

impl Config {
    fn from_env() -> Self {
        let env = |name: &str| std::env::var(name).ok();
        let seconds = env("WB_CATALOG_REFRESH_TIMEOUT_SECS")
            .and_then(|v| v.parse::<u64>().ok())
            .filter(|v| *v > 0)
            .unwrap_or(10);
        let cooldown = env("WB_CATALOG_REFRESH_COOLDOWN")
            .and_then(|v| v.parse::<f64>().ok())
            .filter(|v| v.is_finite() && *v >= 0.0)
            .unwrap_or(30.0);
        let cache_dir = env("WB_PROXY_CATALOG_DIR")
            .filter(|v| !v.trim().is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                Path::new(env!("CARGO_MANIFEST_DIR"))
                    .parent()
                    .unwrap_or_else(|| Path::new("."))
                    .join("catalog")
            });
        let list = |name: &str, default: &str| {
            env(name)
                .unwrap_or_else(|| default.to_string())
                .split(',')
                .map(str::trim)
                .filter(|v| !v.is_empty())
                .map(str::to_string)
                .collect::<Vec<_>>()
        };
        Self {
            timeout: Duration::from_secs(seconds),
            cooldown,
            cache_dir,
            remote_path: env("WB_CATALOG_REMOTE_PATH")
                .filter(|v| v.starts_with('/'))
                .unwrap_or_else(|| "/v3/config".into()),
            virtual_aliases: list(
                "WB_CATALOG_VIRTUAL_ALIASES",
                "default-model,fast-model,balanced-model,primary-model,deep-model,auto,lite",
            )
            .into_iter()
            .collect(),
            non_chat_prefixes: list("WB_CATALOG_NON_CHAT_PREFIXES", "codewise-,completion-"),
            non_chat_suffixes: list(
                "WB_CATALOG_NON_CHAT_SUFFIXES",
                "-image-alpha,-image-alpha-edit,-taco-completion",
            ),
            variant_suffixes: list("WB_CATALOG_VARIANT_SUFFIXES", "-sg,-x"),
        }
    }
}

#[derive(Clone)]
struct RuntimeCatalog {
    entries: Vec<Value>,
    meta: Value,
}

fn runtime() -> &'static RwLock<HashMap<String, RuntimeCatalog>> {
    static RUNTIME: OnceLock<RwLock<HashMap<String, RuntimeCatalog>>> = OnceLock::new();
    RUNTIME.get_or_init(|| RwLock::new(HashMap::new()))
}

fn refresh_at() -> &'static Mutex<HashMap<String, f64>> {
    static AT: OnceLock<Mutex<HashMap<String, f64>>> = OnceLock::new();
    AT.get_or_init(|| Mutex::new(HashMap::new()))
}

fn fetch_lock() -> &'static tokio::sync::Mutex<()> {
    static LOCK: OnceLock<tokio::sync::Mutex<()>> = OnceLock::new();
    LOCK.get_or_init(|| tokio::sync::Mutex::new(()))
}

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// 当前进程里该 realm 的目录；没有刷新过时回落到随二进制打包的静态目录。
pub fn entries_for(realm: &str) -> (Vec<Value>, Value) {
    if let Some(runtime) = runtime()
        .read()
        .unwrap_or_else(|e| e.into_inner())
        .get(realm)
        .cloned()
    {
        return (runtime.entries, runtime.meta);
    }
    (static_entries(realm), bundled_meta())
}

fn bundled_meta() -> Value {
    json!({"source": "bundled", "fetched_at": now(), "file_mtime": Value::Null})
}

fn static_entries(realm: &str) -> Vec<Value> {
    static STATIC: OnceLock<(Vec<Value>, Vec<Value>)> = OnceLock::new();
    let (intl, cn) = STATIC.get_or_init(|| {
        (
            serde_json::from_str(INTL).unwrap_or_default(),
            serde_json::from_str(CN).unwrap_or_default(),
        )
    });
    if realm == "cn" {
        cn.clone()
    } else {
        intl.clone()
    }
}

fn publish(app: &App, realm: &str, entries: Vec<Value>, fetched_at: f64) {
    let meta = json!({
        "source": "remote",
        "fetched_at": fetched_at,
        "file_mtime": Value::Null
    });
    runtime().write().unwrap_or_else(|e| e.into_inner()).insert(
        realm.to_string(),
        RuntimeCatalog {
            entries: entries.clone(),
            meta: meta.clone(),
        },
    );
    let mut state_catalog = app.state.model_catalog.write().unwrap();
    let mut all = match std::mem::take(&mut *state_catalog) {
        Value::Object(map) => map,
        _ => Map::new(),
    };
    all.insert(realm.to_string(), json!({"data": entries, "catalog": meta}));
    *state_catalog = Value::Object(all);
}

fn cooldown_response(realm: &str, wait: f64) -> Value {
    let seconds = (wait + 0.5) as i64;
    json!({
        "ok": false,
        "realm": realm,
        "source": Value::Null,
        "count": 0,
        "cooldown": true,
        "retry_after": (wait * 10.0).round() / 10.0,
        "msg": format!("刷新太频繁：约 {} 秒后可再试", seconds.max(1))
    })
}

fn failure(realm: &str) -> Value {
    json!({
        "ok": false,
        "realm": realm,
        "source": Value::Null,
        "count": 0,
        "msg": "上游未返回可用目录（账号不可用或网络异常），已保留现有清单"
    })
}

/// `POST /catalog/refresh` handler。
///
/// payload: `{"realm": "intl"|"cn"}`。任何结果都以 200 回答，`ok` 区分成败。
pub async fn handle(app: &Arc<App>, req: Request<Incoming>) -> Resp {
    let body = match body_json(req).await {
        Ok(v) => v,
        Err(resp) => return resp,
    };
    json_resp(StatusCode::OK, &refresh(app, &body).await)
}

async fn refresh(app: &Arc<App>, payload: &Value) -> Value {
    let config = Config::from_env();
    let Some(realm) = requested_realm(payload) else {
        return json!({
            "ok": false,
            "realm": Value::Null,
            "source": Value::Null,
            "count": 0,
            "msg": "realm 必须是 intl 或 cn"
        });
    };
    if let Some(response) = begin_refresh(&realm, &config) {
        return response;
    }
    let remote = {
        let _guard = fetch_lock().lock().await;
        fetch_remote(app, &realm, &config).await
    };
    let Some((ids, meta, raw)) = remote else {
        app.log(
            "catalog",
            "WARN",
            &format!("catalog refresh: {realm} kept the previous list (upstream gave none)"),
        );
        return failure(&realm);
    };
    publish_result(app, &realm, ids, meta, raw, &config)
}

fn requested_realm(payload: &Value) -> Option<String> {
    let realm = payload
        .get("realm")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_lowercase();
    matches!(realm.as_str(), "intl" | "cn").then_some(realm)
}

fn begin_refresh(realm: &str, config: &Config) -> Option<Value> {
    let at = now();
    let mut stamps = refresh_at().lock().unwrap_or_else(|e| e.into_inner());
    let wait = config.cooldown - (at - stamps.get(realm).copied().unwrap_or(0.0));
    if wait > 0.0 {
        return Some(cooldown_response(realm, wait));
    }
    stamps.insert(realm.to_string(), at);
    None
}

fn publish_result(
    app: &Arc<App>,
    realm: &str,
    ids: Vec<String>,
    meta: Map<String, Value>,
    raw: Value,
    config: &Config,
) -> Value {
    if let Err(e) = persist_remote_catalog(&realm, &raw, &config.cache_dir) {
        app.log(
            "catalog",
            "WARN",
            &format!("catalog cache: {realm} persist failed ({e})"),
        );
    }
    let curated = curate_remote_catalog(realm, &ids, &meta, config);
    let entries = merge_catalog(realm, &curated, &meta, true, config);
    let fetched_at = now();
    publish(app, realm, entries.clone(), fetched_at);
    app.log(
        "catalog",
        "INFO",
        &format!(
            "catalog refresh: {realm} updated from the product-config endpoint ({} models)",
            entries.len()
        ),
    );
    json!({
        "ok": true,
        "realm": realm,
        "source": "remote",
        "count": entries.len(),
        "msg": format!("已从上游 API 更新清单，共 {} 个模型", entries.len())
    })
}

async fn fetch_remote(
    app: &Arc<App>,
    realm: &str,
    config: &Config,
) -> Option<(Vec<String>, Map<String, Value>, Value)> {
    let candidate = crate::pool::candidates(app, realm, &[], None)
        .into_iter()
        .next()?;
    let token = crate::pool::ensure_fresh(app, &candidate).await;
    let base = crate::upstream::chat_upstream_base(realm, "workbuddy")
        .trim_end_matches('/')
        .to_string();
    let url = format!("{base}{}", config.remote_path);
    let identity = crate::identity::chat_headers(&crate::identity::Ctx {
        realm,
        product: "workbuddy",
        uid: &candidate.uid,
        token: &token,
        enterprise_id: None,
        department: None,
        session_key: None,
    });
    let header = |name: &str| {
        identity
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.clone())
            .unwrap_or_default()
    };
    let mut last = "no usable account".to_string();
    for ua in remote_uas(&identity) {
        match fetch_once(
            app,
            &url,
            &ua,
            &header("Origin"),
            &header("Referer"),
            &candidate.uid,
            &token,
            candidate.is_static_key,
            config.timeout,
        )
        .await
        {
            Ok(Some(parsed)) => return Some(parsed),
            Ok(None) => last = "empty catalogue".into(),
            Err(e) => last = e,
        }
    }
    app.log(
        "catalog",
        "WARN",
        &format!("remote catalog: {realm} fetch failed ({last})"),
    );
    None
}

fn remote_uas(identity: &[(String, String)]) -> Vec<String> {
    let first = identity
        .iter()
        .find(|(key, _)| key.eq_ignore_ascii_case("user-agent"))
        .map(|(_, value)| value.clone())
        .unwrap_or_default();
    let second = first
        .split_whitespace()
        .find(|part| part.starts_with("WorkBuddy/"))
        .unwrap_or(&first)
        .to_string();
    if second == first {
        vec![first]
    } else {
        vec![first, second]
    }
}

async fn fetch_once(
    app: &Arc<App>,
    url: &str,
    user_agent: &str,
    origin: &str,
    referer: &str,
    uid: &str,
    token: &str,
    static_key: bool,
    timeout: Duration,
) -> Result<Option<(Vec<String>, Map<String, Value>, Value)>, String> {
    let mut request = app
        .upstream
        .get(url)
        .header("Accept", "application/json, text/plain, */*")
        .header("User-Agent", user_agent)
        .header("Origin", origin)
        .header("Referer", referer)
        .header("Authorization", format!("Bearer {token}"))
        .header("X-User-Id", uid)
        .timeout(timeout);
    if static_key {
        request = request.header("X-API-Key", token);
    }
    let response = request.send().await.map_err(|e| e.to_string())?;
    if !response.status().is_success() {
        return Err(format!("HTTP {}", response.status()));
    }
    let raw: Value = response.json().await.map_err(|e| e.to_string())?;
    Ok(parse_remote_catalog(&raw).map(|(ids, meta)| (ids, meta, raw)))
}

fn agent_model_lists(payload: &Value) -> (Vec<Vec<String>>, Vec<Vec<String>>) {
    fn collect(root: &Value, cli: &mut Vec<Vec<String>>, other: &mut Vec<Vec<String>>) {
        let Some(agents) = root.get("agents") else {
            return;
        };
        let entries: Vec<(&str, &Value)> = match agents {
            Value::Object(map) => map.iter().map(|(k, v)| (k.as_str(), v)).collect(),
            Value::Array(items) => items
                .iter()
                .map(|v| (v.get("name").and_then(Value::as_str).unwrap_or(""), v))
                .collect(),
            _ => return,
        };
        for (name, entry) in entries {
            let Some(models) = entry.get("models").and_then(Value::as_array) else {
                continue;
            };
            if models.is_empty() || !models[0].is_string() {
                continue;
            }
            let ids: Vec<String> = models
                .iter()
                .filter_map(Value::as_str)
                .map(str::trim)
                .filter(|v| !v.is_empty())
                .map(str::to_string)
                .collect();
            if ids.is_empty() {
                continue;
            }
            if name.trim().eq_ignore_ascii_case("cli") {
                cli.push(ids);
            } else {
                other.push(ids);
            }
        }
    }
    let mut cli = Vec::new();
    let mut other = Vec::new();
    collect(payload, &mut cli, &mut other);
    if let Some(data) = payload.get("data") {
        collect(data, &mut cli, &mut other);
    }
    (cli, other)
}

fn parse_remote_catalog(payload: &Value) -> Option<(Vec<String>, Map<String, Value>)> {
    if !payload.is_object() {
        return None;
    }
    let (cli, other) = agent_model_lists(payload);
    let pool = if cli.is_empty() { other } else { cli };
    let best = pool.into_iter().max_by_key(Vec::len)?;
    let mut seen = HashSet::new();
    let ids = best
        .into_iter()
        .filter(|id| seen.insert(id.clone()))
        .collect::<Vec<_>>();
    if ids.is_empty() {
        return None;
    }

    fn walk(node: &Value, meta: &mut Map<String, Value>) {
        match node {
            Value::Object(map) => {
                if let Some(models) = map.get("models").and_then(Value::as_array) {
                    if models.first().and_then(|m| m.get("id")).is_some() {
                        for item in models {
                            if let Some(id) = item.get("id").and_then(Value::as_str) {
                                let id = id.trim();
                                if !id.is_empty() {
                                    meta.entry(id.to_string()).or_insert_with(|| item.clone());
                                }
                            }
                        }
                    }
                }
                for value in map.values() {
                    walk(value, meta);
                }
            }
            Value::Array(items) => {
                for item in items {
                    walk(item, meta);
                }
            }
            _ => {}
        }
    }
    let mut meta = Map::new();
    walk(payload, &mut meta);
    Some((ids, meta))
}

fn snapshot_credits() -> HashMap<String, String> {
    let mut out = HashMap::new();
    for raw in [INTL, CN] {
        for item in serde_json::from_str::<Vec<Value>>(raw).unwrap_or_default() {
            if let Some(id) = item.get("id").and_then(Value::as_str) {
                out.entry(id.to_string()).or_insert_with(|| {
                    item.get("credits")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .trim()
                        .to_lowercase()
                });
            }
        }
    }
    out
}

fn is_free(credits: &str) -> bool {
    matches!(
        credits.trim().to_lowercase().as_str(),
        "x0.00" | "x0" | "0" | "0.00"
    )
}

fn is_chat_model(id: &str, config: &Config) -> bool {
    !id.is_empty()
        && !config.virtual_aliases.contains(id)
        && !config
            .non_chat_prefixes
            .iter()
            .any(|prefix| id.starts_with(prefix))
        && !config
            .non_chat_suffixes
            .iter()
            .any(|suffix| id.ends_with(suffix))
}

fn curate_remote_catalog(
    _realm: &str,
    ids: &[String],
    meta: &Map<String, Value>,
    config: &Config,
) -> Vec<String> {
    let mut credits = snapshot_credits();
    for (id, item) in meta {
        if let Some(value) = item.get("credits") {
            credits.insert(
                id.clone(),
                value
                    .as_str()
                    .unwrap_or(&value.to_string())
                    .trim()
                    .to_lowercase(),
            );
        }
    }
    let listed = ids.iter().cloned().collect::<HashSet<_>>();
    let mut out = Vec::new();
    for id in ids {
        if !is_chat_model(id, config)
            || config
                .variant_suffixes
                .iter()
                .any(|suffix| id.ends_with(suffix))
        {
            continue;
        }
        if let Some(base) = id.strip_suffix("-f") {
            if listed.contains(base)
                && is_free(credits.get(base).map(String::as_str).unwrap_or(""))
                && !is_free(credits.get(id).map(String::as_str).unwrap_or(""))
            {
                continue;
            }
        } else {
            let sibling = format!("{id}-f");
            if listed.contains(&sibling)
                && is_free(credits.get(&sibling).map(String::as_str).unwrap_or(""))
                && !is_free(credits.get(id).map(String::as_str).unwrap_or(""))
            {
                continue;
            }
        }
        out.push(id.clone());
    }
    out
}

fn apply_remote_meta(base: &mut Map<String, Value>, meta: &Map<String, Value>) {
    apply_scalar_meta(base, meta);
    apply_capabilities(base, meta);
    apply_limits_and_reasoning(base, meta);
}

fn apply_scalar_meta(base: &mut Map<String, Value>, meta: &Map<String, Value>) {
    for key in ["credits", "vendor"] {
        match meta
            .get(key)
            .and_then(Value::as_str)
            .filter(|v| !v.is_empty())
        {
            Some(value) => {
                base.insert(key.to_string(), json!(value));
            }
            None if meta.contains_key(key) => {
                base.remove(key);
            }
            None => {}
        }
    }
    for key in ["temperature", "top_p"] {
        match meta.get(key).filter(|v| !v.is_null()) {
            Some(value) => {
                base.insert(key.to_string(), value.clone());
            }
            None if meta.contains_key(key) => {
                base.remove(key);
            }
            None => {}
        }
    }
    if meta.get("isDefault") == Some(&Value::Bool(true)) {
        base.insert("is_default".into(), json!(true));
    } else if meta.contains_key("isDefault") {
        base.remove("is_default");
    }
    apply_text_meta(base, meta);
}

fn apply_text_meta(base: &mut Map<String, Value>, meta: &Map<String, Value>) {
    if let Some(tags) = meta.get("tags").and_then(Value::as_array) {
        let tags: Vec<&str> = tags
            .iter()
            .filter_map(Value::as_str)
            .filter(|tag| !tag.starts_with("badge:"))
            .collect();
        if tags.is_empty() {
            base.remove("tags");
        } else {
            base.insert("tags".into(), json!(tags));
        }
    } else if meta.contains_key("tags") {
        base.remove("tags");
    }
    if let Some(name) = meta
        .get("name")
        .and_then(Value::as_str)
        .filter(|v| !v.is_empty())
    {
        base.insert("name".into(), json!(name));
    } else if meta.contains_key("name") {
        base.remove("name");
    }
    if let Some(desc) = meta
        .get("descriptionEn")
        .or_else(|| meta.get("descriptionZh"))
        .and_then(Value::as_str)
        .filter(|v| !v.is_empty())
    {
        base.insert("description".into(), json!(desc));
    } else if meta.contains_key("descriptionEn") || meta.contains_key("descriptionZh") {
        base.remove("description");
    }
}

fn apply_capabilities(base: &mut Map<String, Value>, meta: &Map<String, Value>) {
    let existing = |key: &str| base.get(key).and_then(Value::as_bool).unwrap_or(false);
    let vision = match meta.get("supportsImages") {
        Some(value) => value.as_bool().unwrap_or(false),
        None => existing("vision"),
    } && !meta
        .get("disabledMultimodal")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let tools = match meta.get("supportsToolCall") {
        Some(value) => value.as_bool().unwrap_or(false),
        None => existing("supports_tool_calls"),
    };
    let reasoning = match meta.get("supportsReasoning") {
        Some(value) => value.as_bool().unwrap_or(false),
        None => existing("supports_reasoning"),
    };
    if [
        "supportsImages",
        "supportsToolCall",
        "supportsReasoning",
        "disabledMultimodal",
    ]
    .iter()
    .any(|key| meta.contains_key(*key))
    {
        base.insert(
            "capabilities".into(),
            json!({
                "vision": vision, "tool_calls": tools, "reasoning": reasoning
            }),
        );
        for (key, value) in [
            ("supports_vision", vision),
            ("supports_images", vision),
            ("supports_tool_calls", tools),
            ("supports_reasoning", reasoning),
            ("vision", vision),
            ("multimodal", vision),
        ] {
            base.insert(key.into(), json!(value));
        }
        base.insert(
            "abilities".into(),
            json!({
                "vision": vision,
                "functionCall": tools,
                "function_call": tools,
                "reasoning": reasoning
            }),
        );
        set_modalities(base, vision);
    }
}

fn set_modalities(base: &mut Map<String, Value>, vision: bool) {
    let inputs = if vision {
        vec!["text", "image"]
    } else {
        vec!["text"]
    };
    base.insert("input_modalities".into(), json!(inputs));
    base.insert("output_modalities".into(), json!(["text"]));
    base.insert(
        "modalities".into(),
        json!({"input": inputs, "output": ["text"]}),
    );
    base.insert(
        "architecture".into(),
        json!({
            "input_modalities": inputs,
            "output_modalities": ["text"],
            "modality": if vision { "text+image->text" } else { "text->text" }
        }),
    );
}

fn apply_limits_and_reasoning(base: &mut Map<String, Value>, meta: &Map<String, Value>) {
    if let Some(ctx) = meta.get("maxInputTokens").and_then(Value::as_u64) {
        base.insert("context_length".into(), json!(ctx));
        base.insert("max_input_tokens".into(), json!(ctx));
    }
    if let Some(out) = meta.get("maxOutputTokens").and_then(Value::as_u64) {
        base.insert("max_output_tokens".into(), json!(out));
        base.insert("max_completion_tokens".into(), json!(out));
    }
    if let Some(windows) = meta
        .get("contextWindow")
        .and_then(|v| v.get("supportedLengths"))
        .cloned()
    {
        base.insert("context_windows".into(), windows);
    }
    if let Some(reasoning) = meta.get("reasoning").and_then(Value::as_object) {
        if let Some(efforts) = reasoning
            .get("supportedEfforts")
            .and_then(Value::as_array)
            .filter(|efforts| !efforts.is_empty())
        {
            base.insert("reasoning_efforts".into(), Value::Array(efforts.clone()));
        } else if reasoning.contains_key("supportedEfforts") {
            base.remove("reasoning_efforts");
        }
        if let Some(effort) = reasoning.get("effort") {
            if reasoning.get("supportedEfforts").is_none() {
                if base.contains_key("reasoning_efforts") {
                    base.insert("reasoning_default_effort".into(), effort.clone());
                    base.remove("reasoning_fixed_effort");
                } else {
                    base.insert("reasoning_fixed_effort".into(), effort.clone());
                }
            }
        }
        if let Some(effort) = reasoning.get("defaultEffort") {
            base.insert("reasoning_default_effort".into(), effort.clone());
        }
        if let Some(can_disable) = reasoning.get("canDisableThinking") {
            base.insert("reasoning_can_disable".into(), can_disable.clone());
        }
    }
    if let Some(always) = meta.get("onlyReasoning").and_then(Value::as_bool) {
        base.insert("always_reasoning".into(), json!(always));
    }
}

fn merge_catalog(
    realm: &str,
    primary: &[String],
    meta: &Map<String, Value>,
    extras: bool,
    _config: &Config,
) -> Vec<Value> {
    let mut merged: HashMap<String, Value> = HashMap::new();
    for item in static_entries(realm) {
        if let Some(id) = item.get("id").and_then(Value::as_str) {
            merged.insert(id.to_string(), item);
        }
    }
    for id in primary {
        let remote = meta.get(id).and_then(Value::as_object);
        match merged.get_mut(id).and_then(Value::as_object_mut) {
            Some(base) => {
                if let Some(remote) = remote {
                    apply_remote_meta(base, remote);
                }
            }
            None if extras => {
                let Some(remote) = remote else {
                    continue;
                };
                let credits = remote.get("credits").and_then(Value::as_str).unwrap_or("");
                if credits.is_empty() {
                    continue;
                }
                let mut item = Map::new();
                item.insert("id".into(), json!(id));
                item.insert("object".into(), json!("model"));
                item.insert("owned_by".into(), json!("workbuddy"));
                apply_remote_meta(&mut item, remote);
                merged.insert(id.clone(), Value::Object(item));
            }
            None => {}
        }
    }
    let mut out: Vec<Value> = static_entries(realm)
        .into_iter()
        .filter_map(|item| {
            let id = item.get("id").and_then(Value::as_str)?.to_string();
            merged.remove(&id)
        })
        .collect();
    out.extend(primary.iter().filter_map(|id| merged.remove(id)));
    out
}

fn persist_remote_catalog(realm: &str, payload: &Value, dir: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dir)?;
    crate::state::write_json_atomic(&dir.join(format!("product-config-{realm}.json")), payload)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config() -> Config {
        Config {
            timeout: Duration::from_secs(10),
            cooldown: 30.0,
            cache_dir: std::env::temp_dir(),
            remote_path: "/v3/config".into(),
            virtual_aliases: [
                "default-model",
                "fast-model",
                "balanced-model",
                "primary-model",
                "deep-model",
                "auto",
                "lite",
            ]
            .into_iter()
            .map(str::to_string)
            .collect(),
            non_chat_prefixes: vec!["codewise-".into(), "completion-".into()],
            non_chat_suffixes: vec![
                "-image-alpha".into(),
                "-image-alpha-edit".into(),
                "-taco-completion".into(),
            ],
            variant_suffixes: vec!["-sg".into(), "-x".into()],
        }
    }

    fn live_glm_entry() -> Value {
        let live_meta = json!({
            "glm-5.3": {
                "id": "glm-5.3",
                "name": "GLM live",
                "descriptionEn": "live desc",
                "credits": "x0.77",
                "supportsImages": false,
                "supportsToolCall": true,
                "supportsReasoning": true,
                "onlyReasoning": false,
                "maxInputTokens": 123456,
                "maxOutputTokens": 23456,
                "reasoning": {
                    "supportedEfforts": ["low", "high"],
                    "defaultEffort": "high",
                    "canDisableThinking": false
                },
                "contextWindow": {"supportedLengths": [64000, 123456]},
                "vendor": "f",
                "temperature": 0.7,
                "top_p": 0.9,
                "tags": ["badge:x", "craft", "live"]
            }
        });
        let live_meta = live_meta.as_object().unwrap();
        merge_catalog("cn", &["glm-5.3".to_string()], live_meta, true, &config())
            .into_iter()
            .find(|item| item.get("id").and_then(Value::as_str) == Some("glm-5.3"))
            .unwrap()
    }

    #[test]
    fn parse_and_curate_match_python_cn_output() {
        let payload = json!({
            "code": 0,
            "data": {"agents": [{
                "name": "cli",
                "models": [
                    "glm-5.3", "hy3", "default-model",
                    "deepseek-v4.1-flash-sg", "kimi-k3", "kimi-k3-1"
                ]
            }]}
        });
        let (ids, meta) = parse_remote_catalog(&payload).unwrap();
        let c = config();
        assert_eq!(
            curate_remote_catalog("cn", &ids, &meta, &c),
            vec!["glm-5.3", "hy3", "kimi-k3", "kimi-k3-1"]
        );
        let merged = merge_catalog(
            "cn",
            &curate_remote_catalog("cn", &ids, &meta, &c),
            &meta,
            true,
            &c,
        );
        assert_eq!(
            merged
                .iter()
                .filter_map(|m| m.get("id").and_then(Value::as_str))
                .collect::<Vec<_>>(),
            vec![
                "hy4-preview-f",
                "hy3",
                "deepseek-v4.1-flash",
                "glm-5.3",
                "glm-5.3-flash",
                "glm-5.2",
                "glm-5.1",
                "glm-5v-turbo",
                "minimax-m3",
                "kimi-k3-1",
                "kimi-k2.8-preview",
                "kimi-k2.7",
                "kimi-k2.6",
                "deepseek-v4-pro",
            ]
        );
        let entry = live_glm_entry();
        // Expected values were taken from Python model_entry(merge_catalog(...)).
        assert_eq!(entry.pointer("/capabilities/vision"), Some(&json!(false)));
        assert_eq!(entry.get("context_length"), Some(&json!(123456)));
        assert_eq!(
            entry.get("reasoning_efforts"),
            Some(&json!(["low", "high"]))
        );
        assert_eq!(entry.get("tags"), Some(&json!(["craft", "live"])));
        assert_eq!(entry.get("always_reasoning"), Some(&json!(false)));
    }

    #[test]
    fn persist_keeps_the_successful_raw_payload_in_an_isolated_dir() {
        let dir =
            std::env::temp_dir().join(format!("wb-rust-catalog-test-{}", crate::sse::hex_token(8)));
        let payload = json!({"code": 0, "data": {"agents": []}});
        persist_remote_catalog("cn", &payload, &dir).unwrap();
        let saved: Value =
            serde_json::from_slice(&std::fs::read(dir.join("product-config-cn.json")).unwrap())
                .unwrap();
        assert_eq!(saved, payload);
        let _ = std::fs::remove_dir_all(dir);
    }
}
