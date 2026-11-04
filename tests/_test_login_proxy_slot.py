#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""添加账号（OAuth 登录）的出口代理槽。

钉住：
  1. 不选槽（proxySlot 为空）时登录直连，与没有这个选项之前完全一样——
     哪怕配置了代理槽也不会被悄悄用上；
  2. proxySlot=auto 时从未占用的槽里随机挑，不按列表顺序，并发登录不撞槽；
  3. 保存账号时的出口绑定（自动重登保留原槽、槽不写进 legacy url）；
  4. 代理 URL 可能带 user:pass：/accounts/login/start 的日志与响应只给 host:port。

运行：python3 tests/_test_login_proxy_slot.py
"""
import json
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_proxy


AUTO = wb_accounts.LOGIN_SLOT_AUTO


class _Acct(object):
    def __init__(self, slot, uid=""):
        self.proxy_slot = slot
        self.uid = uid


def _pool(slots, bound=(), held=()):
    # 只用到 _login_proxy 读的字段，避免依赖完整的 AccountPool 构造。
    pool = wb_accounts.AccountPool.__new__(wb_accounts.AccountPool)
    pool.dir = "/nonexistent"
    pool._lock = threading.RLock()
    pool.accounts = [_Acct(s) for s in bound]
    pool.logins = {"st%d" % i: {"proxy_slot": s, "created": time.time()}
                   for i, s in enumerate(held)}
    pool._login_slot_reserved = set()
    patch = mock.patch.object(wb_accounts.wb_settings, "proxy_slots", return_value=[
        {"id": sid, "url": "u-" + sid, "enabled": en} for sid, en in slots])
    return pool, patch


class LoginProxyRandomTest(unittest.TestCase):
    def test_picks_only_free_enabled_slots_and_spreads(self):
        slots = [("s1", True), ("s2", True), ("s3", False), ("s4", True), ("s5", True), ("s6", True)]
        pool, patch = _pool(slots, bound=["s1"], held=["s2"])
        picks = set()
        with patch:
            for _ in range(300):
                picks.add(pool._login_proxy(AUTO)[0])
                pool._login_slot_reserved.clear()
        self.assertEqual(picks, {"s4", "s5", "s6"})

    def test_pick_is_reserved_until_released(self):
        pool, patch = _pool([("s1", True), ("s2", True)])
        with patch:
            first = pool._login_proxy(AUTO)[0]
            second = pool._login_proxy(AUTO)[0]
            self.assertEqual({first, second}, {"s1", "s2"})
            self.assertEqual(pool._login_proxy(AUTO), ("", ""))

    def test_expired_login_no_longer_holds_its_slot(self):
        pool, patch = _pool([("s1", True)], held=["s1"])
        pool.logins["st0"]["created"] = time.time() - wb_accounts.LOGIN_TTL_SECONDS - 1
        with patch:
            self.assertEqual(pool._login_proxy(AUTO), ("s1", "u-s1"))

    def test_no_slot_given_stays_direct_even_with_free_slots(self):
        pool, patch = _pool([("s1", True), ("s2", True)])
        with patch:
            self.assertEqual(pool._login_proxy(), ("", ""))
            self.assertEqual(pool._login_proxy(""), ("", ""))
        self.assertEqual(pool._login_slot_reserved, set())

    def test_default_start_login_is_direct_and_not_auto(self):
        pool, patch = _pool([("s1", True)])
        pool._start_login = mock.Mock(return_value={"state": "x"})
        with patch:
            pool.start_login()
        pool._start_login.assert_called_once_with("intl", "CLI", "", "", False)

    def test_auto_start_login_releases_its_reservation(self):
        pool, patch = _pool([("s1", True)])
        pool._start_login = mock.Mock(side_effect=OSError("down"))
        with patch, self.assertRaises(OSError):
            pool.start_login(proxy_slot=AUTO)
        pool._start_login.assert_called_once_with("intl", "CLI", "s1", "u-s1", True)
        self.assertEqual(pool._login_slot_reserved, set())

    def test_auto_skips_slots_bound_elsewhere(self):
        pool, patch = _pool([("s1", True), ("s2", True)])
        with patch:
            for _ in range(50):
                self.assertEqual(pool._login_proxy(AUTO, {"s1"})[0], "s2")
                pool._login_slot_reserved.clear()
            self.assertEqual(pool._login_proxy(AUTO, {"s1", "s2"}), ("", ""))

    def test_all_taken_falls_back_to_direct(self):
        pool, patch = _pool([("s1", True)], bound=["s1"])
        with patch:
            self.assertEqual(pool._login_proxy(AUTO), ("", ""))

    def test_explicit_choices_win(self):
        pool, patch = _pool([("s1", True), ("s2", True), ("s3", False)], bound=["s1"])
        with patch:
            self.assertEqual(pool._login_proxy("s1"), ("s1", "u-s1"))
            with self.assertRaises(wb_accounts.LoginProxySlotError):
                pool._login_proxy("nope")
            with self.assertRaises(wb_accounts.LoginProxySlotError):
                pool._login_proxy("s3")

    def test_explicit_login_does_not_release_anothers_reservation(self):
        pool, patch = _pool([("s1", True), ("s2", True)])
        pool._login_slot_reserved.add("s2")  # an in-flight auto pick
        pool._start_login = mock.Mock(side_effect=OSError("down"))
        with patch, self.assertRaises(OSError):
            pool.start_login(proxy_slot="s2")
        self.assertIn("s2", pool._login_slot_reserved)


class PollLoginBindingTest(unittest.TestCase):
    """poll_login 保存账号时的出口绑定。"""

    def _poll(self, info, existing_slot=None):
        pool = wb_accounts.AccountPool.__new__(wb_accounts.AccountPool)
        pool._lock = threading.RLock()
        pool.logins = {"st": dict(info, created=time.time(), platform="CLI", realm="intl")}
        saved = []
        existing = _Acct(existing_slot, "u1") if existing_slot else None
        pool.get = lambda uid: existing
        pool.add = saved.append
        token = {"code": 0, "data": {"accessToken": "tok", "refreshToken": "r"}}
        with mock.patch.object(wb_accounts, "http_json", return_value=token), \
             mock.patch.object(wb_accounts, "urlopen", side_effect=OSError("offline")), \
             mock.patch.object(wb_accounts, "jwt_uid", return_value="u1"), \
             mock.patch.object(wb_accounts, "jwt_exp", return_value=0):
            pool.poll_login("st")
        self.assertEqual(len(saved), 1)
        return saved[0]

    def test_new_account_binds_slot_without_legacy_url(self):
        acct = self._poll({"proxy_slot": "s4", "proxy": "u-s4", "auto_slot": True})
        self.assertEqual((acct.proxy_slot, acct.proxy_legacy), ("s4", ""))

    def test_relogin_with_auto_pick_keeps_existing_slot(self):
        acct = self._poll({"proxy_slot": "s4", "proxy": "u-s4", "auto_slot": True},
                          existing_slot="s1")
        self.assertEqual(acct.proxy_slot, "")  # add() then keeps the existing s1
        self.assertEqual(acct.proxy_legacy, "")

    def test_relogin_with_explicit_slot_rebinds(self):
        acct = self._poll({"proxy_slot": "s4", "proxy": "u-s4", "auto_slot": False},
                          existing_slot="s1")
        self.assertEqual(acct.proxy_slot, "s4")

    def test_direct_login_profile_fetch_uses_plain_urllib(self):
        pool = wb_accounts.AccountPool.__new__(wb_accounts.AccountPool)
        pool._lock = threading.RLock()
        pool.logins = {"st": {"proxy_slot": "", "proxy": "", "auto_slot": False,
                              "created": time.time(), "platform": "CLI", "realm": "intl"}}
        saved = []
        pool.get = lambda uid: None
        pool.add = saved.append
        token = {"code": 0, "data": {"accessToken": "tok", "refreshToken": "r"}}
        with mock.patch.object(wb_accounts, "http_json", return_value=token), \
             mock.patch.object(wb_accounts, "urlopen", side_effect=AssertionError("pooled")) as pooled, \
             mock.patch.object(wb_accounts.urllib.request, "urlopen",
                               side_effect=OSError("offline")) as plain, \
             mock.patch.object(wb_accounts, "jwt_uid", return_value="u1"), \
             mock.patch.object(wb_accounts, "jwt_exp", return_value=0):
            pool.poll_login("st")
        self.assertFalse(pooled.called)
        self.assertTrue(plain.called)
        self.assertEqual((saved[0].proxy_slot, saved[0].proxy_legacy), ("", ""))

    def test_relogin_direct_keeps_existing_bindings(self):
        acct = self._poll({"proxy_slot": "", "proxy": "", "auto_slot": False},
                          existing_slot="s1")
        self.assertEqual((acct.proxy_slot, acct.proxy_legacy), ("", ""))  # add() keeps s1


SECRET = "s3cr3t-pw"
CRED_URL = "http://alice:%s@proxy.example:8080" % SECRET


class LoginStartRouteRedactionTest(unittest.TestCase):
    """/accounts/login/start 的日志与响应不得带出代理 URL 里的用户名密码。"""

    class Handler(wb_proxy.Handler):
        def __init__(self):
            self.captured = None

        def _json(self, code, obj):
            self.captured = (code, obj)

        def _error(self, code, message, err_type="server_error"):
            self.captured = (code, {"error": message})

    def _start(self, started=None, exc=None, payload=None, codearts_slots=()):
        logs = []
        pool = mock.Mock()
        if exc is not None:
            pool.start_login.side_effect = exc
        else:
            pool.start_login.return_value = started
        handler = self.Handler()
        codearts = mock.Mock(accounts=[_Acct(s) for s in codearts_slots])
        with mock.patch.object(wb_proxy, "POOL", pool), \
             mock.patch.object(wb_proxy.wb_codearts, "pool_for", return_value=codearts), \
             mock.patch.object(wb_proxy, "log", lambda msg, *a, **k: logs.append(msg)):
            handler._route_accounts_login_start(
                {"proxySlot": "slot-1"} if payload is None else payload)
        return handler.captured, logs, pool

    def test_slot_with_credentials_is_redacted(self):
        (code, body), logs, pool = self._start({
            "state": "abcdefgh1234", "authUrl": "https://auth.example/x",
            "realm": "intl", "platform": "CLI",
            "proxySlot": "slot-1", "proxy": CRED_URL})
        self.assertEqual(code, 200)
        self.assertEqual(pool.start_login.call_args[1]["proxy_slot"], "slot-1")
        self.assertEqual(body["proxy"], "proxy.example:8080")
        self.assertEqual(body["proxySlot"], "slot-1")
        dumped = json.dumps(body) + "\n".join(logs)
        self.assertNotIn(SECRET, dumped)
        self.assertNotIn("alice", dumped)
        self.assertTrue(any("proxy=slot-1 proxy.example:8080" in m for m in logs), logs)

    def test_direct_login_logs_direct(self):
        (code, body), logs, _ = self._start({
            "state": "abcdefgh1234", "authUrl": "https://auth.example/x",
            "realm": "intl", "platform": "CLI", "proxySlot": "", "proxy": ""})
        self.assertEqual(code, 200)
        self.assertEqual(body["proxy"], "")
        self.assertTrue(any("proxy=direct" in m for m in logs), logs)

    def test_unknown_slot_is_400(self):
        (code, body), _, _ = self._start(
            exc=wb_accounts.LoginProxySlotError("proxy slot x does not exist or is disabled"))
        self.assertEqual(code, 400)

    def test_raw_proxy_url_in_payload_is_ignored(self):
        _, _, pool = self._start({"state": "abcdefgh1234", "proxySlot": "", "proxy": ""},
                                 payload={"proxy": CRED_URL})
        self.assertEqual(pool.start_login.call_args[1],
                         {"realm": mock.ANY, "platform": "CLI", "proxy_slot": None,
                          "taken_slots": set()})

    def test_auto_passes_codearts_bindings(self):
        _, _, pool = self._start({"state": "abcdefgh1234", "proxySlot": "", "proxy": ""},
                                 payload={"proxySlot": AUTO}, codearts_slots=["slot-2", ""])
        self.assertEqual(pool.start_login.call_args[1]["taken_slots"], {"slot-2"})

    def test_non_string_slot_is_400(self):
        (code, _), _, pool = self._start(payload={"proxySlot": {"a": 1}})
        self.assertEqual(code, 400)
        self.assertFalse(pool.start_login.called)

    def test_other_value_errors_stay_502(self):
        (code, _), _, _ = self._start(exc=ValueError("bad json from upstream"))
        self.assertEqual(code, 502)


class LoginProxyPanelTest(unittest.TestCase):
    """面板：默认直连；新文案在英文词典（DICT）和正体词典（HANT_PHRASES）里都有。"""

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "dashboard.html"), encoding="utf-8") as fh:
            cls.html = fh.read()

    def test_default_option_is_direct(self):
        at = self.html.index('<select id="loginProxySlot"')
        first = self.html[at:self.html.index("</option>", at)]
        self.assertIn('<option value="">直连（不走代理槽）', first)
        self.assertIn('id="loginProxyBox" style="display:none;', self.html)

    def test_new_texts_in_both_dictionaries(self):
        for text in ("登录出口代理（申请授权链接与等待回调都走它）：",
                     "直连（不走代理槽）", "自动（随机挑选未使用的代理槽）",
                     "本次登录出口：", "本次登录直连",
                     "没有空闲的代理槽，本次登录直连"):
            self.assertIn("'%s':" % text, self.html, "DICT 缺英文词条：" + text)
            self.assertIn('"%s":' % text, self.html, "HANT_PHRASES 缺正体词条：" + text)
        self.assertIn("[/已绑定 (\\d+) 个账号/", self.html)


if __name__ == "__main__":
    unittest.main()
