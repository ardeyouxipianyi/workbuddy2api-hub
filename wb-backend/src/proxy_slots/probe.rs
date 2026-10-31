//! Proxy discovery and exit/geo probes.
//!
//! Runtime configuration uses the Python backend's environment names:
//! `WB_PROXY_DISCOVER_HOST` (default `cli-proxy-mihomo`),
//! `WB_PROXY_DISCOVER_PORTS` (default `17901-17910`),
//! `WB_PROXY_DISCOVER_MAX_PORTS` (default `64`),
//! `WB_PROXY_PROBE_URL` (default `https://api.ipify.org`),
//! `WB_PROXY_GEO_URL` (default `http://ip-api.com/json`),
//! `WB_PROXY_PROBE_TIMEOUT_SECS` (default `12`),
//! `WB_PROXY_GEO_TIMEOUT_SECS` (default `8`), and
//! `WB_PROXY_DISCOVER_SAMPLES` (unset by default; a JSON array for deterministic
//! deployments/tests, e.g. `[{"url":"http://proxy:17901","reachable":true}]`).

use std::time::{Duration, Instant};

use hyper::StatusCode;
use serde_json::{json, Value};

use crate::http::{json_resp, Resp};
use crate::App;

pub struct Probe {
    pub ok: bool,
    pub exit_ip: String,
    pub latency_ms: u64,
    pub error: String,
    pub intel_ok: bool,
    pub country: String,
    pub country_code: String,
    pub ip_type: String,
    pub isp: String,
    pub asn: String,
}

pub async fn discover(app: &App) -> Resp {
    if let Some(samples) = discover_samples() {
        return json_resp(StatusCode::OK, &json!({"candidates": samples}));
    }
    let config = DiscoverConfig::from_env();
    let urls = candidate_urls(&config);
    let mut candidates = Vec::with_capacity(urls.len());
    for url in urls {
        let outcome = probe(app, &url).await;
        candidates.push(candidate_json(&url, &outcome));
    }
    json_resp(StatusCode::OK, &json!({"candidates": candidates}))
}

fn discover_samples() -> Option<Vec<Value>> {
    let raw = std::env::var("WB_PROXY_DISCOVER_SAMPLES").ok()?;
    match serde_json::from_str::<Value>(&raw).ok()? {
        Value::Array(items) => Some(items),
        Value::Object(mut object) => object
            .remove("candidates")
            .and_then(|value| value.as_array().cloned()),
        _ => None,
    }
}

fn candidate_urls(config: &DiscoverConfig) -> Vec<String> {
    config
        .ports
        .iter()
        .map(|port| format!("http://{}:{port}", config.host))
        .collect()
}

fn candidate_json(url: &str, outcome: &Probe) -> Value {
    let mut value = probe_json(outcome);
    value["url"] = json!(url);
    value["reachable"] = json!(outcome.ok);
    value
}

pub fn probe_json(outcome: &Probe) -> Value {
    json!({
        "ok": outcome.ok,
        "exit_ip": outcome.exit_ip,
        "latency_ms": outcome.latency_ms,
        "error": outcome.error,
        "intel_ok": outcome.intel_ok,
        "country": outcome.country,
        "country_code": outcome.country_code,
        "ip_type": outcome.ip_type,
        "isp": outcome.isp,
        "asn": outcome.asn,
    })
}

pub async fn probe(app: &App, proxy_url: &str) -> Probe {
    let config = DiscoverConfig::from_env();
    let started = Instant::now();
    let exit = async {
        let proxy = reqwest::Proxy::all(proxy_url)
            .map_err(|error| format!("invalid proxy url: {error}"))?;
        let client = reqwest::Client::builder()
            .proxy(proxy)
            .timeout(config.probe_timeout)
            .build()
            .map_err(|error| error.to_string())?;
        let response = client
            .get(&config.probe_url)
            .send()
            .await
            .map_err(|error| error.to_string())?;
        response.text().await.map_err(|error| error.to_string())
    }
    .await;
    let latency_ms = started.elapsed().as_millis().min(u64::MAX as u128) as u64;
    let (exit_ip, error) = match exit {
        Ok(text) => (text.trim().to_string(), String::new()),
        Err(error) => (String::new(), error.chars().take(160).collect()),
    };
    let intel = if exit_ip.is_empty() {
        empty_intel()
    } else {
        geo_lookup(app, &config, &exit_ip).await
    };
    let intel_ok = [
        &intel.country,
        &intel.country_code,
        &intel.ip_type,
        &intel.isp,
        &intel.asn,
    ]
    .iter()
    .any(|value| !value.is_empty());
    Probe {
        ok: error.is_empty(),
        exit_ip,
        latency_ms,
        error,
        intel_ok,
        country: intel.country,
        country_code: intel.country_code,
        ip_type: intel.ip_type,
        isp: intel.isp,
        asn: intel.asn,
    }
}

struct DiscoverConfig {
    host: String,
    ports: Vec<u16>,
    probe_url: String,
    geo_url: String,
    probe_timeout: Duration,
    geo_timeout: Duration,
}

impl DiscoverConfig {
    fn from_env() -> Self {
        let raw_ports = env_text("WB_PROXY_DISCOVER_PORTS", "17901-17910");
        let maximum = env_number("WB_PROXY_DISCOVER_MAX_PORTS", 64);
        let mut ports = parse_ports(&raw_ports, maximum);
        if ports.is_empty() {
            ports = parse_ports("17901-17910", maximum);
        }
        Self {
            host: env_text("WB_PROXY_DISCOVER_HOST", "cli-proxy-mihomo"),
            ports,
            probe_url: env_text("WB_PROXY_PROBE_URL", "https://api.ipify.org"),
            geo_url: env_text("WB_PROXY_GEO_URL", "http://ip-api.com/json"),
            probe_timeout: Duration::from_secs(env_number("WB_PROXY_PROBE_TIMEOUT_SECS", 12)),
            geo_timeout: Duration::from_secs(env_number("WB_PROXY_GEO_TIMEOUT_SECS", 8)),
        }
    }
}

fn env_text(name: &str, default: &str) -> String {
    std::env::var(name)
        .ok()
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| default.to_string())
}

fn env_number(name: &str, default: u64) -> u64 {
    std::env::var(name)
        .ok()
        .and_then(|value| value.parse().ok())
        .filter(|value| *value > 0)
        .unwrap_or(default)
}

fn parse_ports(raw: &str, maximum: u64) -> Vec<u16> {
    let maximum = maximum.min(u16::MAX as u64) as usize;
    if let Ok(port) = raw.trim().parse::<u16>() {
        return if port > 0 { vec![port] } else { Vec::new() };
    }
    let Some((low, high)) = raw.trim().split_once('-') else {
        return Vec::new();
    };
    let (Ok(low), Ok(high)) = (low.trim().parse::<u16>(), high.trim().parse::<u16>()) else {
        return Vec::new();
    };
    if low == 0 || low > high {
        return Vec::new();
    }
    (low..=high).take(maximum).collect()
}

struct Intel {
    country: String,
    country_code: String,
    ip_type: String,
    isp: String,
    asn: String,
}

fn empty_intel() -> Intel {
    Intel {
        country: String::new(),
        country_code: String::new(),
        ip_type: String::new(),
        isp: String::new(),
        asn: String::new(),
    }
}

async fn geo_lookup(app: &App, config: &DiscoverConfig, ip: &str) -> Intel {
    let url = format!(
        "{}/{}?lang=zh-CN&fields=status,message,country,countryCode,isp,org,as,hosting,proxy,mobile",
        config.geo_url.trim_end_matches('/'),
        ip
    );
    let Ok(response) = app
        .upstream
        .get(url)
        .timeout(config.geo_timeout)
        .header("User-Agent", "wb-proxy")
        .send()
        .await
    else {
        return empty_intel();
    };
    let Ok(value) = response.json::<Value>().await else {
        return empty_intel();
    };
    if value.get("status").and_then(Value::as_str) != Some("success") {
        return empty_intel();
    }
    Intel {
        country: string_of(value.get("country")),
        country_code: string_of(value.get("countryCode")).to_uppercase(),
        ip_type: match value.get("hosting").and_then(Value::as_bool) {
            Some(true) => "datacenter".into(),
            Some(false) => "residential".into(),
            None => String::new(),
        },
        isp: {
            let isp = string_of(value.get("isp"));
            if isp.is_empty() {
                string_of(value.get("org"))
            } else {
                isp
            }
        },
        asn: string_of(value.get("as")),
    }
}

fn string_of(value: Option<&Value>) -> String {
    value
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn discover_candidate_can_be_built_from_a_fixed_sample() {
        let outcome = Probe {
            ok: true,
            exit_ip: "1.2.3.4".into(),
            latency_ms: 12,
            error: String::new(),
            intel_ok: true,
            country: "美国".into(),
            country_code: "US".into(),
            ip_type: "residential".into(),
            isp: "Example".into(),
            asn: "AS1".into(),
        };
        let candidate = candidate_json("http://proxy.test:17901", &outcome);
        assert_eq!(candidate["url"], "http://proxy.test:17901");
        assert_eq!(candidate["reachable"], true);
        assert_eq!(candidate["exit_ip"], "1.2.3.4");
    }

    #[test]
    fn port_configuration_is_bounded_and_validated() {
        assert_eq!(parse_ports("17901", 64), vec![17901]);
        assert_eq!(parse_ports("17901-17903", 2), vec![17901, 17902]);
        assert!(parse_ports("17903-17901", 64).is_empty());
    }
}
