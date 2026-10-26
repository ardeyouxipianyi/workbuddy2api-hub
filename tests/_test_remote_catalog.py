"""The advertised catalogue follows the desktop product-config endpoint.

Issue #85: the gateway used to read the desktop client's *cached* copy of
GET /v3/config, so a machine without the desktop app could only offer the
narrow model endpoint or the bundled snapshot. It now calls the endpoint
itself and curates the result:

  - virtual aliases (default-model ... auto) are dropped: they are picker
    shorthands, not models;
  - "-sg" / "-x" builds are the paid variant of a name the list already has;
  - when a free ("x0.00") sibling exists, the free one is the advertised
    one (deepseek-v4.1-flash over -sg, hy4-preview-f over hy4-preview);
  - a name only the curated tables know - the free hy4-preview-f on the
    domestic exit, which the remote no longer lists - survives, and a name
    only the remote knows (grok-4.7) ships without a release.

No network: the payloads are synthesised and the fetch is stubbed.
"""
import os
import json
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-remote-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_proxy as P

import wb_models as MC


def payload(ids, meta=None):
    """A /v3/config response in the shape both exits answer with."""
    return {
        "code": 0,
        "data": {
            "agents": [{"name": "cli", "models": list(ids)}],
            "products": [{"models": [dict({"id": mid}, **(meta or {}).get(mid, {}))
                                      for mid in ids]}],
        },
    }


INTL_IDS = [
    "default-model", "fast-model", "balanced-model", "primary-model",
    "deep-model", "hy4-preview-f", "hy3", "deepseek-v4.1-flash",
    "deepseek-v4.1-flash-sg", "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra",
    "gpt-5.6-luna", "gpt-5.5", "gpt-5.4", "grok-4.7", "gemini-3.5-flash",
    "glm-5.3-flash", "glm-5.3", "glm-5.2", "kimi-k3", "kimi-k2.6",
    "kimi-k2.8-preview",
]
INTL_META = {
    "deepseek-v4.1-flash": {"credits": "x0.00"},
    "deepseek-v4.1-flash-sg": {"credits": "x0.03"},
    "hy4-preview-f": {"credits": "x0.00"},
    "hy4-preview": {"credits": "x0.29"},
    "grok-4.7": {"credits": "x1.90"},
    "hy3": {"credits": "x0.00"},
    "glm-5.3": {"credits": "x0.79"},
}

CN_IDS = [
    "auto", "hy4-preview", "hy3", "hy3-x", "deepseek-v4.1-flash",
    "glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.1", "glm-5v-turbo",
    "minimax-m3", "kimi-k3-1", "kimi-k2.8-preview", "kimi-k2.7",
    "kimi-k2.6", "deepseek-v4-pro",
]
CN_META = {
    "hy4-preview": {"credits": "x0.29"},
    "hy3": {"credits": "x0.00"},
    "hy3-x": {"credits": "x0.05"},
    "deepseek-v4.1-flash": {"credits": "x0.11"},
}


class RemoteCatalogTests(unittest.TestCase):
    def setUp(self):
        MC._models_cache["intl"] = {"at": 0.0, "data": None}
        MC._models_cache["cn"] = {"at": 0.0, "data": None}
        self._orig = (MC.fetch_remote_product_config,
                                MC.read_product_config_models,
                                MC.fetch_endpoint_models,
                                MC.product_config_path)

    def tearDown(self):
        (MC.fetch_remote_product_config, MC.read_product_config_models,
         MC.fetch_endpoint_models, MC.product_config_path) = self._orig

    def test_credits_follow_the_endpoint_and_fall_back_for_a_pinned_model(self):
        """What the multiplier badge can follow, and what it cannot.

        The badge renders whatever /v1/models reports, so the question is where
        that value comes from. A model the endpoint still lists follows the
        endpoint; a model it no longer lists - the cn free hy4-preview-f, pinned
        by the curated table since #85 - keeps the bundled value, which by
        definition cannot follow a price change until the endpoint lists it
        again. Both halves, plus the model_entry() hop the panel reads.
        """
        # 1. The endpoint declares a new price for a curated model: it wins.
        meta = dict(INTL_META)
        meta["hy4-preview-f"] = {"credits": "x0.29"}
        MC.fetch_remote_product_config = (
            lambda realm: (INTL_IDS, meta) if realm == "intl" else None)
        entries = dict(MC.fetch_models("intl"))
        self.assertEqual(entries["hy4-preview-f"]["credits"], "x0.29")
        self.assertEqual(
            MC.model_entry("hy4-preview-f", entries["hy4-preview-f"])["credits"],
            "x0.29")

        # 2. The endpoint stops listing it: the bundled value survives, and that
        #    is exactly what the panel then shows.
        ids = [m for m in INTL_IDS if m != "hy4-preview-f"]
        meta = dict(INTL_META)
        meta.pop("hy4-preview-f", None)
        MC.fetch_remote_product_config = (
            lambda realm: (ids, meta) if realm == "intl" else None)
        MC._models_cache["intl"] = {"at": 0.0, "data": None}
        entries = dict(MC.fetch_models("intl"))
        self.assertIn("hy4-preview-f", entries)
        self.assertEqual(entries["hy4-preview-f"]["credits"], "x0.00")
        self.assertEqual(
            MC.model_entry("hy4-preview-f", entries["hy4-preview-f"])["credits"],
            "x0.00")

    def test_parse_keeps_order_and_metadata(self):
        ids, meta = MC.parse_remote_catalog(payload(INTL_IDS, INTL_META))
        self.assertEqual(ids, INTL_IDS)
        self.assertEqual(meta["grok-4.7"]["credits"], "x1.90")

    def test_parse_rejects_empty_and_odd_shapes(self):
        self.assertIsNone(MC.parse_remote_catalog({"data": {"agents": []}}))
        self.assertIsNone(MC.parse_remote_catalog(
            {"data": {"agents": [{"models": []}]}}))
        self.assertIsNone(MC.parse_remote_catalog({}))
        self.assertIsNone(MC.parse_remote_catalog(None))
        ids, _ = MC.parse_remote_catalog(
            {"data": {"agents": {"cli": {"models": ["a", "b"]}}}})
        self.assertEqual(ids, ["a", "b"])

    def test_curate_applies_the_rules(self):
        curated = MC.curate_remote_catalog("intl", INTL_IDS, INTL_META)
        self.assertEqual(curated, [m for m in INTL_IDS if m not in (
            "default-model", "fast-model", "balanced-model", "primary-model",
            "deep-model", "deepseek-v4.1-flash-sg")])
        self.assertIn("grok-4.7", curated)
        self.assertIn("hy4-preview-f", curated)

    def test_curate_prefers_the_free_sibling(self):
        ids = ["hy4-preview", "hy4-preview-f", "hy3", "hy3-x"]
        meta = {"hy4-preview": {"credits": "x0.29"},
             "hy4-preview-f": {"credits": "x0.00"},
             "hy3": {"credits": "x0.00"}, "hy3-x": {"credits": "x0.05"}}
        self.assertEqual(MC.curate_remote_catalog("intl", ids, meta),
                    ["hy4-preview-f", "hy3"])

    def test_curate_keeps_a_paid_model_with_no_free_sibling(self):
        ids = ["grok-4.7", "glm-5.3"]
        meta = {"grok-4.7": {"credits": "x1.90"},
             "glm-5.3": {"credits": "x0.79"}}
        self.assertEqual(MC.curate_remote_catalog("intl", ids, meta), ids)

    def test_fetch_models_uses_the_remote_and_orders_by_the_table(self):
        MC.fetch_remote_product_config = (
            lambda realm: (INTL_IDS, INTL_META) if realm == "intl" else None)
        ids = [m for m, _ in MC.fetch_models("intl")]
        self.assertEqual(ids, [
            "hy4-preview-f", "hy3", "deepseek-v4.1-flash", "gpt-6-astra",
            "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5",
            "gpt-5.4", "grok-4.7", "gemini-3.5-flash", "glm-5.3-flash",
            "glm-5.3", "glm-5.2", "kimi-k3", "kimi-k2.6", "kimi-k2.8-preview"])

    def test_cn_keeps_the_pinned_free_variant(self):
        # 实时 meta 为准：上游已下线 hy4-preview-f 且 hy4-preview 为收费 (0.29x) 时，如实对外宣告 hy4-preview
        MC.fetch_remote_product_config = (
            lambda realm: (CN_IDS, CN_META) if realm == "cn" else None)
        ids = [m for m, _ in MC.fetch_models("cn")]
        self.assertEqual(ids[0], "hy4-preview")
        self.assertIn("hy4-preview", ids)
        self.assertNotIn("hy4-preview-f", ids)
        self.assertNotIn("hy3-x", ids)         # -x variant
        self.assertNotIn("auto", ids)          # auto-router alias

    def test_remote_failure_falls_back_and_never_leaks_new_names(self):
        calls = []

        def boom(realm):
            calls.append(realm)
            return None

        MC.fetch_remote_product_config = boom
        MC.read_product_config_models = lambda realm=None: []
        MC.fetch_endpoint_models = lambda: ["kimi-k3", "internal-only-model"]
        ids = [m for m, _ in MC.fetch_models("intl")]
        self.assertEqual(calls, ["intl"])
        self.assertIn("kimi-k3", ids)
        self.assertIn("grok-4.7", ids)   # the bundled snapshot still fills names
        self.assertNotIn("internal-only-model", ids)  # endpoint keeps the table filter

    def test_live_reasoning_efforts_win_over_the_bundled_table(self):
        """The endpoint's reasoning block is the source of truth.

        deepseek-v4.1-flash used to be pinned to low/high/max in code, which
        silently overwrote whatever the live catalogue declared (the pin dates
        from before the gateway called /v3/config at all).
        """
        meta = dict(INTL_META)
        meta["deepseek-v4.1-flash"] = {
            "credits": "x0.00",
            "reasoning": {"supportedEfforts": ["low", "medium", "high"],
                          "defaultEffort": "medium"},
        }
        MC.fetch_remote_product_config = (
            lambda realm: (INTL_IDS, meta) if realm == "intl" else None)
        entries = dict(MC.fetch_models("intl"))
        item = MC.model_entry("deepseek-v4.1-flash",
                             entries["deepseek-v4.1-flash"])
        self.assertEqual(item["reasoning_efforts"], ["low", "medium", "high"])
        self.assertEqual(item["reasoning_default_effort"], "medium")

    def test_bundled_efforts_fill_in_when_the_live_entry_has_none(self):
        """A silent live entry falls back to the snapshot, and says so once."""
        logged = []
        original_log = P.log
        P.log = lambda message, **kw: logged.append(message)
        try:
            MC.fetch_remote_product_config = (
                lambda realm: (INTL_IDS, dict(INTL_META)) if realm == "intl" else None)
            MC._catalog_fallback_log.pop("intl", None)
            entries = dict(MC.fetch_models("intl"))
        finally:
            P.log = original_log
        item = MC.model_entry("deepseek-v4.1-flash",
                             entries["deepseek-v4.1-flash"])
        self.assertEqual(item["reasoning_efforts"], ["low", "high", "max"])
        self.assertEqual(item["reasoning_default_effort"], "high")
        self.assertTrue(any("bundled table" in m for m in logged), logged)

    def test_bare_live_effort_keeps_the_model_selectable(self):
        """Issue #170: a bare live `effort` is the default, not a pin.

        The endpoint answers with {"effort": "high"} for deepseek-v4.1-flash
        while the bundled table knows the level is selectable. Taking the live
        block at face value dropped supportedEfforts, so /v1/models advertised
        the model as pinned to high - and every request was recorded at high
        even when the client sent max, the level it was really forwarded with.
        """
        meta = dict(INTL_META)
        meta["deepseek-v4.1-flash"] = {
            "credits": "x0.00",
            "reasoning": {"effort": "high", "summary": "auto"},
        }
        logged = []
        original_log = P.log
        P.log = lambda message, **kw: logged.append(message)
        try:
            MC.fetch_remote_product_config = (
                lambda realm: (INTL_IDS, meta) if realm == "intl" else None)
            MC._catalog_fallback_log.pop("intl", None)
            entries = dict(MC.fetch_models("intl"))
        finally:
            P.log = original_log
        item = MC.model_entry("deepseek-v4.1-flash",
                             entries["deepseek-v4.1-flash"])
        self.assertEqual(item["reasoning_efforts"], ["low", "high", "max"])
        self.assertEqual(item["reasoning_default_effort"], "high")
        self.assertNotIn("reasoning_fixed_effort", item)
        # The level the request really ran at follows the client again.
        self.assertIsNone(P.model_fixed_effort("deepseek-v4.1-flash"))
        self.assertEqual(
            P.upstream_effort_of({"reasoning_effort": "max"},
                                 "deepseek-v4.1-flash"),
            "max")
        # A model the live catalogue really does pin is untouched: no snapshot
        # supportedEfforts to restore, so its bare effort still pins it.
        pinned = MC.model_entry("gemini-3.5-flash", entries["gemini-3.5-flash"])
        self.assertEqual(pinned["reasoning_fixed_effort"], "medium")
        self.assertNotIn("reasoning_efforts", pinned)
        # And the shape is announced, not applied silently.
        self.assertTrue(any("bundled table" in m for m in logged), logged)

    def test_parse_reads_the_desktop_cache_shape(self):
        """The cache file is the same document without the "data" envelope."""
        ids, _ = MC.parse_remote_catalog(
            {"agents": [{"name": "compact", "models": ["lite"]},
                        {"name": "cli", "models": ["a", "b"]}]})
        self.assertEqual(ids, ["a", "b"])   # cli wins over the other agent
        ids, _ = MC.parse_remote_catalog(
            {"agents": [{"name": "planner", "models": ["a", "b", "c"]}]})
        self.assertEqual(ids, ["a", "b", "c"])

    def test_cached_desktop_file_drives_the_list_without_the_endpoint(self):
        MC.fetch_remote_product_config = lambda realm: None
        payload = {"agents": [{"name": "cli",
                               "models": CN_IDS + ["new-model-9", "uncredited-model"]}],
                   "models": [{"id": "hy3", "credits": "x0.00"},
                              {"id": "new-model-9", "credits": "x1.50"}]}
        path = os.path.join(_TMP, "acc-product-config-v3.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        MC.product_config_path = lambda realm: path
        MC._models_cache["cn"] = {"at": 0.0, "data": None}
        ids = [m for m, _ in MC.fetch_models("cn")]
        self.assertEqual(ids[0], "hy4-preview")
        self.assertNotIn("auto", ids)
        self.assertIn("new-model-9", ids)   # live-only name with credits ships, no release
        self.assertNotIn("uncredited-model", ids)  # unpriced model is hidden until credits configured


if __name__ == "__main__":
    unittest.main()
