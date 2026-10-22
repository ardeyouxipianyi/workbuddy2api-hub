# Changelog / 更新记录

## Unreleased

- 新增独立 CodeArts 桥接启动器，原 Hub 文件不变，默认关闭。
- 复用 Hub 鉴权、Key 并发限制、Chat/Responses 转换与面板样式。
- 新增 CodeArts 模型命名空间、管理页面和独立统计归属。
- 新增受控 HTTP 集成测试，覆盖错误流、工具格式、权限和并发隔离。

- Add an opt-in external provider without editing existing Hub files.
- Reuse Hub authentication, key concurrency, protocol conversion and dashboard.
- Add namespaced models, authenticated provider management and usage attribution.
- Exercise HTTP/SSE errors, tool schemas and authorization through wire tests.
