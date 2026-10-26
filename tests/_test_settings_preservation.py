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


class UsageRetentionTests(unittest.TestCase):
    """用量聚合保留窗口：面板可设、有默认、有上限，且环境变量仍然优先。

    这三个值决定 checkpoint 能覆盖多久（空间换时间），所以「面板没设过就是
    内置默认」和「环境变量优先」都要钉住——前者保证升级不改行为，后者保证
    现场调与测试里来回切的既有用法不被打断。
    """

    def _write(self, directory, data):
        with open(wb_settings.settings_path(directory), "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def test_defaults_when_unset(self):
        with tempfile.TemporaryDirectory() as directory:
            self._write(directory, {})
            self.assertEqual(wb_settings.usage_config(directory),
                             {"hour_keep_hours": 48, "day_keep_days": 32})

    def test_round_trip_and_preservation(self):
        with tempfile.TemporaryDirectory() as directory:
            self._write(directory, {
                "usage": {"unknown_usage_key": 9, "nested": {"deep": 1}},
            })
            wb_settings.set_usage_config(directory, {"hour_keep_hours": 120})
            with open(wb_settings.settings_path(directory), encoding="utf-8") as fh:
                stored = json.load(fh)
            view = wb_settings.usage_config(directory)
        self.assertEqual(view["hour_keep_hours"], 120)
        self.assertEqual(view["day_keep_days"], 32)          # 未设的一侧仍是默认
        self.assertEqual(stored["usage"]["unknown_usage_key"], 9)
        self.assertEqual(stored["usage"]["nested"], {"deep": 1})

    def test_validation_rejects_bad_values(self):
        for bad in ({"hour_keep_hours": 0}, {"hour_keep_hours": -1},
                    {"day_keep_days": "32"}, {"day_keep_days": True},
                    {"day_keep_days": 1.5}, {"nope": 1}):
            with self.assertRaises(ValueError):
                wb_settings.validate_usage_patch(bad)

    def test_validation_rejects_absurd_values(self):
        # 上限防手滑：贴错位数会把 checkpoint 撑到几百 MB 且每轮写盘重写。
        with self.assertRaises(ValueError):
            wb_settings.validate_usage_patch(
                {"hour_keep_hours": wb_settings.USAGE_LIMITS["hour_keep_hours"] + 1})
        with self.assertRaises(ValueError):
            wb_settings.validate_usage_patch(
                {"day_keep_days": wb_settings.USAGE_LIMITS["day_keep_days"] + 1})

    def test_corrupt_stored_value_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as directory:
            self._write(directory, {"usage": {"hour_keep_hours": "many",
                                              "day_keep_days": 99999}})
            view = wb_settings.usage_config(directory)
        self.assertEqual(view, {"hour_keep_hours": 48, "day_keep_days": 32})

    def test_readers_prefer_env_then_setting_then_default(self):
        import wb_proxy as P
        import wb_usage as U
        with tempfile.TemporaryDirectory() as directory:
            self._write(directory, {"usage": {"hour_keep_hours": 72,
                                              "day_keep_days": 8}})
            old_dir = P.ACCOUNTS_DIR
            P.ACCOUNTS_DIR = directory
            try:
                saved = {k: os.environ.get(k) for k in (
                    "WB_USAGE_DAY_KEEP_DAYS", "WB_USAGE_HOUR_KEEP_HOURS",
                    "WB_USAGE_HOUR_KEEP_DAYS")}
                for key in saved:
                    os.environ.pop(key, None)
                try:
                    self.assertEqual(U._usage_day_keep_days(), 8)
                    self.assertEqual(U._usage_hour_keep_hours(), 72)
                    # snapshot 的小时层按天剪枝，跟着同一个「小时层保留」走。
                    self.assertEqual(U._usage_hour_keep_days(), 3)
                    # 环境变量仍然优先（现场调 / 测试里来回切）。
                    os.environ["WB_USAGE_DAY_KEEP_DAYS"] = "2"
                    os.environ["WB_USAGE_HOUR_KEEP_HOURS"] = "24"
                    os.environ["WB_USAGE_HOUR_KEEP_DAYS"] = "1"
                    self.assertEqual(U._usage_day_keep_days(), 2)
                    self.assertEqual(U._usage_hour_keep_hours(), 24)
                    self.assertEqual(U._usage_hour_keep_days(), 1)
                finally:
                    for key, value in saved.items():
                        if value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = value
            finally:
                P.ACCOUNTS_DIR = old_dir


if __name__ == "__main__":
    unittest.main(verbosity=2)
