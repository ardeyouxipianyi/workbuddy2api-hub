#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""launcher 同端口接手：python → rust → python 一个来回。

这条链路的四个关节，缺一个都不算「可选后端」：
  1. `--supervise` 起来时默认还是 python（零行为变化）；
  2. 面板 `POST /admin/backend` 切到 rust，**同一个端口**换人，客户端无感；
  3. 端口上现在是 rust，面板还在，`/admin/backend`（Rust 侧，走 launcher.sock）能切回 python；
  4. 每次切换都由 launcher 落盘 backend.json 并写审计。

没编译 musl 二进制时跳过（Rust 后端是可选组件）。
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CANDIDATES = [
    os.path.join(REPO, "wb-backend/target/x86_64-unknown-linux-musl/release/wb-backend"),
    os.path.join(REPO, "wb-backend/target/release/wb-backend"),
    os.path.join(REPO, "wb-backend/target/debug/wb-backend"),
]
BINARY = next((p for p in CANDIDATES if os.path.exists(p)), None)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http(base, path, data=None, token=None, timeout=30):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Panel-Token"] = token
    request = urllib.request.Request(
        base + path, data=json.dumps(data).encode() if data is not None else None,
        headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as fh:
            return fh.status, fh.read()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read()
        finally:
            exc.close()


class LegacyHealthIdentity(unittest.TestCase):
    """老发布的 python 的 /health 没有 backend 字段，身份判定不能因此拒绝它。"""

    def test_missing_backend_field_means_python(self):
        sys.path.insert(0, REPO)
        import wb_backend
        is_backend = wb_backend.Supervisor._is_backend
        self.assertTrue(is_backend({"ok": True}, "python"))
        self.assertFalse(is_backend({"ok": True}, "rust"))
        self.assertTrue(is_backend({"ok": True, "backend": "rust"}, "rust"))
        self.assertFalse(is_backend({"ok": True, "backend": "rust"}, "python"))

    def test_status_reports_legacy_python_healthy(self):
        """status() 与 wait_healthy() 必须用同一套身份判定。"""
        sys.path.insert(0, REPO)
        import wb_backend
        sup = wb_backend.Supervisor(REPO, "/tmp", "/tmp", "127.0.0.1", 1, [])
        sup.current = "python"
        sup.started_at = 0
        original = wb_backend.probe
        wb_backend.probe = lambda *a, **k: {"ok": True}
        try:
            self.assertTrue(sup.status()["healthy"])
        finally:
            wb_backend.probe = original


@unittest.skipUnless(BINARY, "wb-backend 未编译（cargo build --release --target x86_64-unknown-linux-musl）")
class LauncherSwitch(unittest.TestCase):
    def test_switch_there_and_back(self):
        tmp = tempfile.mkdtemp(prefix="wb-switch-")
        app = os.path.join(tmp, "app")
        os.makedirs(app)
        for name in os.listdir(REPO):
            if name.endswith(".py") or name in ("backend-capabilities.json", "dashboard.html"):
                shutil.copy2(os.path.join(REPO, name), os.path.join(app, name))
        shutil.copy2(BINARY, os.path.join(app, "wb-backend"))
        os.chmod(os.path.join(app, "wb-backend"), 0o755)

        # 六组标 ported 只为让 launcher 肯起它：功能齐不齐是 capabilities 的事，
        # 这条测的是「链路通不通」。
        caps_path = os.path.join(app, "backend-capabilities.json")
        with open(caps_path, encoding="utf-8") as fh:
            caps = json.load(fh)
        for group in caps.get("groups", {}):
            caps["groups"][group]["ported"] = True
        with open(caps_path, "w", encoding="utf-8") as fh:
            json.dump(caps, fh, ensure_ascii=False, indent=1)

        accounts = os.path.join(tmp, "accounts")
        usage = os.path.join(tmp, "usage")
        os.makedirs(accounts)
        os.makedirs(usage)
        with open(os.path.join(accounts, "backend.json"), "w", encoding="utf-8") as fh:
            json.dump({"backend": "python"}, fh)

        port = free_port()
        base = "http://127.0.0.1:%d" % port
        log_path = os.path.join(tmp, "launcher.log")
        log = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, os.path.join(app, "wb_proxy.py"), "--supervise",
             "--host", "127.0.0.1", "--port", str(port),
             "--accounts-dir", accounts, "--usage-dir", usage,
             "--panel-password", "pw", "--api-key", "switch-test-key"],
            cwd=app, stdout=log, stderr=log)
        try:
            self.assertEqual(self._wait_backend(base, "python")["backend"], "python")
            token = json.loads(http(base, "/panel/login", {"password": "pw"})[1])["token"]
            status = json.loads(http(base, "/admin/backend/status", token=token)[1])
            self.assertTrue(status["supervised"], status)
            self.assertTrue(status["rust"]["available"], status["rust"])

            code, raw = http(base, "/admin/backend", {"backend": "rust"}, token=token)
            self.assertEqual(code, 202, raw)
            self.assertEqual(self._wait_backend(base, "rust")["backend"], "rust")
            self._wait_backend_file(accounts, "rust")

            # 换人之后凭据必须还在：命令行上的 --api-key/--panel-password 由 launcher
            # 转成 WB_* 环境变量交给 rust 子进程，否则切过去就变成门大开。
            self.assertEqual(http(base, "/panel/login", {"password": "wrong"})[0], 401)
            self.assertEqual(http(base, "/settings")[0], 401)
            self.assertEqual(http(base, "/v1/models")[0], 401)
            self.assertEqual(http(base, "/v1/models", token="")[0], 401)
            request = urllib.request.Request(
                base + "/v1/models", headers={"Authorization": "Bearer switch-test-key"})
            with urllib.request.urlopen(request, timeout=20) as fh:
                self.assertEqual(fh.status, 200)

            # 端口上换成了 rust：面板仍是那一份，切回的按钮走 Rust 的 /admin/backend。
            token = json.loads(http(base, "/panel/login", {"password": "pw"})[1])["token"]
            status = json.loads(http(base, "/admin/backend/status", token=token)[1])
            self.assertTrue(status["supervised"], status)
            code, raw = http(base, "/admin/backend", {"backend": "python"}, token=token)
            self.assertEqual(code, 202, raw)
            self.assertEqual(self._wait_backend(base, "python")["backend"], "python")
            self._wait_backend_file(accounts, "python")

            audit = os.path.join(usage, "backend-switch.jsonl")
            with open(audit, encoding="utf-8") as fh:
                self.assertEqual(len([l for l in fh.read().splitlines() if l.strip()]), 4)
        except Exception:
            with open(log_path, encoding="utf-8", errors="replace") as fh:
                print(fh.read()[-3000:])
            raise
        finally:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=25)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.close()
            # 端口必须跟着放开：子进程没被带走的话下一次启动会抢不到端口。
            deadline = time.time() + 10
            while time.time() < deadline:
                probe = socket.socket()
                try:
                    probe.connect(("127.0.0.1", port))
                    time.sleep(0.2)
                except OSError:
                    break
                finally:
                    probe.close()
            shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def _wait_backend(base, backend, timeout=40):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                last = json.loads(http(base, "/health")[1])
                if last.get("backend") == backend:
                    return last
            except Exception:
                pass
            time.sleep(0.3)
        raise AssertionError("等不到 backend=%s，最后看到 %s" % (backend, last))

    @staticmethod
    def _wait_backend_file(accounts, backend, timeout=30):
        deadline = time.time() + timeout
        saved = {}
        while time.time() < deadline:
            with open(os.path.join(accounts, "backend.json"), encoding="utf-8") as fh:
                saved = json.load(fh)
            if saved.get("backend") == backend:
                return
            time.sleep(0.2)
        raise AssertionError("backend.json 没落到 %s：%s" % (backend, saved))


if __name__ == "__main__":
    unittest.main()
