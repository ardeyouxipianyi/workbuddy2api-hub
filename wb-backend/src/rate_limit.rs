//! 429 的分类与冷却阶梯，对着 `wb_proxy.py:14141` 与 `wb_accounts.py:723` 搬过来。
//!
//! 三种形状各走各的路：
//!   * 账号级软限流（换号能救）→ 600s 起，逐次翻倍，2h 封顶；
//!   * 没带时钟、但认得是限流措辞 → 只关这个账号的这个模型，60s 起翻倍，10min 封顶；
//!   * 带重置时钟 / code 6004 → 额度窗口，交给 quota 探针，模型级冷却取到点时刻。
//! 之前一律「关整个账号 60s」：既关不住的账号级限流（1 分钟后又撞），也把只因某个
//! 模型被限的账号整条拔掉。

use serde_json::Value;

const SOFT_BASE: f64 = 600.0;
const SOFT_MAX: f64 = 7_200.0;
const UNSCOPED_BASE: f64 = 60.0;
const UNSCOPED_MAX: f64 = 600.0;
const SOFT_LIMIT_CODES: [i64; 1] = [14_003];
const SOFT_LIMIT_MARKERS: [&str; 3] = ["usage exceeds frequency limit", "too many requests", "请求过于频繁"];
/// 上游「这个模型的窗口额度用满」的应答码（唯一带重置墙钟的额度判定）。
const CAP_LIMIT_CODE: i64 = 6_004;

const ACCOUNT_SCOPE_PHRASES: [&str; 24] = [
    "account-level", "account level", "account-wide", "account scope", "account quota",
    "account rate limit", "account limit", "per account", "per-account", "this account",
    "your account", "the account", "an account", "credential-level", "credential level",
    "credential scope", "credential limit", "per credential", "this credential",
    "the credential", "your credential", "账号级", "账户级", "该账号",
];

fn exponential_backoff(count: i64, base: f64, cap: f64) -> f64 {
    let step = (count - 1).clamp(0, 20) as i32;
    (base * 2f64.powi(step)).min(cap)
}

/// 账号级软限流的冷却时长：第 n 次连续撞线 = 600s * 2^(n-1)，2h 封顶。
pub fn soft_backoff(streak: i64) -> f64 {
    exponential_backoff(streak, SOFT_BASE, SOFT_MAX)
}

/// 没定域软限流的冷却时长：第 n 次连续撞线 = 60s * 2^(n-1)，10min 封顶。
pub fn unscoped_backoff(streak: i64) -> f64 {
    exponential_backoff(streak, UNSCOPED_BASE, UNSCOPED_MAX)
}

/// 429 body 里的 `code` 字段（不是 JSON 就没有）。
pub fn code(detail: &str) -> Option<i64> {
    let value: Value = serde_json::from_str(detail).ok()?;
    let raw = value.get("code")?;
    raw.as_i64().or_else(|| raw.as_str().and_then(|text| text.trim().parse().ok()))
}

/// 额度用尽（code 6004）：面板上「达到限额」的唯一标志。
pub fn is_cap(detail: &str) -> bool {
    code(detail) == Some(CAP_LIMIT_CODE)
}

/// 认得出来的限流措辞（含 intl 的 14003）：只说明是限流，不说明范围。
pub fn is_soft_shape(detail: &str) -> bool {
    let lowered = detail.to_lowercase();
    if SOFT_LIMIT_MARKERS.iter().any(|marker| lowered.contains(marker)) {
        return true;
    }
    code(detail).is_some_and(|code| SOFT_LIMIT_CODES.contains(&code))
}

/// body 里点名的范围措辞。ASCII 短语按词边界匹配（`accountId` 不算说范围），
/// 中文短语直接子串匹配（标识符里不会出现）。
pub fn credential_phrase(detail: &str) -> Option<&'static str> {
    let lowered = detail.to_lowercase();
    for phrase in ACCOUNT_SCOPE_PHRASES {
        let hit = if phrase.is_ascii() {
            let mut from = 0;
            let mut found = false;
            while let Some(at) = lowered[from..].find(phrase) {
                let start = from + at;
                let end = start + phrase.len();
                let before = lowered[..start].chars().next_back();
                let after = lowered[end..].chars().next();
                let bounded = |ch: Option<char>| ch.map(|c| !c.is_alphanumeric() && c != '_').unwrap_or(true);
                if bounded(before) && bounded(after) {
                    found = true;
                    break;
                }
                from = end;
            }
            found
        } else {
            lowered.contains(phrase)
        };
        if hit {
            return Some(phrase);
        }
    }
    None
}

/// 只有「带范围措辞、且没带重置时钟」才算账号级。时钟在的时候限的是模型窗口。
pub fn is_account_level(detail: &str, reset_at: Option<f64>) -> bool {
    if reset_at.is_some() {
        return false;
    }
    credential_phrase(detail).is_some()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_ladders_start_low_and_cap() {
        // 与 Python 的 _exponential_backoff(count, base, cap, 1) 同值。
        assert_eq!(soft_backoff(1), 600.0);
        assert_eq!(soft_backoff(2), 1200.0);
        assert_eq!(soft_backoff(4), 4800.0);
        assert_eq!(soft_backoff(9), 7200.0);
        assert_eq!(unscoped_backoff(1), 60.0);
        assert_eq!(unscoped_backoff(2), 120.0);
        assert_eq!(unscoped_backoff(8), 600.0);
    }

    #[test]
    fn scope_needs_the_body_to_say_account_level() {
        // accountId 这类字段名不能当成「账号级」的证据。
        assert!(!is_account_level(r#"{"code":6004,"accountId":"x"}"#, None));
        assert!(is_account_level(r#"{"message":"account-level rate limit"}"#, None));
        assert!(is_account_level(r#"{"message":"账号级限流"}"#, None));
        // 带重置时钟的 429 是模型窗口，不是账号级。
        assert!(!is_account_level(r#"{"message":"account rate limit"}"#, Some(1.0)));
        assert!(is_cap(r#"{"code":6004}"#));
        assert!(!is_cap(r#"{"code":"14003"}"#));
        assert!(is_soft_shape(r#"{"code":14003}"#));
        assert!(is_soft_shape("usage exceeds frequency limit"));
    }
}
