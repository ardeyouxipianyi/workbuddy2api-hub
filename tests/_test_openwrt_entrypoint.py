"""OpenWrt init script must start the proxy as a module, not as a script.

`python3 wb_proxy.py` runs the file as `__main__`; measured idle RSS is
47-54 MB higher than `python3 -m wb_proxy` on every platform sampled so far
(Windows 11 / aarch64, Python 3.12-3.14, issue #702). The package's init
script is the one launcher a router user cannot reasonably fix by hand, so
pin the entry mode and the PYTHONPATH wiring here.

`procd_set_param env` replaces the whole env list instead of appending to it,
so PYTHONPATH has to share a single line with the existing variables.

Run with: python tests/_test_openwrt_entrypoint.py
"""
import io
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
INIT = os.path.join(ROOT, "wrt", "openwrt", "workbuddy2api",
                    "files", "etc", "init.d", "workbuddy2api")


class OpenWrtEntrypointTests(unittest.TestCase):
    def setUp(self):
        with io.open(INIT, encoding="utf-8") as fh:
            self.text = fh.read()

    def test_starts_as_module(self):
        self.assertIn("-m wb_proxy", self.text)

    def test_does_not_pass_the_script_path(self):
        self.assertNotIn('python3 "$PROG"', self.text)

    def test_pythonpath_shares_the_single_env_line(self):
        env_lines = [line for line in self.text.splitlines()
                     if "procd_set_param env" in line]
        self.assertEqual(len(env_lines), 1)
        self.assertIn('PYTHONPATH="$PROG_DIR"', env_lines[0])
        self.assertIn("WB_STATS_TTL", env_lines[0])


if __name__ == "__main__":
    unittest.main()
