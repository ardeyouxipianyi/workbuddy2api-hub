/* ---- 系统运行日志模块 ---- */
let logEntries = [];
let lastLogId = 0;
let logFilterLevel = '';
let logFilterTag = '';
let logSearchText = '';
let logAutoRefresh = true;
let logAutoScroll = true;
let logPollTimer = null;
let logFetching = false;
let logUserScrolledUp = false;
// 增量渲染的簿记：DOM 里此刻画着的是"哪个过滤条件下的多少行"。轮询带回新行时
// 只 append 新增的几行；过滤/搜索变化或状态对不上时才整表重画。
let logDomKey = null;     // 上次渲染时的过滤条件指纹（logFilterKey()）
let logDomCount = 0;      // DOM 中当前的 .log-line 行数
const LOG_DOM_MAX = 2000; // 与 logEntries 的缓冲上限一致，超出就从头部裁

// 过滤谓词抽出来共用：整表渲染与增量 append 必须用同一套判据，否则会出现
// "新行画上去了、但按当前过滤条件它本不该显示"这类两套逻辑漂移出来的脏数据。
function logMatchesFilter(item){
  if(logFilterLevel && item.level !== logFilterLevel) return false;
  if(logFilterTag && item.tag !== logFilterTag) return false;
  if(logSearchText){
    const haystack = ((item.msg || '') + ' ' + (item.tag || '') + ' ' + (item.time || '')).toLowerCase();
    if(!haystack.includes(logSearchText)) return false;
  }
  return true;
}
function logFilterKey(){ return logFilterLevel + '|' + logFilterTag + '|' + logSearchText; }
/* 顶部文案跟着模式走：落库时报告库里累计多少条历史、并指向设置页的上限；纯内存
   时保持上游原话。data.mode 由 /logs 带回（后端按当前开关判定）。 */
function updateLogPageHint(data){
  const hint = document.getElementById('logPageHint');
  if(!hint) return;
  if(data && data.mode === 'db'){
    const stored = (typeof data.stored === 'number') ? data.stored : null;
    hint.textContent = '实时捕获反向代理请求、流式转发、调度巡检、任务打卡与异常告警'
      + ' (已落库' + (stored === null ? '' : '，共 ' + stored + ' 条历史')
      + '，可在设置页调整保留上限)';
  } else {
    hint.textContent = '实时捕获反向代理请求、流式转发、调度巡检、任务打卡与异常告警 (内存保留最新 2000 条)';
  }
}
// 单行 HTML 只有这一处定义，整表渲染和增量 append 都用它。
function logLineHtml(item){
  const lvlCls = 'lvl-' + (item.level || 'INFO');
  const tagCls = 'tag-' + (item.tag || 'system');
  return '<div class="log-line">' +
    '<span class="log-ts">' + escapeHtml(item.time || '') + '</span>' +
    '<span class="log-lvl ' + lvlCls + '">' + escapeHtml(item.level || 'INFO') + '</span>' +
    '<span class="log-tag ' + tagCls + '">[' + escapeHtml(item.tag || 'system') + ']</span>' +
    '<span class="log-msg">' + escapeHtml(item.msg || '') + '</span>' +
  '</div>';
}

function startLogPolling(){
  if(logPollTimer) clearInterval(logPollTimer);
  logPollTimer = setInterval(() => {
    if(logAutoRefresh && currentMainTab === 'logs' && !document.hidden){
      fetchNewLogs(false);
    }
  }, 2000);
}

function stopLogPolling(){
  if(logPollTimer){
    clearInterval(logPollTimer);
    logPollTimer = null;
  }
}

async function fetchNewLogs(forceReset){
  if(logFetching) return;
  logFetching = true;
  const statusEl = document.getElementById('logTerminalStatus');
  if(statusEl && forceReset) statusEl.textContent = '正在拉取...';
  try {
    const since = forceReset ? 0 : lastLogId;
    const limit = forceReset ? 500 : 200;
    const data = await getJSON('/logs?since_id=' + since + '&limit=' + limit);
    if(data && data.logs){
      updateLogPageHint(data);
      let fresh = null;     // 本轮真正新增的条目（去重后）
      let dropped = null;   // 因超上限被挤出缓冲的最旧条目
      if(forceReset){
        logEntries = data.logs;
      } else if(data.logs.length > 0){
        const existingIds = new Set(logEntries.map(x => x.id));
        fresh = data.logs.filter(item => !existingIds.has(item.id));
        fresh.forEach(item => logEntries.push(item));
        if(logEntries.length > 2000){
          dropped = logEntries.slice(0, logEntries.length - 2000);
          logEntries = logEntries.slice(-2000);
        }
      }
      if(data.max_id){
        lastLogId = Math.max(lastLogId, data.max_id);
      } else if(logEntries.length > 0){
        lastLogId = logEntries[logEntries.length - 1].id;
      }
      // 渲染策略：只有真的带着新行回来才碰 DOM。
      //  - 首次进入 / 手动刷新（forceReset）、过滤条件与上次渲染不一致、或 DOM
      //    里现在是空态：整表重画，顺带把簿记校正回来；
      //  - 有新增行且条件一致：只把新增的几行 append 上去，被挤出的旧行从头部
      //    等量删掉；
      //  - 没有任何新行：直接跳过——旧实现里 `data.logs` 为空数组也真，于是每
      //    2 秒把整个终端重写一遍，2000 行时每次约 700KB 的 HTML 解析 + 布局
      //    （实测 layout 254~324ms）全白烧，忙碌时"每 2 秒卡 0.3 秒"就是它。
      if(forceReset || logDomKey !== logFilterKey() || logDomCount === 0){
        renderLogs();
      } else {
        if(dropped && dropped.length) dropLogDomHead(dropped);
        if(fresh && fresh.length) appendLogLines(fresh);
      }
      // 统计徽章的数字只由 logEntries 决定，缓冲没变就不用重写（少一次 DOM 写）。
      if(forceReset || (fresh && fresh.length)) updateLogStats();
    }
    if(statusEl) statusEl.textContent = '就绪 · ' + new Date().toLocaleTimeString();
  } catch(e) {
    if(statusEl) statusEl.textContent = '连接中断: ' + e.message;
  } finally {
    logFetching = false;
  }
}

function updateLogStats(){
  const badge = document.getElementById('logStatsBadge');
  if(!badge) return;
  let errors = 0, warns = 0;
  logEntries.forEach(x => {
    if(x.level === 'ERROR') errors++;
    else if(x.level === 'WARN') warns++;
  });
  let txt = '共 ' + logEntries.length + ' 条';
  if(errors > 0) txt += ' · <span style="color:var(--bad)">' + errors + ' 错误</span>';
  if(warns > 0) txt += ' · <span style="color:var(--warn)">' + warns + ' 警告</span>';
  badge.innerHTML = txt;
}

function setLogFilterLevel(lvl){
  logFilterLevel = lvl;
  ['All', 'Info', 'Warn', 'Error'].forEach(name => {
    const btn = document.getElementById('btnLogLvl' + name);
    if(btn){
      const match = (lvl === '' && name === 'All') || (lvl.toUpperCase() === name.toUpperCase());
      btn.classList.toggle('active', match);
    }
  });
  renderLogs();
}

/* 模块筛选按钮的固定顺序与中文名。顺序按「排查时最常先看哪个模块」排，未列出
   的 tag 排到最后并按字母序补上——后端新增 tag 时这里不改也能用。 */
const LOG_TAG_ORDER = ['chat', 'tasks', 'scheduler', 'accounts', 'agents', 'pricing',
                       'egress', 'upstream', 'webtools', 'catalog', 'auth', 'settings',
                       'update', 'usage', 'system'];
const LOG_TAG_LABELS = {
  chat: '对话', tasks: '任务福利', scheduler: '调度器', accounts: '账号',
  agents: '智能体', pricing: '定价', egress: '出口', upstream: '上游',
  webtools: '网络工具', catalog: '模型', auth: '鉴权', settings: '设置',
  update: '更新', usage: '用量', system: '系统'
};
let logTagBtnKey = null;
/* 按当前缓冲里真正出现过的 tag 重建按钮组。集合没变就不动 DOM——这个函数每
   次渲染都会调，重建本身会让正在点的按钮闪一下。 */
function renderLogTagFilters(){
  const bar = document.getElementById('logTagBar');
  if(!bar) return;
  const seen = new Set();
  logEntries.forEach(x => { if(x.tag) seen.add(x.tag); });
  // 当前筛选的 tag 即使暂时没有日志也留在栏里，否则按钮一消失就不知道自己在
  // 筛什么，也没法一眼切回去。
  if(logFilterTag) seen.add(logFilterTag);
  const ordered = LOG_TAG_ORDER.filter(t => seen.has(t))
    .concat(Array.from(seen).filter(t => LOG_TAG_ORDER.indexOf(t) < 0).sort());
  const key = ordered.join(',');
  if(key !== logTagBtnKey){
    logTagBtnKey = key;
    bar.innerHTML = '';
    [{tag: '', label: '全部模块'}]
      .concat(ordered.map(t => ({tag: t, label: LOG_TAG_LABELS[t] || t})))
      .forEach(item => {
        const btn = document.createElement('button');
        btn.className = 'range-btn';
        btn.dataset.tag = item.tag;
        btn.textContent = item.label;
        btn.onclick = () => setLogFilterTag(item.tag);
        bar.appendChild(btn);
      });
  }
  bar.querySelectorAll('button').forEach(b => {
    b.classList.toggle('active', (b.dataset.tag || '') === logFilterTag);
  });
}

function setLogFilterTag(tag){
  logFilterTag = tag;
  renderLogTagFilters();
  renderLogs();
}

let logSearchDebounce = null;
function onLogSearchInput(){
  clearTimeout(logSearchDebounce);
  logSearchDebounce = setTimeout(() => {
    const input = document.getElementById('logSearchInput');
    logSearchText = input ? input.value.trim().toLowerCase() : '';
    renderLogs();
  }, 200);
}

function toggleLogAuto(){
  logAutoRefresh = !logAutoRefresh;
  const btn = document.getElementById('btnToggleLogAuto');
  if(btn){
    if(logAutoRefresh){
      btn.textContent = '实时监听中';
      btn.style.color = 'var(--accent2)';
      startLogPolling();
    } else {
      btn.textContent = '已暂停监听';
      btn.style.color = 'var(--dim)';
      stopLogPolling();
    }
  }
}

function toggleLogScroll(){
  logAutoScroll = !logAutoScroll;
  const btn = document.getElementById('btnToggleLogScroll');
  if(btn){
    btn.textContent = logAutoScroll ? '自动滚屏: 开' : '自动滚屏: 关';
  }
  if(logAutoScroll){
    const body = document.getElementById('logTerminalBody');
    if(body) body.scrollTop = body.scrollHeight;
  }
}

function handleLogTerminalScroll(){
  const body = document.getElementById('logTerminalBody');
  if(!body) return;
  const atBottom = (body.scrollHeight - body.scrollTop - body.clientHeight) < 40;
  if(!atBottom && logAutoScroll){
    logUserScrolledUp = true;
  } else if(atBottom && logUserScrolledUp){
    logUserScrolledUp = false;
  }
}

function escapeHtml(s){
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function renderLogs(){
  const body = document.getElementById('logTerminalBody');
  if(!body) return;

  renderLogTagFilters();
  const filtered = logEntries.filter(logMatchesFilter);

  if(filtered.length === 0){
    body.innerHTML = '<div class="log-empty">暂无匹配的日志记录</div>';
    logDomKey = logFilterKey();
    logDomCount = 0;
    return;
  }

  body.innerHTML = filtered.map(logLineHtml).join('');
  logDomKey = logFilterKey();
  logDomCount = filtered.length;

  if(logAutoScroll && !logUserScrolledUp){
    body.scrollTop = body.scrollHeight;
  }
}

// 只把新到的行追加到终端末尾（insertAdjacentHTML 只解析这一段，不碰已有行）。
// 旧实现每次轮询都把全部行重拼一遍再整体 innerHTML，2000 行时约 700KB/次；
// 忙碌时新行通常只有几行，append 的代价与新增量成正比。
function appendLogLines(items){
  const body = document.getElementById('logTerminalBody');
  if(!body) return;
  const visible = items.filter(logMatchesFilter);
  if(!visible.length) return;   // 新行全被过滤掉：DOM 一个字节都不动
  body.insertAdjacentHTML('beforeend', visible.map(logLineHtml).join(''));
  logDomCount += visible.length;
  // 兜底上限：正常情况下头部裁剪由 dropLogDomHead 按"被挤出的条目"精确完成，
  // 这里只防簿记与 DOM 万一脱节（比如有人在控制台里手动改过 DOM）。
  while(logDomCount > LOG_DOM_MAX){
    const first = body.firstElementChild;
    if(!first){ logDomCount = 0; break; }
    body.removeChild(first);
    logDomCount--;
  }
  if(logAutoScroll && !logUserScrolledUp){
    body.scrollTop = body.scrollHeight;
  }
}

// 缓冲超上限时，最旧的条目被挤出 logEntries；DOM 头部也要把"其中当前过滤条件
// 下曾经画出来的那些行"删掉，否则开着过滤时终端会攒下已经不在缓冲里的旧行，
// 和切一次过滤后的整表重画结果对不上。按条数从头部删即可：DOM 顺序与条目顺序
// 一致，被挤出的条目必然是最老的，也就是 DOM 头部的那几行。
function dropLogDomHead(droppedEntries){
  const body = document.getElementById('logTerminalBody');
  if(!body) return;
  let cut = 0;
  for(let i = 0; i < droppedEntries.length; i++){
    if(logMatchesFilter(droppedEntries[i])) cut++;
  }
  while(cut > 0){
    const first = body.firstElementChild;
    if(!first){ logDomCount = 0; return; }
    body.removeChild(first);
    logDomCount--;
    cut--;
  }
}

function copyFilteredLogs(){
  const filtered = logEntries.filter(item => {
    if(logFilterLevel && item.level !== logFilterLevel) return false;
    if(logFilterTag && item.tag !== logFilterTag) return false;
    if(logSearchText){
      const haystack = ((item.msg || '') + ' ' + (item.tag || '') + ' ' + (item.time || '')).toLowerCase();
      if(!haystack.includes(logSearchText)) return false;
    }
    return true;
  });
  const text = filtered.map(x => '[' + (x.ts || x.time) + '] [' + x.level + '] [' + x.tag + '] ' + x.msg).join('\n');
  // Same secure-context caveat as writeClipboard(): plain http on a LAN address
  // has no navigator.clipboard at all, so reuse the shared helper.
  writeClipboard(text).then(() => {
    toast('已复制 ' + filtered.length + ' 行日志', 'ok');
  }).catch(e => {
    toast('复制失败: ' + e.message, 'bad');
  });
}

async function exportLogsFile(){
  try {
    const data = await getJSON('/logs?limit=5000');
    if(!data || !data.logs || !data.logs.length){
      toast('暂无日志可导出', 'warn');
      return;
    }
    const lines = data.logs.map(x => '[' + (x.ts || x.time) + '] [' + x.level + '] [' + x.tag + '] ' + x.msg);
    const text = lines.join('\n');
    const blob = new Blob([text], {type: 'text/plain;charset=utf-8'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    a.download = 'wb-proxy-' + ts + '.log';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    toast('已导出 ' + data.logs.length + ' 行日志', 'ok');
  } catch(e) {
    toast('导出日志失败: ' + e.message, 'bad');
  }
}

async function clearServerLogs(){
  if(!confirm('确定要清空网关日志吗？内存缓冲会被清空；若已启用日志落库，数据库里的历史记录也会一并删除。')) return;
  try {
    await postJSON('/logs/clear', {});
    logEntries = [];
    lastLogId = 0;
    renderLogs();
    updateLogStats();
    toast('网关日志已清空', 'ok');
  } catch(e) {
    toast('清空失败: ' + e.message, 'bad');
  }
}
