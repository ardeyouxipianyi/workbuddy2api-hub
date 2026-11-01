/* 账号页自己的版本（国际版 / 国内版）切换。
 *
 * 账号列表、「当前禁用」与签到记录都按 window.VIEW_REALM 过滤，可切换视图的入口
 * 原本只有网关页那两张出口卡片——账号独立成页签之后，换一版得先跳回网关页，再跳
 * 回来。这里钉住补上的那个入口：
 *
 *   1. 控件在 #pageAccounts 里，两个按钮切的就是 switchViewRealm，与网关页两张
 *      卡片同一份状态，所以两边永远一起亮（updateUI 一处改，两边都跟着变）；
 *   2. 合并模式（设置里「账号独立成一个页签」关闭，默认）下账号区块搬到网关页，
 *      控件跟着搬，落在账号区块正上方；拆分时回到账号页页头右侧；
 *   3. 它切的是**视图**，不是网关默认出口——后者只由网关页那张卡片上的按钮改。
 *
 * 这些错法在页面上都只是「切了没反应」「两边显示的不是同一版」，单看代码看不出来，
 * 所以既查结构也把整段脚本配假 DOM 跑起来。
 *
 * Run with Node: node tests/_test_accounts_realm_switch.js
 */
'use strict';
const assert = require('assert');
const {dashboardHtml, dashboardScript} = require('./_dashboard_source.js');
const dom = require('./_dom_stub.js');

const html = dashboardHtml();
const script = dashboardScript();

/* ---- 1. 结构：控件在账号页里，接的是 switchViewRealm ------------------ */

const pageAccountsAt = html.indexOf('<div id="pageAccounts"');
const pageAnalyticsAt = html.indexOf('<div id="pageAnalytics"');
const switchAt = html.indexOf('id="accountsRealmSwitch"');
assert.ok(pageAccountsAt > 0 && pageAnalyticsAt > pageAccountsAt,
  '页面顺序应为 accounts → analytics');
assert.ok(switchAt > pageAccountsAt && switchAt < pageAnalyticsAt,
  '版本切换控件必须落在 #pageAccounts 里');
assert.equal(html.split('id="accountsRealmSwitch"').length, 1 + 1,
  '控件只能有一份，不能两处各写一个（合并模式靠搬，不靠复制）');

// 页头要有可定位的宿主节点：拆分时控件回到这里，靠 id 找，不靠位置。
assert.ok(/<div id="accountsHeader"[^>]*>/.test(html),
  '账号页页头缺少 #accountsHeader，控件搬回来时找不到家');
const headerAt = html.indexOf('id="accountsHeader"');
assert.ok(headerAt > pageAccountsAt && headerAt < switchAt,
  '控件应写在账号页页头里');

// 两个按钮分别切 intl / cn，且是同一份状态（同一个函数）
assert.ok(/id="accountsRealmIntl"[^>]*onclick="switchViewRealm\('intl'\)"/.test(html),
  '「国际版」按钮应接上 switchViewRealm(\'intl\')');
assert.ok(/id="accountsRealmCn"[^>]*onclick="switchViewRealm\('cn'\)"/.test(html),
  '「国内版」按钮应接上 switchViewRealm(\'cn\')');
assert.ok(/id="accountsRealmCodearts"[^>]*onclick="switchViewRealm\('codearts'\)"/.test(html),
  '「CodeArts」按钮应接上 switchViewRealm(\'codearts\')');
// 两组按钮（网关页卡片 + 账号页控件）都必须走同一个入口，否则两边会各记一份状态
assert.equal(html.split("onclick=\"switchViewRealm('intl')\"").length, 1 + 2,
  '切 intl 的入口应恰好两个：网关页卡片 + 账号页控件');
assert.equal(html.split("onclick=\"switchViewRealm('cn')\"").length, 1 + 2,
  '切 cn 的入口应恰好两个：网关页卡片 + 账号页控件');

// 高亮与 aria 由 updateUI 一处写，两边不能各写一份
assert.ok(/rsIntl\.classList\.toggle\("active", isIntlView\)/.test(script),
  'updateUI 应按 VIEW_REALM 点亮账号页控件');
assert.ok(/rsCn\.classList\.toggle\("active", isCnView\)/.test(script),
  'updateUI 应按 VIEW_REALM 点亮账号页控件');
assert.ok(/rsCa\.classList\.toggle\("active", isCodeartsView\)/.test(script),
  'updateUI 应按 VIEW_REALM 点亮 CodeArts 控件');
// 搬家的逻辑要挂在既有的布局函数上（两种布局共用一个入口）
assert.ok(/const realmSwitch = document\.getElementById\('accountsRealmSwitch'\);/.test(script),
  'applyAccountsTabLayout 应负责把控件搬到位');

// 英文词条：aria-label 也在被翻译的属性里，缺了就会漏出中文
for(const [zh, en] of [['版本视图', 'Realm view'],
                       ['国际版', 'Global'],
                       ['国内版', 'China']]){
  assert.ok(html.includes("'" + zh + "': '" + en + "'"),
    'DICT 里缺少「' + zh + '」的英文词条');
}

/* ---- 2. 行为：切换、同步、两种布局下的落点 --------------------------- */

let POSTS = [];
const realmState = {current: 'intl'};
dom.installDom({
  confirm: () => true,
  fetch: (url, opts) => {
    if(opts && opts.method === 'POST'){
      POSTS.push({url: String(url), body: String(opts.body || '')});
      const body = JSON.parse(String(opts.body || '{}'));
      if(body.realm) realmState.current = body.realm;
    }
    const payload = {current: realmState.current, accounts: [], slots: [], data: [],
                     results: [], byAccount: [], models: [], rows: [], total: 0};
    return Promise.resolve({
      status: 200, ok: true,
      json: () => Promise.resolve(payload),
      text: () => Promise.resolve(JSON.stringify(payload)),
    });
  },
});

// 两种布局要搬的节点：账号页页头、账号页里的区块、网关页的合并锚点。
const byId = id => document.getElementById(id);
const pageAccounts = byId('pageAccounts');
const pageGateway = byId('pageGateway');
const header = byId('accountsHeader');
const anchor = byId('accountsSectionsAnchor');
const realmSwitch = byId('accountsRealmSwitch');
const sectionA = dom.makeElement('section');
const sectionB = dom.makeElement('section');

pageAccounts.appendChild(header);
header.appendChild(realmSwitch);
pageAccounts.appendChild(sectionA);
pageAccounts.appendChild(sectionB);
pageGateway.appendChild(anchor);

const realLog = console.log;
console.log = () => {};
let api;
try {
  api = new Function(script + `
    window.updateUI = updateUI;
    window.toast = toast;
    return {
      updateUI: updateUI,
      switchViewRealm: switchViewRealm,
      toggleActiveGatewayRealm: toggleActiveGatewayRealm,
      applyAccountsTabLayout: applyAccountsTabLayout,
      view: () => window.VIEW_REALM,
      active: () => window.ACTIVE_GATEWAY_REALM,
    };`)();
} catch(e) {
  console.log = realLog;
  console.error('脚本求值失败:', e.message);
  process.exit(1);
}
console.log = realLog;

const activeOf = el => el.classList.contains('active');
const indexIn = (parent, el) => parent.children.indexOf(el);

(async () => {
  // 2a. 拆分模式：控件回到账号页页头
  api.applyAccountsTabLayout(true);
  assert.equal(realmSwitch.parentNode, header,
    '拆分模式下控件应在账号页页头里');
  assert.ok(header.children.indexOf(realmSwitch) >= 0, '控件应挂在页头下');

  // 2b. 合并模式：账号区块与控件一起搬到网关页，控件在区块正上方
  api.applyAccountsTabLayout(false);
  assert.equal(sectionA.parentNode, pageGateway, '合并模式下区块应搬到网关页');
  assert.equal(sectionB.parentNode, pageGateway, '合并模式下区块应搬到网关页');
  assert.equal(realmSwitch.parentNode, pageGateway,
    '合并模式下控件应跟到网关页');
  const anchorAt = indexIn(pageGateway, anchor);
  assert.ok(anchorAt > 0, '锚点应在网关页里');
  assert.ok(indexIn(pageGateway, sectionA) < anchorAt && indexIn(pageGateway, sectionB) < anchorAt,
    '账号区块应插在锚点之前');
  assert.ok(indexIn(pageGateway, sectionA) < indexIn(pageGateway, sectionB),
    '区块之间要保持原顺序');
  assert.ok(indexIn(pageGateway, realmSwitch) < indexIn(pageGateway, sectionA),
    '控件应排在账号区块之前');
  assert.equal(indexIn(pageGateway, realmSwitch), 0,
    '控件应是这一段的第一个节点（账号区块正上方）');

  // 2c. 切到国内版：账号页控件与网关页卡片一起亮，两边说的是同一版
  await api.switchViewRealm('cn');
  assert.equal(api.view(), 'cn', '切换后视图应为国内版');
  assert.ok(activeOf(byId('accountsRealmCn')), '账号页「国内版」应高亮');
  assert.ok(!activeOf(byId('accountsRealmIntl')), '账号页「国际版」应熄灭');
  assert.equal(byId('accountsRealmCn').getAttribute('aria-pressed'), 'true',
    'aria-pressed 应跟着走');
  assert.equal(byId('accountsRealmIntl').getAttribute('aria-pressed'), 'false',
    'aria-pressed 应跟着走');
  assert.ok(activeOf(byId('tabCn')), '网关页国内版卡片应高亮');
  assert.ok(!activeOf(byId('tabIntl')), '网关页国际版卡片应熄灭');
  assert.equal(byId('tabCnViewBadge').style.display, 'inline-block',
    '网关页「当前视图」标记应落在国内版');
  assert.equal(byId('tabIntlViewBadge').style.display, 'none',
    '网关页国际版不该还挂着「当前视图」');

  // 2d. 切回国际版：对称
  await api.switchViewRealm('intl');
  assert.equal(api.view(), 'intl', '切换后视图应为国际版');
  assert.ok(activeOf(byId('accountsRealmIntl')), '账号页「国际版」应高亮');
  assert.ok(!activeOf(byId('accountsRealmCn')), '账号页「国内版」应熄灭');
  assert.ok(activeOf(byId('tabIntl')), '网关页国际版卡片应高亮');

  // 2e. 同一个入口幂等：已经是这一版时再点一次不炸、也不换版
  await api.switchViewRealm('intl');
  assert.equal(api.view(), 'intl', '重复点当前版本应保持不动');

  // 2f. 从网关页改默认出口：视图与两边控件同样要跟上（反向同步）
  realmState.current = 'intl';
  api.applyAccountsTabLayout(true);
  window.ACTIVE_GATEWAY_REALM = 'intl';
  window.VIEW_REALM = 'intl';
  api.updateUI();
  POSTS = [];
  await api.toggleActiveGatewayRealm();
  assert.equal(api.active(), 'cn', '默认出口应切到国内版');
  assert.equal(POSTS.length, 1, '应只提交一次 /realm');
  assert.ok(/^\/realm/.test(POSTS[0].url), '切换默认出口应打 /realm');
  assert.equal(api.view(), 'cn', '默认出口切换后视图跟到国内版');
  assert.ok(activeOf(byId('accountsRealmCn')), '账号页控件应跟上默认出口的切换');
  assert.equal(byId('tabCnDefaultBadge').style.display, 'inline-block',
    '网关页「默认出口」标记应落在国内版');

  // 2g. 控件只切视图，不该偷偷改默认出口
  realmState.current = 'intl';
  window.ACTIVE_GATEWAY_REALM = 'intl';
  POSTS = [];
  await api.switchViewRealm('cn');
  assert.equal(api.active(), 'intl', '在账号页切视图不得改网关默认出口');
  assert.equal(POSTS.length, 0, '切视图不该发任何写请求');

  console.log('accounts realm switch assertions passed');
})().catch(error => { console.log = realLog; console.error(error); process.exit(1); });
