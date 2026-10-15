"""An account's outbound identity must survive a restart (issue #76).

The panel's WB / VSC / CLI selector - and, when enabled, the 429 identity
switch - changes account.product, and every save path writes that value into
the credential file. Loading used to ignore it: __init__ hard-wired the
default and parked the stored value in a `saved_product` field that nothing in
the repository ever read, so a switch looked saved on disk and silently
reverted on every restart.

Section [5] also pins the conversation header family every identity must carry
(upstream answers 400 / code 11-128, `conversation id missing`, without it).

No network: accounts are built from dicts and written to a temp directory.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-product-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_accounts as A
import wb_identity

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


def credential(product="__missing__", **extra):
    """One credential file as it sits on disk, before Account sees it."""
    data = {"uid": "u-product-1", "domain": "www.workbuddy.ai", "realm": "intl",
            "accessToken": ""}
    if product != "__missing__":
        data["product"] = product
    data.update(extra)
    return data


print("[1] the identity stored in the credential file is what loads")
for want in ("workbuddy", "vscode", "cli"):
    acc = A.Account(credential(want))
    check("a file saying %s loads as %s" % (want, want), acc.product == want, acc.product)
check("the aliases the panel may have written are accepted",
      A.Account(credential("vsc")).product == "vscode"
      and A.Account(credential("codebuddy")).product == "cli")

print()
print("[2] missing or unusable values keep the pre-upgrade default")
check("no product field -> workbuddy",
      A.Account(credential("__missing__")).product == "workbuddy")
check("an empty value -> workbuddy", A.Account(credential("")).product == "workbuddy")
check("None -> workbuddy", A.Account(credential(None)).product == "workbuddy")
check("an unknown value -> workbuddy", A.Account(credential("chatgpt")).product == "workbuddy")

print()
print("[3] a switched identity round-trips through the credential file")
acc = A.Account(credential("workbuddy"))
check("set_product reports a real change as True", acc.set_product("cli") is True)
check("set_product reports a no-op as False", acc.set_product("cli") is False)
acc.save(_TMP)
path = os.path.join(_TMP, "u-product-1.json")
on_disk = json.load(open(path, encoding="utf-8"))
check("the file carries the switched identity", on_disk.get("product") == "cli",
      on_disk.get("product"))
reloaded = A.Account(json.load(open(path, encoding="utf-8")))
check("a reload keeps the switched identity", reloaded.product == "cli", reloaded.product)

print()
print("[4] the loaded identity reaches everything that depends on it")
cn = A.Account(credential("vscode", realm="cn", domain="www.workbuddy.cn"))
check("public() reports it, so the panel highlights the right button",
      cn.public().get("product") == "vscode", cn.public().get("product"))
check("to_dict() writes it, so the next save keeps it",
      cn.to_dict().get("product") == "vscode", cn.to_dict().get("product"))
check("the chat endpoint follows it",
      cn.chat_base_url() == wb_identity.endpoint_for("cn", "vscode")[0], cn.chat_base_url())
check("and that is not the desktop endpoint",
      cn.chat_base_url() != wb_identity.endpoint_for("cn", "workbuddy")[0])
desktop = A.Account(credential("workbuddy", realm="cn", domain="www.workbuddy.cn"))
check("the outbound headers follow it",
      cn.headers().get("X-IDE-Type") == "VSCode"
      and desktop.headers().get("X-IDE-Type") == "WorkBuddy",
      (cn.headers().get("X-IDE-Type"), desktop.headers().get("X-IDE-Type")))

print()
print("[5] the outbound identity carries the conversation header family")
# 上游的渠道校验在请求完全没有会话 id 时回 400 / code 11-128
# （`conversation id missing`，displayTips「这次调用没通过渠道校验」）。三套
# 身份都得带这套头：VSCode / CLI 一直带，桌面端（所有账号默认的那套）原来
# 一个都没有。X-Conversation-ID 由会话键稳定派生，同一会话内不变。
CONV_HEADERS = ("X-Conversation-ID", "X-Conversation-Request-ID",
                "X-Conversation-Message-ID")
for want in ("workbuddy", "vscode", "cli"):
    h = wb_identity.build_identity_headers(want, "intl", "u1", "tok",
                                           conversation_id="conv-seed")
    check("%s sends the conversation header family" % want,
          all(h.get(k) for k in CONV_HEADERS),
          [k for k in CONV_HEADERS if not h.get(k)])
    check("%s: the message id equals the request id" % want,
          h.get("X-Conversation-Message-ID") == h.get("X-Request-ID"))
check("the conversation id is stable for one seed",
      wb_identity.conversation_uuid("s") == wb_identity.conversation_uuid("s"))
check("and differs across seeds",
      wb_identity.conversation_uuid("s") != wb_identity.conversation_uuid("t"))
no_seed = wb_identity.conversation_uuid(None)
check("no seed still yields an uppercase UUID",
      no_seed == no_seed.upper() and len(no_seed) == 36, no_seed)
conv_acc = A.Account(credential("workbuddy"))
h1 = conv_acc.headers(purpose="chat", conversation_id="pfx-same")
h2 = conv_acc.headers(purpose="chat", conversation_id="pfx-same")
h3 = conv_acc.headers(purpose="chat", conversation_id="pfx-other")
check("Account.headers threads the seed into X-Conversation-ID",
      h1.get("X-Conversation-ID") == wb_identity.conversation_uuid("pfx-same"),
      h1.get("X-Conversation-ID"))
check("same seed -> same header across calls",
      h1.get("X-Conversation-ID") == h2.get("X-Conversation-ID"))
check("different seed -> different header",
      h1.get("X-Conversation-ID") != h3.get("X-Conversation-ID"))
check("the billing headers are untouched",
      "X-Conversation-ID" not in conv_acc.headers(purpose="billing"))

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
