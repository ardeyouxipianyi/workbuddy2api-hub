# -*- coding: utf-8 -*-
"""C 引擎接缝：默认关、缺库静默回退、请求形状与归属先决条件。

    python tests/_test_agg_engine.py

`wb_agg_engine` 只接两条「每次请求都要全量重扫」的兜底路径（窗口快照的兜底
扫描、序列的整段扫描），开关 `WB_AGG_ENGINE` 默认关。这个套件钉三件事：

  * 开关语义：只有 1/on/true/yes 且库可用才算开；任何其它值、库不在场、
    库调用失败、返回形状不对——接缝一律返回 None（调用方走 Python）；
  * 归属先决条件：引擎只认账号文件里的 uid/realm，Python 还会从 token 推导
    realm 并跳过没有 uid 的文件；两者对不上就不加速（宁可不加速也不能让
    realm 过滤/分组分叉）；
  * 接缝的请求形状：段名、窗口边界（0/None 表示无界）、series_realm、
    价表三件套只在需要算价的段上带。

库侧（C 引擎本身）由 native/parity.py 与 native/tests_*.c 覆盖，CI 没有
编译器，所以这里用桩模块把 wb_native_agg 顶掉，只测接缝自己的判断。

Run with: python tests/_test_agg_engine.py
No network, no credentials: temporary directories only.
"""
import json
import os
import sys
import tempfile
import threading
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from _isolated_dirs import isolated_data_dirs  # noqa: E402

# 它自己的 accounts/ + usage/：wb_proxy 在 import 时就会读 ACCOUNTS_DIR。
_TMP = isolated_data_dirs("wb-agg-engine-")

import wb_agg_engine as E  # noqa: E402
import wb_proxy as proxy  # noqa: E402
import wb_pricing  # noqa: E402


class FakeAccount(object):
    def __init__(self, uid, realm="intl"):
        self.uid = uid
        self.realm = realm


class FakePool(object):
    def __init__(self, uids):
        self.accounts = [FakeAccount(uid) for uid in uids]


class StubLib(object):
    """顶掉 wb_native_agg 的桩：记录请求，按脚本返回。"""

    def __init__(self, result=None, available=True):
        self.result = result
        self.available_value = available
        self.requests = []
        self.raised = None

    def available(self):
        return self.available_value

    def run(self, request):
        self.requests.append(request)
        if self.raised is not None:
            raise self.raised
        return self.result


def _noop_log(_message):
    pass


class SeamTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wb-agg-engine-")
        self.accounts = os.path.join(self.dir, "accounts")
        os.makedirs(self.accounts)
        self.saved_env = os.environ.get("WB_AGG_ENGINE")
        self.saved_module = sys.modules.get("wb_native_agg")

    def tearDown(self):
        if self.saved_env is None:
            os.environ.pop("WB_AGG_ENGINE", None)
        else:
            os.environ["WB_AGG_ENGINE"] = self.saved_env
        if self.saved_module is None:
            sys.modules.pop("wb_native_agg", None)
        else:
            sys.modules["wb_native_agg"] = self.saved_module

    def install(self, lib, engine="1"):
        sys.modules["wb_native_agg"] = lib
        if engine is None:
            os.environ.pop("WB_AGG_ENGINE", None)
        else:
            os.environ["WB_AGG_ENGINE"] = engine
        return lib

    def write_account(self, uid, realm="intl", name=None):
        path = os.path.join(self.accounts, name or (uid + ".json"))
        with open(path, "w", encoding="utf-8") as fh:
            data = {"uid": uid}
            if realm is not None:
                data["realm"] = realm
            json.dump(data, fh)
        return path


class SwitchTests(SeamTestCase):
    def test_default_is_off(self):
        os.environ.pop("WB_AGG_ENGINE", None)
        self.install(StubLib(result={}), engine=None)
        os.environ.pop("WB_AGG_ENGINE", None)
        self.assertFalse(E.enabled())

    def test_only_truthy_words_switch_it_on(self):
        lib = StubLib(result={})
        for word in ("1", "on", "true", "yes", "ON", "True", " 1 "):
            self.install(lib, engine=word)
            self.assertTrue(E.enabled(), word)
        for word in ("0", "off", "false", "no", "", "maybe", "2"):
            self.install(lib, engine=word)
            self.assertFalse(E.enabled(), word)

    def test_a_missing_library_is_off(self):
        self.install(StubLib(available=False), engine="1")
        self.assertFalse(E.enabled())

    def test_an_exploding_library_is_off(self):
        class Exploding(object):
            def available(self):
                raise RuntimeError("boom")
        self.install(Exploding(), engine="1")
        self.assertFalse(E.enabled())


class AttributionGuardTests(SeamTestCase):
    def test_matching_directory_and_pool_pass(self):
        self.write_account("acct-a", "intl")
        self.write_account("acct-b", "cn")
        self.assertTrue(E._attribution_inputs_ok(self.accounts, FakePool(["acct-a", "acct-b"])))

    def test_a_pool_member_without_a_file_fails(self):
        self.write_account("acct-a", "intl")
        self.assertFalse(E._attribution_inputs_ok(self.accounts, FakePool(["acct-a", "acct-b"])))

    def test_a_file_without_realm_fails(self):
        # Python 会从 token 推导 realm，引擎不会：这种文件不加速。
        self.write_account("acct-a", "intl")
        self.write_account("acct-b", None)
        self.assertFalse(E._attribution_inputs_ok(self.accounts, FakePool(["acct-a", "acct-b"])))

    def test_a_file_without_uid_fails(self):
        with open(os.path.join(self.accounts, "x.json"), "w", encoding="utf-8") as fh:
            json.dump({"realm": "intl"}, fh)
        self.assertFalse(E._attribution_inputs_ok(self.accounts, FakePool([])))

    def test_a_corrupt_file_fails(self):
        with open(os.path.join(self.accounts, "bad.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertFalse(E._attribution_inputs_ok(self.accounts, FakePool([])))

    def test_an_empty_directory_with_an_empty_pool_passes(self):
        self.assertTrue(E._attribution_inputs_ok(self.accounts, FakePool([])))

    def test_an_empty_directory_with_a_pool_fails(self):
        self.assertFalse(E._attribution_inputs_ok(self.accounts, FakePool(["acct-a"])))

    def test_a_missing_directory_matches_an_empty_pool(self):
        self.assertTrue(E._attribution_inputs_ok(os.path.join(self.dir, "nope"), None))
        self.assertFalse(E._attribution_inputs_ok(
            os.path.join(self.dir, "nope"), FakePool(["acct-a"])))


class RequestShapeTests(SeamTestCase):
    def setUp(self):
        super(RequestShapeTests, self).setUp()
        self.write_account("acct-a", "intl")

    def payload(self, section, value):
        return {"snapshot": value} if section == "snapshot" else {"series": value}

    def test_snapshot_request_carries_the_window_and_pricing(self):
        lib = self.install(StubLib(result={"snapshot": {"requests": 3}}))
        out = E.snapshot_window(_noop_log, "usage.jsonl", self.accounts, "cn",
                                100.0, 200.0, pool=FakePool(["acct-a"]),
                                pricing_on=True)
        self.assertEqual(out, {"requests": 3})
        request = lib.requests[0]
        self.assertEqual(request["log"], "usage.jsonl")
        self.assertEqual(request["sections"], ["snapshot"])
        self.assertEqual(request["realm"], "cn")
        self.assertEqual(request["default_realm"], "intl")
        self.assertEqual(request["accounts"], self.accounts)
        self.assertEqual((request["since"], request["until"]), (100.0, 200.0))
        self.assertTrue(request["has_since"])
        self.assertTrue(request["has_until"])
        self.assertIn("pricing", request)

    def test_an_open_bound_travels_as_has_false(self):
        lib = self.install(StubLib(result={"snapshot": {"requests": 1}}))
        E.snapshot_window(_noop_log, "usage.jsonl", self.accounts, None, None, None,
                          pool=FakePool(["acct-a"]), pricing_on=True)
        request = lib.requests[0]
        self.assertFalse(request["has_since"])
        self.assertFalse(request["has_until"])
        self.assertEqual(request["realm"], "all")

    def test_the_series_request_uses_series_realm_and_skips_pricing(self):
        lib = self.install(StubLib(result={"series": {"series": [], "bucket_seconds": 60}}))
        out = E.series(_noop_log, "usage.jsonl", self.accounts, "intl",
                       10.0, 20.0, 60, pool=FakePool(["acct-a"]))
        self.assertEqual(out["ok"], True)
        self.assertEqual(out["realm"], "intl")
        request = lib.requests[0]
        self.assertEqual(request["sections"], ["series"])
        self.assertEqual(request["series_realm"], "intl")
        self.assertEqual(request["step"], 60)
        self.assertTrue(request["has_since"] and request["has_until"])
        self.assertNotIn("pricing", request)

    def test_a_disabled_pricing_switch_keeps_the_snapshot_on_python(self):
        lib = self.install(StubLib(result={"snapshot": {"requests": 3}}))
        self.assertIsNone(E.snapshot_window(
            _noop_log, "usage.jsonl", self.accounts, "cn", None, None,
            pool=FakePool(["acct-a"]), pricing_on=False))
        self.assertEqual(lib.requests, [])

    def test_the_series_path_does_not_depend_on_pricing(self):
        # 序列桶里没有成本字段：总开关关着也照样能用引擎。
        lib = self.install(StubLib(result={"series": {"series": []}}))
        self.assertIsNotNone(E.series(_noop_log, "usage.jsonl", self.accounts,
                                      None, 0.0, 1.0, 60, pool=FakePool(["acct-a"])))
        self.assertEqual(len(lib.requests), 1)

    def test_the_series_payload_is_float_normalised(self):
        # 引擎把 at/since/until/credit 打成整数文本，Python 那边是 float：
        # 值相同、文本不同就不能声称逐字节相同，接缝逐字段转 float。
        engine_series = {"bucket_seconds": 86400, "since": 1791504000,
                         "until": 1791590400,
                         "series": [{"at": 1791504000, "requests": 1, "credit": 0}],
                         "credits": [{"at": 1791504000, "credit": 1.5}]}
        self.install(StubLib(result={"series": engine_series}))
        out = E.series(_noop_log, "usage.jsonl", self.accounts, None,
                       1791504000.0, 1791590400.0, 86400, pool=FakePool(["acct-a"]))
        self.assertIsInstance(out["since"], float)
        self.assertIsInstance(out["until"], float)
        self.assertIsInstance(out["series"][0]["at"], float)
        self.assertIsInstance(out["series"][0]["credit"], float)
        self.assertIsInstance(out["credits"][0]["at"], float)
        # 计数不是浮点字段，不能被顺手改掉
        self.assertIsInstance(out["series"][0]["requests"], int)
        self.assertNotIsInstance(out["series"][0]["requests"], float)

    def test_the_snapshot_payload_is_float_normalised(self):
        engine_snap = {"requests": 3, "credit": 0, "cost_cny": 0,
                       "by_model": {"m": {"requests": 1, "credit": 0,
                                          "cost_cny": 2.5,
                                          "accounts": {"acct-a": 4}}},
                       "cost_missing": {"m": 2}}
        self.install(StubLib(result={"snapshot": engine_snap}))
        out = E.snapshot_window(_noop_log, "usage.jsonl", self.accounts, None,
                                None, None, pool=FakePool(["acct-a"]), pricing_on=True)
        # 顶层与桶的 cost_cny 在 Python 里初始成 0.0
        self.assertIsInstance(out["credit"], float)
        self.assertIsInstance(out["cost_cny"], float)
        bucket = out["by_model"]["m"]
        self.assertIsInstance(bucket["cost_cny"], float)
        # 桶的 credit 在 Python 里来自 USAGE_FIELDS 的 0（int），不能碰
        self.assertIsInstance(bucket["credit"], int)
        self.assertNotIsInstance(bucket["credit"], float)
        self.assertIsInstance(bucket["accounts"]["acct-a"], int)
        self.assertIsInstance(out["cost_missing"]["m"], int)

    def test_a_broken_result_shape_is_refused(self):
        self.install(StubLib(result={"snapshot": {"nope": 1}}))
        self.assertIsNone(E.snapshot_window(_noop_log, "usage.jsonl", self.accounts,
                                            None, None, None,
                                            pool=FakePool(["acct-a"]), pricing_on=True))
        self.install(StubLib(result={"series": {"nope": 1}}))
        self.assertIsNone(E.series(_noop_log, "usage.jsonl", self.accounts,
                                   None, 0.0, 1.0, 60, pool=FakePool(["acct-a"])))

    def test_a_null_result_falls_back(self):
        self.install(StubLib(result=None))
        self.assertIsNone(E.snapshot_window(_noop_log, "usage.jsonl", self.accounts,
                                            None, None, None,
                                            pool=FakePool(["acct-a"]), pricing_on=True))
        self.assertIsNone(E.series(_noop_log, "usage.jsonl", self.accounts,
                                   None, 0.0, 1.0, 60, pool=FakePool(["acct-a"])))

    def test_an_exception_is_swallowed_and_logged(self):
        lib = self.install(StubLib())
        lib.raised = RuntimeError("boom")
        lines = []
        self.assertIsNone(E.snapshot_window(lines.append, "usage.jsonl", self.accounts,
                                            None, None, None,
                                            pool=FakePool(["acct-a"]), pricing_on=True))
        self.assertTrue(any("agg engine call failed" in line for line in lines))

    def test_an_unmatching_pool_never_reaches_the_library(self):
        lib = self.install(StubLib(result={"snapshot": {"requests": 3}}))
        self.assertIsNone(E.snapshot_window(_noop_log, "usage.jsonl", self.accounts,
                                            None, None, None, pool=FakePool([]),
                                            pricing_on=True))
        self.assertEqual(lib.requests, [])

    def test_concurrent_seam_calls_are_consistent(self):
        # 接缝把引擎调用串行化（内存峰值钉在一次一份），并发下每个调用者
        # 仍要拿到完整结果、且不许抛。
        self.install(StubLib(result={"snapshot": {"requests": 9}}))
        answers, errors = [], []

        def worker():
            try:
                answers.append(E.snapshot_window(
                    _noop_log, "usage.jsonl", self.accounts, None, None, None,
                    pool=FakePool(["acct-a"]), pricing_on=True))
            except Exception as exc:      # pragma: no cover - the failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual([a["requests"] for a in answers], [9] * 8)


class ProxySeamTests(SeamTestCase):
    """两条接缝在 wb_proxy 里的行为：开了用引擎、关了逐字不变。"""

    def setUp(self):
        super(ProxySeamTests, self).setUp()
        self.write_account("acct-a", "intl")
        self.usage = os.path.join(self.dir, "usage")
        os.makedirs(self.usage)
        self.log_path = os.path.join(self.usage, "usage.jsonl")
        with open(self.log_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": 1000.0, "model": "m", "outcome": "completed",
                                 "total_tokens": 5}) + "\n")
        self.saved = (proxy.USAGE_LOG, proxy.ACCOUNTS_DIR, proxy.POOL)
        proxy.USAGE_LOG = self.log_path
        proxy.ACCOUNTS_DIR = self.accounts
        proxy.POOL = FakePool(["acct-a"])

    def tearDown(self):
        proxy.USAGE_LOG, proxy.ACCOUNTS_DIR, proxy.POOL = self.saved
        super(ProxySeamTests, self).tearDown()

    def test_the_window_scan_merges_the_engine_over_the_empty_shape(self):
        engine_snap = {"requests": 7, "errors": 0, "cost_cny": 1.5}
        self.install(StubLib(result={"snapshot": engine_snap}))
        snap = proxy._scan_usage_snapshot_window(None, None, None, pricing_on=True)
        self.assertEqual(snap["requests"], 7)
        self.assertEqual(snap["cost_cny"], 1.5)
        # _empty_stats() 的键一个不能少（ttft/wall 那些没有读者，但形状要稳）
        for key in proxy._empty_stats():
            self.assertIn(key, snap)

    def test_the_window_scan_is_untouched_when_the_switch_is_off(self):
        self.install(StubLib(result={"snapshot": {"requests": 7}}), engine="0")
        snap = proxy._scan_usage_snapshot_window(None, None, None, pricing_on=True)
        self.assertEqual(snap["requests"], 1)      # 那一行真的被 Python 折进去了
        self.assertEqual(snap["total_tokens"], 5)

    def test_the_series_scan_prefers_the_engine(self):
        engine_series = {"bucket_seconds": 86400, "since": 0.0, "until": 10.0,
                         "series": [{"at": 0.0, "requests": 1}], "credits": []}
        self.install(StubLib(result={"series": engine_series}))
        out = proxy._usage_timeseries_scan(None, 0.0, 10.0, 86400)
        self.assertEqual(out["series"], engine_series["series"])
        self.assertEqual(out["ok"], True)
        self.assertEqual(out["realm"], "all")

    def test_the_series_scan_is_untouched_when_the_switch_is_off(self):
        self.install(StubLib(result={"series": {"series": [{"at": 0.0}]}}), engine="0")
        out = proxy._usage_timeseries_scan(None, 0.0, 2000.0, 86400)
        self.assertEqual(len(out["series"]), 1)
        self.assertEqual(out["series"][0]["requests"], 1)   # Python 折出来的那一桶


if __name__ == "__main__":
    unittest.main()
