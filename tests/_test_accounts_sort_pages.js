/* 账号表：表头排序跨页。
 *
 * 账号表分页（每页 50）之后，点表头排序要排的是**全部账号**，再切出当前页——
 * 只在页内重排，第 2 页的最大值可能比第 1 页的最小值还大。renderAccounts 的做法
 * 是把全部行画进一个游离节点、用 applyTableSort 的口径排好，再把这一页的行搬进
 * 表格。页内随后的 applyTableSort('accounts')（以及每 5 秒的 applyAllTableSorts）
 * 只挂表头装饰、不重排，列类型沿用全量推断的那份：只看这 50 行推断时，某一列
 * 这一页全是「—」就会被判成不可排，箭头被摘掉。
 *
 * 公共假 DOM（_dom_stub.js）不解析 innerHTML，这条路径必须真的有表格结构，所以
 * 这里自带一个极小的 HTML 解析器（只认标签、属性、文本），只用在账号表容器与
 * renderAccounts 建的那个游离 div 上。
 *
 * Run with Node: node tests/_test_accounts_sort_pages.js
 */
'use strict';
const assert = require('assert');
const {dashboardScript} = require('./_dashboard_source.js');
const dom = require('./_dom_stub.js');

/* ---- 极小 HTML 树 ------------------------------------------------------- */

const VOID = new Set(['input', 'br', 'img', 'hr', 'meta', 'link', 'wbr', 'col', 'source']);
const decode = t => t.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"')
  .replace(/&#39;/g, "'").replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&');

class Text {
  constructor(t){ this.text = t; this.parent = null; }
  get textContent(){ return this.text; }
}

class El {
  constructor(tag){
    this.tagName = tag.toUpperCase();
    this.attrs = {};
    this.nodes = [];
    this.parent = null;
    const self = this;
    this.classList = {
      contains: c => self.className.split(/\s+/).includes(c),
      add: c => { if(!self.classList.contains(c)) self.className = (self.className + ' ' + c).trim(); },
      remove: c => { self.className = self.className.split(/\s+/).filter(x => x && x !== c).join(' '); },
    };
  }
  get children(){ return this.nodes.filter(n => n instanceof El); }
  get className(){ return this.attrs['class'] || ''; }
  set className(v){ this.attrs['class'] = String(v); }
  get title(){ return this.attrs.title || ''; }
  set title(v){ this.attrs.title = String(v); }
  get textContent(){ return this.nodes.map(n => n.textContent).join(''); }
  set textContent(v){ this.nodes = []; this.append(new Text(String(v))); }
  get innerHTML(){ return '[parsed]'; }
  set innerHTML(html){ this.nodes.forEach(n => { n.parent = null; }); this.nodes = []; parseInto(this, String(html)); }
  getAttribute(k){ return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
  setAttribute(k, v){ this.attrs[k] = String(v); }
  removeAttribute(k){ delete this.attrs[k]; }
  append(n){ if(n.parent) n.parent.nodes.splice(n.parent.nodes.indexOf(n), 1); n.parent = this; this.nodes.push(n); return n; }
  appendChild(n){ return this.append(n); }
  remove(){ if(this.parent) this.parent.nodes.splice(this.parent.nodes.indexOf(this), 1); this.parent = null; }
  insertAdjacentHTML(pos, html){ assert.strictEqual(pos, 'beforeend'); parseInto(this, html); }
  matches(sel){
    return sel.split(',').map(s => s.trim()).some(s =>
      s.startsWith('.') ? this.classList.contains(s.slice(1)) : this.tagName === s.toUpperCase());
  }
  querySelectorAll(sel){
    const out = [];
    const walk = el => el.children.forEach(c => { if(c.matches(sel)) out.push(c); walk(c); });
    walk(this);
    return out;
  }
  querySelector(sel){ return this.querySelectorAll(sel)[0] || null; }
}

function parseInto(root, html){
  const stack = [root];
  const top = () => stack[stack.length - 1];
  const re = /<!--[\s\S]*?-->|<\/([a-zA-Z][\w-]*)\s*>|<([a-zA-Z][\w-]*)((?:\s+[^\s=\/>]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*\/?>|([^<]+)/g;
  let m;
  while((m = re.exec(html))){
    if(m[1]){
      const tag = m[1].toUpperCase();
      const at = stack.map(e => e.tagName).lastIndexOf(tag);
      if(at > 0) stack.length = at;
    }else if(m[2]){
      const el = new El(m[2]);
      const attrRe = /([^\s=\/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
      let a;
      while((a = attrRe.exec(m[3] || ''))) el.attrs[a[1].toLowerCase()] = decode(a[2] ?? a[3] ?? a[4] ?? '');
      top().append(el);
      if(!VOID.has(m[2].toLowerCase())) stack.push(el);
    }else if(m[4]){
      top().append(new Text(decode(m[4])));
    }
  }
}

/* ---- 装页面 ------------------------------------------------------------- */

dom.installDom();
global.fetch = () => Promise.resolve({status: 200, ok: true,
  json: () => Promise.resolve({}), text: () => Promise.resolve('{}')});

const realLog = console.log;
console.log = () => {};
let api;
try {
  api = new Function(dashboardScript() + `
    return {
      renderAccounts: renderAccounts,
      toggleTableSort: toggleTableSort,
      applyTableSort: applyTableSort,
      gotoAcctPage: gotoAcctPage,
      setUsage: u => { USAGE_BY_ACCOUNT = u; },
      sortState: () => SORT_STATE,
      fixedKinds: () => ACCT_FIXED_KINDS,
    };`)();
} catch(e) {
  console.log = realLog;
  console.error('脚本求值失败:', e.message);
  process.exit(1);
}
console.log = realLog;

// 账号表容器与 renderAccounts 的游离 div 换成能解析 HTML 的树；其余照旧走公共假 DOM。
const box = new El('div');
box.attrs.id = 'accounts';
const stubGet = document.getElementById.bind(document);
const stubCreate = document.createElement.bind(document);
document.getElementById = id => id === 'accounts' ? box : stubGet(id);
document.createElement = tag => tag === 'div' ? new El('div') : stubCreate(tag);

const REQ_COL = 7;   // 「请求」列：fmt(usage.requests)，纯数字
const N = 120;
// 请求数与服务端顺序打乱（i*37 mod 120 是 0..119 的一个排列），默认排序（同权重
// 保持服务端顺序）与按请求数排序完全不同。
const reqOf = i => (i * 37) % N;
const accounts = [];
const usage = {};
for(let i = 0; i < N; i++){
  const uid = 'uid-s-' + String(i).padStart(3, '0');
  accounts.push({uid: uid, nickname: 'n' + i, realm: 'intl', enabled: true,
                 source: 'oauth', product: 'workbuddy', weight: 1,
                 credits: {remain: 1, updated_at: 1}});
  usage[uid] = {account: uid, requests: reqOf(i), total_tokens: 0, cached_tokens: 0};
}
window.VIEW_REALM = 'intl';
window.ACCOUNTS = accounts;
api.setUsage(usage);

const pageRows = () => Array.from(box.querySelector('tbody').children);
const pageReqs = () => pageRows().map(r => Number(r.children[REQ_COL].textContent.replace(/,/g, '')));
const reqTh = () => box.querySelectorAll('th')[REQ_COL];
const desc = Array.from({length: N}, (_, i) => N - 1 - i);   // 119..0

/* ---- 1. 点表头：全量降序后切页 ------------------------------------------- */
api.renderAccounts();
assert.ok(!api.sortState().accounts, '起始没有表头排序');
assert.strictEqual(api.fixedKinds(), null, '没有表头排序时不固定列类型');
api.toggleTableSort('accounts', REQ_COL);
assert.deepStrictEqual(api.sortState().accounts, {col: REQ_COL, dir: 'desc'});
assert.deepStrictEqual(pageReqs(), desc.slice(0, 50), '第 1 页必须是全量降序的前 50 个');
assert.strictEqual(reqTh().getAttribute('aria-sort'), 'descending');
assert.ok(Array.isArray(api.fixedKinds()) && api.fixedKinds()[REQ_COL] === 'num',
  '分页 + 表头排序时要记下全量推断的列类型');

api.gotoAcctPage(2);
assert.deepStrictEqual(pageReqs(), desc.slice(50, 100), '第 2 页必须是全量降序的 51~100');
api.gotoAcctPage(3);
assert.deepStrictEqual(pageReqs(), desc.slice(100), '第 3 页是剩下的 20 个');

/* ---- 2. 再点一次：升序，回到第 1 页 --------------------------------------- */
api.toggleTableSort('accounts', REQ_COL);
assert.deepStrictEqual(api.sortState().accounts, {col: REQ_COL, dir: 'asc'});
assert.deepStrictEqual(pageReqs(), desc.slice().reverse().slice(0, 50), '升序第 1 页是最小的 50 个');
assert.strictEqual(reqTh().getAttribute('aria-sort'), 'ascending');
api.toggleTableSort('accounts', REQ_COL);   // 回到降序，下面接着用

/* ---- 3. 页内 applyTableSort（含每 5 秒那轮）不重排、不按页内数据改列类型 --- */
api.gotoAcctPage(2);
const before = pageRows();
// 模拟「这一页这一列全是 —」：只看页内推断会判成不可排、摘掉箭头。
before.forEach(r => { r.children[REQ_COL].textContent = '—'; });
// 再把行顺序打乱：页内那一遍如果还在排序，就会把它们挪回去（或挪成别的顺序）。
const shuffled = before.slice().reverse();
const tbody = box.querySelector('tbody');
shuffled.forEach(r => tbody.appendChild(r));
api.applyTableSort('accounts');
assert.deepStrictEqual(pageRows(), shuffled, '分页 + 表头排序时页内那一遍只挂表头，不重排');
assert.ok(reqTh().classList.contains('sortable'), '列类型沿用全量推断：箭头不能因为这一页全是「—」被摘');
assert.strictEqual(reqTh().getAttribute('aria-sort'), 'descending');
assert.ok(reqTh().querySelector('.sort-arrow'), '排序箭头仍在');

/* ---- 4. 只有一页时回到原来的页内排序 -------------------------------------- */
window.ACCOUNTS = accounts.slice(0, 30);
api.renderAccounts();
assert.strictEqual(api.fixedKinds(), null, '只有一页时不固定列类型');
const one = pageReqs();
assert.strictEqual(one.length, 30);
assert.deepStrictEqual(one, one.slice().sort((a, b) => b - a), '单页时照常按表头降序');

process.stdout.write('OK\n');
process.exit(0);
