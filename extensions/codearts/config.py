"""Independent bridge settings. No writes to Hub's settings or credentials."""
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import urlsplit, urlunsplit

from .credits import DEFAULT_NIGHT_WINDOW, clean_multiplier_table, parse_windows
from .spoof_overlay import single_line, validate_header_family


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
    # Local credit billing for `codeart/*` turns (opt-in, default off). Rates
    # come from the CodeArts model picker; the stored tables are overrides on
    # top of the built-in ones (see credits.BUILTIN_MULTIPLIERS), and the night
    # table replaces a model's day rate inside the night window.
    "credits_enabled": False,
    "credits_enforce": True,
    "credits_base_per_1k": 1.0,
    "credits_night_window": DEFAULT_NIGHT_WINDOW,
    "credits_night_multipliers": {},
    "credits_multipliers": {},
    # Build-time opt-in (default off): headers injected into the IDE-shaped chat
    # request, the observed IDE session id, a per-request timestamp, the official
    # per-request x-ot-* family and its x-ot-function mode value.
    "identity_ide_headers": {},
    "identity_ide_user_session_id": "",
    "identity_ide_timestamp": False,
    "identity_ide_ot_family": False,
    "identity_ide_function": "agent-claw",
    "identity_ide_user_agent": "",
    "identity_ide_client_ip": "",
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
        for key in ("credits_enabled", "credits_enforce"):
            if not isinstance(data[key], bool):
                raise ValueError(key + " must be true or false")
        base = data["credits_base_per_1k"]
        if (isinstance(base, bool) or not isinstance(base, (int, float))
                or not math.isfinite(float(base)) or not 0 < float(base) <= 1000000):
            raise ValueError("credits_base_per_1k must be a positive number")
        data["credits_base_per_1k"] = float(base)
        window = data["credits_night_window"]
        if not isinstance(window, str):
            raise ValueError("credits_night_window must be a string such as 23:00-08:00")
        try:
            parse_windows(window)
        except ValueError as exc:
            raise ValueError("credits_night_window " + str(exc)) from None
        data["credits_night_window"] = window.strip()
        data["credits_multipliers"] = clean_multiplier_table(
            data["credits_multipliers"], "credits_multipliers")
        data["credits_night_multipliers"] = clean_multiplier_table(
            data["credits_night_multipliers"], "credits_night_multipliers")
        if not isinstance(data["identity_ide_timestamp"], bool):
            raise ValueError("identity_ide_timestamp must be true or false")
        headers = data["identity_ide_headers"]
        if not isinstance(headers, dict):
            raise ValueError("identity_ide_headers must be a JSON object of header names to values")
        for name, value in headers.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise ValueError("identity_ide_headers must map header names to string values")
        # Reserved names, non-token names, duplicates (case-insensitive) and
        # multi-line values would corrupt the signed request, so reject them
        # when the file is saved instead of at build time.
        data["identity_ide_headers"] = dict(validate_header_family(headers, value_limit=4096))
        session = data["identity_ide_user_session_id"]
        if not isinstance(session, str):
            raise ValueError("identity_ide_user_session_id must be a string")
        session = single_line(session, "identity_ide_user_session_id")
        if len(session) > 4096:
            raise ValueError("identity_ide_user_session_id must be at most 4096 characters")
        data["identity_ide_user_session_id"] = session
        if not isinstance(data["identity_ide_ot_family"], bool):
            raise ValueError("identity_ide_ot_family must be true or false")
        function = data["identity_ide_function"]
        if not isinstance(function, str):
            raise ValueError("identity_ide_function must be a string")
        function = single_line(function, "identity_ide_function")
        if len(function) > 256:
            raise ValueError("identity_ide_function must be at most 256 characters")
        data["identity_ide_function"] = function
        user_agent = data["identity_ide_user_agent"]
        if not isinstance(user_agent, str):
            raise ValueError("identity_ide_user_agent must be a string")
        user_agent = single_line(user_agent, "identity_ide_user_agent")
        if len(user_agent) > 4096:
            raise ValueError("identity_ide_user_agent must be at most 4096 characters")
        data["identity_ide_user_agent"] = user_agent
        client_ip = data["identity_ide_client_ip"]
        if not isinstance(client_ip, str):
            raise ValueError("identity_ide_client_ip must be a string")
        client_ip = single_line(client_ip, "identity_ide_client_ip")
        if len(client_ip) > 64:
            raise ValueError("identity_ide_client_ip must be at most 64 characters")
        data["identity_ide_client_ip"] = client_ip
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
