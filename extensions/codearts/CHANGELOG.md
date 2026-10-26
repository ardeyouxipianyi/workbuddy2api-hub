# Changelog / 更新记录

## Unreleased

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
