#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rust 后端与 Python 后端的对拍（M1：/health、/v1/models、非流式 chat、usage 行）。

没有编译好的二进制时跳过：Rust 后端是可选组件，装了 rustc 才有。
    cargo build --manifest-path wb-backend/Cargo.toml
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "harness"))

CANDIDATES = [
    os.path.join(REPO, "wb-backend/target/debug/wb-backend"),
    os.path.join(REPO, "wb-backend/target/release/wb-backend"),
    os.path.join(REPO, "wb-backend/target/x86_64-unknown-linux-musl/release/wb-backend"),
]
BINARY = next((p for p in CANDIDATES if os.path.exists(p)), None)

#: 明知还没移植的项。列在这里的东西不会让用例红，但**已经对上了就必须删掉**
#: （用例会报「把它们从 *_TODO 里删掉」），免得这份清单慢慢变成永远为真的假话。
# 协议面已经全绿；这张表留着给下一批缺口用，空着就说明一条都不欠。
PROTOCOL_TODO = set()
PANEL_TODO = set()


def _name(problem):
    return problem.split(":", 1)[0].strip()


@unittest.skipUnless(BINARY, "wb-backend 未编译（cargo build --manifest-path wb-backend/Cargo.toml）")
class RustBackendParity(unittest.TestCase):
    def test_parity_with_python_backend(self):
        import compare
        fixture = compare.Fixture(rust_binary=BINARY)
        try:
            python_side = compare.capture(fixture.base, panel=True)
            rust_side = compare.capture(fixture.rust, include_stream=False, panel=True)
            found = compare.diff(python_side, rust_side)
            found += compare.diff_usage(fixture.usage_dirs[0], fixture.usage_dirs[1])
            panel = compare.diff_panel(python_side, rust_side)
        finally:
            fixture.close()
        total = found + panel
        todo = PROTOCOL_TODO | PANEL_TODO
        problems = [p for p in total if _name(p) not in todo]
        # 清单里写着「还没移植」但其实已经对上的项要删掉，否则这份清单会慢慢失真。
        fixed = sorted(todo - {_name(p) for p in total})
        self.assertEqual(fixed, [], "这些已经对上了，把它们从 *_TODO 里删掉：" + "、".join(fixed))
        self.assertEqual(problems, [], "Rust 后端与 Python 后端不一致：\n" + "\n".join(problems))


@unittest.skipUnless(BINARY, "wb-backend 未编译（cargo build --manifest-path wb-backend/Cargo.toml）")
class RustBodyLimit(unittest.TestCase):
    """413 的报文形状（specs/http-surface.md §413）：独立的进程，改小上限即可验。"""

    def test_payload_too_large(self):
        import json
        import socket
        import subprocess
        import tempfile
        import time
        import urllib.error
        import urllib.request

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        tmp = tempfile.mkdtemp(prefix="wb-limit-")
        env = dict(os.environ, WB_MAX_PAYLOAD_BYTES="1024")
        proc = subprocess.Popen([BINARY, "--host", "127.0.0.1", "--port", str(port),
                                 "--accounts-dir", tmp, "--usage-dir", tmp],
                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=1).read()
                    break
                except Exception:
                    time.sleep(0.1)
            body = b'{"model":"m","messages":[' + b'{"role":"user","content":"x"},' * 200 + b']}'
            req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % port,
                                         data=body, headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(req, timeout=10)
            self.assertEqual(caught.exception.code, 413)
            payload = json.loads(caught.exception.read())["error"]
            self.assertIn("payload too large (", payload["message"])
            self.assertIn("> 1024 limit)", payload["message"])
            self.assertEqual(payload["code"], 413)
            self.assertIn("WB_MAX_PAYLOAD_BYTES", payload["gateway_hint"])
        finally:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
