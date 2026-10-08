"""Shared lifecycle for the suites that start a process: a port, and a managed pair.

Only what more than one suite needs lives here. Gateway readiness and every HTTP
detail stay in _test_connection_reuse.py.

spawn_managed() and stop_managed() are one pair: the spawn always puts the child
in its own group/session, and the stop only takes a process started that way.
That is what makes the tree cleanup unambiguous - there is no ownership flag to
get wrong, and signalling the group can never reach the caller's own group.
"""
import os
import signal
import socket
import subprocess

STOP_TIMEOUT = 10.0


def free_port():
    """A loopback port that is free right now."""
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def spawn_managed(*args, **kwargs):
    """Popen a child in its own group/session, so stop_managed() can take its tree.

    Anything Popen accepts can be passed through; the two group flags are set here.
    """
    kwargs.update({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                  if os.name == "nt" else {"start_new_session": True})
    return subprocess.Popen(*args, **kwargs)


def stop_managed(proc, timeout=STOP_TIMEOUT):
    """Stop a process from spawn_managed(), with everything it started.

    Returns True when the cleanup happened. False means the tree step did not:
    a locked-down host can refuse it (taskkill answers "Access denied"), and on
    Windows a pid that has already exited cannot be walked for its children at
    all. The child itself is reaped either way.
    """
    if proc is None:
        return True
    if os.name == "nt":
        if proc.poll() is not None:
            return False
        # Ask first, force if it does not go - and never wait on a pass that was
        # rejected, which is what turns a refused taskkill into a 10s stall.
        walked = _taskkill(proc.pid, force=False)
        if walked:
            _reap(proc, timeout)
        if proc.poll() is None:
            forced = _taskkill(proc.pid, force=True)
            walked = walked or forced
            if forced:
                _reap(proc, timeout)
        if proc.poll() is None:
            _reap(proc, 5, kill=True)
        return bool(walked and proc.poll() is not None)
    # POSIX: the child leads its own group, so the group id is its pid - and the
    # group outlives the leader, which is what reaches a grandchild left behind.
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
        proc.wait(timeout=timeout)
    except Exception:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
    _reap(proc, 5)
    return proc.poll() is not None


def _reap(proc, timeout, kill=False):
    try:
        if kill and proc.poll() is None:
            proc.kill()
        proc.wait(timeout=timeout)
    except Exception:
        pass


def _taskkill(pid, force):
    """True when taskkill accepted the request."""
    args = ["taskkill", "/T", "/PID", str(pid)]
    if force:
        args.insert(1, "/F")
    try:
        done = subprocess.run(args, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=30)
    except Exception:
        return False
    return done.returncode == 0
