"""Optional local CodeArts service; only a child started here is ever stopped."""
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import time
import urllib.request


class _WindowsJob:
    """Kernel ownership survives os._exit, console close and TerminateProcess."""
    def __init__(self, process):
        import ctypes as c
        from ctypes import wintypes as w

        class Basic(c.Structure):
            _fields_ = [("per_process", c.c_int64), ("per_job", c.c_int64),
                ("flags", w.DWORD), ("min_ws", c.c_size_t), ("max_ws", c.c_size_t),
                ("active", w.DWORD), ("affinity", c.c_size_t),
                ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class IO(c.Structure):
            _fields_ = [(name, c.c_uint64) for name in
                ("reads", "writes", "other", "read_bytes", "write_bytes", "other_bytes")]

        class Extended(c.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_mem", c.c_size_t),
                ("job_mem", c.c_size_t), ("peak_process", c.c_size_t), ("peak_job", c.c_size_t)]

        kernel = c.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        kernel.CreateJobObjectW.restype = w.HANDLE
        kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not kernel.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits))
                or not kernel.AssignProcessToJobObject(self.handle, int(process._handle))):
            error = c.WinError(c.get_last_error())
            self.close()
            raise error

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class _ExitAwareOS:
    def __init__(self, original, stop):
        self.original, self.stop = original, stop

    def __getattr__(self, name):
        return getattr(self.original, name)

    def _exit(self, code):
        try:
            self.stop()
        finally:
            self.original._exit(code)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_backend(source, output, go="go"):
    source, output = Path(source).resolve(), Path(output).resolve()
    if "module codearts2api" not in (source / "go.mod").read_text(encoding="utf-8"):
        raise ValueError("Expected the CodeArts2API source directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    from .backend_overlay import model_id_overlay
    overlay = model_id_overlay(source, output.parent / "build-overlay")
    subprocess.run([str(go), "build", "-overlay", str(overlay), "-o", str(output), "./cmd/server"],
                   cwd=str(source), check=True)
    return output


class ManagedBackend:
    def __init__(self, command, data_dir, port=7866, timeout=20):
        if not 1 <= int(port) <= 65535:
            raise ValueError("Invalid CodeArts backend port")
        self.command = [str(part) for part in command]
        self.data_dir = Path(data_dir).resolve()
        self.port, self.timeout = int(port), timeout
        self.base_url = "http://127.0.0.1:%d" % self.port
        self.process = None
        self.log = None
        self.job = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def _verified(self, key):
        request = urllib.request.Request(self.base_url + "/admin/api/overview",
                                         headers={"Authorization": "Bearer " + key})
        try:
            with self.opener.open(request, timeout=1) as response:
                data = json.loads(response.read(1 << 20))
                return isinstance(data, dict) and data.get("service") == "codearts2api"
        except (OSError, ValueError):
            return False

    def start(self, config):
        snapshot = config.snapshot()
        key = snapshot["api_key"] or secrets.token_urlsafe(32)
        with socket.socket() as probe:
            probe.settimeout(1)
            occupied = probe.connect_ex(("127.0.0.1", self.port)) == 0
        if occupied:
            if not snapshot["api_key"] or not self._verified(key):
                raise RuntimeError("CodeArts port is occupied by an unverified existing service")
        else:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            auth_dir = self.data_dir / "auths"
            auth_dir.mkdir(exist_ok=True)
            private_config = self.data_dir / "config.json"
            private_config.write_text(json.dumps({
                "listen": "127.0.0.1:%d" % self.port,
                "auth_dir": str(auth_dir), "state_file": str(self.data_dir / "state.json"),
            }, indent=2) + "\n", encoding="utf-8")
            # Ignore ambient backend overrides: managed credentials, bind address and
            # account storage belong to this runtime, never an unrelated installation.
            env = {name: value for name, value in os.environ.items() if not name.startswith("CA2A_")}
            env["CA2A_API_KEY"] = key
            self.log = (self.data_dir / "backend.log").open("a", encoding="utf-8")
            try:
                self.process = subprocess.Popen(self.command + ["-config", str(private_config)],
                    cwd=str(self.data_dir), env=env, stdout=self.log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                if os.name == "nt":
                    self.job = _WindowsJob(self.process)
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    if self.process.poll() is not None:
                        raise RuntimeError("CodeArts child exited; inspect " + str(self.data_dir / "backend.log"))
                    if self._verified(key):
                        break
                    time.sleep(.1)
                else:
                    raise RuntimeError("CodeArts readiness timed out; inspect " + str(self.data_dir / "backend.log"))
            except BaseException:
                self.stop()
                raise
        try:
            config.update({"enabled": True, "base_url": self.base_url, "api_key": key})
        except BaseException:
            self.stop()
            raise
        return self

    def attach_exit(self, hub):
        # Native panel shutdown and Windows console-close use hub.os._exit.
        # Wrap only that module reference, leaving the shared os module intact.
        hub.os = _ExitAwareOS(hub.os, self.stop)
        if os.name != "nt":
            def terminate(signum, frame):
                self.stop()
                raise SystemExit(128 + signum)
            signal.signal(signal.SIGTERM, terminate)

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.log is not None:
            self.log.close()
            self.log = None
        if self.job is not None:
            self.job.close()
            self.job = None
