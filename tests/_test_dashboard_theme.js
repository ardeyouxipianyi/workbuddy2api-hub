/* 看板主題選單：內聯處理器必須真的能點。
 *
 * 迴歸背景：主題程式碼整塊包在 head 的 IIFE 裡，而 `window.selectTheme` /
 * `window.toggleThemeMenu` 這兩行匯出被寫進了**函式體內部**——函式第一次被呼叫
 * 之前那個全域性名根本不存在，可按鈕寫的偏偏是 `onclick="toggleThemeMenu(event)"`，
 * 於是每次點選都拋 `ReferenceError: toggleThemeMenu is not defined`，下拉選單永遠
 * 打不開。同一塊裡的 `applyTheme` / `initThemeSystem` 寫在 IIFE 頂層，所以只有這
 * 兩個壞了——README 裡「淺色 / 深色 / 跟隨系統」三檔當時是點了沒反應的。
 *
 * `_test_dashboard_handlers.js` 抓不到它：那個掃描是純文字的，只要檔案裡存在
 * `function toggleThemeMenu(` 就算數，看不見作用域。這裡改用執行式驗證——把 head
 * 裡這段主題程式碼原樣抽出來，在 Node 裡配一套最小 DOM 樁跑一遍，釘住四件事：
 *   1. 頁面內聯屬性引用到的處理器，只要主題塊裡提到過，就必須真的掛在 window 上；
 *   2. 點按鈕能開合選單，`aria-expanded` 跟著走；
 *   3. 選一檔能落地（data-theme / data-theme-pref / localStorage）並收起選單；
 *   4. 首次繪製前就按儲存的偏好定好主題（防白閃），跟隨系統時讀 prefers-color-scheme。
 *
 * Run with Node: node tests/_test_dashboard_theme.js
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard.html'), 'utf8');

const START = "var THEME_KEY = 'wb-theme';";
const END = '// 3. Tab 初始化';
const start = html.indexOf(START);
const end = html.indexOf(END);
assert.ok(start > 0 && end > start,
  'dashboard.html 裡找不到主題塊，測試需要跟著改（找的是 var THEME_KEY / // 3. Tab 初始化）');
const source = html.slice(start, end);

// 頁面裡所有點選型 data-action 名稱（D3 nonce CSP 後不再有 inline 屬性）。
const ACTION_ATTR = /\sdata-action="([A-Za-z_$][\w$]*)"[^>]*?\sdata-on="click"/g;
const handlers = new Set([...html.matchAll(ACTION_ATTR)].map((m) => m[1]));
assert.ok(handlers.size >= 70,
  `只掃到 ${handlers.size} 個 data-action，是提取壞了不是頁面變了`);

function makeElement(id) {
  const classes = new Set();
  return {
    id,
    innerHTML: '',
    title: '',
    attrs: {},
    contains: () => false,
    setAttribute(k, v) { this.attrs[k] = v; },
    getAttribute(k) { return this.attrs[k]; },
    classList: {
      add(c) { classes.add(c); },
      remove(c) { classes.delete(c); },
      contains(c) { return classes.has(c); },
      toggle(c, force) {
        const on = force === undefined ? !classes.has(c) : !!force;
        if (on) classes.add(c); else classes.delete(c);
        return on;
      },
    },
  };
}

// 一套最小 DOM 樁：主題塊只碰這些
function makeHarness(seed) {
  const elements = new Map();
  for (const id of ['themeToggleIcon', 'themeToggleBtn', 'themeDropdown', 'themeDropdownWrap',
                    'themeOptLight', 'themeOptDark', 'themeOptSystem']) {
    elements.set(id, makeElement(id));
  }
  const docListeners = {};
  const mediaListeners = [];
  const media = { matches: !!seed.systemDark, addEventListener(type, fn) { mediaListeners.push(fn); } };
  const store = new Map(seed.stored === undefined ? [] : [['wb-theme', seed.stored]]);

  const documentElement = makeElement('html');
  documentElement.style = {};

  const win = { matchMedia: () => media };
  const doc = {
    documentElement,
    getElementById: (id) => elements.get(id) || null,
    addEventListener: (type, fn) => { (docListeners[type] = docListeners[type] || []).push(fn); },
  };
  const storage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => { store.set(k, String(v)); },
    removeItem: (k) => { store.delete(k); },
  };

  new Function('window', 'document', 'localStorage', source)(win, doc, storage);

  return {
    win, doc, documentElement, storage, store, elements,
    media, mediaListeners, docListeners,
    el: (id) => elements.get(id),
    fire: (type, event) => (docListeners[type] || []).forEach((fn) => fn(event)),
  };
}

const evt = () => ({ prevented: false, stopped: false, preventDefault() { this.prevented = true; },
                     stopPropagation() { this.stopped = true; } });

// ---- 1. 內聯屬性引用到的處理器，主題塊提到過就必須真的掛在 window 上 ----
const mentioned = new Set([
  ...[...source.matchAll(/function\s+([A-Za-z_$][\w$]*)\s*\(/g)].map((m) => m[1]),
  ...[...source.matchAll(/window\.([A-Za-z_$][\w$]*)\s*=/g)].map((m) => m[1]),
]);
const mustBeGlobal = [...mentioned].filter((name) => handlers.has(name)).sort();

// 這一條同時防止「提取壞了」：主題塊就該匯出這兩個處理器
assert.deepStrictEqual(mustBeGlobal, ['selectTheme', 'toggleThemeMenu'],
  `主題塊裡被內聯屬性引用的處理器應當恰好是 selectTheme / toggleThemeMenu，實得 ${mustBeGlobal.join(', ')}`);

const h = makeHarness({});
for (const name of mustBeGlobal) {
  assert.strictEqual(typeof h.win[name], 'function',
    `內聯處理器 ${name}() 在主題塊裡出現過，卻沒掛到 window 上——` +
    '內聯屬性只在全域性作用域找名字，宣告在 IIFE 裡等於不存在（v1.6.12 的按鈕就是這麼壞的）');
}

// ---- 2. 點按鈕能開合選單，aria-expanded 跟著走 ----
const menu = h.el('themeDropdown');
const btn = h.el('themeToggleBtn');
assert.strictEqual(menu.classList.contains('open'), false, '選單初始必須是收起的');

h.win.toggleThemeMenu(evt());
assert.strictEqual(menu.classList.contains('open'), true, '第一次點選應當展開選單');
assert.strictEqual(btn.getAttribute('aria-expanded'), 'true', '展開後 aria-expanded 應為 true');

h.win.toggleThemeMenu(evt());
assert.strictEqual(menu.classList.contains('open'), false, '再點一次應當收起選單');
assert.strictEqual(btn.getAttribute('aria-expanded'), 'false', '收起後 aria-expanded 應為 false');

// ---- 3. 選一檔能落地並收起選單 ----
h.win.toggleThemeMenu(evt());
h.win.selectTheme('dark', evt());
assert.strictEqual(h.documentElement.getAttribute('data-theme'), 'dark', '選深色後 data-theme 應為 dark');
assert.strictEqual(h.documentElement.getAttribute('data-theme-pref'), 'dark', '偏好應記為 dark');
assert.strictEqual(h.documentElement.style.colorScheme, 'dark', 'color-scheme 要同步，否則原生控制元件還是淺色');
assert.strictEqual(h.store.get('wb-theme'), 'dark', '偏好必須持久化到 localStorage');
assert.strictEqual(menu.classList.contains('open'), false, '選完應當收起選單');
assert.strictEqual(h.el('themeOptDark').classList.contains('active'), true, '深色那一檔要標成當前項');
assert.strictEqual(h.el('themeOptLight').classList.contains('active'), false, '其它檔不能殘留 active');
assert.strictEqual(btn.title, '顏色主題: 深色', '按鈕提示要說明當前檔位');

// ---- 4. 首次繪製前就定好主題；跟隨系統時讀 prefers-color-scheme ----
const darkOnLoad = makeHarness({ stored: 'dark' });
assert.strictEqual(darkOnLoad.documentElement.getAttribute('data-theme'), 'dark',
  '存過深色時，指令碼一跑完就該是深色（內聯在 body 之前就是為了防白閃）');

const sysDark = makeHarness({ stored: 'system', systemDark: true });
assert.strictEqual(sysDark.documentElement.getAttribute('data-theme'), 'dark',
  '跟隨系統時應當讀 prefers-color-scheme 而不是預設淺色');
assert.strictEqual(sysDark.documentElement.getAttribute('data-theme-pref'), 'system',
  '偏好本身要記成 system，不能塌成具體檔位');

// 系統偏好變化時，跟隨系統的那一檔要即時跟上
sysDark.win.initThemeSystem();
sysDark.media.matches = false;
sysDark.mediaListeners.forEach((fn) => fn({}));
assert.strictEqual(sysDark.documentElement.getAttribute('data-theme'), 'light',
  '系統切成淺色後看板要跟著切');
assert.strictEqual(sysDark.documentElement.getAttribute('data-theme-pref'), 'system',
  '跟著系統變不應把偏好改成 light');

// ---- 5. 點空白處 / 按 Esc 收起選單 ----
const outside = makeHarness({});
outside.win.initThemeSystem();
outside.win.toggleThemeMenu(evt());
outside.fire('click', { target: makeElement('body') });
assert.strictEqual(outside.el('themeDropdown').classList.contains('open'), false, '點選單外面應當收起');

outside.win.toggleThemeMenu(evt());
outside.fire('keydown', { key: 'Escape' });
assert.strictEqual(outside.el('themeDropdown').classList.contains('open'), false, '按 Esc 應當收起');

// 點在選單裡面（wrap 之內）不應被當成"點外面"
const inside = makeHarness({});
inside.win.initThemeSystem();
inside.el('themeDropdownWrap').contains = () => true;
inside.win.toggleThemeMenu(evt());
inside.fire('click', { target: inside.el('themeOptDark') });
assert.strictEqual(inside.el('themeDropdown').classList.contains('open'), true,
  '點選單自己身上不該收起選單');

console.log('dashboard theme assertions passed '
  + `(${mustBeGlobal.length} exported handlers, ${handlers.size} inline handlers swept)`);
