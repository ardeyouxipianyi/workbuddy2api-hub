#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb_media_client.py —— 媒体链路的出站层：真正把请求发出去、把产物拿回来。

分层与 `wb_media.py` 的分工：那一层只认协议（拼 body、解响应、判错误），
这一层只碰 I/O（发请求、下载、读流）。所以这里没有冷却、没有换号、没有配额——
那些是**派发策略**，属于帐号池与路由层，混进来就没法单独测了。

## 为什么视频单独抽三个函数

生图是一次同步请求就拿到 url；视频是异步任务：`submit_video()` 只拿到 task id，
还要 `poll_video_task()` 轮询到 completed，最后 `download_video()` 把 mp4 拉到本地。
三者拆开是为了让调度层能自己决定"提交后立刻返回 id"（我们的做法）还是
"阻塞到完成"（`?wait=1` 的可选行为）。

## 出站形态照抄官方 CLI

端点、请求体、轮询节奏（5 秒）与整体上限（600 秒）都来自
WorkBuddy 官方 CLI 的 `ImageService` / `VideoService`，取证记录见 `docs/VIDEO_GEN.md`
与 dsh-connect-workbuddy 的同名文档。两处没照抄：一是 `watermark`，官方 CLI 写
`true`，本网关固定 `false`（画面不带"AI生成"字样，且不对调用方开放这个开关）；
二是**输入图的形态**：那边接受
本地路径，这里只接受 data URL 与公网 http(s)——网关面对任意 API 客户端，
接受路径等于开放任意文件读取。

## 输入图的下载为什么自己写一段

`urllib` 默认跟随 3xx。对一个由客户端控制的 URL 来说，"跟随重定向"是 SSRF 的标准
绕过方式：公网 URL 302 到 `http://127.0.0.1:8788/accounts` 就绕过了前一道公网地址
校验。所以这里用一个禁掉重定向的 opener，并且**逐跳校验解析出来的每个 IP**。
"""
import base64
import ipaddress
import json
import os
import random
import re
import string
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

import wb_accounts
import wb_media

#: 生图 / 改图单次请求上限。实测单张 12.9–38.7 秒，留足余量。
IMAGE_TIMEOUT_SECONDS = 180
#: 提交生视频任务的上限：只建任务，很快。
VIDEO_SUBMIT_TIMEOUT_SECONDS = 60
#: 轮询单个请求的上限（不等于整体上限）。
VIDEO_POLL_REQUEST_TIMEOUT_SECONDS = 60
#: 轮询总时长上限，与官方 CLI 的 pollTask 一致。
VIDEO_POLL_TIMEOUT_SECONDS = 600
#: 轮询间隔，与官方 CLI 一致。
VIDEO_POLL_INTERVAL_SECONDS = 5
#: 下载产物（mp4）的上限。
VIDEO_DOWNLOAD_TIMEOUT_SECONDS = 120
#: 读产品配置的上限（只读、免费）。
CONFIG_TIMEOUT_SECONDS = 15
#: 下载客户端给的输入图的上限。
INPUT_IMAGE_TIMEOUT_SECONDS = 30
#: 单个输入图读取的字节上限（多留一点余量给 base64 前的原始流）。
_INPUT_READ_CEILING = wb_media.MAX_INPUT_IMAGE_BYTES + 1024

#: 出站身份形状（`desktop` / `cli` / None=跟随账号自身）。
#:
#: 由调用方在读到媒体配置时同步过来（`set_identity`）。做成模块级而不是逐函数传参，
#: 是因为后台 worker 轮询时也需要同一个形状，而 worker 手里没有配置上下文。
#: 同一个进程的媒体出口只有一种形状，所以一个全局量就够。
_IDENTITY = None


def set_identity(value):
    """设置媒体出站的账号身份形状。`None` / 未知值 = 跟随账号自身。"""
    global _IDENTITY
    _IDENTITY = value if value in ("desktop", "cli") else None


def current_identity():
    return _IDENTITY


#: 预签名与 COS PUT 的超时（参考素材通常是几 MB，给足余量）
UPLOAD_TIMEOUT_SECONDS = 120

CONFIG_PATH = "/v3/config"
IMAGE_GENERATIONS_PATH = "/v2/images/generations"
IMAGE_EDITS_PATH = "/v2/images/edits"
VIDEO_GENERATIONS_PATH = "/v2/videos/generations"
VIDEO_TASKS_PATH = "/v2/videos/tasks"


# ---------------------------------------------------------------- 出站底座

def _error_body(exc, limit=2000):
    """把 HTTPError 的响应体读出来（尽量读一次、缓存起来）。

    `wb_proxy.read_http_error()` 做的是同一件事，但这一层不能 import wb_proxy
    ——那会形成循环依赖（wb_proxy 会 import 本模块）。二十行重复换一条清晰的
    依赖方向，值得。
    """
    cached = getattr(exc, "wb_body", None)
    if cached is not None:
        return cached[:limit]
    try:
        raw = exc.read(limit)
    except Exception:
        return b""
    if os.environ.get("WB_CACHE_HTTP_ERROR_BODY", "").strip().lower() in ("1", "true", "yes", "on"):
        try:
            exc.wb_body = raw
        except Exception:
            pass
    return raw


def _post(account, path, body, timeout, phase):
    """发一次媒体 POST，返回解析后的 JSON；非 2xx 一律翻成 MediaError。

    `phase` 透传给 `describe_media_failure()`，因为它决定一个 400 是
    `REJECTED`（提交阶段、没扣钱、可立刻重试）还是 `UPSTREAM`（已经花过钱）。
    """
    url = account.chat_base_url() + path
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers=account.headers(purpose="media", media_identity=_IDENTITY))
    try:
        with wb_accounts.urlopen(request, timeout=timeout, proxy=account.proxy) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        text = _error_body(exc).decode("utf-8", "replace")
        raise wb_media.describe_media_failure(exc.code, text, phase)
    except Exception as exc:                                  # 网络层
        raise wb_media.MediaError(
            "UPSTREAM", "媒体请求失败：%s" % (exc,), detail=str(exc))
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise wb_media.MediaError(
            "UPSTREAM", "上游返回的不是 JSON：%s" % raw[:200].decode("utf-8", "replace"))


def _get_json(account, path, timeout):
    """读一次 JSON GET（目录）。只用于冷启动兜底，正常路径走内存缓存。"""
    url = account.chat_base_url() + path
    request = urllib.request.Request(
        url, method="GET", headers=account.headers(purpose="media", media_identity=_IDENTITY))
    try:
        with wb_accounts.urlopen(request, timeout=timeout, proxy=account.proxy) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        text = _error_body(exc).decode("utf-8", "replace")
        raise wb_media.describe_media_failure(exc.code, text, "config")
    except Exception as exc:
        raise wb_media.MediaError("UPSTREAM", "读取产品配置失败：%s" % (exc,))
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise wb_media.MediaError("UPSTREAM", "产品配置不是 JSON")


# ---------------------------------------------------------------- 目录与生图

def fetch_media_models(account, timeout=CONFIG_TIMEOUT_SECONDS):
    """读该账号所属区的产品配置，返回**全部媒体模型**（未按能力分组）。

    这是一次额外的只读 GET——本仓库的 `parse_remote_catalog()` 会把同一份文档
    投影成聊天目录、丢掉媒体条目，所以媒体要自己读一次原文。路由层正常情况下
    从内存缓存 `_catalog_raw` 取，只有冷启动才落到这里。
    """
    payload = _get_json(account, CONFIG_PATH, timeout)
    return wb_media.media_models(payload)


def generate_image(account, body, timeout=IMAGE_TIMEOUT_SECONDS):
    """文生图。返回 `{url | b64, credit?, created?}`。"""
    return wb_media.parse_image_response(
        _post(account, IMAGE_GENERATIONS_PATH, body, timeout, "image"))


def edit_image(account, body, timeout=IMAGE_TIMEOUT_SECONDS):
    """改图（图生图）。body 里的 `image` 必须是 data URL 数组。"""
    return wb_media.parse_image_response(
        _post(account, IMAGE_EDITS_PATH, body, timeout, "image"))


# ---------------------------------------------------------------- 视频

def submit_video(account, body, timeout=VIDEO_SUBMIT_TIMEOUT_SECONDS):
    """提交一个生视频任务，返回 `{"taskId": ...}`。"""
    return wb_media.parse_video_submission(
        _post(account, VIDEO_GENERATIONS_PATH, body, timeout, "submit"))


def poll_video_task(account, task_id, interval=VIDEO_POLL_INTERVAL_SECONDS,
                    timeout=VIDEO_POLL_TIMEOUT_SECONDS, sleep=None):
    """轮询到终态：完成返回 `{status, videos, credit?}`，失败抛 MediaError。

    - `status == "completed"` 但没有 `videos[]`：当作**还没好**继续等（上层在整体
      超时后才会报 EMPTY_RESULT）——上游偶尔会在完成态先给 status、后补产物
    - `status == "failed"`：抛 `UPSTREAM`（任务本身失败，钱可能已花）
    - 其它（含未知状态）：继续等。把新状态误判成失败会浪费一次已花钱的任务
    """
    waiter = sleep or time.sleep
    started = time.time()
    while True:
        if time.time() - started > timeout:
            raise wb_media.MediaError(
                "UPSTREAM", "轮询超过 %d 秒仍未完成（task %s）" % (timeout, task_id))
        outcome = wb_media.parse_video_task(
            _post(account, VIDEO_TASKS_PATH, {"task_id": task_id},
                  VIDEO_POLL_REQUEST_TIMEOUT_SECONDS, "poll"))
        status = outcome["status"]
        if status == "completed" and outcome["videos"]:
            return outcome
        if status == "failed":
            reason = outcome.get("error") or "上游没有给出原因"
            raise wb_media.MediaError("UPSTREAM", "生视频任务失败：%s" % reason)
        waiter(interval)


def fetch_bytes(account, url, timeout=IMAGE_TIMEOUT_SECONDS):
    """把一个公网地址的字节取回来（客户端要 `b64_json` 时把 COS URL 转成 base64）。

    与 `download_video` 的区别：不落盘。生图产物的签名 URL 只有 24 小时，转成
    base64 交给客户端由它自己保管，比在网关临时目录里留一份更省事。
    """
    request = urllib.request.Request(url, method="GET")
    try:
        with wb_accounts.urlopen(request, timeout=timeout, proxy=account.proxy) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        raise wb_media.MediaError(
            "UPSTREAM", "读取产物失败（HTTP %d）：%s" % (exc.code, url), status=exc.code)
    except Exception as exc:
        raise wb_media.MediaError("UPSTREAM", "读取产物失败：%s" % (exc,))
    if not data:
        raise wb_media.MediaError("EMPTY_RESULT", "产物是空的：%s" % url)
    return data


def download_video(account, url, dest_dir, index=0, timeout=VIDEO_DOWNLOAD_TIMEOUT_SECONDS):
    """把一条产物 URL 落成 `.mp4`，返回绝对路径。

    产物是 COS 签名 URL，不需要 Authorization；但走同一个 `urlopen()` 是为了
    继承该账号的代理配置（有些部署必须经代理才出网）。
    """
    request = urllib.request.Request(url, method="GET")
    try:
        with wb_accounts.urlopen(request, timeout=timeout, proxy=account.proxy) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        raise wb_media.MediaError(
            "UPSTREAM", "下载产物失败（HTTP %d）：%s" % (exc.code, url), status=exc.code)
    except Exception as exc:
        raise wb_media.MediaError("UPSTREAM", "下载产物失败：%s" % (exc,))
    if not data:
        raise wb_media.MediaError("EMPTY_RESULT", "下载到的产物是空的：%s" % url)
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, "video-%s-%d.mp4" % (_safe_stamp(), int(index)))
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def _safe_stamp():
    return re.sub(r"[^0-9A-Za-z-]+", "-", time.strftime("%Y%m%d-%H%M%S"))


# ---------------------------------------------------------------- 输入图

#: 私网/环回/链路本地/保留段一律拒绝。与 wb_proxy._is_public_addr 同一判据，
#: 但那一份在 wb_proxy 里（本模块不能 import 它，见 `_error_body` 的注释）。
def is_public_ip(text):
    try:
        address = ipaddress.ip_address(str(text).split("%")[0])
    except ValueError:
        return False
    return not (address.is_private or address.is_loopback or address.is_link_local
                or address.is_reserved or address.is_multicast or address.is_unspecified)


#: 预签名上传接口（从官方客户端 `CosUploadService.getPresignedUrls` 逆向出来的：
#: 客户端自己就是这么传参考视频的，所以这条路和官方行为一致）。
UPLOAD_PRESIGN_PATH = "/console/as/support/presigned_url"


def _upload_object_key(filename):
    """客户端 `generateUploadObjectKey` 的同款格式：uploads/<毫秒>-<随机8>-<文件名>。"""
    base = os.path.basename(filename or "") or "file"
    stem, dot, ext = base.rpartition(".")
    if not dot or not re.fullmatch(r"[A-Za-z0-9]{1,8}", ext or ""):
        stem, ext = base, ""
    # 只清洗主干、**扩展名单独保留**：中文名（"舞蹈 参考.mp4"）整段被清洗成下划线后
    # 再 lstrip 会把 ".mp4" 一起吃掉，剩下 "mp4" —— 上游拿不到扩展名就判不出类型。
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)
    stem = re.sub(r"_{2,}", "_", stem).strip("._") or "file"
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    tail = ("." + ext) if ext else ""
    return "uploads/%d-%s-%s%s" % (int(time.time() * 1000), suffix, stem[:110], tail)


def upload_reference_file(account, data, filename, timeout=UPLOAD_TIMEOUT_SECONDS):
    """把一段字节上传到上游 COS，返回**上游能抓到的** download_url。

    为什么需要它：上游对 `reference_videos` / `reference_audios` 的 `video_url`
    是**真去下载的**，本地路径与 data URL 都抓不到（实测 2013
    `media url unreachable`）。官方客户端的做法是「预签名 → PUT 到 COS →
    把 download_url 填进 video_url」，这里照抄同一套。

    签名有效期 24 小时（接口返回 `expire: 86400`），所以上传后要尽快提交任务，
    隔天再用旧地址会重新变成 2013。
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise wb_media.MediaError("INVALID_ARGS", "上传内容为空")
    if len(data) > wb_media.MAX_INPUT_IMAGE_BYTES:
        raise wb_media.MediaError(
            "INVALID_ARGS",
            "参考素材 %d 字节，超过 %d 字节上限"
            % (len(data), wb_media.MAX_INPUT_IMAGE_BYTES))
    object_key = _upload_object_key(filename)
    headers = dict(account.headers(purpose="media", media_identity=_IDENTITY))
    headers["Content-Type"] = "application/json"
    body = json.dumps({"object_keys": [object_key]}).encode("utf-8")
    request = urllib.request.Request(account.chat_base_url() + UPLOAD_PRESIGN_PATH,
                                     method="POST", data=body, headers=headers)
    try:
        with wb_accounts.urlopen(request, timeout=timeout, proxy=account.proxy) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise wb_media.MediaError(
            "UPSTREAM", "预签名失败（HTTP %d）" % exc.code, status=exc.code)
    items = ((payload or {}).get("data") or {}).get("items") or []
    if not items:
        raise wb_media.MediaError("UPSTREAM", "预签名没有返回上传地址")
    item = items[0]
    put = urllib.request.Request(item["upload_url"], method="PUT",
                                 data=bytes(data),
                                 headers={"Content-Type": "application/octet-stream"})
    try:
        with wb_accounts.urlopen(put, timeout=timeout, proxy=account.proxy) as response:
            if response.status not in (200, 201, 204):
                raise wb_media.MediaError(
                    "UPSTREAM", "上传到 COS 失败（HTTP %d）" % response.status)
    except urllib.error.HTTPError as exc:
        raise wb_media.MediaError(
            "UPSTREAM", "上传到 COS 失败（HTTP %d）" % exc.code, status=exc.code)
    return item["download_url"]


def assert_public_url(url, label="输入图"):
    """校验一个 http(s) URL 指向公网地址；不合法就抛 INVALID_ARGS。

    防的是 SSRF：网关可能监听在 LAN 上（`start-wb-proxy-lan.bat`），一个
    `image: "http://192.168.1.10:8080/admin"` 就能拿它当内网扫描器；
    `http://127.0.0.1:8788/accounts` 更直接——自己打自己。

    `label` 只影响报错文案。参考视频/音频走同一个判据，但把它们说成"输入图"
    会让调用方查错地方——传了本地 mp4 收到"输入图 URL 只支持 http/https"，
    第一反应通常是去改首帧，而不是换掉那段视频。
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise wb_media.MediaError(
            "INVALID_ARGS",
            "%s URL 只支持 http/https（给定 %s）：本网关不接受本地文件或 data URL，"
            "请先把素材放到公网 http(s) 地址" % (label, parsed.scheme))
    host = parsed.hostname
    if not host:
        raise wb_media.MediaError("INVALID_ARGS", "%s URL 没有主机名：%s" % (label, url))
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except Exception as exc:
        raise wb_media.MediaError(
            "INVALID_ARGS", "%s URL 解析失败（%s）：%s" % (label, host, exc))
    if not infos:
        raise wb_media.MediaError(
            "INVALID_ARGS", "%s URL 解析不到地址：%s" % (label, host))
    for info in infos:
        address = info[4][0]
        if not is_public_ip(address):
            raise wb_media.MediaError(
                "INVALID_ARGS",
                "%s URL 指向非公网地址 %s，已拒绝（防 SSRF）：%s" % (label, address, url))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁掉 3xx 跟随：跟随重定向是 SSRF 的标准绕过方式。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _no_redirect_opener():
    opener = urllib.request.build_opener(_NoRedirect)
    return opener


_MIME_BY_EXTENSION = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}


def sniff_image_mime(data, fallback="image/png"):
    """按魔数嗅探图片类型，照抄官方 CLI 的 detectMimeTypeFromBase64。

    认不出来时兜底 png：宁可让上游按 png 试一次，也不要因为一个没见过的头
    拒掉用户给的图。
    """
    if len(data) >= 4 and data[0] == 0x89 and data[1] == 0x50 \
            and data[2] == 0x4E and data[3] == 0x47:
        return "image/png"
    if len(data) >= 3 and data[0] == 0xFF and data[1] == 0xD8 and data[2] == 0xFF:
        return "image/jpeg"
    if len(data) >= 4 and data[:4] == b"GIF8":
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 2 and data[:2] == b"BM":
        return "image/bmp"
    return fallback


def _mime_for_url(url):
    path = urllib.parse.urlsplit(url).path.lower()
    dot = path.rfind(".")
    if dot < 0:
        return None
    return _MIME_BY_EXTENSION.get(path[dot:])


def _read_input_image(url, timeout=INPUT_IMAGE_TIMEOUT_SECONDS):
    """下载客户端给的输入图，返回 (bytes, 声明的 content-type)。"""
    assert_public_url(url)
    request = urllib.request.Request(url, method="GET")
    try:
        with _no_redirect_opener().open(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status >= 300:
                raise wb_media.MediaError(
                    "INVALID_ARGS",
                    "输入图 URL 返回了重定向或错误（HTTP %d），已拒绝跟随：%s" % (status, url))
            # 先看 Content-Length 再读，省得为一个明显超限的地址把内存打满。
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    if int(declared) > _INPUT_READ_CEILING:
                        raise wb_media.MediaError(
                            "INVALID_ARGS",
                            "输入图 %.1f MB，超过 %.0f MB 上限"
                            % (int(declared) / 1048576.0,
                               wb_media.MAX_INPUT_IMAGE_BYTES / 1048576.0))
                except ValueError:
                    pass
            data = response.read(_INPUT_READ_CEILING + 1)
            content_type = response.headers.get("Content-Type")
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise wb_media.MediaError(
                "INVALID_ARGS", "输入图 URL 返回重定向（HTTP %d），已拒绝跟随：%s"
                % (exc.code, url))
        raise wb_media.MediaError(
            "INVALID_ARGS", "下载输入图失败（HTTP %d）：%s" % (exc.code, url))
    except wb_media.MediaError:
        raise
    except Exception as exc:
        raise wb_media.MediaError("INVALID_ARGS", "下载输入图失败：%s" % (exc,))
    return data, content_type


def resolve_input_image(value, timeout=INPUT_IMAGE_TIMEOUT_SECONDS):
    """把客户端给的输入图归一成上游唯一接受的形态。

    - `data:image/*;base64,...` → 校验解码后体积，原样返回
    - 公网 `http(s)://`        → 校验可达性与公网地址、禁跟随重定向、下载后编成 data URL
    - 其它（含本地路径）        → `INVALID_ARGS`，说明为什么

    体积按**解码后**的字节数算：base64 比原始字节长约 4/3，拿字符串长度比上限度
    会把一张合法的 6 MB 图误判成超限（并在报错里印出一个虚高的 MB 数）。
    """
    kind, text = wb_media.classify_image_input(value)
    if kind == "data_url":
        wb_media.assert_input_image_bytes(
            wb_media.decoded_base64_length(text), "输入图（内联 data URL）")
        return text
    data, content_type = _read_input_image(text, timeout)
    wb_media.assert_input_image_bytes(len(data), "输入图（%s）" % text)
    declared = (content_type or "").split(";")[0].strip().lower()
    mime = declared if declared.startswith("image/") else (
        _mime_for_url(text) or sniff_image_mime(data))
    return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))
