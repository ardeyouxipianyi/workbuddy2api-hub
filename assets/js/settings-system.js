/* ---- 日志落库 ----
   开关与三条保留上限一次提交；上限框留空就发 null，服务端存成「不限」。
   大小框以 MB 输入，发出去时换算成字节（服务端一律以字节记）。 */
function capInputValue(id){
  const el = document.getElementById(id);
  if(!el) return null;
  const raw = String(el.value == null ? '' : el.value).trim();
  if(raw === '') return null;
  const n = Number(raw);
  if(!isFinite(n) || n <= 0) return null;
  return Math.floor(n);
}
function fillCapInput(id, value){
  const el = document.getElementById(id);
  if(!el) return;
  el.value = (value === null || value === undefined || value === '') ? '' : String(value);
}
function fillCapInputMB(id, bytes){
  const el = document.getElementById(id);
  if(!el) return;
  el.value = (bytes === null || bytes === undefined || bytes === '' || bytes <= 0)
    ? '' : String(Math.max(1, Math.round(bytes / 1048576)));
}
async function saveLogDb(btn){
  const enabledEl = document.getElementById('setLogDbEnabled');
  const rows = capInputValue('setLogDbMaxRows');
  const days = capInputValue('setLogDbMaxDays');
  const mb = capInputValue('setLogDbMaxMB');
  const payload = {
    log_db_enabled: !!(enabledEl && enabledEl.checked),
    log_db_max_rows: rows,
    log_db_max_days: days,
    log_db_max_bytes: mb === null ? null : mb * 1048576,
  };
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', payload);
    toast(payload.log_db_enabled ? '已启用日志落库' : '已关闭日志落库', 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

/* ---- 更新检查与手动安装 ----
   浏览器只请求本机面板接口，不直接下载更新包或访问 GitHub。安装状态由
   root-owned systemd 更新服务回报；自动检查开关永远不会自动安装。 */

/* 200 不等于成功：后端把「检查根本没跑起来」（ok=false）和「这一次查询失败」
   （last_error 非空）都放在 200 的响应体里回来。两处都要看，否则按钮会对着
   一次失败的检查报「已是最新版本」——这正是它以前做的事。 */
function updateCheckFailure(data){
  if(!data) return '响应为空';
  if(data.ok === false) return data.msg || '更新检查未运行';
  return data.last_error || '';
}

function updateStateLabel(data){
  if(!data) return '未知';
  if(updateCheckFailure(data)) return '检查失败';
  if(data.update_available) return '发现新版本';
  if(data.latest_version) return '已是最新';
  return '尚未检查';
}

/* 更新/回退进行中的状态：安装按钮与回退按钮共用同一份，避免两处列表漂移。 */
const INSTALL_BUSY_STATES = ['queued','downloading','verifying','backing_up','stopping',
  'applying','restoring','restarting','checking','rolling_back'];
let LAST_INSTALL_STATE = 'idle';

function applyUpdateStatus(data){
  data = data || {};
  const failure = updateCheckFailure(data);
  const set = (id, value) => { const el = document.getElementById(id); if(el) el.textContent = value || '-'; };
  set('setUpdateCurrent', data.current_version);
  set('setUpdateLatest', data.latest_version);
  set('setUpdateLastCheck', data.last_attempt);
  const state = document.getElementById('setUpdateState');
  if(state){
    state.textContent = '(' + updateStateLabel(data) + ')';
    state.style.color = failure ? 'var(--warn)'
      : data.update_available ? 'var(--accent2)' : 'var(--dim)';
  }
  const enabled = document.getElementById('setUpdateCheckEnabled');
  if(enabled) enabled.checked = data.enabled === true;
  const err = document.getElementById('setUpdateError');
  if(err) err.textContent = failure ? ('上次检查失败：' + failure) : '';
  const installation = data.installation || {};
  const installState = String(installation.state || 'idle');
  LAST_INSTALL_STATE = installState;
  const installLabels = {
    idle: '尚未开始', queued: '等待安装', downloading: '正在下载更新包',
    verifying: '正在校验更新包', backing_up: '正在备份程序文件',
    stopping: '正在停止网关', applying: '正在安装程序',
    restoring: '正在回退到历史版本',
    restarting: '正在重启网关', checking: '正在检查网关',
    succeeded: '更新完成', restored: '回退完成',
    rolling_back: '更新失败，正在回滚',
    rolled_back: '更新已回滚', failed: '更新失败', unavailable: '更新服务未安装'
  };
  const installStatus = document.getElementById('setUpdateInstallStatus');
  if(installStatus){
    const label = installLabels[installState] || '更新状态暂时无法读取';
    installStatus.textContent = label;
  }
  if(err && !failure && (installState === 'failed' || installState === 'rolled_back')){
    const prefix = installState === 'rolled_back' ? '更新已回滚：' : '更新失败：';
    err.textContent = prefix + String(installation.message || '');
  }
  const install = document.getElementById('setUpdateInstallBtn');
  if(install){
    const busy = INSTALL_BUSY_STATES.includes(installState);
    const canInstall = data.install_enabled === true && data.update_available === true
      && !!data.latest_tag && !!data.release_url && !data.checking && !busy
      && installState !== 'unavailable';
    install.disabled = !canInstall;
    install.title = !data.install_enabled ? '更新服务未安装'
      : busy ? '已有更新任务正在运行'
      : !data.update_available ? '当前没有可安装的新版本'
      : (!data.latest_tag || !data.release_url) ? '请先点击立即检查更新' : '';
  }
}

async function loadUpdateStatus(){
  try {
    applyUpdateStatus(await getJSON('/updates'));
  } catch(e) {
    // 读不到更新状态不该影响设置页的其它区块，也不该谎报「已是最新」。
    const state = document.getElementById('setUpdateState');
    if(state){ state.textContent = '(读取失败)'; state.style.color = 'var(--warn)'; }
  }
}

async function checkUpdateNow(btn){
  if(btn) btn.disabled = true;
  try {
    const data = await postJSON('/updates/check', {});
    applyUpdateStatus(data);
    const failure = updateCheckFailure(data);
    if(failure){ toast('检查更新失败: ' + failure, 'bad'); return; }
    toast(data.update_available ? ('发现新版本 ' + data.latest_version) : '已是最新版本', 'ok');
  } catch(e) {
    toast('检查更新失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

async function installUpdateNow(btn){
  if(btn) btn.disabled = true;
  try {
    const current = await getJSON('/updates');
    applyUpdateStatus(current);
    if(current.install_enabled !== true){
      toast('安装更新服务尚未配置', 'bad');
      return;
    }
    if(!current.update_available || !current.latest_tag || !current.release_url){
      toast('请先点击立即检查更新', 'warn');
      return;
    }
    if(btn) btn.disabled = true;
    if(!confirm('安装更新会短暂重启网关。账号与用量数据会保留。继续吗？')) return;
    const accepted = await postJSON('/updates/install', {});
    if(accepted && accepted.ok === false) throw new Error(accepted.msg || '提交更新任务失败');
    toast('已开始安装更新', 'ok');
    await pollUpdateInstall();
  } catch(e) {
    toast('安装更新失败: ' + e.message, 'bad');
    loadUpdateStatus();
  } finally {
    if(btn) btn.disabled = false;
    loadUpdateStatus();
  }
}

async function pollUpdateInstall(){
  for(let i = 0; i < 120; i++){
    await new Promise(resolve => setTimeout(resolve, 2000));
    try {
      const data = await getJSON('/updates');
      applyUpdateStatus(data);
      const state = String((data.installation || {}).state || '');
      if(state === 'succeeded'){
        toast('更新已完成，正在刷新页面', 'ok');
        setTimeout(() => window.location.reload(), 1200);
        return;
      }
      if(state === 'failed' || state === 'rolled_back'){
        const detail = (data.installation || {}).message || '';
        toast((state === 'rolled_back' ? '更新已回滚：' : '更新失败：') + detail, 'bad');
        return;
      }
    } catch(e) {
      // The gateway briefly stops while the root updater replaces its files.
    }
  }
  toast('更新仍在执行，请稍后刷新', 'warn');
}

async function saveUpdateCheckEnabled(btn){
  const el = document.getElementById('setUpdateCheckEnabled');
  const value = !!(el && el.checked);
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {update_check_enabled: value});
    toast(value ? '已启用：每天自动检查更新' : '已关闭：不再自动检查更新', 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

/* ---- 版本回退：列出本机保留的备份，回退到其中一版 ---- */
async function saveKeepBackups(btn){
  const el = document.getElementById('setKeepBackups');
  const value = parseInt((el && el.value) || '1', 10);
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {keep_backups: value});
    toast(value === 0 ? '已保存：不再保留历史版本备份（回退不可用）'
                      : ('已保存：保留 ' + value + ' 份历史版本备份'), 'ok');
    await loadUpdateBackups();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

function renderRollbackRows(data){
  data = data || {};
  const body = document.getElementById('setRollbackRows');
  const hint = document.getElementById('setRollbackHint');
  const state = document.getElementById('setRollbackState');
  const current = String(data.current_version || '');
  const rows = Array.isArray(data.backups) ? data.backups : [];
  const keep = (typeof data.keep_backups === 'number') ? data.keep_backups : 1;
  const enabled = data.rollback_enabled === true;
  const busy = INSTALL_BUSY_STATES.includes(LAST_INSTALL_STATE);
  if(state) state.textContent = current ? ('(当前 ' + current + ')') : '';
  if(body){
    if(!rows.length){
      body.innerHTML = '<tr><td colspan="5" style="color:var(--dim)">没有可回退的本地备份</td></tr>';
    } else {
      body.innerHTML = rows.map(row => {
        const version = String(row.version || '');
        // The onclick argument is built from backend data: keep it to the
        // characters a version can contain, so an odd backup name cannot break
        // out of the attribute.
        const arg = version.replace(/[^0-9.]/g, '');
        const complete = row.complete === true;
        const same = !!version && version === current;
        const reason = !complete ? (row.reason || '备份不完整')
          : same ? '这就是当前版本'
          : !enabled ? '更新服务未安装'
          : busy ? '已有更新任务正在运行' : '';
        const disabled = !!reason;
        const integrity = complete ? '完整'
          : ('<span style="color:var(--warn)">不完整</span>'
             + (row.reason ? '<div style="font-size:11px;color:var(--dim)">' + esc(row.reason) + '</div>' : ''));
        return '<tr>'
          + '<td class="mono">' + esc(version || '-') + '</td>'
          + '<td class="mono">' + esc(row.created_at || '-') + '</td>'
          + '<td>' + esc(String(row.files || 0)) + '</td>'
          + '<td>' + integrity + '</td>'
          + '<td><button class="sec" ' + (disabled ? 'disabled ' : '')
          +   'title="' + esc(reason) + '" '
          +   'onclick="rollbackToVersion(this, \'' + esc(arg) + '\')">回退到此版本</button></td>'
          + '</tr>';
      }).join('');
    }
  }
  if(hint){
    if(keep === 0){
      hint.textContent = '当前设置为不保留备份，回退不可用';
    } else if(!rows.length){
      hint.textContent = '更新成功后才会留下可回退的备份';
    } else {
      hint.textContent = '';
    }
  }
}

async function loadUpdateBackups(showToast){
  const body = document.getElementById('setRollbackRows');
  try {
    const data = await getJSON('/updates/backups');
    renderRollbackRows(data);
  } catch(e) {
    // 老版本后端没有这个接口（404），或者读取本身失败：只让这张卡片降级，
    // 上面的「更新」卡片照旧。
    if(body) body.innerHTML = '<tr><td colspan="5" style="color:var(--warn)">读取备份列表失败</td></tr>';
    if(showToast) toast('读取备份列表失败: ' + e.message, 'bad');
  }
}

async function rollbackToVersion(btn, version){
  if(btn) btn.disabled = true;
  try {
    if(!confirm('回退到 ' + version + ' 会重启网关并中断当前请求，账号与用量数据不受影响。继续吗？')) return;
    const accepted = await postJSON('/updates/rollback', {version: version});
    if(accepted && accepted.ok === false) throw new Error(accepted.msg || '提交回退任务失败');
    toast('已开始回退到 ' + version, 'ok');
    await pollUpdateRollback(version);
  } catch(e) {
    toast('回退失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
    loadUpdateBackups();
  }
}

async function pollUpdateRollback(version){
  for(let i = 0; i < 120; i++){
    await new Promise(resolve => setTimeout(resolve, 2000));
    try {
      const data = await getJSON('/updates');
      applyUpdateStatus(data);
      const installation = data.installation || {};
      const state = String(installation.state || '');
      if(state === 'restored'){
        toast('已回退到 ' + version + '，正在刷新页面', 'ok');
        setTimeout(() => window.location.reload(), 1200);
        return;
      }
      if(state === 'failed' || state === 'rolled_back'){
        toast('回退失败：' + (installation.message || ''), 'bad');
        return;
      }
    } catch(e) {
      // 回退过程中网关会短暂停止，这里的读取失败是预期的。
    }
  }
  toast('回退仍在执行，请稍后刷新', 'warn');
}

/* ---- Model quota scan: watch a chosen set of models on a fixed timer ---- */
let SAVED_QUOTA_SCAN_MODELS = [];
let QUOTA_SCAN_SELECTED = [];

let QUOTA_MODEL_CATALOG = [];

async function loadQuotaScanCatalog(){
  // Only the "all realms" listing can offer every model to watch: MODELS_DATA
  // follows whichever realm tab is open, so half the catalogue would be
  // missing when the panel is showing the other one.
  if(QUOTA_MODEL_CATALOG.length) return QUOTA_MODEL_CATALOG;
  try{
    const r = await getJSON('/v1/models?realm=all');
    QUOTA_MODEL_CATALOG = (r.data || []).filter(m => m && m.id);
  }catch(e){
    QUOTA_MODEL_CATALOG = [];
  }
  // Fall back to whatever the models tab already cached rather than showing
  // an empty box when the listing call fails. Go through window so a test
  // stub that never defined MODELS_DATA reads undefined instead of throwing.
  if(!QUOTA_MODEL_CATALOG.length && Array.isArray(window.MODELS_DATA)){
    QUOTA_MODEL_CATALOG = window.MODELS_DATA.filter(m => m && m.id);
  }
  return QUOTA_MODEL_CATALOG;
}

function renderQuotaScanModels(){
  const box = document.getElementById('quotaScanModels');
  const hint = document.getElementById('quotaScanHint');
  if(!box) return;
  const models = QUOTA_MODEL_CATALOG || [];
  if(!models.length){
    box.innerHTML = '<span style="font-size:12px;color:var(--dim)">暂无可用模型目录</span>';
    if(hint) hint.textContent = '';
    return;
  }
  const selected = {};
  (QUOTA_SCAN_SELECTED || []).forEach(m => { selected[m] = true; });
  box.innerHTML = models.map(m => {
    const id = String(m.id || '');
    if(!id) return '';
    return '<label style="display:flex;align-items:center;gap:5px;font-size:12px;cursor:pointer">'
         + '<input type="checkbox" data-model="' + esc(id) + '"'
         + (selected[id] ? ' checked' : '') + ' style="width:14px;height:14px">'
         + '<span class="mono">' + esc(id) + '</span></label>';
  }).join('');
  // A model that was saved earlier but is no longer published upstream cannot
  // be unchecked through the catalogue; say so rather than silently dropping it.
  const missing = SAVED_QUOTA_SCAN_MODELS.filter(
    m => !models.some(x => String((x && x.id) || '') === m));
  if(hint){
    hint.textContent = missing.length
      ? ('已有 ' + missing.length + ' 个勾选模型不在当前目录中（' + missing.join(', ') + '），保存时保留')
      : ('共 ' + models.length + ' 个模型可选');
  }
}

function toggleAllQuotaScanModels(on){
  const box = document.getElementById('quotaScanModels');
  if(!box) return;
  box.querySelectorAll('input[type=checkbox]').forEach(el => { el.checked = !!on; });
}

async function saveQuotaScanModels(btn){
  const box = document.getElementById('quotaScanModels');
  if(btn){ btn.disabled = true; btn.textContent = '保存中...'; }
  try{
    const checked = [];
    if(box){
      box.querySelectorAll('input[type=checkbox]').forEach(el => {
        if(el.checked && el.dataset.model) checked.push(el.dataset.model);
      });
    }
    const catalogued = {};
    (QUOTA_MODEL_CATALOG || []).forEach(m => { catalogued[String(m.id)] = true; });
    // Keep watch entries the catalogue no longer publishes: they were chosen
    // deliberately and would otherwise vanish on the next save.
    (SAVED_QUOTA_SCAN_MODELS || []).forEach(m => {
      if(m && !catalogued[m] && checked.indexOf(m) < 0) checked.push(m);
    });
    await postJSON('/settings/save', {quota_scan_models: checked});
    toast(checked.length ? ('已保存：定时查询 ' + checked.length + ' 个模型') : '已清空：不再定时查询任何模型', 'ok');
    loadSettings();
  }catch(e){
    toast('保存失败: ' + e.message, 'bad');
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '保存勾选'; }
  }
}

async function runQuotaScan(btn){
  if(btn){ btn.disabled = true; btn.textContent = '查询中...'; }
  try{
    const r = await postJSON('/quota-scan/run', {});
    toast(r.msg || '查询完成', r.ok ? 'ok' : 'bad');
    loadSettings();
  }catch(e){
    toast('查询失败: ' + e.message, 'bad');
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '立即查询'; }
  }
}

function fmtDueSec(sec){
  const s = Math.max(0, Math.round(Number(sec) || 0));
  if(s < 60) return s + ' 秒';
  const m = Math.floor(s / 60);
  if(m < 60) return m + ' 分钟';
  const h = Math.floor(m / 60);
  return h + ' 小时' + (m % 60 ? (m % 60) + ' 分' : '');
}

function renderQuotaScanState(data){
  const stateEm = document.getElementById('setQuotaScanState');
  const box = document.getElementById('quotaScanState');
  const watched = Array.isArray(data.quota_scan_models) ? data.quota_scan_models.length : 0;
  const hours = Number(data.quota_scan_interval_hours || 6);
  if(stateEm){
    stateEm.textContent = watched ? ('(已监视 ' + watched + ' 个模型)') : '(未启用)';
    stateEm.style.color = watched ? 'var(--accent2)' : 'var(--dim)';
  }
  if(!box) return;
  const rows = Array.isArray(data.quota_scans) ? data.quota_scans : [];
  if(!rows.length){
    box.innerHTML = '<div style="font-size:12px;color:var(--dim)">'
      + (watched ? '尚无查询记录，第一条记录会在首次查询后写入。' : '勾选上方模型并保存后开始定时查询。')
      + '</div>';
    return;
  }
  const label = {ok: '正常 (未达限额)', throttled: '已达限额', error: '查询失败'};
  box.innerHTML = '<table><thead><tr><th>模型</th><th>账号</th><th>最近结果</th>'
    + '<th>上次查询</th><th>下次查询</th></tr></thead><tbody>'
    + rows.map(r => {
        const status = r.pending
          ? '<span class="badge s">已登记 429，交给窗口探针</span>'
          : '<span class="badge ' + (r.last_result === 'ok' ? 'ok' : 'warn') + '">'
            + esc(label[r.last_result] || r.last_result || '等待首次查询') + '</span>';
        const last = Number(r.last_scan) || 0
          ? (fmtDueSec((Date.now() / 1000) - Number(r.last_scan)) + ' 前')
          : '尚未查询';
        return '<tr><td class="mono">' + esc(r.model || '-') + '</td>'
             + '<td class="mono">' + esc(r.uid8 || '-') + '</td>'
             + '<td>' + status + '</td><td>' + last + '</td>'
             + '<td>' + (r.pending ? '-' : fmtDueSec(r.due_in) + ' 后') + '</td></tr>';
      }).join('')
    + '</tbody></table>'
    + '<div style="font-size:12px;color:var(--dim);margin-top:8px">查询周期：每 '
    + hours + ' 小时；已登记 429 窗口的配额线不再轮询。</div>';
}
