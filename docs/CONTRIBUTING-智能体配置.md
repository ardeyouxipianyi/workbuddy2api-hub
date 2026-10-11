# 智能体配置模块架构说明与演进指南 (CONTRIBUTING)

本文档面向 `workbuddy2api-hub` 维护者与社区贡献者，详细说明 `wb_agents.py` 智能体一键配置模块的设计思想、内部实现机制、数据存储格式，以及如何新增支持一款新的 AI 客户端。

---

## 1. 核心设计原则

1. **纯标准库实现 (Zero Dependencies)**：
   网关作为绿色免安装工具，不能为了读写 YAML/TOML 而引入 `pyyaml`、`tomli`、`tomlkit` 等三方轮子。所有配置解析与写入完全使用 Python 标准库（`os`, `re`, `json`, `shutil`, `hashlib`, `time`）。
2. **非破坏性文本级编辑 (Non-destructive Text-level Editing)**：
   用户的配置文件中往往包含个人手写的注释、特殊格式缩进、以及其他第三方 provider 的设置。标准库 `json` 只能安全操作 JSON，而对于 YAML、TOML 与 `.env`，模块采用自主实现的文本级更新器（Upserter）：仅在必要路径进行局部插入与替换，**绝不重排或抹除用户未修改的行**。
3. **零风险备份与事务回滚 (Safe Backup & Rollback)**：
   修改前自动留底，首次接入的原始文件永久锁定；针对多文件客户端，采用两阶段写入，出现异常自动回滚已写文件，确保用户配置不损坏、不丢失。

---

## 2. 模块架构与文件布局

```
wb-proxy/
├── wb_agents.py               # 核心模块：文本编辑器、注册表、备份还原、公共 API、CLI 入口
├── wb_proxy.py                # 路由层：/agents, /agents/cli, /agents/apply, /agents/restore
├── dashboard.html             # 交互层：智能体配置看板、状态渲染与异步请求
└── accounts/                  # 数据持久化目录（由网关管理）
    ├── integration-state.json # 智能体集成状态账本
    └── agent-backups/         # 备份根目录
        ├── claude-code/       # 按客户端 ID 隔离备份
        ├── codex/
        ├── opencode/
        ├── dsh/
        └── crush/
```

---

## 3. 客户端注册表机制 (`CLIENTS`)

所有支持的客户端统一在 `wb_agents.py` 的全局字典 `CLIENTS` 中声明。每个客户端条目包含以下字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | `str` | 客户端唯一标识符（全小写、短横线，如 `claude-code`） |
| `label` | `str` | 面向用户的展示名称（如 `Claude Code`） |
| `desc` | `str` | 简短描述，说明该客户端所指向的配置或协议 |
| `protocol` | `str` | 目标通信协议：`anthropic` 或 `openai` |
| `files` | `Callable[[str], dict[str, str]]` | 接收 `home` 目录路径，返回配置逻辑名到绝对路径的映射字典 |
| `probe` | `Callable[[str], bool]` | 接收 `home` 目录路径，返回该客户端是否已在本机安装的布尔值 |
| `apply` | `Callable[[dict, dict], dict[str, str]]` | 执行配置生成，接收 `paths` 与上下文 `ctx`，返回 `{文件绝对路径: 待写入文本内容}` |

### 3.1 探测逻辑规范 (`probe`)
为确保在各种操作系统与安装方式下都能准确识别，探针推荐采用**三重检测**（满足任一即视为已安装）：
1. 客户端配置目录是否存在；
2. 客户端主要配置文件是否存在；
3. 客户端命令行可执行文件是否存在（通过 `shutil.which("命令名")` 探测系统 PATH）。

### 3.2 注入上下文 (`ctx`) 结构
`apply` 回调接收到的 `ctx` 字典包含以下字段：
```python
ctx = {
    "base_url": "http://127.0.0.1:8788",         # 用户指定的网关 Base URL
    "base_url_v1": "http://127.0.0.1:8788/v1",   # 保证以 /v1 结尾的 URL
    "api_key": "sk-...",                         # 用户选定的 API Key
    "model": "deepseek-v4.1-flash",              # 选定的默认模型 ID（可选，可能为 None）
    "models": [                                  # 网关支持的模型列表（字典数组）
        {"id": "...", "context_window": 131072, "max_output": 8192, ...},
        ...
    ],
}
```

---

## 4. 如何新增一个客户端支持（分步指南）

以新增一款名为 `aider` 的终端 AI 编码工具为例：

### 第一步：在 `wb_agents.py` 编写配置路径与生成函数

```python
def _apply_aider(paths, ctx):
    path = paths["env"]
    text = _read_text(path)
    
    # aider 使用 .env 格式配置 OpenAI 兼容接口
    mapping = {
        "OPENAI_API_BASE": ctx["base_url_v1"],
        "OPENAI_API_KEY": ctx["api_key"],
    }
    if ctx.get("model"):
        mapping["AIDER_MODEL"] = "openai/" + ctx["model"]
        
    new_text = env_upsert(text, mapping)
    return {path: new_text}
```

### 第二步：在 `CLIENTS` 注册表中添加配置条目

```python
CLIENTS["aider"] = {
    "id": "aider",
    "label": "Aider",
    "desc": "AI pair programming in terminal; sets OPENAI_API_BASE.",
    "protocol": "openai",
    "files": lambda home: {
        "env": os.path.join(home, ".aider.env"),
    },
    "probe": lambda home: (
        os.path.isfile(os.path.join(home, ".aider.env"))
        or bool(shutil.which("aider"))
    ),
    "apply": _apply_aider,
}
```

### 第三步：添加专项测试

在 `tests/_test_agents.py` 中增加对该客户端的往返测试与格式验证：
```python
def test_aider_integration():
    # 验证 apply/restore 周期以及生成的环境变量格式
    ...
```

运行验证：
```bash
python tests/_test_agents.py
python tests/run_all.py
```

完成上述三步后，网关后端 `overview()` 会自动将新客户端列入 `/agents` 返回的列表中，前端看板会自动渲染对应的配置卡片，命令行配置器也会自动获得 `--client <id>` 的 apply / restore 能力——无需修改任何核心调度逻辑！

---

## 5. 文本级编辑器内部原理与规范

`wb_agents.py` 内部实现了 4 类专用文本修改器，遵循保守写入原则：

### 5.1 YAML 编辑器
- `yaml_upsert_inline(text, key_path, value)`:
  - 递归缩进块扫描：识别标准 2 空格/4 空格缩进层次；
  - 点号路径解析：如 `llm-pi-ai.providers.wb-proxy`，逐级查找父 block；
  - Inline Flow 自动展开：若父节点原先为单行内联 flow 语法（如 `providers: {}`），会自动安全展开为多行 block；
  - 叶子节点内联写入：叶子节点作为紧凑 JSON 对象写入对应缩进下方，避免多层嵌套重构失真；
  - 注释保护：跳过以 `#` 开头的行，不修改原有注释与行尾内容。
- `yaml_refs_upsert(text, key, value)`:
  - 专门用于 DSH `.credentials.yaml` 的顶层 `refs:` 块操作；
  - 严格匹配第 2 层缩进的 `key: value` 进行就地替换；不存在时追加在 `refs:` 块末尾；
  - 若 `refs:` 为不支持的单行内联格式（如 `refs: {}`），主动抛出 `AgentConfigError` 拒绝修改，防止破坏文件。

### 5.2 TOML 编辑器
- `toml_upsert_top_key(text, key, value)`:
  - TOML 规范要求：未归属于任何 `[table]` 的顶层键必须出现在整个文件的第一个 `[table]` 声明之前；
  - 该函数扫描首个 table 出现的位置，将顶层键（如 Codex 的 `model_provider = "wb-proxy"`）插入在该区域内部或就地替换已有键，严禁错位插入到后续 table 中。
- `toml_upsert_table(text, table, mapping)`:
  - 定位匹配的 `[table_name]` 起止范围；
  - 在 table 范围内逐一匹配键并替换；未出现的键追加在 table 末尾；
  - 若整个 table 不存在，则在文件末尾追加 `[table_name]` 块。

### 5.3 Dotenv 编辑器 (`env_upsert`)
- 通过正则 `^\s*([A-Za-z_][A-Za-z0-9_]*)="` 扫描已有变量；
- 存在则就地替换变量值；不存在则在文件末尾追加新键值对；
- 完整保留注释行（`# ...`）与空行间隔。

### 5.4 JSON 深度合并 (`deep_merge`)
- 递归合并字典树；
- 遇到字典类型则向下递归，遇到基本类型或列表则以补丁覆盖；
- 不修改原字典，生成全新字典后调用 `_dump_json()` 格式化（保证两空格缩进与 UTF-8 编码）。

---

## 6. 备份与状态管理机制

### 6.1 备份存储与命名
- 目录路径：`accounts/agent-backups/<client_id>/`
- 文件命名：`<YYYYMMDD-HHMMSS>-<原文件名>`（如 `20260408-153000-settings.json`）
- 自动轮转 (`_prune_backups`)：每个原文件名最多保留最新的 10 份历史备份（`BACKUP_KEEP = 10`），超出时自动删除最旧快照。

### 6.2 首次原件锁定机制
`integrate()` 在执行配置写入时：
- 若目标文件已存在且状态账本中**已有初次备份记录**，则继续沿用最初的备份指针；
- **核心目的**：无论用户后续在看板上切换多少次 API Key 或默认模型，一键还原时**始终还原至首次接入网关前的干净原始配置**，绝不会因多次 apply 把修改后的配置误作为备份。

### 6.3 两阶段事务写入与回滚
对于涉及多个文件的客户端（如 DSH 涉及 `settings.yaml` 与 `.credentials.yaml`）：
- **阶段一 (Stage & Backup)**：检查所有目标文件，在内存中暂存生成的新内容，对现有文件生成快照备份；
- **阶段二 (Atomic Write)**：使用 `.tmp` 临时文件 + `os.replace` 原子写入目标文件；
- **异常回滚**：若写入过程中任何一个文件发生 IO 异常或权限不足，立即触发异常捕获逻辑，将已写入成功的文件按原备份原子还原（若原本不存在则彻底删除），防止在用户机器上留下半套破损配置或脱离状态账本的孤儿文件。

### 6.4 外部修改感知 (`external_change`)
状态账本 `integration-state.json` 记录了每次成功写入时文件的 SHA-256 哈希值与字节数：
```json
{
  "claude-code": {
    "applied_at": "2026-04-08 15:30:00",
    "base_url": "http://127.0.0.1:8788",
    "model": "deepseek-v4.1-flash",
    "files": [
      {
        "path": "C:\\Users\\User\\.claude\\settings.json",
        "existed": true,
        "backup": "20260408-153000-settings.json",
        "sha256": "3a7b...",
        "bytes": 512
      }
    ]
  }
}
```
当用户在外部手动修改过该文件时，`_sha256_bytes()` 比对不一致，`overview()` 会返回 `external_change: true`，前端据此展示醒目的外部修改提示。

---

## 7. API 契约与前后端协同

### 7.1 `GET /agents`
返回系统支持的所有客户端探测状态、模型目录与网关 Base URL 提示：
```json
{
  "clients": [
    {
      "id": "claude-code",
      "label": "Claude Code",
      "desc": "Anthropic CLI; points ANTHROPIC_BASE_URL at this gateway.",
      "protocol": "anthropic",
      "installed": true,
      "configured": true,
      "config_paths": ["/home/user/.claude/settings.json"],
      "applied": {
        "at": "2026-04-08 15:30:00",
        "base_url": "http://127.0.0.1:8788",
        "model": "deepseek-v4.1-flash",
        "files": 1,
        "external_change": false
      }
    }
  ],
  "models": [{"id": "deepseek-v4.1-flash", "context_window": 131072}, ...],
  "keys": [{"id": "key_xxx", "name": "国际版 Key", "masked": "sk-1234***"}, ...],
  "global_key_set": true,
  "gateway": {"base_url": "http://127.0.0.1:8788/v1"}
}
```

### 7.2 `POST /agents/apply`
- 请求体：
  ```json
  {
    "client": "claude-code",        // 同时支持 client 与 client_id
    "client_id": "claude-code",
    "api_key_id": "key_xxx",        // 可选：__global, 具体 key id，留空自动回退
    "model": "deepseek-v4.1-flash", // 可选：指定的默认模型
    "models": [...]                 // 可选：省略时后端自动回退至网关全量模型目录
  }
  ```
- 鉴权：需要面板会话密码认证（通过 Cookie 或 `pwd` 鉴权头）。

### 7.3 `POST /agents/restore`
- 请求体：
  ```json
  {
    "client": "claude-code",        // 同时支持 client 与 client_id
    "client_id": "claude-code"
  }
  ```
- 响应：返回还原的文件清单与操作动作（`restored` 或 `deleted`）。

---

## 8. 命令行配置器（CLI）

`wb_agents.py` 同时是可执行的配置器，供**客户端不在网关机器上**的场景使用（网关跑在服务器 / NAS / 路由器 / 容器，客户端在另一台电脑）。它不走网关的 `/agents/apply`（那条路只认回环、并拒绝容器部署），而是在客户端机器本地完成全部写入，只通过 HTTP 从网关读取模型表。

### 8.1 命令一览

```bash
python wb_agents.py list     [--home DIR] [--state-dir DIR] [--json]
python wb_agents.py status   [--home DIR] [--state-dir DIR] [--json]
python wb_agents.py apply    --client CLIENT --base-url URL [--key KEY] [--model MODEL]
                             [--no-models] [--dry-run] [--home DIR] [--state-dir DIR] [--json]
python wb_agents.py restore  --client CLIENT [--home DIR] [--state-dir DIR] [--json]
```

- **Key 解析顺序**：`--key` → 环境变量 `WB_PROXY_API_KEY` → 交互式 `getpass` 提示。提示仅在 stdin 与 stdout **都是终端**时出现：Windows 上 NUL 设备的 `isatty()` 也为真，而 `getpass` 会直接读 `CONIN$`——在被拉起、没有控制台的进程里会造成永久阻塞；非交互环境缺 Key 时直接报错退出（码 1）。
- **模型表**：默认（等价于 `--models-from-gateway`）带 Key 请求 `GET /v1/models`，为 OpenCode / DSH / Crush 填入内嵌模型表，同时验证 Key。失败区分 401（Key 被拒）与其他错误；`--no-models` 完全跳过（也不校验 Key）。
- **`--dry-run`**：调用 `plan()` 只构建「将要写入的内容」，不备份、不落盘、不写 state；打印的内容与真实写入逐字节一致（同一生成路径）。
- **退出码**：`0` 成功；`1` 配置错误（`AgentConfigError`）；`2` 参数错误（argparse）。
- `--home` / `--state-dir` / `--json` 写在子命令前或后都可以（共享父解析器 + `SUPPRESS` 默认值，避免子解析器覆盖顶层值）。

### 8.2 与网关侧（看板）的关系

| 维度 | 看板一键配置（服务端） | CLI（客户端） |
|---|---|---|
| 运行位置 | 网关进程所在机器 | 客户端所在机器 |
| 可用条件 | 回环访问 + 非容器 / OpenWrt | 无限制（能 HTTP 到网关即可） |
| 状态与备份 | `<ACCOUNTS_DIR>/integration-state.json`、`agent-backups/` | `~/.wb-agent-config/`（`--state-dir` 可覆盖），每台机器一份 |
| 写入与还原逻辑 | `integrate()` / `restore()` / `overview()` / `plan()`（完全共用） | 同左 |

`plan(client_id, base_url, api_key, model, models, home)` 是公开的纯函数：`integrate()` 先 `plan()` 得到全部目标文件内容，再执行「阶段一备份 + 阶段二两阶段写入」。CLI 的 dry-run 与真实写入因此不可能漂移。

网关侧另提供 `GET /agents/cli`（免面板鉴权、无回环限制，内容为仓库内 `wb_agents.py` 原文，不含任何机密），方便远程机器一行 `curl` 取配置器。

### 8.3 备份语义的两条边界

- 首次 apply 时**已存在**的文件：保留**最早**一份备份，之后无论 apply 多少次、Key 换几次，`restore` 都回到首次接入前的字节；
- 首次 apply 时**不存在**（由本模块创建）的文件：只要它仍逐字节等于我们最后一次写入的内容，就仍视为「我们的文件」，`restore` 直接删除（避免把含 API Key 的自产文件当成原件"还原"）；一旦被用户手改过，它就变回用户文件，再次 apply 会为其建立备份，`restore` 恢复用户版本而不是删除。

---

## 9. 测试规范与验证指令

维护或扩展本模块时，必须确保以下测试命令通过：

```bash
# 1. 语法检查
python -m py_compile wb_agents.py wb_proxy.py

# 2. 智能体专项单元测试与契约测试
python tests/_test_agents.py

# 3. 命令行配置器端到端测试（子进程驱动真实 CLI + 桩网关，无网络）
python tests/_test_agents_cli.py

# 4. 前端交互 Handler 契约验证
node tests/_test_dashboard_handlers.js

# 5. 全套套件综合回归
python tests/run_all.py
```
