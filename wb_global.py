# -*- coding: utf-8 -*-
"""wb_global.py — 國際版帳號註冊激活/地區完善 + trial 加油包。

對應 panel 的 upstream/global_register.go 與 trial.go，只適用於國際版
（hub realm == "intl"）帳號：

  1. 新國際版帳號若 chat 回 14017 trial not activated，需先補註冊地區再激活；
     鏈路：POST /billing/area/get-country-code → POST /console/login/account
     （提交地區，冪等）→ GET /auth/realms/copilot/overseas/user/register。
  2. 激活後可領一次性 trial 加油包：POST /billing/ide/trial（冪等碼 14051
     = 已領過，視為正常）。

全部動作都是手動觸發（面板按鈕 / API / scripts/trial.py），不做背景自動註冊。
純標準庫，Python 3.9 兼容。
"""

import json
import urllib.error
import urllib.parse
import urllib.request

import wb_accounts

GLOBAL_WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
# 國際版 web 展示白名單（順序即展示順序，首個通常為 HK）。
INTL_COUNTRY_WHITELIST = ("HK", "MO", "SG", "TH", "PH", "MY", "ID")
TRIAL_ALREADY_MARKERS = ("code=14051", '"code":14051')
TRIAL_PATH = "/billing/ide/trial"


def _base(account):
    return wb_accounts.get_realm_config(account.realm)["billing_upstream"]


def _require_intl(account):
    if str(getattr(account, "realm", "")) != "intl":
        raise RuntimeError("global actions are only available for international accounts")


def _request(account, method, path, body=None, extra_headers=None):
    base = _base(account)
    data = None
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": GLOBAL_WEB_UA,
        "Origin": base,
        "Referer": base + "/",
        "Authorization": "Bearer " + str(account.access_token or ""),
    }
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)
    return urllib.request.Request(base + path, data=data, method=method, headers=headers)


def _envelope(account, req, timeout=20, allow_http_error=False):
    """Send a request and unwrap the {code,msg,data} envelope.

    Returns (code, msg, data, raw_text). With allow_http_error the body of a
    non-2xx response is parsed the same way (the trial endpoint reports the
    idempotent "already claimed" as an HTTP error carrying code 14051).
    """
    try:
        with wb_accounts.urlopen(req, timeout=timeout, proxy=account.proxy) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if not allow_http_error:
            raise
        raw = exc.read(65536).decode("utf-8", "replace")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise RuntimeError("global endpoint returned a non-object")
    return (int(payload.get("code") or 0), str(payload.get("msg") or ""),
            payload.get("data"), raw)


def _unwrap_data(data):
    """get-country-code 的 data 可能是 JSON 字串（雙層信封），需二次解析。"""
    if isinstance(data, str):
        try:
            return json.loads(data)
        except Exception:
            return None
    return data


def fetch_countries(account, intl_only=True, timeout=20):
    """拉取可選註冊地區；intl_only 時按國際版白名單過濾並排序。"""
    _require_intl(account)
    req = _request(account, "POST", "/billing/area/get-country-code",
                   {"filterForbidden": 1})
    code, msg, data, _raw = _envelope(account, req, timeout=timeout)
    if code != 0:
        raise RuntimeError("get-country-code: %s (code=%s)" % (msg, code))
    inner = _unwrap_data(data)
    if isinstance(inner, dict) and isinstance(inner.get("data"), dict):
        inner = inner["data"]
    items = inner.get("list") if isinstance(inner, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("country list parse failed")
    countries = []
    for item in items:
        if not isinstance(item, dict):
            continue
        countries.append({
            "en_name": str(item.get("EnName") or ""),
            "name": str(item.get("Name") or ""),
            "ios2": str(item.get("IOS2") or ""),
            "ios3": str(item.get("IOS3") or ""),
            "code": str(item.get("Code") or ""),
        })
    if not intl_only:
        return countries
    by_code = {c["ios2"]: c for c in countries}
    return [by_code[c] for c in INTL_COUNTRY_WHITELIST if c in by_code]


def register_status(account, timeout=20):
    """回傳 (activated, needs_region, msg)。"""
    _require_intl(account)
    req = _request(
        account, "GET",
        "/auth/realms/copilot/overseas/user/register?userId="
        + urllib.parse.quote(str(account.uid or "")),
        extra_headers={"X-User-Id": str(account.uid or "")})
    code, msg, _data, _raw = _envelope(account, req, timeout=timeout)
    if code == 200:
        return True, False, "register success"
    if code == 500 or "region required" in msg.lower():
        return False, True, msg
    return False, False, msg


def submit_region(account, country, timeout=20):
    """提交註冊地區（冪等）。country 來自 fetch_countries()。"""
    _require_intl(account)
    attrs = {
        "countryCode": [str(country.get("code") or "")],
        "countryFullName": [str(country.get("en_name") or "")],
        "countryName": [str(country.get("ios2") or "")],
    }
    req = _request(account, "POST", "/console/login/account",
                   {"attributes": attrs})
    code, msg, _data, _raw = _envelope(account, req, timeout=timeout)
    if code != 0:
        raise RuntimeError("submit region: %s (code=%s)" % (msg, code))
    return True


def complete_registration(account, timeout=20):
    """一鍵註冊激活：查狀態 → 需補地區則取白名單首個提交 → 重新驗證。冪等。"""
    _require_intl(account)
    activated, needs_region, msg = register_status(account, timeout=timeout)
    if activated:
        return True, "already activated"
    if not needs_region:
        raise RuntimeError("register not activated: %s" % msg)
    countries = fetch_countries(account, intl_only=True, timeout=timeout)
    if not countries:
        raise RuntimeError("no countries available")
    submit_region(account, countries[0], timeout=timeout)
    activated, _needs_region, msg = register_status(account, timeout=timeout)
    if not activated:
        raise RuntimeError("register still not activated after region submit: %s" % msg)
    return True, "activated with region %s" % countries[0]["ios2"]


def claim_trial(account, timeout=20):
    """領取一次性 trial 加油包；回傳 True=新領，False=已領過（冪等成功）。"""
    _require_intl(account)
    req = _request(account, "POST", TRIAL_PATH, None)
    try:
        code, msg, _data, raw = _envelope(account, req, timeout=timeout,
                                          allow_http_error=True)
    except urllib.error.HTTPError:
        raise
    if code == 0:
        return True
    if code == 14051 or any(marker in raw for marker in TRIAL_ALREADY_MARKERS):
        return False
    raise RuntimeError("trial claim failed: %s (code=%s)" % (msg, code))
