"""并发保存 CodeArts 配置不能互相覆盖。

`wb_codearts.save_config()` 是「读 → 改 → 写」三步：
先 `load_config()` 读当前值，改掉 patch 里提到的键，再 `wb_settings.save()`
整份落盘。原先这段没有持锁，只有 `wb_settings.save()` 内部那把锁保护「写」
这一步——读到的旧值到落盘之间的窗口就露在外面。

后果与 `tests/_test_usage_atomic_append.py` 里那个丢行的故障同源：面板连续点
两次保存、或者两个浏览器同时保存，后一次会用「读到的旧值 + 自己那一项」覆盖
前一次刚写进去的另一项，两边都拿到成功，没有任何报错。

`wb_codearts` 里其实早就定义了 `_LOCK = threading.RLock()`（第 112 行），
但整个模块一次都没用过——是死代码。这里把它用起来，并用本套件钉住：

1. 两路并发各改**不同的键**，两个键最终都要保留；
2. 并发跑完文件仍是合法 JSON，且 `codearts` 段的每个字段都在允许范围内；
3. 单线程顺序保存的结果与并发结果一致（没有把 patch 语义改坏）。

为什么用线程而不是进程：`_LOCK` 是进程内锁，跨进程保护由
`wb_settings.save()` 的临时文件 + `os.replace()` 负责，那是另一层、已由
`_test_settings_atomic.py` 覆盖。本套件只钉进程内这一层。
"""
import json
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-codearts-save-race-")
ACCOUNTS = os.environ["ACCOUNTS_DIR"]

import wb_codearts as C  # noqa: E402
import wb_settings  # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


def reset():
    """每个用例从空白配置起步，避免互相污染。"""
    wb_settings.save(ACCOUNTS, {})
    C.load_config(ACCOUNTS)          # 让 settings 的 mtime 缓存有机会刷新
    return C.load_config(ACCOUNTS)


def hammer(patches, rounds=40, workers=None):
    """反复并发保存；返回每轮结束后的最终配置列表。"""
    workers = workers or len(patches)
    barrier = threading.Barrier(workers)
    results = []
    errors = []

    def one(patch):
        try:
            barrier.wait(timeout=10)
            for _ in range(rounds):
                C.save_config(ACCOUNTS, patch)
        except Exception as exc:          # noqa: BLE001 - 测试要看到任何异常
            errors.append(repr(exc))

    threads = [threading.Thread(target=one, args=(p,)) for p in patches]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    results.append(C.load_config(ACCOUNTS))
    return results, errors


def race_with_forced_window(patches, delay=0.2):
    """确定性地撑开「读到旧值 → 落盘」之间的窗口，再并发保存。

    GIL 让真实窗口窄到几十轮都碰不上（实测 40 轮 × 2 线程全过）。所以这里
    在 `load_config` 上挂一个一次性钩子：第一个调用者读到值之后先睡 `delay`，
    让另一个线程完整走完一次保存，然后再继续。这样无论调度怎样，第二个写入者
    读到的必然是**陈旧**的值——这正是并发保存丢更新的那个瞬间。

    有锁的时候：第二个线程会被挡在 `_LOCK` 外，等第一个写完才进来，读到的
    是新值，两个键都在。
    没锁的时候：第二个线程不受阻，用它读到的旧值整体覆盖，第一个键丢失。
    """
    original = C.load_config
    armed = {"n": 0}
    gate = threading.Event()
    done = threading.Event()

    def slow_load(accounts_dir):
        value = original(accounts_dir)
        # 只对前两个调用者生效：一个负责睡，一个负责在此期间写完整一轮。
        if armed["n"] < 2:
            armed["n"] += 1
            if armed["n"] == 1:
                gate.set()               # 告诉对方「我已经读到旧值了」
                done.wait(timeout=10)    # 等对方写完，再继续往下走
            elif armed["n"] == 2:
                gate.wait(timeout=10)
                done.set()
        return value

    C.load_config = slow_load
    try:
        barrier = threading.Barrier(len(patches))
        errors = []

        def one(patch):
            try:
                barrier.wait(timeout=10)
                C.save_config(ACCOUNTS, patch)
            except Exception as exc:      # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=one, args=(p,)) for p in patches]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    finally:
        C.load_config = original

    return C.load_config(ACCOUNTS), errors


print("[1] two writers touching DIFFERENT keys must both survive")

# 1a: 自然并发（大部分时候不会撞上窗口，但能挡住死锁与异常）
reset()
final, errs = hammer([
    {"timeout_seconds": 123.0},
    {"max_concurrent": 7},
], rounds=40)

check("no exception escaped either writer", not errs, errs)
cfg = final[-1]
check("timeout_seconds from writer A survived", cfg["timeout_seconds"] == 123.0,
      "got %r" % (cfg["timeout_seconds"],))
check("max_concurrent from writer B survived", cfg["max_concurrent"] == 7,
      "got %r" % (cfg["max_concurrent"],))

# 1b: 强制撑开读-改-写窗口。这一步才是真正复现丢更新的地方——
#     实测自然并发跑 40 轮根本撞不上（GIL 把窗口压到微秒级）。
reset()
forced_cfg, forced_errs = race_with_forced_window([
    {"timeout_seconds": 234.0},
    {"max_concurrent": 9},
])
check("forced-window run raised nothing", not forced_errs, forced_errs)
check("forced window: timeout_seconds survived",
      forced_cfg["timeout_seconds"] == 234.0,
      "got %r" % (forced_cfg["timeout_seconds"],))
check("forced window: max_concurrent survived",
      forced_cfg["max_concurrent"] == 9,
      "got %r" % (forced_cfg["max_concurrent"],))
check("forced window: no update lost",
      forced_cfg["timeout_seconds"] == 234.0 and forced_cfg["max_concurrent"] == 9,
      forced_cfg)


print("[2] the file stays valid JSON with every field in range")

raw = wb_settings.load(ACCOUNTS)
check("settings.json still parses as a dict", isinstance(raw, dict), type(raw))
section = raw.get("codearts")
check("codearts section is a dict", isinstance(section, dict), type(section))
if isinstance(section, dict):
    check("enabled is a bool", isinstance(section["enabled"], bool), section["enabled"])
    check("timeout_seconds within [1, 600]",
          1.0 <= section["timeout_seconds"] <= 600.0, section["timeout_seconds"])
    check("stream_timeout_seconds within [1, 3600]",
          1.0 <= section["stream_timeout_seconds"] <= 3600.0,
          section["stream_timeout_seconds"])
    check("max_concurrent within [1, 20]",
          1 <= section["max_concurrent"] <= 20, section["max_concurrent"])
    check("every DEFAULTS key is written",
          set(section) == set(C.DEFAULTS), sorted(section))


print("[3] other settings sections are never trampled")

# save_config 只该动 codearts 段。写一个无关段，保存几次后它必须原样还在。
reset()
wb_settings.save(ACCOUNTS, {"ui_language": "en", "keep_backups": 2})
C.save_config(ACCOUNTS, {"enabled": True})
C.save_config(ACCOUNTS, {"max_concurrent": 3})
after = wb_settings.load(ACCOUNTS)
check("ui_language preserved", after.get("ui_language") == "en", after.get("ui_language"))
check("keep_backups preserved", after.get("keep_backups") == 2, after.get("keep_backups"))
check("codearts applied", after["codearts"]["max_concurrent"] == 3,
      after["codearts"]["max_concurrent"])


print("[4] sequential saves behave identically (patch semantics intact)")

reset()
C.save_config(ACCOUNTS, {"enabled": True, "timeout_seconds": 45.0})
C.save_config(ACCOUNTS, {"max_concurrent": 5})
seq = C.load_config(ACCOUNTS)
check("enabled survived the second save", seq["enabled"] is True, seq["enabled"])
check("timeout survived the second save", seq["timeout_seconds"] == 45.0,
      seq["timeout_seconds"])
check("second save applied its own key", seq["max_concurrent"] == 5,
      seq["max_concurrent"])


print("[5] illegal values are still rejected and clamped, not written raw")

reset()
try:
    C.save_config(ACCOUNTS, "not-a-dict")
    raised = False
except ValueError:
    raised = True
check("non-dict patch raises ValueError", raised)

C.save_config(ACCOUNTS, {"timeout_seconds": 99999})       # 超过上限
check("timeout clamped to 600", C.load_config(ACCOUNTS)["timeout_seconds"] == 600.0,
      C.load_config(ACCOUNTS)["timeout_seconds"])
C.save_config(ACCOUNTS, {"max_concurrent": 0})            # 低于下限
check("max_concurrent clamped to 1", C.load_config(ACCOUNTS)["max_concurrent"] == 1,
      C.load_config(ACCOUNTS)["max_concurrent"])


print("[6] the module lock is actually used (guards against it going dead again)")

# `_LOCK` 曾经定义了却一次都没被 `with` 过。用 AST 钉住：save_config 的函数体
# 里必须出现一次对 _LOCK 的 with。这样将来有人把锁删掉时，这个套件会红。
import ast

tree = ast.parse(open(os.path.join(ROOT, "wb_codearts.py"), encoding="utf-8").read())
target = None
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name == "save_config":
        target = node
        break

check("save_config found", target is not None)


def uses_lock(fn):
    for node in ast.walk(fn):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            ctx = item.context_expr
            name = getattr(ctx, "id", None) or getattr(ctx, "attr", None)
            if name == "_LOCK":
                return True
    return False


check("save_config runs inside `with _LOCK`", target is not None and uses_lock(target))


print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
