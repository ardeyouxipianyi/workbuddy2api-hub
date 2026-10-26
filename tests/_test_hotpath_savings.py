"""热路径每请求固定开销的削减：等价性对拍与缓存失效回归。

这个套件钉住四项热路径改动（PR: 代理请求热路径削开销）的正确性：

  1. has_fingerprint 的字面量快路径与原来的两个 (?i) 正则逐字等价 —— 含 re.I 的
     Unicode 特例（'ſ' 能替 s、'İ'/'ı' 能替 i）、大小写混排、内嵌长文本；
  2. build_upstream_body 幂等，open_upstream(prebuilt_body=...) 不再重复构建
     （/v1/chat/completions 每请求只构建一次）；
  3. estimate_tokens 的单遍实现与原来的逐字符循环逐值相等（含 CJK 边界、代理对、
     非字符串输入）；
  4. wb_settings.load 的 (path, mtime, size) 缓存：外部改写后失效、save() 写后
     立即可见（含 limits_data 首读迁移的写）、返回的是顶层拷贝；
  5. 模块级 _usage 只剩进程启动时刻：record_usage/record_error 不再累加没人读的
     requests/ttft/gen/wall/by_model，快照的 started/since 仍从它取。
"""
import json
import os
import random
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _isolated_dirs import isolated_data_dirs  # noqa: E402  (its own data root)

_TMP = isolated_data_dirs("wb-hotpath-")

import wb_proxy as P  # noqa: E402

import wb_usage as U  # noqa: E402
import wb_wire as W  # noqa: E402
import wb_transforms as T  # noqa: E402
import wb_settings  # noqa: E402

# 指纹基串从模块常量派生，而不是手抄一份字面量：手抄两份必然漂移，
# 派生值才是唯一可信来源。
FP = T.SANITIZE_OMO_JUNIOR_RE.pattern
HDR = T.SANITIZE_BARE_HDR_LOWER
# re.I 能匹配 i/s 而 str.lower() 不能的三个 Unicode 字符：İ / ı / ſ。
SPECIALS = ("\u0130", "\u0131", "\u017f")


def old_has_fingerprint(text):
    """改动前的实现（等价性基准）：两个 (?i) 正则做字面量匹配。"""
    if not isinstance(text, str) or not text:
        return False
    for f in T.SANITIZE_FEATURES:
        if f in text:
            return True
    return bool(T.SANITIZE_BARE_HDR_RE.search(text)
                or T.SANITIZE_OMO_JUNIOR_RE.search(text))


def old_estimate_tokens(text):
    """改动前的实现（等价性基准）：逐字符 Python 循环。"""
    if not text:
        return 0
    if not isinstance(text, str):
        text = str(text)
    cjk = sum(1 for c in text
              if '\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf')
    other = len(text) - cjk
    return cjk + max(1, int(other / 3.6)) if text else 0


def fingerprint_cases():
    """定向用例：每个位置换成特例字符、大小写混排、内嵌长文本、随机串。"""
    cases = [
        FP, FP.lower(), FP.upper(), FP.swapcase(),
        HDR, HDR.upper(), HDR.title(),
        "", "plain text with no fingerprint",
        "Sisyphus-Junior alone", "from OhMyOpenCode alone",
    ]
    cases += list(T.SANITIZE_FEATURES)
    for special in SPECIALS:
        for pos in range(len(FP)):
            t = FP[:pos] + special + FP[pos + 1:]
            cases += [t, t.upper(), t.swapcase(), t.lower()]
        for pos in range(len(HDR)):
            cases.append(HDR[:pos] + special + HDR[pos + 1:])
        cases += [special, special * 3, "abc" + special + "def",
                  "Sisyphus" + special + "-Junior - Focused executor"]
    # 内嵌长文本：指纹在 4KB/64KB 正文的中间与结尾
    filler = "lorem ipsum dolor sit amet " * 200
    for text in (FP, FP.lower(), FP.upper()):
        cases += [filler + text + filler, filler * 16 + text, text + filler * 16]
    # 随机串：字母表里塞进全部特例字符，覆盖面比定向用例更宽
    rng = random.Random(20261009)
    alphabet = list("Sisyphus-Junior x-anthropic-billing-hdr") + list(SPECIALS) \
        + ["S", "s", "I", "i", "K", "\u212a", "\u00df", "\u1e9e", "-", " "]
    for _ in range(30000):
        n = rng.randint(0, 40)
        cases.append("".join(rng.choice(alphabet) for _ in range(n)))
    return cases


class FingerprintFastPathTests(unittest.TestCase):
    def test_has_fingerprint_matches_the_regex_implementation(self):
        mismatches = [t for t in fingerprint_cases()
                      if T.has_fingerprint(t) != old_has_fingerprint(t)]
        self.assertEqual(
            mismatches[:5], [],
            "快路径与 (?i) 正则判定不一致（%d 例）" % len(mismatches))

    def test_sanitize_text_matches_the_regex_implementation(self):
        # 判定一致还不够：判 False 时不得发生改写，判 True 时两个实现要给出
        # 同样的 sanitize 结果。
        # OMO 替换串（把整条指纹规范化成不带框架归因的短形式）从模块行为派生：
        # 对整条指纹跑一次 sanitize_text 拿到的就是那个字面量，手抄第二份不可靠。
        replacement = T.sanitize_text(FP)
        self.assertNotEqual(replacement, FP, "指纹应当被改写")

        def old_sanitize(text):
            if not isinstance(text, str) or not text:
                return text
            if not old_has_fingerprint(text):
                return text
            text = T.SANITIZE_OMO_JUNIOR_RE.sub(replacement, text)
            for old, new in T.SANITIZE_REWRITES:
                text = text.replace(old, new)
            text = T.SANITIZE_HDR_RE.sub("", text)
            if "cc_" in text:
                prev = ""
                while prev != text:
                    prev = text
                    text = T.SANITIZE_KV_RE.sub("", text)
            text = T.SANITIZE_BARE_HDR_RE.sub("x-anthropic-billing-hdr", text)
            return text.strip()

        for t in fingerprint_cases():
            self.assertEqual(T.sanitize_text(t), old_sanitize(t), repr(t[:80]))

    def test_lowercase_constants_stay_in_sync_with_the_regexes(self):
        # 快路径的两个小写字面量必须与正则同源，改一处忘另一处会被这里拦下。
        self.assertEqual(T.SANITIZE_OMO_JUNIOR_RE.pattern.lower(),
                         T.SANITIZE_OMO_JUNIOR_LOWER)
        self.assertEqual(T.SANITIZE_BARE_HDR_RE.pattern,
                         "(?i)" + T.SANITIZE_BARE_HDR_LOWER)

    def test_unicode_case_specials_follow_re_ignorecase(self):
        # 'ſ' 替 s、'İ'/'ı' 替 i：这三个字符是 re.I 与 lower() 的全部差异点，
        # 快路径必须和正则一样认出它们。
        self.assertTrue(T.has_fingerprint(FP.replace("s", "\u017f", 1)))
        self.assertTrue(T.has_fingerprint(FP.replace("i", "\u0130", 1)))
        self.assertTrue(T.has_fingerprint(FP.replace("i", "\u0131", 1)))
        self.assertTrue(old_has_fingerprint(FP.replace("s", "\u017f", 1)))
        # 单独出现不是指纹（与改动前一致）
        self.assertFalse(T.has_fingerprint("plain \u017f \u0130 \u0131 text"))
        self.assertEqual(T.has_fingerprint("plain \u017f \u0130 \u0131 text"),
                         old_has_fingerprint("plain \u017f \u0130 \u0131 text"))


class BuildUpstreamBodyReuseTests(unittest.TestCase):
    PAYLOADS = [
        {"model": "deepseek-v4.1-flash",
         "messages": [{"role": "user", "content": "hi"}]},
        {"model": "deepseek-v4.1-flash",
         "messages": [{"role": "user", "content": "hi"}],
         "max_completion_tokens": 8192},
        {"model": "gpt-6-astra", "stream": True,
         "messages": [{"role": "developer", "content": "sys"},
                      {"role": "user", "content": "hi"}]},
        {"model": "deepseek-v4.1-flash",
         "messages": [
             {"role": "assistant", "tool_calls": [
                 {"id": "c1", "type": "function",
                  "function": {"name": "f", "arguments": "{}"}}]},
             {"role": "tool", "tool_call_id": "c1", "content": "ok"}],
         "tools": [{"name": "f", "description": "d", "parameters": {}}],
         "tool_choice": {"type": "function", "function": {"name": "f"}}},
        {"model": "deepseek-v4.1-flash",
         "messages": [{"role": "user", "content": FP}],
         "_internal_marker": True, "reasoning_effort": "high"},
    ]

    def test_build_upstream_body_is_idempotent(self):
        # 复用 prebuilt 的前提：同一个 payload 连建两次结果一致，且不改入参。
        for payload in self.PAYLOADS:
            before = json.dumps(payload, sort_keys=True, ensure_ascii=False)
            first = P.build_upstream_body(payload)
            second = P.build_upstream_body(payload)
            self.assertEqual(first, second, payload.get("model"))
            self.assertEqual(json.dumps(payload, sort_keys=True, ensure_ascii=False),
                             before, "build_upstream_body 不应改动入参")

    def test_open_upstream_reuses_a_prebuilt_body(self):
        payload = dict(self.PAYLOADS[0])
        prebuilt = P.build_upstream_body(payload)
        calls = {"n": 0}
        real_build = P.build_upstream_body
        real_pool = P.POOL

        def counting(body):
            calls["n"] += 1
            return real_build(body)

        P.build_upstream_body = counting
        # 没有池子：open_upstream 会在选号前把 body 建好、然后以
        # RuntimeError("no usable account ...") 结束，全程不碰网络。
        P.POOL = None
        try:
            with self.assertRaises(RuntimeError):
                P.open_upstream(payload, prebuilt_body=prebuilt)
            self.assertEqual(calls["n"], 0, "传了 prebuilt_body 就不该再构建")
            with self.assertRaises(RuntimeError):
                P.open_upstream(payload)
            self.assertEqual(calls["n"], 1, "不传 prebuilt_body 时应构建一次")
        finally:
            P.build_upstream_body = real_build
            P.POOL = real_pool


class EstimateTokensTests(unittest.TestCase):
    def test_matches_the_character_loop(self):
        samples = [
            "", None, 0, False, 123, 12.5, ["a"], {"a": 1},
            "hello world", "你好世界", "mixed 中文 and english",
            # CJK 区段边界：3400/4DBF/4E00/9FFF 在内，33FF/4DC0/A000 在外
            "\u3400\u4dbf\u4e00\u9fff", "\u33ff\u4dc0\ua000",
            "\u9fff" * 10 + "a" * 10, "a\u0301b", "🙂🙂",
            "x" * 4096, "汉" * 4096, "abc中文def" * 1000,
        ]
        for s in samples:
            self.assertEqual(P.estimate_tokens(s), old_estimate_tokens(s),
                             repr(s)[:60])

    def test_cjk_re_covers_exactly_the_loop_ranges(self):
        inside = "\u3400\u4dbf\u4e00\u9fff"
        outside = "\u33ff\u4dc0\ua000 ab"
        self.assertEqual(len(W.CJK_RE.sub("", inside)), 0)
        self.assertEqual(W.CJK_RE.sub("", outside), outside)


class UsageCounterTests(unittest.TestCase):
    """_usage 只剩进程启动时刻：请求路径不再维护没人读的进程级累计。

    record_usage/record_error 曾经每请求往模块级 _usage 里累加 requests /
    令牌 / ttft / gen / wall / by_model，而全仓唯一的读者是快照的
    _usage["started"]。钉住：只留 started、请求不改它、快照的 started/since
    仍然从它取（口径一行未变）。
    """

    def test_usage_holds_only_the_start_marker(self):
        started = U._usage["started"]
        U.record_usage("m-1", {"prompt_tokens": 10, "completion_tokens": 5,
                               "total_tokens": 15}, stream=True, elapsed_ms=1200,
                       ttft_ms=300, gen_ms=900, account=None)
        U.record_error("m-1", 500, "boom", elapsed_ms=200)
        self.assertEqual(U._usage, {"started": started})

    def test_snapshot_started_and_since_come_from_it(self):
        U._usage["started"] = 1791445200.0
        snap = U.usage_snapshot(realm=None, ttl=0)
        self.assertEqual(snap["started"], 1791445200.0)
        self.assertEqual(snap["since"],
                         time.strftime("%Y-%m-%d %H:%M:%S",
                                       time.localtime(1791445200.0)))


class JsonSpy(object):
    """Counts json.load calls while passing everything else through."""

    def __init__(self, real):
        self.real = real
        self.loads = 0

    def load(self, fh):
        self.loads += 1
        return self.real.load(fh)

    def dump(self, *args, **kwargs):
        return self.real.dump(*args, **kwargs)


class SettingsCacheTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wb-hotpath-settings-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = wb_settings.settings_path(self.dir)

    def test_repeated_loads_parse_once(self):
        wb_settings.save(self.dir, {"ui_language": "en"})
        real_json = wb_settings.json
        spy = JsonSpy(real_json)
        wb_settings.json = spy
        try:
            first = wb_settings.load(self.dir)
            second = wb_settings.load(self.dir)
        finally:
            wb_settings.json = real_json
        self.assertEqual(first, {"ui_language": "en"})
        self.assertEqual(second, first)
        self.assertEqual(spy.loads, 1, "第二次 load 应命中缓存，不再解析")

    def test_save_invalidates_the_cache(self):
        wb_settings.save(self.dir, {"ui_language": "en"})
        real_json = wb_settings.json
        spy = JsonSpy(real_json)
        wb_settings.json = spy
        try:
            wb_settings.load(self.dir)              # 1 次解析
            wb_settings.load(self.dir)              # 命中缓存
            wb_settings.save(self.dir, {"ui_language": "zh"})
            self.assertEqual(wb_settings.load(self.dir)["ui_language"], "zh")
            wb_settings.load(self.dir)              # 再次命中
        finally:
            wb_settings.json = real_json
        self.assertEqual(spy.loads, 2, "save() 之后必须重新解析一次，且只一次")

    def test_external_rewrite_is_seen(self):
        wb_settings.save(self.dir, {"ui_language": "en"})
        self.assertEqual(wb_settings.load(self.dir)["ui_language"], "en")
        # 模拟面板之外的进程改写（尺寸不同 -> 缓存键必变）
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"ui_language": "zh-Hant", "extra": "x" * 64}, fh)
        self.assertEqual(wb_settings.load(self.dir)["ui_language"], "zh-Hant")

    def test_write_through_even_when_mtime_is_forced_back(self):
        # 极端情况：新内容与旧文件同尺寸、mtime 又被强行改回旧值（FAT 只有 2s
        # 精度）。save() 的主动失效必须保证下一个 load() 读到新内容。
        wb_settings.save(self.dir, {"k": "aaaa"})
        old_info = os.stat(self.path)
        wb_settings.load(self.dir)
        wb_settings.save(self.dir, {"k": "bbbb"})   # 同尺寸改写
        os.utime(self.path, (old_info.st_atime, old_info.st_mtime))
        self.assertEqual(wb_settings.load(self.dir)["k"], "bbbb")

    def test_limits_migration_write_is_visible(self):
        # limits_data 首读迁移会写文件：迁移后 load() 必须看到新形状。
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"daily_token_limit": 5}, fh)
        self.assertNotIn("limits", wb_settings.load(self.dir))
        limits = wb_settings.limits_data(self.dir)
        self.assertEqual(limits["daily_token_limit"]["global"], 5)
        stored = wb_settings.load(self.dir)
        self.assertIn("limits", stored)
        self.assertNotIn("daily_token_limit", stored)

    def test_missing_file_reads_empty_and_creation_is_seen(self):
        self.assertEqual(wb_settings.load(self.dir), {})
        wb_settings.save(self.dir, {"a": 1})
        self.assertEqual(wb_settings.load(self.dir), {"a": 1})

    def test_returned_dict_is_a_copy(self):
        wb_settings.save(self.dir, {"a": 1})
        first = wb_settings.load(self.dir)
        first["a"] = 999
        first["b"] = "new"
        self.assertEqual(wb_settings.load(self.dir), {"a": 1})

    def test_corrupt_file_reads_empty(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(wb_settings.load(self.dir), {})


if __name__ == "__main__":
    unittest.main()
