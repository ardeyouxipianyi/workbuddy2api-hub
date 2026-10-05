"""wb_scheduler.py —— 后台定时调度器 (Scheduler)

负责常驻后台自动执行：
1. Token 保活 (Keepalive)：定期检查 Token 剩余寿命，不足 2 小时自动调用 Refresh Token。
2. 每日签到 (Daily Checkin)：每日定时为所有国内版账号自动签到领积分。
3. 猫猫旅行与日常结算 (Cat Travel & Welfare)：自动派出猫猫旅行或领取归来奖励。
4. 状态持久化与看板展示：暴露状态、执行记录、支持手动立即触发与开关切换。
"""
import threading
import time
import wb_settings
import wb_tasks
from wb_tasks import do_cat_travel


class Scheduler:
    def __init__(self, pool, task_queue=None):
        self.pool = pool
        # Optional task-center queue: the daily growth run reuses it so the
        # manual "run queue" and the 01:00 automatic run share one state.
        self.task_queue = task_queue
        # 对齐 Sliverkiss/workbuddy2api 官方默认排程 (CST 24小时制)
        self.checkin_hours = [9, 21]     # 每日 09:00、21:00 签到
        self.travel_hours = [9, 21]      # 每日 09:00 派出、21:00 领奖闭环
        self.keepalive_hours = [22]      # 每日 22:00 集中 Token 保活检查
        # Panel parity (C5): the night window is 23:00-08:00; 23:00 catches the
        # start of the window and 01:00 stays as the existing make-up point.
        self.cat_hours = [1, 23]         # 每日 01:00、23:00 夜猫子专属任务
        self.daily_chat_hours = [9, 21]  # 国际版每日活跃打卡
        self.growth_hours = [1]          # 每日 01:00 成长任务队列（Sequential 解锁）
        self.checkin_enabled = True
        self.travel_enabled = True
        self.keepalive_enabled = True
        self.cat_enabled = True
        self.daily_chat_enabled = True
        self.growth_enabled = True
        # Panel parity: when false, scheduled tasks skip disabled accounts
        # (the gateway's existing behaviour is to include them).
        self.include_disabled_in_tasks = True
        self.balance_refresh_enabled = False
        self.balance_refresh_minutes = 5
        self._last_balance_refresh = 0.0
        # uid -> date: the streak bonus runs once per account per day.
        self._streak_bonus_done = {}
        self.all_hours = sorted(list(set(self.checkin_hours + self.travel_hours
                                          + self.keepalive_hours + self.cat_hours
                                          + self.daily_chat_hours
                                          + self.growth_hours)))
        self.enabled = True
        self._stop_event = threading.Event()
        self._thread = None
        self.last_run_time = None
        self.next_run_time = None
        self.logs = []
        # Guards against overlapping runs: trigger_now() spawns a thread per
        # click, and a manual trigger can also land on top of the hourly job.
        self._run_lock = threading.Lock()
        self._calc_next_fire()
        self.apply_settings()
        # Surface task-level failures (dead endpoints, upstream shape changes)
        # in the same log the panel shows.
        wb_tasks.set_logger(self.log)

    def log(self, msg):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{ts}] {msg}"
        self.logs.append(entry)
        if len(self.logs) > 60:
            self.logs = self.logs[-60:]
        try:
            import wb_proxy
            wb_proxy.add_log_entry(f"[调度器] {msg}", tag="scheduler")
        except Exception:
            pass

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self.log("后台定时调度器已启动")

    def stop(self):
        self._stop_event.set()
        self.log("后台定时调度器已暂停")

    def _run_loop(self):
        # 启动后先休眠 10 秒等待主服务就绪，然后执行初次检查
        time.sleep(10)
        try:
            self._execute_cycle("启动初次初始化巡检")
        except Exception as exc:
            self.log(f"初次巡检异常: {exc}")

        while not self._stop_event.is_set():
            self._calc_next_fire()
            # 每 30 秒检查一次当前整点
            now = time.localtime()
            cur_hour = now.tm_hour
            cur_min = now.tm_min
            if self.enabled:
                # 到达设定的整点前 1 分钟内触发
                if cur_min == 0 and cur_hour in self.all_hours:
                    reason = f"整点排程命中 ({cur_hour}:00)"
                    try:
                        self._execute_cycle(reason)
                    except Exception as exc:
                        self.log(f"排程执行异常: {exc}")
                    time.sleep(65) # 避开当前这一分钟重复触发
            if self.enabled and self.balance_refresh_enabled:
                try:
                    self._maybe_refresh_balances()
                except Exception as exc:
                    self.log(f"余额巡检异常: {exc}")
            self._stop_event.wait(30)

    def _calc_next_fire(self):
        if not self.all_hours:
            self.next_run_time = None
            return
        now = time.localtime()
        cur_h = now.tm_hour
        next_h = None
        for h in self.all_hours:
            if h > cur_h or (h == cur_h and now.tm_min == 0 and now.tm_sec < 10):
                next_h = h
                break
        if next_h is not None:
            # 今天
            t_struct = time.struct_time((now.tm_year, now.tm_mon, now.tm_mday, next_h, 0, 0, 0, 0, -1))
        else:
            # 明天第一个小时
            t_tomorrow = time.time() + 86400
            now_tom = time.localtime(t_tomorrow)
            first_h = self.all_hours[0]
            t_struct = time.struct_time((now_tom.tm_year, now_tom.tm_mon, now_tom.tm_mday, first_h, 0, 0, 0, 0, -1))
        self.next_run_time = time.strftime("%Y-%m-%d %H:%M:%S", t_struct)

    def apply_settings(self):
        """Load panel-managed schedule settings and recompute fire hours."""
        if not self.pool or not getattr(self.pool, "dir", None):
            return None
        try:
            cfg = wb_settings.schedule_config(self.pool.dir)
        except Exception as exc:
            self.log(f"排程設定讀取失敗: {exc}")
            return None
        self.checkin_hours = list(cfg["checkin_hours"])
        self.travel_hours = list(cfg["travel_hours"])
        self.keepalive_hours = list(cfg["keepalive_hours"])
        self.cat_hours = list(cfg["cat_hours"])
        self.daily_chat_hours = list(cfg["daily_chat_hours"])
        self.growth_hours = list(cfg["growth_hours"])
        self.checkin_enabled = bool(cfg["checkin_enabled"])
        self.travel_enabled = bool(cfg["travel_enabled"])
        self.keepalive_enabled = bool(cfg["keepalive_enabled"])
        self.cat_enabled = bool(cfg["cat_enabled"])
        self.daily_chat_enabled = bool(cfg["daily_chat_enabled"])
        self.growth_enabled = bool(cfg["growth_enabled"])
        self.include_disabled_in_tasks = bool(cfg["include_disabled_in_tasks"])
        self.balance_refresh_enabled = bool(cfg["balance_refresh_enabled"])
        self.balance_refresh_minutes = int(cfg["balance_refresh_minutes"])
        hours = set()
        if self.checkin_enabled:
            hours.update(self.checkin_hours)
        if self.travel_enabled:
            hours.update(self.travel_hours)
        if self.keepalive_enabled:
            hours.update(self.keepalive_hours)
        if self.cat_enabled:
            hours.update(self.cat_hours)
        if self.daily_chat_enabled:
            hours.update(self.daily_chat_hours)
        if self.growth_enabled:
            hours.update(self.growth_hours)
        self.all_hours = sorted(hours)
        self._calc_next_fire()
        return cfg

    def _account_eligible(self, account):
        """Whether scheduled tasks touch this account."""
        return bool(self.include_disabled_in_tasks
                    or getattr(account, "enabled", True))

    def _maybe_refresh_balances(self):
        """Run the periodic balance scan when its interval has elapsed."""
        interval = max(1, int(self.balance_refresh_minutes or 5)) * 60
        now = time.time()
        if now - self._last_balance_refresh < interval:
            return False
        self._last_balance_refresh = now
        self._refresh_balances()
        return True

    def _refresh_balances(self):
        accounts = list(self.pool.accounts) if self.pool else []
        refreshed = 0
        for index, account in enumerate(accounts):
            try:
                res = account.fetch_credits()
                if isinstance(res, dict) and res.get("ok"):
                    refreshed += 1
            except Exception as exc:
                self.log("余额刷新失败 %s: %s" % (str(getattr(account, "uid", "?"))[:8], exc))
            if index + 1 < len(accounts):
                time.sleep(0.3)
        self.log("余额巡检完成：%d/%d 个账号刷新" % (refreshed, len(accounts)))
        return refreshed

    def trigger_now(self):
        """手动立即触发一次调度检查。"""
        if self._run_lock.locked():
            return {"ok": False, "msg": "已有巡检正在执行，请稍候再试"}
        threading.Thread(target=self._execute_cycle, args=("手动立即触发",), daemon=True).start()
        return {"ok": True, "msg": "已触发后台调度执行"}

    def _execute_cycle(self, trigger_reason="周期巡检"):
        if not self._run_lock.acquire(blocking=False):
            self.log(f"跳过本次巡检 ({trigger_reason})：上一轮仍在执行")
            return
        try:
            self._run_cycle(trigger_reason)
        finally:
            self._run_lock.release()

    def _maybe_streak_bonus(self, account, uid8):
        """Run the idempotent streak bonus at most once per account per day."""
        today = time.strftime("%Y-%m-%d")
        if self._streak_bonus_done.get(account.uid) == today:
            return
        try:
            bonus = wb_tasks.run_streak_bonus(account)
        except Exception as exc:
            self.log(f"! 账号 [{uid8}] 连登管家异常: {exc}")
            return
        self._streak_bonus_done[account.uid] = today
        for line in bonus.get("logs") or []:
            self.log(f"🎁 [{uid8}] {line}")

    def _run_cycle(self, trigger_reason="周期巡检"):
        self.last_run_time = time.strftime("%Y-%m-%d %H:%M:%S")
        self.log(f"开始执行任务 ({trigger_reason})...")
        if not self.pool or not self.pool.accounts:
            self.log("暂无可用的活跃账号，跳过本次巡检")
            return

        refreshed_count = 0
        checkin_count = 0
        travel_count = 0
        daily_chat_count = 0
        # Each family below is gated by its own hour list, not just by the
        # wake-up union: otherwise checkin/travel/keepalive/daily_chat ran at
        # whichever family's hour happened to fire first (audit #8).
        now_hour = time.localtime().tm_hour

        for acc in list(self.pool.accounts):
            if not self._account_eligible(acc):
                continue
            uid8 = acc.uid[:8] if acc.uid else "?"
            # 1. 检查 Token 剩余寿命 (小于 2 小时自动刷新保活)
            if self.keepalive_enabled and now_hour in self.keepalive_hours:
                exp = acc.expires_at or 0
                if exp and (exp - time.time()) < 7200:
                    self.log(f"账号 [{uid8}] Token 即将到期，执行主动保活刷新...")
                    if acc.refresh():
                        refreshed_count += 1
                        self.log(f"✓ 账号 [{uid8}] Token 自动保活刷新成功")
                    else:
                        self.log(f"! 账号 [{uid8}] Token 保活刷新失败: {acc.last_error}")

            # 2. 如果是国内版账号，检查每日签到与猫猫旅行
            if acc.realm == "cn":
                if (self.checkin_enabled and now_hour in self.checkin_hours
                        and acc.can_checkin()):
                    self.log(f"检测到国内版账号 [{uid8}] 今日尚未签到，执行自动签到...")
                    # C6: read the streak before/after so a 200 that silently
                    # fails to register is visible instead of looking fine.
                    before_days = wb_tasks.fetch_streak_days(acc)
                    res = acc.checkin()
                    if res.get("ok"):
                        checkin_count += 1
                        self.log(f"✓ 账号 [{uid8}] 自动签到成功: {res.get('msg')}")
                        after_days = wb_tasks.fetch_streak_days(acc)
                        if (before_days is not None and after_days is not None
                                and after_days <= before_days):
                            self.log(f"! 账号 [{uid8}] 签到返回成功但连登天数未增加"
                                     f"（{before_days} -> {after_days}），上游可能静默丢失")
                    else:
                        self.log(f"! 账号 [{uid8}] 自动签到未成功: {res.get('error') or res.get('msg')}")
                    time.sleep(1.0)
                # C3: 連登管家（每日一次）——補簽、兌換解鎖檔位、抽完所有次數。
                if self.checkin_enabled and now_hour in self.checkin_hours:
                    self._maybe_streak_bonus(acc, uid8)

                # 检查猫猫旅行
                if self.travel_enabled and now_hour in self.travel_hours:
                    tr = do_cat_travel(acc)
                    if tr.get("action") in ("claim", "depart"):
                        travel_count += 1
                        self.log(f"🐱 账号 [{uid8}] 猫猫日常处理: {tr.get('msg')}")
                    time.sleep(1.0)

                # 01:00 夜猫子专属任务: black_cat 只在 23:00-08:00 上报计数,
                # 之前这个整点只是空转通用巡检, 从未真正上报过夜猫事件。
                if self.cat_enabled and now_hour in self.cat_hours:
                    night = wb_tasks.run_night_growth(acc)
                    for line in night.get("logs", []):
                        self.log(f"🌙 {line}")
                    time.sleep(1.0)

            # 3. 如果是国际版账号，检查每日活跃对话 (送 30/50 积分福利)
            if (acc.realm == "intl" and self.daily_chat_enabled
                    and now_hour in self.daily_chat_hours):
                if acc.can_daily_chat():
                    self.log(f"检测到国际版账号 [{uid8}] 今日尚未活跃，执行每日活跃打卡对话...")
                    res = acc.daily_chat()
                    if res.get("ok"):
                        daily_chat_count += 1
                        self.log(f"✓ 账号 [{uid8}] 每日活跃对话成功")
                    else:
                        self.log(f"! 账号 [{uid8}] 每日活跃对话失败: {res.get('error') or res.get('msg')}")
                    time.sleep(1.5)

        # C4: 每日 01:00（可配）自動跑一次任務中心佇列：掃描全部待辦 →
        # 帳號內串行/帳號間併發執行；Sequential 族每日零點解鎖一環，這裡自然接上。
        if (self.growth_enabled and self.task_queue is not None
                and now_hour in self.growth_hours):
            try:
                started = self.task_queue.start()
            except Exception as exc:
                self.log(f"! 成长任务队列启动失败: {exc}")
            else:
                if started.get("started"):
                    self.log(f"📋 每日成长任务队列已启动（{started.get('total')} 项）")
                else:
                    self.log(f"📋 每日成长任务队列未启动: {started.get('msg')}")

        self.log(f"巡检完成：Token保活 {refreshed_count} 个，国内签到 {checkin_count} 个，猫猫日常 {travel_count} 个，国际活跃 {daily_chat_count} 个")

    def status(self):
        return {
            "enabled": self.enabled,
            "checkin_hours": list(self.checkin_hours),
            "travel_hours": list(self.travel_hours),
            "keepalive_hours": list(self.keepalive_hours),
            "cat_hours": list(self.cat_hours),
            "daily_chat_hours": list(self.daily_chat_hours),
            "growth_hours": list(self.growth_hours),
            "include_disabled_in_tasks": self.include_disabled_in_tasks,
            "balance_refresh": {"enabled": self.balance_refresh_enabled,
                                "minutes": self.balance_refresh_minutes},
            "mode": "整点排程 (09:00/21:00 签到旅行 · 22:00 保活 · 01:00/23:00 夜猫)",
            "mode_cn": "整点排程 (09:00/21:00 签到旅行 · 22:00 保活 · 01:00/23:00 夜猫)",
            "mode_intl": "账号 Token 自动保活与凭证常驻 (每日 22:00 集中巡检)",
            "last_run_time": self.last_run_time or "尚未运行",
            "next_run_time": self.next_run_time or "待调度",
            "logs": self.logs[-20:],
        }
