"""wb_window_probe.py —— 模型额度窗口探针

上游的免费额度窗口按「首次请求」起算：某个 (账号, 模型) 打满之后，429 会给出
一个 `reset at` 绝对时间。到点之后如果没有任何请求进来，新一轮窗口就不会开始，
额度一直空转——下一次真实请求来的时候，窗口起点才被动地落到那一刻，整整晚一截。

本模块把这个 reset 时间持久化下来，到点后主动为该 (账号, 模型) 发一个最小探针
请求，把新一轮窗口的计时起点钉在重置那一刻。

四条协作路径：
  * wb_proxy 在 429 分支解析出 reset_at 后调用 schedule() 登记窗口；
  * wb_scheduler 每轮巡检调用 run_due() 执行到点的探针；
  * wb_quota_scan 复用 probe() 主动查那些还没被限流的配额线，并用 has_window()
    判断某条线是否已经交给这里处理（有记录就不再自己查）；
  * 探针成功 -> 清掉该模型的冷却；又 429 -> 用上游新给的 reset_at 重新登记。

状态落在 `accounts/model_windows.json`，重启后仍然有效（进程内冷却表是易失的，
窗口记录不能跟着一起丢，否则重启就把重置时刻错过了）。

只依赖标准库。
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request

import wb_accounts
import wb_runtime
import wb_settings

STATE_NAME = "model_windows.json"
STATE_VERSION = 1

# 探针请求本身要尽量小：它唯一的目的是让上游把这一轮窗口的计时起点定在此刻。
PROBE_PROMPT = "Hi"
PROBE_MAX_TOKENS = 16
PROBE_TIMEOUT = 60

# 到点后上游可能还没真正放开（服务端时钟/处理延迟），需要重试若干轮。
MAX_ATTEMPTS = 20
RETRY_DELAY = 15.0
# 429 给的重置点比「现在 + 这个值」还远，才算上游真的开了一轮新窗口；
# 否则只是探针发早了，仍按同一轮重试。
FRESH_WINDOW_MIN = 30.0
# 过期这么久还没触发的窗口直接丢弃：账号大概已经被弃用或删除。
MAX_AGE = 7 * 86400
# 每轮最多处理几个窗口，避免一个卡死的上游把调度线程占满。
BATCH_LIMIT = 8
# 多个窗口同时到点时，相邻两次探针之间的间隔（测试里会把它置 0）。
PROBE_GAP = 0.4

_lock = threading.RLock()
_run_lock = threading.Lock()
_state = {"dir": None, "windows": {}}


# --------------------------------------------------------------- 状态存取

def _state_path(accounts_dir):
    return os.path.join(accounts_dir, STATE_NAME)


def _key(uid, model):
    return "%s\x00%s" % (uid, model)


def _prune(windows, now):
    """丢掉字段残缺、以及过期太久的记录。"""
    out = {}
    for key, row in (windows or {}).items():
        if not isinstance(row, dict):
            continue
        uid = str(row.get("uid") or "")
        model = str(row.get("model") or "")
        try:
            reset_at = float(row.get("reset_at") or 0)
        except (TypeError, ValueError):
            continue
        if not uid or not model or reset_at <= 0:
            continue
        if reset_at < now - MAX_AGE:
            continue
        out[str(key)] = {
            "uid": uid,
            "model": model,
            "reset_at": reset_at,
            "attempts": int(row.get("attempts") or 0),
            "last_try": float(row.get("last_try") or 0),
            "last_result": str(row.get("last_result") or "")[:200],
        }
    return out


def _load(accounts_dir, now=None):
    """按目录懒加载。换目录（或首次调用）时从磁盘重读。调用方需持有 _lock。"""
    now = time.time() if now is None else now
    if _state["dir"] == accounts_dir:
        return _state["windows"]
    windows = {}
    try:
        # utf-8-sig：这个文件是给人看、也可能被人手改的，记事本一类编辑器
        # 会写 BOM，而 json.load 遇到 BOM 直接抛异常——那会让所有窗口记录
        # 静默消失，探针再也不触发。自己写出去的是无 BOM 的 UTF-8。
        with open(_state_path(accounts_dir), encoding="utf-8-sig") as fh:
            raw = json.load(fh)
        stored = raw.get("windows") if isinstance(raw, dict) else raw
        if isinstance(stored, list):
            for row in stored:
                if isinstance(row, dict) and row.get("uid") and row.get("model"):
                    windows[_key(str(row["uid"]), str(row["model"]))] = row
        elif isinstance(stored, dict):
            windows = {str(k): v for k, v in stored.items() if isinstance(v, dict)}
    except FileNotFoundError:
        pass
    except Exception:
        # 状态文件坏掉不该拖垮网关：当作没有窗口，下一次 429 会重新登记。
        windows = {}
    _state["dir"] = accounts_dir
    _state["windows"] = _prune(windows, now)
    return _state["windows"]


def _save(accounts_dir):
    """原子落盘。调用方需持有 _lock。写失败只丢持久化，不影响本轮探针。"""
    payload = {
        "version": STATE_VERSION,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "windows": list(_state["windows"].values()),
    }
    path = _state_path(accounts_dir)
    tmp = path + ".tmp"
    try:
        os.makedirs(accounts_dir, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


# --------------------------------------------------------------- 对外接口

def schedule(accounts_dir, uid, model, reset_at, now=None):
    """登记一个额度窗口，返回是否新增/更新了记录。

    同一个窗口的重复 429（一次限流会连着撞好几次）时间戳不变，这里直接返回
    False，避免每次 429 都重写一遍状态文件。
    """
    uid = str(uid or "")
    model = str(model or "")
    now = time.time() if now is None else now
    try:
        reset_at = float(reset_at or 0)
    except (TypeError, ValueError):
        return False
    if not uid or not model or not accounts_dir or reset_at <= now:
        return False
    if not wb_settings.window_probe(accounts_dir):
        return False
    with _lock:
        windows = _load(accounts_dir, now)
        key = _key(uid, model)
        row = windows.get(key)
        if row and abs(float(row.get("reset_at") or 0) - reset_at) < 1.0:
            return False
        windows[key] = {
            "uid": uid,
            "model": model,
            "reset_at": reset_at,
            "attempts": 0,
            "last_try": 0.0,
            "last_result": "",
        }
        _save(accounts_dir)
        return True


def drop(accounts_dir, uid, model):
    """撤销一个窗口（探针成功、放弃、或账号已不在池中）。"""
    with _lock:
        windows = _load(accounts_dir)
        if windows.pop(_key(str(uid or ""), str(model or "")), None) is not None:
            _save(accounts_dir)
            return True
    return False


def seconds_until_due(accounts_dir=None, now=None):
    """最近一个窗口还有多少秒到点；没有待触发窗口时返回 None。

    调度器用它决定下一次醒来的时间：把窗口起点钉在重置那一刻，而不是等
    30 秒的巡检周期。
    """
    if not accounts_dir:
        return None
    now = time.time() if now is None else now
    with _lock:
        windows = _load(accounts_dir, now)
        if not windows:
            return None
        soonest = min(float(r.get("reset_at") or 0) for r in windows.values())
    return max(0.0, soonest - now)


def snapshot(accounts_dir=None, now=None):
    """只读快照，按到点时间升序，给看板与调度器状态用。"""
    if not accounts_dir:
        return []
    now = time.time() if now is None else now
    with _lock:
        rows = [dict(r) for r in _load(accounts_dir, now).values()]
    rows.sort(key=lambda r: float(r.get("reset_at") or 0))
    out = []
    for row in rows:
        reset_at = float(row.get("reset_at") or 0)
        uid = str(row.get("uid") or "")
        out.append({
            "uid": uid,
            "uid8": uid[:8],
            "model": str(row.get("model") or ""),
            "reset_at": reset_at,
            "reset_in": max(0, int(reset_at - now)),
            "attempts": int(row.get("attempts") or 0),
            "last_result": str(row.get("last_result") or ""),
        })
    return out


# --------------------------------------------------------------- 探针执行

def run_due(pool, log=None, limit=BATCH_LIMIT, now=None):
    """执行所有已到点的窗口探针，返回实际发出的探针数。

    阻塞式实现，由调用方决定放在哪个线程里跑（wb_scheduler 用独立线程，
    免得一次上游超时把整点巡检的节奏带偏）。同一时刻只允许一轮，重入直接
    返回 0。
    """
    log = log or (lambda msg: None)
    accounts_dir = getattr(pool, "dir", "") or ""
    if pool is None or not accounts_dir:
        return 0
    if not wb_settings.window_probe(accounts_dir):
        return 0
    if not _run_lock.acquire(blocking=False):
        return 0
    try:
        return _run_due_locked(pool, accounts_dir, log, limit, now)
    finally:
        _run_lock.release()


def _run_due_locked(pool, accounts_dir, log, limit, now):
    now = time.time() if now is None else now
    with _lock:
        due = [dict(r) for r in _load(accounts_dir, now).values()
               if float(r.get("reset_at") or 0) <= now]
    due.sort(key=lambda r: float(r.get("reset_at") or 0))
    sent = 0
    for row in due[:max(1, int(limit or BATCH_LIMIT))]:
        uid = str(row.get("uid") or "")
        model = str(row.get("model") or "")
        account = pool.get(uid)
        if account is None:
            log("窗口探针：账号 [%s] 已不在池中，丢弃 %s 的窗口记录" % (uid[:8], model))
            drop(accounts_dir, uid, model)
            continue
        if not account.enabled or not account.access_token:
            # 停用或无凭证的账号不该被自动唤醒：保留记录，等它被重新启用。
            continue
        sent += 1
        try:
            _fire(accounts_dir, account, model, log)
        except Exception as exc:
            log("窗口探针：账号 [%s] 的 %s 执行异常: %s" % (uid[:8], model, exc))
        # 多个窗口同时到点时错开，别在同一秒向上游打一串请求。
        time.sleep(PROBE_GAP)
    return sent


def _fire(accounts_dir, account, model, log):
    """发一次探针，并按结果更新窗口记录。"""
    uid = str(account.uid)
    uid8 = uid[:8]
    now = time.time()
    # Token 快过期就先刷新，否则探针只会拿回 401，白跑一趟。
    exp = account.expires_at or wb_accounts.jwt_exp(account.access_token)
    if exp and (exp - now) < 120:
        try:
            account.refresh()
        except Exception:
            pass

    ok, reset_at, detail = _probe(account, model)
    if ok:
        # 探针通了：这条配额线已经重置，立刻把它放回可用状态。
        # 只清该模型的冷却，账号级 cooldown_until（网络故障留下的）不动。
        account.clear_model_cooldown(model)
        drop(accounts_dir, uid, model)
        log("✓ 窗口探针：账号 [%s] 的 %s 已开始新一轮计时（探针请求成功）"
            % (uid8, model))
        return

    if reset_at and reset_at > time.time() + FRESH_WINDOW_MIN:
        # 上游给了更晚的重置点：这条配额线确实进入了新的等待期。
        _update(accounts_dir, uid, model, reset_at=reset_at, attempts=0,
                last_result=detail)
        log("窗口探针：账号 [%s] 的 %s 仍在限流，下一轮重置 %s"
            % (uid8, model,
               time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(reset_at))))
        return

    row = _get(accounts_dir, uid, model) or {}
    attempts = int(row.get("attempts") or 0) + 1
    if attempts >= MAX_ATTEMPTS:
        drop(accounts_dir, uid, model)
        log("窗口探针：账号 [%s] 的 %s 连续 %d 次未重置，放弃（下一次 429 会重新登记）"
            % (uid8, model, attempts))
        return
    next_at = time.time() + RETRY_DELAY
    _update(accounts_dir, uid, model, reset_at=next_at, attempts=attempts,
            last_result=detail)
    log("窗口探针：账号 [%s] 的 %s 尚未重置（%s），%d 秒后重试 (%d/%d)"
        % (uid8, model, detail or "无详情", int(RETRY_DELAY), attempts, MAX_ATTEMPTS))


def _probe(account, model):
    """给 (账号, 模型) 发一个最小对话请求。

    返回 (ok, reset_at, detail)：
      * ok=True       探针成功，该模型的配额线已重置；
      * reset_at 非空 上游又回了 429，并给出了重置时间；
      * detail        失败原因，只用于日志与状态展示。
    """
    wb_proxy = wb_runtime.gateway()
    url = account.chat_base_url() + wb_proxy.CHAT_PATH
    headers = account.headers(purpose="chat")
    body = {
        "model": model,
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
        "stream": True,
        "max_tokens": PROBE_MAX_TOKENS,
        "reasoning_effort": "none",
    }
    try:
        payload = wb_proxy.build_upstream_body(body)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except Exception as exc:
        return False, None, "构造请求失败: %s" % exc
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with wb_accounts.urlopen(req, timeout=PROBE_TIMEOUT, proxy=account.proxy) as resp:
            resp.read()
        return True, None, ""
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(600).decode("utf-8", "replace")
        except Exception:
            detail = ""
        if exc.code == 429:
            return False, wb_proxy.parse_rate_limit_reset(detail), detail[:200]
        return False, None, "HTTP %d: %s" % (exc.code, detail[:120])
    except Exception as exc:
        return False, None, str(exc)[:200]


def has_window(accounts_dir, uid, model):
    """这条配额线是否已经登记过 429 窗口。

    wb_quota_scan 用它判断「本地是否已有该模型的限流记录」：一旦有，说明
    这条线已经被计入 PB 等待」状态，交给这里的探针到点处理即可，不必再按
    固定间隔主动查询。
    """
    return _get(accounts_dir, uid, model) is not None


def _get(accounts_dir, uid, model):
    with _lock:
        row = _load(accounts_dir).get(_key(uid, model))
        return dict(row) if row else None


def probe(account, model):
    """公开发一次最小探针请求。

    _probe 是内部名字（测试直接 patch 它），wb_quota_scan 需要同一条请求
    路径来判断某条配额线是否已经被打满，所以这里给它一个对外入口。
    """
    return _probe(account, model)


def _update(accounts_dir, uid, model, reset_at, attempts, last_result):
    with _lock:
        windows = _load(accounts_dir)
        row = windows.get(_key(uid, model))
        if row is None:
            return False
        row.update({
            "reset_at": float(reset_at),
            "attempts": int(attempts),
            "last_try": time.time(),
            "last_result": str(last_result or "")[:200],
        })
        _save(accounts_dir)
        return True
