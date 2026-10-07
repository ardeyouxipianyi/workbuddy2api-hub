/* 页面侧栏导航的前端契约：所有主页面共用一套「按区块现场生成」的导航。
 *
 * 侧栏不写死任何页面、也不写死任何区块：initPageNav() 拿一个 .main-page，
 * 扫它内部最外层的 section 生成导航项，标题取 section 首个 h2 的文本（忽略
 * 用来显示状态的 <em>），锚点 id 由标题内容哈希得到。因此增删区块、调整顺序、
 * 甚至以后新增一个页面，都不需要改导航代码——这正是要钉住的性质：
 *
 *   - 嵌套 section 不单独成项（它属于父区块的内容）
 *   - display:none 的区块不进导航，也不能当 #锚点 落点
 *   - 没有 h2 的 section 不成项；不足两项的页面干脆不给侧栏
 *   - 清单没变就不重建 DOM（否则轮询重建表格时高亮会抖）
 *   - 区块清单变了（新增/删除/整块显隐）就重建
 *
 * 这些错法都只会在页面上静默表现成「侧栏少一项 / 点了没反应」，单看代码看不出
 * 来，所以这里对着 dashboard.html 里真实的函数跑。Run with Node.
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard.html'), 'utf8');
const script = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)]
  .map(match => match[1]).join('\n');

/* ---- 一个只够跑导航的极简 DOM ---- */
const ALL = [];
function El(tag, ephemeral){
  this.tagName = String(tag).toUpperCase();
  this.nodeType = 1;
  this.children = [];
  this.parentElement = null;
  this.id = '';
  this._classes = [];
  this.dataset = {};
  this.attrs = {};
  this.listeners = {};
  this.rectTop = 0;
  this.offsetHeight = 40;
  this.scrollHeight = 1000;
  const self = this;
  this.style = { display: '', setProperty(k, v){ self.style[k] = v; },
                 getPropertyValue(k){ return self.style[k] || ''; } };
  this.classList = {
    add(...cs){ cs.forEach(c => { if(!self._classes.includes(c)) self._classes.push(c); }); },
    remove(...cs){ self._classes = self._classes.filter(c => !cs.includes(c)); },
    contains(c){ return self._classes.includes(c); },
    toggle(c, force){
      const has = self.classList.contains(c);
      const want = force === undefined ? !has : !!force;
      if(want && !has) self._classes.push(c);
      if(!want && has) self._classes = self._classes.filter(x => x !== c);
      return want;
    },
  };
  if(!ephemeral) ALL.push(this);
}
Object.defineProperty(El.prototype, 'className', {
  get(){ return this._classes.join(' '); },
  set(v){ this._classes = String(v).split(/\s+/).filter(Boolean); },
});
Object.defineProperty(El.prototype, 'firstChild', { get(){ return this.children[0] || null; } });
Object.defineProperty(El.prototype, 'textContent', {
  get(){ return this.children.map(c => (typeof c === 'string' ? c : c.textContent)).join(''); },
  set(v){ this.children = v === '' ? [] : [String(v)]; },
});
El.prototype.appendChild = function(node){
  if(typeof node !== 'string' && node.parentElement) node.parentElement.removeChild(node);
  if(typeof node !== 'string') node.parentElement = this;
  this.children.push(node);
  return node;
};
El.prototype.removeChild = function(node){
  this.children = this.children.filter(c => c !== node);
  if(typeof node !== 'string') node.parentElement = null;
};
El.prototype.insertBefore = function(node, ref){
  const at = this.children.indexOf(ref);
  if(at < 0) return this.appendChild(node);
  if(node.parentElement) node.parentElement.removeChild(node);
  node.parentElement = this;
  this.children.splice(at, 0, node);
  return node;
};
El.prototype.remove = function(){ if(this.parentElement) this.parentElement.removeChild(this); };
El.prototype.setAttribute = function(k, v){ this.attrs[k] = v; if(k === 'id') this.id = v; };
El.prototype.getAttribute = function(k){ return k in this.attrs ? this.attrs[k] : ''; };
El.prototype.hasAttribute = function(k){ return k in this.attrs; };
El.prototype.removeAttribute = function(k){ delete this.attrs[k]; };
El.prototype.addEventListener = function(type, fn){ (this.listeners[type] = this.listeners[type] || []).push(fn); };
El.prototype.removeEventListener = function(){};
// 启动路径（面板门控、主题、i18n）会调到这些，给个空实现即可
['focus', 'blur', 'click', 'select', 'scrollIntoView', 'insertAdjacentHTML', 'replaceChildren',
 'append', 'prepend', 'after', 'before', 'dispatchEvent', 'setSelectionRange'].forEach(name => {
  El.prototype[name] = function(){};
});
El.prototype.contains = function(node){
  for(let el = node; el; el = el.parentElement) if(el === this) return true;
  return false;
};
El.prototype.matches = function(sel){ return matches(this, sel); };
El.prototype.getBoundingClientRect = function(){ return { top: this.rectTop, left: 0, width: 0, height: 0 }; };
El.prototype.closest = function(sel){
  for(let el = this; el; el = el.parentElement) if(matches(el, sel)) return el;
  return null;
};
El.prototype.querySelectorAll = function(sel){
  const out = [];
  (function walk(node){
    for(const child of node.children){
      if(typeof child === 'string') continue;
      if(matches(child, sel)) out.push(child);
      walk(child);
    }
  })(this);
  return out;
};
El.prototype.querySelector = function(sel){ return this.querySelectorAll(sel)[0] || null; };
El.prototype.cloneNode = function(deep){
  const copy = new El(this.tagName, true);
  copy._classes = this._classes.slice();
  copy.id = this.id;
  copy.dataset = Object.assign({}, this.dataset);
  copy.style = Object.assign({}, this.style);
  if(deep) for(const child of this.children) copy.appendChild(typeof child === 'string' ? child : child.cloneNode(true));
  return copy;
};

/* 只支持本模块用到的选择器形状：tag、.class、.class.class、[id] */
function matches(el, sel){
  const hasId = /\[id\]/.test(sel);
  const parts = sel.replace(/\[id\]/g, '').split('.');
  const tag = parts[0] ? parts[0].toUpperCase() : null;
  if(tag && el.tagName !== tag) return false;
  for(const cls of parts.slice(1)) if(cls && !el.classList.contains(cls)) return false;
  return !(hasId && !el.id);
}

const root = new El('html');
// 吸顶 header：导航跳转要按它的实测高度让位
const header = new El('header');
header.offsetHeight = 40;
root.appendChild(header);
const byId = new Map();
const detached = id => {
  if(!byId.has(id)) byId.set(id, Object.assign(new El('div', true), { id: id }));
  return byId.get(id);
};
const timers = [];

global.document = {
  getElementById: id => ALL.find(el => el.id === id) || detached(id),
  querySelector: sel => ALL.find(el => matches(el, sel)) || null,
  querySelectorAll: sel => ALL.filter(el => matches(el, sel)),
  createElement: tag => new El(tag),
  addEventListener(){}, body: root, documentElement: root, head: root,
  readyState: 'complete',
};
global.getComputedStyle = el => ({ display: (el && el.style && el.style.display) || 'block' });
global.window = {
  addEventListener(){}, matchMedia: () => ({ matches: false, addEventListener(){} }),
  pageYOffset: 0, innerHeight: 800,
  scrollTo(opts){ global.__scroll = opts; },
  location: { href: 'http://panel/', search: '', hash: '' },
};
global.location = global.window.location;
global.history = { replaceState(_s, _t, url){ global.__url = url; } };
global.MutationObserver = function(){ this.observe = () => {}; this.disconnect = () => {}; };
global.setTimeout = fn => { timers.push(fn); return timers.length; };
global.clearTimeout = () => {};
global.setInterval = () => 0;
global.localStorage = { getItem(){ return null; }, setItem(){}, removeItem(){} };
global.sessionStorage = global.localStorage;
global.navigator = { userAgent: 'node' };
global.alert = () => {};
global.confirm = () => false;
global.fetch = () => Promise.resolve({ ok: false, status: 500, json: async () => ({}), text: async () => '' });

const api = new Function(script + `
  return { initPageNav, pageNavSections, pageNavLabel, pageNavAnchorId, pageNavSignature,
           pageNavSpy, restorePageNavHash, scrollToPageSection, switchMainTab };`)();

/* ---- 造页面的小工具 ---- */
function sectionEl(label, opts){
  opts = opts || {};
  const sec = new El('section');
  if(opts.id) sec.id = opts.id;
  if(opts.hidden) sec.style.display = 'none';
  if(label !== null){
    const h = new El('h2');
    h.appendChild(label);
    if(opts.em){ const em = new El('em'); em.id = opts.em; em.appendChild('7'); h.appendChild(em); }
    sec.appendChild(h);
  }
  return sec;
}
function pageEl(id, sections){
  const page = new El('div');
  page.className = 'main-page';
  page.id = id;
  sections.forEach(sec => page.appendChild(sec));
  return page;
}
function navItems(page){
  return page.querySelectorAll('.page-nav-item');
}
function labels(page){
  return navItems(page).map(btn => btn.textContent);
}
function attach(page){ root.appendChild(page); return page; }

/* ---- 1. 真实文件里的契约：不再有「设置页专用」的痕迹 ---- */
const legacy = ['settings-layout', 'settingsSidebar', 'settingsBody', 'settings-nav',
                'settings-sidebar', 'initSettingsSidebar', 'restoreSettingsHash', 'settings-flash'];
for(const name of legacy){
  assert.ok(!html.includes(name), 'dashboard.html 不该再残留设置页专用标识：' + name);
}
assert.ok(html.includes('initPageNav'), 'dashboard.html 应当有 initPageNav');
// 侧栏一律由 JS 现场插入，任何页面都不许在标记里写死
assert.ok(!/class="page-sidebar"/.test(html), '侧栏不应写死在标记里');
assert.ok(/\.main-page\.page-nav-on\s*>\s*\.page-sidebar/.test(html), '缺少页面级两栏样式');
assert.ok(/@media \(max-width:860px\)[\s\S]{0,400}?\.main-page\.page-nav-on > \*\{grid-column:1\}/.test(html),
  '窄屏应把页面收回单栏（否则 grid-column:2 会多出一列）');

// 各页面区块数：导航就是照这些区块生成的，数量只增不减
const regions = {};
const pageMarkers = [...html.matchAll(/<div id="page(\w+)" class="main-page">/g)];
pageMarkers.forEach((m, i) => {
  const start = m.index;
  const end = i + 1 < pageMarkers.length ? pageMarkers[i + 1].index : html.length;
  regions[m[1].toLowerCase()] = (html.slice(start, end).match(/<section/g) || []).length;
});
assert.ok(regions.gateway >= 4, 'gateway 应有多个区块，实际 ' + regions.gateway);
assert.ok(regions.analytics >= 3, 'analytics 应有多个区块，实际 ' + regions.analytics);
assert.ok(regions.settings >= 9, 'settings 应有多个区块，实际 ' + regions.settings);
assert.strictEqual(regions.logs, 0, 'logs 目前是单一视图，没有 section');

/* ---- 2. 哪些区块参与导航 ---- */
const secAccount = sectionEl('账号', { em: 'acctCount' });
const secRecent = sectionEl('最近请求');
const secGrowth = sectionEl('成长任务', { hidden: true });
const secUntitled = sectionEl(null);                 // 没有 h2，不成项
const group = sectionEl('高级设置');
const nestedA = sectionEl('嵌套甲');                  // 嵌套 section 属于父区块的内容
const nestedB = sectionEl('嵌套乙');
group.appendChild(nestedA);
group.appendChild(nestedB);
const gateway = pageEl('pageGateway', [secAccount, secRecent, secGrowth, secUntitled, group]);

assert.deepStrictEqual(
  api.pageNavSections(gateway).map(sec => api.pageNavLabel(sec, 0)),
  ['账号', '最近请求', '高级设置'],
  '只收最外层、有 h2、且未隐藏的区块；标题要剔除 <em>');

// 隐藏的嵌套区块本来也不成项
nestedA.style.display = 'none';
assert.deepStrictEqual(
  api.pageNavSections(gateway).map(sec => api.pageNavLabel(sec, 0)),
  ['账号', '最近请求', '高级设置'],
  '隐藏的嵌套区块本来也不成项');
nestedA.style.display = '';
// 整块显隐确实会改变清单
secGrowth.style.display = 'block';
assert.deepStrictEqual(
  api.pageNavSections(gateway).map(sec => api.pageNavLabel(sec, 0)),
  ['账号', '最近请求', '成长任务', '高级设置'],
  '区块显出来后要进导航');
secGrowth.style.display = 'none';

/* ---- 3. 锚点 id：与顺序无关、可复用、冲突加后缀 ---- */
assert.strictEqual(api.pageNavAnchorId('账号', new Set()), api.pageNavAnchorId('账号', new Set()),
  '同一标题必须得到同一个 id');
assert.notStrictEqual(api.pageNavAnchorId('账号', new Set()), api.pageNavAnchorId('最近请求', new Set()),
  '不同标题必须得到不同 id');
const taken = new Set([api.pageNavAnchorId('账号', new Set())]);
assert.strictEqual(api.pageNavAnchorId('账号', taken),
  api.pageNavAnchorId('账号', new Set()) + '-2', 'id 撞车要顺延');

/* ---- 4. initPageNav：建栏、分配锚点、按标题生成导航项 ---- */
attach(gateway);
gateway.classList.add('active');
// 给区块一点真实位置，否则都在视口顶端，滚动高亮会一路点到最后一项
api.pageNavSections(gateway).forEach((sec, i) => { sec.rectTop = 120 + i * 300; });
api.initPageNav(gateway);
assert.ok(gateway.classList.contains('page-nav-on'), '区块够多应启用页面级两栏');
assert.strictEqual(gateway.firstChild, gateway.querySelector('.page-sidebar'), '侧栏要插在页面最前面');
assert.deepStrictEqual(labels(gateway), ['账号', '最近请求', '高级设置'], '导航项就是区块标题');
for(const sec of api.pageNavSections(gateway)){
  assert.ok(sec.id, '每个参与导航的区块都要有锚点 id');
}
const idOfAccount = api.pageNavSections(gateway)[0].id;
assert.ok(idOfAccount.indexOf('page-sec-') === 0, '锚点 id 由标题哈希得到：' + idOfAccount);
assert.strictEqual(navItems(gateway)[0].dataset.target, idOfAccount, '导航项要指向区块锚点');
assert.ok(navItems(gateway)[0].classList.contains('active'), '默认高亮第一项');

// 重复调用不重建 DOM（否则轮询刷新时高亮会抖）
const sidebarBefore = gateway.querySelector('.page-sidebar');
api.initPageNav(gateway);
assert.strictEqual(gateway.querySelector('.page-sidebar'), sidebarBefore, '清单没变时不应重建侧栏');

/* ---- 5. 区块清单变了就重建 ---- */
secGrowth.style.display = 'block';                    // 成长任务显出来
api.initPageNav(gateway);
assert.deepStrictEqual(labels(gateway), ['账号', '最近请求', '成长任务', '高级设置'],
  '区块显隐变化后导航要跟上');
assert.strictEqual(gateway.querySelector('.page-sidebar'), sidebarBefore,
  '重建复用同一个侧栏容器，只换里面的项');

/* ---- 6. 区块不足两项就不给侧栏 ---- */
const logs = attach(pageEl('pageLogs', [sectionEl('网关运行日志')]));
logs.classList.add('active');
api.initPageNav(logs);
assert.ok(!logs.classList.contains('page-nav-on'), '只有一项的页面不该启用两栏');
assert.strictEqual(logs.querySelector('.page-sidebar'), null, '只有一项的页面不该有侧栏');

// 从够用掉到不够用：侧栏要撤掉
const shrink = attach(pageEl('pageShrink', [sectionEl('甲'), sectionEl('乙')]));
api.initPageNav(shrink);
assert.ok(shrink.querySelector('.page-sidebar'), '两项应启用侧栏');
api.pageNavSections(shrink)[1].remove();
api.initPageNav(shrink);
assert.strictEqual(shrink.querySelector('.page-sidebar'), null, '掉到一项要撤掉侧栏');
assert.ok(!shrink.classList.contains('page-nav-on'), '掉到一项要取消两栏');

/* ---- 7. 滚动高亮：以「越过吸顶 header 的最后一个区块」为准 ---- */
const spyPage = attach(pageEl('pageSpy', [sectionEl('甲'), sectionEl('乙')]));
spyPage.classList.add('active');
gateway.classList.remove('active');
api.initPageNav(spyPage);
const spySections = api.pageNavSections(spyPage);
const spyItems = navItems(spyPage);
spySections[0].rectTop = -400; spySections[1].rectTop = -100;
api.pageNavSpy();
assert.ok(spyItems[1].classList.contains('active'), '两个区块都滚过去了应高亮后一个');
spySections[0].rectTop = -100; spySections[1].rectTop = 300;
api.pageNavSpy();
assert.ok(spyItems[0].classList.contains('active'), '后一个还在下方时应高亮前一个');

/* ---- 8. #锚点 只落在本页可导航的区块上 ---- */
window.location.hash = '#' + spySections[0].id;
global.__scroll = null;
api.restorePageNavHash();
assert.ok(global.__scroll, '本页可导航区块的锚点应触发滚动');
assert.strictEqual(global.__scroll.behavior, 'auto', '带锚点打开是直接落位，不是平滑滚动');

window.location.hash = '#' + secAccount.id;            // 别的页面上的区块
global.__scroll = null;
api.restorePageNavHash();
assert.strictEqual(global.__scroll, null, '别的页面的锚点不该让当前页滚动');

spySections[1].style.display = 'none';                // 本页但已被隐藏
window.location.hash = '#' + spySections[1].id;
global.__scroll = null;
api.restorePageNavHash();
assert.strictEqual(global.__scroll, null, '被隐藏的区块不能当锚点落点');
spySections[1].style.display = '';

// 点导航项：写 hash、加描边、按实测 header 高度让位
window.location.hash = '';
global.__scroll = null;
navItems(spyPage)[1].listeners.click[0]();
assert.strictEqual(global.__url, 'http://panel/#' + spySections[1].id, '点击导航项要把锚点写进 URL');
assert.ok(global.__scroll && global.__scroll.top === spySections[1].rectTop - (header.offsetHeight + 14),
  '要让开吸顶 header 的高度，实际 ' + (global.__scroll && global.__scroll.top));

/* ---- 9. 切标签页时按该页的区块重建 ---- */
const btnGateway = new El('button'); btnGateway.id = 'btnNavGateway';
root.appendChild(btnGateway);
attach(pageEl('pageSettings', [sectionEl('甲'), sectionEl('乙'), sectionEl('丙')]));
api.switchMainTab('gateway');
assert.ok(gateway.classList.contains('active'), '切到 gateway 应激活该页');
assert.deepStrictEqual(labels(gateway), ['账号', '最近请求', '成长任务', '高级设置'],
  '切回 gateway 用的是它自己的区块清单');

// 每个页面各自一份侧栏，互不串台
const pageSettings = document.getElementById('pageSettings');
api.initPageNav(pageSettings);
assert.deepStrictEqual(pageSettings.querySelectorAll('.page-nav-item').map(b => b.textContent),
  ['甲', '乙', '丙'], 'settings 的导航来自 settings 的区块');
assert.deepStrictEqual(labels(gateway), ['账号', '最近请求', '成长任务', '高级设置'],
  '给别的页面建栏不该动到 gateway 的侧栏');
assert.notStrictEqual(pageSettings.querySelector('.page-sidebar'), gateway.querySelector('.page-sidebar'),
  '每个页面有各自的侧栏容器');

console.log('page nav assertions passed');
