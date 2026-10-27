# Verification / 验证记录

日期 / Date: 2026-10-23 (Asia/Taipei)

基线 / Base: `84dbc99b1a34182130ee9035f9d80b69a0fc2e8b`, fork `main`.
所有交付文件均为新增；未修改任何已有 tracked 文件。

| Check | Result | Boundary |
|---|---|---|
| `python -m unittest discover -s extensions/codearts/tests -v` | PASS: 38 tests | Real loopback Hub listener, controlled HTTP/SSE backend and child processes; not Huawei inference acceptance |
| `python tests/run_all.py --jobs 4` | PASS: 222 suites, 0 failed, 0 skipped | Existing suite inventory and assertions unchanged |
| `node --check extensions/codearts/dashboard.js` | PASS | Syntax only |
| `node extensions/codearts/tests/test_login.js` | PASS | Popup opens before asynchronous OAuth; blocked popup fallback, unsafe URL rejection and error cleanup |
| Python compilation of extension and launcher | PASS | Syntax only |
| Native panel login, settings save, secret preservation | PASS | Real browser, isolated instance on 8799 |
| CodeArts panel model list and test reply `bridge-ok` | PASS | Controlled backend; no real Huawei account |
| CodeArts navigation reload/deep-link and scoped scrolling | PASS | Real browser; native async initialization and bounded-card scrolling checked |
| Browser error/warning console after tested flow | PASS: none observed | Tested flow only |
| Gateway → real WorkBuddy upstream | PASS: HTTP 200, reply `OK`, 17 tokens | `deepseek-v4.1-flash`, isolated extension launcher, about 3 seconds |
| Real Huawei authorization page opens from login button | PASS | Real local CodeArts service, popup automatically reached auth.huaweicloud.com login form; no credentials submitted |
| Native gateway model list | PASS | Real browser: WorkBuddy models and canonical `codeart/` names together on one listener |
| Prefix stripping | PASS | Wire tests verify backend receives exact original `GLM-5.2` for both `codeart/` and legacy `codearts/` |
| Real Huawei account login/model invocation | PASS for GLM-5.3 | Human login persisted; corrected 8799 gateway returned HTTP 200 / OK, 57 tokens, 2.53 s. Other selected models were not individually validated in this follow-up |
| Go backend build/test | PASS | Portable official Go 1.27.3, official archive SHA verified; `go test ./...` and real server build succeed; backend source unchanged |
| Owned backend process lifecycle | PASS | Child-process test: even abrupt parent os._exit closes the Windows Job and backend port; verified-service reuse retains external ownership |

## Regression evidence

Initial bridge tests failed before implementation because the bridge modules did
not exist. Independent review then reproduced three additional protocol breaks:
streaming Responses iteration, CodeArts SSE errors misreported as successful
empty replies, and flat Responses function declarations/named choices forwarded
without the required Chat schema. Each gained a regression test: all three were
observed failing together, then passed after the stream and protocol fixes.

Additional passing cases include key authentication, model/account/realm
restrictions, pinned routes, key concurrency, concurrent provider attribution,
per-key conversation identifiers, redirect credential isolation, disabled/offline
backends, server-side management keys and cross-origin mutation rejection.

An independent read-only reviewer reran 27 tests and additional probes for
fragmented UTF-8/CRLF SSE, delayed errors, missing terminal markers, final markers
without a newline, namespace tools and streaming Responses. No Critical or
Important finding remained after fixes. Legacy `functions`/`function_call`
compatibility is explicitly outside the documented modern-tools contract.

The login/port follow-up added canonical prefix and picker wire tests, popup
behavior tests, and process ownership tests. A second review reproduced a native
panel-shutdown child-process leak (native shutdown calls os._exit and skips
finally). The managed launcher now binds owned Windows children to a kill-on-close
Job Object and hooks native hard-exit cleanup; reused services are never assigned
to that job. The abrupt-exit regression test failed before this fix and passed
afterwards. Real WorkBuddy smoke was repeated: HTTP 200, reply OK, 17 tokens,
about 1.3 seconds. Native 222 suites passed again.

Final independent review reran all 37 extension tests and the original native
shutdown probe: the owned child port closed after the parent exited. A separate
cross-process reuse probe confirmed that shutting down a second Hub left the
existing external backend alive. No Critical or Important finding remained.

## Environment and isolation

The first unmodified native-suite run had 220 passing suites and two TLS-related
failures. The inherited Conda `SSL_CERT_FILE` overrode the tests' TLS factory.
Removing that variable **only from the test subprocess environment** made the two
focused suites pass (29 and 89 checks), then the full 222-suite run passed.
No system environment, certificate policy or original source was changed.

The original running 8788 service was left in place. Live WorkBuddy smoke used a
separate runtime directory and a credential copy with its refresh token removed,
so it could not rotate the active service's refresh credential. Test logs,
credentials, screenshots and fixture processes are not part of the commit.

These checks establish the tested bridge mechanisms and WorkBuddy regression
boundary. They do not establish real Huawei account availability or every model's
capabilities, quotas or pricing.

## Exact catalog selection / 精确目录保留

2026-10-23: 38 extension tests and 222 native suites pass. Real isolated browser
login and seven selected catalog IDs verified on 8800, then applied to 8799.
A real WorkBuddy request returned HTTP 200 / OK. Independent review confirmed
exact selection, cache invalidation and missing-model labels. Backend probes
initially misclassified six selected display names as unregistered. The
following registered-model-ID repair and live connectivity run supersede that
catalog-only result. No sampling parameters or original Hub files were changed.
Original 8788 remains untouched.

## Registered model ID repair / 真实模型 ID 修复

2026-10-23: the authenticated catalog declares display name GLM-5.3 but
model_parameters.model_id=glm-5.3. The old backend selected the display name;
uppercase requests failed as unregistered. A minimal direct lowercase request
returned real reasoning/usage (16-token budget, finish=length, no final content).
The corrected full gateway request then returned OK through 8800 (38 tokens,
2.41 s) and deployed 8799 (57 tokens, 2.53 s). The same client-facing
codeart/GLM-5.3 name is retained. This corrects the earlier account-unavailable
interpretation; it did not establish that the account lacked GLM-5.3 access.

Build-time Go overlays preserve the external source checkout. Resolution uses
only the selected account's catalog, gives exact IDs priority and leaves ambiguous
names unchanged. Cold managed requests discover the catalog even without an
initial /v1/models request. Probes and benefit headers resolve the same ID.
Embedded handlers without an auth directory retain their existing discovery flow.
The Hub log's upstream_model remains the name sent to the CodeArts backend;
that backend can then resolve its display alias to the real Huawei ID.

The new Go contract failed against unpatched source and passed with the overlay.
The disposable-copy runner passed all external Go packages. Python extension
38 tests and native Hub 222 suites passed; real WorkBuddy still returned OK.
Independent review additionally tested account collisions, exact-ID priority,
ambiguous aliases, missing-account catalogs, build guards and a real httptest
outbound benefit header. No Critical/Important issue remained after fixes.

## Remaining selected models / 其余已选模型连通性

2026-10-23: after the account-scoped model-ID overlay was deployed to isolated
8799, each of the other six selected models received one non-streaming text
chat request through the Hub gateway. All returned HTTP 200, a non-empty `OK`
reply and normal completion. The display-name aliases resolved to these IDs:

| Hub model | CodeArts model ID | Result | Seconds |
|---|---|---|---:|
| `codeart/GLM-5.2` | `GLM-5.2` | HTTP 200 / OK | 7.03 |
| `codeart/OpenPangu-2.0-Flash` | `openpangu-2.0-flash` | HTTP 200 / OK | 36.62 |
| `codeart/OpenPangu-2.0-Pro` | `openpangu-2.0-pro` | HTTP 200 / OK | 35.03 |
| `codeart/GLM-5.2-ArkTS-SPARK` | `glm-5.2-sft-harmony` | HTTP 200 / OK | 6.70 |
| `codeart/DeepSeek-V4.1-Flash` | `deepseek-v4.1-flash` | HTTP 200 / OK | 21.20 |
| `codeart/Kimi-K3` | `kimi-k3` | HTTP 200 / OK | 23.95 |

Together with the GLM-5.3 result above, all seven selected models have passed
one real basic text-chat request. This run does not establish streaming, tools,
image input, concurrency, or sustained availability for every model.

## Native dashboard alignment / 原生面板对齐（本地待验收）

2026-10-23: native Hub regression suites passed 222/222; Python extension tests
passed 38/38. The Node component test failed on the old account-table structure,
then passed for native tables, inert metadata, edits during load/save, retained
model selection and test result, partial failures, language changes without
network calls, and disabled state. The existing OAuth popup test also passed.
These are component/controlled-HTTP checks, not visual browser acceptance.

The browser tool explicitly denied access to 8799. No alternate browser surface,
raw CDP, or indirect renderer was used. Visual checks of desktop/mobile layouts,
themes, languages and browser console remain NOT_RUN. Per CONTRIBUTING.md, this
UI change must not be committed/pushed until the browser gate is satisfied.
No fresh real Huawei/WorkBuddy inference was made for this presentation-only
change; the previous model connectivity results above describe earlier code.

The independent review found and fixed two P2 races, each reproduced RED then
verified GREEN: a delayed settings read overwrote a successful save, and failed
reads after a backend change retained actionable accounts from the old backend.
Successful saves now invalidate older read generations. A changed backend clears
cached data; failed account refreshes leave cached rows read-only. Independent
review additionally verified same-URL credential changes with delayed responses.
No Important issue remained. Node component and OAuth tests passed after fixes.

The updated extension was loaded into isolated 8799 and health returned HTTP 200.
Original 8788 also returned HTTP 200 and its process creation time was unchanged.
Credentials were retained. Real-page acceptance has been requested from the user;
this is pending, not PASS. The UI work remains local and uncommitted until that
gate and the real WorkBuddy request required by CONTRIBUTING are satisfied.


## 2026-10-26：上游合并与图片输入 / Upstream merge and image input

集成分支 `codex/codearts-upstream-20261026`：原桥接基线
`03486330c640391d3cdc8747780b0e5c17450c51` 合并上游 main
`4f7ae6f2efcae5e6ae85aefa732035206044514b`（v1.8.0，20 个新增提交）。
文本合并无冲突；原工作副本 10 个未提交 UI/文档文件已完整备份，原副本内容未改。
扩展外的既有原生文件必须与上述 upstream 完全相同，不增加对原生源文件的修改。
Integration uses the original bridge plus upstream v1.8.0. All native files remain
identical to upstream; the original dirty UI checkout is preserved separately.

| 检查 / Check | 结果 / Result |
|---|---|
| Native `python tests/run_all.py --jobs 4` | PASS: 226 passed, 0 failed, 0 skipped (69.9 s) |
| Extension `python -m unittest discover -s extensions/codearts/tests -v` | PASS: 44 tests |
| Node dashboard component + OAuth login + syntax | PASS |
| Go disposable source copy, `go test -overlay ... -count=1 ./...` | PASS; original 4 external source hashes unchanged |
| Authenticated current catalog | PASS: original 7 retained, 2 explicitly image-capable IDs added |
| Isolated panel login/logout API | PASS: HTTP 200; this is not browser/visual acceptance |
| Real gateway → WorkBuddy CN `glm-5.3` | PASS: HTTP 200, `OK` (2.39 s on final image build) |
| Real browser loading/console/themes/mobile | PENDING USER CHECK; tool access denied, no workaround used |
| Clean-checkout guard / commit / remote sync | PENDING browser gate and completion of merge commit |

当前账号目录中的两个新增真实 ID 为 `Qwen3-VL-235B`、`kimi-k2.6-vl`，
`supports_images=true`；原有 7 个条目均明确返回 false。两个新增模型的上下文/最大输出
未由当前动态目录给出，因此保持未知，不沿用静态种子的猜测值。
Current authenticated catalog declares exactly these two extra image models. Missing
limits remain absent. Catalog declarations do not establish sustained availability.

真实测试经 8799 网关，使用程序生成的 32×32 纯色 PNG data URL 和同一颜色提问，
请求模型保留 `codeart/`，上游仍使用真实 ID；`detail=low`。不涉及用户图片。
Real tests used synthetic PNG images through the gateway, not private user content:

| Model | Red PNG | Blue PNG |
|---|---|---|
| `codeart/Qwen3-VL-235B` | HTTP 200, `Red`, 11.59 s | HTTP 200, `Blue`, 22.66 s |
| `codeart/kimi-k2.6-vl` | HTTP 200, `Red` plus an additional black-region description, 22.80 s | HTTP 200, `Blue`, 22.01 s |

这些仅证明这两个模型的图片请求有返回；不是识图准确率评估。Kimi 的附加描述原样记录，
不隐藏异常。URL 图片、JPEG/WebP/GIF 的真实调用、auto/high 对模型行为的影响、音视频
及图片生成均未验证或不支持。Streaming/Responses/Messages 图片结构由受控链路测试，
未把该结果记成这三类真实模型调用全部通过。
These are bounded connectivity samples, not an accuracy evaluation. Only PNG data
URLs were exercised live. Other formats/remote URLs and detail semantics remain unproven.

独立审查复现并修复：Messages 心跳关闭后丢 terminal error；展示别名碰撞污染真实 ID
能力/限制/benefit；Responses 工具输出中媒体静默丢弃；图片读取期间外部切换后端的
跨连接发送。相关反例已加入测试，最后审查 0 Critical / 0 Important。
Terminal SSE errors, exact-ID metadata, nested tool-output validation and delayed image
reads across backend changes have regression coverage and no remaining important review findings.


### 2026-10-26 补充：目录标志并非模型原厂能力 / Route declaration correction

用户报告选择 DeepSeek-V4.1-Flash 上传图片后，结果区仍显示「尚未发送请求」。
复现为目录 false 的预检在结果错误边界之外抛出，仅显示页顶错误。已将完整发送流程
纳入错误边界，校验和上游错误直接显示在结果框，组件回归 RED→GREEN。

原厂官方资料明确说明 DeepSeek V4.1 Flash 和 Kimi K3 具备多模态能力：
https://api-docs.deepseek.com/zh-cn/news/news260910/
https://github.com/MoonshotAI/Kimi-K3
因此 false 只表述当前目录/通道声明，不再显示为模型本身仅支持文本，也不再拦截实测。
不会偷偷改选模型，原请求 ID 保持不变。

当前账号与实际 CodeArts 端点：这两个精确模型的合成 PNG 请求均收到
`InferHub.001001020.406 The request model is not multimodal`。这是当前通道实测拒绝，
不能外推为模型原厂不支持，也不能推断其他账号或官方客户端行为。未从 wild-work
发现可直接复用的图片路由；其 CodeArts 目录还隐藏 -VL 模型，不能作为图片成功证据。

修复前后端把该请求错误当成账号故障，整个账号冷却后表现为 503；测试产生的冷却已清除。
Go overlay 现在仅将精确错误码 .001001020.406 归入已有请求参数错误分支：回 HTTP 400，
不重试/轮换/冷却整个账号。HTTP、首帧 SSE、晚期 SSE × 流式/非流式 6 类回归均通过，
其他 502 和相似文本仍按原故障逻辑处理。真实复测两个图片拒绝均为 400，账号 healthy=1、cooling=0。

Official model capability, catalog flags and actual provider-route results are different evidence.
The UI now treats a false flag as advisory, forwards the selected model unchanged, and displays
preflight/upstream errors near the send button. Both disputed models are multimodal at the vendor,
but this account's CodeArts endpoint rejected their image requests with the precise code above.
The build overlay prevents this request mismatch from cooling the account. Independent review:
0 Critical / 0 Important. Node components, 44 Python extension tests and full Go suite pass.
Real browser acceptance remains pending; no browser policy workaround was used.


### 2026-10-27 CodeArts IDE 出站头伪装（官方头族）/ Build-time IDE header family

本轮把原来的「仅 `User-Agent` + trace id 补零」扩展为「官方身份头族 + 逐请求 `x-ot-*`」，
默认关、开启时仍只改出站 header。下面每一项都是独立复跑的结果（不采信实现者自述）。

| Check | Result | Boundary |
|---|---|---|
| `python -m compileall -q extensions/codearts start-codearts-bridge.py` | PASS | 语法检查，不代表 Go 能编译 |
| `python -m unittest discover -s extensions/codearts/tests -v` | PASS: 161 tests OK（`test_spoof_overlay.py` 100、`test_bridge.py` 55、`test_backend.py` 6） | 生成源码的文本契约，本身不等于编译通过 |
| `python tests/run_all.py --jobs 4` | 225 passed / 1 failed（`tests/_test_agents.py`） | 该失败与本轮改动无关：在 `git archive HEAD` 的干净树上同样复现，触发条件是 gitignored 的 `accounts/settings.json` 里保存过的 `agent_models` 勾选；本轮未改任何 `wb_*.py` |
| `node --check extensions/codearts/dashboard.js`、`test_login.js`、`test_dashboard.js` | PASS | 语法与组件行为，未开浏览器 |
| 默认关等价（假 `go` 捕获 argv） | PASS | 仍只传 `build-overlay/overlay.json`；不生成 `spoof/`，不生成 `overlay-merged.json` |
| `go build -overlay` 默认关 / 开启态（portable Go 1.27.3，`GOPROXY=off`） | PASS: 两次都 exit 0 | 产物写 `%TEMP%`；构建前后只读源码 75/75 文件哈希一致 |
| `go vet -overlay overlay-merged.json ./internal/upstream/` | PASS | 注入的 helper 通过 vet |
| `gofmt -l` 生成的 `client.go` | PASS: 无输出 | 仅格式 |
| 注入的运行时用例（`ideID` / `ideBase62` / `ideSpoofDynamic`） | PASS: 5 项 | 只验证形态、唯一性与会话派生，不发真实请求 |
| 生成源码里的签名顺序 | PASS | 静态身份头与动态族都在 `signRequest` 之前；`Content-Type`/`Accept`/`User-Agent` 在其后 |
| 真实华为推理 / 独立 mitmproxy 抓包 | NOT RUN | 头名与取值来自官方内核自身日志（`[header-debug]`，1371 条真机记录）；未验证开启后上游行为，也没有独立抓包复核 |

These results establish three things only: the default build is unchanged, the enabled overlay
compiles and vets clean, and the injected headers have the shape and signing position the
evidence describes. They do not establish that the upstream behaves differently, nor any
account or model availability. The documentation therefore says "the local outbound request
carries these headers" and never "the upstream now sees an official IDE". The header names and
values come from the official client's own `[header-debug]` log (1371 records); an independent
mitmproxy capture of the chat endpoint is still outstanding.

## 2026-10-27 本地积分扣除 / Local credit billing

基线 / Base: 当前分支工作区（`7bf7772` 之上，未提交）。倍率取自 CodeArts 模型选择器
（GLM-5.2 0.70、GLM-5.2-ArkTS-SPARK 0.70、OpenPangu-2.0-Pro 0.70、
OpenPangu-2.0-Flash 0.32、GLM-5.3 0.79、DeepSeek-V4.1-Flash 0.18、Kimi-K3 2.38）；
夜间窗口 23:00-08:00（本地时间），窗口内 DeepSeek-V4.1-Flash 0.10。

| Check | Result | Boundary |
|---|---|---|
| `python -m unittest discover -s extensions/codearts/tests -v` | PASS: 199 tests（含新增 `test_credits.py` 38 项） | 纯本地：不发网络、不碰真实上游；覆盖倍率/夜间边界/余额/损坏文件/配置校验/桥接扣费 |
| `node --check extensions/codearts/dashboard.js`、`node extensions/codearts/tests/test_dashboard.js` | PASS | 组件行为：余额表、倍率表、模型行倍率、调账 POST 与积分设置保存；未开真实浏览器 |
| 隔离实例 8799 + 真实 CodeArts 后端（临时 `--codearts-config`，`credits_enabled=true`） | PASS | 余额 0 时 `POST /v1/chat/completions`（`codeart/deepseek-v4.1-flash`）返回 402，消息含 Key 与余额，请求未打到后端 |
| 面板登录 + `GET /codearts/credits` | PASS | 返回 enabled/enforce/night_window、7 条倍率（DeepSeek night = 0.1）、`launcher` 余额 0 |
| `POST /codearts/credits/grant` 充值 100 后再请求 | PASS | HTTP 200，真实回复 `OK`；79 tokens × 0.18 → 扣 `0.0142`，余额 `99.9858` |
| 强制夜间窗口（`credits_night_window=00:00-23:59`）后请求同模型 | PASS | 事件 `multiplier=0.1`、48 tokens → 扣 `0.0048`、`night=true`（未等到真实 23:00，用窗口覆盖验证分支） |
| 工作区体积 | 74.40 MB → 59.79 MB | 只删 gitignored 可再生缓存与测试产物（`__pycache__`、`.pytest_cache`、`.test-artifacts`），未动 `usage/` 与后端二进制 |

这些结果只说明：本地账本的计费口径、夜间分支、余额下限与面板接口按设计工作，
且真实 CodeArts 通道在充值后能正常返回。它**不**说明本地估算与华为账号真实扣减一致
（本地按公布倍率估算，上游账单未在本轮读取），也不覆盖真实 23:00-08:00 时段的连续观测。
