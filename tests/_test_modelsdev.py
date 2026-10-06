"""Model metadata lookup chain (M5 E1) and cache alias normalization (E3).

Run with: python _test_modelsdev.py
No upstream credentials or outbound network are used (models.dev is stubbed).
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="modelsdev-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_modelsdev
import wb_proxy


class FakeResponse(object):
    def __init__(self, payload):
        self.payload = payload

    def read(self, limit=None):
        return self.payload[:limit] if limit else self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def reset_module():
    with wb_modelsdev._lock:
        wb_modelsdev._index = None
        wb_modelsdev._index_loaded = False
        wb_modelsdev._last_fetch = 0.0
        wb_modelsdev._fetching = False


class KnowledgeTableTests(unittest.TestCase):
    def test_known_values(self):
        self.assertEqual(wb_modelsdev.CONTEXT_FALLBACK["glm-5.2"], (1000000, 131072))
        self.assertEqual(wb_modelsdev.CONTEXT_FALLBACK["auto"], (168000, 0))
        self.assertEqual(wb_modelsdev.DEFAULT_CONTEXT, 1000000)


class BuildIndexTests(unittest.TestCase):
    def test_vendor_beats_aggregator(self):
        doc = {
            "zai": {"models": {"glm-5.2": {"limit": {"context": 1000000, "output": 131072}}}},
            "some-gateway": {"models": {"glm-5.2": {"limit": {"context": 200000, "output": 65536}}}},
        }
        index = wb_modelsdev.build_index(doc)
        self.assertEqual(index["glm-5.2"], (1000000, 131072))

    def test_majority_wins_without_a_vendor(self):
        doc = {
            "gateway-a": {"models": {"m1": {"limit": {"context": 100, "output": 10}}}},
            "gateway-b": {"models": {"m1": {"limit": {"context": 100, "output": 10}}}},
            "gateway-c": {"models": {"m1": {"limit": {"context": 200, "output": 20}}}},
        }
        self.assertEqual(wb_modelsdev.build_index(doc)["m1"], (100, 10))

    def test_namespaced_ids_use_the_bare_tail(self):
        doc = {"openai": {"models": {"openai/gpt-5.5": {
            "limit": {"context": 1050000, "output": 128000}}}}}
        self.assertEqual(wb_modelsdev.build_index(doc)["gpt-5.5"], (1050000, 128000))

    def test_invalid_values_are_dropped(self):
        doc = {"p": {"models": {
            "bad": {"limit": {"context": 0, "output": 0}},
            "huge": {"limit": {"context": 10 ** 12, "output": 1}},
            "ok": {"limit": {"context": 1000, "output": 100}},
        }}}
        index = wb_modelsdev.build_index(doc)
        self.assertNotIn("bad", index)
        self.assertNotIn("huge", index)
        self.assertEqual(index["ok"], (1000, 100))


class LocalCacheTests(unittest.TestCase):
    def setUp(self):
        reset_module()

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            wb_modelsdev.save_index(directory, {"m1": (1000, 100)})
            index = wb_modelsdev.load_index(directory, force=True)
        self.assertEqual(index["m1"], (1000, 100))

    def test_corrupt_cache_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            with open(wb_modelsdev.cache_path(directory), "w", encoding="utf-8") as fh:
                fh.write("{not json")
            index = wb_modelsdev.load_index(directory, force=True)
        self.assertEqual(index, {})


class LookupTests(unittest.TestCase):
    def setUp(self):
        reset_module()

    def test_remote_wins(self):
        context, output, source = wb_modelsdev.lookup(
            "glm-5.2", 555, 55, directory=tempfile.gettempdir())
        self.assertEqual((context, output), (555, 55))
        self.assertEqual(source["context"], "remote")

    def test_table_beats_cache_and_default(self):
        context, output, source = wb_modelsdev.lookup(
            "glm-5.2", 0, 0, directory=tempfile.gettempdir(),
            index={"glm-5.2": (123, 12)})
        self.assertEqual((context, output), (1000000, 131072))
        self.assertEqual(source["context"], "table")

    def test_cache_used_when_table_misses(self):
        context, output, source = wb_modelsdev.lookup(
            "some-new-model", 0, 0, directory=tempfile.gettempdir(),
            index={"some-new-model": (777, 77)})
        self.assertEqual((context, output), (777, 77))
        self.assertEqual(source["context"], "cache")

    def test_unknown_model_defaults_to_1m_and_no_output(self):
        context, output, source = wb_modelsdev.lookup(
            "totally-unknown", 0, 0, directory=tempfile.gettempdir(), index={})
        self.assertEqual(context, 1000000)
        self.assertIsNone(output)
        self.assertEqual(source["output"], "unknown")

    def test_unknown_output_is_omitted_even_with_context(self):
        context, output, _source = wb_modelsdev.lookup(
            "auto", 0, 0, directory=tempfile.gettempdir(), index={})
        self.assertEqual(context, 168000)
        self.assertIsNone(output)


class RefreshTests(unittest.TestCase):
    def setUp(self):
        reset_module()

    def test_refresh_writes_the_cache(self):
        doc = {"zai": {"models": {"glm-5.2": {
            "limit": {"context": 1000000, "output": 131072}}}}}
        payload = json.dumps(doc).encode("utf-8")

        def fake_urlopen(req, timeout=5):
            return FakeResponse(payload)

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(wb_modelsdev.urllib.request, "urlopen",
                                   side_effect=fake_urlopen):
                started = wb_modelsdev.refresh_async(directory)
                deadline = time.time() + 5
                while time.time() < deadline:
                    with wb_modelsdev._lock:
                        if not wb_modelsdev._fetching:
                            break
                    time.sleep(0.02)
            self.assertTrue(started)
            index = wb_modelsdev.load_index(directory, force=True)
        self.assertEqual(index["glm-5.2"], (1000000, 131072))

    def test_offline_refresh_is_silent(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(wb_modelsdev.urllib.request, "urlopen",
                                   side_effect=OSError("offline")):
                wb_modelsdev.refresh_async(directory)
                deadline = time.time() + 5
                while time.time() < deadline:
                    with wb_modelsdev._lock:
                        if not wb_modelsdev._fetching:
                            break
                    time.sleep(0.02)
            index = wb_modelsdev.load_index(directory, force=True)
        self.assertEqual(index, {})


class CatalogEntryTests(unittest.TestCase):
    def setUp(self):
        reset_module()

    def test_model_entry_uses_the_table(self):
        entry = wb_proxy.model_entry("glm-5.2", {})
        self.assertEqual(entry["context_length"], 1000000)
        self.assertEqual(entry["max_output_tokens"], 131072)

    def test_unknown_model_gets_1m_context_and_no_output(self):
        entry = wb_proxy.model_entry("totally-unknown", {})
        self.assertEqual(entry["context_length"], 1000000)
        self.assertNotIn("max_output_tokens", entry)

    def test_remote_values_still_win(self):
        entry = wb_proxy.model_entry("glm-5.2", {"maxInputTokens": 12345,
                                                 "maxOutputTokens": 678})
        self.assertEqual(entry["context_length"], 12345)
        self.assertEqual(entry["max_output_tokens"], 678)

    def test_v1_models_triggers_the_background_refresh(self):
        class Handler(wb_proxy.Handler):
            def __init__(self):
                self.captured = None

            def _authorized(self):
                return True

            def _request_realm(self):
                return "intl"

            def _json(self, code, obj):
                self.captured = (code, obj)

        handler = Handler()
        with mock.patch.object(wb_proxy, "fetch_models",
                               return_value=[("glm-5.2", {})]), \
                mock.patch.object(wb_modelsdev, "refresh_async") as refresh:
            handler._get_v1_models()
        code, obj = handler.captured
        self.assertEqual(code, 200)
        refresh.assert_called_once()
        self.assertEqual(obj["data"][0]["context_length"], 1000000)


class CacheAliasTests(unittest.TestCase):
    def test_best_alias_order(self):
        usage = {"prompt_tokens_details": {"cached_tokens": 42},
                 "prompt_cache_hit_tokens": 0,
                 "cache_read_input_tokens": 0,
                 "cached_tokens": 0}
        self.assertEqual(wb_proxy._best_cached_tokens(usage), 42)

    def test_cache_read_input_tokens_is_considered(self):
        self.assertEqual(wb_proxy._best_cached_tokens(
            {"cache_read_input_tokens": 17, "cached_tokens": 0}), 17)

    def test_normalize_writes_every_alias(self):
        usage = {"prompt_tokens_details": {"cached_tokens": 42},
                 "cache_read_input_tokens": 0, "cached_tokens": 0}
        out = wb_proxy.normalize_usage_cache_aliases(usage)
        self.assertEqual(out["cache_read_input_tokens"], 42)
        self.assertEqual(out["cached_tokens"], 42)
        self.assertEqual(out["prompt_cache_hit_tokens"], 42)
        self.assertEqual(out["prompt_tokens_details"]["cached_tokens"], 42)
        self.assertNotIn("input_tokens_details", out)

    def test_normalize_keeps_existing_input_details(self):
        usage = {"prompt_cache_hit_tokens": 5,
                 "input_tokens_details": {"cached_tokens": 0}}
        out = wb_proxy.normalize_usage_cache_aliases(usage)
        self.assertEqual(out["input_tokens_details"]["cached_tokens"], 5)

    def test_extract_usage_uses_the_best_alias(self):
        fields = wb_proxy._extract_usage({
            "prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
            "prompt_tokens_details": {"cached_tokens": 60},
            "cache_read_input_tokens": 0})
        self.assertEqual(fields["cached_tokens"], 60)

    def test_cached_tokens_stay_integers(self):
        # Codex parses response.completed strictly: a float like 631168.0 is
        # rejected with "failed to parse ResponseCompleted: invalid number".
        usage = {"prompt_tokens_details": {"cached_tokens": 631168.0}}
        best = wb_proxy._best_cached_tokens(usage)
        self.assertIsInstance(best, int)
        self.assertEqual(best, 631168)
        out = wb_proxy.normalize_usage_cache_aliases(dict(usage))
        for value in (out["cached_tokens"], out["prompt_cache_hit_tokens"],
                      out["cache_read_input_tokens"],
                      out["prompt_tokens_details"]["cached_tokens"]):
            self.assertIsInstance(value, int)
        payload = wb_proxy._responses_usage({
            "prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
            "prompt_tokens_details": {"cached_tokens": 631168.0}})
        self.assertIsInstance(payload["input_tokens_details"]["cached_tokens"], int)
        self.assertNotIn(".0", json.dumps(payload))

    def test_clean_chunk_rewrites_zero_aliases(self):
        raw = json.dumps({"choices": [{"delta": {"content": "x"}}],
                          "usage": {"prompt_tokens": 10,
                                    "prompt_tokens_details": {"cached_tokens": 7},
                                    "cached_tokens": 0}})
        cleaned = json.loads(wb_proxy.clean_chunk(raw))
        self.assertEqual(cleaned["usage"]["cached_tokens"], 7)
        self.assertEqual(cleaned["usage"]["cache_read_input_tokens"], 7)

    def test_responses_usage_reports_the_best_alias(self):
        usage = wb_proxy._responses_usage({
            "prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
            "prompt_tokens_details": {"cached_tokens": 6}})
        self.assertEqual(usage["input_tokens_details"]["cached_tokens"], 6)


if __name__ == "__main__":
    unittest.main()
