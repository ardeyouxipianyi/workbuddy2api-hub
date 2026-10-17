"""官方 VSCode 扩展 4.12.39997873 的出站身份必须逐项对齐。

来源：Open VSX 包 `Tencent-Cloud.coding-copilot` 4.12.39997873 的解包源码
（只读分析，见 `pkg/vsix/extension`，`out/extension/index.js` 的模块偏移写在
每条断言旁）：

  * `product.json` / `product.internal.json`：productName=CodeBuddy、
    platform=VSCode、deploymentType=SaaS、productFeatures.RequestBodyGzip=true；
  * `UserAgentHttpInterceptor.buildUserAgent`：UA =
    `<platform>/<platformVersion> <productName>/<productVersion>`；
  * `CustomHeadersHttpProxyInterceptor`：X-IDE-Type / X-IDE-Name /
    X-IDE-Version / X-Product-Version / X-Request-Trace-Id；
  * `HttpEnvRouteInterceptor`：X-Env-ID=production；
  * `BaseAgent`：requestId / conversationId = `uuidv4().replace(/-/g,"")`
    （32hex）；agentType=main（主对话）；
  * model-provider `buildModelRequestHeaders`：X-Agent-Intent、
    X-Conversation-* 家族、X-Model-ID；X-Request-ID 与
    X-Conversation-Request-ID 同值（Message-ID 是另一个独立随机值）；
  * trace span 的 `requestHeaders`：X-Trace-ID / b3（四段）/
    X-B3-TraceId / X-B3-ParentSpanId / X-B3-SpanId / X-B3-Sampled；
    官方自身代码没有 traceparent；
  * `CraftInvokableAgent` 拦截器：X-Private-Data（国内站不发）；
  * `BaseAgent._preRequest` / `_executeSingleStep`：monitor_promptPrepareStartTime /
    monitor_httpSendTime（毫秒时间戳，model 请求）；
  * `TuringShieldServiceImpl`：非企业号带真实 X-Device-Token，取不到时发
    X-Device-Token-Error（hub 无本机 SDK，用官方同款 "sdk_not_initialized"）。

本套件钉住这些值，以及 open_upstream 真的把它们带出去（model / 稳定
conversation id）。桌面端 / CLI 分支的行为也在末尾钉一遍，防止这次对齐改到
它们。gzip 暂不压（官方 RequestBodyGzip=true 与扩展默认开关的关系待抓包
确认），测试里只钉「当前不发 Content-Encoding」。

No network: accounts are built from dicts, the transport is a scripted stub.
"""
import io
import json
import os
import sys
import tempfile
import urllib.error

_TMP = tempfile.mkdtemp(prefix="wb-vscode-identity-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_identity
import wb_proxy

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


def hexs(value, n):
    value = str(value or "")
    return len(value) == n and all(c in "0123456789abcdef" for c in value)


def dashed_uuid(value):
    value = str(value or "")
    return len(value) == 36 and value.count("-") == 4


INTL_UA = "VSCode/1.119.0 CodeBuddy/4.12.39997873"

print("[1] product constants follow the official extension")
check("productVersion is 4.12.39997873",
      wb_identity.VSCODE_PRODUCT_VERSION == "4.12.39997873")
check("host VSCode version stays 1.119.0 (constant)",
      wb_identity.VSCODE_IDE_VERSION == "1.119.0")
check("UA is VSCode/<host> CodeBuddy/<extension>",
      wb_identity.VSCODE_UA == INTL_UA, wb_identity.VSCODE_UA)
check("the endpoint table is untouched",
      wb_identity.endpoint_for("intl", "vscode")
      == ("https://www.workbuddy.ai", "www.workbuddy.ai")
      and wb_identity.endpoint_for("cn", "vscode")
      == ("https://www.workbuddy.cn", "www.workbuddy.cn"))

print()
print("[2] the VSCode chat headers match the official extension")
h = wb_identity.build_identity_headers(
    "vscode", "intl", "u1", "tok", model="deepseek-v4.1-flash")
check("User-Agent", h.get("User-Agent") == INTL_UA, h.get("User-Agent"))
check("X-IDE-Type / X-IDE-Name are VSCode",
      (h.get("X-IDE-Type"), h.get("X-IDE-Name")) == ("VSCode", "VSCode"),
      (h.get("X-IDE-Type"), h.get("X-IDE-Name")))
check("X-IDE-Version is the host VSCode version",
      h.get("X-IDE-Version") == "1.119.0", h.get("X-IDE-Version"))
check("X-Product-Version is the extension version",
      h.get("X-Product-Version") == "4.12.39997873", h.get("X-Product-Version"))
check("X-Env-ID is production", h.get("X-Env-ID") == "production", h.get("X-Env-ID"))
check("X-Product is kept (main-chat evidence pending a 5.8.x capture)",
      h.get("X-Product") == "SaaS", h.get("X-Product"))
check("X-Model-ID carries this request's model",
      h.get("X-Model-ID") == "deepseek-v4.1-flash", h.get("X-Model-ID"))
check("X-Agent-Intent is craft, X-Agent-Type is main",
      (h.get("X-Agent-Intent"), h.get("X-Agent-Type")) == ("craft", "main"),
      (h.get("X-Agent-Intent"), h.get("X-Agent-Type")))
check("no X-Agent-Purpose (0 hits in the official package)",
      "X-Agent-Purpose" not in h)
check("no X-Agent-Runtime (0 hits in the official package)",
      "X-Agent-Runtime" not in h)
check("X-Private-Data is true on the international site",
      h.get("X-Private-Data") == "true", h.get("X-Private-Data"))
check("X-Domain is the international one", h.get("X-Domain") == "www.workbuddy.ai")
check("no X-Device-Token is invented",
      "X-Device-Token" not in h)
check("the main chat sends no X-Root-Request-ID (subagent-only header)",
      "X-Root-Request-ID" not in h)

print()
print("[3] the id relations follow the official model-provider")
check("X-Request-ID equals X-Conversation-Request-ID",
      h.get("X-Request-ID") == h.get("X-Conversation-Request-ID"),
      (h.get("X-Request-ID"), h.get("X-Conversation-Request-ID")))
check("X-Conversation-Message-ID is independent",
      h.get("X-Conversation-Message-ID") != h.get("X-Request-ID"))
check("request / message ids are 32 hex",
      hexs(h.get("X-Request-ID"), 32)
      and hexs(h.get("X-Conversation-Message-ID"), 32))
check("X-Conversation-ID is 32 hex (no dashes)",
      hexs(h.get("X-Conversation-ID"), 32), h.get("X-Conversation-ID"))
check("a caller-supplied conversation id is honoured",
      wb_identity.build_identity_headers(
          "vscode", "intl", "u1", "tok", conversation_id="conv-1")
      ["X-Conversation-ID"] == "conv-1")
check("no model -> no X-Model-ID and no monitor headers",
      "X-Model-ID" not in wb_identity.build_identity_headers(
          "vscode", "intl", "u1", "tok")
      and "monitor_promptPrepareStartTime" not in wb_identity.build_identity_headers(
          "vscode", "intl", "u1", "tok"))
check("the monitor timestamps ride on model requests",
      str(h.get("monitor_promptPrepareStartTime", "")).isdigit()
      and str(h.get("monitor_httpSendTime", "")).isdigit(),
      (h.get("monitor_promptPrepareStartTime"), h.get("monitor_httpSendTime")))

print()
print("[4] the trace family is the official B3 one (no traceparent)")
trace = h
b3 = str(trace.get("b3", "")).split("-")
check("X-Trace-ID equals X-B3-TraceId",
      trace.get("X-Trace-ID") == trace.get("X-B3-TraceId")
      and hexs(trace.get("X-Trace-ID"), 32))
check("b3 has four segments", len(b3) == 4, trace.get("b3"))
check("b3 = <traceId>-<spanId>-1-<parentSpanId>",
      b3[0] == trace.get("X-Trace-ID") and b3[1] == trace.get("X-B3-SpanId")
      and b3[2] == "1" and b3[3] == trace.get("X-B3-ParentSpanId"),
      trace.get("b3"))
check("X-B3-SpanId and X-B3-ParentSpanId are 16 hex",
      hexs(trace.get("X-B3-SpanId"), 16) and hexs(trace.get("X-B3-ParentSpanId"), 16))
check("no traceparent on the VSCode identity",
      "traceparent" not in trace)
check("X-B3-Sampled is 1", trace.get("X-B3-Sampled") == "1")
check("X-Request-Trace-Id is a per-request dashed uuid",
      dashed_uuid(trace.get("X-Request-Trace-Id")), trace.get("X-Request-Trace-Id"))
check("two calls get different X-Request-Trace-Id",
      wb_identity.build_identity_headers("vscode", "intl", "u1", "tok")
      ["X-Request-Trace-Id"]
      != wb_identity.build_identity_headers("vscode", "intl", "u1", "tok")
      ["X-Request-Trace-Id"])

print()
print("[5] X-Private-Data follows the official domestic-site rule")
cn = wb_identity.build_identity_headers("vscode", "cn", "u1", "tok")
check("the CN site does not send X-Private-Data",
      "X-Private-Data" not in cn)
check("the CN site keeps the CN domain", cn.get("X-Domain") == "www.workbuddy.cn")
check("the CN site uses the same UA/versions",
      cn.get("User-Agent") == INTL_UA
      and cn.get("X-Product-Version") == "4.12.39997873")

print()
print("[6] Account.headers wires model / conversation id / device token through")
account = wb_accounts.Account({"uid": "vsc-1", "accessToken": "tok",
                               "realm": "intl", "product": "vscode"})
chat = account.headers("chat", model="hy3", conversation_id="conv-9")
check("chat headers carry X-Model-ID", chat.get("X-Model-ID") == "hy3")
check("chat headers carry the given conversation id",
      chat.get("X-Conversation-ID") == "conv-9")
check("chat headers use the VSCode UA", chat.get("User-Agent") == INTL_UA)
check("no real device token -> the official sdk_not_initialized placeholder",
      chat.get("X-Device-Token-Error") == "sdk_not_initialized"
      and "X-Device-Token" not in chat,
      (chat.get("X-Device-Token-Error"), chat.get("X-Device-Token")))
enterprise = wb_accounts.Account({"uid": "vsc-ent", "accessToken": "tok",
                                  "realm": "intl", "product": "vscode",
                                  "enterpriseId": "ent-1"})
check("enterprise accounts send no device-token header at all (official rule)",
      "X-Device-Token-Error" not in enterprise.headers("chat", model="hy3"))
billing = account.headers("billing")
check("billing headers stay free of the chat identity",
      "X-Model-ID" not in billing and "X-Conversation-ID" not in billing
      and "X-Agent-Intent" not in billing and "X-Request-Trace-Id" not in billing)

print()
print("[7] open_upstream sends the whole VSCode identity (gzip + stable conv id)")


class FakeResp(object):
    pass


def http_error(code, body):
    return urllib.error.HTTPError("https://upstream/chat", code, "err", {},
                                  io.BytesIO(body))


class ScriptedUpstream(object):
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, req, timeout=None, proxy=None):
        self.calls.append(req)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class StubPool(object):
    def __init__(self, accounts):
        self.accounts = accounts
        self.affinity = wb_accounts.SessionAffinity()

    def count_ready(self, realm, model=None):
        return sum(1 for a in self.accounts
                   if a.realm == realm and a.ready(model=model))

    def pick_for_session(self, realm=None, session_key=None, exclude=(),
                         model=None, page=None):
        for a in self.accounts:
            if a.uid in exclude:
                continue
            if realm and a.realm != realm:
                continue
            if a.ready(model=model):
                return a
        return None

    def apply_remaining_weights(self, weights=None):
        return None

    def apply_daily_token_limit(self, *args, **kwargs):
        return 0

    def apply_daily_credit_limit(self, *args, **kwargs):
        return 0

    def apply_model_daily_token_limit(self, *args, **kwargs):
        return 0


def header_map(req):
    return {str(name).lower(): value for name, value in req.header_items()}


PAYLOAD = {"model": "deepseek-v4.1-flash",
           "messages": [{"role": "user", "content": "hi"}]}
account = wb_accounts.Account({"uid": "vsc-1", "accessToken": "tok",
                               "realm": "intl", "product": "vscode"})
pool = StubPool([account])
# 第一次 550（瞬时故障，重试），第二次成功：两次尝试都应带同一套身份。
upstream = ScriptedUpstream([http_error(550, b""), FakeResp()])
old_pool, old_urlopen = wb_proxy.POOL, wb_accounts.urlopen
old_accounts_dir = wb_proxy.ACCOUNTS_DIR
wb_proxy.POOL = pool
wb_proxy.ACCOUNTS_DIR = os.environ["ACCOUNTS_DIR"]
wb_accounts.urlopen = upstream
try:
    _resp, picked, _effort = wb_proxy.open_upstream(dict(PAYLOAD),
                                                   target_realm="intl")
finally:
    wb_proxy.POOL = old_pool
    wb_proxy.ACCOUNTS_DIR = old_accounts_dir
    wb_accounts.urlopen = old_urlopen

check("the retry loop made exactly two upstream calls", len(upstream.calls) == 2,
      len(upstream.calls))
check("the serving account is the VSCode one", getattr(picked, "uid", None) == "vsc-1")
if len(upstream.calls) >= 2:
    first = header_map(upstream.calls[0])
    second = header_map(upstream.calls[1])
    check("UA goes out as the official one", first.get("user-agent") == INTL_UA,
          first.get("user-agent"))
    check("X-IDE-Version / X-Product-Version go out",
          first.get("x-ide-version") == "1.119.0"
          and first.get("x-product-version") == "4.12.39997873")
    check("X-Model-ID goes out with the payload's model",
          first.get("x-model-id") == PAYLOAD["model"], first.get("x-model-id"))
    check("X-Agent-Intent / X-Agent-Type / X-Private-Data go out",
          first.get("x-agent-intent") == "craft"
          and first.get("x-agent-type") == "main"
          and first.get("x-private-data") == "true")
    check("no X-Agent-Purpose / X-Agent-Runtime on the wire",
          "x-agent-purpose" not in first and "x-agent-runtime" not in first)
    check("X-Request-ID equals X-Conversation-Request-ID on the wire",
          first.get("x-request-id") == first.get("x-conversation-request-id"))
    check("X-Conversation-Message-ID is independent on the wire",
          first.get("x-conversation-message-id") != first.get("x-request-id"))
    check("the conversation id is 32 hex",
          hexs(first.get("x-conversation-id"), 32), first.get("x-conversation-id"))
    check("the conversation id is stable across the retry",
          first.get("x-conversation-id") == second.get("x-conversation-id"),
          (first.get("x-conversation-id"), second.get("x-conversation-id")))
    check("X-Request-Trace-Id is fresh per attempt",
          first.get("x-request-trace-id") != second.get("x-request-trace-id"))
    check("the b3 header is four segments on the wire",
          len(str(first.get("b3", "")).split("-")) == 4, first.get("b3"))
    check("the trace family is on the wire",
          first.get("x-trace-id") and first.get("x-b3-traceid")
          and first.get("x-b3-spanid") and first.get("x-b3-parentspanid")
          and first.get("x-b3-sampled") == "1")
    check("no traceparent on the wire", "traceparent" not in first)
    check("the model request carries the monitor timestamps",
          str(first.get("monitor_promptpreparestarttime", "")).isdigit()
          and str(first.get("monitor_httpsendtime", "")).isdigit())
    # gzip 待确认：官方 product.json 有 productFeatures.RequestBodyGzip=true，
    # 但扩展 BaseAgent 的 enableRequestBodyGzip 默认 false；主 chat 是否真的
    # 压缩要等抓包，本 PR 先不压。确认后再把这条断言翻过来。
    check("no request-body gzip while the feature stays unconfirmed",
          "content-encoding" not in first)
    raw = upstream.calls[0].data or b""
    check("the body round-trips",
          json.loads(raw.decode("utf-8"))["model"] == PAYLOAD["model"])

print()
print("[8] the desktop / CLI identities are untouched")
d = wb_identity.build_identity_headers("workbuddy", "intl", "u1", "tok",
                                       model="ignored")
check("desktop UA stays the 5.8.4 capture",
      d.get("User-Agent") == "WorkBuddy/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0",
      d.get("User-Agent"))
check("desktop keeps Request-ID = Message-ID",
      d.get("X-Request-ID") == d.get("X-Conversation-Message-ID"))
check("desktop keeps its traceparent and three-segment b3",
      str(d.get("traceparent", "")).startswith("00-")
      and len(str(d.get("b3", "")).split("-")) == 3, d.get("b3"))
check("desktop does not get X-Model-ID / X-Product-Version / X-Request-Trace-Id",
      "X-Model-ID" not in d and "X-Product-Version" not in d
      and "X-Request-Trace-Id" not in d)
c = wb_identity.build_identity_headers("cli", "intl", "u1", "tok", model="ignored")
check("CLI UA stays CLI/2.168.2 CodeBuddy/2.168.2 OpenAI/JS 6.25.0",
      c.get("User-Agent") == "CLI/2.168.2 CodeBuddy/2.168.2 OpenAI/JS 6.25.0", c.get("User-Agent"))
check("CLI keeps Request-ID = Message-ID",
      c.get("X-Request-ID") == c.get("X-Conversation-Message-ID"))
check("CLI does not get X-Model-ID / X-Request-Trace-Id",
      "X-Model-ID" not in c and "X-Request-Trace-Id" not in c)

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
