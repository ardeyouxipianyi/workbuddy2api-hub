"""`--probe-only` 的三个退出码必须稳定，cmd 侧才有得判断。

    python tests/_test_probe_only_exit_codes.py

静默模式（pythonw + stdout/stderr 指向空设备）里，服务自身那段"端口上已经有反代在
跑"的判断打印永远进空设备：用户双击启动脚本只见窗口一闪，读成"启动失败"。启动脚本
改为先在本 cmd 窗口里用同一个判据问一次，所以三个退出码是被 .bat 依赖的公开契约：

    0 = 已经有一个 wb-proxy 在这个端口上（或端口被别的服务占着，原因由 -only 打印）
    3 = 端口是空的，可以拉起服务
    1 = 端口属于别的服务（不是我们的 /health），或端口能连上但一个字都不答
        （卡死的实例；此前这种超时被读成"端口空闲"，于是又叠了一层实例）

断言只看退出码与 ASCII 片段，不锁中文文案。用一个临时 usage 目录，避免探针本身
把文件写进检出目录（scripts/check_clean_checkout.py 会钉这个）。
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # the gateway lives one level up
PY = os.path.join(ROOT, "python", "python.exe")
if not os.path.exists(PY):
    PY = sys.executable
sys.path.insert(0, HERE)

import _lifecycle as life            # noqa: E402

PROXY = os.path.join(ROOT, "wb_proxy.py")
PASS = 0
FAIL = 0
WORK = tempfile.mkdtemp(prefix="wb-probe-only-")
USAGE = os.path.join(WORK, "usage")
ACCOUNTS = os.path.join(WORK, "accounts")
for _d in (USAGE, ACCOUNTS):
    os.makedirs(_d, exist_ok=True)


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  ok   %s" % label)
    else:
        FAIL += 1
        print("  FAIL %s%s" % (label, (" -> " + detail) if detail else ""))


def probe(port):
    """Run the launcher probe and return (returncode, stdout+stderr)."""
    r = subprocess.run([PY, PROXY, "--port", str(port), "--host", "127.0.0.1",
                        "--accounts-dir", ACCOUNTS, "--usage-dir", USAGE, "--probe-only"],
                       cwd=ROOT, capture_output=True, timeout=60,
                       env=dict(os.environ, WB_PROXY_USAGE_DIR=USAGE))
    out = r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")
    return r.returncode, out


class Foreign(BaseHTTPRequestHandler):
    """另一个服务的 /health：返回 JSON 但没有账号池字段。"""

    def do_GET(self):
        body = json.dumps({"ok": True, "service": "some-other-dev-proxy"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


class Foreign404(Foreign):
    def do_GET(self):
        self.send_response(404)
        self.end_headers()


class ForeignInvalidJson(Foreign):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"not json")


try:
    # 1) 空端口 -> 3
    free = life.free_port()
    code, out = probe(free)
    check("端口空闲时 --probe-only 退 3", code == 3, "rc=%s out=%r" % (code, out[:120]))
    still_free = True
    try:
        socket.create_connection(("127.0.0.1", free), timeout=1).close()
        still_free = False
    except Exception:
        pass
    check("探针没有在空端口上留下监听", still_free)

    # 2) 我们自己的实例在跑 -> 0
    port = life.free_port()
    proc = life.spawn_managed([PY, PROXY, "--port", str(port), "--host", "127.0.0.1",
                              "--accounts-dir", ACCOUNTS, "--usage-dir", USAGE],
                             cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                             env=dict(os.environ, WB_PROXY_USAGE_DIR=USAGE))
    ready = False
    for _ in range(60):
        time.sleep(0.5)
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=2)
            s.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            ready = b"200" in s.recv(200)
            s.close()
            if ready:
                break
        except Exception:
            if proc.poll() is not None:
                break
    check("备用端口 %d 上的网关已就绪" % port, ready)
    if ready:
        code, out = probe(port)
        check("已有 wb-proxy 时 --probe-only 退 0", code == 0, "rc=%s" % code)
        check("并打印可给用户看的看板地址", ("http://127.0.0.1:%d/" % port) in out,
              repr(out[:160]))
    life.stop_managed(proc)

    # 3) 端口是别的服务 -> 1（不抢别人的端口）
    srv = ThreadingHTTPServer(("127.0.0.1", life.free_port()), Foreign)
    fport = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        code, out = probe(fport)
        check("端口被别的服务占用时退 1", code == 1, "rc=%s" % code)
        check("并把 [ERROR] 说给用户听", "[ERROR]" in out, repr(out[:160]))
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)

    # A reachable service remains occupied even if /health is not valid JSON.
    for handler in (Foreign404, ForeignInvalidJson):
        srv = ThreadingHTTPServer(("127.0.0.1", life.free_port()), handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            code, out = probe(srv.server_address[1])
            check("非网关 HTTP 服务不被误判为空端口", code == 1,
                  "handler=%s rc=%s" % (handler.__name__, code))
        finally:
            srv.shutdown()
            srv.server_close()
            t.join(timeout=5)

    # 4) 端口能连上但永远不答（卡死的实例）-> 1，绝不能退 3 再叠一层
    #    此前这里靠 urllib 的 2 秒超时 -> except -> "端口空闲"，正是重复
    #    实例 + Windows SO_REUSEADDR 分流的起因。
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(8)
    sport = silent.getsockname()[1]
    held = []
    try:
        def _hold():
            # 接住连接却一个字节都不写，模拟卡住的服务
            while True:
                try:
                    conn, _ = silent.accept()
                except OSError:
                    return
                held.append(conn)
        th = threading.Thread(target=_hold, daemon=True)
        th.start()
        code, out = probe(sport)
        check("端口能连上但不答时 --probe-only 退 1", code == 1,
              "rc=%s out=%r" % (code, out[:160]))
        check("并说明端口被无响应的程序占着", "answers nothing" in out,
              repr(out[:200]))
    finally:
        try:
            silent.close()
        except OSError:
            pass
        for c in held:
            try:
                c.close()
            except OSError:
                pass
finally:
    import shutil
    shutil.rmtree(WORK, ignore_errors=True)

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
