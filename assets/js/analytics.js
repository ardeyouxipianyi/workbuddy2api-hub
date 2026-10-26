/* ============================================================
   数据看板页（B 版重设计，2026-10-14 集成）
   原独立原型：proto/variant-b.html —— 已按面板机制适配：
   鉴权走 getJSON / 主题跟随 data-theme / 悬浮提示走全局 data-tip /
   刷新入口重接线到 loadAnalytics。
   ============================================================ */
(function(){

/* ============================================================
   基础工具
   ============================================================ */
const $ = id => document.getElementById(id);

function fmtCompact(n, digits){
  n = Number(n) || 0;
  const a = Math.abs(n);
  if (a < 1e4) return n.toLocaleString('zh-CN', {maximumFractionDigits: digits ?? 0});
  if (a < 1e6) return trim0((n/1e3).toFixed(a < 1e5 ? 1 : 0)) + 'K';
  if (a < 1e9) return trim0((n/1e6).toFixed(a < 1e7 ? 2 : 1)) + 'M';
  return trim0((n/1e9).toFixed(2)) + 'B';
}
function trim0(s){ return s.replace(/\.?0+$/, ''); }
function fullNum(v, digits){
  if (v == null || !isFinite(v)) return '—';
  return Number(v).toLocaleString('zh-CN', {maximumFractionDigits: digits ?? 0});
}
/* 紧凑值 + 悬停显示完整精确值的 span */
function numSpan(v, digits){
  if (v == null || !isFinite(v)) return '—';
  const full = fullNum(v, digits);
  const brief = fmtCompact(v, digits);
  return brief === full ? brief : `<span data-tip="${full}">${brief}</span>`;
}
function fmtSec(ms){
  if (ms == null || !isFinite(ms)) return '—';
  const s = ms / 1000;
  return s >= 100 ? s.toFixed(0) + ' s' : s.toFixed(s >= 10 ? 1 : 2) + ' s';
}
function fmtPct(v, d){ return (v == null || !isFinite(v)) ? '—' : Number(v).toFixed(d ?? 1) + '%'; }
function fmtCostCny(v){
  if (v == null || !isFinite(v)) return '—';
  return '￥' + Number(v).toLocaleString('zh-CN', {minimumFractionDigits:2, maximumFractionDigits:2});
}
function fmtTime(epoch){
  const d = new Date(epoch * 1000);
  const p = n => String(n).padStart(2, '0');
  return `${d.getMonth()+1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function fmtDate(epoch){
  const d = new Date(epoch * 1000);
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

/* ============================================================
   数据层：直接用面板的 getJSON（自动带 X-Panel-Token、单飞合并、401 统一处理、
   POLL_READ 走 ETag 条件请求），不再自管 token 与登录。
   ============================================================ */
const api = (path, opts) => getJSON(path, opts || POLL_READ);

/* ============================================================
   状态
   ============================================================ */
const state = {
  range: 'today',
  metric: 'tokens',
  dim: 'model',
  analytics: null,
  series: null,
  perf: null,
  heat: null,
  openKey: null,
};
let prevSummary = null; /* B 版：上一等长周期汇总（环比） */

/* ---------- B 版：环比 / sparkline / 环形卡 / 活动流 ---------- */
function prevRange(untilAt){
  // 环比窗口的上界同样来自调用方：轮询时与 rangeParams 共用同一个量化时刻，
  // 否则 /usage/timeseries?…&until=… 这一条照样每轮都换 URL。
  const now = (untilAt === undefined || untilAt === null) ? Math.floor(Date.now() / 1000) : untilAt;
  if (state.range === 'today'){
    const d = new Date(); d.setHours(0,0,0,0);
    const startToday = Math.floor(d.getTime() / 1000);
    const span = now - startToday;
    return [startToday - 86400, startToday - 86400 + span];
  }
  if (state.range === 'week'){
    const dowMon = (new Date().getDay() + 6) % 7;
    const d = new Date(); d.setHours(0,0,0,0); d.setDate(d.getDate() - dowMon);
    const startWeek = Math.floor(d.getTime() / 1000);
    const span = now - startWeek;
    return [startWeek - 7 * 86400, startWeek - 7 * 86400 + span];
  }
  if (state.range === 'month'){
    const d = new Date();
    const startMonth = Math.floor(new Date(d.getFullYear(), d.getMonth(), 1, 0,0,0).getTime() / 1000);
    const span = now - startMonth;
    const startPrev = Math.floor(new Date(d.getFullYear(), d.getMonth() - 1, 1, 0,0,0).getTime() / 1000);
    return [startPrev, startPrev + span];
  }
  if (state.range === 'd3')  return [now - 6 * 86400, now - 3 * 86400];
  if (state.range === 'd30') return [now - 60 * 86400, now - 30 * 86400];
  return null;
}
async function loadPrevSummary(untilAt){
  const pr = prevRange(untilAt);
  if (!pr) return null;
  try{
    const p = await api(`/usage/timeseries?realm=all&range=custom&since=${pr[0]}&until=${pr[1]}`);
    const t = (p.series || []).reduce((a, b) => ({
      tok:    a.tok    + (b.total_tokens   || 0),
      req:    a.req    + (b.requests       || 0),
      cred:   a.cred   + (b.credit         || 0),
      cached: a.cached + (b.cached_tokens  || 0),
      prompt: a.prompt + (b.prompt_tokens  || 0),
    }), {tok:0, req:0, cred:0, cached:0, prompt:0});
    return (t.req || t.tok) ? t : null;
  }catch(e){ return null; }
}
function deltaBadge(cur, prev){
  if (prev == null || !isFinite(prev) || prev === 0 || cur == null) return '';
  const d = (cur - prev) / Math.abs(prev) * 100;
  if (!isFinite(d)) return '';
  return `${d >= 0 ? '▲' : '▼'} ${Math.abs(d).toFixed(1)}%`;
}
function setDelta(id, cur, prev){
  const el = $(id); if (!el) return;
  const txt = deltaBadge(cur, prev);
  el.textContent = txt;
  el.className = 'delta' + (txt.startsWith('▲') ? ' up' : '');
}
function drawSpark(id, vals, colorVar){
  const cv = $(id);
  if (!cv || !vals || vals.length < 2) return;
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth || 120, H = 44;
  cv.width = W * dpr; cv.height = H * dpr;
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  const color = seriesColor(colorVar);
  const max = Math.max(...vals), min = Math.min(...vals);
  const rng = (max - min) || 1;
  const step = W / (vals.length - 1);
  const X = i => i * step;
  const Y = v => H - 3 - (v - min) / rng * (H - 8);
  ctx.beginPath();
  vals.forEach((v, i) => { i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v)); });
  ctx.strokeStyle = color; ctx.lineWidth = 1.4; ctx.lineJoin = 'round'; ctx.lineCap = 'round';
  ctx.stroke();
  ctx.lineTo(W, H); ctx.lineTo(0, H); ctx.closePath();
  ctx.globalAlpha = .14; ctx.fillStyle = color; ctx.fill(); ctx.globalAlpha = 1;
}
function drawSparks(){
  const s = state.series;
  if (!s || !s.series) return;
  const step = s.bucket_seconds || 3600;
  const arr = s.series;
  drawSpark('sparkTokens', arr.map(b => b.total_tokens || 0), '--chart-bar');
  drawSpark('sparkReqs',   arr.map(b => b.requests || 0), '--chart-line');
  drawSpark('sparkSpeed',  arr.map(b => (b.completion_tokens || 0) / step), '--chart-bar');
  drawSpark('sparkCache',  arr.map(b => b.prompt_tokens ? (b.cached_tokens || 0) / b.prompt_tokens * 100 : 0), '--chart-line');
  drawSpark('sparkCost',   arr.map(b => b.credit || 0), '--chart-bar');
}
function nicknameOf(uid){
  const acc = ((state.analytics || {}).accounts || []).find(x => x.uid === uid);
  return acc ? (acc.nickname || acc.uid.slice(0, 8)) : (uid || '').slice(0, 8);
}
async function loadRecent(){
  try{
    const r = await api('/usage/recent?limit=12');
    const rows = r.rows || [];
    $('recentMeta').textContent = `共 ${fmtCompact(r.total || 0)} 条记录`;
    $('recentList').innerHTML = rows.map(x => {
      const ok = x.outcome === 'completed';
      const nm = nicknameOf(x.account);
      return `<div class="recent-item">
        <span class="t">${(x.iso || '').slice(11, 19)}</span>
        <span class="m"><b>${x.model || ''}</b> → ${nm.length > 16 ? nm.slice(0, 16) + '…' : nm}${ok ? '' : ' <span class="err">[' + (x.outcome || '失败') + ']</span>'}</span>
        <span class="r"><b>${fmtCompact(x.total_tokens || 0)}</b> tok · ${((x.elapsed_ms || 0) / 1000).toFixed(1)}s</span>
      </div>`;
    }).join('') || '<div class="empty">暂无请求记录</div>';
  }catch(e){ console.error('recent failed', e); }
}

/* 轮询类请求的窗口上界量化：服务端 _json_cached 只在「调用前后的缓存条目一模一样」
 * 时才发 ETag（wb_proxy.py:16872），而缓存键里含 until。until 每轮都取 now()，
 * 缓存键每轮都是新的 → before 永远是 None → 304 一次都命不中外加整份重传。
 * 量化到服务端聚合缓存自己的 TTL（wb_proxy.py:1778 _STATS_TTL = 15s）：同一个 15 秒
 * 桶内 URL 逐字相同，第 2、3 轮直接吃 304；代价是最多 15 秒的新鲜度延迟。
 * 只有轮询走这条（loadRange(true)），用户手点区间仍用精确 now。 */
const POLL_UNTIL_BUCKET = 15;
function pollUntil(){
  return Math.floor(Math.floor(Date.now() / 1000) / POLL_UNTIL_BUCKET) * POLL_UNTIL_BUCKET;
}
function rangeParams(range, untilAt){
  // since 必须和 until 用同一个时间点推导，否则 since 仍在逐秒变化，URL 照样每轮都新。
  const now = (untilAt === undefined || untilAt === null) ? Math.floor(Date.now() / 1000) : untilAt;
  if (range === 'd3')  return `range=custom&since=${now - 3*86400}&until=${now}`;
  if (range === 'd30') return `range=custom&since=${now - 30*86400}&until=${now}`;
  if (range === 'all') return `range=custom&since=${now - 365*86400}&until=${now}`;
  return `range=${range}`;
}

/* ============================================================
   主题：跟随面板（documentElement 的 data-theme，由顶部主题菜单控制）。
   切换时重绘画布图表，让配色即时跟上。
   ============================================================ */
// 守卫与 i18n 的观察者同款：测试的 DOM 桩里没有 MutationObserver，缺了它会
// 让整个脚本在求值时就抛 ReferenceError，其余函数一个都测不到。
if(typeof MutationObserver !== 'undefined'){
  new MutationObserver(() => {
    if (state.series) drawSeries();
    drawSparks();
    if (state.heat) buildHeatGrid(state.heat);
  }).observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme']});
}

/* ============================================================
   加载与渲染
   ============================================================ */
/* 区间/轮询的代次。与 refreshInner 里 REFRESH_GEN 同一套理由：取数期间又发起了更新
 * 的一轮（刚切了区间、或轮询撞上点击），这批结果已经过期，直接丢掉——否则慢的那一轮
 * 晚到，会把上一个区间的数字画到新区间的标题下面（实测有 1.2~1.8 秒的错位窗口）。 */
let WBA_GEN = 0;
async function loadRange(polled){
  const gen = ++WBA_GEN;
  const untilAt = polled ? pollUntil() : null;
  $('kpis').style.opacity = .55;
  const rp = rangeParams(state.range, untilAt);
  try{
    const [analytics, series, prev] = await Promise.all([
      api(`/usage/analytics?realm=all&${rp}`),
      api(`/usage/timeseries?realm=all&${rp}`),
      loadPrevSummary(untilAt),
    ]);
    if (gen !== WBA_GEN) return;
    state.analytics = analytics;
    state.series = series;
    prevSummary = prev;
  } catch(e){
    if (gen !== WBA_GEN) return;
    console.error('load range failed', e);
  }
  $('kpis').style.opacity = 1;
  renderKpis();
  renderSeriesMeta();
  drawSeries();
  drawSparks();
  renderDim();
  loadRecent();
}

async function loadStatic(){
  try{ state.perf = await api('/usage/perf'); } catch(e){ console.error(e); }
  await loadHeat();
  renderPerfFold();
  // 剩余用量 / 积分扣减 / 积分获取历史：原版区块的数据管道原来挂在旧 loadAnalytics 上，
  // 页面换成新版后这里补回调用（展开对应折叠区时还会再刷一次）。
  try{ loadRemainingUsage(); }catch(e){}
  try{ loadCreditHistory(); }catch(e){}
  try{ loadCreditGrants(); }catch(e){}
}

/* 热力图按「本地日」取数：since/until 都钉在本地午夜，缓存键一整天不变；
 * until 取到明天 0 点，今天新产生的用量也能落进今天的格子。只在进页面、
 * 跨天或距上次取数满 60 秒时重取——以前只在进页面取一次，跨过午夜后页面
 * 还停在旧的一天（issue #541）。 */
let heatFetchedAt = 0, heatFetchedDay = '', heatLoading = false;
async function loadHeat(){
  if (heatLoading) return;
  heatLoading = true;
  const nowMs = Date.now();
  const midnight = new Date(nowMs); midnight.setHours(0,0,0,0);
  const since = Math.floor(midnight.getTime()/1000) - 366*86400;
  const until = Math.floor(midnight.getTime()/1000) + 86400;
  heatFetchedAt = nowMs/1000;
  heatFetchedDay = fmtDate(nowMs/1000);
  try{
    state.heat = await api(`/usage/timeseries?realm=all&range=custom&since=${since}&until=${until}&bucket_seconds=86400`);
  } catch(e){ console.error('heat failed', e); }
  heatLoading = false;
  buildHeatGrid(state.heat);
}
function maybeLoadHeat(){
  const now = Date.now()/1000;
  if (fmtDate(now) !== heatFetchedDay || now - heatFetchedAt >= 60) loadHeat();
}

/* ---------------- KPI ---------------- */
function renderKpis(){
  const a = state.analytics;
  if (!a){ return; }
  const w = a.summary.window;

  // ① Token
  $('kTokens').innerHTML = numSpan(w.total_tokens);
  // 副行按桌面端原本的「行」分组，每项一个 span：
  //   * 桌面端 .kline 是 block、.ksep 显示为「 · 」，渲染结果与从前逐字一致；
  //   * 窄屏 .kvi 各占一行、.ksep 隐藏，于是每行只有一项（字号/颜色不变）。
  const subItems = (lines) => lines.map(line =>
    '<span class="kline">' +
    line.map(t => `<span class="kvi">${t}</span>`).join('<span class="ksep"> · </span>') +
    '</span>').join('');
  $('kTokensSub').innerHTML = subItems([
    [`输入 <b>${numSpan(w.prompt_tokens)}</b>`,
     `输出 <b>${numSpan(w.completion_tokens)}</b>`],
    [`思考 <b>${numSpan(w.reasoning_tokens)}</b>`,
     `缓存 <b>${numSpan(w.cached_tokens)}</b>`],
  ]);
  const parts = [
    ['输入', w.prompt_tokens - (w.cached_tokens||0), 'var(--accent)'],
    ['缓存', w.cached_tokens, 'var(--blue)'],
    ['输出', w.completion_tokens, 'var(--green)'],
    ['思考', w.reasoning_tokens, 'var(--gold)'],
  ];
  const tot = parts.reduce((s,x)=>s+(x[1]||0),0) || 1;
  $('kTokensFlow').innerHTML = parts.map(([n,v,c]) =>
    `<i style="width:${Math.max(1.5,(v||0)/tot*100)}%;background:${c}" title="${n} ${fmtCompact(v)}"></i>`).join('');

  // ② 请求
  const okRate = w.requests ? (w.requests - w.errors) / w.requests * 100 : 100;
  $('kReqs').innerHTML = `${numSpan(w.requests)}<small>次</small>`;
  $('kReqsSub').innerHTML = subItems([
    [`失败 <b>${numSpan(w.errors)}</b>`,
     `成功率 <span class="${okRate>=99?'ratio-ok':(okRate>=95?'ratio-gold':'ratio-bad')}"><b>${fmtPct(okRate)}</b></span>`],
  ]);

  // ⑥′ 环形成功率卡
  const ringEl = $('ringSuccess');
  if (ringEl && w.requests){
    const p = Math.max(0, Math.min(100, okRate));
    ringEl.style.setProperty('--p', p.toFixed(1));
    $('ringPct').textContent = p >= 99.95 ? '100%' : p.toFixed(1) + '%';
    $('kSuccessSub').innerHTML = `${fmtCompact(w.requests)} 次 · 失败 <b data-tip="${fullNum(w.errors)}">${fmtCompact(w.errors)}</b> 次`;
  }

  // ③ 速度（TTFT 并入副行）
  $('kSpeed').innerHTML = `${w.speed_avg != null ? Number(w.speed_avg).toFixed(1) : '—'}<small>tok/s</small>`;
  const peak = peakThroughput();
  const speedLines = [[
    `平均耗时 <b>${fmtSec(w.elapsed_ms_avg)}</b>`,
    `首字 <b>${fmtSec(w.ttft_ms_avg)}</b>`,
  ]];
  const speedTail = [];
  if(peak != null) speedTail.push(`峰值吞吐 <b>${fmtCompact(peak, 0)} tok/s</b>`);
  if(state.perf && state.perf.ttft_ms) speedTail.push(`p90 首字 <b>${fmtSec(state.perf.ttft_ms.p90)}</b>`);
  if(speedTail.length) speedLines.push(speedTail);
  $('kSpeedSub').innerHTML = subItems(speedLines);

  // ⑤ 缓存
  $('kCache').innerHTML = fmtPct(w.cache_hit_pct);
  $('kCacheSub').innerHTML = `缓存复用 <b>${numSpan(w.cached_tokens)}</b> tokens`;

  // ⑥ 花费
  const usd = a.usd_cny ? w.cost_cny / a.usd_cny : null;
  $('kCost').innerHTML = `<span style="color:var(--red)" ${w.cost_cny != null ? `data-tip="${fullNum(w.cost_cny, 4)} 元"` : ''}>${fmtCostCny(w.cost_cny)}</span>`;
  $('kCostSub').innerHTML = usd != null ? `<span style="color:var(--green)">${usd.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})}</span> · 积分消耗 ${numSpan(w.credit, 1)}` : '—';

  $('pageSub').textContent = `区间 ${fmtCompact(w.total_tokens)} tokens · ${fmtCompact(w.requests)} 次请求 · 更新于 ${new Date().toLocaleTimeString('zh-CN', {hour12:false})}`;

  // B 版：环比徽章
  const selfCache = w.prompt_tokens ? (w.cached_tokens || 0) / w.prompt_tokens * 100 : null;
  const prevCache = (prevSummary && prevSummary.prompt) ? prevSummary.cached / prevSummary.prompt * 100 : null;
  setDelta('deltaTokens', w.total_tokens, prevSummary ? prevSummary.tok : null);
  setDelta('deltaReqs', w.requests, prevSummary ? prevSummary.req : null);
  setDelta('deltaCache', selfCache, prevCache);
  setDelta('deltaCost', w.credit, prevSummary ? prevSummary.cred : null);
}
function peakThroughput(){
  const s = state.series;
  if (!s || !s.series || !s.bucket_seconds) return null;
  let m = 0;
  for (const b of s.series) m = Math.max(m, (b.completion_tokens||0) / s.bucket_seconds);
  return m > 0 ? m : null;
}

/* ---------------- 时序图 ---------------- */
const seriesCanvas = $('seriesCanvas');
const seriesTip = $('seriesTip');
function renderSeriesMeta(){
  const s = state.series;
  if (!s) return;
  const unit = s.bucket_seconds >= 86400 ? '天' : s.bucket_seconds >= 3600 ? '小时' : '分钟';
  const tot = s.series.reduce((acc,b)=>({req:acc.req+b.requests, tok:acc.tok+b.total_tokens, cred:acc.cred+b.credit}),{req:0,tok:0,cred:0});
  $('seriesHint').textContent = `${fmtTime(s.since)}→${fmtTime(s.until)} · ${unit}粒度 · 总 ${fmtCompact(tot.tok)} tok / ${fmtCompact(tot.req)} 请求`;
}
function metricValue(b, metric, step){
  if (metric === 'tokens') return b.total_tokens || 0;
  if (metric === 'requests') return b.requests || 0;
  // Output throughput: prompt tokens are the whole conversation re-sent on
  // every turn, so total_tokens / wall time inflates the number on long
  // sessions (issue #509). What the model actually generates is completion.
  return (b.completion_tokens || 0) / step;
}
const seriesColorHost = document.getElementById('pageAnalytics');
function seriesColor(i){
  return getComputedStyle(seriesColorHost || document.documentElement).getPropertyValue(i).trim();
}
let seriesLayout = null;
function drawSeries(){
  const s = state.series;
  const cv = seriesCanvas, tip = seriesTip;
  const box = cv.parentElement;
  const dpr = window.devicePixelRatio || 1;
  const W = box.clientWidth, H = 176;
  cv.width = W * dpr; cv.height = H * dpr;
  cv.style.width = W + 'px'; cv.style.height = H + 'px';
  const ctx = cv.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);
  if (!s || !s.series){ return; }
  const series = s.series;
  const step = s.bucket_seconds || 3600;
  const metric = state.metric;
  const padL = 62, padR = 18, padT = 16, padB = 30;
  const iw = W - padL - padR, ih = H - padT - padB;
  const vals = series.map(b => metricValue(b, metric, step));
  const maxV = Math.max(1, ...vals);
  const dim = seriesColor('--dim'), dim2 = seriesColor('--dim2'), line = seriesColor('--line');
  const barC = seriesColor('--chart-bar'), lineC = seriesColor('--chart-line');

  // y 网格
  const nGrid = 4;
  ctx.font = '10.5px ' + getComputedStyle(document.body).fontFamily;
  ctx.fillStyle = dim2; ctx.textBaseline = 'middle';
  for (let g = 0; g <= nGrid; g++){
    const y = padT + ih - ih * g / nGrid;
    ctx.strokeStyle = line; ctx.globalAlpha = .7;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
    ctx.globalAlpha = 1;
    const v = maxV * g / nGrid;
    const label = fmtCompact(v, 0);
    ctx.fillText(label, 8, y);
  }

  // 柱（细柱风格：宽度上限 10px，桶少时也不过宽）
  const n = series.length;
  const bw = Math.max(1, Math.min(iw / n - Math.min(6, iw / n * .35), 10));
  const gap = iw / n;
  const bars = [];
  for (let i = 0; i < n; i++){
    const v = vals[i];
    const h = v / maxV * ih;
    const x = padL + gap * i + (gap - bw) / 2;
    const y = padT + ih - h;
    const grd = ctx.createLinearGradient(0, y, 0, padT + ih);
    grd.addColorStop(0, barC);
    grd.addColorStop(1, seriesColor('--chart-bar-soft'));
    ctx.fillStyle = i === n - 1 ? barC : grd;
    if (h >= 0.5){
      const r = Math.min(3, bw / 2);
      ctx.beginPath();
      ctx.moveTo(x, y + h); ctx.lineTo(x, y + r);
      ctx.quadraticCurveTo(x, y, x + r, y);
      ctx.lineTo(x + bw - r, y); ctx.quadraticCurveTo(x + bw, y, x + bw, y + r);
      ctx.lineTo(x + bw, y + h); ctx.closePath(); ctx.fill();
    }
    bars.push({x, y: padT + ih - h, w: bw, h, i});
  }

  // 折线（第二序列）：tokens 视图看请求数；requests 视图看错误；throughput 视图不画
  let lineVals = null, lineMax = 1, lineLabel = '';
  if (metric === 'tokens'){ lineVals = series.map(b => b.requests || 0); lineLabel = '请求数'; }
  if (metric === 'requests'){ lineVals = series.map(b => b.errors || 0); lineLabel = '失败数'; }
  if (lineVals){
    lineMax = Math.max(1, ...lineVals);
    ctx.strokeStyle = lineC; ctx.lineWidth = 1.6; ctx.globalAlpha = .9;
    ctx.beginPath();
    for (let i = 0; i < n; i++){
      const x = padL + gap * i + gap / 2;
      const y = padT + ih - lineVals[i] / lineMax * ih * .92;
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
    ctx.stroke(); ctx.globalAlpha = 1;
  }

  // x 轴标签：均匀取 6 个
  ctx.fillStyle = dim2; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  const ticks = Math.min(6, n);
  for (let t = 0; t < ticks; t++){
    const idx = Math.round(t * (n - 1) / Math.max(1, ticks - 1));
    const b = series[idx];
    const x = padL + gap * idx + gap / 2;
    const lbl = step >= 86400 ? fmtDate(b.at).slice(5)
      : `${String(new Date(b.at*1000).getHours()).padStart(2,'0')}:00`;
    ctx.fillText(lbl, Math.max(padL + 14, Math.min(W - padR - 14, x)), padT + ih + 8);
  }
  ctx.textAlign = 'left';

  // 图例
  const legend = [];
  legend.push(`<span><i style="background:${barC}"></i>${metric === 'tokens' ? 'Token 总量' : metric === 'requests' ? '请求数' : '输出 tok/s'}</span>`);
  if (lineVals) legend.push(`<span><i style="background:${lineC}"></i>${lineLabel}</span>`);
  $('seriesLegend').innerHTML = legend.join('');

  seriesLayout = {bars, series, step, metric, padL, padR, iw, gap, lineVals, lineMax, barC, lineC};

  // hover
  cv.onmousemove = ev => {
    if (!seriesLayout) return;
    // 空数据（今日还没有请求 / 新装实例）时 series.length 是 0，而下面那行的夹取是
    // Math.max(0, Math.min(n - 1, idx))：n-1 = -1 会被 Math.max 拉回 0，于是
    // series[0] 是 undefined，B 版气泡里的 b.at 直接抛 TypeError。没有点就收起返回。
    if (!series.length) { tip.style.opacity = 0; return; }
    const rect = cv.getBoundingClientRect();
    const mx = ev.clientX - rect.left;
    let idx = Math.round((mx - padL) / gap - .5);
    idx = Math.max(0, Math.min(series.length - 1, idx));
    const b = series[idx];
    if (!b) return;
    tip.style.opacity = 1;
    tip.style.left = Math.min(window.innerWidth - 200, ev.clientX + 14) + 'px';
    tip.style.top = Math.max(8, ev.clientY - 46) + 'px';
    const tt = step >= 86400 ? fmtDate(b.at) : fmtTime(b.at);
    const rows = [
      ['请求', fmtCompact(b.requests)],
      ['Token', fmtCompact(b.total_tokens)],
      ['吞吐', ((b.completion_tokens||0)/step).toFixed(1) + ' tok/s'],
      ['失败', fmtCompact(b.errors)],
      ['积分', (b.credit||0).toFixed(2)],
    ];
    tip.innerHTML = `<div class="tt-t">${tt}</div>` +
      rows.map(r => `<div class="tt-r"><span>${r[0]}</span><b>${r[1]}</b></div>`).join('');
  };
  cv.onmouseleave = () => { tip.style.opacity = 0; };
}

/* ---------------- 热力图 ---------------- */
function buildHeatGrid(heat){
  const grid = $('hmGrid'), monthsEl = $('hmMonths');
  if (!heat || !heat.series){ return; }
  const days = heat.series.filter(b => b.at > 0);
  const map = new Map(days.map(b => [fmtDate(b.at), b]));
  const cells = [];
  const today = new Date(); today.setHours(0,0,0,0);
  const start = new Date(today); start.setDate(start.getDate() - 365);
  // 对齐到周一
  const dow = (start.getDay() + 6) % 7;
  start.setDate(start.getDate() - dow);
  const maxReq = Math.max(1, ...days.map(b => b.requests || 0));
  let totalReq = 0, totalTok = 0, activeDays = 0;
  const monthSpans = []; let lastMonth = -1, weekIdx = 0;
  for (let d = new Date(start); d <= today; d.setDate(d.getDate() + 1)){
    const key = fmtDate(d.getTime()/1000);
    const b = map.get(key);
    const req = b ? (b.requests || 0) : 0;
    totalReq += req; totalTok += b ? (b.total_tokens||0) : 0;
    if (req > 0) activeDays++;
    const lv = req === 0 ? 0 : req < maxReq*.08 ? 1 : req < maxReq*.25 ? 2 : req < maxReq*.6 ? 3 : 4;
    const m = d.getMonth();
    if (m !== lastMonth){ monthSpans.push({m, col: weekIdx}); lastMonth = m; }
    cells.push(`<div class="hm-cell l${lv}" data-d="${key}" data-r="${req}" data-t="${b?(b.total_tokens||0):0}"></div>`);
    if (d.getDay() === 0) weekIdx++;
  }
  grid.style.gridTemplateColumns = `repeat(${weekIdx + 1}, 10px)`;
  grid.innerHTML = cells.join('');
  // 月份标签：按列定位
  const totalCols = weekIdx + 1;
  monthsEl.style.gridTemplateColumns = `repeat(${totalCols}, 10px)`;
  monthsEl.style.gap = '2px';
  monthsEl.innerHTML = monthSpans.map((s, i) => {
    const nextCol = i + 1 < monthSpans.length ? monthSpans[i+1].col : totalCols;
    const w = nextCol - s.col;
    if (w < 2) return '';
    return `<span style="grid-column:${s.col+1} / span ${w}">${s.m+1}月</span>`;
  }).join('');
  $('hmMeta').textContent = `${activeDays} 天有活动 · 共 ${fmtCompact(totalReq)} 次请求`;
  $('hmSummary').textContent = `近一年 ${fmtCompact(totalTok)} tokens`;
  // B 版：年度统计数字组
  let peakDay = null;
  for (const b of days){ if (!peakDay || (b.requests || 0) > (peakDay.requests || 0)) peakDay = b; }
  $('hmStats').innerHTML = `
    <div class="hm-stat"><b>${activeDays}</b><span>天有活动</span></div>
    <div class="hm-stat"><b>${fmtCompact(totalReq)}</b><span>总请求</span></div>
    <div class="hm-stat"><b>${fmtCompact(totalTok)}</b><span>总 Token</span></div>
    <div class="hm-stat"><b>${peakDay ? fmtCompact(peakDay.requests || 0) : '—'}</b><span>峰值单日${peakDay ? ' · ' + fmtDate(peakDay.at).slice(5) : ''}</span></div>`;
  // tooltip
  const tip = $('hmTip');
  grid.onmouseover = ev => {
    const c = ev.target.closest('.hm-cell');
    if (!c) return;
    tip.style.opacity = 1;
    tip.innerHTML = `<div class="tt-t">${c.dataset.d}</div>` +
      `<div class="tt-r"><span>请求</span><b>${fmtCompact(+c.dataset.r)}</b></div>` +
      `<div class="tt-r"><span>Token</span><b>${fmtCompact(+c.dataset.t)}</b></div>`;
    tip.style.left = Math.min(window.innerWidth - 200, ev.clientX + 14) + 'px';
    tip.style.top = Math.max(8, ev.clientY - 70) + 'px';
  };
  grid.onmouseleave = () => { tip.style.opacity = 0; };
}

/* ---------------- 用量明细（维度切换） ---------------- */
const dimDefs = {
  model: {
    head: ['模型', '请求', 'Token', '平均速度', '缓存命中', '消耗积分', '花费'],
    columns: [
      {id:'who', label:'模型'}, {id:'req', label:'请求', align:'num'},
      {id:'tok', label:'Token'}, {id:'speed', label:'平均速度', align:'num'},
      {id:'cache', label:'缓存命中', align:'num'}, {id:'credit', label:'消耗积分', align:'num'},
      {id:'cost', label:'花费', align:'num'},
    ],
    rows(){
      const a = state.analytics; if (!a) return [];
      const list = Array.isArray(a.models) ? a.models : Object.entries(a.models || {}).map(([name,v])=>({model:name, window:v}));
      return list.map(m => ({
        key: m.model || m.name,
        who: `<div class="who"><b>${m.model || m.name || '—'}</b><span>${fmtCompact(m.window.requests)} 次请求</span></div>`,
        req: m.window.requests,
        tok: m.window.total_tokens ?? m.window.tokens,
        tokSub: `思考 ${fmtCompact(m.window.reasoning_tokens ?? m.window.reasoning ?? 0)} · 缓存 ${fmtCompact(m.window.cached_tokens ?? 0)}`,
        speed: m.window.speed_avg,
        cache: m.window.cache_hit_pct,
        credit: m.window.credit != null ? Number(m.window.credit).toFixed(2) : '—',
        cost: m.window.cost_cny,
      }));
    }
  },
  account: {
    head: ['账号', '请求', 'Token', '平均速度', '缓存命中', '消耗积分', '花费'],
    columns: [
      {id:'who', label:'账号'}, {id:'req', label:'请求', align:'num'},
      {id:'tok', label:'Token'}, {id:'speed', label:'平均速度', align:'num'},
      {id:'cache', label:'缓存命中', align:'num'}, {id:'credit', label:'消耗积分', align:'num'},
      {id:'cost', label:'花费', align:'num'},
    ],
    rows(){
      const a = state.analytics; if (!a) return [];
      return (a.accounts || []).map(acc => ({
        key: acc.uid,
        who: `<div class="who"><b>${acc.nickname || acc.uid.slice(0,8)} <span class="realm-tag ${acc.realm === 'intl' ? 'realm-intl' : 'realm-cn'}">${acc.realm === 'intl' ? '国际版' : '国内版'}</span></b><span data-tip="UID：${acc.uid}">${(acc.uid||'').slice(0,13)}…</span></div>`,
        req: acc.window.requests,
        tok: acc.window.total_tokens,
        tokSub: `思考 ${fmtCompact(acc.window.reasoning_tokens)} · 缓存 ${fmtCompact(acc.window.cached_tokens)}`,
        speed: acc.window.speed_avg,
        cache: acc.window.cache_hit_pct,
        credit: acc.window.credit != null ? Number(acc.window.credit).toFixed(2) : '—',
        cost: acc.window.cost_cny,
      }));
    }
  },
  key: {
    head: ['API Key', '请求', 'Token', '平均速度', '缓存命中', '消耗积分', '花费'],
    columns: [
      {id:'who', label:'API Key'}, {id:'req', label:'请求', align:'num'},
      {id:'tok', label:'Token'}, {id:'speed', label:'平均速度', align:'num'},
      {id:'cache', label:'缓存命中', align:'num'}, {id:'credit', label:'消耗积分', align:'num'},
      {id:'cost', label:'花费', align:'num'},
    ],
    rows(){
      const a = state.analytics; if (!a) return [];
      return (a.keys || []).map(k => ({
        key: 'key:' + (k.key || k.name),
        who: `<div class="who"><b>${k.name || k.key}</b><span>${k.source === 'panel' ? '面板创建' : k.source || ''}${k.realm ? ' · ' + k.realm : ''}</span></div>`,
        req: k.window.requests,
        tok: k.window.total_tokens,
        tokSub: `思考 ${fmtCompact(k.window.reasoning_tokens)} · 缓存 ${fmtCompact(k.window.cached_tokens)}`,
        speed: k.window.speed_avg,
        cache: k.window.cache_hit_pct,
        credit: k.window.credit != null ? Number(k.window.credit).toFixed(2) : '—',
        cost: k.window.cost_cny,
      }));
    }
  },
};
const DIM_LAYOUT_KEY = 'wb-analytics-dim-layout-v1';
function dimLayout(dim){
  const known = dimDefs[dim].columns.map(c => c.id);
  const fallback = {order: known.slice(), hidden: []};
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(DIM_LAYOUT_KEY + ':' + dim) || 'null'); } catch(e){}
  const order = [], seen = new Set();
  (saved && Array.isArray(saved.order) ? saved.order : []).forEach(id => {
    if(known.includes(id) && !seen.has(id)){ seen.add(id); order.push(id); }
  });
  known.forEach(id => { if(!seen.has(id)) order.push(id); });
  const hidden = new Set(saved && Array.isArray(saved.hidden) ? saved.hidden.filter(id => known.includes(id)) : []);
  hidden.delete('who');
  if(order.every(id => hidden.has(id))) hidden.delete('who');
  return {order, hidden: Array.from(hidden)};
}
function saveDimLayout(dim, layout){
  try { localStorage.setItem(DIM_LAYOUT_KEY + ':' + dim, JSON.stringify(layout)); } catch(e){}
}
function visibleDimColumns(dim){
  const def = dimDefs[dim], layout = dimLayout(dim);
  return layout.order.map(id => def.columns.find(c => c.id === id)).filter(c => c && !layout.hidden.includes(c.id));
}
function renderDimColumnsMenu(){
  const menu = $('dimColumnsMenu'); if(!menu) return;
  const def = dimDefs[state.dim], layout = dimLayout(state.dim);
  const byId = Object.fromEntries(def.columns.map(c => [c.id, c]));
  menu.innerHTML = layout.order.map(id => {
    const c = byId[id]; if(!c) return '';
    const locked = id === 'who';
    return `<label class="dim-col-item ${locked ? 'locked' : ''}" draggable="${locked ? 'false' : 'true'}" data-dim-col="${id}">
      <span class="dim-col-grip">${locked ? '•' : '⋮⋮'}</span><input type="checkbox" ${layout.hidden.includes(id) ? '' : 'checked'} ${locked ? 'disabled checked' : ''} data-dim-toggle="${id}"><span>${c.label}</span></label>`;
  }).join('');
}
function updateDimLayout(dim, mutate){
  const layout = dimLayout(dim); mutate(layout);
  layout.order = layout.order.filter((id, i, arr) => arr.indexOf(id) === i && dimDefs[dim].columns.some(c => c.id === id));
  dimDefs[dim].columns.forEach(c => { if(!layout.order.includes(c.id)) layout.order.push(c.id); });
  layout.hidden = layout.hidden.filter(id => id !== 'who' && layout.order.includes(id));
  if(layout.order.every(id => layout.hidden.includes(id))) layout.hidden = layout.hidden.filter(id => id !== 'who');
  saveDimLayout(dim, layout); renderDim();
}


function renderDim(){
  const a = state.analytics;
  if (a){
    const cnt = def => dimDefs[def].rows().filter(r => (r.req || 0) > 0 || (r.tok || 0) > 0).length;
    $('cntModel').textContent = a ? cnt('model') : 0;
    $('cntAccount').textContent = a ? cnt('account') : 0;
    $('cntKey').textContent = a ? cnt('key') : 0;
  }
  const def = dimDefs[state.dim];
  const columns = visibleDimColumns(state.dim);
  const rows = def.rows()
    .filter(r => (r.req || 0) > 0 || (r.tok || 0) > 0)
    .sort((a, b) => (b.tok || 0) - (a.tok || 0));
  $('dimHead').innerHTML = '<tr>' + columns.map(c => `<th data-dim-col="${c.id}" draggable="true" class="${c.align === 'num' ? 'num' : ''}">${c.label}</th>`).join('') + '</tr>';
  if (!rows.length){
    $('dimBody').innerHTML = `<tr><td colspan="${columns.length}" class="skel">该区间暂无数据</td></tr>`;
    return;
  }
  const maxTok = Math.max(1, ...rows.map(r => r.tok || 0));
  const colors = ['var(--accent)', 'var(--blue)', 'var(--green)', 'var(--gold)', 'var(--red)'];
  const canExpand = state.dim !== 'key';
  $('dimBody').innerHTML = rows.map((r, ri) => {
    const pct = (r.tok || 0) / maxTok * 100;
    const cacheCls = r.cache == null ? '' : r.cache >= 90 ? 'ratio-ok' : r.cache >= 60 ? 'ratio-gold' : 'ratio-bad';
    const remainTxt = r.remain != null
      ? (r.remain > 100 ? `${fmtCompact(r.remain)}` : `<span class="${r.remain > 10 ? '' : 'ratio-bad'}">${Number(r.remain).toFixed(1)}</span>`)
      : '—';
    const cells = {
      who: `${canExpand ? '<span class="expander">▶</span>' : ''}${r.who}`,
      req: numSpan(r.req),
      tok: `<div class="tokcell"><span class="t">${numSpan(r.tok)}</span><div class="minibar"><i style="width:${Math.max(2,pct)}%;--bar-color:${colors[ri % colors.length]}"></i></div><span class="sub">${r.tokSub || ''}</span></div>`,
      speed: r.speed != null ? Number(r.speed).toFixed(0) + ' tok/s' : '—',
      cache: fmtPct(r.cache),
      credit: (r.credit != null && r.credit !== '—') ? (Number(r.credit) > 0 ? '<b style="color:var(--warn)">'+r.credit+'</b>' : r.credit) : '—',
      remain: remainTxt,
      cost: r.cost != null ? fmtCostCny(r.cost) : '—',
    };
    return `<tr class="dim-row" data-key="${encodeURIComponent(r.key || '')}">` + columns.map(c => `<td class="${c.align === 'num' ? 'num ' : ''}${c.id === 'cache' ? cacheCls : ''}" data-dim-col="${c.id}">${cells[c.id] == null ? '—' : cells[c.id]}</td>`).join('') + '</tr>';
  }).join('');
  applyOpenKey();
}
/* 轮询重渲染后恢复展开行（openKey 在 renderDim 的点击委托里维护） */
function applyOpenKey(){
  if (!state.openKey) return;
  const tr = Array.from($('dimBody').children).find(el => el.dataset && el.dataset.key === state.openKey);
  if (!tr){ state.openKey = null; return; }
  const row = buildExpandRow(state.openKey);
  if (row){
    tr.insertAdjacentHTML('afterend', row);
    tr.classList.add('open');
  }
}

/* ---------------- 行展开：分布钻取 ---------------- */
function modelByAccount(modelName){
  const a = state.analytics;
  const out = [];
  for (const acc of (a.accounts || [])){
    const wm = (acc.window_models || {})[modelName];
    if (wm && (wm.tokens || wm.requests)){
      out.push({
        name: acc.nickname || acc.uid.slice(0, 8),
        realm: acc.realm,
        tokens: wm.tokens || 0,
        requests: wm.requests || 0,
      });
    }
  }
  return out.sort((x, y) => y.tokens - x.tokens);
}
function accountByModel(uid){
  const a = state.analytics;
  const acc = (a.accounts || []).find(x => x.uid === uid);
  if (!acc || !acc.window_models) return [];
  return Object.entries(acc.window_models)
    .filter(([, v]) => (v.tokens || 0) > 0 || (v.requests || 0) > 0)
    .map(([model, v]) => ({ name: model, tokens: v.tokens || 0, requests: v.requests || 0 }))
    .sort((x, y) => y.tokens - x.tokens);
}
function donutHTML(items, colors){
  const total = items.reduce((s, x) => s + x.tokens, 0) || 1;
  let acc = 0;
  const segs = items.map((x, i) => {
    const s0 = acc / total * 360; acc += x.tokens;
    const s1 = acc / total * 360;
    return `${colors[i % colors.length]} ${s0.toFixed(2)}deg ${s1.toFixed(2)}deg`;
  });
  const top = items[0];
  const topPct = top ? (top.tokens / total * 100) : 0;
  return `<div class="donut" style="background:conic-gradient(${segs.join(',')})">
    <div class="hole"><b>${topPct.toFixed(0)}%</b><span>${top ? top.name.slice(0, 9) : ''}</span></div>
  </div>`;
}
function buildExpandRow(rowKey){
  const cols = visibleDimColumns(state.dim).length;
  let items = [], label = '';
  if (state.dim === 'model'){
    items = modelByAccount(decodeURIComponent(rowKey));
    label = '各账号使用分布';
  } else if (state.dim === 'account'){
    items = accountByModel(decodeURIComponent(rowKey));
    label = '各模型使用分布';
  } else {
    return null;
  }
  if (!items.length){
    return `<tr class="dim-expand"><td colspan="${cols}"><div class="exp-wrap"><span class="exp-none">该区间暂无分布明细</span></div></td></tr>`;
  }
  const colors = ['var(--accent)', 'var(--blue)', 'var(--green)', 'var(--gold)', 'var(--red)',
                  'var(--accent-2)', 'var(--dim)', 'var(--line2)'];
  const total = items.reduce((s, x) => s + x.tokens, 0) || 1;
  // 超过 8 项聚合为「其他」
  let shown = items;
  if (items.length > 8){
    const head = items.slice(0, 7);
    const rest = items.slice(7);
    head.push({ name: `其他 ${rest.length} 项`, tokens: rest.reduce((s, x) => s + x.tokens, 0), requests: rest.reduce((s, x) => s + x.requests, 0) });
    shown = head;
  }
  const list = shown.map((x, i) => `<div class="exp-item">
      <i style="background:${colors[i % colors.length]}"></i>
      <span class="nm" title="${x.name}">${x.name}</span>
      <span class="pct">${(x.tokens / total * 100).toFixed(1)}%</span>
      <span class="val" ${Number.isFinite(x.tokens) ? `data-tip="${fullNum(x.tokens)}"` : ''}>${fmtCompact(x.tokens)}</span>
    </div>`).join('');
  return `<tr class="dim-expand"><td colspan="${cols}">
    <div class="exp-wrap">
      ${donutHTML(shown, colors)}
      <div style="flex:1;min-width:280px">
        <div class="exp-title">${label} · 共 ${items.length} 项 · ${fullNum(total)} tokens</div>
        <div class="exp-list">${list}</div>
      </div>
    </div>
  </td></tr>`;
}
$('dimBody').addEventListener('click', ev => {
  const tr = ev.target.closest('tr.dim-row');
  if (!tr) return;
  const next = tr.nextElementSibling;
  const isOpen = next && next.classList.contains('dim-expand');
  document.querySelectorAll('.dim-expand').forEach(x => x.remove());
  document.querySelectorAll('tr.dim-row.open').forEach(x => x.classList.remove('open'));
  if (isOpen){ state.openKey = null; return; } // 再点收起
  const row = buildExpandRow(tr.dataset.key);
  if (row){
    tr.insertAdjacentHTML('afterend', row);
    tr.classList.add('open');
    state.openKey = tr.dataset.key;
  } else {
    state.openKey = null;
  }
});

/* ---------------- 折叠区 ---------------- */
function renderPerfFold(){
  const p = state.perf;
  if (!p){ return; }
  $('foldPerfBadge').textContent = `近 ${fmtCompact(p.sampled)} 个样本 · 成功率 ${fmtPct(p.success_rate_pct)}`;
  const rows = [['首字延迟 TTFT', p.ttft_ms, v => fmtSec(v)], ['生成时长', p.generation_ms, v => fmtSec(v)],
                ['全程耗时', p.wall_ms, v => fmtSec(v)], ['生成速度', p.tokens_per_sec, v => v?.toFixed(1) + ' tok/s'],
                ['缓存命中', p.cache_hit_pct, v => fmtPct(v)]];
  $('foldPerf').innerHTML = `<table class="dim"><thead><tr>
    <th>指标</th><th class="num">平均</th><th class="num">P50</th><th class="num">P90</th><th class="num">P99</th><th class="num">最大</th></tr></thead>
    <tbody>${rows.map(([name, d, f]) => d ? `<tr>
      <td><b>${name}</b></td>
      <td class="num">${f(d.avg)}</td><td class="num">${f(d.p50)}</td><td class="num">${f(d.p90)}</td><td class="num">${f(d.p99)}</td><td class="num">${f(d.max)}</td>
    </tr>` : '').join('')}</tbody></table>`;
}

/* ============================================================
   事件
   ============================================================ */
$('rangeSeg').addEventListener('click', ev => {
  const btn = ev.target.closest('button'); if (!btn) return;
  [...$('rangeSeg').children].forEach(b => b.classList.toggle('on', b === btn));
  state.range = btn.dataset.range;
  // 新区间的数字还没到：先把页头的旧读数清成占位，别让「全部」标题下面继续挂着本月的
  // 数字（那一段是页头最长的一行，误读代价最大）。取回后 renderKpis() 会重新写满。
  $('pageSub').textContent = '—';
  loadRange();
});
$('metricSeg').addEventListener('click', ev => {
  const btn = ev.target.closest('button'); if (!btn) return;
  [...$('metricSeg').children].forEach(b => b.classList.toggle('on', b === btn));
  state.metric = btn.dataset.metric;
  drawSeries();
});
$('dimTabs').addEventListener('click', ev => {
  const btn = ev.target.closest('button'); if (!btn) return;
  [...$('dimTabs').children].forEach(b => b.classList.toggle('on', b === btn));
  state.dim = btn.dataset.dim;
  state.openKey = null;
  renderDim();
  renderDimColumnsMenu();
});
const dimColumnsBtn = $('dimColumnsBtn');
const dimColumnsMenu = $('dimColumnsMenu');
if(dimColumnsBtn && dimColumnsMenu){
  dimColumnsBtn.addEventListener('click', ev => {
    ev.stopPropagation();
    const open = dimColumnsMenu.hidden;
    dimColumnsMenu.hidden = !open;
    dimColumnsBtn.setAttribute('aria-expanded', String(open));
    if(open) renderDimColumnsMenu();
  });
  dimColumnsMenu.addEventListener('change', ev => {
    const id = ev.target && ev.target.dataset && ev.target.dataset.dimToggle;
    if(!id || id === 'who') return;
    updateDimLayout(state.dim, layout => {
      const hidden = new Set(layout.hidden);
      if(ev.target.checked) hidden.delete(id); else hidden.add(id);
      layout.hidden = Array.from(hidden);
    });
    renderDimColumnsMenu();
  });
  let dragId = '';
  dimColumnsMenu.addEventListener('dragstart', ev => {
    const item = ev.target.closest('.dim-col-item');
    if(!item || item.classList.contains('locked')) return;
    dragId = item.dataset.dimCol;
    ev.dataTransfer.effectAllowed = 'move';
    ev.dataTransfer.setData('text/plain', dragId);
  });
  dimColumnsMenu.addEventListener('dragover', ev => {
    if(ev.target.closest('.dim-col-item')) ev.preventDefault();
  });
  dimColumnsMenu.addEventListener('drop', ev => {
    ev.preventDefault();
    const target = ev.target.closest('.dim-col-item');
    const toId = target && target.dataset.dimCol;
    if(!dragId || !toId || dragId === toId) return;
    updateDimLayout(state.dim, layout => {
      const from = layout.order.indexOf(dragId), to = layout.order.indexOf(toId);
      if(from < 0 || to < 0) return;
      layout.order.splice(from, 1); layout.order.splice(to, 0, dragId);
    });
    dragId = '';
    renderDimColumnsMenu();
  });
  document.addEventListener('click', ev => {
    if(!dimColumnsMenu.hidden && !ev.target.closest('#dimColumnsMenu, #dimColumnsBtn')){
      dimColumnsMenu.hidden = true; dimColumnsBtn.setAttribute('aria-expanded', 'false');
    }
  });
}
renderDimColumnsMenu();
window.addEventListener('resize', () => { drawSeries(); drawSparks(); });

/* 数字悬停：复用面板全局 [data-tip] 气泡（ensureHoverTip 的委托监听），
   本页的 numSpan/data-tip 元素自动生效。 */
try { ensureHoverTip(); } catch(e){}

/* 剩余用量 / 积分扣减 / 积分获取历史：数据由原版管道（loadRemainingUsage /
   loadCreditHistory / loadCreditGrants）画进折叠区；展开时按需再刷新一次
   （老版是每次进入看板都拉，这里收窄到按需）。积分扣减历史要跟随本页的范围
   选择器（旧管线的 analyticsRange 只认 today/week/month/all/custom）。 */
function syncOldRange(){
  const r = state.range;
  analyticsRange = (r === 'd3' || r === 'd30') ? 'today' : r;
}
{
  const rb = $('foldRemainBox');
  if (rb) rb.addEventListener('toggle', () => { if (rb.open) { try{ loadRemainingUsage(); }catch(e){} } });
  const cb = $('foldCreditsBox');
  if (cb) cb.addEventListener('toggle', () => { if (cb.open) { try{ syncOldRange(); loadCreditHistory(); }catch(e){} } });
  const gb = $('foldGrantsBox');
  if (gb) gb.addEventListener('toggle', () => { if (gb.open) { try{ loadCreditGrants(); }catch(e){} } });
}

/* ============================================================
   启动与对外接线
   ============================================================ */
let wbaStarted = false;
function wbaTick(){
  if (!wbaStarted){ wbaStarted = true; loadStatic(); }
  maybeLoadHeat();
  loadRange(true);   // 轮询：窗口上界按 15 秒桶量化，连续两轮同桶才有可能吃 304
}
window.loadAnalytics = wbaTick;            /* 面板 5s 轮询 / 切页 / 回前台统一入口 */
window.loadAnalyticsMatrix = function(){}; /* 旧性能矩阵已并入本页（性能分位折叠区） */
window.wbaLoad = wbaTick;                  /* 调试入口 */
/* 面板每 5 秒扫描全页表格并自动接排序（applyAllTableSorts）：把本页表格登记进
   禁排名单，避免表头被挂上排序箭头、破坏紧凑版式。剩余用量 / 积分扣减 / 积分
   获取三张表沿用原版的排序接线（自带 id 且已登记），不在此列。 */
if (typeof SORT_DENY_TABLES !== 'undefined' && SORT_DENY_TABLES && SORT_DENY_TABLES.add){
  ['dimTable', 'foldPerf'].forEach(function(id){ SORT_DENY_TABLES.add(id); });
}

})();
