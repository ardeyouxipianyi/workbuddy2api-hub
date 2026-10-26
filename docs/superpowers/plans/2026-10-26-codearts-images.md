# CodeArts 图片输入实施计划 / Image input plan

用户已确认图片优先和加入账号目录明确支持图片的模型。保持原生文件与上游一致。
User approved image-first support plus currently declared image models, preserving native upstream files.

- [x] 检查嵌套 Python 参考实现和现有 Go 请求链，定位 flattenContent 丢图。
- [x] Go overlay 保留图文顺序/detail，规范图片指纹，三态能力元数据与真实 ID 对齐。
- [x] 默认关闭的 catalog_include_images 配置，保留既有精确目录选择并去除真实 ID 重复。
- [x] 原生面板图片 URL/上传/detail 控件，明确媒体边界和错误。
- [x] 回归覆盖 Chat/Responses/Messages、工具输出、别名碰撞和延迟读取跨后端。
- [x] Go/Python/Node/原生测试，最小真实华为图片请求和 WorkBuddy 原链路验证。
- [x] 独立审查，修复所有 Important 项。
- [ ] 用户真实页面验收（工具访问被拒绝，不能绕过）。
- [ ] 满足门禁后提交合并、干净检出检查、推送 fork 对应分支并回读远端 SHA。

Reference research: nested Python preserves full messages and reads
model_parameters.supports_images; it is an AK/SK Flask implementation, not a drop-in
replacement for the OAuth Go service. Keep existing auth/pool/check-in behavior.
Official client guidance: https://support.huaweicloud.com/intl/zh-cn/usermanual-codeartsagent/codeartsagent_ug_0026.html
Client UI limits differ by mode; this bridge independently uses a 4 MiB upload cap.
No audio/vision capability is inferred from a name or an old cache.
