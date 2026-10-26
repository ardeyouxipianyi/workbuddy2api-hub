/* Optional provider UI: native Hub components, isolated data and scoped styles. */
(() => {
  'use strict';
  const nav = document.getElementById('btnNavSettings');
  const gateway = document.getElementById('pageGateway');
  if (!nav || !gateway || typeof window.switchMainTab !== 'function') return;
  const lang = () => window.WB_I18N?.current?.() || document.documentElement.dataset.uiLanguage || 'zh';
  const t = (zh, en, tw) => lang().startsWith('en') ? en : (/Hant|TW/.test(lang()) ? (tw || zh) : zh);
  const labels = new Map();
  const node = (tag, cls = '', text = '') => {
    const el = document.createElement(tag); el.className = cls; el.textContent = String(text); return el;
  };
  const labeled = (el, ...words) => { labels.set(el, words); el.textContent = t(...words); return el; };
  const missing = () => t('未提供', 'Not provided', '未提供');
  const page = node('div', 'main-page'); page.id = 'pageCodearts'; page.setAttribute('data-no-i18n', ''); gateway.after(page);
  const button = node('button', 'main-nav-btn', 'CodeArts'); button.id = 'btnNavCodearts'; button.type = 'button'; nav.before(button);
  const state = {config: null, overview: null, catalog: null, usage: null};
  const backendIdentity = () => JSON.stringify([state.config?.enabled,state.config?.base_url,state.config?.api_key_env,state.config?.has_api_key]);
  let pendingAdmin = 0, generation = 0, refreshing = false, configReady = false, overviewFresh = false;
  let session = '', dirty = false, revision = 0, loading = null, initialized = false;
  let connection = ['尚未连接', 'Not connected', '尚未連線'];
  let connectionKind = 'warn';
  const backendActions = [];
  const header = node('div', 'ca-header');
  const heading = node('div');
  heading.append(node('h2', 'ca-title', 'CodeArts'), labeled(node('p', 'hint'),
    '华为账号与模型独立管理，客户端统一使用当前网关。',
    'Manage Huawei accounts and models here. Clients share the current gateway.',
    '華為帳號與模型獨立管理，用戶端統一使用目前閘道。'));
  const headerActions = node('div', 'toolbar');
  const badge = node('span', 'badge'); badge.id = 'ca-connection-badge'; headerActions.append(badge);
  header.append(heading, headerActions); page.append(header);
  const status = node('p', 'hint'); status.id = 'ca-status'; status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  page.append(status);
  const stats = node('div', 'cards');
  const statValues = {};
  for (const [key, words] of Object.entries({total:['账号数','Accounts','帳號數'],healthy:['可用账号','Healthy accounts','可用帳號'],requests:['请求数','Requests','請求數'],tokens:['总 Tokens','Total tokens','總 Tokens']})) {
    const card = node('div', 'card'); const value = node('div', 'v', '—'); statValues[key] = value;
    card.append(labeled(node('div', 'k'), ...words), value);
    if (key === 'requests' || key === 'tokens') card.append(labeled(node('div', 's'), '最近 5000 条网关记录', 'Last 5000 gateway records', '最近 5000 筆閘道紀錄'));
    stats.append(card);
  }
  page.append(stats);
  function section(id, words, collapsible = false, open = true) {
    const outer = node('section'); outer.id = id;
    const h = node('h2'); const body = node('div', 'ca-section-body'); body.id = id + '-body';
    let toggle;
    function setOpen(value) { body.hidden = !value; if (toggle) toggle.setAttribute('aria-expanded', String(value)); }
    if (collapsible) {
      toggle = node('button', 'disclosure'); toggle.type = 'button'; toggle.setAttribute('aria-controls', body.id);
      const chevron = node('span', 'disclosure-chevron'); chevron.setAttribute('aria-hidden', 'true');
      toggle.append(chevron, labeled(node('span'), ...words)); toggle.addEventListener('click', () => setOpen(body.hidden)); h.append(toggle);
    } else h.append(labeled(node('span'), ...words));
    outer.append(h, body); page.append(outer); setOpen(open); return {outer, body, setOpen};
  }
  function group(parent) { const el = node('div', 'tb-group'); parent.append(el); return el; }
  function notify(text, bad = false) { status.textContent = text; status.className = bad ? 'hint ca-error' : 'hint'; }
  function updateActions() {
    for (const btn of backendActions) btn.disabled = !state.config?.enabled || !configReady || refreshing || pendingAdmin > 0 || (btn.dataset.accountAction === '1' && !overviewFresh) || btn.dataset.busy === '1' || (btn === poll && !session);
    const selected = state.catalog?.data?.find(m=>m.id===select.value);
    imageNote.textContent = selected?.supports_images===false?t('目录未声明该通道支持图片，仍可发送实测；具体结果以上游返回为准。','This catalog does not declare image support for this route. Testing remains available; the upstream response is authoritative.','目錄未宣告此通道支援圖片，仍可傳送實測；具體結果以上游回傳為準。'):'';
    send.disabled = !state.config?.enabled || !configReady || !overviewFresh || refreshing || !select.value || send.dataset.busy === '1';
  }
  function action(parent, words, callback, cls = 'sec', backend = false, id = '') {
    const btn = labeled(node('button', cls), ...words); btn.type = 'button'; if (id) btn.id = id;
    btn.addEventListener('click', async () => {
      if (btn.disabled) return;
      btn.dataset.busy = '1'; btn.disabled = true;
      try { await callback(); } catch (error) { notify(error.message, true); }
      finally { btn.dataset.busy = ''; btn.disabled = false; updateActions(); }
    });
    parent.append(btn); if (backend) { backendActions.push(btn); if (backend === 'account') btn.dataset.accountAction = '1'; } return btn;
  }
  action(headerActions, ['刷新状态','Refresh status','重新整理狀態'], () => load());
  const accountSection = section('ca-account-section', ['华为账号','Huawei accounts','華為帳號']);
  const toolbar = node('div', 'toolbar'); accountSection.body.append(toolbar);
  const loginGroup = group(toolbar), accountGroup = group(toolbar);
  const admin = async (name, body = {}) => {
    pendingAdmin++; updateActions();
    try { return await window.postJSON('/codearts/admin/' + name, body); }
    finally { pendingAdmin--; updateActions(); }
  };
  async function reloadAfterChange() { if (loading) await loading; return load(); }
  action(loginGroup, ['登录华为账号','Sign in to Huawei','登入華為帳號'], async () => {
    const result = await window.CodeArtsLogin.start(() => window.open('about:blank', '_blank'), () => admin('oauth/start'));
    session = result.sessionId; updateActions();
    const link = labeled(node('a'), '打开华为授权页面', 'Open Huawei authorization page', '開啟華為授權頁面');
    link.href = result.authUrl; link.target = '_blank'; link.rel = 'noopener noreferrer'; status.replaceChildren(link);
    if (!result.popupOpened) status.append(labeled(node('span'), '（弹窗被阻止，请点击链接）',' (popup blocked; use the link)','（彈出視窗遭封鎖，請點擊連結）'));
  }, '', true);
  const poll = action(loginGroup, ['检查登录结果','Check sign-in result','檢查登入結果'], async () => {
    if (!session) throw Error(t('请先发起登录','Start sign-in first','請先發起登入'));
    const result = await admin('oauth/poll', {session_id: session}); notify(result.message || result.status);
    if (result.status === 'done') { session = ''; await reloadAfterChange(); }
  }, 'sec', true);
  action(accountGroup, ['全员签到','Check in all accounts','全員簽到'], async () => {
    const result = await admin('checkin'); notify(result.message || t('签到请求已完成','Check-in request completed','簽到請求已完成'));
  }, 'sec', true);
  action(accountGroup, ['重载账号','Reload accounts','重新載入帳號'], async () => { await admin('reload'); await reloadAfterChange(); }, 'sec', true);
  const accountError = node('p', 'hint ca-error'); accountSection.body.append(accountError);
  function table(parent, id, columns) {
    const wrap = node('div', 'table-wrap'), table = node('table', 'data-cards'); table.id = id;
    const head = node('thead'), row = node('tr'), body = node('tbody');
    for (const words of columns) row.append(labeled(node('th', 'ca-left'), ...words));
    head.append(row); table.append(head, body); wrap.append(table); parent.append(wrap); return body;
  }
  const accountColumns = [['账号','Account','帳號'],['状态','Status','狀態'],['Token 剩余时间','Token time remaining','Token 剩餘時間'],['操作','Actions','操作']];
  const accountBody = table(accountSection.body, 'ca-accounts-table', accountColumns);
  const modelSection = section('ca-model-section', ['模型目录','Model catalog','模型目錄']);
  modelSection.body.append(labeled(node('p','hint'), '目录状态不代表调用结果；点击“测试”验证当前模型。', 'Catalog membership is not a successful request. Use Test to check a model.', '目錄狀態不代表呼叫結果；點擊「測試」驗證目前模型。'));
  const modelError = node('p', 'hint ca-error'); modelError.id = 'ca-models-error'; modelSection.body.append(modelError);
  const modelColumns = [['模型','Model','模型'],['客户端模型 ID','Client model ID','用戶端模型 ID'],['目录状态','Catalog status','目錄狀態'],['操作','Actions','操作']];
  const modelBody = table(modelSection.body, 'ca-models-table', modelColumns);
  const testing = section('ca-testing', ['请求测试','Request test','請求測試'], true, false);
  const testGrid = node('div','ca-form-grid'); testing.body.append(testGrid);
  function control(parent, id, words, tag = 'input', type = 'text') {
    const label = node('label','ca-field'); const input = node(tag); input.id = id;
    if (tag === 'input') input.type = type;
    if (tag === 'textarea') label.classList.add('ca-wide');
    label.append(labeled(node('span'), ...words), input); parent.append(label); return input;
  }
  const select = control(testGrid, 'ca-test-model', ['测试模型','Test model','測試模型'], 'select');
  const imageURL = control(testGrid, 'ca-image-url', ['图片 URL（可选）','Image URL (optional)','圖片 URL（選填）'], 'input', 'url');
  const imageFile = control(testGrid, 'ca-image-file', ['上传图片（可选，最大 4 MiB）','Upload image (optional, max 4 MiB)','上傳圖片（選填，最大 4 MiB）'], 'input', 'file');
  imageFile.accept = 'image/png,image/jpeg,image/webp,image/gif';
  const imageDetail = control(testGrid, 'ca-image-detail', ['图片细节 detail','Image detail','圖片細節 detail'], 'select');
  for (const value of ['auto','low','high']) { const option=node('option','',value); option.value=value; imageDetail.append(option); } imageDetail.value='auto';
  testing.body.append(labeled(node('p','hint'), '图片 URL 与上传二选一；支持 PNG/JPEG/WebP/GIF。detail 原样传递，实际行为取决于模型。音频、视频和图片生成暂不支持。', 'Choose an image URL or upload PNG/JPEG/WebP/GIF. Detail is forwarded as supplied; behavior depends on the model. Audio, video and image generation are not supported.', '圖片 URL 與上傳二選一；支援 PNG/JPEG/WebP/GIF。detail 原樣傳遞，實際行為取決於模型。音訊、影片與圖片生成暫不支援。'));
  const prompt = control(testGrid, 'ca-test-message', ['测试消息','Test message','測試訊息'], 'textarea'); prompt.value = 'Reply with OK.'; prompt.rows = 3;
  const imageNote = node('p','hint'); imageNote.id='ca-image-capability-note'; imageNote.setAttribute('role','status'); testing.body.append(imageNote);
  const testMeta = node('p','hint'); testMeta.setAttribute('aria-live','polite');
  const answer = node('pre','ca-result'); answer.id = 'ca-test-result'; answer.setAttribute('aria-live','polite');
  labeled(answer, '尚未发送请求','No request sent yet','尚未傳送請求');
  const send = action(testing.body, ['发送测试请求','Send test request','傳送測試請求'], async () => {
    try {
      if (!select.value || !prompt.value.trim()) throw Error(t('请选择模型并填写消息','Select a model and enter a message','請選擇模型並填寫訊息'));
      const testGeneration = generation, testBackend = backendIdentity();
      const model = select.value, message = prompt.value, detail = imageDetail.value;
      const file = imageFile.files?.[0]; let url = imageURL.value.trim();
      if (file && url) throw Error(t('请只选择图片 URL 或上传文件','Choose either an image URL or a file','請只選擇圖片 URL 或上傳檔案'));
      if (file || url) {
        if (file) {
          if (!['image/png','image/jpeg','image/webp','image/gif'].includes(file.type) || !file.size || file.size > 4*1024*1024) throw Error(t('图片须为 PNG/JPEG/WebP/GIF 且不超过 4 MiB','Use PNG/JPEG/WebP/GIF, nonempty and at most 4 MiB','圖片須為 PNG/JPEG/WebP/GIF 且不超過 4 MiB'));
          url = await new Promise((resolve,reject)=>{ const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(Error(t('读取图片失败','Failed to read image','讀取圖片失敗')));reader.onabort=()=>reject(Error(t('读取图片已取消','Image read cancelled','圖片讀取已取消')));reader.readAsDataURL(file); });
        } else {
          let parsed;try {parsed=new URL(url);}catch{throw Error(t('图片 URL 无效','Invalid image URL','圖片 URL 無效'));}
          if (!['http:','https:'].includes(parsed.protocol) || parsed.username || parsed.password) throw Error(t('图片 URL 必须是无凭证的 HTTP(S) 地址','Use an HTTP(S) image URL without credentials','圖片 URL 必須是無憑證的 HTTP(S) 位址'));
        }
      }
      if (generation !== testGeneration || testBackend !== backendIdentity()) throw Error(t('连接设置已变更，请重新发送','Connection settings changed; send again','連線設定已變更，請重新傳送'));
      const content = url ? [{type:'text',text:message},{type:'image_url',image_url:{url,detail}}] : message;
      labels.delete(answer); const started = performance.now();
      answer.textContent = t('正在等待模型返回…','Waiting for the model…','正在等待模型回傳…'); testMeta.textContent = '';
      const result = await window.postJSON('/v1/chat/completions', {model, messages:[{role:'user', content}], stream:false});
      if (generation !== testGeneration || testBackend !== backendIdentity()) return;
      answer.textContent = result.choices?.[0]?.message?.content || t('请求完成，但未返回正文。','Request completed without final text.','請求完成，但未回傳正文。');
      const used = result.usage?.total_tokens;
      testMeta.textContent = `${((performance.now() - started) / 1000).toFixed(2)} s` + (used == null ? '' : ` · Tokens: ${used}`);
    } catch (error) { labels.delete(answer); answer.textContent = error.message; testMeta.textContent = t('请求失败','Request failed','請求失敗'); answer.scrollIntoView({block:'nearest',behavior:'smooth'}); throw error; }
  }, '', false, 'ca-send-test');
  testing.body.append(testMeta, answer); select.addEventListener('change', updateActions);
  const settings = section('ca-settings', ['连接设置','Connection settings','連線設定'], true, false);
  const endpoint = node('code','ca-endpoint', location.origin + '/v1'); settings.body.append(endpoint);
  settings.body.append(labeled(node('p','hint'), '客户端模型示例：codeart/GLM-5.3。前缀仅用于网关路由。', 'Client model example: codeart/GLM-5.3. The prefix is used only for gateway routing.', '用戶端模型範例：codeart/GLM-5.3。前綴僅用於閘道路由。'));
  const form = node('form'); form.id = 'ca-settings-form'; settings.body.append(form);
  const fields = {};
  const enabledLabel = node('label','ca-enabled');
  fields.enabled = node('input'); fields.enabled.type = 'checkbox'; fields.enabled.id = 'ca-enabled';
  enabledLabel.append(fields.enabled, labeled(node('span'), '启用 CodeArts','Enable CodeArts','啟用 CodeArts')); form.append(enabledLabel);
  const formGrid = node('div','ca-form-grid'); form.append(formGrid);
  fields.base_url = control(formGrid,'ca-base_url',['CodeArts 服务地址','CodeArts service URL','CodeArts 服務位址'],'input','url'); fields.base_url.required = true;
  fields.api_key = control(formGrid,'ca-api_key',['服务密钥（留空保留）','Service key (leave blank to keep)','服務金鑰（留空保留）'],'input','password'); fields.api_key.autocomplete = 'new-password';
  const secret = node('p','hint'); form.append(secret);
  const advanced = node('details','ca-advanced'); advanced.append(labeled(node('summary'),'高级设置','Advanced settings','進階設定')); form.append(advanced);
  const advancedGrid = node('div','ca-form-grid'); advanced.append(advancedGrid);
  fields.api_key_env = control(advancedGrid,'ca-api_key_env',['密钥环境变量','API key environment variable','金鑰環境變數']);
  fields.timeout_seconds = control(advancedGrid,'ca-timeout_seconds',['请求超时（秒）','Request timeout (seconds)','請求逾時（秒）'],'input','number');
  fields.timeout_seconds.min = '1'; fields.timeout_seconds.max = '3600'; fields.timeout_seconds.required = true;
  const includeLabel = node('label','ca-checkbox'); fields.catalog_include_images = node('input'); fields.catalog_include_images.id='ca-catalog_include_images'; fields.catalog_include_images.type='checkbox';
  includeLabel.append(fields.catalog_include_images,labeled(node('span'),'加入目录确认支持图片的模型','Include models explicitly declared image capable','加入目錄確認支援圖片的模型')); advanced.append(includeLabel);
  const formActions = node('div','toolbar'); form.append(formActions);
  const save = labeled(node('button'), '保存设置','Save settings','儲存設定'); save.type = 'submit'; formActions.append(save);
  const editStatus = node('span','hint'); formActions.append(editStatus);
  function markDirty() { dirty = true; revision++; labeled(editStatus, '有未保存的修改','Unsaved changes','有未儲存的修改'); }
  for (const input of Object.values(fields)) { input.addEventListener('input', markDirty); input.addEventListener('change', markDirty); }
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (save.disabled) return;
    const savedRevision = revision; save.disabled = true;
    try {
      await window.postJSON('/codearts/config', {enabled:fields.enabled.checked,catalog_include_images:fields.catalog_include_images.checked,base_url:fields.base_url.value,api_key:fields.api_key.value,api_key_env:fields.api_key_env.value,timeout_seconds:Number(fields.timeout_seconds.value)});
      generation++; configReady = false; overviewFresh = false;
      state.overview = state.catalog = state.usage = null; render();
      if (revision === savedRevision) { revision++; dirty = false; fields.api_key.value = ''; labeled(editStatus, '已保存','Saved','已儲存'); }
      await reloadAfterChange();
    } catch (error) { notify(error.message, true); }
    finally { save.disabled = false; }
  });
  function cell(row, words, content) {
    const td = node('td','ca-left'); td.setAttribute('data-label', t(...words));
    if (typeof content === 'string') td.textContent = content; else td.append(content);
    row.append(td); return td;
  }
  function empty(body, columns, words) {
    const row = node('tr'), td = labeled(node('td','ca-empty'), ...words); td.colSpan = columns.length; row.append(td); body.append(row);
  }
  function clearRows(body) {
    const forget = el => { labels.delete(el); for (const child of el.children) forget(child); };
    for (const child of body.children) forget(child);
    body.replaceChildren();
  }
  function renderAccounts() {
    clearRows(accountBody);
    const accounts = state.overview?.accounts || [];
    if (!accounts.length) return empty(accountBody, accountColumns, state.config?.enabled ? ['暂无账号，请登录华为账号','No accounts. Sign in to Huawei to get started.','暫無帳號，請登入華為帳號'] : ['启用并连接 CodeArts 后显示账号','Enable and connect CodeArts to view accounts','啟用並連線 CodeArts 後顯示帳號']);
    for (const account of accounts) {
      const row = node('tr'), identity = node('div'); identity.append(node('div','',account.nickname || account.uid || missing()));
      if (account.nickname && account.uid) identity.append(node('small','ca-muted',account.uid));
      cell(row,accountColumns[0],identity);
      cell(row,accountColumns[1],node('span','badge '+(account.disabled?'warn':'ok'),account.disabled?t('已停用','Disabled','已停用'):t('已启用','Enabled','已啟用')));
      cell(row,accountColumns[2],account.token_remaining == null || account.token_remaining === '' ? missing() : String(account.token_remaining));
      const controls = node('div','ca-row-actions');
      action(controls,['续期','Refresh token','續期'],async()=>{await admin('keepalive',{uid:account.uid});await reloadAfterChange();},'sec mini','account');
      action(controls,['签到','Check in','簽到'],async()=>{const r=await admin('checkin',{uid:account.uid});notify(r.message || t('签到请求已完成','Check-in request completed','簽到請求已完成'));},'sec mini','account');
      action(controls,account.disabled?['启用','Enable','啟用']:['停用','Disable','停用'],async()=>{await admin('accounts/'+(account.disabled?'enable':'disable'),{uid:account.uid});await reloadAfterChange();},account.disabled?'sec mini':'danger mini','account');
      cell(row,accountColumns[3],controls); accountBody.append(row);
    }
  }
  function renderModels() {
    clearRows(modelBody); const old = select.value; select.replaceChildren(); const models = state.catalog?.data || [];
    if (!models.length) empty(modelBody,modelColumns,['暂无模型，启用连接后刷新状态','No models. Enable the connection and refresh status.','暫無模型，啟用連線後重新整理狀態']);
    for (const model of models) {
      const row = node('tr');
      const identity = node('div'); identity.append(node('div','',model.name || model.id.replace(/^codearts?\//,'')));
      const params=[];
      if(Number.isFinite(model.contextWindow) && model.contextWindow>0)params.push(t('上下文 ','Context ','上下文 ')+model.contextWindow);
      if(Number.isFinite(model.maxTokens) && model.maxTokens>0)params.push(t('最大输出 ','Max output ','最大輸出 ')+model.maxTokens);
      params.push(model.supports_images===true?t('支持图片','Images supported','支援圖片'):model.supports_images===false?t('目录未声明图片支持','Catalog does not declare image support','目錄未宣告圖片支援'):t('图片能力未知','Image capability unknown','圖片能力未知'));
      if(params.length)identity.append(node('small','ca-muted',params.join(' · ')));
      cell(row,modelColumns[0],identity);
      cell(row,modelColumns[1],node('code','ca-model-id',model.id));
      cell(row,modelColumns[2],node('span','badge '+(model.backend_listed===false?'warn':'s'),model.backend_listed===false?t('未列出，已保留','Not listed; retained','未列出，已保留'):t('已列出','Listed','已列出')));
      const controls = node('div','ca-row-actions');
      action(controls,['复制 ID','Copy ID','複製 ID'],async()=>{
        if (!navigator.clipboard?.writeText) throw Error(t('浏览器不支持复制，请手动选择模型 ID','Clipboard unavailable; select the model ID manually','瀏覽器不支援複製，請手動選取模型 ID'));
        await navigator.clipboard.writeText(model.id); notify(t('已复制模型 ID','Model ID copied','已複製模型 ID'));
      },'sec mini');
      action(controls,['测试','Test','測試'],()=>{select.value=model.id;testing.setOpen(true);prompt.focus();updateActions();},'sec mini');
      cell(row,modelColumns[3],controls); modelBody.append(row);
      const option = node('option','',model.id); option.value=model.id; select.append(option);
    }
    if (old && !models.some(m=>m.id===old)) { const option=node('option','',old+' · '+t('目录未列出','not in catalog','目錄未列出'));option.value=old;select.append(option); }
    select.value=old || models[0]?.id || '';
  }
  function render() {
    // Account row controls are ephemeral; avoid retaining detached buttons.
    backendActions.splice(4);
    badge.textContent=t(...connection);badge.className='badge '+connectionKind;
    const number = value => Number.isFinite(value) ? String(value) : '—';
    statValues.total.textContent=number(state.overview?.stats?.total);statValues.healthy.textContent=number(state.overview?.stats?.healthy);
    statValues.requests.textContent=number(state.usage?.requests);statValues.tokens.textContent=number(state.usage?.total_tokens);
    renderAccounts();renderModels();updateActions();
  }
  async function load() {
    if (loading) return loading;
    const readGeneration = generation; refreshing = true; updateActions();
    loading = (async()=>{
      const before = revision;
      try {
        const cfg = await window.getJSON('/codearts/config');
        if (readGeneration !== generation) return;
        const changed = state.config && (cfg.base_url !== state.config.base_url || cfg.api_key_env !== state.config.api_key_env || cfg.has_api_key !== state.config.has_api_key);
        if (changed) { state.overview = state.catalog = state.usage = null; overviewFresh = false; }
        state.config = cfg; configReady = true;
        if (changed) render();
        if (!dirty && revision === before) {
          fields.enabled.checked=cfg.enabled;
          fields.catalog_include_images.checked=!!cfg.catalog_include_images;
          for (const name of ['base_url','api_key_env','timeout_seconds']) fields[name].value=cfg[name];
        }
        labeled(secret, ...(cfg.has_api_key?['服务密钥已设置','Service key is set','服務金鑰已設定']:['尚未设置服务密钥','Service key is missing','尚未設定服務金鑰']));
        if (!initialized) { settings.setOpen(!cfg.enabled || !cfg.has_api_key); initialized=true; }
        if (!cfg.enabled) {
          state.overview=state.catalog=state.usage=null;overviewFresh=false;accountError.textContent=modelError.textContent='';connection=['已关闭','Disabled','已關閉'];connectionKind='warn';render();return;
        }
        connection=['正在读取状态…','Loading status…','正在讀取狀態…'];connectionKind='s';badge.textContent=t(...connection);
        const parts = [['overview','/codearts/overview',accountError],['catalog','/codearts/models',modelError],['usage','/codearts/usage',null]];
        const values = await Promise.allSettled(parts.map(([,path])=>window.getJSON(path)));
        if (readGeneration !== generation) return;
        overviewFresh = values[0].status === 'fulfilled';
        const failures=[];
        values.forEach((result,i)=>{
          const [key,,errorBox]=parts[i];
          if (result.status==='fulfilled') { state[key]=result.value;if(errorBox)errorBox.textContent=''; }
          else { const message=result.reason?.message || String(result.reason);if(errorBox)errorBox.textContent=message;failures.push(message); }
        });
        connection=values[0].status==='fulfilled'?['服务已连接','Service connected','服務已連線']:['服务连接失败','Service connection failed','服務連線失敗'];connectionKind=failures.length?'warn':'ok';
        if(failures.length)notify(failures.join(' · '),true);
        render();
      } catch(error) {configReady=false;overviewFresh=false;connection=['无法读取配置','Could not load settings','無法讀取設定'];connectionKind='bad';notify(error.message,true);render();}
    })();
    try { await loading; } finally { loading=null; refreshing=false; updateActions(); }
  }
  if (typeof MutationObserver !== 'undefined') new MutationObserver(()=>{
    for(const [el,words] of labels)el.textContent=t(...words);
    if(dirty)editStatus.textContent=t('有未保存的修改','Unsaved changes','有未儲存的修改');
    render();
  }).observe(document.documentElement,{attributes:true,attributeFilter:['data-lang']});
  const originalSwitch=window.switchMainTab;
  if(typeof MAIN_TABS!=='undefined'&&!MAIN_TABS.includes('codearts'))MAIN_TABS.push('codearts');
  window.switchMainTab=function(tab){
    page.classList.remove('active');button.classList.remove('active');
    if(tab!=='codearts')return originalSwitch.call(this,tab);
    originalSwitch.call(this,'gateway');
    document.querySelectorAll('.main-page').forEach(el=>el.classList.remove('active'));
    document.querySelectorAll('.main-nav-btn').forEach(el=>el.classList.remove('active'));
    page.classList.add('active');button.classList.add('active');window.__MAIN_TAB__='codearts';
    if(typeof currentMainTab!=='undefined')currentMainTab='codearts';
    try{localStorage.setItem('wb-proxy-main-tab','codearts');}catch(error){}
    const url=new URL(location.href);url.searchParams.set('tab','codearts');history.replaceState(null,'',url);
    return load();
  };
  button.addEventListener('click',()=>window.switchMainTab('codearts'));
  render();
  if(window.__CODEARTS_REQUESTED__){
    window.__MAIN_TAB__='codearts';
    if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',()=>window.switchMainTab('codearts'));
    else window.switchMainTab('codearts');
  }
})();
