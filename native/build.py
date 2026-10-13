# -*- coding: utf-8 -*-
"""Build the optional C aggregation engine (wb-agg) and its shared-library form.

The gateway itself is pure Python and stays that way: this engine is an
optional accelerator, nothing in the release pipeline requires it, and the
Python side only uses it when the shared object is present (wb_native_agg.py).

    python native/build.py              # CLI + shared library + C self-tests
    python native/build.py --test       # ... and run the C self-tests
    python native/build.py --static     # static CLI (musl/alpine, no runtime deps)
    python native/build.py --cc clang   # pick a compiler (default: $CC, then cc)

Outputs (all written next to this script):

    wb-agg / wb-agg.exe           standalone CLI: request JSON -> result JSON
    libwb_agg.so                  the same engine as a shared object, loaded
    (wb_agg.dll / libwb_agg.dylib) by wb_native_agg.py over the C ABI below
    tests_json, tests_pricing, probe_round   the C self-tests

The C ABI the Python side binds (see wbc_api.h):

    const char *wb_agg_version(void);
    char *wb_agg_run_json(const char *request, size_t len);
    void wb_agg_release(char *result);
    const char *wb_agg_error(void);

The build needs a C99 compiler and nothing else: no third-party libraries and
no Python headers - the shared object is a plain C ABI and ctypes does the
binding.
"""
import argparse
import os
import shlex
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# Every translation unit of the engine. main.c is in the shared library too:
# its `main` symbol is harmless there and the library caller goes through
# engine_run() / wb_agg_run_json().
SOURCES = [
    "main.c",
    "wbc_api.c",
    "wbc_folds_analytics.c",
    "wbc_folds_core.c",
    "wbc_json.c",
    "wbc_perf.c",
    "wbc_pricing.c",
    "wbc_remaining.c",
    "wbc_row.c",
    "wbc_util.c",
]
# Test programs: each has its own main(), so they stay out of the library.
SELF_TESTS = {
    "tests_json": ["tests_json.c", "wbc_json.c", "wbc_util.c"],
    "tests_pricing": ["tests_pricing.c", "wbc_pricing.c", "wbc_json.c",
                      "wbc_util.c"],
    "probe_round": ["probe_round.c", "wbc_util.c"],
}
FLAGS = ["-O2", "-std=c99", "-Wall", "-Wextra"]
# -static-libgcc on the shared object: without it a musl build records
# DT_NEEDED = libgcc_s.so.1, libc.so; with it the .so needs the system libc
# and nothing else (verified with an ELF parse of both builds - the CLI is
# fully static either way: no PT_INTERP, no DT_NEEDED at all).
LIB_FLAGS = ["-fPIC", "-shared"] + ([] if sys.platform == "darwin" else ["-static-libgcc"])


def lib_name():
    if os.name == "nt":
        return "wb_agg.dll"
    if sys.platform == "darwin":
        return "libwb_agg.dylib"
    return "libwb_agg.so"


def run(cmd, cwd=None):
    print("+ " + " ".join(shlex.quote(part) for part in cmd))
    subprocess.check_call(cmd, cwd=cwd)


def build(cc, static=False, test=False):
    src = [os.path.join(HERE, name) for name in SOURCES]

    cli = os.path.join(HERE, "wb-agg.exe" if os.name == "nt" else "wb-agg")
    run(cc + FLAGS + (["-static"] if static else []) + ["-o", cli] + src)

    lib = os.path.join(HERE, lib_name())
    run(cc + FLAGS + LIB_FLAGS + ["-o", lib] + src)

    for name, parts in sorted(SELF_TESTS.items()):
        exe = os.path.join(HERE, name + (".exe" if os.name == "nt" else ""))
        run(cc + FLAGS + ["-o", exe] + [os.path.join(HERE, p) for p in parts])
        if test:
            # tests_pricing writes its fixture files into the CWD; give it a
            # throwaway directory so a build tree never picks up stray files.
            with tempfile.TemporaryDirectory(prefix="wb-agg-selftest-") as tmp:
                run([exe], cwd=tmp)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cc", default=os.environ.get("CC") or "cc",
                        help="C compiler command (default: $CC, then cc)")
    parser.add_argument("--static", action="store_true",
                        help="link the CLI statically (musl: no runtime deps)")
    parser.add_argument("--test", action="store_true",
                        help="run the C self-tests after building them")
    args = parser.parse_args()
    build(shlex.split(args.cc), static=args.static, test=args.test)
    return 0


if __name__ == "__main__":
    sys.exit(main())
