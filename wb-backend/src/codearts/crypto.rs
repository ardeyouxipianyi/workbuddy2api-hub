//! 华为云 CodeArts 的密码学原语：P-256 ECDSA(ES256)、DPoP、PKCE、SDK-HMAC-SHA256。
//!
//! 逐项对齐 `wb_codearts_crypto.py`（Python 手写 P-256 是为了便携运行时没有
//! `cryptography`；rust 直接用 `p256`，但输出格式必须一致：JOSE raw `r||s`、低 S、
//! b64url 不带 padding）。金标准向量在 `tests/_test_codearts_crypto.py` 里，由
//! codearts2api 的 Go 实现生成，本文件单测原样复用。

use rand::RngCore;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use p256::ecdsa::signature::{Signer, Verifier};
use p256::ecdsa::{Signature, SigningKey, VerifyingKey};

pub const SIGNING_ALGORITHM: &str = "SDK-HMAC-SHA256";

fn b64url(raw: &[u8]) -> String {
    URL_SAFE_NO_PAD.encode(raw)
}

fn b64url_decode(text: &str) -> Option<Vec<u8>> {
    URL_SAFE_NO_PAD.decode(text.trim_end_matches('=')).ok()
}

fn sha256(parts: &[&[u8]]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    for part in parts {
        hasher.update(part);
    }
    hasher.finalize().into()
}

fn hex(raw: &[u8]) -> String {
    raw.iter().map(|byte| format!("{byte:02x}")).collect()
}

pub fn random_bytes(len: usize) -> Vec<u8> {
    let mut buf = vec![0u8; len];
    rand::thread_rng().fill_bytes(&mut buf);
    buf
}

/// 等价 `wb_codearts_crypto.random_hex`：`secrets.token_hex(size)`（size 是字节数）。
pub fn random_hex(size: usize) -> String {
    hex(&random_bytes(size))
}

/// DPoP 用的 P-256 密钥对。私钥只在内存/账号文件里，对外只出公钥 JWK。
#[derive(Clone)]
pub struct KeyPair {
    key: SigningKey,
}

impl KeyPair {
    pub fn generate() -> Self {
        Self { key: SigningKey::random(&mut rand::thread_rng()) }
    }

    /// `keypair_from_jwk`：kty/crv 必须是 EC/P-256，且公私钥要自洽。
    pub fn from_jwk(jwk: &serde_json::Value) -> Result<Self, String> {
        if jwk.get("kty").and_then(|v| v.as_str()) != Some("EC")
            || jwk.get("crv").and_then(|v| v.as_str()) != Some("P-256")
        {
            return Err("invalid DPoP JWK curve".into());
        }
        let field = |name: &str| -> Result<Vec<u8>, String> {
            jwk.get(name)
                .and_then(|v| v.as_str())
                .and_then(b64url_decode)
                .ok_or_else(|| format!("invalid DPoP JWK fields: {name}"))
        };
        let d = field("d")?;
        let x = field("x")?;
        let y = field("y")?;
        let key = SigningKey::from_slice(&d).map_err(|_| "invalid DPoP JWK private scalar".to_string())?;
        let public = key.verifying_key().to_encoded_point(false);
        // 公钥必须与 d 推出的点一致（对应 Python 的 mismatch 检查）。
        if public.x().map(|v| v.as_slice()) != Some(x.as_slice())
            || public.y().map(|v| v.as_slice()) != Some(y.as_slice())
        {
            return Err("DPoP JWK public/private key mismatch".into());
        }
        Ok(Self { key })
    }

    /// `jwk_from_keypair`：可持久化的私有 JWK，字段顺序与 Go/Python 一致。
    pub fn jwk(&self) -> serde_json::Value {
        let public = self.key.verifying_key().to_encoded_point(false);
        serde_json::json!({
            "kty": "EC",
            "crv": "P-256",
            "x": b64url(public.x().expect("P-256 point has x").as_slice()),
            "y": b64url(public.y().expect("P-256 point has y").as_slice()),
            "d": b64url(self.key.to_bytes().as_slice()),
        })
    }

    pub fn public_jwk(&self) -> serde_json::Value {
        let jwk = self.jwk();
        serde_json::json!({
            "kty": "EC",
            "crv": "P-256",
            "x": jwk["x"].clone(),
            "y": jwk["y"].clone(),
        })
    }

    /// ES256 签名，JOSE raw `r||s`（64 字节，低 S）。
    pub fn sign_es256(&self, message: &[u8]) -> Vec<u8> {
        let signature: Signature = self.key.sign(message);
        // ecdsa 默认可能给高 S；JOSE/Go 都要求低 S（等价于 Python 的 s > N/2 → N-s）。
        let signature = signature.normalize_s().unwrap_or(signature);
        signature.to_bytes().to_vec()
    }

    pub fn verify_es256(&self, message: &[u8], signature: &[u8]) -> bool {
        match Signature::from_slice(signature) {
            Ok(sig) => self.key.verifying_key().verify(message, &sig).is_ok(),
            Err(_) => false,
        }
    }
}

/// 只用公钥验证（`verify_es256(x, y, msg, sig)` 的等价物，测试与自检用）。
pub fn verify_with_public(x: &[u8], y: &[u8], message: &[u8], signature: &[u8]) -> bool {
    let mut point = vec![4u8];
    point.extend_from_slice(x);
    point.extend_from_slice(y);
    let Ok(key) = VerifyingKey::from_sec1_bytes(&point) else { return false };
    match Signature::from_slice(signature) {
        Ok(sig) => key.verify(message, &sig).is_ok(),
        Err(_) => false,
    }
}

fn json_compact(value: &serde_json::Value) -> String {
    // Python 用 `separators=(",", ":"), sort_keys=True`；serde_json 的 Map 默认保序，
    // 这里按 key 排序后再序列化，保证签名输入逐字节一致。
    fn sorted(value: &serde_json::Value) -> serde_json::Value {
        match value {
            serde_json::Value::Object(map) => {
                let mut out = serde_json::Map::new();
                let mut keys: Vec<&String> = map.keys().collect();
                keys.sort();
                for key in keys {
                    out.insert(key.clone(), sorted(&map[key]));
                }
                serde_json::Value::Object(out)
            }
            other => other.clone(),
        }
    }
    serde_json::to_string(&sorted(value)).unwrap_or_default()
}

/// `dpop_proof`：htm 固定 POST（DPoP 只用在 token 端点）。
pub fn dpop_proof(key: &KeyPair, htu: &str) -> String {
    let header = serde_json::json!({
        "alg": "ES256",
        "typ": "dpop+jwt",
        "jwk": key.public_jwk(),
    });
    let payload = serde_json::json!({
        "htm": "POST",
        "htu": htu,
        "iat": std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0),
        "jti": random_hex(16),
    });
    let head = b64url(json_compact(&header).as_bytes());
    let body = b64url(json_compact(&payload).as_bytes());
    let signing_input = format!("{head}.{body}");
    let signature = key.sign_es256(signing_input.as_bytes());
    format!("{signing_input}.{}", b64url(&signature))
}

/// `pkce_pair`：64 字节随机（与 Go 实现一致）。
pub fn pkce_pair() -> (String, String) {
    let verifier = b64url(&random_bytes(64));
    let challenge = b64url(&sha256(&[verifier.as_bytes()]));
    (verifier, challenge)
}

fn percent_encode(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for byte in text.as_bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(*byte as char)
            }
            other => out.push_str(&format!("%{other:02X}")),
        }
    }
    out
}

/// CanonicalURI：每段 percent-encode，末尾补 `/`（Go 的 signRequest 同款）。
fn canonical_uri(path: &str) -> String {
    let path = if path.is_empty() { "/" } else { path };
    let encoded: Vec<String> = path.split('/').map(percent_encode).collect();
    let mut out = encoded.join("/");
    if !out.ends_with('/') {
        out.push('/');
    }
    out
}

/// CanonicalQuery：按 (key, value) 排序后逐项编码，空值保留。
fn canonical_query(raw: &str) -> String {
    if raw.is_empty() {
        return String::new();
    }
    let mut pairs: Vec<(String, String)> = raw
        .split('&')
        .filter(|part| !part.is_empty())
        .map(|part| {
            let (key, value) = part.split_once('=').unwrap_or((part, ""));
            (percent_decode(key), percent_decode(value))
        })
        .collect();
    pairs.sort();
    pairs
        .iter()
        .map(|(key, value)| format!("{}={}", percent_encode(key), percent_encode(value)))
        .collect::<Vec<_>>()
        .join("&")
}

fn percent_decode(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%' && index + 2 < bytes.len() {
            let hex = std::str::from_utf8(&bytes[index + 1..index + 3]).unwrap_or("");
            if let Ok(value) = u8::from_str_radix(hex, 16) {
                out.push(value);
                index += 3;
                continue;
            }
        }
        out.push(if bytes[index] == b'+' { b' ' } else { bytes[index] });
        index += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// `sign_request`：给请求头加 `SDK-HMAC-SHA256` 签名。返回的列表含调用方原有头。
pub fn sign_request(
    method: &str,
    url: &str,
    headers: &[(String, String)],
    body: &[u8],
    access_key: &str,
    secret_key: &str,
    security_token: &str,
    x_date: &str,
) -> Result<Vec<(String, String)>, String> {
    let parsed = reqwest::Url::parse(url).map_err(|err| format!("bad url: {err}"))?;
    let host = match (parsed.host_str(), parsed.port()) {
        (Some(host), Some(port)) => format!("{host}:{port}"),
        (Some(host), None) => host.to_string(),
        _ => return Err("url has no host".into()),
    };
    let payload_hash = hex(&sha256(&[body]));

    let mut lower: BTreeMap<String, String> = BTreeMap::new();
    for (key, value) in headers {
        lower.insert(key.to_lowercase(), value.trim().to_string());
    }
    lower.insert("host".into(), host);
    lower.insert("x-sdk-date".into(), x_date.to_string());
    lower.insert("x-sdk-content-sha256".into(), payload_hash.clone());
    if !security_token.is_empty() {
        lower.insert("x-security-token".into(), security_token.to_string());
    }

    let canonical_headers: String = lower
        .iter()
        .map(|(key, value)| format!("{key}:{value}\n"))
        .collect();
    let signed_headers = lower.keys().cloned().collect::<Vec<_>>().join(";");
    let canonical_request = [
        method.to_uppercase(),
        canonical_uri(parsed.path()),
        canonical_query(parsed.query().unwrap_or("")),
        canonical_headers,
        signed_headers.clone(),
        payload_hash.clone(),
    ]
    .join("\n");
    let string_to_sign = [
        SIGNING_ALGORITHM.to_string(),
        x_date.to_string(),
        hex(&sha256(&[canonical_request.as_bytes()])),
    ]
    .join("\n");

    let signature = {
        use hmac::{Hmac, Mac};
        let mut mac = Hmac::<Sha256>::new_from_slice(secret_key.as_bytes())
            .map_err(|err| format!("bad secret: {err}"))?;
        mac.update(string_to_sign.as_bytes());
        hex(&mac.finalize().into_bytes())
    };

    let authorization = format!(
        "{SIGNING_ALGORITHM} Access={access_key}, SignedHeaders={signed_headers}, Signature={signature}"
    );

    let mut out: Vec<(String, String)> = headers
        .iter()
        .filter(|(key, _)| {
            !matches!(
                key.to_lowercase().as_str(),
                "host" | "x-sdk-date" | "x-sdk-content-sha256" | "x-security-token" | "authorization"
            )
        })
        .cloned()
        .collect();
    out.push(("Host".into(), lower["host"].clone()));
    out.push(("X-Sdk-Date".into(), lower["x-sdk-date"].clone()));
    out.push(("X-Sdk-Content-Sha256".into(), payload_hash));
    if let Some(token) = lower.get("x-security-token") {
        out.push(("X-Security-Token".into(), token.clone()));
    }
    out.push(("Authorization".into(), authorization));
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hex_bytes(text: &str) -> Vec<u8> {
        (0..text.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&text[i..i + 2], 16).unwrap())
            .collect()
    }

    #[test]
    fn es256_signs_and_verifies_low_s() {
        let key = KeyPair::generate();
        let signature = key.sign_es256(b"hello-codearts");
        assert_eq!(signature.len(), 64);
        assert!(key.verify_es256(b"hello-codearts", &signature));
        assert!(!key.verify_es256(b"hello-codeart5", &signature));
        // 低 S：再归一化不该有变化（normalize_s 返回 None 表示本来就是低 S）。
        let sig = Signature::from_slice(&signature).unwrap();
        assert!(sig.normalize_s().is_none(), "signature must already be low-S");
    }

    #[test]
    fn verifies_the_go_reference_signature() {
        // tests/_test_codearts_crypto.py 里 d=1（公钥就是 G）的 Go 签名。
        let signature = hex_bytes(
            "8de8b67b0182b0ad3e966bbf0e377da14e3d3850490e4272dfe98933d5a9bf30\
             98e4e89d233a038da948e4cc788be8592cdef4f9aa45229002455be3473322c1",
        );
        let generator = hex_bytes(
            "6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296",
        );
        let generator_y = hex_bytes(
            "4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5",
        );
        assert!(verify_with_public(&generator, &generator_y, b"hello-codearts", &signature));
    }

    #[test]
    fn jwk_round_trips_and_rejects_mismatch() {
        let key = KeyPair::generate();
        let jwk = key.jwk();
        for field in ["kty", "crv", "x", "y", "d"] {
            assert!(jwk.get(field).is_some(), "missing {field}");
        }
        assert!(!jwk["x"].as_str().unwrap().contains('='));
        let restored = KeyPair::from_jwk(&jwk).expect("round trip");
        let message = b"round-trip";
        assert!(restored.verify_es256(message, &key.sign_es256(message)));

        let mut bad = jwk.clone();
        let other = KeyPair::generate().jwk();
        bad["d"] = other["d"].clone();
        assert!(KeyPair::from_jwk(&bad).is_err(), "mismatched d must be rejected");
    }

    #[test]
    fn dpop_proof_has_the_expected_shape() {
        let key = KeyPair::generate();
        let htu = "https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens";
        let proof = dpop_proof(&key, htu);
        let parts: Vec<&str> = proof.split('.').collect();
        assert_eq!(parts.len(), 3);
        let header: serde_json::Value =
            serde_json::from_slice(&b64url_decode(parts[0]).unwrap()).unwrap();
        let payload: serde_json::Value =
            serde_json::from_slice(&b64url_decode(parts[1]).unwrap()).unwrap();
        assert_eq!(header["alg"], "ES256");
        assert_eq!(header["typ"], "dpop+jwt");
        assert!(header["jwk"].get("d").is_none(), "public JWK only");
        assert_eq!(payload["htm"], "POST");
        assert_eq!(payload["htu"], htu);
        assert!(payload["iat"].is_i64());
        assert_eq!(payload["jti"].as_str().unwrap().len(), 32);
        let signing_input = format!("{}.{}", parts[0], parts[1]);
        assert!(key.verify_es256(
            signing_input.as_bytes(),
            &b64url_decode(parts[2]).unwrap()
        ));
    }

    #[test]
    fn pkce_pair_matches_the_challenge_derivation() {
        let (verifier, challenge) = pkce_pair();
        assert_eq!(verifier.len(), 86); // 64 字节的 b64url
        assert_eq!(challenge, b64url(&sha256(&[verifier.as_bytes()])));
        assert!(!verifier.contains('=') && !challenge.contains('='));
    }

    #[test]
    fn huawei_signature_matches_the_go_vector() {
        let body = br#"{"model":"GLM-5.2","stream":true,"messages":[{"role":"user","content":"hi"}]}"#;
        let headers: Vec<(String, String)> = [
            ("Content-Type", "application/json"),
            ("Accept", "text/event-stream"),
            ("x-auth-token", "TOKEN"),
            ("x-snap-traceid", "trace123"),
            ("X-Language", "zh-cn"),
            ("app-id", "CodeAgent3.0"),
            ("is_confidential", "false"),
        ]
        .iter()
        .map(|(key, value)| (key.to_string(), value.to_string()))
        .collect();
        let signed = sign_request(
            "POST",
            "https://snap-access.cn-north-4.myhuaweicloud.com/api/v2/chat/completions",
            &headers,
            body,
            "AKID",
            "SECRETKEY",
            "TOKEN",
            "20261031T000000Z",
        )
        .unwrap();
        let get = |name: &str| {
            signed
                .iter()
                .find(|(key, _)| key == name)
                .map(|(_, value)| value.clone())
                .unwrap_or_default()
        };
        assert_eq!(
            get("Authorization"),
            "SDK-HMAC-SHA256 Access=AKID, SignedHeaders=accept;app-id;content-type;host;\
             is_confidential;x-auth-token;x-language;x-sdk-content-sha256;x-sdk-date;\
             x-security-token;x-snap-traceid, Signature=\
             28c9c49de88825070ab6fba37f9b30a44bc22f165743bb6c409338cb5b78ad09"
        );
        assert_eq!(get("Host"), "snap-access.cn-north-4.myhuaweicloud.com");
        assert_eq!(get("X-Sdk-Date"), "20261031T000000Z");
        assert_eq!(get("X-Security-Token"), "TOKEN");
        assert_eq!(get("X-Sdk-Content-Sha256"), hex(&sha256(&[body])));
        assert_eq!(get("x-snap-traceid"), "trace123");
    }

    #[test]
    fn canonical_uri_and_query_follow_the_sdk_rules() {
        assert_eq!(canonical_uri("/a b/c"), "/a%20b/c/");
        assert_eq!(canonical_uri("/"), "/");
        assert_eq!(canonical_query("b=2&a=1&a=0"), "a=0&a=1&b=2");
        assert_eq!(canonical_query(""), "");
    }
}
