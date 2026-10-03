/* Every inline event handler in the dashboard must have a function behind it.
 *
 * A handler name that no longer exists fails only in the browser, only when
 * that exact control is used, and only as a console error - no server-side
 * test can see it. The empty state's "登录新账号 (OAuth)" button shipped that
 * way for a month (issue #66): it still called startLogin(), which had been
 * replaced by openLoginModal() back in v1.1.0, so the first button a fresh
 * deployment puts in front of a new user did nothing at all.
 *
 * The sweep is textual on purpose: it reads the shipped dashboard.html, so it
 * covers every handler at once instead of the one that happened to be
 * reported, and it keeps working when the page is refactored.
 *
 * Run with Node: node tests/_test_dashboard_handlers.js
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard.html'), 'utf8');
const activeHtml = html.replace(/<!--[\s\S]*?-->/g, '');
for (const id of ['btnScanDesktop', 'btnCheckin', 'btnDailyChat', 'btnDailyChatWeb']) {
  assert.ok(activeHtml.includes(`id="${id}"`), `missing account action: ${id}`);
}

// Inline attributes, e.g. onclick="setAnalyticsRange('today')".
const HANDLER_ATTR = /\son(?:click|change|input|submit|keydown|keyup|blur|focus)\s*=\s*"([^"]*)"/g;
// A plain call: `foo(` counts, `this.foo(` is a method and does not.
const CALL = /(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(/g;
// Keywords and browser builtins, which are not page functions.
const NOT_PAGE_FUNCTIONS = new Set([
  'if', 'for', 'while', 'switch', 'return', 'typeof', 'new', 'function',
  'alert', 'confirm', 'prompt', 'setTimeout', 'setInterval', 'clearTimeout',
  'parseInt', 'parseFloat', 'Number', 'String', 'Boolean', 'Date', 'Math',
  'JSON', 'encodeURIComponent', 'decodeURIComponent',
]);

const referenced = new Set();
for (const [, body] of html.matchAll(HANDLER_ATTR)) {
  for (const [, name] of body.matchAll(CALL)) {
    if (!NOT_PAGE_FUNCTIONS.has(name)) referenced.add(name);
  }
}

const defined = new Set();
for (const [, name] of html.matchAll(/function\s+([A-Za-z_$][\w$]*)\s*\(/g)) defined.add(name);
for (const [, name] of html.matchAll(/(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\()/g)) {
  defined.add(name);
}

const missing = [...referenced].filter(name => !defined.has(name)).sort();

// A regex that stopped matching would let this test pass silently, so insist
// it is still looking at a real page.
assert.ok(referenced.size >= 30,
  `the sweep found only ${referenced.size} handlers; the extraction broke, not the page`);

assert.deepStrictEqual(missing, [],
  `inline handlers with no function behind them: ${missing.join(', ')}`);

// The reported case, pinned directly so a rename cannot quietly reintroduce it.
assert.ok(/onclick="openLoginModal\(\)"/.test(html),
  'the empty-state login button must call openLoginModal()');
assert.ok(!/onclick="startLogin\(\)"/.test(html),
  'nothing may call the removed startLogin()');

const scanStart = html.indexOf('function renderDesktopScan(');
const scanEnd = html.indexOf('async function doImportDesktop(', scanStart);
assert.ok(scanStart >= 0 && scanEnd > scanStart);
const scanBody = {innerHTML: '', querySelectorAll: () => []};
const scanWindow = {SCAN_POOL: new Set(['aabbccdd-current'])};
const renderScan = new Function('window', 'document', 'esc',
  html.slice(scanStart, scanEnd) + '\nreturn renderDesktopScan;')(
    scanWindow, {getElementById: () => scanBody}, String);
const encryptedCurrent = {uid: 'aabbccdd-current', valid: false, backup: false,
  file: 'workbuddy-desktop.info', error: 'Encrypted credential; OAuth required'};
renderScan([encryptedCurrent]);
assert.ok(scanBody.innerHTML.includes('aabbccdd'));
assert.ok(scanBody.innerHTML.includes('当前客户端登录'));
assert.ok(scanBody.innerHTML.includes('已在网关中，无需重复导入'));
assert.ok(!scanBody.innerHTML.includes('btn-import-desktop'));
renderScan([encryptedCurrent, {uid: 'backup', valid: true, backup: true,
  file: 'workbuddy-desktop.2030-10-03T00.info', realm: 'cn'}]);
assert.ok(scanBody.innerHTML.indexOf('当前客户端登录') < scanBody.innerHTML.indexOf('历史登录备份'));
assert.equal((scanBody.innerHTML.match(/class="primary mini btn-import-desktop"/g) || []).length, 1);
scanWindow.SCAN_POOL.clear();
renderScan([encryptedCurrent]);
assert.ok(!scanBody.innerHTML.includes('已在网关中，无需重复导入'));
assert.ok(scanBody.innerHTML.includes('OAuth required'));

console.log(`dashboard handler assertions passed (${referenced.size} handlers checked)`);
