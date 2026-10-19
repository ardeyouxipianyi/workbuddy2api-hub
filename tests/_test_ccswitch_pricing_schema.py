"""The cc-switch export: schema, field names and units, verbatim from its source.

cc-switch never trusts a cost reported upstream. It reads the token counts of
every request, multiplies them by the per-million prices in its own
`model_pricing` table, and stores the result. When that table has no row for the
model id it saw, the row lands with `total_cost_usd = 0` and the UI labels the
whole request as unpriced (`src/types/usage.ts`, `isUnpricedUsage()`: a 2xx row
with tokens, a finite multiplier, and `totalCostUsd === 0`). The DSH session
importer is explicitly on that path: it parses tokens only, then looks the price
up with `find_model_pricing(conn, &record.model)`
(`src-tauri/src/services/session_usage_dsh.rs`). The reverse-proxy usage parser
carries no cost field at all (`src-tauri/src/proxy/usage/parser.rs`).

That is why a `cost` field appended to our response cannot fix the symptom: the
value cc-switch displays is computed from its own table, and what it needs from
us is a price row whose `modelId` is the model string it sees. The deliverable
is therefore a file it can deserialize, and this suite pins that file's shape.

The shape below is taken from cc-switch's own definitions, not inferred from
our naming:

- `model-pricing.json` is `ModelPricingFile` (`services/model_pricing.rs`):
  top level `version` (u32, currently 1), `modelsDevSync`, `models`,
  `deletedModelIds`. Only `version` is mandatory on input; the rest carry
  `#[serde(default)]`.
- Each entry is `ModelPricingInfo`, and the struct has
  `#[serde(rename_all = "camelCase")]`, so the wire names are exactly
  `modelId`, `displayName`, `inputCostPerMillion`, `outputCostPerMillion`,
  `cacheReadCostPerMillion`, `cacheCreationCostPerMillion`. Snake_case keys are
  ignored by serde and the entry is then rejected for missing fields, so a
  wrong key spelling costs the whole import.
- The four prices are Rust `String`, parsed with `Decimal::from_str` in
  `normalize_decimal()`. A JSON number is not accepted, an empty string and a
  negative value are rejected. `displayName` and `modelId` must be non-empty or
  `normalize_pricing()` refuses the file.
- The unit is US dollars per million tokens: the UI prints them under
  `USD / 每百万 Tokens` and `CostCalculator` divides by 1,000,000. That is the
  same unit as `flat` in `pricing/pricing.json`, so the export must not scale.

Two consequences the export has to live with, both pinned below: a model has a
single price set in cc-switch, so a snapshot `bands` entry can only export its
`flat` base rate (long-context requests are then priced low, not wrong); and
nothing upstream counts cache-write tokens, so `cacheCreationCostPerMillion` is
`"0"` rather than a made-up rate.

The suite drives `wb_pricing.cc_switch_pricing()` itself. There is no local
mirror on purpose: a copy here would pass while the real export drifted.

    python tests/_test_ccswitch_pricing_schema.py
"""
import json
import os
import re
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _isolated_dirs import isolated_data_dirs  # noqa: E402  (its own data root)

_TMP = isolated_data_dirs("wb-ccswitch-pricing-")

import wb_pricing  # noqa: E402

# Verbatim from cc-switch: `MODEL_PRICING_FILE_VERSION` and the two structs in
# `src-tauri/src/services/model_pricing.rs`. Hard-coded on purpose - a test that
# imported these from our own code would agree with any renaming.
FILE_VERSION = 1
TOP_LEVEL_KEYS = ("version", "modelsDevSync", "models", "deletedModelIds")
SYNC_KEYS = ("autoSyncEnabled", "includeCommonModels", "selectedModelKeys",
             "excludedCommonModelKeys", "lastSyncAt", "lastSyncError")
MODEL_KEYS = ("modelId", "displayName", "inputCostPerMillion",
              "outputCostPerMillion", "cacheReadCostPerMillion",
              "cacheCreationCostPerMillion")
PRICE_KEYS = ("inputCostPerMillion", "outputCostPerMillion",
              "cacheReadCostPerMillion", "cacheCreationCostPerMillion")

MILLION = Decimal("1000000")
PLAIN_DECIMAL = re.compile(r"^\d+(\.\d+)?$")
# Prices are per million tokens, so a tolerance this tight still catches the
# mistake that matters - exporting a per-token rate (a factor of 1e6 off).
TOLERANCE = Decimal("0.000001")


def snapshot_entries():
    """The active snapshot's model table, entries only."""
    models = wb_pricing.load_pricing().get("models") or {}
    return dict((mid, entry) for mid, entry in models.items()
                if isinstance(entry, dict))


def assigned_policies():
    """{model: policy} for the policy in force now; empty on a fresh data dir."""
    _at, assignment = wb_pricing.current_assignment()
    policies = wb_pricing.load_policies() or {}
    return dict((mid, policies[pid]) for mid, pid in (assignment or {}).items()
                if pid in policies)


def expected_rates(entry):
    """The four per-million USD rates the snapshot's flat table implies.

    The cache-read rate falls back to the miss rate when the snapshot has no
    `input_cache_hit` (three models have none) - the same fallback
    `wb_pricing._pick(rate, "input_cache_hit", "input_cache_miss")` applies, so
    the two sides price a cache hit identically instead of one side guessing 0.
    """
    flat = (entry or {}).get("flat") or {}
    miss = float(flat.get("input_cache_miss") or 0.0)
    hit = flat.get("input_cache_hit")
    return {
        "inputCostPerMillion": miss,
        "outputCostPerMillion": float(flat.get("output") or 0.0),
        "cacheReadCostPerMillion": miss if hit is None else float(hit),
        "cacheCreationCostPerMillion": 0.0,
    }


class CcSwitchPricingSchemaTests(unittest.TestCase):
    """The exported document, and what cc-switch would compute from it."""

    @classmethod
    def setUpClass(cls):
        cls.export_fn = getattr(wb_pricing, "cc_switch_pricing", None)
        cls.export = cls.export_fn() if callable(cls.export_fn) else None
        models = cls.export.get("models") if isinstance(cls.export, dict) else None
        cls.models = models if isinstance(models, list) else []
        cls.by_id = {}
        for item in cls.models:
            if isinstance(item, dict):
                cls.by_id.setdefault(item.get("modelId"), item)

    def document(self):
        """The export, with a failure that names the missing function."""
        if not callable(self.export_fn):
            self.fail("wb_pricing.cc_switch_pricing() is missing: contract F "
                      "needs it to return cc-switch's model-pricing.json shape")
        if not isinstance(self.export, dict):
            self.fail("cc_switch_pricing() did not return a dict")
        return self.export

    def assertPriceClose(self, text, expected, where):
        """One price string, parsed, close to the rate the snapshot implies."""
        self.assertIsInstance(text, str, "%s is not a string: %r" % (where, text))
        self.assertTrue(PLAIN_DECIMAL.match(text),
                        "%s is not a plain decimal literal: %r" % (where, text))
        self.assertLessEqual(abs(Decimal(text) - Decimal(str(expected))),
                             TOLERANCE,
                             "%s: exported %s, snapshot implies %s"
                             % (where, text, expected))

    def test_export_function_exists(self):
        """Contract F names the function; its absence is the failure, not a skip."""
        self.assertTrue(callable(self.export_fn),
                        "wb_pricing.cc_switch_pricing() is missing")

    def test_top_level_keys_are_verbatim_from_the_file_struct(self):
        """Every top-level key is a `ModelPricingFile` field, spelled as serde expects."""
        doc = self.document()
        self.assertEqual(set(doc), set(TOP_LEVEL_KEYS),
                         "top level must be exactly %s, got %s"
                         % (list(TOP_LEVEL_KEYS), sorted(doc)))

    def test_version_is_the_current_file_version(self):
        """`version` is a JSON number: `normalize_file()` rejects anything above 1."""
        doc = self.document()
        version = doc.get("version")
        self.assertIsInstance(version, int)
        self.assertFalse(isinstance(version, bool))
        self.assertEqual(version, FILE_VERSION)
        self.assertEqual(doc.get("deletedModelIds"), [])

    def test_models_dev_sync_block_matches_the_rust_default(self):
        """The sync block must be what Rust falls back to, not a policy change.

        All six fields are camelCase `ModelsDevSyncConfig` fields. The two flags
        carry `#[serde(default = "default_true")]`, so an old file that omits
        them reads back as `true`; the export says the same thing explicitly
        instead of quietly disabling the user's own models.dev sync on import.
        """
        doc = self.document()
        block = doc.get("modelsDevSync")
        self.assertIsInstance(block, dict)
        self.assertEqual(set(block), set(SYNC_KEYS),
                         "modelsDevSync must be exactly %s, got %s"
                         % (list(SYNC_KEYS), sorted(block)))
        self.assertIs(block.get("autoSyncEnabled"), True)
        self.assertIs(block.get("includeCommonModels"), True)
        self.assertEqual(block.get("selectedModelKeys"), [])
        self.assertEqual(block.get("excludedCommonModelKeys"), [])
        self.assertIsNone(block.get("lastSyncAt"))
        self.assertIsNone(block.get("lastSyncError"))

    def test_entries_use_the_six_rust_field_names(self):
        """One wrong key spelling and serde drops the entry for missing fields."""
        doc = self.document()
        self.assertIsInstance(doc.get("models"), list)
        self.assertTrue(self.models, "the export carries no models")
        for item in self.models:
            self.assertIsInstance(item, dict)
            self.assertEqual(set(item), set(MODEL_KEYS),
                             "entry %r must have exactly %s, got %s"
                             % (item.get("modelId"), list(MODEL_KEYS),
                                sorted(item)))

    def test_prices_are_plain_decimal_strings(self):
        """A JSON number is not a String: `normalize_decimal()` would reject it."""
        doc = self.document()
        self.assertIsInstance(doc.get("models"), list)
        for item in self.models:
            for key in PRICE_KEYS:
                value = item.get(key)
                self.assertIsInstance(value, str,
                                      "%s.%s is %r, not a string"
                                      % (item.get("modelId"), key, value))
                self.assertTrue(PLAIN_DECIMAL.match(value),
                                "%s.%s is %r: a plain decimal is required, "
                                "no exponent and no sign"
                                % (item.get("modelId"), key, value))
                self.assertGreaterEqual(Decimal(value), Decimal("0"))

    def test_prices_are_per_million_usd_matching_the_snapshot(self):
        """Same unit as the snapshot's `flat`: per million tokens, US dollars."""
        doc = self.document()
        self.assertIsInstance(doc.get("models"), list)
        entries = snapshot_entries()
        policies = assigned_policies()
        for item in self.models:
            model = item.get("modelId")
            if model in policies:
                # A policy in force replaces the snapshot row (the same order
                # cost_for_row resolves prices in), so it is not this test's
                # business to restate that price here.
                continue
            self.assertIn(model, entries,
                          "exported model %r is neither in the snapshot nor "
                          "assigned by a policy" % (model,))
            rates = expected_rates(entries[model])
            for key in PRICE_KEYS:
                self.assertPriceClose(item.get(key), rates[key],
                                      "%s.%s" % (model, key))

    def test_cache_read_falls_back_to_the_miss_rate(self):
        """No cache-hit price in the snapshot must not become a free cache read."""
        doc = self.document()
        self.assertIsInstance(doc.get("models"), list)
        entries = snapshot_entries()
        absent = [mid for mid, entry in entries.items()
                  if (entry.get("flat") or {}).get("input_cache_hit") is None]
        self.assertTrue(absent, "the snapshot no longer has a null cache-hit "
                                "price; this test's premise needs revisiting")
        for mid in absent:
            item = self.by_id.get(mid)
            self.assertIsNotNone(item, "model %r is missing from the export" % mid)
            hit = Decimal(item["cacheReadCostPerMillion"])
            miss = Decimal(item["inputCostPerMillion"])
            self.assertEqual(hit, miss,
                             "%s has no cache-hit rate in the snapshot, so the "
                             "export must reuse the miss rate" % mid)
            self.assertGreater(hit, Decimal("0"))

    def test_cache_creation_is_zero_for_every_model(self):
        """Nothing upstream counts cache writes, so inventing a rate would bill air."""
        doc = self.document()
        self.assertIsInstance(doc.get("models"), list)
        for item in self.models:
            self.assertEqual(Decimal(item["cacheCreationCostPerMillion"]),
                             Decimal("0"),
                             "%s exported a cache-write rate"
                             % (item.get("modelId"),))

    def test_banded_models_export_the_flat_base_rate(self):
        """cc-switch holds one price set, so a banded model exports its base rate.

        The alternative - exporting the first band - would overstate ordinary
        requests; exporting `flat` only understates the long-context ones, which
        is the limitation `docs/cc-switch-pricing.md` records.
        """
        doc = self.document()
        self.assertIsInstance(doc.get("models"), list)
        entries = snapshot_entries()
        banded = [mid for mid, entry in entries.items() if entry.get("bands")]
        self.assertTrue(banded, "the snapshot no longer prices any model by band")
        for mid in banded:
            item = self.by_id.get(mid)
            self.assertIsNotNone(item, "banded model %r is missing" % (mid,))
            rates = expected_rates(entries[mid])
            for key in PRICE_KEYS:
                self.assertPriceClose(item.get(key), rates[key],
                                      "%s.%s (flat base)" % (mid, key))

    def test_model_ids_are_the_snapshot_ids_and_stay_unpadded(self):
        """The id must be byte-identical to the model string cc-switch sees.

        `find_model_pricing_row` looks the row up by that string, so a padded or
        re-spelled id silently misses and the request stays unpriced.
        """
        doc = self.document()
        self.assertIsInstance(doc.get("models"), list)
        entries = snapshot_entries()
        policies = assigned_policies()
        ids = [item.get("modelId") for item in self.models]
        self.assertEqual(len(ids), len(set(ids)), "duplicate modelId in the export")
        for mid in ids:
            self.assertIsInstance(mid, str)
            self.assertTrue(mid)
            self.assertEqual(mid, mid.strip(),
                             "%r has surrounding whitespace" % (mid,))
            self.assertNotIn("/", mid)
            self.assertIn(mid, entries if mid not in policies else policies)
        missing = sorted(set(entries) - set(ids))
        self.assertEqual(missing, [],
                         "the snapshot prices these models but the export "
                         "dropped them: %s" % (missing,))

    def test_every_entry_has_a_non_empty_display_name(self):
        """`normalize_pricing()` rejects the whole file on an empty displayName."""
        doc = self.document()
        self.assertIsInstance(doc.get("models"), list)
        for item in self.models:
            name = item.get("displayName")
            self.assertIsInstance(name, str)
            self.assertTrue(name.strip(), "%r has an empty displayName"
                            % (item.get("modelId"),))

    def test_no_entry_leaves_cc_switch_reporting_unpriced(self):
        """The point of the export: a normal request's total is not zero.

        Replays `CostCalculator::calculate` for app type `dsh`, which is not one
        of the cache-inclusive app types: input tokens are fresh input, cache
        reads are billed at the cache-read rate, and everything is divided by
        one million and multiplied by the row's cost multiplier (1 here).
        """
        doc = self.document()
        self.assertIsInstance(doc.get("models"), list)
        usage = (("inputCostPerMillion", Decimal("1000")),
                 ("outputCostPerMillion", Decimal("500")),
                 ("cacheReadCostPerMillion", Decimal("200")),
                 ("cacheCreationCostPerMillion", Decimal("0")))
        for item in self.models:
            total = Decimal("0")
            for key, tokens in usage:
                total += tokens * Decimal(item[key]) / MILLION
            self.assertGreater(total, Decimal("0"),
                               "%s costs 0 for a 1000/500 request: cc-switch "
                               "would still print 未定价" % (item.get("modelId"),))
        flash = self.by_id.get("deepseek-v4.1-flash")
        self.assertIsNotNone(flash, "deepseek-v4.1-flash is not exported")
        # 1000 * 0.03 / 1M + 500 * 0.5 / 1M + 200 * 0.01 / 1M
        # 这条断言故意绑 builtin 出厂价口径：价表一改它就红，那是要的信号而不是
        # 脆弱点 —— 改价时把上面这行算式和下面的 expected 一起更新即可。
        expected = (Decimal("0.00003") + Decimal("0.00025")
                    + Decimal("0.000002"))
        actual = (Decimal("1000") * Decimal(flash["inputCostPerMillion"])
                  + Decimal("500") * Decimal(flash["outputCostPerMillion"])
                  + Decimal("200") * Decimal(flash["cacheReadCostPerMillion"])) / MILLION
        self.assertEqual(actual, expected)

    def test_export_round_trips_through_json(self):
        """The bytes on disk are read by serde, so they must survive JSON."""
        doc = self.document()
        text = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True)
        self.assertNotIn("NaN", text.replace('"NaN"', ""))
        self.assertNotIn("Infinity", text.replace('"Infinity"', ""))
        self.assertEqual(json.loads(text), doc)
        self.assertEqual(json.loads(text)["models"], doc["models"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
