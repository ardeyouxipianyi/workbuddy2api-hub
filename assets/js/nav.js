/* ---- 页面区块导航 ----
   导航项完全由页面内的 section 现场生成：标题取该 section 首个 h2 的文本
   （忽略用于显示状态的 <em>），锚点 id 由标题内容哈希得到。对所有
   .main-page 生效，所以新增页面、增删区块或调整顺序都不需要改这里。 */
const PAGE_NAV_TOP_VAR = '--page-nav-top';
/* 侧栏收起状态跨页面、跨刷新共用一份 */
const PAGE_NAV_COLLAPSE_KEY = 'wb-page-nav-collapsed';
/* 只有一项的页面不给侧栏：白占一列宽度，也没什么可跳的 */
const PAGE_NAV_MIN_SECTIONS = 2;
let pageNavLock = 0;
let pageNavTimer = 0;

/* 参与导航的区块：页面内最外层的 section（嵌套的 section 属于父区块的内容，
   不单独成项），自身没被 display:none 隐藏，且带 h2 标题。判据用 computed
   display 而不是 offsetParent —— 后者在整页隐藏（切到其它标签页）时会把所有
   区块都判成不可见，导航会整个空掉。 */
function pageNavSections(page){
  if(!page) return [];
  return Array.from(page.querySelectorAll('section')).filter(function(sec){
    if(getComputedStyle(sec).display === 'none') return false;
    if(!sec.querySelector('h2')) return false;
    for(let p = sec.parentElement; p && p !== page; p = p.parentElement){
      if(p.tagName === 'SECTION') return false;
    }
    return true;
  });
}

function pageNavLabel(section, index){
  const h2 = section.querySelector('h2');
  if(h2){
    const clone = h2.cloneNode(true);
    // em 是计数/版本这类附属标注；.page-nav-exclude 是标题里那些不该进导航的
    // 控件（例如「网关模型清单」标题里的 全选 / 全不选 / 反选 按钮）——它们
    // 只是摆在同一行，不是标题文本，混进来会让导航项和锚点 id 都带上按钮名。
    clone.querySelectorAll('em, .page-nav-exclude').forEach(el => el.remove());
    const text = (clone.textContent || '').replace(/\s+/g, ' ').trim();
    if(text) return text;
  }
  return '未命名区块 ' + (index + 1);
}

/* 由标题内容导出稳定 id：与区块顺序无关，链接可长期复用 */
function pageNavAnchorId(label, taken){
  let hash = 5381;
  for(let i = 0; i < label.length; i++) hash = ((hash << 5) + hash + label.charCodeAt(i)) >>> 0;
  const base = 'page-sec-' + hash.toString(36);
  let id = base, n = 2;
  while(taken.has(id)) id = base + '-' + (n++);
  return id;
}

/* 区块清单的指纹：只有它变了才重建导航，免得轮询重建表格时把高亮抖掉 */
function pageNavSignature(page){
  return pageNavSections(page).map(function(sec, i){ return pageNavLabel(sec, i); }).join('\u0001');
}

/* 跳转时要让开的距离：吸顶 header，窄屏下再加上同样吸顶的导航条本身 */
/* 吸顶 header 占掉的高度。手机断点上 header 不吸顶（随页面滚走），占位为 0。 */
function stickyHeaderHeight(){
  const header = document.querySelector('header');
  if(!header) return 64;
  if(getComputedStyle(header).position === 'static') return 0;
  return header.offsetHeight;
}

function pageNavHeaderOffset(page){
  const narrow = window.matchMedia('(max-width:860px)').matches;
  // 窄屏下二级导航条紧贴 header 底边（见 syncPageNavOffset），不再留 14px 缝，
  // 否则内容会从两条吸顶栏之间的缝隙里穿过去。
  const base = stickyHeaderHeight() + (narrow ? 0 : 14);
  const sidebar = page ? page.querySelector('.page-sidebar') : null;
  if(sidebar && narrow){
    return base + sidebar.offsetHeight + 10;
  }
  return base;
}

function syncPageNavOffset(){
  const narrow = window.matchMedia('(max-width:860px)').matches;
  const top = stickyHeaderHeight() + (narrow ? 0 : 14);
  document.documentElement.style.setProperty(PAGE_NAV_TOP_VAR, top + 'px');
}

function setActivePageNav(page, id){
  const nav = page ? page.querySelector('.page-nav') : null;
  if(!nav) return;
  nav.querySelectorAll('.page-nav-item').forEach(function(item){
    item.classList.toggle('active', !!id && item.dataset.target === id);
  });
}

function scrollToPageSection(page, id, smooth){
  const section = document.getElementById(id);
  if(!section) return;
  const top = section.getBoundingClientRect().top + window.pageYOffset - pageNavHeaderOffset(page);
  window.scrollTo({ top: Math.max(0, top), behavior: smooth === false ? 'auto' : 'smooth' });
  setActivePageNav(page, id);
  // 平滑滚动期间抑制滚动高亮，避免中途掠过其它区块把高亮抢走
  pageNavLock = Date.now() + 700;
  section.classList.add('page-nav-flash');
  setTimeout(() => section.classList.remove('page-nav-flash'), 900);
  try {
    const url = new URL(window.location.href);
    url.hash = id;
    history.replaceState(null, '', url.toString());
  } catch(e){}
}

/* 回到顶部：和区块跳转一样在平滑滚动期间抑制滚动高亮，免得中途掠过其它区块
   把高亮抢走；顺便清掉锚点，免得刷新后又落回刚才那个区块。滚完不会再触发滚动
   事件，所以高亮得自己收尾到第一项。 */
function scrollPageToTop(page){
  window.scrollTo({ top: 0, behavior: 'smooth' });
  pageNavLock = Date.now() + 700;
  const first = page ? page.querySelector('.page-nav-item') : null;
  if(first) setActivePageNav(page, first.dataset.target);
  try {
    const url = new URL(window.location.href);
    history.replaceState(null, '', url.pathname + url.search);
  } catch(e){}
}

function pageNavSpy(){
  if(Date.now() < pageNavLock) return;
  const page = document.querySelector('.main-page.active.page-nav-on');
  if(!page) return;
  const nav = page.querySelector('.page-nav');
  if(!nav) return;
  const items = Array.from(nav.querySelectorAll('.page-nav-item'));
  if(!items.length) return;
  const threshold = pageNavHeaderOffset(page) + 8;
  let activeId = items[0].dataset.target;
  for(const item of items){
    const section = document.getElementById(item.dataset.target);
    if(!section) continue;
    if(section.getBoundingClientRect().top - threshold <= 0) activeId = item.dataset.target;
    else break;
  }
  // 滚到底时高亮最后一项，否则末尾区块永远点不亮
  if(window.innerHeight + window.pageYOffset >= document.documentElement.scrollHeight - 4){
    activeId = items[items.length - 1].dataset.target;
  }
  setActivePageNav(page, activeId);
}

/* 滚动高亮：滚动事件本身已按帧节流，这里不再叠一层 requestAnimationFrame
   （后台标签页会挂起 rAF，反而让高亮停在旧位置）。 */
function onPageNavScroll(){
  pageNavSpy();
}

function pageNavCollapsedPref(){
  try { return localStorage.getItem(PAGE_NAV_COLLAPSE_KEY) === '1'; } catch(e){ return false; }
}

/* 收起与否只体现为页面上的一个类，样式和窄屏回退都由 CSS 决定；按钮的
   文字/标题跟着状态走，收起后它就是唯一的入口。 */
function applyPageNavCollapse(page, collapsed){
  if(!page) return;
  page.classList.toggle('page-nav-collapsed', !!collapsed);
  const btn = page.querySelector('.page-sidebar-toggle');
  if(!btn) return;
  const label = collapsed ? '展开本页导航' : '收起本页导航';
  btn.textContent = collapsed ? '»' : '«';
  btn.title = label;
  btn.setAttribute('aria-label', label);
  btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
}

function togglePageNavCollapse(page){
  const collapsed = !page.classList.contains('page-nav-collapsed');
  try { localStorage.setItem(PAGE_NAV_COLLAPSE_KEY, collapsed ? '1' : '0'); } catch(e){}
  applyPageNavCollapse(page, collapsed);
}

function initPageNav(page){
  if(!page) return;
  syncPageNavOffset();
  const sections = pageNavSections(page);
  const signature = pageNavSignature(page);
  let sidebar = page.querySelector('.page-sidebar');

  // 区块不足两项（例如日志页只有一个视图）就不给侧栏，页面保持单栏
  if(sections.length < PAGE_NAV_MIN_SECTIONS){
    if(sidebar) sidebar.remove();
    page.classList.remove('page-nav-on', 'page-nav-collapsed');
    delete page.dataset.pageNavSignature;
    return;
  }
  // 清单没变就不动 DOM，保留现有高亮
  if(sidebar && page.dataset.pageNavSignature === signature) return;

  const taken = new Set(Array.from(document.querySelectorAll('[id]')).map(el => el.id));
  const anchors = [];
  sections.forEach(function(section, i){
    const label = pageNavLabel(section, i);
    let id = section.id;
    if(!id){
      id = pageNavAnchorId(label, taken);
      section.id = id;
    }
    taken.add(id);
    anchors.push({ id: id, label: label });
  });

  // 重建时保留原来的高亮，别因为一次重建就闪回第一项
  const prevActive = sidebar ? sidebar.querySelector('.page-nav-item.active') : null;
  const prevTarget = prevActive ? prevActive.dataset.target : null;

  // 内容整体包一层：grid 里只剩「侧栏 + 内容」两项，侧栏才跨得满整个内容区，
  // 区块之间的间距也不会被网格行间距再叠一次。
  let body = page.querySelector('.page-nav-body');
  if(!body){
    body = document.createElement('div');
    body.className = 'page-nav-body';
    Array.from(page.children).forEach(function(el){ if(el !== sidebar) body.appendChild(el); });
    page.appendChild(body);
  }

  if(!sidebar){
    sidebar = document.createElement('aside');
    sidebar.className = 'page-sidebar';
    sidebar.setAttribute('aria-label', '本页区块导航');
    page.insertBefore(sidebar, page.firstChild);
  }
  sidebar.textContent = '';
  const head = document.createElement('div');
  head.className = 'page-sidebar-head';
  const title = document.createElement('div');
  title.className = 'page-sidebar-title';
  title.textContent = '本页导航';
  head.appendChild(title);
  const collapseBtn = document.createElement('button');
  collapseBtn.type = 'button';
  collapseBtn.className = 'page-sidebar-toggle';
  collapseBtn.addEventListener('click', function(){ togglePageNavCollapse(page); });
  head.appendChild(collapseBtn);
  sidebar.appendChild(head);
  applyPageNavCollapse(page, pageNavCollapsedPref());

  // 排在区块清单之外：它不是一个区块，不参与高亮与滚动侦测；窄屏下也就能
  // 留在横向胶囊行的左端，不跟着一起滚走。
  const toTop = document.createElement('button');
  toTop.type = 'button';
  toTop.className = 'page-nav-top';
  toTop.textContent = '↑ 回到顶部';
  toTop.title = '回到顶部';
  toTop.addEventListener('click', function(){ scrollPageToTop(page); });
  sidebar.appendChild(toTop);

  const nav = document.createElement('nav');
  nav.className = 'page-nav';
  anchors.forEach(function(a){
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'page-nav-item';
    btn.dataset.target = a.id;
    btn.textContent = a.label;
    btn.title = a.label;
    btn.addEventListener('click', function(){ scrollToPageSection(page, a.id); });
    nav.appendChild(btn);
  });
  sidebar.appendChild(nav);

  page.classList.add('page-nav-on');
  page.dataset.pageNavSignature = signature;

  if(!window.__pageNavBound){
    window.__pageNavBound = true;
    window.addEventListener('scroll', onPageNavScroll, { passive: true });
    window.addEventListener('resize', function(){
      syncPageNavOffset();
      initPageNav(document.querySelector('.main-page.active'));
      onPageNavScroll();
    });
  }
  observePageNav();
  const keep = prevTarget && anchors.some(function(a){ return a.id === prevTarget; });
  setActivePageNav(page, keep ? prevTarget : anchors[0].id);
  onPageNavScroll();
}

/* 区块在运行时可能增删或整块显隐（例如国内版的成长任务区块），让导航跟上，
   免得侧栏停在旧清单上。只盯当前页：非当前页的变化在切过去时由 initPageNav()
   重算，没必要在后台一直扫。 */
function refreshPageNav(){
  pageNavTimer = 0;
  initPageNav(document.querySelector('.main-page.active'));
}

function observePageNav(){
  if(window.__pageNavObserved || typeof MutationObserver === 'undefined') return;
  window.__pageNavObserved = true;
  const schedule = function(records){
    // 导航自身重建/改高亮引起的变更不必再算一遍，否则滚动时会自激
    if(records && records.every(function(r){
      const el = r.target;
      return el && el.closest && el.closest('.page-sidebar');
    })) return;
    if(pageNavTimer) clearTimeout(pageNavTimer);
    pageNavTimer = setTimeout(refreshPageNav, 200);
  };
  new MutationObserver(schedule).observe(document.body, {
    childList: true, subtree: true, attributes: true, attributeFilter: ['style', 'class'],
  });
}

/* 带 #锚点 打开时直接落到对应区块 */
function restorePageNavHash(){
  const page = document.querySelector('.main-page.active.page-nav-on');
  if(!page) return;
  const id = (window.location.hash || '').slice(1);
  if(!id) return;
  // 被隐藏的区块不进导航，也不该作为锚点落点（否则会滚到一个不可见的位置）
  const navigable = pageNavSections(page).some(function(s){ return s.id === id; });
  if(navigable) scrollToPageSection(page, id, false);
}

// ==== 账号区折叠开始 ====
// 折叠纯粹是呈现：账号轮询（loadAccounts）、realm 切换、签到/启用停用等逻辑
// 都不碰这里，所以收起之后它们照常跑，也不会把区域顶开。偏好存在服务端的
// accounts/settings.json（accounts_collapsed），不用 localStorage。
let _accountsCollapsed = false;

function accountsSectionBody(){
  return document.getElementById('accountsBody');
}

// 唯一的写入口：aria-expanded、正文的 collapsed 类与内存状态一起改，
// 三者不可能不同步。
function applyAccountsCollapsed(collapsed){
  _accountsCollapsed = collapsed === true;
  const body = accountsSectionBody();
  const btn = document.getElementById('accountsToggle');
  if(body) body.classList.toggle('collapsed', _accountsCollapsed);
  if(btn) btn.setAttribute('aria-expanded', _accountsCollapsed ? 'false' : 'true');
  return _accountsCollapsed;
}

function accountsCollapsed(){
  return _accountsCollapsed;
}

// 显式选择走既有的 settings save 通道（一个键的 patch，不动别的设置）。
async function toggleAccountsSection(evt){
  if(evt && evt.preventDefault) evt.preventDefault();
  const next = !_accountsCollapsed;
  applyAccountsCollapsed(next);
  try {
    await postJSON('/settings/save', {accounts_collapsed: next});
  } catch(e) {
    toast('折叠状态保存失败: ' + e.message, 'bad');
  }
}
// ==== 账号区折叠结束 ====

// ==== 顶部页签显示开始 ====
// 顶部页签的显隐偏好。清单不写死：按 .main-nav 里现有的按钮现场生成，上游以后
// 增删页面这里自动跟上，不必同步改这份代码。
// 偏好存在服务端（accounts/settings.json 的 hidden_pages），与账号区折叠、
// 「(切换前)」行是同一套通道，换浏览器、重启服务都记得，不用 localStorage。

/* 「设置」固定保留：它是这个开关自己的入口，收起来之后面板里再没有别的地方能把它
   放回来。其余页面都能自由选择。 */
const PAGE_KEEP_VISIBLE = 'settings';
/* 页签 key 由按钮 id 推出（btnNavGateway → gateway），与 switchMainTab() 和启动
   脚本用的是同一套命名；id 不是这个形状的按钮直接跳过，不猜。 */
const NAV_BUTTON_PREFIX = 'btnNav';
/* 页面 key 的合法形状，与 wb_settings.PAGE_KEY_RE 一致 */
const PAGE_KEY_SHAPE = /^[a-z][a-z0-9_-]{0,31}$/;

let _hiddenPages = [];

function navPageKey(btn){
  const id = (btn && btn.id) || '';
  if(id.indexOf(NAV_BUTTON_PREFIX) !== 0) return '';
  return id.slice(NAV_BUTTON_PREFIX.length).toLowerCase();
}

function navPageRows(){
  const nav = document.querySelector('.main-nav');
  if(!nav) return [];
  const rows = [];
  Array.from(nav.querySelectorAll('.main-nav-btn')).forEach(function(btn){
    const key = navPageKey(btn);
    if(!key) return;
    rows.push({
      key: key,
      btn: btn,
      page: document.getElementById('page' + key.charAt(0).toUpperCase() + key.slice(1)),
    });
  });
  return rows;
}

/* 清单上的标签取页签按钮的文案。i18n 把原文留在文本节点的 __wbSrc 上（译文写在
   data 上），所以这里读 __wbSrc 拿到的一律是中文原文——插进设置页后由面板自己的
   i18n 观察者翻译，切语言时也跟着 restore/walk 走，不用在这里额外重建一次。
   没被 walk 过的节点（界面一直是中文）没有 __wbSrc，textContent 本身就是原文。 */
function navPageLabel(row){
  const btn = row.btn;
  const node = btn ? btn.firstChild : null;
  const source = (node && node.nodeType === 3 && node.__wbSrc != null)
    ? node.__wbSrc : (btn ? btn.textContent : '');
  return String(source || '').replace(/\s+/g, ' ').trim() || row.key;
}

function hiddenPages(){
  return _hiddenPages.slice();
}

/* 「设置」固定可见，所以一定有结果；真到了没有可见页面那一步，退回网关页。 */
function firstVisiblePage(){
  const rows = navPageRows();
  for(let i = 0; i < rows.length; i++){
    if(!pageHidden(rows[i].key)) return rows[i].key;
  }
  return 'gateway';
}

/* 这一页是否被收起。判断散在六个地方，两个方向写反一次就是「点一下收起来、
   再点一下还是收起来」，收进这里只留一处。 */
function pageHidden(key){
  return _hiddenPages.indexOf(key) >= 0;
}

/* 服务端下发的清单照单收下，但仍过一遍归一化：手改过的文件、将来的客户端或上游
   新增的页面都可能带来这里不认识的 key，不认识的忽略即可。「设置」无论从哪个来源
   来都不收起——不这样兜底，手改 settings.json 就能把面板唯一的入口关掉。
   归一化口径与 wb_settings._clean_page_keys 对齐（形状、去重、丢掉「设置」），
   免得两边记的清单不是一回事；条数上限只在服务端，面板一次最多提交自己那几个页签。 */
function normaliseHiddenPages(hidden){
  if(!Array.isArray(hidden)) return [];
  const out = [];
  hidden.forEach(function(key){
    if(typeof key !== 'string') return;
    const page = key.trim();
    if(!PAGE_KEY_SHAPE.test(page) || page === PAGE_KEEP_VISIBLE) return;
    if(out.indexOf(page) >= 0) return;
    out.push(page);
  });
  return out;
}

/* 唯一写内存状态的入口：页签按钮、页面容器与设置页的勾选框一起改，三者不可能不同步。 */
function applyHiddenPages(hidden){
  _hiddenPages = normaliseHiddenPages(hidden);
  navPageRows().forEach(function(row){
    const off = pageHidden(row.key);
    // 用 hidden 属性而不是内联 display：「智能体配置」不可用时也是自己改 display
    // 收起来的，两套机制各管各的，谁都不会把对方放出来。
    row.btn.hidden = off;
    if(row.page) row.page.hidden = off;
  });
  // 当前所在页被收起来了（书签、上次停留的页签）就退到第一个还看得见的页面，
  // 免得停在一个顶部没有任何入口的页面上。「设置」固定可见，所以一定有落点。
  if(pageHidden(currentMainTab)){
    const visible = navPageRows().filter(function(row){ return !pageHidden(row.key); });
    if(visible.length) switchMainTab(visible[0].key);
  }
  renderPageVisibility();
  return _hiddenPages;
}

function pageVisibilityBox(list, key){
  const items = Array.from(list.children);
  for(let i = 0; i < items.length; i++){
    const box = items[i].firstElementChild;
    if(box && box.dataset && box.dataset.pageKey === key) return box;
  }
  return null;
}

function pageVisibilityItem(row){
  const item = document.createElement('label');
  item.style.cssText = 'display:flex;align-items:center;gap:6px;font-size:13px;min-height:32px;cursor:pointer';
  const box = document.createElement('input');
  box.type = 'checkbox';
  box.style.cssText = 'width:16px;height:16px';
  box.dataset.pageKey = row.key;
  box.disabled = (row.key === PAGE_KEEP_VISIBLE);
  if(box.disabled) item.title = '「设置」是页面显示开关自己的入口，收起来就放不回去了，固定保留。';
  box.addEventListener('change', function(){ togglePageVisibility(row.key); });
  const text = document.createElement('span');
  text.textContent = navPageLabel(row);
  item.appendChild(box);
  item.appendChild(text);
  return item;
}

/* 重建只在清单变了的时候做：其余情况只同步勾选状态，免得每次 loadSettings 都把
   焦点和滚动位置抖掉。指纹的写法与侧栏导航的 pageNavSignature 一致。 */
function renderPageVisibility(){
  const list = document.getElementById('setPagesList');
  if(!list) return;
  const rows = navPageRows();
  const stamp = rows.map(function(row){ return row.key + '\u0001' + navPageLabel(row); }).join('\u0002');
  if(list.dataset.pagesStamp !== stamp){
    while(list.firstChild) list.removeChild(list.firstChild);
    rows.forEach(function(row){ list.appendChild(pageVisibilityItem(row)); });
    list.dataset.pagesStamp = stamp;
  }
  rows.forEach(function(row){
    const box = pageVisibilityBox(list, row.key);
    if(box) box.checked = !pageHidden(row.key);
  });
  const state = document.getElementById('setPagesState');
  if(state){
    const shown = rows.length - _hiddenPages.length;
    state.textContent = _hiddenPages.length ? ('(显示 ' + shown + '/' + rows.length + ')') : '(全部显示)';
    state.style.color = _hiddenPages.length ? 'var(--warn)' : 'var(--dim)';
  }
}

/* 显式选择走既有的 settings save 通道（一个键的 patch，不动别的设置）。 */
async function togglePageVisibility(key){
  const before = _hiddenPages.slice();
  const next = before.filter(function(k){ return k !== key; });
  if(before.indexOf(key) < 0) next.push(key);
  applyHiddenPages(next);
  try {
    const res = await postJSON('/settings/save', {hidden_pages: next});
    // 服务端会丢掉不合法的 key，以它存的为准，免得两边记的不是一回事
    if(res && Array.isArray(res.hidden_pages)) applyHiddenPages(res.hidden_pages);
  } catch(e) {
    applyHiddenPages(before);
    toast('页面显示设置保存失败: ' + e.message, 'bad');
  }
}
// ==== 顶部页签显示结束 ====

// ==== 账号页签模式开始 ====
// 「账号」是独立成一个主菜单，还是留在「网关与账号」页，由服务端设置
// accounts_separate_tab 决定（默认关闭 = 合并，与拆分前一致）。
//
// 三个账号区块始终是同一批 DOM 节点，只是挂在不同页下面：合并时挂到
// #pageGateway 的锚点处（控制栏之后、最近请求之前，也就是拆分前的位置），
// 拆分时挂回 #pageAccounts 的页头之后。挂在谁下面就归谁的侧栏——区块侧栏是
// initPageNav() 现场扫描本页区块生成的，所以搬完不用另外维护导航。
let _accountsSeparateTab = false;
let _accountsSections = null;

function accountsPageSections(){
  // 区块可能已经被 initPageNav() 包进 .page-nav-body（页面内容整体包一层，
  // grid 里才只剩「侧栏 + 内容」两项），所以页面 div 与那层包裹都要找一遍。
  // 只在拿到非空结果时才记住：搬走之后账号页里就只剩页头了，那时再找也找不回来。
  if(_accountsSections && _accountsSections.length) return _accountsSections;
  const page = document.getElementById('pageAccounts');
  if(!page) return [];
  const scopes = [page];
  Array.from(page.children).forEach(el => {
    if(el.classList && el.classList.contains('page-nav-body')) scopes.push(el);
  });
  const found = [];
  scopes.forEach(scope => Array.from(scope.children).forEach(el => {
    if(el.tagName === 'SECTION' && found.indexOf(el) === -1) found.push(el);
  }));
  if(found.length) _accountsSections = found;
  return found;
}

function accountsSeparateTab(){
  return _accountsSeparateTab;
}

// 插到锚点前面——插在锚点**所在的那个容器**里，而不是插到页面 div 上：
// 锚点被 .page-nav-body 包起来之后就不再是页面 div 的直接子节点了，直接对页面
// div 调 insertBefore 会抛 NotFoundError（而 loadSettings 的 try/catch 会把它
// 吞掉，表现成「开关没反应」这种最难查的样子）。
function insertBeforeAnchor(sections, anchor){
  const host = anchor && anchor.parentNode;
  if(!host || !sections.length) return false;
  sections.forEach(s => host.insertBefore(s, anchor));
  return true;
}

function applyAccountsTabLayout(separate){
  _accountsSeparateTab = (separate === true);
  const pageGateway = document.getElementById('pageGateway');
  const pageAccounts = document.getElementById('pageAccounts');
  if(!pageGateway || !pageAccounts) return _accountsSeparateTab;

  const sections = accountsPageSections();
  // 版本切换跟着账号区块走：拆分时回到账号页页头右侧，合并时搬到网关页的账号
  // 区块正上方。合并模式下网关页最上面那两张出口卡片也能切版本，但账号区在页面
  // 中段，要换一版得先滚回页首——跟着搬一次就不必了。
  // 先摆它再摆区块：两者都是「插到锚点前面」，谁先插谁在前，顺序才是控件 → 区块。
  const realmSwitch = document.getElementById('accountsRealmSwitch');
  if(realmSwitch){
    if(_accountsSeparateTab){
      const home = document.getElementById('accountsHeader');
      if(home && realmSwitch.parentNode !== home) home.appendChild(realmSwitch);
    } else {
      insertBeforeAnchor([realmSwitch], document.getElementById('accountsSectionsAnchor'));
    }
  }

  if(_accountsSeparateTab){
    // 两个页签：区块回账号页的页头之后，按原顺序。
    insertBeforeAnchor(sections, document.getElementById('accountsHomeAnchor'));
  } else {
    // 一个页签：区块挂到网关页的锚点处，位置与拆分前一致。
    insertBeforeAnchor(sections, document.getElementById('accountsSectionsAnchor'));
  }

  const btnAccounts = document.getElementById('btnNavAccounts');
  if(btnAccounts) btnAccounts.style.display = _accountsSeparateTab ? '' : 'none';
  // 名字跟着走：账号已经不在这一页了，就不能再叫「网关与账号」。
  const gatewayLabel = document.getElementById('navGatewayLabel');
  if(gatewayLabel) gatewayLabel.textContent = _accountsSeparateTab ? '网关' : '网关与账号';
  // 页头（标题 + 一行说明）跟着走：账号在不在这一页，名字与说明都得对得上。
  const pageTitle = document.getElementById('gatewayPageTitle');
  if(pageTitle) pageTitle.textContent = _accountsSeparateTab ? '网关' : '网关与账号';
  const pageDesc = document.getElementById('gatewayPageDesc');
  if(pageDesc){
    pageDesc.textContent = _accountsSeparateTab
      ? '管理代理出口与网关模型，查看当日调用量、估算花费、成功率与最近请求。'
      : '管理账号池与代理出口，查看当日调用量、估算花费、成功率、最近请求与网关模型清单。';
  }
  return _accountsSeparateTab;
}

// 显式选择走既有的 settings save 通道（一个键的 patch，不动别的设置）。
async function saveAccountsSeparateTab(btn){
  const el = document.getElementById('setAccountsSeparate');
  const value = !!(el && el.checked);
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {accounts_separate_tab: value});
    applyAccountsTabLayout(value);
    // 布局换了，导航、侧栏与当前页都要按新布局重算一遍；正停在「账号」页而
    // 刚关掉开关时，switchMainTab 会把这一页退回「网关与账号」。
    switchMainTab(currentMainTab);
    toast(value ? '已开启：账号独立成一个页签' : '已关闭：账号与网关回到同一页');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}
// ==== 账号页签模式结束 ====

// ==== 面板窗口限高开始 ====
// 每个窗口（区块 + 日志终端）超过限高时在窗口内部滚动、标题行吸顶，整页不再被
// 单个窗口拉长。限高存在服务端（accounts/settings.json 的 section_max_height，
// 0 = 不限），这里只负责把它落到 CSS 变量与 body 类上：CSS 那三条规则全部挂在
// body.win-capped 之下，没挂类时版式与从前逐像素一致。loadSettings 每次都按服务端
// 存的值重放一遍，所以多个标签页之间不会各说各话。
function applyWinMaxHeight(px){
  const value = Number(px);
  const on = Number.isFinite(value) && value > 0;
  const root = document.documentElement;
  if(on) root.style.setProperty('--win-max-h', Math.round(value) + 'px');
  else root.style.removeProperty('--win-max-h');
  document.body.classList.toggle('win-capped', on);
  const state = document.getElementById('setWinMaxHeightState');
  if(state) state.textContent = on ? ('(当前 ' + Math.round(value) + 'px)') : '(不限)';
  return on;
}

async function saveWinMaxHeight(btn){
  const el = document.getElementById('setWinMaxHeight');
  // 空值必须显式拦下：Number('') 是 0，放过去就是「清空输入框」静默把限高关掉。
  const raw = el ? String(el.value) : '';
  const value = Number(raw);
  if(raw.trim() === '' || !Number.isFinite(value) || value < 0){
    toast('请填写 0 或正数（像素）', 'warn'); return;
  }
  if(btn) btn.disabled = true;
  try {
    const res = await postJSON('/settings/save', {section_max_height: Math.round(value)});
    // 回包里的才是服务端最终存下的（超上限会被夹到 4000），按它重放。
    const stored = res && res.section_max_height !== undefined
      ? Number(res.section_max_height) : Math.round(value);
    applyWinMaxHeight(stored);
    toast(stored > 0 ? ('已限高：窗口最高 ' + stored + 'px') : '已取消窗口限高（不限）', 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}
// ==== 面板窗口限高结束 ====
