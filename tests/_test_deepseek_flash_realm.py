"""deepseek-v4.1-flash credits: the free channel is international-only.

The same model id is a 0.11x metered model in the domestic catalog. Treating
the id as free in `model_entry()` made the panel show "free for a limited time"
for domestic keys, and `_apply_deepseek_flash_target()` could rewrite a
domestic request to the international-only `-sg` variant.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="flash-realm-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy


class FlashRealmTest(unittest.TestCase):
    def test_domestic_keeps_catalog_credits(self):
        item = wb_proxy.model_entry(
            "deepseek-v4.1-flash", {"credits": "x0.11"}, "cn")
        self.assertEqual(item["credits"], "x0.11")
        self.assertNotIn("upstream_target", item)

    def test_international_is_free(self):
        item = wb_proxy.model_entry(
            "deepseek-v4.1-flash", {"credits": "x0.11"}, "intl")
        self.assertEqual(item["credits"], "x0.00")
        self.assertEqual(item["upstream_target"], "deepseek-v4.1-flash")

    def test_forced_sg_rewrites_international_only(self):
        with mock.patch.object(
                wb_proxy.wb_settings, "deepseek_flash_force_sg",
                return_value=True):
            domestic = {"model": "deepseek-v4.1-flash"}
            wb_proxy._apply_deepseek_flash_target(domestic, "cn")
            self.assertEqual(domestic["model"], "deepseek-v4.1-flash")

            international = {"model": "deepseek-v4.1-flash"}
            wb_proxy._apply_deepseek_flash_target(international, "intl")
            self.assertEqual(international["model"], "deepseek-v4.1-flash-sg")


if __name__ == "__main__":
    unittest.main()
