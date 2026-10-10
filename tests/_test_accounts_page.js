/* 账号页（issue #176）。
 *
 * issue 的两条诉求是「账号多起来一直向下排列不方便管理」和「看不出来签到成功
 * 没有」。折叠由 #226 落地、活动历史的落库与读接口由 #206 落地、历史的界面由
 * #261 落地（账号行的「今日活动」徽章 + 签到记录 modal）。本 PR 只做剩下那条：
 *
 *   1. 「账号」是一个独立的主菜单，账号列表与「当前禁用」总览都搬进 #pageAccounts，
 *      网关页不再留账号区；
 *   2. 两个主标签清单（head 里的 TABS 与主脚本的 MAIN_TABS）必须一致 —— 它们漂开
 *      的后果是刷新后停在一个不存在的页，或者菜单点不动；
 *   3. 设置页的开关决定账号区是独立成页还是留在「网关与账号」页，默认合并。
 *
 * 这些错法在页面上都只是「少了一列 / 点了没反应」，单看代码看不出来，所以既查
 * 结构也把整段脚本配假 DOM 跑起来。
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
// 上游的「任务与福利」页也必须留着 —— 它是另一个功能，我们只是往清单里插一项
assert.ok(names(mainTabs[1]).includes('tasks'), 'tasks 必须在主标签清单里');

/* ---- 1b. 开关：账号是不是独立成一个页签 ----------------------------- */

// 开关本体：勾选框 + 保存按钮
assert.ok(/<input id="setAccountsSeparate" type="checkbox"/.test(html),
  '设置页缺少「账号单独一页」勾选框 #setAccountsSeparate');
assert.ok(/onclick="saveAccountsSeparateTab\(this\)"/.test(html),
  '开关的保存按钮应接上 saveAccountsSeparateTab(this)');
// 合并模式下两个区块要插回网关页的这个位置——拆分前它们就在这里
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
assert.ok(/if\(tab === 'accounts' && !_accountsSeparateTab\) tab = 'gateway';/.test(script),
  '合并模式下 ?tab=accounts 应退回「网关与账号」页');

// 开关自己的英文词条，漏了切到 EN 时这半块还是中文
for(const [zh, en] of [['账号独立成一个页签', 'Accounts as a separate tab'],
                       ['账号单独一页', 'Accounts on their own page'],
                       ['网关与账号', 'Gateway & accounts'],
                       ['网关', 'Gateway']]){
  assert.ok(new RegExp("'" + zh + "':\\s*'" + en + "'").test(html),
    'DICT 里缺少「' + zh + '」的英文词条');
}

/* ---- 2. 行为：区块在两页之间搬家，页签名跟着走 ---------------------- */

dom.installDom({
  fetch: () => Promise.resolve({ok: true, status: 200,
    json: () => Promise.resolve({}), text: () => Promise.resolve('')}),
});

const realLog = console.log;
console.log = () => {};
let api;
try {
  api = new Function(script + `
    return {
      switchMainTab: switchMainTab,
      applyAccountsTabLayout: applyAccountsTabLayout,
      accountsSeparateTab: accountsSeparateTab,
    };`)();
} catch(e) {
  console.log = realLog;
  console.error('脚本求值失败:', e.message);
  process.exit(1);
}

const el = id => document.getElementById(id);
function check(label, ok, detail){
  assert.ok(ok, label + (detail === undefined ? '' : '  -> ' + detail));
}

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
const moved = [0, 1].map(() => document.createElement('section'));
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

check('场景就位：账号页（含包裹层）里有两个区块', sectionsOfAccounts().length === 2,
  String(sectionsOfAccounts().length));
check('合并锚点不在页面 div 直下，而在 .page-nav-body 里',
  kids(pageGateway).indexOf(anchor) === -1 && kids(gatewayBody).indexOf(anchor) >= 0);

api.applyAccountsTabLayout(false);
check('合并后账号页不再有区块', sectionsOfAccounts().length === 0,
  String(sectionsOfAccounts().length));
check('合并后两个区块都挂在网关页的包裹层里',
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
check('拆开后两个区块回到账号页', sectionsOfAccounts().length === 2,
  String(sectionsOfAccounts().length));
check('拆开后顺序不变', sectionsOfAccounts().join('|') === moved.join('|'));
check('拆开后网关页签改回「网关」', el('navGatewayLabel').textContent === '网关',
  el('navGatewayLabel').textContent);
check('拆开后开关读回来是开', api.accountsSeparateTab() === true);

console.log = realLog;
console.log('ok - accounts page & tab toggle (structure + behaviour)');
