# CodeArts native UI implementation plan

> Execute inline with superpowers:executing-plans. User approved the native layout and controls.

**Goal:** Align CodeArts with the current WorkBuddy dashboard and preserve provider isolation.
**Architecture:** Build persistent controls with cached data renderers in dashboard.js; scoped styling in dashboard.css, served by the extension only.
**Tech Stack:** Browser DOM, native CSS tokens, Python standard library, Node VM component tests.
**Spec:** docs/superpowers/specs/2026-10-23-codearts-native-ui-design.md

## Global constraints

Original Hub files and 8788 stay unchanged. Keep all seven selected models, account credentials and codeart/ routing. No automatic model calls, no fabricated availability/parameters. Simplified Chinese, Traditional Chinese and English remain supported.

## Review focus

- Refresh during editing must preserve unsaved settings and test input/result.
- A failing section must preserve other successfully loaded data and report its own error.
- Backend nicknames/model IDs must remain text, never executable HTML.
- Token validity, account enabled status and model catalog membership are different facts.
- Changing language/theme must not reset edits or issue inference requests.

## Task 1: Native layout and state handling

Files: extensions/codearts/dashboard.js, dashboard.css, bridge.py, tests/test_dashboard.js.
Consumes existing config/overview/models/usage/admin endpoints; produces native cards, toolbar, account/model tables, collapsible test/settings panels.
- [x] Add component tests for the review focus, observe failures against old UI.
- [x] Implement persistent DOM, scoped CSS, static asset serving, independent section loads, edit revisions and reactive labels.
- [x] Run component, login and extension tests; independently review the complete change.

## Task 2: Verification and handoff

Files: extensions/codearts/README.md, CHANGELOG.md, VERIFICATION.md.
- [x] Run native regression suite and diff/privacy checks.
- [ ] Perform browser acceptance only if browser policy permits. Do not bypass the prior 8799 denial.
- [ ] Record actual validation boundaries. CONTRIBUTING requires browser acceptance before commit/push, so keep local changes if that gate remains blocked.

## Execution ledger

- Component RED: old UI lacked native tables.
- Component GREEN: tables, inert text, selection/test result retention, edit/save races, backend isolation, live labels.
- Independent review: two P2 findings reproduced and fixed; final review clean.
- Python extension 38/38, native Hub 222/222, OAuth Node test PASS; syntax checks PASS.
- Isolated 8799 restarted with existing credentials; both 8799 and untouched 8788 health 200.
- Browser access explicitly denied by browser policy. No workaround attempted.
- User was asked to perform actual page/login/console checks; answer pending.
- Ruling: defer commits/pushes until CONTRIBUTING browser gate is satisfied; prior branch HEAD remains 0348633.
