"""媒体路由的**真实 HTTP** 测试（L3）：分发、状态码、体上限、鉴权顺序。

前面几套用 `Handler` 替身跑流程，绕过了 `do_POST` / `do_GET` 的真实分发——
白名单漏一条就是 404、体上限写错就是 413，而替身测试照样全绿。这一套补上：

    起两个真实实例（8799 一类的空闲端口，隔离 accounts / usage / catalog）
      A：媒体默认关
      B：WB_MEDIA_ENABLED=1
    然后打真实 HTTP，逐条断言状态码与错误信封。

**一个字节不出网、一次积分不花**：下面所有用例要么在分发层被拦（404/401/413），
要么在参数校验层被拦（400），要么因为隔离环境里没有账号而停在"选号"（5xx）。
真实上游的验收在 `docs/MEDIA-TEST-PLAN.md` §6（L4），不进这里。

Run with: python tests/_test_media_http.py
"""
import http.client
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _lifecycle import free_port, spawn_managed, stop_managed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_KEY = "sk-media-http-test"

#: 三个媒体模型的真实形状（2026-11-01 从本机 catalog 抄，只留必要字段）。
CATALOG = {"code": 0, "data": {"models": [
    {"id": "glm-5.3", "name": "GLM-5.3", "tags": ["craft"]},
    {"id": "hunyuan-image-alpha", "name": "Hunyuan Image Alpha",
     "tags": ["text-to-image"]},
    {"id": "hunyuan-image-alpha-edit", "name": "Hunyuan Image Alpha Edit",
     "tags": ["image-to-image"]},
    #: 参考素材只有 minimax 支持（kling 的三个上限都是 0）。夹具原先漏了它，
    #: 于是"多图+视频混用"这类测试只能拿 kling 顶替，测出来的结论是反的。
    {"id": "minimax-video-h3", "name": "MiniMax-H3",
     "tags": ["text-to-video", "image-to-video"],
     "videoGeneration": {"minSeconds": 4, "maxSeconds": 15, "defaultSeconds": 5,
                         "resolutions": ["768P", "2K"], "defaultResolution": "768P",
                         "aspectRatios": ["16:9", "9:16", "1:1"],
                         "maxPromptLength": 2000, "audioMode": "always-on",
                         "allowMixedReferenceMaterials": True,
                         "maxReferenceImages": 9, "maxReferenceVideos": 3,
                         "maxReferenceAudios": 3, "maxReferenceMaterials": 12,
                         "framesExclusiveWithReferenceMaterials": True}},
    {"id": "kling-video-v3", "name": "Kling-Video-V3",
     "tags": ["text-to-video", "image-to-video"],
     "videoGeneration": {"minSeconds": 3, "maxSeconds": 15, "defaultSeconds": 5,
                         "resolutions": ["720P", "1080P", "4K"],
                         "defaultResolution": "720P",
                         "aspectRatios": ["16:9", "9:16", "1:1"],
                         "defaultAspectRatio": "16:9",
                         "maxPromptLength": 2500, "audioMode": "switchable"}},
]}}


class _Instance(object):
    """一个隔离的网关实例：自己的 accounts / usage / catalog / media 目录。"""

    def __init__(self, root, media_enabled):
        self.port = free_port()
        self.base = "http://127.0.0.1:%d" % self.port
        self.root = root
        self.env = dict(os.environ)
        self.env.update({
            "ACCOUNTS_DIR": os.path.join(root, "accounts"),
            "WB_PROXY_USAGE_DIR": os.path.join(root, "usage"),
            "WB_PROXY_CATALOG_DIR": os.path.join(root, "catalog"),
            "WB_MEDIA_DIR": os.path.join(root, "media"),
            "WB_PROXY_LOGS_DIR": os.path.join(root, "logs"),
            "PYTHONIOENCODING": "utf-8",
        })
        self.env.pop("WB_MEDIA_ENABLED", None)
        if media_enabled:
            self.env["WB_MEDIA_ENABLED"] = "1"
        for name in ("accounts", "usage", "catalog", "media", "logs"):
            os.makedirs(os.path.join(root, name), exist_ok=True)
        with open(os.path.join(root, "catalog", "product-config-cn.json"),
                  "w", encoding="utf-8") as handle:
            json.dump(CATALOG, handle, ensure_ascii=False)
        self.proc = None

    def start(self):
        self.proc = spawn_managed(
            [sys.executable, "wb_proxy.py", "--port", str(self.port),
             "--api-key", API_KEY],
            cwd=ROOT, env=self.env,
            stdout=subprocess_devnull(), stderr=subprocess_devnull())
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(self.base + "/health", timeout=1) as response:
                    if response.status == 200:
                        return
            except Exception:
                time.sleep(0.2)
        raise AssertionError("实例没能在 40 秒内起来（端口 %d）" % self.port)

    def stop(self):
        stop_managed(self.proc)
        self.proc = None

    # ---- HTTP 帮手
    def request(self, method, path, body=None, key=None, raw=None, timeout=20):
        url = self.base + path
        data = raw
        if data is None and body is not None:
            data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if key:
            request.add_header("Authorization", "Bearer " + key)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
                return response.status, payload, dict(response.headers)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            return exc.code, payload, dict(exc.headers or {})

    def json(self, method, path, body=None, key=API_KEY, raw=None):
        status, payload, headers = self.request(method, path, body, key, raw)
        try:
            parsed = json.loads(payload.decode("utf-8", "replace"))
        except ValueError:
            parsed = None
        return status, parsed, headers


def subprocess_devnull():
    import subprocess
    return subprocess.DEVNULL


_TMP = tempfile.TemporaryDirectory(prefix="wb-media-http-")
_OFF = None
_ON = None


def setUpModule():
    global _OFF, _ON
    _OFF = _Instance(os.path.join(_TMP.name, "off"), media_enabled=False)
    _OFF.start()
    _ON = _Instance(os.path.join(_TMP.name, "on"), media_enabled=True)
    _ON.start()


def tearDownModule():
    for instance in (_ON, _OFF):
        if instance is not None:
            try:
                instance.stop()
            except Exception:
                pass
    _TMP.cleanup()


class GateTests(unittest.TestCase):
    """开关与鉴权：决定"这个路由存不存在"。"""

    def test_disabled_route_is_404(self):
        status, body, _ = _OFF.json("POST", "/v1/images/generations",
                                    {"prompt": "一只猫"})
        self.assertEqual(status, 404)

    def test_enabled_route_reaches_the_parameter_gate(self):
        """同为 404 的两种情形要分开：开关开着时不该再是"没这条路由"。"""
        status, body, _ = _ON.json("POST", "/v1/images/generations", {"prompt": "一只猫"})
        self.assertNotEqual(status, 404)

    def test_missing_key_is_401(self):
        status, body, _ = _ON.json("POST", "/v1/images/generations",
                                   {"prompt": "一只猫"}, key=None)
        self.assertEqual(status, 401)
        self.assertIn("error", body)

    def test_media_models_needs_a_key(self):
        status, _, _ = _ON.json("GET", "/v1/media/models", key=None)
        self.assertEqual(status, 401)

    def test_video_route_disabled_is_404(self):
        status, _, _ = _OFF.json("POST", "/v1/videos", {"prompt": "海边"})
        self.assertEqual(status, 404)

    def test_get_on_a_post_route_is_404(self):
        """方法不匹配不该走进媒体层（那会白跑一次参数校验）。"""
        status, _, _ = _ON.json("GET", "/v1/images/generations", key=API_KEY)
        self.assertEqual(status, 404)


class ParameterGateTests(unittest.TestCase):
    """参数闸门：全部在"发请求"之前被拦，所以一分钱不花。"""

    def test_missing_prompt(self):
        status, body, _ = _ON.json("POST", "/v1/images/generations", {})
        self.assertEqual(status, 400)
        self.assertIn("prompt", json.dumps(body, ensure_ascii=False))

    def test_oversized(self):
        status, body, _ = _ON.json("POST", "/v1/images/generations",
                                   {"prompt": "x", "size": "2560x2560"})
        self.assertEqual(status, 400)
        self.assertIn("4.19", json.dumps(body, ensure_ascii=False))

    def test_batch_is_rejected(self):
        status, body, _ = _ON.json("POST", "/v1/images/generations",
                                   {"prompt": "x", "n": 4})
        self.assertEqual(status, 400)

    def test_unknown_model_lists_the_available_ones(self):
        status, body, _ = _ON.json("POST", "/v1/images/generations",
                                   {"prompt": "x", "model": "nope"})
        self.assertEqual(status, 400)
        self.assertIn("hunyuan-image-alpha", json.dumps(body, ensure_ascii=False))

    def test_edit_rejects_local_path(self):
        """S1 的端到端版本：本地路径会被当成"要读服务器文件"。"""
        status, body, _ = _ON.json("POST", "/v1/images/edits",
                                   {"prompt": "改成油画", "image": "/etc/passwd"})
        self.assertEqual(status, 400)
        self.assertIn("服务器", json.dumps(body, ensure_ascii=False))

    def test_edit_rejects_loopback_url(self):
        """S3 的端到端版本：SSRF 在公网地址校验那一层被拦。"""
        status, body, _ = _ON.json("POST", "/v1/images/edits",
                                   {"prompt": "改成油画",
                                    "image": "http://127.0.0.1:%d/accounts" % _ON.port})
        self.assertEqual(status, 400)

    def test_edit_rejects_file_url(self):
        status, body, _ = _ON.json("POST", "/v1/images/edits",
                                   {"prompt": "x", "image": "file:///etc/passwd"})
        self.assertEqual(status, 400)

    def test_video_missing_prompt(self):
        status, body, _ = _ON.json("POST", "/v1/videos", {"seconds": 5})
        self.assertEqual(status, 400)

    def test_video_seconds_below_catalog_minimum(self):
        """目录说 kling 最低 3 秒，本地就该拦下，不必让上游拒一次。"""
        # 指定 kling：不指定时按目录顺序会先选中 minimax（最低 4 秒），
        # 这条测的是"按模型自己的下限拦"，所以模型必须写死。
        status, body, _ = _ON.json("POST", "/v1/videos",
                                   {"prompt": "海边", "seconds": 1,
                                    "model": "kling-video-v3"})
        self.assertEqual(status, 400)
        self.assertIn("3", json.dumps(body, ensure_ascii=False))

    def test_video_rejects_local_image(self):
        status, body, _ = _ON.json("POST", "/v1/videos",
                                   {"prompt": "海边", "image": "/etc/passwd"})
        self.assertEqual(status, 400)


    def test_reference_video_rejection_names_the_video(self):
        """参考视频被拒时，报错必须说"参考视频"，不能说"输入图"。

        本地 mp4 会先被客户端转成 data URL，网关在 assert_public_url 拦下。
        文案如果写"输入图 URL 只支持 http/https"，调用方第一反应是去查首帧，
        而真正要换掉的是那段视频——排查方向直接跑偏。实测踩过。
        """
        status, body, _ = _ON.json(
            "POST", "/v1/videos",
            {"prompt": "海边", "seconds": 5,
             "model": "minimax-video-h3",
             "reference_videos": ["/etc/passwd"]})
        self.assertEqual(status, 400)
        message = json.dumps(body, ensure_ascii=False)
        self.assertIn("参考视频", message)
        self.assertNotIn("输入图", message)

    def test_reference_audio_rejection_names_the_audio(self):
        status, body, _ = _ON.json(
            "POST", "/v1/videos",
            {"prompt": "海边", "seconds": 5,
             "model": "minimax-video-h3",
             "reference_audios": ["/etc/passwd"]})
        self.assertEqual(status, 400)
        message = json.dumps(body, ensure_ascii=False)
        self.assertIn("参考音频", message)
        self.assertNotIn("输入图", message)

    def test_reference_images_and_video_may_be_mixed(self):
        """多图 + 一段视频是允许的组合（minimax allowMixedReferenceMaterials）。

        舞步迁移就是这个用法：三视图锁角色、舞蹈视频给动作。这里只断言
        **不是被"不允许混用"拦下**，避免把合法组合误拦。
        """
        png = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
               "AAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")
        status, body, _ = _ON.json(
            "POST", "/v1/videos",
            {"prompt": "海边", "seconds": 5,
             "model": "minimax-video-h3",
             "reference_images": [png, png, png],
             "reference_videos": ["/etc/passwd"]})
        message = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("不允许混用", message)
        # 走到视频 URL 校验那一层才被拒，说明混用本身是通过的。
        self.assertIn("参考视频", message)


class PayloadLimitTests(unittest.TestCase):
    """体上限：媒体走 max(通用上限, 24MB)，只放宽不收紧。"""

    def _declared_length(self, path, length):
        """只发请求头、声明 Content-Length，不发 body。

        `_read_payload()` 是先看 Content-Length 再看数据的，所以一个超限的声明会在
        读完 body **之前**得到 413。这样测既快又稳：真发 26 MB 会被服务端的
        close_connection 打断（Windows 上表现为 ConnectionAbortedError），反而
        拿不到状态码——而我们要证明的正是"服务端拒绝了这个大小"。
        """
        conn = http.client.HTTPConnection("127.0.0.1", _ON.port, timeout=20)
        try:
            conn.putrequest("POST", path)
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(length))
            conn.putheader("Authorization", "Bearer " + API_KEY)
            conn.endheaders()
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_26mb_is_rejected_before_the_body_is_read(self):
        """26 MB 超出媒体的 24 MB 上限 → 413，且不读 body。"""
        status, body = self._declared_length("/v1/images/generations", 26 * 1024 * 1024)
        self.assertEqual(status, 413)
        self.assertIn("payload too large", body.decode("utf-8", "replace"))

    def test_20mb_passes_the_media_limit(self):
        """20 MB 在 24 MB 之内：不该被体上限拦下（会因"没有可用账号"失败，但不是 413）。

        这一条必须**真发数据**：上限放行之后服务端会真的读完 20 MB，只发头会一直
        等到客户端超时——那测的就不是上限，而是"服务端在等 body"。
        """
        head = b'{"prompt": "x", "padding": "'
        tail = b'"}'
        target = 20 * 1024 * 1024
        body = head + b"a" * (target - len(head) - len(tail)) + tail
        status, _, _ = _ON.json("POST", "/v1/images/generations", raw=body)
        self.assertNotEqual(status, 413)


class ReadEndpointsTests(unittest.TestCase):
    def test_media_models_lists_media_only(self):
        status, body, _ = _ON.json("GET", "/v1/media/models")
        self.assertEqual(status, 200)
        ids = [model["id"] for model in body["models"]]
        self.assertIn("hunyuan-image-alpha", ids)
        self.assertIn("kling-video-v3", ids)
        self.assertNotIn("glm-5.3", ids)

    def test_media_models_carries_video_generation(self):
        status, body, _ = _ON.json("GET", "/v1/media/models")
        video = [model for model in body["models"] if model["id"] == "kling-video-v3"][0]
        generation = video["videoGeneration"]
        self.assertEqual(generation["minSeconds"], 3)
        self.assertEqual(generation["maxSeconds"], 15)
        self.assertEqual(generation["resolutions"], ["720P", "1080P", "4K"])
        self.assertEqual(generation["maxPromptLength"], 2500)

    def test_media_models_price_is_per_resolution(self):
        """同一模型换分辨率就是另一个价，必须分开报。

        实测：minimax 4 秒 768P 扣 57.14、2K 扣 91.43（贵 60%）。只报一个
        `creditPerSecond`（默认档的价）时，调用方按 768P 算 2K 的花费会少算 60%。
        没观测过的那档**不出现**——拿别档的价冒充比不报更坏。
        """
        with tempfile.TemporaryDirectory() as root:
            inst = _Instance(os.path.join(root, "per-res"), media_enabled=True)
            prices = os.path.join(root, "per-res", "media", "video-prices.json")
            with open(prices, "w", encoding="utf-8") as handle:
                json.dump({"kling-video-v3|720P": 25.714,
                           "kling-video-v3|4K": 64.285}, handle)
            inst.start()
            try:
                status, body, _ = inst.json("GET", "/v1/media/models")
                self.assertEqual(status, 200)
                video = [m for m in body["models"] if m["id"] == "kling-video-v3"][0]
                self.assertAlmostEqual(video["creditsPerSecond"]["720P"], 25.714, places=3)
                self.assertAlmostEqual(video["creditsPerSecond"]["4K"], 64.285, places=3)
                # 1080P 从没观测过：不许拿 720P 或 4K 的价来充数。
                self.assertNotIn("1080P", video["creditsPerSecond"])
                # 默认档那档仍走旧字段，老调用方不受影响。
                self.assertAlmostEqual(video["creditPerSecond"], 25.714, places=3)
            finally:
                inst.stop()

    def test_media_models_tag_filter(self):
        status, body, _ = _ON.json("GET", "/v1/media/models?tag=text-to-image")
        self.assertEqual([model["id"] for model in body["models"]], ["hunyuan-image-alpha"])

    def test_chat_model_list_never_contains_media_models(self):
        """媒体模型不许进 /v1/models：聊天客户端会拿它们去对话。"""
        status, body, _ = _ON.json("GET", "/v1/models")
        self.assertEqual(status, 200)
        ids = [model.get("id") for model in body.get("data", [])]
        for media_id in ("hunyuan-image-alpha", "hunyuan-image-alpha-edit", "kling-video-v3"):
            self.assertNotIn(media_id, ids)

    def test_health_reports_media_state(self):
        status, body, _ = _ON.json("GET", "/health", key=None)
        self.assertEqual(status, 200)
        self.assertIn("media", body)
        self.assertTrue(body["media"]["enabled"])
        self.assertIn("slots", body["media"])
        self.assertIn("inflight", body["media"])

    def test_health_off_instance_reports_disabled(self):
        status, body, _ = _OFF.json("GET", "/health", key=None)
        self.assertFalse(body["media"]["enabled"])

    def test_media_tasks_endpoint(self):
        status, body, _ = _ON.json("GET", "/v1/media/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(body["tasks"], [])
        self.assertIn("stats", body)

    def test_unknown_video_id_is_404(self):
        status, _, _ = _ON.json("GET", "/v1/videos/vid_doesnotexist")
        self.assertEqual(status, 404)

    def test_video_content_without_token_is_401(self):
        status, _, _ = _ON.json("GET", "/v1/videos/vid_doesnotexist/content", key=None)
        self.assertEqual(status, 401)

    def test_video_content_with_bad_token_is_401(self):
        status, _, _ = _ON.json("GET", "/v1/videos/vid_doesnotexist/content?t=1.deadbeef",
                                key=None)
        self.assertEqual(status, 401)


class NoAccountTests(unittest.TestCase):
    """隔离环境里没有账号：校验通过之后必须停在"选号"，而不是打到上游去。"""

    def test_image_request_stops_at_account_selection(self):
        status, body, _ = _ON.json("POST", "/v1/images/generations",
                                   {"prompt": "一只在窗台上晒太阳的猫"})
        # 5xx = 没有可用账号；关键是不能是 200（那意味着真的发了请求）。
        self.assertGreaterEqual(status, 500)
        text = json.dumps(body, ensure_ascii=False)
        self.assertTrue("账号" in text or "account" in text.lower(), text)

    def test_video_request_stops_at_account_selection(self):
        status, body, _ = _ON.json("POST", "/v1/videos", {"prompt": "海边日落", "seconds": 5})
        self.assertGreaterEqual(status, 500)
        self.assertNotEqual(status, 202)      # 没有账号却回了 202 就说明钱白花了


if __name__ == "__main__":
    unittest.main(verbosity=2)
