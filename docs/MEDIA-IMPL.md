# 媒体能力接入 · 实现级细节（MEDIA-IMPL）

> 配套 `docs/MEDIA-PLAN.md`（决策与取舍）。这份只讲**怎么改代码**：改哪个函数、插在哪一行、
> 每个字段为什么这么定。基线 main @ `a797c1d`（v1.10.0）。

## 1. 改动总览

| 文件 | 改动性质 | 预计行数 | 风险 |
| --- | --- | --- | --- |
| `wb_media.py` | 新增，纯逻辑 | ~420 | 无（零 I/O，可单测） |
| `wb_media_client.py` | 新增，出站 | ~330 | 中（协议形状） |
| `wb_media_tasks.py` | 新增，任务表 | ~300 | 中（线程/状态机） |
| `wb_accounts.py` | 改 1 处：`Account.headers()` 加 `purpose="media"` 分支 | +55 | **低**（新分支，不动 chat/billing 两支） |
| `wb_proxy.py` | 改 5 处 | +180 | 中（路由分发是热路径） |
| `wb_settings.py` | 加 1 个 `media_config()` + 默认值 | +40 | 无 |
| `catalog/README.txt` | 改措辞（媒体会读它作冷启动回落） | +3 | 无 |
| `README.md` | 套件数 244 → 246 | 1 | 有测试钉住 |
| `tests/_test_media_logic.py` | 新增 | ~40 用例 | — |
| `tests/_test_media_routes.py` | 新增 | ~20 用例 | — |
| `scripts/probe_media.py` | 新增（P0 取证，可不进 PR） | ~90 | — |

`wb_proxy.py` 的 5 处：

1. 模块级：`_catalog_raw` 缓存字典（1 行）+ `MEDIA_POST_PATHS` 常量
2. `fetch_remote_product_config()` 成功后 `_catalog_raw[realm] = payload`（1 行）
3. `do_GET()`：4 条只读路由
4. `do_POST()`：白名单放行 3 条 + 提前分流
5. `record_usage()`：`kind` 字段 + `cost_policy` 保护

## 2. 改动点逐处说明

### 2.1 `wb_proxy.py` · 目录缓存（2 行）

```python
# 模块级，紧邻 _models_cache
#: /v3/config 的原始 payload（含 media tags），媒体目录从这里投影。
#: 与 _models_cache 分开是因为后者已被投影成聊天目录、媒体条目在投影中丢失。
_catalog_raw = {}
```

`fetch_remote_product_config()` 里，`persist_remote_catalog(realm, payload)` 那行旁边：

```python
            persist_remote_catalog(realm, payload)
            _catalog_raw[realm] = payload      # ← 新增
            return parsed
```

**为什么不是读留档文件**：`catalog/README.txt` 立着一条不变量「Nothing reads it back，删掉永远安全」。
让媒体去读它，这条不变量就破了，以后谁清理磁盘都可能踩雷。内存为主、留档只作冷启动回落（见 §3），
并把 README 的措辞改成「媒体目录冷启动时会回落读取」。一条额外请求都不发，比 dsh 少一次往返。

### 2.2 `wb_proxy.py` · POST 白名单与分流

现状（`do_POST` 尾部）：

```python
        if not is_account_route and not is_messages_route and path not in (
                "/v1/chat/completions", "/chat/completions",
                "/v1/completions", "/completions",
                "/v1/responses", "/responses",
                "/v1/responses/compact", "/responses/compact"):
            return self._error(404, "not found", "invalid_request_error")
```

改成：

```python
        is_media_route = path in MEDIA_POST_PATHS
        if not is_account_route and not is_messages_route and not is_media_route and path not in (
                ...原样...):
            return self._error(404, "not found", "invalid_request_error")
```

体读取要**给媒体单独的上限**——这是必须的一处，不能省：

```python
        if is_media_route:
            # 改图的输入图是 data URL，8 MB 原图 ≈ 10.7 MB base64，再包一层 JSON。
            # 通用上限（默认 16 MB，还可能被 max_payload_mb 调小）会把合法请求判 413。
            payload = self._payload_or_error_for_media()
        else:
            payload = self._payload_or_error(allow_list=(path == "/accounts/import"))
```

```python
MEDIA_MAX_PAYLOAD_BYTES = 24 * 1024 * 1024

    def _payload_or_error_for_media(self):
        """媒体路由的体上限：不小于 24 MB，也不小于用户配置的通用上限。"""
        configured = wb_settings.max_payload_mb(ACCOUNTS_DIR)
        generic = configured * 1024 * 1024 if configured is not None else MAX_PAYLOAD_BYTES
        try:
            return self._read_payload(max_bytes=max(generic, MEDIA_MAX_PAYLOAD_BYTES),
                                      allow_list=False)
        except BodyTooLarge as exc:
            self.close_connection = True
            self._body_consumed = True
            return self._error(413, "payload too large (%d bytes)" % exc.length,
                               "invalid_request_error") or None
        except BadJSON:
            return self._error(400, "invalid JSON body", "invalid_request_error") or None
```

分流位置（**在并发槽之前，chat 一行不动**）：

```python
        _apply_model_aliases(payload, aliases)
        _apply_background_model_alias(payload)
        if is_media_route:
            return self._handle_media_post(path, payload)      # ← 提前返回
        key_ok, cur_key_conc, max_key_conc = _acquire_key_slot(self.key_entry)
        ...（后面全不动）
```

### 2.3 `wb_proxy.py` · GET 4 条

插在 `/v1/models` 附近（**在 `_is_panel_route` 判断之后**，媒体 GET 走 API key 不走面板门）：

```python
        if path in ("/v1/media/models", "/media/models"):
            return self._media_models(query)
        if path == "/v1/media/tasks":
            return self._media_tasks(query)
        if path.startswith("/v1/videos/"):
            return self._media_video_get(path, query)     # /v1/videos/{id} 与 /v1/videos/{id}/content
```

`/v1/videos/{id}/content` 是**唯一允许无 key** 的媒体路由（靠 `?t=` 签名令牌），因为客户端常把 URL 直接塞进
浏览器或播放器，那里没有 Authorization 头。

### 2.4 `wb_accounts.py` · `Account.headers(purpose="media")`

在现有 `if purpose != "chat":` 里加一支，**不动 billing 那一支**：

```python
        if purpose == "media":
            # 媒体端点的头。形状取自 dsh 实测能拿到 200 的那套（Authorization / Accept /
            # Content-Type / User-Agent / X-Requested-With / X-Product: SaaS /
            # X-User-Id / X-Enterprise-Id / X-Domain + 会话三件套），但 User-Agent 用本账号
            # 自己的桌面 UA —— 本仓库的账号是 product=workbuddy，不是 CLI。
            # 与 billing 的差别只有三条：不带 Accept-Language、不带 X-CodeBuddy-Request、
            # 补会话三件套（官方 CLI 的 ImageService 也是每请求现生成一组）。
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "User-Agent": cfg["chat_ua"] if media_identity_is_desktop() else cfg["billing_ua"],
                "X-Requested-With": "XMLHttpRequest",
                "Authorization": "Bearer " + self.access_token,
                "X-User-Id": self.uid,
                "X-Domain": self.domain or cfg["domain"],
            }
            if self.enterprise_id:
                headers["X-Enterprise-Id"] = self.enterprise_id
                headers["X-Tenant-Id"] = self.enterprise_id
            else:
                headers["X-No-Enterprise-Id"] = "1"
            if self.realm == "cn":
                headers["X-Product"] = "SaaS"      # dsh 实测 cn 需要
            if self.is_api_key:
                headers["X-API-Key"] = self.access_token
            conv = uuid7_header_triplet()          # 每请求现生成，三个 id 同一批
            headers.update(conv)
            return headers
```

`media_identity` 开关（`desktop` / `cli`）只影响 `User-Agent` 与是否补 `Origin/Referer`，
**P0 取证的结论直接落成这个开关的默认值**，不需要改调用路径。

> 为什么不一上来就用 CLI 形状：dsh 的账号是 CLI 通道，本仓库的 3 个账号是桌面通道，
> 拿别人的实测结论套过来是猜测。P0 一次请求就能定。

### 2.5 `record_usage()` · kind 与价格表隔离

```python
def record_usage(model, usage, ..., realm=None, kind="chat"):
    ...
    row["kind"] = kind
    # 媒体模型不在 chat 价目表里，current_policy_id() 会给一个 chat 的默认策略，
    # 那会让「按价目表统计」把媒体请求算进某个 chat 策略的样本里。
    row["cost_policy"] = ("media" if kind != "chat" else
                          "codearts:unpriced" if realm == "codearts"
                          else wb_pricing.current_policy_id(model))
```

`kind` 默认 `"chat"`，旧调用点一行不改，旧行没有这个字段按 chat 处理（面板读 `row.get("kind", "chat")`）。

计费本身不用写新代码：`fields["credit"]` 一有值就自动走 `acc.consume_credit()`。

## 3. `wb_media.py` 契约（纯逻辑）

```python
class MediaError(Exception):
    code: str      # INVALID_ARGS | NO_MEDIA_MODEL | NO_ROUTE | AUTH | RATE_LIMIT
                   # | REJECTED | UPSTREAM | EMPTY_RESULT | NO_CREDIT
    status: int    # 上游 HTTP，本地错误为 0
    detail: str    # 上游原文，原样透传给客户端（含 supported range）

def media_models(payload, tag=None) -> [dict]      # 按 tag 过滤、按 id 去重，videoGeneration 原样保留
def pick_model(models, requested, tag) -> dict     # 点名不在组内 → MediaError(NO_MEDIA_MODEL)，附可用清单
def check_size(size) -> str                        # "宽x高"，缺省 1024x1024；>419 万像素 → INVALID_ARGS
def build_image_body(model_id, prompt, size, response_format=None) -> dict
def build_edit_body(model_id, prompt, size, images) -> dict
def build_video_body(task_params) -> dict
def resolve_video_params(payload, video_generation) -> dict
def parse_supported_seconds_range(text) -> (min,max) | None
def parse_image_response(payload) -> {"url"|"b64", "credit", "created"}
def parse_video_submission(payload) -> {"taskId"}
def parse_video_task(payload) -> {"status", "videos", "credit"}
def describe_media_failure(status, text, phase) -> MediaError
def estimate_cost(kind, model_id, seconds) -> float
```

### 3.1 `build_image_body` —— 三分支（照抄官方 CLI `buildBaseRequestBody`）

```python
def build_image_body(model_id, prompt, size, response_format=None):
    body = {"model": model_id, "prompt": prompt, "size": size}
    if model_id.startswith("hunyuan-"):
        body["n"] = 1                       # 只加 n，不加 response_format
        return body
    if model_id.startswith("gemini-"):
        return body                         # 什么都不加
    body["response_format"] = "b64_json"    # 其它模型（含 intl 的 hy-image-3.5）
    return body
```

`response_format` 是**客户端要什么**（`url` 还是 `b64_json`），与上游三分支是两件事：
上游分支决定**发什么**，客户端诉求决定**回什么**（b64 时我们下载后转 base64，URL 时原样给）。

### 3.2 `check_size` —— dsh 实测边界落成本地硬闸

```python
IMAGE_MAX_PIXELS = 4_194_304      # 2048x2048 = 4.19 MP 通过，2560x2560 = 6.55 MP 被拒

def check_size(size):
    text = (size or "1024x1024").strip().lower()
    m = re.fullmatch(r"(\d{2,5})x(\d{2,5})", text)
    if not m:
        raise MediaError("INVALID_ARGS", "size 要写成「宽x高」，如 1024x1024、1536x1024")
    w, h = int(m.group(1)), int(m.group(2))
    if w * h > IMAGE_MAX_PIXELS:
        raise MediaError("INVALID_ARGS",
            "size %dx%d = %.2f MP 超过上限 %.2f MP（dsh 逐档实测：2560x1600 通过、"
            "2560x2560 被直接拒绝且不会降级）" % (w, h, w*h/1e6, IMAGE_MAX_PIXELS/1e6))
    return "%dx%d" % (w, h)
```

dsh 是把这条写在**工具描述里**靠模型自觉（它是 agent 插件，模型会读描述），网关面对的是程序，
必须硬拦——否则一次 4K 请求换来一个上游 400，而客户端只会觉得"网关坏了"。

### 3.3 `resolve_video_params` —— 「知道才拦，不知道就透传」

```python
def resolve_video_params(payload, cfg):
    cfg = cfg or {}
    prompt = payload.get("prompt") or ""
    limit = cfg.get("maxPromptLength") or 2500        # kling 2500 / minimax 2000
    if len(prompt) > limit:
        raise MediaError("INVALID_ARGS", "prompt 不能超过 %d 字符（模型 %s 的限制）"
                                       % (limit, payload.get("model")))
    is_i2v = bool(payload.get("image"))
    warnings = []

    # 时长：只有目录真的给了区间才拦
    lo, hi = cfg.get("minSeconds"), cfg.get("maxSeconds")
    seconds = payload.get("seconds")
    if seconds is None:
        seconds = cfg.get("defaultSeconds") or 5
    elif lo is not None and hi is not None and not (lo <= seconds <= hi):
        raise MediaError("INVALID_ARGS", "时长必须在 %s 到 %s 秒之间（给定 %s）" % (lo, hi, seconds))

    # 分辨率：有清单才校验，命中发目录的拼写（上游认它自己那份，大小写不敏感匹配）
    resolutions = cfg.get("resolutions") or []
    resolution = cfg.get("defaultResolution")
    asked = payload.get("resolution")
    if asked:
        if not resolutions:
            resolution = asked           # 没清单 = 不知道，原样透传交给上游
        else:
            hit = asked if asked in resolutions else next(
                (r for r in resolutions if r.lower() == asked.lower()), None)
            if hit is None:
                warnings.append("resolution %s 不被该模型支持；改用 %s"
                                % (asked, resolution or "上游默认"))
            else:
                resolution = hit

    # 比例：仅文生视频；图生视频由输入图决定，传了什么都不发
    aspect = None
    if not is_i2v:
        allowed = cfg.get("aspectRatios") or ["16:9", "9:16", "1:1"]
        aspect = payload.get("aspect_ratio") or cfg.get("defaultAspectRatio") or "16:9"
        if aspect not in allowed:
            raise MediaError("INVALID_ARGS", "aspect_ratio 只能是 %s 之一（给定 %s）"
                                            % ("/".join(allowed), aspect))
    return {"seconds": seconds, "resolution": resolution,
            "aspect_ratio": aspect, "enable_audio": payload.get("enable_audio"), "warnings": warnings}
```

`minimax` 的 `audioMode: always-on` 要单独处理：`enable_audio` 传 `false` 时**照发**，但响应里带
`warnings: ["该模型音频常开，enable_audio=false 可能被忽略"]`——不猜上游行为，也不假装做到了。

### 3.4 `describe_media_failure` —— 提交阶段 4xx 单列

```python
def describe_media_failure(status, text, phase):
    detail = _envelope_msg(text) or text[:240]
    if status in (401, 403):
        return MediaError("AUTH", "凭据被拒（HTTP %d）：%s" % (status, detail), status=status)
    if status == 402:
        return MediaError("NO_CREDIT", "积分不足：%s" % detail, status=status)
    if status == 429:
        return MediaError("RATE_LIMIT", "上游限流：%s" % detail, status=status)
    if status == 404:
        return MediaError("NO_ROUTE", "该区没有这条媒体路由：%s" % detail, status=status)
    if phase == "submit" and 400 <= status < 500:
        # 建任务之前被退回：任务没建、积分没花 → 不进冷却、可以立刻重试
        return MediaError("REJECTED", "上游拒绝了参数（未扣积分）：%s" % detail, status=status)
    return MediaError("UPSTREAM", detail, status=status)
```

`REJECTED` 是本方案唯一新增的错误码，对应 dsh 踩过的坑：
`seconds=1` 被 400 退回后还白等 60 秒冷却，用户原话「上游返回这种错误，也要等半天」。

## 4. `wb_media_client.py` 契约

```python
def generate_image(account, body, timeout=180)     # POST {chat_base}/v2/images/generations
def edit_image(account, body, timeout=180)         # POST .../v2/images/edits
def submit_video(account, body, timeout=60)        # POST .../v2/videos/generations → taskId
def poll_video_task(account, task_id, interval=5, timeout=600)   # 循环 POST .../v2/videos/tasks
def download_video(url, dest, timeout=120)         # GET COS 签名 URL → 本地 .mp4
```

统一出口（保留每账号代理与 keep-alive 池）：

```python
def _post(account, path, body, timeout):
    url = account.chat_base_url() + path
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 method="POST", headers=account.headers(purpose="media"))
    try:
        with _accounts.urlopen(req, timeout=timeout, proxy=account.proxy) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise wb_media.describe_media_failure(
            exc.code, _accounts.read_http_error(exc, 400).decode("utf-8", "replace"), phase)
    except Exception as exc:
        raise wb_media.MediaError("UPSTREAM", "网络失败：%s" % exc)
```

`phase` 由调用方传（`"image"` / `"submit"` / `"poll"`），因为它决定 400 是 `REJECTED` 还是 `UPSTREAM`。

轮询（照 dsh 的 5s / 600s）：

```python
def poll_video_task(account, task_id, interval=5, timeout=600):
    started = time.time()
    while True:
        if time.time() - started > timeout:
            raise wb_media.MediaError("UPSTREAM", "轮询超过 %d 秒仍未完成" % timeout)
        data = wb_media.parse_video_task(_post(account, "/v2/videos/tasks",
                                               {"task_id": task_id}, 60, "poll"))
        if data["status"] == "completed":
            if not data["videos"]:
                raise wb_media.MediaError("EMPTY_RESULT", "任务完成但没有视频 URL")
            return data
        if data["status"] == "failed":
            raise wb_media.MediaError("UPSTREAM", "上游任务失败")
        time.sleep(interval)          # 未知 status 一律当"还没好"
```

## 5. `wb_media_tasks.py` 契约

```python
class MediaTaskStore:
    def __init__(self, out_dir, workers=1)
    def submit(self, kind, model, params, account_uid, body) -> task_id
    def get(self, task_id) -> dict | None           # 不在内存则查磁盘
    def list(self, limit=50) -> [dict]
    def sign(self, task_id, index, ttl=3600) -> str
    def verify(self, task_id, index, token) -> bool
    def _worker(self)                               # 单线程消费
    def _persist(self, task)                        # media/tasks.json，原子写
```

### 5.1 状态机

```
submit() → queued ─worker 取到→ running ─┬→ completed      （有 videos[] 且已下载）
                                          ├→ rejected        （REJECTED：没建任务、没花钱）
                                          ├→ failed          （TASK_FAILED / 轮询超时 / 下载失败）
                                          └→ lost            （进程重启时仍在 running）
```

- `lost` 必须显出来：上游可能已经扣了费，面板若只显示 `failed` 就查不到「钱花了没产物」
- 终态任务保留 7 天（`media/tasks.json` 里带 `finished_at`），清理在 `submit()` 里机会式执行

### 5.2 单 worker 的理由

视频本来就慢（几十秒到几分钟），串行不损失什么；而**同一账号同时跑两条视频**是明确的风控放大面。
真需要并发时把 `workers` 调大即可，但默认 1。

### 5.3 签名下载令牌

```python
def sign(self, task_id, index, ttl=3600):
    exp = int(time.time()) + ttl
    msg = ("%s|%d|%d" % (task_id, index, exp)).encode("utf-8")
    mac = hmac.new(self._secret, msg, hashlib.sha256).hexdigest()[:32]
    return "%d.%s" % (exp, mac)
```

`self._secret = secrets.token_bytes(32)`，进程内生成。重启后旧链接失效——**与任务表一起失效**，
口径一致，不留"能下载但查不到任务"的悬挂状态。

### 5.4 产物目录

`media/` 跟随代码目录的绝对路径派生（与 `CATALOG_CACHE_DIR` 同一口径），**不用 `os.tmpdir()**`：
网关是长跑进程，系统临时目录会被清理工具扫掉，用户还拿着链接就 404 了。

```
media/
├── out/<task_id>-<i>.mp4
└── tasks.json
```

## 6. `wb_proxy.py` 的处理函数

### 6.1 `_handle_media_post`

```python
    def _handle_media_post(self, path, payload):
        if not wb_settings.media_config(ACCOUNTS_DIR)["enabled"]:
            # 关着就是不存在：不给 403，少一个探测面
            return self._error(404, "not found", "invalid_request_error")
        try:
            if path in ("/v1/images/generations", "/images/generations"):
                return self._media_image(payload, edit=False)
            if path in ("/v1/images/edits", "/images/edits"):
                return self._media_image(payload, edit=True)
            return self._media_video_submit(payload)
        except wb_media.MediaError as exc:
            return self._media_error(exc)
```

### 6.2 `_media_image`（完整流程）

```python
    def _media_image(self, payload, edit):
        realm = self._media_realm()
        prompt = (payload.get("prompt") or "").strip()
        if not prompt:
            return self._error(400, "prompt is required", "invalid_request_error")
        try:
            models = self._media_catalog(realm)
            tag = wb_media.TAG_IMAGE_TO_IMAGE if edit else wb_media.TAG_TEXT_TO_IMAGE
            model = wb_media.pick_model(models, payload.get("model"), tag)
            size = wb_media.check_size(payload.get("size"))
            inputs = self._media_inputs(payload) if edit else []
            if edit and not inputs:
                raise wb_media.MediaError("INVALID_ARGS", "改图要带 image")
            body = (wb_media.build_edit_body(model["id"], prompt, size, inputs) if edit
                    else wb_media.build_image_body(model["id"], prompt, size))
        except wb_media.MediaError as exc:
            # 全部发生在"发请求"之前：不花积分、不冷却、可以直接改参数重来
            return self._media_error(exc)

        if not self._media_rate_ok():                       # 每 key 每分钟次数
            return self._rate_limited(429, "media rate limit", wait=60)
        if not _acquire_media_slot():
            return self._error(503, "media slots busy; retry shortly", "server_error")

        excluded, tried = set(), 0
        try:
            while tried < MEDIA_ROTATE_MAX:                  # 3
                account = self._pick_media_account(realm, model["id"], cost_hint=8)
                if account is None:
                    return self._error(503, "no usable account for media", "server_error")
                t0 = time.time()
                try:
                    out = (client.edit_image(account, body) if edit
                           else client.generate_image(account, body))
                except wb_media.MediaError as exc:
                    self._media_note_failure(account, exc, model["id"], t0)
                    if exc.code in MEDIA_RETRYABLE and tried + 1 < MEDIA_ROTATE_MAX:
                        excluded.add(account.uid); tried += 1; continue
                    return self._media_error(exc)
                self._media_note_success(account, model["id"], out.get("credit"),
                                         "image", t0, realm)
                return self._json(200, {"created": int(time.time()), "model": model["id"],
                                        "credit": out.get("credit"),
                                        "data": [self._media_image_payload(out, payload)]})
        finally:
            _release_media_slot()
```

### 6.3 `_media_video_submit`（提交只管到 task id）

```python
    def _media_video_submit(self, payload):
        realm = self._media_realm()
        try:
            models = self._media_catalog(realm)
            tag = (wb_media.TAG_IMAGE_TO_VIDEO if payload.get("image")
                   else wb_media.TAG_TEXT_TO_VIDEO)
            model = wb_media.pick_model(models, payload.get("model"), tag)
            payload = dict(payload, model=model["id"])
            params = wb_media.resolve_video_params(payload, model.get("videoGeneration"))
            body = wb_media.build_video_body(payload, params)
            cost = wb_media.estimate_cost("video", model["id"], params["seconds"])
        except wb_media.MediaError as exc:
            return self._media_error(exc)

        account = self._pick_media_account(realm, model["id"], cost_hint=cost)
        if account is None:
            return self._error(503, "no usable account (or none can afford %.0f credits)" % cost,
                               "server_error")
        task = MEDIA_TASKS.submit("video", model["id"], params,
                                  account.uid, body, realm, self._key_id())
        if str(payload.get("wait") or "").lower() in ("1", "true", "yes"):
            # 阻塞式：客户端不会轮询时用。最长 600s 占一个请求线程，默认不建议。
            done = MEDIA_TASKS.wait(task["id"], timeout=VIDEO_POLL_TIMEOUT)
            return self._media_task_json(done)
        return self._json(202, {"id": task["id"], "status": task["status"],
                                "model": model["id"], "seconds": params["seconds"],
                                "resolution": params["resolution"],
                                "poll": "/v1/videos/%s" % task["id"]})
```

`202` 而不是 `200`：任务确实还没完成，客户端据此知道该轮询。

### 6.4 成本预检（视频必需）

```python
def estimate_cost(kind, model_id, seconds):
    """一次调用的积分上界估计。只用于「这个账号还付得起吗」，不用于报价。

    数据来自 dsh 的实测与目录 fps 口径：kling 文生 25.71/s（最贵）、图生 17.14/s，
    minimax 两个模式都 14.29/s；图片 5.71/张。取最贵那条当上界，宁可少派一次号，
    也不要在任务成功后排干一个账号的余额。
    """
    if kind == "image":
        return 6.0
    if kind == "video":
        return 26.0 * float(seconds or 5)
    return 0.0
```

`_pick_media_account` 里多一道：

```python
def _enough_credit(account, cost):
    credits = getattr(account, "credits", None)
    if not isinstance(credits, dict):
        return True                     # 余额未知 = 不拦（reserve_blocked 同一口径）
    try:
        remain = float(credits.get("remain"))
    except (TypeError, ValueError):
        return True
    return remain >= cost
```

理由：`ready()` 只保证账号"能用"，不保证"付得起"。一条 5 秒 kling 视频 128 积分，
在 300 积分余额的账号上是 43% —— 一次调用就该换号，而不是等 402。

### 6.5 失败归因（照抄 dsh 的「只有真花过钱才冷却」）

```python
MEDIA_RETRYABLE = {"AUTH", "RATE_LIMIT", "NO_CREDIT", "UPSTREAM"}   # 换号有意义
MEDIA_BILLABLE = {"AUTH", "RATE_LIMIT", "NO_CREDIT", "UPSTREAM",
                  "EMPTY_RESULT", "TASK_FAILED"}                    # 可能已扣费 → 冷却
# REJECTED / INVALID_ARGS / NO_MEDIA_MODEL / NO_ROUTE(本地判定) 不进冷却

def _media_note_failure(self, account, exc, model_id, t0):
    if exc.code == "REJECTED":
        return                          # 没建任务、没花钱：不冷却、不记账号黑账
    if exc.code == "AUTH" and exc.status == 403 and restriction_is_account_level(exc.detail):
        account.note_restricted(detail=exc.detail)      # 11140 红线：整池暂停，不换号
        _persist_restriction(account)
        note_account_restriction(account.realm, account.uid)
        return
    if exc.code in MEDIA_BILLABLE:
        account.note_error(exc.message, cooldown=60, model=model_id, detail=exc.detail)
```

403 + 11140 这条**必须**先于换号判断：账号级风控下换号继续打同一台设备，正是仓库红线要防的事。

### 6.6 计费落账

```python
def _media_note_success(self, account, model_id, credit, kind, t0, realm):
    account.note_success(model=model_id)
    record_usage(model_id, {"credit": credit or 0}, stream=False,
                 elapsed_ms=(time.time() - t0) * 1000, fp=None,
                 account=account.uid, outcome="completed",
                 key=self._key_id(), realm=realm, kind=kind)
```

`_extract_usage` 只认 `credit`/token 字段，传 `{"credit": x}` 即可；token 全 0 → 不写
`cache_hit_pct`、不写 `tokens_per_sec`，面板的 token 统计不会被动到。

## 7. `wb_settings.py` 开关

```python
MEDIA_DEFAULTS = {
    "enabled": False,          # 总闸；false → 路由 404
    "realm": "cn",
    "identity": "desktop",     # P0 结论落这里
    "image_max_per_minute": 6,
    "video_max_concurrent": 1,
    "video_max_seconds": 15,
    "max_rotate": 3,
}

def media_config(accounts_dir):
    """带默认值的读取；环境变量 WB_MEDIA_ENABLED=1 可临时置 enabled（CI/冒烟用）。"""
    data = (load(accounts_dir) or {}).get("media") or {}
    cfg = dict(MEDIA_DEFAULTS)
    cfg.update({k: v for k, v in data.items() if k in MEDIA_DEFAULTS})
    if os.environ.get("WB_MEDIA_ENABLED") == "1":
        cfg["enabled"] = True
    return cfg
```

老安装的 `settings.json` 没有 `media` 键 → 全默认 → 行为与今天逐字节一致（这是 PR 里要写明的门禁）。

## 8. 测试清单

`tests/_test_media_logic.py`（新）
- 目录投影：夹具**直接用 `catalog/product-config-cn.json` 的真实形状**（60 条 → 4 条带 tag），
  断言 `videoGeneration` 字段一个不丢（kling 的 `audioMode: switchable` / minimax 的 `always-on`）
- `pick_model`：点名不在组内 → `NO_MEDIA_MODEL` 且消息列出可用项；跨组点名（文生图模型去改图）同名报错
- `check_size`：`1024x1024` 过、`2048x2048`(4.19MP) 过、`2560x1600`(4.10MP) 过、
  `2560x2560`(6.55MP) 拒、`abc` 拒、`0x0` 拒
- `build_image_body` 三分支：`hunyuan-*` 有 `n` 无 `response_format`；`gemini-*` 两者都无；
  `hy-image-3.5` 有 `response_format`
- `resolve_video_params`：kling 3 秒过/2 秒拒、minimax 3 秒拒/4 秒过（**同一函数、不同模型不同边界**）、
  拿不到 config 时越界值**透传**、`resolution` 大小写不敏感命中后发目录拼写、
  目录无 `resolutions` 时透传、`aspect_ratio` 图生视频恒 `None`
- `parse_supported_seconds_range`：`4-15` / `to` / `~` / en dash / 倒序 / 0 / 无匹配
- `describe_media_failure`：提交阶段 400 → `REJECTED`；轮询阶段同样 400 → `UPSTREAM`
- `estimate_cost`：视频按秒线性、图片 6

`tests/_test_media_routes.py`（新）
- `enabled=false` → 404（三条 POST 全测）
- 无 key → 401；开着 + 有 key → 通过
- `model` 不在组里 → 400 且 **上游零请求**（monkeypatch `urlopen` 断言未调用）
- 上游 400 → 客户端 400、账号**不进冷却**、任务态 `rejected`
- 上游 401 → 换号（第二次 `urlopen` 用另一个 uid）
- 上游 429 → 客户端 429 且带 `Retry-After`
- usage 行：`kind=image`、`credit=5.71`、`cost_policy="media"`
- 视频 `?wait=1` 长轮询超时 → 504（而不是挂死线程）

既有：`python tests/run_all.py` 全绿、`python scripts/check_clean_checkout.py` 干净、
README 套件数 244 → 246。

## 9. P0 取证脚本（`scripts/probe_media.py`）

不做成正式功能，只回答三个问题，跑完把结论写进 `MEDIA_PLAN` 的「未定论」一节：

1. **桌面身份打媒体端点是否 200**（花 ~5.71 积分）
   - 用 `purpose="media"` 组头 → `POST /v2/images/generations` + `hunyuan-image-alpha` + `1024x1024`
   - 记：HTTP 码、`usage.credit`、产物 URL 的桶与签名有效期
   - 401/403 → 换 `billing_ua` 再试一次；还不通 → 记「需 CLI 身份」，`media_identity` 默认改 `cli`
2. **`REJECTED` 链路**（不花钱）：`POST /v2/videos/generations` + `seconds=1`，
   期望 400 + `code 14407` + `supported range: 3-15` —— 同时验证 `parse_supported_seconds_range`
   能不能从**真实**文案里读出来（dsh 的样例是 `4-15`，本机 kling 目录写 3-15，可能不一致）
3. **intl 有没有媒体模型**：拿不到 intl 账号就记「未取证」，不猜

脚本只读 token（复用 `wb_accounts` 的加载），不落盘任何凭证，输出一份 JSON 小结。

## 10. 提交纪律（按 CONTRIBUTING 硬性规则）

- 分支：`feat/media-image`（P1）、`feat/media-video`（P2），各自基于**最新 main**
- 提交主题：`feat(media): 接入生图代理（/v1/images/generations|edits）`，正文中英双语
- 冒烟（提交前必须过）：8788 被占 → 起 8799；面板能开；`WB_MEDIA_ENABLED=1` 后真账号出图；
  视频走 `REJECTED` 零成本链路
- README「六、版本更新记录 → ### Unreleased」记一条，套件数同步
- 默认行为不变：老安装不配 `media` 键 → 路由 404，与今天一致

## 11. 账号池策略：媒体怎么跟 chat 共用同一套调度

原则：**媒体不是另写一套调度，而是池子的一种新「请求类型」消费者**——直接调 `POOL.pick()`，
靠 `model` 参数天然继承策略。下面把「白拿 / 要补 / 必须禁」三类分清楚。

### 11.1 白拿的（一行代码不用写）

`POOL.pick()` 与 `Account.ready()` 本来就是 model 参数化的，媒体只要传对 model 就继承：

| 策略 | 机制 | 媒体为什么自动成立 |
| --- | --- | --- |
| 手动权重优先（面板设 >1） | `_pick_weighted_first` | 同一层，传 model 即可 |
| 临期积分优先 | `_pick_expiring_first` + `in_expiring_window()` | 媒体最贵，正该优先花临期积分 |
| 剩余用量优先（默认关） | `_pick_remaining_first` | 媒体模型不在剩余权重表 → 这层自动 no-op，**不会误判** |
| 轮转 | `_rotate_pick` | 见 11.4 的游标讨论 |
| 账号级冷却 / 熔断 / 降权窗口 | `ready()` → `throttle_wait()` 取 max | 上游真有病，媒体也该避开 |
| 日 token 限额 | `daily_limit_blocked` / `model_token_limit_blocked` | 媒体不消耗 token → 恒不命中，不会误拦 |
| 日积分上限 | `credit_limit_blocked(model)` | 媒体模型不在 `free_models` → **按付费模型处理**，正是要的 |
| 低余额保底 | `reserve_blocked(model)` | 同上，付费口径生效 |
| **模型级冷却隔离** | `note_error(model=X)` **只写 `model_cooldowns[X]` 后 return** | **最大的红利**：媒体失败不污染 chat 的账号可用性 |
| 余额即时扣减 | `consume_credit()` | `record_usage` 传了 credit 就自动走 |

两条值得单独记住的实现事实：

1. `note_error(message, cooldown=60, model=<媒体模型>)` 命中 `if model:` 分支后**直接 return**，
   不碰账号级 `cooldown_until`。所以只要每次都传 model，媒体冷却天然是「这台的这个模型」，
   另一个窗口里的 chat 完全不受影响。
2. 媒体模型不在 `self.free_models` 里，于是 `reserve_blocked()` / `credit_limit_blocked()`
   把它当**付费模型**——低余额保底与日积分上限自动生效。这条不用写代码，但要在 PR 里点明，
   否则后人容易误以为"媒体是特殊路径，护栏要另加"。

### 11.2 必须补的四道闸门（`_pick_media_account()` 里）

```python
def _pick_media_account(self, realm, model_id, cost_hint, excluded):
    # ① 派发前刷新护栏状态（open_upstream 也是这么开头的）
    apply_daily_guards()
    apply_reserve_credits()
    if POOL is not None:
        POOL.apply_remaining_weights(
            remaining_schedule_weights() if remaining_priority_enabled() else None)

    # ② 连坐保护：整池暂停时一个上游请求都不发
    hold = restriction_hold_remaining(realm)
    if hold > 0:
        raise wb_media.MediaError("RATE_LIMIT",
            "上游账号级限制生效中，%d 秒后再试" % (hold + 1))

    # ③ 账号白名单（安全边界）
    allowed = self._key_allowed_accounts()

    # ④ 钉住的账号：必须用它，不能用就报错，绝不静默换
    pinned = pinned_account()
    if pinned is not None:
        if allowed is not None and pinned.uid not in allowed:
            raise wb_media.MediaError("AUTH", "钉住的账号不在本 Key 的绑定范围内")
        if pinned.realm != realm:
            raise wb_media.MediaError("INVALID_ARGS",
                "钉住的账号属于 %s 区，媒体只走 %s" % (pinned.realm, realm))
        if not pinned.ready(model=model_id) or not _enough_credit(pinned, cost_hint):
            raise wb_media.MediaError("UPSTREAM",
                "钉住的账号此刻不可用：%s" % pinned.unavailable_reason(model=model_id))
        return pinned

    for _ in range(4):                      # pick 之后再过一道「付得起」
        account = POOL.pick(realm=realm, exclude=excluded, model=model_id,
                            allowed=allowed)
        if account is None or _enough_credit(account, cost_hint):
            return account
        excluded.add(account.uid)
    return None
```

四条都不能省，各自的后果：

| 漏掉 | 后果 |
| --- | --- |
| `apply_daily_guards` / `apply_reserve_credits` | `ready()` 读的是过期的守卫缓存，被日限额/保底挡住的号会被派出去 |
| `restriction_hold_remaining` | 11140 之后继续打同一台设备 → 连坐清扫其它账号（仓库红线） |
| `_key_allowed_accounts()` | **API Key 越权**：绑定了 A/B 两个号的 key 能花掉 C 号的积分——这是安全边界，不是功能 |
| 钉住检查 | 用户以为在用 A 号，实际扣了 B 号的钱 |

「没有可用账号」的报错直接复用现成的：

```python
    return self._error(503, "no usable account for media: %s"
                       % pool_unavailable_detail(realm, model_id, allowed), "server_error")
```

它会把每个号「八位 UID + 不可用原因」列出来，且尊重 `allowed_accounts`
（issue #665 就是「Key 绑的号在冷却，报错却列了全区号」）。

### 11.3 必须禁的三条（别把 chat 语义带进来）

| 禁止 | 理由 |
| --- | --- |
| `page` 参数 | `POOL.pick(page=...)` 是 chat 分页派发用的独立游标（`_page_cursors`）。媒体传 `page=None`，否则媒体请求会推进 chat 的分页游标 |
| `pick_for_session()` | 会话亲和（同一会话粘同一账号）。媒体没有会话概念，用了会变成「同一个人连画三张永远同一个号」，把单账号打穿并放大风控 |
| `open_upstream()` | 它建 chat body、做 realm fallback、跑 `prepareChatBody` 全套改写。媒体进去既改坏请求体，又会继承 chat 的降级语义 |

另外两条**看起来能复用、其实必须禁**的：

- `note_soft_rate()` —— 它写账号级 `cooldown_until` 并累加 `soft_streak`（降权/熔断阶梯）。
  媒体 429 若调它，一次画图失败会把 chat 的账号级降权阶梯推高。媒体 429 只写 **model 级 60s**。
- `open_upstream_with_fallback()` —— 那是 intl→cn 的出口回退。媒体只走 cn（`media_realm`），
  「回退」无处可去；intl 没账号也没目录留档，回退过去只会 404 而不是成功。

### 11.4 轮转游标：共享还是独立（唯一需要拍板的一处）

`_rotate_pick(group, exclude, model, cursor_key=None)`：`cursor_key=None` 用全池 `_cursor`。

| 选项 | 行为 | 代价 |
| --- | --- | --- |
| **共享（建议默认）** | 媒体与 chat 共用一个游标 | 一条 3 分钟视频算一次派发，会把 chat 的严格交替打散（长期占比不变） |
| 独立 `cursor_key="media"` | 各转各的 | 媒体与 chat 可能同一时刻打到同一个号，短时冲突 |

建议共享：全局公平比「chat 的节奏不被打扰」更重要，而且 `_cursor` 只是递增计数、不会长时间占用。

### 11.5 换号语义（媒体自己的策略，与 chat 有意不同）

```python
MEDIA_ROTATE_MAX = 3                    # 与 chat 的 rotate 上限同量级

MEDIA_RETRYABLE = {"AUTH", "RATE_LIMIT", "NO_CREDIT", "UPSTREAM"}   # 换号可能就过了
MEDIA_BILLABLE  = MEDIA_RETRYABLE | {"EMPTY_RESULT", "TASK_FAILED"} # 可能已扣费 → 冷却
# REJECTED / INVALID_ARGS / NO_MEDIA_MODEL：换谁都是同一个 400 → 不换、不冷却
# 403 + 11140：note_restricted + 整池暂停 → 立刻放弃，不换号
```

| 上游结果 | 换号 | 记账 |
| --- | --- | --- |
| 401 / 403（非 11140） | 是 | `note_error(60s, model=)` |
| 403 + 11140 | **否** | `note_restricted()` + `note_account_restriction()` → 整池暂停 |
| 429 | 是 | `note_error(60s, model=)`（**不调** `note_soft_rate`） |
| 402 | 是 | `note_balance_cooled()`（账号级，余额是账号属性） |
| 5xx / 网络 | 是 | `note_error(60s, model=)` |
| 提交阶段 4xx | 否 | 什么都不记 |
| 轮询失败 / 超时 / 任务 failed | 否（任务已花钱） | 任务态 `failed` |

### 11.6 怎么证明策略真的接上了（测试用例）

读代码说服不了人，用下面几条钉住：

1. **权重优先**：mock 3 个账号权重 A=3 / B=1 / C=1，连发 9 次媒体请求 →
   断言分配比例 ≈ 5:2:2（平滑加权轮转，不是 3:1:1），且**一次都不落到权重 1 的账号上直到 A/B 用尽**
2. **临期优先**：把 A 的临期窗口设为 2 天 → 前几次请求全落 A
3. **模型级隔离（最关键）**：让 A 的 `hunyuan-image-alpha` 冷却 → 断言
   `A.ready(model="hunyuan-image-alpha") == False` **且** `A.ready(model="glm-5.3") == True`
4. **付不起就换**：A 余额 100、视频预估 128 → A 不被选
5. **白名单**：key 绑 {B} → 只派 B；B 冷却 → 503 且报错文案只提 B（`pool_unavailable_detail` 的 `allowed_accounts` 口径）
6. **连坐**：`note_account_restriction(realm)` 后立刻请求 → 503 且**上游零调用**
7. **游标不污染**：媒体请求前后，chat 的 `_page_cursors` 不变

前 3 条在 `tests/_test_media_routes.py` 里用 mock 池即可，不需要真账号。

## 12. 前端（dashboard.html）

### 12.1 现状（实测，18,693 行单文件）

| 机制 | 事实 | 对媒体意味着 |
| --- | --- | --- |
| 主页面 | 7 个 `btnNav<Page>` + `<div id="page<Page>" class="main-page">`，`switchMainTab()` 里一个**硬编码 7 项**的 `tabs` 数组 | 加页签要同时动三处：按钮、容器、tabs 数组 + 加载分支 |
| 设置页区块 | 多个 `<section>`；左侧锚点导航由 `initPageNav()` **扫描 section 自动生成** | 加区块**不用动导航** |
| 「顶部页签显示」 | 清单由 `navPageRows()` **扫描 `.main-nav-btn` 现场生成** | 加页签自动出现在这个清单里，`hidden_pages` 也自动认识它 |
| i18n 英文 | `EN_DICT` **1347 条**，中文原文 → 英文，**键必须与 HTML 文本逐字一致** | 每条新文案都要配一条，漏了就回落中文 |
| i18n 繁体 | `HANT_CHARS` / `HANT_PHRASES` **逐字映射** | 新文案只要用的字在字表里，自动转繁中，不用配 |
| 设置保存 | `postJSON('/settings/save', {key: value})` → 后端 `_handle_settings_save` 是**显式白名单**（`if "xxx" in payload:`） | 未知键**静默忽略**；加 `media` 必须同时改白名单，否则前端保存"成功"但什么都没存 |

### 12.2 分三步

#### P3a · 设置页一个 section（先能开关）

```html
<section>
  <h2>媒体能力（生图 / 生视频） <em id="setMediaState"></em></h2>
  <div style="font-size:12px;color:var(--dim);margin-bottom:12px">
    默认关闭。开启后网关才提供 /v1/images/* 与 /v1/videos/*；关闭时这些路由根本不存在（404）。
    每次生图约 5.71 积分、每条 5 秒视频约 71–129 积分，直接从账号积分里扣，不经 token 计费。
  </div>
  <label><input id="setMediaEnabled" type="checkbox"> 开启生图 / 生视频路由</label>
  <select id="setMediaRealm">      <option value="cn">国内版</option><option value="intl">国际版</option></select>
  <select id="setMediaIdentity">   <option value="desktop">桌面身份</option><option value="cli">CLI 身份</option></select>
  <input id="setMediaImageRate" type="number" min="1" max="60">    每分钟生图上限
  <input id="setMediaVideoSeconds" type="number" min="1" max="15"> 视频时长上限
  <button onclick="saveMediaConfig(this)">保存</button>
</section>
```

后端配套（3 处）：
1. `runtime_settings_view()` 加 `"media": wb_settings.media_config(ACCOUNTS_DIR)`
2. `_handle_settings_save` 加 `if "media" in payload:` + 校验 + `wb_settings.set_media_config()`
3. `set_media_config()` 只接收白名单字段（同 `validate_usage_patch` 的写法），非法值回 400

文案：**12 条**中文 → 12 条 `EN_DICT`。

#### P3b · 独立「媒体」页签（视频必需）

为什么不能塞进「任务与福利」：那一页装的是**上游白送的福利任务**（签到／成长／猫猫旅行）。
媒体的任务是**我们自己花的钱**，混在一起会让「花了多少、成没成」读不出来。

```
<div id="pageMedia" class="main-page">
  <h2>媒体生成</h2>  <p>生图与生视频的开关在「设置」页；这里看模型约束与最近任务。</p>

  <section> 媒体模型清单（只读）
    模型 | 标签（文生图/改图/文生视频/图生视频） | 时长区间 | 分辨率 | 比例 | prompt 上限
    来源 GET /v1/media/models；前端 5 分钟 TTL + 「重新读取」按钮（refresh=1）
  </section>

  <section> 最近任务
    时间 | 状态 | 模式 | 模型 | 时长 | 花费 | 耗时 | 操作
    状态色：completed 绿 / running 琥珀 / rejected 灰 / failed 红 / lost 红+虚线框
    failed 行可展开显示上游 error 原文（含 supported range）
    操作：预览（<video src=后端给的签名 URL>）、下载、重试
    running 时 3 秒轮询一次，全部终态就停
  </section>

  <section> 手动试一次（默认折叠，调试用）
    prompt / size / 参考图 / model / seconds → 直接打 /v1/images/* 或 /v1/videos/*
  </section>
</div>
```

改动清单（4 处 + 2 个函数）：

| # | 位置 | 改动 |
| --- | --- | --- |
| 1 | `<div class="main-nav">` | 加 `<button class="main-nav-btn" id="btnNavMedia" onclick="switchMainTab('media')">媒体</button>` |
| 2 | 页面容器区 | 加 `<div id="pageMedia" class="main-page">` |
| 3 | `switchMainTab()` | `tabs` 数组加 `['media', btnNavMedia, pageMedia]`；末尾加 `if(tab === 'media'){ loadMediaPage(); }` |
| 4 | 新 JS | `loadMediaPage()` / `loadMediaTasks()` / `renderMediaModels()` / `mediaStatusChip()` |

**签名 URL 由后端在任务 JSON 里给出，前端不做任何 HMAC 计算**——前端算签名等于把 secret 送进浏览器。

文案：**约 25 条**（清单表头 6、状态词 5、按钮 5、说明段落 5、错误提示 4）。

#### P3c · 数据看板加媒体分栏（可选）

usage 行已带 `kind`，现有「按模型统计」天然就能看到 `hunyuan-image-alpha` 的花费。
若想单列，加一个 `kind=image|video` 过滤按钮即可（约 30 行）。

### 12.3 压文案成本的具体做法

总量 37 条英文条目是真实成本，所以设计上**能复用就不造新词**：

| 场景 | 做法 |
| --- | --- |
| 表头「模型 / 时长 / 分辨率 / 积分」 | 看板里全是现成词条，直接复用 |
| 状态词 | `completed` → 复用「已完成」；`failed` → 复用「失败」；只有 `rejected`（参数被拒）与 `lost`（进程重启丢失）是新词 |
| 按钮 | 「刷新」「下载」「重试」全是现成词；只有「预览」要新加 |
| 长说明段落 | 尽量拆成短句拼装，不要整段新写（整段新写 = 整段要译） |

目标：把**真正新增的整句**压到 15 条以内，其余靠现有词条拼装。

### 12.4 一点必须实测的：手机端 8 个页签装不装得下

现有 CSS 有两处窄屏断点（`@media` 里 `.main-nav-btn{flex:1;...;font-size:12px/11px}`，`white-space:nowrap`）。
7 → 8 个按钮在 375px 宽上均分后每个约 44px，「媒体」两个字约 26px 加内边距，**大概率还装得下**，
但要在真机（或 DevTools 375px）上看一眼再定，别等上线才发现横滚。

### 12.5 前端测试

`tests/_test_media_page.js`（新，复用 `_dashboard_source.js` 的 sandbox 模式）：
1. `pageMedia` 容器与 `btnNavMedia` 都存在，且 `navPageRows()` 能扫到 `media`
2. `switchMainTab('media')` 会调用 `loadMediaPage`
3. 状态 → 颜色映射：`rejected` 灰、`lost` 红虚框、`failed` 红
4. **断言 dashboard.html 源码里没有 HMAC / crypto.subtle**（签名不许进前端）
5. 设置页 section 的五个控件 id 都在，且 `saveMediaConfig` 发的 payload 形状正确

`tests/_test_i18n_media.js`（新，或并入现有 i18n 套件）：P3a + P3b 的每条新中文文案在 `EN_DICT` 里都有条目，键与 HTML 逐字一致。

### 12.6 前端刻意不做的三件事

| 不做 | 理由 |
| --- | --- |
| 实时进度条 | 上游 `status` 只有 pending/running/completed/failed，**没有百分比**。画一个假的进度条就是撒谎 |
| 批量生成按钮 | 一次一张/一条是刻意的花钱限制；P2 的服务端也没有批量参数 |
| 花费预估器 | 目录条目里**没有价格字段**，价格只在结算时以 `usage.credit` 返回。预估只能是区间，容易让人以为能算准 |

## 13. 还没考虑的：缺口清单

前三块（账号池调度、前端、接口）之外，把工程全生命周期扫一遍，找到 20 项。
前三项是**本方案自身的错误**，必须改；第 4 项是最容易被忽略的运维坑。

### 13.1 🔴 三个必须改的（方案本身的错）

#### ① 本地路径输入 = 任意文件读取 —— 必须禁
方案里照抄了 dsh 的四种输入形态（本地绝对路径 / `file://` / `http(s)` / 裸 base64）。
**在 dsh 里这是对的**：它是一个人本机的 agent 插件，模型读的是用户自己的文件。
**在网关里这是漏洞**：任何能调 API 的客户端都能让网关读服务器上的任意文件并**发到腾讯的图像接口**——
`image: "/etc/passwd"`、`image: "C:\Users\admin\.ssh\id_rsa"`。读失败的错误信息还会泄露"这个文件存在不存在"。

**改法**：媒体路由**只接受** `data:image/*;base64,` 与 `http(s)://` 两种形态。本地路径统一回
`INVALID_ARGS: 本网关不接受本地路径（会把服务器文件发给上游）`。需要本地图就客户端自己读、自己编码。

#### ② `http(s)` 输入 = SSRF —— 必须过公网地址校验
网关有 `start-wb-proxy-lan.bat`，也就是说它可能监听在 LAN 上。若客户端传
`image: "http://192.168.1.10:8080/admin"`，网关会替它去访问内网并回显上游对那张"图"的反应；
传 `http://127.0.0.1:8788/accounts` 更直接——自己打自己。

**改法**：复用现成的 `_is_public_addr()`（15477 行，已经拒绝环回/私有/链路本地/保留段）：
1. 先解析主机名 → 每个结果都必须 `_is_public_addr`
2. 只允许 `http`/`https`
3. 下载时**不跟随重定向**（3xx 直接拒），否则公网 URL 可以 302 到内网
4. 下载体积上限（8 MB）在**读流的同时**判定，不是先 `arrayBuffer()` 再一次比

#### ③ 接口形状要改成 OpenAI 的现成标准
我原来设计的 `/v1/videos/generations` 是自己编的。而 **OpenAI 已经有 Video API**（Sora），
LiteLLM 与官方 Python SDK 都实现了，形状是：

| 方法 | 路径 | 参数 / 语义 |
| --- | --- | --- |
| POST | `/v1/videos` | `prompt`（必填）、`model`、`seconds`（**字符串**，如 `"8"`）、`size`（如 `"1280x720"`）、`input_reference`（参考图） |
| GET | `/v1/videos/{id}` | 返回 `{id, object, status, created_at, ...}`，status ∈ `queued` / `in_progress` / `completed` / `failed` |
| GET | `/v1/videos/{id}/content` | 下载 mp4（我这条猜对了） |
| POST | `/v1/videos/{id}/remix` | 改一条已生成的视频 |

**收益**：`litellm.video_generation()` 和 `client.videos.create()` 零适配直接可用。
**代价**：`seconds` 是字符串、`size` 是 `宽x高`（要对到目录的 `720P/1080P` 上）、status 枚举名要映射
（我们的 `running` → `in_progress`、`rejected` → `failed` + `error.code`）。
本地方便的 `resolution` / `aspect_ratio` / `negative_prompt` / `enable_audio` 作为**可选扩展字段**保留，
不传就按目录默认。

图片侧同理：`POST /v1/images/generations` 与 `/edits` 形状已对；但
- `n > 1` 必须**显式 400**（不能静默只给一张，用户以为拿到四张）
- `/v1/images/edits` 官方是 **multipart**，我们只收 JSON → OpenAI SDK 的 `images.edit()` 会失败。
  要么 P3 补 multipart（手写解析约 60 行，`cgi` 在 3.13 已弃用），要么文档写明"本网关的 edits 用 JSON"

### 13.2 🟠 最大的运维坑：上游更新会把功能抹掉

`wb_update_portable.apply_stage()` 只按发布清单 `manifest["files"]` 覆盖文件——**不删多余文件**。
所以：

- `wb_media*.py` 三个新文件**会留下**
- `wb_proxy.py` / `dashboard.html` **被覆盖回官方版** → 路由入口、开关、前端全没了
- 结果：**功能静默消失**（`/v1/images/*` 又变 404，三个孤儿文件躺在目录里没人调），不报错、不留提示

而且 `update_check_enabled` 默认 **False**（面板手动检查也照样能查），但面板上那个「安装」按钮是**一键**的——
点一下，自定义功能就没了。

**必须先定策略，再动代码**，三选一：

| 方案 | 做法 | 代价 |
| --- | --- | --- |
| A. 冻结上游（推荐） | 把这套改动当自己的分支维护，**不点面板的更新**，`update_check_enabled=false` 保持默认；上游有新版本时手工 rebase | 要定期人工跟进 |
| B. 补丁集 | 改动落成 3 个 `git format-patch` 文件（`wb_proxy.py` 的路由钩子 / `dashboard.html` 的 UI / 三个新模块），更新后 `git apply` 重放 | 冲突要手工解 |
| C. 上游 PR | 按 CONTRIBUTING 提 PR 争取合入 | 周期长、不保证接受 |

我建议 **A + B 并行**：日常冻结，同时把改动记成补丁，这样某天真要跟上游时能重放。
这件事**方案文档里一个字都没提**，属于漏得最狠的一项。

### 13.3 🟡 工程细节缺口

| # | 缺口 | 说明 / 改法 |
| --- | --- | --- |
| 4 | **任务队列没有上限** | 单 worker 串行、每条最长 600s。客户端连发 20 条 = 后面 19 条排队 100 分钟，界面上只显示 `queued`。要设队列上限（建议 10），超了回 **429 + 队列长度**，不要无限排 |
| 5 | **没有取消** | 上游没有 cancel 接口（未证实），但本地 worker 可以放弃轮询并释放槽。至少给 `POST /v1/videos/{id}/cancel` 做"本地放弃" |
| 6 | **任务表线程安全没写** | worker 线程写、请求线程读同一个 dict。必须 `threading.Lock`；`tasks.json` 原子写（tmp + `os.replace`，`persist_remote_catalog` 有现成写法） |
| 7 | **磁盘没有总量上限** | 7 天清理只管"旧"，不管"多"。100 条 × 3.5MB ≈ 350MB；极端一天几百条能吃掉几个 G。加 `media_max_disk_mb`（默认 2048），超了先删最旧的 |
| 8 | **重试 = 重复扣费** | 客户端超时重发，网关当两次请求 → 两张图、两笔钱。要 `Idempotency-Key` 头（key+prompt+size 的 hash 存 10 分钟，命中直接返回上次结果）；不做的话必须在文档里写"本接口不幂等" |
| 9 | **只有每分钟次数，没有累计上限** | 一个 agent 循环 10 分钟 = 340 积分。要 `media_daily_credits_per_key`（默认建议 200，约 35 张图或 1.5 条视频） |
| 10 | **媒体花费会不会撞 chat 的日限额** | `consume_credit()` 扣的是同一个 `remain`，账号若设了 `daily_credit_limit`，媒体花到线就被 `credit_limit_blocked` 拒。**要决策**：让媒体受约束（可预期，但用户可能觉得莫名其妙），还是媒体豁免（花得更快、更难控）。我倾向**不豁免**，但要在文档里写明 |
| 11 | **perf 统计** | 已核对 `_perf_payload` 是**按模型分桶**，媒体不会污染 chat 的 p95，会自成一个模型行（wall 13s、0 t/s）。需要确认面板那一行不会被误读成"这个模型很慢" |
| 12 | **运行日志的 tag** | 媒体要 `log(..., tag="media")`，否则「运行日志」页和 chat 混在一起，排查时要靠猜 |
| 13 | **`/health` 不反映媒体状态** | 媒体队列积压、worker 卡死，`/health` 现在一点都看不出来。加 `media: {enabled, queue_depth, running, last_error}` |
| 14 | **没有"清空队列/暂停派发"** | 出烧钱 bug 时，`media_enabled=false` 能立刻挡住新请求，但**在跑的那条会跑完**（已经花钱了，跑完是对的）。要能一键暂停后续 |
| 15 | **多实例互不可见** | 同一台机器起两个网关（不同端口不同账号）时，任务表各在各的内存里。不是缺陷，但文档要写明"媒体任务不跨实例" |
| 16 | **prompt 落盘** | `tasks.json` 若存 params（含 prompt），等于把用户写的内容落盘。`usage.jsonl` 是不存 prompt 的——任务表要不要存？我倾向只存前 80 字符 + hash，够排查、不留全文 |
| 17 | **产物目录权限** | Windows 上继承父目录 ACL 即可，但要确认 `media/out` 不被 LAN 共享出去。现有 `windows-acl` 相关逻辑（`wb_atrest`）可参照 |
| 18 | **错误码映射表要定死** | 402 不是 OpenAI 标准码；`REJECTED`(400) / 上游 5xx(502) / 队列满(429) / 开关关(404) 各自对客户端意味着什么，要一张表，前端、文档、测试三处口径一致 |
| 19 | **README 的 API 文档** | 现在 README 只讲 chat。用户是程序，必须有一节"媒体接口"（形状、错误码、单价量级、幂等性声明） |
| 20 | **发现性** | 默认关，用户不知道有这个功能。README 一条 + 设置页那段说明 + 首次开启时的 toast |

### 13.4 复盘：上面这些里已经补齐的（2026-11-01 晚）

第一版实现漏了几项，真实验收后逐条补上，记在这里免得文档与代码不一致：

| 项 | 状态 | 落点 |
| --- | --- | --- |
| **任务归属**（#4 安全面） | ✅ 已修 | `_media_task_visible()`：Key 只看得到自己那条，别的 Key 查别人的任务回 404（不确认存在性）；面板会话看全部 |
| **`daily_credits_per_key` 从未生效** | ✅ 已修 | `media_key_spend_allow()` / `media_key_spend_add()`：Key 级每日媒体积分，预检用预估上界、累计用实际扣费 |
| **`media_identity` 从未生效** | ✅ 已修 | `wb_media_client.set_identity()`：`_media_config()` 读到配置就同步过去，出站头真的按它换形状 |
| **`response_format=b64_json` 未实现** | ✅ 已修 | 上游给 COS URL 时由网关 `fetch_bytes()` 下载转码；转码失败退回 url 并在 hint 说明 |
| **取消任务无路由** | ✅ 已补 | `POST /v1/videos/{id}/cancel`：只对 `queued` 生效，`running` 回 409 并说明上游没有取消接口 |
| 运行日志按 kind 打 tag | ✅ 已做 | `image done:` / `video done:`，不再冒充 `chat done` |
| `/health` 反映媒体状态 | ✅ 已做 | `media: {enabled, realm, identity, slots, inflight, tasks?}` |
| perf 统计不污染 chat | ✅ 已核对 | `_perf_payload` 按模型分桶，媒体自成一列 |
| `scripts/probe_media.py` | ✅ 已补 | L4 端到端探针：默认 dry-run、`--yes` 才花钱；`--models` 与 `--video-reject` 零成本 |
| 面板 UI | ⬜ 未做 | 见 §12 |
| 幂等（`Idempotency-Key`） | ⬜ 未做 | 已知缺口，README 里明确写了"不幂等、勿自动重试" |

### 13.5 三个要你决策的

1. **媒体花费受不受 chat 的日积分上限约束**（#10）—— 我倾向受约束，可预期优先
2. **更新策略选 A / B / C**（13.2）—— 这件事不定，代码写完可能下次更新就没了
3. **`/v1/videos` 改不改用 OpenAI 形状**（③）—— 我强烈建议改，代价只是几个字段映射

## 14. 路线 C（上游 PR）：落地方案

决策：向上游提 PR 争取合入。下面是这条路要求的东西，与"自己用"版本的差别，以及拆分方式。

### 14.1 上游现状取证（2026-11-01）

| 事实 | 依据 | 含义 |
| --- | --- | --- |
| **没有任何 issue 提过生图/生视频需求** | 用 `生图 / 画图 / 视频生成 / images / videos` 搜全量 issue，无命中 | 提 PR 前**必须先开 issue 探口风**，否则是"没人要的功能" |
| 大功能 PR 能合入，但常由维护者重新整合 | #712（CodeArts 第三出口，20 文件）被 CLOSED，维护者在 #715 里"完整保留并集成"后合入 | 大 PR 可行，但要有被二次加工的心理准备 |
| 维护者会亲自加固安全与权限 | #715 里"API Key 权限与账号白名单强制校验、OAuth 回调安全性加固" | 我们**主动**把 SSRF、本地路径、key 白名单做对，能显著提高接受率 |
| 合并门槛明确 | #712 评论："全量 244 个测试套件与跨平台 CI 已全部通过，现已随 #715 正式合并" | 244 套件全绿 + 三条腿 CI 是硬门槛 |
| 大 UI 改动容易被搁置 | #725（面板改版，5 文件）OPEN、**0 条评论** | UI 相关必须拆细、不动外壳 |
| 主分支迭代极快 | 一天多个 PR，常出现"合入 #710/#700/#709… 后本 PR 冲突" | 每个 PR 提交前必须 rebase，且要能独立回滚 |

### 14.2 PR 拆分：6 个而不是 2 个

原来按"生图 / 生视频"拆 2 个 PR。走 C 路线要拆更细——前两个 PR **纯新增文件、零接线**，
是最容易合、最不容易冲突的部分：

| PR | 内容 | 改动的既有文件 | 风险 |
| --- | --- | --- | --- |
| **PR1** | `wb_media.py` 纯逻辑 + `tests/_test_media_logic.py` | **无** | 极低（新文件，谁都不会碰） |
| **PR2** | `wb_media_client.py` + `Account.headers(purpose="media")` + 测试 | `wb_accounts.py`（+55 行，新分支） | 低 |
| **PR3** | 生图两条路由 + `media_enabled` 开关 + usage 记账 | `wb_proxy.py`（+90）、`wb_settings.py`（+40） | 中（热路径） |
| **PR4** | 视频任务表 + 三条路由 + 学习区间 | 新增 `wb_media_tasks.py`；`wb_proxy.py`（+90） | 中 |
| **PR5** | 设置页 section（开关 UI） | `dashboard.html` | 低（只加 section） |
| **PR6** | 「媒体」页签 | `dashboard.html` | 中（动 nav + tabs 数组） |

PR1/PR2 合入后，PR3~PR6 就是"接线"，各自独立。若 PR3 被拒，PR1/PR2 仍是无害的铺垫（可以只留作将来用）。

### 14.3 相比"自己用"版本，走 C 要额外满足的

| 要求 | 原因 |
| --- | --- |
| **不能假设用户账号有媒体模型** | 别人可能只有 chat 模型。目录为空时必须优雅说"该区没有媒体模型"，不能报错崩 |
| **realm 通用** | 我只验了 cn。代码不能写死 cn（`media_realm` 默认 cn 是配置，不是硬编码） |
| **默认关 + 文档** | 花钱能力不能升级附赠（维护者的既有口径，见 dsh 同一思路） |
| **i18n 完备** | 英文 `EN_DICT` 每条都要有，否则英文界面回落中文——CONTRIBUTING 点名过 |
| **纯标准库 / Python 3.9** | CI 矩阵 3.9 / 3.12 × Ubuntu / Windows |
| **244 → 246+ 套件数同步** | README 那行有测试钉住 |
| **README `### Unreleased` + `docs/CHANGELOG.md`** | 发布纪律 |
| **不带本机绝对路径 / token / 个人信息** | CONTRIBUTING 隐私条 |
| **一个 PR 一件事** | 所以拆 6 个，不合成 1 个 |
| **五段式 PR 正文** | 动机 / 做了什么 / 怎么验证 / 影响面与回滚 / 与其他 PR 关系，中英双语 |

### 14.4 三个安全项必须在 PR3 之前做对

维护者会审这块（#715 就是亲自加固权限校验）。这三项如果被别人先发现，PR 基本就是被打回：

1. **本地路径输入禁止**（否则任意文件读取）
2. **`http(s)` 输入过 `_is_public_addr` + 不跟随重定向**（否则 SSRF）
3. **`_key_allowed_accounts()` 必须生效**（否则 API Key 越权花别人账号的钱）

第 3 项是**账号池**设计的一部分，第 1、2 项是**输入契约**的一部分——这也是为什么我说它们要在写代码前改。

### 14.5 接口形状必须一次做对

选 C 之后，`/v1/videos` 的形状从"我家自用"变成**公共契约**：合入后改形状就是 breaking change。

所以 §13.1 ③ 的判断必须现在就落：
- **主形状按 OpenAI Video API**（`POST /v1/videos` + `GET /{id}` + `GET /{id}/content` + `POST /{id}/remix`、
  `seconds` 字符串、`size` 宽x高、`input_reference` 参考图、status ∈ `queued/in_progress/completed/failed`）
- 本地扩展字段（`resolution` / `aspect_ratio` / `negative_prompt` / `enable_audio`）作为可选附加，不改变标准形状
- 图片侧 `n>1` 显式 400；`/v1/images/edits` 是 JSON-only 还是补 multipart，要在 PR3 的正文里写明取舍

### 14.6 提 PR 前的第 0 步：先开 issue

不发代码，先问。标题与正文要点：

```
[Feature] 支持生图 / 生视频（/v1/images/* 与 /v1/videos/*）
- 动机：WorkBuddy 官方 CLI 的 ImageService / VideoService 是一等能力，网关目前只代理了 chat；
        目录里已经有 hunyuan-image-alpha、kling-video-v3、minimax-video-h3，但 /v1/models 里被
        is_chat_model() 过滤掉了，客户端无从发现。
- 已取证：端点、请求体分支、单价（图 ~5.71 / 视频 57~129 积分）、尺寸上限（419 万像素）、
        视频为异步任务（提交→轮询→下载）。附 docs 里的两份文档。
- 设计要点：复用现有账号池（model 级冷却隔离）、逐 key 每分钟配额 + 每日积分上限、
        默认关、产物回 URL / 本地文件不塞 JSON、按 OpenAI Video API 形状对外。
- 安全：不接受本地路径输入（防任意文件读取）、http(s) 过公网地址校验（防 SSRF）、
        尊重 Key 的账号白名单。
- 想请作者定：要不要这个功能？要不要拆成 6 个 PR？UI 是并入设置页还是独立页签？
```

**如果 issue 被否**：退回路线 A（冻结上游、自己用）。所以代码组织必须两条路都走得通——
这也是为什么 `wb_proxy.py` 只改 5 处：接线点越少，自己维护的 patch 越好重放。

## 15. 决策记录与最终契约（以本节为准）

三项决策已定，本节是**唯一权威口径**。§2 / §6.3 / §12 / §14.2 中与之冲突的条目作废。

| # | 决策 | 值 | 影响 |
| --- | --- | --- | --- |
| D1 | 媒体花费与 chat 的日积分上限关系 | **受约束**（不豁免） | 需要新增"日限额预检"，见 15.3 |
| D2 | `/v1/videos` 形状 | **OpenAI Video API** | 重写对外契约，见 15.1 |
| D3 | PR 拆分 | **一个 PR**（内含 6 个 commit） | 见 15.4 的风险与缓解 |

### 15.1 最终对外契约（D2）

图片侧沿用 OpenAI Images，视频侧对齐 OpenAI Video API：

| 方法 | 路径 | 参数 | 响应 |
| --- | --- | --- | --- |
| POST | `/v1/images/generations` | `prompt`(必填)、`model`、`size`、`n`、`response_format`(`url`\|`b64_json`) | `{created, data:[{url\|b64_json, revised_prompt?}], credit, model}` |
| POST | `/v1/images/edits` | 同上 + `image`（**仅 data URL / 公网 http(s)**） | 同上 |
| POST | `/v1/videos` | `prompt`(必填)、`model`、`seconds`(**字符串** `"5"`)、`size`(`"1280x720"`)、`input_reference`（图生视频的参考图） | `{id, object:"video", status, created_at, model, seconds, size, credit?}` |
| GET | `/v1/videos/{id}` | — | 同上，`status` 推进；完成时带 `url`（本地下载地址） |
| GET | `/v1/videos/{id}/content` | `?i=<序号>`，支持 `?t=<签名>` | `video/mp4` 字节流 |
| POST | `/v1/videos/{id}/remix` | `prompt` | 新建一个任务（复用原任务的模型与参数） |
| GET | `/v1/media/models` | `?realm=cn` | 媒体模型清单（含 `videoGeneration` 原样） |

**扩展字段**（不传即按目录默认，不破坏标准形状）：`negative_prompt`、`enable_audio`、`resolution`（可直接指定 `720P`/`1080P`/`768P`/`2K`/`4K`，绕开 `size` 映射）。

**status 映射**（内→外，一处映射，前端/文档/测试三处同源）：

| 内部 | 对外 | 含义 |
| --- | --- | --- |
| `queued` | `queued` | 排队中 |
| `running` | `in_progress` | 上游在跑 |
| `completed` | `completed` | 完成，带 `url` |
| `rejected` | `failed` + `error.code="invalid_request"` | 提交阶段被 4xx 退回，**没扣钱** |
| `failed` | `failed` + `error.code="upstream_error"` | 上游任务失败/轮询超时/下载失败，**可能已扣钱** |
| `lost` | `failed` + `error.code="task_lost"` | 进程重启丢失，**可能已扣钱** |

`rejected` 与 `failed` 在对外都是 `failed`，但 `error.code` 不同——**这个区分必须保留**，因为它决定客户端能不能立刻重试（`invalid_request` 重试有意义，`upstream_error` 重试要小心重复扣费）。

### 15.2 `size` ↔ 目录 `resolutions[]` 的映射（D2 必须定死）

OpenAI 用 `size: "宽x高"`，我们的目录用 `"720P"/"1080P"/"768P"/"2K"/"4K"`。映射规则：

```python
def size_to_resolution(size, resolutions, default):
    """按短边分档，再在该模型的 resolutions[] 里选最高的不超过它的那一档。

    短边而不是长边，是因为竖版 720x1280 的短边才是「720 这一档」。
    """
    w, h = parse(size)                       # "1280x720" → (1280, 720)
    short = min(w, h)
    ladder = {"720P": 720, "768P": 768, "1080P": 1080, "2K": 1440, "4K": 2160}
    wanted = [r for r in resolutions if ladder.get(r.upper(), 0) <= short]
    if not wanted:
        return default, "size %s 低于该模型最低档 %s；改用 %s" % (size, resolutions, default)
    best = max(wanted, key=lambda r: ladder[r.upper()])
    return best, None
```

配套两个规则：
- `size` 的宽高比决定 `aspect_ratio`（仅文生视频）：`w>h → 16:9`、`w<h → 9:16`、`w==h → 1:1`；
  该模型 `aspectRatios` 里没有对应值时回落到 `defaultAspectRatio` 并给 `warnings`
- 显式传了 `resolution` 时**以它为准**，`size` 只用来决定比例（避免两个字段打架）
- 目录没有 `resolutions[]` 时：`size` 原样透传不拦（与 §3.3 的"知道才拦"同一条判据）

> 实测提示：dsh 只观察到 1280×720 与 1344×768 两种产物，目录却写着 `1080P`/`4K`。
> 所以映射出来的是**请求档位**，不是承诺的产物尺寸；文档与前端都要写「以实际产物为准」。

### 15.3 D1 引入的新问题：日积分上限需要"成本预检"

`credit_limit_blocked()` 只在 `used >= limit` 时为 True（`wb_accounts.py:1540`），
**不预判本次要花多少**。原样复用会出这个问题：

```
账号日上限 200，今天已用 150 → ready() 通过
提交一条 5 秒 kling 视频（约 128 积分）→ 上游真扣 128
今日累计 278，超上限 78 → 之后 credit_limit_blocked 变 True → 该账号被 chat 摘掉
```

也就是说：**媒体单次超额会连带降低 chat 的可用账号数**。图片（5.71）影响小，视频（57–129）是上限量级的 30–65%，必须拦住。

**改法**：`_pick_media_account()` 里加第三道预检（前两道是 `ready()` 与 `_enough_credit(remain)`）：

```python
def _daily_headroom(account, cost_hint):
    """今天还剩多少积分额度；None 表示没设上限或未知。"""
    try:
        limit = int(account.daily_credit_limit or 0)
    except (TypeError, ValueError):
        return None
    if limit <= 0:
        return None
    used = account.daily_credits_today
    if used is None:
        return None
    try:
        return limit - float(used)
    except (TypeError, ValueError):
        return None

# 派发循环里
head = _daily_headroom(account, cost_hint)
if head is not None and head < cost_hint:
    excluded.add(account.uid)
    continue           # 换个号，而不是发出去撞上限
```

全部账号都不过时的报错（比起让上游 402 有意义得多）：

```
402 insufficient_quota
"本日媒体额度不足：账号 5554c749 剩 12 积分、本次预估 128；
 账号 987e734d 剩 95 积分、本次预估 128。可等到本地 04:00 额度重置，或提高账号日积分上限。"
```

（`402` 沿用 `note_balance_cooled` 的口径；`daily_credits_today` 的重置时刻在 `_local_midnight`。）

**要让用户能自己控**：`media_daily_credits_per_key`（key 级每日媒体额度，默认 200）是第二道闸门，
与账号级的 `daily_credit_limit` 是**两层**：key 级防"某个客户端把整个池子烧光"，
账号级防"某个账号被烧穿"。两层都过才派发。

### 15.4 D3：一个 PR 的执行方式

一个 PR 与 CONTRIBUTING「一个 PR 一件事」是有张力的，而这条规则不是形式主义——
#712 的教训就是"20 个文件的大 PR 因主分支冲突被关闭、由维护者重做了一遍"。单 PR 的风险是：

| 风险 | 缓解 |
| --- | --- |
| 评审难度大、维护者可能要求拆分 | **PR 里按 6 个 commit 组织**，正文给出「评审顺序」，让评审可以逐 commit 看 |
| 一旦有异议，整块要改 | commit 边界 = 模块边界，`git revert` 单个 commit 仍然可回滚 |
| 与主分支冲突面大 | 提交前 rebase 到最新 main；接线点压到 `wb_proxy.py` 5 处，冲突面天然小 |
| CI 一红全红，定位慢 | 每个 commit 自己带着对应测试，`git bisect` 可用 |

**commit 序列（与正文的评审顺序一致）**：

```
1  feat(media): 媒体纯逻辑层（目录投影/body 构造/响应解析/参数校验 + 测试）
2  feat(accounts): Account.headers 增加 media 用途（+ 测试）
3  feat(media): 生图两条路由与开关（wb_proxy + wb_settings + 测试）
4  feat(media): 视频任务表与三条路由（wb_media_tasks + 测试）
5  feat(dashboard): 设置页「媒体能力」分区
6  feat(dashboard): 「媒体」页签（模型清单 + 任务列表）
```

正文里的「怎么验证」必须写清：244 → 246+ 套件全绿、三条腿 CI、以及**冒烟证据**（真账号一次性
`1024x1024` 出图 + 视频 `seconds=1` 走到 `rejected` 的零成本链路——附终端输出片段）。

### 15.5 待办顺序（更新）

```
第 0 步  开 issue 探口风（问要不要、单 PR 能不能接、edits 是否需 multipart）
第 1 步  P0 取证：scripts/probe_media.py（花 ~5.71 积分，定 media_identity 默认值）
第 2 步  写 6 个 commit 的代码 + 测试
第 3 步  本地冒烟（8799 实例 + 真账号）
第 4 步  提 PR（中英双语五段式正文 + 评审顺序 + 冒烟证据）
```

第 0 步仍然建议做：issue 被否的话，同样是这 6 个 commit，改成堆在自己分支上（路线 A），
代码一行都不用改——这正是把接线点压到 5 处的好处。


## 16. 参考素材：字段形状是实测出来的，不是猜的

2026-11-01 晚补。此前网关只映射单个首帧 `image_url`，**误以为云端不支持参考图**——
实际上 `minimax-video-h3` 的目录声明是 `maxReferenceImages: 9`、`maxReferenceVideos: 3`、
`maxReferenceAudios: 3`、`maxReferenceMaterials: 12`、`allowMixedReferenceMaterials: true`。
能力一直都在，缺的是网关侧的映射。

### 16.1 字段形状

上游 `/v2/videos/generations` 的提交体（全部由零成本探针实测，见 §16.3）：

| 字段 | 类型 | 内层键 | 说明 |
| --- | --- | --- | --- |
| `image_url` | string | — | 首帧 |
| `last_image_url` | string | — | 尾帧 |
| `reference_images` | `[]*v2.ReferenceImages` | `image_url` | 参考图，最多 9 |
| `reference_videos` | `[]*v2.ReferenceVideos` | `video_url` | 参考视频，最多 3 |
| `reference_audios` | `[]*v2.ReferenceAudios` | `audio_url` | 参考音频，最多 3 |
| `reference_materials` | **不存在** | — | 传了会被静默忽略，别用 |

所以正确写法是**对象数组**，不是字符串数组：

```json
"reference_images": [{"image_url": "https://…"}, {"image_url": "https://…"}]
```

### 16.2 三条互相牵连的约束

1. **分类上限**：`maxReferenceImages` / `maxReferenceVideos` / `maxReferenceAudios`。
   值为 `0` 代表"不支持"（kling 就是 0），报错要说"不支持"而不是"最多 0 个"。
2. **合计上限**：`maxReferenceMaterials`。minimax 各类加起来是 15、合计只有 12，
   所以真的有这道闸。
3. **首尾帧与参考素材互斥**：minimax 的 `framesExclusiveWithReferenceMaterials: true`
   ——同时发等于让上游在两套控制方式之间挑。

### 16.3 怎么在零成本下验出字段名

`seconds=0` **不能**当"必定被拒"的保险：实测上游会接受它并真的建任务（这次因此误提交
了 2~3 个任务）。可靠的护栏是**一个不存在的模型名**：

- Go 的 json unmarshal 在业务校验之前执行，所以构造 `reference_images: "字符串"`
  会先抛 `cannot unmarshal string into Go struct field CreateVideoRequest.reference_images
  of type []*v2.ReferenceImages`；
- 内层键用"类型错位"识别：给候选键喂 `123`，若该键存在且是 string，报错会点名字段
  （`ReferenceImages.reference_images.image_url : string`）；键不存在则被静默忽略。
- 模型名不存在 → 最后一定被 `14407 route config` 拒掉，全程不建任务、不扣费。

探针脚本必须记录 taskId 并设花费上限，否则"探针"会变成"烧钱"。


## 17. 并发生视频：3 条并行 + 本地排队 + 账号分散

默认 `media.video_concurrency = 3`：**同时最多 3 条视频在跑**，第 4 条起在本地排队。

### 17.1 为什么排队，而不是直接全部提交

提交是异步的（上游受理就返回），所以"客户端连发 10 条"以前会**真的向上游提交 10 条**
——上游并行度不受我们控制，风控面放大，而且一旦客户端只是想试探，钱已经花了。

现在超过并发的提交在**本地**排队：

| 阶段 | 状态（内部） | 对外 `status` | 花过钱 |
| --- | --- | --- | --- |
| 排队等并发位 | `waiting` | `queued` | **否** |
| 正在提交的一瞬 | `submitting` | `queued` | 否 |
| 上游已受理、等结果 | `queued` / `running` | `queued` / `in_progress` | 是 |
| 上游拒收（4xx） | `rejected` | `failed` + `invalid_request` | 否 |
| 提交成功但重启 | `lost` | `failed` + `task_lost` | 可能已花 |

`waiting` 与 `lost` 的区分是刻意的：前者**一分没花**，可以立刻让客户端重试；后者
上游可能已经扣费，必须能看见。重启时 `waiting`/`submitting` 一律标 `failed` 并写明
"未扣费，请重新提交"，只有 `queued`/`running` 才标 `lost`。

### 17.2 调度怎么跑

- 提交器由服务层注入（`set_submitter`）：任务表只管"该不该提交"，选账号与调上游属于
  服务层。
- 触发点是 worker 的空转分支与任务结束时：`_pump()` 在**并发未满 + 有等待者**时提交
  队首，一次调用可以把空位填满。没有额外的定时线程。
- 提交失败**不重试**：标 `rejected` 交给客户端改参数，避免后台反复烧上游。
- 轮询线程数默认**等于并发数**（3）。单线程时一条慢轮询会拖住其它条，看起来像
  "并发没生效"——这是最容易误判的一点。

### 17.3 账号分散

`store.busy_accounts()` 报出正在跑任务的账号 uid，提交时优先选**不在这个集合里**的账号；
全都在跑才退一步复用（排队优于拒单）。这样 3 条并行会落在 3 个不同账号上，避免同一
账号同时跑多条放大风控面。

### 17.4 已知边界

- 排队队列**只在内存里**（任务体不入盘），进程重启即作废——但没花过钱，代价只是
  客户端重发。
- 并发数是全局的，不是按 Key 的。多 Key 部署下先到先得，公平性没做（也没有证据表明
  需要）。
- `?wait=1` 只在同步提交成功时有意义；排队中的任务会立刻返回 202 + `queue_position`，
  客户端应轮询 `GET /v1/videos/{id}`。
