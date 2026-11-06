/* 账号表性能护栏：分页 + 轮询指纹跳过 + 自动补积分只补一次 + 轮询只在用得上的页签跑。
 *
 * 账号上百个时，账号区每 15 秒整表重画、每次轮询都可能触发一次全池刷积分，
 * 面板明显卡顿。这里钉住四件事：
 *
 *   1. 客户端分页：每页 50 个，排序/区域过滤作用在全量上再切片；数量变少时页码
 *      夹回最后一页；翻页条与最近请求表同款（range-btn + 上一页/下一页）。
 *   2. 指纹跳过：只有轮询（renderAccounts(true)）在数据与视图都没变时跳过重画；
 *      无参调用（用户操作）永远重画；数据一变轮询也重画。
 *   3. 自动补积分：只补缺积分的号、每次页面加载每个号最多一次、带 uids 点名，
 *      绝不发不带参数的全池刷；撞上 busy 放回去下一轮再补。
 *   4. 轮询门控：定时器与回前台只在网关/账号/任务页签拉账号；/accounts 一律
 *      no-store（响应里有带凭据的代理地址，不能落浏览器磁盘缓存）。
 *
 * 表头排序跨页的那条路径（游离节点全量排序再切页）在 _test_accounts_sort_pages.js。
 *
 * Run with Node: node tests/_test_accounts_pagination.js
 */
'use strict';
const assert = require('assert');
const {dashboardScript} = require('./_dashboard_source.js');
const dom = require('./_dom_stub.js');

const script = dashboardScript();

/* ---- 0. 结构 ------------------------------------------------------------ */

assert.ok(/const ACCT_PAGE_SIZE = 50;/.test(script), '账号表每页 50 个');
assert.ok(/setInterval\(\(\) => \{ if\(!document\.hidden && accountsTabActive\(\)\) loadAccounts\(true\); \}, 15000\)/.test(script),
  '15 秒账号轮询要同时看 document.hidden 与 accountsTabActive，并以轮询身份调用');
assert.ok(!/getJSON\('\/accounts\?realm=all', [^)]*POLL_READ/.test(script),
  '/accounts 不能走 POLL_READ：响应里有带凭据的代理地址，必须 no-store');
assert.ok(!/postJSON\('\/accounts\/credits', \{\}\)/.test(script),
  '面板里不能再有不带参数（= 全池扫）的自动补积分请求');

/* ---- 行为：整段脚本配假 DOM ------------------------------------------- */

const calls = [];
let ACCT_PAYLOAD = {accounts: []};
let CREDITS_REPLY = () => ({results: [], accounts: ACCT_PAYLOAD.accounts});

dom.installDom();
global.fetch = (url, options) => {
  const body = options && options.body;
  calls.push({url: String(url), method: (options && options.method) || 'GET',
              cache: options && options.cache, body: body});
  let payload = {};
  if(String(url).indexOf('/accounts?realm=all') === 0) payload = ACCT_PAYLOAD;
  else if(url === '/accounts/credits') payload = CREDITS_REPLY(JSON.parse(body || '{}'));
  else if(String(url).indexOf('/usage/by-account') === 0) payload = {accounts: []};
  else payload = {accounts: [], slots: [], results: []};
  return Promise.resolve({
    status: 200, ok: true,
    json: () => Promise.resolve(payload),
    text: () => Promise.resolve(JSON.stringify(payload)),
  });
};

const realLog = console.log;
console.log = () => {};
let api;
try {
  api = new Function(script + `
    return {
      renderAccounts: renderAccounts,
      loadAccounts: loadAccounts,
      gotoAcctPage: gotoAcctPage,
      changeAcctPage: changeAcctPage,
      accountsTabActive: accountsTabActive,
      filters: ACCOUNT_FILTERS,
      page: () => ACCT_PAGE,
      pages: () => ACCT_TOTAL_PAGES,
      triedSize: () => CREDITS_AUTO_TRIED.size,
      WB_I18N: window.WB_I18N,
      resetAccountFilters: resetAccountFilters,
      panelSessionLost: panelSessionLost,
    };`)();
} catch(e) {
  console.log = realLog;
  console.error('脚本求值失败:', e.message);
  process.exit(1);
}
console.log = realLog;

const box = () => document.getElementById('accounts');
function makeAccounts(n, withCredits){
  const out = [];
  for(let i = 0; i < n; i++){
    const uid = 'uid-p-' + String(i).padStart(3, '0');
    const a = {uid: uid, nickname: 'n' + i, realm: 'intl', enabled: true,
               source: 'oauth', product: 'workbuddy', weight: 1};
    if(withCredits) a.credits = {remain: 1, updated_at: 1};
    out.push(a);
  }
  return out;
}
const uidsShown = () => new Set((box().innerHTML.match(/uid-p-\d{3}/g) || []));
// 假 DOM 接管了 setTimeout，这里用 setImmediate 让出事件循环。
const tick = () => new Promise(r => setImmediate(r));

(async () => {
  /* ---- 1. 分页 -------------------------------------------------------- */
  window.VIEW_REALM = 'intl';
  window.ACCOUNTS = makeAccounts(120, true);
  api.renderAccounts();
  assert.strictEqual(api.pages(), 3, '120 个账号应分 3 页');
  assert.strictEqual(api.page(), 1);
  let shown = uidsShown();
  assert.strictEqual(shown.size, 50, '第 1 页只渲染 50 个，实际 ' + shown.size);
  assert.ok(shown.has('uid-p-000') && shown.has('uid-p-049') && !shown.has('uid-p-050'),
    '第 1 页应是排序后的前 50 个');
  assert.ok(/id="acctPagination"/.test(box().innerHTML), '多于一页时要有翻页条');
  assert.ok(/第 1 \/ 3 页 · 共 120 个账号/.test(box().innerHTML), '翻页条要写明页码与总数');
  assert.ok(/class="range-btn[^"]*"[^>]*onclick="gotoAcctPage\(2\)"/.test(box().innerHTML),
    '页码按钮与最近请求表同款 range-btn');

  api.gotoAcctPage(3);
  shown = uidsShown();
  assert.strictEqual(shown.size, 20, '最后一页 20 个');
  assert.ok(shown.has('uid-p-100') && shown.has('uid-p-119'));

  // 停用的号沉底：排序作用在全量上，而不是只排当前页。
  window.ACCOUNTS = makeAccounts(120, true);
  window.ACCOUNTS[0].enabled = false;
  api.renderAccounts();   // 还停在第 3 页（gotoAcctPage 同页是空操作）
  assert.strictEqual(api.page(), 3);
  assert.ok(uidsShown().has('uid-p-000'), '停用的号应排到最后一页（全量排序后再切片）');
  api.gotoAcctPage(1);
  assert.ok(!uidsShown().has('uid-p-000'));

  // 页码跨轮询保持；数量变少时夹回最后一页。
  window.ACCOUNTS = makeAccounts(120, true);
  api.gotoAcctPage(3);
  window.ACCOUNTS = makeAccounts(70, true);
  api.renderAccounts(true);
  assert.strictEqual(api.pages(), 2);
  assert.strictEqual(api.page(), 2, '账号变少后页码应夹回最后一页');
  assert.strictEqual(uidsShown().size, 20);

  // 不足一页：不出翻页条。
  window.ACCOUNTS = makeAccounts(30, true);
  api.renderAccounts();
  assert.strictEqual(api.page(), 1);
  assert.ok(!/id="acctPagination"/.test(box().innerHTML), '只有一页时不显示翻页条');
  assert.strictEqual(uidsShown().size, 30);

  // 切区域回到第 1 页。
  window.ACCOUNTS = makeAccounts(120, true);
  api.gotoAcctPage(2);
  window.VIEW_REALM = 'cn';
  api.renderAccounts();
  window.VIEW_REALM = 'intl';
  api.renderAccounts();
  assert.strictEqual(api.page(), 1, '切换区域后从第 1 页开始');

  /* ---- 2. 指纹跳过 ---------------------------------------------------- */
  window.ACCOUNTS = makeAccounts(60, true);
  api.renderAccounts();
  box().innerHTML = 'SENTINEL';
  api.renderAccounts(true);
  assert.strictEqual(box().innerHTML, 'SENTINEL', '轮询且数据没变：不该重画');
  api.renderAccounts({type: 'click'});
  assert.notStrictEqual(box().innerHTML, 'SENTINEL', '只认 === true；其它参数（事件对象）照常重画');
  box().innerHTML = 'SENTINEL';
  api.renderAccounts();
  assert.notStrictEqual(box().innerHTML, 'SENTINEL', '用户操作（无参调用）必须重画');
  box().innerHTML = 'SENTINEL';
  window.ACCOUNTS = makeAccounts(60, true);
  window.ACCOUNTS[5].nickname = 'changed';
  api.renderAccounts(true);
  assert.notStrictEqual(box().innerHTML, 'SENTINEL', '数据变了轮询也必须重画');
  box().innerHTML = 'SENTINEL';
  api.changeAcctPage(1);
  assert.notStrictEqual(box().innerHTML, 'SENTINEL', '翻页必须重画');

  /* ---- 3. 自动补积分：只补缺的、只补一次、点名 uids -------------------- */
  const accts = makeAccounts(10, true);
  delete accts[2].credits;
  delete accts[7].credits;
  ACCT_PAYLOAD = {accounts: accts};
  let busyOnce = true;
  CREDITS_REPLY = req => {
    if(busyOnce){ busyOnce = false; return {results: [], accounts: accts, busy: true}; }
    return {results: [], accounts: accts};
  };
  calls.length = 0;
  await api.loadAccounts(true);
  await tick(); await tick();
  let posts = calls.filter(c => c.url === '/accounts/credits');
  assert.strictEqual(posts.length, 1, '缺积分时补一次');
  assert.deepStrictEqual(JSON.parse(posts[0].body), {uids: ['uid-p-002', 'uid-p-007']},
    '只点名缺积分的号');
  assert.strictEqual(api.triedSize(), 0, 'busy 回执：放回去下一轮再补');

  await api.loadAccounts(true);
  await tick(); await tick();
  posts = calls.filter(c => c.url === '/accounts/credits');
  assert.strictEqual(posts.length, 2, 'busy 之后的下一轮应重试');
  assert.strictEqual(api.triedSize(), 2);

  // 服务端仍然没给出积分（例如上游取不到）：本次页面加载内不再重复请求。
  await api.loadAccounts(true);
  await tick(); await tick();
  await api.loadAccounts();
  await tick(); await tick();
  posts = calls.filter(c => c.url === '/accounts/credits');
  assert.strictEqual(posts.length, 2, '同一个号每次页面加载最多自动补一次');

  // 新出现的缺积分号会被单独补，且只带它自己。
  const more = accts.concat([{uid: 'uid-p-900', nickname: 'x', realm: 'intl', enabled: true}]);
  ACCT_PAYLOAD = {accounts: more};
  await api.loadAccounts(true);
  await tick(); await tick();
  posts = calls.filter(c => c.url === '/accounts/credits');
  assert.strictEqual(posts.length, 3);
  assert.deepStrictEqual(JSON.parse(posts[2].body), {uids: ['uid-p-900']});

  /* ---- 4. /accounts 轮询与回读都是 no-store -------------------------------- */
  calls.length = 0;
  await api.loadAccounts(true);
  await api.loadAccounts();
  const reads = calls.filter(c => c.url.indexOf('/accounts?realm=all') === 0).map(c => c.cache);
  assert.deepStrictEqual(reads, ['no-store', 'no-store'], '/accounts 一律 no-store: ' + reads);

  /* ---- 5. 页签门控 ---------------------------------------------------- */
  for(const t of ['gateway', 'accounts', 'tasks']) assert.ok(api.accountsTabActive(t), t + ' 要轮询账号');
  for(const t of ['analytics', 'logs', 'settings', 'agents']) assert.ok(!api.accountsTabActive(t), t + ' 不轮询账号');
  window.__NEXT_UI__ = false;
  assert.ok(!api.accountsTabActive('overview'), '旧布局没有总览页，不轮询');
  window.__NEXT_UI__ = true;
  assert.ok(api.accountsTabActive('overview'), '新导航总览页有账号健康卡，要轮询');
  window.__NEXT_UI__ = false;

  /* ---- 6. 改筛选回到第 1 页 ------------------------------------------- */
  window.ACCOUNTS = makeAccounts(120, true);
  api.renderAccounts();
  api.gotoAcctPage(3);
  assert.strictEqual(api.page(), 3);
  api.filters.query = 'uid-p-0';   // 命中 000-099，共 2 页
  api.renderAccounts();
  assert.strictEqual(api.page(), 1, '改筛选后要回到第 1 页，实际 ' + api.page());
  assert.ok(uidsShown().has('uid-p-000'), '筛选后第 1 页应从最前面的匹配开始');
  api.gotoAcctPage(2);
  api.renderAccounts(true);
  assert.strictEqual(api.page(), 2, '筛选不变时轮询不重置页码');
  api.filters.query = '';

  /* ---- 7. 筛选进指纹：就算有人用轮询身份调过来，筛选变了也要重画 -------- */
  window.ACCOUNTS = makeAccounts(60, true);
  api.renderAccounts();
  box().innerHTML = 'SENTINEL';
  api.filters.status = 'disabled';
  api.renderAccounts(true);
  assert.notStrictEqual(box().innerHTML, 'SENTINEL', '筛选变了，轮询也必须重画');
  box().innerHTML = 'SENTINEL';
  api.renderAccounts(true);
  assert.strictEqual(box().innerHTML, 'SENTINEL', '筛选没再变：轮询照常跳过');
  api.filters.status = '';

  /* ---- 7b. 会话丢失（网关重启）后旧筛选不能把账号列表过滤成空 --------- */
  // 账号筛选是内存态，重新登录不刷新页面。网关重启前设的状态/搜索/分组会留到
  // 登录后，把账号全部过滤掉；「当前禁用」区用的是未筛选名单，反而还显示账号，
  // 于是看起来像账号全丢、必须手动点「清除筛选」才恢复。
  api.resetAccountFilters();
  window.ACCOUNTS = makeAccounts(3, true);
  api.renderAccounts();
  api.filters.status = 'attention';
  api.filters.query = 'nothing-matches';
  api.filters.group = 'some-group';
  api.renderAccounts();
  assert.ok(/没有符合当前条件的账号/.test(box().innerHTML), '前提：旧筛选确实把列表过滤空了');

  api.panelSessionLost();
  assert.strictEqual(api.filters.status, '', '会话丢失要清掉状态筛选');
  assert.strictEqual(api.filters.query, '', '会话丢失要清掉搜索词');
  assert.strictEqual(api.filters.group, '', '会话丢失要清掉分组筛选');

  // 重新登录后（startDashboard 也调一次），账号应全部显示，不再需要手动清筛选。
  api.filters.status = 'attention';
  api.resetAccountFilters();
  api.renderAccounts();
  assert.ok(!/没有符合当前条件的账号/.test(box().innerHTML), '登录后不应再显示筛选空态');
  assert.strictEqual(uidsShown().size, 3, '登录后应显示全部账号');

  /* ---- 8. 三语：翻页条文案 ------------------------------------------- */
  window.ACCOUNTS = makeAccounts(1234, true);
  api.renderAccounts();
  const pagerText = (box().innerHTML.match(/<div id="acctPagination"[\s\S]*$/) || [''])[0]
    .replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
  assert.ok(/第 1 \/ 25 页 · 共 1,234 个账号/.test(pagerText), pagerText);
  const en = api.WB_I18N.translate(pagerText);
  assert.ok(!/[\u4e00-\u9fff]/.test(en), '英文翻页条不得漏出中文: ' + en);
  assert.ok(/Page 1 \/ 25 · 1,234 accounts/.test(en), en);
  assert.ok(/Previous/.test(en) && /Next/.test(en), en);
  const hant = api.WB_I18N.translateHant(pagerText);
  assert.ok(/第 1 \/ 25 頁 · 共 1,234 個帳號/.test(hant), hant);
  assert.ok(/上一頁/.test(hant) && /下一頁/.test(hant), hant);
  assert.ok(!/[页个账号]/.test(hant), '繁体翻页条不得漏出简体: ' + hant);

  process.stdout.write('OK\n');
  process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
