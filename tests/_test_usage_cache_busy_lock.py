"""checkpoint 写盘不等正在全量重折的状态锁。

all-time 折叠在自己的状态锁里扫整个日志；GB 级日志的一次重折要几十分钟。
写盘路径会顺带收集所有推进过的类，而调用方此时持着 _usage_cache_write_lock、
往往还持着自己视图的缓存锁（by-account 持 _byacct_lock）。收集若阻塞等锁，
by-account 就陪着 snapshot 重折一起挂住，面板 /usage/by-account 超时 524。

钉住：状态锁被占时 _usage_cache_collect_kind 立刻返回 None（这一类跳过，
下次再写）；锁空闲时照常收集；锁在返回后已释放。

No network: the usage log and settings live in a temp directory.
"""
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="wb-busylock-")
USAGE_DIR = os.path.join(_TMP, "usage")
ACCOUNTS_DIR = os.path.join(_TMP, "accounts")
os.makedirs(USAGE_DIR, exist_ok=True)
os.makedirs(ACCOUNTS_DIR, exist_ok=True)
os.environ["WB_PROXY_USAGE_DIR"] = USAGE_DIR
os.environ["ACCOUNTS_DIR"] = ACCOUNTS_DIR

import wb_proxy as P

P.ACCOUNTS_DIR = ACCOUNTS_DIR

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


LOCKS = {
    "snapshot": P._usage_snap_state_lock,
    "by_account": P._byacct_state_lock,
    "analytics": P._analytics_state_lock,
    "series": P._series_state_lock,
    "remaining": P._remaining_state_lock,
}

print("[1] 状态锁被另一个线程占着：收集立即返回 None")
for kind, lock in LOCKS.items():
    taken = threading.Event()
    release = threading.Event()

    def holder():
        with lock:
            taken.set()
            release.wait(10)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    taken.wait(5)
    started = time.time()
    result = P._usage_cache_collect_kind(kind)
    elapsed = time.time() - started
    release.set()
    t.join(5)
    check("%s: busy -> None" % kind, result is None, result)
    check("%s: 不阻塞 (%.3fs)" % (kind, elapsed), elapsed < 1.0, elapsed)

print("[2] 锁空闲：收集正常返回且锁被释放")
P._byacct_state.update({"buckets": {"a": {
    "account": "a", "requests": 1, "prompt_tokens": 1, "completion_tokens": 1,
    "reasoning_tokens": 0, "cached_tokens": 0, "total_tokens": 2,
    "models": {"m": 1}}}, "offset": 10, "key": P._usage_log_key(), "tail": b"x"})
collected = P._usage_cache_collect_kind("by_account")
check("by_account: 空闲时照常收集", collected is not None and collected[1] == 10, collected)
for kind, lock in LOCKS.items():
    P._usage_cache_collect_kind(kind)
    ok = lock.acquire(blocking=False)
    if ok:
        lock.release()
    check("%s: 返回后锁已释放" % kind, ok)

print("\n%d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
