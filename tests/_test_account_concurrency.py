#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账号级并发上限（account_max_concurrent）的单元测试。

覆盖三层：
  1. 设置读写与值域（0 = 不限，1..10 合法，坏数据回退 0）
  2. 名额的占用/归还与幂等性（关闭时是空操作，绝不留状态）
  3. 名额绑定到响应对象的生命周期（流式回复读几十秒，必须占到 body 结束）

运行：python3 tests/_test_account_concurrency.py
"""
import json
import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy
import wb_settings


class _FakeAccount(object):
    def __init__(self, uid):
        self.uid = uid


class _FakeResponse(object):
    """形状对齐 _PooledResponse：有 close()，可被多次调用。"""

    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


class _SettingsDirMixin(object):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "accounts"), exist_ok=True)
        self._old_dir = wb_proxy.ACCOUNTS_DIR
        wb_proxy.ACCOUNTS_DIR = self.dir
        wb_proxy._ACCOUNT_INFLIGHT.clear()

    def tearDown(self):
        wb_proxy._ACCOUNT_INFLIGHT.clear()
        wb_proxy.ACCOUNTS_DIR = self._old_dir


class TestSettings(_SettingsDirMixin, unittest.TestCase):
    def test_default_is_unlimited(self):
        self.assertEqual(wb_settings.account_max_concurrent(self.dir), 0)

    def test_roundtrip(self):
        for value in (0, 1, 3, 10):
            wb_settings.set_account_max_concurrent(self.dir, value)
            self.assertEqual(wb_settings.account_max_concurrent(self.dir), value)

    def test_rejects_out_of_range_and_non_int(self):
        for bad in (-1, 11, "3", True, 2.5, None):
            with self.assertRaises(ValueError):
                wb_settings.set_account_max_concurrent(self.dir, bad)

    def test_corrupt_value_falls_back_to_unlimited(self):
        wb_settings.set_account_max_concurrent(self.dir, 3)
        path = wb_settings.settings_path(self.dir)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["account_max_concurrent"] = "garbage"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        wb_settings._load_cache.update({"key": None, "value": {}})
        self.assertEqual(wb_settings.account_max_concurrent(self.dir), 0)

    def test_out_of_range_on_disk_falls_back_to_unlimited(self):
        wb_settings.set_account_max_concurrent(self.dir, 3)
        path = wb_settings.settings_path(self.dir)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["account_max_concurrent"] = 99
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        wb_settings._load_cache.update({"key": None, "value": {}})
        self.assertEqual(wb_settings.account_max_concurrent(self.dir), 0)


class TestSlots(_SettingsDirMixin, unittest.TestCase):
    def test_disabled_is_noop_and_never_leaks(self):
        account = _FakeAccount("u1")
        for _ in range(50):
            self.assertTrue(wb_proxy.account_slot_acquire(account))
        # 关闭时不记账：没有快照，也不会因为「重复归还」出现负数。
        self.assertEqual(wb_proxy.account_inflight_snapshot(), {})
        wb_proxy.account_slot_release(account)
        wb_proxy.account_slot_release(account)
        self.assertEqual(wb_proxy.account_inflight_snapshot(), {})

    def test_limit_one_serialises(self):
        wb_settings.set_account_max_concurrent(self.dir, 1)
        account = _FakeAccount("u1")
        self.assertTrue(wb_proxy.account_slot_acquire(account))
        self.assertFalse(wb_proxy.account_slot_acquire(account))
        self.assertEqual(wb_proxy.account_inflight_snapshot(), {"u1": 1})
        wb_proxy.account_slot_release(account)
        self.assertEqual(wb_proxy.account_inflight_snapshot(), {})
        self.assertTrue(wb_proxy.account_slot_acquire(account))
        wb_proxy.account_slot_release(account)

    def test_limit_three_allows_three(self):
        wb_settings.set_account_max_concurrent(self.dir, 3)
        account = _FakeAccount("u1")
        results = [wb_proxy.account_slot_acquire(account) for _ in range(5)]
        self.assertEqual(results, [True, True, True, False, False])
        for _ in range(3):
            wb_proxy.account_slot_release(account)
        self.assertEqual(wb_proxy.account_inflight_snapshot(), {})

    def test_accounts_are_counted_independently(self):
        wb_settings.set_account_max_concurrent(self.dir, 1)
        a, b = _FakeAccount("a"), _FakeAccount("b")
        self.assertTrue(wb_proxy.account_slot_acquire(a))
        self.assertTrue(wb_proxy.account_slot_acquire(b))
        self.assertFalse(wb_proxy.account_slot_acquire(a))
        self.assertEqual(wb_proxy.account_inflight_snapshot(), {"a": 1, "b": 1})
        wb_proxy.account_slot_release(a)
        wb_proxy.account_slot_release(b)
        self.assertEqual(wb_proxy.account_inflight_snapshot(), {})

    def test_release_below_zero_is_safe(self):
        wb_settings.set_account_max_concurrent(self.dir, 1)
        account = _FakeAccount("u1")
        wb_proxy.account_slot_release(account)
        wb_proxy.account_slot_release(account)
        self.assertEqual(wb_proxy.account_inflight_snapshot(), {})


class TestResponseBinding(_SettingsDirMixin, unittest.TestCase):
    def test_slot_released_on_close(self):
        wb_settings.set_account_max_concurrent(self.dir, 1)
        account = _FakeAccount("u1")
        self.assertTrue(wb_proxy.account_slot_acquire(account))
        response = _FakeResponse()
        wb_proxy.bind_account_slot_to_response(response, account)
        # 绑定后仍占着：流式回复拿到头之后还要读几十秒。
        self.assertEqual(wb_proxy.account_inflight_snapshot(), {"u1": 1})
        self.assertFalse(wb_proxy.account_slot_acquire(account))
        response.close()
        self.assertEqual(wb_proxy.account_inflight_snapshot(), {})
        self.assertTrue(wb_proxy.account_slot_acquire(account))
        wb_proxy.account_slot_release(account)

    def test_repeated_close_releases_once(self):
        wb_settings.set_account_max_concurrent(self.dir, 1)
        account = _FakeAccount("u1")
        self.assertTrue(wb_proxy.account_slot_acquire(account))
        response = _FakeResponse()
        wb_proxy.bind_account_slot_to_response(response, account)
        for _ in range(5):
            response.close()
        # 只归还一次：再占仍应成功，且快照里不会出现负数残留。
        self.assertEqual(wb_proxy.account_inflight_snapshot(), {})
        self.assertTrue(wb_proxy.account_slot_acquire(account))
        wb_proxy.account_slot_release(account)

    def test_binding_without_acquire_still_cleans_up(self):
        # 关闭状态下绑定：归还必须是空操作，不能凭空造出一个负数计数。
        account = _FakeAccount("u1")
        response = _FakeResponse()
        wb_proxy.bind_account_slot_to_response(response, account)
        response.close()
        self.assertEqual(wb_proxy.account_inflight_snapshot(), {})

    def test_none_response_releases_immediately(self):
        wb_settings.set_account_max_concurrent(self.dir, 1)
        account = _FakeAccount("u1")
        self.assertTrue(wb_proxy.account_slot_acquire(account))
        wb_proxy.bind_account_slot_to_response(None, account)
        self.assertEqual(wb_proxy.account_inflight_snapshot(), {})




# ---------------------------------------------------------------------------
# 集成层：用真实 open_upstream + 打桩池驱动，确认「名额满就换号」而不是
# 「排队/失败」，并且任何失败路径都不泄漏名额。
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wb_accounts as A  # noqa: E402


class _Sock(object):
    def settimeout(self, value):
        pass


class _Body(object):
    """HTTPError 的 fp：够 read_http_error 读出正文即可。"""

    def __init__(self, data):
        self._data = data
        self._sent = False

    def read(self, *args):
        if self._sent:
            return b""
        self._sent = True
        return self._data

    def close(self):
        pass


class _UpstreamResponse(object):
    def __init__(self):
        self.closed = False

    @property
    def fp(self):
        sock = _Sock()

        class Raw(object):
            _sock = sock

        class Fp(object):
            raw = Raw()

        return Fp()

    def close(self):
        self.closed = True


class TestOpenUpstreamDispatch(unittest.TestCase):
    """名额满时换号，且失败路径不漏名额。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "accounts"), exist_ok=True)
        wb_proxy._ACCOUNT_INFLIGHT.clear()
        self._old_pool = wb_proxy.POOL
        self._old_urlopen = A.urlopen
        self._old_dir = wb_proxy.ACCOUNTS_DIR
        wb_proxy.ACCOUNTS_DIR = self.dir
        self.accounts = [self._make_account("uid-a"), self._make_account("uid-b")]
        wb_proxy.POOL = self._make_pool(self.accounts)
        self.hits = []

    def tearDown(self):
        wb_proxy.POOL = self._old_pool
        A.urlopen = self._old_urlopen
        wb_proxy.ACCOUNTS_DIR = self._old_dir
        wb_proxy._ACCOUNT_INFLIGHT.clear()

    def _make_account(self, uid):
        return A.Account(
            {"uid": uid, "accessToken": "t", "realm": "intl"},
            os.path.join(self.dir, uid + ".json"))

    def _make_pool(self, accounts):
        class Pool(object):
            affinity = type("Affinity", (), {
                "unbind": lambda self, key: None,
                "demote": lambda self, key, uid=None: None})()

            def count_ready(self, realm, model=None):
                return len(accounts)

            def pick_for_session(self, realm, session_key=None, exclude=(),
                                 model=None, page=None):
                for account in accounts:
                    if account.uid not in exclude:
                        return account
                return None

            def list_public(self):
                return []

            def apply_daily_token_limit(self, *a, **k):
                return 0

            def apply_daily_credit_limit(self, *a, **k):
                return 0

            def apply_model_daily_token_limit(self, *a, **k):
                return 0

            def apply_remaining_weights(self, weights=None):
                return weights or {}

        pool = Pool()
        pool.accounts = accounts
        return pool

    def _request(self):
        def urlopen(req, timeout=30, proxy=""):
            self.hits.append(req.full_url)
            return _UpstreamResponse()

        A.urlopen = urlopen
        return wb_proxy.open_upstream(
            {"model": "deepseek-v4.1-flash",
             "messages": [{"role": "user", "content": "hi"}]},
            target_realm="intl")

    def test_full_account_is_skipped_not_queued(self):
        wb_settings.set_account_max_concurrent(self.dir, 1)
        # 先把 uid-a 的名额占满，模拟它正在跑一个长流。
        self.assertTrue(wb_proxy.account_slot_acquire(self.accounts[0]))
        try:
            response, account, _effort = self._request()
            self.assertEqual(account.uid, "uid-b", "满号的账号应被跳过，改派另一个")
            self.assertEqual(len(self.hits), 1)
            response.close()
        finally:
            wb_proxy.account_slot_release(self.accounts[0])

    def test_slot_held_until_response_closed(self):
        wb_settings.set_account_max_concurrent(self.dir, 1)
        response, account, _effort = self._request()
        self.assertEqual(wb_proxy.account_inflight_snapshot(), {account.uid: 1})
        response.close()
        self.assertEqual(wb_proxy.account_inflight_snapshot(), {})

    def test_all_accounts_busy_reports_pool_busy(self):
        wb_settings.set_account_max_concurrent(self.dir, 1)
        for account in self.accounts:
            self.assertTrue(wb_proxy.account_slot_acquire(account))
        try:
            with self.assertRaises(Exception) as caught:
                self._request()
            self.assertEqual(self.hits, [], "全满时不该发出任何上游请求")
            self.assertIn("already serving", str(caught.exception))
        finally:
            for account in self.accounts:
                wb_proxy.account_slot_release(account)
        self.assertEqual(wb_proxy.account_inflight_snapshot(), {})

    def test_failed_attempt_does_not_leak_the_slot(self):
        wb_settings.set_account_max_concurrent(self.dir, 1)
        calls = []

        def urlopen(req, timeout=30, proxy=""):
            calls.append(1)
            raise urllib.error.URLError(ConnectionResetError(10054, "reset"))

        A.urlopen = urlopen
        with self.assertRaises(Exception):
            wb_proxy.open_upstream(
                {"model": "deepseek-v4.1-flash",
                 "messages": [{"role": "user", "content": "hi"}]},
                target_realm="intl")
        self.assertTrue(calls)
        self.assertEqual(wb_proxy.account_inflight_snapshot(), {},
                         "失败路径必须归还名额，否则账号会被永久占住")

    def test_http_400_does_not_leak_the_slot(self):
        """上游 400 原样透传时也必须归还名额。

        现场（2026-11-03）：`/v1/messages` 撞上游 11-128 渠道校验 → 400 分支
        直接 `raise`，跳过循环后的统一归还。`account_max_concurrent=1` 下这个
        账号从此永远显示「已在服务 1 个请求」，之后每次挑选都被当成满号跳过，
        面板看起来「号是好的、测试也绿」，但派发永远轮不到它——只有重启进程
        才恢复。回归点就是 raise 之前的归还。
        """
        wb_settings.set_account_max_concurrent(self.dir, 1)

        def urlopen(req, timeout=30, proxy=""):
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", {},
                _Body(b'{"code":11-128,"msg":"Illegal API invocation"}'))

        A.urlopen = urlopen
        with self.assertRaises(urllib.error.HTTPError):
            wb_proxy.open_upstream(
                {"model": "deepseek-v4.1-flash",
                 "messages": [{"role": "user", "content": "hi"}]},
                target_realm="intl")
        self.assertEqual(wb_proxy.account_inflight_snapshot(), {},
                         "400 透传路径必须归还名额")

    def test_other_unhandled_4xx_does_not_leak_the_slot(self):
        """未单独处理的 4xx（如 404 打错路径）走的是同一个 `raise`，同样要归还。"""
        wb_settings.set_account_max_concurrent(self.dir, 1)

        def urlopen(req, timeout=30, proxy=""):
            raise urllib.error.HTTPError(
                req.full_url, 404, "Not Found", {}, _Body(b"not found"))

        A.urlopen = urlopen
        with self.assertRaises(urllib.error.HTTPError):
            wb_proxy.open_upstream(
                {"model": "deepseek-v4.1-flash",
                 "messages": [{"role": "user", "content": "hi"}]},
                target_realm="intl")
        self.assertEqual(wb_proxy.account_inflight_snapshot(), {},
                         "未处理的 4xx 也必须归还名额")

    def test_disabled_matches_upstream_behaviour(self):
        # 默认 0：同一个账号可以连续接单，与上游行为一致。
        first, account_a, _ = self._request()
        second, account_b, _ = self._request()
        self.assertEqual(account_a.uid, account_b.uid)
        self.assertEqual(wb_proxy.account_inflight_snapshot(), {})
        first.close()
        second.close()




# ---------------------------------------------------------------------------
# 与「更稳的会话粘滞键」叠加：两个开关管的是不同的事，同时打开必须都生效。
#   并发上限：同一个账号同时最多跑 N 个请求（防上游 TPM 429）
#   会话键  ：两个会话不要算出同一个键（防每轮互换账号）
# ---------------------------------------------------------------------------
class TestWithStableAffinityKey(_SettingsDirMixin, unittest.TestCase):
    def _pool(self, count=8):
        import wb_accounts as A
        accounts = [A.Account({"uid": "u%02d" % i, "accessToken": "t",
                               "realm": "intl"},
                              os.path.join(self.dir, "u%02d.json" % i))
                    for i in range(count)]
        pool = A.AccountPool(self.dir)
        pool.accounts = accounts
        return pool

    @staticmethod
    def _turn(reply):
        return [{"role": "system", "content": "sys"},
                {"role": "user", "content": "帮我看看这个项目"},
                {"role": "assistant", "content": reply},
                {"role": "user", "content": "继续"}]

    def test_both_off_keeps_upstream_behaviour(self):
        pool = self._pool()
        key = wb_proxy.affinity_route(self._turn("回复 A"))[0]
        self.assertEqual(key, wb_proxy.affinity_route(self._turn("回复 B"))[0])
        # 上限关闭：不记账，重复占名额恒真。
        account = pool.get("u00")
        for _ in range(5):
            self.assertTrue(wb_proxy.account_slot_acquire(account))
        self.assertEqual(wb_proxy.account_inflight_snapshot(), {})

    def test_stable_key_alone_separates_conversations(self):
        wb_settings.set_affinity_stable_key_enabled(self.dir, True)
        pool = self._pool()
        key_a = wb_proxy.affinity_route(self._turn("回复 A"))[0]
        key_b = wb_proxy.affinity_route(self._turn("回复 B"))[0]
        self.assertNotEqual(key_a, key_b)
        first = pool.pick_for_session(realm="intl", session_key=key_a)
        second = pool.pick_for_session(realm="intl", session_key=key_b)
        self.assertNotEqual(first.uid, second.uid, "两会话应各占一个账号")
        # 第二轮各自回到自己的账号（粘滞成立）。
        self.assertEqual(pool.pick_for_session(realm="intl", session_key=key_a).uid,
                         first.uid)
        self.assertEqual(pool.pick_for_session(realm="intl", session_key=key_b).uid,
                         second.uid)

    def test_concurrency_cap_alone_switches_when_bound_account_is_busy(self):
        wb_settings.set_account_max_concurrent(self.dir, 1)
        pool = self._pool()
        key = wb_proxy.affinity_route(self._turn("回复 A"))[0]
        bound = pool.pick_for_session(realm="intl", session_key=key)
        self.assertTrue(wb_proxy.account_slot_acquire(bound))
        try:
            # 绑定的账号正在跑别的请求：这一轮必须换号（否则会违反上限）。
            other = pool.pick_for_session(realm="intl", session_key=key,
                                          exclude={bound.uid})
            self.assertNotEqual(other.uid, bound.uid)
        finally:
            wb_proxy.account_slot_release(bound)

    def test_both_on_each_session_keeps_its_own_account_and_slot(self):
        wb_settings.set_account_max_concurrent(self.dir, 1)
        wb_settings.set_affinity_stable_key_enabled(self.dir, True)
        pool = self._pool()
        key_a = wb_proxy.affinity_route(self._turn("回复 A"))[0]
        key_b = wb_proxy.affinity_route(self._turn("回复 B"))[0]
        self.assertNotEqual(key_a, key_b)

        held = []
        seq_a, seq_b = [], []
        for _ in range(3):
            # 两个会话同时在飞：各自选号并占名额。
            for key, seq in ((key_a, seq_a), (key_b, seq_b)):
                busy = {uid for _k, uid in held}
                account = pool.pick_for_session(realm="intl", session_key=key,
                                                exclude=busy)
                self.assertIsNotNone(account)
                self.assertTrue(wb_proxy.account_slot_acquire(account),
                                "每个会话都该占到自己的名额")
                held.append((key, account.uid))
                seq.append(account.uid)
            for _key, uid in held:
                wb_proxy.account_slot_release(pool.get(uid))
            held = []

        self.assertEqual(len(set(seq_a)), 1, "会话 A 每轮同一个账号")
        self.assertEqual(len(set(seq_b)), 1, "会话 B 每轮同一个账号")
        self.assertNotEqual(seq_a[0], seq_b[0], "两个会话不共用账号")
        self.assertEqual(wb_proxy.account_inflight_snapshot(), {})

    def test_both_on_still_releases_every_slot(self):
        wb_settings.set_account_max_concurrent(self.dir, 2)
        wb_settings.set_affinity_stable_key_enabled(self.dir, True)
        pool = self._pool()
        keys = [wb_proxy.affinity_route(self._turn("回复 %d" % i))[0]
                for i in range(4)]
        self.assertEqual(len(set(keys)), 4, "四段不同对话应算出四个键")
        held = []
        for key in keys:
            account = pool.pick_for_session(realm="intl", session_key=key)
            self.assertTrue(wb_proxy.account_slot_acquire(account))
            held.append(account)
        self.assertTrue(all(v <= 2 for v in
                            wb_proxy.account_inflight_snapshot().values()))
        for account in held:
            wb_proxy.account_slot_release(account)
        self.assertEqual(wb_proxy.account_inflight_snapshot(), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
