/* 顶部页签显示：默认全显示、能隐藏能恢复、清单跟着页签走，且收起来的页面切不过去。
 *
 * 这个开关很好写坏，坏法都很安静：清单在设置页里又硬编码一份页表（上游加一个页面
 * 就漏一个）；隐藏只收页签不收页面，于是「看不见入口但内容还在」；或者反过来只收
 * 页面不收页签，点进去一片空白；把「设置」也收起来，用户再没有任何地方能把它放回来；
 * 书签/上次停留的页签指向一个已收起的页面，点亮它又带着 hidden，屏幕全空；
 * 又或者偏好走了 localStorage —— 那样换一台浏览器就丢，也违反了「偏好存在部署自己
 * 的数据目录」这条既有约定（与账号区折叠、「(切换前)」行同一套通道）。
 *
 * 所以这里既看结构也跑行为：结构上钉住设置页那个区块、清单容器与那条 [hidden] 规则
 * （页签按钮自带 inline-flex、页面自带 display:block，作者样式会盖掉 [hidden] 的
 * UA 样式，少了这条规则隐藏根本不生效）；行为上把 dashboard 的整段脚本配一套假 DOM
 * 跑起来，喂不同的 /settings 载荷与页签结构，断言归一化、往返、失败回滚与切页拦截。
 *
 * Run with Node: node tests/_test_page_visibility.js
 */
'use strict';
const assert = require('assert');
const {dashboardHtml, dashboardScript} = require('./_dashboard_source.js');
const dom = require('./_dom_stub.js');

const html = dashboardHtml();
const script = dashboardScript();

function check(label, ok, detail) {
  assert.ok(ok, label + (detail === undefined ? '' : '  -> ' + detail));
}

/* ---- 1. 结构：设置页那个区块、清单容器与 [hidden] 规则 ------------------ */

check(/<h2>顶部页签显示 <em id="setPagesState"><\/em><\/h2>/.test(html),
  'dashboard.html 里找不到「顶部页签显示」区块标题');
check(/<div id="setPagesList"[^>]*><\/div>/.test(html),
  '清单容器 #setPagesList 必须是设置页里的空容器：选项由脚本现场生成');
// 页签按钮的 display 来自 .main-nav-btn（inline-flex），页面的来自 .main-page.active
// （block / grid）——都是作者样式，都压过 [hidden] 的 UA 样式。少了这条规则，
// 隐藏只会在 DOM 里生效，屏幕上看不出来。
check(/\.main-nav-btn\[hidden\],\.main-page\[hidden\]\{display:none!important\}/.test(html),
  '缺少 [hidden] 的 display 规则：作者样式会盖掉它，隐藏不生效');

const start = script.indexOf('顶部页签显示开始');
const end = script.indexOf('顶部页签显示结束');
check('页签显示块必须能被测试定位', start > 0 && end > start);
const block = script.slice(start, end);

// 偏好必须留在部署自己的数据目录里：块里不许出现浏览器存储。注释先剥掉，否则
// 「不用 localStorage」这句话本身就会误报。
const blockCode = block.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '');
check('页签显示块不得使用 localStorage / sessionStorage',
  !/localStorage|sessionStorage/.test(blockCode));
check('页签显示块必须把偏好发给服务端（/settings/save）',
  blockCode.indexOf("'/settings/save'") >= 0);
check('页签显示块不得自己写一份页表',
  !/MAIN_TABS/.test(blockCode) && !/\[[^[\]]*'(gateway|tasks|analytics|agents|logs)'/.test(blockCode),
  '选项必须由顶部页签现场读出，不能在这里再列一遍');

/* ---- 2. 行为：把整段 dashboard 脚本配假 DOM 跑起来 -------------------- */

/* 顶部页签：六个按钮，id 与文案都照 dashboard.html 里那份抄。
   __wbSrc 是 i18n 留在文本节点上的原文（界面切到英文时文案被换成译文，原文还在），
   这里给「设置」带上它，用来断言清单读的是原文而不是译文。 */
function textNode(text, source) {
  const node = {nodeType: 3, data: text, textContent: text};
  if (source !== undefined) node.__wbSrc = source;
  return node;
}

const PAGES = [
  ['btnNavGateway', '网关与账号'],
  ['btnNavTasks', '任务与福利'],
  ['btnNavAnalytics', '数据看板'],
  ['btnNavAgents', '智能体配置'],
  ['btnNavSettings', 'Settings', '设置'],
  ['btnNavLogs', '运行日志'],
];

const BUTTONS = PAGES.map(([id, text, source]) => {
  const btn = dom.createElement('button');
  btn.id = id;
  btn.className = 'main-nav-btn';
  btn.appendChild(textNode(' ' + text + ' ', source));
  btn.textContent = ' ' + text + ' ';
  return btn;
});

const nav = dom.makeElement('nav', {querySelectorAll: {'.main-nav-btn': BUTTONS}});
nav.className = 'main-nav';

let SETTINGS = {};
let SAVE_STATUS = 200;
let SAVE_REPLY = null;   // null = 回显请求体（等于服务端原样收下）
const calls = [];

const json = (status, payload) => Promise.resolve({
  status: status, ok: status >= 200 && status < 300,
  json: () => Promise.resolve(payload),
  text: () => Promise.resolve(JSON.stringify(payload)),
});

dom.installDom({
  querySelector: {'.main-nav': nav},
  fetch: (url, options) => {
    calls.push({url: url, body: options && options.body});
    if (url === '/settings') return json(200, SETTINGS);
    if (url === '/settings/save') {
      if (SAVE_STATUS !== 200) {
        return json(SAVE_STATUS, {error: {message: 'nope'}});
      }
      return json(200, SAVE_REPLY === null
        ? JSON.parse((options && options.body) || '{}')
        : SAVE_REPLY);
    }
    return json(200, {accounts: [], slots: [], results: [], authenticated: false});
  },
});

const realLog = console.log;
console.log = () => {};
let api;
try {
  api = new Function(script + `
    return {
      loadSettings: loadSettings,
      applyHiddenPages: applyHiddenPages,
      hiddenPages: hiddenPages,
      normaliseHiddenPages: normaliseHiddenPages,
      renderPageVisibility: renderPageVisibility,
      togglePageVisibility: togglePageVisibility,
      navPageRows: navPageRows,
      navPageLabel: navPageLabel,
      firstVisiblePage: firstVisiblePage,
      switchMainTab: switchMainTab,
      currentTab: function(){ return currentMainTab; },
    };`)();
} catch (e) {
  console.log = realLog;
  console.error('脚本求值失败:', e.message);
  process.exit(1);
}

const element = id => document.getElementById(id);
const list = () => element('setPagesList');
const boxes = () => Array.from(list().children).map(item => item.firstElementChild);
const boxFor = key => boxes().filter(box => box.dataset.pageKey === key)[0];
const navButton = key => BUTTONS.filter(btn => btn.id === 'btnNav' + key.charAt(0).toUpperCase() + key.slice(1))[0];
const pageFor = key => element('page' + key.charAt(0).toUpperCase() + key.slice(1));
const saves = () => calls.filter(c => c.url === '/settings/save');

(async () => {
  // 2a. 服务端没存过这个字段：全部显示，清单按页签现场生成。
  SETTINGS = {version: 'v1.6.19'};
  await api.loadSettings(true);
  check('缺省应全部显示', JSON.stringify(api.hiddenPages()) === '[]', JSON.stringify(api.hiddenPages()));
  check('清单应覆盖每个页签', list().children.length === PAGES.length,
    '渲染了 ' + list().children.length + ' 行');
  check('缺省时所有勾选框都勾上', boxes().every(box => box.checked === true));
  check('缺省时页签按钮都可见', BUTTONS.every(btn => !btn.hidden));
  check('缺省时页面容器都可见', PAGES.every(([id]) => !pageFor(id.slice(6).toLowerCase()).hidden));
  check('缺省时状态文案是「全部显示」',
    element('setPagesState').textContent === '(全部显示)',
    element('setPagesState').textContent);

  // 清单的标签来自页签按钮；带 __wbSrc 的读原文（中文），不能把译文抄进来。
  const labels = Array.from(list().children).map(item => item.children[1].textContent);
  assert.deepStrictEqual(labels, ['网关', '自动化', '用量', '智能体', '设置', '日志'],
    '清单标签必须与页签文案一一对应，且带 __wbSrc 的取原文: ' + JSON.stringify(labels));

  // 2b. 「设置」固定保留：它是这个开关自己的入口。
  check('「设置」的勾选框 disabled', boxFor('settings').disabled === true);
  check('其余页面的勾选框可用', PAGES.filter(([id]) => id !== 'btnNavSettings')
    .every(([id]) => boxFor(id.slice(6).toLowerCase()).disabled === false));

  // 2c. 存过一份清单：恢复，页签与页面一起收起。
  SETTINGS = {hidden_pages: ['tasks', 'logs']};
  await api.loadSettings(true);
  check('恢复已隐藏的清单',
    JSON.stringify(api.hiddenPages()) === JSON.stringify(['tasks', 'logs']),
    JSON.stringify(api.hiddenPages()));
  check('收起页签按钮', navButton('tasks').hidden === true && navButton('logs').hidden === true);
  check('收起页面容器', pageFor('tasks').hidden === true && pageFor('logs').hidden === true);
  check('没被收起的照常可见',
    navButton('gateway').hidden === false && pageFor('analytics').hidden === false);
  check('被收起的勾选框不勾', boxFor('tasks').checked === false && boxFor('logs').checked === false);
  check('没被收起的仍然勾着', boxFor('gateway').checked === true && boxFor('settings').checked === true);
  check('状态文案标出比例', element('setPagesState').textContent === '(显示 4/6)',
    element('setPagesState').textContent);

  // 2d. 存坏了：一律按「全部显示」算，且不能被真值字符串或页表外的 key 骗到。
  //     'settings' 单独列在这里：手改文件也不能把开关自己的入口关掉。
  for (const bad of ['tasks', 'settings', ['settings'], 1, 0, true,
                     null, {}, [], [1], [null], ['TASKS'], ['../etc'], ['has space'],
                     'yes']) {
    SETTINGS = {hidden_pages: bad};
    await api.loadSettings(true);
    check('损坏值应回落到全部显示: ' + JSON.stringify(bad),
      JSON.stringify(api.hiddenPages()) === '[]' && BUTTONS.every(btn => !btn.hidden),
      JSON.stringify(api.hiddenPages()));
  }
  // 逐项清洗：合法的留下，「设置」无论如何都丢掉，重复项并成一项。
  // 归一化本身：只留形状合法的 key，去重，「设置」无论如何都不收。
  assert.deepStrictEqual(api.normaliseHiddenPages(['logs', 'logs', 'settings', 'bad key', 7, 'ok_page']),
    ['logs', 'ok_page'], '归一化没按 wb_settings 的口径来');
  SETTINGS = {hidden_pages: ['settings', 'logs', 'logs']};
  await api.loadSettings(true);
  check('「设置」被丢掉、重复项并成一项',
    JSON.stringify(api.hiddenPages()) === JSON.stringify(['logs']),
    JSON.stringify(api.hiddenPages()));
  check('「设置」丢掉后仍然可见', pageFor('settings').hidden === false);

  // 2e. 显式往返：走既有 /settings/save 通道，一个键的 patch。
  SETTINGS = {};
  await api.loadSettings(true);
  calls.length = 0;
  await api.togglePageVisibility('logs');
  check('隐藏要 POST /settings/save', saves().length === 1, JSON.stringify(calls));
  assert.deepStrictEqual(JSON.parse(saves()[0].body), {hidden_pages: ['logs']},
    '保存的载荷必须只有这一个键');
  check('隐藏后页签收起', navButton('logs').hidden === true);
  check('隐藏后页面收起', pageFor('logs').hidden === true);
  check('隐藏后勾选框不勾', boxFor('logs').checked === false);

  await api.togglePageVisibility('logs');
  assert.deepStrictEqual(JSON.parse(saves()[saves().length - 1].body), {hidden_pages: []},
    '恢复要把空清单存回去');
  check('恢复后页签回显', navButton('logs').hidden === false && pageFor('logs').hidden === false);
  check('恢复后勾选框回勾', boxFor('logs').checked === true);

  // 2f. 服务端会丢掉不合法的 key，以它存的为准，免得两边记的不是一回事。
  calls.length = 0;
  SAVE_REPLY = {hidden_pages: ['analytics']};
  await api.togglePageVisibility('logs');
  check('服务端归一化后的清单盖过本地那份',
    JSON.stringify(api.hiddenPages()) === JSON.stringify(['analytics']),
    JSON.stringify(api.hiddenPages()));
  check('本地跟着服务端收起', navButton('analytics').hidden === true && navButton('logs').hidden === false);
  SAVE_REPLY = null;

  // 2g. 保存失败要回滚，不能只改了界面。
  SETTINGS = {};
  await api.loadSettings(true);
  SAVE_STATUS = 500;
  await api.togglePageVisibility('tasks');
  check('保存失败后回滚清单', JSON.stringify(api.hiddenPages()) === '[]',
    JSON.stringify(api.hiddenPages()));
  check('保存失败后页签回显', navButton('tasks').hidden === false && pageFor('tasks').hidden === false);
  check('保存失败后勾选框回勾', boxFor('tasks').checked === true);
  SAVE_STATUS = 200;

  // 2h. 被收起的页面切不过去：书签、上次停留的页签点亮它会让屏幕一片空白。
  api.applyHiddenPages(['tasks']);
  api.switchMainTab('tasks');
  check('switchMainTab 不能被调到已收起的页面', api.currentTab() !== 'tasks', api.currentTab());
  check('落到第一个还看得见的页面', api.currentTab() === 'gateway', api.currentTab());
  api.switchMainTab('logs');
  check('没被收起的页面照常能切', api.currentTab() === 'logs', api.currentTab());

  // 2i. 当前页被收起时退到第一个可见页（此时刻的清单里 gateway 是唯一可见的）。
  api.applyHiddenPages(['logs']);
  api.switchMainTab('logs');
  check('当前页被收起后退回可见页', api.currentTab() === 'gateway', api.currentTab());
  api.applyHiddenPages([]);

  // 2j. 清单是现场读页签的：上游加一个页面，这里自动多一行，不用改这份代码。
  const added = dom.createElement('button');
  added.id = 'btnNavScratch';
  added.className = 'main-nav-btn';
  added.appendChild(textNode('临时页面'));
  added.textContent = '临时页面';
  BUTTONS.push(added);
  api.renderPageVisibility();
  check('新页签自动进清单', list().children.length === PAGES.length + 1,
    '渲染了 ' + list().children.length + ' 行');
  check('新页签也参与隐藏', boxFor('scratch') !== undefined);
  api.applyHiddenPages(['scratch']);
  check('新页签能收起', element('pageScratch').hidden === true && added.hidden === true);
  api.applyHiddenPages([]);
  BUTTONS.pop();

  // 2k. 重建只在清单变了的时候做，重复 loadSettings 不得把行数翻倍。
  SETTINGS = {};
  await api.loadSettings(true);
  await api.loadSettings(true);
  await api.loadSettings(true);
  check('重复加载不重复渲染', list().children.length === PAGES.length,
    '渲染了 ' + list().children.length + ' 行');

  console.log = realLog;
  console.log('顶部页签显示断言通过（结构 7 项 + 行为 41 项）');
})().catch(e => {
  console.log = realLog;
  console.error(e && e.stack || e);
  process.exit(1);
});
