"""deepseek-v4.1-flash credits: 0.11x metered model in both realms.
"""
import os
import sys
import tempfile
import unittest

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

    def test_international_keeps_catalog_credits(self):
        item = wb_proxy.model_entry(
            "deepseek-v4.1-flash", {"credits": "x0.11"}, "intl")
        self.assertEqual(item["credits"], "x0.11")
        self.assertNotIn("upstream_target", item)


if __name__ == "__main__":
    unittest.main()
