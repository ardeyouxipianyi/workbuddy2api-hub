"""Settings writes must not drop keys the panel does not manage.

The web form only submits the keys it knows. If a setter replaces a whole
group object, any hand-written or future key beside them disappears on the
next save - silently, and only for the people who had added something. This
pins the deep-merge contract on the one grouped setter the panel has, plus
the top-level behaviour the plain save path already had.

    python tests/_test_settings_preservation.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_settings


class SettingsPreservationTests(unittest.TestCase):
    def _write(self, directory, data):
        with open(wb_settings.settings_path(directory), "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def _read(self, directory):
        with open(wb_settings.settings_path(directory), "r", encoding="utf-8") as fh:
            return json.load(fh)

    def test_upstream_group_keeps_unknown_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            self._write(directory, {
                "user_custom_top": {"keep": 1},
                "upstream": {
                    "header_timeout_seconds": 120,
                    "unknown_upstream_key": 42,
                    "nested": {"deep": 7},
                },
            })
            wb_settings.set_upstream_config(directory, {"idle_timeout_seconds": 600})
            stored = self._read(directory)
        self.assertEqual(stored["upstream"]["unknown_upstream_key"], 42)
        self.assertEqual(stored["upstream"]["nested"], {"deep": 7})
        self.assertEqual(stored["upstream"]["header_timeout_seconds"], 120)
        self.assertEqual(stored["upstream"]["idle_timeout_seconds"], 600)
        self.assertEqual(stored["user_custom_top"], {"keep": 1})

    def test_known_values_still_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            self._write(directory, {"upstream": {}})
            wb_settings.set_upstream_config(directory, {"header_timeout_seconds": 45})
            view = wb_settings.upstream_config(directory)
            stored = self._read(directory)
        self.assertEqual(view["header_timeout_seconds"], 45)
        self.assertEqual(stored["upstream"]["header_timeout_seconds"], 45)

    def test_deep_merge_is_recursive_and_pure(self):
        base = {"a": 1, "nested": {"x": 1, "keep": True}}
        patch = {"nested": {"x": 2}, "b": 3}
        merged = wb_settings.deep_merge(base, patch)
        self.assertEqual(merged, {"a": 1, "nested": {"x": 2, "keep": True}, "b": 3})
        self.assertEqual(base, {"a": 1, "nested": {"x": 1, "keep": True}})
        self.assertEqual(patch, {"nested": {"x": 2}, "b": 3})
        # A non-dict patch replaces the slot, matching a plain assignment.
        self.assertEqual(wb_settings.deep_merge({"a": 1}, None), None)
        self.assertEqual(wb_settings.deep_merge(None, {"a": 1}), {"a": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
