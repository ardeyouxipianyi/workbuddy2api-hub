/* 面板的四组子系统必须一直在页面上（issue #270 的守卫）。
 *
 * 6cd1bc0 合并 #267 时，dashboard.html 里四组功能被整块回退，CI 却全绿：其中最大的
 * 一组（#265 的表头排序，339 行）当初就没配任何套件，另外三组的套件也只在别处间接
 * 依赖它们。这类事故的坏法很安静——页面照常打开，只是少了东西，控制台一行报错都没有。
 *
 * 所以这里只钉「还在不在、接线断没断」，不重复各功能自己的行为断言：
 *   1. 表头排序子系统（SORT_* / applyTableSort / inferColKind / 点击接线）；
 *   2. 签到记录：账号表入口 + 弹窗 + 筛选（#261）；
 *   3. 设置页的更新检查卡片（#262）；
 *   4. 剩余用量优先调度开关（#263/#264）；
 * 外加两条合并事故的直接教训：
 *   5. 「顶部页签显示」区块必须在 #pageSettings 里（#268 曾被挤到运行日志页）；
 *   6. 顶层同名函数只能有一个定义 —— #261 的弹窗与 #267 的页内看板撞名时，后声明的
 *      会静默顶掉前一套，这正是那次事故的根因。
 *
 * Run with Node: node tests/_test_panel_subsystems.js
 */
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const html = require('./_dashboard_source.js').dashboardHtml();
const script = (html.match(/<script>([\s\S]*?)<\/script>/g) || []).join('\n');

function has(label, needle){
  assert.ok(html.includes(needle), 'dashboard.html 里找不到' + label + '：' + needle);
}

/* ---- 1. 表头排序（#265）---- */
for (const name of ['SORT_COL_KINDS', 'SORT_DENY_TABLES', 'SORT_WIDGET_SEL', 'SORT_HINT',
                    'function applyTableSort', 'function applyAllTableSorts',
                    'function inferColKind', 'function sortHeaderAt', 'function cellSortValue']) {
  has('排序子系统', name);
}
const sortCalls = (script.match(/applyTableSort\(/g) || []).length;
assert.ok(sortCalls >= 5,
  'applyTableSort 的调用点只剩 ' + sortCalls + ' 处：排序没有接到表格上');
assert.ok(/addEventListener\('click',\s*e\s*=>\s*\{[\s\S]{0,200}sortHeaderAt\(e\)/.test(script),
  '表头点击没有接到 sortHeaderAt：排序点不动');

/* ---- 2. 签到记录：入口 + 弹窗 + 筛选（#261）---- */
has('账号工具栏里的签到记录入口', 'onclick="openActivityHistory()"');
has('签到记录弹窗', 'id="activityHistoryModal"');
has('签到记录表体', 'id="activityHistoryRows"');
for (const name of ['function openActivityHistory', 'function closeActivityHistory',
                    'function setActivityFilter', 'function activityFilterQuery',
                    'function activityHistoryRow', 'function loadActivityToday', 'ACT_FILTER']) {
  has('签到记录', name);
}

/* ---- 3. 设置页的更新检查卡片（#262）---- */
has('更新检查卡片', 'id="setUpdateCurrent"');
has('更新检查开关', 'id="setUpdateCheckEnabled"');
has('立即检查更新按钮', 'onclick="checkUpdateNow(this)"');
for (const name of ['function loadUpdateStatus', 'function applyUpdateStatus',
                    'function checkUpdateNow', 'function saveUpdateCheckEnabled']) {
  has('更新检查', name);
}

/* ---- 4. 剩余用量优先调度开关（#263/#264）---- */
has('优先调度开关', 'id="setRemainingPriority"');
has('优先调度保存', 'function saveRemainingPriority');
has('优先调度设置键', 'remaining_priority_enabled');
has('剩余用量分区域表体', 'id="remainingTbodyIntl"');
has('模型筛选', 'function toggleRemainingModelFilter');

/* ---- 5. #268 的区块必须在设置页里 ---- */
const settingsAt = html.indexOf('<div id="pageSettings"');
const listAt = html.indexOf('id="setPagesList"');
assert.ok(settingsAt > 0 && listAt > settingsAt,
  '「顶部页签显示」区块不在 #pageSettings 里（#268 被挤出去过一次）');
const nextPageAt = html.indexOf('<div id="page', settingsAt + 10);
assert.ok(nextPageAt === -1 || listAt < nextPageAt,
  '「顶部页签显示」区块掉到别的页面里去了');

/* ---- 6. 顶层同名函数只能有一个定义 ---- */
const topLevel = {};
for (const m of script.matchAll(/^(?:async )?function ([A-Za-z_$][\w$]*)\s*\(/gm)) {
  topLevel[m[1]] = (topLevel[m[1]] || 0) + 1;
}
const dupes = Object.keys(topLevel).filter(name => topLevel[name] > 1);
assert.deepStrictEqual(dupes, [],
  '同一份文件里有重名的顶层函数，后声明的会静默顶掉前一套：' + dupes.join(', '));

console.log('panel subsystem assertions passed (' + sortCalls + ' sort call sites)' );
