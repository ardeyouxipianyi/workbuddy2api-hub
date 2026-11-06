"""checkpoint 写盘被跳过之后必须有人接着写（P0 回归套件）。

现场事故：某一类（remaining）的 checkpoint 连续 8 天没落盘，进程 CPU 跑满、
日志里一个字都没有；每次重启都从旧 offset 再全量重折一遍（548MB 日志里
350MB 要重扫）。根因是写盘路径的三条「静默跳过」——状态锁被占、状态不可
序列化、写盘异常——任何一条成立就 `continue`，下一次不一定轮得到它。

钉住这些事：
  1. 跳过必须留痕（节流日志）并登记补写，绝不静默，且不阻塞请求线程；
  2. 补写线程在锁释放后**真的把文件写出来**；
  3. 写成功后取消补写计划、退避计数归零；
  4. 写盘抛异常同样留痕 + 登记补写（不是只有「收集为空」那条路）；
  5. 冷启动（本进程还没折过）不许误报「日志被替换/截断」，只有续读失败才报；
  6. checkpoint 落后日志很多时要告警，落后少/形状不认识时闭嘴。

No network: usage/accounts live in a temp directory.
"""
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="wb-retry-save-")
USAGE_DIR = os.path.join(_TMP, "usage")
ACCOUNTS_DIR = os.path.join(_TMP, "accounts")
os.makedirs(USAGE_DIR, exist_ok=True)
os.makedirs(ACCOUNTS_DIR, exist_ok=True)
os.environ["WB_PROXY_USAGE_DIR"] = USAGE_DIR
os.environ["ACCOUNTS_DIR"] = ACCOUNTS_DIR
# 补写退避压到毫秒级、写盘节流阈值关掉：套件要的是「会不会写」，不是等 30 秒。
os.environ["WB_USAGE_CACHE_RETRY_MIN_SECONDS"] = "0.05"
os.environ["WB_USAGE_CACHE_MIN_SECONDS"] = "0"
# 日志文件当场建出来：_usage_log_key() 要拿到真实 (size, dev, inode)。
with open(os.path.join(USAGE_DIR, "usage.jsonl"), "wb") as _fh:
    _fh.write(b'{"at": 1}\n')

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


# 拦截模块级 log()，套件要断言「有没有留痕」。
LOGS = []
_REAL_LOG = P.log


def _capture(message, level=None, tag=None):
    LOGS.append(str(message))


P.log = _capture

OFFSET = 3 * 1024 * 1024      # 超过写盘字节阈值（1MB），保证判定为「该写」
CHECKPOINT = P._usage_cache_kind_path("remaining")


def skip_retry_for(kind="remaining"):
    with P._usage_cache_retry_lock:
        return "remaining" in P._usage_cache_retry_due


def attempts_for(kind="remaining"):
    with P._usage_cache_retry_lock:
        return P._usage_cache_retry_attempts.get(kind)


def reset_state(offset=OFFSET, key="auto"):
    """给 remaining 一份可序列化的活状态，并让写盘判定认为「有推进、该写」。"""
    P._remaining_state.update({
        "events": [],
        "usage": {},
        "keys": set(),
        "day": "",
        "log": {"offset": offset,
                "key": P._usage_log_key() if key == "auto" else key,
                "tail": b"x"},
        "file": {"offset": 0, "key": None, "tail": b""},
    })
    with P._usage_cache_lock:
        P._usage_cache_progress["remaining"] = offset
        P._usage_cache_checkpointed.pop("remaining", None)
        P._usage_cache_last_attempt = 0.0


def clear_all():
    P._usage_cache_clear_retry("remaining")
    with P._usage_cache_lock:
        P._usage_cache_progress.pop("remaining", None)
        P._usage_cache_checkpointed.pop("remaining", None)
    try:
        os.remove(CHECKPOINT)
    except OSError:
        pass


def wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


print("[1] 状态锁被占：跳过要留痕 + 登记补写，且不阻塞请求线程")
del LOGS[:]
reset_state()
holder_taken = threading.Event()
holder_release = threading.Event()


def holder():
    with P._remaining_state_lock:
        holder_taken.set()
        holder_release.wait(15)


t = threading.Thread(target=holder, daemon=True)
t.start()
holder_taken.wait(5)
started = time.time()
returned = P._usage_cache_maybe_save("remaining", OFFSET)
elapsed = time.time() - started
scheduled = skip_retry_for()
attempts = attempts_for()
holder_release.set()
t.join(5)

check("跳过不阻塞 (%.3fs)" % elapsed, elapsed < 1.0, elapsed)
check("没写成时返回 False", returned is False, returned)
check("寄存器里登记了补写", scheduled, P._usage_cache_retry_due)
check("退避计数已 +1", isinstance(attempts, int) and attempts >= 1, attempts)
check("打了一条 skip 日志", any("skipped" in m and "remaining" in m for m in LOGS), LOGS)
check("日志能区分「锁被占」",
      any("state lock busy" in m for m in LOGS), LOGS)

print("[2] 锁释放后补写线程真的把文件写出来")
check("checkpoint 文件已生成",
      wait_for(lambda: os.path.exists(CHECKPOINT)), CHECKPOINT)
check("checkpointed 追到本类 offset",
      wait_for(lambda: P._usage_cache_checkpointed.get("remaining") == OFFSET),
      P._usage_cache_checkpointed)

print("[3] 写成功后取补写计划、退避归零")
check("补写计划已清除", wait_for(lambda: not skip_retry_for()), P._usage_cache_retry_due)
check("退避计数归零", wait_for(lambda: attempts_for() is None), P._usage_cache_retry_attempts)

print("[4] 写盘抛异常：同样留痕 + 登记补写（不是只有「收集为空」那条路）")
del LOGS[:]
reset_state()
_real_write = P._usage_cache_write_kind


def boom(kind, entries):
    raise OSError("disk on fire")


P._usage_cache_write_kind = boom
try:
    returned = P._usage_cache_maybe_save("remaining", OFFSET)
finally:
    P._usage_cache_write_kind = _real_write
check("写失败时返回 False", returned is False, returned)
check("写失败留痕（带异常类型）",
      any("write failed" in m and "OSError" in m for m in LOGS), LOGS)
check("写失败也登记补写", skip_retry_for(), P._usage_cache_retry_due)

print("[5] 跳过日志有节流：短时间内反复跳过不刷屏")
# 这一段不能被补写线程抢跑（它写成功会清掉 skip 节流状态），把退避临时拉长。
os.environ["WB_USAGE_CACHE_RETRY_MIN_SECONDS"] = "60"
del LOGS[:]
reset_state()
P._usage_cache_clear_retry("remaining")      # 清掉前面用例留下的节流时间戳
for _ in range(3):
    P._usage_cache_note_skip("remaining", why="test")
check("三次跳过只记一条",
      len([m for m in LOGS if "skipped" in m]) == 1, LOGS)
os.environ["WB_USAGE_CACHE_RETRY_MIN_SECONDS"] = "0.05"
clear_all()

print("[6] 冷启动不误报「日志被替换/截断」，续读失败才报")
del LOGS[:]
cold_state = {"events": [], "usage": {}, "keys": set(), "day": "",
              "log": {"offset": 0, "key": None, "tail": b""},
              "file": {"offset": 0, "key": None, "tail": b""}}
P._remaining_refresh(cold_state)
check("冷启动不告警",
      not [m for m in LOGS if "replaced or truncated" in m], LOGS)

stale_state = {"events": [], "usage": {}, "keys": set(), "day": "",
               "log": {"offset": 8, "key": (0, 1, 1), "tail": b"zz"},
               "file": {"offset": 0, "key": (0, 1, 1), "tail": b""}}
P._remaining_refresh(stale_state)
check("续读失败告警",
      len([m for m in LOGS if "replaced or truncated" in m]) >= 1, LOGS)

print("[7] 补写没写成时归还名额：闸门挡回要重登记，已登记的不重复登记")
clear_all()
with P._usage_cache_retry_lock:
    P._usage_cache_retry_due.clear()
    P._usage_cache_retry_attempts.clear()
P._usage_cache_requeue_if_needed("remaining", True)
check("写成了不留名额", not skip_retry_for(), P._usage_cache_retry_due)
P._usage_cache_requeue_if_needed("remaining", False)
check("没写成（闸门挡回）要重登记", skip_retry_for(), P._usage_cache_retry_due)
attempts_before = attempts_for()
P._usage_cache_requeue_if_needed("remaining", False)
check("已登记时不重复登记（避免退避翻倍）",
      attempts_for() == attempts_before, (attempts_before, attempts_for()))
clear_all()

print("[8] 状态不可序列化：原因要如实上报（不是一律说成锁被占）")
del LOGS[:]
P._remaining_state.update({
    "events": [], "usage": {}, "keys": set(), "day": "",
    "log": {"offset": OFFSET, "key": P._usage_log_key(), "tail": None},   # 形状坏了
    "file": {"offset": 0, "key": None, "tail": b""},
})
with P._usage_cache_lock:
    P._usage_cache_progress["remaining"] = OFFSET
    P._usage_cache_checkpointed.pop("remaining", None)
    P._usage_cache_last_attempt = 0.0
returned = P._usage_cache_maybe_save("remaining", OFFSET)
check("形状坏了不写盘", returned is False, returned)
check("收集失败时记下了真实原因（形如 state not serialisable）",
      P._usage_cache_collect_reason.get("remaining", "").startswith("state not serialisable"),
      P._usage_cache_collect_reason)
check("日志里是「不可序列化」而不是「锁被占」",
      any("not serialisable" in m for m in LOGS)
      and not any("state lock busy" in m for m in LOGS), LOGS)
clear_all()

print("[9] checkpoint 落后日志很多时要告警，落后少/形状不认识时闭嘴")
del LOGS[:]
_real_log_key = P._usage_log_key
P._usage_log_key = lambda path=None: (200 * 1024 * 1024, 1, 1)   # 假装日志 200MB
try:
    check("小幅落后不告警",
          P._usage_cache_report_lag("remaining", [{"offset": 198 * 1024 * 1024}]) is None)
    message = P._usage_cache_report_lag("remaining", [{"offset": 10 * 1024 * 1024}])
    check("大幅落后给出告警文案", isinstance(message, str) and "lag" in message, message)
    # 布尔值在 Python 里是 int：True 不能被当成 offset（否则会谎报落后整份日志）
    check("形状不认识/布尔值不告警也不抛",
          P._usage_cache_report_lag("remaining", [{"offset": None}, {}]) is None
          and P._usage_cache_report_lag("remaining", [{"offset": True}]) is None)
finally:
    P._usage_log_key = _real_log_key

clear_all()
P._usage_cache_forget()
P.log = _REAL_LOG
print("\n%d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
