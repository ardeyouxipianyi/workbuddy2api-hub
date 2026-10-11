#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The portable runtime the release ZIP ships (issue #28).

The existing portable asset is not a source archive: it carries a trimmed
CPython 3.12 Windows runtime under ``python/``, and the launchers pick that up
before falling back to a system interpreter (``%HERE%python\\python.exe`` in
``start-wb-proxy.bat``). The repository cannot rebuild it - it is gitignored
precisely because it is a build input - so CI fetches it from a pinned
python-build-standalone release and trims it with the rules below.

Those rules are not invented. The ``python/`` tree of the previously shipped
package is a strict subset of the pinned build, and the sets below are exactly
the difference between the two: same ``Lib/`` top level, same ``DLLs/``, same
root files. ``Lib/multiprocessing`` is part of that difference, and the README
documents the consequence ("绿色包那份精简 Python 里根本没有这个模块，用它会直接
报错"), so dropping it is the shipped behaviour rather than an accident.

    python release/runtime.py prepare --tarball <tar.gz> --dest python
    python release/runtime.py verify  --root python
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tarfile

# ---- pinned release input -------------------------------------------------
#
# A release asset must be reproducible from the tag alone, so the runtime is a
# pinned download with a checksum, not "whatever the maintainer had locally".
# Bumping it means bumping all four constants together; `prepare` refuses a
# tarball whose sha256 does not match.
RUNTIME_VERSION = "3.12.15"
RUNTIME_RELEASE = "20261003"
RUNTIME_ASSET = "cpython-3.12.15+20261003-x86_64-pc-windows-msvc-install_only.tar.gz"
RUNTIME_SHA256 = "4b6f0beebbb695a0f3ea237b8c3eaa5bd424f47a7bc25b2fbe3a43390c770f08"
RUNTIME_URL = ("https://github.com/astral-sh/python-build-standalone/releases/download/"
               "%s/%s" % (RUNTIME_RELEASE, RUNTIME_ASSET))

# ---- trim rules -----------------------------------------------------------

# Directories next to python.exe that the shipped package does not carry:
# headers, import libs, the tcl/tk runtime and the pip entry-point scripts.
DROP_TOP_DIRS = ("Scripts", "include", "libs", "tcl")

# Stdlib packages the shipped package does not carry. multiprocessing is
# deliberate (see the module docstring); the rest are development extras.
DROP_LIB_DIRS = ("__pycache__", "ensurepip", "idlelib", "lib2to3", "multiprocessing",
                 "pydoc_data", "site-packages", "tkinter", "turtledemo", "venv")

# Extension modules the shipped package does not carry: the CPython test
# extensions, the tk bindings, their tcl/tk DLLs, and zlib1.dll (CPython 3.12
# links zlib into python312.dll, so the DLL is a leftover of the build).
DROP_DLLS = ("_ctypes_test.pyd", "_testbuffer.pyd", "_testcapi.pyd", "_testclinic.pyd",
             "_testconsole.pyd", "_testimportmultiple.pyd", "_testinternalcapi.pyd",
             "_testmultiphase.pyd", "_testsinglephase.pyd", "_tkinter.pyd",
             "tcl86t.dll", "tk86t.dll", "zlib1.dll")

DROP_ROOT_FILES = ("pythonw.exe",)

# Everything with these extensions goes, wherever it lives: the build ships
# debug symbols for every binary, and stale bytecode caches.
DROP_SUFFIXES = (".pdb", ".pyc")

# ---- what the package must be able to do ----------------------------------
#
# The launchers look for python/python.exe, and the gateway imports these
# modules (ast-walked from wb_*.py) - so the trim may not take any of them,
# and their extension modules have to survive with them.
REQUIRED_FILES = (
    "python.exe", "python3.dll", "python312.dll",
    "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt",
    "Lib/os.py", "Lib/io.py", "Lib/ssl.py", "Lib/hashlib.py", "Lib/hmac.py",
    "Lib/socket.py",
    "Lib/json/__init__.py", "Lib/urllib/__init__.py", "Lib/urllib/request.py",
    "Lib/uuid.py", "Lib/secrets.py",
    "Lib/argparse.py", "Lib/base64.py", "Lib/bisect.py", "Lib/datetime.py",
    "Lib/difflib.py", "Lib/fnmatch.py", "Lib/getpass.py", "Lib/ipaddress.py",
    "Lib/queue.py", "Lib/shutil.py",
    "Lib/subprocess.py", "Lib/threading.py",
    "Lib/html/__init__.py", "Lib/http/__init__.py", "Lib/http/client.py",
    "Lib/concurrent/__init__.py", "Lib/concurrent/futures/__init__.py",
    "Lib/ctypes/__init__.py", "Lib/collections/__init__.py", "Lib/re/__init__.py",
    "DLLs/_socket.pyd", "DLLs/_ssl.pyd", "DLLs/_hashlib.pyd", "DLLs/_ctypes.pyd",
    "DLLs/select.pyd", "DLLs/unicodedata.pyd", "DLLs/_uuid.pyd", "DLLs/_decimal.pyd",
    "DLLs/libffi-8.dll", "DLLs/libssl-3-x64.dll", "DLLs/libcrypto-3-x64.dll",
)

# Absent from the shipped package, so they must stay absent here too.
FORBIDDEN_PATHS = (
    "Scripts", "include", "libs", "tcl", "pythonw.exe",
    "Lib/multiprocessing", "Lib/tkinter", "Lib/idlelib", "Lib/lib2to3",
    "Lib/ensurepip", "Lib/site-packages", "Lib/venv", "Lib/turtledemo",
    "Lib/pydoc_data", "DLLs/_tkinter.pyd", "DLLs/tcl86t.dll", "DLLs/tk86t.dll",
    "DLLs/zlib1.dll",
)


def sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def keep(rel):
    """Should this member of the pinned build survive the trim?

    `rel` is relative to the runtime root and uses forward slashes.
    """
    parts = rel.split("/")
    if len(parts) > 1 and parts[0] in DROP_TOP_DIRS:
        return False
    if parts[0] in DROP_ROOT_FILES:
        return False
    if any(p in DROP_LIB_DIRS for p in parts[1:]):
        return False
    if len(parts) == 2 and parts[0] == "DLLs" and parts[1] in DROP_DLLS:
        return False
    return not rel.endswith(DROP_SUFFIXES)


def prepare(tarball, dest, expect_sha=RUNTIME_SHA256):
    """Extract the pinned build into `dest`, dropping what the trim removes.

    Members are filtered while reading, so the dropped ~110 MB never lands on
    disk. Returns (kept_files, kept_dirs).
    """
    got = sha256(tarball)
    if expect_sha and got != expect_sha:
        raise SystemExit("运行时校验和不符：\n  期望 %s\n  实际 %s\n（%s）"
                         % (expect_sha, got, tarball))
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)

    files = dirs = 0
    with tarfile.open(tarball, "r:gz") as tf:
        for member in tf:
            name = member.name.lstrip("./")
            if not name or name == "python":
                continue
            rel = name[len("python/"):] if name.startswith("python/") else name
            if not rel or not keep(rel):
                continue
            target = os.path.join(dest, *rel.split("/"))
            if member.isdir():
                os.makedirs(target, exist_ok=True)
                dirs += 1
                continue
            if not member.isfile():
                continue  # symlinks do not exist in the Windows build
            os.makedirs(os.path.dirname(target), exist_ok=True)
            src = tf.extractfile(member)
            with open(target, "wb") as fh:
                shutil.copyfileobj(src, fh)
            os.chmod(target, 0o755 if rel.endswith(".exe") else 0o644)
            files += 1
    return files, dirs


def verify(root):
    """Raise SystemExit unless `root` is a runtime the launchers can use."""
    problems = []
    for rel in REQUIRED_FILES:
        if not os.path.isfile(os.path.join(root, *rel.split("/"))):
            problems.append("缺少 %s" % rel)
    for rel in FORBIDDEN_PATHS:
        if os.path.exists(os.path.join(root, *rel.split("/"))):
            problems.append("不该出现 %s（已发布的精简运行时里没有它）" % rel)
    stray = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith(DROP_SUFFIXES):
                stray.append(os.path.relpath(os.path.join(dirpath, fn), root))
    if stray:
        problems.append("残留调试符号/字节码：%s%s"
                        % (", ".join(sorted(stray)[:5]),
                           " 等 %d 个" % len(stray) if len(stray) > 5 else ""))
    if problems:
        raise SystemExit("运行时不合格（%s）：\n  - %s" % (root, "\n  - ".join(problems)))
    return len(REQUIRED_FILES)


def _utf8_stdio():
    """Let the progress lines survive a non-UTF-8 console.

    Windows runners hand Python a cp1252 stdout, and the first Chinese summary
    line then dies with UnicodeEncodeError. The repository already paid for this
    once in tests/run_all.py; the same two lines fix it here.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def info():
    """KEY=VALUE lines, so the workflow reads the pin from one place."""
    return [
        "RUNTIME_PYTHON=%s" % RUNTIME_VERSION,
        "RUNTIME_ASSET=%s" % RUNTIME_ASSET,
        "RUNTIME_URL=%s" % RUNTIME_URL,
        "RUNTIME_SHA256=%s" % RUNTIME_SHA256,
    ]


def main(argv=None):
    _utf8_stdio()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("info", help="print the pinned runtime as KEY=VALUE lines")

    p = sub.add_parser("prepare", help="extract + trim the pinned runtime")
    p.add_argument("--tarball", required=True)
    p.add_argument("--dest", required=True)
    p.add_argument("--sha256", default=RUNTIME_SHA256,
                   help="expected checksum ('' to skip, for local experiments)")

    v = sub.add_parser("verify", help="check a runtime against the launcher contract")
    v.add_argument("--root", required=True)

    args = ap.parse_args(argv)

    if args.cmd == "info":
        for line in info():
            print(line)
        return 0

    if args.cmd == "prepare":
        files, dirs = prepare(args.tarball, args.dest, args.sha256 or None)
        print("runtime: %s（%d 个文件 / %d 个目录，Python %s）"
              % (args.dest, files, dirs, RUNTIME_VERSION))
        verify(args.dest)
        print("runtime: 契约检查通过（%d 个必需文件都在，%d 个已裁项都没回来）"
              % (len(REQUIRED_FILES), len(FORBIDDEN_PATHS)))
        return 0

    if args.cmd == "verify":
        verify(args.root)
        print("runtime ok: %s" % args.root)
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
