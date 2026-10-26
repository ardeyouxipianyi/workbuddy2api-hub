# CodeArts 图片输入（已获用户确认）

保留原 7 个模型，新增当前账号目录明确声明 supports_images=true 的模型。未知能力不从名称推断；历史 models-cache 仅作实现参考。默认不自动扩大旧安装的目录，新增 catalog_include_images 开关。

客户端统一使用 Hub /v1 和 codeart/ 路由。Go 构建 overlay 保留图片内容与 detail，既有源文件不写入。扩展提供图片 URL / 本地图片测试，限制 JPEG/PNG/WebP/GIF 和 4 MiB 文件。图片正文不写测试报告。音频、视频输入和生成显式拒绝，避免静默丢弃。

参考嵌套 Python server.py 的 model_parameters.supports_images 及完整请求转发，继续使用现有 Go OAuth/账号池/签到，不复制 AK/SK 服务或历史模型能力猜测。

验证：Go 解析到 upstream 序列化契约；Hub Chat/Responses/Messages 结构传递与错误；Node 控件逻辑；原生全套；当前账号目录和合成图片真实调用。真实浏览器验收仍需用户完成，受已有浏览器访问策略限制。
