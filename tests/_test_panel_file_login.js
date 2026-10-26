/* Opening dashboard.html from disk cannot log in.
 *
 * A reporter was opening the file directly and got "登录失败: Failed to
 * fetch". Relative fetch('/panel/login') under file:// either fails or talks
 * to a non-existent local server; the correct entry point is the running
 * gateway's HTTP address. Pin the message and make sure the check happens
 * before fetch, so users get an actionable explanation instead of a network
 * error.
 *
 * Run with Node: node tests/_test_panel_file_login.js
 */
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard.html'), 'utf8');
const start = html.indexOf('async function submitPanelLogin(){');
assert.ok(start > 0, 'submitPanelLogin moved; update this test marker');
const end = html.indexOf('\nasync function panelLogout()', start);
assert.ok(end > start, 'panelLogout marker not found');
const source = html.slice(start, end);

const message = {textContent: ''};
let fetchCalls = 0;
let needsLoginCalls = 0;
const input = {value: 'admin'};
const elements = {
  panelPwdInput: input,
  panelLoginMsg: message,
};
const sandbox = {
  document: {getElementById: id => elements[id] || null},
  location: {protocol: 'file:'},
  fetch: () => { fetchCalls++; throw new Error('fetch must not run for file://'); },
  panelNeedsLogin: () => { needsLoginCalls++; },
  sessionStorage: {setItem() {}, removeItem() {}},
  toast() {},
  console,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(source + '\nthis.__submit = submitPanelLogin;', sandbox,
  {filename: 'dashboard-panel-login-file.js'});

Promise.resolve(sandbox.__submit()).then(() => {
  assert.strictEqual(fetchCalls, 0, 'file:// must not attempt a relative API request');
  assert.strictEqual(needsLoginCalls, 1, 'the gate must stay visible');
  assert.ok(message.textContent.includes('http://127.0.0.1:8788/'),
    `expected the HTTP address in: ${message.textContent}`);
  assert.ok(message.textContent.includes('dashboard.html'),
    'the message should explain not to open the HTML file directly');
  console.log('dashboard file:// login assertions passed');
}).catch(err => {
  console.error(err);
  process.exitCode = 1;
});
