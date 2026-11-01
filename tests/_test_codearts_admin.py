"""`/codearts/*` 管理端点：本地账号池的增删启停、OAuth 与设置。"""
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-codearts-admin-")
ACCOUNTS = os.environ["ACCOUNTS_DIR"]

import wb_codearts as C  # noqa: E402
import wb_codearts_crypto as crypto  # noqa: E402
import wb_proxy  # noqa: E402
from _fake_codearts import start_server, stop_server  # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


def call(method, path, payload=None, token=None, key=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Panel-Token"] = token
    if key:
        headers["Authorization"] = "Bearer " + key
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(HUB + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"raw": raw}


srv, base, state = start_server()
C.SNAP_HOST = base
C.SNAP_MANAGER = base + "/snap-manager"
C.STS_HOST = base
C.BENEFIT_HOST = base
C.PORTAL_HOST = base + "/portal"

hub = wb_proxy.GatewayServer(("127.0.0.1", 0), wb_proxy.Handler)
threading.Thread(target=hub.serve_forever, daemon=True).start()
HUB = "http://127.0.0.1:%d" % hub.server_address[1]
HUB_PORT = hub.server_address[1]

try:
    wb_proxy.POOL = None
    wb_proxy.API_KEY = None
    C.save_config(ACCOUNTS, {"enabled": True})
    auth = {
        "user_id": "u1", "user_name": "账号一", "domain_id": "d1",
        "cloud_dragon_token": state.token,
        "access_key_id": state.api_key, "secret_access_key": state.secret_key,
        "expiration": state.expiration, "refresh_token": state.refresh_token,
        "code_verifier": "v", "client_id": C.CLIENT_ID,
        "dpop_private_key": crypto.jwk_from_keypair(*crypto.generate_keypair()),
    }
    C.pool_for(ACCOUNTS).add(C.save_auth(ACCOUNTS, auth))

    status, body = call("POST", "/panel/login", {"password": "admin"})
    check("panel login", status == 200 and body.get("token"), (status, body))
    token = body.get("token")

    print("== auth ==")
    status, body = call("GET", "/codearts/overview")
    check("overview without panel -> 401", status == 401, (status, body))
    status, body = call("POST", "/codearts/accounts/delete", {"uid": "u1"},
                        key="any-key")
    check("delete with api key only -> 401", status == 401, (status, body))
    status, body = call("GET", "/codearts/overview", token=token)
    check("overview with panel", status == 200, (status, body))
    check("overview accounts", len(body.get("accounts") or []) == 1, body)
    check("overview online", body.get("online") is True, body)
    check("overview no token leak", state.token not in json.dumps(body), body)

    print("== account actions ==")
    for action in ("disable", "enable", "clear-cooldown", "checkin"):
        status, body = call("POST", "/codearts/accounts/" + action,
                            {"uid": "u1"}, token=token)
        check("%s ok" % action, status == 200 and body.get("ok") is True,
              (status, body))
    check("account still in pool", C.pool_for(ACCOUNTS).get("u1") is not None)

    print("== proxy slots ==")
    import wb_settings
    wb_settings.set_proxy_slots(ACCOUNTS, [
        {"id": "slot-1", "name": "one", "url": "http://127.0.0.1:7890",
         "enabled": True},
    ])
    status, body = call("POST", "/codearts/accounts/proxy",
                        {"uid": "u1", "slot_id": "slot-1"}, token=token)
    check("proxy bind ok", status == 200 and body.get("proxy_slot") == "slot-1",
          (status, body))
    check("proxy resolved", body.get("proxy") == "http://127.0.0.1:7890", body)
    check("proxy persisted",
          C.load_auths(ACCOUNTS)[0].get("proxySlot") == "slot-1",
          C.load_auths(ACCOUNTS)[0])
    status, body = call("POST", "/codearts/accounts/proxy",
                        {"uid": "u1", "slot_id": ""}, token=token)
    check("proxy direct ok", status == 200 and body.get("proxy_slot") == "",
          (status, body))
    check("proxy direct resolved", body.get("proxy") == "", body)
    status, body = call("POST", "/codearts/accounts/proxy",
                        {"uid": "nope", "slot_id": "slot-1"}, token=token)
    check("proxy unknown -> 404", status == 404, (status, body))
    status, body = call("POST", "/codearts/accounts/proxy",
                        {"slot_id": "slot-1"}, token=token)
    check("proxy missing uid -> 400", status == 400, (status, body))
    # Removing a slot must clear the CodeArts binding, like WorkBuddy.
    status, body = call("POST", "/codearts/accounts/proxy",
                        {"uid": "u1", "slot_id": "slot-1"}, token=token)
    status, body = call("POST", "/proxy/slots/save", {"slots": []}, token=token)
    check("proxy slots save", status == 200, (status, body))
    check("codearts binding cleared",
          not C.load_auths(ACCOUNTS)[0].get("proxySlot"),
          C.load_auths(ACCOUNTS)[0])
    check("codearts direct after removal",
          C.pool_for(ACCOUNTS).get("u1").proxy == "",
          C.pool_for(ACCOUNTS).get("u1").proxy)

    status, body = call("POST", "/codearts/accounts/enable", {}, token=token)
    check("missing uid -> 400", status == 400, (status, body))

    status, body = call("POST", "/codearts/keepalive", {}, token=token)
    check("keepalive", status == 200, (status, body))

    status, body = call("POST", "/codearts/models/refresh", {}, token=token)
    check("models refresh", status == 200 and body.get("models", 0) >= 3,
          (status, body))

    status, body = call("POST", "/codearts/test", {}, token=token)
    check("test connection", status == 200 and body.get("ok") is True,
          (status, body))

    print("== oauth ==")
    status, body = call("POST", "/codearts/accounts/oauth/start", {}, token=token)
    check("oauth start", status == 200 and body.get("session_id"), (status, body))
    session_id = body["session_id"]
    check("oauth url", "authorize" in body.get("auth_url", ""), body)
    status, body = call("POST", "/codearts/accounts/oauth/poll",
                        {"session_id": session_id}, token=token)
    check("oauth poll pending", status == 200 and body.get("status") == "pending",
          (status, body))
    status, body = call("POST", "/codearts/accounts/oauth/callback", {
        "session_id": session_id,
        "callback_url": "http://127.0.0.1:%d/oauth/callback?code=abc" % HUB_PORT,
    }, token=token)
    check("oauth callback done", status == 200 and body.get("status") == "done",
          (status, body))

    print("== browser callback ==")
    started = call("POST", "/codearts/accounts/oauth/start", {}, token=token)[1]
    try:
        urllib.request.urlopen(HUB + "/oauth/callback?code=abc", timeout=20)
        check("callback without session identifier rejected", False)
    except urllib.error.HTTPError as exc:
        check("callback without session identifier rejected", exc.code == 400, exc.code)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(started["auth_url"]).query)
    ticket = (query.get("ticket_id") or [""])[0]
    req = urllib.request.Request(
        HUB + "/oauth/callback?code=abc&ticket_id=" + urllib.parse.quote(ticket))
    with urllib.request.urlopen(req, timeout=20) as resp:
        html = resp.read().decode("utf-8", "replace")
    check("browser callback 200", resp.status == 200, resp.status)
    check("browser callback html", "登录成功" in html, html[:200])
    check("browser callback saved account",
          C.pool_for(ACCOUNTS).get("u1") is not None)

    print("== delete ==")
    status, body = call("POST", "/codearts/accounts/delete", {"uid": "nope"},
                        token=token)
    check("delete missing -> 404", status == 404, (status, body))
    status, body = call("POST", "/codearts/accounts/delete", {"uid": "u1"},
                        token=token)
    check("delete ok", status == 200 and body.get("deleted") is True,
          (status, body))
    check("account gone", C.pool_for(ACCOUNTS).get("u1") is None)
    check("auth file gone", C.load_auths(ACCOUNTS) == [])

    print("== settings ==")
    status, body = call("GET", "/settings", token=token)
    check("settings has codearts", isinstance(body.get("codearts"), dict), body)
    check("settings no secret", state.token not in json.dumps(body), body)
    check("settings codearts has no key", "api_key" not in body["codearts"], body)
    status, body = call("POST", "/settings/save", {
        "codearts": {"enabled": True, "max_concurrent": 2},
    }, token=token)
    check("settings save", status == 200, (status, body))
    check("settings max concurrent",
          body["codearts"].get("max_concurrent") == 2, body)
    wb_proxy.CURRENT_REALM = "codearts"
    status, body = call("POST", "/settings/save", {
        "codearts": {"enabled": False},
    }, token=token)
    check("disable save", status == 200, (status, body))
    check("disable warning", "warning" in body, body)
    check("disable resets realm", wb_proxy.CURRENT_REALM == "intl",
          wb_proxy.CURRENT_REALM)
finally:
    try:
        hub.shutdown()
        hub.server_close()
    except Exception:
        pass
    stop_server(srv)

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
