# 让 cc-switch 不再显示「未定价」：model-pricing.json 导出手册

> 涉及本仓库文件：`wb_pricing.py`（导出实现）、`tests/_test_ccswitch_pricing_schema.py`（结构核验）、本文。
> 文中 `src-tauri/...`、`src/...` 均指 **cc-switch 仓库**内的路径。行号按核对时的源码，函数名稳定、行号会随上游漂移；引用前请用函数名重新定位。

## 0. 结论先行

cc-switch 的花费**全部由它自己按模型定价表重算**，它不解析、也不采信网关在响应里报的 cost。所以「WorkBuddy 的请求在 cc-switch 里显示未定价」不是上游少发了字段，而是 cc-switch 本地 `model_pricing` 表里没有这些模型的价格（或价格算出来是 0）。修法是**把价格送进 cc-switch 的定价表**：

1. `wb_pricing.cc_switch_pricing()` 把当前生效价表导成 cc-switch 的 `model-pricing.json` 结构（每百万 USD 的**十进制字符串**）；
2. 把这份 JSON 落到 cc-switch 的本地定价文件 `model-pricing.json`（或在它的「模型定价」界面里逐条录入——两者写的是同一张表）；
3. cc-switch 点一次「重新加载文件」，此后导入的 DSH 会话请求四列花费不再是 0，合计行显示美元金额而不是「未定价」。

三步都能离线完成，不要求网关和 cc-switch 同时在线。

## 1. 为什么只在响应里加 cost 不够

### 1.1 cc-switch 的花费是它自己算的

定价数据结构只有四个价格字段，全是 `Decimal`：

```rust
// cc-switch-pr7356/src-tauri/src/proxy/usage/calculator.rs:20-26
pub struct ModelPricing {
    pub input_cost_per_million: Decimal,
    pub output_cost_per_million: Decimal,
    pub cache_read_cost_per_million: Decimal,
    pub cache_creation_cost_per_million: Decimal,
}
```

计算入口 `CostCalculator::calculate` / `calculate_for_app`（同文件 `:44-70`），核心公式在 `:72-120`：

```rust
let million = Decimal::from(1_000_000);
let input_cost = Decimal::from(billable_input_tokens) * pricing.input_cost_per_million / million;
let output_cost = Decimal::from(usage.output_tokens) * pricing.output_cost_per_million / million;
let cache_read_cost = Decimal::from(usage.cache_read_tokens) * pricing.cache_read_cost_per_million / million;
let base_total = input_cost + output_cost + cache_read_cost + cache_creation_cost;
let total_cost = base_total * cost_multiplier;
```

也就是说：**cc-switch 算出来的钱 = token 数 × 它自己表里的单价**，与上游报文无关。

### 1.2 DSH 会话这条路上，价格只有一个来源：`find_model_pricing`

`services/session_usage_dsh.rs` 导入 DSH 会话日志时，先查价、再算钱；查不到就写 `"0"`：

```rust
// cc-switch-pr7356/src-tauri/src/services/session_usage_dsh.rs:379-386
let pricing = find_model_pricing(conn, &record.model);
let cost = pricing.as_ref().map(|pricing| {
    CostCalculator::calculate_for_app(APP_TYPE, &record.usage, pricing, Decimal::ONE)
});
let total_cost = cost
    .as_ref()
    .map(|cost| cost.total_cost.to_string())
    .unwrap_or_else(|| "0".into());
```

`input_cost` / `output_cost` / `cache_read_cost` / `cache_creation_cost` 四项同理，缺价一律回落 `"0"`（同上 `:387-402`）。`DshUsageRecord` 这个结构体里根本没有任何 cost 字段，会话 JSONL 里也没有。

代理这条路上同样如此：`src-tauri/src/proxy/usage/parser.rs` 全文只解析 token 字段，**不含任何 cost 关键字**——即使网关在响应体里回传一份精确的 cost，parser 也不会读它、不会落库。

`find_model_pricing` 的真实匹配规则（`services/usage_stats.rs`，`find_model_pricing` → `find_model_pricing_row`）：

1. 先把 model 字符串规整成一组候选：`clean_model_id_for_pricing`（`:2766-2781`）切掉最后一个 `/` 之前的部分与第一个 `:` 之后的部分、去两端空白、`@` 换成 `-`、**整体转小写**；随后逐级剥掉 Claude Desktop 前缀、Bedrock 版本后缀、日期后缀、推理强度后缀（`model_pricing_candidates`，`:2729-2763`）。
2. 对每个候选**先精确匹配**：`WHERE model_id = ?1 COLLATE NOCASE`（`query_model_pricing_exact`，`:2680-2701`）。候选已经是小写、列比较又 `NOCASE`，所以 `DeepSeek-V4.1-Flash` 也能命中 `deepseek-v4.1-flash`，大小写不会导致查不到。
3. 精确匹配不到才**前缀兜底**：`WHERE model_id LIKE '<候选>-%' ORDER BY LENGTH(model_id) ASC LIMIT 1`（`query_model_pricing_prefix`，`:2703-2727`），这条是留给「价表里存的是带日期/变体后缀的名字」的情形，并优先取最短（最贴切）的那一行。

所以 `modelId` 直接写 hub 的模型名（`deepseek-v4.1-flash`、`gpt-6-astra` …）就能命中。真正会漏的是「连候选都规整不出来」的情形：模型名中间夹了多余空格或全角字符，或 hub 侧名字与 DSH 记录里的模型名根本不是同一个词。

### 1.3 「未定价」的唯一判据：`totalCostUsd === 0`

```ts
// cc-switch-pr7356/src/types/usage.ts:342-356
export function isUnpricedUsage(log: UsageCostLog): boolean {
  const totalCost = Number.parseFloat(log.totalCostUsd);
  const multiplier = log.costMultiplier == null ? undefined : Number.parseFloat(log.costMultiplier);
  return (
    log.statusCode >= 200 && log.statusCode < 300 &&
    hasUsageTokens(log) &&
    Number.isFinite(totalCost) &&
    (!Number.isFinite(multiplier) || multiplier !== 0) &&
    totalCost === 0
  );
}
```

两个直接推论：

- 判定与「上游说了什么」完全无关，只看库里 `total_cost_usd` 是不是 0；
- 价格为 **0** 和价格 **缺失**，在 UI 上是同一个症状（所以「这个模型免费」也会显示未定价）。

请求详情面板渲染的就是这四个花费行 + 合计行：

```tsx
// cc-switch-pr7356/src/components/usage/RequestDetailPanel.tsx:183-204（四行）
{ key: "input",  label: t("usage.freshInput"),       value: usd(request.inputCostUsd) },
{ key: "output", label: t("usage.outputTokens"),     value: usd(request.outputCostUsd) },
{ key: "read",   label: t("usage.cacheReadTokens"),  value: usd(request.cacheReadCostUsd) },
{ key: "write",  label: t("usage.pricing.cacheWrite"), value: usd(request.cacheCreationCostUsd) },

// :212-222（合计）
value: unpriced ? t("usage.unpriced") : usd(request.totalCostUsd),
```

`usage.unpriced` 的中文文案是「未定价」（`src/i18n/locales/zh.json:2196`）。

### 1.4 缺的是一行价格，不是一列字段

| 你看到的症状 | 真实原因 | 修法 |
| --- | --- | --- |
| 明细四行全 0、合计「未定价」 | `model_pricing` 表里没有该 `model_id` | 补价格（第 2 节任一路径） |
| 表里有该模型但四行全 0 | 四个价格写成了 `"0"` | 用真实单价重导 |
| 有价格、新请求正常、旧请求仍「未定价」 | 历史行没有回填 | 触发 backfill（见 3.3） |
| 上游 `/v1/usage` 里 `cost` 有值 | 该字段只服务别的消费方 | 与 cc-switch 无关，不影响本问题 |

## 2. 三条落地路径

### 2.1 路径 A（推荐）：把导出文件放到 cc-switch 的本地定价文件

生成（在本仓库根目录）：

```powershell
python -c "import io,json,wb_pricing; io.open('model-pricing.json','w',encoding='utf-8',newline='\n').write(json.dumps(wb_pricing.cc_switch_pricing(), ensure_ascii=False, indent=2) + '\n')"
```

`indent=2` + 末尾换行是刻意的：cc-switch 自己写这个文件也用 pretty JSON 并补 `\n`（`services/model_pricing.rs:219-225`，`serde_json::to_vec_pretty` + `data.push(b'\n')`）。

**放到哪个目录**——文件名固定，目录由 `get_app_config_dir()` 决定：

1. 若 `app_paths.json` 里配置了 `app_config_dir_override` **且该目录存在**，就用它（`src-tauri/src/app_store.rs:9`、`:25-55`；路径不存在会打印警告并退回默认）；
2. 否则默认 `dirs::home_dir()\.cc-switch`（`src-tauri/src/config.rs:313-318`）；
3. Windows 兼容分支：默认位置的 `cc-switch.db` 不存在、而环境变量 `HOME` 下的 `.cc-switch\cc-switch.db` 存在时，改用后者（v3.10.3 时代的老路径，`config.rs:334-352`）。

最终文件路径 = `<app config dir>\model-pricing.json`（`services/model_pricing.rs:13` 的文件名常量 + `:102-104` 的拼接函数）。

不想猜路径就别猜：cc-switch → 用量统计 → **定价** 页签 → 右上角 `⋯` 菜单，第一行直接显示「本地定价文件」的完整路径，下面还有「打开目录」「重新加载文件」两项（`src/components/usage/ModelsDevAutoSyncPanel.tsx:656-687`；文案见 `zh.json:2390-2394`）。

**让它生效（不用重启）**：`⋯` →「重新加载文件」→ `reloadLocalFile()` 调用 `usageApi.getModelPricing()`（`ModelsDevAutoSyncPanel.tsx:504-522`）→ 后端 `get_model_pricing` 里执行 `sync_local_model_pricing`（`src-tauri/src/commands/usage.rs:186-189` → `services/model_pricing.rs:297-315`），把文件 upsert 进 DB；`upserted > 0` 时顺手 `backfill_missing_usage_costs()` 重算历史 0 花费行。重启 App 也会走同一条同步（`src-tauri/src/database/mod.rs` 打开库时 seed + sync）。

**覆盖之前必须知道的三件事**（因为它在 cc-switch 眼里是「用户覆盖表」，不是完整表）：

```rust
// cc-switch-pr7356/src-tauri/src/services/model_pricing.rs:232-234
// The local file stores user/models.dev overrides only. Exporting the
// complete seeded table here would turn built-in prices into overrides and
// roll back future repair_current_model_pricing corrections on startup.
```

1. 我们导的是**整张价表**，等于把内置价格钉成 override：以后 App 升级里带的 `repair_current_model_pricing` 价格修正不再覆盖这些模型；
2. `modelsDevSync` 段会被整段替换。导出写的是 `autoSyncEnabled: true` / `includeCommonModels: true`——**就是 Rust 侧 `ModelsDevSyncConfig::default()` 的取值**（`services/model_pricing.rs:41-69`，两个字段都带 `#[serde(default = "default_true")]`），也就是「行为与 App 的默认值一致、不替用户改设置」。反过来说：如果你曾经手动关掉 models.dev 自动同步，导入后这两项会被写成 true，需要再手动关一次（导出不写 false，是因为那等于替所有用户静默关闭同步）；
3. 你自己在界面里改过的价格和 `deletedModelIds` 墓碑都会被覆盖（导出 `deletedModelIds: []`，之前删掉的模型会重新出现）。

只想补 WorkBuddy 那几个模型？把导出的 `models` 数组裁到需要的那几条再落地，效果一样、副作用最小。

### 2.2 路径 B：通过 cc-switch 的「模型定价」界面录入

界面位置：用量统计 → **定价** 页签（`UsageDashboard.tsx:784-800` 挂载 `<PricingConfigPanel />`）。这个面板**没有**「从 JSON 导入」按钮，只有「新增」「从 models.dev 导入」「编辑」「删除」（`PricingConfigPanel.tsx:353-370`）——因为它本身就是 2.1 那份文件的编辑器：

- 新增行默认四个价格都是 `"0"`（`PricingConfigPanel.tsx:320-330`），表头单位文案 `"unit": "USD / 每百万 Tokens"`（`zh.json:2565`）；
- 保存时走 `update_model_pricing` / `update_model_pricing_batch`，**同时写 DB 并重写 `model-pricing.json`**（`services/model_pricing.rs:431-440` → `:395-413` 的 `write_file_unlocked`）；
- 单条改价只回填该模型的历史行（`:420-424`），批量改价回填全表（`:416-418`）。

适合只补 1~2 个模型。注意输入必须是**十进制字符串**且非负（`normalize_decimal`，`model_pricing.rs:106-122`）：`1e-3`、`$0.28`、`abc` 都会被拒；`modelId` 与 `displayName` 不能为空（`model_pricing.rs:125-157`）。

### 2.3 路径 C：`GET /pricing/cc-switch.json` 直接拉取

本仓库把同一份导出挂在下载端点上（契约 F），行为对齐 `/pricing/mapping`：属于**面板路由**、用面板会话鉴权（`X-Panel-Token` 头，见 `wb_proxy.py` 的 `_panel_token` / `_panel_ok` / `_is_panel_route`），返回：

- `Content-Type: application/json; charset=utf-8`
- `Content-Disposition: attachment; filename="model-pricing.json"`

```powershell
# 1) 登录换面板 token（默认端口 8788；密码是你自己设置的面板密码）
$login = curl.exe -s -X POST http://127.0.0.1:8788/panel/login `
  -H "Content-Type: application/json" -d '{\"password\":\"<面板密码>\"}' | ConvertFrom-Json

# 2) 下载并直接覆盖 cc-switch 的本地定价文件（路径换成 2.1 里确认过的那个）
curl.exe -s -H "X-Panel-Token: $($login.token)" `
  http://127.0.0.1:8788/pricing/cc-switch.json -o "$env:USERPROFILE\.cc-switch\model-pricing.json"

# 3) 回到 cc-switch：定价 → ⋯ →「重新加载文件」
```

要点：文件名必须保持 `model-pricing.json`（cc-switch 只认这个名字）；这个路由不吃 API key，要的是面板 token。

### 2.6 为什么加响应头不够，非得导入这张表

网关现在也会把每次请求的花费写在响应头上（非流式 `X-Api-Cost-Usd` / `X-Api-Cost-Cny` / `X-Api-Cost-Known` / `X-Api-Pricing-Source` / `X-Api-Cost-Model`，流式 `X-Api-Cost-In-Stream: usage`，花在最后一帧的 `usage` 里），body 的 `usage` 里也有 `cost` / `cost_details`。这些是给别的客户端和网关、访问日志用的。

cc-switch **不读上游响应里的任何金额**：它解析响应只取 token 数（`proxy/usage/parser.rs`），四行花费由它自己的 `model_pricing` 表加 `CostCalculator` 现算（`logger.rs`、`session_usage_dsh.rs`），表里查不到 `deepseek-v4.1-flash` 就显示「未定价」——哪怕上游说这次花了多少。DSH 会话日志导入时只写 `inputTokens` / `outputTokens` / `totalTokens` / `cacheReadTokens` 四个数，不会带价格，所以补价只能靠这张表。结论：要消掉「未定价」，导入 `model-pricing.json` 是唯一一步；响应头解决的是另一件事。

## 3. 怎么验证真的生效了

### 3.0 先确认导出本身是对的（可放进 CI）

```powershell
python tests/_test_ccswitch_pricing_schema.py
python -c "import wb_pricing,json; d=wb_pricing.cc_switch_pricing(); print(len(d['models'])); print(json.dumps(d['models'][0], ensure_ascii=False, indent=2))"
```

`tests/run_all.py` 会自动发现这个 `_test_` 前缀的用例。断言覆盖：

- 顶层四个键与 cc-switch 的 `ModelPricingFile` 完全同名：`version` / `modelsDevSync` / `models` / `deletedModelIds`；
- 每个模型六个键与 `ModelPricingInfo` 完全同名：`modelId` / `displayName` / `inputCostPerMillion` / `outputCostPerMillion` / `cacheReadCostPerMillion` / `cacheCreationCostPerMillion`；
- 四个价格都是**字符串**、都是普通十进制（无科学计数法、无前缀 `$`）；
- `version == 1`；`cacheCreationCostPerMillion` 恒为 `"0"`；`cacheReadCostPerMillion` 在缺 `input_cache_hit` 时回落 `input_cache_miss`；
- `modelsDevSync` 六个键齐全，且 `autoSyncEnabled` / `includeCommonModels` 为 `true`（Rust 默认值，不改变用户既有同步设置）；
- 带 `bands` 的模型导出的是 `flat` 基准价（无 `flat` 时取 `bands[0]`）；
- 用一组真实 token 数复算，`totalCostUsd > 0`——即 `isUnpricedUsage` 会返回 false。

### 3.1 文件落地之后

1. 定价页 → `⋯` →「重新加载文件」，等 toast「本地定价文件已重新加载」（`zh.json:2394`）；
2. 在表格里找到 `deepseek-v4.1-flash`（或任一 WorkBuddy 模型）：四列价格应非 0，单位是 USD / 每百万 Tokens；
3. 若模型出现了但价格仍是 0：回看导出的那一行，`pricing/pricing.json` 里该模型可能缺 `input_cache_miss` 或 `output`；
4. 若模型压根没出现：`modelId` 与 cc-switch 看到的 model 字符串不是同一个词——匹配前会规整大小写、`vendor/` 前缀、`:`/`@` 后缀与两端空白（`usage_stats.rs:2766-2781`），但词中夹空格或全角字符救不回来（详见 §1.2）。

### 3.2 打开一条具体请求看（最终验收）

1. 用量统计 → 请求列表 → 点开一条 `app === "dsh"` 的请求；
2. 花费四行（fresh input / output / cache read / cache write）不再全是 0（`RequestDetailPanel.tsx:183-204`）；
3. 底部「合计」显示美元金额而不是「未定价」（同文件 `:212-222`；判据 `types/usage.ts:342-356`）；
4. **反向对照**：临时把该模型的 `modelId` 改错一个字符（或四个价格全写 `"0"`）→ 重新加载 → 那条请求回到「未定价」。这个对照能证明因果，而不是「碰巧变了」。

> **别被 4 位小数骗了。** 请求详情里的金额走 `usd = (value) => money(value, 4)`（`RequestDetailPanel.tsx:88-89` → `hooks/useMoneyFormat.ts:27-28` → `components/usage/format.ts:139-150` 的 `value.toFixed(digits)`），即**四舍五入到 4 位小数**。单条小额请求的分项不足 `$0.00005` 时就显示成 `$0.0000`，看着像没算；但**合计不再是「未定价」**，会是 `$0.0001`~`$0.0009` 这种值。以 `deepseek-v4.1-flash`（输入 `0.03`、输出 `0.5`、缓存读 `0.01` 每百万）为例，一条 1000 输入 / 500 输出 / 200 缓存读的请求：输入 `$0.00003`、输出 `$0.00025`、缓存读 `$0.000002`，按 4 位显示分别是 `$0.0000` / `$0.0003` / `$0.0000`，**合计 `$0.000282` → 4 位显示 `$0.0003`**。想确认精确值：悬停合计可看到 6 位（`RequestDetailPanel.tsx:221` 的 `moneyTitle(..., 6)`），或直接查库/看网关响应里的 `cost`。

### 3.3 历史请求会不会一起变好

- **新请求**：从下一次会话同步起按新价计算（启动时一轮、之后每 10 分钟一轮，`src-tauri/src/lib.rs:1317-1342`）；
- **历史请求**：只有 `backfill_missing_usage_costs*` 才会重算，且只补 `total_cost == 0` 的行（同上 `:1323-1337`，首轮 `backfill: true`）。触发点有四个：文件同步时 `upserted > 0`（`services/model_pricing.rs:309-313`）、界面单条改价（仅该模型）、批量改价（全表）、启动首轮会话同步；
- 想立刻看到历史行变化：就走 2.1 的「重新加载文件」（sync → backfill 一步到位）。

### 3.4 常见报错与处置

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| 「重新加载文件」报错 | JSON 语法错 / 类型错 | 价格必须是字符串；`version` 不得大于 1；`modelId`、`displayName` 不得为空（`normalize_file`、`normalize_pricing`） |
| 价格被改成 0 | 传了负数或不可解析的值 | `normalize_decimal` 会直接拒绝并整份加载失败，不会静默归零 |
| 只有部分模型有价 | 导出时策略/快照集合不同 | 用 `cc_switch_pricing()` 重新导出，别手工拼接 |
| 加载成功但历史行没变 | 那几行 `total_cost` 本来就不为 0 | 属正常；backfill 只补 0 值行 |

## 4. 已知限制（为什么 cc-switch 的数字不等于账单）

1. **分档价只能导一档**。cc-switch 一个模型只有一组静态价（`ModelPricing` 四个字段），而本仓库 13 个模型带 `bands`。导出取 `flat` 基准价（无 `flat` 时取 `bands[0]`），于是：
   ```json
   "gpt-6-astra": { "flat": { "input_cache_hit": 1.0, "input_cache_miss": 10.0, "output": 50.0 },
                    "bands": [ { "min_prompt_tokens": 272000,
                                 "flat": { "input_cache_hit": 2.0, "input_cache_miss": 20.0, "output": 75.0 } } ] }
   ```
   超过 272k prompt 的请求在 cc-switch 里会**按基准价低估**。权威金额仍看网关的 `cost_details`。
2. **只导价格、不导口径**。cc-switch 按 `app_type` 决定 `input_tokens` 是否已含缓存读（`calculator.rs:56-70` → `is_cache_inclusive_app`：codex / gemini / grokbuild 为「含」）。DSH 的 `app_type` 是 `"dsh"`，不在名单里 → 它把 `input_tokens` 当 fresh input，缓存读另计。若哪天上游把 DSH 的 usage 改成「包含缓存」，**改价救不了口径**，需要动 cc-switch 的名单。
3. **缓存写入价恒 0**。上游不报 cache write tokens，所以导出 `cacheCreationCostPerMillion: "0"`，与响应侧 `cache_write` 恒 0 一致；上游哪天开始计费，这里要同步改。
4. **覆盖语义的副作用**（详见 2.1）：整表覆盖会让内置价格变成 override；`modelsDevSync` 两项会被写成 Rust 默认的 `true`（自己关过同步的要再关一次）；墓碑被清空。
5. **单位与格式**。值一律是「每百万 token 的 USD 十进制字符串」。Rust 侧是 `String` 字段 + `Decimal::from_str`（`model_pricing.rs:35-38`、`:106-122`），写成 JSON 数字能反序列化失败、写成 `1e-3` 会被判非法。
6. **契约措辞与源码的差异**：契约 F 把这几项描述为「每百万 USD 的数值」，而源码里是 `String`。以源码为准——导出必须是字符串（`"0.28"`，不是 `0.28`）。

## 5. 附录 A：字段映射与源码原文

| Rust 字段（`#[serde(rename_all = "camelCase")]`） | JSON 键 | 含义 |
| --- | --- | --- |
| `model_id` | `modelId` | 必须与 cc-switch 见到的 model 原文一致 |
| `display_name` | `displayName` | 非空；界面显示名 |
| `input_cost_per_million` | `inputCostPerMillion` | 未命中缓存的输入价（USD / 每百万 tokens） |
| `output_cost_per_million` | `outputCostPerMillion` | 输出价 |
| `cache_read_cost_per_million` | `cacheReadCostPerMillion` | 命中缓存读价 |
| `cache_creation_cost_per_million` | `cacheCreationCostPerMillion` | 缓存写入价（本仓库恒 `"0"`） |

```rust
// cc-switch-pr7356/src-tauri/src/services/model_pricing.rs:30-39
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelPricingInfo {
    pub model_id: String,
    pub display_name: String,
    pub input_cost_per_million: String,
    pub output_cost_per_million: String,
    pub cache_read_cost_per_million: String,
    pub cache_creation_cost_per_million: String,
}
```

```rust
// cc-switch-pr7356/src-tauri/src/services/model_pricing.rs:71-82
struct ModelPricingFile {
    #[serde(default = "default_file_version")]
    version: u32,
    #[serde(default)]
    models_dev_sync: ModelsDevSyncConfig,
    #[serde(default)]
    models: Vec<ModelPricingInfo>,
    #[serde(default)]
    deleted_model_ids: Vec<String>,
}
```

`ModelsDevSyncConfig`（同文件 `:41-56`，camelCase）共六个键：`autoSyncEnabled`、`includeCommonModels`、`selectedModelKeys`、`excludedCommonModelKeys`、`lastSyncAt`、`lastSyncError`。前两个带 `#[serde(default = "default_true")]`——**文件里省略这两个键时它们读回来就是 `true`**（`:22-24` 的 `default_true`、`:58-69` 的 `Default`）。所以导出显式写 `true` / `true`：这是「与 Rust 默认一致」的取值，既不会因为缺键而在不同版本上读成两个值，也不会替用户静默关掉 models.dev 同步；写 `false` 反而是替所有导入者改设置。

## 6. 附录 B：本仓库侧的导出实现

`wb_pricing.cc_switch_pricing()`（`wb_pricing.py`，函数名稳定、行号会漂）负责组装整份文件，要点：

- 模型集合 = `pricing/pricing.json` 快照 ∪ 当前生效策略（策略命中时以策略价为准），按 `model_id` 排序；
- 每个条目固定六个键，`displayName` 取条目的 `display`，缺失时回落 `model_id`；
- 单价换算 `_usd_per_million()`：取 `flat`；`entry["bands"]` 存在而 `flat` 缺失时取 `bands[0]["flat"]`；仅当条目 `currency == "CNY"` 时按 `meta.usd_cny` 折算，`USD` 条目原样输出；折算后再按 `entry["unit"]`（默认 `1000000`）归一化到「每百万」——历史数据或手写策略若用了别的单位，不会被放大或缩小一百万倍；
- `inputCostPerMillion` 用 `input_cache_miss`；`cacheReadCostPerMillion` 取 `input_cache_hit`，缺失时**回落 `input_cache_miss`**（宁可高估，也不能把缓存读算成免费）；
- `_decimal_string()`：定点格式化后去掉尾零与小数点，`<= 0` 一律输出 `"0"`；
- `modelsDevSync` 固定 `autoSyncEnabled: true` / `includeCommonModels: true` / `selectedModelKeys: []` / `excludedCommonModelKeys: []` / `lastSyncAt: null` / `lastSyncError: null`，`deletedModelIds` 固定 `[]`（true/true 是 Rust `ModelsDevSyncConfig::default()` 的取值，理由见 §2.1 第 2 条与 §4 第 4 条）。

快速自查：

```powershell
python -c "import wb_pricing; d=wb_pricing.cc_switch_pricing(); print(sorted(d)); print(len(d['models'])); print(d['modelsDevSync'])"
```
