const fmt = n => (n ?? 0).toLocaleString('en-US');
/* Token 数的紧凑显示：<1 万精确显示（千分位），≥1 万 K，≥100 万 M，≥10 亿 B；
   小数位随量级收缩（123K / 12.3K / 1.23K），尾零去掉。单位用大写 K/M/B，
   与表头排序解析器（cellSortValue 的 'K'/'M'/'G' 换算）同一口径，带单位的
   单元格照样按数值排序。 */
const fmtCompact = n => {
  const v = Number(n) || 0;
  const abs = Math.abs(v);
  const pick = (div, suffix) => {
    const x = v / div;
    const d = Math.abs(x) >= 100 ? 0 : Math.abs(x) >= 10 ? 1 : 2;
    return x.toFixed(d).replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, '') + suffix;
  };
  if(abs >= 1e9) return pick(1e9, 'B');
  if(abs >= 1e6) return pick(1e6, 'M');
  if(abs >= 1e4) return pick(1e3, 'K');
  return fmt(v);
};
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const ms = v => v == null ? '—' : (v >= 1000 ? (v/1000).toFixed(2) + ' s' : Math.round(v) + ' ms');
const pct = v => v == null ? '—' : v.toFixed(1) + '%';

/* ---- OpenRouter 价估算（等价 token 花费） ---- */
// 后端按权威定价快照把每条请求的 token 折算成人民币；这里只负责展示，
// 展示货币默认人民币，可切美元（按接口返回的 usd_cny 折算，无需重新请求）。
let costCurrency = 'CNY';
try { if(localStorage.getItem('wb-cost-currency') === 'USD') costCurrency = 'USD'; } catch(e) {}
// 汇率由 /usage、/usage/recent、/usage/analytics 的 usd_cny 字段更新。
let costUsdRate = 7.1;
// 价估算总开关（后端设置 pricing_enabled，默认开）。关掉只是为了省掉算价与
// 渲染开销：价格列与卡片都留在原地，内容显示「—」，观感不变。
let PRICING_ON = true;
function costSymbol(){ return costCurrency === 'USD' ? '$' : '￥'; }
// 小额保留更多小数位，避免 0.00x 被显示成 0.00；与费用估算的展示口径一致。
function fmtCostVal(val, sym){
  const a = Math.abs(val);
  if(a > 0 && a < 0.01) return sym + val.toFixed(4);
  if(a < 1) return sym + val.toFixed(3);
  if(a < 1000) return sym + val.toFixed(2);
  return sym + val.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
}
function fmtCost(cny){
  if(cny == null || cny === '') return '—';
  const raw = Number(cny) || 0;
  const n = costCurrency === 'USD' ? raw / (costUsdRate || 1) : raw;
  return fmtCostVal(n, costSymbol());
}
// size 是数值字号（默认 18px，网关页的卡片用）：KPI 卡片窄，调用方可以调小。
// 中间那个「/」跟网关页「次」的处理保持一致：小一号、dim 色、左右各 4px。
// 红/绿用主题变量而不是写死的色值：深色主题下要跟着变，而且旁边
// 「请求成功率」的绿就是 --accent2，两张卡并排时是同一个绿。
function fmtCostBoth(cny, size){
  if(cny == null || cny === '') return '—';
  const raw = Number(cny) || 0;
  const fs = size || 18;
  const sep = Math.max(10, Math.round(fs * 0.6));
  const cnyStr = fmtCostVal(raw, '￥');
  const usdStr = fmtCostVal(raw / (costUsdRate || 7.1), '$');
  return '<span style="color:var(--bad);font-size:' + fs + 'px;font-weight:700">' + cnyStr + '</span> ' +
         '<span style="color:var(--dim);font-weight:500;font-size:' + sep + 'px;margin:0 4px">/</span> ' +
         '<span style="color:var(--accent2);font-size:' + fs + 'px;font-weight:700">' + usdStr + '</span>';
}

// ---- 表头点击排序 ---------------------------------------------------------
// 默认对所有数据表生效：任何带 id 的表都会自动接上，新加一张表不用改这里。例外
// 只有三处登记——SORT_DENY_TABLES（整表不排）、SORT_COL_KINDS（列类型 / 不可排）、
// SORT_PIN_ROWS（表体开头的汇总行）。
//
// 排的是渲染后的 DOM 行，不是回头排数据数组：单元格里已经是最终显示值，按数据重排
// 就得把那些格式化逻辑再抄一遍，两处不同步「看到的值」和「排出来的顺序」就会打架。
//
// 状态放在模块级变量里，所以 5 秒一次的整表重画不会把排序丢掉；没点过任何表头时
// SORT_STATE 里没有条目，各面板原本的默认顺序（多数是服务端顺序）原样保留。
//
// 手型、tooltip、箭头一律运行时挂到 DOM 上，源码标记一个字都不动——积分扣减历史表
// 的表头必须保持裸 <th>，最近请求表 / 禁用表 / 按 Key 表 / 按账号表的行必须保持裸
// <tr>，这几条都有测试写死。

/* 整表不参与排序。默认任何带 id 的表都可排——新加一张表不必改这里。登记进来的
   三条，理由各是：
     recent              最近请求严格按时间倒序，顺序本身就是要看的信息
     desktopScanBody     桌面扫描结果按「有效 / 无效 / 需要 Key」分组，跨组排序会打散分组
     creditsModalContent 套餐权益包明细，已经有 #cmPkgSort 下拉（含「最快到期优先」这类语义排序） */
const SORT_DENY_TABLES = new Set(['recent', 'desktopScanBody', 'creditsModalContent']);

/* 列类型的显式登记。默认按单元格内容推断（见 inferColKind），登记过的表按登记
   的来——「进度 4 / 10」「剩余 42 / 50 积分」「首字延迟 1.89 s (P50 1.90 s)」
   这类复合单元格只取其中一个数字是错的，得点名。

   下标 = 表头列序号。'num' 取单元格里的数字，'text' 按文本比，null = 该列不排
   （下拉框、按钮、拼给人看的标签串）。 */
const SORT_COL_KINDS = {
  modelsTable:      ['text', 'text', 'num', 'text', 'num', 'num', 'text', 'text'],
  growthTable:      ['text', 'text', 'num', 'num', 'text'],
  // 时间列是 ISO 串，字典序就是时间序，不必另给排序值。
  creditHistoryTable: ['text', 'text', 'text', 'num', 'num'],
  // 这两张表的时间列显示的是 MM-DD HH:MM，跨年字典序就错了，排序值由 render
  // 挂 data-sort 单独给（见 renderRemainingUsage / renderCreditGrants）。
  remainingUsageTable: ['text', 'text', 'text', 'text', 'num', 'num', 'num'],
  creditGrantsTable:   ['text', 'text', 'text', 'text', 'text', 'num', 'num', 'text', 'text'],
  analyticsAccountTable: ['text', 'text', 'num', 'num', 'num', 'num', null, 'num'],
  analyticsKeyTable: ['text', 'text', 'num', 'num', 'num', 'num', 'num', null],
  perfMatrix:       ['text', 'text', 'text', 'num', 'num', 'num', 'num', 'num',
                     'num', 'num', 'num', 'num', 'num'],
  accounts:         ['text', 'text', 'text', null, 'num', 'num', 'num', 'num', null],
  // 名称 / 代理地址是 <input>（textContent 恒为空串，按它排等于没排）、启用是复选框、
  // 操作是按钮列——登记成 null，别让表头挂上一个点了没反应的箭头。
  slotList:         [null, null, 'text', null, 'num', null],
  disabledList:     ['text', 'text', 'text', 'text', 'text', 'text'],
};
/* 表体开头有几行是汇总行、不是数据行：矩阵面板的「全部模型合计」钉在表体第一行，
   它跟着表头走，不参与排序。 */
const SORT_PIN_ROWS = {perfMatrix: 1};
const SORT_STATE = {};
const SORT_HINT = '按此列排序（再点一次反向）';
/* 可排但当前没在排的列挂这个双向箭头——光靠手型和 tooltip 声明不了「这一列能排」，
   截图里看不见、触屏上也没有 hover。它是「能排」的常驻声明，▼ / ▲ 是「正在按这一列
   排」的状态，两者互斥。 */
const SORT_ARROW_IDLE = '⇅';
const SORT_KEY_PREFIX = 'wb-sort-';

/* 刷新后接着上次的选择排（键名照 wb-theme 的习惯）。读回来的列序号可能对不上
   表头——列增减、顺序调整都会让同一个序号指向别的字段，所以越界或那一列已经
   不可排就整条丢弃，退回该面板原本的默认顺序。

   遍历 localStorage 的键而不是 SORT_COL_KINDS：没登记过的表也存过选择，那些
   要一起恢复（列序号等 applyTableSort 推断出列类型时再校验）。 */
try {
  for(const key of Object.keys(localStorage)){
    if(key.indexOf(SORT_KEY_PREFIX) !== 0) continue;
    const id = key.slice(SORT_KEY_PREFIX.length);
    if(SORT_DENY_TABLES.has(id)) continue;
    const m = /^(\d+):(asc|desc)$/.exec(localStorage.getItem(key) || '');
    if(!m) continue;
    const col = Number(m[1]);
    const spec = SORT_COL_KINDS[id];
    if(spec && !spec[col]) continue;   // 登记过的表：越界或该列本就不排
    SORT_STATE[id] = {col: col, dir: m[2]};
  }
} catch(e) {}

/* 一个单元格的排序取值。返回 {text, forced}：forced 表示这个值是 data-sort 给的，
   不是显示出来的那个——那是给「显示的不是能排序的那个数」的列用的：剩余用量估算与
   积分获取历史的时间列显示 MM-DD HH:MM，跨年字典序就错，排序要的是 ISO 串。 */
function cellRaw(cell){
  const forced = cell.getAttribute ? cell.getAttribute('data-sort') : null;
  if(forced != null && forced !== '') return {text: String(forced), forced: true};
  return {text: String(cell.textContent == null ? '' : cell.textContent).trim(), forced: false};
}

/* 单元格取值。数字列忽略千分位、货币符号和百分号，并换算单位——否则「950 ms」和
   「4.01 s」会按 950 > 4.01 排反。量级单位只认大写 K/M/G：小写 m 在界面上是
   「分钟」（fmtLeft 的 29m），当成百万去缩放会把冷却时间排得乱七八糟。取不到数的
   格（「—」「-」「未查询」）返回 null，恒沉底，不随升降序翻转。 */
function cellSortValue(cell, kind){
  if(!cell) return null;
  const raw = cellRaw(cell);
  const text = raw.text;
  if(kind === 'text') return text || null;
  // data-sort 给的已经是排序用的那个值，不再做千分位 / 单位解析。
  if(raw.forced){
    const n = Number(text);
    return isFinite(n) ? n : null;
  }
  const m = text.match(/(-?\d[\d,]*(?:\.\d+)?)\s*([A-Za-z%]*)/);
  if(!m) return null;
  const n = Number(m[1].replace(/,/g, ''));
  if(!isFinite(n)) return null;
  switch(m[2]){
    case 's': return n * 1000;   // 换算到毫秒，好跟同一列里的 ms 比
    case 'K': return n * 1e3;
    case 'M': return n * 1e6;
    case 'G': return n * 1e9;
    case 'B': return n * 1e9;   // fmtCompact 用 B 表示十亿，别让那一格按 1.5 排序
    default: return n;           // ms / % / 空 / 其它后缀（tok/s 之类）一律原值
  }
}

/* 整格就是一个「数字 + 可选单位」才算数字格。锚定整串而不是「搜到数字就算」：
   「glm-5.3」「10-09 16:19」里也有数字，但那两列该按文本排。 */
const SORT_NUM_CELL = /^[\s￥$]*(-?\d[\d,]*(?:\.\d+)?)\s*([A-Za-z%/]*)$/;
/* 取不到值的占位格，不参与整列的类型判断。 */
const SORT_BLANK_CELL = /^(—|-|–|未查询|无调用|无)$/;
/* 单元格里是交互控件的列不排：「出口」是下拉框、「操作」是按钮，拿它们的文字比
   大小没有意义。放在推断里而不是逐表登记，新表也能自动免掉这类列。 */
const SORT_WIDGET_SEL = 'select,button,input,textarea,a';

/* 整列的类型：'num' / 'text' / null（不可排）。只要有一个格按文本看，整列就按文本
   排——同一列里一半数字一半文本，混着比会乱。 */
function inferColKind(rows, col){
  let sawNum = false;
  for(const r of rows){
    const cell = r.children[col];
    if(!cell) continue;
    if(cell.querySelector && cell.querySelector(SORT_WIDGET_SEL)) return null;
    const text = cellRaw(cell).text;
    if(!text || SORT_BLANK_CELL.test(text)) continue;
    if(SORT_NUM_CELL.test(text)) sawNum = true;
    else return 'text';
  }
  return sawNum ? 'num' : null;
}

/* 一列的可排性与类型：登记过的表查表，没登记的按内容推断。 */
function colKind(id, i, rows, nCols){
  const spec = SORT_COL_KINDS[id];
  /* 登记表的列数与实际表头对不上时（上游加了一列或删了一列）不硬套登记——硬套
     会让每个序号都指向错的列，排出来的顺序看着对、其实全错。退回按内容推断：
     某一列可能排得不如登记时准，但不会错位。 */
  if(spec && spec.length === nCols) return spec[i] || null;
  return inferColKind(rows, i);
}

/* 按当前选中的列重排表体。 */
function applyTableSort(id){
  if(SORT_DENY_TABLES.has(id)) return;
  const root = document.getElementById(id);
  const table = !root ? null
    : (root.tagName === 'TABLE' ? root : root.querySelector('table'));
  if(!table) return;
  const thead = table.querySelector('thead');
  const tbody = table.querySelector('tbody');
  // 没有表头表体的不是数据表（KV 列表、纯布局表），不接。
  if(!thead || !tbody) return;
  // 把「表 → 登记项」接上，委托的点击监听才知道点的是哪张表。运行时挂，不进标记。
  table.setAttribute('data-sort-id', id);
  const rows = Array.from(tbody.children).filter(r => r.tagName === 'TR');
  const pinN = SORT_PIN_ROWS[id] || 0;
  /* 续行（账号表里 cool-row 的冷却 / 达限胶囊）是上一行的附注，不是独立数据行：
     只排 accountRow 会把胶囊留在原地，排完就是「账号 账号 胶囊 胶囊」——胶囊跟
     的不是它自己那行账号。按「行 + 它的续行」成组排，整块一起走。 */
  const isCont = r => !!(r.classList && r.classList.contains('cool-row'));
  const units = [];
  for(const r of rows){
    if(units.length && isCont(r)) units[units.length - 1].extras.push(r);
    else units.push({row: r, extras: []});
  }
  const body = units.slice(pinN).map(u => u.row);
  const ths = Array.from(thead.querySelectorAll('th'));
  const kinds = ths.map((th, i) => colKind(id, i, body, ths.length));
  const st = SORT_STATE[id] || null;
  ths.forEach((th, i) => {
    const kind = kinds[i];
    const active = !!(kind && st && st.col === i);
    if(!kind){
      /* 这一列不可排（登记成 null，或者推断出来整列没有可比内容）。把上一轮可能
         挂过的装饰摘干净——否则某一列的数据变空之后，表头还留着旧的手型和箭头，
         点上去没反应。 */
      th.classList.remove('sortable');
      th.removeAttribute('title');
      th.removeAttribute('tabindex');
      th.removeAttribute('aria-sort');
      const dead = th.querySelector('.sort-arrow');
      if(dead && dead.remove) dead.remove();
      return;
    }
    th.classList.add('sortable');
    /* tooltip 只补一次：i18n 会把它换成译文（title 在 ATTRS 里），每轮重写成中文
       会让它再翻一遍——英文 / 繁中界面下每 5 秒白跑一轮翻译。 */
    if(!th.title) th.title = SORT_HINT;
    // 可 Tab 聚焦，回车 / 空格触发（见下面那条 keydown 委托）。
    th.setAttribute('tabindex', '0');
    th.setAttribute('aria-sort', active ? (st.dir === 'asc' ? 'ascending' : 'descending') : 'none');
    /* 箭头只在真要变的时候动 DOM：这个函数每轮轮询都会跑一遍，白改一次就会多
       触发一轮 i18n 的 MutationObserver 重扫。 */
    const old = th.querySelector('.sort-arrow');
    const want = active ? (st.dir === 'asc' ? '▲' : '▼') : SORT_ARROW_IDLE;
    const cls = 'sort-arrow' + (active ? '' : ' idle');
    if(old){
      if(old.textContent !== want) old.textContent = want;
      if(old.className !== cls) old.className = cls;
    }else{
      // 前面不留空格：箭头是绝对定位的，不参与行内排版，留一个空格只会变成表头
      // 文本末尾的一个多余空白（读屏和复制表头时会带上）。
      th.insertAdjacentHTML('beforeend',
        '<span class="' + cls + '" aria-hidden="true">' + want + '</span>');
    }
  });
  if(!st || !kinds[st.col]) return;
  const kind = kinds[st.col];
  const dir = st.dir === 'asc' ? 1 : -1;
  const sorted = units.slice(pinN)
    .map((u, i) => [cellSortValue(u.row.children[st.col], kind), i, u])
    .sort((a, b) => {
      const av = a[0], bv = b[0];
      if(av == null || bv == null){
        if(av == null && bv == null) return a[1] - b[1];
        return av == null ? 1 : -1;   // 缺值的行沉底，两个方向都一样
      }
      const c = (typeof av === 'number' && typeof bv === 'number')
        ? av - bv : String(av).localeCompare(String(bv), 'zh-Hans-CN', {numeric: true});
      return c ? c * dir : a[1] - b[1];   // 同值保持原序，表不会自己抖
    })
    .map(t => t[2]);
  const ordered = units.slice(0, pinN).concat(sorted)
    .reduce((acc, u) => acc.concat([u.row], u.extras), []);
  /* 顺序没变就别碰 DOM：每一轮轮询都会走这里，白挪一遍行没有任何收益，只会多触发
     一轮 i18n 的 MutationObserver 重扫。 */
  if(ordered.some((r, i) => rows[i] !== r)){
    // appendChild 对已在册的子节点是「移动」，所以这一遍就把顺序定下来了。
    ordered.forEach(r => tbody.appendChild(r));
  }
}

/* 扫一遍页面上所有数据表，把排序接上。挂在每 5 秒那条轮询上，新加一张表只要带
   id 就自动可排——不必在它的 render 函数里再写一行 applyTableSort。

   「带 id」这条把非数据表挡在外面：设置页的护栏表单、运行信息 / 其他客户端的 KV
   列表都没有 id（它们本来也不是能按列比大小的东西）。 */
function applyAllTableSorts(){
  if(!document.querySelectorAll) return;
  Array.from(document.querySelectorAll('table')).forEach(t => {
    const id = sortTableId(t);
    if(id) applyTableSort(id);
  });
}

/* 表的 id：自己带就用自己，否则找最近带 id 的祖先——最近请求、账号列表这些表是
   innerHTML 生成的，id 在包着它们的容器上。

   页面容器（.main-page）不算：那是「这一页」的 id，不是「这张表」的 id，拿它当
   表 id 会把设置页的护栏表单、智能体页的表格一起误认成数据表。都没有就不接。 */
function sortTableId(table){
  let el = table;
  while(el && el !== document.body){
    if(el.id && !(el.classList && el.classList.contains('main-page'))) return el.id;
    el = el.parentElement || el.parentNode;
  }
  return '';
}

/* 同一列再点一次翻转方向；换一列一律从降序开始——「最大的排最上面」是这些表
   最常用的读法。 */
function toggleTableSort(id, col){
  if(SORT_DENY_TABLES.has(id)) return;
  const st = SORT_STATE[id];
  if(st && st.col === col) st.dir = st.dir === 'desc' ? 'asc' : 'desc';
  else SORT_STATE[id] = {col: col, dir: 'desc'};
  try { localStorage.setItem(SORT_KEY_PREFIX + id, col + ':' + SORT_STATE[id].dir); } catch(e) {}
  applyTableSort(id);
}

/* 点击与键盘两条委托都从这里问「点到 / 按到的是哪张表的哪一列」。表是每 5 秒整体
   重画的，逐个头挂监听就得跟着重挂，所以委托到 document。只认挂过 sortable 的表头
   ——不可排的列（下拉框、按钮、标签串）没挂，点了没反应。 */
function sortHeaderAt(e){
  const th = e.target && e.target.closest ? e.target.closest('th') : null;
  if(!th || !th.parentNode) return null;
  if(!th.classList || !th.classList.contains('sortable')) return null;
  const table = th.closest('table');
  const id = table && table.getAttribute ? table.getAttribute('data-sort-id') : null;
  if(!id) return null;
  const col = Array.from(th.parentNode.children).indexOf(th);
  return col < 0 ? null : {id: id, col: col};
}

document.addEventListener('click', e => {
  const hit = sortHeaderAt(e);
  if(hit) toggleTableSort(hit.id, hit.col);
});

/* 键盘走同一条路。空格在浏览器里默认是滚动页面，所以只在表头上吃掉它。 */
document.addEventListener('keydown', e => {
  if(e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return;
  const hit = sortHeaderAt(e);
  if(!hit) return;
  e.preventDefault();
  toggleTableSort(hit.id, hit.col);
});

function setCostCurrency(cur){
  costCurrency = (cur === 'USD') ? 'USD' : 'CNY';
  try { localStorage.setItem('wb-cost-currency', costCurrency); } catch(e) {}
  renderCostToggle();
  // 已经渲染出来的表格/卡片就地换算重画，不必等下一轮轮询。
  if(typeof refreshAllCostViews === 'function') refreshAllCostViews();
}
// 悬停里说清这个数是怎么来的：哪条定价策略算的、三档单价多少、落在哪个条件
// 档、这条价是怎么匹配到 OpenRouter 条目的、是不是补算。
//
// 用自绘气泡而不是原生 title：单价要按列对齐才好读，而且原生 tooltip 由浏览器
// 绘制，截图验收时根本截不到。内容全部来自 /usage/recent 每行自带的 cost_* 字
// 段，前端一个价都不重算。
const COST_VIA_TEXT = {
  direct: '直接匹配（归一化同名且唯一命中）',
  override: '人工映射表 OVERRIDES',
  variant: '变体后缀继承'
};
// 变体继承剥掉的后缀：基准名是原始名去掉尾部一段（剥的是后缀，留下的基准名
// 就成了前缀），差值就是被剥的那截。
function costVariantSuffix(r){
  const base = r.cost_inherited_from;
  const name = String(r.model || '');
  return (base && name.length > base.length && name.slice(0, base.length) === base)
    ? name.slice(base.length) : '';
}
// 每百万 token 的美元原价：按原样显示，不折算、不补齐。
function fmtRate(v){
  if(v == null || v === '') return '—';
  const n = Number(v);
  if(!isFinite(n)) return '—';
  return n.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 6});
}
function costUnitLabel(r){
  const n = Number(r.cost_unit);
  return (isFinite(n) && n > 0) ? fmt(n) + ' token' : '百万 token';
}
// 气泡的结构化内容：{lines: 纯文字行, rates: 三档单价, notes: 补充说明}。
// 文本与 HTML 两种渲染共用这一份，口径不会两处走样。
function costTipModel(r){
  if(!r || r.cost_cny == null){
    return {lines: [], rates: [], notes: ['该模型暂无定价数据']};
  }
  const sym = (r.cost_currency === 'CNY') ? '￥' : '$';
  const lines = [];
  if(r.cost_source === 'builtin'){
    lines.push('定价依据：出厂快照（还没有取到策略）');
  } else if(r.cost_source){
    lines.push('定价策略：' + r.cost_source
      + (r.cost_source_at ? '（' + new Date(r.cost_source_at * 1000).toLocaleString() + ' 首次取到）' : ''));
  }
  const via = r.cost_via || 'direct';
  if(via === 'override'){
    lines.push('匹配来源：' + COST_VIA_TEXT.override
      + (r.cost_via_derived ? '（策略早于该字段，按当前映射表推断）' : '')
      + '：' + (r.cost_override_from || r.model) + ' → ' + (r.cost_or_id || '—'));
  } else if(via === 'variant'){
    const cut = costVariantSuffix(r);
    lines.push('匹配来源：' + COST_VIA_TEXT.variant + '：' + r.model
      + ' → ' + (r.cost_inherited_from || '—') + '（基准） → ' + (r.cost_or_id || '—'));
    if(cut) lines.push('剥掉的后缀：' + cut);
  } else {
    lines.push('匹配来源：' + COST_VIA_TEXT.direct + '：' + (r.cost_or_id || '—'));
  }
  const rates = r.cost_rates || {};
  const hit = rates.input_cache_hit;
  const rows = [
    {k: '输入（缓存未命中）', v: sym + fmtRate(rates.input_cache_miss)},
    {k: '输入（缓存命中）', v: hit == null ? '未公布，按未命中价计' : sym + fmtRate(hit)},
    {k: '输出', v: sym + fmtRate(rates.output)}
  ];
  const notes = [];
  if(r.cost_usd_cny != null){
    notes.push('汇率：1 USD = ' + fmtRate(r.cost_usd_cny) + ' CNY（策略记录的汇率，'
      + '上面的美元单价按它折算成人民币）');
  }
  if(r.cost_band != null){
    notes.push('条件档位：第 ' + (r.cost_band + 1) + ' 档'
      + (r.cost_band_note ? ' · ' + r.cost_band_note : '') + '，本行按该档单价计');
  }
  if(r.cost_backfilled){
    notes.push('补算：这条请求发生在该策略取到之前，用之后第一次取到的价补算');
  }
  return {lines: lines, rates: rows, notes: notes};
}
// 纯文本版（多行），给无气泡的场合与单元测试用。
function costTitle(r){
  const m = costTipModel(r);
  const out = ['按 OpenRouter 公布的模型价折算的等价 token 花费'];
  out.push.apply(out, m.lines);
  if(m.rates.length){
    out.push('单价（USD / ' + costUnitLabel(r) + '，OpenRouter 原始价，未折算）');
    m.rates.forEach(row => out.push('　' + row.k + '：' + row.v));
  }
  out.push.apply(out, m.notes);
  return out.join('\n');
}
function costTipHtml(r){
  const m = costTipModel(r);
  if(!m.rates.length && !m.lines.length){
    return '<div class="ct-note">' + esc(m.notes.join('')) + '</div>';
  }
  let html = '<div class="ct-h">按 OpenRouter 公布的模型价折算的等价 token 花费</div>';
  m.lines.forEach(line => {
    html += '<div class="ct-line">' + esc(line) + '</div>';
  });
  html += '<div class="ct-sep"></div>'
    + '<div class="ct-h">单价 · USD / ' + esc(costUnitLabel(r)) + ' · 原始价，未折算</div>';
  m.rates.forEach(row => {
    html += '<div class="ct-row"><span class="ct-k">' + esc(row.k)
      + '</span><span class="ct-v">' + esc(row.v) + '</span></div>';
  });
  m.notes.forEach(note => {
    html += '<div class="ct-note">' + esc(note) + '</div>';
  });
  return html;
}
// 气泡只此一个，挂在 body 上；行数据按本次渲染的 key 索引，重画后按 key 复位。
let COST_TIP_EL = null;
let COST_TIP_KEY = null;
const COST_TIP_ROWS = new Map();
function costRowKey(r, i){
  return [r.at, r.model, r.account, r.prompt_tokens, r.completion_tokens,
          r.cached_tokens, r.elapsed_ms, i].join('|');
}
function costTipEl(){
  if(!COST_TIP_EL){
    COST_TIP_EL = document.createElement('div');
    COST_TIP_EL.id = 'costTip';
    document.body.appendChild(COST_TIP_EL);
  }
  return COST_TIP_EL;
}
function hideCostTip(){
  COST_TIP_KEY = null;
  if(COST_TIP_EL) COST_TIP_EL.style.display = 'none';
}
function showCostTip(cell, r){
  const el = costTipEl();
  el.innerHTML = costTipHtml(r);
  el.style.display = 'block';
  const box = cell.getBoundingClientRect();
  const w = el.offsetWidth, h = el.offsetHeight;
  // 贴着列尾右对齐，越界就往回收；放不下就翻到单元格下面。
  let left = Math.max(8, Math.min(box.right - w, window.innerWidth - w - 8));
  let top = box.top - h - 8;
  if(top < 8) top = Math.min(box.bottom + 8, window.innerHeight - h - 8);
  el.style.left = left + 'px';
  el.style.top = Math.max(8, top) + 'px';
}
// 表格每 5 秒整体重画一次，重画后把气泡复位到同一行，别让它悬在旧位置。
function reanchorCostTip(){
  if(!COST_TIP_KEY) return;
  const cells = document.querySelectorAll('[data-cost-key]');
  for(let i = 0; i < cells.length; i++){
    if(cells[i].getAttribute('data-cost-key') === COST_TIP_KEY){
      const r = COST_TIP_ROWS.get(COST_TIP_KEY);
      if(r){ showCostTip(cells[i], r); return; }
    }
  }
  hideCostTip();
}
document.addEventListener('mouseover', e => {
  const cell = e.target && e.target.closest ? e.target.closest('[data-cost-key]') : null;
  if(!cell) return;
  const key = cell.getAttribute('data-cost-key');
  if(!key || key === COST_TIP_KEY) return;
  const r = COST_TIP_ROWS.get(key);
  if(!r) return;
  COST_TIP_KEY = key;
  showCostTip(cell, r);
});
document.addEventListener('mouseout', e => {
  const cell = e.target && e.target.closest ? e.target.closest('[data-cost-key]') : null;
  if(!cell) return;
  if(e.relatedTarget && cell.contains(e.relatedTarget)) return;
  hideCostTip();
});
function renderCostToggle(){
  document.querySelectorAll('.cost-toggle').forEach(el => {
    el.querySelectorAll('button').forEach(b => {
      b.classList.toggle('active', b.dataset.cur === costCurrency);
    });
  });
}
// CNY/USD 二选一的切换按钮组；用于费用卡片与表格标题区。
function costToggleHtml(){
  return '<span class="cost-toggle" style="display:inline-flex;gap:4px">'
    + '<button type="button" class="range-btn' + (costCurrency === 'CNY' ? ' active' : '') + '" data-cur="CNY" onclick="setCostCurrency(\'CNY\')">￥ 人民币</button>'
    + '<button type="button" class="range-btn' + (costCurrency === 'USD' ? ' active' : '') + '" data-cur="USD" onclick="setCostCurrency(\'USD\')">$ 美元</button>'
    + '</span>';
}
// 输入 / 输出 / 思考 三个数字，各自一色，斜杠分隔。思考为 0 也写出来，
// 列的形状因此固定，不会因某个模型不推理就缩短。
const tripleTokens = (inp, out, think) =>
  '<span style="color:var(--accent)">' + fmt(inp || 0) + '</span>' +
  ' <span style="color:var(--dim-light)">/</span> ' +
  '<span style="color:var(--accent2)">' + fmt(out || 0) + '</span>' +
  ' <span style="color:var(--dim-light)">/</span> ' +
  '<span style="color:var(--think)">' + fmt(think || 0) + '</span>';

function perfRow(label, b, unit, fmtFn, color){
  if(!b) return '<tr><td>' + label + '</td><td colspan="5" style="color:var(--dim)">暂无数据</td></tr>';
  const f = fmtFn || (v => v);
  const dot = color ? ' <i style="display:inline-block;width:7px;height:7px;border-radius:2px;background:' + color + ';margin-left:6px"></i>' : '';
  return '<tr><td>' + label + dot + '</td>'
    + '<td><b>' + f(b.avg) + '</b>' + unit + '</td>'
    + '<td>' + f(b.p50) + unit + '</td>'
    + '<td>' + f(b.p90) + unit + '</td>'
    + '<td>' + f(b.p99) + unit + '</td>'
    + '<td style="color:var(--dim)">' + b.samples + ' 次</td></tr>';
}

function card(k, v, sub, cls){
  return '<div class="card ' + (cls||'') + '"><div class="k">' + k + '</div>'
    + '<div class="v">' + v + '</div>' + (sub ? '<div class="s">' + sub + '</div>' : '') + '</div>';
}
