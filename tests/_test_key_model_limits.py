"""A key may be limited to part of the catalogue, and the limit is enforced.

The panel binds each API key to an exit; this adds a second, optional binding:
the models that key is allowed to ask for. A request that names a model outside
the list is answered with a readable 400 locally, so nothing reaches upstream
and no credits are spent - which is the whole point, since clients fire
background requests straight at the catalogue from outside the model picker.

These cases pin the storage shape, the matching rules and the enforcement
point, plus the upgrade path: every key written before this field existed has
to read back as unrestricted. No network access required.

A limited key that names no model is refused rather than waved through: the
request cannot be shown to be asking for something the key may use, and
forwarding it would only reach the upstream with an empty model.
"""

import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy as proxy
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


class FakeRequest(object):
    """Just enough of the handler for the pieces under test."""

    _key_model_error = proxy.Handler._key_model_error
    _handle_settings_save = proxy.Handler._handle_settings_save
    _validate_settings_save = proxy.Handler._validate_settings_save

    def __init__(self, key_entry=None, payload=None):
        self.key_entry = key_entry
        self.payload = payload
        self.answering = []

    def _payload_or_error(self, allow_list=False):
        return self.payload

    def _error(self, status, message, kind=""):
        self.answering.append(("error", status, message))
        return status, message

    def _json(self, status, payload):
        self.answering.append(("json", status, payload))
        return status, payload


print("[1] the allow-list round-trips through accounts/settings.json")

d = tempfile.mkdtemp(prefix="wb-keymodels-")
S.set_api_keys(d, [
    {"id": "k1", "name": "DeepSeek only", "key": "key-alpha", "realm": "cn",
     "models": ["deepseek/*"]},
    {"id": "k2", "name": "everything", "key": "key-beta", "realm": ""},
])
entries = S.api_keys(d)
check("both keys survive", len(entries) == 2, entries)
check("the limited key keeps its patterns", entries[0]["models"] == ["deepseek/*"], entries[0])
check("a key without the field is unrestricted", entries[1]["models"] == [], entries[1])

print()
print("[2] a hand-edited settings.json may hold a comma separated string")

d2 = tempfile.mkdtemp(prefix="wb-keymodels-")
S.save(d2, {"api_keys": [
    {"id": "k1", "name": "mixed", "key": "key-gamma",
     "models": " gpt-6-astra , deepseek/* ,, GPT-6-ASTRA "},
]})
entry = S.api_keys(d2)[0]
check("split, trimmed and lowercased",
      entry["models"] == ["gpt-6-astra", "deepseek/*"], entry)
check("duplicates collapse", len(entry["models"]) == 2, entry)

print()
print("[3] matching rules")

limited = {"models": ["deepseek/*", "gpt-6-astra"]}
unlimited = {"models": []}
check("an empty list allows anything", S.key_allows_model(unlimited, "gpt-5.6-terra"))
check("a missing list allows anything", S.key_allows_model({}, "gpt-5.6-terra"))
check("exact name matches", S.key_allows_model(limited, "gpt-6-astra"))
check("matching ignores case", S.key_allows_model(limited, "GPT-6-ASTRA"))
check("wildcard matches the family", S.key_allows_model(limited, "deepseek/deepseek-v4.1-flash"))
check("a model outside the list is refused", not S.key_allows_model(limited, "gpt-5.6-terra"))
check("the wildcard does not leak across families",
      not S.key_allows_model(limited, "gemini-3.5-flash"))
check("a trailing wildcard covers effort suffixes",
      S.key_allows_model({"models": ["gpt-6-astra*"]}, "gpt-6-astra-high"))
check("an exact name does not cover its suffixes",
      not S.key_allows_model({"models": ["gpt-6-astra"]}, "gpt-6-astra-high"))
check("a launcher entry stays unrestricted",
      S.key_allows_model({"models": [], "source": "launcher"}, "gpt-5.6-terra"))
check("a limited key that names no model is refused",
      not S.key_allows_model(limited, ""))
check("a limited key that omits the model is refused too",
      not S.key_allows_model(limited, None))
check("an unrestricted key is unaffected by a missing model",
      S.key_allows_model(unlimited, None))

print()
print("[4] the handler refuses the request instead of forwarding it")

unrestricted = FakeRequest(key_entry={"name": "open", "models": []})
restricted = FakeRequest(key_entry={"name": "DeepSeek only", "models": ["deepseek/*"]})
anonymous = FakeRequest(key_entry=None)

check("an unrestricted key passes", unrestricted._key_model_error("gpt-5.6-terra") == "")
check("an allowed model passes",
      restricted._key_model_error("deepseek/deepseek-v4.1-flash") == "")
blocked = restricted._key_model_error("gpt-5.6-terra")
check("a refused model returns an explanation", bool(blocked), blocked)
check("the explanation names the key", "DeepSeek only" in blocked, blocked)
check("the explanation names the model", "gpt-5.6-terra" in blocked, blocked)
check("the explanation lists what is allowed", "deepseek/*" in blocked, blocked)
check("no key entry means no restriction", anonymous._key_model_error("gpt-5.6-terra") == "")
modelless = restricted._key_model_error("")
check("a limited key sending no model is refused", bool(modelless), modelless)
check("the explanation says no model was named", "未指定模型" in modelless, modelless)
check("an unrestricted key sending no model still passes",
      unrestricted._key_model_error("") == "")

print()
print("[5] /settings/save keeps a stored limit when the field is absent")

d3 = tempfile.mkdtemp(prefix="wb-keymodels-")
S.set_api_keys(d3, [{"id": "k1", "name": "limited", "key": "key-delta",
                     "realm": "cn", "models": ["deepseek/*"]}])
with mock.patch.multiple(proxy, ACCOUNTS_DIR=d3, POOL=None, SCHEDULER=None):
    # An older cached panel posts rows without the new field at all.
    request = FakeRequest(payload={"api_keys": [
        {"id": "k1", "name": "limited", "key": "", "realm": "cn", "enabled": True},
    ]})
    proxy.Handler._handle_settings_save(request)
    kept = S.api_keys(d3)[0]
    check("the stored patterns survive a save that omits them",
          kept["models"] == ["deepseek/*"], kept)
    check("the key itself is still the stored one", kept["key"] == "key-delta", kept)

    request = FakeRequest(payload={"api_keys": [
        {"id": "k1", "name": "limited", "key": "", "realm": "cn", "enabled": True,
         "models": ["gpt-6-astra", "gpt-5.6-sol"]},
    ]})
    proxy.Handler._handle_settings_save(request)
    updated = S.api_keys(d3)[0]
    check("an explicit list replaces the stored one",
          updated["models"] == ["gpt-6-astra", "gpt-5.6-sol"], updated)

    request = FakeRequest(payload={"api_keys": [
        {"id": "k1", "name": "limited", "key": "", "realm": "cn", "enabled": True,
         "models": []},
    ]})
    proxy.Handler._handle_settings_save(request)
    cleared = S.api_keys(d3)[0]
    check("an explicit empty list removes the limit", cleared["models"] == [], cleared)

print()
print("[6] an install that predates the field stays unrestricted")

d4 = tempfile.mkdtemp(prefix="wb-keymodels-")
S.save(d4, {"api_key_set": True, "api_key": "key-legacy"})
legacy = S.api_keys(d4)
check("the legacy key reads back", len(legacy) == 1, legacy)
check("with no restriction", legacy[0]["models"] == [], legacy)
check("so it still reaches every model",
      S.key_allows_model(legacy[0], "gpt-5.6-terra"))

S.set_api_keys(d4, [{"id": "k1", "name": "legacy row", "key": "key-legacy"}])
check("a row saved without the field is unrestricted too",
      S.api_keys(d4)[0]["models"] == [], S.api_keys(d4))

print()
print("[7] /settings/save validates the whole payload before writing anything")

d5 = tempfile.mkdtemp(prefix="wb-keymodels-")
S.set_reserve_credits(d5, 7)
with mock.patch.multiple(proxy, ACCOUNTS_DIR=d5, POOL=None, SCHEDULER=None):
    request = FakeRequest(payload={"reserve_credits": 123,
                                   "pool": {"soft_rate": -1}})
    status, _message = proxy.Handler._handle_settings_save(request)
    check("the invalid later block is rejected", status == 400, request.answering)
    check("the earlier block was not written", S.reserve_credits(d5) == 7,
          S.reserve_credits(d5))
    request = FakeRequest(payload={"reserve_credits": 123,
                                   "pool": {"soft_rate": 30}})
    status, _payload = proxy.Handler._handle_settings_save(request)
    check("a fully valid payload still saves", status == 200, request.answering)
    check("the reserve was applied", S.reserve_credits(d5) == 123,
          S.reserve_credits(d5))


print("[7] runtime_settings_view exposes models and round-trips when adding a new key (issue #92)")

d5 = tempfile.mkdtemp(prefix="wb-keymodels-view-")
S.set_api_keys(d5, [{"id": "k1", "name": "first", "key": "key-first-secret",
                     "realm": "intl", "models": ["deepseek*"]}])
with mock.patch.multiple(proxy, ACCOUNTS_DIR=d5, POOL=None, SCHEDULER=None):
    view = proxy.runtime_settings_view()
    keys_in_view = view.get("api_keys") or []
    check("runtime_settings_view returns the key list", len(keys_in_view) == 1, keys_in_view)
    check("runtime_settings_view includes models", keys_in_view[0].get("models") == ["deepseek*"], keys_in_view[0])

    # Simulates panel round-trip: the panel reads data.api_keys from /settings,
    # maps them to editable rows, adds a new row, and calls saveApiKeys().
    mapped_rows = []
    for k in keys_in_view:
        mapped_rows.append({
            "id": k.get("id") or "",
            "name": k.get("name") or "",
            "realm": k.get("realm") or "",
            "models": list(k.get("models") or []),
            "enabled": k.get("enabled") is not False,
            "key": "",  # Blank means keep stored secret
        })
    # Append a newly created key
    mapped_rows.append({
        "id": "",
        "name": "second",
        "realm": "cn",
        "models": ["gpt*"],
        "enabled": True,
        "key": "key-second-secret",
    })
    req_add = FakeRequest(payload={"api_keys": mapped_rows})
    proxy.Handler._handle_settings_save(req_add)
    saved = S.api_keys(d5)
    by_name = {entry["name"]: entry for entry in saved}
    check("both keys exist after save", len(saved) == 2, saved)
    check("the existing key preserves its model restriction",
          by_name.get("first", {}).get("models") == ["deepseek*"],
          by_name.get("first"))
    check("the newly added key gets its own restriction",
          by_name.get("second", {}).get("models") == ["gpt*"],
          by_name.get("second"))

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
