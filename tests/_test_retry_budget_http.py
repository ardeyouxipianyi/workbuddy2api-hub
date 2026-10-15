"""重试链预算与断连感知的端到端面。

Runs a real gateway process-in-thread against a mock upstream that reads the
request and then never sends a response header - the shape that used to keep
the chain rotating accounts long after the caller had given up. Checks:

  1. 预算花完 -> 504 + Retry-After，且耗时被夹在预算附近；
  2. 客户端先走 -> 链在当次尝试失败后就收手，不再开第二次尝试；
  3. 429 仍然是 429（限流语义没被预算改写）。

No upstream credentials and no outbound network are used.
"""
import base64
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import _lifecycle as life  # noqa: E402  (shared spare-port pick)

WORK = tempfile.mkdtemp(prefix="retry-budget-http-")
ACCOUNTS = os.path.join(WORK, "accounts")
os.makedirs(ACCOUNTS, exist_ok=True)
USAGE = os.path.join(WORK, "usage")
os.makedirs(USAGE, exist_ok=True)
os.environ["ACCOUNTS_DIR"] = ACCOUNTS
os.environ["WB_PROXY_USAGE_DIR"] = USAGE
os.environ["HOME"] = WORK
os.environ["USERPROFILE"] = WORK
os.environ["WB_PROXY_KEY"] = "TESTKEY"


def jwt(uid):
    def seg(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return seg({"alg": "none", "typ": "JWT"}) + "." + seg(
        {"uid": uid, "exp": int(time.time()) + 86400 * 30}) + ".x"


# 4 个账号：预算把它截在 2 次尝试上，「是预算收的手、不是池子空了」才可证。
# lastDailyChat 写成今天：调度器启动 10 秒后会跑一次无条件巡检，其中对国际账号
# 只有「每日活跃打卡」这一步会发请求——它会用一个代表账号往同一个假上游打一发
# "Hi"，把「这一轮链打了几次」数成 2 且换个账号（CI 上就是它把这条用例搞红的）。
# 标成今天已打卡，can_daily_chat() 即为 False，那次巡检对本套件就没有出站请求了。
_TODAY = time.strftime("%Y-%m-%d %H:%M:%S")
for index in (1, 2, 3, 4):
    uid = "acct-budget-%d" % index
    with open(os.path.join(ACCOUNTS, "acct%d.json" % index), "w", encoding="utf-8") as fh:
        json.dump({
            "uid": uid,
            "accessToken": jwt(uid),
            "refreshToken": "",
            "expiresAt": int(time.time()) + 86400 * 30,
            "realm": "intl",
            "domain": "www.workbuddy.ai",
            "platform": "CLI",
            "product": "workbuddy",
            "lastDailyChat": _TODAY,
            "enabled": True,
        }, fh)

UPSTREAM_PORT = life.free_port()
MOCK = {"mode": "stall", "attempts": 0, "uids": set(), "stall_seconds": 30}


class MockUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        MOCK["attempts"] += 1
        MOCK["uids"].add(self._account_uid())
        if MOCK["mode"] == "429":
            payload = json.dumps({"code": 14003, "msg": "too many requests",
                                  "requestId": "mock"}).encode("utf-8")
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        # stall：读过请求就什么都不发，等客户端自己超时。
        time.sleep(MOCK["stall_seconds"])

    def _account_uid(self):
        """这一轮是哪个账号打的（JWT 的 uid 声明）。

        链的轮次不能拿「假上游收到几次 POST」来数：wb_upstream_pool 在拿到一条
        已被对端关掉的 keep-alive 连接时会自己重发一次，同一条请求因此可能落两
        次 POST。重发不会换号，所以「换没换号」要看账号数，不看 POST 数。
        """
        auth = self.headers.get("Authorization") or ""
        token = auth.split(" ", 1)[1] if auth.startswith("Bearer ") else ""
        parts = token.split(".")
        if len(parts) < 2:
            return ""
        try:
            raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
            return json.loads(raw.decode("utf-8")).get("uid") or ""
        except Exception:
            return ""

    def do_GET(self):
        payload = b'{"data":{}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


upstream = ThreadingHTTPServer(("127.0.0.1", UPSTREAM_PORT), MockUpstream)
threading.Thread(target=upstream.serve_forever, daemon=True).start()

import wb_identity  # noqa: E402
import wb_proxy as P  # noqa: E402
import wb_settings  # noqa: E402

_ENDPOINTS = dict(wb_identity._ENDPOINTS)
for key in list(_ENDPOINTS):
    _ENDPOINTS[key] = ("http://127.0.0.1:%d" % UPSTREAM_PORT, _ENDPOINTS[key][1])
wb_identity._ENDPOINTS.update(_ENDPOINTS)

# 小值让测试快：预算 6s、首次 2s、响应头 2s。4 个账号够跑 5 次尝试，
# 所以停在第 2 次只能是预算收的手。
wb_settings.set_upstream_config(ACCOUNTS, {
    "header_timeout_seconds": 2,
    "idle_timeout_seconds": 30,
    "retry_budget_seconds": 6,
    "first_attempt_timeout_seconds": 2,
})

GATEWAY_PORT = life.free_port()


class Args(object):
    host = "127.0.0.1"
    port = GATEWAY_PORT
    api_key = "TESTKEY"
    system_prompt = "You are a helpful assistant."
    user_agent = None
    usage_dir = USAGE
    accounts_dir = ACCOUNTS
    import_desktop = False
    lan = False
    panel_password = None
    info = None


P._apply_cli_overrides(Args())
P._bootstrap_runtime(Args())
# 隔离自检：这批账号今天都「已打卡」，调度器启动后的那次巡检就不该对国际账号
# 发任何出站请求。少了它，假上游的计数会被后台流量污染，而失败现场看起来像是
# 重试链多换了一次号（CI 上正是如此）。哪天 fixture 的 lastDailyChat 被拿掉，
# 这里会直接报出来，而不是留成一条偶发红。
assert P.POOL and P.POOL.accounts, "账号池没起来"
assert all(not a.can_daily_chat() for a in P.POOL.accounts),     "fixture 的 lastDailyChat 没生效，调度器巡检会污染上游计数"
gateway = ThreadingHTTPServer(("127.0.0.1", GATEWAY_PORT), P.Handler)
threading.Thread(target=gateway.serve_forever, daemon=True).start()

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (label, detail))


def usage_rows():
    path = os.path.join(USAGE, "usage.jsonl")
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    return rows


def post(body, timeout=30):
    """返回 (status, headers, body_text, elapsed)。HTTPError 也算返回值。"""
    url = "http://127.0.0.1:%d/v1/chat/completions" % GATEWAY_PORT
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer TESTKEY")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8"), \
                time.monotonic() - started
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8"), \
            time.monotonic() - started


print("== 1. 预算花完：504 + Retry-After ==")
MOCK["mode"] = "stall"
MOCK["attempts"] = 0
MOCK["uids"].clear()
status, headers, text, elapsed = post(
    {"model": "deepseek-v4.1-flash", "stream": True,
     "messages": [{"role": "user", "content": "hi"}]}, timeout=30)
print("  [INFO] 实测耗时 %.1fs，上游收到 POST %d 次（不同账号 %d 个）"
      % (elapsed, MOCK["attempts"], len(MOCK["uids"])))
check("状态码是 504", status == 504, "got %s" % status)
check("带 Retry-After", headers.get("Retry-After") == "15",
      "got %r" % headers.get("Retry-After"))
check("耗时被夹在预算附近（6s 预算，<= 12s）", elapsed <= 12.0, "%.1fs" % elapsed)
check("耗时没有退化到旧的 120s 形状", elapsed >= 2.0, "%.1fs" % elapsed)
check("4 个账号只换了 2 个（预算收的手）", len(MOCK["uids"]) == 2,
      "uids=%d attempts=%d" % (len(MOCK["uids"]), MOCK["attempts"]))
try:
    payload = json.loads(text)
    check("错误体是 budget 文案",
          "retry budget" in payload["error"]["message"],
          payload.get("error", {}).get("message", "")[:80])
    check("错误体带 retry_after", payload["error"].get("retry_after") == 15)
except ValueError:
    check("错误体是 JSON", False, text[:80])

print("== 2. 客户端先走：链不再开第二次尝试 ==")
MOCK["mode"] = "stall"
MOCK["attempts"] = 0
MOCK["uids"].clear()
before = len(usage_rows())
sock = socket.create_connection(("127.0.0.1", GATEWAY_PORT), timeout=5)
body = json.dumps({"model": "deepseek-v4.1-flash", "stream": True,
                   "messages": [{"role": "user", "content": "bye"}]}).encode("utf-8")
sock.sendall(
    b"POST /v1/chat/completions HTTP/1.1\r\n"
    b"Host: 127.0.0.1\r\n"
    b"Content-Type: application/json\r\n"
    b"Authorization: Bearer TESTKEY\r\n"
    b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
time.sleep(0.4)
sock.close()
# 第一次尝试的响应头超时是 2s，之后链应当在探测到断连时收手。
time.sleep(3.5)
# 数「换没换号」而不是数 POST：连接池对一条已被对端关掉的 keep-alive 连接会
# 自己重发一次，POST 可能是 2 而链其实只跑了一轮（见 MockUpstream._account_uid）。
check("链没有换号（只用了 1 个账号）", len(MOCK["uids"]) == 1,
      "uids=%d attempts=%d" % (len(MOCK["uids"]), MOCK["attempts"]))
rows = usage_rows()[before:]
aborted = [r for r in rows if r.get("outcome") == "client_aborted"]
check("记了一行 client_aborted", len(aborted) == 1,
      json.dumps(rows[-1] if rows else {}, ensure_ascii=False)[:120])

print("== 3. 429 仍然是 429 ==")
MOCK["mode"] = "429"
MOCK["attempts"] = 0
MOCK["uids"].clear()
status, headers, text, elapsed = post(
    {"model": "deepseek-v4.1-flash", "stream": False,
     "messages": [{"role": "user", "content": "hi"}]}, timeout=30)
check("状态码是 429 而不是 504", status == 429, "got %s" % status)
try:
    payload = json.loads(text)
    check("错误体带 retry_after", isinstance(payload["error"].get("retry_after"), int),
          text[:120])
    check("文案是限流而不是预算", "rate limit" in payload["error"]["message"],
          payload["error"]["message"][:80])
except ValueError:
    check("错误体是 JSON", False, text[:80])

print("\n%d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
