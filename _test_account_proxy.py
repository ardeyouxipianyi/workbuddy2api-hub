"""Deterministic tests for per-account outbound proxy support.

No external network: a local stub proxy records absolute-URI requests and
answers with canned JSON, so we can prove an account's traffic is routed
through its configured proxy instead of the default direct opener.
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault(
    "ACCOUNTS_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "_acc_proxy"),
)
os.environ.setdefault(
    "USAGE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "_use_proxy")
)

import wb_accounts as A

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


SEEN = []


class StubProxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen = None

    def do_GET(self):
        if self.seen is not None:
            self.seen.append(self.path)
        body = json.dumps({"ok": True, "via": "proxy", "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def start_stub(seen=None):
    handler = type("StubProxyBound", (StubProxy,), {"seen": seen})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


print("[1] Account persists and exposes its proxy binding")
acc = A.Account(
    {
        "uid": "u-proxy-1",
        "nickname": "p1",
        "domain": "www.workbuddy.ai",
        "realm": "intl",
        "accessToken": "",
        "proxy": "http://127.0.0.1:9",
    }
)
check("proxy loaded from data", acc.proxy == "http://127.0.0.1:9", acc.proxy)
check("proxy survives to_dict", acc.to_dict().get("proxy") == "http://127.0.0.1:9")
check("proxy exposed in public()", acc.public().get("proxy") == "http://127.0.0.1:9")

acc2 = A.Account({"uid": "u-proxy-2", "domain": "www.workbuddy.ai", "realm": "intl"})
check("missing proxy defaults to empty", acc2.proxy == "", repr(acc2.proxy))

print()
print("[2] opener_for_proxy: direct when empty, opener when set")
check("no proxy -> None (direct)", A.opener_for_proxy("") is None)
check("whitespace proxy -> None (direct)", A.opener_for_proxy("   ") is None)
op = A.opener_for_proxy("http://127.0.0.1:9")
check("proxy set -> opener object", op is not None and hasattr(op, "open"))
check("opener is cached per proxy", A.opener_for_proxy("http://127.0.0.1:9") is op)

print()
print("[3] urlopen routes through the account proxy")
srv, port = start_stub(SEEN)
direct, direct_port = start_stub()
try:
    req = A.urllib.request.Request("http://upstream.invalid/v1/models", method="GET")
    with A.urlopen(req, timeout=5, proxy="http://127.0.0.1:%d" % port) as resp:
        payload = json.loads(resp.read().decode())
    check("request reached the stub proxy", len(SEEN) == 1, SEEN)
    check(
        "proxy saw absolute URI",
        SEEN and SEEN[0].startswith("http://upstream.invalid/"),
        SEEN,
    )
    check("response parsed via proxy", payload.get("via") == "proxy", payload)

    SEEN.clear()
    with A.urlopen(
        A.urllib.request.Request("http://127.0.0.1:%d/x" % direct_port, method="GET"),
        timeout=5,
        proxy="",
    ) as resp:
        direct_payload = json.loads(resp.read().decode())
    check("empty proxy goes direct (stub untouched)", len(SEEN) == 0, SEEN)
    check(
        "empty proxy reached the direct server",
        direct_payload.get("path") == "/x",
        direct_payload,
    )
except Exception as exc:
    check("urlopen through proxy raised", False, repr(exc))
finally:
    srv.shutdown()
    direct.shutdown()

print()
print("[4] http_json honours the proxy argument")
srv, port = start_stub(SEEN)
try:
    SEEN.clear()
    data = A.http_json(
        "http://billing.invalid/credits",
        method="GET",
        headers={"X-Test": "1"},
        timeout=5,
        retries=1,
        proxy="http://127.0.0.1:%d" % port,
    )
    check("http_json returned proxy payload", data.get("via") == "proxy", data)
    check("http_json used the proxy", len(SEEN) == 1, SEEN)
except Exception as exc:
    check("http_json through proxy raised", False, repr(exc))
finally:
    srv.shutdown()

print()
print("[5] AccountPool.set_proxy persists the binding")
import tempfile

tmpdir = tempfile.mkdtemp(prefix="wbproxy-")
pool = A.AccountPool(tmpdir, log=lambda m: None)
pool.add(
    A.Account(
        {
            "uid": "u-pool-1",
            "nickname": "pool1",
            "domain": "www.workbuddy.ai",
            "realm": "intl",
            "accessToken": "",
        }
    )
)
view = pool.set_proxy("u-pool-1", "http://cli-proxy-mihomo:17901")
check(
    "set_proxy returns public view",
    view and view.get("proxy") == "http://cli-proxy-mihomo:17901",
    view,
)
reloaded = A.AccountPool(tmpdir, log=lambda m: None)
reloaded.load()
again = reloaded.get("u-pool-1")
check(
    "proxy survives reload from disk",
    again and again.proxy == "http://cli-proxy-mihomo:17901",
    again.proxy if again else None,
)
check("set_proxy on unknown uid -> None", pool.set_proxy("nope", "http://x") is None)
check(
    "set_proxy('') clears the binding",
    (pool.set_proxy("u-pool-1", "") or {}).get("proxy") == "",
)

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
