/* 账号页与签到记录（issue #176）。
 *
 * issue 的两条诉求分别是「账号多起来一直向下排列不方便管理」和「看不出来签到成功
 * 没有」。折叠与落库/读接口已由 #226、#206 落地，这里钉住剩下的界面部分：
 *
 *   1. 「账号」是一个独立的主菜单，账号列表与「当前禁用」总览都搬进 #pageAccounts，
 *      网关页不再留账号区；
 *   2. 两个主标签清单（head 里的 TABS 与主脚本的 MAIN_TABS）必须一致 —— 它们漂开
 *      的后果是刷新后停在一个不存在的页，或者菜单点不动；
 *   3. 签到与活跃记录读 GET /activity/history：筛选进查询串、结果按行渲染、
 *      「加载更多」按页加 limit、空态与读取失败都有话说。
 *
 * 这些错法在页面上都只是「少了一列 / 点了没反应 / 表格停在上一份」，单看代码看不
 * 出来，所以既查结构也把整段脚本配假 DOM 跑起来。
 *
 * Run with Node: node tests/_test_accounts_page.js
 */
'use strict';
const assert = require('assert');
const {dashboardHtml, dashboardScript} = require('./_dashboard_source.js');
const dom = require('./_dom_stub.js');

const html = dashboardHtml();
const script = dashboardScript();

/* ---- 1. 结构：账号有了自己的主菜单 ---------------------------------- */

assert.ok(/id="btnNavAccounts"[^>]*onclick="switchMainTab\('accounts'\)"/.test(html),
  '顶部导航应有「账号」菜单，并指向 switchMainTab(\'accounts\')');
assert.ok(/<div id="pageAccounts" class="main-page">/.test(html),
  '缺少账号页容器 #pageAccounts.main-page');

const pageGatewayAt = html.indexOf('<div id="pageGateway"');
const pageAccountsAt = html.indexOf('<div id="pageAccounts"');
const pageAnalyticsAt = html.indexOf('<div id="pageAnalytics"');
const accountsBodyAt = html.indexOf('id="accountsBody"');
const disabledListAt = html.indexOf('id="disabledList"');
assert.ok(pageGatewayAt > 0 && pageAccountsAt > pageGatewayAt && pageAnalyticsAt > pageAccountsAt,
  '页面顺序应为 gateway → accounts → analytics');
assert.ok(accountsBodyAt > pageAccountsAt && accountsBodyAt < pageAnalyticsAt,
  '账号列表必须在账号页里');
assert.ok(disabledListAt > pageAccountsAt && disabledListAt < pageAnalyticsAt,
  '「当前禁用」总览必须在账号页里');
assert.ok(!html.slice(pageGatewayAt, pageAccountsAt).includes('accountsBody'),
  '网关页不该再留着账号列表');
assert.ok(!html.slice(pageGatewayAt, pageAccountsAt).includes('disabledList'),
  '网关页不该再留着「当前禁用」总览');

// 两份主标签清单必须逐项一致：head 里那份决定首屏点亮哪一页，主脚本那份决定
// 切页时谁被激活，漂开就会出现「菜单亮着但内容是空页」。
const headTabs = html.match(/var TABS = \[([^\]]*)\];/);
const mainTabs = html.match(/const MAIN_TABS = \[([^\]]*)\];/);
assert.ok(headTabs && mainTabs, '找不到 TABS / MAIN_TABS 清单');
const names = s => s.split(',').map(x => x.trim().replace(/['"]/g, '')).filter(Boolean);
assert.deepStrictEqual(names(headTabs[1]), names(mainTabs[1]),
  'head 的 TABS 与主脚本的 MAIN_TABS 必须一致');
assert.ok(names(mainTabs[1]).includes('accounts'), 'accounts 必须在主标签清单里');

// 切到账号页要顺手把签到记录拉起来；开关关掉（默认）时账号区就在
// 「网关与账号」页，切到网关页同样要拉。
assert.ok(script.includes("if(tab === 'accounts' || (!_accountsSeparateTab && tab === 'overview')){ loadActivityBoard(); }")
  || script.includes("if(tab === 'accounts' || (!_accountsSeparateTab && tab === 'gateway')){ loadActivityBoard(); }"),
  '切到账号页（或合并后的网关/总览页）应触发 loadActivityBoard');

// 签到记录区块的骨架
for(const id of ['activityRange', 'activityTask', 'activityResult', 'activityAccount',
                 'activityList', 'activityMore', 'activityMoreInfo', 'activityCount']){
  assert.ok(html.includes('id="' + id + '"'), '签到记录区块缺少 #' + id);
}
assert.ok(/id="btnActivityMore"[^>]*onclick="loadActivityBoard\(false, true\)"/.test(html),
  '「加载更多」应接上 loadActivityBoard(false, true)');
assert.ok(/id="activityRange"[\s\S]{0,600}?<option value="7d" selected>/.test(html),
  '默认区间应是近 7 天');
// 筛选值的取值必须与服务端约定一致，写错了服务端会回 400
for(const v of ['today', '1d', '7d', '30d', '90d', 'all']){
  assert.ok(new RegExp('<option value="' + v + '"[ >]').test(html), '区间下拉缺少 ' + v);
}
for(const v of ['checkin', 'daily_chat']){
  assert.ok(new RegExp('<option value="' + v + '"[ >]').test(html), '任务下拉缺少 ' + v);
}
assert.ok(/<option value="ok"[ >]/.test(html) && /<option value="failed"[ >]/.test(html),
  '结果下拉应提供 ok / failed');

// 英文词条齐全，否则切到 EN 时整块是中文
for(const [zh, en] of [['签到与活跃记录', 'Check-in & activity log'],
                       ['时间区间', 'Time range'],
                       ['全部任务', 'All tasks'],
                       ['国内版签到', 'China check-in'],
                       ['国际版每日活跃', 'Global daily activity'],
                       ['加载更多', 'Load more'],
                       ['来源', 'Trigger'],
                       ['网关', 'Gateway'],
                       ['这个区间里没有签到或活跃记录。', 'No check-in or activity records in this range.']]){
  assert.ok(new RegExp("'" + zh + "':\\s*'" + en.replace(/[&.]/g, m => '\\' + m) + "'").test(html),
    'DICT 里缺少「' + zh + '」的英文词条');
}

/* ---- 1b. 开关：账号是不是独立成一个页签 ----------------------------- */

// 开关本体：勾选框 + 保存按钮
assert.ok(/<input id="setAccountsSeparate" type="checkbox"/.test(html),
  '设置页缺少「账号单独一页」勾选框 #setAccountsSeparate');
assert.ok(/onclick="saveAccountsSeparateTab\(this\)"/.test(html),
  '开关的保存按钮应接上 saveAccountsSeparateTab(this)');
// 合并模式下三个区块要插回网关页的这个位置——拆分前它们就在这里
assert.ok(/<span id="accountsSectionsAnchor" hidden><\/span>/.test(html),
  '网关页缺少账号区块的合并锚点 #accountsSectionsAnchor');
// 拆开时区块要回到账号页页头之后
assert.ok(/<span id="accountsHomeAnchor" hidden><\/span>/.test(html),
  '账号页缺少区块的回家锚点 #accountsHomeAnchor');
// 页签文字要被改写，所以得有个能定位的节点，不能只留一段裸文本
assert.ok(/<span id="navGatewayLabel">网关<\/span>/.test(html),
  '网关页签的文字节点缺少 id，合并时改不成「网关与账号」');

// 字段名必须与 wb_settings / wb_proxy 一致：写错一个字母就是「开关存了，
// 下次打开又是老样子」这种最安静的坏法。
assert.ok(/data\.accounts_separate_tab === true/.test(script),
  'loadSettings 应按 accounts_separate_tab === true 判定，缺省即合并');
assert.ok(/postJSON\('\/settings\/save', \{accounts_separate_tab: value\}\)/.test(script),
  '保存应只 patch accounts_separate_tab 一个键');
// 合并之后「账号」的书签不能停在内容已经不在了的空页上
assert.ok(/if\(tab === 'accounts' && !_accountsSeparateTab\) tab = window\.__NEXT_UI__ \? 'overview' : 'gateway';/.test(script),
  '合并模式下 ?tab=accounts 应退回「网关与账号」/总览页');

/* ---- 1c. 一键切换所有出站身份 --------------------------------------- */

// 按钮在账号工具栏里，紧跟「一键分配代理」
assert.ok(/id="btnProductAll"[^>]*onclick="cycleAllProducts\(this\)"/.test(html),
  '账号工具栏缺少「一键切换所有出站身份」按钮');
assert.ok(/一键分配代理<\/button>\s*<button class="sec" id="btnProductAll"/.test(html),
  '按钮应排在「一键分配代理」之后');
// 当前身分要有个能定位的节点，不能只留一段裸文本（否则 JS 改不动它）
assert.ok(html.includes('<span id="productAllLabel">'), '按钮上的当前身分缺少可定位节点');
// 批量靠「不带 uid」：带上 uid 就退化成单账号切换
assert.ok(/postJSON\('\/accounts\/product', \{product: next\}\)/.test(script),
  '批量切换只应提交 product，不能带 uid');
// 身分取值与轮转顺序必须与服务端 wb_identity.VALID_PRODUCTS 一致
assert.ok(/PRODUCT_ORDER = \['workbuddy', 'vscode', 'cli'\]/.test(script),
  '轮转顺序应与服务端身分取值一致');
// 各账号身分不一致时不能显示成其中某一种
assert.ok(/cur \? PRODUCT_SHORT\[cur\] : '混合'/.test(script),
  '身分不一致时应显示「混合」');
// 英文词条齐全，否则切到 EN 时这一块是中文
for(const [zh, en] of [['一键切换所有出站身份：', 'Switch all outbound identities: '],
                       ['混合', 'mixed'],
                       ['将把全部账号的出站身份切换为 ', 'Switch the outbound identity of ALL accounts to '],
                       ['？（含另一个版本）', '? (including the other version)'],
                       ['已把全部账号的出站身份切换为 ', 'Switched the outbound identity of ALL accounts to ']]){
  assert.ok(html.includes("'" + zh + "': '" + en + "'"),
    'DICT 里缺少「' + zh + '」的英文词条');
}


/* ---- 2. 行为：拼查询串、渲染、翻页、出错 ---------------------------- */

let POSTS = [];
let REQUESTS = [];
let RESPONSE = {rows: [], total: 0};
let FETCH_FAILS = false;
dom.installDom({
  confirm: () => true,
  fetch: (url, opts) => {
    if(opts && opts.method === 'POST'){
      POSTS.push({url: String(url), body: String(opts.body || '')});
    }
    REQUESTS.push(String(url));
    if(FETCH_FAILS){
      return Promise.resolve({ok: false, status: 400,
        json: () => Promise.resolve({}), text: () => Promise.resolve('')});
    }
    return Promise.resolve({ok: true, status: 200,
      json: () => Promise.resolve(RESPONSE), text: () => Promise.resolve('')});
  },
});

const realLog = console.log;
console.log = () => {};
let api;
try {
  api = new Function(script + `
    return {
      loadActivityBoard: loadActivityBoard,
      renderActivityAccounts: renderActivityAccounts,
      switchMainTab: switchMainTab,
      applyAccountsTabLayout: applyAccountsTabLayout,
      accountsSeparateTab: accountsSeparateTab,
      updateProductAllButton: updateProductAllButton,
      cycleAllProducts: cycleAllProducts,
    };`)();
} catch(e) {
  console.log = realLog;
  console.error('脚本求值失败:', e.message);
  process.exit(1);
}

const el = id => document.getElementById(id);
const listHtml = () => el('activityList').innerHTML;
function check(label, ok, detail){
  assert.ok(ok, label + (detail === undefined ? '' : '  -> ' + detail));
}

(async () => {
  // 2a. 默认筛选：近 7 天、100 行；没有记录时给出空态而不是一张空表
  el('activityRange').value = '7d';
  el('activityTask').value = '';
  el('activityResult').value = '';
  el('activityAccount').value = '';
  REQUESTS = [];
  RESPONSE = {rows: [], total: 0};
  await api.loadActivityBoard();
  check('默认请求近 7 天、100 行', REQUESTS[0] === '/activity/history?range=7d&limit=100', REQUESTS[0]);
  check('空结果给出空态', listHtml().includes('这个区间里没有签到或活跃记录。'), listHtml());
  check('空结果不显示加载更多', el('activityMore').style.display === 'none');
  check('空结果不显示计数', el('activityCount').textContent === '', el('activityCount').textContent);

  // 2b. 四个筛选条件都进查询串，空值不占位
  el('activityRange').value = '30d';
  el('activityTask').value = 'checkin';
  el('activityResult').value = 'failed';
  el('activityAccount').value = 'uid-abcdef1234';
  REQUESTS = [];
  await api.loadActivityBoard();
  check('筛选条件进查询串',
    REQUESTS[0] === '/activity/history?range=30d&task=checkin&result=failed&uid=uid-abcdef1234&limit=100',
    REQUESTS[0]);

  // 2c. 一行记录渲染成人能读的七列
  el('activityTask').value = '';
  el('activityResult').value = '';
  el('activityAccount').value = '';
  RESPONSE = {total: 2, limit: 100, rows: [
    {ts: '2026-10-09T21:03:34+08:00', uid: 'uid-abcdef1234', nickname: '老王',
     realm: 'cn', task: 'checkin', trigger: 'scheduler', ok: true,
     message: '签到成功，奖励 30 积分'},
    {ts: '2026-10-08T08:00:00+08:00', uid: 'uid-abcdef1234', nickname: '',
     realm: 'intl', task: 'daily_chat', trigger: 'manual', ok: false, message: ''},
  ]};
  await api.loadActivityBoard();
  const h = listHtml();
  check('时间按落盘的墙上时间显示', h.includes('10-09 21:03'), h.slice(0, 160));
  check('昵称进账号列', h.includes('老王'));
  check('昵称标记为不翻译', h.includes('data-no-i18n') && h.includes('>老王</span>'));
  check('没有昵称时回落到 uid 前 8 位', h.includes('uid-abcd'), h.slice(0, 400));
  check('区域列', h.includes('国内版') && h.includes('国际版'));
  check('任务列', h.includes('国内版签到') && h.includes('国际版每日活跃'));
  check('来源列', h.includes('定时') && h.includes('手动'));
  check('结果徽章', h.includes('<span class="badge ok">成功</span>')
                 && h.includes('<span class="badge bad">失败</span>'));
  check('说明列', h.includes('签到成功，奖励 30 积分'));
  check('计数显示总数', el('activityCount').textContent === '(共 2 条)', el('activityCount').textContent);
  check('全部取回时不显示加载更多', el('activityMore').style.display === 'none');

  // 2d. 总数大于已取回：显示「加载更多」，点一下按页加 limit
  RESPONSE = {total: 250, limit: 100, rows: Array.from({length: 100}, (_, i) => ({
    ts: '2026-10-09T00:00:00+08:00', uid: 'u' + i, nickname: 'a' + i, realm: 'cn',
    task: 'checkin', trigger: 'scheduler', ok: true, message: 'ok'}))};
  await api.loadActivityBoard();
  check('还有更多时显示加载更多', el('activityMore').style.display === 'flex');
  check('更多提示带已显示与总数',
    el('activityMoreInfo').textContent === '已显示最新 100 条 / 共 250 条',
    el('activityMoreInfo').textContent);
  REQUESTS = [];
  await api.loadActivityBoard(false, true);
  check('加载更多按页加 limit',
    REQUESTS[0] === '/activity/history?range=30d&limit=200', REQUESTS[0]);
  check('加载更多不清空已有表格', listHtml().includes('<tbody>'), listHtml().slice(0, 80));

  // 2e. limit 有上限，点到底也不会无限往上加
  for(let i = 0; i < 12; i++) await api.loadActivityBoard(false, true);
  check('limit 封顶在 1000', REQUESTS[REQUESTS.length - 1] === '/activity/history?range=30d&limit=1000',
    REQUESTS[REQUESTS.length - 1]);

  // 2f. 换筛选条件要回到第一页，否则第一屏会缺最新记录
  el('activityRange').value = 'today';
  REQUESTS = [];
  await api.loadActivityBoard();
  check('换筛选条件重置 limit', REQUESTS[0] === '/activity/history?range=today&limit=100', REQUESTS[0]);

  // 2g. 账号下拉取自账号池，重建选项不能把已选中的账号冲掉
  window.ACCOUNTS = [{uid: 'uid-abcdef1234', nickname: '老王'},
                     {uid: 'uid-2', nickname: '老李'}];
  el('activityAccount').value = 'uid-2';
  api.renderActivityAccounts();
  const opts = el('activityAccount').innerHTML;
  check('账号下拉包含账号池里的账号', opts.includes('老王') && opts.includes('老李'), opts);
  check('账号选项标记为不翻译', opts.includes('data-no-i18n'), opts);
  check('重建选项后保留已选中的账号', el('activityAccount').value === 'uid-2',
    el('activityAccount').value);

  // 2h. 服务端拒绝筛选值（400）时给出提示，并把刷新按钮放开
  FETCH_FAILS = true;
  await api.loadActivityBoard();
  check('读取失败时给出提示', listHtml().includes('读取签到记录失败'), listHtml());
  check('失败后刷新按钮恢复可用', el('btnActivityRefresh').disabled === false);
  FETCH_FAILS = false;

  // 2i. 切到账号页会拉起记录
  REQUESTS = [];
  api.switchMainTab('accounts');
  check('切到账号页触发一次读取',
    REQUESTS.some(u => u.indexOf('/activity/history') === 0), JSON.stringify(REQUESTS));

  // 2j. 开关：区块在两页之间搬家，页签名与「账号」页签的显隐跟着走。
  // 此段验证用户显式选择经典布局时的文案；默认控制台另行验证。
  document.documentElement.removeAttribute('data-console-layout');
  // 假 DOM 不把 HTML 解析成树（getElementById 是惰性建元素），所以这里按**运行时**
  // 的真实结构自己搭最小场景——initPageNav() 会把页面内容整体包进 .page-nav-body，
  // 两个锚点因此都不是页面 div 的直接子节点。踩过的坑：直接对页面 div 调
  // insertBefore 会抛 NotFoundError，而 loadSettings 的 try/catch 把它吞掉，
  // 现场表现只是「开关点了一点反应都没有」。
  const pageGateway = el('pageGateway');
  const pageAccounts = el('pageAccounts');
  const kids = node => Array.from(node.children);
  const bodyOf = node => kids(node).find(n => n.classList && n.classList.contains('page-nav-body'));

  const accountsBody = document.createElement('div');
  accountsBody.classList.add('page-nav-body');
  pageAccounts.appendChild(accountsBody);
  const accountsHeader = document.createElement('div');
  accountsBody.appendChild(accountsHeader);
  const home = el('accountsHomeAnchor');
  accountsBody.appendChild(home);
  const moved = [0, 1, 2].map(() => document.createElement('section'));
  moved.forEach(s => accountsBody.appendChild(s));

  const gatewayBody = document.createElement('div');
  gatewayBody.classList.add('page-nav-body');
  pageGateway.appendChild(gatewayBody);
  const anchor = el('accountsSectionsAnchor');
  gatewayBody.appendChild(anchor);

  const sectionsOfAccounts = () => {
    const body = bodyOf(pageAccounts);
    return body ? kids(body).filter(n => n.tagName === 'SECTION') : [];
  };

  check('场景就位：账号页（含包裹层）里有三个区块', sectionsOfAccounts().length === 3,
    String(sectionsOfAccounts().length));
  check('合并锚点不在页面 div 直下，而在 .page-nav-body 里',
    kids(pageGateway).indexOf(anchor) === -1 && kids(gatewayBody).indexOf(anchor) >= 0);

  api.applyAccountsTabLayout(false);
  check('合并后账号页不再有区块', sectionsOfAccounts().length === 0,
    String(sectionsOfAccounts().length));
  check('合并后三个区块都挂在网关页的包裹层里',
    moved.every(s => kids(gatewayBody).includes(s)));
  check('合并后区块排在锚点之前',
    moved.every(s => kids(gatewayBody).indexOf(s) < kids(gatewayBody).indexOf(anchor)));
  check('合并后「账号」页签被藏起来', el('btnNavAccounts').style.display === 'none',
    el('btnNavAccounts').style.display);
  check('合并后网关页签改名「网关与账号」', el('navGatewayLabel').textContent === '网关与账号',
    el('navGatewayLabel').textContent);
  check('合并后开关读回来是关', api.accountsSeparateTab() === false);

  // 再调一次必须还是同一个结果：每次进设置页 loadSettings 都会调
  api.applyAccountsTabLayout(false);
  check('重复应用合并是幂等的',
    moved.every(s => kids(gatewayBody).includes(s)) && sectionsOfAccounts().length === 0);

  api.applyAccountsTabLayout(true);
  check('拆开后三个区块回到账号页', sectionsOfAccounts().length === 3,
    String(sectionsOfAccounts().length));
  check('拆开后顺序不变', sectionsOfAccounts().join('|') === moved.join('|'));
  check('拆开后网关页签改回「网关」', el('navGatewayLabel').textContent === '网关',
    el('navGatewayLabel').textContent);
  check('拆开后开关读回来是开', api.accountsSeparateTab() === true);

  document.documentElement.setAttribute('data-console-layout', 'on');
  api.applyAccountsTabLayout(false);
  check('控制台布局保持运行概览文案', el('navGatewayLabel').textContent === '运行概览');
  check('控制台布局仍将账号区合并到网关页', moved.every(s => kids(gatewayBody).includes(s)));


  // 2k. 一键切换所有出站身份：按钮显示当前身分，点一下切到下一个并提交全量
  window.ACCOUNTS = [{uid: 'u1', product: 'workbuddy'}, {uid: 'u2', product: 'workbuddy'}];
  api.updateProductAllButton();
  check('身分一致时按钮显示该身分', el('productAllLabel').textContent === 'WB',
    el('productAllLabel').textContent);

  window.ACCOUNTS = [{uid: 'u1', product: 'workbuddy'}, {uid: 'u2', product: 'cli'}];
  api.updateProductAllButton();
  check('身分不一致时显示「混合」', el('productAllLabel').textContent === '混合',
    el('productAllLabel').textContent);
  // 缺 product 字段的账号按 WB 算（与行内按钮的判定一致）
  window.ACCOUNTS = [{uid: 'u1'}, {uid: 'u2', product: 'workbuddy'}];
  api.updateProductAllButton();
  check('缺 product 字段按 WB 算', el('productAllLabel').textContent === 'WB',
    el('productAllLabel').textContent);

  // 混合时点一下 = 统一到轮转的第一项
  window.ACCOUNTS = [{uid: 'u1', product: 'workbuddy'}, {uid: 'u2', product: 'cli'}];
  RESPONSE = {accounts: [], rows: [], total: 0};
  POSTS = [];
  await api.cycleAllProducts(null);
  check('混合时切到 WB',
    POSTS.length === 1 && POSTS[0].url === '/accounts/product'
    && JSON.parse(POSTS[0].body).product === 'workbuddy', JSON.stringify(POSTS));

  // 一致时按 WB -> VSC -> CLI -> WB 轮转，且不带 uid
  for(const [from, to] of [['workbuddy', 'vscode'], ['vscode', 'cli'], ['cli', 'workbuddy']]){
    window.ACCOUNTS = [{uid: 'u1', product: from}, {uid: 'u2', product: from}];
    POSTS = [];
    await api.cycleAllProducts(null);
    const body = POSTS.length === 1 ? JSON.parse(POSTS[0].body) : {};
    check(from + ' 之后切到 ' + to,
      POSTS.length === 1 && POSTS[0].url === '/accounts/product'
      && body.product === to && body.uid === undefined, JSON.stringify(POSTS));
  }

  // 一个账号都没有时不发请求（服务端全量切换等于空转）
  window.ACCOUNTS = [];
  POSTS = [];
  await api.cycleAllProducts(null);
  check('没有账号时不发请求', POSTS.length === 0, JSON.stringify(POSTS));


  console.log = realLog;
  console.log('ok - accounts page & activity history (structure + behaviour)');
})().catch(e => {
  console.log = realLog;
  console.error(e && e.stack ? e.stack : e);
  process.exit(1);
});
