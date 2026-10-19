"""API-equivalent cost on the responses the proxy hands back.

The dashboard has priced every logged row for a while, but a client reading a
response had no way to learn what that request cost. This suite pins the
additive contract that closes the gap:

  A/B  `/v1/chat/completions` usage (streaming and not) gains `cost`,
       `cost_cny`, `cost_details`, `pricing_known`; the four USD components
       still sum to `total` and `total` still times the exchange rate is
       `total_cny`.
  C    the same four fields on `/v1/messages`' `message_start.message.usage`,
       `message_delta.usage` and on `/v1/responses` usage.
  D    `api_cost_response` is on by default and is the only gate: off appends
       nothing at all, so a client that rejects unknown keys sees the exact
       bytes it saw before the feature existed.
  E    `/v1/models` carries a per-token `pricing` block whose numbers are
       fixed-point strings - `0.00000004186`, never `4.186e-08`.
  F    the cc-switch export speaks that app's own field names and units.
  G    the same amounts ride out as `X-Api-Cost-*` headers on non-streaming
       replies; streaming replies only announce `X-Api-Cost-In-Stream`.

Unpriced is never free. An unknown model reports null components and
`pricing_known: false`, because a zero is a number somebody can bill against.

Run with: python tests/_test_api_cost_response.py
No network, no credentials. Every directory is a `tempfile` one and removed in
`tearDown`, so the checkout stays clean.
"""
import copy
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_pricing
import wb_proxy
import wb_settings

#: Contract B/C: exactly these four keys are appended, in both API dialects.
COST_KEYS = ("cost", "cost_cny", "cost_details", "pricing_known")

#: Contract A: the keys `cost_payload` always returns, priced or not.
PAYLOAD_KEYS = ("known", "currency", "unit", "input", "output", "cache_read",
                "cache_write", "total", "total_cny", "usd_cny",
                "pricing_model", "pricing_source", "band")

#: Contract B: `cost_details` is the payload minus the envelope keys.
DETAIL_KEYS = ("input", "output", "cache_read", "cache_write", "total",
               "total_cny", "usd_cny", "pricing_model", "pricing_source", "band")

#: A token count big enough that 6-decimal USD rounding still leaves the
#: components distinguishable from each other and from zero.
PROMPT_TOKENS = 1000000
COMPLETION_TOKENS = 400000
CACHED_TOKENS = 250000


def _plain_model(doc):
    """A model with one flat USD rate and no token bands.

    Derived from the shipped snapshot rather than hardcoded, so a price-table
    refresh does not turn this suite red for the wrong reason; the arithmetic
    is still checked against the entry the snapshot carries.
    """
    for mid in sorted(doc.get("models") or {}):
        entry = doc["models"][mid]
        if not isinstance(entry, dict):
            continue
        if (entry.get("currency") or "USD").upper() != "USD":
            continue
        flat = entry.get("flat") or {}
        if entry.get("bands") or not (flat.get("input_cache_miss")
                                      and flat.get("input_cache_hit")
                                      and flat.get("output")):
            continue
        return mid
    raise AssertionError("no plain USD model in the bundled snapshot")


class _CostTest(unittest.TestCase):
    """A temporary accounts/de data pair plus the switch caches reset."""

    def setUp(self):
        self.accounts = tempfile.mkdtemp(prefix="wb-api-cost-")
        self.data = tempfile.mkdtemp(prefix="wb-api-cost-data-")
        self._prev_settings = wb_pricing._settings_dir_override
        self._prev_data = wb_pricing.data_dir()
        self._prev_accounts = wb_proxy.ACCOUNTS_DIR
        wb_pricing.set_settings_dir(self.accounts)
        wb_pricing.set_data_dir(self.data)
        wb_proxy.ACCOUNTS_DIR = self.accounts
        self.doc = wb_pricing.load_pricing()
        self.model = _plain_model(self.doc)
        self.entry = self.doc["models"][self.model]
        self.flat = self.entry["flat"]
        self.unit = float(self.entry.get("unit") or 1000000)
        self.rate = wb_pricing.usd_cny()

    def tearDown(self):
        wb_pricing.set_settings_dir(self._prev_settings)
        wb_pricing.set_data_dir(self._prev_data)
        wb_proxy.ACCOUNTS_DIR = self._prev_accounts
        for path in (self.accounts, self.data):
            shutil.rmtree(path, ignore_errors=True)

    # -- helpers ---------------------------------------------------------

    def settings(self, **values):
        """Write settings.json and drop the (path, mtime, size) memos.

        Two writes inside one filesystem timestamp tick would otherwise leave a
        cached read warm - the suite invalidates instead of sleeping.
        """
        path = os.path.join(self.accounts, "settings.json")
        data = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        data.update(values)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        for cache in (wb_pricing._api_cost_cache, wb_pricing._enabled_cache):
            cache.update({"key": None, "value": True})

    def usage(self, prompt=PROMPT_TOKENS, completion=COMPLETION_TOKENS,
              cached=CACHED_TOKENS):
        """An OpenAI-shaped usage block, cached count included."""
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "prompt_tokens_details": {"cached_tokens": cached},
        }

    def expected_usd(self, prompt=PROMPT_TOKENS, completion=COMPLETION_TOKENS,
                     cached=CACHED_TOKENS):
        """Recompute the components from the rate card, independently.

        Contract A: uncached*miss + cached*hit + completion*output, per million
        tokens, each component rounded to 6 decimals.
        """
        cached = max(0, min(int(cached), int(prompt)))
        uncached = int(prompt) - cached
        return {
            "input": round(uncached * float(self.flat["input_cache_miss"])
                           / self.unit, 6),
            "output": round(int(completion) * float(self.flat["output"])
                            / self.unit, 6),
            "cache_read": round(cached * float(self.flat["input_cache_hit"])
                                / self.unit, 6),
            "cache_write": 0.0,
        }


class CostPayloadTests(_CostTest):
    """Contract A: the payload the response side is allowed to publish."""

    def test_the_components_sum_to_the_total(self):
        payload = wb_pricing.cost_payload(self.model, self.usage())
        self.assertTrue(payload["known"], payload)
        self.assertEqual(payload["pricing_model"],
                         self.entry.get("or_id") or payload["pricing_model"])
        self.assertTrue(payload["pricing_model"])
        total = (payload["input"] + payload["output"]
                 + payload["cache_read"] + payload["cache_write"])
        self.assertAlmostEqual(total, payload["total"], delta=1e-6)
        self.assertAlmostEqual(payload["total"] * self.rate,
                               payload["total_cny"], delta=1e-6)

    def test_the_components_match_the_rate_card(self):
        # A formula that swapped hit for miss, or dropped the /1e6, would still
        # sum to its own total - so compare against the card.
        payload = wb_pricing.cost_payload(self.model, self.usage())
        expected = self.expected_usd()
        for key, value in expected.items():
            self.assertAlmostEqual(payload[key], value, delta=1e-6,
                                   msg="%s: %r != %r" % (key, payload[key], value))
        self.assertEqual(payload["currency"], "USD")
        self.assertEqual(payload["unit"], "USD")
        self.assertIsNone(payload["band"])

    def test_every_cached_token_spelling_prices_the_same(self):
        # The upstreams disagree about the key; the price must not.
        shapes = (
            {"prompt_tokens_details": {"cached_tokens": CACHED_TOKENS}},
            {"cache_read_input_tokens": CACHED_TOKENS},
            {"cached_tokens": CACHED_TOKENS},
            {"prompt_cache_hit_tokens": CACHED_TOKENS},
            # Responses API nests it under input_tokens_details.
            {"input_tokens_details": {"cached_tokens": CACHED_TOKENS}},
        )
        reference = wb_pricing.cost_payload(self.model, self.usage())
        for extra in shapes:
            usage = self.usage(cached=0)
            usage.pop("prompt_tokens_details")
            usage.update(extra)
            payload = wb_pricing.cost_payload(self.model, usage)
            self.assertEqual(payload["cache_read"], reference["cache_read"],
                             "cached_tokens under %s was not counted"
                             % sorted(extra))
            self.assertEqual(payload["total"], reference["total"])

    def test_a_cached_count_larger_than_the_prompt_is_capped(self):
        # Upstreams emit cache-read counts that can exceed the prompt on a
        # resumed session; uncached input must not go negative.
        payload = wb_pricing.cost_payload(
            self.model, self.usage(prompt=1000, cached=5000))
        self.assertGreaterEqual(payload["input"], 0)
        self.assertAlmostEqual(payload["input"], 0.0, delta=1e-9)
        self.assertAlmostEqual(
            payload["cache_read"],
            round(1000 * float(self.flat["input_cache_hit"]) / self.unit, 6),
            delta=1e-6)

    def test_an_unpriced_model_reports_nulls_never_zero(self):
        payload = wb_pricing.cost_payload("no-such-model-in-the-card", self.usage())
        self.assertFalse(payload["known"])
        for key in ("input", "output", "cache_read", "cache_write", "total",
                    "total_cny", "pricing_model"):
            self.assertIsNone(payload[key], "%s must be null, not 0" % key)
        self.assertNotIn("disabled", payload,
                         "the master switch is on, so nothing is disabled")
        # `cost_for_row` still reports `source: "builtin"` on this path, which
        # would read as "matched the shipped card" - the one thing that did not
        # happen. Nothing priced it, so nothing can be named as its source.
        self.assertIsNone(payload["pricing_source"])
        self.assertIsNone(wb_pricing.cost_details(payload),
                          "an unpriced request has no details block at all")

    def test_the_master_switch_reports_disabled_and_unpriced(self):
        self.settings(pricing_enabled=False)
        self.assertIs(wb_pricing.pricing_enabled(), False)
        payload = wb_pricing.cost_payload(self.model, self.usage())
        self.assertIs(payload["disabled"], True)
        self.assertFalse(payload["known"])
        self.assertIsNone(payload["total"])
        self.assertIsNone(payload["input"])
        self.assertIsNone(payload["pricing_source"],
                          "a switch that is off priced nothing")

    def test_the_payload_always_carries_the_same_keys(self):
        priced = wb_pricing.cost_payload(self.model, self.usage())
        unpriced = wb_pricing.cost_payload("no-such-model-in-the-card", self.usage())
        self.assertEqual(set(priced), set(PAYLOAD_KEYS))
        self.assertEqual(set(unpriced), set(PAYLOAD_KEYS))

    def test_the_requested_alias_only_fills_in_for_an_unknown_model(self):
        # What the upstream executed wins; the client's spelling is a fallback
        # for aliases the price card does not know under their billed name.
        fallback = wb_pricing.cost_payload(
            "alias-not-in-the-card", self.usage(), requested_model=self.model)
        self.assertTrue(fallback["known"])
        self.assertEqual(fallback["pricing_model"],
                         self.entry.get("or_id") or fallback["pricing_model"])
        # A known billed model is never re-priced through the alias.
        direct = wb_pricing.cost_payload(
            self.model, self.usage(), requested_model="alias-not-in-the-card")
        self.assertTrue(direct["known"])
        self.assertEqual(direct["total"],
                         wb_pricing.cost_payload(self.model, self.usage())["total"])


class ResponseAttachTests(_CostTest):
    """Contracts B and D: what actually lands on the client-facing usage."""

    def test_off_appends_nothing_at_all(self):
        self.settings(api_cost_response=False)
        self.assertIs(wb_pricing.api_cost_response(), False)
        usage = self.usage()
        before = copy.deepcopy(usage)
        result = wb_proxy.attach_api_cost(usage, self.model, requested_model=self.model)
        self.assertIs(result, usage)
        self.assertEqual(usage, before, "the switch is off: bytes must be unchanged")
        for key in COST_KEYS:
            self.assertNotIn(key, usage)

    def test_on_appends_the_four_keys_and_leaves_the_rest_alone(self):
        self.settings(api_cost_response=True)
        usage = self.usage()
        before = copy.deepcopy(usage)
        wb_proxy.attach_api_cost(usage, self.model, requested_model=self.model)
        for key in before:
            self.assertEqual(usage[key], before[key],
                             "existing usage field %s was modified" % key)
        self.assertEqual(set(usage) - set(before), set(COST_KEYS))
        self.assertIs(usage["pricing_known"], True)
        self.assertIsInstance(usage["cost"], float)
        self.assertIsInstance(usage["cost_cny"], float)
        self.assertEqual(set(usage["cost_details"]), set(DETAIL_KEYS))
        self.assertAlmostEqual(usage["cost_details"]["total"], usage["cost"],
                               delta=1e-9)
        self.assertAlmostEqual(usage["cost_details"]["total_cny"],
                               usage["cost_cny"], delta=1e-9)

    def test_an_unpriced_request_carries_null_fields(self):
        self.settings(api_cost_response=True)
        usage = self.usage()
        wb_proxy.attach_api_cost(usage, "no-such-model-in-the-card")
        self.assertIsNone(usage["cost"])
        self.assertIsNone(usage["cost_cny"])
        self.assertIsNone(usage["cost_details"])
        self.assertIs(usage["pricing_known"], False)

    def test_message_start_zero_usage_prices_to_zero_and_is_still_known(self):
        # Contract C puts the group on message_start.message.usage as well as
        # message_delta.usage. The proxy seeds that first frame with
        # {"input_tokens": 0, "output_tokens": 0}, so the group there says
        # cost 0.0 / pricing_known true: this frame really does carry usage,
        # the usage really is empty, and the model really is priced. C only
        # forbids editing `input_tokens` - the four keys are an append - so
        # zero here is a statement about the frame, not a claim the request
        # was free. Kept deliberately; the totals ride on message_delta.
        seed = wb_proxy.attach_api_cost(
            {"input_tokens": 0, "output_tokens": 0}, self.model,
            requested_model=self.model)
        self.assertEqual(seed["input_tokens"], 0)
        self.assertIs(seed["pricing_known"], True)
        self.assertEqual(seed["cost"], 0.0)
        self.assertEqual(seed["cost_details"]["total"], 0.0)
        # An unpriced model seeds the same frame with nulls, not a free zero.
        unpriced = wb_proxy.attach_api_cost(
            {"input_tokens": 0, "output_tokens": 0}, "no-such-model-in-the-card")
        self.assertIsNone(unpriced["cost"])
        self.assertIs(unpriced["pricing_known"], False)

    def test_a_responses_usage_block_is_priced_like_a_chat_one(self):
        # /v1/responses has its own usage spelling; the price must not depend
        # on which dialect the client speaks.
        shaped = {
            "input_tokens": PROMPT_TOKENS,
            "input_tokens_details": {"cached_tokens": CACHED_TOKENS},
            "output_tokens": COMPLETION_TOKENS,
            "output_tokens_details": {"reasoning_tokens": 1000},
            "total_tokens": PROMPT_TOKENS + COMPLETION_TOKENS,
        }
        before = copy.deepcopy(shaped)
        wb_proxy.attach_api_cost(shaped, self.model, requested_model=self.model)
        for key in before:
            self.assertEqual(shaped[key], before[key],
                             "existing usage field %s was modified" % key)
        self.assertEqual(set(shaped) - set(before), set(COST_KEYS))
        expected = self.expected_usd()
        for key in ("input", "output", "cache_read"):
            self.assertAlmostEqual(shaped["cost_details"][key], expected[key],
                                   delta=1e-6)
        # The converter that builds that block keeps the cached count visible,
        # which is what makes the cached component non-zero above.
        converted = wb_proxy._responses_usage({
            "prompt_tokens": PROMPT_TOKENS,
            "completion_tokens": COMPLETION_TOKENS,
            "total_tokens": PROMPT_TOKENS + COMPLETION_TOKENS,
            "prompt_tokens_details": {"cached_tokens": CACHED_TOKENS},
        })
        self.assertEqual(converted["input_tokens_details"]["cached_tokens"],
                         CACHED_TOKENS)

    def test_the_default_is_on_for_an_install_without_the_key(self):
        # settings.json does not exist yet: an upgrade must not need the key.
        self.assertIs(wb_settings.api_cost_response(self.accounts), True)
        self.assertIs(wb_pricing.api_cost_response(), True)
        usage = self.usage()
        wb_proxy.attach_api_cost(usage, self.model)
        self.assertIn("cost", usage)

    def test_the_setting_round_trips_through_wb_settings(self):
        self.assertIs(wb_settings.set_api_cost_response(self.accounts, False), False)
        for cache in (wb_pricing._api_cost_cache, wb_pricing._enabled_cache):
            cache.update({"key": None, "value": True})
        self.assertIs(wb_pricing.api_cost_response(), False)
        with open(os.path.join(self.accounts, "settings.json"), encoding="utf-8") as fh:
            self.assertIs(json.load(fh)["api_cost_response"], False)
        self.assertIs(wb_settings.set_api_cost_response(self.accounts, True), True)

    def test_attaching_twice_does_not_drift(self):
        # The streaming path and the /v1/messages path both converge on one
        # usage dict; a second pass must be idempotent, not additive.
        self.settings(api_cost_response=True)
        usage = self.usage()
        wb_proxy.attach_api_cost(usage, self.model, requested_model=self.model)
        once = copy.deepcopy(usage)
        wb_proxy.attach_api_cost(usage, self.model, requested_model=self.model)
        self.assertEqual(usage, once)

    def test_a_pricing_failure_degrades_to_unknown_not_a_500(self):
        # `cost_details` raising must not take the response down with it.
        original = wb_pricing.cost_payload

        def boom(*args, **kwargs):
            raise RuntimeError("price table exploded")

        wb_pricing.cost_payload = boom
        try:
            usage = self.usage()
            wb_proxy.attach_api_cost(usage, self.model)
        finally:
            wb_pricing.cost_payload = original
        self.assertIsNone(usage["cost"])
        self.assertIs(usage["pricing_known"], False)
        self.assertIn("prompt_tokens", usage)


class _FakeHandler(object):
    """Just enough of the request handler for `_json` to write a reply."""

    _json_etag = None

    def __init__(self):
        self.code = None
        self.headers_sent = []
        self.path = "/v1/chat/completions"
        self.close_connection = False
        self.wfile = io.BytesIO()

    def send_response(self, code):
        self.code = code

    def send_header(self, name, value):
        self.headers_sent.append((name, value))

    def end_headers(self):
        pass


class _ExplodingHandler(_FakeHandler):
    """`send_header` fails for one name, the way a socket error would."""

    def send_header(self, name, value):
        if name == "X-Api-Cost-Cny":
            raise OSError("wire went away")
        _FakeHandler.send_header(self, name, value)


def _send_json(headers, handler_cls=_FakeHandler):
    handler = handler_cls()
    wb_proxy.Handler._json(handler, 200, {"ok": True}, headers)
    return handler


class ResponseHeaderTests(_CostTest):
    """Contract G: the same numbers on the wire, for clients that read headers.

    cc-switch does not use these (it prices from its own table), but a gateway,
    an nginx access log or a `curl -i` show the request's cost without anyone
    re-reading the body. Streaming replies cannot know the amount at header
    time, so they only announce where it will arrive.
    """

    def test_a_priced_response_mirrors_the_body_numbers(self):
        usage = wb_proxy.attach_api_cost(self.usage(), self.model)
        headers = dict(wb_proxy.api_cost_headers(usage))
        self.assertEqual(headers["X-Api-Cost-Usd"], "%.6f" % usage["cost"])
        self.assertEqual(headers["X-Api-Cost-Cny"], "%.6f" % usage["cost_cny"])
        self.assertEqual(headers["X-Api-Cost-Known"], "true")
        self.assertEqual(headers["X-Api-Pricing-Source"],
                         usage["cost_details"]["pricing_source"])
        self.assertEqual(headers["X-Api-Cost-Model"],
                         usage["cost_details"]["pricing_model"])

    def test_the_header_amounts_are_decimal_strings_not_floats(self):
        # `1e-05` in a header is a value half the log parsers read as zero.
        usage = wb_proxy.attach_api_cost(
            self.usage(prompt=1, completion=1, cached=0), self.model)
        headers = dict(wb_proxy.api_cost_headers(usage))
        for name in ("X-Api-Cost-Usd", "X-Api-Cost-Cny"):
            self.assertIsInstance(headers[name], str)
            self.assertNotIn("e", headers[name].lower(), headers[name])

    def test_every_header_is_ascii_so_http_can_encode_it(self):
        usage = wb_proxy.attach_api_cost(self.usage(), self.model)
        for name, value in wb_proxy.api_cost_headers(usage):
            name.encode("latin-1")
            value.encode("latin-1")

    def test_an_unpriced_response_sends_no_amount_only_known_false(self):
        self.settings(api_cost_response=True)
        usage = wb_proxy.attach_api_cost(
            self.usage(), "no-such-model-in-the-card")
        self.assertEqual(wb_proxy.api_cost_headers(usage),
                         [("X-Api-Cost-Known", "false")],
                         "an unpriced request must not report 0.000000")

    def test_the_switch_off_sends_no_headers_at_all(self):
        self.settings(api_cost_response=False)
        usage = wb_proxy.attach_api_cost(self.usage(), self.model)
        self.assertEqual(wb_proxy.api_cost_headers(usage), [])

    def test_a_stream_announces_where_the_cost_arrives(self):
        self.settings(api_cost_response=True)
        self.assertEqual(wb_proxy.api_cost_stream_headers(),
                         [("X-Api-Cost-In-Stream", "usage")])

    def test_the_switch_off_sends_no_stream_header_either(self):
        self.settings(api_cost_response=False)
        self.assertEqual(wb_proxy.api_cost_stream_headers(), [])

    def test_the_body_writer_emits_the_headers_it_is_handed(self):
        usage = wb_proxy.attach_api_cost(self.usage(), self.model)
        handler = _send_json(wb_proxy.api_cost_headers(usage))
        self.assertIn(("X-Api-Cost-Known", "true"), handler.headers_sent)
        self.assertIn(("X-Api-Cost-Usd", "%.6f" % usage["cost"]),
                      handler.headers_sent)
        self.assertIn(b'"ok"', handler.wfile.getvalue())

    def test_a_plain_json_reply_gains_no_cost_headers(self):
        # Everything else on the panel shares `_json`; only cost replies grow.
        handler = _send_json(None)
        self.assertEqual(
            [name for name, _ in handler.headers_sent
             if name.lower().startswith("x-api-cost")], [])

    def test_one_unwritable_header_does_not_take_the_reply_down(self):
        usage = wb_proxy.attach_api_cost(self.usage(), self.model)
        handler = _send_json(wb_proxy.api_cost_headers(usage),
                            handler_cls=_ExplodingHandler)
        self.assertIn(b'"ok"', handler.wfile.getvalue())
        self.assertIn(("X-Api-Cost-Usd", "%.6f" % usage["cost"]),
                      handler.headers_sent)
        self.assertIn(("X-Api-Cost-Known", "true"), handler.headers_sent)
        self.assertNotIn("X-Api-Cost-Cny",
                         [name for name, _ in handler.headers_sent])


class ModelsPricingTests(_CostTest):
    """Contract E: the per-token block on /v1/models entries."""

    def test_a_priced_entry_carries_fixed_point_per_token_strings(self):
        self.settings(api_cost_response=True)
        item = wb_proxy.model_entry(self.model, {})
        block = item.get("pricing")
        self.assertIsInstance(block, dict, "/v1/models lost its pricing block")
        self.assertEqual(
            set(block),
            {"prompt", "completion", "input_cache_read", "input_cache_write",
             "currency", "unit", "pricing_model", "pricing_source"})
        for key in ("prompt", "completion", "input_cache_read", "input_cache_write"):
            self.assertIsInstance(block[key], str, "%s must be a string" % key)
            self.assertNotIn("e", block[key].lower(), "float notation in " + key)
            self.assertNotIn("+", block[key])
        # per_token = per-million / 1e6, exactly, as a decimal string.
        self.assertAlmostEqual(float(block["prompt"]),
                               float(self.flat["input_cache_miss"]) / 1e6,
                               delta=1e-15)
        self.assertAlmostEqual(float(block["completion"]),
                               float(self.flat["output"]) / 1e6, delta=1e-15)
        self.assertAlmostEqual(float(block["input_cache_read"]),
                               float(self.flat["input_cache_hit"]) / 1e6,
                               delta=1e-15)
        self.assertEqual(block["input_cache_write"], "0")
        self.assertEqual(block["currency"], "USD")
        self.assertEqual(block["unit"], "per_token")
        self.assertTrue(block["pricing_model"])
        self.assertTrue(block["pricing_source"])

    def test_small_rates_do_not_turn_into_float_notation(self):
        # 4.186e-08 is the shape this replaces: clients that parse a price with
        # a fixed decimal type reject it.
        pricing = wb_pricing.per_token_pricing(self.model)
        for key in ("prompt", "completion", "input_cache_read", "input_cache_write"):
            self.assertRegex(pricing[key], r"^\d+(\.\d+)?$",
                             "%s is not a plain decimal: %r" % (key, pricing[key]))

    def test_an_unpriced_entry_has_no_block(self):
        self.settings(api_cost_response=True)
        item = wb_proxy.model_entry("no-such-model-in-the-card", {})
        self.assertNotIn("pricing", item)
        self.assertIsNone(wb_pricing.per_token_pricing("no-such-model-in-the-card"))

    def test_the_block_disappears_when_the_switch_is_off(self):
        self.settings(api_cost_response=False)
        item = wb_proxy.model_entry(self.model, {})
        self.assertNotIn("pricing", item)


class CcSwitchExportTests(_CostTest):
    """Contract F: the export, against cc-switch's own wire format.

    The field names below are copied verbatim from
    `ccs/cc-switch-pr7356/src-tauri/src/services/model_pricing.rs`
    (`ModelPricingFile` / `ModelPricingInfo` / `ModelsDevSyncConfig`, all
    `#[serde(rename_all = "camelCase")]`). Prices are Rust `String`s parsed with
    `Decimal::from_str`, in USD per million tokens - a JSON number would fail to
    deserialise, so they are pinned as strings here too.
    """

    def test_the_export_matches_cc_switchs_own_wire_format(self):
        doc = wb_pricing.cc_switch_pricing()
        self.assertEqual(set(doc), {"version", "modelsDevSync", "models",
                                    "deletedModelIds"})
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["deletedModelIds"], [])
        self.assertIsInstance(doc["models"], list)
        self.assertTrue(doc["models"])
        for entry in doc["models"]:
            self.assertEqual(
                set(entry),
                {"modelId", "displayName", "inputCostPerMillion",
                 "outputCostPerMillion", "cacheReadCostPerMillion",
                 "cacheCreationCostPerMillion"})
            self.assertTrue(entry["modelId"])
            self.assertTrue(entry["displayName"])
            for key in ("inputCostPerMillion", "outputCostPerMillion",
                        "cacheReadCostPerMillion", "cacheCreationCostPerMillion"):
                self.assertIsInstance(entry[key], str, key + " must be a string")
                self.assertRegex(entry[key], r"^\d+(\.\d+)?$",
                                 "%s is not a Decimal literal: %r"
                                 % (key, entry[key]))

    def test_the_prices_are_the_rate_card_in_usd_per_million(self):
        doc = wb_pricing.cc_switch_pricing()
        entries = {e["modelId"]: e for e in doc["models"]}
        self.assertIn(self.model, entries)
        entry = entries[self.model]
        self.assertEqual(entry["displayName"], self.model)
        self.assertAlmostEqual(float(entry["inputCostPerMillion"]),
                               float(self.flat["input_cache_miss"]), delta=1e-9)
        self.assertAlmostEqual(float(entry["outputCostPerMillion"]),
                               float(self.flat["output"]), delta=1e-9)
        self.assertAlmostEqual(float(entry["cacheReadCostPerMillion"]),
                               float(self.flat["input_cache_hit"]), delta=1e-9)
        # The upstream reports no cache-write tokens, so the price is zero and
        # saying otherwise would invent a charge.
        self.assertEqual(entry["cacheCreationCostPerMillion"], "0")

    def test_models_dev_sync_keeps_the_apps_defaults(self):
        # Importing our table must not silently switch the app's own models.dev
        # sync off: `true/true` is ModelsDevSyncConfig::default(), i.e. "what
        # the user had before".
        sync = wb_pricing.cc_switch_pricing()["modelsDevSync"]
        self.assertEqual(set(sync), {"autoSyncEnabled", "includeCommonModels",
                                     "selectedModelKeys", "excludedCommonModelKeys",
                                     "lastSyncAt", "lastSyncError"})
        self.assertIs(sync["autoSyncEnabled"], True)
        self.assertIs(sync["includeCommonModels"], True)
        self.assertEqual(sync["selectedModelKeys"], [])
        self.assertEqual(sync["excludedCommonModelKeys"], [])
        self.assertIsNone(sync["lastSyncAt"])
        self.assertIsNone(sync["lastSyncError"])

    def test_every_exported_id_is_a_model_the_card_can_price(self):
        doc = wb_pricing.cc_switch_pricing()
        for entry in doc["models"]:
            self.assertIsNotNone(wb_pricing.per_token_pricing(entry["modelId"]),
                                 "%s exported but not priceable" % entry["modelId"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
