"""A rejected request must not desynchronise a keep-alive connection.

An early rejection (401, 429, 404) used to reply without reading the request
body, so the payload stayed in the socket. The next request on that connection
then parsed the leftover JSON as its request line and failed - surfacing as a
bogus "414 Request-URI Too Long" with an empty request line in the log, on an
otherwise healthy connection.

Any client using a connection pool hits this as "random" failures that
disappear on retry, which is why it is worth a standing test. A raw socket is
required: an HTTP client library would silently open a fresh connection and
hide the bug entirely.

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


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


port = free_port()
work = tempfile.mkdtemp(prefix="connreuse_")
store = os.path.join(work, "accounts")
os.makedirs(store)
with open(os.path.join(store, "settings.json"), "w", encoding="utf-8") as fh:
    json.dump({"api_keys": [{"id": "k1", "name": "t", "key": "GOODKEY",
                             "enabled": True}]}, fh)

proc = subprocess.Popen(
    [PY, os.path.join(ROOT, "wb_proxy.py"), "--port", str(port), "--host", "127.0.0.1",
     "--accounts-dir", store],
    cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    env=dict(os.environ, WB_PROXY_USAGE_DIR=os.path.join(work, "usage")),
)


def wait_ready():
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
        time.sleep(1.0)
        first = sock.recv(400)

        small = b'{"model":"x","messages":[{"role":"user","content":"hi"}]}'
        sock.sendall(
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
            b"Authorization: Bearer GOODKEY\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(small)).encode() + b"\r\n\r\n" + small
        )
        time.sleep(2.5)
        try:
            second = sock.recv(900)
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
    time.sleep(2.5)
    try:
        resp = sock.recv(900)
    except Exception:
        resp = b""
    sock.close()
    check("no accounts configured -> clean JSON 503, not a hang",
          resp.startswith(b"HTTP/1.1 503") and b"no usable account" in resp,
          resp.split(b"\r\n", 1)[0].decode("latin-1", "replace"))

    print("[4] an over-long request line answers JSON, not stdlib HTML")
    sock = socket.create_connection(("127.0.0.1", port), timeout=15)
    sock.sendall(b"GET /" + b"a" * (2 * 1024 * 1024) + b" HTTP/1.1\r\nHost: x\r\n\r\n")
    time.sleep(2.5)
    try:
        resp = sock.recv(900)
    except Exception:
        resp = b""
    sock.close()
    check("over-long request line -> 414", b"414" in resp[:40],
          resp.split(b"\r\n", 1)[0].decode("latin-1", "replace"))
    check("the 414 body is our JSON error shape", b"application/json" in resp,
          resp[:120].decode("latin-1", "replace"))
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    shutil.rmtree(work, ignore_errors=True)

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
