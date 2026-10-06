/* 看板主题菜单：内联处理器必须真的能点。
 *
 * 回归背景：主题代码整块包在 head 的 IIFE 里，而 `window.selectTheme` /
 * `window.toggleThemeMenu` 这两行导出被写进了**函数体内部**——函数第一次被调用
 * 之前那个全局名根本不存在，可按钮写的偏偏是 `onclick="toggleThemeMenu(event)"`，
 * 于是每次点击都抛 `ReferenceError: toggleThemeMenu is not defined`，下拉菜单永远
 * 打不开。同一块里的 `applyTheme` / `initThemeSystem` 写在 IIFE 顶层，所以只有这
 * 两个坏了——README 里「浅色 / 深色 / 跟随系统」三档当时是点了没反应的。
 *
 * `_test_dashboard_handlers.js` 抓不到它：那个扫描是纯文本的，只要文件里存在
 * `function toggleThemeMenu(` 就算数，看不见作用域。这里改用执行式验证——把 head
 * 里这段主题代码原样抽出来，在 Node 里配一套最小 DOM 桩跑一遍，钉住四件事：
 *   1. 页面内联属性引用到的处理器，只要主题块里提到过，就必须真的挂在 window 上；
 *   2. 点按钮能开合菜单，`aria-expanded` 跟着走；
 *   3. 选一档能落地（data-theme / data-theme-pref / localStorage）并收起菜单；
 *   4. 首次绘制前就按存储的偏好定好主题（防白闪），跟随系统时读 prefers-color-scheme。
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
  'dashboard.html 里找不到主题块，测试需要跟着改（找的是 var THEME_KEY / // 3. Tab 初始化）');
const source = html.slice(start, end);

// 页面里所有内联属性引用到的函数名（与 _test_dashboard_handlers.js 同一套口径）
const HANDLER_ATTR = /\son(?:click|change|input|submit|keydown|keyup|blur|focus)\s*=\s*"([^"]*)"/g;
const CALL = /(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(/g;
const handlers = new Set();
for (const [, body] of html.matchAll(HANDLER_ATTR)) {
  for (const [, name] of body.matchAll(CALL)) handlers.add(name);
}
assert.ok(handlers.size >= 30,
  `只扫到 ${handlers.size} 个内联处理器，是提取坏了不是页面变了`);

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

// 一套最小 DOM 桩：主题块只碰这些
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

// ---- 1. 内联属性引用到的处理器，主题块提到过就必须真的挂在 window 上 ----
const mentioned = new Set([
  ...[...source.matchAll(/function\s+([A-Za-z_$][\w$]*)\s*\(/g)].map((m) => m[1]),
  ...[...source.matchAll(/window\.([A-Za-z_$][\w$]*)\s*=/g)].map((m) => m[1]),
]);
const mustBeGlobal = [...mentioned].filter((name) => handlers.has(name)).sort();

// 这一条同时防止「提取坏了」：主题块就该导出这两个处理器
assert.deepStrictEqual(mustBeGlobal, ['selectTheme', 'toggleThemeMenu'],
  `主题块里被内联属性引用的处理器应当恰好是 selectTheme / toggleThemeMenu，实得 ${mustBeGlobal.join(', ')}`);

const h = makeHarness({});
for (const name of mustBeGlobal) {
  assert.strictEqual(typeof h.win[name], 'function',
    `内联处理器 ${name}() 在主题块里出现过，却没挂到 window 上——` +
    '内联属性只在全局作用域找名字，声明在 IIFE 里等于不存在（v1.6.12 的按钮就是这么坏的）');
}

// ---- 2. 点按钮能开合菜单，aria-expanded 跟着走 ----
const menu = h.el('themeDropdown');
const btn = h.el('themeToggleBtn');
assert.strictEqual(menu.classList.contains('open'), false, '菜单初始必须是收起的');

h.win.toggleThemeMenu(evt());
assert.strictEqual(menu.classList.contains('open'), true, '第一次点击应当展开菜单');
assert.strictEqual(btn.getAttribute('aria-expanded'), 'true', '展开后 aria-expanded 应为 true');

h.win.toggleThemeMenu(evt());
assert.strictEqual(menu.classList.contains('open'), false, '再点一次应当收起菜单');
assert.strictEqual(btn.getAttribute('aria-expanded'), 'false', '收起后 aria-expanded 应为 false');

// ---- 3. 选一档能落地并收起菜单 ----
h.win.toggleThemeMenu(evt());
h.win.selectTheme('dark', evt());
assert.strictEqual(h.documentElement.getAttribute('data-theme'), 'dark', '选深色后 data-theme 应为 dark');
assert.strictEqual(h.documentElement.getAttribute('data-theme-pref'), 'dark', '偏好应记为 dark');
assert.strictEqual(h.documentElement.style.colorScheme, 'dark', 'color-scheme 要同步，否则原生控件还是浅色');
assert.strictEqual(h.store.get('wb-theme'), 'dark', '偏好必须持久化到 localStorage');
assert.strictEqual(menu.classList.contains('open'), false, '选完应当收起菜单');
assert.strictEqual(h.el('themeOptDark').classList.contains('active'), true, '深色那一档要标成当前项');
assert.strictEqual(h.el('themeOptLight').classList.contains('active'), false, '其它档不能残留 active');
assert.strictEqual(btn.title, '颜色主题: 深色', '按钮提示要说明当前档位');

// ---- 4. 首次绘制前就定好主题；跟随系统时读 prefers-color-scheme ----
const darkOnLoad = makeHarness({ stored: 'dark' });
assert.strictEqual(darkOnLoad.documentElement.getAttribute('data-theme'), 'dark',
  '存过深色时，脚本一跑完就该是深色（内联在 body 之前就是为了防白闪）');

const sysDark = makeHarness({ stored: 'system', systemDark: true });
assert.strictEqual(sysDark.documentElement.getAttribute('data-theme'), 'dark',
  '跟随系统时应当读 prefers-color-scheme 而不是默认浅色');
assert.strictEqual(sysDark.documentElement.getAttribute('data-theme-pref'), 'system',
  '偏好本身要记成 system，不能塌成具体档位');

// 系统偏好变化时，跟随系统的那一档要即时跟上
sysDark.win.initThemeSystem();
sysDark.media.matches = false;
sysDark.mediaListeners.forEach((fn) => fn({}));
assert.strictEqual(sysDark.documentElement.getAttribute('data-theme'), 'light',
  '系统切成浅色后看板要跟着切');
assert.strictEqual(sysDark.documentElement.getAttribute('data-theme-pref'), 'system',
  '跟着系统变不应把偏好改成 light');

// ---- 5. 点空白处 / 按 Esc 收起菜单 ----
const outside = makeHarness({});
outside.win.initThemeSystem();
outside.win.toggleThemeMenu(evt());
outside.fire('click', { target: makeElement('body') });
assert.strictEqual(outside.el('themeDropdown').classList.contains('open'), false, '点菜单外面应当收起');

outside.win.toggleThemeMenu(evt());
outside.fire('keydown', { key: 'Escape' });
assert.strictEqual(outside.el('themeDropdown').classList.contains('open'), false, '按 Esc 应当收起');

// 点在菜单里面（wrap 之内）不应被当成"点外面"
const inside = makeHarness({});
inside.win.initThemeSystem();
inside.el('themeDropdownWrap').contains = () => true;
inside.win.toggleThemeMenu(evt());
inside.fire('click', { target: inside.el('themeOptDark') });
assert.strictEqual(inside.el('themeDropdown').classList.contains('open'), true,
  '点菜单自己身上不该收起菜单');

console.log('dashboard theme assertions passed '
  + `(${mustBeGlobal.length} exported handlers, ${handlers.size} inline handlers swept)`);
