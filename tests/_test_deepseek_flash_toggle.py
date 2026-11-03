"""Tests for the deepseek-v4.1-flash catalog model entry."""
import os
import tempfile
import unittest

import wb_settings
import wb_proxy


class DeepseekFlashToggleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ds-toggle-")
        self.orig_dir = wb_proxy.ACCOUNTS_DIR
        wb_proxy.ACCOUNTS_DIR = self.tmp

    def tearDown(self):
        wb_proxy.ACCOUNTS_DIR = self.orig_dir

    def test_default_is_false(self):
        self.assertFalse(wb_settings.deepseek_flash_force_sg(self.tmp))

    def test_model_entry_credits_uniform(self):
        entry = wb_proxy.model_entry("deepseek-v4.1-flash", {"credits": "x0.11"}, "intl")
        self.assertEqual(entry.get("credits"), "x0.11")
        self.assertNotIn("upstream_target", entry)


if __name__ == "__main__":
    unittest.main()
