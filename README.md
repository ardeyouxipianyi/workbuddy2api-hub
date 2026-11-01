# WorkBuddy2API-Hub — 国际版、国内版多账号网关中枢

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
  <a href="https://github.com/ardeyouxipianyi/workbuddy2api-hub/releases"><img src="https://img.shields.io/badge/Release-v1.10.0-2496ED?style=flat-square" alt="Version 1.10.0"></a>
  <img src="https://img.shields.io/badge/Python-3.9+-blue.svg?style=flat-square" alt="Python">
  <img src="https://img.shields.io/badge/API-OpenAI_Compatible-412991?style=flat-square" alt="OpenAI API">
  <img src="https://img.shields.io/badge/Dual_Realm-Intl_&_CN-0DBD8B?style=flat-square" alt="Dual Realm">
  <img src="https://img.shields.io/badge/License-MIT-green.svg?style=flat-square" alt="License">
  <img src="https://img.shields.io/badge/Maintenance-Low--frequency-c96f3d?style=flat-square" alt="Low-frequency maintenance">
</p>

把腾讯 **[www.workbuddy.ai](https://www.workbuddy.ai)**（国际版）与 **[codebuddy.cn](https://www.codebuddy.cn)**（国内版）的原生服务封装成标准 OpenAI 兼容接口（Chat Completions / Responses），并提供默认关闭的实验性 Anthropic Messages 适配；同时补齐多账号调度与运维能力：

- **开箱即用**：绿色包自带精简 Python（Windows），双击脚本即启；
- **双区域独立路由**：国际版 / 国内版各自配置与调度，看板一键切换，状态落盘；
- **协议与风险**：默认支持 Chat Completions、Responses API（Codex）；Anthropic Messages（Claude Code / Anthropic SDK）经协议转换且缓存兼容性尚未验证，默认禁用，仅可显式选择启用；
- **模型目录对齐官方桌面端**：剔除补全通道与专线变体，能力与规格按桌面端宣告，上游新模型无需发版即进 `/v1/models`；
- **设备指纹隔离 (`derive_id`)**：以账号 UID 稳定派生机器码与会话标识，同一账号不漂移、不同账号互不关联；
- **账号接入**：看板点链接走浏览器 OAuth；Windows 上还能直接从运行中的桌面客户端导入（加密 token 就地解密），macOS / Linux 可手动粘贴解码密钥后导入；
- **每日自动化**：国内版签到 / 成长与积分任务 / 猫猫旅行；国际版每日活跃打卡（真跑完网页会话）；后台调度器按 09:00 & 21:00、22:00、01:00 排程；
- **限额护栏**：保留积分 · 每日 Token · 每日积分 · 按模型 Token 四条账号级护栏，单账号独立计数，本地 0 点解封（默认全部关闭）；
- **OpenRouter 价估算**：把 token 折算成等价花费（人民币 / 美元可切），逐条请求可悬停查看定价来源；
- **多 Key 与出口绑定**：每个 Key 可固定走国际版或国内版、可限定模型，用量按 Key 归属；
- **Web 看板**：指标卡片、模型性能与用量大表、请求流水、账号与任务管理一屏可查。

> ⚡ **Vibe Coding 产物**：本项目为 100% Vibe Coding 协同产物——人类开发者提出架构与业务意图，AI 助手端到端完成逆向分析、链路调度、WAF 指纹脱敏与界面编写。

> 📌 **提交代码贡献前**：请先查看并**严格遵循**贡献手册 [`CONTRIBUTING.md`](CONTRIBUTING.md)——提交格式、测试门禁与端到端冒烟要求都在那里（AI / 智能体请特别注意其中的硬性规则节）。

---

## 一、快速启动

### 1. 本机运行

**Windows** 双击 **`start-wb-proxy.bat`**：默认静默启动（`pythonw` 后台运行 + 独立日志窗口，关掉日志窗口不会停服务，用面板电源按钮或 `restart-wb-proxy.bat` 停止）；想用「前台控制台 + 关窗即停」，双击 **`start-wb-proxy-visible.bat`**。**macOS** 双击 **`start-wb-proxy.command`**（首次被 Gatekeeper 拦截时右键 →「打开」确认一次），或在终端执行 `./start-wb-proxy.sh [端口]`（默认 8788）。

启动后：**API 地址** `http://127.0.0.1:8788/v1`，**Web 看板** `http://127.0.0.1:8788/`。首次启动还没有账号，打开看板点「+ 添加账号 (OAuth)」完成授权即自动入库。

> 绿色包自带的精简 Python 运行时只覆盖 Windows；macOS / Linux 直接使用系统 Python，需要 **3.9+**（启动脚本找不到内置运行时会明确打印所用的解释器与版本）。Docker 部署自带基础镜像，不受影响。

> zip 解压后若提示权限不足，先执行一次：`chmod +x start-wb-proxy.sh start-wb-proxy.command start-wb-proxy-lan.sh start-wb-proxy-lan.command allow-firewall.command`

### 2. 面板访问密码

看板需要**面板访问密码**（默认 `admin`），它与 API Key 相互独立：密码只用于打开看板，可在「设置」页修改（或启动时 `--panel-password` 指定），以 PBKDF2-SHA256 摘要存于 `accounts/settings.json`（不存明文），登录态只留在浏览器会话里。

本机 / 受信局域网自用可带 `?pwd=面板密码` 免手输直接进面板（如 `http://127.0.0.1:8788/?pwd=admin`）；公网暴露时不要用——密码会留在浏览器历史与反向代理日志里。注意它和局域网共享的 `?key=` 不同：`?key=` 只把 API Key 存给 `/v1` 用，不会自动登录面板。**首次登录后请立即改掉默认密码。**

### 3. 局域网共享

- **Windows** 双击 `start-wb-proxy-lan.bat`（静默）或 `start-wb-proxy-lan-visible.bat`（前台窗口，关窗即停）；**macOS** 双击 `start-wb-proxy-lan.command`，或 `./start-wb-proxy-lan.sh [端口] [Key]`；
- **Base URL** `http://<本机局域网IP>:8788/v1`；带密钥直达面板 `http://<IP>:8788/?key=生成的Key`；
- **API Key**：首次启动生成高强度随机 Key，写入 `accounts/settings.json` 并打印在终端，重启复用；也可用第二个参数指定自己的 Key；
- **macOS 防火墙**：首次监听端口时选「允许」；macOS 15+ 还要在「系统设置 → 隐私与安全性 → 本地网络」允许终端，`./allow-firewall.command` 可查看状态并加白。

### 4. 多 API Key 与出口绑定

「设置」页可管理多个 Key，每个 Key 独立出口——不同客户端各用各的 Key，国内外流量互不干扰：

- **生成 / 绑定**：填名称点「生成随机 Key」；可固定走 🌐 国际版或 🇨🇳 国内版，不绑定则跟随看板顶部的全局出口开关；
- **模型限制**：可填允许的模型（如 `deepseek*`、`gpt-6-astra`，支持 `*`，逗号分隔），留空不限；不在列表里的请求本机直接返回 400，不送达上游、不消耗额度；
- **区域自检**：Key 绑定的出口与模型不匹配时（如国际版 Key 调国内独占的 `deepseek-v4-pro`）直接返回可读 400，而不是上游晦涩的 WAF 报错；
- **启停 / 删除**：可单独启停；删除后密钥立即失效，条目以只读形式留在「设置」页的「已删除」折叠区，历史用量仍显示它的名字；
- **用量归因**：「数据看板」页按 Key 列出请求数、Token、缓存命中、积分与模型分布，口径与账号表一致；
- **防冲突**：面板保存过 Key 后，启动参数里的 `--api-key` 自动失效。

### 5. Docker 部署

预编译双架构镜像（`linux/amd64`、`linux/arm64`）发布在 GHCR 与 Docker Hub，**无需克隆代码、无需本地编译**：

**方式一：一键脚本（推荐）**

```bash
# 官方源（可直连 GitHub 环境）：
curl -fsSL https://raw.githubusercontent.com/ardeyouxipianyi/workbuddy2api-hub/main/quick-deploy.sh | bash

# 国内网络 / NAS 加速（Connection reset 时用）：
curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/ardeyouxipianyi/workbuddy2api-hub/main/quick-deploy.sh | sudo bash
```

NAS（如飞牛 fnOS）普通用户没有 Docker 权限时，把结尾的 `bash` 换成 `sudo bash`。再次运行同一条命令即平滑升级，账号与用量数据不受影响。

**部署模式**：默认 `lan`（`8788:8788`，局域网内可访问）。部署到公网服务器、或只想本机能连时，把环境变量放在管道后的 `bash` 前面：

```bash
curl -fsSL https://raw.githubusercontent.com/ardeyouxipianyi/workbuddy2api-hub/main/quick-deploy.sh | WB_DEPLOY_MODE=public bash
```

- `local` / `public` 只把宿主端口绑到 `127.0.0.1`，容器内仍是 `HOST=0.0.0.0`（Docker 转发需要）；
- `public` 请再套 Nginx / Caddy / Cloudflare Tunnel 并加 TLS，不要把 8788 直接开到公网；
- 已经存在的 `docker-compose.yml` 不会被自动改写端口映射，脚本只提示手动调整。

**方式二：NAS / 面板单文件 Compose**

在 NAS 或面板的 Compose 界面新建项目并粘贴以下内容保存启动，无需拉取源码：

```yaml
services:
  wb-proxy:
    image: ghcr.io/ardeyouxipianyi/workbuddy2api-hub:latest   # 或 ardeyouxipianyi/workbuddy2api-hub:latest
    container_name: wb-proxy
    restart: unless-stopped
    ports:
      - "8788:8788"          # 左侧宿主端口可自选；右侧必须与下面的 PORT 一致
    environment:
      - HOST=0.0.0.0
      - PORT=8788
      # - API_KEY=your_secret_key   # 留空则自动生成并打印在启动日志
      - TZ=Asia/Shanghai
    volumes:
      - ./accounts:/app/accounts    # 账号凭证与配置（更新/重建容器不丢）
      - ./usage:/app/usage          # 用量流水日志（更新/重建容器不丢）
```

更新：`docker compose pull && docker compose up -d`（重建容器）。面板的「安装更新」按钮在镜像部署里默认不可用——镜像里既没有 systemd unit，也没有让 `wb_update_portable.available()` 成立的应用内 `release-manifest.json`；需要注册外部执行者（见下一节），而它只替换应用层文件，不会重建镜像。

**无 systemd 部署的面板一键更新**：容器 / LXC / 便携版没有 systemd 时，面板默认不能安装更新。部署方可以注册一个外部执行者：

1. 让网关能写请求：挂载一个目录（例如 `/run/workbuddy2api-hub-updater`），设置 `WB_UPDATE_INSTALL_ENABLED=1`、`WB_UPDATE_REQUEST_DIR=<目录>`，并设置 `WB_UPDATE_EXECUTOR=external`（或在该目录放一个 `executor.json`）。
2. 执行者监听 `<目录>/request.json`，读取 `action` / `tag` / `version` / `pid` / `port` / `root`，执行下载 → 校验 `SHA256SUMS` → 备份 → 停进程 → 替换 → 等 supervisor 拉起 → 探针 `/health`；失败回滚。`action` 缺省为 `install`，`rollback` 表示用本地备份恢复旧版本（不下载）。参考实现：`wb_update_external.py --request-dir <目录>`（需要 supervisor 在网关退出后自动拉起）。
3. 执行者写 `<目录>/status.json`，`state` 使用 `queued` / `downloading` / `verifying` / `backing_up` / `stopping` / `applying` / `restoring` / `restarting` / `succeeded` / `restored` / `rolled_back` / `failed`，面板沿用现有状态机。

保留历史版本备份：设置页可选 0–3 份（默认 1），更新成功后把旧版本的程序文件留在 `updates/backup-<这次更新去的版本>/`（内容其实是更新前的版本），面板「版本回退」据此列出可回退的版本。`0` = 不留备份，回退不可用。

约束：`accounts/`、`usage/` 不参与替换；执行者必须有应用目录写权限；网关只写请求，不碰自己的代码。

**方式三：Watchtower 自动静默更新**

```bash
docker run -d --name wb-proxy-watchtower --restart unless-stopped \
  -v /var/run/docker.sock:/var/run/docker.sock \
  containrrr/watchtower:latest --interval 86400 --cleanup wb-proxy
```

**补充与排错**

- **持久化**：`./accounts` 与 `./usage` 由宿主机挂载，更新或重建容器都不丢数据；
- **docker run 快捷启动**：
  ```bash
  docker run -d --name wb-proxy --restart unless-stopped -p 8788:8788 \
    -v $(pwd)/accounts:/app/accounts -v $(pwd)/usage:/app/usage \
    ghcr.io/ardeyouxipianyi/workbuddy2api-hub:latest
  ```
- **鉴权**：容器以 `--lan` 启动，无显式 `API_KEY` 时自动生成 Key 写入 `./accounts/settings.json` 并打印在日志里：`docker compose logs wb-proxy | grep -i "api key"`；
- **镜像名要带 registry**：写成 `ardeyouxipianyi/workbuddy2api-hub` 会去 Docker Hub 找并报 `pull access denied`，正确写法是 `ghcr.io/ardeyouxipianyi/workbuddy2api-hub:latest`（GHCR 公开包，拉取无需登录）；
- **目录权限**：默认以 root（`0:0`）运行；想以宿主用户跑就设 `PUID` / `PGID`（或 `--user $(id -u):$(id -g)`），并确保两个挂载目录对该 uid 可写；
- **健康检查**：镜像自带 `HEALTHCHECK`（每 30s 探一次 `/health`），`docker ps` 的 STATUS 列会显示 healthy / unhealthy；
- **源码构建**：`docker compose -f docker-compose.build.yml up -d --build`。

### 6. CodeArts 第三出口

CodeArts 的 OAuth、token 续期、账号池、模型发现、聊天 SSE 和签到都由本进程直接完成，不需要再跑独立的 codearts2api 服务。

- 顶部出口卡片从 `国际版 / 国内版` 变成 `国际版 / 国内版 / CodeArts`；
- 选中 CodeArts 时，`/v1/models` 返回 CodeArts 的模型目录，`/v1/chat/completions`、`/v1/responses`、Anthropic Messages 都由本进程直连华为云；
- API Key 可以绑定 `codearts` 出口，三个出口可以同时用；
- 看板「账号」页的 CodeArts 视图可以添加（OAuth）、启用、停用、清冷却、签到、删除 CodeArts 账号。
- CodeArts 账号支持绑定「账号代理槽」：在账号表的「出口」列选择槽位后，聊天、模型发现、token 续期、签到、积分查询都走该槽；槽位地址变化自动跟随，禁用或删除槽位回退直连。首次 OAuth 换取 token 时还没有账号可绑定，仍直连。

**首次配置**

1. 打开 wb-hub 看板 →「设置 → CodeArts 接入」，勾选「启用 CodeArts 出口」并保存。不需要 Base URL 或 API Key。
2. 切到 CodeArts 视图，点「+ 添加账号 (OAuth)」完成华为云授权。
3. 在「设置 → API Key 与出口绑定」里新建 Key，出口选 `CodeArts`，或留空跟随顶部默认出口。

**口径与限制**

- 账号、token 续期、冷却、签到、并发都由本进程直接执行；华为云凭据只存在本机 `accounts/codearts/`。
- CodeArts 积分余额通过 `snap-manager/v1/statistics/plugin` 查询，看板显示剩余 / 总额 / 已用；单次请求的积分扣减上游不返回，最近请求表的积分列显示 `0.00`、估算价格显示 `—`。
- CodeArts 出口不参与 WorkBuddy 的积分/额度护栏，也不参与 `国际版 → 国内版` 的跨出口回退。
- API Key 的「允许账号」白名单同样限制 CodeArts 请求；没有列入任何 CodeArts 账号时不会使用其他账号。面板的账号选择当前主要面向 WorkBuddy 账号。
- CodeArts 出口暂不支持图片输入；包含图片的请求会返回错误，不会静默丢弃图片。
- CodeArts 出口没有 wb-hub 的 web_search / web_fetch 代跑；客户端自带工具照常透传。
- `/codearts/*` 管理端点只接受面板会话，不接受普通 API Key；删除账号不可恢复。

### 7. 测试

```bash
python tests/run_all.py            # 全部套件
python tests/run_all.py realm      # 只跑名字里含 realm 的
```

- 244 个套件：181 个 Python + 63 个 JS；JS 需要 PATH 上有 `node`，缺失时会跳过并提示。
- `tests/_mobile_check.py` 是独立的 Playwright 手机/桌面布局检查器（需自行安装 Playwright），按需手动运行，不在上面的套件集里。
- CI（`.github/workflows/tests.yml`）跑同一条命令：Ubuntu 上 python 3.9 与 3.12（3.9 是本项目声称的最低版本），Windows 上 python 3.12；推送 `v*` tag 时额外断言 **tag == 源码版本**（`-ci` 演练 tag 豁免）。

---

## 二、核心特性

### 1. 模型列表对齐官方桌面端

对官方本地配置清单（50+ 底层模型）做清洗：剔除行内补全专用模型（`codewise-*`、`completion-gf`、`hunyuan-3b/7b` 等）与多云专线变体（`*-volc`、`*-lkeap` 等），严格对齐官方 Windows 桌面端，并宣告每个模型的上下文窗口、单次最大输出、视觉支持、工具调用与推理档位。

- **🌐 国际版（17 个）**：`hy4-preview-f`、`hy3`、`deepseek-v4.1-flash`、`gpt-6-astra`、`gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna`、`gpt-5.5`、`gpt-5.4`、`grok-4.7`、`gemini-3.5-flash`、`glm-5.3-flash`、`glm-5.3`、`glm-5.2`、`kimi-k3`、`kimi-k2.6`、`kimi-k2.8-preview`
- **🇨🇳 国内版（14 个）**：`hy4-preview-f`、`hy3`、`deepseek-v4.1-flash`、`deepseek-v4-pro`、`glm-5.3`、`glm-5.3-flash`、`glm-5.2`、`glm-5.1`、`glm-5v-turbo`、`minimax-m3`、`kimi-k3-1`、`kimi-k2.8-preview`、`kimi-k2.7`、`kimi-k2.6`

清单与上游 `GET /v3/config` 的 `agents[cli].models` 保持同步（接口不可用时依次回落到桌面端缓存文件、内置快照）；过滤规则：去掉 5 个档位别名与 `auto`、去掉 `-sg` / `-x` 变体、同名的只留 0.00 倍率那一档。上游新上架的模型无需发版即出现在 `/v1/models`。

> 同名模型（如 `deepseek-v4.1-flash`）**暂未实现跨国内 / 国际账号的混合轮询**：两个区域作为独立出口分别配置与调度，请求只走当前所选网关。这是出站指纹对齐与账号防风控的取舍，待长期实测确认稳定后再补。

### 2. 设备指纹隔离（`derive_id`）

国际版与国内版共用同一套算法内核：以账号 UID 结合固定业务盐值单向哈希派生机器码与会话标识——同一账号每次出站都来自同一台虚拟设备（不随机漂移），不同账号之间彼此独立（阻断跨账号关联风控）。

### 3. 国内版自动化

- **每日签到**：一键完成国内版打卡领积分；
- **自动连续打卡与连登管家**：每日定时为国内版账号上报轻量会话事件（复刻客户端 `chat_request_send`），点亮官方成长中心连登天数与热力墙；自动闭环检查昨日漏签（有补签卡则自动补签保连登）、领取新手礼包/补偿、兑换 7d/14d/28d 连登档位奖励，并自动抽完抽奖次数；
- **成长任务与积分任务**：自动批量接取未接任务，构造规范行为事件上报点亮（画布创建、灵感案例、模板使用、模型体验、多轮对话等 14 项），并自动领奖入账；
- **猫猫日常**：自动检查旅行状态，在家自动派出、归来自动领奖。

### 4. 后台常驻调度器

常驻后台，按固定整点执行自动化运维排程：

- **每日 09:00 & 21:00**：国内版账号自动签到、对话活跃上报（点亮连登）与猫猫旅行闭环；国际版账号执行每日活跃打卡（领官方每日 30/50 积分福利）；
- **每日 22:00**：集中扫描全库账号，Token 剩余寿命不足 2 小时自动调用 Refresh Token 保活；
- **每日 01:00**：深夜时段执行夜猫子任务；
- 看板顶部另有「立即巡检保活」与「每日活跃打卡 (国际版)」可随时手动触发。

### 5. 限额护栏（设置 → 账号限额）

四条账号级护栏放在**同一张表**里，每行一条、每列一个作用域：

| 护栏 | 作用 |
| --- | --- |
| 保留积分 | 账号余额低于该值时不再接单，避免余额被用尽后触发上游的提醒短信 |
| 每日 Token 限额 | 账号当日消耗的 token 达到该值时暂停接单，请求自动切到其他账号 |
| 每日积分限额 | 账号当日消费的积分达到该值后只服务免费模型，需要花积分的模型自动切号 |
| 按模型每日 Token 限额 | 账号在单个模型上当日消耗的 token 达到该值时只禁该模型，同账号其他模型照常 |

- **全局默认 + 可选分版本**：每条护栏的「全局默认」同时作用于国际版与国内版；勾上「分别设置国际版 / 国内版」后可单独设值，某版本**留空即继承全局**（占位符会写出继承到的数字），取消勾选再保存等于清回继承。
- 每条填 `0` 表示关闭（默认值）；四条都按**本地时间 0 点**解封，只在请求路径生效（定时任务不受影响），且都是**单账号独立计数**。
- 免费 / 付费以**各出口自己的模型目录**为准，未知模型按付费处理；账号行会显示对应徽章与 `模型 · N tok 达限` 标记。
- 某个出口的全部账号都达额时，该出口的请求返回 `429`（文案说明本地 0 点恢复，`Retry-After` 指向 0 点）。
- 存储：`accounts/settings.json` 里的一组 `limits` 映射（`{"global": …, "intl": …, "cn": …}`，`null` 表示继承全局）；旧版写在外层的四个扁平键会在第一次读取时自动折入。

### 6. OpenRouter 价估算（等价 token 花费）

把每条请求的 token 消耗按 **OpenRouter 公布的模型价**折算成等价金额，回答「这些 token 放在 OpenRouter 上值多少钱」——与账号实际扣除的积分是两个口径，看板里并列显示：

- **总开关**（「设置 → 模型价格估算」，默认开启）：关掉后价格列、「API 等价花费」卡片、取价控件与未定价清单一起隐藏，后端也不再取价与折算；重新打开会立刻抓一次价并**补算关闭期间的请求**（历史日志一个字不改，金额始终是读时算的）。
- **计价口径**：输入按缓存命中 / 未命中两档单价拆分，输出单独单价，乘 token 数再按汇率折算；输出的 token 数取上游 `completion_tokens`（已包含推理 token，不另计）。按条件定价的模型（按输入长度或按 UTC 时段）按每条请求取最紧的那一档。
- **定价来源**：OpenRouter 模型目录的**模型级公布价**。OpenRouter 把它定义为该模型 top provider 的价——对多 provider 模型通常就是最低档（实测 `deepseek-v4.1-flash` 的模型级价只比 34 家 provider 的最低价高 5%）。这组数字只用于「这些 token 放在 OpenRouter 上值多少钱」的参考估算，**不等于 WorkBuddy 官方价，也不能用于结算**。每份价格按内容存成一条「定价策略」，每条请求只记引用了哪条策略，所以之后调价不会改写历史数字；`wb_pricing.py` 另内嵌一份快照作出厂价。
- **刷新**：「设置 → 定价刷新」默认每 5 分钟一次（填 0 关闭）；只有价格真的变了才写策略与时间轴，价格不动时每轮只是一次 HTTP GET。面板可看当前生效策略、上次 / 下次取价时间与失败原因，也能点「立即取价」。
- **新增模型自动取价**：候选清单 = 内置目录 ∪ 网关实时目录，上游新上架的名字 5 分钟内进入取价；两次取价之间就被调用也会按需补价（请求路径不发网络请求）。匹配顺序：面板手填覆盖 → 人工覆盖表 → 同名唯一命中 → 变体后缀继承（剥 `-lkeap` / `-taiji` / `-volc` / `-sg` 等，仍要求唯一命中），继承可在设置里整体关掉。
- **按模型校准**：在 `usage/pricing-markup.json` 写 `{"deepseek-v4.1-flash": 5.36}` 之类的系数，估算金额会乘以它；文件缺失或值非法时按 1.0。它只影响读时估算，不改写历史策略。
- **未定价可见**：`/pricing` 列出未定价模型及分类（别名 / OpenRouter 无对应 / 剥后缀后无唯一基准）与相似候选（仅建议，不自动采用），可在「设置 → 定价刷新」为某条手填 OpenRouter id 并登记（写 `pricing-overrides.json`，不改源码，登记后立即取价）。
- **展示位置**：数据看板「API 等价花费」卡片、各账号用量透视与模型性能表**最后一列**、网关页「估算价格」卡片、最近请求**最后一列**（悬停可看完整定价链：三档原始单价、汇率、匹配证据、策略 id 与补算标记 `*`）。
- **人民币 / 美元**一键切换（记在浏览器本地）；快照覆盖不到的模型显示 `—`，不猜数字。

### 7. 本地网络工具（可选，默认关闭）

Codex App 这类客户端会在 Responses 请求里声明 `web_search` / `web_fetch`，而上游没有对应的执行器——声明送上去只会拿到一句 unsupported call。打开「设置 → 本地网络工具」后，网关把那份声明换成自己的同名 function，拦下模型的调用并在本地执行（搜索在 DuckDuckGo / Bing / Wikipedia / Stack Overflow / Google News 之间自动轮换，抓页面抓模型给出的 URL），再把结果喂回模型，最多代跑 3 轮（`WB_MAX_WEB_ROUNDS` 可调，上限 8）；搜索过程会作为 `web_search_call` 卡片事件与 `url_citation` 引用回到客户端。

- 关闭时（默认）声明原样透传，客户端自己声明的搜索工具照常拿到调用；
- 打开后网关会主动出网抓取模型给出的 URL（字面私网地址与解析进内网的域名都会被挡；
  `WB_WEB_TOOLS_PROXY` 可以给这组工具单独指定出口代理），每轮代跑都会多跑一次上游、多消耗该账号额度；
- 某个搜寻引擎被限流／拦截时会自动轮换到下一家，只有全部不可用才把明确错误交给模型；常规引擎（DuckDuckGo / Bing）接手时对模型无感，Wikipedia / Stack Overflow / Google News 这类非常规来源会在结果里标明来源；
- 只影响声明了这两个工具的客户端，普通 `/v1/chat/completions` 客户端不经过这条路径。

### 8. 智能体一键配置

看板「智能体配置」页可检测本机已装的 AI 客户端，一键把它们的配置指向本网关，改前自动备份、随时字节级还原：

| 客户端 | 写入位置 | 协议 |
| --- | --- | --- |
| Claude Code | `~/.claude/settings.json` | 原生 Anthropic Messages（自动剥 `/v1` 后缀） |
| Codex CLI | `~/.codex/config.toml`、`~/.codex/auth.json` | Responses API |
| OpenCode | `~/.config/opencode/opencode.json` | OpenAI 兼容（`@ai-sdk/openai-compatible`） |
| DSH (DeepSeek Harness) | `~/.dsh/profiles/<profile>/cordis.patch.yml`（无 profile 时回退 `~/.dsh/settings.yaml`）、`~/.dsh/.credentials.yaml` | OpenAI 兼容 |
| Crush (Charm Crush) | `~/.config/crush/crush.json` | OpenAI 兼容 |
| ZCode | `~/.zcode/v2/provider_config.json` | OpenAI 兼容（`openai-chat-completions`；**配置前先退出 ZCode**，运行中它会重写该文件） |

用法：打开看板 →「智能体配置」→ 选 API Key（支持全局默认或绑定特定出口的多 Key）、默认模型与网关地址 → 在检测到的客户端卡片上点「一键配置」；随时可点「一键还原」。

- **模型清单可控**：「网关与账号」页的「网关模型清单」每行模型 ID 前有一个复选框，**默认全部勾选**（从没动过勾选时等价于全量），表头另有「全选 / 全不选 / 反选」三个批量动作；只有勾选的模型才会被一键配置写进 DSH / OpenCode / Crush / ZCode 这类把模型清单抄进自身配置的客户端（Claude Code / Codex 不读这份清单，不受影响）。一个都没勾时，这几家的「一键配置」会直接拒绝并提示先勾选。勾选结果存在 `accounts/settings.json` 的 `agent_models` 里（对所有版本视图生效；字段缺失 = 从未保存 = 全量，空数组 = 明确全不选，两者语义不同），改完需要重新一键配置并重启对应客户端才生效；
- **非侵入**：纯标准库实现的文本级 YAML / TOML / JSON / .env 编辑器，只增量插入或更新 `wb-proxy` 那段，绝不重排或抹掉用户的注释、缩进与其他 provider 配置；
- **可回滚**：写入前自动备份到 `accounts/agent-backups/<客户端ID>/`（每文件保留最新 10 份），首次接入的原件永久保留；多文件客户端（DSH、Codex）两阶段写入，任一文件失败立即回退；
- **还原**：byte-exact 回到首次接入前，网关新建的文件会被清理；若之后手动改过配置，看板会提示「检测到外部修改」，还原仍会覆盖回原件；
- **密钥安全**：API Key 只写进客户端自己的本地配置目录（权限仅限当前系统用户），单文件超过 8MB 拒绝编辑。

**DSH：思考档位、上下文窗口、识图与限流重试写在哪**（`~/.dsh/profiles/<profile>/cordis.patch.yml`）

当前 DSH 只在首次启动时导入一次 `~/.dsh/settings.yaml`，随后改名 `settings.yaml.imported`，之后不再读它；桌面版 / Web 版真正生效的是各自 profile 的 `cordis.patch.yml`。一键配置因此直接写这一层（`~/.dsh/settings.yaml` 只在它本来就有、或机器上还没有 profile 时写入）。给 DSH 写的每个模型条目都带上网关宣告的规格，DSH 会据此生成它自己的选择器：

```yaml
    wb-proxy:
      api: openai-completions
      baseURL: http://127.0.0.1:8788/v1
      compat: {thinkingFormat: openai, supportsReasoningEffort: true}
      retryPolicy:                     # 限流等待：见下「限流重试」
        mode: normal
        maxRetries: 8
        backoff: {initialDelayMs: 2000, maxDelayMs: 30000, jitterRatio: 0.1}
      models:
        - id: deepseek-v4.1-flash
          contextWindow: 1000000     # 客户端认为的上下文容量（压缩 / 截断按它算）
          maxTokens: 128000          # 单次输出上限
          input: [text, image]       # 输入模态：不写 image 就贴不了图
          reasoningEfforts:          # 会话里可选的思考档位：键=档位，值=发给网关的拼写
            low: low
            high: high
            max: max
```

- **识图**：DSH 只认模型条目里的 `input`（`inputModalities`）声明，不读 `supportsVision`，也不读 `/v1/models` 里的能力字段。所以网关宣告 `capabilities.vision` 的模型都会写上 `input: [text, image]`，纯文本模型（如 `glm-5.3-flash`）不写。缺了这一行，贴图会被 DSH 直接拒掉：`Model "<id>" does not support image input.`
- **思考程度在 DSH 里怎么选**：不在「设置 → 模型」页（DSH 有意不把 per-model 的档位放进 provider 级表单），而在**输入框旁的模型菜单**：菜单里有两行 `模型` 与 `推理等级`，点「推理等级」选档位；`/model` 弹窗只列模型、不列档位。选项完全来自上面的 `reasoningEfforts` —— 不声明就没有这一行（`/v1/models` 里的 `reasoning_efforts` DSH 不读，它只读 `id` / `name` / `contextWindow` / `maxTokens` / `input`）。
- **上下文长度怎么改**：`contextWindow` 就是 DSH 用的容量。改客户端侧就编辑这一行（或在 DSH 的模型编辑界面里填），改网关侧就改 `/v1/models` 宣告的 `context_length`；两条链路现在共用 `wb_modelsdev.lookup` 的同一套取值（上游值 > 内建知识表 > 本地缓存 > 1M 默认），不会再出现「网关宣告 1M、一键配置写 300k」这种对不上的情况。
- **上游钉死档位的模型**（目录里只有 `reasoning.effort`、没有 `supportedEfforts`）不写 `reasoningEfforts`：给了选择器也不会改变上游实际跑的档位。
- **限流重试（「对话到一半链接中断」的根因）**：DSH 对失败请求的重试由每个 provider 自己的 `retryPolicy` 决定，默认 normal 模式、最多 5 次、退避 500ms 起按 2 倍翻、封顶 10s——五次加起来不到 16 秒。WorkBuddy 的模型冷却（429）通常要一分钟，DSH 在冷却结束前就把重试次数用光，回合以 `RATE_LIMIT` 失败，客户端看到的就是对话中途断掉。一键配置因此给 `wb-proxy` 单独放宽成最多 8 次、退避 2s 起、封顶 30s（合计约 2 分半），足够熬过一次 60 秒冷却。字段名与取值范围按 `@deepseek-ai/dsh-llm` 的 `RetryPolicySchema`；不写 `retryableCodes`，沿用 DSH 默认（`EMPTY_RESPONSE` / `RATE_LIMIT` / `SERVER` / `TIMEOUT` / `TRANSPORT`，429 落在 `RATE_LIMIT`）。只影响 `wb-proxy`，其它 provider 仍是默认策略；已经接入过的机器重新点一次「一键配置」补上这段（幂等），重启 DSH 生效。
- **注意 profile 覆盖层**：`$DSH_HOME/profiles/<profile>/cordis.patch.yml` 里的 `- id: llm-pi-ai` 条目会**整体替换**该插件的配置（不是逐键合并）。一键配置现在直接更新 patch 层里的这一条：只替换 `wb-proxy` 那段，其他 provider（如 `qwen-token-plan-cn`）、其他条目与注释原样保留，所以不会再出现「写进 `settings.yaml` 被 patch 盖住、不生效」的情况。

**在另一台机器上接入（远程 / 非本机）**：看板的一键配置运行在网关进程里，改的是**网关所在机器**的配置目录——所以它只在网关本机、且用 `127.0.0.1` 打开看板时可用（远程访问、容器 / OpenWrt 部署下按设计隐藏入口）。客户端装在别的电脑（网关跑在服务器 / NAS / 路由器上）时，改用自带的配置器 CLI：它在**客户端所在的机器**上运行，写本机配置、只通过 HTTP 从网关取模型表：

```bash
# 1. 取配置器（网关直接提供该文件；也可从仓库 / 绿色包复制 wb_agents.py）
curl -fsSL http://<网关地址>:8788/agents/cli -o wb_agents.py

# 2. 一条命令接入（需要 Python 3.9+，Key 在「设置」页生成）
python wb_agents.py list                                   # 本机装了哪些客户端
python wb_agents.py apply --client codex --base-url http://<网关地址>:8788/v1 --key sk-xxxx
python wb_agents.py restore --client codex                 # 随时字节级还原
```

- 支持 `claude-code` / `codex` / `opencode` / `dsh` / `crush` / `zcode` 六个客户端，与看板共用同一套写入、备份与还原逻辑（`plan()` 同时驱动 `--dry-run` 与真实写入）；
- 默认带 Key 请求一次 `GET /v1/models`：既为 OpenCode / DSH / Crush / ZCode 填入模型表，也顺带验证 Key（401 = Key 被拒）；网关不可达或想完全离线时加 `--no-models`；
- 状态与备份存在该机器自己的 `~/.wb-agent-config/`（可用 `--state-dir` 覆盖），每台机器一份，`restore` 只对执行过 `apply` 的机器有效；
- `--dry-run` 只打印将要写入的内容、不落盘；Key 也可用环境变量 `WB_PROXY_API_KEY` 传入。

模块内部设计与新增客户端的方法见 **[docs/CONTRIBUTING-智能体配置.md](docs/CONTRIBUTING-智能体配置.md)**。

### 9. 剩余用量估算（数据看板 → 剩余用量估算）

上游按 **24 小时窗口**给「账号 × 模型」配额（用满时 429 / code 6004 会带上重置墙钟），但从不告诉你这个窗口的预算是多少。看板新增的「剩余用量估算」区块把它反推出来：

- **预算 = 撞线账号窗口用量的平均**：账号撞线（收到带重置时刻的 429）的那一刻，它在该模型上「自窗口起点以来的成功用量」就是预算的一个样本；按区域聚合、先按账号平均再跨账号平均。样本在 429 处理的那一刻就记进 `usage/limit-events.jsonl`（重试循环里只有最后一个账号会留下带归属的 429 用量行，光靠日志会漏掉大部分撞线），历史日志里带账号的 429 行作为补充，两者按「账号 + 模型 + 重置时刻」去重。
- **已用从每个账号自己的重置时刻起算**：窗口起点取该账号该模型最近一次重置时刻；重置时刻在未来的组合（正在冷却）剩余记 0 并显示恢复时间。
- **没有撞线记录的组合标「按 24h 估算」**：窗口起点未知时按最近 24 小时用量统计——窗口起点必然落在最近 24h 内，所以这是剩余量的**下界**（偏保守，不会高估）。
- 接口 `GET /usage/remaining`（需面板会话）返回 `budgets`（每个区域 × 模型的预算均值 / 区间 / 样本数）与 `rows`（每个账号 × 模型：已用、预算、剩余、窗口起点、是否冷却、是否估算），区块只读不写，不参与请求路径（唯一的请求路径接触点是撞线时记一行事件）。
- **查询侧不做重复计算**：每个「账号 × 模型」的用量缓冲是**时间戳 + 累计和**两个列表（窗口求和 = 两次二分 + 一次相减，O(log n)，内存约 16 字节/行），载荷再挂一个短 TTL（`WB_REMAINING_TTL`，默认 15 秒）并支持 `ETag` / `If-None-Match`：面板 5 秒一轮的轮询在 TTL 内直接拿上次算好的载荷（连日志尾部都不再扫），带条件请求时回 304。实测（2 万行缓冲）：单次重建 ~0.7ms（旧版逐行重算 ~1.8ms）、轮询一分钟 2.9ms（旧版 21.5ms）、缓冲内存 321KB（旧版 2.3MB）。

### 10. 积分获取历史（数据看板 → 积分获取历史）

「积分扣减历史」看的是花掉的部分，这一块补上进账的部分：上游对每个账号返回一份**积分包清单**（每日活跃奖励的 Bonus Pack、免费套餐、活动包…），每个包带面额、发放时间与到期时间。区块把全部账号的包摊平成一张表、新的在前：

- 列：时间（发放）/ 账号 / 区域 / 名称 / **来源** / 积分 / 剩余 / 到期 / 状态；
- 状态按「过期 > 用完 > 在扣减 > 可用」归类：**生效中**（上游标了 `in_usage`，当前正从它扣减）、**可用**、**已用完**、**已过期**；`no_expiry` 的包显示「不过期」；
- **来源**：上游自带的发放原因原样显示（如「Buddy 加油站签到」「成长计划奖励」「官方活动发放」）；国际版的 Bonus Pack 30/50 没有原因字段，按官方规则推断为「每日活跃奖励」（悬停里注明这是推断）。我们自己的**签到 / 每日活跃**记录也会按时间关联上去（取发放前 2 小时内最近的一次尝试，带成功 / 失败），悬停即可对上「本机动作：每日签到 10-10 00:07 成功」——一次发放到底由哪次动作带来，一眼可见；
- 摘要行给出笔数、合计面额、合计剩余与**快照时刻**。接口 `GET /accounts/credits/grants` 只读内存里的积分快照、**不发上游请求**——数字的新鲜度取决于最近一次「一键刷新积分」（签到与每日活跃打卡也会顺带刷新它），所以要看最新的包先点账号页那个按钮。

### 11. 单请求体体积限制（`WB_MAX_PAYLOAD_BYTES`）

针对携带超长历史、大内联附件或高分辨率图片的请求，网关默认对单次请求体做上限保护（默认 16MB = `16777216` 字节），以防止异常巨大的请求挤占内存。若请求体超过上限，网关在解析前即直接返回 HTTP 413（`payload too large` 并附带 `gateway_hint` 说明）。

如需调整上限（例如提升至 32MB）：
- 设置环境变量 `WB_MAX_PAYLOAD_BYTES=33554432`（单位为字节）；
- 此环境变量仅在进程启动时读取一次，修改后重启 `wb-proxy` 即可生效。

---

### 12. 后台模型重定向（`WB_BACKGROUND_MODEL_NAMES` / `WB_BACKGROUND_MODEL_TARGET`）

桌面客户端（Codex 等）的模型选择器**只管主对话线程**。客户端内置的「模型路由配置」会把后台工作——记忆生成、环境建议、临时内容生成、自动化任务——送到自己硬编码的模型上，UI 里选什么免费模型都改不掉。这些请求带着同一把 Key 打到网关，于是在共享 Key 的部署里悄悄烧付费积分。

网关在唯一的 POST 分发点（选账号之前）把这类模型名改写成免费目标：

- `WB_BACKGROUND_MODEL_NAMES`：要拦截的模型名清单（逗号分隔，大小写不敏感），默认 `gpt-5.6-terra,gpt-5.6-luna,gpt-5.3-codex`；
- `WB_BACKGROUND_MODEL_TARGET`：改写目标，默认 `deepseek-v4.1-flash`。

改写只动 `model` 字段，请求体其余部分原样透传；命中与改写都会记一条 `alias: background model X -> Y` 日志，方便核对。OpenAI 形状的两个路由（`/v1/chat/completions`、`/v1/responses`）与 Anthropic 形状的 `/v1/messages` 都覆盖。不需要时把名单设为空即可关闭。

---

### 13. 自签证书 / TLS 解密代理（WB_CA_BUNDLE）

如果网关跑在公司代理、Clash/Surge 等 HTTPS 解密后面，或 macOS 上 Python 没有读到系统钥匙串里的根证书，上游请求会报 CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain，面板上的 OAuth 登录和账号测试会显示 502。把签发该证书的根证书导出为 PEM，然后设置 WB_CA_BUNDLE=/path/to/ca.pem（优先）或 SSL_CERT_FILE=/path/to/ca.pem，重启 wb-proxy。网关会在系统根证书之外追加这份 PEM，不关闭证书校验；证书错误也不再重试，日志和面板会直接给出这段提示。

---

### 14. 国内版上游地址（`WB_CN_CHAT_UPSTREAM`）

国内版对话侧默认发往 `https://copilot.tencent.com`。网关跑在海外出口后面（代理软件的全局模式、海外服务器）时，到它的 TLS 握手会被复位，日志里是 `SSL: UNEXPECTED_EOF_WHILE_READING` 或 `WinError 10054`，国内版账号的对话、刷新 token、模型目录、签到与成长任务全部失败。`www.workbuddy.cn` 提供同一套接口且海外可达（官方桌面端当前即以它为 endpoint，VSCode 身分本来也走它）。

- 设置 `WB_CN_CHAT_UPSTREAM=https://www.workbuddy.cn`，重启 `wb-proxy`；启动日志的 `realm` 行会显示新的出口，并多一行 `cn upstream: ... (WB_CN_CHAT_UPSTREAM)`；
- 只改「往哪发」：`X-Domain` 仍用账号登录时记下的 domain，计费侧（`www.codebuddy.cn`）不变；
- 不是 `http(s)://` 开头的值会被忽略并回落默认地址，启动日志给出提示；不设则行为与此前完全一致。

---

## 三、账号添加与管理

打开看板 `http://127.0.0.1:8788/`，在「账号」区域操作。若上游对某账号的单个模型返回 429，账号行会显示受限模型与预计恢复时间（浏览器本地时间），该账号仍可用于其他模型；模型冷却只在当前进程内保留，重启清空。

窗口探针管的是「已经撞到 429 之后」；想提前知道某条配额线有没有被打满，可在「设置 → 模型额度定时主动查询」勾选模型：网关每 6 小时为每个「账号 + 模型」发一个最小请求做体检，查到 429 就立刻记该模型的冷却并把重置时刻交给窗口探针，请求通了就说明还没到限额。一旦某个「账号 + 模型」已经有了登记的 429 记录，就停止定时查询（改由窗口探针负责），记录消失后自动恢复轮询；每次查询都会消耗该账号极少量额度，默认不勾选任何模型。

### 方式一：浏览器 OAuth 授权（推荐，免客户端）

1. 点「+ 添加账号 (OAuth)」；
2. 选择要登录的区域（国际版 / 国内版），点弹出的官方授权链接并在浏览器完成登录；
3. 程序自动检测回调，账号自动加入账号池，无需手动复制凭证。

### 方式二：从本机桌面客户端导入

1. 让 **WorkBuddy 桌面客户端保持运行并已登录**（Windows 上网关要从它的进程内存里取解码密钥，这一步不能省）；
2. 看板点「扫描桌面客户端账号」，弹窗列出本机已登录的国际版 / 国内版账号；
3. 若提示凭据已加密，先把解码密钥交给网关，再点账号行的「导入」：
   - **Windows**：点弹窗里的「回收密钥」（只读，实测 1 秒内完成）；
   - **macOS / Linux**：进程内存回收只在 Windows 上实现，弹窗里会直接给出输入框，粘贴密钥即可（见下）。

关于加密凭据：桌面客户端从 2026-09-24 起把 `accessToken` / `refreshToken` 存成 `$wbEncrypted` 信封，网关按客户端 `packages/at-rest-crypto` 的同一套方案（AES-256-GCM + `WB-AAD` 帧头）就地解密，导入的仍是可直接使用的 token；解码密钥编译在客户端原生模块里、磁盘上没有明文。Windows 上可以从**正在运行的**桌面端进程内存里找回（只读 `OpenProcess(PROCESS_VM_READ)` + `ReadProcessMemory`，不向客户端写任何东西）；macOS / Linux 上没有这条路，看板会显示输入框让用户**手动粘贴**一次——两种方式拿到的密钥都只留在网关进程内存、不落盘不写日志，且都要先过 keyblob 自检（protectorKeyId 对得上、能解出 32 字节主密钥）才生效。

手动粘贴接受两种写法（都不用自己换算）：64 位 hex 的保护密钥，或 44 字符 base64 的 `atRestSecretKey`。拿不到密钥时，macOS / Linux 上请直接用方式一走浏览器登录。提示权限不足时，以管理员身份启动网关再试一次。

---

## 四、客户端配置与接入

- **API 接口地址 (Base URL)**：`http://127.0.0.1:8788/v1`（局域网为 `http://<局域网IP>:8788/v1`）
- **API Key**：本机单机模式（未配置 Key 且未开 LAN）可留空或填任意字符；已在看板配置 Key 或 LAN 模式，请用看板「设置」页里绑好出口的 Key；
- **模型名称**：填 `/v1/models` 里列出的任意官方对齐模型 ID（如 `deepseek-v4.1-flash`、`gpt-6-astra`、`glm-5.3`）。

**Codex CLI / Claude Code（Responses API）**

```bash
export OPENAI_BASE_URL="http://127.0.0.1:8788/v1"
export OPENAI_API_KEY="你在看板设置中添加并绑定的API_Key"
```

**Claude Code / Anthropic SDK（实验性 Anthropic Messages 适配，默认禁用）**

> ⚠️ **积分风险：默认拒绝调用。** 网关对外接受 Anthropic Messages，但对内将请求转换为 WorkBuddy 的 `/v2/chat/completions`；`cache_control` 被忽略。已有 [#367](https://github.com/ardeyouxipianyi/workbuddy2api-hub/issues/367) 报告长会话缓存命中率仅约 11%，单次消耗超过 100 积分。该问题的因果机制仍待定位，故暂时默认禁用整个 Anthropic Messages 入口，而不假装完全兼容。禁用时 `/v1/messages`、`/messages` 及对应 `count_tokens` 入口返回 Anthropic 格式的 HTTP 400（`invalid_request_error`），请求不会转发至上游。

只有确认理解风险后，才在**网关服务端**显式设置 `WB_ENABLE_ANTHROPIC_MESSAGES=1` 并重启。Docker Compose 可在 `wb-proxy.environment` 中加入 `- WB_ENABLE_ANTHROPIC_MESSAGES=1`；其他环境则在启动网关前设置此环境变量。客户端配置示例：

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8788"
export ANTHROPIC_API_KEY="你在看板设置中添加并绑定的API_Key"
```

实验性适配包含 `system`、`text`、`image`、`document`、`tool_use`、`tool_result`、`tools` 与 `tool_choice` 等字段转换，并将响应包装为 Anthropic 的流式事件序列或非流式消息。这不是向上游的原生 Anthropic 透传。

已知边界：服务端工具（`web_search` 等）上游不支持，会被丢弃并在 system 里注明；`thinking` / `redacted_thinking` 历史块不回放；`top_k`、`cache_control`、`context_management` 与 `betas` 被忽略；`/v1/messages/count_tokens` 在显式启用后只提供网关的 CJK 感知估算值，**不是**官方分词器的精确值。

---

## 五、看板与接口一览

访问 `http://127.0.0.1:8788/` 使用集成看板：顶部页签默认是「网关与账号 / 数据看板 / 智能体配置 / 设置 / 运行日志」，每个主页面左侧有一条按页面区块现场生成的导航（点击直达、滚动高亮、地址栏带锚点，可收起成窄轨）。

账号多起来之后（国际版十几个），账号列表与「当前禁用」总览会把网关页挤下去。设置页的「账号独立成一个页签」默认关闭，账号区就留在「网关与账号」页（位置与拆分前一致）；打开后它们和签到记录一起成为独立的「账号」页：添加 / 导入 / 导出、启用停用、凭证与积分刷新、代理出口绑定都在那里，顶部页签变成「网关 / 账号 / 数据看板 / 智能体配置 / 设置 / 运行日志」。页内的「签到与活跃记录」读 `GET /activity/history`，可按**区间 / 任务 / 结果 / 账号**筛选（筛选在服务端做），一行一次真实尝试，列出 时间 · 账号 · 区域 · 任务 · 来源 · 结果 · 说明，最新在前；超过一页时用「加载更多」往下翻，上限 1000 行——「这个账号昨天到底签成了没有」不用再去调度器日志里翻。

「数据看板」页顶部可切换统计口径：**今日 / 本周 / 本月 / 全部历史 / 自定义**（本周自周一零点起算、本月自 1 号零点起算，任一侧留空表示不限），切换后 KPI 卡片、账号用量透视表与模型性能表一起切到同一窗口。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | / | Web 用量与任务监控看板 |
| POST | /v1/chat/completions | 标准 Chat Completions 接口 |
| POST | /v1/responses | Responses API 协议接口 |
| POST | /v1/messages | 实验性 Anthropic Messages 适配，默认禁用（HTTP 400）；需显式启用并承担缓存风险 |
| POST | /v1/messages/count_tokens | 实验性 Anthropic 计数估算接口，随 Messages 入口默认禁用 |
| GET | /v1/models | 官方对齐模型列表（含能力与规格宣告） |
| GET | /u/\<ref\>/v1/models | 定址账号的模型清单：\<ref\> 支持完整 uid / uid 前 8 位 / 昵称，过滤该账号禁用模型 |
| POST | /u/\<ref\>/v1/chat/completions | 定址调用：锁定该账号，不参与会话亲和 / 轮换 / 自动换号；账号不可用直接报错 |
| POST | /u/\<ref\>/v1/responses | 定址 Responses API，语义同上 |
| GET | /v1/balance-summary | 当前 API Key 所属区域的启用账号积分汇总；只读内存快照、不发上游请求，不返回账号身份 |
| GET | /pricing | 定价状态：当前生效策略、上次/下次取价时间、未定价清单（分类 + 候选） |
| POST | /pricing/refresh | 立即取一次价（需面板会话） |
| POST | /pricing/mapping | 手填 / 清除「模型 → OpenRouter id」运行期映射，随后自动取价（需面板会话） |
| GET | /agents | 客户端探测概览、支持的模型清单及网关连接地址 |
| GET | /agents/cli | 智能体配置器源码（供远程机器直接 `curl` 取用，免面板鉴权；与仓库内 `wb_agents.py` 逐字节一致） |
| POST | /agents/apply | 一键写入客户端配置并备份原件（需面板会话） |
| POST | /agents/restore | 一键还原客户端至首次配置前的状态（需面板会话） |
| GET | /tasks | 国内版成长任务、连续打卡与猫猫日常状态 |
| POST | /tasks/run | 触发国内成长任务全自动点亮与领奖 |
| POST | /tasks/travel | 触发猫猫日常旅行（派出 / 领奖） |
| GET | /scheduler | 定时调度器运行状态与排程日志 |
| POST | /scheduler/trigger | 手动立即执行后台巡检保活 |
| GET | /activity/history | 账号每日活动历史：签到与每日活跃的每一次真实尝试（`range` / `uid` / `task` / `result` / `limit`，最新在前） |
| GET | /accounts/credits/grants | 积分获取历史：各账号积分包（发放时间 / 面额 / 剩余 / 到期 / 状态），只读内存快照、不发上游请求 |
| GET | /usage/remaining | 剩余用量估算：每个账号 × 模型在当前 24h 窗口的已用 / 预算 / 剩余（预算取撞线账号的平均用量；需面板会话） |

---

## 六、版本更新记录 (Changelog)

<!-- 发版时：把下面的 Unreleased 段落整理成新版本号（## vX.Y.Z），整体移入 docs/CHANGELOG.md 顶部 -->

- **v1.10.0 发布**：集成 CodeArts 原生第三出口（#712/#715）、请求体上限面板动态调节（#709/#714）、Key 账号选择器搜索备注（#708/#717）、今日活动无记录状态优化（#706/#716）、最近请求跨天日期显示（#700/#713）与设置卡片重复修复（#710）。完整变更、验证范围与已知限制见 **[docs/CHANGELOG.md](docs/CHANGELOG.md#v1100)**；下一版的改动继续写在本段。
- **v1.9.2 发布**：数据看板轮询防慢堆叠（#656）、更新卡片操作提示优化（#696）、密码组件环境异常明确报错（#658），详见 [更新记录](docs/CHANGELOG.md#v192)。
- **v1.9.1 发布**：集成 #692 与 #693，详见 [更新记录](docs/CHANGELOG.md#v191)。
- **流式终止**：设置 `WB_STOP_AT_SSE_DONE=1` 后，收到上游 SSE `[DONE]` 立即结束读取；默认关闭，保持原有行为。 / **Stream termination**: set `WB_STOP_AT_SSE_DONE=1` to stop reading at the upstream SSE `[DONE]` marker; disabled by default to retain existing behavior.
- **HTTP 错误正文**：设置 `WB_CACHE_HTTP_ERROR_BODY=1` 后缓存首次读取的错误正文，供后续读取使用；默认关闭。 / **HTTP error body**: set `WB_CACHE_HTTP_ERROR_BODY=1` to retain the first error-body read for later readers; disabled by default.

- **v1.9.0 发布**：汇总 v1.8.0 之后的 10 个社区 PR（#649、#650、#652、#653、#669、#670、#671、#672、#684、#686）。完整变更、验证范围与已知限制见 **[docs/CHANGELOG.md](docs/CHANGELOG.md#v190)**；下一版的改动继续写在本段。
- **v1.8.0 发布**：汇总 v1.7.33 之后的 #640、#642、#643、#645/#647，并补齐 #578、#618、#625 的发布记录。完整变更、验证范围与已知限制见 **[docs/CHANGELOG.md](docs/CHANGELOG.md#v180)**；下一版的改动继续写在本段。
- **修正 CN 专属模型判定，并给 OpenCode 写入真实上下文上限**（#618 / #625）：`minimax-` 曾被当成整个模型家族的 CN 专属前缀，于是国际版也提供的 `minimax-m3.1-flash` 被本地 400 拦下。现在只保留确定的 CN 独占项（`deepseek-v4-pro*` 与显式 `minimax-m3`），同族其他模型按出口实际目录判断。OpenCode 一键配置此前只写空的模型条目，客户端只能回落到自己的 200k 默认值；现在把网关目录中的 `context_window` / `max_output` 写进 `limit.context` / `limit.output`，没有规格的条目仍保持空对象。新增回归覆盖。 / Narrow the China-only model gate and give OpenCode the real context limit (#618 / #625): `minimax-` used to mark the whole family as CN-exclusive, so the globally served `minimax-m3.1-flash` was refused locally with a 400. Only the confirmed China-only entries remain (`deepseek-v4-pro*` and the explicit `minimax-m3`); other members of the family follow the actual catalogue of the selected exit. OpenCode's one-click config used to write empty model entries, so it fell back to its own 200k default; the gateway's catalogue `context_window` / `max_output` now go into `limit.context` / `limit.output`, while entries without specs stay empty objects. Regression coverage added.
- **流式回复在真实静默期发送 SSE 心跳**（#578）：上游思考期间可能长时间一个字节都不发，而客户端会把静默误判为断流；只在每次阻塞读之前检查心跳，恰好无法覆盖它。Chat、Responses 与 Anthropic Messages 三条流式路径现在共用后台 `SSEHeartbeat`：每 15 秒无写入就发送合法的 `: keepalive` 注释帧，与业务帧共用锁，保证不会截断 JSON；响应正常时不会多插帧，流结束后线程立即停止。新增 `tests/_test_stream_keepalive.py`（9 项，含读线程阻塞期间仍收到心跳与并发写入完整性），并复跑流式 id、usage 帧、断流错误帧与重试套件。 / Keep SSE streams alive through real upstream silence (#578): while the model is thinking the upstream may send nothing for longer than the client's idle window, and checking a heartbeat only before the next blocking read cannot cover that gap. Chat, Responses and Anthropic Messages now share a background `SSEHeartbeat`: after 15 idle seconds it writes a valid `: keepalive` comment frame under the same lock as application writes, so JSON frames cannot be split; responsive streams gain no extra frames and the thread stops as soon as the stream ends. Added `tests/_test_stream_keepalive.py` (9 checks, including heartbeat delivery while the reader is blocked and write integrity under contention), and re-ran the stream id, usage-frame, abort-frame and retry suites.

- **修复调度器日志与区域风控闸门读错模块副本**：网关以 `python[w] wb_proxy.py` 启动时主模块是 `__main__`，`wb_scheduler` 里裸写 `import wb_proxy` 会再加载一份副本——副本的 `LOG_STORE` 是 None、`restriction_hold_remaining()` 也是另一份状态。结果是调度器写的每条日志都掉进黑洞（实测 6.4 万行日志库里 `[调度器]` 0 条，同期启动横幅 8 次），区域级连锁暂停的闸门恒为 0、从未生效。现在统一走 `_proxy_module()`，优先取正在运行的那一份。新增 2 项断言。 / Fix the scheduler reading a module copy: launched as `python[w] wb_proxy.py` the main module is `__main__`, so a plain `import wb_proxy` inside `wb_scheduler` loads a second copy whose `LOG_STORE` is None and whose `restriction_hold_remaining()` is separate state — every scheduler log line was dropped (64k log rows, zero `[调度器]` entries, against 8 startup banners) and the realm-level pause gate was always 0, so it never took effect. Both now go through `_proxy_module()`, which prefers the running instance. Two assertions added.
- **修复签到把业务失败记成成功**：`ok: (code == 0 or code == 10001)` 无条件成功，而 10001 在本仓库不是「签到成功」专属码（活动补偿的「每人限领一次，您已领取过」、content rejected 都用它），上游还会在 `code=0` 下回「签到失败，已签到天数不足 7 天」——「请求处理中，请勿重复操作」这类明确没签成的回执因此被记成成功（实测一轮 27 个账号里 4 条，当天真签成 8 个）。现在按文案归 `checked` / `already` / `pending` / `failed` 四类，并且**只有 checked / already 才写 `lastCheckin`**：旧实现拿到 HTTP 200 就先写盘，于是当天 `can_checkin()` 恒为 False，9 点与 21 点两轮都不会重试，这次失败被永久放弃，面板还显示「今日已领」。新增 `tests/_test_checkin_state.py`（7 项）。 / Fix check-in recording business failures as success: `ok: (code == 0 or code == 10001)` treated everything as success, but 10001 is not the check-in success code here — the compensation path's "already claimed" and content rejection use it too, and upstream also returns code 0 with 「签到失败，已签到天数不足 7 天」. Receipts like 「请求处理中，请勿重复操作」 (still processing, do not repeat) were therefore recorded as successes (4 of 27 accounts in one measured round). Receipts are now classified as `checked` / `already` / `pending` / `failed`, and only `checked` / `already` write `lastCheckin`: the old code wrote it as soon as it saw HTTP 200, so `can_checkin()` stayed False for the rest of the day, the 09:00 and 21:00 rounds never retried, and the panel showed "claimed today". Covered by `tests/_test_checkin_state.py` (7 tests).
- **修复「一键签到」不按任务开关选号**：成长任务、猫猫旅行、连登管家与网页活跃都按 `task_active()` 选号（#521），只有签到路由漏了——不传 `uid` 时 `targets` 只按 `realm == "cn"` 过滤，`tasksEnabled=False` 的账号照样被打一遍上游，签到记录也写进只做 API 派号的账号。显式点名单个 `uid` 时仍允许执行，与其它手动入口一致。`tests/_test_task_participation.py` 新增 2 项断言（名单里排除、显式 uid 放行）。 / Fix "check in all" ignoring the task switch: growth tasks, cat travel, streak bonus and the intl daily activity all select accounts by `task_active()` (#521), but the check-in route was missed — without a `uid` its target list only filtered on `realm == "cn"`, so accounts with `tasksEnabled=False` were still hit upstream and got check-in records written into accounts that only do API dispatch. Naming a single `uid` still runs, matching the other manual entry points. Two assertions added to `tests/_test_task_participation.py`.
- **看板下拉框改为自绘（原生 select 的展开层无法美化）**：原生 `<select>` 的弹出列表由浏览器绘制，CSS 完全够不着——深色主题下尤其明显：直角白框 + 系统蓝高亮，和暖色面板割裂。现在页面里所有 `<select>` 在运行时被自动增强：触发器沿用主题变量（圆角、hover / focus 环、旋转箭头），展开层是自绘面板（圆角、阴影、选中行打勾、hover 高亮、过长滚动、贴近视口底部时向上翻），并支持键盘（↑↓ / Enter / Space / Esc / Tab）与 listbox / option / aria-selected / aria-expanded 语义。**原生 select 元素保留**：`value`、`selectedIndex`、`change` 事件、表单提交全部不变，只是视觉被隐藏；JS 未执行时页面照常可用（渐进增强）。动态插入的 select（例如 Key 编辑器按行生成）由 MutationObserver 接管。新增 1 个 JS 套件（19 项断言）。 / Custom-drawn selects: the native `<select>` popup is browser-painted and unreachable from CSS — most visible in the dark theme, where a square white box with system-blue highlighting clashes with the warm panel. Every `<select>` is now enhanced at runtime: the trigger follows theme variables (rounded, hover/focus ring, rotating chevron) and the popup is a self-drawn panel (rounded, shadowed, tick on the selected row, hover highlight, scrolls when long, flips upward near the viewport edge), with keyboard support (arrows / Enter / Space / Esc / Tab) and listbox / option / aria-selected / aria-expanded semantics. The native element is kept: `value`, `selectedIndex`, `change` events and form submission are untouched, only its visuals are hidden; the page still works with JS disabled (progressive enhancement). Selects inserted later (e.g. the per-row key editor) are picked up by a MutationObserver. One new JS suite (19 assertions).
- **网页活跃：还没跑到会话就失败时补退避（设置页可改）**：网页通道一直把「等待预算」当成重试的代价，但那只对「会话没跑完」成立——建会话返回 5xx（例如上游 `14220 failed to create initial session`）或取沙箱失败只用掉几百毫秒，旧实现立刻重打，回退组合开着时几个候选模型会在一秒内全部打光；上游抖动约 100 秒就能让整批账号全灭（#558：8 个账号 × 6 次尝试全落在同一个窗口，队列最后一个反而赶上恢复）。现在这两类轮次之间补一段指数退避，跑过 turn 的轮次维持原样、不重复惩罚；`_daily_chat_web_once` 的结果新增 `phase`（`create` / `session` / `turn`）用来区分停在哪一步，旧调用方与测试桩返回的结果没有该字段时按「已跑到 turn」处理。设置页在「回退模型组合」下面新增「建会话失败退避」秒数（`daily_chat_web_retry_backoff`，0–60，**填 0 = 不退避**，即旧行为），状态行直接列出每一轮实际等待；没配过时用内置默认 `WB_WEB_RETRY_BACKOFF`（默认 5 秒、单次上限 `WB_WEB_RETRY_BACKOFF_MAX` 默认 30 秒，单次上限取 30 与基数的较大者）。默认 5 个候选最坏多花约 65 秒/账号，只在真的失败时才付。新增 15 项单测（含一条用本地 500 服务驱动真实建会话路径、验证两轮之间确实退避的用例）。 / Back off before retrying a web-channel round that never reached the turn, now configurable in the panel: the retry has always treated the wait budget as its cost, which only holds for "conversation still running". A create-conversation 5xx (e.g. the upstream `14220 failed to create initial session`) or a sandbox lookup failure costs a few hundred milliseconds, so the old loop retried immediately and, with model fallback on, burned every candidate inside one second — a ~100 s upstream hiccup then wiped the whole sweep (#558: 8 accounts × 6 attempts all inside the same window, while the last account in the queue happened to hit the recovery). Those rounds now sleep in between; rounds that did reach the turn are unchanged because they already paid with the wait budget. `_daily_chat_web_once` results gain a `phase` field (`create` / `session` / `turn`) so the retry loop can tell, and results without it (older callers, test stubs) keep the previous behaviour. A new "backoff after a create failure" seconds field sits under the fallback combination (`daily_chat_web_retry_backoff`, 0–60, **0 = no backoff**, i.e. the old timing) and the state line spells out the waits each round will use; an unset install keeps the built-in `WB_WEB_RETRY_BACKOFF` (5 s by default, capped per wait by `WB_WEB_RETRY_BACKOFF_MAX`, 30 s, never below the base). Five candidates cost at most ~65 s extra per account, paid only on a real failure. Covered by 15 new tests, one of which drives the real create path against a local server answering 500 and asserts the rounds are actually spaced out.

- **修复面板登录闸门首帧闪烁与 `?pwd=` 失败后卡死**：`#panelGate` 的可见性原先只由 `panelNeedsLogin()` 打开，markup 上是 `display:none`，于是首帧会先把整块看板骨架（静态标记：空表、0 分卡片）画出来、等 `/panel/status` 回来才盖住——无痕窗口 / 首次访问每次都能看到「登录后的界面」闪一下；带 `?pwd=` 自动登录且密码错时更是直接停在隐藏态，用户看到的是「看着已登录、其实进不去」的空看板，连重输密码的框都没有。现在闸门 fail-closed：markup 默认 `display:flex`，只有「本标签页已有面板令牌」时由 `<head>` 里那段同步脚本先隐藏（`html.wb-panel-session` + `!important`），复核失败再由 `panelNeedsLogin()` 摘类显示；登录失败（密码错 / 网络错）也统一回到闸门。新增 1 个 JS 套件（19 项断言）。 / Fixed the panel gate flashing the dashboard before the login screen, and the `?pwd=` dead end: the gate's visibility was only ever turned on by `panelNeedsLogin()`, so the first paint showed the whole dashboard skeleton (static markup: empty tables, zeroed cards) until `/panel/status` came back — every incognito or first visit flashed the "signed in" view, and a wrong `?pwd=` left the page hidden for good: a dashboard that looked signed in but accepted nothing, not even a retyped password. The gate is now fail-closed: `display:flex` in the markup, hidden ahead of first paint by a synchronous `<head>` script only when this tab already holds a panel token (`html.wb-panel-session` + `!important`), and re-shown by `panelNeedsLogin()` when that session turns out to be stale; a failed login (wrong password or a network error) also returns to the gate. One new JS suite (19 assertions).
- **修复面板按钮之后的 keep-alive 连接失步（`{}GET … 501`）**：面板上「立即检查更新」「安装」「立即查询额度」这几个按钮会 POST 一个 `{}` body，而对应路由从不解析它，于是这两个字节留在 socket 里；同一条 keep-alive 连接上的下一个请求（通常就是面板自己 5 秒一轮的 `/usage` 轮询）以 `{}GET /usage?realm=intl` 进场，被当成未知方法回 501 并关掉连接——面板右上角因此闪一句「无法连接反代」，而网关、账号、额度其实都正常。日志判据是 `code 501, message Unsupported method ('{}GET')` 紧跟一条 200 的 POST（本机实测 18 次里 16 次紧跟 `POST /updates/check`，延迟 1–2 秒）。现在把「不解析 body 的面板路由」收进一个元组，在 `do_POST` 分发前统一排空（顺序与 `_error()` 一致：先应对 `Expect: 100-continue`，否则客户端根本不发 body）；`_discard_body()` 排空后把 body 标记为已取走，路由里的错误分支不会重复读同一段字节而卡住。`/console/toggle` 与 `/shutdown` 原有两份逐路由排空随之上移——同一件事已经按路由漏过两回，只留一处。新增 9 项断言（四条路由各自「POST `{}` 之后，同一连接上的下一个请求仍是 200」）。 / Fix the keep-alive desync after panel buttons (`{}GET … 501`): the panel's "check for updates", "install" and "run quota scan" buttons POST a `{}` body the route never parses, so those two bytes stayed in the socket; the next request on the same keep-alive connection — usually the panel's own five-second `/usage` poll — entered as `{}GET /usage?realm=intl`, was answered as an unknown method with a 501, and the connection was closed. The panel then flashes "cannot reach the reverse proxy" while the gateway, accounts and quota are all healthy. The tell in the log is `code 501, message Unsupported method ('{}GET')` one to two seconds after a 200 POST (16 of 18 observed events followed `POST /updates/check`). The body-less panel routes are now one tuple drained in `do_POST` before dispatch (same order as `_error()`: answer `Expect: 100-continue` first, or the client never sends the body), and `_discard_body()` marks the body as taken so a later error branch cannot block on the same bytes. The two per-route drains in `/console/toggle` and `/shutdown` move up with it — this was missed twice when written per route, so it lives in one place now. Nine assertions added (each of the four routes: a POST of `{}` followed by a request on the same connection that still answers 200).
- **账号行显示会员档位胶囊**：档位（`tier`）自 v1.7.27 起已经落盘，但只在「积分与套餐明细」弹窗里露出，列表层看不到哪些号是免费档，运营得逐个点开弹窗。现在「版本」格里、区域徽章下方多一枚胶囊（窄屏卡片视图与区域/状态徽章同排），文字用上游包名原样（国际版 `Free`、国内版 `体验版`，与 workbuddy 官方叫法一致，不自造「免费 / 付费」标签），付费档加 `ok` 高亮；悬停写明 `会员类型: <包名> · 读取于 <时刻>`。`tier` 为空表示还没读过，那是「未知」而不是免费（企业号与被风控限制的号也是空），这种行一个胶囊都不画；包名按 HTML 转义输出。新增 1 个 JS 套件（16 项）。 / The membership tier (`tier`) has been on disk since v1.7.27 but only surfaced inside the credits modal, so the account list never showed which accounts are free — you had to open the modal account by account. Each row now carries a capsule in the `版本` cell, right under the realm badge (and inline with the realm/state badges in the narrow-screen card view), using the upstream package name verbatim (intl `Free`, CN `体验版`, matching workbuddy's own wording rather than inventing a free/paid label), with an `ok` highlight for paid tiers and `会员类型: <package> · 读取于 <when>` on hover. An empty `tier` means "not read yet", not "free" (enterprise and rate-limited accounts stay empty), and those rows render no capsule at all; the package name is HTML-escaped. Covered by one new JS suite (16 checks).
- **Token 保活提前量改为 48 小时（可用 `WB_KEEPALIVE_LEAD_HOURS` 调整）**：保活巡检默认每天 22:00 一次，旧代码写死「剩余不足 2 小时才续」，于是只有到期时刻落在 22:00 之后两小时的账号能提前续上，其余 23/24 的到期时刻都要等过期后的下一轮巡检才发现，账号在此期间所有上游请求都是 401。提前量现在默认 48 小时——大于 24 小时的巡检间隔，每个到期时刻至少被两轮巡检覆盖，单次刷新失败还有补一次的机会；可按小时数覆盖，非法或越界回落默认。拿不到到期时间的账号（opaque token，或回执里没有 `expiresAt`）不再被 `if exp and …` 永久跳过，而是照常续期。日志同时从「即将到期」改为写明剩余或已过期秒数。新增 9 项单测。 / The keepalive lead time is now 48 hours, configurable through `WB_KEEPALIVE_LEAD_HOURS`. There is one keepalive sweep a day (22:00 by default) and the lead was hard-coded to two hours, so only tokens expiring between 22:00 and midnight were renewed early; the other 23 of 24 expiry hours were noticed at the *next* sweep, already lapsed, leaving the account answering 401 upstream for up to a day. A 48-hour lead exceeds the 24-hour sweep interval, so every expiry is covered by at least two sweeps before it lapses and a single failed refresh still has a retry. Accounts without a usable expiry (opaque tokens, no `expiresAt` in the receipt) are no longer skipped forever by `if exp and …`. The log line now reports the remaining or elapsed seconds. Covered by 9 new tests.
- **静态 API Key 账号（企业微信注册的 WorkBuddy / CodeBuddy CLI）**（#572）：企业微信认证后的 WorkBuddy 账号只能在 API 管理页手工建 Key，而 CodeBuddy 的登录页不支持企业微信扫码，这类账号因此进不了池子。新增第二种账号类型：静态 API Key（`ck_` / `pt_` 前缀），走 `Authorization: Bearer` 与 `X-API-Key` 双头鉴权，realm 固定国内版，不参与 OAuth 刷新与保活，也没有签到 / 成长任务 / 连登 / 猫猫旅行（没有个人身份，上游一律拒）。额度走企业口径端点——个人 `get-user-resource` 对这类 Key 返回 200 但 `Accounts` 为空，复用个人逻辑会让余额恒 0、被积分保底当成耗尽号挡掉；且必须带 `/v2` 前缀，不带 `/v2` 的同一路径被 APISIX 直接回 401 的 HTML 页。加账号走「先验后落盘」：拿企业额度端点探一次，成功才写文件（坏号进池会让选号侧反复选中它、每次失败都要冷却换号，表象是「网关时好时坏」）。面板新增「+ API Key」入口与弹窗，账号行标 `API Key` 标签、悬停显示 `ck_<id>.***`（OAuth token 继续不回显），并隐藏「任务开/关」开关。看板 `context_length` 与对话链路都已实测：`/v3/config` 的模型清单受 UA 闸门控制且静默降级（UA 缺 `CLI/<ver>` 段时只下发 37 条、HTTP 仍 200），静态 Key 只发 `Authorization` 时下发 54 条而不是 56 条，所以两个鉴权头都发。新增 23 项 Python 断言与 1 个 JS 套件（21 项）。 / Static API-key accounts for the enterprise-WeCom WorkBuddy/CodeBuddy CLI path (issue #572): these accounts can only mint a key in the API console (the CodeBuddy login page has no WeCom QR flow), so they could not join the pool. A second account type is added - a static API key (`ck_` / `pt_` prefix) authenticating with both `Authorization: Bearer` and `X-API-Key`, pinned to the China realm, outside the OAuth refresh/keepalive path and without check-in, growth, streak or cat-travel programmes (there is no personal identity; the upstream refuses them). Quota goes through the enterprise endpoint, because the personal `get-user-resource` answers 200 with an empty `Accounts` for such a key - reusing the personal path would pin the balance at 0 and let the credit floor park a usable account. The path also needs the `/v2` prefix: the same route without it is answered by APISIX with a bare 401 HTML page. Adding an account probes the enterprise endpoint first and only writes the file on success, since a bad key in the pool gets picked repeatedly and cools down on every failure, which reads as "the gateway is flaky". The dashboard gains a "+ API Key" entry and dialog, an `API Key` badge on the account row with `ck_<id>.***` on hover (OAuth tokens stay unshown), and hides the task toggle for these accounts. Covered by 23 new Python assertions and one new JS suite (21 checks).

- **修复国际版每日活跃「活跃会话已满」后再也打不了卡（10105）**：网页通道每跑一轮都会新建一条会话，但从来不做收尾，而上游对每个账号同时存在的活跃会话（`status=working`）有 10 条硬上限——每跑一次漏一条、每次重试再漏两条，攒满十条之后 `CreateConversation` 直接返回 HTTP 429 / `10105: you have reached the maximum number of active conversations(10)`，该账号当天再也打不了卡，面板上五个账号一起失败。现在每轮结束都会 `POST /console/as/conversations/{id}/stop` 释放名额（成功的一轮保留记录，状态转 `completed`；失败的一轮再 `/delete` 删掉，免得网页端历史堆满「Hi」），收尾失败不影响已经跑完的结果；撞到 10105 时先按创建时间停掉最旧的、名字属于每日活跃提示词池的会话（保留 8 条，用户自己的真实对话不碰）再重建一次，腾不出名额就把错误标成不可重试并提示去网页端收尾，不再拿 3 次重试去撞同一堵墙。新增 12 项单测。 / Fix the intl daily activity dying for good once the upstream active-conversation cap is hit (10105): the web channel opened a new conversation per run but never closed it, while upstream allows only 10 *active* (`working`) conversations per account — one leaked per run and three per retried run, so after ten the create call answers HTTP 429 / `10105: you have reached the maximum number of active conversations(10)` and the account can never check in again (all five accounts failed together in the panel). Every turn now releases its slot with `POST /console/as/conversations/{id}/stop` (a finished turn keeps its record as `completed`; a failed one is also `/delete`d so the web history does not fill up with "Hi"), and a cleanup failure never turns a finished turn into a failure. On 10105 the oldest conversations whose name belongs to the daily-activity prompt pool are stopped (eight are kept, the operator's own conversations are untouched) and the create is retried once; when nothing can be freed the error is final with a pointer to the web console instead of burning three retries on the same wall. Covered by 12 new tests.
- **启动时补一次更新检查**：落盘的只有「上次尝试时间 / 上次成功时间 / 见过的版本号」，`tag` 与发布页只存在内存里，于是进程每次重启后，面板都停在「显示有新版本、安装按钮却要求先手动检查」的状态——而 24 小时窗口锚在上一次尝试上，重启不会让它放行，最长要等一天。现在后台线程起来后的第一次检查走 `startup=True`，绕过 24 小时窗口补问一次（`due()` 仍管稳态的一天一次）；开关仍然优先，每日检查关闭时启动也不发请求。防滥用用的是**预算**而不是最小间隔：一小时内最多补 `STARTUP_CHECKS_PER_WINDOW`（6）次，正常重启永远花不完，崩溃重启循环用光后停手；预算账本只是几个时间戳，随 `update_check` 落盘。新增 11 项断言。 / The stored bookkeeping is only "last attempt / last success / version seen"; the tag and the release page live in memory, so after every restart the panel sat in "an update is available, but the install button wants a manual check first" - and the 24h window, anchored at the previous attempt, never opened on its own. The background thread's first pass now runs with `startup=True`, bypassing the 24h window (which still governs the steady-state once-a-day check); the preference still wins, so a disabled daily check sends no request at boot either. Abuse protection is a **budget**, not a minimum interval: at most `STARTUP_CHECKS_PER_WINDOW` (6) startup passes per hour, which a normal restart never exhausts while a crash-restart loop stops asking once it is spent; the ledger is a handful of timestamps stored alongside `update_check`. Eleven assertions added.
- **修复一键更新在 macOS/Linux 上必然回滚：解包丢失可执行权限位**：`wb_update_portable.safe_extract()` 用裸 `open()` 写暂存文件，zip 里 755 的 `start-wb-proxy*.sh` 落盘变 644，更新走到「重启网关」一步 `Popen()` 报 `Errno 13 (EACCES)`，整次更新失败并触发自动回滚——非 Windows 的便携包部署每次更新都会命中（实测一次：status 终态 `rolled_back`，message 即该 EACCES）。现在按 zip 条目的 unix 权限位（`external_attr` 高 16 位）还原文件模式：无 unix 属性的条目维持默认权限不凭空加执行位，setuid / sticky 位不还原；Windows 不受影响（`chmod` 在 Windows 只映射只读位，本次改动对其是空操作）。新增 1 项断言（755 / 644 / 无属性三类条目）。 / Fixed the one-click update on macOS/Linux always rolling back: `wb_update_portable.safe_extract()` staged files with a bare `open()`, so the zip's 755 launchers landed as 644 and the restart step died with `Errno 13 (EACCES)`, failing the update and triggering the automatic rollback — every non-Windows portable update hit this (observed live: a `rolled_back` status whose message is exactly that EACCES). The extractor now restores each file's unix mode from the zip entry's `external_attr`: entries without unix attrs keep the default mode and never gain an executable bit, setuid / sticky bits are not restored, and Windows is unaffected (its `chmod` only maps the read-only bit, so this is a no-op there). One new assertion covering 755 / 644 / unattributed entries.
- **修复流式回复里出现两个 chunk id，导致客户端停在「生成中」**：真实上游的思考帧带自己的 id（`01a1…`），而网关把正文重写成自己的 `chatcmpl-wb-*` 帧——一次回复因此分成两条交错的流。思考那条永远不带 `finish_reason`，按 id 管理流状态的客户端就会一直等它，界面停在「生成回复中」、思考区不再更新，而网关日志里这一轮其实几秒前就完成了。现在出站前把每一帧的 id 统一成网关自己的 id（思考、正文、结束帧、usage 帧一致）。真机复现：修复前一次回复出现 2 个 id、首条流 `finish=None`，修复后 1 个 id、`finish=stop`。新增 15 项断言。 / **Fix the two chunk ids in one streamed reply that left clients stuck on "generating"**: the real upstream tags its reasoning frames with its own id (`01a1…`) while the gateway rewrites the answer text into frames carrying its own `chatcmpl-wb-*` id, so one reply left as two interleaved streams. The reasoning half never carried a `finish_reason`, and a client that keys stream state by chunk id waited on it forever - the UI sat on "generating", the reasoning pane stopped updating, while the gateway had finished the turn seconds earlier. Every outbound frame now carries the gateway's own id (reasoning, answer, finish and usage frames alike). Reproduced against the live upstream: 2 ids with `finish=None` on the first stream before the fix, 1 id with `finish=stop` after. Covered by 15 new assertions.
- **Codex 远程压缩（remote compaction v2）不再被当作普通输入丢弃**：Codex 在长会话里发来的 `{"type":"compaction_trigger"}` 以前会落到 `responses: WARNING unhandled input item type='compaction_trigger'` 并被丢弃；上游只回一段普通 reasoning，Codex 收不到要求的 `compaction` 输出项（`got 0 compaction output items`），于是保留未压缩的历史继续跑，最终顶到模型窗口报 `11115 prompt is too long`。现在网关自己完成这次总结：识别 `compaction_trigger` 与旧的 `/responses/compact` 路径，追加 handoff 摘要提示词并**收起工具**（避免这一轮被用来调用工具），把模型返回的摘要编码进 `compaction` 输出项的 `encrypted_content`；流式只发 `response.output_item.done`（Codex 只读这一帧，多一帧 `added` 会把空摘要交给它），非流式直接回一个 `output:[{type:"compaction"}]`。Codex 下一轮回放的 `compaction` 项会被还原成一条 system 摘要消息，压缩后的会话因此能继续。空摘要按 502 报错而不是发一个空项。新增 15 项单测。 / Codex remote compaction v2 is no longer dropped as an unknown input: a `{"type":"compaction_trigger"}` item used to hit `responses: WARNING unhandled input item type='compaction_trigger'` and vanish, the upstream answered with plain reasoning, and Codex reported `got 0 compaction output items` - so it kept the uncompacted history and eventually died on `11115 prompt is too long`. The gateway now performs that summarization itself: it recognizes `compaction_trigger` and the legacy `/responses/compact` path, appends the handoff-summary prompt and withholds tools for the turn, and returns the model's summary inside the `compaction` item's `encrypted_content`. The stream emits only `response.output_item.done` (Codex reads that frame; an extra `added` frame hands it an empty summary), and the non-streaming path returns `output:[{type:"compaction"}]`. A replayed `compaction` item is turned back into a system summary message, so a compacted thread continues correctly. An empty summary is a 502 instead of an empty item. Covered by 15 new tests.

- **账号级并发上限（默认 0 = 不限）**：设置页新增「账号级并发上限」，可限制一个上游账号同时接几个请求（0 不限、1 串行、N 最多 N 个）。名额满时换号而不是排队，固定账号最多等 2 秒。它只防同一瞬间多个请求压到同一个账号上（上游按账号+分钟计 token，撞一起会 429 并停放账号）；默认 0 保持旧行为。新增 24 项单测（含 5 项与会话粘滞键的联动用例）。 / **Per-account concurrency cap (default 0 = unlimited)**: a new settings field limits how many requests one upstream account may serve at once (0 unlimited, 1 serial, N up to N). When full the gateway switches accounts instead of queueing; a pinned account waits at most 2 seconds. It only prevents several requests landing on one account at the same instant (the upstream meters tokens per account per minute, and collisions earn a 429 plus a parked account); the default 0 keeps the old behaviour. Covered by 24 new tests (5 of them cover the interaction with the stable affinity key).
- **更稳的会话粘滞键（默认关闭）**：`affinity_route()` 只用前两条消息（system + 首条 user）算会话键，两个开场白相同的会话会共享同一个绑定槽、每轮互换账号，缓存每轮归零。开关打开后哈希头扩到前三条（多算首条 assistant 回复），两个会话从第 2 轮起各走各的账号；关闭时与上游逐字一致，可随时在面板回退。新增 14 项单测。 / **Stable conversation affinity key (off by default)**: `affinity_route()` hashes only the first two messages (system plus the first user turn), so two conversations with the same opening line share one binding slot, swap accounts every turn and lose the cache each time. With the switch on the hash also covers the first assistant reply, and the two conversations stick to their own accounts from turn two. Off keeps byte-identical upstream behaviour and can be reverted from the panel at any time. Covered by 14 new tests.
- **11-128：GUI 描述段的渠道指纹（#570 / #577）**：国内版仍会返回 400 / 11-128。触发面是 system 提示里的一段 GUI 描述——`… the DeepSeek Harness Web GUI whose URL the runtime context names.` 紧跟 `When the user refers to "this page", "this GUI", or "this app" …`；DSH 的默认系统提示就带这段，等于每次请求都命中。逐段二分实测：指纹是这两句的连续文本，改 A 句、改 B 句、或在两句之间插东西都会失配。这里选改动最小的一种——只把 A 句尾的语序换一下（`the runtime context names` → `is named in the runtime context`），**一个词都不删，`DeepSeek Harness` 全名保留，句子意思不变**；既不动品牌词，也不往提示词里塞不可见字符。规则锚定整句，作用域沿用既有口径（`system` / `developer`，`user` / `tool` 不改写），句子之外的同名品牌与连字符路径原样保留。真机国内账号复测：system 与 developer 的 GUI 段由 400 转为 200，user 侧对照不变。新增 8 项断言。 / Another 11-128 fingerprint on the CN realm: the GUI paragraph in the system prompt, specifically the pair `… the DeepSeek Harness Web GUI whose URL the runtime context names.` followed by `When the user refers to "this page", "this GUI", or "this app" …`. DSH ships that paragraph by default, so every request hit it. Bisection shows the fingerprint is the contiguous pair — editing A, editing B, or inserting anything between them all clear it. This takes the smallest edit: reorder A's trailing clause (`the runtime context names` -> `is named in the runtime context`), deleting no word at all, so `DeepSeek Harness` survives and the meaning is unchanged; no invisible characters are injected either. The rule is anchored to the whole sentence, keeps the existing scope (`system` / `developer`; `user` / `tool` untouched), and leaves the same brand elsewhere plus hyphenated paths alone. Verified against a live CN account: the GUI paragraph in system and developer moves from 400 to 200, with the user-side control unchanged. Eight assertions added.
- **跨出口回退：国际版限流 → 国内版（默认关闭）**：两个出口是两套独立上游（`www.workbuddy.ai` / `copilot.tencent.com`）、两套配额、两套积分，而 `AccountPool.pick()` 一直严格按 realm 过滤——国际版整池被限流时，即使国内版还有空号，请求也只能等一个 429。设置页新增「跨出口回退（国际版 → 国内版）」：国际版对某模型限流、24h 额度窗口用满、整池都在冷却，或该出口池子空掉时，把这次请求改用同名的国内版出口重放一次。只对两个出口都提供的模型生效（`gpt-*` / `gemini-*` 等国际版独占模型不回退）；方向单向，国内版永远不会回退到国际版；出口被显式指定的请求（Key 绑定了 realm，或客户端带了 `X-Realm` / `?realm=`）不受影响；403 11140 这类非配额失败不重放。两边都限流时按国内版给出的重置时钟回报退避。新增 16 项单测。 / Cross-exit fallback from the Global to the China exit (off by default): the exits are separate upstreams with separate quota and credit ledgers, while `AccountPool.pick()` only ever dispatched same-realm accounts, so a fully throttled Global pool had to wait out a 429 even with idle China accounts. A new settings switch replays the request once on the same model at the China exit when the Global exit throttles a model, spends its 24h window, has every account cooling, or has an empty pool. Only models both exits serve are eligible (Global-only `gpt-*` / `gemini-*` never fall back); the direction is one-way (China never falls back to Global); requests naming an exit explicitly (a key bound to a realm, `X-Realm`, `?realm=`) are left alone; non-quota failures such as 403 11140 are not replayed. When both exits throttle, the reported backoff follows the China exit's reset clock. Covered by 16 new tests.
- **修复账号表排序把冷却 / 达限胶囊拆离自己的账号行**：`accountRow()` 把 `tr.cool-row` 作为独立兄弟行追加，而 `applyTableSort()` 按「一行一个排序单元」摊平重排，胶囊行因此在点过任意可排序表头后跟错账号（按「版本」排时尤其明显：账号行与胶囊行各自扎堆沉底）。现在排序单元改为「数据行 + 紧跟它的 `tr.cool-row` 续行」，成组排序后整块 append；表头列类型推断仍只看数据行，顺序未变时依旧不碰 DOM。 / The cooldown / daily-limit pills (`tr.cool-row`) were appended as independent sibling rows, so `applyTableSort()` — which treated every row as its own sort unit — detached them from their account after any sortable header was clicked. The sort unit is now "data row + its following `tr.cool-row` continuations", moved as a block after sorting; column kind inference still looks at data rows only, and the DOM is untouched when the order is unchanged.
- **保留历史版本备份 + 面板版本回退**：更新成功后把被替换的程序文件留一份在 `updates/backup-<这次更新去的版本>/`，份数由新增设置「保留历史版本备份」控制（0–3，**默认 1**；`0` = 与今天一致，不留备份、回退不可用）。设置页新增「版本回退」卡片：列出本机备份的版本、时间、文件数与完整性，点「回退到此版本」按本地文件恢复，不下载旧安装包，回退会重启网关但不动账号与用量数据。`request.json` 新增 `action`（`install` / `rollback`），便携版、外部执行者与 systemd 服务三个执行者都认；**旧执行者遇到 `action=rollback` 会当「重装该 tag」处理**（多一次下载，仍能回到旧版本）。回退前复核备份完整性（缺文件 / 版本不符 / `backup.json` 损坏都禁用并写出原因），回退后用 `Server: wb-proxy/<版本>` 校验确实在跑旧版本，探针失败自动恢复到回退前的版本。新增 26 项 Python 断言与 1 个 JS 套件（29 项）。 / Retained backups plus a panel rollback: after a successful update the replaced program files stay in `updates/backup-<the version that update went to>/`, and the new "keep previous versions" setting caps how many folders are kept (0-3, default 1; `0` keeps today's behaviour and disables rollback). A new "version rollback" card lists the local backups with version, time, file count and integrity; "roll back to this version" restores them from disk without downloading an old package, restarting the gateway but leaving accounts and usage data alone. `request.json` gains `action` (`install` / `rollback`), understood by all three executors (portable, external, systemd); an executor that predates this treats `action=rollback` as "reinstall that tag", which costs a download but still lands on the old version. Integrity is re-checked before a restore (missing files, mismatched version or an unreadable `backup.json` disable the row with a reason), the health probe compares `Server: wb-proxy/<version>`, and a rollback that never serves the old version is undone automatically. Covered by 26 new Python assertions and one new JS suite (29 checks).

- **11-128：身份句渠道指纹的动词搭配补全**：v1.7.18 的正则只认 `agent powered by` / `assistant built on` 两种搭配，而同一判据下的 `built by`（如 `You are <Product> Agent, built by <Vendor>.`）、`created by` / `developed by` / `made by` / `trained by`，以及省略名词的 `You are powered by …` 都会漏检——上游只要补其中一条规则，这些句子就会重新命中 400 / 11-128。现按同一判据补全动词表：名词可省略、冠词后允许 0~3 个修饰词（专名 + 可选逗号）；`You are free to use tools built by …` 这类分词修饰宾语的句子保持不改写。新增 5 项断言（built by 变体、其余动词、省略名词、反误伤、角色作用域）。 / Broaden the identity-sentence channel fingerprint: v1.7.18 only matched `agent powered by` / `assistant built on`, so `built by` (`You are <Product> Agent, built by <Vendor>.`), `created by` / `developed by` / `made by` / `trained by`, and the noun-less `You are powered by …` slipped through — one rule added upstream would put them back on 400 / 11-128. The same rule now covers those verb forms, with an optional noun and up to three modifiers (proper names plus an optional comma), while participles modifying an object (`You are free to use tools built by …`) stay untouched. Five assertions added.
- **网页活跃重试等待改为线性递增**：第 N 轮等待从「首轮 × 2^(N-1)」改为「首轮 × N」，默认 3 次从 120 / 240 / 480 秒变为 120 / 240 / 360 秒；最坏占用从指数级降到线性级。设置页说明同步更新。新增 4 项单测。 / The web-channel retry wait now grows linearly: round N waits base × N instead of base × 2^(N-1), so the default three tries are 120 / 240 / 360 seconds instead of 120 / 240 / 480. The worst case is linear rather than exponential. Covered by 4 new tests.
- **网页活跃支持并发数设置（默认 1）**：设置页新增「并发数」，按这个上限并发跑当天的国际版每日活跃；默认 1 保持逐账号串行。并发启动带轻微错峰，调度日志加锁，多 worker 同时写不会丢行。新增 7 项单测。 / The daily activity can now run several intl accounts at once: a new concurrency setting (default 1, the serial behaviour) caps the batch, workers start with a small stagger, and the scheduler log is lock-protected for concurrent writers. Covered by 7 new tests.
- **当日活跃成功后不再重复网页活跃**：`daily_chat()` 与 `daily_chat_web()` 在当天已有成功记录（`lastDailyChat` 是今天）时直接返回 `skipped`，不再发桌面腿或新建网页会话。覆盖多个定时点、手动补跑与并发调度的重复触发。新增 5 项单测。 / Once the day's activity has succeeded (`lastDailyChat` is today), `daily_chat()` and `daily_chat_web()` return a skipped result instead of sending another desktop turn or opening another web conversation. Covers repeated schedule slots, manual retries and overlapping sweeps. Covered by 5 new tests.
- **出站身份版本号可在面板查看与修改（需面板密码）**：新增「出站身份版本号」区块，列出 WorkBuddy 国际/国内、桌面端内置 CLI、VSCode 扩展/宿主、CodeBuddy CLI、SDK 与 Node 的当前值与默认值。修改必须输入面板密码；只有默认值有抓包/解包核对过的头族与请求体配套，改版本号只改字符串，可能触发上游校验或封号。国内版桌面端按 Go 版 2026-10-18 实跑从 5.5.6 对齐到 5.8.3（CLI 2.161.0）。新增 9 项单测。 / The panel now lists and (password-gated) edits the outbound identity versions. Only the verified defaults have matching header families and request bodies; an override changes the string only and can trip upstream checks or bans. The CN desktop version is aligned to 5.8.3 / CLI 2.161.0 from the Go gateway's 2026-10-18 live run. Covered by 9 new tests.

- **修复「数据用途声明」文案反了**（#528）：`X-Private-Data` 是「数据私有」声明，`true` = 不参与模型优化、`false` = 允许参与；下拉选项与英文词条此前正好相反，用户按文案选择会得到相反的效果。已交换两处文案，并加 4 项断言。

- **修复模型清单「消费倍率」列截断**（#527）：带「Pro Only」胶囊的行在 1366/1440/1512 宽度下被 `nowrap` 裁掉第二枚胶囊；该列改用 `rate-cell` 类允许换行，不再依赖会被 i18n 翻译的 `data-label` 选择器。
- **思考等级档位映射（默认开启）**：客户端传入的 `reasoning_effort` 不在模型目录声明的 `supportedEfforts` 里时，改写为该模型的默认档位（`defaultEffort` → 固定档位 → 支持列表第一项），避免上游直接拒绝整个请求；固定档位模型只接受它自己的档位，目录明确声明 `canDisableThinking: false` 时 `off`/`none` 也改为默认档位。设置页可关闭映射，关闭后请求原样透传。新增 12 项单测。 / Map an unsupported `reasoning_effort` to the model's default level (default on): when the catalog declares the levels a model accepts and the request asks for another one, the value is rewritten to `defaultEffort`, then the pinned effort, then the first supported level; a model pinned to one level only accepts that level, and `off`/`none` is rewritten only when the catalog explicitly says thinking cannot be disabled. The settings page can turn the mapping off for a byte-faithful passthrough. Covered by 12 new tests.
- **网页活跃模型回退（默认关闭）**：国际版每日活跃的网页通道失败后，按配置的模型组合逐个回退；组合按目录倍率自动排序（免费 → 低积分 → 高积分），候选至少各试一次，`最多尝试次数`更大时再跑第二轮。设置页可开关并编辑组合，留空时使用首选模型 + 该区域免费模型。新增 13 项单测。 / Fall back through a model combination when the intl daily activity's web turn fails (off by default): the combination is ordered by catalogue multiplier (free, then low credits, then expensive), every candidate gets at least one try, and a larger attempt count starts a second pass. The settings page toggles it and edits the combination; empty means the preferred model plus the realm's free models. Covered by 13 new tests.
- **API Key 可按账号限制（允许账号 / 允许模型）**：每条 Key 新增 `accounts` 白名单，请求时调度器只从名单内账号取号；粘性绑定不会绕过限制，`/u/<ref>` 定址到名单外账号直接 403。名单留空保持不限制，旧 Key 行为不变。面板 Key 编辑卡片可填账号 UID（逗号分隔），与已有的模型限制并排展示。新增 11 项单测。 / Each API key can now name the accounts it may use, next to the existing model patterns: the scheduler picks only from that list, a conversation binding cannot route around it, and a pinned account outside the list is refused with 403. An empty list keeps the key unrestricted, so existing keys behave exactly as before. The panel's key editor takes account UIDs, comma separated. Covered by 11 new tests.
- **网页活跃随机提示词（默认关闭）**：开启后每次网页活跃从内置 300 条短句 + 自定义提示词里随机抽一条；关闭时仍旧只发 `Hi`。自定义提示词在设置页按行填写（最多 200 条、单条 200 字节），内置池只含不需要联网、不容易引出长答案的小问题。新增 10 项单测。 / With the setting on, each web activity draws one line from the built-in 300-line pool plus the operator's custom prompts; off keeps the original `Hi`. Custom lines are entered one per line in the settings page (200 lines max, 200 bytes each), and the built-in pool only holds short questions that need no network and do not invite long answers. Covered by 10 new tests.
- **只列出可用模型（默认关闭）**：开启后 `/v1/models` 只返回当前账号池里至少有一个账号能服务的模型。国际版 `modelTiers` 里标为 PRO ONLY 的模型，只有在池里存在付费账号（`credits.is_paid_user`）时才列出；调度侧 `ready()` 同时拒绝把这类模型派给免费账号，不再等上游回 11158。档位表随 `/v3/config` 一起更新，缺失时行为不变。新增 16 项单测。 / With the filter on, `/v1/models` only lists models at least one account in the pool can serve. An intl model the catalogue marks PRO-only is listed only while a paid account (`credits.is_paid_user`) exists, and `ready()` refuses to hand such a model to a free account instead of discovering 11158 after the request. The tier map follows `/v3/config`; when it is absent nothing changes. Covered by 16 new tests.
- **全局提示词支持面板在线编辑（默认透传）**：设置页新增「全局提示词」区块，可选「追加到 system」或「替换 system」，正文直接写进 `settings.json` 的 `prompt.text`，改完即时生效、不必往容器里挂文件。在线正文优先于原有的 `prompt.file`；留空仍用内置默认提示词。默认「透传」不改写任何客户端内容。新增 10 项单测。 / The settings page now edits the global prompt inline: append it to the system block or replace the client's system messages, stored as `prompt.text` and applied immediately without mounting a file into the container. The inline body wins over the existing `prompt.file`; empty falls back to the built-in default. Passthrough stays the default and rewrites nothing. Covered by 10 new tests.
- **网页活跃定时可单独设置（分钟级）**：`schedule.daily_chat` 支持 1–24 个 `HH:MM` 时段，设置页的「每日活跃定时」输入框可直接编辑，改完 30 秒内生效、无需重启。留空时保持旧行为（跟随签到整点 09:00/21:00）；填写后每日活跃只在这些时刻触发。新增 14 项单测。 / The web activity can now have its own minute-level slots: `schedule.daily_chat` takes 1–24 `HH:MM` times, editable from the settings page and picked up within 30 seconds without a restart. Empty keeps the old behaviour (the checkin hours); with slots set, the activity only runs at those times. Covered by 14 new tests.

- **修复全量刷新积分在默认间隔下崩溃**：`_credits_sweep()` 在账号间使用 `random.uniform(0.8, 1.2)` 做拟人间隔，但 `wb_proxy.py` 漏了 `import random`，多账号点「一键刷新积分」会以 `NameError` 中断，单账号与 `WB_CREDITS_SWEEP_GAP_MS=0` 不受影响。补上导入，并新增 1 项回归测试，实际走默认 800~1200ms 抖动路径。 / Fix the full credit sweep crashing at the default pacing: `_credits_sweep()` used `random.uniform(0.8, 1.2)` between accounts, but `wb_proxy.py` never imported `random`, so "refresh all credits" failed with `NameError`; single-account refreshes and `WB_CREDITS_SWEEP_GAP_MS=0` were unaffected. Add the import and one regression test that exercises the default 800-1200 ms jitter path.
- **成长任务补充「无畏契约拼豆专家」**：`WorkBuddy_Valorant` 登记为专家任务，事件固定使用真实专家 id `ex_xQHHD1u5Jkua`，不再走通用专家池；一键做任务时自动推进并领奖。实测一个国内版账号上报后进度 0→1，领奖 +100 积分、+5 能量。
- **11-128：身份句 + 品牌的组合指纹改为中性句**（#533 / #537 / #539）：上游对 `You are an AI agent powered by DeepSeek Harness.`、`You are a coding agent powered by the deepseek-v4.1-flash model.`、`You are an AI assistant built on DeepSeek.` 这类「自我介绍句式 + 品牌」做大小写不敏感的连续子串匹配，命中即 400 / code 11-128；单独品牌词、改词或去掉句式都放行。现在把 system / developer 里的这类整句改写为 `You are a helpful AI assistant.`，不依赖具体品牌；user / assistant / tool 里的同名文本保持原样（用户数据）。补了大小写、两句变体、`built on` 变体、内嵌、幂等、作用域测试。 / The upstream channel check rejects the combination of a client self-introduction and a brand (`You are an AI agent powered by DeepSeek Harness.`, `You are a coding agent powered by the deepseek-v4.1-flash model.`, `You are an AI assistant built on DeepSeek.`) with 400 / code 11-128. The sanitizer now replaces that whole sentence in system / developer messages with `You are a helpful AI assistant.`, independent of the brand; the same text in user / assistant / tool messages is left alone.

- **模型清单标出「Pro Only」改由目录 tier 表驱动**（#505）：上游 `/v3/config` 的 `modelTiers[]` 里 `requiredUserType: standard` 的模型，`/v1/models` 每条带 `entitlement`（`source: "catalog"`、`required`、`at` = 该 realm 目录的刷新时刻），面板在倍率旁显示「Pro Only」胶囊（与上游徽章同名），hover 给出声明来源、刷新时刻与「仅标记不可用，未标记不代表可用」。数据源就是调度器拦人用的那份 tier map，所以标注与实际拦截不可能互相矛盾——被门控的模型在本地就被拒，请求根本不发往上游。原先「撞到 11158 才写标、成功即撤标」的运行时通道因此一直空着（v1.7.19 起免费账号已经打不到上游），本次连同 `wb_entitlements.py` 与 `accounts/model_entitlements.json` 一起删除，不再有第二套事实来源；标签仍然单向，只标不可用，没有标注 ≠ 可用。新增 3 项 Python 断言与 6 项 JS 断言。 / The "Pro Only" label on the model list is now driven by the catalogue tier map rather than a runtime 11158: `/v1/models` marks every model whose `modelTiers[]` entry says `requiredUserType: standard` with `entitlement` (`source: "catalog"`, `required`, and `at` = when that realm's catalogue was last refreshed), and the panel shows a "Pro Only" pill (the upstream badge wording) next to the multiplier with a tooltip naming the source, the refresh time and "only blocked models are labelled; an unlabelled model is not a promise". The data is the same tier map the scheduler uses to refuse the request, so the label and the actual gate cannot disagree - a gated model is refused locally and never reaches the upstream. That is also why the old runtime channel (record on 11158, clear on a later success) had gone quiet: since v1.7.19 a free account no longer reaches the upstream with a gated model. It is removed together with `wb_entitlements.py` and `accounts/model_entitlements.json`, so there is no second source of truth. The label stays one-way: it never claims availability. Three Python assertions and six JS assertions added.

- **OpenRouter 估价可按模型校准**（#514）：`usage/pricing-markup.json` 支持 `{模型: 系数}`，估算金额乘以该系数，便于把 OR 的 top-provider 参考价校准到官方价；文件缺失、损坏或非正数按 1.0，策略历史不改写。新增 5 项测试。

- **成长任务支持手动领取**（#507）：任务区新增「自动领取」复选框（默认开）。取消后「一键做任务」只推进进度，任务停在「待领奖」，在任务表里按账号逐个点「领取」；新增 `POST /tasks/claim`。这样可以把积分分散到不同时间领取，避免同一分钟集中过期。

- **面板改密结果以服务端为准**（#523）：`/panel/password` 现在返回 `using_default_password`，设置页不再按输入框内容猜「已自定义」；改密成功会在日志里写一行 `panel : password changed`，便于确认改的是不是当前实例。改密的存储/校验补了 5 项单测（默认密码、覆盖、盐变化、改回 admin）。

- **流式断流：首字节前自动重试一次，出口退避按「读完」记账**（#522）：`open_upstream` 拿到响应头就返回，`IncompleteRead` 发生在 `_chat_stream_response` 读 body 时，回不到重试循环；chat 路径还补一个 `[DONE]` 当作正常结束。现在 `emitted=False`（客户端一个字节都没收到）时在同一账号、同一出口上重开一次，递归复用已经发出的响应头；`iter_sse_lines` 对 Content-Length 提前 EOF 补抛 `IncompleteRead`。出口维度的成功记账从「拿到响应头」挪到「流读完」：读流断掉记 transient，重试成功不抵消这次信号，干净的流才清零。`emitted=True` 的截断仍按原样收尾（可选 error 帧留待后续）。

- **修复把日限额改回 0 不生效**：`apply_daily_guards()` 此前只对「至少一个作用域 > 0」的护栏推送新值，而账号对象上的 `daily_token_limit` / `daily_credit_limit` / `model_daily_token_limit` 只存在于内存（`Account.__init__` 归零、不落盘）。于是「关掉限额」恰好成了唯一推不出去的那次编辑：号上还留着上一次的非零值，面板显示旧限额、账号一直 `dailyLimitBlocked`，直到进程重启才恢复。现在**设置保存路径**（`refresh=True`）会把三条护栏都推一次（值为 0 也推），把旧值覆盖回落；请求热路径仍维持「没配限额就完全不碰池子」的原有契约，不为此多付开销。计数仍只在至少一条护栏开着时才折叠，`stats is None` 时池侧保留上次计数，所以「没配限额 -> 计数未知 -> 永不 park」以及关闭限额的行为都与从前一致。新增 `tests/_test_daily_token_limit.py` 的 `GuardClearTests`（2 例）——走 `apply_daily_guards()` 而非直接调 pool，复现并钉住这条路径。 / Fix turning a daily limit back to 0 having no effect: `apply_daily_guards()` only published a guard when at least one scope was above 0, while the per-account limit fields live in memory only (set to 0 in `Account.__init__`, never persisted). "Turn the limit off" was therefore the one edit that never reached the pool - the accounts kept the last non-zero value and stayed parked until the process restarted. The settings-save path (`refresh=True`) now publishes all three guards even when every value is 0, overwriting the stale ones, while the request hot path keeps its "no limit configured -> do not touch the pool" contract and pays nothing extra. Counters are still folded only when at least one guard is on (`stats is None` keeps the last known counts), leaving the "no limit -> unknown count -> never park" rule untouched. `GuardClearTests` (2 cases) in `tests/_test_daily_token_limit.py` calls `apply_daily_guards()` itself (not the pool directly) to pin it.
- **一键部署区分部署模式**（#512）：新增 `WB_DEPLOY_MODE=local|lan|public`（默认 `lan`，保持原行为）。`local`/`public` 只把宿主 8788 绑到 `127.0.0.1`，公网机需要再套反向代理；容器内 `HOST` 仍是 `0.0.0.0`。已有部署不自动改写 compose，只提示手动调整。

- **写清 OpenRouter 估价的口径**（#514）：OpenRouter 的模型级价是它 top provider 的价，多 provider 模型通常就是最低档——实测 `deepseek-v4.1-flash` 的模型级价只比 34 家最低价高 5%。README、设置页说明与定价策略备注统一改为「参考价，不等于 WorkBuddy 官方价或结算价」；按 credit 计价与 provider 区间价仍待产品决策。

- **Codex 一键配置不再依赖环境变量**（#513）：Codex 0.163 在启动时解析 `env_key`，从图形界面启动时没有继承 `OPENAI_API_KEY` 就直接报 `Missing environment variable`，写进 `auth.json` 也不再兜底。现在 provider 表直接写 `experimental_bearer_token`，并顺手移除旧的 `env_key` / `env_key_instructions`；重复点一次「一键配置」就会把旧配置迁过来，`auth.json` 仍照写以兼容旧版本。

- **积分包到期显示统一口径**（#474）：个人体验版 / 免费包的 CycleEndTime 只是每月计费边界，真正作废时间在 DeductionEndTime（实测还在 8 年后）。后端已把这类包标为 no_expiry，但前端表格仍用 CycleEndTime 算倒计时，于是出现「表里剩 12 天、最近到期卡片却不认」的矛盾。现在表格与卡片都按 no_expiry 显示「长期有效」；套餐包列表默认选「有剩余」，已用完 / 已过期的包需要时点「全部」或「已用完/过期」查看。

- **身份请求头的 X-Private-Data 可选**（#516）：设置页新增「数据用途声明」，可选 auto / true / false（默认 auto）。auto 保持原三套身份（WB / VSCode / CLI）的默认取值；显式 true/false 会对三套身份都覆盖，包括 VSCode 中国区本来不发该头的分支。保存在 accounts/settings.json 的 identity.private_data，也可 POST /settings/save 传 {"identity":{"private_data":"false"}}，非法值返回 400。

- **/v1/responses 不再把「只有 reasoning」当 completed**（#508）：上游偶发把整轮预算花在 reasoning 上、正文为空，之前仍发 response.completed，客户端以为回合正常结束。现在流式发 response.failed，非流式返回 502，usage 行记为 failed；新增 WB_DEFAULT_REASONING_EFFORT（high 默认 / catalog / off），客户端未指定 effort 时可以只按目录默认或完全不注入。

- **模型级 5xx 不再熔断整池**（#499）：`11134`（模型提供方暂不可用）、`11102`、`11150`、`11158`、`11129` 等错误会先解析上游 body 的 `code`；命中时只停手并把上游 500 原样返回，不再给账号记 fails / 熔断。普通 500 仍按账号故障处理；设置 `WB_MODEL_SCOPED_ERRORS_PENALIZE=1` 可恢复旧行为。

- **修复 Token 时序图吞吐量计算**（#509）：此前用 total_tokens / 桶宽，长上下文会话每轮都会重复计入 prompt，单小时可显示三万以上 tok/s。现在图表、峰值和悬停都改用 completion_tokens，标签改为「输出 tok/s」；Token 总量仍显示 total_tokens，请求数视图不变。

- **修复后台排程时段被忽略**（#515）：此前只要命中任意一个配置小时（例如默认的 01:00 夜猫），巡检就会把签到、活跃上报、猫猫旅行、连登管家和 Token 保活一起跑一遍，`schedule.checkin` 设成 10:00/20:00 仍会在 01:00 签到。现在每个任务只在自己的配置小时执行；面板「立即执行」仍会强制跑完整轮。
- 修复上游瞬时故障重试：精确识别 Windows `10053/10054/10060`（`10061` 连接被拒仍按原规则记账冷却 60 秒），避免把连接重置误报为本机断网；账号池轮转不再消耗实际请求次数，短暂超时、连接重置或 DNS/路由故障恢复后可在原有次数上限内继续请求。证书校验、固定账号及失败归账规则保持原有约束。 / Fix transient upstream retries: classify Windows transport errors by exact OS code and preserve real request attempts across account-pool rotations, allowing recovery within the existing retry budget without changing TLS verification, pinned-account or failure-accounting rules.
- **流式对话的 usage 按 OpenAI 契约放到末尾空 choices 帧**：`stream_options.include_usage` 要求 usage 出现在 finish 帧之后、`[DONE]` 之前的 `choices: []` 尾帧，而上游把它挂在仍然带 choices 的 chunk 上，`finish_reason` 随后单独到达。原样透传时，按 chunk 契约校验的客户端与按流记账的下游网关会把这段 usage 当成有歧义的中间帧丢掉——实测某网关该渠道 247/310 次流式请求没有落 usage 行，prompt / completion 与缓存命中数一起消失。现在代理把上游 usage 从原帧里摘掉，在 finish 帧之后用 `choices: []` 尾帧一次性发出（缓存别名照旧规范化），`usage.jsonl` 的统计口径不变。新增 `tests/_test_chat_stream_usage_frame.py`（16 项，帧顺序、唯一 usage 帧、cached_tokens 与 usage 行）。
- **日志通道不再能打死请求**：`log()` 原先裸写 `sys.stderr`，而 `send_response()` 会先触发访问日志——启动用的终端被回收、stderr 的读端消失后，每条需要记日志的请求都会在**响应头发出之前**抛异常断开，只剩 `/health`、`/realm`、`/panel/status` 这几个静默白名单端点照常 200（看起来"服务健在"，实际 `/v1/*` 全部不可用）。现在写失败一律吞掉，日志坏了最多少一行。
- **静默启动会告知"已有实例在运行"**：pythonw 的 stdout/stderr 指向空设备，"已有一个反代在 8788 端口运行，无需重复启动"这句话用户永远看不到，双击只表现为"启动失败"。启动脚本改为先在本窗口用 `wb_proxy.py --probe-only` 跑同一段探测（退出码 0=已有实例／端口被别的服务占用，3=端口空闲可启动），已有实例时把原因打印出来并停住等按键；`start-wb-proxy.bat` 与 `start-wb-proxy-lan.bat` 都已接入。
- **网关模型清单可勾选**：「网关模型清单」每行模型 ID 前新增复选框（**默认全部勾选**，表头带「全选 / 全不选 / 反选」三个批量动作），只有勾选的模型才会被一键配置写进客户端的模型清单；选择持久化在 `accounts/settings.json` 的 `agent_models`，并新增 `GET/POST /agents/model-selection` 接口（§8）。
- **面板与日志出口统一脱敏账号手机号**（#415 跟进）：账号昵称常被设成手机号，此前面板 JSON、日志看板、stderr、`/logs/export` 与账号导出文档一律明文。现在 66 处面板出口走新增的 `_json_panel` / `_json_cached_panel` 封装（LLM 的 `/v1` 生成路由保持 `_json`，模型输出一字不动，有源码级用例钉住）；日志在 level/tag 推断之后才掩码，分类不受掩码影响；账号导出两条路（`?download=1` 与内联）共用一次脱敏。匹配只认整值 11 位手机号与文本内三段分组形态（容忍空格/点/中点等常见分隔符），数字边界保护 26 位订单号、10 位时间戳不被误切；掩码幂等、copy-on-write，不污染共享 TTL 缓存。新增 `tests/_test_nickname_privacy.py`（34 例）。
- **全量刷积分加并发与节奏护栏**：「一键刷新积分」（POST `/accounts/credits`）与 GET `/accounts/credits` 在不带 uid/realm 时对整池逐个发上游计费请求，此前没有单飞也没有账号间隔——真机上出现过 10 秒内 9 次全量刷（约 207 次上游调用），触发条件是某账号始终拿不到快照、前端每 15 秒重试一次。现在：多账号目标先抢 CAS 单飞，抢不到直接回 `200 + busy=true` 与当前快照（不排队、不算错）；账号之间按 800~1200ms 随机间隔（`WB_CREDITS_SWEEP_GAP_MS` 可改，0 为关闭）；带 uid 的单账号刷新不受影响。新增 `tests/_test_credits_sweep_guard.py`（17 例）。

- **修复 DSH 一键配置后仍不能识图**（#385 的后续修正）：DSH 的 pi-ai 适配器只认模型条目里的 `input`（`inputModalities`），不读 `supportsVision`，也不读 `/v1/models` 里的能力字段。此前一键配置只写 `supportsVision: true`，贴图会被 DSH 拒掉：`Model "<id>" does not support image input.`。现在多模态模型写 `input: [text, image]`，纯文本模型（如 `glm-5.3-flash`）不写。
- **DSH 一键配置改写 profile 补丁层**：当前 DSH 只在首次启动导入一次 `~/.dsh/settings.yaml`，之后真正生效的是 `~/.dsh/profiles/*/cordis.patch.yml`。一键配置现在直接更新这一层（`settings.yaml` 只在它本来就有、或机器上还没有 profile 时写入），且只替换 `wb-proxy` 那段，patch 里的其他 provider、条目与注释原样保留。
  DSH's pi-ai adapter reads only a model's `input` modalities, not `supportsVision` and not `/v1/models` capability fields, so the previous one-click config could not enable image input. Multimodal models now get `input: [text, image]`, and the config is written into the profile patch layer (`~/.dsh/profiles/*/cordis.patch.yml`) that DSH actually loads, leaving other providers, entries and comments untouched.
- **DSH 一键配置放宽限流重试，修掉「对话到一半链接中断」**：WorkBuddy 的 429 冷却通常约一分钟，而 DSH 的默认 `retryPolicy` 是 `normal` / 最多 5 次 / 退避 500ms 起翻倍、封顶 10s——加起来不到 16 秒就放弃，冷却还没结束回合就以 `RATE_LIMIT` 失败。一键配置现在给 `wb-proxy` 单独写 `retryPolicy: {mode: normal, maxRetries: 8, backoff: {initialDelayMs: 2000, maxDelayMs: 30000, jitterRatio: 0.1}}`（合计约 2 分半），只影响 `wb-proxy`，其它 provider 仍用默认策略；字段与取值按 `@deepseek-ai/dsh-llm` 的 `RetryPolicySchema`。已经接入过的机器重新点一次「一键配置」即可补上这段（幂等），重启 DSH 生效。
  DSH's default per-provider retry policy (normal, at most 5 retries, 500ms→10s backoff, under 16 seconds in total) gives up before WorkBuddy's ~60s rate-limit cooldown ends, so the turn dies with `RATE_LIMIT` — which the user sees as the conversation breaking mid-flight. The one-click config now writes a wider `retryPolicy` for `wb-proxy` only (8 retries, 2s initial delay, 30s cap, ~2.5 minutes in total); other providers keep the default policy. Field names and bounds follow `@deepseek-ai/dsh-llm`'s `RetryPolicySchema`. Re-running the one-click config on an already-integrated machine adds it idempotently, and a DSH restart applies it.

已发布版本的完整记录（v1.4.5 ~ v1.10.0，含每版的 PR 归属）见 **[docs/CHANGELOG.md](docs/CHANGELOG.md)**。

## 七、致谢与引用声明 (Credits & References)

协议兼容、风控规避与任务链路设计过程中，参考并吸纳了以下开源项目的经验与逆向成果：

- **[Sliverkiss/workbuddy2api](https://github.com/Sliverkiss/workbuddy2api)**：成长任务全链路逆向、设备指纹稳定派生（`derive_id`）、整点排程调度（`Scheduler`）、指纹脱敏与 `reasoning_content` 回填；
- **[CangShui/workbuddy-cliproxy-fix](https://github.com/CangShui/workbuddy-cliproxy-fix)**：早期客户端代理修复与接口差异参考；
- **[lovingfish/workbuddy-cliproxy](https://github.com/lovingfish/workbuddy-cliproxy)** 与 **[mmqz/cpa-multi-plugins](https://github.com/mmqz/cpa-multi-plugins)**：网关通信与多插件管理原型参考；
- **[ardeyouxipianyi/workbuddy2api](https://github.com/ardeyouxipianyi/workbuddy2api)**：国内版分发包逆向分析与出站 User-Agent 规范参考；
- **[linguo2625469/workbuddy2api-panel](https://github.com/linguo2625469/workbuddy2api-panel)**：国内版连登管家（补签保连登、7d/14d/28d 档位兑换、抽奖闭环）、对话活跃上报的事件形状与「必须带 `userId`、每号每天一次」的风控口径、企业版门控（企业号无个人成长体系），以及新手礼包 / 活动补偿接口；
- **[aodianjun/workbuddy2api-hub](https://github.com/aodianjun/workbuddy2api-hub)**：OpenWrt 打包配方（`.ipk` / `.apk` 构建脚本、包 Makefile、init.d 与 uci 配置、面板缓存预热器），审计后并入 `wrt/`。

PR 贡献者（v1.4.5 之前的改动未进上方更新记录，这里一并列出）：

- **[@ddddd-ren](https://github.com/ddddd-ren)**：用量日志倒序检索与看板防堆叠（PR #14）、原子写入与并发竞争修复（PR #13）、账号池 JSON 导出导入（PR #5）；
- **[@wylftw0314-glitch](https://github.com/wylftw0314-glitch)**：Responses API custom 工具协议双向转译（PR #12）；
- **[@shuishuipingan](https://github.com/shuishuipingan)**：成长任务领取竞态与专家/团队事件 id 去重、猫猫旅行派出修复、夜猫子任务接入调度器、启动端口误判（PR #21）、按模型冷却限流（PR #22）、任务接取强化与轮询加速（PR #27）、网络抖动重试与 403 直通（PR #28）、HTTP 连接同步（PR #30）；
- **[@ayeaaaa](https://github.com/ayeaaaa)**：按账号绑定出口代理槽（PR #26）、DeepSeek `reasoning_content` 回填（PR #36）、看板移动端布局（PR #37）；
- **[@t-789](https://github.com/t-789)**：macOS 启动脚本与防火墙助手（PR #31）；
- **[@Cekxri](https://github.com/Cekxri)**：Codex App namespace 工具支持（PR #33）；
- **[@wiggins-kong](https://github.com/wiggins-kong)**：API Key 行 id 唯一化（PR #40）、Docker 镜像缺少运行时模块（PR #41）；
- **[@Pro-XK](https://github.com/Pro-XK)**：看板积分消耗与账号昵称（PR #45）；
- **[@teddyli18000](https://github.com/teddyli18000)**：单模型限流可视化（PR #50）、`/health` 鉴权状态修正（PR #52）；
- **[@LuFering](https://github.com/LuFering)**：Docker 部署下的 Linux 桌面凭据挂载说明（PR #55）；
- **[@zhangzm0](https://github.com/zhangzm0)**：`tool_choice="none"` 保留工具声明（PR #57）。

---

## 八、免责声明 (Disclaimer)

1. 本项目为非官方自托管网关，仅供技术研究、逆向协议学习与个人合法授权账号在私有环境测试使用。
2. 本项目不提供任何账号及额度。请严格遵守官方服务条款，禁止用于任何商业转售、恶意并发或违规滥用。
