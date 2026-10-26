"""Remaining-usage estimate: budget learned from cap events, counted per window.

The upstream caps each account's usage of a model inside a 24h window and names
the reset clock on the 429 (code 6004). The budget itself is never told to the
gateway, so this feature infers it from accounts that actually hit the cap: the
usage of that model in the 24h ending at the reset is one sample, and the
per-realm average of those samples is the estimated budget. Remaining usage for
an account is then the budget minus what it burned since its own window started
(the reset clock it was last handed).

Pinned here, with the usage log synthesised in a temp directory and `now`
injected so nothing depends on the wall clock:

  - the sample is the 24h before the reset, successful rows only, and the 429
    rows themselves never count as usage;
  - an account that capped repeatedly weighs once (per-account mean first, then
    across accounts) and repeated 429 rows for one reset collapse to one event;
  - both the intl and the cn reset wordings produce cap events;
  - the window usage counts from the last reset; a pair still cooling reports
    remaining 0 with the reset clock that hands the quota back;
  - a pair with no cap event falls back to the trailing 24h (conservative) and
    is flagged estimated;
  - segmented accounting: the spent window's usage is truncated at the cap,
    the window after the reset re-counts from scratch (the old window's usage
    never counts into the new one), the payload carries both segments, chained
    caps' samples start at the previous reset, and a cap persisted in the
    credential file anchors the pair even when the event journal has none;
  - the per-model total is the sum of the usable groups' remaining (cooling
    groups count 0 and never drag it down);
  - models with no usage yesterday or today do not take part (rows and totals),
    while their budget samples stay in the payload marked inactive;
  - the budget is the weighted mean of the last three local days (today 6 /
    yesterday 3 / the day before 1, normalised over the days that have
    samples), the earlier days read back from usage/estimate-daily.json
    instead of recomputed; a pair with no sample in three days has no budget;
  - crossing local midnight (or the first build after a restart) archives the
    day that just passed into estimate-daily.json: its per-day budget (account
    mean first, then across accounts) and each pair's pre/post usage (pre + post
    == that day's usage, the truncation figures the panel asks for) — merged
    idempotently, never overwriting a day already persisted;
  - the archive keeps the last three days and only the last two days' segments;
  - the fold resumes incrementally from its byte offset.
"""
import io
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-remaining-")
ACCOUNTS_DIR = os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_accounts
import wb_settings
import wb_proxy as P
import wb_usage as U

MODEL = "deepseek-v4.1-flash"
HOUR = 3600


def reset_epoch(stamp):
    """The epoch parse_rate_limit_reset() reads out of an intl 6004 body.

    Built through the parser itself so the expectation never depends on the
    host timezone: the body names a wall clock, and the parser is the one
    thing that decides what that clock means on this machine.
    """
    detail = ('{"code":6004,"msg":"usage exceeds frequency limit, but don\'t '
              'worry, your usage will reset at %s UTC+8, alternatively, you can '
              'switch to the other models to continue using it.","requestId":"x"}'
              % stamp)
    parsed = P.parse_rate_limit_reset(detail)
    assert parsed is not None, "test body must parse"
    return parsed, detail


def local_epoch(year, month, day, hour=0, minute=0, second=0):
    """本地墙钟 -> epoch（日敏感用例的锚点，与主机时区无关）。

    预算按撞线当天的**本地日期**分桶，所以「哪一天」必须由本地墙钟决定：
    reset_epoch() 的 UTC+8 墙钟在别的时区会落到另一天。mktime 按本地时区
    解释，tm_isdst=-1 让它自己判夏令时——构造出的 epoch 的本地日就是这里
    写的那个日期。
    """
    return time.mktime((year, month, day, hour, minute, second, 0, 0, -1))


def cap_detail(reset):
    """一段 6004 文本，其 UTC+8 墙钟解析回给定的 reset（任意主机时区）。

    reset_epoch() 是「先写墙钟、再解析」；这里反过来：先有 epoch（local_epoch
    锚住的本地日），再把它格式化成 UTC+8 墙钟——解析器对 UTC+8 的换算是纯
    算术，与主机时区无关，解回来就是同一个时刻。文本只有整秒，所以 reset
    取整。夏令时区里解析器拿标准偏移（time.timezone）换算、与 mktime 的
    夏令时偏移差一小时，这里用真解析结果反过来校正戳（映射是分段线性的，
    校正一两次就精确命中）。
    """
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
        "total_tokens": tokens, "account": account, "realm": realm,
    }


def cap_row(at, account, detail, model=MODEL, realm="intl"):
    return {
        "at": at, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at)),
        "model": model, "error": True, "outcome": "failed", "status": 429,
        "message": detail, "account": account, "realm": realm,
    }


def write_log(rows):
    with io.open(P.USAGE_LOG, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_log(rows):
    with io.open(P.USAGE_LOG, "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def clear_events():
    try:
        os.unlink(P.LIMIT_EVENTS_FILE)
    except OSError:
        pass


def clear_daily():
    """清掉按天归档文件与它的解析缓存（每个用例从空归档开始）。"""
    try:
        os.unlink(P.ESTIMATE_DAILY_FILE)
    except OSError:
        pass
    U._estimate_daily_cache["entry"] = ((P.ESTIMATE_DAILY_FILE, None, None), {})


def clear_log():
    """清掉 usage.jsonl（每个用例从空日志开始）。

    整套用例共用一个 USAGE_LOG：write_log() 截断，append_log() 只追加。于是
    「跨天归档」那条把行追加进了上一个用例留下的日志里，而留下的那些行用的是
    **真实时钟**（含 429 行的重置时刻），它们的重置时刻会插进本用例的窗口、被
    _cap_window_start 当成链式起点，样本从 820 被截成 120。真实部署只有一个
    进程、一个日志，不存在「上一个用例」，所以这是测试隔离问题。
    """
    try:
        os.unlink(P.USAGE_LOG)
    except OSError:
        pass


def restart_fold():
    """Drop the in-memory fold the way a process restart would."""
    with U._remaining_state_lock:
        U._remaining_state.clear()
        U._remaining_state.update({
            "events": [], "keys": set(), "usage": {}, "day": "",
            "log": {"offset": 0, "key": None, "tail": b""},
            "file": {"offset": 0, "key": None, "tail": b""},
        })
    with U._remaining_cache_lock:
        U._remaining_cache.update({"at": 0.0, "built_at": 0.0, "data": None})


def remaining_checkpoint_path():
    """remaining 这一类状态的 checkpoint 文件（v4 起一类一份）。"""
    return U._usage_cache_kind_path("remaining")


def reload_checkpoint_cache():
    """忘掉已读的 checkpoint（新进程只读一次，测试要重放）。"""
    U._usage_cache_forget()


def cache_env(min_bytes=None, min_seconds=None):
    """测试用写盘节流：默认值（1MB / 900s）在测试里几乎永远不落盘。"""
    if min_bytes is None:
        os.environ.pop("WB_USAGE_CACHE_MIN_BYTES", None)
    else:
        os.environ["WB_USAGE_CACHE_MIN_BYTES"] = str(min_bytes)
    if min_seconds is None:
        os.environ.pop("WB_USAGE_CACHE_MIN_SECONDS", None)
    else:
        os.environ["WB_USAGE_CACHE_MIN_SECONDS"] = str(min_seconds)


def account(uid, nickname="", realm="intl"):
    return wb_accounts.Account({"uid": uid, "nickname": nickname,
                                "accessToken": "t", "realm": realm})


def payload(accounts, now):
    return U._remaining_payload(now=now, accounts=accounts)


def find_row(data, uid, model=MODEL):
    for row in data["rows"]:
        if row["uid"] == uid and row["model"] == model:
            return row
    return None


class RemainingUsageTests(unittest.TestCase):
    def setUp(self):
        clear_events()
        clear_daily()
        clear_log()
        restart_fold()
    def test_sample_is_the_24h_before_the_reset(self):
        reset, detail = reset_epoch("2026-10-10 15:18:17")
        write_log([
            # outside the window: must not count
            usage_row(reset - 25 * HOUR, "acct-A", 111111),
            # inside the window: counted
            usage_row(reset - 23 * HOUR, "acct-A", 1000),
            usage_row(reset - 2 * HOUR, "acct-A", 2000),
            # failed rows never count
            dict(usage_row(reset - HOUR, "acct-A", 999999),
                 error=True, outcome="failed", status=500),
            # the 429 that reports the cap
            cap_row(reset - HOUR + 60, "acct-A", detail),
        ])
        data = payload([account("acct-A")], now=reset + HOUR)
        self.assertEqual(len(data["budgets"]), 1)
        budget = data["budgets"][0]
        self.assertEqual(budget["model"], MODEL)
        self.assertEqual(budget["realm"], "intl")
        self.assertEqual(budget["avg"], 3000)
        self.assertEqual(budget["n"], 1)
        self.assertEqual(len(budget["samples_list"]), 1)
        sample = budget["samples_list"][0]
        self.assertEqual(sample["account"], "acct-A")
        self.assertEqual(sample["sample"], 3000)
        self.assertTrue(sample["at_iso"])

    def test_budget_averages_accounts_not_events(self):
        # 新口径（近 3 天加权）下这条同时钉两件事：同一天内一个账号的多个
        # 样本先按账号平均（撞两次的账号在当天只算一个均值），跨天再按
        # 6/3/1 加权。时间锚在本地日上（跨天分桶看的是撞线当天的本地日期）。
        r1 = local_epoch(2026, 10, 9, 10, 0, 0)         # 昨天
        r2 = local_epoch(2026, 10, 10, 10, 0, 0)        # 今天
        r3 = local_epoch(2026, 10, 10, 12, 0, 0)        # 今天
        r4 = local_epoch(2026, 10, 10, 14, 0, 0)        # 今天，acct-A 当天第二次
        write_log([
            # acct-A caps on both days: yesterday's sample 1000, today's two
            # samples average (2000 + 3000) / 2 = 2500.
            usage_row(r1 - 2 * HOUR, "acct-A", 1000),
            cap_row(r1 - HOUR, "acct-A", cap_detail(r1)),
            usage_row(r2 - 2 * HOUR, "acct-A", 2000),
            cap_row(r2 - HOUR, "acct-A", cap_detail(r2)),
            usage_row(r4 - 2 * HOUR, "acct-A", 3000),
            cap_row(r4 - HOUR, "acct-A", cap_detail(r4)),
            # acct-B caps once today: sample 3000
            usage_row(r3 - 2 * HOUR, "acct-B", 3000),
            cap_row(r3 - HOUR, "acct-B", cap_detail(r3)),
        ])
        data = payload([account("acct-A"), account("acct-B")], now=r3 + HOUR)
        budget = data["budgets"][0]
        # 今天 {A: mean(2000,3000)=2500, B: 3000} → 2750（权重 6）；
        # 昨天 {A: 1000} → 1000（权重 3）；(6×2750 + 3×1000)/9 = 2167。
        self.assertEqual(budget["avg"], 2167)
        self.assertEqual(budget["min"], 1000)            # 各天账号均值的极值
        self.assertEqual(budget["max"], 3000)
        self.assertEqual(budget["n"], 3)                 # 各天账号数之和
        self.assertEqual([(d["day"], d["w"], d["avg"], d["n"])
                          for d in budget["days"]],
                         [("2026-10-10", 6, 2750, 2), ("2026-10-09", 3, 1000, 1)])

    def test_budget_weights_the_last_three_days_six_three_one(self):
        """近 3 天加权：今天 6 / 昨天 3 / 前天 1，只在有样本的天之间归一。

        三天各撞一次线（样本 300/200/100）：估算 = (6×300 + 3×200 + 1×100)/10
        = 250；min/max 取各天账号均值的极值、n 为各天账号数之和；载荷里 days
        列出各天构成。前两天的口径在跨天时归档，最后这次构建直接读文件。
        """
        days = [local_epoch(2026, 10, 13),               # 前天（本地零点）
                local_epoch(2026, 10, 14),               # 昨天
                local_epoch(2026, 10, 15)]               # 今天
        now = local_epoch(2026, 10, 15, 12, 0, 0)
        real_time = time.time
        clock = {"now": now}
        time.time = lambda: clock["now"]
        try:
            # 逐天追加（真实日志就是这样长出来的：写入时当天的行才是最新的，
            # 26h 缓冲的裁剪按最后一行算）。
            for day, tokens in zip(days, (100, 200, 300)):
                with io.open(P.USAGE_LOG, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(usage_row(day + 8 * HOUR, "acct-A", tokens),
                                        ensure_ascii=False) + "\n")
                clock["now"] = day + 9 * HOUR            # 撞线时刻
                U.note_limit_event(account("acct-A"), MODEL,
                                   day + 9 * HOUR + 30 * 60)
            # 进程在昨天与今天各构建过一次：前两天的口径依次归档落盘。
            clock["now"] = days[1] + 12 * HOUR
            payload([account("acct-A")], now=clock["now"])
            clock["now"] = days[2] + 8 * HOUR
            payload([account("acct-A")], now=clock["now"])
            clock["now"] = now
            data = payload([account("acct-A")], now=now)
        finally:
            time.time = real_time
        budget = data["budgets"][0]
        # (6×300 + 3×200 + 1×100)/10 = 250
        self.assertEqual(budget["avg"], 250)
        self.assertEqual(budget["min"], 100)             # 各天账号均值的极值
        self.assertEqual(budget["max"], 300)
        self.assertEqual(budget["n"], 3)                 # 各天账号数之和
        self.assertEqual([(d["day"], d["w"], d["avg"], d["n"])
                          for d in budget["days"]],
                         [("2026-10-15", 6, 300, 1),
                          ("2026-10-14", 3, 200, 1),
                          ("2026-10-13", 1, 100, 1)])
        # 载荷里的行按加权后的预算算剩余（今天没用过，剩全额）。
        row = find_row(data, "acct-A")
        self.assertEqual(row["budget"], 250)
        self.assertEqual(row["samples"], 3)
        self.assertEqual(row["remaining"], 250)

    def test_crossing_midnight_archives_the_finished_day(self):
        """跨天归档：刚过去的那天的预算与分段写进 estimate-daily.json。

        预算是撞线时刻落在那天的样本（先按账号平均、再跨账号平均）；分段是
        那天每个 (uid, model) 的「截断前 / 截断后」用量：post = 恢复时刻落
        在当天的窗口在当天内的用量（从恢复时刻到「下一个撞线时刻或当天
        结束」），pre = 当天用量 − post。该天已在文件里就不覆盖（幂等），
        之后构建直接读文件里的值、不重算。
        """
        reset1 = local_epoch(2026, 10, 14, 7, 30, 0)     # 昨天 07:30（本地）
        reset2 = local_epoch(2026, 10, 14, 20, 30, 0)    # 昨天第二次撞线
        day_end = local_epoch(2026, 10, 15, 0, 0, 0)     # 今天 00:00
        now = day_end + 30 * 60                          # 今天 00:30
        real_time = time.time
        clock = {"now": reset1 - 30 * 60}                # 撞线时刻：昨天 07:00
        time.time = lambda: clock["now"]
        try:
            # 逐条追加（真实日志就是这样长出来的：26h 缓冲的裁剪按最后一行算）。
            append_log([usage_row(reset1 - 90 * 60, "acct-A", 4000),   # 截断前
                        usage_row(reset1 - 15 * 60, "acct-A", 50)])    # 截断前残行
            U.note_limit_event(account("acct-A"), MODEL, reset1)
            append_log([usage_row(reset1 + 90 * 60, "acct-A", 700),    # 截断后
                        usage_row(reset2 - HOUR, "acct-A", 120),       # 截断后
                        usage_row(reset1 - 90 * 60, "acct-B", 200)])   # 另一组合
            clock["now"] = reset2 - 30 * 60              # 撞线时刻：昨天 20:00
            U.note_limit_event(account("acct-A"), MODEL, reset2)
            append_log([usage_row(reset2 + 30 * 60, "acct-A", 180),    # 截断后
                        usage_row(day_end - HOUR, "acct-A", 300),      # 截断后
                        usage_row(day_end + 5 * 60, "acct-C", 77)])    # 今天
        finally:
            time.time = real_time
        accounts = [account("acct-A"), account("acct-B"), account("acct-C")]
        data = payload(accounts, now=now)

        with io.open(P.ESTIMATE_DAILY_FILE, encoding="utf-8") as fh:
            stored = json.load(fh)
        self.assertEqual(stored["schema"], 1)
        day = stored["days"]["2026-10-14"]
        # 两个样本（4000 与 820）同属一个账号：先按账号平均 = 2410。
        self.assertEqual(day["budgets"]["intl\x1f" + MODEL],
                         {"avg": 2410, "min": 2410, "max": 2410, "n": 1})
        segments = day["segments"]
        # post：窗口 1 = [07:30, 20:00) = 820（到下一个撞线时刻为止），
        # 窗口 2 = [20:30, 当天结束) = 480；合计 1300。
        self.assertEqual(segments["acct-A\x1f" + MODEL],
                         {"pre": 4050, "post": 1300})
        self.assertEqual(segments["acct-B\x1f" + MODEL],
                         {"pre": 200, "post": 0})
        # 两个都是 0 的组合不写（acct-C 昨天没有用量）。
        self.assertNotIn("acct-C\x1f" + MODEL, segments)
        # 不变式：pre + post == 当天该组合的用量（A：4000+50+700+120+180+300）。
        self.assertEqual(segments["acct-A\x1f" + MODEL]["pre"]
                         + segments["acct-A\x1f" + MODEL]["post"], 5350)
        # 本次构建的估算直接吃这份归档（今天没有样本，昨天权重 3）。
        self.assertEqual(data["budgets"][0]["avg"], 2410)
        self.assertEqual([(d["day"], d["w"]) for d in data["budgets"][0]["days"]],
                         [("2026-10-14", 3)])

        # 幂等 + 「调昨天的数据不重新计算」：改掉文件里的值再构建一次——估算
        # 读的是文件里的 999999，文件也不会被重算的值覆盖。
        with io.open(P.ESTIMATE_DAILY_FILE, encoding="utf-8") as fh:
            tampered = json.load(fh)
        tampered["days"]["2026-10-14"]["budgets"]["intl\x1f" + MODEL]["avg"] = 999999
        with io.open(P.ESTIMATE_DAILY_FILE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(tampered, ensure_ascii=False))
        again = payload(accounts, now=now)
        self.assertEqual(again["budgets"][0]["avg"], 999999)
        with io.open(P.ESTIMATE_DAILY_FILE, encoding="utf-8") as fh:
            kept = json.load(fh)
        self.assertEqual(kept["days"]["2026-10-14"]["budgets"]
                         ["intl\x1f" + MODEL]["avg"], 999999)

        # 重启（state["day"] 为空）会让第一次构建再试归档昨天一次：重算出的
        # 4000 不得盖掉文件里已持久化的值（该天已在文件里就不覆盖）。
        restart_fold()
        reload_checkpoint_cache()
        after_restart = payload(accounts, now=now)
        self.assertEqual(after_restart["budgets"][0]["avg"], 999999)
        with io.open(P.ESTIMATE_DAILY_FILE, encoding="utf-8") as fh:
            kept = json.load(fh)
        self.assertEqual(kept["days"]["2026-10-14"]["budgets"]
                         ["intl\x1f" + MODEL]["avg"], 999999)

    def test_repeated_429_rows_for_one_reset_collapse(self):
        reset, detail = reset_epoch("2026-10-10 09:00:00")
        write_log([
            usage_row(reset - 2 * HOUR, "acct-A", 500),
            cap_row(reset - HOUR, "acct-A", detail),
            cap_row(reset - HOUR + 5, "acct-A", detail),
            cap_row(reset - HOUR + 9, "acct-A", detail),
        ])
        data = payload([account("acct-A")], now=reset + HOUR)
        self.assertEqual(data["budgets"][0]["n"], 1)
        self.assertEqual(data["budgets"][0]["avg"], 500)

    def test_cn_reset_wording_is_a_cap_event(self):
        reset, _ = reset_epoch("2026-10-10 14:44:59")
        detail = ('{"code":6004,"msg":"usage exceeds frequency limit，将在 '
                  '2026-10-10 14:44:59 UTC+8 重置，或切换其他模型继续使用"}')
        write_log([
            usage_row(reset - 3 * HOUR, "acct-C", 700, realm="cn"),
            cap_row(reset - HOUR, "acct-C", detail, realm="cn"),
        ])
        data = payload([account("acct-C", realm="cn")], now=reset + HOUR)
        self.assertEqual(data["budgets"][0]["realm"], "cn")
        self.assertEqual(data["budgets"][0]["avg"], 700)

    def test_remaining_counts_from_the_reset(self):
        reset, detail = reset_epoch("2026-10-10 08:00:00")
        write_log([
            # the spent window that produced the cap: 5000 tokens total
            usage_row(reset - 20 * HOUR, "acct-A", 2000),
            usage_row(reset - 2 * HOUR, "acct-A", 3000),
            cap_row(reset - HOUR, "acct-A", detail),
            # the new window, after the reset: 1200 burned so far
            usage_row(reset + 30 * 60, "acct-A", 700),
            usage_row(reset + 45 * 60, "acct-A", 500),
        ])
        data = payload([account("acct-A")], now=reset + HOUR)
        row = find_row(data, "acct-A")
        self.assertEqual(row["budget"], 5000)
        self.assertEqual(row["used"], 1200)
        self.assertEqual(row["remaining"], 3800)
        self.assertFalse(row["estimated"])
        self.assertFalse(row["cooling"])
        self.assertEqual(row["window_start"], reset)
        self.assertEqual(row["reset_at"], reset)

    def test_cooling_pair_reports_zero_remaining(self):
        reset, detail = reset_epoch("2026-10-10 16:00:00")
        write_log([
            usage_row(reset - 20 * HOUR, "acct-A", 4000),
            usage_row(reset - HOUR, "acct-A", 1000),
            cap_row(reset - 30 * 60, "acct-A", detail),
        ])
        # now is inside the spent window: the reset clock is still ahead
        now = reset - 10 * 60
        data = payload([account("acct-A")], now=now)
        row = find_row(data, "acct-A")
        self.assertTrue(row["cooling"])
        self.assertEqual(row["remaining"], 0)
        self.assertEqual(row["reset_at"], reset)
        self.assertEqual(row["window_start"], reset - U.LIMIT_WINDOW_SECONDS)
        self.assertEqual(row["used"], 5000)

    def test_unknown_window_uses_trailing_24h(self):
        # acct-A provides the budget for the model (5000) through a cap event
        # on the *previous day* — it gets archived into estimate-daily.json at
        # this build and stays in the 3-day estimate; acct-B never capped.
        reset = local_epoch(2026, 10, 10, 8, 0, 0)       # 昨天 08:00（本地）
        now = local_epoch(2026, 10, 11, 14, 0, 0)        # 今天 14:00
        write_log([
            usage_row(reset - 2 * HOUR, "acct-A", 5000),
            cap_row(reset - HOUR, "acct-A", cap_detail(reset)),
            # acct-B: 900 inside the trailing 24h, 400 outside it
            usage_row(now - 30 * HOUR, "acct-B", 400),
            usage_row(now - 20 * HOUR, "acct-B", 900),
        ])
        data = payload([account("acct-A"), account("acct-B")], now=now)
        row = find_row(data, "acct-B")
        self.assertTrue(row["estimated"])
        self.assertFalse(row["cooling"])
        self.assertEqual(row["window_start"], now - U.LIMIT_WINDOW_SECONDS)
        self.assertEqual(row["used"], 900)
        self.assertEqual(row["budget"], 5000)
        self.assertEqual(row["remaining"], 4100)
        # acct-A's reset is 30h old: that window expired long ago and the
        # current one cannot be anchored either, so it is estimated too - the
        # trailing 24h is a lower bound, never a claim to know the window.
        stale = find_row(data, "acct-A")
        self.assertTrue(stale["estimated"])
        self.assertEqual(stale["window_start"], now - U.LIMIT_WINDOW_SECONDS)
        self.assertEqual(stale["used"], 0)

    def test_budgeted_model_with_zero_usage_is_reported(self):
        reset, detail = reset_epoch("2026-10-09 08:00:00")
        now = reset + 20 * HOUR
        write_log([
            usage_row(reset - 2 * HOUR, "acct-A", 600),
            cap_row(reset - HOUR, "acct-A", detail),
        ])
        data = payload([account("acct-A"), account("acct-B", nickname="fresh")],
                       now=now)
        row = find_row(data, "acct-B")
        self.assertIsNotNone(row)
        self.assertEqual(row["used"], 0)
        self.assertEqual(row["remaining"], 600)
        self.assertEqual(row["samples"], 1)

    def test_models_without_a_budget_keep_their_usage_row(self):
        reset, detail = reset_epoch("2026-10-09 08:00:00")
        now = reset + 20 * HOUR
        write_log([
            usage_row(reset - 2 * HOUR, "acct-A", 600),
            cap_row(reset - HOUR, "acct-A", detail),
            usage_row(now - HOUR, "acct-B", 42, model="glm-5.3"),
        ])
        data = payload([account("acct-A"), account("acct-B")], now=now)
        row = find_row(data, "acct-B", model="glm-5.3")
        self.assertIsNotNone(row)
        self.assertIsNone(row["budget"])
        self.assertIsNone(row["remaining"])
        self.assertEqual(row["used"], 42)

    def test_realm_budgets_stay_separate(self):
        r1, d1 = reset_epoch("2026-10-09 08:00:00")
        r2, d2 = reset_epoch("2026-10-09 20:00:00")
        write_log([
            usage_row(r1 - 2 * HOUR, "acct-A", 1000),
            cap_row(r1 - HOUR, "acct-A", d1),
            usage_row(r2 - 2 * HOUR, "acct-C", 4000, realm="cn"),
            cap_row(r2 - HOUR, "acct-C", d2, realm="cn"),
        ])
        data = payload([account("acct-A"), account("acct-C", realm="cn")],
                       now=r2 + HOUR)
        by_realm = {b["realm"]: b for b in data["budgets"]}
        self.assertEqual(by_realm["intl"]["avg"], 1000)
        self.assertEqual(by_realm["cn"]["avg"], 4000)

    def test_fold_resumes_incrementally(self):
        reset, detail = reset_epoch("2026-10-09 08:00:00")
        now = reset + 20 * HOUR
        write_log([
            usage_row(reset - 2 * HOUR, "acct-A", 600),
            cap_row(reset - HOUR, "acct-A", detail),
        ])
        data = payload([account("acct-A")], now=now)
        self.assertEqual(find_row(data, "acct-A")["used"], 0)

        # A row appended after the first pass is folded in without a rescan.
        with io.open(P.USAGE_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(usage_row(now - HOUR, "acct-A", 123),
                                ensure_ascii=False) + "\n")
        data = payload([account("acct-A")], now=now)
        self.assertEqual(find_row(data, "acct-A")["used"], 123)

    def test_shrunk_log_starts_over(self):
        # 撞线那天（昨天）的口径在第一次构建时已归档进 estimate-daily.json：
        # 日志被换掉后重折的只是用量行，预算从归档里继续读（新口径）。
        reset = local_epoch(2026, 10, 9, 8, 0, 0)        # 昨天 08:00（本地）
        now = local_epoch(2026, 10, 10, 4, 0, 0)         # 今天 04:00
        write_log([
            usage_row(reset - 2 * HOUR, "acct-A", 600),
            cap_row(reset - HOUR, "acct-A", cap_detail(reset)),
        ])
        self.assertEqual(payload([account("acct-A")], now=now)["budgets"][0]["avg"], 600)

        # The log was replaced by a shorter one (rotation / a copy in place):
        # the cached offset points past its end, so the fold starts over
        # instead of resuming into unrelated bytes.
        write_log([usage_row(reset - 2 * HOUR, "acct-A", 800)])
        data = payload([account("acct-A")], now=now)
        row = find_row(data, "acct-A")
        self.assertEqual([b["avg"] for b in data["budgets"]], [600])
        self.assertEqual(row["budget"], 600)
        self.assertEqual(row["used"], 800)
        self.assertEqual(row["remaining"], 0)

    def test_range_sum_matches_a_naive_sum(self):
        # The window sums are cumulative-sum lookups now; pin them against a
        # plain sum over the same rows, head-trim and compaction included.
        reset, detail = reset_epoch("2026-10-10 08:00:00")
        rows = [usage_row(reset - 25 * HOUR + i * 137, "acct-A", 100 + i * 7)
                for i in range(600)]
        write_log(rows)
        now = reset + HOUR
        payload([account("acct-A")], now=now)

        def naive(lo, hi):
            return sum(r["total_tokens"] for r in rows if lo <= r["at"] <= hi)

        with U._remaining_state_lock:
            buf = U._remaining_state["usage"][("acct-A", MODEL)]
            for lo, hi in ((rows[0]["at"] - 1, now), (reset - 24 * HOUR, now),
                           (now - HOUR, now), (rows[300]["at"], rows[400]["at"]),
                           (rows[-1]["at"] + 1, now), (rows[0]["at"], rows[5]["at"])):
                self.assertEqual(U._range_sum(buf, lo, hi), naive(lo, hi), (lo, hi))
            # Move the head forward: entries before it are out of the window
            # by construction, and the sums must agree from there on.
            U._pair_trim(buf, rows[100]["at"])
        self.assertEqual(buf["head"], 100)
        with U._remaining_state_lock:
            for lo, hi in ((rows[100]["at"], now), (rows[150]["at"], now),
                           (now - HOUR, now)):
                self.assertEqual(U._range_sum(buf, lo, hi), naive(lo, hi), (lo, hi))
            # Past the batch threshold the dead prefix is compacted away, and
            # the cumulative sums must still subtract correctly.
            U._pair_trim(buf, rows[550]["at"])
        self.assertEqual(buf["head"], 0)
        self.assertEqual(len(buf["at"]), 50)
        with U._remaining_state_lock:
            for lo, hi in ((rows[550]["at"], now), (rows[580]["at"], now),
                           (rows[560]["at"], rows[590]["at"])):
                self.assertEqual(U._range_sum(buf, lo, hi), naive(lo, hi), (lo, hi))

    def test_repeated_compaction_keeps_base_absolute(self):
        """压缩记前缀必须记绝对值。

        cum 从最早的一行累计、切片不重置，所以 base 应当直接等于
        cum[head-1]；写成累加会让第二次压缩起把前缀算两遍，之后任何左端
        落在保留区之前的区间求和都会变成负数（真机上 deepseek 的「上段」
        因此显示过 -1293.5M）。
        """
        tokens = 3
        buf = {"at": [], "cum": [], "head": 0, "base": 0}
        for i in range(2600):
            U._pair_append(buf, 1000000.0 + i, tokens)
        # 第一次压缩：裁掉前 1500 行（死前缀超过一半才批量压）。
        U._pair_trim(buf, 1000000.0 + 1500)
        self.assertEqual((buf["head"], len(buf["at"])), (0, 1100))
        self.assertEqual(buf["base"], 1500 * tokens)
        # 第二次压缩：再裁掉 700 行。base 必须是绝对值（2200 行），不是叠加。
        U._pair_trim(buf, 1000000.0 + 2200)
        self.assertEqual((buf["head"], len(buf["at"])), (0, 400))
        self.assertEqual(buf["base"], 2200 * tokens)
        # 左端落在保留区之前：求和等于保留区内的和，且不可能为负。
        self.assertEqual(U._range_sum(buf, 999000.0, 1000000.0 + 2599),
                         len(buf["at"]) * tokens)
        self.assertEqual(U._range_sum(buf, 1000000.0 + 2300, 1000000.0 + 2400),
                         101 * tokens)

    def test_payload_is_served_from_cache_within_the_ttl(self):
        # 数据锚在真实 now 上（载荷吃的是真实时钟）：固定日期会在跨天后被
        # 「昨日或今日」过滤掉，用例就再也看不到行。
        now = time.time()
        reset = int(now) - 20 * HOUR
        write_log([
            usage_row(reset - 2 * HOUR, "acct-A", 600),
            cap_row(reset - HOUR, "acct-A", cap_detail(reset)),
        ])

        class _Pool(object):
            accounts = [account("acct-A")]

        old_pool = P.POOL
        P.POOL = _Pool()
        try:
            first = U.remaining_usage(ttl=30)
            self.assertEqual(find_row(first, "acct-A")["used"], 0)

            # A row appended behind the cache must NOT be picked up inside the
            # TTL (that is the whole point: no rescan per poll)...
            with io.open(P.USAGE_LOG, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(usage_row(now - HOUR, "acct-A", 123),
                                    ensure_ascii=False) + "\n")
            cached = U.remaining_usage(ttl=30)
            self.assertIs(cached, first)
            self.assertEqual(find_row(cached, "acct-A")["used"], 0)

            # ...and an expired entry rebuilds and picks it up.
            rebuilt = U.remaining_usage(ttl=0)
            self.assertIsNot(rebuilt, first)
            self.assertEqual(find_row(rebuilt, "acct-A")["used"], 123)
        finally:
            P.POOL = old_pool

    def test_etag_is_stable_until_a_rebuild(self):
        reset, detail = reset_epoch("2026-10-09 08:00:00")
        write_log([
            usage_row(reset - 2 * HOUR, "acct-A", 600),
            cap_row(reset - HOUR, "acct-A", detail),
        ])
        U.remaining_usage(ttl=30)
        tag = U.remaining_usage_etag()
        self.assertTrue(tag and tag.startswith('"'))
        # Same cache entry -> same validator (the poll gets a 304)…
        U.remaining_usage(ttl=30)
        self.assertEqual(U.remaining_usage_etag(), tag)
        # …and a rebuild mints a new one.
        time.sleep(0.01)
        U.remaining_usage(ttl=0)
        self.assertNotEqual(U.remaining_usage_etag(), tag)

    def test_checkpoint_saves_and_a_restart_folds_nothing(self):
        # 重启免冷扫：折一遍写出 checkpoint，模拟重启（清内存 + 重读文件）后
        # 必须从 checkpoint 续读——折叠的行数为 0，载荷与冷折逐值一致。
        reset, detail = reset_epoch("2026-10-09 08:00:00")
        now = reset + 20 * HOUR
        write_log([usage_row(reset - 24 * HOUR + i * 60, "acct-A", 10 + i)
                   for i in range(400)] +
                  [cap_row(reset - HOUR, "acct-A", detail)])
        accounts = [account("acct-A")]
        cache_env(min_bytes=1, min_seconds=0)
        try:
            first = payload(accounts, now=now)
            self.assertTrue(os.path.exists(remaining_checkpoint_path()),
                            "折过一遍之后应该写出 checkpoint")
            self.assertGreater(len(first["rows"]), 0)

            folded = {"n": 0}
            real_fold = U._fold_remaining

            def counting_fold(row, state):
                folded["n"] += 1
                return real_fold(row, state)

            U._fold_remaining = counting_fold
            try:
                restart_fold()
                reload_checkpoint_cache()
                again = payload(accounts, now=now)
            finally:
                U._fold_remaining = real_fold
            self.assertEqual(folded["n"], 0, "重启后不该重折任何一行")
            self.assertEqual(again, first, "checkpoint 续读的载荷应与冷折一致")
        finally:
            cache_env()

    def test_checkpoint_rejects_a_corrupted_entry(self):
        # at 非单调 = 二分求和的全部前提没了，加载端必须整份拒绝、退回冷折。
        reset, detail = reset_epoch("2026-10-09 08:00:00")
        now = reset + 20 * HOUR
        write_log([usage_row(reset - 3 * HOUR, "acct-A", 100),
                   usage_row(reset - 2 * HOUR, "acct-A", 200),
                   usage_row(reset - HOUR, "acct-A", 300)])
        accounts = [account("acct-A")]
        cache_env(min_bytes=1, min_seconds=0)
        try:
            first = payload(accounts, now=now)
            path = pathlib.Path(remaining_checkpoint_path())
            raw = json.loads(path.read_text(encoding="utf-8"))
            entry = raw["entries"][0]
            pair = entry["usage"][next(iter(entry["usage"]))]
            pair["at"] = list(reversed(pair["at"]))
            pair["cum"] = list(reversed(pair["cum"]))
            path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

            restart_fold()
            reload_checkpoint_cache()
            again = payload(accounts, now=now)
            self.assertEqual(again, first, "坏 checkpoint 必须被拒，冷折结果不变")
        finally:
            cache_env()

    def test_checkpoint_rejects_an_inconsistent_base(self):
        # base 超过 cum[0] 只可能来自旧版压缩的重复计入（cum 含被裁前缀，
        # base 是其中「保留区之前」那一段，不可能更大）：这种条目必须整份
        # 拒绝、退回冷折——带着它继续算，左端落在保留区之前的区间求和会
        # 出负数（真机面板的「上段」曾显示 -1293.5M）。
        reset, detail = reset_epoch("2026-10-09 08:00:00")
        now = reset + 20 * HOUR
        write_log([usage_row(reset - 3 * HOUR, "acct-A", 100),
                   usage_row(reset - 2 * HOUR, "acct-A", 200),
                   usage_row(reset - HOUR, "acct-A", 300)])
        accounts = [account("acct-A")]
        cache_env(min_bytes=1, min_seconds=0)
        try:
            first = payload(accounts, now=now)
            path = pathlib.Path(remaining_checkpoint_path())
            raw = json.loads(path.read_text(encoding="utf-8"))
            entry = raw["entries"][0]
            pair = entry["usage"][next(iter(entry["usage"]))]
            pair["base"] = pair["cum"][-1] + 1
            path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

            restart_fold()
            reload_checkpoint_cache()
            again = payload(accounts, now=now)
            self.assertEqual(again, first, "坏 base 必须被拒，冷折结果不变")
        finally:
            cache_env()

    def test_daily_guards_share_one_fold(self):
        # 三个护栏共用一次设置读取与一次折叠（open_upstream 每请求都调）。
        # 桩必须照抄真实池的返回形状：AccountPool.apply_daily_* 返回的是各自
        # 的 limits 字典，不是计数——返回 1 会让「拿返回值做算术」的错误溜过去。
        calls = []

        class _Pool(object):
            def apply_daily_token_limit(self, limits, usage):
                calls.append(("token", dict(limits), usage)); return dict(limits)

            def apply_daily_credit_limit(self, limits, credits, free_models):
                calls.append(("credit", dict(limits), credits)); return dict(limits)

            def apply_model_daily_token_limit(self, limits, per_model):
                calls.append(("model", dict(limits), per_model)); return dict(limits)

        stats_calls = {"n": 0}
        real_stats = U.daily_usage_stats

        def counting_stats(ttl=None):
            stats_calls["n"] += 1
            return real_stats(ttl=ttl)

        old_pool = P.POOL
        old_dir = P.ACCOUNTS_DIR
        P.POOL = _Pool()
        # ACCOUNTS_DIR 是脚本自身目录、不走环境变量，这里显式指到临时目录，
        # 否则护栏读的是仓库里的 accounts/。
        P.ACCOUNTS_DIR = ACCOUNTS_DIR
        U.daily_usage_stats = counting_stats
        try:
            # 没配限额：一次折叠都不该发生，也不该碰池子。
            for key in ("daily_token_limit", "daily_credit_limit",
                        "model_daily_token_limit"):
                wb_settings.set_limit(ACCOUNTS_DIR, key, "global", 0)
            self.assertEqual(U.apply_daily_guards(), 0)
            self.assertEqual(stats_calls["n"], 0)
            self.assertEqual(calls, [])

            wb_settings.set_limit(ACCOUNTS_DIR, "daily_token_limit", "global", 1000)
            wb_settings.set_limit(ACCOUNTS_DIR, "model_daily_token_limit", "global", 50)
            write_log([usage_row(time.time() - HOUR, "acct-A", 700)])
            self.assertEqual(U.apply_daily_guards(), 2)
            self.assertEqual(stats_calls["n"], 1, "三份限额共用一次折叠")
            self.assertEqual([c[0] for c in calls], ["token", "model"])
            # 折叠失败（读不到日志）时三份都拿到 None，绝不据此停号。
            calls[:] = []
            P.USAGE_LOG, saved = P.USAGE_LOG + ".gone", P.USAGE_LOG
            try:
                # refresh=True 绕开计数器的 TTL，确保这次真的去读（读不到）。
                self.assertEqual(U.apply_daily_guards(refresh=True), 2)
            finally:
                P.USAGE_LOG = saved
            self.assertTrue(all(c[2] is None for c in calls),
                            "读不到计数时必须传 None（未知 ≠ 0）")

            # 真池再走一遍：桩的返回形状写错（比如返回 1）时这条会红。
            real_pool = wb_accounts.AccountPool(ACCOUNTS_DIR, log=None)
            real_pool.accounts = [account("acct-A")]
            P.POOL = real_pool
            self.assertEqual(U.apply_daily_guards(refresh=True), 2)
            self.assertEqual(real_pool.accounts[0].daily_token_limit, 1000)

            # 一次读取多键：逐值与单键入口一致，且只读一次限额表。
            reads = {"n": 0}
            real_limits_data = wb_settings.limits_data

            def counting_limits_data(directory):
                reads["n"] += 1
                return real_limits_data(directory)

            wb_settings.limits_data = counting_limits_data
            try:
                many = wb_settings.limit_values_many(
                    ACCOUNTS_DIR, ("daily_token_limit", "daily_credit_limit",
                                   "model_daily_token_limit"))
                self.assertEqual(reads["n"], 1, "三份限额共用一次读取")
            finally:
                wb_settings.limits_data = real_limits_data
            for key in ("daily_token_limit", "daily_credit_limit",
                        "model_daily_token_limit"):
                self.assertEqual(many[key],
                                 wb_settings.limit_values(ACCOUNTS_DIR, key))
        finally:
            P.POOL = old_pool
            P.ACCOUNTS_DIR = old_dir
            U.daily_usage_stats = real_stats
            for key in ("daily_token_limit", "daily_credit_limit",
                        "model_daily_token_limit"):
                try:
                    wb_settings.set_limit(ACCOUNTS_DIR, key, "global", 0)
                except Exception:
                    pass

    def test_live_cap_event_is_persisted_and_survives_restart(self):
        # The request path records the cap the moment it is classified: the
        # rows that led to it are in the log but may not be scanned yet.
        reset = time.time() + 30 * 60          # the window is spent, resets later
        write_log([
            usage_row(reset - 25 * HOUR, "acct-A", 111111),   # outside: ignored
            usage_row(reset - 3 * HOUR, "acct-A", 4000),
            usage_row(reset - HOUR, "acct-A", 1500),
        ])
        U.note_limit_event(account("acct-A"), MODEL, reset)

        data = payload([account("acct-A")], now=reset - 10 * 60)
        budget = data["budgets"][0]
        self.assertEqual(budget["avg"], 5500)   # 4000 + 1500, live sample
        self.assertEqual(budget["n"], 1)
        row = find_row(data, "acct-A")
        self.assertTrue(row["cooling"])
        self.assertEqual(row["remaining"], 0)
        self.assertEqual(row["reset_at"], reset)

        # A restart re-reads the journal: the event (and its sample) survives.
        restart_fold()
        data = payload([account("acct-A")], now=reset - 10 * 60)
        self.assertEqual(data["budgets"][0]["avg"], 5500)
        self.assertTrue(find_row(data, "acct-A")["cooling"])

    def test_live_event_dedupes_against_the_log_row(self):
        reset = int(time.time()) + 30 * 60
        detail = cap_detail(reset)
        write_log([
            usage_row(reset - 2 * HOUR, "acct-A", 900),
            # The 429 row the log keeps for this same cap (the last account of
            # the retry batch keeps its attribution).
            cap_row(reset - HOUR, "acct-A", detail),
        ])
        U.note_limit_event(account("acct-A"), MODEL, reset)
        data = payload([account("acct-A")], now=reset - 10 * 60)
        # One cap, one sample - the live event and the log row are the same
        # (account, model, reset) and must not double-count.
        self.assertEqual(len(data["budgets"]), 1)
        self.assertEqual(data["budgets"][0]["n"], 1)

    def test_the_limit_event_journal_follows_the_usage_dir(self):
        """--usage-dir 必须把事件日志、按天归档与 recent 索引一起搬走。

        LIMIT_EVENTS_FILE / ESTIMATE_DAILY_FILE / RECENT_INDEX_FILE 都是 import
        时按默认目录算出来的常量；漏掉它们，真机上（目录来自命令行参数而不是
        环境变量）事件日志会一直写 ENOENT，预算样本就只剩日志里带归属的那几条
        429 行（2026-10-10 真机实测），跨天归档则会写错目录，recent 的行偏移
        索引同样会写错目录（换目录后每次冷启动都要重扫一遍日志）。
        """
        import wb_activity
        directory = tempfile.mkdtemp(prefix="wb-usage-dir-")
        old = (P.USAGE_DIR, P.USAGE_LOG, P.LIMIT_EVENTS_FILE,
               P.ESTIMATE_DAILY_FILE, P.RECENT_INDEX_FILE)
        old_activity = wb_activity.data_dir()
        try:
            P._apply_cli_overrides(types.SimpleNamespace(
                lan=False, host="127.0.0.1", user_agent=None, usage_dir=directory))
            self.assertEqual(P.USAGE_LOG, os.path.join(directory, "usage.jsonl"))
            self.assertEqual(P.LIMIT_EVENTS_FILE,
                             os.path.join(directory, "limit-events.jsonl"))
            self.assertEqual(P.ESTIMATE_DAILY_FILE,
                             os.path.join(directory, "estimate-daily.json"))
            self.assertEqual(P.RECENT_INDEX_FILE,
                             os.path.join(directory, "usage-recent-index.json"))
            # 归档也真的写进新目录（而不是旧目录 / ENOENT）。
            U._estimate_daily_sync(
                {"2026-10-14": {"budgets": {}, "segments": {}}},
                local_epoch(2026, 10, 15, 12, 0, 0))
            self.assertTrue(os.path.exists(
                os.path.join(directory, "estimate-daily.json")))
        finally:
            P.USAGE_DIR, P.USAGE_LOG, P.LIMIT_EVENTS_FILE, \
                P.ESTIMATE_DAILY_FILE, P.RECENT_INDEX_FILE = old
            wb_activity.set_data_dir(old_activity)
            # 缓存里还留着临时目录的戳，还原后立刻失效掉。
            U._estimate_daily_cache["entry"] = (
                (P.ESTIMATE_DAILY_FILE, None, None), {})

    def test_live_event_for_a_different_reset_is_a_second_sample(self):
        # Two caps on the same pair a day apart (the 24h window in between),
        # with the clock frozen so each hook call sees the usage of its own
        # window only. 时间锚在本地日上：跨天分桶看的是撞线当天的本地日期。
        real_time = time.time
        day1 = local_epoch(2026, 10, 10, 9, 0, 0)        # 昨天
        day2 = local_epoch(2026, 10, 11, 10, 0, 0)       # 今天
        clock = {"now": day1}
        time.time = lambda: clock["now"]
        try:
            reset1 = day1 + 30 * 60
            write_log([usage_row(day1 - 2 * HOUR, "acct-A", 700)])
            U.note_limit_event(account("acct-A"), MODEL, reset1)

            clock["now"] = day2
            reset2 = day2 + 30 * 60
            write_log([
                usage_row(day1 - 2 * HOUR, "acct-A", 700),      # 窗口 1
                usage_row(day2 - 6 * HOUR, "acct-A", 1300),     # 窗口 2
            ])
            U.note_limit_event(account("acct-A"), MODEL, reset2)

            data = payload([account("acct-A")], now=clock["now"])
        finally:
            time.time = real_time
        budget = data["budgets"][0]
        # 两天各一个样本（同一个账号）：今天 1300 权重 6、昨天 700 权重 3。
        self.assertEqual(budget["avg"], 1100)            # (6×1300 + 3×700)/9
        self.assertEqual(budget["min"], 700)
        self.assertEqual(budget["max"], 1300)
        self.assertEqual(budget["n"], 2)                 # 各天账号数之和
        self.assertEqual([(d["day"], d["w"], d["avg"]) for d in budget["days"]],
                         [("2026-10-11", 6, 1300), ("2026-10-10", 3, 700)])
        # The window in force is the later reset's, and it is still cooling.
        row = find_row(data, "acct-A")
        self.assertEqual(row["reset_at"], reset2)
        self.assertTrue(row["cooling"])
        self.assertEqual(row["used"], 1300)

    def test_cap_truncates_the_spent_window(self):
        # 撞线后窗口的记账在撞线处截断：撞线之后、恢复之前的行（上游本该拒收，
        # 但日志里可能有残留）不计入这个窗口的已用。
        reset, detail = reset_epoch("2026-10-10 16:00:00")
        write_log([
            usage_row(reset - 20 * HOUR, "acct-A", 4000),
            cap_row(reset - HOUR, "acct-A", detail),
            usage_row(reset - 30 * 60, "acct-A", 777),
        ])
        data = payload([account("acct-A")], now=reset - 10 * 60)
        row = find_row(data, "acct-A")
        self.assertTrue(row["cooling"])
        self.assertEqual(row["used"], 4000)
        self.assertEqual(row["remaining"], 0)
        segments = row["segments"]
        self.assertEqual(len(segments), 1)
        self.assertTrue(segments[0]["capped"])
        self.assertTrue(segments[0]["current"])
        self.assertEqual(segments[0]["used"], 4000)
        self.assertEqual(segments[0]["end"], reset - HOUR)   # 截断点 = 撞线时刻

    def test_recovered_pair_counts_only_the_new_window(self):
        # 恢复时刻之后：新窗口从恢复时刻重新记账，旧窗口（在撞线处截断）的
        # 用量不计进新窗口的已用；两段都在载荷里给出（分段相加）。
        reset, detail = reset_epoch("2026-10-10 08:00:00")
        write_log([
            usage_row(reset - 20 * HOUR, "acct-A", 2000),   # 旧窗口
            usage_row(reset - 2 * HOUR, "acct-A", 3000),    # 旧窗口
            cap_row(reset - HOUR, "acct-A", detail),
            usage_row(reset + 30 * 60, "acct-A", 700),      # 新窗口
            usage_row(reset + 45 * 60, "acct-A", 500),      # 新窗口
        ])
        data = payload([account("acct-A")], now=reset + HOUR)
        row = find_row(data, "acct-A")
        self.assertFalse(row["cooling"])
        self.assertFalse(row["estimated"])
        self.assertEqual(row["window_start"], reset)
        self.assertEqual(row["used"], 1200)
        self.assertEqual(row["budget"], 5000)
        self.assertEqual(row["remaining"], 3800)
        segments = row["segments"]
        self.assertEqual(len(segments), 2)
        self.assertTrue(segments[0]["capped"])
        self.assertFalse(segments[0]["current"])
        self.assertEqual(segments[0]["start"], reset - 24 * HOUR)
        self.assertEqual(segments[0]["end"], reset - HOUR)
        self.assertEqual(segments[0]["used"], 5000)
        self.assertTrue(segments[1]["current"])
        self.assertFalse(segments[1]["capped"])
        self.assertEqual(segments[1]["start"], reset)
        self.assertEqual(segments[1]["used"], 1200)

    def test_chained_caps_sample_excludes_the_previous_window(self):
        # 链式撞线（两个重置时刻相隔不到 24h）：第二个样本从上一个重置时刻
        # 起算，旧窗口的用量不进新窗口的样本。
        r1 = local_epoch(2026, 10, 10, 10, 0, 0)
        r2 = local_epoch(2026, 10, 11, 6, 0, 0)         # 20h 之后
        write_log([
            usage_row(r1 - 2 * HOUR, "acct-A", 1000),   # 窗口 1
            cap_row(r1 - HOUR, "acct-A", cap_detail(r1)),
            usage_row(r1 + 2 * HOUR, "acct-A", 500),    # 窗口 2
            cap_row(r2 - HOUR, "acct-A", cap_detail(r2)),
        ])
        data = payload([account("acct-A")], now=r2 + HOUR)
        budget = data["budgets"][0]
        # 样本 1 = 1000；样本 2 从 r1 起算 = 500（不含窗口 1 的 1000）。
        # 不带分段截断时样本 2 会是 1500（把窗口 1 的 1000 也算进去）。
        # 两个样本落在两天（r1 的撞线在昨天、r2 的在今天）：6/3 加权
        # (6×500 + 3×1000)/9 = 667。
        self.assertEqual(budget["avg"], 667)
        self.assertEqual(budget["min"], 500)             # 各天账号均值的极值
        self.assertEqual(budget["max"], 1000)
        self.assertEqual(budget["n"], 2)                 # 两天各一个账号
        self.assertEqual([(d["day"], d["w"], d["avg"]) for d in budget["days"]],
                         [("2026-10-11", 6, 500), ("2026-10-10", 3, 1000)])
        row = find_row(data, "acct-A")
        self.assertEqual(row["used"], 0)
        self.assertEqual(row["remaining"], 667)
        segments = row["segments"]
        self.assertEqual(segments[0]["start"], r1)
        self.assertEqual(segments[0]["end"], r2 - HOUR)
        self.assertEqual(segments[0]["used"], 500)

    def test_disk_cap_record_anchors_without_an_event(self):
        # 本次进程没撞过线（事件日志里没有），但凭证档里有 model_caps 记录：
        # 冷却、窗口起点、截断点全部照它算。
        reset = time.time() + 30 * 60
        write_log([
            usage_row(reset - 25 * HOUR, "acct-A", 111111),   # 窗口外
            usage_row(reset - 3 * HOUR, "acct-A", 4000),
            usage_row(reset - HOUR, "acct-A", 1500),
        ])
        acct = account("acct-A")
        acct.model_caps[MODEL] = {"at": reset - 20 * 60, "reset": reset}
        data = payload([acct], now=reset - 10 * 60)
        row = find_row(data, "acct-A")
        self.assertTrue(row["cooling"])
        self.assertEqual(row["remaining"], 0)
        self.assertEqual(row["reset_at"], reset)
        self.assertEqual(row["used"], 5500)              # 截断在 at 之前
        self.assertEqual(row["segments"][0]["end"], reset - 20 * 60)
        # model_caps 不带样本：没有预算，但冷却与分段照常。
        self.assertEqual(data["budgets"], [])
        self.assertIsNone(row["budget"])

        # 恢复之后：新窗口自磁盘上的恢复时刻起算，旧窗口用量不计入。
        with io.open(P.USAGE_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(usage_row(reset + 10 * 60, "acct-A", 900),
                                ensure_ascii=False) + "\n")
        data = payload([acct], now=reset + 30 * 60)
        row = find_row(data, "acct-A")
        self.assertFalse(row["cooling"])
        self.assertFalse(row["estimated"])
        self.assertEqual(row["window_start"], reset)
        self.assertEqual(row["used"], 900)
        segments = row["segments"]
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["used"], 5500)
        self.assertEqual(segments[1]["start"], reset)
        self.assertEqual(segments[1]["used"], 900)

    def test_cooling_uses_the_earliest_upcoming_reset(self):
        # 链上还有更远的恢复时刻时，冷却到最近的那个（第一个恢复时刻额度就
        # 回来了），已用在第一次撞线处截断。恢复时刻直接取 epoch 并反构 6004
        # 文本（cap_detail）：reset_epoch(strftime(localtime(…))) 的墙钟往返
        # 只在 UTC+8 主机上才是同一个时刻，UTC 上会整整偏 8 小时。
        now = time.time()
        r1 = int(now) + HOUR
        r2 = int(now) + 30 * HOUR
        write_log([
            usage_row(now - 3 * HOUR, "acct-A", 1000),
            cap_row(now - 2 * HOUR, "acct-A", cap_detail(r1)),
            cap_row(now - HOUR, "acct-A", cap_detail(r2)),
        ])
        data = payload([account("acct-A")], now=now)
        row = find_row(data, "acct-A")
        self.assertTrue(row["cooling"])
        self.assertEqual(row["reset_at"], r1)
        self.assertEqual(row["remaining"], 0)
        self.assertEqual(row["used"], 1000)
        self.assertEqual(row["segments"][0]["end"], now - 2 * HOUR)

    def test_model_total_sums_available_groups_only(self):
        # 按模型总剩余 = 可用组的剩余之和；冷却的组记 0 且不拉低别的组。
        reset, detail = reset_epoch("2026-10-10 16:00:00")
        write_log([
            usage_row(reset - 20 * HOUR, "acct-A", 3000),
            cap_row(reset - HOUR, "acct-A", detail),
            usage_row(reset - 2 * HOUR, "acct-B", 500),
        ])
        data = payload([account("acct-A"), account("acct-B", nickname="b")],
                       now=reset - 10 * 60)
        models = {m["model"]: m for m in data["models"]}
        total = models[MODEL]
        self.assertEqual(total["realm"], "intl")
        self.assertEqual(total["pairs"], 2)
        self.assertEqual(total["cooling"], 1)
        self.assertEqual(total["available"], 1)
        # 预算 3000（A 的样本）：B 用 500 → 剩 2500；A 冷却记 0。
        self.assertEqual(total["budget"], 3000)
        self.assertEqual(total["total_remaining"], 2500)

    def test_stale_models_are_filtered_out(self):
        # 昨天或今天没消耗过的模型不参与剩余用量：两天前的用量行不算「在用」，
        # 整行从载荷里去掉；昨天/今天用过的照常。
        now = time.time()
        write_log([
            usage_row(now - 50 * HOUR, "acct-A", 600, model="glm-5.3"),
            usage_row(now - HOUR, "acct-A", 42),
        ])
        data = payload([account("acct-A")], now=now)
        row = find_row(data, "acct-A")
        self.assertIsNotNone(row)
        self.assertEqual(row["used"], 42)
        self.assertIsNone(find_row(data, "acct-A", model="glm-5.3"))
        self.assertEqual([m["model"] for m in data["models"]], [MODEL])
        self.assertTrue(data["activity_floor"] <= now)

    def test_inactive_budget_samples_stay_visible(self):
        # 有预算样本但近两日没用的模型：不参与剩余计算（行被过滤、不进总剩余），
        # 但预算样本仍留在载荷里并标 active=False——预算要撞线才学得到，不能
        # 因为暂时没用就从面板上消失（否则每次跨零点面板都抖）。
        # 新口径：预算按天持久化。样本在前天（前天结束时归档），今天构建时
        # 从归档里读出来（3 天窗口的最后一档，权重 1）。
        reset = local_epoch(2026, 10, 13, 8, 0, 0)       # 前天 08:00（本地）
        real_time = time.time
        clock = {"now": reset - HOUR}                    # 撞线时刻：前天 07:00
        time.time = lambda: clock["now"]
        try:
            write_log([
                usage_row(reset - 2 * HOUR, "acct-A", 900, model="glm-5.3"),
            ])
            U.note_limit_event(account("acct-A"), "glm-5.3", reset)
        finally:
            time.time = real_time
        # 前天结束时归档一次（进程在昨天构建过），今天再从归档里读。
        payload([account("acct-A")], now=local_epoch(2026, 10, 14, 12, 0, 0))
        data = payload([account("acct-A")],
                       now=local_epoch(2026, 10, 15, 12, 0, 0))
        budget = data["budgets"][0]
        self.assertEqual(budget["model"], "glm-5.3")
        self.assertEqual(budget["avg"], 900)
        self.assertFalse(budget["active"])
        self.assertEqual([(d["day"], d["w"], d["avg"]) for d in budget["days"]],
                         [("2026-10-13", 1, 900)])
        self.assertIsNone(find_row(data, "acct-A", model="glm-5.3"))
        self.assertEqual(data["models"], [])

    def test_daily_file_keeps_three_days_and_two_days_of_segments(self):
        """保留策略：只留最近 3 天（估算窗口）；segments 只留最近 2 天。

        更早的天整条删掉（估算永远读不到）；前天的 segments 字段删掉、预算
        保留（用户要求分段「仅保留到第二天」，多留一天防跨天边界）。
        """
        now = local_epoch(2026, 10, 15, 12, 0, 0)        # 今天
        record = {"budgets": {"intl\x1f" + MODEL: {"avg": 1, "min": 1,
                                                   "max": 1, "n": 1}},
                  "segments": {"acct-A\x1f" + MODEL: {"pre": 1, "post": 0}}}
        days = {day: json.loads(json.dumps(record))
                for day in ("2026-10-11", "2026-10-12", "2026-10-13", "2026-10-14")}
        with io.open(P.ESTIMATE_DAILY_FILE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"schema": 1, "days": days}, ensure_ascii=False))

        payload([account("acct-A")], now=now)

        with io.open(P.ESTIMATE_DAILY_FILE, encoding="utf-8") as fh:
            stored = json.load(fh)
        # 只留最近 3 天：4 天前（10-11）与 3 天前（10-12）都清掉。
        self.assertEqual(sorted(stored["days"]),
                         ["2026-10-13", "2026-10-14"])
        # 前天（10-13）只留预算，昨天（10-14）两样都在。
        self.assertNotIn("segments", stored["days"]["2026-10-13"])
        self.assertIn("segments", stored["days"]["2026-10-14"])
        self.assertEqual(stored["days"]["2026-10-13"]["budgets"],
                         {"intl\x1f" + MODEL: {"avg": 1, "min": 1,
                                               "max": 1, "n": 1}})

    def test_a_corrupt_daily_file_is_ignored(self):
        """坏文件（手改 / 半写坏）按空处理：估算退回今天现算，绝不报错。"""
        now = local_epoch(2026, 10, 15, 12, 0, 0)
        with io.open(P.ESTIMATE_DAILY_FILE, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        data = payload([account("acct-A")], now=now)
        self.assertEqual(data["budgets"], [])

        # 结构对但值不合法的那天跳过（avg 是字符串 / n 不是正整数）。
        days = {"2026-10-14": {"budgets": {"intl\x1f" + MODEL: {
                    "avg": "x", "min": 1, "max": 1, "n": 1}}},
                "2026-10-13": {"budgets": {"intl\x1f" + MODEL: {
                    "avg": 5, "min": 5, "max": 5, "n": 0}}}}
        with io.open(P.ESTIMATE_DAILY_FILE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"schema": 1, "days": days}, ensure_ascii=False))
        data = payload([account("acct-A")], now=now)
        self.assertEqual(data["budgets"], [])

    def test_a_restart_archives_the_day_before_on_the_first_build(self):
        """重启后 state["day"] 为空：第一次构建按「刚过去的那天」试归档一次。

        幂等（数据不在就跳过），这次数据在事件日志与用量缓冲里，归档成功；
        本次构建的估算直接吃它。
        """
        reset = local_epoch(2026, 10, 14, 9, 0, 0)       # 昨天 09:00（本地）
        real_time = time.time
        clock = {"now": reset - HOUR}                    # 撞线时刻：昨天 08:00
        time.time = lambda: clock["now"]
        try:
            write_log([
                usage_row(reset - 3 * HOUR, "acct-A", 1200),
                usage_row(reset - 2 * HOUR, "acct-B", 800),
            ])
            U.note_limit_event(account("acct-A"), MODEL, reset)
        finally:
            time.time = real_time
        # 进程重启：内存折叠清空、checkpoint 缓存重读；第一次构建才发现跨天。
        restart_fold()
        reload_checkpoint_cache()
        data = payload([account("acct-A"), account("acct-B")],
                       now=local_epoch(2026, 10, 15, 10, 0, 0))

        with io.open(P.ESTIMATE_DAILY_FILE, encoding="utf-8") as fh:
            stored = json.load(fh)
        day = stored["days"]["2026-10-14"]
        self.assertEqual(day["budgets"]["intl\x1f" + MODEL],
                         {"avg": 1200, "min": 1200, "max": 1200, "n": 1})
        self.assertEqual(day["segments"]["acct-A\x1f" + MODEL],
                         {"pre": 1200, "post": 0})
        self.assertEqual(day["segments"]["acct-B\x1f" + MODEL],
                         {"pre": 800, "post": 0})
        self.assertEqual(data["budgets"][0]["avg"], 1200)


class RemainingPrioritySettingTests(unittest.TestCase):
    """优先调度开关：默认关，只有真正的布尔 true 才算开。"""

    def test_default_is_off(self):
        with tempfile.TemporaryDirectory(prefix="remaining-priority-setting-") as directory:
            self.assertFalse(wb_settings.remaining_priority_enabled(directory))

    def test_round_trip(self):
        with tempfile.TemporaryDirectory(prefix="remaining-priority-setting-") as directory:
            self.assertTrue(wb_settings.set_remaining_priority_enabled(directory, True))
            self.assertTrue(wb_settings.remaining_priority_enabled(directory))
            self.assertFalse(wb_settings.set_remaining_priority_enabled(directory, False))
            self.assertFalse(wb_settings.remaining_priority_enabled(directory))

    def test_a_hand_edited_string_does_not_read_as_enabled(self):
        with tempfile.TemporaryDirectory(prefix="remaining-priority-setting-") as directory:
            data = wb_settings.load(directory)
            data[wb_settings.REMAINING_PRIORITY_ENABLED_KEY] = "true"
            wb_settings.save(directory, data)
            self.assertFalse(wb_settings.remaining_priority_enabled(directory))


class RemainingPriorityTests(unittest.TestCase):
    """优先调度：估计剩余越少的 (账号, 模型) 权重越高、先被交出。

    权重表由 wb_proxy 从剩余估算载荷算出（_remaining_schedule_weight /
    remaining_schedule_weights），池只消费（apply_remaining_weights 推表、
    _pick_remaining_first 加权轮询）。这里同时钉住：分档边界、无预算样本 /
    剩余为 0 不加权、开关关（表为空）时与纯轮询逐字节一致、冷却或被排除的
    加权组合不参与、权重只在同一模型内生效、临期积分那条仍然优先。
    """

    def setUp(self):
        clear_events()
        restart_fold()

    def _pool(self, accounts, weights=None):
        directory = tempfile.mkdtemp(prefix="remaining-priority-")
        pool = wb_accounts.AccountPool(directory, log=lambda _m: None)
        pool.accounts = accounts
        if weights is not None:
            pool.apply_remaining_weights(weights)
        return pool

    def test_weight_bands_and_boundaries(self):
        weight = U._remaining_schedule_weight
        # 剩不到 30% 开始加权，5% 以内最重：边界落在分档内的一侧。
        self.assertEqual(weight(50, 1000), 4)
        self.assertEqual(weight(51, 1000), 3)
        self.assertEqual(weight(150, 1000), 3)
        self.assertEqual(weight(151, 1000), 2)
        self.assertEqual(weight(300, 1000), 2)
        self.assertEqual(weight(301, 1000), 1)
        self.assertEqual(weight(1000, 1000), 1)

    def test_unknown_or_empty_estimates_are_not_weighted(self):
        weight = U._remaining_schedule_weight
        # 没有预算样本：没有可比较的尺度，不猜。
        self.assertEqual(weight(None, 1000), 1)
        self.assertEqual(weight(100, None), 1)
        self.assertEqual(weight(100, 0), 1)
        # 剩余为 0：没有「先用掉」的意义（冷却中的本来也不可用）。
        self.assertEqual(weight(0, 1000), 1)
        self.assertEqual(weight(-5, 1000), 1)
        # 看不懂的数字不参与（日志是自由文本）。
        self.assertEqual(weight("junk", 1000), 1)
        self.assertEqual(weight(100, "junk"), 1)

    def test_weights_come_from_the_payload(self):
        # 撞线样本反推预算 1000，之后又用了 970 → 剩 30/1000 = 3% → 权重 4；
        # 没怎么用的账号不加权。载荷与权重表都吃真实时钟，这里把它冻住。
        real_time = time.time
        clock = {"now": real_time()}
        time.time = lambda: clock["now"]
        try:
            reset, detail = reset_epoch("2026-10-09 08:00:00")
            clock["now"] = reset + 2 * HOUR
            write_log([
                usage_row(reset - 2 * HOUR, "acct-A", 1000),
                cap_row(reset - HOUR, "acct-A", detail),
                usage_row(reset + 30 * 60, "acct-A", 970),
            ])

            class _Pool(object):
                accounts = [account("acct-A"), account("acct-B")]

            old_pool = P.POOL
            P.POOL = _Pool()
            try:
                weights = U.remaining_schedule_weights()
            finally:
                P.POOL = old_pool
        finally:
            time.time = real_time
        self.assertEqual(weights, {("acct-A", MODEL): 4})

    def test_no_pool_means_no_weights(self):
        old_pool = P.POOL
        P.POOL = None
        try:
            self.assertEqual(U.remaining_schedule_weights(), {})
        finally:
            P.POOL = old_pool

    def test_pool_serves_the_weighted_pair_first(self):
        pool = self._pool([account("acct-A"), account("acct-B")],
                          weights={("acct-A", MODEL): 4})
        picked = {pool.pick(realm="intl", model=MODEL).uid for _ in range(6)}
        self.assertEqual(picked, {"acct-A"})

    def test_two_weighted_pairs_share_by_weight(self):
        pool = self._pool([account("acct-A"), account("acct-B")],
                          weights={("acct-A", MODEL): 4, ("acct-B", MODEL): 2})
        picks = [pool.pick(realm="intl", model=MODEL).uid for _ in range(60)]
        # 4:2 的平滑加权轮询是 2:1 的份额，且谁都不独占。
        self.assertEqual((picks.count("acct-A"), picks.count("acct-B")), (40, 20))

    def test_switch_off_keeps_the_plain_round_robin(self):
        pool = self._pool([account("acct-A"), account("acct-B")])
        picked = [pool.pick(realm="intl", model=MODEL).uid for _ in range(4)]
        self.assertEqual(picked, ["acct-A", "acct-B", "acct-A", "acct-B"])

    def test_clearing_the_table_restores_the_plain_rotation(self):
        pool = self._pool([account("acct-A"), account("acct-B")],
                          weights={("acct-A", MODEL): 4})
        pool.pick(realm="intl", model=MODEL)
        self.assertEqual(pool.apply_remaining_weights(None), {})
        self.assertEqual(pool._remaining_weights, {})
        picked = [pool.pick(realm="intl", model=MODEL).uid for _ in range(2)]
        self.assertEqual(picked, ["acct-A", "acct-B"])

    def test_the_table_only_keeps_boosted_pairs(self):
        pool = self._pool([account("acct-A")])
        table = pool.apply_remaining_weights({
            ("acct-A", MODEL): 1,          # 权重 1 = 没有偏好
            ("acct-B", MODEL): 0,
            ("acct-C", MODEL): "junk",
            ("acct-D", MODEL): 3,
        })
        self.assertEqual(table, {("acct-D", MODEL): 3})

    def test_cooling_weighted_pair_falls_back_to_the_pool(self):
        hot = account("acct-A")
        hot.model_cooldowns[MODEL] = time.time() + 600
        pool = self._pool([hot, account("acct-B")],
                          weights={("acct-A", MODEL): 4})
        picked = {pool.pick(realm="intl", model=MODEL).uid for _ in range(4)}
        self.assertEqual(picked, {"acct-B"})

    def test_excluded_weighted_pair_falls_through(self):
        pool = self._pool([account("acct-A"), account("acct-B")],
                          weights={("acct-A", MODEL): 4})
        self.assertEqual(
            pool.pick(realm="intl", model=MODEL, exclude={"acct-A"}).uid,
            "acct-B")

    def test_weights_are_scoped_to_their_model(self):
        pool = self._pool([account("acct-A"), account("acct-B")],
                          weights={("acct-A", MODEL): 4})
        picked = [pool.pick(realm="intl", model="hy4-preview-f").uid
                  for _ in range(4)]
        self.assertEqual(picked, ["acct-A", "acct-B", "acct-A", "acct-B"])

    def test_rotation_state_is_bounded_to_the_live_pairs(self):
        pool = self._pool([account("acct-A"), account("acct-B")],
                          weights={("acct-A", MODEL): 4, ("acct-B", MODEL): 2})
        for _ in range(6):
            pool.pick(realm="intl", model=MODEL)
        self.assertTrue(set(pool._remaining_pick_state)
                        <= {("acct-A", MODEL), ("acct-B", MODEL)})
        pool.accounts = pool.accounts[:1]
        pool.pick(realm="intl", model=MODEL)
        self.assertEqual(set(pool._remaining_pick_state), {("acct-A", MODEL)})

    def test_a_pick_for_one_model_keeps_another_models_rotation(self):
        # 换一个模型分派不能把这条偏好的轮次清零：轮次一清零权重就退化成
        # 「总是轮到的第一个」。两个模型都加权、交替分派，各自都保持 2:1。
        other = "hy4-preview-f"
        pool = self._pool([account("acct-A"), account("acct-B")],
                          weights={("acct-A", MODEL): 4, ("acct-B", MODEL): 2,
                                   ("acct-A", other): 4, ("acct-B", other): 2})
        picks = []
        for _ in range(30):
            picks.append(pool.pick(realm="intl", model=MODEL).uid)
            picks.append(pool.pick(realm="intl", model=other).uid)
        first, second = picks[0::2], picks[1::2]
        self.assertEqual((first.count("acct-A"), first.count("acct-B")), (20, 10))
        self.assertEqual((second.count("acct-A"), second.count("acct-B")), (20, 10))

    def test_the_expiring_window_still_wins_over_the_remaining_weights(self):
        # 层序：临期积分那条先跑，它在窗口里有人可交时剩余权重不参与。
        urgent = account("acct-A")
        urgent.in_expiring_window = lambda: True
        pool = self._pool([urgent, account("acct-B")],
                          weights={("acct-B", MODEL): 4})
        picked = {pool.pick(realm="intl", model=MODEL).uid for _ in range(4)}
        self.assertEqual(picked, {"acct-A"})

    def test_many_threads_pick_without_errors_and_keep_the_share(self):
        accounts = [account("acct-%02d" % i) for i in range(6)]
        pool = self._pool(accounts, weights={("acct-00", MODEL): 4,
                                             ("acct-01", MODEL): 2})
        results = []
        errors = []
        guard = threading.Lock()
        barrier = threading.Barrier(6)

        def worker():
            local = []
            try:
                barrier.wait(timeout=10)
                for _ in range(50):
                    local.append(pool.pick(realm="intl", model=MODEL).uid)
            except Exception as exc:
                with guard:
                    errors.append(exc)
            with guard:
                results.extend(local)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 6 * 50)
        # 权重 4:2 在并发下也按 2:1 出账（每次分派在池锁里完成，总数是确定的）；
        # 两个加权组合都可用时其余账号不会被轮到，它们只在加权组合不可用时兜底。
        self.assertEqual(results.count("acct-00"), 200)
        self.assertEqual(results.count("acct-01"), 100)
        self.assertEqual(results.count("acct-02"), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
