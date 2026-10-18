# WorkBuddy2API-Hub — 国际版、国内版多账号网关中枢

<p align="center">
  <a href="https://github.com/ardeyouxipianyi/workbuddy2api-hub/releases"><img src="https://img.shields.io/badge/Release-v1.7.13-2496ED?style=flat-square" alt="Version 1.7.13"></a>
  <img src="https://img.shields.io/badge/Python-3.9+-blue.svg?style=flat-square" alt="Python">
  <img src="https://img.shields.io/badge/API-OpenAI_Compatible-412991?style=flat-square" alt="OpenAI API">
  <img src="https://img.shields.io/badge/Dual_Realm-Intl_&_CN-0DBD8B?style=flat-square" alt="Dual Realm">
  <img src="https://img.shields.io/badge/License-MIT-green.svg?style=flat-square" alt="License">
  <img src="https://img.shields.io/badge/Vibe_Coding-100%25-ff69b4?style=flat-square" alt="Vibe Coding">
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

**Windows** 双击 **`start-wb-proxy.bat`**，保持窗口运行；**macOS** 双击 **`start-wb-proxy.command`**（首次被 Gatekeeper 拦截时右键 →「打开」确认一次），或在终端执行 `./start-wb-proxy.sh [端口]`（默认 8788）。

启动后：**API 地址** `http://127.0.0.1:8788/v1`，**Web 看板** `http://127.0.0.1:8788/`。首次启动还没有账号，打开看板点「+ 添加账号 (OAuth)」完成授权即自动入库。

> 绿色包自带的精简 Python 运行时只覆盖 Windows；macOS / Linux 直接使用系统 Python，需要 **3.9+**（启动脚本找不到内置运行时会明确打印所用的解释器与版本）。Docker 部署自带基础镜像，不受影响。

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

- 170 个套件：127 个 Python + 43 个 JS；JS 需要 PATH 上有 `node`，缺失时会跳过并提示。
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
| DSH (DeepSeek Harness) | `~/.dsh/settings.yaml`、`~/.dsh/.credentials.yaml` | OpenAI 兼容 |
| Crush (Charm Crush) | `~/.config/crush/crush.json` | OpenAI 兼容 |
| ZCode | `~/.zcode/v2/provider_config.json` | OpenAI 兼容（`openai-chat-completions`；**配置前先退出 ZCode**，运行中它会重写该文件） |

用法：打开看板 →「智能体配置」→ 选 API Key（支持全局默认或绑定特定出口的多 Key）、默认模型与网关地址 → 在检测到的客户端卡片上点「一键配置」；随时可点「一键还原」。

- **非侵入**：纯标准库实现的文本级 YAML / TOML / JSON / .env 编辑器，只增量插入或更新 `wb-proxy` 那段，绝不重排或抹掉用户的注释、缩进与其他 provider 配置；
- **可回滚**：写入前自动备份到 `accounts/agent-backups/<客户端ID>/`（每文件保留最新 10 份），首次接入的原件永久保留；多文件客户端（DSH、Codex）两阶段写入，任一文件失败立即回退；
- **还原**：byte-exact 回到首次接入前，网关新建的文件会被清理；若之后手动改过配置，看板会提示「检测到外部修改」，还原仍会覆盖回原件；
- **密钥安全**：API Key 只写进客户端自己的本地配置目录（权限仅限当前系统用户），单文件超过 8MB 拒绝编辑。

**DSH：思考档位与上下文窗口写在哪**（`~/.dsh/settings.yaml`）

一键配置给 DSH 写的每个模型条目都带上网关宣告的规格，DSH 会据此生成它自己的选择器：

```yaml
    wb-proxy:
      api: openai-completions
      baseURL: http://127.0.0.1:8788/v1
      compat: {thinkingFormat: openai, supportsReasoningEffort: true}
      models:
        - id: deepseek-v4.1-flash
          contextWindow: 1000000     # 客户端认为的上下文容量（压缩 / 截断按它算）
          maxTokens: 128000          # 单次输出上限
          reasoningEfforts:          # 会话里可选的思考档位：键=档位，值=发给网关的拼写
            low: low
            high: high
            max: max
```

- **思考程度在 DSH 里怎么选**：不在「设置 → 模型」页（DSH 有意不把 per-model 的档位放进 provider 级表单），而在**输入框旁的模型菜单**：菜单里有两行 `模型` 与 `推理等级`，点「推理等级」选档位；`/model` 弹窗只列模型、不列档位。选项完全来自上面的 `reasoningEfforts` —— 不声明就没有这一行（`/v1/models` 里的 `reasoning_efforts` DSH 不读，它只读 `id` / `name` / `contextWindow` / `maxTokens`）。
- **上下文长度怎么改**：`contextWindow` 就是 DSH 用的容量。改客户端侧就编辑这一行（或在 DSH 的模型编辑界面里填），改网关侧就改 `/v1/models` 宣告的 `context_length`；两条链路现在共用 `wb_modelsdev.lookup` 的同一套取值（上游值 > 内建知识表 > 本地缓存 > 1M 默认），不会再出现「网关宣告 1M、一键配置写 300k」这种对不上的情况。
- **上游钉死档位的模型**（目录里只有 `reasoning.effort`、没有 `supportedEfforts`）不写 `reasoningEfforts`：给了选择器也不会改变上游实际跑的档位。
- **注意 profile 覆盖层**：若 `$DSH_HOME/profiles/<profile>/cordis.patch.yml` 里有 `- id: llm-pi-ai` 条目，它会**整体替换**该插件的配置（不是逐键合并）——此时 `settings.yaml` 里那份是被盖住的，模型清单与档位以 patch 那份为准，一键配置写进去不会生效。

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

### 12. 单请求体体积限制（`WB_MAX_PAYLOAD_BYTES`）

针对携带超长历史、大内联附件或高分辨率图片的请求，网关默认对单次请求体做上限保护（默认 16MB = `16777216` 字节），以防止异常巨大的请求挤占内存。若请求体超过上限，网关在解析前即直接返回 HTTP 413（`payload too large` 并附带 `gateway_hint` 说明）。

如需调整上限（例如提升至 32MB）：
- 设置环境变量 `WB_MAX_PAYLOAD_BYTES=33554432`（单位为字节）；
- 此环境变量仅在进程启动时读取一次，修改后重启 `wb-proxy` 即可生效。

---

### 11. 后台模型重定向（`WB_BACKGROUND_MODEL_NAMES` / `WB_BACKGROUND_MODEL_TARGET`）

桌面客户端（Codex 等）的模型选择器**只管主对话线程**。客户端内置的「模型路由配置」会把后台工作——记忆生成、环境建议、临时内容生成、自动化任务——送到自己硬编码的模型上，UI 里选什么免费模型都改不掉。这些请求带着同一把 Key 打到网关，于是在共享 Key 的部署里悄悄烧付费积分。

网关在唯一的 POST 分发点（选账号之前）把这类模型名改写成免费目标：

- `WB_BACKGROUND_MODEL_NAMES`：要拦截的模型名清单（逗号分隔，大小写不敏感），默认 `gpt-5.6-terra,gpt-5.6-luna,gpt-5.3-codex`；
- `WB_BACKGROUND_MODEL_TARGET`：改写目标，默认 `deepseek-v4.1-flash`。

改写只动 `model` 字段，请求体其余部分原样透传；命中与改写都会记一条 `alias: background model X -> Y` 日志，方便核对。OpenAI 形状的两个路由（`/v1/chat/completions`、`/v1/responses`）与 Anthropic 形状的 `/v1/messages` 都覆盖。不需要时把名单设为空即可关闭。

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

### Unreleased

- **剩余用量估算新增「撞线样本」区块**（#253）：按 (区域, 模型) 列出每个撞线窗口的原始值（账号、撞线时刻、窗口用量），并提供「复制为征集格式」按钮，方便汇总跨实例样本。数据来自现有 `limit-events`，不新增上游请求。
- **修复工具结果前插入消息导致 11148**（#420）：`repack_tool_result_blocks` 以前在 `assistant tool_calls` 与第一条 `tool` 结果之间出现 user / developer 消息时直接放弃重排；现在会把这类插入消息移到整批结果之后，保证结果紧跟调用。
- **按账号定址的调用入口**（#455）：新增 `/u/<ref>/v1/...`（`chat/completions`、`responses`、`models` 等），`<ref>` 支持完整 uid、uid 前 8 位或昵称；歧义 / 找不到 / 账号不可用都会如实报错，不会猜号或偷偷换号。不带 `/u/` 前缀的旧路径行为不变；定址请求同样走现有 API Key / 面板鉴权与用量统计。
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

- **模型目录按账号实际权益过滤**（`wb_entitlements.py`）：`/v1/models` 过去直接透出桌面端全量目录，免费账号（`personal_free_user`）会看到一批必然 `400 code 11158` 的 GPT / Grok / Gemini 模型，客户端选中才报错。现在上游以该错误码拒绝过的模型会记入 `accounts/model_entitlements.json` 并从目录隐藏，任何一次成功调用即自动恢复（账号升级或换号后无需重启）；判定只认上游错误码，不按模型名前缀猜；直接按 id 调用仍然放行，保留自愈通道。新增 `scripts/probe_model_entitlements.py` 一次性对齐整张目录，`GET /v1/models?include_hidden=1` 供复查被隐藏的模型，测试套件 `tests/_test_model_entitlements.py`。

已发布版本的完整记录（v1.4.5 ~ v1.7.13，含每版的 PR 归属）见 **[docs/CHANGELOG.md](docs/CHANGELOG.md)**。

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
