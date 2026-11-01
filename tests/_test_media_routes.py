"""媒体路由的单元测试：开关、参数闸门、选号与换号、记账、预算预检、429。

这里**不启动网关进程**，而是给 `Handler` 造一个不碰 socket 的替身：路由方法
只用到 `_json` / `_error` / `_authorized` / `_key_id` / `_key_realm` /
`_key_allowed_accounts` 这几个口子，覆盖掉它们就能把整条流程跑完。上游与账号池
同样被替身接住，所以一个字节不出网、一次积分不花。

钉住的是「钱」与「错」的边界：

  - 开关关着 → 404（不是 403：少一个探测面）
  - 参数不对 / 模型不在组里 / n>1 / 尺寸超限 → 400，且**上游零调用**
  - 上游 401 → 换号；三次都失败 → 502
  - 上游 429 → 429 且带 Retry-After
  - 并发槽满 → 503
  - 余额或日额度不够 → 这个账号根本不参与派发（不让它撞上限）
  - 成功 → usage 行带 kind=image 与真实 credit

Run with: python tests/_test_media_routes.py
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.TemporaryDirectory(prefix="wb-media-routes-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP.name, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = os.path.join(_TMP.name, "usage")
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_media as M
import wb_media_client
import wb_proxy as P
import wb_settings


# ---------------------------------------------------------------- 替身

class FakeAccount(object):
    def __init__(self, uid, realm="cn", remain=1000.0, daily_limit=0, used=0.0):
        self.uid = uid
        self.realm = realm
        self.credits = {"remain": remain, "size": remain, "used": 0} if remain is not None else None
        self.daily_credit_limit = daily_limit
        self.daily_credits_today = used
        self.notes = []
        self.successes = []

    def ready(self, model=None, refresh=True):
        return True

    def unavailable_reason(self, model=None):
        return ""

    def note_error(self, message, cooldown=60, single_account=False, model=None,
                   until=None, detail=None):
        self.notes.append({"message": message, "model": model, "cooldown": cooldown})

    def note_success(self, model=None):
        self.successes.append(model)

    def consume_credit(self, amount):
        # record_usage 在 credit 有值时会扣缓存余额；替身不关心余额变化。
        pass

    def note_restricted(self, detail="", cooldown=None):
        self.notes.append({"restricted": True, "detail": detail})


class FakePool(object):
    def __init__(self, accounts):
        self.accounts = list(accounts)
        self.picks = []

    def pick(self, realm=None, exclude=None, model=None, page=None, allowed=None):
        exclude = exclude or set()
        for account in self.accounts:
            if account.uid in exclude:
                continue
            if allowed is not None and account.uid not in allowed:
                continue
            self.picks.append(account.uid)
            return account
        return None

    def get(self, uid):
        for account in self.accounts:
            if account.uid == uid:
                return account
        return None


class FakeHandler(P.Handler):
    """不初始化 socket 的 Handler 替身。"""

    def __init__(self, key_id="k-1", key_realm="", allowed=None):
        self.key_entry = {"id": key_id}
        self._key_realm_value = key_realm
        self._allowed = allowed
        self.sent = None
        self.sent_kind = None

    # --- 被覆盖的出口
    def _json(self, code, obj):
        self.sent_kind = "json"
        self.sent = (code, obj)
        return obj

    def _error(self, code, message, err_type="server_error"):
        self.sent_kind = "error"
        self.sent = (code, message, err_type)
        return None

    def _rate_limited(self, exc):
        self.sent_kind = "rate_limited"
        self.sent = (getattr(exc, "wait", 60), getattr(exc, "message", ""))
        return None

    def _authorized(self):
        return True

    def _key_id(self):
        return (self.key_entry or {}).get("id")

    def _key_realm(self):
        return self._key_realm_value

    def _key_allowed_accounts(self):
        return self._allowed

    def _suppress_record_error(self):
        pass


def catalog_payload():
    return {"code": 0, "data": {"models": [
        {"id": "glm-5.3", "name": "GLM-5.3", "tags": ["craft"]},
        {"id": "hunyuan-image-alpha", "name": "Hunyuan Image Alpha",
         "tags": ["text-to-image"]},
        {"id": "hunyuan-image-alpha-edit", "name": "Hunyuan Image Alpha Edit",
         "tags": ["image-to-image"]},
        {"id": "kling-video-v3", "name": "Kling-Video-V3",
         "tags": ["text-to-video", "image-to-video"],
         "videoGeneration": {"minSeconds": 3, "maxSeconds": 15,
                             "resolutions": ["720P", "1080P", "4K"],
                             "defaultResolution": "720P", "defaultSeconds": 5,
                             "maxPromptLength": 2500}},
    ]}}


# ---------------------------------------------------------------- 环境装配

class MediaRouteCases(unittest.TestCase):
    def setUp(self):
        self.saved = {
            "POOL": P.POOL,
            "catalog": dict(P._catalog_raw),
            "media": dict(wb_settings.load(os.environ["ACCOUNTS_DIR"]).get("media") or {}),
            "rate": dict(P._MEDIA_RATE),
        }
        P._catalog_raw.clear()
        P._catalog_raw["cn"] = catalog_payload()
        P._MEDIA_RATE.clear()
        self.account = FakeAccount("u-1")
        self.pool = FakePool([self.account])
        P.POOL = self.pool
        self.set_media({"enabled": True, "realm": "cn", "identity": "desktop",
                        "image_max_per_minute": 60, "video_max_seconds": 15,
                        "daily_credits_per_key": 200})
        # 派发前的护栏与连坐：这两条在真实环境里读磁盘/内存状态，测试里给常数
        self.patches = [
            mock.patch.object(P, "apply_daily_guards", lambda: None),
            mock.patch.object(P, "apply_reserve_credits", lambda: None),
            mock.patch.object(P, "restriction_hold_remaining", lambda realm, now=None: 0.0),
            mock.patch.object(P, "pinned_account", lambda: None),
            mock.patch.object(P, "record_error", lambda *a, **k: None),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in self.patches:
            patcher.stop()
        for key, value in self.saved.items():
            if key == "POOL":
                P.POOL = value
            elif key == "catalog":
                P._catalog_raw.clear()
                P._catalog_raw.update(value)
            elif key == "rate":
                P._MEDIA_RATE.clear()
                P._MEDIA_RATE.update(value)
            elif key == "media":
                wb_settings.save(os.environ["ACCOUNTS_DIR"],
                                 dict(wb_settings.load(os.environ["ACCOUNTS_DIR"]),
                                      media=value))

    def set_media(self, patch):
        wb_settings.set_media_config(os.environ["ACCOUNTS_DIR"], patch)

    def post(self, path="/v1/images/generations", payload=None, handler=None):
        handler = handler or FakeHandler()
        body = {"prompt": "一只在窗台上晒太阳的猫"}
        body.update(payload or {})
        handler._handle_media_post(path, body)
        return handler


# ---------------------------------------------------------------- 开关与参数

class GateTests(MediaRouteCases):
    def test_disabled_is_404(self):
        self.set_media({"enabled": False})
        handler = self.post()
        self.assertEqual(handler.sent_kind, "error")
        self.assertEqual(handler.sent[0], 404)

    def test_missing_prompt_is_400(self):
        handler = self.post(payload={"prompt": "   "})
        self.assertEqual(handler.sent[0], 400)

    def test_unknown_model_is_400_and_never_calls_upstream(self):
        with mock.patch.object(wb_media_client, "generate_image") as generate:
            handler = self.post(payload={"model": "not-exist"})
        self.assertEqual(handler.sent[0], 400)
        self.assertIn("hunyuan-image-alpha", handler.sent[1])
        generate.assert_not_called()

    def test_batch_is_rejected(self):
        with mock.patch.object(wb_media_client, "generate_image") as generate:
            handler = self.post(payload={"n": 4})
        self.assertEqual(handler.sent[0], 400)
        self.assertIn("一次生成一张图", handler.sent[1])
        generate.assert_not_called()

    def test_oversized_is_rejected_locally(self):
        with mock.patch.object(wb_media_client, "generate_image") as generate:
            handler = self.post(payload={"size": "2560x2560"})
        self.assertEqual(handler.sent[0], 400)
        self.assertIn("419", handler.sent[1].replace("4.19", "419"))
        generate.assert_not_called()

    def test_key_realm_mismatch_is_explained(self):
        handler = self.post(handler=FakeHandler(key_realm="intl"))
        self.assertEqual(handler.sent[0], 400)
        self.assertIn("出口", handler.sent[1])

    def test_rate_limit_answers_429(self):
        self.set_media({"image_max_per_minute": 1})
        with mock.patch.object(wb_media_client, "generate_image",
                               return_value={"url": "https://cos/x.png"}):
            self.post()
            handler = self.post()
        self.assertEqual(handler.sent_kind, "rate_limited")
        self.assertGreaterEqual(handler.sent[0], 1)


# ---------------------------------------------------------------- 成功路径

class SuccessTests(MediaRouteCases):
    def test_returns_openai_images_shape(self):
        outcome = {"url": "https://cos/x.png", "credit": 5.71, "created": 1762000000}
        with mock.patch.object(wb_media_client, "generate_image",
                               return_value=outcome) as generate:
            handler = self.post()
        code, body = handler.sent
        self.assertEqual(code, 200)
        self.assertEqual(body["data"][0]["url"], "https://cos/x.png")
        self.assertEqual(body["credit"], 5.71)
        self.assertEqual(body["model"], "hunyuan-image-alpha")
        self.assertEqual(body["mode"], "generate")
        self.assertIn("24 小时", body["hint"])
        self.assertEqual(generate.call_args[0][1]["model"], "hunyuan-image-alpha")

    def test_b64_is_passed_through_not_written_to_disk(self):
        with mock.patch.object(wb_media_client, "generate_image",
                               return_value={"b64": "AAA", "credit": 0.03}):
            handler = self.post()
        code, body = handler.sent
        self.assertEqual(body["data"][0]["b64_json"], "AAA")
        self.assertNotIn("hint", body)

    def test_records_usage_with_kind_and_credit(self):
        with mock.patch.object(wb_media_client, "generate_image",
                               return_value={"url": "u", "credit": 5.71}), \
                mock.patch.object(P, "record_usage") as record:
            self.post()
        record.assert_called_once()
        args, kwargs = record.call_args
        self.assertEqual(args[0], "hunyuan-image-alpha")
        self.assertEqual(args[1], {"credit": 5.71})
        self.assertEqual(kwargs["kind"], "image")
        self.assertEqual(kwargs["account"], "u-1")
        self.assertEqual(kwargs["key"], "k-1")

    def test_account_success_clears_penalties(self):
        with mock.patch.object(wb_media_client, "generate_image",
                               return_value={"url": "u"}):
            self.post()
        self.assertEqual(self.account.successes, ["hunyuan-image-alpha"])

    def test_slot_is_released_even_when_upstream_fails(self):
        before = P.media_inflight()
        with mock.patch.object(wb_media_client, "generate_image",
                               side_effect=M.MediaError("UPSTREAM", "boom")):
            self.post()
        self.assertEqual(P.media_inflight(), before)


# ---------------------------------------------------------------- 换号与失败

class FailoverTests(MediaRouteCases):
    def test_auth_failure_rotates_to_the_next_account(self):
        second = FakeAccount("u-2")
        P.POOL = FakePool([self.account, second])
        outcomes = [M.MediaError("AUTH", "bad token", status=401),
                    {"url": "https://cos/x.png", "credit": 5.71}]
        with mock.patch.object(wb_media_client, "generate_image", side_effect=outcomes):
            handler = self.post()
        self.assertEqual(handler.sent[0], 200)
        self.assertEqual(second.successes, ["hunyuan-image-alpha"])
        # 401 是可换号失败，而且要记在这个模型上
        self.assertEqual(self.account.notes[0]["model"], "hunyuan-image-alpha")

    def test_gives_up_after_rotate_limit(self):
        accounts = [FakeAccount("u-%d" % i) for i in range(1, 6)]
        P.POOL = FakePool(accounts)
        with mock.patch.object(wb_media_client, "generate_image",
                               side_effect=M.MediaError("AUTH", "bad", status=401)):
            handler = self.post()
        self.assertEqual(handler.sent_kind, "error")
        self.assertEqual(handler.sent[0], 502)
        self.assertEqual(P.MEDIA_ROTATE_MAX, 3)

    def test_rejected_does_not_rotate_and_does_not_cooldown(self):
        """REJECTED 不是可换号失败：换谁都是同一个 400，而且一分钱没花。"""
        with mock.patch.object(wb_media_client, "generate_image",
                               side_effect=M.MediaError("REJECTED", "bad size", status=400)):
            handler = self.post()
        self.assertEqual(handler.sent[0], 400)
        self.assertIn("未扣积分", handler.sent[1])
        self.assertEqual(self.account.notes, [])

    def test_restriction_does_not_rotate(self):
        error = M.MediaError("AUTH", "HTTP 403 restricted", status=403,
                             detail='{"code":11140}')
        with mock.patch.object(P, "restriction_is_account_level", lambda text: True), \
                mock.patch.object(P, "_persist_restriction", lambda account: None), \
                mock.patch.object(P, "note_account_restriction", lambda *a, **k: None), \
                mock.patch.object(wb_media_client, "generate_image", side_effect=error):
            handler = self.post()
        self.assertEqual(handler.sent[0], 502)
        self.assertTrue(self.account.notes[0].get("restricted"))

    def test_pool_pause_blocks_before_any_upstream_call(self):
        with mock.patch.object(P, "restriction_hold_remaining", lambda realm, now=None: 120.0), \
                mock.patch.object(wb_media_client, "generate_image") as generate:
            handler = self.post()
        self.assertEqual(handler.sent[0], 429)
        self.assertIn("整池暂停", handler.sent[1])
        generate.assert_not_called()

    def test_busy_slots_answer_503(self):
        with mock.patch.object(P, "acquire_media_slot", lambda: False), \
                mock.patch.object(wb_media_client, "generate_image") as generate:
            handler = self.post()
        self.assertEqual(handler.sent[0], 503)
        generate.assert_not_called()


# ---------------------------------------------------------------- 预算闸门

class BudgetTests(MediaRouteCases):
    def test_insufficient_balance_skips_the_account(self):
        self.account.credits = {"remain": 3.0}
        with mock.patch.object(wb_media_client, "generate_image") as generate:
            handler = self.post()
        self.assertEqual(handler.sent[0], 402)
        self.assertIn("付得起", handler.sent[1])
        generate.assert_not_called()

    def test_daily_headroom_is_checked(self):
        """日限额剩 2 积分、本次预估 6：不许派发，否则会顶穿当日额度并连累 chat。"""
        self.account.daily_credit_limit = 100
        self.account.daily_credits_today = 98.0
        with mock.patch.object(wb_media_client, "generate_image") as generate:
            handler = self.post()
        self.assertEqual(handler.sent[0], 402)
        generate.assert_not_called()

    def test_unknown_balance_does_not_block(self):
        self.account.credits = None
        with mock.patch.object(wb_media_client, "generate_image",
                               return_value={"url": "u"}):
            handler = self.post()
        self.assertEqual(handler.sent[0], 200)

    def test_media_budget_ok_reasons(self):
        ok, reason = P.media_budget_ok(FakeAccount("a", remain=1.0), 6.0)
        self.assertFalse(ok)
        self.assertIn("余额", reason)
        ok, reason = P.media_budget_ok(FakeAccount("b", remain=100.0), 6.0)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_rate_allow_windows(self):
        P._MEDIA_RATE.clear()
        ok, _ = P.media_rate_allow("k", 2, now=1000.0)
        self.assertTrue(ok)
        ok, _ = P.media_rate_allow("k", 2, now=1001.0)
        self.assertTrue(ok)
        ok, wait = P.media_rate_allow("k", 2, now=1002.0)
        self.assertFalse(ok)
        self.assertGreaterEqual(wait, 1)
        # 下一分钟重新计数
        ok, _ = P.media_rate_allow("k", 2, now=1060.0)
        self.assertTrue(ok)
        # 0 表示不限
        self.assertTrue(P.media_rate_allow("k", 0, now=1060.0)[0])


# ---------------------------------------------------------------- 目录端点

class MediaModelsEndpointTests(MediaRouteCases):
    def test_lists_media_models_with_video_config(self):
        handler = FakeHandler()
        handler._get_media_models({})
        code, body = handler.sent
        self.assertEqual(code, 200)
        ids = [model["id"] for model in body["models"]]
        self.assertIn("hunyuan-image-alpha", ids)
        self.assertNotIn("glm-5.3", ids)
        video = [m for m in body["models"] if m["id"] == "kling-video-v3"][0]
        self.assertEqual(video["videoGeneration"]["minSeconds"], 3)

    def test_tag_filter(self):
        handler = FakeHandler()
        handler._get_media_models({"tag": ["text-to-image"]})
        ids = [model["id"] for model in handler.sent[1]["models"]]
        self.assertEqual(ids, ["hunyuan-image-alpha"])

    def test_rejects_unknown_realm(self):
        handler = FakeHandler()
        handler._get_media_models({"realm": ["mars"]})
        self.assertEqual(handler.sent[0], 400)

    def test_works_while_disabled(self):
        """配置之前先看看有什么——这正是这个端点存在的理由之一。"""
        self.set_media({"enabled": False})
        handler = FakeHandler()
        handler._get_media_models({})
        self.assertEqual(handler.sent[0], 200)
        self.assertFalse(handler.sent[1]["enabled"])


# ---------------------------------------------------------------- 错误映射

class ErrorMappingTests(unittest.TestCase):
    def test_status_codes(self):
        handler = FakeHandler()
        cases = {"INVALID_ARGS": 400, "NO_MEDIA_MODEL": 400, "REJECTED": 400,
                 "NO_ROUTE": 404, "NO_CREDIT": 402, "RATE_LIMIT": 429,
                 "AUTH": 502, "UPSTREAM": 502, "EMPTY_RESULT": 502}
        for code, expected in cases.items():
            handler._media_error(M.MediaError(code, "msg"))
            self.assertEqual(handler.sent[0], expected, code)

    def test_free_failures_say_so(self):
        handler = FakeHandler()
        handler._media_error(M.MediaError("REJECTED", "unsupported duration"))
        self.assertIn("未扣积分", handler.sent[1])

    def test_paid_failures_do_not_claim_freeness(self):
        handler = FakeHandler()
        handler._media_error(M.MediaError("UPSTREAM", "boom"))
        self.assertNotIn("未扣积分", handler.sent[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
