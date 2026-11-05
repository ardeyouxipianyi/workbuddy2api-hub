/* 面板闸门的首帧可见性，以及「登录失败必须把闸门放回屏幕」。

   两个问题同源：#panelGate 的可见性只由 panelNeedsLogin() 打开，markup 上是
   display:none。于是

   1. 首帧会先把整块看板骨架（静态标记，含空表、0 分卡片）画出来，等
      /panel/status 回来才被闸门盖住——无痕窗口 / 首次访问每次都能看到「登录后的
      界面」闪一下；
   2. 带 ?pwd= 自动登录且密码错时，页面不走 bootPanel 的未认证分支，闸门一直停在
      隐藏态：用户看到的是「看着已登录、其实进不去」的空看板，连重输的框都没有。

   修法是让闸门 fail-closed（markup 默认可见），只有「本标签页已经有面板令牌」时
   由 <head> 里那段同步脚本先隐藏，复核失败再由 panelNeedsLogin() 摘类显示。
   这里钉住三件事：markup 的默认态、首帧判定与 PANEL_STORE 的键一致、以及
   panelNeedsLogin() 确实会摘掉那个类（否则闸门永远出不来）。

   Run with: node tests/_test_panel_gate_first_paint.js
*/
'use strict';
const assert = require('assert');
const vm = require('vm');
const {dashboardHtml} = require('./_dashboard_source.js');
const {loadDashboard} = require('./_dashboard_sandbox.js');

const html = dashboardHtml();

/* ---- 1. markup：闸门默认可见（fail-closed） ---------------------------- */

const gateTag = html.match(/<div id="panelGate"[^>]*>/);
assert.ok(gateTag, 'dashboard.html 里应当有 #panelGate');
assert.ok(/display:flex/.test(gateTag[0]),
  '#panelGate 必须默认可见（display:flex）：默认 none 会让首帧先画出看板骨架再盖住');
assert.ok(!/display:none/.test(gateTag[0]),
  '#panelGate 不能默认 display:none（那正是闪烁的成因）');

/* ---- 2. 首帧判定在 <head>，且与 PANEL_STORE 用同一个令牌键 ------------- */

const headEnd = html.indexOf('</head>');
const headHtml = html.slice(0, headEnd);
assert.ok(/html\.wb-panel-session #panelGate\{display:none!important\}/.test(headHtml),
  '隐藏规则必须写在 <head>，并且带 !important（闸门的可见性在它自己的内联 style 上）');

const headScripts = [...headHtml.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const gateScripts = headScripts.filter(src => src.indexOf('wb-panel-session') !== -1);
assert.strictEqual(gateScripts.length, 1,
  '首帧判定应当正好是一段内联脚本（多段会让「哪一段负责隐藏」变得不清楚）');
const firstPaintScript = gateScripts[0];

const storeMatch = html.match(/const PANEL_STORE = '([^']+)'/);
assert.ok(storeMatch, '找不到 PANEL_STORE 常量');
const tokenKey = storeMatch[1];
assert.ok(firstPaintScript.indexOf("const PANEL_STORE = '" + tokenKey + "'") !== -1,
  'PANEL_STORE 必须定义在 <head> 的首帧脚本里：首帧判定要用它，之后所有读写复用同一个绑定');
assert.strictEqual(html.split("'" + tokenKey + "'").length - 1, 1,
  '令牌键的字面量全页只能出现一次（就是那处定义），其余一律写 PANEL_STORE');
assert.ok(html.slice(headEnd).indexOf('const PANEL_STORE =') === -1,
  'PANEL_STORE 不能在 <head> 之外再声明一次：同一个经典脚本作用域里会直接 SyntaxError');

/* ---- 3. 首帧判定的行为：无令牌不隐藏 / 有令牌先隐藏 / 读存储抛错不算崩 -- */

function runFirstPaint(tokenOrThrow, store) {
  const added = [];
  const sessionStorage = {
    getItem: (key) => {
      if (tokenOrThrow === 'throw') throw new Error('storage disabled');
      return store !== 'local' && tokenOrThrow && key === storeMatch[1] ? tokenOrThrow : null;
    },
  };
  const localStorage = {
    getItem: (key) => {
      if (tokenOrThrow === 'throw' || store === 'local-blocked') throw new Error('storage disabled');
      return store === 'local' && tokenOrThrow && key === storeMatch[1] ? tokenOrThrow : null;
    },
  };
  const sandbox = {
    sessionStorage,
    localStorage,
    document: {documentElement: {
      classList: {add: (name) => added.push(name)},
      setAttribute: () => {},
    }},
  };
  vm.createContext(sandbox);
  vm.runInContext(firstPaintScript, sandbox);
  return added;
}

assert.deepStrictEqual(runFirstPaint(null), [],
  '没有面板令牌时不能隐藏闸门（无痕 / 首次访问必须直接看到登录框）');
assert.deepStrictEqual(runFirstPaint('panel-token'), ['wb-panel-session'],
  '有面板令牌时先隐藏闸门，避免已登录用户反而闪一下登录框');
assert.deepStrictEqual(runFirstPaint('local-token', 'local'), ['wb-panel-session'],
  'localStorage 里的持久令牌也要在首帧隐藏闸门');
assert.deepStrictEqual(runFirstPaint('session-token', 'local-blocked'), ['wb-panel-session'],
  'localStorage 不可用时仍应识别 sessionStorage 中的令牌');
assert.deepStrictEqual(runFirstPaint('throw'), [],
  '读 sessionStorage 抛错（隐私模式）时按「没有令牌」处理，不能把页面弄崩');
assert.ok(!/<input id="panelRemember"[^>]*\bchecked\b/.test(html), '保持登录默认关闭');

/* ---- 4. 页面行为：两个开关闸门的函数都要摘掉那个类 -------------------- */

// /panel/status 故意永不落定：bootPanel() 的续体会调 panelNeedsLogin()，
// 让它跑起来会把「闸门为什么可见」搅成两件事，这里只测函数本身。
const stalledFetch = (url) => (String(url).indexOf('/panel/status') !== -1
  ? new Promise(() => {})
  : Promise.resolve({status: 401, ok: false, json: () => Promise.resolve({})}));

const page = loadDashboard({fetch: stalledFetch});
const root = page.document.documentElement;
const gate = page.document.getElementById('panelGate');

page.panelStoreToken('remember-token', true);
assert.strictEqual(page.localStorage.getItem('wb-proxy-panel-token'), 'remember-token',
  '保持登录时令牌写入 localStorage');
assert.strictEqual(page.sessionStorage.getItem('wb-proxy-panel-token'), null,
  '保持登录时不能同时留在 sessionStorage');
page.panelStoreToken('session-token', false);
assert.strictEqual(page.sessionStorage.getItem('wb-proxy-panel-token'), 'session-token',
  '不保持登录时令牌只写 sessionStorage');
assert.strictEqual(page.localStorage.getItem('wb-proxy-panel-token'), null,
  '不保持登录时必须清掉持久令牌');
page.panelClearToken();
assert.strictEqual(page.localStorage.getItem('wb-proxy-panel-token'), null);
assert.strictEqual(page.sessionStorage.getItem('wb-proxy-panel-token'), null);

root.classList.add('wb-panel-session');
page.panelNeedsLogin();
assert.ok(!root.classList.contains('wb-panel-session'),
  'panelNeedsLogin() 必须先摘掉 html.wb-panel-session，否则 !important 会压住内联的 display:flex');
assert.strictEqual(gate.style.display, 'flex', 'panelNeedsLogin() 要把闸门显示出来');

root.classList.add('wb-panel-session');
page.panelHideGate();
assert.ok(!root.classList.contains('wb-panel-session'),
  'panelHideGate() 也要摘类，让「有类 ⇔ 首帧判定隐藏了闸门」这条不变式只作用于启动阶段');
assert.strictEqual(gate.style.display, 'none', 'panelHideGate() 要把闸门藏起来');

/* ---- 5. ?pwd= 登录失败：闸门必须留在屏幕上（旧行为是永久空白看板） ---- */

(async () => {
  // 模拟「本标签页有陈旧令牌」：闸门已被首帧判定隐藏，然后自动登录失败。
  const loginPage = loadDashboard({
    fetch: (url) => (String(url).indexOf('/panel/status') !== -1
      ? new Promise(() => {})
      : Promise.resolve({
          status: 401, ok: false,
          json: () => Promise.resolve({error: {message: 'invalid panel password'}}),
        })),
  });
  const loginRoot = loginPage.document.documentElement;
  const loginGate = loginPage.document.getElementById('panelGate');
  const loginMsg = loginPage.document.getElementById('panelLoginMsg');
  loginRoot.classList.add('wb-panel-session');
  loginGate.style.display = 'none';
  loginPage.document.getElementById('panelPwdInput').value = 'wrong-password';

  await loginPage.submitPanelLogin();

  assert.ok(!loginRoot.classList.contains('wb-panel-session'),
    '登录失败后不能留着首帧判定的隐藏态');
  assert.strictEqual(loginGate.style.display, 'flex',
    '登录失败（?pwd= 密码错）后闸门必须在屏幕上，否则用户看到的是进不去的空看板');
  assert.ok(/invalid panel password/.test(loginMsg.textContent),
    '错误文案要留在闸门里给用户看');

  console.log('panel gate first-paint assertions passed');
})().catch(error => { console.error(error); process.exit(1); });
