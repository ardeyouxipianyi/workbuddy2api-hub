"""HTTP 服务层的两个「一行修」+ 一个内存护栏，钉住路由器上实测过的三个数字。

三处改动都来自 OpenWrt 路由器（2 核 Celeron N2840 / 2GB，前面挂 nginx）上的实测：

  - Handler.disable_nagle_algorithm：响应头（end_headers）和响应体是两次独立
    write，服务端 Nagle 碰上客户端 delayed ACK 让每个小响应白付 ~40ms（真机
    keep-alive 连续小响应中位数 50.0ms -> 1.41ms；面板几乎所有 <64KB 的 JSON
    都中招）。
  - GatewayServer.request_queue_size：stdlib 默认 backlog=5，突发并发下内核来
    不及 accept 的 SYN 被丢弃、客户端按 1s 粒度重传（真机 64 并发突发 43/64
    卡 >=1s；backlog=128 后 0/64）。
  - MAX_PAYLOAD_BYTES 默认 50MB -> 16MB：读 body 发生在 chat 信号量 acquire
    之前、线程数又无上限，50MB body 单请求峰值 ~160MB，而路由器可用内存只有
    ~480MB。

断言分两层：类属性是「声明」，行为断言（真实 socket 上读 TCP_NODELAY、实例化
时监听套接字 listen() 的实参）才是「声明真的生效」。最后一组用桩证明超限 body
在 Content-Length 检查处就被拒、一个字节都不读，413 与断开（close）两条路径都
一并走到——这正是 50MB 默认值时 OOM 的那条路。

Run with: python tests/_test_http_limits.py
No network, no credentials: only loopback sockets on ephemeral ports.
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock as mock
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# 导入 wb_proxy 前先把可写目录指到临时区：模块级虽不落盘，但保持一致，避免任何
# 意外路径写进仓库（与 _test_anthropic_http.py 同一套做法）。
_WORK = tempfile.mkdtemp(prefix="http-limits-")
os.environ.setdefault("WB_PROXY_USAGE_DIR", os.path.join(_WORK, "usage"))
os.environ.setdefault("HOME", _WORK)
os.environ.setdefault("USERPROFILE", _WORK)

import wb_proxy as P  # noqa: E402  (needs sys.path/env set above)


class CountingReader(object):
    """rfile 桩：记下每个 read/readline 调用与被读走的字节数。

    计数就是证据——超限 body 若被读走哪怕一个字节，bytes_read/read_calls 都会
    变，断言随即失败。
    """

    def __init__(self, data=b""):
        self.data = data
        self.read_calls = 0
        self.readline_calls = 0
        self.bytes_read = 0

    def read(self, n=-1):
        self.read_calls += 1
        if n is None or n < 0:
            chunk, self.data = self.data, b""
        else:
            chunk, self.data = self.data[:n], self.data[n:]
        self.bytes_read += len(chunk)
        return chunk

    def readline(self, n=-1):
        self.readline_calls += 1
        idx = self.data.find(b"\n")
        if idx < 0:
            chunk, rest = self.data, b""
        else:
            chunk, rest = self.data[:idx + 1], self.data[idx + 1:]
        if n is not None and n >= 0 and len(chunk) > n:
            rest, chunk = chunk[n:] + rest, chunk[:n]
        self.data = rest
        self.bytes_read += len(chunk)
        return chunk


def make_handler(headers, rfile=None, path="/v1/chat/completions"):
    """造一个只带 _read_payload 需要的最小状态的 Handler 实例。

    不走 __init__（那会要求一个真实 socket 并立刻开始跑请求循环），用 __new__
    直接给属性，桩上只覆盖本用例要观察的那条路径。
    """
    handler = P.Handler.__new__(P.Handler)
    handler.headers = headers
    handler.rfile = rfile if rfile is not None else CountingReader()
    handler._body_consumed = False
    handler.path = path
    return handler


class NagleFlagTests(unittest.TestCase):
    """keep-alive 上每个小响应白付 40~50ms 的根修。"""

    def test_the_flag_is_on_the_handler(self):
        self.assertIs(P.Handler.disable_nagle_algorithm, True,
                      "Handler must disable Nagle; without it every small "
                      "keep-alive response pays the delayed-ACK penalty")

    def test_a_real_connection_gets_tcp_nodelay(self):
        """行为级：accept 之后的连接上 TCP_NODELAY 真的被置位。

        只看类属性会放过「属性写了但没人用」的情况；socketserver 的
        StreamRequestHandler.setup() 是把它变成 setsockopt 的那一步，这里用
        真实 socket 把整条链走一遍。
        """
        class Probe(P.Handler):
            seen = []
            done = threading.Event()

            def setup(self):
                super().setup()
                self.__class__.seen.append(
                    self.connection.getsockopt(socket.IPPROTO_TCP,
                                               socket.TCP_NODELAY))
                self.__class__.done.set()

            def handle(self):
                # 不跑请求循环：本用例只关心连接建立时的套接字选项。
                pass

        server = P.GatewayServer(("127.0.0.1", 0), Probe)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = socket.create_connection(server.server_address, timeout=5)
            try:
                self.assertTrue(Probe.done.wait(5),
                                "the server never set up the connection")
            finally:
                client.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(Probe.seen, [1],
                         "TCP_NODELAY is not set on the accepted connection")


class BacklogTests(unittest.TestCase):
    """并发突发的 1 秒停顿：backlog 必须远高于 stdlib 默认的 5。"""

    def test_the_service_class_raises_the_backlog(self):
        self.assertEqual(P.GatewayServer.request_queue_size, 128)
        self.assertTrue(issubclass(P.GatewayServer, ThreadingHTTPServer))

    def test_the_listening_socket_is_listened_with_the_new_backlog(self):
        """行为级：listen() 是构造时调的，这里拦住它读实参。

        这是「类属性必须在实例化前设」那个坑的直接证明：把 request_queue_size
        从类属性改成实例属性赋值，listen 就会用回默认的 5，本用例立刻红。
        """
        seen = []
        real_listen = socket.socket.listen

        def spy(self, backlog=-1):
            seen.append(backlog)
            return real_listen(self, backlog)

        with mock.patch.object(socket.socket, "listen", spy):
            server = P.GatewayServer(("127.0.0.1", 0), P.Handler)
        try:
            self.assertEqual(seen, [P.GatewayServer.request_queue_size],
                             "listen() did not use request_queue_size=%d"
                             % P.GatewayServer.request_queue_size)
            self.assertLess(ThreadingHTTPServer.request_queue_size,
                            P.GatewayServer.request_queue_size,
                            "the stdlib default is no longer the small value "
                            "this subclass exists to replace")
        finally:
            server.server_close()

    def test_the_service_starts_that_class(self):
        """钉住接线：_serve_forever 必须实例化 GatewayServer。

        没有这条，上面两条可以全绿而线上仍然跑着默认 backlog 的
        ThreadingHTTPServer。源码文本断言与 _test_release_engineering.py 的
        做法一致（那类接线没有便宜的运行期入口）。
        """
        with open(os.path.join(ROOT, "wb_proxy.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("server = GatewayServer((args.host, args.port), Handler)",
                      src,
                      "_serve_forever no longer starts GatewayServer, so the "
                      "backlog fix is not on the serving path")


class PayloadCapTests(unittest.TestCase):
    """50MB body 单请求峰值 ~160MB 对 ~480MB 可用内存的护栏。"""

    def test_the_default_cap_is_the_new_value(self):
        self.assertEqual(P.MAX_PAYLOAD_BYTES, 16 * 1024 * 1024,
                         "the default body cap must stay at the tightened "
                         "16MB; 50MB allowed a single request to peak at "
                         "~160MB on a ~480MB router")

    def test_panel_limit_applies_before_reading(self):
        handler = make_handler({"Content-Length": str(2 * 1024 * 1024)})
        calls = []
        handler._error = lambda *args: calls.append(args)
        with mock.patch.object(P.wb_settings, "max_payload_mb", return_value=1):
            self.assertIsNone(handler._payload_or_error())
        self.assertEqual(calls[0][0], 413)
        self.assertIn(str(1024 * 1024), calls[0][1])
        self.assertEqual(handler.rfile.read_calls, 0)
        self.assertTrue(handler.close_connection)

    def test_increased_limit_allows_a_previously_rejected_json_body(self):
        body = b'{"text":"' + b'x' * (1024 * 1024) + b'"}'
        handler = make_handler({"Content-Length": str(len(body))}, CountingReader(body))
        with mock.patch.object(P.wb_settings, "max_payload_mb", return_value=2):
            self.assertEqual(len(handler._payload_or_error()["text"]), 1024 * 1024)

    def test_chunk_exceeding_limit_is_not_drained(self):
        reader = CountingReader(b'200000\r\n' + b'x' * 8)
        handler = make_handler({"Transfer-Encoding": "chunked"}, rfile=reader)
        calls = []
        handler._error = lambda *args: calls.append(args)
        with mock.patch.object(P.wb_settings, "max_payload_mb", return_value=1):
            self.assertIsNone(handler._payload_or_error())
        self.assertEqual(calls[0][0], 413)
        self.assertEqual(reader.read_calls, 0)
        self.assertTrue(handler.close_connection)

    def test_panel_limit_rejects_invalid_values(self):
        for value in (0, 257, True, 1.5, '16'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                P.wb_settings.set_max_payload_mb(_WORK, value)

    def test_panel_limit_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(P.wb_settings.max_payload_mb(directory))
            P.wb_settings.set_max_payload_mb(directory, 64)
            self.assertEqual(P.wb_settings.max_payload_mb(directory), 64)

    def test_an_over_limit_body_is_never_read(self):
        """行为级：Content-Length 超限在解析前就被拒，rfile 一个字节没动。

        旧默认值 50MB 正是「读完再 parse」的那条路；这里用声明 50MB 的请求
        复现当时的形状，桩上的计数证明它现在连读都不读。
        """
        for declared in (50 * 1024 * 1024, P.MAX_PAYLOAD_BYTES + 1):
            with self.subTest(content_length=declared):
                reader = CountingReader()
                handler = make_handler(
                    {"Content-Length": str(declared)}, rfile=reader)
                with self.assertRaises(P.BodyTooLarge) as raised:
                    handler._read_payload()
                self.assertEqual(raised.exception.length, declared)
                self.assertEqual(reader.read_calls, 0)
                self.assertEqual(reader.bytes_read, 0)

    def test_the_413_reply_path_still_answers(self):
        """超限 -> _payload_or_error 走 413（而不是抛穿线程）。"""
        calls = []
        handler = make_handler({"Content-Length": str(50 * 1024 * 1024)})
        handler._error = lambda code, message, err_type="server_error": \
            calls.append((code, message, err_type))
        result = handler._payload_or_error()
        self.assertIsNone(result)
        self.assertEqual(len(calls), 1)
        code, message, err_type = calls[0]
        self.assertEqual(code, 413)
        self.assertEqual(err_type, "invalid_request_error")
        self.assertIn(str(P.MAX_PAYLOAD_BYTES), message)

    def test_an_over_limit_body_closes_instead_of_draining(self):
        """断开路径：_error 的 drain 对超限 body 只置 close_connection。

        读它要等到客户端把 50MB 发完，而回包早已发出——旧行为会一直阻塞在
        drain 上；这里证明超限时走的是断开而不是读。
        """
        reader = CountingReader()
        handler = make_handler({"Content-Length": str(50 * 1024 * 1024)},
                               rfile=reader)
        handler.close_connection = False
        handler._discard_body()
        self.assertTrue(handler.close_connection)
        self.assertEqual(reader.read_calls, 0)

    def test_a_body_within_the_cap_still_parses(self):
        """反方向护栏：收紧默认值不能把正常请求一并挡掉。"""
        body = b'{"model": "x", "messages": []}'
        reader = CountingReader(body)
        handler = make_handler({"Content-Length": str(len(body))}, rfile=reader)
        payload = handler._read_payload()
        self.assertEqual(payload, {"model": "x", "messages": []})
        self.assertTrue(handler._body_consumed)
        self.assertEqual(reader.bytes_read, len(body))

    def test_the_env_override_still_raises_the_cap(self):
        """WB_MAX_PAYLOAD_BYTES 仍是逃生口：注释里的权衡必须真的成立。

        模块常量在导入时求值，所以只能起子进程换环境变量验证。
        """
        env = dict(os.environ)
        env["WB_MAX_PAYLOAD_BYTES"] = str(32 * 1024 * 1024)
        env["PYTHONPATH"] = ROOT
        done = subprocess.run(
            [sys.executable, "-c",
             "import wb_proxy; print(wb_proxy.MAX_PAYLOAD_BYTES)"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertEqual(done.stdout.strip(), str(32 * 1024 * 1024))


def _cleanup():
    shutil.rmtree(_WORK, ignore_errors=True)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        _cleanup()
