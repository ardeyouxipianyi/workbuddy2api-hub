"""Real output-limit probes (M5 E2).

Run with: python _test_output_probes.py
No upstream credentials or outbound network are used (the probe is stubbed).
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="output-probes-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_probes
import wb_proxy

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


class FakeResponse(object):
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class ProbeFileTests(unittest.TestCase):
    def setUp(self):
        wb_probes._cache.update({"at": 0.0, "dir": None, "data": {}})

    def test_roundtrip_and_clamp_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            wb_probes.save_probe(directory, "m1", {"clamped": 32000})
            wb_probes.save_probe(directory, "m2", {"clamped": None})
            self.assertEqual(wb_probes.clamp_for(directory, "m1"), 32000)
            self.assertIsNone(wb_probes.clamp_for(directory, "m2"))
            self.assertIsNone(wb_probes.clamp_for(directory, "missing"))
            with open(wb_probes.probe_path(directory), encoding="utf-8") as fh:
                stored = json.load(fh)
        self.assertEqual(stored["m1"]["clamped"], 32000)

    def test_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(wb_probes.load_probes(directory, force=True), {})


class ProbeRunTests(unittest.TestCase):
    def test_dry_run_sends_nothing(self):
        result = wb_probes.probe_model("http://127.0.0.1:8789", "k", "m1",
                                       dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["url"], "http://127.0.0.1:8789/v1/chat/completions")
        self.assertEqual(result["request"]["max_tokens"], 1000000)

    def test_length_finish_reason_is_the_clamp(self):
        def fake_urlopen(req, timeout=900):
            return FakeResponse({
                "choices": [{"message": {"content": "1"}, "finish_reason": "length"}],
                "usage": {"completion_tokens": 32000, "total_tokens": 32100},
            })

        result = wb_probes.probe_model("http://127.0.0.1:8789", "k", "m1",
                                       urlopen=fake_urlopen)
        self.assertEqual(result["clamped"], 32000)
        self.assertEqual(result["finish_reason"], "length")

    def test_natural_stop_has_no_clamp(self):
        def fake_urlopen(req, timeout=900):
            return FakeResponse({
                "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
                "usage": {"completion_tokens": 120, "total_tokens": 200},
            })

        result = wb_probes.probe_model("http://127.0.0.1:8789", "k", "m1",
                                       urlopen=fake_urlopen)
        self.assertIsNone(result["clamped"])
        self.assertEqual(result["observed_completion_tokens"], 120)

    def test_model_entry_annotates_the_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            wb_probes.save_probe(directory, "glm-5.2", {"clamped": 32000})
            with mock.patch.object(wb_proxy, "ACCOUNTS_DIR", directory):
                entry = wb_proxy.model_entry("glm-5.2", {})
        self.assertEqual(entry["output_clamp"], 32000)
        self.assertEqual(entry["max_output_tokens_clamped"], 32000)
        # The spec value from the knowledge table is untouched.
        self.assertEqual(entry["max_output_tokens"], 131072)


class ProbeScriptTests(unittest.TestCase):
    def load_script(self):
        spec = importlib.util.spec_from_file_location(
            "probe_script", os.path.join(ROOT, "scripts", "probe_max_tokens.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_dry_run_prints_and_writes_nothing(self):
        module = self.load_script()
        with tempfile.TemporaryDirectory() as directory:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = module.main(["--model", "m1", "--accounts-dir", directory,
                                    "--output-dir", directory, "--dry-run"])
            self.assertEqual(code, 0)
            self.assertIn("dry_run", buffer.getvalue())
            self.assertFalse(os.path.exists(wb_probes.probe_path(directory)))


if __name__ == "__main__":
    unittest.main()
