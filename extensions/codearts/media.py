"""Reject unsupported media before native translators can silently discard it."""
from urllib.parse import urlsplit
import base64
import binascii

IMAGE_MIMES = ("image/png", "image/jpeg", "image/webp", "image/gif")
MAX_IMAGE_BYTES = 4 * 1024 * 1024


def image_url(value, detail=None):
    if not isinstance(value, str) or not value:
        raise ValueError("image requires an HTTP(S) or base64 data URL")
    if detail not in (None, "auto", "low", "high"):
        raise ValueError("image detail must be auto, low or high")
    if value.startswith("data:"):
        header, sep, data = value.partition(",")
        if not sep or header not in tuple("data:" + m + ";base64" for m in IMAGE_MIMES):
            raise ValueError("image must be JPEG, PNG, WebP or GIF base64")
        if len(data) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
            raise ValueError("image exceeds 4 MiB")
        try:
            raw = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("invalid image base64") from None
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("image must be nonempty and at most 4 MiB")
    else:
        try:
            parsed = urlsplit(value)
            valid = parsed.scheme in ("http", "https") and parsed.hostname and parsed.username is None and parsed.password is None
            parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("image URL must use HTTP(S) without credentials")


def validate_payload(payload):
    if payload.get("audio") or payload.get("modalities", ["text"]) != ["text"]:
        raise ValueError("CodeArts supports text output only; audio/video are not supported")

    def walk(value):
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            kind = value.get("type", "")
            if kind in ("input_audio", "audio", "audio_url", "input_video", "video", "video_url", "input_file", "file", "document"):
                raise ValueError("CodeArts supports text and image input only; audio/video/files are not supported")
            if kind in ("input_image", "image_url", "image"):
                source = value.get("source")
                if isinstance(source, dict):  # Anthropic
                    if source.get("type") == "base64":
                        url = "data:" + str(source.get("media_type", "")) + ";base64," + str(source.get("data", ""))
                    elif source.get("type") == "url":
                        url = source.get("url")
                    else:
                        raise ValueError("unsupported image source")
                    detail = value.get("detail")
                else:
                    url = value.get("image_url") or value.get("url")
                    detail = value.get("detail")
                    if isinstance(url, dict):
                        detail = url.get("detail", detail)
                        url = url.get("url")
                    if not url and value.get("data"):
                        url = "data:" + str(value.get("mimeType") or value.get("mime_type") or "image/png") + ";base64," + str(value["data"])
                image_url(url, detail)
            if kind in ("function_call_output", "custom_tool_call_output"):
                walk(value.get("output"))
            # Inspect only content containers, never tool schemas/arguments.
            for key in ("content", "input"):
                if key in value:
                    walk(value[key])

    walk(payload.get("messages", []))
    walk(payload.get("input", []))


def has_image(payload):
    """payload（chat/Responses/Messages 任一形态）是否含图片 parts。

    与 validate_payload 同一遍历范围（messages / input / function_call_output
    的嵌套输出），只检测不校验：URL/base64 是否合法由 dispatch 入口的
    validate_payload 负责，这里只回答「这次请求带了图没有」。
    """
    found = []

    def walk(value):
        if found:
            return
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            kind = value.get("type", "")
            if kind in ("input_image", "image_url", "image") or "image_url" in value:
                found.append(True)
                return
            for key in ("content", "input", "output"):
                if key in value:
                    walk(value[key])

    walk(payload.get("messages", []))
    walk(payload.get("input", []))
    return bool(found)


def install_responses_detail(hub, runtime):
    original = hub._flatten_content

    def flatten(content):
        result = original(content)
        if not getattr(runtime.local, "codearts_media", False) or not isinstance(result, list):
            return result
        images = [p for p in content if isinstance(p, dict) and (p.get("type") in ("input_image", "image_url", "image") or "image_url" in p)]
        outputs = [p for p in result if p.get("type") == "image_url"]
        for source, target in zip(images, outputs):
            src = source.get("image_url")
            detail = src.get("detail", source.get("detail")) if isinstance(src, dict) else source.get("detail")
            if detail is not None:
                target["image_url"]["detail"] = detail
        return result

    hub._flatten_content = flatten
