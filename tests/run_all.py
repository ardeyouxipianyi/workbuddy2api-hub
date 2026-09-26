"""Run every bundled suite and print one line per file.

    python tests/run_all.py            # everything
    python tests/run_all.py realm      # only suites whose name contains "realm"

Python suites run under the current interpreter; the JS suites need `node` on
PATH and are reported as skipped when it is missing. `_mobile_check.py` is not
part of this set: it drives the dashboard with Playwright/Firefox and is run by
hand.

Each suite's output goes to a temporary file rather than a pipe, so a suite that
spawns the gateway still sees a normal console and a failure can be shown with
its tail.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TAIL_LINES = 25


def suites(pattern):
    names = sorted(n for n in os.listdir(HERE)
                   if n.startswith("_test_") and n.endswith((".py", ".js")))
    return [n for n in names if pattern in n]


def command(name):
    path = os.path.join(HERE, name)
    if name.endswith(".js"):
        return ["node", path]
    return [sys.executable, path]


def tail(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = [line.rstrip() for line in fh if line.strip()]
    except OSError:
        return []
    return lines[-TAIL_LINES:]


def main(argv):
    if len(argv) > 1 and argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    pattern = argv[1] if len(argv) > 1 else ""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [ROOT] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    have_node = shutil.which("node") is not None

    passed, failed, skipped = [], [], []
    for name in suites(pattern):
        if name.endswith(".js") and not have_node:
            skipped.append(name)
            print("  [skip] %-38s node is not on PATH" % name)
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as log:
            log_path = log.name
        try:
            with open(log_path, "w", encoding="utf-8") as sink:
                result = subprocess.run(command(name), cwd=ROOT, env=env,
                                        stdout=sink, stderr=subprocess.STDOUT)
            lines = tail(log_path)
            ok = result.returncode == 0
            print("  [%s] %-38s %s"
                  % ("PASS" if ok else "FAIL", name, (lines[-1] if lines else "")[:80]))
            if ok:
                passed.append(name)
            else:
                failed.append(name)
                print("        --- last %d lines of %s ---" % (TAIL_LINES, name))
                for line in lines:
                    print("        " + line)
        finally:
            try:
                os.unlink(log_path)
            except OSError:
                pass

    print("")
    print("  %d passed, %d failed, %d skipped  (%s)"
          % (len(passed), len(failed), len(skipped), ROOT))
    if failed:
        print("  failed: %s" % ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
