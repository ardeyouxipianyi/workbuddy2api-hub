/* 积分扣减历史面板的两处缺陷：

   ① 表头不固定。外层容器自带 max-height + 纵向滚动，表头跟着一起滚走，
      滑到下面就看不出哪一列是哪一列。修法是给 thead 里的 th 加 sticky。
   ② 账号列只显示 uid 前 8 位。昵称来自 /accounts（与 /usage/timeseries 并行的
      另一条请求），所以晚到时要先用 uid 兜底、等账号到位再补画一次；
      账号已删除时同样回退 uid 前 8 位，不留空。Run with Node.
*/
const assert = require('assert');
const {dashboardHtml, dashboardScript} = require('./_dashboard_source.js');

const html = dashboardHtml();
const script = dashboardScript();

// One persistent element per id, so the rendered table can be read back.
const elements = new Map();
const element = id => {
  if (!elements.has(id)) {
    elements.set(id, {
      id, innerHTML: '', textContent: '', value: '', className: '', style: {},
      classList: {add(){}, remove(){}, toggle(){}, contains(){ return false; }},
      addEventListener(){}, querySelector(){ return null; }, querySelectorAll(){ return []; },
      appendChild(){}, focus(){}, setAttribute(){}, getAttribute(){ return ''; },
    });
  }
  return elements.get(id);
};
const TBODY = element('creditHistoryBody');
global.document = {
  getElementById: element,
  querySelector: sel => (sel === '#creditHistoryTable tbody' ? TBODY : null),
  querySelectorAll: () => [],
  addEventListener(){}, createElement: () => element('created'),
  body: element('body'), head: element('head'), documentElement: element('html'),
};
global.window = {addEventListener(){}, location: {href: '', search: ''},
  matchMedia: () => ({matches: false, addEventListener(){}}), ACCOUNTS: []};
global.ACCOUNTS = global.window.ACCOUNTS;
global.localStorage = {getItem(){ return null; }, setItem(){}, removeItem(){}};
global.sessionStorage = global.localStorage;
global.navigator = {userAgent: 'node'};
global.setInterval = () => 0;
global.setTimeout = () => 0;
global.location = {href: '', search: '', hash: ''};
global.alert = () => {};
global.confirm = () => false;
global.fetch = () => Promise.resolve({
  status: 200, ok: true,
  json: () => Promise.resolve({}),
  text: () => Promise.resolve('{}'),
});

const DATA = {
  credits: [
    {iso: '2026-10-08T15:30:42', model: 'deepseek-v4.1-flash',
     account: '1c0dd7a8b9c0', credit: 0.59, total_tokens: 8831},
    {iso: '2026-10-08T08:57:39', model: 'gpt-5.6-sol',
     account: '5e93a7361122', credit: 0.03, total_tokens: 57},
    {iso: '2026-10-08T08:00:00', model: 'gpt-5.6-sol',
     account: '', credit: 0.01, total_tokens: 12},
  ],
};

const api = new Function(script + `
  return { renderCreditHistory };`)();

let checks = 0;
const check = (label, cond, extra) => {
  checks += 1;
  assert.ok(cond, label + (extra ? '  [' + extra + ']' : ''));
};

// ---- ① 表头固定 ----------------------------------------------------------
const thead = html.match(/<table class="data-cards" id="creditHistoryTable">[\s\S]*?<\/thead>/);
check('积分扣减历史表头仍是 5 列',
      thead && (thead[0].match(/<th>/g) || []).length === 5,
      thead ? String((thead[0].match(/<th>/g) || []).length) : 'thead not found');
check('表头列名没变',
      thead && ['时间', '模型', '账号', '积分', 'Token']
        .every(name => thead[0].includes('<th>' + name + '</th>')));
// 页面里的 CSS 规则，按出现顺序。只解析到本套件需要的程度：不模拟层叠、
// 继承与 @media 的生效条件，选择器取 `{` 之前的最后一段，声明按 `;` 拆开。
const cssRules = [...html.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
  .map(m => ({
    selector: m[1].split(/[\n;]/).pop().replace(/\s+/g, ' ').trim(),
    body: m[2],
  }))
  .filter(rule => rule.selector && !rule.selector.startsWith('@'));
const decls = text => {
  const out = {};
  text.split(';').forEach(part => {
    const at = part.indexOf(':');
    if (at > 0) out[part.slice(0, at).trim().toLowerCase()] = part.slice(at + 1).trim();
  });
  return out;
};

// 本表自己的身份（id 与 class）从标记里读，而不是写死。
const tableTag = (html.match(/<table([^>]*id="creditHistoryTable"[^>]*)>/) || [])[1] || '';
const tableId = (tableTag.match(/id="([^"]*)"/) || [])[1] || '';
const tableClasses = ((tableTag.match(/class="([^"]*)"/) || [])[1] || '')
  .split(/\s+/).filter(Boolean);
// 「命中本表表头」= 选择器（逗号分隔，任一部分成立即可）确实能把声明作用到
// 本表的表头：末尾落在 th / thead 上，路径上没有 tbody / tfoot 这类把范围排除
// 在表头之外的分组，且限定词只认本表自己的 id / class。于是 `thead th`、
// `#creditHistoryTable thead th`、`thead > tr > th` 都算，而
// `#creditHistoryTable tbody th` 与别的表的规则不算。
const targetsHeader = selector =>
  selector.split(',').some(part => {
    const compounds = part.trim().split(/\s*[>+~]\s*|\s+/).filter(Boolean);
    if (!compounds.length) return false;
    if (compounds.some(token => /^(tbody|tfoot)\b/.test(token))) return false;
    if (!/^th(e?ad)?\b/.test(compounds[compounds.length - 1])) return false;
    return (part.match(/[#.][\w-]+/g) || [])
      .every(token => token === '#' + tableId || tableClasses.includes(token.slice(1)));
  });
// 反例守卫：把 thead 换成 tbody 之后，规则再也作用不到表头，分类器必须拒绝它 ——
// 否则「表头 sticky」会在浏览器行为已经坏掉时继续全绿。
check('作用不到表头的选择器不算表头规则',
      !targetsHeader('#creditHistoryTable tbody th')
      && !targetsHeader('#creditHistoryTable tbody > tr > th')
      && targetsHeader('#creditHistoryTable thead > tr > th'),
      'tbody th -> ' + targetsHeader('#creditHistoryTable tbody th'));

const headerRules = cssRules
  .filter(rule => targetsHeader(rule.selector))
  .map(rule => ({selector: rule.selector, decl: decls(rule.body)}));
const stickyRule = headerRules
  .filter(rule => /^(?:-webkit-)?sticky$/.test(rule.decl.position || ''))
  .pop();
const zeroOffset = value => /^0(?:\.0+)?[a-z%]*$/.test(value || '');
check('表头 th 声明了 sticky 定位',
      !!stickyRule,
      headerRules.map(rule => rule.selector + '{' + (rule.decl.position || '-') + '}')
        .join(' | ') || 'no header rule found');
check('sticky 表头钉在容器顶部',
      !!stickyRule && zeroOffset(stickyRule.decl.top),
      stickyRule ? stickyRule.selector + '{top:' + stickyRule.decl.top + '}' : 'no sticky rule');

// 容器：包住本表的那个元素**自己**的声明（行内样式，或作用在它自己身上的
// class 规则），而不是"页面上某处出现过这段字符串"。有高度上限、且至少一个轴
// 在滚动，表头才有可粘的上下文 —— overflow 的计算规则保证另一轴会按 auto 处理。
const wrapperMatch = html.match(/<([a-zA-Z][\w-]*)([^>]*)>\s*<table[^>]*id="creditHistoryTable"/) || [];
const wrapper = wrapperMatch[2] || '';
// 容器元素自己的身份，全部从标记里读：标签、class token、id、属性名 → 值。
// 属性名先把带引号的值中和掉再找，免得把值里出现的 `x=` 当成属性。
const wrapperAttrs = {};
[...wrapper.replace(/"[^"]*"/g, '""').matchAll(/([\w-]+)\s*=/g)].forEach(m => {
  const value = wrapper.match(new RegExp('(?:^|\\s)' + m[1] + '\\s*=\\s*"([^"]*)"', 'i'));
  wrapperAttrs[m[1].toLowerCase()] = value ? value[1] : '';
});
const wrapperElement = {
  tag: (wrapperMatch[1] || '').toLowerCase(),
  classes: ((wrapper.match(/class="([^"]*)"/) || [])[1] || '').split(/\s+/).filter(Boolean),
  ids: [...wrapper.matchAll(/(?:^|\s)id="([^"]*)"/g)].map(m => m[1]),
  attrs: wrapperAttrs,
};
// [属性] 限定词：属性必须真的在这个元素上；写了 `=` 值还要相等（其余运算符一律
// 判为不满足 —— 宁可判"选不中"，也不把没校验的限定词当成满足）。
const attrSatisfied = (qualifier, element) => {
  const m = qualifier.match(/^\s*([\w-]+)\s*(?:([~^$*|]?=)\s*(.*?)\s*)?$/);
  if (!m) return false;
  const name = m[1].toLowerCase();
  if (!(name in element.attrs)) return false;
  if (!m[2]) return true;
  if (m[2] !== '=') return false;
  return element.attrs[name] === (m[3] || '').replace(/^["']|["']$/g, '');
};
// 一条规则要算在容器**元素自身**头上：选择器（逗号分隔，任一部分成立即可）
// 某个部分的**末尾组合**（组合器右侧才是被选中的元素）必须带上容器自己的
// class，且该组合里出现的**每一个**限定词 —— 元素名、其它 class、id、[属性]
// —— 都要由这个元素真的满足：class 按 token **整名**相等（`creditHistoryWrapX`
// 不算命中），多出来的限定词缺一个就拒绝。`.creditHistoryWrap table` /
// `.creditHistoryWrap td` 这类后代选择器打在别的元素上，同样不算。
const selectorTargetsElement = (selector, element) =>
  selector.split(',').some(part => {
    const compounds = part.trim().split(/\s*[>+~]\s*|\s+/).filter(Boolean);
    const last = compounds[compounds.length - 1] || '';
    const classes = [...last.matchAll(/\.([\w-]+)/g)].map(m => m[1]);
    const elementName = (last.match(/^([a-zA-Z][\w-]*)/) || [])[1];
    return classes.length > 0
      && classes.every(cls => element.classes.includes(cls))
      && [...last.matchAll(/#([\w-]+)/g)].every(m => element.ids.includes(m[1]))
      && [...last.matchAll(/\[([^\]]+)\]/g)].every(m => attrSatisfied(m[1], element))
      && (!elementName || elementName.toLowerCase() === element.tag);
  });
const WRAP = {tag: 'div', classes: ['creditHistoryWrap'], ids: [], attrs: {class: 'creditHistoryWrap'}};
// 反例守卫：后代选择器不能向容器自己的声明里贡献 max-height / overflow。
check('后代选择器不算作用在容器元素自身',
      !selectorTargetsElement('.creditHistoryWrap table', WRAP)
      && !selectorTargetsElement('.creditHistoryWrap td', WRAP)
      && !selectorTargetsElement('table.creditHistoryWrap', WRAP)
      && selectorTargetsElement('.creditHistoryWrap', WRAP)
      && selectorTargetsElement('div.creditHistoryWrap', WRAP),
      '.creditHistoryWrap table -> ' + selectorTargetsElement('.creditHistoryWrap table', WRAP));
// 反例守卫：class 按 token 整名相等，前缀/超集写法不算命中。
check('class 判定按 token 整名相等，不是子串',
      !selectorTargetsElement('.creditHistoryWrapX', WRAP)
      && !selectorTargetsElement('xcreditHistoryWrap', WRAP)
      && !selectorTargetsElement('.foo.creditHistoryWrapbar', WRAP),
      '.creditHistoryWrapX -> ' + selectorTargetsElement('.creditHistoryWrapX', WRAP));
// 反例守卫：同一组合里多出来的限定词必须真的能被容器满足。
check('组合里的额外限定词必须由容器真的满足',
      !selectorTargetsElement('div.other.creditHistoryWrap', WRAP)
      && !selectorTargetsElement('#other.creditHistoryWrap', WRAP)
      && !selectorTargetsElement('[data-x].creditHistoryWrap', WRAP)
      && !selectorTargetsElement('[class="other"].creditHistoryWrap', WRAP)
      && selectorTargetsElement('[class].creditHistoryWrap', WRAP),
      'div.other.creditHistoryWrap -> '
      + selectorTargetsElement('div.other.creditHistoryWrap', WRAP));
const wrapperDecl = decls(
  ((wrapper.match(/\bstyle="([^"]*)"/) || [])[1] || '') + ';'
  + cssRules.filter(rule => selectorTargetsElement(rule.selector, wrapperElement))
    .map(rule => rule.body).join(';'));
const scrolls = ['overflow', 'overflow-x', 'overflow-y']
  .some(key => /^(auto|scroll|overlay)$/.test(wrapperDecl[key] || ''));
check('外层容器确实会纵向滚动（否则 sticky 无意义）',
      !!(wrapperDecl['max-height'] || wrapperDecl['height']) && scrolls,
      JSON.stringify(wrapperDecl));

// ---- ② 账号列带昵称 ------------------------------------------------------
// 断言按「行」读渲染结果：一行就是一次扣减记录，账号单元格取表头里「账号」
// 那一列。整表子串搜索既会被一个无害的属性变化绊倒（给 <td> 加个 class，
// `<td title="…">` 就不再匹配），也看不出昵称到底挂在哪一行、哪一列。
const cellOf = row => [...row.matchAll(/<td\b([^>]*)>([\s\S]*?)<\/td>/g)].map(m => ({
  attrs: m[1],
  text: m[2],
  title: (m[1].match(/(?:^|\s)title="([^"]*)"/) || [])[1] || '',
}));
const rowFor = (html, iso) => [...html.matchAll(/<tr>([\s\S]*?)<\/tr>/g)]
  .map(m => cellOf(m[1]))
  .find(cells => cells[0] && cells[0].text === iso) || [];
const headerNames = thead
  ? [...thead[0].matchAll(/<th>([\s\S]*?)<\/th>/g)].map(m => m[1].trim()) : [];
const ACCOUNT_COL = headerNames.indexOf('账号');
const accountOf = (html, iso) => rowFor(html, iso)[ACCOUNT_COL] || null;
// 昵称是中文，账号单元格不该套等宽字体：看的是那个单元格自己（含内部标记），
// 而不是整张表里某一段精确的 class 字符串。
const notMono = cell => !!cell && !/\bclass="[^"]*\bmono\b/.test(cell.attrs + cell.text);

// 值属于哪一行、哪一列：行按时间（首列）认出来，列按表头里的列名认出来 —— 与
// 账号列同一套机器。读的是单元格的**可见文本**：单元格内部允许有无害标记
// （<b>、class、title、换行缩进），只要这一行这一列显示的数字没变。
const visibleText = cell => cell ? cell.text.replace(/<[^>]*>/g, '').trim() : null;
const cellAt = (html, iso, col) => visibleText(rowFor(html, iso)[col]);
const CREDIT_COL = headerNames.indexOf('积分');
const TOKEN_COL = headerNames.indexOf('Token');
const creditTokenOf = (html, iso) => [cellAt(html, iso, CREDIT_COL), cellAt(html, iso, TOKEN_COL)];

let body = '';
const checkAccount = (label, iso, text, title) => {
  const cell = accountOf(body, iso);
  check(label, !!cell && cell.text === text && cell.title === title,
        iso + ' -> ' + JSON.stringify(cell));
};

// 账号还没加载完：只能用 uid 前 8 位兜底。
window.ACCOUNTS = [];
api.renderCreditHistory(DATA);
body = TBODY.innerHTML;
checkAccount('账号未加载时回退 uid 前 8 位',
             '2026-10-08T15:30:42', '1c0dd7a8', '1c0dd7a8b9c0');
checkAccount('uid 前 8 位不是硬截断（第二位账号同样处理）',
             '2026-10-08T08:57:39', '5e93a736', '5e93a7361122');
checkAccount('账号为空时不渲染空白', '2026-10-08T08:00:00', '—', '');

// 账号到位后补画：昵称替换 uid；不在池中的账号（已删除）仍回退 uid。
window.ACCOUNTS = [{uid: '1c0dd7a8b9c0', nickname: '老王'}];
api.renderCreditHistory();          // 不带参数 = 用上一次的数据重画
body = TBODY.innerHTML;
checkAccount('账号到位后补画显示昵称', '2026-10-08T15:30:42', '老王', '1c0dd7a8b9c0');
checkAccount('账号不在池中（已删除）仍回退 uid 前 8 位',
             '2026-10-08T08:57:39', '5e93a736', '5e93a7361122');

// 重新传入数据也要更新缓存。
window.ACCOUNTS = [{uid: '1c0dd7a8b9c0', nickname: '张三'}];
api.renderCreditHistory({credits: [DATA.credits[0]]});
body = TBODY.innerHTML;
checkAccount('传入新数据后昵称跟随更新', '2026-10-08T15:30:42', '张三', '1c0dd7a8b9c0');
check('旧的昵称不会留在表里', !body.includes('老王'), body.slice(0, 200));
check('账号单元格不再强制等宽字体（昵称是中文）',
      notMono(accountOf(body, '2026-10-08T15:30:42')),
      JSON.stringify(accountOf(body, '2026-10-08T15:30:42')));
// ---- ③ 积分 / Token 绑定到记录行与列 --------------------------------------
// 整表子串搜索（`body.includes('>0.59<')`）只能证明这两个数字在页面某处出现过：
// 把两行的值互换、或者把它们挂到别的列上，它照样是绿的。下面按「时间行 + 表头
// 定出的列」读，值属于哪条记录、哪一列由渲染结果自己回答。
check('重传数据后第一行的积分与 Token 仍各归其位',
      creditTokenOf(body, '2026-10-08T15:30:42').join(' / ') === '0.59 / 8,831',
      JSON.stringify(creditTokenOf(body, '2026-10-08T15:30:42')));

api.renderCreditHistory(DATA);            // 三行 fixture 全量重画
body = TBODY.innerHTML;
[
  ['2026-10-08T15:30:42', '0.59', '8,831'],
  ['2026-10-08T08:57:39', '0.03', '57'],
  ['2026-10-08T08:00:00', '0.01', '12'],
].forEach(([iso, credit, tokens]) => {
  // 数值仍按原样渲染（两位小数、千位分隔符），只是现在必须落在它自己那一行。
  check('记录行 ' + iso + ' 的积分与 Token 各归其位',
        creditTokenOf(body, iso).join(' / ') === credit + ' / ' + tokens,
        iso + ' -> ' + JSON.stringify(creditTokenOf(body, iso)));
});
// 可见文本的容错：单元格里多一层标记或缩进不该改变读到的数字。旧写法要求
// `>0.59<` 精确相邻，遇到缩进/换行就会误报。
check('单元格里的无害标记不影响取值',
      cellAt('<tr><td>2026-10-08T15:30:42</td><td class="num">\n  0.59\n</td></tr>',
             '2026-10-08T15:30:42', 1) === '0.59'
      && cellAt('<tr><td>2026-10-08T15:30:42</td><td><b>0.59</b></td></tr>',
                '2026-10-08T15:30:42', 1) === '0.59',
      'decorated cell');

// 空数据走占位行。
api.renderCreditHistory({credits: []});
check('无记录时保留占位行',
      TBODY.innerHTML.includes('没有积分扣减记录') &&
      TBODY.innerHTML.includes('colspan="5"'));

console.log('credit-history assertions passed (' + checks + ' checks)');
