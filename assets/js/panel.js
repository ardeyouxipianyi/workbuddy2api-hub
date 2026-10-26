/* ---- panel password gate ---- */
function isAuthError(err){
  // Every 401/403 from the JSON helpers carries this message; the gate is
  // the single place that explains it, so callers stay quiet instead of
  // stacking an identical toast on top of it.
  return !!err && String(err.message) === 'unauthorized';
}

function panelNeedsLogin(){
  // 闸门的可见性有两处来源：markup 上的 display:flex，和 <head> 首帧判定加的
  // html.wb-panel-session（!important）。必须先摘掉那个类，否则闸门永远出不来
  // ——会话过期、登录失败都靠这个函数把登录框放回屏幕。
  document.documentElement.classList.remove('wb-panel-session');
  const gate = document.getElementById('panelGate');
  if(gate) gate.style.display = 'flex';
  const input = document.getElementById('panelPwdInput');
  if(input){ input.focus(); }
}

function panelSessionLost(){
  // The session this tab holds is no longer valid - the gateway restarted,
  // or the token expired. Drop it and stop every poller: leaving them
  // running means each tick fails, re-opens the gate and logs another 401
  // server-side, which is how a stale tab ends up filling the log.
  PANEL_READY = false;
  PANEL_TIMERS.forEach(t => clearInterval(t));
  PANEL_TIMERS = [];
  if(typeof stopLogPolling === 'function') stopLogPolling();
  // 「+ 添加账号 (OAuth)」弹窗里的登录轮询不在 PANEL_TIMERS 里，是它自己的一条
  // 2.5s 定时器。不清它的话：网关重启/会话过期时弹窗还开着，pollLogin 每 2.5 秒
  // 打一次 /accounts/login/poll，每次 401 又回到这里开闸门，这个标签页就会永远
  // 每 2.5 秒刷一次 401（实测 9 秒内又发 3 次，永不停止）。
  if(loginTimer){ clearInterval(loginTimer); loginTimer = null; }
  PANEL_TOKEN = '';
  try { sessionStorage.removeItem(PANEL_STORE); } catch(e) {}
  const msg = document.getElementById('panelLoginMsg');
  if(msg) msg.textContent = '会话已失效，请重新输入密码';
  panelNeedsLogin();
}

function panelHideGate(){
  // 首帧判定的类只在「复核还没回来」时有用；一旦确认已登录就清掉，
  // 让「有类 ⇔ 闸门被首帧判定隐藏」这条不变式只作用于启动阶段。
  document.documentElement.classList.remove('wb-panel-session');
  const gate = document.getElementById('panelGate');
  if(gate) gate.style.display = 'none';
}

async function submitPanelLogin(){
  const input = document.getElementById('panelPwdInput');
  const msg = document.getElementById('panelLoginMsg');
  const password = (input && input.value) || '';
  if(!password){ if(msg) msg.textContent = '请输入面板密码'; return; }
  if(msg) msg.textContent = '';
  if(location.protocol === 'file:'){
    if(msg) msg.textContent = '请通过 http://127.0.0.1:8788/ 打开面板，不要直接双击 dashboard.html';
    panelNeedsLogin();
    return;
  }
  try {
    const r = await fetch('/panel/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password: password})
    });
    const data = await r.json().catch(() => ({}));
    if(!r.ok){
      if(msg) msg.textContent = (data.error && data.error.message) || '密码错误';
      // 登录失败必须保证闸门在屏幕上：带 ?pwd= 自动登录时页面不会走 bootPanel 的
      // 未认证分支，闸门若停在首帧判定的隐藏态，用户看到的是「看着已登录、其实
      // 进不去」的空看板，连重输的框都没有。
      panelNeedsLogin();
      return;
    }
    PANEL_TOKEN = data.token || '';
    try { sessionStorage.setItem(PANEL_STORE, PANEL_TOKEN); } catch(e) {}
    if(input) input.value = '';
    panelHideGate();
    toast('已进入面板', 'ok');
    if(data.using_default_password){
      toast('当前是默认密码 admin，建议到「设置」里修改', 'warn');
    }
    await startDashboard();
  } catch(e) {
    if(msg) msg.textContent = '登录失败: ' + e.message;
    // 网络层失败（网关没起来、反代 502）同样要把闸门留在屏幕上。
    panelNeedsLogin();
  }
}

async function panelLogout(){
  try { await postJSON('/panel/logout', {}); } catch(e) {}
  PANEL_READY = false;
  PANEL_TIMERS.forEach(t => clearInterval(t));
  PANEL_TIMERS = [];
  PANEL_TOKEN = '';
  try { sessionStorage.removeItem(PANEL_STORE); } catch(e) {}
  location.reload();
}

async function bootPanel(){
  try {
    const r = await fetch('/panel/status', {cache: 'no-store', headers: authHeaders()});
    PANEL_STATUS = await r.json().catch(() => ({}));
  } catch(e) { PANEL_STATUS = {}; }
  const hint = document.getElementById('panelDefaultHint');
  if(hint && PANEL_STATUS.panel_password_is_default) hint.style.display = 'block';
  if(PANEL_STATUS.authenticated){
    panelHideGate();
    await startDashboard();
  } else {
    const pwdFromUrl = new URLSearchParams(window.location.search).get('pwd');
    if(pwdFromUrl){
      const input = document.getElementById('panelPwdInput');
      if(input) input.value = pwdFromUrl;
      await submitPanelLogin();
    } else {
      panelNeedsLogin();
    }
  }
}

let PANEL_READY = false;
let PANEL_TIMERS = [];
let visHookInstalled = false;

async function startDashboard(){
  if(PANEL_READY) return;
  PANEL_READY = true;
  // 前两步决定下面各自查什么（VIEW_REALM / 面板可见性），必须先行；之后就并行，
  // 别让国内版那条约 2 秒的成长任务接口把首屏的用量卡片拖在后面。
  await loadSettings();
  await initRealm();
  // 成长任务那条走上游、约 2 秒，只服务「任务与福利」页，而且它自己渲染。原来
  // 它在这一批 Promise.all 里，switchMainTab 因此被押到它后面——落在数据看板
  // 页签时，KPI 要等这两秒过去才开始发 /usage/analytics。这里只把它摘出来让它
  // 并行跑，其余照旧等待；异常自己收，别变成未处理的拒绝。
  loadGrowthTasks().catch(() => {});
  await Promise.all([loadAccounts(), loadModels(), loadAgentModelSelection(),
                     loadSchedulerStatus(), refresh(), loadAgentsAvailability()]);
  updateRecentLimitButtons();
  // The head script already resolved this before first paint; re-apply it
  // here so the data loaders for the active page actually run.
  switchMainTab(MAIN_TABS.includes(window.__MAIN_TAB__) ? window.__MAIN_TAB__ : 'gateway');
  // 首屏把数据表接上排序：静态标记里就有的表不会被任何 loader 触发 render，
  // 轮询那条兜底要等 5 秒才轮到它。
  applyAllTableSorts();
  // Polling only starts after the panel is unlocked, so a locked browser
  // never hammers the server with 401s.
  PANEL_TIMERS.forEach(t => clearInterval(t));
  // 后台标签页不再轮询：浏览器停止绘制后台页，但定时器照跑——实测网关页在后台
  // 仍有 50 次/分钟、统计页 110 次/分钟的请求，全是白跑的。可见性变化时下面会
  // 补一次刷新，所以切回前台看到的数据仍是新的。
  PANEL_TIMERS = [
    setInterval(() => {
      if(!PANEL_READY || document.hidden) return;
      if(currentMainTab === 'analytics'){ loadAnalytics(); loadAnalyticsMatrix(); }
    }, 5000),
    // 每 5 秒在任何页面都跑，顺带把页面上的数据表接上排序（见
    // applyAllTableSorts）：表本来就是 5 秒重画一次，扫描跟同一个节奏走，新加
    // 一张表不必在它的 render 函数里再写一行，下一轮自己就接上了。
    setInterval(() => { if(!document.hidden){ refresh(); applyAllTableSorts(); } }, 5000),
    setInterval(() => { if(!document.hidden) loadSchedulerStatus(); }, 30000),
    setInterval(() => { if(!document.hidden) loadAccounts(); }, 15000),
  ];
  if(!visHookInstalled){
    visHookInstalled = true;
    // 回到前台补一次：按当前所在页刷新，复用 refresh 自身的并发合并（撞上在飞
    // 的那一轮会排队补跑），所以不会与刚跑完的一轮重复。
    document.addEventListener('visibilitychange', () => {
      if(document.hidden || !PANEL_READY) return;
      refresh();
      loadAccounts();
      loadSchedulerStatus();
      if(currentMainTab === 'analytics'){ loadAnalytics(); loadAnalyticsMatrix(); }
      if(currentMainTab === 'logs' && logAutoRefresh) fetchNewLogs(false);
    });
  }
}
