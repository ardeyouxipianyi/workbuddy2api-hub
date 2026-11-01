"""媒体出站层（wb_media_client）的测试：端点、错误分类、轮询状态机、输入图与 SSRF。

这一层碰 I/O，所以 `wb_accounts.urlopen` 与 DNS 全部被替身接住——**一个字节都不出网**。
钉住的是四件事：

1. **端点与阶段**：生图走 /v2/images/generations、改图走 /v2/images/edits、
   视频走 /v2/videos/generations 与 /v2/videos/tasks。阶段名（image / submit / poll）
   一路传到 `describe_media_failure()`，因为同一个 400 在两者含义完全不同。
2. **轮询状态机**：completed 但产物为空 → 继续等；failed → 立刻抛；未知状态 → 继续等。
3. **输入图归一**：data URL 只校验体积，公网 URL 要下载再编成 data URL。
4. **SSRF 判据**：环回 / 私网 / 链路本地一律拒，且**不跟随 3xx**。

Run with: python tests/_test_media_client.py
"""
import io
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_media as M
import wb_media_client as C

_TMP = tempfile.TemporaryDirectory(prefix="wb-media-client-")


class FakeAccount(object):
    realm = "cn"
    product = "workbuddy"
    uid = "u-media-1"
    access_token = "media-client-token"
    proxy = ""
    enterprise_id = ""
    last_headers_purpose = None

    def chat_base_url(self):
        return "https://copilot.tencent.com"

    def headers(self, purpose="chat", **kwargs):
        FakeAccount.last_headers_purpose = purpose
        return {"X-Test-Purpose": purpose, "Authorization": "Bearer " + self.access_token}


class FakeResponse(object):
    def __init__(self, body=b"", headers=None, status=200):
        self._body = body
        self.headers = headers or {}
        self.status = status

    def read(self, amount=None):
        if amount is None:
            return self._body
        return self._body[:amount]

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def http_error(code, body):
    return urllib.error.HTTPError(
        "https://copilot.tencent.com/x", code, "err", {}, io.BytesIO(body.encode("utf-8")))


def json_body(payload):
    import json
    return json.dumps(payload).encode("utf-8")


class FakeUrlopen(object):
    """记录每次调用，并按顺序返回预置的响应（或抛预置的异常）。"""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, request, timeout=30, proxy=""):
        self.calls.append({
            "url": request.full_url,
            "method": request.get_method(),
            "headers": dict(request.headers),
            "data": request.data,
            "timeout": timeout,
            "proxy": proxy,
        })
        if not self.outcomes:
            raise AssertionError("urlopen 被调用的次数超出预置")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class PostTests(unittest.TestCase):
    def test_uses_media_headers_and_expected_url(self):
        fake = FakeUrlopen(FakeResponse(json_body({"code": 0, "data": {
            "data": [{"url": "https://cos/x.png"}], "usage": {"credit": 5.71}}})))
        with mock.patch.object(C.wb_accounts, "urlopen", fake):
            out = C.generate_image(FakeAccount(), {"model": "m", "prompt": "p", "size": "1024x1024"})
        self.assertEqual(out["url"], "https://cos/x.png")
        self.assertEqual(fake.calls[0]["url"],
                         "https://copilot.tencent.com/v2/images/generations")
        self.assertEqual(fake.calls[0]["method"], "POST")
        self.assertEqual(FakeAccount.last_headers_purpose, "media")

    def test_edit_uses_edits_endpoint(self):
        fake = FakeUrlopen(FakeResponse(json_body({"code": 0, "data": {
            "data": [{"url": "https://cos/y.png"}]}})))
        with mock.patch.object(C.wb_accounts, "urlopen", fake):
            C.edit_image(FakeAccount(), {"model": "m", "prompt": "p", "size": "1024x1024",
                                         "image": ["data:image/png;base64,AAA"]})
        self.assertEqual(fake.calls[0]["url"],
                         "https://copilot.tencent.com/v2/images/edits")

    def test_image_phase_400_is_upstream(self):
        fake = FakeUrlopen(http_error(400, '{"code":1,"msg":"bad size"}'))
        with mock.patch.object(C.wb_accounts, "urlopen", fake):
            try:
                C.generate_image(FakeAccount(), {"model": "m", "prompt": "p"})
            except M.MediaError as exc:
                self.assertEqual(exc.code, "UPSTREAM")
                self.assertIn("bad size", exc.message)
            else:
                raise AssertionError("应当抛 MediaError")

    def test_submit_phase_400_is_rejected(self):
        """提交阶段被退回：任务没建、钱没花，可立刻重试 → REJECTED。"""
        fake = FakeUrlopen(http_error(
            400, '{"code":14407,"msg":"unsupported video duration: 1 seconds, '
                 'supported range: 4-15 seconds"}'))
        with mock.patch.object(C.wb_accounts, "urlopen", fake):
            try:
                C.submit_video(FakeAccount(), {"model": "m", "prompt": "p", "seconds": 1})
            except M.MediaError as exc:
                self.assertEqual(exc.code, "REJECTED")
                self.assertIn("supported range", exc.message)
            else:
                raise AssertionError("应当抛 MediaError")

    def test_auth_error_keeps_status(self):
        fake = FakeUrlopen(http_error(403, "restricted"))
        with mock.patch.object(C.wb_accounts, "urlopen", fake):
            try:
                C.generate_image(FakeAccount(), {"model": "m", "prompt": "p"})
            except M.MediaError as exc:
                self.assertEqual(exc.code, "AUTH")
                self.assertEqual(exc.status, 403)
                self.assertEqual(exc.detail, "restricted")
            else:
                raise AssertionError("应当抛 MediaError")

    def test_network_failure_is_upstream(self):
        fake = FakeUrlopen(OSError("connection reset"))
        with mock.patch.object(C.wb_accounts, "urlopen", fake):
            try:
                C.generate_image(FakeAccount(), {"model": "m", "prompt": "p"})
            except M.MediaError as exc:
                self.assertEqual(exc.code, "UPSTREAM")
                self.assertIn("connection reset", exc.message)
            else:
                raise AssertionError("应当抛 MediaError")

    def test_non_json_body_is_upstream(self):
        fake = FakeUrlopen(FakeResponse(b"<html>gateway</html>"))
        with mock.patch.object(C.wb_accounts, "urlopen", fake):
            self.assertEqual(
                self._code(lambda: C.generate_image(FakeAccount(), {"model": "m"})), "UPSTREAM")

    def test_proxy_from_account_is_passed_through(self):
        account = FakeAccount()
        account.proxy = "http://127.0.0.1:7897"
        fake = FakeUrlopen(FakeResponse(json_body({"code": 0, "data": {
            "data": [{"url": "u"}]}})))
        with mock.patch.object(C.wb_accounts, "urlopen", fake):
            C.generate_image(account, {"model": "m", "prompt": "p"})
        self.assertEqual(fake.calls[0]["proxy"], "http://127.0.0.1:7897")

    def test_envelope_error_on_http_200(self):
        fake = FakeUrlopen(FakeResponse(json_body({"code": 11158, "msg": "pro only", "data": {}})))
        with mock.patch.object(C.wb_accounts, "urlopen", fake):
            self.assertEqual(
                self._code(lambda: C.generate_image(FakeAccount(), {"model": "m"})), "UPSTREAM")

    def _code(self, callable_obj):
        try:
            callable_obj()
        except M.MediaError as exc:
            return exc.code
        return None


class PollTests(unittest.TestCase):
    def task(self, status, videos=(), credit=None, error=None):
        data = {"status": status}
        if videos:
            data["data"] = [{"url": url} for url in videos]
        if credit is not None:
            data["usage"] = {"credit": credit}
        if error is not None:
            data["error"] = error
        return FakeResponse(json_body({"code": 0, "data": data}))

    def test_returns_videos_when_completed(self):
        fake = FakeUrlopen(self.task("running"), self.task("completed", ["https://cos/a.mp4"], 71.43))
        with mock.patch.object(C.wb_accounts, "urlopen", fake):
            out = C.poll_video_task(FakeAccount(), "t-1", sleep=lambda _: None)
        self.assertEqual(out["videos"], ["https://cos/a.mp4"])
        self.assertEqual(out["credit"], 71.43)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.calls[0]["url"],
                         "https://copilot.tencent.com/v2/videos/tasks")

    def test_completed_without_videos_keeps_waiting(self):
        """完成态先给 status、后补产物：不该当成终态，也不该报 EMPTY_RESULT。"""
        fake = FakeUrlopen(self.task("completed"), self.task("completed", ["https://cos/a.mp4"]))
        with mock.patch.object(C.wb_accounts, "urlopen", fake):
            out = C.poll_video_task(FakeAccount(), "t-1", sleep=lambda _: None)
        self.assertEqual(out["videos"], ["https://cos/a.mp4"])
        self.assertEqual(len(fake.calls), 2)

    def test_failed_task_raises_with_reason(self):
        fake = FakeUrlopen(self.task("failed", error="content rejected"))
        with mock.patch.object(C.wb_accounts, "urlopen", fake):
            try:
                C.poll_video_task(FakeAccount(), "t-1", sleep=lambda _: None)
            except M.MediaError as exc:
                self.assertEqual(exc.code, "UPSTREAM")
                self.assertIn("content rejected", exc.message)
            else:
                raise AssertionError("应当抛 MediaError")

    def test_unknown_status_keeps_waiting(self):
        fake = FakeUrlopen(self.task("brand-new-state"), self.task("completed", ["u"]))
        with mock.patch.object(C.wb_accounts, "urlopen", fake):
            out = C.poll_video_task(FakeAccount(), "t-1", sleep=lambda _: None)
        self.assertEqual(out["videos"], ["u"])

    def test_timeout_raises(self):
        fake = FakeUrlopen(self.task("running"))
        with mock.patch.object(C.wb_accounts, "urlopen", fake):
            try:
                C.poll_video_task(FakeAccount(), "t-1", timeout=-1, sleep=lambda _: None)
            except M.MediaError as exc:
                self.assertEqual(exc.code, "UPSTREAM")
                self.assertIn("轮询超过", exc.message)
            else:
                raise AssertionError("应当超时")

    def test_sleep_interval_is_used(self):
        waits = []
        fake = FakeUrlopen(self.task("running"), self.task("completed", ["u"]))
        with mock.patch.object(C.wb_accounts, "urlopen", fake):
            C.poll_video_task(FakeAccount(), "t-1", interval=7, sleep=waits.append)
        self.assertEqual(waits, [7])


class FetchBytesTests(unittest.TestCase):
    """`fetch_bytes`：客户端要 b64_json 时把 COS URL 转成 base64 用。"""

    def test_returns_bytes(self):
        fake = FakeUrlopen(FakeResponse(bytes([0x89]) + b"PNGdata"))
        with mock.patch.object(C.wb_accounts, "urlopen", fake):
            self.assertEqual(C.fetch_bytes(FakeAccount(), "https://cos/x.png"), bytes([0x89]) + b"PNGdata")
        self.assertEqual(fake.calls[0]["url"], "https://cos/x.png")

    def test_empty_body_is_an_error(self):
        fake = FakeUrlopen(FakeResponse(b""))
        with mock.patch.object(C.wb_accounts, "urlopen", fake):
            try:
                C.fetch_bytes(FakeAccount(), "https://cos/x.png")
            except M.MediaError as exc:
                self.assertEqual(exc.code, "EMPTY_RESULT")
            else:
                raise AssertionError("应当抛 MediaError")

    def test_http_error_is_upstream(self):
        fake = FakeUrlopen(http_error(404, "gone"))
        with mock.patch.object(C.wb_accounts, "urlopen", fake):
            try:
                C.fetch_bytes(FakeAccount(), "https://cos/x.png")
            except M.MediaError as exc:
                self.assertEqual(exc.code, "UPSTREAM")
                self.assertEqual(exc.status, 404)
            else:
                raise AssertionError("应当抛 MediaError")


class DownloadTests(unittest.TestCase):
    def test_writes_mp4(self):
        fake = FakeUrlopen(FakeResponse(b"\x00\x00\x00\x18ftypmp42"))
        dest = os.path.join(_TMP.name, "out")
        with mock.patch.object(C.wb_accounts, "urlopen", fake):
            path = C.download_video(FakeAccount(), "https://cos/a.mp4", dest, index=2)
        self.assertTrue(path.endswith("-2.mp4"))
        self.assertTrue(os.path.isfile(path))
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"\x00\x00\x00\x18ftypmp42")

    def test_empty_body_is_an_error(self):
        fake = FakeUrlopen(FakeResponse(b""))
        with mock.patch.object(C.wb_accounts, "urlopen", fake):
            try:
                C.download_video(FakeAccount(), "https://cos/a.mp4", _TMP.name)
            except M.MediaError as exc:
                self.assertEqual(exc.code, "EMPTY_RESULT")
            else:
                raise AssertionError("应当抛 MediaError")


class PublicUrlTests(unittest.TestCase):
    def test_rejects_loopback_private_and_link_local(self):
        for url in ("http://127.0.0.1/x", "http://10.0.0.5/x", "http://192.168.1.10:8080/admin",
                    "http://169.254.169.254/latest/meta-data", "http://[::1]/x",
                    "http://0.0.0.0/x"):
            self.assertEqual(
                self._code(lambda: C.assert_public_url(url)), "INVALID_ARGS", url)

    def test_accepts_numeric_public_address(self):
        C.assert_public_url("https://8.8.8.8/x.png")     # 数字 IP，本地解析、不出网

    def test_rejects_non_http_scheme(self):
        for url in ("ftp://example.com/x", "file:///etc/passwd", "gopher://x"):
            self.assertEqual(self._code(lambda: C.assert_public_url(url)), "INVALID_ARGS")

    def test_rejects_missing_host(self):
        self.assertEqual(self._code(lambda: C.assert_public_url("http:///x")), "INVALID_ARGS")

    def test_is_public_ip_matches_proxy_helper(self):
        self.assertFalse(C.is_public_ip("127.0.0.1"))
        self.assertFalse(C.is_public_ip("fe80::1"))
        self.assertTrue(C.is_public_ip("8.8.8.8"))
        self.assertFalse(C.is_public_ip("not-an-ip"))

    def _code(self, callable_obj):
        try:
            callable_obj()
        except M.MediaError as exc:
            return exc.code
        return None


class InputImageTests(unittest.TestCase):
    DATA_URL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="

    def test_data_url_passes_through(self):
        self.assertEqual(C.resolve_input_image(self.DATA_URL), self.DATA_URL)

    def test_oversized_data_url_is_rejected(self):
        big = "data:image/png;base64," + "A" * ((M.MAX_INPUT_IMAGE_BYTES // 3) * 4 + 8)
        self.assertEqual(self._code(lambda: C.resolve_input_image(big)), "INVALID_ARGS")

    def test_local_path_is_rejected_before_any_io(self):
        self.assertEqual(
            self._code(lambda: C.resolve_input_image("/etc/passwd")), "INVALID_ARGS")

    def test_public_url_is_downloaded_and_encoded(self):
        png = b"\x89PNG\r\n\x1a\n" + b"x" * 32
        fake_opener = mock.MagicMock()
        response = FakeResponse(png, headers={"Content-Type": "image/png"})
        response.headers = {"Content-Type": "image/png"}
        fake_opener.open.return_value = response
        with mock.patch.object(C, "assert_public_url"), \
                mock.patch.object(C, "_no_redirect_opener", return_value=fake_opener):
            out = C.resolve_input_image("https://8.8.8.8/a.png")
        self.assertTrue(out.startswith("data:image/png;base64,"))

    def test_mime_falls_back_to_sniffing(self):
        jpeg = b"\xff\xd8\xff" + b"y" * 32
        fake_opener = mock.MagicMock()
        response = FakeResponse(jpeg, headers={"Content-Type": "application/octet-stream"})
        fake_opener.open.return_value = response
        with mock.patch.object(C, "assert_public_url"), \
                mock.patch.object(C, "_no_redirect_opener", return_value=fake_opener):
            out = C.resolve_input_image("https://8.8.8.8/a.bin")
        self.assertTrue(out.startswith("data:image/jpeg;base64,"))

    def test_sniff_image_mime(self):
        self.assertEqual(C.sniff_image_mime(b"\x89PNG\r\n\x1a\n"), "image/png")
        self.assertEqual(C.sniff_image_mime(b"\xff\xd8\xff\xe0"), "image/jpeg")
        self.assertEqual(C.sniff_image_mime(b"GIF89a"), "image/gif")
        self.assertEqual(C.sniff_image_mime(b"RIFF\0\0\0\0WEBPVP8 "), "image/webp")
        self.assertEqual(C.sniff_image_mime(b"BM\0\0"), "image/bmp")
        self.assertEqual(C.sniff_image_mime(b"????"), "image/png")

    def _code(self, callable_obj):
        try:
            callable_obj()
        except M.MediaError as exc:
            return exc.code
        return None


class MediaModelFetchTests(unittest.TestCase):
    def test_reads_and_projects_catalog(self):
        payload = {"code": 0, "data": {"models": [
            {"id": "glm-5.3", "tags": ["craft"]},
            {"id": "hunyuan-image-alpha", "tags": ["text-to-image"]},
        ]}}
        fake = FakeUrlopen(FakeResponse(json_body(payload)))
        with mock.patch.object(C.wb_accounts, "urlopen", fake):
            models = C.fetch_media_models(FakeAccount())
        self.assertEqual([model["id"] for model in models], ["hunyuan-image-alpha"])
        self.assertEqual(fake.calls[0]["url"], "https://copilot.tencent.com/v3/config")
        self.assertEqual(fake.calls[0]["method"], "GET")


if __name__ == "__main__":
    unittest.main(verbosity=2)
