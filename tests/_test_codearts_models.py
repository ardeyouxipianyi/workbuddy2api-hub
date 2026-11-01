"""`/v1/models?realm=codearts`：本地账号目录的并集、Key 过滤与种子兜底。"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-codearts-models-")
ACCOUNTS = os.environ["ACCOUNTS_DIR"]

import wb_codearts as C  # noqa: E402
import wb_codearts_crypto as crypto  # noqa: E402
import wb_proxy  # noqa: E402
import wb_settings  # noqa: E402
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


def get(url, key):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
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

try:
    wb_settings.set_api_keys(ACCOUNTS, [{
        "key": "hub-key-1234", "name": "test", "realm": "codearts",
    }])
    wb_proxy.API_KEY = None
    C.save_config(ACCOUNTS, {"enabled": True})
    C.invalidate_models()

    # 没有账号：种子目录
    status, payload = get(HUB + "/v1/models?realm=codearts", "hub-key-1234")
    check("seed status", status == 200, (status, payload))
    check("seed has GLM-5.2", any(m["id"] == "GLM-5.2" for m in payload["data"]),
          payload)
    check("seed benefit flag",
          any(m.get("benefit") for m in payload["data"]
              if m["id"] == "deepseek-v4-pro-0813"), payload)

    # 加一个账号
    auth = {
        "user_id": "u1", "user_name": "账号一", "domain_id": "d1",
        "cloud_dragon_token": state.token,
        "access_key_id": state.api_key, "secret_access_key": state.secret_key,
        "expiration": state.expiration, "refresh_token": state.refresh_token,
        "code_verifier": "v", "client_id": C.CLIENT_ID,
        "dpop_private_key": crypto.jwk_from_keypair(*crypto.generate_keypair()),
    }
    C.pool_for(ACCOUNTS).add(C.save_auth(ACCOUNTS, auth))
    C.invalidate_models()

    status, payload = get(HUB + "/v1/models?realm=codearts", "hub-key-1234")
    check("status 200", status == 200, (status, payload))
    ids = [m["id"] for m in payload["data"]]
    check("ids", set(ids) == {"GLM-5.2", "GLM-4.7", "deepseek-v4-pro-0813"}, ids)
    check("owned_by", payload["data"][0]["owned_by"] == "codearts")
    check("provider", payload["data"][0]["provider"] == "codearts")
    check("catalog source", payload["catalog"]["source"] == "codearts")
    check("benefit flag", any(m.get("benefit") for m in payload["data"]
                              if m["id"] == "deepseek-v4-pro-0813"), payload)

    # Key 模型限制
    wb_settings.set_filter_models_by_key(ACCOUNTS, True)
    wb_settings.set_api_keys(ACCOUNTS, [{
        "key": "hub-key-1234", "name": "test", "realm": "codearts",
        "models": ["glm-*"],
    }])
    C.invalidate_models()
    status, payload = get(HUB + "/v1/models?realm=codearts", "hub-key-1234")
    check("filtered status", status == 200, (status, payload))
    check("filtered ids", [m["id"] for m in payload["data"]] ==
          ["GLM-5.2", "GLM-4.7"], payload)
    wb_settings.set_filter_models_by_key(ACCOUNTS, False)

    # 未启用 -> 503
    C.save_config(ACCOUNTS, {"enabled": False})
    status, payload = get(HUB + "/v1/models?realm=codearts", "hub-key-1234")
    check("disabled 503", status == 503, (status, payload))
finally:
    try:
        hub.shutdown()
        hub.server_close()
    except Exception:
        pass
    stop_server(srv)

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
