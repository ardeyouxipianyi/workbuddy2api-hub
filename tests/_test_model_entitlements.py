"""Model entitlements: what the pool can actually run, learned from upstream.

`/v1/models` lists the product's catalogue; a free account still gets
`400 code 11158` for the premium models in it. The gateway records that denial
and hides the model, and a later successful call puts it back.

Run with: python _test_model_entitlements.py
No upstream credentials or outbound network are used.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_entitlements as ent

DENIAL = ('upstream 400: {"code":11158,"msg":"model [gpt-6-astra] is not '
          'allowed for user type [personal_free_user]","requestId":"abc"}')


class DenialDetectionTest(unittest.TestCase):
    def test_real_upstream_body_is_a_denial(self):
        self.assertTrue(ent.is_denial(DENIAL))
        self.assertEqual(ent.user_type(DENIAL), "personal_free_user")

    def test_other_400s_are_not_denials(self):
        self.assertFalse(ent.is_denial("upstream 400: invalid model"))
        self.assertFalse(ent.is_denial(""))
        self.assertFalse(ent.is_denial(None))

    def test_code_survives_a_truncated_message(self):
        # record_error keeps only the first 200 characters, which still holds
        # the code even when the message is cut mid-JSON.
        self.assertTrue(ent.is_denial('{"code":11158,"msg":"model [gpt-6-astra"'))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="entitlements-")

    def test_deny_then_filter(self):
        ent.note_denied(self.dir, "intl", "gpt-6-astra", DENIAL)
        entries = [("gpt-6-astra", {}), ("hy3", {})]
        self.assertEqual([m for m, _ in ent.filter_entries(self.dir, "intl", entries)],
                         ["hy3"])

    def test_success_restores_the_model(self):
        ent.note_denied(self.dir, "intl", "gpt-6-astra", DENIAL)
        self.assertIn("gpt-6-astra", ent.blocked(self.dir, "intl"))
        ent.note_allowed(self.dir, "intl", "gpt-6-astra")
        self.assertEqual(ent.blocked(self.dir, "intl"), {})
        entries = [("gpt-6-astra", {})]
        self.assertEqual(ent.filter_entries(self.dir, "intl", entries), entries)

    def test_realms_are_independent(self):
        ent.note_denied(self.dir, "intl", "gpt-6-astra", DENIAL)
        self.assertNotIn("gpt-6-astra", ent.blocked(self.dir, "cn"))
        self.assertIn("gpt-6-astra", ent.blocked(self.dir, "all"))

    def test_store_round_trips_as_json(self):
        ent.note_denied(self.dir, "intl", "gpt-6-astra", DENIAL)
        with open(os.path.join(self.dir, ent.FILENAME), encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload["realms"]["intl"]["gpt-6-astra"]["user_type"],
                         "personal_free_user")


if __name__ == "__main__":
    unittest.main()
