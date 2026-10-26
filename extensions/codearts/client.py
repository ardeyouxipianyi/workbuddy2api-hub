"""The only outbound boundary: OpenAI SSE and allowlisted CodeArts administration."""
import copy
import http.client
import json
import threading
import time
import urllib.error
import urllib.request

PREFIX = "codeart/"
LEGACY_PREFIX = "codearts/"
ADMIN_ACTIONS = frozenset((
    "credits", "checkin", "keepalive", "reload", "accounts/enable",
    "accounts/disable", "accounts/clear-cooldown", "oauth/start", "oauth/poll",
    "oauth/import-callback",
))
CHAT_FIELDS = frozenset((
    "model", "messages", "stream", "stream_options", "tools", "tool_choice",
    "max_tokens", "max_completion_tokens", "temperature", "top_p",
    "reasoning_effort", "conversation_id",
))


def is_codearts(model):
    return isinstance(model, str) and model.startswith((PREFIX, LEGACY_PREFIX))


def upstream_model(model):
    for prefix in (PREFIX, LEGACY_PREFIX):
        if isinstance(model, str) and model.startswith(prefix):
            return model[len(prefix):]
    raise ValueError("CodeArts model must be codeart/<upstream model id>")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ModelStream:
    """Rewrite model aliases incrementally; never buffer the complete response."""
    def __init__(self, response, model):
        self.response = response
        self.model = model
        self.pending = b""
        self.headers = response.headers
        self._wb_billed_model = model
        self.stream_error = None
        self.error_event = False
        self.done = False

    def __iter__(self):
        while True:
            block = self.read1()
            if not block:
                return
            yield from block.splitlines(keepends=True)

    def read1(self, size=65536):
        if self.stream_error is not None:
            raise self.stream_error
        while True:
            chunk = self.response.read1(size)
            self.pending += chunk
            if len(self.pending) > 8 * 1024 * 1024:
                raise ValueError("CodeArts SSE event exceeds 8 MiB")
            if not chunk:
                if getattr(self.response, "length", 0):
                    raise http.client.IncompleteRead(b"", self.response.length)
                if not self.pending:
                    if not self.done:
                        raise OSError("CodeArts stream ended without [DONE]")
                    return b""
                head, self.pending = self.pending, b""
            elif b"\n" in self.pending:
                head, _, self.pending = self.pending.rpartition(b"\n")
            else:
                continue
            out = []
            for line in head.split(b"\n"):
                if line.startswith(b"event:"):
                    self.error_event = line[6:].strip() == b"error"
                if line.startswith(b"data:") and line[5:].strip() == b"[DONE]":
                    self.done = True
                if line.startswith(b"data:") and line[5:].strip().startswith(b"{"):
                    obj = json.loads(line[5:].strip())
                    if self.error_event or (isinstance(obj, dict) and obj.get("error")):
                        self.pending = b""
                        self.stream_error = OSError("CodeArts reported an upstream stream error")
                        if out:
                            return b"".join(out)
                        raise self.stream_error
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


class CodeArtsClient:
    def __init__(self, config):
        self.config = config
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.lock = threading.RLock()
        self.cached_models = []
        self.cache_at = 0
        self.cache_identity = None
        self.last_error = ""

    def invalidate(self):
        with self.lock:
            self.cached_models = []
            self.cache_at = 0
            self.cache_identity = None

    def _open(self, path, payload=None, timeout=None):
        cfg = self.config.ready()
        raw = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(cfg["base_url"] + path, data=raw, headers={
            "Authorization": "Bearer " + cfg["api_key"],
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if path == "/v1/chat/completions" else "application/json",
        })
        return self.opener.open(req, timeout=timeout or cfg["timeout_seconds"])

    def _json(self, path, payload=None, timeout=None):
        with self._open(path, payload, timeout) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("CodeArts JSON response exceeds 8 MiB")
            return json.loads(raw)

    def models(self):
        cfg = self.config.ready()
        identity = (cfg["base_url"], cfg["api_key"], tuple(cfg["catalog_models"]), cfg["catalog_include_images"])
        with self.lock:
            if self.cache_identity == identity and time.monotonic() - self.cache_at < cfg["models_ttl_seconds"]:
                return copy.deepcopy(self.cached_models)
            obj = self._json("/v1/models", timeout=min(cfg["timeout_seconds"], 10))
            if not isinstance(obj, dict) or not isinstance(obj.get("data"), list):
                raise ValueError("CodeArts returned an invalid model list")
            models = []
            seen = set()
            for entry in obj["data"]:
                if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"].strip():
                    continue
                if (cfg["catalog_models"] and entry["id"] not in cfg["catalog_models"] and
                        not (cfg["catalog_include_images"] and entry.get("supports_images") is True
                             and not entry.get("capability_conflict"))):
                    continue

                # 后端为每个真实模型登记「注册 ID + 展示名」两条可调用条目，二者
                # registered_id 相同。以 registered_id 为规范键去重，使每个真实模型
                # 只保留一行；首个出现者胜出（后端顺序为先 ID 后 Name，故天然留下
                # 规范 ID 那条）。缺 registered_id 的条目回填为 real，供下游复用。
                # 仅接受非空字符串 registered_id；非字符串/空值一律回退到 id，避免
                # PREFIX + real 因类型不符抛 TypeError（上游畸形数据不应击穿本方法）。
                registered = entry.get("registered_id")
                real = registered if isinstance(registered, str) and registered.strip() else entry["id"]
                mid = PREFIX + real
                if mid in seen:
                    continue
                seen.add(mid)
                models.append({**entry, "id": mid, "registered_id": real,
                               "owned_by": "codearts", "provider": "codearts", "realm": "codearts"})
            if cfg["catalog_models"]:
                indexed = {entry["id"]: entry for entry in models}
                selected = []
                for want in cfg["catalog_models"]:
                    hit = indexed.get(PREFIX + want)
                    if hit is None:
                        # 用户可能配置展示名（如 DeepSeek-V4.1-Flash）：规范 id 精确未命中
                        # 时，在**已通过上方大小写敏感 catalog 过滤**的条目中做大小写不敏感
                        # 回退匹配注册 id 或展示名。精确优先，避免把同名大小写别名误判为同一
                        # 模型。注意：与任何上游原始 id 都大小写不同的配置在过滤阶段即被丢弃，
                        # 走不到此处，会退化为占位（已知限制）。
                        low = want.lower()
                        hit = next((entry for entry in models
                                    if entry["id"][len(PREFIX):].lower() == low
                                    or (entry.get("name") or "").lower() == low), None)
                    if hit is None:
                        hit = {
                            "id": PREFIX + want, "object": "model", "owned_by": "codearts",
                            "provider": "codearts", "realm": "codearts", "backend_listed": False,
                            "availability_note": "Configured selection; not listed by backend",
                        }
                    selected.append(hit)
                registered = {m.get("registered_id", m["id"][len(PREFIX):]) for m in selected}
                if cfg["catalog_include_images"]:
                    for entry in models:
                        real = entry.get("registered_id", entry["id"][len(PREFIX):])
                        if real in registered or entry.get("supports_images") is not True or entry.get("capability_conflict"):
                            continue
                        registered.add(real)
                        selected.append({**entry, "id": PREFIX + real})
                models = selected
            self.cached_models = models
            self.cache_at = time.monotonic()
            self.cache_identity = identity
            self.last_error = ""
            return copy.deepcopy(models)

    def chat(self, payload, session_key=None):
        model = payload.get("model")
        if not is_codearts(model) or not upstream_model(model).strip():
            raise ValueError("CodeArts model must be codeart/<upstream model id>")
        body = {key: copy.deepcopy(value) for key, value in payload.items() if key in CHAT_FIELDS}
        body["model"] = upstream_model(model)
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        # Hub's Responses translator deliberately leaves ordinary functions in
        # the flat Responses shape. CodeArts expects Chat's nested function
        # object. Only normalize this protocol boundary; never use WorkBuddy's
        # prompt sanitizer or its string-only named-tool-choice conversion.
        if isinstance(body.get("tools"), list):
            tools = []
            for tool in body["tools"]:
                if isinstance(tool, dict) and "name" in tool and "function" not in tool:
                    tool = {"type": "function", "function": {k: v for k, v in tool.items() if k != "type"}}
                tools.append(tool)
            body["tools"] = tools
        choice = body.get("tool_choice")
        if isinstance(choice, dict) and choice.get("type") == "function" and "function" not in choice:
            body["tool_choice"] = {"type": "function", "function": {"name": choice.get("name", "")}}
        if session_key:
            body["conversation_id"] = session_key
        response = self._open("/v1/chat/completions", body)
        if "text/event-stream" not in response.headers.get("Content-Type", "").lower():
            response.close()
            raise ValueError("CodeArts returned a non-SSE response to a streaming request")
        return ModelStream(response, model)

    def overview(self):
        return self._json("/admin/api/overview")

    def admin(self, action, payload):
        if action not in ADMIN_ACTIONS:
            raise ValueError("unsupported CodeArts action")
        return self._json("/admin/api/" + action, payload)
