# -*- coding: utf-8 -*-
"""wb_taskqueue.py — 任務中心：全帳號掃描 + 執行佇列（panel taskcenter.go）。

語義：
  - scan()：只讀；並發拉取每個國內版帳號的成長任務列表，匯總「未完成且可
    自動化」的待辦（已領取 / 上游鎖定 / 不可自動化 / 無動作的任務不入列）。
  - start()：把待辦按帳號分組排隊執行；帳號內串行（per-account 鎖，與其他
    任務動作互斥），帳號間並發（concurrency 1–3，預設 1）。狀態可輪詢。
  - status()：running / seq / started_at / items（pending|running|done|error|skipped）。

runner 可注入（測試/複用），預設 wb_tasks.run_single_task。純標準庫。
"""

import threading
import time

import wb_tasks


class TaskQueue(object):
    def __init__(self, pool, runner=None, concurrency=1, log=None):
        self.pool = pool
        self.runner = runner or wb_tasks.run_single_task
        self._default_concurrency = self.clamp_concurrency(concurrency)
        self._log = log or (lambda msg: None)
        self._lock = threading.RLock()
        self._items = []
        self._running = False
        self._starting = False
        self._started_at = 0.0
        self._seq = 0
        self._account_locks = {}

    @staticmethod
    def clamp_concurrency(value):
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = 1
        return max(1, min(3, number))

    def account_lock(self, uid):
        """Per-account mutex shared by the queue and single-task actions."""
        with self._lock:
            lock = self._account_locks.get(uid)
            if lock is None:
                lock = threading.Lock()
                self._account_locks[uid] = lock
            return lock

    @staticmethod
    def pending(task):
        """未完成且可自動化（panel growthPending 同口徑）。"""
        code = str(task.get("task_code") or "")
        if task.get("claimed") or task.get("status") == "claimed":
            return False
        if task.get("locked"):
            return False
        if task.get("unforgeable"):
            return False
        spec = wb_tasks.TASK_SPECS.get(code)
        if not spec or spec.get("unforgeable"):
            return False
        if code in wb_tasks.NIGHT_TASK_CODES and not wb_tasks.in_night_window():
            return False
        return True

    def scan(self, uids=None):
        """Read-only scan of every enabled CN account (concurrent fetch)."""
        wanted = set(uids or [])
        accounts = [a for a in self.pool.accounts
                    if a.realm == "cn" and getattr(a, "enabled", True)
                    and (not wanted or a.uid in wanted)]
        results = []
        results_lock = threading.Lock()
        # A scan gives up on a worker after 60s; bumping the token under the
        # same lock makes any late worker drop its result instead of writing
        # into the list this scan is about to return (audit #10).
        state = {"token": object()}
        token = state["token"]

        def worker(account):
            tasks = wb_tasks.fetch_growth_tasks(account)
            # C7: mp-only tasks (Sequential family) live behind the miniprogram
            # header; merge them by code so the queue sees the whole to-do list.
            try:
                mp_tasks = wb_tasks.fetch_growth_tasks(account, mp=True)
            except Exception:
                mp_tasks = []
            seen = {t.get("task_code") for t in tasks}
            for task in mp_tasks:
                if task.get("task_code") not in seen:
                    tasks.append(task)
            pending = [t for t in tasks if self.pending(t)]
            item = {
                "uid": account.uid,
                "nickname": account.nickname or (account.uid[:8] if account.uid else "?"),
                "growth": pending,
                "growth_error": "" if tasks else "无法获取任务清单",
            }
            with results_lock:
                if state["token"] is not token:
                    return
                results.append(item)

        threads = []
        for account in accounts:
            thread = threading.Thread(target=worker, args=(account,), daemon=True)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join(timeout=60)
        timed_out = any(thread.is_alive() for thread in threads)
        with results_lock:
            if timed_out:
                state["token"] = None
            snapshot = list(results)
        order = {account.uid: index for index, account in enumerate(accounts)}
        snapshot.sort(key=lambda item: order.get(item["uid"], 0))
        return {"ok": True, "accounts": snapshot,
                "pending_count": sum(len(item["growth"]) for item in snapshot),
                "timed_out": timed_out}
    def start(self, uids=None, codes=None, concurrency=None):
        """Queue every pending task and run it in the background."""
        with self._lock:
            if self._running or self._starting:
                return {"ok": True, "started": False, "total": 0,
                        "seq": self._seq, "msg": "队列已在运行"}
            # scan() can take seconds (network). Keeping _starting set until the
            # finally below stops a second caller from slipping through the
            # running check while the first one is still scanning.
            self._starting = True
        try:
            scan = self.scan(uids=uids)
            code_filter = set(codes or [])
            items = []
            for account in scan["accounts"]:
                for task in account["growth"]:
                    if code_filter and task["task_code"] not in code_filter:
                        continue
                    items.append({
                        "uid": account["uid"],
                        "nickname": account["nickname"],
                        "kind": "growth",
                        "code": task["task_code"],
                        "name": task.get("name") or task["task_code"],
                        "status": "pending",
                        "message": "",
                    })
            if not items:
                return {"ok": True, "started": False, "total": 0, "seq": self._seq,
                        "msg": "没有待办任务"}
            with self._lock:
                self._items = items
                self._running = True
                self._started_at = time.time()
                self._seq += 1
                seq = self._seq
        finally:
            with self._lock:
                self._starting = False
        concurrency = self.clamp_concurrency(
            concurrency if concurrency is not None else self._default_concurrency)
        thread = threading.Thread(target=self._run, args=(items, concurrency),
                                  daemon=True)
        thread.start()
        self._log("task queue started: %d item(s), concurrency=%d"
                  % (len(items), concurrency))
        return {"ok": True, "started": True, "total": len(items), "seq": seq,
                "concurrency": concurrency, "msg": "队列已启动"}
    def _run(self, items, concurrency):
        semaphore = threading.Semaphore(concurrency)
        threads = []
        by_uid = {}
        for index, item in enumerate(items):
            by_uid.setdefault(item["uid"], []).append(index)

        def run_account(uid, indexes):
            with semaphore:
                account = self.pool.get(uid)
                if account is None:
                    for index in indexes:
                        self._mark(index, "error", "账号不存在")
                    return
                lock = self.account_lock(uid)
                if not lock.acquire(blocking=False):
                    for index in indexes:
                        self._mark(index, "skipped", "该账号有其它任务动作在执行，跳过")
                    return
                try:
                    for index in indexes:
                        code = items[index]["code"]
                        self._mark(index, "running", "")
                        try:
                            ok, message, _credit = self.runner(account, code)
                        except Exception as exc:
                            self._mark(index, "error", str(exc)[:300])
                        else:
                            self._mark(index, "done" if ok else "error", message or "")
                        time.sleep(0.5)
                finally:
                    lock.release()

        for uid, indexes in by_uid.items():
            thread = threading.Thread(target=run_account, args=(uid, indexes),
                                      daemon=True)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()
        with self._lock:
            self._running = False
        self._log("task queue finished: %d item(s)" % len(items))

    def _mark(self, index, status, message):
        with self._lock:
            if 0 <= index < len(self._items):
                self._items[index]["status"] = status
                self._items[index]["message"] = message

    def _counts(self):
        counts = {}
        for item in self._items:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        return counts

    def status(self):
        with self._lock:
            return {
                "ok": True,
                "running": self._running,
                "seq": self._seq,
                "started_at": self._started_at,
                "items": [dict(item) for item in self._items],
                "counts": self._counts(),
            }

    def wait(self, timeout=600):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if not self._running:
                    return True
            time.sleep(0.2)
        return False
