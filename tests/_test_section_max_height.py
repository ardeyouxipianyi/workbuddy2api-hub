"""The panel window height cap persists as one whole number of pixels.

    python tests/_test_section_max_height.py

The settings tab's "面板窗口限高" field caps how tall a dashboard window (a
section, and the log terminal) may get before it scrolls internally with its
title row pinned, instead of stretching the whole page. The value lives in
accounts/settings.json beside the other panel settings, so it inherits the
same lock and the same atomic replace, and the UI needs no second persistence
subsystem.

0 is a meaningful value - no cap - so the getter's job is to tell a real 0
apart from a broken file. Anything that is not a whole number reads back as
the default rather than raising: the file is hand-editable and this module
deliberately does not know how the dashboard renders, so the safe reading of
a broken value is "the default cap", never "windows a pixel tall". True is
named explicitly for that reason: bool is an int subclass, and a hand-edited
`true` would otherwise read as a 1px cap. A value past the ceiling is
clamped, not rejected, in both directions - same as credits_refresh_hours.

No network access required.
"""
import json
import os
import subprocess
import sys
import threading
import unittest
import unittest.mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from _isolated_dirs import isolated_data_dirs  # noqa: E402

# Its own accounts/ + usage/ before wb_proxy reads ACCOUNTS_DIR at import.
_TMP = isolated_data_dirs("wb-section-max-height-")

import wb_proxy as proxy  # noqa: E402
import wb_settings as S  # noqa: E402


class FakeRequest(object):
    """Just enough of the handler for the save route under test."""

    _handle_settings_save = proxy.Handler._handle_settings_save

    def __init__(self, payload):
        self.payload = payload
        self.answering = []

    def _payload_or_error(self, allow_list=False):
        return self.payload

    def _error(self, status, message, kind=""):
        self.answering.append(("error", status, message))
        return status, message

    def _json(self, status, payload):
        self.answering.append(("json", status, payload))
        return status, payload


class SectionMaxHeightTests(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.join(_TMP.name, "accounts")
        os.makedirs(self.dir, exist_ok=True)
        proxy.ACCOUNTS_DIR = self.dir
        path = S.settings_path(self.dir)
        if os.path.exists(path):
            os.unlink(path)

    def _write(self, data):
        with open(S.settings_path(self.dir), "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def _read(self):
        with open(S.settings_path(self.dir), encoding="utf-8") as fh:
            return json.load(fh)

    def _save(self, payload):
        """Run the save route; return the errors it answered with."""
        request = FakeRequest(payload)
        request._handle_settings_save()
        return [entry for entry in request.answering if entry[0] == "error"]

    def _reply(self, payload):
        """Run the save route; return the JSON reply it answered with."""
        request = FakeRequest(payload)
        request._handle_settings_save()
        replies = [entry for entry in request.answering if entry[0] == "json"]
        self.assertEqual(len(replies), 1, replies)
        return replies[0][2]

    def _view(self):
        return proxy.runtime_settings_view()["section_max_height"]

    # --- the stored value -------------------------------------------------

    def test_missing_key_reads_as_the_default(self):
        self.assertFalse(os.path.exists(S.settings_path(self.dir)))
        self.assertEqual(S.section_max_height(self.dir), S.DEFAULT_SECTION_MAX_HEIGHT)
        self.assertEqual(self._view(), S.DEFAULT_SECTION_MAX_HEIGHT)

    def test_a_value_round_trips(self):
        self.assertEqual(S.set_section_max_height(self.dir, 500), 500)
        self.assertEqual(S.section_max_height(self.dir), 500)
        self.assertEqual(self._read()["section_max_height"], 500)
        self.assertEqual(self._view(), 500)

    def test_zero_means_no_cap_and_survives_a_round_trip(self):
        # 0 must come back as 0, not fall back to the default: it is the "off"
        # switch, and the getter's default-fallback must not swallow it.
        self.assertEqual(S.set_section_max_height(self.dir, 0), 0)
        self.assertEqual(S.section_max_height(self.dir), 0)
        self.assertEqual(self._read()["section_max_height"], 0)
        self.assertEqual(self._view(), 0)

    def test_digits_in_a_string_are_accepted(self):
        # The same slack credits_refresh_hours gives a hand-edited file.
        self._write({"section_max_height": "500"})
        self.assertEqual(S.section_max_height(self.dir), 500)

    def test_a_float_truncates_to_whole_pixels(self):
        self._write({"section_max_height": 500.9})
        self.assertEqual(S.section_max_height(self.dir), 500)

    def test_a_transient_stat_failure_is_not_served_from_the_cache(self):
        # 这一条钉的是 CI 上那条偶发红（Windows 腿，142 passed / 1 failed）：
        # 先读一次「文件不存在」的状态——缓存里就留下那条空条目（import 期正是
        # 这个状态）——接着直接写盘（模拟人工编辑，绕过 save() 的主动失效），
        # 然后让**这一次** stat 失败（杀毒/索引器短暂持有刚写的文件）。读到的
        # 必须是文件里的值：把「stat 失败」当成「文件不存在」就会命中那条空缓存，
        # 返回默认值（现场症状：期望 500，读回 640）。
        self.assertFalse(os.path.exists(S.settings_path(self.dir)))
        self.assertEqual(S.section_max_height(self.dir), S.DEFAULT_SECTION_MAX_HEIGHT)
        self._write({"section_max_height": 500.9})
        real_stat = os.stat

        def flaky(path, *args, **kwargs):
            if str(path).endswith("settings.json"):
                raise PermissionError("transient lock (simulated)")
            return real_stat(path, *args, **kwargs)

        os.stat = flaky
        try:
            self.assertEqual(S.section_max_height(self.dir), 500)
        finally:
            os.stat = real_stat

    def test_a_transient_read_failure_is_not_cached(self):
        # 读盘被瞬时占用时不能把「空」写进缓存：文件本身是好的，下一次读要拿到
        # 真值（否则一次瞬时失败会一直读到默认值，直到文件再变）。
        self._write({"section_max_height": 700})
        real_open = open

        def flaky_open(path, *args, **kwargs):
            if str(path).endswith("settings.json"):
                raise PermissionError("transient lock (simulated)")
            return real_open(path, *args, **kwargs)

        with unittest.mock.patch.object(S, "open", flaky_open, create=True):
            self.assertEqual(S.section_max_height(self.dir), S.DEFAULT_SECTION_MAX_HEIGHT)
        self.assertEqual(S.section_max_height(self.dir), 700)

    def test_negative_reads_as_the_default(self):
        # Not as 0: a negative is a broken file, and 0 would silently switch
        # the cap off. Same reading credits_refresh_hours uses.
        self._write({"section_max_height": -5})
        self.assertEqual(S.section_max_height(self.dir), S.DEFAULT_SECTION_MAX_HEIGHT)

    def test_a_value_past_the_ceiling_is_clamped_not_rejected(self):
        self.assertEqual(S.set_section_max_height(self.dir, 99999),
                         S.MAX_SECTION_MAX_HEIGHT)
        self._write({"section_max_height": 99999})
        self.assertEqual(S.section_max_height(self.dir), S.MAX_SECTION_MAX_HEIGHT)
        self.assertEqual(self._view(), S.MAX_SECTION_MAX_HEIGHT)

    def test_the_setter_clamps_a_negative_to_off(self):
        # The route rejects negatives before they get here; a direct call
        # clamps to 0 like set_credits_refresh_hours does.
        self.assertEqual(S.set_section_max_height(self.dir, -5), 0)
        self.assertEqual(S.section_max_height(self.dir), 0)

    def test_malformed_values_read_as_the_default(self):
        for stored in ("abc", "640px", "", "  ", None, {}, [], [640], {}, 1e400,
                       float("nan"), float("inf")):
            with self.subTest(stored=stored):
                self._write({"section_max_height": stored})
                self.assertEqual(S.section_max_height(self.dir),
                                 S.DEFAULT_SECTION_MAX_HEIGHT)

    def test_a_boolean_reads_as_the_default(self):
        # bool is an int subclass: true would otherwise read as a 1px cap.
        for stored in (True, False):
            with self.subTest(stored=stored):
                self._write({"section_max_height": stored})
                self.assertEqual(S.section_max_height(self.dir),
                                 S.DEFAULT_SECTION_MAX_HEIGHT)

    def test_the_setter_treats_a_boolean_as_the_default(self):
        self.assertEqual(S.set_section_max_height(self.dir, True),
                         S.DEFAULT_SECTION_MAX_HEIGHT)
        self.assertEqual(S.set_section_max_height(self.dir, False),
                         S.DEFAULT_SECTION_MAX_HEIGHT)

    def test_a_new_process_sees_the_persisted_value(self):
        """A restart re-reads the file, so the cap is not process-local."""
        S.set_section_max_height(self.dir, 500)
        code = ("import sys; sys.path.insert(0, %r); import wb_settings as S; "
                "print(S.section_max_height(%r))" % (ROOT, self.dir))
        done = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "500")

    # --- living beside the other settings ---------------------------------

    def test_saving_the_value_preserves_unrelated_settings(self):
        self._write({
            "user_custom_top": {"keep": 1},
            "limits": {"global": {"reserve_credits": 7}},
            "panel_password": {"salt": "ab", "digest": "cd", "rounds": 1},
            "hidden_pages": ["tasks"],
        })
        S.set_section_max_height(self.dir, 500)
        stored = self._read()
        self.assertEqual(stored["user_custom_top"], {"keep": 1})
        self.assertEqual(stored["limits"], {"global": {"reserve_credits": 7}})
        self.assertEqual(stored["panel_password"],
                         {"salt": "ab", "digest": "cd", "rounds": 1})
        self.assertEqual(stored["hidden_pages"], ["tasks"])
        self.assertEqual(stored["section_max_height"], 500)

    def test_another_setting_write_keeps_the_value(self):
        S.set_section_max_height(self.dir, 500)
        S.set_hidden_pages(self.dir, ["logs"])
        self.assertEqual(S.section_max_height(self.dir), 500)
        self.assertEqual(S.hidden_pages(self.dir), ["logs"])

    def test_concurrent_writes_leave_a_readable_file(self):
        self._write({"user_custom_top": {"keep": 1}})
        errors = []

        def write(value):
            try:
                for _ in range(25):
                    S.set_section_max_height(self.dir, value)
            except Exception as exc:  # reported through the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(value,))
                   for value in (0, 480, 640, 720) * 2]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        stored = self._read()  # a truncated file would raise here
        self.assertIsInstance(stored.get("section_max_height"), int)
        self.assertEqual(stored.get("user_custom_top"), {"keep": 1})

    # --- the save route ---------------------------------------------------

    def test_save_route_stores_the_value_and_replies_with_it(self):
        reply = self._reply({"section_max_height": 500})
        self.assertEqual(reply["section_max_height"], 500)
        self.assertEqual(self._view(), 500)
        self.assertEqual(self._read()["section_max_height"], 500)

    def test_save_route_accepts_zero(self):
        self.assertEqual(self._save({"section_max_height": 0}), [])
        self.assertEqual(self._view(), 0)
        self.assertEqual(self._read()["section_max_height"], 0)

    def test_save_route_clamps_a_value_past_the_ceiling(self):
        reply = self._reply({"section_max_height": 99999})
        self.assertEqual(reply["section_max_height"], S.MAX_SECTION_MAX_HEIGHT)
        self.assertEqual(self._view(), S.MAX_SECTION_MAX_HEIGHT)

    def test_save_route_rejects_anything_that_is_not_a_whole_number(self):
        # True is the one worth naming: an int subclass, accepted it would
        # store a 1px cap and leave every window a sliver. inf is the other:
        # a JSON payload may carry Infinity, and int() raises OverflowError
        # on it - a 400, never a 500.
        for bad in (True, False, None, "abc", "640px", "", {}, [], [640], -1, -0.5,
                    float("inf"), float("nan")):
            with self.subTest(value=bad):
                errors = self._save({"section_max_height": bad})
                self.assertEqual(len(errors), 1, errors)
                self.assertEqual(errors[0][1], 400)
                self.assertEqual(S.section_max_height(self.dir),
                                 S.DEFAULT_SECTION_MAX_HEIGHT)

    def test_save_route_patch_leaves_other_settings_alone(self):
        self._write({"user_custom_top": {"keep": 1}, "hidden_pages": ["tasks"]})
        self.assertEqual(self._save({"section_max_height": 500}), [])
        stored = self._read()
        self.assertEqual(stored["user_custom_top"], {"keep": 1})
        self.assertEqual(stored["hidden_pages"], ["tasks"])

    def test_save_route_ignores_a_payload_that_omits_the_value(self):
        S.set_section_max_height(self.dir, 500)
        self.assertEqual(self._save({"user_custom_top": {"keep": 2}}), [])
        self.assertEqual(S.section_max_height(self.dir), 500)

    def test_the_view_carries_the_normalised_value(self):
        self._write({"section_max_height": 99999})
        self.assertEqual(self._view(), S.MAX_SECTION_MAX_HEIGHT)

    # --- where it lives ---------------------------------------------------

    def test_the_setting_lives_in_the_isolated_tree(self):
        S.set_section_max_height(self.dir, 500)
        path = os.path.abspath(S.settings_path(self.dir))
        self.assertTrue(path.startswith(os.path.abspath(_TMP.name)), path)
        self.assertFalse(path.startswith(os.path.abspath(os.path.join(ROOT, "accounts"))),
                         path)
        self.assertEqual(os.path.dirname(path), os.path.abspath(self.dir))


if __name__ == "__main__":
    unittest.main(verbosity=2)
