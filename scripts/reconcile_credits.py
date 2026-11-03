"""核对账本里的积分与账号余额的真实变化。

## 用法

    python scripts/reconcile_credits.py snapshot   # 记录基线（生成前跑）
    ...生成并等待任务完成...
    python scripts/reconcile_credits.py check      # 比对（生成后跑）

`check` 会把基线之后新增的已完成任务，逐条按**服务账号**归因，要求

    Σ(该账号任务 credit) == 该账号 remain 的减少量

## 两个坑（都是实测踩出来的）

1. **入账有延迟，约 2 分钟。** 任务刚 `completed` 时去拉余额会看到"一分没扣"，
   从而误判成"credit 是假的"。所以 `check` 会提示等待，别急着下结论。
2. **`credits.used` 不能用。** 它不是单调累计值（实测同一账号两次拉到
   84.28 → 41.42，反而变小）。`remain` 才是可信的那个数。

另：账号文件里的 `credits` 是上次拉取的快照（自带 `updated_iso`），
直接读文件算差值是错的——会把陈旧快照当成"这次的变化"。一律走 `fetch_credits()`。
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wb_accounts import Account  # noqa: E402

ACCOUNTS_DIR = os.path.join(ROOT, "accounts")
BASELINE = os.path.join(ROOT, "usage", "credit-baseline.json")
SKIP = {"settings.json", "model.json", "active_realm.json"}
#: 上游计费入账的延迟（实测 ~2 分钟），check 时用它决定要不要提示"再等等"。
SETTLE_SECONDS = 180


def live_balances():
    """实时余额 {uid完整id: {'remain','updated'}}。fetch_credits 才会更新。"""
    rows = {}
    for name in sorted(os.listdir(ACCOUNTS_DIR)):
        if not name.endswith(".json") or name in SKIP:
            continue
        path = os.path.join(ACCOUNTS_DIR, name)
        with open(path, encoding="utf-8") as fh:
            acc = Account(json.load(fh), path)
        acc.fetch_credits()
        credits = acc.credits or {}
        rows[name[:-5]] = {
            "remain": credits.get("remain") or 0.0,
            "updated": credits.get("updated_iso") or "",
            "error": acc.last_error or "",
        }
    return rows


def completed_tasks():
    path = os.path.join(ROOT, "media", "tasks.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        blob = json.load(fh)
    tasks = blob.get("tasks") if isinstance(blob, dict) else blob
    if isinstance(tasks, dict):
        tasks = list(tasks.values())
    return [t for t in (tasks or []) if t.get("status") == "completed" and t.get("credit")]


def snapshot():
    rows = live_balances()
    blob = {
        "taken_at": time.time(),
        "taken_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        "balances": {uid: row["remain"] for uid, row in rows.items()},
    }
    os.makedirs(os.path.dirname(BASELINE), exist_ok=True)
    with open(BASELINE, "w", encoding="utf-8") as fh:
        json.dump(blob, fh, ensure_ascii=False, indent=1)
    total = sum(blob["balances"].values())
    print("基线已存 %s（%d 个账号，合计 %.2f）" % (BASELINE, len(rows), total))
    for uid, remain in blob["balances"].items():
        print("  %-38s %10.2f" % (uid, remain))
    return 0


def check():
    if not os.path.exists(BASELINE):
        print("还没有基线：先跑 `reconcile_credits.py snapshot`，再生成，再 check。")
        return 1
    with open(BASELINE, encoding="utf-8") as fh:
        base = json.load(fh)
    taken_at = base.get("taken_at") or 0
    after = live_balances()

    # 只算基线之后开始的任务：基线之前的扣费不该记到这一轮
    events = [t for t in completed_tasks() if (t.get("created_at") or 0) >= taken_at]
    per_account = {}
    for task in events:
        uid = task.get("account") or ""
        per_account.setdefault(uid, []).append(task)

    print("基线 %s → 现在 %s（间隔 %d 秒）" % (
        base.get("taken_iso"), time.strftime("%Y-%m-%d %H:%M:%S"),
        int(time.time() - taken_at)))
    print()
    print("%-38s %10s %10s %10s %8s" % ("账号", "基线", "现在", "余额减少", "记账"))
    ok = True
    settled = True
    for uid in sorted(set(list(base["balances"].keys()) + list(after.keys()))):
        before = (base["balances"] or {}).get(uid)
        if before is None:
            continue
        now = (after.get(uid) or {}).get("remain")
        tasks = per_account.get(uid) or []
        booked = sum(float(t["credit"]) for t in tasks)
        if now is None:
            print("%-38s %10.2f %10s %10s %8.2f  ! 拉不到" % (uid, before, "—", "—", booked))
            ok = False
            continue
        drop = before - now
        mark = ""
        if abs(drop - booked) > 0.02:
            # 没扣够就再等一会儿：可能是还没入账，不是记错了
            ok = False
            settled = False
            mark = "  <-- 不符"
        print("%-38s %10.2f %10.2f %10.2f %8.2f%s" % (uid, before, now, drop, booked, mark))

    print()
    if ok:
        print("对得上：余额减少量 == 账本记账量。credit 是可信的支出数字。")
        return 0
    if not settled and (time.time() - taken_at) < SETTLE_SECONDS * 4:
        print("暂时对不上。上游计费入账约需 2 分钟，等一会儿再 check 一次——")
        print("刚完成就查会看到\"一分没扣\"，那是延迟，不代表记错。")
        return 2
    print("对不上，且已过入账延迟窗口：需要人工看是哪一侧的问题。")
    return 3


def main():
    action = (sys.argv[1] if len(sys.argv) > 1 else "check").strip().lower()
    if action == "snapshot":
        return snapshot()
    if action == "check":
        return check()
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
