"""run_all.py must survive a non-UTF-8 console when a suite prints Chinese.

The Windows CI runner's stdout is cp1252. Suites are executed with
PYTHONIOENCODING=utf-8 and their output is read back from a utf-8 log, but
run_all.py then re-prints the summary line itself - and that print used the
console encoding. The first Chinese summary aborted the whole run with
UnicodeEncodeError *after* every suite had already passed, which is why the
Windows job failed on main for a string of releases.

Run with: python _test_run_all_encoding.py
Runs one narrow suite as a subprocess; no network, no credentials.
"""
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RUN_ALL = os.path.join(HERE, "run_all.py")

# A suite that prints a Chinese summary, so the parent's own print() is the
# thing under test. Kept narrow so this stays a subprocess, not a full run.
NON_ASCII_SUITE = "settings_load"


class RunAllEncodingTests(unittest.TestCase):
    def _run(self, io_encoding):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = io_encoding
        return subprocess.run([sys.executable, RUN_ALL, NON_ASCII_SUITE],
                              cwd=ROOT, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=300)

    def test_survives_a_cp1252_console(self):
        """The regression: this used to exit non-zero with UnicodeEncodeError."""
        result = self._run("cp1252")
        output = result.stdout.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0,
                         "run_all.py failed under a cp1252 console:\n" + output)
        self.assertNotIn("UnicodeEncodeError", output)

    def test_survives_utf8_console(self):
        result = self._run("utf-8")
        self.assertEqual(result.returncode, 0,
                         result.stdout.decode("utf-8", "replace"))

    def test_reports_the_selected_suite(self):
        """Guard against the fix silently short-circuiting the run."""
        result = self._run("cp1252")
        output = result.stdout.decode("utf-8", "replace")
        self.assertIn(NON_ASCII_SUITE, output)
        self.assertIn("1 passed", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
