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


def _merge_overlays(overlays, target):
    """Combine -overlay files, keeping every replacement from both sides.

    Each input must carry a ``Replace`` object; one source file may only map to
    the same output in both overlays, so a silent overwrite cannot hide a
    broken overlay.
    """
    replacements = {}
    for overlay in overlays:
        path = Path(overlay)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("Replace"), dict):
            raise ValueError("Overlay %s has no Replace object" % path)
        for original, replacement in data["Replace"].items():
            if not isinstance(original, str) or not isinstance(replacement, str):
                raise ValueError("Overlay %s must map strings to strings" % path)
            if original in replacements and replacements[original] != replacement:
                raise ValueError("Conflicting overlay replacements for %s" % original)
            replacements[original] = replacement
    target.write_bytes((json.dumps({"Replace": replacements}, indent=2, ensure_ascii=False)
                        + "\n").encode("utf-8"))
    return target


def _header_payload(value, variable):
    """Explicit header mapping first, then the environment JSON object.

    None means "not configured": an empty explicit mapping falls through to the
    environment exactly like a blank User-Agent does, so a cleared UI field
    cannot silently suppress the documented env entry point.
    """
    if value is not None:
        if not isinstance(value, dict):
            raise TypeError("spoof_headers must be a JSON object of header names to values")
        if value:
            return value
    raw = os.environ.get(variable, "")
    if not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise ValueError("%s must be a JSON object of header names to values"
                         % variable) from None
    if not isinstance(parsed, dict):
        raise ValueError("%s must be a JSON object of header names to values" % variable)
    return parsed


def _session_id(value):
    """Explicit session id first, then CODEARTS_IDE_SESSION_ID; blank means unset."""
    if value is not None and not isinstance(value, str):
        raise TypeError("spoof_session_id must be a string")
    return ((value or "").strip()
            or os.environ.get("CODEARTS_IDE_SESSION_ID", "").strip())


def spoof_build_options(snapshot):
    """Translate a bridge configuration snapshot into ``build_backend`` keywords.

    Returns ``{"spoof_user_agent", "spoof_headers", "spoof_session_id",
    "spoof_timestamp", "spoof_ot_family", "spoof_client_ip"}``. Missing or blank
    settings translate to "not configured" (None/False), so the CODEARTS_IDE_*
    environment entries keep working and a configuration without identity
    settings builds exactly like before.

    ``User-Agent`` has its own argument: ``chat_headers_overlay`` writes it first
    and rejects a duplicate name, so a ``User-Agent`` entry inside
    ``identity_ide_headers`` is moved to ``spoof_user_agent`` (matched
    case-insensitively, since HTTP names are) and removed from the mapping.
    ``identity_ide_user_agent`` wins over that entry when both are set.

    ``identity_ide_function`` is the ``x-ot-function`` mode value. It only has a
    meaning for the OT family, so it becomes a static header override only when
    ``identity_ide_ot_family`` is on (the built-in default comes from
    ``spoof_overlay.DEFAULT_OT_FUNCTION``). Wrong types raise instead of being
    silently dropped, and the input snapshot is never mutated.
    """
    if not isinstance(snapshot, dict):
        raise TypeError("snapshot must be a configuration dict")
    headers = snapshot.get("identity_ide_headers")
    if headers is None:
        headers = {}
    if not isinstance(headers, dict):
        raise TypeError("identity_ide_headers must be a JSON object of header names to values")
    headers = dict(headers)
    user_agent = None
    for name in [key for key in headers if isinstance(key, str) and key.lower() == "user-agent"]:
        value = headers.pop(name)
        if not isinstance(value, str):
            raise TypeError("User-Agent in identity_ide_headers must be a string")
        value = value.strip()
        if value and user_agent and value != user_agent:
            raise ValueError("duplicate header name 'User-Agent'")
        user_agent = value or user_agent
    from .spoof_overlay import validate_header_family
    headers = dict(validate_header_family(headers))
    session = snapshot.get("identity_ide_user_session_id")
    if session is None:
        session = ""
    if not isinstance(session, str):
        raise TypeError("identity_ide_user_session_id must be a string")
    timestamp = snapshot.get("identity_ide_timestamp", False)
    if not isinstance(timestamp, bool):
        raise TypeError("identity_ide_timestamp must be true or false")
    ot_family = snapshot.get("identity_ide_ot_family", False)
    if not isinstance(ot_family, bool):
        raise TypeError("identity_ide_ot_family must be true or false")
    function = snapshot.get("identity_ide_function")
    if function is None:
        function = ""
    if not isinstance(function, str):
        raise TypeError("identity_ide_function must be a string")
    if ot_family and function.strip():
        headers = dict(validate_header_family({**headers, "x-ot-function": function.strip()}))
    user_agent_setting = snapshot.get("identity_ide_user_agent")
    if user_agent_setting is None:
        user_agent_setting = ""
    if not isinstance(user_agent_setting, str):
        raise TypeError("identity_ide_user_agent must be a string")
    if user_agent_setting.strip():
        user_agent = user_agent_setting.strip()
    client_ip = snapshot.get("identity_ide_client_ip")
    if client_ip is None:
        client_ip = ""
    if not isinstance(client_ip, str):
        raise TypeError("identity_ide_client_ip must be a string")
    return {
        "spoof_user_agent": user_agent,
        "spoof_headers": headers or None,
        "spoof_session_id": session.strip() or None,
        "spoof_timestamp": timestamp,
        "spoof_ot_family": ot_family,
        "spoof_client_ip": client_ip.strip() or None,
    }


def build_backend(source, output, go="go", spoof_user_agent=None,
                  spoof_headers=None, spoof_session_id=None, spoof_timestamp=False,
                  spoof_ot_family=False, spoof_client_ip=None):
    # Opt-in stays off by default; CODEARTS_IDE_USER_AGENT / CODEARTS_IDE_HEADERS
    # / CODEARTS_IDE_SESSION_ID let the CLI and UI enable the IDE header overlay
    # without editing Go sources.
    if spoof_user_agent is not None and not isinstance(spoof_user_agent, str):
        raise TypeError("user_agent must be a string")
    if not isinstance(spoof_timestamp, bool):
        raise TypeError("spoof_timestamp must be true or false")
    if not isinstance(spoof_ot_family, bool):
        raise TypeError("spoof_ot_family must be true or false")
    if spoof_client_ip is not None and not isinstance(spoof_client_ip, str):
        raise TypeError("spoof_client_ip must be a string")
    # A blank explicit value means "not supplied" (the UI clears the field), so
    # the documented environment fallback still applies.
    spoof_user_agent = ((spoof_user_agent or "").strip()
                        or os.environ.get("CODEARTS_IDE_USER_AGENT", "").strip())
    spoof_headers = _header_payload(spoof_headers, "CODEARTS_IDE_HEADERS")
    spoof_session_id = _session_id(spoof_session_id)
    source, output = Path(source).resolve(), Path(output).resolve()
    if "module codearts2api" not in (source / "go.mod").read_text(encoding="utf-8"):
        raise ValueError("Expected the CodeArts2API source directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    from .backend_overlay import model_id_overlay
    from .spoof_overlay import chat_headers_overlay, header_family
    directory = output.parent / "build-overlay"
    overlay = model_id_overlay(source, directory)
    # Opt-in only: nothing configured keeps the current build byte-for-byte. The
    # official static identity table only joins OT-family builds; a UA-only or
    # timestamp-only build keeps exactly the bytes it had before the family.
    spoof = chat_headers_overlay(source, directory / "spoof", spoof_user_agent,
                                 extra_headers=header_family(spoof_headers, spoof_session_id,
                                                             static_family=spoof_ot_family),
                                 with_timestamp=spoof_timestamp,
                                 with_ot_family=spoof_ot_family,
                                 session_id=spoof_session_id or "",
                                 client_ip=spoof_client_ip or "")
    if spoof is not None:
        overlay = _merge_overlays((overlay, spoof), directory / "overlay-merged.json")
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
