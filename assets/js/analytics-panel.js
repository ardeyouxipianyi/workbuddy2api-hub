/* 时间范围：每个窗口都是服务端的一段切片，切换即重新取数。
 *
 * 只有「今日 / 全部历史」时，一次 /usage/analytics 同时带回两个桶，切换按钮
 * 重渲染缓存就够了；窗口变多之后一个 payload 装不下所有窗口，继续重渲染缓存
 * 只会把上一个窗口的数字挂到新标签下面，所以一律重取。 */
function setAnalyticsRange(range){
  analyticsRange = range;
  const btns = {today:'btnRangeToday', week:'btnRangeWeek', month:'btnRangeMonth',
                all:'btnRangeAll', custom:'btnRangeCustom'};
  Object.keys(btns).forEach(k => {
    const b = document.getElementById(btns[k]);
    if(b) b.classList.toggle('active', k === range);
  });
  const box = document.getElementById('rangeCustomBox');
  if(box) box.style.display = (range === 'custom') ? 'inline-flex' : 'none';
  if(range === 'custom') prefillCustomRange();
  loadAnalytics();
  // The model matrix reads /usage and /usage/perf directly, so it has to be
  // refetched: without this the table kept showing all-time figures while the
  // page said "today", which is the inconsistency the range buttons were
  // meant to remove.
  loadAnalyticsMatrix();
}

/* 自定义区间的输入框初始为空，而空 = 不限，画面会和「全部历史」一模一样，看起来
 * 像按钮坏了。第一次点开时把起点填成今天零点：一个能看懂的默认窗口，操作者再自己
 * 放宽或收窄；终点留空表示「到现在」。 */
function prefillCustomRange(){
  const el = document.getElementById('rangeCustomSince');
  if(!el || el.value) return;
  const d = new Date();
  el.value = toLocalInputValue(new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() / 1000);
}

function pad2(n){ return String(n).padStart(2, '0'); }

/* datetime-local 与 epoch 互转：输入框里是操作者按日历读到的本地墙上时间，所以
 * 转换用浏览器自己的时区；服务端拿 epoch 与它自己的时钟比较。 */
function toLocalInputValue(epochSec){
  if(!epochSec) return '';
  const d = new Date(epochSec * 1000);
  if(isNaN(d.getTime())) return '';
  return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate())
    + 'T' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
}

function fromLocalInputValue(value){
  if(!value) return null;
  const t = new Date(value).getTime();
  return isNaN(t) ? null : Math.floor(t / 1000);
}

function onRangeCustomChange(){
  loadAnalytics();
  loadAnalyticsMatrix();
}

/* 三个取数端点共用同一个查询串，避免 /usage 与 /usage/perf 各拼一份而漏掉自定义
 * 区间——那正是「卡片与表格口径不一致」的复发路径。 */
function analyticsQuery(){
  let q = '?realm=all&range=' + encodeURIComponent(analyticsRange || 'today');
  if(analyticsRange === 'custom'){
    const sEl = document.getElementById('rangeCustomSince');
    const uEl = document.getElementById('rangeCustomUntil');
    const s = fromLocalInputValue(sEl ? sEl.value : '');
    const u = fromLocalInputValue(uEl ? uEl.value : '');
    if(s !== null) q += '&since=' + s;
    if(u !== null) q += '&until=' + u;
  }
  return q;
}

/* 模型性能指标与用量一览
 *
 * Reads the combined view on purpose. The table carries its own 区域
 * column, so scoping it to the gateway's current exit would hide exactly the
 * models this page exists to compare. */
// /v1/models?realm=all 的目录在这里只作展示白名单（哪些模型算"已知模型"），
// 内容变化极慢，却跟着 5 秒轮询每轮重取一份（实测统计页开着约 12~15 次/分钟、
// 约 24KB/次）。改成一个 TTL：进统计页时取一次，之后最多每 10 分钟回源一次；
// 取失败不刷新时间戳，下一轮继续重试（和原来"失败也不清空白名单"一致）。
let matrixModelsAt = 0;
const MATRIX_MODELS_TTL_MS = 10 * 60 * 1000;
async function loadAnalyticsMatrix(){
  try{
      const q = analyticsQuery();
    const needModels = !MATRIX_MODELS.size || (Date.now() - matrixModelsAt) >= MATRIX_MODELS_TTL_MS;
    const [usage, perf, models] = await Promise.all([
      getJSON('/usage' + q, POLL_READ),
      getJSON('/usage/perf' + q, POLL_READ),
      // The catalog is only a display filter, so a failure here must not
      // blank the table; null keeps the previous allowlist.
      needModels ? getJSON('/v1/models?realm=all').catch(() => null) : Promise.resolve(null),
    ]);
    if(models && models.data){
      MATRIX_MODELS = new Set(models.data.map(m => m.id));
      matrixModelsAt = Date.now();
    }
    cachedMatrixData = { usage: usage, perf: perf };
    fillMatrixFilters(usage);
    renderPerfMatrix(usage, perf);
  }catch(err){
    if(!isAuthError(err)) console.error('loadAnalyticsMatrix failed:', err);
  }
}

/* Rebuild the two matrix filter dropdowns from the payload just fetched.
 *
 * Options are derived from the data rather than from the account pool, so the
 * list only offers accounts and models that actually have rows - a filter that
 * can only ever yield an empty table is worse than no filter. A previously
 * chosen value is kept when it is still present, and cleared when it is not,
 * which is what happens when the range flips and an account drops out.
 */
function fillMatrixFilters(usage){
  const acctSel = document.getElementById('matrixAcctFilter');
  const modelSel = document.getElementById('matrixModelFilter');
  const names = (usage && usage.accounts_map) || {};
  const modelCount = {};
  Object.keys((usage && usage.by_model) || {}).forEach(id => {
    if(!id.endsWith('-model')) modelCount[id] = (usage.by_model[id] || {}).requests || 0;
  });
  if(acctSel){
    const seen = new Set();
    Object.values((usage && usage.by_model_acct) || {}).forEach(byRealm => {
      Object.values(byRealm || {}).forEach(byAcct => {
        Object.keys(byAcct || {}).forEach(uid => {
          if(uid && uid !== '(unattributed)') seen.add(uid);
        });
      });
    });
    const opts = Array.from(seen).sort((a, b) => {
      const na = (names[a] || {}).nickname || a;
      const nb = (names[b] || {}).nickname || b;
      return na.localeCompare(nb);
    });
    if(matrixAcctFilter && !seen.has(matrixAcctFilter)) matrixAcctFilter = '';
    acctSel.innerHTML = '<option value="">全部账号</option>' + opts.map(uid => {
      const info = names[uid] || {};
      const label = (info.nickname || uid.slice(0, 8)) + ' (' + uid.slice(0, 6) + ')';
      return '<option value="' + esc(uid) + '"' + (uid === matrixAcctFilter ? ' selected' : '') + '>' + esc(label) + '</option>';
    }).join('');
  }
  if(modelSel){
    const ids = Object.keys(modelCount).filter(id => modelCount[id] > 0).sort();
    if(matrixModelFilter && ids.indexOf(matrixModelFilter) === -1) matrixModelFilter = '';
    modelSel.innerHTML = '<option value="">全部模型</option>' + ids.map(id =>
      '<option value="' + esc(id) + '"' + (id === matrixModelFilter ? ' selected' : '') + '>' + esc(id) + '</option>'
    ).join('');
  }
}

function renderPerfMatrix(usage, perf){
  const matrixEl = document.getElementById("perfMatrix");
  if(!matrixEl) return;
  const bmPerf = (perf && perf.by_model) || {};
  const validModels = MATRIX_MODELS;
  const modelMap = {};
  Object.entries(usage.by_model || {}).forEach(([id, m]) => {
    if((!validModels.size || validModels.has(id)) && m.requests > 0 && !id.endsWith('-model')){
      modelMap[id] = Object.assign({id: id}, m);
    }
  });
  Object.entries(bmPerf).forEach(([id, p]) => {
    if((!validModels.size || validModels.has(id)) && p.requests > 0 && !id.endsWith('-model') && !modelMap[id]){
      modelMap[id] = {
        id: id,
        requests: p.requests || 0,
        prompt_tokens: 0,
        completion_tokens: 0,
        reasoning_tokens: 0,
        cached_tokens: 0,
        total_tokens: 0
      };
    }
  });
  // One row per (model, exit). A model served by both exits gets two rows,
  // each carrying its own tokens, latency and speed; the region column then
  // describes exactly the numbers next to it instead of collapsing both.
  const realmMap = (usage && usage.by_model_realm) || {};
  const acctMap = (usage && usage.by_model_acct) || {};
  const perfAcctMap = (perf && perf.by_model_acct) || {};
  const rowsData = [];
  Object.values(modelMap)
    .sort((a,b) => (b.total_tokens || b.requests) - (a.total_tokens || a.requests))
    .forEach(m => {
      if(matrixModelFilter && m.id !== matrixModelFilter) return;
      const per = realmMap[m.id];
      const realms = per ? Object.keys(per).filter(r => (per[r].requests || 0) > 0) : [];
      if(realms.length > 1){
        // Most-used exit first, so the ordering stays meaningful.
        realms.sort((x, y) => (per[y].total_tokens || 0) - (per[x].total_tokens || 0));
      }
      // Within an exit, one row per account: two accounts sharing an exit are
      // two different consumers, and merging them hid which one spent what.
      const parts = [];
      (realms.length ? realms : ['']).forEach(r => {
        const accts = ((acctMap[m.id] || {})[r]) || {};
        const perfAccts = ((perfAcctMap[m.id] || {})[r]) || {};
        // Union of both maps: a bucket holding only failures exists in perf
        // alone, and dropping it would hide failures the total counts.
        const keys = Array.from(new Set(
          Object.keys(accts).concat(Object.keys(perfAccts))
        )).filter(a => {
          const u = accts[a] || {};
          const p = perfAccts[a] || {};
          return (u.requests || 0) > 0 || (u.errors || 0) > 0 ||
                 (p.requests || 0) > 0 || (p.errors || 0) > 0 ||
                 (p.client_aborted || 0) > 0;
        });
        // The account filter narrows the rows themselves, not just their
        // visibility, so the summary below always matches what is shown.
        const shown = matrixAcctFilter ? keys.filter(a => a === matrixAcctFilter) : keys;
        if(shown.length){
          shown.sort((x, y) =>
            ((accts[y] || {}).total_tokens || 0) - ((accts[x] || {}).total_tokens || 0));
          shown.forEach(a => {
            // Tokens come from usage; a failure-only bucket has none.
            const u = accts[a] || {requests: 0, prompt_tokens: 0, completion_tokens: 0,
                                   reasoning_tokens: 0, cached_tokens: 0, total_tokens: 0};
            parts.push({ stat: u, realm: r, acct: a,
                         accts: { [a]: u.requests || 0 } });
          });
        } else if(!matrixAcctFilter){
          // No per-account data for this exit (an older log, say): fall back
          // to the exit totals rather than dropping the row.
          const fallback = per ? per[r] : m;
          parts.push({ stat: fallback, realm: r, acct: '',
                       accts: fallback.accounts || {} });
        }
      });
      parts.forEach((p, i) => {
        rowsData.push(Object.assign({}, p.stat, {
          id: m.id, realm: p.realm, acct: p.acct, accts: p.accts,
          split: parts.length > 1, first: i === 0,
        }));
      });
    });

  const countEm = document.getElementById("matrixModelCount");
  if(countEm){
    const nModels = new Set(rowsData.map(r => r.id)).size;
    const anyFilter = !!(matrixAcctFilter || matrixModelFilter);
    countEm.textContent = nModels ? ("(有调用的模型: " + nModels +
      " 个·共 " + rowsData.length + " 行明细" + (anyFilter ? " · 已筛选" : "") + ")") : "";
  }

  if(!rowsData.length){
    matrixEl.innerHTML = '<div class="empty">当前版本暂无模型请求或性能数据</div>';
  } else {
    /* Summary row (Total / All Models).
     *
     * With no filter this is the endpoint-wide figure, as before. With a
     * filter active it is recomputed from the visible rows instead, because a
     * total that ignores the filter contradicts the rows directly beneath it -
     * the exact confusion the filter is meant to remove. Latency, speed and
     * wall time are averages over the filtered rows' own sample counts, so a
     * barely-used row cannot outvote a heavily-used one.
     */
    const filtering = !!(matrixAcctFilter || matrixModelFilter);
    let sumReq, sumErr, sumTok, sumP, sumC, sumR, totTtft, totTps, totWall, totCache, sumCost;
    if(!filtering){
      sumReq = usage.requests; sumErr = perf.errors; sumTok = usage.total_tokens;
      sumP = usage.prompt_tokens; sumC = usage.completion_tokens; sumR = usage.reasoning_tokens;
      // OpenRouter 价估算的合计：未筛选时取出口级汇总（与上面的数字同源）。
      sumCost = usage.cost_cny || 0;
      totTtft = perf.ttft_ms ? (ms(perf.ttft_ms.avg) + " (P50 " + ms(perf.ttft_ms.p50) + ")") : "-";
      totTps = perf.tokens_per_sec ? (perf.tokens_per_sec.avg.toFixed(1) + " tok/s") : "-";
      totWall = perf.wall_ms ? ms(perf.wall_ms.avg) : "-";
      totCache = perf.cache_hit_pct ? (perf.cache_hit_pct.avg.toFixed(1) + "%") : "-";
    } else {
      sumReq = 0; sumErr = 0; sumTok = 0; sumP = 0; sumC = 0; sumR = 0; sumCost = 0;
      let ttftW = 0, ttftN = 0, tpsW = 0, tpsN = 0, wallW = 0, wallN = 0, cacheW = 0, cacheN = 0;
      rowsData.forEach(m => {
        sumReq += m.requests || 0;
        sumTok += m.total_tokens || 0;
        sumP += m.prompt_tokens || 0;
        sumC += m.completion_tokens || 0;
        sumR += m.reasoning_tokens || 0;
        sumCost += m.cost_cny || 0;
        const mp = ((perf && perf.by_model_realm) || {})[m.id] || {};
        const mpAcctAll = ((perf && perf.by_model_acct) || {})[m.id] || {};
        const mpRealm = m.realm ? (mp[m.realm] || null) : null;
        const mpAcct = (m.realm && m.acct && mpAcctAll[m.realm])
          ? (mpAcctAll[m.realm][m.acct] || null) : null;
        const e = mpAcct || mpRealm || bmPerf[m.id] || {};
        sumErr += e.errors || 0;
        if(e.ttft_ms && e.ttft_ms.samples){ ttftW += e.ttft_ms.avg * e.ttft_ms.samples; ttftN += e.ttft_ms.samples; }
        if(e.tokens_per_sec && e.tokens_per_sec.samples){ tpsW += e.tokens_per_sec.avg * e.tokens_per_sec.samples; tpsN += e.tokens_per_sec.samples; }
        if(e.wall_ms && e.wall_ms.samples){ wallW += e.wall_ms.avg * e.wall_ms.samples; wallN += e.wall_ms.samples; }
        if(e.cache_hit_pct && e.cache_hit_pct.samples){ cacheW += e.cache_hit_pct.avg * e.cache_hit_pct.samples; cacheN += e.cache_hit_pct.samples; }
      });
      totTtft = ttftN ? ms(ttftW / ttftN) : "-";
      totTps = tpsN ? (((tpsW / tpsN)).toFixed(1) + " tok/s") : "-";
      totWall = wallN ? ms(wallW / wallN) : "-";
      totCache = cacheN ? ((cacheW / cacheN).toFixed(1) + "%") : "-";
    }
    const summaryRow = '<tr style="background:rgba(37,99,235,.06);font-weight:600">'
      + '<td style="text-align:left" data-label="模型"><b>' + (filtering ? '筛选结果合计 (Filtered)' : '全部模型合计 (Total)') + '</b></td>'
      + '<td style="text-align:center" data-label="区域"><span style="color:var(--dim)">—</span></td>'
      + '<td style="text-align:left" data-label="账号"><span style="color:var(--dim)">' + (filtering ? '已筛选' : '全部账号') + '</span></td>'
      + '<td data-label="请求数">' + fmt(sumReq) + '</td>'
      + '<td data-label="失败">' + (sumErr
          ? ('<span style="color:var(--bad)">' + fmt(sumErr) + '</span>')
          : '<span style="color:var(--dim)">0</span>') + '</td>'
      + '<td data-label="总 Token"><b style="color:var(--accent)">' + fmt(sumTok) + '</b></td>'
      + '<td class="triple" data-label="输入 / 输出 / 思考">' + tripleTokens(sumP, sumC, sumR) + '</td>'
      + '<td data-label="首字延迟">' + totTtft + '</td>'
      + '<td data-label="生成速度"><b style="color:var(--accent2)">' + totTps + '</b></td>'
      + '<td data-label="端到端耗时">' + totWall + '</td>'
      + '<td data-label="缓存命中">' + totCache + '</td>'
      + '<td data-label="用量占比">100%</td>'
      + '<td data-label="OpenRouter 价估算" class="pricing-col" style="color:var(--warn)">' + (PRICING_ON ? fmtCost(sumCost) : '—') + '</td>'
      + '</tr>';

    const rows = rowsData.map(m => {
      // Latency/speed come from the matching (model, exit) bucket when the
      // backend supplied one; otherwise fall back to the model-wide figure.
      const mp = ((perf && perf.by_model_realm) || {})[m.id] || {};
      const mpAcctAll = ((perf && perf.by_model_acct) || {})[m.id] || {};
      const mpRealm = m.realm ? (mp[m.realm] || null) : null;
      // Most specific first: this account on this exit, then the exit, then
      // the model. A split row must not borrow another account's latency.
      const mpAcct = (m.realm && m.acct && mpAcctAll[m.realm])
        ? (mpAcctAll[m.realm][m.acct] || null) : null;
      const mpEff = mpAcct || mpRealm || bmPerf[m.id] || {};
      const m_ttft = mpEff.ttft_ms ? (ms(mpEff.ttft_ms.avg) + " <span style='color:var(--dim);font-size:11px'>(P50 " + ms(mpEff.ttft_ms.p50) + ")</span>") : "-";
      const m_tps = mpEff.tokens_per_sec ? ('<b style="color:var(--accent2)">' + mpEff.tokens_per_sec.avg.toFixed(1) + '</b> <span style="font-size:11px;color:var(--dim)">tok/s</span>') : "-";
      const m_wall = mpEff.wall_ms ? ms(mpEff.wall_ms.avg) : "-";
      const m_cache = (m.prompt_tokens > 0) ? pct(m.cached_tokens / m.prompt_tokens * 100) : (mpEff.cache_hit_pct ? mpEff.cache_hit_pct.avg.toFixed(1) + "%" : "-");
      /* Share of the table's own total - the figure in the summary row -
       * rather than of the largest row. Dividing by the maximum made the
       * biggest model read 100% and every other row a fraction of it, which
       * is a relative ranking, not the share the column name promises.
       * One decimal, like every other percentage on the page: a whole-percent
       * reading collapses a model that spent millions of tokens to "0%". */
      const pctTok = sumTok > 0 ? Math.round(m.total_tokens / sumTok * 1000) / 10 : 0;
      // 0 is a real result (the model ran and never failed), so it stays
      // visible as a muted zero rather than the dash used for no data.
      const errCell = mpEff.errors
        ? ('<span style="color:var(--bad)">' + fmt(mpEff.errors) + '</span>')
        : '<span style="color:var(--dim)">0</span>';
      const acctsMap = usage.accounts_map || {};
      // This row's own accounts, not every account that touched the model.
      const mAccts = m.accts || m.accounts || {};
      const acctEntries = Object.entries(mAccts);
      let realmCell = '<span style="color:var(--dim)">—</span>';
      let acctCell = '<span style="color:var(--dim)">—</span>';
      const realms = new Set();
      if(m.realm){
        realms.add(m.realm);
      } else {
        acctEntries.forEach(([uid]) => {
          const info = acctsMap[uid] || {};
          if(info.realm) realms.add(info.realm);
        });
      }
      if(realms.size === 1){
        const r = Array.from(realms)[0];
        realmCell = r === 'cn' ? '<span class="realm-badge-cn">国内版</span>' : '<span class="realm-badge-intl">国际版</span>';
      } else if(realms.size > 1){
        realmCell = '<span class="realm-badge-intl">国际版</span> <span class="realm-badge-cn">国内版</span>';
      } else {
        // No account attribution on these rows (a test call,
        // say). Decide from the model id itself: this table
        // spans both realms, so the gateway's current exit
        // would mislabel domestic-only models.
        const isIntlOnly = ['gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna', 'gpt-5.5', 'gpt-5.4', 'gpt-5.3-codex', 'gemini-3.5-flash', 'grok-4.7'].includes(m.id);
        const isCnOnly = ['deepseek-v4-pro', 'glm-5.1', 'glm-5v-turbo', 'kimi-k3-1', 'kimi-k2.7', 'minimax-m3'].includes(m.id);
        realmCell = isIntlOnly ? '<span class="realm-badge-intl">国际版</span>'
          : (isCnOnly ? '<span class="realm-badge-cn">国内版</span>'
            : '<span style="color:var(--dim)">—</span>');
      }
      if(acctEntries.length === 1 && acctEntries[0][0] === '(unattributed)'){
        acctCell = '<span style="color:var(--dim)">—</span>';
      } else if(acctEntries.length === 1){
        const [uid, count] = acctEntries[0];
        const info = acctsMap[uid] || {};
        const name = esc(info.nickname || uid.slice(0, 8));
        acctCell = '<b>' + name + '</b>';
      } else if(acctEntries.length > 1){
        acctCell = acctEntries.map(([uid, count]) => {
          const info = acctsMap[uid] || {};
          const name = esc(info.nickname || uid.slice(0, 6));
          return '<span class="badge mini" style="margin:1px 2px">' + name + ' <span style="color:var(--dim)">(' + count + ')</span></span>';
        }).join('');
      }
      return '<tr>'
        + '<td style="text-align:left" class="mono" data-label="模型"><b>' + esc(m.id) + '</b></td>'
        + '<td style="text-align:center;white-space:nowrap" data-label="区域">' + realmCell + '</td>'
        + '<td style="text-align:left" data-label="账号">' + acctCell + '</td>'
        + '<td data-label="请求数">' + fmt(m.requests) + '</td>'
        + '<td data-label="失败">' + errCell + '</td>'
        + '<td data-label="总 Token"><b>' + fmt(m.total_tokens) + '</b></td>'
        + '<td class="triple" data-label="输入 / 输出 / 思考">' + tripleTokens(m.prompt_tokens, m.completion_tokens, m.reasoning_tokens) + '</td>'
        + '<td data-label="首字延迟">' + m_ttft + '</td>'
        + '<td data-label="生成速度">' + m_tps + '</td>'
        + '<td data-label="端到端耗时">' + m_wall + '</td>'
        + '<td data-label="缓存命中">' + m_cache + '</td>'
        + '<td style="min-width:90px" data-label="用量占比"><span class="bar-fill" style="width:' + Math.max(3, pctTok).toFixed(1) + '%"></span> <span style="font-size:11px;color:var(--dim)">' + pctTok.toFixed(1) + '%</span></td>'
        + '<td data-label="OpenRouter 价估算" class="pricing-col" style="color:var(--warn)" title="OpenRouter 价估算：按该行（模型·区域·账号）的 token 消耗折算；按条件定价的模型（输入长度阈值或时段）按每条请求取档；单条请求实际用的策略、三档单价与匹配来源见「最近请求」里那条的悬停提示">'
        +   ((m.cost_cny || 0) > 0 ? fmtCost(m.cost_cny) : '<span style="color:var(--dim)">—</span>')
        + '</td>'
        + '</tr>';
    }).join('');

    // 延迟 / 速度列取自日志末尾的采样，窗口比采样更宽时它们只覆盖最新的那一段。
    // 明说覆盖范围，比让局部数字冒充整个窗口要好。
    let coverageNote = '';
    if(perf && perf.sample_capped && perf.sample_from){
      coverageNote = '<div style="font-size:12px;color:var(--dim);margin-bottom:8px">'
        + '延迟 / 速度列只覆盖日志末尾的部分请求（自 '
        + esc(new Date(perf.sample_from * 1000).toLocaleString())
        + ' 起），更早的请求未纳入这三列。</div>';
    }
    matrixEl.innerHTML = coverageNote + '<table class="data-cards"><thead><tr>'
      + '<th style="text-align:left">模型名称</th>'
      + '<th style="text-align:center;width:76px">区域</th>'
      + '<th style="text-align:left">账号</th>'
      + '<th>请求数</th><th>失败</th><th>总 Token</th><th style="min-width:190px">输入 / 输出 / 思考</th>'
      + '<th>首字延迟 (TTFT)</th><th>生成速度</th><th>端到端耗时</th><th>缓存命中率</th><th>用量占比</th>'
      + '<th class="pricing-col" style="color:var(--warn)">估算价格</th>'
      + '</tr></thead><tbody>' + summaryRow + rows + '</tbody></table>';
    applyTableSort('perfMatrix');
  }

}

async function loadAnalytics(){
  try {
    // 载荷永远按当前选中的窗口取，它的 window 桶就是第一列要展示的数字。
    const data = await getJSON('/usage/analytics' + analyticsQuery(), POLL_READ);
    renderAnalytics(data);
    loadUsageSeries();
    loadRemainingUsage();
    loadCreditGrants();
  } catch(err) {
    console.error('loadAnalytics failed:', err);
  }
}

// ---------------------------------------------------------------------------
// 剩余用量估算（/usage/remaining）
//
// 上游按 24 小时窗口给「账号 × 模型」配额，预算 = 撞线账号窗口用量的平均；
// 这个区块把每个账号当前窗口的剩余列出来。数字随用量实时变化，不跟时间窗
// 选择器联动（它只描述「现在」），所以载荷不带 range 参数。
// ---------------------------------------------------------------------------
async function loadRemainingUsage(){
  try {
    const data = await getJSON('/usage/remaining', POLL_READ);
    renderRemainingUsage(data);
  } catch(err) {
    if(!isAuthError(err)) console.error('loadRemainingUsage failed:', err);
  }
}

function fmtTok(v){
  if(v == null) return '—';
  const n = Number(v) || 0;
  if(Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if(Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(n);
}

// 上一轮真正画进 DOM 的载荷指纹（同 refreshInner 里的 recentFp 那套）。
let lastRemainingFp = null;
// 模型筛选（点「模型总量」徽章只看那个模型，再点一次显示全部）与最近一次载荷
// ——切换筛选要立刻重画，不能等下一轮轮询。
let remainingModelFilter = '';
let lastRemainingData = null;

function toggleRemainingModelFilter(model){
  remainingModelFilter = (remainingModelFilter === model) ? '' : (model || '');
  if(lastRemainingData) renderRemainingUsage(lastRemainingData);
}

// 剩余用量按区域分成两块（国际版 / 国内版）：各自的徽章、统计与表格；区域列
// 因此不再需要——区块标签已经说明了区域。
const REMAINING_REALMS = [
  {realm: 'intl', budgets: 'remainingBudgetsIntl',
   summary: 'remainingSummaryIntl', tbody: 'remainingTbodyIntl'},
  {realm: 'cn', budgets: 'remainingBudgetsCn',
   summary: 'remainingSummaryCn', tbody: 'remainingTbodyCn'},
];

function renderRemainingUsage(data){
  const bodies = REMAINING_REALMS.map(r => document.getElementById(r.tbody));
  if(!bodies[0]) return;
  const tbody = bodies[0];   // 指纹守卫只关心「这一块画过没有」
  // 5 秒一轮的重画里，载荷没变就不碰 DOM：接口挂了 15 秒 TTL，同一个缓存
  // 条目会连着命中好几轮轮询，每轮重写三十行 innerHTML 是白付的解析与布局。
  // 指纹取载荷本身——它包含所有会画进表格的输入，而同一缓存条目的两次响应
  // 逐字节相同。tbody 为空时照常重画（换页签/清空后不能停在空白上）。
  // 筛选状态也算指纹的一部分：切换筛选必须立刻重画。
  const fp = remainingModelFilter + '\u0000' + JSON.stringify(data);
  if(fp === lastRemainingFp && tbody.innerHTML) return;
  lastRemainingFp = fp;
  lastRemainingData = data;
  const rows = (data && data.rows) || [];
  const budgets = (data && data.budgets) || [];
  // 模型筛选：徽章选中的模型只留它自己的行（'' = 全部）。统计行按可见行算，
  // 免得「表里 3 行、上面写 20 项」。
  const visible = remainingModelFilter
    ? rows.filter(r => r.model === remainingModelFilter) : rows;

  const winEm = document.getElementById('remainingUsageWindow');
  if(winEm){
    winEm.textContent = data ? ('窗口 ' + (data.window_seconds / 3600) + 'h') : '';
  }
  // 预算样本与按模型总剩余并成同一个徽章：同一个 (区域, 模型) 的两个指标
  // （预算来自撞线样本；总剩余 = 可用组的剩余之和，冷却/已满记 0、只加不减），
  // 分两行展示会被当成重复条目。徽章按区域各画一块。
  const models = (data && data.models) || [];
  const merged = new Map();
  budgets.forEach(b => merged.set(b.realm + '\u0000' + b.model, {budget: b}));
  models.forEach(m => {
    const key = m.realm + '\u0000' + m.model;
    const entry = merged.get(key) || {};
    entry.model = m;
    merged.set(key, entry);
  });
  REMAINING_REALMS.forEach(({realm, budgets: elId}) => {
    const el = document.getElementById(elId);
    if(!el) return;
    const chips = [];
    merged.forEach(entry => {
      const budget = entry.budget, model = entry.model;
      const src = model || budget;
      if(src.realm !== realm) return;
      // 近两日没消耗的模型不参与剩余计算（服务端过滤），但预算样本保留并
      // 标「未参与」——预算要撞线才学得到，不能因为暂时没用就从面板消失。
      const off = budget && budget.active === false;
      const bits = [];
      const tips = [];
      if(budget){
        bits.push('预算 ≈ <b>' + fmtTok(budget.avg) + '</b>'
                  + ' <span style="color:var(--dim)">(n=' + budget.n + ')</span>'
                  + (off ? ' <span style="color:var(--dim)">未参与</span>' : ''));
        tips.push('样本 ' + budget.n + ' · 区间 ' + fmtTok(budget.min) + ' ~ ' + fmtTok(budget.max)
                  + (off ? ' · 近两日未使用，未参与剩余用量' : ''));
      }
      if(model){
        bits.push('总剩余 <b>'
                  + (model.total_remaining == null ? '—' : fmtTok(model.total_remaining)) + '</b>'
                  + ' <span style="color:var(--dim)">(可用 ' + (model.available || 0)
                  + ' · 冷却 ' + (model.cooling || 0)
                  + (model.unknown ? ' · 未知 ' + model.unknown : '') + ')</span>');
        tips.push('按模型汇总：可用组的剩余之和；冷却/用完的组记 0，不拉低别的组');
      }
      chips.push('<span class="badge" style="font-size:11px;cursor:pointer'
        + (off ? ';opacity:.55' : '')
        + (remainingModelFilter === src.model
           ? ';outline:2px solid var(--accent);outline-offset:1px' : '')
        + '" title="' + esc(tips.join('；') + ' · 点击只看这个模型，再点一次显示全部') + '"'
        + ' data-model="' + esc(src.model) + '"'
        + ' onclick="toggleRemainingModelFilter(this.dataset.model)">'
        + esc(src.model) + ' · ' + bits.join(' · ') + '</span>');
    });
    el.innerHTML = chips.length
      ? chips.join(' ')
      : '<span style="font-size:12px;color:var(--dim)">暂无预算样本：等某个账号把某个模型的窗口用满（撞线）后自动出现。</span>';
  });

  const countLine = list => {
    const cooling = list.filter(r => r.cooling).length;
    const tight = list.filter(r => !r.cooling && r.used_pct != null && r.used_pct >= 80).length;
    return list.length
      ? (list.length + ' 项 · 冷却中 ' + cooling + ' · 用量 ≥80% ' + tight)
      : '暂无数据';
  };
  const summary = document.getElementById('remainingUsageSummary');
  if(summary) summary.textContent = countLine(visible);
  REMAINING_REALMS.forEach(({realm, summary: elId}) => {
    const el = document.getElementById(elId);
    if(el) el.textContent = countLine(visible.filter(r => r.realm === realm));
  });

const emptyRow = '<tr><td colspan="6" style="text-align:center;color:var(--dim)">'
    + (remainingModelFilter
       ? esc('「' + remainingModelFilter + '」在当前范围内没有记录（再点一次选中的徽章显示全部）')
       : '暂无数据（需要账号先有用量或撞线记录）')
    + '</td></tr>';
  REMAINING_REALMS.forEach(({realm, tbody: bodyId}) => {
    const body = document.getElementById(bodyId);
    if(!body) return;
    const realmRows = visible.filter(r => r.realm === realm);
    if(!realmRows.length){
      body.innerHTML = emptyRow;
      return;
    }
    body.innerHTML = realmRows.map(r => {
      const pct = r.used_pct == null ? null : Math.max(0, Math.min(100, r.used_pct));
      const color = r.cooling || (pct != null && pct >= 90) ? 'var(--bad)'
        : (pct != null && pct >= 70 ? 'var(--warn)' : 'var(--accent2)');
      const bar = '<div style="display:flex;align-items:center;gap:6px">'
        + '<div style="flex:1;height:6px;background:var(--panel2);border:1px solid var(--line);border-radius:3px;overflow:hidden">'
        + '<div style="height:100%;width:' + (pct == null ? 0 : pct) + '%;background:' + color + ';transition:width .3s ease"></div>'
        + '</div>'
        + '<span style="font-size:11px;color:var(--dim);min-width:44px;text-align:right">'
        + (r.used_pct == null ? '—' : r.used_pct.toFixed(1) + '%') + '</span></div>';
      const flags = [];
      if(r.cooling) flags.push('<span class="badge" style="font-size:10px;color:var(--bad)">冷却至 '
        + esc((r.reset_iso || '').slice(5, 16)) + '</span>');
        else if(r.estimated) flags.push('<span class="badge" style="font-size:10px;color:var(--dim)" '
        + 'title="当前窗口的起点未知（没有撞线记录，或记录已过期），已用按最近 24 小时统计，是剩余量的下界">按 24h 估算</span>');
      const remainCell = r.remaining == null ? '—'
        : (r.cooling ? '<b style="color:var(--bad)">0</b>' : '<b>' + fmtTok(r.remaining) + '</b>');
      // 分段明细不在这张表里展示：两段相加的总量看「各账号用量透视与模型消耗
      // 分布」（原始消耗本来就含两段）。这里只保留冷却行的悬停提示——窗口的
      // 记账在撞线处截止。
      const segs = Array.isArray(r.segments) ? r.segments : [];
      const usedTitle = r.cooling && segs.length
        ? ' title="' + esc('已用到撞线时刻为止（' + (segs[0].end_iso || '').slice(5, 16)
                           + '），额度 ' + (r.reset_iso || '').slice(5, 16) + ' 恢复') + '"'
        : '';
      return '<tr>'
        + '<td style="text-align:left">' + esc(r.nickname || (r.uid || '').slice(0, 8)) + '</td>'
        + '<td style="text-align:left">' + esc(r.model) + ' ' + flags.join(' ') + '</td>'
        + '<td style="text-align:center;font-size:11px;color:var(--dim)">'
        + esc((r.window_iso || '').slice(5, 16)) + '</td>'
        + '<td style="text-align:right"' + usedTitle + '>' + fmtTok(r.used) + '</td>'
        + '<td style="text-align:right">' + remainCell + '</td>'
        + '<td>' + bar + '</td>'
        + '</tr>';
    }).join('');
  });
  const sampleBody = document.getElementById('remainingSamplesBody');
  if(sampleBody){
    const groups = budgets.filter(b => Array.isArray(b.samples_list) && b.samples_list.length);
    if(!groups.length){
      sampleBody.textContent = '暂无撞线样本。';
    } else {
      sampleBody.innerHTML = groups.map(b => {
        const rows = b.samples_list.map(s =>
          '<div style="margin-left:12px">'
          + esc((s.at_iso || '').slice(5, 16)) + ' · '
          + esc((s.account || '').slice(0, 8)) + ' · '
          + fmtTok(s.sample) + ' tok</div>').join('');
        return '<div style="margin:6px 0"><b>'
          + esc(b.realm === 'cn' ? '国内版' : '国际版') + ' / ' + esc(b.model)
          + '</b> <span style="color:var(--dim)">n=' + b.n
          + ' · ' + fmtTok(b.min) + ' ~ ' + fmtTok(b.max) + '</span>'
          + rows + '</div>';
      }).join('');
    }
  }
    applyTableSort('remainingUsageTableIntl');
    applyTableSort('remainingUsageTableCn');
}

function remainingSamplesText(){
  const budgets = (lastRemainingData && lastRemainingData.budgets) || [];
  const lines = [];
  budgets.forEach(b => {
    const rows = b.samples_list || [];
    if(!rows.length) return;
    const vals = rows.map(s => fmtTok(s.sample)).join(' / ');
    lines.push((b.realm === 'cn' ? '国内版' : '国际版') + ' / ' + b.model + ' / ' + vals);
  });
  return lines.join('\n');
}

function copyRemainingSamples(){
  const text = remainingSamplesText();
  if(!text){ toast('暂无撞线样本', 'warn'); return; }
  writeClipboard(text).then(() => {
    toast('已复制 ' + text.split('\n').length + ' 组样本', 'ok');
  }).catch(e => toast('复制失败: ' + e.message, 'bad'));
}

function loadUsageSeries(){
  getJSON('/usage/timeseries' + analyticsQuery(), POLL_READ)
    .then(data => { renderCreditHistory(data); })
    .catch(() => {});
}

async function loadCreditHistory(btn){
  if(btn) btn.disabled = true;
  try{
    const data = await getJSON('/usage/timeseries' + analyticsQuery(), POLL_READ);
    renderCreditHistory(data);
    if(btn) toast('积分扣减历史已刷新', 'ok');
  }catch(e){
    if(btn) toast('刷新失败: ' + e.message, 'bad');
  }finally{
    if(btn) btn.disabled = false;
  }
}

// 最近一次的积分扣减数据。账号昵称来自 /accounts（另一条并行请求），
// 它比 /usage/timeseries 晚到时就先用 uid 兜底，等账号到位后再补画一次。
let LAST_CREDIT_DATA = null;

// uid -> 昵称。取不到（账号已删除/尚未加载完）就回退 uid 前 8 位，不留空。
function creditAccountLabel(uid){
  const id = String(uid || '');
  if(!id) return '—';
  const acct = (window.ACCOUNTS || []).find(a => a.uid === id);
  return (acct && acct.nickname) || id.slice(0, 8);
}

function renderCreditHistory(data){
  if(data) LAST_CREDIT_DATA = data;
  const rows = ((LAST_CREDIT_DATA && LAST_CREDIT_DATA.credits) || []).slice(0, 50);
  const tbody = document.querySelector('#creditHistoryTable tbody');
  if(!tbody) return;
  tbody.innerHTML = rows.map(x => '<tr>'
    + '<td>' + esc(x.iso || '') + '</td>'
    + '<td>' + esc(x.model || '') + '</td>'
    + '<td title="' + esc(x.account || '') + '">' + esc(creditAccountLabel(x.account)) + '</td>'
    + '<td>' + fmt(x.credit) + '</td>'
    + '<td>' + fmt(x.total_tokens || 0) + '</td>'
    + '</tr>').join('') || '<tr><td colspan="5" style="color:var(--dim)">没有积分扣减记录</td></tr>';
  applyTableSort('creditHistoryTable');
}

// ---------------------------------------------------------------------------
// 积分获取历史（/accounts/credits/grants）
//
// 上游给每个账号一份积分包清单（免费套餐、每日活跃奖励的 Bonus Pack、活动包…），
// 每个包带面额与发放时间。这个区块把全部账号的包摊平成一张表、新的在前；接口只读
// 最近一次「刷新积分」留下的快照（不发上游请求），所以摘要里带上快照时刻。
// ---------------------------------------------------------------------------
let LAST_GRANTS_DATA = null;
const GRANT_STATUS_LABEL = {active: '生效中', available: '可用', used_up: '已用完', expired: '已过期'};

async function loadCreditGrants(btn){
  if(btn){ btn.disabled = true; btn.textContent = '刷新中...'; }
  try{
    const data = await getJSON('/accounts/credits/grants', POLL_READ);
    renderCreditGrants(data);
    if(btn) toast('积分获取历史已刷新', 'ok');
  }catch(e){
    if(!isAuthError(e)) toast('积分获取历史加载失败: ' + e.message, 'bad');
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '刷新'; }
  }
}

function renderCreditGrants(data){
  if(data) LAST_GRANTS_DATA = data;
  const d = LAST_GRANTS_DATA || {};
  const rows = (d.rows || []).slice(0, 100);
  const s = d.summary || {};
  const summary = document.getElementById('creditGrantsSummary');
  if(summary){
    summary.textContent = rows.length
      ? ('共 ' + fmt(s.count || rows.length) + ' 笔 · 合计 ' + fmt(s.size || 0)
         + ' · 剩余 ' + fmt(s.remain || 0)
         + (d.fetched_iso ? ' · 数据截至 ' + String(d.fetched_iso).slice(5, 16) : ''))
      : '暂无积分获取记录（上游只返回当前仍可见的积分包，先点「一键刷新积分」拉一次）';
  }
  const tbody = document.getElementById('creditGrantsTbody');
  if(!tbody) return;
  tbody.innerHTML = rows.map(r => {
    const status = GRANT_STATUS_LABEL[r.status] || String(r.status || '');
    const tone = (r.status === 'expired' || r.status === 'used_up') ? 'bad'
      : (r.status === 'active' ? 'ok' : '');
    const expiry = r.no_expiry ? '不过期' : String(r.expire_iso || '').slice(5, 16) || '—';
    // 来源：上游给了发放原因就用它的原文（如「Buddy 加油站签到」「成长计划奖励」），
    // 没有就按包名与面额推断（国际版的 Bonus Pack 30/50 就是每日活跃奖励）。本机
    // 签到 / 每日活跃的记录按时间对得上时一并挂进悬停提示，指出这次发放对应哪次动作。
    const reason = String(r.grant_reason || '').trim();
    const inferred = !reason && r.realm === 'intl' && /bonus/i.test(String(r.name || ''))
      && (Number(r.size) === 30 || Number(r.size) === 50);
    const source = reason || (inferred ? '每日活跃奖励' : '');
    const tips = [];
    if(reason) tips.push('上游发放原因：' + reason);
    if(inferred) tips.push('按包名与面额推断（官方规则：每日活跃 30 / Pro 50 积分）');
    if(r.action && r.action.task){
      const task = r.action.task === 'checkin' ? '每日签到' : '每日活跃打卡';
      tips.push('本机动作：' + task + ' ' + String(r.action.ts || '').slice(5, 16).replace('T', ' ')
                + ' ' + (r.action.ok ? '成功' : '失败'));
    }
    return '<tr>'
      + '<td' + (r.create_iso ? ' data-sort="' + esc(r.create_iso) + '"' : '') + '>'
      + esc(String(r.create_iso || '').slice(5, 16) || '—') + '</td>'
      + '<td title="' + esc(r.uid || '') + '">' + esc(r.nickname || creditAccountLabel(r.uid)) + '</td>'
      + '<td style="text-align:center">' + esc(r.realm === 'cn' ? '国内版' : '国际版') + '</td>'
      + '<td' + (r.grant_reason ? ' title="' + esc(r.grant_reason) + '"' : '') + '>' + esc(r.name || '') + '</td>'
      + '<td' + (tips.length ? ' title="' + esc(tips.join('；')) + '"' : '') + '>' + esc(source || '—') + '</td>'
      + '<td style="text-align:right">' + fmt(r.size || 0) + '</td>'
      + '<td style="text-align:right">' + fmt(r.remain || 0) + '</td>'
      + '<td style="text-align:center;font-size:11px;color:var(--dim)"'
      + (!r.no_expiry && r.expire_iso ? ' data-sort="' + esc(r.expire_iso) + '"' : '') + '>'
      + esc(expiry) + '</td>'
      + '<td style="text-align:center"><span class="badge ' + tone + '">' + esc(status) + '</span></td>'
      + '</tr>';
  }).join('') || '<tr><td colspan="9" style="color:var(--dim)">暂无积分获取记录</td></tr>';
  applyTableSort('creditGrantsTable');
}

/* 第一列的口径随范围切换，标签必须跟着走，否则「本周」的数字会被当成「今日」读。 */
const RANGE_LABELS = {today:'今日', week:'本周', month:'本月', custom:'自定义区间', all:'全部历史'};
function rangeLabel(){ return RANGE_LABELS[analyticsRange] || RANGE_LABELS.today; }

function renderAnalytics(data){
  if(!data || !data.summary) return;
  const sumWindow = data.summary.window || {};
  const sumAll = data.summary.all_time || {};
  const currentSum = sumWindow;

  // KPI 1: follows the range selector. It used to be hard-wired to the Today
  // bucket while the card next to it was hard-wired to All Time, so the range
  // buttons appeared to do nothing to the headline number.
  const elTodayTok = document.getElementById('kpiTodayTokens');
  const elTodayDetail = document.getElementById('kpiTodayTokensDetail');
  const elTodayTitle = document.getElementById('kpiRangeTokenTitle');
  if(elTodayTitle) elTodayTitle.textContent = rangeLabel() + '消耗 Token';
  if(elTodayTok) elTodayTok.textContent = fmtCompact(currentSum.total_tokens || 0);
  if(elTodayDetail) elTodayDetail.innerHTML =
    '<span style="color:var(--accent)">输入 ' + fmtCompact(currentSum.prompt_tokens || 0) + '</span>' +
    ' <span style="color:var(--dim-light)">/</span> ' + '<span style="color:var(--accent2)">输出 ' + fmtCompact(currentSum.completion_tokens || 0) + '</span>' +
    ' <span style="color:var(--dim-light)">/</span> ' + '<span style="color:var(--think)">思考 ' + fmtCompact(currentSum.reasoning_tokens || 0) + '</span>' +
    ' <span style="color:var(--dim-light)">/</span> ' + '<span style="color:var(--accent2)">缓存 ' + fmtCompact(currentSum.cached_tokens || 0) + '</span>';

  // KPI 2: always the all-time figure, as the stable reference next to KPI 1.
  const elTotalTok = document.getElementById('kpiTotalTokens');
  const elTotalDetail = document.getElementById('kpiTotalTokensDetail');
  if(elTotalTok) elTotalTok.textContent = fmtCompact(sumAll.total_tokens || 0);
  if(elTotalDetail) elTotalDetail.innerHTML =
    '<span style="color:var(--accent)">输入 ' + fmtCompact(sumAll.prompt_tokens || 0) + '</span>' +
    ' <span style="color:var(--dim-light)">/</span> ' + '<span style="color:var(--accent2)">输出 ' + fmtCompact(sumAll.completion_tokens || 0) + '</span>' +
    ' <span style="color:var(--dim-light)">/</span> ' + '<span style="color:var(--think)">思考 ' + fmtCompact(sumAll.reasoning_tokens || 0) + '</span>' +
    ' <span style="color:var(--dim-light)">/</span> ' + '<span style="color:var(--accent2)">缓存 ' + fmtCompact(sumAll.cached_tokens || 0) + '</span>' +
    // 只有「所选范围已经覆盖了日志里的全部请求」这一种情况需要说明：两张卡数字
    // 相同不是计数器坏了，而是没有更早的数据。
    ((analyticsRange !== 'all' && (sumAll.total_tokens || 0) === (sumWindow.total_tokens || 0) && (sumAll.total_tokens || 0) > 0)
      ? '<br><span style="color:var(--dim);font-size:11px">与' + rangeLabel() + '相同：所选范围已覆盖日志中的全部请求</span>'
      : '');

  // KPI 3: 网关平均生成速度（耗时作为副文案，速度与耗时是一对）
  const elAvgSpeed = document.getElementById('kpiAvgSpeed');
  const elAvgElapsed = document.getElementById('kpiAvgElapsed');
  if(elAvgSpeed) elAvgSpeed.innerHTML = (currentSum.speed_avg ? currentSum.speed_avg.toFixed(1) : '0.0') + ' <span style="font-size:12px;font-weight:400;color:var(--dim)">tok/s</span>';
  if(elAvgElapsed) elAvgElapsed.textContent = '平均耗时 ' + ms(currentSum.elapsed_ms_avg);

  // KPI 4: 平均首字延迟，单独一张卡
  const elAvgLatency = document.getElementById('kpiAvgLatency');
  if(elAvgLatency) elAvgLatency.textContent = ms(currentSum.ttft_ms_avg);

  // KPI 5: 缓存命中率
  const elCacheRate = document.getElementById('kpiCacheRate');
  if(elCacheRate) elCacheRate.textContent = (currentSum.cache_hit_pct != null ? currentSum.cache_hit_pct.toFixed(1) : '0.0') + '%';
  const elCacheDetail = document.getElementById('kpiCacheDetail');
  if(elCacheDetail) elCacheDetail.textContent = '缓存 ' + fmtCompact(currentSum.cached_tokens || 0) + ' tokens · 提示词前缀命中复用比';

  // KPI 6: 请求数（成功数）与 KPI 7: 成功率，拆成两张卡
  const elReqTitle = document.getElementById('kpiReqTitle');
  const elReqCount = document.getElementById('kpiReqCount');
  const elReqDetail = document.getElementById('kpiReqDetail');
  const elSuccessRate = document.getElementById('kpiSuccessRate');
  const elSuccessDetail = document.getElementById('kpiSuccessDetail');
  if(elReqTitle) elReqTitle.textContent = (analyticsRange === 'all' ? '累计' : rangeLabel()) + '请求数';
  const okReq = currentSum.requests || 0;
  const errReq = currentSum.errors || 0;
  const totalReq = okReq + errReq;
  const successPct = totalReq > 0 ? ((okReq / totalReq) * 100).toFixed(1) : '100.0';
  if(elReqCount) elReqCount.textContent = fmt(okReq) + ' 次';
  if(elReqDetail) elReqDetail.textContent = '含失败共 ' + fmt(totalReq) + ' 次';
  if(elSuccessRate) elSuccessRate.innerHTML = '<span style="color:' + (errReq ? 'var(--warn)' : 'var(--accent2)') + '">' + successPct + '%</span>';
  if(elSuccessDetail) elSuccessDetail.textContent = '失败 ' + fmt(errReq) + ' 次';

  // KPI 8: API 等价花费（跟随范围切换；与网关页一致，人民币与美元同时给出）
  if(data.usd_cny) costUsdRate = Number(data.usd_cny) || costUsdRate;
  const elCostRange = document.getElementById('kpiCostRange');
  const elCostValue = document.getElementById('kpiCostValue');
  const elCostDetail = document.getElementById('kpiCostDetail');
  if(elCostRange) elCostRange.textContent = '(' + rangeLabel() + ')';
  // 关闭时卡片留在原地（观感不变），数值显示「—」：后端根本没有折算金额，
  // 这里不能把「没有数」渲染成 ￥0.000。
  if(elCostValue) elCostValue.innerHTML = PRICING_ON
    ? fmtCostBoth(currentSum.cost_cny != null ? currentSum.cost_cny : 0, 14) : '—';
  if(elCostDetail) elCostDetail.innerHTML = (PRICING_ON && analyticsRange !== 'all' && sumAll.cost_cny != null)
    ? '总计（全部历史）' + fmtCostBoth(sumAll.cost_cny, 11) + '<br>按 OpenRouter 模型价折算'
    : '按 OpenRouter 模型价折算的等价 token 花费';

  // Table 1: 账号透视表
  const acctTbody = document.getElementById('analyticsAccountTbody');
  const acctCountEm = document.getElementById('analyticsAcctCount');
  let accts = (data.accounts || []).filter(a => {
    if(a.uid === '(unattributed)' && !((a.window || {}).requests || a.all_time.requests)) return false;
    return true;
  });
  if(acctCountEm) acctCountEm.textContent = accts.length ? '(' + accts.length + ' 个账号)' : '';

  if(acctTbody){
    if(!accts.length){
      acctTbody.innerHTML = '<tr><td colspan="8" class="empty">暂无账号用量数据</td></tr>';
    } else {
      acctTbody.innerHTML = accts.map(a => {
        // window 桶就是当前选中的范围，也就是这张表要展示的口径。
        const st = a.window || {};
        const modelsMap = a.window_models || {};
        const entries = Object.entries(modelsMap || {}).sort((x, y) => (y[1].tokens || 0) - (x[1].tokens || 0));
        
        let pills = '<span style="color:var(--dim)">无调用</span>';
        if(entries.length){
          pills = entries.map(([mid, ms]) => {
            return '<span class="model-pill">'
              + '<b style="color:var(--fg)">' + esc(mid) + '</b>: '
              + ms.requests + '次 · <b style="color:var(--accent)">' + fmtCompact(ms.tokens) + '</b> tok'
              + '</span>';
          }).join('');
        }

        const realmBadge = a.realm === 'cn'
          ? '<span class="badge warn" style="font-size:10px">国内版</span>'
          : '<span class="badge s" style="font-size:10px">国际版</span>';

        let creditStr = '<span style="color:var(--dim)">—</span>';
        if(a.credits && a.credits.remain != null){
          if(a.credits.size){
            creditStr = '<b style="color:var(--accent2)">' + fmt(a.credits.remain) + '</b> <span style="color:var(--dim);font-size:11px">/ ' + fmt(a.credits.size) + ' 积分</span>';
          } else {
            creditStr = '<b style="color:var(--accent2)">' + fmt(a.credits.remain) + '</b> <span style="color:var(--dim);font-size:11px">积分</span>';
          }
        }

        return '<tr>'
          + '<td style="text-align:left">'
          +   '<b>' + esc(a.nickname || a.uid) + '</b>'
          +   '<div style="color:var(--dim);font-size:11px;font-family:monospace">' + esc((a.uid || '').slice(0, 10)) + '...</div>'
          + '</td>'
          + '<td>' + realmBadge + '</td>'
          + '<td>' + creditStr + '</td>'
          + '<td>' + fmt(st.requests || 0) + '</td>'
          + '<td><b style="color:var(--accent)">' + fmtCompact(st.total_tokens || 0) + '</b></td>'
          + '<td><b style="color:var(--accent2)">' + (st.credit != null ? Number(st.credit).toFixed(2) : '0.00') + '</b></td>'
          + '<td style="text-align:left;max-width:440px">' + pills + '</td>'
          + '<td class="pricing-col" style="color:var(--warn)" title="OpenRouter 价估算：按 OpenRouter 公布的模型价折算，覆盖该账号在所选区间内的请求；按条件定价的模型（输入长度阈值或时段）按每条请求取档；单条请求实际用的策略、三档单价与匹配来源见「最近请求」里那条的悬停提示">'
          +   (st.cost_cny != null && st.cost_cny > 0 ? fmtCost(st.cost_cny) : '<span style="color:var(--dim)">—</span>')
          + '</td>'
          + '</tr>';
      }).join('');
    }
    applyTableSort('analyticsAccountTable');
  }

  renderKeyTable(data);
}

/* 按 API Key 的归属表。口径与账号表完全一致（window 桶 = 当前选中范围），
   所以两张表的请求数应当相等；对不上是数据问题，不是渲染能修的，故这里
   不隐藏任何会兜差额的行——(未知 key) 正是用来兜住差额的。唯一能收起的是
   「(切换前)」：它是升级前的历史尾巴，只减不增，收起与否都不影响对账。 */

/* 「(切换前)」行开关的状态。表格每 5 秒重绘一次，所以状态不能存在渲染函数
   里，得放在外面；偏好与账号区折叠一样存服务端的 accounts/settings.json
   （key_before_hidden），不用 localStorage。 */
let keyBeforeHidden = false;
let lastKeyData = null;
/* 唯一的写入口：内存状态、复选框与表格一起改，三者不会不同步。 */
function applyKeyBeforeHidden(hidden){
  keyBeforeHidden = hidden === true;
  const box = document.getElementById('keyBeforeToggle');
  if(box) box.checked = keyBeforeHidden;
  if(lastKeyData) renderKeyTable(lastKeyData);
  return keyBeforeHidden;
}
// 显式选择走既有的 settings save 通道（一个键的 patch，不动别的设置）。
async function onKeyBeforeToggle(box){
  const next = !!(box && box.checked);
  applyKeyBeforeHidden(next);
  try {
    await postJSON('/settings/save', {key_before_hidden: next});
  } catch(e) {
    toast('「(切换前)」行开关保存失败: ' + e.message, 'bad');
  }
}
function keyRealmCell(k){
  if(k.source === 'bucket') return '<span style="color:var(--dim)">—</span>';
  if(k.realm === 'cn') return '<span class="badge warn" style="font-size:10px">国内版</span>';
  if(k.realm === 'intl') return '<span class="badge s" style="font-size:10px">国际版</span>';
  // realm 为空的 Key 不绑出口，而是跟着模型走：它可能两个出口都用过，
  // 那时代理返回的积分是两个价格体系相加的结果，必须说出来。
  if(k.cross_realm) return '<span class="badge warn" style="font-size:10px">跟随 · 混合</span>';
  return '<span class="badge off" style="font-size:10px">跟随</span>';
}

function keyModelPills(k){
  const pills = (k.models || []).map(m =>
    '<span class="model-pill">'
    + '<b style="color:var(--fg)">' + esc(m.model) + '</b>: '
    + m.requests + '次 · <b style="color:var(--accent)">' + fmtCompact(m.tokens) + '</b> tok'
    + '</span>');
  const rest = k.models_other;
  if(rest){
    pills.push('<span class="model-pill">'
      + '<b style="color:var(--dim)">' + esc(rest.model || '(其他)') + '</b> '
      + '<span style="color:var(--dim)">' + (rest.models || 0) + ' 个模型 · </span>'
      + rest.requests + '次 · ' + fmtCompact(rest.tokens) + ' tok</span>');
  }
  return pills.length ? pills.join('') : '<span style="color:var(--dim)">无调用</span>';
}

function renderKeyTable(data){
  const tbody = document.getElementById('analyticsKeyTbody');
  if(!tbody) return;
  const countEm = document.getElementById('analyticsKeyCount');
  const note = document.getElementById('analyticsKeyNote');
  const keys = data.keys || [];
  lastKeyData = data;
  const wrap = document.getElementById('keyBeforeSwitch');
  const box = document.getElementById('keyBeforeToggle');
  const hasBefore = keys.some(k => k.key === '__before_keys__');
  // 新装的环境没有这一行，就没得收，开关也不该出现。
  if(wrap) wrap.style.display = hasBefore ? '' : 'none';
  if(box) box.checked = keyBeforeHidden;
  const shown = keyBeforeHidden ? keys.filter(k => k.key !== '__before_keys__') : keys;
  if(countEm) countEm.textContent = shown.length ? '(' + shown.length + ' 把)' : '';

  if(note){
    const named = keys.filter(k => k.source !== 'bucket');
    const cross = named.filter(k => k.cross_realm).length;
    const parts = [];
    if(!named.length) parts.push('设置里还没有任何 API Key，全部流量都落在下面的未归属行里。');
    if(cross) parts.push('有 ' + cross + ' 把 Key 没有绑定出口，它的积分由国际版与国内版两套价格相加而来，只能当量级看。');
    // 收起那行时就不再解释那行，否则读者找不到话里指的东西。
    if(!keyBeforeHidden) parts.push('Key 维度只从本次升级之后开始记录：更早的请求没有这个字段，会一直留在「(切换前)」里且不会再增长。');
    note.innerHTML = parts.join(' ');
  }

  if(!shown.length){
    tbody.innerHTML = '<tr><td colspan="8" class="empty">暂无 API Key 用量数据</td></tr>';
    return;
  }

  tbody.innerHTML = shown.map(k => {
    const st = k.window || {};
    const sub = {panel:'面板 Key', launcher:'启动参数', bucket:'未归属', builtin:'内置'}[k.source] || '';
    let nameCell = '<td style="text-align:left" data-label="API Key">'
      + '<b>' + esc(k.name || k.key) + '</b>';
    if(sub || !k.enabled){
      nameCell += '<div style="font-size:11px">'
        + (sub ? '<span style="color:var(--dim)">' + esc(sub) + '</span>' : '')
        + (!k.enabled && k.source === 'panel' ? '<span class="badge off mini">已禁用</span>' : '')
        + '</div>';
    }
    nameCell += '</td>';

    const reqCell = '<td data-label="请求次数">' + fmt(st.requests || 0) + ' 次'
      + (st.errors ? '<span style="color:var(--bad);font-size:11px"> · 失败 ' + fmt(st.errors) + '</span>' : '')
      + '</td>';
    const tokCell = '<td data-label="消耗总 Token"><b style="color:var(--accent)">'
      + fmtCompact(st.total_tokens || 0) + '</b></td>';
    const splitCell = '<td data-label="输入 · 输出 · 思考" style="font-size:12px">'
      + '<span style="color:var(--accent)">' + fmtCompact(st.prompt_tokens || 0) + '</span>'
      + ' <span style="color:var(--dim-light)">/</span> '
      + '<span style="color:var(--accent2)">' + fmtCompact(st.completion_tokens || 0) + '</span>'
      + ' <span style="color:var(--dim-light)">/</span> '
      + '<span style="color:var(--think)">' + fmtCompact(st.reasoning_tokens || 0) + '</span>'
      + '</td>';
    const cacheCell = '<td data-label="缓存命中">'
      + (st.cache_hit_pct != null ? st.cache_hit_pct.toFixed(1) : '0.0') + '%</td>';
    const creditCell = '<td data-label="消耗积分"><b style="color:var(--accent2)">'
      + (st.credit != null ? Number(st.credit).toFixed(2) : '0.00') + '</b></td>';
    const modelCell = '<td style="text-align:left;max-width:440px" data-label="调用的模型分布">'
      + keyModelPills(k) + '</td>';

    return '<tr>'
      + nameCell
      + '<td data-label="出口">' + keyRealmCell(k) + '</td>'
      + reqCell + tokCell + splitCell + cacheCell + creditCell + modelCell
      + '</tr>';
  }).join('');
  applyTableSort('analyticsKeyTable');
}
