"""Log ring buffer + channel classification (M4 D4).

Run with: python _test_log_ring.py
No upstream credentials or outbound network are used.
"""
import os
import sys
import tempfile
import unittest

_startup_dir = tempfile.TemporaryDirectory(prefix="log-ring-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy


class ChannelTests(unittest.TestCase):
    def setUp(self):
        wb_proxy.clear_logs()

    def test_channels_are_classified_from_the_message(self):
        cases = [
            ("chat: model=x stream=True", "chat"),
            ("[调度器] 巡检开始", "scheduler"),
            ("task template_5 done", "tasks"),
            ("account uid-1 imported", "accounts"),
            ("model catalog refreshed", "catalog"),
            ("auth token refreshed", "auth"),
            ("settings saved", "settings"),
            ("something else entirely", "system"),
        ]
        for message, expected in cases:
            entry = wb_proxy.add_log_entry(message)
            self.assertEqual(entry["tag"], expected, message)

    def test_level_is_inferred(self):
        self.assertEqual(wb_proxy.add_log_entry("failed to connect")["level"], "ERROR")
        self.assertEqual(wb_proxy.add_log_entry("retry scheduled")["level"], "WARN")
        self.assertEqual(wb_proxy.add_log_entry("all good")["level"], "INFO")

    def test_explicit_tag_and_level_win(self):
        entry = wb_proxy.add_log_entry("hello", level="WARN", tag="chat")
        self.assertEqual(entry["level"], "WARN")
        self.assertEqual(entry["tag"], "chat")


class RingBufferTests(unittest.TestCase):
    def setUp(self):
        wb_proxy.clear_logs()

    def test_ring_keeps_the_newest_entries(self):
        capacity = wb_proxy.LOG_BUFFER.maxlen
        self.assertEqual(capacity, 2000)
        for i in range(capacity + 50):
            wb_proxy.add_log_entry("message %d" % i, tag="system")
        items = wb_proxy.get_logs(limit=0)["logs"]
        self.assertEqual(len(items), capacity)
        self.assertEqual(items[0]["msg"], "message 50")
        self.assertEqual(items[-1]["msg"], "message %d" % (capacity + 49))

    def test_get_logs_filters(self):
        wb_proxy.add_log_entry("chat one", level="INFO", tag="chat")
        wb_proxy.add_log_entry("chat error", level="ERROR", tag="chat")
        wb_proxy.add_log_entry("task one", level="INFO", tag="tasks")
        self.assertEqual(wb_proxy.get_logs(tag="chat")["total"], 2)
        self.assertEqual(wb_proxy.get_logs(level="error")["total"], 1)
        self.assertEqual(wb_proxy.get_logs(search="task")["total"], 1)
        self.assertEqual(wb_proxy.get_logs(limit=2)["total"], 3)

    def test_since_id_returns_only_new_entries(self):
        first = wb_proxy.add_log_entry("one", tag="system")
        wb_proxy.add_log_entry("two", tag="system")
        result = wb_proxy.get_logs(since_id=first["id"])
        self.assertEqual([entry["msg"] for entry in result["logs"]], ["two"])
        self.assertEqual(result["max_id"] > first["id"], True)

    def test_clear_logs(self):
        wb_proxy.add_log_entry("one", tag="system")
        wb_proxy.clear_logs()
        self.assertEqual(wb_proxy.get_logs()["total"], 0)


if __name__ == "__main__":
    unittest.main()
