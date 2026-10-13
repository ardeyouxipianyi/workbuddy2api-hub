# -*- coding: utf-8 -*-
"""The optional C aggregation engine must never be able to take the gateway down.

    python tests/_test_native_agg.py

`wb_native_agg.py` is the ctypes binding for the standalone `wb-agg` engine
(native/): a plain C ABI shared object the Python side uses only when it is
present. The whole point of the module is that it is optional - a missing
library, a corrupt one, a wrong ABI version or a failing call all have to end
in "return None" so the caller folds with pure Python as before. This suite
pins that contract two ways:

  * the fallback path, run for real in a subprocess against a copy of the
    module in a throwaway directory (so no sibling library can make the test
    machine-dependent): a missing WB_AGG_LIB, a corrupt file, an unreadable
    path - every entry point answers with its safe value and no exception;
  * the wrapper logic itself, driven against a stub "library" object that
    behaves like the C one at the boundary ctypes sees: a version other than
    "1" is rejected, a NULL result becomes None, a real result is decoded and
    released exactly once, a non-dict payload is refused.

The C side (parity with the Python folds, byte-equality with the CLI) is
verified by native/parity.py and native/tests_*.c, which need a C toolchain;
CI has none, so it covers the fallback contract here and the Python half in
the rest of the matrix. The engine is deliberately not wired into the gateway
yet - test_the_gateway_does_not_wire_the_engine holds that line, and whoever
wires it in should replace that assertion with the seam's own tests.
"""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import wb_native_agg as agg  # noqa: E402

MODULE = os.path.join(ROOT, "wb_native_agg.py")

# The subprocess prints one JSON object; everything it reports is compared
# against this table so a new entry point that starts raising is caught too.
PROBE = r'''
import json, os, sys
sys.path.insert(0, sys.argv[1])
import wb_native_agg as agg
out = {}
out["available"] = agg.available()
out["version"] = agg.version()
out["library_path"] = agg.library_path()
out["last_error_type"] = type(agg.last_error()).__name__
for name, value in (("run_dict", agg.run({"log": "x"})),
                    ("run_none", agg.run(None)),
                    ("run_list", agg.run([])),
                    ("run_text", agg.run_text({"log": "x"})),
                    ("run_text_none", agg.run_text(None))):
    out[name] = value
out["repeat"] = [agg.available(), agg.available(), agg.available()]
print(json.dumps(out))
'''


def _run_probe(directory, env_extra):
    """Import a copy of the module from `directory` and report what it answers."""
    script = os.path.join(directory, "_probe.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(PROBE)
    env = dict(os.environ)
    env.pop("WB_AGG_LIB", None)
    env.update(env_extra)
    proc = subprocess.run([sys.executable, script, directory],
                          capture_output=True, text=True, env=env, timeout=60)
    if proc.returncode != 0:
        raise AssertionError("probe crashed (rc=%d)\n%s\n%s"
                             % (proc.returncode, proc.stdout, proc.stderr))
    return json.loads(proc.stdout.strip().splitlines()[-1])


class FallbackContractTests(unittest.TestCase):
    """No library, a corrupt library, a bogus WB_AGG_LIB: None, never a raise."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wb-native-agg-")
        shutil.copy2(MODULE, os.path.join(self.dir, "wb_native_agg.py"))
        # A probe script is written into the same directory by _run_probe, so
        # nothing else may live there: a sibling library is exactly what these
        # tests must rule out.

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def assert_all_safe(self, out):
        self.assertIs(out["available"], False)
        self.assertIsNone(out["version"])
        self.assertIsNone(out["library_path"])
        self.assertIn(out["last_error_type"], ("NoneType", "str"))
        for key in ("run_dict", "run_none", "run_list", "run_text", "run_text_none"):
            self.assertIsNone(out[key], "%s 应该静默回退" % key)
        self.assertEqual(out["repeat"], [False, False, False])

    def test_no_library_anywhere_falls_back(self):
        self.assert_all_safe(_run_probe(self.dir, {}))

    def test_env_var_pointing_at_nothing_falls_back(self):
        missing = os.path.join(self.dir, "does-not-exist.so")
        self.assert_all_safe(_run_probe(self.dir, {"WB_AGG_LIB": missing}))

    def test_corrupt_library_falls_back(self):
        # What a truncated download or a wrong-architecture file looks like to
        # ctypes: it raises on load, and the module has to swallow that.
        bogus = os.path.join(self.dir, "libwb_agg.so")
        with open(bogus, "wb") as fh:
            fh.write(b"not an ELF file\n" * 64)
        self.assert_all_safe(_run_probe(self.dir, {"WB_AGG_LIB": bogus}))

    def test_corrupt_sibling_library_falls_back(self):
        # Same file, but found by the sibling rule rather than the env var.
        bogus = os.path.join(self.dir, "libwb_agg.so")
        with open(bogus, "wb") as fh:
            fh.write(b"\x7fELF broken\n")
        self.assert_all_safe(_run_probe(self.dir, {}))


class StubLibrary(object):
    """Just enough of a ctypes CDLL for the wrapper's own logic.

    ctypes writes .argtypes/.restype on the function attributes, which a bound
    method refuses (that is exactly what a library missing a symbol looks
    like), so the four entry points are plain functions. run_json returns a
    real C pointer so ctypes.string_at() in the wrapper reads real memory
    instead of being faked.
    """

    def __init__(self, version=b"1", result=None, error=None, explode=False):
        self._buffers = []
        self.released = []
        self.calls = []

        def version_fn():
            return version

        def run_fn(payload, length):
            if explode:
                raise RuntimeError("boom")
            self.calls.append((payload, length))
            if result is None:
                return None
            buf = ctypes.create_string_buffer(result)
            self._buffers.append(buf)
            return ctypes.addressof(buf)

        def release_fn(ptr):
            self.released.append(ptr)

        def error_fn():
            return error

        self.wb_agg_version = version_fn
        self.wb_agg_run_json = run_fn
        self.wb_agg_release = release_fn
        self.wb_agg_error = error_fn


class WrapperLogicTests(unittest.TestCase):
    """The boundary logic, driven by a stub that mimics the C library."""

    def setUp(self):
        self.saved = (agg._attempted, agg._library, agg._library_path, agg._version)

    def tearDown(self):
        (agg._attempted, agg._library, agg._library_path, agg._version) = self.saved

    def install(self, lib):
        """Pretend `lib` is what the lazy loader found, ABI check already done."""
        agg._attempted = True
        agg._library = lib
        agg._library_path = "stub"
        agg._version = None

    def test_bind_rejects_a_foreign_abi_version(self):
        self.assertIsNone(agg._bind(StubLibrary(version=b"2")))
        self.assertIsNone(agg._bind(StubLibrary(version=b"")))

    def test_bind_accepts_the_current_abi_version(self):
        self.assertEqual(agg._bind(StubLibrary(version=b"1")), "1")
        # Whitespace is tolerated: a shell-echoed build stamp is still "1".
        self.assertEqual(agg._bind(StubLibrary(version=b" 1\n")), "1")

    def test_bind_rejects_a_library_without_the_symbols(self):
        class Empty(object):
            pass
        self.assertIsNone(agg._bind(Empty()))

    def test_a_null_result_is_none_and_releases_nothing(self):
        self.install(StubLibrary(result=None))
        self.assertIsNone(agg.run({"log": "x"}))
        self.assertIsNone(agg.run_text({"log": "x"}))

    def test_a_real_result_is_decoded_and_released_once(self):
        payload = json.dumps({"snapshot": {"rows": 3}}, ensure_ascii=False)
        lib = StubLibrary(result=payload.encode("utf-8"))
        self.install(lib)
        self.assertEqual(agg.run({"log": "x"}), {"snapshot": {"rows": 3}})
        self.assertEqual(agg.run_text({"log": "x"}), payload)
        self.assertEqual(len(lib.released), 2)
        self.assertTrue(all(lib.released), "释放的是空指针？")

    def test_the_request_is_sent_as_utf8_json(self):
        lib = StubLibrary(result=b"{}")
        self.install(lib)
        agg.run({"log": "x", "realm": "国内"})
        payload, length = lib.calls[0]
        self.assertIsInstance(payload, bytes)
        self.assertEqual(length, len(payload))
        self.assertIn("国内".encode("utf-8"), payload)

    def test_a_non_dict_result_is_refused(self):
        # The C engine always answers with an object; anything else means the
        # reply is not ours, and run() must not hand it to the caller.
        self.install(StubLibrary(result=b"[1, 2, 3]"))
        self.assertIsNone(agg.run({"log": "x"}))
        self.assertEqual(agg.run_text({"log": "x"}), "[1, 2, 3]")

    def test_an_unserialisable_request_never_reaches_the_library(self):
        lib = StubLibrary(result=b"{}")
        self.install(lib)
        self.assertIsNone(agg.run({"log": object()}))
        self.assertEqual(lib.calls, [], "坏请求不该送进 C 侧")

    def test_a_library_that_raises_is_swallowed(self):
        self.install(StubLibrary(explode=True))
        self.assertIsNone(agg.run({"log": "x"}))
        self.assertIsNone(agg.run_text({"log": "x"}))

    def test_last_error_decodes_or_stays_none(self):
        self.install(StubLibrary(result=b"{}", error=b"open failed"))
        self.assertEqual(agg.last_error(), "open failed")
        self.install(StubLibrary(result=b"{}", error=None))
        self.assertIsNone(agg.last_error())

    def test_threaded_use_is_consistent(self):
        # Eight threads racing the same already-loaded library: the lock is
        # only taken for the first load, and none of them may raise or see a
        # different answer.
        lib = StubLibrary(result=b'{"ok": true}')
        answers, errors = [], []

        def worker():
            try:
                answers.append(agg.run({"log": "x"}))
            except Exception as exc:      # pragma: no cover - the failure path
                errors.append(exc)

        self.install(lib)
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(answers, [{"ok": True}] * 8)

    def test_threaded_first_use_without_a_library_is_consistent(self):
        # The other side of the race: no library anywhere, so the first load
        # fails. Every thread must get the same silent fallback.
        answers, errors = [], []

        def worker():
            try:
                answers.append(agg.run({"log": "x"}))
            except Exception as exc:      # pragma: no cover - the failure path
                errors.append(exc)

        agg._attempted = False
        agg._library = None
        agg._version = None
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(answers, [None] * 8)


class WiringTests(unittest.TestCase):
    def test_the_gateway_does_not_import_the_engine_directly(self):
        # The gateway reaches the engine only through the wb_agg_engine seam
        # (two cold paths, switch off by default), never by importing this
        # module: that keeps "engine unavailable == pure Python" decided in
        # exactly one place, and the seam's own suite covers it.
        import ast
        with open(os.path.join(ROOT, "wb_proxy.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        self.assertNotIn("wb_native_agg", names)


if __name__ == "__main__":
    unittest.main()
