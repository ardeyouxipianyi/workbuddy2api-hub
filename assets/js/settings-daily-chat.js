// ---- 国际版每日活跃：候选模型序列 + 预算 -------------------------------------
// 旧的六个旋钮（最多尝试次数 / 失败重试 / 回退开关 / 回退组合 / 首轮等待 /
// 建会话退避）在代码里互相覆盖，面板上又看不出来，所以整块换成一个有序候选序列
// 加一个总预算：顺序即优先级，预算用完必给结论。
let DAILY_CHAT_MODEL_SELECTED = [];   // 面板上正在编辑的有序候选
let DAILY_CHAT_MODELS = null;         // /v1/models 的元数据缓存

async function loadDailyChatModels(){
  if(DAILY_CHAT_MODELS) return DAILY_CHAT_MODELS;
  try {
    const r = await getJSON('/v1/models?realm=intl');
    const out = [];
    for(const m of (r.data || [])){
      if(!m.id) continue;
      const credits = String(m.credits == null ? '' : m.credits).trim();
      const raw = credits.toLowerCase().replace(/^x/, '');
      const free = (raw === '0.00' || raw === '0');
      out.push({id: m.id, free: free, credits: credits});
    }
    // 免费的在前，其余在后，各自按 id 排序。
    out.sort((a, b) => (a.free === b.free ? a.id.localeCompare(b.id) : (a.free ? -1 : 1)));
    DAILY_CHAT_MODELS = out;
  } catch(e) {
    DAILY_CHAT_MODELS = [];
  }
  return DAILY_CHAT_MODELS;
}

async function renderDailyChatModelList(selected){
  DAILY_CHAT_MODEL_SELECTED = Array.isArray(selected) ? selected.slice() : [];
  const state = document.getElementById('setDailyChatModelsState');
  if(state){
    state.textContent = DAILY_CHAT_MODEL_SELECTED.length
      ? ('已指定 ' + DAILY_CHAT_MODEL_SELECTED.length + ' 个 · 按顺序试')
      : '(自动：首选 + 该区域免费模型)';
    state.style.color = DAILY_CHAT_MODEL_SELECTED.length ? 'var(--accent2)' : 'var(--dim)';
  }
  const box = document.getElementById('setDailyChatModelList');
  if(!box) return;
  if(!DAILY_CHAT_MODEL_SELECTED.length){
    box.innerHTML = '<div style="font-size:12px;color:var(--dim)">自动模式：只用首选模型，失败时按目录倍率挑下一个免费模型。</div>';
    return;
  }
  const models = await loadDailyChatModels();
  const byId = {};
  for(const m of models) byId[m.id] = m;
  box.innerHTML = DAILY_CHAT_MODEL_SELECTED.map(function(id, index){
    const meta = byId[id];
    const tag = meta ? (meta.free ? '免费' : (meta.credits || '付费')) : '不在目录里';
    return '<div style="display:flex;align-items:center;gap:8px;font-size:12px">'
      + '<span style="color:var(--dim);min-width:18px">' + (index + 1) + '.</span>'
      + '<span style="font-family:var(--mono,monospace)">' + esc(id) + '</span>'
      + '<span style="color:var(--dim-light)">' + esc(tag) + '</span>'
      + '<button class="sec mini" onclick="moveDailyChatModel(' + index + ',-1)">↑</button>'
      + '<button class="sec mini" onclick="moveDailyChatModel(' + index + ',1)">↓</button>'
      + '<button class="sec mini" onclick="removeDailyChatModel(' + index + ')">×</button>'
      + '</div>';
  }).join('');
}

function moveDailyChatModel(index, delta){
  const next = index + delta;
  if(index < 0 || index >= DAILY_CHAT_MODEL_SELECTED.length) return;
  if(next < 0 || next >= DAILY_CHAT_MODEL_SELECTED.length) return;
  const list = DAILY_CHAT_MODEL_SELECTED.slice();
  const item = list.splice(index, 1)[0];
  list.splice(next, 0, item);
  renderDailyChatModelList(list);
}
function removeDailyChatModel(index){
  const list = DAILY_CHAT_MODEL_SELECTED.slice();
  if(index < 0 || index >= list.length) return;
  list.splice(index, 1);
  renderDailyChatModelList(list);
}
async function toggleDailyChatModelPicker(){
  const el = document.getElementById('setDailyChatModelPicker');
  if(!el) return;
  const opening = el.style.display === 'none';
  el.style.display = opening ? 'block' : 'none';
  if(opening) await renderDailyChatModelPicker();
}
async function renderDailyChatModelPicker(){
  const list = document.getElementById('setDailyChatModelPickList');
  if(!list) return;
  const models = await loadDailyChatModels();
  list.innerHTML = (models || []).map(function(m){
    const checked = DAILY_CHAT_MODEL_SELECTED.indexOf(m.id) >= 0 ? ' checked' : '';
    const tag = m.free ? '免费' : (m.credits || '付费');
    return '<label class="fallback-pick-row" data-search="' + esc(m.id.toLowerCase()) + '" style="display:flex;align-items:center;gap:6px;font-size:12px;padding:3px 4px"><input type="checkbox" value="' + esc(m.id) + '"' + checked + ' onchange="toggleDailyChatModel(this)">' + esc(m.id) + ' <span style="color:var(--dim-light)">' + esc(tag) + '</span></label>';
  }).join('') || '<div style="font-size:12px;color:var(--dim);padding:4px">模型清单为空</div>';
}
function toggleDailyChatModel(el){
  const id = el.value;
  const list = DAILY_CHAT_MODEL_SELECTED.slice();
  const index = list.indexOf(id);
  if(el.checked && index < 0) list.push(id);
  if(!el.checked && index >= 0) list.splice(index, 1);
  renderDailyChatModelList(list);
}
function setDailyChatModelAll(on){
  const box = document.getElementById('setDailyChatModelPickList');
  if(!box) return;
  const ids = Array.from(box.querySelectorAll('input[type=checkbox]')).map(function(el){ return el.value; });
  if(!on){ renderDailyChatModelList([]); renderDailyChatModelPicker(); return; }
  const merged = DAILY_CHAT_MODEL_SELECTED.slice();
  for(const id of ids) if(merged.indexOf(id) < 0) merged.push(id);
  renderDailyChatModelList(merged);
  renderDailyChatModelPicker();
}
function filterDailyChatModelPicker(){
  const input = document.getElementById('setDailyChatModelSearch');
  const box = document.getElementById('setDailyChatModelPickList');
  if(!box) return;
  const q = String((input && input.value) || '').trim().toLowerCase();
  box.querySelectorAll('.fallback-pick-row').forEach(function(el){
    el.style.display = !q || (el.getAttribute('data-search') || '').indexOf(q) >= 0 ? 'flex' : 'none';
  });
}
async function resetDailyChatModels(){
  renderDailyChatModelList([]);
  await renderDailyChatModelPicker();
}
function resetDailyChatTimes(){
  renderDailyChatTimes([]);
}

async function saveDailyChatModule(btn){
  const webEl = document.getElementById('setDailyChatWeb');
  const budgetEl = document.getElementById('setDailyChatBudget');
  const turnEl = document.getElementById('setDailyChatTurnTimeout');
  const concEl = document.getElementById('setDailyChatWebConcurrency');
  const backoffEl = document.getElementById('setDailyChatWebRetryBackoff');
  const catchupEl = document.getElementById('setDailyChatStartupCatchup');
  const fallbackEl = document.getElementById('setDailyChatActivityFallback');
  const payload = {
    daily_chat_web: !!(webEl && webEl.checked),
    daily_chat_models: DAILY_CHAT_MODEL_SELECTED.slice(),
    daily_chat_budget: Number((budgetEl && budgetEl.value) || 0),
    daily_chat_web_timeout: Number((turnEl && turnEl.value) || 120),
    daily_chat_web_concurrency: Number((concEl && concEl.value) || 1),
    daily_chat_web_retry_backoff: Number((backoffEl && backoffEl.value) || 0),
    daily_chat_startup_catchup: !!(catchupEl && catchupEl.checked),
    daily_chat_web_fallback_activity: !!(fallbackEl && fallbackEl.checked),
    schedule: {daily_chat: DAILY_CHAT_TIMES_SELECTED.slice()}
  };
  if(btn) btn.disabled = true;
  const state = document.getElementById('setDailyChatSaveState');
  try {
    await postJSON('/settings/save', payload);
    if(state){ state.textContent = '已保存'; state.style.color = 'var(--accent2)'; }
    toast('每日活跃设置已保存', 'ok');
    loadSettings();
  } catch(e) {
    if(state){ state.textContent = ''; }
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

async function resetDailyChatModule(btn){
  const budgetEl = document.getElementById('setDailyChatBudget');
  const turnEl = document.getElementById('setDailyChatTurnTimeout');
  const concEl = document.getElementById('setDailyChatWebConcurrency');
  const backoffEl = document.getElementById('setDailyChatWebRetryBackoff');
  const catchupEl = document.getElementById('setDailyChatStartupCatchup');
  const fallbackEl = document.getElementById('setDailyChatActivityFallback');
  if(budgetEl) budgetEl.value = '600';
  if(turnEl) turnEl.value = '120';
  if(concEl) concEl.value = '1';
  if(backoffEl) backoffEl.value = '5';
  if(catchupEl) catchupEl.checked = true;
  if(fallbackEl) fallbackEl.checked = false;
  renderDailyChatModelList([]);
  renderDailyChatTimes([]);
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {
      daily_chat_models: [],
      daily_chat_budget: 600,
      daily_chat_web_timeout: 120,
      daily_chat_web_concurrency: 1,
      daily_chat_web_retry_backoff: 5,
      daily_chat_startup_catchup: true,
      daily_chat_web_fallback_activity: false,
      schedule: {daily_chat: []}
    });
    toast('已恢复推荐值：随机两个时段、自动候选、预算 600 秒', 'ok');
    loadSettings();
  } catch(e) {
    toast('恢复失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

async function saveGlobalPrompt(btn){
  const modeEl = document.getElementById('setGlobalPromptMode');
  const textEl = document.getElementById('setGlobalPromptText');
  const mode = (modeEl && modeEl.value) || 'passthrough';
  const text = (textEl && textEl.value) || '';
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {prompt: {mode: mode, text: text}});
    toast(mode === 'passthrough' ? '已保存：全局提示词透传'
        : ('已保存：全局提示词' + (mode === 'append' ? '追加到 system' : '替换 system')), 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

let DAILY_CHAT_TIMES_SELECTED = [];
function renderDailyChatTimes(times){
  DAILY_CHAT_TIMES_SELECTED = Array.isArray(times) ? times.slice().sort() : [];
  const list = document.getElementById('setDailyChatTimesList');
  if(list){
    list.innerHTML = DAILY_CHAT_TIMES_SELECTED.map(function(t){
      return '<span class="badge s" style="font-size:12px;padding:4px 8px;display:inline-flex;align-items:center;gap:6px">' + esc(t) + '<button class="key-action-btn" style="padding:0 4px" data-time="' + esc(t) + '" onclick="removeDailyChatTime(this)">×</button></span>';
    }).join('') || '<span style="font-size:12px;color:var(--dim)">未设置：每天随机两个时间（间隔 12 小时）</span>';
  }
  const state = document.getElementById('setDailyChatTimesState');
  if(state){
    state.textContent = DAILY_CHAT_TIMES_SELECTED.length
      ? ('独立时段 · ' + DAILY_CHAT_TIMES_SELECTED.join(' / '))
      : '每天随机两个时间（间隔 12 小时）';
    state.style.color = DAILY_CHAT_TIMES_SELECTED.length
      ? 'var(--accent2)' : 'var(--dim)';
  }
}
function addDailyChatTime(){
  const input = document.getElementById('setDailyChatTimeInput');
  const value = String((input && input.value) || '').trim();
  if(!value){ toast('请先选择时间', 'warn'); return; }
  if(DAILY_CHAT_TIMES_SELECTED.indexOf(value) < 0){
    DAILY_CHAT_TIMES_SELECTED.push(value);
    DAILY_CHAT_TIMES_SELECTED.sort();
  }
  renderDailyChatTimes(DAILY_CHAT_TIMES_SELECTED);
  if(input) input.value = '';
}
function removeDailyChatTime(el){
  const value = (el && el.getAttribute) ? el.getAttribute('data-time') : '';
  DAILY_CHAT_TIMES_SELECTED = DAILY_CHAT_TIMES_SELECTED.filter(function(t){ return t !== value; });
  renderDailyChatTimes(DAILY_CHAT_TIMES_SELECTED);
}
async function saveDailyChatTimes(btn){
  const times = DAILY_CHAT_TIMES_SELECTED.slice();
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {schedule: {daily_chat: times}});
    toast(times.length ? ('已保存每日活跃时段：' + times.join(' / '))
                       : '已清空：每日活跃改为每天随机两个时间', 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

async function saveLocalWebTools(btn){
  const el = document.getElementById('setLocalWebTools');
  const value = !!(el && el.checked);
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {local_web_tools: value});
    toast(value ? '已启用：由网关代跑 web_search / web_fetch' : '已关闭：工具声明原样透传', 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

async function saveRemainingPriority(btn){
  const el = document.getElementById('setRemainingPriority');
  const value = !!(el && el.checked);
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {remaining_priority_enabled: value});
    toast(value ? '已启用：剩余用量接近耗尽的组合优先调度' : '已关闭剩余用量优先调度', 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

async function saveRealmFallback(btn){
  const el = document.getElementById('setRealmFallback');
  const value = !!(el && el.checked);
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {realm_fallback_enabled: value});
    toast(value ? '已启用：国际版限流时回退到国内版' : '已关闭跨出口回退', 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

async function saveWindowProbe(btn){
  const el = document.getElementById('setWindowProbe');
  const value = !!(el && el.checked);
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {window_probe: value});
    toast(value ? '已启用：额度窗口到点自动请求' : '已关闭额度窗口探针', 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

async function saveAffinityStableKey(btn){
  const el = document.getElementById('setAffinityStableKey');
  const value = !!(el && el.checked);
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {affinity_stable_key_enabled: value});
    toast(value ? '已启用：会话键包含首条回复' : '已关闭：会话键只用前两条消息', 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

/* 账号级并发上限：0 = 不限，1..10 = 每账号同时几个。后端只接受整数，
   空值/小数/非数字就地拦下，不送上去换 400。 */
async function saveAccountConcurrent(btn){
  const el = document.getElementById('setAccountConcurrent');
  const raw = (el && el.value !== '') ? Number(el.value) : NaN;
  const value = Math.floor(raw);
  if(!isFinite(value) || value < 0 || value > 10){
    toast('请输入 0 到 10 的整数（0 = 不限）', 'bad');
    return;
  }
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {account_max_concurrent: value});
    toast(value > 0 ? ('已保存：每个账号同时最多 ' + value + ' 个请求') : '已保存：账号并发不限', 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

async function saveEgressBackoff(btn){
  const el = document.getElementById('setEgressBackoff');
  const value = !!(el && el.checked);
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {egress_backoff_enabled: value});
    toast(value ? '已启用：出口级瞬时失败退避' : '已关闭出口级瞬时失败退避', 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

/* 用量聚合保留窗口（空间换时间）。后端只接受 >=1 的整数，且各有上限，所以
   先把输入收成整数再发；空值/小数/非数字就地拦下，不送上去换 400。 */
async function saveUsageKeep(btn){
  const hourEl = document.getElementById('setUsageHourKeep');
  const dayEl = document.getElementById('setUsageDayKeep');
  const hours = Math.floor(Number(hourEl && hourEl.value));
  const days = Math.floor(Number(dayEl && dayEl.value));
  if(!isFinite(hours) || hours < 1 || !isFinite(days) || days < 1){
    toast('请输入不小于 1 的整数', 'bad');
    return;
  }
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {usage: {hour_keep_hours: hours, day_keep_days: days}});
    toast('已保存：小时层 ' + hours + ' 小时 / 日桶 ' + days + ' 天', 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}
/* 上游响应头超时。后端只接受 >=1 的整数秒，所以先把输入收成整数再发：空值、
   小数、非数字就地拦下，而不是送上去换一个 400 回来。 */
async function saveHeaderTimeout(btn){
  const el = document.getElementById('setHeaderTimeout');
  const value = Math.floor(Number(el && el.value));
  if(!isFinite(value) || value < 1){
    toast('请输入不小于 1 的整数秒', 'bad');
    return;
  }
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {upstream: {header_timeout_seconds: value}});
    toast('已保存：上游响应头超时 ' + value + ' 秒', 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}
