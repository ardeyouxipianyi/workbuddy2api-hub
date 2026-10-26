/* ---- 「网关模型清单」勾选（一键配置用，全局一份） ----
   这张表每行前面的复选框只决定「一键配置」把哪些模型写进 DSH 等客户端，不改
   网关路由：未勾选的模型在网关侧照常可以调用。选择是全局的（不按版本视图划
   分），后端存一份 id 列表，见 /agents/model-selection。
   默认态是「全部勾选」：服务端还没存过时（configured=false）空集合表示
   「全都要」，所以升级后第一次打开面板看到的就是全勾，一键配置写出的清单与
   升级前逐字相同。只有用户改过（含表头的批量动作）才会落一份显式列表。 */
let AGENT_MODEL_SELECTION = new Set();
// 服务端是否存过勾选。false = 从未保存 = 视作全部勾选（与「明确全不选」区分开，
// 后端的 agent_models 也是这个三态：None / [] / 非空）。
let MODEL_SELECTION_CONFIGURED = false;
// 上一次确认写进服务端的那份选择：失败回滚与「没变化就不重发」都靠它。
// null = 还没保存过（与空集合语义不同）。
let MODEL_SELECTION_SAVED = null;
let MODEL_SELECTION_TIMER = null;
let MODEL_SELECTION_SAVING = false;
let MODEL_SELECTION_QUEUED = false;
let MODEL_SELECTION_LOADING = false;

function modelSelectionKey(list){
  return (list || []).map(String).sort().join('\n');
}
function currentModelSelection(){
  return Array.from(AGENT_MODEL_SELECTION);
}
// 某个模型当前算不算被勾选。所有「读勾选」的地方都走这里，别直接看集合，
// 否则「从未保存」的全选态会被读成「全都不要」。
function isModelPicked(id){
  return !MODEL_SELECTION_CONFIGURED || AGENT_MODEL_SELECTION.has(id);
}
// 批量动作与「把全选态展开成集合」用的全量 id：以表格里画出来的为准
// （清单会被排序/过滤），表格还没渲染时退回当前视图的目录。
function allModelPickIds(){
  const boxes = Array.from(document.querySelectorAll('#modelsTable input.model-pick'));
  if(boxes.length) return boxes.map(b => (b.dataset ? b.dataset.modelId : '')).filter(Boolean);
  return (MODELS_DATA || []).map(m => String((m && m.id) || '')).filter(Boolean);
}

// 计数挂在「网关模型清单」标题后面：共几个 + 已勾选几个。勾选数放在这里而
// 不是每行旁边，是因为「一个都没勾」在第一眼看不出来（所有框都是空的）。
function updateAvailModelCount(){
  const em = document.getElementById('availModelCount');
  if(!em) return;
  const total = MODELS_DATA.length;
  const picked = MODEL_SELECTION_CONFIGURED ? AGENT_MODEL_SELECTION.size : total;
  em.textContent = '(共 ' + total + ' 个) · 已选 ' + picked;
}

// 只改复选框的勾选状态与计数，不重建行——避免把滚动位置、排序和焦点抖掉。
// 选择在别处变化（GET 回来、POST 失败回滚、批量动作）时用这个同步。
function syncModelPickBoxes(){
  Array.from(document.querySelectorAll('#modelsTable input.model-pick')).forEach(box => {
    box.checked = isModelPicked(box.dataset.modelId);
  });
  updateAvailModelCount();
}

async function loadAgentModelSelection(){
  if(MODEL_SELECTION_LOADING) return;
  MODEL_SELECTION_LOADING = true;
  try{
    const r = await getJSON('/agents/model-selection');
    const list = (r && (r.selected_models || r.models)) || [];
    // configured=false 表示服务端还没存过：这时维持「全部勾选」的默认态，
    // 空集合在这里读作「全都要」。存过之后才按存的那份画。
    MODEL_SELECTION_CONFIGURED = !!(r && r.configured);
    AGENT_MODEL_SELECTION = new Set(MODEL_SELECTION_CONFIGURED ? list.map(String) : []);
    MODEL_SELECTION_SAVED = MODEL_SELECTION_CONFIGURED ? new Set(AGENT_MODEL_SELECTION) : null;
    syncModelPickBoxes();
  }catch(e){
    // 读不到就维持「全部勾选」的默认态（等价于升级前的行为：整份目录都写进客户端），
    // 不打断页面其它加载；授权失效由面板登录门统一处理，这里不重复弹提示。
    if(!isAuthError(e)) console.error('loadAgentModelSelection error:', e);
  }finally{
    MODEL_SELECTION_LOADING = false;
  }
}

// 勾选落库：本地立即生效，POST 防抖 400ms——连点几行只发一次整份列表。
// 选择没变化（比如勾上又取消）时不发请求。
function scheduleModelSelectionSave(){
  if(MODEL_SELECTION_TIMER) clearTimeout(MODEL_SELECTION_TIMER);
  MODEL_SELECTION_TIMER = setTimeout(saveModelSelection, 400);
}

async function saveModelSelection(){
  MODEL_SELECTION_TIMER = null;
  if(MODEL_SELECTION_SAVING){ MODEL_SELECTION_QUEUED = true; return; }
  const payload = currentModelSelection();
  const key = modelSelectionKey(payload);
  // 还没存过时（SAVED=null）不能拿空集合当"上次保存的结果"比，否则用户从全选态
  // 里取消一个模型，会被误判成"没变化"而永远发不出去。
  if(MODEL_SELECTION_SAVED && key === modelSelectionKey(Array.from(MODEL_SELECTION_SAVED))) return;
  MODEL_SELECTION_SAVING = true;
  try{
    const r = await postJSON('/agents/model-selection', {models: payload});
    MODEL_SELECTION_SAVED = new Set(payload);
    MODEL_SELECTION_CONFIGURED = true;
    // 服务端会清洗列表（去重 / 超长截断）。保存期间没人继续勾选时以返回值为准，
    // 让面板上的勾选与真正落盘的清单一致；有人在期间改过就保留本地状态，等
    // 下面 finally 里的补发把最新一份送上去。
    const saved = r && (r.selected_models || r.models);
    if(Array.isArray(saved) && modelSelectionKey(currentModelSelection()) === key){
      const normalized = new Set(saved.map(String));
      if(modelSelectionKey(Array.from(normalized)) !== key){
        AGENT_MODEL_SELECTION = normalized;
        MODEL_SELECTION_SAVED = new Set(normalized);
        syncModelPickBoxes();
        if(AGENT_MODELS.length) renderAgentConnForm();
      }
    }
  }catch(e){
    // 面板上的勾选必须和服务器保持一致：回滚到上次成功的快照并重画。智能体页
    // 的默认模型下拉是按勾选集合画的，回滚后也要跟着重画，否则它会继续列一个
    // 已经被取消勾选的模型。
    AGENT_MODEL_SELECTION = MODEL_SELECTION_SAVED ? new Set(MODEL_SELECTION_SAVED) : new Set();
    MODEL_SELECTION_CONFIGURED = !!MODEL_SELECTION_SAVED;
    syncModelPickBoxes();
    if(AGENT_MODELS.length) renderAgentConnForm();
    if(!isAuthError(e)) toast('模型勾选保存失败：' + e.message, 'bad');
  }finally{
    MODEL_SELECTION_SAVING = false;
    // 保存期间又有勾选：串行补发一次，别让旧响应把新的选择盖掉。
    if(MODEL_SELECTION_QUEUED){
      MODEL_SELECTION_QUEUED = false;
      scheduleModelSelectionSave();
    } else if(modelSelectionKey(currentModelSelection())
              !== modelSelectionKey(MODEL_SELECTION_SAVED ? Array.from(MODEL_SELECTION_SAVED) : [])){
      scheduleModelSelectionSave();
    }
  }
}

// 复选框走事件委托：表格每次重画都会换掉整批 input，逐行挂监听会跟着丢。
// 排序的点击委托只认表头（sortHeaderAt），勾选不会触发表格排序。
document.addEventListener('change', function(ev){
  const box = ev.target;
  if(!box || !box.classList || !box.classList.contains('model-pick')) return;
  const id = box.dataset ? box.dataset.modelId : '';
  if(!id) return;
  if(!MODEL_SELECTION_CONFIGURED){
    // 从「从未保存」的全选态改起：先把整份清单落成显式集合，再应用这一次改动。
    // 少了这一步，「取消一个模型」会被存成「只选这一个」的反面。
    AGENT_MODEL_SELECTION = new Set(allModelPickIds());
    MODEL_SELECTION_CONFIGURED = true;
  }
  if(box.checked) AGENT_MODEL_SELECTION.add(id);
  else AGENT_MODEL_SELECTION.delete(id);
  updateAvailModelCount();
  scheduleModelSelectionSave();
  // 智能体页的默认模型下拉只列已勾选的模型，选择变了就跟着重画；目录还没
  // 加载（没打开过智能体页）时由 loadAgents() 自己按当前选择画。
  if(AGENT_MODELS.length) renderAgentConnForm();
});

// 表头三个批量动作：全选 / 全不选 / 反选。
// 做成按钮而不是「反选」复选框：反选是动作、不是状态。复选框形态要么得额外记一份
// 「反选之前的集合」（否则两次开关回不到原状），要么让「界面显示的勾选」和
// 「实际保存的清单」分家——两者都是最难查的那类双状态 bug。
function applyModelPickAction(kind){
  const all = allModelPickIds();
  if(kind === 'all') AGENT_MODEL_SELECTION = new Set(all);
  else if(kind === 'none') AGENT_MODEL_SELECTION = new Set();
  else if(kind === 'invert') AGENT_MODEL_SELECTION = new Set(all.filter(id => !isModelPicked(id)));
  else return;
  MODEL_SELECTION_CONFIGURED = true;
  syncModelPickBoxes();
  scheduleModelSelectionSave();
  if(AGENT_MODELS.length) renderAgentConnForm();
}

// 清单来源横幅：把「通过 API 获取的最新数据」和「项目默认/缓存文件数据」在
// 视觉上分清楚——非 API 来源给黄色警示（模型、倍率数据可能不一致），API 来源
// 给绿色状态条并附刷新时间。数据由 /v1/models 响应的 catalog 字段驱动（见
// wb_proxy.catalog_meta）；旧版服务端没有该字段时如实提示。
const CATALOG_SOURCE_ORIGIN = {
  remote:   '上游 API · /v3/config',
  endpoint: '上游 API · /v1/models 端点',
  cache:    '本地配置文件（桌面客户端缓存）',
  legacy:   '本地配置文件（桌面客户端缓存）',
  bundled:  '内置快照'
};
const CATALOG_BANNER_BASE = 'font-size:12px;margin:0 0 12px;padding:7px 10px;border-radius:6px;'
  + 'display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;line-height:1.5;';

function fmtCatalogStamp(sec){
  const n = Number(sec);
  if(!n || n <= 0) return '';
  const d = new Date(n * 1000);
  if(isNaN(d.getTime())) return '';
  const p = v => String(v).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
    + ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

function renderModelsSourceLine(meta){
  const el = document.getElementById('modelsSourceLine');
  if(!el) return;
  const tip = '清单构建顺序：先请求上游 API（/v3/config，用当前视图版本的账号）→ 失败则读取本机桌面客户端缓存文件 → 都拿不到才用内置快照。'
    + '清单在 5 分钟缓存期内复用，过期自动重取；网关后台每天也会兜底获取一次；点「刷新模型清单」可立即从上游更新。';
  if(!meta || !meta.source){
    el.style.cssText = CATALOG_BANNER_BASE + 'flex-direction:column;align-items:flex-start;gap:2px;background:transparent;border:1px dashed var(--line);color:var(--dim)';
    el.title = tip;
    el.innerHTML = '数据来源：未知（当前网关进程版本较旧未提供来源信息，重启网关后可见）';
    return;
  }
  const isApi = (meta.source === 'remote' || meta.source === 'endpoint');
  const at = fmtCatalogStamp(meta.fetched_at);
  el.title = tip;
  if(isApi){
    el.style.cssText = CATALOG_BANNER_BASE
      + 'background:var(--accent2-soft);border:1px solid rgba(94,140,79,.28);color:var(--accent2)';
    el.innerHTML = '<b>✓ 当前为通过 API 获取的最新数据</b>'
      + '<span style="color:var(--dim)">刷新时间 ' + esc(at || '—')
      + ' · 来源 ' + esc(CATALOG_SOURCE_ORIGIN[meta.source] || meta.source) + '</span>';
    return;
  }
  // 非 API（缓存文件 / 内置快照）：警示横幅 + 真实出处做小字。
  let origin = CATALOG_SOURCE_ORIGIN[meta.source] || meta.source;
  if(meta.source === 'cache' || meta.source === 'legacy'){
    const fm = fmtCatalogStamp(meta.file_mtime);
    if(fm) origin += ' · 文件写入于 ' + fm;
    if(at) origin += ' · 网关读取于 ' + at;
  } else if(at){
    origin += ' · 生成于 ' + at;
  }
  el.style.cssText = CATALOG_BANNER_BASE
    + 'flex-direction:column;align-items:flex-start;gap:2px;'
    + 'background:var(--warn-soft);border:1px solid rgba(183,121,31,.35);color:var(--warn)';
  el.innerHTML = '<b>⚠ 当前为项目默认模型信息（未经 API 刷新）：模型、倍率数据可能不一致</b>'
    + '<span style="color:var(--dim)">数据出处：' + esc(origin)
    + ' · 建议点击「刷新模型清单」获取最新数据</span>';
}

function renderAvailableModels(){
  const tb = document.querySelector('#modelsTable tbody');
  if(!tb) return;
  updateAvailModelCount();
  if(!MODELS_DATA.length){
    tb.innerHTML = '<tr><td colspan="8" class="empty">暂无可用模型</td></tr>';
    return;
  }
  tb.innerHTML = MODELS_DATA.map(m => {
    // 1. 倍率与折扣 —— 一律以 /v1/models 取回的 credits 为准（活目录优先、
    //    内置快照兜底）。以前这里还按模型 id 写死了 hy3 / hy4-preview-f /
    //    国际版 deepseek-v4.1-flash 三个"限时免费"，与 credits 值是 OR 关系，
    //    上游一旦调价面板会继续宣传免费；cn 的夜间注记在 credits 缺失时还会
    //    兜底显示写死的 0.79x。现在取不到就不显示数字。
    let credBadge = '-';
    const credText = (m.credits == null ? '' : String(m.credits)).trim();
    const c = credText.toLowerCase().replace('x', '');
    if(c === '0.00' || c === '0'){
      credBadge = '<span class="badge ok" style="background:var(--accent2-soft);color:var(--accent2)">限时免费 0.00x</span>';
    } else if(credText){
      // 统一成倍率写法：'x0.79' / '0.79' / 数字 0.5 都显示成 '0.79x' / '0.5x'。
      const raw = credText.replace(/^x/i, '') + 'x';
      const night = ((m.id === 'glm-5.2' || m.id === 'deepseek-v4-pro') && window.VIEW_REALM === 'cn')
        ? ' <span style="font-size:10px;color:var(--accent)">(夜间0.5x)</span>' : '';
      credBadge = '<span class="badge s">' + esc(raw) + night + '</span>';
    }
    // #505: 只标「Pro Only」（与上游徽章同名），不标「可用」。没有 entitlement 就什么都不显示。
    // 标注由 /v1/models 给出：source=catalog 是上游目录 modelTiers 的声明（与
    // 调度器拦人用的是同一份数据），source=runtime 是历史上真撞到过 11158。
    const ent = m.entitlement;
    if(ent){
      const stamp = ent.checked_at || ent.at;
      const when = stamp ? new Date(Number(stamp) * 1000).toLocaleString() : '';
      const tip = (ent.source === 'catalog')
        ? ('上游目录声明：该模型仅限 PRO 账号（requiredUserType=standard）'
           + (when ? (' · 目录刷新于 ' + when) : '')
           + ' · 免费账号的请求会被本地拦下，不发往上游 · 仅标记不可用，未标记不代表可用')
        : ('上游 ' + ent.code + ' · ' + (ent.userType ? ('user type ' + ent.userType) : '免费账号')
           + (when ? (' · ' + when) : '') + ' · 仅标记不可用，未标记不代表可用');
      credBadge += ' <span class="badge warn" style="font-size:11px;background:var(--warn-soft);color:var(--warn);border-color:rgba(183,121,31,.35)" title="'
        + esc(tip) + '">Pro Only</span>';
    }

    // 2. 模型能力 (Vision, Tool calls)
    const caps = [];
    const hasVis = m.supports_vision || m.vision || m.multimodal || (m.input_modalities && m.input_modalities.includes('image'));
    if(hasVis){
      caps.push('<span class="badge ok" style="font-size:11px">视觉</span>');
    } else if(m.id === 'glm-5.3-flash' || m.vision_note){
      const tip = m.vision_note || '上游配置宣告支持多模态，但实际后端未接入视觉通道，已强制修正为纯文本';
      caps.push('<span class="badge warn" style="font-size:11px;background:var(--warn-soft);color:var(--warn);border-color:rgba(183,121,31,.35)" title="' + esc(tip) + '">文本 (实测无视觉)</span>');
    } else {
      caps.push('<span class="badge" style="color:var(--dim);font-size:11px">文本</span>');
    }
    const hasTool = m.supports_tool_calls || (m.capabilities && m.capabilities.tool_calls);
    if(hasTool){
      caps.push('<span class="badge ok" style="font-size:11px">工具</span>');
    }
    const hasReasoning = m.supports_reasoning || m.reasoning_fixed_effort || (m.reasoning_efforts && m.reasoning_efforts.length);
    if(hasReasoning){
      caps.push('<span class="badge" style="background:rgba(124,58,237,.12);color:var(--think);border-color:rgba(124,58,237,.3);font-size:11px;font-weight:600">推理</span>');
    }
    const capHtml = caps.join(' ');

    const fmtK = v => {
      if(!v) return '-';
      const k = Math.round(v / 1000);
      return k >= 1000 ? (k / 1000) + 'M' : k + 'K';
    };

    // 3. 上下文窗口
    let ctx = '-';
    if(m.context_length){
      ctx = '<b>' + fmtK(m.context_length) + '</b>';
    } else if(m.context_windows && m.context_windows.length){
      ctx = '<b>' + m.context_windows.map(fmtK).join(' / ') + '</b>';
    }

    // 4. 最大输出（有实测钳制时标注「钳制 N×」，数据来自 output_probes.json）
    let maxOut = m.max_output_tokens ? ('<b>' + fmtK(m.max_output_tokens) + '</b>') : '-';
    const clamp = Number(m.output_clamp || 0);
    if(clamp > 0){
      const claimed = Number(m.max_output_tokens || 0);
      const tip = '声称 ' + (claimed ? fmtK(claimed) : '?') + ' · 实测 ' + fmtK(clamp);
      if(!claimed){
        maxOut = '<b style="color:var(--warn)">' + fmtK(clamp) + ' ⚠</b>'
          + '<div style="font-size:11px;color:var(--warn);margin-top:2px" title="' + esc(tip) + '">实测上限</div>';
      }else if(clamp < claimed){
        const ratio = claimed / clamp;
        const xs = (ratio >= 10 ? Math.round(ratio) : Math.round(ratio * 10) / 10) + '×';
        maxOut = '<b style="color:var(--warn)">' + fmtK(clamp) + ' ⚠</b>'
          + '<div style="font-size:11px;color:var(--warn);margin-top:2px" title="' + esc(tip) + '">钳制 ' + xs + '</div>';
      }else{
        maxOut = '<b>' + fmtK(clamp) + '</b> <span style="color:var(--accent2)">✓</span>';
      }
    }

    // 5. 思考档位（档位徽章 + 默认档位 / 能否关闭思考两行注记）
    let effort = '<span style="color:var(--dim)">—</span>';
    const renderEffortBadge = e => {
      const lvl = String(e || '').toLowerCase();
      return '<span class="badge-effort">' + esc(lvl) + '</span>';
    };
    if(m.reasoning_fixed_effort){
      effort = renderEffortBadge(m.reasoning_fixed_effort);
    } else if(m.reasoning_efforts && m.reasoning_efforts.length === 1){
      effort = renderEffortBadge(m.reasoning_efforts[0]);
    } else if(m.reasoning_efforts && m.reasoning_efforts.length){
      effort = m.reasoning_efforts.map(renderEffortBadge).join(' ');
    } else if(m.supports_reasoning){
      effort = '<span class="badge-effort">原生</span>';
    }
    const effortNotes = [];
    if(m.reasoning_default_effort){
      effortNotes.push('默认 ' + esc(String(m.reasoning_default_effort)));
    }
    if(m.reasoning_can_disable === true){
      effortNotes.push('可关闭思考');
    } else if(m.reasoning_can_disable === false){
      effortNotes.push('不可关闭思考');
    }
    if(effortNotes.length){
      effort += '<div style="font-size:11px;color:var(--dim);margin-top:2px">' + effortNotes.join(' · ') + '</div>';
    }

    // 6. 名称（默认模型带星标；描述挂在名称的悬浮提示上）
    const nm = String(m.name || '').trim();
    const nmDesc = String(m.description || '').trim();
    const nameCell = '<td style="text-align:left" data-label="名称">'
      + (m.is_default ? '<span title="默认模型" style="color:var(--accent)">★</span> ' : '')
      + (nm
          ? '<span' + (nmDesc ? ' title="' + esc(nmDesc) + '"' : '') + '>' + esc(nm) + '</span>'
          : '<span style="color:var(--dim)">—</span>')
      + '</td>';

    // 7. 默认采样参数（上游对缺省请求生效的采样值；有的模型只给部分字段）
    const samples = [];
    if(m.temperature != null) samples.push('温度 ' + esc(String(m.temperature)));
    if(m.top_p != null) samples.push('top_p ' + esc(String(m.top_p)));
    const sampleCell = '<td style="text-align:left;padding-right:14px" data-label="默认采样参数">'
      + (samples.length
          ? '<span style="font-size:12px;display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap">' + samples.map(s => '<span style="background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:2px 8px">' + s + '</span>').join('') + '</span>'
          : '<span style="color:var(--dim)">—</span>')
      + '</td>';

    // 8. 模型 ID 前的「一键配置」复选框：勾上才会被写进 DSH 等客户端的模型
    //    清单（选择全局一份，见 AGENT_MODEL_SELECTION）。服务端还没存过勾选时
    //    isModelPicked() 一律返回 true——默认全部勾选，首屏不必等 GET 回来。
    const picked = isModelPicked(m.id);
    return '<tr>'
      + '<td style="text-align:left" class="mono model-id-cell" data-label="模型 ID" title="' + esc(m.id) + '">'
      + '<label><input type="checkbox" class="model-pick" data-model-id="' + esc(m.id) + '"'
      + (picked ? ' checked' : '')
      + ' title="勾选后该模型才会写入 DSH 等客户端的一键配置"><b>' + esc(m.id) + '</b></label></td>'
      + nameCell
      + '<td style="text-align:center" class="rate-cell" data-label="消费倍率">' + credBadge + '</td>'
      + '<td style="text-align:center" data-label="能力">' + capHtml + '</td>'
      + '<td style="text-align:center" data-label="上下文">' + ctx + '</td>'
      + '<td style="text-align:center" data-label="最大输出">' + maxOut + '</td>'
      + '<td style="text-align:left" data-label="思考档位">' + effort + '</td>'
      + sampleCell
      + '</tr>';
  }).join('');
  applyTableSort('modelsTable');
}
