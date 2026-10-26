/* Per-account task participation toggle (issue #521).
 *
 * The account row carries a "任务开/关" chip; toggling it must PATCH only
 * `tasksEnabled` so API dispatch (`enabled`) stays untouched. The task cards
 * and pending-daily-chat list must filter on `tasksEnabled`, not `enabled`.
 *
 * Run with Node: node tests/_test_task_toggle.js
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const html = require('./_dashboard_source.js').dashboardHtml();

const start = html.indexOf('async function toggleAccountTasks');
const end = html.indexOf('async function setAccountWeight');
assert.ok(start > 0 && end > start, 'dashboard.html 里找不到 toggleAccountTasks，测试需要更新');
const source = html.slice(start, end);

const calls = [];
const postJSON = async (url, body) => { calls.push([url, body]); return {}; };
const loadAccounts = async () => {};
const toast = () => {};
const factory = new Function('postJSON', 'loadAccounts', 'toast',
  source + '\nreturn toggleAccountTasks;');
const toggle = factory(postJSON, loadAccounts, toast);

(async () => {
  await toggle('uid-1', true);
  assert.deepStrictEqual(calls[0],
    ['/accounts/set', { uid: 'uid-1', tasksEnabled: true }],
    '切换任务必须只发 tasksEnabled，不能顺手改 enabled');
  await toggle('uid-1', false);
  assert.deepStrictEqual(calls[1],
    ['/accounts/set', { uid: 'uid-1', tasksEnabled: false }]);

  assert.ok(html.includes("a.tasksEnabled ? 'ok' : 'off'"),
    '账号行应显示任务开关状态');
  assert.ok(html.includes('已停用 · 任务中'),
    '停用但参与任务的账号应有明确状态');
  for (const needle of [
    "a.realm === 'intl' && taskActive(a)",
    "a.realm === 'cn' && taskActive(a)",
  ]) {
    assert.ok(html.includes(needle), '任务视图应按 tasksEnabled 过滤：' + needle);
  }
  assert.ok(html.includes("a.tasksEnabled !== undefined ? !!a.tasksEnabled : !!a.enabled"),
    '旧网关没有 tasksEnabled 时应回落到 enabled');
  console.log('task toggle assertions passed (' + calls.length + ' calls)');
})().catch((err) => { console.error(err); process.exit(1); });

