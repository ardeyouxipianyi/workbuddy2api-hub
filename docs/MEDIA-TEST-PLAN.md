# 媒体能力（生图 / 生视频）测试方案

> 适用：`feat/media` 分支。基线 main @ `a797c1d`。
> 这份文档回答三件事：**已经测了什么、还缺什么、缺的部分怎么补**。
> 与 `docs/MEDIA-PLAN.md`（决策）、`docs/MEDIA-IMPL.md`（实现细节）配套。

## 1. 原则

| 原则 | 为什么 |
| --- | --- |
| **每一层用自己的诚实手段** | 纯逻辑用单测、出站用替身、路由用假实例、上游只能真打。混着来就会出现「测了一层，以为测了全部」 |
| **花钱的验证不进 CI** | 一次视频 57–129 积分，CI 跑不起也不该跑。真实链路归"一次性验收"，写进 PR 的验证段 |
| **断言必须能失败** | 反过来想这条断言怎样才会红。红不了的就是装饰（§10 有清单） |
| **失败的证据要留存** | 真实链路的验收要贴 HTTP 码、`credit`、产物字节数与魔数——不是"我试过了" |
| **默认关 = 也要测开** | 开关默认关，所以"关"的路径反而最常被走到；"开"的路径只在该开时才走到 |

## 2. 分层矩阵

| 层 | 手段 | 能证明什么 | 不能证明什么 | 进 CI |
| --- | --- | --- | --- | --- |
| **L0 纯逻辑** | `tools`：直接调 `wb_media` 函数 | 目录投影、请求体分支、参数校验、响应解析、状态映射 | 任何网络行为 | ✅ |
| **L1 出站替身** | `_test_media_client`：`urlopen` 与 DNS 全替身 | 端点、头、错误分类、轮询状态机、输入图归一、SSRF 判据 | 上游真实接受我们的请求体 | ✅ |
| **L2 路由替身** | `_test_media_routes`：`Handler` 子类不碰 socket | 开关、参数闸门、选号/换号、预算预检、记账、错误码映射 | 真实分发（白名单、体上限、鉴权顺序） | ✅ |
| **L2.5 任务表** | `_test_media_tasks`：同步驱动 worker | 状态机、落盘、签名、清理、重启语义 | 真实线程调度 | ✅ |
| **L3 真实实例** | 起 8799，打真实 HTTP | 分发、状态码与响应头、413、并发槽、鉴权顺序 | 上游是否接受 | ✅（不花钱） |
| **L4 真实上游** | 真账号直打 | 协议真的对、`credit` 真的扣、产物真的能用 | 长尾场景 | ❌ 一次性验收 |

### 现有覆盖（207 条）

| 套件 | 条数 | 层 |
| --- | --- | --- |
| `_test_media_logic.py` | 103 | L0 |
| `_test_media_headers.py` | 15 | L1 |
| `_test_media_client.py` | 29 | L1 |
| `_test_media_routes.py` | 30 | L2 |
| `_test_media_tasks.py` | 30 | L2.5 |

**L3 与 L4 目前是 0 条。** 这是本文档要解决的主要缺口。

## 3. 缺口清单（按风险排序）

| # | 缺口 | 风险 | 补法 |
| --- | --- | --- | --- |
| **G1** | **路由分发未测**：`_test_media_routes` 用 Handler 替身，绕过了 `do_POST` 的白名单、`_payload_or_error_for_media`、鉴权顺序 | 高：白名单漏一条就 404，而替身测试全绿 | L3 |
| **G2** | **体上限 413 未测**：24 MB 那把尺子从没被执行过 | 高：改图传大图会得到一个指不出原因的 413 | L3 |
| **G3** | **鉴权顺序未测**：无 key → 401 该在参数校验之前 | 中：顺序错了会泄露"参数错在哪"给未授权者 | L3 |
| **G4** | **并发槽未真实并发**：只测了 `acquire_media_slot()` 返回 False 的分支 | 中：真并发下槽会不会泄漏 | L3 多线程 |
| **G5** | **worker 真实线程从未跑过**：所有用例 `autostart=False` | 中：`ensure_worker()` / `stop()` 只有代码可读性保证 | L3 |
| **G6** | **与 chat 的隔离没有端到端证据**：只测了「`note_error(model=…)` 不动账号级冷却」这个单点 | 高：这是整个设计最大的承诺 | L2 补 + L3 |
| **G7** | **幂等未实现**：客户端超时重发 = 两张图两笔钱 | 高（钱） | 实现后再测，或写进文档声明"不幂等" |
| **G8** | **内容下载路由未实现**（`/v1/videos/{id}/content`） | — | commit 6 |
| **G9** | **面板 UI 未做** | — | commit 7/8 |
| **G10** | **真实上游未验**（P0 取证） | 最高：桌面身份到底通不通，决定 `media_identity` 默认值 | L4 |
| **G11** | 幂等之外的重放：同一 `Idempotency-Key` 并发两次 | 中 | 实现后再测 |

## 4. L2 补强：三条设计承诺的单点证据

这三条现在只有代码注释承诺，没有断言。补在 `_test_media_routes.py` / `_test_media_tasks.py`：

```python
# C1 媒体失败不污染 chat：同一个账号，画图失败后 chat 仍可用
account.note_error("boom", cooldown=60, model="hunyuan-image-alpha")
assert account.ready(model="glm-5.3") is True           # chat 不受影响
assert account.ready(model="hunyuan-image-alpha") is False

# C2 媒体不占 chat 的并发槽：媒体满了，chat 的 _CHAT_SLOTS 一个没动
#    （用 proxy_slots_view() 前后对比，而不是读信号量内部值）
before = P.proxy_slots_view()
… 塞满媒体槽 …
assert P.proxy_slots_view()["chat"] == before["chat"]

# C3 换号上限与 chat 同量级：媒体 3 次，且失败后错误里说明试过几个账号
```

## 5. L3：真实实例测试方案

**目的**：证明 `do_POST` / `do_GET` 的分发真的把请求送到了媒体层，且 HTTP 语义正确。
**关键前提**：这一层**不需要真账号**——下面所有用例都在"上游之前"就被拦下。

### 5.1 起实例

```bash
# 8788 被占（本机常驻实例），用隔离端口；数据目录也要隔离，别碰真账号
python tests/_test_media_http.py       # 自带 free_port + 临时 ACCOUNTS_DIR
```

测试内部：

```python
_TMP = isolated_data_dirs("wb-media-http-")     # tests/_isolated_dirs.py
os.environ["WB_MEDIA_ENABLED"] = "1"            # 只覆盖总闸，不覆盖限额
proc = spawn_managed([sys.executable, "wb_proxy.py", "--port", str(port)], ...)
# 轮询 /health 直到 ok
```

### 5.2 用例矩阵（全部零成本）

| # | 请求 | 期望 | 证明什么 |
| --- | --- | --- | --- |
| H1 | `POST /v1/images/generations` 无 key（配置了 key 时） | **401** 且 body 是 OpenAI 错误信封 | 鉴权在参数校验之前 |
| H2 | 同上，带 key，`media_enabled=false` | **404** | 关着就是不存在 |
| H3 | `WB_MEDIA_ENABLED=1`，body `{}` | **400** 且提到 `prompt` | 参数闸门 |
| H4 | body `{"prompt":"x","size":"4096x4096"}` | **400** 且提到 `4.19 MP` | 尺寸本地拦 |
| H5 | body `{"prompt":"x","n":4}` | **400** 且提到「一次生成一张图」 | 批量拒绝 |
| H6 | body `{"prompt":"x","image":"/etc/passwd"}`（edits） | **400** 且提到「服务器」 | 本地路径拒绝（G3 的安全面） |
| H7 | body `{"prompt":"x","image":"http://127.0.0.1:8788/accounts"}` | **400** 且提到「非公网地址」 | SSRF 拒绝 |
| H8 | **26 MB 的 body** | **413** 且 `payload too large` | G2 体上限真的生效 |
| H9 | 16 MB 的 body（通用上限内、媒体上限内） | 不是 413 | 证明放宽生效 |
| H10 | `GET /v1/media/models` 带 key | **200**，`models[]` 非空或明确说该区没有 | 目录端点连通 |
| H11 | `GET /v1/models` 带 key | **200** 且**不含** `hunyuan-image-alpha` | 媒体模型不污染聊天清单 |
| H12 | `GET /v1/media/models` 无 key | **401** | 目录也要鉴权 |
| H13 | `GET /v1/images/generations`（GET 打 POST 路由） | **404** | 方法不匹配不进媒体层 |
| H14 | 媒体路由带合法 key 但**未开**，随后 `/health` | `/health` 里有 `media.enabled=false`、`slots`、`inflight` | health 字段连通 |

### 5.3 并发用例（G4）

```python
# 4 个槽，同时发 6 个"会卡住"的请求
# 用 monkeypatch 把 wb_media_client.generate_image 换成 sleep(2) 的假函数——
# 这需要实例内可控，所以做成环境变量钩子：WB_MEDIA_TEST_HANG_SECONDS
# 期望：4 个 200/502，2 个 503 且带 "media slots busy"
#      并发期间 /health 的 inflight 峰值 == 4；结束后回 0（槽不泄漏）
```

> **需要一处实现改动**：为了可测并发，加一个**只在 `WB_MEDIA_SELFTEST=1` 时生效**的
> 钩子，把出站调用换成本地 sleep。生产路径一个字不变，测试里却能真的把槽占满。
> 这比"读信号量私有字段"可靠，也比"起 6 个真请求"省积分。

### 5.4 worker 真实线程用例（G5）

同一实例内提交一个视频任务（不花钱的路径：**参数越界**让上游在提交前拒），
断言任务出现在 `GET /v1/media/tasks` 里且状态是 `rejected` → 证明任务表被真实创建。
不跑"成功"的轮询（那要花钱），成功路径归 L4。

## 6. L4：真实上游验收（一次性，明确成本）

**只有这一层能证明协议是对的。** 不进 CI，由人在 `feat/media` 分支上跑一次，结果贴进 PR。

### 6.1 成本预算

| # | 项目 | 预估成本 | 说明 |
| --- | --- | --- | --- |
| V1 | 文生图 `1024x1024` | **5.71** | 核心：桌面身份通不通 |
| V2 | 改图（用 V1 产物做输入） | **5.71** | 证明 `/v2/images/edits` 与 data URL 归一 |
| V3 | 视频 `seconds=1` | **0** | 上游提交前拒绝 → 验 `REJECTED` 与文案解析 |
| V4 | 文生视频 4 秒 | **57.14** | 证明提交/轮询/下载全链 |
| | **合计** | **≈ 68.6 积分** | 可只跑 V1 + V3 压到 5.71 |

### 6.2 每项要记录的证据

```
V1   HTTP 200 · credit 5.71 · data[0].url 的桶名 · 下载后 PNG 魔数 89 50 4E 47 · 实际宽高
V2   HTTP 200 · credit · 产物与输入的差异（人工看一眼）
V3   HTTP 400 · code 14407 · 文案里的 supported range · 确认「未扣积分」提示出现
V4   HTTP 202 · task id · 轮询次数与耗时 · completed 时的 credit · mp4 的 ftyp 魔数
     · moov 解析出的真实时长/分辨率（请求 4 秒应得 4.0–4.5 秒）
```

### 6.3 验收脚本（`scripts/probe_media.py`）

```
python scripts/probe_media.py --image            # V1
python scripts/probe_media.py --image-edit FILE  # V2
python scripts/probe_media.py --video-reject     # V3（零成本）
python scripts/probe_media.py --video 4          # V4
python scripts/probe_media.py --all --yes        # 明确提示预估成本后开跑
```

实现要点（已落地）：
- 默认 dry-run，不加 `--yes` 不花钱，且会先打印预估消耗
- 每次打印 `credit` 与累计消耗
- 只读 `wb_accounts` 的凭据，不落盘、不打印 token
- 结论写成 JSON（贴进 PR）

## 7. 安全专项

| # | 用例 | 断言 |
| --- | --- | --- |
| S1 | `image` = `/etc/passwd`、`C:\\…\\id_rsa`、`\\\\server\\share\\x` | 400，且错误里说明「会把服务器文件发给上游」 |
| S2 | `image` = `file:///etc/passwd` | 400 |
| S3 | `image` = `http://127.0.0.1`、`10.x`、`192.168.x`、`169.254.169.254`、`[::1]`、`0.0.0.0` | 全部 400（L1 已覆盖，L3 抽两条端到端） |
| S4 | `image` = 公网 URL → 302 到 `http://127.0.0.1:8788/accounts` | 400，**不跟随** |
| S5 | API Key 绑定账号 {B}，池里有 A/B | 只派 B；B 不可用时 503 且报错只列 B |
| S6 | 钉住账号 A，A 冷却 | 503 且说明是钉住的号不可用，**不静默换号** |
| S7 | 403 + code 11140 | 不换号、整池暂停、后续请求 429 且上游零调用 |
| S8 | 日志与 usage 行 | 不含 prompt 全文、不含 token、不含 data URL |
| S9 | `media/out` 目录 | 不在仓库清单里、被 .gitignore、不被任何静态路由暴露 |

## 8. 钱相关专项

| # | 用例 | 断言 |
| --- | --- | --- |
| M1 | 缓存余额 < 预估 | 该账号不派发；全部不足时 402 并列出「谁差多少」 |
| M2 | 日限额余量 < 预估 | 同上（这条防的是「媒体顶穿额度连累 chat」） |
| M3 | 余额未知（`credits=None`） | **不拦**（与 `reserve_blocked` 同口径） |
| M4 | 队列满 | 429，且**在提交之前**拦下（不花钱） |
| M5 | 上游 400（提交阶段） | 400 + `REJECTED`，**账号不冷却**，可立刻重试 |
| M6 | 上游 5xx | 502 + 冷却 60s，且冷却只作用于该媒体模型 |
| M7 | 每 key 每分钟次数 | 超限 429 + `Retry-After`；跨分钟重新计数；`0` 表示不限 |
| M8 | 重发同一请求（无 `Idempotency-Key`） | **会扣两次**——这条现在应当**红**，作为已知缺口登记在 PR 里 |

## 9. 并发与时序

| # | 用例 | 断言 |
| --- | --- | --- |
| T1 | 6 并发、4 槽 | 2 个 503，且 `inflight` 峰值 4、事后回 0 |
| T2 | 媒体跑满时发 chat | chat 正常返回（媒体不占 chat 槽） |
| T3 | worker 处理中途 `stop()` | 任务保持 `running`，下次启动变 `lost` |
| T4 | 单条任务抛任意异常 | worker 线程存活，后续任务继续被处理 |
| T5 | 任务表并发读写 | 100 次 create + list 无异常、无丢任务（用 4 线程压） |

## 10. 变异测试清单（证明断言会红）

PR 里逐条说明"改了哪里会红"：

| 变异 | 应当变红 |
| --- | --- |
| `media_models()` 的默认 `tag=None` 改成「不过滤」 | L0 的 `test_reads_data_models_from_full_envelope`（这个 bug 真出现过） |
| `_media_note_failure()` 去掉 `model=` 参数 | C1（媒体失败污染 chat） |
| `REJECTED` 加进 `MEDIA_RETRYABLE` | `test_rejected_does_not_rotate` |
| 11140 分支去掉 `restriction_is_account_level` 判断 | `test_restriction_does_not_rotate`（这个 bug 真出现过） |
| `media_budget_ok()` 去掉日限额那段 | `test_daily_headroom_is_checked` |
| `classify_image_input` 接受本地路径 | `test_rejects_local_absolute_paths` + S1 |
| `assert_public_url` 去掉"不跟随重定向" | S4 |
| `record_usage` 不传 `kind` | `test_records_usage_with_kind_and_credit` |
| `_media_error` 把 `NO_CREDIT` 映射成 400 | `ErrorMappingTests.test_status_codes` |
| worker 里 `_finish` 不写 `videos` | `test_download_failure_keeps_upstream_urls` |

## 11. 发布门禁（PR 前逐条打勾）

```bash
# 1) 全量套件（本机已知两个 SSL 基线失败，见下）
python tests/run_all.py --jobs 4
#    期望：249 套件；本机 247 passed / 2 failed
#    2 个失败 = _test_shared_ssl_context / _test_upstream_pool，
#    本机自签证书导致，基线 a797c1d 同样失败（已用 worktree 复核）

# 2) 干净检出（套件不许写脏仓库）
python scripts/check_clean_checkout.py

# 3) 套件计数与清单
python tests/_test_release_engineering.py      # README 套件数 == 实际
python tests/_test_release_assets.py           # portable.txt 列全 wb_*.py

# 4) 新套件单跑
python tests/run_all.py media
#    期望：全部 PASS

# 5) Python 地板
py -3.8 tests/run_all.py media                  # 3.9 只会更宽松

# 6) L3 真实实例（不花钱）
python tests/_test_media_http.py

# 7) L4 真实上游（一次性，人工确认成本后）
python scripts/probe_media.py --image --yes     # 5.71 积分
```

任一红：先修再提交，不以「CI 会跑」为由跳过（CONTRIBUTING §1 硬性规则）。

## 12. 实施顺序（与 commit 对应）

| 阶段 | 补什么 | 依赖 |
| --- | --- | --- |
| 当前 | L0 / L1 / L2 / L2.5 —— 207 条 | 已完成 |
| commit 6 之后 | §4 的 C1–C3 三条单点证据、L3 的 H1–H14 | 视频路由接线 |
| commit 6 之后 | §5.3 并发（含 `WB_MEDIA_SELFTEST` 钩子）、§5.4 worker | 同上 |
| commit 7/8 之后 | UI 侧：`_test_media_page.js`（容器/状态色/无 HMAC/开关 payload） | 面板 |
| 提 PR 前 | §6 的 V1–V4（人工跑一次，结果贴 PR） | 真账号 |

## 13. 已知缺口登记（写进 PR 正文，不藏着）

1. **不幂等**：客户端超时重发会扣两次费（M8）。本期不实现 `Idempotency-Key`，在
   README 的接口小节里明确写「本接口不幂等，请勿自动重试」。
2. **上游无 cancel 接口**：`/v1/videos/{id}/cancel` 只能停本地轮询，任务照样跑完照样计费。
3. **4K / 2K 档位未验证**：目录写着，实产可能降级。文档与 UI 一律写「以实际产物为准」。
4. **intl 区未取证**：本机没有 intl 账号，`media_realm` 默认 cn；intl 走同一套代码但没有实测记录。
5. **`minimax` 的 `audioMode: always-on`**：`enable_audio=false` 很可能被忽略，已在 warnings 里如实提示而非假装生效。
6. **重启会丢进行中任务**（标 `lost`）：上游已扣费但本地没有产物路径，只能靠面板显示出来人工核对。
