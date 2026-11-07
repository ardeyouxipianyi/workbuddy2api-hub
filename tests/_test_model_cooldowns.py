"""Account and panel state after a model-scoped upstream 429.

Run with: python _test_model_cooldowns.py
No upstream credentials or outbound network are used.
"""
import atexit
import io
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
import urllib.error

_startup_dir = tempfile.TemporaryDirectory(prefix="model-cooldowns-")
atexit.register(_startup_dir.cleanup)
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
# The 429 path now journals a limit event (usage/limit-events.jsonl, the
# remaining-usage estimate's samples), so the usage dir has to be isolated too
# or this suite would write that journal into the checkout.
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts as accounts
import wb_proxy as proxy
import wb_settings as settings

# The real 429 shapes from the incident environment (issue #70, sanitized).
# S1 is 229 of the 263 recorded 429s and carries no reset information at all;
# S2 is the one provably intl body with no reset field (the log keeps only the
# first 200 characters, so the JSON below is that prefix closed into valid
# JSON); S3 is a parsed control that names its reset instant.
S1_BARE = "usage exceeds frequency limit"
S2_INTL_14003 = ('{"code":14003,"msg":"too many requests",'
                 '"retry_policy":{"retry":true},'
                 '"displayMsg":{"en":"Too many requests. Please retry later.",'
                 '"zh":"请求过于频繁，请稍后重试。"}}')
# The same body as the gateway sees it: it reads up to 600 characters while the
# log kept 200, so the real body carries a metadata tail. accountId and
# credentialId are field names, not scope statements.
S2_INTL_FULL = ('{"code":14003,"msg":"too many requests",'
                '"accountId":"<redacted>","credentialId":"<redacted>",'
                '"requestId":"<redacted>","retry_policy":{"retry":true},'
                '"displayMsg":{"en":"Too many requests. Please retry later.",'
                '"zh":"请求过于频繁，请稍后重试。"},"ts":1790500000}')
S3_INTL_6004 = ('{"code":6004,"msg":"usage exceeds frequency limit, but don\'t '
                'worry, your usage will reset at 2026-10-06 21:00:09 UTC+8, '
                'alternatively, you can switch to the other models to continue '
                'using it."}')

# A 429 also registers a window record (wb_window_probe) next to the gateway's
# accounts dir. The stub pool below has no `dir`, so the write would fall back
# to the repo's own accounts/ folder - point the constant at the temp dir as
# well, so running the suite never leaves state behind in the checkout.
proxy.ACCOUNTS_DIR = _startup_dir.name


class ModelCooldownTests(unittest.TestCase):
    def account(self):
        return accounts.Account({"uid": "synthetic-cn", "realm": "cn", "accessToken": "token"})

    def test_account_snapshot_and_selection(self):
        account = self.account()
        now = time.time()
        account.note_error("429", model="glm-5.3", until=now + 600)
        account.note_error("429", model="glm-5.2", until=now + 60)
        state = account.public()

        self.assertEqual([item["model"] for item in state["modelCooldowns"]],
                         ["glm-5.2", "glm-5.3"])
        self.assertTrue(all(isinstance(item["expiresAt"], int)
                            for item in state["modelCooldowns"]))
        self.assertFalse(state["inCooldown"])
        self.assertTrue(account.ready(model="another-model"))
        self.assertFalse(account.ready(model="glm-5.3"))

        account.clear_error(model="glm-5.3")
        self.assertEqual([item["model"] for item in account.public()["modelCooldowns"]],
                         ["glm-5.2"])
        with account._throttle_lock:
            account.model_cooldowns["glm-5.2"] = time.time() - 1
        self.assertEqual(account.model_cooldowns_snapshot(), [])

        with account._throttle_lock:
            account.cooldown_until = 1000.0
        with mock.patch.object(accounts.time, "time", return_value=1000.0):
            at_deadline = account.public()
        self.assertFalse(at_deadline["inCooldown"])
        self.assertIsNone(at_deadline["cooldownFor"])

    def test_snapshot_does_not_wait_for_token_refresh(self):
        account = self.account()
        account.note_error("429", model="glm-5.3", until=time.time() + 60)
        with account._refresh_lock:
            start = time.monotonic()
            self.assertEqual(account.public()["modelCooldowns"][0]["model"], "glm-5.3")
            self.assertLess(time.monotonic() - start, 1)

    def test_concurrent_updates_and_panel_reads(self):
        account = self.account()
        stop = threading.Event()
        failures = []

        def writer():
            n = 0
            while not stop.is_set():
                account.note_error("429", model="m%d" % (n % 20), until=time.time() + 5)
                account.clear_error(model="m%d" % ((n + 1) % 20))
                n += 1

        def reader():
            try:
                while not stop.is_set():
                    account.public()
            except Exception as exc:
                failures.append(exc)

        threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader)
                                                      for _ in range(2)]
        for thread in threads:
            thread.start()
        try:
            time.sleep(0.5)
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=2)
        self.assertFalse(any(thread.is_alive() for thread in threads), "worker did not stop")
        self.assertEqual(failures, [])

    def drive_one_429(self, account, detail="usage exceeds frequency limit",
                      stub_parser=True, model="glm-5.3", target_realm="cn"):
        """Drive open_upstream into an upstream 429 with a stubbed urlopen.

        Returns the reset instant the stubbed parser reported, so callers can
        assert the cooldown the gateway recorded. `stub_parser=False` leaves the
        real parser in place, which is what the unparsed-body case needs.
        """
        reset = time.time() + 600
        error = urllib.error.HTTPError("https://upstream.invalid", 429, "rate limit", {},
                                       io.BytesIO(detail.encode("utf-8")))

        class Pool(object):
            accounts = [account]
            affinity = types.SimpleNamespace(unbind=lambda _key: None,
                                             demote=lambda _key, _uid=None: None)

            def count_ready(self, realm, model=None):
                return sum(a.ready(model=model) for a in self.accounts)

            def pick_for_session(self, realm, session_key=None, exclude=(), model=None,
                                 page=None):
                return next((a for a in self.accounts if a.uid not in exclude
                             and a.realm == realm and a.ready(model=model)), None)

            def list_public(self):
                return [a.public() for a in self.accounts]

            def apply_daily_token_limit(self, value=None, usage=None):
                # The production path pushes the daily guard into the pool before
                # picking; this stub only needs to answer the call.
                return value or 0

            def apply_daily_credit_limit(self, value=None, credits=None,
                                         free_models=None):
                return value or 0

            def apply_model_daily_token_limit(self, value=None, per_model=None):
                return value or 0

            def apply_remaining_weights(self, weights=None):
                # 剩余用量优先调度的权重表也由请求路径推给池；桩只负责接住
                # 调用（开关默认关，推来的就是 None）。
                return weights or {}

        old_pool, old_urlopen = proxy.POOL, accounts.urlopen
        old_parser = proxy.parse_rate_limit_reset
        proxy.POOL = Pool()
        accounts.urlopen = lambda *args, **kwargs: (_ for _ in ()).throw(error)
        if stub_parser:
            # Keep these tests on the model-cooldown path, independent of the
            # existing parser's timezone handling.
            proxy.parse_rate_limit_reset = lambda _detail: reset
        try:
            with self.assertRaises(proxy.RateLimited):
                proxy.open_upstream({"model": model, "messages": [
                    {"role": "user", "content": "hello"}]}, target_realm=target_realm)
        finally:
            proxy.POOL, accounts.urlopen = old_pool, old_urlopen
            proxy.parse_rate_limit_reset = old_parser
            error.close()
        return reset

    def cap_body(self, reset_wall, code=6004):
        """A 429 body whose reset clock resolves back to `reset_wall`.

        The stamp is the instant's UTC+8 wall clock (gmtime), so the shipped
        parser resolves it to the same instant on any host timezone - the same
        recipe the remaining-usage suite uses (cap_detail). In a DST zone the
        parser converts with the standard offset while mktime uses the summer
        one, so the stamp is corrected against the shipped parser before
        returning (the mapping is piecewise linear; one correction hits).
        """
        reset = int(reset_wall)
        offset = reset + 8 * 3600
        detail = ""
        for _ in range(3):
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(offset))
            detail = ('{"code":%d,"msg":"usage exceeds frequency limit, but don\'t '
                      'worry, your usage will reset at %s UTC+8, alternatively, you '
                      'can switch to the other models to continue using it."}'
                      % (code, stamp))
            parsed = proxy.parse_rate_limit_reset(detail)
            if parsed == reset:
                break
            offset += reset - parsed
        return detail

    def test_upstream_429_reaches_accounts_payload(self):
        account = self.account()
        # 429 会把撞线时刻持久化进凭证档（_save_account_caps），而这条用例的
        # 合成账号没有自己的文件——不隔离账号目录，它会往 checkout 的 accounts/
        # 落一份 synthetic-cn.json，并污染并行跑的 checkpoint 套件（那些套件的
        # 子进程按硬编码路径读到它，realm 指纹就和父进程对不上了）。
        directory = tempfile.mkdtemp(prefix="cap-save-")
        old_dir = proxy.ACCOUNTS_DIR
        proxy.ACCOUNTS_DIR = directory
        try:
            reset = self.drive_one_429(account)
        finally:
            proxy.ACCOUNTS_DIR = old_dir
        row = account.public()
        self.assertFalse(row["inCooldown"])
        self.assertEqual(row["modelCooldowns"][0]["model"], "glm-5.3")
        self.assertLess(abs(row["modelCooldowns"][0]["expiresAt"] - reset), 2)
        self.assertTrue(account.ready(model="another-model"))

    def test_a_model_429_records_the_moment_it_started(self):
        """面板要如实说「限流时刻 + 恢复时间」。

        model_cooldowns 只留恢复时刻，起始时刻以前被丢掉，禁用总览因此只能用
        「—」占位——把 60s 的短时 parking 和几小时的 6004 撞线画成同一副样子。
        这里钉住：非 6004 的模型级 429 也要记下起始时刻，并且冷却清掉后不留残影。
        """
        before = time.time()
        account = self._drive_unparsed(S1_BARE, "started-at")
        entry = account.public()["modelCooldowns"][0]
        self.assertIn("startedAt", entry, "模型级 429 要记下起始时刻")
        self.assertGreaterEqual(entry["startedAt"], before - 1)
        self.assertLessEqual(entry["startedAt"], time.time() + 1)
        self.assertLessEqual(entry["startedAt"], entry["expiresAt"])
        self.assertNotIn("cappedAt", entry, "临时限流没有撞线语义")
        # 清掉冷却后起始时刻不能留在快照里（否则下一行的「开始时间」会串）。
        account.note_success(model="glm-5.3")
        self.assertEqual(account.public()["modelCooldowns"], [])
        self.assertNotIn("glm-5.3", account.model_cooldowns_at)

    def test_a_6004_reports_the_cap_moment_as_the_start(self):
        """6004 的起始时刻就是上游撞线时刻，与 cappedAt 一致（同一瞬间）。"""
        account = accounts.Account({"uid": "synthetic-intl", "realm": "intl",
                                    "accessToken": "token"})
        directory = tempfile.mkdtemp(prefix="cap-start-")
        old_dir = proxy.ACCOUNTS_DIR
        proxy.ACCOUNTS_DIR = directory
        try:
            detail = self.cap_body(time.time() + 3600)
            self.drive_one_429(account, detail=detail, stub_parser=False,
                               model="deepseek-v4.1-flash-sg", target_realm="intl")
        finally:
            proxy.ACCOUNTS_DIR = old_dir
        entry = account.public()["modelCooldowns"][0]
        self.assertIn("cappedAt", entry)
        self.assertIn("startedAt", entry)
        self.assertLessEqual(abs(entry["startedAt"] - entry["cappedAt"]), 1)
        # keep_caps 只保留 cap 冷却，起始时刻跟着一起留下。
        account.clear_error(model="deepseek-v4.1-flash-sg", keep_caps=True)
        kept = account.public()["modelCooldowns"][0]
        self.assertEqual(kept["startedAt"], entry["startedAt"])
        # 不带 keep_caps 则两者一起清掉。
        account.clear_error(model="deepseek-v4.1-flash-sg")
        self.assertEqual(account.public()["modelCooldowns"], [])
        self.assertNotIn("deepseek-v4.1-flash-sg", account.model_cooldowns_at)

    def test_the_real_bare_429_parks_only_the_model(self):
        """S1 - 87% of the recorded 429s, no reset information at all.

        It used to be read as an account-level soft limit purely because there
        was no timestamp, which is how a realm ended up fully cooling while the
        panel test kept reporting every account healthy.
        """
        account = self._drive_unparsed(S1_BARE, "bare-shape")
        row = account.public()
        self.assertEqual(account.unscoped_streak, 1,
                         "the repeat is counted on the model ladder")
        self.assertEqual(account.soft_streak, 0,
                         "and never on the credential one")
        self.assertFalse(row["inCooldown"])
        self.assertIsNone(row["cooldownFor"])
        self.assertEqual([item["model"] for item in row["modelCooldowns"]], ["glm-5.3"])
        self.assertLessEqual(row["modelCooldowns"][0]["expiresAt"] - time.time(), 61,
                             "first tier is the short window, not 600s")
        self.assertTrue(account.ready(model="another-model"))

    def test_the_full_intl_14003_body_with_metadata_parks_only_the_model(self):
        """S2 as the gateway really sees it: the recorded prefix plus the
        metadata tail the log truncated (accountId / credentialId).

        Field names are not scope evidence - reading them as such put the
        600s -> 7200s credential ladder back on the most common intl shape.
        """
        self.assertIn('"accountId"', S2_INTL_FULL)
        self.assertIn('"credentialId"', S2_INTL_FULL)
        account = self._drive_unparsed(S2_INTL_FULL, "intl-14003-full")
        row = account.public()
        self.assertEqual(account.soft_streak, 0, "metadata is not scope evidence")
        self.assertFalse(row["inCooldown"])
        self.assertIsNone(row["cooldownFor"])
        self.assertEqual([item["model"] for item in row["modelCooldowns"]], ["glm-5.3"])
        self.assertTrue(account.ready(model="another-model"))

    def test_the_real_intl_14003_429_parks_only_the_model(self):
        """S2 - the intl body, recognised by code 14003 (and by its message)."""
        account = self._drive_unparsed(S2_INTL_14003, "intl-14003")
        row = account.public()
        self.assertEqual(account.soft_streak, 0)
        self.assertFalse(row["inCooldown"])
        self.assertIsNone(row["cooldownFor"])
        self.assertEqual([item["model"] for item in row["modelCooldowns"]], ["glm-5.3"])
        self.assertTrue(account.ready(model="another-model"))

    def test_a_parsed_intl_429_still_parks_until_its_clock(self):
        """S3 - the control: a named reset instant still drives a model park."""
        expected = proxy.parse_rate_limit_reset(S3_INTL_6004)
        self.assertEqual(expected, 1791291609)
        account = self._drive_unparsed(S3_INTL_6004, "intl-6004")
        row = account.public()
        self.assertEqual(account.soft_streak, 0, "a parsed clock is not a streak")
        self.assertFalse(row["inCooldown"])
        self.assertEqual([item["model"] for item in row["modelCooldowns"]], ["glm-5.3"])
        # The named instant is already past for this run, so the park is the 1s
        # floor - which is what tells it apart from the 60s unparsed window and
        # from the credential ladder.
        self.assertLessEqual(row["modelCooldowns"][0]["expiresAt"] - time.time(), 2)

    def _drive_unparsed(self, detail, tag):
        """Feed one real 429 body through open_upstream, parser left in place."""
        directory = tempfile.mkdtemp(prefix="real-429-%s-" % tag)
        old_dir = proxy.ACCOUNTS_DIR
        proxy.ACCOUNTS_DIR = directory
        try:
            account = self.account()
            self.drive_one_429(account, detail=detail, stub_parser=False)
            return account
        finally:
            proxy.ACCOUNTS_DIR = old_dir

    def test_repeated_real_429s_do_not_amplify_into_an_account_park(self):
        """S1 three times in a row used to mean 600 -> 1200 -> 2400s on the
        whole credential. It has to stay three short windows on one model."""
        directory = tempfile.mkdtemp(prefix="real-429-repeat-")
        old_dir = proxy.ACCOUNTS_DIR
        proxy.ACCOUNTS_DIR = directory
        windows = []
        try:
            account = self.account()
            for round_number in range(3):
                self.drive_one_429(account, detail=S1_BARE, stub_parser=False)
                self.assertIsNone(account.public()["cooldownFor"],
                                  "round %d parked the credential" % round_number)
                self.assertTrue(account.ready(model="another-model"),
                                "round %d took a sibling model down" % round_number)
                windows.append(account.model_cooldowns["glm-5.3"] - time.time())
                # Let the window lapse, the way it does in production when the
                # same shape comes back; the streak is what must survive.
                with account._throttle_lock:
                    account.model_cooldowns["glm-5.3"] = time.time() - 1
        finally:
            proxy.ACCOUNTS_DIR = old_dir
        self.assertEqual(account.unscoped_streak, 3)
        self.assertEqual(account.soft_streak, 0, "the credential counter is untouched")
        self.assertLessEqual(windows[0], 61, "first tier stays short")
        self.assertLess(windows[0], windows[1], "repetition escalates")
        self.assertLess(windows[1], windows[2], "repetition keeps escalating")
        self.assertLessEqual(windows[2], 241, "and stays far below the 2h ceiling")

    def test_unscoped_hits_do_not_lift_the_credential_ladder(self):
        """Four unscoped S1s, then one credential-scoped body, end to end.

        Sharing one counter made this sequence start the credential ladder at
        600 * 2**4 - the 7200s ceiling - instead of at 600s.
        """
        directory = tempfile.mkdtemp(prefix="mixed-seq-")
        old_dir = proxy.ACCOUNTS_DIR
        proxy.ACCOUNTS_DIR = directory
        try:
            account = self.account()
            for _ in range(4):
                self.drive_one_429(account, detail=S1_BARE, stub_parser=False)
                with account._throttle_lock:
                    account.model_cooldowns["glm-5.3"] = time.time() - 1
            self.assertEqual(account.unscoped_streak, 4)
            self.assertEqual(account.soft_streak, 0)

            self.drive_one_429(account,
                               detail='{"msg":"too many requests for this account"}',
                               stub_parser=False)
        finally:
            proxy.ACCOUNTS_DIR = old_dir
        self.assertEqual(account.soft_streak, 1)
        self.assertEqual(account.unscoped_streak, 4, "the other ladder is untouched")
        self.assertLessEqual(
            account.cooldown_until - time.time(), accounts.SOFT_RATE_BASE + 1,
            "the credential ladder starts at its own base, not at the ceiling")
        self.assertEqual([item["model"] for item in account.public()["modelCooldowns"]],
                         [], "and adds no model window")

    def test_the_auto_switch_setting_is_opt_in(self):
        """Off on a fresh install, and only a real JSON boolean turns it on."""
        directory = tempfile.mkdtemp(prefix="auto-switch-setting-")
        self.assertFalse(settings.auto_switch_product(directory))
        self.assertTrue(settings.set_auto_switch_product(directory, True))
        self.assertTrue(settings.auto_switch_product(directory))
        self.assertFalse(settings.set_auto_switch_product(directory, False))
        self.assertFalse(settings.auto_switch_product(directory))
        with open(settings.settings_path(directory), "w", encoding="utf-8") as fh:
            json.dump({"auto_switch_product": "false"}, fh)
        self.assertFalse(settings.auto_switch_product(directory),
                         "a hand-edited string must not read as enabled")

    def test_auto_switch_products_defaults_and_validates(self):
        """Allowed identities: all three by default; unknown entries rejected."""
        directory = tempfile.mkdtemp(prefix="auto-switch-products-")
        self.assertEqual(settings.auto_switch_products(directory),
                         ["workbuddy", "vscode", "cli"])
        self.assertEqual(
            settings.set_auto_switch_products(directory, ["vscode", "cli"]),
            ["vscode", "cli"])
        self.assertEqual(settings.auto_switch_products(directory),
                         ["vscode", "cli"])
        self.assertEqual(
            settings.set_auto_switch_products(directory, ["vscode", "vscode"]),
            ["vscode"], "duplicates are collapsed")
        with self.assertRaises(ValueError):
            settings.set_auto_switch_products(directory, ["bogus"])
        with self.assertRaises(ValueError):
            settings.set_auto_switch_products(directory, "vscode")
        with open(settings.settings_path(directory), "w", encoding="utf-8") as fh:
            json.dump({"auto_switch_products": ["bogus", 7, "vscode"]}, fh)
        self.assertEqual(settings.auto_switch_products(directory), ["vscode"],
                         "hand-edited garbage is filtered, not trusted")

    def test_a_429_keeps_the_identity_while_the_setting_is_off(self):
        directory = tempfile.mkdtemp(prefix="auto-switch-off-")
        old_dir = proxy.ACCOUNTS_DIR
        proxy.ACCOUNTS_DIR = directory
        proxy._SWITCH_LOG.clear()
        try:
            account = self.account()
            self.drive_one_429(account)
            self.assertEqual(account.product, "workbuddy")
            self.assertEqual(proxy._SWITCH_LOG, {})
        finally:
            proxy.ACCOUNTS_DIR = old_dir
            proxy._SWITCH_LOG.clear()

    def test_a_429_rotates_the_identity_once_the_setting_is_on(self):
        directory = tempfile.mkdtemp(prefix="auto-switch-on-")
        settings.set_auto_switch_product(directory, True)
        old_dir = proxy.ACCOUNTS_DIR
        old_budget = proxy.MAX_PRODUCT_SWITCHES
        proxy.ACCOUNTS_DIR = directory
        # One switch makes the assertion exact and independent of how large the
        # real budget is (an even number of rotations ends back at workbuddy).
        proxy.MAX_PRODUCT_SWITCHES = 1
        proxy._SWITCH_LOG.clear()
        try:
            account = self.account()
            self.drive_one_429(account)
            self.assertEqual(account.product, "vscode")
            self.assertTrue(proxy._SWITCH_LOG)
        finally:
            proxy.ACCOUNTS_DIR = old_dir
            proxy.MAX_PRODUCT_SWITCHES = old_budget
            proxy._SWITCH_LOG.clear()

    def test_a_6004_keeps_the_disable_across_an_identity_switch(self):
        """端到端：auto-switch 开着时，撞线后账号仍然处于禁用。

        429 分支的顺序是「记 cap（挂模型冷却 + 写凭证档）→ 换身分」，换身分
        会走 set_product/clear_error 的清理路径；这里钉住整条链结束时 cap 与
        冷却都还在（真机 2026-10-10 的四个撞线账号在切换后都被清成了可用）。
        """
        directory = tempfile.mkdtemp(prefix="cap-switch-")
        settings.set_auto_switch_product(directory, True)
        old_dir = proxy.ACCOUNTS_DIR
        old_budget = proxy.MAX_PRODUCT_SWITCHES
        proxy.ACCOUNTS_DIR = directory
        proxy.MAX_PRODUCT_SWITCHES = 1
        proxy._SWITCH_LOG.clear()
        try:
            account = self.account()
            reset = time.time() + 600
            self.drive_one_429(account, detail=self.cap_body(reset),
                               model="hy4-preview-f", target_realm="cn")
            # 身分换了（auto-switch 生效）……
            self.assertEqual(account.product, "vscode")
            self.assertTrue(proxy._SWITCH_LOG)
            # ……但禁用必须留下：cap 记录与模型冷却都还在。
            self.assertIn("hy4-preview-f", account.model_caps)
            self.assertFalse(account.ready(model="hy4-preview-f"))
            self.assertEqual([c["model"] for c in account.public()["modelCooldowns"]],
                             ["hy4-preview-f"])
        finally:
            proxy.ACCOUNTS_DIR = old_dir
            proxy.MAX_PRODUCT_SWITCHES = old_budget
            proxy._SWITCH_LOG.clear()

    def test_switching_the_identity_keeps_a_cap_backed_disable(self):
        """set_product 的清理不能把 6004 的禁用一起清掉。

        这是真机 bug 的直接钉法：换身分（无论谁调）只清限流窗口，恢复期内
        的 cap 禁用跨身分成立。
        """
        account = self.account()
        account.note_model_cap("hy4-preview-f", time.time() + 600)
        account.note_error("429", model="glm-5.3", until=time.time() + 60)
        self.assertTrue(account.set_product("vscode"))
        self.assertFalse(account.ready(model="hy4-preview-f"),
                         "换身分不能解除上游额度用满的禁用")
        self.assertTrue(account.ready(model="glm-5.3"),
                        "普通限流窗口照清")

    def test_clear_error_keep_caps_spares_only_cap_backed_cooldowns(self):
        account = self.account()
        now = time.time()
        account.note_model_cap("hy4-preview-f", now + 600)
        account.note_error("429", model="glm-5.3", until=now + 60)
        account.clear_error(keep_caps=True)
        self.assertFalse(account.ready(model="hy4-preview-f"),
                         "上游额度用满的禁用要留下")
        self.assertTrue(account.ready(model="glm-5.3"),
                        "普通限流窗口照清")
        account.clear_error()
        self.assertTrue(account.ready(model="hy4-preview-f"),
                        "不带 keep_caps 时仍然整片清掉")

    def test_a_repeated_6004_rearms_a_cleared_cooldown(self):
        account = self.account()
        reset = time.time() + 600
        self.assertTrue(account.note_model_cap("hy4-preview-f", reset))
        account.clear_error()
        self.assertTrue(account.ready(model="hy4-preview-f"))
        # 同一个窗口的第二次 6004：不用重写凭证档（返回 False），但冷却要挂
        # 回来——否则这个窗口就一直不会被禁用。
        self.assertFalse(account.note_model_cap("hy4-preview-f", reset))
        self.assertFalse(account.ready(model="hy4-preview-f"))

    # ------------------------------------------------------------------
    # 限额联动：6004 → 记录（撞线/恢复时刻）→ 跨重启禁用，仅限免费模型
    # ------------------------------------------------------------------

    def test_the_free_cap_scope_is_the_confirmed_model_lists(self):
        """范围是用户确认过的口径：intl 三个免费模型，cn 两个（deepseek 收费）。"""
        self.assertEqual(sorted(accounts.FREE_CAP_MODELS["intl"]),
                         ["deepseek-v4.1-flash", "deepseek-v4.1-flash-sg", "hy3", "hy4-preview-f"])
        self.assertEqual(sorted(accounts.FREE_CAP_MODELS["cn"]), ["hy3", "hy4-preview-f"])
        self.assertTrue(accounts.is_free_cap_model("intl", "deepseek-v4.1-flash"))
        self.assertTrue(accounts.is_free_cap_model("intl", "deepseek-v4.1-flash-sg"))
        self.assertTrue(accounts.is_free_cap_model("cn", "hy4-preview-f"))
        self.assertFalse(accounts.is_free_cap_model("cn", "deepseek-v4.1-flash"),
                         "国内版的 deepseek-v4.1-flash 是收费模型（x0.11）")
        self.assertFalse(accounts.is_free_cap_model("intl", "hy4-preview"),
                         "hy4-preview 是付费变体（x0.29）")
        self.assertFalse(accounts.is_free_cap_model("intl", "glm-5.3"))
        self.assertFalse(accounts.is_free_cap_model("", "hy3"), "未知区域失败关闭")
        self.assertFalse(accounts.is_free_cap_model("intl", ""))

    def test_note_model_cap_only_records_models_in_the_free_scope(self):
        cn = self.account()
        intl = accounts.Account({"uid": "synthetic-intl", "realm": "intl",
                                 "accessToken": "token"})
        now = time.time()
        # 范围外：付费模型、以及国内版不算免费的 deepseek
        self.assertFalse(cn.note_model_cap("glm-5.3", now + 600))
        self.assertFalse(cn.note_model_cap("deepseek-v4.1-flash", now + 600))
        self.assertEqual(cn.model_caps, {})
        # 范围内：两区都免费的两个 + intl 的 deepseek
        self.assertTrue(cn.note_model_cap("hy3", now + 600))
        self.assertTrue(intl.note_model_cap("deepseek-v4.1-flash", now + 600))
        # 同一个窗口重复撞线不算新记录（避免每次都重写凭证档）
        self.assertFalse(cn.note_model_cap("hy3", now + 600))
        # 没有恢复时刻可记的 6004 也不写
        self.assertFalse(cn.note_model_cap("hy4-preview-f", 0))
        self.assertNotIn("hy4-preview-f", cn.model_caps)

    def test_a_stored_out_of_scope_cap_is_dropped_and_not_restored(self):
        """旧版本存下的范围外 cap 在加载时丢弃，也不会被 restore 重新挂上。"""
        now = time.time()
        account = accounts.Account({
            "uid": "synthetic-cn", "realm": "cn", "accessToken": "token",
            "modelCaps": {
                "glm-5.3": {"at": now, "reset": now + 600},
                "hy3": {"at": now, "reset": now + 600},
            },
        })
        self.assertEqual(list(account.model_caps), ["hy3"])
        self.assertEqual(account.restore_model_caps(), ["hy3"])
        self.assertIn("hy3", account.model_cooldowns)
        self.assertNotIn("glm-5.3", account.model_cooldowns)
        self.assertFalse(account.ready(model="hy3"))
        self.assertTrue(account.ready(model="glm-5.3"))
        with mock.patch.object(accounts.time, "time", return_value=now + 601):
            self.assertTrue(account.ready(model="hy3"), "恢复时间过后自动解除")

    def test_a_6004_on_a_free_model_survives_a_restart_and_then_expires(self):
        """整条链：6004 → cap 写盘 → 重启（新建池 + load）仍在禁用 → 到点解除。"""
        directory = tempfile.mkdtemp(prefix="cap-restart-")
        old_dir = proxy.ACCOUNTS_DIR
        proxy.ACCOUNTS_DIR = directory
        try:
            detail = self.cap_body(time.time() + 3600)
            reset = proxy.parse_rate_limit_reset(detail)
            self.assertIsNotNone(reset, "夹具的 6004 必须带可解析的重置时刻")
            account = accounts.Account({"uid": "synthetic-intl", "realm": "intl",
                                        "accessToken": "token"})
            started = time.time()
            self.drive_one_429(account, detail=detail, stub_parser=False,
                               model="deepseek-v4.1-flash-sg", target_realm="intl")

            cap = account.model_caps["deepseek-v4.1-flash-sg"]
            self.assertLessEqual(abs(cap["reset"] - reset), 2)
            self.assertGreaterEqual(cap["at"], started - 1)
            self.assertLessEqual(cap["at"], time.time())
            # 面板显示的数据源：冷却条目带 cappedAt（撞线时刻）
            entry = next(item for item in account.public()["modelCooldowns"]
                         if item["model"] == "deepseek-v4.1-flash-sg")
            self.assertLessEqual(abs(entry["cappedAt"] - cap["at"]), 2)

            # 持久化：凭证档里带着 modelCaps（RUNTIME_ONLY_FIELDS 不带它）
            saved_path = os.path.join(directory, "synthetic-intl.json")
            self.assertTrue(os.path.exists(saved_path))
            with open(saved_path, encoding="utf-8") as fh:
                saved = json.load(fh)
            self.assertIn("deepseek-v4.1-flash-sg", saved.get("modelCaps") or {})

            # 重启 = 新建池 + load()：恢复时间内仍在禁用，选择逻辑跳过该模型
            pool = accounts.AccountPool(directory)
            loaded = pool.load()
            self.assertEqual(len(loaded), 1)
            revived = loaded[0]
            self.assertGreaterEqual(revived.model_cooldowns.get("deepseek-v4.1-flash-sg", 0.0),
                                    reset - 2)
            self.assertFalse(revived.ready(model="deepseek-v4.1-flash-sg"))
            self.assertTrue(revived.ready(model="glm-5.3"), "同账号其他模型不受影响")
            self.assertIsNone(pool.pick(realm="intl", model="deepseek-v4.1-flash-sg"),
                              "重启后选择逻辑仍跳过被禁用的模型")
            self.assertEqual(pool.pick(realm="intl", model="glm-5.3").uid, "synthetic-intl")
            revived_row = next(item for item in revived.public()["modelCooldowns"]
                               if item["model"] == "deepseek-v4.1-flash-sg")
            self.assertIn("cappedAt", revived_row, "重启加载后面板仍能显示撞线时间")
            # 起始时刻是运行时态，重启即清零；撞线时刻另有持久化的 cap 兜底，
            # 面板的「开始时间」列因此仍有值（这是刻意的分工，不是缺失）。
            self.assertNotIn("startedAt", revived_row,
                             "startedAt 是运行时态，不该跨重启")

            # 恢复时间过了以后自动解除
            with mock.patch.object(accounts.time, "time", return_value=reset + 1):
                self.assertTrue(revived.ready(model="deepseek-v4.1-flash-sg"))
                self.assertEqual(pool.pick(realm="intl", model="deepseek-v4.1-flash-sg").uid,
                                 "synthetic-intl")
        finally:
            proxy.ACCOUNTS_DIR = old_dir

    def test_a_6004_outside_the_scope_is_never_recorded(self):
        """范围外的模型（付费、或另一区域的免费模型）收到 6004 也不记 cap。

        既有的运行时冷却不受影响：429 带时钟时仍停到时钟（这条语义有测试钉住，
        不随限额联动改变）。

        #668 之后有一处变化：**面板**要分得清「配额用满」与「临时限流」，所以
        撞线时刻（cappedAt）对任何模型都给。但限额联动的范围不变——下面两行
        仍然成立：凭证档的 modelCaps 仍为空、凭证档仍不写盘（重启后靠窗口
        记录恢复，而不是靠持久化禁用）。
        """
        for realm, model in (("cn", "glm-5.3"), ("cn", "deepseek-v4.1-flash"),
                             ("intl", "glm-5.3")):
            directory = tempfile.mkdtemp(prefix="cap-scope-%s-" % realm)
            old_dir = proxy.ACCOUNTS_DIR
            proxy.ACCOUNTS_DIR = directory
            try:
                detail = self.cap_body(time.time() + 3600)
                reset = proxy.parse_rate_limit_reset(detail)
                account = accounts.Account({"uid": "synthetic-%s" % realm,
                                            "realm": realm, "accessToken": "token"})
                self.drive_one_429(account, detail=detail, stub_parser=False,
                                   model=model, target_realm=realm)
                self.assertEqual(account.model_caps, {},
                                 "%s/%s 不在限额联动范围内" % (realm, model))
                row = account.public()["modelCooldowns"][0]
                self.assertEqual(row["model"], model)
                # 面板据此写「该模型配额耗尽（code 6004）」而不是「临时限流」。
                self.assertIn("cappedAt", row)
                self.assertLessEqual(abs(row["expiresAt"] - reset), 2,
                                     "运行时仍按上游时钟停到点")
                self.assertFalse(os.path.exists(os.path.join(
                    directory, "synthetic-%s.json" % realm)),
                    "不写盘：重启后自动放开")
            finally:
                proxy.ACCOUNTS_DIR = old_dir

    def test_only_code_6004_counts_as_a_cap(self):
        """「达到限额」的唯一标志是 6004：带时钟的 14003 也不是额度判定。"""
        self.assertTrue(proxy.rate_limit_is_cap(S3_INTL_6004))
        self.assertTrue(proxy.rate_limit_is_cap('{"code":"6004","msg":"x"}'),
                        "字符串形式的 code 不能把真 cap 漏掉")
        self.assertFalse(proxy.rate_limit_is_cap(S1_BARE))
        self.assertFalse(proxy.rate_limit_is_cap(S2_INTL_14003))
        self.assertFalse(proxy.rate_limit_is_cap(""))
        self.assertFalse(proxy.rate_limit_is_cap('{"code":null}'))

        directory = tempfile.mkdtemp(prefix="cap-code-")
        old_dir = proxy.ACCOUNTS_DIR
        proxy.ACCOUNTS_DIR = directory
        try:
            detail = self.cap_body(time.time() + 3600, code=14003)
            reset = proxy.parse_rate_limit_reset(detail)
            self.assertIsNotNone(reset, "夹具必须带可解析的重置时刻")
            account = accounts.Account({"uid": "synthetic-cn", "realm": "cn",
                                        "accessToken": "token"})
            # hy3 在范围内——挡住这条记录的是 code 判据本身，不是范围。
            self.drive_one_429(account, detail=detail, stub_parser=False, model="hy3")
            self.assertEqual(account.model_caps, {}, "非 6004 不记 cap")
            row = account.public()["modelCooldowns"][0]
            self.assertEqual(row["model"], "hy3")
            self.assertNotIn("cappedAt", row)
            self.assertLessEqual(abs(row["expiresAt"] - reset), 2)
            self.assertFalse(os.path.exists(os.path.join(directory, "synthetic-cn.json")))
        finally:
            proxy.ACCOUNTS_DIR = old_dir


if __name__ == "__main__":
    unittest.main()
