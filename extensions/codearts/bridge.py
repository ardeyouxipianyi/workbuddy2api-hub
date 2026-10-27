"""Guarded, process-local Hub adapters. Original launchers never import this file."""
from contextlib import contextmanager
import copy
import hashlib
import inspect
import json
import math
from pathlib import Path
import threading
import urllib.error
from urllib.parse import parse_qs, urlsplit

from .client import ADMIN_ACTIONS, CodeArtsClient, is_codearts, upstream_model
from .config import BridgeConfig
from .credits import (CreditLedger, credits_for, effective_multipliers,
                      effective_night_multipliers, panel_view, parse_windows,
                      resolve_multiplier)
from .media import validate_payload, install_responses_detail, has_image


class _AliasStream:
    """回退流的别名重写：只把 SSE chunk 的 model 字段改回客户端请求的 codeart 别名。

    与 client.ModelStream 的行缓冲机制同构（参照 client.py:40-107），差异：
    不识别 event: error、不做 [DONE] 强制检测、不产生 stream_error——错误语义由
    wb 原生路径（_chat_stream_response 的 pending_finish/异常分支）自己管理。
    同时修复非流式路径：aggregate_stream 会用 chunk 的 model 覆盖初始值，
    不重写则非流式响应体 model 会变成目标模型。
    """
    def __init__(self, response, model):
        self.response = response
        self.model = model
        self.pending = b""
        self.headers = getattr(response, "headers", None)
        self._wb_billed_model = model

    @property
    def length(self):
        """动态透传底层 HTTPResponse.length，供 iter_sse_lines 检测上游提前关闭
        （见 wb_proxy.py 的 IncompleteRead 路径，issue #522 的 reopen 重试）。

        必须动态读取而非在 __init__ 快照：底层 read1() 会随读取递减 length，
        快照值永不更新，完整读完也会被误判成截断。
        """
        return getattr(self.response, "length", 0)

    def __iter__(self):
        """Responses 流式路径用 `for raw in upstream:` 直接迭代（见 wb_proxy.py 的
        stream_responses_events），只实现 read1 会抛 "not iterable"。与 client.py 的
        ModelStream.__iter__ 同构：按块读出后切行产出。
        """
        while True:
            block = self.read1()
            if not block:
                return
            yield from block.splitlines(keepends=True)

    def read1(self, size=65536):
        while True:
            chunk = self.response.read1(size)
            self.pending += chunk
            if not chunk:
                if not self.pending:
                    return b""
                head, self.pending = self.pending, b""
            elif b"\n" in self.pending:
                head, _, self.pending = self.pending.rpartition(b"\n")
            else:
                continue
            out = []
            for line in head.split(b"\n"):
                if line.startswith(b"data:") and line[5:].strip().startswith(b"{"):
                    try:
                        obj = json.loads(line[5:].strip())
                    except ValueError:
                        obj = None
                    if isinstance(obj, dict) and "model" in obj:
                        obj["model"] = self.model
                        line = b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8")
                out.append(line + b"\n")
            return b"".join(out)

    def close(self):
        self.response.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _safe_fallback_token(value):
    """把可能来自客户端/配置的字符串压成一个可安全放进 HTTP 头值的单行 token。

    客户端可控的 model 会写进 X-WB-Vision-Fallback 头值与日志。这里的字符集是
    **printable ASCII（0x20..0x7E）白名单**，遇到首个越界字符即截断到其之前：
      * CR/LF 等控制字符被截断，杜绝响应头被拆行注入（如 "codeart/x\\r\\nX-Evil: pwned"）；
      * CJK/emoji 等非 ASCII 被截断，保证头值可被 http.server 以 latin-1 编码——
        否则 send_header() 会抛 UnicodeEncodeError，整个响应都发不出去；
      * 日志同样只记录这批安全字符，避免日志被拆行。
    截断后为空则退化为占位值。
    """
    text = str(value or "")
    for index, ch in enumerate(text):
        if ord(ch) < 0x20 or ord(ch) > 0x7e:
            text = text[:index]
            break
    return text or "unknown"


class ServicePool:
    """An upstream service handle, never inserted into WorkBuddy's account pool."""
    uid = "codearts:pool"
    realm = "codearts"
    proxy = ""

    def __init__(self, client):
        self.client = client

    def chat_base_url(self):
        return self.client.config.snapshot()["base_url"]


class BridgeRuntime:
    def __init__(self, hub, path):
        self.hub = hub
        self.config = BridgeConfig(path)
        self.config.view()  # Invalid stored settings fail before the listener starts.
        self.client = CodeArtsClient(self.config)
        self.service_pool = ServicePool(self.client)
        # Local credit ledger for codeart turns. It sits next to the bridge
        # configuration, so `--codearts-config` moves it with the settings and
        # a portable install keeps its balances.
        self.ledger = CreditLedger(self.config.path.parent / "credits.json")
        self.local = threading.local()

    # -- local credit billing ------------------------------------------
    def credit_settings(self):
        return self.config.snapshot()

    def credit_quote(self, model, cfg=None, at=None):
        """The rate table row that applies to `model` at `at` (default: now)."""
        cfg = cfg or self.config.snapshot()
        return resolve_multiplier(
            model,
            multipliers=effective_multipliers(cfg.get("credits_multipliers")),
            night_multipliers=effective_night_multipliers(
                cfg.get("credits_night_multipliers")),
            windows=parse_windows(cfg.get("credits_night_window")),
            at=at)

    def charge_usage_row(self, row):
        """Deduct one persisted `codeart/*` usage row from the key that paid.

        The Hub records `credit: 0` for these turns, so the amount comes from
        the multiplier table and the row's own token counts. Rows without a key
        (panel test, launcher key, no-key deployment) and failed rows (no token
        settlement) are not billed. Returns the ledger entry, or None when the
        row was not billed.
        """
        key = str(row.get("key") or "").strip()
        if not key or row.get("error"):
            return None
        cfg = self.config.snapshot()
        if not cfg.get("credits_enabled"):
            return None
        tokens = int(row.get("total_tokens") or 0)
        if tokens <= 0:
            return None
        quote = self.credit_quote(row.get("model") or row.get("upstream_model"),
                                  cfg, at=row.get("at"))
        amount = credits_for(row.get("prompt_tokens"), row.get("completion_tokens"),
                             quote["multiplier"], cfg["credits_base_per_1k"])
        if amount <= 0:
            return None
        return self.ledger.charge(key, amount, {
            "model": row.get("model"),
            "upstream_model": row.get("upstream_model"),
            "tokens": tokens,
            "multiplier": quote["multiplier"],
            "night": quote["night_rate"],
            "base_per_1k": cfg["credits_base_per_1k"],
        })

    def credits_block_reason(self):
        """Why this request may not start, or "" when it may (402 otherwise).

        Only a key that can actually be billed is subject to the balance floor;
        a panel test or a deployment without API keys keeps working, because
        there is no ledger row to charge against.
        """
        cfg = self.config.snapshot()
        if not cfg.get("credits_enabled") or not cfg.get("credits_enforce"):
            return ""
        handler = getattr(self.local, "handler", None)
        key = (handler._key_id() if handler is not None else None) or ""
        if not key:
            return ""
        balance = self.ledger.balance(key)
        if balance > 0:
            return ""
        return ("CodeArts 积分不足：%s 的余额为 %.4f，请在 CodeArts 面板「积分」区充值后再试"
                % (key, balance))

    def key_names(self):
        """Hub key id -> display name, so a balance row survives a rename."""
        try:
            entries = self.hub.wb_settings.api_keys(self.hub.ACCOUNTS_DIR,
                                                    include_deleted=True)
        except Exception:
            entries = []
        names = {}
        for entry in entries or []:
            key_id = str(entry.get("id") or "")
            if key_id:
                names[key_id] = str(entry.get("name") or key_id)
        # The launcher key (`--api-key`) is a billable key too, but it is not a
        # row in the panel's key list; list it here so it can be funded before
        # enforcement blocks its first turn.
        if str(getattr(self.hub, "API_KEY", "") or ""):
            names.setdefault("launcher", "launcher (--api-key)")
        return names

    def credit_view(self):
        return panel_view(self.config.snapshot(), self.ledger,
                          key_names=self.key_names())

    def grant_credits(self, payload):
        """Apply one operator adjustment from the panel (POST)."""
        if not isinstance(payload, dict):
            raise ValueError("credit adjustment must be a JSON object")
        key_id = payload.get("key_id")
        if not isinstance(key_id, str) or not key_id.strip():
            raise ValueError("key_id must name one configured API key")
        amount = payload.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) \
                or not math.isfinite(float(amount)) or abs(float(amount)) > 1e9:
            raise ValueError("amount must be a finite number of credits")
        note = payload.get("note") or ""
        if not isinstance(note, str) or len(note) > 200:
            raise ValueError("note must be a string of at most 200 characters")
        self.ledger.grant(key_id.strip(), float(amount), note=note)
        return self.credit_view()

    @contextmanager
    def request(self, handler, media=False):
        previous = getattr(self.local, "handler", None)
        previous_media = getattr(self.local, "codearts_media", False)
        self.local.codearts_media = media
        previous_stream = getattr(self.local, "codearts_stream", False)
        previous_fallback = getattr(self.local, "vision_fallback", None)
        self.local.handler = handler
        try:
            yield
        finally:
            self.local.handler = previous
            self.local.codearts_stream = previous_stream
            self.local.codearts_media = previous_media
            self.local.vision_fallback = previous_fallback

    def open(self, payload, session_key=None, **kwargs):
        hub = self.hub
        try:
            self.config.ready()
        except ValueError as exc:
            raise hub.PinnedAccountError(503, str(exc)) from None
        # 本请求已回退过（回退后的流断重连 reopen 场景），直接走 workbuddy 通道。
        # 此时 pin_account 是上次回退得到的 wb 真实账号，不能进下面的 service_pool
        # 白名单检查，也不再向 CodeArts 多付一次 406 往返。
        if getattr(self.local, "vision_fallback", None):
            return self._vision_fallback(payload, session_key=session_key, **kwargs)
        # Local credit floor: only a billed key is stopped here, and only when
        # enforcement is on. The check runs before the pinned-account rules so a
        # key without credits never reaches the CodeArts backend at all.
        reason = self.credits_block_reason()
        if reason:
            raise hub.PinnedAccountError(402, reason)
        if hub.pinned_account() is not None or kwargs.get("pin_account") not in (None, self.service_pool):
            raise hub.PinnedAccountError(403, "CodeArts cannot use a pinned WorkBuddy account")
        if kwargs.get("allowed_accounts") is not None:
            raise hub.PinnedAccountError(403, "WorkBuddy account-restricted keys cannot access the CodeArts service pool")
        handler = getattr(self.local, "handler", None)
        if handler and handler._key_realm():
            raise hub.PinnedAccountError(403, "WorkBuddy realm-bound keys cannot access CodeArts")
        conversation = payload.get("conversation_id") or session_key
        if conversation:
            scope = (handler._key_id() or "panel") if handler else "internal"
            conversation = "hub-ca-" + hashlib.sha256((scope + ":" + str(conversation)).encode()).hexdigest()
        self.local.codearts_stream = True
        try:
            response = self.client.chat(payload, conversation)
        except urllib.error.HTTPError as exc:
            # 上游把「模型不支持多模态」映射成 400；body 会被 http_error_detail
            # 缓存到 exc.wb_body，不匹配时原样 raise，dispatch 的 read_http_error
            # 仍能拿到完整 body。
            if not self._vision_fallback_ok(payload, exc):
                raise
            return self._vision_fallback(payload, session_key=session_key, **kwargs)
        return response, self.service_pool, payload.get("reasoning_effort")

    def _vision_fallback_ok(self, payload, exc):
        """仅三个条件同时成立才回退：开关开 + 请求含图 + 上游 406 多模态拒绝。"""
        if exc.code not in (400, 406):          # Go overlay 映射为 400；406 兜底防御
            return False
        cfg = self.config.snapshot()            # 冷路径，读最新配置（面板改完即生效）
        if not cfg.get("vision_fallback_enabled"):
            return False
        if not has_image(payload):
            return False
        detail = self.hub.http_error_detail(exc)
        return "001001020.406" in (detail or "")

    def _vision_fallback(self, payload, session_key=None, **kwargs):
        cfg = self.config.snapshot()
        target = cfg.get("vision_fallback_model") or "deepseek-v4.1-flash"
        original_model = payload.get("model")
        # 客户端可控的 model 可能含 CR/LF；直接拼进响应头或日志会造成头注入/日志注入。
        # 取首个控制字符之前的安全前缀（见 _safe_fallback_token），标记与日志都用它。
        safe_from = _safe_fallback_token(original_model)
        safe_to = _safe_fallback_token(target)
        fb = dict(payload)                      # 浅拷贝：messages（含图片 parts）原样复用
        fb["model"] = target
        # conversation_id 保留客户端原值（wb 原生协议字段）；session_key 透传，
        # wb 原生会话亲和照常工作。prebuilt_body 是 codeart 形态的 body，剔除后
        # 让原生 build_upstream_body 对 target 重建。
        kwargs = dict(kwargs)
        kwargs.pop("prebuilt_body", None)
        if kwargs.get("pin_account") is self.service_pool:
            kwargs["pin_account"] = None        # service_pool 不是 wb 账号，不能进原生 pin 逻辑
        # 标记先行：即使 wb 侧失败，错误响应（_error→_json）也带上
        # X-WB-Vision-Fallback，客户端能看到「尝试过回退且失败」。
        self.local.vision_fallback = {"from": safe_from, "to": safe_to}
        self.hub.log("codearts vision fallback: %s rejected images (406), retrying on workbuddy '%s'"
                     % (safe_from, safe_to), level="WARN")
        resp, account, effort = self.fallback_open(fb, session_key=session_key, **kwargs)
        wrapped = _AliasStream(resp, original_model)     # chunk model 重写回客户端别名
        wrapped._wb_billed_model = target                # record_upstream_usage 据此记账
        return wrapped, account, effort

    def summary(self):
        rows = []
        for line in self.hub._tail_lines(self.hub.USAGE_LOG, 5000):
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            if row.get("provider") == "codearts" or is_codearts(row.get("model")):
                rows.append(row)
        return {
            "sample_limit": 5000,
            "requests": len(rows),
            "errors": sum(bool(r.get("error")) for r in rows),
            "total_tokens": sum(int(r.get("total_tokens") or 0) for r in rows),
            "recent": list(reversed(rows[-20:])),
        }


def install(hub, config_path):
    """Install once before starting Hub; fail explicitly when its seams change."""
    if hasattr(hub, "_codearts_bridge"):
        raise RuntimeError("CodeArts bridge is already installed in this process")
    for name in ("Handler", "open_upstream", "open_upstream_with_fallback", "build_upstream_body", "_persist_usage", "_tail_lines", "http_error_detail"):
        if not callable(getattr(hub, name, None)):
            raise RuntimeError("incompatible Hub: missing " + name)
    if "allowed_accounts" not in inspect.signature(hub.open_upstream).parameters:
        raise RuntimeError("incompatible Hub: open_upstream account restrictions are unavailable")
    runtime = BridgeRuntime(hub, config_path)
    from .compat import install_heartbeat_compat
    install_heartbeat_compat(hub, runtime)
    install_responses_detail(hub, runtime)
    original_open = hub.open_upstream
    original_fallback = hub.open_upstream_with_fallback
    original_body = hub.build_upstream_body
    original_persist = hub._persist_usage
    native_handler = hub.Handler
    # patch 前的原生 open_upstream_with_fallback：调用它不会递归回桥。
    runtime.fallback_open = original_fallback

    def open_upstream(payload, *args, **kwargs):
        if not is_codearts(payload.get("model")):
            return original_open(payload, *args, **kwargs)
        return runtime.open(payload, *args, **kwargs)

    def open_with_fallback(payload, *args, **kwargs):
        if not is_codearts(payload.get("model")):
            return original_fallback(payload, *args, **kwargs)
        return runtime.open(payload, *args, **kwargs)

    def build_body(payload):
        if not is_codearts(payload.get("model")):
            return original_body(payload)
        body = copy.deepcopy(payload)
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        return body

    def persist(row, fail_label):
        if is_codearts(row.get("model")):
            row["provider"] = "codearts"
            row["realm"] = "codearts"
            row["upstream_model"] = upstream_model(row["model"])
            row["account"] = "codearts:pool"
            # Local credit billing: charge from the row the Hub is about to
            # append. A ledger failure must not turn a finished turn into an
            # error, so it is logged instead of raised.
            try:
                runtime.charge_usage_row(row)
            except Exception as exc:
                hub.log("codearts credits: charge failed: %s" % exc, level="ERROR")
        elif is_codearts(row.get("requested_model")):
            # 回退行：record_usage 已写 requested_model=<codeart 别名>，model 是
            # wb 目标模型。不加 provider="codearts"——计量归实际服务的 wb 通道，
            # realm 由 record_usage 按 wb 账号写实。
            row["vision_fallback"] = True
        return original_persist(row, fail_label)

    class CodeArtsHandler(native_handler):
        def _cross_realm_error(self, model, realm):
            if not is_codearts(model):
                return super()._cross_realm_error(model, realm)
            if hub.pinned_account() is not None:
                return "CodeArts cannot use a pinned WorkBuddy account"
            explicit = super()._request_realm()
            if explicit and explicit not in ("codearts", "all"):
                return "CodeArts cannot use a WorkBuddy realm-bound key or explicit WorkBuddy exit"
            return ""

        def _extra_response_headers(self):
            mark = getattr(runtime.local, "vision_fallback", None)
            if not mark:
                return None
            # 纵深防御：标记在 _vision_fallback 已清洗一次，这里再压一遍，确保
            # 注入到 HTTP 头的值绝不含 CR/LF。
            return {"X-WB-Vision-Fallback": "%s;via=workbuddy:%s" % (
                _safe_fallback_token(mark.get("from")), _safe_fallback_token(mark.get("to")))}

        def _dispatch_chat_post(self, path, payload):
            if is_codearts(payload.get("model")):
                try:
                    validate_payload(payload)
                except ValueError as exc:
                    return self._error(400, str(exc), "invalid_request_error")
            with runtime.request(self):
                return super()._dispatch_chat_post(path, payload)

        def _handle_responses(self, payload):
            media = is_codearts(payload.get("model"))
            if media:
                try:
                    validate_payload(payload)
                except ValueError as exc:
                    return self._error(400, str(exc), "invalid_request_error")
            with runtime.request(self, media=media):
                return super()._handle_responses(payload)

        def _handle_messages(self, payload):
            if is_codearts(payload.get("model")):
                try:
                    validate_payload(payload)
                except ValueError as exc:
                    return self._anthropic_error(400, str(exc))
            with runtime.request(self):
                return super()._handle_messages(payload)

        def _get_v1_models(self):
            if self._request_realm() == "codearts":
                if not self._authorized():
                    return
                if self._key_realm() or self._key_allowed_accounts() is not None or hub.pinned_account():
                    return self._error(403, "this key or pinned account cannot access CodeArts")
                try:
                    data = [m for m in runtime.client.models()
                            if hub.wb_settings.key_allows_model(self.key_entry, m["id"])]
                    return super()._json(200, {"object": "list", "data": data, "realm": "codearts"})
                except Exception as exc:
                    return self._bridge_error(exc)
            return super()._get_v1_models()

        def _agents_models(self):
            models = super()._agents_models()
            try:
                extra = runtime.client.models()
            except Exception:
                extra = []
            known = {m.get("id") for m in models}
            return models + [m for m in extra if m["id"] not in known]

        def _json(self, code, obj):
            path = urlsplit(getattr(self, "path", "")).path
            explicit = self._request_realm() if path in ("/v1/models", "/models") else None
            if (code == 200 and path in ("/v1/models", "/models") and
                    isinstance(obj, dict) and isinstance(obj.get("data"), list) and
                    (explicit in (None, "all") or self._panel_ok()) and not self._key_realm() and
                    self._key_allowed_accounts() is None and not hub.pinned_account()):
                try:
                    extra = runtime.client.models()
                except Exception as exc:
                    extra = []
                    runtime.client.last_error = type(exc).__name__
                known = {m.get("id") for m in obj["data"]}
                extra = [m for m in extra if m["id"] not in known and
                         hub.wb_settings.key_allows_model(self.key_entry, m["id"])]
                obj = {**obj, "data": obj["data"] + extra}
            return super()._json(code, obj)

        def _send_asset(self, body, content_type, etag=None):
            if etag and hub._if_none_match_hit(self.headers.get("If-None-Match", ""), etag):
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache" if etag else "no-store")
            if etag:
                self.send_header("ETag", etag)
            self.end_headers()
            self.wfile.write(body)

        def _dashboard(self):
            body = Path(hub.DASHBOARD_HTML).read_bytes()
            language = hub.wb_settings.ui_language(hub.ACCOUNTS_DIR)
            body = body.replace(b'data-ui-language="__WB_UI_LANGUAGE__"',
                                ('data-ui-language="%s"' % language).encode())
            # Capture before native early-paint validation discards an unknown
            # tab. The deferred extension later registers its own tab.
            early = (b'<script>try{window.__CODEARTS_REQUESTED__='
                     b'new URL(location.href).searchParams.get("tab")==="codearts"||'
                     b'(!new URL(location.href).searchParams.has("tab")&&'
                     b'localStorage.getItem("wb-proxy-main-tab")==="codearts");}catch(e){}</script>')
            body = body.replace(b"<head>", b"<head>" + early, 1)
            body = body.replace(b"</body>", b'<link rel="stylesheet" href="/codearts/dashboard.css"><script src="/codearts/login.js" defer></script><script src="/codearts/dashboard.js" defer></script></body>')
            etag = '"codearts-%s"' % hashlib.sha256(body).hexdigest()[:24]
            return self._send_asset(body, "text/html; charset=utf-8", etag)

        def _bridge_error(self, exc):
            status = exc.code if isinstance(exc, urllib.error.HTTPError) else (503 if isinstance(exc, ValueError) else 502)
            # Do not relay arbitrary backend error bodies or credential-bearing configuration.
            message = str(exc) if isinstance(exc, ValueError) else "CodeArts backend request failed (%s)" % status
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            return self._error(status, message, "invalid_request_error")

        def _same_origin(self):
            if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
                return False
            origin = self.headers.get("Origin")
            if not origin:
                return True  # Header-only panel token also supports local API clients.
            parsed = urlsplit(origin)
            return parsed.scheme in ("http", "https") and parsed.netloc.lower() == self.headers.get("Host", "").lower()

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in ("/codearts/dashboard.js", "/codearts/login.js", "/codearts/dashboard.css"):
                body = (Path(__file__).parent / path.rsplit("/", 1)[-1]).read_bytes()
                return self._send_asset(body, "text/css; charset=utf-8" if path.endswith(".css") else "text/javascript; charset=utf-8")
            if not path.startswith("/codearts/"):
                return super().do_GET()
            if not self._panel_ok():
                return self._error(401, "panel password required", "invalid_request_error")
            try:
                if path == "/codearts/config":
                    return self._json(200, runtime.config.view())
                if path == "/codearts/overview":
                    return self._json(200, runtime.client.overview())
                if path == "/codearts/models":
                    return self._json(200, {"data": runtime.client.models()})
                if path == "/codearts/credits":
                    return self._json(200, runtime.credit_view())
                if path == "/codearts/usage":
                    return self._json(200, runtime.summary())
            except Exception as exc:
                return self._bridge_error(exc)
            return self._error(404, "not found", "invalid_request_error")

        def do_POST(self):
            path = urlsplit(self.path).path
            if not path.startswith("/codearts/"):
                return super().do_POST()
            if not self._panel_ok():
                return self._error(401, "panel password required", "invalid_request_error")
            if not self._same_origin():
                return self._error(403, "cross-origin CodeArts management is forbidden", "invalid_request_error")
            action = path[len("/codearts/admin/"):] if path.startswith("/codearts/admin/") else None
            if path not in ("/codearts/config", "/codearts/models/refresh",
                            "/codearts/credits/grant") and action not in ADMIN_ACTIONS:
                return self._error(404, "not found", "invalid_request_error")
            payload = self._payload_or_error()
            if payload is None:
                return
            try:
                if path == "/codearts/config":
                    result = runtime.config.update(payload)
                    runtime.client.invalidate()
                elif path == "/codearts/models/refresh":
                    runtime.client.invalidate()
                    result = {"data": runtime.client.models()}
                elif path == "/codearts/credits/grant":
                    result = runtime.grant_credits(payload)
                else:
                    result = runtime.client.admin(action, payload)
                return self._json(200, result)
            except Exception as exc:
                return self._bridge_error(exc)

    hub.open_upstream = open_upstream
    hub.open_upstream_with_fallback = open_with_fallback
    hub.build_upstream_body = build_body
    hub._persist_usage = persist
    hub.Handler = CodeArtsHandler
    hub._codearts_bridge = runtime
    return runtime
