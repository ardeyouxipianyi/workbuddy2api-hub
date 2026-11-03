"""媒体协议层（wb_media）的纯逻辑测试：目录、请求体、参数、"知道才拦"、失败分类。

这个套件不碰网络、不碰磁盘、不碰账号——`wb_media` 本身就是零 I/O 的一层，
所以这里可以把「哪些参数在本地就被拦下」逐条钉死。

夹具用的是 **2026-11-01 从本机 `catalog/product-config-cn.json` 抄下来的真实形状**
（那份文件是运行时产物、被 .gitignore 忽略，测试不能依赖它）。两个视频模型的
`videoGeneration` 字段一个都没删——本机目录比 dsh 插件的文档更细，
`aspectRatios` / `maxPromptLength` / `audioMode` 这三项正是靠它才校验得动：

  - kling-video-v3：minSeconds 3、resolutions 720P/1080P/4K、audioMode switchable、
    maxPromptLength 2500
  - minimax-video-h3：minSeconds 4、resolutions 768P/2K、audioMode **always-on**、
    maxPromptLength **2000**

Run with: python tests/_test_media_logic.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_media as M


# ---------------------------------------------------------------- 夹具（真实形状）

def _kling_generation():
    return {
        "allowMixedReferenceMaterials": False,
        "aspectRatios": ["16:9", "9:16", "1:1"],
        "audioMode": "switchable",
        "defaultAspectRatio": "16:9",
        "defaultEnableAudio": True,
        "defaultResolution": "720P",
        "defaultSeconds": 5,
        "framesExclusiveWithReferenceMaterials": False,
        "lastFrameRequiresFirstFrame": True,
        "maxPromptLength": 2500,
        "maxReferenceAudios": 0,
        "maxReferenceImages": 0,
        "maxReferenceMaterials": 0,
        "maxReferenceVideos": 0,
        "maxSeconds": 15,
        "minSeconds": 3,
        "referenceAudioRequiresVisualMaterial": False,
        "resolutions": ["720P", "1080P", "4K"],
        "supportedSeconds": [],
        "supportsFirstFrame": True,
        "supportsFirstLastFrame": True,
    }


def _minimax_generation():
    return {
        "allowMixedReferenceMaterials": True,
        "aspectRatios": ["adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
        "audioMode": "always-on",
        "defaultAspectRatio": "adaptive",
        "defaultEnableAudio": True,
        "defaultResolution": "768P",
        "defaultSeconds": 5,
        "framesExclusiveWithReferenceMaterials": True,
        "lastFrameRequiresFirstFrame": True,
        "maxPromptLength": 2000,
        "maxReferenceAudios": 3,
        "maxReferenceImages": 9,
        "maxReferenceMaterials": 12,
        "maxReferenceVideos": 3,
        "maxSeconds": 15,
        "minSeconds": 4,
        "referenceAudioRequiresVisualMaterial": True,
        "resolutions": ["768P", "2K"],
        "supportedSeconds": [],
        "supportsFirstFrame": True,
        "supportsFirstLastFrame": True,
    }


def catalog_payload():
    """一份 /v3/config 的最小真实形状：两个聊天模型 + 四个媒体模型。"""
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "models": [
                {"id": "glm-5.3", "name": "GLM-5.3", "tags": ["craft"]},
                {"id": "deepseek-v4.1-flash", "name": "DeepSeek V4.1 Flash", "tags": ["craft"]},
                {"id": "hunyuan-image-alpha", "name": "Hunyuan Image Alpha",
                 "tags": ["text-to-image"]},
                {"id": "hunyuan-image-alpha-edit", "name": "Hunyuan Image Alpha Edit",
                 "tags": ["image-to-image"]},
                {"id": "kling-video-v3", "name": "Kling-Video-V3",
                 "tags": ["text-to-video", "image-to-video"],
                 "videoGeneration": _kling_generation()},
                {"id": "minimax-video-h3", "name": "MiniMax-H3",
                 "tags": ["text-to-video", "image-to-video"],
                 "videoGeneration": _minimax_generation()},
            ]
        },
    }


def models():
    return M.media_models(catalog_payload())


def model_by_id(model_id):
    for model in models():
        if model["id"] == model_id:
            return model
    raise AssertionError("夹具里没有 %s" % model_id)


def error_code_of(callable_obj, *args, **kwargs):
    """跑一次并返回 MediaError.code；没抛就失败。"""
    try:
        callable_obj(*args, **kwargs)
    except M.MediaError as exc:
        return exc.code
    raise AssertionError("期望抛 MediaError，但没有抛")


# ---------------------------------------------------------------- 目录

class InlineMaterialTests(unittest.TestCase):
    """参考视频/音频可以走 data URL（2026-11-02 实测上游接受）。

    原来网关只收公网 URL，理由是"data URL 对这两类没有意义"。实测推翻了它：
    3 张参考图 + 1 段 88KB mp4 的 data URL 提交后照常出片（扣 128.57）。
    真正要守的是体积闸门——base64 膨胀 4/3，一次请求还能带 3+3 个。
    """

    def test_is_data_url(self):
        self.assertTrue(M.is_data_url("data:video/mp4;base64,AAAA"))
        self.assertTrue(M.is_data_url("  DATA:audio/mp3;base64,AA"))
        self.assertFalse(M.is_data_url("https://example.com/a.mp4"))
        self.assertFalse(M.is_data_url("C:/tmp/a.mp4"))
        self.assertFalse(M.is_data_url(None))

    def test_size_gate_rejects_empty(self):
        with self.assertRaises(M.MediaError):
            M.assert_data_url_size("data:video/mp4;base64,", 1024, "参考视频")

    def test_size_gate_rejects_oversize(self):
        # 4 KB 原始字节 → base64 约 5.4 KB；上限给 1 KB 必拒
        payload = "data:video/mp4;base64," + ("A" * 5460)
        with self.assertRaises(M.MediaError):
            M.assert_data_url_size(payload, 1024, "参考视频")

    def test_size_gate_accepts_within_limit(self):
        payload = "data:video/mp4;base64," + ("A" * 120)
        M.assert_data_url_size(payload, 1024, "参考视频")   # 不抛就算过

    def test_size_counts_decoded_bytes_not_string_length(self):
        """按 base64 字符串长度卡会把合法文件误拒。90 字节原始 → 120 字符。"""
        payload = "data:video/mp4;base64," + ("A" * 120)
        self.assertEqual(M.decoded_base64_length(payload), 90)


class CatalogProjectionTests(unittest.TestCase):
    def test_reads_data_models_from_full_envelope(self):
        self.assertEqual(len(models()), 4)

    def test_accepts_payload_without_envelope(self):
        payload = {"models": catalog_payload()["data"]["models"]}
        self.assertEqual(len(M.media_models(payload)), 4)

    def test_accepts_data_only(self):
        payload = catalog_payload()["data"]
        self.assertEqual(len(M.media_models(payload)), 4)

    def test_rejects_garbage_payload(self):
        self.assertEqual(M.media_models(None), [])
        self.assertEqual(M.media_models("not a dict"), [])
        self.assertEqual(M.media_models({"data": {"models": "nope"}}), [])

    def test_drops_entries_without_tags(self):
        payload = {"data": {"models": [
            {"id": "no-tags"},
            {"id": ""},
            {"tags": ["text-to-image"]},
            {"id": "ok", "tags": ["text-to-image"]},
        ]}}
        ids = [model["id"] for model in M.media_models(payload)]
        self.assertEqual(ids, ["ok"])

    def test_dedupes_by_id_keeping_first(self):
        payload = {"data": {"models": [
            {"id": "dup", "name": "first", "tags": ["text-to-image"]},
            {"id": "dup", "name": "second", "tags": ["text-to-image"]},
        ]}}
        out = M.media_models(payload)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "first")

    def test_tag_filter_single_and_list(self):
        self.assertEqual(
            [m["id"] for m in M.media_models(catalog_payload(), M.TAG_TEXT_TO_IMAGE)],
            ["hunyuan-image-alpha"])
        # 一个模型同时带两个 video tag，过滤两组会各返回一次同一模型（分组的语义）
        video_ids = [m["id"] for m in M.media_models(
            catalog_payload(), [M.TAG_TEXT_TO_VIDEO, M.TAG_IMAGE_TO_VIDEO])]
        self.assertEqual(video_ids, ["kling-video-v3", "minimax-video-h3"])

    def test_video_generation_transfers_every_field_we_use(self):
        """本机目录比 dsh 文档细的那三项必须原样搬过来，否则校验是死代码。"""
        generation = model_by_id("minimax-video-h3")["videoGeneration"]
        self.assertEqual(generation["audioMode"], "always-on")
        self.assertEqual(generation["maxPromptLength"], 2000)
        self.assertEqual(generation["aspectRatios"][0], "adaptive")
        self.assertEqual(generation["resolutions"], ["768P", "2K"])
        self.assertEqual(generation["minSeconds"], 4)
        self.assertEqual(generation["maxSeconds"], 15)
        self.assertIs(generation["defaultEnableAudio"], True)
        self.assertEqual(generation["defaultResolution"], "768P")

    def test_image_models_carry_no_video_generation(self):
        self.assertNotIn("videoGeneration", model_by_id("hunyuan-image-alpha"))

    def test_read_video_generation_omits_missing_fields(self):
        self.assertIsNone(M.read_video_generation(None))
        self.assertIsNone(M.read_video_generation({}))
        self.assertIsNone(M.read_video_generation("nope"))
        partial = M.read_video_generation({"minSeconds": 3})
        self.assertEqual(partial, {"minSeconds": 3})


class PickModelTests(unittest.TestCase):
    def test_defaults_to_first_in_group(self):
        self.assertEqual(
            M.pick_model(models(), None, M.TAG_TEXT_TO_IMAGE)["id"], "hunyuan-image-alpha")
        self.assertEqual(
            M.pick_model(models(), None, M.TAG_TEXT_TO_VIDEO)["id"], "kling-video-v3")

    def test_accepts_requested_member(self):
        self.assertEqual(
            M.pick_model(models(), "minimax-video-h3", M.TAG_TEXT_TO_VIDEO)["id"],
            "minimax-video-h3")

    def test_rejects_model_from_another_group(self):
        """拿文生图模型去改图：报错要说出「图生图」并列出可用项。"""
        try:
            M.pick_model(models(), "hunyuan-image-alpha", M.TAG_IMAGE_TO_IMAGE)
        except M.MediaError as exc:
            self.assertEqual(exc.code, "NO_MEDIA_MODEL")
            self.assertIn("图生图", exc.message)
            self.assertIn("hunyuan-image-alpha-edit", exc.message)
        else:
            raise AssertionError("跨组点名应当报错")

    def test_rejects_unknown_name(self):
        self.assertEqual(
            error_code_of(M.pick_model, models(), "nope", M.TAG_TEXT_TO_IMAGE),
            "NO_MEDIA_MODEL")

    def test_reports_empty_group(self):
        payload = {"data": {"models": [
            {"id": "glm-5.3", "tags": ["craft"]},
        ]}}
        self.assertEqual(
            error_code_of(M.pick_model, M.media_models(payload), None, M.TAG_TEXT_TO_VIDEO),
            "NO_MEDIA_MODEL")

    def test_model_with_two_video_tags_picks_from_either(self):
        kling = model_by_id("kling-video-v3")
        self.assertIn(M.TAG_TEXT_TO_VIDEO, kling["tags"])
        self.assertIn(M.TAG_IMAGE_TO_VIDEO, kling["tags"])


# ---------------------------------------------------------------- 尺寸

class SizeTests(unittest.TestCase):
    def test_parses_plain_and_upper_x(self):
        self.assertEqual(M.parse_size("1024x1024"), (1024, 1024))
        self.assertEqual(M.parse_size("1024X1536"), (1024, 1536))
        self.assertEqual(M.parse_size(" 2048 x 1152 "), (2048, 1152))

    def test_auto_and_missing_fall_back_to_default(self):
        self.assertEqual(M.check_size("auto"), M.DEFAULT_IMAGE_SIZE)
        self.assertEqual(M.check_size(None), M.DEFAULT_IMAGE_SIZE)

    def test_accepts_dsh_measured_boundary(self):
        """dsh 逐档实测通过的那几档。"""
        for size in ("1024x1024", "1024x1536", "1536x1024", "2048x1152",
                     "2048x1536", "2560x1440", "2560x1600", "2048x2048"):
            self.assertEqual(M.check_size(size), size)

    def test_rejects_above_pixel_cap(self):
        for size in ("2560x2560", "3072x3072", "4096x4096"):
            self.assertEqual(error_code_of(M.check_size, size), "INVALID_ARGS")

    def test_rejects_malformed(self):
        for size in ("abc", "1024", "1024x", "x1024", "0x0", "1024x1024x2", "-1x100"):
            self.assertEqual(error_code_of(M.check_size, size), "INVALID_ARGS")

    def test_pixel_cap_message_mentions_both_numbers(self):
        try:
            M.check_size("2560x2560")
        except M.MediaError as exc:
            self.assertIn("6.55 MP", exc.message)
            self.assertIn("4.19 MP", exc.message)
        else:
            raise AssertionError("应当拒绝 2560x2560")


# ---------------------------------------------------------------- 生图请求体

class ImageBodyTests(unittest.TestCase):
    def test_hunyuan_branch_adds_n_only(self):
        body = M.build_image_body("hunyuan-image-alpha", "一只猫", "1024x1024")
        self.assertEqual(body["n"], 1)
        self.assertNotIn("response_format", body)
        self.assertEqual(body["size"], "1024x1024")

    def test_gemini_branch_adds_nothing(self):
        body = M.build_image_body("gemini-image-x", "一只猫", "1024x1024")
        self.assertNotIn("n", body)
        self.assertNotIn("response_format", body)

    def test_other_branch_requests_b64(self):
        body = M.build_image_body("hy-image-3.5", "一只猫", "1024x1024")
        self.assertEqual(body["response_format"], "b64_json")
        self.assertNotIn("n", body)

    def test_rejects_batch(self):
        """一次一张是刻意的花钱限制，静默只给一张比报错更糟。"""
        self.assertEqual(
            error_code_of(M.build_image_body, "hunyuan-image-alpha", "猫", "1024x1024", 4),
            "INVALID_ARGS")

    def test_requires_prompt(self):
        self.assertEqual(
            error_code_of(M.build_image_body, "hunyuan-image-alpha", "  ", "1024x1024"),
            "INVALID_ARGS")

    def test_size_is_normalized_in_body(self):
        body = M.build_image_body("hunyuan-image-alpha", "猫", "AUTO")
        self.assertEqual(body["size"], "1024x1024")


class EditBodyTests(unittest.TestCase):
    DATA_URL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="

    def test_appends_image_array(self):
        body = M.build_edit_body("hunyuan-image-alpha-edit", "改成油画", "1024x1024",
                                 [self.DATA_URL])
        self.assertEqual(body["image"], [self.DATA_URL])
        self.assertEqual(body["n"], 1)

    def test_rejects_empty_image_list(self):
        self.assertEqual(
            error_code_of(M.build_edit_body, "hunyuan-image-alpha-edit", "x", "1024x1024", []),
            "INVALID_ARGS")

    def test_rejects_raw_base64_without_prefix(self):
        """上游只认 data URL 数组；裸 base64 在这里就该被挡，不该发上去换 400。"""
        self.assertEqual(
            error_code_of(M.build_edit_body, "hunyuan-image-alpha-edit", "x", "1024x1024",
                          ["iVBORw0KGgo="]),
            "INVALID_ARGS")


# ---------------------------------------------------------------- 视频参数

class VideoParamTests(unittest.TestCase):
    def kling(self):
        return model_by_id("kling-video-v3")["videoGeneration"]

    def minimax(self):
        return model_by_id("minimax-video-h3")["videoGeneration"]

    def resolve(self, request, generation):
        return M.resolve_video_params(request, generation)

    def test_prompt_is_required(self):
        for bad in (None, "", "   "):
            self.assertEqual(
                error_code_of(self.resolve, {"prompt": bad}, self.kling()), "INVALID_ARGS")

    def test_defaults_from_model(self):
        params = self.resolve({"prompt": "海边日落"}, self.kling())
        self.assertEqual(params["seconds"], 5)              # defaultSeconds
        self.assertEqual(params["resolution"], "720P")      # defaultResolution
        self.assertEqual(params["aspect_ratio"], "16:9")    # defaultAspectRatio
        self.assertTrue(params["enable_audio"])
        self.assertFalse(params["image_to_video"])
        self.assertEqual(params["warnings"], [])

    def test_seconds_range_differs_per_model(self):
        """同一个函数、不同模型不同边界——这正是逐模型读目录的意义。"""
        self.assertEqual(self.resolve({"prompt": "x", "seconds": 3}, self.kling())["seconds"], 3)
        self.assertEqual(error_code_of(self.resolve, {"prompt": "x", "seconds": 2}, self.kling()),
                         "INVALID_ARGS")
        self.assertEqual(error_code_of(self.resolve, {"prompt": "x", "seconds": 3}, self.minimax()),
                         "INVALID_ARGS")
        self.assertEqual(self.resolve({"prompt": "x", "seconds": 4}, self.minimax())["seconds"], 4)

    def test_seconds_accepts_openai_string_form(self):
        self.assertEqual(self.resolve({"prompt": "x", "seconds": "8"}, self.kling())["seconds"], 8)

    def test_seconds_rejects_non_numeric(self):
        for value in ("abc", "", True, {"a": 1}):
            self.assertEqual(
                error_code_of(self.resolve, {"prompt": "x", "seconds": value}, self.kling()),
                "INVALID_ARGS")

    def test_seconds_passthrough_when_model_has_no_range(self):
        """拿不到 videoGeneration 时不盲拒：原样交给上游判。"""
        params = self.resolve({"prompt": "x", "seconds": 99}, None)
        self.assertEqual(params["seconds"], 99)

    def test_resolution_case_insensitive_keeps_catalog_spelling(self):
        params = self.resolve({"prompt": "x", "resolution": "1080p"}, self.kling())
        self.assertEqual(params["resolution"], "1080P")

    def test_unsupported_resolution_falls_back_with_warning(self):
        params = self.resolve({"prompt": "x", "resolution": "480P"}, self.kling())
        self.assertEqual(params["resolution"], "720P")
        self.assertEqual(len(params["warnings"]), 1)
        self.assertIn("480P", params["warnings"][0])

    def test_resolution_passthrough_when_catalog_has_none(self):
        params = self.resolve({"prompt": "x", "resolution": "1080P"}, {"defaultSeconds": 5})
        self.assertEqual(params["resolution"], "1080P")
        self.assertEqual(params["warnings"], [])

    def test_size_maps_to_resolution(self):
        params = self.resolve({"prompt": "x", "size": "1920x1080"}, self.kling())
        self.assertEqual(params["resolution"], "1080P")

    def test_explicit_resolution_beats_size(self):
        params = self.resolve({"prompt": "x", "size": "1280x720", "resolution": "4K"}, self.kling())
        self.assertEqual(params["resolution"], "4K")

    def test_aspect_ratio_derived_from_size(self):
        self.assertEqual(
            self.resolve({"prompt": "x", "size": "720x1280"}, self.kling())["aspect_ratio"], "9:16")
        self.assertEqual(
            self.resolve({"prompt": "x", "size": "1024x1024"}, self.kling())["aspect_ratio"], "1:1")

    def test_aspect_ratio_absent_for_image_to_video(self):
        params = self.resolve({"prompt": "x", "image": "data:image/png;base64,AAA"}, self.kling())
        self.assertTrue(params["image_to_video"])
        self.assertIsNone(params["aspect_ratio"])

    def test_input_reference_counts_as_image(self):
        params = self.resolve({"prompt": "x", "input_reference": "https://a/b.png"}, self.kling())
        self.assertTrue(params["image_to_video"])

    def test_always_on_audio_warns_and_stays_on(self):
        params = self.resolve({"prompt": "x", "enable_audio": False}, self.minimax())
        self.assertTrue(params["enable_audio"])
        self.assertTrue(any("always-on" in warning for warning in params["warnings"]))

    def test_switchable_audio_can_be_turned_off(self):
        params = self.resolve({"prompt": "x", "enable_audio": False}, self.kling())
        self.assertFalse(params["enable_audio"])

    def test_prompt_limit_is_per_model(self):
        long_prompt = "字" * 2200
        self.assertEqual(self.resolve({"prompt": long_prompt}, self.kling())["seconds"], 5)
        self.assertEqual(
            error_code_of(self.resolve, {"prompt": long_prompt}, self.minimax()),
            "INVALID_ARGS")

    def test_negative_prompt_uses_same_limit(self):
        self.assertEqual(
            error_code_of(self.resolve, {"prompt": "x", "negative_prompt": "字" * 2100},
                          self.minimax()),
            "INVALID_ARGS")


class SizeToResolutionTests(unittest.TestCase):
    def test_short_edge_decides(self):
        self.assertEqual(M.size_to_resolution("1280x720", ["720P", "1080P", "4K"]), ("720P", None))
        self.assertEqual(M.size_to_resolution("720x1280", ["720P", "1080P", "4K"]), ("720P", None))

    def test_picks_highest_not_above(self):
        self.assertEqual(M.size_to_resolution("1920x1080", ["720P", "1080P", "4K"])[0], "1080P")
        self.assertEqual(M.size_to_resolution("2560x1440", ["768P", "2K"])[0], "2K")

    def test_below_lowest_falls_back(self):
        name, warning = M.size_to_resolution("640x480", ["768P", "2K"], "768P")
        self.assertEqual(name, "768P")
        self.assertIn("640x480", warning)

    def test_empty_catalog_list(self):
        name, warning = M.size_to_resolution("1280x720", [], "720P")
        self.assertEqual(name, "720P")
        self.assertIsNotNone(warning)


class VideoBodyTests(unittest.TestCase):
    def build(self, request, generation):
        params = M.resolve_video_params(request, generation)
        return M.build_video_body("kling-video-v3", request, params)

    def test_text_to_video_shape(self):
        body = self.build({"prompt": "海边日落", "seconds": 5},
                          model_by_id("kling-video-v3")["videoGeneration"])
        self.assertIs(body["watermark"], False)  # 网关口径：始终不带水印
        self.assertEqual(body["negative_prompt"], "")
        self.assertEqual(body["extra_parameters"]["resolution"], "720P")
        self.assertEqual(body["extra_parameters"]["aspect_ratio"], "16:9")
        self.assertNotIn("image_url", body)

    def test_watermark_always_off_and_not_overridable(self):
        """水印固定关闭，且**不接受调用方覆盖**。

        这是网关的统一口径：上游 `watermark=false` 后画面左上角没有"AI生成"
        （实测）。既然不对外暴露这个选项，那么传什么都得是 false——传 true
        也必须被忽略，否则等于偷偷开了一个后门。
        """
        generation = model_by_id("kling-video-v3")["videoGeneration"]
        for value in (True, "true", "yes", 1, "", "随便写", None, False, "false", 0):
            body = self.build({"prompt": "x", "watermark": value}, generation)
            self.assertIs(body["watermark"], False,
                          "watermark=%r 也必须产出 false" % (value,))
        self.assertIs(self.build({"prompt": "x"}, generation)["watermark"], False)

    def test_image_to_video_drops_aspect_ratio(self):
        body = self.build({"prompt": "动起来", "image": "https://a/b.png"},
                          model_by_id("kling-video-v3")["videoGeneration"])
        self.assertEqual(body["image_url"], "https://a/b.png")
        self.assertNotIn("aspect_ratio", body["extra_parameters"])

    def test_last_image_rides_along(self):
        body = self.build({"prompt": "x", "image": "https://a/b.png",
                           "last_image": "https://a/c.png"},
                          model_by_id("kling-video-v3")["videoGeneration"])
        self.assertEqual(body["last_image_url"], "https://a/c.png")

    def test_input_reference_maps_to_image_url(self):
        body = self.build({"prompt": "x", "input_reference": "https://a/b.png"},
                          model_by_id("kling-video-v3")["videoGeneration"])
        self.assertEqual(body["image_url"], "https://a/b.png")


# ---------------------------------------------------------------- 学区间

class LearnedRangeTests(unittest.TestCase):
    def test_reads_the_real_upstream_wording(self):
        text = ("Create video failed with error: unsupported video duration: 1 seconds, "
                "supported range: 4-15 seconds")
        self.assertEqual(M.parse_supported_seconds_range(text), (4, 15))

    def test_accepts_alternate_separators(self):
        self.assertEqual(M.parse_supported_seconds_range("supported range: 3 to 12"), (3, 12))
        self.assertEqual(M.parse_supported_seconds_range("supported range: 3~12"), (3, 12))
        self.assertEqual(M.parse_supported_seconds_range("supported range: 3\u201312"), (3, 12))

    def test_rejects_silly_ranges(self):
        self.assertIsNone(M.parse_supported_seconds_range("supported range: 15-4"))
        self.assertIsNone(M.parse_supported_seconds_range("supported range: 0-15"))

    def test_returns_none_when_absent(self):
        self.assertIsNone(M.parse_supported_seconds_range("aspect ratio not supported"))
        self.assertIsNone(M.parse_supported_seconds_range(""))
        self.assertIsNone(M.parse_supported_seconds_range(None))


# ---------------------------------------------------------------- 响应解析

class ImageResponseTests(unittest.TestCase):
    def test_url_form(self):
        out = M.parse_image_response({"code": 0, "data": {
            "created": 1762000000,
            "data": [{"url": "https://cos/x.png"}],
            "usage": {"credit": 5.71}}})
        self.assertEqual(out["url"], "https://cos/x.png")
        self.assertEqual(out["credit"], 5.71)
        self.assertEqual(out["created"], 1762000000)

    def test_b64_form(self):
        out = M.parse_image_response({"code": 0, "data": {
            "data": [{"b64_json": "AAA"}], "usage": {"credit": 0.03}}})
        self.assertEqual(out["b64"], "AAA")
        self.assertEqual(out["credit"], 0.03)

    def test_small_credit_is_not_flattened(self):
        """0.03 不能被抹成 0——intl 的单价就是这个量级。"""
        out = M.parse_image_response({"code": 0, "data": {
            "data": [{"url": "u"}], "usage": {"credit": 0.03}}})
        self.assertEqual(out["credit"], 0.03)

    def test_nonzero_code_is_upstream_error_even_on_http_200(self):
        self.assertEqual(
            error_code_of(M.parse_image_response,
                          {"code": 11158, "msg": "pro only", "data": {}}),
            "UPSTREAM")

    def test_empty_result(self):
        self.assertEqual(
            error_code_of(M.parse_image_response, {"code": 0, "data": {"data": []}}),
            "EMPTY_RESULT")

    def test_error_includes_request_id(self):
        try:
            M.parse_image_response({"code": 7, "msg": "bad", "request_id": "rid-1", "data": {}})
        except M.MediaError as exc:
            self.assertIn("rid-1", exc.message)
            self.assertIn("7", exc.message)
        else:
            raise AssertionError("应当抛 MediaError")

    def test_revised_prompt_is_carried(self):
        out = M.parse_image_response({"code": 0, "data": {
            "data": [{"url": "u", "revised_prompt": "a cat"}]}})
        self.assertEqual(out["revised_prompt"], "a cat")


class VideoResponseTests(unittest.TestCase):
    def test_submission_returns_task_id(self):
        self.assertEqual(
            M.parse_video_submission({"code": 0, "data": {"id": "t-1"}}), {"taskId": "t-1"})

    def test_submission_without_id(self):
        self.assertEqual(
            error_code_of(M.parse_video_submission, {"code": 0, "data": {}}), "EMPTY_RESULT")

    def test_submission_envelope_error(self):
        self.assertEqual(
            error_code_of(M.parse_video_submission, {"code": 14407, "msg": "bad", "data": {}}),
            "UPSTREAM")

    def test_completed_carries_videos_and_credit(self):
        out = M.parse_video_task({"code": 0, "data": {
            "status": "completed",
            "data": [{"url": "https://cos/a.mp4"}, {"url": "https://cos/b.mp4"}],
            "usage": {"credit": 71.43}}})
        self.assertEqual(out["status"], "completed")
        self.assertEqual(len(out["videos"]), 2)
        self.assertEqual(out["credit"], 71.43)

    def test_completed_without_videos_is_not_treated_as_done(self):
        """上层据此继续轮询/报 EMPTY_RESULT，而不是当成成功。"""
        out = M.parse_video_task({"code": 0, "data": {"status": "completed", "data": []}})
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["videos"], [])

    def test_unknown_status_counts_as_not_finished(self):
        out = M.parse_video_task({"code": 0, "data": {"status": "weird-new-state"}})
        self.assertEqual(out["videos"], [])
        self.assertEqual(out["status"], "weird-new-state")

    def test_failed_surfaces_error_text(self):
        out = M.parse_video_task({"code": 0, "data": {
            "status": "failed", "error": "content rejected"}})
        self.assertEqual(out["error"], "content rejected")


# ---------------------------------------------------------------- 失败分类

class FailureClassificationTests(unittest.TestCase):
    def test_auth(self):
        for status in (401, 403):
            self.assertEqual(M.describe_media_failure(status, "x", "submit").code, "AUTH")

    def test_quota_rate_route(self):
        self.assertEqual(M.describe_media_failure(402, "x", "submit").code, "NO_CREDIT")
        self.assertEqual(M.describe_media_failure(429, "x", "submit").code, "RATE_LIMIT")
        self.assertEqual(M.describe_media_failure(404, "x", "submit").code, "NO_ROUTE")

    def test_submit_4xx_is_rejected(self):
        """建任务之前被退回：没扣积分，改完参数可以立刻重试 → 不进冷却。"""
        error = M.describe_media_failure(
            400, '{"code":14407,"msg":"unsupported video duration: 1 seconds, supported range: 4-15 seconds"}',
            "submit")
        self.assertEqual(error.code, "REJECTED")
        self.assertIn("supported range", error.message)
        self.assertIn("未扣积分", error.message)

    def test_same_400_during_poll_is_upstream(self):
        """轮询阶段同样的 400 已经花过钱了，不能算成免费失败。"""
        self.assertEqual(M.describe_media_failure(400, "bad", "poll").code, "UPSTREAM")

    def test_image_phase_400_is_upstream(self):
        self.assertEqual(M.describe_media_failure(400, "bad", "image").code, "UPSTREAM")

    def test_server_error(self):
        self.assertEqual(M.describe_media_failure(502, "bad gateway", "submit").code, "UPSTREAM")

    def test_envelope_message_extracted(self):
        error = M.describe_media_failure(400, '{"code":14407,"msg":"duration bad"}', "submit")
        self.assertIn("14407", error.message)
        self.assertIn("duration bad", error.message)

    def test_non_json_body_kept_as_is(self):
        error = M.describe_media_failure(500, "<html>oops</html>", "submit")
        self.assertIn("oops", error.message)

    def test_status_is_preserved_for_callers(self):
        self.assertEqual(M.describe_media_failure(429, "x", "submit").status, 429)


# ---------------------------------------------------------------- 输入图

class ImageInputTests(unittest.TestCase):
    DATA_URL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="

    def test_accepts_data_url(self):
        self.assertEqual(M.classify_image_input(self.DATA_URL), ("data_url", self.DATA_URL))

    def test_accepts_public_http_and_https(self):
        self.assertEqual(M.classify_image_input("https://a/b.png")[0], "url")
        self.assertEqual(M.classify_image_input("HTTP://a/b.png")[0], "url")

    def test_rejects_local_absolute_paths(self):
        """网关不是单用户插件：接受路径 = 任意文件读取（dsh 那种用法在这里是漏洞）。"""
        for value in ("C:\\Users\\admin\\.ssh\\id_rsa", "/etc/passwd",
                      "\\\\server\\share\\a.png"):
            self.assertEqual(error_code_of(M.classify_image_input, value), "INVALID_ARGS")

    def test_rejects_file_url(self):
        self.assertEqual(error_code_of(M.classify_image_input, "file:///C:/a.png"),
                         "INVALID_ARGS")

    def test_rejects_non_image_data_url(self):
        self.assertEqual(
            error_code_of(M.classify_image_input, "data:text/plain;base64,AAAA"),
            "INVALID_ARGS")

    def test_rejects_broken_data_url(self):
        for value in ("data:image/png,AAAA", "data:image/png;base64,"):
            self.assertEqual(error_code_of(M.classify_image_input, value), "INVALID_ARGS")

    def test_rejects_empty(self):
        for value in ("", "   ", None):
            self.assertEqual(error_code_of(M.classify_image_input, value), "INVALID_ARGS")

    def test_local_path_error_explains_why(self):
        try:
            M.classify_image_input("/etc/passwd")
        except M.MediaError as exc:
            self.assertIn("服务器", exc.message)
            self.assertIn("data:image", exc.message)
        else:
            raise AssertionError("应当拒绝本地路径")

    def test_decoded_length_uses_bytes_not_string_length(self):
        raw = b"\x89PNG" + b"x" * 600
        import base64
        encoded = base64.b64encode(raw).decode("ascii")
        text = "data:image/png;base64," + encoded
        self.assertEqual(M.decoded_base64_length(text), len(raw))
        self.assertGreater(len(encoded), len(raw))   # base64 更长，正是不能拿它比的原因

    def test_byte_gate(self):
        self.assertEqual(error_code_of(M.assert_input_image_bytes, 0), "INVALID_ARGS")
        self.assertEqual(error_code_of(M.assert_input_image_bytes, M.MAX_INPUT_IMAGE_BYTES + 1),
                         "INVALID_ARGS")
        M.assert_input_image_bytes(M.MAX_INPUT_IMAGE_BYTES)   # 边界值通过


# ---------------------------------------------------------------- 计费与对外形状

class CostAndShapeTests(unittest.TestCase):
    def test_image_cost(self):
        self.assertEqual(M.estimate_cost("image"), 6.0)

    def test_video_cost_scales_with_seconds(self):
        self.assertEqual(M.estimate_cost("video", 5), 130.0)
        self.assertEqual(M.estimate_cost("video", 1), 26.0)

    def test_video_cost_defaults_to_five_seconds(self):
        self.assertEqual(M.estimate_cost("video"), 130.0)

    def test_estimate_prefers_measured_price(self):
        """有实测单价就按实测算，别用那个一刀切的 26/秒。

        minimax 实测 14.2857/秒：用上界算 4 秒是 104，虚高 82%，余额紧时会把
        本来跑得起的调用挡掉。
        """
        self.assertEqual(M.estimate_cost("video", 4, price_per_second=14.2857), 57.14)
        self.assertEqual(M.estimate_cost("video", 5, price_per_second=25.7143), 128.57)

    def test_estimate_falls_back_to_ceiling_without_price(self):
        # 没有观测价时必须退回保守上界——低估会让账号被一次调用打穿。
        self.assertEqual(M.estimate_cost("video", 4), 104.0)
        self.assertEqual(M.estimate_cost("video", 4, price_per_second=0), 104.0)
        self.assertEqual(M.estimate_cost("video", 4, price_per_second=None), 104.0)

    def test_estimate_ignores_bad_price(self):
        for bad in ("abc", -1, 0, "", None):
            self.assertEqual(M.estimate_cost("video", 4, price_per_second=bad), 104.0)

    def test_unknown_kind_is_free(self):
        self.assertEqual(M.estimate_cost("audio"), 0.0)

    def test_status_mapping(self):
        self.assertEqual(M.status_out("queued"), "queued")
        self.assertEqual(M.status_out("running"), "in_progress")
        self.assertEqual(M.status_out("completed"), "completed")
        self.assertEqual(M.status_out("rejected"), "failed")
        self.assertEqual(M.status_out("lost"), "failed")
        self.assertEqual(M.status_out(None), "in_progress")

    def test_error_codes_distinguish_retryability(self):
        self.assertEqual(M.error_code_for("rejected"), "invalid_request")
        self.assertEqual(M.error_code_for("failed"), "upstream_error")
        self.assertEqual(M.error_code_for("lost"), "task_lost")
        self.assertIsNone(M.error_code_for("completed"))
        self.assertIsNone(M.error_code_for("running"))

    def test_public_task_running_has_no_error_and_no_url(self):
        out = M.public_task({"id": "t-1", "status": "running", "model": "kling-video-v3",
                             "created_at": 1, "seconds": 5, "resolution": "720P"})
        self.assertEqual(out["object"], "video")
        self.assertEqual(out["status"], "in_progress")
        self.assertEqual(out["seconds"], "5")          # OpenAI 侧是字符串
        self.assertNotIn("error", out)
        self.assertNotIn("url", out)

    def test_public_task_rejected_is_failed_invalid_request(self):
        out = M.public_task({"id": "t-1", "status": "rejected", "error": "duration bad"})
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["error"]["code"], "invalid_request")
        self.assertEqual(out["error"]["message"], "duration bad")

    def test_public_task_completed_carries_signed_url(self):
        out = M.public_task({"id": "t-1", "status": "completed",
                             "videos": ["/v1/videos/t-1/content?i=0"]},
                            content_url="/v1/videos/t-1/content?i=0&t=abc")
        self.assertEqual(out["url"], "/v1/videos/t-1/content?i=0&t=abc")
        self.assertNotIn("videos", out)

    def test_public_task_credit_survives(self):
        out = M.public_task({"id": "t", "status": "completed", "credit": 57.14})
        self.assertEqual(out["credit"], 57.14)


class ReferenceMaterialTests(unittest.TestCase):
    """参考素材：数量上限、首尾帧互斥、上游字段形状。

    这些断言钉的都是**实测出来的上游契约**（见 docs/MEDIA-IMPL.md §16）：
    `reference_images` 是 `[{image_url}]` 对象数组，不是字符串数组；数量上限来自
    目录的 `maxReferenceImages` 等字段；minimax 的首尾帧与参考素材互斥。
    """

    GENERATION = {
        "minSeconds": 4, "maxSeconds": 15, "defaultSeconds": 5,
        "resolutions": ["768P"], "aspectRatios": ["16:9"], "maxPromptLength": 2000,
        "maxReferenceImages": 9, "maxReferenceVideos": 3, "maxReferenceAudios": 3,
        "maxReferenceMaterials": 12, "allowMixedReferenceMaterials": True,
        "framesExclusiveWithReferenceMaterials": True, "supportsFirstFrame": True,
    }

    def body(self, request, generation=None):
        gen = dict(self.GENERATION if generation is None else generation)
        params = M.resolve_video_params(request, gen)
        return M.build_video_body("minimax-video-h3", request, params)

    def test_images_become_object_array(self):
        """上游要 `[{"image_url": …}]`。发成字符串数组会被 11101 拒掉。"""
        body = self.body({"prompt": "x", "reference_images": ["https://a/1.png"]})
        self.assertEqual(body["reference_images"], [{"image_url": "https://a/1.png"}])

    def test_accepts_the_upstream_shape_too(self):
        """客户端直接把上游形状贴过来也要认（省一层转换）。"""
        body = self.body({"prompt": "x",
                          "reference_images": [{"image_url": "https://a/1.png"}]})
        self.assertEqual(body["reference_images"], [{"image_url": "https://a/1.png"}])

    def test_images_alias(self):
        """`images` 是同一件事的顺口写法。"""
        body = self.body({"prompt": "x", "images": ["https://a/1.png"]})
        self.assertEqual(body["reference_images"], [{"image_url": "https://a/1.png"}])

    def test_videos_and_audios_use_their_own_url_keys(self):
        body = self.body({"prompt": "x",
                          "reference_videos": ["https://a/v.mp4"],
                          "reference_audios": ["https://a/a.mp3"]})
        self.assertEqual(body["reference_videos"], [{"video_url": "https://a/v.mp4"}])
        self.assertEqual(body["reference_audios"], [{"audio_url": "https://a/a.mp3"}])

    def test_over_the_category_cap_is_refused(self):
        with self.assertRaises(M.MediaError) as ctx:
            self.body({"prompt": "x", "reference_images": ["https://a/%d.png" % i
                                                           for i in range(10)]})
        self.assertIn("9", ctx.exception.message)

    def test_cap_of_zero_says_unsupported(self):
        """kling 的 maxReferenceImages 是 0：要提示"不支持"，不是"最多 0 个"。"""
        gen = dict(self.GENERATION, maxReferenceImages=0)
        with self.assertRaises(M.MediaError) as ctx:
            self.body({"prompt": "x", "reference_images": ["https://a/1.png"]}, gen)
        self.assertIn("不支持", ctx.exception.message)

    def test_total_cap_counts_every_kind(self):
        """三类加起来 13 > maxReferenceMaterials 12。"""
        request = {"prompt": "x",
                   "reference_images": ["https://a/%d.png" % i for i in range(9)],
                   "reference_videos": ["https://a/%d.mp4" % i for i in range(3)],
                   "reference_audios": ["https://a/0.mp3"]}
        with self.assertRaises(M.MediaError) as ctx:
            self.body(request)
        self.assertIn("12", ctx.exception.message)

    def test_frame_and_references_are_mutually_exclusive(self):
        """minimax 的 framesExclusiveWithReferenceMaterials 就是这条。"""
        with self.assertRaises(M.MediaError) as ctx:
            self.body({"prompt": "x", "image": "https://a/f.png",
                       "reference_images": ["https://a/1.png"]})
        self.assertIn("互斥", ctx.exception.message)

    def test_mixing_kinds_needs_permission(self):
        gen = dict(self.GENERATION, allowMixedReferenceMaterials=False,
                   framesExclusiveWithReferenceMaterials=False)
        with self.assertRaises(M.MediaError) as ctx:
            self.body({"prompt": "x", "reference_images": ["https://a/1.png"],
                       "reference_videos": ["https://a/v.mp4"]}, gen)
        self.assertIn("混用", ctx.exception.message)

    def test_mixing_is_fine_when_allowed(self):
        gen = dict(self.GENERATION, framesExclusiveWithReferenceMaterials=False)
        body = self.body({"prompt": "x", "reference_images": ["https://a/1.png"],
                          "reference_videos": ["https://a/v.mp4"]}, gen)
        self.assertIn("reference_images", body)
        self.assertIn("reference_videos", body)

    def test_missing_caps_mean_no_check(self):
        """目录没说上限就不拦——把判断交给上游。"""
        gen = {"minSeconds": 4, "maxSeconds": 15, "maxPromptLength": 2000}
        body = self.body({"prompt": "x",
                          "reference_images": ["https://a/%d.png" % i for i in range(30)]}, gen)
        self.assertEqual(len(body["reference_images"]), 30)

    def test_reference_images_switch_the_model_group(self):
        """有参考图＝有输入图：选模型要按 image-to-video 走。"""
        params = M.resolve_video_params(
            {"prompt": "x", "reference_images": ["https://a/1.png"]}, dict(self.GENERATION))
        self.assertTrue(params["image_to_video"])

    def test_reference_list_tolerates_junk_items(self):
        """一条读不出来的素材只丢它自己，不要让整单失败。"""
        params = M.resolve_video_params(
            {"prompt": "x", "reference_images": ["https://a/1.png", 42, {}]},
            dict(self.GENERATION))
        self.assertEqual(params["references"]["reference_images"], ["https://a/1.png"])




class ReferenceUploadTests(unittest.TestCase):
    """参考视频/音频的 data URL 必须在提交前换成上传后的 download_url。

    上游对 `video_url` 是真去下载的：data URL 抓不到，实测报
    `2013: content[4].video_url: media url unreachable`。官方客户端走的是
    「预签名 → PUT 到 COS → 把 download_url 填进去」，网关照抄这条路。
    """

    def setUp(self):
        import wb_proxy
        self.proxy = wb_proxy
        self.calls = []
        self._orig_upload = wb_proxy.wb_media_client.upload_reference_file
        self._orig_pick = wb_proxy._pick_media_account_for

    def tearDown(self):
        self.proxy.wb_media_client.upload_reference_file = self._orig_upload
        self.proxy._pick_media_account_for = self._orig_pick

    def _fake(self, account, data, filename):
        self.calls.append((len(data), filename))
        return "https://cos.example.com/uploaded-%d.mp4" % len(self.calls)

    def _patch(self):
        self.proxy._pick_media_account_for = lambda *a, **k: object()
        self.proxy.wb_media_client.upload_reference_file = self._fake

    def _data_url(self, payload=b"x" * 64, mime="video/mp4"):
        import base64
        return "data:%s;base64,%s" % (mime, base64.b64encode(payload).decode())

    def test_data_url_gets_uploaded_and_replaced(self):
        self._patch()
        body = {"reference_images": [{"image_url": "https://x/a.png"}],
                "reference_videos": [{"video_url": self._data_url()}]}
        out = self.proxy.resolve_reference_uploads(body, "cn", "minimax-video-h3")
        self.assertEqual(out["reference_videos"],
                         [{"video_url": "https://cos.example.com/uploaded-1.mp4"}])
        self.assertEqual(len(self.calls), 1)

    def test_public_url_passes_through_untouched(self):
        self._patch()
        body = {"reference_videos": [{"video_url": "https://cdn.example.com/a.mp4"}]}
        out = self.proxy.resolve_reference_uploads(body, "cn", "minimax-video-h3")
        self.assertEqual(out["reference_videos"],
                         [{"video_url": "https://cdn.example.com/a.mp4"}])
        self.assertEqual(self.calls, [])

    def test_audio_also_uploaded(self):
        self._patch()
        body = {"reference_audios": [{"audio_url": self._data_url(mime="audio/mpeg")}]}
        self.proxy.resolve_reference_uploads(body, "cn", "minimax-video-h3")
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0][1].endswith(".mp3"))

    def test_oversize_material_is_rejected_before_upload(self):
        self._patch()
        big = self._data_url(b"x" * (M.MAX_INPUT_IMAGE_BYTES + 10))
        with self.assertRaises(M.MediaError) as ctx:
            self.proxy.resolve_reference_uploads(
                {"reference_videos": [{"video_url": big}]}, "cn", "minimax-video-h3")
        self.assertIn("上限", str(ctx.exception))
        self.assertEqual(self.calls, [])

    def test_no_reference_material_costs_no_account_pick(self):
        def boom(*a, **k):
            raise AssertionError("没有素材时不该去挑账号")
        self.proxy._pick_media_account_for = boom
        self.proxy.resolve_reference_uploads({"prompt": "x"}, "cn", "minimax-video-h3")


class ReferencePriceTierTests(unittest.TestCase):
    """带参考素材的任务按 2 倍计价，两档必须分开记账。

    2026-11-02 实测：3 张参考图 + 1 段参考视频、5 秒 768P 实扣 142.86，
    即 28.572/秒，正好是普通 768P（14.2857/秒）的 2.00 倍。共用一个键会把
    普通档写成 2 倍价，之后所有普通任务报价翻倍。
    """

    def setUp(self):
        import wb_proxy
        self.proxy = wb_proxy
        import tempfile, os
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_path = wb_proxy._MEDIA_PRICES_PATH
        wb_proxy._MEDIA_PRICES_PATH = os.path.join(self.tmp.name, "video-prices.json")
        with wb_proxy._MEDIA_PRICES_LOCK:
            wb_proxy._MEDIA_PRICES.clear()

    def tearDown(self):
        import wb_proxy
        with wb_proxy._MEDIA_PRICES_LOCK:
            wb_proxy._MEDIA_PRICES.clear()
        wb_proxy._MEDIA_PRICES_PATH = self._orig_path
        self.tmp.cleanup()

    def test_two_tiers_do_not_overwrite_each_other(self):
        self.proxy.learn_media_price("minimax-video-h3", "768P", 57.14, 4)
        self.proxy.learn_media_price("minimax-video-h3", "768P", 142.86, 5,
                                     with_reference=True)
        self.assertAlmostEqual(
            self.proxy.media_price_per_second("minimax-video-h3", "768P"), 14.285, places=3)
        self.assertAlmostEqual(
            self.proxy.media_price_per_second("minimax-video-h3", "768P",
                                              with_reference=True), 28.572, places=3)

    def test_fallback_never_crosses_tiers(self):
        """普通档没观测值时，不许拿带参考素材的 2 倍价顶上。"""
        self.proxy.learn_media_price("minimax-video-h3", "2K", 45.7, 2,
                                     with_reference=True)
        self.assertIsNone(
            self.proxy.media_price_per_second("minimax-video-h3", "768P"))

    def test_ref_tier_is_persisted_under_its_own_key(self):
        """落盘格式要带 `|ref` 后缀，否则重启后两档又会被读成同一档。

        直接看磁盘文件：`load_media_prices()` 会把路径改回默认目录，用它验证会
        读到真实账本，测出来的东西和这个用例无关。
        """
        import json, os
        self.proxy.learn_media_price("minimax-video-h3", "768P", 142.86, 5,
                                     with_reference=True)
        with self.proxy._MEDIA_PRICES_LOCK:
            payload = json.load(open(self.proxy._MEDIA_PRICES_PATH, encoding="utf-8"))
        self.assertIn("minimax-video-h3|768P|ref", payload)
        self.assertAlmostEqual(payload["minimax-video-h3|768P|ref"], 28.572, places=3)
        self.assertNotIn("minimax-video-h3|768P", payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
