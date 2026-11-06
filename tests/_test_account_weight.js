/* 账号手动权重：看板列 + 下拉 + /accounts/set 落库。
 *
 * 权重是面板上用户能直接改的分派意图：账号表要有「权重」列（下拉 1-10），
 * 改动走 POST /accounts/set {uid, weight}，服务端持久化由
 * tests/_test_manual_weight.py 钉住。这里钉界面部分：
 *
 *   1. 表头有「权重」列，行里渲染下拉，当前值被选中，超出 1-10 的历史值
 *      补一个选中项而不是悄悄跳回档位；
 *   2. 改动发出正确的请求体；
 *   3. 三语词典都有「权重」与提示语的条目（EN 整句、Hant 整句）。
 *
 * Run with Node: node tests/_test_account_weight.js
 */
'use strict';
const assert = require('assert');
const {dashboardHtml, dashboardScript} = require('./_dashboard_source.js');
const dom = require('./_dom_stub.js');

const html = dashboardHtml();
const script = dashboardScript();

/* ---- 1. 结构：账号表有「权重」列，改动走 /accounts/set -------------- */

assert.ok(/<th style="text-align:center;width:78px">权重<\/th>/.test(html),
  '账号表应有 78px 宽的「权重」表头（够放自绘下拉的值 + 箭头）');
/* 回归：64px 的列减去 td 内边距只剩 40px，通用触发器的 padding/gap/箭头
   吃掉 37px，权重数字被挤成 1px（界面上只剩一个箭头）。这里钉住该列的
   紧凑触发器与放宽后的列宽。 */
assert.ok(/table\.account-cards td\[data-label="权重"\] \.cs-trigger\{padding:3px 6px;gap:4px\}/
  .test(html), '权重列的触发器应收紧 padding/gap，给数字留出宽度');
assert.ok(/const WEIGHT_TIP = /.test(script), '应定义 WEIGHT_TIP 提示语');
assert.ok(script.includes("title=\"' + esc(WEIGHT_TIP) + '\""),
  '权重下拉应把 WEIGHT_TIP 挂在 title 上（title 才会被 i18n 翻译）');
assert.ok(/onchange="setAccountWeight/.test(html.replace(/\n/g, '')) ||
  /onchange="setAccountWeight/.test(script),
  '权重下拉应挂 setAccountWeight');
assert.ok(/async function setAccountWeight\(uid, weight, sel\)/.test(script),
  '应定义 setAccountWeight(uid, weight, sel)');

/* 三语：EN 词典整句、Hant 短语表整句（逐字转换会把「账」转成「賬」， */
/* 而站内惯用「帳號」，所以提示语必须有整句条目）。                  */
assert.ok(/'权重': 'Weight'/.test(script), 'EN 词典应有「权重」条目');
const tip = '权重 >1 的账号优先接单，多个高权重账号按权重比分流量；它们都接不了单时其他账号照常顶上。1 = 不干预，临期积分优先等自动规则照旧。';
assert.ok(script.includes("'" + tip + "': 'Accounts with weight >1"),
  'EN 词典应有权重提示语整句条目');
assert.ok(script.includes('"' + tip + '": "權重 >1 的帳號優先接單'),
  'Hant 短语表应有权重提示语整句条目');

/* ---- 2. 行为：下拉渲染与请求体 ------------------------------------- */

let REQUESTS = [];
let RESPONSE = {};
dom.installDom({
  fetch: (url, opts) => {
    REQUESTS.push({url: String(url), opts: opts || {}});
    return Promise.resolve({ok: true, status: 200,
      json: () => Promise.resolve(RESPONSE), text: () => Promise.resolve(JSON.stringify(RESPONSE))});
  },
});

const realLog = console.log;
console.log = () => {};
let api;
try {
  api = new Function(script + `
    return {
      accountRow: accountRow,
      setAccountWeight: setAccountWeight,
      renderAccounts: renderAccounts,
    };`)();
} catch(e) {
  console.log = realLog;
  console.error('脚本求值失败:', e.message);
  process.exit(1);
}
console.log = realLog;

function rowHtml(weight){
  return api.accountRow({
    uid: 'uid-weight-1', nickname: 'n', realm: 'cn', enabled: true,
    source: 'oauth', product: 'workbuddy', weight: weight,
  });
}

/* 权重 1（默认）到 10 都有选项，当前值被选中 */
assert.ok(rowHtml(undefined).includes('<option value="1" selected>'),
  '没设过权重的账号应选中默认值 1');
assert.ok(rowHtml(7).includes('<option value="7" selected>'),
  '权重 7 应选中 7');
assert.ok(!rowHtml(7).includes('<option value="7" >'), '同一档位不应重复渲染');
assert.ok(rowHtml(99).includes('<option value="99" selected>'),
  '超出 1-10 的历史值应补一个选中项，不能让 select 跳回档位');
assert.ok(rowHtml(3).includes('setAccountWeight(&quot;uid-weight-1&quot;'),
  '下拉 onchange 应带上本账号 uid');

/* ---- 2b. 表格排序：有备注置顶 → 权重高在前 → 启用在前 -------------- */

window.VIEW_REALM = 'intl';
window.ACCOUNTS = [
  {uid: 'ord-plain-a', nickname: 'n-plainA', realm: 'intl', enabled: true,  weight: 1},
  {uid: 'ord-heavy',   nickname: 'n-heavy',  realm: 'intl', enabled: true,  weight: 5},
  {uid: 'ord-noted',   nickname: 'n-noted',  realm: 'intl', enabled: true,  weight: 1, note: '自用'},
  {uid: 'ord-disabled',nickname: 'n-dis',    realm: 'intl', enabled: false, weight: 9},
  {uid: 'ord-disabled-eq', nickname: 'n-disEq', realm: 'intl', enabled: false, weight: 1},
];
api.renderAccounts();
{
  const box = document.getElementById('accounts');
  const out = String(box && box.innerHTML);
  const pos = name => out.indexOf(name);
  assert.ok(pos('n-noted') < pos('n-heavy'), '备注是加分项：启用组内有备注的排前面（w1 备注在 w5 之前）');
  assert.ok(pos('n-heavy') < pos('n-plainA'), '同无备注时权重 5 在权重 1 之前');
  assert.ok(pos('n-plainA') < pos('n-dis'), '停用的永远沉底（哪怕权重 9、有备注）');
  assert.ok(pos('n-dis') < pos('n-disEq'), '停用组内同规则：权重 9 在权重 1 之前');
}

/* ---- 3. setAccountWeight 发出正确的请求体 -------------------------- */

(async () => {
  RESPONSE = {account: {uid: 'uid-weight-1', weight: 3}};
  REQUESTS = [];
  await api.setAccountWeight('uid-weight-1', '3', null);
  const setCall = REQUESTS.find(r => r.url === '/accounts/set');
  assert.ok(setCall, '应 POST /accounts/set');
  const body = JSON.parse(setCall.opts.body);
  assert.deepStrictEqual(body, {uid: 'uid-weight-1', weight: 3},
    '请求体应是 {uid, weight:3}（数字而非字符串）');

  console.log('_test_account_weight.js: all assertions passed');
})().catch(e => { console.error(e.message); process.exit(1); });
