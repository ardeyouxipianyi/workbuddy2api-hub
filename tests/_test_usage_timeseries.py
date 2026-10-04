"""Token time-series and credit history plumbing (M4 D5).

Run with: python _test_usage_timeseries.py
No upstream credentials or outbound network are used.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="usage-series-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy


def row(at, total_tokens=100, credit=0.0, error=False, outcome="completed",
        realm="intl"):
    return {"at": at, "model": "m1", "realm": realm, "total_tokens": total_tokens,
            "prompt_tokens": 60, "completion_tokens": 30, "reasoning_tokens": 10,
            "cached_tokens": 5, "credit": credit, "error": error,
            "outcome": outcome}


class TimeseriesTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.dir.name, "usage.jsonl")
        self.patch = mock.patch.object(wb_proxy, "USAGE_LOG", self.log)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.dir.cleanup)

    def write(self, rows):
        with open(self.log, "w", encoding="utf-8") as fh:
            for item in rows:
                fh.write(json.dumps(item) + "\n")

    def test_bucket_size_auto_scales(self):
        now = time.time()
        one_hour = wb_proxy.usage_timeseries(range="custom", since=now - 3600, until=now)
        self.assertEqual(one_hour["bucket_seconds"], 60)
        three_days = wb_proxy.usage_timeseries(range="custom", since=now - 3 * 86400, until=now)
        self.assertEqual(three_days["bucket_seconds"], 3600)
        thirty_days = wb_proxy.usage_timeseries(range="custom", since=now - 30 * 86400, until=now)
        self.assertEqual(thirty_days["bucket_seconds"], 86400)
        explicit = wb_proxy.usage_timeseries(range="custom", since=now - 3600, until=now,
                                             bucket_seconds=120)
        self.assertEqual(explicit["bucket_seconds"], 120)

    def test_aggregation_counts_tokens_and_credit(self):
        now = time.time()
        self.write([
            row(now - 150, total_tokens=100, credit=2.0),
            row(now - 120, total_tokens=0, credit=1.0, error=True,
                outcome="failed"),
            row(now - 90, total_tokens=0, credit=99.0, outcome="client_aborted"),
            row(now - 60, total_tokens=50, credit=0.5),
        ])
        result = wb_proxy.usage_timeseries(range="custom", since=now - 180, until=now,
                                           bucket_seconds=60)
        totals = {
            "requests": sum(b["requests"] for b in result["series"]),
            "errors": sum(b["errors"] for b in result["series"]),
            "tokens": sum(b["total_tokens"] for b in result["series"]),
            "credit": sum(b["credit"] for b in result["series"]),
        }
        self.assertEqual(totals["requests"], 2)
        # usage_snapshot counts every non-completed row as an error, including
        # client aborts; the series keeps that same definition.
        self.assertEqual(totals["errors"], 2)
        self.assertEqual(totals["tokens"], 150)
        self.assertAlmostEqual(totals["credit"], 3.5)

    def test_realm_filter(self):
        now = time.time()
        self.write([
            row(now - 30, total_tokens=100, realm="cn"),
            row(now - 20, total_tokens=200, realm="intl"),
        ])
        cn = wb_proxy.usage_timeseries(realm="cn", range="custom", since=now - 60, until=now)
        self.assertEqual(sum(b["total_tokens"] for b in cn["series"]), 100)
        all_realms = wb_proxy.usage_timeseries(realm="all", range="custom", since=now - 60,
                                               until=now)
        self.assertEqual(sum(b["total_tokens"] for b in all_realms["series"]), 300)


class TimeseriesRouteTests(unittest.TestCase):
    def make_handler(self, authorized=True):
        class Handler(wb_proxy.Handler):
            def __init__(self):
                self.captured = None

            def _authorized(self):
                return authorized

            def _json(self, code, obj):
                self.captured = (code, obj)

        return Handler()

    def test_route_parses_realm_range_and_bucket(self):
        captured = {}

        def fake_series(**kwargs):
            captured.update(kwargs)
            return {"ok": True, "series": []}

        handler = self.make_handler()
        with mock.patch.object(wb_proxy, "usage_timeseries", side_effect=fake_series):
            handler._get_usage_timeseries({
                "realm": ["cn"], "range": ["custom"],
                "since": ["1000"], "until": ["2000"], "bucket": ["120"]})
        code, _obj = handler.captured
        self.assertEqual(code, 200)
        self.assertEqual(captured["realm"], "cn")
        self.assertEqual(captured["range"], "custom")
        self.assertEqual(captured["since"], "1000")
        self.assertEqual(captured["until"], "2000")
        self.assertEqual(captured["bucket_seconds"], 120)

    def test_route_requires_auth(self):
        handler = self.make_handler(authorized=False)
        handler._get_usage_timeseries({})
        self.assertIsNone(handler.captured)


if __name__ == "__main__":
    unittest.main()
