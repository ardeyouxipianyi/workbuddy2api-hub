# -*- coding: utf-8 -*-
"""Gateway backend launcher: owns the process that binds the port.

The gateway has two implementations that can serve the same port: the Python
one in ``wb_proxy.py`` (today) and the Rust one (optional, still being ported).
Exactly one of them may bind at a time, and only this module decides which:

    python3 wb_backend.py --host 0.0.0.0 --port 8788 --lan     # supervise
    python3 wb_proxy.py --supervise --host ... --port ...      # same thing

Nothing here imports wb_proxy / wb_settings: a launcher that drags the whole
gateway into memory would defeat the point of switching to a smaller backend.

Three rules keep the handover safe:

* the port has exactly one owner - the child, never this process;
* ``accounts/backend.json`` is written by this module only, and only after the
  new backend answers /health with its own identity;
* the child is stopped before the next one starts, so two processes never
  write ``accounts/`` or ``usage/`` at the same time.

The panel reaches this process over ``accounts/launcher.sock`` (a Unix socket,
so an isolated test instance with its own --accounts-dir gets its own control
channel instead of fighting the production one).
"""
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

BACKEND_FILE = "backend.json"
CAPS_FILE = "backend-capabilities.json"
SOCK_FILE = "launcher.sock"
AUDIT_FILE = "backend-switch.jsonl"

BACKENDS = ("python", "rust")
#: 一组后端连着崩这么多次，就判定它不可用并回落另一个。
CRASH_LIMIT = 3
CRASH_WINDOW = 600.0
#: 起子进程后多久必须答 /health。
START_TIMEOUT = 30.0
#: 切换时等在飞请求归零的上限，超时强制切（会记一条 audit）。
DRAIN_TIMEOUT = 60.0
#: SIGTERM 之后等进程退出的上限，超时 SIGKILL。
STOP_TIMEOUT = 10.0


def log(msg):
    sys.stderr.write("[launcher] %s %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stderr.flush()


def _atomic_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_backend(accounts_dir):
    """The backend the operator last chose. Defaults to python."""
    try:
        with open(os.path.join(accounts_dir, BACKEND_FILE), encoding="utf-8") as fh:
            data = json.load(fh)
        name = data.get("backend")
        return name if name in BACKENDS else "python"
    except (OSError, ValueError):
        return "python"


def write_backend(accounts_dir, name, reason=""):
    if name not in BACKENDS:
        raise ValueError("unknown backend: %r" % (name,))
    os.makedirs(accounts_dir, exist_ok=True)
    _atomic_json(os.path.join(accounts_dir, BACKEND_FILE), {
        "backend": name,
        "updated_at": time.time(),
        "reason": reason,
    })


def capabilities(app_dir):
    """What the shipped Rust backend claims to implement, plus binary state.

    The file travels with the binary, so "is Rust ready" is answered by the
    artifact itself instead of by a human-maintained checklist.
    """
    path = os.path.join(app_dir, CAPS_FILE)
    caps = {"binary": {"path": os.path.join(app_dir, "wb-backend"), "present": False,
                       "version": None},
            "groups": {}, "labels": {}, "ported": False}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if isinstance(data, dict):
        groups = data.get("groups")
        if isinstance(groups, dict):
            for key, value in groups.items():
                if isinstance(value, dict):
                    caps["groups"][str(key)] = bool(value.get("ported"))
                    label = value.get("label")
                    if isinstance(label, str) and label:
                        caps["labels"][str(key)] = label
                else:
                    caps["groups"][str(key)] = bool(value)
        version = data.get("version")
        if isinstance(version, str):
            caps["binary"]["version"] = version
        name = data.get("binary")
        if isinstance(name, str) and name:
            caps["binary"]["path"] = os.path.join(app_dir, name)
    binary = caps["binary"]["path"]
    caps["binary"]["present"] = os.path.isfile(binary) and os.access(binary, os.X_OK)
    # 就绪 = 二进制在 + 六组功能全部 ported。缺项要能显示给人看，所以给出人话标签。
    missing = sorted(k for k, ok in caps["groups"].items() if not ok)
    caps["missing"] = [caps["labels"].get(k, k) for k in missing]
    caps["ported"] = bool(caps["binary"]["present"] and caps["groups"] and not missing)
    return caps


def probe(host, port, timeout=2.0):
    """GET /health; returns the parsed payload or None."""
    addr = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    url = "http://%s:%d/health" % (addr, port)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


class Supervisor(object):
    def __init__(self, app_dir, accounts_dir, usage_dir, host, port, passthrough):
        self.app_dir = app_dir
        self.accounts_dir = accounts_dir
        self.usage_dir = usage_dir
        self.host = host
        self.port = port
        self.passthrough = list(passthrough)
        self.proc = None
        self.current = None
        self.started_at = 0.0
        self.exits = []
        self.lock = threading.RLock()
        self.stopping = False
        self.last_switch = {"at": 0.0, "to": None, "ok": None, "detail": "", "ms": 0}

    # ---- 审计 ------------------------------------------------------------
    def audit(self, event, backend=None, detail="", **extra):
        row = {"at": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "event": event, "backend": backend or self.current, "detail": detail}
        row.update(extra)
        try:
            os.makedirs(self.usage_dir, exist_ok=True)
            with open(os.path.join(self.usage_dir, AUDIT_FILE), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as exc:
            log("audit write failed: %s" % exc)

    # ---- 子进程 ----------------------------------------------------------
    def _argv_for(self, backend):
        if backend == "python":
            return [sys.executable, os.path.join(self.app_dir, "wb_proxy.py"), "--serve"] + self.passthrough
        caps = capabilities(self.app_dir)
        return [caps["binary"]["path"],
                "--host", self.host, "--port", str(self.port),
                "--accounts-dir", self.accounts_dir, "--usage-dir", self.usage_dir]

    def start(self, backend):
        argv = self._argv_for(backend)
        log("starting %s backend: %s" % (backend, " ".join(argv[:3])))
        self.proc = subprocess.Popen(argv, cwd=self.app_dir)
        self.current = backend
        self.started_at = time.time()
        return self.proc

    def wait_healthy(self, backend, timeout=START_TIMEOUT):
        """Wait until /health answers *and* says it is `backend`.

        Matching the identity matters: right after a switch the port can still
        be answered by the process we are stopping, and a bare 200 would be
        read as "the new backend is up" while nothing has changed.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                return None
            payload = probe(self.host, self.port)
            if payload and payload.get("backend") == backend and payload.get("ok"):
                return payload
            time.sleep(0.2)
        return None

    def stop_child(self):
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return True
        try:
            proc.send_signal(signal.SIGTERM)
        except OSError:
            return True
        deadline = time.time() + STOP_TIMEOUT
        while time.time() < deadline:
            if proc.poll() is not None:
                return True
            time.sleep(0.1)
        log("child ignored SIGTERM for %.0fs, killing" % STOP_TIMEOUT)
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
        self.audit("kill", backend=self.current, detail="SIGTERM timeout")
        return False

    # ---- 切换 ------------------------------------------------------------
    def _drain(self):
        """等旧后端的在飞请求归零；返回 (ok, 等了几秒)."""
        deadline = time.time() + DRAIN_TIMEOUT
        start = time.time()
        while time.time() < deadline:
            payload = probe(self.host, self.port, timeout=1.0)
            if payload is None:
                return True, time.time() - start          # 已经不在了
            inflight = payload.get("inflight")
            if not isinstance(inflight, int) or inflight <= 0:
                return True, time.time() - start
            time.sleep(0.2)
        return False, time.time() - start

    def switch(self, target, wait_inflight=True):
        with self.lock:
            t0 = time.time()
            previous = self.current
            if target not in BACKENDS:
                return {"ok": False, "error": "unknown backend: %r" % (target,)}
            if target == previous and self.proc is not None and self.proc.poll() is None:
                return {"ok": True, "backend": target, "detail": "already serving", "ms": 0}
            if target == "rust":
                caps = capabilities(self.app_dir)
                if not caps["ported"]:
                    missing = ", ".join(caps["missing"]) or "binary missing"
                    return {"ok": False, "error": "rust backend not ready", "missing": missing}
            if wait_inflight:
                drained, waited = self._drain()
                if not drained:
                    self.audit("switch_forced", backend=previous,
                               detail="%.0fs with inflight>0" % waited)
            self.stop_child()
            self.audit("switch_start", backend=target, detail="from %s" % previous)
            self.start(target)
            payload = self.wait_healthy(target)
            if payload is None:
                log("%s backend failed to come up, restoring %s" % (target, previous))
                self.stop_child()
                if previous:
                    self.start(previous)
                    self.wait_healthy(previous, timeout=START_TIMEOUT)
                ms = int((time.time() - t0) * 1000)
                self.audit("switch_failed", backend=previous, detail="back to %s" % previous, ms=ms)
                self.last_switch = {"at": time.time(), "to": target, "ok": False,
                                    "detail": "new backend did not answer /health", "ms": ms}
                return {"ok": False, "error": "new backend did not answer /health",
                        "backend": previous, "ms": ms}
            write_backend(self.accounts_dir, target, reason="panel switch")
            ms = int((time.time() - t0) * 1000)
            self.audit("switch_ok", backend=target, detail="from %s" % previous, ms=ms)
            self.last_switch = {"at": time.time(), "to": target, "ok": True, "detail": "", "ms": ms}
            return {"ok": True, "backend": target, "ms": ms}

    # ---- 监督循环 --------------------------------------------------------
    def status(self):
        payload = probe(self.host, self.port, timeout=1.0)
        caps = capabilities(self.app_dir)
        return {
            "backend": self.current,
            "pid": self.proc.pid if self.proc is not None and self.proc.poll() is None else None,
            "healthy": bool(payload and payload.get("backend") == self.current),
            "uptime_s": round(time.time() - self.started_at, 1) if self.started_at else 0,
            "inflight": (payload or {}).get("inflight"),
            "python": {"available": True},
            "rust": {"available": caps["ported"], "missing": caps["missing"],
                     "binary": caps["binary"]},
            "last_switch": self.last_switch,
            "supervised": True,
        }

    def _handle(self, request):
        cmd = (request or {}).get("cmd")
        if cmd == "status":
            return self.status()
        if cmd == "switch":
            return self.switch(str(request.get("to") or ""),
                               wait_inflight=request.get("wait_inflight", True) is not False)
        if cmd == "shutdown":
            self.stopping = True
            threading.Thread(target=self.stop_child, daemon=True).start()
            return {"ok": True, "detail": "shutting down"}
        return {"ok": False, "error": "unknown command: %r" % (cmd,)}

    def serve_control(self):
        path = os.path.join(self.accounts_dir, SOCK_FILE)
        try:
            os.unlink(path)
        except OSError:
            pass
        os.makedirs(self.accounts_dir, exist_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        os.chmod(path, 0o600)
        srv.listen(8)
        log("control socket: %s" % path)
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                try:
                    raw = conn.makefile("rb").readline(65536)
                    resp = self._handle(json.loads(raw.decode("utf-8"))) if raw else {"ok": False}
                except Exception as exc:      # 一个坏请求不能带走 launcher
                    resp = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
                try:
                    conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                except OSError:
                    pass

    def run(self):
        backend = read_backend(self.accounts_dir)
        if backend == "rust" and not capabilities(self.app_dir)["ported"]:
            self.audit("fallback", backend="python", detail="rust not ready")
            write_backend(self.accounts_dir, "python", reason="rust not ready")
            backend = "python"
        self.start(backend)
        threading.Thread(target=self.serve_control, daemon=True).start()
        if self.wait_healthy(backend) is None:
            log("%s backend did not answer /health within %.0fs" % (backend, START_TIMEOUT))
            self.audit("start_failed", backend=backend)
        backoff = 1.0
        while not self.stopping:
            code = self.proc.poll() if self.proc is not None else None
            if code is None:
                time.sleep(0.3)
                continue
            now = time.time()
            self.exits = [t for t in self.exits if now - t < CRASH_WINDOW] + [now]
            log("%s backend exited (code %s), %d exit(s) in %.0fs"
                % (self.current, code, len(self.exits), CRASH_WINDOW))
            if len(self.exits) >= CRASH_LIMIT:
                other = "python" if self.current == "rust" else "rust"
                caps = capabilities(self.app_dir)
                if other == "rust" and not caps["ported"]:
                    log("no fallback backend available (rust not ready)")
                else:
                    log("too many crashes, falling back to %s" % other)
                    write_backend(self.accounts_dir, other, reason="crash fallback")
                    self.audit("fallback", backend=other, detail="%d crashes" % len(self.exits))
                    self.exits = []
                    self.start(other)
                    self.wait_healthy(other)
                    backoff = 1.0
                    continue
            time.sleep(backoff)
            backoff = min(backoff * 2, 5.0)
            self.start(self.current)
            self.wait_healthy(self.current)
        self.stop_child()


def main(argv=None, app_dir=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    app_dir = app_dir or os.path.dirname(os.path.abspath(__file__))
    import argparse
    ap = argparse.ArgumentParser(description="Supervise the gateway backend that owns the port")
    ap.add_argument("--host", default=os.environ.get("HOST") or "127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT") or "8788"))
    ap.add_argument("--accounts-dir", default=os.environ.get("ACCOUNTS_DIR") or
                    os.path.join(app_dir, "accounts"))
    ap.add_argument("--usage-dir", default=os.environ.get("USAGE_DIR") or
                    os.path.join(app_dir, "usage"))
    ap.add_argument("--lan", action="store_true")
    ap.add_argument("--api-key", default=None)
    args, _passthrough = ap.parse_known_args(argv)
    host = "0.0.0.0" if args.lan else args.host
    # 子进程拿的是**原始 argv**：--lan / --api-key / --panel-password 这些参数
    # 本模块不解释，原样传给 Python 后端，行为与今天直接起 wb_proxy.py 一致。
    sup = Supervisor(app_dir, os.path.abspath(args.accounts_dir), os.path.abspath(args.usage_dir),
                     host, args.port, argv)

    def _term(signum, frame):
        sup.stopping = True
        sup.stop_child()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    try:
        sup.run()
    except SystemExit:
        raise
    except Exception as exc:
        log("launcher crashed: %s: %s" % (type(exc).__name__, exc))
        sup.stop_child()
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
