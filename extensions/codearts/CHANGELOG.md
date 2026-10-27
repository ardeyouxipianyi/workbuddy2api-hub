# Changelog / 更新记录

## Unreleased

- 新增「本地积分扣除」opt-in（默认关）：按 CodeArts 模型选择器公布的倍率给 `codeart/*` 回合本地计费（`积分 = tokens ÷ 1000 × 生效倍率 × 基准单价`），每个 Hub API Key 一份余额；夜间窗口 `23:00-08:00` 内 DeepSeek-V4.1-Flash 由 `0.18` 降为 `0.10`，其余模型维持白天倍率；`credits_enforce` 打开时余额 ≤ 0 的 Key 在转发前收到 402。
- 新增面板「积分 / Credits」区（倍率表、模型行倍率、余额、调账、最近 30 条扣减/调账）、`GET /codearts/credits`、`POST /codearts/credits/grant`、6 个 `credits_*` 配置键，以及 `accounts/codearts/credits.json`（原子重写，损坏时改名保留）与 `credits-events.jsonl`（追加，8 MiB 轮转）；新增 `tests/test_credits.py`（38 项）并扩展 `tests/test_dashboard.js`。
- Add an opt-in local credit ledger for `codeart/*` turns (off by default): priced on the multipliers the CodeArts model picker publishes (`credits = tokens / 1000 x rate x base price`) with one balance per Hub API key; inside the `23:00-08:00` night window DeepSeek-V4.1-Flash drops from `0.18` to `0.10`; with `credits_enforce` on, a key at zero balance is refused with 402 before the backend is contacted.
- Add the panel's credits section (rate table, per-model rate, balances, adjustments, last 30 events), `GET /codearts/credits`, `POST /codearts/credits/grant`, six `credits_*` settings, the local `credits.json` (atomic rewrite, renamed instead of overwritten when corrupt) and `credits-events.jsonl` (append-only, rotated once at 8 MiB), covered by 38 new unit tests plus dashboard assertions.

- 新增「CodeArts IDE 出站头伪装」构建期 opt-in（默认关）：官方静态身份头 7 项、逐请求 `x-ot-*`/`user-msg-id`/`created-time`/`model-*`/`client-ip` 头族、32 位 hex trace id 与可选 `timestamp`，全部在 `signRequest` 之前注入，`Content-Type`/`Accept`/`User-Agent` 改为签名后补回。
- 新增 7 个 `identity_ide_*` 配置键与 `CODEARTS_IDE_HEADERS`/`CODEARTS_IDE_SESSION_ID` 构建期环境变量，`start-codearts-bridge.py --codearts-source` 已接线；面板暂未提供控件，外部预编译后端需重新构建。
- Opt-in build-time CodeArts IDE outbound header spoofing (off by default): seven static identity headers, the per-request `x-ot-*` / `user-msg-id` / `created-time` / `model-*` / `client-ip` family, 32-hex trace ids and an optional `timestamp`, all written before `signRequest` while `Content-Type` / `Accept` / `User-Agent` are re-added after signing.
- Seven new `identity_ide_*` settings plus `CODEARTS_IDE_HEADERS` / `CODEARTS_IDE_SESSION_ID` build-time environment variables, wired into `start-codearts-bridge.py --codearts-source`; the panel has no controls yet and an external prebuilt backend must be rebuilt.

- 修复图片预检错误仅显示在页顶、测试区仍显示未发送的问题；目录 false 改为通道声明提示，允许实测，模型不被替换。
- 将上游精确 `InferHub.001001020.406` 归为请求不匹配，避免图片拒绝误冷却整个账号。
- Display preflight/upstream errors in test results. Treat catalog false as advisory and preserve the requested model.
- Classify the precise non-multimodal refusal as an invalid request without account rotation or cooldown.

- 合并上游 v1.8.0（4f7ae6f），原生文件保留上游内容；扩展兼容新增心跳，CodeArts Messages 流失败仍返回 terminal error。
- 修复 Go 后端丢弃图片块；保留 detail 和图片会话指纹。加入实时能力元数据、默认关闭的图片模型目录开关和图片测试控件。
- Merge upstream v1.8.0 without changing native files. Preserve terminal Messages errors after heartbeat shutdown for CodeArts only.
- Retain image blocks/detail in the Go build overlay, expose declared capabilities and add opt-in image models plus upload/URL test controls.

- CodeArts 页面统一原生主题、状态卡、工具栏、表格与折叠设置，保留刷新时的编辑及测试内容。
- Reuse native dashboard components; preserve unsaved settings and test results across refreshes.

- 修复后端将 GLM-5.3 展示名当作真实 ID 的问题，通过构建 overlay 保留原源码。
- 按账号目录解析唯一别名，后台探测和福利路由使用同一真实 ID，避免错误判为不可用。
- Fix backend display-name/model-ID confusion with source-preserving Go overlays.
- Resolve unique aliases per account; use the same ID for requests, probes and benefit routing.

- 支持精确筛选模型目录，排除未选择的别名与兜底项，不改写出站模型名称。
- Support exact catalog selection without changing outbound model IDs.

- 修复华为登录按钮：先同步打开窗口再等待授权地址，弹窗被拦截时保留可点击链接。
- 统一客户端入口到 Hub 默认 8788；原模型表及智能体选择器加入 `codeart/` 模型。
- `codeart/` 只用于网关，转发前还原真实模型 ID；旧 `codearts/` 请求保持兼容。
- 增加显式启用的本地后端构建/托管、就绪检查和自建子进程退出联动。

- Open the Huawei login window before awaiting OAuth; retain a fallback link.
- Share the default 8788 gateway and add `codeart/` models to native pickers.
- Strip only the routing prefix before forwarding; preserve the upstream model ID.
- Add opt-in backend build/management, readiness checks and owned-child cleanup.

- 新增独立 CodeArts 桥接启动器，原 Hub 文件不变，默认关闭。
- 复用 Hub 鉴权、Key 并发限制、Chat/Responses 转换与面板样式。
- 新增 CodeArts 模型命名空间、管理页面和独立统计归属。
- 新增受控 HTTP 集成测试，覆盖错误流、工具格式、权限和并发隔离。

- Add an opt-in external provider without editing existing Hub files.
- Reuse Hub authentication, key concurrency, protocol conversion and dashboard.
- Add namespaced models, authenticated provider management and usage attribution.
- Exercise HTTP/SSE errors, tool schemas and authorization through wire tests.
