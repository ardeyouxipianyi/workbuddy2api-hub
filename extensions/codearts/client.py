"""The only outbound boundary: OpenAI SSE and allowlisted CodeArts administration."""
import copy
import http.client
import json
import threading
import time
import urllib.error
import urllib.request

PREFIX = "codearts/"
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
    return isinstance(model, str) and model.startswith(PREFIX)


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
        identity = (cfg["base_url"], cfg["api_key"])
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
                mid = PREFIX + entry["id"]
                if mid in seen:
                    continue
                seen.add(mid)
                models.append({**entry, "id": mid, "owned_by": "codearts", "provider": "codearts", "realm": "codearts"})
            self.cached_models = models
            self.cache_at = time.monotonic()
            self.cache_identity = identity
            self.last_error = ""
            return copy.deepcopy(models)

    def chat(self, payload, session_key=None):
        model = payload.get("model")
        if not is_codearts(model) or not model[len(PREFIX):].strip():
            raise ValueError("CodeArts model must be codearts/<upstream model id>")
        body = {key: copy.deepcopy(value) for key, value in payload.items() if key in CHAT_FIELDS}
        body["model"] = model[len(PREFIX):]
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
