"""Panel routes for the account pool: list, import/export, login, credits,
per-account switches, connectivity tests and the desktop-client scan.

`AccountRoutes` is a mixin of wb_proxy.Handler. The pool itself and its
background jobs live in wb_accounts / wb_scheduler; these handlers only parse
requests, call into the pool and shape the panel's JSON. Gateway state is
read through `_gw()`.
"""

import os
import time

import wb_accounts
import wb_atrest
import wb_identity
import wb_runtime

_gw = wb_runtime.gateway


class AccountRoutes(object):
    def _get_accounts_credits(self):
        if not self._authorized():
            return
        # Refresh credits for all accounts —— 全池扫，吃单飞与拟人间隔
        # （见 _credits_sweep 的说明）。已有一次在跑时不排队、不报错：直接把当前
        # 快照回给调用方，前端拿到的仍是可用数据。
        targets = list(_gw().POOL.accounts) if _gw().POOL else []
        busy = not _gw()._credits_sweep_begin()
        if not busy:
            try:
                _gw()._credits_sweep(targets)
            finally:
                _gw()._credits_sweep_end()
        return self._json_panel(200, {"accounts": _gw().account_views(), "busy": busy})

    def _get_account_credits_grants(self):
        """积分获取历史：只读账号积分快照，不发上游请求。

        要更新的数字先走「一键刷新积分」（POST /accounts/credits），这里只把
        最近一次快照里的积分包摊平成一张表；载荷带上快照时刻，面板据此提醒
        数据有多旧。
        """
        if not self._authorized():
            return
        return self._json_panel(200, _gw().credit_grants())

    def _get_account_credits_detail(self, query):
        if not self._authorized():
            return
        uid = (query.get("uid") or [""])[0]
        refresh = (query.get("refresh") or ["0"])[0] in ("1", "true", "yes")
        if not _gw().POOL:
            return self._json_panel(200, {"ok": False, "error": "账号池未初始化"})
        account = _gw().POOL.get(uid) if uid else None
        if not account:
            if _gw().POOL.accounts:
                account = _gw().POOL.accounts[0]
            else:
                return self._json_panel(200, {"ok": False, "error": "未找到指定账号"})

        if refresh or not account.credits or not account.credits.get("packages"):
            res = account.fetch_credits()
            if not res.get("ok"):
                return self._json_panel(200, {
                    "ok": False,
                    "uid": account.uid,
                    "nickname": account.nickname,
                    "realm": account.realm,
                    "credits": account.credits,
                    "error": res.get("error", "获取积分明细失败")
                })

        return self._json_panel(200, {
            "ok": True,
            "uid": account.uid,
            "nickname": account.nickname,
            "realm": account.realm,
            "credits": account.credits
        })

    def _get_accounts(self, query):
        if not self._authorized():
            return
        # Fold the usage log before building the view, so the 日限额 badge and
        # the parked count describe right now instead of the last request.
        _gw().apply_daily_guards()
        return self._json_panel(200, {
            "accounts": _gw().account_views(realm=query.get('realm', [None])[0] or _gw().CURRENT_REALM),
            "storage": _gw().ACCOUNTS_DIR,
            "usable": _gw().POOL.count_ready() if _gw().POOL else 0,
        })

    def _get_accounts_export(self, query):
        if not self._authorized():
            return
        # ?download=1 makes the browser save it as a file; without it the
        # document is returned inline so the dashboard can show a summary.
        # ?uid= narrows it to specific accounts (repeatable, comma-joined),
        # which is how the per-row "export" button works.
        realm = (query.get("realm") or [None])[0] or None
        if realm not in ("intl", "cn"):
            realm = None
        include_secrets = (query.get("secrets") or ["1"])[0] not in ("0", "false", "no")
        uids = []
        for raw in query.get("uid") or []:
            uids.extend(part.strip() for part in str(raw).split(",") if part.strip())
        if uids:
            known = {a.uid for a in (_gw().POOL.accounts if _gw().POOL else [])}
            missing = [u for u in uids if u not in known]
            if missing:
                return self._error(404, "no such account: %s" % ", ".join(missing[:5]),
                                   "invalid_request_error")
        doc = wb_accounts.build_export_document(
            _gw().POOL.accounts if _gw().POOL else [],
            realm=realm,
            include_secrets=include_secrets,
            uids=uids or None,
        )
        # 一个出口覆盖两条路：不带 download=1 走下面的 _json（前端实际用的就是
        # 这条），带 download=1 走 _download()（自带 json.dumps，绕过 _json）。
        # 登记后果：这份掩码昵称若经 /accounts/import 回灌，会以掩码形态落盘；
        # 可接受——昵称正是要隐藏的东西，且 sync_nickname() 能从上游取回真名，
        # accessToken/refreshToken 等凭证字段不受影响。
        doc = _gw()._redact_account_names(doc)
        if (query.get("download") or ["0"])[0] in ("1", "true", "yes"):
            stamp = time.strftime("%Y%m%d-%H%M%S")
            if len(uids) == 1:
                # Name a single-account export after the account, so a
                # folder of them stays readable.
                label = uids[0][:8]
            else:
                label = realm + "-" if realm else ""
            name = "workbuddy-accounts-%s%s.json" % (label, stamp)
            return self._download(name, doc)
        return self._json(200, doc)

    def _get_accounts_login_poll(self, query):
        if not self._authorized():
            return
        state = (query.get("state") or [""])[0]
        return self._json_panel(200, _gw().POOL.poll_login(state))

    def _handle_accounts(self, path, payload):
        """Account-management endpoints (dashboard uses these)."""
        if _gw().POOL is None:
            return self._error(503, "account pool unavailable")
        if path == "/accounts/import" and isinstance(payload, list):
            # A bare array is only meaningful for import; wrap it so the rest
            # of this handler can keep assuming a dict.
            payload = {"data": payload}
        if not isinstance(payload, dict):
            return self._error(400, "expected a JSON object", "invalid_request_error")
        if path in ("/accounts/credits", "/accounts/credits/fetch"):
            return self._route_accounts_credits_fetch(payload)
        if path == "/accounts/credits/detail":
            return self._route_account_credits_detail(payload)
        if path == "/tasks/run":
            return self._route_tasks_run(payload)
        if path == "/tasks/claim":
            return self._route_tasks_claim(payload)
        if path == "/tasks/travel":
            return self._route_tasks_travel(payload)
        if path == "/tasks/streak-bonus":
            return self._route_tasks_streak_bonus(payload)
        if path == "/scheduler/trigger":
            return self._route_scheduler_trigger(payload)
        if path == "/scheduler/toggle":
            return self._route_scheduler_toggle(payload)
        if path == "/pricing/refresh":
            return self._route_pricing_refresh(payload)
        if path == "/logs/clear":
            return self._route_logs_clear(payload)
        if path == "/realm":
            return self._route_realm(payload)
        if path == "/accounts/checkin":
            return self._route_accounts_checkin(payload)
        if path == "/accounts/daily-chat":
            return self._route_accounts_daily_chat(payload)
        if path == "/accounts/daily-chat-web":
            return self._route_accounts_daily_chat_web(payload)
        if path == "/accounts/login/start":
            return self._route_accounts_login_start(payload)
        if path == "/accounts/login/cancel":
            return self._route_accounts_login_cancel(payload)
        if path == "/accounts/import/desktop":
            return self._route_accounts_import_desktop(payload)
        if path == "/accounts/refresh":
            return self._route_accounts_refresh(payload)
        if path == "/accounts/sync-profile":
            return self._route_accounts_sync_profile(payload)
        if path == "/accounts/test":
            return self._route_accounts_test(payload)
        if path == "/accounts/set":
            return self._route_accounts_set(payload)
        if path == "/accounts/product":
            return self._route_accounts_product(payload)
        if path == "/accounts/set-all":
            return self._route_accounts_set_all(payload)
        if path == "/accounts/delete":
            return self._route_accounts_delete(payload)
        if path == "/accounts/import":
            return self._route_accounts_import(payload)
        if path == "/accounts/add-api-key":
            return self._route_accounts_add_api_key(payload)
        return self._error(404, "unknown account endpoint", "invalid_request_error")

    def _route_accounts_product(self, payload):
        """切换出站身分（cli <-> workbuddy），并即时回传结果。

        官方有两套身分、两条配额线。某条满了可以切到另一条继续用。
        """
        target = str(payload.get("product") or "").strip().lower()
        uid = payload.get("uid")
        realm = payload.get("realm")

        if target not in wb_identity.VALID_PRODUCTS:
            return self._error(400, "product must be 'workbuddy', 'vscode', or 'cli'",
                               "invalid_request_error")

        if uid:
            targets = [_gw().POOL.get(uid)]
        elif realm and realm != "all":
            targets = [a for a in _gw().POOL.accounts if a.realm == realm]
        else:
            targets = list(_gw().POOL.accounts)

        changed = []
        for account in targets:
            if account is None:
                continue
            try:
                if account.set_product(target):
                    # 立刻落盘：set_product() 只改记忆体，而面板上这一下是操作者
                    # 的明确选择，不能等到别的路径（refresh / 签到 / 查积分）刚好
                    # 存档才生效——切完就重启容器的人会白白丢掉这次切换。
                    try:
                        account.save(_gw().ACCOUNTS_DIR)
                    except Exception as exc:
                        _gw().log("product save failed for %s: %s" % (account.uid[:8], exc),
                            level="WARN")
                    changed.append(account.uid[:8])
                    _gw().log("account %s: 面板手動切換身分 -> %s"
                        % (account.uid[:8], target), level="INFO")
            except Exception as exc:
                _gw().log("product switch failed for %s: %s" % (account.uid[:8], exc),
                    level="WARN")

        return self._json_panel(200, {
            "ok": True,
            "product": target,
            "changed": changed,
            "accounts": _gw().account_views(),
        })

    def _route_accounts_credits_fetch(self, payload):
        uid = payload.get("uid")
        realm = payload.get("realm")
        if uid:
            targets = [_gw().POOL.get(uid)]
        elif realm and realm != "all":
            targets = [a for a in _gw().POOL.accounts if a.realm == realm]
        else:
            targets = list(_gw().POOL.accounts)
        # 多账号目标才吃单飞与拟人间隔：单账号刷新本来就只发一次请求，不该被别人的
        # 全量刷挡住，也不该为它空等一个间隔。
        multi = len([t for t in targets if t is not None]) > 1
        if multi and not _gw()._credits_sweep_begin():
            # 已有一次全量刷在跑：不排队、不重复轰炸上游，把当前快照原样回给调用方。
            return self._json_panel(200, {"results": [], "accounts": _gw().account_views(),
                                          "busy": True})
        try:
            results = _gw()._credits_sweep(targets, paced=multi)
        finally:
            if multi:
                _gw()._credits_sweep_end()
        return self._json_panel(200, {"results": results, "accounts": _gw().account_views()})

    def _route_account_credits_detail(self, payload):
        if not self._authorized():
            return
        uid = payload.get("uid")
        refresh = payload.get("refresh", True)
        if not _gw().POOL:
            return self._json_panel(200, {"ok": False, "error": "账号池未初始化"})
        account = _gw().POOL.get(uid) if uid else None
        if not account:
            return self._json_panel(200, {"ok": False, "error": "未找到指定账号"})

        if refresh or not account.credits or not account.credits.get("packages"):
            res = account.fetch_credits()
            if not res.get("ok"):
                return self._json_panel(200, {
                    "ok": False,
                    "uid": account.uid,
                    "nickname": account.nickname,
                    "realm": account.realm,
                    "credits": account.credits,
                    "error": res.get("error", "获取积分明细失败")
                })

        return self._json_panel(200, {
            "ok": True,
            "uid": account.uid,
            "nickname": account.nickname,
            "realm": account.realm,
            "credits": account.credits
        })

    def _route_accounts_checkin(self, payload):
        uid = payload.get("uid")
        _gw().invalidate_tasks_cache()          # 签到会改连续打卡状态，成长任务快照随之作废
        # 只挑有个人成长体系的号：静态 API Key 与企业号都没有，签到端点对它们
        # 一律拒绝，发过去只是白烧一次上游请求。
        # 任务开关（#521）同样要遵守：停用任务参与的号不跑签到。
        targets = ([_gw().POOL.get(uid)] if uid
                   else [a for a in (_gw().POOL.accounts if _gw().POOL else [])
                         if a.realm == "cn"
                         and a.task_active()
                         and not a.uses_enterprise_billing()])
        results = []
        for account in targets:
            if account is None:
                continue
            res = account.checkin(trigger="manual")
            nick = account.nickname or account.uid[:8]
            _gw().add_log_entry(f"[任务福利] 账号 [{nick}] 每日签到: {res.get('msg') if res.get('ok') else res.get('error')}",
                          level="INFO" if res.get("ok") else "WARN", tag="tasks")
            results.append({"uid": account.uid, "nickname": account.nickname, **res})
        return self._json_panel(200, {"results": results, "accounts": _gw().account_views()})

    def _route_accounts_login_start(self, payload):
        platform = payload.get("platform") or "CLI"
        target_realm = payload.get("realm") or _gw().CURRENT_REALM
        try:
            started = _gw().POOL.start_login(realm=target_realm, platform=platform)
        except wb_accounts.TLSVerifyError as exc:
            # The hint already names the file/variable to fix; prefixing it
            # with "could not start login" only buries it.
            return self._error(502, str(exc))
        except Exception as exc:
            return self._error(502, "could not start login: %s" % exc)
        _gw().log("oauth login started (realm=%s, platform=%s, state=%s)" % (target_realm, platform, started["state"][:8]))
        return self._json(200, started)

    def _route_accounts_login_cancel(self, payload):
        state = payload.get("state") or ""
        return self._json(200, {"cancelled": _gw().POOL.cancel_login(state)})

    def _route_accounts_import_desktop(self, payload):
        # Two ways to call this:
        #   {}                     -> scan only (read-only, nothing imported)
        #   {"path": "..."}        -> import that credential
        #   {"all": true}          -> import everything the scan found
        #   {"recoverKey": true}   -> recover the at-rest key from the desktop client
        target_path = payload.get("path")
        if payload.get("recoverKey"):
            try:
                key = wb_accounts.desktop_atrest_key(
                    force=bool(payload.get("force")), log=lambda m: _gw().log(m, tag="accounts"))
            except Exception as exc:
                _gw().log("at-rest key recovery failed: %s" % exc, level="WARN", tag="accounts")
                return self._json_panel(200, {"ok": False, "msg": str(exc),
                                        "atrest": wb_atrest.status()})
            return self._json_panel(200, {
                "ok": True,
                "keyId": wb_atrest.derive_key_id(key),
                "msg": "已从桌面客户端进程回收密钥（只留在内存里）",
                "atrest": wb_atrest.status(),
            })
        if payload.get("keyText") is not None:
            # 手工导入解码密钥：macOS / Linux 上无法回收进程内存，只能让用户贴一次。
            # 与回收同一道闸——过不了 keyblob 自检就原样报错，不落内存。
            try:
                key = wb_atrest.adopt_key(payload.get("keyText"),
                                          log=lambda m: _gw().log(m, tag="accounts"))
            except Exception as exc:
                _gw().log("at-rest key import rejected: %s" % exc, level="WARN", tag="accounts")
                return self._json_panel(200, {"ok": False, "msg": str(exc),
                                        "atrest": wb_atrest.status()})
            return self._json_panel(200, {
                "ok": True,
                "keyId": wb_atrest.derive_key_id(key),
                "msg": "已导入解码密钥（只留在内存里）",
                "atrest": wb_atrest.status(),
            })
        if payload.get("forgetKey"):
            wb_atrest.forget_key()
            return self._json_panel(200, {"ok": True, "atrest": wb_atrest.status()})
        if target_path:
            realm = payload.get("realm")
            try:
                account = _gw().POOL.import_desktop_credential(
                    path=target_path, realm=realm, source="desktop-app")
            except Exception as exc:
                return self._error(400, "import failed: %s" % exc)
            _gw().log("imported %s from %s (user confirmed)" % (account.uid[:8], os.path.basename(target_path)))
            return self._json_panel(200, {
                "imported": [account.public()],
                "accounts": _gw().account_views(),
            })
        if payload.get("all"):
            imported = _gw().import_desktop_accounts(payload.get("realm"))
            return self._json_panel(200, {
                "imported": [a.public() for a in imported],
                "accounts": _gw().account_views(),
            })
        return self._json_panel(200, {
            "detected": _gw().desktop_credential_scan(),
            "accounts": _gw().account_views(),
            "pool_uids": [a.uid for a in _gw().POOL.accounts],
            "atrest": wb_atrest.status(),
        })

    def _route_accounts_refresh(self, payload):
        uid = payload.get("uid")
        targets = [_gw().POOL.get(uid)] if uid else list(_gw().POOL.accounts)
        results = []
        for account in targets:
            if account is None:
                continue
            ok = account.refresh()
            account.save(_gw().ACCOUNTS_DIR)
            results.append({"uid": account.uid, "ok": ok, "error": account.last_error})
        return self._json_panel(200, {"results": results})

    def _route_accounts_sync_profile(self, payload):
        """Manual nickname refresh from the web console.

        Opt-in by nature: only runs when the operator asks for it. The
        upstream response carries phone numbers and other personal fields;
        wb_accounts.fetch_account_profile parses only uid/nickname.
        """
        uid = str(payload.get("uid") or "").strip()
        realm = payload.get("realm")
        if uid:
            targets = [_gw().POOL.get(uid)]
        elif realm and realm != "all":
            targets = [a for a in _gw().POOL.accounts if a.realm == realm]
        else:
            targets = list(_gw().POOL.accounts)
        updated = []
        failed = []
        for account in targets:
            if account is None:
                continue
            try:
                nickname = account.sync_nickname()
            except Exception as exc:
                failed.append({"uid": account.uid, "error": str(exc)[:200]})
                _gw().log("profile sync failed for %s: %s" % (account.uid[:8], exc),
                    level="WARN")
                continue
            updated.append({"uid": account.uid, "nickname": nickname})
            _gw().log("account %s: nickname synced from the web console"
                % account.uid[:8], level="INFO")
        return self._json_panel(200, {"updated": updated, "failed": failed,
                                "accounts": _gw().account_views()})

    def _route_accounts_test(self, payload):
        uid = payload.get("uid")
        if not uid:
            # 不带 uid 就是「一键测试」：一次看全区域里每个账号 + 本机连通性。
            return self._route_accounts_test_all(payload)
        account = _gw().POOL.get(uid)
        if not account:
            return self._error(404, "no such account")
        model = payload.get("model") or _gw().TEST_MODEL_DEFAULT
        row = _gw().probe_account(account, model)
        _gw().log("account test: uid=%s model=%s wall=%sms ok=%s%s"
            % (account.uid[:8], model, row.get("elapsed_ms"), row.get("ok"),
               "" if row.get("ok") else " error=%s" % (row.get("error") or "")),
            level="INFO" if row.get("ok") else "WARN", tag="accounts")
        # 单账号测试的返回体保持原样（面板与客户端都在用），只在失败时多带一个
        # network 标记：提示这是本机网络的问题，不是账号的问题。
        result = {"ok": row["ok"], "uid": row["uid"], "model": row["model"],
                  "elapsed_ms": row["elapsed_ms"]}
        if row["ok"]:
            result["reply"] = row["reply"]
        else:
            result["status"] = row["status"] if row["status"] is not None else 500
            result["error"] = row["error"]
            if row["network"]:
                result["network"] = True
        return self._json(200, result)

    def _route_accounts_test_all(self, payload):
        """一键测试：该区域每个已启用账号各打一条最小请求，外加一条本机连通性。

        面板上的「测试」原先只能一个个点，而池子出问题时最需要的偏偏是一次看全：
        哪个账号真不行、哪个只是被本地窗口（熔断 / 降权 / 软限流）关着——测试成功
        会顺手把这些计数清掉——以及本机网络通不通。断网时每个账号都会失败，只报
        「账号全挂了」的话，用户只能看着一屏红点猜。
        """
        realm = str(payload.get("realm") or "").strip().lower()
        if realm not in ("intl", "cn"):
            realm = _gw().CURRENT_REALM
        model = payload.get("model") or _gw().TEST_MODEL_DEFAULT
        targets = [a for a in (_gw().POOL.accounts if _gw().POOL else [])
                   if a.realm == realm and a.enabled]
        started = time.time()
        network = _gw().probe_local_network(realm)
        rows = []
        if targets:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(_gw().PROBE_CONCURRENCY, len(targets))) as pool:
                rows = list(pool.map(lambda acc: _gw().probe_account(acc, model), targets))
        passed = [r for r in rows if r.get("ok")]
        failed = [r for r in rows if not r.get("ok")]
        summary = {"realm": realm, "model": model, "tested": len(rows),
                   "passed": len(passed), "failed": len(failed),
                   "networkDown": not network.get("ok"),
                   "elapsed_ms": int((time.time() - started) * 1000)}
        _gw().log("test all: realm=%s model=%s tested=%d passed=%d failed=%d network=%s"
            % (realm, model, len(rows), len(passed), len(failed),
               "ok" if network.get("ok") else "DOWN"), tag="accounts")
        return self._json_panel(200, {"ok": True, "summary": summary, "network": network,
                                "results": rows, "accounts": _gw().account_views()})

    def _route_accounts_set(self, payload):
        uid = payload.get("uid")
        if not uid:
            return self._error(400, "uid required")
        # Each field is applied on its own so a caller can change one thing
        # without restating the others; at least one must be present.
        updated = None
        if "proxySlot" in payload:
            updated = _gw().POOL.set_proxy_slot(uid, payload.get("proxySlot"))
            if updated is None:
                return self._error(404, "no such account")
            _gw().log("account %s proxy slot set to %s"
                % (uid[:8], updated.get("proxySlot") or "(direct)"))
        if "proxy" in payload:
            updated = _gw().POOL.set_proxy(uid, payload.get("proxy"))
            if updated is None:
                return self._error(404, "no such account")
            _gw().log("account %s proxy set to %s"
                % (uid[:8], updated.get("proxy") or "(direct)"))
        if "enabled" in payload:
            updated = _gw().POOL.set_enabled(uid, bool(payload.get("enabled")))
            if updated is None:
                return self._error(404, "no such account")
            _gw().log("account %s %s"
                % (uid[:8], "enabled" if payload.get("enabled") else "disabled"))
        if "tasksEnabled" in payload:
            updated = _gw().POOL.set_tasks_enabled(uid, bool(payload.get("tasksEnabled")))
            if updated is None:
                return self._error(404, "no such account")
            _gw().log("account %s tasks %s"
                % (uid[:8], "enabled" if payload.get("tasksEnabled") else "disabled"))
        if "weight" in payload:
            try:
                weight = int(payload.get("weight"))
            except (TypeError, ValueError):
                return self._error(400, "weight must be an integer between 1 and 100")
            if not 1 <= weight <= 100:
                return self._error(400, "weight out of range (1-100)")
            updated = _gw().POOL.set_weight(uid, weight)
            if updated is None:
                return self._error(404, "no such account")
            _gw().log("account %s weight set to %d" % (uid[:8], weight))
        if "note" in payload:
            updated = _gw().POOL.set_note(uid, payload.get("note"))
            if updated is None:
                return self._error(404, "no such account")
            _gw().log("account %s note set (%d chars)"
                % (uid[:8], len(updated.get("note") or "")))
        if "disabledModels" in payload:
            # 人工黑名单：这些模型不再派给该账号，其他模型与别的账号都不受影响。
            # 传空数组 = 清空禁用。
            updated = _gw().POOL.set_disabled_models(uid, payload.get("disabledModels"))
            if updated is None:
                return self._error(404, "no such account")
            _gw().log("account %s disabled models: %s"
                % (uid[:8],
                   ",".join(updated.get("disabledModels") or []) or "(none)"))
        if updated is None:
            return self._error(
                400,
                "nothing to update: pass 'enabled', 'tasksEnabled', 'proxy', "
                "'proxySlot', 'weight' or 'note'"
            )
        return self._json_panel(200, {"account": updated})

    def _route_accounts_set_all(self, payload):
        _gw().POOL.set_all_enabled(bool(payload.get("enabled")))
        return self._json_panel(200, {"accounts": _gw().account_views()})

    def _route_accounts_delete(self, payload):
        uid = payload.get("uid")
        if not uid:
            return self._error(400, "uid required")
        removed = _gw().POOL.remove(uid)
        _gw().log("account %s deleted" % uid[:8])
        return self._json_panel(200, {"deleted": removed, "accounts": _gw().account_views()})

    def _route_accounts_add_api_key(self, payload):
        """新增静态 API Key 账号（CodeBuddy 平台签发，ck_/pt_ 前缀）。

        与 OAuth 扫码是两条并列的入口：静态 Key 无法走设备流（CodeBuddy 的
        登录页不支持企业微信扫码），只能手工粘贴。

        流程是「先验后落盘」：这类 Key 没有本地可校验的签名字段，唯一可靠的
        判据是拿它打一次企业额度端点。探测失败就 400 且不写任何文件——坏号
        进池之后选号侧会反复选中它、每次失败都要冷却换号，表象是「网关时好
        时坏」，比拒绝添加难查得多。
        """
        key = str(payload.get("key") or payload.get("accessToken") or "").strip()
        if not key:
            return self._error(400, "key is required", "invalid_request_error")
        if not wb_accounts.is_api_key_token(key):
            return self._error(
                400,
                "this is not a static API key: expected the ck_/pt_ prefix "
                "issued by the CodeBuddy API console",
                "invalid_request_error")
        nickname = str(payload.get("nickname") or "").strip()
        uid = str(payload.get("uid") or "").strip()
        try:
            account = wb_accounts.Account({
                "uid": uid,
                "nickname": nickname,
                "accessToken": key,
                "realm": "cn",
                "platform": "CLI",
                "source": wb_accounts.SOURCE_API_KEY,
                "enabled": True,
                "tasksEnabled": False,
            })
        except Exception as exc:
            return self._error(400, "cannot build account: %s" % exc,
                               "invalid_request_error")
        if not account.uid:
            return self._error(
                400,
                "cannot derive a uid from this key; pass uid explicitly",
                "invalid_request_error")
        probe = account.probe_api_key()
        if not probe.get("ok"):
            return self._error(
                400,
                "the key did not pass the balance probe, so it was not saved: %s"
                % (probe.get("error") or "unknown error"),
                "invalid_request_error")
        existed = _gw().POOL.get(account.uid) is not None if _gw().POOL else False
        _gw().POOL.add(account)
        try:
            account.fetch_credits()
        except Exception:
            # 额度读失败不影响账号可用性：add() 已落盘，下一个刷新周期会补。
            pass
        _gw().log("account added (api key): uid=%s nickname=%r probe=ok(%s/%s)"
            % (account.uid[:8], account.nickname,
               probe.get("used"), probe.get("limit")), tag="accounts")
        return self._json_panel(200, {
            "ok": True,
            "uid": account.uid,
            "nickname": account.nickname or account.uid[:8],
            "updated": existed,
            "probe": probe,
            "account": account.public(),
        })

    def _route_accounts_import(self, payload):
        # Import a previously exported document (or any hand-written list
        # of accounts). Body shapes accepted, see wb_accounts._coerce_account_rows:
        #   {"format":"workbuddy-accounts","accounts":[...]}   <- our export
        #   [...]                                              <- bare list
        #   {"accessToken": ...}                               <- single account
        #   {"account":{...},"auth":{...}}                     <- desktop credential
        #
        # Options:
        #   dryRun    (bool) - validate and report, write nothing
        #   overwrite (bool) - replace accounts whose uid already exists
        #   realm     ("intl"|"cn") - force a realm instead of detecting it
        #
        # `data` carries the document. It is preferred over the bare body so
        # the body can also hold the options above.
        blob = payload.get("data") if "data" in payload else payload
        if not isinstance(blob, (dict, list)):
            return self._error(400, "the document must be a JSON object or array",
                               "invalid_request_error")
        rows, problem = wb_accounts._coerce_account_rows(blob)
        if problem:
            return self._error(400, "cannot read the document: %s" % problem,
                               "invalid_request_error")
        dry_run = bool(payload.get("dryRun"))
        overwrite = bool(payload.get("overwrite"))
        forced_realm = (payload.get("realm") or "").strip().lower() or None
        if forced_realm and forced_realm not in ("intl", "cn"):
            return self._error(400, "realm must be intl or cn", "invalid_request_error")
        if dry_run:
            # Validate every row without touching the pool so the caller can
            # see exactly what an import would do before committing to it.
            # Shares its rules with the real import, so the preview cannot
            # disagree with what would actually happen.
            return self._json_panel(200, {
                "dryRun": True,
                "count": len(rows),
                "result": _gw().POOL.preview_import_rows(rows, realm=forced_realm, overwrite=overwrite),
                "accounts": _gw().account_views(),
            })
        report = _gw().POOL.import_rows(rows, realm=forced_realm, overwrite=overwrite)
        _gw().log("account import: %d added, %d updated, %d skipped, %d invalid"
            % (len(report["added"]), len(report["updated"]),
               len(report["skipped"]), len(report["invalid"])))
        return self._json_panel(200, {
            "count": len(rows),
            "result": report,
            "accounts": _gw().account_views(),
        })
