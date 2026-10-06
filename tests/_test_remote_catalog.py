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
        P._models_cache["intl"] = {"at": 0.0, "data": None}
        P._models_cache["cn"] = {"at": 0.0, "data": None}
        self._orig = (P.fetch_remote_product_config,
                                P.read_product_config_models,
                                P.fetch_endpoint_models,
                                P.product_config_path)

    def tearDown(self):
        (P.fetch_remote_product_config, P.read_product_config_models,
         P.fetch_endpoint_models, P.product_config_path) = self._orig

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
        P.fetch_remote_product_config = (
            lambda realm: (INTL_IDS, meta) if realm == "intl" else None)
        entries = dict(P.fetch_models("intl"))
        self.assertEqual(entries["hy4-preview-f"]["credits"], "x0.29")
        self.assertEqual(
            P.model_entry("hy4-preview-f", entries["hy4-preview-f"])["credits"],
            "x0.29")

        # 2. The endpoint stops listing it: the bundled value survives, and that
        #    is exactly what the panel then shows.
        ids = [m for m in INTL_IDS if m != "hy4-preview-f"]
        meta = dict(INTL_META)
        meta.pop("hy4-preview-f", None)
        P.fetch_remote_product_config = (
            lambda realm: (ids, meta) if realm == "intl" else None)
        P._models_cache["intl"] = {"at": 0.0, "data": None}
        entries = dict(P.fetch_models("intl"))
        self.assertIn("hy4-preview-f", entries)
        self.assertEqual(entries["hy4-preview-f"]["credits"], "x0.00")
        self.assertEqual(
            P.model_entry("hy4-preview-f", entries["hy4-preview-f"])["credits"],
            "x0.00")

    def test_parse_keeps_order_and_metadata(self):
        ids, meta = P.parse_remote_catalog(payload(INTL_IDS, INTL_META))
        self.assertEqual(ids, INTL_IDS)
        self.assertEqual(meta["grok-4.7"]["credits"], "x1.90")

    def test_parse_rejects_empty_and_odd_shapes(self):
        self.assertIsNone(P.parse_remote_catalog({"data": {"agents": []}}))
        self.assertIsNone(P.parse_remote_catalog(
            {"data": {"agents": [{"models": []}]}}))
        self.assertIsNone(P.parse_remote_catalog({}))
        self.assertIsNone(P.parse_remote_catalog(None))
        ids, _ = P.parse_remote_catalog(
            {"data": {"agents": {"cli": {"models": ["a", "b"]}}}})
        self.assertEqual(ids, ["a", "b"])

    def test_curate_applies_the_rules(self):
        curated = P.curate_remote_catalog("intl", INTL_IDS, INTL_META)
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
        self.assertEqual(P.curate_remote_catalog("intl", ids, meta),
                    ["hy4-preview-f", "hy3"])

    def test_curate_keeps_a_paid_model_with_no_free_sibling(self):
        ids = ["grok-4.7", "glm-5.3"]
        meta = {"grok-4.7": {"credits": "x1.90"},
             "glm-5.3": {"credits": "x0.79"}}
        self.assertEqual(P.curate_remote_catalog("intl", ids, meta), ids)

    def test_fetch_models_uses_the_remote_and_orders_by_the_table(self):
        P.fetch_remote_product_config = (
            lambda realm: (INTL_IDS, INTL_META) if realm == "intl" else None)
        ids = [m for m, _ in P.fetch_models("intl")]
        self.assertEqual(ids, [
            "hy4-preview-f", "hy3", "deepseek-v4.1-flash", "gpt-6-astra",
            "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5",
            "gpt-5.4", "grok-4.7", "gemini-3.5-flash", "glm-5.3-flash",
            "glm-5.3", "glm-5.2", "kimi-k3", "kimi-k2.6", "kimi-k2.8-preview"])

    def test_cn_keeps_the_pinned_free_variant(self):
        P.fetch_remote_product_config = (
            lambda realm: (CN_IDS, CN_META) if realm == "cn" else None)
        ids = [m for m, _ in P.fetch_models("cn")]
        self.assertEqual(ids[0], "hy4-preview-f")
        self.assertIn("hy4-preview-f", ids)    # pin: free, remote no longer lists it
        self.assertNotIn("hy4-preview", ids)   # paid sibling of the free one
        self.assertNotIn("hy3-x", ids)         # -x variant
        self.assertNotIn("auto", ids)          # auto-router alias

    def test_remote_failure_falls_back_and_never_leaks_new_names(self):
        calls = []

        def boom(realm):
            calls.append(realm)
            return None

        P.fetch_remote_product_config = boom
        P.read_product_config_models = lambda realm=None: []
        P.fetch_endpoint_models = lambda: ["kimi-k3", "internal-only-model"]
        ids = [m for m, _ in P.fetch_models("intl")]
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
        P.fetch_remote_product_config = (
            lambda realm: (INTL_IDS, meta) if realm == "intl" else None)
        entries = dict(P.fetch_models("intl"))
        item = P.model_entry("deepseek-v4.1-flash",
                             entries["deepseek-v4.1-flash"])
        self.assertEqual(item["reasoning_efforts"], ["low", "medium", "high"])
        self.assertEqual(item["reasoning_default_effort"], "medium")

    def test_bundled_efforts_fill_in_when_the_live_entry_has_none(self):
        """A silent live entry falls back to the snapshot, and says so once."""
        logged = []
        original_log = P.log
        P.log = lambda message, **kw: logged.append(message)
        try:
            P.fetch_remote_product_config = (
                lambda realm: (INTL_IDS, dict(INTL_META)) if realm == "intl" else None)
            P._catalog_fallback_log.pop("intl", None)
            entries = dict(P.fetch_models("intl"))
        finally:
            P.log = original_log
        item = P.model_entry("deepseek-v4.1-flash",
                             entries["deepseek-v4.1-flash"])
        self.assertEqual(item["reasoning_efforts"], ["low", "high", "max"])
        self.assertEqual(item["reasoning_default_effort"], "high")
        self.assertTrue(any("bundled table" in m for m in logged), logged)

    def test_parse_reads_the_desktop_cache_shape(self):
        """The cache file is the same document without the "data" envelope."""
        ids, _ = P.parse_remote_catalog(
            {"agents": [{"name": "compact", "models": ["lite"]},
                        {"name": "cli", "models": ["a", "b"]}]})
        self.assertEqual(ids, ["a", "b"])   # cli wins over the other agent
        ids, _ = P.parse_remote_catalog(
            {"agents": [{"name": "planner", "models": ["a", "b", "c"]}]})
        self.assertEqual(ids, ["a", "b", "c"])

    def test_cached_desktop_file_drives_the_list_without_the_endpoint(self):
        P.fetch_remote_product_config = lambda realm: None
        payload = {"agents": [{"name": "cli",
                               "models": CN_IDS + ["new-model-9"]}],
                   "models": [{"id": "hy3", "credits": "x0.00"}]}
        path = os.path.join(_TMP, "acc-product-config-v3.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        P.product_config_path = lambda realm: path
        P._models_cache["cn"] = {"at": 0.0, "data": None}
        ids = [m for m, _ in P.fetch_models("cn")]
        self.assertEqual(ids[0], "hy4-preview-f")
        self.assertNotIn("auto", ids)
        self.assertIn("new-model-9", ids)   # live-only name ships, no release


if __name__ == "__main__":
    unittest.main()
