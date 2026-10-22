"use strict";
const assert = require("node:assert/strict");
const {start} = require("../login.js");

(async () => {
  // Opening only after the awaited HTTP request loses the browser's user
  // gesture. Verify the popup exists before that request can resolve.
  const events = [];
  let resolveRequest;
  const popup = {opener: {}, location: {href: "about:blank"}, closed: false,
    close() { this.closed = true; }};
  const request = new Promise(resolve => { resolveRequest = resolve; });
  const running = start(() => { events.push("open"); return popup; },
    () => { events.push("request"); return request; });
  assert.deepEqual(events, ["open", "request"]);
  assert.equal(popup.opener, null);
  resolveRequest({session_id: "session-1", auth_url: "https://auth.example.test/authorize?ticket=1"});
  const result = await running;
  assert.equal(result.sessionId, "session-1");
  assert.equal(popup.location.href, "https://auth.example.test/authorize?ticket=1");
  assert.equal(result.popupOpened, true);

  const blocked = await start(() => null, async () => ({session_id: "s2", auth_url: "https://auth.example.test/authorize"}));
  assert.equal(blocked.popupOpened, false);
  assert.equal(blocked.authUrl, "https://auth.example.test/authorize");

  const failed = {opener: {}, location: {}, closed: false, close() { this.closed = true; }};
  await assert.rejects(start(() => failed, async () => { throw new Error("backend offline"); }), /backend offline/);
  assert.equal(failed.closed, true);
  const invalid = {opener: {}, location: {}, closed: false, close() { this.closed = true; }};
  await assert.rejects(start(() => invalid, async () => ({session_id: "s3", auth_url: "javascript:alert(1)"})), /authorization URL/);
  assert.equal(invalid.closed, true);
  assert.equal(invalid.location.href, undefined);
  console.log("PASS: immediate login popup, safe navigation, blocked-popup fallback and failure cleanup");
})().catch(error => { console.error(error); process.exitCode = 1; });
