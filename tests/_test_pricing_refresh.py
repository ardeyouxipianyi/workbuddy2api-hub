"""The pricing refresh interval: minutes as the stored unit, the one-time
migration from the old hours key, and the loop that consumes it.

The setting used to be counted in hours (`pricing_refresh_hours`). Reading
that number as minutes would turn 6 hours into 6 minutes, so an upgrade has to
convert it (x60), write the new key and drop the old one - once. These tests
pin the conversion, the 0-disables contract, the cap, the invalid-value
fallback, and the refresher's wait arithmetic.

No network: the refresher's fetch is stubbed out, and the loop tests only
observe how long a cycle takes.
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_pricing
import wb_settings


def _read_settings(path):
    with open(os.path.join(path, "settings.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _write_settings(path, data):
    with open(os.path.join(path, "settings.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh)


class PricingRefreshSettingTests(unittest.TestCase):
    """Minutes are the unit in storage; a legacy hours value is converted."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wb-refresh-setting-")

    def test_default_is_five_minutes(self):
        self.assertEqual(wb_settings.pricing_refresh_minutes(self.dir), 5.0)

    def test_a_legacy_hours_value_is_converted_once(self):
        _write_settings(self.dir, {"pricing_refresh_hours": 6})
        self.assertEqual(wb_settings.pricing_refresh_minutes(self.dir), 360.0)
        stored = _read_settings(self.dir)
        self.assertEqual(stored["pricing_refresh_minutes"], 360.0)
        self.assertNotIn("pricing_refresh_hours", stored)

    def test_the_conversion_does_not_run_again(self):
        _write_settings(self.dir, {"pricing_refresh_hours": 6})
        wb_settings.pricing_refresh_minutes(self.dir)
        # A stale legacy key - a rolled-back install writing one - must not
        # resurrect an hours value over the migrated one.
        data = _read_settings(self.dir)
        data["pricing_refresh_hours"] = 12
        _write_settings(self.dir, data)
        self.assertEqual(wb_settings.pricing_refresh_minutes(self.dir), 360.0)

    def test_a_legacy_zero_still_means_off(self):
        _write_settings(self.dir, {"pricing_refresh_hours": 0})
        self.assertEqual(wb_settings.pricing_refresh_minutes(self.dir), 0.0)
        self.assertEqual(_read_settings(self.dir)["pricing_refresh_minutes"], 0.0)

    def test_fractional_legacy_hours_convert(self):
        _write_settings(self.dir, {"pricing_refresh_hours": 0.5})
        self.assertEqual(wb_settings.pricing_refresh_minutes(self.dir), 30.0)

    def test_the_old_cap_remeasures_to_a_month_of_minutes(self):
        # The cap was 720 hours; in the new unit that is 43200 minutes and has
        # to survive rather than clamp to the same raw number.
        _write_settings(self.dir, {"pricing_refresh_hours": 720})
        self.assertEqual(wb_settings.pricing_refresh_minutes(self.dir), 43200.0)

    def test_a_legacy_value_that_is_not_a_number_falls_back(self):
        _write_settings(self.dir, {"pricing_refresh_hours": "soon"})
        self.assertEqual(wb_settings.pricing_refresh_minutes(self.dir), 5.0)
        stored = _read_settings(self.dir)
        self.assertNotIn("pricing_refresh_hours", stored)
        self.assertNotIn("pricing_refresh_minutes", stored)

    def test_the_minutes_key_wins_over_a_leftover_legacy_key(self):
        _write_settings(self.dir, {"pricing_refresh_minutes": 15,
                                   "pricing_refresh_hours": 6})
        self.assertEqual(wb_settings.pricing_refresh_minutes(self.dir), 15.0)

    def test_a_negative_or_unparsable_minutes_value(self):
        _write_settings(self.dir, {"pricing_refresh_minutes": -5})
        self.assertEqual(wb_settings.pricing_refresh_minutes(self.dir), 0.0)
        _write_settings(self.dir, {"pricing_refresh_minutes": "often"})
        self.assertEqual(wb_settings.pricing_refresh_minutes(self.dir), 5.0)

    def test_the_setter_stores_minutes_and_drops_the_legacy_key(self):
        _write_settings(self.dir, {"pricing_refresh_hours": 6})
        self.assertEqual(
            wb_settings.set_pricing_refresh_minutes(self.dir, 20), 20.0)
        stored = _read_settings(self.dir)
        self.assertEqual(stored["pricing_refresh_minutes"], 20.0)
        self.assertNotIn("pricing_refresh_hours", stored)

    def test_the_setter_clamps_in_minutes(self):
        self.assertEqual(
            wb_settings.set_pricing_refresh_minutes(self.dir, 10 ** 9), 43200.0)
        self.assertEqual(
            wb_settings.set_pricing_refresh_minutes(self.dir, -3), 0.0)

    def test_the_setter_keeps_the_stored_value_for_garbage_input(self):
        _write_settings(self.dir, {"pricing_refresh_minutes": 30})
        self.assertEqual(
            wb_settings.set_pricing_refresh_minutes(self.dir, "x"), 30.0)


class PriceRefresherTests(unittest.TestCase):
    """The refresher waits minutes, and a live change is picked up at once."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wb-refresher-")
        self._previous = wb_pricing.data_dir()
        wb_pricing.set_data_dir(self.dir)
        self._threads = []

    def tearDown(self):
        for thread in self._threads:
            thread.stop()
            thread.join(timeout=5)
        wb_pricing.set_data_dir(self._previous)

    def _refresher(self, interval_minutes):
        refresher = wb_pricing.PriceRefresher(interval_minutes=interval_minutes)
        refresher.log = lambda msg: None
        self.calls = []
        refresher.run_once = lambda: (self.calls.append(time.time()),
                                      (True, ""))[1]
        self._threads.append(refresher)
        return refresher

    @staticmethod
    def _wait_for_calls(calls, count, deadline_sec):
        end = time.time() + deadline_sec
        while len(calls) < count and time.time() < end:
            time.sleep(0.05)
        return len(calls)

    def test_the_unit_is_minutes(self):
        refresher = wb_pricing.PriceRefresher(interval_minutes=5)
        self.assertEqual(refresher.interval_minutes, 5)
        self.assertAlmostEqual(refresher.interval_seconds(), 300.0)
        self.assertEqual(wb_pricing.DEFAULT_REFRESH_MINUTES, 5)

    def test_status_reports_minutes(self):
        status = wb_pricing.PriceRefresher(interval_minutes=7).status()
        self.assertEqual(status["interval_minutes"], 7)
        self.assertTrue(status["enabled"])
        self.assertNotIn("interval_hours", status)

    def test_the_loop_cycles_on_minutes(self):
        # 0.02 minutes = 1.2 s. A loop still sleeping hours * 3600 would not
        # produce a second call inside the deadline.
        refresher = self._refresher(0.02)
        refresher.start()
        self.assertGreaterEqual(self._wait_for_calls(self.calls, 2, 8.0), 2)
        self.assertLess(self.calls[1] - self.calls[0], 5.0)

    def test_set_interval_cuts_a_long_wait_short(self):
        refresher = self._refresher(60)
        refresher.start()
        # The first boot fetches immediately, then the loop parks for an hour.
        time.sleep(0.3)
        self.assertEqual(len(self.calls), 1)
        refresher.set_interval(0.02)
        self.assertGreaterEqual(self._wait_for_calls(self.calls, 2, 8.0), 2)

    def test_zero_parks_instead_of_fetching(self):
        refresher = self._refresher(0)
        refresher.start()
        time.sleep(0.4)
        self.assertEqual(self.calls, [])
        self.assertFalse(refresher.status()["enabled"])
        self.assertIsNone(refresher.next_run)

    def test_zero_then_a_value_resumes(self):
        refresher = self._refresher(0)
        refresher.start()
        time.sleep(0.2)
        self.assertEqual(self.calls, [])
        refresher.set_interval(0.02)
        self.assertGreaterEqual(self._wait_for_calls(self.calls, 1, 8.0), 1)


if __name__ == "__main__":
    unittest.main()
