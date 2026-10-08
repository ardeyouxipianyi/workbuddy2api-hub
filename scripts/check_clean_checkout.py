#!/usr/bin/env python3
"""Fail when a full suite run leaves the checkout dirty.

Every suite is supposed to keep its scratch state in a temporary directory, but
a suite that writes into the checkout instead is invisible: it passes, and the
next suite - or the next release - inherits a modified tree. Reviews kept
catching that by hand with `git status`; this turns that expectation into a CI
contract.

Git decides what "dirty" means, so the ignore rules stay authoritative. The
outputs that are intentionally persistent are not dirt: `suite-logs/` from
`run_all.py --logs`, `__pycache__/`, `accounts/*.json` and the rest of
`.gitignore`. What fails the guard is a modified tracked file, a deleted tracked
file, or a new file or directory that `.gitignore` does not cover.

    python scripts/check_clean_checkout.py

Exit status: 0 clean, 1 dirty, 2 the check could not run.

This assumes the checkout was clean before the suites ran, which is what
`actions/checkout` gives CI. Run over a developer's own working tree it reports
their own edits, which is the same answer with the same wording.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# core.quotePath=false prints a non-ASCII path as itself instead of as escaped
# octal, so the path in the log is one the reader can paste back into a shell.
STATUS = ("git", "-c", "core.quotePath=false", "status", "--porcelain")

# Porcelain's second column is the worktree state, which is what a suite can
# change; the first is the index. "??" is untracked and has no letter of its own.
KINDS = {
    "M": "modified",
    "D": "deleted",
    "A": "added",
    "R": "renamed",
    "C": "copied",
    "T": "type changed",
    "U": "unmerged",
}


def porcelain():
    """`git status --porcelain` for the repository, or None when it cannot run."""
    try:
        done = subprocess.run(STATUS, cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except OSError as exc:
        print("cannot run git: %s" % exc)
        return None
    if done.returncode != 0:
        print("git status failed with %d: %s"
              % (done.returncode, (done.stderr or "").strip()))
        return None
    return done.stdout


def describe(line):
    """One porcelain line as (kind, path)."""
    code, path = line[:2], line[3:]
    if code == "??":
        return "untracked", path
    return KINDS.get(code[1].strip() or code[0], "changed"), path


def main():
    out = porcelain()
    if out is None:
        return 2

    dirty = [line.rstrip() for line in out.splitlines() if line.strip()]
    if not dirty:
        print("checkout is clean: the suites left no repository-local state behind")
        return 0

    print("the suites left the checkout dirty - %d path(s):" % len(dirty))
    for line in dirty:
        kind, path = describe(line)
        print("  %-12s %s" % (kind, path))
    print("")
    print("A suite has to keep its scratch state in a temporary directory.")
    print("If an output is meant to persist, add it to .gitignore with a comment")
    print("saying why; do not delete files here to make this pass.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
