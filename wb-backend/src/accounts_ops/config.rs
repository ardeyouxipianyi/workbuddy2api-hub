//! Account-route configuration.
//!
//! Every external origin and timeout has an environment override. The defaults
//! match `wb_accounts.py`; deployments can set, for example,
//! `WB_INTL_WEB_ORIGIN=https://staging.example` or `WB_ACCOUNT_LOGIN_TTL=600`.

use std::time::Duration;

fn env_string(name: &str, default: &str) -> String {
    std::env::var(name)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| default.to_string())
}

fn env_u64(name: &str, default: u64) -> u64 {
    std::env::var(name)
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(default)
}

pub(crate) fn timeout(name: &str, default_secs: u64) -> Duration {
    Duration::from_secs(env_u64(name, default_secs).max(1))
}

pub(crate) fn login_ttl_secs() -> f64 {
    env_u64("WB_ACCOUNT_LOGIN_TTL_SECONDS", 600).max(1) as f64
}

pub(crate) fn chat_base(realm: &str) -> String {
    crate::upstream::chat_upstream_base(realm, "cli")
}

pub(crate) fn origin(realm: &str) -> String {
    if realm == "cn" {
        env_string("WB_CN_ORIGIN", "https://www.codebuddy.cn")
    } else {
        env_string("WB_INTL_ORIGIN", "https://www.workbuddy.ai")
    }
}

pub(crate) fn domain(realm: &str) -> String {
    if realm == "cn" {
        env_string("WB_CN_DOMAIN", "copilot.tencent.com")
    } else {
        env_string("WB_INTL_DOMAIN", "www.workbuddy.ai")
    }
}

pub(crate) fn billing_user_agent(realm: &str) -> String {
    format!("WorkBuddy/{}", crate::identity::desktop_version(realm))
}

pub(crate) fn web_origin(realm: &str) -> String {
    if realm == "cn" {
        env_string("WB_CN_WEB_ORIGIN", "https://www.workbuddy.cn")
    } else {
        env_string("WB_INTL_WEB_ORIGIN", "https://www.workbuddy.ai")
    }
}

pub(crate) fn web_user_agent() -> String {
    env_string(
        "WB_WEB_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 \
         (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0",
    )
}

pub(crate) fn default_model() -> String {
    env_string("WB_DAILY_CHAT_MODEL", "deepseek-v4.1-flash")
}

pub(crate) fn daily_prompt() -> String {
    env_string("WB_DAILY_CHAT_WEB_PROMPT", "Hi")
}

pub(crate) fn web_default_cwd() -> String {
    env_string("WB_WEB_DEFAULT_CWD", "/workspace")
}
