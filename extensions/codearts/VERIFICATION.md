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
currently exclude six selected IDs as unregistered; retaining them is a user
selection, not proof of callability. No sampling parameters or original Hub
files were changed. Original 8788 remains untouched.

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
