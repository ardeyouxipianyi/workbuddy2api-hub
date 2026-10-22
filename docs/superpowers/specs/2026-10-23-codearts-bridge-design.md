# CodeArts 外置桥接设计 / External bridge design

用户已在会话中批准：原有 Hub 文件零修改，以独立扩展复用网关和面板，并推送到 fork 的新分支。

## Contract

- Only add files. Keep every pre-existing tracked Hub file unchanged.
- Keep CodeArts2API as a separate service. Its OAuth, tokens, account rotation,
  refresh and check-in remain owned by that service.
- A separate launcher imports Hub and installs process-local adapters. Starting
  the original launcher never imports or enables the extension.
- Bridge configuration is separate, disabled by default, and stores secrets only
  in the ignored accounts directory or reads them from an environment variable.
- Public models use `codearts/<upstream-id>`. Native WorkBuddy names keep their
  original routing. There is no cross-provider fallback.
- Reuse native authentication, per-key concurrency, model restrictions, SSE
  handling and Chat/Responses translation. Keep Anthropic's existing opt-in gate.
- A WorkBuddy realm-bound, account-restricted or pinned-account request cannot
  silently access the CodeArts pool. Return a clear local error instead.
- Never apply WorkBuddy identity/prompt sanitization to CodeArts messages.
- Preserve upstream usage and attribute bridge rows to provider/realm CodeArts,
  rather than the selected WorkBuddy realm. The bridge represents a service pool,
  not a fabricated Huawei account or balance.
- Extend the served dashboard in memory. Add a CodeArts page using the native
  visual shell, panel session, model list and explicit account actions.
- Management requests use a fixed route allowlist and server-side API key.
  Never expose the backend key to the browser; reject cross-origin mutations.
- Keep backend URL changes under panel authentication. Do not follow redirects
  carrying the backend credential to another host.
- Version-check required Hub seams at startup and fail clearly if incompatible.

## Verification

Run a failing behavioral test before implementation, then loopback integration
tests for stream/nonstream/tools/Responses, authentication, key restrictions,
disabled/offline backends, concurrent provider attribution and management.
Run the original full suite without changing its inventory or assertions.
Verify the real dashboard in a browser and a real WorkBuddy upstream response
on an isolated port before committing/pushing, as CONTRIBUTING.md requires.
Huawei end-to-end acceptance requires an authenticated CodeArts account; report
it separately from mock-wire and zero-account service checks.

## Delivery

Branch: `codex/codearts-bridge`, based on the fork's main at implementation start.
Ship extension sources, tests, examples, launchers and operation instructions.
Keep the original running service and client/provider configuration untouched.
All secrets and test artifacts stay untracked. No merge or upstream publication.
