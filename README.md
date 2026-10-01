# WorkBuddy2API-Hub — 国际版、国内版多账号网关中枢

> ⏸️ **暂停更新（预计 10 天）**：从 **2026-09-29** 起暂停发布新版本，预计 **10 天**后恢复；期间 issue 与 PR 照常收，但合并与回复会慢一些。已发布的版本不受影响，照常可用；恢复更新后会删掉这段说明。

<p align="center">
  <a href="https://github.com/ardeyouxipianyi/workbuddy2api-hub/releases"><img src="https://img.shields.io/badge/Release-v1.6.10-2496ED?style=flat-square" alt="Version 1.6.10"></a>
  <img src="https://img.shields.io/badge/Python-3.9+-blue.svg?style=flat-square" alt="Python">
  <img src="https://img.shields.io/badge/API-OpenAI_Compatible-412991?style=flat-square" alt="OpenAI API">
  <img src="https://img.shields.io/badge/Dual_Realm-Intl_&_CN-0DBD8B?style=flat-square" alt="Dual Realm">
  <img src="https://img.shields.io/badge/License-MIT-green.svg?style=flat-square" alt="License">
  <img src="https://img.shields.io/badge/Vibe_Coding-100%25-ff69b4?style=flat-square" alt="Vibe Coding">
</p>

把腾讯 **[www.workbuddy.ai](https://www.workbuddy.ai)**（国际版）与 **[codebuddy.cn](https://www.codebuddy.cn)**（国内版）的原生服务封装成标准 OpenAI 兼容接口（Chat Completions 与 Responses API），并补齐多账号调度与运维能力：

- **开箱即用**：绿色包自带精简 Python，双击脚本即启；
- **双区域独立路由**：国际版 / 国内版独立配置与调度，看板一键切换，状态落盘；
- **模型目录对齐官方桌面端**：剔除代码补全通道与底层专线变体，能力与规格按桌面端宣告；
- **设备指纹隔离 (`derive_id`)**：以账号 UID 稳定派生机器码与会话标识，防多号关联风控；
- **OAuth 免客户端登录**：看板点链接完成授权即自动入库；
- **国内版自动化**：每日签到、成长任务与积分任务自动接取点亮领奖、猫猫日常旅行与连续打卡；
- **国际版每日活跃打卡**：自动建网页端会话并接上沙箱把这一轮真正跑完（ACP over HTTP+SSE），全自动领满官方每日活跃 30/50 积分奖励；
- **后台定时调度器**：09:00/21:00 国内签到旅行与国际版活跃打卡 · 22:00 保活 · 01:00 夜猫；
- **双协议支持**：Chat Completions 与 Responses API（Codex / Claude Code）；
- **Web 看板**：指标卡片、模型性能与用量大表、按 API Key 的用量归属、实时请求流水一屏可查。

> ⚡ **Vibe Coding 产物**：本项目为 100% Vibe Coding 协同产物，由人类开发者提出架构与业务意图，AI 助手端到端完成逆向分析、链路调度、WAF 指纹脱敏与界面编写。

---

## 一、快速启动

### 1. 本机单机使用

**Windows**：双击 **`start-wb-proxy.bat`**，保持窗口运行。**macOS**：双击 **`start-wb-proxy.command`**（首次被 Gatekeeper 拦截时，右键 →「打开」确认一次），或在终端执行：

```bash
./start-wb-proxy.sh          # 默认 8788 端口
./start-wb-proxy.sh 9000     # 自定义端口
```

启动后：

- **API 接口地址**：`http://127.0.0.1:8788/v1`
- **Web 监控看板**：`http://127.0.0.1:8788/`

首次启动若无账号，打开看板点 **「+ 添加账号 (OAuth)」** 完成授权即自动入库。macOS 启动脚本会自动挑选可用的 Python 3.9+（`/usr/bin/python3`、Homebrew 或包内 `python/bin/python3`），未安装可用 `xcode-select --install` / `brew install python`。

> zip 解压后若提示权限不足，先执行一次：
> `chmod +x start-wb-proxy.sh start-wb-proxy.command start-wb-proxy-lan.sh start-wb-proxy-lan.command allow-firewall.command`

### 2. 面板访问密码

打开看板需要先输入**面板访问密码**（默认 `admin`），它与 API Key 相互独立：密码只用于打开看板，可在「设置」页修改（或启动时用 `--panel-password` 指定），以 PBKDF2-SHA256 摘要存于 `accounts/settings.json`（不存明文）；登录状态保存在浏览器会话中，关闭浏览器或重启网关后需重新输入。

> 首次登录后请立即修改默认密码。

### 3. 局域网共享模式
允许局域网内其他设备（手机、平板、协同电脑）访问：

- **Windows**：双击 `start-wb-proxy-lan.bat`；**macOS**：双击 `start-wb-proxy-lan.command`，或：

```bash
./start-wb-proxy-lan.sh              # 端口 8788，自动生成/复用 API Key
./start-wb-proxy-lan.sh 8788 我的Key  # 自定义端口与 Key
```

- **Base URL**：`http://<本机局域网IP>:8788/v1`；带密钥直达面板：`http://<IP>:8788/?key=生成的Key`；
- **API Key**：不使用写死的默认密钥，首次启动生成高强度随机 Key 保存到 `accounts/settings.json` 并在终端打印，重启复用；也可用第二个参数传入自己的 Key（以传入的为准）；
- **macOS 防火墙**：首次监听端口时系统会询问是否允许 Python 接受连接，选「允许」；macOS 15+ 还需在「系统设置 → 隐私与安全性 → 本地网络」中允许终端访问。可用 `./allow-firewall.command` 查看状态并把 Python 加入允许列表。

---

### 4. 多 API Key 管理与出口绑定

在「设置」页可管理多个 API Key，并为每个 Key 指定独立出口——不同客户端各用各的 Key，国内 / 国外流量互不干扰，无需频繁切换全局出口：

- **添加与生成**：输入名称后点「生成随机 Key」，可随时复制；
- **出口绑定**：可固定走 🌐 国际版（`www.workbuddy.ai`）或 🇨🇳 国内版（`copilot.tencent.com`）；不绑定则跟随看板顶部的全局出口开关；
- **模型限制**：可为每个 Key 填写允许调用的模型（如 `deepseek*`、`gpt-6-astra`，支持 `*` 通配，多个用逗号分隔）；留空表示不限制。不在列表内的模型请求在本机直接返回可读的 400，既不会送达上游、也不会消耗任何额度——用来挡掉客户端背景请求偷偷调用的付费模型；
- **启停与删除**：可单独启用 / 停用；删除会立刻抹掉密钥（该 Key 再也无法调用），但条目本身以只读形式留在「设置」页底部的「已删除」折叠区，好让看板里的历史用量仍然显示它的名字；所有 Key 保存在 `accounts/settings.json`，重启保持；
- **用量归因**：看板「数据指标」页在账号表下方多一张按 API Key 归属的表——每个 Key 的请求数、Token、缓存命中、积分与模型分布（窗口内最多列 5 个模型，其余并入「其他」），口径与账号表一致，两表的请求数应当相等。该维度从升级后开始记录，更早的请求会一直留在「(切换前)」一行里；没绑定出口却两个出口都用过的 Key 会标成「跟随 · 混合」，它的积分是两套价格相加的结果；
- **防冲突**：面板保存过 Key 后，启动命令或脚本里的旧参数（如 `--api-key`）自动失效；
- **区域自检**：Key 绑定的出口与其请求的模型不匹配时（如用国际版 Key 调国内独占的 `deepseek-v4-pro`），直接返回可读的 400 校验错误，而不是上游晦涩的 WAF 拒流报错。

### 5. Docker 容器化部署

自带完整容器配置，零外部依赖：

```bash
docker compose up -d          # 后台启动（自动构建）
docker compose logs -f        # 查看网关日志
```

也可直接用 `docker run`：

```bash
docker run -d --name wb-proxy --restart unless-stopped -p 8788:8788 \
  -v $(pwd)/accounts:/app/accounts -v $(pwd)/usage:/app/usage \
  -e API_KEY=your_secret_key $(docker build -q .)
```

每次 GitHub Release 发布后，也可从 GHCR 拉取预编译镜像运行（正式版同步更新 `latest`，预发布版只有版本标签）：

```bash
docker pull ghcr.io/ardeyouxipianyi/workbuddy2api-hub:latest
docker run -d --name wb-proxy --restart unless-stopped -p 8788:8788 \
  -v $(pwd)/accounts:/app/accounts -v $(pwd)/usage:/app/usage \
  -e API_KEY=your_secret_key ghcr.io/ardeyouxipianyi/workbuddy2api-hub:latest
```

GHCR 新包默认私有；如需免登录拉取，首次发布后在 Packages 设置中将其改为 Public。保持私有时需先登录 `ghcr.io`。

- **持久化目录**：`./accounts`（账号凭证与活动区域）与 `./usage`（请求流水与指标快照）；
- **配置参数**：环境变量 `API_KEY`、`PORT`；
- **改 `PORT` 要同步改端口映射**：`PORT` 只决定容器内监听哪个端口，`-p HOST:CONTAINER` 的**右侧必须与之一致**，例如 `-e PORT=9000 -p 9000:9000`；只改 `PORT` 而映射仍是 `8788:8788`，请求会打到没人监听的端口上。用 compose 时 `ports` 与 `PORT` 要同时改（默认的 `8788:8788` + `PORT=8788` 本来就一致）。
- **鉴权**：容器以 `--lan` 启动（监听 `0.0.0.0`），会生成 API Key 写入 `./accounts/settings.json`，并打印在启动日志里：
  `docker compose logs wb-proxy | grep -i "api key"`。不带这个 Key 调 `/v1` 会收到 401；想用自己的 Key 就传 `-e API_KEY=...`。

### 6. 测试

全部测试集中在 `tests/`，一条命令跑完：

```bash
python tests/run_all.py            # 全部套件
python tests/run_all.py realm      # 只跑名字里含 realm 的
```

- 20 个套件：17 个 Python + 3 个 JS；JS 需要 PATH 上有 `node`，缺失时会跳过并提示。
- `tests/_mobile_check.py` 是独立的 Playwright 手机/桌面布局检查器（需自行安装 Playwright），按需手动运行，不在上面的套件集里。
- CI（`.github/workflows/tests.yml`）跑同一条命令：Ubuntu 上 python 3.9 与 3.12（3.9 是本项目声称的最低版本），Windows 上 python 3.12。

---

## 二、核心特性详解

### 1. 模型列表严格按照桌面应用 1:1 对齐

针对官方本地配置清单（50+ 底层模型）进行了深度清洗，剔除行内代码补全专用模型（如 `codewise-*`、`completion-gf`、`hunyuan-3b/7b`）与底层多云专线变体（如 `*-volc`、`*-lkeap`），严格对齐官方Windows桌面端，每个模型均宣告完整桌面软件中显示的上下文窗口（K/M 规范）、单次最大输出、视觉支持、工具调用以及推理档位。

* **🌐 国际版 (17 个)**：`hy4-preview-f`、`hy3`、`deepseek-v4.1-flash`、`gpt-6-astra`、`gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna`、`gpt-5.5`、`gpt-5.4`、`grok-4.7`、`gemini-3.5-flash`、`glm-5.3-flash`、`glm-5.3`、`glm-5.2`、`kimi-k3`、`kimi-k2.6`、`kimi-k2.8-preview`。
* **🇨🇳 国内版 (14 个)**：`hy4-preview-f`、`hy3`、`deepseek-v4.1-flash`、`deepseek-v4-pro`、`glm-5.3`、`glm-5.3-flash`、`glm-5.2`、`glm-5.1`、`glm-5v-turbo`、`minimax-m3`、`kimi-k3-1`、`kimi-k2.8-preview`、`kimi-k2.7`、`kimi-k2.6`。

> 清单与上游 `GET /v3/config` 的 `agents[cli].models` 保持同步，没装桌面端的机器也能取到同一份（接口不可用时依次回落到桌面端缓存文件、内置快照）。过滤规则：去掉 5 个档位别名与 `auto`，去掉 `-sg` / `-x` 变体，同名的只留 0.00 倍率那一档。上游新上的模型无需发版即可出现在 `/v1/models`。

> 💡 **关于同模型跨区域混合轮询的说明**：
> 目前对于同时存在于国内版和国际版的同名模型（如 `deepseek-v4.1-flash` 等），**暂未实现跨国内/国际账号的自动混合轮询**，而是作为两个独立区域分别配置与调度，请求只能走当前所选网关的独立出口。这主要是出于各区域网络环境隔离、出站指纹对齐与账号防风控安全考量；待作者后续实测验证确认长期使用稳定且无封号风险后，会尽快跟进并补齐同名模型的跨区域混合轮询能力。

### 2. 稳定物理设备指纹隔离 (`derive_id`)
国际版与国内版共用同一套算法内核：以账号 UID 结合固定业务盐值单向哈希派生机器码与会话标识——同一账号每次出站都来自同一台虚拟设备，不随机漂移；不同账号之间彼此独立，阻断跨账号关联风控。

### 3. 国内版每日签到、成长任务与积分任务全自动完成
- **每日签到**：一键完成国内版打卡领积分；
- **成长任务与积分任务**：自动批量接取未接任务，构造规范行为事件上报点亮（画布创建、灵感案例、模板使用、模型体验、多轮对话等 14 项），并自动领奖入账；
- **猫猫日常**：自动检查旅行状态，在家自动派出、归来自动领奖。

### 4. 后台常驻定时调度器 (Scheduler) 与每日自动化
常驻后台，每日按固定整点执行自动化运维排程：

- **每日 09:00 & 21:00**：国内版账号自动签到与猫猫旅行闭环；国际版账号自动执行每日活跃打卡对话（领官方每日 30/50 积分福利）；
- **每日 22:00**：集中扫描全库账号，Token 剩余寿命不足 2 小时自动调用 Refresh Token 保活；
- **每日 01:00**：深夜时段自动执行夜猫子任务；
- **国际版动态自适应**：切换至国际版视图时，看板顶部提供「每日活跃打卡 (国际版)」一键触发按钮。

### 5. 保留积分（避免余额被用尽）
看板「设置 → 保留积分」可设定一个最低余额，账号剩余积分低于该值时不再接单，账号行会显示「保留积分」标记。

- 上游在余额耗尽后会给账号发提醒短信，设一个阈值即可避免余额被用到 0；
- 填 `0` 表示关闭，这是默认值；
- 判定依据是最近一次查询到的余额（看板「积分」列），从未查询过余额的账号不受影响；
- 账号只是停止接单，仍留在池中并继续定时任务（签到与猫猫旅行本身是赚积分），充值后自动恢复可用。

### 6. 本地网络工具（可选，默认关闭）

部分客户端（如 Codex App）会在 Responses 请求里宣告 `web_search` / `web_fetch` 这类服务端工具，而上游没有对应的执行器——声明送上去，模型看得到工具却没有执行器，客户端最后只拿到一句 unsupported call。

看板「设置 → 本地网络工具」打开后，网关把那份声明换成自己的同名 function、拦下模型的调用、在本地执行（搜索走 DuckDuckGo HTML 版，抓页面抓模型给出的 URL），再把结果喂回模型，最多代跑 3 轮（`WB_MAX_WEB_ROUNDS` 可调，上限 8）；搜索过程会作为 `web_search_call` 卡片事件与 `url_citation` 引用回到客户端。

- **默认关闭**：工具声明原样透传，客户端自己声明的搜索工具照常拿到调用（v1.5.3 之后的既有行为，升级不受影响）；
- 打开后网关会主动出网抓取模型给出的 URL（只挡字面私网地址），且每轮代跑都会多跑一次上游、多消耗该账号额度；国内网络下 DuckDuckGo 可能连不上，那时模型拿到的是错误文本；
- 只影响声明了这两个工具的客户端，普通 `/v1/chat/completions` 客户端不经过这条路径。

---

## 三、账号添加与管理

打开看板 `http://127.0.0.1:8788/`，在「账号」区域操作：

若上游对某账号的单个模型返回 429，账号行会显示受限模型和预计恢复时间（浏览器本地时间）；该账号仍可用于其他模型。模型冷却状态仅在当前服务进程中保留，重启后清空。

### 方式一：浏览器 OAuth 授权（推荐，免客户端）
1. 点击 **「+ 添加账号 (OAuth)」**；
2. 选择要登录的区域（国际版 / 国内版），点击弹出的官方授权链接并在浏览器完成登录；
3. 程序自动检测回调，完成后账号自动加入账号池，无需手动复制凭证。

### ~~方式二：从本地桌面应用导入（暂不可用）~~
~~桌面客户端自 2026-09-24 起把 `accessToken` / `refreshToken` 改成加密存储（`$wbEncrypted` 信封）。扫描仍能读到文件，但拿不到可用的 token——导入后每个请求都会返回 401（聊天、刷新凭证、查积分都会被拒）。看板上的「扫描桌面客户端账号」入口已暂时隐藏，**请改用上面的 OAuth 方式添加账号**。~~

~~相关代码保留未删（前端 `scanDesktop()` 与后端 `/accounts/import/desktop` 都在），等解密打通或改走其他凭据来源之后再放出来。~~

---

## 四、客户端配置与接入

- **API 接口地址 (Base URL)**：`http://127.0.0.1:8788/v1`（局域网为 `http://<局域网IP>:8788/v1`）
- **API Key**：
  - 本机单机模式（未配置 Key 且未开 LAN）：可留空或填任意字符；
  - 已在看板配置 Key 或 LAN 模式：在看板「设置」页面添加或复制已绑好出口的 API Key（如固定走国际版的 Key 或国内版的 Key）。
- **模型名称**：填入 `/v1/models` 中列出的任意官方对齐模型 ID（如 `deepseek-v4.1-flash`、`gpt-6-astra`、`glm-5.3` 等）

### Codex CLI / Claude Code (Responses API)
网关原生内置 Responses 协议双向转换与 WAF 指纹脱敏：
```bash
export OPENAI_BASE_URL="http://127.0.0.1:8788/v1"
export OPENAI_API_KEY="你在看板设置中添加并绑定的API_Key"
```

---

## 五、看板与接口一览

访问 `http://127.0.0.1:8788/` 即可使用集成看板，核心接口包括：

「数据指标看板」页顶部可切换统计口径：**今日 / 本周 / 本月 / 全部历史 / 自定义**。本周自周一零点起算、本月自 1 号零点起算，自定义可指定起止时间（任一侧留空表示不限）。切换后 KPI 卡片、账号用量透视表与模型性能表会一起切到同一窗口。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | / | Web 用量与任务监控看板 |
| POST | /v1/chat/completions | 标准 Chat Completions 接口 |
| POST | /v1/responses | Responses API 协议接口 |
| GET | /v1/models | 官方对齐模型列表（含能力与规格宣告） |
| GET | /tasks | 国内版成长任务、连续打卡与猫猫日常状态 |
| POST | /tasks/run | 触发国内成长任务全自动点亮与领奖 |
| POST | /tasks/travel | 触发猫猫日常旅行（派出 / 领奖） |
| GET | /scheduler | 定时调度器运行状态与排程日志 |
| POST | /scheduler/trigger | 手动立即执行后台巡检保活 |

---

## 六、版本更新记录 (Changelog)

### v1.6.10

- **修复停用账号会丢掉出口绑定**（issue #89，感谢 [@lkxlzx](https://github.com/lkxlzx)）：此前停用账号时会顺手把它的 `proxySlot` 清空（`set_all_enabled` 与启动时的迁移也一样），重新启用不会恢复，那条账号就回落到直连——报告人说的「启用禁用账号后代理出口会被重置为直连」正是这个。现在绑定是操作者的选择，停用/启用不再动它：停用只是不接单，重新启用仍走原来的出口。
  - 槽位卡片的「已绑定」计数依旧只统计**启用中**的账号（表示这条出口当前有谁在用）；要真正解绑就显式选「直连」，或把槽位删掉（删槽位仍会把绑在它上面的账号解绑）。
  - `tests/_test_proxy_slots.py` 与 `tests/_test_proxy_slot_lifecycle.py` 里那几条「停用即释放」的断言改成钉住新行为：停用后绑定仍在、运行时出口不变、重新启用仍走同一槽位。

### v1.6.9

- **修好网页通道打卡：会话会被真正驱动到完成**（issue #90，感谢 [@Saracino34](https://github.com/Saracino34) 的准确定位；issue #75）：v1.6.4 只建了会话，而建会话只是**排队**——agent 要等客户端接上这条会话的沙箱并请求这一轮才会跑，所以网关建的那些会话全部停在 `CREATING`、没有任何输出，第二天自然不加积分（报告人 4/4 复现：手动发的会话十几秒 `completed`，网关建的一条都没动过）。现在按网页端的顺序走完：建会话 → `GET /console/as/conversations/{id}/session` 取沙箱 `link` + `token` → ACP（JSON-RPC over HTTP，服务端事件走 SSE）`initialize` → `session/load` → `session/prompt` → 轮询到 `completed`。实现放在新的 `wb_webagent.py`，只用标准库。
  - 打卡结果里带上会话状态与输出段数（如「网页通道 completed：12 段输出，15420 ms」），跑没跑成一眼可见，不用等第二天看积分；失败时错误里带会话 id。
  - 一轮最多等 120 秒（`WB_WEB_TURN_TIMEOUT` 可调）；实测一条「Hi」18.6 秒跑完、12 段输出。
  - 顺带更正 v1.6.4 的一条判断：`GET /v2/activity/banner` 返回的 `{"code":12302,"msg":"activity is offline"}` 只是 banner 模块自己的状态，不能当作「活动停发」的证据。
- **本地网络工具（`web_search` / `web_fetch`）改成默认关闭的看板开关**（[PR #87](https://github.com/ardeyouxipianyi/workbuddy2api-hub/pull/87)，感谢 [@Cekxri](https://github.com/Cekxri)）：默认「直通」——工具声明原样透传，客户端自己声明的搜索工具照常拿到调用（v1.5.3 之后的既有行为，升级不受影响）；要在看板「设置 → 本地网络工具」打开，网关才会把声明换成自己的同名函数、在本地执行并喂回模型。关闭时连同名调用的拦截也一并关掉，客户端自己的 `web_search` 不会被吞。
- 新增 `tests/_test_web_agent.py`（6 项，钉住驱动顺序与结果上报）；`tests/_test_daily_chat.py` 扩到 10 项、`tests/_test_local_web_tools.py` 扩到 68 项；整套 29 个测试文件全绿。

### v1.6.8

- **模型列表改为跟随上游 `GET /v3/config` 的实时清单**（issue #85，感谢 [@Jay-Young](https://github.com/Jay-Young)）：此前只认桌面端缓存文件与内置快照，没装桌面端的机器（Docker / NAS / Linux 服务器）拿不到桌面端 picker 的那份列表。现在 `/v1/models` 直接向出口要 `agents[cli].models`——与桌面端同一份清单，缓存文件退为回落。
  - 过滤规则：去掉 5 个档位别名（`default-model`、`fast-model`、`balanced-model`、`primary-model`、`deep-model`）与国内版的 `auto` 路由项，去掉 `-sg` / `-x` 变体，同名的只留 0.00 倍率那一档（国际版留 `deepseek-v4.1-flash`、丢 `-sg`，留 `hy4-preview-f`、丢 `hy4-preview`）。
  - 上游新上的模型无需发版即可出现在 `/v1/models`（表外的新名字按上游顺序追加在末尾）；表顺序与国内版 `hy4-preview-f` 这类免费档的保留不变。
  - 回落顺序：远端 → 桌面端缓存文件 → 窄端点（仍走旧白名单）→ 内置快照；10 秒一次、最多两次（聊天桌面 UA 失败后换应用 UA）。
  - 顺带修掉一处隐性退化：缓存文件是同一份文档但没有 `data` 信封，旧解析只认 `data.agents`，会让缓存路径悄悄退回旧读取器（数量对、元数据丢）；现在两种形态都认，并优先取 `cli` 这个 agent。
- **国际版模型清单补上 `grok-4.7`**：16 → 17，看板国际版专属标记同步。
- 新增 `tests/_test_remote_catalog.py`（10 项）钉住解析、过滤规则、免费同级优先、免发版追加、缓存文件驱动与回落不泄漏窄端点未知名。

### v1.6.5
### v1.6.6
### v1.6.7

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

### v1.6.4

- **国际版每日活跃打卡改走网页通道**（issue #75、issue #59）：两位报告人的实测一致——网关自动发出的桌面端身分对话拿不到每日 30 积分，而在网页版手动发一句就能拿到。顺着这条线索抓包后确认：网页版 app 的「对话」根本不是 `chat/completions`，而是 `/console/as/conversations/` 下的 agent 会话，创建会话时带上 prompt，后端就按该 prompt 起一次任务；而且这条链路只用 `Authorization: Bearer <accessToken>` 与 `X-User-Id` 两个凭据头（没有桌面端的 `X-IDE-*` 指纹），所以网关手里同一份账号凭据可以直接调用，不需要额外的网页登录——实测 GET 会话列表、POST batch-get 都返回业务响应而不是 401。
  - 现在国际版打卡是两步：先发一条桌面端身分的轻量对话（保持原行为），再在网页通道建一个带 prompt 的会话；返回结果里会带上会话 id，便于核对是否真的建上。
  - 账号栏新增 **「网页通道打卡 (国际版)」** 按钮：手动为所有已启用的国际版账号各建一个网页端会话，点击后会先弹一次确认（它会真的起任务、消耗少量积分）。这个按钮不写 `lastDailyChat`，所以不会让定时巡检跳过当天的正常打卡流程。
  - 「设置」页新增「国际版每日活跃打卡」开关（默认开启），关掉即回到只发桌面端对话的旧行为；取值同样严格限定 JSON 布尔，字符串一律 400 拒绝。
  - 需要留意：网页通道会真的起一次任务，会消耗该账号少量积分，换来的是每日 30/50 积分活跃奖励；面板上已写明这一点。
  - 另外记录一条上游状态：抓包期间 `GET /v2/activity/banner` 返回 `{"code":12302,"msg":"activity is offline"}`，即该活动模块当前处于下线状态。如果网页端也拿不到积分，原因可能在上游而不在通道——这条留待后续观察。

### v1.6.3

- **修复空状态「登录新账号 (OAuth)」按钮点击无反应**（issue #66，感谢 [@shis23](https://github.com/shis23) 的准确定位）：该按钮调用的是 `startLogin()`，而这个函数早在 v1.1.0 引入 `openLoginModal()` 时就已经不存在了，因此从 v1.1.0 起，账号池为空的首次部署用户点它不会有任何反应，浏览器控制台报 `startLogin is not defined`，而顶部工具栏的同名入口一直正常。现已改为调用真实存在的入口，并新增 `tests/_test_dashboard_handlers.js`：扫描 `dashboard.html` 中全部内联事件处理器，断言每一个都能找到对应的函数定义。这类「按钮绑定了一个不存在的函数」的问题只会在浏览器里、且只在该按钮被点击时暴露，任何服务端测试都看不见它。
- **看板时间范围扩展：本周 / 本月 / 自定义区间**（issue #68）：
  - 除「今日 / 全部历史」外，新增「本周」（周一零点起）、「本月」（1 号零点起）与「自定义」（起止时间自选，任一侧留空表示该侧不限）。口径与既有「今日」保持一致，都是本地零点锚定的自然区间；刻意不提供「最近 7 天 / 30 天」这类滚动别名，否则按钮标签在一周里有六天是错的。
  - `/usage`、`/usage/perf`、`/usage/analytics` 三个取数端点统一接受 `range` / `since` / `until` 参数，KPI 卡片、账号透视表与模型性能表会一起切到同一窗口，第一列的标题同步变为「本周消耗 Token」等，不会再出现「卡片显示今日、表格显示全部」的口径分裂。
  - 缓存键由原来的 today/all 二值改为真实窗口边界：本周与本月是重叠区间，二值键会让其中一个窗口的数字被另一个顶掉。
  - 模型性能表的延迟 / 速度列取自日志末尾的采样，窗口比采样更宽时会在表头注明覆盖起点，不再让局部数据冒充整个窗口。
- **修复出站身分切换后重启即丢失**（issue #76，感谢 [@1766266028](https://github.com/1766266028) 的完整定位与复现）：账号加载时把出站身分硬编码成默认的 WorkBuddy 桌面端，凭证文件里保存的值被读进一个全仓无人使用的字段（`saved_product`），于是面板上的 WB / VSC / CLI 切换（以及启用后的 429 自动切换）虽然确实写进了凭证文件，重启后却一律打回 WB——`set_product()` 的注释承诺「重启后仍然有效」，与实际行为矛盾。现在加载时读回凭证文件中的身分，非法值仍由 `normalize_product()` 回退到默认；同时面板切换在改完内存后立即落盘，不必再等 refresh / 签到 / 查积分之类的路径顺带保存——切完就重启容器的人不会再白白丢掉这次切换。新增 `tests/_test_product_persistence.py`（17 项断言）覆盖加载、别名归一、非法值回退、切换落盘与重载，以及身分最终落到端点与出站标头。

- **429 自动切换出站身分改为面板开关**（issue #67）：切换逻辑本身一直存在（WB / VSC / CLI 轮转、每轮最多 4 次、60 秒内算同一轮、成功即归零），但总开关是源码里的常量 `AUTO_SWITCH_PRODUCT = False`，面板上没有入口，想用只能改代码。现在改为「设置」页的开关，默认关闭（与改动前行为一致），保存后下一次请求即生效，不再需要动源码。取值严格限定为 JSON 布尔：字符串 `"false"` 之类一律 400 拒绝，否则一个真值字符串会把开关悄悄打开，而这正是关掉它的人最不希望发生的事。需要留意的是，开启后切换到的身分同样会随凭证文件持久化（见上一条），重启后不会自动回到 WB——面板上已写明这一点。

### v1.6.2

- **全套测试收拢与官方 CI 流水线建设**（PR #65，感谢 [@teddyli18000](https://github.com/teddyli18000)）：
  - 将散落在根目录的 20 个测试套件整齐规整至 `tests/` 目录下；
  - 新增统一测试运行器 `tests/run_all.py`，支持一键隔离运行全部 20 个测试套件或按关键词过滤；
  - 引入官方 GitHub Actions 自动化 CI 流水线（`.github/workflows/tests.yml`），每次提交与 PR 自动覆盖 Ubuntu（Python 3.9/3.12）与 Windows 跨平台测试矩阵。

### v1.6.1

- **修复 Docker 部署默认无鉴权开放代理漏洞**（PR #64，感谢 [@teddyli18000](https://github.com/teddyli18000)）：容器 CMD 默认追加 `--lan` 启动并移除写死的 `--port 8788`。无显式 `API_KEY` 时将自动生成高强度 Key 持久化保存并打印在日志中，拒绝匿名公网调用，消除未授权盗刷风险，同时支持通过 `PORT` 环境变量动态指定内部端口。
- **修复签到与活跃打卡后视图强制跳转**（PR #63，感谢 [@teddyli18000](https://github.com/teddyli18000)）：拆分 `refreshActiveRealm()` 与 `initRealm()`，国内签到和国际版每日活跃打卡完成后仅更新出口状态与用量，不再将当前浏览的区域视图强行跳回默认出口。

### v1.6.0

- **国际版每日活跃自动打卡领 30/50 积分**（issue #59）：官方国际站订阅规则规定「通过客户端发起有效对话可领每日活跃 30 积分（Pro 为 50 积分），网页端对话不计入」。现为国际版账号新增每日活跃自动化支持：
  - 后台调度器排程自动在 09:00 / 21:00 巡检时为当日未活跃的国际版账号发送一条轻量微型对话（默认走官方 `WB` 客户端出站标头与低消耗模型）；
  - 看板切换至国际版视图时，顶部工具栏提供「每日活跃打卡 (国际版)」一键触发按钮；
  - 严格记录 `lastDailyChat`，保证每个账号每天仅触发一次，不浪费额度。

### v1.5.9

- **修复 OmO / OpenCode 子代理 11128 WAF 拦截**（PR #62，感谢 [@Sakura1618](https://github.com/Sakura1618)，issue #61）：在 `deepseek-v4.1-flash` 上驱动 OmO 等多智能体调度框架时，上游 WAF 会对 `Sisyphus-Junior - Focused executor from OhMyOpenCode` 这一连续短语进行指纹特征匹配并拒流返回 `code: 11128 (Illegal API invocation from an unapproved channel)`。现于脱敏管线中针对性将该短语清洗为 `Sisyphus-Junior - Focused executor`（去掉末尾归属文本），既保留子代理业务身份与指令执行，又彻底消除拦截。

### v1.5.8

- **隐藏「扫描桌面客户端账号」入口**：桌面客户端自 2026-09-24 起把 `accessToken` / `refreshToken` 改成加密存储（`$wbEncrypted` 信封），扫描仍能读到文件，但拿不到可用的 token——导入后聊天、刷新凭证、查积分全部返回 401。入口已隐藏，请改用 OAuth 添加账号；相关代码（前端 `scanDesktop()` 与后端 `/accounts/import/desktop`）保留未删，等解密打通或改走其他凭据来源后再放出来。
- **两个按钮改名**：「一键自动分配出口」→「分配代理出口给未绑定账号」（它只给尚未绑定出口的已启用账号轮询分配，已有绑定的账号不动，原名容易被读成重新平衡全部账号；同时补了 tooltip 并修正两条 toast 的措辞）；账号行的「刷新」→「刷新凭证」（换的是该账号的登录凭证，不是页面、积分或账号列表）。
- **README 全面精简**：345 行压到 305 行、字符数减少约 23%，事实与贡献者记录一条未删；顺带修掉两处已失效的说法——头部特性里的「亦支持扫描本地客户端导入」，以及 Docker 那节整段的桌面凭据挂载说明。

### v1.5.7

- **`tool_choice="none"` 不再删除工具声明**（PR #57，感谢 [@zhangzm0](https://github.com/zhangzm0)，issue #56）：此前客户端发 `tool_choice="none"` 时，`normalize_tool_choice()` 会把 `tools` / `functions` 声明整个删掉。模型失去结构化工具通道后，把调用降级成 DSML／伪 JSON 文本塞进 `content`（`tool_calls` 为空、`finish_reason=stop`），Agent 客户端解析不到调用只能再追问一轮，模型重复一遍 —— 上下文每轮 +2 条消息、token 线性膨胀，直到撑爆窗口或用户手动断开。现在保留工具声明，由 `tool_choice` 字段自己表达「本轮不许调用」；上游只认字符串，对象形式仍降级成字符串（发对象会 11101）。实测上游并不真正遵守 `tool_choice="none"`，保留声明后它仍可能返回 `tool_calls`——这比让 Agent 原地空转好；确实需要禁止调用时，请由客户端不传 `tools`。

### v1.5.6

- **Docker 部署下的 Linux 桌面凭据挂载**（PR #55，感谢 [@LuFering](https://github.com/LuFering)）：新增 `docker-compose.override.yml.example`，以只读方式把宿主机 `~/.local/share/CodeBuddyExtension/Data/Public/auth` 挂进容器，补上 Linux + Docker 场景下看板扫描不到桌面凭据的说明；`.gitignore` 同时忽略本地 `docker-compose.override.yml`。
- **保留积分开关**（issue #44）：看板「设置」新增最低保留积分，账号余额低于该值时不再接单，避免余额被用尽后触发上游的提醒短信。填 `0` 关闭（默认）；从未查询过余额的账号不受影响；账号只是停止接单，仍在池中并继续定时任务，充值后自动恢复。阈值保存在 `accounts/settings.json` 的 `reserve_credits`，改动即时生效、无需重启。

### v1.5.5

- **出站身分改为三套模式**：账号行新增 `WB` / `VSC` / `CLI` 三档切换，默认 `WB`（WorkBuddy 独立桌面客户端，`X-IDE-Type: WorkBuddy`），另可切到官方 VSCode 插件（`VSCode`）或官方 CodeBuddy CLI（`CLI`），三者各自对应不同的出站指纹与端点。原先的两档实现把桌面端与插件端混为一谈，且默认走 CLI。
- **国际版 CLI 端点修正**：`www.codebuddy.ai` 在实测网络上无法解析（getaddrinfo 失败，系统解析器回 0.0.0.1 空路由），国际版 CLI 身分改走 `www.workbuddy.ai`，该域名接受 CLI 头并正常应答。此前国际版账号在默认身分下直接 502。
- **国际版模型列表对齐官方客户端**（issue #51）：现为 16 个，取自官方缓存 `agents[0]` 声明的真实模型（已排除 5 个档位别名与同名的 SG 区域变体）。补上 `glm-5.3-flash`（0.06x）与 `kimi-k2.8-preview`（0.77x），移除官方并未提供的 `hy4-preview` 与 `gpt-5.3-codex`。
- **`kimi-k2.8-preview` 解除国内独占限制**：此前被 `CN_EXCLUSIVE` 拦下并提示“请改用对应出口的 Key”，但官方国际版账号实测可正常调用（HTTP 200 且正常出内容），现已在两个区域同时开放。同类误判的 `glm-5.1`、`glm-5v-turbo`、`minimax-m3` 已实测可用但未动，留待后续处理。
- **国内版 `deepseek-v4.1-flash` 倍率修正**（issue #51）：看板此前对该模型写死显示「独家优惠 0.03x」，与实际上游计价的 0.11x 无关（官方国内版缓存中该模型没有任何促销折扣），现已改为直接沿用上报倍率。内置快照同步由 0.03 修正为 0.11。
- **`/health` 鉴权状态修正**（PR #52，感谢 [@teddyli18000](https://github.com/teddyli18000)）：`api_key_required` 此前只反映启动参数里的 Key，仅配了面板 Key 时会误报 `false`，与 `/v1` 实际拒绝无 Key 请求的行为矛盾。现改为复用手持路径的判定。
- **单模型限流可视化**（PR #50，感谢 [@teddyli18000](https://github.com/teddyli18000)）：`/accounts` 新增 `modelCooldowns`，看板账号行显示受限模型与本地恢复时间；429 状态改由独立短锁保护，避免看板读取与请求线程更新竞争。
- **国内账号昵称容错**：国内桌面端把昵称存成 `{"$wbEncrypted": ...}` 加密信封，此前会被 `str()` 成一整行字典画在账号行上；现在非字符串值一律回退显示 UID 前缀。

### v1.5.4

- **国内版目录补上 `hy4-preview-f`**：内置静态目录里只有旧 id `hy4-preview`（x0.29），它不在白名单里会被裁掉，而 `hy4-preview-f` 只能靠本机桌面端缓存补进来——没装过国内版桌面端的机器上该模型会消失。现按桌面端缓存补进静态目录（x0.00、1M 输入 / 64k 输出、推理档 high）。
- **看板显示积分消耗与账号昵称**（PR #45，感谢 [@Pro-XK](https://github.com/Pro-XK)）：最近请求表新增「积分」列，账号列改显示昵称（tooltip 保留完整 uid，账号不在池中时回退 uid 前缀）；「网关调用量」卡片副标题追加累计积分；账号透视表新增「消耗积分」列。
- **积分口径统一**：卡片与透视表此前一个只累计成功请求、一个含失败请求，同一页面上两个「消耗积分」永远对不上。现统一为「上游实际计费过的请求都计入，客户端取消不计」，并各自写明覆盖范围；credit 为 0 的行显示 `0.00` 而非 `—`。

### v1.5.3

- **移除网关内置的 `web_search` / `web_fetch` 代跑**（issue #43）：实测上游本来就没有服务端搜索能力（声明与不声明工具时模型反应一致、调用次数为 0），而代跑实现有参数名只认 `query`、工具重复下发、失败时发合成 `resp_wrapup` 把失败伪装成正常结束三处缺陷。现工具声明原样透传，客户端自己声明的搜索工具会正常拿到调用。

### v1.5.2

- **修复 Docker 镜像缺少运行时模块**（PR #41，感谢 [@wiggins-kong](https://github.com/wiggins-kong)）：Dockerfile 的显式 COPY 清单漏掉 v1.5.0 新增的 `wb_identity.py` 与 `wb_webtools.py`，容器启动即 `ModuleNotFoundError`。现改为 `COPY wb_*.py dashboard.html ./`。仅影响 Docker 部署，绿色包与本地运行不受影响。

### v1.5.1

- **看板时间范围与筛选修正**（issue #39）：「今日 / 全部历史」此前只影响部分指标卡，现首张卡跟随切换、第二张固定为累计并注明差异原因；模型性能表跟随所选范围（`/usage` 与 `/usage/perf` 新增 `range` 参数），并新增「账号」「模型」筛选，汇总行随筛选重算、失效筛选自动清除。
- **修复账号用量透视表丢失**：该表格标记曾被误删，`getElementById` 恒为 null，整个「各账号用量透视」区块从未渲染；现恢复并适配移动端卡片布局。
- **新增测试**：`_test_usage_range.py`（22 项断言）与 `_test_matrix_filters.js`（19 项断言）。

### v1.5.0

- **Codex App namespace 工具支持**（PR #33，感谢 [@Cekxri](https://github.com/Cekxri)）：展开 `namespace` 后转发，回程补上该字段；同时支持 `agent_message`（子代理）与无 `call_id` 的 `function_call_output`。
- **出站身分标头修正**（PR #33）：原 `X-Product: WorkBuddy` 为自创组合，官方为 `X-Product: SaaS`；账号行可按需切换 WB / VSC / CLI 三套身分。
- **本地 `web_search` / `web_fetch`**（PR #33）：客户端声明时由网关代跑（v1.5.3 已移除）。
- **DeepSeek 多轮 `reasoning_content` 回填补全**（PR #36，感谢 [@ayeaaaa](https://github.com/ayeaaaa)）：thinking 开启即回填，并把字段镜像到 `reasoning` 且保证非空；与 v1.4.9 的档位注入互补。
- **看板移动端布局**（PR #37，感谢 [@ayeaaaa](https://github.com/ayeaaaa)）：新增 `≤640px` 手机布局与 `≤400px` 微调，桌面布局不变。
- **API Key 行 id 唯一化**（PR #40，感谢 [@wiggins-kong](https://github.com/wiggins-kong)）：避免两行同 id 时 `/settings/reveal` 返回别人的 key；读取时也去重，历史文件自愈。
- **修复 `/v1/responses` 非流式路径崩溃**：该路径引用了未定义的 `ns_map`，任何非流式请求都会抛 `NameError` 断开连接；流式路径不受影响。

### v1.4.9

- **DeepSeek 思维链默认开启**：此前只注入 `thinking:{type:"enabled"}` 而不带推理档位，上游仍按「不思考」应答。现缺档时按模型目录声明的默认档补齐（无声明回退 `high`）；客户端显式档位不覆盖，`thinking:{type:"disabled"}` 与 `reasoning_effort:"none"` 照常退出。
- **工具调用配对自愈**：客户端写不回工具结果时，坏历史被每轮重放、上游对之后每条消息返回 `400 code 11148`，一次失败调用即可报废整条会话；并行调用间插入的消息（如 Codex 的 `image_resize_notice`）同样打断配对。现出站前把结果块移回所属批次，并按同一份 id 集合对称裁剪孤儿。
- **`prompt_cache_key` 注入（默认关闭）**：按账号隔离的缓存键（`wb2a-<uid8>-<摘要>`），用 `WB_PROMPT_CACHE_KEY=1` 开启。默认关闭是因为实测该上游本就会复用重复前缀，带不带结果一致。
- **新增 `_test_upstream_repairs.py`**（49 项断言，无网络依赖）。

### v1.4.8

- **HTTP 连接同步修复**（PR #30）：请求被提前拒绝时未读取请求体，会让后续请求在同一 keep-alive 连接上解析失败（日志表现为空请求行的伪 414）；同时支持 chunked 请求体、`Expect: 100-continue`、超大请求体立即 413。
- **超长请求行回复丢失修复**：414 后直接关闭会因未读数据触发 RST，客户端收不到响应；现先有限度排空再回复。
- **macOS 启动脚本**（PR #31）：新增 `start-wb-proxy.sh` / `.command`、局域网版本与防火墙助手；Windows `.bat` 未修改。

### v1.4.7

- **每账号独立出口代理**（PR #26，感谢 [@ayeaaaa](https://github.com/ayeaaaa)）：新增可命名、可启停的代理槽位，账号绑定后其全部出站请求固定走该出口；看板支持槽位增删、出口 IP 测试与逐账号绑定。
- **账号身份请求全量走代理**：`refresh` / `checkin` / `fetch_credits` 此前从宿主机真实 IP 发出，会把账号身份与宿主 IP 关联在一起。
- **槽位 ID 不再回收**：ID 改由持久化计数器分配，删除槽位时同步解绑指向它的账号。
- **顶部 GitHub 仓库入口**。

### v1.4.6

- **看板数据口径与展示修正**：指标看板固定展示两区合计，不再跟随当前出口；模型性能表按「模型 × 出口 × 账号」逐行展开，新增「失败」列与三色分列。
- **看板会话与页面保持**：会话失效后立即停止轮询并清除旧凭证，不再刷 401 日志；刷新后保持所在页面。

### v1.4.5

- **GPT 系列流式 Token 与生成速度修复**：忽略中间帧全 0 的 usage 占位，并加入断流 Fallback 估算，修复 `gpt-5.6-luna` / `gpt-6-astra` 等模型输入输出为 0、生成速度缺失的问题。

---

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
