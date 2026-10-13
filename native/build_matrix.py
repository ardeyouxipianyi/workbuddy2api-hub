# -*- coding: utf-8 -*-
"""Build the engine for every platform / architecture the gateway runs on.

The engine is C99 plus one portability shim (`wbc_port.h`: monotonic clock,
process usage, `localtime_r`, binary stdout, DLL exports), so a single source
tree builds for all of them. This script only knows which compiler to call,
what to name the artifacts and how to smoke-test them.

    python native/build_matrix.py --list
    python native/build_matrix.py --target linux-x86_64 --target windows-x86_64
    python native/build_matrix.py --all --out native/dist
    python native/build_matrix.py --verify native/dist --log fixture/usage/usage.jsonl

Targets (the compiler has to be on PATH; the Debian/Ubuntu package names are
what the cross toolchains install):

    linux-x86_64        cc                          wb-agg, libwb_agg.so
    linux-i686          i686-linux-gnu-gcc          gcc-i686-linux-gnu
    linux-armv7         arm-linux-gnueabihf-gcc     gcc-arm-linux-gnueabihf
    linux-aarch64       aarch64-linux-gnu-gcc       gcc-aarch64-linux-gnu
    linux-x86_64-musl   x86_64-linux-musl-gcc       (OpenWrt SDK / musl.cc)
    linux-armv7-musl    armv7l-linux-musleabihf-gcc (musl.cc)
    linux-aarch64-musl  aarch64-linux-musl-gcc      (musl.cc)
    windows-x86_64      x86_64-w64-mingw32-gcc      gcc-mingw-w64-x86-64
    windows-i686        i686-w64-mingw32-gcc        gcc-mingw-w64-i686
    darwin-x86_64       clang -arch x86_64          (build on a Mac)
    darwin-arm64        clang -arch arm64           (build on a Mac)

The Linux and Windows CLIs are linked statically: copy the file to the target
machine and run it, nothing else is needed. The shared library only depends on
the system libc (musl or glibc, matching the toolchain it was built with), so
build it with the toolchain that matches the target's libc.

macOS: there is no legal cross toolchain, so those two entries are just the
command to run on a Mac (clang is there); the wrapper already looks for
libwb_agg.dylib.
"""
import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

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
FLAGS = ["-O2", "-std=c99", "-Wall", "-Wextra"]
# -lm 放在对象之后（静态链接时库的顺序有意义）
LIBS = ["-lm"]

# name -> compiler, artifact names, extra flags, and the qemu runner used to
# smoke-test a foreign-architecture build on a Linux host.
TARGETS = {
    "linux-x86_64": dict(cc="cc", cli="wb-agg", lib="libwb_agg.so",
                         cli_extra=["-static"], lib_extra=["-fPIC", "-shared", "-static-libgcc"],
                         qemu=None),
    "linux-i686": dict(cc="i686-linux-gnu-gcc", cli="wb-agg", lib="libwb_agg.so",
                       cli_extra=["-static"], lib_extra=["-fPIC", "-shared", "-static-libgcc"],
                       qemu="qemu-i386"),
    "linux-armv7": dict(cc="arm-linux-gnueabihf-gcc", cli="wb-agg", lib="libwb_agg.so",
                        cli_extra=["-static"], lib_extra=["-fPIC", "-shared", "-static-libgcc"],
                        qemu="qemu-arm"),
    "linux-aarch64": dict(cc="aarch64-linux-gnu-gcc", cli="wb-agg", lib="libwb_agg.so",
                          cli_extra=["-static"], lib_extra=["-fPIC", "-shared", "-static-libgcc"],
                          qemu="qemu-aarch64"),
    "linux-x86_64-musl": dict(cc="x86_64-linux-musl-gcc", cli="wb-agg", lib="libwb_agg.so",
                              cli_extra=["-static"], lib_extra=["-fPIC", "-shared", "-static-libgcc"],
                              qemu=None),
    "linux-armv7-musl": dict(cc="armv7l-linux-musleabihf-gcc", cli="wb-agg", lib="libwb_agg.so",
                             cli_extra=["-static"], lib_extra=["-fPIC", "-shared", "-static-libgcc"],
                             qemu="qemu-arm"),
    "linux-aarch64-musl": dict(cc="aarch64-linux-musl-gcc", cli="wb-agg", lib="libwb_agg.so",
                               cli_extra=["-static"], lib_extra=["-fPIC", "-shared", "-static-libgcc"],
                               qemu="qemu-aarch64"),
    "windows-x86_64": dict(cc="x86_64-w64-mingw32-gcc", cli="wb-agg.exe", lib="wb_agg.dll",
                           cli_extra=["-static"], lib_extra=["-shared", "-static-libgcc",
                                                             "-DWBC_BUILD_DLL"],
                           qemu=None),
    "windows-i686": dict(cc="i686-w64-mingw32-gcc", cli="wb-agg.exe", lib="wb_agg.dll",
                         cli_extra=["-static"], lib_extra=["-shared", "-static-libgcc",
                                                           "-DWBC_BUILD_DLL"],
                         qemu=None),
    "darwin-x86_64": dict(cc="clang -arch x86_64", cli="wb-agg", lib="libwb_agg.dylib",
                          cli_extra=[], lib_extra=["-fPIC", "-shared"], qemu=None),
    "darwin-arm64": dict(cc="clang -arch arm64", cli="wb-agg", lib="libwb_agg.dylib",
                         cli_extra=[], lib_extra=["-fPIC", "-shared"], qemu=None),
}


def sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def run(cmd, **kwargs):
    print("+ " + " ".join(shlex.quote(p) for p in cmd))
    return subprocess.check_call(cmd, **kwargs)


def build_target(name, out_root, keep_going=False):
    spec = TARGETS[name]
    cc = shlex.split(spec["cc"])
    if not shutil.which(cc[0]):
        print("  [skip] %s: %s not on PATH" % (name, cc[0]))
        return None
    out_dir = os.path.join(out_root, name)
    os.makedirs(out_dir, exist_ok=True)
    src = [os.path.join(HERE, s) for s in SOURCES]
    entry = {"target": name, "cc": spec["cc"], "artifacts": {}}
    for key, extra in (("cli", spec["cli_extra"]), ("lib", spec["lib_extra"])):
        out = os.path.join(out_dir, spec[key])
        try:
            run(cc + FLAGS + extra + ["-o", out] + src + LIBS)
        except subprocess.CalledProcessError as exc:
            # One broken toolchain must not hide the rest of the matrix: record
            # it and let the caller decide (the exit code is non-zero).
            entry["error"] = "%s failed with rc=%d" % (spec[key], exc.returncode)
            print("  [fail] %s: %s" % (name, entry["error"]))
            return entry
        entry["artifacts"][spec[key]] = {"sha256": sha256(out),
                                         "bytes": os.path.getsize(out)}
    return entry


def verify(out_root, log, accounts=None, pricing=None, reference="linux-x86_64"):
    """Run every built CLI over the same log and compare bytes with the reference.

    Only the sections that do not depend on the wall clock are asked for, and
    the window is pinned, so two runs are comparable; the meta block (timings,
    CPU, peak RSS) is stripped before the comparison.
    """
    args = ["--only", "snapshot,analytics,series,by_account,count",
            "--since", "0", "--until", "1e18", "--step", "86400"]
    if accounts:
        args += ["--accounts", accounts]
    if pricing:
        args += ["--pricing", pricing]
    ref_cli = os.path.join(out_root, reference, TARGETS[reference]["cli"])
    if not os.path.isfile(ref_cli):
        raise SystemExit("reference build missing: %s (build %s first)" % (ref_cli, reference))
    ref_out = os.path.join(out_root, "_reference.json")
    run([ref_cli, "--log", log, "--out", ref_out] + args)
    with open(ref_out, "rb") as fh:
        reference_bytes = fh.read()
    results = {}
    for name in sorted(os.listdir(out_root)):
        spec = TARGETS.get(name)
        if not spec:
            continue
        cli = os.path.join(out_root, name, spec["cli"])
        if not os.path.isfile(cli) or name == reference:
            continue
        runner = spec.get("qemu")
        if runner and not shutil.which(runner):
            results[name] = "skip (no %s)" % runner
            continue
        if cli.endswith(".exe"):
            results[name] = "skip (verify on Windows: run the exe, load the dll)"
            continue
        tmp = os.path.join(out_root, "_%s.json" % name.replace("/", "_"))
        cmd = ([runner] if runner else []) + [cli, "--log", log, "--out", tmp] + args
        try:
            run(cmd)
        except subprocess.CalledProcessError as exc:
            results[name] = "RUN FAILED rc=%d" % exc.returncode
            continue
        with open(tmp, "rb") as fh:
            got = fh.read()
        results[name] = "identical" if _strip_meta(got) == _strip_meta(reference_bytes) \
            else "DIFFERS"
    for name, verdict in sorted(results.items()):
        print("  %-22s %s" % (name, verdict))
    return results


def _strip_meta(payload):
    """Drop the meta block (timings/cpu/peak-rss differ per machine and run)."""
    try:
        doc = json.loads(payload.decode("utf-8"))
    except Exception:
        return payload
    doc.pop("meta", None)
    return json.dumps(doc, sort_keys=True, ensure_ascii=False).encode("utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="print the target table")
    parser.add_argument("--target", action="append", default=[], help="target name (repeatable)")
    parser.add_argument("--all", action="store_true", help="every target")
    parser.add_argument("--out", default=os.path.join(HERE, "dist"), help="output root")
    parser.add_argument("--verify", default=None, help="verify a built tree (needs --log)")
    parser.add_argument("--log", default=None, help="usage.jsonl for --verify")
    parser.add_argument("--accounts", default=None, help="accounts dir for --verify")
    parser.add_argument("--pricing", default=None, help="pricing.json for --verify")
    args = parser.parse_args()

    if args.list:
        for name in sorted(TARGETS):
            spec = TARGETS[name]
            print("  %-20s %-30s %s / %s" % (name, spec["cc"], spec["cli"], spec["lib"]))
        return 0

    if args.verify:
        if not args.log:
            parser.error("--verify needs --log")
        results = verify(args.verify, args.log, accounts=args.accounts,
                         pricing=args.pricing)
        return 1 if any(v.startswith(("RUN FAILED", "DIFFERS")) for v in results.values()) else 0

    targets = sorted(TARGETS) if args.all else (args.target or [])
    if not targets:
        parser.error("pick --target, --all or --list")
    # The host build is the reference the verifier compares against: build it
    # first so a broken cross toolchain cannot hide it.
    if "linux-x86_64" in targets:
        targets = ["linux-x86_64"] + [t for t in targets if t != "linux-x86_64"]
    entries = []
    failed = 0
    for name in targets:
        if name not in TARGETS:
            parser.error("unknown target %s (see --list)" % name)
        print("== %s ==" % name)
        entry = build_target(name, args.out)
        if entry:
            entries.append(entry)
            if entry.get("error"):
                failed += 1
    manifest = os.path.join(args.out, "manifest.json")
    os.makedirs(args.out, exist_ok=True)
    with open(manifest, "w", encoding="utf-8") as fh:
        json.dump({"targets": entries}, fh, ensure_ascii=False, indent=2)
    print("manifest: %s (%d targets, %d failed)" % (manifest, len(entries), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
