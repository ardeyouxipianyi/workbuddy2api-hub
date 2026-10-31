//! 出站身份头族（`specs/identity-upstream.md` §1.2/§1.3）。
//!
//! 上游对「像不像官方客户端」有渠道校验：真上游在**小请求**上放行，但 Codex 那种带
//! 一堆 tools 的大 payload 会直接被 `11-128 "Illegal API invocation from an unapproved
//! channel"` 拦掉。所以这一层不是装饰，缺了就不可用。
//!
//! 只实现桌面端（`product=desktop`，我们账号池里的全部是它）；VSCode/CLI 的头族在
//! spec §1.4/§1.5，真要用时照表加。头顺序 = 官方 dict 插入序，上游不校验顺序，但按
//! spec 抄能省掉以后对拍的麻烦。

use serde_json::{json, Value};

/// `_VERSION_DEFAULTS`（`wb_identity.py:189-198`）
const DESKTOP_INTL: &str = "5.8.4";
const DESKTOP_CN: &str = "5.8.3";
const DESKTOP_CLI: &str = "2.161.0";
const CLI_VERSION: &str = "2.168.2";

pub(crate) fn desktop_version(realm: &str) -> &'static str {
    if realm == "cn" { DESKTOP_CN } else { DESKTOP_INTL }
}

/// 桌面 UA：cn 次段大写（`WorkBuddy/`），intl 次段小写（`workbuddy-ai/`）。
fn desktop_ua(realm: &str) -> String {
    let v = desktop_version(realm);
    if realm == "cn" {
        format!("WorkBuddy/{v} WorkBuddy/{v} CLI/{DESKTOP_CLI}")
    } else {
        format!("WorkBuddy/{v} workbuddy-ai/{v} CLI/{DESKTOP_CLI}")
    }
}

/// `Origin` 与 `X-Domain` 不同源：Origin 是门户域名，X-Domain 是 chat 端点主机。
fn origin(realm: &str) -> &'static str {
    if realm == "cn" { "https://www.codebuddy.cn" } else { "https://www.workbuddy.ai" }
}

fn domain(realm: &str) -> &'static str {
    if realm == "cn" { "copilot.tencent.com" } else { "www.workbuddy.ai" }
}

/// `conversation_id_for(uid, session_key)`（`wb_identity.py:342-362`）：
/// sha256 前 32 hex，改第 12 位为 `4`、第 16 位为 `a`，再插连字符。
fn conversation_id_for(uid: &str, session_key: &str) -> String {
    use sha2::{Digest, Sha256};
    if session_key.is_empty() {
        return String::new();
    }
    let digest = format!("{:x}", Sha256::digest(format!("{uid}|{session_key}").as_bytes()));
    let mut h: Vec<char> = digest.chars().take(32).collect();
    h[12] = '4';
    h[16] = 'a';
    let s: String = h.into_iter().collect();
    format!("{}-{}-{}-{}-{}", &s[0..8], &s[8..12], &s[12..16], &s[16..20], &s[20..32])
}

pub struct Ctx<'a> {
    pub realm: &'a str,
    pub uid: &'a str,
    pub token: &'a str,
    pub enterprise_id: Option<&'a str>,
    pub department: Option<&'a str>,
    /// 客户端给的会话键（`X-Conversation-Id`/`payload.session_id`…），没有就每请求新生成。
    pub session_key: Option<&'a str>,
}

/// 桌面端 chat 头族，按官方插入序。
pub fn chat_headers(ctx: &Ctx<'_>) -> Vec<(String, String)> {
    let request_id = crate::sse::hex_token(32);
    let message_id = crate::sse::hex_token(32);
    let conversation = match ctx.session_key.filter(|s| !s.is_empty()) {
        Some(key) => conversation_id_for(ctx.uid, key),
        None => String::new(),
    };
    let conversation = if conversation.is_empty() {
        crate::sse::hex_token(32)
    } else {
        conversation
    };
    let trace = crate::sse::hex_token(32);
    let span = crate::sse::hex_token(16);
    let mut h: Vec<(String, String)> = vec![
        ("Content-Type".into(), "application/json".into()),
        ("Accept".into(), "application/json, text/plain, */*".into()),
        ("X-Requested-With".into(), "XMLHttpRequest".into()),
        ("Origin".into(), origin(ctx.realm).into()),
        ("Referer".into(), format!("{}/", origin(ctx.realm))),
        ("X-CodeBuddy-Request".into(), "1".into()),
        (
            "Accept-Language".into(),
            if ctx.realm == "cn" { "zh-CN" } else { "en-US" }.into(),
        ),
        ("X-Agent-Intent".into(), "craft".into()),
        ("X-Agent-Purpose".into(), "conversation".into()),
        ("X-Agent-Type".into(), "main".into()),
        ("X-IDE-Name".into(), "WorkBuddy".into()),
        ("X-IDE-Type".into(), "WorkBuddy".into()),
        ("X-IDE-Version".into(), desktop_version(ctx.realm).into()),
        ("X-Private-Data".into(), "true".into()),
        ("X-Product".into(), "SaaS".into()),
        ("X-Domain".into(), domain(ctx.realm).into()),
        ("User-Agent".into(), desktop_ua(ctx.realm)),
        (
            "X-User-Id".into(),
            if ctx.uid.is_empty() { "anonymous".into() } else { ctx.uid.into() },
        ),
        ("Authorization".into(), format!("Bearer {}", ctx.token)),
        ("X-Conversation-ID".into(), conversation),
        ("X-Conversation-Request-ID".into(), request_id.clone()),
        ("X-Root-Request-ID".into(), request_id),
        ("X-Conversation-Message-ID".into(), message_id.clone()),
        ("X-Request-ID".into(), message_id),
        ("X-Trace-ID".into(), trace.clone()),
        ("traceparent".into(), format!("00-{trace}-{span}-01")),
        ("b3".into(), format!("{trace}-{span}-1")),
        ("X-B3-TraceId".into(), trace),
        ("X-B3-SpanId".into(), span),
        ("X-B3-Sampled".into(), "1".into()),
    ];
    // 令牌本身是 API key 形态时官方两个都发（`wb_identity.py:625-626`）。
    if ctx.token.starts_with("ck_") || ctx.token.starts_with("pt_") {
        h.push(("X-API-Key".into(), ctx.token.to_string()));
    }
    if let Some(enterprise) = ctx.enterprise_id.filter(|s| !s.is_empty()) {
        h.push(("X-Enterprise-Id".into(), enterprise.to_string()));
        h.push(("X-Tenant-Id".into(), enterprise.to_string()));
    }
    if let Some(department) = ctx.department.filter(|s| !s.is_empty()) {
        h.push(("X-Department-Info".into(), department.to_string()));
    }
    h
}

/// 账号文件里这几个字段可能叫不同名字（面板/官方客户端各写一份）。
pub fn enterprise_of(account: &Value) -> Option<&str> {
    for key in ["enterpriseId", "enterprise_id", "tenantId", "tenant_id"] {
        if let Some(v) = account.get(key).and_then(Value::as_str) {
            if !v.is_empty() {
                return Some(v);
            }
        }
    }
    None
}

pub fn department_of(account: &Value) -> Option<&str> {
    for key in ["department", "departmentInfo", "department_info"] {
        if let Some(v) = account.get(key).and_then(Value::as_str) {
            if !v.is_empty() {
                return Some(v);
            }
        }
    }
    None
}

#[allow(dead_code)]
fn _unused() -> Value {
    json!({})
}
