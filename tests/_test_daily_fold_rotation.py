"""The daily token/credit fold must not carry an offset across a rewritten log.

`daily_usage_stats()` folds `usage.jsonl` incrementally: it remembers a byte
offset and only reads rows past it. That only works while the bytes it already
counted are still there. The original check was `offset > size`, which catches
a truncated log and a replaced-but-shorter file, but **not** the case that
actually bites in practice:

    logrotate with `copytruncate`

empties the file in place (same inode) and it then grows past the old offset.
Size is now larger, so the guard passes; the bytes underneath were replaced, so
the fold keeps counting rows that no longer exist *and* silently skips every
row in the first `offset` bytes of the new file.

Both halves are wrong in the same direction only by accident - one inflates, the
other deflates - so the bug does not announce itself. What it does produce is
the daily guard parking accounts on a number the operator cannot reproduce from
the log, which is why this is pinned rather than left to the aggregate folds
that already got it right (`_log_offset_holds` + `_log_tail_signature`, used by
perf / aggregate / recent).

No network: the log is synthesised in a temp directory.
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-daily-rotate-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_proxy as P

LOG = os.path.join(os.environ["WB_PROXY_USAGE_DIR"], "usage.jsonl")


def row(at, account, total, model="deepseek-v4.1-flash"):
    return {
        "at": at, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at)),
        "model": model, "stream": True, "outcome": "completed",
        "total_tokens": total, "account": account, "realm": "intl",
    }


def append(*rows):
    with open(LOG, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def reset_cache():
    """Drop every field the cache carries, including the new key/tail seam."""
    with P._daily_usage_lock:
        P._daily_usage.update({
            "day": "", "totals": None, "credits": None, "models": None,
            "offset": 0, "at": 0.0, "key": None, "tail": None,
        })


class DailyFoldRotationTests(unittest.TestCase):
    def setUp(self):
        reset_cache()
        if os.path.exists(LOG):
            os.remove(LOG)
        self.now = time.time()

    def tearDown(self):
        reset_cache()

    # -- the regression this suite exists for ------------------------------

    def test_copytruncate_does_not_keep_the_old_fold(self):
        """Empty in place, write new rows: the old totals must not survive."""
        for _ in range(10):
            append(row(self.now, "u1", 100))
        first = P.daily_usage_stats(ttl=0)
        self.assertEqual(first["tokens"].get("u1"), 1000)

        # copytruncate: same inode, emptied, then regrown past the old offset.
        os.truncate(LOG, 0)
        for _ in range(12):
            append(row(self.now, "u2", 50))

        after = P.daily_usage_stats(ttl=0)
        self.assertNotIn(
            "u1", after["tokens"],
            "the emptied file no longer holds u1's rows, so they must not count",
        )
        self.assertEqual(
            after["tokens"].get("u2"), 600,
            "every row after the truncate point must be scanned, not skipped",
        )

    def test_copytruncate_then_grow_past_old_offset(self):
        """The nasty shape: new file is *longer* than the old offset."""
        for _ in range(4):
            append(row(self.now, "old", 1000))
        P.daily_usage_stats(ttl=0)
        old_offset = P._daily_usage["offset"]
        self.assertGreater(old_offset, 0)

        os.truncate(LOG, 0)
        # Longer than old_offset on purpose: the size test would pass here.
        padded = row(self.now, "new", 7, model="m" * 40)
        while os.path.getsize(LOG) <= old_offset:
            append(padded)

        expected_rows = 0
        with open(LOG, encoding="utf-8") as fh:
            expected_rows = sum(1 for line in fh if line.strip())

        after = P.daily_usage_stats(ttl=0)
        self.assertNotIn("old", after["tokens"])
        self.assertEqual(after["tokens"].get("new"), 7 * expected_rows)

    # -- the cases the old size check already handled (must keep working) ---

    def test_plain_append_still_skips_nothing(self):
        append(row(self.now, "u1", 10))
        first = P.daily_usage_stats(ttl=0)
        self.assertEqual(first["tokens"].get("u1"), 10)

        append(row(self.now, "u1", 5))
        second = P.daily_usage_stats(ttl=0)
        self.assertEqual(second["tokens"].get("u1"), 15)

    def test_truncated_to_zero_restarts(self):
        append(row(self.now, "u1", 100))
        P.daily_usage_stats(ttl=0)
        os.truncate(LOG, 0)
        after = P.daily_usage_stats(ttl=0)
        self.assertEqual(after["tokens"], {})

    def test_replaced_by_shorter_file_restarts(self):
        for _ in range(6):
            append(row(self.now, "u1", 100))
        P.daily_usage_stats(ttl=0)

        os.remove(LOG)
        append(row(self.now, "u2", 3))
        after = P.daily_usage_stats(ttl=0)
        self.assertNotIn("u1", after["tokens"])
        self.assertEqual(after["tokens"].get("u2"), 3)

    def test_missing_log_returns_none_not_empty_dict(self):
        """The contract callers rely on: `None` means "could not read", so a
        broken read never parks an account as if it had used nothing."""
        self.assertIsNone(P.daily_usage_stats(ttl=0))

    # -- the seam must actually be recorded --------------------------------

    def test_cache_records_the_seam_it_consumed(self):
        append(row(self.now, "u1", 1))
        P.daily_usage_stats(ttl=0)
        state = P._daily_usage
        self.assertIsNotNone(state.get("key"), "must remember size/dev/inode")
        self.assertIsNotNone(state.get("tail"), "must remember the bytes before offset")
        self.assertEqual(state["key"][0], os.path.getsize(LOG))
        self.assertEqual(len(state["tail"]), min(state["offset"], P._TAIL_SIGNATURE))

    def test_same_size_rewrite_is_caught(self):
        """Overwrite in place without changing the length: size cannot help."""
        append(row(self.now, "u1", 100))
        P.daily_usage_stats(ttl=0)

        # Rewrite the same number of bytes with a different account name.
        size = os.path.getsize(LOG)
        new = json.dumps(row(self.now, "u2", 100), ensure_ascii=False) + "\n"
        line = new
        if len(line) < size:
            line = line[:-1] + " " * (size - len(line)) + "\n"
        elif len(line) > size:
            self.skipTest("generated row is longer than the original; shape differs")
        with open(LOG, "w", encoding="utf-8") as fh:
            fh.write(line)

        after = P.daily_usage_stats(ttl=0)
        self.assertNotIn(
            "u1", after["tokens"],
            "a same-size rewrite still replaced the counted bytes",
        )


class DailyFoldOrderingTests(unittest.TestCase):
    """A scan that fails must not leave a half-advanced cache behind."""

    def setUp(self):
        reset_cache()
        if os.path.exists(LOG):
            os.remove(LOG)

    def tearDown(self):
        reset_cache()

    def test_failed_scan_clears_the_seam(self):
        append(row(time.time(), "u1", 5))
        P.daily_usage_stats(ttl=0)
        self.assertIsNotNone(P._daily_usage["key"])

        original = P._scan_daily_usage
        P._scan_daily_usage = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
        try:
            self.assertIsNone(P.daily_usage_stats(ttl=0))
        finally:
            P._scan_daily_usage = original

        self.assertIsNone(P._daily_usage["totals"], "totals must go unknown")
        self.assertEqual(P._daily_usage["offset"], 0, "offset must restart at zero")
        self.assertIsNone(P._daily_usage["key"], "a stale seam must not be reused")
        self.assertIsNone(P._daily_usage["tail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
