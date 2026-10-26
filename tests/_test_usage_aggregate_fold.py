"""聚合热路径：折叠循环 + 悬停明细跳过 + 总开关整扫只问一次。

指标页整表（_scan_usage_log）与 /usage 快照（_usage_snapshot_uncached）每行
都要算一次价，但两处只读 cost["known"] / cost["cny"]（快照再加一个 disabled
标记）：悬停明细（rates / unit / currency / usd_cny / or_id / via / ...）构造
出来就被丢掉，而每行都要为此付 policy_entry + _details_for 的钱；总开关也是
每行重问一次（每次一次 os.stat）。两笔在 2.7 万行的日志上都是可观的开销。

这里钉三件事：
  1. 聚合扫描一次都不构造悬停明细——把 _details_for 换成会抛异常的桩，整表
     重建与快照仍必须成功、数字不变；逐行展示路径（/usage/recent 悬停）仍会
     逐行调用它，明细字段原样挂在行上。
  2. 总开关在一次扫描里只查一次（扫描方把值传进来）；逐行路径保持原语义，
     不传 enabled 时仍然每行都查、改设置当场生效。设置改动从下一次扫描起
     生效，这正是本文件里那半开半关的桩要证明的。
  3. 聚合结果与逐行算价一致：同一份小日志，逐行用 cost_for_row（全明细）
     手工折叠出的数字，与整表重建的输出逐字段相等——跳过的只有明细，计价
     口径一个子都不变。折叠循环本身（_fold_stat / _bump_model_row）也在
     这层断言里被覆盖。

无网络：策略表、时间线、日志全部在临时目录里现造。
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_pricing
import wb_proxy as P
import wb_usage as U
import wb_settings

T0 = 1791000000.0
MODEL_A = "fold-model-a"          # 走策略分支（行上带 cost_policy）
MODEL_B = "fold-model-b"          # 走时间线回填分支（行上没有 cost_policy）
MODEL_NONE = "no-such-model-xyz"  # 没价：known=False 的分支
ACCT_A, ACCT_B = "acct-A", "acct-B"

# 手工折叠要逐字段比对的累计量（_new_analytics_stat 的原始字段）。
FOLD_FIELDS = ("requests", "errors",
               "prompt_tokens", "completion_tokens", "reasoning_tokens",
               "cached_tokens", "total_tokens", "credit", "cost_cny",
               "ttft_sum", "ttft_n", "speed_sum", "speed_n",
               "elapsed_sum", "elapsed_n")


def _row(at, model, account, prompt, completion, outcome="completed",
         policy=None, cached=0, reasoning=0, ttft=400, speed=120.0,
         elapsed=1200, credit=0):
    row = {"at": at, "model": model, "account": account, "realm": "intl",
           "outcome": outcome, "prompt_tokens": prompt,
           "completion_tokens": completion, "reasoning_tokens": reasoning,
           "cached_tokens": cached, "total_tokens": prompt + completion,
           "credit": credit, "ttft_ms": ttft, "tokens_per_sec": speed,
           "elapsed_ms": elapsed}
    if policy:
        row["cost_policy"] = policy
    return row


def _new_fold():
    stat = {field: 0 for field in FOLD_FIELDS}
    stat["cost_cny"] = 0.0
    stat["credit"] = 0.0
    return stat


def fold_rows(rows):
    """逐行用全明细的 cost_for_row 手工折叠，与 _fold_stat 的口径对照。

    行没价（known=False）时 cost_cny 不动，但请求计数与令牌照折——上游已
    经计费的失败请求也算，client_aborted 整行跳过。这份期望值不碰任何聚合
    实现，所以它能钉住折叠循环的等价性。
    """
    stat = _new_fold()
    for r in rows:
        if U.row_outcome(r) == "client_aborted":
            continue
        cost = wb_pricing.cost_for_row(dict(r))
        if U.row_outcome(r) != "completed":
            stat["errors"] += 1
        else:
            stat["requests"] += 1
        for field in ("prompt_tokens", "completion_tokens", "reasoning_tokens",
                      "cached_tokens", "total_tokens", "credit"):
            stat[field] += (r.get(field) or 0)
        if cost["known"]:
            stat["cost_cny"] += cost["cny"]
        if r.get("ttft_ms"):
            stat["ttft_sum"] += r["ttft_ms"]
            stat["ttft_n"] += 1
        if r.get("tokens_per_sec"):
            stat["speed_sum"] += r["tokens_per_sec"]
            stat["speed_n"] += 1
        if r.get("elapsed_ms"):
            stat["elapsed_sum"] += r["elapsed_ms"]
            stat["elapsed_n"] += 1
    return stat


class AggregateHarness(unittest.TestCase):
    """一份现造的小日志：策略分支 / 时间线分支 / 内置快照 / 没价 / 失败 / 中止。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb-agg-fold-")
        self.accounts = os.path.join(self.tmp, "accounts")
        os.makedirs(self.accounts, exist_ok=True)
        self._prev_data = wb_pricing.data_dir()
        self._prev_settings = wb_pricing._settings_dir_override
        self._prev_log = P.USAGE_LOG
        wb_pricing.set_data_dir(self.tmp)
        wb_pricing.set_settings_dir(self.accounts)

        # 策略表与时间线：一条模型策略，行上的 cost_policy 与时间线回填都指它。
        doc = {"meta": {"usd_cny": 7.1},
               "models": {
                   MODEL_A: {"flat": {"input_cache_miss": 2.0,
                                      "input_cache_hit": 1.0, "output": 4.0},
                             "currency": "USD", "unit": 1000000,
                             "or_id": "vendor/" + MODEL_A},
                   MODEL_B: {"flat": {"input_cache_miss": 3.0,
                                      "input_cache_hit": 1.5, "output": 6.0},
                             "currency": "USD", "unit": 1000000,
                             "or_id": "vendor/" + MODEL_B},
               }}
        _added, _changed, assignment = wb_pricing.record_policies(doc, at=T0)
        self.pid_a = assignment[MODEL_A]

        # 内置快照里挑一个能定价的模型，覆盖 cost_for_row 的 builtin 分支。
        self.builtin = ""
        for mid in sorted((wb_pricing.load_pricing().get("models") or {})):
            probe = wb_pricing.cost_for_row(
                {"at": T0, "model": mid, "prompt_tokens": 10,
                 "completion_tokens": 5})
            if probe["known"]:
                self.builtin = mid
                break
        self.assertTrue(self.builtin, "内置快照里至少该有一个可定价的模型")

        self.rows = [
            _row(T0 + 100, MODEL_A, ACCT_A, 4000, 1000, policy=self.pid_a),
            # 失败但上游已经计费：令牌与费用照折，只记 errors。
            _row(T0 + 200, MODEL_A, ACCT_B, 2000, 500, outcome="failed",
                 policy=self.pid_a, credit=0.25),
            # 没有 cost_policy：走时间线回填，价与 A 相同。
            _row(T0 + 300, MODEL_B, ACCT_A, 3000, 600),
            # 只有内置快照认识：走 builtin 分支。
            _row(T0 + 400, self.builtin, ACCT_A, 1500, 300),
            # 谁都不认识：known=False，聚合不得把它当成禁用。
            _row(T0 + 500, MODEL_NONE, ACCT_B, 700, 100),
            # 客户端中止：聚合整行跳过，快照只记 errors。
            _row(T0 + 600, MODEL_A, ACCT_A, 900, 100, outcome="client_aborted",
                 policy=self.pid_a),
        ]
        self.log_path = os.path.join(self.tmp, "usage.jsonl")
        with open(self.log_path, "w", encoding="utf-8") as fh:
            for r in self.rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        P.USAGE_LOG = self.log_path

    def tearDown(self):
        P.USAGE_LOG = self._prev_log
        wb_pricing.set_data_dir(self._prev_data)
        wb_pricing.set_settings_dir(self._prev_settings)


class AggregateSkipsDetailsTests(AggregateHarness):
    """聚合扫描一次都不构造悬停明细；逐行展示路径照旧。"""

    def test_aggregates_survive_a_raising_details_stub(self):
        def boom(*_args, **_kwargs):
            raise AssertionError("聚合路径不该构造悬停明细")
        original = wb_pricing._details_for
        wb_pricing._details_for = boom
        try:
            analytics = U._compute_usage_analytics_uncached()
            snap = U._usage_snapshot_uncached()
        finally:
            wb_pricing._details_for = original
        # 明细被跳过，价本身照算：三档单价那部分才是被省掉的。
        self.assertGreater(analytics["summary"]["all_time"]["cost_cny"], 0)
        self.assertGreater(snap["cost_cny"], 0)
        # 4 条 completed + 1 条 failed；client_aborted 整行跳过。
        self.assertEqual(analytics["summary"]["all_time"]["requests"], 4)
        self.assertEqual(analytics["summary"]["all_time"]["errors"], 1)

    def test_the_payload_is_identical_when_details_are_skipped(self):
        normal = U._compute_usage_analytics_uncached()

        def boom(*_args, **_kwargs):
            raise AssertionError("聚合路径不该构造悬停明细")
        original = wb_pricing._details_for
        wb_pricing._details_for = boom
        try:
            lean = U._compute_usage_analytics_uncached()
        finally:
            wb_pricing._details_for = original
        self.assertEqual(normal, lean)

    def test_the_per_row_path_still_builds_them(self):
        seen = []
        original = wb_pricing._details_for

        def counting(*args, **kwargs):
            seen.append(1)
            return original(*args, **kwargs)

        wb_pricing._details_for = counting
        try:
            page = U.recent_usage(limit=100)
        finally:
            wb_pricing._details_for = original
        # 逐行展示：每一页行都调用一次明细，包括没价的行（整组 None）。
        self.assertEqual(len(seen), len(self.rows))
        row = page["rows"][0]
        for key in ("cost_rates", "cost_unit", "cost_currency", "cost_usd_cny",
                    "cost_or_id", "cost_via", "cost_band_note", "cost_cny"):
            self.assertIn(key, row)

    def test_a_lean_cost_carries_no_detail_keys(self):
        full = wb_pricing.cost_for_row(dict(self.rows[0]))
        lean = wb_pricing.cost_for_row(dict(self.rows[0]), details=False)
        self.assertEqual(full["known"], lean["known"])
        self.assertEqual(full["cny"], lean["cny"])
        # 聚合只读 known/cny：明细专属的键一个都不该出现。
        for key in ("unit", "currency", "usd_cny", "or_id", "via",
                    "inherited_from", "override_from", "band_note",
                    "via_derived"):
            self.assertNotIn(key, lean)
        # rates 是 compute_row 的产出，聚合用不到但也不算白花，保持原样。
        self.assertEqual(full["rates"], lean["rates"])

    def test_a_disabled_lean_cost_keeps_the_flag_the_snapshot_reads(self):
        # 快照的 _fold_cost 靠 disabled 区分「没价」与「功能关着」，
        # 跳过明细之后这个标记必须原样在。
        wb_settings.set_pricing_enabled(self.accounts, False)
        lean = wb_pricing.cost_for_row(dict(self.rows[0]), details=False)
        self.assertFalse(lean["known"])
        self.assertTrue(lean.get("disabled"))
        self.assertNotIn("unit", lean)


class SwitchReadCountTests(AggregateHarness):
    """总开关：一次扫描只问一次；逐行路径保持每行都问。"""

    def _counting_switch(self, value=True):
        calls = []
        original = wb_pricing.pricing_enabled

        def stub(*_args, **_kwargs):
            calls.append(1)
            return value

        wb_pricing.pricing_enabled = stub
        self.addCleanup(lambda: setattr(wb_pricing, "pricing_enabled", original))
        return calls

    def test_a_scan_asks_the_switch_once(self):
        calls = self._counting_switch(True)
        U._compute_usage_analytics_uncached()
        self.assertEqual(len(calls), 1, "整表重建只该问一次总开关")
        del calls[:]
        U._usage_snapshot_uncached()
        self.assertEqual(len(calls), 1, "快照重建只该问一次总开关")

    def test_the_per_row_path_still_asks_per_row(self):
        calls = self._counting_switch(True)
        for row in self.rows[:5]:
            wb_pricing.cost_for_row(dict(row))
        self.assertEqual(len(calls), 5, "逐行路径仍然每行都查")
        # 传了 enabled 就不再自己查：聚合侧把值带进来的形状。
        del calls[:]
        wb_pricing.cost_for_row(dict(self.rows[0]), details=False, enabled=True)
        wb_pricing.cost_for_row(dict(self.rows[0]), details=False, enabled=False)
        self.assertEqual(len(calls), 0)

    def test_a_falsy_row_does_not_consult_the_switch(self):
        # 原语义的短路：row 为空时连问都不问，行为一字不变。
        calls = self._counting_switch(True)
        cost = wb_pricing.cost_for_row({})
        self.assertEqual(len(calls), 0)
        self.assertFalse(cost["known"])
        self.assertNotIn("disabled", cost)

    def test_the_switch_value_wins_over_the_file(self):
        # enabled 是「扫描开始时的那份判定」，它比此刻的 settings.json 权威。
        wb_settings.set_pricing_enabled(self.accounts, False)
        priced = wb_pricing.cost_for_row(dict(self.rows[0]), details=False,
                                         enabled=True)
        self.assertTrue(priced["known"])
        wb_settings.set_pricing_enabled(self.accounts, True)
        off = wb_pricing.cost_for_row(dict(self.rows[0]), details=False,
                                      enabled=False)
        self.assertFalse(off["known"])
        self.assertTrue(off.get("disabled"))

    def test_a_flip_lands_on_the_next_scan_not_mid_scan(self):
        # 第一次问之后把开关关掉：本次扫描用开始时的值，下一次扫描才看到
        # 新值——这正是把查询提到循环外要写进注释的语义变化。
        state = {"on": True}
        calls = []
        original = wb_pricing.pricing_enabled

        def flaky(*_args, **_kwargs):
            calls.append(1)
            value = state["on"]
            state["on"] = False
            return value

        wb_pricing.pricing_enabled = flaky
        try:
            first = U._usage_snapshot_uncached()
            self.assertEqual(len(calls), 1)
            self.assertGreater(first["cost_cny"], 0, "本次扫描用开始时的开关")
            second = U._usage_snapshot_uncached()
            self.assertEqual(len(calls), 2)
            self.assertEqual(second["cost_cny"], 0, "下一次扫描才看到新值")
            self.assertEqual(second.get("cost_missing"), {})
            # 逐行路径相反：同一时刻已经关掉，下一行立刻是 disabled。
            self.assertTrue(wb_pricing.cost_for_row(dict(self.rows[0])).get("disabled"))
        finally:
            wb_pricing.pricing_enabled = original


class AggregateMatchesPerRowCostingTests(AggregateHarness):
    """聚合结果 == 逐行全明细算价的手工折叠（口径不变的硬约束）。"""

    def test_the_all_time_summary_equals_a_manual_fold(self):
        analytics = U._compute_usage_analytics_uncached()
        expected = fold_rows(self.rows)
        for field in FOLD_FIELDS:
            self.assertEqual(analytics["summary"]["all_time"][field],
                             expected[field], field)

    def test_the_window_summary_covers_only_the_selected_rows(self):
        analytics = U._compute_usage_analytics_uncached(since=T0 + 250,
                                                        until=T0 + 450)
        expected = fold_rows([r for r in self.rows
                              if T0 + 250 <= r["at"] <= T0 + 450])
        for field in FOLD_FIELDS:
            self.assertEqual(analytics["summary"]["window"][field],
                             expected[field], field)
        # all_time 与窗口无关，仍是整份日志（4 条 completed、1 条 failed）。
        self.assertEqual(analytics["summary"]["all_time"]["requests"], 4)
        self.assertEqual(analytics["summary"]["all_time"]["errors"], 1)

    def test_model_rows_fold_every_outcome(self):
        analytics = U._compute_usage_analytics_uncached()
        models = {m["model"]: m for m in analytics["models"]}
        for model in (MODEL_A, MODEL_B, self.builtin, MODEL_NONE):
            rows = [r for r in self.rows if r["model"] == model]
            expected = fold_rows(rows)
            for field in FOLD_FIELDS:
                self.assertEqual(models[model]["all_time"][field], expected[field],
                                 (model, field))
                self.assertEqual(models[model]["window"][field], expected[field],
                                 (model, field))

    def test_the_model_distribution_keeps_the_successful_only_rule(self):
        # 模型分布（账号行的 window_models / all_models）只统计成功请求：失败的
        # 调用记成需求会误导。_bump_model_row 重构后这条规则必须原样。
        analytics = U._compute_usage_analytics_uncached()
        accounts = {a["uid"]: a for a in analytics["accounts"]}
        pills_b = accounts[ACCT_B]["window_models"]
        # acct-B 的 fold-model-a 只有一条失败请求 → 分布里不该出现。
        self.assertNotIn(MODEL_A, pills_b)
        self.assertEqual(pills_b[MODEL_NONE]["requests"], 1)
        self.assertEqual(pills_b[MODEL_NONE]["tokens"], 800)
        self.assertEqual(pills_b[MODEL_NONE]["cost_cny"], 0.0)
        pills_a = accounts[ACCT_A]["window_models"]
        # +100 成功、+600 中止被整行跳过 → 只剩一条。
        row_a = self.rows[0]
        cost_a = wb_pricing.cost_for_row(dict(row_a))
        self.assertEqual(pills_a[MODEL_A]["requests"], 1)
        self.assertEqual(pills_a[MODEL_A]["tokens"], row_a["total_tokens"])
        self.assertAlmostEqual(pills_a[MODEL_A]["cost_cny"], cost_a["cny"],
                               places=9)

    def test_account_rows_hold_both_buckets(self):
        analytics = U._compute_usage_analytics_uncached(since=T0 + 250)
        accounts = {a["uid"]: a for a in analytics["accounts"]}
        for uid in (ACCT_A, ACCT_B):
            rows = [r for r in self.rows if r["account"] == uid]
            expected_all = fold_rows(rows)
            expected_window = fold_rows([r for r in rows if r["at"] >= T0 + 250])
            for field in FOLD_FIELDS:
                self.assertEqual(accounts[uid]["all_time"][field],
                                 expected_all[field], (uid, field))
                self.assertEqual(accounts[uid]["window"][field],
                                 expected_window[field], (uid, field))

    def test_the_snapshot_equals_a_manual_fold(self):
        snap = U._usage_snapshot_uncached()
        expected_cost = 0.0
        missing = {}
        completed_tokens = 0
        for r in self.rows:
            outcome = U.row_outcome(r)
            if outcome == "client_aborted":
                continue
            cost = wb_pricing.cost_for_row(dict(r))
            if cost["known"]:
                expected_cost += cost["cny"]
            else:
                missing[r["model"]] = missing.get(r["model"], 0) + 1
            if outcome == "completed":
                completed_tokens += r["total_tokens"]
        self.assertAlmostEqual(snap["cost_cny"], expected_cost, places=9)
        self.assertEqual(snap["cost_missing"], missing)
        self.assertEqual(snap["requests"], 4)
        self.assertEqual(snap["errors"], 2)
        self.assertEqual(snap["total_tokens"], completed_tokens)

    def test_the_key_rows_fold_the_same_way(self):
        # key_map 轴与 acct/model 同源：没有 key 字段的历史行落进 __before_keys__，
        # 这些行的数字必须与手工折叠一致。
        analytics = U._compute_usage_analytics_uncached()
        rows = {k["key"]: k for k in analytics["keys"]}
        bucket = rows.get(U.KEY_BUCKET_BEFORE)
        self.assertIsNotNone(bucket)
        expected = fold_rows(self.rows)
        for field in FOLD_FIELDS:
            self.assertEqual(bucket["all_time"][field], expected[field], field)


class RowResultMemoTests(AggregateHarness):
    """行级结果记忆：跨视图共享「这行折进哪天 / 这行值多少钱」。

    钉四件事：
      1. 开表与关表（两张表上限都置 0）跑同一份日志，三份载荷逐字节相同
         （连键序都不许动）——记忆只该影响速度；
      2. 三个视图扫同一份日志时，日键与算价只在第一遍真算（计数桩证明），
         第二遍一行都不真算；
      3. 表有界：满了以后只留最近的一批，旧行不进表，结果照旧正确；
      4. 失效条件：价表被改写、进程时区被 tzset() 换过，旧条目都不许再命中
         ——价表那条真改磁盘上的策略表来验，不看内部字段。
    """

    def setUp(self):
        super().setUp()
        self._caps = (U._ROW_MEMO_MAX, U._ROW_COST_MEMO_MAX)
        U._row_day_memo.clear()
        U._row_cost_memo.clear()

    def tearDown(self):
        U._ROW_MEMO_MAX, U._ROW_COST_MEMO_MAX = self._caps
        U._row_day_memo.clear()
        U._row_cost_memo.clear()
        super().tearDown()

    # ---- 工具 ----

    def _cold_views(self):
        """三份载荷各清一遍状态后冷扫；记忆表清不清由调用方决定。

        另外各取一份 realm 过滤下的载荷：per-realm 状态是独立的，记忆表却是
        跨 realm 共享的（日键与定价都与 realm 无关），两条都要对得上。
        """
        out = {}
        for names, key, fn in (
                (("_snap_cache", "_usage_snap_state"), "snapshot",
                 lambda: U._usage_snapshot_uncached()),
                (("_analytics_cache", "_analytics_state"), "analytics",
                 lambda: U._compute_usage_analytics_uncached())):
            for name in names:
                getattr(U, name).clear()
            out[key] = fn()
        for name in ("_series_cache", "_series_state"):
            getattr(U, name).clear()
        out["series"] = U.usage_timeseries(
            realm="all", range="custom", since=T0, until=T0 + 2 * 86400,
            bucket_seconds=86400, ttl=0)
        out["snap_intl"] = U._usage_snapshot_uncached(realm="intl")
        out["snap_cn"] = U._usage_snapshot_uncached(realm="cn")
        out["an_intl"] = U._compute_usage_analytics_uncached(realm="intl")
        return out

    def _payload(self):
        return json.dumps(self._cold_views(), ensure_ascii=False)

    def _memo_off(self):
        U._ROW_MEMO_MAX = 0
        U._ROW_COST_MEMO_MAX = 0
        U._row_day_memo.clear()
        U._row_cost_memo.clear()

    def _memo_on(self):
        U._ROW_MEMO_MAX, U._ROW_COST_MEMO_MAX = self._caps
        U._row_day_memo.clear()
        U._row_cost_memo.clear()

    # ---- [1] 逐字节对拍 ----

    def test_memo_off_and_on_produce_the_same_bytes(self):
        self._memo_off()
        want = self._payload()
        self._memo_on()
        got = self._payload()
        if want != got:
            for i in range(min(len(want), len(got))):
                if want[i] != got[i]:
                    self.fail("@%d: %r vs %r"
                              % (i, want[i:i + 70], got[i:i + 70]))
        # 第二次（表里已有条目）再跑一遍：命中路径同样逐字节相同。
        self.assertEqual(want, self._payload())

    # ---- [2] 计数桩：省下的工作真实存在 ----

    def test_each_row_is_day_keyed_and_priced_once_per_scan_batch(self):
        counts = {"strftime": 0, "cost": 0}
        real_strftime = U.time.strftime
        real_cost = wb_pricing.cost_for_row

        def counted_strftime(fmt, *args, **kwargs):
            if fmt == "%Y-%m-%d":        # _local_day_key 的格式
                counts["strftime"] += 1
            return real_strftime(fmt, *args, **kwargs)

        def counted_cost(row, details=True, enabled=None):
            counts["cost"] += 1
            return real_cost(row, details=details, enabled=enabled)

        U.time.strftime = counted_strftime
        wb_pricing.cost_for_row = counted_cost
        try:
            self._memo_on()
            self._cold_views()
            first = dict(counts)
            counts["strftime"] = counts["cost"] = 0
            self._cold_views()
            second = dict(counts)
            self._memo_off()
            counts["strftime"] = counts["cost"] = 0
            self._cold_views()
            plain = dict(counts)
        finally:
            U.time.strftime = real_strftime
            wb_pricing.cost_for_row = real_cost

        n = len(self.rows)
        # 第一遍：日键每行一次（+ 剪枝线那几次），算价每行一次（analytics 对
        # 中止行不算价，但那些行在快照里已经算过）。
        self.assertGreaterEqual(first["strftime"], n)
        self.assertLessEqual(first["strftime"], 2 * n)
        self.assertGreaterEqual(first["cost"], n)
        self.assertLessEqual(first["cost"], n + 2)
        # 第二遍：日键与算价都在表里，一行都不真算。
        self.assertEqual(second["strftime"], 0)
        self.assertEqual(second["cost"], 0)
        # 关表跑同一份日志：日键三遍全算、算价两个视图各算一遍——上面那两个
        # 0 不是桩坏了，是记忆真的把工作省掉了。
        self.assertGreaterEqual(plain["strftime"], 3 * n)
        self.assertGreater(plain["cost"], first["cost"] + n - 2)

    # ---- [3] 有界 ----

    def test_the_day_table_keeps_the_newest_keys_only(self):
        U._ROW_MEMO_MAX = 3
        U._row_day_memo.clear()
        for ts in (10, 20, 30, 40, 50):
            U._local_day_key(ts)
        self.assertEqual(sorted(U._row_day_memo), [30, 40, 50])
        # 比表里最旧的还旧的 ts 不进表，但答案照旧真算、照旧正确。
        self.assertEqual(U._local_day_key(5),
                         time.strftime("%Y-%m-%d", time.localtime(5)))
        self.assertEqual(sorted(U._row_day_memo), [30, 40, 50])

    def test_the_cost_table_is_bounded_too(self):
        U._ROW_COST_MEMO_MAX = 2
        U._row_cost_memo.clear()
        U._usage_snapshot_uncached()
        self.assertLessEqual(len(U._row_cost_memo), 2)
        kept = [key[2] for key in U._row_cost_memo]
        self.assertIn(max(r["at"] for r in self.rows), kept)

    # ---- [4] 失效条件 ----

    def test_a_rewritten_price_table_is_never_served_from_the_table(self):
        """策略表被改写：同一个策略 id 的费率翻倍，快照必须用新价。

        改的是磁盘上的真文件（不是内部字段），所以这条同时钉住了「价表换代
        → 记忆条目整体失效」与「状态指纹照样作废重折」两件事。
        """
        first = U._usage_snapshot_uncached()
        self.assertGreater(first["cost_cny"], 0)
        path = wb_pricing.policies_path()
        lines = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                flat = row.get("flat") or {}
                if flat.get("output"):
                    flat["output"] = flat["output"] * 2
                    row["flat"] = flat
                lines.append(json.dumps(row, ensure_ascii=False))
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            for line in lines:
                fh.write(line + "\n")
        # 路径指纹有个 1 秒 TTL（本进程之外写下的变更最迟 1 秒内可见）：
        # 测试不等这一秒，直接把它清掉，语义与「等过了 TTL」一致。
        wb_pricing._file_key_cache.clear()
        second = U._usage_snapshot_uncached()
        self.assertGreater(second["cost_cny"], first["cost_cny"])
        # 表里那条旧价必须已经作废：再折一遍仍是新价。
        self.assertAlmostEqual(U._usage_snapshot_uncached()["cost_cny"],
                               second["cost_cny"], places=9)

    def test_a_timezone_change_drops_the_day_table(self):
        """进程时区被 tzset() 换过：日键表整体作废（定价表不受影响）。

        CPython 的 time.tzset() 每次都会重建 time.tzname 这个元组对象，所以
        这里用一个 time 代理模拟：换掉 tzname 之前命中，换掉之后必须重算。
        """

        class _Clock(object):
            def __init__(self, tz, key):
                self.tzname = tz
                self._key = key

            def localtime(self, ts):
                return ts

            def strftime(self, fmt, ts):
                return self._key

        real_time = U.time
        clock = _Clock(("A", "B"), "2001-01-01")
        U.time = clock
        try:
            self.assertEqual(U._local_day_key(123), "2001-01-01")
            self.assertEqual(U._local_day_key(456), "2001-01-01")
            self.assertIn(456, U._row_day_memo)
            clock._key = "2001-01-02"          # 时区没变：表照用
            self.assertEqual(U._local_day_key(123), "2001-01-01")
            clock.tzname = ("C", "D")          # tzset() 会重建这个元组
            self.assertEqual(U._local_day_key(123), "2001-01-02")
            self.assertNotIn(456, U._row_day_memo)
        finally:
            U.time = real_time

    # ---- 形状与退化输入 ----

    def test_the_table_returns_exactly_what_cost_for_row_returns(self):
        """命中与未命中给出的 dict 与现算逐键同序、逐值相等。"""
        pricing = U._pricing_inputs(True)
        self.assertIsNotNone(pricing)
        off_pricing = U._pricing_inputs(False)
        for r in self.rows:
            for enabled, inputs in ((True, pricing), (False, off_pricing)):
                want = wb_pricing.cost_for_row(dict(r), details=False,
                                               enabled=enabled)
                miss = U._row_cost(dict(r), inputs, enabled)
                hit = U._row_cost(dict(r), inputs, enabled)
                self.assertEqual(list(miss.keys()), list(want.keys()))
                self.assertEqual(list(hit.keys()), list(want.keys()))
                self.assertEqual(miss, want)
                self.assertEqual(hit, want)

    def test_degenerate_rows_behave_exactly_as_before(self):
        pricing = U._pricing_inputs(True)
        for empty in (None, {}, []):
            self.assertEqual(
                U._row_cost(empty, pricing, True),
                wb_pricing.cost_for_row(empty, details=False, enabled=True))
        # 字段不可哈希（坏行）：两边都从同一处抛 TypeError，记忆不抢先。
        bad = {"model": ["x"], "at": T0}
        with self.assertRaises(TypeError):
            U._row_cost(bad, pricing, True)
        with self.assertRaises(TypeError):
            wb_pricing.cost_for_row(bad, details=False, enabled=True)
        # 价表读不出来（_pricing_inputs 返回 None）时不进表，照旧现算。
        self.assertEqual(U._row_cost(dict(self.rows[0]), None, True),
                         wb_pricing.cost_for_row(dict(self.rows[0]),
                                                 details=False, enabled=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
