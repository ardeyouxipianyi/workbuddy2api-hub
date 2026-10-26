let CURRENT_GROWTH_UID = 'all';
try {
  const _autoClaim = document.getElementById('growthAutoClaim');
  if(_autoClaim && localStorage.getItem('wb-growth-auto-claim') === '0') _autoClaim.checked = false;
} catch(e) {}

async function switchGrowthAccount(uid){
  CURRENT_GROWTH_UID = uid;
  await loadGrowthTasks();
}

/* Task participation: `tasksEnabled` is authoritative; older gateways do
 * not send it, so fall back to `enabled`. */
function taskActive(a){
  return !!a && (a.tasksEnabled !== undefined ? !!a.tasksEnabled : !!a.enabled);
}

async function loadIntlTasks(){
  try{
    const [r, g] = await Promise.all([
      getJSON('/accounts?realm=all'),
      getJSON('/accounts/credits/grants').catch(() => ({}))
    ]);
    const accts = (r.accounts || []).filter(a => a.realm === 'intl' && taskActive(a));
    const countEl = document.getElementById("intlAcctCount");
    if(countEl) countEl.textContent = accts.length + ' 个';
    const bonusEl = document.getElementById("intlBonusCredits");
    if(bonusEl) bonusEl.textContent = '30 / 50 积分';

    const intlTotalEl = document.getElementById("intlTotalGranted");
    if(intlTotalEl){
      const gRows = (g.rows || []).filter(x => x.realm === 'intl' && (x.name === 'Bonus Pack' || (x.source_inferred && x.source_inferred.includes('活跃'))));
      const totalAmt = gRows.reduce((acc, x) => acc + (Number(x.size || x.amount) || 0), 0);
      intlTotalEl.textContent = '+' + fmt(totalAmt) + ' 积分';
    }

    if(typeof updateTaskPageCheckinCards === 'function') updateTaskPageCheckinCards();
  }catch(e){
    console.error('loadIntlTasks error:', e);
  }
}

async function loadGrowthTasks(){
  loadIntlTasks();
  const sec = document.getElementById("sectionGrowthTasks");
  if(!sec) return;
  sec.style.display = 'block';
  try{
    const q = (CURRENT_GROWTH_UID && CURRENT_GROWTH_UID !== 'all') ? ('?uid=' + encodeURIComponent(CURRENT_GROWTH_UID)) : '';
    const r = await getJSON('/tasks' + q);
    const tasks = r.tasks || [];
    const sum = r.summary || {};
    const accts = r.accounts || [];
    const streak = sum.streak_days || 0;
    const energy = sum.energy || 0;
    const tr = sum.travel || {};

    const sel = document.getElementById("growthAcctSelect");
    if(sel && accts.length){
      const opts = ['<option value="all"' + (CURRENT_GROWTH_UID === 'all' ? ' selected' : '') + '>全部国内账号 (批量)</option>']
        .concat(accts.map(a => '<option value="' + esc(a.uid) + '"' + (CURRENT_GROWTH_UID === a.uid ? ' selected' : '') + '>' + esc(a.nickname) + ' (' + esc(a.uid.slice(0,6)) + ')</option>'))
        .join('');
      sel.innerHTML = opts;
    }

    if(typeof updateTaskPageCheckinCards === 'function') updateTaskPageCheckinCards();

    const elStk = document.getElementById("stkDays");
    if(elStk) elStk.textContent = streak + ' 天';
    const elNrg = document.getElementById("nrgBalance");
    if(elNrg) elNrg.textContent = energy;
    const elCards = document.getElementById("makeupCards");
    if(elCards) elCards.textContent = (sum.makeup_cards != null ? sum.makeup_cards : '-') + ' 张';
    const elLottery = document.getElementById("lotteryChances");
    if(elLottery) elLottery.textContent = (sum.lottery_chances != null ? sum.lottery_chances : '-') + ' 次';

    const cnTotalEl = document.getElementById("cnTotalGranted");
    if(cnTotalEl){
      try{
        const g = await getJSON('/accounts/credits/grants');
        let gRows = (g.rows || []).filter(x => x.realm === 'cn');
        if(CURRENT_GROWTH_UID && CURRENT_GROWTH_UID !== 'all'){
          gRows = gRows.filter(x => x.uid === CURRENT_GROWTH_UID);
        }
        // 排除掉不带任务/活动属性的纯体验包，统计签到、成长、活动等福利
        const perksRows = gRows.filter(x => {
          const r = String(x.grant_reason || '');
          const n = String(x.name || '');
          return r.includes('签到') || r.includes('成长') || r.includes('活动') || r.includes('裂变') || n.includes('裂变');
        });
        const totalAmt = perksRows.reduce((acc, x) => acc + (Number(x.size || x.amount) || 0), 0);
        cnTotalEl.textContent = '+' + fmt(totalAmt) + ' 积分';
      }catch(e){
        cnTotalEl.textContent = '-';
      }
    }

    const elCat = document.getElementById("catState");
    const elCatDesc = document.getElementById("catDesc");
    if(elCat){
      if(tr.state === 'arrived') {
        elCat.innerHTML = '<span style="color:var(--accent2)">待领奖</span>';
        if(elCatDesc) elCatDesc.textContent = '猫猫旅行 (奖励 +' + (tr.reward_credit || 0) + ' 积分)';
      } else if(tr.state === 'traveling') {
        elCat.innerHTML = '<span style="color:var(--accent)">旅行中</span>';
        if(elCatDesc) elCatDesc.textContent = '猫猫旅行 (预计稍后归来)';
      } else if(tr.daily_limit_reached) {
        elCat.innerHTML = '<span style="color:var(--dim)">今日已完成</span>';
        if(elCatDesc) elCatDesc.textContent = '次日 00:00 刷新';
      } else {
        elCat.innerHTML = '<span style="color:var(--accent2)">在家待命</span>';
        if(elCatDesc) elCatDesc.textContent = '随时可出发旅行';
      }
    }

    const tb = document.querySelector("#growthTable tbody");
    if(!tb) return;
    if(!tasks.length){
      tb.innerHTML = '<tr><td colspan="5" class="empty">暂无任务或未登录国内版账号</td></tr>';
      return;
    }
    tb.innerHTML = tasks.map(t => {
      let stBadge = '<span class="badge">' + esc(t.status) + '</span>';
      if(t.status === 'claimed'){
        stBadge = '<span class="badge off">已领取</span>';
      } else if(t.status === 'completed' || t.current >= t.target){
        stBadge = '<span class="badge ok">待领奖</span>';
      } else if(t.status === 'accepted' || t.status === 'in_progress'){
        stBadge = '<span class="badge s">进行中</span>';
      } else if(t.status === 'not_accepted'){
        stBadge = '<span class="badge">未接取</span>';
      }
      const prog = (t.status === 'claimed' || t.status === 'completed') ? '已达成' : (t.current + ' / ' + t.target);
      const rwd = (t.reward_credit ? ('<b style="color:var(--accent2)">+' + t.reward_credit + '</b> 积分') : '') +
                  (t.reward_energy ? (' <span style="color:var(--accent)">+' + t.reward_energy + ' 能量</span>') : '');
      return '<tr>'
        + '<td data-label="任务"><b>' + esc(t.name) + '</b><br><span class="mono" style="font-size:11px;color:var(--dim)">' + esc(t.task_code) + '</span></td>'
        + '<td style="font-size:12px;color:var(--dim);max-width:300px" data-label="说明">' + esc(t.description || '-') + '</td>'
        + '<td data-label="进度">' + prog + '</td>'
        + '<td data-label="奖励">' + (rwd || '-') + '</td>'
        + '<td data-label="状态">' + stBadge
        + ((t.status !== 'claimed' && (t.status === 'completed' || (Number(t.current) || 0) >= (Number(t.target) || 0)))
           ? ' <button class="sec mini" data-code="' + esc(t.task_code) + '" onclick="claimGrowthTask(this)">领取</button>' : '')
        + '</td>'
        + '</tr>';
    }).join('');
    applyTableSort('growthTable');
  }catch(e){
    console.error('loadGrowthTasks error:', e);
  }
}

async function runGrowthTasks(btn){
  btn.disabled = true;
  const oldText = btn.textContent;
  btn.textContent = '任务执行中...';
  const logBox = document.getElementById("taskLogBox");
  if(logBox){
    logBox.style.display = 'block';
    logBox.innerHTML = '<div style="color:var(--accent)">[task_runner] 正在启动全自动任务接取、事件上报与奖励领取流水...</div>';
  }
  try{
    const payload = {};
    if(CURRENT_GROWTH_UID && CURRENT_GROWTH_UID !== 'all'){
      payload.uid = CURRENT_GROWTH_UID;
    }
    const autoClaim = document.getElementById('growthAutoClaim');
    payload.claim = !autoClaim || autoClaim.checked;
    const r = await postJSON("/tasks/run", payload);
    const logs = r.logs || [];
    if(logBox){
      logBox.innerHTML = logs.map(l => '<div>' + esc(l) + '</div>').join('');
      logBox.scrollTop = logBox.scrollHeight;
    }
    await loadAccounts();
    await loadGrowthTasks();
    const sumC = r.credit_added != null ? ('，共增加 +' + r.credit_added + ' 积分') : '';
    toast("成长任务流水执行完成" + sumC + "！");
  }catch(e){
    if(logBox) logBox.innerHTML += '<div style="color:var(--warn)">执行出错: ' + esc(e.message) + '</div>';
    alert("任务执行失败: " + e.message);
  }finally{
    btn.disabled = false;
    btn.textContent = oldText;
  }
}

function saveGrowthAutoClaim(checked){
  try { localStorage.setItem('wb-growth-auto-claim', checked ? '1' : '0'); } catch(e) {}
}

async function claimGrowthTask(btn){
  const code = (btn && btn.dataset && btn.dataset.code) || '';
  const uid = (CURRENT_GROWTH_UID && CURRENT_GROWTH_UID !== 'all') ? CURRENT_GROWTH_UID : '';
  if(!code) return;
  if(!uid){ toast('请先在右上角选择一个国内版账号', 'warn'); return; }
  if(btn) btn.disabled = true;
  try{
    const r = await postJSON('/tasks/claim', {uid, code});
    if(r.ok){
      toast('领取成功' + (r.credit ? ('，+' + r.credit + ' 积分') : ''), 'ok');
    } else {
      toast('领取失败: ' + (r.msg || '未知原因'), 'bad');
    }
    await loadGrowthTasks();
    await loadAccounts();
  }catch(e){
    toast('领取失败: ' + e.message, 'bad');
  }finally{
    if(btn) btn.disabled = false;
  }
}

async function triggerCatTravel(btn){
  if(btn){ btn.disabled = true; btn.textContent = '旅行中...'; }
  try{
    const payload = {};
    if(CURRENT_GROWTH_UID && CURRENT_GROWTH_UID !== 'all'){
      payload.uid = CURRENT_GROWTH_UID;
    }
    const r = await postJSON("/tasks/travel", payload);
    toast(r.msg || (r.ok ? "猫猫旅行结算完成" : "操作失败"), r.ok ? "ok" : "warn");
    await loadAccounts();
    await loadGrowthTasks();
  }catch(e){
    toast("猫猫旅行失败: " + e.message, "bad");
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '猫猫旅行'; }
  }
}

async function triggerBuddyCheckin(btn){
  btn.disabled = true; const ot = btn.textContent; btn.textContent = "签到中...";
  try {
    const payload = {};
    if(CURRENT_GROWTH_UID && CURRENT_GROWTH_UID !== 'all'){
      payload.uid = CURRENT_GROWTH_UID;
    }
    const r = await postJSON("/accounts/checkin", payload);
    const res = r.results || [];
    if(!res.length) toast("未发现国内版账号", "warn");
    else {
      const summary = res.map(x => (x.nickname||x.uid.slice(0,6)) + ": " + (x.ok ? "签到成功 (+100积分)" : (x.error||x.msg))).join("；");
      toast("Buddy 加油站签到: " + summary, res.some(x => x.ok) ? "ok" : "warn");
    }
    await loadAccounts(); await loadGrowthTasks();
  }catch(e){ toast("签到失败: " + e.message, "bad"); }
  finally { btn.disabled = false; btn.textContent = ot; }
}

async function triggerStreakBonus(btn){
  if(btn){ btn.disabled = true; btn.textContent = '执行中...'; }
  try{
    const payload = {};
    if(CURRENT_GROWTH_UID && CURRENT_GROWTH_UID !== 'all'){
      payload.uid = CURRENT_GROWTH_UID;
    }
    const r = await postJSON("/tasks/streak-bonus", payload);
    const msg = r.msg || (r.ok ? "连登管家与抽奖执行完成" : "操作未完成");
    toast(msg, r.ok ? "ok" : "warn");
    await loadAccounts();
    await loadGrowthTasks();
  }catch(e){
    toast("连登管家执行失败: " + e.message, "bad");
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '连登管家与抽奖'; }
  }
}

async function loadSchedulerStatus(){
  try{
    const s = await getJSON("/scheduler");
    const bar = document.getElementById("schedulerBar");
    if(!bar) return;
    const dot = document.getElementById("schedDot");
    const txt = document.getElementById("schedStatusText");
    const times = document.getElementById("schedTimes");
    const btn = document.getElementById("btnToggleSched");
    const btnTrig = document.getElementById("btnTriggerSched");

    if(dot) dot.className = s.enabled ? 'dot' : 'dot off';
    const modeText = (window.VIEW_REALM === 'intl')
      ? (s.mode_intl || '账号 Token 自动保活与凭证常驻 (每日 22:00 集中巡检)')
      : (s.mode_cn || s.mode || '整点排程');
    if(txt) txt.textContent = s.enabled ? ('运行中 · ' + modeText) : '已暂停';
    if(times){
      let line = '上次: ' + (s.last_run_time || '-') + ' · 下次: ' + (s.next_run_time || '-');
      // 待触发的模型额度窗口：探针会在这些时刻到点时自动发请求，看不到就成了黑盒。
      const wins = Array.isArray(s.windows) ? s.windows : [];
      if(wins.length){
        const w = wins[0];
        const left = Math.max(0, Math.round(Number(w.reset_in) || 0));
        const h = Math.floor(left / 3600);
        const m = Math.floor((left % 3600) / 60);
        line += ' · 待触发窗口 ' + wins.length + ' 个，最近: ' + (w.model || '-')
             + '（' + (w.uid8 || '-') + '）' + (h ? h + 'h' : '') + m + 'm 后';
      }
      // 定时主动查询同样要看得见：知道下一次会查哪个模型，比黑盒轮询好。
      const scans = Array.isArray(s.quota_scans) ? s.quota_scans.filter(x => !x.pending) : [];
      if(scans.length){
        const sc = scans[0];
        line += ' · 额度查询 ' + scans.length + ' 条，最近: ' + (sc.model || '-')
             + ' ' + fmtDueSec(sc.due_in) + ' 后';
      }
      times.textContent = line;
    }
    if(btn) btn.textContent = s.enabled ? '暂停调度' : '启用调度';
    if(btnTrig) btnTrig.textContent = '立即巡检保活';
  }catch(e){}
}

async function triggerSchedulerNow(btn){
  if(btn){ btn.disabled = true; btn.textContent = '巡检中...'; }
  try{
    const r = await postJSON("/scheduler/trigger", {});
    toast(r.msg || "已触发巡检保活", "ok");
    setTimeout(loadSchedulerStatus, 2000);
  }catch(e){
    toast("触发失败: " + e.message, "bad");
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = (window.VIEW_REALM === 'intl') ? '立即保活刷新' : '立即巡检保活'; }
  }
}

async function toggleScheduler(btn){
  if(btn){ btn.disabled = true; }
  try{
    const r = await postJSON("/scheduler/toggle", {});
    await loadSchedulerStatus();
    toast("后台调度器已" + (r.enabled ? "启用" : "暂停"), "ok");
  }catch(e){
    toast("切换失败: " + e.message, "bad");
  }finally{
    if(btn){ btn.disabled = false; }
  }
}
function filterRealm(m){
  REALM_FILTER = m;
  document.getElementById("fltAll").className = m === "all" ? "active" : "";
  document.getElementById("fltIntl").className = m === "intl" ? "active" : "";
  document.getElementById("fltCn").className = m === "cn" ? "active" : "";
  renderAccounts();
}
async function doCheckin(btn){
  btn.disabled = true; const ot = btn.textContent; btn.textContent = "签到中...";
  try {
    const r = await postJSON("/accounts/checkin", {});
    const res = r.results || [];
    if(!res.length) toast("未发现国内版账号", "warn");
    else {
      const summary = res.map(x => (x.nickname||x.uid.slice(0,6)) + ": " + (x.ok ? "签到成功" : (x.error||x.msg))).join("；");
      toast("每日签到: " + summary, res.some(x => x.ok) ? "ok" : "warn");
    }
    await refreshActiveRealm(); window.updateUI(); loadAccounts(); loadModels();
  }catch(e){ toast("签到失败: " + e.message, "bad"); }
  finally { btn.disabled = false; btn.textContent = ot; }
}

async function doDailyChat(btn){
  btn.disabled = true; const ot = btn.textContent; btn.textContent = "打卡中...";
  try {
    const r = await postJSON("/accounts/daily-chat", {});
    const res = r.results || [];
    if(!res.length) toast("未发现国际版账号", "warn");
    else {
      const summary = res.map(x => (x.nickname||x.uid.slice(0,6)) + ": " + (x.ok ? "活跃打卡成功" : (x.error||x.msg))).join("；");
      toast("每日活跃: " + summary, res.some(x => x.ok) ? "ok" : "warn");
    }
    await refreshActiveRealm(); window.updateUI(); loadAccounts(); loadModels();
  }catch(e){ toast("打卡失败: " + e.message, "bad"); }
  finally { btn.disabled = false; btn.textContent = ot; }
}

/* 今天还没有活跃打卡记录的国际版账号。判定口径与「今日活跃打卡状态」卡片完全一致
 * （都用 activityStatusOf()），所以卡片上悬停看到的未打卡名单，就是下面按钮要打的那批。 */
function pendingDailyChatAccounts(){
  const all = window.ACCOUNTS || [];
  return all.filter(a => a.realm === 'intl' && taskActive(a)).filter(a => {
    const s = activityStatusOf(a);
    return !(s.kind === 'ok' || s.kind === 'done');
  });
}

// 卡片悬停提示：把未打卡账号逐个列出来，省得去账号页一个个比对。
function pendingDailyChatTip(pending){
  const MAX = 30;
  const lines = pending.slice(0, MAX).map(a => '· ' + (a.nickname || String(a.uid || '').slice(0, 8)));
  if(pending.length > MAX) lines.push('· …');
  return '今日未打卡账号 (' + pending.length + ')：\n' + lines.join('\n');
}

/* 未活跃打卡一键打卡：只对今天还没打过卡的国际版账号补跑，名单交给服务端精确执行。
 * 全部已打卡时直接提示，不发请求——重复打卡不会多给积分，只会白烧账号额度。 */
async function doDailyChatPending(btn){
  const pending = pendingDailyChatAccounts();
  if(!pending.length){ toast("国际版账号今天都已打卡", "ok"); return; }
  btn.disabled = true; const ot = btn.textContent; btn.textContent = "打卡中...";
  try {
    const r = await postJSON("/accounts/daily-chat", {uids: pending.map(a => String(a.uid))});
    const res = r.results || [];
    if(!res.length) toast("未发现待打卡的国际版账号", "warn");
    else {
      const summary = res.map(x => (x.nickname||x.uid.slice(0,6)) + ": " + (x.ok ? "活跃打卡成功" : (x.error||x.msg))).join("；");
      toast("未活跃打卡: " + summary, res.some(x => x.ok) ? "ok" : "warn");
    }
    await refreshActiveRealm(); window.updateUI(); loadAccounts(); loadModels();
  }catch(e){ toast("未活跃打卡失败: " + e.message, "bad"); }
  finally { btn.disabled = false; btn.textContent = ot; }
}

/* 网页通道打卡：只建网页端会话（官方每日活跃只认网页端对话）。
 * 会真的起一次任务，所以先确认一次。 */
async function doDailyChatWeb(btn){
  if(!confirm('将为每个已启用的国际版账号各建一个网页端会话。\n\n这会真的起一次任务，用于计入官方「每日活跃」；活跃走免费模型（0.00x）。继续？')) return;
  btn.disabled = true; const ot = btn.textContent; btn.textContent = "打卡中...";
  try {
    const r = await postJSON("/accounts/daily-chat-web", {});
    const res = r.results || [];
    if(!res.length) toast("未发现国际版账号", "warn");
    else {
      const summary = res.map(x => (x.nickname||x.uid.slice(0,6)) + ": " + (x.ok ? ("已建会话 " + (x.conversation||"")) : (x.error||"失败"))).join("；");
      toast("网页通道打卡: " + summary, res.some(x => x.ok) ? "ok" : "warn");
    }
    await refreshActiveRealm(); window.updateUI(); loadAccounts(); loadModels();
  }catch(e){ toast("网页通道打卡失败: " + e.message, "bad"); }
  finally { btn.disabled = false; btn.textContent = ot; }
}
