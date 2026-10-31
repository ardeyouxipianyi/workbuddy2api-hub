#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对话并发闸门（Python 的 `_chat_slots`）：满了要 503，不满要放行。

两件事，缺一件都不算「有闸门」：
  1. 上限占满时，后来的请求立刻拿到 503，报文和 `gateway_hint` 与 Python 同形；
  2. 没占满时，请求照常打到上游并正常返回（闸门不是只会挡）。

没编译 musl 二进制时跳过（Rust 后端是可选组件）。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "harness"))
import fake_upstream  # noqa: E402

CANDIDATES = [
    os.path.join(REPO, "wb-backend/target/x86_64-unknown-linux-musl/release/wb-backend"),
    os.path.join(REPO, "wb-backend/target/release/wb-backend"),
    os.path.join(REPO, "wb-backend/target/debug/wb-backend"),
]
BINARY = next((p for p in CANDIDATES if os.path.exists(p)), None)
BUSY = ("gateway is at its concurrent chat limit (1 in flight); retry shortly")
HINT = "gateway is busy at its concurrency limit; retry shortly"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def post_chat(base, tag, timeout=30):
    body = {"model": "deepseek-v4.1-flash",
            "messages": [{"role": "user", "content": "slot probe " + tag}]}
    request = urllib.request.Request(
        base + "/v1/chat/completions?realm=cn",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as fh:
            return fh.status, fh.read().decode()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read().decode()
        finally:
            exc.close()


class ChatSlots(unittest.TestCase):
    def setUp(self):
        if BINARY is None:
            self.skipTest("没有 wb-backend 二进制")
        self.tmp = tempfile.mkdtemp(prefix="wb-slots-")
        self.accounts, self.usage = fake_upstream.bootstrap(self.tmp)
        self.procs = []
        self.logs = []
        self.httpd = None

    def tearDown(self):
        for proc in self.procs:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        for handle in self.logs:
            handle.close()
        if self.httpd is not None:
            self.httpd.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def gateway(self, upstream, cap="1", wait="0"):
        port = free_port()
        env = dict(os.environ, WB_CN_CHAT_UPSTREAM=upstream,
                   WB_MAX_CONCURRENT_CHAT=cap, WB_CHAT_SLOT_WAIT=wait)
        log = open(os.path.join(self.tmp, "gateway.log"), "w")
        proc = subprocess.Popen(
            [BINARY, "--host", "127.0.0.1", "--port", str(port),
             "--accounts-dir", self.accounts, "--usage-dir", self.usage],
            env=env, stdout=log, stderr=log)
        self.procs.append(proc)
        self.logs.append(log)
        self.log = log.name
        base = "http://127.0.0.1:%d" % port
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(base + "/health", timeout=1) as fh:
                    if json.loads(fh.read()).get("accounts_ready"):
                        return base
            except Exception:
                pass
            time.sleep(0.2)
        with open(self.log, encoding="utf-8", errors="replace") as fh:
            self.fail("网关没起来：" + fh.read()[-1500:])

    def test_full_pool_answers_503_with_the_python_wording(self):
        # 只收不回的假上游：占着槽不放，闸门才有机会满。
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        upstream = "http://127.0.0.1:%d" % listener.getsockname()[1]

        def hold():
            while True:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                threading.Thread(target=lambda c: time.sleep(15), args=(conn,),
                                 daemon=True).start()

        threading.Thread(target=hold, daemon=True).start()
        base = self.gateway(upstream)

        first = {}

        def take_slot():
            first["result"] = post_chat(base, "holder", timeout=60)

        threading.Thread(target=take_slot, daemon=True).start()
        time.sleep(0.8)  # 让第一条真的把唯一的槽占住

        code, raw = post_chat(base, "late")
        self.assertEqual(code, 503, raw)
        body = json.loads(raw)["error"]
        self.assertEqual(body["message"], BUSY)
        self.assertEqual(body["type"], "server_error")
        self.assertEqual(body["code"], 503)
        self.assertEqual(body["gateway_hint"], HINT)
        listener.close()

    def test_a_lone_request_goes_through(self):
        up_port = free_port()
        self.httpd = fake_upstream.serve(up_port, None)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        base = self.gateway("http://127.0.0.1:%d" % up_port, wait="5")

        code, raw = post_chat(base, "solo")
        self.assertEqual(code, 200, raw[:400])
        self.assertIn("choices", json.loads(raw))


if __name__ == "__main__":
    unittest.main(verbosity=2)
