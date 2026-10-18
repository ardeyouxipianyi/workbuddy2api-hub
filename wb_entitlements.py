# -*- coding: utf-8 -*-
"""wb_entitlements.py — 账号实际能跑的模型（按 realm 记录被上游拒绝的模型）。

`/v1/models` 的目录来自桌面端目录 + 内置表，回答的是「这个产品有哪些模型」，
不是「这个账号池能跑哪些模型」。免费账号（personal_free_user）请求 GPT / Grok /
Gemini 系模型时上游会回 400 `code: 11158`，目录里却照列，客户端选中才报错。

这里把上游的拒绝落成一条持久事实（accounts/model_entitlements.json），
/v1/models 直接不列它；任何一次成功请求又把这条事实清掉——账号升级成付费后
无需重启、无需重新探测，下一次成功调用就自动恢复。

判定只认上游自己的错误码，不按模型名前缀猜。隐藏也只发生在目录层面：直接按
id 调用仍然会发到上游，成功即解除，所以「换账号后误隐藏」有自愈通道。

纯标准库，Python 3.9 兼容。
"""

import json
import os
import re
import threading
import time

FILENAME = "model_entitlements.json"
CACHE_TTL = 30

_lock = threading.Lock()
_cache = {"at": 0.0, "dir": None, "data": {}}

#: 上游拒绝「这个账号类型不能跑这个模型」时的错误码。
DENIAL_CODE = 11158

#: 错误体里同时带着人类可读的原因，两种形状都认（JSON 体 / 已转义的字符串）。
_DENIAL_RE = re.compile(
    r'"?code"?\s*:\s*%d\b|not allowed for user type' % DENIAL_CODE,
    re.IGNORECASE)
_USER_TYPE_RE = re.compile(r'user type\s*\[([^\]]+)\]', re.IGNORECASE)


def is_denial(text):
    """这条上游错误是不是「账号类型不支持该模型」。"""
    return bool(text) and bool(_DENIAL_RE.search(str(text)))


def user_type(text):
    """从拒绝信息里取账号类型（取不到返回空串）。"""
    match = _USER_TYPE_RE.search(str(text or ""))
    return match.group(1).strip() if match else ""


def store_path(directory):
    return os.path.join(str(directory or "."), FILENAME)


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return {}
    realms = payload.get("realms") if isinstance(payload, dict) else None
    return realms if isinstance(realms, dict) else {}


def load(directory, force=False):
    """缓存读取整张表：{"intl": {model: {"at": ts, ...}}, "cn": {...}}。"""
    now = time.time()
    with _lock:
        if (not force and _cache["dir"] == directory
                and now - _cache["at"] < CACHE_TTL):
            return json.loads(json.dumps(_cache["data"]))
    data = _read(store_path(directory))
    with _lock:
        _cache.update({"at": now, "dir": directory, "data": data})
    return json.loads(json.dumps(data))


def _write(directory, realms):
    path = store_path(directory)
    tmp = path + ".tmp"
    payload = {"version": 1, "realms": realms}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    with _lock:
        _cache.update({"at": time.time(), "dir": directory, "data": realms})


def note_denied(directory, realm, model, detail="", log=None):
    """记下「这个 realm 跑不了这个模型」。重复调用只刷新时间戳。"""
    realm = realm or "intl"
    model = str(model or "").strip()
    if not model:
        return False
    realms = _read(store_path(directory))
    bucket = realms.setdefault(realm, {})
    entry = {"at": time.time(), "user_type": user_type(detail)}
    if detail:
        entry["detail"] = str(detail)[:200]
    if bucket.get(model, {}).get("user_type") == entry["user_type"]:
        bucket[model]["at"] = entry["at"]
    else:
        bucket[model] = entry
    _write(directory, realms)
    if log:
        log("entitlement: %s/%s hidden (%s)"
            % (realm, model, entry["user_type"] or "upstream denial"))
    return True


def note_allowed(directory, realm, model, log=None):
    """一次成功调用就解除隐藏（账号升级 / 换账号后自动恢复）。"""
    realm = realm or "intl"
    model = str(model or "").strip()
    if not model:
        return False
    realms = _read(store_path(directory))
    bucket = realms.get(realm) or {}
    if model not in bucket:
        return False
    bucket.pop(model, None)
    if not bucket:
        realms.pop(realm, None)
    _write(directory, realms)
    if log:
        log("entitlement: %s/%s restored after a successful call" % (realm, model))
    return True


def blocked(directory, realm):
    """该 realm 下被上游拒绝过的模型集合。"""
    realms = load(directory)
    if realm == "all":
        merged = {}
        for bucket in realms.values():
            if isinstance(bucket, dict):
                merged.update(bucket)
        return merged
    bucket = realms.get(realm or "intl")
    return bucket if isinstance(bucket, dict) else {}


def filter_entries(directory, realm, entries):
    """从 (model, meta) 列表里去掉该 realm 跑不了的模型。"""
    hidden = blocked(directory, realm)
    if not hidden:
        return list(entries)
    return [(mid, meta) for mid, meta in entries if mid not in hidden]
