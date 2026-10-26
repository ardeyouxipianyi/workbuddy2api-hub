"""跨出口回退（国际版 → 国内版，开关默认关）。

两个出口是两套独立上游：国际版打 www.workbuddy.ai，国内版打
copilot.tencent.com，各自有独立的频率墙与额度窗口。派发路径一直把它们当两个
互不相干的池子——realm 在请求入口定死，AccountPool.pick() 只挑同 realm 的账号
（见 wb_accounts.AccountPool.pick），所以国际版整池被限流时，即使国内版还有
空号，请求也只会等一个 429。

这里钉住新加的兜底：只在开关打开、只在国际版、只对双出口模型、只在对方确实有
可用账号时，才把一次已经失败的请求重放到国内版出口；其余失败照原样抛出。

不联网：上游调用与账号池都是桩。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy
import wb_settings
import _dashboard_source  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOUBLE_EXIT_MODEL = "deepseek-v4.1-flash"   # 两个出口都提供
INTL_ONLY_MODEL = "gpt-6-astra"             # 只有国际版提供


class _Pool(object):
    """count_ready 的最小实现：只关心「国内版有没有空号」。"""

    def __init__(self, ready_cn=True):
        self.ready_cn = ready_cn
        self.asked = []

    def count_ready(self, realm=None, model=None, allowed=None):
        self.asked.append((realm, model))
        if realm == "cn":
            return 1 if self.ready_cn else 0
        return 1


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wb-realm-fallback-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.calls = []
        self.pool = _Pool()
        patches = [
            mock.patch.object(wb_proxy, "ACCOUNTS_DIR", self.dir),
            mock.patch.object(wb_proxy, "POOL", self.pool),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def enable(self, value=True):
        wb_settings.set_realm_fallback_enabled(self.dir, value)

    def payload(self, model=DOUBLE_EXIT_MODEL):
        return {"model": model, "messages": [{"role": "user", "content": "hi"}]}

    def stub_upstream(self, cn_result=("upstream", "acct", None),
                      intl_error=None, cn_error=None, any_error=None):
        """把 open_upstream 换成桩：记录每次尝试的 realm。

        `any_error` 让**所有** realm 都失败（用来验证「国内版请求也不回退」
        这条单向规则，否则国内版那次调用会直接成功）。
        """
        def fake(payload, session_key=None, target_realm=None, **kwargs):
            self.calls.append(target_realm)
            if any_error is not None:
                raise any_error
            if target_realm == "cn":
                if cn_error is not None:
                    raise cn_error
                return cn_result
            raise intl_error if intl_error is not None else wb_proxy.RateLimited(
                None, "usage exceeds frequency limit", wait=60)
        patch = mock.patch.object(wb_proxy, "open_upstream", fake)
        patch.start()
        self.addCleanup(patch.stop)


class SwitchTests(_Base):
    """开关本身：默认关、可持久化、严格布尔。"""

    def test_defaults_to_off(self):
        self.assertFalse(wb_settings.realm_fallback_enabled(self.dir))

    def test_roundtrip(self):
        self.assertTrue(wb_settings.set_realm_fallback_enabled(self.dir, True))
        self.assertTrue(wb_settings.realm_fallback_enabled(self.dir))
        wb_settings.set_realm_fallback_enabled(self.dir, False)
        self.assertFalse(wb_settings.realm_fallback_enabled(self.dir))


class TargetTests(_Base):
    """realm_fallback_target 的四个前提，逐个拆开。"""

    def test_intl_double_exit_model_has_a_target(self):
        self.enable()
        self.assertEqual(
            wb_proxy.realm_fallback_target("intl", DOUBLE_EXIT_MODEL), "cn")

    def test_domestic_never_falls_back(self):
        # 单向：国内号是便宜的一侧，把流量挪到国际出口会花掉没打算花的额度。
        self.enable()
        self.assertEqual(wb_proxy.realm_fallback_target("cn", DOUBLE_EXIT_MODEL), "")

    def test_exclusive_models_never_fall_back(self):
        self.enable()
        self.assertEqual(
            wb_proxy.realm_fallback_target("intl", INTL_ONLY_MODEL), "")

    def test_no_target_without_a_domestic_account(self):
        self.enable()
        self.pool.ready_cn = False
        self.assertEqual(
            wb_proxy.realm_fallback_target("intl", DOUBLE_EXIT_MODEL), "")
        # 空池也要能问出来，不能靠异常吞掉
        self.assertTrue(self.pool.asked)


class FallbackTests(_Base):
    """open_upstream_with_fallback 的实际重放行为。"""

    def test_off_by_default_raises_without_retry(self):
        self.stub_upstream()
        with self.assertRaises(wb_proxy.RateLimited):
            wb_proxy.open_upstream_with_fallback(
                self.payload(), target_realm="intl")
        self.assertEqual(self.calls, ["intl"])

    def test_enabled_replays_on_the_domestic_exit(self):
        self.enable()
        self.stub_upstream()
        result = wb_proxy.open_upstream_with_fallback(
            self.payload(), target_realm="intl")
        self.assertEqual(result[0], "upstream")
        self.assertEqual(self.calls, ["intl", "cn"])

    def test_exclusive_model_does_not_retry(self):
        self.enable()
        self.stub_upstream()
        with self.assertRaises(wb_proxy.RateLimited):
            wb_proxy.open_upstream_with_fallback(
                self.payload(INTL_ONLY_MODEL), target_realm="intl")
        self.assertEqual(self.calls, ["intl"])

    def test_domestic_request_never_retries_on_intl(self):
        self.enable()
        self.stub_upstream(any_error=wb_proxy.RateLimited(
            None, "usage exceeds frequency limit", wait=60))
        with self.assertRaises(wb_proxy.RateLimited):
            wb_proxy.open_upstream_with_fallback(
                self.payload(), target_realm="cn")
        self.assertEqual(self.calls, ["cn"])

    def test_explicit_exit_is_respected(self):
        # Key 绑定出口 / X-Realm / ?realm= 是运营者写下的意图，兜底不该越过它。
        self.enable()
        self.stub_upstream()
        with self.assertRaises(wb_proxy.RateLimited):
            wb_proxy.open_upstream_with_fallback(
                self.payload(), target_realm="intl", allow_realm_fallback=False)
        self.assertEqual(self.calls, ["intl"])

    def test_empty_pool_also_falls_back(self):
        self.enable()
        self.stub_upstream(intl_error=RuntimeError(
            "no usable account for realm 'intl': all are disabled"))
        result = wb_proxy.open_upstream_with_fallback(
            self.payload(), target_realm="intl")
        self.assertEqual(result[0], "upstream")
        self.assertEqual(self.calls, ["intl", "cn"])

    def test_other_failures_pass_through(self):
        # 403/内容拒绝/参数错误换出口也修不好，不重放。
        self.enable()
        self.stub_upstream(intl_error=RuntimeError("something else entirely"))
        with self.assertRaises(RuntimeError):
            wb_proxy.open_upstream_with_fallback(
                self.payload(), target_realm="intl")
        self.assertEqual(self.calls, ["intl"])

    def test_both_exits_throttled_reports_the_domestic_wait(self):
        self.enable()
        self.stub_upstream(cn_error=wb_proxy.RateLimited(None, "cn throttled", wait=900))
        with self.assertRaises(wb_proxy.RateLimited) as ctx:
            wb_proxy.open_upstream_with_fallback(
                self.payload(), target_realm="intl")
        # 两条都限流时，客户端该等多久取决于国内版给的重置时钟。
        self.assertEqual(ctx.exception.wait, 900)
        self.assertEqual(self.calls, ["intl", "cn"])


class PanelTests(unittest.TestCase):
    """面板三处文案必须逐字一致：正文 / 英文词典 / 繁体词典。"""

    @classmethod
    def setUpClass(cls):
        cls.html = _dashboard_source.dashboard_html()

    def test_switch_exists(self):
        self.assertIn('id="setRealmFallback"', self.html)
        self.assertIn("saveRealmFallback", self.html)
        self.assertIn("realm_fallback_enabled", self.html)

    def test_copy_is_in_both_dictionaries(self):
        phrases = [
            "跨出口回退（国际版 → 国内版）",
            "启用跨出口回退",
            "已启用：国际版限流时回退到国内版",
            "已关闭跨出口回退",
        ]
        for phrase in phrases:
            # 正文 + DICT + HANT_PHRASES
            self.assertEqual(self.html.count(phrase), 3,
                             "「%s」应当在正文、英文词典、繁体词典里各出现一次"
                             % phrase)


if __name__ == "__main__":
    unittest.main(verbosity=2)
