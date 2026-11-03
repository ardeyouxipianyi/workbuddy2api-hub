#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb_media_tasks.py —— 生视频的任务表、后台轮询与产物管理。

生视频与生图根本不同：提交只拿到一个 task id，产物要再轮询、再下载。网关是
`ThreadingHTTPServer`（一请求一线程），一条视频要几十秒到几分钟，**绝不能把请求
线程挂在那里**，所以拆成：

    请求线程：校验 → 选号 → 提交（花钱在这里）→ 记一行任务 → 立刻 202
    worker ：按账号轮询 → 下载 mp4 → 标 completed

## 为什么「提交」留在请求线程里

提交本身只要几秒，而且要能在请求里把上游的拒绝直接翻译给客户端——dsh 踩过的坑是
`seconds=1` 被上游 400 退回后还要白等 60 秒冷却，用户原话「上游返回这种错误，也要
等半天」。放在请求线程里，`REJECTED` 就能当场回 400 并说明「未扣积分、改完可立刻重试」。

代价是队列上限必须在**提交之前**检查：提交之后队列满了，任务已经花了钱，丢掉就是丢钱。

## 为什么轮询必须用同一个账号

task id 属于提交它的那个账号，换号轮询只会得到一个 404。所以任务行里钉住
`account`，worker 只用它。这也意味着**任务失败时不换号重试**——重试等于重新提交，
那是再花一次钱，得由客户端自己决定。

## 五种状态

    queued  → running → completed
                       ↘ failed      （轮询失败 / 上游任务失败 / 下载失败，可能已扣费）
                       ↘ rejected    （提交阶段被 4xx 退回，没扣费；本模块不会产生，留给路由层）
                       ↘ lost        （进程重启时还在跑：上游可能已扣费，必须能看见）

`lost` 单独存在是刻意的：只报 `failed` 的话，「钱花了没产物」就查不到原因。
"""
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import threading
import time

import wb_media

#: 同时在跑的生视频任务数。**这是"并发生成"的闸**：超过它的提交不进上游，先在我们
#: 这边排队（status=waiting），等有任务结束再提交。
#:
#: 默认 3 与默认工人数相同：每条在跑的任务各占一个轮询线程，避免一条的轮询拖住其它条。
#: 为什么不设更大：真正的成本在上游——同一账号同时跑多条会放大风控面，所以调度器
#: 会把并行任务分散到**不同账号**（`busy_accounts()`），账号不够时宁可排队。
DEFAULT_CONCURRENCY = 3
#: 轮询线程数。**默认等于并发数**：一条任务的轮询可能阻塞几十秒（上游慢/超时），
#: 单线程会把其他条一起拖住，看起来像"并发没生效"。
DEFAULT_WORKERS = 3
#: 未完成任务超过这个数就拒绝**新提交**（在花钱之前拦）。视频按分钟算，20 条已经是
#: 很长的等待了；再多客户端还不如直接看到 429。
DEFAULT_MAX_PENDING = 20
#: 任务表保留多少条。超出后先删最旧的已完成任务。
DEFAULT_MAX_TASKS = 200
#: 产物目录的总量上限（MB）。超出后先删最旧的 mp4。
DEFAULT_MAX_DISK_MB = 2048
#: 已完成任务的保留天数。
DEFAULT_TTL_DAYS = 7
#: 下载令牌的有效期（秒）。
SIGN_TTL_SECONDS = 3600


def default_out_dir():
    """产物目录：跟随代码目录，可用 `WB_MEDIA_DIR` 覆盖。

    不用 `os.tmpdir()`：网关是长跑进程，系统临时目录会被清理工具扫掉，而用户手里
    还拿着链接，隔天打开就是 404。容器部署里代码目录可能只读，所以留了环境变量。
    """
    override = os.environ.get("WB_MEDIA_DIR", "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "media")


def _safe_token(value):
    return re.sub(r"[^0-9A-Za-z_-]+", "-", str(value or ""))[:48] or "task"


class MediaTaskStore(object):
    """任务表 + 后台轮询 worker + 产物目录 + 下载令牌。

    构造参数全部可注入，便于测试：`client` 是出站层（默认 wb_media_client），
    `resolve_account` 把 uid 变成一个账号对象（默认查 POOL，测试里直接给表），
    `clock` 覆盖时间以便测 TTL。
    """

    def __init__(self, out_dir=None, workers=DEFAULT_WORKERS,
                 concurrency=DEFAULT_CONCURRENCY,
                 max_pending=DEFAULT_MAX_PENDING, max_tasks=DEFAULT_MAX_TASKS,
                 max_disk_mb=DEFAULT_MAX_DISK_MB, ttl_days=DEFAULT_TTL_DAYS,
                 client=None, resolve_account=None, logger=None, clock=None,
                 autostart=True, on_finish=None):
        self.out_dir = out_dir or default_out_dir()
        self.state_path = os.path.join(self.out_dir, "tasks.json")
        self.workers = max(1, int(workers))
        self.concurrency = max(1, int(concurrency))
        #: 等待提交的任务体：`task_id → {body, model, realm, key}`。**只在内存里**——
        #: 这些任务还没花过钱，重启直接丢弃（标 failed 让客户端重试）比把 prompt
        #: 整段写进磁盘更合适。
        self._waiting_bodies = {}
        #: 提交器：`fn(task) -> {upstream_id, account, warning?}`。由 wb_proxy 注入，
        #: 因为"选哪个账号、怎么提交"属于服务层的事，任务表只管调度。
        self._submitter = None
        self.max_pending = max(1, int(max_pending))
        self.max_tasks = max(1, int(max_tasks))
        self.max_disk_bytes = int(max_disk_mb) * 1024 * 1024
        self.ttl_seconds = int(ttl_days) * 86400
        self._client = client
        self._resolve_account = resolve_account
        self._logger = logger
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._queue = queue.Queue()
        self._tasks = {}
        self._order = []                  # 提交顺序，用于淘汰最旧
        self._threads = []
        self._started = False
        self._stopping = False
        self._secret = secrets.token_bytes(32)
        self.autostart = bool(autostart)
        # 任务到终态时的回调（记账用）。放在注入而不是 import 里：记账属于
        # wb_proxy 的事，本模块不能反过来依赖它（那会成环）。
        self._on_finish = on_finish
        self._load()

    # ------------------------------------------------------------ 依赖注入

    @property
    def client(self):
        if self._client is None:
            import wb_media_client
            self._client = wb_media_client
        return self._client

    def _account_for(self, uid):
        """把 uid 变成账号对象。**必须由调用方注入**，这里绝不去 import wb_proxy。

        踩过的坑：wb_proxy 以脚本方式运行时模块名是 `__main__`，`import wb_proxy` 会
        把模块代码再执行一遍、拿到一个 `POOL is None` 的影子模块。后果是任务提交成功
        （上游已受理、可能已扣费）却在 0.8 秒内被标成失败——报「提交该任务的账号已不可
        用」，而账号其实好好的。
        """
        if self._resolve_account is None:
            self._log("media tasks: 没有注入 account_resolver，无法轮询")
            return None
        return self._resolve_account(uid)

    def _log(self, message):
        if self._logger is not None:
            try:
                self._logger(message)
            except Exception:
                pass

    # ------------------------------------------------------------ 任务表

    def create(self, model, params, account_uid, body, upstream_id, realm="cn",
               key="", warning=None):
        """登记一个**已经提交成功**的任务（上游已受理、已扣费）。

        `body` 里含 prompt，但落盘时只留 `prompt_head` + 长度：排查够用，
        又不会把用户写的内容整段写进磁盘（`usage.jsonl` 同样不存 prompt）。
        """
        with self._lock:
            task_id = self._new_id()
            now = self._clock()
            prompt = ""
            if isinstance(body, dict):
                prompt = body.get("prompt") or ""
            task = {
                "id": task_id,
                "status": "queued",
                "model": model,
                "realm": realm,
                "account": account_uid,
                "key": key or "",
                "upstream_id": upstream_id,
                "seconds": params.get("seconds"),
                "resolution": params.get("resolution"),
                "aspect_ratio": params.get("aspect_ratio"),
                "enable_audio": params.get("enable_audio"),
                "image_to_video": bool(params.get("image_to_video")),
                # 带参考素材的任务**按 2 倍计价**（实测 5 秒 768P 扣 142.86，
                # 正好是普通 768P 的两倍）。这个标记必须在建行时就落盘：完成时
                # 只拿得到 credit / seconds，body 早就没留了，事后无从判断是哪一档，
                # 会把 2 倍价写进普通档、污染之后所有的报价。
                "has_reference_materials": bool(
                    isinstance(body, dict) and (
                        body.get("reference_videos") or body.get("reference_audios")
                        or body.get("reference_images"))),
                "prompt_head": prompt[:80],
                "prompt_len": len(prompt),
                "created_at": int(now),
                "started_at": None,
                "completed_at": None,
                "credit": None,
                "error": "",
                "videos": [],
                "warnings": list(params.get("warnings") or []) + ([warning] if warning else []),
            }
            self._tasks[task_id] = task
            self._order.append(task_id)
            self._evict_locked()
            self._persist_locked()
        self._queue.put(task_id)
        if self.autostart:
            self.ensure_worker()
        return dict(task)

    def create_pending(self, model, params, body, realm="cn", key=""):
        """登记一个**还没提交到上游**的任务（排队中，没花钱）。

        与 `create()` 的区别就是钱：`create()` 记的是"上游已受理、已扣费"，失败也只能
        认亏；这里失败说明**一分没花**，可以放心让客户端重试。

        `body` 留在内存（`_waiting_bodies`），不进磁盘：这些任务最终要么提交、要么在
        重启时作废，把 prompt 整段写盘没有收益。
        """
        with self._lock:
            task_id = self._new_id()
            now = self._clock()
            prompt = ""
            if isinstance(body, dict):
                prompt = body.get("prompt") or ""
            task = {
                "id": task_id,
                "status": "waiting",
                "model": model,
                "realm": realm,
                "account": None,
                "key": key or "",
                "upstream_id": None,
                "seconds": params.get("seconds"),
                "resolution": params.get("resolution"),
                "aspect_ratio": params.get("aspect_ratio"),
                "enable_audio": params.get("enable_audio"),
                "image_to_video": bool(params.get("image_to_video")),
                "prompt_head": prompt[:80],
                "prompt_len": len(prompt),
                "created_at": int(now),
                "started_at": None,
                "completed_at": None,
                "credit": None,
                "error": "",
                "videos": [],
                "warnings": list(params.get("warnings") or []),
            }
            self._tasks[task_id] = task
            self._order.append(task_id)
            self._waiting_bodies[task_id] = {"body": body, "model": model,
                                             "realm": realm, "key": key or ""}
            self._evict_locked()
            self._persist_locked()
        if self.autostart:
            self.ensure_worker()
        return dict(task)

    def set_submitter(self, submitter):
        """注入"怎么提交"（选账号 + 调上游）。`None` 表示本进程不代提。"""
        self._submitter = submitter

    def active_count(self):
        """**已经提交到上游、还没结束**的任务数（= 真正占着并发的那些）。"""
        with self._lock:
            return sum(1 for task in self._tasks.values()
                       if task["status"] in ("queued", "running"))

    def busy_accounts(self):
        """正在跑任务的账号 uid 集合。调度器用它把并行任务摊到不同账号上。"""
        with self._lock:
            return {task.get("account") for task in self._tasks.values()
                    if task["status"] in ("queued", "running") and task.get("account")}

    def waiting_count(self):
        with self._lock:
            return sum(1 for task in self._tasks.values() if task["status"] == "waiting")

    def queue_position(self, task_id):
        """排队位次（1 = 下一个被提交）。不在队列里返回 0。"""
        with self._lock:
            if self._tasks.get(task_id, {}).get("status") != "waiting":
                return 0
            waiters = [tid for tid in self._order
                       if self._tasks.get(tid, {}).get("status") == "waiting"]
            try:
                return waiters.index(task_id) + 1
            except ValueError:
                return 0

    def _pump(self):
        """把排队中的任务尽量提交出去：并发未满 + 有等待者 + 有提交器。

        由 worker 的空转分支与任务结束时调用。**不阻塞、不重试**：提交失败就把任务标
        `rejected`（一分没花），让客户端改参数重来，而不是在后台反复烧上游。
        """
        submitter = self._submitter
        if submitter is None:
            return
        while True:
            with self._lock:
                if self.active_count() >= self.concurrency:
                    return
                candidate = next((tid for tid in self._order
                                  if self._tasks.get(tid, {}).get("status") == "waiting"),
                                 None)
                if candidate is None:
                    return
                # 先占位再提交：否则两个 worker 会同时挑中同一条。
                self._tasks[candidate]["status"] = "submitting"
                snapshot = dict(self._tasks[candidate])
            payload = self._waiting_bodies.pop(candidate, None)
            if payload is None:
                self._finish(candidate, "failed",
                             error="排队中的任务体丢失（进程重启过），请重新提交")
                continue
            task_for_submit = dict(snapshot)
            task_for_submit.update(payload)
            try:
                outcome = submitter(task_for_submit) or {}
            except wb_media.MediaError as exc:
                # 未扣费：可以放心让客户端立刻重试。
                self._finish(candidate, "rejected", error=exc.message)
                continue
            except Exception as exc:                      # noqa: BLE001
                self._finish(candidate, "rejected", error="提交失败：%s" % (exc,))
                continue
            upstream_id = outcome.get("upstream_id")
            if not upstream_id:
                self._finish(candidate, "rejected", error="上游没有返回任务 id")
                continue
            with self._lock:
                task = self._tasks.get(candidate)
                if task is not None:
                    task.update({
                        "status": "queued",
                        "upstream_id": upstream_id,
                        "account": outcome.get("account") or task.get("account"),
                    })
                    if outcome.get("warning"):
                        task.setdefault("warnings", []).append(outcome["warning"])
                self._persist_locked()
            # 交给轮询队列（与同步提交成功的任务同一条路）。
            self._queue.put(candidate)

    def get(self, task_id):
        with self._lock:
            task = self._tasks.get(str(task_id or ""))
            return dict(task) if task is not None else None

    def list(self, limit=50):
        with self._lock:
            ids = list(reversed(self._order))[:max(1, int(limit))]
            return [dict(self._tasks[i]) for i in ids if i in self._tasks]

    def pending_count(self):
        with self._lock:
            return sum(1 for task in self._tasks.values()
                       if task["status"] in ("queued", "running"))

    def stats(self):
        with self._lock:
            counts = {}
            for task in self._tasks.values():
                counts[task["status"]] = counts.get(task["status"], 0) + 1
            return {"total": len(self._tasks), "pending": self.pending_count(),
                    "by_status": counts, "queue_depth": self._queue.qsize(),
                    "workers": self.workers,
                    # 并发生视频：concurrency 是在跑的上限，active 是当前占位，
                    # waiting 是排队等提交（**没花钱**）的条数。
                    "concurrency": self.concurrency,
                    "active": self.active_count(),
                    "waiting": self.waiting_count(),
                    "busy_accounts": len(self.busy_accounts())}

    def cancel(self, task_id):
        """放弃一个还没交到上游的任务：**排队中的取消 = 一分没花**。

        三档语义，区别就是钱：

        | 状态 | 能否取消 | 结果 |
        | --- | --- | --- |
        | `waiting`（排队等并发位） | **能** | 从队列摘掉，从未提交、从未扣费 |
        | `queued`（上游已受理） | 能（仅本地） | 停止轮询；上游照样跑完、照样计费 |
        | `submitting` / `running` | 不能 | 已经或正在交给上游，必须等结果 |

        上游没有 cancel 接口（未证实），所以 `queued` 的"取消"只是停掉我们这边的轮询。
        `running` 的必须等它出结果，否则连"钱有没有花、产物在哪"都查不到。
        """
        with self._lock:
            task = self._tasks.get(str(task_id or ""))
            if task is None:
                return False
            status = task["status"]
            if status == "waiting":
                # 排队中：还没花过钱，可以干净地摘掉。
                # 单独用 `cancelled` 而不是 `failed`：对外契约里 `failed` 意味着
                # "上游可能已扣费"，而这条**根本没到过上游**，可以放心重试。
                task["status"] = "cancelled"
                task["error"] = "cancelled while queued (未提交到上游，未扣费)"
                task["completed_at"] = int(self._clock())
                self._waiting_bodies.pop(task_id, None)
                self._persist_locked()
                return True
            if status != "queued":
                return False
            task["status"] = "cancelled"
            task["error"] = "cancelled before polling（上游已受理，仍会跑完并计费）"
            task["completed_at"] = int(self._clock())
            self._persist_locked()
            return True

    def wait(self, task_id, timeout=600, interval=0.5, sleep=None):
        """阻塞到终态（给 `?wait=1` 的客户端）。超时返回当前状态，不抛异常。"""
        waiter = sleep or time.sleep
        deadline = self._clock() + max(0, int(timeout))
        while True:
            task = self.get(task_id)
            # `waiting` / `submitting` 也算"没结束"：它们对外是 queued，客户端用
            # ?wait=1 就是想等到出片，不能因为还在本地排队就立刻返回。
            if task is None or task["status"] not in ("waiting", "submitting",
                                                      "queued", "running"):
                return task
            if self._clock() >= deadline:
                return task
            waiter(interval)

    # ------------------------------------------------------------ 产物与令牌

    def content_path(self, task_id, index=0):
        task = self.get(task_id)
        if task is None:
            return None
        videos = task.get("videos") or []
        try:
            path = videos[int(index)]
        except (IndexError, ValueError, TypeError):
            return None
        return path if os.path.isfile(path) else None

    def sign(self, task_id, index=0, ttl=SIGN_TTL_SECONDS, now=None):
        """给下载地址发一个短期令牌。

        客户端常把 URL 直接贴进浏览器或播放器，那里没有 Authorization 头，所以下载
        路由要能靠查询参数鉴权。签名留在服务端（进程内 secret）：前端与客户端都不该
        参与计算 HMAC。进程重启后旧链接失效——与任务表一起失效，口径一致。
        """
        exp = int((now if now is not None else self._clock()) + ttl)
        message = ("%s|%d|%d" % (_safe_token(task_id), int(index), exp)).encode("utf-8")
        mac = hmac.new(self._secret, message, hashlib.sha256).hexdigest()[:32]
        return "%d.%s" % (exp, mac)

    def verify(self, task_id, index, token, now=None):
        token = str(token or "")
        exp_text, _, mac = token.partition(".")
        try:
            exp = int(exp_text)
        except ValueError:
            return False
        if exp < int(now if now is not None else self._clock()):
            return False
        expected = self.sign(task_id, index, ttl=0, now=exp)
        return hmac.compare_digest(expected, token)

    # ------------------------------------------------------------ worker

    def ensure_worker(self):
        with self._lock:
            if self._started or self._stopping:
                return
            self._started = True
        for index in range(self.workers):
            thread = threading.Thread(target=self._worker, name="wb-media-%d" % index,
                                      daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self):
        """停止 worker（测试与优雅退出用）。进行中的任务保持原状，下次启动标 lost。"""
        self._stopping = True
        for _ in self._threads:
            self._queue.put(None)
        self._threads = []
        self._started = False

    def _worker(self):
        while not self._stopping:
            try:
                task_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                # 空转时看看有没有排队等提交的任务：并发有空位就提交它。
                # 放在这里（而不是定时线程）是为了不引入第三个循环。
                try:
                    self._pump()
                except Exception:                         # noqa: BLE001
                    pass
                continue
            if task_id is None:
                break
            try:
                self._run_task(task_id)
            except Exception as exc:                      # 兜底：worker 不能死
                self._finish(task_id, "failed", error="worker crashed: %s" % (exc,))
            finally:
                self._queue.task_done()
                # 一条结束就腾出一个并发位：立刻提交排队中的下一条。
                try:
                    self._pump()
                except Exception:                         # noqa: BLE001
                    pass

    def _run_task(self, task_id):
        task = self.get(task_id)
        if task is None or task["status"] != "queued":
            return
        account = self._account_for(task.get("account"))
        if account is None:
            # 轮询必须用提交它的那个账号，取不到就只能停。但**不能记 failed**：
            # 上游任务很可能已经受理并扣了费，记 failed 会让「钱花了没产物」查不到
            # 原因。lost 的语义正是「本地丢了线索、上游可能已经花过钱」。
            self._finish(task_id, "lost",
                         error="提交该任务的账号当前不可用，上游任务可能仍在进行且已扣费")
            return
        self._update(task_id, status="running", started_at=int(self._clock()))
        try:
            outcome = self.client.poll_video_task(account, task["upstream_id"])
        except wb_media.MediaError as exc:
            self._finish(task_id, "failed", error=exc.message)
            return
        except Exception as exc:
            self._finish(task_id, "failed", error=str(exc))
            return
        paths = []
        try:
            for index, url in enumerate(outcome.get("videos") or []):
                paths.append(self.client.download_video(
                    account, url, self.out_dir, index=index))
        except wb_media.MediaError as exc:
            # 产物已经生成了却下载不下来：把已知的 URL 留在任务行里，别让线索丢失。
            self._finish(task_id, "failed", error="下载产物失败：%s" % exc.message,
                         videos=list(outcome.get("videos") or []))
            return
        except Exception as exc:
            self._finish(task_id, "failed", error="下载产物失败：%s" % (exc,))
            return
        self._finish(task_id, "completed", credit=outcome.get("credit"), videos=paths)

    # ------------------------------------------------------------ 内部状态

    def _new_id(self):
        return "vid_" + secrets.token_hex(8)

    def _update(self, task_id, **fields):
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.update(fields)
            self._persist_locked()

    def _finish(self, task_id, status, error="", credit=None, videos=None):
        fields = {"status": status, "completed_at": int(self._clock()), "error": error}
        if credit is not None:
            fields["credit"] = credit
        if videos is not None:
            fields["videos"] = list(videos)
        self._update(task_id, **fields)
        if self._on_finish is not None:
            task = self.get(task_id)
            if task is not None:
                try:
                    # 锁外调用：回调里会写 usage、碰账号池，不能在持锁时进另一个锁域。
                    self._on_finish(task)
                except Exception as exc:
                    self._log("media task %s: 完成回调失败 %s" % (task_id, exc))
        self._prune_outputs()

    def _evict_locked(self):
        """任务表超过上限时删最旧的已完成任务（调用方已持锁）。"""
        if len(self._order) <= self.max_tasks:
            return
        for task_id in list(self._order):
            if len(self._order) <= self.max_tasks:
                break
            task = self._tasks.get(task_id)
            if task is None:
                self._order.remove(task_id)
                continue
            if task["status"] in ("completed", "failed", "rejected", "lost", "cancelled"):
                self._order.remove(task_id)
                self._tasks.pop(task_id, None)

    def _prune_outputs(self):
        """清理旧产物与超量磁盘。只删**已完成**任务的 mp4，进行中的不动。"""
        now = self._clock()
        with self._lock:
            victims = []
            total = 0
            entries = []
            for task_id in self._order:
                task = self._tasks.get(task_id)
                if task is None or not task.get("videos"):
                    continue
                size = 0
                for path in task["videos"]:
                    try:
                        size += os.path.getsize(path)
                    except OSError:
                        pass
                entries.append((task_id, task.get("completed_at") or 0, size, task["videos"]))
                total += size
            for task_id, completed_at, size, videos in entries:
                expired = completed_at and (now - completed_at) > self.ttl_seconds
                oversized = total > self.max_disk_bytes
                if not expired and not oversized:
                    continue
                victims.append((task_id, videos))
                total -= size
        for task_id, videos in victims:
            for path in videos:
                try:
                    os.remove(path)
                except OSError:
                    pass
            with self._lock:
                task = self._tasks.get(task_id)
                if task is not None:
                    task["videos"] = []
            self._log("media task %s: 产物已清理（超期或超量）" % task_id)

    # ------------------------------------------------------------ 落盘

    def _persist_locked(self):
        try:
            os.makedirs(self.out_dir, exist_ok=True)
            payload = {"saved_at": int(self._clock()),
                       "tasks": [self._tasks[i] for i in self._order if i in self._tasks]}
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(tmp, self.state_path)
        except Exception as exc:
            # 落盘失败不该让一次已经花钱的提交变成错误：内存里仍然是对的。
            self._log("media tasks: 落盘失败 %s" % (exc,))

    def _load(self):
        """读回上次的任务表；**进行中的一律标 lost**（见模块 docstring）。"""
        try:
            with open(self.state_path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return
        tasks = payload.get("tasks") if isinstance(payload, dict) else None
        if not isinstance(tasks, list):
            return
        for task in tasks:
            if not isinstance(task, dict) or not task.get("id"):
                continue
            if task.get("status") in ("queued", "running"):
                task["status"] = "lost"
                task["error"] = "网关重启时任务仍在进行，上游可能已扣费"
            elif task.get("status") in ("waiting", "submitting"):
                # 还没提交到上游 ⇒ **一分没花**，按 failed 报并明说可以重试。
                # 与 lost 的区别很重要：lost 意味着钱可能已经花了。
                task["status"] = "failed"
                task["error"] = "排队期间网关重启（未提交到上游，未扣费），请重新提交"
            self._tasks[task["id"]] = task
            self._order.append(task["id"])
        self._log("media tasks: 读回 %d 条历史任务" % len(self._order))
