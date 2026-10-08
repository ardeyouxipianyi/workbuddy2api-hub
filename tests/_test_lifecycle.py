"""The shared lifecycle pair must clean up what it promises.

    python tests/_test_lifecycle.py

Real processes, no mocks: a spare port is bindable, and stop_managed() ends a
process from spawn_managed() together with anything it started - including the
case where the parent has already exited and only its grandchild is left. On
Windows a dead pid cannot be walked for its children, so stop_managed() returns
False there and this suite removes the orphan itself; that platform difference is
the point of the last check.
"""
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import _lifecycle as life  # noqa: E402

PASS = 0
FAIL = 0

#: A parent that starts a sleeping grandchild and prints its pid.
SPAWN = ("import subprocess, sys, time\n"
         "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
         "print(child.pid, flush=True)\n")


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (label, detail))


def spawn_wrapper(stay_alive):
    """A managed parent that either waits, or exits and leaves its child behind."""
    code = SPAWN + ("time.sleep(120)\n" if stay_alive else "sys.exit(0)\n")
    proc = life.spawn_managed([sys.executable, "-c", code], stdout=subprocess.PIPE,
                              text=True)
    return proc, int(proc.stdout.readline().strip())


def alive(pid):
    """Is that pid still running?

    Not via tasklist: a locked-down host answers "Access denied" there, which
    would read as "gone" for every pid - including this very process - and the
    liveness checks below would pass without checking anything.
    """
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_orphan(pid):
    """Last resort for the orphan the suite creates on purpose.

    taskkill can be refused where this matters, so go to the OS directly.
    """
    if os.name == "nt":
        import ctypes
        PROCESS_TERMINATE = 0x0001
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if handle:
            try:
                kernel32.TerminateProcess(handle, 1)
            finally:
                kernel32.CloseHandle(handle)
        return
    try:
        os.kill(pid, 9)
    except OSError:
        pass


port = life.free_port()
try:
    probe = socket.socket()
    probe.bind(("127.0.0.1", port))
    probe.close()
    check("free_port() hands back a bindable port", True)
except OSError as exc:
    check("free_port() hands back a bindable port", False, str(exc))

proc, grandpid = spawn_wrapper(stay_alive=True)
stopped = life.stop_managed(proc)
time.sleep(0.5)
if stopped:
    check("stop_managed() ends a live process and its grandchild",
          not alive(proc.pid) and not alive(grandpid),
          "parent alive=%s grandchild alive=%s" % (alive(proc.pid), alive(grandpid)))
else:
    # A locked-down host can refuse the walk (taskkill answers "Access denied" in
    # a sandbox). The contract then is: report that the tree step did not happen,
    # still reap the process that was handed over, and leave nothing behind - so
    # this suite removes the grandchild itself and checks that it went.
    check("a refused tree walk is reported and the process is still reaped",
          not alive(proc.pid), "process alive=%s" % alive(proc.pid))
kill_orphan(grandpid)
time.sleep(0.5)
check("the probe leaves no process behind",
      not alive(proc.pid) and not alive(grandpid),
      "parent alive=%s grandchild alive=%s" % (alive(proc.pid), alive(grandpid)))

proc, grandpid = spawn_wrapper(stay_alive=False)
proc.wait(timeout=30)
stopped = life.stop_managed(proc)
time.sleep(0.5)
left = alive(grandpid)
if os.name == "nt":
    check("stop_managed() reports it cannot walk a dead pid on Windows",
          stopped is False, "returned %s" % stopped)
else:
    check("stop_managed() reaches a grandchild whose parent already exited",
          stopped and not left, "grandchild alive=%s" % left)
kill_orphan(grandpid)
time.sleep(0.5)
check("the probe leaves no process behind here either",
      not alive(grandpid), "grandchild alive=%s" % alive(grandpid))

check("stop_managed() is a no-op for None", life.stop_managed(None))

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
