/* ---- 智能体一键配置模块 ----
   后端接口（wb_agents.py / wb_proxy.py）：
     GET  /agents          → {clients:[...], keys:[...], models:[...], gateway:{base_url}}
     POST /agents/apply    {client, client_id, base_url, key_id, model, models} → {client, label, applied_at, base_url, model, files, warnings}
     POST /agents/restore  {client, client_id}                                 → {client, label, restored, deleted}
   每个 client: {id, label, desc, protocol('anthropic'|'openai'), config_paths:[...],
                 installed, configured, applied:{at, base_url, model, files, external_change}}
   这里只用 getJSON/postJSON/toast/esc，接口字段缺省时尽量降级展示而不是报错。 */
let AGENTS_DATA = null;
let AGENT_MODELS = [];
let AGENT_KEYS = [];
let AGENTS_LOADING = false;

function agentClients(){
  const d = AGENTS_DATA || {};
  return d.clients || d.agents || [];
}

async function loadAgents(showToast){
  if(AGENTS_LOADING) return;
  AGENTS_LOADING = true;
  const state = document.getElementById('agentDetectState');
  if(state){ state.textContent = '(检测中...)'; state.style.color = 'var(--dim)'; }
  try {
    const data = await getJSON('/agents');
    AGENTS_DATA = data || {};
    AGENT_MODELS = AGENTS_DATA.models || [];
    AGENT_KEYS = AGENTS_DATA.keys || AGENTS_DATA.api_keys || [];
    renderAgentConnForm();
    renderAgentCards();
    renderAgentManualUrls();
    const n = agentClients().filter(c => c.installed || (c.status && c.status !== 'missing')).length;
    if(state){
      // 计数必须按卡片自己的判定来数（agentStatusBadge 认 installed / detected），
      // 否则会出现「5 张卡片、标题 4 个」的错位（issue #250）：卡片列表是全部
      // 支持的客户端，检测到的只是其中一部分，两个数字都写出来。
      const clients = agentClients();
      const found = clients.filter(c => c.installed || c.detected).length;
      state.textContent = clients.length > found
        ? '(已检测到 ' + found + ' / 共 ' + clients.length + ' 个)'
        : '(已检测到 ' + found + ' 个)';
      state.style.color = found ? 'var(--accent2)' : 'var(--dim)';
    }
    if(showToast) toast('客户端检测完成', 'ok');
  } catch(e) {
    if(state){ state.textContent = '(加载失败)'; state.style.color = 'var(--bad)'; }
    if(!isAuthError(e)) toast('智能体列表加载失败: ' + e.message, 'bad');
  } finally {
    AGENTS_LOADING = false;
  }
}

function renderAgentConnForm(){
  // 网关地址：只在用户还没改过时填默认值，避免刷新把用户编辑冲掉。
  const baseInput = document.getElementById('agentBaseUrl');
  if(baseInput && !baseInput.dataset.touched){
    const def = (AGENTS_DATA && AGENTS_DATA.gateway && AGENTS_DATA.gateway.base_url)
      || (location.origin + '/v1');
    baseInput.value = def;
  }
  const keySel = document.getElementById('agentKeySelect');
  if(keySel){
    const prev = keySel.value;
    let html = '<option value="">全局 API Key</option>';
    AGENT_KEYS.forEach(k => {
      const id = k.id != null ? String(k.id) : '';
      const name = k.name || id || 'Key';
      html += '<option value="' + esc(id) + '"' + (k.enabled === false ? ' disabled' : '')
        + '>' + esc(name) + (k.realm ? ' (' + esc(k.realm) + ')' : '') + '</option>';
    });
    keySel.innerHTML = html;
    if(prev && keySel.querySelector('option[value="' + prev.replace(/"/g, '\\"') + '"]')) keySel.value = prev;
  }
  const modelSel = document.getElementById('agentModelSelect');
  if(modelSel){
    const prev = modelSel.value;
    let html = '<option value="">(不指定模型)</option>';
    AGENT_MODELS.forEach(m => {
      const id = String((typeof m === 'string' ? m : (m.id || m.model || '')) || '');
      if(!id) return;
      // 只列「网关模型清单」里勾选的模型：一键配置只写这份清单，下拉里出现
      // 没勾选的模型会让用户以为它也会被写进去。
      if(!isModelPicked(id)) return;
      const cw = (typeof m === 'object' && m) ? (m.context_window || m.contextWindow || 0) : 0;
      const label = id + (cw ? ' (' + fmt(cw) + ' ctx)' : '');
      html += '<option value="' + esc(id) + '">' + esc(label) + '</option>';
    });
    modelSel.innerHTML = html;
    if(prev && modelSel.querySelector('option[value="' + prev.replace(/"/g, '\\"') + '"]')) modelSel.value = prev;
  }
}

function renderAgentManualUrls(){
  const origin = location.origin;
  const openAIEl = document.getElementById('agentOpenAIUrl');
  const anthEl = document.getElementById('agentAnthropicUrl');
  if(openAIEl) openAIEl.textContent = origin + '/v1';
  if(anthEl) anthEl.textContent = origin; // Anthropic 客户端的 Base URL 不带 /v1 前缀
}

async function copyAgentUrl(elId, btn){
  const el = document.getElementById(elId);
  const text = el ? el.textContent : '';
  if(!text){ toast('没有可复制的内容', 'warn'); return; }
  try {
    await writeClipboard(text);
    if(btn){
      const old = btn.textContent;
      btn.textContent = '已复制';
      setTimeout(() => { btn.textContent = old; }, 1600);
    }
    toast('已复制到剪贴板', 'ok');
  } catch(e) {
    toast('复制失败: ' + e.message, 'bad');
  }
}

function agentProtocolLabel(p){
  return p === 'anthropic' ? 'Anthropic Messages' : 'OpenAI 兼容';
}

function agentStatusBadge(c){
  if(c.applied) return '<span class="badge ok">已配置</span>';
  if(c.installed || c.detected) return '<span class="badge s">已检测到</span>';
  return '<span class="badge off">未检测到</span>';
}

function renderAgentCards(){
  const grid = document.getElementById('agentCardGrid');
  if(!grid) return;
  const clients = agentClients();
  if(!clients.length){
    grid.innerHTML = '<div style="font-size:13px;color:var(--dim);padding:18px;border:1px dashed var(--line);border-radius:10px">'
      + '暂未获取到客户端列表，点右上角「刷新」重新检测。' + '</div>';
    return;
  }
  const cards = clients.map(c => {
    const id = c.id || '';
    const idAttr = esc(id);
    const name = esc(c.label || c.name || id);
    const desc = c.desc ? '<div style="font-size:12px;color:var(--dim);margin-top:4px">' + esc(c.desc) + '</div>' : '';
    const paths = (c.config_paths || (c.config_path ? [c.config_path] : []));
    const pathsHtml = paths.length
      ? '<div class="mono" style="font-size:11px;color:var(--dim);margin-top:8px;word-break:break-all">'
        + paths.map(p => esc(p)).join('<br>') + '</div>'
      : '';
    const applied = c.applied || null;
    let appliedHtml = '';
    let warnHtml = '';
    if(applied){
      let at = '-';
      if(applied.at){
        if(typeof applied.at === 'number'){
          const d = new Date(applied.at > 1e11 ? applied.at : applied.at * 1000);
          at = isNaN(d.getTime()) ? String(applied.at) : (d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0') + ' ' + String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0') + ':' + String(d.getSeconds()).padStart(2,'0'));
        } else {
          at = esc(String(applied.at).replace('T', ' ').slice(0, 19));
        }
      }
      appliedHtml = '<div style="font-size:11px;color:var(--dim);margin-top:8px;line-height:1.7">'
        + '<div>应用时间: <span class="mono">' + at + '</span></div>'
        + '<div>base_url: <span class="mono">' + esc(applied.base_url || '-') + '</span></div>'
        + '<div>模型: <span class="mono">' + esc(applied.model || '(未指定)') + '</span></div>'
        + '</div>';
      if(applied.external_change){
        warnHtml = '<div class="badge warn" style="margin-top:8px" title="配置文件在应用之后被外部修改过">'
          + '检测到外部修改：还原将覆盖外部改动</div>';
      }
    }
    const applyLabel = applied ? '重新应用' : '一键配置';
    const notDetected = !(c.installed || c.detected);
    const btns = '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px">'
      + '<button class="mini" onclick="applyAgent(&quot;' + idAttr + '&quot;, this)"' + (notDetected ? ' disabled title="未检测到该客户端"' : '') + '>' + applyLabel + '</button>'
      + (applied ? '<button class="danger mini" onclick="restoreAgent(&quot;' + idAttr + '&quot;, this)">还原原始配置</button>' : '')
      + '</div>';
    return '<div style="background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:14px 16px;display:flex;flex-direction:column;gap:6px">'
      + '<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap">'
      + '<div style="font-weight:600;color:var(--fg);font-size:14px">' + name + '</div>'
      + agentStatusBadge(c) + '</div>'
      + '<div style="font-size:11px;color:var(--accent)">' + agentProtocolLabel(c.protocol) + '</div>'
      + desc
      + pathsHtml
      + appliedHtml
      + warnHtml
      + btns
      + '</div>';
  });
  grid.innerHTML = cards.join('');
}

function agentFindClient(id){
  return agentClients().find(c => String(c.id) === String(id)) || null;
}

async function applyAgent(id, btn){
  const client = agentFindClient(id);
  if(!client){ toast('未找到客户端: ' + id, 'bad'); return; }
  const baseInput = document.getElementById('agentBaseUrl');
  const keySel = document.getElementById('agentKeySelect');
  const modelSel = document.getElementById('agentModelSelect');
  const baseUrl = baseInput ? baseInput.value.trim() : '';
  if(!baseUrl){ toast('请先填写网关地址', 'warn'); return; }
  // 只把「网关模型清单」里勾选的模型写进客户端（与 AGENT_MODELS 取交集，避免
  // 把不在当前模型目录里的 id 也发上去）。一个都没勾就先别发请求：后端对 DSH
  // 这类客户端收到空列表会返回 400，这里给一句更明确的下一步提示。
  const pickedModels = (AGENT_MODELS || [])
    .map(m => String((typeof m === 'object' ? (m.id || m.model || '') : m) || ''))
    .filter(id => id && isModelPicked(id));
  // 只有把模型清单写进自身配置的客户端（DSH / OpenCode / Crush / ZCode，
  // 后端 /agents 的 model_embedding_clients 声明）才需要「至少勾一个」；
  // Claude Code / Codex 不读这份清单，别因为没勾模型就把它们的一键配置也挡掉。
  const embeddingClients = (AGENTS_DATA && AGENTS_DATA.model_embedding_clients) || [];
  const embedsModels = embeddingClients.map(String).indexOf(String(id)) !== -1;
  if(!pickedModels.length && embedsModels){
    toast('请先在「网关模型清单」勾选至少一个模型', 'warn');
    return;
  }
  if(client.applied && client.applied.external_change){
    if(!confirm('「' + (client.label || id) + '」的配置文件在应用后被外部修改过，重新应用将覆盖这些改动。确定继续吗？')) return;
  }
  if(btn){ btn.disabled = true; }
  try {
    const r = await postJSON('/agents/apply', {
      client: id,
      client_id: id,
      base_url: baseUrl,
      key_id: keySel ? keySel.value : '',
      model: modelSel ? modelSel.value : '',
      models: pickedModels
    });
    if(r && (r.ok === false || r.error)){
      toast('配置失败: ' + (r.message || (r.error && r.error.message) || '未知错误'), 'bad');
    } else {
      toast('已配置「' + (client.label || id) + '」，原配置已备份', 'ok');
    }
  } catch(e) {
    if(!isAuthError(e)) toast('配置失败: ' + e.message, 'bad');
  } finally {
    if(btn){ btn.disabled = false; }
    await loadAgents();
  }
}

async function restoreAgent(id, btn){
  const client = agentFindClient(id);
  const label = (client && (client.label || client.name)) || id;
  if(!confirm('确定还原「' + label + '」的原始配置吗？将恢复成一键配置之前的备份文件。')) return;
  if(btn){ btn.disabled = true; }
  try {
    const r = await postJSON('/agents/restore', {client: id, client_id: id});
    if(r && (r.ok === false || r.error)){
      toast('还原失败: ' + (r.message || (r.error && r.error.message) || '未知错误'), 'bad');
    } else {
      toast('已还原「' + label + '」的原始配置', 'ok');
    }
  } catch(e) {
    if(!isAuthError(e)) toast('还原失败: ' + e.message, 'bad');
  } finally {
    if(btn){ btn.disabled = false; }
    await loadAgents();
  }
}

// 网关地址输入框：用户改过后打标，刷新检测不再覆盖。
(function(){
  const bind = () => {
    const baseInput = document.getElementById('agentBaseUrl');
    if(baseInput) baseInput.addEventListener('input', () => { baseInput.dataset.touched = '1'; });
  };
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind);
  else bind();
})();

window.loadAgents = loadAgents;
window.loadAgentModelSelection = loadAgentModelSelection;
window.renderAgentCards = renderAgentCards;
window.applyAgent = applyAgent;
window.restoreAgent = restoreAgent;
window.copyAgentUrl = copyAgentUrl;
