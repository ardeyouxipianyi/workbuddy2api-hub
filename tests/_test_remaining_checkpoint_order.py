"""Remaining-usage checkpoint ordering: an out-of-order log still adopts.

`at` is the request *completion* clock and the fold appends rows in log order,
so two request threads finishing milliseconds apart leave a pair whose `at`
regresses a little (the real local log has such a pair 4.96ms apart at lines
18338/18339; the router's own 674KB checkpoint has one 0.47ms apart). The
loading side used to demand a monotonically ordered list, so that single
inversion rejected the whole remaining entry and every restart paid the cold
scan again (3.05s on the router, 4.5s measured later) even though the file
had just been written.

Pinned here:

  - the fold keeps every pair sorted by `at`, so a window sum equals the sum
    recomputed on the sorted sequence; on an unsorted buffer `_range_sum`'s
    bisection skipped whole stretches (the same rows could sum to 0);
  - a checkpoint written from an out-of-order log adopts after a restart with
    zero rows re-folded and a payload byte-identical to the cold fold's;
  - a *legacy* checkpoint (buffers in log order, the shape every pre-fix
    writer wrote - including the router's real file) is stable-sorted and
    repaired at adopt with the same byte-identical payload;
  - repair-unable data (a negative amount) is still rejected as a whole;
  - truncation and same-size rewrites still invalidate the checkpoint;
  - two independent processes - cold fold vs adopt from checkpoint, both
    through the public `remaining_usage()` - produce identical payload bytes.

The fixture shapes come from the real files: the 1100-row pair stands for the
router checkpoint's base>0/head=0 pair, the 560-row one for its head=178/176
pair, and the two regression constants are the real deltas.
"""
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-remaining-order-")
ACCOUNTS_DIR = os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_accounts
import wb_proxy as P

MODEL = "deepseek-v4.1-flash"
MODEL_B = "hy4"
HOUR = 3600
# 真实回退量：本机日志 L18338/18339 的相邻两行，与路由器 checkpoint 里
# 3a92cd99-.../deepseek-v4.1-flash 那条（head=178 的长尾，回退在最后一格）。
REAL_REGRESSION = 0.00496
ROUTER_REGRESSION = 0.0004658699035644531
RESET_STAMP = "2026-10-09 08:00:00"


def reset_epoch(stamp):
    """6004 文本里那个 UTC+8 墙钟解出来的 epoch（与主机时区无关）。"""
    detail = ('{"code":6004,"msg":"usage exceeds frequency limit, but don\'t '
              'worry, your usage will reset at %s UTC+8, alternatively, you can '
              'switch to the other models to continue using it.","requestId":"x"}'
              % stamp)
    parsed = P.parse_rate_limit_reset(detail)
    assert parsed is not None, "test body must parse"
    return parsed, detail


def cap_detail(reset):
    """反过来：把 epoch 格式化成 UTC+8 墙钟，解析回来就是同一个时刻。"""
    reset = int(reset)
    offset = reset + 8 * HOUR
    for _ in range(3):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(offset))
        detail = ('{"code":6004,"msg":"usage exceeds frequency limit, but don\'t '
                  'worry, your usage will reset at %s UTC+8, alternatively, you '
                  'can switch to the other models to continue using it.",'
                  '"requestId":"x"}' % stamp)
        parsed = P.parse_rate_limit_reset(detail)
        if parsed == reset:
            return detail
        offset += reset - parsed
    raise AssertionError("cap detail did not parse back to %r" % (reset,))


def usage_row(at, account, tokens, model=MODEL, realm="intl"):
    return {
        "at": at, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at)),
        "model": model, "stream": True, "outcome": "completed",
        "account": account, "realm": realm,
        # 放在最后：同尺寸重写的变异点必须落在 tail 签名看得到的 64 字节里。
        "total_tokens": tokens,
    }


def cap_row(at, account, detail, model=MODEL, realm="intl"):
    return {
        "at": at, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at)),
        "model": model, "error": True, "outcome": "failed", "status": 429,
        "message": detail, "account": account, "realm": realm,
    }


def write_log(rows):
    with io.open(P.USAGE_LOG, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def clear_events():
    try:
        os.unlink(P.LIMIT_EVENTS_FILE)
    except OSError:
        pass


def clear_daily():
    try:
        os.unlink(P.ESTIMATE_DAILY_FILE)
    except OSError:
        pass
    P._estimate_daily_cache["entry"] = ((P.ESTIMATE_DAILY_FILE, None, None), {})


def clear_checkpoint():
    try:
        os.unlink(P._usage_cache_path())
    except OSError:
        pass


def restart_fold():
    """清掉内存折叠与 TTL 缓存，等价于一次进程重启。"""
    with P._remaining_state_lock:
        P._remaining_state.clear()
        P._remaining_state.update({
            "events": [], "keys": set(), "usage": {}, "day": "",
            "log": {"offset": 0, "key": None, "tail": b""},
            "file": {"offset": 0, "key": None, "tail": b""},
        })
    with P._remaining_cache_lock:
        P._remaining_cache.update({"at": 0.0, "built_at": 0.0, "data": None})
    # 写盘节流的两张表也是进程内存：重启后它们同样清空（否则删掉文件重折
    # 一次会被「这个 offset 已经写过了」挡掉，测试里什么都写不出来）。
    with P._usage_cache_lock:
        P._usage_cache_progress.clear()
        P._usage_cache_checkpointed.clear()
        P._usage_cache_last_attempt = time.time()


def reload_checkpoint_cache():
    """让 _usage_cache_data() 重新读一次文件（新进程只读一次）。"""
    with P._usage_cache_lock:
        P._usage_cache_loaded = False
        P._usage_cache_read_result = None


def cache_env(min_bytes=None, min_seconds=None):
    for name, value in (("WB_USAGE_CACHE_MIN_BYTES", min_bytes),
                        ("WB_USAGE_CACHE_MIN_SECONDS", min_seconds)):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = str(value)


def account(uid, nickname="", realm="intl"):
    return wb_accounts.Account({"uid": uid, "nickname": nickname,
                                "accessToken": "t", "realm": realm})


def payload(accounts, now):
    return P._remaining_payload(now=now, accounts=accounts)


def find_row(data, uid, model=MODEL):
    for row in data["rows"]:
        if row["uid"] == uid and row["model"] == model:
            return row
    return None


def pair_buffer(uid, model=MODEL):
    with P._remaining_state_lock:
        return P._remaining_state["usage"].get((uid, model))


class CountingFold(object):
    """把 P._fold_remaining 换成计数器：采纳成功时折叠行数必须是 0。"""

    def __enter__(self):
        self.n = 0
        self.real = P._fold_remaining

        def counting(row, state):
            self.n += 1
            return self.real(row, state)

        P._fold_remaining = counting
        return self

    def __exit__(self, *exc):
        P._fold_remaining = self.real
        return False


def legacy_buffers(rows):
    """旧版写入侧（纯 append、不排序）会写出的 pair 缓冲。

    这是修复前 `_pair_append` 的语义：按日志顺序追加、按行裁剪。把它盖回
    checkpoint 里，就是路由器那份被整份拒绝的文件的等价物。
    """
    usage = {}
    for row in rows:
        uid = row.get("account")
        model = row.get("model")
        at = row.get("at") or 0
        if not uid or not model or not at:
            continue
        if row.get("status") == 429 or row.get("error") or row.get("usage_missing"):
            continue
        if P.row_outcome(row) == "client_aborted":
            continue
        key = (uid, model)
        buf = usage.get(key)
        if buf is None:
            buf = usage[key] = {"at": [], "cum": [], "head": 0, "base": 0}
        tokens = int(row.get("total_tokens") or 0)
        buf["at"].append(float(at))
        buf["cum"].append((buf["cum"][-1] if buf["cum"] else 0) + tokens)
        P._pair_trim(buf, at - P._REMAINING_KEEP_SECONDS)
    return usage


def rewrite_as_legacy(rows):
    """把 checkpoint 文件里的 remaining 缓冲换成旧版（日志顺序）形态。"""
    path = pathlib.Path(P._usage_cache_path())
    data = json.loads(path.read_text(encoding="utf-8"))
    entry = data["remaining"][0]
    buffers = legacy_buffers(rows)
    entry["usage"] = {
        "%s\x1f%s" % (uid, model): {"at": list(buf["at"]),
                                    "cum": list(buf["cum"]),
                                    "head": buf["head"], "base": buf["base"]}
        for (uid, model), buf in buffers.items()}
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(data, ensure_ascii=False))
    unsorted = [key for key, buf in buffers.items()
                if any(buf["at"][i] < buf["at"][i - 1]
                       for i in range(1, len(buf["at"])))]
    return buffers, unsorted


def fixture_rows():
    """一份把三种真实形态都摆进去的日志（顺序 = 日志追加顺序）。"""
    reset, detail = reset_epoch(RESET_STAMP)
    now = reset + 20 * HOUR
    rows = []
    # (1) acct-A 的长尾：55h 跨度 + 180s 间距，裁剪推进超过一半时批量压缩
    #     （base>0、head 归零）——对应路由器 checkpoint 里 base=1397930811、
    #     head=0 的那条。
    start = reset - 56 * HOUR
    for i in range(1100):
        rows.append(usage_row(start + i * 180, "acct-A", 1000 + i))
    # (2) 撞线前后的那一对：日志顺序是「晚的在前」，`at` 回退 1ms 且骑在
    #     重置时刻上——排序前 _range_sum 会把本段（[reset, now]）算成 0、
    #     把上段多算一行。
    rows.append(usage_row(reset + 0.001, "acct-A", 111))
    rows.append(usage_row(reset - 0.001, "acct-A", 222))
    # (3) 真实 4.96ms 回退对（本机日志 L18338/18339），落在存活区中段。
    rows.append(usage_row(reset + 2 * HOUR, "acct-A", 5000))
    rows.append(usage_row(reset + 2 * HOUR - REAL_REGRESSION, "acct-A", 7000))
    # 429 放在 acct-A 行之后、acct-B 行之前：折叠到它时 acct-A 的缓冲已就位，
    # 样本（与采纳后读回的事件）一致；同时让最后一行是 usage 行，同尺寸重写
    # 的变异点才落在 tail 签名看得到的 64 字节里。
    rows.append(cap_row(reset + HOUR, "acct-A", detail))
    # (4) acct-B 的短尾：裁剪过但没有压缩（head=300、base=0），对应路由器
    #     head=178/176、n=358/295 的那两条。
    start_b = reset - 40 * HOUR
    for i in range(560):
        rows.append(usage_row(start_b + i * 360, "acct-B", 2000 + i, model=MODEL_B))
    # (5) 真实 0.47ms 回退对，位置就是日志最后一对（路由器那条回退在末尾）。
    rows.append(usage_row(reset + 16 * HOUR, "acct-B", 900, model=MODEL_B))
    rows.append(usage_row(reset + 16 * HOUR - ROUTER_REGRESSION, "acct-B", 300,
                          model=MODEL_B))
    return rows, reset, now, detail


ROWS, RESET, NOW, CAP_DETAIL = fixture_rows()
ACCOUNTS = [account("acct-A"), account("acct-B", realm="intl")]


def naive(uid, lo, hi, model=MODEL):
    """存活区内按排序口径重算的窗口和（tokens 只看非 429 的成功行）。"""
    return sum(int(row.get("total_tokens") or 0) for row in ROWS
               if row.get("account") == uid and row.get("model") == model
               and row.get("at") and lo <= row["at"] <= hi
               and row.get("status") != 429 and not row.get("error")
               and P.row_outcome(row) != "client_aborted")


def is_sorted(values):
    return all(values[i] >= values[i - 1] for i in range(1, len(values)))


class RemainingCheckpointOrderTests(unittest.TestCase):
    def setUp(self):
        clear_events()
        clear_daily()
        clear_checkpoint()
        restart_fold()
        reload_checkpoint_cache()
        write_log(ROWS)

    def tearDown(self):
        cache_env()

    def fold_once(self):
        """在当前日志上折一遍并强制落盘；返回冷折载荷。"""
        cache_env(min_bytes=1, min_seconds=0)
        data = payload(ACCOUNTS, now=NOW)
        self.assertTrue(os.path.exists(P._usage_cache_path()),
                        "折过一遍之后应该写出 checkpoint")
        return data

    # ------------------------------------------------------------------
    # 折叠：乱序日志上的缓冲必须是有序的，窗口和按排序口径
    # ------------------------------------------------------------------
    def test_fold_keeps_pairs_sorted(self):
        self.fold_once()
        for uid, model in (("acct-A", MODEL), ("acct-B", MODEL_B)):
            buf = pair_buffer(uid, model)
            self.assertIsNotNone(buf)
            self.assertTrue(is_sorted(buf["at"]),
                            "%s/%s 的 at 必须有序：%r" % (uid, model, buf["at"]))
            self.assertTrue(is_sorted(buf["cum"]))
        # 两种真实形态：长尾压缩过（base>0），短尾只裁不压（base=0、head>0）。
        long_buf = pair_buffer("acct-A", MODEL)
        self.assertGreater(long_buf["base"], 0, "长尾应该触发过压缩")
        short_buf = pair_buffer("acct-B", MODEL_B)
        self.assertEqual(short_buf["base"], 0)
        self.assertGreater(short_buf["head"], 0, "短尾应该有裁剪过的死前缀")
        self.assertEqual(len(short_buf["at"]), 562)

    def test_window_sums_follow_the_sorted_sequence(self):
        # 三行的小日志：第三行回退 10s，两个窗口正好骑在回退对上。
        # 排序前 _range_sum 对 [1095, 2000] 只算回退那一段（5 而不是 15）、
        # 对 [1000, 1095] 会多算一行（30 而不是 20）。
        rows = [usage_row(1100.0, "acct-A", 10),
                usage_row(1090.0, "acct-A", 20),
                usage_row(1100.0, "acct-A", 5)]
        write_log(rows)
        payload(ACCOUNTS, now=NOW)
        buf = pair_buffer("acct-A", MODEL)
        self.assertTrue(is_sorted(buf["at"]))
        self.assertEqual(buf["at"], [1090.0, 1100.0, 1100.0])
        self.assertEqual(buf["cum"], [20, 30, 35])
        self.assertEqual(P._range_sum(buf, 1095.0, 2000.0), 15)
        self.assertEqual(P._range_sum(buf, 1000.0, 1095.0), 20)
        self.assertEqual(P._range_sum(buf, 1000.0, 2000.0), 35)
        # 旧版顺序（追加序）上的同一套二分：这两个窗口的值就是被修掉的
        # 「少算」形态，钉在这里防止退回。
        legacy = legacy_buffers(rows)[("acct-A", MODEL)]
        self.assertEqual(legacy["at"], [1100.0, 1090.0, 1100.0])

        def old_range_sum(buffer, lo, hi):
            at, cum = buffer["at"], buffer["cum"]
            j = P.bisect.bisect_left(at, lo, buffer["head"])
            k = P.bisect.bisect_right(at, hi, buffer["head"])
            if k <= j:
                return 0
            low = cum[j - 1] if j > 0 else buffer["base"]
            return cum[k - 1] - low

        self.assertEqual(old_range_sum(legacy, 1095.0, 2000.0), 5)
        self.assertEqual(old_range_sum(legacy, 1000.0, 1095.0), 30)

    def test_payload_window_and_segments_use_the_sorted_buffers(self):
        data = self.fold_once()
        row = find_row(data, "acct-A")
        # 本段 [reset, now]：只算重置时刻之后的行（111 在里面，222 不在）。
        self.assertEqual(row["used"], naive("acct-A", RESET, NOW))
        self.assertEqual(row["segments"][-1]["used"], naive("acct-A", RESET, NOW))
        # 上段 [reset-24h, reset]：222 在里面，111 不在（都在存活区内）。
        prev = row["segments"][0]
        self.assertEqual(prev["start"], RESET - 24 * HOUR)
        self.assertEqual(prev["used"], naive("acct-A", RESET - 24 * HOUR, RESET))
        # 骑在重置时刻上的那对（日志顺序是晚的在前）：排序前本段会把两行
        # 都算进来或都不算，二分给出的窗口和跟着乱序漂。
        buf = pair_buffer("acct-A", MODEL)
        self.assertTrue(is_sorted(buf["at"]))
        self.assertEqual(P._range_sum(buf, RESET, RESET + 0.001), 111)
        self.assertEqual(P._range_sum(buf, RESET - 0.001, RESET), 222)

    # ------------------------------------------------------------------
    # 采纳：乱序日志写出的 checkpoint、旧版形态、坏数据
    # ------------------------------------------------------------------
    def test_out_of_order_log_adopts_after_a_restart(self):
        first = self.fold_once()
        # 写入侧已经有序：新 checkpoint 不会再因为乱序被拒。
        written = json.loads(pathlib.Path(P._usage_cache_path())
                             .read_text(encoding="utf-8"))
        for pair in written["remaining"][0]["usage"].values():
            self.assertTrue(is_sorted(pair["at"]))
        with CountingFold() as counted:
            restart_fold()
            reload_checkpoint_cache()
            again = payload(ACCOUNTS, now=NOW)
        self.assertEqual(counted.n, 0, "重启后不该重折任何一行")
        self.assertEqual(json.dumps(again, ensure_ascii=False),
                         json.dumps(first, ensure_ascii=False))

    def test_legacy_log_order_checkpoint_is_repaired_on_adopt(self):
        first = self.fold_once()
        buffers, unsorted = rewrite_as_legacy(ROWS)
        self.assertTrue(unsorted, "夹具必须包含乱序对，否则测不出东西")
        with CountingFold() as counted:
            restart_fold()
            reload_checkpoint_cache()
            again = payload(ACCOUNTS, now=NOW)
        self.assertEqual(counted.n, 0, "旧版 checkpoint 必须被整理后采纳，而不是被拒")
        self.assertEqual(json.dumps(again, ensure_ascii=False),
                         json.dumps(first, ensure_ascii=False),
                         "整理后的载荷必须与冷折逐字节一致")
        # 内存里的缓冲已经规范化：有序、死前缀被丢弃、base 记绝对值。
        long_buf = pair_buffer("acct-A", MODEL)
        self.assertTrue(is_sorted(long_buf["at"]))
        self.assertEqual(long_buf["head"], 0)
        self.assertGreater(long_buf["base"], 0)
        self.assertEqual(len(long_buf["at"]), len(buffers[("acct-A", MODEL)]["at"])
                         - buffers[("acct-A", MODEL)]["head"])

    def test_legacy_checkpoint_with_a_negative_amount_is_rejected(self):
        # cum 回退 = 负增量，没有任何合法解释：整份拒绝、退回冷折（载荷不变）。
        # 形状校验先一步拦下它（cum 非降），_pair_repair 的负增量检查是同一
        # 契约的第二道防线。
        first = self.fold_once()
        path = pathlib.Path(P._usage_cache_path())
        data = json.loads(path.read_text(encoding="utf-8"))
        entry = data["remaining"][0]
        pair = entry["usage"][next(iter(entry["usage"]))]
        pair["at"] = list(reversed(pair["at"]))
        pair["cum"] = list(reversed(pair["cum"]))
        self.assertFalse(P._usage_cache_remaining_ok(entry))
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(data, ensure_ascii=False))
        with CountingFold() as counted:
            restart_fold()
            reload_checkpoint_cache()
            again = payload(ACCOUNTS, now=NOW)
        self.assertGreater(counted.n, 0, "坏数据必须退回冷折")
        self.assertEqual(json.dumps(again, ensure_ascii=False),
                         json.dumps(first, ensure_ascii=False))

    def test_legacy_checkpoint_with_an_empty_live_region_is_rejected(self):
        # head == len：存活区为空，整理不出任何条目 -> 整份拒绝、退回冷折。
        first = self.fold_once()
        path = pathlib.Path(P._usage_cache_path())
        data = json.loads(path.read_text(encoding="utf-8"))
        entry = data["remaining"][0]
        pair = entry["usage"][next(iter(entry["usage"]))]
        pair["head"] = len(pair["at"])
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(data, ensure_ascii=False))
        with CountingFold() as counted:
            restart_fold()
            reload_checkpoint_cache()
            again = payload(ACCOUNTS, now=NOW)
        self.assertGreater(counted.n, 0)
        self.assertEqual(json.dumps(again, ensure_ascii=False),
                         json.dumps(first, ensure_ascii=False))

    # ------------------------------------------------------------------
    # 位置校验不放松：截断 / 同尺寸重写照旧作废
    # ------------------------------------------------------------------
    def test_truncated_log_invalidates_the_checkpoint(self):
        self.fold_once()
        # 只留 acct-A 的行（1100 行长尾 + 4 行补丁对）：offset 落在文件外，
        # 429 事件与 acct-B 的行都不在了——没有锚点的组合退回「最近 24h」。
        prefix = ROWS[:1104]
        write_log(prefix)
        with CountingFold() as counted:
            restart_fold()
            reload_checkpoint_cache()
            data = payload(ACCOUNTS, now=NOW)
        self.assertGreater(counted.n, 0, "截短后必须从零重折")
        row = find_row(data, "acct-A")
        self.assertEqual(row["used"], naive("acct-A", NOW - 24 * HOUR, NOW))
        self.assertIsNone(find_row(data, "acct-B", MODEL_B),
                          "截短后的日志里没有 acct-B 的行")

    def test_same_size_rewrite_invalidates_the_checkpoint(self):
        first = self.fold_once()
        size = os.path.getsize(P.USAGE_LOG)
        with io.open(P.USAGE_LOG, encoding="utf-8") as fh:
            text = fh.read()
        needle = '"total_tokens": 300'          # 最后一行的值
        pos = text.rfind(needle)
        self.assertGreaterEqual(pos, 0)
        self.assertLess(len(text) - pos, 64, "变异点必须落在 tail 签名里")
        mutated = text[:pos] + '"total_tokens": 999' + text[pos + len(needle):]
        with io.open(P.USAGE_LOG, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(mutated)
        self.assertEqual(os.path.getsize(P.USAGE_LOG), size,
                         "同尺寸重写：size 校验对它是瞎的")
        with CountingFold() as counted:
            restart_fold()
            reload_checkpoint_cache()
            data = payload(ACCOUNTS, now=NOW)
        self.assertGreater(counted.n, 0, "tail 签名必须挡住这份 checkpoint")
        self.assertNotEqual(json.dumps(data, ensure_ascii=False),
                            json.dumps(first, ensure_ascii=False))

    # ------------------------------------------------------------------
    # 追加插入：落到死前缀里的乱序行不能被吞掉
    # ------------------------------------------------------------------
    def test_insert_before_the_dead_prefix_stays_live(self):
        # 死前缀 = 已被裁剪的旧行。新行（刚折进来）即使 at 比它们还老，也
        # 必须留在存活区：追加路径先把死前缀按 _pair_trim 的规则压掉再插入。
        buf = {"at": [], "cum": [], "head": 0, "base": 0}
        for i in range(700):
            P._pair_append(buf, 1000000.0 + i, 2)
        P._pair_trim(buf, 1000000.0 + 400)      # 死前缀 400 > 512 不成立
        self.assertEqual(buf["head"], 400)
        P._pair_append(buf, 1000000.0 + 300, 7)  # 比整个死前缀都老
        self.assertEqual(buf["head"], 0)
        self.assertTrue(is_sorted(buf["at"]))
        self.assertEqual(buf["base"], 400 * 2)
        self.assertEqual(len(buf["at"]), 301)
        self.assertEqual(P._range_sum(buf, 1000000.0 + 300, 1000000.0 + 300), 7)
        self.assertEqual(P._range_sum(buf, 1000000.0 + 399, 1000000.0 + 401), 4)

    def test_pair_repair_rejects_unrepairable_buffers(self):
        good = {"at": [2.0, 1.0], "cum": [5, 9], "head": 0, "base": 0}
        self.assertTrue(P._pair_repair(good))
        self.assertEqual(good["at"], [1.0, 2.0])
        self.assertEqual(good["cum"], [4, 9])
        self.assertEqual(good["base"], 0)
        backwards = {"at": [1.0, 2.0], "cum": [9, 5], "head": 0, "base": 0}
        self.assertFalse(P._pair_repair(backwards))
        empty = {"at": [1.0], "cum": [5], "head": 1, "base": 0}
        self.assertFalse(P._pair_repair(empty))
        mismatched = {"at": [1.0], "cum": [5, 6], "head": 0, "base": 0}
        self.assertFalse(P._pair_repair(mismatched))

    # ------------------------------------------------------------------
    # 两个独立进程：冷折 vs 采纳，公开路径逐字节一致
    # ------------------------------------------------------------------
    def test_two_processes_cold_and_adopted_payloads_match(self):
        write_log(ROWS)
        accounts_file = os.path.join(_TMP, "accounts.json")
        with io.open(accounts_file, "w", encoding="utf-8", newline="\n") as fh:
            json.dump([["acct-A", "intl"], ["acct-B", "intl"]], fh)
        cache_env(min_bytes=0, min_seconds=0)
        cold = self.run_child(accounts_file)
        self.assertGreater(cold["folded"], 0, "第一个进程必须是冷折")
        adopted = self.run_child(accounts_file)
        self.assertEqual(adopted["folded"], 0, "第二个进程必须从 checkpoint 采纳")
        self.assertEqual(json.dumps(cold["payload"], ensure_ascii=False),
                         json.dumps(adopted["payload"], ensure_ascii=False))

    CHILD_SOURCE = r'''
import io, json, os, sys, time as _t
FROZEN = __FROZEN__
_t.time = lambda: FROZEN
sys.path.insert(0, __ROOT__)
import wb_accounts
import wb_proxy as P

class _Pool(object):
    accounts = [
        wb_accounts.Account({"uid": uid, "accessToken": "t", "realm": realm})
        for uid, realm in json.load(
            io.open(os.environ["WB_CHILD_ACCOUNTS"], encoding="utf-8"))]

P.POOL = _Pool()
folded = {"n": 0}
real = P._fold_remaining

def counting(row, state):
    folded["n"] += 1
    return real(row, state)

P._fold_remaining = counting
payload = P.remaining_usage(ttl=0)
with io.open(os.environ["WB_CHILD_OUT"], "w", encoding="utf-8",
             newline="\n") as fh:
    json.dump({"payload": payload, "folded": folded["n"]}, fh,
              ensure_ascii=False)
print("child ok")
'''

    def run_child(self, accounts_file):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = os.path.join(_TMP, "child_remaining.py")
        with io.open(script, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(self.CHILD_SOURCE.replace("__FROZEN__", repr(NOW))
                     .replace("__ROOT__", repr(root)))
        dest = os.path.join(_TMP, "child_out.json")
        if os.path.exists(dest):
            os.unlink(dest)
        env = dict(os.environ)
        env["PYTHONPATH"] = root
        env["WB_CHILD_ACCOUNTS"] = accounts_file
        env["WB_CHILD_OUT"] = dest
        done = subprocess.run([sys.executable, script], cwd=root, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=300)
        out = done.stdout.decode("utf-8", "replace")
        if done.returncode != 0 or not os.path.exists(dest):
            raise AssertionError("子进程失败 rc=%s\n%s"
                                 % (done.returncode, out[-2000:]))
        with io.open(dest, encoding="utf-8") as fh:
            return json.load(fh)


if __name__ == "__main__":
    unittest.main(verbosity=2)
