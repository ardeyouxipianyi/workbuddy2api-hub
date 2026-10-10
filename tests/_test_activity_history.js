/* Drive the dashboard's daily-activity surface in Node.
 *
 * Two things break silently here. A status chip can be rendered from the wrong
 * account's event - the string is still somewhere in the table, so a whole-table
 * substring check cannot see it - and the history modal can ask the wrong
 * question (wrong range, missing account filter), which looks fine on screen
 * until someone trusts a filtered list. Both are per-account properties, so
 * this suite reads the rendered rows back through their data-label and asserts
 * the query string the panel actually builds.
 *
 * The page functions are the real ones out of dashboard.html; only fetch and
 * the DOM are stubbed.
 *
 * Requires node (no other dependency).
 *
 *   node _test_activity_history.js
 */
const assert = require('assert');
const {dashboardScript, dashboardHtml} = require('./_dashboard_source.js');

const code = dashboardScript();

// --- fixtures ---------------------------------------------------------------
const stamp = (d) => {
  const p = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
};
const TODAY = stamp(new Date());

// One account per state, and both realms, so a chip taken from the wrong row
// changes the answer rather than duplicating it.
const ACCOUNTS = [
  {uid: 'cn-uid-0001', nickname: '甲 · 国内', realm: 'cn', enabled: true,
   source: 'panel', lastCheckin: null},
  {uid: 'cn-uid-0004', nickname: '丁 · 失败', realm: 'cn', enabled: true,
   source: 'panel', lastCheckin: null},
  {uid: 'intl-uid-0002', nickname: '乙 · 国际', realm: 'intl', enabled: true,
   source: 'panel', lastDailyChat: TODAY + ' 09:00:00'},
  {uid: 'cn-uid-0003', nickname: '丙 · 无记录', realm: 'cn', enabled: true,
   source: 'panel', lastCheckin: null},
];

// Newest first, as the API returns them.
const TODAY_ROWS = [
  {ts: TODAY + 'T10:00:00+08:00', uid: 'cn-uid-0001', nickname: '甲 · 国内',
   realm: 'cn', task: 'checkin', trigger: 'scheduler', ok: true, message: '签到成功'},
  {ts: TODAY + 'T08:00:00+08:00', uid: 'cn-uid-0004', nickname: '丁 · 失败',
   realm: 'cn', task: 'checkin', trigger: 'manual', ok: false,
   message: '<img src=x onerror=alert(1)> 上游 500'},
];

const WEEK_ROWS = [
  {ts: TODAY + 'T10:00:00+08:00', uid: 'cn-uid-0001', nickname: '甲 · 国内',
   realm: 'cn', task: 'checkin', trigger: 'scheduler', ok: true, message: '签到成功'},
  {ts: '2026-10-07T09:30:00+08:00', uid: 'intl-uid-0002', nickname: '乙 · 国际',
   realm: 'intl', task: 'daily_chat', trigger: 'scheduler', ok: true, message: '活跃完成'},
];

// --- fake network -----------------------------------------------------------
let calls = [];
let historyReply = () => ({rows: WEEK_ROWS, total: 2, limit: 200, range: '7d',
                           filters: {uid: '', task: '', result: ''}});
const reply = (payload) => Promise.resolve({
  ok: true, status: 200,
  json: () => Promise.resolve(payload), text: () => Promise.resolve(''),
});

const dom = require('./_dom_stub.js');
const {window: domWindow} = dom.installDom({
  fetch: (url) => {
    const target = String(url);
    calls.push(target);
    if (target.startsWith('/activity/history')) {
      const payload = historyReply();
      return payload instanceof Error ? Promise.reject(payload) : reply(payload);
    }
    if (target.startsWith('/accounts')) return reply({accounts: ACCOUNTS});
    return reply({});
  },
});
domWindow.ACCOUNTS = ACCOUNTS;
global.ACCOUNTS = domWindow.ACCOUNTS;

const api = new Function(code + `
  window.updateUI = updateUI;
  window.toast = toast;
  return { loadAccounts, loadActivityToday, renderAccounts, openActivityHistory,
           closeActivityHistory, setActivityFilter, loadActivityHistory };`)();

// --- tiny row reader --------------------------------------------------------
// Same shape as the other dashboard suites: split rows, read a labelled cell.
// Matching is on data-label and text only, so attribute order or an extra class
// is not a failure.
const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const rowsOf = (html) => html.split(/<tr[^>]*>/).slice(1).map(r => r.split(/<\/tr>/)[0]);
const cellOf = (row, label) => {
  const m = row.match(new RegExp('<td[^>]*data-label="' + escapeRe(label) + '"[^>]*>([\\s\\S]*?)</td>'));
  return m ? m[1] : null;
};
const accountRow = (html, name) => {
  const hits = rowsOf(html).filter(r => (cellOf(r, '账号') || '').includes(name));
  return hits.length === 1 ? hits[0] : null;
};
const chipOf = (html, name) => {
  const row = accountRow(html, name);
  return row === null ? null : (cellOf(row, '今日活动') || '');
};
// The chip's visible text, without the tooltip. The tooltip for the neutral
// state says "没有记录不等于失败" on purpose, so a substring test against the
// whole chip would read that explanation as a failure.
const chipText = (chip) => {
  const m = String(chip || '').match(/>([^<]*)<\/span>\s*$/);
  return m ? m[1] : '';
};
const accountsOut = () => document.getElementById('accounts').innerHTML;
const historyOut = () => document.getElementById('activityHistoryRows').innerHTML;

let pass = 0, fail = 0;
const check = (label, cond, extra) => {
  if (cond) { pass++; console.log('  [PASS] ' + label); }
  else { fail++; console.log('  [FAIL] ' + label + (extra ? '  ' + extra : '')); }
};

(async () => {
  const todayReply = () => ({rows: TODAY_ROWS, total: 2, limit: 200, range: 'today',
                             filters: {uid: '', task: '', result: ''}});

  console.log('[1] compact status is bound to the account row it describes');
  // The page resets window.ACCOUNTS at load time, so the fixtures go in through
  // the real loader rather than by poking the global.
  window.VIEW_REALM = 'cn';
  historyReply = todayReply;
  await api.loadAccounts();
  let out = accountsOut();

  check('every account in the view renders exactly one row',
        ['甲 · 国内', '丁 · 失败', '丙 · 无记录'].every(n => accountRow(out, n) !== null));
  check('the other realm is not in this view', accountRow(out, '乙 · 国际') === null);
  check('a CN account with a recorded success says so on its own row',
        (chipOf(out, '甲 · 国内') || '').includes('今日签到 · 成功'),
        JSON.stringify(chipOf(out, '甲 · 国内')));
  check('a CN account with a recorded failure says so on its own row',
        (chipOf(out, '丁 · 失败') || '').includes('今日签到 · 失败'),
        JSON.stringify(chipOf(out, '丁 · 失败')));
  check('an account with no event and no completion says 无记录, not a failure',
        chipText(chipOf(out, '丙 · 无记录')) === '今日签到 · 无记录'
        && (chipOf(out, '丙 · 无记录') || '').includes('badge off'),
        JSON.stringify(chipOf(out, '丙 · 无记录')));
  check('the failure chip is rendered as a failure, not as a success',
        (chipOf(out, '丁 · 失败') || '').includes('badge bad'),
        JSON.stringify(chipOf(out, '丁 · 失败')));
  check('no other row claims the failure',
        !chipText(chipOf(out, '甲 · 国内')).includes('失败')
        && !chipText(chipOf(out, '丙 · 无记录')).includes('失败'),
        JSON.stringify([chipText(chipOf(out, '甲 · 国内')), chipText(chipOf(out, '丙 · 无记录'))]));
  check('exactly one row carries the success state',
        (out.match(/今日签到 · 成功/g) || []).length === 1,
        (out.match(/今日签到 · 成功/g) || []).length);
  check('exactly one row carries the failure state',
        (out.match(/今日签到 · 失败/g) || []).length === 1,
        (out.match(/今日签到 · 失败/g) || []).length);
  check('the 今日活动 cell is labelled for the phone layout',
        (chipOf(out, '甲 · 国内') !== null));

  window.VIEW_REALM = 'intl';
  api.renderAccounts();
  out = accountsOut();
  check('an INTL account that completed today is labelled 今日活跃, not 今日签到',
        (chipOf(out, '乙 · 国际') || '').includes('今日活跃 · 已完成')
        && !(chipOf(out, '乙 · 国际') || '').includes('今日签到'),
        JSON.stringify(chipOf(out, '乙 · 国际')));

  console.log();
  console.log('[2] the chip opens the modal pre-filtered to its account');
  window.VIEW_REALM = 'cn';
  api.renderAccounts();
  out = accountsOut();
  check('the chip carries the account uid into the click handler',
        (chipOf(out, '甲 · 国内') || '').includes('openActivityHistory(&quot;cn-uid-0001&quot;)'),
        JSON.stringify(chipOf(out, '甲 · 国内')));
  calls = [];
  historyReply = () => ({rows: WEEK_ROWS, total: 2, limit: 200, range: '7d',
                         filters: {uid: 'cn-uid-0001', task: '', result: ''}});
  await api.openActivityHistory('cn-uid-0001');
  check('the modal asks for that account',
        calls.length === 1 && calls[0] === '/activity/history?range=7d&limit=200&uid=cn-uid-0001',
        JSON.stringify(calls));
  check('the modal defaults to the last 7 days',
        calls.length === 1 && calls[0].includes('range=7d'), JSON.stringify(calls));
  check('the account select is pre-filtered too',
        document.getElementById('actUid').value === 'cn-uid-0001',
        document.getElementById('actUid').value);
  check('the modal is shown',
        document.getElementById('activityHistoryModal').style.display === 'flex',
        document.getElementById('activityHistoryModal').style.display);
  check('the modal lists the rows it received', historyOut().includes('签到成功'));
  api.closeActivityHistory();
  check('closing hides the modal again',
        document.getElementById('activityHistoryModal').style.display === 'none',
        document.getElementById('activityHistoryModal').style.display);

  console.log();
  console.log('[3] each filter asks the server the right question');
  await api.openActivityHistory('');          // the toolbar entry: all accounts
  check('with no account picked the query omits uid',
        calls[calls.length - 1] === '/activity/history?range=7d&limit=200',
        calls[calls.length - 1]);
  await api.setActivityFilter('range', 'today');
  check('range=today replaces the default window',
        calls[calls.length - 1] === '/activity/history?range=today&limit=200',
        calls[calls.length - 1]);
  await api.setActivityFilter('range', '30d');
  check('range=30d is passed through',
        calls[calls.length - 1] === '/activity/history?range=30d&limit=200',
        calls[calls.length - 1]);
  await api.setActivityFilter('uid', 'intl-uid-0002');
  check('an account filter is appended',
        calls[calls.length - 1] === '/activity/history?range=30d&limit=200&uid=intl-uid-0002',
        calls[calls.length - 1]);
  await api.setActivityFilter('task', 'checkin');
  check('a task filter is appended',
        calls[calls.length - 1].endsWith('&task=checkin'), calls[calls.length - 1]);
  await api.setActivityFilter('result', 'failed');
  check('a result filter is appended',
        calls[calls.length - 1].endsWith('&result=failed'), calls[calls.length - 1]);
  check('filtering never reloads the account list',
        calls.filter(u => u.startsWith('/accounts')).length === 0, JSON.stringify(calls));
  await api.setActivityFilter('range', '7d');
  await api.setActivityFilter('uid', '');
  await api.setActivityFilter('task', '');
  await api.setActivityFilter('result', '');

  console.log();
  console.log('[4] messages are escaped and long ones stay out of the layout');
  historyReply = () => ({rows: TODAY_ROWS, total: 2, limit: 200, range: '7d',
                         filters: {uid: '', task: '', result: ''}});
  await api.loadActivityHistory();
  check('an upstream message cannot become markup',
        !historyOut().includes('<img src=x') && historyOut().includes('&lt;img src=x'),
        historyOut().slice(0, 200));
  check('the unescaped form is kept for the tooltip, escaped',
        historyOut().includes('title="&lt;img src=x onerror=alert(1)&gt; 上游 500"'),
        historyOut().slice(0, 300));
  check('the message cell truncates instead of widening the modal',
        historyOut().includes('text-overflow:ellipsis'));
  check('both results render, each on its own row',
        historyOut().includes('badge bad') && historyOut().includes('badge ok'));

  console.log();
  console.log('[5] empty and failing payloads degrade without touching the accounts');
  historyReply = () => ({rows: [], total: 0, limit: 200, range: '7d', filters: {}});
  await api.loadActivityHistory();
  check('an empty window says so', historyOut().includes('没有活动记录'), historyOut());
  historyReply = () => new Error('500');
  await api.loadActivityHistory();
  check('a failing read says so instead of rendering a stale list',
        historyOut().includes('读取失败'), historyOut());
  check('the accounts are still rendered after the history read failed',
        accountRow(accountsOut(), '甲 · 国内') !== null);

  calls = [];
  historyReply = () => new Error('offline');
  await api.loadAccounts();
  out = accountsOut();
  check('a failed activity read leaves the account area intact',
        accountRow(out, '甲 · 国内') !== null && accountRow(out, '丙 · 无记录') !== null);
  check('and the chips fall back to the neutral state, not to a failure',
        chipText(chipOf(out, '甲 · 国内')) === '今日签到 · 无记录'
        && (chipOf(out, '甲 · 国内') || '').includes('badge off'),
        JSON.stringify(chipOf(out, '甲 · 国内')));
  check('loadAccounts asks for today\u2019s activity',
        calls.some(u => u === '/activity/history?range=today&limit=200'), JSON.stringify(calls));

  console.log();
  console.log('[6] switching realm keeps every status on its own account');
  historyReply = todayReply;
  await api.loadActivityToday();
  window.VIEW_REALM = 'cn';
  api.renderAccounts();
  out = accountsOut();
  check('the CN view shows the CN accounts',
        accountRow(out, '甲 · 国内') !== null && accountRow(out, '丁 · 失败') !== null);
  check('the CN view drops the INTL account', accountRow(out, '乙 · 国际') === null);
  check('the success stays on 甲 after the realm switch',
        (chipOf(out, '甲 · 国内') || '').includes('今日签到 · 成功'),
        JSON.stringify(chipOf(out, '甲 · 国内')));
  check('the failure stays on 丁 after the realm switch',
        (chipOf(out, '丁 · 失败') || '').includes('今日签到 · 失败'),
        JSON.stringify(chipOf(out, '丁 · 失败')));
  window.VIEW_REALM = 'intl';
  api.renderAccounts();
  out = accountsOut();
  check('the INTL view shows the completion on 乙, not on a CN row',
        (chipOf(out, '乙 · 国际') || '').includes('今日活跃 · 已完成'),
        JSON.stringify(chipOf(out, '乙 · 国际')));

  console.log();
  console.log('[7] the surfaces the phone layout depends on are still there');
  const page = dashboardHtml();
  const modal = page.slice(page.indexOf('id="activityHistoryModal"'),
                           page.indexOf('id="creditsDetailModal"'));
  check('the modal markup is in the shipped page',
        modal.includes('签到与每日活跃记录'), modal.slice(0, 80));
  check('the history table is a data-cards table, so it collapses on a phone',
        modal.includes('<table class="data-cards">'));
  check('every filter control is in the modal',
        ['actRange', 'actUid', 'actTask', 'actResult'].every(id => modal.includes('id="' + id + '"')));
  check('an empty or failed list spans the whole card grid',
        historyOut().includes('colspan="5"'), historyOut());
  historyReply = () => ({rows: WEEK_ROWS, total: 2, limit: 200, range: '7d', filters: {}});
  await api.loadActivityHistory();
  check('every history row cell carries a data-label for the phone layout',
        (historyOut().match(/<td/g) || []).length === (historyOut().match(/data-label=/g) || []).length,
        (historyOut().match(/<td/g) || []).length + ' vs ' + (historyOut().match(/data-label=/g) || []).length);
  check('the toolbar entry exists next to the account controls',
        page.includes('onclick="openActivityHistory()"'));

  console.log();
  console.log('PASS=' + pass + ' FAIL=' + fail);
  process.exit(fail ? 1 : 0);
})();
