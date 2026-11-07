"""国际版模型改名：deepseek-v4.1-flash -> deepseek-v4.1-flash-sg 的兼容映射。

    python tests/_test_intl_model_alias.py

背景：国际版上游 2026-11 把对话模型换成了 `-sg` 这一支，官方客户端的模型目录
里只有 `-sg`；继续发旧 ID 的账号会收到 429 / code 6004（旧额度桶已空）。这里钉住
三件事：映射只在 intl 生效、面板「账号测试」也走映射、WB_INTL_MODEL_ALIAS=0 能关。
"""
import gzip
import io
import json
import os
import sys
import unittest
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import wb_accounts as A  # noqa: E402
import wb_proxy as P  # noqa: E402

LEGACY = "deepseek-v4.1-flash"
RENAMED = "deepseek-v4.1-flash-sg"


class AliasTests(unittest.TestCase):
    def test_intl_renames_the_legacy_id(self):
        self.assertEqual(P.intl_model_alias(LEGACY, "intl"), RENAMED)

    def test_renamed_id_is_stable(self):
        self.assertEqual(P.intl_model_alias(RENAMED, "intl"), RENAMED)

    def test_other_realms_keep_the_id(self):
        for realm in ("cn", "codearts"):
            self.assertEqual(P.intl_model_alias(LEGACY, realm), LEGACY)

    def test_apply_rewrites_the_body(self):
        payload = {"model": LEGACY}
        self.assertEqual(P.apply_intl_model_alias(payload, "intl"), RENAMED)
        self.assertEqual(payload["model"], RENAMED)

    def test_escape_hatch(self):
        previous = os.environ.get("WB_INTL_MODEL_ALIAS")
        os.environ["WB_INTL_MODEL_ALIAS"] = "0"
        try:
            self.assertEqual(P.intl_model_alias(LEGACY, "intl"), LEGACY)
        finally:
            if previous is None:
                os.environ.pop("WB_INTL_MODEL_ALIAS", None)
            else:
                os.environ["WB_INTL_MODEL_ALIAS"] = previous


class ProbeAccountTests(unittest.TestCase):
    def account(self, product):
        return A.Account({"uid": "u-intl-test", "nickname": "intl-test",
                          "domain": "www.workbuddy.ai", "realm": "intl",
                          "product": product, "accessToken": "opaque-token"},
                         "")

    def sent_model(self, product, model):
        seen = {}

        def fake_urlopen(req, timeout=None, proxy=None):
            raw = req.data
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            seen["model"] = json.loads(raw.decode("utf-8"))["model"]
            raise urllib.error.HTTPError(
                req.full_url, 400, "stop", {}, io.BytesIO(b'{"code":0}'))

        previous = A.urlopen
        A.urlopen = fake_urlopen
        try:
            P.probe_account(self.account(product), model)
        finally:
            A.urlopen = previous
        return seen.get("model")

    def test_panel_test_sends_the_renamed_id(self):
        self.assertEqual(self.sent_model("workbuddy", LEGACY), RENAMED)

    def test_panel_test_renames_the_gzipped_cli_body(self):
        self.assertEqual(self.sent_model("cli", LEGACY), RENAMED)


if __name__ == "__main__":
    unittest.main()
