"""Automatic pricing for models the upstream adds after a release.

Pinned here, in one place, the four pieces that let a new upstream model be
priced without a code change:

  - the fetch input is the bundled catalogue **union** the gateway's live
    catalogue, filtered the way /v1/models is (aliases and paid variants
    never enter the price input);
  - a model called between two fetches is priced on demand from the index the
    last fetch left in memory (no network on the request path), idempotently
    and without a rewrite of anything already stored;
  - a name carrying a channel suffix (`-lkeap`) inherits from its base, but
    only when that base matches OpenRouter uniquely, and the policy records
    that it was inherited; the setting can turn the whole rule off;
  - the unpriced report the panel reads: the reason per model and, where one
    exists, a hand-entered mapping that lives in the data folder.

No network: OpenRouter is a fixture, the live catalogue is a stub. The real
catalogue is exercised separately, in the deployment check.
"""
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-price-auto-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_pricing
import wb_proxy as P
import wb_settings


def rate(prompt, completion, cache=None):
    """One OpenRouter entry, per-token USD strings like the API sends."""
    return {"prompt": str(prompt), "completion": str(completion),
            "input_cache_read": str(prompt if cache is None else cache)}


# A stand-in for the live catalogue: the base models the variant names in the
# bundled catalogue need, plus one name the bundled catalogue has never seen.
OR = {
    "deepseek/deepseek-v4.1-flash": rate(0.00000003, 0.0000005, 0.00000001),
    "deepseek/deepseek-r1-0528": rate(0.0000005, 0.00000215, 0.00000035),
    "deepseek/deepseek-chat-v3.1": rate(0.00000025, 0.00000095, 0.00000013),
    "deepseek/deepseek-chat-v3-0324": rate(0.00000029, 0.00000114, 0.00000011),
    "z-ai/glm-5.3-flash": rate(0.00000015, 0.0000005, 0.00000003),
    "z-ai/glm-5.3-flashx": rate(0.0000002, 0.0000007, 0.00000004),
    "moonshotai/kimi-k2.6": rate(0.00000043415, 0.000001828, 0.00000007312),
}

# The five virtual alias names in the bundled catalogue: not models, never
# priced, never counted as a coverage gap.
ALIASES = ("default-model", "fast-model", "balanced-model", "primary-model",
           "deep-model")


class PricingTestCase(unittest.TestCase):
    """A private data dir plus the module-level state each test can dirty."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wb-price-auto-case-")
        self.accounts = os.path.join(self.dir, "accounts")
        os.makedirs(self.accounts, exist_ok=True)
        self._previous_dir = wb_pricing.data_dir()
        self._previous_settings = wb_pricing._settings_dir_override
        wb_pricing.set_data_dir(self.dir)
        wb_pricing.set_settings_dir(self.accounts)
        self._fetch = wb_pricing.fetch_openrouter
        self._live = wb_pricing._live_index["models"]
        wb_pricing.fetch_openrouter = lambda: dict(OR)

    def tearDown(self):
        wb_pricing.fetch_openrouter = self._fetch
        wb_pricing.set_settings_dir(self._previous_settings)
        wb_pricing.set_data_dir(self._previous_dir)
        wb_pricing.remember_live_index(None)
        if self._live is not None:
            wb_pricing.remember_live_index(self._live)

    def _lines(self, name):
        path = os.path.join(self.dir, name)
        if not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [line for line in fh.read().splitlines() if line.strip()]

    def _refresher(self, interval=5):
        refresher = wb_pricing.PriceRefresher(interval)
        refresher.log = lambda msg: None
        return refresher


class UnionInputTests(PricingTestCase):
    """The fetch input is the bundled catalogue plus the live additions."""

    def test_the_static_baseline_is_unchanged(self):
        ids = wb_pricing.hub_model_ids()
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("hy4-preview-f", ids)
        # A live-only name must not leak into the baseline; that is the whole
        # reason priced_model_ids() exists as a separate function.
        self.assertNotIn("glm-5.3-flashx", ids)

    def test_extra_ids_are_appended_in_order_without_duplicates(self):
        static = wb_pricing.hub_model_ids()
        ids = wb_pricing.priced_model_ids(
            ["glm-5.3-flashx", "deepseek-v4.1-flash", "glm-5.3-flashx", "  "])
        self.assertEqual(ids[:len(static)], static)
        self.assertEqual(ids[len(static):], ["glm-5.3-flashx"])

    def test_the_snapshot_covers_a_live_only_model(self):
        doc, unpriced, overridden = wb_pricing.build_snapshot(
            OR, None, ["glm-5.3-flashx"])
        self.assertIn("glm-5.3-flashx", doc["models"])
        self.assertEqual(doc["models"]["glm-5.3-flashx"]["or_id"],
                         "z-ai/glm-5.3-flashx")
        self.assertNotIn("glm-5.3-flashx", unpriced)
        # Nothing about it was human-entered, so it is not an override.
        self.assertNotIn("glm-5.3-flashx", overridden)

    def test_a_failed_snapshot_fetch_keeps_previous_prices(self):
        doc, unpriced, _ov = wb_pricing.build_snapshot(
            OR, None, ["glm-5.3-flashx"])
        previous = doc["models"]
        again, unpriced2, _ov2 = wb_pricing.build_snapshot(
            None, previous, ["glm-5.3-flashx"])
        self.assertEqual(again["models"]["glm-5.3-flashx"],
                         previous["glm-5.3-flashx"])
        # Every id that had a price keeps it; only the never-priced ones are
        # reported, exactly as before the union input existed.
        self.assertNotIn("glm-5.3-flashx", unpriced2)
        self.assertEqual(sorted(unpriced2), sorted(unpriced))

    def test_run_once_prices_a_live_only_model(self):
        original = P.read_cached_remote_catalog
        P.read_cached_remote_catalog = lambda realm: (["glm-5.3-flashx"], {})
        try:
            refresher = self._refresher()
            ok, message = refresher.run_once()
        finally:
            P.read_cached_remote_catalog = original
        self.assertTrue(ok, message)
        self.assertEqual(refresher.last_extra, 1)
        _at, assignment = wb_pricing.current_assignment()
        self.assertIn("glm-5.3-flashx", assignment)
        self.assertEqual(refresher.last_models, len(assignment))

    def test_a_live_read_failure_keeps_the_static_coverage(self):
        def boom(realm):
            raise RuntimeError("live catalogue unavailable")

        original = P.curated_live_sources
        P.curated_live_sources = boom
        try:
            refresher = self._refresher()
            ok, message = refresher.run_once()
        finally:
            P.curated_live_sources = original
        self.assertTrue(ok, message)
        self.assertEqual(refresher.last_extra, 0)
        # Coverage is exactly the static baseline - not less than it.
        static_doc, _unpriced, _ov = wb_pricing.build_snapshot(OR)
        self.assertEqual(refresher.last_models, len(static_doc["models"]))
        self.assertGreater(refresher.last_models, 3)

    def test_live_ids_use_the_same_filter_as_the_model_list(self):
        original = P.read_cached_remote_catalog
        P.read_cached_remote_catalog = lambda realm: (
            ["default-model", "auto", "lite", "codewise-inline",
             "glm-5.3-flashx", "hy4-preview-sg", "glm-5.3-flash"], {})
        try:
            ids = self._refresher().live_ids()
        finally:
            P.read_cached_remote_catalog = original
        # The new name comes through...
        self.assertIn("glm-5.3-flashx", ids)
        # ...while the virtual aliases, the non-chat helper and the paid
        # "-sg" build are dropped by the gateway's own curation, and a name
        # the bundled catalogue already carries is not duplicated.
        for dropped in ("default-model", "auto", "lite", "codewise-inline",
                        "hy4-preview-sg", "glm-5.3-flash"):
            self.assertNotIn(dropped, ids, dropped)

    def test_live_ids_ask_both_realms_and_dedupe(self):
        asked = []
        original = P.curated_live_sources

        def fake(realm):
            asked.append(realm)
            if realm == "cn":
                return [("glm-5.3-flashx", {}), ("kimi-k2.6", {})], True
            return [("glm-5.3-flashx", {}), ("glm-5.3-flash", {})], True

        P.curated_live_sources = fake
        try:
            ids = self._refresher().live_ids()
        finally:
            P.curated_live_sources = original
        # Both realms are asked - the remote catalogue is fetched per realm,
        # and either tab's additions belong in the price input.
        self.assertEqual(asked, ["intl", "cn"])
        self.assertEqual(ids, ["glm-5.3-flashx"])

    def test_one_failing_realm_does_not_hide_the_other(self):
        original = P.curated_live_sources

        def fake(realm):
            if realm == "cn":
                raise RuntimeError("cn catalogue down")
            return [("glm-5.3-flashx", {})], True

        P.curated_live_sources = fake
        try:
            refresher = wb_pricing.PriceRefresher(5)
            ids = refresher.live_ids()
        finally:
            P.curated_live_sources = original
        self.assertEqual(ids, ["glm-5.3-flashx"])
        self.assertTrue(any("cn" in line for line in refresher.logs), refresher.logs)

    def test_an_extra_id_never_pollutes_the_alias_rows(self):
        # A live catalogue that somehow still names an alias must not get it
        # priced (it cannot match OpenRouter anyway) nor logged as an override.
        doc, unpriced, overridden = wb_pricing.build_snapshot(
            OR, None, list(ALIASES))
        for alias in ALIASES:
            self.assertNotIn(alias, doc["models"])
            self.assertIn(alias, unpriced)  # unchanged from before the change
        for alias in ALIASES:
            self.assertNotIn(alias, overridden)


class SingleGatewayInstanceTests(PricingTestCase):
    """Pricing must talk to the gateway module the server actually runs.

    The container starts `python wb_proxy.py`, which makes the live instance
    __main__; a second `import wb_proxy` inside that process has no account
    pool - the live catalogue reads empty, silently - and its own log buffer,
    so pricing lines never reach the panel. Production showed exactly both: a
    refresh reporting no live additions while /v1/models still listed them,
    and an empty log page for the pricing tag. `__main__` stands in for the
    server here, with wb_proxy imported the ordinary way right beside it.
    """

    @staticmethod
    def _fake_gateway():
        class FakeGateway(object):
            VIRTUAL_ALIAS_MODELS = ("default-model", "auto")

            def __init__(self):
                self.realms = []
                self.lines = []

            def curated_live_sources(self, realm):
                self.realms.append(realm)
                return ([("gpt-6-sol", {})], True)

            def add_log_entry(self, message, tag=None):
                self.lines.append((message, tag))

        return FakeGateway()

    def test_the_live_catalogue_is_read_from_the_running_gateway(self):
        fake = self._fake_gateway()
        with unittest.mock.patch.dict(sys.modules, {"__main__": fake}):
            ids = self._refresher().live_ids()
        # Both realms were asked on the running module...
        self.assertEqual(fake.realms, ["intl", "cn"])
        # ...and the name the bundled catalogue has never seen is the one
        # that enters the price input.
        self.assertEqual(ids, ["gpt-6-sol"])

    def test_the_pricing_lines_reach_the_running_gateway(self):
        fake = self._fake_gateway()
        refresher = wb_pricing.PriceRefresher(5)
        with unittest.mock.patch.dict(sys.modules, {"__main__": fake}):
            refresher.log("已取价：1 个模型")
            wb_pricing._log_pricing("按需补价：gpt-6-sol")
        self.assertEqual(fake.lines, [("[定价] 已取价：1 个模型", "pricing"),
                                      ("[定价] 按需补价：gpt-6-sol", "pricing")])

    def test_the_alias_set_comes_from_the_running_gateway_too(self):
        fake = self._fake_gateway()
        with unittest.mock.patch.dict(sys.modules, {"__main__": fake}):
            self.assertEqual(wb_pricing.virtual_alias_names(),
                             {"default-model", "auto"})

    def test_a_stale_second_copy_is_never_preferred(self):
        running, twin = self._fake_gateway(), self._fake_gateway()
        with unittest.mock.patch.dict(sys.modules, {"__main__": running,
                                                    "wb_proxy": twin}):
            self.assertIs(wb_pricing.gateway_module(), running)

    def test_a_plain_process_uses_the_imported_wb_proxy_module(self):
        with unittest.mock.patch.dict(
                sys.modules, {"__main__": types.ModuleType("__main__")}):
            self.assertIs(wb_pricing.gateway_module(), P)

    def test_nothing_imports_wb_proxy_behind_the_resolver(self):
        """A stray plain import anywhere else would recreate the twin."""
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "wb_pricing.py")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        hits = [line.strip() for line in source.splitlines()
                if line.strip().startswith("import wb_proxy")]
        self.assertEqual(hits, ["import wb_proxy as module"])


class OnDemandPricingTests(PricingTestCase):
    """A model called between two fetches still gets a price on the first row."""

    def setUp(self):
        super().setUp()
        wb_pricing.remember_live_index(OR)

    def test_a_hit_registers_a_policy_and_returns_its_id(self):
        pid = wb_pricing.ensure_policy("glm-5.3-flashx")
        self.assertIsNotNone(pid)
        policy = wb_pricing.load_policies()[pid]
        self.assertEqual(policy["model"], "glm-5.3-flashx")
        self.assertEqual(policy["or_id"], "z-ai/glm-5.3-flashx")
        self.assertEqual(policy["via"], "direct")
        self.assertEqual(wb_pricing.current_policy_id("glm-5.3-flashx"), pid)

    def test_the_write_path_prices_the_first_row_of_a_live_only_model(self):
        # wb_proxy stamps every row with current_policy_id(model) at write
        # time; that call must mint the price on the spot, from memory only.
        pid = wb_pricing.current_policy_id("glm-5.3-flashx")
        self.assertIsNotNone(pid)
        row = {"model": "glm-5.3-flashx", "at": time.time(),
               "cost_policy": pid, "prompt_tokens": 1000000,
               "completion_tokens": 0, "cached_tokens": 0}
        cost = wb_pricing.cost_for_row(row)
        self.assertTrue(cost["known"])
        self.assertAlmostEqual(cost["cny"], 0.2 * wb_pricing.USD_CNY, places=6)
        self.assertEqual(cost["source"], pid)

    def test_the_registered_price_is_what_the_row_is_stamped_with(self):
        pid = wb_pricing.ensure_policy("glm-5.3-flashx")
        row = {"model": "glm-5.3-flashx", "at": time.time(),
               "prompt_tokens": 1000000, "completion_tokens": 0,
               "cached_tokens": 0}
        cost = wb_pricing.cost_for_row(row)
        self.assertTrue(cost["known"])
        self.assertAlmostEqual(cost["cny"], 0.2 * wb_pricing.USD_CNY, places=6)
        self.assertEqual(cost["source"], pid)

    def test_a_miss_returns_none_and_writes_nothing(self):
        self.assertIsNone(wb_pricing.ensure_policy("kimi-k2.8-preview"))
        self.assertIsNone(wb_pricing.ensure_policy("no-such-model"))
        self.assertEqual(self._lines("pricing-policies.jsonl"), [])
        self.assertEqual(self._lines("pricing-timeline.jsonl"), [])

    def test_a_cold_index_degrades_to_none(self):
        wb_pricing.remember_live_index(None)
        wb_pricing._live_index.update({"models": None, "by_norm": None, "at": None})
        self.assertIsNone(wb_pricing.ensure_policy("glm-5.3-flashx"))
        self.assertEqual(self._lines("pricing-policies.jsonl"), [])

    def test_the_second_call_writes_nothing_again(self):
        first = wb_pricing.ensure_policy("deepseek-r1-0528-lkeap")
        second = wb_pricing.ensure_policy("deepseek-r1-0528-lkeap")
        self.assertEqual(first, second)
        self.assertEqual(len(self._lines("pricing-policies.jsonl")), 1)
        self.assertEqual(len(self._lines("pricing-timeline.jsonl")), 1)

    def test_a_variant_hit_records_where_the_price_came_from(self):
        pid = wb_pricing.ensure_policy("deepseek-r1-0528-lkeap")
        policy = wb_pricing.load_policies()[pid]
        self.assertEqual(policy["via"], "variant")
        self.assertEqual(policy["inherited_from"], "deepseek-r1-0528")
        self.assertEqual(policy["or_id"], "deepseek/deepseek-r1-0528")

    def test_an_inherited_price_still_prices_the_row(self):
        wb_pricing.ensure_policy("deepseek-v3-1-lkeap")
        row = {"model": "deepseek-v3-1-lkeap", "at": time.time(),
               "prompt_tokens": 1000000, "completion_tokens": 0,
               "cached_tokens": 0}
        cost = wb_pricing.cost_for_row(row)
        self.assertTrue(cost["known"])
        self.assertAlmostEqual(cost["cny"], 0.25 * wb_pricing.USD_CNY, places=6)

    def test_concurrent_first_requests_register_once(self):
        results = []
        lock = threading.Lock()

        def work():
            pid = wb_pricing.ensure_policy("glm-5.3-flashx")
            with lock:
                results.append(pid)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(pid == results[0] for pid in results), results)
        # One policy row, one timeline row, and every line is valid JSON -
        # no interleaved half-writes.
        for name in ("pricing-policies.jsonl", "pricing-timeline.jsonl"):
            lines = self._lines(name)
            self.assertEqual(len(lines), 1, name)
            json.loads(lines[0])

    def test_an_on_demand_row_keeps_the_other_models_in_force(self):
        # A single-model registration must not shrink the timeline's view of
        # what is in force: it merges into the previous assignment.
        wb_pricing.record_policies(wb_pricing.build_snapshot(OR)[0])
        at, before = wb_pricing.current_assignment()
        self.assertGreater(len(before), 3)
        wb_pricing.ensure_policy("glm-5.3-flashx")
        _at, after = wb_pricing.current_assignment()
        for model in before:
            self.assertEqual(after.get(model), before[model], model)
        self.assertIn("glm-5.3-flashx", after)


class VariantInheritanceTests(PricingTestCase):
    """The suffix rule: inherit only from a unique base, and only when on."""

    def setUp(self):
        super().setUp()
        self.by_norm = wb_pricing.index_openrouter(OR)

    def test_the_three_suffix_names_inherit_from_their_bases(self):
        for mid, ref in (("deepseek-r1-0528-lkeap", "deepseek/deepseek-r1-0528"),
                         ("deepseek-v3-1-lkeap", "deepseek/deepseek-chat-v3.1"),
                         ("deepseek-v3-0324-lkeap",
                          "deepseek/deepseek-chat-v3-0324")):
            self.assertEqual(
                wb_pricing.resolve(mid, OR, self.by_norm, variants=True)[0],
                ref, mid)

    def test_the_two_names_without_a_counterpart_stay_unpriced(self):
        # kimi-k2-instruct-taiji strips to a base OpenRouter does not publish,
        # and kimi-k2.8-preview has no suffix to strip. Neither may be guessed.
        for mid in ("kimi-k2-instruct-taiji", "kimi-k2.8-preview"):
            self.assertIsNone(
                wb_pricing.resolve(mid, OR, self.by_norm, variants=True)[0], mid)

    def test_a_base_that_is_not_unique_is_not_inherited(self):
        two_vendors = dict(OR)
        two_vendors["othervendor/deepseek-r1-0528"] = rate(0.1, 0.1)
        by_norm = wb_pricing.index_openrouter(two_vendors)
        self.assertIsNone(wb_pricing.resolve("deepseek-r1-0528-lkeap",
                                            two_vendors, by_norm,
                                            variants=True)[0])

    def test_a_base_missing_from_the_list_is_not_inherited(self):
        self.assertIsNone(wb_pricing.resolve(
            "glm-5.3-flash-f", OR, self.by_norm, variants=True)[0])

    def test_the_uncertain_suffixes_are_not_peeled(self):
        # -f / -dev / -x never strip: hub uses them for its own tiers, and
        # peeling them would silently drop a per-tier price difference.
        for mid in ("hy3-x", "hy4-preview-f", "hy4-preview-dev"):
            self.assertEqual(list(wb_pricing.variant_bases(mid)), [], mid)
        self.assertEqual(list(wb_pricing.variant_bases("deepseek-r1-0528-lkeap")),
                         ["deepseek-r1-0528"])

    def test_the_switch_off_restores_the_old_behaviour(self):
        self.assertIsNone(wb_pricing.resolve("deepseek-r1-0528-lkeap", OR,
                                            self.by_norm, variants=False)[0])
        doc, unpriced, _ov = wb_pricing.build_snapshot(
            OR, None, None, variants=False)
        self.assertNotIn("deepseek-r1-0528-lkeap", doc["models"])
        self.assertIn("deepseek-r1-0528-lkeap", unpriced)
        doc_on, unpriced_on, _ov = wb_pricing.build_snapshot(
            OR, None, None, variants=True)
        self.assertIn("deepseek-r1-0528-lkeap", doc_on["models"])
        self.assertNotIn("deepseek-r1-0528-lkeap", unpriced_on)

    def test_the_setting_defaults_to_on_and_is_persisted(self):
        self.assertTrue(wb_settings.pricing_variant_inherit(self.accounts))
        self.assertTrue(wb_pricing.variant_inherit_enabled(self.accounts))
        self.assertFalse(wb_settings.set_pricing_variant_inherit(
            self.accounts, False))
        self.assertFalse(wb_settings.pricing_variant_inherit(self.accounts))
        self.assertFalse(wb_pricing.variant_inherit_enabled(self.accounts))
        with open(os.path.join(self.accounts, "settings.json"),
                  encoding="utf-8") as fh:
            self.assertIs(json.load(fh)["pricing_variant_inherit"], False)
        wb_settings.set_pricing_variant_inherit(self.accounts, True)
        self.assertTrue(wb_pricing.variant_inherit_enabled(self.accounts))

    def test_a_settings_file_without_the_key_reads_as_on(self):
        # An install upgraded from a build that never wrote the key keeps the
        # new behaviour (default on), which is what the release notes say.
        with open(os.path.join(self.accounts, "settings.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"pricing_refresh_minutes": 5}, fh)
        self.assertTrue(wb_pricing.variant_inherit_enabled(self.accounts))

    def test_the_switch_read_is_cached_but_never_goes_stale(self):
        # ensure_policy reads this on every request for an unpriced model, so
        # the read is cached by (path, mtime, size) - an external rewrite (the
        # panel saving settings.json) must still be picked up.
        path = os.path.join(self.accounts, "settings.json")
        self.assertTrue(wb_pricing.variant_inherit_enabled(self.accounts))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"pricing_variant_inherit": False}, fh)
        self.assertFalse(wb_pricing.variant_inherit_enabled(self.accounts))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"pricing_variant_inherit": True}, fh)
        self.assertTrue(wb_pricing.variant_inherit_enabled(self.accounts))


class GapReportTests(PricingTestCase):
    """What the panel shows for models that still have no price."""

    def test_the_report_categorises_every_kind(self):
        items, summary = wb_pricing.gap_items(OR, None, None, variants=True)
        by_model = {item["model"]: item for item in items}
        for alias in ALIASES:
            self.assertEqual(by_model[alias]["reason"], "alias", alias)
            self.assertNotIn("candidates", by_model[alias])
        self.assertEqual(by_model["kimi-k2.8-preview"]["reason"], "or_missing")
        self.assertEqual(by_model["kimi-k2-instruct-taiji"]["reason"],
                         "variant_unmatched")
        # Aliases are reported but never counted into the gap total, which is
        # the number an operator chases.
        self.assertEqual(summary["aliases"], len(ALIASES))
        self.assertEqual(summary["total"],
                         summary["or_missing"] + summary["variant_unmatched"])
        self.assertTrue(summary["variants_enabled"])
        self.assertEqual(summary["total"], len(items) - len(ALIASES))

    def test_a_variant_that_resolves_is_not_reported_at_all(self):
        items, _summary = wb_pricing.gap_items(OR, None, None, variants=True)
        reported = {item["model"] for item in items}
        for priced in ("deepseek-r1-0528-lkeap", "deepseek-v3-1-lkeap",
                       "deepseek-v3-0324-lkeap"):
            self.assertNotIn(priced, reported, priced)

    def test_with_the_switch_off_the_variant_names_are_reported(self):
        items, _summary = wb_pricing.gap_items(OR, None, None, variants=False)
        by_model = {item["model"]: item for item in items}
        self.assertEqual(by_model["deepseek-r1-0528-lkeap"]["reason"],
                         "variant_unmatched")
        # A model in the factory snapshot is priced regardless of the switch.
        self.assertNotIn("deepseek-v4.1-flash", by_model)

    def test_candidates_are_suggestions_never_applied(self):
        cands = wb_pricing.suggest_matches("glm-5.3-flash-20260101", OR)
        self.assertEqual(cands[0], "z-ai/glm-5.3-flash")
        # Suggesting is all it does: no policy, no override, no price.
        self.assertIsNone(wb_pricing.ensure_policy("glm-5.3-flash-20260101"))
        self.assertEqual(wb_pricing.runtime_overrides(), {})

    def test_a_hand_entered_mapping_prices_a_model(self):
        wb_pricing.remember_live_index(OR)
        self.assertIsNone(wb_pricing.ensure_policy("kimi-k2.8-preview"))
        wb_pricing.save_runtime_override("kimi-k2.8-preview",
                                        "moonshotai/kimi-k2.6")
        self.assertEqual(wb_pricing.overrides_path(),
                         os.path.join(self.dir, "pricing-overrides.json"))
        by_norm = wb_pricing.index_openrouter(OR)
        self.assertEqual(
            wb_pricing.resolve("kimi-k2.8-preview", OR, by_norm)[0],
            "moonshotai/kimi-k2.6")
        pid = wb_pricing.ensure_policy("kimi-k2.8-preview")
        self.assertIsNotNone(pid)
        self.assertEqual(wb_pricing.load_policies()[pid]["or_id"],
                         "moonshotai/kimi-k2.6")

    def test_a_hand_entered_mapping_that_matches_nothing_is_ignored(self):
        wb_pricing.save_runtime_override("kimi-k2.8-preview",
                                        "moonshotai/not-in-this-list")
        by_norm = wb_pricing.index_openrouter(OR)
        self.assertIsNone(
            wb_pricing.resolve("kimi-k2.8-preview", OR, by_norm)[0])

    def test_a_mapping_can_be_cleared(self):
        wb_pricing.save_runtime_override("kimi-k2.8-preview",
                                        "moonshotai/kimi-k2.6")
        wb_pricing.save_runtime_override("kimi-k2.8-preview", "")
        self.assertEqual(wb_pricing.runtime_overrides(), {})
        by_norm = wb_pricing.index_openrouter(OR)
        self.assertIsNone(
            wb_pricing.resolve("kimi-k2.8-preview", OR, by_norm)[0])

    def test_the_report_covers_a_live_only_name_that_could_not_be_priced(self):
        original = P.curated_live_sources
        P.curated_live_sources = lambda realm: (
            ([("glm-5.3-flash-20260101", {})] if realm == "intl"
             else [("glm-5.3-flashx", {})]), True)
        try:
            refresher = self._refresher()
            ok, message = refresher.run_once()
        finally:
            P.curated_live_sources = original
        self.assertTrue(ok, message)
        items, summary = refresher.gap_report(ttl=0)
        by_model = {item["model"]: item for item in items}
        # The live-only name OpenRouter does not carry is visible, with the
        # closest entry suggested for a hand mapping, and counted in the total.
        row = by_model["glm-5.3-flash-20260101"]
        self.assertEqual(row["reason"], "or_missing")
        self.assertEqual(row["candidates"][0], "z-ai/glm-5.3-flash")
        self.assertEqual(summary["total"], len(items) - len(ALIASES))
        # A live-only name that did resolve was priced by that very cycle, so
        # it is not a gap at all.
        self.assertNotIn("glm-5.3-flashx", by_model)
        self.assertIsNotNone(wb_pricing.ensure_policy("glm-5.3-flashx"))

    def test_the_status_payload_carries_the_report(self):
        wb_pricing.remember_live_index(OR)
        refresher = self._refresher()
        refresher.run_once()
        status = refresher.status()
        self.assertIn("gap_summary", status)
        self.assertIsInstance(status["gaps"], list)
        self.assertIn("variant_inherit", status)
        # The on-demand path is fed by the same fetch that just ran.
        self.assertTrue(wb_pricing.ensure_policy("glm-5.3-flashx"))
        models = {item["model"] for item in status["gaps"]}
        self.assertNotIn("glm-5.3-flashx", models)

    def test_the_report_never_counts_a_price_that_is_already_stored(self):
        wb_pricing.remember_live_index(OR)
        wb_pricing.ensure_policy("kimi-k2.8-preview")
        wb_pricing.save_runtime_override("kimi-k2.8-preview",
                                        "moonshotai/kimi-k2.6")
        wb_pricing.ensure_policy("kimi-k2.8-preview")
        refresher = self._refresher()
        items, _summary = refresher.gap_report(ttl=0)
        self.assertNotIn("kimi-k2.8-preview", {i["model"] for i in items})


class PricingEndpointTests(PricingTestCase):
    """The panel's two pricing writes, driven through the handler methods."""

    def _handler(self, payload, pricing=None):
        test = self

        class Stub(object):
            _handle_settings_save = P.Handler._handle_settings_save
            _validate_settings_save = P.Handler._validate_settings_save
            _route_pricing_mapping = P.Handler._route_pricing_mapping

            def __init__(self):
                self.replies = []

            def _payload_or_error(self, allow_list=()):
                return payload

            def _json(self, status, data):
                self.replies.append((status, data))
                return status, data

            def _error(self, status, message, kind=""):
                self.replies.append((status, message))
                return status, message

        return Stub()

    def test_the_variant_switch_is_persisted_and_kicks_a_refresh(self):
        started = threading.Event()

        class Pricing(object):
            def invalidate_gaps(self):
                pass

            def run_once(self):
                started.set()
                return True, ""

        with unittest.mock.patch.object(P, "ACCOUNTS_DIR", self.accounts), \
                unittest.mock.patch.object(P, "PRICING", Pricing()):
            handler = self._handler({"pricing_variant_inherit": False})
            status, data = handler._handle_settings_save()
        self.assertEqual(status, 200)
        self.assertIs(data["pricing_variant_inherit"], False)
        self.assertTrue(data["pricing_refresh_started"])
        self.assertTrue(started.wait(5.0), "the switch must trigger a re-price")
        self.assertFalse(wb_settings.pricing_variant_inherit(self.accounts))

    def test_the_variant_switch_rejects_a_string(self):
        with unittest.mock.patch.object(P, "ACCOUNTS_DIR", self.accounts):
            handler = self._handler({"pricing_variant_inherit": "false"})
            status, _data = handler._handle_settings_save()
        self.assertEqual(status, 400)
        # Strictly boolean: a string would read as true and keep the feature
        # on for the one operator trying to turn it off.
        self.assertTrue(wb_settings.pricing_variant_inherit(self.accounts))

    def test_an_unchanged_switch_does_not_kick_a_refresh(self):
        wb_settings.set_pricing_variant_inherit(self.accounts, False)
        with unittest.mock.patch.object(P, "ACCOUNTS_DIR", self.accounts), \
                unittest.mock.patch.object(P, "PRICING", None):
            handler = self._handler({"pricing_variant_inherit": False})
            status, data = handler._handle_settings_save()
        self.assertEqual(status, 200)
        self.assertNotIn("pricing_refresh_started", data)

    def test_a_hand_entered_mapping_is_written_and_a_bad_id_is_refused(self):
        class Pricing(object):
            def invalidate_gaps(self):
                pass

            def run_once(self):
                return True, ""

        with unittest.mock.patch.object(P, "PRICING", Pricing()):
            handler = self._handler(None)
            status, data = handler._route_pricing_mapping(
                {"model": "kimi-k2.8-preview", "or_id": "moonshotai/kimi-k2.6"})
            self.assertEqual(status, 200)
            self.assertIn("msg", data)
            # 形状不像 OpenRouter id 的值直接拒绝
            status, _data = handler._route_pricing_mapping(
                {"model": "kimi-k2.8-preview", "or_id": "not an id"})
            self.assertEqual(status, 400)
            # 留空 = 删除映射
            status, _data = handler._route_pricing_mapping(
                {"model": "kimi-k2.8-preview", "or_id": ""})
            self.assertEqual(status, 200)
        self.assertEqual(wb_pricing.runtime_overrides(), {})


class RouteRegistrationTests(PricingTestCase):
    """POST /pricing/* must be registered, or the panel's buttons 404.

    The dashboard's 立即取价 button used to fail with a 404 because the path
    was never routed, and nothing but the browser noticed. This is a textual
    guard on the dispatcher, the same way the dashboard handler test sweeps
    the page.
    """

    def test_do_post_routes_the_pricing_writes(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "wb_proxy.py")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        body = source.split("def do_POST(self):", 1)[1]
        head = body.split("def ", 1)[0]
        for route in ("/pricing/refresh", "/pricing/mapping"):
            self.assertIn(route, head, route)


if __name__ == "__main__":
    unittest.main()
