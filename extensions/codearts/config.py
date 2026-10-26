"""Independent bridge settings. No writes to Hub's settings or credentials."""
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import urlsplit, urlunsplit


DEFAULTS = {
    "enabled": False,
    "base_url": "http://127.0.0.1:7866",
    "api_key": "",
    "api_key_env": "CODEARTS_API_KEY",
    "timeout_seconds": 120,
    "models_ttl_seconds": 60,
    "catalog_models": [],
    "catalog_include_images": False,
    "vision_fallback_enabled": False,
    "vision_fallback_model": "deepseek-v4.1-flash",
}


class BridgeConfig:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def _load(self):
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            stored = {}
        if not isinstance(stored, dict):
            raise ValueError("CodeArts configuration must be a JSON object")
        return self._validate({**DEFAULTS, **stored})

    @staticmethod
    def _validate(data):
        if set(data) - set(DEFAULTS):
            raise ValueError("unknown CodeArts setting")
        if not isinstance(data["enabled"], bool):
            raise ValueError("enabled must be true or false")
        if not isinstance(data["catalog_include_images"], bool):
            raise ValueError("catalog_include_images must be true or false")
        selection = data["catalog_models"]
        if (not isinstance(selection, list) or any(not isinstance(mid, str) or
                not mid.strip() or mid != mid.strip() for mid in selection)):
            raise ValueError("catalog_models must be a list of exact upstream model IDs")
        data["catalog_models"] = list(dict.fromkeys(selection))
        for key in ("base_url", "api_key", "api_key_env"):
            if not isinstance(data[key], str):
                raise ValueError(key + " must be a string")
            data[key] = data[key].strip()
        url = urlsplit(data["base_url"])
        if (url.scheme not in ("http", "https") or not url.hostname or
                url.username is not None or url.password is not None or url.query or url.fragment):
            raise ValueError("base_url must be an HTTP(S) service URL without credentials, query or fragment")
        try:
            url.port
        except ValueError:
            raise ValueError("base_url has an invalid port") from None
        path = url.path.rstrip("/")
        if path.endswith("/v1"):
            path = path[:-3]
        data["base_url"] = urlunsplit((url.scheme, url.netloc, path, "", ""))
        if data["api_key_env"] and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", data["api_key_env"]):
            raise ValueError("api_key_env must be an environment variable name")
        if any(c in data["api_key"] for c in "\r\n\x00"):
            raise ValueError("api_key must be a single line")
        for key in ("timeout_seconds", "models_ttl_seconds"):
            value = data[key]
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3600:
                raise ValueError(key + " must be an integer from 1 to 3600")
        if not isinstance(data["vision_fallback_enabled"], bool):
            raise ValueError("vision_fallback_enabled must be true or false")
        model = data["vision_fallback_model"]
        if not isinstance(model, str) or not model.strip() or model != model.strip():
            raise ValueError("vision_fallback_model must be a non-empty upstream model id")
        data["vision_fallback_model"] = model
        if model.startswith(("codeart/", "codearts/")):
            raise ValueError("vision_fallback_model must be a WorkBuddy-native model, not a bridge alias")
        return data

    def snapshot(self):
        with self.lock:
            data = self._load()
            data["api_key"] = os.environ.get(data["api_key_env"], "").strip() or data["api_key"]
            if any(c in data["api_key"] for c in "\r\n\x00"):
                raise ValueError("CodeArts environment key must be a single line")
            return data

    def ready(self):
        data = self.snapshot()
        if not data["enabled"]:
            raise ValueError("CodeArts bridge is disabled; enable it in the CodeArts panel")
        if not data["api_key"]:
            raise ValueError("CodeArts backend API key is missing")
        return data

    def view(self):
        data = self.snapshot()
        data["has_api_key"] = bool(data.pop("api_key"))
        data["model_prefix"] = "codeart/"
        return data

    def update(self, patch):
        if not isinstance(patch, dict):
            raise ValueError("settings must be a JSON object")
        if set(patch) - (set(DEFAULTS) | {"clear_api_key"}):
            raise ValueError("unknown CodeArts setting")
        patch = dict(patch)
        clear = patch.pop("clear_api_key", False)
        if not isinstance(clear, bool):
            raise ValueError("clear_api_key must be true or false")
        with self.lock:
            current = self._load()
            if patch.get("api_key") == "":
                patch.pop("api_key")
            if clear:
                patch["api_key"] = ""
            data = self._validate({**current, **patch})
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".codearts-", dir=str(self.path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(data, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return self.view()
