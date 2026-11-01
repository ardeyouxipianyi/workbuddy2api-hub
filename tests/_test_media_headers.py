"""媒体端点的出站标头：Account.headers(purpose="media")。

钉住的是「媒体这一路发什么、不发什么」，以及它**不该碰另外两条路**：

  - 复用 chat 的身分（同一个 build_identity_headers），所以会话三件套
    （X-Conversation-ID / -Request-ID / -Message-ID）一个不少，且**每请求现生成**
    —— 官方 CLI 的 ImageService 也是这么组的。
  - 媒体特有的三个头：Accept 列表形态、X-Requested-With、企业标识。
  - 刻意不带 Origin / Referer：dsh 的取证探针就是不带这两个头拿到 200 的，
    多带只多一个被网关拒掉的变量。
  - 国内版才发 X-Product: SaaS 与 X-Device-Token；国际版与 CLI 身份都不发。
  - billing 分支一个字不改（它的头形状与媒体不同，改它就是动计费链路）。

Run with: python tests/_test_media_headers.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_identity

_TMP = tempfile.TemporaryDirectory(prefix="wb-media-headers-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP.name, "accounts")
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

# 一个不像 JWT 也不像静态 Key 的普通 token：is_api_key_token 只看 ck_/pt_ 前缀，
# jwt_uid 解析失败会返回空串，uid 因此取落盘字段——正是我们要的稳定值。
TOKEN = "media-header-test-token"


def make_account(realm="cn", product=wb_identity.PRODUCT_DESKTOP,
                 enterprise_id="", token=TOKEN):
    return wb_accounts.Account({
        "accessToken": token,
        "uid": "u-media-1",
        "nickname": "tester",
        "realm": realm,
        "product": product,
        "enterpriseId": enterprise_id,
        "domain": "copilot.tencent.com" if realm == "cn" else "www.workbuddy.ai",
    })


class MediaHeaderTests(unittest.TestCase):
    def setUp(self):
        self.account = make_account()

    def test_has_auth_and_json_wire_headers(self):
        headers = self.account.headers(purpose="media")
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Accept"], "application/json, text/plain, */*")
        self.assertEqual(headers["X-Requested-With"], "XMLHttpRequest")

    def test_carries_the_session_triplet(self):
        headers = self.account.headers(purpose="media")
        for name in ("X-Conversation-ID", "X-Conversation-Request-ID",
                     "X-Conversation-Message-ID"):
            self.assertTrue(headers.get(name), "%s 缺失" % name)

    def test_session_ids_are_fresh_per_request(self):
        """官方 CLI 的 ImageService 每次现生成一组；复用一个 id 就多一个关联键。"""
        first = self.account.headers(purpose="media")
        second = self.account.headers(purpose="media")
        self.assertNotEqual(first["X-Conversation-ID"], second["X-Conversation-ID"])
        self.assertNotEqual(first["X-Conversation-Message-ID"],
                            second["X-Conversation-Message-ID"])

    def test_cn_gets_saas_product_flag(self):
        headers = self.account.headers(purpose="media")
        self.assertEqual(headers["X-Product"], "SaaS")

    def test_international_omits_cn_only_headers(self):
        headers = make_account(realm="intl").headers(purpose="media")
        self.assertEqual(headers["X-Domain"], "www.workbuddy.ai")
        self.assertNotIn("X-Device-Token", headers)

    def test_no_enterprise_sends_the_explicit_negation(self):
        headers = self.account.headers(purpose="media")
        self.assertEqual(headers["X-No-Enterprise-Id"], "1")
        self.assertNotIn("X-Enterprise-Id", headers)
        self.assertNotIn("X-Tenant-Id", headers)

    def test_enterprise_sends_both_ids(self):
        headers = make_account(enterprise_id="ent-9").headers(purpose="media")
        self.assertEqual(headers["X-Enterprise-Id"], "ent-9")
        self.assertEqual(headers["X-Tenant-Id"], "ent-9")
        self.assertNotIn("X-No-Enterprise-Id", headers)

    def test_does_not_send_origin_or_referer(self):
        """dsh 的取证探针不带这两个头；多带只多一个可能被拒的变量。"""
        headers = self.account.headers(purpose="media")
        self.assertNotIn("Origin", headers)
        self.assertNotIn("Referer", headers)

    def test_api_key_account_sends_both_auth_headers(self):
        headers = make_account(token="ck_abc123.secret").headers(purpose="media")
        self.assertEqual(headers["X-API-Key"], "ck_abc123.secret")
        self.assertEqual(headers["Authorization"], "Bearer ck_abc123.secret")

    def test_media_identity_switch_changes_the_user_agent(self):
        desktop = self.account.headers(purpose="media")
        cli = self.account.headers(purpose="media", media_identity="cli")
        self.assertNotEqual(desktop["User-Agent"], cli["User-Agent"])
        self.assertIn("CLI", cli["User-Agent"])
        # 切到 CLI 形状时，官方 CLI 全包没有设备 token 逻辑
        self.assertNotIn("X-Device-Token", cli)

    def test_unknown_identity_falls_back_to_account_product(self):
        headers = self.account.headers(purpose="media", media_identity="nonsense")
        self.assertEqual(headers["User-Agent"],
                         self.account.headers(purpose="media")["User-Agent"])


class BillingBranchUntouchedTests(unittest.TestCase):
    """媒体分支是插在 `purpose != "chat"` 里的一个 if，billing 必须逐字不动。"""

    def setUp(self):
        self.account = make_account()

    def test_billing_still_has_origin_and_referer(self):
        headers = self.account.headers(purpose="billing")
        self.assertIn("Origin", headers)
        self.assertIn("Referer", headers)
        self.assertEqual(headers["Referrer"] if "Referrer" in headers else headers["Referer"],
                         "https://www.codebuddy.cn/")

    def test_billing_keeps_machine_and_session_ids(self):
        headers = self.account.headers(purpose="billing")
        self.assertIn("X-Machine-ID", headers)
        self.assertIn("X-Session-ID", headers)

    def test_billing_has_no_session_triplet(self):
        """计费端点不吃会话三件套；媒体补了它，但那不该漏到 billing。"""
        headers = self.account.headers(purpose="billing")
        self.assertNotIn("X-Conversation-ID", headers)

    def test_billing_accept_language_preserved(self):
        headers = self.account.headers(purpose="billing")
        self.assertEqual(headers["Accept-Language"], "zh-CN")


if __name__ == "__main__":
    unittest.main(verbosity=2)
