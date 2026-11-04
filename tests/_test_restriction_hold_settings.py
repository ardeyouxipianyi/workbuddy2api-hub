"""Tests for restriction hold protection settings, runtime hold state, and instant reset.

Pins the contract for operator-configurable pause duration and manual unfreeze
when upstream triggers 403 / code 11140 restriction waves.

    python -m unittest tests/_test_restriction_hold_settings.py
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_settings
import wb_proxy


class RestrictionHoldSettingsTests(unittest.TestCase):
    def setUp(self):
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.temp_dir = tempfile.mkdtemp(dir=self.base_dir)
        wb_proxy.reset_restriction_state()

    def tearDown(self):
        wb_proxy.reset_restriction_state()
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_default_and_env_fallback(self):
        # Default is 1800.0 seconds
        self.assertEqual(wb_settings.restriction_hold_seconds(self.temp_dir), 1800.0)

        # Environment variable override
        old_env = os.environ.get("WB_RESTRICTION_HOLD_SECONDS")
        try:
            os.environ["WB_RESTRICTION_HOLD_SECONDS"] = "300"
            self.assertEqual(wb_settings.restriction_hold_seconds(self.temp_dir), 300.0)
        finally:
            if old_env is None:
                os.environ.pop("WB_RESTRICTION_HOLD_SECONDS", None)
            else:
                os.environ["WB_RESTRICTION_HOLD_SECONDS"] = old_env

    def test_set_restriction_hold_validation(self):
        # Valid positive seconds
        saved = wb_settings.set_restriction_hold_seconds(self.temp_dir, 600)
        self.assertEqual(saved, 600)
        self.assertEqual(wb_settings.restriction_hold_seconds(self.temp_dir), 600.0)

        # Valid 0 to disable
        saved0 = wb_settings.set_restriction_hold_seconds(self.temp_dir, 0)
        self.assertEqual(saved0, 0)
        self.assertEqual(wb_settings.restriction_hold_seconds(self.temp_dir), 0.0)

        # Negative number raises ValueError
        with self.assertRaises(ValueError):
            wb_settings.set_restriction_hold_seconds(self.temp_dir, -1)

        # Boolean raises ValueError
        with self.assertRaises(ValueError):
            wb_settings.set_restriction_hold_seconds(self.temp_dir, True)

        # Exceeds max raises ValueError
        with self.assertRaises(ValueError):
            wb_settings.set_restriction_hold_seconds(self.temp_dir, 86400 * 30)

    def test_runtime_holds_and_instant_reset(self):
        now = 10000.0
        # Manually seed a restriction hold on 'international'
        with wb_proxy._RESTRICTION_LOCK:
            wb_proxy._RESTRICTION_HOLD["international"] = now + 500.0
            wb_proxy._RESTRICTION_HOLD["domestic"] = now + 200.0

        holds = wb_proxy.all_restriction_holds(now=now)
        self.assertEqual(holds.get("international"), 500)
        self.assertEqual(holds.get("domestic"), 200)
        self.assertAlmostEqual(wb_proxy.restriction_hold_remaining("international", now=now), 500.0)

        # Test clamp_restriction_holds
        wb_proxy.clamp_restriction_holds(100.0, now=now)
        holds_clamped = wb_proxy.all_restriction_holds(now=now)
        self.assertEqual(holds_clamped.get("international"), 100)
        self.assertEqual(holds_clamped.get("domestic"), 100)

        # Test reset specific realm
        wb_proxy.reset_restriction_state(realm="domestic")
        holds_after_domestic = wb_proxy.all_restriction_holds(now=now)
        self.assertNotIn("domestic", holds_after_domestic)
        self.assertIn("international", holds_after_domestic)

        # Test full reset
        wb_proxy.reset_restriction_state()
        self.assertEqual(wb_proxy.all_restriction_holds(now=now), {})
        self.assertEqual(wb_proxy.restriction_hold_remaining("international", now=now), 0.0)

    def test_disabled_hold_prevents_wave_pause(self):
        now = 20000.0
        # Configure 0 seconds (disabled)
        old_accounts_dir = wb_proxy.ACCOUNTS_DIR
        wb_proxy.ACCOUNTS_DIR = self.temp_dir
        try:
            wb_settings.set_restriction_hold_seconds(self.temp_dir, 0)
            # Trigger restrictions for distinct accounts
            res1 = wb_proxy.note_account_restriction("international", uid="u1", now=now)
            res2 = wb_proxy.note_account_restriction("international", uid="u2", now=now + 1)
            res3 = wb_proxy.note_account_restriction("international", uid="u3", now=now + 2)
            self.assertEqual(res1, 0.0)
            self.assertEqual(res2, 0.0)
            self.assertEqual(res3, 0.0)
            self.assertEqual(wb_proxy.restriction_hold_remaining("international", now=now + 2), 0.0)
        finally:
            wb_proxy.ACCOUNTS_DIR = old_accounts_dir

    def test_bodyless_posts_and_settings_view(self):
        self.assertIn("/settings/restriction/reset", wb_proxy.BODYLESS_PANEL_POSTS)
        view = wb_proxy.runtime_settings_view()
        self.assertIn("restriction_hold_seconds", view)
        self.assertIn("restriction_holds", view)


if __name__ == "__main__":
    unittest.main()
