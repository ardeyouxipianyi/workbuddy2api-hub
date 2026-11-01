/* CodeArts 第三出口：卡片、视图按钮、默认出口循环与区块显隐。
 *
 * 结构断言钉住入口和 id；行为断言把整段脚本放进假 DOM 跑一次 updateUI，
 * 确认 codearts 视图下 WorkBuddy 区块隐藏、CodeArts 区块出现，未配置时卡片隐藏。
 *
 * Run with Node: node tests/_test_codearts_switch.js
 */
'use strict';
const assert = require('assert');
const {dashboardHtml, dashboardScript} = require('./_dashboard_source.js');
const dom = require('./_dom_stub.js');

const html = dashboardHtml();
const script = dashboardScript();

/* ---- 1. 结构 ---- */
assert.ok(/id="tabCodearts"[^>]*onclick="switchViewRealm\('codearts'\)"/.test(html),
  '网关页缺少 CodeArts 出口卡片');
assert.ok(/id="accountsRealmCodearts"[^>]*onclick="switchViewRealm\('codearts'\)"/.test(html),
  '账号页缺少 CodeArts 视图按钮');
assert.ok(html.includes('id="codeartsSummary"'), '缺少 CodeArts 统计条');
assert.ok(html.includes('id="codeartsCards"'), '缺少 CodeArts 统计卡片容器');
assert.ok(html.includes('id="codeartsAccountsSection"'), '缺少 CodeArts 账号区');
assert.ok(html.includes('id="codeartsAccounts"'), '缺少 CodeArts 账号表容器');
assert.ok(html.includes('id="tabCodeartsCredits"'), '缺少 CodeArts 积分显示');
assert.ok(html.includes('<th>积分</th>'), 'CodeArts 账号表缺少积分列');
assert.ok(html.includes('<th>出口</th>'), 'CodeArts 账号表缺少出口列');
assert.ok(/codeartsSlotSelectHtml/.test(script), '缺少 CodeArts 出口选择器');
assert.ok(/function fmtCredits/.test(script), '缺少积分格式化函数');
assert.ok(html.includes('id="workbuddyAccountsSection"'), 'WorkBuddy 账号区缺少可定位 id');
assert.ok(html.includes('id="workbuddyDisabledSection"'), 'WorkBuddy 禁用区缺少可定位 id');
assert.ok(html.includes('id="workbuddyActivitySection"'), 'WorkBuddy 活动区缺少可定位 id');

assert.ok(/const nextGw = gwOptions\[\(gwIdx \+ 1\) % gwOptions\.length\]/.test(script),
  '默认出口应按服务端 options 循环（未配置时跳过 codearts）');
assert.ok(/window\.REALM_OPTIONS/.test(script),
  '默认出口循环应读取服务端返回的 options');
assert.ok(/if\(r === "codearts"\)\{/.test(script),
  'switchViewRealm 应有 codearts 分支');
assert.ok(/\[\'intl\', \'cn\', \'codearts\'\]\.includes\(viewFromUrl\)/.test(script),
  '?view=codearts 应被接受');

/* ---- 2. 行为 ---- */
dom.installDom({
  confirm: () => true,
  fetch: (url, opts) => {
    const payload = {current: 'intl', options: ['intl', 'cn', 'codearts'],
                     accounts: [], data: [], models: [], rows: [], total: 0};
    return Promise.resolve({
      status: 200, ok: true,
      json: () => Promise.resolve(payload),
      text: () => Promise.resolve(JSON.stringify(payload)),
    });
  },
});

const realLog = console.log;
console.log = () => {};
let api;
try {
  api = new Function(script + `
    window.CODEARTS_OVERVIEW = {online: true, stats: {total: 2, healthy: 1},
                                models: [{id: 'GLM-5.2'}], accounts: [],
                                base_url: 'http://codearts:7866'};
    window.REALM_OPTIONS = ['intl', 'cn', 'codearts'];
    return {updateUI: updateUI, setViewRealm: setViewRealm,
            switchViewRealm: switchViewRealm, realmLabel: realmLabel};
  `)();
} catch (e) {
  console.log = realLog;
  console.error('脚本求值失败:', e.message);
  process.exit(1);
}
console.log = realLog;

const byId = id => document.getElementById(id);

api.setViewRealm('codearts');
api.updateUI();
assert.notEqual(byId('tabCodearts').style.display, 'none',
  '已配置时 CodeArts 卡片应显示');
assert.equal(byId('codeartsAccountsSection').style.display, '',
  'CodeArts 视图应显示 CodeArts 账号区');
assert.equal(byId('workbuddyAccountsSection').style.display, 'none',
  'CodeArts 视图应隐藏 WorkBuddy 账号区');
assert.equal(byId('workbuddyDisabledSection').style.display, 'none',
  'CodeArts 视图应隐藏 WorkBuddy 禁用区');
assert.equal(byId('workbuddyActivitySection').style.display, 'none',
  'CodeArts 视图应隐藏 WorkBuddy 活动区');
assert.equal(byId('tabCodeartsHealthy').textContent, 1,
  'CodeArts 卡片应显示健康账号数');
assert.equal(byId('tabCodeartsModels').textContent, 1,
  'CodeArts 卡片应显示模型数');

api.setViewRealm('intl');
api.updateUI();
assert.equal(byId('codeartsAccountsSection').style.display, 'none',
  'WorkBuddy 视图应隐藏 CodeArts 账号区');
assert.equal(byId('workbuddyAccountsSection').style.display, '',
  'WorkBuddy 视图应显示 WorkBuddy 账号区');

window.REALM_OPTIONS = ['intl', 'cn'];
window.VIEW_REALM = 'intl';
api.updateUI();
assert.equal(byId('tabCodearts').style.display, 'none',
  '未配置时 CodeArts 卡片应隐藏');
assert.equal(byId('accountsRealmCodearts').style.display, 'none',
  '未配置时 CodeArts 视图按钮应隐藏');

assert.equal(api.realmLabel('codearts'), 'CodeArts', 'CodeArts 出口名称');
console.log('codearts switch assertions passed');
