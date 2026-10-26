"""Retries that never started a turn must back off before trying again.

Round N of the web channel waits ``base * N`` seconds for the conversation to
finish. That budget only exists once the sandbox has been reached and the turn
has been sent: a failure at *create conversation* (upstream 5xx, e.g. the
AgentOS ``14220 failed to create initial session`` from issue #558) or at
*fetch sandbox* costs a few hundred milliseconds, so the old loop retried
immediately. With model fallback enabled that put every candidate in the same
one-second burst, and a ~100 s upstream hiccup swallowed the whole sweep.

The retry loop now sleeps between those rounds (``WEB_RETRY_BACKOFF`` base,
``WEB_RETRY_BACKOFF_MAX`` cap, 0 disables). Rounds that did reach the turn keep
the old behaviour: they already paid with the wait budget, and sleeping again
would punish the same failure twice.

No network for the first half: the upstream call is stubbed and ``time.sleep``
is recorded instead of slept. The last test drives the real ``_daily_chat_web_once``
against a local server that answers 500 with the issue's body.
"""
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-web-retry-backoff-")

import wb_accounts  # noqa: E402
import wb_settings  # noqa: E402

ACCOUNTS = os.path.join(_TMP.name, "accounts")

CREATE_FAIL = {"ok": False, "error": "HTTP 500: ...", "retryable": True,
               "phase": "create"}
TURN_FAIL = {"ok": False, "error": "会话在 120s 内没有跑完（状态=working）",
             "conversation": "c1", "retryable": True, "phase": "turn"}


def make_account(name, rounds=None, models=None):
    """轮数现在由候选序列决定：给几个候选就是几轮（至少两轮）。"""
    directory = os.path.join(ACCOUNTS, name)
    os.makedirs(directory, exist_ok=True)
    if models is None and rounds:
        models = ["cand-%d" % i for i in range(rounds)]
    if models is not None:
        wb_settings.set_daily_chat_models(directory, models)
    return wb_accounts.Account(
        {"uid": "u1", "realm": "intl", "accessToken": "t"},
        path=os.path.join(directory, "acc.json"))


class RetryBackoffTests(unittest.TestCase):
    def run_rounds(self, acc, results, base=None, cap=None):
        sleeps = []
        rounds = []

        def fake(prompt=None, wait_seconds=None, model=None):
            rounds.append(wait_seconds)
            return dict(results[len(rounds) - 1])

        patches = [mock.patch.object(acc, "_daily_chat_web_once", side_effect=fake),
                   mock.patch.object(time, "sleep", side_effect=sleeps.append)]
        if base is not None:
            patches.append(mock.patch.object(wb_accounts, "WEB_RETRY_BACKOFF", base))
        if cap is not None:
            patches.append(mock.patch.object(wb_accounts, "WEB_RETRY_BACKOFF_MAX", cap))
        for p in patches:
            p.start()
        try:
            res = acc._daily_chat_web_upstream()
        finally:
            for p in reversed(patches):
                p.stop()
        return sleeps, rounds, res

    def test_create_failures_sleep_between_rounds(self):
        acc = make_account("create")
        sleeps, rounds, res = self.run_rounds(acc, [CREATE_FAIL] * 3)
        self.assertFalse(res["ok"])
        # 自动模式只有一个候选 = 两轮（第二轮留给「会话只是慢」）。
        self.assertEqual(len(rounds), wb_accounts.WEB_MIN_ROUNDS)
        self.assertEqual(sleeps, [5.0])

    def test_backoff_doubles_and_stops_at_the_cap(self):
        acc = make_account("capped", rounds=5)
        sleeps, rounds, _res = self.run_rounds(acc, [CREATE_FAIL] * 5)
        self.assertEqual(len(rounds), 5)
        self.assertEqual(sleeps, [5.0, 10.0, 20.0, 30.0])

    def test_success_stops_the_sequence(self):
        acc = make_account("success")
        sleeps, _rounds, res = self.run_rounds(acc, [CREATE_FAIL, {"ok": True}])
        self.assertTrue(res["ok"])
        self.assertEqual(sleeps, [5.0])

    def test_turn_failures_do_not_sleep(self):
        # 跑过 turn 的轮次已经用等待预算付过账了，不该再睡一次。
        acc = make_account("turn")
        sleeps, _rounds, res = self.run_rounds(acc, [TURN_FAIL] * 3)
        self.assertFalse(res["ok"])
        self.assertEqual(sleeps, [])

    def test_results_without_phase_keep_the_old_timing(self):
        # 旧调用方 / 测试桩返回的结果里没有 phase：按「已跑到 turn」处理。
        acc = make_account("legacy")
        legacy = {"ok": False, "retryable": True}
        sleeps, _rounds, res = self.run_rounds(acc, [legacy] * 3)
        self.assertFalse(res["ok"])
        self.assertEqual(sleeps, [])

    def test_business_reject_does_not_sleep(self):
        acc = make_account("reject")
        reject = {"ok": False, "error": "code=14220 msg=...", "retryable": False,
                  "phase": "create"}
        sleeps, rounds, res = self.run_rounds(acc, [reject] * 3)
        self.assertFalse(res["ok"])
        self.assertEqual(len(rounds), 1)
        self.assertEqual(sleeps, [])

    def test_zero_base_disables_the_backoff(self):
        acc = make_account("disabled")
        sleeps, rounds, _res = self.run_rounds(acc, [CREATE_FAIL] * 3, base=0.0)
        self.assertEqual(len(rounds), wb_accounts.WEB_MIN_ROUNDS)
        self.assertEqual(sleeps, [])

    def test_model_fallback_rounds_also_back_off(self):
        # 回退模式下每一轮换一个候选模型，但失败仍停在建会话阶段，同样要退避。
        acc = make_account("fallback", models=["hy3", "glm-5.3-flash", "kimi-k2.6"])
        sleeps, rounds, _res = self.run_rounds(acc, [CREATE_FAIL] * 6)
        self.assertGreaterEqual(len(rounds), 3)
        self.assertEqual(sleeps, [5.0, 10.0, 20.0, 30.0][:len(rounds) - 1])


class RetryBackoffSettingTests(unittest.TestCase):
    """设置页的「建会话失败退避」覆盖内置默认，0 就是关闭。"""

    def run_rounds(self, acc, results):
        sleeps = []
        rounds = []

        def fake(prompt=None, wait_seconds=None, model=None):
            rounds.append(wait_seconds)
            return dict(results[len(rounds) - 1])

        with mock.patch.object(acc, "_daily_chat_web_once", side_effect=fake), \
                mock.patch.object(time, "sleep", side_effect=sleeps.append):
            res = acc._daily_chat_web_upstream()
        return sleeps, rounds, res

    def test_setting_overrides_the_builtin_default(self):
        acc = make_account("setting-2")
        wb_settings.set_daily_chat_web_retry_backoff(
            os.path.dirname(acc.path), 2)
        sleeps, _rounds, _res = self.run_rounds(acc, [CREATE_FAIL] * 3)
        self.assertEqual(sleeps, [2.0])

    def test_setting_zero_disables_the_backoff(self):
        acc = make_account("setting-0")
        wb_settings.set_daily_chat_web_retry_backoff(
            os.path.dirname(acc.path), 0)
        sleeps, rounds, _res = self.run_rounds(acc, [CREATE_FAIL] * 3)
        self.assertEqual(len(rounds), wb_accounts.WEB_MIN_ROUNDS)
        self.assertEqual(sleeps, [])

    def test_setting_larger_than_the_cap_is_not_clamped_away(self):
        # 单次上限取 max(30, base)，否则操作者把基数调大后会被悄悄压回去。
        acc = make_account("setting-40")
        wb_settings.set_daily_chat_web_retry_backoff(
            os.path.dirname(acc.path), 40)
        sleeps, _rounds, _res = self.run_rounds(acc, [CREATE_FAIL] * 3)
        self.assertEqual(sleeps, [40.0])

    def test_unset_setting_falls_back_to_the_builtin_default(self):
        acc = make_account("setting-unset")
        self.assertEqual(acc.daily_chat_web_retry_backoff(),
                         wb_accounts.WEB_RETRY_BACKOFF)

    def test_hand_edited_values_fall_back_to_the_default(self):
        directory = os.path.join(ACCOUNTS, "setting-bad")
        os.makedirs(directory, exist_ok=True)
        for bad in (-1, 9999, True, "5", 2.5, None):
            with mock.patch.object(wb_settings, "load",
                                   return_value={"daily_chat_web_retry_backoff": bad}):
                self.assertEqual(
                    wb_settings.daily_chat_web_retry_backoff(directory, default=7), 7)

    def test_setter_rejects_out_of_range_values(self):
        directory = os.path.join(ACCOUNTS, "setting-reject")
        os.makedirs(directory, exist_ok=True)
        for bad in (-1, 61, 2.5, True, "5"):
            with self.assertRaises(ValueError):
                wb_settings.set_daily_chat_web_retry_backoff(directory, bad)
        self.assertEqual(wb_settings.set_daily_chat_web_retry_backoff(directory, 0), 0)
        self.assertEqual(wb_settings.daily_chat_web_retry_backoff(directory), 0)


class RealCreateFailureTests(unittest.TestCase):
    """The real ``_daily_chat_web_once`` must report ``phase=create``."""

    def test_upstream_500_is_a_create_phase_failure(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.dumps({
                    "code": 10000,
                    "msg": "CreateConversation: 10000:14284:AgentOS API error: "
                           "code=14220, msg=failed to create initial session",
                }).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = "http://127.0.0.1:%d/console/as/conversations/" % server.server_address[1]
        acc = make_account("real", rounds=2)
        try:
            with mock.patch.object(wb_accounts, "WEB_CONVERSATIONS_URL", url), \
                    mock.patch.object(wb_accounts, "WEB_RETRY_BACKOFF", 0.2), \
                    mock.patch.object(wb_accounts, "WEB_RETRY_BACKOFF_MAX", 0.2):
                started = time.monotonic()
                res = acc._daily_chat_web_upstream()
                elapsed = time.monotonic() - started
        finally:
            server.shutdown()
            server.server_close()
        self.assertFalse(res["ok"])
        self.assertEqual(res.get("phase"), "create")
        self.assertIn("14220", res.get("error") or "")
        self.assertEqual(res.get("attempts"), 2)
        # 两轮之间真的睡了一次（0.2s），不是背靠背。阈值留出调度抖动：
        # Windows 上 time.sleep(0.05) 实测偶尔只走 47ms，卡在边界上会假红。
        self.assertGreaterEqual(elapsed, 0.15)


if __name__ == "__main__":
    unittest.main(verbosity=2)
