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


@unittest.skipUnless(BINARY, "wb-backend 未编译（cargo build --manifest-path wb-backend/Cargo.toml）")
class RustBackendParity(unittest.TestCase):
    def test_parity_with_python_backend(self):
        import compare
        fixture = compare.Fixture(rust_binary=BINARY)
        try:
            python_side = compare.capture(fixture.base)
            rust_side = compare.capture(fixture.rust, include_stream=False)
            problems = compare.diff(python_side, rust_side)
            problems += compare.diff_usage(fixture.usage_dirs[0], fixture.usage_dirs[1])
        finally:
            fixture.close()
        self.assertEqual(problems, [], "Rust 后端与 Python 后端不一致：\n" + "\n".join(problems))


if __name__ == "__main__":
    unittest.main(verbosity=2)
