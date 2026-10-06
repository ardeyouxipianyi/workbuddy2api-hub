"""悬停提示背后的明细字段，以及它们绝不能动哈希。

面板「最近请求」最后一列悬停时要能回答「这个数怎么来的」：哪条策略、那一档的
三档单价、按什么汇率折算、这条价是直接同名命中 / 人工映射 / 剥后缀继承、落在
哪个条件档、是不是补算。这份明细由 cost_for_row 组装，wb_proxy 原样挂到
/usage/recent 每一行上，前端一个价都不重算。

更要紧的是反面：policy_id 是内容哈希，历史 usage 行的 cost_policy 直接引用
它。via / inherited_from / override_from 这些是审计痕迹，只能看不能算；一旦
漏进哈希，历史行就会指向不存在的策略，整片费用口径被改写。这里用「改动前生成
的策略表」和「现场 usage 采样」把这条钉死。

夹具（tests/fixtures/pricing_tooltip/）取自 2026-10-02 的 VM102 现场：
  or_models.json              OpenRouter 目录（{id: pricing}）
  snapshot_before.json        改动前 build_snapshot 的完整输出
  policies_before.jsonl       改动前由该快照生成的策略表（逐条比对 id）
  policies_live_before.jsonl  现场策略表（52 条历史策略，逐条重算 id）
  timeline_live_before.jsonl  现场时间线
  usage_sample.jsonl          现场 usage 采样（账号已脱敏）
  cost_sources_before.json    采样行逐行的 source（改动前算出来的）

全量 usage.jsonl 的逐行比对见 FullUsageSweepTests：需要现场数据，没给环境变量
就跳过（跑法与结果见交付说明）。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_pricing

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "fixtures", "pricing_tooltip")


def fixture(name):
    return os.path.join(FIX, name)


def read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class PricingFixtureMixin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb-tooltip-")
        wb_pricing.set_data_dir(self.tmp)
        self.reload()

    def tearDown(self):
        wb_pricing.set_data_dir(None)

    def reload(self):
        wb_pricing._policies_cache.update({"key": None, "data": None})
        wb_pricing._timeline_cache.update({"key": None, "data": None})

    def install(self, policies, timeline=None):
        """把一份策略表（与时间线）放进数据目录，模拟现场。"""
        shutil.copyfile(fixture(policies),
                        os.path.join(self.tmp, "pricing-policies.jsonl"))
        timeline_path = os.path.join(self.tmp, "pricing-timeline.jsonl")
        if timeline:
            shutil.copyfile(fixture(timeline), timeline_path)
        elif os.path.exists(timeline_path):
            os.remove(timeline_path)
        self.reload()


class HashCompatibilityTests(PricingFixtureMixin):
    """历史策略 id 逐条不变 —— 这是本次改动唯一的硬约束。"""

    def test_regenerated_table_has_the_same_ids(self):
        """改动后重新生成一遍策略表，id 集合与改动前逐条一致。"""
        or_models = read_json(fixture("or_models.json"))
        doc, _unpriced, _overridden = wb_pricing.build_snapshot(or_models,
                                                                variants=True)
        _added, _changed, assignment = wb_pricing.record_policies(
            doc, at=1759360000)
        before = {row["model"]: row["id"]
                  for row in read_jsonl(fixture("policies_before.jsonl"))}
        self.assertTrue(before, "夹具里的改动前策略表不该是空的")
        self.assertEqual(set(before.values()), set(assignment.values()),
                         "重新生成的策略 id 集合变了")
        self.assertEqual(before, assignment, "有模型的策略 id 变了")

    def test_live_table_ids_recompute_the_same(self):
        """现场 52 条历史策略，逐条按新代码重算内容哈希，必须一模一样。"""
        rows = read_jsonl(fixture("policies_live_before.jsonl"))
        self.assertEqual(len(rows), 52, "现场策略表的规模变了，夹具要跟着更新")
        for row in rows:
            self.assertEqual(
                row["id"],
                wb_pricing.policy_id(row["model"], row.get("flat"),
                                     row.get("bands"), row.get("usd_cny")),
                "策略 %s（%s）的 id 重算后变了" % (row["id"], row["model"]))

    def test_display_fields_stay_out_of_the_hash(self):
        """via / inherited_from / override_from 加不加都不该影响 id。"""
        rows = read_jsonl(fixture("policies_live_before.jsonl"))
        extras = [{"via": "direct"},
                  {"via": "override", "override_from": "some-hub-name"},
                  {"via": "variant", "inherited_from": "base-name"},
                  {"via": "variant", "inherited_from": "base-name",
                   "override_from": None, "band_note": "随便写点什么"}]
        for row in rows:
            for extra in extras:
                merged = dict(row, **extra)
                self.assertEqual(
                    row["id"],
                    wb_pricing.policy_id(merged["model"], merged.get("flat"),
                                         merged.get("bands"),
                                         merged.get("usd_cny")),
                    "%s 的 id 被展示字段带偏了" % row["model"])

    def test_entry_for_keeps_the_display_fields_out_of_the_hash(self):
        """entry_for 写出来的快照条目，带上展示字段后 id 也不变。"""
        or_models = read_json(fixture("or_models.json"))
        by_norm = wb_pricing.index_openrouter(or_models)
        for hub_id, expect_via in (("deepseek-v3-1-volc", "override"),
                                   ("deepseek-r1-0528-lkeap", "variant"),
                                   ("deepseek-v4.1-flash", "direct")):
            ref, via = wb_pricing.resolve(hub_id, or_models, by_norm, variants=True)
            self.assertTrue(ref, hub_id)
            inherited = wb_pricing.inherited_from(hub_id, or_models, by_norm)
            entry = wb_pricing.entry_for(hub_id, ref, or_models,
                                         inherited=inherited, overridden=via)
            self.assertEqual(entry.get("via", "direct"), expect_via, hub_id)
            stripped = {k: v for k, v in entry.items()
                        if k not in ("via", "inherited_from", "override_from",
                                     "display", "source")}
            self.assertEqual(
                wb_pricing.policy_id(hub_id, entry.get("flat"),
                                     entry.get("bands"), 7.1),
                wb_pricing.policy_id(hub_id, stripped.get("flat"),
                                     stripped.get("bands"), 7.1),
                "%s 的 id 被展示字段带偏了" % hub_id)

    def test_every_sampled_row_keeps_its_source(self):
        """采样行逐行跑 cost_for_row，source 与改动前 0 条失配。"""
        self.install("policies_live_before.jsonl", "timeline_live_before.jsonl")
        before = read_json(fixture("cost_sources_before.json"))
        rows = read_jsonl(fixture("usage_sample.jsonl"))
        self.assertEqual(len(rows), len(before), "采样规模与基线不一致")
        bad = []
        for i, row in enumerate(rows):
            got = wb_pricing.cost_for_row(row)["source"]
            if got != before[str(i)]:
                bad.append((i, row.get("model"), before[str(i)], got))
        self.assertEqual(bad, [], "%d 条 source 失配：%r" % (len(bad), bad[:5]))


class FullUsageSweepTests(PricingFixtureMixin):
    """全量 usage.jsonl 的逐行 source 比对（现场数据，未配置就跳过）。

    跑法：
      WB_PRICING_SWEEP_USAGE=<现场 usage.jsonl 路径>
      WB_PRICING_SWEEP_BASELINE=<改动前逐行 source 的 JSON>
    两者都在时才执行；baseline 的生成命令见交付说明。
    """

    @unittest.skipUnless(os.environ.get("WB_PRICING_SWEEP_USAGE")
                         and os.environ.get("WB_PRICING_SWEEP_BASELINE"),
                         "需要现场 usage.jsonl 与改动前基线（见类文档）")
    def test_full_file_has_no_source_drift(self):
        usage = os.environ["WB_PRICING_SWEEP_USAGE"]
        wb_pricing.set_data_dir(os.path.dirname(os.path.abspath(usage)))
        self.reload()
        baseline = read_json(os.environ["WB_PRICING_SWEEP_BASELINE"])
        rows = read_jsonl(usage)
        self.assertEqual(len(rows), len(baseline),
                         "行数与基线不一致，基线不是这一版数据生成的")
        bad = []
        for i, row in enumerate(rows):
            got = wb_pricing.cost_for_row(row)["source"]
            if got != baseline[i]:
                bad.append((i, row.get("model"), baseline[i], got))
        self.assertEqual(bad, [], "%d 条 source 失配：%r" % (len(bad), bad[:5]))


class RowDetailTests(PricingFixtureMixin):
    """明细字段本身：三种匹配方式、条件档、未定价，都要能自圆其说。"""

    def setUp(self):
        super().setUp()
        self.install("policies_live_before.jsonl", "timeline_live_before.jsonl")
        self.policies = {row["model"]: row
                         for row in read_jsonl(fixture("policies_live_before.jsonl"))}
        self.sample = read_jsonl(fixture("usage_sample.jsonl"))

    def row(self, model, **overrides):
        base = {"model": model, "at": 1790908275.0, "prompt_tokens": 1000,
                "completion_tokens": 100, "cached_tokens": 0}
        base.update(overrides)
        return base

    def sample_row(self, model, needs_policy=False):
        for row in self.sample:
            if row.get("model") != model:
                continue
            if needs_policy and not row.get("cost_policy"):
                continue
            return dict(row)
        self.fail("采样里没有 %s 的行" % model)

    def test_direct_match_carries_the_whole_picture(self):
        row = self.sample_row("deepseek-v4.1-flash", needs_policy=True)
        cost = wb_pricing.cost_for_row(row)
        # 一个模型可以有多条历史策略（改过价），这条行引用的是哪条就按哪条。
        policy = wb_pricing.load_policies()[row["cost_policy"]]
        self.assertTrue(cost["known"])
        self.assertEqual(cost["source"], policy["id"])
        self.assertEqual(cost["via"], "direct")
        self.assertEqual(cost["or_id"], "deepseek/deepseek-v4.1-flash")
        self.assertEqual(cost["currency"], "USD")
        self.assertEqual(cost["unit"], 1000000)
        self.assertEqual(cost["usd_cny"], policy["usd_cny"])
        self.assertEqual(cost["rates"], policy["flat"])
        self.assertIsNone(cost["band"])
        self.assertIsNone(cost["band_note"])
        self.assertFalse(cost["via_derived"])

    def test_override_match_names_the_source_mapping(self):
        cost = wb_pricing.cost_for_row(self.sample_row("hy4-preview-f"))
        self.assertEqual(cost["via"], "override")
        self.assertEqual(cost["override_from"], "hy4-preview-f")
        self.assertEqual(cost["or_id"], "tencent/hy4-preview")
        # 现场这条策略写于 via 字段引入之前，只能按当前映射表推断，要标出来。
        self.assertTrue(cost["via_derived"])

    def test_variant_match_names_the_base_and_the_suffix(self):
        policy = self.policies["deepseek-r1-0528-lkeap"]
        cost = wb_pricing.cost_for_row(
            self.row("deepseek-r1-0528-lkeap", cost_policy=policy["id"]))
        self.assertEqual(cost["via"], "variant")
        self.assertEqual(cost["inherited_from"], "deepseek-r1-0528")
        self.assertEqual(cost["or_id"], "deepseek/deepseek-r1-0528")
        self.assertFalse(cost["via_derived"])

    def test_band_row_reports_that_band_rates_and_note(self):
        """落档的行给的是该档的价与条件，不是基准价。"""
        policy = self.policies["gpt-5.5"]
        band = policy["bands"][0]
        row = self.row("gpt-5.5", cost_policy=policy["id"],
                       prompt_tokens=band["min_prompt_tokens"] + 1)
        cost = wb_pricing.cost_for_row(row)
        self.assertEqual(cost["band"], 0)
        self.assertEqual(cost["rates"], band["flat"])
        self.assertNotEqual(cost["rates"], policy["flat"])
        self.assertIn("≥", cost["band_note"])
        self.assertIn("272,000", cost["band_note"])

    def test_utc_band_row_says_which_window(self):
        policy = self.policies["hy3"]
        # hy3 的两档是 UTC 时段：0–1600 与 1600–2400。取一档明确的时刻。
        row = self.row("hy3", cost_policy=policy["id"], at=1790908275.0)
        cost = wb_pricing.cost_for_row(row)
        self.assertIsNotNone(cost["band"])
        self.assertIn("UTC", cost["band_note"])
        self.assertEqual(cost["rates"], policy["bands"][cost["band"]]["flat"])

    def test_unpriced_row_says_so_instead_of_zero(self):
        cost = wb_pricing.cost_for_row(self.row("no-such-model-at-all"))
        self.assertFalse(cost["known"])
        self.assertEqual(cost["cny"], 0.0)
        self.assertEqual(cost["source"], "builtin")
        for key in wb_pricing._DETAIL_KEYS:
            self.assertIsNone(cost[key], "%s 该是 None 而不是编出来的值" % key)

    def test_builtin_fallback_still_explains_itself(self):
        """出厂快照兜底的行也要给全明细，via 按直接匹配记。"""
        shipped = wb_pricing.load_pricing().get("models") or {}
        pick = None
        for model, entry in shipped.items():
            if entry.get("flat"):
                pick = model
                break
        self.assertIsNotNone(pick, "出厂快照里没有带价的模型")
        # 一个还没有任何策略与时间线的数据目录 = 出厂快照兜底那条路径。
        empty = tempfile.mkdtemp(prefix="wb-tooltip-empty-")
        wb_pricing.set_data_dir(empty)
        self.reload()
        cost = wb_pricing.cost_for_row(self.row(pick))
        self.assertTrue(cost["known"], pick)
        self.assertEqual(cost["source"], "builtin")
        self.assertEqual(cost["via"], "direct")
        self.assertEqual(cost["rates"], shipped[pick]["flat"])
        self.assertEqual(cost["or_id"], shipped[pick]["or_id"])
        self.assertIsNone(cost["source_at"])
        self.assertFalse(cost["backfilled"])

    def test_band_note_formats_both_kinds_of_condition(self):
        self.assertIsNone(wb_pricing.band_note({}, 0))
        self.assertIsNone(wb_pricing.band_note({"bands": []}, 0))
        self.assertIsNone(wb_pricing.band_note({"bands": [{}]}, 3))
        self.assertEqual(
            wb_pricing.band_note({"bands": [{"min_prompt_tokens": 128000}]}, 0),
            "输入长度 ≥ 128,000 tokens")
        self.assertEqual(
            wb_pricing.band_note({"bands": [{"days": ["mon", "tue"],
                                             "start": 1600, "end": 2400}]}, 0),
            "每周 周一/周二 16:00–24:00 UTC")
        self.assertEqual(
            wb_pricing.band_note({"bands": [{"days": None, "start": 0,
                                             "end": 1600}]}, 0),
            "每天 00:00–16:00 UTC")

    def test_ensure_policy_records_the_same_match_source(self):
        """按需补价（新模型首见即定价）走同一个 entry_for，来源也要写对。"""
        or_models = read_json(fixture("or_models.json"))
        by_norm = wb_pricing.index_openrouter(or_models)
        empty = tempfile.mkdtemp(prefix="wb-tooltip-fresh-")
        wb_pricing.set_data_dir(empty)
        self.reload()
        original = wb_pricing.live_index
        wb_pricing.live_index = lambda: (or_models, by_norm)
        try:
            cases = (("deepseek-v3-1-volc", "override", "deepseek-v3-1-volc", None),
                     ("deepseek-r1-0528-lkeap", "variant", None, "deepseek-r1-0528"),
                     ("deepseek-v4.1-flash", "direct", None, None))
            for hub_id, via, override_from, inherited in cases:
                pid = wb_pricing.ensure_policy(hub_id)
                self.assertIsNotNone(pid, hub_id)
                policy = wb_pricing.load_policies()[pid]
                self.assertEqual(policy["via"], via, hub_id)
                self.assertEqual(policy.get("override_from"), override_from, hub_id)
                self.assertEqual(policy.get("inherited_from"), inherited, hub_id)
        finally:
            wb_pricing.live_index = original

    def test_match_source_prefers_the_recorded_field(self):
        """策略行里记着 via 就照它说，不去猜。"""
        self.assertEqual(
            wb_pricing.match_source({"via": "variant",
                                     "inherited_from": "base"}, "x"),
            ("variant", "base", None, False))
        self.assertEqual(
            wb_pricing.match_source({"via": "override",
                                     "override_from": "x"}, "x"),
            ("override", None, "x", False))
        # 没有 via 的老行：映射表能解释才认，解释不了就按直接匹配。
        self.assertEqual(
            wb_pricing.match_source(
                {"or_id": wb_pricing.OVERRIDES["deepseek-v3-1-volc"]},
                "deepseek-v3-1-volc"),
            ("override", None, "deepseek-v3-1-volc", True))
        self.assertEqual(
            wb_pricing.match_source({"or_id": "some/other"}, "deepseek-v3-1-volc"),
            ("direct", None, None, False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
