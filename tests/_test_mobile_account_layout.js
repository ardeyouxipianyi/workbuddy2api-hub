/* 手机端账号区的三处布局回归（都只在 ≤640px 生效，桌面不受影响）。
 *
 * 1) 批量操作那一组有 6 个按钮，一行摆不下时每个按钮被压成竖排文字
 *    （「一 / 键 / 分 / 配 / 代 / 理」）。窄屏改成两列网格 → 3 行 2 列，
 *    按钮文字保持横排可读。
 * 2) 账号卡片第一行塞了昵称、备注、分组、任务开关、版本、状态、账户类型，
 *    长昵称时整行顶出卡片宽度（超屏）。任务开关与版本/状态/类型收到第二行
 *    单独显示，第一行只留昵称 / 备注 / 分组。
 * 3) 操作区两层 flex 容器各自换行时，出站身份那组会整块另起一行、中间空出
 *    一段。两行容器拆平（display:contents），按钮按 DOM 顺序自然流动。
 *
 * 桌面布局必须保持不变：.acct-mobile-meta 用 display:contents 拆平，任务开关
 * 仍跟在账号名后面；两行操作容器在桌面上仍是两行。
 *
 * Run with Node: node tests/_test_mobile_account_layout.js
 */
'use strict';
const assert = require('assert');
const {dashboardHtml} = require('./_dashboard_source.js');
const html = dashboardHtml();

/* ---- 1. 批量操作组：两列网格，文字不竖排 ---------------------------- */

assert.ok(/class="tb-group tb-bulk"/.test(html),
  '批量操作那一组缺少 .tb-bulk 标记，窄屏网格规则会落到所有 tb-group 上');
assert.ok(/#accountsBody \.toolbar \.tb-bulk\{display:grid;grid-template-columns:repeat\(2,minmax\(0,1fr\)\)/
    .test(html),
  '窄屏下 .tb-bulk 应改成两列网格（6 个按钮排成 3 行 2 列）');
assert.ok(/#accountsBody \.toolbar \.tb-bulk button\.sec\{[^}]*white-space:nowrap/.test(html),
  '网格里的按钮必须 white-space:nowrap，否则名称又被压成竖排');

// 6 个按钮都在这一组里：一键分配代理 / 一键切换所有出站身份 / 全部启用 /
// 全部停用 / 当前分组启用 / 当前分组停用。
const bulkAt = html.indexOf('class="tb-group tb-bulk"');
assert.ok(bulkAt > 0, '找不到 .tb-bulk 组');
const bulkBlock = html.slice(bulkAt, html.indexOf('</div>', bulkAt));
for (const label of ['一键分配代理', '一键切换所有出站身份', '全部启用', '全部停用',
                     '当前分组启用', '当前分组停用']) {
  assert.ok(bulkBlock.includes(label), '批量操作组里缺少「' + label + '」');
}

/* ---- 2. 账号卡第二行：任务开关 + 版本/状态/类型 ---------------------- */

assert.ok(html.includes('<span class="acct-mobile-meta">'),
  '账号卡缺少 .acct-mobile-meta 容器，任务开关与徽章无法收到第二行');
assert.ok(/\.acct-mobile-meta\{display:contents\}/.test(html),
  '桌面必须用 display:contents 把 .acct-mobile-meta 拆平，否则任务开关会掉到新行');
assert.ok(/\.acct-mobile-meta\{display:flex[^}]*flex-wrap:wrap/.test(html),
  '窄屏下 .acct-mobile-meta 应是一行可换行的 flex 容器');
// 只给 width:100% 不够：它是 nowrap flex 的子项，仍会与昵称同排。必须
// flex-basis:100% 强制独占一行，并让账号名那行允许换行。
assert.ok(/\.acct-mobile-meta\{[^}]*flex-basis:100%/.test(html),
  '.acct-mobile-meta 需要 flex-basis:100%，否则第二行不会真正另起一行');
assert.ok(/table\.account-cards td:nth-child\(2\) \.acct-name-row\{flex-wrap:wrap\}/.test(html),
  '窄屏账号名那行要允许换行，第二行才能落到下面');

// 顺序契约：昵称行先渲染（含 + 分组），第二行容器在它之后。
const nameRowAt = html.indexOf('class="acct-name-row"');
const metaAt = html.indexOf('class="acct-mobile-meta"');
assert.ok(nameRowAt > 0 && metaAt > nameRowAt,
  '第二行容器必须排在账号名那一行之后');

/* ---- 3. 操作区拆平：没有中间空块 ------------------------------------ */

assert.ok(/table\.account-cards \.acct-actions-row\{display:contents\}/.test(html),
  '窄屏下操作区的两行容器应拆平，避免出站身份整块另起一行留出空段');

// 按钮顺序：导出紧跟同步昵称，删除排在出站身份之后。
const syncAt = html.indexOf('>同步昵称<');
const exportAt = html.indexOf('>导出<', syncAt);
const idGroupAt = html.indexOf('id-btn-group', syncAt);
const deleteAt = html.indexOf('>删除<', idGroupAt);
assert.ok(syncAt > 0 && exportAt > syncAt,
  '「导出」应排在「同步昵称」之后（同一行按钮流里紧挨着）');
assert.ok(idGroupAt > exportAt,
  '出站身份组应排在「导出」之后');
assert.ok(deleteAt > idGroupAt,
  '「删除」应排在出站身份组之后');

/* ---- 4. 桌面布局没被改动 -------------------------------------------- */

// 桌面（≥641px）两行操作容器保持原样：.acct-actions-row 仍是 flex。
assert.ok(/table\.account-cards \.acct-actions-row\{flex-wrap:wrap;gap:3px\}/.test(html),
  '桌面操作区的两行紧凑布局被改掉了');
assert.ok(/table\.account-cards \.acct-actions-col\{min-width:max-content;max-width:none\}/.test(html),
  '桌面操作列宽度规则被改掉了');
// 移动端徽章组在桌面上仍隐藏（版本/状态有自己的两列）。
assert.ok(/\.acct-mobile-badges\{display:none\}/.test(html),
  '桌面必须隐藏移动端徽章组，否则版本/状态会重复显示');

console.log('_test_mobile_account_layout.js: all assertions passed');
