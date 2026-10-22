# Changelog / 更新记录

## Unreleased

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
