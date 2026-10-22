# CodeArts Bridge Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans inline. The user has approved implementation and publication to a new fork branch.

**Goal:** Add an optional CodeArts provider while preserving all existing Hub files.

**Architecture:** An additive launcher installs process-local wrappers around the
Hub handler, model discovery, body builder and upstream opener. A separate client
talks to CodeArts2API; a dashboard extension uses authenticated management routes.

**Tech Stack:** Python 3.9+ standard library, browser JavaScript, separate Go backend.

**Spec:** `docs/superpowers/specs/2026-10-23-codearts-bridge-design.md`

## Global Constraints

- Add files only; original tracked Hub files and launch behavior remain unchanged.
- Default disabled; use `codeart/` model names; no provider fallback.
- No secrets, local absolute paths, runtime data or test outputs in commits.
- Respect native key concurrency/model/account/realm restrictions.
- Commit/push only after the repository's mandatory smoke checks pass.

## Review Focus

- Backend redirects must not leak authorization (Task 1 integration test).
- Key restrictions and pinned routes must not be bypassed (Task 2 tests).
- WorkBuddy and CodeArts requests must not share attribution or prompts (Task 2 concurrent tests).
- Missing/broken backend must not break native model discovery or UI (Tasks 1/2 tests).
- Panel mutations require a session and same-origin checks (Task 3 tests/browser).

### Task 1: Backend boundary

**Files:** Create `extensions/codearts/config.py`, `client.py`, and tests.
**Consumes:** Separate config JSON with enabled/base_url/api_key/api_key_env/timeouts.
**Produces:** `BridgeConfig`, `CodeArtsClient.models()`, `.chat(payload, session_key)`, `.admin(action, payload)`.

- [x] Write behavioral tests for config validation and a loopback HTTP backend.
- [x] Observe RED for absent bridge behavior.
- [x] Implement configuration, credential isolation, timeout and redirect behavior.
- [x] Run `python -m unittest discover -s extensions/codearts/tests -v` to GREEN.

### Task 2: Hub gateway integration

**Files:** Create `extensions/codearts/bridge.py`, `start-codearts-bridge.py`, tests.
**Consumes:** Task 1 client/config; native Hub Handler, upstream opener and SSE helpers.
**Produces:** `install(hub, config_path)` returning bridge runtime; additive launcher.

- [x] Test Chat stream/nonstream/tools and Responses through a real Hub listener.
- [x] Test native WorkBuddy delegation, key restrictions and provider attribution.
- [x] Implement guarded wrappers and scoped request context without changing globals per request.
- [x] Verify RED/GREEN and native suite.

### Task 3: Dashboard and operations

**Files:** Create `extensions/codearts/dashboard.js`, README, example config and launcher helpers.
**Consumes:** Runtime from Task 2 and native panel-session authentication.
**Produces:** Authenticated CodeArts settings, overview/models and allowlisted account actions.

- [x] Test unauthorized access, masked secrets and cross-origin mutations.
- [x] Implement additive in-memory dashboard extension and route allowlist.
- [x] Browser-check native login, extension settings and model/account display.
- [x] Run isolated real WorkBuddy smoke and report Huawei credentials boundary.
- [x] Review; verify all pre-existing files are unchanged; commit bilingual metadata.
- [x] Push only `codex/codearts-bridge` to the user-authorized fork and verify remote SHA.

## Execution rulings

- The task directory is not a Git repository, so native create_worktree returned
  `Not a git repository`. Use a separate clean clone of the authorized fork.
- Existing local Hub changes are not part of this branch. Its baseline is the
  fork's current main; no reset/stash/cherry-pick of the active checkout.
- Defer all commits until mandatory live WorkBuddy and browser smoke gates pass;
  this repository rule takes precedence over skill suggestions to commit per task.
- Keep extension tests outside the native runner inventory to honor the approved
  zero-modification boundary; run both commands explicitly and ship instructions.

## Completion evidence

- Implementation and review complete; extension 27/27, native 222/222.
- Real browser login/settings/model request/reload passed on an isolated port.
- Real WorkBuddy upstream returned HTTP 200 / OK.
- User explicitly accepted real Huawei login/inference as NOT_RUN for this delivery.
- Publication targets only the new fork branch; verify its SHA after pushing.

## Login and unified-port follow-up / 登录及统一端口跟进

The client gateway stays on the native default 8788; 8799 is for isolated tests.
Canonical model IDs use codeart/ only for routing and display; the prefix is
removed before forwarding the exact upstream model ID. Legacy codearts/ requests
remain supported. Login opens a window synchronously before awaiting OAuth.
Optional explicit backend management builds or starts CodeArts2API on loopback,
enables the bridge after readiness, and binds only owned children to gateway exit.

客户端统一使用默认 8788，8799 仅用于隔离验收；前缀不进入真实后端模型名称。
登录弹窗、托管后端及进程回收已纳入本轮回归，真实华为推理仍待人工登录后验证。
