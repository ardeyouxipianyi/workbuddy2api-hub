/* 侧栏导航的「当前页」必须同时体现在视觉与语义上。
 *
 * active 类只决定颜色，屏幕阅读器不读它；aria-current="page" 才是「你在哪」
 * 的语义标记（WCAG 2.4.8 Location）。两者一旦分叉，视力用户看到的高亮页和
 * 读屏用户听到的当前页就不是同一个地方，而这种分叉不会报错、也不会崩，只能
 * 靠断言抓。
 *
 * 这里钉住三件事：
 *
 * 1. switchMainTab() 给每个页面的按钮设 active 时，必须在同一处设/摘
 *    aria-current —— 两个属性同生同死，不能一条路径管颜色、另一条路径管语义。
 * 2. setConsoleNavActive() 清理时只能碰网关页内部的三个视图入口
 *    （运行概览 / 模型管理 / 请求记录）。它原先用 `.console-sidebar
 *    .main-nav-btn.active`，那条选择器会命中侧栏里所有一级页面按钮；而这个
 *    函数在网关视图每次重算时都会跑，于是「点设置 → 几帧后高亮消失」。
 * 3. 网关按钮的回填必须受当前页约束，否则它会和 switchMainTab() 的结论打架。
 *
 * Run with Node: node tests/_test_nav_aria_current.js
 */
'use strict';
const assert = require('assert');
const {dashboardHtml, dashboardScript} = require('./_dashboard_source.js');

const html = dashboardHtml();
const script = dashboardScript();

let pass = 0, fail = 0;
function check(label, ok, detail) {
  if (ok) { pass++; console.log('  [PASS] ' + label); }
  else { fail++; console.log('  [FAIL] ' + label + (detail === undefined ? '' : '  -> ' + detail)); }
}

/* ---- 1. 结构：每个一级页面按钮都必须是可被脚本寻址的 main-nav-btn ---- */

const NAV_IDS = [
  'btnNavGateway', 'btnNavAccounts', 'btnNavTasks',
  'btnNavMedia', 'btnNavAnalytics', 'btnNavLogs', 'btnNavSettings',
  'btnNavAgents', 'btnNavModels', 'btnNavRequests',
];
NAV_IDS.forEach(function(id){
  check('dashboard.html 里存在 #' + id,
    new RegExp('id="' + id + '"').test(html));
});

/* 首帧：脚本跑起来之前，静态 HTML 里那个带 active 的按钮也得带 aria-current。
   否则从解析出 markup 到 switchMainTab() 首次执行之间有一瞬语义缺失——视觉上
   已经高亮「网关」，读屏软件还不知道当前页是谁。 */
const staticActiveTags = html.match(/<button[^>]*class="[^"]*main-nav-btn[^"]*active[^"]*"[^>]*>/g) || [];
check('静态 HTML 里带 active 的导航按钮不超过一个',
  staticActiveTags.length <= 1,
  '一次只应有一个初始选中项，实际 ' + staticActiveTags.length);
if (staticActiveTags.length === 1) {
  check('静态 HTML 里那个 active 按钮同时带 aria-current="page"',
    /aria-current="page"/.test(staticActiveTags[0]),
    '首帧只高亮不标语义：脚本首跑前读屏软件读不到当前页');
}

/* ---- 2. switchMainTab：active 与 aria-current 必须同生同死 ------------- */

// 定位 tabs.forEach 那段
const tabLoop = /tabs\.forEach\(\(\[name, btn, page\]\)\s*=>\s*\{([\s\S]*?)\}\);/.exec(script);
check('能定位到 switchMainTab 里的 tabs.forEach 循环', !!tabLoop);

if (tabLoop) {
  const body = tabLoop[1];
  check('循环体里设置 active 类', /classList\.toggle\('active', on\)/.test(body));
  check('循环体里同时设置 aria-current',
    /setAttribute\('aria-current',\s*'page'\)/.test(body),
    '只设 active 不设 aria-current：读屏软件读不到当前页');
  check('循环体里同时摘除 aria-current',
    /removeAttribute\('aria-current'\)/.test(body),
    '切走时没摘：会同时存在多个 aria-current="page"');

  // 两个动作必须在同一个 if(btn) 分支里，不能一个受 btn 保护、另一个裸奔
  check('设置 active 与 aria-current 在同一个 if(btn) 分支内',
    /if\s*\(btn\)\s*\{[\s\S]*classList\.toggle\('active', on\)[\s\S]*aria-current[\s\S]*\}/.test(body),
    'aria-current 若不受 btn 存在性保护，btn 为 null 时会抛错');
}

/* ---- 3. setConsoleNavActive：只清网关页内部的三个视图入口 -------------- */

const setNav = /function setConsoleNavActive\(linkId, label\)\{([\s\S]*?)\n\}/.exec(script);
check('能定位到 setConsoleNavActive', !!setNav);

if (setNav) {
  // 注释先剥掉：注释里出现的旧写法不算数（这条断言就是在解释它为什么被换掉）
  const body = setNav[1]
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/\/\/[^\n]*/g, '');

  // 禁止使用会命中全部一级按钮的宽选择器
  check('清理时不得使用 .console-sidebar .main-nav-btn.active 这类宽选择器',
    !/\.console-sidebar\s+\.main-nav-btn\.active/.test(body),
    '该选择器会命中设置/日志等一级按钮，网关视图重算时把它们的高亮摘掉');

  // 必须只针对网关页内部的三个入口
  const narrow = /#btnNavGateway\.active[\s\S]*#btnNavModels\.active[\s\S]*#btnNavRequests\.active/.test(body)
              || /#btnNavGateway,\s*#btnNavModels,\s*#btnNavRequests/.test(body);
  check('清理范围限定为运行概览 / 模型管理 / 请求记录三个视图入口',
    narrow,
    '清理范围必须收窄，否则会误伤其它一级页面');

  // 网关按钮回填必须受 currentMainTab 约束
  check('网关按钮回填受 currentMainTab 约束',
    /currentMainTab\s*===\s*'gateway'/.test(body),
    '无条件回填会让任务/媒体/看板页的 aria-current 被改成「网关」');
}

/* ---- 4. syncConsoleContext：清理 active 时也要摘 aria-current ---------- */

const syncCtx = /function syncConsoleContext\(tab\)\{([\s\S]*?)\n\}/.exec(script);
check('能定位到 syncConsoleContext', !!syncCtx);

if (syncCtx) {
  const body = syncCtx[1];
  const clearsActive = /classList\.remove\('active'\)/.test(body);
  const clearsAria = /removeAttribute\('aria-current'\)/.test(body);
  check('syncConsoleContext 摘 active 的同时也摘 aria-current',
    !clearsActive || clearsAria,
    '只摘 active 不摘 aria-current：语义状态会比视觉状态活得久');
}

/* ---- 5. 全仓不变量：任何 removeAttribute('aria-current') 附近都该有 active */

console.log('');
console.log('SUMMARY: PASS=' + pass + ' FAIL=' + fail);
process.exit(fail ? 1 : 0);
