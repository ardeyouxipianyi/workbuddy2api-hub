"""CodeArts 密码学：P-256 ECDSA、DPoP、PKCE、华为云 HMAC 签名。

Go 侧对照向量由 codearts2api 的 `signRequest` / `crypto/ecdsa` 生成，硬编码在
下面的断言里；这样不依赖 Go 工具链也能钉住字节级兼容。
"""
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import wb_codearts_crypto as C  # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


print("== ECDSA ==")
d, x, y = C.generate_keypair()
check("public point on curve",
      (y * y - (x * x * x + C.A * x + C.B)) % C.P == 0)
sig = C.sign_es256(d, b"hello-codearts")
check("signature length", len(sig) == 64, len(sig))
check("self verify", C.verify_es256(x, y, b"hello-codearts", sig))
check("tampered message rejected",
      C.verify_es256(x, y, b"hello-codeart5", sig) is False)

# Go 用 d=1 签的向量；Python 必须能验。
go_sig = bytes.fromhex(
    "8de8b67b0182b0ad3e966bbf0e377da14e3d3850490e4272dfe98933d5a9bf30"
    "98e4e89d233a038da948e4cc788be8592cdef4f9aa45229002455be3473322c1")
check("Go signature verifies", C.verify_es256(C.GX, C.GY, b"hello-codearts", go_sig))

# 低 S：签名里的 s 不得超过 n/2。
s = int.from_bytes(sig[32:], "big")
check("low-S normalized", s <= C.N // 2, s)

print("== JWK ==")
jwk = C.jwk_from_keypair(d, x, y)
check("jwk fields", set(jwk) == {"kty", "crv", "x", "y", "d"}, jwk)
check("jwk no padding", "=" not in jwk["x"] and "=" not in jwk["d"])
d2, x2, y2 = C.keypair_from_jwk(jwk)
check("jwk round trip", (d2, x2, y2) == (d, x, y))
bad = dict(jwk, d=C.b64url((d + 1).to_bytes(32, "big")))
try:
    C.keypair_from_jwk(bad)
    check("mismatched jwk rejected", False)
except ValueError:
    check("mismatched jwk rejected", True)

print("== DPoP ==")
proof = C.dpop_proof(jwk, "https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens")
parts = proof.split(".")
check("dpop three parts", len(parts) == 3, proof)
header = json.loads(C.b64url_decode(parts[0]))
payload = json.loads(C.b64url_decode(parts[1]))
check("dpop alg", header["alg"] == "ES256")
check("dpop typ", header["typ"] == "dpop+jwt")
check("dpop jwk has no d", "d" not in header["jwk"])
check("dpop htm", payload["htm"] == "POST")
check("dpop htu", payload["htu"] == "https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens")
check("dpop iat", isinstance(payload["iat"], int))
check("dpop jti length", len(payload["jti"]) == 32)
check("dpop signature", C.verify_es256(
    x, y, (parts[0] + "." + parts[1]).encode("ascii"),
    C.b64url_decode(parts[2])))

print("== PKCE ==")
verifier, challenge = C.pkce_pair()
check("verifier length", 43 <= len(verifier) <= 128, len(verifier))
check("challenge", challenge == C.b64url(hashlib.sha256(verifier.encode()).digest()))
check("no padding", "=" not in verifier and "=" not in challenge)

print("== Huawei HMAC ==")
body = (b'{"model":"GLM-5.2","stream":true,'
        b'"messages":[{"role":"user","content":"hi"}]}')
headers = {
    "Content-Type": "application/json",
    "Accept": "text/event-stream",
    "x-auth-token": "TOKEN",
    "x-snap-traceid": "trace123",
    "X-Language": "zh-cn",
    "app-id": "CodeAgent3.0",
    "is_confidential": "false",
}
out = C.sign_request(
    "POST",
    "https://snap-access.cn-north-4.myhuaweicloud.com/api/v2/chat/completions",
    headers, body, "AKID", "SECRETKEY", "TOKEN", x_date="20261031T000000Z")
expected = (
    "SDK-HMAC-SHA256 Access=AKID, SignedHeaders=accept;app-id;content-type;host;"
    "is_confidential;x-auth-token;x-language;x-sdk-content-sha256;x-sdk-date;"
    "x-security-token;x-snap-traceid, Signature="
    "28c9c49de88825070ab6fba37f9b30a44bc22f165743bb6c409338cb5b78ad09")
check("authorization matches Go", out["Authorization"] == expected,
      out["Authorization"])
check("host header", out["Host"] ==
      "snap-access.cn-north-4.myhuaweicloud.com")
check("date header", out["X-Sdk-Date"] == "20261031T000000Z")
check("security token header", out["X-Security-Token"] == "TOKEN")
check("payload hash", out["X-Sdk-Content-Sha256"] ==
      hashlib.sha256(body).hexdigest())
check("caller headers preserved", out["x-snap-traceid"] == "trace123")

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
