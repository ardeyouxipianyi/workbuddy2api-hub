"""把 usage.jsonl 里的坏行隔离出去，保持账本可解析。

坏行来自并发追加交错（已由 `wb_usage_io.append_line` 修掉根因）。已经写坏的那一行
留在文件里没有任何好处：读的一侧 `except: continue` 会静默跳过它，于是它既不在
请求数里、也不在合计里，只是让"为什么总数对不上"变得更难查。

所以这里做的是**隔离而不是删除**：坏行原文搬进 `usage.bad.jsonl`，并标注它原来
在第几行，便于人工核对；原文件用"写临时文件再 os.replace"的方式原子替换。

用法：
    python scripts/repair_usage_jsonl.py            # 只报告，不动文件
    python scripts/repair_usage_jsonl.py --apply    # 真修（自动留 .bak 备份）
"""
import argparse
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def default_log_path():
    usage_dir = os.environ.get("WB_PROXY_USAGE_DIR") or os.path.join(ROOT, "usage")
    return os.path.join(usage_dir, "usage.jsonl")


def scan(path):
    """返回 (总行数, 坏行列表[(行号, 原文)])。"""
    total = 0
    bad = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            total += 1
            try:
                json.loads(line)
            except ValueError:
                bad.append((number, line.rstrip("\n")))
    return total, bad


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default=None, help="usage.jsonl 路径")
    parser.add_argument("--apply", action="store_true", help="真的修改文件")
    args = parser.parse_args()

    path = args.path or default_log_path()
    if not os.path.exists(path):
        print("找不到账本：%s" % path)
        return 2

    total, bad = scan(path)
    print("账本：%s" % path)
    print("可解析行数：%d" % total)
    print("坏行数：%d" % len(bad))
    for number, text in bad[:10]:
        print("  第 %d 行：%r" % (number, text[:100]))
    if len(bad) > 10:
        print("  …还有 %d 条" % (len(bad) - 10))

    if not bad:
        print("无需修复。")
        return 0
    if not args.apply:
        print("\n（只报告。加 --apply 才会隔离坏行并重写文件）")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = "%s.bak-%s" % (path, stamp)
    shutil.copy2(path, backup)
    print("\n备份：%s" % backup)

    quarantine = os.path.join(os.path.dirname(path), "usage.bad.jsonl")
    with open(quarantine, "a", encoding="utf-8") as handle:
        for number, text in bad:
            handle.write(json.dumps({"source_line": number, "raw": text,
                                     "quarantined_at": int(time.time())},
                                    ensure_ascii=False) + "\n")
    print("坏行已隔离到：%s" % quarantine)

    # 保留坏行会被跳过的现有语义，这里只把它们从主账本里拿掉。
    bad_numbers = {number for number, _ in bad}
    temp = path + ".tmp"
    kept = 0
    with open(path, encoding="utf-8", errors="replace") as source, \
            open(temp, "w", encoding="utf-8", newline="\n") as target:
        for number, line in enumerate(source, start=1):
            if number in bad_numbers:
                continue
            if not line.strip():
                continue
            target.write(line if line.endswith("\n") else line + "\n")
            kept += 1
    os.replace(temp, path)

    after_total, after_bad = scan(path)
    print("重写完成：保留 %d 行，复核坏行 %d 条" % (after_total, len(after_bad)))
    if after_bad:
        print("仍有坏行，说明不只是并发交错——请人工检查。")
        return 1
    print("原文件备份在 %s，确认无误后可自行删除。" % backup)
    return 0


if __name__ == "__main__":
    sys.exit(main())
