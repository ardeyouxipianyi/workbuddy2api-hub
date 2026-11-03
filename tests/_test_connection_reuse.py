"""A rejected request must not desynchronise a keep-alive connection.

An early rejection (401, 429, 404) used to reply without reading the request
body, so the payload stayed in the socket. The next request on that connection
then parsed the leftover JSON as its request line and failed - surfacing as a
bogus "414 Request-URI Too Long" with an empty request line in the log, on an
otherwise healthy connection.

The same desync has a second entry point: a panel route that answers 200 without
parsing the `{}` body its button posts. The leftover two bytes then become the
next request line ("{}GET /usage?..." -> 501), which the dashboard shows as
"cannot reach the reverse proxy" while everything behind it is healthy. Both
shapes are covered here, because both are invisible to an HTTP client library
that silently opens a fresh connection.

Any client using a connection pool hits this as "random" failures that
disappear on retry, which is why it is worth a standing test. A raw socket is
required: an HTTP client library would silently open a fresh connection and
hide the bug entirely.

Each response is read up to its own boundary instead of being waited for with a
fixed sleep, so the suite synchronizes on the server's answer rather than on
machine speed: a correct server on a busy runner used to answer after the sleep
had already expired, which is indistinguishable from the regression under test.

Starts a real server on a spare port with a throwaway store, so it needs no
network access and does not touch the real accounts.
"""

import json
import os
import shutil
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
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (label, detail))


# How long a complete response may take before the suite calls the server
# broken. A working gateway answers in milliseconds; the bound exists so a
# stalled one fails the suite instead of holding the job until the runner's own
# timeout fires.
RESPONSE_DEADLINE = 15.0

# 面板会话口令：这一套件要用带 `{}` body 的面板按钮验证连接是否保持同步，而
# 那些路由都要面板身份。口令只存在于临时 store 里，跑完随目录删掉。
PANEL_PASSWORD = "conn-reuse-suite"


class IncompleteResponse(Exception):
    """The peer closed before the response it had started was complete.

    Kept apart from the deadline: a timeout means the answer is late, this means
    it will never arrive. Both are failures. The reader is the suite's
    synchronization point, so a truncated exchange must never be handed back as
    a finished one - "looks complete" is exactly the reading this suite exists
    to refuse.
    """


def content_length(head):
    """The body length the headers declare, or None when they declare none."""
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None


def read_response(sock, deadline=RESPONSE_DEADLINE):
    """Read one complete HTTP response off a raw socket, bounded by `deadline`.

    Returns the bytes exactly as they arrived, so the assertions below stay on
    the wire format. Stops at the response's own boundary - the Content-Length
    body when the headers declare one, otherwise the close - which is what
    leaves the connection reusable for the next request. An answer that never
    completes fails instead of coming back short: TimeoutError when the deadline
    passes with the peer still open, IncompleteResponse when the peer closes
    early. Either way the caller never receives a truncated response that could
    pass for a whole one.

    Chunked framing is deliberately not implemented. The routes under test
    answer with a Content-Length, and a body with no end must fail loudly
    instead of being guessed at.
    """
    end = time.monotonic() + deadline
    no_response = "no complete response within %.1fs" % deadline

    def recv(size):
        left = end - time.monotonic()
        if left <= 0:
            raise TimeoutError(no_response)
        sock.settimeout(left)
        try:
            return sock.recv(size)
        except socket.timeout:
            # socket.timeout only became an alias of TimeoutError in 3.10 and
            # the CI floor is 3.9, where recv() raises a class of its own:
            # normalize it, so one exception type means "the bound was hit".
            raise TimeoutError(no_response) from None

    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = recv(4096)
        if not chunk:
            raise IncompleteResponse(
                "closed after %d bytes, before the header block ended" % len(buf))
        buf += chunk
    head, _, body = buf.partition(b"\r\n\r\n")
    length = content_length(head)
    if length is None:
        # No length: the close ends the body, which is how the 414 path replies.
        while True:
            chunk = recv(4096)
            if not chunk:
                return head + b"\r\n\r\n" + body
            body += chunk
    while len(body) < length:
        chunk = recv(min(4096, length - len(body)))
        if not chunk:
            raise IncompleteResponse(
                "closed after %d of the %d declared body bytes" % (len(body), length))
        body += chunk
    return head + b"\r\n\r\n" + body


port = life.free_port()
work = tempfile.mkdtemp(prefix="connreuse_")
store = os.path.join(work, "accounts")
os.makedirs(store)
with open(os.path.join(store, "settings.json"), "w", encoding="utf-8") as fh:
    json.dump({"api_keys": [{"id": "k1", "name": "t", "key": "GOODKEY",
                             "enabled": True}]}, fh)

# Managed spawn: its own group/session, so the stop_managed() in the finally
# below takes the whole tree - a gateway that leaves a helper behind also leaves
# its port held.
proc = life.spawn_managed(
    [PY, os.path.join(ROOT, "wb_proxy.py"), "--port", str(port), "--host", "127.0.0.1",
     "--accounts-dir", store, "--panel-password", PANEL_PASSWORD],
    cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    env=dict(os.environ, WB_PROXY_USAGE_DIR=os.path.join(work, "usage"),
             # 更新检查的仓库指向一个不存在的域名：/updates/check 的排空行为要测，
             # 但这一套件不该碰网络，也不该吃 GitHub 的匿名配额。域名解析必然失败，
             # 检查本身按契约返回 200 + last_error。
             WB_UPDATE_REPO="offline.invalid/none"))


def wait_ready():
    """Wait for /health to answer; a gateway that could not bind exits instead."""
    for _ in range(40):
        time.sleep(0.5)
        try:
            c = socket.create_connection(("127.0.0.1", port), timeout=2)
            c.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            data = c.recv(200)
            c.close()
            if b"200" in data:
                return True
        except Exception:
            if proc.poll() is not None:
                return False
    return False


def send_pair(bad_body, label):
    """Bad-key request with `bad_body`, then a good small one, same socket."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=20)
    try:
        sock.sendall(
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
            b"Authorization: Bearer BADKEY\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(bad_body)).encode() + b"\r\n\r\n" + bad_body
        )
        first = read_response(sock)
        small = b'{"model":"x","messages":[{"role":"user","content":"hi"}]}'
        sock.sendall(
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
            b"Authorization: Bearer GOODKEY\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(small)).encode() + b"\r\n\r\n" + small
        )
        try:
            second = read_response(sock)
        except Exception as exc:
            second = ("recv error: %s" % exc).encode()
    finally:
        sock.close()

    first_line = first.split(b"\r\n", 1)[0].decode("latin-1") if first else "<closed>"
    second_line = second.split(b"\r\n", 1)[0].decode("latin-1") if second else "<closed>"
    print("      first:  %s" % first_line)
    print("      second: %s" % second_line)
    return first, second


def panel_login():
    """Open a panel session; returns its token, or None when it was refused."""
    body = json.dumps({"password": PANEL_PASSWORD}).encode()
    sock = socket.create_connection(("127.0.0.1", port), timeout=20)
    try:
        sock.sendall(b"POST /panel/login HTTP/1.1\r\nHost: x\r\n"
                     b"Content-Type: application/json\r\n"
                     b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        resp = read_response(sock)
    finally:
        sock.close()
    head, _, payload = resp.partition(b"\r\n\r\n")
    if b" 200 " not in head.split(b"\r\n", 1)[0]:
        return None
    try:
        return json.loads(payload.decode("utf-8")).get("token")
    except ValueError:
        return None


def send_panel_pair(path, token):
    """`{}` to a body-less panel route, then a good panel GET, same socket.

    The body is what the dashboard's buttons actually send (`postJSON(path, {})`):
    two bytes the route never parses. The GET that follows stands in for the
    panel's next poll, which is what used to be answered with a 501.
    """
    sock = socket.create_connection(("127.0.0.1", port), timeout=30)
    try:
        sock.sendall(
            b"POST " + path.encode() + b" HTTP/1.1\r\nHost: x\r\n"
            b"X-Panel-Token: " + token.encode() + b"\r\n"
            b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
        # /updates/check 会去问一次发布（这里的仓库指向不存在的域名，按契约 5s
        # 封顶、失败也只回状态），所以给它单独的、更宽的截止时间。
        first = read_response(sock, deadline=30.0)
        sock.sendall(
            b"GET /usage?realm=intl HTTP/1.1\r\nHost: x\r\n"
            b"X-Panel-Token: " + token.encode() + b"\r\n\r\n")
        try:
            second = read_response(sock)
        except Exception as exc:
            second = ("recv error: %s" % exc).encode()
    finally:
        sock.close()

    first_line = first.split(b"\r\n", 1)[0].decode("latin-1") if first else "<closed>"
    second_line = second.split(b"\r\n", 1)[0].decode("latin-1") if second else "<closed>"
    print("      first:  %s" % first_line)
    print("      second: %s" % second_line)
    return first, second


try:
    if not wait_ready():
        print("  [FAIL] server did not start")
        sys.exit(1)

    print("[1] large body rejected with 401, then a good request")
    big = json.dumps({"model": "x",
                      "messages": [{"role": "user", "content": "A" * 70000}]}).encode()
    first, second = send_pair(big, "large")
    check("the bad request was rejected", b"401" in first)
    check("the connection stayed in sync (not 414)", b"414" not in second,
          second.split(b"\r\n", 1)[0].decode("latin-1", "replace"))
    check("the second request reached the application",
          second.startswith(b"HTTP/1.1") and b"414" not in second)

    print("[2] small body rejected with 401, then a good request")
    small_bad = b'{"model":"x","messages":[{"role":"user","content":"hi"}]}'
    first, second = send_pair(small_bad, "small")
    check("the bad request was rejected", b"401" in first)
    check("the second request parsed", b"414" not in second,
          second.split(b"\r\n", 1)[0].decode("latin-1", "replace"))

    print("[3] a good request alone still works")
    sock = socket.create_connection(("127.0.0.1", port), timeout=15)
    body = b'{"model":"x","messages":[{"role":"user","content":"hi"}]}'
    sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
                 b"Authorization: Bearer GOODKEY\r\nContent-Type: application/json\r\n"
                 b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    try:
        resp = read_response(sock)
    except Exception:
        resp = b""
    sock.close()
    check("no accounts configured -> clean JSON 503, not a hang",
          resp.startswith(b"HTTP/1.1 503") and b"no usable account" in resp,
          resp.split(b"\r\n", 1)[0].decode("latin-1", "replace"))

    print("[4] an over-long request line answers JSON, not stdlib HTML")
    sock = socket.create_connection(("127.0.0.1", port), timeout=15)
    sock.sendall(b"GET /" + b"a" * (2 * 1024 * 1024) + b" HTTP/1.1\r\nHost: x\r\n\r\n")
    try:
        resp = read_response(sock)
    except Exception:
        resp = b""
    sock.close()
    check("over-long request line -> 414", b"414" in resp[:40],
          resp.split(b"\r\n", 1)[0].decode("latin-1", "replace"))
    check("the 414 body is our JSON error shape", b"application/json" in resp,
          resp[:120].decode("latin-1", "replace"))

    # The reader's bound, checked here rather than trusted: a peer that never
    # answers, and one that stops mid-body, must both fail the read on its
    # deadline. Without this the deadline could be deleted and every other check
    # would still pass - on a server that answers.
    print("[5] the reader's deadline is a bound, not a wait")
    quiet, silent = socket.socketpair()
    try:
        for stall in ("never answers", "stops mid-body"):
            if stall == "stops mid-body":
                silent.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 40\r\n\r\nhalf")
            started = time.monotonic()
            try:
                read_response(quiet, deadline=0.5)
                raised = False
            except TimeoutError:
                raised = True
            waited = time.monotonic() - started
            check("a peer that %s fails the read" % stall, raised)
            # Loose on purpose: the ceiling only has to separate "gave up" from
            # "hung", and a loaded runner may be slow to schedule the wakeup.
            # The floor is loose too: a 0.5s deadline can wake a few
            # milliseconds early once the OS timer granularity is applied, so
            # an exact 0.5 lower bound reports a flake under parallel load.
            check("and gives up on its deadline, not eventually",
                  0.4 <= waited < 8.0, "%.2fs" % waited)
    finally:
        quiet.close()
        silent.close()

    # The other half of "never complete": the peer closes early. A truncated
    # exchange must fail the read rather than come back as a short response that
    # a caller could take for the whole answer.
    print("[6] a truncated response fails instead of passing for complete")
    for truncation, payload in (
            ("headers", b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"),
            ("body", b"HTTP/1.1 200 OK\r\nContent-Length: 40\r\n\r\nhalf")):
        quiet, peer = socket.socketpair()
        try:
            peer.sendall(payload)
            peer.close()              # EOF: the rest of the response never comes
            try:
                partial = read_response(quiet, deadline=5.0)
                raised = None
            except Exception as exc:
                partial, raised = None, exc
            check("a response truncated in the %s fails the read" % truncation,
                  isinstance(raised, IncompleteResponse),
                  "%s: %r" % (type(raised).__name__ if raised else "returned", partial))
        finally:
            quiet.close()
            peer.close()

    # The other entry point into the same desync: a panel route that answers 200
    # without parsing the `{}` body its button posts. The two leftover bytes then
    # become the next request line, and the dashboard's next poll gets a 501 -
    # which it renders as "cannot reach the reverse proxy" on a healthy gateway.
    # /shutdown belongs to the same set but stops the server, so it is not driven
    # here; /updates/check is the one that bites in the field, so it is.
    print("[7] a body-less panel POST keeps the connection in sync")
    token = panel_login()
    check("the panel session opened", bool(token))
    if token:
        for path, want in (("/console/toggle", b"200"),
                           ("/updates/check", b"200"),
                           ("/updates/install", b"409"),
                           ("/quota-scan/run", b"200")):
            first, second = send_panel_pair(path, token)
            check("POST %s answered %s" % (path, want.decode()),
                  want in first.split(b"\r\n", 1)[0],
                  first.split(b"\r\n", 1)[0].decode("latin-1", "replace"))
            check("the request after %s still parsed (not 501)" % path,
                  second.startswith(b"HTTP/1.1 200")
                  and b"501" not in second.split(b"\r\n", 1)[0],
                  second.split(b"\r\n", 1)[0].decode("latin-1", "replace"))
finally:
    life.stop_managed(proc)
    shutil.rmtree(work, ignore_errors=True)

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
