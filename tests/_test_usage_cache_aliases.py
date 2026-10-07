"""Pin the cached-token alias contract.

Upstreams disagree on where the cache hit lives: prompt_tokens_details
.cached_tokens, prompt_cache_hit_tokens, cache_read_input_tokens,
input_tokens_details.cached_tokens ... and some responses carry the real value
in one while emitting 0 in the others. Strict clients pick one alias and then
report "no cache hits" forever. The gateway now reads the best positive value
across every alias and writes it back into all of them.

Run with: python tests/_test_usage_cache_aliases.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy as proxy


class BestCachedTokensTests(unittest.TestCase):
    def test_every_alias_is_read(self):
        cases = [
            {"prompt_tokens_details": {"cached_tokens": 12}},
            {"prompt_cache_hit_tokens": 12},
            {"cache_read_input_tokens": 12},
            {"cached_tokens": 12},
            {"input_tokens_details": {"cached_tokens": 12}},
            {"completion_tokens_details": {"cached_tokens": 12}},
        ]
        for usage in cases:
            self.assertEqual(proxy._best_cached_tokens(usage), 12, usage)

    def test_zero_aliases_do_not_shadow_a_real_hit(self):
        usage = {"prompt_tokens_details": {"cached_tokens": 0},
                 "cache_read_input_tokens": 5}
        self.assertEqual(proxy._best_cached_tokens(usage), 5)

    def test_missing_or_broken_usage_is_zero(self):
        self.assertEqual(proxy._best_cached_tokens(None), 0)
        self.assertEqual(proxy._best_cached_tokens({}), 0)
        self.assertEqual(proxy._best_cached_tokens({"cached_tokens": "x"}), 0)


class NormalizeAliasesTests(unittest.TestCase):
    def test_best_value_is_written_into_every_alias(self):
        usage = {"prompt_tokens_details": {"cached_tokens": 42},
                 "cache_read_input_tokens": 0, "cached_tokens": 0}
        proxy.normalize_usage_cache_aliases(usage)
        self.assertEqual(usage["cached_tokens"], 42)
        self.assertEqual(usage["cache_read_input_tokens"], 42)
        self.assertEqual(usage["prompt_cache_hit_tokens"], 42)
        self.assertEqual(usage["prompt_tokens_details"]["cached_tokens"], 42)

    def test_input_details_are_updated_only_when_present(self):
        usage = {"cached_tokens": 7}
        proxy.normalize_usage_cache_aliases(usage)
        self.assertNotIn("input_tokens_details", usage)
        usage2 = {"cached_tokens": 7, "input_tokens_details": {"cached_tokens": 0}}
        proxy.normalize_usage_cache_aliases(usage2)
        self.assertEqual(usage2["input_tokens_details"]["cached_tokens"], 7)

    def test_no_hit_leaves_the_payload_untouched(self):
        usage = {"cached_tokens": 0, "prompt_tokens": 10}
        before = dict(usage)
        proxy.normalize_usage_cache_aliases(usage)
        self.assertEqual(usage, before)

    def test_extract_usage_uses_the_best_alias(self):
        usage = {"prompt_tokens": 100, "completion_tokens": 5,
                 "total_tokens": 105, "cached_tokens": 0,
                 "prompt_tokens_details": {"cached_tokens": 80}}
        fields = proxy._extract_usage(usage)
        self.assertEqual(fields["cached_tokens"], 80)
        self.assertEqual(fields["total_tokens"], 105)


if __name__ == "__main__":
    unittest.main(verbosity=2)
