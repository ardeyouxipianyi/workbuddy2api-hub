"""An account's outbound identity must survive a restart (issue #76).

The panel's WB / VSC / CLI selector - and, when enabled, the 429 identity
switch - changes account.product, and every save path writes that value into
the credential file. Loading used to ignore it: __init__ hard-wired the
default and parked the stored value in a `saved_product` field that nothing in
the repository ever read, so a switch looked saved on disk and silently
reverted on every restart.

Sections [5]-[7] also pin the outbound identity against the 5.8.4 capture: the
conversation header family every identity must carry (upstream answers 400 /
code 11-128, `conversation id missing`, without it), the desktop version / UA /
agent headers, and the id shapes (dashed UUID vs 32hex). Section [6b] pins the
CLI identity against the official @tencent-ai/codebuddy-code 2.168.2 package:
version / UA, the agent header family, the four-segment b3 trace family, and
the official id relations (stable session id, per-turn request id, per-request
message id).

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
import wb_fingerprint
import wb_identity
import wb_settings

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
# 一个都没有。形态按各自官方客户端的实测/源码：桌面端会话 id 是带连字符的
# UUID、请求/消息 id 是 32 位 hex；VSCode 扩展（4.12.39997873）的
# conversationId / requestId 都是 uuidv4 去连字符的 32 位 hex。
CONV_HEADERS = ("X-Conversation-ID", "X-Conversation-Request-ID",
                "X-Conversation-Message-ID")
HEX32 = ("X-Request-ID", "X-Conversation-Request-ID", "X-Conversation-Message-ID")
for want in ("workbuddy", "vscode", "cli"):
    h = wb_identity.build_identity_headers(want, "intl", "u1", "tok")
    if want in ("cli", "vscode"):
        # 官方 CLI 只在 meta.rootRequestId 存在时发 X-Root-Request-ID（子代理用
        # 主会话的 conversationRequestId）；VSCode 主对话也不发（仅 subagent 带
        # rootRequestId）。两者都不冒充子代理。
        want_conv = tuple(k for k in CONV_HEADERS if k != "X-Root-Request-ID")
        want_hex = tuple(k for k in HEX32 if k != "X-Root-Request-ID")
        check("%s: no X-Root-Request-ID on a main-conversation request" % want,
              "X-Root-Request-ID" not in h)
        check("%s: no X-Parent-Conversation-ID (subagent-only header)" % want,
              "X-Parent-Conversation-ID" not in h)
    else:
        want_conv, want_hex = CONV_HEADERS, HEX32
    check("%s sends the conversation header family" % want,
          all(h.get(k) for k in want_conv),
          [k for k in want_conv if not h.get(k)])
    if want == "vscode":
        # 官方 VSCode 扩展 4.12.39997873 的 model-provider：X-Request-ID 与
        # X-Conversation-Request-ID 是同一个本轮 requestId，Message-ID 是另一个
        # 独立的随机值。
        check("%s: request id equals conversation request id" % want,
              h.get("X-Request-ID") == h.get("X-Conversation-Request-ID"))
        check("%s: message id is independent" % want,
              h.get("X-Conversation-Message-ID") != h.get("X-Request-ID"))
    else:
        # 桌面端按 5.8.4 抓包、CLI 按 2.168.2 契约：Request-ID = Message-ID。
        check("%s: message id equals request id" % want,
              h.get("X-Conversation-Message-ID") == h.get("X-Request-ID"))
        if want != "cli":
            check("%s: root request id equals conversation request id" % want,
                  h.get("X-Root-Request-ID") == h.get("X-Conversation-Request-ID"))
    conv = str(h.get("X-Conversation-ID"))
    if want == "vscode":
        check("%s: the conversation id is 32 hex" % want,
              len(conv) == 32
              and all(c in "0123456789abcdef" for c in conv), conv)
    else:
        check("%s: the conversation id is a dashed uuid" % want,
              len(conv) == 36 and conv.count("-") == 4, conv)
    check("%s: the request/message ids are 32 hex chars" % want,
          all(len(str(h.get(k))) == 32
              and all(c in "0123456789abcdef" for c in str(h.get(k)))
              for k in want_hex),
          [h.get(k) for k in want_hex])
check("desktop: a fresh conversation id per call (upstream keeps no history by it)",
      wb_identity.build_identity_headers("workbuddy", "intl", "u1", "tok")["X-Conversation-ID"]
      != wb_identity.build_identity_headers("workbuddy", "intl", "u1", "tok")["X-Conversation-ID"])
# CLI 按官方语义（getEffectiveConversationRequestId / session.id）：会话 id 在
# 一个会话里恒定、一轮 user send 的 request id 整轮复用；只有 message id 每请求新。
cli_a = wb_identity.build_identity_headers("cli", "intl", "u1", "tok",
                                           session_key="sess-1")
cli_b = wb_identity.build_identity_headers("cli", "intl", "u1", "tok",
                                           session_key="sess-1")
check("cli: the conversation id is stable for one (uid, session_key)",
      cli_a["X-Conversation-ID"] == cli_b["X-Conversation-ID"],
      (cli_a["X-Conversation-ID"], cli_b["X-Conversation-ID"]))
check("cli: another session key gets another conversation id",
      wb_identity.build_identity_headers("cli", "intl", "u1", "tok",
                                         session_key="sess-2")["X-Conversation-ID"]
      != cli_a["X-Conversation-ID"])
check("cli: another account gets another conversation id",
      wb_identity.build_identity_headers("cli", "intl", "u2", "tok",
                                         session_key="sess-1")["X-Conversation-ID"]
      != cli_a["X-Conversation-ID"])
turn = "f" * 32
turn_a = wb_identity.build_identity_headers("cli", "intl", "u1", "tok",
                                            session_key="sess-1",
                                            turn_request_id=turn)
turn_b = wb_identity.build_identity_headers("cli", "intl", "u1", "tok",
                                            session_key="sess-1",
                                            turn_request_id=turn)
check("cli: the caller's turn request id is what goes out",
      turn_a["X-Conversation-Request-ID"] == turn,
      turn_a["X-Conversation-Request-ID"])
check("cli: retries of one turn reuse the turn request id",
      turn_a["X-Conversation-Request-ID"] == turn_b["X-Conversation-Request-ID"])
check("cli: the message id is fresh per request",
      turn_a["X-Request-ID"] != turn_b["X-Request-ID"]
      and turn_a["X-Request-ID"] == turn_a["X-Conversation-Message-ID"],
      (turn_a["X-Request-ID"], turn_b["X-Request-ID"]))
check("desktop keeps generating its own conversation request id per request",
      wb_identity.build_identity_headers("workbuddy", "intl", "u1", "tok",
                                         turn_request_id=turn)["X-Conversation-Request-ID"]
      != turn)
a7, b7 = wb_identity.uuid7(), wb_identity.uuid7()
check("uuid7 is time-ordered and version 7",
      a7.hex[:12] <= b7.hex[:12] and a7.version == 7 and b7.version == 7,
      (a7, b7))

print()
print("[6] the desktop identity matches the 5.8.4 capture")
d = wb_identity.build_identity_headers("workbuddy", "intl", "u1", "tok")
check("X-IDE-Version is 5.8.4", d.get("X-IDE-Version") == "5.8.4",
      d.get("X-IDE-Version"))
check("the UA is the captured one",
      d.get("User-Agent") == "WorkBuddy/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0",
      d.get("User-Agent"))
check("X-Product is SaaS", d.get("X-Product") == "SaaS", d.get("X-Product"))
check("X-Agent-Type / X-Agent-Purpose / X-Agent-Intent",
      (d.get("X-Agent-Type"), d.get("X-Agent-Purpose"), d.get("X-Agent-Intent"))
      == ("main", "conversation", "craft"),
      (d.get("X-Agent-Type"), d.get("X-Agent-Purpose"), d.get("X-Agent-Intent")))
check("X-Private-Data is true", d.get("X-Private-Data") == "true")
check("the trace family is present",
      all(d.get(k) for k in ("X-Trace-ID", "traceparent", "b3",
                             "X-B3-TraceId", "X-B3-SpanId", "X-B3-Sampled")))
check("traceparent follows the W3C shape",
      str(d.get("traceparent", "")).startswith("00-")
      and str(d.get("traceparent", "")).endswith("-01"), d.get("traceparent"))
check("the desktop trace keeps its old shape (no parent span, b3 in three parts)",
      "X-B3-ParentSpanId" not in d and len(str(d.get("b3", "")).split("-")) == 3,
      (d.get("X-B3-ParentSpanId"), d.get("b3")))
check("the CN desktop keeps its own version and UA",
      wb_identity.build_identity_headers("workbuddy", "cn", "u1", "tok")["X-IDE-Version"]
      == "5.5.6")

print()
print("[6b] the CLI identity matches the official CodeBuddy CLI 2.168.2")
# 取值来源：官方 npm 包 @tencent-ai/codebuddy-code 2.168.2 解包（product.json 的
# 产品常量 + dist 里的 header 常量 / buildUserAgent / 请求头组装 / 请求体处理器）。
cli = wb_identity.build_identity_headers("cli", "intl", "u1", "tok")
check("the CLI UA is buildUserAgent's three segments",
      cli.get("User-Agent") == "CLI/2.168.2 CodeBuddy/2.168.2 OpenAI/JS 6.25.0",
      cli.get("User-Agent"))
check("the CLI X-IDE-* triple",
      (cli.get("X-IDE-Type"), cli.get("X-IDE-Name"), cli.get("X-IDE-Version"))
      == ("CLI", "CLI", "2.168.2"),
      (cli.get("X-IDE-Type"), cli.get("X-IDE-Name"), cli.get("X-IDE-Version")))
check("the CLI X-Product is SaaS", cli.get("X-Product") == "SaaS",
      cli.get("X-Product"))
check("the CLI agent headers",
      (cli.get("X-Agent-Intent"), cli.get("X-Agent-Purpose"),
       cli.get("X-Agent-Type"), cli.get("X-Agent-Runtime"))
      == ("craft", "conversation", "main", "other"),
      (cli.get("X-Agent-Intent"), cli.get("X-Agent-Purpose"),
       cli.get("X-Agent-Type"), cli.get("X-Agent-Runtime")))
check("the CLI does not send X-Private-Data on a normal personal session",
      "X-Private-Data" not in cli, cli.get("X-Private-Data"))
# OpenAI Node SDK 6.25.0 的 X-Stainless-* 指纹头。
stainless = {
    "X-Stainless-Lang": "js",
    "X-Stainless-Package-Version": "6.25.0",
    "X-Stainless-OS": "Windows",
    "X-Stainless-Arch": "x64",
    "X-Stainless-Runtime": "node",
    "X-Stainless-Runtime-Version": "v22.21.1",
    "X-Stainless-Retry-Count": "0",
    "X-Stainless-Timeout": "1200",
}
check("the CLI carries the OpenAI SDK's X-Stainless-* family",
      all(cli.get(k) == v for k, v in stainless.items()),
      [(k, cli.get(k)) for k, v in stainless.items() if cli.get(k) != v])
trace = (cli.get("X-Trace-ID"), cli.get("X-B3-TraceId"), cli.get("X-B3-SpanId"),
         cli.get("X-B3-ParentSpanId"))
check("the CLI trace family is complete",
      all(cli.get(k) for k in ("X-Trace-ID", "traceparent", "b3", "X-B3-TraceId",
                               "X-B3-ParentSpanId", "X-B3-SpanId", "X-B3-Sampled")),
      [k for k in ("X-Trace-ID", "traceparent", "b3", "X-B3-TraceId",
                   "X-B3-ParentSpanId", "X-B3-SpanId", "X-B3-Sampled")
       if not cli.get(k)])
check("the CLI trace family is all one trace",
      cli.get("X-Trace-ID") == cli.get("X-B3-TraceId")
      and cli.get("traceparent") == "00-%s-%s-01" % (trace[0], trace[2])
      and cli.get("b3") == "%s-%s-1-%s" % (trace[0], trace[2], trace[3])
      and cli.get("X-B3-Sampled") == "1", (trace, cli.get("traceparent")))
check("the CLI b3 has the official four segments (parent span present)",
      len(str(cli.get("b3", "")).split("-")) == 4, cli.get("b3"))
check("the CLI sends none of the headers the official CLI does not send",
      not any(k in cli for k in ("X-Product-Version", "X-Session-ID",
                                 "X-Model-ID", "X-Device-Token",
                                 "X-Root-Request-ID", "X-Parent-Conversation-ID")),
      sorted(set(("X-Product-Version", "X-Session-ID", "X-Model-ID",
                  "X-Device-Token", "X-Root-Request-ID",
                  "X-Parent-Conversation-ID")) & set(cli)))
check("the CN CLI carries the same official version",
      wb_identity.build_identity_headers("cli", "cn", "u1", "tok")["X-IDE-Version"]
      == "2.168.2")
check("the official chat body encoding is gzip",
      wb_identity.REQUEST_CONTENT_ENCODING == "gzip"
      and wb_identity.gzip_body(b'{"a":1}')[:2] == b"\x1f\x8b")

print()
print("[7] chat headers drop the feedback-only ids and keep the fingerprint shape")
conv_acc = A.Account(credential("workbuddy"))
chat = conv_acc.headers(purpose="chat")
check("no X-Machine-ID on the chat request", "X-Machine-ID" not in chat)
check("no X-Session-ID on the chat request", "X-Session-ID" not in chat)
cli_acc = A.Account(credential("cli"))
cli_chat = cli_acc.headers(purpose="chat", session_key="sess-9")
check("the CLI chat headers carry the official agent / trace family",
      (cli_chat.get("X-Agent-Runtime"), cli_chat.get("X-Agent-Purpose"),
       cli_chat.get("X-IDE-Version")) == ("other", "conversation", "2.168.2")
      and bool(cli_chat.get("X-B3-ParentSpanId")),
      (cli_chat.get("X-Agent-Runtime"), cli_chat.get("X-IDE-Version"),
       cli_chat.get("X-B3-ParentSpanId")))
check("the CLI chat headers drop the four browser-only headers",
      not any(k in cli_chat for k in ("Origin", "Referer",
                                      "X-CodeBuddy-Request", "Accept-Language"))
      and cli_chat.get("Accept") == "application/json",
      (cli_chat.get("Accept"), sorted(set(("Origin", "Referer",
                                          "X-CodeBuddy-Request",
                                          "Accept-Language")) & set(cli_chat))))
check("the desktop chat headers keep their browser headers (unchanged)",
      chat.get("Accept") == "application/json, text/plain, */*"
      and all(k in chat for k in ("Origin", "Referer",
                                  "X-CodeBuddy-Request", "Accept-Language")))
check("the CLI chat headers carry no feedback-only ids",
      "X-Machine-ID" not in cli_chat and "X-Session-ID" not in cli_chat)
check("the CLI chat headers follow the account's session key",
      cli_chat.get("X-Conversation-ID")
      == cli_acc.headers(purpose="chat", session_key="sess-9").get("X-Conversation-ID")
      and cli_chat.get("X-Conversation-ID")
      != cli_acc.headers(purpose="chat", session_key="sess-10").get("X-Conversation-ID"))
# 官方 CLI 全包没有设备 token 逻辑：CLI 身份一律不发 X-Device-Token（chat 与
# billing 都不发），桌面端维持原行为。
device_dir = os.path.join(_TMP, "device-token")
os.makedirs(device_dir, exist_ok=True)
wb_settings.set_upstream_config(device_dir, {"device_token": "dev-token-1"})
cli_dev = A.Account(credential("cli", realm="cn", domain="copilot.tencent.com"),
                    os.path.join(device_dir, "u-product-1.json"))
desk_dev = A.Account(credential("workbuddy", realm="cn", domain="copilot.tencent.com"),
                     os.path.join(device_dir, "u-product-1.json"))
check("the CLI identity never sends X-Device-Token",
      "X-Device-Token" not in cli_dev.headers(purpose="chat")
      and "X-Device-Token" not in cli_dev.headers(purpose="billing"))
check("the desktop identity still sends it when configured",
      desk_dev.headers(purpose="chat").get("X-Device-Token") == "dev-token-1")
check("the billing headers carry no conversation family",
      "X-Conversation-ID" not in conv_acc.headers(purpose="billing"))
machine = wb_fingerprint.derive_id("u1", "machine")
check("the machine id is a stable dashed uuid",
      machine == wb_fingerprint.derive_id("u1", "machine")
      and len(machine) == 36 and machine.count("-") == 4, machine)
# 每账号一台设备：sessionId 必须按账号派生。取进程级的值会让全部账号共用
# 同一个 session id，那是把它们串起来的关联键（device_token 是同一类问题）。
sess = wb_fingerprint.session_id("u1")
check("the session id is a stable dashed uuid",
      sess == wb_fingerprint.session_id("u1")
      and len(sess) == 36 and sess.count("-") == 4, sess)
check("the session id differs across accounts",
      wb_fingerprint.session_id("u1") != wb_fingerprint.session_id("u2"))
check("the session id is not the machine id",
      wb_fingerprint.session_id("u1") != machine)
check("the request id is 32 hex chars",
      len(wb_fingerprint.generate_request_id("u1")) == 32)

# 计费路径同样按账号隔离：两个账号的 X-Session-ID 不能相同。
sess_acc_a = A.Account(credential("workbuddy"))
sess_acc_b = A.Account(credential("workbuddy"))
sess_acc_b.uid = sess_acc_a.uid + "-other"
check("the billing session id is per account",
      sess_acc_a.headers(purpose="billing").get("X-Session-ID")
      != sess_acc_b.headers(purpose="billing").get("X-Session-ID"),
      sess_acc_a.headers(purpose="billing").get("X-Session-ID"))
check("the billing session id is stable for one account",
      sess_acc_a.headers(purpose="billing").get("X-Session-ID")
      == sess_acc_a.headers(purpose="billing").get("X-Session-ID"))

print()
print("[8] every desktop UA / version comes from wb_identity (single source)")
# PR #381 把 wb_identity 立为桌面端身份的唯一来源，但漏改了 REALM_CONFIGS
# 与两个死常量里各写一份的旧值（intl 那份还停在 5.5.2）。REALM_CONFIGS 的
# chat_ua 不只是留档：remote_config_headers() 拿它拉产品目录，UA 不对上游
# 直接 400 / code 12403。这几条断言把「单一来源」钉住，下次对齐漏一处就红。
for realm in ("intl", "cn"):
    cfg = A.get_realm_config(realm)
    want_ua = (wb_identity.DESKTOP_UA_INTL if realm == "intl"
               else wb_identity.DESKTOP_UA_CN)
    want_ver = (wb_identity.DESKTOP_VERSION_INTL if realm == "intl"
                else wb_identity.DESKTOP_VERSION_CN)
    check("%s chat_ua is wb_identity's" % realm, cfg["chat_ua"] == want_ua,
          cfg["chat_ua"])
    check("%s billing_ua is built from wb_identity's version" % realm,
          cfg["billing_ua"] == "WorkBuddy/" + want_ver, cfg["billing_ua"])
check("DEFAULT_UA_VERSION is wb_identity's intl version",
      A.DEFAULT_UA_VERSION == wb_identity.DESKTOP_VERSION_INTL,
      A.DEFAULT_UA_VERSION)
check("USER_AGENT follows the intl chat UA", A.USER_AGENT == wb_identity.DESKTOP_UA_INTL,
      A.USER_AGENT)
# 5.5.2 是这次修掉的过时值，任何身份常量再退回它都算回归。
check("no realm still advertises the stale 5.5.2",
      all("5.5.2" not in str(A.get_realm_config(r).get(k, ""))
          for r in ("intl", "cn") for k in ("chat_ua", "billing_ua")))

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
