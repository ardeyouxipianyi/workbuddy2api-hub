"""Tests for the deepseek-v4.1-flash backend target toggle."""
import os
import tempfile
import unittest
from unittest import mock

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

    def test_persists_setting(self):
        wb_settings.set_deepseek_flash_force_sg(self.tmp, True)
        self.assertTrue(wb_settings.deepseek_flash_force_sg(self.tmp))
        wb_settings.set_deepseek_flash_force_sg(self.tmp, False)
        self.assertFalse(wb_settings.deepseek_flash_force_sg(self.tmp))

    def test_env_var_override(self):
        with mock.patch.dict(os.environ, {"WB_DEEPSEEK_FLASH_FORCE_SG": "1"}):
            self.assertTrue(wb_settings.deepseek_flash_force_sg(self.tmp))
        with mock.patch.dict(os.environ, {"WB_DEEPSEEK_FLASH_FORCE_SG": "true"}):
            self.assertTrue(wb_settings.deepseek_flash_force_sg(self.tmp))

    def test_route_rewrite_when_disabled(self):
        wb_settings.set_deepseek_flash_force_sg(self.tmp, False)
        payload = {"model": "deepseek-v4.1-flash"}
        wb_proxy._apply_deepseek_flash_target(payload)
        self.assertEqual(payload["model"], "deepseek-v4.1-flash")

    def test_route_rewrite_when_enabled(self):
        wb_settings.set_deepseek_flash_force_sg(self.tmp, True)
        payload = {"model": "deepseek-v4.1-flash"}
        wb_proxy._apply_deepseek_flash_target(payload)
        self.assertEqual(payload["model"], "deepseek-v4.1-flash-sg")

    def test_other_models_untouched(self):
        wb_settings.set_deepseek_flash_force_sg(self.tmp, True)
        for name in ("deepseek-v4.1-flash-sg", "hy4-preview", "glm-5.1"):
            payload = {"model": name}
            wb_proxy._apply_deepseek_flash_target(payload)
            self.assertEqual(payload["model"], name)

    def test_model_entry_credits_reflect_toggle(self):
        wb_settings.set_deepseek_flash_force_sg(self.tmp, False)
        entry_free = wb_proxy.model_entry("deepseek-v4.1-flash", {"credits": "x0.00"})
        self.assertEqual(entry_free.get("credits"), "x0.00")
        self.assertEqual(entry_free.get("upstream_target"), "deepseek-v4.1-flash")

        wb_settings.set_deepseek_flash_force_sg(self.tmp, True)
        entry_paid = wb_proxy.model_entry("deepseek-v4.1-flash", {"credits": "x0.00"})
        self.assertEqual(entry_paid.get("credits"), "x0.11")
        self.assertEqual(entry_paid.get("upstream_target"), "deepseek-v4.1-flash-sg")


if __name__ == "__main__":
    unittest.main()
