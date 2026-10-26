"""Panel routes for background operations: growth tasks, self-update, and the
per-account proxy slots.

`OpsRoutes` is a mixin of wb_proxy.Handler. The work itself lives in wb_tasks,
wb_updates / wb_update_request and wb_egress; these handlers expose it to the
panel.
"""

import time

import wb_runtime
import wb_settings
import wb_update_request
import wb_updates

_gw = wb_runtime.gateway


class OpsRoutes(object):
    def _get_tasks(self, query):
        if not self._authorized():
            return
        cn_accounts = _gw().POOL.task_accounts("cn") if _gw().POOL else []
        if not cn_accounts:
            return self._json_panel(200, {"tasks": [], "summary": {}, "accounts": [], "msg": "未找到参与任务的国内版账号"})
        uid = (query.get("uid") or [None])[0]
        acc = None
        if uid and uid != "all":
            target = _gw().POOL.get(uid) if _gw().POOL else None
            if target and target.realm == "cn":
                acc = target
        if not acc:
            acc = cn_accounts[0]
        tasks, summary = _gw().growth_snapshot(acc)
        acct_list = [{"uid": a.uid, "nickname": a.nickname or a.uid[:8]} for a in cn_accounts]
        return self._json_panel(200, {
            "tasks": tasks,
            "summary": summary,
            "account": acc.public(),
            "accounts": acct_list,
        })

    def _get_updates(self):
        """What the running build is, and whether a newer stable release exists.

        Read-only and cheap: it reports the last check's outcome, never runs
        one. The panel's "check now" is the POST below.
        """
        if not self._authorized():
            return
        if _gw().UPDATES:
            payload = _gw().UPDATES.status()
        else:
            payload = {
                "current_version": _gw().running_version(),
                "latest_version": None, "update_available": False,
                "enabled": wb_settings.update_check_enabled(_gw().ACCOUNTS_DIR),
                "checking": False, "last_attempt": None, "last_success": None,
                "last_error": "", "release_url": "", "latest_tag": "",
                "published_at": "",
                "msg": "更新检查未运行",
            }
        payload.update(wb_update_request.status())
        return self._json(200, payload)

    def _get_update_backups(self):
        """What a rollback could restore, for the panel's rollback card.

        Read-only: it lists the backups the registered executor kept, says which
        ones are complete (and why not, when they are not), and reports the
        retention setting so the card can explain an empty list. The panel never
        sees a path - only versions, sizes and times.
        """
        if not self._authorized():
            return
        try:
            payload = wb_update_request.backups()
        except Exception as exc:
            payload = {"source": "", "backups": [],
                       "msg": "无法读取备份列表: %s" % str(exc)[:120]}
        payload["current_version"] = _gw().running_version()
        payload["keep_backups"] = wb_settings.keep_backups(_gw().ACCOUNTS_DIR)
        payload["keep_backups_max"] = wb_settings.MAX_KEEP_BACKUPS
        payload["rollback_enabled"] = wb_update_request.install_enabled()
        return self._json(200, payload)

    def _handle_proxy_slots(self, path, payload):
        """Proxy-slot management (panel-authenticated)."""
        if path == "/proxy/slots":
            return self._json(200, {"slots": _gw().proxy_slots_view()})
        if path == "/proxy/slots/save":
            raw = payload.get("slots")
            if not isinstance(raw, list):
                return self._error(400, "slots must be a list", "invalid_request_error")
            cleaned = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                # Normalized by the same helper the store uses, so the probed
                # exit fields the panel echoes back survive a save instead of
                # being dropped by a second, hand-written field list here.
                entry = wb_settings._clean_slot_entry(item)
                if entry is not None:
                    cleaned.append(entry)
            saved = wb_settings.set_proxy_slots(_gw().ACCOUNTS_DIR, cleaned)
            if _gw().POOL:
                # A slot may have been removed: unbind anyone still naming it
                # before recomputing, so a stale id cannot survive.
                dropped = wb_settings.drop_missing_bindings(_gw().POOL, saved)
                _gw().POOL.apply_proxy_slots(saved)
                if dropped:
                    _gw().log("proxy slots: unbound %d account(s) from removed slots"
                        % dropped)
            _gw().log("proxy slots saved: %d slot(s)" % len(saved))
            return self._json(200, {"slots": _gw().proxy_slots_view()})
        if path == "/proxy/slots/test":
            slot_id = str(payload.get("id") or "").strip()
            slot = wb_settings.find_proxy_slot(_gw().ACCOUNTS_DIR, slot_id)
            if slot is None:
                return self._error(404, "no such proxy slot")
            probe = _gw().probe_proxy_intel(slot["url"])
            reply = dict(probe)
            reply["id"] = slot_id
            reply["slot"] = None
            if probe["ok"]:
                # Remember what the exit turned out to be, so the panel shows it
                # without probing again. `ok` covers the IP probe only: when the
                # geo lookup came back with nothing, the exit info already stored
                # is kept rather than overwritten with blanks - a blip at the
                # lookup must not lose what the last good probe learned.
                updated_fields = {
                    "ip": probe["exit_ip"],
                    "probed_at": int(time.time()),
                }
                if probe.get("intel_ok"):
                    updated_fields.update({
                        "country": probe["country"],
                        "country_code": probe["country_code"],
                        "ip_type": probe["ip_type"],
                        "isp": probe["isp"],
                        "asn": probe["asn"],
                    })
                else:
                    _gw().log("proxy slots: %s probed %s but the geo lookup returned "
                        "nothing; keeping the exit info already stored"
                        % (slot_id, probe["exit_ip"] or "-"))
                # The name is deliberately left alone. An empty name is how the
                # store marks a slot as auto-named ("label this slot by its
                # exit"), and slot_label() derives that label from the country
                # and kind it shows - writing the derived text into the name
                # would freeze it to the exit it happened to have at the time.
                updated = wb_settings.update_proxy_slot(
                    _gw().ACCOUNTS_DIR,
                    slot_id,
                    updated_fields,
                )
                reply["slot"] = updated
                reply["name"] = (updated or {}).get("name", "")
            return self._json(200, reply)
        if path == "/proxy/discover":
            return self._json(200, {"candidates": _gw().discover_proxy_slots()})
        return self._error(404, "not found", "invalid_request_error")

    def _route_tasks_run(self, payload):
        if not _gw().POOL:
            return self._json_panel(200, {"ok": False, "msg": "账号池不可用"})
        _gw().invalidate_tasks_cache()          # 跑完任务状态就变了，别再端旧快照
        uid = payload.get("uid")
        if uid and uid != "all":
            target = _gw().POOL.get(uid)
            if not target or target.realm != "cn":
                return self._json_panel(200, {"ok": False, "msg": "未找到指定的国内版账号"})
            targets = [target]
        else:
            targets = _gw().POOL.task_accounts("cn")
        if not targets:
            return self._json_panel(200, {"ok": False, "msg": "未找到参与任务的国内版账号"})
        from wb_tasks import run_growth_tasks
        claim = payload.get("claim", True) is not False
        combined_logs = []
        total_credit = 0
        for i, acc in enumerate(targets):
            uid_str = acc.uid[:8] if acc.uid else "?"
            nick = acc.nickname or uid_str
            combined_logs.append(f"====== 正在为账号 [{nick} ({acc.uid})] 执行全自动成长任务 ({i+1}/{len(targets)}) ======")
            res = run_growth_tasks(acc, gap=1.0, claim=claim)
            # run_growth_tasks() reports its total as "earned_credit";
            # reading the old "credit_added" name silently summed zeros
            # and the dashboard always showed "+0 积分".
            total_credit += res.get("earned_credit") or 0
            for l in res.get("logs") or []:
                combined_logs.append(f"  {l}")
            if i < len(targets) - 1:
                time.sleep(1.5)
        combined_logs.append(f"====== 全部 {len(targets)} 个账号任务执行完毕，累计新增积分: +{total_credit} ======")
        _gw().add_log_entry(f"[任务福利] 批量成长任务执行完成，共 {len(targets)} 个账号，新增积分 +{total_credit}", tag="tasks")
        return self._json_panel(200, {
            "ok": True,
            "credit_added": total_credit,
            "logs": combined_logs,
            "accounts_count": len(targets)
        })

    def _route_tasks_claim(self, payload):
        if not _gw().POOL:
            return self._json_panel(200, {"ok": False, "msg": "账号池不可用"})
        uid = payload.get("uid")
        code = str(payload.get("code") or "").strip()
        if not uid or uid == "all":
            return self._json_panel(200, {"ok": False, "msg": "请先选择一个国内版账号"})
        if not code:
            return self._json_panel(200, {"ok": False, "msg": "缺少任务代码"})
        target = _gw().POOL.get(uid)
        if not target or target.realm != "cn":
            return self._json_panel(200, {"ok": False, "msg": "未找到指定的国内版账号"})
        from wb_tasks import claim_task
        res = claim_task(target, code)
        _gw().invalidate_tasks_cache()
        return self._json_panel(200, {
            "ok": bool(res.get("ok")),
            "msg": res.get("msg") or ("领取成功" if res.get("ok") else "领取失败"),
            "credit": res.get("credit", 0),
        })

    def _route_tasks_streak_bonus(self, payload):
        if not _gw().POOL:
            return self._json_panel(200, {"ok": False, "msg": "账号池不可用"})
        _gw().invalidate_tasks_cache()
        uid = payload.get("uid")
        if uid and uid != "all":
            target = _gw().POOL.get(uid)
            if not target or target.realm != "cn":
                return self._json_panel(200, {"ok": False, "msg": "未找到指定的国内版账号"})
            targets = [target]
        else:
            targets = _gw().POOL.task_accounts("cn")
        if not targets:
            return self._json_panel(200, {"ok": False, "msg": "未找到参与任务的国内版账号"})
        from wb_tasks import run_streak_bonus
        results = []
        for i, acc in enumerate(targets):
            uid_str = acc.uid[:8] if acc.uid else "?"
            nick = acc.nickname or uid_str
            res = run_streak_bonus(acc)
            results.append(f"{nick}: {res.get('msg')}")
            _gw().add_log_entry(f"[任务福利] 账号 [{nick}] 连登管家与抽奖: {res.get('msg')}", tag="tasks")
            if i < len(targets) - 1:
                time.sleep(1.0)
        return self._json_panel(200, {"ok": True, "msg": chr(10).join(results)})

    def _route_tasks_travel(self, payload):
        if not _gw().POOL:
            return self._json_panel(200, {"ok": False, "msg": "账号池不可用"})
        _gw().invalidate_tasks_cache()          # 同上：旅行会改任务/体力状态
        uid = payload.get("uid")
        if uid and uid != "all":
            target = _gw().POOL.get(uid)
            if not target or target.realm != "cn":
                return self._json_panel(200, {"ok": False, "msg": "未找到指定的国内版账号"})
            targets = [target]
        else:
            targets = _gw().POOL.task_accounts("cn")
        if not targets:
            return self._json_panel(200, {"ok": False, "msg": "未找到参与任务的国内版账号"})
        from wb_tasks import do_cat_travel
        results = []
        for i, acc in enumerate(targets):
            uid_str = acc.uid[:8] if acc.uid else "?"
            nick = acc.nickname or uid_str
            res = do_cat_travel(acc)
            results.append({
                "uid": acc.uid,
                "nickname": nick,
                "action": res.get("action"),
                "msg": res.get("msg") or "",
                # do_cat_travel() returns the amount as "credit".
                "reward_credit": res.get("credit", 0)
            })
            if i < len(targets) - 1:
                time.sleep(1.0)
        summary_msg = chr(10).join([f"{r['nickname']}: {r['msg']}" for r in results])
        for r in results:
            _gw().add_log_entry(f"[任务福利] 账号 [{r['nickname']}] 猫猫旅行: {r['msg']}", tag="tasks")
        return self._json_panel(200, {
            "ok": True,
            "results": results,
            "msg": summary_msg,
            "accounts_count": len(targets)
        })

    def _route_update_check(self):
        """Run one release check now, on the operator's explicit request.

        Inline rather than "started, poll /updates": it is a single bounded
        GitHub request, and the answer is what the button is for. `manual=True`
        ignores both the daily switch and the 24h window.
        """
        if not _gw().UPDATES:
            return self._json(200, {"ok": False, "msg": "更新检查未运行"})
        status = _gw().UPDATES.check(manual=True)
        status.update(wb_update_request.status())
        status["ok"] = True
        return self._json(200, status)

    def _route_update_install(self):
        """Queue the checked stable release for the root-owned systemd worker."""
        if not _gw().UPDATES:
            return self._json(503, {"ok": False, "msg": "更新检查未运行"})
        status = _gw().UPDATES.status()
        if status.get("last_error"):
            return self._json(409, {"ok": False, "msg": "请先成功检查更新"})
        if not status.get("update_available"):
            return self._json(409, {"ok": False, "msg": "当前没有可安装的新版本"})
        if not status.get("latest_tag") or not status.get("release_url"):
            return self._json(409, {"ok": False, "msg": "请先点击立即检查更新"})
        try:
            host = self.server.server_address[0]
            lan = host not in ("127.0.0.1", "::1", "localhost")
            queued = wb_update_request.request_install(
                status["latest_tag"], status["current_version"],
                port=self.server.server_port, lan=lan,
                repo=wb_updates.configured_repository())
        except wb_update_request.UpdateRequestError as exc:
            return self._json(409, {"ok": False, "msg": str(exc)})
        queued["ok"] = True
        return self._json(202, queued)

    def _route_update_rollback(self, payload):
        """Restore one retained backup, on the operator's explicit request.

        409 with a Chinese reason for everything the panel could have got wrong:
        an unknown version, a half-written backup, or a task already in flight.
        """
        version = str(payload.get("version") or "").strip()
        if not version:
            return self._json(409, {"ok": False, "msg": "请选择要回退的版本"})
        try:
            host = self.server.server_address[0]
            lan = host not in ("127.0.0.1", "::1", "localhost")
            queued = wb_update_request.request_rollback(
                version, _gw().running_version(),
                port=self.server.server_port, lan=lan)
        except wb_update_request.UpdateRequestError as exc:
            return self._json(409, {"ok": False, "msg": str(exc)})
        queued["ok"] = True
        return self._json(202, queued)
