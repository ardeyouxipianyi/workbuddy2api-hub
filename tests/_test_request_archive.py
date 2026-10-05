"""Request archive + metrics (M4 D1/D2).

Run with: python _test_request_archive.py
No upstream credentials or outbound network are used.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="request-archive-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy
import wb_reqlog
import wb_settings


def row(at, model="m1", account="uid-1", status=200, error=False,
        elapsed_ms=100, ttft_ms=50, outcome="completed", path="/v1/responses",
        request_id="req-1"):
    return {"at": at, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at)),
            "model": model, "account": account, "status": status, "error": error,
            "elapsed_ms": elapsed_ms, "ttft_ms": ttft_ms, "outcome": outcome,
            "path": path, "request_id": request_id}


class MetricsTests(unittest.TestCase):
    def test_compute_metrics(self):
        now = time.time()
        rows = [row(now, elapsed_ms=100, ttft_ms=40),
                row(now, elapsed_ms=200, ttft_ms=60),
                row(now, status=502, error=True, elapsed_ms=300, ttft_ms=None,
                    outcome="upstream_aborted")]
        metrics = wb_reqlog.compute_metrics(rows)
        self.assertEqual(metrics["requests"], 3)
        self.assertEqual(metrics["errors"], 1)
        self.assertAlmostEqual(metrics["completion_rate"], 2 / 3)
        self.assertAlmostEqual(metrics["http_success_rate"], 2 / 3)
        self.assertAlmostEqual(metrics["avg_elapsed_ms"], 200)
        self.assertEqual(metrics["p50_elapsed_ms"], 200)
        self.assertEqual(metrics["p95_elapsed_ms"], 300)
        self.assertAlmostEqual(metrics["avg_ttfb_ms"], 50)

    def test_empty_metrics_are_none(self):
        metrics = wb_reqlog.compute_metrics([])
        self.assertEqual(metrics["requests"], 0)
        self.assertIsNone(metrics["completion_rate"])
        self.assertIsNone(metrics["avg_elapsed_ms"])

    def test_filter_rows(self):
        now = time.time()
        rows = [row(now - 100, model="a", status=200),
                row(now - 50, model="b", status=502, error=True,
                    outcome="failed", path="/v1/chat/completions"),
                row(now - 10, model="a", status=200, account="uid-2")]
        self.assertEqual(len(wb_reqlog.filter_rows(rows, model="a")), 2)
        self.assertEqual(len(wb_reqlog.filter_rows(rows, account="uid-2")), 1)
        self.assertEqual(len(wb_reqlog.filter_rows(rows, status=502)), 1)
        self.assertEqual(len(wb_reqlog.filter_rows(rows, error=True)), 1)
        self.assertEqual(len(wb_reqlog.filter_rows(rows, error=False)), 2)
        self.assertEqual(len(wb_reqlog.filter_rows(rows, outcome="failed")), 1)
        self.assertEqual(len(wb_reqlog.filter_rows(rows,
                                                   path="/v1/chat/completions")), 1)
        self.assertEqual(len(wb_reqlog.filter_rows(rows, since=now - 60)), 2)
        self.assertEqual(len(wb_reqlog.filter_rows(rows, until=now - 60)), 1)
        self.assertEqual(len(wb_reqlog.filter_rows(rows, request_id="req-1")), 3)


class ArchiveTests(unittest.TestCase):
    def write_rows(self, path, rows):
        with open(path, "w", encoding="utf-8") as fh:
            for item in rows:
                fh.write(json.dumps(item) + "\n")

    def test_read_rows_combines_archives_and_main(self):
        with tempfile.TemporaryDirectory() as directory:
            now = time.time()
            self.write_rows(os.path.join(directory, "usage-archive-20260101.jsonl"),
                            [row(now - 200, request_id="old")])
            self.write_rows(os.path.join(directory, "usage.jsonl"),
                            [row(now - 100, request_id="new")])
            rows = wb_reqlog.read_rows(directory)
            limited = wb_reqlog.read_rows(directory, limit=1)
        self.assertEqual([r["request_id"] for r in rows], ["old", "new"])
        self.assertEqual([r["request_id"] for r in limited], ["new"])

    def test_compaction_moves_old_rows_to_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "usage.jsonl")
            now = time.time()
            # Force the size check over 1MB with padding on the OLD rows;
            # the recent row stays small so it survives the size budget.
            old_rows = []
            for i in range(30):
                item = row(now - 10 * 86400, request_id="old-%d" % i)
                item["pad"] = "x" * (40 * 1024)
                old_rows.append(item)
            recent = row(now - 60, request_id="recent")
            self.write_rows(path, old_rows + [recent])
            changed = wb_reqlog.compact_main(path, max_mb=1, retention_days=7,
                                             now=now)
            self.assertTrue(changed)
            with open(path, encoding="utf-8") as fh:
                main_rows = [json.loads(line) for line in fh if line.strip()]
            self.assertEqual([r["request_id"] for r in main_rows], ["recent"])
            archives = wb_reqlog.archive_files(directory)
            self.assertEqual(len(archives), 1)
            with open(archives[0], encoding="utf-8") as fh:
                archived = [json.loads(line) for line in fh if line.strip()]
            self.assertEqual([r["request_id"] for r in archived],
                             ["old-%d" % i for i in range(30)])

    def test_append_during_rotation_is_not_lost(self):
        """BUG-3: an append that races the read->replace window survives."""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "usage.jsonl")
            now = time.time()
            old_rows = []
            for i in range(30):
                item = row(now - 10 * 86400, request_id="old-%d" % i)
                item["pad"] = "x" * (40 * 1024)
                old_rows.append(item)
            self.write_rows(path, old_rows + [row(now - 60, request_id="recent")])

            appended = threading.Event()
            state = {"injected": False}
            real_loads = json.loads

            def append_now():
                with wb_reqlog.LOCK:
                    with open(path, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(row(time.time(), request_id="during")) + "\n")
                appended.set()

            def slow_loads(line, *args, **kwargs):
                if not state["injected"]:
                    state["injected"] = True
                    threading.Thread(target=append_now).start()
                    time.sleep(0.25)  # let the append reach the lock
                return real_loads(line, *args, **kwargs)

            with mock.patch.object(wb_reqlog.json, "loads", side_effect=slow_loads):
                changed = wb_reqlog.compact_main(path, max_mb=1, retention_days=7, now=now)
            self.assertTrue(changed)
            self.assertTrue(appended.wait(timeout=5),
                            "the concurrent append never completed")
            with open(path, encoding="utf-8") as fh:
                ids = [json.loads(line)["request_id"] for line in fh if line.strip()]
        self.assertIn("during", ids)

    def test_read_rows_is_cached_until_a_file_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "usage.jsonl")
            now = time.time()
            self.write_rows(path, [row(now - 100, request_id="one")])
            calls = {"n": 0}
            real_iter = wb_reqlog._iter_rows

            def counting_iter(path, *args, **kwargs):
                calls["n"] += 1
                return real_iter(path, *args, **kwargs)

            with mock.patch.object(wb_reqlog, "_iter_rows",
                                   side_effect=counting_iter):
                first = wb_reqlog.read_rows(directory)
                second = wb_reqlog.read_rows(directory)
                self.assertEqual(calls["n"], 1)
                self.write_rows(path, [row(now - 100, request_id="one"),
                                       row(now - 50, request_id="two")])
                third = wb_reqlog.read_rows(directory)
            self.assertEqual(calls["n"], 2)
            self.assertEqual([r["request_id"] for r in first], ["one"])
            self.assertEqual([r["request_id"] for r in second], ["one"])
            self.assertEqual([r["request_id"] for r in third], ["one", "two"])

    def test_rotation_aborts_if_the_file_changed_after_the_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "usage.jsonl")
            now = time.time()
            old_rows = []
            for i in range(30):
                item = row(now - 10 * 86400, request_id="old-%d" % i)
                item["pad"] = "x" * (40 * 1024)
                old_rows.append(item)
            self.write_rows(path, old_rows + [row(now - 60, request_id="recent")])
            with open(path, encoding="utf-8") as fh:
                before = fh.read()
            real_stamp = wb_reqlog._file_stamp
            calls = {"n": 0}

            def racing_stamp(target):
                calls["n"] += 1
                if calls["n"] == 1:
                    return real_stamp(target)
                return ("changed", 1)  # a writer slipped in after the read

            with mock.patch.object(wb_reqlog, "_file_stamp",
                                   side_effect=racing_stamp):
                changed = wb_reqlog.compact_main(path, max_mb=1,
                                                 retention_days=7, now=now)
            self.assertFalse(changed)
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), before)
            self.assertFalse(os.path.exists(path + ".rotate.tmp"))
            self.assertEqual(wb_reqlog.archive_files(directory), [])

    def test_prune_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            old_path = os.path.join(directory, "usage-archive-20260101.jsonl")
            new_path = os.path.join(directory, "usage-archive-20990101.jsonl")
            for path in (old_path, new_path):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("{}\n")
            old_time = time.time() - 30 * 86400
            os.utime(old_path, (old_time, old_time))
            removed = wb_reqlog.prune_archives(directory, retention_days=7)
            self.assertEqual(removed, 1)
            self.assertFalse(os.path.exists(old_path))
            self.assertTrue(os.path.exists(new_path))

    def test_rotation_is_throttled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "usage.jsonl")
            now = time.time()
            self.write_rows(path, [row(now - 60)])
            key = os.path.abspath(path)
            wb_reqlog._last_check[key] = now
            # Within the throttle window: no rotation attempt at all.
            self.assertFalse(wb_reqlog.rotate_if_needed(path, max_mb=1,
                                                        retention_days=7,
                                                        now=now + 5))
            # Past the window: the (small) file needs no rotation, but the
            # check itself is allowed to run.
            self.assertFalse(wb_reqlog.rotate_if_needed(path, max_mb=1,
                                                        retention_days=7,
                                                        now=now + 120))


class LoggingSettingsTests(unittest.TestCase):
    def test_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = wb_settings.logging_config(directory)
        self.assertEqual(cfg, {"record_client_info": False, "retention_days": 7,
                               "archive_max_mb": 100})

    def test_roundtrip_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            wb_settings.set_logging_config(directory, {"record_client_info": True})
            cfg = wb_settings.logging_config(directory)
            self.assertTrue(cfg["record_client_info"])
            wb_settings.set_logging_config(directory, {"retention_days": 14})
            self.assertEqual(wb_settings.logging_config(directory)["retention_days"], 14)
        for bad in ({"record_client_info": "yes"}, {"retention_days": 0},
                    {"retention_days": True}, {"archive_max_mb": -1},
                    {"unknown": 1}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                wb_settings.validate_logging_patch(bad)


class RequestContextTests(unittest.TestCase):
    def setUp(self):
        wb_proxy._LOGGING_CFG_CACHE["cfg"] = None
        wb_proxy._LOGGING_CFG_CACHE["at"] = 0.0
        self.dir = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(wb_proxy, "ACCOUNTS_DIR", self.dir.name)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.dir.cleanup)

    def test_request_id_always_recorded(self):
        wb_proxy.set_request_context(request_id="req-9", client_ip="1.2.3.4",
                                     user_agent="UA", path="/v1/responses")
        fields = wb_proxy._request_context_fields()
        self.assertEqual(fields["request_id"], "req-9")
        self.assertEqual(fields["path"], "/v1/responses")
        self.assertNotIn("client_ip", fields)
        self.assertNotIn("user_agent", fields)

    def test_client_info_opt_in(self):
        wb_settings.set_logging_config(self.dir.name, {"record_client_info": True})
        wb_proxy._LOGGING_CFG_CACHE["cfg"] = None
        wb_proxy.set_request_context(request_id="req-9", client_ip="1.2.3.4",
                                     user_agent="U" * 400, path="/v1/responses")
        fields = wb_proxy._request_context_fields()
        self.assertEqual(fields["client_ip"], "1.2.3.4")
        self.assertEqual(len(fields["user_agent"]), 300)

    def test_persist_usage_includes_the_context(self):
        wb_proxy.set_request_context(request_id="req-77", client_ip="1.2.3.4",
                                     user_agent="UA", path="/v1/chat/completions")
        with tempfile.TemporaryDirectory() as directory:
            usage_log = os.path.join(directory, "usage.jsonl")
            stored_row = row(time.time(), request_id="ignored")
            stored_row.update(wb_proxy._request_context_fields())
            with mock.patch.object(wb_proxy, "USAGE_DIR", directory), \
                    mock.patch.object(wb_proxy, "USAGE_LOG", usage_log):
                wb_proxy._persist_usage(stored_row, "test")
            with open(usage_log, encoding="utf-8") as fh:
                stored = json.loads(fh.readline())
        self.assertEqual(stored["request_id"], "req-77")
        self.assertEqual(stored["path"], "/v1/chat/completions")
        self.assertNotIn("client_ip", stored)


class RequestRouteTests(unittest.TestCase):
    def make_handler(self):
        class Handler(wb_proxy.Handler):
            def __init__(self):
                self.captured = None

            def _json(self, code, obj):
                self.captured = (code, obj)

        return Handler()

    def test_requests_route_filters_and_orders(self):
        now = time.time()
        rows = [row(now - 100, request_id="a"),
                row(now - 50, request_id="b", error=True, status=502),
                row(now - 10, request_id="c")]
        handler = self.make_handler()
        with mock.patch.object(wb_proxy.wb_reqlog, "read_rows", return_value=rows):
            handler._route_requests({"error": True})
            code, obj = handler.captured
        self.assertEqual(code, 200)
        self.assertEqual(obj["total"], 1)
        self.assertEqual(obj["rows"][0]["request_id"], "b")

    def test_requests_route_limit(self):
        now = time.time()
        rows = [row(now - i, request_id="r%d" % i) for i in range(10)]
        handler = self.make_handler()
        with mock.patch.object(wb_proxy.wb_reqlog, "read_rows", return_value=rows):
            handler._route_requests({"limit": 3})
            _code, obj = handler.captured
        self.assertEqual(len(obj["rows"]), 3)
        self.assertEqual(obj["rows"][0]["request_id"], "r9")

    def test_metrics_route(self):
        now = time.time()
        rows = [row(now - 10, elapsed_ms=100, ttft_ms=50)]
        handler = self.make_handler()
        with mock.patch.object(wb_proxy.wb_reqlog, "read_rows", return_value=rows):
            handler._route_requests_metrics({})
            _code, obj = handler.captured
        self.assertEqual(obj["metrics"]["requests"], 1)
        self.assertAlmostEqual(obj["metrics"]["avg_elapsed_ms"], 100)


if __name__ == "__main__":
    unittest.main()
