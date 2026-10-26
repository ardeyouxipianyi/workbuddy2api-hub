"""Panel routes for gateway settings: read, reveal secrets, save.

`SettingsRoutes` is a mixin of wb_proxy.Handler. `/settings/save` takes one
JSON object whose keys map onto the persisted settings in wb_settings; each
key is validated and applied on its own, so a bad field rejects the request
before anything is written. Runtime state (accounts dir, pool, scheduler,
pricing) is read from the running gateway through `_gw()`.
"""

import re
import threading
import time

import wb_identity
import wb_runtime
import wb_settings
import wb_update_request

_gw = wb_runtime.gateway


class SettingsRoutes(object):
    def _get_settings(self):
        if not self._authorized():
            return
        return self._json(200, _gw().runtime_settings_view())

    def _get_settings_reveal(self, query):
        # The panel only ever draws masked keys, so copying one needs an
        # explicit request. Panel session required, API key is not enough.
        if not self._panel_ok():
            return self._error(401, "panel password required", "invalid_request_error")
        wanted = (query.get("id") or [""])[0]
        for entry in _gw().configured_keys():
            if entry.get("id") == wanted:
                return self._json(200, {"id": wanted, "key": entry.get("key") or ""})
        return self._error(404, "no such key", "invalid_request_error")

    def _handle_settings_save(self):
        """Persist panel-managed settings from the web settings tab."""
        payload = self._payload_or_error()
        if payload is None:
            return
        reply = {}
        if "api_keys" in payload:
            raw = payload.get("api_keys")
            if not isinstance(raw, list):
                return self._error(400, "api_keys must be a list", "invalid_request_error")
            # The panel only ever shows a masked key, so a blank value means
            # "keep what is stored" for that row rather than "clear it".
            existing = {entry.get("id"): entry for entry in _gw().configured_keys()}
            # A submission that carries an explicit delete list is an upsert:
            # only the ids it names are retired. That is what stops a stale or
            # incomplete list (a second tab, a save racing the post-save
            # reload) from wiping a key the user never removed. An older panel
            # sends no such field and keeps the replace-by-omission contract.
            delete_ids = payload.get("deleted_api_key_ids")
            upsert = isinstance(delete_ids, list)
            cleaned = []
            for item in raw:
                if not isinstance(item, dict):
                    return self._error(400, "each api key must be an object",
                                       "invalid_request_error")
                entry_id = str(item.get("id") or "").strip()
                stored = existing.get(entry_id) or {}
                value = str(item.get("key") or "").strip()
                if not value and entry_id and entry_id in existing:
                    value = existing[entry_id].get("key") or ""
                # A new row keeps an empty id here; wb_settings mints a random
                # one on write. Deriving it from the row's position reused ids
                # of rows deleted earlier, and two rows sharing an id made
                # /settings/reveal answer with the wrong key.
                if value and len(value) < 4:
                    return self._error(400, "api key must be at least 4 characters",
                                       "invalid_request_error")
                if not value:
                    return self._error(400, "a key entry is empty - fill it in or remove the row",
                                       "invalid_request_error")
                # A field the submission omits keeps whatever is stored, the
                # same rule `models` already follows: an older or partial
                # client must not silently clear a key's exit binding, its
                # name, or its disabled state.
                if "realm" in item:
                    realm = str(item.get("realm") or "").strip().lower()
                else:
                    realm = str(stored.get("realm") or "").strip().lower()
                if realm not in ("", "intl", "cn"):
                    return self._error(400, "realm must be intl, cn or empty",
                                       "invalid_request_error")
                # An older cached panel does not know this field at all, so a
                # row that omits it keeps whatever is stored instead of
                # silently dropping the restriction.
                if "models" in item:
                    models = item.get("models")
                else:
                    models = stored.get("models")
                if "name" in item:
                    name = str(item.get("name") or "").strip()
                else:
                    name = str(stored.get("name") or "").strip()
                if "enabled" in item:
                    enabled = item.get("enabled", True) is not False
                else:
                    enabled = stored.get("enabled", True) is not False
                # 账号白名单与并发上限：此前**完全没被转发** —— 重建的 dict 里
                # 没有这两个键，set_api_keys 归一时读到 None，用户每次保存都被
                # 静默清空（面板显示已选、落盘却是 []）。
                # 与上面 realm/models/name 同一规则：补丁没提到就沿用已存值。
                if "accounts" in item:
                    accounts = item.get("accounts")
                else:
                    accounts = stored.get("accounts")
                if "concurrency_limit" in item:
                    concurrency_limit = item.get("concurrency_limit")
                else:
                    concurrency_limit = stored.get("concurrency_limit")
                created_at = item.get("created_at") or (stored.get("created_at") if entry_id in existing else None) or time.strftime("%Y/%m/%d %H:%M")
                cleaned.append({
                    "id": entry_id,
                    "name": name,
                    "key": value,
                    "realm": realm,
                    "models": models,
                    "accounts": accounts,
                    "concurrency_limit": concurrency_limit,
                    "enabled": enabled,
                    "created_at": created_at,
                })
            wb_settings.set_api_keys(
                _gw().ACCOUNTS_DIR, cleaned,
                delete_ids=(delete_ids if upsert else None),
            )
            reply["api_keys_saved"] = len(cleaned)
        if "auth_disabled" in payload:
            wb_settings.set_auth_disabled(_gw().ACCOUNTS_DIR, payload.get("auth_disabled"))
            reply["auth_disabled"] = bool(payload.get("auth_disabled"))

        # The four guards can arrive grouped under "limits" (the panel's own
        # shape) or as the flat top-level keys older clients still send, which
        # are read as the global default. Both land in the same grouped store,
        # so there is one source of truth no matter who writes it.
        limits_payload = payload.get("limits")
        if limits_payload is not None and not isinstance(limits_payload, dict):
            return self._error(400, "limits must be an object",
                               "invalid_request_error")
        touched = set()
        for key in wb_settings.LIMIT_KEYS:
            grouped = (limits_payload or {}).get(key)
            if isinstance(grouped, dict):
                scopes = {scope: grouped[scope]
                          for scope in wb_settings.LIMIT_SCOPES
                          if scope in grouped}
            elif key in payload:
                scopes = {"global": payload.get(key)}
            else:
                continue
            for scope, raw in scopes.items():
                if scope == "global":
                    if isinstance(raw, bool) or raw is None:
                        return self._error(400, "%s must be a whole number" % key,
                                           "invalid_request_error")
                    try:
                        number = int(raw)
                    except (TypeError, ValueError):
                        return self._error(400, "%s must be a whole number" % key,
                                           "invalid_request_error")
                    if number < 0:
                        return self._error(400, "%s cannot be negative" % key,
                                           "invalid_request_error")
                else:
                    # Blank clears the override back to "inherit the global".
                    if raw is None or raw == "":
                        number = None
                    else:
                        if isinstance(raw, bool):
                            return self._error(
                                400, "%s must be a whole number" % key,
                                "invalid_request_error")
                        try:
                            number = int(raw)
                        except (TypeError, ValueError):
                            return self._error(
                                400, "%s must be a whole number" % key,
                                "invalid_request_error")
                        if number < 0:
                            return self._error(
                                400, "%s cannot be negative" % key,
                                "invalid_request_error")
                wb_settings.set_limit(_gw().ACCOUNTS_DIR, key, scope, number)
                touched.add(key)
            reply[key] = wb_settings.limit_value(_gw().ACCOUNTS_DIR, key)
        if limits_payload is not None:
            reply["limits"] = wb_settings.limits_snapshot(_gw().ACCOUNTS_DIR)
        if touched:
            if _gw().POOL and "reserve_credits" in touched:
                _gw().POOL.apply_reserve_credits()
            if any(k in touched for k in ("daily_token_limit", "daily_credit_limit",
                                          "model_daily_token_limit")):
                # One pass refreshes all three: a limit edit is rare, and the
                # guards publish from the same fold anyway.
                _gw().apply_daily_guards(refresh=True)
            if "expiring_window_days" in touched and _gw().POOL:
                _gw().POOL.apply_expiring_window()

        if "pricing_enabled" in payload:
            raw = payload.get("pricing_enabled")
            if not isinstance(raw, bool):
                return self._error(400, "pricing_enabled must be true or false",
                                   "invalid_request_error")
            wb_settings.set_pricing_enabled(_gw().ACCOUNTS_DIR, raw)
            reply["pricing_enabled"] = raw
            if _gw().PRICING:
                _gw().PRICING.wake()
        if "pricing_refresh_minutes" in payload or "pricing_refresh_hours" in payload:
            # The interval is in minutes. The old field name is still accepted
            # (x60) so a panel page cached from the previous build cannot set
            # the wrong unit; it is answered under the new name.
            field = "pricing_refresh_minutes" \
                if "pricing_refresh_minutes" in payload else "pricing_refresh_hours"
            raw = payload.get(field)
            if isinstance(raw, bool) or raw is None:
                return self._error(400, "%s must be a number" % field,
                                   "invalid_request_error")
            try:
                minutes = float(raw)
            except (TypeError, ValueError):
                return self._error(400, "%s must be a number" % field,
                                   "invalid_request_error")
            if field == "pricing_refresh_hours":
                minutes *= 60.0
            if minutes < 0:
                return self._error(400, "pricing_refresh_minutes cannot be negative",
                                   "invalid_request_error")
            stored = wb_settings.set_pricing_refresh_minutes(_gw().ACCOUNTS_DIR, minutes)
            if _gw().PRICING:
                # A running wait picks the new interval up on the spot.
                _gw().PRICING.set_interval(stored)
            reply["pricing_refresh_minutes"] = stored
        if "credits_refresh_hours" in payload:
            # How stale a credit balance may get before the background refresher
            # updates it. Zero turns the refresher off.
            raw = payload.get("credits_refresh_hours")
            if isinstance(raw, bool) or raw is None:
                return self._error(400, "credits_refresh_hours must be a number",
                                   "invalid_request_error")
            try:
                hours = float(raw)
            except (TypeError, ValueError):
                return self._error(400, "credits_refresh_hours must be a number",
                                   "invalid_request_error")
            if hours < 0:
                return self._error(400, "credits_refresh_hours cannot be negative",
                                   "invalid_request_error")
            reply["credits_refresh_hours"] = \
                wb_settings.set_credits_refresh_hours(_gw().ACCOUNTS_DIR, hours)
            if _gw().CREDITS_REFRESHER:
                _gw().CREDITS_REFRESHER.wake()
        if "section_max_height" in payload:
            # Panel window height cap in pixels; 0 turns the cap off. bool is an
            # int subclass and must be rejected explicitly - true would
            # otherwise store 1 and shrink every window to a sliver.
            raw = payload.get("section_max_height")
            if isinstance(raw, bool) or raw is None:
                return self._error(400, "section_max_height must be a whole number",
                                   "invalid_request_error")
            try:
                number = int(raw)
            except (TypeError, ValueError, OverflowError):
                # OverflowError: a JSON payload may carry Infinity (1e400),
                # which int() refuses - answer 400, not a 500.
                return self._error(400, "section_max_height must be a whole number",
                                   "invalid_request_error")
            # A fractional count is refused rather than truncated: int(-0.5) is
            # 0, which would slip past the negative check and silently turn the
            # cap off.
            if float(raw) != number:
                return self._error(400, "section_max_height must be a whole number",
                                   "invalid_request_error")
            if number < 0:
                return self._error(400, "section_max_height cannot be negative",
                                   "invalid_request_error")
            reply["section_max_height"] = \
                wb_settings.set_section_max_height(_gw().ACCOUNTS_DIR, number)
        if "schedule" in payload:
            raw = payload.get("schedule")
            if not isinstance(raw, dict):
                return self._error(400, "schedule must be an object",
                                   "invalid_request_error")
            if "daily_chat" in raw:
                try:
                    wb_settings.set_daily_chat_times(_gw().ACCOUNTS_DIR,
                                                     raw.get("daily_chat"))
                except ValueError as exc:
                    return self._error(400, str(exc), "invalid_request_error")
            hours_patch = {k: v for k, v in raw.items() if k != "daily_chat"}
            if hours_patch:
                try:
                    wb_settings.set_schedule_hours(_gw().ACCOUNTS_DIR, hours_patch)
                except ValueError as exc:
                    return self._error(400, str(exc), "invalid_request_error")
            reply["schedule"] = dict(
                wb_settings.schedule_hours(_gw().ACCOUNTS_DIR),
                daily_chat=wb_settings.daily_chat_times(_gw().ACCOUNTS_DIR))
        if "ui_language" in payload:
            # Instance-wide default language. The dashboard overrides this per
            # browser with localStorage; this value is the fallback when no
            # browser-local preference exists.
            raw = payload.get("ui_language")
            if not isinstance(raw, str) or raw not in ("zh", "zh-Hant", "en"):
                return self._error(400, "ui_language must be zh, zh-Hant or en",
                                   "invalid_request_error")
            reply["ui_language"] = wb_settings.set_ui_language(_gw().ACCOUNTS_DIR, raw)
        if "pricing_variant_inherit" in payload:
            # Strictly a JSON boolean, like the other switches: "false" as a
            # string would be truthy and silently keep the feature on.
            raw = payload.get("pricing_variant_inherit")
            if not isinstance(raw, bool):
                return self._error(400, "pricing_variant_inherit must be true or false",
                                   "invalid_request_error")
            previous = wb_settings.pricing_variant_inherit(_gw().ACCOUNTS_DIR)
            wb_settings.set_pricing_variant_inherit(_gw().ACCOUNTS_DIR, raw)
            reply["pricing_variant_inherit"] = raw
            if _gw().PRICING and previous != raw:
                # The switch only takes effect on the next fetch - a refresh
                # drops (off) or adds (on) the suffix-inherited assignments -
                # so kick one off rather than waiting out the interval.
                threading.Thread(target=_gw().PRICING.run_once, daemon=True,
                                 name="price-refresh-inherit").start()
                reply["pricing_refresh_started"] = True
        if "enable_anthropic_messages" in payload:
            raw = payload.get("enable_anthropic_messages")
            if not isinstance(raw, bool):
                return self._error(400, "enable_anthropic_messages must be true or false", "invalid_request_error")
            wb_settings.set_enable_anthropic_messages(_gw().ACCOUNTS_DIR, raw)
            reply["enable_anthropic_messages"] = raw
        if "filter_models_by_key" in payload:
            raw = payload.get("filter_models_by_key")
            if not isinstance(raw, bool):
                return self._error(400, "filter_models_by_key must be true or false", "invalid_request_error")
            wb_settings.set_filter_models_by_key(_gw().ACCOUNTS_DIR, raw)
            reply["filter_models_by_key"] = raw
        if "reasoning_effort_map" in payload:
            raw = payload.get("reasoning_effort_map")
            if not isinstance(raw, bool):
                return self._error(400, "reasoning_effort_map must be true or false", "invalid_request_error")
            wb_settings.set_reasoning_effort_map(_gw().ACCOUNTS_DIR, raw)
            reply["reasoning_effort_map"] = raw
        if "models_only_available" in payload:
            raw = payload.get("models_only_available")
            if not isinstance(raw, bool):
                return self._error(400, "models_only_available must be true or false",
                                   "invalid_request_error")
            wb_settings.set_models_only_available(_gw().ACCOUNTS_DIR, raw)
            reply["models_only_available"] = raw
        if "identity" in payload:
            raw = payload.get("identity")
            if not isinstance(raw, dict):
                return self._error(400, "identity must be an object",
                                   "invalid_request_error")
            if "private_data" in raw:
                try:
                    value = wb_settings.set_identity_private_data(
                        _gw().ACCOUNTS_DIR, raw.get("private_data"))
                except ValueError as exc:
                    return self._error(400, str(exc), "invalid_request_error")
                reply["identity_private_data"] = value
        if "identity_versions" in payload:
            password = payload.get("panel_password") or ""
            if not wb_settings.verify_panel_password(_gw().ACCOUNTS_DIR, password):
                return self._error(401, "invalid panel password",
                                   "invalid_request_error")
            raw = payload.get("identity_versions")
            if not isinstance(raw, dict):
                return self._error(400, "identity_versions must be an object",
                                   "invalid_request_error")
            try:
                stored = wb_settings.set_identity_versions(_gw().ACCOUNTS_DIR, raw)
            except ValueError as exc:
                return self._error(400, str(exc), "invalid_request_error")
            wb_identity.configure_versions(stored)
            reply["identity_versions"] = stored
        if "free_model_credit_fallback" in payload:
            raw = payload.get("free_model_credit_fallback")
            if not isinstance(raw, bool):
                return self._error(400, "free_model_credit_fallback must be true or false", "invalid_request_error")
            wb_settings.set_free_model_credit_fallback(_gw().ACCOUNTS_DIR, raw)
            reply["free_model_credit_fallback"] = raw
        if "auto_switch_product" in payload:
            # Strictly a JSON boolean: a string like "false" would be truthy and
            # silently switch the feature on, which is the one thing an operator
            # turning it off must not get.
            raw = payload.get("auto_switch_product")
            if not isinstance(raw, bool):
                return self._error(400, "auto_switch_product must be true or false",
                                   "invalid_request_error")
            wb_settings.set_auto_switch_product(_gw().ACCOUNTS_DIR, raw)
            reply["auto_switch_product"] = raw
        if "auto_switch_products" in payload:
            # 允许自动切换落在哪些身份上：必须是已知 product id 的列表。
            raw = payload.get("auto_switch_products")
            if not isinstance(raw, list) or any(not isinstance(p, str) for p in raw):
                return self._error(
                    400, "auto_switch_products must be a list of product ids",
                    "invalid_request_error")
            try:
                clean = wb_settings.set_auto_switch_products(_gw().ACCOUNTS_DIR, raw)
            except ValueError as exc:
                return self._error(400, str(exc), "invalid_request_error")
            reply["auto_switch_products"] = clean
        if "daily_chat_web" in payload:
            raw = payload.get("daily_chat_web")
            if not isinstance(raw, bool):
                return self._error(400, "daily_chat_web must be true or false",
                                   "invalid_request_error")
            wb_settings.set_daily_chat_web(_gw().ACCOUNTS_DIR, raw)
            reply["daily_chat_web"] = raw
        if "daily_chat_web_timeout" in payload:
            # Whole seconds only, and bounded: the retry doubles this, so a
            # stray extra digit would park the scheduler on one account for
            # hours. wb_settings owns both checks and the message.
            raw = payload.get("daily_chat_web_timeout")
            try:
                wb_settings.set_daily_chat_web_timeout(_gw().ACCOUNTS_DIR, raw)
            except ValueError as exc:
                return self._error(400, str(exc), "invalid_request_error")
            reply["daily_chat_web_timeout"] = raw
        if "daily_chat_web_retry_backoff" in payload:
            # Whole seconds, 0 = off. Only rounds that never reached the turn
            # sleep, and each wait is capped, so a stray extra digit costs
            # seconds rather than hours - wb_settings owns both checks.
            raw = payload.get("daily_chat_web_retry_backoff")
            try:
                wb_settings.set_daily_chat_web_retry_backoff(_gw().ACCOUNTS_DIR, raw)
            except ValueError as exc:
                return self._error(400, str(exc), "invalid_request_error")
            reply["daily_chat_web_retry_backoff"] = raw
        if "daily_chat_web_random_prompt" in payload:
            raw = payload.get("daily_chat_web_random_prompt")
            if not isinstance(raw, bool):
                return self._error(400, "daily_chat_web_random_prompt must be true or false",
                                   "invalid_request_error")
            wb_settings.set_daily_chat_web_random_prompt(_gw().ACCOUNTS_DIR, raw)
            reply["daily_chat_web_random_prompt"] = raw
        if "daily_chat_web_prompts" in payload:
            raw = payload.get("daily_chat_web_prompts")
            if not isinstance(raw, (list, tuple, str)):
                return self._error(
                    400, "daily_chat_web_prompts must be a list or newline string",
                    "invalid_request_error")
            reply["daily_chat_web_prompts"] = \
                wb_settings.set_daily_chat_web_prompts(_gw().ACCOUNTS_DIR, raw)
        if "daily_chat_web_concurrency" in payload:
            raw = payload.get("daily_chat_web_concurrency")
            try:
                wb_settings.set_daily_chat_web_concurrency(_gw().ACCOUNTS_DIR, raw)
            except ValueError as exc:
                return self._error(400, str(exc), "invalid_request_error")
            reply["daily_chat_web_concurrency"] = raw
        if "daily_chat_models" in payload:
            # Ordered candidate list: first entry runs first, the rest are the
            # fallback combination. An empty list means "automatic". Every id
            # has to look like a model id - a stray value here would be sent
            # upstream on every daily run.
            raw = payload.get("daily_chat_models")
            if isinstance(raw, str):
                raw = [part for part in re.split(r"[,\n]", raw) if part.strip()]
            if not isinstance(raw, (list, tuple)):
                return self._error(
                    400, "daily_chat_models must be a list or comma string",
                    "invalid_request_error")
            for item in raw:
                if not isinstance(item, str) or len(item) > 64 or \
                        (item and not re.match(r"^[A-Za-z0-9._:-]+$", item)):
                    return self._error(
                        400, "daily_chat_models entries must be model ids",
                        "invalid_request_error")
            if len(raw) > wb_settings.DAILY_CHAT_MODELS_MAX:
                return self._error(
                    400, "daily_chat_models cannot hold more than %d models"
                         % wb_settings.DAILY_CHAT_MODELS_MAX,
                    "invalid_request_error")
            reply["daily_chat_models"] = \
                wb_settings.set_daily_chat_models(_gw().ACCOUNTS_DIR, raw)
        if "daily_chat_budget" in payload:
            # Whole seconds, 0 = no budget. This is the only time bound of the
            # whole web activity, so wb_settings owns the range check and the
            # message; a stray extra digit would park the scheduler.
            raw = payload.get("daily_chat_budget")
            try:
                wb_settings.set_daily_chat_budget(_gw().ACCOUNTS_DIR, raw)
            except ValueError as exc:
                return self._error(400, str(exc), "invalid_request_error")
            reply["daily_chat_budget"] = raw
        if "daily_chat_startup_catchup" in payload:
            raw = payload.get("daily_chat_startup_catchup")
            if not isinstance(raw, bool):
                return self._error(
                    400, "daily_chat_startup_catchup must be true or false",
                    "invalid_request_error")
            wb_settings.set_daily_chat_startup_catchup(_gw().ACCOUNTS_DIR, raw)
            reply["daily_chat_startup_catchup"] = raw
        if "daily_chat_web_fallback_activity" in payload:
            raw = payload.get("daily_chat_web_fallback_activity")
            if not isinstance(raw, bool):
                return self._error(
                    400, "daily_chat_web_fallback_activity must be true or false",
                    "invalid_request_error")
            wb_settings.set_daily_chat_web_fallback_activity(_gw().ACCOUNTS_DIR, raw)
            reply["daily_chat_web_fallback_activity"] = raw
        if "local_web_tools" in payload:
            raw = payload.get("local_web_tools")
            if not isinstance(raw, bool):
                return self._error(400, "local_web_tools must be true or false",
                                   "invalid_request_error")
            wb_settings.set_local_web_tools(_gw().ACCOUNTS_DIR, raw)
            reply["local_web_tools"] = raw
        if "remaining_priority_enabled" in payload:
            raw = payload.get("remaining_priority_enabled")
            if not isinstance(raw, bool):
                return self._error(400, "remaining_priority_enabled must be true or false",
                                   "invalid_request_error")
            wb_settings.set_remaining_priority_enabled(_gw().ACCOUNTS_DIR, raw)
            reply["remaining_priority_enabled"] = raw
        if "egress_backoff_enabled" in payload:
            # 出口级瞬时失败退避（issue #271）：与 remaining_priority_enabled
            # 同一形状——严格 JSON 布尔，字符串 "false" 会是真值。
            raw = payload.get("egress_backoff_enabled")
            if not isinstance(raw, bool):
                return self._error(400, "egress_backoff_enabled must be true or false",
                                   "invalid_request_error")
            wb_settings.set_egress_backoff_enabled(_gw().ACCOUNTS_DIR, raw)
            reply["egress_backoff_enabled"] = raw
            if not raw and _gw().POOL is not None:
                # 关掉就立刻清掉池里上一次推的权重，不等下一个请求；打开则
                # 由下一个请求照常推（此刻就算推也要先折一次剩余估算，没必要
                # 为一个开关多付这一趟）。
                _gw().POOL.apply_remaining_weights(None)
        if "realm_fallback_enabled" in payload:
            # 跨出口回退（默认关）：与 remaining_priority_enabled 同一形状——
            # 严格 JSON 布尔，字符串 "false" 会是真值。
            raw = payload.get("realm_fallback_enabled")
            if not isinstance(raw, bool):
                return self._error(400, "realm_fallback_enabled must be true or false",
                                   "invalid_request_error")
            wb_settings.set_realm_fallback_enabled(_gw().ACCOUNTS_DIR, raw)
            reply["realm_fallback_enabled"] = raw
        if "account_max_concurrent" in payload:
            # 账号级并发上限：0 = 不限（默认，与上游一字不差）；1..10 = 每账号
            # 同时最多几个 chat 请求。严格整数，字符串 "1" 不算数（它会被当成
            # 布尔/字符串混进 settings.json，下次读出来就不是上限了）。
            raw = payload.get("account_max_concurrent")
            if isinstance(raw, bool) or not isinstance(raw, int):
                return self._error(
                    400, "account_max_concurrent must be a whole number",
                    "invalid_request_error")
            try:
                value = wb_settings.set_account_max_concurrent(_gw().ACCOUNTS_DIR, raw)
            except ValueError as exc:
                return self._error(400, str(exc), "invalid_request_error")
            reply["account_max_concurrent"] = value
        if "affinity_stable_key_enabled" in payload:
            # 更稳的会话粘滞键：严格 JSON 布尔（字符串 "false" 会是真值）。
            raw = payload.get("affinity_stable_key_enabled")
            if not isinstance(raw, bool):
                return self._error(
                    400, "affinity_stable_key_enabled must be true or false",
                    "invalid_request_error")
            wb_settings.set_affinity_stable_key_enabled(_gw().ACCOUNTS_DIR, raw)
            reply["affinity_stable_key_enabled"] = raw
        if "log_db_enabled" in payload or "log_db_max_rows" in payload \
                or "log_db_max_days" in payload or "log_db_max_bytes" in payload:
            # 日志落库（默认关闭）：开关与三条保留上限一次提交里可以任选子集，
            # 没带到的键原样保留。上限留空（null / "" / 0）一律存成「不限」。
            log_db_touched = False
            if "log_db_enabled" in payload:
                raw = payload.get("log_db_enabled")
                if not isinstance(raw, bool):
                    return self._error(400, "log_db_enabled must be true or false",
                                       "invalid_request_error")
                wb_settings.set_log_db_enabled(_gw().ACCOUNTS_DIR, raw)
                reply["log_db_enabled"] = raw
                log_db_touched = True
            for key, setter in (
                    ("log_db_max_rows", wb_settings.set_log_db_max_rows),
                    ("log_db_max_days", wb_settings.set_log_db_max_days),
                    ("log_db_max_bytes", wb_settings.set_log_db_max_bytes)):
                if key in payload:
                    reply[key] = setter(_gw().ACCOUNTS_DIR, payload.get(key))
                    log_db_touched = True
            if log_db_touched:
                _gw().apply_log_db_settings()
        if "accounts_collapsed" in payload:
            # A disclosure state, and the only thing this branch may touch: the
            # submission carries just this key, so the settings it does not name
            # survive the write.
            raw = payload.get("accounts_collapsed")
            if not isinstance(raw, bool):
                return self._error(400, "accounts_collapsed must be true or false",
                                   "invalid_request_error")
            wb_settings.set_accounts_collapsed(_gw().ACCOUNTS_DIR, raw)
            reply["accounts_collapsed"] = raw
        if "accounts_separate_tab" in payload:
            # Same shape again: strictly a JSON boolean, because "false" as a
            # string would be truthy and silently split the nav.
            raw = payload.get("accounts_separate_tab")
            if not isinstance(raw, bool):
                return self._error(400, "accounts_separate_tab must be true or false",
                                   "invalid_request_error")
            wb_settings.set_accounts_separate_tab(_gw().ACCOUNTS_DIR, raw)
            reply["accounts_separate_tab"] = raw
        if "key_before_hidden" in payload:
            # Same shape as accounts_collapsed: one disclosure state per
            # submission, and strictly a JSON boolean, because "false" as a
            # string would be truthy and silently fold the row away.
            raw = payload.get("key_before_hidden")
            if not isinstance(raw, bool):
                return self._error(400, "key_before_hidden must be true or false",
                                   "invalid_request_error")
            wb_settings.set_key_before_hidden(_gw().ACCOUNTS_DIR, raw)
            reply["key_before_hidden"] = raw
        if "hidden_pages" in payload:
            # A list of page keys, and the only thing this branch may touch: the
            # submission carries just this key, so the settings it does not name
            # survive the write. A bare string would iterate character by
            # character and hide nothing but garbage, so anything that is not a
            # list of strings is rejected outright rather than normalised away.
            raw = payload.get("hidden_pages")
            if not isinstance(raw, list) or any(not isinstance(k, str) for k in raw):
                return self._error(400, "hidden_pages must be a list of page keys",
                                   "invalid_request_error")
            # Reply with the normalised list, not the raw one: keys the request
            # spelled wrong are dropped on the way in, so the panel echoes back
            # what was actually stored.
            reply["hidden_pages"] = wb_settings.set_hidden_pages(_gw().ACCOUNTS_DIR, raw)
        if "update_check_enabled" in payload:
            # Strictly a JSON boolean, like the switches above: "false" as a
            # string would be truthy and silently start the daily GitHub call.
            raw = payload.get("update_check_enabled")
            if not isinstance(raw, bool):
                return self._error(400, "update_check_enabled must be true or false",
                                   "invalid_request_error")
            wb_settings.set_update_check_enabled(_gw().ACCOUNTS_DIR, raw)
            reply["update_check_enabled"] = raw
            if _gw().UPDATES and raw:
                # Turning it on should not wait out the rest of the poll sleep.
                _gw().UPDATES.wake()
        if "keep_backups" in payload:
            # An integer count, not a boolean-ish string: "2" would otherwise
            # be accepted by int() and "true" would silently become 1.
            raw = payload.get("keep_backups")
            if isinstance(raw, bool) or not isinstance(raw, int):
                return self._error(400, "keep_backups must be an integer 0-3",
                                   "invalid_request_error")
            stored = wb_settings.set_keep_backups(_gw().ACCOUNTS_DIR, raw)
            reply["keep_backups"] = stored
            # The panel's hint promises this takes effect now: a lower value
            # has to drop the extra folders immediately, not at the next update.
            reply["pruned_backups"] = wb_update_request.apply_keep_backups(stored)
        if "upstream" in payload:
            raw = payload.get("upstream")
            if not isinstance(raw, dict):
                return self._error(400, "upstream must be an object",
                                   "invalid_request_error")
            try:
                patch = wb_settings.validate_upstream_patch(raw)
            except ValueError as exc:
                return self._error(400, str(exc), "invalid_request_error")
            wb_settings.set_upstream_config(_gw().ACCOUNTS_DIR, patch)
            reply["upstream"] = wb_settings.upstream_config(_gw().ACCOUNTS_DIR)
        if "usage" in payload:
            raw = payload.get("usage")
            if not isinstance(raw, dict):
                return self._error(400, "usage must be an object",
                                   "invalid_request_error")
            try:
                patch = wb_settings.validate_usage_patch(raw)
            except ValueError as exc:
                return self._error(400, str(exc), "invalid_request_error")
            wb_settings.set_usage_config(_gw().ACCOUNTS_DIR, patch)
            reply["usage"] = wb_settings.usage_config(_gw().ACCOUNTS_DIR)
        if "prompt" in payload:
            raw = payload.get("prompt")
            if not isinstance(raw, dict):
                return self._error(400, "prompt must be an object",
                                   "invalid_request_error")
            try:
                patch = wb_settings.validate_prompt_patch(raw)
            except ValueError as exc:
                return self._error(400, str(exc), "invalid_request_error")
            wb_settings.set_prompt_config(_gw().ACCOUNTS_DIR, patch)
            reply["prompt"] = wb_settings.prompt_config(_gw().ACCOUNTS_DIR)
        if "window_probe" in payload:
            raw = payload.get("window_probe")
            if not isinstance(raw, bool):
                return self._error(400, "window_probe must be true or false",
                                   "invalid_request_error")
            wb_settings.set_window_probe(_gw().ACCOUNTS_DIR, raw)
            reply["window_probe"] = raw
        if "quota_scan_models" in payload:
            raw_models = payload.get("quota_scan_models")
            # The watch list is either a list of model ids or nothing at all
            # (clearing it switches the scanner off); anything else is a panel
            # bug worth rejecting rather than silently dropping every model.
            if raw_models is not None and not isinstance(raw_models, list):
                return self._error(400, "quota_scan_models must be a list of model ids",
                                   "invalid_request_error")
            if isinstance(raw_models, list):
                for item in raw_models:
                    if item is not None and not isinstance(item, str):
                        return self._error(400, "quota_scan_models holds a non-string entry",
                                           "invalid_request_error")
            if len(raw_models or []) > 200:
                return self._error(400, "quota_scan_models cannot hold more than 200 models",
                                   "invalid_request_error")
            wb_settings.set_quota_scan_models(_gw().ACCOUNTS_DIR, raw_models)
            reply["quota_scan_models_saved"] = len(
                wb_settings.quota_scan_models(_gw().ACCOUNTS_DIR))
        new_key = payload.get("api_key")
        if new_key is not None:
            new_key = str(new_key).strip()
            if new_key and len(new_key) < 4:
                return self._error(400, "api key must be at least 4 characters",
                                   "invalid_request_error")
            _gw().set_global_api_key(new_key)
            reply["api_key_set"] = bool(new_key)
        if payload.get("restart_scheduler"):
            if _gw().SCHEDULER:
                _gw().SCHEDULER.stop()
                _gw().SCHEDULER.start()
            reply["scheduler"] = "restarted"
        reply.update(_gw().runtime_settings_view())
        return self._json(200, reply)
