"""429 的模型级限流窗口，重启后必须重新挂上。

# 缺陷

上游 429 带重置时刻时，`wb_window_probe` 会把窗口落盘到
`accounts/model_windows.json`（跨重启有效），请求路径上也会挂
`account.model_cooldowns[model] = reset_at`。

但重启之后只有 6004（配额用满）会被 `AccountPool.restore_model_caps()`
从凭证档的 `modelCaps` 挂回来。普通 429 的窗口虽然躺在
`model_windows.json` 里，却没有任何代码读它 —— 同一个事实存了两处，
只读回来一处。后果：

  * `throttle_wait(model)` 归零 → 池子把还在限流期的账号当可用，一路派单，
    每一单都再吃一次 429；
  * `public()['modelCooldowns']` 为空 → 面板「当前禁用」什么都不显示，而页脚
    写着「『上游限流』到点自动恢复」；
  * 窗口探针到点仍会执行（它读的是 `model_windows.json`），所以「恢复」照常
    发生 —— 只有「限流中」这一段对用户不可见。

# 覆盖的边界

  * 未到期的窗口 → 挂回，且只封该模型（同账号其他模型照常接单）；
  * 已过期的窗口 → 不挂：那是探针的活，挂上等于凭空多等一段；
  * 状态文件损坏 → 加载不受影响（宁可这次不恢复，也不能让整池加载失败）；
  * `clear_model_cooldown` 连起始时刻一起清，否则下次限流会显示上次的开始时间。

不需要网络，也不碰真实账号目录。
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_window_probe

MODEL = "deepseek-v4.1-flash"
OTHER = "hy3"


def write_account(directory, uid, realm="cn"):
    """最小可加载的凭证档：只要 uid / realm / accessToken 就能进池。"""
    data = {
        "uid": uid,
        "realm": realm,
        "domain": "www.codebuddy.cn",
        "accessToken": "token-" + uid,
        "refreshToken": "refresh-" + uid,
        "nickname": uid[:8],
    }
    path = os.path.join(directory, uid + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    return path


def write_windows(directory, rows):
    """按 wb_window_probe._save 的形状写状态文件（绕过调度器，直接铺状态）。"""
    payload = {
        "version": 1,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "windows": rows,
    }
    with open(os.path.join(directory, wb_window_probe.STATE_NAME), "w",
              encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)


def window(uid, model, reset_at):
    return {"uid": uid, "model": model, "reset_at": float(reset_at),
            "attempts": 0, "last_try": 0.0, "last_result": ""}


class WindowCooldownRestoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wb-window-restore-")
        self.uid = "11111111-2222-3333-4444-555555555555"
        write_account(self.dir, self.uid)
        self._reset_probe_cache()

    def tearDown(self):
        self._reset_probe_cache()

    @staticmethod
    def _reset_probe_cache():
        """探针模块按目录懒加载并缓存，测试之间必须清掉，否则读到上一个用例的状态。"""
        wb_window_probe._state["dir"] = None
        wb_window_probe._state["windows"] = {}

    def _load(self):
        pool = wb_accounts.AccountPool(self.dir, log=lambda _m: None)
        pool.load()
        return pool

    def _account(self):
        return self._load().get(self.uid)

    # --- 主缺陷 -----------------------------------------------------------

    def test_future_window_restores_the_model_cooldown(self):
        """重启后仍在恢复期内 → 冷却必须挂回来（这就是被修的缺陷）。"""
        reset_at = time.time() + 6 * 3600
        write_windows(self.dir, [window(self.uid, MODEL, reset_at)])

        account = self._account()
        self.assertGreater(account.throttle_wait(model=MODEL), 0,
                           "未到期的窗口没有挂回模型冷却")
        self.assertFalse(account.ready(model=MODEL))

    def test_restored_window_reaches_the_panel(self):
        """面板「当前禁用」读 public()['modelCooldowns']：挂回后必须出现。"""
        reset_at = time.time() + 6 * 3600
        write_windows(self.dir, [window(self.uid, MODEL, reset_at)])

        rows = self._account().public()["modelCooldowns"]
        models = [r["model"] for r in rows]
        self.assertIn(MODEL, models)
        entry = next(r for r in rows if r["model"] == MODEL)
        self.assertEqual(entry["expiresAt"], int(reset_at))

    def test_other_models_of_the_same_account_stay_usable(self):
        """模型级限流不该连坐同账号的其他模型 —— 这是整个机制的前提。"""
        write_windows(self.dir, [window(self.uid, MODEL, time.time() + 6 * 3600)])

        account = self._account()
        self.assertFalse(account.ready(model=MODEL))
        self.assertTrue(account.ready(model=OTHER),
                        "同一账号的其他模型被误封")
        self.assertEqual(account.throttle_wait(model=OTHER), 0)

    # --- 边界 -------------------------------------------------------------

    def test_expired_window_does_not_arm(self):
        """已过期的窗口不挂：到点后由窗口探针处理，挂上等于凭空多等一段。"""
        write_windows(self.dir, [window(self.uid, MODEL, time.time() - 60)])

        account = self._account()
        self.assertEqual(account.throttle_wait(model=MODEL), 0)
        self.assertTrue(account.ready(model=MODEL))

    def test_later_stored_cooldown_wins(self):
        """盘上的窗口比内存里已有的更早时，不覆盖更晚的那个。"""
        write_windows(self.dir, [window(self.uid, MODEL, time.time() + 60)])

        pool = wb_accounts.AccountPool(self.dir, log=lambda _m: None)
        account = wb_accounts.Account(self._read_account_data(), None)
        later = time.time() + 7200
        account.model_cooldowns[MODEL] = later
        pool.accounts = [account]
        pool.restore_window_cooldowns()
        self.assertGreaterEqual(account.model_cooldowns[MODEL], later)

    def test_corrupt_state_file_does_not_break_loading(self):
        """状态文件坏掉只丢这次恢复，不能让整池加载失败。"""
        with open(os.path.join(self.dir, wb_window_probe.STATE_NAME), "w",
                  encoding="utf-8") as fh:
            fh.write("{ this is not json")
        pool = self._load()
        self.assertEqual(len(pool.accounts), 1)
        self.assertEqual(pool.accounts[0].throttle_wait(model=MODEL), 0)

    def test_unknown_account_in_state_is_ignored(self):
        """窗口指向已删除的账号时静默跳过（探针那边负责清理该记录）。"""
        write_windows(self.dir, [
            window("99999999-8888-7777-6666-555555555555", MODEL, time.time() + 600),
        ])
        pool = self._load()
        self.assertEqual(len(pool.accounts), 1)
        self.assertEqual(pool.accounts[0].throttle_wait(model=MODEL), 0)

    # --- 顺带修掉的小问题 -------------------------------------------------

    def test_clear_model_cooldown_also_drops_the_start_time(self):
        """清冷却要连起始时刻一起清，否则下次限流会显示上一次的开始时间。"""
        write_windows(self.dir, [window(self.uid, MODEL, time.time() + 6 * 3600)])
        account = self._account()
        account.model_cooldowns_at[MODEL] = time.time() - 600

        account.clear_model_cooldown(MODEL)

        self.assertNotIn(MODEL, account.model_cooldowns)
        self.assertNotIn(MODEL, account.model_cooldowns_at)

    def _read_account_data(self):
        with open(os.path.join(self.dir, self.uid + ".json"), encoding="utf-8") as fh:
            return json.load(fh)


if __name__ == "__main__":
    unittest.main(verbosity=2)
