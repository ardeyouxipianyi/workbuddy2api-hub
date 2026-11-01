"""多进程同时追加 usage.jsonl 不能写坏任何一行。

这是实测踩到过的真实故障，而且**两种表现都测到了**：

- 多进程同时 append：文件里出现过一条 `9.8}` 残片（两次 `.write()` 交错）；
- 单进程 8 线程（**网关的实际形态**）200 行为一组连写，2400 行只落进去 2224 行，
  丢了 7.3%；多进程时更差（480 行只落 408~430 行）。

后者更危险：不产生坏行，只是**悄悄少记**。读的一侧全部 `except: continue`，
所以丢掉的调用既不在请求数里，也不在积分合计里——账面直接少一笔，且没有任何报错。

所以这里断言两件事：

1. 行数正好等于写入次数（没有互相覆盖、没有丢行）；
2. **每一行都能完整解析**（没有半行）。
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import wb_usage_io

PASS = FAIL = 0

#: 子进程脚本：往同一个文件连写 N 行，行里带进程号便于核对。
CHILD = r'''
import json, os, sys
sys.path.insert(0, %r)
import wb_usage_io
path, worker, count = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
for index in range(count):
    row = {"worker": worker, "index": index, "at": 1700000000 + index,
           "padding": "x" * 200, "iso": "2026-11-01T00:00:00+0800"}
    wb_usage_io.append_line(path, json.dumps(row, ensure_ascii=False))
''' % (ROOT,)


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (label, extra))


def main():
    print("concurrent appends never corrupt a usage line")
    workers, per_worker = 4, 120
    expected = workers * per_worker

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "usage.jsonl")
        script = os.path.join(tmp, "child.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(CHILD)

        procs = [subprocess.Popen([sys.executable, script, path, str(w), str(per_worker)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for w in range(workers)]
        for proc in procs:
            proc.communicate()
            check("worker exits cleanly", proc.returncode == 0, proc.returncode)

        with open(path, "rb") as handle:
            raw = handle.read()
        text = raw.decode("utf-8")
        check("file ends with a newline", raw.endswith(b"\n"), repr(raw[-20:]))

        lines = [line for line in text.splitlines() if line.strip()]
        check("no line lost or duplicated", len(lines) == expected,
              "got %d, want %d" % (len(lines), expected))

        bad = 0
        seen = set()
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                bad += 1
                continue
            seen.add((row["worker"], row["index"]))
        check("every line parses", bad == 0, "%d bad lines" % bad)
        check("no row overwritten", len(seen) == expected,
              "unique %d, want %d" % (len(seen), expected))

    # 单进程多线程——这才是网关的真实形态（每个请求各写一行）
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "threads.jsonl")
        threads, per_thread = 8, 150

        def worker(index):
            for i in range(per_thread):
                wb_usage_io.append_line(
                    path, json.dumps({"w": index, "i": i, "padding": "x" * 200}))

        import threading
        handles = [threading.Thread(target=worker, args=(w,)) for w in range(threads)]
        for handle in handles:
            handle.start()
        for handle in handles:
            handle.join()

        with open(path, encoding="utf-8") as handle:
            lines = [line for line in handle if line.strip()]
        check("threaded: no line lost", len(lines) == threads * per_thread,
              "got %d, want %d" % (len(lines), threads * per_thread))
        parsed = 0
        for line in lines:
            try:
                json.loads(line)
                parsed += 1
            except ValueError:
                pass
        check("threaded: every line parses", parsed == len(lines),
              "%d/%d" % (parsed, len(lines)))

    # 单进程：一行里带换行也要落成一个物理行
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "one.jsonl")
        wb_usage_io.append_line(path, '{"a": 1}\n')
        wb_usage_io.append_line(path, '{"a": 2}')
        with open(path, encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
        check("trailing newline in input is not duplicated", len(rows) == 2, len(rows))
        check("values preserved", rows == [{"a": 1}, {"a": 2}], rows)

    # 写不进去时必须走 error_hook，且不抛异常（账本失败不该拖垮请求）
    seen_errors = []
    written = wb_usage_io.append_line(os.path.join(tempfile.gettempdir(),
                                                   "wb-usage-io-test",
                                                   "sub", "x.jsonl"),
                                      '{"ok": 1}')
    check("creates missing directories", written > 0, written)
    written = wb_usage_io.append_line("\0invalid\0path/usage.jsonl", '{"ok": 1}',
                                      error_hook=seen_errors.append)
    check("failure reported through hook", bool(seen_errors), seen_errors)
    check("failure returns zero but does not raise", written == 0, written)

    print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
