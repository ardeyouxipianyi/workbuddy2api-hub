#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""429 必须给出「什么时候恢复」，不能只给「还要等多久」。

`Retry-After` 是一个相对秒数，它只对**读到它的那一刻**成立：多 worker 的
客户端各自记一份、各自漂移；任务被转交给别的进程后，那个秒数直接失效；
日志里翻到一条 429 也看不出它是什么时候写的。所以 429 还要带一个绝对
时刻——上游 6004 的正文里本来就写了墙钟，其余 429 由网关按自己算出的
冷却窗口换算。

本套件钉三件事：

  1. 头与正文：拿到时刻时 `X-RateLimit-Reset`（epoch）/ `X-RateLimit-Reset-At`
     （ISO-8601 UTC）都在，正文里的 `reset_at` 与之一致；拿不到时刻时一个
     都不发，绝不用「现在 + 猜测」凑一个。
  2. 来源优先级：上游正文的墙钟压过本地冷却窗口换算出来的值（前者是上游
     说的，后者是网关猜的）。
  3. 不回归：非 429 响应一个头都不多发，`Retry-After` 的语义与取值不变。

运行：python tests/_test_rate_limit_reset_at.py
"""
import email
import io
import json
import os
import sys
import time
import unittest
import urllib.error
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _isolated_dirs import isolated_data_dirs    # noqa: E402

_TMP = isolated_data_dirs("wb-rate-limit-reset-")

import wb_accounts as A       # noqa: E402
import wb_proxy as P          # noqa: E402

MODEL = "deepseek-v4.1-flash"

# 上游 6004 的真实形状：英文与中文各一（parse_rate_limit_reset 认这两种）。
# 时刻按运行时钟往前推，不写死：正文里的墙钟必须是「还没过去」的，否则
# `_usable_reset_epoch` 会按设计把它当成陈旧响应丢掉——写死日期等于给这套件
# 装了个定时器，发布次日自己变红（v1.10.1 就是栽在这里）。UTC+8 后缀保留，
# 偏移换算改由 `SAMPLE_RESET_AT` 这个绝对 epoch 钉住（见 ResetSourceTests）：
# 只写「parse 出来的值与 parse 出来的值相等」是两边同源的循环断言，解析器
# 把整个偏移吃掉也照样全绿。
SAMPLE_RESET_IN = 600
SAMPLE_RESET_AT = int(time.time()) + SAMPLE_RESET_IN
SAMPLE_RESET_STAMP = time.strftime("%Y-%m-%d %H:%M:%S",
                                   time.gmtime(SAMPLE_RESET_AT + 8 * 3600))
DETAIL_EN = ("usage exceeds frequency limit, but don't worry, your usage will "
             "reset at %s UTC+8" % SAMPLE_RESET_STAMP)
DETAIL_CN = "您的使用量已超出频率限制，将在 %s UTC+8 重置" % SAMPLE_RESET_STAMP


class HeaderHelperTests(unittest.TestCase):
    """纯函数：头怎么拼，拿不到时刻时怎么退避。"""

    def test_both_absolute_headers_are_emitted(self):
        reset_at = int(time.time()) + 600
        headers = dict(P.rate_limit_headers(reset_at))
        self.assertEqual(headers["X-RateLimit-Reset"], str(reset_at))
        self.assertEqual(headers["X-RateLimit-Reset-At"],
                         time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                       time.gmtime(reset_at)))

    def test_no_instant_means_no_headers(self):
        """宁可不说，也不能编一个恢复时刻出来。"""
        for value in (None, 0, ""):
            with self.subTest(value=value):
                self.assertEqual(P.rate_limit_headers(value), [])

    def test_unusable_value_is_dropped_not_raised(self):
        self.assertEqual(P.rate_limit_headers("not-a-number"), [])

    def test_note_is_empty_without_an_instant(self):
        self.assertEqual(P.rate_limit_reset_note(None), "")
        reset_at = int(time.time()) + 600
        # 整串比对而不是只比日期前缀：期望值由组成的算式给出，不拿喂进去的
        # 值反过来当期望，否则「正文里的时刻其实取的是 now()」这种错看不见。
        self.assertEqual(P.rate_limit_reset_note(reset_at),
                         " (resets at %s)" % time.strftime(
                             "%Y-%m-%dT%H:%M:%SZ", time.gmtime(reset_at)))


class ResetSourceTests(unittest.TestCase):
    """时刻从哪来：上游正文 > 本地窗口换算；都没有就不编。"""

    def test_upstream_wall_clock_wins(self):
        exc = P.RateLimited(None, DETAIL_EN, wait=60)
        parsed = P.parse_rate_limit_reset(DETAIL_EN)
        self.assertIsNotNone(parsed)
        # 夹具自己钉在绝对 epoch 上：别的用例都是 parse 对 parse，同源到
        # 解析器把 UTC+8 偏移整个吃掉（offset=0）也全绿。
        self.assertEqual(parsed, SAMPLE_RESET_AT)
        self.assertEqual(P.rate_limit_reset_at(exc), int(parsed))

    def test_chinese_wording_is_understood_too(self):
        exc = P.RateLimited(None, DETAIL_CN, wait=60)
        self.assertEqual(P.rate_limit_reset_at(exc),
                         P.rate_limit_reset_at(P.RateLimited(None, DETAIL_EN)))

    def test_carried_instant_wins_over_the_body(self):
        """调用方已经按冷却窗口算过时，用它，而不是再解析一次正文。"""
        carried = int(time.time()) + 600
        exc = P.RateLimited(None, DETAIL_EN, wait=60, reset_at=carried)
        self.assertEqual(P.rate_limit_reset_at(exc), carried)

    def test_without_either_source_there_is_no_instant(self):
        exc = P.RateLimited(None, "usage exceeds frequency limit", wait=60)
        self.assertIsNone(P.rate_limit_reset_at(exc))


class PoolErrorPathTests(unittest.TestCase):
    """整池 429 的出口：时刻与 Retry-After 必须描述同一个窗口。

    这条路径（`open_upstream` 里 `last_429` 那个出口）以前没人钉过：它同时
    决定 `retry_after` 与 `reset_at`，两者必须指向同一个瞬间——一个说「再等
    600 秒」、另一个说「三小时后恢复」，客户端只会照着后一个睡过头。

    必须让上游真的回一个 429 才能走到这里：`realm_model_throttled` 之类的
    前置分支会在发请求之前就抛，钉不到出口那几行。
    """

    def setUp(self):
        self.original = P.POOL
        self.addCleanup(setattr, P, "POOL", self.original)
        self.pool = A.AccountPool(os.environ["ACCOUNTS_DIR"])
        P.POOL = self.pool
        acc = A.Account({"uid": "cn000001-0000-0000-0000-000000000000",
                         "accessToken": "x.y.z", "nickname": "cn000001",
                         "enabled": True, "realm": "cn"})
        self.pool.accounts = [acc]
        self.addCleanup(setattr, P, "POOL", self.original)

    def _open(self, detail):
        """让上游对每次尝试都回 429，返回抛出的 RateLimited。"""
        def always_429(*_args, **_kwargs):
            raise urllib.error.HTTPError(
                "https://up/v2/chat/completions", 429, "rate limited", {},
                io.BytesIO(detail.encode("utf-8")))
        with unittest.mock.patch.object(P.wb_accounts, "urlopen", always_429):
            with self.assertRaises(P.RateLimited) as ctx:
                P.open_upstream({"model": MODEL,
                                 "messages": [{"role": "user", "content": "hi"}]},
                                target_realm="cn")
        return ctx.exception

    def test_the_upstream_wall_clock_reaches_the_client(self):
        exc = self._open(DETAIL_EN)
        expected = P.parse_rate_limit_reset(DETAIL_EN)
        self.assertIsNotNone(expected, "样本正文必须能被解析，否则这条用例没意义")
        self.assertEqual(P.rate_limit_reset_at(exc), int(expected))

    def test_without_a_wall_clock_the_local_window_fills_in(self):
        exc = self._open("usage exceeds frequency limit")
        reset_at = P.rate_limit_reset_at(exc)
        self.assertIsNotNone(reset_at, "整池 429 也必须给出恢复时刻")
        self.assertLessEqual(abs((reset_at - time.time()) - exc.wait), 5,
                             "相对值与绝对值指向了不同的瞬间")

    def test_the_wall_clock_survives_the_round_trip_unchanged(self):
        """正文里的墙钟不能被冷却窗口的取整带偏。

        这条是变异测试逼出来的：把循环里记下的 `reset_at` 删掉之后，出口会
        退回 `time.time() + wait`，而 `wait` 是 `retry_after_seconds()` 取整后
        的秒数——测试若只断言「有 reset_at 且与 wait 接近」，删掉记录照样绿。
        真机后果是客户端按一个比上游晚若干秒的时刻醒来，正好撞在窗口边上。
        """
        exc = self._open(DETAIL_EN)
        self.assertEqual(P.rate_limit_reset_at(exc),
                         int(P.parse_rate_limit_reset(DETAIL_EN)),
                         "恢复时刻必须与上游正文逐秒一致，不能被本地窗口取整")


class StaleWallClockTests(unittest.TestCase):
    """上游正文里的墙钟只在还没过去时才压过本地冷却窗口。"""

    def setUp(self):
        self.original = P.POOL
        self.addCleanup(setattr, P, "POOL", self.original)
        self.pool = A.AccountPool(os.environ["ACCOUNTS_DIR"])
        P.POOL = self.pool
        self.pool.accounts = [A.Account(
            {"uid": "cn000001-0000-0000-0000-000000000000",
             "accessToken": "x.y.z", "nickname": "cn000001",
             "enabled": True, "realm": "cn"})]

    def _open(self, detail):
        def always_429(*_a, **_k):
            raise urllib.error.HTTPError(
                "https://up/v2/chat/completions", 429, "rl", {},
                io.BytesIO(detail.encode("utf-8")))
        with unittest.mock.patch.object(P.wb_accounts, "urlopen", always_429):
            with self.assertRaises(P.RateLimited) as ctx:
                P.open_upstream({"model": MODEL,
                                 "messages": [{"role": "user", "content": "hi"}]},
                                target_realm="cn")
        return ctx.exception

    def test_a_stale_wall_clock_falls_back_to_the_local_window(self):
        """正文里是一个早就到期的时刻：不能照抄给客户端。"""
        stale = ("usage exceeds frequency limit, but don't worry, your usage "
                 "will reset at 2020-01-01 00:00:00 UTC+8")
        exc = self._open(stale)
        reset_at = P.rate_limit_reset_at(exc)
        self.assertIsNotNone(reset_at)
        self.assertGreater(reset_at, time.time(),
                           "恢复时刻落在了过去，客户端会立刻重试并再次撞墙")
        # 回落到本地冷却窗口时允许一个取整量级的偏差（wait 是整秒）。
        self.assertLessEqual(abs((reset_at - time.time()) - exc.wait), 60)

    def test_a_live_wall_clock_is_still_preferred(self):
        future = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 7200))
        detail = ("usage exceeds frequency limit, your usage will reset at "
                  "%s UTC+0" % future)
        exc = self._open(detail)
        self.assertEqual(P.rate_limit_reset_at(exc),
                         int(P.parse_rate_limit_reset(detail)))


class InstantSanityTests(unittest.TestCase):
    """对外宣称的恢复时刻必须能用：不崩、不在过去、不离谱。"""

    def test_a_far_future_stamp_is_dropped_instead_of_raising(self):
        """越界 epoch 在 Windows 上让 time.gmtime 抛 OSError。

        漏捕的后果不是「少个头」，而是整条 429 连一个字节都发不出去：客户端
        看到的是连接被重置，而不是「稍后重试」。`parse_rate_limit_reset` 的
        契约写着「坏正文不许打断请求路径」，这里把它钉住。
        """
        for stamp in (253402271999, 99999999999999):
            with self.subTest(stamp=stamp):
                self.assertEqual(P.rate_limit_headers(stamp), [])
                self.assertEqual(P.rate_limit_reset_note(stamp), "")

    def test_an_instant_in_the_past_is_dropped(self):
        """陈旧响应被重放时，正文里的墙钟已经过期。

        照抄会让响应同时说「再等 60 秒」和「304 天前就恢复了」，客户端选哪个
        都不对。宁可不发。
        """
        past = int(time.time()) - 3600
        self.assertEqual(P.rate_limit_headers(past), [])
        self.assertEqual(P.rate_limit_reset_note(past), "")
        self.assertIsNone(P._usable_reset_epoch(past))

    def test_the_boundary_is_exclusive_on_both_ends(self):
        now = int(time.time())
        self.assertIsNone(P._usable_reset_epoch(now))          # 就是现在
        self.assertIsNone(P._usable_reset_epoch(0))            # epoch 0
        self.assertIsNone(P._usable_reset_epoch(-5))           # 负值
        self.assertIsNotNone(P._usable_reset_epoch(now + 60))  # 正常

    def test_junk_is_dropped_not_raised(self):
        for junk in ("abc", None, "", [], {}, object()):
            with self.subTest(junk=repr(junk)):
                self.assertIsNone(P._usable_reset_epoch(junk))


class Dispatch(P.Handler):
    """真实的 429 写出路径：头与正文都由处理器决定，必须走它。"""

    def __init__(self, path="/v1/chat/completions", body=b"{}"):
        self.path = path
        self.command = "POST"
        self.request_version = "HTTP/1.1"
        self.close_connection = True
        self.client_address = ("127.0.0.1", 40000)
        raw = "".join("%s: %s\r\n" % item
                      for item in {"Content-Length": str(len(body))}.items())
        self.headers = email.message_from_string(raw)
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = None
        self.headers_out = []

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, key, value):
        self.headers_out.append((key, value))

    def end_headers(self):
        pass

    def log_message(self, fmt, *args):
        pass

    def header(self, name):
        for key, value in self.headers_out:
            if key.lower() == name.lower():
                return value
        return None

    def payload(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


class ResponseTests(unittest.TestCase):
    """处理器写出的东西：状态码、头、正文三者互相一致。"""

    def setUp(self):
        self.original = P.POOL
        self.addCleanup(setattr, P, "POOL", self.original)

    def _dispatch(self, exc, path="/v1/chat/completions"):
        request = Dispatch(path=path)
        with unittest.mock.patch.object(P, "cors_origin_allowed", lambda _p: False):
            if path.startswith("/v1/messages"):
                request._anthropic_rate_limited(exc)
            else:
                request._rate_limited(exc)
        return request

    def test_openai_envelope_carries_the_instant(self):
        reset_at = int(time.time()) + 600
        exc = P.RateLimited(None, DETAIL_EN, wait=600, reset_at=reset_at)
        request = self._dispatch(exc)
        self.assertEqual(request.status, 429)
        self.assertEqual(request.header("Retry-After"), "600")
        self.assertEqual(request.header("X-RateLimit-Reset"), str(reset_at))
        body = request.payload()["error"]
        self.assertEqual(body["retry_after"], 600)
        self.assertIn("reset_at", body, "正文必须带 reset_at 字段")
        self.assertEqual(body["reset_at"], reset_at)
        self.assertIn("resets at", body["message"])

    def test_anthropic_envelope_carries_the_instant_too(self):
        reset_at = int(time.time()) + 600
        exc = P.RateLimited(None, DETAIL_EN, wait=600, reset_at=reset_at)
        request = self._dispatch(exc, path="/v1/messages")
        self.assertEqual(request.status, 429)
        self.assertEqual(request.header("X-RateLimit-Reset"), str(reset_at))
        self.assertIn("resets at",
                      request.payload()["error"]["message"])

    def test_retry_after_semantics_are_unchanged(self):
        """老客户端只看 Retry-After，它的取值规则一个字都没动。"""
        exc = P.RateLimited(None, "", wait=1)
        request = self._dispatch(exc)
        self.assertEqual(request.header("Retry-After"), "1")
        self.assertIsNone(request.header("X-RateLimit-Reset"))

    def test_non_429_error_gains_no_extra_headers(self):
        request = Dispatch()
        with unittest.mock.patch.object(P, "cors_origin_allowed", lambda _p: False):
            request._error(400, "bad request", "invalid_request_error")
        self.assertEqual(request.status, 400)
        self.assertIsNone(request.header("X-RateLimit-Reset"))
        self.assertIsNone(request.header("Retry-After"))
        self.assertNotIn("reset_at", request.payload()["error"])

    def test_a_non_429_never_emits_the_instant_even_when_given_one(self):
        """非 429 带上恢复时刻没有意义，给了也不许发出去。

        这条是变异测试逼出来的：把 `_error` 里的 429 判断去掉之后，既有
        用例全绿——因为它们本来就没给非 429 传时刻，等于什么都没验证。
        """
        request = Dispatch()
        reset_at = int(time.time()) + 30
        with unittest.mock.patch.object(P, "cors_origin_allowed", lambda _p: False):
            request._error(400, "bad request", "invalid_request_error",
                           reset_at=reset_at)
        self.assertEqual(request.status, 400)
        self.assertIsNone(request.header("X-RateLimit-Reset"))
        self.assertIsNone(request.header("X-RateLimit-Reset-At"))
        self.assertNotIn("reset_at", request.payload()["error"])

    def test_error_helper_forwards_an_explicit_instant(self):
        request = Dispatch()
        reset_at = int(time.time()) + 30
        with unittest.mock.patch.object(P, "cors_origin_allowed", lambda _p: False):
            request._error(429, "too many login attempts", "rate_limit_error",
                           reset_at=reset_at)
        self.assertEqual(request.status, 429)
        self.assertEqual(request.header("X-RateLimit-Reset"), str(reset_at))



if __name__ == "__main__":
    unittest.main(verbosity=2)
