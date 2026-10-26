# CodeArts 外置桥接 / External CodeArts bridge

在保留 Hub 原文件的前提下，复用 Hub 的面板登录、API Key、并发控制、
Chat/Responses 转换与请求日志，接入独立的 CodeArts2API 服务。

The dedicated launcher adds a provider inside its own process. The original
Hub launcher and all pre-existing tracked files remain unchanged.

## 启动 / Start

客户端统一使用 **`http://127.0.0.1:8788/v1`** 和原 Hub API Key。
CodeArts 的 7866 只是环回后端端口，客户端切换模型不需要切换端口。
普通 Hub 启动器不加载扩展，统一入口需要运行本扩展启动器。

已有可用后端时：

```powershell
python start-codearts-bridge.py
```

面板默认地址 `http://127.0.0.1:8788/`。在 CodeArts 页填写后端地址和
`CA2A_API_KEY` 对应的服务密钥，启用并保存，然后点击 **登录华为账号**。
按钮会立即打开授权窗口；弹窗被拦截时可点击返回的授权链接。
在华为页面完成登录后，回面板点击 **检查登录结果**、**刷新状态**。

还没有部署后端时，可从自己的 CodeArts2API 源码构建并托管：

```powershell
python start-codearts-bridge.py --codearts-source ..\codearts2api-main
```

构建需要 Go（版本以该源码 `go.mod` 为准）；可用 `--go-executable <go路径>`。
构建会以 Go overlay 修正后端模型 ID 解析：使用账号目录的真实 `model_id`，
展示名仅作为同账号内的唯一别名。原始源码不被写入；不识别的源码结构会明确失败。
后台探测与真实请求使用同一解析结果，福利头也按真实 ID 判断。
已构建的旧后端二进制需从源码重新构建一次，才会包含此修复。
有已构建的程序时无需 Go：

```powershell
python start-codearts-bridge.py --codearts-backend <CodeArts2API程序路径>
```

托管模式显式启用桥接，自动生成独立密钥、仅监听 `127.0.0.1:7866`，
就绪检查成功后才保存连接。凭证和二进制放在桥接配置旁的 `backend/`。
`--codearts-backend-port` 只改变内部端口。网关退出时只关闭自己启动的后端；
已有端口服务必须通过身份和密钥验证才能复用，复用服务不会被关闭。
Windows 使用 Job Object 保证网关被直接终止时也能回收自建后端。

Windows/Linux 启动脚本仍支持原 Hub 的 `--host`、`--accounts-dir`、
`--usage-dir`、`--api-key` 等参数。**8788 已有运行中的服务时不要直接停它**，
先用 `--port 8799` 隔离验收，确认切换时机后再用扩展实例替换原端口服务。
启动器不会把扩展热注入已有进程，也不会自动停止占用端口的进程。

Clients share the original Hub gateway and keys. The default client endpoint is
`http://127.0.0.1:8788/v1`; 7866 is an internal loopback service only. An external
backend can be configured in the panel. Alternatively, explicitly opt into local
management with `--codearts-source` (requires Go) or `--codearts-backend` (compiled
executable). This generates a private backend key and enables the bridge only
after readiness succeeds. Only owned children are stopped on gateway exit.
No Huawei credentials or backend executable are included in the repository.

## 配置与密钥 / Configuration and secrets

`catalog_models` 可配置精确的后端模型 ID 列表，空列表默认展示全部。该项只筛选
模型目录（含原生选择器），不改变真实请求模型名称或替代 API Key 的权限规则。
例如 `["GLM-5.2", "Kimi-K3"]` 不会额外展示小写别名或静态福利项。
显式选择但后端未列出的名称仍保留，标记 `backend_listed=false`，不补造参数或保证可用。
`catalog_models` optionally filters the catalog by exact upstream IDs. Empty means
all models; request routing and key authorization remain unchanged.
Selected IDs missing from the backend remain explicitly marked as not listed;
no parameters or availability claims are invented.

- 默认关闭。普通 Hub 启动器完全不会加载此扩展。
- 默认配置文件为 `accounts/codearts/bridge.json`，该目录自带忽略规则。
  `--accounts-dir` 改变时，配置随账号目录移动。
- 可用 `--codearts-config <path>` 指定独立配置；自定义位置需要自行保护、忽略。
- 示例在 `extensions/codearts/config.example.json`。新配置无需先手工复制，面板保存
  会原子创建文件。文件权限使用操作系统当前用户目录权限；建议存放在专用私有目录。
- 后端密钥仅保存在服务端。浏览器只能看到“已设置”；密钥输入留空表示保留原值。
  清除保存值可向配置接口发送 `{"clear_api_key":true}`；环境变量仍需在启动环境中清除。
- **客户端模型使用 `codeart/<模型 ID>`**，例如 `codeart/GLM-5.2`。
  前缀仅用于网关路由；转发前去除，后端收到的仍是原始 `GLM-5.2`，大小写和剩余内容不变。
  旧版 `codearts/` 请求仍兼容，新模型清单只返回 `codeart/`。
  真实模型以服务返回为准；不在代码中复制一份静态华为目录。
- 禁止自动跟随后端重定向，避免服务密钥被发送到其他地址。

The `codeart/` prefix is removed before forwarding: the original upstream model
ID is preserved exactly. Old `codearts/` request names remain accepted.

The backend key never appears in settings responses or dashboard source. Use the
same server-side secret as CodeArts2API, not a Huawei access/refresh token. Keep
custom configuration paths out of Git. Original Hub API keys remain client keys.

## 客户端 / Clients

使用此扩展实例的 `/v1` 地址和 Hub API Key，模型增加 `codeart/` 前缀。
支持 Chat Completions 流式与非流式，以及原 Hub 的 Responses 转换。
Responses 工具声明和命名 tool_choice 在出站时转为 CodeArts 需要的 Chat 格式。
工具接口使用现代 `tools` / `tool_choice`；旧客户端的 `functions` /
`function_call` 别名不在本扩展的兼容范围内。
Anthropic Messages 沿用 Hub 自身的默认关闭开关与现有能力边界。

```json
{
  "model": "codeart/GLM-5.2",
  "messages": [{"role": "user", "content": "Reply with OK."}],
  "stream": false
}
```

`GET /v1/models` 将可访问的 CodeArts 模型加入原模型列表；
`GET /v1/models?realm=codearts` 只获取 CodeArts 模型。
面板国内版/国际版视图的模型表和智能体配置选择器也列出 CodeArts 模型，便于统一切换。
普通 API 客户端显式选择 WorkBuddy 区域时仍只列相应区域模型。
CodeArts 后端失败不会把其模型调用转发给 WorkBuddy。

Use an unbound Hub key, optionally limited to `codeart/*` in the native model
allowlist. A key bound to WorkBuddy's international/domestic realm, a key with
WorkBuddy account restrictions, or a `/u/<account>/...` pinned route is refused
for CodeArts. This bridge addresses the CodeArts **service pool**; it cannot
enforce per-Huawei-account restrictions inside that separate service.

显式会话 ID 会按 Hub Key 派生独立后端会话标识。独立用户或客户端应使用独立 Key，
并为不同会话提供不同 `conversation_id` 或客户端会话头；省略时沿用 CodeArts 服务
自身的会话策略。不要将不同租户放在同一个不带会话标识的共享客户端中。

## 面板 / Dashboard

CodeArts 页沿用原生状态卡、分组工具栏、账号/模型表格、折叠区块与主题变量。
账号和模型在首屏，连接配置与请求测试可展开；刷新状态保留未保存的配置和测试输入/结果。
语言切换在页面内同步更新，不发送模型请求。模型的“已列出”仅表示后端目录存在，
不代表已完成实时连通性测试；上下文/最大输出仅在接口提供数值时显示。

The provider page reuses native cards, toolbars, tables and theme tokens. Settings
and request testing are collapsible. Refresh and language changes retain edits
and test results. Catalog membership does not imply a successful inference.

## 账号与统计 / Accounts and usage

- 登录华为账号、检查登录结果、签到、续期、启停、重载调用 CodeArts 的管理接口。
  浏览器需要有效 Hub 面板会话；普通 API Key 不能修改桥接配置或操作华为账号。
- OAuth 授权链接由后端生成。若后端与浏览器不在同一台机器，需按 CodeArts2API 的
  部署说明设置可访问的 OAuth 回调地址；桥接不会自动改写后端 OAuth 配置。
- 华为凭证、DPoP 私钥、刷新 Token 以及内部账号轮转均由独立后端保存和处理。
  不导入 WorkBuddy 账号池，也不伪造华为积分余额。
- 继续使用 Hub `usage.jsonl`；CodeArts 记录含 `provider=codearts`、`realm=codearts`
  和 `upstream_model`，不会归到当前 WorkBuddy 区域。
- CodeArts 页面的请求/Token 汇总基于最近 5000 条网关日志，并明确显示采样范围。
  WorkBuddy 国内/国际统计仍按原区域筛选；原图表不是完整的三供应商管理系统。
- 上游 SSE 错误和提前断流走 Hub 的失败处理，不能将空回复计为完成。

## 验证 / Verification

```powershell
python -m unittest discover -s extensions/codearts/tests -v
python extensions/codearts/tests/run_backend_tests.py --source ../codearts2api-main --go go
python tests/run_all.py --jobs 4
python scripts/check_clean_checkout.py
node --check extensions/codearts/dashboard.js
node extensions/codearts/tests/test_login.js
node extensions/codearts/tests/test_dashboard.js
```

新增测试位于扩展目录，独立运行；原测试清单和套件数量未修改。
测试使用环回 HTTP 服务验证协议和权限，不证明真实华为模型可用。
实际验收需在面板登录真实账号后调用，并单独记录结果。

本地验证记录见 `VERIFICATION.md`。这份文件区分受控后端、真实 WorkBuddy 和
真实 Huawei 结果，未测项目不会写通过。

## 更新与回滚 / Updates and rollback

扩展使用 Hub 内部接缝，启动时检查必需接口。升级 Hub 后需重跑上述测试；
检查不是对所有未来版本的兼容保证。

回滚时关闭扩展实例，使用原 Hub 启动脚本。配置中关闭 CodeArts 只停用该供应商，
不会删除凭证。运行中的原服务及客户端全局配置不会被扩展启动器自动修改。

All source changes are additive. Returning to the original launcher removes the
extension from the process; no original source restoration is required.


## 图片输入与能力目录 / Images and capability catalog

连接设置 → 高级设置可启用「加入目录确认支持图片的模型」
（`catalog_include_images`，默认 `false`）。在保留 `catalog_models` 原选择的基础上，
仅加入实时后端目录返回 `supports_images: true` 的真实 ID，并去除展示名/大小写别名重复。
缺失或相互冲突的能力显示为未知；静态种子、模型名字和历史缓存不会作为支持依据。
`supports_images: false` 显示为「目录未声明图片支持」，仍允许发送图片实测，不据此否定模型本身的多模态能力。目录声明与实际调用结果分别验证。

请求测试可选图片 URL 或单张本地图片，并指定 `detail: auto | low | high`。
允许 PNG/JPEG/WebP/GIF，上传上限 4 MiB；这属于桥接的输入限制，并非华为所有客户端的统一限制。
格式和 detail 是否被具体模型接受，以实际返回为准。桥接本身不下载 URL；图片随消息发送给后端/华为。
URL 与文件不可同时选用，文件和图片数据 URL 不保存到设置或验证报告。

Chat 使用标准 `image_url` 内容块；Responses 的 `input_image` 保留 detail。
Anthropic Messages 在原生开关启用时可沿用图片转换（其 image source 没有 detail 参数）。
现有 Go 后端必须通过本扩展重新构建：旧二进制会将图片块拍平为文本，单更新 Hub 不足以修复。
Go overlay 保留图片与文本顺序，并将图片纳入会话指纹，防止不同图片复用同一上下文。
普通文本请求保持既有行为，`codeart/` 不进入真实模型请求名称。

```json
{
  "model": "codeart/<当前目录确认支持图片的真实模型ID>",
  "messages": [{"role": "user", "content": [
    {"type": "text", "text": "这张图片是什么颜色？"},
    {"type": "image_url", "image_url": {"url": "https://example.com/image.png", "detail": "auto"}}
  ]}],
  "stream": false
}
```

音频/视频输入、附件和音频/图片生成不属于本次支持范围；不支持的媒体输入或音频输出请求会明确拒绝。
华为客户端的语音输入可以先转文字，不等同于本桥接支持音频模型请求。
参考：[华为多模态输入文档](https://support.huaweicloud.com/intl/zh-cn/usermanual-codeartsagent/codeartsagent_ug_0026.html)。

Enable `catalog_include_images` (default false) in advanced settings to append only
models explicitly declared image capable by the current backend catalog. Existing
selected IDs remain. Unknown/conflicting metadata stays unknown; aliases are deduplicated
by registered ID. The image test accepts a URL or one local file, up to 4 MiB, with
`auto/low/high` detail. Rebuild the Go backend with this extension to retain image blocks.
Chat and Responses preserve detail; native opt-in Messages supports its image source
conversion. Audio/video/files and generation are outside this image-input feature.
Only PNG has been checked against live models; other accepted file formats still depend
on the upstream model. The bridge does not download image URLs itself.


### 目录声明与实际通道 / Declaration versus route behavior

模型原厂能力、华为 CodeArts 目录标志、当前账号端点的真实调用结果可能不同。
图片测试不会仅因目录 false 而拦截或偷偷切换模型。当前通道若返回
`InferHub.001001020.406 The request model is not multimodal`，面板显示其错误，
后端将其归入请求不匹配（HTTP 400），不轮换或冷却整个账号；普通服务故障处理不变。
模型原厂多模态能力参考：[DeepSeek V4.1 Flash](https://api-docs.deepseek.com/zh-cn/news/news260910/)、
[Kimi K3](https://github.com/MoonshotAI/Kimi-K3)。这不构成该华为端点已开放相同能力的保证。

Vendor capability, catalog metadata and actual account-route behavior are separate evidence.
A false catalog flag is advisory: image tests remain possible and never rewrite the selected
model. The precise upstream non-multimodal refusal code is treated as an invalid request,
not an account outage. Validation and upstream errors appear next to the test controls.
