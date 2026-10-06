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
// `window.name = function(...)` is the other way this page defines a handler;
// the theme block's applyTheme / selectTheme / toggleThemeMenu all use it.
// Whether such a name is actually reachable from an inline attribute is a
// scope question this textual sweep cannot answer - _test_dashboard_theme.js
// executes the theme block and checks that.
for (const [, name] of html.matchAll(/window\.([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\()/g)) {
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

console.log(`dashboard handler assertions passed (${referenced.size} handlers checked)`);
