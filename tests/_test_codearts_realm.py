"""CodeArts 出口：持久化、Key 绑定、停用收敛与跨区放行。"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-codearts-realm-")
ACCOUNTS = os.environ["ACCOUNTS_DIR"]

import wb_codearts  # noqa: E402
import wb_proxy  # noqa: E402
import wb_settings  # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


print("== settings ==")
check("REALMS includes codearts", "codearts" in wb_settings.REALMS)
check("limit realms unchanged", wb_settings.LIMIT_REALMS == ("intl", "cn"))
check("not configured by default", wb_settings.codearts_configured(ACCOUNTS) is False)

entry = wb_settings._clean_key_entry({"key": "abcd1234", "realm": "codearts"})
check("key keeps codearts", entry["realm"] == "codearts", entry)
entry = wb_settings._clean_key_entry({"key": "abcd1234", "realm": "nope"})
check("unknown realm clears", entry["realm"] == "", entry)

print("== options ==")
check("unconfigured options", wb_proxy.realm_options() == ["intl", "cn"])
wb_codearts.save_config(ACCOUNTS, {"enabled": True})
check("configured options", wb_proxy.realm_options() ==
      ["intl", "cn", "codearts"])

print("== persistence ==")
wb_proxy.CURRENT_REALM = "intl"
wb_proxy.save_persisted_realm("codearts")
check("saved codearts", wb_proxy.CURRENT_REALM == "codearts")
check("persisted file", json.load(open(wb_proxy.realm_state_file()))["realm"] ==
      "codearts")
wb_proxy.CURRENT_REALM = "intl"
check("loaded codearts", wb_proxy.load_persisted_realm() == "codearts")

print("== disable reconciles ==")
wb_codearts.save_config(ACCOUNTS, {"enabled": False})
check("reconcile returns True", wb_proxy.reconcile_codearts_realm() is True)
check("current reset to intl", wb_proxy.CURRENT_REALM == "intl")
check("persisted reset", json.load(open(wb_proxy.realm_state_file()))["realm"] ==
      "intl")
check("options shrink", wb_proxy.realm_options() == ["intl", "cn"])

print("== unconfigured persisted realm falls back ==")
with open(wb_proxy.realm_state_file(), "w", encoding="utf-8") as fh:
    json.dump({"realm": "codearts"}, fh)
wb_proxy.CURRENT_REALM = "intl"
check("fallback on load", wb_proxy.load_persisted_realm() == "intl")
wb_proxy.CURRENT_REALM = "intl"
wb_proxy.save_persisted_realm("codearts")
check("cannot save unconfigured codearts", wb_proxy.CURRENT_REALM == "intl")

print("== cross realm ==")
wb_codearts.save_config(ACCOUNTS, {"enabled": True})
h = wb_proxy.Handler.__new__(wb_proxy.Handler)
h.key_entry = {"name": "测试 Key"}
check("codearts bypasses exclusive realm",
      h._cross_realm_error("deepseek-v4-pro-0813", "codearts") == "")
check("workbuddy still rejects mismatch",
      h._cross_realm_error("deepseek-v4-pro-0813", "intl") != "")

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
