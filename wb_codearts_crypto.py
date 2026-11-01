# -*- coding: utf-8 -*-
"""华为云 CodeArts 需要的密码学原语（纯标准库）。

- P-256 ECDSA（ES256）：DPoP 证明与 JWK 持久化
- PKCE：OAuth authorization_code
- 华为云 SDK-HMAC-SHA256 请求签名（AK/SK + 可选 STS security token）

为什么自己写 P-256：便携运行时只有标准库，不能依赖 `cryptography`。曲线参数
与签名格式（JOSE raw r||s、低 S 归一化）和 codearts2api 的 Go 实现逐项对齐；
`tests/_test_codearts_crypto.py` 里有 Go 侧生成的对照向量。
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.parse

# NIST P-256 (secp256r1)
P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
A = (P - 3) % P
B = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b
N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551
GX = 0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296
GY = 0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5
G = (GX, GY)

SIGNING_ALGORITHM = "SDK-HMAC-SHA256"


def b64url(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64url_decode(text):
    text = str(text or "")
    pad = "=" * ((4 - len(text) % 4) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _inv(value, mod):
    return pow(value % mod, -1, mod)


def _point_add(left, right):
    if left is None:
        return right
    if right is None:
        return left
    x1, y1 = left
    x2, y2 = right
    if x1 == x2:
        if (y1 + y2) % P == 0:
            return None
        slope = (3 * x1 * x1 + A) * _inv(2 * y1, P) % P
    else:
        slope = (y2 - y1) * _inv(x2 - x1, P) % P
    x3 = (slope * slope - x1 - x2) % P
    y3 = (slope * (x1 - x3) - y1) % P
    return (x3, y3)


def _point_mul(scalar, point):
    result = None
    addend = point
    while scalar:
        if scalar & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        scalar >>= 1
    return result


def _int_bytes(value, size=32):
    return int(value).to_bytes(size, "big")


def generate_keypair():
    """生成 P-256 私钥，返回 (d, x, y)。"""
    d = secrets.randbelow(N - 1) + 1
    x, y = _point_mul(d, G)
    return d, x, y


def keypair_from_jwk(jwk):
    """从持久化 JWK 恢复 (d, x, y)；校验公私钥一致。"""
    if not isinstance(jwk, dict) or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise ValueError("invalid DPoP JWK curve")
    try:
        d = int.from_bytes(b64url_decode(jwk["d"]), "big")
        x = int.from_bytes(b64url_decode(jwk["x"]), "big")
        y = int.from_bytes(b64url_decode(jwk["y"]), "big")
    except Exception as exc:
        raise ValueError("invalid DPoP JWK fields: %s" % exc)
    if not (0 < d < N):
        raise ValueError("invalid DPoP JWK private scalar")
    if not (0 <= x < P and 0 <= y < P):
        raise ValueError("invalid DPoP JWK public point")
    wx, wy = _point_mul(d, G)
    if wx != x or wy != y:
        raise ValueError("DPoP JWK public/private key mismatch")
    return d, x, y


def jwk_from_keypair(d, x, y):
    """生成可持久化的私有 JWK（与 codearts2api 的字段一致）。"""
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": b64url(_int_bytes(x)),
        "y": b64url(_int_bytes(y)),
        "d": b64url(_int_bytes(d)),
    }


def public_jwk(jwk):
    return {"kty": "EC", "crv": "P-256", "x": jwk["x"], "y": jwk["y"]}


def sign_es256(private_d, message):
    """ECDSA P-256 / SHA-256，返回 JOSE raw r||s（低 S）。"""
    digest = int.from_bytes(hashlib.sha256(message).digest(), "big")
    while True:
        k = secrets.randbelow(N - 1) + 1
        point = _point_mul(k, G)
        r = point[0] % N
        if r == 0:
            continue
        s = (_inv(k, N) * (digest + r * private_d)) % N
        if s == 0:
            continue
        if s > N // 2:
            s = N - s
        return _int_bytes(r) + _int_bytes(s)


def verify_es256(public_x, public_y, message, signature):
    """验证 JOSE raw r||s 签名；只用于自检/测试。"""
    if len(signature) != 64:
        return False
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if not (1 <= r < N and 1 <= s < N):
        return False
    digest = int.from_bytes(hashlib.sha256(message).digest(), "big")
    w = _inv(s, N)
    u1 = (digest * w) % N
    u2 = (r * w) % N
    point = _point_add(_point_mul(u1, G), _point_mul(u2, (public_x, public_y)))
    return point is not None and point[0] % N == r


def dpop_proof(jwk, htu):
    """生成 DPoP JWT（htm=POST，htu=token 端点）。"""
    d, x, y = keypair_from_jwk(jwk)
    header = {
        "alg": "ES256",
        "typ": "dpop+jwt",
        "jwk": public_jwk(jwk),
    }
    payload = {
        "htm": "POST",
        "htu": htu,
        "iat": int(time.time()),
        "jti": secrets.token_hex(16),
    }
    head = b64url(json.dumps(header, separators=(",", ":"), sort_keys=True).encode())
    body = b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signing_input = (head + "." + body).encode("ascii")
    return head + "." + body + "." + b64url(sign_es256(d, signing_input))


def pkce_pair():
    """返回 (code_verifier, code_challenge)；与 Go 的 64 字节随机一致。"""
    verifier = b64url(secrets.token_bytes(64))
    challenge = b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def random_hex(size):
    return secrets.token_hex(size)


def _sha256_hex(raw):
    return hashlib.sha256(raw).hexdigest()


def _canonical_uri(path):
    segments = [urllib.parse.quote(seg, safe="") for seg in (path or "/").split("/")]
    out = "/".join(segments)
    if not out.endswith("/"):
        out += "/"
    return out


def _canonical_query(raw):
    if not raw:
        return ""
    pairs = urllib.parse.parse_qsl(raw, keep_blank_values=True)
    pairs.sort()
    return "&".join(
        urllib.parse.quote(key, safe="") + "=" + urllib.parse.quote(value, safe="")
        for key, value in pairs)


def sign_request(method, url, headers, body, access_key, secret_key,
                 security_token="", x_date=None):
    """给请求头加华为云 SDK-HMAC-SHA256 签名，返回新的 headers dict。

    与 Go 的 `signRequest` 对齐：signed headers = 全部头（小写排序），
    CanonicalURI 每段 percent-encode 且末尾补 "/"，payload hash 取
    X-Sdk-Content-Sha256。`headers` 不会被修改。
    """
    parsed = urllib.parse.urlsplit(url)
    out = {str(k): str(v) for k, v in (headers or {}).items()}
    date = x_date or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out["X-Sdk-Date"] = date
    out["Host"] = parsed.netloc
    if security_token:
        out["X-Security-Token"] = security_token
    payload_hash = _sha256_hex(body or b"")
    out["X-Sdk-Content-Sha256"] = payload_hash

    lower = {}
    for key, value in out.items():
        lower[key.lower()] = str(value).strip()
    signed_keys = sorted(lower)
    canonical_headers = "".join(key + ":" + lower[key] + "\n" for key in signed_keys)
    signed_headers = ";".join(signed_keys)
    canonical_request = "\n".join([
        method.upper(),
        _canonical_uri(parsed.path or "/"),
        _canonical_query(parsed.query),
        canonical_headers,
        signed_headers,
        payload_hash,
    ])
    string_to_sign = "\n".join([
        SIGNING_ALGORITHM,
        date,
        _sha256_hex(canonical_request.encode("utf-8")),
    ])
    signature = hmac.new(secret_key.encode("utf-8"),
                         string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    out["Authorization"] = (
        SIGNING_ALGORITHM
        + " Access=" + access_key
        + ", SignedHeaders=" + signed_headers
        + ", Signature=" + signature)
    return out
