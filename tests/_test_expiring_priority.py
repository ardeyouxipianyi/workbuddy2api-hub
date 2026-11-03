"""临期积分优先分派：快到期的积分先被消耗。

分派原本是纯轮询。加了这条偏好之后，活跃积分包进入窗口（默认 7 天）的账号先
被交出去，窗口内按紧迫度加权轮询——越接近到期分到的流量越多，但没人独占；
窗口外的账号只在窗口内无人可用时兜底。既要证明「临期账号确实被优先」，也要
证明「没有临期账号时行为与原来完全一致」，还要证明「看不懂的积分数据不会被
当成临期」。

到期语义按实测校正：认 DeductionEndTime（抵扣截止时间）而不是 CycleEndTime
（计费周期结束）。免费包/体验版这两个字段能差 8 年，只认周期结束会让这些账号
每到月底都被误判成临期。

Run with the current interpreter (python tests/run_all.py expiring).
"""
import datetime
import os
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_settings


def make_account(uid, realm="cn", credits=None, **extra):
    data = {"uid": uid, "accessToken": "token-%s" % uid, "realm": realm}
    if credits is not None:
        data["credits"] = credits
    data.update(extra)
    return wb_accounts.Account(data)


def credits(*days, package_code="package", remain=100, no_expiry=False):
    """A credits blob whose packages expire in the given days.

    A None day stands for a package the upstream gave no end date for.
    """
    packages = []
    for index, day in enumerate(days):
        packages.append({
            "name": "pkg%d" % index,
            "package_code": package_code,
            "remain": remain,
            "used": 0,
            "size": remain,
            "days_left": day,
            "no_expiry": no_expiry,
            "is_expired": day is not None and day < 0,
        })
    total = remain * len(packages)
    return {"remain": total, "used": 0, "size": total, "packages": packages}


def _local_text(offset_days, base=None):
    stamp = (base or datetime.datetime.now()) + datetime.timedelta(days=offset_days)
    return stamp.strftime("%Y-%m-%d %H:%M:%S")


def _epoch_ms(offset_days, base=None):
    stamp = (base or datetime.datetime.now()) + datetime.timedelta(days=offset_days)
    return int(stamp.timestamp() * 1000)


def raw_package(cycle_end, deduction_end=None, size=100, remain=100, code="pkg"):
    acc = {
        "PackageName": "P", "PackageCode": code,
        "CycleCapacitySize": size, "CycleCapacityRemain": remain,
        "CycleCapacityUsed": size - remain,
        "CycleEndTime": cycle_end,
    }
    if deduction_end is not None:
        acc["DeductionEndTime"] = deduction_end
    return acc


def built_package(cycle_end, deduction_end):
    """A package the way _parse_package_account would have built it."""
    return make_account("helper")._parse_package_account(
        raw_package(cycle_end=cycle_end, deduction_end=deduction_end))


class ExpiringWindowSettingTests(unittest.TestCase):
    def test_default_is_seven_days(self):
        with tempfile.TemporaryDirectory(prefix="expiry-setting-") as directory:
            self.assertEqual(wb_settings.expiring_window_days(directory), 7)
            snapshot = wb_settings.limits_snapshot(directory)
            self.assertEqual(snapshot["expiring_window_days"]["global"], 7)

    def test_round_trip_and_explicit_off_survives_reread(self):
        with tempfile.TemporaryDirectory(prefix="expiry-setting-") as directory:
            self.assertEqual(wb_settings.set_expiring_window_days(directory, 0), 0)
            # 显式写 0 表示「关」，重读时不能被默认值 7 顶回来。
            self.assertEqual(wb_settings.expiring_window_days(directory), 0)
            self.assertEqual(wb_settings.set_expiring_window_days(directory, 3), 3)
            self.assertEqual(wb_settings.expiring_window_days(directory), 3)

    def test_other_guards_still_default_to_off(self):
        with tempfile.TemporaryDirectory(prefix="expiry-setting-") as directory:
            self.assertEqual(wb_settings.reserve_credits(directory), 0)
            self.assertEqual(wb_settings.daily_credit_limit(directory), 0)
            self.assertEqual(wb_settings.model_daily_token_limit(directory), 0)


class CreditsRefreshSettingTests(unittest.TestCase):
    def test_default_is_twelve_hours(self):
        with tempfile.TemporaryDirectory(prefix="refresh-setting-") as directory:
            self.assertEqual(wb_settings.credits_refresh_hours(directory), 12.0)

    def test_round_trip(self):
        with tempfile.TemporaryDirectory(prefix="refresh-setting-") as directory:
            self.assertEqual(wb_settings.set_credits_refresh_hours(directory, 24), 24.0)
            self.assertEqual(wb_settings.credits_refresh_hours(directory), 24.0)
            self.assertEqual(wb_settings.set_credits_refresh_hours(directory, 0), 0.0)
            self.assertEqual(wb_settings.credits_refresh_hours(directory), 0.0)

    def test_junk_and_negative_fall_back_to_the_default(self):
        with tempfile.TemporaryDirectory(prefix="refresh-setting-") as directory:
            data = wb_settings.load(directory)
            data[wb_settings.CREDITS_REFRESH_HOURS_KEY] = "not a number"
            wb_settings.save(directory, data)
            self.assertEqual(wb_settings.credits_refresh_hours(directory), 12.0)

            data = wb_settings.load(directory)
            data[wb_settings.CREDITS_REFRESH_HOURS_KEY] = -5
            wb_settings.save(directory, data)
            self.assertEqual(wb_settings.credits_refresh_hours(directory), 12.0)


class ExpirySemanticsTests(unittest.TestCase):
    """到期认 DeductionEndTime，不认 CycleEndTime（实测校正）。"""

    def test_deduction_end_wins_over_the_cycle_end(self):
        account = make_account("s1")
        package = account._parse_package_account(raw_package(
            cycle_end=_local_text(20), deduction_end=_epoch_ms(3)))
        self.assertAlmostEqual(package["days_left"], 3.0, delta=0.2)
        self.assertFalse(package["no_expiry"])

    def test_a_far_future_deduction_end_means_no_expiry(self):
        # 免费包/体验版：周期月底就结束，抵扣截止却远在 8 年后。
        account = make_account("s2")
        package = account._parse_package_account(raw_package(
            cycle_end=_local_text(23), deduction_end=_epoch_ms(365 * 8)))
        self.assertTrue(package["no_expiry"])
        self.assertIsNone(package["days_left"])
        self.assertFalse(package["is_expired"])

    def test_missing_deduction_end_falls_back_to_the_cycle_end(self):
        account = make_account("s3")
        package = account._parse_package_account(raw_package(
            cycle_end=_local_text(4)))
        self.assertAlmostEqual(package["days_left"], 4.0, delta=0.2)
        self.assertFalse(package["no_expiry"])

    def test_the_free_plan_no_longer_looks_urgent(self):
        # 这条修正的实际收益：月底的免费包不该让账号进临期窗口。
        account = make_account("s4", credits={
            "remain": 100, "used": 0, "size": 100,
            "packages": [built_package(_local_text(23), _epoch_ms(365 * 8))],
        })
        account.expiring_window_days = 7
        self.assertFalse(account.in_expiring_window())

    def test_soonest_expiring_days_skips_no_expiry_packages(self):
        account = make_account("s5", credits={
            "remain": 200, "used": 0, "size": 200,
            "packages": [
                built_package(_local_text(23), _epoch_ms(365 * 8)),
                built_package(_local_text(5), _epoch_ms(5)),
            ],
        })
        self.assertAlmostEqual(account.soonest_expiring_days(), 5.0, delta=0.2)


class SoonestExpiringTests(unittest.TestCase):
    def test_picks_the_earliest_live_package(self):
        account = make_account("u1", credits=credits(9, 2.5, 4))
        self.assertEqual(account.soonest_expiring_days(), 2.5)

    def test_skips_expired_and_empty_packages(self):
        blob = credits(-1, 3)
        blob["packages"][1]["remain"] = 0
        account = make_account("u2", credits=blob)
        self.assertIsNone(account.soonest_expiring_days())

    def test_skips_the_enterprise_quota(self):
        # 企业额度的 cycleEndTime 是周期重置（额度回满），不是积分作废。
        account = make_account("u3", credits=credits(1, package_code="enterprise"))
        self.assertIsNone(account.soonest_expiring_days())

    def test_skips_packages_marked_no_expiry(self):
        account = make_account("u3b", credits=credits(1, no_expiry=True))
        self.assertIsNone(account.soonest_expiring_days())

    def test_unknown_data_is_not_a_preference(self):
        self.assertIsNone(make_account("u4").soonest_expiring_days())
        self.assertIsNone(make_account("u5", credits={}).soonest_expiring_days())
        self.assertIsNone(make_account("u6", credits=credits(None)).soonest_expiring_days())


class InWindowTests(unittest.TestCase):
    def test_window_boundaries(self):
        account = make_account("w1", credits=credits(7))
        account.expiring_window_days = 7
        self.assertTrue(account.in_expiring_window())
        account.expiring_window_days = 6
        self.assertFalse(account.in_expiring_window())

    def test_window_off_and_unknown_are_never_urgent(self):
        off = make_account("w2", credits=credits(1))
        off.expiring_window_days = 0
        self.assertFalse(off.in_expiring_window())

        unknown = make_account("w3")
        unknown.expiring_window_days = 7
        self.assertFalse(unknown.in_expiring_window())


class PickPrefersExpiringTests(unittest.TestCase):
    def _pool(self, accounts, window=7):
        directory = tempfile.mkdtemp(prefix="expiry-pool-")
        pool = wb_accounts.AccountPool(directory, log=lambda _m: None)
        pool.accounts = accounts
        pool.apply_expiring_window({"global": window, "intl": window, "cn": window})
        return pool

    def test_window_account_takes_every_pick(self):
        soon = make_account("soon", credits=credits(2))
        later = make_account("later", credits=credits(40))
        pool = self._pool([later, soon])
        self.assertEqual({pool.pick(realm="cn").uid for _ in range(6)}, {"soon"})

    def test_same_day_accounts_still_take_turns(self):
        # 同一天到期的两个账号之间必须轮询，否则一拨流量会全压在单个账号上。
        first = make_account("same-a", credits=credits(3.2))
        second = make_account("same-b", credits=credits(3.8))
        pool = self._pool([first, second])
        picked = [pool.pick(realm="cn").uid for _ in range(4)]
        self.assertEqual(sorted(set(picked)), ["same-a", "same-b"])
        self.assertEqual(picked, ["same-a", "same-b", "same-a", "same-b"])

    def test_no_window_account_keeps_the_plain_round_robin(self):
        a = make_account("plain-a", credits=credits(40))
        b = make_account("plain-b", credits=credits(50))
        pool = self._pool([a, b])
        picked = [pool.pick(realm="cn").uid for _ in range(4)]
        self.assertEqual(sorted(set(picked)), ["plain-a", "plain-b"])

    def test_window_off_keeps_the_plain_round_robin(self):
        soon = make_account("off-soon", credits=credits(1))
        later = make_account("off-later", credits=credits(40))
        pool = self._pool([soon, later], window=0)
        picked = {pool.pick(realm="cn").uid for _ in range(4)}
        self.assertEqual(picked, {"off-soon", "off-later"})

    def test_unavailable_window_account_falls_back_to_the_pool(self):
        # 临期账号这一刻被保留积分挡住时，请求要落到普通账号上，而不是空转。
        parked = make_account("parked", credits=credits(2))
        parked.reserve_credits = 1000
        later = make_account("spare", credits=credits(40))
        pool = self._pool([parked, later])
        self.assertEqual({pool.pick(realm="cn").uid for _ in range(4)}, {"spare"})

    def test_excluded_window_account_falls_through(self):
        soon = make_account("excl-soon", credits=credits(1))
        later = make_account("excl-later", credits=credits(40))
        pool = self._pool([soon, later])
        self.assertEqual(pool.pick(realm="cn", exclude={"excl-soon"}).uid, "excl-later")

    def test_realms_are_ranked_separately(self):
        # 国际版临期账号只在国际版里优先，不影响国内版。
        intl_soon = make_account("intl-soon", realm="intl", credits=credits(1))
        intl_later = make_account("intl-later", realm="intl", credits=credits(40))
        cn_later = make_account("cn-later", realm="cn", credits=credits(40))
        pool = self._pool([intl_soon, intl_later, cn_later])
        self.assertEqual({pool.pick(realm="intl").uid for _ in range(4)}, {"intl-soon"})
        self.assertEqual({pool.pick(realm="cn").uid for _ in range(4)}, {"cn-later"})


class LoadSpreadTests(unittest.TestCase):
    """窗口内不再由最早到期的账号独占。"""

    def _pool(self, accounts, window=7):
        directory = tempfile.mkdtemp(prefix="expiry-spread-")
        pool = wb_accounts.AccountPool(directory, log=lambda _m: None)
        pool.accounts = accounts
        pool.apply_expiring_window({"global": window, "intl": window, "cn": window})
        return pool

    def test_the_sooner_account_gets_the_larger_share_not_everything(self):
        sooner = make_account("sooner", credits=credits(1))
        later = make_account("later", credits=credits(6))
        pool = self._pool([sooner, later])
        picks = [pool.pick(realm="cn").uid for _ in range(60)]
        sooner_count = picks.count("sooner")
        later_count = picks.count("later")
        self.assertGreater(later_count, 0, "较晚到期的账号也要分到流量，否则就是独占")
        self.assertGreater(sooner_count, later_count, "越接近到期应分到更多流量")
        self.assertGreater(sooner_count, later_count * 1.5)

    def test_a_lone_window_account_still_serves_everything(self):
        # 窗口内只有一个账号时它当然全接——负载分散是窗口内的事。
        only = make_account("only", credits=credits(1))
        outside = make_account("outside", credits=credits(60))
        pool = self._pool([only, outside])
        self.assertEqual({pool.pick(realm="cn").uid for _ in range(6)}, {"only"})


class SessionAffinityTests(unittest.TestCase):
    def _pool(self, accounts):
        directory = tempfile.mkdtemp(prefix="expiry-affinity-")
        pool = wb_accounts.AccountPool(directory, log=lambda _m: None)
        pool.accounts = accounts
        pool.apply_expiring_window({"global": 7, "intl": 7, "cn": 7})
        return pool

    def test_a_bound_session_keeps_its_account(self):
        # 会话亲和优先：已经绑定的账号不会被临期账号抢走。
        bound = make_account("bound", credits=credits(40))
        soon = make_account("bound-soon", credits=credits(1))
        pool = self._pool([bound, soon])
        pool.affinity.bind("s1", "bound")
        for _ in range(4):
            self.assertEqual(
                pool.pick_for_session(realm="cn", session_key="s1").uid, "bound")

    def test_a_fresh_session_lands_on_the_window_account(self):
        bound = make_account("fresh-later", credits=credits(40))
        soon = make_account("fresh-soon", credits=credits(1))
        pool = self._pool([bound, soon])
        self.assertEqual(
            pool.pick_for_session(realm="cn", session_key="s2").uid, "fresh-soon")


class ApplyWindowTests(unittest.TestCase):
    def test_pool_copies_the_setting_onto_every_account(self):
        with tempfile.TemporaryDirectory(prefix="expiry-apply-") as directory:
            wb_settings.set_expiring_window_days(directory, 3)
            pool = wb_accounts.AccountPool(directory, log=lambda _m: None)
            pool.accounts = [make_account("a1"), make_account("a2", realm="intl")]
            pool.apply_expiring_window()
            self.assertEqual([a.expiring_window_days for a in pool.accounts], [3, 3])

    def test_load_applies_the_window(self):
        with tempfile.TemporaryDirectory(prefix="expiry-load-") as directory:
            wb_settings.set_expiring_window_days(directory, 5)
            account = make_account("loaded", credits=credits(4))
            account.path = os.path.join(directory, "loaded.json")
            account.save(directory)
            pool = wb_accounts.AccountPool(directory, log=lambda _m: None)
            pool.load()
            self.assertEqual(pool.accounts[0].expiring_window_days, 5)
            self.assertTrue(pool.accounts[0].in_expiring_window())


class CreditsRefresherTests(unittest.TestCase):
    def _pool_with(self, accounts, hours=6):
        directory = tempfile.mkdtemp(prefix="expiry-refresh-")
        wb_settings.set_credits_refresh_hours(directory, hours)
        pool = wb_accounts.AccountPool(directory, log=lambda _m: None)
        pool.accounts = accounts
        return directory, pool

    def _account(self, uid, age_seconds, fetch=None):
        if age_seconds is None:
            blob = None
        else:
            blob = {"remain": 1, "used": 0, "size": 1, "packages": [],
                    "updated_at": time.time() - age_seconds}
        account = make_account(uid, credits=blob)
        if fetch is not None:
            account.fetch_credits = fetch
        return account

    def test_picks_the_stalest_account_past_the_ttl(self):
        fresh = self._account("fresh", 60)
        old = self._account("old", 7 * 3600)
        _, pool = self._pool_with([fresh, old])
        refresher = wb_accounts.CreditsRefresher(pool)
        self.assertEqual(refresher.stalest_account(refresher.ttl_seconds()).uid, "old")

    def test_never_fetched_counts_as_the_oldest(self):
        fetched = self._account("fetched", 7 * 3600)
        never = self._account("never", None)
        _, pool = self._pool_with([fetched, never])
        refresher = wb_accounts.CreditsRefresher(pool)
        self.assertEqual(refresher.stalest_account(refresher.ttl_seconds()).uid, "never")

    def test_nothing_is_due_inside_the_ttl(self):
        _, pool = self._pool_with([self._account("fresh", 60)])
        refresher = wb_accounts.CreditsRefresher(pool)
        self.assertIsNone(refresher.stalest_account(refresher.ttl_seconds()))
        self.assertIsNone(refresher.refresh_once())

    def test_ttl_zero_turns_the_refresher_off(self):
        called = []
        account = self._account("off", None,
                                fetch=lambda: called.append(1) or {"ok": True})
        _, pool = self._pool_with([account], hours=0)
        refresher = wb_accounts.CreditsRefresher(pool)
        self.assertIsNone(refresher.refresh_once())
        self.assertEqual(called, [])

    def test_a_successful_refresh_records_the_uid(self):
        account = self._account("ok", None, fetch=lambda: {"ok": True})
        _, pool = self._pool_with([account])
        refresher = wb_accounts.CreditsRefresher(pool)
        self.assertEqual(refresher.refresh_once(), "ok")
        self.assertEqual(refresher.last_uid, "ok")
        self.assertIsNone(refresher.last_error)

    def test_a_failure_parks_the_account(self):
        account = self._account("bad", None,
                                fetch=lambda: {"ok": False, "error": "boom"})
        _, pool = self._pool_with([account])
        refresher = wb_accounts.CreditsRefresher(pool)
        self.assertIsNone(refresher.refresh_once())
        self.assertEqual(refresher.last_error, "boom")
        # 失败后一小时内不再重试同一个账号，免得每个 tick 都打上游。
        self.assertIsNone(refresher.stalest_account(refresher.ttl_seconds()))

    def test_disabled_accounts_are_skipped(self):
        account = self._account("off-account", None)
        account.enabled = False
        _, pool = self._pool_with([account])
        refresher = wb_accounts.CreditsRefresher(pool)
        self.assertIsNone(refresher.stalest_account(refresher.ttl_seconds()))


class RefreshCadenceTests(unittest.TestCase):
    """刷新节奏要保守：上游计费调用有明确上界，且不会比 TTL 更勤。"""

    def _pool(self, count=28, hours=12, age_hours=12):
        directory = tempfile.mkdtemp(prefix="expiry-cadence-")
        wb_settings.set_credits_refresh_hours(directory, hours)
        pool = wb_accounts.AccountPool(directory, log=lambda _m: None)
        base = time.time() - age_hours * 3600
        accounts = []
        for index in range(count):
            blob = {"remain": 1, "used": 0, "size": 1, "packages": [],
                    "updated_at": base - index * 60}
            accounts.append(make_account("acct-%02d" % index, credits=blob))
        pool.accounts = accounts
        return pool, accounts

    def _wire(self, accounts, clock, calls):
        def make_fetch(account):
            def fetch():
                calls.append(account.uid)
                blob = dict(account.credits or {})
                blob["updated_at"] = clock[0]
                account.credits = blob
                return {"ok": True}
            return fetch
        for account in accounts:
            account.fetch_credits = make_fetch(account)

    def test_the_tick_is_half_an_hour(self):
        pool, _ = self._pool(count=1)
        refresher = wb_accounts.CreditsRefresher(pool)
        self.assertEqual(refresher.interval_seconds, 1800)

    def test_a_broken_account_is_parked_for_hours_not_minutes(self):
        pool, _ = self._pool(count=1)
        refresher = wb_accounts.CreditsRefresher(pool)
        self.assertEqual(refresher._failed_cooldown, 6 * 3600)

    def test_a_day_of_ticks_stays_within_the_request_budget(self):
        pool, accounts = self._pool(count=28)
        refresher = wb_accounts.CreditsRefresher(pool)
        clock = [time.time()]
        calls = []
        self._wire(accounts, clock, calls)

        start = clock[0]
        while clock[0] - start < 24 * 3600:
            clock[0] += refresher.interval_seconds
            refresher.refresh_once(now=clock[0])

        # 30 分钟一 tick → 一天 48 tick，每次最多一个账号 → 48 次上界，
        # 与池子大小无关。
        self.assertGreater(len(calls), 0)
        self.assertLessEqual(len(calls), 48)
        # 12 小时 TTL → 一天内任何账号都不会被刷超过两次。
        self.assertLessEqual(max(Counter(calls).values()), 2)

    def test_a_small_pool_is_not_refreshed_more_often_than_the_ttl(self):
        pool, accounts = self._pool(count=3, hours=12, age_hours=12)
        refresher = wb_accounts.CreditsRefresher(pool)
        clock = [time.time()]
        calls = []
        self._wire(accounts, clock, calls)

        start = clock[0]
        while clock[0] - start < 24 * 3600:
            clock[0] += refresher.interval_seconds
            refresher.refresh_once(now=clock[0])

        # 三个账号、一天：每个最多两次（12 小时 TTL），远小于 48 个 tick。
        self.assertLessEqual(max(Counter(calls).values()), 2)
        self.assertLessEqual(len(calls), 6)

    def test_nothing_is_fetched_before_the_ttl_elapses(self):
        pool, accounts = self._pool(count=4, hours=12, age_hours=0)
        refresher = wb_accounts.CreditsRefresher(pool)
        clock = [time.time()]
        calls = []
        self._wire(accounts, clock, calls)

        start = clock[0]
        while clock[0] - start < 11 * 3600:
            clock[0] += refresher.interval_seconds
            refresher.refresh_once(now=clock[0])
        self.assertEqual(calls, [], "TTL 未到不该发请求")


class ConcurrencyTests(unittest.TestCase):
    """并发下池子必须线程安全：无异常、无交错、分布仍然按紧迫度。"""

    def _pool(self, accounts, window=7):
        directory = tempfile.mkdtemp(prefix="expiry-concurrent-")
        pool = wb_accounts.AccountPool(directory, log=lambda _m: None)
        pool.accounts = accounts
        pool.apply_expiring_window({"global": window, "intl": window, "cn": window})
        return pool

    def test_many_threads_pick_without_errors_and_keep_the_share(self):
        accounts = [make_account("c%02d" % i, credits=credits(1 + i * 0.7))
                    for i in range(8)]
        pool = self._pool(accounts)
        results = []
        errors = []
        guard = threading.Lock()
        barrier = threading.Barrier(8)

        def worker():
            local = []
            try:
                barrier.wait(timeout=10)
                for _ in range(200):
                    account = pool.pick(realm="cn")
                    local.append(account.uid)
            except Exception as exc:
                with guard:
                    errors.append(exc)
            with guard:
                results.extend(local)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8 * 200)
        counts = Counter(results)
        # 越接近到期分到越多，并发下这个次序也要保持。
        self.assertGreater(counts["c00"], counts["c07"])

    def test_the_rotation_state_does_not_leak_accounts(self):
        accounts = [make_account("keep%02d" % i, credits=credits(2)) for i in range(3)]
        pool = self._pool(accounts)
        for _ in range(20):
            pool.pick(realm="cn")
        self.assertTrue(set(pool._expiry_weights) <= set(a.uid for a in accounts))

        pool.accounts = [accounts[0]]
        pool.pick(realm="cn")
        self.assertEqual(set(pool._expiry_weights), {accounts[0].uid})

    def test_concurrent_fetches_on_one_account_do_not_interleave(self):
        account = make_account("one")
        active = []
        overlaps = []
        guard = threading.Lock()

        def slow_fetch():
            with guard:
                active.append(1)
                if len(active) > 1:
                    overlaps.append(1)
            time.sleep(0.02)
            with guard:
                active.pop()
            return {"ok": True}

        account._fetch_credits_raw = slow_fetch
        threads = [threading.Thread(target=account.fetch_credits) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertEqual(overlaps, [], "两个取数同时进行说明锁没生效")


class ExpiryWeightTests(unittest.TestCase):
    """权重必须同时反映「还剩几天」与「还剩多少积分」。

    旧算法只用 `round(window - days) + 1`：一来把一整段天数压成同一个整数，
    二来完全不看剩余量。实测生产池里 112 个账号有 110 个 days_left 都是
    0.10，权重全部相同，临期优先退化成纯轮询；同样两天到期的账号，剩 1 积分
    与剩 30 积分也拿一样的份额——而「把临期积分花掉」正是这个功能的全部目的。
    """

    def _pool(self, accounts, window=7):
        directory = tempfile.mkdtemp(prefix="expiry-weight-")
        pool = wb_accounts.AccountPool(directory, log=lambda _m: None)
        pool.accounts = accounts
        pool.apply_expiring_window({"global": window, "intl": window, "cn": window})
        return pool

    def test_soonest_expiring_remain_reads_the_lapsing_package(self):
        account = make_account("r1", credits=credits(9, 2.5, 4))
        self.assertEqual(account.soonest_expiring_remain(), 100)
        # 最早的包里剩多少就读多少，不是全部包的合计。
        blob = {
            "remain": 130, "used": 0, "size": 130,
            "packages": [
                {"package_code": "p", "remain": 30, "days_left": 1.0,
                 "no_expiry": False, "is_expired": False},
                {"package_code": "p", "remain": 100, "days_left": 5.0,
                 "no_expiry": False, "is_expired": False},
            ],
        }
        self.assertEqual(make_account("r2", credits=blob).soonest_expiring_remain(), 30)

    def test_soonest_expiring_remain_is_none_when_nothing_can_be_judged(self):
        self.assertIsNone(make_account("r3").soonest_expiring_remain())
        self.assertIsNone(make_account("r4", credits={}).soonest_expiring_remain())
        self.assertIsNone(
            make_account("r5", credits=credits(2, no_expiry=True)).soonest_expiring_remain())

    def test_more_remaining_credits_earn_a_larger_share(self):
        # 同一天到期、剩余量差 30 倍：剩余多的应分到明显更多流量，但不能
        # 独占（对数压缩，不是 30 倍）。
        big = make_account("big", credits=credits(2, remain=300))
        small = make_account("small", credits=credits(2, remain=10))
        pool = self._pool([big, small])
        picks = [pool.pick(realm="cn").uid for _ in range(400)]
        self.assertGreater(picks.count("big"), picks.count("small"))
        self.assertGreater(picks.count("small"), 0, "剩余少的也要有份额，不能饿死")

    def test_a_pool_that_all_lapses_together_is_not_a_flat_rotation(self):
        """110 个账号 days_left 全相同（生产池实测形态）时仍要有区分度。

        旧算法在这里给出的权重全相同，加权轮询退化成等权轮询；现在剩余量
        参与权重，份额按剩余多少分开。
        """
        accounts = [
            make_account("same-%02d" % i, credits=credits(0.1, remain=10 + i))
            for i in range(10)
        ]
        pool = self._pool(accounts)
        picks = [pool.pick(realm="cn").uid for _ in range(600)]
        counts = Counter(picks)
        self.assertEqual(len(counts), 10, "每个账号都该分到流量")
        # 剩余最多的账号份额应大于剩余最少的。
        self.assertGreater(counts["same-09"], counts["same-00"])

    def test_weight_stays_bounded_so_nobody_takes_everything(self):
        # 极端悬殊（剩 1 vs 剩 1000）也不能让一个账号吃掉全部流量。
        rich = make_account("rich", credits=credits(0.5, remain=1000))
        poor = make_account("poor", credits=credits(0.5, remain=1))
        pool = self._pool([rich, poor])
        picks = [pool.pick(realm="cn").uid for _ in range(300)]
        self.assertGreater(picks.count("poor"), 0)
        self.assertLess(picks.count("rich") / max(1, picks.count("poor")), 10.0)

    def test_urgency_still_dominates_when_amounts_match(self):
        # 剩余量相同时，越接近到期分到的越多——原有语义不能被新因子冲掉。
        sooner = make_account("sooner-w", credits=credits(0.5, remain=100))
        later = make_account("later-w", credits=credits(6.0, remain=100))
        pool = self._pool([sooner, later])
        picks = [pool.pick(realm="cn").uid for _ in range(300)]
        self.assertGreater(picks.count("sooner-w"), picks.count("later-w") * 1.5)

    def test_identical_packages_still_rotate_evenly(self):
        """数据完全相同的账号必须均分——那说明不了谁更急，硬要区分就是编造。

        生产池实测存在这种形态（109 个账号 days_left=0.10 且 remain=30），
        此时权重全相同、平滑加权轮询退化成等权轮询，正是我们想要的：它们
        之间本来就该轮流。
        """
        accounts = [
            make_account("twin-%02d" % i, credits=credits(0.1, remain=30))
            for i in range(6)
        ]
        pool = self._pool(accounts)
        picks = Counter(pool.pick(realm="cn").uid for _ in range(600))
        self.assertEqual(len(picks), 6, "每个账号都要分到流量")
        shares = sorted(picks.values())
        self.assertLessEqual(shares[-1] - shares[0], 60, "同数据账号之间份额应接近均分")

    def test_unknown_credit_data_keeps_weight_one(self):
        # 判不了就回 1，不猜。
        unknown = make_account("unknown-w")
        pool = self._pool([unknown], window=7)
        self.assertEqual(pool._expiry_weight(unknown), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
