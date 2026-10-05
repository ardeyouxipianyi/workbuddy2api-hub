# -*- coding: utf-8 -*-
"""wb_reqlog.py — 請求歸檔的輪轉/查詢/指標（panel internal/reqlog 語義）。

usage.jsonl 是既有的事實來源（每請求一行 JSON）。本模組補上：

  - 輪轉：主檔超過 max_mb 時，把「保留窗口外」與「超出容量」的舊行搬到
    usage-archive-YYYYmmdd.jsonl，主檔原子重寫為最近的保留窗口。這樣既有
    聚合讀者（usage_snapshot 等）仍讀主檔即拿到保留窗口內的全部資料，不會
    因輪轉少算；查詢端則把主檔 + 歸檔一起讀。
  - 保留：刪除超過 retention_days 的歸檔檔。
  - 查詢：since/until/model/account/status/outcome/error/path/request_id/
    limit 多維過濾。
  - 指標：完成成功率、HTTP 成功率、平均耗時、TTFB 平均與 p50/p95。

純標準庫，Python 3.9 兼容。
"""

import glob
import json
import os
import threading
import time

_last_check = {}
CHECK_INTERVAL_SECONDS = 60
ARCHIVE_PREFIX = "usage-archive-"

# Appends (wb_proxy._persist_usage) and the rotate read->replace chain share
# this lock, so a row written by another thread can never land on the file
# that rotation is about to replace (BUG-3 in the 2026-10-05 audit).
LOCK = threading.RLock()


def _file_stamp(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def archive_files(usage_dir):
    return sorted(glob.glob(os.path.join(usage_dir, ARCHIVE_PREFIX + "*.jsonl")))


def log_files(usage_dir, main_name="usage.jsonl"):
    """Oldest first: archives, then the live main log."""
    files = archive_files(usage_dir)
    main = os.path.join(usage_dir, main_name)
    if os.path.exists(main):
        files.append(main)
    return files


def _iter_rows(path, max_bytes=8 * 1024 * 1024):
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    try:
        if size > max_bytes:
            with open(path, "rb") as fh:
                fh.seek(max(0, size - max_bytes))
                fh.readline()
                data = fh.read().decode("utf-8", "replace")
        else:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                data = fh.read()
    except OSError:
        return
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            yield row


def _files_stamp(usage_dir, main_name):
    """(path, mtime_ns, size) for every file that contributes rows."""
    stamp = []
    for path in log_files(usage_dir, main_name):
        try:
            st = os.stat(path)
        except OSError:
            continue
        stamp.append((path, st.st_mtime_ns, st.st_size))
    return tuple(stamp)


_rows_cache = {}


def read_rows(usage_dir, limit=None, main_name="usage.jsonl"):
    """All rows, oldest first; cached until a contributing file changes.

    /requests and /requests/metrics both parse the main log and every archive
    on the dashboard's 5s poll; the stamp cache makes that one parse per file
    change instead (audit #7).
    """
    key = (os.path.abspath(str(usage_dir or ".")), main_name)
    stamp = _files_stamp(usage_dir, main_name)
    with LOCK:
        hit = _rows_cache.get(key)
        rows = hit[1] if (hit and hit[0] == stamp) else None
    if rows is None:
        rows = []
        for path in log_files(usage_dir, main_name):
            rows.extend(_iter_rows(path))
        rows.sort(key=lambda row: float(row.get("at") or 0))
        with LOCK:
            _rows_cache[key] = (stamp, rows)
    if limit:
        return list(rows[-int(limit):])
    return list(rows)


def filter_rows(rows, model=None, account=None, status=None, outcome=None,
                error=None, path=None, since=None, until=None, request_id=None):
    out = []
    for row in rows:
        at = float(row.get("at") or 0)
        if since is not None and at < float(since):
            continue
        if until is not None and at > float(until):
            continue
        if model and row.get("model") != model:
            continue
        if account and row.get("account") != account:
            continue
        if status is not None and int(row.get("status") or 0) != int(status):
            continue
        if outcome and row.get("outcome") != outcome:
            continue
        if error is not None and bool(row.get("error")) != bool(error):
            continue
        if path and row.get("path") != path:
            continue
        if request_id and row.get("request_id") != request_id:
            continue
        out.append(row)
    return out


def _percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    idx = int(round((q / 100.0) * (len(ordered) - 1)))
    return ordered[max(0, min(len(ordered) - 1, idx))]


def compute_metrics(rows):
    """Completion/HTTP success rates plus elapsed/TTFB averages and percentiles."""
    total = len(rows)
    errors = sum(1 for row in rows if row.get("error"))
    http_ok = sum(1 for row in rows
                  if 200 <= int(row.get("status") or 0) < 300)
    elapsed = [float(row["elapsed_ms"]) for row in rows
               if isinstance(row.get("elapsed_ms"), (int, float))]
    ttfb = [float(row["ttft_ms"]) for row in rows
            if isinstance(row.get("ttft_ms"), (int, float))]
    return {
        "requests": total,
        "errors": errors,
        "completion_rate": ((total - errors) / total) if total else None,
        "http_success_rate": (http_ok / total) if total else None,
        "avg_elapsed_ms": (sum(elapsed) / len(elapsed)) if elapsed else None,
        "p50_elapsed_ms": _percentile(elapsed, 50),
        "p95_elapsed_ms": _percentile(elapsed, 95),
        "avg_ttfb_ms": (sum(ttfb) / len(ttfb)) if ttfb else None,
        "p50_ttfb_ms": _percentile(ttfb, 50),
        "p95_ttfb_ms": _percentile(ttfb, 95),
    }


def prune_archives(usage_dir, retention_days, now=None, log=None):
    """Delete archive files older than the retention window."""
    now = time.time() if now is None else float(now)
    cutoff = now - max(1, int(retention_days)) * 86400
    removed = 0
    for path in archive_files(usage_dir):
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    if removed and log:
        log("request archive: pruned %d expired file(s)" % removed)
    return removed


def compact_main(path, max_mb, retention_days, now=None, log=None):
    """Move rows outside the retention window / size budget into an archive."""
    now = time.time() if now is None else float(now)
    # The read -> tmp -> os.replace chain and every append share LOCK, so an
    # append from another thread waits here and then lands on the rotated file
    # instead of being erased with the old one (audit BUG-3).
    with LOCK:
        if not os.path.exists(path):
            return False
        max_bytes = max(1, int(max_mb)) * 1024 * 1024
        try:
            if os.path.getsize(path) < max_bytes:
                return False
        except OSError:
            return False
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
            read_stamp = _file_stamp(path)
        except OSError:
            return False
        cutoff = now - max(1, int(retention_days)) * 86400
        keep, archived = [], []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
                at = float(row.get("at") or 0)
            except Exception:
                keep.append(line)
                continue
            if at and at < cutoff:
                archived.append(line)
            else:
                keep.append(line)
        size = sum(len(line.encode("utf-8")) for line in keep)
        while keep and size > max_bytes:
            dropped = keep.pop(0)
            size -= len(dropped.encode("utf-8"))
            archived.append(dropped)
        if not archived:
            return False
        usage_dir = os.path.dirname(path)
        stamp = time.strftime("%Y%m%d", time.localtime(now))
        archive = os.path.join(usage_dir, ARCHIVE_PREFIX + stamp + ".jsonl")
        tmp = path + ".rotate.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.writelines(keep)
        except OSError:
            return False
        # Cooperating writers are held off by LOCK; this check narrows the
        # window for a writer that ignores the lock: if the file grew while
        # the keep/archive split was being written, abandon the rotation
        # instead of replacing its freshly appended row (audit BUG-3).
        if _file_stamp(path) != read_stamp:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
        try:
            with open(archive, "a", encoding="utf-8") as fh:
                fh.writelines(archived)
            os.replace(tmp, path)
        except OSError:
            return False
        if log:
            log("request archive: rotated %d row(s) into %s"
                % (len(archived), os.path.basename(archive)))
        return True

def rotate_if_needed(path, max_mb, retention_days, now=None, log=None):
    """Throttled rotation check (at most once per CHECK_INTERVAL_SECONDS)."""
    now = time.time() if now is None else float(now)
    key = os.path.abspath(path)
    if now - _last_check.get(key, 0.0) < CHECK_INTERVAL_SECONDS:
        return False
    _last_check[key] = now
    usage_dir = os.path.dirname(path) or "."
    prune_archives(usage_dir, retention_days, now=now, log=log)
    return compact_main(path, max_mb, retention_days, now=now, log=log)
