"""Contract tests for tests/run_all.py - the runner's reliability promises.

run_all.py owns behavior no single suite can check for itself: a per-suite wall
clock, tree cleanup when that clock runs out, a failure line that names the real
error instead of a bare "Node.js v20.20.2", log retention, argument validation,
and "a pattern that matches nothing is not a pass". Today only the console
encoding failure mode has dedicated coverage (_test_run_all_encoding.py), so
every other promise rests on the full CI matrix noticing indirectly.

These tests drive the real runner as a subprocess against throwaway probe
suites, so each contract is exercised end to end. Nothing is mocked: the failing
probe really exits non-zero, and the hanging probe really has to be killed.

Isolation: the runner discovers suites next to itself (os.listdir(HERE)), so a
byte-identical copy of run_all.py is placed in a temporary directory with the
probe suites beside it. Nothing is ever written into the repository's tests/
directory, and the copy is compared against the real file first, so these tests
cannot silently drift away from the code they cover.

Cleanup never asks the code under test for help. The thing under test is exactly
what may be broken, so the harness starts every sandbox runner in a group it
owns, keeps the pids it may have to kill outside the disposable scenario
directory, and terminates the whole tree through the OS (TerminateProcess /
SIGKILL) rather than through taskkill - a locked-down host refuses that just as
readily as it refuses the runner's own call.

Run with: python tests/_test_run_all_contracts.py
No network, no credentials. Every probe file, process and log directory this
suite creates is removed in a finally block.
"""
import ctypes
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
REAL_RUNNER = os.path.join(HERE, "run_all.py")

#: A passing probe writes more lines than the runner keeps in its tail, so the
#: --logs assertion can tell "the full output" apart from "the last 25 lines".
FILLER_LINES = 40

#: Bounds are deliberately loose: they only have to separate "bounded by
#: --timeout" from "waited for the probe", which sleeps for ten minutes.
HANG_TIMEOUT = 5
HANG_SECONDS = 600
HANG_BUDGET = 60

#: The bound the harness itself puts on a sandbox runner, far below
#: HANG_SECONDS on purpose: the harness must never wait for a runner that has
#: stopped honouring its own wall clock.
SANDBOX_BUDGET = 15

#: The grandchild outlives the check but not the test session, so a host that
#: refuses the tree kill cannot leave a ten-minute orphan behind.
ORPHAN_SECONDS = 120

#: The line in run_all.py that applies the per-suite wall clock. The negative
#: harness check removes it from its own copy, so the runner under test stops
#: honouring --timeout and only the harness's own bound can end the run.
TIMEOUT_ANCHOR = "code = proc.wait(timeout=timeout)"

_WINDOWS = os.name == "nt"

if _WINDOWS:
    _KERNEL32 = ctypes.windll.kernel32
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_TERMINATE = 0x0001
    _STILL_ACTIVE = 259
    _TH32CS_SNAPPROCESS = 0x2

    class _PROCESSENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_char * 260)]

    def _process_tree():
        """{pid: parent pid} straight from the OS - no tasklist, no taskkill."""
        snapshot = _KERNEL32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        table = {}
        entry = _PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        ok = _KERNEL32.Process32First(snapshot, ctypes.byref(entry))
        while ok:
            table[entry.th32ProcessID] = entry.th32ParentProcessID
            ok = _KERNEL32.Process32Next(snapshot, ctypes.byref(entry))
        _KERNEL32.CloseHandle(snapshot)
        return table

    def alive(pid):
        """Liveness through the OS, not tasklist.

        A locked-down host answers "Access denied" to tasklist and prints
        nothing, and a helper that greps that output then calls every pid dead.
        OpenProcess/GetExitCodeProcess answers about the process itself.
        """
        handle = _KERNEL32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION,
                                       False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not _KERNEL32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            _KERNEL32.CloseHandle(handle)

    def terminate_pid(pid):
        """Kill one pid through the OS, with no shell-out to taskkill.

        taskkill is what the runner uses, and the host that refuses it refuses
        the harness too; the last line of defence cannot be the same call.
        """
        handle = _KERNEL32.OpenProcess(_PROCESS_TERMINATE, False, pid)
        if not handle:
            return False
        try:
            return bool(_KERNEL32.TerminateProcess(handle, 1))
        finally:
            _KERNEL32.CloseHandle(handle)
else:
    def _process_tree():
        """{pid: parent pid} from /proc, which is where Linux CI lives."""
        return _proc_process_tree()

    def alive(pid):
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def terminate_pid(pid):
        try:
            os.kill(pid, signal.SIGKILL)
            return True
        except OSError:
            return False


def _parse_proc_stat(data):
    """The parent pid out of one /proc/<pid>/stat line.

    Split on the *last* ')' rather than on whitespace: the command name sits in
    parentheses and may itself contain spaces and parentheses, and everything
    after it is fixed-position (state, then ppid).
    """
    rest = data.rsplit(b")", 1)[1].split()
    return int(rest[1])


def _proc_process_tree(proc_root="/proc"):
    """{pid: parent pid} from a /proc-shaped tree.

    Parameterised on the root so the Linux branch can be exercised from any
    platform against a synthetic tree, instead of only ever failing on CI. The
    shape has to match the Windows branch: _descendants() reads one mapping and
    must not have to know which platform filled it in.
    """
    try:
        # Sorted, not raw listdir order: readdir is arbitrary on Linux (hash
        # order), so an unsorted walk makes _descendants' sibling order depend
        # on the filesystem. That passed on Windows and flaked on CI.
        names = sorted((n for n in os.listdir(proc_root) if n.isdigit()), key=int)
    except OSError:
        return None
    table = {}
    for name in names:
        try:
            with open(os.path.join(proc_root, name, "stat"), "rb") as fh:
                table[int(name)] = _parse_proc_stat(fh.read())
        except (OSError, IndexError, ValueError):
            continue
    return table


def _descendants(root, table=None):
    """Every pid below root, deepest first, with root itself last.

    Windows keeps the creator's pid in a child's parent field even after that
    parent exits, so the walk still finds an orphan whose parent is already
    gone. On POSIX this needs /proc; without it the caller's group kill is the
    only tree mechanism, which is why probes register their own pids as well.

    `table` is a {pid: parent pid} mapping and exists so the traversal can be
    tested without the platform it happens to be running on.
    """
    if table is None:
        table = _process_tree()
    if table is None:
        return [root]
    children = {}
    for pid, parent in table.items():
        children.setdefault(parent, []).append(pid)
    levels, frontier, seen = [], [root], {root}
    while frontier:
        levels.append(frontier)
        nxt = []
        for pid in frontier:
            for child in children.get(pid, []):
                if child not in seen:
                    seen.add(child)
                    nxt.append(child)
        frontier = nxt
    order = []
    for level in reversed(levels):
        order.extend(level)
    return order


def new_group_kwargs():
    """Start a child in its own group/session.

    Used twice for the same reason: the harness owns the sandbox runner so it
    can take that runner's tree back, and run_all.py gives every suite its own
    group for exactly the same purpose.
    """
    if _WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_owned_tree(proc):
    """Terminate a harness-owned runner and everything under it, then reap.

    Deliberately independent of the code under test and of taskkill: this is
    the path that has to work when the runner has stopped honouring its own
    timeout, and when the host refuses the runner's own tree kill.

    It also never raises. It runs on the path where the run has already failed,
    and an exception here would skip the rest of the cleanup and leave exactly
    the orphans this function exists to remove.
    """
    if proc.poll() is not None:
        return
    try:
        if not _WINDOWS:
            # The runner leads its own session, so its group catches anything
            # that did not detach into a group of its own.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                pass
        for pid in _descendants(proc.pid):
            terminate_pid(pid)
    except Exception:
        pass
    terminate_pid(proc.pid)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def wait_gone(pid, seconds=10.0):
    """True once the pid is gone; a killed process is not gone instantly."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.2)
    return not alive(pid)


def read_pids(path, seconds=15.0):
    """Read a probe's registered pids, waiting for the file to appear."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            time.sleep(0.1)
    raise AssertionError("the probe never registered its pids in %s" % path)


#: A parent that starts a grandchild, registers both pids and then waits. Used
#: both as the hanging probe and, unmodified, to ask the host what it permits.
#: The grandchild is given a working directory outside the scenario tree, so a
#: host that cannot kill it still leaves the scenario directory removable.
TREE_PROBE = (
    "import json, os, subprocess, sys, tempfile, time\n"
    "child = subprocess.Popen([sys.executable, '-I', '-c',\n"
    "    'import time; time.sleep(%d)'], cwd=tempfile.gettempdir())\n"
    "with open(os.environ['PROBE_PID_FILE'], 'w', encoding='utf-8') as fh:\n"
    "    json.dump({'parent': os.getpid(), 'child': child.pid}, fh)\n"
    "print('probe started', flush=True)\n"
    "time.sleep(%d)\n"
)


def host_can_kill_trees():
    """Ask this host whether the runner's tree kill actually works on it.

    Returns (can, reason). The probe repeats exactly what run_all.kill_tree()
    does - taskkill /F /T on Windows, killpg elsewhere - against a throwaway
    parent/grandchild pair. A locked-down host refuses it (taskkill answers
    "Access denied"), and the runner then falls back to killing only the direct
    child; the caller has to know which of the two it is looking at, otherwise a
    refused kill reads as a passing cleanup.

    The probe's own cleanup does not use taskkill: that is the call being
    measured, and it is the call that may be refused.
    """
    env = dict(os.environ)
    handle, pid_file = tempfile.mkstemp(prefix="runall-tree-", suffix=".json")
    os.close(handle)
    os.unlink(pid_file)
    env["PROBE_PID_FILE"] = pid_file
    # The probe runs with the shared temp directory as cwd. A stray module
    # dropped there (for example an unrelated `bisect.py`) would shadow the
    # stdlib on sys.path and the probe would die before registering its pids.
    # -I keeps cwd out of sys.path without changing what the probe measures.
    proc = subprocess.Popen([sys.executable, "-I", "-c",
                             TREE_PROBE % (ORPHAN_SECONDS, ORPHAN_SECONDS)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            cwd=tempfile.gettempdir(), env=env,
                            **new_group_kwargs())
    grand = None
    try:
        grand = read_pids(pid_file)["child"]

        permitted = False
        if _WINDOWS:
            done = subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=30)
            permitted = done.returncode == 0
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                permitted = True
            except OSError:
                permitted = False

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        if not permitted:
            return False, "this host refuses the tree kill"
        if wait_gone(grand, 5):
            return True, "the tree kill removed parent and grandchild"
        return False, "the tree kill was accepted but the grandchild survived"
    finally:
        kill_owned_tree(proc)
        if grand is not None and alive(grand):
            terminate_pid(grand)
        try:
            os.unlink(pid_file)
        except OSError:
            pass


class SandboxTimeout(Exception):
    """The sandbox runner outlived the harness's own bound."""


class Scenario(object):
    """A throwaway tests/ directory: a copy of the runner plus probe suites."""

    def __init__(self, label, pids_root):
        self.uid = "%s-%s" % (label, uuid.uuid4().hex[:8])
        self.dir = tempfile.mkdtemp(prefix="runall-contracts-")
        self.tests = os.path.join(self.dir, "tests")
        os.makedirs(self.tests)
        self.runner = os.path.join(self.tests, "run_all.py")
        shutil.copyfile(REAL_RUNNER, self.runner)
        self.logs_dir = os.path.join(self.dir, "kept-logs")
        #: Registered probe pids live beside the scenario directory, never
        #: inside it: tearDown has to read them after the run and before the
        #: files go away, and a probe can outlive the run.
        self.pids_dir = os.path.join(pids_root, self.uid)
        os.makedirs(self.pids_dir)
        self.owned = []
        self.names = []
        self._runs = 0

    def add(self, label, body):
        """Write a probe suite and return its file name."""
        name = "_test_%s_%s.py" % (self.uid, label)
        with open(os.path.join(self.tests, name), "w", encoding="utf-8") as fh:
            fh.write(body)
        self.names.append(name)
        return name

    def pid_file(self, label="probe"):
        """Where a probe registers the pids the harness may have to kill."""
        return os.path.join(self.pids_dir, label + ".json")

    def break_timeout(self):
        """Regress the copied runner so its own --timeout never fires.

        Anchored on the exact line and loud when the anchor is gone: a negative
        check that quietly stopped breaking anything would be worse than no
        check at all.
        """
        with open(self.runner, encoding="utf-8") as fh:
            text = fh.read()
        broken = text.replace(TIMEOUT_ANCHOR, "code = proc.wait()", 1)
        if broken == text:
            raise AssertionError(
                "cannot break the copied runner: %r is no longer in run_all.py, "
                "so the negative harness check would prove nothing"
                % TIMEOUT_ANCHOR)
        with open(self.runner, "w", encoding="utf-8") as fh:
            fh.write(broken)

    def run(self, *extra, **kwargs):
        """Run the sandboxed runner; return (returncode, output, seconds).

        The runner is started in a group the harness owns and managed with
        Popen, not subprocess.run(timeout=...): that helper's timeout owns only
        the direct process, so a runner that stopped honouring --timeout would
        leave its probe tree behind. Here the outer bound takes the whole tree
        and reaps it before raising.

        The output goes to a file, never a pipe: a probe that survives the run
        would otherwise hold the pipe open and stall the reader.
        """
        timeout = kwargs.pop("timeout", 180)
        env = dict(os.environ)
        env.update(kwargs.pop("env", {}))
        self._runs += 1
        out_path = os.path.join(self.dir, "runner-output-%d.txt" % self._runs)
        started = time.time()
        with open(out_path, "w", encoding="utf-8") as sink:
            proc = subprocess.Popen([sys.executable, self.runner] + list(extra),
                                    cwd=self.dir, env=env, stdout=sink,
                                    stderr=subprocess.STDOUT,
                                    **new_group_kwargs())
            self.owned.append(proc)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                kill_owned_tree(proc)
                # run_all.py gives every suite its own group, so a probe is not
                # in the runner's group; the pid it registered is reachable.
                self.kill_registered()
                raise SandboxTimeout(
                    "the sandbox runner did not return within %ds: %s"
                    % (timeout, " ".join(extra)))
        seconds = time.time() - started
        with open(out_path, encoding="utf-8", errors="replace") as fh:
            return proc.returncode, fh.read(), seconds

    def kill_registered(self):
        """Kill any probe that registered a pid and is still running.

        Deepest first: a grandchild is nobody's child once its parent is gone,
        and on Windows the tree walk could no longer reach it.
        """
        try:
            entries = sorted(os.listdir(self.pids_dir))
        except OSError:
            return
        for entry in entries:
            try:
                with open(os.path.join(self.pids_dir, entry),
                          encoding="utf-8") as fh:
                    pids = json.load(fh)
            except (OSError, ValueError):
                continue
            for key in ("child", "parent"):
                pid = pids.get(key)
                if pid and alive(pid):
                    terminate_pid(pid)

    def stop_owned(self):
        for proc in self.owned:
            kill_owned_tree(proc)

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.pids_dir, ignore_errors=True)


class RunnerContractTests(unittest.TestCase):
    def setUp(self):
        self._scenarios = []
        self._pids_root = tempfile.mkdtemp(prefix="runall-contract-pids-")

    def tearDown(self):
        # Order matters: the harness takes its processes back before any file
        # it needs to find them by is deleted.
        for scenario in self._scenarios:
            scenario.stop_owned()
            scenario.kill_registered()
            scenario.cleanup()
        shutil.rmtree(self._pids_root, ignore_errors=True)

    def scenario(self, label):
        created = Scenario(label, self._pids_root)
        self._scenarios.append(created)
        return created

    # -- the copy under test ------------------------------------------------

    def test_the_sandbox_runner_is_the_real_one(self):
        """Every other check is meaningless unless the copy is byte-identical."""
        scenario = self.scenario("identity")
        with open(REAL_RUNNER, "rb") as real, open(scenario.runner, "rb") as copy:
            self.assertEqual(real.read(), copy.read(),
                             "the sandbox runner is not the repository's run_all.py")

    # -- the tree walk the cleanup depends on -------------------------------

    def test_the_process_table_is_pid_to_parent(self):
        """Both platform branches must hand _descendants the same shape.

        Windows walks Toolhelp32 and POSIX parses /proc. A mismatch between the
        two only shows up on the platform the author is not sitting in front
        of, which is exactly how a cleanup path rots unnoticed.
        """
        table = _process_tree()
        if table is None:
            self.skipTest("this host exposes no process table")
        self.assertTrue(table, "the process table came back empty")
        for pid, parent in table.items():
            self.assertIsInstance(pid, int, "a process-table key is not an int")
            self.assertIsInstance(parent, int,
                                  "the parent of pid %r is not an int" % (pid,))
        self.assertIn(os.getpid(), table,
                      "the process table does not list this process")
        self.assertIn(os.getpid(), _descendants(os.getpid()),
                      "_descendants dropped the root it was given")

    def test_the_proc_stat_parser_survives_an_awkward_command_name(self):
        """The Linux branch is the one a local Windows run cannot exercise."""
        line = b"4242 (python (weird) name) S 99 4242 4242 0 -1 4194560 0 0"
        self.assertEqual(_parse_proc_stat(line), 99)
        self.assertEqual(_parse_proc_stat(b"7 (sh) R 1 7 7 0 -1 0 0"), 1)

    def test_the_proc_reader_gives_the_same_shape_as_the_other_platform(self):
        """A synthetic /proc, so the Linux reader is checked everywhere.

        The two branches have to agree on {pid: parent pid}; when they did not,
        nothing on Windows noticed and the cleanup path only fell over on CI.
        """
        root = tempfile.mkdtemp(prefix="fake-proc-")
        try:
            for pid, ppid in ((11, 1), (22, 11), (33, 11)):
                os.makedirs(os.path.join(root, str(pid)))
                with open(os.path.join(root, str(pid), "stat"), "wb") as fh:
                    fh.write(b"%d (python) S %d %d %d 0 -1 0 0"
                             % (pid, ppid, pid, pid))
            # Neither of these may contribute a row: a non-numeric directory
            # and a stat file that cannot be parsed.
            os.makedirs(os.path.join(root, "not-a-pid"))
            os.makedirs(os.path.join(root, "44"))
            with open(os.path.join(root, "44", "stat"), "wb") as fh:
                fh.write(b"garbage")

            table = _proc_process_tree(root)
            self.assertEqual(table, {11: 1, 22: 11, 33: 11})
            self.assertEqual(_descendants(11, table), [22, 33, 11])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_the_proc_reader_reports_no_table_when_proc_is_absent(self):
        """macOS and friends: no /proc means "fall back", not "empty tree"."""
        self.assertIsNone(_proc_process_tree(os.path.join(tempfile.gettempdir(),
                                                          "no-such-proc-root")))

    def test_descendants_are_returned_deepest_first(self):
        """The order decides what is reachable when: kill a parent first and
        its child becomes nobody's child, which on Windows means the tree walk
        can no longer find it."""
        table = {10: 1, 11: 10, 12: 11, 13: 10, 99: 1}
        self.assertEqual(_descendants(10, table), [12, 11, 13, 10])
        self.assertEqual(_descendants(12, table), [12])
        self.assertEqual(_descendants(77, table), [77])

    # -- argument validation ------------------------------------------------

    def test_a_non_positive_jobs_value_is_a_usage_error(self):
        scenario = self.scenario("jobs")
        for bad in ("0", "-3"):
            with self.subTest(jobs=bad):
                code, output, _ = scenario.run("--jobs", bad, timeout=60)
                self.assertEqual(code, 2, "expected a usage error:\n" + output)
                self.assertIn("--jobs must be a positive number of suites", output)
                self.assertIn("usage:", output)

    def test_a_non_positive_timeout_is_a_usage_error(self):
        scenario = self.scenario("timeout")
        for bad in ("0", "-1"):
            with self.subTest(timeout=bad):
                code, output, _ = scenario.run("--timeout", bad, timeout=60)
                self.assertEqual(code, 2, "expected a usage error:\n" + output)
                self.assertIn("--timeout must be a positive number of seconds",
                              output)

    def test_a_pattern_that_matches_nothing_is_not_a_pass(self):
        """A typo must not be reported as "0 passed, 0 failed" and exit 0."""
        scenario = self.scenario("nomatch")
        code, output, _ = scenario.run("no-such-suite-" + uuid.uuid4().hex,
                                       timeout=60)
        self.assertEqual(code, 2, output)
        self.assertIn("no suite matches", output)
        self.assertNotIn("0 passed", output)

    # -- failure reporting --------------------------------------------------

    def test_a_failing_probe_reports_the_real_error(self):
        """The probe really runs and really exits non-zero; nothing is mocked."""
        scenario = self.scenario("failing")
        name = scenario.add("fail", (
            "import sys\n"
            "print('probe is running')\n"
            "raise RuntimeError('probe-boom-%s')\n" % scenario.uid))
        code, output, _ = scenario.run(scenario.uid, timeout=120)
        self.assertEqual(code, 1, output)
        self.assertIn("[FAIL] %s" % name, output)
        self.assertIn("RuntimeError: probe-boom-%s" % scenario.uid, output)
        self.assertIn("0 passed, 1 failed", output)
        self.assertIn("failed: %s" % name, output)
        self.assertIn("--- last 25 lines of %s ---" % name, output)

    def test_the_failure_line_survives_a_node_style_tail(self):
        """A JS suite dies with a bare "Node.js v20.20.2"; name the reason."""
        scenario = self.scenario("nodetail")
        name = scenario.add("nodetail", (
            "import sys\n"
            "print('Traceback (most recent call last):')\n"
            "print('  File \"probe.js\", line 1, in <module>')\n"
            "print('TypeError: probe-not-a-function-%s')\n"
            "print('Node.js v20.20.2')\n"
            "sys.exit(1)\n" % scenario.uid))
        code, output, _ = scenario.run(scenario.uid, timeout=120)
        self.assertEqual(code, 1, output)
        line = [l for l in output.splitlines() if "[FAIL]" in l and name in l]
        self.assertEqual(len(line), 1, output)
        self.assertIn("TypeError: probe-not-a-function-%s" % scenario.uid,
                      line[0])
        self.assertNotIn("Node.js v20.20.2", line[0])

    # -- log retention ------------------------------------------------------

    def test_logs_dir_keeps_the_full_output_not_just_the_tail(self):
        scenario = self.scenario("logs")
        sentinel = "first-line-%s" % scenario.uid
        name = scenario.add("verbose", (
            "import sys\n"
            "print('%s')\n"
            "for i in range(%d):\n"
            "    print('filler %%d' %% i)\n"
            "print('probe-summary-%s')\n" % (sentinel, FILLER_LINES, scenario.uid)))
        code, output, _ = scenario.run(scenario.uid, "--logs", scenario.logs_dir,
                                       timeout=120)
        self.assertEqual(code, 0, output)
        kept = os.path.join(scenario.logs_dir, name + ".log")
        self.assertTrue(os.path.exists(kept), "no log kept in %s" % scenario.logs_dir)
        with open(kept, encoding="utf-8") as fh:
            kept_text = fh.read()
        # The runner echoes only the last line of a passing suite, so the first
        # line proves the kept log is the whole output rather than that echo.
        self.assertNotIn(sentinel, output)
        self.assertIn(sentinel, kept_text)
        self.assertIn("filler %d" % (FILLER_LINES - 1), kept_text)
        self.assertIn("probe-summary-%s" % scenario.uid, kept_text)

    def test_a_failed_probe_points_at_its_kept_log(self):
        scenario = self.scenario("logsfail")
        name = scenario.add("logfail", (
            "import sys\n"
            "print('probe is running')\n"
            "sys.exit(3)\n"))
        code, output, _ = scenario.run(scenario.uid, "--logs", scenario.logs_dir,
                                       timeout=120)
        self.assertEqual(code, 1, output)
        self.assertIn("full output: %s" % os.path.join(scenario.logs_dir,
                                                       name + ".log"), output)

    # -- the wall clock -----------------------------------------------------

    def test_a_hanging_probe_is_bounded_and_its_tree_is_reaped(self):
        """The probe sleeps for ten minutes; --timeout has to end it."""
        scenario = self.scenario("hang")
        name = scenario.add("hang", TREE_PROBE % (ORPHAN_SECONDS, HANG_SECONDS))
        pid_file = scenario.pid_file("hang")
        code, output, seconds = scenario.run(scenario.uid, "--timeout",
                                             str(HANG_TIMEOUT), timeout=HANG_BUDGET + 60,
                                             env={"PROBE_PID_FILE": pid_file})
        self.assertEqual(code, 1, output)
        self.assertIn("timed out after %ds" % HANG_TIMEOUT, output)
        self.assertIn("[FAIL] %s" % name, output)
        self.assertLess(seconds, HANG_BUDGET,
                        "run_all.py was not bounded by --timeout: %.1fs" % seconds)

        pids = read_pids(pid_file)
        parent, grand = pids["parent"], pids["child"]

        can, why = host_can_kill_trees()
        if can:
            self.assertTrue(wait_gone(grand, 10),
                            "the timed-out suite's grandchild %d survived; "
                            "tree cleanup did not happen" % grand)
        else:
            # The runner falls back to killing the direct child here, so the
            # descendant check cannot be made on this host. Say so loudly
            # instead of turning a refused kill into a green check.
            print("  [NOTE] %s; only the direct child is checked here" % why,
                  flush=True)
        try:
            self.assertTrue(wait_gone(parent, 10),
                            "the timed-out suite %d was not reaped" % parent)
        finally:
            if alive(grand):
                terminate_pid(grand)

    def test_a_broken_runner_is_abandoned_boundedly_and_leaves_nothing(self):
        """The harness must clean up without the runner's cooperation.

        The copied runner is regressed so its own wall clock never fires: the
        probe sleeps for ten minutes and nothing inside run_all.py will stop
        it. Only the harness's own bound can end the run, and it has to take
        the probe and its grandchild with it - otherwise the failure path of
        this very suite is what leaks processes.
        """
        scenario = self.scenario("broken")
        scenario.break_timeout()
        scenario.add("hang", TREE_PROBE % (ORPHAN_SECONDS, HANG_SECONDS))
        pid_file = scenario.pid_file("broken")

        started = time.time()
        with self.assertRaises(SandboxTimeout):
            scenario.run(scenario.uid, "--timeout", str(HANG_TIMEOUT),
                         timeout=SANDBOX_BUDGET,
                         env={"PROBE_PID_FILE": pid_file})
        elapsed = time.time() - started
        self.assertLess(elapsed, SANDBOX_BUDGET + 30,
                        "the harness did not give up boundedly: %.1fs" % elapsed)

        pids = read_pids(pid_file)
        self.assertTrue(wait_gone(pids["parent"], 10),
                        "the probe %d outlived the harness timeout"
                        % pids["parent"])
        self.assertTrue(wait_gone(pids["child"], 10),
                        "the grandchild %d outlived the harness timeout"
                        % pids["child"])

    # -- serial and parallel agree -----------------------------------------

    def test_jobs_1_and_4_agree_on_the_pass_fail_set(self):
        scenario = self.scenario("jobsagree")
        for label in ("alpha", "beta"):
            scenario.add(label, "print('probe-%s-ok')\n" % label)
        for label in ("gamma", "delta"):
            scenario.add(label, (
                "import sys\n"
                "print('probe-%s-running')\n"
                "raise RuntimeError('probe-%s-boom')\n" % (label, label)))

        serial_code, serial, _ = scenario.run(scenario.uid, "--jobs", "1",
                                              timeout=180)
        parallel_code, parallel, _ = scenario.run(scenario.uid, "--jobs", "4",
                                                  timeout=180)

        self.assertEqual(serial_code, 1, serial)
        self.assertEqual(parallel_code, 1, parallel)
        self.assertIn("2 passed, 2 failed", serial)
        self.assertIn("2 passed, 2 failed", parallel)
        # Completion order is not fixed under --jobs > 1, so compare the sets.
        self.assertEqual(_failed_set(serial), _failed_set(parallel),
                         "serial and parallel disagreed:\n%s\n%s"
                         % (serial, parallel))
        self.assertEqual(_failed_set(serial), set(scenario.names[2:]))
        self.assertIn("--jobs 1", serial)
        self.assertIn("--jobs 4", parallel)


def _failed_set(output):
    for line in output.splitlines():
        if line.strip().startswith("failed:"):
            return {part.strip() for part in line.split(":", 1)[1].split(",")}
    return set()


if __name__ == "__main__":
    unittest.main(verbosity=2)
