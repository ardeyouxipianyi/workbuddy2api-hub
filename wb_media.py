#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb_media.py —— WorkBuddy 生图 / 生视频的协议层（纯逻辑，零 I/O）。

这一层只做四件事：**认目录、拼请求、解响应、判错误**。它不发请求、不读文件、
不碰账号，所以能被单测完整覆盖，也能被任何调用方复用（HTTP 路由、脚本、将来的
CLI）。所有网络与文件动作都在 `wb_media_client.py`。

## 协议来源

端点与请求体照抄 WorkBuddy 官方 CLI 的 `ImageService` / `VideoService`
（`WorkBuddy/resources/app.asar.unpacked/cli/dist/codebuddy-lite-wb.mjs`），
与独立插件 dsh-connect-workbuddy 的取证结论一致：

    POST {chat_base}/v2/images/generations     body={model,prompt,size,...按前缀分支}
    POST {chat_base}/v2/images/edits           body=上面 + image:[dataURL,...]
    POST {chat_base}/v2/videos/generations     body={prompt,model,seconds,negative_prompt,
                                                     watermark:false(网关口径),image_url?,
                                                     extra_parameters:{resolution,enable_audio,
                                                                       aspect_ratio(仅文生)}}
    POST {chat_base}/v2/videos/tasks           body={task_id}   → status/ videos[]/ usage.credit

信封一律是 `{code, msg, request_id, data}`；**HTTP 200 也可能 `code != 0`**，所以每个
解析函数都先看 code。

## 与 dsh 插件的差别（改过的三处，都有理由）

1. **不接受本地路径**。dsh 让模型传 `image: "C:\\\\Users\\\\me\\\\pic.png"`，因为它是
   本机单用户插件。网关面对的是任意 API 客户端——接受路径等于开放任意文件读取
   （读服务器上的任何文件并发到上游图像接口）。这里只认 data URL 与公网 http(s)。
2. **不写死 prompt 上限 2500**。本机目录里 kling 是 2500、minimax 只有 2000，逐模型取。
3. **多搬了 `videoGeneration` 的字段**。dsh 只搬 5 个（resolutions/defaultResolution/
   defaultSeconds/minSeconds/maxSeconds），而本机目录还下发 `aspectRatios`、
   `defaultAspectRatio`、`maxPromptLength`、`audioMode`。既然有，就该拿来校验，
   而不是在代码里再猜一份默认值。

## 对外形状由这里决定

`public_task()` 产出 OpenAI Video API 的形状（`status` ∈ queued/in_progress/completed/
failed），内部却保留五态（多出 `rejected` 与 `lost`）。两者的映射函数在
`status_out()` / `error_code_for()`，**前端、文档、测试必须共用这一份**，否则
「同一个失败」在不同地方会有三种说法。
"""
import base64
import json
import re

# ---------------------------------------------------------------- 标签

#: 目录里标记「文生图」的 tag。
TAG_TEXT_TO_IMAGE = "text-to-image"
#: 「图生图 / 改图」。
TAG_IMAGE_TO_IMAGE = "image-to-image"
#: 「文生视频」。
TAG_TEXT_TO_VIDEO = "text-to-video"
#: 「图生视频」。
TAG_IMAGE_TO_VIDEO = "image-to-video"

#: 本模块认作媒体模型的全部标签。目录只读一次就返回全部媒体条目，
#: 调用方按 tag 分组——比每种能力各读一遍 /v3/config 少一次往返。
MEDIA_TAGS = (TAG_TEXT_TO_IMAGE, TAG_IMAGE_TO_IMAGE,
              TAG_TEXT_TO_VIDEO, TAG_IMAGE_TO_VIDEO)

# ---------------------------------------------------------------- 上限与默认值

#: 生图总像素上限。dsh 逐档实测：2048x2048（4.19 MP）通过，2560x2560（6.55 MP）
#: 被直接拒绝且**不会悄悄降级**。所以这条必须在本地拦——发上去只会换一个 400，
#: 而客户端看到的错误里没有任何线索指向尺寸。
IMAGE_MAX_PIXELS = 4 * 1024 * 1024

#: 输入图体积上限（按**解码后**字节数算；base64 比原始字节长 4/3，拿字符串长度比
#: 会把一张合法的 6 MB 图误判成超限）。
MAX_INPUT_IMAGE_BYTES = 8 * 1024 * 1024

#: OpenAI Images 的默认尺寸，也是官方 CLI 的默认值。
DEFAULT_IMAGE_SIZE = "1024x1024"

#: 视频默认时长（秒），模型没给 defaultSeconds 时用它。
DEFAULT_VIDEO_SECONDS = 5
#: 视频默认比例，仅文生视频使用。
DEFAULT_VIDEO_ASPECT_RATIO = "16:9"
#: 官方 CLI 支持的比例集合（模型自己的 aspectRatios 缺失时用它兜底）。
VIDEO_ASPECT_RATIOS = ("16:9", "9:16", "1:1")
#: prompt 长度上限的兜底值，模型给了 maxPromptLength 时以模型为准。
PROMPT_LIMIT_DEFAULT = 2500

#: 分辨率档位表：短边像素。映射 OpenAI 的 `size`（宽x高）到目录的档位名。
RESOLUTION_LADDER = {
    "480P": 480,
    "720P": 720,
    "768P": 768,
    "1080P": 1080,
    "2K": 1440,
    "1440P": 1440,
    "4K": 2160,
}

#: 成本预估值（积分），只用于「这个账号还付得起吗」这一道闸，**不用于报价**。
#: 目录条目里根本没有价格字段，价格只在结算时以 usage.credit 返回；所以这里取
#: dsh 实测的最贵那条当上界：kling 文生 25.71/s → 26；图片 5.71 → 6。
COST_IMAGE_ESTIMATE = 6.0
COST_VIDEO_PER_SECOND_CEILING = 26.0


class MediaError(Exception):
    """媒体链路的错误。带稳定的 `code`，调用方据此决定换号/冷却/直接回。

    code 取值：
      INVALID_ARGS    参数或输入图不合法（本地拦下，**没花积分**）
      NO_MEDIA_MODEL  该区目录里没有这一组模型（没花积分）
      NO_ROUTE        该区没有这条媒体路由（HTTP 404）
      AUTH            凭据被上游拒绝（401/403）
      NO_CREDIT       积分不足（402）
      RATE_LIMIT      上游限流（429）
      REJECTED        **提交阶段**被上游 4xx 退回：任务没建、积分没花，可立刻重试
      UPSTREAM        其它上游失败 / 网络
      EMPTY_RESULT    上游答成功但没有产物
    """

    def __init__(self, code, message, status=0, detail=""):
        Exception.__init__(self, message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail


# ---------------------------------------------------------------- 小工具

def _as_string(value):
    if isinstance(value, str) and value.strip() != "":
        return value
    return None


def _as_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_int(value):
    number = _as_number(value)
    return None if number is None else int(number)


def _string_list(value):
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for item in value:
        text = _as_string(item)
        if text is not None:
            out.append(text)
    return out


def _record(value):
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------- 目录

def models_from_payload(payload):
    """从 `/v3/config` 响应里取出 `data.models` 原文（不投影、不筛选）。

    上游把模型清单放在 `data.models`；本仓库的 `parse_remote_catalog()` 会把同一份
    文档投影成聊天目录并丢掉媒体条目，所以媒体这一层必须自己读原文。
    兼容三种形状（完整信封 / 只有 data / 直接是 models 数组的包装），
    是为了让「从留档文件冷启动」与「从内存缓存读」走同一个解析入口。
    """
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, dict):
        models = data.get("models")
        if isinstance(models, list):
            return models
    models = payload.get("models")
    if isinstance(models, list):
        return models
    return []


def read_video_generation(raw):
    """把目录条目上的 `videoGeneration` 读成已知字段的配置；缺省即不写键。

    只搬真的会用的字段，**不补默认值**：`minSeconds`/`maxSeconds` 缺失就代表
    "上游没说"，此时不校验、把判断交给上游，而不是猜一个区间去拦用户。
    """
    if not isinstance(raw, dict):
        return None
    config = {}
    resolutions = _string_list(raw.get("resolutions"))
    if resolutions:
        config["resolutions"] = resolutions
    for key in ("defaultResolution", "defaultAspectRatio", "audioMode"):
        text = _as_string(raw.get(key))
        if text is not None:
            config[key] = text
    for key in ("defaultSeconds", "minSeconds", "maxSeconds", "maxPromptLength",
                "maxReferenceImages", "maxReferenceVideos", "maxReferenceAudios",
                "maxReferenceMaterials"):
        number = _as_int(raw.get(key))
        if number is not None:
            config[key] = number
    # 素材相关的能力开关。**只有上游明确写 true 才算 true**：缺省时按"不支持"处理，
    # 因为多送素材比少送更容易被上游拒（首尾帧与参考素材可能互斥）。
    for key in ("allowMixedReferenceMaterials", "framesExclusiveWithReferenceMaterials",
                "supportsFirstFrame", "supportsFirstLastFrame",
                "lastFrameRequiresFirstFrame", "referenceAudioRequiresVisualMaterial"):
        flag = raw.get(key)
        if isinstance(flag, bool):
            config[key] = flag
    aspect_ratios = _string_list(raw.get("aspectRatios"))
    if aspect_ratios:
        config["aspectRatios"] = aspect_ratios
    default_audio = raw.get("defaultEnableAudio")
    if isinstance(default_audio, bool):
        config["defaultEnableAudio"] = default_audio
    return config or None


def media_models(payload, tag=None):
    """从 `/v3/config` 响应里挑出媒体模型：按 tag 过滤、按 id 去重。

    `tag` 缺省时按 {@link MEDIA_TAGS} 过滤，也就是「本区有哪些媒体模型」——
    聊天模型（`tags: ["craft"]`）一律不进结果集。这是函数名该有的语义：
    调用方问的是媒体，返回 chat 模型只会让前端清单多出几行不该出现的条目。
    指定 `tag`（单个字符串或一组标签，命中任一即算）时再细分到某种能力。

    去重按 `id`：目录是外部数据，重复条目不该让前端或参数里出现两个同名选项。
    第一个出现的条目胜出（保持上游顺序，前端清单的顺序才稳定）。
    """
    if isinstance(tag, str):
        wanted = [tag]
    elif isinstance(tag, (list, tuple)):
        wanted = [one for one in tag if isinstance(one, str)]
    else:
        wanted = list(MEDIA_TAGS)
    out = []
    seen = set()
    for entry in models_from_payload(payload):
        if not isinstance(entry, dict):
            continue
        model_id = _as_string(entry.get("id"))
        if model_id is None or model_id in seen:
            continue
        tags = _string_list(entry.get("tags"))
        if not tags:
            continue
        if wanted is not None and not any(one in tags for one in wanted):
            continue
        seen.add(model_id)
        item = {
            "id": model_id,
            "name": _as_string(entry.get("name")) or model_id,
            "tags": tags,
        }
        generation = read_video_generation(entry.get("videoGeneration"))
        if generation is not None:
            item["videoGeneration"] = generation
        out.append(item)
    return out


def _tag_label(tag):
    return {
        TAG_TEXT_TO_IMAGE: "文生图",
        TAG_IMAGE_TO_IMAGE: "图生图",
        TAG_TEXT_TO_VIDEO: "文生视频",
        TAG_IMAGE_TO_VIDEO: "图生视频",
    }.get(tag, tag)


def pick_model(models, requested, tag):
    """按能力标签选一个模型：点名的必须**在这一组里**，否则取组内第一个。

    点名但不在组内时直接报错并列出可用项，而不是把它发上去换一个上游 404
    （那样报错只会是 `route config not found`，读者无从知道是名字写错了、
    还是该区这一组压根没有模型）。拿文生图模型去改图也走这条路径。
    """
    group = [model for model in models if tag in model.get("tags", ())]
    requested = _as_string(requested)
    if requested is not None:
        for model in group:
            if model["id"] == requested:
                return model
        known = "、".join(model["id"] for model in group) or "（空）"
        raise MediaError(
            "NO_MEDIA_MODEL",
            "目录里没有%s模型「%s」。可用：%s" % (_tag_label(tag), requested, known),
        )
    if not group:
        raise MediaError(
            "NO_MEDIA_MODEL",
            "该区账号的产品配置里没有任何带 %s 标签的模型（%s）。"
            "国内区文生图应是 hunyuan-image-alpha、改图 hunyuan-image-alpha-edit、"
            "视频是 kling-video-v3 / minimax-video-h3；一个都没有说明该区目录暂时"
            "没有这类模型。" % (tag, _tag_label(tag)),
        )
    return group[0]


# ---------------------------------------------------------------- 生图参数

_SIZE_PATTERN = re.compile(r"^(\d{2,5})\s*[xX×*]\s*(\d{2,5})$")


def parse_size(size):
    """把尺寸字符串解析成 (宽, 高)。`auto`/None 走默认值，非法形状抛 INVALID_ARGS。"""
    text = _as_string(size)
    if text is None or text.strip().lower() == "auto":
        text = DEFAULT_IMAGE_SIZE
    match = _SIZE_PATTERN.match(text.strip())
    if match is None:
        raise MediaError(
            "INVALID_ARGS",
            "size 要写成「宽x高」，如 1024x1024、1536x1024、1024x1536（给定 %r）" % (size,),
        )
    width = int(match.group(1))
    height = int(match.group(2))
    if width < 16 or height < 16:
        raise MediaError("INVALID_ARGS", "size 的宽高都得大于 16 像素（给定 %s）" % text)
    return width, height


def check_size(size):
    """校验并规范化尺寸，返回 `"宽x高"`。

    上限按**总像素**算，不是长边——dsh 逐档实测的那条边界就是像素数：
    `2560x1600`（4.10 MP）通过、`2560x2560`（6.55 MP）被拒。所以本地拦在 419 万，
    超了直接报，别让客户端收到一个指不出原因的 400。
    """
    width, height = parse_size(size)
    pixels = width * height
    if pixels > IMAGE_MAX_PIXELS:
        raise MediaError(
            "INVALID_ARGS",
            "size %dx%d = %.2f MP 超过上限 %.2f MP。上限是按总像素算的："
            "2048x2048、2048x1536、2560x1440、2560x1600 都在限内，"
            "2560x2560 起会被上游直接拒绝且不会降级。"
            % (width, height, pixels / 1000000.0, IMAGE_MAX_PIXELS / 1000000.0),
        )
    return "%dx%d" % (width, height)


def build_image_body(model_id, prompt, size, count=1):
    """按官方 CLI `buildBaseRequestBody` 的前缀分支拼文生图请求体。

    - `hunyuan-*`：只额外带 `n`，**不带** `response_format`
    - `gemini-*` ：什么都不加
    - 其它      ：带 `response_format: "b64_json"`

    `count != 1` 一律拒：本网关一次只出一张图。静默只给一张会让客户端以为
    拿到了四张（并且按四张付费的预期落空），这比报错更糟。
    """
    prompt = _as_string(prompt)
    if prompt is None:
        raise MediaError("INVALID_ARGS", "prompt 必填，且必须是非空字符串")
    model_id = _as_string(model_id)
    if model_id is None:
        raise MediaError("INVALID_ARGS", "model 必填")
    if count != 1:
        raise MediaError(
            "INVALID_ARGS",
            "本网关一次生成一张图（n 只能为 1）。要更多请多次调用——"
            "每张都真实扣积分，不做不可控的批量支出。",
        )
    body = {"model": model_id, "prompt": prompt, "size": check_size(size)}
    if model_id.startswith("hunyuan-"):
        body["n"] = 1
        return body
    if model_id.startswith("gemini-"):
        return body
    body["response_format"] = "b64_json"
    return body


def build_edit_body(model_id, prompt, size, images):
    """改图请求体：生图那一套 + `image`（**data URL 字符串数组**）+ 可选 `input_fidelity`。

    上游只认数组，单个字符串会被拒——所以这里连类型都挡掉。
    """
    data_urls = _string_list(images)
    if not data_urls:
        raise MediaError(
            "INVALID_ARGS",
            "改图要带 image（data URL）。没有输入图的改图请求是调用方的拼接错误，"
            "不是可以重试的上游问题。",
        )
    for one in data_urls:
        if not one.startswith("data:image/"):
            raise MediaError(
                "INVALID_ARGS",
                "改图的 image 必须已经归一成 data:image/*;base64,... 形式",
            )
    body = build_image_body(model_id, prompt, size)
    body["image"] = data_urls
    return body


# ---------------------------------------------------------------- 视频参数

def _coerce_seconds(value):
    """把 OpenAI 的字符串秒数（`"5"`）或数字都收成 int；认不出返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        return int(number) if float(number).is_integer() else None
    number = _as_number(value)
    if number is None:
        return None
    return int(number) if float(number).is_integer() else None


def size_to_resolution(size, resolutions, default=None):
    """OpenAI 的 `size`（宽x高）→ 目录的档位名（`720P`/`768P`/`1080P`/`2K`/`4K`）。

    按**短边**分档，不是长边：竖版 `720x1280` 的短边才是「720 这一档」，
    按长边会把它判成 1280P 而该档不存在。选档规则是「不超过短边的最高一档」，
    所以 `1280x720` 在 `["720P","1080P","4K"]` 里落到 `720P`。

    返回 `(档位名, 告警或 None)`。一个档都够不上时回落到 `default` 并带告警——
    **不编一个档位**交给上游。
    """
    width, height = parse_size(size)
    short = min(width, height)
    candidates = []
    for name in resolutions or ():
        pixels = RESOLUTION_LADDER.get(str(name).upper())
        if pixels is not None and pixels <= short:
            candidates.append((pixels, name))
    if not candidates:
        return default, (
            "size %dx%d 低于该模型的最低分辨率档（%s）；改用 %s"
            % (width, height, "、".join(resolutions or ()) or "未声明", default or "上游默认")
        )
    candidates.sort(key=lambda pair: pair[0])
    return candidates[-1][1], None


def aspect_ratio_from_size(size):
    """`size` 的宽高比 → 比例字符串。仅文生视频用；图生视频由输入图决定。"""
    width, height = parse_size(size)
    if width > height:
        return "16:9"
    if width < height:
        return "9:16"
    return "1:1"


def resolve_video_params(request, generation=None):
    """校验并回落一次生视频请求的参数，返回可直接拼 body 的字典。

    返回 `{"seconds","resolution","aspect_ratio","enable_audio","warnings"}`。
    抛 `MediaError('INVALID_ARGS')` 的情况：prompt 超长、秒数越界、分辨率/比例非法。

    核心判据是「**知道才拦，不知道就透传**」：
      - 模型给了 `minSeconds`/`maxSeconds` 才校验秒数；没给就原样发给上游，让上游判
      - 模型给了 `resolutions[]` 才校验分辨率；没给就原样透传
    盲拒会让同一个参数在有的账号上能用、有的账号上不能用，比不拦更难解释。
    """
    generation = generation or {}
    request = request or {}

    prompt = request.get("prompt")
    prompt = prompt if isinstance(prompt, str) else ""
    if prompt.strip() == "":
        # 必填检查放在长度检查之前：空 prompt 不是"太短"，是根本没给。
        raise MediaError("INVALID_ARGS", "prompt 必填，且必须是非空字符串")
    limit = generation.get("maxPromptLength") or PROMPT_LIMIT_DEFAULT
    if len(prompt) > limit:
        raise MediaError(
            "INVALID_ARGS",
            "prompt 不能超过 %d 字符（该模型的 maxPromptLength）" % limit,
        )
    negative = request.get("negative_prompt")
    if isinstance(negative, str) and len(negative) > limit:
        raise MediaError(
            "INVALID_ARGS",
            "negative_prompt 不能超过 %d 字符（该模型的 maxPromptLength）" % limit,
        )

    warnings = []
    has_image = bool(_as_string(request.get("image")) or _as_string(request.get("input_reference")))
    # 参考图同样是"有输入图"：选模型要按 image-to-video 那一组走。
    # 这里用 try 是因为 read_reference_materials 会做数量校验并抛 MediaError——
    # 校验失败要在**估算花费之前**抛出去，不能等到提交才发现。
    references = read_reference_materials(request, generation)
    if references.get("reference_images"):
        has_image = True
    image_to_video = has_image

    # ---- 时长
    raw_seconds = request.get("seconds")
    seconds = _coerce_seconds(raw_seconds) if raw_seconds is not None else None
    if raw_seconds is not None and seconds is None:
        raise MediaError(
            "INVALID_ARGS",
            "seconds 要写成秒数（如 5 或 \"5\"），给定 %r" % (raw_seconds,),
        )
    if seconds is None:
        seconds = generation.get("defaultSeconds") or DEFAULT_VIDEO_SECONDS
    low = generation.get("minSeconds")
    high = generation.get("maxSeconds")
    if low is not None and high is not None and not (low <= seconds <= high):
        raise MediaError(
            "INVALID_ARGS",
            "时长必须在 %d 到 %d 秒之间（给定 %d）" % (low, high, seconds),
        )

    # ---- 分辨率：显式 resolution 优先，其次 size 映射，再次模型默认
    resolutions = generation.get("resolutions") or []
    default_resolution = generation.get("defaultResolution")
    asked_resolution = _as_string(request.get("resolution"))
    asked_size = _as_string(request.get("size"))
    resolution = default_resolution
    if asked_resolution is not None:
        if resolutions:
            hit = asked_resolution if asked_resolution in resolutions else next(
                (one for one in resolutions
                 if one.lower() == asked_resolution.lower()), None)
            if hit is None:
                warnings.append(
                    "resolution %s 不被该模型支持（可选 %s）；改用 %s"
                    % (asked_resolution, "、".join(resolutions), default_resolution or "上游默认")
                )
                resolution = default_resolution
            else:
                resolution = hit
        else:
            resolution = asked_resolution
    elif asked_size is not None:
        resolution, warning = size_to_resolution(asked_size, resolutions, default_resolution)
        if warning is not None:
            warnings.append(warning)

    # ---- 比例：仅文生视频；图生视频由输入图决定，一个字段都不发
    aspect_ratio = None
    if not image_to_video:
        allowed = generation.get("aspectRatios") or list(VIDEO_ASPECT_RATIOS)
        asked = _as_string(request.get("aspect_ratio"))
        if asked is None:
            if asked_size is not None:
                asked = aspect_ratio_from_size(asked_size)
            else:
                asked = generation.get("defaultAspectRatio") or DEFAULT_VIDEO_ASPECT_RATIO
        hit = asked if asked in allowed else next(
            (one for one in allowed if one.lower() == asked.lower()), None)
        if hit is None:
            # aspectRatios 里可能有 21:9 / adaptive 这类我们不能主动挑的值，
            # 所以「不在集合里」不是错误，而是回落到默认并告警。
            warnings.append(
                "aspect_ratio %s 不在该模型的可选集合（%s）；改用 %s"
                % (asked, "、".join(allowed), generation.get("defaultAspectRatio") or DEFAULT_VIDEO_ASPECT_RATIO)
            )
            hit = generation.get("defaultAspectRatio") or DEFAULT_VIDEO_ASPECT_RATIO
        aspect_ratio = hit

    # ---- 音频：模型声明常开时，用户关不掉，如实告警而不是假装做到了
    enable_audio = request.get("enable_audio")
    if not isinstance(enable_audio, bool):
        enable_audio = generation.get("defaultEnableAudio")
    if not isinstance(enable_audio, bool):
        enable_audio = True
    if enable_audio is False and generation.get("audioMode") == "always-on":
        warnings.append("该模型音频常开（audioMode=always-on），enable_audio=false 可能被忽略")
        enable_audio = True

    return {
        "seconds": seconds,
        "resolution": resolution,
        "aspect_ratio": aspect_ratio,
        "enable_audio": enable_audio,
        "image_to_video": image_to_video,
        "warnings": warnings,
        # 目录能力（含各类参考素材上限）：build_video_body 要用它做第二道校验，
        # 顺手带出来省得调用方再传一遍。
        "generation": generation,
        "references": references,
    }


#: 参考素材的三种类别：对外字段名 → (上游字段名, 数量上限键, 单项 URL 键, 中文名)
REFERENCE_KINDS = (
    ("reference_images", "reference_images", "maxReferenceImages", "image_url", "参考图"),
    ("reference_videos", "reference_videos", "maxReferenceVideos", "video_url", "参考视频"),
    ("reference_audios", "reference_audios", "maxReferenceAudios", "audio_url", "参考音频"),
)


def _reference_list(request, key):
    """把请求里的参考素材读成 `[url, …]`。

    容忍三种写法，因为客户端各有各的习惯：`["url"]`、`[{"image_url": "url"}]`
    （上游原始形状）、以及 OpenAI Video API 的 `input_reference` 数组。读不出来
    的条目直接跳过——宁可少一条素材，也不要因为一个字段形态猜错就整单失败。
    """
    raw = request.get(key)
    if raw is None and key == "reference_images":
        raw = request.get("images")
    if raw is None:
        return []
    if isinstance(raw, (str, bytes)):
        raw = [raw]
    if not isinstance(raw, list):
        raise MediaError("INVALID_ARGS", "%s 要是数组（给定 %s）" % (key, type(raw).__name__))
    out = []
    url_key = {k: uk for k, _, _, uk, _ in REFERENCE_KINDS}[key]
    for item in raw:
        if isinstance(item, str):
            text = _as_string(item)
        elif isinstance(item, dict):
            text = _as_string(item.get(url_key)) or _as_string(item.get("url"))
        else:
            text = None
        if text:
            out.append(text)
    return out


def read_reference_materials(request, generation):
    """校验并整理参考素材，返回 `{上游字段名: [url, …]}`（空 dict 表示没有素材）。

    三条约束逐条对着目录声明检查，不猜：

    1. 每个类别的数量不得超过 `maxReferenceXxx`；
    2. 总数不得超过 `maxReferenceMaterials`（minimax 是 12，各类上限加起来是 15，
       所以真的有"合计"这道闸）；
    3. `framesExclusiveWithReferenceMaterials` 为真时，**首/尾帧与参考素材互斥**
       —— minimax 就是这样，同时发等于让上游在两套控制方式之间挑。

    上游没声明的上限不校验（缺省代表"上游没说"，把判断交给它）。
    """
    generation = generation or {}
    request = request or {}
    found = {}
    for key, upstream_key, cap_key, _url_key, label in REFERENCE_KINDS:
        items = _reference_list(request, key)
        if not items:
            continue
        cap = generation.get(cap_key)
        if cap is not None and len(items) > cap:
            if cap == 0:
                raise MediaError(
                    "INVALID_ARGS",
                    "该模型不支持%s（%s = 0）；去掉 %s 再提交" % (label, cap_key, key))
            raise MediaError(
                "INVALID_ARGS",
                "%s最多 %d 个（给定 %d 个）" % (label, cap, len(items)))
        found[upstream_key] = items

    if found:
        total = sum(len(v) for v in found.values())
        total_cap = generation.get("maxReferenceMaterials")
        if total_cap is not None and total > total_cap:
            raise MediaError(
                "INVALID_ARGS",
                "参考素材合计最多 %d 个（给定 %d 个）" % (total_cap, total))

        has_frame = bool(_as_string(request.get("image"))
                         or _as_string(request.get("input_reference"))
                         or _as_string(request.get("last_image")))
        if has_frame and generation.get("framesExclusiveWithReferenceMaterials"):
            raise MediaError(
                "INVALID_ARGS",
                "该模型的首/尾帧与参考素材互斥：要么给首帧（image / input_reference），"
                "要么给参考图（%s），不要同时给" % "、".join(sorted(found)))
        if not generation.get("allowMixedReferenceMaterials") and len(found) > 1:
            raise MediaError(
                "INVALID_ARGS",
                "该模型不允许混用多类参考素材（同时给了 %s）" % "、".join(sorted(found)))
    return found


def build_video_body(model_id, request, params):
    """拼提交任务的请求体，完全照抄官方 CLI 的 `submitTask`。

    `watermark` **固定 false**（不带"AI生成"字样），且不接受调用方覆盖：
    这是网关的统一口径，不属于对外参数。图生视频带 `image_url`
    （首帧）与可选 `last_image_url`（尾帧），**不带** `aspect_ratio`——
    输入图已经决定了比例，多发一个字段只会让上游在两套规则之间挑。
    """
    model_id = _as_string(model_id)
    if model_id is None:
        raise MediaError("INVALID_ARGS", "model 必填")
    request = request or {}
    params = params or {}
    body = {
        "prompt": request.get("prompt") or "",
        "model": model_id,
        "seconds": params.get("seconds"),
        "negative_prompt": request.get("negative_prompt") or "",
        # 水印固定关闭：上游 `CreateVideoRequest.watermark=false` 产出画面
        # 左上角没有"AI生成"字样（实测，见 docs/MEDIA-IMPL.md）。
        # **不对外暴露这个开关**——它是网关的统一口径，不由调用方决定。
        "watermark": False,
    }
    image = _as_string(request.get("image")) or _as_string(request.get("input_reference"))
    if image is not None:
        body["image_url"] = image
    last_image = _as_string(request.get("last_image"))
    if last_image is not None:
        body["last_image_url"] = last_image
    # 参考素材：上游形状是对象数组（`[]*v2.ReferenceImages`），不是字符串数组。
    # 字段名与内层键都是实测出来的，见 docs/MEDIA-IMPL.md §16。
    references = read_reference_materials(request, params.get("generation") or {})
    for upstream_key, items in (references or {}).items():
        inner = {k: uk for k, _, _, uk, _ in REFERENCE_KINDS}[upstream_key]
        body[upstream_key] = [{inner: url} for url in items]
    extra = {
        "resolution": params.get("resolution"),
        "enable_audio": params.get("enable_audio"),
    }
    if not params.get("image_to_video"):
        extra["aspect_ratio"] = params.get("aspect_ratio")
    body["extra_parameters"] = extra
    return body


_RANGE_PATTERN = re.compile(
    r"supported\s+range\s*:\s*(\d+(?:\.\d+)?)\s*(?:-|–|—|~|to)\s*(\d+(?:\.\d+)?)",
    re.I,
)


def parse_supported_seconds_range(text):
    """从上游的拒绝文案里读出它自己报的时长区间。

    实测文案形如 `unsupported video duration: 1 seconds, supported range: 4-15 seconds`。
    目录没有 `videoGeneration` 的账号本地无从校验，只能让上游先拒一次；把这段读出来
    记进本次进程，第二次同样的越界值就能在本地拦下（零请求、零冷却）。

    读不出来就返回 None —— **绝不猜**一个区间。
    """
    if not isinstance(text, str) or not text:
        return None
    match = _RANGE_PATTERN.search(text)
    if match is None:
        return None
    low = float(match.group(1))
    high = float(match.group(2))
    if low <= 0 or high < low:
        return None
    return int(low), int(high)


# ---------------------------------------------------------------- 响应解析

def _envelope(payload, what):
    """拆信封：先看 code，再交出 data。HTTP 200 也可能 code != 0。"""
    if not isinstance(payload, dict):
        raise MediaError("UPSTREAM", "上游返回的不是 JSON 对象（%s）" % what)
    code = payload.get("code")
    if isinstance(code, int) and code != 0:
        message = _as_string(payload.get("msg")) or "unknown error"
        request_id = _as_string(payload.get("request_id"))
        raise MediaError(
            "UPSTREAM",
            "%s失败（code %d）: %s%s" % (
                what, code, message,
                "" if request_id is None else "（request_id %s）" % request_id),
        )
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def parse_image_response(payload):
    """解析生图/改图的响应：`{url | b64_json, credit, created}`，两者至少一个。"""
    data = _envelope(payload, "生图")
    items = data.get("data")
    first = items[0] if isinstance(items, list) and items else None
    first = first if isinstance(first, dict) else {}
    url = _as_string(first.get("url"))
    b64 = _as_string(first.get("b64_json"))
    if url is None and b64 is None:
        raise MediaError(
            "EMPTY_RESULT",
            "上游返回成功，但 data.data 里既没有 url 也没有 b64_json",
        )
    usage = _record(data.get("usage"))
    outcome = {}
    if url is not None:
        outcome["url"] = url
    if b64 is not None:
        outcome["b64"] = b64
    credit = _as_number(usage.get("credit"))
    if credit is not None:
        outcome["credit"] = credit
    created = _as_int(data.get("created"))
    if created is not None:
        outcome["created"] = created
    revised = _as_string(first.get("revised_prompt"))
    if revised is not None:
        outcome["revised_prompt"] = revised
    return outcome


def parse_video_submission(payload):
    """解析提交响应：只要 `data.id`（任务 id）。没有 id 就无法进入轮询。"""
    data = _envelope(payload, "提交生视频任务")
    task_id = _as_string(data.get("id"))
    if task_id is None:
        raise MediaError(
            "EMPTY_RESULT",
            "上游没返回 task id，无法进入轮询",
        )
    return {"taskId": task_id}


def parse_video_task(payload):
    """解析轮询响应：`{status, videos, credit}`。

    未知 status 一律当「还没好」——把新状态误判成失败会浪费一次已经花钱的任务。
    """
    data = _envelope(payload, "轮询生视频任务")
    status = (_as_string(data.get("status")) or "unknown").lower()
    videos = []
    if status == "completed":
        items = data.get("data")
        for item in items if isinstance(items, list) else ():
            if isinstance(item, dict):
                url = _as_string(item.get("url"))
                if url is not None:
                    videos.append(url)
    usage = _record(data.get("usage"))
    credit = _as_number(usage.get("credit"))
    outcome = {"status": status, "videos": videos}
    if credit is not None:
        outcome["credit"] = credit
    error, error_code = parse_task_error(data)
    if error is not None:
        outcome["error"] = error
    if error_code is not None:
        outcome["error_code"] = error_code
    return outcome


def parse_task_error(data):
    """从轮询响应的 `data` 里读出失败原因，返回 `(message, code)`。

    上游的 `error` **是对象不是字符串**：

        "error": {"code": "2013",
                  "message": "2013: content[4].video_url: media url unreachable ..."}

    这是实测（2026-11-02）才发现的：原来这里用 `_as_string(data["error"])`，
    读到 dict 一律返回 None，于是所有上游失败都被报成"上游没有给出原因"——
    整整两轮带参考素材的排查因此白做（真实原因就写在这个 message 里：
    参考视频的地址上游抓不到，需要换可达的公网地址或 `mm_file://{file_id}`）。

    所以这里把 dict 形态当一等公民：内层 `message`/`msg`/`error_message` 取正文，
    `code` 单独留着给上层分类。字符串形态（老响应）仍然兼容。
    """
    raw = data.get("error")
    code = None
    if isinstance(raw, dict):
        message = (_as_string(raw.get("message")) or _as_string(raw.get("msg"))
                   or _as_string(raw.get("error_message")) or _as_string(raw.get("detail")))
        code = (_as_string(raw.get("code")) or _as_string(raw.get("error_code"))
                or _as_string(raw.get("type")))
        if message is None and code is not None:
            message = "上游错误码 %s（未给说明）" % code
        if message is None:
            # 认不出字段就带上原始 JSON，宁可难看也别再说"没有原因"。
            message = json.dumps(raw, ensure_ascii=False)[:400]
        return message, code
    message = _as_string(raw) or _as_string(data.get("error_message"))
    return message, None


# ---------------------------------------------------------------- 失败分类

def describe_media_failure(status, text, phase):
    """把一次非 2xx 的响应翻成带 code 的 MediaError。

    `phase` 决定 400 的归属：
      - `submit`：上游在建任务**之前**退回 → `REJECTED`，任务没建、积分没花，
                  不进冷却、改完参数可以立刻重试
      - 其它（`image` / `poll`）：一律 `UPSTREAM`

    这条分界不是洁癖：dsh 踩过 `seconds=1` 被 400 退回后还要白等 60 秒冷却的坑，
    用户原话是「上游返回这种错误，也要等半天」。
    """
    raw = text if isinstance(text, str) else ""
    detail = raw.strip()
    message = detail
    if detail.startswith("{"):
        try:
            parsed = json.loads(detail)
            if isinstance(parsed, dict):
                msg = _as_string(parsed.get("msg"))
                code = parsed.get("code")
                if msg is not None:
                    message = msg if code is None else "code %s: %s" % (code, msg)
        except ValueError:
            pass
    detail = detail[:2000]
    message = message[:400]

    if status in (401, 403):
        return MediaError("AUTH", "凭据被上游拒绝（HTTP %d）：%s" % (status, message),
                          status=status, detail=detail)
    if status == 402:
        return MediaError("NO_CREDIT", "积分不足（HTTP 402）：%s" % message,
                          status=status, detail=detail)
    if status == 429:
        return MediaError("RATE_LIMIT", "触发上游限流（HTTP 429）：%s" % message,
                          status=status, detail=detail)
    if status == 404:
        return MediaError("NO_ROUTE", "该区没有这条媒体路由（HTTP 404）：%s" % message,
                          status=status, detail=detail)
    if phase == "submit" and 400 <= status < 500:
        return MediaError("REJECTED", "上游拒绝了参数（未扣积分）：%s" % message,
                          status=status, detail=detail)
    return MediaError("UPSTREAM", "上游失败（HTTP %d）：%s" % (status, message),
                      status=status, detail=detail)


# ---------------------------------------------------------------- 输入图

_DATA_URL_PATTERN = re.compile(r"^data:([^;,]+);base64,(.+)$", re.S)


def classify_image_input(value):
    """判定输入图属于哪种形态，返回 `(kind, normalized)`；不合法就抛 INVALID_ARGS。

    **只接受两种**：`data:image/*;base64,...` 与公网 `http(s)://`。

    刻意不接受本地绝对路径与 `file://`。dsh 作为本机单用户插件接受它们是对的，
    而网关是给任意 API 客户端用的：接受路径等于开放任意文件读取——一个
    `image: "C:/Users/admin/.ssh/id_rsa"` 就能把服务器上的文件发到上游图像接口，
    失败时的报错还会泄露「这个文件存不存在」。需要本地图就请客户端自己读、自己编码。

    返回的 `normalized` 对 data URL 是原文（体积校验在 client 层按解码后字节算），
    对 http(s) 是原样 URL（可达性与公网地址校验在 client 层做，那里才有 DNS 上下文）。
    """
    text = value.strip() if isinstance(value, str) else ""
    if text == "":
        raise MediaError("INVALID_ARGS", "image 是空字符串")
    lowered = text.lower()
    if lowered.startswith("data:"):
        match = _DATA_URL_PATTERN.match(text)
        if match is None:
            raise MediaError(
                "INVALID_ARGS",
                "image 看起来是 data URL，但格式不对（需要 data:image/*;base64,...）",
            )
        mime = match.group(1).lower()
        if not mime.startswith("image/"):
            raise MediaError(
                "INVALID_ARGS",
                "data URL 的媒体类型必须是 image/*，给定 %s" % mime,
            )
        if match.group(2).strip() == "":
            raise MediaError("INVALID_ARGS", "data URL 里的 base64 是空的")
        return "data_url", text
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return "url", text
    if lowered.startswith("file://"):
        raise MediaError(
            "INVALID_ARGS",
            "本网关不接受 file:// 输入（会把服务器本地文件发给上游）。"
            "请把图片读成 data:image/*;base64,... 再传。",
        )
    raise MediaError(
        "INVALID_ARGS",
        "image 只接受 data:image/*;base64,... 或公网 http(s) URL。"
        "本地路径与 file:// 一律拒绝——那会让网关读取并把服务器上的文件发给上游。",
    )


def decoded_base64_length(text):
    """data URL 里 base64 段的**解码后**字节数，用于体积闸门。

    不能拿字符串长度比上限：base64 比原始字节长约 4/3，拿字符串长度会把一张
    合法的 6 MB 图误判成超限（并在报错里印出一个虚高的 MB 数）。
    """
    match = _DATA_URL_PATTERN.match(text or "")
    if match is None:
        return 0
    body = match.group(2).strip()
    padding = body.count("=")
    return max(0, len(body) * 3 // 4 - padding)


def is_data_url(text):
    """是不是内联 data URL（`data:<mime>;base64,…`）。"""
    if not isinstance(text, str):
        return False
    return text.strip().lower().startswith("data:")


def split_data_url(text):
    """把 data URL 拆成 `(bytes, 建议文件名)`。

    文件名要带扩展名：上传到 COS 的 object key 里没有扩展名时，上游判不出这是
    视频还是音频（实测上传器早先会把 `.mp4` 吃掉，只剩 `mp4`）。类型从 mime 推。
    """
    if not is_data_url(text):
        raise MediaError("INVALID_ARGS", "不是 data URL")
    header, _, payload = text.strip().partition(",")
    mime = header[5:].split(";")[0].strip().lower() if header.lower().startswith("data:") else ""
    try:
        data = base64.b64decode(payload, validate=False)
    except Exception as exc:                                  # noqa: BLE001
        raise MediaError("INVALID_ARGS", "data URL 解不出内容：%s" % exc)
    if not data:
        raise MediaError("INVALID_ARGS", "data URL 解不出内容")
    ext = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
           "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/wav": ".wav",
           "audio/x-wav": ".wav", "audio/aac": ".aac", "image/png": ".png",
           "image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime, "")
    return data, "upload" + (ext or ".bin")


def assert_data_url_size(text, limit, label="素材"):
    """内联素材的体积闸门：空、超限都当场拒，别让上游或内存去承担。

    参考视频/音频走 data URL 时用这个。上限比图片更该卡死——base64 会让体积
    涨三分之一，而一次请求里最多能带 3 段视频 + 3 段音频。
    """
    size = decoded_base64_length(text)
    if size <= 0:
        raise MediaError("INVALID_ARGS", "%s的 data URL 解不出内容" % label)
    if size > limit:
        raise MediaError(
            "INVALID_ARGS",
            "%s %.1f MB，超过 %.0f MB 上限；请压缩后再传，或改用公网 URL"
            % (label, size / 1048576.0, limit / 1048576.0))


def assert_input_image_bytes(byte_length, source="输入图"):
    """体积与空文件闸门：所有输入形态都从这里过，所以文案只有一份。"""
    if byte_length <= 0:
        raise MediaError("INVALID_ARGS", "%s是空文件" % source)
    if byte_length > MAX_INPUT_IMAGE_BYTES:
        raise MediaError(
            "INVALID_ARGS",
            "%s %.1f MB，超过 %.0f MB 上限"
            % (source, byte_length / 1048576.0, MAX_INPUT_IMAGE_BYTES / 1048576.0),
        )


# ---------------------------------------------------------------- 计费与对外形状

def estimate_cost(kind, seconds=None, price_per_second=None):
    """一次调用的积分估计，**只用于派发前的「付得起吗」**，不用于对外报价。

    价格由调用方传入（`price_per_second`）：这一层是纯逻辑，不持有账本、不读文件。
    服务层用**实测单价**填它（实扣 ÷ 秒数），拿不到就传 None。

    拿不到实测价时退回一刀切的 26 积分/秒——那是"不知道价格时"的保守上界，
    不是价格：它高估 minimax（14.29/秒）约 82%，余额紧时会把跑得起的调用挡掉。
    """
    if kind == "image":
        return COST_IMAGE_ESTIMATE
    if kind == "video":
        try:
            span = float(seconds or DEFAULT_VIDEO_SECONDS)
        except (TypeError, ValueError):
            span = float(DEFAULT_VIDEO_SECONDS)
        try:
            price = float(price_per_second) if price_per_second else 0.0
        except (TypeError, ValueError):
            price = 0.0
        if price > 0:
            return round(price * span, 2)
        return COST_VIDEO_PER_SECOND_CEILING * span
    return 0.0


#: 内部五态 → OpenAI Video API 的 status。多出来的两态都落到 failed，
#: 靠 `error_code_for()` 区分「能不能立刻重试」。
_STATUS_OUT = {
    "queued": "queued",
    # 还没提交到上游（在本地排队等并发位）。对外仍然是 queued——对客户端来说
    # "在排队"和"已受理"没有区别，都是"还没开始出片"。
    "waiting": "queued",
    # 正在提交的那一瞬。对外也是 queued，避免客户端看到一闪而过的未知状态。
    "submitting": "queued",
    "running": "in_progress",
    "completed": "completed",
    "rejected": "failed",
    "failed": "failed",
    "lost": "failed",
    # 取消：对外是 failed（OpenAI 没有 cancelled 这个 status），但 error.code 会
    # 单独给出 cancelled，让客户端知道"没扣费，可以直接重试"。
    "cancelled": "failed",
}


def status_out(status):
    """内部任务状态 → 对外 status。未知状态按 `in_progress`（没结论就别报失败）。"""
    return _STATUS_OUT.get(str(status or "").lower(), "in_progress")


def error_code_for(status):
    """内部失败态的对外 error.code；不是失败态就返回 None。

    `invalid_request` 是「提交阶段被 4xx 退回、没扣积分」——**客户端可以立刻重试**；
    `upstream_error` / `task_lost` 可能已经扣过费，重试要谨慎。这个区分必须在
    对外契约里保留，否则客户端只剩「failed」一个词可用。
    """
    status = str(status or "").lower()
    if status == "rejected":
        return "invalid_request"
    if status == "lost":
        return "task_lost"
    if status == "cancelled":
        return "cancelled"
    if status == "failed":
        return "upstream_error"
    return None


def public_task(task, content_url=None):
    """内部任务行 → OpenAI Video API 的形状。

    `content_url` 由服务层传入**已签名**的下载地址：签名要留在服务端（进程内 secret），
    前端与客户端都不该参与计算。不传就只回 `videos` 原始字段（轮询期间还没有产物）。
    """
    task = task or {}
    out = {
        "id": task.get("id"),
        "object": "video",
        "status": status_out(task.get("status")),
        "model": task.get("model"),
        "created_at": task.get("created_at"),
    }
    if task.get("completed_at") is not None:
        out["completed_at"] = task.get("completed_at")
    if task.get("seconds") is not None:
        out["seconds"] = str(task.get("seconds"))
    if task.get("resolution") is not None:
        out["resolution"] = task.get("resolution")
    if task.get("credit") is not None:
        out["credit"] = task.get("credit")
    code = error_code_for(task.get("status"))
    if code is not None:
        out["error"] = {"code": code, "message": task.get("error") or ""}
    if content_url is not None:
        out["url"] = content_url
    elif task.get("videos"):
        out["videos"] = list(task.get("videos"))
    return out
