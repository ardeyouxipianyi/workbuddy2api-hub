"""Web-activity concurrency: run N intl daily conversations at once.

The sweep used to walk every account one at a time. A deployment with many
intl accounts can raise the concurrency limit so the daily activity finishes
sooner; 1 keeps the historical serial behaviour.
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-daily-concurrency-")

import wb_accounts  # noqa: E402
import wb_proxy  # noqa: E402
import wb_scheduler  # noqa: E402
import wb_settings  # noqa: E402

ACCOUNTS = os.path.join(_TMP.name, "accounts")


class FakePool(object):
    def __init__(self, directory, accounts):
        self.dir = directory
        self.accounts = accounts


def make_account(uid, realm="intl"):
    acc = mock.MagicMock()
    acc.uid = uid
    acc.realm = realm
    acc.restricted_until = 0.0
    acc.can_daily_chat.return_value = True
    acc.daily_chat.return_value = {"ok": True, "msg": "ok"}
    return acc


class ConcurrencySettingTests(unittest.TestCase):
    def test_default_is_one(self):
        d = tempfile.mkdtemp(prefix="daily-conc-default-")
        self.assertEqual(wb_settings.daily_chat_web_concurrency(d), 1)

    def test_round_trip_and_bounds(self):
        d = tempfile.mkdtemp(prefix="daily-conc-roundtrip-")
        self.assertEqual(wb_settings.set_daily_chat_web_concurrency(d, 3), 3)
        self.assertEqual(wb_settings.daily_chat_web_concurrency(d), 3)
        with self.assertRaises(ValueError):
            wb_settings.set_daily_chat_web_concurrency(d, 0)
        with self.assertRaises(ValueError):
            wb_settings.set_daily_chat_web_concurrency(
                d, wb_settings.DAILY_CHAT_WEB_CONCURRENCY_MAX + 1)

    def test_invalid_stored_value_falls_back(self):
        d = tempfile.mkdtemp(prefix="daily-conc-invalid-")
        data = wb_settings.load(d)
        data["daily_chat_web_concurrency"] = "three"
        wb_settings.save(d, data)
        self.assertEqual(wb_settings.daily_chat_web_concurrency(d), 1)


class RuntimeViewTests(unittest.TestCase):
    """The panel reads /settings and posts it back on the next save.

    `runtime_settings_view()` listed `daily_chat_web` and its retry/attempts/
    timeout/fallback neighbours but not `daily_chat_web_concurrency`, so
    dashboard.html fell back to 1 and posted 1 back - a configured concurrency
    of 4 was flattened the first time any setting was saved.
    """

    def _view(self, directory):
        with mock.patch.multiple(wb_proxy, ACCOUNTS_DIR=directory,
                                 POOL=None, SCHEDULER=None):
            return wb_proxy.runtime_settings_view()

    def test_view_reports_the_stored_concurrency(self):
        d = tempfile.mkdtemp(prefix="daily-conc-view-")
        wb_settings.set_daily_chat_web_concurrency(d, 4)
        self.assertEqual(self._view(d).get("daily_chat_web_concurrency"), 4)

    def test_view_defaults_to_one_when_unset(self):
        d = tempfile.mkdtemp(prefix="daily-conc-view-default-")
        self.assertEqual(self._view(d).get("daily_chat_web_concurrency"), 1)


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="daily-conc-batch-")
        self.accounts = [make_account("u%d" % i) for i in range(4)]
        self.sched = wb_scheduler.Scheduler(FakePool(self.dir, self.accounts))
        self.sched.log = lambda *a, **k: None

    def test_concurrency_limit_is_honoured(self):
        wb_settings.set_daily_chat_web_concurrency(self.dir, 2)
        active = [0]
        peak = [0]
        lock = threading.Lock()
        release = threading.Event()
        result = []

        def fake_run(acc):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            # Hold the first wave until the main thread has observed the peak;
            # a plain sleep would race with the scheduler's start stagger.
            release.wait(timeout=2)
            with lock:
                active[0] -= 1
            return True

        with mock.patch.object(self.sched, "_run_one_daily_chat",
                               side_effect=fake_run):
            worker = threading.Thread(
                target=lambda: result.append(
                    self.sched._run_daily_chat_batch(self.accounts)))
            worker.start()
            deadline = time.time() + 3
            while peak[0] < 2 and time.time() < deadline:
                time.sleep(0.01)
            release.set()
            worker.join(timeout=5)
        self.assertEqual(result, [4])
        self.assertEqual(peak[0], 2)

    def test_serial_path_runs_one_at_a_time(self):
        wb_settings.set_daily_chat_web_concurrency(self.dir, 1)
        active = [0]
        peak = [0]
        lock = threading.Lock()

        def fake_run(acc):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            with lock:
                active[0] -= 1
            return True

        with mock.patch.object(self.sched, "_run_one_daily_chat",
                               side_effect=fake_run), \
             mock.patch.object(wb_scheduler.time, "sleep", return_value=None):
            count = self.sched._run_daily_chat_batch(self.accounts)
        self.assertEqual(count, 4)
        self.assertEqual(peak[0], 1)

    def test_failures_are_counted_separately(self):
        wb_settings.set_daily_chat_web_concurrency(self.dir, 2)
        with mock.patch.object(self.sched, "_run_one_daily_chat",
                               side_effect=[True, False, True, False]), \
             mock.patch.object(wb_scheduler.time, "sleep", return_value=None):
            count = self.sched._run_daily_chat_batch(self.accounts)
        self.assertEqual(count, 2)


class CycleCollectsIntlAccountsTests(unittest.TestCase):
    def test_cycle_hands_intl_accounts_to_the_batch(self):
        directory = tempfile.mkdtemp(prefix="daily-conc-cycle-")
        intl = [make_account("i1"), make_account("i2")]
        pool = FakePool(directory, intl)
        sched = wb_scheduler.Scheduler(pool)
        sched.log = lambda *a, **k: None
        seen = []

        def fake_batch(accounts):
            seen.extend(accounts)
            return len(accounts)

        at_nine = time.struct_time((2026, 10, 21, 9, 0, 0, 0, 0, -1))
        with mock.patch.object(sched, "_run_daily_chat_batch",
                               side_effect=fake_batch), \
             mock.patch.object(wb_proxy, "restriction_hold_remaining",
                               return_value=0.0), \
             mock.patch.object(wb_scheduler.time, "localtime",
                               return_value=at_nine):
            sched._run_cycle()
        self.assertEqual(seen, intl)


if __name__ == "__main__":
    unittest.main(verbosity=2)
