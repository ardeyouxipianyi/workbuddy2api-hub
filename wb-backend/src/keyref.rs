//! Pinned-account routes: `/u/<ref>/v1/...`.
//!
//! A ref resolves in the same order as `wb_accounts.AccountPool.find_unique`:
//! exact uid, unique nickname, then unique uid prefix. The rewritten request
//! carries the pin in a private request extension so a later request on the
//! same connection cannot inherit it.

use hyper::body::Incoming;
use hyper::http::uri::{PathAndQuery, Uri};
use hyper::{Request, StatusCode};
use serde_json::Value;

use crate::http::{error_resp, Resp};
use crate::pool::Candidate;
use crate::state::AccountFile;
use crate::App;

#[derive(Clone, Debug)]
pub struct PinnedAccount {
    pub reference: String,
    pub uid: String,
}

#[derive(Debug, PartialEq, Eq)]
pub enum ResolveError {
    NotFound,
    Ambiguous,
}

/// Rewrite a pinned path and attach its account pin for the rest of this request.
pub fn prepare(app: &App, req: &mut Request<Incoming>) -> Result<(), Resp> {
    let path = req.uri().path();
    if !path.starts_with("/u/") {
        return Ok(());
    }
    let parsed = parse_ref(path).ok_or_else(|| {
        error_resp(
            StatusCode::NOT_FOUND,
            "pinned account route must be /u/<ref>/v1/...",
        )
    })?;
    let account = resolve(&app.state.accounts_snapshot(), &parsed.reference).map_err(|why| {
        let status = if why == ResolveError::Ambiguous {
            StatusCode::CONFLICT
        } else {
            StatusCode::NOT_FOUND
        };
        let reason = if why == ResolveError::Ambiguous {
            "is ambiguous"
        } else {
            "was not found"
        };
        error_resp(
            status,
            &format!("pinned account '{}' {reason}", parsed.reference),
        )
    })?;
    let tail = &path[3 + parsed.reference_raw_len + 1..];
    let target = format!("/{tail}");
    let path_and_query = match req.uri().query() {
        Some(query) if !query.is_empty() => format!("{target}?{query}"),
        _ => target,
    };
    let uri = Uri::builder()
        .path_and_query(
            PathAndQuery::try_from(path_and_query)
                .map_err(|_| error_resp(StatusCode::NOT_FOUND, "not found"))?,
        )
        .build()
        .map_err(|_| error_resp(StatusCode::NOT_FOUND, "not found"))?;
    *req.uri_mut() = uri;
    req.extensions_mut().insert(PinnedAccount {
        reference: parsed.reference,
        uid: account.uid(),
    });
    Ok(())
}

pub fn pinned(req: &Request<Incoming>) -> Option<PinnedAccount> {
    req.extensions().get::<PinnedAccount>().cloned()
}

fn parse_ref(path: &str) -> Option<ParsedRef> {
    let rest = path.strip_prefix("/u/")?;
    let (encoded_ref, tail) = rest.split_once('/')?;
    if encoded_ref.is_empty() || tail != "v1" && !tail.starts_with("v1/") {
        return None;
    }
    let reference = percent_decode(encoded_ref)?;
    if reference.trim().is_empty() || reference.contains('/') {
        return None;
    }
    Some(ParsedRef {
        reference: reference.trim().to_string(),
        reference_raw_len: encoded_ref.len(),
    })
}

fn percent_decode(text: &str) -> Option<String> {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%' {
            let high = hex(bytes.get(index + 1).copied()?)?;
            let low = hex(bytes.get(index + 2).copied()?)?;
            out.push((high << 4) | low);
            index += 3;
        } else {
            out.push(bytes[index]);
            index += 1;
        }
    }
    Some(String::from_utf8_lossy(&out).into_owned())
}

fn hex(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

pub fn resolve(accounts: &[AccountFile], reference: &str) -> Result<AccountFile, ResolveError> {
    let reference = reference.trim();
    if reference.is_empty() {
        return Err(ResolveError::NotFound);
    }
    let exact: Vec<&AccountFile> = accounts
        .iter()
        .filter(|account| account.uid() == reference)
        .collect();
    if let Some(account) = only(&exact)? {
        return Ok(account.clone());
    }

    let named: Vec<&AccountFile> = accounts
        .iter()
        .filter(|account| account.str("nickname").trim() == reference)
        .collect();
    if let Some(account) = only(&named)? {
        return Ok(account.clone());
    }

    let prefixed: Vec<&AccountFile> = accounts
        .iter()
        .filter(|account| account.uid().starts_with(reference))
        .collect();
    only(&prefixed)?.cloned().ok_or(ResolveError::NotFound)
}

fn only<'a>(matches: &[&'a AccountFile]) -> Result<Option<&'a AccountFile>, ResolveError> {
    match matches {
        [] => Ok(None),
        [account] => Ok(Some(*account)),
        _ => Err(ResolveError::Ambiguous),
    }
}

pub enum CandidateError {
    Realm { actual: String, requested: String },
    Unavailable(String),
}

/// Validate and copy the pinned account into the normal upstream candidate shape.
pub fn candidate(
    app: &App,
    pin: &PinnedAccount,
    realm: &str,
    model: &str,
) -> Result<Candidate, CandidateError> {
    let account = app
        .state
        .accounts_snapshot()
        .into_iter()
        .find(|account| account.uid() == pin.uid)
        .ok_or_else(|| CandidateError::Unavailable("was not found".into()))?;
    let actual = account.realm();
    if actual != realm {
        return Err(CandidateError::Realm {
            actual,
            requested: realm.to_string(),
        });
    }
    let now = crate::state::now_epoch();
    if !account.ready(now) {
        return Err(CandidateError::Unavailable(if !account.enabled() {
            "is disabled".into()
        } else if account.access_token().is_empty() {
            "has no access token".into()
        } else {
            "token is expired".into()
        }));
    }
    if account.restricted_until() > now as f64 {
        return Err(CandidateError::Unavailable(
            "is restricted by upstream".into(),
        ));
    }
    if app.pool.cooling(&account.uid()) {
        return Err(CandidateError::Unavailable("is cooling down".into()));
    }
    if model_disabled(&account, model) {
        return Err(CandidateError::Unavailable(format!(
            "is disabled for model {model}"
        )));
    }
    Ok(Candidate {
        uid: account.uid(),
        token: account.access_token(),
        realm: actual,
        domain: account.domain(),
        product: account.product(),
        expires_at: account.expires_at(),
        is_static_key: account.is_api_key(),
    })
}

pub fn candidate_error(pin: &PinnedAccount, error: CandidateError) -> Resp {
    let (status, detail) = match error {
        CandidateError::Realm { actual, requested } => (
            StatusCode::CONFLICT,
            format!("belongs to realm {actual}, request targets {requested}"),
        ),
        CandidateError::Unavailable(detail) => (StatusCode::SERVICE_UNAVAILABLE, detail),
    };
    error_resp(
        status,
        &format!("pinned account '{}' {detail}", pin.reference),
    )
}

pub fn disabled_models(account: &AccountFile) -> Vec<String> {
    account
        .data
        .get("disabledModels")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

fn model_disabled(account: &AccountFile, model: &str) -> bool {
    !model.is_empty()
        && account
            .data
            .get("disabledModels")
            .and_then(Value::as_array)
            .map(|items| items.iter().any(|item| item.as_str() == Some(model)))
            .unwrap_or(false)
}

struct ParsedRef {
    reference: String,
    reference_raw_len: usize,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::path::PathBuf;

    fn account(uid: &str, nickname: &str) -> AccountFile {
        AccountFile {
            path: PathBuf::from(format!("{uid}.json")),
            name: format!("{uid}.json"),
            data: json!({
                "uid": uid,
                "nickname": nickname,
                "accessToken": "token",
                "realm": "intl",
                "enabled": true,
            })
            .as_object()
            .unwrap()
            .clone(),
        }
    }

    #[test]
    fn resolves_exact_nickname_and_unique_prefix() {
        let accounts = vec![
            account("abcdef123456", "alpha"),
            account("abcdef999999", "beta"),
        ];
        assert_eq!(
            resolve(&accounts, "abcdef123456").unwrap().uid(),
            "abcdef123456"
        );
        assert_eq!(resolve(&accounts, "alpha").unwrap().uid(), "abcdef123456");
        assert_eq!(
            resolve(&accounts, "abcdef123").unwrap().uid(),
            "abcdef123456"
        );
    }

    #[test]
    fn ambiguity_and_missing_ref_are_distinct_404s() {
        let accounts = vec![
            account("abcdef123456", "same"),
            account("abcdef999999", "same"),
        ];
        assert_resolve_error(&accounts, "abcdef", ResolveError::Ambiguous);
        assert_resolve_error(&accounts, "same", ResolveError::Ambiguous);
        assert_resolve_error(&accounts, "nope", ResolveError::NotFound);
    }

    #[test]
    fn parser_accepts_only_v1_paths_and_preserves_decoded_ref() {
        let parsed = parse_ref("/u/abc%2D123/v1/models").unwrap();
        assert_eq!(parsed.reference, "abc-123");
        assert!(parse_ref("/u/abc/not-v1/models").is_none());
        assert!(parse_ref("/u//v1/models").is_none());
    }

    fn assert_resolve_error(accounts: &[AccountFile], reference: &str, expected: ResolveError) {
        match resolve(accounts, reference) {
            Err(actual) => assert_eq!(actual, expected),
            Ok(_) => panic!("expected resolution to fail"),
        }
    }
}
