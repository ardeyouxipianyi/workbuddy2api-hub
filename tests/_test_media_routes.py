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
os.environ["WB_MEDIA_DIR"] = os.path.join(_TMP.name, "media")
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_accounts
import wb_media as M
import wb_media_client
import wb_media_tasks as T
import wb_proxy as P
import wb_settings

#: 给每个任务表一个独立目录（共用会让上一条用例落盘的 tasks.json 被下一条读回来）。
_STORE_SEQ = [0]


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
        self.path = "/v1/images/generations"

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

    def _panel_ok(self):
        return False

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
        {"id": "minimax-video-h3", "name": "MiniMax-H3",
         "tags": ["text-to-video", "image-to-video"],
         "videoGeneration": {"minSeconds": 4, "maxSeconds": 15,
                             "resolutions": ["768P", "2K"],
                             "defaultResolution": "768P", "defaultSeconds": 5,
                             "maxPromptLength": 2000,
                             "maxReferenceImages": 9, "maxReferenceVideos": 3,
                             "maxReferenceAudios": 3, "maxReferenceMaterials": 12,
                             "allowMixedReferenceMaterials": True,
                             "framesExclusiveWithReferenceMaterials": True,
                             "supportsFirstFrame": True}},
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

    def test_b64_json_converts_the_upstream_url(self):
        """客户端要 b64_json 时，上游给的 COS URL 由网关下载转码。"""
        png = bytes([0x89, 0x50, 0x4E, 0x47]) + b"payload"
        with mock.patch.object(wb_media_client, "generate_image",
                               return_value={"url": "https://cos/x.png", "credit": 5.71}),                 mock.patch.object(wb_media_client, "fetch_bytes",
                                  return_value=png) as fetch:
            handler = self.post(payload={"response_format": "b64_json"})
        code, body = handler.sent
        item = body["data"][0]
        self.assertIn("b64_json", item)
        self.assertNotIn("url", item)
        import base64 as _b64
        self.assertEqual(_b64.b64decode(item["b64_json"]), png)
        fetch.assert_called_once()

    def test_b64_json_falls_back_to_url_when_conversion_fails(self):
        """转码失败不能丢图：退回 url，并在 hint 里说清楚。"""
        with mock.patch.object(wb_media_client, "generate_image",
                               return_value={"url": "https://cos/x.png"}),                 mock.patch.object(wb_media_client, "fetch_bytes",
                                  side_effect=M.MediaError("UPSTREAM", "boom")):
            handler = self.post(payload={"response_format": "b64_json"})
        code, body = handler.sent
        self.assertEqual(body["data"][0]["url"], "https://cos/x.png")
        self.assertIn("转码失败", body["hint"])

    def test_invalid_response_format_is_rejected(self):
        with mock.patch.object(wb_media_client, "generate_image") as generate:
            handler = self.post(payload={"response_format": "png"})
        self.assertEqual(handler.sent[0], 400)
        generate.assert_not_called()

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




# ---------------------------------------------------------------- 生视频（提交与学区间）

class VideoSubmitTests(MediaRouteCases):
    """/v1/videos 的提交流程。

    注意这一层**不测轮询**：轮询在后台 worker 里，已由 `_test_media_tasks` 覆盖；
    这里只测"提交之前的每一道闸"和提交之后的登记。
    """

    def setUp(self):
        super(VideoSubmitTests, self).setUp()
        self.saved_tasks = P._MEDIA_TASKS
        self.saved_learned = dict(P._MEDIA_LEARNED_SECONDS)
        P._MEDIA_LEARNED_SECONDS.clear()
        _STORE_SEQ[0] += 1
        self.store = T.MediaTaskStore(
            out_dir=os.path.join(_TMP.name, "video-%d" % _STORE_SEQ[0]),
            client=object(), resolve_account=lambda uid: FakeAccount(uid),
            autostart=False)
        P._MEDIA_TASKS = self.store
        # 目录里增加一个**没有 videoGeneration** 的视频模型：用来验证「拿不到区间
        # 就不拦、让上游先拒一次、再把区间学下来」这条路径。
        models = P._catalog_raw["cn"]["data"]["models"]
        if not any(m["id"] == "kling-noconfig" for m in models):
            models.append({"id": "kling-noconfig", "name": "Kling NoConfig",
                           "tags": ["text-to-video", "image-to-video"]})

    def tearDown(self):
        P._MEDIA_TASKS = self.saved_tasks
        P._MEDIA_LEARNED_SECONDS.clear()
        P._MEDIA_LEARNED_SECONDS.update(self.saved_learned)
        self.store.stop()
        models = P._catalog_raw["cn"]["data"]["models"]
        P._catalog_raw["cn"]["data"]["models"] = [
            m for m in models if m["id"] != "kling-noconfig"]
        super(VideoSubmitTests, self).tearDown()

    def submit(self, payload=None, handler=None):
        handler = handler or FakeHandler()
        body = {"prompt": "海边日落", "seconds": 5}
        body.update(payload or {})
        handler._media_video_submit(body)
        return handler

    def test_submit_returns_202_and_registers_the_task(self):
        with mock.patch.object(wb_media_client, "submit_video",
                               return_value={"taskId": "up-1"}) as submit:
            handler = self.submit()
        code, body = handler.sent
        self.assertEqual(code, 202)
        self.assertTrue(body["id"].startswith("vid_"))
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["seconds"], "5")           # OpenAI 侧是字符串
        self.assertEqual(len(self.store.list()), 1)
        # 提交用的是该区的账号；水印由网关统一关闭，不接受调用方覆盖
        self.assertIs(submit.call_args[0][1]["watermark"], False)

    def test_queue_full_is_429_and_costs_nothing(self):
        self.store.max_pending = 1
        self.store.create("m", {}, "u-1", {"prompt": "x"}, "up-0")
        with mock.patch.object(wb_media_client, "submit_video") as submit:
            handler = self.submit()
        self.assertEqual(handler.sent[0], 429)
        self.assertIn("队列已满", handler.sent[1])
        submit.assert_not_called()

    def test_seconds_over_the_gateway_cap(self):
        self.set_media({"video_max_seconds": 4})
        with mock.patch.object(wb_media_client, "submit_video") as submit:
            handler = self.submit({"seconds": 5})
        self.assertEqual(handler.sent[0], 400)
        self.assertIn("4 秒", handler.sent[1])
        submit.assert_not_called()

    def test_missing_prompt_is_400(self):
        with mock.patch.object(wb_media_client, "submit_video") as submit:
            handler = self.submit({"prompt": "  "})
        self.assertEqual(handler.sent[0], 400)
        submit.assert_not_called()

    def test_rejected_learns_the_range_then_blocks_locally(self):
        """上游拒一次之后，同一个越界值第二次在本地就被拦下（零请求、零冷却）。"""
        error = M.MediaError(
            "REJECTED",
            "上游拒绝了参数（未扣积分）：unsupported video duration: 1 seconds, "
            "supported range: 4-15 seconds",
            status=400,
            detail="unsupported video duration: 1 seconds, supported range: 4-15 seconds")
        with mock.patch.object(wb_media_client, "submit_video", side_effect=error) as submit:
            first = self.submit({"model": "kling-noconfig", "seconds": 1})
        self.assertEqual(first.sent[0], 400)
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(P.learned_video_seconds("cn", "kling-noconfig"), (4, 15))

        # 第二次：本地直接拦，submit 一次都不再调用
        with mock.patch.object(wb_media_client, "submit_video") as submit2:
            second = self.submit({"model": "kling-noconfig", "seconds": 1})
        self.assertEqual(second.sent[0], 400)
        self.assertIn("4", second.sent[1])
        submit2.assert_not_called()

    def test_image_to_video_sends_image_url_and_drops_aspect_ratio(self):
        """图生视频：输入图归一后进 image_url，且**不带** aspect_ratio（比例由图决定）。"""
        with mock.patch.object(wb_media_client, "submit_video",
                               return_value={"taskId": "up-2"}) as submit:
            handler = self.submit({"input_reference": "data:image/png;base64,AAA"})
        self.assertEqual(handler.sent[0], 202)
        body = submit.call_args[0][1]
        self.assertTrue(body["image_url"].startswith("data:image/png;base64,"))
        self.assertNotIn("aspect_ratio", body["extra_parameters"])

    def test_video_tasks_endpoint_returns_public_shape(self):
        self.store.create("kling-video-v3", {"seconds": 5, "resolution": "720P"},
                          "u-1", {"prompt": "x"}, "up-9", key="k-1")
        handler = FakeHandler()
        handler._get_media_tasks({})
        code, body = handler.sent
        self.assertEqual(code, 200)
        self.assertEqual(len(body["tasks"]), 1)
        self.assertEqual(body["tasks"][0]["object"], "video")
        self.assertEqual(body["stats"]["total"], 1)

    def test_wait_flag_on_the_query_blocks_until_done(self):
        """`?wait=1` 写在 query 上时必须真的等——曾经只读 body，于是静默失效。"""
        handler = FakeHandler()
        handler.path = "/v1/videos?wait=1"
        with mock.patch.object(wb_media_client, "submit_video",
                               return_value={"taskId": "up-1"}),                 mock.patch.object(self.store, "wait",
                                  return_value={"id": "vid_x", "status": "completed",
                                                "model": "kling", "videos": [],
                                                "credit": 57.14}) as wait:
            handler._media_video_submit({"prompt": "海边日落", "seconds": 5})
        wait.assert_called_once()
        self.assertEqual(handler.sent[0], 202)
        self.assertEqual(handler.sent[1]["status"], "completed")

    def test_without_the_wait_flag_it_returns_immediately(self):
        handler = FakeHandler()
        handler.path = "/v1/videos"
        with mock.patch.object(wb_media_client, "submit_video",
                               return_value={"taskId": "up-1"}),                 mock.patch.object(self.store, "wait") as wait:
            handler._media_video_submit({"prompt": "海边日落", "seconds": 5})
        wait.assert_not_called()
        self.assertEqual(handler.sent[1]["status"], "queued")

    def test_signed_content_url_downloads_without_an_api_key(self):
        """签名 URL 必须能不带 Authorization 头打开。

        浏览器与播放器带不上这个头，所以 content 分支不能走 `_authorized()`。
        曾经的顺序错误让"下载地址明明是对的、客户端却拿到 401"。
        """
        out_dir = os.path.join(_TMP.name, "content-%d" % _STORE_SEQ[0])
        _STORE_SEQ[0] += 1
        os.makedirs(out_dir, exist_ok=True)
        payload = bytes([0, 0, 0, 0x18]) + b"ftypmp42FAKE"
        path = os.path.join(out_dir, "fake.mp4")
        with open(path, "wb") as handle:
            handle.write(payload)
        task = self.store.create("kling-video-v3", {"seconds": 4}, "u-1",
                                 {"prompt": "x"}, "up-1")
        self.store._finish(task["id"], "completed", credit=57.14, videos=[path])

        handler = FakeHandler()
        handler._authorized = lambda: False              # 没有 API Key
        handler.send_response = mock.MagicMock()         # _send_bytes 走真实写出路径
        handler.send_header = mock.MagicMock()
        handler.end_headers = mock.MagicMock()
        handler.wfile = mock.MagicMock()
        handler._get_media_video(
            "/v1/videos/%s/content" % task["id"], {"t": [self.store.sign(task["id"], 0)]})
        handler.wfile.write.assert_called_once_with(payload)

    def test_content_without_token_and_without_key_is_401(self):
        handler = FakeHandler()
        handler._authorized = lambda: False
        handler._get_media_video("/v1/videos/vid_x/content", {})
        self.assertEqual(handler.sent[0], 401)

    def test_cancel_queued_task(self):
        task = self.store.create("m", {}, "u-1", {"prompt": "x"}, "up-1", key="k-1")
        handler = FakeHandler(key_id="k-1")
        handler._media_cancel(task["id"])
        code, body = handler.sent
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "failed")
        self.assertIn("cancelled", body["error"]["message"])

    def test_cancel_running_task_is_409(self):
        task = self.store.create("m", {}, "u-1", {"prompt": "x"}, "up-1", key="k-1")
        self.store._update(task["id"], status="running")
        handler = FakeHandler(key_id="k-1")
        handler._media_cancel(task["id"])
        self.assertEqual(handler.sent[0], 409)
        self.assertIn("上游没有取消接口", handler.sent[1])

    def test_cancel_foreign_task_is_404(self):
        task = self.store.create("m", {}, "u-2", {"prompt": "x"}, "up-2", key="k-2")
        handler = FakeHandler(key_id="k-1")
        handler._media_cancel(task["id"])
        self.assertEqual(handler.sent[0], 404)

    def test_unknown_video_get_is_404(self):
        handler = FakeHandler()
        handler._get_media_video("/v1/videos/vid_nope", {})
        self.assertEqual(handler.sent[0], 404)


# ---------------------------------------------------------------- 两条设计承诺

class OwnershipTests(MediaRouteCases):
    """多 Key 部署下的任务归属：Key A 不该看到 Key B 的任务。"""

    def setUp(self):
        super(OwnershipTests, self).setUp()
        self.saved_tasks = P._MEDIA_TASKS
        _STORE_SEQ[0] += 1
        self.store = T.MediaTaskStore(
            out_dir=os.path.join(_TMP.name, "own-%d" % _STORE_SEQ[0]),
            client=object(), resolve_account=lambda uid: FakeAccount(uid),
            autostart=False)
        P._MEDIA_TASKS = self.store

    def tearDown(self):
        P._MEDIA_TASKS = self.saved_tasks
        self.store.stop()
        super(OwnershipTests, self).tearDown()

    def test_list_shows_only_the_callers_tasks(self):
        self.store.create("m", {}, "u-1", {"prompt": "mine"}, "up-1", key="k-1")
        self.store.create("m", {}, "u-2", {"prompt": "theirs"}, "up-2", key="k-2")
        handler = FakeHandler(key_id="k-1")
        handler._get_media_tasks({})
        code, body = handler.sent
        self.assertEqual(code, 200)
        self.assertEqual(len(body["tasks"]), 1)
        self.assertEqual(body["stats"]["total"], 2)      # 统计是全局的，可接受

    def test_get_other_key_task_is_404_not_403(self):
        """用 404 而不是 403：不向别的 Key 确认"这个 id 存在"。"""
        task = self.store.create("m", {}, "u-2", {"prompt": "x"}, "up-2", key="k-2")
        handler = FakeHandler(key_id="k-1")
        handler._get_media_video("/v1/videos/%s" % task["id"], {})
        self.assertEqual(handler.sent[0], 404)

    def test_own_task_is_visible(self):
        task = self.store.create("m", {}, "u-1", {"prompt": "x"}, "up-1", key="k-1")
        handler = FakeHandler(key_id="k-1")
        handler._get_media_video("/v1/videos/%s" % task["id"], {})
        self.assertEqual(handler.sent[0], 200)

    def test_panel_session_sees_everything(self):
        task = self.store.create("m", {}, "u-2", {"prompt": "x"}, "up-2", key="k-2")
        handler = FakeHandler(key_id="k-1")
        handler._panel_ok = lambda: True
        handler._get_media_video("/v1/videos/%s" % task["id"], {})
        self.assertEqual(handler.sent[0], 200)

    def test_download_with_key_but_foreign_task_is_refused(self):
        out_dir = os.path.join(_TMP.name, "own-file-%d" % _STORE_SEQ[0])
        _STORE_SEQ[0] += 1
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "x.mp4")
        with open(path, "wb") as handle:
            handle.write(b"data")
        task = self.store.create("m", {}, "u-2", {"prompt": "x"}, "up-2", key="k-2")
        self.store._finish(task["id"], "completed", videos=[path])
        handler = FakeHandler(key_id="k-1")
        handler.send_response = mock.MagicMock()
        handler.send_header = mock.MagicMock()
        handler.end_headers = mock.MagicMock()
        handler.wfile = mock.MagicMock()
        handler._get_media_video("/v1/videos/%s/content" % task["id"], {})
        self.assertEqual(handler.sent[0], 404)
        handler.wfile.write.assert_not_called()


class KeySpendTests(MediaRouteCases):
    """Key 级每日额度：第二道钱闸门。"""

    def setUp(self):
        super(KeySpendTests, self).setUp()
        P._MEDIA_KEY_SPEND.clear()

    def tearDown(self):
        P._MEDIA_KEY_SPEND.clear()
        super(KeySpendTests, self).tearDown()

    def test_limit_allows_then_blocks(self):
        ok, _ = P.media_key_spend_allow("k", 6.0, 10)
        self.assertTrue(ok)
        P.media_key_spend_add("k", 8.0)
        ok, why = P.media_key_spend_allow("k", 6.0, 10)   # 8 + 6 > 10
        self.assertFalse(ok)
        self.assertIn("10", why)

    def test_zero_means_unlimited(self):
        P.media_key_spend_add("k", 9999)
        self.assertTrue(P.media_key_spend_allow("k", 9999, 0)[0])

    def test_keys_are_separate(self):
        P.media_key_spend_add("a", 50)
        self.assertTrue(P.media_key_spend_allow("b", 6, 60)[0])
        self.assertFalse(P.media_key_spend_allow("a", 20, 60)[0])

    def test_route_blocks_when_exhausted(self):
        self.set_media({"daily_credits_per_key": 5})
        from unittest import mock as _mock
        with _mock.patch.object(wb_media_client, "generate_image") as generate:
            handler = self.post()
        self.assertEqual(handler.sent[0], 402)
        self.assertIn("积分上限", handler.sent[1])
        generate.assert_not_called()

    def test_successful_call_accrues(self):
        from unittest import mock as _mock
        with _mock.patch.object(wb_media_client, "generate_image",
                                return_value={"url": "u", "credit": 5.71}):
            self.post()
        self.assertAlmostEqual(P.media_key_spend("k-1"), 5.71, places=2)


class IdentityWiringTests(unittest.TestCase):
    def test_identity_reaches_the_wire(self):
        """`media_identity` 配置必须真的传到出站头——曾经只是存了、没人读。"""
        saved = wb_media_client.current_identity()
        try:
            wb_media_client.set_identity("cli")
            self.assertEqual(wb_media_client.current_identity(), "cli")
            wb_media_client.set_identity("nonsense")      # 未知值收敛成 None
            self.assertIsNone(wb_media_client.current_identity())
        finally:
            wb_media_client.set_identity(saved)


class ReferenceValidationOrderTests(MediaRouteCases):
    """数量/互斥校验必须在**下载之前**发生。

    否则客户端拿十个坏地址就能让我们白下载一轮，才收到"最多 9 个"。
    断言方式是给一个必然下载失败的地址：只要报的是数量错而不是下载错，
    就说明顺序对了。
    """

    def test_count_is_checked_before_any_download(self):
        with mock.patch.object(wb_media_client, "resolve_input_image",
                               side_effect=AssertionError("不该走到下载")):
            handler = self.post(path="/v1/videos",
                                payload={"prompt": "x", "model": "minimax-video-h3",
                                         "reference_images":
                                         ["https://e.com/%d.png" % i for i in range(10)]})
        code, message = handler.sent[0], handler.sent[1]
        self.assertEqual(code, 400)
        self.assertIn("9", json.dumps(message, ensure_ascii=False) if not isinstance(message, str) else message)

    def test_frame_and_reference_conflict_is_checked_first(self):
        with mock.patch.object(wb_media_client, "resolve_input_image",
                               side_effect=AssertionError("不该走到下载")):
            handler = self.post(path="/v1/videos",
                                payload={"prompt": "x", "model": "minimax-video-h3",
                                         "image": "https://e.com/f.png",
                                         "reference_images": ["https://e.com/a.png"]})
        code, message = handler.sent[0], handler.sent[1]
        self.assertEqual(code, 400)
        self.assertIn("互斥", json.dumps(message, ensure_ascii=False) if not isinstance(message, str) else message)


class PriceLedgerTests(unittest.TestCase):
    """实测单价账本：只从"实扣 ÷ 秒数"学，学不到就不用。"""

    def setUp(self):
        self.saved = dict(P._MEDIA_PRICES)
        self.saved_path = P._MEDIA_PRICES_PATH
        P._MEDIA_PRICES.clear()
        # 用例不落盘：路径设 None，_save 会直接返回
        P._MEDIA_PRICES_PATH = None

    def tearDown(self):
        P._MEDIA_PRICES.clear()
        P._MEDIA_PRICES.update(self.saved)
        P._MEDIA_PRICES_PATH = self.saved_path

    def test_learns_from_actual_charge(self):
        price = P.learn_media_price("minimax-video-h3", "768P", 57.14, 4)
        self.assertAlmostEqual(price, 14.285, places=3)
        self.assertAlmostEqual(P.media_price_per_second("minimax-video-h3", "768P"), 14.285, places=3)
        # 学到之后估算就该按实测价，而不是 26/秒
        self.assertAlmostEqual(P.media_cost_hint("minimax-video-h3", 4, "768P"), 57.14, places=2)

    def test_refuses_to_guess(self):
        """缺 credit 或秒数就不记——错的价格比没有价格更坏。"""
        for credit, seconds in ((None, 4), (0, 4), (57.14, None), (57.14, 0), ("x", 4)):
            self.assertIsNone(P.learn_media_price("m", "768P", credit, seconds))
        self.assertIsNone(P.media_price_per_second("m", "768P"))

    def test_other_resolution_uses_model_max(self):
        """同模型别的分辨率：取最大值。高估只是少派一次，低估会把账号打穿。"""
        P.learn_media_price("kling-video-v3", "720P", 128.57, 5)      # 25.714/秒
        P.learn_media_price("kling-video-v3", "4K", 257.14, 5)        # 51.428/秒
        self.assertAlmostEqual(P.media_price_per_second("kling-video-v3", "1080P"), 51.428, places=3)

    def test_unknown_model_has_no_price(self):
        self.assertIsNone(P.media_price_per_second("never-seen", "720P"))
        # 于是估算退回上界
        self.assertEqual(P.media_cost_hint("never-seen", 5), 130.0)

    def test_price_survives_restart(self):
        """账本要落盘，否则每次重启都退回 26/秒的上界。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "video-prices.json")
            P._MEDIA_PRICES_PATH = path
            P.learn_media_price("minimax-video-h3", "768P", 57.14, 4)
            P._MEDIA_PRICES.clear()
            P._MEDIA_PRICES_PATH = path
            # 模拟重启：清空内存后重新读文件
            P._MEDIA_PRICES.clear()
            saved = P.media_prices_path
            P.media_prices_path = lambda: path
            try:
                P.load_media_prices()
            finally:
                P.media_prices_path = saved
            self.assertAlmostEqual(P.media_price_per_second("minimax-video-h3", "768P"),
                                   14.285, places=3)


class ImagePriceTests(unittest.TestCase):
    """生图也是"用一个上界当价格"，同样要学实测值。"""

    def setUp(self):
        self.saved = dict(P._MEDIA_IMAGE_PRICES)
        self.saved_path = P._MEDIA_IMAGE_PRICES_PATH
        P._MEDIA_IMAGE_PRICES.clear()
        P._MEDIA_IMAGE_PRICES_PATH = None

    def tearDown(self):
        P._MEDIA_IMAGE_PRICES.clear()
        P._MEDIA_IMAGE_PRICES.update(self.saved)
        P._MEDIA_IMAGE_PRICES_PATH = self.saved_path

    def test_learns_actual_image_charge(self):
        self.assertIsNone(P.media_image_price("hunyuan-image-alpha"))
        # 观测前用 6.0 上界，观测后用实扣
        self.assertEqual(P.media_image_cost_hint("hunyuan-image-alpha"), 6.0)
        P.learn_image_price("hunyuan-image-alpha", 5.71)
        self.assertAlmostEqual(P.media_image_price("hunyuan-image-alpha"), 5.71)
        self.assertAlmostEqual(P.media_image_cost_hint("hunyuan-image-alpha"), 5.71)

    def test_never_guesses(self):
        for bad in (None, 0, -1, "x"):
            self.assertIsNone(P.learn_image_price("m", bad))
        self.assertIsNone(P.media_image_price("m"))

    def test_video_and_image_ledgers_do_not_mix(self):
        """一个按秒、一个按张，混表迟早把单位搞错。"""
        P.learn_image_price("minimax-video-h3", 5.71)
        self.assertIsNone(P.media_price_per_second("minimax-video-h3", "768P"))
        P.learn_media_price("minimax-video-h3", "768P", 57.14, 4)
        self.assertAlmostEqual(P.media_image_price("minimax-video-h3"), 5.71)


class VideoConcurrencyTests(MediaRouteCases):
    """并发生视频：满了就排队（不提交、不花钱），并行任务摊到不同账号。"""

    def setUp(self):
        super(VideoConcurrencyTests, self).setUp()
        self.saved_tasks = P._MEDIA_TASKS
        _STORE_SEQ[0] += 1
        self.store = T.MediaTaskStore(
            out_dir=os.path.join(_TMP.name, "conc-%d" % _STORE_SEQ[0]),
            client=object(), resolve_account=lambda uid: FakeAccount(uid),
            autostart=False)
        P._MEDIA_TASKS = self.store

    def tearDown(self):
        P._MEDIA_TASKS = self.saved_tasks
        self.store.stop()

    def test_full_concurrency_queues_without_submitting(self):
        store = self.store
        store.concurrency = 2
        # 两条已经在跑
        store.create("kling-video-v3", {"seconds": 5}, "u-1", {"prompt": "a"}, "up-1")
        store.create("kling-video-v3", {"seconds": 5}, "u-2", {"prompt": "b"}, "up-2")
        with mock.patch.object(wb_media_client, "submit_video",
                               side_effect=AssertionError("并发满时不该提交")):
            handler = self.post(path="/v1/videos",
                                payload={"prompt": "c", "model": "minimax-video-h3",
                                         "seconds": 4})
        code, body = handler.sent
        self.assertEqual(code, 202)
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["queue_position"], 1)
        self.assertIn("未扣积分", body["hint"])

    def test_parallel_submissions_avoid_busy_accounts(self):
        store = self.store
        store.concurrency = 3
        store.create("kling-video-v3", {"seconds": 5}, "u-1", {"prompt": "a"}, "up-1")
        picked = []

        def fake_pick(self_, realm, model_id, cost_hint, excluded):
            picked.append(set(excluded))
            return FakeAccount("u-9")

        with mock.patch.object(FakeHandler, "_pick_media_account", fake_pick),                 mock.patch.object(wb_media_client, "submit_video",
                                  return_value={"taskId": "up-9"}):
            handler = self.post(path="/v1/videos",
                                payload={"prompt": "c", "model": "minimax-video-h3",
                                         "seconds": 4})
        code, _body = handler.sent
        self.assertEqual(code, 202)
        # 第一条抛出的"要避开的账号"里必须包含 u-1。
        self.assertTrue(any("u-1" in avoided for avoided in picked), picked)


class IsolationTests(unittest.TestCase):
    """整个设计里最大的两个承诺，各要一条能失败的证据。"""

    def test_media_failure_does_not_park_the_account_for_chat(self):
        """画图失败只让「这个账号的这个图像模型」冷却，同账号的 chat 照常可用。

        这条靠的是 `note_error(model=…)` 命中那个分支后**直接 return**，不碰账号级
        `cooldown_until`。改掉那个 return 这条就会红。
        """
        account = _real_account()
        account.note_error("image failed", cooldown=60, model="hunyuan-image-alpha")
        self.assertFalse(account.ready(model="hunyuan-image-alpha"))
        self.assertTrue(account.ready(model="glm-5.3"))

    def test_chat_level_cooldown_still_parks_media_too(self):
        """反过来：账号级冷却（chat 那儿真出问题）本来就该连媒体一起挡住。"""
        account = _real_account()
        account.note_error("account level", cooldown=60)
        self.assertFalse(account.ready(model="hunyuan-image-alpha"))
        self.assertFalse(account.ready(model="glm-5.3"))

    def test_the_singleton_injects_an_account_resolver(self):
        """`media_tasks()` 造出来的任务表必须带 resolver。

        少了它，worker 永远找不到账号，任务会在提交成功后立刻变 lost——
        而提交的那一下**已经扣过钱**了。这个 bug 真出现过（见
        `wb_media_tasks._account_for` 的注释）。
        """
        saved = P._MEDIA_TASKS
        try:
            P._MEDIA_TASKS = None
            store = P.media_tasks()
            self.assertIs(P._MEDIA_TASKS, store)
            self.assertIsNotNone(store._resolve_account)
            self.assertIs(P.media_account_by_uid, store._resolve_account)
        finally:
            if P._MEDIA_TASKS is not None:
                P._MEDIA_TASKS.stop()
            P._MEDIA_TASKS = saved

    def test_media_slots_are_an_independent_pool(self):
        """媒体槽是一组独立的信号量：拿满 `MEDIA_SLOTS` 个之后第 N+1 个就失败。

        这条不能证明"chat 的槽没被动"（那要读 CHAT 的信号量），但它证明媒体**用的是
        自己的一组槽**——`_CHAT_SLOTS_SEM` 与 `_MEDIA_SLOTS_SEM` 是两个对象，媒体这边
        没有一行代码碰前者。
        """
        taken = 0
        try:
            while P.acquire_media_slot():
                taken += 1
                if taken > 100:
                    break
            self.assertEqual(taken, P.MEDIA_SLOTS)
            self.assertFalse(P.acquire_media_slot())         # 满了就是拿不到
            self.assertEqual(P.media_inflight(), P.MEDIA_SLOTS)
        finally:
            for _ in range(taken):
                P.release_media_slot()
        self.assertEqual(P.media_inflight(), 0)
        self.assertTrue(P.acquire_media_slot())              # 释放之后能再拿到
        P.release_media_slot()


def _real_account(uid="u-real-1"):
    """真的 Account 对象（隔离目录）：`note_error` 的模型级隔离要用真实实现才准。"""
    return wb_accounts.Account({
        "accessToken": "isolation-test-token",
        "uid": uid,
        "realm": "cn",
        "product": "workbuddy",
        "domain": "copilot.tencent.com",
    })


if __name__ == "__main__":
    unittest.main(verbosity=2)
