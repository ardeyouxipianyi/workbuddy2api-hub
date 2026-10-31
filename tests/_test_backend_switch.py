"""可切换后端：launcher 的端口归属、健康闸门与回退语义。

这一层最容易出的错不是"切不过去"，而是"看起来切过去了"：

* 旧进程还在答 /health 时，一个裸 200 会被当成新后端已就绪；
* 切换请求本身算在飞请求，同步等待会把自己锁死；
* 新后端起不来却已落盘 ``backend.json``，下次重启就再也起不来。

所以这里跑的是真进程：launcher + 真 wb_proxy.py + 一个桩 Rust 后端，
断言全落在 /health、backend.json、backend-switch.jsonl 与 launcher socket 上。

    python tests/_test_backend_switch.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import wb_backend  # noqa: E402

STUB = '''#!/usr/bin/env python3
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
port = int(sys.argv[sys.argv.index("--port") + 1])
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"ok": True, "backend": "rust", "inflight": 0,
                           "version": "stub"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass
ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
'''


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def caps(ported):
    groups = {k: {"label": k, "ported": ported} for k in
              ("protocols", "accounts", "pool", "panel", "tasks", "ops")}
    return {"version": "0.0.0-test", "binary": "wb-backend", "groups": groups}


class Case(object):
    """一个临时 app 目录 + 一个 launcher 实例。"""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="wb-switch-")
        self.app = os.path.join(self.tmp, "app")
        self.accounts = os.path.join(self.tmp, "accounts")
        self.usage = os.path.join(self.tmp, "usage")
        os.makedirs(self.app)
        os.makedirs(self.accounts)
        os.makedirs(self.usage)
        for name in os.listdir(ROOT):
            if name.endswith(".py") and name.startswith("wb_") or name in ("dashboard.html",):
                os.symlink(os.path.join(ROOT, name), os.path.join(self.app, name))
        self.port = free_port()
        self.proc = None
        self.write_caps(False)

    # ---- 装配 ------------------------------------------------------------
    def write_caps(self, ported, with_binary=True):
        with open(os.path.join(self.app, "backend-capabilities.json"), "w") as fh:
            json.dump(caps(ported), fh)
        binary = os.path.join(self.app, "wb-backend")
        if with_binary:
            if os.path.exists(binary):
                os.unlink(binary)
            with open(binary, "w") as fh:
                fh.write(STUB)
            os.chmod(binary, 0o755)

    def start(self, attempts=3):
        """起 launcher；并行跑套件时端口可能被别的套件抢走，所以换端口重试。"""
        last = None
        for attempt in range(attempts):
            if attempt:
                self.port = free_port()
            argv = [sys.executable, os.path.join(self.app, "wb_proxy.py"), "--supervise",
                    "--host", "127.0.0.1", "--port", str(self.port),
                    "--accounts-dir", self.accounts, "--usage-dir", self.usage,
                    "--panel-password", "pw"]
            self.log = open(os.path.join(self.tmp, "launcher-%d.log" % attempt), "w")
            self.proc = subprocess.Popen(argv, cwd=self.app, stdout=self.log, stderr=self.log)
            try:
                self.wait_health("python")
                return self
            except AssertionError as exc:
                last = exc
                if self.proc.poll() is None:
                    self.proc.terminate()
                    self.proc.wait(timeout=15)
                self.log.close()
        raise last

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if self.log is not None:
            self.log.close()

    def cleanup(self):
        self.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 观测 ------------------------------------------------------------
    def health(self, timeout=2.0):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/health" % self.port, timeout=timeout) as fh:
                return json.loads(fh.read())
        except Exception:
            return None

    def wait_health(self, backend, timeout=40.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            payload = self.health(0.5)
            if payload and payload.get("backend") == backend:
                return payload
            time.sleep(0.1)
        raise AssertionError("backend %r 没在 %.0fs 内就位；最后一条 /health = %r；日志：\n%s"
                             % (backend, timeout, self.health(), self.read_log()))

    def read_log(self):
        try:
            name = os.path.basename(self.log.name) if self.log else "launcher-0.log"
            with open(os.path.join(self.tmp, name), encoding="utf-8", errors="replace") as fh:
                return fh.read()[-4000:]
        except OSError:
            return ""

    def token(self):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/panel/login" % self.port,
            data=json.dumps({"password": "pw"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as fh:
            return json.loads(fh.read())["token"]

    def get(self, path, token=None, timeout=10):
        headers = {"X-Panel-Token": token} if token else {}
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as fh:
                return fh.status, json.loads(fh.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def post(self, path, payload, token=None, timeout=10):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Panel-Token"] = token
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                     data=json.dumps(payload).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as fh:
                return fh.status, json.loads(fh.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def control(self, payload, timeout=120.0):
        """直接问 launcher：新后端不实现控制面时，这是唯一权威来源。"""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(os.path.join(self.accounts, wb_backend.SOCK_FILE))
        try:
            sock.sendall((json.dumps(payload) + "\n").encode())
            raw = sock.makefile("rb").readline(1 << 20)
        finally:
            sock.close()
        return json.loads(raw)

    def backend_file(self):
        with open(os.path.join(self.accounts, wb_backend.BACKEND_FILE), encoding="utf-8") as fh:
            return json.load(fh)

    def wait_backend_file(self, name, timeout=15.0):
        """/health 会先于落盘翻到新后端，所以断言设置要另等一会儿。

        这个顺序是故意的：先健康、后落盘，进程死在中间也只是下次仍起旧后端。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.backend_file()["backend"] == name:
                    return True
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.1)
        raise AssertionError("backend.json 没在 %.0fs 内变成 %r；日志：\n%s"
                             % (timeout, name, self.read_log()))

    def audit_events(self):
        path = os.path.join(self.usage, wb_backend.AUDIT_FILE)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


class CapabilitiesTests(unittest.TestCase):
    def test_missing_lists_human_labels(self):
        tmp = tempfile.mkdtemp(prefix="wb-caps-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "backend-capabilities.json"), "w") as fh:
            json.dump({"groups": {"protocols": {"label": "协议面", "ported": True},
                                  "tasks": {"label": "自动化", "ported": False}}}, fh)
        binary = os.path.join(tmp, "wb-backend")
        with open(binary, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(binary, 0o755)
        caps = wb_backend.capabilities(tmp)
        self.assertEqual(caps["missing"], ["自动化"])
        self.assertFalse(caps["ported"])

    def test_no_caps_file_means_not_ready(self):
        tmp = tempfile.mkdtemp(prefix="wb-caps-empty-")
        self.addCleanup(shutil.rmtree, tmp, True)
        caps = wb_backend.capabilities(tmp)
        self.assertFalse(caps["ported"])
        self.assertFalse(caps["binary"]["present"])

    def test_backend_file_roundtrip_defaults_to_python(self):
        tmp = tempfile.mkdtemp(prefix="wb-backend-json-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertEqual(wb_backend.read_backend(tmp), "python")
        wb_backend.write_backend(tmp, "rust", reason="test")
        self.assertEqual(wb_backend.read_backend(tmp), "rust")
        with open(os.path.join(tmp, wb_backend.BACKEND_FILE), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["reason"], "test")


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.case = Case()
        self.addCleanup(self.case.cleanup)
        self.case.start()

    def test_health_carries_identity_and_inflight(self):
        payload = self.case.health()
        self.assertEqual(payload["backend"], "python")
        self.assertIsInstance(payload["inflight"], int)
        self.assertIsInstance(payload["pid"], int)
        self.assertTrue(payload["version"])

    def test_status_without_launcher_is_degraded_not_an_error(self):
        # 单进程起法（旧启动脚本）没有可切换的对象，面板要显示「不可用」。
        port = free_port()
        solo_accounts = os.path.join(self.case.tmp, "solo-accounts")
        solo_usage = os.path.join(self.case.tmp, "solo-usage")
        os.makedirs(solo_accounts)
        os.makedirs(solo_usage)
        argv = [sys.executable, os.path.join(self.case.app, "wb_proxy.py"),
                "--host", "127.0.0.1", "--port", str(port),
                "--accounts-dir", solo_accounts, "--usage-dir", solo_usage,
                "--panel-password", "pw"]
        log = open(os.path.join(self.case.tmp, "solo.log"), "w")
        proc = subprocess.Popen(argv, cwd=self.case.app, stdout=log, stderr=log)
        self.addCleanup(lambda: (proc.terminate(), proc.wait(timeout=15), log.close()))
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=1) as fh:
                    if json.loads(fh.read()).get("backend") == "python":
                        break
            except Exception:
                time.sleep(0.1)
        req = urllib.request.Request("http://127.0.0.1:%d/panel/login" % port,
                                     data=json.dumps({"password": "pw"}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as fh:
            token = json.loads(fh.read())["token"]
        req = urllib.request.Request("http://127.0.0.1:%d/admin/backend/status" % port,
                                     headers={"X-Panel-Token": token})
        with urllib.request.urlopen(req, timeout=10) as fh:
            payload = json.loads(fh.read())
        self.assertFalse(payload["supervised"])
        self.assertEqual(payload["backend"], "python")

    def test_status_needs_panel_session(self):
        code, body = self.case.get("/admin/backend/status")
        self.assertEqual(code, 401)

    def test_switch_refused_while_rust_is_not_ported(self):
        token = self.case.token()
        code, payload = self.case.post("/admin/backend", {"backend": "rust"}, token=token)
        self.assertEqual(code, 409)
        self.assertIn("protocols", payload["error"]["message"])
        self.assertEqual(self.case.health()["backend"], "python")
        # 端口没换手、设置没被写：这一条是「切换失败不留半切状态」的判据。
        self.assertFalse(os.path.exists(os.path.join(self.case.accounts, wb_backend.BACKEND_FILE)))

    def test_switch_to_rust_and_back(self):
        self.case.write_caps(True)
        token = self.case.token()
        code, payload = self.case.post("/admin/backend", {"backend": "rust"}, token=token)
        self.assertEqual(code, 202, payload)
        self.case.wait_health("rust")
        self.assertTrue(self.case.wait_backend_file("rust"))
        # 切回来：此时面板由桩后端服务，控制面只能走 launcher socket。
        result = self.case.control({"cmd": "switch", "to": "python"})
        self.assertTrue(result["ok"], result)
        self.case.wait_health("python")
        self.assertTrue(self.case.wait_backend_file("python"))
        events = [row["event"] for row in self.case.audit_events()]
        self.assertEqual(events.count("switch_ok"), 2, events)

    def test_failed_switch_rolls_back_to_python(self):
        self.case.write_caps(True, with_binary=False)
        with open(os.path.join(self.case.app, "wb-backend"), "w") as fh:
            fh.write("#!/bin/sh\nexit 1\n")
        os.chmod(os.path.join(self.case.app, "wb-backend"), 0o755)
        # 先落一次盘，回滚的判据才是「设置保持原样」而不是「文件本来就不存在」。
        wb_backend.write_backend(self.case.accounts, "python", reason="seed")
        wb_backend.START_TIMEOUT = 3.0      # 桩后端立刻退出，别等满 30s
        self.addCleanup(setattr, wb_backend, "START_TIMEOUT", 30.0)
        result = self.case.control({"cmd": "switch", "to": "rust"})
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["backend"], "python")
        self.assertEqual(self.case.health()["backend"], "python")
        self.assertEqual(self.case.backend_file()["backend"], "python")

    def test_startup_falls_back_when_rust_is_not_ready(self):
        # 上次切到 rust 之后二进制被更新删掉了：launcher 必须回落并在启动时落盘。
        wb_backend.write_backend(self.case.accounts, "rust", reason="stale")
        self.case.stop()
        self.case.write_caps(False, with_binary=False)
        self.case.start()
        self.assertEqual(self.case.health()["backend"], "python")
        self.assertEqual(self.case.backend_file()["backend"], "python")
        events = [row["event"] for row in self.case.audit_events()]
        self.assertIn("fallback", events)


if __name__ == "__main__":
    unittest.main()
