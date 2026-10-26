/* ------------------------------- dashboard ------------------------------ */
let REFRESH_RUNNING = false;
let REFRESH_QUEUED = false;
let REFRESH_GEN = 0;
async function refresh(){
  // 5 秒轮询。撞上正在飞的那一轮时**排队补跑**，而不是直接丢掉：早先的实现是
  // `if(REFRESH_BUSY) return;`，切区域/切账号那一下如果正好撞上轮询在飞，这次
  // 刷新就没了下文，只能干等下一个 5 秒周期——"点了要过几秒才变"就是这么来的。
  REFRESH_GEN++;
  if(REFRESH_RUNNING){ REFRESH_QUEUED = true; return; }
  REFRESH_RUNNING = true;
  const gen = REFRESH_GEN;
  try { await refreshInner(gen); }
  finally {
    REFRESH_RUNNING = false;
    if(REFRESH_QUEUED){ REFRESH_QUEUED = false; refresh(); }
  }
}
// 展示货币切换后，把带费用的视图就地重画：各接口有服务端 TTL 缓存，
// 重新拉取的代价很小，不必为每处维护一份数据副本。
function refreshAllCostViews(){
  refresh();
  if(currentMainTab === 'analytics'){
    loadAnalytics();
    loadAnalyticsMatrix();
  }
}
async function refreshInner(gen){
  let usage, recent, perf;
  try{
    [usage, recent, perf] = await Promise.all([
      getJSON('/usage?realm=' + encodeURIComponent(window.VIEW_REALM), POLL_READ),
      // /usage/recent 不在这里改成可校验：它没有服务端缓存，每次新请求都进榜，
      // 响应体每轮都在变，服务端也就没有稳定的校验符可发——照旧整份重取。
      getJSON('/usage/recent?limit=' + RECENT_LIMIT + '&page=' + RECENT_PAGE + '&realm=' + encodeURIComponent(window.VIEW_REALM)),
      getJSON('/usage/perf?realm=' + encodeURIComponent(window.VIEW_REALM), POLL_READ),
    ]);
    // 取数期间又发起了更新的一轮（例如刚切了区域），这批结果已经过期，
    // 直接扔掉，交给排队的那一轮渲染，免得把旧区域的数字画上去。
    if(gen !== REFRESH_GEN) return;
    document.getElementById('statusDot').className = 'dot';
  }catch(e){
    if(gen === REFRESH_GEN){
      document.getElementById('statusDot').className = 'dot err';
      if(String(e.message) !== 'unauthorized'){
        document.getElementById('meta').innerHTML = '<b>无法连接反代</b>';
      }
    }
    return;
  }

  const t = usage;
  const metaEl = document.getElementById('meta');
  if(metaEl) metaEl.innerHTML = '';
  document.getElementById('logfile').textContent = t.log_file || '';
  const rEl = document.getElementById('realm'); if(rEl) rEl.textContent = t.realm ? ' · ' + t.realm : '';

  // 分别统计国际版与国内版账号与积分
  const accts = window.ACCOUNTS || [];
  const intlAccts = accts.filter(a => a.realm === 'intl');
  const cnAccts = accts.filter(a => a.realm === 'cn');

  const isAcctActive = a => a.enabled && !a.reserveBlocked && !a.dailyLimitBlocked && !a.creditLimitReached;
  const intlActive = intlAccts.filter(isAcctActive).length;
  const cnActive = cnAccts.filter(isAcctActive).length;

  let intlRemain = 0, cnRemain = 0;
  intlAccts.forEach(a => { if(a.credits) intlRemain += (a.credits.remain || 0); });
  cnAccts.forEach(a => { if(a.credits) cnRemain += (a.credits.remain || 0); });

  const poolHtml =
    '<div class="v-split">' +
      '<div class="v-row"><span class="v-label">国际</span><span class="v-num" style="color:var(--accent)">' + intlActive + '<span class="v-unit">/' + intlAccts.length + '</span></span></div>' +
      '<div class="v-row"><span class="v-label">国内</span><span class="v-num" style="color:var(--accent)">' + cnActive + '<span class="v-unit">/' + cnAccts.length + '</span></span></div>' +
    '</div>';

  const credHtml =
    '<div class="v-split">' +
      '<div class="v-row"><span class="v-label">国际</span><span class="v-num" style="color:var(--accent2)">' + fmt(intlRemain) + '</span></div>' +
      '<div class="v-row"><span class="v-label">国内</span><span class="v-num" style="color:var(--accent2)">' + fmt(cnRemain) + '</span></div>' +
    '</div>';

  const succStr = (perf.success_rate_pct != null ? pct(perf.success_rate_pct) : '100.0%');
  if(t.usd_cny) costUsdRate = Number(t.usd_cny) || costUsdRate;

  // 这三张卡的数字都按当前视图区域取（/usage 等接口带 realm），标题带上区域，
  // 免得把国际版的用量看成全局的。
  const realmTag = '（' + (window.VIEW_REALM === 'cn' ? '国内版' : '国际版') + '）';
  document.getElementById('cards').innerHTML =
    card('网关调用量' + realmTag, fmt(t.requests) + ' <span style="font-size:13px;font-weight:500;color:var(--dim);margin-left:4px">次</span>', '', '') +
    card('积分消耗' + realmTag, (t.credit != null ? Number(t.credit).toFixed(2) : '0.00') + ' <span style="font-size:13px;font-weight:500;color:var(--dim);margin-left:4px">积分</span>', ((PRICING_ON && t.cost_cny != null) ? ('估算价值: ' + fmtCostBoth(t.cost_cny, 12)) : ''), 'warn') +
    card('请求成功率' + realmTag, '<span style="color:' + (perf.errors ? 'var(--warn)' : 'var(--accent2)') + '">' + succStr + '</span>', '', '');

  const total = recent.total || 0;
  RECENT_PAGE = recent.page || 1;
  RECENT_TOTAL_PAGES = recent.total_pages || Math.max(1, Math.ceil(total / RECENT_LIMIT));

  const pageInfo = document.getElementById('recentPageInfo');
  if(pageInfo){
    pageInfo.textContent = '第 ' + RECENT_PAGE + ' / ' + RECENT_TOTAL_PAGES + ' 页 · 共 ' + fmt(total) + ' 条记录';
  }
  const btnPrev = document.getElementById('btnRecentPrev');
  if(btnPrev) btnPrev.disabled = (RECENT_PAGE <= 1);
  const btnNext = document.getElementById('btnRecentNext');
  if(btnNext) btnNext.disabled = (RECENT_PAGE >= RECENT_TOTAL_PAGES);
  const btnBox = document.getElementById('recentPageButtons');
  if(btnBox){
    const pages = generatePageList(RECENT_PAGE, RECENT_TOTAL_PAGES);
    btnBox.innerHTML = pages.map(p => {
      if(p === '...'){
        return '<span style="color:var(--dim);padding:0 3px">…</span>';
      }
      const active = (p === RECENT_PAGE);
      return '<button class="range-btn' + (active ? ' active' : '') + '" style="min-width:26px;padding:3px 6px;font-size:11px" onclick="gotoRecentPage(' + p + ')">' + p + '</button>';
    }).join('');
  }

  const rows = recent.rows || [];
  // Terminal state of a request. Rows written before the outcome field only
  // carry error/status, so fall back to that; a client cancellation is shown
  // apart from a real failure because the gateway did nothing wrong.
  const outcomeBadge = r => {
    const o = r.outcome || (r.error ? 'failed' : 'completed');
    if(o === 'completed') return '<span class="badge ok">完成</span>';
    if(o === 'client_aborted') return '<span class="badge off">客户端取消</span>';
    if(o === 'upstream_aborted') return '<span class="badge warn">上游中断</span>';
    return '<span class="badge bad">失败</span>';
  };
  const countEl = document.getElementById('recentCount');
  if(countEl) countEl.textContent = '(第 ' + RECENT_PAGE + ' 页 / 共 ' + fmt(total) + ' 条)';
  // 推理强度：这次请求实际跑在哪个档位（网关写入 usage 行时的 reasoning_effort）。
  // v1.6.16 拆分为独立「推理强度」列展示。
  // 账号列显示昵称：uid -> 昵称取自 /usage 的 accounts_map（当前账号池）。
  // 已删除/不存在的账号回退到 uid 前 8 位，避免留空。
  const acctName = (uid) => {
    if(!uid) return '—';
    const m = (t.accounts_map && t.accounts_map[uid]);
    return (m && m.nickname) ? m.nickname : String(uid).slice(0,8);
  };
  // 5 秒一轮的重画里，内容没变就不碰 DOM：实测 100 行时一次 innerHTML 重写要花
  // ~10ms 解析 + 50~65ms 强制布局（每个 tick 一个 57~77ms 的长任务），而相邻两轮
  // 里有 1/4~1/2 的轮次数据逐字节相同——代价全白付。指纹覆盖所有会画进表格的
  // 输入：行数据本身（JSON 快照，任何字段变化都算）、页码/每页条数/区域、账号
  // 昵称（来自 /usage 的 accounts_map，与行分开变化）、以及价格列的三个开关
  // （PRICING_ON、CNY/USD 显示币种、汇率——它们和行数据分开变化，漏一个就会
  // 让"切换币种"这类操作被指纹挡住、表格停在旧币种上）。相同就整段跳过：
  // 不重拼 HTML、不重排、不重挂悬停明细。
  const recentFp = [
    total, RECENT_PAGE, RECENT_LIMIT, window.VIEW_REALM,
    PRICING_ON ? '1' : '0', costCurrency, costUsdRate,
    rows.map(r => acctName(r.account) + '\u0000' + (r.key_name || '')).join('\u0001'),
    JSON.stringify(rows),
  ].join('\u0002');
  if(recentFp === lastRecentFp) return;
  lastRecentFp = recentFp;
  COST_TIP_ROWS.clear();
  document.getElementById('recent').innerHTML = rows.length ?
    '<table class="data-cards"><thead><tr><th>时间</th><th>模型</th><th>推理强度</th><th>账号</th><th>API Key</th><th>模式</th><th>结果</th><th>耗时</th><th>首字</th><th>速度</th><th>输入</th><th>输出</th><th>思考</th><th>缓存</th><th>总 token</th><th>积分</th><th class="pricing-col" style="color:var(--warn)">估算价格</th></tr></thead><tbody>'
    + rows.map((r, i) => {
        // 关闭时不登记悬停明细，也不挂 data-cost-key：列还在（观感不变），
        // 但没有任何可解释的价格，省掉整张表的明细构建与 hover 开销。
        const costKey = PRICING_ON ? costRowKey(r, i) : '';
        if(costKey) COST_TIP_ROWS.set(costKey, r);
        return '<tr>'
        + '<td class="mono" data-label="时间">' + esc((r.iso||'').replace('T',' ').slice(5)) + '</td>'
        + '<td class="mono" data-label="模型">' + esc(r.model) + '</td>'
        + '<td data-label="推理强度">' + (r.reasoning_effort ? ('<span class="badge-effort">' + esc(r.reasoning_effort) + '</span>') : '<span style="color:var(--dim)">—</span>') + '</td>'
        + '<td class="mono" style="font-size:11px;color:var(--dim)" data-label="账号" title="' + esc(r.account||'') + '">' + esc(acctName(r.account)) + '</td>'
        + '<td class="mono" style="font-size:11px;color:var(--dim)" data-label="API Key" title="' + esc(r.key||'') + '">' + esc(r.key_name || '—') + '</td>'
        + '<td data-label="模式"><span class="badge ' + (r.stream?'s':'') + '">' + (r.stream?'流式':'非流式') + '</span></td>'
        + '<td data-label="结果">' + outcomeBadge(r) + '</td>'
        + '<td data-label="耗时">' + ms(r.elapsed_ms) + '</td>'
        + '<td data-label="首字">' + ms(r.ttft_ms) + '</td>'
        + '<td data-label="速度">' + (r.tokens_per_sec ? r.tokens_per_sec.toFixed(1) : '—') + '</td>'
        + '<td data-label="输入">' + fmt(r.prompt_tokens) + '</td>'
        + '<td data-label="输出">' + fmt(r.completion_tokens) + '</td>'
        + '<td style="color:var(--think)" data-label="思考">' + fmt(r.reasoning_tokens) + '</td>'
        + '<td data-label="缓存">' + (r.cache_hit_pct!=null ? r.cache_hit_pct+'%' : '—') + '</td>'
        + '<td data-label="总 token"><b>' + fmt(r.total_tokens) + '</b></td>'
        + '<td data-label="积分" style="color:var(--accent2)">' + (r.credit != null ? Number(r.credit).toFixed(2) : '—') + '</td>'
        + '<td data-label="OpenRouter 价估算" class="cost-cell pricing-col" style="color:var(--warn)"'
        +   (costKey ? ' data-cost-key="' + esc(costKey) + '"' : '') + '>'
        +   fmtCost(r.cost_cny)
        +   (r.cost_backfilled ? '<span style="color:var(--dim)">*</span>' : '')
        + '</td>'
      + '</tr>';
      }).join('') + '</tbody></table>'
    : '<div class="empty">还没有请求记录</div>';
  reanchorCostTip();
}


/* 侧栏尽早建：区块是静态标记，现场生成不依赖任何接口。原来只在
   switchMainTab() 里建，而它排在启动链的末端——/panel/status、/settings、
   /realm 三趟串行往返，再加 Promise.all 里那批数据加载（含走上游的
   /v1/models 与国内版成长任务）之后才轮到，所以刷新时左侧要先空一列、
   过一会儿才补上。这里在 DOMContentLoaded 就建（head 里那段已按 ?tab=
   与上次选择定好 .active），switchMainTab() 那次会因清单未变直接返回，
   不会重建、也不会闪。 */
document.addEventListener('DOMContentLoaded', function(){
  initPageNav(document.querySelector('.main-page.active'));
});

let RECENT_LIMIT = parseInt(localStorage.getItem('WB_RECENT_LIMIT') || '20', 10);
if(![10, 20, 50, 100].includes(RECENT_LIMIT)) RECENT_LIMIT = 20;
let RECENT_PAGE = 1;
let RECENT_TOTAL_PAGES = 1;
// 最近请求表上次真正画进 DOM 的内容指纹（见 refreshInner 里的 recentFp）。
// 5 秒一轮的重画代价几乎全在布局（100 行实测 50~65ms），数据没变就不该重画。
let lastRecentFp = null;

function updateRecentLimitButtons(){
  [10, 20, 50, 100].forEach(n => {
    const btn = document.getElementById('btnRecentLimit' + n);
    if(btn) btn.classList.toggle('active', RECENT_LIMIT === n);
  });
}

function setRecentLimit(n){
  RECENT_LIMIT = n;
  RECENT_PAGE = 1;
  try { localStorage.setItem('WB_RECENT_LIMIT', String(n)); } catch(e){}
  updateRecentLimitButtons();
  refresh();
}

async function changeRecentPage(delta){
  const next = RECENT_PAGE + delta;
  if(next < 1 || next > RECENT_TOTAL_PAGES) return;
  RECENT_PAGE = next;
  await refresh();
}

async function gotoRecentPage(p){
  if(p < 1 || p > RECENT_TOTAL_PAGES || p === RECENT_PAGE) return;
  RECENT_PAGE = p;
  await refresh();
}

function generatePageList(current, total){
  if(total <= 7){
    return Array.from({length: total}, (_, i) => i + 1);
  }
  const pages = [1];
  let start = Math.max(2, current - 2);
  let end = Math.min(total - 1, current + 2);
  if(current <= 4){
    end = 5;
  }
  if(current >= total - 3){
    start = total - 4;
  }
  if(start > 2) pages.push('...');
  for(let i = start; i <= end; i++){
    pages.push(i);
  }
  if(end < total - 1) pages.push('...');
  pages.push(total);
  return pages;
}

// Tab ids in nav order. One list so the restore path and the nav cannot
// drift apart.
const MAIN_TABS = ['gateway', 'accounts', 'tasks', 'analytics', 'agents', 'settings', 'logs'];
const MAIN_TAB_STORE = 'wb-proxy-main-tab';
let currentMainTab = 'gateway';
let analyticsRange = 'today';
let cachedMatrixData = null;
let matrixAcctFilter = '';
let matrixModelFilter = '';
// Model allowlist for the combined matrix; filled by loadAnalyticsMatrix().
let MATRIX_MODELS = new Set();
