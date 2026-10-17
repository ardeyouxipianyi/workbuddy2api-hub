"""上游连接复用池（keep-alive pool）的行为套件。

测的是 wb_upstream_pool 对 wb_accounts.urlopen 出站路径的接管：复用命中、容量与
空闲回收、连接被对端掐掉时的探活与「安全重试一次」、Connection: close 与半截
body 的丢弃、流式（chunked）读干净才归还、复用前重置读超时、按代理串分池、
3xx 回退、TLS、WB_UPSTREAM_KEEPALIVE=0 时与改动前逐项一致、chat 路径的头名
保真（有序原名原样上线）、以及按账号凭据分池（不同账号不共用 TCP/TLS 连接）。

全部用本机假上游：真 TCP、会说 keep-alive / chunked / Connection: close / 429 /
302 / 半截 body，并且逐条记录收到的请求头。连接数就是证据——「每请求一次 SYN」
直接体现在 FakeUpstream.accepted 上。不碰外网、不碰真账号。

TLS 用例用内嵌的自签证书（EC P-256，仅供本套件），客户端侧只把 http.client 的
默认 context 换成不校验证书的，目的就是让 https 这条主路径（TLS 上的探活、
settimeout 重设、chunked 归还）也真跑一遍。
"""
import http.client
import json
import os
import socket
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-upstream-pool-")

import wb_accounts            # noqa: E402
import wb_proxy               # noqa: E402  (_apply_stream_idle_timeout 的对齐)
import wb_upstream_pool as pool  # noqa: E402

PASS = 0
FAIL = 0

OK_BODY = b'{"ok":true}'


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (label, detail))


# ---------------------------------------------------------------------------
# 假上游：迷你 HTTP/1.1 服务端，兼做 HTTP 代理（绝对 URI 请求目标）
# ---------------------------------------------------------------------------

#: 测试专用自签证书（EC P-256，100 年），只在本套件里加载。
CERT_PEM = """-----BEGIN CERTIFICATE-----
MIIBVTCB/KADAgECAhRjBYVVqXLLq9XPGJLkh4vjllA88jAKBggqhkjOPQQDAjAX
MRUwEwYDVQQDDAx3Yi1wb29sLXRlc3QwIBcNMjYwMTAxMDAwMDAwWhgPMjEyNTEy
MDgwMDAwMDBaMBcxFTATBgNVBAMMDHdiLXBvb2wtdGVzdDBZMBMGByqGSM49AgEG
CCqGSM49AwEHA0IABO9/UQG2Y7AHaVYraezrSQRqTF7CNRvyB92HN0Z1UcZihDrJ
gdfNQ7roarcMLQp+iF/4Mptz9aK6JVpxHrhBDCajJDAiMCAGA1UdEQQZMBeCCWxv
Y2FsaG9zdIcEfwAAAYcEZGznVDAKBggqhkjOPQQDAgNIADBFAiEA3pmrNdsS5rsF
g61sIWJjuMTnhITTEpfEirLsDswzTJsCIHtKSt51ZnqleBoee2sC4uAZmSNogyzW
un5auKxKku42
-----END CERTIFICATE-----
"""

KEY_PEM = """-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgAfOCWr5OraU9SONE
4eagN97nxaEte1HcQNq0dPXa982hRANCAATvf1EBtmOwB2lWK2ns60kEakxewjUb
8gfdhzdGdVHGYoQ6yYHXzUO66Gq3DC0Kfohf+DKbc/WiuiVacR64QQwm
-----END PRIVATE KEY-----
"""


class FakeUpstream(object):
    """一个会说 keep-alive 的迷你 HTTP/1.1 服务端。

    accepted 计每条 TCP 连接（含 TLS 握手前的那条），requests 记每条请求的
    method/target/小写头名。kill_conns() 从服务端把打开着的连接全部掐掉——
    「对端关掉空闲连接」这个场景就靠它复现。sync_n>0 时 /sync 会等够 n 条
    并发请求再一起应答，用来制造「多条连接同时在用」的确定性时序。
    """

    def __init__(self, tls=False, sync_n=0):
        self.tls = tls
        self.sync_n = sync_n
        self.lock = threading.Lock()
        self.accepted = 0
        self.requests = []
        self.conns = []
        self.inflight = 0
        self.sync_evt = threading.Event()
        self.closed = False
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(32)
        self.port = self.srv.getsockname()[1]
        self.ctx = None
        if tls:
            certdir = tempfile.mkdtemp(prefix="wbpool-cert-")
            cert = os.path.join(certdir, "cert.pem")
            key = os.path.join(certdir, "key.pem")
            with open(cert, "w", encoding="ascii", newline="\n") as fh:
                fh.write(CERT_PEM)
            with open(key, "w", encoding="ascii", newline="\n") as fh:
                fh.write(KEY_PEM)
            self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.ctx.load_cert_chain(cert, key)
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def base(self, scheme="http"):
        return "%s://127.0.0.1:%d" % (scheme, self.port)

    def stop(self):
        self.closed = True
        try:
            self.srv.close()
        except OSError:
            pass
        self.kill_conns()

    def kill_conns(self):
        """把当前打开的所有连接掐掉（服务端视角的「空闲超时到点」）。"""
        with self.lock:
            conns, self.conns = self.conns, []
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                c.close()
            except OSError:
                pass

    def _accept_loop(self):
        while not self.closed:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            with self.lock:
                self.accepted += 1
                self.conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, raw):
        conn = raw
        try:
            if self.tls:
                # 先给握手一个上限，再在完成后把被跟踪的 socket 换成包装后的
                # 那个：wrap_socket 会接管 fd，原来的 raw 对象被 detach，
                # kill_conns() 掐它就掐不动了。
                raw.settimeout(10)
                conn = self.ctx.wrap_socket(raw, server_side=True)
                with self.lock:
                    for i, c in enumerate(self.conns):
                        if c is raw:
                            self.conns[i] = conn
                            break
            fh = conn.makefile("rb")
            while True:
                line = fh.readline(65536)
                if not line:
                    break
                if line in (b"\r\n", b"\n"):
                    continue
                parts = line.decode("latin-1").split()
                if len(parts) < 2:
                    break
                method, target = parts[0], parts[1]
                headers = {}
                header_lines = []
                while True:
                    h = fh.readline(65536)
                    if h in (b"\r\n", b"\n", b""):
                        break
                    name, _, value = h.decode("latin-1").partition(":")
                    name, value = name.strip(), value.strip()
                    # 原名与原始顺序都留下来：头名保真用例要逐字节核对，
                    # 小写化的 headers 字典只够现有用例做取值断言。
                    header_lines.append((name, value))
                    headers[name.lower()] = value
                length = int(headers.get("content-length") or 0)
                if length:
                    fh.read(length)
                with self.lock:
                    self.requests.append({"method": method, "target": target,
                                          "headers": headers,
                                          "header_lines": header_lines})
                if not self._respond(conn, method, target):
                    break
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _send(self, conn, status, reason, body=b"", extra=()):
        head = "HTTP/1.1 %d %s\r\nContent-Length: %d\r\n" % (status, reason, len(body))
        for name, value in extra:
            head += "%s: %s\r\n" % (name, value)
        conn.sendall(head.encode("ascii") + b"\r\n" + body)

    def _respond(self, conn, method, target):
        """处理一条请求；返回 False 表示这条连接就此结束。"""
        path = target
        if path.startswith("http://") or path.startswith("https://"):
            path = urllib.parse.urlsplit(path).path
        if path == "/ok":
            self._send(conn, 200, "OK", OK_BODY,
                       extra=[("Content-Type", "application/json")])
        elif path == "/echo":
            payload = json.dumps({"method": method, "target": target}).encode()
            self._send(conn, 200, "OK", payload,
                       extra=[("Content-Type", "application/json")])
        elif path == "/sse":
            # 整段一次发出：客户端读完第一行后，其余数据已经在它的接收缓冲里，
            # 后面测「复用前重设超时」时不会被网络时序干扰。
            body = b""
            for i in range(3):
                body += b"data: {\"i\":%d}\n\n" % i
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                         b"Transfer-Encoding: chunked\r\n\r\n"
                         b"%x\r\n" % len(body) + body + b"\r\n0\r\n\r\n")
        elif path == "/close":
            self._send(conn, 200, "OK", b"bye", extra=[("Connection", "close")])
            return False
        elif path == "/half":
            # 声明 100 字节只给 10 字节：客户端读一半就关，连接必须被丢弃。
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n"
                         b"0123456789")
            return False
        elif path == "/err":
            body = b'{"code":6004,"msg":"usage exceeds frequency limit"}'
            self._send(conn, 429, "Too Many Requests", body,
                       extra=[("Content-Type", "application/json")])
        elif path == "/reset":
            # 读完请求再关，不发任何响应字节：客户端必然停在 RemoteDisconnected
            return False
        elif path == "/delay":
            time.sleep(0.5)
            self._send(conn, 200, "OK", OK_BODY)
        elif path == "/sync":
            with self.lock:
                self.inflight += 1
                if self.inflight >= self.sync_n:
                    self.sync_evt.set()
            self.sync_evt.wait(10)
            with self.lock:
                self.inflight -= 1
            self._send(conn, 200, "OK", OK_BODY)
        elif path == "/redirect":
            self._send(conn, 302, "Found", b"",
                       extra=[("Location", "http://127.0.0.1:%d/ok" % self.port)])
        else:
            self._send(conn, 404, "Not Found", b"{}")
        return True


def fetch(url, timeout=5, proxy="", data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    return wb_accounts.urlopen(req, timeout=timeout, proxy=proxy)


def sdelta(before):
    after = pool.stats()
    return {k: after[k] - before.get(k, 0) for k in after}


def idle_for(port):
    """某个假上游端口对应的空闲连接总数（池键里带目标端口）。"""
    return sum(n for key, n in pool.idle_counts().items() if key[2] == port)


servers = []


def new_server(**kw):
    srv = FakeUpstream(**kw)
    servers.append(srv)
    return srv


try:
    # ------------------------------------------------------------------
    print("[1] 复用命中：连续请求只开一条 TCP")
    srv = new_server()
    pool.close_all()
    before, accepted0 = pool.stats(), srv.accepted
    bodies = []
    for _ in range(3):
        resp = fetch(srv.base() + "/ok")
        bodies.append(resp.read())
        resp.close()
    delta = sdelta(before)
    check("3 次顺序请求只开 1 条 TCP（每请求一次 SYN 变成一次）",
          srv.accepted - accepted0 == 1, srv.accepted - accepted0)
    check("后两次命中池（created=1, reused=2）",
          delta["created"] == 1 and delta["reused"] == 2, delta)
    check("body 全部正确", bodies == [OK_BODY] * 3, bodies)
    check("请求带 Connection: keep-alive",
          all(q["headers"].get("connection") == "keep-alive" for q in srv.requests))
    resp = fetch(srv.base() + "/ok")
    check("响应形状与 urllib 一致（url/msg/status）",
          resp.url == srv.base() + "/ok" and resp.msg == resp.reason and resp.status == 200)
    resp.read()
    resp.close()

    # ------------------------------------------------------------------
    print("[2] 流式（chunked SSE）：读干净才归还，半截就丢弃")
    before = pool.stats()
    resp = fetch(srv.base() + "/sse")
    lines = list(resp)
    resp.close()
    check("SSE 迭代到终块（3 段数据 + 空行）", len(lines) == 6, lines)
    resp = fetch(srv.base() + "/ok")
    body = resp.read()
    resp.close()
    check("chunked 流读干净后连接可复用（没有新开 TCP）",
          body == OK_BODY and srv.accepted - accepted0 == 1,
          (body, srv.accepted - accepted0))
    resp = fetch(srv.base() + "/sse")
    first = resp.readline()
    resp.close()                      # 半截 body 就关闭
    check("半截流的第一行读到了", first.startswith(b"data:"), first)
    check("半截关闭的连接被丢弃，不留在池里", idle_for(srv.port) == 0,
          pool.idle_counts())
    resp = fetch(srv.base() + "/ok")
    resp.read()
    resp.close()
    check("丢弃之后下一条请求新建连接", srv.accepted - accepted0 == 2,
          srv.accepted - accepted0)

    # ------------------------------------------------------------------
    print("[3] Connection: close 与半截 body 都要主动丢弃")
    resp = fetch(srv.base() + "/close")
    body = resp.read()
    resp.close()
    check("Connection: close 的响应正常读完", body == b"bye")
    check("Connection: close 的连接不入池", idle_for(srv.port) == 0,
          pool.idle_counts())
    n = srv.accepted
    resp = fetch(srv.base() + "/ok")
    resp.read()
    resp.close()
    check("下一条请求为它新建连接", srv.accepted == n + 1)
    pool.close_all()                  # 让 /half 从新连接开始，计数才干净
    n = srv.accepted
    resp = fetch(srv.base() + "/half")
    part = resp.read(10)
    resp.close()
    check("半截 body 读到 10 字节后关闭", part == b"0123456789", part)
    check("半截 body 的连接不入池", idle_for(srv.port) == 0, pool.idle_counts())
    resp = fetch(srv.base() + "/ok")
    resp.read()
    resp.close()
    check("之后仍能正常请求（丢弃后新建连接）", srv.accepted == n + 2,
          srv.accepted - n)

    # ------------------------------------------------------------------
    print("[4] 复用前重置读超时（流式把 socket 超时改小，下一条请求不受影响）")
    pool.close_all()
    applied = []
    resp = fetch(srv.base() + "/sse")
    for i, line in enumerate(resp):
        if i == 0:
            # 与 wb_proxy 的流式路径同款调用：池化响应必须同样支持
            applied.append(wb_proxy._apply_stream_idle_timeout(resp, 0.15))
    resp.close()
    check("池化响应支持 _apply_stream_idle_timeout（fp 透传）",
          applied == [True], applied)
    started = time.monotonic()
    resp = fetch(srv.base() + "/delay")     # 服务端 0.5s 后才回响应头
    body = resp.read()
    resp.close()
    elapsed = time.monotonic() - started
    check("复用后慢响应没被流式留下的 0.15s 超时打断（超时已重设为 5s）",
          body == OK_BODY and elapsed >= 0.3, "%.2fs" % elapsed)
    stale = []
    for entries in pool._POOL._idle.values():
        for conn, _ts in entries:
            try:
                if conn.sock is not None:
                    stale.append(conn.sock.gettimeout())
            except Exception:
                pass
    check("池里的连接带着本次请求的超时（5.0），没有残留 0.15",
          stale and all(t != 0.15 for t in stale), stale)

    # ------------------------------------------------------------------
    print("[5] 容量：并发峰值可以多条，归还后每键只留 2 条")
    srv5 = new_server(sync_n=4)
    pool.close_all()
    before = pool.stats()
    barrier = threading.Barrier(4)
    results = []
    errors = []

    def worker():
        try:
            barrier.wait(10)
            resp = fetch(srv5.base() + "/sync", timeout=15)
            results.append(resp.read())
            resp.close()
        except Exception as exc:
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    delta = sdelta(before)
    check("4 条并发请求各自建连（池里当时没有空闲）", srv5.accepted == 4,
          (srv5.accepted, errors))
    check("4 条请求全部成功", results == [OK_BODY] * 4 and not errors,
          (results, errors))
    check("归还后每键只保留 2 条空闲（容量上限）", idle_for(srv5.port) == 2,
          pool.idle_counts())
    check("多出来的连接被回收（evicted>=2）", delta["evicted"] >= 2, delta)

    # ------------------------------------------------------------------
    print("[6] 空闲超时回收：超龄连接在取用时被关掉")
    srv6 = new_server()
    pool.close_all()
    resp = fetch(srv6.base() + "/ok")
    resp.read()
    resp.close()
    original_ttl = pool._idle_ttl
    pool._idle_ttl = lambda: 0.1
    try:
        time.sleep(0.3)
        before = pool.stats()
        resp = fetch(srv6.base() + "/ok")
        body = resp.read()
        resp.close()
        delta = sdelta(before)
    finally:
        pool._idle_ttl = original_ttl
    check("超龄连接没有被复用（换新连接，请求仍成功）",
          body == OK_BODY and srv6.accepted == 2, (srv6.accepted, body))
    check("超龄回收计入 evicted", delta["evicted"] >= 1, delta)

    # 回收是全局的：连接所属的键之后再没人用过，也要在下一次任何流量里被关掉。
    # 只扫「本次碰到的键」的话，被禁用账号/换掉的代理留下的连接会一直占着 socket。
    srv6b = new_server()
    pool.close_all()
    resp = fetch(srv6b.base() + "/ok")
    resp.read()
    resp.close()
    held = [c for key, entries in pool._POOL._idle.items()
            if key[2] == srv6b.port for c, _ts in entries]
    pool._idle_ttl = lambda: 0.1
    try:
        time.sleep(0.3)
        before = pool.stats()
        resp = fetch(srv6.base() + "/ok")      # 另一个池键的流量触发扫描
        other_body = resp.read()
        resp.close()
        delta = sdelta(before)
    finally:
        pool._idle_ttl = original_ttl
    check("另一个键的流量也会回收本键的超龄连接（全局扫描）",
          other_body == OK_BODY and held and held[0].sock is None,
          (other_body, [c.sock for c in held]))
    check("被回收的连接不再占着池键", idle_for(srv6b.port) == 0, pool.idle_counts())
    check("全局扫描的回收计入 evicted", delta["evicted"] >= 1, delta)

    # ------------------------------------------------------------------
    print("[7] 探活：对端掐掉的空闲连接不会被复用")
    srv7 = new_server()
    pool.close_all()
    resp = fetch(srv7.base() + "/ok")
    resp.read()
    resp.close()
    srv7.kill_conns()
    time.sleep(0.1)
    before = pool.stats()
    resp = fetch(srv7.base() + "/ok")
    body = resp.read()
    resp.close()
    delta = sdelta(before)
    check("请求成功且用的是新连接（没有把请求发上死连接）",
          body == OK_BODY and srv7.accepted == 2, (srv7.accepted, body))
    check("探活判死计入 dead", delta["dead"] >= 1, delta)
    check("请求只发生一次（探活把它拦在了发送之前）",
          len(srv7.requests) == 2, len(srv7.requests))

    # ------------------------------------------------------------------
    print("[8] 安全重试一次：探活漏掉的竞态（复用连接已被关）")
    srv8 = new_server()
    pool.close_all()
    resp = fetch(srv8.base() + "/ok")
    resp.read()
    resp.close()
    srv8.kill_conns()
    time.sleep(0.1)
    original_alive = pool._connection_looks_alive
    pool._connection_looks_alive = lambda conn: True    # 模拟探活与发送之间的窗口
    before = pool.stats()
    try:
        resp = fetch(srv8.base() + "/ok")
        body = resp.read()
        resp.close()
    finally:
        pool._connection_looks_alive = original_alive
    delta = sdelta(before)
    check("复用连接被掐后重试成功", body == OK_BODY, body)
    check("重试只多开一条新连接（死的那条不算新连接）", srv8.accepted == 2,
          srv8.accepted)
    check("死连接被丢弃、重试用新连接", delta["discarded"] >= 1 and delta["created"] == 1,
          delta)

    # ------------------------------------------------------------------
    print("[9] 全新连接的失败不重试（错误形状与改动前一致）")
    srv9 = new_server()
    pool.close_all()
    before = pool.stats()
    raised = None
    try:
        resp = fetch(srv9.base() + "/reset")
        resp.read()
    except Exception as exc:
        raised = exc
    delta = sdelta(before)
    check("/reset 上失败被原样上抛", raised is not None)
    check("错误形状与 urllib 一致（ConnectionError / URLError）",
          isinstance(raised, (ConnectionError, urllib.error.URLError)), repr(raised))
    check("没有对全新连接重试（只开了 1 条 TCP）", srv9.accepted == 1,
          (srv9.accepted, delta))

    # ------------------------------------------------------------------
    print("[10] HTTPError：形状不变，错误响应也不污染池")
    srv10 = new_server()
    pool.close_all()
    raised = None
    try:
        resp = fetch(srv10.base() + "/err")
        resp.read()
    except urllib.error.HTTPError as exc:
        raised = exc
    check("429 -> HTTPError", raised is not None and raised.code == 429, repr(raised))
    check("exc.read(600) 拿到完整 body",
          raised is not None and raised.read(600) ==
          b'{"code":6004,"msg":"usage exceeds frequency limit"}')
    check("exc.fp 可读（与 urllib 形状一致）", raised is not None and bool(raised.fp))
    check("exc.headers 是响应头",
          raised is not None and (raised.headers or {}).get("Content-Type")
          == "application/json")
    resp = fetch(srv10.base() + "/ok")
    body = resp.read()
    resp.close()
    check("错误响应读干净后连接照样复用", body == OK_BODY and srv10.accepted == 1,
          (srv10.accepted, body))

    # ------------------------------------------------------------------
    print("[11] 代理分池：按代理串分键，绝对 URI 与 Proxy-Authorization 正确")
    srv11 = new_server()
    pool.close_all()
    target = "http://upstream.invalid/ok"
    p1 = "http://127.0.0.1:%d" % srv11.port
    p2 = "http://user:pass@127.0.0.1:%d/" % srv11.port
    for _ in range(2):
        resp = fetch(target, proxy=p1)
        body = resp.read()
        resp.close()
    check("经代理的请求目标是绝对 URI", srv11.requests[0]["target"] == target,
          srv11.requests[0]["target"])
    check("Host 头是源站而不是代理",
          srv11.requests[0]["headers"].get("host") == "upstream.invalid")
    check("同一代理串的两条请求复用一条连接",
          body == OK_BODY and srv11.accepted == 1, srv11.accepted)
    for _ in range(2):
        resp = fetch(target, proxy=p2)
        resp.read()
        resp.close()
    check("不同代理串不共用连接（各开各的）", srv11.accepted == 2, srv11.accepted)
    check("带凭据的代理串发 Proxy-Authorization",
          srv11.requests[2]["headers"].get("proxy-authorization")
          == "Basic dXNlcjpwYXNz", srv11.requests[2]["headers"])
    keys = sorted(k[3] for k in pool.idle_counts() if k[3])
    check("两个代理串各占一个池键", keys == sorted([p1, p2]), keys)

    # ------------------------------------------------------------------
    print("[12] 3xx 重定向：回退 urllib 跟随，客户端拿到最终响应")
    srv12 = new_server()
    pool.close_all()
    resp = fetch(srv12.base() + "/redirect")
    body = resp.read()
    status = resp.status
    resp.close()
    check("最终拿到 200 与 /ok 的 body", status == 200 and body == OK_BODY,
          (status, body))
    check("服务端看到了原始请求与回退重放", len(srv12.requests) >= 2,
          len(srv12.requests))

    # ------------------------------------------------------------------
    print("[13] TLS（https 直连）：复用、探活、流式归还都成立")
    srv13 = new_server(tls=True)
    pool.close_all()

    def permissive(_version=None):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    # 客户端侧把默认的证书校验换成放行的，只为了让本地自签证书能握手。
    # 3.13 用 http.client._create_https_context，3.9~3.11 直接问
    # ssl._create_default_https_context；两边都在就都换，finally 里原样还回去。
    patched = []
    for module, name in ((http.client, "_create_https_context"),
                         (ssl, "_create_default_https_context")):
        if hasattr(module, name):
            patched.append((module, name, getattr(module, name)))
            setattr(module, name, permissive)
    try:
        bodies = []
        for _ in range(2):
            resp = fetch(srv13.base("https") + "/ok")
            bodies.append(resp.read())
            resp.close()
        check("TLS 上两条请求复用一条连接", bodies == [OK_BODY] * 2
              and srv13.accepted == 1, (srv13.accepted, bodies))
        resp = fetch(srv13.base("https") + "/sse")
        lines = list(resp)
        resp.close()
        resp = fetch(srv13.base("https") + "/ok")
        body = resp.read()
        resp.close()
        check("TLS + chunked 流读干净后可复用", len(lines) == 6
              and body == OK_BODY and srv13.accepted == 1, srv13.accepted)
        srv13.kill_conns()
        time.sleep(0.1)
        resp = fetch(srv13.base("https") + "/ok")
        body = resp.read()
        resp.close()
        check("TLS 上探活识别被掐掉的空闲连接",
              body == OK_BODY and srv13.accepted == 2, srv13.accepted)
    finally:
        for module, name, original in patched:
            setattr(module, name, original)

    # ------------------------------------------------------------------
    print("[14] 开关关闭：行为与改动前一致（Connection: close、无复用）")
    srv14 = new_server()
    pool.close_all()
    os.environ["WB_UPSTREAM_KEEPALIVE"] = "0"
    try:
        check("enabled() 认关闭开关", pool.enabled() is False)
        before = pool.stats()
        accepted0 = srv14.accepted
        for _ in range(2):
            resp = fetch(srv14.base() + "/ok")
            resp.read()
            resp.close()
        check("两次请求两条连接（无复用）", srv14.accepted - accepted0 == 2,
              srv14.accepted - accepted0)
        check("开关关闭时不碰池", sdelta(before) == {k: 0 for k in before},
              sdelta(before))
        check("走的是原来的 urllib 路径（Connection: close）",
              all(q["headers"].get("connection") == "close" for q in srv14.requests))
        # 同一条请求，两种路径各发一次：除 Connection 外，线上头完全一致
        def post_once():
            body = b'{"model":"x","messages":[]}'
            resp = fetch(srv14.base() + "/echo", data=body, method="POST", headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer t",
                "X-User-Id": "u1",
                "User-Agent": "WorkBuddy/5.5.2",
            })
            resp.read()
            resp.close()

        os.environ.pop("WB_UPSTREAM_KEEPALIVE", None)
        post_once()                       # 池路径
        os.environ["WB_UPSTREAM_KEEPALIVE"] = "0"
        post_once()                       # 原 urllib 路径
    finally:
        os.environ.pop("WB_UPSTREAM_KEEPALIVE", None)
    h_pool = srv14.requests[-2]["headers"]
    h_urllib = srv14.requests[-1]["headers"]
    pool_side = {k: v for k, v in h_pool.items() if k != "connection"}
    urllib_side = {k: v for k, v in h_urllib.items() if k != "connection"}
    check("线上头与改动前一致（只差 Connection 的值）", pool_side == urllib_side,
          (h_pool, h_urllib))
    check("池路径 keep-alive / 原路径 close",
          h_pool.get("connection") == "keep-alive"
          and h_urllib.get("connection") == "close")
    check("POST 的 Content-Length 一致",
          h_pool.get("content-length") == h_urllib.get("content-length")
          == "27", (h_pool.get("content-length"), h_urllib.get("content-length")))

    # ------------------------------------------------------------------
    print("[15] 系统代理旁路：命中旁路清单时直连（与 urllib 的 proxy_bypass 一致）")
    srv15 = new_server()
    pool.close_all()
    # 这个「代理」地址不可连：如果池没做旁路判断，请求会打到它并失败
    dead_proxy = "http://127.0.0.1:1"
    original_bypass = urllib.request.proxy_bypass
    urllib.request.proxy_bypass = lambda host: True
    try:
        resp = fetch(srv15.base() + "/ok", proxy=dead_proxy)
        body = resp.read()
        resp.close()
    finally:
        urllib.request.proxy_bypass = original_bypass
    check("旁路命中时直连源站（没有去连那个不可用的代理）", body == OK_BODY, body)
    check("旁路命中的连接归入直连池键（键里的代理串为空）",
          any(key[3] == "" for key in pool.idle_counts()), pool.idle_counts())
    check("源站确实收到了这条请求", len(srv15.requests) == 1, len(srv15.requests))

    # ------------------------------------------------------------------
    print("[16] 单元：代理串解析 / 开关 / 死连接判定")
    sp = pool._split_proxy
    check("_split_proxy: 空串 -> None", sp("") is None)
    info = sp("http://h:8080")
    check("_split_proxy: http://h:8080",
          info and info["host"] == "h" and info["port"] == "8080", info)
    info = sp("http://u:p%40x@h:1")
    check("_split_proxy: 凭据解码（p%40x -> p@x）",
          info and info["user"] == "u" and info["password"] == "p@x", info)
    info = sp("h:3128")
    check("_split_proxy: authority 形式 host:port",
          info and info["host"] == "h" and info["port"] == "3128", info)
    check("_split_proxy: socks5 -> None（回退原路径）", sp("socks5://h:1") is None)
    check("_split_proxy: https 代理 -> None（回退原路径）", sp("https://h:1") is None)
    info = sp("http://[::1]:8080")
    check("_split_proxy: IPv6 字面量", info and info["host"] == "::1", info)
    info = sp("http://[::1]")
    check("_split_proxy: IPv6 无端口", info and info["host"] == "::1"
          and info["port"] == "", info)
    check("_split_proxy: 非数字端口 -> None（回退原路径）",
          sp("http://h:notaport") is None)
    os.environ.pop("WB_UPSTREAM_KEEPALIVE", None)
    check("enabled(): 未设置默认开", pool.enabled() is True)
    for off in ("0", "off", "FALSE", "No"):
        os.environ["WB_UPSTREAM_KEEPALIVE"] = off
        if pool.enabled() is not False:
            check("enabled(): %r 关闭" % off, False)
            break
    else:
        check("enabled(): 0/off/false/no 都关闭", True)
    os.environ.pop("WB_UPSTREAM_KEEPALIVE", None)
    os.environ["WB_UPSTREAM_KEEPALIVE"] = "1"
    check("enabled(): 1 开启", pool.enabled() is True)
    os.environ.pop("WB_UPSTREAM_KEEPALIVE", None)
    stale = pool._stale_connection_error
    check("死连接判定：RemoteDisconnected / BrokenPipe / 重置 都算",
          stale(http.client.RemoteDisconnected("x")) is True
          and stale(BrokenPipeError()) is True
          and stale(ConnectionResetError()) is True
          and stale(ssl.SSLEOFError(8, "EOF")) is True)
    check("死连接判定：超时 / 普通 OSError 不算",
          stale(TimeoutError("t")) is False and stale(OSError("x")) is False)
    try:
        pool.urlopen(urllib.request.Request("ftp://example.com/x"))
        bypassed = False
    except pool.PoolBypass:
        bypassed = True
    check("非 http(s) 目标抛 PoolBypass（回退原路径）", bypassed)
    raised = None
    try:
        fetch("http://upstream.invalid/ok", proxy="socks5://127.0.0.1:1", timeout=3)
    except Exception as exc:
        raised = exc
    check("非 HTTP 代理回退原路径（错误形状与改动前一致）",
          isinstance(raised, urllib.error.URLError), repr(raised))

    # ------------------------------------------------------------------
    print("[17] 头名保真：有序 (name, value) 原样上线（chat 路径）")
    srv17 = new_server()
    pool.close_all()
    # 与 wb_identity.build_identity_headers 同形的头名：ID / IDE / B3 都是全大写
    # 缩写，traceparent / b3 是小写——正是 .title() 会改错的那批。
    exact = [
        ("Content-Type", "application/json"),
        ("Accept", "application/json, text/plain, */*"),
        ("X-Requested-With", "XMLHttpRequest"),
        ("Origin", "https://www.workbuddy.ai"),
        ("Referer", "https://www.workbuddy.ai/"),
        ("X-CodeBuddy-Request", "1"),
        ("Accept-Language", "en-US"),
        ("X-Agent-Intent", "craft"),
        ("X-IDE-Type", "WorkBuddy"),
        ("X-IDE-Version", "5.8.4"),
        ("User-Agent", "WorkBuddy/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0"),
        ("Authorization", "Bearer tok-exact"),
        ("X-User-Id", "uid-exact"),
        ("X-Conversation-ID", "4c470210-e6c7-41f4-a163-9b84b5a70485"),
        ("X-Conversation-Request-ID", "01a14a72172775568e9c011c87bf2463"),
        ("X-Root-Request-ID", "01a14a72172775568e9c011c87bf2463"),
        ("X-Conversation-Message-ID", "01a14a721727766bbd5b56f85485e50d"),
        ("X-Request-ID", "01a14a721727766bbd5b56f85485e50d"),
        ("X-Trace-ID", "01a14a7217277c8086fcf8675238cbf2"),
        ("traceparent", "00-01a14a7217277c8086fcf8675238cbf2-da0eb39ec077a50c-01"),
        ("b3", "01a14a7217277c8086fcf8675238cbf2-da0eb39ec077a50c-1"),
        ("X-B3-TraceId", "01a14a7217277c8086fcf8675238cbf2"),
        ("X-B3-SpanId", "da0eb39ec077a50c"),
        ("X-B3-Sampled", "1"),
    ]
    req = urllib.request.Request(srv17.base() + "/echo", data=b'{"x":1}',
                                 method="POST")
    req.wb_exact_headers = exact          # wb_proxy.open_upstream 的同款挂法
    resp = wb_accounts.urlopen(req, timeout=5)
    resp.read()
    resp.close()
    sent = [name for name, _value in srv17.requests[0]["header_lines"]]
    want = (["Accept-Encoding", "Content-Length", "Host"]
            + [name for name, _value in exact] + ["Connection"])
    check("线上头名与顺序逐项原样（不再 .title()）", sent == want, (sent, want))
    check("ID / IDE / B3 / traceparent 的大小写没被改写",
          all(name in sent for name in ("X-Request-ID", "X-Conversation-ID",
                                        "X-IDE-Type", "X-B3-TraceId",
                                        "X-B3-SpanId", "traceparent", "b3")),
          sent)
    check("没有出现 .title() 的旧形态",
          not any(name in sent for name in ("X-Request-Id", "X-Conversation-Id",
                                            "X-Ide-Type", "X-B3-Traceid",
                                            "X-B3-Spanid", "Traceparent", "B3")),
          sent)
    # 默认路径（billing / 登录 / 签到等）不挂 wb_exact_headers，行为必须与改动前
    # 完全一致：仍是 .title() 形态。
    req = urllib.request.Request(srv17.base() + "/echo", data=b'{"x":1}',
                                 method="POST", headers=dict(exact))
    resp = wb_accounts.urlopen(req, timeout=5)
    resp.read()
    resp.close()
    legacy = [name for name, _value in srv17.requests[-1]["header_lines"]]
    check("未声明保真的请求仍是原 .title() 路径（billing 等不回归）",
          "X-Request-Id" in legacy and "X-Ide-Type" in legacy
          and "Traceparent" in legacy and "B3" in legacy, legacy)

    # ------------------------------------------------------------------
    print("[18] 按账号分池：不同凭据不共用连接，同凭据仍复用")
    srv18 = new_server()
    pool.close_all()

    def call(token):
        req = urllib.request.Request(srv18.base() + "/ok",
                                     headers={"Authorization": "Bearer " + token})
        resp = wb_accounts.urlopen(req, timeout=5)
        body = resp.read()
        resp.close()
        return body

    call("tok-a")
    call("tok-a")
    check("同一凭据的两次请求复用一条连接", srv18.accepted == 1, srv18.accepted)
    call("tok-b")
    check("不同凭据各开一条连接（不再跨账号共用）", srv18.accepted == 2,
          srv18.accepted)
    call("tok-a")
    check("凭据 A 回到自己的池连接，没有落到 B 的连接上", srv18.accepted == 2,
          srv18.accepted)
    identities = {key[4] for key in pool.idle_counts() if key[4]}
    check("池键里带上了凭据摘要（两个账号两个键）", len(identities) == 2,
          pool.idle_counts())
    # 没有凭据的请求（登录前 / 匿名探活）仍共用一个键，与改动前一致。
    srv18b = new_server()
    pool.close_all()
    for _ in range(2):
        req = urllib.request.Request(srv18b.base() + "/ok")
        resp = wb_accounts.urlopen(req, timeout=5)
        resp.read()
        resp.close()
    check("无凭据请求仍复用一条连接", srv18b.accepted == 1, srv18b.accepted)
    # 退避口径不变：egress_key 仍是物理出口的 4 元组，不掺账号。
    check("egress_key 仍是 4 元组、不含账号",
          len(pool.egress_key(srv18.base() + "/ok", "")) == 4,
          pool.egress_key(srv18.base() + "/ok", ""))
finally:
    for srv in servers:
        srv.stop()
    pool.close_all()

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
