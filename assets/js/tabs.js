/* 智能体配置只在「浏览器和网关进程同一台机器」时成立（issue #246）：服务端 /
   Docker / OpenWrt 部署下看板是远程打开的，一键配置改的是容器或路由器里的
   配置目录，到不了用户自己的电脑。服务端据此回 enabled:false，这里把入口整个
   撤掉；判定失败（网络抖动等）保持可见——本机用户不该因此丢掉这个入口。 */
let AGENTS_AVAILABLE = true;
async function loadAgentsAvailability(){
  try {
    const r = await getJSON('/agents/available');
    if(r && r.enabled === false){
      AGENTS_AVAILABLE = false;
      const btn = document.getElementById('btnNavAgents');
      if(btn) btn.style.display = 'none';
    }
  } catch(e){}
}

function switchMainTab(tab){
  // 入口被撤掉后（?tab=agents 的书签、上次停留的页签）退回网关页
  if(tab === 'agents' && !AGENTS_AVAILABLE) tab = 'gateway';
  // 被「顶部页签显示」收起来的页面同理：书签与上次停留的页签都不能把内容调出来，
  // 否则页面容器带着 hidden 被点亮，屏幕上一片空白。
  if(pageHidden(tab)) tab = firstVisiblePage();
  // 账号页被合并掉之后（设置里的「账号独立成一个页签」关闭，默认），
  // ?tab=accounts 的书签与上次停留的页签都退回「网关与账号」页——区块就在那一页里。
  if(tab === 'accounts' && !_accountsSeparateTab) tab = 'gateway';
  currentMainTab = tab;
  // Remember the page across reloads. The URL is updated so the address bar
  // matches what is on screen and a copied link reopens the same page;
  // storage covers a plain reload and the ?key= link, which strips the
  // query string on load.
  try {
    const url = new URL(window.location.href);
    url.searchParams.set('tab', tab);
    history.replaceState(null, '', url.toString());
  } catch(e){}
  try { localStorage.setItem(MAIN_TAB_STORE, tab); } catch(e){}
  const btnGateway = document.getElementById('btnNavGateway');
  const btnTasks = document.getElementById('btnNavTasks');
  const btnAccounts = document.getElementById('btnNavAccounts');
  const btnAnalytics = document.getElementById('btnNavAnalytics');
  const btnLogs = document.getElementById('btnNavLogs');
  const btnSettings = document.getElementById('btnNavSettings');
  const btnAgents = document.getElementById('btnNavAgents');
  const pageGateway = document.getElementById('pageGateway');
  const pageTasks = document.getElementById('pageTasks');
  const pageAccounts = document.getElementById('pageAccounts');
  const pageAnalytics = document.getElementById('pageAnalytics');
  const pageLogs = document.getElementById('pageLogs');
  const pageSettings = document.getElementById('pageSettings');
  const pageAgents = document.getElementById('pageAgents');
  const tabs = [
    ['gateway', btnGateway, pageGateway],
    ['accounts', btnAccounts, pageAccounts],
    ['tasks', btnTasks, pageTasks],
    ['analytics', btnAnalytics, pageAnalytics],
    ['logs', btnLogs, pageLogs],
    ['settings', btnSettings, pageSettings],
    ['agents', btnAgents, pageAgents]
  ];
  tabs.forEach(([name, btn, page]) => {
    if(!btn || !page) return;
    const on = (name === tab);
    btn.classList.toggle('active', on);
    page.classList.toggle('active', on);
    // 每个页面各自按自己的区块重建侧栏；非当前页的区块变化在切过来时补上
    if(on) initPageNav(page);
  });
  if(tab === 'tasks'){ loadGrowthTasks(); }
  if(tab === 'accounts' || (!_accountsSeparateTab && tab === 'gateway')){ loadActivityBoard(); }
  if(tab === 'analytics'){ loadAnalytics(); loadAnalyticsMatrix(); }
  if(tab === 'settings'){ loadSettings(); loadProxySlots(); }
  if(tab === 'agents'){ loadAgents(); }
  if(tab === 'logs'){
    startLogPolling();
    fetchNewLogs(true);
  } else {
    stopLogPolling();
  }
  restorePageNavHash();
}
