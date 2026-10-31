#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diff two gateway backends on the same requests, without spending credits.

    python3 harness/compare.py --self-check
    python3 harness/compare.py --base http://127.0.0.1:8090 --base http://127.0.0.1:8091

``--self-check`` is the harness's own test: it boots a throwaway gateway against
``fake_upstream.py``, captures every case twice and asserts the two captures are
identical (so the diff is not noise), then flips one upstream answer and asserts
the diff *does* fire (so a comparison can actually fail).

What gets compared is the body after normalisation (ids, timestamps and
fingerprints removed) plus the ordered SSE frames. Status codes are compared
verbatim.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import fake_upstream  # noqa: E402

MODEL = "deepseek-v4.1-flash"
#: 每条 case 是一组请求；加 case 就加一行，覆盖协议形状而不是覆盖业务分支。
CASES = [
    ("chat.plain", {"model": MODEL, "max_tokens": 32,
                    "messages": [{"role": "user", "content": "hi"}]}),
    ("chat.system", {"model": MODEL, "max_tokens": 32,
                     "messages": [{"role": "system", "content": "be brief"},
                                  {"role": "user", "content": "hi"}]}),
    ("chat.stream", {"model": MODEL, "max_tokens": 32, "stream": True,
                     "messages": [{"role": "user", "content": "hi"}]}),
    ("chat.tools", {"model": MODEL, "max_tokens": 32,
                    "messages": [{"role": "user", "content": "weather?"}],
                    "tools": [{"type": "function", "function": {
                        "name": "get_weather", "description": "weather",
                        "parameters": {"type": "object", "properties": {}}}}]}),
]
VOLATILE = re.compile(r'"(id|created|system_fingerprint|first_chunk_at|at|iso|elapsed_ms|'
                      r'ttft_ms|gen_ms)"\s*:\s*("[^"]*"|[0-9.]+)')


def normalise(text):
    return VOLATILE.sub(r'"\1":<volatile>', text)


def call(base, case, body, timeout=60):
    url = base.rstrip("/") + "/v1/chat/completions?realm=cn"
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, raw
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def capture(base):
    out = {}
    for name, body in CASES:
        status, raw = call(base, name, body)
        if raw.lstrip().startswith("data:"):
            frames = [normalise(f) for f in raw.split("\n\n") if f.strip()]
            out[name] = {"status": status, "frames": frames}
        else:
            out[name] = {"status": status, "body": normalise(raw)}
    return out


def diff(a, b):
    problems = []
    for name, _ in CASES:
        left, right = a.get(name), b.get(name)
        if left == right:
            continue
        if left["status"] != right["status"]:
            problems.append("%s: status %s != %s" % (name, left["status"], right["status"]))
            continue
        if "frames" in left:
            for i, (lf, rf) in enumerate(zip(left["frames"], right["frames"])):
                if lf != rf:
                    problems.append("%s: frame %d differs\n  A: %s\n  B: %s" % (name, i, lf, rf))
                    break
            else:
                problems.append("%s: frame count %d != %d"
                                % (name, len(left["frames"]), len(right["frames"])))
        else:
            problems.append("%s: body differs\n  A: %s\n  B: %s"
                            % (name, left.get("body"), right.get("body")))
    return problems


class Fixture(object):
    """假上游 + 一个临时网关（默认 python 后端）。"""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="wb-compare-")
        self.upstream_port = _free_port()
        self.gateway_port = _free_port()
        accounts, usage = fake_upstream.bootstrap(self.tmp)
        self.httpd = fake_upstream.serve(self.upstream_port, None)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        env = dict(os.environ)
        env.update({"ACCOUNTS_DIR": accounts, "WB_PROXY_USAGE_DIR": usage,
                    "WB_CN_CHAT_UPSTREAM": "http://127.0.0.1:%d" % self.upstream_port})
        self.log = open(os.path.join(self.tmp, "gateway.log"), "w")
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "wb_proxy.py"), "--serve",
             "--host", "127.0.0.1", "--port", str(self.gateway_port)],
            cwd=ROOT, env=env, stdout=self.log, stderr=self.log)
        self.base = "http://127.0.0.1:%d" % self.gateway_port
        self.wait_ready()

    def wait_ready(self, timeout=40.0):
        # 账号池在启动后异步载入：等 ok 且 accounts_ready>0，否则第一条请求会
        # 撞上「no usable account」，两次抓取自然对不上。
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(self.base + "/health", timeout=1) as fh:
                    payload = json.loads(fh.read())
                if payload.get("ok") and payload.get("accounts_ready"):
                    return
            except Exception:
                pass
            time.sleep(0.2)
        raise AssertionError("网关没在 %.0fs 内就绪；日志：\n%s" % (timeout, self.read_log()))

    def read_log(self):
        try:
            with open(os.path.join(self.tmp, "gateway.log"), encoding="utf-8",
                      errors="replace") as fh:
                return fh.read()[-3000:]
        except OSError:
            return ""

    def close(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=15)
        except Exception:
            self.proc.kill()
        self.httpd.shutdown()
        self.log.close()


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def self_check():
    fixture = Fixture()
    try:
        first = capture(fixture.base)
        second = capture(fixture.base)
        # 请求必须落在假上游上：落到真上游既烧额度，也说明这个夹具是假的。
        assert fake_upstream.Handler.seen, "请求没打到假上游（可能打到了真上游），比对不可用"
        problems = diff(first, second)
        assert not problems, "同一后端两次抓取就不一致（差值不可用）：%s" % problems
        print("[ok] 同一后端两次抓取逐字节一致（%d 条 case）" % len(CASES))

        # 反向：让假上游答得不一样，diff 必须报错，否则这个比对器永远不会红。
        fake_upstream.completion_body = _mutated(fake_upstream.completion_body)
        fake_upstream.sse_frames = _mutated_sse(fake_upstream.sse_frames)
        third = capture(fixture.base)
        problems = diff(first, third)
        assert problems, "上游答案变了，比对器却没发现"
        print("[ok] 上游答案变化时比对器报差异：%s" % problems[0].splitlines()[0])
    finally:
        fixture.close()
    print("SELF-CHECK PASS")


def _mutated(original):
    """把上游答案换掉，用来证明比对器会红。

    两处都要改：网关对内总是按流式取上游（非流式客户端由它缓冲后再回），所以只改
    ``completion_body`` 时，走流式那一路的答案根本没变，比对器当然不会报差异。
    """
    def patched(model, text="OK", usage=None):
        body = original(model, text, usage)
        body["choices"][0]["message"]["content"] = "DIFFERENT"
        return body
    return patched


def _mutated_sse(original):
    def patched(model, text="OK"):
        return original(model, "DIFFERENT")
    return patched


def main(argv=None):
    ap = argparse.ArgumentParser(description="diff gateway backends on identical requests")
    ap.add_argument("--base", action="append", default=[], help="base URL; pass twice to compare")
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args(argv)
    if args.self_check:
        self_check()
        return 0
    if len(args.base) < 2:
        ap.error("give --base twice (A and B), or use --self-check")
    a, b = capture(args.base[0]), capture(args.base[1])
    problems = diff(a, b)
    for line in problems:
        print("[diff] " + line)
    print("%d/%d cases identical" % (len(CASES) - len(problems), len(CASES)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
