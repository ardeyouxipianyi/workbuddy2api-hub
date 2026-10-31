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
#: 每条 case 是 (名字, 路径, body, 是否属于 M1 最小集)；加 case 就加一行，
#: 覆盖协议形状而不是覆盖业务分支。
CHAT = "/v1/chat/completions"
CASES = [
    ("chat.plain", CHAT, {"model": MODEL, "max_tokens": 32,
                          "messages": [{"role": "user", "content": "hi"}]}, True),
    ("chat.system", CHAT, {"model": MODEL, "max_tokens": 32,
                           "messages": [{"role": "system", "content": "be brief"},
                                        {"role": "user", "content": "hi"}]}, True),
    ("chat.stream", CHAT, {"model": MODEL, "max_tokens": 32, "stream": True,
                           "messages": [{"role": "user", "content": "hi"}]}, False),
    # 上游不报 usage：网关要按字面估一条（写作 usage_missing 的那条路径）。
    ("chat.nousage", CHAT, {"model": MODEL, "max_tokens": 32,
                            "messages": [{"role": "user", "content": "NOUSAGE hi"}]}, True),
    # 上游报了缓存命中：网关要把四个别名补齐，usage 行与响应都用同一份。
    ("chat.cached", CHAT, {"model": MODEL, "max_tokens": 32,
                           "messages": [{"role": "user", "content": "WITHCACHE hi"}]}, True),
    ("chat.tools", CHAT, {"model": MODEL, "max_tokens": 32,
                          "messages": [{"role": "user", "content": "weather?"}],
                          "tools": [{"type": "function", "function": {
                              "name": "get_weather", "description": "weather",
                              "parameters": {"type": "object", "properties": {}}}}]}, True),
    # 另外三种协议面：OpenAI completions、Anthropic messages（Claude Code 走这条）、
    # responses（Codex）。每一种都要有自己的请求/响应信封，缺一个就不是「可选后端」。
    ("completions.plain", "/v1/completions",
     {"model": MODEL, "prompt": "hi", "max_tokens": 32}, True),
    ("messages.plain", "/v1/messages",
     {"model": MODEL, "max_tokens": 32,
      "messages": [{"role": "user", "content": "hi"}]}, True),
    ("messages.count", "/v1/messages/count_tokens",
     {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}, True),
    ("responses.plain", "/v1/responses",
     {"model": MODEL, "max_tokens": 32, "input": "hi"}, True),
]
#: GET 探测点：只读路由，两个后端都必须给出同一份字节。
PROBES = ["/health", "/v1/models", "/v1/usage", "/v1/balance-summary"]

#: 面板只读路由：面板页面加载时真会调的那些。放在 chat 用例之后抓，
#: 这样两侧的 /usage* 都刚好看见自己那 6 条请求（各自一个 usage 目录）。
#: `--self-check` 不比这一组：同一后端抓两次，第二次的用量已经翻倍，
#: 那是夹具的账，不是契约的错。
PANEL_PROBES = [
    "/realm", "/accounts", "/settings", "/scheduler", "/tasks",
    "/agents", "/agents/available", "/agents/model-selection",
    "/pricing", "/proxy/slots", "/activity/history", "/updates",
    "/usage", "/usage/recent", "/usage/perf", "/usage/analytics",
    "/usage/by-account", "/usage/timeseries", "/usage/remaining",
]

#: 每次运行都不同、或按设计就该不同的键：比较前整个删掉。
#: `backend`/`version` 是「谁在服务」的自述，跨后端比较时必须忽略；其余是时间戳。
VOLATILE_KEYS = {
    "id", "created", "system_fingerprint", "first_chunk_at", "at", "iso",
    "elapsed_ms", "ttft_ms", "gen_ms", "fetched_at", "file_mtime", "pid",
    "expires_at", "backend", "version", "created_at",
}


#: 由耗时推导出来的值：字段该不该出现是契约，具体数字不是（同一请求两次跑都不一样）。
RATE_KEYS = {"tokens_per_sec"}


def sanitise(obj):
    """递归删掉易变键；JSON 结构比较按对象比，不按字节比。

    之前按字节比，Rust 得去模仿 Python `json.dumps` 的 `", "` 分隔符才「相等」——
    那是给机器看的分隔符，不是契约。真正要盯的是字段、类型、取值和顺序。
    """
    if isinstance(obj, dict):
        return {k: ("<derived>" if k in RATE_KEYS else sanitise(v))
                for k, v in obj.items() if k not in VOLATILE_KEYS}
    if isinstance(obj, list):
        return [sanitise(v) for v in obj]
    return obj


def parse(raw):
    try:
        return sanitise(json.loads(raw))
    except ValueError:
        return sanitise(raw)


def probe(base, path, timeout=60, token=None):
    req = urllib.request.Request(base.rstrip("/") + path)
    if token:
        req.add_header("X-Panel-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def panel_login(base):
    """面板默认口令是 admin（wb_settings.DEFAULT_PANEL_PASSWORD），夹具里没改过。

    返回 X-Panel-Token；拿不到就退化成无 token（面板路由会 401，报告里能看出来）。
    """
    req = urllib.request.Request(
        base.rstrip("/") + "/panel/login",
        data=json.dumps({"password": "admin"}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode()).get("token") or ""
    except Exception:
        return ""


def call(base, case, body, timeout=90):
    # ?realm=cn：夹具账号是 cn，默认区域是 intl，不指定就取不到号。
    sep = "&" if "?" in case else "?"
    url = base.rstrip("/") + case + sep + "realm=cn"
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def capture(base, include_stream=True, skip=(), panel=False):
    out = {}
    for path in PROBES:
        status, raw = probe(base, path)
        out["GET " + path] = {"status": status, "body": parse(raw)}
    for name, path, body, m1 in CASES:
        if name in skip or (not include_stream and not m1):
            continue
        status, raw = call(base, path, body)
        if raw.lstrip().startswith("data:"):
            frames = [parse(f[len("data:"):].strip()) for f in raw.split("\n\n") if f.strip()]
            out[name] = {"status": status, "frames": frames}
        else:
            out[name] = {"status": status, "body": parse(raw)}
    if panel:
        token = panel_login(base)
        for path in PANEL_PROBES:
            status, raw = probe(base, path, token=token)
            out["panel:" + path] = {"status": status, "body": parse(raw)}
    return out


def shape(obj, depth=0):
    """键集合与类型，忽略取值（面板 payload 的值是运行时状态）。"""
    if isinstance(obj, dict):
        return {k: shape(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        if not obj:
            return []
        inner = [shape(v, depth + 1) for v in obj]
        merged = {}
        for item in inner:
            if not isinstance(item, dict):
                return [item]
            for k, v in item.items():
                merged[k] = v
        return [merged]
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        return "bool"
    if isinstance(obj, (int, float)):
        return "number"
    return "string"


def _shape_delta(a, b, prefix=""):
    """把两棵 shape 树的差异压成一行行 `路径: py=X rs=Y` / `缺键`。"""
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in a:
            here = prefix + "." + k if prefix else k
            if k not in b:
                out.append("%s: 缺键" % here)
            else:
                out += _shape_delta(a[k], b[k], here)
        for k in b:
            if k not in a:
                out.append("%s: 多出" % (prefix + "." + k if prefix else k))
        return out
    if a != b:
        out.append("%s: py=%s rs=%s" % (prefix, json.dumps(a, ensure_ascii=False)[:60],
                                        json.dumps(b, ensure_ascii=False)[:60]))
    return out


def diff_panel(a, b):
    """面板路由逐条比「键集合 + 类型」；状态码不同也算差异。"""
    problems = []
    for name in list(a) + [k for k in b if k not in a]:
        if not name.startswith("panel:"):
            continue
        left, right = a.get(name), b.get(name)
        if left is None or right is None:
            continue
        if left["status"] != right["status"]:
            problems.append("%s: status %s != %s" % (name[6:], left["status"], right["status"]))
            continue
        deltas = _shape_delta(shape(left["body"]), shape(right["body"]))
        if deltas:
            problems.append("%s: %s" % (name[6:], "；".join(deltas[:12])))
    return problems


def diff(a, b):
    problems = []
    for name in list(a) + [k for k in b if k not in a]:
        if name.startswith("panel:"):
            continue
        left, right = a.get(name), b.get(name)
        if left is None or right is None:
            # 一侧按里程碑跳过（如 M1 还没移植的流式），不算差异但要报出来。
            continue
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

    def __init__(self, rust_binary=None):
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
        # 同一份 accounts/usage，第二个后端：Rust 侧用 --port 起，契约相同。
        self.rust = None
        if rust_binary:
            # Rust 侧单独一个 usage 目录：两边写同一个文件就分不清谁写了哪行。
            rust_usage = os.path.join(self.tmp, "usage-rust")
            os.makedirs(rust_usage, exist_ok=True)
            self.usage_dirs = (usage, rust_usage)
            self.rust_port = _free_port()
            self.rust_proc = subprocess.Popen(
                [os.path.abspath(rust_binary), "--host", "127.0.0.1",
                 "--port", str(self.rust_port), "--accounts-dir", accounts,
                 "--usage-dir", rust_usage],
                cwd=ROOT, env=env, stdout=self.log, stderr=self.log)
            self.rust = "http://127.0.0.1:%d" % self.rust_port
            self.wait_ready_any(self.rust, "rust", timeout=20.0)

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

    def wait_ready_any(self, base, backend, timeout=20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(base + "/health", timeout=1) as fh:
                    payload = json.loads(fh.read())
                if payload.get("ok") and payload.get("backend") == backend:
                    return
            except Exception:
                pass
            time.sleep(0.2)
        raise AssertionError("%s 后端没在 %.0fs 内就绪；日志：\n%s"
                             % (backend, timeout, self.read_log()))

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
        if getattr(self, "rust", None):
            try:
                self.rust_proc.terminate()
                self.rust_proc.wait(timeout=10)
            except Exception:
                self.rust_proc.kill()
        self.httpd.shutdown()
        self.log.close()


def read_usage(path):
    file = os.path.join(path, "usage.jsonl")
    if not os.path.exists(file):
        return []
    rows = []
    with open(file, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(sanitise(json.loads(line)))
    return rows


def diff_usage(left_dir, right_dir):
    """usage.jsonl 按请求配对后逐字段比。

    不按行号配对：一侧跳过的用例（M1 还没移植流式）会让行号错位。`msgs_sha`
    是请求内容的指纹，同一请求在两个后端应当算出一模一样的值 —— 它同时也是
    这里最该被验证的东西：算错了就配不上对，会直接暴露成「没有可比对的条目」。
    """
    def index(rows):
        # stream 必须进键：`chat.plain` 和 `chat.stream` 的 messages 相同、msgs_sha 相同，
        # 只用 sha 配对会把流式行和非流式行错配。
        return {(row.get("msgs_sha"), row.get("model"), row.get("stream")): row for row in rows}

    left, right = index(read_usage(left_dir)), index(read_usage(right_dir))
    shared = set(left) & set(right)
    if not shared:
        return ["usage 没有可比对的条目（左 %d 行 / 右 %d 行，msgs_sha 可能算错）"
                % (len(left), len(right))]
    problems = []
    # cost_policy 是计价引用：Python 侧价表在内存里就盖章，Rust 侧计价整组没移植
    # （backend-capabilities.json 的 pricing 仍 ported:false），所以这一列恒为 null。
    # 拿它比对只会暴露「Rust 不做计价」这件已知的事，与请求语义无关。
    known_gaps = {"cost_policy"}
    for key in sorted(shared, key=str):
        lrow, rrow = left[key], right[key]
        if lrow == rrow:
            continue
        skip = {k: (lrow.get(k), rrow.get(k)) for k in known_gaps if lrow.get(k) != rrow.get(k)}
        if skip and {k: v for k, v in lrow.items() if k not in known_gaps} == \
                {k: v for k, v in rrow.items() if k not in known_gaps}:
            continue
        missing = {k: lrow[k] for k in lrow if k not in rrow}
        extra = {k: rrow[k] for k in rrow if k not in lrow}
        changed = {k: (lrow[k], rrow[k]) for k in lrow if k in rrow and lrow[k] != rrow[k]}
        problems.append("usage(%s stream=%s) 不同：缺 %s / 多 %s / 变 %s"
                        % (key[1], key[2], list(missing), list(extra), changed))
    if len(problems) == 0:
        print("[ok] usage 行 %d 条逐字段一致（另有 %d 条只在一侧，按里程碑跳过）"
              % (len(shared), len(set(left) ^ set(right))))
    return problems


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
    ap.add_argument("--with-rust", metavar="BINARY",
                    help="同时起 Rust 后端并与其比对（同一份 accounts/usage）")
    args = ap.parse_args(argv)
    if args.self_check:
        self_check()
        return 0
    if args.with_rust:
        fixture = Fixture(rust_binary=args.with_rust)
        try:
            a = capture(fixture.base, panel=True)
            b = capture(fixture.rust, panel=True)
            response_problems = diff(a, b)
            panel_problems = diff_panel(a, b)
            usage_problems = diff_usage(fixture.usage_dirs[0], fixture.usage_dirs[1])
            for line in response_problems + usage_problems:
                print("[diff] " + line[:2000])
            for line in panel_problems:
                print("[panel] " + line[:2000])
            print("面板路由 %d/%d 键集合与类型一致"
                  % (len([k for k in b if k.startswith("panel:")]) - len(panel_problems),
                     len([k for k in b if k.startswith("panel:")])))
            skipped = len(a) - len(b)
            print("%d/%d 响应项一致（python vs rust）%s；usage %s"
                  % (len(b) - len(response_problems), len(b),
                     "；跳过 %d 项未移植流式（M2）" % skipped if skipped else "",
                     "一致" if not usage_problems else "%d 处不同" % len(usage_problems)))
            problems = response_problems + usage_problems + panel_problems
            return 1 if problems else 0
        finally:
            fixture.close()
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
