# Changelog / 更新记录

## Unreleased

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
