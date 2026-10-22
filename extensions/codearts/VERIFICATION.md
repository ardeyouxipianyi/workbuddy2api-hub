# Verification / 验证记录

日期 / Date: 2026-10-23 (Asia/Taipei)

基线 / Base: `84dbc99b1a34182130ee9035f9d80b69a0fc2e8b`, fork `main`.
所有交付文件均为新增；未修改任何已有 tracked 文件。

| Check | Result | Boundary |
|---|---|---|
| `python -m unittest discover -s extensions/codearts/tests -v` | PASS: 27 tests | Real loopback Hub listener and controlled HTTP/SSE backend; not Huawei acceptance |
| `python tests/run_all.py --jobs 4` | PASS: 222 suites, 0 failed, 0 skipped | Existing suite inventory and assertions unchanged |
| `node --check extensions/codearts/dashboard.js` | PASS | Syntax only |
| Python compilation of extension and launcher | PASS | Syntax only |
| Native panel login, settings save, secret preservation | PASS | Real browser, isolated instance on 8799 |
| CodeArts panel model list and test reply `bridge-ok` | PASS | Controlled backend; no real Huawei account |
| CodeArts navigation reload/deep-link and scoped scrolling | PASS | Real browser; native async initialization and bounded-card scrolling checked |
| Browser error/warning console after tested flow | PASS: none observed | Tested flow only |
| Gateway → real WorkBuddy upstream | PASS: HTTP 200, reply `OK`, 17 tokens | `deepseek-v4.1-flash`, isolated extension launcher, about 3 seconds |
| Real Huawei login/model invocation | NOT_RUN | User confirmed no deployed/logged-in backend and requested branch delivery first |
| Go backend build/test | NOT_RUN | Separate backend source is not changed or bundled; Go not available on PATH |

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
