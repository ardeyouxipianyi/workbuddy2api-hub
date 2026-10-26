"""Panel routes for one-click client configuration (wb_agents).

`AgentRoutes` is a mixin of wb_proxy.Handler: detect installed clients, pick
the key / models a client is written with, apply and restore its config files.
"""

import os
import re

import wb_catalog
import wb_modelsdev
import wb_runtime
import wb_settings

_gw = wb_runtime.gateway


class AgentRoutes(object):
    def _agents_client_allowed(self):
        """本请求的来源地址能不能用一键配置（判定见模块级 agents_client_allowed）。"""
        return _gw().agents_client_allowed(getattr(self, "client_address", None))

    def _agents_base_url_hint(self):
        """Best guess at the URL a local client should point at.

        Derived from the bound socket; a 0.0.0.0 bind is unreachable for a
        client, so it is reported as 127.0.0.1 instead.
        """
        host, port = "", 0
        try:
            host, port = self.server.server_address[:2]
        except Exception:
            pass
        host = str(host or "").strip() or "127.0.0.1"
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        return "http://%s:%d/v1" % (host, int(port or 0))

    def _agents_models(self):
        """Flat model list for the picker; bundled intl+cn catalog, deduped.

        Live sources stay out of this path on purpose: the panel must answer
        even when the upstream is down, so a failure here is simply a shorter
        list, never an error.
        """
        models = []
        seen = set()
        try:
            try:
                entries = _gw().fetch_models()
            except Exception:
                entries = []
            if entries:
                sources = ((mid, meta) for mid, meta in entries)
            else:
                sources = (
                    (item.get("id"), item)
                    for item in (list(getattr(wb_catalog, "STATIC_INTL_MODELS", []))
                                 + list(getattr(wb_catalog, "STATIC_CN_MODELS", [])))
                    if isinstance(item, dict)
                )
            for mid, meta in sources:
                mid = str(mid or "").strip()
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                meta = meta if isinstance(meta, dict) else {}
                entry = {"id": mid}
                # 与 /v1/models 走同一条查找链（上游值 > 内建知识表 > 本地缓存 >
                # 默认）：否则一键配置写进客户端的窗口会和网关自己宣告的对不上
                # （目录里的 contextWindow.defaultLength 只是桌面端的默认档，
                # deepseek-v4.1-flash 是 300k，而网关宣告的是 1M）。
                try:
                    ctx, out, _src = wb_modelsdev.lookup(
                        mid, meta.get("maxInputTokens"),
                        meta.get("maxOutputTokens"), directory=_gw().ACCOUNTS_DIR)
                except Exception:
                    ctx = meta.get("maxInputTokens")
                    out = meta.get("maxOutputTokens")
                if not ctx:
                    window = meta.get("contextWindow")
                    if isinstance(window, dict):
                        lengths = window.get("supportedLengths") or []
                        ctx = window.get("defaultLength") or (
                            lengths[-1] if lengths else None)
                if ctx:
                    try:
                        entry["context_window"] = int(ctx)
                    except (TypeError, ValueError):
                        pass
                if out:
                    try:
                        entry["max_output"] = int(out)
                    except (TypeError, ValueError):
                        pass
                name = meta.get("name")
                if name:
                    entry["name"] = name
                # Vision support flag (DSH and clients use this)
                vision = bool(meta.get("supportsImages")) and not meta.get("disabledMultimodal")
                if mid == "glm-5.3-flash":
                    vision = False
                if vision:
                    entry["supports_vision"] = True
                # 可选的思考档位交给客户端（DSH 用它生成会话里的档位选择器）。
                # 只宣告目录明确列出档位的模型：固定档位的模型（只有
                # reasoning.effort、没有 supportedEfforts）不列，选了也不会改变
                # 上游实际跑的档位。
                efforts = (meta.get("reasoning") or {}).get("supportedEfforts")
                if isinstance(efforts, (list, tuple)):
                    levels = [str(e).strip() for e in efforts
                              if str(e or "").strip()]
                    if levels:
                        entry["reasoning_efforts"] = levels
                models.append(entry)
        except Exception:
            pass
        return models

    def _get_agents_cli(self):
        """Serve wb_agents.py so a remote machine can fetch the configurator.

        No panel session and no loopback check on purpose: the callers are
        other machines, which is the whole point of the CLI (it writes the
        config files on the machine it runs on). The file carries no secrets
        - it is byte-identical to the copy in the repository.
        """
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "wb_agents.py")
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError as exc:
            return self._error(500, "wb_agents.py unavailable: %s" % exc)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition",
                         'inline; filename="wb_agents.py"')
        self.send_header("Content-Length", str(len(body)))
        if _gw().cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
        return

    def _get_agents(self):
        if not self._agents_client_allowed():
            # 远程看板（服务端 / Docker / OpenWrt）：一键配置改的是网关所在
            # 机器的配置目录，到不了用户自己的电脑，直接按不可用返回。
            return self._json(200, {
                "enabled": False,
                "reason": "agent config is only available from the machine running the gateway",
            })
        wb_agents = _gw().agents_module()
        keys = []
        try:
            for entry in _gw().configured_keys():
                if entry.get("enabled"):
                    keys.append({
                        "id": entry.get("id"),
                        "name": entry.get("name") or "",
                        "enabled": True,
                    })
        except Exception:
            keys = []
        selection = wb_settings.agent_model_selection(_gw().ACCOUNTS_DIR)
        return self._json(200, {
            "enabled": True,
            "clients": list(wb_agents.overview(_gw().ACCOUNTS_DIR).values()),
            "models": self._agents_models(),
            # 「网关模型清单」里勾选的模型：一键配置只把这些写进客户端。
            # None（从未保存）时给空列表，由 models_configured 区分「没选过」
            # 与「选了 0 个」——前者 apply 退回完整目录，后者 apply 拒绝。
            "selected_models": selection or [],
            "models_configured": selection is not None,
            # 哪些客户端会把模型清单写进自身配置（前端据此决定「一个都没勾」
            # 时要不要在发请求前拦下来；Claude Code / Codex 不读清单）。
            "model_embedding_clients": sorted(_gw().MODEL_EMBEDDING_CLIENTS),
            "keys": keys,
            "global_key_set": bool(_gw().API_KEY),
            "auth_required": _gw().auth_required(),
            "gateway": {"base_url": self._agents_base_url_hint()},
        })

    def _get_agents_model_selection(self):
        """面板在「网关模型清单」勾选的模型（一键配置写进客户端的清单）。"""
        selection = wb_settings.agent_model_selection(_gw().ACCOUNTS_DIR)
        return self._json(200, {
            "configured": selection is not None,
            "selected_models": selection or [],
        })

    def _agents_resolve_key(self, payload, warnings):
        """Pick the gateway key to hand to the client, per the payload.

        Returns the key string, or None when the request should fail. The
        failure message is appended to `warnings` only for the soft-fallback
        case; hard failures raise via the caller's 400 mapping.
        """
        wb_agents = _gw().agents_module()
        key_id = str(payload.get("key_id") or "").strip()
        if key_id == "__global":
            return _gw().API_KEY or None
        keys = _gw().configured_keys()
        if key_id:
            for entry in keys:
                if entry.get("id") == key_id:
                    return entry.get("key") or None
            raise wb_agents.AgentConfigError(
                "no configured key with id %r" % key_id)
        # Default: the global key when set, else the first enabled panel key.
        if _gw().API_KEY:
            return _gw().API_KEY
        for entry in keys:
            if entry.get("enabled"):
                return entry.get("key") or None
        return None

    def _handle_agents_apply(self, payload):
        wb_agents = _gw().agents_module()
        client_id = str(payload.get("client") or payload.get("client_id") or "").strip()
        if not client_id:
            return self._error(400, "client is required", "invalid_request_error")
        base_url = str(payload.get("base_url") or "").strip()
        if not re.match(r"^https?://", base_url):
            return self._error(
                400, "base_url must start with http:// or https://",
                "invalid_request_error")
        model = str(payload.get("model") or "").strip() or None
        models = payload.get("models")
        if models is None:
            # 前端没带清单（老看板 / 直接调接口）：优先用「网关模型清单」里
            # 勾选的模型；从未保存过勾选（升级前的老安装）才退回完整目录。
            selection = wb_settings.agent_model_selection(_gw().ACCOUNTS_DIR)
            if selection is None:
                models = self._agents_models()
            else:
                models = [{"id": mid} for mid in selection]
        if not isinstance(models, list):
            return self._error(400, "models must be a list",
                               "invalid_request_error")
        # A runaway picker must not turn into a megabyte config file.
        models = models[:80]
        cleaned_models = []
        for item in models:
            if isinstance(item, dict) and item.get("id"):
                cleaned_models.append(item)
            elif isinstance(item, str) and item.strip():
                cleaned_models.append({"id": item.strip()})
        # 看板只送模型 id，而 DSH / Crush 这类客户端要求把规格写进自己的配置
        # （上下文窗口、输出上限、思考档位）。这里按 id 把网关自己的目录规格
        # 补上（见 enrich_agent_models），显式带来的字段优先——否则一键配置
        # 写出来的模型列表只有 id，客户端就只能吃自己的内置默认值。
        try:
            specs = {}
            for spec in self._agents_models():
                if isinstance(spec, dict) and spec.get("id"):
                    specs[str(spec["id"])] = spec
        except Exception:
            specs = {}
        cleaned_models = _gw().enrich_agent_models(cleaned_models, specs)
        if not cleaned_models and client_id in _gw().MODEL_EMBEDDING_CLIENTS:
            return self._error(
                400,
                "请先在「网关模型清单」中勾选至少一个模型：只有勾选的模型"
                "才会写入客户端配置",
                "invalid_request_error")
        warnings = []
        try:
            api_key = self._agents_resolve_key(payload, warnings)
        except wb_agents.AgentConfigError as exc:
            return self._error(400, str(exc), "invalid_request_error")
        if not api_key:
            if _gw().auth_required():
                return self._error(
                    400, "no gateway key available - 请先配置 API Key",
                    "invalid_request_error")
            api_key = "wb-local"
            warnings.append("gateway has no key configured; wrote placeholder "
                            "'wb-local' (auth is off, so any value works)")
        try:
            result = wb_agents.integrate(
                _gw().ACCOUNTS_DIR, client_id, base_url, api_key,
                model=model, models=cleaned_models)
        except wb_agents.AgentConfigError as exc:
            return self._error(400, str(exc), "invalid_request_error")
        except Exception as exc:
            return self._error(500, "agents apply failed: %s" % exc)
        result["warnings"] = warnings
        _gw().log("agents apply: client=%s base_url=%s model=%s files=%d"
            % (client_id, base_url, model or "-", len(result.get("files") or [])),
            tag="agents")
        return self._json(200, result)

    def _handle_agents_restore(self, payload):
        wb_agents = _gw().agents_module()
        client_id = str(payload.get("client") or payload.get("client_id") or "").strip()
        if not client_id:
            return self._error(400, "client is required", "invalid_request_error")
        try:
            result = wb_agents.restore(_gw().ACCOUNTS_DIR, client_id)
        except wb_agents.AgentConfigError as exc:
            return self._error(400, str(exc), "invalid_request_error")
        except Exception as exc:
            return self._error(500, "agents restore failed: %s" % exc)
        _gw().log("agents restore: client=%s files=%d"
            % (client_id, len(result.get("restored") or [])), tag="agents")
        return self._json(200, result)

    def _handle_agents_model_selection(self, payload):
        """保存「网关模型清单」里勾选的模型；空列表是合法输入（全部取消）。"""
        models = payload.get("models")
        if models is None:
            return self._error(400, "models is required", "invalid_request_error")
        if not isinstance(models, list):
            return self._error(400, "models must be a list",
                               "invalid_request_error")
        selection = wb_settings.set_agent_model_selection(_gw().ACCOUNTS_DIR, models)
        _gw().log("agents model selection: %d model(s)" % len(selection),
            tag="agents")
        return self._json(200, {
            "ok": True,
            "configured": True,
            "selected_models": selection,
        })
