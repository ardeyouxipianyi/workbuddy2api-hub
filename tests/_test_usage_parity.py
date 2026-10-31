#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据层对拍：同一个 usage 目录，rust 与 python 的响应必须逐字段相等。

这不是「跑通」测试，而是口径测试 —— rust 重写了面板的取数与聚合，只要两边对同一
份数据给出不同数字，用户看到的就是「今日的数据不对」。所以判据只有一条：同一个
(path, method) 下，去掉少量天然每进程不同的字段后，两边的 JSON 完全一致。

用法：
    /usr/bin/python3 tests/_test_usage_parity.py           # 跑矩阵
    WB_PARITY_REPORT=1 ... tests/_test_usage_parity.py      # 只打印差异表，不判失败

没编译 rust 二进制时跳过（rust 是可选后端）。逐字段对齐是一波一波推进的，已经
登记的差异记在 KNOWN_DIFFS 里；出现登记之外的差异才算回归，这样 CI 全程有牙。
"""
import json
import math
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

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CANDIDATES = [
    os.path.join(REPO, "wb-backend/target/x86_64-unknown-linux-musl/release/wb-backend"),
    os.path.join(REPO, "wb-backend/target/release/wb-backend"),
    os.path.join(REPO, "wb-backend/target/debug/wb-backend"),
]
BINARY = next((path for path in CANDIDATES if os.path.exists(path)), None)

# 每个进程天然不同的字段：pid、启动时刻、日志路径、文件时间戳。
VOLATILE = {"pid", "started", "log_file", "updated_at", "iso", "fetched_at",
            "next_run", "next_run_label", "uptime", "storage", "running",
            "policies_file", "overrides_file"}
# 这些键是「当前时刻算出来的窗口边界」，两边取值相差几秒是正常的。
SOFT_TIME = ("since", "until", "at", "mtime", "last_")

# 端点矩阵：面板「数据看板」实际会打的那几条，加上各自的分支参数。
MATRIX = [
    "/usage?realm=all&range=today",
    "/usage?realm=all&range=week",
    "/usage?realm=all&range=month",
    "/usage?realm=cn&range=today",
    "/usage/perf?realm=all&range=today",
    "/usage/perf?realm=all&range=month",
    "/usage/analytics?realm=all&range=today",
    "/usage/analytics?realm=all&range=month",
    "/usage/timeseries?realm=all&range=today",
    "/usage/timeseries?realm=all&range=week",
    "/usage/timeseries?realm=all&range=month",
    "/usage/by-account",
    "/usage/remaining",
    "/usage/recent?limit=20",
    "/accounts",
    "/pricing",
]

# 已登记的口径差异：{endpoint: {字段名}}，字段名取路径最后一段（去掉 [n] 下标），
# 这样与具体模型/账号无关。{"*"} 表示这个接口还没开始对齐。
# 修掉一批就把它从表里删掉 —— 表变小才是进度，剩下的差异才叫回归。
KNOWN_DIFFS = {
    # rows/models 需要 Python 的 cap 来源（limit-events + model_caps），尚未移植；
    # floor 与 iso 已对齐。见 _test_rust_backend.py 的 PANEL_TODO。
    "/usage/remaining": {"rows", "models", "budgets"},
}


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def usage_rows(now):
    """一份小而全的语料：两个 realm、三个模型、两种账号、三种结果。

    时间刻意铺在 today / 本周 / 本月的边界两侧，否则「区间不生效」这类 bug
    在测试里看不出来。金额只用 credit（price 表不放进 fixture，cost_cny 的
    对拍等计价引擎端口径时再加一份带价表的语料）。
    """
    def row(at, model, account, realm, outcome="completed", prompt=1000,
            completion=200, reasoning=0, cached=100, credit=0.1,
            ttft=120, gen=900, elapsed=1100):
        total = prompt + completion + reasoning
        return {
            "at": at,
            "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at)),
            "model": model,
            "account": account,
            "realm": realm,
            "outcome": outcome,
            "stream": True,
            "key": "k1",
            "n_msgs": 3,
            "msgs_sha": "deadbeef",
            "system_sha": "cafe",
            "prefix_sha": "babe",
            "cache_hit_pct": round(cached / prompt * 100, 2) if prompt else 0,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "reasoning_tokens": reasoning,
            "cached_tokens": cached,
            "total_tokens": total,
            "tokens_per_sec": round(completion / (gen / 1000), 2) if gen else 0,
            "credit": credit,
            "ttft_ms": ttft,
            "gen_ms": gen,
            "elapsed_ms": elapsed,
        }

    midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    rows = []
    # 今天：三条模型都覆盖，含一条失败、一条客户端中断。
    rows.append(row(now - 60, "glm-5.3", "acct-a", "cn"))
    rows.append(row(now - 900, "glm-5.3", "acct-a", "cn", prompt=2000, completion=400,
                    reasoning=120, cached=800, credit=0.3, ttft=90, gen=1500,
                    elapsed=1700))
    rows.append(row(now - 1800, "kimi-k2.7", "acct-b", "cn", credit=0.2))
    rows.append(row(now - 2400, "hy3", "acct-b", "intl", prompt=500, completion=80,
                    cached=0, credit=0.05))
    rows.append(row(now - 3000, "hy3", "acct-b", "intl", outcome="error",
                    prompt=0, completion=0, cached=0, credit=0.0, ttft=0, gen=0,
                    elapsed=40))
    rows.append(row(now - 3600, "glm-5.3", "acct-a", "cn", outcome="client_aborted",
                    prompt=700, completion=120, cached=100, credit=0.07))
    # 今天零点之前、仍在昨天：用来分辨「今日」到底从哪儿切。
    rows.append(row(midnight - 120, "glm-5.3", "acct-a", "cn", credit=0.11))
    # 本周内（可能跨到上周末，两边都应该用同一个周一）。
    rows.append(row(now - 3 * 86400, "hy3", "acct-b", "intl", credit=0.4))
    rows.append(row(now - 5 * 86400, "kimi-k2.7", "acct-a", "cn", credit=0.15))
    # 本月内、超出本周。
    rows.append(row(now - 12 * 86400, "glm-5.3", "acct-b", "cn", credit=0.25))
    # 本月之前：只应出现在「全部」里。
    rows.append(row(now - 40 * 86400, "hy3", "acct-a", "intl", credit=0.5))
    return rows


class Backend:
    """一个后端进程 + 它的面板 token，两个后端拿到的是同一份语料的各自副本。"""

    def __init__(self, label, argv, cwd, usage_dir, accounts_dir):
        self.label = label
        self.usage_dir = usage_dir
        self.accounts_dir = accounts_dir
        self.port = free_port()
        argv = [part.format(port=self.port, usage=usage_dir, accounts=accounts_dir)
                for part in argv]
        self.log_path = os.path.join(os.path.dirname(usage_dir), label + ".log")
        self.log = open(self.log_path, "w")
        self.proc = subprocess.Popen(argv, cwd=cwd, stdout=self.log, stderr=self.log,
                                     env={**os.environ, "WB_PANEL_PASSWORD": "pw"})

    def health(self, timeout=1.0):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/health" % self.port,
                                        timeout=timeout) as fh:
                return json.loads(fh.read())
        except Exception:
            return None

    def wait_ready(self, expect, timeout=60.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            payload = self.health(0.5)
            if payload and payload.get("backend") == expect:
                return True
            if self.proc.poll() is not None:
                break
            time.sleep(0.2)
        raise AssertionError("%s 没起来；日志：\n%s"
                             % (self.label, open(self.log_path).read()[-2000:]))

    def token(self):
        request = urllib.request.Request(
            "http://127.0.0.1:%d/panel/login" % self.port,
            data=json.dumps({"password": "pw"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as fh:
            return json.loads(fh.read())["token"]

    def get(self, path, token):
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                         headers={"X-Panel-Token": token})
        try:
            with urllib.request.urlopen(request, timeout=120) as fh:
                return fh.status, json.loads(fh.read())
        except urllib.error.HTTPError as exc:
            return exc.code, {}

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log.close()


def _soft(key):
    return any(piece in key for piece in SOFT_TIME)


def diff(left, right, path="", out=None):
    """逐字段比：数值按容差，时间键按 120 秒，其余要求严格相等。"""
    out = [] if out is None else out
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            if key in VOLATILE:
                continue
            step = path + "/" + key
            if key not in left:
                out.append((step, "<缺>", short(right[key])))
            elif key not in right:
                out.append((step, short(left[key]), "<缺>"))
            else:
                diff(left[key], right[key], step, out)
        return out
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            out.append((path + "[len]", len(left), len(right)))
        for index, (litem, ritem) in enumerate(zip(left, right)):
            diff(litem, ritem, "%s[%d]" % (path, index), out)
        return out
    if isinstance(left, bool) or isinstance(right, bool):
        if left != right:
            out.append((path, short(left), short(right)))
        return out
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if _soft(path):
            if abs(left - right) > 120:
                out.append((path, left, right))
        elif not math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9):
            out.append((path, left, right))
        return out
    if _soft(path) and isinstance(left, str) and isinstance(right, str):
        # 形如 "2026-10-31 22:12:14" 的「本响应生成时刻」：两次请求必然差几秒。
        return out
    if left != right:
        out.append((path, short(left), short(right)))
    return out


def short(value):
    if isinstance(value, (dict, list)):
        return "<%s len=%d>" % (type(value).__name__, len(value))
    text = str(value)
    return text if len(text) <= 48 else text[:45] + "..."


def normalise(body, backend):
    """把响应里指向本后端自己的目录换成占位符。

    两个后端各拿一份语料副本，所以 `timeline`/`log_file` 这类字段里的绝对路径
    必然不同。这是夹具的差异，不是口径差异，比之前要抹掉。
    """
    text = json.dumps(body, ensure_ascii=False)
    for prefix, token in ((backend.usage_dir, "<usage>"), (backend.accounts_dir, "<accounts>")):
        text = text.replace(prefix, token)
    return json.loads(text)


class UsageParity(unittest.TestCase):
    """rust 与 python 对同一份 usage 的每个面板数据接口逐字段相等。"""

    def setUp(self):
        if BINARY is None:
            self.skipTest("没有 rust 二进制（可选后端），跳过对拍")
        self.tmp = tempfile.mkdtemp(prefix="wb-parity-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        now = time.time()
        for label in ("py", "rust"):
            os.makedirs(os.path.join(self.tmp, label, "usage"))
            os.makedirs(os.path.join(self.tmp, label, "accounts"))
            rows = usage_rows(now)
            with open(os.path.join(self.tmp, label, "usage", "usage.jsonl"), "w") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")
            with open(os.path.join(self.tmp, label, "accounts", "settings.json"), "w") as fh:
                json.dump({"api_keys": []}, fh)
        self.py = Backend(
            "py",
            [sys.executable, os.path.join(REPO, "wb_proxy.py"), "--host", "127.0.0.1",
             "--port", "{port}", "--accounts-dir", "{accounts}", "--usage-dir", "{usage}",
             "--panel-password", "pw"],
            REPO, os.path.join(self.tmp, "py", "usage"), os.path.join(self.tmp, "py", "accounts"))
        self.addCleanup(self.py.stop)
        self.rust = Backend(
            "rust",
            [BINARY, "--host", "127.0.0.1", "--port", "{port}", "--accounts-dir",
             "{accounts}", "--usage-dir", "{usage}"],
            REPO, os.path.join(self.tmp, "rust", "usage"),
            os.path.join(self.tmp, "rust", "accounts"))
        self.addCleanup(self.rust.stop)
        self.py.wait_ready("python")
        self.rust.wait_ready("rust")
        self.py_token = self.py.token()
        self.rust_token = self.rust.token()

    def test_panel_data_matches_python_field_by_field(self):
        unexpected = []
        report = []
        for path in MATRIX:
            py_status, py_body = self.py.get(path, self.py_token)
            rust_status, rust_body = self.rust.get(path, self.rust_token)
            if py_status != rust_status:
                unexpected.append("%s: 状态码 python=%s rust=%s"
                                  % (path, py_status, rust_status))
                continue
            found = diff(normalise(rust_body, self.rust), normalise(py_body, self.py))
            known = KNOWN_DIFFS.get(path, set())
            for step, rust_value, py_value in found:
                leaf = step.rsplit("/", 1)[-1].split("[")[0]
                if leaf in known or "*" in known:
                    continue
                unexpected.append("%s%s  rust=%s  python=%s" % (path, step, rust_value, py_value))
            report.append("%-42s 差异 %3d 处%s"
                          % (path, len(found), "（已登记）" if found else ""))
            if os.environ.get("WB_PARITY_SHOW"):
                for step, rust_value, py_value in found[:int(os.environ["WB_PARITY_SHOW"])]:
                    report.append("    rust%s = %s | python = %s" % (step, rust_value, py_value))
        print("\n".join(report))
        if os.environ.get("WB_PARITY_REPORT"):
            raise unittest.SkipTest("WB_PARITY_REPORT：只出报告")
        self.assertEqual(unexpected[:20], [],
                         "口径不一致 %d 处（只列前 20）：\n%s"
                         % (len(unexpected), "\n".join(unexpected[:20])))


if __name__ == "__main__":
    unittest.main()
