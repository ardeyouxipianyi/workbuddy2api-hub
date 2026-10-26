let loginState = null, loginTimer = null;

function openLogin(){
  const m = document.getElementById("loginModal");
  if(m) m.classList.add("show");
}

function closeLogin(){
  const m = document.getElementById("loginModal");
  if(m) m.classList.remove("show");
  if(loginTimer){ clearInterval(loginTimer); loginTimer = null; }
  if(loginState){ postJSON("/accounts/login/cancel", {state: loginState}).catch(()=>{}); loginState = null; }
}

function openLoginModal(targetRealm){
  openLogin();
  const realm = targetRealm || window.VIEW_REALM || "intl";
  const isCn = (realm === "cn");
  const rCn = document.getElementById("radioCn");
  const rIntl = document.getElementById("radioIntl");
  if(rCn) rCn.checked = isCn;
  if(rIntl) rIntl.checked = !isCn;
  triggerLoginForSelectedRealm();
}

function switchLoginRealm(realm){
  triggerLoginForSelectedRealm();
}

function restartLogin(){
  triggerLoginForSelectedRealm();
}

async function triggerLoginForSelectedRealm(){
  const checkedRadio = document.querySelector('input[name="loginRealm"]:checked');
  const realm = checkedRadio ? checkedRadio.value : (window.VIEW_REALM || "intl");
  const isCn = (realm === "cn");

  if(loginTimer){ clearInterval(loginTimer); loginTimer = null; }
  if(loginState){ postJSON("/accounts/login/cancel", {state: loginState}).catch(()=>{}); loginState = null; }

  const linkBox = document.getElementById("loginLinkBox");
  const statusBox = document.getElementById("loginStatus");
  if(linkBox) linkBox.innerHTML = "";
  if(statusBox) statusBox.textContent = "";

  const s1 = document.getElementById("step1");
  const s2 = document.getElementById("step2");
  const s3 = document.getElementById("step3");
  if(s1) s1.className = "step active";
  if(s2) s2.className = "step";
  if(s3) s3.className = "step";

  const s1Text = document.getElementById("step1Text");
  if(s1Text) s1Text.textContent = isCn ? "正在向 copilot.tencent.com (国内版) 申请授权..." : "正在向 www.workbuddy.ai (国际版) 申请授权...";

  try{
    const r = await postJSON("/accounts/login/start", {platform: "CLI", realm: realm});
    loginState = r.state;
    if(s1) s1.className = "step done";
    if(s2) s2.className = "step active";
    if(linkBox) linkBox.innerHTML =
      '<a class="login-link" id="loginLink" href="' + esc(r.authUrl) + '" target="_blank" rel="noopener">' + esc(r.authUrl) + '</a>'
      + '<button class="sec mini" onclick="copyLoginLink()">复制登录链接</button>'
      + '<div class="hint">点击上方链接在浏览器里完成登录授权。登录完成后本窗口会自动检测并完成入池。</div>';
    pollLogin();
    loginTimer = setInterval(pollLogin, 2500);
  }catch(e){
    if(statusBox) statusBox.textContent = "发起失败: " + e.message;
    if(s1) s1.className = "step";
  }
}

async function copyLoginLink(){
  const link = document.getElementById("loginLink");
  if(!link) return;
  try{
    await writeClipboard(link.getAttribute("href"));
    toast('已复制到剪贴板', 'ok');
  }catch(e){
    toast('复制失败: ' + e.message, 'bad');
  }
}

async function pollLogin(){
  if(!loginState) return;
  try{
    const r = await getJSON("/accounts/login/poll?state=" + encodeURIComponent(loginState));
    const s3 = document.getElementById("step3");
    const statusBox = document.getElementById("loginStatus");
    if(s3) s3.className = "step active";
    if(statusBox) statusBox.textContent = r.message || r.status;
    if(r.status === "ok"){
      if(s3) s3.className = "step done";
      if(statusBox) statusBox.textContent = "登录成功：" + (r.account && r.account.nickname) + " 已加入账号池";
      if(loginTimer){ clearInterval(loginTimer); loginTimer = null; }
      loginState = null;
      await loadAccounts();
      setTimeout(closeLogin, 1200);
    }else if(r.status === "error" || r.status === "expired" || r.status === "unknown"){
      if(loginTimer){ clearInterval(loginTimer); loginTimer = null; }
    }
  }catch(e){}
}


window.ACTIVE_GATEWAY_REALM = "intl";
window.VIEW_REALM = "intl";
window.REALM_FILTER = "all";
window.MODELS_DATA = [];
window.ACCOUNTS = [];

function updateUI(){
  const isIntlView = (window.VIEW_REALM === "intl");
  const tIntl = document.getElementById("tabIntl");
  const tCn = document.getElementById("tabCn");
  if(tIntl) tIntl.className = "realm-card" + (isIntlView ? " active" : "");
  if(tCn) tCn.className = "realm-card" + (!isIntlView ? " active" : "");
  const vbIntl = document.getElementById("tabIntlViewBadge");
  if(vbIntl) vbIntl.style.display = isIntlView ? "inline-block" : "none";
  const vbCn = document.getElementById("tabCnViewBadge");
  if(vbCn) vbCn.style.display = !isIntlView ? "inline-block" : "none";

  // 账号页页头的版本切换与上面两张卡片同一份状态（VIEW_REALM），所以两边永远
  // 一起亮：在账号页切过去，回网关页看到的就是刚切的那一版，反之亦然。
  const rsIntl = document.getElementById("accountsRealmIntl");
  if(rsIntl){
    rsIntl.classList.toggle("active", isIntlView);
    rsIntl.setAttribute("aria-pressed", isIntlView ? "true" : "false");
  }
  const rsCn = document.getElementById("accountsRealmCn");
  if(rsCn){
    rsCn.classList.toggle("active", !isIntlView);
    rsCn.setAttribute("aria-pressed", !isIntlView ? "true" : "false");
  }

  // 区块标题上的出口标签：这几个区块的内容都按当前视图区域取（账号、禁用汇总、
  // 最近请求、模型清单），标出来免得把国际版的数字看成全局的。用 <em> 承载：
  // 侧栏导航由 h2 生成时会剥掉 <em>，所以标签不会串进导航项。
  const sectionRealm = isIntlView ? "（国际版）" : "（国内版）";
  ["disabledRealmTag", "recentRealmTag", "modelsRealmTag"].forEach(function(id){
    const el = document.getElementById(id);
    if(el) el.textContent = sectionRealm;
  });

  // 工具栏上「一键切换所有出站身份」的当前值：它作用于全部账号（含另一个
  // 版本），所以不看视图、只看 window.ACCOUNTS。
  updateProductAllButton();

  const isIntlGw = (window.ACTIVE_GATEWAY_REALM === "intl");
  const dbIntl = document.getElementById("tabIntlDefaultBadge");
  if(dbIntl) dbIntl.style.display = isIntlGw ? "inline-block" : "none";
  const dbCn = document.getElementById("tabCnDefaultBadge");
  if(dbCn) dbCn.style.display = !isIntlGw ? "inline-block" : "none";

  const gwName = document.getElementById("activeGatewayName");
  if(gwName) {
    gwName.textContent = isIntlGw ? "国际版 (Global)" : "国内版 (China)";
    gwName.style.color = isIntlGw ? "var(--accent)" : "var(--bad)";
  }
  const ep = document.getElementById("activeEndpoint");
  if(ep) ep.textContent = isIntlGw ? "https://www.workbuddy.ai/v2" : "https://copilot.tencent.com/v2";

  const btnToggle = document.getElementById("btnToggleRealm");
  if(btnToggle) {
    btnToggle.textContent = isIntlGw ? "切换默认出口为【国内版】" : "切换默认出口为【国际版】";
    btnToggle.style.borderColor = isIntlGw ? "var(--bad)" : "var(--accent)";
    btnToggle.style.color = isIntlGw ? "var(--bad)" : "var(--accent)";
  }
  const cardGw = document.getElementById("cardGatewayCtrl");
  if(cardGw){
    cardGw.style.borderColor = isIntlGw ? "rgba(37,99,235,.28)" : "rgba(220,38,38,.28)";
    cardGw.style.background = isIntlGw ? "rgba(37,99,235,.03)" : "rgba(220,38,38,.03)";
  }

  const btnCheckin = document.getElementById("btnCheckin");
  if(btnCheckin) btnCheckin.style.display = (window.VIEW_REALM === "cn" ? "inline-block" : "none");
  const btnDailyChat = document.getElementById("btnDailyChat");
  if(btnDailyChat) btnDailyChat.style.display = (window.VIEW_REALM === "intl" ? "inline-block" : "none");
  // btnDailyChatWeb 不上屏（见按钮上的注释），所以这里不再按视图显隐它。

  // 计算当前绑定与生效的 Key 数量
  const keys = (typeof API_KEY_ROWS !== 'undefined' && API_KEY_ROWS.length) ? API_KEY_ROWS : (window.API_KEY_ROWS || []);
  const intlKeys = keys.filter(k => k.enabled !== false && k.realm === 'intl').length;
  const cnKeys = keys.filter(k => k.enabled !== false && k.realm === 'cn').length;
  const followKeys = keys.filter(k => k.enabled !== false && !k.realm).length;
  const activeKeysTotal = isIntlGw ? (intlKeys + followKeys) : (cnKeys + followKeys);

  const elGwKeyBadge = document.getElementById("activeGatewayKeyCount");
  if(elGwKeyBadge) elGwKeyBadge.textContent = activeKeysTotal + ' 个 Key 生效';

  const elIntlKey = document.getElementById("tabIntlKeyBadge");
  if(elIntlKey) elIntlKey.textContent = intlKeys + ' 个 Key';

  const elCnKey = document.getElementById("tabCnKeyBadge");
  if(elCnKey) elCnKey.textContent = cnKeys + ' 个 Key';

  // 左侧卡片账号数与积分统计：大数字是「总剩余」（含停用账号），副行的
  // 「可用」只算 isAcctActive（启用且未触发保留/限额护栏）的账号——与旁边
  // 的 N/M 账号计数同一个口径，两个数字才有可比性。
  const allAccts = window.ACCOUNTS || [];
  const intlAccts = allAccts.filter(a => a.realm === 'intl' || !a.realm);
  const cnAccts = allAccts.filter(a => a.realm === 'cn');

  const isAcctActive = a => a.enabled && !a.reserveBlocked && !a.dailyLimitBlocked && !a.creditLimitReached;

  let intlCred = 0, hasIntlCred = false, intlAvailCred = 0, hasIntlAvail = false;
  intlAccts.forEach(a => {
    if(a.credits && a.credits.remain != null){
      intlCred += a.credits.remain; hasIntlCred = true;
      if(isAcctActive(a)){ intlAvailCred += a.credits.remain; hasIntlAvail = true; }
    }
  });
  let cnCred = 0, hasCnCred = false, cnAvailCred = 0, hasCnAvail = false;
  cnAccts.forEach(a => {
    if(a.credits && a.credits.remain != null){
      cnCred += a.credits.remain; hasCnCred = true;
      if(isAcctActive(a)){ cnAvailCred += a.credits.remain; hasCnAvail = true; }
    }
  });

  const intlActive = intlAccts.filter(isAcctActive).length;
  const cnActive = cnAccts.filter(isAcctActive).length;

  // 区域卡片：大字 = 可用积分（只算 isAcctActive——启用且未触发保留/限额
  // 护栏的账号，与旁边的 N/M 账号计数同一口径）；小字 = 总积分（全部账号
  // 含停用）。数字各挂各的节点，样式（大字/小字）由 HTML 决定。
  const elIntlCred = document.getElementById("tabIntlCredits");
  if(elIntlCred) elIntlCred.textContent = hasIntlCred ? fmt(intlCred) : "—";
  const elIntlAvail = document.getElementById("tabIntlAvailCred");
  if(elIntlAvail) elIntlAvail.innerHTML = '<span class="credit-kind">可用</span> ' + (hasIntlAvail ? fmt(intlAvailCred) : "—");
  const elIntlAcct = document.getElementById("tabIntlAccts");
  if(elIntlAcct) elIntlAcct.innerHTML = '<b style="color:var(--accent)">' + intlActive + '</b>/' + intlAccts.length + ' 账号';

  const elCnCred = document.getElementById("tabCnCredits");
  if(elCnCred) elCnCred.textContent = hasCnCred ? fmt(cnCred) : "—";
  const elCnAvail = document.getElementById("tabCnAvailCred");
  if(elCnAvail) elCnAvail.innerHTML = '<span class="credit-kind">可用</span> ' + (hasCnAvail ? fmt(cnAvailCred) : "—");
  const elCnAcct = document.getElementById("tabCnAccts");
  if(elCnAcct) elCnAcct.innerHTML = '<b style="color:var(--accent)">' + cnActive + '</b>/' + cnAccts.length + ' 账号';
};

// 切换视图的唯一入口：改完 VIEW_REALM 立刻用**已经加载好的**全量 window.ACCOUNTS
// 重画一次「当前禁用账号与模型」，不等后面的异步账号重载 —— 否则在重载返回之前那
// 段间隙里，DOM 里还留着上一个 realm 的停用行，重载失败时它们会一直留着（issue
// #31 要求切换立即生效、不留旧行）。启动时的赋值不走这里：那时还没有任何行被画
// 出来，第一次 loadAccounts() 会画。
function setViewRealm(r){
  window.VIEW_REALM = r;
  renderDisabled();
}

async function switchViewRealm(r){
  if(window.VIEW_REALM === r) return;
  setViewRealm(r);
  RECENT_PAGE = 1;
  window.updateUI();
  // 并行发起，不要逐个 await：国内版的成长任务一次要约 2 秒（上游两个接口），
  // 串行排队时后面那些毫秒级的用量接口全被它堵着，切过去看着就是"过几秒才刷新"。
  await Promise.all([loadAccounts(), loadModels(), loadGrowthTasks(),
                     loadSchedulerStatus(), loadAnalytics(), refresh()]);
};

async function toggleActiveGatewayRealm(){
  const next = (window.ACTIVE_GATEWAY_REALM === "intl" ? "cn" : "intl");
  try {
    const res = await postJSON("/realm", {realm: next});
    window.ACTIVE_GATEWAY_REALM = res.current || next;
    toast("网关默认上游已切换并永久保存为: " + (window.ACTIVE_GATEWAY_REALM === "intl" ? "国际版" : "国内版"));
    setViewRealm(window.ACTIVE_GATEWAY_REALM);
    window.updateUI();
    await Promise.all([loadAccounts(), loadModels(), loadGrowthTasks(),
                       loadSchedulerStatus(), loadAnalytics(), refresh()]);
  }catch(e){
    alert("切换失败: " + e.message);
  }
};

async function refreshActiveRealm(){
  // Refresh which exit the gateway is switched to. Deliberately does NOT touch
  // VIEW_REALM: the view is the user's choice, and an account action taken from
  // one realm's view must not throw them into the other one. Boot-time view
  // selection (including the ?view= parameter) lives in initRealm().
  try {
    const r = await getJSON("/realm");
    if(r && r.current) window.ACTIVE_GATEWAY_REALM = r.current;
  } catch(e){}
}

async function initRealm(){
  // Boot only - the initial view follows the active exit unless the URL pins it.
  // Account actions call refreshActiveRealm() instead: reading ?view= here was
  // what moved the panel back to the other realm after 签到 / 活跃打卡, because
  // the view buttons do not rewrite the URL.
  await refreshActiveRealm();
  window.VIEW_REALM = window.ACTIVE_GATEWAY_REALM;
  const viewFromUrl = new URLSearchParams(window.location.search).get('view');
  if(viewFromUrl && ['intl', 'cn'].includes(viewFromUrl)) window.VIEW_REALM = viewFromUrl;
  const limitFromUrl = new URLSearchParams(window.location.search).get('limit');
  if(limitFromUrl && [10, 20, 50, 100].includes(parseInt(limitFromUrl, 10))) RECENT_LIMIT = parseInt(limitFromUrl, 10);
  window.updateUI();
}

async function refreshModelCatalog(btn){
  // 手动刷新模型清单：强制走一次上游 /v3/config（服务端执行，绕过 300s 缓存），
  // 只刷当前视图版本——这张表显示的是哪个版本，刷新按钮就只拉哪个版本，绝
  // 不跨区（服务端会挑该版本自己的账号发请求）。完成后立即重载表格。
  const realm = window.VIEW_REALM === 'cn' ? 'cn' : 'intl';
  const label = realm === 'cn' ? '国内版' : '国际版';
  if(btn) btn.disabled = true;
  const prevText = btn ? btn.textContent : '';
  if(btn) btn.textContent = '刷新中...';
  try{
    const r = await postJSON('/catalog/refresh', {realm: realm});
    if(r && r.ok){
      toast(label + '模型清单：' + (r.msg || '已更新'), 'ok');
      await loadModels();
    } else if(r && r.cooldown){
      // 冷却不是失败：清单还在、只是这次没到窗口，按提示语气说。
      toast(label + '模型清单：' + (r.msg || '刷新太频繁'), 'warn');
    } else {
      toast(label + '模型清单刷新失败：' + ((r && r.msg) || '未知原因'), 'warn');
    }
  }catch(e){
    toast(label + '模型清单刷新失败：' + e.message, 'bad');
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = prevText; }
  }
}

async function loadModels(){
  try{
    const r = await getJSON('/v1/models?realm=' + encodeURIComponent(window.VIEW_REALM));
    MODELS_DATA = r.data || [];
    renderModelsSourceLine(r.catalog || null);
    renderAvailableModels();
  }catch(e){
    console.error('loadModels error:', e);
  }
}
