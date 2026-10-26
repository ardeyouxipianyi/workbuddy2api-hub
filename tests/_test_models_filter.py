"""Optional /v1/models filtering by the requesting key's model restriction (#473).

The endpoint has always advertised the upstream catalogue; turning the filter
on makes a restricted key see only what it may use, so clients that
auto-discover models cannot pick one the gateway rejects locally. A key without
a restriction, and a panel session, keep seeing everything. Default is off, so
installs that predate the setting behave exactly as before.
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy as proxy

import wb_models as MC
import wb_settings as S

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


CATALOG = [
    ("deepseek-v4.1-flash", {}),
    ("deepseek-v4-pro", {}),
    ("gpt-6-astra", {}),
]


class FakeRequest(object):
    """Just enough of the handler for /v1/models and settings saving."""

    _get_v1_models = proxy.Handler._get_v1_models
    _handle_settings_save = proxy.Handler._handle_settings_save

    def __init__(self, key_entry=None, payload=None):
        self.key_entry = key_entry
        self.payload = payload
        self.answering = []
        self.headers = {}
        self.path = "/v1/models"

    def _authorized(self):
        return True

    def _request_realm(self):
        return "cn"

    def _json(self, status, payload):
        self.answering.append(("json", status, payload))
        return status, payload

    def _error(self, status, message, kind=""):
        self.answering.append(("error", status, message))
        return status, message

    def _payload_or_error(self, allow_list=False):
        return self.payload

    def _body_consumed(self):
        pass

    def ids(self):
        kind, status, payload = self.answering[-1]
        return [m.get("id") for m in (payload.get("data") or [])]


def models_for(key_entry, enabled):
    d = tempfile.mkdtemp(prefix="wb-models-filter-")
    S.set_filter_models_by_key(d, enabled)
    old = proxy.ACCOUNTS_DIR
    proxy.ACCOUNTS_DIR = d
    try:
        req = FakeRequest(key_entry=key_entry)
        with mock.patch.object(proxy, "fetch_models", return_value=list(CATALOG)), \
             mock.patch.object(proxy, "catalog_meta", return_value=None), \
             mock.patch.object(proxy.wb_modelsdev, "refresh_async", return_value=None):
            proxy.Handler._get_v1_models(req)
        return req.ids()
    finally:
        proxy.ACCOUNTS_DIR = old


print("[1] default: full catalogue, even for a restricted key")

restricted = {"id": "k1", "name": "deepseek only", "models": ["deepseek*"]}
unrestricted = {"id": "k2", "name": "everything", "models": []}

ids = models_for(restricted, False)
check("filter off keeps all three models", len(ids) == 3, ids)
check("filter off still advertises gpt-6-astra", "gpt-6-astra" in ids, ids)

print()
print("[2] filter on: a restricted key only sees what it may use")

ids = models_for(restricted, True)
check("only deepseek models remain", sorted(ids) == ["deepseek-v4-pro", "deepseek-v4.1-flash"], ids)
check("gpt-6-astra is hidden", "gpt-6-astra" not in ids, ids)

print()
print("[3] filter on: unrestricted keys and panel sessions keep everything")

ids = models_for(unrestricted, True)
check("a key with no restriction sees all three", len(ids) == 3, ids)

ids = models_for(None, True)
check("a panel session (no key entry) sees all three", len(ids) == 3, ids)

print()
print("[4] the toggle round-trips through accounts/settings.json")

d = tempfile.mkdtemp(prefix="wb-models-filter-")
check("off by default", S.filter_models_by_key(d) is False)
check("set true returns true", S.set_filter_models_by_key(d, True) is True)
check("read back true", S.filter_models_by_key(d) is True)
check("set false returns false", S.set_filter_models_by_key(d, False) is False)
check("read back false", S.filter_models_by_key(d) is False)

print()
print("[5] /settings/save accepts booleans and rejects strings")

d = tempfile.mkdtemp(prefix="wb-models-filter-")
old = proxy.ACCOUNTS_DIR
proxy.ACCOUNTS_DIR = d
try:
    req = FakeRequest(payload={"filter_models_by_key": True})
    proxy.Handler._handle_settings_save(req)
    check("a boolean is persisted", S.filter_models_by_key(d) is True)

    req = FakeRequest(payload={"filter_models_by_key": "false"})
    proxy.Handler._handle_settings_save(req)
    kind, status, _ = req.answering[-1]
    check("a string is rejected with 400", kind == "error" and status == 400, req.answering[-1])
    check("the stored value is unchanged", S.filter_models_by_key(d) is True)
finally:
    proxy.ACCOUNTS_DIR = old

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
