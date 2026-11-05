"""单进程 CodeArts：配置、账号文件、账号池、模型、续期、签到、OAuth。"""
import io
import json
import os
import sys
import time
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-codearts-native-")
ACCOUNTS = os.environ["ACCOUNTS_DIR"]

import wb_codearts as C  # noqa: E402
import wb_codearts_crypto as crypto  # noqa: E402
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


def make_auth(state, uid="u1", name="账号一", expiration=None):
    return {
        "user_id": uid,
        "user_name": name,
        "domain_id": "d1",
        "cloud_dragon_token": state.token,
        "access_key_id": state.api_key,
        "secret_access_key": state.secret_key,
        "expiration": expiration or state.expiration,
        "refresh_token": state.refresh_token,
        "code_verifier": "verifier",
        "client_id": C.CLIENT_ID,
        "dpop_private_key": crypto.jwk_from_keypair(*crypto.generate_keypair()),
        "updated_at": int(time.time()),
    }


srv, base, state = start_server()
C.SNAP_HOST = base
C.SNAP_MANAGER = base + "/snap-manager"
C.STS_HOST = base
C.BENEFIT_HOST = base
C.PORTAL_HOST = base + "/portal"

try:
    print("== config ==")
    cfg = C.load_config(ACCOUNTS)
    check("default disabled", cfg["enabled"] is False)
    check("default max concurrent", cfg["max_concurrent"] == 1)
    C.save_config(ACCOUNTS, {"enabled": True, "max_concurrent": 2})
    check("enabled", C.configured(ACCOUNTS) is True)
    check("max concurrent saved", C.load_config(ACCOUNTS)["max_concurrent"] == 2)
    check("masked no secrets", "api_key" not in C.masked_config(ACCOUNTS))

    print("== auth storage ==")
    auth = make_auth(state)
    saved = C.save_auth(ACCOUNTS, auth)
    check("auth file exists", os.path.isfile(saved["_path"]), saved["_path"])
    loaded = C.load_auths(ACCOUNTS)
    check("auth loaded", len(loaded) == 1 and loaded[0]["user_id"] == "u1")
    check("dpop persisted", isinstance(loaded[0].get("dpop_private_key"), dict))

    print("== pool ==")
    pool = C.pool_for(ACCOUNTS)
    acct = pool.add(saved)
    check("pool has account", pool.get("u1") is acct)
    check("stats healthy", pool.stats()["healthy"] == 1, pool.stats())
    check("pick", pool.pick("GLM-5.2") is acct)
    check("acquire", pool.acquire(acct) is True)
    check("second acquire", pool.acquire(acct) is True)
    check("third acquire blocked at max 2", pool.acquire(acct) is False)
    pool.release(acct)
    pool.release(acct)
    check("release", acct.active_concurrent == 0)
    pool.disable("u1", "manual")
    check("disabled", acct.disabled is True and pool.stats()["disabled"] == 1)
    pool.enable("u1")
    check("enabled again", acct.disabled is False)
    print("== task opt-out ==")
    with mock.patch.object(C, '_maybe_auto_checkin') as auto:
        C.set_tasks_enabled(C.load_config(ACCOUNTS), 'u1', False)
        check("disabling tasks does not trigger auto checkin", not auto.called)
    pool.config['credits_auto_claim'] = True
    with mock.patch.object(C.threading, 'Thread') as worker:
        C._maybe_auto_checkin(pool)
        check("task opt-out blocks lazy auto checkin", not worker.called)
    with mock.patch.object(C, '_ensure_token') as ensure:
        C._auto_checkin_worker([acct])
        check("task opt-out blocks already queued worker", not ensure.called)
    import wb_scheduler
    scheduler = wb_scheduler.Scheduler(type('P', (), {'dir': ACCOUNTS})())
    with mock.patch.object(C, 'load_config', return_value={'enabled': True, 'credits_auto_claim': True}), \
         mock.patch.object(C, 'pool_for', return_value=pool) as lookup, \
         mock.patch.object(C, 'checkin') as claim:
        check("task opt-out blocks scheduler pending", not scheduler._codearts_checkin_pending(True))
        check("task opt-out blocks scheduler claim", scheduler._run_codearts_checkin(True) == 0 and not claim.called)
        check("scheduler lookup suppresses lazy trigger", all(c.kwargs.get('trigger_auto') is False for c in lookup.call_args_list))
    C.set_tasks_enabled(C.load_config(ACCOUNTS), 'u1', True)
    pool.config['credits_auto_claim'] = False
    pool.note_error(acct, "boom")
    pool.note_error(acct, "boom")
    pool.note_error(acct, "boom")
    check("cooldown after threshold", acct.cool_until > time.time())
    pool.clear_cooldown("u1")
    check("cooldown cleared", acct.cool_until == 0)

    print("== model id identity ==")
    acct.models = [
        {"id": "deepseek-v4.1-flash", "max_tokens": 1000},
        {"id": "deepseek-v4.1-flash-1017", "max_tokens": 2000},
    ]
    check("1017 canonical id stays exact",
          C._canonical_model(acct, "DEEPSEEK-V4.1-FLASH-1017")
          == "deepseek-v4.1-flash-1017")
    check("base canonical id stays exact",
          C._canonical_model(acct, "DEEPSEEK-V4.1-FLASH")
          == "deepseek-v4.1-flash")
    check("1017 has its own output limit",
          C._model_output_limit(acct, "deepseek-v4.1-flash-1017") == 2000)
    check("base has its own output limit",
          C._model_output_limit(acct, "deepseek-v4.1-flash") == 1000)

    print("== proxy slots ==")
    import urllib.request
    import wb_settings
    slots = wb_settings.set_proxy_slots(ACCOUNTS, [
        {"id": "slot-1", "name": "one", "url": "http://127.0.0.1:7890",
         "enabled": True},
        {"id": "slot-2", "name": "two", "url": "http://127.0.0.1:7891",
         "enabled": False},
    ])
    acct.proxy_slot = "slot-1"
    acct.auth["proxySlot"] = "slot-1"
    acct.auth = C.save_auth(ACCOUNTS, acct.auth)
    pool.apply_proxy_slots(slots)
    check("proxy slot resolved", acct.proxy == "http://127.0.0.1:7890",
          acct.proxy)
    acct.proxy_slot = "slot-2"
    pool.apply_proxy_slots(slots)
    check("disabled slot falls back direct", acct.proxy == "", acct.proxy)
    acct.proxy_slot = ""
    pool.apply_proxy_slots(slots)
    check("unbound slot is direct", acct.proxy == "", acct.proxy)
    acct.proxy_slot = "slot-1"
    pool.apply_proxy_slots(slots)
    opener = C._opener(acct.proxy)
    handler = next(h for h in opener.handlers
                   if isinstance(h, urllib.request.ProxyHandler))
    check("opener pins http/https",
          handler.proxies.get("http") == acct.proxy
          and handler.proxies.get("https") == acct.proxy, handler.proxies)
    direct_proxies = [h.proxies for h in C._opener("").handlers
                      if isinstance(h, urllib.request.ProxyHandler)]
    check("direct opener ignores env proxies",
          all(not proxies for proxies in direct_proxies), direct_proxies)
    seen = []
    orig_json = C._json_http
    C._json_http = lambda method, url, headers=None, body=None, \
        timeout=30.0, proxy="": (seen.append(proxy) or {})
    C.signed_get(acct, "http://example.test")
    C._json_http = orig_json
    check("signed_get threads account proxy", seen == [acct.proxy], seen)
    # Re-login writes a fresh auth document without panel-side fields; the
    # existing slot binding must survive and be written back.
    relogin = dict(acct.auth)
    relogin.pop("proxySlot", None)
    relogin = C.save_auth(ACCOUNTS, relogin)
    pool.add(relogin)
    check("relogin keeps proxy binding",
          pool.get("u1").proxy_slot == "slot-1",
          pool.get("u1").proxy_slot)
    check("relogin persists proxy binding",
          C.load_auths(ACCOUNTS)[0].get("proxySlot") == "slot-1",
          C.load_auths(ACCOUNTS)[0])
    check("relogin re-resolves proxy",
          pool.get("u1").proxy == "http://127.0.0.1:7890",
          pool.get("u1").proxy)
    # A real HTTP proxy must receive the absolute-form request line.
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    proxied = []

    class ProxyProbe(BaseHTTPRequestHandler):
        def do_GET(self):
            proxied.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    proxy_srv = ThreadingHTTPServer(("127.0.0.1", 0), ProxyProbe)
    threading.Thread(target=proxy_srv.serve_forever, daemon=True).start()
    proxy_url = "http://127.0.0.1:%d" % proxy_srv.server_address[1]
    status, raw = C._http("GET", "http://codearts.invalid/probe",
                          proxy=proxy_url)
    proxy_srv.shutdown()
    proxy_srv.server_close()
    check("http request goes through proxy", status == 200 and raw == b"{}",
          (status, raw))
    check("proxy saw absolute URI",
          proxied == ["http://codearts.invalid/probe"], proxied)
    # Do not leave the fake proxy bound for the chat tests below.
    acct.proxy_slot = ""
    acct.auth.pop("proxySlot", None)
    acct.auth = C.save_auth(ACCOUNTS, acct.auth)
    wb_settings.set_proxy_slots(ACCOUNTS, [])
    pool.apply_proxy_slots([])

    print("== models ==")
    models = C.fetch_models(acct)
    ids = [m["id"] for m in models]
    check("agent/builtin/benefit merged", set(ids) ==
          {"GLM-5.2", "GLM-4.7", "Qwen3-VL-235B",
           "deepseek-v4-pro-0813"}, ids)
    check("benefit marked", acct.is_benefit("deepseek-v4-pro-0813") is True)
    check("normal not benefit", acct.is_benefit("GLM-5.2") is False)
    entries = C.model_entries(C.load_config(ACCOUNTS))
    check("entries owned_by", entries[0]["owned_by"] == "codearts")
    check("entries benefit flag",
          any(e.get("benefit") for e in entries if e["id"] == "deepseek-v4-pro-0813"))
    check("models meta source", C.models_meta(C.load_config(ACCOUNTS))["source"] == "codearts")

    print("== credits ==")
    info = C.fetch_account_info(acct)
    check("credit package", info["is_credit_package"] is True, info)
    check("credit package name", info["package_name"] == "体验版", info)
    check("credit total", info["credits"]["total"]["remain"] == 4499.19, info)
    check("credit used", info["credits"]["total"]["used"] == 0.81, info)
    check("credit models", info["models"][0]["name"] == "GLM-5.2", info)
    ov = C.overview(C.load_config(ACCOUNTS))
    check("overview credits", ov["stats"]["credits"] == 4499.19, ov["stats"])
    check("account credits", ov["accounts"][0]["credits"]["remain"] == 4499.19,
          ov["accounts"][0])

    print("== refresh ==")
    acct.auth["expiration"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 10))
    before = state.token_calls
    C._ensure_token(acct)
    check("refresh called", state.token_calls > before)
    check("token restored", acct.expires_at() > time.time())
    check("auth file rewritten", C.load_auths(ACCOUNTS)[0]["expiration"] ==
          acct.auth["expiration"])

    print("== content parts ==")
    parts_body = C.build_chat_body({
        "model": "GLM-5.2",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "hello "},
            {"type": "text", "text": "world"},
        ]}],
    }, "sess-parts")
    check("content parts flattened",
          parts_body["messages"][0]["content"] == "hello world", parts_body)
    image_body = C.build_chat_body({"model": "Qwen3-VL-235B",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64,AAA"}},
        ]}]}, "sess-image")
    image_parts = image_body["messages"][0]["content"]
    check("image parts preserved", isinstance(image_parts, list)
          and image_parts[0] == {"type": "text", "text": "look"}
          and image_parts[1] == {"type": "image_url",
                                 "image_url": {"url": "data:image/png;base64,AAA"}},
          image_body)
    anthropic_body = C.build_chat_body({"model": "Qwen3-VL-235B",
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg", "data": "BBB"}},
        ]}]}, "sess-anthropic-image")
    anthropic_parts = anthropic_body["messages"][0]["content"]
    check("anthropic image normalized",
          anthropic_parts == [{"type": "image_url", "image_url": {
              "url": "data:image/jpeg;base64,BBB"}}], anthropic_body)

    print("== reasoning effort ==")
    effort_body = C.build_chat_body({
        "model": "GLM-5.2",
        "reasoning_effort": "high",
        "messages": [{"role": "user", "content": "hi"}],
    }, "sess-effort")
    check("effort forwarded", effort_body.get("reasoning_effort") == "high",
          effort_body)
    thinking_body = C.build_chat_body({
        "model": "GLM-5.2",
        "thinking": {"type": "enabled"},
        "messages": [{"role": "user", "content": "hi"}],
    }, "sess-thinking")
    check("thinking maps to high",
          thinking_body.get("reasoning_effort") == "high", thinking_body)
    try:
        C.build_chat_body({
            "model": "GLM-5.2", "reasoning_effort": "auto",
            "messages": [{"role": "user", "content": "hi"}],
        }, "sess-bad-effort")
        check("invalid effort rejected", False)
    except C.CodeArtsBadRequest:
        check("invalid effort rejected", True)
    always_body = C.build_chat_body({
        "model": "glm-5.3-flash", "reasoning_effort": "none",
        "messages": [{"role": "user", "content": "hi"}],
    }, "sess-always")
    check("always-thinking uses accepted level",
          always_body.get("reasoning_effort") == "low", always_body)
    sampling_body = C.build_chat_body({
        "model": "GLM-5.2",
        "messages": [{"role": "user", "content": "hi"}],
        "stop": "END", "seed": 7, "top_k": 5,
        "frequency_penalty": 0.2, "presence_penalty": 0.1,
        "response_format": {"type": "json_object"},
    }, "sess-sampling")
    check("sampling fields forwarded",
          sampling_body.get("stop") == ["END"]
          and sampling_body.get("seed") == 7
          and sampling_body.get("top_k") == 5
          and sampling_body.get("frequency_penalty") == 0.2
          and sampling_body.get("presence_penalty") == 0.1
          and sampling_body.get("response_format") == {"type": "json_object"},
          sampling_body)

    print("== output token clamp ==")
    body = C.build_chat_body({
        "model": "GLM-5.2",
        "messages": [{"role": "user", "content": "hi"}],
        "max_completion_tokens": 384000,
    }, "sess-clamp")
    stream, _acct, _model = C.open_chat(C.load_config(ACCOUNTS), body,
                                        "sess-clamp")
    b"".join(stream)
    stream.close()
    sent = state.last_body or {}
    check("output tokens clamped to advertised max",
          sent.get("max_tokens") == 8192, sent)
    check("completion spelling removed",
          "max_completion_tokens" not in sent, sent)
    bad = C.build_chat_body({
        "model": "GLM-5.2",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": "abc",
    }, "sess-bad-max")
    try:
        C.open_chat(C.load_config(ACCOUNTS), bad, "sess-bad-max")
        check("invalid max_tokens rejected locally", False)
    except C.CodeArtsBadRequest:
        check("invalid max_tokens rejected locally", True)
    check("invalid parameter did not cool the account",
          acct.cool_until == 0, acct.cool_until)

    embedded = C.CodeArtsStream(
        io.BytesIO(b'data: {"error_code":"invalid_param","error_msg":'
                   b'"The request param is invalid, Please check it"}\n\n'),
        "GLM-5.2", acct, acct.pool)
    try:
        embedded.preflight()
        check("embedded parameter error is a 400", False)
    except C.CodeArtsBadRequest as exc:
        check("embedded parameter error is a 400", exc.status == 400, exc.status)

    import io
    stream = C.CodeArtsStream(io.BytesIO(b'data: {"text":"hello"}\n\ndata: [DONE]\n\n'),
                              "model", acct, acct.pool)
    stream.preflight()
    result = b''.join(stream)
    check("first SSE text normalized", b'"content": "hello"' in result, result)

    print("== named tool choice ==")
    tool_body = C.build_chat_body({
        "model": "GLM-5.2",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {"type": "function", "function": {"name": "a", "parameters": {}}},
            {"type": "function", "function": {"name": "b", "parameters": {}}},
        ],
        "tool_choice": {"type": "function", "function": {"name": "b"}},
    }, "sess-tool")
    check("tool choice narrowed", len(tool_body["tools"]) == 1
          and tool_body["tools"][0]["function"]["name"] == "b", tool_body)
    check("tool choice required", tool_body["tool_choice"] == "required",
          tool_body)

    print("== malformed tools ==")
    dirty_body = C.build_chat_body({
        "model": "GLM-5.2",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {"type": "function", "function": {"name": "ok", "parameters": {}}},
            {"type": "function"},
            {"type": "web_search"},
            {"type": "function", "function": {"name": "no_params"}},
            {"type": "function", "function": {"name": "ok"}},
            {"name": "flat", "description": "d"},
        ],
    }, "sess-dirty-tools")
    names = [t["function"]["name"] for t in dirty_body.get("tools") or []]
    check("unusable tools dropped", names == ["ok", "no_params", "flat"], names)
    check("missing parameters defaulted",
          dirty_body["tools"][1]["function"]["parameters"]
          == {"type": "object", "properties": {}}, dirty_body)
    check("flat tool wrapped",
          dirty_body["tools"][2]["type"] == "function"
          and dirty_body["tools"][2]["function"]["parameters"]
          == {"type": "object", "properties": {}}, dirty_body)
    empty_tools_body = C.build_chat_body({
        "model": "GLM-5.2",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function"}, {"type": "web_search"}],
        "tool_choice": "auto",
    }, "sess-empty-tools")
    check("all-unusable tools removed",
          "tools" not in empty_tools_body and "tool_choice" not in empty_tools_body,
          empty_tools_body)

    print("== concurrency retry ==")
    state.chat_error = (400, {"error_msg": "并发会话数已达上限(3个)，请关闭部分会话后重试。"})
    state.chat_error_remaining = 1
    body = C.build_chat_body({"model": "GLM-5.2",
                              "messages": [{"role": "user", "content": "hi"}],
                              "stream": False}, "sess-retry")
    stream, acct3, model3 = C.open_chat(C.load_config(ACCOUNTS), body, "sess-retry")
    raw = b"".join(stream)
    stream.close()
    check("concurrency retry succeeds", "你好" in raw.decode("utf-8", "replace"),
          raw[:200])
    state.chat_error = None
    state.chat_error_remaining = -1

    print("== session header ==")

    def _chat_headers_of(session_key):
        body = C.build_chat_body({"model": "GLM-5.2",
                                  "messages": [{"role": "user", "content": "hi"}],
                                  "stream": False}, session_key)
        stream, _acct, _model = C.open_chat(C.load_config(ACCOUNTS), body, session_key)
        b"".join(stream)
        stream.close()
        for req in reversed(state.requests):
            if req["path"].endswith("/chat/completions"):
                return req["headers"]
        return {}

    ot_first = _chat_headers_of("sess-ot-1")
    ot_second = _chat_headers_of("sess-ot-2")
    ot_id = ot_first.get("x-ot-session-id") or ""
    check("session header sent", bool(ot_id), ot_first)
    check("session header is 32 hex",
          len(ot_id) == 32 and all(c in "0123456789abcdef" for c in ot_id), ot_id)
    check("session header stable per account",
          ot_id == (ot_second.get("x-ot-session-id") or ""),
          (ot_id, ot_second.get("x-ot-session-id")))
    check("user-session-id mirrors it",
          ot_first.get("user-session-id") == ot_id, ot_first.get("user-session-id"))
    check("trace id differs per request",
          ot_first.get("x-ot-trace-id") != ot_second.get("x-ot-trace-id"),
          (ot_first.get("x-ot-trace-id"), ot_second.get("x-ot-trace-id")))
    check("chat id still per conversation",
          ot_first.get("chat-id") != ot_second.get("chat-id"),
          (ot_first.get("chat-id"), ot_second.get("chat-id")))

    print("== text snapshot stream ==")
    state.chat_mode = "text"
    body = C.build_chat_body({"model": "GLM-5.2",
                              "messages": [{"role": "user", "content": "hi"}],
                              "stream": False}, "sess-text")
    stream, acct2, model2 = C.open_chat(C.load_config(ACCOUNTS), body, "sess-text")
    raw = b"".join(stream)
    stream.close()
    text = raw.decode("utf-8", "replace")
    chunks = [json.loads(line[6:]) for line in text.splitlines()
              if line.startswith('data: {')]
    joined = ''.join(((chunk.get('choices') or [{}])[0].get('delta') or {}).get('content') or ''
                     for chunk in chunks if chunk.get('choices'))
    check("text stream content", joined == "你好", joined)
    check("text stream no done content", '"content": "[DONE]"' not in text, text[:300])
    state.chat_mode = "delta"

    print("== checkin ==")
    result = C.checkin(C.load_config(ACCOUNTS))
    check("checkin ok", result["ok"] is True, result)
    check("checkin credits", result["credits"] == 1000, result)
    check("checkin stored", acct.checkin and acct.checkin.get("ok") is True)
    pool.save_state()
    reloaded = C.CodeArtsPool(ACCOUNTS, C.load_config(ACCOUNTS))
    check("checkin persisted", reloaded.get("u1").checkin == acct.checkin,
          reloaded.get("u1").checkin)
    saved_checkin = acct.checkin
    check("checked_in_today true", acct.checked_in_today() is True)
    acct.checkin = {"ok": True, "checked_at": "2000-01-01T00:00:00"}
    check("checked_in_today old false", acct.checked_in_today() is False)
    acct.checkin = {"ok": False,
                    "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    check("checked_in_today failed false", acct.checked_in_today() is False)
    acct.checkin = saved_checkin

    print("== oauth ==")
    started = C.oauth_start(C.load_config(ACCOUNTS), port=8788)
    check("oauth session", bool(started.get("session_id")))
    check("oauth url", "authorize" in started["auth_url"] and
          "code_challenge_method=SHA-256" in started["auth_url"])
    pending = C.oauth_poll(C.load_config(ACCOUNTS), started["session_id"])
    check("oauth pending", pending["status"] == "pending", pending)
    done = C.oauth_callback(
        C.load_config(ACCOUNTS), started["session_id"],
        "http://127.0.0.1:8788/oauth/callback?code=abc")
    check("oauth done", done["status"] == "done", done)
    check("oauth account saved", C.pool_for(ACCOUNTS).get("u1") is not None)

    started2 = C.oauth_start(C.load_config(ACCOUNTS), port=8788)
    cont = C.oauth_callback(
        C.load_config(ACCOUNTS), started2["session_id"],
        "http://127.0.0.1:8788/oauth/callback?secret=s1&redirect="
        + __import__("urllib.parse").parse.quote(C.PORTAL_HOST + "/x?ticket_id="
                                                  + started2["session_id"], safe=""))
    check("oauth continue", cont["status"] == "continue", cont)
    check("oauth next_url", cont["next_url"].startswith(C.PORTAL_HOST), cont)
    check("oauth rejects foreign redirect", not C._safe_oauth_redirect("https://evil.invalid/"))
    check("oauth rejects embedded credentials", not C._safe_oauth_redirect("https://codearts.huaweicloud.com@evil.invalid/"))

    print("== oauth without identity ==")
    import base64
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "sub-123"}).encode()).decode().rstrip("=")
    check("refresh token sub", C.identity_from_refresh_token("a." + payload + ".b") == "sub-123")
    state.omit_identity = True
    started3 = C.oauth_start(C.load_config(ACCOUNTS), port=8788)
    done3 = C.oauth_callback(
        C.load_config(ACCOUNTS), started3["session_id"],
        "http://127.0.0.1:8788/oauth/callback?code=abc")
    check("oauth no identity done", done3["status"] == "done", done3)
    saved3 = [a for a in C.load_auths(ACCOUNTS) if a["user_id"] == "u1"]
    check("identity looked up", bool(saved3) and saved3[0]["user_name"] == "账号一",
          saved3)
    state.omit_identity = False

    print("== overview ==")
    ov = C.overview(C.load_config(ACCOUNTS))
    check("overview service", ov["service"] == "codearts")
    check("overview accounts", len(ov["accounts"]) >= 1, ov)
    check("overview models", len(ov["models"]) >= 1, ov)
    check("health enabled", C.health(C.load_config(ACCOUNTS))["ok"] is True)
finally:
    stop_server(srv)

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
