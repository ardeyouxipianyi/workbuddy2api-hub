# WorkBuddy2API-Hub — 国际版、国内版多账号网关中枢

<p align="center">
  <a href="https://github.com/ardeyouxipianyi/workbuddy2api-hub/releases"><img src="https://img.shields.io/badge/Release-v1.6.19-2496ED?style=flat-square" alt="Version 1.6.19"></a>
  <img src="https://img.shields.io/badge/Python-3.9+-blue.svg?style=flat-square" alt="Python">
  <img src="https://img.shields.io/badge/API-OpenAI_Compatible-412991?style=flat-square" alt="OpenAI API">
  <img src="https://img.shields.io/badge/Dual_Realm-Intl_&_CN-0DBD8B?style=flat-square" alt="Dual Realm">
  <img src="https://img.shields.io/badge/License-MIT-green.svg?style=flat-square" alt="License">
  <img src="https://img.shields.io/badge/Vibe_Coding-100%25-ff69b4?style=flat-square" alt="Vibe Coding">
</p>

把腾讯 **[www.workbuddy.ai](https://www.workbuddy.ai)**（国际版）与 **[codebuddy.cn](https://www.codebuddy.cn)**（国内版）的原生服务封装成标准 OpenAI 兼容接口（Chat Completions / Responses）与原生 Anthropic Messages 接口，并补齐多账号调度与运维能力：

- **开箱即用**：绿色包自带精简 Python，双击脚本即启；
- **双区域独立路由**：国际版 / 国内版各自配置与调度，看板一键切换，状态落盘；
- **三协议**：Chat Completions、Responses API（Codex）、原生 Anthropic Messages（Claude Code / Anthropic SDK）；
- **模型目录对齐官方桌面端**：剔除补全通道与专线变体，能力与规格按桌面端宣告，上游新模型无需发版即进 `/v1/models`；
- **设备指纹隔离 (`derive_id`)**：以账号 UID 稳定派生机器码与会话标识，同一账号不漂移、不同账号互不关联；
- **账号接入**：看板点链接走浏览器 OAuth；Windows 上还能直接从运行中的桌面客户端导入（加密 token 就地解密）；
- **每日自动化**：国内版签到 / 成长与积分任务 / 猫猫旅行；国际版每日活跃打卡（真跑完网页会话）；后台调度器按 09:00 & 21:00、22:00、01:00 排程；
- **限额护栏**：保留积分 · 每日 Token · 每日积分 · 按模型 Token 四条账号级护栏，单账号独立计数，本地 0 点解封（默认全部关闭）；
- **OpenRouter 价估算**：把 token 折算成等价花费（人民币 / 美元可切），逐条请求可悬停查看定价来源；
- **多 Key 与出口绑定**：每个 Key 可固定走国际版或国内版、可限定模型，用量按 Key 归属；
- **Web 看板**：指标卡片、模型性能与用量大表、请求流水、账号与任务管理一屏可查。

> ⚡ **Vibe Coding 产物**：本项目为 100% Vibe Coding 协同产物——人类开发者提出架构与业务意图，AI 助手端到端完成逆向分析、链路调度、WAF 指纹脱敏与界面编写。

---

## 一、快速启动

### 1. 本机运行

**Windows** 双击 **`start-wb-proxy.bat`**，保持窗口运行；**macOS** 双击 **`start-wb-proxy.command`**（首次被 Gatekeeper 拦截时右键 →「打开」确认一次），或在终端执行 `./start-wb-proxy.sh [端口]`（默认 8788）。

启动后：**API 地址** `http://127.0.0.1:8788/v1`，**Web 看板** `http://127.0.0.1:8788/`。首次启动还没有账号，打开看板点「+ 添加账号 (OAuth)」完成授权即自动入库。

> zip 解压后若提示权限不足，先执行一次：`chmod +x start-wb-proxy.sh start-wb-proxy.command start-wb-proxy-lan.sh start-wb-proxy-lan.command allow-firewall.command`

### 2. 面板访问密码

看板需要**面板访问密码**（默认 `admin`），它与 API Key 相互独立：密码只用于打开看板，可在「设置」页修改（或启动时 `--panel-password` 指定），以 PBKDF2-SHA256 摘要存于 `accounts/settings.json`（不存明文），登录态只留在浏览器会话里。

本机 / 受信局域网自用可带 `?pwd=面板密码` 免手输直接进面板（如 `http://127.0.0.1:8788/?pwd=admin`）；公网暴露时不要用——密码会留在浏览器历史与反向代理日志里。注意它和局域网共享的 `?key=` 不同：`?key=` 只把 API Key 存给 `/v1` 用，不会自动登录面板。**首次登录后请立即改掉默认密码。**

### 3. 局域网共享

- **Windows** 双击 `start-wb-proxy-lan.bat`；**macOS** 双击 `start-wb-proxy-lan.command`，或 `./start-wb-proxy-lan.sh [端口] [Key]`；
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

更新：面板里点「拉取最新镜像并重启」，或 `docker compose pull && docker compose up -d`。

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

### 6. 测试

```bash
python tests/run_all.py            # 全部套件
python tests/run_all.py realm      # 只跑名字里含 realm 的
```

- 113 个套件：84 个 Python + 29 个 JS；JS 需要 PATH 上有 `node`，缺失时会跳过并提示。
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
- **定价来源**：OpenRouter 模型目录的**模型级公布价**（对应它默认路由的那家 provider）。每份价格按内容存成一条「定价策略」，每条请求只记引用了哪条策略，所以之后调价不会改写历史数字；`wb_pricing.py` 另内嵌一份快照作出厂价。
- **刷新**：「设置 → 定价刷新」默认每 5 分钟一次（填 0 关闭）；只有价格真的变了才写策略与时间轴，价格不动时每轮只是一次 HTTP GET。面板可看当前生效策略、上次 / 下次取价时间与失败原因，也能点「立即取价」。
- **新增模型自动取价**：候选清单 = 内置目录 ∪ 网关实时目录，上游新上架的名字 5 分钟内进入取价；两次取价之间就被调用也会按需补价（请求路径不发网络请求）。匹配顺序：面板手填覆盖 → 人工覆盖表 → 同名唯一命中 → 变体后缀继承（剥 `-lkeap` / `-taiji` / `-volc` / `-sg` 等，仍要求唯一命中），继承可在设置里整体关掉。
- **未定价可见**：`/pricing` 列出未定价模型及分类（别名 / OpenRouter 无对应 / 剥后缀后无唯一基准）与相似候选（仅建议，不自动采用），可在「设置 → 定价刷新」为某条手填 OpenRouter id 并登记（写 `pricing-overrides.json`，不改源码，登记后立即取价）。
- **展示位置**：数据看板「API 等价花费」卡片、各账号用量透视与模型性能表**最后一列**、网关页「估算价格」卡片、最近请求**最后一列**（悬停可看完整定价链：三档原始单价、汇率、匹配证据、策略 id 与补算标记 `*`）。
- **人民币 / 美元**一键切换（记在浏览器本地）；快照覆盖不到的模型显示 `—`，不猜数字。

### 7. 本地网络工具（可选，默认关闭）

Codex App 这类客户端会在 Responses 请求里声明 `web_search` / `web_fetch`，而上游没有对应的执行器——声明送上去只会拿到一句 unsupported call。打开「设置 → 本地网络工具」后，网关把那份声明换成自己的同名 function，拦下模型的调用并在本地执行（搜索走 DuckDuckGo HTML 版，抓页面抓模型给出的 URL），再把结果喂回模型，最多代跑 3 轮（`WB_MAX_WEB_ROUNDS` 可调，上限 8）；搜索过程会作为 `web_search_call` 卡片事件与 `url_citation` 引用回到客户端。

- 关闭时（默认）声明原样透传，客户端自己声明的搜索工具照常拿到调用；
- 打开后网关会主动出网抓取模型给出的 URL（只挡字面私网地址），且每轮代跑都会多跑一次上游、多消耗该账号额度；国内网络下 DuckDuckGo 可能连不上，那时模型拿到的是错误文本；
- 只影响声明了这两个工具的客户端，普通 `/v1/chat/completions` 客户端不经过这条路径。

### 8. 智能体一键配置

看板「智能体配置」页可检测本机已装的 AI 客户端，一键把它们的配置指向本网关，改前自动备份、随时字节级还原：

| 客户端 | 写入位置 | 协议 |
| --- | --- | --- |
| Claude Code | `~/.claude/settings.json` | 原生 Anthropic Messages（自动剥 `/v1` 后缀） |
| Codex CLI | `~/.codex/config.toml`、`~/.codex/auth.json` | Responses API |
| OpenCode | `~/.config/opencode/opencode.json` | OpenAI 兼容（`@ai-sdk/openai-compatible`） |
| DSH (DeepSeek Harness) | `~/.dsh/settings.yaml`、`~/.dsh/.credentials.yaml` | OpenAI 兼容 |
| Crush (Charm Crush) | `~/.config/crush/crush.json` | OpenAI 兼容 |

用法：打开看板 →「智能体配置」→ 选 API Key（支持全局默认或绑定特定出口的多 Key）、默认模型与网关地址 → 在检测到的客户端卡片上点「一键配置」；随时可点「一键还原」。

- **非侵入**：纯标准库实现的文本级 YAML / TOML / JSON / .env 编辑器，只增量插入或更新 `wb-proxy` 那段，绝不重排或抹掉用户的注释、缩进与其他 provider 配置；
- **可回滚**：写入前自动备份到 `accounts/agent-backups/<客户端ID>/`（每文件保留最新 10 份），首次接入的原件永久保留；多文件客户端（DSH、Codex）两阶段写入，任一文件失败立即回退；
- **还原**：byte-exact 回到首次接入前，网关新建的文件会被清理；若之后手动改过配置，看板会提示「检测到外部修改」，还原仍会覆盖回原件；
- **密钥安全**：API Key 只写进客户端自己的本地配置目录（权限仅限当前系统用户），单文件超过 8MB 拒绝编辑。

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

---

## 三、账号添加与管理

打开看板 `http://127.0.0.1:8788/`，在「账号」区域操作。若上游对某账号的单个模型返回 429，账号行会显示受限模型与预计恢复时间（浏览器本地时间），该账号仍可用于其他模型；模型冷却只在当前进程内保留，重启清空。

### 方式一：浏览器 OAuth 授权（推荐，免客户端）

1. 点「+ 添加账号 (OAuth)」；
2. 选择要登录的区域（国际版 / 国内版），点弹出的官方授权链接并在浏览器完成登录；
3. 程序自动检测回调，账号自动加入账号池，无需手动复制凭证。

### 方式二：从本机桌面客户端导入（仅 Windows）

1. 让 **WorkBuddy 桌面客户端保持运行并已登录**（网关要从它的进程内存里取解码密钥，这一步不能省）；
2. 看板点「扫描桌面客户端账号」，弹窗列出本机已登录的国际版 / 国内版账号；
3. 若提示凭据已加密，先点弹窗里的「回收密钥」（只读，实测 1 秒内完成），再点账号行的「导入」。

关于加密凭据：桌面客户端从 2026-09-24 起把 `accessToken` / `refreshToken` 存成 `$wbEncrypted` 信封，网关按客户端 `packages/at-rest-crypto` 的同一套方案（AES-256-GCM + `WB-AAD` 帧头）就地解密，导入的仍是可直接使用的 token；解码密钥编译在客户端原生模块里、磁盘上没有明文，只能从**正在运行的**桌面端进程内存里找回（只读 `OpenProcess(PROCESS_VM_READ)` + `ReadProcessMemory`，不向客户端写任何东西），密钥只留在网关进程内存、不落盘不写日志。提示权限不足时，以管理员身份启动网关再试一次。Docker / Linux / macOS 下请用方式一。

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

**Claude Code / Anthropic SDK（原生 Anthropic Messages API）**

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8788"
export ANTHROPIC_API_KEY="你在看板设置中添加并绑定的API_Key"
```

网关原生实现 `/v1/messages`（流式与非流式）：`system`（字符串或文本块数组）、`text` / `image` / `document` / `tool_use` / `tool_result` 内容块双向转换，`tools` + `tool_choice`、`stop_sequences`、`metadata.user_id`、`thinking` / `output_config.effort` 全部映射到上游；流式输出是原生事件序列（`message_start` → `content_block_start` / `content_block_delta` → `content_block_stop` → `message_delta` → `message_stop`）；鉴权接受 `x-api-key` 或 `Authorization: Bearer`，错误一律用 Anthropic 的错误信封。

边界：服务端工具（`web_search` 等）上游不支持，会被丢弃并在 system 里注明，不会伪造调用；`thinking` / `redacted_thinking` 块不回放（上游不提供可验证签名）；`top_k`、`cache_control`、`context_management` 与 `betas` 忽略；`/v1/messages/count_tokens` 返回的是网关的 CJK 感知估算值（与用量统计同一套估算器），**不是**官方分词器的精确值。

---

## 五、看板与接口一览

访问 `http://127.0.0.1:8788/` 使用集成看板：顶部页签为「网关与账号 / 数据看板 / 智能体配置 / 设置 / 运行日志」，每个主页面左侧有一条按页面区块现场生成的导航（点击直达、滚动高亮、地址栏带锚点，可收起成窄轨）。

「数据看板」页顶部可切换统计口径：**今日 / 本周 / 本月 / 全部历史 / 自定义**（本周自周一零点起算、本月自 1 号零点起算，任一侧留空表示不限），切换后 KPI 卡片、账号用量透视表与模型性能表一起切到同一窗口。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | / | Web 用量与任务监控看板 |
| POST | /v1/chat/completions | 标准 Chat Completions 接口 |
| POST | /v1/responses | Responses API 协议接口 |
| POST | /v1/messages | 原生 Anthropic Messages 协议接口（流式 / 非流式，`x-api-key` 或 `Authorization` 鉴权） |
| POST | /v1/messages/count_tokens | Anthropic 计数接口（CJK 感知估算值，非官方分词器） |
| GET | /v1/models | 官方对齐模型列表（含能力与规格宣告） |
| GET | /pricing | 定价状态：当前生效策略、上次/下次取价时间、未定价清单（分类 + 候选） |
| POST | /pricing/refresh | 立即取一次价（需面板会话） |
| POST | /pricing/mapping | 手填 / 清除「模型 → OpenRouter id」运行期映射，随后自动取价（需面板会话） |
| GET | /agents | 客户端探测概览、支持的模型清单及网关连接地址 |
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

### Unreleased

- **国内版自动连续打卡（对话活跃上报点亮连登）**：国内版每日签到只能领积分但无法推进官方成长中心的「连登天数」（官方只认真实会话行为上报）。现将国内版对话活跃上报接入调度器：每日定时（09:00/21:00）为国内版账号上报规范会话事件（复刻客户端 `chat_request_send`，严格携带 `userId` 并保持每号每天 1 次防风控口径），点亮成长中心连续打卡天数与热力墙；上报后只读查询并回显连登天数，同时持久化记录 `lastActivityReport` 避免重复调用；新增测试套件 `tests/_test_streak_report.py`（10 项）。

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

已发布版本的完整记录（v1.4.5 ~ v1.6.19，含每版的 PR 归属）见 **[docs/CHANGELOG.md](docs/CHANGELOG.md)**。
## 七、致谢与引用声明 (Credits & References)

协议兼容、风控规避与任务链路设计过程中，参考并吸纳了以下开源项目的经验与逆向成果：

- **[Sliverkiss/workbuddy2api](https://github.com/Sliverkiss/workbuddy2api)**：成长任务全链路逆向、设备指纹稳定派生（`derive_id`）、整点排程调度（`Scheduler`）、指纹脱敏与 `reasoning_content` 回填；
- **[CangShui/workbuddy-cliproxy-fix](https://github.com/CangShui/workbuddy-cliproxy-fix)**：早期客户端代理修复与接口差异参考；
- **[lovingfish/workbuddy-cliproxy](https://github.com/lovingfish/workbuddy-cliproxy)** 与 **[mmqz/cpa-multi-plugins](https://github.com/mmqz/cpa-multi-plugins)**：网关通信与多插件管理原型参考；
- **[ardeyouxipianyi/workbuddy2api](https://github.com/ardeyouxipianyi/workbuddy2api)**：国内版分发包逆向分析与出站 User-Agent 规范参考。

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
