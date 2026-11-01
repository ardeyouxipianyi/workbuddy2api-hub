## v1.10.0

- **CodeArts 原生第三出口（单进程）**（#712 / #715）：新增 `codearts` 原生出口，看板可在「国际版 / 国内版 / CodeArts」三区域间自由切换；`/v1/models`、`/v1/chat/completions`、`/v1/responses` 与 Anthropic Messages 均直连华为云 CodeArts。进程内完整实现 OAuth2 PKCE + DPoP、token 自动续期轮转、账号池并发与冷却管理、内置与限时福利模型发现、聊天 SSE 协议转换与每日签到/积分查询，不再需要单独运行 codearts2api 服务；华为凭据不出本机，默认关闭。
- **CodeArts 账号代理槽与统一出站连接池**（#712 / #715）：CodeArts 账号复用与 WorkBuddy 一致的出口代理槽，支持连接复用、代理认证及账号级出口独立绑定，槽位变更自动跟随，失效自动回退直连。
- **CodeArts 权限安全与首帧增强**（#715）：修复 CodeArts 文本流首帧规范化转换；对暂不支持的视觉/图片输入主动拦截并回退 400 明确提示；严格校验 API Key 的模型禁用、白名单与账号绑定范围；OAuth 回调严格校验会话标识并限制跳转安全。
- **Key 允许账号选择器展示与搜索备注**（#708 / #717）：API Key 编辑弹窗内的允许账号选择器中补充账号备注展示，并支持在搜索框内直接按备注筛选，便于大规模账号池精细化权限配置。
- **今日活动状态区分「无本地记录」**（#706 / #716）：网关当天缺少加油站或成长计划活动流水时，状态胶囊精准展示为「无本地记录」，避免外部打卡已被上游记录却被本地误报为「未签到」。
- **单请求体上限面板动态调节**（#709 / #714）：设置页支持将 HTTP 请求体限制在 1–256 MB 范围内动态热配置并立即生效；超限返回清晰的 413 状态并提示当前限额；超限分块传输自动提前熔断，避免无谓占用上行带宽。
- **修复设置页出现两张「上游响应头超时」卡片**（#710）：清除设置页重复的 DOM 节点，解决第二张卡片数据未回填以及点击保存时读取首张卡片数值的逻辑串扰。
- **看板「最近请求」的时间补上日期**（#700 / #713）：数据看板的实时请求流水列表在跨天时智能显示 `MM-DD HH:MM`，当日请求保持 `HH:MM:SS`，时间戳缺失显示占位符号 `—`。

验证：
- 本地完整测试矩阵：244 个套件全部通过（181 Python + 63 JS，`244 passed, 0 failed, 0 skipped`）。
- GitHub Actions 跨平台 CI（Ubuntu 3.9 / Ubuntu 3.12 / Windows 3.12 / revert guard）全部通过。
- 发布工程套件 `_test_release_engineering.py` 与发布资产套件 `_test_release_assets.py` 全部通过。

## v1.9.2

- 数据看板在同一时间范围的请求尚未完成时跳过重复轮询，避免慢请求堆积（#656）；首次冷计算仍需扫描历史数据。
- 更新卡片直接显示安装按钮不可用的原因，以及可安装时的操作提示（#696）。
- 面板密码加密组件不可用时显示环境错误（#658）；启动时指定的面板密码无法设置则退出，避免意外启用旧密码或默认密码。

## v1.9.1

- **SSE 终态停止读取**（#692）：设置 `WB_STOP_AT_SSE_DONE=1` 后，在聚合、Responses 和 Chat 路径遇到 `[DONE]` 时停止读取；默认关闭。
- **HTTP 错误正文缓存**（#693）：设置 `WB_CACHE_HTTP_ERROR_BODY=1` 后缓存首次读取的错误正文，供后续读取；缓存最多保留首次读取的字节数，默认关闭。

验证：完整测试套件 233 通过；隔离面板登录、控制台和真实上游请求通过。

## v1.9.0

本版汇总 `v1.8.0` 之后的十个社区 PR，修复 APISIX 401 挑战头判定、DSH 11128 渠道指纹、收费模型 6004 跨重启持久化、池空 503/429 报错精准归因与用量行对齐、/health 区域与模型细分、零费率模型保留额度豁免、国际版每日活跃 11140 风控持久化避让，以及看板下拉与活动记录窄屏布局优化。套件 231 个（172 Python + 59 JS）。

- **认网关的 `WWW-Authenticate` 判死 401，面板测试不再把「凭证真失效」显示成一页代理报错**（#686）：国内版上游是 APISIX，凭证被拒时回的是 `<html>401 Authorization Required</html>`，死因只在响应头 `WWW-Authenticate: Bearer realm="copilot", error="invalid_token"` 里。#435 为解决「中间代理页把健康号踢出池子」把所有带 `<html` 的 401 一律豁免，导致真失效的账号未被标记；现在 `http_error_message()` 将挑战头置前展示，`_auth_failure_text()` 在 HTML 豁免前识别 RFC 6750 的 `invalid_token`，真失效账号及时退出派单。新增 `tests/_test_token_expired.py` 断言。
- **中和 DSH 文件引用段 11-128 渠道指纹**（#684 / #680 / #688 / #677 / #675）：DSH（DeepSeek Harness）官方插件 `@deepseek-ai/dsh-file-reference` 注入的 `FILE_REFERENCE_PROMPT` 被上游识别为非官方客户端特征并返回 400 / code 11128。通过对首句增加定语关系代词 `that` 进行微调，在保持模型语义等价与幂等的前提下打断连续文本匹配。新增 `tests/_test_sanitize_fingerprint.py` 回归覆盖。
- **国内版收费模型撞 6004 也保住"配额耗尽"这个事实**（#668 / #672）：国内版计费模型（如 `deepseek-v4.1-flash`）撞上游 6004 配额耗尽时，将 `cappedAt` 连同窗口信息落盘至 `accounts/model_windows.json`，重启后由 `restore_window_cooldowns()` 挂回，面板不再误判为短时 429 临时限流。新增 `tests/_test_cap_mark_any_model.py`。
- **池空报错说清"在等谁、等多久"**（#665 / #671）：API Key 绑定账号场景下，池空 503 报错精确定位绑定候选账号并输出逐账号原因；候选账号仅因等待模型冷却窗口恢复时，改回返回 429 与 `Retry-After`，避免客户端高频空转重试。新增 `tests/_test_no_usable_account_scope.py`。
- **/health 按区域与模型报可用数**（#667 / #670）：`/health` 保留原汇总字段，新增 `accounts_by_realm`、`accounts_ready_by_realm`、`accounts_ready_current_realm` 与 `models_ready`，带 Key 调用时返回专属 `key_accounts_ready` 与 `key_accounts_bound`，准确反映模型可用性。新增 `tests/_test_health_readiness.py`。
- **池空失败的状态码与用量行对齐**（#666 / #669）：统一错误出口（`upstream_failure_response()`），先确定状态码再落盘，使 `usage/usage.jsonl`、请求日志与实际给客户端返回的 503 状态码一致，消除 502/503 混淆。新增 `tests/_test_error_status_consistency.py`。
- **签到与活跃记录窄屏挤压修复**（#653）：说明列与昵称列省略号截断收敛至 `.note-clip`，中屏上限收至 300px，手机端卡片折行显示全文，短标签列（时间/区域/任务/来源/结果）禁止逐字折行，行高恢复单行 42px。
- **看板下拉不再误判全宽竖排**（#652）：自定义下拉 `enhance()` 仅认显式 `style.width: 100%`，避免 flex 子项与隐藏页签将 computed display 规范化为 block 导致 16 处下拉误判全宽竖排的问题。新增 `tests/_test_custom_select.js`。
- **保留积分不再饿死免费模型**（#650）：正数 `reserve_credits` 只拦截付费或目录未知的模型；本区域目录标为 `x0.00` 的零费率模型免除阻断，避免低额保留误伤免费模型。新增 `tests/_test_reserve_free_models.py`。
- **国际版每日活跃持久化 11140 风控限制并停止重试**（#649 / #659 / #661）：国际版桌面打卡腿遇 HTTP 403 / code 11140 时调用 `note_restricted()` 并持久化 `restrictedUntil`，限制期内 `can_daily_chat()` 返回 False，调度器自动跳过，避免重复撞墙加重风控。新增 `tests/_test_daily_chat.py` 覆盖。

验证：

- 本地完整矩阵：`231 passed, 0 failed, 0 skipped`（63s 全绿）。
- GitHub Actions CI：Ubuntu 3.9、Ubuntu 3.12、Windows 3.12 与 revert guard 均通过。
- 发布工程套件 `_test_release_engineering.py` 与发布资产套件 `_test_release_assets.py` 全部通过。

## v1.8.0

本版汇总 `v1.7.33` 之后的四个修复，并补记此前已进入发布页、但未写入仓库 CHANGELOG 的三项改动。套件 226 个（167 Python + 59 JS）。

> **维护状态（2026-10-23）**：WorkBuddy 对非官方客户端的识别与拦截持续收紧，维护者近期工作压力增大，更新与 Issue / PR 处理将显著放缓。上游可能随时彻底收紧导致不可用；账号风控 / 限流 / 封禁风险由使用者自行承担。请优先使用官方客户端，不要使用主账号或商业转售。

- **更新检查超时放宽到 10 秒**（#640）：Releases API 受限时，回退下载路径在代理出口实测需要 4.2～10 秒；旧的 5 秒上限会把本可成功的回退一起掐掉。现在两条路径各有 10 秒，失败仍按每日节流重试，不会挂住面板。
- **stderr 日志通道不再阻塞请求线程**（#642）：请求线程只向 2000 行有界队列 `put_nowait()`，由单个守护线程负责真正写入；队列满时丢行并计数，首次和每 1000 行写一条 WARN 到看板日志。退出路径用 0.5 秒超时刷队列，`sys.stderr is None`（pythonw）直接跳过。日志内容、脱敏与看板顺序不变。
- **桌面 chat 可选协议一致性**（#643）：设置进程环境 `WB_DESKTOP_CHAT_COMPAT=1` 后重启，仅桌面产品的 chat 去掉浏览器请求头，补齐 SDK runtime 元数据与 B3 parent span。默认关闭；CLI、VSCode、计费 REST、请求体、设备 token 与退避逻辑不变。参考 workbuddy2api-panel 的桌面报文对照，未独立复核原始抓包，不构成 TLS 指纹模拟或免封保证。
- **国际版每日活跃证书失败降级**（#645 / #647）：ACP 沙箱域名由 EdgeOne 下发 `*.cdn.myqcloud.com` 通配证书，Python ssl 必然报 Hostname mismatch。国际版现在也能按 `www.workbuddy.ai` 调用 `POST /v2/report`，连登查询同样按账号出口选 base；设置页新增「证书失败改走活跃上报」，**默认关闭**。仅证书类错误可降级，沙箱未就绪、轮次超时、业务拒绝仍如实失败；回退上报被拒时保留当天未完成状态供下轮重试。
- **补记 v1.7.31 发布内容**：恢复 `model_aliases` 映射并避免后台模型改写误伤显式选择（#617 / #619）；收窄 CN 专属模型判定（#618）；OpenCode 一键配置写入真实 `limit.context` / `limit.output`（#625）；Chat / Responses / Anthropic Messages 三条流式路径在真实静默期发送 SSE 心跳（#578）。

验证：

- 本地完整矩阵：`226 passed, 0 failed, 0 skipped`。
- GitHub CI：Ubuntu 3.9、Ubuntu 3.12、Windows 3.12 与 revert guard 均通过。
- 发布前补跑发布工程与资产套件、`py_compile`、`git diff --check` 与干净检出检查。
- 本机没有真实账号池，**未执行网关到 WorkBuddy 上游的真实模型冒烟**；#645 的上游证书恢复与真实账号连登 `0 -> 1` 仍需部署方复测。

## v1.7.33

四个社区 PR 的集成版本，并自本版起进入低频维护。套件 225 个（166 Python + 59 JS）。

> **维护状态（2026-10-23）**：WorkBuddy 对非官方客户端的识别与拦截持续收紧，维护者近期工作压力增大，更新与 Issue / PR 处理将显著放缓。上游可能随时彻底收紧导致不可用；账号风控 / 限流 / 封禁风险由使用者自行承担。请优先使用官方客户端，不要使用主账号或商业转售。

- **成功请求解除凭证失效标记**（#630）：`note_success()` 现在清除进程内的 `token_expired`，单账号测试、一键测试与真实派单成功后都会立即恢复面板徽章与派单资格；没有 access token 的账号仍不可派发。`tests/_test_token_expired.py` 共 9 项。
- **Responses 流式首段工具参数增量**（#632）：首个 `tool_calls` 分片携带的参数现在会在 `output_item.added` 后发出，并且只累计一次；覆盖部分 / 完整 / 空首段、交错调用和 custom 工具，避免下游拼接与最终 `arguments` 不一致。
- **每日活跃改为预算 + 有序候选序列**（#635）：旧的六个互相覆盖的旋钮收敛为单账号预算（默认 600 秒，0 = 不限）与有序模型候选（至少两轮）。时段改为到期窗口判定，新增启动补跑、网页会话卡死收尾、登记表上界与预算内退避；旧首选模型与已启用回退组合会迁移到新列表。
- **429 模型窗口跨重启恢复**（#638）：账号加载时从 `model_windows.json` 恢复未到期的模型冷却，过期窗口不挂，损坏状态文件不阻断加载；同时清理冷却起始时刻残留。
- **维护与文档**：README 顶部新增低频维护公告、上游收紧与账号责任说明，同步版本徽章、下载入口和本版记录；版本号同步到 `wb_proxy.py`、OpenWrt `PKG_VERSION` 与 README。

验证：本地 `225 passed, 0 failed, 0 skipped`。本机无真实账号池，未执行到 WorkBuddy 上游的真实模型冒烟。

## v1.7.30

15 笔社区改动：签到 / 保活 / 调度器判定修复、面板闸门与自绘下拉框、流式 chunk id、面板请求体排空、Pro Only 改由目录驱动、Codex 远程压缩、账号级并发与粘滞、静态 API Key 账号、会员档位胶囊、每日活跃会话释放。套件 222 个（165 Python + 57 JS）。

- **调度器日志与区域风控闸门改取正在运行的 wb_proxy**（#594）：网关以 `python[w] wb_proxy.py` 启动时主模块是 `__main__`，`import wb_proxy` 会再加载一份副本；副本的 `LOG_STORE` 是 None、风控暂停状态也是另一份，于是调度器日志全部掉进黑洞、巡检看到的 `hold` 恒为 0。现在先取 `__main__`，再退回已加载的模块。
- **签到业务失败不再记成成功**（#596）：`200 + code 10001 +「请求处理中，请勿重复操作」` 等明确失败回执曾被判成签到成功，现在按失败处理。
- **一键签到按任务开关选号**（#598）：批量签到只挑 `tasksEnabled` 的号，停用账号不再被无差别签到。
- **Token 保活提前量必须大于巡检间隔**（#600）：写死的 2 小时远小于 24 小时巡检间隔，约 92% 的到期时刻要等过期后才补；现在提前量默认 48 小时（`WB_KEEPALIVE_LEAD_HOURS` 可调），拿不到到期时间时也按「该续」处理。
- **下拉框改为自绘**（#601）：原生 `<select>` 的展开层由浏览器绘制，深色主题下会露出直角白框与系统蓝高亮；改为自绘列表，桌面与手机一致。
- **每日活跃建会话失败后补退避**（#605）：建会话 / 取沙箱失败只用掉几百毫秒，背靠背重试会把整批尝试挤进同一个上游抖动窗口；现在按 `phase` 判断，只在这两步失败后退避（默认 5s、指数增长、30s 封顶，设置页可改）。
- **面板闸门 fail-closed**（#606）：首帧不再闪出看板骨架，`?pwd=` 失败后也不会卡死。
- **一次流式回复只带一个 chunk id**（#607）：客户端按 chunk id 管理流状态时，多次变化的 id 会让界面卡在「生成中」；现在整轮复用同一个 id。
- **面板 POST 的空 body 会被排空**（#608）：按钮发 `{}` 后没读完请求体，同一条 keep-alive 连接上的下一个请求会 501，表现为偶发「无法连接反代」。
- **Pro Only 标注改由目录 tier 表驱动**（#609）：`entitlement` 只在真撞上游 11158 时才写，而 `ready()` 早已按 `modelTiers` 在本地拦掉 PRO-only 模型，运行时那条通道几乎不再触发；现在清单直接读目录 tier，删掉冗余通道。
- **Codex 远程压缩 v2**（#610）：长会话的压缩请求按 v2 语义处理，不再把压缩内容当普通消息透传导致上游报错。
- **账号级并发上限 + 更稳的会话粘滞键**（#612）：两个开关默认关闭；开启后限制单个上游账号同时接几个 chat 请求，并让同一会话尽量落在同一个账号。
- **静态 API Key 账号**（#620，Refs #572）：企业微信认证的 WorkBuddy / CodeBuddy CLI 账号可以只用 `Authorization: Bearer ck_...` 入池，免 refreshToken、强制流式上游、Key 脱敏落盘。
- **账号行显示会员档位胶囊**（#621）：直接显示计费摘要里的套餐名（`Free` / `体验版` / 付费包名），悬停给出读取时刻。
- **每日活跃网页会话跑完即释放**（#623）：上游对单账号活跃会话有 10 条硬上限，旧实现只建不收尾，攒满后建会话一律 429 / 10105；现在成功轮次 stop、失败轮次 stop + delete，撞上限时自动清理最旧的本网关会话。

## v1.7.29

七笔社区改动：四个手机端布局修复、启动时补一次更新检查、便携更新保留可执行位、API Key 的账号白名单与并发上限落盘。套件 210 个（157 Python + 53 JS）。

- **手机端工具栏打卡按钮靠右**（#585）：`#accountsBody .toolbar .tb-checkin` 的 CSS 在 #498 里只进了样式表，HTML 的类名被并成了 `tb-group`，规则因此悬空；补回类名，桌面端不受影响。
- **手机端用量统计副行每项一行、图表底边对齐**（#586）：窄屏 KPI 副行改用 `kline` / `kvi` / `ksep` 分组，`display:contents` 让每项独占一行并隐藏分隔符；数值与单位不断行，第一行三张卡改纵向 flex 让 sparkline 贴底等高。桌面端 1280 / 1100 / 900 / 700 / 641px 逐属性对照无变化。
- **手机端账号卡出口与权重同行**（#587）：出口下拉与权重各占半行、两个 select 统一 34px 高；积分与请求铺满整行，积分格不再把列标签挤进 `credit-meta`。
- **窄屏统计条让积分消耗独占一行**（#588）：≤860px 时把「积分消耗」与「请求成功率」对调，积分消耗占满整行，副行的估算价值不再被省略号截断。
- **启动时补一次更新检查**（#589）：`tag` 与发布页只在内存里，重启后 24 小时窗口又锚在上一次尝试上，面板会停在「有新版本但装不了」，最长等一天。后台线程第一次 pass 带 `startup=True`，绕过 24h 窗口但仍受开关与每小时预算约束（`STARTUP_CHECK_WINDOW_SECONDS` / `STARTUP_CHECKS_PER_WINDOW = 6`），预算账本随 `update_check` 落盘；稳态仍是 24 小时一次，关掉每日检查则一个请求都不发。
- **便携更新保留 unix 权限位**（#582）：`safe_extract()` 用裸 `open()` 写暂存文件，zip 里 755 的 `start-wb-proxy*.sh` 落盘变 644，重启那步 `Popen()` 报 EACCES 并回滚——macOS / Linux 的便携更新每次都会命中。现在按 zip 条目的 `external_attr` 还原模式，无 unix 属性不加执行位，setuid / sticky 不还原，Windows 不受影响。
- **API Key 的账号白名单与并发上限能落盘了**（#592）：`_handle_settings_save` 重建 key 条目时漏了 `accounts` 与 `concurrency_limit`，`set_api_keys()` 读不到就回落成 `[]` / 0，面板勾选的账号保存后不生效。现在与 `realm` / `models` / `name` / `enabled` 同一规则：补丁没提到就沿用已存值。

## v1.7.28

面板「操作失败: no such account」的根因修复（#581）：手机号脱敏把 uid 改写成了池子里不存在的值。套件 209 个（156 Python + 53 JS）。

- **uid 不再被脱敏改写**（#581）：`_redact_account_names()` 对所有字符串值跑手机号文本正则，国内号 uid `6ef20bf4-76fa-4085-b179-97517476cc1e` 里的 `179-97517476` 恰好满足 3-4-4 分组，被改成 `…b179****7476cc1e`。`/accounts` 把这个假 uid 返回给浏览器，前端点「停用账号 / 测试」时回传，`AccountPool.get()` 查不到 → 404 `no such account` → 「操作失败」。现在 `uid` / `account` / `accountId` / `id` / `file` / `path` / `machineId` / `sessionId` / `key` / `token` 这些标识符键一律原样返回；`nickname` 的脱敏不变。
- **文本级边界收紧**：`_PHONE_IN_TEXT` 的左右边界从「不是数字」扩到「不是标识符字符」（字母 / 数字 / 下划线），日志或摘要串里嵌着的十六进制 id 不会再被切一段出来当号码。
- **测试**：`tests/_test_nickname_privacy.py` 新增 3 项，覆盖该 uid 原样、标识符字段不被改写、同载荷里的昵称仍然脱敏。

## v1.7.27

账号档位改为读计费摘要（#505）：付费 / 免费不再靠猜，`is_paid_user` 有了权威来源。套件 209 个（156 Python + 53 JS）。

- **档位来自计费摘要**（#505）：`/billing/meter/get-user-resource-summary` 的 `IsPaidUser` / `SubscriptionPackageName` / `IsProtectedPriceUser` / `ProTrialStatus` 解析成账号上的 `tier` 记录并落盘。国内版的详细积分路径本来就在打这个接口，直接复用同一份响应，零新增请求；国际版的 `get-user-resource` 没有档位字段，因此每账号每天最多多一次摘要请求（`TIER_TTL_SECONDS` = 24h）。
- **失败不写结论**：401 / 非 0 code / 网络错误保留旧值与时间戳，绝不把一次失败写成「免费」；企业账号（上游语义未验证）与风控限制中的账号跳过。`tier` 按 `VOLATILE_FIELDS` 处理，导入到另一台机器时不沿用上一台的结论。
- **接线**：`fetch_credits()` 成功后顺带刷新档位，不新开定时器；`is_paid_user` 优先取 `tier`，没有结论时回落到上一次积分快照（与旧行为一致）。面板「会员类型」旁显示包名与读取时刻，hover 给出摘要时间。
- **测试**：新增 `tests/_test_account_tier.py`（19 项），覆盖解析、缺字段不当免费、企业 / 限制跳过、一天一次、国际版余额取档位、落盘与导入。

## v1.7.26

两个已确认的修复：assistant 历史里的身份句 / 品牌词指纹，以及镜像部署升级写进便携包专用文件。套件 208 个（155 Python + 53 JS）。

- **11-128：assistant 历史也清洗，品牌词缩成 `DeepSeek`**（#574）：同一句 `You are an AI agent powered by DeepSeek Harness.` 放在 assistant 里 400/400/400、放在 user 里 200/200/200，说明身份句的作用域漏了 assistant；`DeepSeek Harness` 与 GUI 描述句（`this page` / `this GUI` / `this app`）连续出现时仍会命中，单独品牌词放行。现在 `sanitize_messages()` 的 identity 作用域扩到 system / developer / assistant，并在这一层把散文里的 `DeepSeek[ \t]+Harness` 缩成 `DeepSeek`。连字符 / 下划线形式（`deepseek-harness`、`deepseek_harness`、`/opt/deepseek-harness/app`）与 user / tool 内容保持原样。新增 5 项断言。
- **镜像 / rootfs 部署升级不再写便携包专用文件**（#573）：release manifest 新增 `profiles.app`（`wb_*.py` + `dashboard.html`）。非便携包布局（清单与代码同层）的 `request.json` 会带上 `profile: "app"`，执行者据此只替换应用层：`start-wb-proxy*`、`ops/aliyun/*`、`README.md`、`LICENSE` 与 `pricing/pricing.json` 不再落到 `/app`。便携包布局（清单在包根、代码在 `wb-proxy/`）不带档位，行为不变；旧执行者忽略该字段，行为也不变。备份把本次实际管理的 `files` 记进 `backup.json`，按档位备份的目录仍能通过完整性校验并回退。新增 7 项 Python 断言与 3 项 manifest 断言。

## v1.7.25

四笔社区改动：DSH GUI 指纹、跨出口回退、账号表排序续行、更新备份与版本回退。套件 208 个（155 Python + 53 JS）。

- **11-128：DSH GUI 描述段的句尾语序**（#579）：国内版 system 提示里的 GUI 描述段 `… the DeepSeek Harness Web GUI whose URL the runtime context names.` 紧跟 `When the user refers to "this page" …` 会命中 11-128；现在只把 A 句尾改成 `whose URL is named in the runtime context.`，品牌名与句意不变，作用域仍是 system / developer。新增 8 项断言。
- **跨出口回退：国际版限流 → 国内版（默认关闭）**（#564）：国际版整池被限流 / 24h 窗口用满 / 整池冷却 / 空池时，把同一请求在同名国内版出口重放一次；只对两边都提供的模型、单向、显式指定 realm 的请求不回退，403 11140 不重放。设置页新增开关。新增 16 项单测。
- **修复账号表排序把冷却 / 达限胶囊拆离账号行**（#568）：`applyTableSort()` 现在按「主行 + 紧跟的 `tr.cool-row` 续行」成组排序；表头列类型推断仍只看数据行。新增 JS 断言。
- **保留历史版本备份 + 面板版本回退**（#571）：更新成功后保留 `updates/backup-<目标版本>/`，份数由设置控制（0–3，默认 1；0 = 不留备份、回退不可用）。设置页新增「版本回退」卡片，按本地备份恢复、不下载旧包；回退前校验完整性，回退后校验 `Server: wb-proxy/<版本>`，失败自动恢复到回退前版本。`request.json` 新增 `action`（`install` / `rollback`），便携版、外部执行者与 systemd 服务都认；旧执行者会把 rollback 当「重装该 tag」。新增 26 项 Python 断言与 1 个 JS 套件。

## v1.7.24

非 systemd 部署的面板一键更新通道（#561）：`install_enabled()` 不再只认 systemd unit；部署方可以用外部执行者轮询 `request.json`，网关仍只写请求、不碰自己的代码。套件 205 个（153 Python + 52 JS）。

- **外部执行者通道**（#561）：`WB_UPDATE_INSTALL_ENABLED=1` + `WB_UPDATE_REQUEST_DIR` + `WB_UPDATE_EXECUTOR=external`（或目录里的 `executor.json`）即可让面板「安装更新」按钮可用；systemd 路径不变。
- **`request.json` 扩展**：除 `tag` / `version` / `requested_at` 外，新增 `pid` / `port` / `root`，供外部执行者停进程、探针和替换目录；systemd 执行者忽略额外字段。
- **参考执行者** `wb_update_external.py`：读取并校验 request（属主 / 权限 / 大小，读完即删），下载 release zip + `SHA256SUMS`，校验后按 manifest 备份 / 替换，停止网关，等 supervisor 拉起，探针 `/health`，失败回滚；启动时如果发现上次状态仍是 `applying` 等活跃状态且有备份，会先自动回滚（供 pre-start hook 使用）。
- **边界**：只替换 manifest 的 `files`，`accounts/` / `usage/` 不动；应用目录不可写会在下载前报「应用目录不可写」；不替换内置 `python/`。
- **测试**：`tests/_test_portable_update.py` 扩到 16 项，覆盖外部通道注册、`request.json` 字段、执行者读取 / 删除、无 start 重启模式、半途更新恢复。

## v1.7.23

Windows 便携版支持面板一键更新（#560）：不再要求 systemd unit；面板检测到新版本后，由独立 helper 下载 zip、校验 SHA256SUMS、停服、按 `release-manifest.json` 替换受管文件、重启，失败自动回滚。套件 205 个（153 Python + 52 JS）。

- **便携版一键更新**（#560）：新增 `wb_update_portable.py` 与 `wb_update_apply.py`。`install_enabled()` 在便携包（存在 `release-manifest.json`、`wb_update_apply.py`、`python/python.exe`）里返回 true，面板按钮不再因缺少 systemd unit 而禁用。
- **更新流程**：下载 `workbuddy2api-hub-vX.Y.Z.zip` 和 `SHA256SUMS` → 校验 → 解包（拒绝 `..` / 绝对路径 / 符号链接）→ 校验 manifest 与 `wb_proxy.py` 版本 → 备份受管文件 → 停止网关 → 替换 → 按原启动模式（静默 / 可见 / LAN）重启 → 轮询 `/health`；失败时恢复备份并重启旧版本。
- **边界**：只替换 `release-manifest.json` 的 `files`，`accounts/`、`usage/` 等受保护目录不动；不替换内置 `python/` 运行时（运行时升级仍需手动覆盖）；状态写入 `updates/status.json`，面板沿用现有轮询。
- **测试**：新增 `tests/_test_portable_update.py`（10 项），覆盖 URL / 校验、路径穿越、版本校验、备份 / 替换 / 回滚、helper 启动参数与 bridge 状态；`release/portable.txt` 已加入两个新模块，运行时契约补 `posixpath` / `signal` / `zipfile`。

## v1.7.22

账号任务参与独立开关（#521）：`enabled` 只管 API 取号，新增 `tasksEnabled` 控制签到、成长任务、连登管家、猫猫旅行和国际版每日活跃；停用账号可以只做任务，启用账号也可以不跑任务。套件 204 个（152 Python + 52 JS）。

- **账号任务参与独立开关**（#521）：账号新增 `tasksEnabled`，`enabled` 仍控制 API 取号；调度器的签到 / 成长任务 / 连登管家 / 猫猫旅行 / 国际版活跃改为按 `task_active()` 选号，Token 保活不受影响。手动任务入口（`/tasks`、`/tasks/run`、`/tasks/streak`、`/tasks/travel`、`/accounts/daily-chat`）同样按任务开关过滤；显式指定单个 uid 时仍允许手动执行。
- **面板「任务开 / 关」胶囊**：账号行新增任务开关，停用但参与任务的账号显示「已停用 · 任务中」；任务卡片和未活跃打卡名单按 `tasksEnabled` 过滤，旧网关没有该字段时回落到 `enabled`。
- **默认与兼容**：旧账号没有 `tasksEnabled` 时跟随 `enabled`；停用账号默认不跑任务，需要保留任务时在账号行点一下「任务关」变成「任务开」。导出 / 导入会带上该字段。新增 `tests/_test_task_participation.py`（9 项）和 `tests/_test_task_toggle.js`，调度器与打卡名单用例同步覆盖。

## v1.7.21

Windows 便携包补一个可选的前台窗口启动入口：默认仍是静默（pythonw + 日志窗口），需要「关窗即停」时用新的 `start-wb-proxy-visible.bat` / `start-wb-proxy-lan-visible.bat`。套件 202 个（151 Python + 51 JS）。

- **Windows 可选前台窗口启动**（#559）：新增两个薄封装脚本，设置 `WB_LAUNCH_MODE=console` 后调用原启动脚本，走现成的 `:use_python` 可见分支；默认启动方式不变。两个脚本已加入 `release/portable.txt`，并新增 2 项断言（封装脚本设置模式并调用正确目标、主脚本在 pythonw 探测前检查模式）。
- **README 快速启动说明**：Windows 默认静默启动，关掉日志窗口不会停服务；需要「前台控制台 + 关窗即停」时用 `-visible.bat`；局域网一节同步补上。

## v1.7.20

三处回归修复：裸对象账号导入、流式断流错误帧与打印工具调用延迟、热力图跨天不刷新。套件 202 个（151 Python + 51 JS）。

- **修复 `/accounts/import` 拒绝裸 snake_case 账号对象**（#556）：外层容器判断只认 `accessToken`，单个 cockpit 导出的 `{"uid": ..., "access_token": ...}` 在到达内层 snake_case 转换前就被 400 拒绝；补上 `access_token` 判断后，裸对象与 `{"accounts": [...]}` 的行为一致。新增 2 项断言。
- **流式断流不再伪造 `[DONE]`**（#549）：Chat Completions 与 Responses 在读 body 中途失败时，原来补一个 `data: [DONE]` 当作正常结束，客户端会把截断的答案当成完整回复；现在先释放已缓冲文本，再发标准 error 帧（`upstream_stream_error`）并关闭连接，不再发送 `[DONE]`。新增真实网关回归 `tests/_test_stream_abort_frame.py`（6 项）。
- **打印式工具调用即时释放**（#549）：`LeakedToolCallStreamFilter` 原来从候选前缀开始一直扣留到 EOF，长工具调用会让客户端长时间没有输出、最后一次性弹出；现在识别到调用自身的闭合标签（含全角 DSML 写法）就立即解析并发出，调用后的散文也立即释放，只有仍可能长成调用的文本留在缓冲里。新增 4 项断言。
- **热力图按本地日刷新**（#541）：工作回溯热力图原来只在进页面时取一次 366 天序列，跨过午夜后仍停在旧的一天；现在 `since` / `until` 都钉在本地午夜（`until` 取到明天 0 点），进页面、跨天或距上次取数满 60 秒时重取，今天新产生的用量会落进今天的格子。新增 `tests/_test_dashboard_heat.js`（8 项断言）。

## v1.7.19

网页活跃链路成组升级、Key 级账号白名单与模型可用性过滤：本版收下社区 12 个 PR（#542–#554），把国际版每日活跃的时段、并发、重试、去重、模型回退与提示词都做成可配置项，并补上 Key 级账号限制、思考档位映射、全局提示词在线编辑、出站身份版本面板和身份句动词收口。套件 200 个（150 Python + 50 JS）。

- **网页活跃可单独定时（分钟级）**（#548）：`schedule.daily_chat` 支持 1–24 个 `HH:MM` 时段，留空保持跟随签到整点；到点只触发国际版每日活跃，不再顺带跑签到 / 旅行 / 保活；设置页可逐个增删时间点，改完 30 秒内生效。
- **网页活跃并发数（默认 1）**（#552）：新增 `daily_chat_web_concurrency`（1–10）。巡检先收集当天未活跃的国际版账号，再按上限并发执行；worker 启动带轻微错峰，调度日志加锁，多 worker 同时写不丢行。
- **当日活跃成功后不再重复网页活跃**（#553）：`lastDailyChat` 已是今天时，`daily_chat()` 与 `daily_chat_web()` 直接返回 `skipped`，不再发桌面腿或新建网页会话；覆盖多个定时点、手动补跑与并发触发。
- **网页活跃重试改为线性递增**（#551）：第 N 轮等待从 `base × 2^(N-1)` 改为 `base × N`，默认 3 次由 120 / 240 / 480 秒变为 120 / 240 / 360 秒，最坏占用从指数级降到线性级。
- **网页活跃失败后按模型组合回退（默认关闭）**（#543）：按目录倍率排序（免费 → 低积分 → 高积分）逐个换模型，候选至少各试一次；建会话被业务拒绝时仍立即停止，不换模型。
- **网页活跃随机提示词（默认关闭）**（#545）：开启后从内置 300 条短句加最多 200 条自定义提示词里随机抽一条；关闭时仍只发 `Hi`。设置页弹窗支持搜索、全选与逐条复选。
- **API Key 支持允许账号白名单**（#544）：Key 新增 `accounts`，调度器只从名单内取号，会话粘性绑定不能绕过，`/u/<ref>` 定址到名单外账号返回 403；留空保持不限制，旧 Key 行为不变。
- **只列出可用模型（默认关闭）**（#546）：`/v1/models` 只返回池里至少有一个账号能服务的模型；国际版 PRO ONLY 模型只有在池里存在付费账号时才列出，`ready()` 同时拒绝把它派给免费账号。
- **思考档位映射（默认开启）**（#542）：请求的 `reasoning_effort` 不在模型目录声明的档位里时，改写为默认档位（`defaultEffort` → 固定档位 → 支持列表首项）；目录声明 `canDisableThinking: false` 时 `off` / `none` 也回落。可关闭后原样透传。
- **全局提示词支持面板在线编辑**（#547）：`prompt.text` 优先于原有的 `prompt.file`，可选追加到 system 或替换 system，保存后即时生效，不必再往容器里挂文件；默认透传不改写。
- **出站身份版本号可查看与修改**（#554）：面板列出 WorkBuddy 国际 / 国内、桌面内置 CLI、VSCode、CodeBuddy CLI、SDK 与 Node 的当前值和默认值，修改必须输入面板密码。国内版桌面端按 Go 版 2026-10-18 实跑对齐到 5.8.3 / CLI 2.161.0。
- **11-128 身份句动词搭配补全**（#550）：动词表从 `powered by` / `built on` 两种扩到 7 种（`built by` / `created by` / `developed by` / `made by` / `trained by` 与省略名词的 `You are powered by …`），不引入新判据；`You are free to use tools built by …` 这类分词修饰宾语的句子保持不改写。

## v1.7.18

11-128 身份句指纹、积分刷新崩溃、无畏契约任务与两处面板修复：本版把「客户端自我介绍句式 + 品牌」这一类渠道指纹从单句扩展到通用句式，并收了积分批量刷新、任务清单和面板文案/布局的几处回归。套件 189 个（139 Python + 50 JS）。

- **11-128：身份句 + 品牌的组合指纹改为中性句**（#533 / #537 / #539）：上游对 `You are an AI agent powered by DeepSeek Harness.`、`You are a coding agent powered by the deepseek-v4.1-flash model.`、`You are an AI assistant built on DeepSeek.` 这类「自我介绍句式 + 品牌」做大小写不敏感的连续子串匹配，命中即 400 / code 11-128；单独品牌词、改词或去掉句式都放行。现在把 system / developer 里的这类整句改写为 `You are a helpful AI assistant.`，不依赖具体品牌；user / assistant / tool 里的同名文本保持原样（用户数据）。`has_fingerprint()` 的小写快路径同步补齐，新增 12 项单测。
- **修复全量刷新积分在默认间隔下崩溃**（#530）：`_credits_sweep()` 使用 `random.uniform(0.8, 1.2)` 做账号间拟人间隔，但 `wb_proxy.py` 漏了 `import random`，多账号点「一键刷新积分」会以 `NameError` 中断；单账号与 `WB_CREDITS_SWEEP_GAP_MS=0` 不受影响。补上导入并新增回归测试。
- **成长任务补充「无畏契约拼豆专家」**（#525）：`WorkBuddy_Valorant` 登记为专家任务，事件固定使用真实专家 id `ex_xQHHD1u5Jkua`，不再走通用专家池；一键做任务时自动推进并领奖。
- **修复「数据用途声明」文案反了**（#528）：`X-Private-Data` 是「数据私有」声明，`true` = 不参与模型优化、`false` = 允许参与；下拉选项与英文词条此前正好相反。已交换两处文案并加 4 项断言。
- **修复模型清单「消费倍率」列截断**（#527）：带「免费账号不可用」胶囊的行在 1366/1440/1512 宽度下被 `nowrap` 裁掉第二枚胶囊；该列改用 `rate-cell` 类允许换行，不再依赖会被 i18n 翻译的 `data-label` 选择器。
- **测试：聚合缓存节流用例改为统计写盘调用**（#526）：exFAT 的 mtime 粒度粗，内容相同的重写与「没有写」在文件上看不出区别；用例改为计数 `_usage_cache_write_kind` 的 snapshot 调用，断言不变。

## v1.7.17

流式断流重试与出口退避、模型级 5xx 不熔断、Codex 一键配置、积分与估价口径、成长任务手动领取、免费账号不可用标注：本版把「断流被伪装成正常结束」「模型错误拖垮整池」「关限额不生效」这几条长尾问题一起收了，并补上几个新能力。套件 188 个（139 Python + 49 JS）。

- **流式断流：首字节前重开一次，出口按读完记账**（#522）：`open_upstream` 拿到响应头就返回，`IncompleteRead` 发生在读 body 时，回不到重试循环；chat 路径还会补一个 `[DONE]` 当作正常结束。现在 `emitted=False`（客户端一个字节都没收到）时在同一账号、同一出口上重开一次，递归复用已经发出的响应头；`iter_sse_lines` 对 Content-Length 提前 EOF 补抛 `IncompleteRead`。出口维度的成功记账从「拿到响应头」挪到「流读完」：断流记 transient，重试成功不抵消，干净的流才清零。`/v1/responses` 的读流重试仍待后续。
- **模型级 5xx 不再熔断整池**（#499）：`11134` / `11102` / `11150` / `11158` / `11129` 等错误先解析上游 body 的 `code`；命中时只停手并把上游 500 原样返回，不再给账号记 fails / 熔断。普通 500 仍按账号故障处理；`WB_MODEL_SCOPED_ERRORS_PENALIZE=1` 可恢复旧行为。
- **关闭日限额立即生效**（#519）：账号上的 `daily_*` 限额只活在内存里，原来在全部为 0 时不会推送，导致关掉限额后账号仍保留旧值。现在设置保存路径会把三条护栏无条件推一次（值为 0 也推），请求热路径保持原契约。
- **Codex 一键配置改用内联 bearer token**（#513）：Codex 0.163 启动时解析 `env_key`，GUI 未继承 `OPENAI_API_KEY` 就直接报 `Missing environment variable`；`auth.json` 不再兜底。provider 表改为直接写 `experimental_bearer_token`，并移除旧的 `env_key` / `env_key_instructions`。
- **X-Private-Data 可由设置选择**（#516）：设置页新增「数据用途声明」，`auto` 保持三套身份原取值，显式 `true`/`false` 对 WB / VSCode / CLI 都覆盖；非法值返回 400。
- **积分包到期显示统一按 no_expiry**（#474）：体验版 / 免费包的 `CycleEndTime` 只是每月计费边界，后端已按 `DeductionEndTime` 标成 `no_expiry`；前端表格此前仍拿周期结束算倒计时，出现「表里剩 12 天、最近到期卡片却不认」。现在表格与卡片都显示「长期有效」，套餐列表默认筛成「有剩余」。
- **OpenRouter 估价可按模型校准**（#514）：`usage/pricing-markup.json` 支持 `{模型: 系数}`，估算金额乘以该系数，便于把 OR 的 top-provider 参考价校准到官方价；缺失、损坏或非正数按 1.0，策略历史不改写。
- **成长任务支持手动领取**（#507）：任务区新增「自动领取」复选框（默认开）。取消后「一键做任务」只推进进度，任务停在「待领奖」，可在任务表按账号逐个领取；新增 `POST /tasks/claim`。
- **模型清单标出「免费账号不可用」**（#505，运行时部分）：上游 11158 时写入 `accounts/model_entitlements.json`，同一 `(realm, model)` 后续成功请求自动撤标，`11102` / `11134` 不记；`/v1/models` 每条带 `entitlement` 字段，面板显示「免费账号不可用」胶囊，24 小时后自动过期。刷新清单时的主动探测仍待采样口径。
- **一键部署区分 local / lan / public**（#512）：新增 `WB_DEPLOY_MODE`，默认 `lan` 保持原行为；`local`/`public` 只把宿主 8788 绑到 `127.0.0.1`，公网机需要再套反向代理。已有部署不自动改写 compose。
- **排程只在自己的配置小时执行**（#515）：此前命中任意一个配置小时就会把签到、活跃、猫猫旅行、连登和保活一起跑；现在每个任务只在自己的小时执行，面板「立即执行」仍会强制跑完整轮。
- **吞吐量改用 completion tokens**（#509）：Token 时序图的吞吐量、峰值与悬停都改用 `completion_tokens`，标签改为「输出 tok/s」；Token 总量仍显示 `total_tokens`。
- **只有 reasoning 的回复改为 failed**（#508）：上游把整轮预算花在 reasoning 上、正文为空时，流式发 `response.failed`，非流式返回 502，不再伪装成 completed；新增 `WB_DEFAULT_REASONING_EFFORT`。
- **区块导航不再把按钮文字算进标题**（#517）：「网关模型清单」标题里的全选 / 全不选 / 反选按钮不再混进导航项与锚点 id。
- **DSH 一键配置直写 profile 补丁层**：只替换 `wb-proxy` 段，保留其它 provider 与注释；放宽 `wb-proxy` 的限流重试，修掉「对话到一半链接中断」。
- **网关模型清单可勾选**：每行模型 ID 前新增复选框，默认全部勾选，表头带「全选 / 全不选 / 反选」；只有勾选的模型才会写进 DSH / OpenCode / Crush / ZCode 的模型清单。
- **移动端布局修复**：KPI 对比百分比、账号卡版本与状态徽章、今日活动标签、打卡按钮、积分格「明细」按钮等窄屏错行一并修正。
- **面板与日志出口统一脱敏手机号**：面板 JSON、日志看板、stderr、`/logs/export` 与账号导出统一掩码 11 位手机号，不误伤订单号与时间戳。
- **全量刷积分加并发与节奏护栏**：整池刷新加 CAS 单飞与账号间 800~1200ms 随机间隔，避免前端每 15 秒重试把上游打满。

## v1.7.16

国内版上游可改址、面板稳定性与英文界面修复：新增 WB_CN_CHAT_UPSTREAM，海外出口可把国内版对话 / 刷新 / 任务一起切到 www.workbuddy.cn（#465）；修复空数据时 Token 时序悬停崩溃、英文界面词级拼接、区间切换串号与轮询 304 不命中（#470）。套件 172 个（127 Python + 45 JS）。

- **国内版对话上游可通过 `WB_CN_CHAT_UPSTREAM` 改址**：`copilot.tencent.com` 从海外出口（代理软件全局模式等）连接时 TLS 握手被复位（`SSL: UNEXPECTED_EOF_WHILE_READING` / `WinError 10054`），国内版对话、刷新 token、模型目录、签到与成长任务全部失败，账号还会因此被熔断。设 `WB_CN_CHAT_UPSTREAM=https://www.workbuddy.cn` 后，桌面端 / CLI 身分的对话端点、`REALM_CONFIGS["cn"]["chat_upstream"]` 与成长任务的 `CHAT_BASE` 一起改走该地址；`X-Domain` 仍用账号自己的 domain。不设时行为与此前完全一致。
  `WB_CN_CHAT_UPSTREAM` overrides the China-realm chat upstream. From an overseas exit (e.g. a proxy in global mode) the TLS handshake to `copilot.tencent.com` is reset, so China-realm chat, token refresh, the model catalogue, check-in and growth tasks all fail and accounts get tripped by the breaker. With `WB_CN_CHAT_UPSTREAM=https://www.workbuddy.cn` the desktop / CLI chat endpoints, `REALM_CONFIGS["cn"]["chat_upstream"]` and the growth-task `CHAT_BASE` all move to that host; `X-Domain` still comes from the account's own domain. Unset, nothing changes.
- **面板三处缺陷与 README 结构修正**（#470）：空数据时 Token 时序图悬停不再抛 TypeError；补齐 12 条英文短语词条，消除「Taskand福利」「callsRequests」这类杂交串；区间切换加代次保护，迟到的旧响应会被丢弃，轮询窗口上界量化到 15 秒桶，连续轮询也能命中 304；README 的 §二 小节号与断开的端点表一并修正。

## v1.7.15

账号面板的模型冷却 / 达日限额提醒改为整行展示：长模型名与恢复时刻不再被 180px 账号列截断，悬停提示只保留原因与影响范围（#461）。套件 170 个（126 Python + 44 JS）。

- **账号列的提醒胶囊改成整行完整显示**：账号列宽 180px，模型冷却胶囊（`模型 · 撞线 X · 恢复 Y`）与按模型达日限额胶囊（`模型 · 今日已用 N / 上限 M token 已达限额`）原本挤在这一列里，`white-space:nowrap` + 省略号让模型名一长（如 `deepseek-v4.1-flash`）就把时刻整段切掉。现在这两类胶囊从账号列拆出，紧跟账号行另起一整行（`colspan` 跨满表头列数，窄屏 grid 下跨满 6 栏），文本自己完整：模型名、撞线 / 恢复时刻、本地时间口径，达限那类还带已用 / 上限数值与 0 点恢复。**悬停提示只保留可见文本没说的部分**——上游判定额度用满（code 6004）/ 临时限流（429）等原因，以及「仅此模型受限，同账号其他模型照常接单」的影响范围；模型名与时刻已画在胶囊上，不再在悬停里重复一遍。这一行是账号行的**续行**，两行之间不画分割线（账号行交出 `td` 默认的 `border-bottom`，续行也不画 `border-top`，窄屏下负 margin 把两张卡片拼成一块），悬停账号行时续行一起高亮，避免「徽章和所属账号像两块」的割裂感。排序仍按账号整体走，账号行 ↔ tbody 行的对应关系不变。
  Model cooldown and per-model daily-limit pills no longer live in the 180px account column, where `nowrap` + ellipsis cut the timestamps off; they now render as their own full-width row right below the account row (`colspan` spanning every header column, 6 grid columns on narrow screens), carrying the model name, cap and reset times, the "times are local" note, and used-vs-cap counts for daily limits. **The tooltip keeps only what the pill does not already say**: the reason (quota exhausted with code 6004 / temporary 429) and the scope ("only this model is restricted, the account's other models still take traffic"). The model name and timestamps are drawn on the pill, so they are no longer repeated on hover. That row is a **continuation** of the account row: no divider is drawn between them (the account row gives up the `td` default `border-bottom`, the continuation draws no `border-top`, and on narrow screens a negative margin joins the two cards into one), and hovering the account row highlights the continuation too, so the pill never looks detached from the account it belongs to. Sorting still reorders whole account rows, so the account↔row mapping is unchanged.

## v1.7.14

渠道校验与账号治理、协议兼容与面板体验：传输层头名保真与按账号隔离连接、CLI / VSCode 身份对齐官方客户端、11140 默认停止换号并持久化（#429 / #431 / #448 / #450）；401 先刷新 token 再换号、自签证书代理支持 WB_CA_BUNDLE（#441 / #439 / #435）；补齐 Anthropic tool_result 图片结构化、工具结果插入顺序与函数 Schema 规整（#452 / #420 / #446 / #451）；新增按账号定址路由、排程时段配置、6004 标签修复、撞线样本区块（#455 / #456 / #454 / #253）。套件 169 个（126 Python + 43 JS）。

- **修复自签证书代理导致的 OAuth 502**（#441）：证书校验失败是配置错误，不再按网络抖动重试 3 次。网关支持 `WB_CA_BUNDLE` / `SSL_CERT_FILE`，在系统根证书之外追加私有 CA；没配或配错时直接返回包含变量名和原始错误的提示，不再只显示一句 502。
- **HTTP 401 先刷新 token 再换号**（#435、#439）：同一次请求内每个账号最多刷新一次，刷新成功优先重试同一个号；只有刷新失败或重试仍 401 才按原逻辑轮换。减少「凭证其实能恢复、却被当成坏号冷却」的情况。
- **收紧 401 判定，避免中间层 HTML 401 锁死账号**（#440）：凭证失效的判定不再只看状态码；中间层返回 HTML 401 时不会再把账号标成 token 过期并停用，只有 JSON 凭证失效才走重新登录引导。
- **剩余用量估算新增「撞线样本」区块**（#253）：按 (区域, 模型) 列出每个撞线窗口的原始值（账号、撞线时刻、窗口用量），并提供「复制为征集格式」按钮，方便汇总跨实例样本。数据来自现有 `limit-events`，不新增上游请求。
- **修复工具结果前插入消息导致 11148**（#420）：`repack_tool_result_blocks` 以前在 `assistant tool_calls` 与第一条 `tool` 结果之间出现 user / developer 消息时直接放弃重排；现在会把这类插入消息移到整批结果之后，保证结果紧跟调用。
- **按账号定址的调用入口**（#455）：新增 `/u/<ref>/v1/...`（`chat/completions`、`responses`、`models` 等），`<ref>` 支持完整 uid、uid 前 8 位或昵称；歧义 / 找不到 / 账号不可用都会如实报错，不会猜号或偷偷换号。不带 `/u/` 前缀的旧路径行为不变；定址请求同样走现有 API Key / 面板鉴权与用量统计。
- **修复用量时序热力图跨日合并**（#447）：时序桶网格改为对齐本地午夜 / 整点，跨日查询不再把两天并进同一个桶。
- **修复账号表 6004 短标签在冷却到期后不消失**（#454）：模型级 429 的 `lastError` 是历史记录，冷却到期后 `pills` 已空，面板仍把它画成账号级 `HTTP 429 · code:6004`；现在该哨兵值不再进入账号级错误标签，冷却期间仍由 `coolPills` 显示「撞线 / 恢复」。
- **后台排程时段可配置**（#456）：`accounts/settings.json` 新增 `schedule` 段（`checkin` / `travel` / `keepalive` / `cat`，24 小时制，支持数组或逗号串），缺省 / 非法 / 越界 / 空逐项回落旧默认（09:00/21:00、22:00、01:00），按 mtime 缓存免重启生效；`/scheduler` 的 `mode` / `mode_cn` / `mode_intl` 按实际时段生成。
- **修复 Anthropic `tool_result` 图片被拍平成文本**（#452）：`/v1/messages` 的工具结果里带图片时，base64 data URL 以前被当普通文本发给上游，按约 1 token/字符计费，一张截图就能把长上下文顶到 11115；现在含图时保留结构化 `image_url` 数组，纯文本仍折叠成字符串。实测同一张 31KB PNG 从约 3 万 input tokens 降到 665。
- **修复函数参数 Schema 结构缺陷与工具参数反序列化失败导致上游 400 拦截**：针对客户端（如 Hermes Agent、各类 OpenAI 兼容 SDK 等）声明无参工具时发送 `"parameters": {}`（缺失顶层 `type: "object"`）触发上游 Go struct 校验拒绝报 `code 11129 invalid function call parameters` 的问题，网关层自动补齐 `type: "object"` 与 `properties: {}` 并清洗非法的 `required` 字段；同步修复历史对话消息中 `tool_calls[].function.arguments` 为字典对象导致上游反序列化失败（`code 11101`）的缺陷。
- **自动切换出站身份支持配置允许的身份**：此前 429 自动换身分固定按 WB → VSC → CLI 轮转，无法排除某条身份（例如不想让账号落到某个身份上）。现在「设置 → 自动切换出站身份」可以勾选允许的 WB / VSC / CLI（至少一个），自动切换只在勾选的身份之间按原顺序轮转；未配置时默认三条全允许，行为与旧版一致。设置持久化在 `accounts/settings.json` 的 `auto_switch_products`，读取时会过滤未知值，保存时校验非法值返回 400。

- **修复 Safari 无法复制 API Key**：面板只渲染遮罩后的 Key，复制时必先 `await /settings/reveal`，而 Safari 把剪贴板绑定在点击的 transient activation 上，`await` 之后 `writeText()` 以 `NotAllowedError` 拒绝。现在值已在手时同步写入、需取真值时改用 `ClipboardItem` 的 Promise 形式；`copyFilteredLogs()` 也改走 `writeClipboard()`，修掉纯 http 区网（非安全上下文）下的 `TypeError`。
- 新增 `GET /v1/balance-summary`：使用模型 API Key 读取当前区域启用账号的积分汇总，仅返回余额、账号数量与快照时间；只读缓存，不刷新上游，也不暴露 UID、昵称或单账号信息。
- 新增可选 `WB_PRESERVE_REQUEST_CONTRACT=1`（默认关闭，修改后重启网关）：将 Responses `text.format` 映射为 Chat `response_format`，保留 namespace 扁平函数的 `strict`，并跳过历史工具参数及 `tool` / `function` 结果的指纹替换，避免把 `11-128` 等业务数据改坏。其余角色文本仍沿用既有清洗；关闭时全部维持旧行为。此开关只保真字段和工具数据，不校验输出、不保证上游执行 JSON Schema 或 `tool_choice`。关闭开关并重启即可回退。
  Opt in with `WB_PRESERVE_REQUEST_CONTRACT=1` (off by default; restart the gateway after changing it) to map Responses `text.format` to Chat `response_format`, preserve flat namespace function `strict`, and bypass fingerprint rewriting of historical tool arguments and `tool` / `function` results. Other role text keeps existing sanitization; disabled mode is unchanged. This preserves fields/data only: it neither validates output nor guarantees upstream JSON Schema / `tool_choice` enforcement. Unset the flag and restart to roll back.

### 模型额度定时主动查询

- **新增「模型额度定时主动查询」：不用等真实请求撞限额，就知道哪条配额线已经满了**。窗口探针（`wb_window_probe`）补的是下半段——某条配额线已经 429 之后，把重置时刻用掉；上半段此前没人管：一条线在被打满之前是完全黑盒的，只能等真实请求撞上去才暴露。现在在「设置 → 模型额度定时主动查询」勾选模型后，网关每 **6 小时**为每个「账号 + 模型」发一个最小请求体检：
  - **查到 429 + 重置时刻** → 立刻记该模型的冷却（真实请求不再撞这条已满的线），并把重置时刻登记给窗口探针，到点抢占新一轮窗口；这条线之后**不再定时查询**——本地已经存了它的 429 记录，交给探针就够了。
  - **请求通了** → 这条线还没到限额，继续按 6 小时轮询；探针成功、窗口记录撤销之后，轮询自动恢复。
  - **网络/解析失败** → 不算查过（不会因此推迟整整一个周期），15 分钟后重试；连续失败到上限才推迟到下一个周期。
  - 记录落在 `accounts/quota_scan.json`（原子写，**重启后仍保留每个组合的上次查询时刻**），重启不会让所有模型重新查一遍；一次性勾上一堆模型时，首次查询会自动错峰（相邻组合相隔 15 秒），不会在同一瞬间把请求全打上去。
  - 默认**不勾选任何模型**（即关闭），勾了才会发请求——每次查询都会真的消耗该账号极少量额度。设置页能看到每个「账号 + 模型」的最近结果与下次查询时间，也可以点「立即查询」跳过等待；已经有 429 记录、或正在冷却中的组合不会被额外撞一次。
- 新增 `tests/_test_quota_scan.py`（16 项）：钉住未勾选时什么都不做、查通之后要等满一个间隔、429 时既记冷却又登记窗口且之后停止轮询、失败不算查过并按短间隔重试、连续失败到上限才推迟到下一周期、持久化与重启视角、停用/无凭证/冷却中的组合被跳过、首次登记错峰与批次上限。
- **出站 chat 请求：头名保真 + 按账号隔离连接 + 紧凑 JSON**：出站头名此前被 `urllib` / 连接池统一改写大小写（`X-Request-ID` → `X-Request-Id`、`X-IDE-*` → `X-Ide-*`、`traceparent` / `b3` → `Traceparent` / `B3`），与官方客户端代码里的写法不是同一字节串；现在 chat / 每日活跃 / 面板探活三条路径按原名原样发送，billing / 登录 / 签到等未传有序头的路径行为不变。连接池键新增凭据摘要（`Authorization` / `X-Refresh-Token` / `X-User-Id` 的 sha256 前 16 位）：同一出口上的不同账号不再共用同一条 TCP/TLS 连接（同一账号仍复用；`egress_key` 与退避 / 熔断口径不变）。chat 请求体改为紧凑 JSON（与 Node `JSON.stringify` 一致）。测试：`tests/_test_upstream_pool.py` 新增 [17] 头名保真 / [18] 按账号分池，`tests/_test_upstream_repairs.py` 新增 [6] 紧凑 JSON。
- **CLI 身份对齐官方 CodeBuddy CLI 2.168.2**：`X-IDE-Version` 与 UA 升到
  `2.168.2`（`CLI/2.168.2 CodeBuddy/2.168.2`；官方 adapter 会删掉 SDK 的
  user-agent，CLI 也没有 userAgentExtension，所以是两段）；补齐官方 CLI 的
  `X-Agent-Purpose` / `X-Agent-Type` / `X-Agent-Runtime`、`X-Private-Data`
  （个人号 false、企业号 true）、四段 b3 链路头族（含 `X-B3-ParentSpanId`）
  与 OpenAI Node SDK 6.25.0 的 `x-stainless-*` 指纹头（OS/arch/node 版本默认取
  宿主，可用 `WB_IDENTITY_OS` / `WB_IDENTITY_ARCH` /
  `WB_IDENTITY_NODE_VERSION` 覆盖）。`X-Conversation-ID`
  按（账号, 会话键）稳定、`X-Conversation-Request-ID` 在一轮 user send 的
  所有重试/换号里复用同一个值，`X-Request-ID` 仍等于
  `X-Conversation-Message-ID`，`X-Root-Request-ID` = 本轮
  `X-Conversation-Request-ID`。chat 请求去掉 Origin / Referer /
  X-CodeBuddy-Request / X-Requested-With / Accept-Language，`Accept` 改为
  `application/json`；请求体 gzip 并注入 `cache_retention=long`，CLI 身份
  不再发 `X-Device-Token`。桌面端 / VSCode 身份行为不变。国际版端点仍是
  `www.workbuddy.ai`（官方 CLI 用 `www.codebuddy.ai`，解析不了，属已知差异，
  本版不改）。
- **VSCode 身份对齐官方扩展 4.12.39997873**：UA 改为
  `VSCode/1.119.0 CodeBuddy/4.12.39997873`（productName=CodeBuddy、扩展版本
  4.12.39997873），补 `X-Product-Version` / `X-Model-ID` / `X-Agent-Type` /
  `X-Private-Data`（仅 intl）/ B3 链路头族（含 `X-B3-ParentSpanId` 与四段 b3）/
  `X-Request-Trace-Id` / `monitor_*` 时间戳；`X-Request-ID` 与
  `X-Conversation-Request-ID` 同值、`X-Conversation-Message-ID` 独立，
  `X-Conversation-ID` 为 32 位 hex（宿主 VSCode 版本默认 1.119.0，可用
  `WB_VSCODE_IDE_VERSION` 覆盖）。桌面端 / CLI 身份行为不变。官方主 chat
  默认请求体 gzip（`RequestBodyGzip=true` → `executeByMode` →
  `createCustomFetch` 压缩），本版启用；非企业号没有真实设备 token 时按官方
  失败态发 `X-Device-Token-Error: sdk_not_initialized`（不伪造 token）。
- **阿里云 systemd 部署支持手动安装稳定版**：设置页可从配置的 fork Release 安装新版本；更新包必须通过 `SHA256SUMS` 与 `release-manifest.json` 校验，账号与用量目录列为保护路径。网关只提交版本号，root-owned systemd 更新服务负责备份、替换、重启与健康检查；失败会恢复程序文件。每日检查仍默认关闭，打开后也只检查、不自动安装。
- **风控限制 403（code 11140）不再换号连坐：默认停手、限制持久化、整池暂停**（#422、#425）：上游对国际版账号的风控在 2026-10-15/16 之后明显收紧，账号级限制的正文是 `403 {"code":11140,"msg":"request illegal","displayMsg":"账号存在异常行为，已被限制使用"}`；社区复盘（[Zhengyuuuui/workbuddy2api](https://github.com/Zhengyuuuui/workbuddy2api/blob/main/doc/incident-postmortem-and-hardening-checklist.md)）给出的处置红线是「看到 11140 立即停手，解封前严禁换号继续打同一设备——设备指纹会连坐」。旧实现在 11140 上冷却该号后立刻换下一个号继续同一请求，一个请求最多遍历整个池子（#422 用户实测 7 个号在一次任务里全被封），而且冷却只存在内存里，重启 / 刷新 token / 「一键测试」都会把它清掉。现在：
  - **默认不再跨号重试**：第一个账号被限制就停手，把原因交给客户端，下一次请求自然落到别的账号；需要旧行为时设 `WB_RESTRICTED_403_ROTATE_MAX`（默认 0）。
  - **限制期限持久化**：账号档案新增 `restrictedUntil`（2 小时），重启 / 换身分 / 刷新凭证 / 一键测试都不再把它放回池子；只有真正成功一次（说明上游已解除）才清。
  - **整池连坐暂停**：同一区域在 `WB_RESTRICTION_WAVE_WINDOW`（默认 900 秒）内被限制 `WB_RESTRICTION_WAVE_MIN`（默认 2）个账号，就暂停该区域派发 `WB_RESTRICTION_HOLD_SECONDS`（默认 1800 秒），期间一个上游请求都不发、直接返回 429 与申诉指引；设 `WB_RESTRICTION_HOLD_SECONDS=0` 关闭暂停。
  - 后台巡检 / 打卡会跳过受限账号与暂停中的区域；「一键测试」识别到 11140 也按账号级限制记账，不再用 60 秒冷却覆盖。
  - `tests/_test_restricted_403.py` 扩到 14 项（默认不换号 / 开关恢复旧行为 / 单号场景 / 连坐暂停 / 关闭暂停 / 持久化与清冷却 / 内容审核 403 不变）。
- **国际版 chat 不再发送 `X-Device-Token`**：国内版客户端带真实 Turing Shield 设备 token，国际版客户端没有这个头（Zhengyuuuui 三轮抓包与 5.7.6 / 5.8.4 二进制构造器都不含）；网关配置的是全局单值，发给国际版账号既是伪造头、又把多个账号串到同一台设备上。billing 路径与国内版 chat 维持原行为。
- **`X-Conversation-ID` 按会话稳定**：官方客户端同一会话复用同一个会话 id（主对话是 UUIDv4），网关此前每个请求都新生成，等于把一次多轮对话拆成上千个「一次性会话」。现在 chat 路径用 `wb_identity.conversation_id_for(uid, session_key)` 按「账号 + 会话键」稳定派生，不同账号 / 会话互不相同；客户端没带会话键时保持每请求新生成。

## v1.7.13

积分安全、便携部署与面板行为修复：请求结束后按上游回包的真实 credit 立即扣减缓存余额，`reserve_credits` 不再在两次刷新之间失效（#442）；便携包补回 `console_tail.py` 与 `pythonw.exe`，静默启动不再一闪而过（#416）；日志按钮改为跨平台新窗口打开运行日志（#427）；关闭服务用 `/health` 复核，容器或守护进程自动拉起时不再谎报成功（#426）；OpenWrt 包补齐 ssl / sqlite3 / codecs 依赖（#419）；补全 atrest 提示的英/正体词条并修正 README 徽章 alt（#415）。套件 160 个（117 Python + 43 JS）。

- **请求结束立即扣减余额，低积分护栏不再失效**（#442）：`reserve_blocked()` 以前只读上一次 `fetch_credits()` 的缓存值，而刷新器每 30 分钟只刷一个号，48 个号的池子刷完一轮要 24 小时。现在每条完成的请求都会按上游回包里的 `credit` 扣减内存余额（加锁、下限 0），并同步 `used` 与百分比；后台刷新回来再校正。串行场景下 `reserve_credits` 立即生效，不会再出现「53 分钟扣 709 分、护栏全程没拦住」。
- **便携包补回静默启动依赖**（#416）：`release/portable.txt` 新增 `console_tail.py`，精简运行时不再删除 `pythonw.exe`。双击 `start-wb-proxy*.bat` 后服务在后台静默运行、日志窗口正常弹出，不再「窗口一闪而过」；发布工作流的冒烟检查也会断言这两个文件在包内。
- **日志按钮跨平台可用**（#427）：面板顶栏的眼睛按钮不再让服务端 `spawn` 进程，改为 `window.open('/dashboard?tab=logs')` 在新窗口打开运行日志。Windows / macOS / 容器走同一条路，不会再出现「Linux 上每次都提示已关闭」；`/console/toggle` 保留为兼容端点并返回 `mode=web`。
- **关闭服务不再把失败渲染成成功**（#426）：点击电源按钮后轮询 `/health`，只有端口真的不再应答才标记「已关闭」；容器或守护进程把进程重新拉起时会如实提示「停止请求已发送，但服务仍在运行」，按钮也不会被永久禁用。
- **OpenWrt 依赖补齐**（#419）：`DEPENDS` 增加 `python3-openssl`、`python3-sqlite3`、`python3-codecs`，避免安装后因缺少 ssl / sqlite3 / `encodings.idna` 而启动失败或域名解析报错。
- **i18n 与 README 小修**（#415）：补上 `当前系统不支持从进程内存回收密钥。` 的英文与正体词条；README 徽章 `alt` 跟实际版本对齐。

## v1.7.12

用量监控、账号治理与体验全景升级：用量明细三维度统一接入真实消耗积分列并大幅提升深色模式文字对比度（#412）；支持账号与模型精确黑名单控制（#405、#402）；实时日志与最近请求表格直观展示发起方 API Key 标识（#401、#397）；软限流阶梯补全时间窗衰减并优化秒级可读剩余时间（#408）；桌面端 UA 与版本常量收敛至单一来源（#403）；定时调度巡检在账号之间加入随机错峰延时防 IP 集中风控（#394）；将根目录维护脚本归档至 tools/ 净化入口结构（#398）；对齐全量套件计数校验（158 个套件）。

- **数据看板用量明细三维度统一「消耗积分」列**（#412）：账号维度表头由静态剩余积分改为当前时间范围内的真实「消耗积分」（window.credit），模型与 API Key 维度同步补齐消耗积分列，产生积分消耗时直观高亮展示。
- **深色模式小字与弱文字对比度全面优化**（#412）：将深色模式下用于小字、轴标签及副标题的 --dim2 / --dim-light 从 #7c796f 提亮至 #9c9a90，在卡片与表头上达到 4.6~5.2:1 对比度，完全满足 WCAG AA 级 4.5:1 可读性标准；清晰勾勒热力图网格边界。
- **支持按账号禁用个别模型**（#405、#402）：账号持久化支持 disabledModels 黑名单，控制台账号行点击「+ 禁用模型」可弹窗按当前区域复选框多选精确禁用，命中的模型不再派给该账号，其余模型与账号接单完全不受影响。
- **实时日志与看板展示 API Key 标识**（#401、#397）：chat / chat done / messages / responses 四条日志行以及「最近请求」表格新增 API Key 列，直观辨别各请求的具体调用方。
- **软限流阶梯时间窗与可读剩余时间**（#408）：soft_streak 与 unscoped_streak 同样纳入 WB_FAIL_WINDOW（默认 600 秒）时间窗衰减判定；冷却时间由「0 min」优化为「剩余 42s / 1m30s」等可读格式。
- **定时调度账号随机错峰延时**（#394）：多账号在后台执行每日打卡与巡检时，在账号之间增加 2~5 秒动态随机浮动延时，防止整点请求集中引发出口 IP 频率风控。
- **出站 UA 与版本号收敛至 wb_identity**（#403）：彻底清除代码内残留的硬编码 5.5.2 旧版常量。
- **根目录调试脚本归档至 tools/**（#398）：净化根目录条目，使核心入口更清晰。

## v1.7.11

用量监控、账号治理与体验全景升级：用量明细三维度统一接入真实消耗积分列并大幅提升深色模式文字对比度（#412）；支持账号与模型精确黑名单控制（#405、#402）；实时日志与最近请求表格直观展示发起方 API Key 标识（#401、#397）；软限流阶梯补全时间窗衰减并优化秒级可读剩余时间（#408）；桌面端 UA 与版本常量收敛至单一来源（#403）；定时调度巡检在账号之间加入随机错峰延时防 IP 集中风控（#394）；将根目录维护脚本归档至 tools/ 净化入口结构（#398）。

- **数据看板用量明细三维度统一「消耗积分」列**（#412）：账号维度表头由静态剩余积分改为当前时间范围内的真实「消耗积分」（window.credit），模型与 API Key 维度同步补齐消耗积分列，产生积分消耗时直观高亮展示。
- **深色模式小字与弱文字对比度全面优化**（#412）：将深色模式下用于小字、轴标签及副标题的 --dim2 / --dim-light 从 #7c796f 提亮至 #9c9a90，在卡片与表头上达到 4.6~5.2:1 对比度，完全满足 WCAG AA 级 4.5:1 可读性标准；清晰勾勒热力图网格边界。
- **支持按账号禁用个别模型**（#405、#402）：账号持久化支持 disabledModels 黑名单，控制台账号行点击「+ 禁用模型」可弹窗按当前区域复选框多选精确禁用，命中的模型不再派给该账号，其余模型与账号接单完全不受影响。
- **实时日志与看板展示 API Key 标识**（#401、#397）：chat / chat done / messages / responses 四条日志行以及「最近请求」表格新增 API Key 列，直观辨别各请求的具体调用方。
- **软限流阶梯时间窗与可读剩余时间**（#408）：soft_streak 与 unscoped_streak 同样纳入 WB_FAIL_WINDOW（默认 600 秒）时间窗衰减判定；冷却时间由「0 min」优化为「剩余 42s / 1m30s」等可读格式。
- **定时调度账号随机错峰延时**（#394）：多账号在后台执行每日打卡与巡检时，在账号之间增加 2~5 秒动态随机浮动延时，防止整点请求集中引发出口 IP 频率风控。
- **出站 UA 与版本号收敛至 wb_identity**（#403）：彻底清除代码内残留的硬编码 5.5.2 旧版常量。
- **根目录调试脚本归档至 tools/**（#398）：净化根目录条目，使核心入口更清晰。

- 国际版 `deepseek-v4.1-flash` 免费模型支持额度耗尽后回退到 `deepseek-v4.1-flash-sg` 积分版。默认关闭；仅当所有已启用账号都收到带重置时间的 6004 限额记录时才回退。设置网关环境变量 `WB_ENABLE_FREE_MODEL_CREDIT_FALLBACK=1` 启用，可能产生积分费用；用量记录会显示实际计费模型，并保留客户端请求模型字段。
- **软限流阶梯补上时间窗，冷却剩余时间不再写成「0 min」**（承接「连续失败要有时间窗」那条：当时只修了 `fails` / `degrade`，两条软限流阶梯漏了）：
  - `soft_streak` / `unscoped_streak` 也按 `WB_FAIL_WINDOW`（默认 600 秒）判定「连续」：它们以前只加不减、只有成功才归零，于是昨天被限过 3 次，今天第一个 429 就直接从 2400s 起步（模型档 240s），后天更高，一路顶到 7200s——账号早就恢复正常（面板测试是绿的），却动不动被按几十分钟到两小时。
  - `unavailable_reason()` 的剩余时间改成秒级（`_human_left()`）：还剩 42 秒时以前写「剩余 0 min」，读起来像「早该能用了、网关还挡着」；现在写「剩余 42s」，连续第 2 次起附上「（连续第 N 次）」，看得出是在升级还是刚开始。
  - 通用失败分支（非 429 / 403 / 402 / 5xx / 传输抖动）以前**静默**给账号记一笔并冷却 3/60 秒：池子空了的报错只说得清谁在冷却，说不清是谁把它记成冷却的。现在打一行 `account <uid>: <异常原文>, cooling Ns (fails=…, ready=…)`。
  - `tests/_test_breaker_fairness.py` 扩到 27 项（两条阶梯的窗口衰减、`WB_FAIL_WINDOW=0` 的旧语义、剩余时间格式，以及「这条静默分支必须留日志」）。
- 支持**按账号禁用个别模型**（账号 × 模型黑名单）：账号新增持久化字段 `disabledModels`，面板账号行点「+ 禁用模型」会弹出该账号所属区域的模型复选框列表（**默认全选；取消勾选即禁用**，带全选 / 全不选与实时计数），也可直接 `POST /accounts/set` 传 `disabledModels`（空数组 = 清空）。命中的模型不再派给该账号，请求自动落到池子里其他账号；该账号的其他模型完全不受影响。默认空 = 不限制、行为不变。仅**精确匹配**模型 id（不做前缀）。这是**人工**配置，与 `model_caps` / `model_cooldowns` 那些自动节流互不干扰；池子里所有账号都禁用了某个模型时，报错会逐个账号写明原因。
- 请求日志与看板「最近请求」表显示发起方 API Key：`chat:` / `chat done:` / `messages:` / `responses:` 四条日志行，以及「最近请求」表格新增的一列，都会带上该次请求所用 key 的名字（没填 name 就回退 id）；未带 key 的记录显示 `-` / `—`。纯显示层改动——`usage.jsonl` 里的 `key` 字段仍是 key id，「按 API Key 的消耗归属」的统计口径不变。

## v1.7.10

协议与并发控制重大增强（修复与发布）：系统设置新增「启用 Anthropic Messages 接口 (Claude)」与「免费额度耗尽回退积分版」两个可视化开关，默认关闭；支持为每个 API Key 配置独立最大并发数（#359）；一键配置 DeepSeek Harness 默认开启视觉识图能力（#385）；修复流式 finish_reason: error 被误记为成功的问题（#389）；修复类内方法缩进保证全量套件全绿。

- **系统设置支持可视化开关 Anthropic Messages 接口 (Claude)**：用户知晓协议转换长上下文可能增加积分消耗的风险后，可直接在控制台勾选启用，无需再配置环境变量并重启。
- **系统设置支持可视化开关免费额度耗尽回退积分版**（#387）：国际版 deepseek-v4.1-flash 全池 6004 撞线后可选回退到 deepseek-v4.1-flash-sg，支持面板可视化一键开关，默认关闭防误耗积分。
- **支持为每个 API Key 配置独立并发限制**（#359）：编辑 Key 时支持设置最大并发数（0 或留空 = 不限并发），并在卡片展示「并发上限 N」徽章，请求层实时计数并在超限时返回 429。
- **一键配置写入 DeepSeek Harness 默认开启识图**（#385）：为多模态模型自动配置 supportsVision: true。
- **修复流式 finish_reason=error 记账统计不实**（#389）：上游流内返回 error 时如实调用 record_error 记录失败，避免看板成功率虚高。

## v1.7.9

协议与并发控制重大增强：系统设置新增「启用 Anthropic Messages 接口 (Claude)」与「免费额度耗尽回退积分版」两个可视化开关，默认关闭；支持为每个 API Key 配置独立最大并发数（#359）；一键配置 DeepSeek Harness 默认开启视觉识图能力（#385）；修复流式 finish_reason: error 被误记为成功的问题（#389）。

- **系统设置支持可视化开关 Anthropic Messages 接口 (Claude)**：用户知晓协议转换长上下文可能增加积分消耗的风险后，可直接在控制台勾选启用，无需再配置环境变量并重启。
- **系统设置支持可视化开关免费额度耗尽回退积分版**（#387）：国际版 deepseek-v4.1-flash 全池 6004 撞线后可选回退到 deepseek-v4.1-flash-sg，支持面板可视化一键开关，默认关闭防误耗积分。
- **支持为每个 API Key 配置独立并发限制**（#359）：编辑 Key 时支持设置最大并发数（0 或留空 = 不限并发），并在卡片展示「并发上限 N」徽章，请求层实时计数并在超限时返回 429。
- **一键配置写入 DeepSeek Harness 默认开启识图**（#385）：为多模态模型自动配置 supportsVision: true。
- **修复流式 finish_reason=error 记账统计不实**（#389）：上游流内返回 error 时如实调用 record_error 记录失败，避免看板成功率虚高。

## v1.7.8

打卡鲁棒性与下游聚合规范对齐：每日活跃打卡支持在首选模型额度用满（6004 cap）或限流（429 冷却）时自动回退同区域备选免费模型，确保任务永不中断（#341）；严格对齐下游聚合器（New API / One API）缓存解析规范，流式尾包与非流式返回强制补齐并透传 prompt_tokens_details.cached_tokens（#370）。

- **每日活跃打卡自动防冷却回退**（#341）：在 `daily_chat_model()` 中增加冷却感知机制，若当前账号的 `deepseek-v4.1-flash` 处于 429 冷却或 6004 额度耗尽状态，自动顺延回退至该区域可用的备选免费模型（如 `hy3`、`hy4-preview`、`glm-5.3-flash` 等），避免单一模型耗尽导致当天 30/50 活跃奖励丢失。
- **下游聚合器缓存命中规范对齐**（#370）：针对 New API / One API 严格依赖 `usage.prompt_tokens_details.cached_tokens` 的解析行为，在网关响应清洗层无论流式还是非流式均规范化补齐该字段，确保下游能正确呈现缓存命中绿色标识与 Token 统计。

## v1.7.7

渠道校验对齐与稳定性提升：对齐国际版 5.8.4 客户端出站身份头与链路追踪头族，彻底解决上游 11-128 conversation id missing 拦截（#380、#382）；熔断机制引入时间窗与本机断网识别，新增账号池一键连通性测试（#375）；任务福利页新增「未活跃账号一键打卡」与悬停预览（#379）；国际版每日活跃网页通道支持失败自动阶梯重试（#377）；禁用总览区分配额耗尽与临时限流（#376）；一键配置 DeepSeek Harness 补全思考档位（#374、#378）；修复 413 请求体超限 gateway_hint 提示（#372）与 GPT 家族 max_tokens < 16 自动夹取保护（#364、#252）。

- **任务与福利页新增「未活跃打卡一键打卡」**：「国际版每日活跃福利」区在「每日活跃打卡 (国际版)」右边多一个按钮，只对**今天还没有活跃打卡记录**的国际版启用账号补跑。判定口径与「今日活跃打卡状态」卡片完全一致（都走 `activityStatusOf()`），所以不会把已经打过的账号再打一遍——重复打卡不给积分，只会白烧账号额度；一个待打卡的都没有时只提示、不发请求。服务端 `POST /accounts/daily-chat` 新增 `uids` 参数按名单精确执行，名单里混进的停用账号、其它区域账号或不存在的 uid 一律忽略；**空名单等于「一个都不打」**，不会回落成「全部账号」（要打全部就别传 `uids`）。
- **「今日活跃打卡状态」卡片悬停可看未打卡名单**：鼠标放在卡片上会列出今天还没打卡的账号昵称与数量，不用再去账号页一个个比对；这份名单就是上面那个按钮要打的那批，看到什么就打什么。
- 新增 `tests/_test_daily_chat_pending.js`（22 项，按钮位置/样式、名单同源、请求体只带未打卡 uid、空名单与无国际账号的边界）与 `tests/_test_daily_chat_pending_route.py`（6 项，服务端 `uids` 过滤与空名单语义）。
- **熔断不再乱开：连续失败要有时间窗，本机断网也不算账号的账**。两个现象同源——账号治理只看次数、不看间隔，也不区分「这是谁的问题」：
  - `fails` 以前只加不减（只有成功才归零）：几小时前的一次 502 + 昨天的一次超时 + 刚才一次抖动就能凑成「连续 3 次」，把账号关 30 分钟；下一个窗口还按 1800 → 3600 → 7200… 翻倍，于是越关越久——「明明测了能用，它还熔断」就是这么来的。现在两次失败间隔超过 `WB_FAIL_WINDOW`（默认 600 秒，设 0 关闭）就重新从 1 数起，成功照旧清零。
  - DNS 解析不了、路由不可达这类**本机没网**的失败，以前也逐账号记一笔并冷却 60 秒；断网那一分钟里整个池子被走一遍，等网络恢复，账号全在冷却里。现在这类失败先被认出来（`looks_like_local_network()`），一个账号都不记，按「本机网络不可用」上报并给出 `gateway_hint`；`/v1/chat/completions`、`/v1/responses`、`/v1/messages` 三条链路都走这个分支。
  - 账号行与禁用总览新增可解释字段：连续失败次数、窗口长度与最近一次失败距今（`fails` / `failWindow` / `lastFailFor`），「还差几次熔断」一眼可见。
- **新增「一键测试」**：工具栏按钮一次测完当前区域所有已启用账号，并同时给出「本机 ↔ 上游」的连通性（DNS 与 TCP 耗时）。结果表逐账号显示通过 / 失败、耗时与原因（失败行附带「连续失败 N 次」），**本机断网的行单独标成「本机网络」**，不会和账号真失败混在一起。测试通过会清掉该账号的熔断 / 降权 / 软限流计数（沿用 `/accounts/test` 的成功路径）；测试失败只记录原因、不冷却账号——诊断动作不该改变账号状态。后端不带 `uid` 即为整池测试：`POST /accounts/test {"realm": "cn"}`。
- 新增 `tests/_test_breaker_fairness.py`（19 项）与 `tests/_test_account_test_all.js`（27 项）；看板套件新增共享的 `tests/_dashboard_sandbox.js`（把页面脚本装进显式沙箱再跑，本套件用它；其它看板套件仍各自 `new Function`，可后续统一）。
- **禁用总览如实区分「配额耗尽」与「临时限流」**：面板「当前禁用」页把模型级限流一律写成「该模型配额耗尽（code 6004）」，但其中多数只是上游一次 429 临时限流（60s~10m 的短窗口，到点自动恢复）。现在按上游返回体里是否带重置墙钟（`cappedAt`）分开措辞：带的是 6004 配额耗尽，不带的是 429 临时限流，与账号卡片上的口径一致。
- **禁用总览的时间列给出真实起始时刻**：模型级限流以前只在 6004 撞线时才有时间（列头「撞线时间」），临时 429 那一行永远是个「—」。现在网关把每次模型级限流的**发生时刻**一并记下并在 `/accounts` 的 `modelCooldowns[]` 里以 `startedAt` 透出，列头改为中性的「开始时间」——6004 行给上游撞线时刻，429 行给限流时刻，两类都有「起始 + 恢复」两段真实时间（`startedAt` 为运行时态，重启即清零；6004 的撞线时刻另有持久化兜底）。
- **国际版每日活跃网页通道失败重试**：网页通道一轮没跑完（会话状态仍是 `working`）时，原来直接判失败、当天 30/50 积分静默丢掉。现在**默认开启**失败重试，设置页「国际版每日活跃打卡」可调「最多尝试次数」（默认 3 次，含第一轮，上限 5）与「首轮等待」秒数（默认 120，上限 3600），也可以整个关掉。第 N 轮的等待是首轮的 2 倍（默认 3 次 = 120 / 240 / 480 秒）。建会话被业务拒绝（非 0 code）不再试，只有网络/5xx/408/429、沙箱未就绪、会话超时这类瞬时失败才继续；成功与失败说明都会写明实际试了几轮。

## v1.7.6

积分防护与客户端后台流量控制安全更新：针对桌面客户端内置路由悄悄调用硬编码付费模型消耗积分的问题，引入后台模型透明改写重定向；针对 Anthropic Messages 协议转换层在长上下文下可能存在的缓存复用异常与积分剧烈消耗风险，将该入口调整为默认禁用（安全闭环），仅保留显式环境变量选择启用（#363、#369）。

- **桌面客户端后台模型重定向（免费替代）**（#363）：桌面客户端（如 Codex 等）内置路由将记忆生成、环境分析、自动化等后台任务硬编码发往 `gpt-5.6-terra` / `gpt-5.6-luna` / `gpt-5.3-codex` 等付费模型，UI 选择免费模型亦无法控制。网关在 POST 全局分发入口（选账号前）拦截名单内的后台模型并原子重定向至 `WB_BACKGROUND_MODEL_TARGET`（默认 `deepseek-v4.1-flash`），其余请求体原样透传并记录审计日志；支持环境变量自定义名单或置空禁用。
- **Anthropic Messages 接口默认禁用与安全闭环**（#369）：鉴于协议转换层未向 WorkBuddy 上游透传 `cache_control`，在长上下文会话（如 Claude Code）中容易出现缓存失效率高、剧烈消耗积分的问题（#367），暂时对 `/v1/messages`、`/messages` 及计数端点采取安全策略——默认直接返回 HTTP 400（`invalid_request_error`）拒绝调用并阻止转发上游；知晓风险的用户可通过显式设置 `WB_ENABLE_ANTHROPIC_MESSAGES=1` 开启。
- **套件与稳定性验证**：新增 `tests/_test_background_model_alias.py`（7 项专项测试），更新 `tests/_test_anthropic_http.py` 回归覆盖默认拦截与显式启用路径；套件总数由 148 扩充至 149（110 Python + 39 JS）。

## v1.7.5

全新数据看板与设计升级：数据看板页全面重构为现代化 Bento 布局并换装 Claude 暖色调主题；网关模型清单重磅升级——新增手动「刷新模型清单」按钮（POST /catalog/refresh）与每日自动 API 获取兜底，模型列表扩展为 8 列（新增官方名称、采样参数与思考注记）并配备数据来源警示横幅与原始目录存档（catalog/）；首页价格卡片改造为优先显示实际积分消耗并附带公网估算价值（#358）。

- **数据看板（Analytics）整体重构为 Bento 布局**（#357）：
  - **KPI 核心大卡**：Token / 请求 / 速度 / 缓存 / 花费五张总览卡，内嵌 sparkline 迷你趋势线与环比徽章，Token 卡集成输入/缓存/输出/思考四色比例流条；
  - **Token 时序可视化升级**：细柱状图与次级折线双序列，支持 Token / 请求数 / 吞吐量指标切换、悬停明细图例与多维度时间范围筛选；
  - **工作回溯**：引入 GitHub 式的年度工作活跃贡献热力图；
  - **用量多视角下钻**：支持按模型、账号、API Key 三视角切换与分布钻取（模型→各账号、账号→各模型，环形占比图与前 8 项分布分析）。
- **全局面板换装 Claude 暖色主题**（#357）：
  - 采用象牙白（#f7f5ef）、暖黑（#1f1e1b）与陶土红（#c96442）的现代温和调色板，衬线标题与卡片视觉全面重塑；
  - 控件、表格、徽章、弹窗与滚动条收敛至一致的 CSS 语义变量；网关页三张统计卡与调度器状态合并为统领式的「系统概览」区块。
- **网关模型清单主动刷新与来源追溯**（#360）：
  - **手动刷新模型清单**：模型清单标题行新增「刷新模型清单」按钮（`POST /catalog/refresh`），支持点击立即从上游官方 `GET /v3/config` 拉取最新目录，两区隔离且带 30 秒防抖保护；
  - **每日自动获取兜底**：后台守护线程对超过 24h 未通过 API 获取到目录的版本自动补拉；
  - **数据来源警示横幅**：通过 API 获取展示绿色状态条（带刷新时间与出处），未经 API 刷新展示黄色警示条，杜绝倍率信息滞后；
  - **模型表扩充至 8 列**：新增官方模型名称（带官方描述 tooltip）、默认采样参数（温度 / top_p）与细化思考档位注记；
  - **原始目录落盘存档**：成功拉取后将原始 JSON 响应原子写入 `catalog/product-config-<realm>.json` 备查。
- **首页用量统计支持真实积分消耗与估算价值并显**（#358）：首页卡片大字展示当日累计扣减的真实积分，副标题小字保留公网市场估算价值参考，多语言字典补齐相应词条。
- **套件扩充与全量测试**：新增 `tests/_test_catalog_refresh.py`（27 项）与 `tests/_test_models_catalog_source.js`（35 项），套件总数由 146 扩充至 148（109 Python + 39 JS）。

## v1.7.4

系统健壮性与多平台全面增强：新增可选的 SQLite 日志落库与模块级别过滤；macOS/Linux 支持手工导入桌面端解码密钥与一键配置权限安全强化（0600）；修复用量聚合旧小时桶剪裁误废 checkpoint（首屏恢复至 10ms 级）；面板首屏加载解耦成长任务并支持独立账号页切版本；模型目录自动隐藏上游未定倍率模型并修正 glm-5.3-flash 纯文本标注；修复 /agents/apply 无效 key 抛 NameError（#352）。

- **可选的本地日志落库与级别模块分类**（#338）：设置页新增「本地日志落库（SQLite）」开关（默认关闭），开启后写入 `usage/logs.sqlite3`，重启崩溃仍可回看；支持按天数/体积/行数三维保留轮转；重写日志级别与模块推断逻辑（补充丰富中文关键词与细分模块，错误日志补充账号）。
- **macOS / Linux 凭据导入与多平台适配**（#350）：扫描到桌面端加密凭据后支持在前端弹窗手工输入或导入 keyblob 密钥，解决非 Windows 无法读内存导致账号数恒为 0 的问题；启动脚本与 README 澄清便携包 Python 运行时为 Windows 专属，macOS/Linux 回退系统环境。
- **一键配置客户端凭据权限强化**（#349、#345）：写出客户端配置与凭据文件在 POSIX 系统下强制指定为 `0600`（当前用户独占读写），修复 DeepSeek Harness 等客户端在 macOS / Linux 启动时安全校验失败的问题。
- **修复用量分析 Checkpoint 误判废**（#342）：修复旧天小时桶被剪裁时导致 `_usage_cache_analytics_day_ok` 校验失败整份判废的问题，首屏从全量重折（2~3 秒）恢复至缓存秒开（~10ms）。
- **面板首屏加载性能与独立账号页切版本**（#346、#347）：
  - 启动链解耦：进入看板首屏时，国内版耗时约 2 秒的成长任务接口不再阻塞当前页签的关键 KPI 渲染；
  - 独立账号页视图切换：当开启「账号独立成单独页签」后，账号页顶部补齐国际版/国内版视图切换器，无需切回网关页。
- **用量聚合保留窗口可视化配置**（#348）：设置页新增日桶与小时桶保留窗口配置项，支持在磁盘体积与毫秒级求和跨度之间按需调节。
- **模型目录自动过滤与视觉能力修正**（97925e9、414f204）：
  - 自动过滤隐藏上游尚未在计费元数据中配置倍率的模型（如 `gpt-6.1-sol`），直到上游正式下发倍率后再自动透出；
  - 针对上游元数据宣告多模态但实际通道不支持读图的 `glm-5.3-flash`（#315），网关强制修正为纯文本模型，并在看板模型清单中展示「文本 (实测无视觉)」醒目标注与悬停说明。
- **修复 /agents/apply 传不存在的 key_id 报 NameError**（#352）：补齐 `Handler._agents_resolve_key()` 的 `wb_agents = agents_module()` 懒加载获取，避免抛出未定义异常导致 500。

## v1.7.3

性能飞跃与协议闭环：SSE 流式按块处理与出站 TLS 连接池化（高并发流式 CPU 消耗降低 76%、系统调用降低 98%）；闭环 Codex 0.162+ 命名空间内自由格式工具调用；面板上线超长窗口限高与吸顶；设置页开放上游响应头超时与国际版每日活跃模型指定；修复 macOS 启动脚本因换行符报 bad interpreter 与用量索引缓存时序问题。

- **流式链路系统级性能优化：SSE 逐行读取与写出改为按块处理，出站 TLS Context 进程级复用**（#333）：
  - **按块读取**：新增 `iter_sse_lines()`，以 64 KiB 块一次性取回底层 socket 缓冲并在内存切行，取代原先每一行一次 read() 系统调用的模式，大幅降低多并发线程争夺 Python GIL 的交接开销；
  - **按块写出**：在下一次可能阻塞的读取之前批量写出缓冲事件，将零散频发的单个 chunk `sendall` 合并为批量写入，系统调用大幅减少，且首字延迟（TTFT）与输出字节流完全不变；
  - **出站 TLS Context 共享**：实现进程级共享 SSLContext（懒加载），避免每次 HTTPS 建连重复解析 121 张系统 CA 证书（节省 ~20ms），并完整保留 TLS Session Resumption；
  - 实测表现：每请求 CPU 消耗降低 76%，总系统调用次数降低 98%（新增测试套件 `tests/_test_sse_read_batching.py` 与 `tests/_test_shared_ssl_context.py`，套件计数 143 → 145）。
- **闭环 Codex Desktop 0.162+ 命名空间内自由格式工具调用**（#310）：跟进修复 #308，在新版 Codex 中自由格式工具被嵌套在 `functions` 命名空间或整份下发在 `input.additional_tools` 时，回程侧递归识别命名空间内 `custom` 工具并统一出站与回程工具清单，彻底解决调用发出去无返回、无限重试的问题。
- **面板窗口限高与标题行吸顶**（#314）：长区块（如签到记录与长日志）超高时在窗口内部滚动且标题行吸顶，可在看板「设置」页自定义限高（默认 640px，0 为不限）；新增测试套件 `tests/_test_section_max_height.py` 与 `tests/_test_section_max_height.js`。
- **设置页开放上游响应头超时配置**（#318）：看板设置页直接暴露 `upstream.header_timeout_seconds`（默认 120s），方便应对极端大 Prompt 或慢网络场景下的客户端首字超时控制。
- **国际版每日打卡优化**（#319、#325）：
  - 网页通道打卡失败时如实记录为失败，不再误标为成功；
  - 网页打卡会话补发 `session/set_model`，确保打卡真正生效于轻量模型，并可在设置页自定义打卡所用的轻量模型。
- **最近请求索引改走安全尾部直读**（195753c）：默认关闭易受容器时序影响的 `usage-recent-index.json` 缓存索引，改走尾部流式直读，确保新请求永远绝对实时显示且杜绝卡在旧时间。
- **修复 macOS 启动脚本换行符与执行权限**（#329）：Release 便携包打包阶段对 `*.sh` 与 `*.command` 强制归一化为 LF 换行并赋予 `0o755` 可执行权限，彻底解决 macOS 双击脚本报 `bad interpreter: /bin/bash^M` 的问题；仓库根目录新增 `.gitattributes` 统一跨平台换行规范。
- **日志与用量套件稳定性增强**（#317、#320、#321）：出口瞬时 hiccup 日志补充底层具体断连原因；修复用量与限高测试用例在部分环境下的跨用例残留与 CI 偶发抖动。

# 版本更新记录 (Changelog)

已发布版本的完整记录；开发中的改动先记在 [README](../README.md) 的「版本更新记录」段，发版时整理成新版本号移到这里。

## v1.7.2

功能补全与深水区性能重构：补齐 Anthropic 协议下的思维链推理与 Codex Desktop 0.162+ 工具调用，兼容国际版桌面客户端凭据导入；数据看板用量系统迎来四项核心性能优化（闭窗口走桶、bytes 级预过滤、冷窗口免重折、状态指纹收窄为真正用到的价），并修复若干已知异常。

- **针对 Anthropic Messages 协议补齐 Thinking 思考过程转译**（#309）：上游（DeepSeek 等）在返回 `reasoning_content` 时，针对 Claude Code / Anthropic SDK 等客户端自动转换为 Anthropic 原生 `thinking` 块（流式下发送 `thinking_delta`），使 Claude Code 能正常渲染思考折叠框。
- **兼容 Codex Desktop 0.162+ 的额外工具注入规范**（#308）：Codex Desktop 新版将工具定义下发至 `input` 数组内部的 `additional_tools` 条目中，网关增加对该条目的提取合并，彻底解决新版 Codex Agent 丢失工具无法读写文件和执行命令的问题。
- **修复流式中间分片空字符串 `finish_reason` 导致严格客户端断流问题**（#296）：在流式清洗层将上游偶发的规范外 `` 安全规范化为 `None`（JSON 中的 `null`），避免 Rust/Go 客户端首帧提前截断。
- **国内版任务与福利展示优化**：卡片明确区分「本期 Buddy 加油站活动」周期与「WorkBuddy 成长计划」连续登录天数，且账号列表拆分展示加油站领取与成长计划签到双状态；修复已签到时上游返回 400 被误记为失败的问题。
- **修复：模型以文本形式打印工具调用时自动恢复为标准结构化调用**（#297）：DeepSeek 等上游模型在工具调用时，有时不发结构化的 `tool_calls` 字典，而是直接在正文里吐文本标签（DSML / <function=...> 伪 XML），导致客户端认为调用提前结束或无法触发工具。现在流式层增加兜底解析器，将文本打印的工具标记自动恢复为标准的结构化事件并剥离正文噪音；新增 `tests/_test_dsml_chat_fallback.py`。
- **修复：国际版桌面客户端（`WorkBuddyAI.exe`）的加密凭据无法扫描导入**：`wb_atrest` 把客户端配置目录写死为 `~/.workbuddy`、把主进程名写死为 `workbuddy.exe`，而国际版安装布局把 keyblob 放在 `~/.workbuddy-ai`、主进程叫 `WorkBuddyAI.exe`——两处都对不上，「扫描桌面客户端账号」报「keyblob 不存在」或「没有发现正在运行的 workbuddy.exe」。现在 `config_dir()` 按 `CONFIG_DIR_CANDIDATES`（`~/.workbuddy-ai` 优先、`~/.workbuddy` 兜底，与 `wb_proxy.read_product_config_models` 读产品模型目录的口径一致）探测 keyblob 实际所在；`_main_pids()` 按 `DESKTOP_EXE_CANDIDATES`（`workbuddy.exe` / `workbuddyai.exe`）选举主进程，找不到时报错文案列出全部候选名。`WORKBUDDY_CONFIG_DIR` / `CODEBUDDY_CONFIG_DIR` 环境变量优先级不变，便携版照旧。`tests/_test_desktop_atrest.py` 新增 `ConfigDirProbeTests` 与 `MainPidElectionTests` 共 10 项（套件 26 → 36）：钉住两种布局的探测与优先序、环境变量最高优先、两种 exe 名的进程选举与大小写不敏感、无客户端时的报错文案。
- **闭窗口（自定义区间两端都填）对齐走桶：`/usage`、`/usage/analytics`、`/usage/timeseries` 的闭窗口不再整段扫日志**：此前只有 `until` 开放的窗口能借用存量桶/环，`until` 一给出（面板自定义区间两个输入框都填）就退回整段扫描——真机（47k 行）闭 30 天 **13 337ms**、闭整点 7:00-8:00 **1 072ms**、timeseries 闭窗口 **1 101ms**。现在闭窗口 `[since, until]`（**两端都含**，口径未动）在两端都落在同一套格点上时按「`at < until` 的那批桶」求和：日格点走日桶、小时格点走小时层、`step=60` 的窗口走分钟环，三条路都不再读日志。桶按 `at` 严格切分，切不出「恰好落在 `until` 上」的行，所以折叠时给整秒行记一笔**边沿账**（真实日志的 `at` 是浮点秒，几乎为零；手改过的日志超过 256 条就停记并留痕），查询时那一刻没有整秒行才走桶，否则退回整段扫描（正确性优先，结果与改动前逐字节一致）。边沿账随三份状态落盘（checkpoint 加 `hits`/`hits_overflow` 两个键，schema 不变）；旧条目没有这两个键会被校验器拒 → 升级后第一次刷新一次性重折（与 v3→v4 缺小时表时的处置一致）。逐字节对拍：同窗口「桶求和」vs「整段扫」在边界行（`at` 恰好 = `since` / = `until` / = 整点 / = 午夜）上全部相同；新增测试套件 `tests/_test_closed_window_buckets.py`。
- **未对齐窗口的整段扫描：窗外的行不再 decode（bytes 级预过滤）**：起点不落在本地午夜/整点/分钟格点上的自定义区间走 `_scan_usage_snapshot_window` 一族——整段读日志、逐行 `decode + strip`，再由 `_line_outside_window` 判窗口。窗外的行只是不折，decode 照付；而窄窗口在几万行的日志上把绝大部分行都判在窗外，decode 是这条路径上唯一能整段省掉的开销。现在同一道判定先跑一遍**未解码的行**（新增 `_line_outside_window_bytes`，`_scan_usage_from` 新增 `skip_raw` 参数，六个窗口读取器都传）：能按字节读准、且整行 ASCII（decode 必然成功）的窗外行连 decode 都不再付；读不准的行——非首键 at、重复 at、整数 token、转义引号、坏字节、非 ASCII——一律返回 False，照旧解码后交给文本判定，保守语义一条不改（字节版说"窗外"的行，文本版也必须说"窗外"，反之只许出现在"字节版认不出"那一侧）。本机（11.7MB / 21445 行，与其他任务共享的机器，多轮取最小）：26 分钟未对齐窗口 `_scan_usage_snapshot_window` 91ms → 84ms、端到端 `usage_snapshot(custom)` 86ms → 82ms，10 小时窗口 90ms → 87ms；同窗口 decode 次数 **21445 → 352**（`json.loads` 次数与旧路径完全相同），输出逐字节不变（31 场景冻结时钟跨树对拍 0 差异）。新增测试套件 `tests/_test_usage_bytes_prefilter.py`（25 项：27 条 TRICKY 形状逐条对拍、十三个读取器开/关逐字节对拍、decode 计数桩、坏字节/半行/跨块行/窗口边界）。
- **冷状态的窗口请求不再付全量重折（T14）**：价表**内容**变化会让带价格指纹的状态（snapshot×3 + analytics×2 共 5 条）判废，此前首个窗口请求（`/usage` 的 `range=today/week/month`、自定义区间）会先把整份日志从零重折一遍（真机 47k 行实测 15.1s）才答得出窗口——预热器每 2 分钟才吸收一次，撞上就卡。现在窗口路径发现状态冷（从没折过 / checkpoint 采纳失败 / 刚被价表、realm、日志判废）时不再折状态，直接走既有的按日/按小时分组的冷扫描（`_scan_usage_snapshot_window_days` / `_hours`、`_scan_usage_analytics_window_*`，~1s 量级，窗口外的行挡在解析之前）；全量重折留给 all-time 视图（`/usage` 不带 range、analytics 的 all-time 半——它们本来就是载荷的一半）与预热器，重折照常发生，窗口请求也不会把状态冻在冷态。冷扫描与热态桶求和逐字节相同（既有承诺，本批新增套件重新对拍，含与闭窗口走桶的共存用例）。analytics 的窗口半同理（端点仍照折 all-time 半）；series 不在价表失效范围内（折叠不算价、没有 all-time 重建入口），行为不变。新增 `tests/_test_cold_window_scan.py`；README 套件计数 136 → 137。
- **重折成本有界：状态指纹收窄为「折叠行真正用到的价」（T15）**：价表**内容**一变（哪怕只是新增一个无关模型的价格），此前 snapshot×3 + analytics×2 五条状态全部判废、各重折 12-15s（真机 47k 行 60-75s CPU，由预热器吸收）；但已折行的成本在设计上就是历史价（带 `cost_policy` 的行按不可变策略计价，无引用老行由时间线解析——"a later price change cannot rewrite yesterday's totals"），所以绝大多数重折是白付。现在状态指纹从「(policies, timeline, pricing_doc) 三张表的对象身份」收窄成一份**「用到的价」记录**（`state["pricing"]`，checkpoint 的 `"pricing"` 字段）：`refs` 记每个解析出来的 pid 的策略内容摘要（显式引用 + 时间线解析；折叠时查不到的 pid 按「必须继续缺席」记）；无引用且成本非零的行另记时间线依赖（`timeline_at` 上界 + 模型集 + `backfilled` + 整条时间线都没提到的 `unresolved` 模型），判定侧 `_pricing_used_unchanged` 从当前价表重算同一份投影摘要逐项比对；用到内置快照时记内置价目表文档摘要。判定收窄只影响「要不要重折」，不影响任何数值（同日志收窄前后折出的载荷逐字节相同，新套件对拍钉住）；**行级结果记忆**（`_row_cost_remember` 的整表代次判定）、series（不算价）与全部聚合口径未动。checkpoint schema **4 → 5**（旧条目的 `"pricing"` 字段是 `["on", d1, d2, d3]` / `["off"]` 旧形状，采纳端一律拒 → 升级后第一次刷新一次性重折，与 v3→v4 同处置）。已知保守方向残留（文档化）：零成本无引用行（错误行、以及「先写日志、后来才补价」的未定价行）不进时间线依赖，它们的 `known` / `cost_missing` 标记（以及未定价行日后被补价时的 `cost_cny`）可能滞后——只在「模型价从表里出现/消失、且没有别的作废原因」时显现，等下一次自然重折补上。新增 `tests/_test_pricing_used_fingerprint.py`（对拍 + 作废矩阵①-⑦ + checkpoint 往返与旧格式被拒 + 计数桩）；README 套件计数 139 → 140。

## v1.7.1

日常维护与界面回归修复：彻底统一任务与福利卡片与网关账号列表的状态判定（时区与来源对齐），并修复设置页更新卡片标签闭合错误导致的页面排版溢出问题。

- **任务与福利页面打卡/签到卡片状态向网关与账号看齐**：彻底移除任务卡片原先使用的 `new Date().toISOString()` UTC 字符串匹配（东八区凌晨 0 点至 8 点会被严重误判为昨天）；完全复用网关与账号的 `activityStatusOf(a)` 核心判定，同一套事实来源（优先取今日活动流水记录，无流水时回落到 `todayStamp` 本地日期匹配），两边状态始终实时同步。
- **修复设置页「更新」卡片缺少外层标签导致闭合溢出的 bug**：补齐 `<div style="display:flex;...">` 开标签，解决因为提前闭合导致设置页底部的「更新检查」和「运行信息」常驻溢出到其他页面底部的排版问题。

## v1.7.0

一次跨派发、用量看板与客户端接入的大版本：出口与派发层重做失败归账（风控 403 分流换号、出口级秒级退避、单请求只记一笔、一键切换所有出站身份），用量看板完成冷路径优化与 checkpoint 新布局（一类一份、小时/分钟层、乱序采纳修复），面板补齐 token 紧凑单位与剩余积分两维度，账号可设权重与备注，另新增配置器 CLI 与 ZCode 客户端接入、网络工具多引擎轮换，以及若干协议修复。

- **设置页补上「出口级瞬时失败退避」开关**：`egress_backoff_enabled`（#292 引入，默认关）此前只能改 `accounts/settings.json`，面板上没有入口——功能等于打不开。现在「设置」页有勾选框与「保存」，走 `/settings/save` 的严格布尔校验（字符串 "false" 会被 400 拒掉，不会静默当真值）；英繁词典一并补齐。
- **账号手动权重与备注**（#289）：账号表新增「权重」列（下拉 1-10，默认 1）——权重 >1 的账号优先于一切自动偏好接单，多个高权重账号之间按权重平滑加权分流量（nginx 同款算法，谁也不独占），它们都不可用时照旧落回自动偏好与轮询；全部为 1（默认）时分派与从前完全一致。另有账号备注（点「+ 备注」编辑，空串清除，上限 200 字），纯展示、不参与调度，随凭证档持久化；账号列表排序改为「有备注置顶 → 权重高在前 → 同权重启用在前」。新增 `tests/_test_manual_weight.py`、`tests/_test_account_note.py`、`tests/_test_account_weight.js`。
- **修复：`stop` 传字符串的请求一律被上游 400（code 11101）**：OpenAI 把 `stop` 声明成 `string | array`，客户端传单个字符串是合法请求；本上游的 Go 端把该字段定死为 `[]string`，收到字符串直接 `Unmarshal chat params failed with error: json: cannot unmarshal string into Go struct field Request.stop of type []string`。网关此前原样透传，于是按 OpenAI 规范写的客户端在这个字段上根本发不出去（实测一个 Go 客户端连续 622 次全部失败，横跨 11 天）。现在 `build_upstream_body()` 在出网关前调用新的 `normalize_stop()`：单字符串归一化成单元素数组（语义等价——OpenAI 里单字符串就是单元素数组），数组形式过滤掉空串与非字符串元素，空字符串 / 空数组 / `null` / 非法类型一律删字段（它们的实际含义都是「没有停止序列」，而空值在上游同样会被判参数不合法）。
  - `tests/_test_stop_normalization.py`（新套件，17 项）：钉住字符串包装、数组保序与过滤、六种空值 / 非法类型的删字段、无 `stop` 时不发明字段，以及 `build_upstream_body()` 端到端的四种形状；README 套件计数 125 → 126。
- **修复「一次请求给同一个账号重复记账」**（issue #271）：重试轮在遇到传输类失败后会清空 `tried` 整池重轮，于是同一个账号会被再挑一遍、再记一笔 `fails`/`degrade_count`。池子越空重轮越频繁，一个刚抖了一下的账号因此可能**一次请求就被推过熔断阈值**（两次抖动触发 3 次硬失败的断路，而不是三次）。现在一次请求对一个账号最多记一笔：`open_upstream()` 用 `charged` 集合记住这一轮已经记过账的账号，重轮再挑到它时不再重复计数（`note_error()` 的冷却语义不变）。
  - `tests/_test_pool_backoff.py` 新增 `RetryRoundChargeTests`：钉住传输类与 502 两种失败下「一次请求只记一笔」，以及这道护栏**不跨请求**（第二个请求仍然照记，否则会掩盖真正在失败的账号）。
- **新增「出口级瞬时失败退避」（默认关，issue #271）**：账号级熔断把「连续 3 次硬失败」记在**账号**头上（30 分钟起、翻倍到 6 小时）。当失败来自出口 / 链路的**瞬时抖动**而不是账号本身时，这个口径会把一次抖动记到每个共享该出口的账号头上——现场实测 4 小时 196 次 `URLError` 陆续熔断 13 个账号（最长窗口约 3 小时），期间池子几乎空掉。打开 `egress_backoff_enabled` 后，**传输类失败与 502/503/504** 不再喂账号的 `fails` / `degrade_count`，改记在**出口**上：同一个出口的账号共用一个计数器，退避降到**秒级**（1 秒起、翻倍、封顶 15 秒、带抖动），任一次成功即清零。出口身份用 `wb_upstream_pool.egress_key()`——与连接池键同一口径 `(scheme, host, port, 代理串)`，并把「配了代理但命中系统旁路」折叠成直连，所以「直连」本来就是一个共享出口。**500 仍留给账号**（可能是与凭证相关的真实故障，需要再看一眼）；401/403/429 各走原有路径。单次请求的睡眠封顶 5 秒（`EGRESS_SLEEP_MAX`），共享计数器仍让并发请求一起变慢。
  - 开关缺失键读作**关**：它改变的是派发路径上的失败归账与退避尺度，老安装保持原样（传输错误仍喂账号熔断）。
  - 新增 `wb_egress.py` 与 `tests/_test_egress_backoff.py`：钉住出口身份与池键同口径、旁路折叠、秒级阶梯与封顶、共享计数器、成功清零、静默窗口重开阶梯，以及开关开 / 关两种口径下账号计数的差异（含 500 仍记账号）。
  - `release/portable_runtime.py` 的 `REQUIRED_FILES` 补 `Lib/random.py`（`wb_egress` 用它做退避抖动）；`release/portable.txt` 加 `wb_egress.py`。
- **数据看板：token 紧凑单位、缓存 token、总计花费、延迟自动单位**（#287）：两张 token 卡的大数字与输入/输出/思考明细改为紧凑单位（`fmtCompact`：<1 万精确千分位，≥1 万 K、≥100 万 M、≥10 亿 B，小数位随量级收缩、尾零去掉）；明细行新增「缓存」一段，缓存命中率卡副行带缓存 token 数；「API 等价花费」卡副行给出总计（全部历史）；KPI 的平均首字延迟 / 平均耗时与最近请求表的耗时 / 首字列改用自动单位；账号透视表与 Key 表的 token 列同步。新增 `tests/_test_token_units.js`。
- **数据看板：剩余积分分「总剩余 / 可用」两个维度**（#286）：区域卡片的大数字保持「总剩余」（全部账号之和，含停用账号），副行新增「可用 N」——只算启用且未触发保留积分 / 日限额 / 积分限额护栏的账号（与旁边的 N/M 账号计数同一个口径）。没有积分数据时两个维度都显示 —，不会把可用误显示成 0。新增 `tests/_test_credits_dimensions.js`。
- **修复：上游风控限制 403（code 11140）不再被当成内容审核**（#288）：账号被上游限制时（正文「账号存在异常行为，已被限制使用」/「restricted due to unusual activity」），原先落进内容审核分支——降级提示词重试一次后就把错误透传给客户端，被限制的账号既不冷却也继续被选用，同一条对话被一次次打死。现在按账号级限制处理：冷却该账号 2 小时并轮到下一个账号，上游解封后自愈；池里只剩一个账号时仍把限制原因交给客户端。内容审核 403 的降级路径不变。顺带把重试循环的 5xx 从固定清单（500/502/503/504）放宽为一切 >= 500——上游自定义的 550 这类原先会直接甩回客户端，现在按瞬时故障轮转。新增 `tests/_test_restricted_403.py`（6 项）。
- **修复：`remaining` 的 checkpoint 从来没能被采纳（写者/校验者口径矛盾）**：折叠侧按日志顺序追加，而 `at` 取的是请求**完成**时刻——多线程下会回退几毫秒（本机日志有一对 4.96ms，路由器自己写出的 checkpoint 里有一对 0.47ms），加载侧却要求每个 (账号 × 模型) 的 `at` 严格单调，一处乱序就整份拒绝：每次重启 `/usage/remaining` 都退回从零冷扫（真机实测 3.05~4.5s）。现在折叠侧按 `at` 有序插入（只在乱序时才付 O(n) 挪动，pair 通常几十~几百条；顺序追加仍是 O(1)），加载侧对旧版（日志顺序）缓冲做一次稳定排序并重建累计和/`base`（(at, 增量) 配对不变、已裁剪的死前缀物理丢弃、`base` 记绝对值），形状校验不再要求有序。同一处乱序在窗口求和上的连带错误一并修掉：`_range_sum`/`_range_sum_open` 的二分前提恢复后，乱序日志下的窗口和等于「按排序后重算」的值（旧版会少算、甚至算成 0）。新增 `tests/_test_remaining_checkpoint_order.py`（12 项：乱序日志的折叠与窗口和、乱序/旧版 checkpoint 的重启采纳与逐字节对拍、截断与同尺寸重写仍然作废、两个独立进程的冷折 vs 采纳对拍）。
- **本地网络工具（`web_search` / `web_fetch`）改成多引擎自动轮换**（[PR #290](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/290)）：旧的单引擎实现只走 DuckDuckGo HTML 版，上游对出口 IP 限流、返回反爬页时，解析不到结果就会把失败误报成「查无结果」，同一网关上的对话都只看到搜不到。现在同一句查询会按 `duckduckgo -> bing -> wikipedia -> stackoverflow -> google news` 自动轮换（只用国际来源；`WB_WEB_ENGINES` 可覆盖顺序与子集），常规引擎（DuckDuckGo / Bing）接手时结果与平常完全一致、模型无感；换成 Wikipedia / Stack Overflow / Google News 这类非常规来源时，结果明确带上 `Source: ...` 并要模型告知用户出处；全部引擎都不可用才回 `web search is temporarily unavailable`（附各家原因，并要求模型不要重试同一句、直接告知使用者）。
  - 「真的没有结果」与「被限流／拦截」分开分类：HTTP 202/403/429 或反爬页（DDG anomaly 页等签名）判为被挡，引擎进冷却（`WB_WEB_COOLDOWN`，默认 600 秒；其他引擎健康时跳过冷却中的，其他全挂时再补试一轮）；查询结果 TTL 快取（`WB_WEB_CACHE_TTL`，默认 600 秒；空结果 120 秒）＋同 key 併发去重＋对外最小间隔（`WB_WEB_MIN_INTERVAL`，默认 0.5 秒）＋单次搜寻总预算（`WB_WEB_BUDGET`，默认 25 秒）。
  - `web_fetch` 一并硬化：只收文本型内容（PDF 等二进位直接回明确错误）、支援 gzip 压缩、4 MB 读档上限、charset 依标头与 `<meta>` 推断、结果进快取；URL 守卫除原有字面检查外，会解析 DNS 并核对每个位址都是公网（防 DNS rebinding 把 fetch 指进内网）；新增 `WB_WEB_TOOLS_PROXY` 给这组工具单独指定出口代理。
  - 多引擎轮换、冷却与失败分类写进看板日志（`tag=webtools`）；测试：`tests/_test_local_web_tools.py` 从 68 项扩到 120 项（轮换、来源标注、冷却、快取与节流、失败分类、抓取闸门、DNS/IP 守卫，全部离线跑）；`release/portable_runtime.py` 的运行时契约补 `Lib/gzip.py`。
- **`/usage/perf` 加增量样本（稳态最大项）+ `_tail_lines` 逐块读取**：perf 是面板轮询里唯一没有增量的聚合——每次 TTL 过期都重读日志末尾 `sample` 行、逐行 `json.loads`、把五个数值往全局与三层模型桶各 append 一遍，再对每个桶排序求分位；真机 558ms/次、TTL 15 秒，面板开着就每 15 秒付一次。现在按 `(sample, realm, 窗口)` 各记一份**环形样本**（最近 `sample` 行 + 每行对聚合的贡献），TTL 过期只把新追加的行并进来、把滑出样本的行撤出去，0 新字节时连渲染都省掉；位置校验（`offset` + `(size, dev, ino)` + offset 前 64B tail 签名）不过才从日志尾部重建。本机（11.7MB / 21445 行）`perf_stats`：冷 220~250ms 不变，**TTL 过期后的重算 190~280ms → ~1ms**；`sample=20000` 的冷扫 800ms → 660ms。口径未动：`sample` 仍是"最近 N 行"（不是时间窗）、`sample_capped` 仍表示 tail 截断、realm/窗口预过滤照旧、分位始终从原始样本算（D6）、`sampled`/`sample_from`/三层桶的内容与顺序与整段重扫逐字节一致；状态**不落盘**（重启付一次冷扫），每进程最多留 4 份（面板实际只用两三个键；`sample=5000` 单份约 3.4MB）。顺带把 `_tail_lines` 的逐行循环收成"整块过滤 + 一次 extend"（字节口径不变，20000 行读取 1.2 倍左右），并在新套件里用 `open`/`json.loads` 计数桩钉住"0 新字节不解析、N 行只解析 N 行、读入字节数就是尾部那一段"。新增 `tests/_test_perf_incremental.py`。
- **用量两处小提速（输出逐字节不变）**：窗口序列的「credit 前 50 条」从「整窗计费行全量 append + 整份排序 + 切前 50」改成大小 50 的有界堆——进不了前 50 的行不再建载荷字典，20 万计费行的选取 170ms → 75ms（at 倒序、相同 at 保持日志顺序的语义与切片路径一致）；请求路径不再维护模块级 `_usage` 里没人读的 requests/ttft/gen/wall/by_model 累加（全仓唯一读者是快照的 `started`），每请求省一次全局锁与约 6µs 的加法，快照的 `started`/`since` 照旧。`import heapq` 已加进便携运行时清单（`release/portable_runtime.py`）。
- **`/usage/recent` 的行偏移索引**：分页从「从日志尾部往回读」改成按索引直接定位。新落盘 `usage/usage-recent-index.json`（每 64KB 记一条行边界 + 到该处的累计记录数，4 万行日志 4.5KB），页数标签与页内容都从同一次扫描里取；位置校验复用聚合那套（`key` + 64B tail 签名），截断 / 替换 / 同尺寸重写一律作废重扫，索引答不了（半行、坏字节、池没就绪）就整段退回原实现，两条路的输出逐字节相同。本机（11.7MB / 21445 行）：第 1 页读 100KB（旧 256KB）、第 200 页读 128KB（旧 11.0MB）、空页读 128B（旧 11.7MB）；带索引重启后的第一页 ~15ms（旧冷 133ms）。索引只在有推进时落盘（256KB / 900s 节流），首次构建要过一遍日志（约等于一次解析扫描）。开关 `WB_RECENT_INDEX=0`（另见 `WB_RECENT_INDEX_MIN_BYTES` / `WB_RECENT_INDEX_MIN_SECONDS`）。新增套件 `tests/_test_recent_index.py`。
- **行级结果记忆：日键与定价跨视图只算一遍**：`_local_day_key`（strftime）在 snapshot / analytics / series 三次独立扫描里各算一遍，`cost_for_row` 在 snapshot + analytics 各算一遍——本机 21445 行日志上分别是 63065 次与 42173 次真算。新增两张**进程内有界**的记忆表（日键按 `at`、定价按 `(model, cost_policy, at, prompt_tokens, cached_tokens, completion_tokens)`，键都不用行对象 id；定价条目带价表代次，换代整体作废；时区被 `tzset()` 换过则日键表作废；`WB_ROW_MEMO_MAX` / `WB_ROW_COST_MEMO_MAX` 可调，0 = 关）：本机三视图冷启动合计 **4269ms → 3609ms（-15.5%）**、指标页（第二视图）**1802ms → 1231ms（-32%）**，日键真算 -66%、定价真算 -39%。记忆只存「这行折进哪天/值多少钱」两个标量，五个聚合的折叠状态一行未动；不落盘、不共享状态。
- **聚合状态 checkpoint 一类一份：只跑 `/usage` 的进程不再解析全部状态**：落盘布局从「一个文件装五份」（`usage-aggregate-cache.json`，schema 3）拆成「一类一份」（`usage-aggregate-cache-<kind>.json`，schema 4），每份自带位置/指纹校验；加载端按需解析，写侧只重写有推进的那一类。本机（11.7MB / 21445 行；单视图进程逐入口独立跑 5 次取中位）：`/usage` 采纳 25.6ms → **12.4ms**、analytics 25.0ms → 13.1ms、by_account 15.8ms → 1.3ms、timeseries 16.0ms → 2.8ms；面板五视图全跑（同进程）合计 27.4ms → 21.0ms。顺带修掉旧布局的一条实锤破坏路径：任何一次 save 都会把当时**不活跃的类**写成空段、整份覆盖，盘上那些类的旧数据被清掉——一个进程里 remaining 重折一次，其余四段就没了，下次启动五类全部冷重折。同一类里按 realm 分的条目同理：写盘只重写活跃 realm 的那些，**盘上还没被本进程碰过的 realm 条目原样保留**（真机实测：不保留时，重启后先请求 `realm=all` 再请求 `realm=intl`，intl 要重折 15s；保留后两个 realm 重启都免冷扫）。旧单文件只读兼容：某一类还没有自己的文件时从旧文件里取它那一节（升级后的第一次刷新不必整段重折），类文件一旦存在就只认类文件；旧文件不重写、也不删除，等各类都落过一次盘就自然被忽略。新增测试套件 `tests/_test_usage_cache_kinds.py`。
- **面板自定义区间的整点窗口不再整段扫描**：面板「自定义」区间的起点是任意时刻，而输入框给的是分钟粒度——操作者选整点的概率远高于选 37 分。改动前只有「起点是本地午夜」的窗口能由存量桶（按日分桶）回答，整点窗口一律从头扫一遍日志（真机宽窗口冷扫 **20.5s**）。现在每个日桶里再挂一张小时子表（保留最近 2 天 = 最少 48 / 最多 72 小时，`WB_USAGE_HOUR_KEEP_DAYS` 可调；对 flash 敏感的部署可调到 1），整点窗口由「整小时桶之和」回答：token / requests / credit 与成本字段（`cost_cny` / `cost_missing` / `by_model_acct`）全部来自同一份桶，不借时间序列的小时桶（那层不算价，借了就会丢掉面板矩阵的成本列）。层与它自己的冷路径（按小时分组）逐字节一致；与改动前的逐行口径相比只有 `cost_cny` 的浮点末位差异（与日桶同一类，`WB_USAGE_DAY_BUCKETS=0` 可整体退回旧口径）。覆盖不到的窗口（起点非整点、起点超出保留期、`until` 落在小时中间）退回改动前的逐行口径，数字一字不变。本机（11.7MB / 21445 行）宽整点窗口 **782ms → 4.2ms**、窄整点窗口 **642ms → 3.3ms**；小时表随状态落盘（checkpoint 109KB → 187KB），旧文件因缺小时表被整体作废、升级后第一次刷新做一次全量折叠（一次性代价，与 v2→v3 同一条规矩）。
- **时间序列补分钟层：span ≤ 6h 的窗口不再整段扫描**：`/usage/timeseries` 的桶宽由窗口跨度决定，span ≤ 6h 给 `bucket_seconds=60`（面板自定义区间，起点是 datetime-local 给的整分钟），而日桶/小时桶都锚在本地午夜上，答不了这种任意分钟格点的窗口——这一档一直退回整段扫描（真机 6h 窗口 881ms 级，TTL 过期后每次轮询都要重扫一遍）。折叠时顺手维护「最近 6 小时的分钟环」（每行折进它 `at` 所属的 epoch 分钟，与日/小时桶同一 realm 过滤与折叠口径；保留窗口与选择器给出 step=60 的最大跨度一一对应，更早的窗口照旧退回扫描），窗口就是它的切片。启动条件：窗口起点落在分钟格点上、`hi ≥ 折过的最大 at`、起点不早于 `max_at - 6h`；任何一条不成立就退回整段扫描，输出与改动前逐字节一致（跨午夜、跨小时、空窗口、realm 过滤、显式/自动两种桶宽都钉住）。环随 series 状态落盘（紧凑位置数组，满环 361 桶约 19KB 上限，本机实测每桶 52 字节；本机整份 checkpoint +6.3%），新进程采用后窗口与冷折逐字节一致；旧版 checkpoint（没有分钟环）按「环只从采用点之后完整」处理，采用点之前的窗口退回扫描。新增测试套件 `tests/_test_usage_series_minutes.py`（74 项）。
- **analytics 的自定义区间窗口不再整段扫日志**：数据看板「自定义」区间的起点落在整点上时，此前每次都要从头扫一遍 `usage.jsonl`（路由器实测宽窗口冷 49.4s）；现在 analytics 在自己的日桶里挂了一层小时子表（analytics 的 token 口径含失败行，与 snapshot/series 不同，借不了它们的层），起点在整点上的窗口就是窗口内小时桶之和，毫秒级返回（本机实测 496ms → 2.9ms）。非整点的起点、窗口上界切在桶中间、小时被剪掉的天照旧退回扫描，结果不变；与改动前的逐行口径相比只有 `cost_cny` 的浮点末位差异（与日桶同一类）。小时子表只保留最近 48 小时（`WB_USAGE_HOUR_KEEP_HOURS` 可调），随聚合 checkpoint 一起落盘（本机实测 +122KB / +27%），旧 checkpoint 因缺 `at`/`hours` 被整体拒收、升级后 analytics 重折一次（一次性代价）。新增测试套件 `tests/_test_analytics_hour_buckets.py`。
- **修复「网页通道打卡」在认证代理下必然 407**：账号绑定带 user/pwd 的代理槽时，网页通道那一步固定失败并回 `OSError: Tunnel connection failed: 407 Proxy Authentication Required`，而桌面通道与面板「出口 IP 测试」都正常。原因是 `wb_webagent.AcpChannel._connect()` 用裸 `http.client` + `set_tunnel()` 直连代理，只传了 `host/port`：`urllib.request.ProxyHandler` 会替 urlopen 那条路径把 URL 里的凭据转成 `Proxy-Authorization: Basic ...`，而 `set_tunnel()` 不带这个头——于是同一个代理 URL 在两条通道上行为不一致。
  - 现在 `_connect()` 走 `_proxy_parts()` 解析代理 URL，把 `Proxy-Authorization` 显式塞进 CONNECT（取值口径与 `ProxyHandler.proxy_open` 一致：先 `unquote` 再 base64），两种写法都认：`http://user:pass@host:port` 与无 scheme 的 `user:pass@host:port`（后者此前连 host/port 都会被 `urlparse` 误判成 scheme，一并修正）；没填凭据时不发该头，免认证代理行为不变。
  - 新增 `tests/_test_web_agent.py` 的 `ProxyTunnelTests`：钉住两种 URL 形态的解析、转义字符与 urllib 同口径、无凭据不发头、隧道确实带上该头，以及直连时不走隧道。
- **修复「网页腿失败也写 lastDailyChat，当天积分静默丢掉」**：`_daily_chat_upstream()` 在桌面腿成功后立刻写 `lastDailyChat`，而网页腿（唯一计入官方「每日活跃」的那条）失败也照写；于是当天后续巡检 `can_daily_chat()` 直接跳过该账号，那天的 30/50 积分既不到账也不再重试，面板还显示「成功」。现在只有网页腿成功（或 `web=False`、没开网页通道）才写盘，失败时留到下一个巡检周期重试。
  - `tests/_test_daily_chat.py` 补三条：网页腿失败不写盘且 `can_daily_chat()` 仍为真、网页腿成功写盘、`web=False` 维持原语义。
- **新增「配置器 CLI」：客户端不在网关机器上也能一条命令接入**（#280）：看板的一键配置运行在网关进程里，只在网关本机、用 `127.0.0.1` 打开看板时可用（远程访问与容器 / OpenWrt 部署按设计拒绝），网关跑在服务器 / NAS / 路由器上时，客户端装在别的电脑就用不上。现在 `wb_agents.py` 同时是可执行的配置器——`list` / `status` / `apply` / `restore`，在**客户端所在的机器**本地写配置（与看板共用同一套写入、备份、还原逻辑），只通过 HTTP 从网关取模型表；网关侧新增 `GET /agents/cli` 下发该脚本（免面板鉴权，正文与仓库内 `wb_agents.py` 逐字节一致、不含机密）。`--dry-run` 与真实写入共用同一条 `plan()` 生成路径；Key 走 `--key` → `WB_PROXY_API_KEY` → 交互提示，提示仅在 stdin 与 stdout 都是终端时出现（避免没有控制台的进程里 `getpass` 永久阻塞）。
  - 顺手修「apply → apply → restore 把自产文件当原件还原」：按内容哈希判定归属——仍逐字节等于我们最后写入的 → `restore` 删除；被用户手改过 → 备份并恢复用户版本。此前含 API Key 的 `.credentials.yaml` 会留在磁盘上，与文档承诺相悖。
  - `release/portable_runtime.py` 的 `REQUIRED_FILES` 补 `Lib/getpass.py`（新增 import 后裁剪运行时缺该模块）；README 套件计数 119 → 120。
- **账号工具栏新增「一键切换所有出站身份」**（#254）：一次把**全部账号**（含另一个版本）的出站身份切到下一个——WB → VSC → CLI → 循环。按钮上直接显示当前身分，各账号不一致时显示「混合」，此时点一下统一成 WB。服务端 `/accounts/product` 本来就支持不带 `uid` 的全量切换（面板此前只有单账号的行内按钮，以及 429 时的自动换身分），这次补的是入口与提示；切完当场落盘，重启不丢。
  - `tests/_test_product_switch.py`（新套件）：钉住全量 / 按区域 / 单账号三条分支、非法身分被拒、落盘后重启仍在、重复切换不报变更；
  - `tests/_test_accounts_page.js`：钉住按钮位置、轮转顺序与「不带 uid」的提交形状，并在假 DOM 里真跑一遍。
- **补一道「冲突标记」门禁**：仓库里已经三次把解冲突的残留提交进 main（#131 专门清过一次 dashboard 的，README 又在 #243/#245 那批合并里带进 12~15 行），此前没有任何测试盯它——Python 文件会直接 `SyntaxError`，HTML/JS 则是静默坏掉，回滚检查还会把「合并时顺手整理掉这些行」算成弄丢 main 的改动（#245 那次报红就是这个）。`tests/_test_release_engineering.py` 新增 `RepoHygieneTests`：扫全树（跳过运行时数据与打包产物目录），任一行以 `<<<<<<<` / `>>>>>>>` / `|||||||` 开头即失败。
- **智能体一键配置新增 ZCode 客户端**：把网关作为 `wb-proxy` provider 写进 ZCode 的活配置 `~/.zcode/v2/provider_config.json`（`providerConfigRules.providerRules` + `providerOrder`）。规则按稳定 `providerId` 定位，重复 apply 更新同一条而非堆叠；其余 provider、它们的顺序与 `modelConfigRules` 逐字节保留。模型表来自 `GET /v1/models`，写成 `personalModelIds` + `modelOrder`。该文件由 ZCode 应用自己持有、运行中会被重写——卡片文案与文档都提示「配置前先退出 ZCode」。
  - `tests/_test_agents.py` 扩到 126 项：zcode 形状断言 + 「重复 apply 不堆叠 / 其他 provider 与 modelConfigRules 逐字节保留 / 还原字节级」。

## v1.6.20

面板回归修复、剩余用量估算的后两批、表头排序与页签开关，外加一处 Windows 双栈下的登录限流修正：

- **找回合并 #267 时被整块回退的四组面板实现**（issue #270）：`6cd1bc0` 是一次合并提交，解 `dashboard.html` 冲突时整块取了 #267 那侧（其基点比当时的 main 落后六轮），把上游同文件里的近期工作一起带掉了——#265 的整套表头排序、#261 的「今日活动」列与签到记录弹窗、#262 的设置页更新检查卡片、#264 的剩余用量优先调度设置，以及 #268 的「顶部页签显示」区块（它被挤进了运行日志页）。`git show --stat` 在合并提交上只跟第一父比、CI 又因为 #265 当年没配套件而放行，所以这是一次静默回退。本次以 `1642b93`（CI 全绿的那棵树）为基线重做合并、冲突处两边都留，并给页内「签到与活跃记录」看板那套函数改名（`*ActivityBoard`），让它与 #261 的弹窗并存而不互相顶掉。新增 `tests/_test_panel_subsystems.js` 钉住这四组子系统的存在与接线——把 `dashboard.html` 换回 `6cd1bc0` 那棵树时它会红。

- **剩余用量估算第二批 / 第三批**（[PR #264](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/264)，感谢 [@aodianjun](https://github.com/aodianjun)）：分段记账（撞线把窗口切成两段，「已用」只算本窗口）、按 (区域, 模型) 汇总总剩余、按需过滤（昨日 00:00 起既没消耗也没撞线的组合不参与）、免费模型 6004 的限额联动（写进凭证档、跨重启保持）、`usage/estimate-daily.json` 按天归档并按 6/3/1 加权估算，外加三处修复：`_pair_trim` 的累计和重基（旧版写下的坏 checkpoint 升级后自动作废重建）、事件日志跟随 `--usage-dir`、remaining 折叠接上 checkpoint 写盘。

- **剩余用量优先调度（默认关）+ 超限对话页内轮转与暖号回退**（[PR #263](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/263)，感谢 [@aodianjun](https://github.com/aodianjun)）：设置页新增「剩余用量优先调度」——某 (账号 × 模型) 的估计剩余越接近耗尽，分到的请求越多（先把快用完的额度用掉）；默认关闭，开启后接近耗尽的账号会明显多接流量、也可能更早撞到上游限额。对话页在账号撞限时改为页内轮转，并按 `WB_AFFINITY_PAGE_SIZE` 回退到暖号。

- **所有数据表支持点表头排序**（[PR #265](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/265)，感谢 [@LeoK77S](https://github.com/LeoK77S)）：任意数据表点表头即可按该列升序/降序，数字列按数值、时间列按时间、其余按文本；带 `aria-sort` 与「这些表不参与排序」的清单（`SORT_DENY_TABLES`），分区域渲染的表格也各自接上。

- **账号列表的「今日活动」列与签到记录弹窗**（[PR #261](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/261)，感谢 [@LeoK77S](https://github.com/LeoK77S)）：账号表每行显示该账号今天签到/活跃的结果（成功 / 失败 / 无记录），点开是它自己的签到记录弹窗，可按 区间 / 账号 / 任务 / 结果 筛选，数据读 `GET /activity/history`（一行一次真实尝试）。

- **设置页新增「更新检查」卡片**（[PR #262](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/262)，感谢 [@LeoK77S](https://github.com/LeoK77S)）：显示当前版本 / 最新版本 / 上次检查时间，可手动「立即检查更新」；另有「每天自动检查更新」开关（默认关闭，只检查、不下载不安装）。

- **IPv4-mapped 的回环对端也算可信代理**：Windows 双栈下连 `127.0.0.1` 的对端会显示成 `::ffff:127.0.0.1`，而 `ipaddress` 在 Python 3.13 之前不把 IPv4-mapped 地址算作回环——仓库声称支持 3.9，于是在这些版本上本机反代的请求会被当成不可信来源，登录限流退化成所有客户端共用一个桶（#243 修掉的那个问题）。只在回环判定上补一个分支，`TRUSTED_PROXIES` 的匹配口径未动。

- **账号页可选独立成一个菜单，并加签到与活跃记录看板**（issue #176）：账号多起来之后（国际版十几个），账号列表与「当前禁用」总览一直向下排列，把网关页的卡片与最近请求挤到下面——折叠只解决占屏，不解决「账号本来就是另一件事」。新增的「签到与活跃记录」读既有的 `GET /activity/history`，可按 **区间 / 任务 / 结果 / 账号** 筛选（筛选全部在服务端做，前端不再自己过滤一遍），一行一次真实尝试，列出 时间 · 账号 · 区域 · 任务 · 来源 · 结果 · 说明，最新在前，超过一页时用「加载更多」往下翻、上限 1000 行。设置页新增开关「账号独立成一个页签」：**默认关闭**，账号列表、「当前禁用」与签到记录留在「网关与账号」页（位置与拆分前一致）；打开后它们搬到独立的「账号」页，页签变成 网关 → 账号 → 数据看板 → 智能体配置 → 设置 → 运行日志，网关页只剩网关自己的东西。偏好存服务端（`accounts_separate_tab`）。新增测试套件 `tests/_test_accounts_page.js`（结构 + 行为）。

- **正體中文的「签」不再是「籤」**：`HANT_CHARS` 把 `签` 映射成抽籤的 `籤`，既有文案靠 `HANT_PHRASES` 逐条覆盖绕过了它，新文案绕不过去（区块标题显示成「籤到與活躍記錄」）。本仓库所有 `签` 都是签到/签名语义，抽籤那种用法一处都没有，所以直接修字表：`签 → 簽`；已有的逐条词条保持不变（先命中，结果一致）。

- **国内版自动连续打卡（对话活跃上报点亮连登）**：国内版每日签到只能领积分但无法推进官方成长中心的「连登天数」（官方只认真实会话行为上报）。现将国内版对话活跃上报接入调度器：每日定时（09:00/21:00）为国内版账号上报规范会话事件（复刻客户端 `chat_request_send`，严格携带 `userId` 并保持每号每天 1 次防风控口径），点亮成长中心连续打卡天数与热力墙；上报后只读查询并回显连登天数，同时持久化记录 `lastActivityReport` 避免重复调用；新增测试套件 `tests/_test_streak_report.py`（10 项）。

- **国内版连登管家与企业版门控**（口径参考 [linguo2625469/workbuddy2api-panel](https://github.com/linguo2625469/workbuddy2api-panel)）：把连登这一整套接进调度器——昨日漏签且有补签卡时自动补签（保连登）、7d/14d/28d 档位解锁即自动兑换（积分 / 能量 / 补签卡 / 抽奖次数）、有抽奖次数自动抽完，全程幂等（未解锁 / 已领过 / 没有次数都静默跳过）；同时按上游实测把企业版账号从成长体系里摘出去——企业号没有个人成长体系（`/activity/growth/*` 一律 403、签到 400 `code 10001`），签到 / 活跃上报 / 猫猫旅行 / 夜猫子 / 连登管家五类调用一律跳过，面板也不再给它渲染可签到标记；顺带补上新手礼包与活动补偿的领取（`claim-gift` / `claim-compensation`，每号一次、已领静默跳过）。

- **小响应不再白付 40ms、突发并发不再卡 1 秒**（[PR #237](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/237)，感谢 [@aodianjun](https://github.com/aodianjun)）：服务端关掉 Nagle（响应头与响应体两次 write 不再互相等 ACK，werkzeug/uvicorn 同做法），listen backlog 从 stdlib 默认的 5 提到 128（面板打开一页就是 ~7 个并发）。真机（OpenWrt / Celeron N2840）：`/health` 中位 50.0ms → 1.41ms，64 并发突发「卡 ≥1s」43/64 → 0/64。顺带把单请求体上限默认从 50MB 收到 16MB（`WB_MAX_PAYLOAD_BYTES` 可调回）——读 body 发生在 chat 信号量之前，路由器上几个并发大 body 就能把内存打穿。

- **面板轮询不再自己把自己堵住**（[PR #238](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/238)，感谢 [@aodianjun](https://github.com/aodianjun)）：`getJSON` 加 URL 级单飞（冷窗口时同一 URL 曾发 13 份、6 份并发，还把 `/accounts`、`/scheduler` 一起堵住）；日志页改成只 append 新行、最近请求渲染前比指纹跳过；英文界面的 i18n 观察者加 WeakMap 缓存；修掉会话失效后 401 轮询停不下来的 bug；`/v1/models?realm=all` 轮询从 5s 改为 10 分钟 TTL。

- **请求热路径提速**（[PR #239](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/239)，感谢 [@aodianjun](https://github.com/aodianjun)）：指纹判定从小写副本上的字面量 `in` 走（等价性按 `re.IGNORECASE` 的 Unicode 特例归一化后逐码点验证），`/v1/chat/completions` 不再重复构建上游 body，token 估算改单遍扫描，`wb_settings.load` 加 (path, mtime, size) 缓存（写入仍即时可见）。168KB 请求的 pre-upstream 合计 48.07ms → 18.50ms。

- **用量聚合最后两个全量读者改增量，外加三个 bug**（[PR #240](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/240)，感谢 [@aodianjun](https://github.com/aodianjun)）：`perf_stats` 加窗口预过滤（`range=today` 389~422ms → 211ms）、`count_usage_rows` 改增量折叠（319ms → 0.23ms，位置校验不过才从零重数）；修掉 checkpoint 空闲也整份重写（每 900s 白写 120KB）、`wb_agents.integrate()` 回滚分支的 `NameError`（多文件客户端写一半失败时既不回滚也不写 state）、两处死代码。

- **OpenWrt 预热器覆盖面板真正轮询的接口**（[PR #241](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/241)，感谢 [@aodianjun](https://github.com/aodianjun)）：预热清单 5 → 20 个目标（补上带 range 的 `/usage`、`/usage/perf` 与 `realm=intl|cn`、by-account、timeseries 四个窗口），服务启动后后台预热一次，预热 cron 由 `*/12` 收紧到 `*/2`（缓存命中不延长 TTL，间隔必须显著小于 TTL）。

- **按 API Key 归属表可收起「(切换前)」行**（[PR #242](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/242)，感谢 [@LeoK77S](https://github.com/LeoK77S)）：表头新增与「启用价估算」同款的开关，默认显示、偏好存服务端；同时「启动参数」只在日志里确实出现过它的用量时才占一行（面板有 key 时它恒为 0）。

- **登录限流按真实来源 IP 分桶**（[PR #243](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/243)，感谢 [@aodianjun](https://github.com/aodianjun)）：反代部署下所有浏览器与缓存预热器共用一个 `127.0.0.1` 桶，一个人连错 5 次就把所有人锁 60 秒。现在只在对端可信（回环或 `WB_TRUSTED_PROXIES` 列出的代理）时才读 `X-Real-IP` / `X-Forwarded-For`，否则仍按对端地址分桶——公网客户端伪造头换不了桶。阈值、窗口与会话语义一行未动。

- **上游连接复用**（[PR #245](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/245)，感谢 [@aodianjun](https://github.com/aodianjun)）：urllib 写死 `Connection: close`，每个请求都要重新 TCP+TLS 握手（真机裸握手 TCP 55.5ms、TLS 118.0ms，是请求路径上最大的一笔）。新增 `wb_upstream_pool.py`：按（目标, 代理串）分池复用连接，每键最多 2 条空闲、LIFO、90s 回收、取出探活、复用前重置读超时、只在 body 读到自然结尾时归还。真机 A/B：14 条流式请求的 TCP 连接 14 → 1，loopback p50 19.9ms → 13.3ms、经真实 RTT 链路 36.2ms → 20.4ms。只在「还没发出请求体 / 还没读到任何响应字节」时安全重试一次；非 http(s) 目标、非 HTTP 代理、3xx 一律回退原路径，`WB_UPSTREAM_KEEPALIVE=0` 可整条关掉。

- **每日活跃打卡的日志带上网页通道结果**（issue #236）：国际版打卡分两步，桌面端那条轻量对话几乎不会失败，真正决定 30/50 积分的是网页通道会话——而巡检日志只写「✓ 每日活跃对话成功」，网页通道失败时面板上完全看不出来，只能等第二天发现积分没涨。现在巡检与手动打卡的日志都直接带上结果（`网页通道 completed：N 段输出，N ms` / `网页通道失败：<原因>`），与国内签到那条日志的写法一致。

- **一键配置只在网关本机可用，服务端形态整个不加载；顺带修掉第 5 个 Tab 挤坏移动端顶栏**（issue #246，感谢 [@houfukude](https://github.com/houfukude)）：这个功能写的是「网关进程所在机器」的客户端配置，只有浏览器和网关同一台机器时成立——服务端 / Docker / OpenWrt 部署下看板是远程打开的，改了也到不了用户自己的电脑。
  - `wb_agents` 改成**懒加载**：路由命中且判定通过才 import，用不到它的部署零常驻、零探测、零备份；
  - 新增可用性判定：请求来源**只认回环**（`127.0.0.1` / `::1` / `::ffff:127.0.0.1`），容器（`/.dockerenv`、`/proc/1/cgroup` 里的 docker/containerd/kubepods）与 OpenWrt（`/etc/openwrt_release`、`os-release` 的 `ID=openwrt`）标记另算一道、压过来源判定；不通过时 `GET /agents` 回 `enabled:false`，`/agents/apply`、`/agents/restore` 直接 403；
  - 看板启动时用新增的廉价接口 `GET /agents/available` 判定（不 import、不探测），判定为不可用就把入口整个撤掉，`?tab=agents` 的书签退回网关页；本机打开看板的行为一点没变；
  - **移动端顶栏**：≤640px 主 Tab 行改成横向滚动（与 `.page-nav` 在 ≤860px 的做法一致），按钮保持可读宽度、放得下时仍然平分整行——issue 里的实测溢出（414px +5px、360px +19px、320px +59px）全部归零，390px 五个 Tab 一行放得下；
  - 测试：`tests/_test_agents.py` +11 项、`tests/_test_panel_route_auth.py` +4 项（远程来源读 `enabled:false`、写 403、容器标记压过回环、能力探测两侧答案）、`tests/_test_agent_ui.js` +3 项（入口隐藏与书签回退）；`tests/_mobile_check.py` 的 `nav-equal-width` 不再写死 4 个 Tab，改钉「标签不截断 / 不顶出导航条 / 填满整行」三条几何性质。

- **剩余用量估算：把上游 24h 窗口的预算反推出来**（[PR #247](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/247)，感谢 [@aodianjun](https://github.com/aodianjun)）：上游按 24 小时窗口给「账号 × 模型」配额（用满即 429 / code 6004，带重置墙钟），预算数字却从不公开。新增 `GET /usage/remaining` 与数据看板「剩余用量估算」区块：**预算 = 撞线账号在窗口内用量的平均**（撞线那一刻该账号「自窗口起点以来的成功用量」就是一个样本；按区域聚合，先按账号平均、再跨账号平均），**已用从各账号自己的重置时刻起算**，重置时刻还在未来的组合（冷却中）剩余记 0 并显示恢复时间；没有撞线记录的组合按最近 24h 统计并标「按 24h 估算」——窗口起点必然落在最近 24h 内，所以那是剩余量的下界，偏保守。样本由 429 处理路径**实时**写进 `usage/limit-events.jsonl`：一次请求的重试可能接连撞好几个账号，而日志里只有最后那个账号留着带归属的 429 行，光靠日志会漏掉大部分撞线；历史日志里带账号的 429 行作补充，按（账号 + 模型 + 重置时刻）去重。新增 `tests/_test_remaining_usage.py`（15 项）与 `tests/_test_remaining_usage.js`（26 项）。

- **perf(usage): 剩余用量估算不再重复计算**（[PR #248](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/248)，感谢 [@aodianjun](https://github.com/aodianjun)）：每个「账号 × 模型」的用量缓冲从 deque-of-tuple 换成**时间戳 + 累计和**（窗口求和 = 两次二分 + 一次相减，O(log n)，约 16 字节/行），载荷挂 15 秒短 TTL（`WB_REMAINING_TTL`）并支持 `ETag` / `If-None-Match`：面板 5 秒一轮的轮询在 TTL 内复用上次算好的载荷（连日志尾部都不再扫），带条件请求时回 304。实测 2 万行缓冲：轮询一分钟 21.5ms → 2.9ms、缓冲内存 2.3MB → 321KB（116 → 16 字节/行）、热重建 1.8ms → 0.7ms；并修掉一处裁剪后累计和的重基错误（`tests/_test_remaining_usage.py` 的 600 行对拍用例抓到的，15 → 18 项）。

- **「国际版每日活跃打卡」的说明精简**：设置页那段解释删掉实现细节与「会消耗少量积分」的提示，只留「国际版账号每日打卡时，除桌面端身分的轻量对话外，再走一次网页通道的会话。默认开启。」；英文与正體中文词条同步。

- **数据看板新增「积分获取历史」**：原来只有扣减（花掉的积分），现在把上游发给每个账号的**积分包清单**（每日活跃奖励的 Bonus Pack、免费套餐、活动包…）摊平成一张表——发放时间 / 账号 / 区域 / 名称（悬停看发放原因）/ 积分 / 剩余 / 到期 / 状态，新的在前；状态按「过期 > 用完 > 在扣减 > 可用」归类（`in_usage` 的包标「生效中」，`no_expiry` 的显示「不过期」），并新增**来源**列：上游的发放原因原样显示（「Buddy 加油站签到」「成长计划奖励」「官方活动发放」），国际版 Bonus Pack 30/50 按官方规则推断为「每日活跃奖励」；网关自己的签到 / 每日活跃记录按时间关联到发放上（发放前 2 小时内最近一次尝试），悬停即可对上「本机动作：每日签到 10-10 00:07 成功」。摘要行给出笔数与合计并标**快照时刻**。新增 `GET /accounts/credits/grants`（管理面路由）：只读内存里的积分快照、**不发上游请求**，数字随「一键刷新积分」（签到 / 每日活跃打卡也会刷新它）更新。新增 `tests/_test_credit_grants.py`（36 项，含关联窗口与历史投影）与 `tests/_test_credit_grants.js`（23 项）。

- **顶部页签可收起（设置 → 顶部页签显示）**：看板上用不到的页签（任务与福利、运行日志…）以前只能一直挂在顶部。设置页新增「顶部页签显示」区块，取消勾选即把该页签从顶部收起来——页签按钮与页面容器**一起**收，不会留下「看不见入口但内容还在」的状态，勾回来即恢复。选项按顶部现有的页签**现场生成**（读 `.main-nav` 里的按钮，标签跟着界面语言走），上游以后增删页面这里自动跟上，不必再维护第二份页表；偏好存服务端（`accounts/settings.json` 的 `hidden_pages`），换浏览器、重启服务都记得，与账号区折叠、「(切换前)」行同一套通道，不用 localStorage。「设置」固定保留：它是这个开关自己的入口，收起来就再没有地方能把它放回来——服务端归一化时也把它丢掉，手改 `settings.json` 同样关不掉。被收起的页面 `switchMainTab()` 会拒绝切入，`?tab=` 书签与上次停留的页签都退回第一个可见页面，不会点亮一个带着 `hidden` 的空页面；保存失败会把界面回滚，不只改内存。新增 `tests/_test_page_visibility.py`（22 项）与 `tests/_test_page_visibility.js`（48 项），英文与正體中文词条同步补上。

## v1.6.19

看板细节打磨与发布自动化：工具栏文案与顺序重排、页面头部与区块标题样式统一、用量卡片字号配色对齐、隐藏冗余按钮；发布侧改由 tag 触发自动打包并开 Draft Release，另修好猫猫旅行的 Buddy 误判：

- **猫猫旅行不再误判「没有 Buddy」**（[PR #235](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/235)，感谢 [@ayeaaaa](https://github.com/ayeaaaa)）：`travel/status` 的 `buddy_id` 是**当前旅行实例**的 id，猫在家（idle）时恒为 0，之前拿它判断「有没有 Buddy」，于是所有 idle 账号（包括早就领过 Buddy 的）点旅行都会被挡下。现在以 `depart` 返回的 400 `no active buddy` 为准：先直接派，确实缺 Buddy 才完成并领取 `first_buddy`（其奖励就是 Buddy 实例）后重试一次，其余失败原样透出上游提示。

- **页面头部结构统一**：「网关与账号」页补上标题 + 一行说明（原来直接从卡片开始），并给「网关设置」补上同款说明行（原来只有标题）——五个页面现在都是「16px 深色标题 + 12px 灰字说明」。新标题不在 `<section>` 里，所以不会变成侧栏「本页导航」项；英文与正體中文词条同步补上。

- **顶部页签顺序调整**：改成「网关与账号 → 数据看板 → 智能体配置 → 设置 → 运行日志」，把运行日志放最后、设置放倒数第二；`MAIN_TABS` 与启动脚本里那份 `TABS` 同步按新顺序排列（两处注释本来就要求与导航顺序一致）。

- **「账号列表」折叠开关的焦点态改柔和**：原来聚焦时是贴着文字画的 2px 蓝色实线描边（用 Tab 键盘走到它时特别扎眼），现在换成淡蓝底 + 1.5px 细描边的胶囊高亮，圆角也跟面板统一成 8px；按钮 padding 与负 margin 成对，聚焦与否标题文字都不位移。

- **区块标题样式统一**：数据看板的「Token 时序」「积分扣减历史」两个**区块**标题原来写成了 16px 深色（那是页面标题那一档的样式），跟同页其他区块标题（13px、`--dim`、大写 + `.6px` 字距）不一致，现在拉回统一样式。页面标题（网关设置 / 智能体配置 / 网关运行日志 / Token 消耗与推理指标透视）仍保持 16px 深色这一档，两级层次不变。

- **网关页三张用量卡片对齐字号与配色**：`估算价格` 的数值原来写死 18px，比旁边两张卡的 22px 小一号，现在统一到 22px；中间的 `/` 分隔符与「网关调用量」的 `次` 单位统一成 13px、`--dim` 色、左右 4px；价格的红绿改用主题变量（`--bad` / `--accent2`）而不是写死色值，深色主题下跟着变，而且与「请求成功率」的绿是同一个绿。`fmtCostBoth` 的分隔符字号改为随数值字号缩放（22px→13px，数据看板那张 14px 的卡仍是 10px，观感不变）。

- **账号工具栏文案精简，账号行补「出站身份：」标签**：`+ 添加账号 (OAuth) → 添加`、`扫描桌面客户端账号 → 扫描桌面客户端`、`导入账号 → 导入`、`导出账号 → 导出`、`刷新积分 → 一键刷新积分`、`分配代理出口给未绑定账号 → 一键分配代理`（`一键刷新凭证` 不变）；账号行操作列里 WB / VSC / CLI 三个按钮前面加上「出站身份：」，不用再看表格下面那行图例才知道它们是什么。英文与正體中文词条同步补上。

- **账号工具栏按钮按用途重排**：添加账号 → 扫描桌面客户端账号 → 导入账号 → 导出账号 → 刷新积分 → 一键刷新凭证 → 每日签到 / 每日活跃打卡 → 分配代理出口给未绑定账号 → 全部启用 / 全部停用（原来打卡按钮夹在添加账号与扫描之间、导出在导入之前）；四组之间用一条浅色竖线（`.toolbar-sep`）分开，换行时分隔线跟着它后面那组走。

- **面板隐藏「网页通道打卡 (国际版)」按钮**：网页通道那一步已经由设置里的「国际版每日活跃打卡」自动带在「每日活跃打卡」里，单独那个按钮不再上屏（手动补跑走 `/accounts/daily-chat-web`）。按钮元素留在 DOM 里但始终 `display:none`，要放回面板时去掉它并恢复 `updateUI()` 里的按视图显隐即可。

- **tag 触发的发布打包与 Draft Release（issue #28）**：`v*` tag 现在由 `.github/workflows/release.yml` 一条链路走完——校验 tag / `wb_proxy.py` / `wrt` 包 Makefile 三处版本一致，拿到**完整测试矩阵**（Ubuntu 3.9 + Ubuntu 3.12 + Windows 3.12，腿名从 `tests.yml` 读出来逐条核对）后，构建便携 ZIP、OpenWrt `.ipk` 与 `.apk`，按最终资产生成 `SHA256SUMS`，最后创建或更新 **Draft Release**。工作流永不 publish，最后一步还会断言它仍是 draft。便携 ZIP 是**绿色包**：在 `windows-latest` 上按「既有线上包那份运行时的文件集合」裁剪钉死并校验 sha256 的上游 CPython 3.12 运行时（527 个文件），打进 `wb-proxy/python/`，然后用包内解释器**真的启动一次网关并探通 `/health`**，所以资产不是被换了名字的源码包。包内 `release-manifest.json` 记录版本、`root`、`python/` 运行时子树与受保护目录 `accounts/`、`usage/`，作为自更新（#29）的消费契约；`release/portable.txt` 的显式清单同时补上了 `pricing/pricing.json` 这类运行时数据（`wb_pricing._candidate_file()` 优先读它）。OpenWrt 配方来自 #190 引用的 `aodianjun/workbuddy2api-hub/wrt/`，审计后并入：保留 `.ipk`/`.apk` 两个打包脚本、包 Makefile、init.d、uci 配置与面板缓存预热器；去掉 fork 专属的 GitHub 自更新器（`workbuddy2api-update` 及其 cron、`auto_update` 选项——OpenWrt 升级走包管理器）、fork 的工作流激活脚本与上游同步工作流，以及钉死上游 commit 的 `PIN_SHA`/`PIN_VER`（配方进了上游仓库后"从 GitHub 拉另一个 commit 的上游源码"没有意义，版本改为取自当前检出）。`-ci` 演练 tag 走完全相同的打包与 draft 流程，只是额外标成 prerelease。**只有 `v*` tag 推送能写 release**：`workflow_dispatch` 是 packaging-only，写入点单独放在一个 `if:` 为「事件是 push 且 ref 是 `refs/tags/v*`」的 job 里，手动运行的任何输入组合都够不到它（因此手动运行也不再有 `dry_run` 开关）。新增 `tests/_test_release_assets.py`（60 项）：清单覆盖每个 `wb_*.py` 与 `pricing/pricing.json`、清单路径都存在且不含受保护目录、运行时裁剪规则与启动契约（缺 `python.exe`／混进 `Lib/multiprocessing`／残留 `.pdb` 都必须被拒）、ZIP 结构（`wb-proxy/` + `python/` + 标记文件）与"不是源码包"、重建逐字节相同、`SHA256SUMS` 覆盖每个资产且随字节变化、正文重写幂等且保留维护者写在标记之上的说明、矩阵腿名确实来自 `tests.yml`（删一条腿就会少一条要求）、工作流必须 `--draft`、便携资产必须在 Windows 上打包并启动、任何写 release 的路径都必须拿到完整矩阵，以及**「只有 tag 推送能到写入点」这条不变量本身**。后者配了一张命令形态表：`gh release create/edit/upload/delete`、显式 `-X/--method POST|PATCH|PUT|DELETE`、以及**靠 `-f`/`-F`/`--field`/`--raw-field`/`--input` 触发隐式 POST 的 `gh api`** 都算写，显式 `-X GET`/`--method GET` 与 `echo` 出来的命令不算；扫描前先归一化 `\` 续行，并按 `&&`/`||`/`;` 切分，所以被拆开的命令也跑不掉。变异测试常驻：把写入点搬到 dispatch 路径、塞进别的 job、改成单行 `run:`、用隐式 POST 的 `gh api` 建 release、给 `workflow_dispatch` 加一个可能授权写入的输入、或抹掉全部写入点，检查器都必须判红。

## v1.6.18

看板体验与性能版本：侧栏区块导航回归、账号区可折叠、用量聚合全面提速，并新增智能体配置页、账号活跃历史与更新检查：

- **侧栏区块导航回归**：每个主页面左侧恢复「本页导航」——导航项按页面内的 section 现场生成、滚动自动高亮、点击把锚点写进地址栏；新增「回到顶部」与可收起（收起状态存 localStorage，窄屏下自动变横向胶囊行），#169 把首行撑高的布局缺陷一并修掉（页面内容包一层 `.page-nav-body`，区块间距回到 20px）；
- **账号区可折叠**（[PR #201](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/201)、[PR #226](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/226)，感谢 [@teddyli18000](https://github.com/teddyli18000)，issue #176）：「账号列表」标题旁的小三角把整块账号区收起，选择存在服务端（`accounts_collapsed`），换浏览器、重启服务都记得，账号轮询照常跑；
- **数据看板 KPI 拆分与双币金额**：六张卡拆成八张——「平均首字延迟」从生成速度卡里独立出来（耗时留在速度卡），「请求数 & 成功率」拆成两张（成功率按有无失败染绿/琥珀）；「API 等价花费」不再挂货币切换按钮，改成与网关页一致的 `￥0.197 / $0.028`（人民币红、美元绿）；去掉「当前为『全部历史』视图，两张卡片口径相同」那句提示；
- **页签与区块改名**：页签「网关与运维 → 网关与账号」「数据指标看板 → 数据看板」，标题「WorkBuddy 网关看板 → WorkBuddy 网关」；设置页区块「API Key 与出口绑定 → API Key」「代理槽 → 账号代理槽」「限额 → 账号限额」「OpenRouter 价估算 → 模型价格估算」「429 自动切换出站身分 → 自动切换出站身份」「本地网络工具 (web_search / web_fetch) → 本地网络工具」；网关页「账号 → 账号列表」「当前禁用账号与模型 → 当前禁用」「当前版本模型库与能力清单 → 网关模型清单」。导航项由区块 h2 现场生成，侧栏与页内小标题两处同步；
- **网关页用量卡片标出区域**：`网关调用量` / `估算价格` / `请求成功率` 的数字本来就按当前视图区域取（`/usage` 等接口带 `realm`），标题补上「（国际版）/（国内版）」跟着区域卡片切换；
- **正體中文（台灣）介面**（[PR #204](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/204)，感谢 [@Cekxri](https://github.com/Cekxri)）：看板語言從「简体中文 ⇄ English」擴充為三態循環「简 → 繁 → English」。正體中文以 OpenCC 台灣用語轉換（軟體、網路、記憶體、預設、登入、帳號…），切回簡中時還原原文。語言偏好採三層優先序：URL `?lang=` > 瀏覽器 `localStorage` 覆蓋 > 實例預設值；网关设置里可保存實例預設語言，右上角按钮只覆蓋当前浏览器。新增 `tests/_test_i18n_traditional.js`（39 项）与 `tests/_test_ui_language.py`（13 项）；
- **智能体配置页**（[PR #219](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/219)，感谢 [@2xz7vmvpv4-art](https://github.com/2xz7vmvpv4-art)）：探测本机已装的 Claude Code / Codex CLI / OpenCode / DSH / Crush，一键把它们的配置指向本网关（改前自动备份、可字节级还原、外部改动可感知），只碰本机配置文件，不参与请求路径；
- **账号签到与每日活跃历史**（[PR #206](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/206)，感谢 [@teddyli18000](https://github.com/teddyli18000)）：签到与活跃打卡结果落库，新增 `GET /activity/history` 读接口；
- **新版本检查**（[PR #207](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/207)，感谢 [@teddyli18000](https://github.com/teddyli18000)）：面板可发现上游新版本，默认关闭，开启后每天最多查一次 GitHub；
- **OAuth 授权链接一键复制**（[PR #222](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/222)，感谢 [@AstrosQ](https://github.com/AstrosQ)）：添加账号弹窗里的官方授权链接可直接复制；
- **用量聚合全面提速**（[PR #223](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/223)、[PR #189](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/189)，感谢 [@aodianjun](https://github.com/aodianjun)）：折叠循环重构（一行只读一次字段、不再每行造默认字典）、聚合扫描不再构造悬停明细、总开关整扫只问一次；整表重建改为增量——只折日志尾部新增的行，并把聚合状态连同位置写进数据目录，重启后从 checkpoint 续读，不再冷扫全量日志；
- **面板静态页 ETag + 304**（[PR #224](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/224)，感谢 [@aodianjun](https://github.com/aodianjun)）：首页从 `no-store` 改成 `no-cache` + `ETag`（文件 mtime/size 与注入语言一起算进校验符），命中条件请求回 304，不再每次重下 400KB；
- **修复 cn 区 429 中文重置时间被当成账号级冷却**（[PR #221](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/221)，感谢 [@LeoK77S](https://github.com/LeoK77S)）：`reset at …` 之外补上「将在 … 重置」文案，解析不出 reset 时间的 429 不再退化成整账号 2 小时冷却；
- **账号被熔断 / 降权时，看板与报错都会说清楚是谁、卡在哪一条**（[PR #228](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/228)，感谢 [@shuishuipingan](https://github.com/shuishuipingan)）：账号卡片新增「熔断 Xm」「降权 Xm」徽章（熔断优先于冷却展示），「当前禁用总览」把熔断与降权各列一行；新增 `Account.unavailable_reason()`，把「哪个账号、卡在哪一条」拼到那条 503 后面；上游连接抖动的日志行补上账号 UID。新增 `tests/_test_pool_diagnostics.py`（13 项）与 `tests/_test_account_penalty_badges.js`（18 项）；
- **修复国内版成长任务全部接取失败与猫猫旅行 400**（[PR #217](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/217)，感谢 [@ayeaaaa](https://github.com/ayeaaaa)）：满足 `first_buddy` 前置链；
- **修复 web 工具可被重定向到私网**（[PR #212](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/212)，感谢 [@AstrosQ](https://github.com/AstrosQ)）：跳转目标同样过 URL 白名单，环回 / 私网 / 链路本地地址直接拒绝；
- **修复 `/pricing` 未要求面板会话**（[PR #210](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/210)，感谢 [@teddyli18000](https://github.com/teddyli18000)）：管理面接口的鉴权边界与其它管理路由对齐；
- **修复「当前禁用」汇总跨区域串台**（[PR #215](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/215)，感谢 [@teddyli18000](https://github.com/teddyli18000)）；
- **修复输出上限探针默认端口**（[PR #205](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/205)，感谢 [@Cekxri](https://github.com/Cekxri)）：改回 8788；
- **测试基建与测试质量**（[PR #195](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/195)、[#196](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/196)、[#197](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/197)、[#198](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/198)、[#200](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/200)、[#202](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/202)、[#208](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/208)、[#209](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/209)、[#213](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/213)、[#214](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/214)、[#216](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/216)、[#218](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/218)、[#220](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/220)、[#225](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/225)、[#227](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/227)，感谢 [@teddyli18000](https://github.com/teddyli18000)）：套件弄脏检出时 CI 直接失败、隔离数据目录与进程生命周期统一抽取、页面源码走同一个 helper、把脆弱的文案与列数断言换成行为断言；
- **文档**：README 的版本历史拆到本文件，README 只留 Unreleased 段与入口（issue #193）。

## v1.6.17

重磅生态兼容与架构演进版本：正式支持 Claude Code、修复 API Key 误覆盖、引入临期积分优先分派机制，并实现测试基础设施多进程并行加速：

- **全面兼容 Claude Code 接入**（[PR #180](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/180)，感谢 [@LeoK77S](https://github.com/LeoK77S)，issue #171）：自动将 `messages` 内部的 `system` 角色提取并与顶层 system 合并，彻底解决 Claude Code 调用 `/v1/messages` 报 400 失败的问题；
- **修复 API Key 连续添加时误覆盖老 Key**（[PR #178](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/178)，感谢 [@LeoK77S](https://github.com/LeoK77S)，issue #175）：前端与服务端采用 upsert 语义同步，彻底杜绝连续添加 Key 导致老 Key 与出口绑定被意外软删除的问题；
- **智能调度：平滑加权优先分派临期积分账号**（[PR #174](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/174)，感谢 [@LeoK77S](https://github.com/LeoK77S)）：自动识别 7 天内即将过期的账号积分包，采用平滑加权轮询优先消耗快过期的账号额度，杜绝积分浪费；
- **大幅提升用量统计与时序端点性能**（[PR #185](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/185)，感谢 [@aodianjun](https://github.com/aodianjun)）：内存缓存消除重复 stat，解决万级日志时面板与时序图加载慢的痛点；
- **修复上游 live 目录只声明默认思考时丢掉可选档位**（[PR #177](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/177)，感谢 [@LeoK77S](https://github.com/LeoK77S)，issue #170）；
- **账号工具栏新增「一键刷新全部凭证」**（[PR #179](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/179)，感谢 [@LeoK77S](https://github.com/LeoK77S)，issue #167）；
- **数据指标看板「积分扣减历史」表头吸顶与账号昵称显示**（[PR #186](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/186)，感谢 [@LeoK77S](https://github.com/LeoK77S)）；
- **测试基础设施全面升级**（[PR #181](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/181) ~ [PR #184](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/184)，感谢 [@teddyli18000](https://github.com/teddyli18000)，issue #151）：统一抽取严格标准的 `tests/_dom_stub.js`，支持 `--jobs 4` 多进程安全并发跑测试，测试耗时从 2 分钟缩短至 27 秒；
- **看板 UI 全面优化**：彻底清理侧栏网格空隙恢复原生全宽布局，顶部卡片精简并突出账号可用对比。


## v1.6.16

重大稳定性与观测治理版本：涵盖账号熔断降权、工具调用防拆分修复、时序图表、全页面导航及多项深度优化：

- **上游工具调用配对修复**（[PR #153](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/153)，感谢 [@Cekxri](https://github.com/Cekxri)）：自动合并被部分客户端拆散的连续 assistant tool_calls 批次并丢弃截断参数，根治 DeepSeek 报 400 失败；
- **账号级软限流指数退避、熔断与降权治理**（[PR #163](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/163)，感谢 [@Cekxri](https://github.com/Cekxri)）：账号连续失败时自动指数退避，连续硬错误熔断，有效保护账号不被频繁失败打挂；
- **402 余额不足账号精准冷却至次日 04:00**（[PR #157](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/157)，感谢 [@Cekxri](https://github.com/Cekxri)）：余额用尽账号避免频繁重试，支持余额恢复后实时提前解冻；
- **面板新增 Token 时序图与积分扣减历史**（[PR #161](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/161)，感谢 [@Cekxri](https://github.com/Cekxri)）：新增 `/usage/timeseries` 时序聚合接口与看板可视化走势图；
- **侧栏区块导航推广至所有主页面**（[PR #169](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/169)，感谢 [@LeoK77S](https://github.com/LeoK77S)）：通用化侧边栏组件，网关运维与数据指标页均支持左侧吸顶导航与滚动高亮；
- **系统提示词模式注入与 403 重试**（[PR #160](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/160)，感谢 [@Cekxri](https://github.com/Cekxri)）：支持 passthrough/custom/append 三种系统提示词模式；
- **缓存命中别名归一化**（[PR #164](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/164)，感谢 [@Cekxri](https://github.com/Cekxri)）：消除别名遮蔽，客户端始终读取统一的真实缓存命中数；
- **错误信封新增 gateway_hint 归因解释**（[PR #154](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/154)，感谢 [@Cekxri](https://github.com/Cekxri)）；
- **面板一键同步账号昵称**（[PR #158](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/158)，感谢 [@Cekxri](https://github.com/Cekxri)）；
- **会话亲和历史长度上限与号池弹性并发**（[PR #152](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/152)，感谢 [@ddddd-ren](https://github.com/ddddd-ren)）；
- **四级模型上下文/输出查找链与输出上限探针**（[PR #165](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/165)、[PR #166](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/166)，感谢 [@Cekxri](https://github.com/Cekxri)）；
- **看板 UI 深度打磨**：最近请求表格表头与数据全列居中对齐、模型与推理强度拆分为独立列、KPI 卡片双币种优雅对齐。


## 未发布

- **智能体一键配置 (Agent Config)**：参照 EasyCLIProxyAPI 的 agents 机制，支持对本机 Claude Code、Codex CLI、OpenCode、DSH、Crush 客户端的一键检测、配置写入与安全备份还原；内置纯标准库文本级 YAML/TOML/JSON 编辑器与两阶段事务回滚保护。
- 新增 `tests/_test_agents.py`（26 项 / 97 断言）：覆盖 YAML/TOML/dotenv/JSON 编辑器、客户端注册表、两阶段原子回滚、备份还原与网关 handlers 端到端测试。
- 新增 `tests/_test_agent_ui.js`（18 项断言）：把看板脚本载入 DOM 桩后直接调用真实的 `applyAgent()` / `restoreAgent()` / `loadAgents()`，断言实际发出的请求体与渲染结果，钉住请求字段名漂移与未声明标识符这两类只在浏览器里暴露的缺陷。

两项与「大请求 + 号池规模」相关的可调限制，默认行为不变：

- **会话亲和加长度上限（`WB_AFFINITY_MAX_MSGS`，默认 400）**：前缀亲和把整段对话钉在同一个账号上以吃满上游的账号级 prompt cache，但对话上下文是单调增长的，于是那个账号要反复接收越来越大的请求体。实测某生产实例（wk4，333 个请求）请求体与上游断连率的关系：

  | 消息条数 | 请求数 | 断连率 |
  |---|---|---|
  | < 150 | 82 | 0.0% |
  | 150–300 | 59 | 3.4% |
  | 300–400 | 64 | 7.8% |
  | 400–500 | 50 | 10.0% |

  断连（`TimeoutError` / `RemoteDisconnected`）触发重试，把 8.8s 的请求拖到 11.6s，首字延迟随之翻倍。超过上限的对话不再钉在同一个账号上：默认改在一个**页**里轮转（下一条），设 `WB_AFFINITY_PAGE_SIZE=0` 则恢复 1.6.x 的整池轮询。阈值不宜调低——同一实例 94% 的请求靠亲和拿到 98.5% 的缓存命中率。

- **聊天并发上限可按号池规模自动取值（`WB_MAX_CONCURRENT_CHAT=auto`）**：原先是固定 32，与号池里有几个账号无关，5 个账号和 200 个账号的部署共用同一个值。设为 `auto` 后取「就绪账号数」，且不低于 32。仅扩容、不缩容：已在飞行的请求持有旧信号量的许可，缩容会让归还次数超过上限并触发 `BoundedSemaphore` 的 `ValueError`。默认仍是固定值 32，行为不变。

- 新增 `tests/_test_affinity_length_cap.py`（13 项）：钉住阈值边界（`msgs == cap` 仍绑定、`cap + 1` 释放）、`0` 关闭上限、长对话的键稳定性、不同对话不碰撞、`None` / 空列表安全，以及并发上限的按池取值、下限回落、只增不减、固定值下为空操作、脏输入忽略与扩容后的许可计数。

- **超限对话改成页内轮转，换号优先回「暖号」（`WB_AFFINITY_PAGE_SIZE`，默认 3）**：长度上限把长对话放回整池轮询，但整池轮转下每个账号要等「池子大小」轮才再见到这段对话，间隔一旦超过上游缓存的存活期就整段漏。线上实测（2026-10-10，37,706 条请求、12 个账号）：超限对话占 59% 的 prompt token，却贡献了 **64% 的漏掉量**，签名是只命中约 24k 的系统提示（那个账号见过同一客户端的系统提示、没见过这段历史）——典型的「落在没预热过的账号上」。现在超限对话不再撒到整个池子，而是在一个**页**（uid 排序后按页大小取模的一小组账号）里轮转：每个账号「再见到」这段对话的周期从池子大小轮缩到页大小轮，缓存存活期内必定留着一份；超大请求体仍摊在页里那几个账号上，不是压回一个（那正是长度上限要解决的断连问题）。取值 **≥2 为页大小、1 为照旧钉住（等于取消上限）、0 为 1.6.x 的整池轮转**；页比池子大时页即池子（否则取模会把对话钉成一个号）。页的划分按**本区域**账号的 uid 排序取模（不是全池——混合区域部署下全池排名会让某个区域的页只剩一两个号，等于又钉死），重启与重载不改变成员，增删账号每页最多挪一个槽位；页内轮转有自己的游标，不挪动整池轮询的位置。

- **换号优先回「暖号」，失败只降级不遗忘**：绑定的账号不可用时（冷却、停牌、停用），不再把整段对话的绑定擦掉，而是按**最近使用顺序**试这段对话的其他账号——它们可能还留着这段前缀，冷启动一次大 prompt 的代价远高于重选一次账号；请求失败（429 / 402 / 401 / 403 / 5xx / 断连）时把出错的账号**降级**（`demote`）到队尾而不是忘掉整段对话，出错账号的冷却结束后仍可能作为后续的暖号回退。绑定本身在账号恢复后照旧优先（服务过最近一轮的账号前缀最新鲜）。

- 新增 `tests/_test_affinity_pages.py`（23 项）：钉住暖号回退顺序、历史去重与上限、降级语义、页的成员与稳定性（与加载顺序无关）、页内轮转覆盖整页且不挪动整池游标、页全不可用时回落整池、页对话不绑定、页大于池子即整池、脏页安全。`tests/_test_affinity_length_cap.py` 增至 22 项：补页键与页稳定性、页大小 0 / 1 的边界、`affinity_route` 与 `derive_affinity_key` 一致。

- **修复 `tests/run_all.py` 在非 UTF-8 控制台下崩溃**（Windows CI 长期红灯的根因）：各套件本身以 `PYTHONIOENCODING=utf-8` 运行、输出也按 utf-8 从日志读回，但 `run_all.py` **自己**再打印这行摘要时用的是控制台编码。Windows runner 的 stdout 是 cp1252，于是第一条含中文的摘要就抛 `UnicodeEncodeError` —— 而这时所有套件其实**已经全部通过**，是汇总环节把整轮判成了失败。main 上连续多个版本（含 v1.6.15 自身）的 Windows job 都是这么挂的。现在启动时把本进程的 stdout/stderr 重设为 utf-8，并以 `errors="replace"` 兜底（生僻码位退化成 `?` 而不是终止整轮）。新增 `tests/_test_run_all_encoding.py`（3 项）：分别在 cp1252 与 utf-8 下跑一个含中文摘要的套件，断言退出码为 0、输出里没有 `UnicodeEncodeError`，并确认摘要确实来自被选中的那个套件。

- **一键刷新凭证**：账号工具栏新增批量按钮，等价于对池中每个账号点一次「刷新凭证」——后端 `/accounts/refresh` 不带 `uid` 时本就刷新整池，但看板上一直没有入口，`refreshAccounts()` 是没人调用的死代码（issue #167）。按钮在飞行期间禁用并显示「刷新中...」，结束后按成功数回报（全部成功为绿色，有失败则降级为黄色，并附上首个错误与失败条数），随后重画账号卡片让新凭证立刻可见。新增 `tests/_test_refresh_all_credentials.js`（10 项）：只发一次不带 `uid` 的请求、成功 / 部分失败 / 网络异常三档回报与配色、缺失 `error` 时兜底、刷新后必重载列表、按钮禁用与复位、按钮与英文词条确实在页面上。

## v1.6.15

里程碑版本：正式支持原生 Anthropic Messages 协议、完善企业版积分查询，以及多项重要修复与移动端体验优化：

- **原生 Anthropic Messages 协议支持 (`/v1/messages`)**（[PR #147](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/147)，感谢 [@Cekxri](https://github.com/Cekxri)）：原生提供 `POST /v1/messages`（流式 / 非流式）与 `POST /v1/messages/count_tokens`，鉴权支持 `x-api-key` 与 `Authorization: Bearer`，支持完整的 Anthropic 原生 SSE 事件序列与双向工具调用映射，现可无缝接入 Claude Code、Cursor 等全套 Anthropic 客户端生态；
- **修复设置页加载异常**（[PR #141](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/141)，issue #138）：修复 `loadSettings()` 遗漏 `pricingOn` 变量引发 ReferenceError 导致 API Key 列表与底部设置项无法加载的问题；
- **企业版账号积分显示与护栏修复**（[PR #149](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/149)，感谢 [@johnsken-jerry](https://github.com/johnsken-jerry)）：适配企业空间计费接口，彻底解决企业账号积分查回为 0/0 以及误触保留积分拦截的问题；
- **上游 SSE 超时保护与流式容错**（[PR #148](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/148)，感谢 [@Cekxri](https://github.com/Cekxri)）：增加上游 header/idle 读超时配置，防止流式挂起与死锁；
- **移动端中英文切换优化**（issue #150）：中英双胶囊按钮重构为紧凑单按钮一键切换（中 ⇄ EN），与主题图标按钮完全对齐，彻底解决小屏下顶部 UI 挤压变形；
- **修复多模态孤立函数输出报错**（[PR #145](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/145)，感谢 [@yorushikasama](https://github.com/yorushikasama)）：修复 Codex 等客户端在 `/v1/responses` 传递带图片的结构化输出时的 AttributeError 崩溃；
- **防止陈旧 429 报错跨重启复活**（[PR #144](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/144)，感谢 [@LeoK77S](https://github.com/LeoK77S)）：重启时自动清理过期的 `cooldownUntil`，消除历史冷却误报；
- **模型用量占比基准校准**（[PR #140](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/140)，感谢 [@LeoK77S](https://github.com/LeoK77S)）：表格百分比分母改为全体模型总 Token，避免第一名失真显示 100%；
- **价估算胶囊开关体验优化**（[PR #142](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/142)，感谢 [@LeoK77S](https://github.com/LeoK77S)）：开关改为即点即存交互并补齐英文翻译词条；
- **测试环境 DOM 桩补全**（[PR #139](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/139)）。


## v1.6.14

重磅功能与体验升级版本，涵盖限额护栏统一矩阵、看板双语切换、设置项侧栏直达、估算开关以及多处界面优化：

- **限额护栏统一配置表与国际/国内版独立阈值**（[PR #130](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/130)，感谢 [@LeoK77S](https://github.com/LeoK77S)）：将「保留积分」、「每日 Token 限额」、「每日积分限额」、「按模型每日 Token 限额」合并为统一的矩阵配置表；支持针对国际版与国内版分别指定不同的限额阈值（留空自动继承全局，升级自动兼容无损迁移旧配置）；
- **看板新增 CN/EN 中英双语切换与完整翻译**（[PR #134](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/134)，感谢 [@many1337](https://github.com/many1337)）：看板右上角支持一键切换简体中文与 English，内置全量前端英文化翻译字典并持久化记忆；
- **设置页动态左侧锚点导航栏**（[PR #129](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/129)，感谢 [@LeoK77S](https://github.com/LeoK77S)）：依据页面区块动态渲染左侧吸顶/浮动侧边栏，支持滚动高亮与点击直达，大幅改善多设置项下的查找体验；
- **账号错误悬停查看完整响应与当前禁用总览**（[PR #132](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/132)，感谢 [@LeoK77S](https://github.com/LeoK77S)）：账号卡片支持悬停查看上游完整报错（429 恢复时刻一眼可见）；新增「当前禁用账号与模型」总览表，集中感知限流与停用状态；
- **增加 OpenRouter 价估算总开关**（[PR #133](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/133)，感谢 [@LeoK77S](https://github.com/LeoK77S)）：为估价模块补齐总开关（默认开启），关闭后彻底停用后台抓取与逐行折算开销，提升大日志量下的处理性能；
- **清理设置页合并冲突残留标记**（[PR #131](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/131)，issue #137）。
- **原生 Anthropic Messages 协议（2026-10-07）**：`/v1/messages` 与 `/v1/messages/count_tokens` 全原生实现（流式事件序列、`x-api-key` 鉴权、Anthropic 错误信封、内容块与工具双向映射），Claude Code / Anthropic SDK 可直连；服务端工具、thinking 回放与 `top_k` / `cache_control` 的取舍见「四、客户端配置与接入」。


## v1.6.13

- **修复看板右上角颜色主题菜单按钮无法打开**（[PR #127](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/127)，感谢 [@LeoK77S](https://github.com/LeoK77S)）：修正 IIFE 作用域中 `toggleThemeMenu` 与 `selectTheme` 的全局导出时机，修复点击按钮报 `ReferenceError` 导致下拉菜单无法弹出的问题，现可正常手动选择「浅色 / 深色 / 跟随系统」；新增 `tests/_test_dashboard_theme.js` DOM 级可达性与交互回归测试；
- **Docker 一键安装脚本补充国内网络加速与权限说明**（issue #126）：README 补充国内网络环境下通过 GitHub 代理加速拉取命令与 NAS 非 root 账户下的 `sudo bash` 说明。

## v1.6.12

- **Docker 部署与更新生态全面升级**（PR #125）：
  - **一键部署与更新脚本 (`quick-deploy.sh`)**：终端仅需执行一行命令 `curl -fsSL https://raw.githubusercontent.com/ardeyouxipianyi/workbuddy2api-hub/main/quick-deploy.sh | bash`。首次运行自动检测环境并拉取启动；后续再次执行同一命令即可完成自动平滑升级，账号配置与历史用量绝不丢失；
  - **NAS / 面板单文件 Compose 模板（免源码克隆）**：官方 `docker-compose.yml` 剔除 `build: .` 依赖，飞牛 fnOS、群晖、1Panel 等用户无需 `git clone`，直接复制粘贴 YAML 即可建站并支持面板一键更新；开发者本地构建单独拆分为 `docker-compose.build.yml`；
  - **支持双镜像仓库推送（GHCR + Docker Hub）**：工作流新增对 Docker Hub（`ardeyouxipianyi/workbuddy2api-hub`）的自动同步推送，消除前缀缺省报错困扰，兼顾国内 Docker 镜像加速器拉取；
  - **Watchtower 全自动静默更新**：提供开箱即用的 Watchtower 配置与命令，支持后台无感自动升级。


## v1.6.11

- **修复 Codex Responses 缺省 max_output_tokens 导致 32k 截断**（issue #121）：当客户端未显式传入输出上限时，网关自动根据模型目录中宣告的 `maxOutputTokens`（如 `deepseek-v4.1-flash` 为 128,000）进行兜底补全，避免长推理因触发上游默认 32k 限额而中断无正文。
- **修复面板新增 API Key 时已有 Key 模型限制被清空**（issue #92）：补齐 `/settings` 接口返回的 API Key 列表中的 `models` 字段，防止前端重新打包保存时将未带限制的数组传回导致误清空。

- **新增「每日积分限额」：账号当日积分花超后只服务免费模型**（默认 `0` 关闭，面板可设阈值）：账号当日消费的积分（按上游 `credit` 累计）达到阈值后，需要花费积分的模型自动切到其他账号，目录中标记为 `x0.00` 的免费模型照常服务——例如 `gpt-6-astra` 花满 50 积分后，免费期的 `deepseek-v4.1-flash` 不受影响。免费/付费判定以**各出口自己的模型目录**为准（同一 id 在不同出口可以一个免费一个收费），未知模型按付费处理（保守）。本地时间 0 点自动解封；单账号独立计数。账号行显示「积分限额」徽章，池子卡片显示「N 个达积分限额」，全部账号达额时返回 `429`（文案说明免费模型仍可用）。
- **新增「按模型每日 Token 限额」**（默认 `0` 不限）：账号在单个模型上当日消耗的 token 达到阈值后，只把**该模型**切到其他账号，同一账号的其他模型不受影响——例如 `hy4-preview` 用满 2 亿后该模型被跳过，`deepseek-v4.1-flash` 仍可用。账号行显示 `模型 · N tok 达限` 标记；本地时间 0 点自动解封；单账号独立计数。
- 两个限额与现有「每日 Token 限额」共用同一份增量扫描（`daily_usage_stats`），请求热路径开销不变；新增 `tests/_test_daily_credit_limit.py`（10 项），整套增至 30 个套件（25 个 Python + 5 个 JS）全绿。
- **新增「OpenRouter 价估算（等价 token 花费）」**：把请求 token 按 OpenRouter 公布的模型价折算成等价金额，与账号实际扣除的积分并列展示。定价快照内嵌在 `wb_pricing.py`（单一来源：OpenRouter 目录，美元，按快照汇率折算；大多数模型一个价，按条件定价的模型（输入长度阈值或 UTC 时段）按每条请求取档），`_fetch_pricing.py` 可随时刷新（支持 `--embed` 回写内嵌、`--dry-run` 只打印，`--extra-ids-file` 额外覆盖一批名字），并按「面板手填覆盖 → 人工覆盖表 → 名字归一化全等 → 变体后缀继承」依次为模型取价。计价含缓存命中/未命中分档。看板显示位：数据指标看板 KPI 卡片、账号透视表最后一列、模型用量表最后一列（含合计）、网关页卡片、最近请求最后一列；**人民币/美元一键切换**，无定价模型显示 `—` 并计入「未覆盖」。
- **定价按策略留档、定时自动刷新**：网关每隔一段时间（面板「设置 → 定价刷新」，默认 **5 分钟**，单位即分钟，填 0 关闭）去 OpenRouter 取一次价，按**内容**存成一条条独立的定价策略（同一模型同一份价格只存一条，A → B → A 只占两条），并用一条时间轴声明每个模型当前生效的是哪一条。`usage.jsonl` 每条请求记下它**引用了哪条策略**，计价按引用查——之后上游调价不会改写昨天已经算出的数字；当时还没有价的模型用之后第一次取到的价补算并标 `*`。没有任何请求引用、又已经不作数的策略会在抓取后清掉（每个模型至少留一条，刚取到的那批不动）。所有汇总都是把逐条估算相加，不拿汇总 token 乘单价重算。取价逻辑一并从 `_fetch_pricing.py` 移进 `wb_pricing.py`（Docker 镜像只 COPY `wb_*.py`，运行时取不到那个脚本），后者只负责生成出厂快照。
- **取价间隔改为分钟计（默认 5 分钟），旧的小时配置自动折算**：此前该设置按小时计、存在 `pricing_refresh_hours`；现在统一为分钟并存 `pricing_refresh_minutes`，`settings.json` 里遗留的小时值在升级后第一次读取时按 ×60 折算写回新键、旧键删除（只迁移一次），所以「6 小时」不会变成「6 分钟」。0 仍表示关闭自动刷新，「立即取价」不受影响。
- **上游新增模型无需改代码即可自动取价**：取价的候选清单从「只读 `wb_catalog.py`」改为 **静态目录 ∪ 网关实时目录里新增的模型**（intl 与 cn 两个区域各自的实时目录都会并进来，走 `/v1/models` 同一套过滤；某个区域取不到时该区域回落静态目录，一次抓取失败不会让覆盖变少）。两次取价之间新模型就被调用时，第一笔请求也会带价：`wb_pricing.ensure_policy()` 用最近一次抓取留在内存的目录按需登记策略，**请求路径零网络 I/O**，登记在 `RLock` 内完成、并发重复调用也只写一条，新登记会打一条 `[定价]` 日志。匹配链新增**变体后缀继承**（`VARIANT_SUFFIXES`：剥掉 `-lkeap` / `-taiji` / `-volc` / `-sg` 等渠道后缀，拿基名重走解析链，仍要求唯一命中），继承来的策略记 `via=variant` 与 `inherited_from` 供面板审计；设置页新增「变体后缀继承」开关（默认开，关掉即恢复只有覆盖表与同名匹配才定价）。`-f` / `-dev` / `-x` 不剥——它们是 hub 自己的档位，单价可能不同。匹配始终「宁可漏也不错」，多轮未命中的不写价。
- **未定价模型可见化，并可在面板手填 OpenRouter id 收口**：`GET /pricing` 新增未定价清单，每条带分类（`alias` 虚拟别名，不计入缺口 / `or_missing` OpenRouter 无对应 / `variant_unmatched` 剥后缀后仍无唯一基准）与相似度 top 3 候选（**仅建议，绝不自动采用**）；面板「设置 → 定价刷新」下新增未定价区域，可为某条模型直接填 OpenRouter id「登记」，映射写入数据目录的 `pricing-overrides.json`（运行期覆盖，不改源码 `OVERRIDES`，升级镜像不丢），提交后立即触发一次取价；留空提交即删除映射。新增 `POST /pricing/mapping`。
- **修好「立即取价」按钮的 404**（顺带）：`POST /pricing/refresh` 此前从未注册进 `do_POST`（`is_account_route` 不覆盖 `/pricing`），面板点「立即取价」实际收到 404；现在与 `/pricing/mapping` 一起走面板会话鉴权的新分支。新增文本级回归测试钉住这两条路由必须在 `do_POST` 里出现。
- 新增 `tests/_test_pricing_auto.py`（40 项）：并集输入、按需补价（含并发幂等与写路径计价）、变体后缀继承与否决项（`kimi-k2-instruct-taiji`、`kimi-k2.8-preview`、5 个别名保持未定价）、缺口分类与候选、面板映射端点与开关。整套增至 32 个套件（27 Python + 5 JS），全绿。
- **悬停即可看清一条请求的价是怎么来的**：最近请求最后一列的金额加了自绘气泡，给出三档原始单价（缓存命中/未命中输入、输出，USD 每百万 token）、汇率与折算说明、匹配来源的证据链（`direct` / `override` / `variant`，`variant` 还会写出基准名与剥掉的后缀）、命中的条件档位与该档单价、策略 id 与首次取到时刻、补算标记；没有定价的行仍只说「暂无定价数据」，不编 0。`/usage/recent` 每行直接带上 `cost_rates` / `cost_unit` / `cost_currency` / `cost_usd_cny` / `cost_or_id` / `cost_via` / `cost_inherited_from` / `cost_override_from` / `cost_band_note` / `cost_via_derived`，不为展示再开接口。**没有动计价口径**：`policy_id` 的算法一字未改，全量 15176 行逐行 `source` 与改动前 0 条失配，历史策略 id 逐条不变，已固化成测试。早于 `via` 字段写下的策略行按当前映射表推断并标注是推断（`via_derived=true`），指不到就不认。新增 `tests/_test_pricing_tooltip.py` 与 `tests/_test_pricing_tooltip.js`（夹具取自 2026-10-02 的线上现场），整套增至 34 个套件（28 Python + 6 JS），全绿。

- **「扫描桌面客户端账号」重新可用：桌面端加密凭据现在能在网关里直接解密**（入口此前因加密改造被隐藏）：桌面客户端从 2026-09-24 起把 `accessToken` / `refreshToken`（国内版还连带 `nickname` / `phoneNumber`）改成 `$wbEncrypted` 信封存储，扫描只能读到信封文本，导入后聊天、刷新凭证、查积分一律 401，当时只能把入口摘掉、让用户改走 OAuth。现在按客户端 `packages/at-rest-crypto` 的同一套方案就地解密（`key = sha256(atRestSecretKey)`、`keyId = sha256(key)[:16]`、AAD 为 `WB-AAD\0` + 版本 + `LP(WBEF1/WBEV1)` + `LP("sym-v1")` + `u32(suite)` + `LP(keyId)` + 帧代码 + 两个 0），入口放回来了。
  - 解码密钥只存在于客户端原生模块的运行内存里、磁盘上没有明文，所以网关从**正在运行的** `WorkBuddy.exe` 主进程内存里把它找回来（`OpenProcess(PROCESS_VM_READ)` + `VirtualQueryEx` + `ReadProcessMemory`，全程只读，不向目标进程写任何东西）；找到后用 keyblob 的 `protectorKeyId` 与一次 GCM 解封双重校验，校验不过就当没找到。密钥只留在网关进程内存中，不落盘、不进日志，网关重启后重新回收一次。
  - 内存回收不再逐字节哈希。三档扫描、命中即止：先按 `atRestSecretKey` 字段名和孤立的 44 字符规范 base64 捞出密钥载荷直接推导（正则走 C 层，约 460 MB/s），不中再按 8 字节栅格逐窗口哈希，最后才逐字节兜底；内存段按 64 MB 切片后均分给多个扫描子进程，任意一个命中其余立刻收工。对比上游那版逐字节脚本（单进程 1.36 MB/s、12 进程 16 MB/s），实测 462 MB 的客户端内存 **0.8 秒**拿到密钥。
  - 新模块 `wb_atrest.py` 只用标准库（自带 AES-256-GCM 与 GF(2^128) 实现，不引入任何 pip 依赖，绿色包内置的 Python 直接能跑）；并行用的是 `sys.executable` 拉子进程，而不是 `multiprocessing`——绿色包那份精简 Python 里根本没有这个模块，用它会直接报错。
  - 实测完整链路（Windows + 真实客户端）：两个 `.info`（国内版 / 国际版）的 token 与昵称都正常解出并导入成功；导入后的国内版账号 `/accounts/test` 直接拿到模型回复，国际版账号的凭证查询接口返回真实积分（顺带确认导入的是可直接使用的那串 token，不是信封）。
- **修掉区域判定的两处旧账**（手动导入 JSON 时「明明是国内版却进了国际版」就是这么来的）：`detect_realm_from_token()` 只认 `copilot.tencent.com` 与 `codebuddy.cn`，而国内版后来把出口换成了 `workbuddy.cn`（新版国内客户端的 JWT issuer 就是 `https://www.workbuddy.cn/…`），这类国内账号一律被判成国际版；JSON 里没有 `realm` 字段就会中招，而且一旦存错，导出再导入还会把这个错误一路带着。现在：
  - 三个国内出口（`copilot.tencent.com` / `codebuddy.cn` / `workbuddy.cn`）都认，国际版认 `workbuddy.ai` / `codebuddy.ai`，两边都看不出时不再假装知道（`realm_evidence()` 返回空，由调用方决定怎么兜底）；
  - 区域判定以 **token 自己的 issuer** 为准，域名只作兜底——同一台机器切区登录过时，`.info` 里的 `domain` 字段可能还是另一边的，不能让它盖过 token；
  - 桌面凭据的文件名（`workbuddy-desktop.info` = 国内、`-ai.info` = 国际）退为提示：同一个文件里登录另一区域账号时按 token 归位；扫描列表和导入用的是同一套判断，不会再出现「列表里写着国内版、导入却跑进国际版」；
  - `X-Domain` 头跟着最终区域走，不会拿着上一边的域名去请求另一边的出口；
  - 新增 `tests/_test_desktop_realm.py`（19 项）：三个国内出口 × 域名/issuer 组合、过期的 `realm` 字段、显式 `realm` 覆盖、导出再导入不漂移。
- 看板：工具栏入口放回；扫描结果新增「已加密 / 待解码」状态，遇到加密凭据会提示先点「回收密钥」（后端在同一接口上新增 `{"recoverKey": true}` / `{"forgetKey": true}` 两个动作，沿用面板鉴权），拿不到密钥时（客户端没开、非 Windows、权限不足）直接把原因显示出来，并引导回 OAuth。
  - 新增 `tests/_test_desktop_atrest.py`（26 项：FIPS-197 的 AES-256 分组向量、信封往返 / 篡改 / 错钥 / 帧隔离、keyblob 自检、路径限制，以及拉起一个靶子进程真跑一遍内存回收、确认密钥不落盘），整套 31 个测试文件全绿。

- **新增：积分与套餐权益包全维度明细查看与到期管理**：
  - 参考 CodeBuddy 官方直连计费接口（`/billing/meter/get-user-resource-summary`、`get-user-resource-free-packages`、`get-user-resource-paid-packages` 与 `checkin-activity-status`），实现各账号积分与权益包的秒级同步与完整解析；
  - **全维度信息透出**：解析各套餐包名称、子产品名称、发放来源/原因（如官方活动发放、裂变拉新、月度赠送等）、资源 ID、订单号、生效时间与精确到秒的到期时间、总容量、已用及剩余可用积分；
  - **到期倒计时与临期提醒**：自动按当前时间计算剩余天数，区分「已过期」、「≤3天即将到期」、「≤7天到期提醒」及「长期有效」；账号池主表实时感知临期状态并在「积分」列透出警示徽章，弹窗内设「最近将过期」概览卡片；
  - **交互体验与筛选排序**：看板支持 Tab 快速筛选（全部 / 有剩余 / 即将到期 / 已用完过期）、多字段模糊搜索（名称/代码/来源）以及四档动态排序（最快到期优先、剩余额度最大、已用最多、总容量最大），并支持一键实时向上游刷新；「积分」列精简为单入口（点击 `[明细]` / `[查询]` 徽章唤起窗口）。
- **新增：看板明/暗主题切换（浅色 / 深色 / 跟随系统）**：
  - 顶部右侧新增主题切换按钮，下拉可选「浅色」「深色」「跟随系统」三档，图标随当前偏好变化（太阳 / 月亮 / 显示器）；
  - 全看板配色改为 CSS 变量驱动，深色主题（`[data-theme="dark"]`）覆盖背景、面板、边框、文字、徽章、模态遮罩等全套组件，`color-scheme` 同步切换；
  - 偏好持久化到 `localStorage`（`wb-theme`），刷新与重开看板保持；内联脚本在 `<body>` 解析前即设定主题，避免首屏白闪；
  - 「跟随系统」实时监听 `prefers-color-scheme` 变化，系统切换深浅色时看板即时跟随；移动端同步适配主题按钮尺寸。

## v1.6.10

- **修复停用账号会丢掉出口绑定**（issue #89，感谢 [@lkxlzx](https://github.com/lkxlzx)）：此前停用账号时会顺手把它的 `proxySlot` 清空（`set_all_enabled` 与启动时的迁移也一样），重新启用不会恢复，那条账号就回落到直连——报告人说的「启用禁用账号后代理出口会被重置为直连」正是这个。现在绑定是操作者的选择，停用/启用不再动它：停用只是不接单，重新启用仍走原来的出口。
  - 槽位卡片的「已绑定」计数依旧只统计**启用中**的账号（表示这条出口当前有谁在用）；要真正解绑就显式选「直连」，或把槽位删掉（删槽位仍会把绑在它上面的账号解绑）。
  - `tests/_test_proxy_slots.py` 与 `tests/_test_proxy_slot_lifecycle.py` 里那几条「停用即释放」的断言改成钉住新行为：停用后绑定仍在、运行时出口不变、重新启用仍走同一槽位。

## v1.6.9

- **修好网页通道打卡：会话会被真正驱动到完成**（issue #90，感谢 [@Saracino34](https://github.com/Saracino34) 的准确定位；issue #75）：v1.6.4 只建了会话，而建会话只是**排队**——agent 要等客户端接上这条会话的沙箱并请求这一轮才会跑，所以网关建的那些会话全部停在 `CREATING`、没有任何输出，第二天自然不加积分（报告人 4/4 复现：手动发的会话十几秒 `completed`，网关建的一条都没动过）。现在按网页端的顺序走完：建会话 → `GET /console/as/conversations/{id}/session` 取沙箱 `link` + `token` → ACP（JSON-RPC over HTTP，服务端事件走 SSE）`initialize` → `session/load` → `session/prompt` → 轮询到 `completed`。实现放在新的 `wb_webagent.py`，只用标准库。
  - 打卡结果里带上会话状态与输出段数（如「网页通道 completed：12 段输出，15420 ms」），跑没跑成一眼可见，不用等第二天看积分；失败时错误里带会话 id。
  - 一轮最多等 120 秒（`WB_WEB_TURN_TIMEOUT` 可调）；实测一条「Hi」18.6 秒跑完、12 段输出。
  - 顺带更正 v1.6.4 的一条判断：`GET /v2/activity/banner` 返回的 `{"code":12302,"msg":"activity is offline"}` 只是 banner 模块自己的状态，不能当作「活动停发」的证据。
- **本地网络工具（`web_search` / `web_fetch`）改成默认关闭的看板开关**（[PR #87](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/87)，感谢 [@Cekxri](https://github.com/Cekxri)）：默认「直通」——工具声明原样透传，客户端自己声明的搜索工具照常拿到调用（v1.5.3 之后的既有行为，升级不受影响）；要在看板「设置 → 本地网络工具」打开，网关才会把声明换成自己的同名函数、在本地执行并喂回模型。关闭时连同名调用的拦截也一并关掉，客户端自己的 `web_search` 不会被吞。
- 新增 `tests/_test_web_agent.py`（6 项，钉住驱动顺序与结果上报）；`tests/_test_daily_chat.py` 扩到 10 项、`tests/_test_local_web_tools.py` 扩到 68 项；整套 29 个测试文件全绿。

## v1.6.8

- **模型列表改为跟随上游 `GET /v3/config` 的实时清单**（issue #85，感谢 [@Jay-Young](https://github.com/Jay-Young)）：此前只认桌面端缓存文件与内置快照，没装桌面端的机器（Docker / NAS / Linux 服务器）拿不到桌面端 picker 的那份列表。现在 `/v1/models` 直接向出口要 `agents[cli].models`——与桌面端同一份清单，缓存文件退为回落。
  - 过滤规则：去掉 5 个档位别名（`default-model`、`fast-model`、`balanced-model`、`primary-model`、`deep-model`）与国内版的 `auto` 路由项，去掉 `-sg` / `-x` 变体，同名的只留 0.00 倍率那一档（国际版留 `deepseek-v4.1-flash`、丢 `-sg`，留 `hy4-preview-f`、丢 `hy4-preview`）。
  - 上游新上的模型无需发版即可出现在 `/v1/models`（表外的新名字按上游顺序追加在末尾）；表顺序与国内版 `hy4-preview-f` 这类免费档的保留不变。
  - 回落顺序：远端 → 桌面端缓存文件 → 窄端点（仍走旧白名单）→ 内置快照；10 秒一次、最多两次（聊天桌面 UA 失败后换应用 UA）。
  - 顺带修掉一处隐性退化：缓存文件是同一份文档但没有 `data` 信封，旧解析只认 `data.agents`，会让缓存路径悄悄退回旧读取器（数量对、元数据丢）；现在两种形态都认，并优先取 `cli` 这个 agent。
- **国际版模型清单补上 `grok-4.7`**：16 → 17，看板国际版专属标记同步。
- 新增 `tests/_test_remote_catalog.py`（10 项）钉住解析、过滤规则、免费同级优先、免发版追加、缓存文件驱动与回落不泄漏窄端点未知名。

## v1.6.5
## v1.6.6
## v1.6.7

- **新增：按 API Key 限制可用模型**（issue #73 由 [PR #84](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/84) 实现，感谢 [@Cekxri](https://github.com/Cekxri)）：每个 Key 可以填一个模型白名单（如 `deepseek*`、`gpt-6-astra`，支持 `*` 通配、多个用逗号分隔），不在名单里的模型请求在网关本地直接返回可读的 400——不送上游、不消耗额度。留空 = 不限制，旧 `settings.json` 读回来一律不限制，升级无需迁移。主要用来挡客户端自己发的背景请求（标题生成、记忆整理、自动复核这类不经过模型选择器、直接按目录模型 ID 发出的调用）。面板 Key 编辑卡新增「模型限制」一栏，设了限制的 Key 会显示徽章。
  - 匹配用 `fnmatch`、大小写不敏感；`deepseek*` 同时覆盖 `deepseek-v4.1-flash` 这种裸 ID 和 `deepseek/deepseek-v4.1-flash` 这种带前缀的形态；精确名字不会连带命中后缀（`gpt-6-astra` 不含 `gpt-6-astra-high`，要连带就写 `gpt-6-astra*`）。
  - `/settings/save` 在提交的行省略该字段时保留已存的值，旧版缓存面板不会把限制洗掉；`/v1/chat/completions` 与 `/v1/responses` 两条路径都会拦。
- **修复 `BLOCK_BACKGROUND_REQUESTS` 误拦使用者的「压缩上下文」**（[PR #86](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/86)，感谢 [@Cekxri](https://github.com/Cekxri)）：该开关的关键字表里有 `compaction`，而使用者按「压缩上下文」时发出的请求 `request_kind` 同样是 `compaction`，于是开关一打开，按钮收到的是拒绝报文而不是摘要。现在按「这次压缩是谁发起的」区分：客户端自己发起的压缩带 `thread_source=memory_consolidation`（继续拦），使用者在自己线程上按的压缩放行；`auto_review` 这类即使跑在用户线程上也仍然拦。新增 `tests/_test_background_requests.py` 钉住区分规则。
- **新增 Docker 镜像发布工作流**（[PR #83](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/83)，感谢 [@xihan123](https://github.com/xihan123)）：Release 发布后自动构建并推送 `linux/amd64` + `linux/arm64` 双架构镜像到 GHCR（`ghcr.io/ardeyouxipianyi/workbuddy2api-hub`，正式版同步打 `latest`），README 补了从 GHCR 拉取运行的说明（GHCR 新包默认私有，要免登录拉取需在 Packages 设置里改为 Public）。


- **新增「每日 Token 限额」：按账号当天用量提前停用、自动切号**（issue #82，感谢 [@RiggTIan](https://github.com/RiggTIan)、[@lkxlzx](https://github.com/lkxlzx)）：上游的免费额度是按 token 计窗口的（如 `deepseek-v4.1-flash` 约 2 亿 / 12 小时），打满后该账号当天只能等窗口重置——报告里「把用满的号停用后，另一个号也请求失败」，实际是上游把第二个号的大请求也判了限额（`code 6004`），而 1 条消息的小请求仍能通过，所以账号行「测试」显示正常、大请求却 429。现在看板「设置 → 每日 Token 限额」填一个数即可：账号当日消耗的 token 达到该值后暂停接单、请求自动切到其他账号，本地时间 0 点后自动恢复；**填 0 表示不限**（默认值）。
  - 计数取自 `usage.jsonl` 里该账号当天的 token 合计，与看板「今日消耗」同一口径（跳过客户端中断的行）；增量扫描 + 15 秒缓存，热路径只读新增的行。计数由日志折算，重启后停用状态依然有效。
  - 被停用的账号在账号行显示「日限额」徽章（悬停可看今日已用 / 上限），池子卡片显示「N 个达日限额」，控制台打印 `account xxx parked: daily token limit reached (...)`；所有账号都达额时请求返回 `429` + `Retry-After`（到本地 0 点），文案说明是本地限额，不碰上游。
  - 定时任务（签到、打卡、保活）不受影响，与「保留积分」一致：只是不接新单。两个限制各自独立、按「或」生效——账号要同时不触发两者才会接单（卡片说明里已写明）。
  - 新增 `tests/_test_daily_token_limit.py`：钉住「0 = 不限」「只有计数过的天才拦」「只统计今天、跳过客户端中断的行、按字节偏移增量折叠」「池子跳过被停账号并发布状态」；`_test_model_cooldowns.py` 的桩池补上了新的池方法。


- **修复代理槽编辑器被轮询刷掉**（issue #79，感谢 [@lkxlzx](https://github.com/lkxlzx)）：点「+ 添加槽位」后刚加的那一行撑不过 15 秒就消失——`loadAccounts()` 挂在 15 秒轮询上，而它会顺带刷新代理槽，刷新是「拉服务端列表 → 整体替换 → 重绘整张表」，那一行还没保存到服务端，于是被旧列表顶掉，正好是报告里说的「还没来得及填写内容就返回了」。（同一个机制也会把已有行的改动打回服务端版本，只是行还在、不容易察觉。）
  - 现在编辑器里有未保存改动时会跳过刷新，「代理槽」标题旁显示「（N 个 · 未保存）」，让「列表为什么不再自动刷新」是看得见的；保存成功后清零、轮询恢复——点「测试」时触发的那次自动保存同样会清零。
  - 新增 `tests/_test_slot_editor.js`：在假 DOM 下加一行、调用轮询用的 `loadProxySlots()`，断言工作副本没有被服务端列表替换；再断言保存之后会正常刷新。

## v1.6.4

- **国际版每日活跃打卡改走网页通道**（issue #75、issue #59）：两位报告人的实测一致——网关自动发出的桌面端身分对话拿不到每日 30 积分，而在网页版手动发一句就能拿到。顺着这条线索抓包后确认：网页版 app 的「对话」根本不是 `chat/completions`，而是 `/console/as/conversations/` 下的 agent 会话，创建会话时带上 prompt，后端就按该 prompt 起一次任务；而且这条链路只用 `Authorization: Bearer <accessToken>` 与 `X-User-Id` 两个凭据头（没有桌面端的 `X-IDE-*` 指纹），所以网关手里同一份账号凭据可以直接调用，不需要额外的网页登录——实测 GET 会话列表、POST batch-get 都返回业务响应而不是 401。
  - 现在国际版打卡是两步：先发一条桌面端身分的轻量对话（保持原行为），再在网页通道建一个带 prompt 的会话；返回结果里会带上会话 id，便于核对是否真的建上。
  - 账号栏新增 **「网页通道打卡 (国际版)」** 按钮：手动为所有已启用的国际版账号各建一个网页端会话，点击后会先弹一次确认（它会真的起任务、消耗少量积分）。这个按钮不写 `lastDailyChat`，所以不会让定时巡检跳过当天的正常打卡流程。
  - 「设置」页新增「国际版每日活跃打卡」开关（默认开启），关掉即回到只发桌面端对话的旧行为；取值同样严格限定 JSON 布尔，字符串一律 400 拒绝。
  - 需要留意：网页通道会真的起一次任务，会消耗该账号少量积分，换来的是每日 30/50 积分活跃奖励；面板上已写明这一点。
  - 另外记录一条上游状态：抓包期间 `GET /v2/activity/banner` 返回 `{"code":12302,"msg":"activity is offline"}`，即该活动模块当前处于下线状态。如果网页端也拿不到积分，原因可能在上游而不在通道——这条留待后续观察。

## v1.6.3

- **修复空状态「登录新账号 (OAuth)」按钮点击无反应**（issue #66，感谢 [@shis23](https://github.com/shis23) 的准确定位）：该按钮调用的是 `startLogin()`，而这个函数早在 v1.1.0 引入 `openLoginModal()` 时就已经不存在了，因此从 v1.1.0 起，账号池为空的首次部署用户点它不会有任何反应，浏览器控制台报 `startLogin is not defined`，而顶部工具栏的同名入口一直正常。现已改为调用真实存在的入口，并新增 `tests/_test_dashboard_handlers.js`：扫描 `dashboard.html` 中全部内联事件处理器，断言每一个都能找到对应的函数定义。这类「按钮绑定了一个不存在的函数」的问题只会在浏览器里、且只在该按钮被点击时暴露，任何服务端测试都看不见它。
- **看板时间范围扩展：本周 / 本月 / 自定义区间**（issue #68）：
  - 除「今日 / 全部历史」外，新增「本周」（周一零点起）、「本月」（1 号零点起）与「自定义」（起止时间自选，任一侧留空表示该侧不限）。口径与既有「今日」保持一致，都是本地零点锚定的自然区间；刻意不提供「最近 7 天 / 30 天」这类滚动别名，否则按钮标签在一周里有六天是错的。
  - `/usage`、`/usage/perf`、`/usage/analytics` 三个取数端点统一接受 `range` / `since` / `until` 参数，KPI 卡片、账号透视表与模型性能表会一起切到同一窗口，第一列的标题同步变为「本周消耗 Token」等，不会再出现「卡片显示今日、表格显示全部」的口径分裂。
  - 缓存键由原来的 today/all 二值改为真实窗口边界：本周与本月是重叠区间，二值键会让其中一个窗口的数字被另一个顶掉。
  - 模型性能表的延迟 / 速度列取自日志末尾的采样，窗口比采样更宽时会在表头注明覆盖起点，不再让局部数据冒充整个窗口。
- **修复出站身分切换后重启即丢失**（issue #76，感谢 [@1766266028](https://github.com/1766266028) 的完整定位与复现）：账号加载时把出站身分硬编码成默认的 WorkBuddy 桌面端，凭证文件里保存的值被读进一个全仓无人使用的字段（`saved_product`），于是面板上的 WB / VSC / CLI 切换（以及启用后的 429 自动切换）虽然确实写进了凭证文件，重启后却一律打回 WB——`set_product()` 的注释承诺「重启后仍然有效」，与实际行为矛盾。现在加载时读回凭证文件中的身分，非法值仍由 `normalize_product()` 回退到默认；同时面板切换在改完内存后立即落盘，不必再等 refresh / 签到 / 查积分之类的路径顺带保存——切完就重启容器的人不会再白白丢掉这次切换。新增 `tests/_test_product_persistence.py`（17 项断言）覆盖加载、别名归一、非法值回退、切换落盘与重载，以及身分最终落到端点与出站标头。

- **429 自动切换出站身分改为面板开关**（issue #67）：切换逻辑本身一直存在（WB / VSC / CLI 轮转、每轮最多 4 次、60 秒内算同一轮、成功即归零），但总开关是源码里的常量 `AUTO_SWITCH_PRODUCT = False`，面板上没有入口，想用只能改代码。现在改为「设置」页的开关，默认关闭（与改动前行为一致），保存后下一次请求即生效，不再需要动源码。取值严格限定为 JSON 布尔：字符串 `"false"` 之类一律 400 拒绝，否则一个真值字符串会把开关悄悄打开，而这正是关掉它的人最不希望发生的事。需要留意的是，开启后切换到的身分同样会随凭证文件持久化（见上一条），重启后不会自动回到 WB——面板上已写明这一点。

## v1.6.2

- **全套测试收拢与官方 CI 流水线建设**（PR #65，感谢 [@teddyli18000](https://github.com/teddyli18000)）：
  - 将散落在根目录的 20 个测试套件整齐规整至 `tests/` 目录下；
  - 新增统一测试运行器 `tests/run_all.py`，支持一键隔离运行全部 20 个测试套件或按关键词过滤；
  - 引入官方 GitHub Actions 自动化 CI 流水线（`.github/workflows/tests.yml`），每次提交与 PR 自动覆盖 Ubuntu（Python 3.9/3.12）与 Windows 跨平台测试矩阵。

## v1.6.1

- **修复 Docker 部署默认无鉴权开放代理漏洞**（PR #64，感谢 [@teddyli18000](https://github.com/teddyli18000)）：容器 CMD 默认追加 `--lan` 启动并移除写死的 `--port 8788`。无显式 `API_KEY` 时将自动生成高强度 Key 持久化保存并打印在日志中，拒绝匿名公网调用，消除未授权盗刷风险，同时支持通过 `PORT` 环境变量动态指定内部端口。
- **修复签到与活跃打卡后视图强制跳转**（PR #63，感谢 [@teddyli18000](https://github.com/teddyli18000)）：拆分 `refreshActiveRealm()` 与 `initRealm()`，国内签到和国际版每日活跃打卡完成后仅更新出口状态与用量，不再将当前浏览的区域视图强行跳回默认出口。

## v1.6.0

- **国际版每日活跃自动打卡领 30/50 积分**（issue #59）：官方国际站订阅规则规定「通过客户端发起有效对话可领每日活跃 30 积分（Pro 为 50 积分），网页端对话不计入」。现为国际版账号新增每日活跃自动化支持：
  - 后台调度器排程自动在 09:00 / 21:00 巡检时为当日未活跃的国际版账号发送一条轻量微型对话（默认走官方 `WB` 客户端出站标头与低消耗模型）；
  - 看板切换至国际版视图时，顶部工具栏提供「每日活跃打卡 (国际版)」一键触发按钮；
  - 严格记录 `lastDailyChat`，保证每个账号每天仅触发一次，不浪费额度。

## v1.5.9

- **修复 OmO / OpenCode 子代理 11128 WAF 拦截**（PR #62，感谢 [@Sakura1618](https://github.com/Sakura1618)，issue #61）：在 `deepseek-v4.1-flash` 上驱动 OmO 等多智能体调度框架时，上游 WAF 会对 `Sisyphus-Junior - Focused executor from OhMyOpenCode` 这一连续短语进行指纹特征匹配并拒流返回 `code: 11128 (Illegal API invocation from an unapproved channel)`。现于脱敏管线中针对性将该短语清洗为 `Sisyphus-Junior - Focused executor`（去掉末尾归属文本），既保留子代理业务身份与指令执行，又彻底消除拦截。

## v1.5.8

- **隐藏「扫描桌面客户端账号」入口**：桌面客户端自 2026-09-24 起把 `accessToken` / `refreshToken` 改成加密存储（`$wbEncrypted` 信封），扫描仍能读到文件，但拿不到可用的 token——导入后聊天、刷新凭证、查积分全部返回 401。入口已隐藏，请改用 OAuth 添加账号；相关代码（前端 `scanDesktop()` 与后端 `/accounts/import/desktop`）保留未删，等解密打通或改走其他凭据来源后再放出来。
- **两个按钮改名**：「一键自动分配出口」→「分配代理出口给未绑定账号」（它只给尚未绑定出口的已启用账号轮询分配，已有绑定的账号不动，原名容易被读成重新平衡全部账号；同时补了 tooltip 并修正两条 toast 的措辞）；账号行的「刷新」→「刷新凭证」（换的是该账号的登录凭证，不是页面、积分或账号列表）。
- **README 全面精简**：345 行压到 305 行、字符数减少约 23%，事实与贡献者记录一条未删；顺带修掉两处已失效的说法——头部特性里的「亦支持扫描本地客户端导入」，以及 Docker 那节整段的桌面凭据挂载说明。

## v1.5.7

- **`tool_choice="none"` 不再删除工具声明**（PR #57，感谢 [@zhangzm0](https://github.com/zhangzm0)，issue #56）：此前客户端发 `tool_choice="none"` 时，`normalize_tool_choice()` 会把 `tools` / `functions` 声明整个删掉。模型失去结构化工具通道后，把调用降级成 DSML／伪 JSON 文本塞进 `content`（`tool_calls` 为空、`finish_reason=stop`），Agent 客户端解析不到调用只能再追问一轮，模型重复一遍 —— 上下文每轮 +2 条消息、token 线性膨胀，直到撑爆窗口或用户手动断开。现在保留工具声明，由 `tool_choice` 字段自己表达「本轮不许调用」；上游只认字符串，对象形式仍降级成字符串（发对象会 11101）。实测上游并不真正遵守 `tool_choice="none"`，保留声明后它仍可能返回 `tool_calls`——这比让 Agent 原地空转好；确实需要禁止调用时，请由客户端不传 `tools`。

## v1.5.6

- **Docker 部署下的 Linux 桌面凭据挂载**（PR #55，感谢 [@LuFering](https://github.com/LuFering)）：新增 `docker-compose.override.yml.example`，以只读方式把宿主机 `~/.local/share/CodeBuddyExtension/Data/Public/auth` 挂进容器，补上 Linux + Docker 场景下看板扫描不到桌面凭据的说明；`.gitignore` 同时忽略本地 `docker-compose.override.yml`。
- **保留积分开关**（issue #44）：看板「设置」新增最低保留积分，账号余额低于该值时不再接单，避免余额被用尽后触发上游的提醒短信。填 `0` 关闭（默认）；从未查询过余额的账号不受影响；账号只是停止接单，仍在池中并继续定时任务，充值后自动恢复。阈值保存在 `accounts/settings.json` 的 `reserve_credits`，改动即时生效、无需重启。

## v1.5.5

- **出站身分改为三套模式**：账号行新增 `WB` / `VSC` / `CLI` 三档切换，默认 `WB`（WorkBuddy 独立桌面客户端，`X-IDE-Type: WorkBuddy`），另可切到官方 VSCode 插件（`VSCode`）或官方 CodeBuddy CLI（`CLI`），三者各自对应不同的出站指纹与端点。原先的两档实现把桌面端与插件端混为一谈，且默认走 CLI。
- **国际版 CLI 端点修正**：`www.codebuddy.ai` 在实测网络上无法解析（getaddrinfo 失败，系统解析器回 0.0.0.1 空路由），国际版 CLI 身分改走 `www.workbuddy.ai`，该域名接受 CLI 头并正常应答。此前国际版账号在默认身分下直接 502。
- **国际版模型列表对齐官方客户端**（issue #51）：现为 16 个，取自官方缓存 `agents[0]` 声明的真实模型（已排除 5 个档位别名与同名的 SG 区域变体）。补上 `glm-5.3-flash`（0.06x）与 `kimi-k2.8-preview`（0.77x），移除官方并未提供的 `hy4-preview` 与 `gpt-5.3-codex`。
- **`kimi-k2.8-preview` 解除国内独占限制**：此前被 `CN_EXCLUSIVE` 拦下并提示“请改用对应出口的 Key”，但官方国际版账号实测可正常调用（HTTP 200 且正常出内容），现已在两个区域同时开放。同类误判的 `glm-5.1`、`glm-5v-turbo`、`minimax-m3` 已实测可用但未动，留待后续处理。
- **国内版 `deepseek-v4.1-flash` 倍率修正**（issue #51）：看板此前对该模型写死显示「独家优惠 0.03x」，与实际上游计价的 0.11x 无关（官方国内版缓存中该模型没有任何促销折扣），现已改为直接沿用上报倍率。内置快照同步由 0.03 修正为 0.11。
- **`/health` 鉴权状态修正**（PR #52，感谢 [@teddyli18000](https://github.com/teddyli18000)）：`api_key_required` 此前只反映启动参数里的 Key，仅配了面板 Key 时会误报 `false`，与 `/v1` 实际拒绝无 Key 请求的行为矛盾。现改为复用手持路径的判定。
- **单模型限流可视化**（PR #50，感谢 [@teddyli18000](https://github.com/teddyli18000)）：`/accounts` 新增 `modelCooldowns`，看板账号行显示受限模型与本地恢复时间；429 状态改由独立短锁保护，避免看板读取与请求线程更新竞争。
- **国内账号昵称容错**：国内桌面端把昵称存成 `{"$wbEncrypted": ...}` 加密信封，此前会被 `str()` 成一整行字典画在账号行上；现在非字符串值一律回退显示 UID 前缀。

## v1.5.4

- **国内版目录补上 `hy4-preview-f`**：内置静态目录里只有旧 id `hy4-preview`（x0.29），它不在白名单里会被裁掉，而 `hy4-preview-f` 只能靠本机桌面端缓存补进来——没装过国内版桌面端的机器上该模型会消失。现按桌面端缓存补进静态目录（x0.00、1M 输入 / 64k 输出、推理档 high）。
- **看板显示积分消耗与账号昵称**（PR #45，感谢 [@Pro-XK](https://github.com/Pro-XK)）：最近请求表新增「积分」列，账号列改显示昵称（tooltip 保留完整 uid，账号不在池中时回退 uid 前缀）；「网关调用量」卡片副标题追加累计积分；账号透视表新增「消耗积分」列。
- **积分口径统一**：卡片与透视表此前一个只累计成功请求、一个含失败请求，同一页面上两个「消耗积分」永远对不上。现统一为「上游实际计费过的请求都计入，客户端取消不计」，并各自写明覆盖范围；credit 为 0 的行显示 `0.00` 而非 `—`。

## v1.5.3

- **移除网关内置的 `web_search` / `web_fetch` 代跑**（issue #43）：实测上游本来就没有服务端搜索能力（声明与不声明工具时模型反应一致、调用次数为 0），而代跑实现有参数名只认 `query`、工具重复下发、失败时发合成 `resp_wrapup` 把失败伪装成正常结束三处缺陷。现工具声明原样透传，客户端自己声明的搜索工具会正常拿到调用。

## v1.5.2

- **修复 Docker 镜像缺少运行时模块**（PR #41，感谢 [@wiggins-kong](https://github.com/wiggins-kong)）：Dockerfile 的显式 COPY 清单漏掉 v1.5.0 新增的 `wb_identity.py` 与 `wb_webtools.py`，容器启动即 `ModuleNotFoundError`。现改为 `COPY wb_*.py dashboard.html ./`。仅影响 Docker 部署，绿色包与本地运行不受影响。

## v1.5.1

- **看板时间范围与筛选修正**（issue #39）：「今日 / 全部历史」此前只影响部分指标卡，现首张卡跟随切换、第二张固定为累计并注明差异原因；模型性能表跟随所选范围（`/usage` 与 `/usage/perf` 新增 `range` 参数），并新增「账号」「模型」筛选，汇总行随筛选重算、失效筛选自动清除。
- **修复账号用量透视表丢失**：该表格标记曾被误删，`getElementById` 恒为 null，整个「各账号用量透视」区块从未渲染；现恢复并适配移动端卡片布局。
- **新增测试**：`_test_usage_range.py`（22 项断言）与 `_test_matrix_filters.js`（19 项断言）。

## v1.5.0

- **Codex App namespace 工具支持**（PR #33，感谢 [@Cekxri](https://github.com/Cekxri)）：展开 `namespace` 后转发，回程补上该字段；同时支持 `agent_message`（子代理）与无 `call_id` 的 `function_call_output`。
- **出站身分标头修正**（PR #33）：原 `X-Product: WorkBuddy` 为自创组合，官方为 `X-Product: SaaS`；账号行可按需切换 WB / VSC / CLI 三套身分。
- **本地 `web_search` / `web_fetch`**（PR #33）：客户端声明时由网关代跑（v1.5.3 已移除）。
- **DeepSeek 多轮 `reasoning_content` 回填补全**（PR #36，感谢 [@ayeaaaa](https://github.com/ayeaaaa)）：thinking 开启即回填，并把字段镜像到 `reasoning` 且保证非空；与 v1.4.9 的档位注入互补。
- **看板移动端布局**（PR #37，感谢 [@ayeaaaa](https://github.com/ayeaaaa)）：新增 `≤640px` 手机布局与 `≤400px` 微调，桌面布局不变。
- **API Key 行 id 唯一化**（PR #40，感谢 [@wiggins-kong](https://github.com/wiggins-kong)）：避免两行同 id 时 `/settings/reveal` 返回别人的 key；读取时也去重，历史文件自愈。
- **修复 `/v1/responses` 非流式路径崩溃**：该路径引用了未定义的 `ns_map`，任何非流式请求都会抛 `NameError` 断开连接；流式路径不受影响。

## v1.4.9

- **DeepSeek 思维链默认开启**：此前只注入 `thinking:{type:"enabled"}` 而不带推理档位，上游仍按「不思考」应答。现缺档时按模型目录声明的默认档补齐（无声明回退 `high`）；客户端显式档位不覆盖，`thinking:{type:"disabled"}` 与 `reasoning_effort:"none"` 照常退出。
- **工具调用配对自愈**：客户端写不回工具结果时，坏历史被每轮重放、上游对之后每条消息返回 `400 code 11148`，一次失败调用即可报废整条会话；并行调用间插入的消息（如 Codex 的 `image_resize_notice`）同样打断配对。现出站前把结果块移回所属批次，并按同一份 id 集合对称裁剪孤儿。
- **`prompt_cache_key` 注入（默认关闭）**：按账号隔离的缓存键（`wb2a-<uid8>-<摘要>`），用 `WB_PROMPT_CACHE_KEY=1` 开启。默认关闭是因为实测该上游本就会复用重复前缀，带不带结果一致。
- **新增 `_test_upstream_repairs.py`**（49 项断言，无网络依赖）。

## v1.4.8

- **HTTP 连接同步修复**（PR #30）：请求被提前拒绝时未读取请求体，会让后续请求在同一 keep-alive 连接上解析失败（日志表现为空请求行的伪 414）；同时支持 chunked 请求体、`Expect: 100-continue`、超大请求体立即 413。
- **超长请求行回复丢失修复**：414 后直接关闭会因未读数据触发 RST，客户端收不到响应；现先有限度排空再回复。
- **macOS 启动脚本**（PR #31）：新增 `start-wb-proxy.sh` / `.command`、局域网版本与防火墙助手；Windows `.bat` 未修改。

## v1.4.7

- **每账号独立出口代理**（PR #26，感谢 [@ayeaaaa](https://github.com/ayeaaaa)）：新增可命名、可启停的代理槽位，账号绑定后其全部出站请求固定走该出口；看板支持槽位增删、出口 IP 测试与逐账号绑定。
- **账号身份请求全量走代理**：`refresh` / `checkin` / `fetch_credits` 此前从宿主机真实 IP 发出，会把账号身份与宿主 IP 关联在一起。
- **槽位 ID 不再回收**：ID 改由持久化计数器分配，删除槽位时同步解绑指向它的账号。
- **顶部 GitHub 仓库入口**。

## v1.4.6

- **看板数据口径与展示修正**：指标看板固定展示两区合计，不再跟随当前出口；模型性能表按「模型 × 出口 × 账号」逐行展开，新增「失败」列与三色分列。
- **看板会话与页面保持**：会话失效后立即停止轮询并清除旧凭证，不再刷 401 日志；刷新后保持所在页面。

## v1.4.5

- **GPT 系列流式 Token 与生成速度修复**：忽略中间帧全 0 的 usage 占位，并加入断流 Fallback 估算，修复 `gpt-5.6-luna` / `gpt-6-astra` 等模型输入输出为 0、生成速度缺失的问题。

