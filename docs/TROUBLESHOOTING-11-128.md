# 11-128 渠道校验排障

上游返回的 `400 / code 11-128 / Illegal API invocation from an unapproved
channel` 是**请求内容或客户端身份**没有通过渠道校验，不是账号被封。换账号、
换 API Key 通常不会改变结果；请先拿到同一条失败请求的真实差异，再决定改什么。

本文只列可复现的检查步骤。上游的判定规则没有公开，`11-128` 的原因不能仅凭
网关日志推断；每一步都要用同一请求做前后对比。

## 1. 先区分“账号问题”和“请求问题”

1. 在面板里对同一个账号点一次「测试」。面板测试使用最小请求；如果它能成功，
   说明账号凭据和基本出站地址是通的，客户端实际请求的 **body 或客户端头**更
   值得怀疑。
2. 用同一个 API Key 发一条最小请求（见第 4.1 节）。如果最小请求成功、Codex /
   Claude Code 的实际请求失败，优先按请求内容排查。
3. 记录错误 JSON 里的 `requestId`。后续日志、`usage/usage.jsonl` 和上游反馈
   都用它对齐。

不要通过轮换账号来“消掉” 11-128：同一条被拒绝的 body 换多少个账号仍然会被
拒绝，轮换只会把额度耗在重复请求上。

## 2. 收集最小证据

请提供以下信息，且**不要附带完整 API Key、Authorization、Cookie 或设备 token**：

- 网关版本、宿主系统（Windows / Ubuntu / macOS）、启动方式；
- 客户端与协议（Codex / Claude Code / OpenAI SDK；`/v1/responses` 或
  `/v1/chat/completions`）；
- `model`、`stream`、是否带 `tools` / 图片 / MCP；
- 账号区域（国际版 / 国内版）和出站身份（面板账号卡上的 WB / VSC / CLI）；
- 完整错误 JSON（至少保留 `code`、`msg`、`requestId`）；
- 面板设置的提示词模式（透传 / 追加 / 替换）；
- 同一条请求在面板「测试」和客户端里分别成功还是失败。

日志与用量行的定位命令：

```powershell
# Windows PowerShell
Select-String -Path usage\usage.jsonl -Pattern '<requestId>|11-128'
# 面板「日志」页导出，或带鉴权请求：
Invoke-WebRequest http://127.0.0.1:8788/logs/export -OutFile wb-proxy.log -Headers @{Authorization="Bearer <API_KEY>"}
```

```bash
# Linux / macOS
rg -n '<requestId>|11-128' usage/usage.jsonl
# 面板「日志」页导出，或带鉴权请求：
curl -sS -H "Authorization: Bearer <API_KEY>" http://127.0.0.1:8788/logs/export -o wb-proxy.log
```

`usage.jsonl` 的失败行通常带 `status`、`message`、`account`、`realm`、
`proxy_slot` / `proxy_exit`（如果走了代理槽），以及成功请求才有的
`system_sha` / `msgs_sha` / `n_msgs`。不要只贴一行 `[failed]` 徽章。

### 2.1 抓取出站 body（可选，默认关闭）

网关可以只在遇到 `400 / 11-128` 时把**实际发往上游**的 body 落到本机。
这是显式开启的诊断开关，默认不写任何文件；复现一次后就可以关掉并重启。

PowerShell：

```powershell
$env:WB_DUMP_11_128 = "full"
.\start-wb-proxy.bat 8788
# 复现一次失败后：
Get-ChildItem usage\11-128-dumps
```

Linux / macOS：

```bash
WB_DUMP_11_128=full ./start-wb-proxy.sh 8788
# 复现一次失败后：
ls usage/11-128-dumps
```

两种取值：

- `WB_DUMP_11_128=1`：只写摘要。包含消息角色 / 长度 / 哈希、工具名、已知指纹命中、
  脱敏后的请求头与上游错误体，**不含提示词明文**；
- `WB_DUMP_11_128=full`：摘要 + `*.body.json`（或 `.body.bin`），内容就是当时发往
  上游的 body，已解压 gzip。**会含对话明文**，分享前请自行检查。

Authorization、API Key、Cookie、设备 token 等头在这两种模式下都不会落盘。
同一个 body 的重试只写一份，避免客户端重试刷出大量文件。

## 3. 查看有效的出站身份

仓库自带一个**不发网络请求**的诊断脚本。它会为每个账号构造真实 chat 头，
但把 `Authorization`、`X-API-Key`、`X-Device-Token`、用户/企业标识等字段
替换成 `[REDACTED]`：

```bash
python scripts/diagnose_identity.py --accounts-dir accounts
```

只看某个账号或预览另一种身份：

```bash
python scripts/diagnose_identity.py --accounts-dir accounts --uid <uid前8位>
python scripts/diagnose_identity.py --accounts-dir accounts --product cli
```

输出里的关键字段：

- `product`：这一份头属于 WB、VSC 还是 CLI；
- `endpoint`：该身份实际使用的上游地址；
- `stainless_headers`：只有 CLI 身份才会出现 `x-stainless-*`；
- `headers`：已脱敏的完整头族，可直接和另一台机器对比；
- `environment.effective.os` / `arch` / `node`：进程当前解析出的 SDK 宿主值。

把这份 JSON 与 Windows / Ubuntu 两边的结果并排比较。**WB 和 VSC 身份不发
`x-stainless-*`**，所以在这两种身份上，设置 `WB_IDENTITY_OS` /
`WB_IDENTITY_ARCH` 不会改变出站头；不要把它当成通用修复。

## 4. 三项对照实验

### 4.1 最小请求

先用不含系统提示词、工具和历史的最小请求确认基础链路。下面示例里的
`$env:WB_API_KEY` / `$WB_API_KEY` 请替换成面板里创建的 API Key，不要把真实
Key 贴进 issue。

PowerShell：

```powershell
$headers = @{ Authorization = "Bearer $env:WB_API_KEY"; "Content-Type" = "application/json" }
$body = '{"model":"deepseek-v4.1-flash","messages":[{"role":"user","content":"hi"}],"stream":false}'
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8788/v1/chat/completions' -Headers $headers -Body $body
```

Linux / macOS：

```bash
curl -sS http://127.0.0.1:8788/v1/chat/completions \
  -H "Authorization: Bearer $WB_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4.1-flash","messages":[{"role":"user","content":"hi"}],"stream":false}'
```

结果解释：

- 最小请求成功、实际请求失败：优先检查客户端注入的 system / developer 内容、
  工具定义和历史；
- 两者都失败：继续对比第 3 节的出站身份，以及出口代理 / TLS / 设备 token。

### 4.2 隔离系统提示词

在面板「设置 → 全局提示词」把模式临时改成「替换」，正文使用最小中性提示词：

```text
You are a helpful assistant. Answer directly and concisely.
```

保存后重试同一条失败请求：

- 成功：问题在客户端 system / developer 提示词或提示词拼接方式，记录触发字段；
- 仍失败：不要只盯着系统提示词，继续检查客户端头、设备标识和 body 中的工具/
  历史内容；
- 测试完成后恢复原来的提示词模式。

### 4.3 只有 CLI 身份才比较平台字段

如果账号卡显示 **CLI**，可在一台机器上临时覆盖 SDK 平台字段并重启网关：

```bash
export WB_IDENTITY_OS=Windows
export WB_IDENTITY_ARCH=x64
export WB_IDENTITY_NODE_VERSION=v22.21.1
./start-wb-proxy.sh 8788
```

Windows PowerShell：

```powershell
$env:WB_IDENTITY_OS = "Linux"
$env:WB_IDENTITY_ARCH = "x64"
$env:WB_IDENTITY_NODE_VERSION = "v22.21.1"
.\start-wb-proxy.bat 8788
```

这是一项**对照实验**，不是已证实的通用修复。只有 `product=cli` 的请求会带
这些字段；WB / VSC 身份不要据此修改平台变量。实验后用第 3 节脚本确认
`environment.effective.os` 和 `headers.x-stainless-os` 确实变了。

### 4.4 国内版设备 token

国内版桌面身份可能在出站头里带 `X-Device-Token`；国际版和 CLI 身份不带。
用诊断脚本查看是否出现 `X-Device-Token`（值已被脱敏），再比较两台机器的
`accounts/settings.json` 是否配置了相同的 `device_token` 或
`device_token_file`。

这是排查项，不是 11-128 的已证实根因。没有真实 token 时不要伪造一个随机值；
网关自己也不会为 CLI 身份生成设备 token。

## 5. 提交问题时应附什么

一个可处理的最小包：

1. 完整错误 JSON（含 `requestId`，不含密钥）；
2. `python scripts/diagnose_identity.py` 的输出；
3. 最小请求的结果（成功 / 失败 + 状态码）；
4. 提示词模式对照实验的结果；
5. `usage/usage.jsonl` 对应失败行，以及网关日志中的 `request error` 行；
6. Windows 与 Ubuntu 的 `environment`、`product`、`headers` 差异。
7. 如维护者要求，附上 `usage/11-128-dumps/` 里本次失败对应的摘要；分享 `full`
   生成的 body 前先检查其中的提示词与业务数据。

如果第 3 节显示两种机器只有 `x-stainless-os` 不同，而账号身份是 WB 或 VSC，
请先不要把它当成根因；此时平台变量根本不在请求里。

## 6. 不要做的事

- 不要为了避开 11-128 批量换账号或换 Key；
- 不要提交完整的 `Authorization`、API Key、设备 token 或 Cookie；
- 不要只改测试断言来让 CI 变绿；
- 不要把“某次手动请求成功”当成客户端实际请求已经修复。
