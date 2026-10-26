/* ---- API key list: several keys, each bound to an upstream exit ---- */
let API_KEY_ROWS = [];
let DELETED_KEY_ROWS = [];
/* Ids the user removed but that are still stored server-side, pending the
   next save. Deletion is sent explicitly (rather than by leaving the row out
   of the list) so a submission that is stale or incomplete - a second tab, a
   save that raced the post-save reload - can never retire a key nobody
   removed. */
let DELETED_KEY_IDS = [];

function randomKeyValue(){
  const bytes = new Uint8Array(18);
  if(window.crypto && crypto.getRandomValues) crypto.getRandomValues(bytes);
  const raw = Array.from(bytes).map(b => b.toString(16).padStart(2, '0')).join('');
  // crypto.getRandomValues is present in every supported browser; the
  // timestamp fallback only exists so the field is never left empty.
  return raw || ('wb-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10));
}

const REALM_CHOICES = [
  ['', '跟随面板切换'],
  ['intl', '固定国际版出口'],
  ['cn', '固定国内版出口'],
];

/* Model allow-list per key. Patterns are matched case-insensitively with `*`
   wildcards ("deepseek*", "gpt-6-astra"). An empty list means no limit, which
   is how every key saved before this field existed comes back. */
function parseModelPatterns(text){
  const out = [];
  String(text || '').split(/[,;]/).forEach(part => {
    const pattern = part.trim().toLowerCase();
    if(pattern && out.indexOf(pattern) === -1) out.push(pattern);
  });
  return out;
}
function modelPatternsText(patterns){
  return Array.isArray(patterns) ? patterns.join(', ') : '';
}

/* 允许账号按精确 uid 匹配（不做通配），所以这里只做去空白与去重。 */
function parseAccountRefs(text){
  const out = [];
  String(text || '').split(/[,;\n]/).forEach(part => {
    const ref = part.trim();
    if(ref && out.indexOf(ref) === -1) out.push(ref);
  });
  return out;
}
function accountRefsText(refs){
  return Array.isArray(refs) ? refs.join(', ') : '';
}

/* 已删除的 Key 是只读历史：密钥已被抹掉，所以这里不提供复制，也不提供恢复。 */
function renderDeletedKeyRows(){
  const box = document.getElementById('deletedKeySection');
  if(!box) return;
  if(!DELETED_KEY_ROWS.length){ box.innerHTML = ''; return; }
  const rows = DELETED_KEY_ROWS.map(k => {
    const realmLbl = (k.realm === 'intl') ? '固定国际版出口'
      : ((k.realm === 'cn') ? '固定国内版出口' : '跟随面板切换');
    return '<div style="font-size:12px;color:var(--dim);display:flex;gap:14px;flex-wrap:wrap;padding:7px 0;border-top:1px solid var(--line)">'
      + '<span style="color:var(--fg);font-weight:600">' + esc(k.name || '未命名') + '</span>'
      + '<span>' + esc(realmLbl) + '</span>'
      + (k.created_at ? '<span>创建 ' + esc(k.created_at) + '</span>' : '')
      + (k.deleted_at ? '<span>删除 ' + esc(k.deleted_at) + '</span>' : '')
      + '<span class="mono">' + esc(k.id) + '</span>'
      + '</div>';
  }).join('');
  box.innerHTML = '<details style="margin-top:14px">'
    + '<summary style="cursor:pointer;font-size:12px;color:var(--dim);font-weight:600">已删除的 API Key（'
    + DELETED_KEY_ROWS.length + '）· 只读</summary>'
    + '<div style="margin-top:4px">' + rows + '</div>'
    + '<div style="font-size:11px;color:var(--dim);margin-top:8px">'
    + '删除时密钥已被抹掉，无法恢复、也无法再用于请求。保留这条记录是为了让「按 API Key 的消耗归属」里这些 Key 的历史用量仍然显示名字。'
    + '</div></details>';
}

function renderKeyRows(){
  renderDeletedKeyRows();
  const box = document.getElementById('keyList');
  if(!box) return;
  if(!API_KEY_ROWS.length){
    box.innerHTML = '<div class="empty" style="padding:24px 0">还没有配置 API Key，点击右上角「+ 添加 API Key」创建。</div>';
    return;
  }
  box.innerHTML = API_KEY_ROWS.map((row, i) => {
    if(row._editing){
      const options = REALM_CHOICES.map(([val, lbl]) =>
        '<option value="' + val + '"' + ((row.realm || '') === val ? ' selected' : '') + '>' + lbl + '</option>'
      ).join('');
      return '<div class="key-card" style="border-color:var(--accent);background:var(--panel);box-shadow:0 0 0 1px var(--accent), var(--shadow-md)">'
        + '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">'
        +   '<div style="font-size:14px;font-weight:700;color:var(--fg)">' + (row.id ? '编辑 API Key' : '新建 API Key') + '</div>'
        +   '<span class="badge s mini">设置出口</span>'
        + '</div>'
        + '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:14px">'
        +   '<div>'
        +     '<div style="font-size:12px;color:var(--dim);margin-bottom:4px;font-weight:500">备注名称</div>'
        +     '<input type="text" id="editKeyName_' + i + '" value="' + esc(row.name || '') + '" placeholder="如: Cursor / DSH / 本地测试" '
        +       'style="width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:7px 10px;font:inherit;color:var(--fg)">'
        +   '</div>'
        +   '<div>'
        +     '<div style="font-size:12px;color:var(--dim);margin-bottom:4px;font-weight:500">出口绑定</div>'
        +     '<select id="editKeyRealm_' + i + '" style="width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:7px 10px;font:inherit;color:var(--fg)">'
        +       options
        +       '</select>'
        +   '</div>'
        +   '<div>'
        +     '<div style="font-size:12px;color:var(--dim);margin-bottom:4px;font-weight:500">最大并发限制 <span style="color:var(--dim-light)">(选填，0 = 不限)</span></div>'
        +     '<input type="number" min="0" max="1000" id="editKeyConc_' + i + '" value="' + (row.concurrency_limit || '') + '" placeholder="0 代表不限制" '
        +       'style="width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:7px 10px;font:inherit;color:var(--fg)">'
        +   '</div>'
        +   '<div style="grid-column:1 / -1">'
        +     '<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:4px">'
        +       '<div style="font-size:12px;color:var(--dim);font-weight:500">模型准入 <span id="editKeyModelsSummary_' + i + '" style="color:var(--dim-light)"></span></div>'
        +       '<button class="sec mini" style="border-radius:8px;padding:4px 12px" onclick="toggleKeyPicker(\'model\',' + i + ')">选择模型</button>'
        +     '</div>'
        +     '<div id="editKeyModelPicker_' + i + '" style="display:none;border:1px solid var(--line);border-radius:8px;padding:8px;margin-top:4px">'
        +       '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px">'
        +         '<input id="editKeyModelSearch_' + i + '" placeholder="搜索模型" oninput="filterKeyModelPicker(' + i + ')" style="flex:1;min-width:160px;background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:6px 9px;font:inherit;color:var(--fg)">'
        +         '<button class="sec mini" onclick="setKeyModelsAll(' + i + ',true)">全选</button>'
        +         '<button class="sec mini" onclick="setKeyModelsAll(' + i + ',false)">全不选</button>'
        +       '</div>'
        +       '<div id="editKeyModelList_' + i + '" style="max-height:220px;overflow:auto;display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:2px 8px"></div>'
        +     '</div>'
        +     '<div id="editKeyModelWildcards_' + i + '" style="font-size:11px;color:var(--warn);margin-top:4px"></div>'
        +   '</div>'
        +   '<div style="grid-column:1 / -1">'
        +     '<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:4px">'
        +       '<div style="font-size:12px;color:var(--dim);font-weight:500">允许账号 <span id="editKeyAccountsSummary_' + i + '" style="color:var(--dim-light)"></span></div>'
        +       '<button class="sec mini" style="border-radius:8px;padding:4px 12px" onclick="toggleKeyPicker(\'account\',' + i + ')">选择账号</button>'
        +     '</div>'
        +     '<div id="editKeyAccountPicker_' + i + '" style="display:none;border:1px solid var(--line);border-radius:8px;padding:8px;margin-top:4px">'
        +       '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px">'
        +         '<input id="editKeyAccountSearch_' + i + '" placeholder="搜索昵称或 UID" oninput="filterKeyAccountPicker(' + i + ')" style="flex:1;min-width:160px;background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:6px 9px;font:inherit;color:var(--fg)">'
        +         '<button class="sec mini" onclick="setKeyAccountsAll(' + i + ',true)">全选</button>'
        +         '<button class="sec mini" onclick="setKeyAccountsAll(' + i + ',false)">全不选</button>'
        +       '</div>'
        +       '<div id="editKeyAccountList_' + i + '" style="max-height:220px;overflow:auto;display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:2px 8px"></div>'
        +     '</div>'
        +   '</div>'
        +   '<div style="grid-column:1 / -1">'
        +     '<div style="font-size:12px;color:var(--dim);margin-bottom:4px;font-weight:500">API Key 内容 ' + (row.masked ? '<span style="color:var(--dim-light)">(已保存: ' + esc(row.masked) + '，留空表示不改)</span>' : '') + '</div>'
        +     '<div style="display:flex;gap:8px">'
        +       '<input type="text" id="editKeyValue_' + i + '" value="' + esc(row.key || '') + '" placeholder="' + (row.masked ? '留空表示保持原 Key 不变' : '输入自定义密钥，或点击右侧生成') + '" '
        +         'style="flex:1;background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:7px 10px;font:inherit;color:var(--fg)">'
        +       '<button class="sec mini" style="white-space:nowrap;border-radius:8px;padding:7px 14px" onclick="generateKeyForEdit(' + i + ')">随机生成</button>'
        +     '</div>'
        +   '</div>'
        + '</div>'
        + '<div style="display:flex;gap:8px;justify-content:flex-end">'
        +   '<button class="sec mini" style="border-radius:8px;padding:6px 16px" onclick="cancelEditKey(' + i + ')">取消</button>'
        +   '<button class="mini" style="border-radius:8px;padding:6px 18px" onclick="saveSingleKey(' + i + ', this)">保存</button>'
        + '</div>'
        + '</div>';
    }

    const statusBadge = row.enabled
      ? '<span class="badge ok" style="font-size:11px;font-weight:600;padding:2px 10px">已启用</span>'
      : '<span class="badge off" style="font-size:11px;font-weight:600;padding:2px 10px">已禁用</span>';

    const realmBadge = (row.realm === 'intl')
      ? '<span class="realm-badge-intl" style="font-size:11px">固定国际版出口</span>'
      : (row.realm === 'cn')
      ? '<span class="realm-badge-cn" style="font-size:11px">固定国内版出口</span>'
      : '<span class="badge s" style="font-size:11px">跟随面板切换</span>';

    const concBadge = (row.concurrency_limit && row.concurrency_limit > 0)
      ? '<span class="badge s mini" style="font-size:11px">并发上限 ' + row.concurrency_limit + '</span>'
      : '';
    const modelBadge = (Array.isArray(row.models) && row.models.length)
      ? '<span class="badge s" style="font-size:11px">限 ' + esc(modelPatternsText(row.models)) + '</span>'
      : '';
    const accountBadge = (Array.isArray(row.accounts) && row.accounts.length)
      ? '<span class="badge s" style="font-size:11px">限 ' + row.accounts.length + ' 个账号</span>'
      : '';

    const displayKey = row.masked || (row.key ? (row.key.slice(0, 4) + '******' + row.key.slice(-4)) : '未设置');
    const createdStr = row.created_at ? ('创建时间 ' + esc(row.created_at)) : '';

    return '<div class="key-card' + (row.enabled ? '' : ' disabled') + '">'
      + '<div class="key-card-main">'
      +   '<div>'
      +     '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap">'
      +       '<span style="font-size:15px;font-weight:700;color:var(--fg)">' + esc(row.name || '未命名') + '</span>'
      +       statusBadge
      +       realmBadge
      +       modelBadge
        +       accountBadge
        +       concBadge
      +     '</div>'
      +     '<div style="font-size:12px;color:var(--dim);display:flex;align-items:center;gap:16px;flex-wrap:wrap">'
      +       '<span>Key: <span class="mono" style="color:var(--fg);font-weight:500">' + esc(displayKey) + '</span></span>'
      +       (createdStr ? ('<span>' + createdStr + '</span>') : '')
      +     '</div>'
      +   '</div>'
      +   '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
      +     '<button class="key-action-btn" id="copyBtn' + i + '" onclick="copyKeyRow(' + i + ')">复制</button>'
      +     '<button class="key-action-btn" onclick="openEditKey(' + i + ')">编辑</button>'
      +     '<button class="key-action-btn" onclick="toggleKeyRow(' + i + ')">' + (row.enabled ? '禁用' : '启用') + '</button>'
      +     '<button class="key-action-btn danger" onclick="removeKeyRow(' + i + ')">删除</button>'
      +   '</div>'
      + '</div>'
      + '</div>';
  }).join('');
  API_KEY_ROWS.forEach((row, i) => { if(row._editing) renderKeyPickers(i); });
}

function keyModelWildcards(row){
  return (row.models || []).filter(p => String(p).indexOf('*') >= 0);
}
function keyModelExact(row){
  return (row.models || []).filter(p => String(p).indexOf('*') < 0);
}
function updateKeyModelSummary(i){
  const row = API_KEY_ROWS[i];
  if(!row) return;
  const exact = keyModelExact(row).length;
  const wild = keyModelWildcards(row).length;
  const el = document.getElementById('editKeyModelsSummary_' + i);
  if(el){
    el.textContent = (exact || wild)
      ? ('（已选 ' + exact + ' 个' + (wild ? ' + ' + wild + ' 个通配符' : '') + '）')
      : '（不限制）';
  }
  const wildEl = document.getElementById('editKeyModelWildcards_' + i);
  if(wildEl) wildEl.textContent = wild.length ? ('保留通配符：' + wild.join(', ')) : '';
}
function updateKeyAccountSummary(i){
  const row = API_KEY_ROWS[i];
  if(!row) return;
  const n = (row.accounts || []).length;
  const el = document.getElementById('editKeyAccountsSummary_' + i);
  if(el) el.textContent = n ? ('（已选 ' + n + ' 个账号）') : '（不限制）';
}
function toggleKeyPicker(kind, i){
  const id = kind === 'model' ? 'editKeyModelPicker_' + i : 'editKeyAccountPicker_' + i;
  const el = document.getElementById(id);
  if(el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
}
async function loadKeyModelCatalog(){
  if(Array.isArray(window.KEY_MODEL_CATALOG) && window.KEY_MODEL_CATALOG.length){
    return window.KEY_MODEL_CATALOG;
  }
  const out = [];
  const seen = new Set();
  const add = function(list){
    for(const m of (list || [])){
      const id = String((m && m.id) || '');
      if(id && !seen.has(id)){ seen.add(id); out.push(m); }
    }
  };
  // The unscoped listing is the widest catalogue the gateway advertises; the
  // per-realm calls fill in anything it omits.
  try { const r = await getJSON('/v1/models'); add(r.data); } catch(e) { /* keep going */ }
  for(const realm of ['intl', 'cn']){
    try {
      const r = await getJSON('/v1/models?realm=' + realm);
      add(r.data);
    } catch(e) { /* one realm failing must not blank the picker */ }
  }
  if(!out.length && Array.isArray(window.MODELS_DATA)) out.push(...window.MODELS_DATA);
  window.KEY_MODEL_CATALOG = out;
  return out;
}
async function renderKeyPickers(i){
  const row = API_KEY_ROWS[i];
  if(!row) return;
  const modelList = document.getElementById('editKeyModelList_' + i);
  if(modelList){
    modelList.innerHTML = '<div style="font-size:12px;color:var(--dim);padding:4px">正在加载模型清单…</div>';
    let models = [];
    try { models = await loadKeyModelCatalog(); } catch(e) { models = []; }
    // The all-realm listing is the primary source; fall back to the models tab
    // cache so the picker is never blank just because that one call failed.
    if(!(models || []).length && Array.isArray(window.MODELS_DATA)){
      models = window.MODELS_DATA;
    }
    if(!(models || []).length){
      modelList.innerHTML = '<div style="font-size:12px;color:var(--warn);padding:4px">模型清单为空或加载失败，<a href="javascript:void(0)" onclick="renderKeyPickers(' + i + ')">重试</a></div>';
    } else {
      modelList.innerHTML = models.map(m => {
        const id = String((m && m.id) || '');
        if(!id) return '';
        const checked = keyModelExact(row).indexOf(id) >= 0 ? ' checked' : '';
        return '<label class="key-pick-row" data-search="' + esc(id.toLowerCase()) + '" style="display:flex;align-items:center;gap:6px;font-size:12px;padding:3px 4px"><input type="checkbox" value="' + esc(id) + '"' + checked + ' onchange="toggleKeyModel(' + i + ',this)">' + esc(id) + '</label>';
      }).join('');
    }
    updateKeyModelSummary(i);
  }
  const accountList = document.getElementById('editKeyAccountList_' + i);
  if(accountList){
    if(!(window.ACCOUNTS || []).length){
      try { await loadAccounts(); } catch(e) { /* keep the empty hint */ }
    }
    const accts = window.ACCOUNTS || [];
    accountList.innerHTML = accts.map(a => {
      const uid = String((a && a.uid) || '');
      if(!uid) return '';
      const label = (a.nickname || uid.slice(0, 8)) + ' · ' + uid.slice(0, 8) + (a.realm ? ' · ' + a.realm : '');
      const checked = (row.accounts || []).indexOf(uid) >= 0 ? ' checked' : '';
      return '<label class="key-pick-row" data-search="' + esc((label + ' ' + uid).toLowerCase()) + '" style="display:flex;align-items:center;gap:6px;font-size:12px;padding:3px 4px"><input type="checkbox" value="' + esc(uid) + '"' + checked + ' onchange="toggleKeyAccount(' + i + ',this)">' + esc(label) + '</label>';
    }).join('') || '<div style="font-size:12px;color:var(--warn);padding:4px">账号列表为空或加载失败，<a href="javascript:void(0)" onclick="renderKeyPickers(' + i + ')">重试</a></div>';
    updateKeyAccountSummary(i);
  }
}
function toggleKeyModel(i, el){
  const row = API_KEY_ROWS[i];
  if(!row) return;
  const exact = new Set(keyModelExact(row));
  if(el.checked) exact.add(el.value); else exact.delete(el.value);
  row.models = keyModelWildcards(row).concat(Array.from(exact).sort());
  updateKeyModelSummary(i);
}
function toggleKeyAccount(i, el){
  const row = API_KEY_ROWS[i];
  if(!row) return;
  const set = new Set(row.accounts || []);
  if(el.checked) set.add(el.value); else set.delete(el.value);
  row.accounts = Array.from(set).sort();
  updateKeyAccountSummary(i);
}
function setKeyModelsAll(i, on){
  const list = document.getElementById('editKeyModelList_' + i);
  const row = API_KEY_ROWS[i];
  if(!list || !row) return;
  list.querySelectorAll('input[type=checkbox]').forEach(el => { el.checked = on; });
  const exact = on
    ? Array.from(list.querySelectorAll('input[type=checkbox]')).map(el => el.value).sort()
    : [];
  row.models = keyModelWildcards(row).concat(exact);
  updateKeyModelSummary(i);
}
function setKeyAccountsAll(i, on){
  const list = document.getElementById('editKeyAccountList_' + i);
  const row = API_KEY_ROWS[i];
  if(!list || !row) return;
  list.querySelectorAll('input[type=checkbox]').forEach(el => { el.checked = on; });
  row.accounts = on
    ? Array.from(list.querySelectorAll('input[type=checkbox]')).map(el => el.value).sort()
    : [];
  updateKeyAccountSummary(i);
}
function filterKeyModelPicker(i){
  const input = document.getElementById('editKeyModelSearch_' + i);
  const list = document.getElementById('editKeyModelList_' + i);
  if(!list) return;
  const q = String((input && input.value) || '').trim().toLowerCase();
  list.querySelectorAll('.key-pick-row').forEach(el => {
    el.style.display = !q || (el.getAttribute('data-search') || '').indexOf(q) >= 0 ? 'flex' : 'none';
  });
}
function filterKeyAccountPicker(i){
  const input = document.getElementById('editKeyAccountSearch_' + i);
  const list = document.getElementById('editKeyAccountList_' + i);
  if(!list) return;
  const q = String((input && input.value) || '').trim().toLowerCase();
  list.querySelectorAll('.key-pick-row').forEach(el => {
    el.style.display = !q || (el.getAttribute('data-search') || '').indexOf(q) >= 0 ? 'flex' : 'none';
  });
}

function openEditKey(index){
  if(!API_KEY_ROWS[index]) return;
  API_KEY_ROWS[index]._editing = true;
  renderKeyRows();
}

function cancelEditKey(index){
  if(!API_KEY_ROWS[index]) return;
  if(!API_KEY_ROWS[index].id && !API_KEY_ROWS[index].masked){
    API_KEY_ROWS.splice(index, 1);
  } else {
    API_KEY_ROWS[index]._editing = false;
  }
  renderKeyRows();
}

function generateKeyForEdit(index){
  const el = document.getElementById('editKeyValue_' + index);
  if(el){
    el.value = randomKeyValue();
  }
}

async function saveSingleKey(index, btn){
  const row = API_KEY_ROWS[index];
  if(!row) return;
  const nameInput = document.getElementById('editKeyName_' + index);
  const realmSelect = document.getElementById('editKeyRealm_' + index);
  const keyInput = document.getElementById('editKeyValue_' + index);
    const concInput = document.getElementById('editKeyConc_' + index);

  const name = nameInput ? nameInput.value.trim() : (row.name || '');
  const realm = realmSelect ? realmSelect.value : (row.realm || '');
  const keyVal = keyInput ? keyInput.value.trim() : (row.key || '');

  if(!keyVal && !row.masked){
    toast('API Key 不能为空', 'warn');
    return;
  }

  row.name = name || '未命名';
  row.realm = realm;
  row.models = Array.isArray(row.models) ? row.models.slice() : [];
  row.accounts = Array.isArray(row.accounts) ? row.accounts.slice() : [];
    row.concurrency_limit = Math.max(0, parseInt((concInput ? concInput.value : row.concurrency_limit) || 0, 10) || 0);
  if(keyVal) row.key = keyVal;
  row._editing = false;

  if(btn) btn.disabled = true;
  try {
    if(!await saveApiKeys()) return;
    toast('API Key [' + row.name + '] 已保存', 'ok');
  } catch(e){
    toast('保存失败: ' + e.message, 'bad');
  }
}

async function toggleKeyRow(index){
  const row = API_KEY_ROWS[index];
  if(!row) return;
  row.enabled = !row.enabled;
  try {
    if(!await saveApiKeys()){
      // The save was rejected; put the switch back so the row does not lie.
      row.enabled = !row.enabled;
      renderKeyRows();
      return;
    }
    toast(row.enabled ? 'Key [' + row.name + '] 已启用' : 'Key [' + row.name + '] 已禁用', 'ok');
  } catch(e){
    row.enabled = !row.enabled;
    renderKeyRows();
    toast('操作失败: ' + e.message, 'bad');
  }
}

async function removeKeyRow(index){
  const row = API_KEY_ROWS[index];
  if(!row) return;
  if(!confirm('确定删除 API Key [' + (row.name || '未命名') + '] 吗？密钥会被抹掉且无法恢复；看板里它已经产生的历史用量仍会保留这个名字。')) return;
  if(row.id && DELETED_KEY_IDS.indexOf(row.id) === -1) DELETED_KEY_IDS.push(row.id);
  API_KEY_ROWS.splice(index, 1);
  try {
    if(!await saveApiKeys()){
      // The server kept the key; drop the pending delete and resync the list
      // so the row comes back instead of lingering as a phantom.
      DELETED_KEY_IDS = DELETED_KEY_IDS.filter(id => id !== row.id);
      await loadSettings();
      return;
    }
    toast('已删除 API Key', 'ok');
  } catch(e){
    toast('删除失败: ' + e.message, 'bad');
  }
}

function addApiKeyRow(){
  API_KEY_ROWS.push({
    id: '',
    name: '',
    key: randomKeyValue(),
    realm: '',
    models: [],
      concurrency_limit: 0,
    enabled: true,
    masked: '',
    _editing: true
  });
  renderKeyRows();
  setTimeout(() => {
    const el = document.getElementById('editKeyName_' + (API_KEY_ROWS.length - 1));
    if(el) el.focus();
  }, 50);
}
/* Copy the key in a row, fetching the stored value when the field is blank
   (the panel only ever displays a masked key).

   Safari ties navigator.clipboard to the click's transient activation: an
   `await` before the write drops it and the promise rejects with
   NotAllowedError ("... not allowed by the user agent ..."). Chrome is looser
   about "recent activation", which is why only Safari fails here. So copy
   synchronously when the value is already in hand, and when it has to be
   fetched hand the plaintext to the clipboard as a promise inside a
   ClipboardItem -- the one asynchronous copy form Safari accepts. */
async function copyKeyRow(index){
  const row = API_KEY_ROWS[index];
  if(!row) return;
  const btn = document.getElementById('copyBtn' + index);
  const markCopied = () => {
    if(!btn) return;
    const old = btn.textContent;
    btn.textContent = '已复制';
    setTimeout(() => { btn.textContent = old; }, 1600);
  };
  const value = (row.key || '').trim();
  try {
    if(value){
      // Already in hand: copy without yielding, keeping the click activation.
      await writeClipboard(value);
      markCopied();
      toast('已复制到剪贴板', 'ok');
      return;
    }
    if(!row.id){
      toast('这一行还没有 Key，点「随机生成」或直接填写', 'warn');
      return;
    }
    const reveal = () => getJSON('/settings/reveal?id=' + encodeURIComponent(row.id))
      .then(d => (d.key || '').trim());
    if(window.ClipboardItem && navigator.clipboard && window.isSecureContext){
      // write() is called synchronously inside the click handler; the plaintext
      // arrives later through the item's promise.
      const item = new ClipboardItem({
        'text/plain': reveal().then(k => {
          if(!k) throw new Error('这一行还没有 Key');
          return new Blob([k], { type: 'text/plain' });
        }),
      });
      await navigator.clipboard.write([item]);
    } else {
      // Older engines / insecure context: fetch first, then the textarea path.
      const k = await reveal();
      if(!k){
        toast('这一行还没有 Key，点「随机生成」或直接填写', 'warn');
        return;
      }
      await writeClipboard(k);
    }
    markCopied();
    toast('已复制到剪贴板', 'ok');
  } catch(e) {
    toast('复制失败: ' + e.message, 'bad');
  }
}

async function writeClipboard(text){
  // navigator.clipboard needs a secure context; plain http on a LAN address
  // does not qualify, so fall back to a temporary textarea.
  if(navigator.clipboard && window.isSecureContext){
    await navigator.clipboard.writeText(text);
    return;
  }
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.cssText = 'position:fixed;top:-1000px;opacity:0';
  document.body.appendChild(area);
  area.select();
  area.setSelectionRange(0, area.value.length);
  const ok = document.execCommand('copy');
  document.body.removeChild(area);
  if(!ok) throw new Error('浏览器拒绝了复制操作');
}

/* Save the whole visible list, plus the ids the user explicitly removed.
   Returns true when the server accepted the save.

   The submit is an upsert: the server retires only the ids named in
   `deleted_api_key_ids` and leaves every other stored key alone. That is what
   keeps a stale or incomplete list from wiping keys the user never removed,
   which is how "adding a new key" used to lose the old ones. */
async function saveApiKeys(btn){
  const payload = API_KEY_ROWS
    .filter(row => !row.id || DELETED_KEY_IDS.indexOf(row.id) === -1)
    .map(row => ({
      id: row.id || '',
      name: row.name || '',
      realm: row.realm || '',
      models: row.models || [],
      accounts: row.accounts || [],
        concurrency_limit: parseInt(row.concurrency_limit || 0, 10) || 0,
      enabled: row.enabled !== false,
      // Blank means "keep the stored key", which the server honours per row.
      key: (row.key || '').trim(),
    }));
  if(API_KEY_ROWS.some(row => !row.masked && !row.id && !(row.key || '').trim())){
    toast('新增的行还没有填写 Key', 'warn');
    return false;
  }
  // Several callers save without going through a button (saveSingleKey,
  // toggleKeyRow, removeKeyRow), so a missing button must not abort the save.
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {
      api_keys: payload,
      deleted_api_key_ids: DELETED_KEY_IDS.slice(),
    });
    DELETED_KEY_IDS = [];
    toast('已保存 ' + payload.length + ' 个 Key', 'ok');
    // Reload before returning: the next edit must start from the ids the
    // server just minted, or a quick second save would resubmit the rows with
    // empty ids and duplicate them.
    await loadSettings();
    return true;
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
    return false;
  } finally {
    if(btn) btn.disabled = false;
  }
}

let PROXY_SLOTS = [];
/* 编辑器里有未保存改动时为 true（issue #79）。
 *
 * 刷新槽位是「拉服务端列表 → 整体替换 → 重绘整张表」，而「+ 添加槽位」只是把
 * 一行 push 进本地列表、服务端并不知道它存在；loadAccounts() 每 15 秒会顺带
 * 刷新一次槽位，于是刚加的那行（以及改了一半的已有行）会被服务端的旧列表顶掉。
 * 置位期间跳过刷新，保存成功后清零。 */
let SLOTS_DIRTY = false;
let SLOT_DISCOVER = [];

function slotSelectHtml(a){
  const current = a.proxySlot || '';
  // A legacy per-account proxy URL still routes traffic while no slot is
  // bound. Surface it instead of pretending the account is direct.
  const legacy = (!current && a.proxy) ? String(a.proxy) : '';
  const legacyLabel = legacy.length > 28 ? legacy.slice(0, 25) + '...' : legacy;
  const opts = [];
  if(legacy){
    opts.push('<option value="__legacy__" selected>旧代理：' + esc(legacyLabel) + '</option>');
    opts.push('<option value="">直连（清除旧代理）</option>');
  } else {
    opts.push('<option value=""' + (current ? '' : ' selected') + '>直连</option>');
  }
  opts.push.apply(opts, PROXY_SLOTS.map(s =>
    '<option value="' + esc(s.id) + '"' + (s.id === current ? ' selected' : '') + '>'
    + esc(s.label || s.name || s.id) + (s.enabled === false ? '（停用）' : '') + '</option>'));
  const uidAttr = esc(String(a.uid || '').replace(/[^\w.@-]/g, ''));
  return '<select class="slot-select" onchange="setAccountSlot(&quot;' + uidAttr + '&quot;, this.value, this)">'
    + opts.join('') + '</select>';
}

async function setAccountSlot(uid, slotId, el){
  if(slotId === '__legacy__') return;
  if(el) el.disabled = true;
  try{
    await postJSON('/accounts/set', {uid: uid, proxySlot: slotId});
    toast('出口已更新', 'ok');
    await loadAccounts();
    await loadProxySlots();
  }catch(e){
    toast('出口更新失败: ' + e.message, 'bad');
  }finally{
    if(el) el.disabled = false;
  }
}

async function loadProxySlots(force){
  // 编辑器有未保存改动时不动它：轮询与「切到设置页」都不该覆盖正在填的内容。
  if(SLOTS_DIRTY && !force) return;
  try{
    const r = await getJSON('/proxy/slots');
    PROXY_SLOTS = r.slots || [];
    renderProxySlots();
  }catch(e){
    // Panel not logged in yet; actions on the settings page surface errors.
  }
}

/* 标题旁的状态提示。单独抽出来，是因为标记「未保存」时只能更新这段文字，
 * 不能重绘表格——重绘会让正在输入的那个框失焦。 */
function renderSlotState(){
  const stateEl = document.getElementById('slotState');
  if(!stateEl) return;
  const parts = [];
  if(PROXY_SLOTS.length) parts.push(PROXY_SLOTS.length + ' 个');
  if(SLOTS_DIRTY) parts.push('未保存');
  stateEl.textContent = parts.length ? ('（' + parts.join(' · ') + '）') : '';
  stateEl.style.color = SLOTS_DIRTY ? 'var(--warn)' : '';
}

function markSlotsDirty(){
  if(SLOTS_DIRTY) return;
  SLOTS_DIRTY = true;
  renderSlotState();
}

/* 出口信息的展示口径，与服务端 wb_ipintel 保持一致：hosting 地址算机房，
   其余算住宅；国家名由查询服务直接给中文。 */
const IP_TYPE_LABELS = {residential: '住宅', datacenter: '机房'};

function ipTypeLabel(t){ return IP_TYPE_LABELS[t] || ''; }

/* 默认名称：国家 + 类型（如「美国 住宅」）。没有国家就不自动命名——只有
   「住宅」两个字区分不了不同槽位，此时回退显示槽位 id。 */
function autoSlotName(c){
  const where = String((c && c.country) || '').trim();
  if(!where) return '';
  const kind = ipTypeLabel(c && c.ip_type);
  return kind ? (where + ' ' + kind) : where;
}

/* 保存时要把探测到的出口字段一起送回，否则一次保存就把它们抹掉了。 */
function slotPayload(){
  return PROXY_SLOTS.map(s => ({
    id: s.id || '', name: s.name || '', url: (s.url || '').trim(),
    enabled: s.enabled !== false,
    ip: s.ip || '', country: s.country || '', country_code: s.country_code || '',
    ip_type: s.ip_type || '', isp: s.isp || '', asn: s.asn || '',
    probed_at: s.probed_at || 0,
  }));
}

function slotExitHtml(s){
  if(!s.ip) return '<span style="color:var(--dim-light)">未探测</span>';
  const kind = ipTypeLabel(s.ip_type);
  const badge = kind
    ? '<span class="badge ' + (kind === '住宅' ? 's' : '') + '" style="font-size:11px">' + kind + '</span>'
    : '';
  const where = [s.country ? esc(s.country) : '', badge].filter(Boolean).join(' ');
  const tip = [s.isp, s.asn].filter(Boolean).join(' · ');
  return '<div class="mono" style="font-size:12px">' + esc(s.ip) + '</div>'
    + '<div style="font-size:12px;color:var(--dim);margin-top:2px"'
    + (tip ? ' title="' + esc(tip) + '"' : '') + '>'
    + (where || '<span style="color:var(--dim-light)">国家未知</span>') + '</div>';
}

function renderProxySlots(){
  const box = document.getElementById('slotList');
  if(!box) return;
  renderSlotState();
  if(!PROXY_SLOTS.length){
    box.innerHTML = '<div class="hint" style="color:var(--dim)">还没有代理槽。点击「自动发现 mihomo 端口」或「+ 添加槽位」。</div>';
    return;
  }
  box.innerHTML = '<div class="table-wrap"><table class="data-cards"><thead><tr>'
    + '<th style="width:150px">名称</th>'
    + '<th>代理地址</th>'
    + '<th style="width:150px">出口</th>'
    + '<th style="width:90px;text-align:center">启用</th>'
    + '<th style="width:90px;text-align:center">绑定账号</th>'
    + '<th style="width:190px;text-align:center">操作</th>'
    + '</tr></thead><tbody>'
    + PROXY_SLOTS.map((s, i) =>
        '<tr>'
        + '<td data-label="名称"><input value="' + esc(s.name || '') + '" placeholder="' + esc(s.label || '') + '" oninput="PROXY_SLOTS[' + i + '].name=this.value; markSlotsDirty()" class="slot-input"></td>'
        + '<td data-label="代理地址"><input value="' + esc(s.url || '') + '" oninput="PROXY_SLOTS[' + i + '].url=this.value; markSlotsDirty()" class="slot-input mono"></td>'
        + '<td data-label="出口">' + slotExitHtml(s) + '</td>'
        + '<td style="text-align:center" data-label="启用"><input type="checkbox" ' + (s.enabled === false ? '' : 'checked')
        +   ' onchange="PROXY_SLOTS[' + i + '].enabled=this.checked; markSlotsDirty()"></td>'
        + '<td style="text-align:center" data-label="绑定账号">' + (s.bound || 0) + '</td>'
        + '<td style="text-align:center;white-space:nowrap" data-label="">'
        +   '<button class="sec mini" onclick="testProxySlot(' + i + ', this)">测试</button> '
        +   '<button class="danger mini" onclick="removeProxySlot(' + i + ')">删除</button>'
        + '</td></tr>'
      ).join('')
    + '</tbody></table></div>'
    + '<div class="legend"><span>「绑定账号」只统计已启用账号；停用账号会自动释放槽位，重新启用后需重新绑定。在网关页账号表的「出口」下拉中调整绑定。名称留空时按出口自动显示「国家 类型」，点「测试」会补全出口信息并据此命名。</span></div>';
  applyTableSort('slotList');
}

function addProxySlotRow(){
  SLOTS_DIRTY = true;
  // 名称留空：探测到出口后按「国家 类型」自动命名，填过就按填的显示。
  PROXY_SLOTS.push({id: '', name: '', url: '', enabled: true, bound: 0});
  renderProxySlots();
}

function removeProxySlot(index){
  const s = PROXY_SLOTS[index];
  if(!s) return;
  if((s.bound || 0) > 0 && !confirm('该槽位被 ' + s.bound + ' 个账号绑定，删除后它们将回退直连。继续？')) return;
  SLOTS_DIRTY = true;
  PROXY_SLOTS.splice(index, 1);
  renderProxySlots();
}

async function saveProxySlots(btn){
  if(btn) btn.disabled = true;
  try{
      const r = await postJSON('/proxy/slots/save', {slots: slotPayload()});
      PROXY_SLOTS = r.slots || [];
      SLOTS_DIRTY = false;
      renderProxySlots();
      await loadAccounts();
      toast('已保存 ' + PROXY_SLOTS.length + ' 个代理槽', 'ok');
  }catch(e){
    toast('保存失败: ' + e.message, 'bad');
  }finally{
    if(btn) btn.disabled = false;
  }
}

async function testProxySlot(index, btn){
  const s = PROXY_SLOTS[index];
  if(!s || !s.url){ toast('槽位还没有填写代理地址', 'warn'); return; }
  if(btn){ btn.disabled = true; btn.textContent = '测试中...'; }
  try{
    let id = s.id;
    if(!id){
      const r = await postJSON('/proxy/slots/save', {slots: slotPayload()});
      PROXY_SLOTS = r.slots || [];
      SLOTS_DIRTY = false;
      renderProxySlots();
      id = (PROXY_SLOTS[index] || {}).id;
    }
    const r = await postJSON('/proxy/slots/test', {id: id});
    if(r.ok){
      // 服务端已把出口信息写进槽位，这里只把这一行同步过来，不整表重载——
      // 其他行里还没保存的输入都还在 PROXY_SLOTS 里，重绘不会丢。
      const row = PROXY_SLOTS[index];
      if(row && r.slot){
        ['ip', 'country', 'country_code', 'ip_type', 'isp', 'asn', 'probed_at'].forEach(k => {
          if(r.slot[k] !== undefined) row[k] = r.slot[k];
        });
        if(r.slot.name) row.name = r.slot.name;
        row.label = row.name || autoSlotName(row) || row.id;
        renderProxySlots();
      }
      const where = [r.country, ipTypeLabel(r.ip_type)].filter(Boolean).join(' ');
      toast('出口 ' + r.exit_ip + (where ? ' · ' + where : '') + '（' + r.latency_ms + 'ms）', 'ok');
    }else{
      toast('测试失败: ' + (r.error || 'unknown'), 'bad');
    }
  }catch(e){
    toast('测试失败: ' + e.message, 'bad');
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '测试'; }
  }
}

async function discoverProxySlots(btn){
  if(btn){ btn.disabled = true; btn.textContent = '探测中...'; }
  try{
    const r = await postJSON('/proxy/discover', {});
    SLOT_DISCOVER = r.candidates || [];
    renderDiscover();
    const ok = SLOT_DISCOVER.filter(c => c.reachable).length;
    toast('发现 ' + ok + ' 个可用出口', ok ? 'ok' : 'warn');
  }catch(e){
    toast('自动发现失败: ' + e.message, 'bad');
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '自动发现 mihomo 端口'; }
  }
}

function renderDiscover(){
  const box = document.getElementById('slotDiscover');
  if(!box) return;
  if(!SLOT_DISCOVER.length){ box.innerHTML = ''; return; }
  box.innerHTML = '<div style="margin-top:14px"><div style="font-size:12px;color:var(--dim);margin-bottom:8px">'
    + '勾选要导入为槽位的出口：</div>'
    + SLOT_DISCOVER.map((c, i) =>
        '<label style="display:flex;align-items:center;gap:8px;padding:6px 0;font-size:13px">'
        + '<input type="checkbox" ' + (c.reachable ? 'checked' : 'disabled') + ' data-idx="' + i + '">'
        + '<span class="mono">' + esc(c.url) + '</span>'
        + '<span style="color:' + (c.reachable ? 'var(--accent2)' : 'var(--bad)') + '">'
        + (c.reachable
            ? ('出口 ' + esc(c.exit_ip)
               + (autoSlotName(c) ? ' · ' + esc(autoSlotName(c)) : '')
               + ' · ' + c.latency_ms + 'ms')
            : '不可达') + '</span>'
        + '</label>').join('')
    + '<button class="sec" onclick="importDiscovered()" style="margin-top:8px">导入勾选的出口</button></div>';
}

function importDiscovered(){
  const box = document.getElementById('slotDiscover');
  if(!box) return;
  const picked = Array.from(box.querySelectorAll('input[type=checkbox]:checked')).map(el => SLOT_DISCOVER[+el.dataset.idx]);
  if(!picked.length){ toast('没有勾选任何出口', 'warn'); return; }
  picked.forEach(c => {
    if(PROXY_SLOTS.some(s => s.url === c.url)) return;
    // 导入时就把探测到的出口信息一起带上。名称留空：空名称 = 「按出口自动命名」，
    // 标签由 country/ip_type 每次现算，所以出口换了名字跟着换；写进 name 就成了
    // 用户名称，会被 slot_label() 永久优先，出口变了也还显示旧的国家。
    PROXY_SLOTS.push({
      id: '', name: '', url: c.url, enabled: true, bound: 0,
      ip: c.exit_ip || '', country: c.country || '', country_code: c.country_code || '',
      ip_type: c.ip_type || '', isp: c.isp || '', asn: c.asn || '',
      probed_at: Math.floor(Date.now() / 1000),
    });
  });
  renderProxySlots();
  toast('已加入 ' + picked.length + ' 个槽位，记得点「保存槽位」', 'ok');
}

async function autoAssignSlots(){
  // Only fills in accounts that have no exit yet: an account that is already
  // bound keeps its slot, so this is not a rebalancer.
  const unbound = (window.ACCOUNTS || []).filter(a => a.enabled && !a.proxySlot);
  if(!unbound.length){ toast('所有已启用账号都已绑定代理出口', 'ok'); return; }
  const slots = PROXY_SLOTS.filter(s => s.enabled !== false);
  if(!slots.length){ toast('没有启用的代理槽，请先在「设置 → 账号代理槽」添加并保存', 'warn'); return; }
  let i = 0;
  for(const a of unbound){
    const slot = slots[i % slots.length];
    i++;
    try{ await postJSON('/accounts/set', {uid: a.uid, proxySlot: slot.id}); }catch(e){}
  }
  await loadAccounts();
  await loadProxySlots();
  toast('已为 ' + unbound.length + ' 个未绑定账号分配代理出口', 'ok');
}
