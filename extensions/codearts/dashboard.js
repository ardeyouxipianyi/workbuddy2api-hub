/* Additive dashboard page. Uses the Hub's panel session, theme and navigation. */
(() => {
  'use strict';
  const language = document.documentElement.dataset.uiLanguage || '';
  const en = language.startsWith('en');
  const tw = /TW|Hant/.test(language);
  const t = (zh, english, traditional) => en ? english : (tw && traditional ? traditional : zh);
  const nav = document.getElementById('btnNavSettings');
  const gateway = document.getElementById('pageGateway');
  if (!nav || !gateway || typeof window.switchMainTab !== 'function') return;
  const button = document.createElement('button');
  button.id = 'btnNavCodearts';
  button.className = 'main-nav-btn';
  button.type = 'button';
  button.textContent = 'CodeArts';
  nav.before(button);
  const page = document.createElement('div');
  page.id = 'pageCodearts';
  page.className = 'main-page';
  page.setAttribute('data-no-i18n', '');
  gateway.after(page);
  const card = document.createElement('section');
  card.className = 'card';
  card.style.cssText = 'padding:24px;margin:16px 0;max-width:1100px';
  page.append(card);
  const title = document.createElement('h2');
  title.textContent = t('CodeArts 网关', 'CodeArts gateway', 'CodeArts 網關');
  card.append(title);
  const description = document.createElement('p');
  description.textContent = t(
    '连接独立 CodeArts2API 服务，模型使用 codearts/ 前缀。华为账号登录、轮转与续期由该服务负责。',
    'Connect a separate CodeArts2API service. Models use the codearts/ prefix. Huawei account login, rotation and refresh belong to that service.',
    '連接獨立 CodeArts2API 服務，模型使用 codearts/ 前綴。華為帳號登入、輪轉與續期由該服務負責。');
  card.append(description);
  const form = document.createElement('form');
  form.style.cssText = 'display:grid;gap:14px;max-width:720px;margin-top:20px';
  const fields = {};
  function field(name, label, type) {
    const wrap = document.createElement('label');
    wrap.style.cssText = 'display:grid;gap:6px';
    const text = document.createElement('span');
    text.textContent = label;
    const input = document.createElement('input');
    input.type = type || 'text';
    input.id = 'ca-' + name;
    input.name = name;
    if (type === 'password') input.autocomplete = 'new-password';
    if (type === 'number') { input.min = '1'; input.max = '3600'; }
    if (type === 'checkbox') {
      wrap.style.cssText = 'display:flex;align-items:center;gap:10px';
      input.style.width = 'auto';
    } else {
      input.style.cssText = 'width:100%;min-height:38px;padding:8px 10px;box-sizing:border-box;border:1px solid var(--border);border-radius:8px;background:var(--bg2);color:var(--text)';
    }
    fields[name] = input;
    wrap.append(text, input);
    form.append(wrap);
  }
  field('enabled', t('启用 CodeArts', 'Enable CodeArts', '啟用 CodeArts'), 'checkbox');
  field('base_url', t('CodeArts 服务地址', 'CodeArts service URL', 'CodeArts 服務位址'), 'url');
  field('api_key', t('服务密钥（留空保留已保存值）', 'Service API key (leave blank to keep saved value)', '服務金鑰（留空保留已儲存值）'), 'password');
  field('api_key_env', t('密钥环境变量', 'API key environment variable', '金鑰環境變數'));
  field('timeout_seconds', t('请求超时（秒）', 'Request timeout (seconds)', '請求逾時（秒）'), 'number');
  const secretState = document.createElement('small');
  form.append(secretState);
  function action(parent, text, callback) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn';
    btn.textContent = text;
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try { await callback(); } catch (error) { status.textContent = error.message; }
      finally { btn.disabled = false; }
    });
    parent.append(btn);
    return btn;
  }
  const controls = document.createElement('div');
  controls.style.cssText = 'display:flex;flex-wrap:wrap;gap:8px';
  const save = document.createElement('button');
  save.type = 'submit';
  save.className = 'btn';
  save.textContent = t('保存设置', 'Save settings', '儲存設定');
  controls.append(save);
  action(controls, t('刷新状态', 'Refresh status', '重新整理狀態'), () => load());
  form.append(controls);
  card.append(form);
  const status = document.createElement('p');
  status.id = 'ca-status';
  status.role = 'status';
  status.style.whiteSpace = 'pre-wrap';
  card.append(status);
  const stats = document.createElement('p');
  card.append(stats);
  const actions = document.createElement('div');
  actions.style.cssText = 'display:flex;flex-wrap:wrap;gap:8px';
  card.append(actions);
  let session = '';
  const admin = (name, body = {}) => window.postJSON('/codearts/admin/' + name, body);
  action(actions, t('登录华为账号', 'Sign in to Huawei', '登入華為帳號'), async () => {
    const result = await admin('oauth/start');
    const url = new URL(result.auth_url);
    if (!['https:', 'http:'].includes(url.protocol)) throw new Error('Invalid authorization URL');
    session = result.session_id;
    const link = document.createElement('a');
    link.href = url.href;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = t('打开华为授权页面', 'Open Huawei authorization page', '開啟華為授權頁面');
    status.replaceChildren(link);
  });
  action(actions, t('检查登录结果', 'Check sign-in result', '檢查登入結果'), async () => {
    if (!session) throw new Error(t('请先发起登录', 'Start sign-in first', '請先發起登入'));
    const result = await admin('oauth/poll', {session_id: session});
    status.textContent = result.message || result.status;
    if (result.status === 'done') await load();
  });
  action(actions, t('全员签到', 'Check in all accounts', '全員簽到'), async () => {
    const result = await admin('checkin'); status.textContent = result.message || JSON.stringify(result);
  });
  action(actions, t('重载账号', 'Reload accounts', '重新載入帳號'), async () => {
    await admin('reload'); await load();
  });
  const accounts = document.createElement('div');
  const models = document.createElement('div');
  card.append(accounts, models);
  const test = document.createElement('div');
  test.style.cssText = 'display:grid;gap:8px;margin-top:20px';
  const modelLabel = document.createElement('label');
  modelLabel.textContent = t('测试模型', 'Test model', '測試模型');
  const select = document.createElement('select');
  select.id = 'ca-test-model';
  modelLabel.append(select);
  const promptLabel = document.createElement('label');
  promptLabel.textContent = t('测试消息', 'Test message', '測試訊息');
  const prompt = document.createElement('textarea');
  prompt.value = 'Reply with OK.';
  prompt.id = 'ca-test-message';
  promptLabel.append(prompt);
  const answer = document.createElement('pre');
  answer.id = 'ca-test-result';
  answer.style.cssText = 'white-space:pre-wrap;overflow-wrap:anywhere';
  test.append(modelLabel, promptLabel);
  action(test, t('发送测试请求', 'Send test request', '傳送測試請求'), async () => {
    if (!select.value) throw new Error(t('请先刷新模型列表', 'Refresh models first', '請先重新整理模型清單'));
    const result = await window.postJSON('/v1/chat/completions', {
      model: select.value, messages: [{role: 'user', content: prompt.value}], stream: false
    });
    answer.textContent = result.choices?.[0]?.message?.content || JSON.stringify(result);
  });
  test.append(answer);
  card.append(test);
  const originalSwitch = window.switchMainTab;
  if (typeof MAIN_TABS !== 'undefined' && !MAIN_TABS.includes('codearts')) MAIN_TABS.push('codearts');
  window.switchMainTab = function(tab) {
    page.classList.remove('active');
    button.classList.remove('active');
    if (tab !== 'codearts') return originalSwitch.call(this, tab);
    originalSwitch.call(this, 'gateway');
    document.querySelectorAll('.main-page').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.main-nav-btn').forEach(el => el.classList.remove('active'));
    page.classList.add('active');
    button.classList.add('active');
    window.__MAIN_TAB__ = 'codearts';
    if (typeof currentMainTab !== 'undefined') currentMainTab = 'codearts';
    try { localStorage.setItem('wb-proxy-main-tab', 'codearts'); } catch (error) {}
    const url = new URL(location.href); url.searchParams.set('tab', 'codearts');
    history.replaceState(null, '', url);
    load();
  };
  button.addEventListener('click', () => window.switchMainTab('codearts'));
  form.addEventListener('submit', async event => {
    event.preventDefault(); save.disabled = true;
    try {
      await window.postJSON('/codearts/config', {
        enabled: fields.enabled.checked, base_url: fields.base_url.value,
        api_key: fields.api_key.value, api_key_env: fields.api_key_env.value,
        timeout_seconds: Number(fields.timeout_seconds.value)
      });
      fields.api_key.value = '';
      await load();
    } catch (error) { status.textContent = error.message; }
    finally { save.disabled = false; }
  });
  async function load() {
    try {
      const cfg = await window.getJSON('/codearts/config');
      fields.enabled.checked = cfg.enabled;
      for (const name of ['base_url', 'api_key_env', 'timeout_seconds']) fields[name].value = cfg[name];
      secretState.textContent = cfg.has_api_key ? t('服务密钥已设置', 'Service API key is set', '服務金鑰已設定') : t('尚未设置服务密钥', 'Service API key is missing', '尚未設定服務金鑰');
      status.textContent = cfg.enabled ? t('正在读取服务状态…', 'Loading service status…', '正在讀取服務狀態…') : t('CodeArts 已关闭。保存地址和密钥并启用后可连接服务。', 'CodeArts is disabled. Save the service URL and key, then enable it.', 'CodeArts 已關閉。儲存位址和金鑰並啟用後可連接服務。');
      accounts.replaceChildren(); models.replaceChildren(); select.replaceChildren();
      if (!cfg.enabled) return;
      const [overview, catalog, usage] = await Promise.all([
        window.getJSON('/codearts/overview'), window.getJSON('/codearts/models'), window.getJSON('/codearts/usage')
      ]);
      status.textContent = t('CodeArts 服务已连接', 'Connected to CodeArts', 'CodeArts 服務已連接');
      stats.textContent = `${t('账号', 'Accounts', '帳號')}: ${overview.stats?.total || 0} · ${t('可用', 'Healthy', '可用')}: ${overview.stats?.healthy || 0} · ${t('请求', 'Requests', '請求')}: ${usage.requests} · Tokens: ${usage.total_tokens} (${t('最近 5000 条网关记录', 'last 5000 gateway records', '最近 5000 筆網關紀錄')})`;
      for (const account of overview.accounts || []) {
        const row = document.createElement('div');
        row.style.cssText = 'display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin-top:12px';
        const name = document.createElement('span');
        name.textContent = `${account.nickname || account.uid} · ${account.disabled ? t('已停用', 'Disabled', '已停用') : t('已启用', 'Enabled', '已啟用')} · ${account.token_remaining || ''}`;
        row.append(name);
        action(row, account.disabled ? t('启用', 'Enable', '啟用') : t('停用', 'Disable', '停用'), async () => {
          await admin('accounts/' + (account.disabled ? 'enable' : 'disable'), {uid: account.uid}); await load();
        });
        action(row, t('续期', 'Refresh token', '續期'), async () => { await admin('keepalive', {uid: account.uid}); await load(); });
        action(row, t('签到', 'Check in', '簽到'), async () => { const r = await admin('checkin', {uid: account.uid}); status.textContent = r.message || JSON.stringify(r); });
        accounts.append(row);
      }
      const heading = document.createElement('h3');
      heading.textContent = t('可用模型', 'Available models', '可用模型');
      models.append(heading);
      for (const model of catalog.data || []) {
        const line = document.createElement('div'); line.textContent = model.id; models.append(line);
        const option = document.createElement('option'); option.value = model.id; option.textContent = model.id; select.append(option);
      }
    } catch (error) { status.textContent = t('连接失败：', 'Connection failed: ', '連接失敗：') + error.message; }
  }
  if (window.__CODEARTS_REQUESTED__) {
    window.__MAIN_TAB__ = 'codearts';
    // Native early-paint listeners run on DOMContentLoaded. Restore after
    // those listeners so the old gateway page is not active alongside ours.
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => window.switchMainTab('codearts'));
    } else {
      window.switchMainTab('codearts');
    }
  }
})();
