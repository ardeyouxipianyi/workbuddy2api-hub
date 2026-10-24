"""stderr 的两种坏形态都不能打死服务：读端消失（会抛异常）、读端不消费（只阻塞）。

    python tests/_test_dead_stderr_keeps_serving.py

`send_response()` 会先走 log_request -> log_message -> log()。log() 曾经裸写
sys.stderr，于是日志通道的形态直接决定业务死活，而两种形态失败得还不一样：

1. **读端消失**（启动网关的终端 / 管道被回收）：写抛 OSError，位置同样在响应头
   发出之前 —— 客户端只看到连接被空关，一条访问日志也不留。`#489` 的 try/except
   盖住的就是这一种。
2. **读端还在、但没人消费**（后台任务、服务管理器、`| tee` 的消费端已退出、智能体
   runner 拉了 stderr=PIPE 却不读）：写满约 64KB 后 `write()` 永久阻塞，且**不抛
   异常** —— try/except 观察不到阻塞，所以 `#489` 覆盖不到（`#642`）。请求线程因此
   停在「还没发出响应头」的位置，除静默白名单（/health、/realm、/panel/status）外的
   端点全部挂死。

两种形态的共同指纹：进程活着、端口在听、`/health` 绿，业务接口 100% 不可用。
现在写 stderr 的活由单个守护线程承担（有界队列 + 丢弃计数），请求线程只入队，
所以下面三个场景都必须「照常应答 / 写不阻塞请求线程」：

- 形态一：读端被关掉（抛异常那一种）；
- 形态二：真网关 + 没人读的管道，端点必须照常答。v1.7.33 上实测第 54 个请求卡死
  （Windows 匿名管道缓冲约 4KB，一次 /v1/models 的访问日志约 55 字节），具体序号随该机
  的缓冲与日志长度变化；判据是「一个请求都不许卡住」，不是「卡在第几个」。
- 形态三：进程内钉住真正的不变量 —— **stderr 的写发生在哪条线程**。同步写在调用者
  线程里，那条线程堵住就是请求堵住；写在后台线程里，堵住也只是那条线程堵住。这一条与
  操作系统、缓冲大小都无关，未修的版本必然失败。

判据是"有没有响应"，不是"响应对不对"——无凭据的网关对 /v1/models 返回 502 也算通过。
用临时 accounts/usage 与备用端口起真服务，不碰网络、不需要凭据、不动真实账号。
"""

import os
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # the gateway lives one level up
PY = os.path.join(ROOT, "python", "python.exe")
if not os.path.exists(PY):
    PY = sys.executable
sys.path.insert(0, HERE)

import _lifecycle as life            # noqa: E402  (spare port + managed process)

PASS = 0
FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  ok   %s" % label)
    else:
        FAIL += 1
        print("  FAIL %s%s" % (label, (" -> " + detail) if detail else ""))


def answered(port, path, timeout=15):
    """(True, status line) when the peer answers at all; (False, why) otherwise."""
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except Exception as exc:
        return False, "connect failed: %s" % exc
    try:
        sock.sendall(("GET %s HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n" % path)
                     .encode("ascii"))
        data = sock.recv(512)
    except Exception as exc:
        return False, "recv: %s: %s" % (type(exc).__name__, exc)
    finally:
        try:
            sock.close()
        except Exception:
            pass
    if not data:
        return False, "empty reply（连接被空关，正是要抓的形态）"
    head = data.split(b"\r\n", 1)[0].decode("latin-1")
    if not head.startswith("HTTP/"):
        return False, "不是状态行: %r" % head
    return True, head


def spawn_gateway(stderr):
    """起一个真网关（临时 accounts/usage，备用端口），stderr 交给调用方决定形态。"""
    work = tempfile.mkdtemp(prefix="wb-dead-stderr-")
    store = os.path.join(work, "accounts")
    usage = os.path.join(work, "usage")
    os.makedirs(store, exist_ok=True)
    os.makedirs(usage, exist_ok=True)
    port = life.free_port()
    proc = life.spawn_managed(
        [PY, os.path.join(ROOT, "wb_proxy.py"), "--port", str(port), "--host", "127.0.0.1",
         "--accounts-dir", store],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=stderr,
        env=dict(os.environ, WB_PROXY_USAGE_DIR=usage))
    return proc, port, work


def wait_ready(proc, port):
    for _ in range(60):
        time.sleep(0.5)
        ok, detail = answered(port, "/health", timeout=2)
        if ok and " 200" in detail:
            return True
        if proc.poll() is not None:
            return False
    return False


def spam_logged_endpoint(port, attempts, timeout=5):
    """连续打会写访问日志的端点。返回 (第一个没有应答的序号, 细节)；全答则 (None, "")。"""
    for i in range(1, attempts + 1):
        ok, detail = answered(port, "/v1/models", timeout=timeout)
        if not ok:
            return i, detail
    return None, ""


def cleanup(work, proc):
    life.stop_managed(proc)
    import shutil
    shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------
# 形态一：读端被关掉 —— 写会抛异常（#489）
# --------------------------------------------------------------------------
print("[形态一] stderr 读端被关掉（写会抛 OSError）")

# stderr 必须是管道：后面要在本进程里关掉它的读端，才复现得出发端 OSError
proc, port, work = spawn_gateway(subprocess.PIPE)
try:
    check("备用端口 %d 上的网关已就绪" % port, wait_ready(proc, port))
    if proc.poll() is None:
        # 关键一步：父进程不再读这个管道
        try:
            proc.stderr.close()
        except Exception:
            pass
        print("  ..   已关闭子进程 stderr 管道的读端（模拟启动终端被回收）")

        for path in ("/health", "/realm", "/panel/status"):
            ok, detail = answered(port, path)
            check("免日志端点 %s 仍有响应" % path, ok, detail)
        for path in ("/v1/models", "/", "/favicon.ico"):
            ok, detail = answered(port, path)
            check("被记日志的端点 %s 仍有响应" % path, ok, detail)
        check("进程没有因为日志写失败而退出", proc.poll() is None,
              "exit=%s" % proc.poll())
finally:
    cleanup(work, proc)


# --------------------------------------------------------------------------
# 形态二：读端开着、但从不读 —— 写会永久阻塞且不抛异常（#642）
# --------------------------------------------------------------------------
print()
print("[形态二] stderr 是「没人读的管道」（写会阻塞，不抛异常）")

# 每条约 1.2KB 访问日志，Windows 匿名管道缓冲约 64KB —— 给足 3 倍余量，
# 确保未修的版本一定会在中途卡住，而修好的版本一个请求都不许卡。
ATTEMPTS = 600

read_fd, write_fd = os.pipe()
proc, port, work = spawn_gateway(write_fd)
os.close(write_fd)          # 父进程只留读端，且从不 read() —— 正是要复现的形态
try:
    check("备用端口 %d 上的网关已就绪（stderr = 没人读的管道）" % port, wait_ready(proc, port))
    if proc.poll() is None:
        blocked_at, detail = spam_logged_endpoint(port, ATTEMPTS)
        check("连续 %d 个会写访问日志的请求全部有应答" % ATTEMPTS, blocked_at is None,
              "第 %d 个请求先卡住: %s" % (blocked_at, detail) if blocked_at else "")
        for path in ("/v1/models", "/", "/health", "/panel/status"):
            ok, why = answered(port, path, timeout=6)
            check("管道仍没人读时 %s 仍有响应" % path, ok, why)
        check("进程没有因为 stderr 写阻塞而退出", proc.poll() is None,
              "exit=%s" % proc.poll())
finally:
    try:
        os.close(read_fd)    # 放开写端，免得清理时陪着一起等
    except Exception:
        pass
    cleanup(work, proc)

# --------------------------------------------------------------------------
# 形态三：进程内 —— stderr 的写入必须发生在后台线程，而不是发请求的那条线程
# --------------------------------------------------------------------------
print()
print("[形态三] 进程内：stderr 由后台写线程承担，请求线程只入队")

unit_dir = tempfile.mkdtemp(prefix="wb-logwriter-")
os.environ["ACCOUNTS_DIR"] = unit_dir            # 不碰真实账号
os.environ["WB_PROXY_USAGE_DIR"] = unit_dir
sys.path.insert(0, ROOT)
import threading                                 # noqa: E402
import wb_proxy                                  # noqa: E402  (真网关的 log() 实现)


class RecordingStream(object):
    """只记录「谁写的、写了什么」的 stderr —— 不阻塞，用来判断写发生在哪条线程。"""

    def __init__(self):
        self.writes = []

    def write(self, text):
        self.writes.append((threading.current_thread().name, text))
        return len(text)

    def flush(self):
        pass


recorder = RecordingStream()
saved_stderr = sys.stderr
try:
    wb_proxy._LOG_STREAM_CLOSED = False          # 从「通道是好的」这个状态开始
    sys.stderr = recorder
    wb_proxy.log("形态三：这一行必须由后台写线程落下去")
    flusher = getattr(wb_proxy, "flush_log_channel", None)
    if flusher is not None:
        flusher(2.0)                             # 等它真的写下去，避免读到「还没写」
finally:
    sys.stderr = saved_stderr

writers = sorted({name for name, _ in recorder.writes})
caller = threading.current_thread().name
check("stderr 的写入发生在后台线程（%s），不是调用 log() 的线程" % (", ".join(writers) or "无"),
      writers and caller not in writers,
      "写入线程: %s —— 写在调用者线程里，那条线程一卡就是请求一卡" % (", ".join(writers) or "无"))
check("这一行确实落到了 stderr", any("形态三" in text for _, text in recorder.writes),
      "共 %d 次写入" % len(recorder.writes))

import shutil                                    # noqa: E402
shutil.rmtree(unit_dir, ignore_errors=True)

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
