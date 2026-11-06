# WorkBuddy2API-Hub — 国际版、国内版多账号网关中枢

> [!CAUTION]
> **项目停止维护（2026-11-06）**：后续不再更新代码、不跟进上游变化、不再处理新的 issue / PR。感谢大家的 star。

> [!IMPORTANT]
> **项目进入低频维护（2026-10-23） / Low-frequency maintenance**
>
> 大幅降低更新频率与 Issue / PR 处理速度。请先阅读风险说明，再决定是否继续使用。
>
> Updates and responses to issues / pull requests will be significantly reduced. Read the risk notice below before continuing.

> [!WARNING]
> **WorkBuddy 上游正在收紧对非官方客户端的识别与拦截。**
>
> - **封禁**：2026-10-23 下午起，非官方客户端的识别与拦截明显收紧。
> - **维护者**：近期工作压力增大，无法保证及时更新与响应。
> - **可用性**：上游可能随时进一步收紧，导致本项目不可用。
> - **账号风险**：使用非官方客户端造成的风控、限流或封禁，由使用者自行承担。
>
> **Upstream enforcement** tightened detection and blocking of unofficial clients from the afternoon of 2026-10-23. The maintainer is under increased workload and cannot guarantee timely updates or replies. The upstream may tighten further at any time; rate limits, account restrictions or bans caused by using an unofficial client are the user's responsibility.

本项目仅用于协议学习、研究与个人自用。请优先使用官方客户端，不要使用主账号，不要共享账号，也不要用于商业转售。

This project is for protocol research and personal use only. Prefer the official client, do not use your primary account, and do not resell or share access.

<p align="center">
  <a href="https://github.com/ardeyouxipianyi/workbuddy2api-hub/releases"><img src="https://img.shields.io/badge/Release-v2.0.0-2496ED?style=flat-square" alt="Version 2.0.0"></a>
  <img src="https://img.shields.io/badge/Python-3.9+-blue.svg?style=flat-square" alt="Python">
  <img src="https://img.shields.io/badge/API-OpenAI_Compatible-412991?style=flat-square" alt="OpenAI API">
  <img src="https://img.shields.io/badge/Dual_Realm-Intl_&_CN-0DBD8B?style=flat-square" alt="Dual Realm">
  <img src="https://img.shields.io/badge/License-MIT-green.svg?style=flat-square" alt="License">
  <img src="https://img.shields.io/badge/Maintenance-Ended-9f2d2d?style=flat-square" alt="Maintenance ended">
</p>

## 核心特性

- **标准 API 兼容**：将 WorkBuddy（国际版/国内版）与 CodeArts 封装为标准 OpenAI 格式（Chat Completions / Responses），无缝支持 Codex、Claude Code、NextChat 等各类客户端。
- **开箱即用与多端部署**：便携版免装环境（Windows 自带精简运行时），支持 Docker、NAS（群晖/飞牛）一键部署及 OpenWrt 路由器固件。
- **多账号池与出口隔离**：支持浏览器 OAuth 快速导入、每账号独立出口代理槽绑定、设备指纹隔离、自动故障转移与配额轮转。
- **自动化福利与签到**：内置国内版全套成长任务、加油站签到与猫猫旅行，支持国际版每日活跃会话与 CodeArts 自动签到。
- **可视化管理看板**：提供 Web 仪表盘，支持实时请求日志、Token/积分透视、多 API Key 权限分发（模型/账号白名单）与额度护栏。

> ⚡ **Vibe Coding 产物**：本项目为 100% Vibe Coding 协同产物——人类开发者提出架构与业务意图，AI 助手端到端完成逆向分析、链路调度、WAF 指纹脱敏与界面编写。

> 📌 **提交代码贡献前**：请先查看并**严格遵循**贡献手册 [`CONTRIBUTING.md`](CONTRIBUTING.md)——提交格式、测试门禁与端到端冒烟要求都在那里（AI / 智能体请特别注意其中的硬性规则节）。

---

## 💖 赞助商

> 想出现在这里？联系 [ardeyouxipianyi@gmail.com](mailto:ardeyouxipianyi@gmail.com)

<table>
<tr>
<td width="180" align="center" valign="middle">
  <a href="https://agentrouter.org/register?aff=unrV"><img src="https://raw.githubusercontent.com/88lin/picx-images-hosting/master/90C5FAD072EA247822CB88BB32512A41.webp" alt="Agent Router" width="150"></a>
</td>
<td valign="middle"><b><a href="https://agentrouter.org/register?aff=unrV">Agent Router</a></b>&nbsp;是免费公益大模型 API 平台，支持 Claude-opus-5.5、GPT-6 Astra、GPT-6.1 Sol、deepseek-v4-flash 等主流模型，国内直连。注册送 ＄175（每日签到得 ＄25，被邀得 ＄50），支持 GitHub / LinuxDo 登录。</td>
</tr>
</table>


---

## 一、快速启动

### 1. 本机运行

- **Windows**：双击 `start-wb-proxy.bat`（静默后台运行 + 独立日志窗口）；如需前台控制台窗口，双击 `start-wb-proxy-visible.bat`。
- **macOS / Linux**：双击 `start-wb-proxy.command` 或在终端执行 `./start-wb-proxy.sh [端口]`（默认 8788，需要 Python 3.9+）。
- **局域网共享模式**：运行 `start-wb-proxy-lan.bat`（Windows）或 `./start-wb-proxy-lan.sh`（macOS/Linux），首次启动将自动生成高强度 API Key 并在控制台打印。
- 启动后访问：
  - **Web 看板**：`http://127.0.0.1:8788/`（默认访问密码 `admin`，首次登录请在设置中修改）
  - **API 地址**：`http://127.0.0.1:8788/v1`

### 2. Docker 部署

预编译多架构镜像发布在 GHCR，无需本地构建：

**方式一：一键脚本（推荐）**

```bash
# 官方源（直连 GitHub 环境）：
curl -fsSL https://raw.githubusercontent.com/ardeyouxipianyi/workbuddy2api-hub/main/quick-deploy.sh | bash

# 国内网络 / NAS 加速：
curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/ardeyouxipianyi/workbuddy2api-hub/main/quick-deploy.sh | sudo bash
```

> 公网部署或配合反向代理：在管道前指定 `WB_DEPLOY_MODE=public bash`，并搭配 Nginx / Caddy / Cloudflare Tunnel 配置 TLS。

**方式二：单文件 Docker Compose**

```yaml
services:
  wb-proxy:
    image: ghcr.io/ardeyouxipianyi/workbuddy2api-hub:latest
    container_name: wb-proxy
    restart: unless-stopped
    ports:
      - "8788:8788"
    environment:
      - HOST=0.0.0.0
      - PORT=8788
      - TZ=Asia/Shanghai
    volumes:
      - ./accounts:/app/accounts    # 账号凭据与配置（持久化）
      - ./usage:/app/usage          # 用量流水日志（持久化）
```

更新镜像：`docker compose pull && docker compose up -d`。

### 3. 启动入口：优先用 `-m`

同一份代码、同一个账号池，`python3 wb_proxy.py` 与 `python3 -m wb_proxy` 的常驻内存差 **47-54 MB**
（Windows 11 / aarch64、Python 3.12-3.14 四套环境实测一致，见 #702）。成因仍在排查，
但只要换成 `-m` 就能拿到这段收益——512 MB 的设备上约 10%。

```bash
cd /path/to/workbuddy2api-hub        # 必须先进入 wb_proxy.py 所在目录
python3 -m wb_proxy --port 8788
```

`-m` 把**当前目录**放进 `sys.path`，而脚本方式放的是脚本所在目录，所以务必先 `cd`。
仓库内的资源路径都基于 `__file__` 解析，不受影响。Docker 镜像和 OpenWrt 包已改用 `-m`；
Windows / macOS 的 `start-wb-proxy.*` 启动脚本暂未改动。

### 4. CodeArts 原生第三出口

网关内置进程内直连华为云 CodeArts，支持聊天 SSE 协议转换与自动签到，无需单独运行第三方桥接服务：
1. 打开看板 →「设置 → CodeArts 接入」，勾选「启用 CodeArts 出口」并保存；
2. 切换至看板顶部「CodeArts」视图，点击「+ 添加账号 (OAuth)」完成华为云授权；
3. 在「设置 → API Key」中新建 Key 并绑定出口为 `CodeArts`（或全局通用）即可调用。

CodeArts 的模型 ID 按上游目录**精确匹配**：`deepseek-v4.1-flash` 与
`deepseek-v4.1-flash-1017` 是两个独立模型，不会互相别名或静默回退。
`/v1/models?realm=codearts` 返回的是账号当前可用的真实目录 ID；调用时请直接使用
返回的完整 ID。

---

## 二、账号接入与 API Key 权限

### 1. 账号添加与代理出口绑定
- 打开看板 `http://127.0.0.1:8788/`，点击「+ 添加账号 (OAuth)」，在浏览器中完成官方登录授权即可自动入库。
- **独立代理出口槽（Proxy Slot）**：在看板中可按需配置并命名 HTTP/SOCKS5 出口代理（支持账号密码认证），支持将账号与特定代理槽位绑定，账号身份请求与出站调用均自动走该代理。

### 2. 多 API Key 权限管理
- 「设置」页支持创建多个独立 API Key：
  - **出口绑定**：可固定走国际版、国内版或 CodeArts；
  - **模型限制**：支持通配符白名单（如 `deepseek*`, `gpt-6-astra`），违规请求本机直接返回 400；
  - **账号白名单**：可精确限定该 Key 仅允许调度的账号范围；
  - **用量透视**：数据看板按 Key 独立统计调用次数、Token 消耗、缓存命中与积分。

---

## 三、客户端接入配置

- **Base URL**：`http://127.0.0.1:8788/v1`（局域网为 `http://<局域网IP>:8788/v1`）
- **API Key**：看板「设置」中添加并绑定的 Key
- **模型名称**：调用 `/v1/models` 中列出的任意官方对齐模型 ID（如 `deepseek-v4.1-flash`, `gpt-6-astra`, `hy3`）

**Codex CLI / Responses API**

```bash
export OPENAI_BASE_URL="http://127.0.0.1:8788/v1"
export OPENAI_API_KEY="你在看板设置中添加并绑定的API_Key"
```

**Claude Code / Anthropic Messages（实验性）**

网关支持实验性 Messages 协议适配，因长会话缓存命中率差异，默认关闭：
```bash
# 网关端设置 WB_ENABLE_ANTHROPIC_MESSAGES=1 开启后生效：
export ANTHROPIC_BASE_URL="http://127.0.0.1:8788"
export ANTHROPIC_API_KEY="你在看板设置中添加并绑定的API_Key"
```

---

## 四、常用环境变量

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `8788` | 网关监听端口 |
| `API_KEY` | 自动生成 | 初始 API Key（看板配置多 Key 后优先由看板管理） |
| `WB_DEPLOY_MODE` | `lan` | 部署模式：`lan`（0.0.0.0）、`local`（127.0.0.1）、`public` |
| `WB_MAX_PAYLOAD_BYTES` | `16777216` | 单请求体上限（字节，默认 16MB，支持 1–256 MB） |
| `WB_ENABLE_ANTHROPIC_MESSAGES` | `0` | 是否开启实验性 Anthropic Messages 协议转换 |
| `WB_BACKGROUND_MODEL_TARGET` | 留空 | 后台模型别名改写目标（如 DSH 客户端自动映射） |
| `WB_CA_BUNDLE` | 留空 | 自签根证书路径（供抓包调试或中间代理使用） |
| `WB_CN_CHAT_UPSTREAM` | 官方默认 | 国内版出站代理地址 |
| `WB_IDENTITY_OS` | 宿主系统 | 仅 CLI 身份：覆盖 `x-stainless-os`（如 `Linux` / `Windows`） |
| `WB_IDENTITY_ARCH` | 宿主架构 | 仅 CLI 身份：覆盖 `x-stainless-arch`（如 `x64` / `arm64`） |
| `WB_IDENTITY_NODE_VERSION` | 官方默认 | 仅 CLI 身份：覆盖 `x-stainless-runtime-version` |

遇到上游 `400 / 11-128` 时，先按 [`docs/TROUBLESHOOTING-11-128.md`](docs/TROUBLESHOOTING-11-128.md) 收集同一请求的
身份、最小请求和提示词对照结果；不要先轮换账号。

---

## 五、主要 API 端点

| 接口 | 方法 | 端点 | 说明 |
|---|---|---|---|
| 聊天补全 | `POST` | `/v1/chat/completions` | 标准 OpenAI 聊天接口（支持流式 SSE 与思维链回填） |
| 交互式响应 | `POST` | `/v1/responses` | OpenAI Responses 协议接口（Codex 专用） |
| 模型目录 | `GET` | `/v1/models` | 获取当前出口对齐的官方可用模型列表 |
| 生图 | `POST` | `/v1/images/generations` | 文生图 / 图生图（需在设置中启用媒体模块） |
| 生视频 | `POST` | `/v1/videos` | 异步生视频任务提交（本地并发排队与空闲调度） |
| 视频状态 | `GET` | `/v1/videos/<id>` | 轮询视频生成进度与结果 |
| 产物下载 | `GET` | `/v1/videos/<id>/content` | 下载视频 MP4 产物直链 |
| 健康检查 | `GET` | `/health` | 网关运行状态、可用账号数与就绪模型分布 |

---

## 六、测试验证

```bash
python tests/run_all.py            # 全部套件
python tests/run_all.py realm      # 只跑名字里含 realm 的
```

- 273 个套件：206 个 Python + 67 个 JS；JS 需要 PATH 上有 `node`，缺失时会跳过并提示。
- `tests/_mobile_check.py` 是独立的 Playwright 手机/桌面布局检查器（需自行安装 Playwright），按需手动运行，不在上面的套件集里。
- CI（`.github/workflows/tests.yml`）跑同一条命令：Ubuntu 上 python 3.9 与 3.12（3.9 是本项目声称的最低版本），Windows 上 python 3.12；推送 `v*` tag 时额外断言 **tag == 源码版本**（`-ci` 演练 tag 豁免）。

---

## 七、版本更新记录 (Changelog)

### Unreleased

- **用量聚合缓存跳过写盘时不再静默停更**：checkpoint 写盘因状态锁被占、状态暂不可序列化或写入异常而跳过时，按类节流告警并登记后台异步补写（30 秒起指数退避，封顶 15 分钟）；补写成功立即取消，失败继续重试。新增 64MB checkpoint 落后告警、蓝绿双进程临时文件隔离，避免 `remaining` 一类缓存连续多日不落盘、重启后反复全量重折。
- **设置页分组在正體中文 / 英文界面不再整页空白**：分组胶囊靠各区块的 `h2` 标题文字比对归类，界面语言切到正體中文或英文后标题已被 i18n 换掉，比对全部落空、每个区块都归到「日志与系统」，于是点「基础 / 自动化 / 稳定性」时把整页 section 全部 hidden（看起来设置页清空）。现在改用 i18n 翻译前保留的原文比对，三种语言的分组结果一致；另补上漏归的「选择网页活跃提示词」（原来被归到「日志与系统」）。
- **账号分组**（#707）：账号新增可选分组，支持按组筛选、导入时指定分组、单账号编辑、分组启停，以及刷新凭证 / 测试 / 补积分等批量操作仅作用于当前分组；API Key 账号选择器也支持按组选入与移除。
- **API Key 配置示例**（#704）：设置页新增可折叠的 Base URL、cURL 与各协议端点示例，并支持一键复制。
- **检查更新支持代理槽**（#798）：可在设置页指定 GitHub 更新检查使用的代理槽；槽停用或删除后自动回退直连。
- **会话粘滞修复**（#718）：小账号池的长会话不再按页轮换账号，避免前缀缓存丢失和 Token 消耗翻倍。
- **请求体超限 413**（#799）：先返回 413，再在有限时间内排空上传，避免连接重置吞掉响应。
- **CodeArts 参数与模型 ID**（#794 / #797）：输出 token 按模型上限收敛、`stop` 字符串归一化；参数错误返回 400 且不冷却账号。`deepseek-v4.1-flash` 与 `-1017` 为独立模型，精确匹配。
- **提示词改写与模型倍率**（#796 / #774）：恢复退化为空操作的 `SANITIZE_REWRITES`；live 目录的 `x0.00` 不再覆盖已知付费倍率。
- **风控连坐暂停保护可配可立即解除**：设置页「稳定性」新增「风控连坐暂停保护」卡片。上游 403 / 11140 异常行为触发全区域暂停派发时，面板展示实时剩余倒计时，提供「立即解除暂停」一键解冻恢复请求；支持自定义暂停时长（默认 1800 秒，设为 0 可彻底关闭连坐熔断保护），且保存更短时间时即时截断现有倒计时。
- **账号多时面板更顺**：账号表改为客户端分页（每页 50 个，排序与筛选作用在全部账号上再切页；改区域 / 改筛选 / 点表头回第 1 页，账号变少时页码夹回最后一页）；15 秒账号轮询拿回的数据与视图都没变时不再整表重画，且只在显示账号数据的页签（网关、账号、任务，新导航的总览）上跑，切回这些页签时立即拉一次；自动补积分改为只点名缺积分的号（`POST /accounts/credits` 新增 `uids` 列表参数，非列表回 400），每个号每次页面加载最多补一次，不再触发全池逐个刷。手动「一键刷新积分」不变。
- 移除已废弃的 `WB_DEEPSEEK_FLASH_FORCE_SG` 文档行与两个恒返回 `False` 的存根函数（#776）。
- **CodeArts 模型倍率**：`/v1/models?realm=codearts` 的条目新增 `credits`（如 `x0.7`，与 WorkBuddy 目录同一写法），取自上游模型目录的标准档；错峰优惠档不计入，上游没有倍率的模型不带此字段。
- **改善 Claude Code 兼容性**（#733）：`sanitize` 现在把
  `You are Claude Code, Anthropic's official CLI tool for Claude.` 整句中性化；
  旧规则只把 `CLI for Claude` 改写成 `CLI tool for Claude`，反而保留了
  `You are Claude Code` 指纹。覆盖大小写与弯引号，`user` / `tool` 内容不改写。
- **添加账号时可指定登录出口代理槽**：OAuth 登录弹窗新增「登录出口代理」下拉（只在配置了启用的代理槽时出现），申请授权链接、等待回调与拉取账号资料都走选中的槽，登录成功后账号绑定到该槽，避免登录 IP 与后续调用 IP 不一致。默认「直连（不走代理槽）」，与以前完全一样；选「自动」(`proxySlot: "auto"`) 时随机挑一个没绑定账号（含 CodeArts 账号）、也没被其他进行中登录占用的槽，没有空闲槽则直连。已绑定槽的账号用「自动」重登会保留原槽；槽绑定不写进账号的旧版 `proxy` 字段。`/accounts/login/start` 的日志与响应只给代理的 host:port，不含用户名密码；不存在或已停用的槽返回 400。
- 启动检查不再把"端口能连上但一直不答"当成端口空闲。此前 `--probe-only` 用 2 秒超时探 `/health`，卡死的实例只会让它超时，于是判定端口可用、又叠了一层实例；Windows 的 `SO_REUSEADDR` 允许重复监听，内核仍把新连接分给卡死的那个，看板与日志查看器就一直是空的。现在这种情况明确报错（退出码 1）并给出占端口 PID 的查法，不再静默重复启动。
- 部署入口改用 `python3 -m wb_proxy`（OpenWrt init 与 Dockerfile）：同一份代码实测空闲内存少 47-54 MB（#702 / #777）。
- 新增 `WB_DUMP_11_128` 本地取证开关（默认关闭）：`1` 只写脱敏摘要，`full` 额外写出站
  body，文件落在 `usage/11-128-dumps/`；用于定位 Claude Code 专属 11-128 的真实请求差异
  （#733）。摘要不含提示词明文，`full` 含明文但由使用者显式开启。
- **国际版每日活跃总开关**：设置页「国际版每日活跃打卡」新增「定时自动打卡」勾选框（`settings.json` 键 `daily_activity`，`/settings/save` 只收 JSON 布尔）。缺省开启，老安装行为不变；关掉后调度器不再自动跑每日活跃（定时时段、立即巡检保活、启动补跑都跳过，启动补跑留到重新打开后再消费），账号上的手动打卡不受影响。
- **用量缓存写盘不再拖住其他视图**：checkpoint 写盘收集各类聚合状态时，状态锁改为非阻塞尝试；某一类正在锁里全量重折（GB 级 `usage.jsonl` 冷启动要几十分钟）时这一类本次跳过、下次再写，不再连带 `/usage/by-account` 等视图一起卡住（面板「账号加载失败」/ Cloudflare 524）。

完整历史版本与详细变更日志请查阅 **[docs/CHANGELOG.md](docs/CHANGELOG.md)**。

---

## 八、致谢与引用声明 (Credits & References)

在协议逆向、风控规避、任务链路与工程化设计过程中，本项目参考、吸纳并致谢以下优秀开源项目与技术成果（排名不分先后）：

### 核心逆向与协议参考
- **[Sliverkiss/workbuddy2api](https://github.com/Sliverkiss/workbuddy2api)**：成长任务逆向、设备指纹派生（`derive_id`）、调度器（`Scheduler`）与 `reasoning_content` 回填设计；
- **[L0NE-6/WorkBuddy-Daily](https://github.com/L0NE-6/WorkBuddy-Daily)**：国内版微信小程序专属事件指纹与动态专家深链任务模型；
- **[88lin/workbuddy-auto-signin](https://github.com/88lin/workbuddy-auto-signin)**：猫猫日常旅行与成长能量开盲盒（Buddy 盲盒）安全生命周期；
- **[linguo2625469/workbuddy2api-panel](https://github.com/linguo2625469/workbuddy2api-panel)**：国内版连登管家闭环逻辑与轻量对话活跃上报事件；
- **[Zhengyuuuui/workbuddy2api](https://github.com/Zhengyuuuui/workbuddy2api)**：国际版 11140 风控限制识别、冷却规避与故障排查清单；
- **[yuanyuanyuan430/workbuddy2api-hub](https://github.com/yuanyuanyuan430/workbuddy2api-hub)**：国际版 EdgeOne CDN 证书错配排查与活跃上报优雅降级方案；
- **[CangShui/workbuddy-cliproxy-fix](https://github.com/CangShui/workbuddy-cliproxy-fix)** & **[lovingfish/workbuddy-cliproxy](https://github.com/lovingfish/workbuddy-cliproxy)**：早期客户端代理修复与网关通信原型；
- **[mmqz/cpa-multi-plugins](https://github.com/mmqz/cpa-multi-plugins)**：多插件管理与出站调度结构参考；
- **[ardeyouxipianyi/workbuddy2api](https://github.com/ardeyouxipianyi/workbuddy2api)**：国内版早期分发包逆向分析与出站 User-Agent 规范；

### 工程化与生态基础
- **[aodianjun/workbuddy2api-hub](https://github.com/aodianjun/workbuddy2api-hub)**：OpenWrt 路由器打包配方（`.ipk` / `.apk`）与系统服务支持；
- **[astral-sh/python-build-standalone](https://github.com/astral-sh/python-build-standalone)**：Windows 便携版免安装精简独立 Python 运行时构建基石；
- **[deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)**：DSH 客户端协议与提示词指纹清洗规避参考。

感谢所有向本项目提交 PR、反馈 Issue 与分享实测样本的社区贡献者！

---

## 九、免责声明 (Disclaimer)

本项目仅用于协议学习与个人开发测试，不提供任何商业服务或可用性担保。因使用非官方客户端产生的账号风控、限制或封禁由使用者自行承担，请优先使用官方客户端。

## ⭐ Star 趋势

<p align="center">
  <a href="https://star-history.com/#ardeyouxipianyi/workbuddy2api-hub&Date">
    <img src="https://api.star-history.com/svg?repos=ardeyouxipianyi/workbuddy2api-hub&type=Date" alt="Star History Chart" width="760">
  </a>
</p>
