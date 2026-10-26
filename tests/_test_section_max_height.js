/* 面板窗口限高：所有窗口（区块 + 日志终端）超高时在窗口内部滚动、标题行吸顶，
   限高存在服务端，设置页能调，0 = 不限。

   为什么值得一个套件：
   - 限高是「三条 CSS 规则 + 一个服务端数字」的组合，缺一半都不生效：规则全部挂在
     body.win-capped 之下，类没挂上（或变量没写）整条落空；类挂上了但服务端存的
     值没读出来，用户改的高度重启就丢。
   - 吸顶要干净，两处折算缺一不可：区块自带的 padding-top 必须归零（否则吸顶后
     顶部漏出 18px 的滚动内容）、首行的 margin 必须折成 padding（否则下方那条缝
     同样漏内容）。两条都是「少写不会报错、只会难看」的规则，所以在这里钉住。
   - 默认 0（不限）：区块随页面滚动；用户设了正数才在区块内部滚动。字段缺失/存坏
     回落默认，与页签显示、账号折叠那几处的口径一致。
   - 日志终端自带 620px 高度，限高同样要盖得住它（max-height 只在更小时生效）。

   Run with Node: node tests/_test_section_max_height.js
*/
'use strict';
const assert = require('assert');
const {dashboardHtml, dashboardScript} = require('./_dashboard_source.js');
const dom = require('./_dom_stub.js');

const html = dashboardHtml();
const script = dashboardScript();

let checks = 0;
function check(label, ok, detail) {
  checks++;
  assert.ok(ok, label + (detail === undefined ? '' : '  -> ' + detail));
}

/* ---- 1. 结构：CSS 三条规则、设置页区块、i18n 词条 -------------------- */

check(/body\.win-capped \.main-page section\{max-height:var\(--win-max-h\);overflow-y:auto;padding-top:0\}/.test(html),
  'dashboard.html 里找不到区块限高规则（max-height + 内部滚动 + padding-top 归零）');
check(/body\.win-capped \.main-page section>:first-child\{position:sticky;top:0;z-index:3;/.test(html),
  '找不到吸顶首行规则：没有它标题行会跟着滚走');
check(/background:var\(--panel\);padding-top:18px;margin-bottom:0!important;padding-bottom:14px\}/.test(html),
  '吸顶首行必须有不透明底色、把区块的 padding-top 挪上来、并把 margin 折成 padding，否则缝隙漏内容');
check(/body\.win-capped #logTerminalBody\{max-height:var\(--win-max-h\)\}/.test(html),
  '日志终端（自带 620px 高度）也要盖得住：缺这条，限高调到 620 以下时终端不受限');
check(/body\.win-capped \.main-page section>:first-child\{padding-top:14px\}/.test(html),
  '窄屏（≤860px）区块 padding 是 14px，吸顶首行的 padding-top 要跟着缩');

check(/<h2>面板窗口限高 <em id="setWinMaxHeightState"><\/em><\/h2>/.test(html),
  'dashboard.html 里找不到「面板窗口限高」区块标题');
check(/<input id="setWinMaxHeight" type="number" min="0" max="4000" step="20" value="0"/.test(html),
  '找不到限高输入框 #setWinMaxHeight（或 min/max/step/默认值对不上）');
check(/onclick="saveWinMaxHeight\(this\)"/.test(html),
  '找不到保存按钮（saveWinMaxHeight）');

// 偏好必须留在部署自己的数据目录里：块里不许出现浏览器存储。注释先剥掉，否则
// 「不用 localStorage」这句话本身就会误报。
const start = script.indexOf('面板窗口限高开始');
const end = script.indexOf('面板窗口限高结束');
check('窗口限高块必须能被测试定位', start > 0 && end > start);
const block = script.slice(start, end);
const blockCode = block.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '');
check('窗口限高块不得使用 localStorage / sessionStorage',
  !/localStorage|sessionStorage/.test(blockCode));
check('窗口限高块必须把偏好发给服务端（/settings/save）',
  blockCode.indexOf("'/settings/save'") >= 0);
check('窗口限高块必须落在与 CSS 约定的变量/类名上',
  blockCode.indexOf("'--win-max-h'") >= 0 && blockCode.indexOf("'win-capped'") >= 0,
  'CSS 与 JS 各写一半，名字对不上就是整条落空');

// 词条：英文是整句替换，正体是整句短语，动态文案（toast 拼数字）也各钉一条。
check(/'面板窗口限高': 'Panel window height cap'/.test(html),
  '英文词典缺「面板窗口限高」');
check(/每个窗口（区块）超过这个高度时在窗口内部滚动，不再把整页拉长；滚动时标题行固定在窗口顶部。单位像素；填 0 表示不限。': 'Each window scrolls internally/.test(html),
  '英文词典缺限高说明');
check(/'已限高：窗口最高 ': 'Height cap on: windows up to '/.test(html),
  '英文词典缺保存成功的 toast 前缀（toast 由「前缀 + 数字 + px」拼出，前缀必须能整段命中）');
check(/'已取消窗口限高（不限）': 'Height cap removed \(no limit\)'/.test(html),
  '英文词典缺取消限高的 toast');
check(/"面板窗口限高": "面板視窗限高"/.test(html),
  '正體詞典缺「面板窗口限高」（窗口→視窗不是逐字映射，必须整句给出）');
check(/"已限高：窗口最高 ": "已限高：視窗最高 "/.test(html),
  '正體詞典缺保存成功的 toast 前缀');

/* ---- 2. 行为：把整段 dashboard 脚本配假 DOM 跑起来 -------------------- */

// loadSettings 会走一遍顶部页签显示（它按 .main-nav 现场读页签），给一个空导航即可。
const nav = dom.makeElement('nav', {querySelectorAll: {'.main-nav-btn': []}});
nav.className = 'main-nav';

let SETTINGS = {};
let SAVE_STATUS = 200;
let SAVE_REPLY = null;   // null = 回显请求体并落盘（等于服务端原样收下）
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
      if (SAVE_REPLY !== null) return json(200, SAVE_REPLY);
      const body = JSON.parse((options && options.body) || '{}');
      Object.assign(SETTINGS, body);   // 服务端收下并落盘
      return json(200, body);
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
      applyWinMaxHeight: applyWinMaxHeight,
      saveWinMaxHeight: saveWinMaxHeight,
      i18n: window.WB_I18N,
    };`)();
} catch (e) {
  console.log = realLog;
  console.error('脚本求值失败:', e.message);
  process.exit(1);
}

const element = id => document.getElementById(id);
const body = () => document.body;
const rootVar = () => document.documentElement.style.getPropertyValue('--win-max-h');
const toasts = () => {
  const box = element('toastBox');
  return Array.from(box.children).map(item => item.textContent);
};
const lastToast = () => toasts()[toasts().length - 1];
const saves = () => calls.filter(c => c.url === '/settings/save');
// 保存函数收尾会 fire-and-forget 一次 loadSettings（读回服务端最终值），等它落地
// 再断言，免得和回显应用抢最后一步。
const settle = () => new Promise(resolve => setImmediate(resolve));

(async () => {
  // 2a. 服务端没存过这个字段：默认不限高（区块随页面滚动，避免区块与表格各一根纵向滚动条）。
  SETTINGS = {pricing_enabled: false, version: 'v1.6.19'};
  await api.loadSettings(true);
  await settle();
  check('字段缺失时不挂限高类', body().classList.contains('win-capped') === false);
  check('字段缺失时不写变量', rootVar() === '', JSON.stringify(rootVar()));
  check('字段缺失时输入框填 0', element('setWinMaxHeight').value === '0',
    element('setWinMaxHeight').value);
  check('字段缺失时状态文案是 (不限)',
    element('setWinMaxHeightState').textContent === '(不限)',
    element('setWinMaxHeightState').textContent);

  // 2b. 存过 500：恢复。
  SETTINGS = {pricing_enabled: false, section_max_height: 500};
  await api.loadSettings(true);
  await settle();
  check('恢复已存的限高', rootVar() === '500px', rootVar());
  check('恢复后类仍然挂着', body().classList.contains('win-capped') === true);
  check('恢复后输入框跟着走', element('setWinMaxHeight').value === '500',
    element('setWinMaxHeight').value);
  check('恢复后状态文案跟着走',
    element('setWinMaxHeightState').textContent === '(当前 500px)',
    element('setWinMaxHeightState').textContent);

  // 2c. 0 = 不限：类摘掉、变量撤掉，版式回到从前。
  SETTINGS = {pricing_enabled: false, section_max_height: 0};
  await api.loadSettings(true);
  await settle();
  check('0 时不挂限高类', body().classList.contains('win-capped') === false);
  check('0 时变量撤掉', rootVar() === '', JSON.stringify(rootVar()));
  check('0 时状态文案是 (不限)',
    element('setWinMaxHeightState').textContent === '(不限)',
    element('setWinMaxHeightState').textContent);
  check('0 时输入框是 0', element('setWinMaxHeight').value === '0',
    element('setWinMaxHeight').value);

  // 2d. 存坏了：回落默认（不限），与页签显示、账号折叠的坏值口径一致。
  for (const bad of ['garbage', null, -5, NaN, Infinity, {}, []]) {
    SETTINGS = {pricing_enabled: false, section_max_height: bad};
    await api.loadSettings(true);
    await settle();
    check('坏值应回落默认（不限）: ' + JSON.stringify(bad),
      body().classList.contains('win-capped') === false && rootVar() === '',
      rootVar());
  }

  // 2e. 低层应用函数：数字直接落成变量与类，非数回落「不限」（调用方负责归一化）。
  api.applyWinMaxHeight(480);
  check('applyWinMaxHeight(480) 写变量', rootVar() === '480px', rootVar());
  check('applyWinMaxHeight(480) 挂类', body().classList.contains('win-capped') === true);
  check('applyWinMaxHeight(480.6) 取整', (api.applyWinMaxHeight(480.6), rootVar()) === '481px',
    rootVar());
  api.applyWinMaxHeight(0);
  check('applyWinMaxHeight(0) 摘类撤变量',
    body().classList.contains('win-capped') === false && rootVar() === '', rootVar());
  api.applyWinMaxHeight('garbage');
  check('applyWinMaxHeight 非数按不限处理',
    body().classList.contains('win-capped') === false && rootVar() === '', rootVar());

  // 2f. 保存：输入框的值发给服务端，回包与页面按它重放。
  SETTINGS = {pricing_enabled: false, section_max_height: 500};
  await api.loadSettings(true);
  await settle();
  element('setWinMaxHeight').value = '800';
  const before = saves().length;
  await api.saveWinMaxHeight(null);
  await settle();
  const posted = saves().slice(before);
  check('保存必须发一条 /settings/save', posted.length === 1, posted.length);
  check('保存的载荷只有 section_max_height',
    posted[0] && posted[0].body === JSON.stringify({section_max_height: 800}),
    posted[0] && posted[0].body);
  check('保存后按服务端值重放', rootVar() === '800px', rootVar());
  check('保存后状态文案更新',
    element('setWinMaxHeightState').textContent === '(当前 800px)',
    element('setWinMaxHeightState').textContent);
  check('保存成功的 toast', lastToast() === '已限高：窗口最高 800px', lastToast());

  // 2g. 服务端夹取（4000 上限）：页面听服务端的，不听输入框的。
  SETTINGS = {pricing_enabled: false, section_max_height: 4000};
  SAVE_REPLY = {section_max_height: 4000};
  element('setWinMaxHeight').value = '99999';
  await api.saveWinMaxHeight(null);
  await settle();
  check('超上限时按服务端夹取后的值重放', rootVar() === '4000px', rootVar());
  check('夹取后状态文案跟着走',
    element('setWinMaxHeightState').textContent === '(当前 4000px)',
    element('setWinMaxHeightState').textContent);
  SAVE_REPLY = null;

  // 2h. 0 走同一条通道：关掉限高。
  element('setWinMaxHeight').value = '0';
  await api.saveWinMaxHeight(null);
  await settle();
  check('保存 0 后限高关闭',
    body().classList.contains('win-capped') === false && rootVar() === '', rootVar());
  check('取消限高的 toast', lastToast() === '已取消窗口限高（不限）', lastToast());

  // 2i. 非法输入：本地就拦下，不发请求。
  for (const bad of ['-5', 'abc', '']) {
    const beforeBad = saves().length;
    element('setWinMaxHeight').value = bad;
    await api.saveWinMaxHeight(null);
    check('非法输入不发请求: ' + JSON.stringify(bad),
      saves().length === beforeBad, saves().length - beforeBad);
    check('非法输入给警告 toast: ' + JSON.stringify(bad),
      lastToast() === '请填写 0 或正数（像素）', lastToast());
  }

  // 2j. 保存失败：toast 报错，页面保持原样。
  SETTINGS = {pricing_enabled: false, section_max_height: 500};
  await api.loadSettings(true);
  await settle();
  SAVE_STATUS = 500;
  element('setWinMaxHeight').value = '700';
  await api.saveWinMaxHeight(null);
  await settle();
  check('保存失败给错误 toast', lastToast() === '保存失败: nope', lastToast());
  check('保存失败后页面不动', rootVar() === '500px', rootVar());
  SAVE_STATUS = 200;

  // 2k. 三语词条：英文整句、正体整句、动态 toast 拼接都要能命中。
  check('英文词条命中', api.i18n.translate('面板窗口限高') === 'Panel window height cap',
    api.i18n.translate('面板窗口限高'));
  check('英文动态 toast 命中',
    api.i18n.translate('已限高：窗口最高 800px') === 'Height cap on: windows up to 800px',
    api.i18n.translate('已限高：窗口最高 800px'));
  check('英文取消 toast 命中',
    api.i18n.translate('已取消窗口限高（不限）') === 'Height cap removed (no limit)',
    api.i18n.translate('已取消窗口限高（不限）'));
  check('英文状态文案命中（当前/不限复用既有词条）',
    api.i18n.translate('(当前 640px)') === '(Current 640px)',
    api.i18n.translate('(当前 640px)'));
  check('正体词条命中', api.i18n.translateHant('面板窗口限高') === '面板視窗限高',
    api.i18n.translateHant('面板窗口限高'));
  check('正体动态 toast 命中',
    api.i18n.translateHant('已限高：窗口最高 800px') === '已限高：視窗最高 800px',
    api.i18n.translateHant('已限高：窗口最高 800px'));
  check('正体状态文案命中',
    api.i18n.translateHant('(不限)') === '(不限)',
    api.i18n.translateHant('(不限)'));

  console.log = realLog;
  console.log(`面板窗口限高断言通过（${checks} 项）`);
})().catch(e => {
  console.log = realLog;
  console.error(e && e.stack || e);
  process.exit(1);
});
