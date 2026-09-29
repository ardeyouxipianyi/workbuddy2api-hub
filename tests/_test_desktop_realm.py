"""区域归属：桌面凭据与手动导入的 JSON 都要落到正确的那一边。

    python tests/_test_desktop_realm.py

背景：国内版出口换过几轮（copilot.tencent.com → codebuddy.cn → workbuddy.cn），
旧识别只认前两个，于是 workbuddy.cn 的国内账号被判成国际版——手动导入 JSON 时
「明明是国内的却进了国际版」就是这么来的。这里把判定规则钉住。
"""
import base64
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import wb_accounts as A  # noqa: E402

CN_ISS = "https://www.workbuddy.cn/auth/realms/copilot"
CN_ISS_LEGACY = "https://www.codebuddy.cn/auth/realms/copilot"
CN_ISS_TENCENT = "https://copilot.tencent.com/auth/realms/copilot"
INTL_ISS = "https://www.workbuddy.ai/auth/realms/copilot"


def jwt(iss, sub="u-1"):
    def part(obj):
        raw = base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")
        return raw.rstrip("=")
    return "%s.%s.sig" % (part({"alg": "RS256", "typ": "JWT"}),
                          part({"iss": iss, "sub": sub, "exp": 4102444800}))


CN_TOKEN = jwt(CN_ISS)
CN_TOKEN_LEGACY = jwt(CN_ISS_LEGACY)
CN_TOKEN_TENCENT = jwt(CN_ISS_TENCENT)
INTL_TOKEN = jwt(INTL_ISS)
OPAQUE_TOKEN = jwt("https://example.invalid/realms/x")   # 看不出区域


class RealmEvidenceTests(unittest.TestCase):
    def test_cn_markers(self):
        for token, domain in ((CN_TOKEN, ""), (CN_TOKEN_LEGACY, ""), (CN_TOKEN_TENCENT, ""),
                              (OPAQUE_TOKEN, "www.workbuddy.cn"),
                              (OPAQUE_TOKEN, "copilot.tencent.com"),
                              (OPAQUE_TOKEN, "www.codebuddy.cn")):
            self.assertEqual(A.realm_evidence(token, domain), "cn",
                             "should read as CN: %s / %s" % (token[-8:], domain))

    def test_intl_markers(self):
        for token, domain in ((INTL_TOKEN, ""), (OPAQUE_TOKEN, "www.workbuddy.ai")):
            self.assertEqual(A.realm_evidence(token, domain), "intl")

    def test_unknown_is_none_not_intl(self):
        """看不出区域要老实返回 None，别默认成 intl（老实现的问题就在这）。"""
        self.assertIsNone(A.realm_evidence(OPAQUE_TOKEN, ""))
        self.assertIsNone(A.realm_evidence("", ""))

    def test_detect_realm_default_unchanged(self):
        """对外的 detect_realm_from_token 仍然把看不出的当国际版（保持兼容）。"""
        self.assertEqual(A.detect_realm_from_token(OPAQUE_TOKEN), "intl")
        self.assertEqual(A.detect_realm_from_token(CN_TOKEN), "cn")

    def test_workbuddy_cn_is_not_confused_with_intl(self):
        self.assertEqual(A.realm_evidence(OPAQUE_TOKEN, "www.workbuddy.cn"), "cn")
        self.assertEqual(A.realm_evidence(OPAQUE_TOKEN, "www.workbuddy.ai"), "intl")


class DesktopRealmTests(unittest.TestCase):
    """桌面凭据：token 说了算，文件名只兜底。"""

    def test_token_wins_over_filename(self):
        # 国内账号登录在国际版那套客户端里（切区登录），文件名叫 -ai.info
        self.assertEqual(A.desktop_effective_realm("intl", CN_TOKEN, "www.workbuddy.cn"), "cn")
        self.assertEqual(A.desktop_effective_realm("cn", INTL_TOKEN, "www.workbuddy.ai"), "intl")

    def test_filename_used_when_token_has_no_evidence(self):
        self.assertEqual(A.desktop_effective_realm("cn", OPAQUE_TOKEN), "cn")
        self.assertEqual(A.desktop_effective_realm("intl", OPAQUE_TOKEN), "intl")

    def test_unknown_hint_defaults_to_intl(self):
        self.assertEqual(A.desktop_effective_realm(None, OPAQUE_TOKEN), "intl")


class ImportRowRealmTests(unittest.TestCase):
    """手动导入 JSON：区域同样以 token 为准。"""

    def row(self, token, **extra):
        row = {"uid": "u-1", "accessToken": token, "refreshToken": "r.r.r"}
        row.update(extra)
        return A.normalise_import_row(row)

    def test_cn_account_with_workbuddy_cn_domain(self):
        """报的那个 bug：域名是 workbuddy.cn 的国内账号不能再被判成国际版。"""
        self.assertEqual(self.row(OPAQUE_TOKEN, domain="www.workbuddy.cn")["realm"], "cn")

    def test_stale_realm_field_does_not_win(self):
        """行里带着过期的 realm（当初就存错了）时，按 token 纠正回来。"""
        out = self.row(CN_TOKEN, realm="intl", domain="www.workbuddy.cn")
        self.assertEqual(out["realm"], "cn")

    def test_intl_account_stays_intl(self):
        out = self.row(INTL_TOKEN, domain="www.workbuddy.ai")
        self.assertEqual(out["realm"], "intl")

    def test_row_without_domain_uses_issuer(self):
        self.assertEqual(self.row(CN_TOKEN)["realm"], "cn")
        self.assertEqual(self.row(INTL_TOKEN)["realm"], "intl")

    def test_forced_realm_still_wins(self):
        """接口上的 realm 参数是显式指定，优先级最高（原有行为）。"""
        self.assertEqual(A.normalise_import_row(
            {"uid": "u-1", "accessToken": CN_TOKEN}, realm="intl")["realm"], "intl")

    def test_row_realm_used_when_token_is_opaque(self):
        self.assertEqual(self.row(OPAQUE_TOKEN, realm="cn")["realm"], "cn")
        self.assertEqual(self.row(OPAQUE_TOKEN)["realm"], "intl")

    def test_domain_follows_realm(self):
        """区域定了，X-Domain 就不能还指着另一边。"""
        self.assertEqual(self.row(CN_TOKEN, domain="www.workbuddy.ai")["domain"],
                         A.get_realm_config("cn")["domain"])
        self.assertEqual(self.row(INTL_TOKEN, domain="www.codebuddy.cn")["domain"],
                         A.get_realm_config("intl")["domain"])

    def test_matching_domain_is_kept(self):
        self.assertEqual(self.row(CN_TOKEN, domain="www.codebuddy.cn")["domain"],
                         "www.codebuddy.cn")
        self.assertEqual(self.row(INTL_TOKEN, domain="www.workbuddy.ai")["domain"],
                         "www.workbuddy.ai")

    def test_exported_account_survives_round_trip(self):
        """导出再导入，区域不能漂移。"""
        for token, want in ((CN_TOKEN, "cn"), (INTL_TOKEN, "intl")):
            account = A.Account({"uid": "u-1", "accessToken": token, "refreshToken": "r.r.r"})
            self.assertEqual(account.realm, want)
            doc = A.build_export_document([account])
            rows, problem = A._coerce_account_rows(doc)
            self.assertEqual(problem, "")
            self.assertEqual(A.normalise_import_row(rows[0])["realm"], want)


class AccountRealmTests(unittest.TestCase):
    def test_account_realm_from_token(self):
        self.assertEqual(A.Account({"accessToken": CN_TOKEN}).realm, "cn")
        self.assertEqual(A.Account({"accessToken": INTL_TOKEN}).realm, "intl")

    def test_explicit_realm_in_data_wins(self):
        self.assertEqual(A.Account({"accessToken": CN_TOKEN, "realm": "intl"}).realm, "intl")


if __name__ == "__main__":
    unittest.main(verbosity=2)
