# 媒体能力接入方案：生图 / 生视频（feat）

> 状态：**方案稿，未实现、未提交**。写成 `docs/MEDIA-PLAN.md` 是为了将来直接当 PR 描述的基础。
> 基线：main @ `a797c1d`（v1.10.0）；本机实例在 8788 运行中（PID 21336），3 个 cn 账号。
> 参考实现：`dsh-connect-workbuddy`（DSH 插件，MIT）的 `workbuddy_draw_image` / `workbuddy_draw_video`。

## 0. 一句话

在现有 chat 代理旁边，**并列**加一层媒体代理：目录复用已经拉到的 `/v3/config`（不发新请求），
生图一条同步请求、生视频「提交→轮询→落盘」三段异步任务，全部走既有账号池、
既有 usage 记账、既有冷却/熔断；对外是 OpenAI 兼容的 `/v1/images/*` 与 `/v1/videos/*`，
**默认关闭**，没开时路由直接 404。

## 1. 本机取证（2026-11-01 实地读代码 + 留档目录）

| 事实 | 值 | 用途 |
| --- | --- | --- |
| 媒体目录来源 | `catalog/product-config-cn.json` 的 `data.models[].tags` | **已在本机**，60 条模型，4 条带媒体标签 |
| 生图模型 | `hunyuan-image-alpha`(文生图)、`hunyuan-image-alpha-edit`(改图) | 每个 tag 组 1 个，无选择困难 |
| 视频模型 | `kling-video-v3`、`minimax-video-h3`（各带文生+图生） | 需要「钉模型」或显式指定 |
| kling 约束 | seconds 3–15，resolutions `720P/1080P/4K`，aspect `16:9/9:16/1:1`，audioMode `switchable` | 比 dsh 文档更宽（它只实测到 720p） |
| minimax 约束 | seconds 4–15，resolutions `768P/2K`，aspect 含 `adaptive`/`21:9`/`4:3`，audioMode **always-on** | `enable_audio:false` 可能被忽略 |
| prompt 上限 | kling 2500 / minimax **2000** | 逐模型取值，不能写死 2500 |
| 首尾帧 | 两者 `supportsFirstLastFrame: true`，`lastFrameRequiresFirstFrame: true` | 图生视频可带尾帧，但必须同时给首帧 |
| 多参考素材 | minimax `maxReferenceImages: 9`、`maxReferenceVideos: 3` | dsh 没做的能力，可留 P3 |
| 上游 base | cn = `copilot.tencent.com`（= 现有 `account.chat_base_url()`） | 与 chat 同主机，别再写第二份 |
| 现有身份头 | `Account.headers(purpose="billing")` 已产出一套与 dsh `imageHeaders()` 高度同形的头 | 少写一套头 |

结论：**目录层零成本，出站层可复用，只剩协议层 + 任务层是新的。**

## 2. 目标 API（对外形状）

| 方法 | 路径 | 语义 | 鉴权 |
| --- | --- | --- | --- |
| GET | `/v1/media/models?realm=cn` | 媒体模型清单，**原样返回 `videoGeneration`** 与 `tags` | API key |
| POST | `/v1/images/generations` | `{model,prompt,size,response_format?}` → `{data:[{url|b64_json}], credit, model}` | API key |
| POST | `/v1/images/edits` | 同上 + `image`（data URL / http(s) / 本地路径 / 裸 base64），自动选 `image-to-image` 模型 | API key |
| POST | `/v1/videos/generations` | 提交任务 → `{id,status:"running",model,seconds,resolution}`；`?wait=1` 阻塞到完成 | API key |
| GET | `/v1/videos/{id}` | `{id,status,videos:[url],credit,error}` | API key |
| GET | `/v1/videos/{id}/content` | 下载本地 mp4（`Content-Disposition`），支持 `?i=0` | 签名 token（见 §6.3） |
| GET | `/v1/media/tasks?limit=50` | 面板用：最近任务列表 | 面板会话 |

设计取舍（对着 dsh 抄 vs 不抄）：

| 项 | dsh 做法 | 本方案 | 为什么 |
| --- | --- | --- | --- |
| 模式选择 | 由「有没有 `image`」决定，无 `mode` 参数 | **照抄** | 避免 `mode=edit` 却没图的矛盾请求 |
| 模型缺失 | 直接报错并列出可用项 | **照抄** | 不发上去换一个「route config not found」 |
| 目录额外请求 | 再读一次 `/v3/config`（+5 分钟缓存） | **不读**：在 `fetch_remote_product_config` 里把原始 payload 存进内存 `_catalog_raw[realm]`（TTL 300s），留档文件只作冷启动回落 | 本仓库已经有这条管线与节流，dsh 那次额外请求在这里是浪费 |
| 视频参数校验 | 拿不到 `videoGeneration` 就透传，不盲拒 | **照抄** | 盲拒会让自定义值时灵时不灵 |
| 学区间 | 从 400 文案里读 `supported range` | **照抄** | 幂等且零成本 |
| 提交阶段 4xx | 单列 `REJECTED`，不进冷却 | **照抄** | 「参数写错却要白等一分钟」是真踩过的坑 |
| 冷却判据 | 只看「请求是否真的发出」 | **照抄** | 读目录/校验失败不该冷却 |
| 注册门 | 逐区 opt-in，不勾根本不存在 | **照抄**（默认关，关=404） | 唯一一项「模型自己决定花钱」的能力 |
| 头形状 | 自带 `imageHeaders()`（无 Origin/Referer） | 复用 `purpose="media"`，**首期用桌面身份 + `X-Product: SaaS`**，实测不通再切 CLI 形状（一个开关） | 本仓库装的是 desktop 账号，dsh 用的是 CLI 身份；这条必须实测，不能假设 |
| 并发 | 独占执行（不声明并发安全） | 媒体独立并发槽（默认 4，独立于 CHAT_SLOTS） | 视频一次几分钟，不能占 chat 的 32 个槽 |
| 产物 | 图片回 URL；视频落 `os.tmpdir()` | 图片回 URL + 可选落盘；视频落 `media/out/`（跟随 `__file__` 的绝对路径） | 网关是长跑进程，tmp 会被清；也避免模块级用 `__file__` 相对路径（同 CATALOG_CACHE_DIR 的理由） |

## 3. 代码结构

```
wb_media.py          纯逻辑：目录投影、常量、body 构造、响应解析、参数校验、错误分类（零 I/O）
wb_media_client.py   出站：generate_image / edit_image / submit_video / poll_video_task / download_video
wb_media_tasks.py    任务表 + worker 线程 + 产物目录 + 签名下载 token
wb_proxy.py          接入：路由、开关、usage 记账、面板状态
```

### 3.1 `wb_media.py`（约 400 行，纯 stdlib）

```python
TAG_TEXT_TO_IMAGE  = "text-to-image"
TAG_IMAGE_TO_IMAGE = "image-to-image"
TAG_TEXT_TO_VIDEO  = "text-to-video"
TAG_IMAGE_TO_VIDEO = "image-to-video"
MEDIA_TAGS = (上述四个)
IMAGE_MAX_PIXELS = 4_194_304     # 419 万，dsh 逐档实测边界
MAX_INPUT_IMAGE_BYTES = 8 * 1024 * 1024

def media_models(payload, tag=None)          # data.models 里按 tag 过滤、按 id 去重，videoGeneration 原样保留
def pick_model(models, requested, tag)       # 点名的必须在这一组里，否则抛 MediaError('NO_MEDIA_MODEL')
def build_image_body(model, prompt, size, n=1)   # hunyuan-* 加 n；gemini-* 不加；其它加 response_format=b64_json
def build_edit_body(...)                     # 上面 + image:[dataurl,...]
def build_video_body(...)                    # watermark:false 写死（网关口径，不接受调用方覆盖）；图生视频不带 aspect_ratio
def resolve_video_params(request, config)    # → (seconds, resolution, aspect_ratio, warnings) 或抛 INVALID_ARGS
def parse_supported_seconds_range(text)      # "supported range: 4-15" → (4,15)，读不出就 None
def parse_image_response(payload)            # 信封 code!=0 / data.data[0].url|b64_json / usage.credit
def parse_video_submission(payload)          # data.id
def parse_video_task(payload)                # status / videos[] / credit
def describe_media_failure(status, text, phase)  # → MediaError(AUTH|RATE_LIMIT|NO_ROUTE|REJECTED|UPSTREAM|EMPTY_RESULT)
def check_size(size)                         # 宽高解析 + 像素上限，非法 → INVALID_ARGS
class MediaError(Exception): code
```

规则与 dsh 逐条对齐，另加两条本土化：
- `check_size` 本地拦 419 万像素（dsh 只在工具描述里写，靠模型自觉；网关面对的是程序，必须硬拦）
- `maxPromptLength` 从该模型的 `videoGeneration` 取，不再写死 2500

### 3.2 `wb_media_client.py`（约 350 行）

```python
def generate_image(account, body, timeout=180, signal=None)   # POST {chat_base}/v2/images/generations
def edit_image(account, body, ...)                            # POST /v2/images/edits
def submit_video(account, body, ...)                          # POST /v2/videos/generations → taskId
def poll_video_task(account, task_id, interval=5, timeout=600)  # 循环 POST /v2/videos/tasks
def download_video(url, dest, timeout=120)                    # GET COS 签名 URL → 本地 .mp4
```

- 出站统一走 `wb_accounts.urlopen(req, timeout=..., proxy=account.proxy)`（保留代理/egress 一致性）
- 错误分类：`MediaError` 携带 `code` 与 `status`，供上层决定换号/冷却，**不抛裸异常**

### 3.3 `Account.headers(purpose="media")`（wb_accounts.py 内新增一个分支）

照抄 `purpose="billing"` 那一支（`User-Agent: WorkBuddy/<ver>`、`Origin`/`Referer`、`X-Requested-With`、
`X-CodeBuddy-Request: 1`、`Accept-Language`、`X-Enterprise-Id`/`X-No-Enterprise-Id`、
cn 加 `X-Product: SaaS`、cn 加 `X-Device-Token`），**另加会话三件套**（`X-Conversation-Id` /
`X-Conversation-Request-Id` / `X-Conversation-Message-Id`，每请求现生成 UUIDv7）——
dsh 的生图头就是这么组的（它复用 CLI 的会话 UUID 逻辑）。

新增设置 `media_identity`：`"desktop"`（默认）| `"cli"`。P0 若桌面形状不通，改这一项即切，
不动代码路径。

### 3.4 `wb_media_tasks.py`（约 300 行）

```python
class MediaTaskStore:
    def submit(self, kind, model, params, account_uid) -> task_id
    def get(self, task_id) -> row | None
    def list(self, limit=50) -> [row]
    def _worker(self)          # 单后台线程：submit → poll → download，逐 5s 轮询
    def sign(self, task_id, index) -> token      # 下载令牌，HMAC(进程内 secret, task_id|index|exp)
```

- 内存字典 + 可选落盘 `media/tasks.json`（重启后已完成的仍可取，进行中的标 `lost`）
- **单 worker 线程**足够：视频本来就慢，串行更省上游风控暴露面；并发需求由「同账号不能同时跑两条」约束
- 任务态：`queued → running → completed | failed | rejected | lost`；`error` 字段存上游原文
- 产物：`media/out/<task_id>.mp4`，7 天机会式清理（写新任务后顺手清）

### 3.5 `wb_proxy.py` 接入点（5 处，都不改 chat 路径）

1. `do_GET`：`/v1/media/models`、`/v1/videos/{id}`、`/v1/videos/{id}/content`、`/v1/media/tasks`
2. `do_POST`：`/v1/images/generations`、`/v1/images/edits`、`/v1/videos/generations`；
   并把它们加进 §「白名单那条 `path not in (...)`」——**这是唯一的必改硬点**，不加就 404
3. `fetch_remote_product_config` 成功时 `_catalog_raw[realm] = payload`（1 行）
4. `record_usage` 的 row 增加 `kind`（`chat|image|video`，默认 `chat`，旧行无此字段按 chat）
5. 面板 `/settings` 暴露开关（§7），P3 再做 UI

## 4. 请求流（生图）

```
POST /v1/images/generations
  → _authorized()（同一个门）
  → media_enabled?  否 → 404
  → 拿目录（_catalog_raw[realm]，冷则 fetch，失败 → 503 NO_ROUTE）
  → pick_model(tag=text-to-image)（显式 model 不在组里 → 400）
  → check_size()（>419 万像素 → 400，一个上游请求都不发）
  → 选账号：POOL.pick(realm, exclude=已试过的, model=<media model>)（复用权重/临期/轮转）
  → generate_image() → 200? → parse → record_usage(credit) → acc.note_success()
                    → 401/403/429/402/5xx → acc.note_error(cooldown) → 换号重试（≤3）
                    → 400 → REJECTED：不换号、不冷却、直接回客户端
  → 响应 {data:[{url}], credit, model, mode}
```

视频同一流程，只是 `submit` 后立刻返回 task id，轮询与下载在 worker 里。

## 5. 失败与换号口径

| 上游 | code | 换号 | 冷却 | 客户端看到 |
| --- | --- | --- | --- | --- |
| 401/403 | `AUTH` | 是 | 60s（403 走账号级限制判定） | 502 + 上游原文 |
| 429 | `RATE_LIMIT` | 是 | 按现有 429 阶梯 | 429 + `Retry-After` |
| 402 | `NO_CREDIT` | 是 | 走 `revive_balance_cooldown` 同款 | 402 |
| 5xx / 网络 | `UPSTREAM` | 是 | 60s | 502 |
| 400（提交阶段） | `REJECTED` | **否** | **否** | 400 + 上游原文（含 `supported range`） |
| 本地校验失败 | `INVALID_ARGS` | 否 | 否 | 400 |

403 必须过 `restriction_is_account_level(detail)` —— 账号级风控（11140）沿用现有红线：
不跨区重打、整池暂停。**这条是硬约束，媒体不得例外**。

## 6. 产物

### 6.1 图片
- 默认回 `url`（COS 签名 URL，24h 有效）。响应里**必须**带 `expires_hint`，否则客户端会存死链
- `response_format: "b64_json"` → 下载后转 base64 回（OpenAI 兼容形状），或 `response_format: "path"` 落 `media/out/*.png` 回 `file://`
- 不缩放、不转码

### 6.2 视频
- 必须落盘（二进制不能进 JSON），回 `GET /v1/videos/{id}/content?i=0` 的相对 URL
- 顺带把 `moov` 里的真实时长/分辨率解析出来放进任务行（dsh 就是这么核的：请求 5s 实得 5.09–5.17s）

### 6.3 下载令牌
`/v1/videos/{id}/content` 不带 API key 时要求 `?t=<hmac>`：`HMAC_SHA256(进程内 secret, f"{task_id}|{index}|{exp}")`，
有效期 `exp` 写进令牌（默认 1h）。理由：客户端常把 URL 直接贴进浏览器/播放器，那里没有 Authorization 头。

## 7. 开关（默认关，行为不变）

`wb_settings` 新增，全部有默认值、老安装不受影响：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `media_enabled` | `false` | **总闸**。false → 全部媒体路由 404 |
| `media_realm` | `"cn"` | 媒体只走 cn（本机 3 个账号都是 cn；intl 目录里有没有模型待 P0 顺带看一眼） |
| `media_identity` | `"desktop"` | `desktop` / `cli`，见 §3.3 |
| `media_image_max_per_minute` | `6` | 每 key 每分钟生图次数（**花钱护栏**） |
| `media_video_max_concurrent` | `1` | 同账号同时进行的视频任务上限 |
| `media_video_max_seconds` | `15` | 本地上限，取目录 `maxSeconds` 与它的小值 |

环境变量镜像：`WB_MEDIA_ENABLED=1` 可临时覆盖（CI/冒烟用），优先级低于面板设置。

## 8. 阶段与 PR 拆分

| 阶段 | 内容 | 交付 | 风险 |
| --- | --- | --- | --- |
| **P0** 取证 | `scripts/probe_media.py`：真打 1 次 `1024x1024`（花 ~5.71 积分）+ 1 次 `seconds=1` 的错误视频提交（不花钱）+ 读 intl 目录 | 一份实测记录（头是否 200、credit 数值、错误信封） | 5.71 积分；**决定后面所有形状** |
| **P1** PR | `wb_media.py` + `wb_media_client.py` + 生图两条路由 + 目录内存缓存 + usage 记账 + 开关 + 单测 | feat PR，可独立回滚 | 低（同步请求） |
| **P2** PR | `wb_media_tasks.py` + 视频三条路由 + 学区间 + 下载令牌 + 单测 | feat PR | 中（后台线程/状态机） |
| **P3** 可选 | 面板「能力」栏（开关 + 目录清单 + 任务列表）、multipart edits、多参考图、key 级每日配额 | 独立 PR | 低 |

P1/P2 分开是为了满足「一个 PR 一件事」：生图与生视频的回滚面完全不同。

## 9. 测试

- `tests/_test_media_logic.py`（新，约 40 条）：目录投影（**夹具直接用 `catalog/product-config-cn.json` 的真实形状**）、三分支 body、
  `check_size` 边界（419 万过 / 419 万+1 拒）、`resolve_video_params`（越界抛错 / 拿不到 config 时透传 / 大小写不敏感命中发目录拼写）、
  `parse_supported_seconds_range`（`4-15` / `to` / `~` / 倒序 / 0）、`describe_media_failure` 的 REJECTED 单列、`maxPromptLength` 逐模型
- `tests/_test_media_routes.py`（新，约 20 条）：`media_enabled=false → 404`、无 key → 401、model 不在组里 → 400 且**上游零请求**、
  上游 400 → 客户端 400 且不计冷却、上游 401 → 换号、429 → Retry-After、usage 行带 `kind` 与 `credit`
- 既有 `python tests/run_all.py` 必须全绿；套件数从 244 改成 246（README 那行有测试钉住）
- **冒烟（提交前硬性，按 CONTRIBUTING §1）**：8788 被占 → 用 8799 起实例；面板可开；`media_enabled=true` 后真账号出一张 1024×1024；
  视频用 `seconds=1` 走到 `REJECTED`（零成本链路）验证错误透传

## 10. 风险与未知（不猜，列出来）

1. **桌面身份是否被媒体端点接受** —— 未知。dsh 用的是 CLI 身份实测过；本仓库账号 `product=workbuddy`。
   P0 第一件事就是验它。不通 → `media_identity=cli`（需确认 CLI 头在 cn 端点也 200，`wb_identity` 已有该身份）。
2. **4K / 2K 目录里有，实际可能降级** —— dsh 实测只出 1280×720 / 1344×768。目录写 4K 不等于给你 4K。
   文档与面板都得写「以实际产物为准」，别承诺 1080P。
3. **minimax `audioMode: always-on`** —— `enable_audio:false` 可能被忽略，别在 UI 上承诺「无音轨」。
4. **风控暴露面增加** —— 媒体请求是新的一条指纹面。缓解：默认关、单 worker、`media_image_max_per_minute`、不批量、不进 `/v1/models`。
5. **单价极陡** —— 生图 5.71/张、视频 57–129/条。一个 key 循环几十次就是几百积分。
   本期只做「每分钟次数」这种最小护栏，**key 级每日配额必须排进 P3 且上线前打开**。
6. **intl 未取证** —— 本机没有 intl 账号，`catalog/product-config-intl.json` 不存在。dsh 说 intl 有
   `hy-image-3.5` / `kling-video-v3`，但那是别人的账号。方案里 `media_realm` 默认 cn，intl 留待有号再验。
7. **重启丢任务** —— 内存任务表在重启后把进行中的任务标 `lost`（上游可能已经扣费）。
   面板要能把 `lost` 显出来，否则「钱花了没产物」查不到原因。

## 11. 需要拍板的三件事

1. **默认值**：`media_enabled` 默认关我坚持（花钱能力不能升级附赠）。你要是想一上线就能用，
   改成 `WB_MEDIA_ENABLED=1` 环境变量，但面板默认关。
2. **P0 要不要现在就花那 5.71 积分**：不跑就只能按 dsh 的形状写，头一错整条链要返工。
3. **视频 API 形状**：现在是异步任务 + 轮询（贴合上游），代价是客户端要会轮询。
   要不要同时给 `?wait=1` 阻塞式（最长 600s 占一个线程）——我倾向给，但默认关。
