/* ---- 账号导出 / 导入 ---- */
async function downloadExport(query, fallbackLabel){
  // Fetch through the authorised JSON API and save the blob client-side.
  // A plain <a href> would not carry the X-Panel-Token header, so a
  // password-only session would be rejected.
  const r = await fetch('/accounts/export' + (query || ''), {cache:'no-store', headers: authHeaders()});
  if(r.status === 401 || r.status === 403){ panelSessionLost(); throw new Error('unauthorized'); }
  if(r.status === 404){
    let msg = '账号不存在';
    try{ const e = await r.json(); msg = (e.error && e.error.message) || msg; }catch(_){}
    throw new Error(msg);
  }
  if(!r.ok) throw new Error('HTTP ' + r.status);
  const text = await r.text();
  let doc;
  try{ doc = JSON.parse(text); }catch(e){ throw new Error('返回内容不是 JSON'); }
  const stamp = new Date().toISOString().slice(0,19).replace(/[-:T]/g,'');
  const name = 'workbuddy-accounts-' + (fallbackLabel || '') + stamp + '.json';
  const blob = new Blob([text], {type:'application/json'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return {count: doc.count || 0, name: name};
}

async function exportAccounts(btn){
  if(btn){ btn.disabled = true; btn.textContent = '导出中...'; }
  try{
    const r = await downloadExport('', '');
    toast('已导出 ' + r.count + ' 个账号 → ' + r.name);
  }catch(e){
    toast('导出失败: ' + e.message, 'err');
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '导出'; }
  }
}

async function exportOne(uid){
  // Same document format as the bulk export, just narrowed to one account, so
  // the file can be re-imported on another instance without editing.
  try{
    const r = await downloadExport('?uid=' + encodeURIComponent(uid), uid.slice(0,8) + '-');
    toast('已导出 1 个账号 → ' + r.name);
  }catch(e){
    toast('导出失败: ' + e.message, 'err');
  }
}

let importRows = null;   // 已解析、待导入的账号行

function openImport(){
  importRows = null;
  const m = document.getElementById('importModal');
  const body = document.getElementById('importBody');
  const ow = document.getElementById('importOverwrite');
  if(ow) ow.checked = false;
  if(body) body.innerHTML = '<p class="hint">正在打开文件…</p>';
  if(m) m.classList.add('show');
  // The picker is created per call so choosing the same file twice still fires.
  const picker = document.createElement('input');
  picker.type = 'file';
  picker.accept = '.json,application/json';
  picker.onchange = () => {
    const f = picker.files && picker.files[0];
    if(!f){ closeImport(); return; }
    readImportFile(f);
  };
  picker.click();
}

function openApiKeyModal(){
  const m = document.getElementById('apiKeyModal');
  const input = document.getElementById('apiKeyInput');
  const name = document.getElementById('apiKeyNickname');
  const msg = document.getElementById('apiKeyModalMsg');
  const btn = document.getElementById('btnApiKeyCommit');
  if(input) input.value = '';
  if(name) name.value = '';
  if(msg) msg.textContent = '';
  if(btn){ btn.disabled = false; btn.textContent = '验证并添加'; }
  if(m) m.classList.add('show');
  if(input) setTimeout(() => input.focus(), 50);
}

function closeApiKeyModal(){
  const m = document.getElementById('apiKeyModal');
  if(m) m.classList.remove('show');
  const input = document.getElementById('apiKeyInput');
  // 关掉就抹掉：这串东西等同于账号本身，留在 DOM 里没有任何好处。
  if(input) input.value = '';
}

async function commitApiKey(btn){
  const input = document.getElementById('apiKeyInput');
  const name = document.getElementById('apiKeyNickname');
  const msg = document.getElementById('apiKeyModalMsg');
  const key = (input && input.value || '').trim();
  if(!key){
    if(msg){ msg.style.color = 'var(--warn)'; msg.textContent = '请先填入 API Key'; }
    return;
  }
  if(!/^(ck_|pt_)/.test(key)){
    if(msg){ msg.style.color = 'var(--warn)'; msg.textContent = '前缀不对：应当以 ck_ 或 pt_ 开头'; }
    return;
  }
  if(btn){ btn.disabled = true; btn.textContent = '验证中…'; }
  if(msg){ msg.style.color = 'var(--dim)'; msg.textContent = '正在用企业额度端点验一次…'; }
  try{
    const r = await postJSON('/accounts/add-api-key', {
      key: key,
      nickname: (name && name.value || '').trim(),
    });
    const probe = r.probe || {};
    let msg = (r.updated ? '已更新 ' : '已添加 ') + (r.nickname || r.uid || 'API Key 账号');
    if(probe.limit !== undefined && probe.limit !== null){
      msg += ' · 额度 ' + probe.limit
           + (probe.used !== undefined && probe.used !== null ? '，本期已用 ' + probe.used : '');
    }
    toast(msg);
    closeApiKeyModal();
    // 与导入一致：只重拉账号列表，不整页刷新（会打断正在看的其他面板）。
    await loadAccounts();
  }catch(e){
    if(msg){ msg.style.color = 'var(--warn)'; msg.textContent = '添加失败: ' + e.message; }
    if(btn){ btn.disabled = false; btn.textContent = '验证并添加'; }
  }
}

function closeImport(){
  const m = document.getElementById('importModal');
  if(m) m.classList.remove('show');
  importRows = null;
  const btn = document.getElementById('btnImportCommit');
  if(btn) btn.disabled = true;
}

async function readImportFile(file){
  const body = document.getElementById('importBody');
  try{
    const text = await file.text();
    let doc;
    try{ doc = JSON.parse(text); }
    catch(e){ throw new Error('文件不是合法 JSON'); }

    // Accept the export document, a bare array, or a single account object.
    let rows;
    if(Array.isArray(doc)) rows = doc;
    else if(doc && Array.isArray(doc.accounts)) rows = doc.accounts;
    else if(doc && (doc.accessToken || doc.auth)) rows = [doc];
    else throw new Error('文件里没有找到账号（既没有 accounts 数组，也不是单个账号）');

    if(!rows.length) throw new Error('账号列表是空的');
    importRows = rows;

    // Dry-run first so the user sees exactly what will happen.
    const r = await postJSON('/accounts/import', {data: doc, dryRun: true});
    renderImportPreview(file.name, r.result || {}, rows.length);
  }catch(e){
    importRows = null;
    const btn = document.getElementById('btnImportCommit');
    if(btn) btn.disabled = true;
    if(body) body.innerHTML = '<p class="hint" style="color:var(--warn)">读取失败: ' + esc(e.message) + '</p>';
  }
}

function renderImportPreview(filename, res, total){
  const body = document.getElementById('importBody');
  const btn = document.getElementById('btnImportCommit');
  const added = res.added || [], updated = res.updated || [];
  const skipped = res.skipped || [], invalid = res.invalid || [];

  const rowsHtml = list => list.map(x => {
    const uid = (typeof x === 'string' ? x : x.uid) || '?';
    const reason = (typeof x === 'object' && x.reason) ? ' — ' + esc(x.reason) : '';
    return '<li><code>' + esc(String(uid).slice(0,8)) + '</code>' + reason + '</li>';
  }).join('');

  let html = '<p class="hint" style="margin-top:0">文件：<b>' + esc(filename) + '</b>'
    + ' · 共 ' + total + ' 条</p>';
  html += '<div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:10px">'
    + '<span>新增 <b style="color:var(--accent2)">' + added.length + '</b></span>'
    + '<span>覆盖 <b>' + updated.length + '</b></span>'
    + '<span>跳过 <b>' + skipped.length + '</b></span>'
    + '<span>无效 <b style="color:var(--warn)">' + invalid.length + '</b></span>'
    + '</div>';

  if(added.length)   html += '<div style="margin-bottom:8px"><b>将新增</b><ul class="hint" style="margin:4px 0 0 18px;padding:0">' + rowsHtml(added) + '</ul></div>';
  if(updated.length) html += '<div style="margin-bottom:8px"><b>将覆盖</b><ul class="hint" style="margin:4px 0 0 18px;padding:0">' + rowsHtml(updated) + '</ul></div>';
  if(skipped.length) html += '<div style="margin-bottom:8px"><b>将跳过</b><ul class="hint" style="margin:4px 0 0 18px;padding:0">' + rowsHtml(skipped) + '</ul></div>';
  if(invalid.length) html += '<div style="margin-bottom:8px"><b>无法解析</b><ul class="hint" style="margin:4px 0 0 18px;padding:0">' + rowsHtml(invalid) + '</ul></div>';

  if(!added.length && !updated.length){
    html += '<p class="hint" style="color:var(--warn)">没有可导入的账号。'
      + (skipped.length ? '若要覆盖已存在的账号，请勾选下方「覆盖同 UID 账号」。' : '') + '</p>';
  }
  if(body) body.innerHTML = html;

  const canCommit = (added.length + updated.length) > 0;
  if(btn){
    btn.disabled = !canCommit;
    btn.textContent = '确认导入 ' + (added.length + updated.length) + ' 个';
  }
}

async function commitImport(btn){
  if(!importRows) return;
  const ow = document.getElementById('importOverwrite');
  const overwrite = !!(ow && ow.checked);
  if(overwrite && !confirm('将覆盖同 UID 的现有账号（含其凭证），确定继续？')) return;

  if(btn){ btn.disabled = true; btn.textContent = '导入中...'; }
  try{
    const r = await postJSON('/accounts/import', {data: importRows, overwrite: overwrite});
    const res = r.result || {};
    const n = (res.added || []).length + (res.updated || []).length;
    toast('导入完成：新增 ' + (res.added || []).length
          + '，覆盖 ' + (res.updated || []).length
          + '，跳过 ' + (res.skipped || []).length
          + '，无效 ' + (res.invalid || []).length);
    if(n > 0){ closeImport(); await loadAccounts(); }
    else { renderImportPreview('（已导入）', res, importRows.length); }
  }catch(e){
    toast('导入失败: ' + e.message, 'err');
    if(btn){ btn.disabled = false; btn.textContent = '确认导入'; }
  }
}

/* 桌面客户端账号：两步确认（先扫描展示弹窗，再逐条导入） */
async function scanDesktop(btn){
  if(btn){ btn.disabled = true; btn.textContent = '扫描中...'; }
  openDesktopScan();
  try{
    const acct = await getJSON('/accounts?realm=all').catch(() => ({accounts: []}));
    window.SCAN_POOL = new Set((acct.accounts || []).map(a => a.uid));
    const r = await postJSON('/accounts/import/desktop', {});
    renderDesktopScan(r.detected || [], r.atrest || null);
  }catch(e){
    const body = document.getElementById('desktopScanBody');
    if(body) body.innerHTML = '<p class="hint" style="color:var(--warn)">扫描失败: ' + esc(e.message) + '</p>';
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '扫描桌面客户端'; }
  }
}

/* --------------------------- credits detail modal --------------------------- */
let CURRENT_CREDIT_DETAIL_UID = null;
let CURRENT_CREDITS_DATA = null;
let CURRENT_PKG_FILTER = 'active';
let CURRENT_PKG_SEARCH = '';
let CURRENT_PKG_SORT = 'expiry_asc';

async function openCreditsDetail(uid, forceRefresh = false){
  CURRENT_CREDIT_DETAIL_UID = uid;
  CURRENT_PKG_FILTER = 'active';
  CURRENT_PKG_SEARCH = '';
  CURRENT_PKG_SORT = 'expiry_asc';

  const m = document.getElementById("creditsDetailModal");
  if(m) m.classList.add("show");

  const errEl = document.getElementById("creditsModalError");
  if(errEl) errEl.style.display = "none";
  const contEl = document.getElementById("creditsModalContent");
  if(contEl) contEl.style.display = "block";

  const searchInput = document.getElementById("cmPkgSearch");
  if(searchInput) searchInput.value = "";
  const sortSelect = document.getElementById("cmPkgSort");
  if(sortSelect) sortSelect.value = "expiry_asc";

  // 重置过滤器按钮高亮
  const filterBtns = document.querySelectorAll(".realm-filter button[data-filter]");
  filterBtns.forEach(b => {
    if(b.getAttribute("data-filter") === "all") b.classList.add("active");
    else b.classList.remove("active");
  });

  const acct = (window.ACCOUNTS || []).find(a => a.uid === uid);
  const titleEl = document.getElementById("creditsModalTitle");
  const realmEl = document.getElementById("creditsModalRealm");
  const tierEl = document.getElementById("creditsModalTier");
  if(titleEl) titleEl.textContent = (acct ? (acct.nickname || acct.uid.slice(0,8)) : uid.slice(0,8)) + " · 积分明细";
  if(realmEl && acct){
    realmEl.className = acct.realm === 'cn' ? 'realm-badge-cn' : 'realm-badge-intl';
    realmEl.textContent = acct.realm === 'cn' ? '国内版' : '国际版';
  }
  if(tierEl){
    if(acct && acct.credits && acct.credits.is_paid_user !== undefined){
      tierEl.style.display = "inline-block";
      tierEl.className = acct.credits.is_paid_user ? "badge mini ok" : "badge mini";
      tierEl.textContent = acct.credits.is_paid_user ? "付费用户" : "普通用户";
      const tier = acct.tier;
      tierEl.title = (tier && tier.package)
        ? (tier.package + (tier.checked_at ? " · " + new Date(tier.checked_at * 1000).toLocaleString() : ""))
        : "";
    } else {
      tierEl.style.display = "none";
    }
  }

  // 检查已缓存的数据是否包含有效期字段，若包含且无需强制刷新，先秒开渲染本地已缓存数据
  const cachedPkgs = (acct && acct.credits && Array.isArray(acct.credits.packages)) ? acct.credits.packages : [];
  const hasExpiryData = cachedPkgs.length > 0 && cachedPkgs.some(p => p.cycle_end_time || p.cycleEndTime || p.CycleEndTime || p.expired_time || p.ExpiredTime);
  if(!forceRefresh && hasExpiryData){
    CURRENT_CREDITS_DATA = acct.credits;
    renderCreditsDetail(acct.credits, acct);
    return;
  }

  await reloadCreditsDetail();
}

async function reloadCreditsDetail(btn){
  if(!CURRENT_CREDIT_DETAIL_UID) return;
  const btnEl = btn || document.getElementById("btnRefreshCreditsDetail");
  if(btnEl){ btnEl.disabled = true; btnEl.textContent = "刷新中..."; }
  const loadingEl = document.getElementById("creditsModalLoading");
  const contentEl = document.getElementById("creditsModalContent");
  const errorEl = document.getElementById("creditsModalError");
  if(loadingEl && (!contentEl || contentEl.style.display === 'none')) loadingEl.style.display = "block";
  if(errorEl) errorEl.style.display = "none";

  try {
    const res = await postJSON("/accounts/credits/detail", { uid: CURRENT_CREDIT_DETAIL_UID, refresh: true });
    if(res && res.ok && res.credits){
      CURRENT_CREDITS_DATA = res.credits;
      // 同步更新全局 window.ACCOUNTS 中的 credits
      const acct = (window.ACCOUNTS || []).find(a => a.uid === CURRENT_CREDIT_DETAIL_UID);
      if(acct){
        acct.credits = res.credits;
        acct.tokenExpired = false;
        renderAccounts();
      }
      if(errorEl) errorEl.style.display = "none";
      if(contentEl) contentEl.style.display = "block";
      renderCreditsDetail(res.credits, acct);
    } else {
      const errMsg = (res && res.error) || "请确认后端代理服务已重启以加载新代码";
      const isAuthErr = errMsg.includes("401") || errMsg.includes("登录") || errMsg.includes("过期")
        || errMsg.includes("Unauthorized") || errMsg.includes("Authorization")
        || errMsg.toLowerCase().includes("token") || !!(res && res.need_relogin);
      if(contentEl) contentEl.style.display = "none";
      if(errorEl){
        errorEl.style.display = "block";
        const titleEl = document.getElementById("creditsModalErrorTitle");
        const msgEl = document.getElementById("creditsModalErrorMsg");
        const btnRelogin = document.getElementById("btnCreditsRelogin");
        if(titleEl) titleEl.textContent = isAuthErr ? "登录凭证已失效" : "拉取积分明细失败";
        if(msgEl) msgEl.textContent = errMsg;
        if(btnRelogin) btnRelogin.style.display = isAuthErr ? "inline-block" : "none";
      }
      if(isAuthErr){
        const acct = (window.ACCOUNTS || []).find(a => a.uid === CURRENT_CREDIT_DETAIL_UID);
        if(acct){
          acct.tokenExpired = true;
          acct.lastError = errMsg;
          renderAccounts();
        }
      }
      toast(isAuthErr ? "登录已过期，请重新登录" : ("拉取积分明细失败: " + errMsg), "bad");
    }
  } catch(e) {
    if(contentEl) contentEl.style.display = "none";
    if(errorEl){
      errorEl.style.display = "block";
      const titleEl = document.getElementById("creditsModalErrorTitle");
      const msgEl = document.getElementById("creditsModalErrorMsg");
      const btnRelogin = document.getElementById("btnCreditsRelogin");
      if(titleEl) titleEl.textContent = "网络异常";
      if(msgEl) msgEl.textContent = e.message;
      if(btnRelogin) btnRelogin.style.display = "none";
    }
    toast("网络异常: " + e.message, "bad");
  } finally {
    if(loadingEl) loadingEl.style.display = "none";
    if(btnEl){ btnEl.disabled = false; btnEl.textContent = "刷新明细"; }
  }
}

function filterCreditsPackages(filterType, btn){
  CURRENT_PKG_FILTER = filterType;
  const parent = btn && btn.parentNode;
  if(parent){
    parent.querySelectorAll("button").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
  }
  renderCreditsPackagesTable();
}

function onCreditsSearchInput(val){
  CURRENT_PKG_SEARCH = (val || "").trim().toLowerCase();
  renderCreditsPackagesTable();
}

function onCreditsSortChange(val){
  CURRENT_PKG_SORT = val || "expiry_asc";
  renderCreditsPackagesTable();
}

function renderCreditsDetail(credits, acct){
  if(!credits) return;
  CURRENT_CREDITS_DATA = credits;
  const remain = Number(credits.remain) || 0;
  const used = Number(credits.used) || 0;
  const size = Number(credits.size) || (remain + used);
  const usedPct = credits.used_percent || (size > 0 ? (used / size * 100).toFixed(1) + '%' : '0.0%');

  const elRemain = document.getElementById("cmMetricRemain");
  const elUsed = document.getElementById("cmMetricUsed");
  const elTotal = document.getElementById("cmMetricTotal");
  const elUsedPct = document.getElementById("cmUsedPercent");
  const elBar = document.getElementById("cmProgressBar");
  const elUpdated = document.getElementById("cmUpdatedText");

  if(elRemain) elRemain.textContent = fmt(remain);
  if(elUsed) elUsed.textContent = fmt(used);
  if(elTotal) elTotal.textContent = fmt(size);
  if(elUsedPct) elUsedPct.textContent = usedPct;
  if(elBar){
    const frac = size > 0 ? Math.min(100, Math.max(0, (used / size) * 100)) : 0;
    elBar.style.width = frac.toFixed(1) + "%";
    elBar.style.background = frac >= 90 ? "var(--bad)" : frac >= 70 ? "var(--warn)" : "var(--accent)";
  }
  if(elUpdated) elUpdated.textContent = "更新时间: " + (credits.updated_iso || "刚刚");

  // 会员等级徽章
  const tierEl = document.getElementById("creditsModalTier");
  if(tierEl){
    if(credits.is_paid_user !== undefined){
      tierEl.style.display = "inline-block";
      tierEl.className = credits.is_paid_user ? "badge mini ok" : "badge mini";
      tierEl.textContent = credits.is_paid_user ? "付费用户" : "普通用户";
    } else {
      tierEl.style.display = "none";
    }
  }

  // 最近到期卡片（优先取后端计算的 earliest_expiring，缺失时前端自动推导）
  const elExpVal = document.getElementById("cmExpiryValue");
  const elExpSub = document.getElementById("cmExpirySub");
  if(elExpVal && elExpSub){
    let ep = credits.earliest_expiring;
    if(!ep && Array.isArray(credits.packages)){
      const candidates = [];
      credits.packages.forEach(p => {
        if(p.no_expiry) return;
        const rem = Number(p.remain ?? p.CycleCapacityRemain) || 0;
        const eStr = p.cycle_end_time || p.cycleEndTime || p.CycleEndTime || p.expired_time || p.ExpiredTime || '';
        if(rem > 0 && eStr && eStr !== '-'){
          try {
            const clean = eStr.replace(/-/g, '/').replace('T', ' ');
            const ts = new Date(clean).getTime();
            if(!isNaN(ts)){
              const d = Math.round((ts - Date.now()) / 86400000 * 10) / 10;
              candidates.push({ name: p.name || '套餐包', remain: rem, cycle_end_time: eStr, days_left: d });
            }
          } catch(e){}
        }
      });
      if(candidates.length){
        candidates.sort((a, b) => a.days_left - b.days_left);
        ep = candidates[0];
      }
    }

    if(ep && ep.days_left !== undefined && ep.days_left !== null){
      const days = Number(ep.days_left);
      const endStr = String(ep.cycle_end_time || '');
      if(days < 0){
        elExpVal.textContent = "已有包过期";
        elExpVal.style.color = "var(--bad)";
        elExpSub.textContent = esc(ep.name) + " · 周期已过";
      } else if(days <= 3){
        elExpVal.textContent = "剩 " + days.toFixed(1) + " 天";
        elExpVal.style.color = "var(--bad)";
        elExpSub.textContent = esc(endStr.slice(5, 16)) + " · " + fmt(ep.remain) + " 积分即将到期";
      } else if(days <= 7){
        elExpVal.textContent = "剩 " + Math.ceil(days) + " 天";
        elExpVal.style.color = "var(--warn)";
        elExpSub.textContent = esc(endStr.slice(5, 10)) + " 到期 · " + fmt(ep.remain) + " 积分";
      } else {
        elExpVal.textContent = "剩 " + Math.floor(days) + " 天";
        elExpVal.style.color = "var(--fg)";
        elExpSub.textContent = esc(endStr.slice(5, 10)) + " 到期 · " + fmt(ep.remain) + " 积分";
      }
      elExpSub.title = (ep.name || "") + " (" + endStr + ")";
    } else {
      elExpVal.textContent = "长期有效";
      elExpVal.style.color = "var(--accent2)";
      elExpSub.textContent = "暂无临期生效套餐包";
    }
  }

  // 扩展状态（签到与会员信息）
  const checkinCard = document.getElementById("cmCheckinCard");
  const checkinText = document.getElementById("cmCheckinText");
  if(checkinCard && checkinText){
    let items = [];
    if(credits.checkin){
      const c = credits.checkin;
      const st = c.today_checked_in
        ? '<span style="color:var(--accent2);font-weight:600">今日已签到</span>'
        : '<span style="color:var(--warn);font-weight:600">今日未签到</span>';
      items.push('每日签到: ' + st + ' (连续 ' + (c.streak_days || 0) + ' 天)');
      if(c.daily_credit) items.push('签到奖励: ' + c.daily_credit + ' 积分');
      if(c.today_credit) items.push('今日领取: ' + c.today_credit + ' 积分');
    }
    if(credits.is_paid_user !== undefined){
      // 档位来自计费摘要（#505）：付费 / 免费之外把包名也带上，方便一眼看出
      // 池子里哪些号是免费档、结论是什么时候读的。
      let tierText = credits.is_paid_user ? '<span class="badge ok mini">付费用户</span>' : '<span class="badge mini">普通/体验用户</span>';
      const tier = acct && acct.tier;
      if(tier && tier.package){
        const checked = tier.checked_at ? new Date(tier.checked_at * 1000).toLocaleString() : '';
        tierText += ' <span class="badge mini" title="计费摘要' + (checked ? ' · ' + checked : '') + '">' + escapeHtml(tier.package) + '</span>';
      }
      items.push('会员类型: ' + tierText);
    }
    if(items.length){
      checkinText.innerHTML = items.join(' · ');
      checkinCard.style.display = "block";
    } else {
      checkinCard.style.display = "none";
    }
  }

  // 渲染套餐权益包明细表格
  renderCreditsPackagesTable();
}

function renderCreditsPackagesTable(){
  const credits = CURRENT_CREDITS_DATA;
  if(!credits) return;
  const tbody = document.getElementById("cmPackagesBody");
  if(!tbody) return;

  const rawPkgs = Array.isArray(credits.packages) ? credits.packages : [];
  const countBadge = document.getElementById("cmPkgCountBadge");
  if(countBadge) countBadge.textContent = "共 " + rawPkgs.length + " 个";

  // 统一预处理各套餐包的有效期与数值
  const preparedPkgs = rawPkgs.map(p => {
    const pSize = Number(p.size ?? p.total ?? p.CycleCapacitySize) || 0;
    const pUsed = Number(p.used ?? p.CycleCapacityUsed) || 0;
    const pRemain = Number(p.remain ?? p.CycleCapacityRemain) || 0;
    const endTime = p.cycle_end_time || p.cycleEndTime || p.CycleEndTime || p.expired_time || p.ExpiredTime || '';
    const startTime = p.cycle_start_time || p.cycleStartTime || p.CycleStartTime || '';

    // no_expiry：上游的抵扣截止时间远超真实周期，CycleEndTime 只是每月
    // 计费边界。这种包不能拿周期结束算倒计时，否则表格会显示「剩 12 天」
    // 而最近到期卡片又正确地不认它（#474）。
    const noExpiry = !!p.no_expiry;
    let days = (p.days_left !== undefined && p.days_left !== null) ? Number(p.days_left) : null;
    if(days === null && !noExpiry && endTime && endTime !== '-'){
      try {
        const cleanStr = endTime.replace(/-/g, '/').replace('T', ' ');
        const endTs = new Date(cleanStr).getTime();
        if(!isNaN(endTs)){
          days = Math.round((endTs - Date.now()) / 86400000 * 10) / 10;
        }
      } catch(e){}
    }
    const isExpired = p.is_expired !== undefined ? !!p.is_expired : (days !== null && days < 0);
    return {
      raw: p,
      name: p.name || '套餐包',
      package_code: p.package_code || p.packageCode || p.PackageCode || '',
      resource_id: p.resource_id || p.resourceId || p.ResourceId || '',
      grant_reason: p.grant_reason || p.grantReason || '',
      sub_product_name: p.sub_product_name || p.subProductName || p.SubProductName || '',
      in_usage: p.in_usage ?? p.inUsage ?? p.InUsage,
      auto_renew: p.auto_renew ?? p.autoRenew ?? p.AutoRenewFlag,
      size: pSize,
      used: pUsed,
      remain: pRemain,
      cycle_start_time: startTime,
      cycle_end_time: endTime,
      days_left: days,
      no_expiry: noExpiry,
      is_expired: isExpired
    };
  });

  // 过滤
  let pkgs = preparedPkgs.filter(p => {
    const remain = p.remain;
    const isExpired = p.is_expired;
    const daysLeft = p.days_left;

    if(CURRENT_PKG_FILTER === 'active'){
      if(remain <= 0 || isExpired) return false;
    } else if(CURRENT_PKG_FILTER === 'expiring'){
      if(isExpired || remain <= 0) return false;
      if(daysLeft === null || daysLeft > 7) return false;
    } else if(CURRENT_PKG_FILTER === 'exhausted'){
      if(remain > 0 && !isExpired) return false;
    }

    if(CURRENT_PKG_SEARCH){
      const q = CURRENT_PKG_SEARCH;
      const match = (p.name && p.name.toLowerCase().includes(q))
        || (p.package_code && p.package_code.toLowerCase().includes(q))
        || (p.grant_reason && p.grant_reason.toLowerCase().includes(q))
        || (p.sub_product_name && p.sub_product_name.toLowerCase().includes(q))
        || (p.resource_id && p.resource_id.toLowerCase().includes(q));
      if(!match) return false;
    }
    return true;
  });

  // 排序
  pkgs.sort((a, b) => {
    if(CURRENT_PKG_SORT === 'expiry_asc'){
      const inUseA = a.in_usage ? 0 : 1;
      const inUseB = b.in_usage ? 0 : 1;
      if(inUseA !== inUseB) return inUseA - inUseB;
      const expA = a.is_expired ? 1 : 0;
      const expB = b.is_expired ? 1 : 0;
      if(expA !== expB) return expA - expB;
      const daysA = a.days_left !== null ? a.days_left : 99999;
      const daysB = b.days_left !== null ? b.days_left : 99999;
      if(daysA !== daysB) return daysA - daysB;
      return (b.remain || 0) - (a.remain || 0);
    } else if(CURRENT_PKG_SORT === 'remain_desc'){
      return (b.remain || 0) - (a.remain || 0);
    } else if(CURRENT_PKG_SORT === 'used_desc'){
      return (b.used || 0) - (a.used || 0);
    } else if(CURRENT_PKG_SORT === 'size_desc'){
      return (b.size || 0) - (a.size || 0);
    }
    return 0;
  });

  if(!pkgs.length){
    tbody.innerHTML = '<tr><td colspan="6" class="empty" style="text-align:center;padding:24px;color:var(--dim)">无符合条件的套餐权益包</td></tr>';
    return;
  }

  tbody.innerHTML = pkgs.map(p => {
    const pSize = p.size;
    const pUsed = p.used;
    const pRemain = p.remain;
    const pct = pSize > 0 ? Math.min(100, (pUsed / pSize) * 100) : 0;
    const days = p.days_left;

    // 到期倒计时与周期格式化
    let expiryBadge = '';
    if(p.is_expired || (days !== null && days < 0)){
      expiryBadge = '<span class="badge mini off">已过期</span>';
    } else if(days !== null){
      if(days <= 3){
        expiryBadge = '<span class="badge mini bad">剩 ' + days.toFixed(1) + ' 天 (即将到期)</span>';
      } else if(days <= 7){
        expiryBadge = '<span class="badge mini warn">剩 ' + Math.ceil(days) + ' 天</span>';
      } else if(days < 365){
        expiryBadge = '<span class="badge mini ok">剩 ' + Math.floor(days) + ' 天</span>';
      } else {
        expiryBadge = '<span class="badge mini">长期有效</span>';
      }
    } else if(p.no_expiry){
      expiryBadge = '<span class="badge mini">长期有效</span>';
    } else if(p.cycle_end_time){
      expiryBadge = '<span class="badge mini">正常</span>';
    }

    const cycleEndTimeStr = p.cycle_end_time ? p.cycle_end_time.slice(0, 19) : '-';
    const cycleStartTimeStr = p.cycle_start_time ? p.cycle_start_time.slice(0, 19) : '';

    // 来源/原因/产品描述
    const reasonParts = [];
    if(p.grant_reason) reasonParts.push(esc(p.grant_reason));
    if(p.sub_product_name && p.sub_product_name !== p.name) reasonParts.push(esc(p.sub_product_name));
    const reasonLine = reasonParts.length ? ('<div style="font-size:11px;color:var(--dim);margin-top:2px">' + reasonParts.join(' · ') + '</div>') : '';

    // 标识行
    const idParts = [];
    if(p.package_code) idParts.push(esc(p.package_code));
    if(p.resource_id) idParts.push(esc(p.resource_id));
    const idLine = idParts.length ? ('<div class="mono" style="font-size:10px;color:var(--dim);margin-top:2px">' + idParts.join(' · ') + '</div>') : '';

    // 状态标签
    let statusTags = '';
    if(p.in_usage){
      statusTags += '<span class="badge mini s" style="margin-left:4px;font-size:10px">生效中</span>';
    }
    if(p.auto_renew){
      statusTags += '<span class="badge mini ok" style="margin-left:4px;font-size:10px">自动续费</span>';
    }

    const barColor = pct >= 90 ? "var(--bad)" : pct >= 70 ? "var(--warn)" : "var(--accent)";

    return '<tr>'
      + '<td style="text-align:left;padding:8px 10px">'
      +   '<div style="display:flex;align-items:center;flex-wrap:wrap;gap:4px">'
      +     '<span style="font-weight:600;color:var(--fg)">' + esc(p.name) + '</span>'
      +     statusTags
      +   '</div>'
      +   reasonLine
      +   idLine
      + '</td>'
      + '<td style="text-align:center;padding:8px 10px">'
      +   (expiryBadge ? ('<div style="margin-bottom:3px">' + expiryBadge + '</div>') : '')
      +   '<div class="mono" style="font-size:11px;color:var(--fg)" title="' + (p.no_expiry ? '计费周期结束（积分不过期）' : '到期时间') + '">' + esc(cycleEndTimeStr) + '</div>'
      +   (cycleStartTimeStr ? ('<div class="mono" style="font-size:10px;color:var(--dim)" title="生效时间">自 ' + esc(cycleStartTimeStr.slice(0, 10)) + '</div>') : '')
      + '</td>'
      + '<td style="text-align:right;padding:8px 10px;font-weight:600">' + fmt(pSize) + '</td>'
      + '<td style="text-align:right;padding:8px 10px;color:var(--dim)">' + fmt(pUsed) + '</td>'
      + '<td style="text-align:right;padding:8px 10px;color:var(--accent2);font-weight:700;font-size:13px">' + fmt(pRemain) + '</td>'
      + '<td style="text-align:center;padding:8px 10px">'
      +   '<div style="display:flex;align-items:center;justify-content:center;gap:6px">'
      +     '<span style="font-size:11px;min-width:34px;text-align:right">' + pct.toFixed(1) + '%</span>'
      +     '<div style="width:46px;height:5px;background:var(--line);border-radius:3px;overflow:hidden">'
      +       '<div style="width:' + pct.toFixed(1) + '%;height:100%;background:' + barColor + '"></div>'
      +     '</div>'
      +   '</div>'
      + '</td>'
      + '</tr>';
  }).join('');
}

function reloginFromCreditsModal(){
  const acct = (window.ACCOUNTS || []).find(a => a.uid === CURRENT_CREDIT_DETAIL_UID);
  const realm = acct ? acct.realm : (window.VIEW_REALM || "intl");
  closeCreditsDetail();
  openLoginModal(realm);
}

function closeCreditsDetail(){
  const m = document.getElementById("creditsDetailModal");
  if(m) m.classList.remove("show");
  CURRENT_CREDIT_DETAIL_UID = null;
}

function openDesktopScan(){
  const m = document.getElementById('desktopScanModal');
  const body = document.getElementById('desktopScanBody');
  if(body) body.innerHTML = '<p class="hint">正在检测本机桌面客户端账号…</p>';
  if(m) m.classList.add('show');
}

function closeDesktopScan(){
  const m = document.getElementById('desktopScanModal');
  if(m) m.classList.remove('show');
}

async function refreshDesktopScan(btn){
  if(btn){ btn.disabled = true; btn.textContent = '扫描中...'; }
  const body = document.getElementById('desktopScanBody');
  if(body) body.innerHTML = '<p class="hint">正在检测本机桌面客户端账号…</p>';
  try{
    const r = await postJSON('/accounts/import/desktop', {});
    renderDesktopScan(r.detected || [], r.atrest || null);
  }catch(e){
    if(body) body.innerHTML = '<p class="hint" style="color:var(--warn)">扫描失败: ' + esc(e.message) + '</p>';
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '重新扫描'; }
  }
}

/* at-rest 密钥：桌面端把 token 加密存了，得先从客户端进程内存里把静态密钥
   找回来才能解密导入。密钥只留在网关进程内存里，不落盘、不写日志。 */
async function recoverAtrestKey(btn){
  const idle = btn.textContent;
  btn.disabled = true;
  btn.textContent = '正在扫描 WorkBuddy.exe 内存…';
  try{
    const r = await postJSON('/accounts/import/desktop', {recoverKey: true});
    if(!r.ok){ throw new Error(r.msg || '回收失败'); }
    toast('密钥已回收，正在重新扫描');
    await refreshDesktopScan();
  }catch(e){
    toast('回收失败: ' + e.message, 'err');
    btn.disabled = false;
    btn.textContent = idle;
    const tip = document.getElementById('atrestTip');
    if(tip){
      tip.innerHTML = '回收失败：' + esc(e.message)
        + '<br><span style="color:var(--dim)">请确认 WorkBuddy 桌面客户端正在运行并已登录；'
        + '权限不足时可以管理员身份运行网关再试一次。</span>';
    }
  }
}

/* 手工导入解码密钥：macOS / Linux 上回收不了客户端进程内存（huntSupported
   为 false），用户可以把保护密钥贴进来。后端会用 keyblob 自检，贴错直接报错。 */
async function adoptAtrestKey(btn){
  const input = document.getElementById('adoptKeyInput');
  const value = (input && input.value || '').trim();
  if(!value){ toast('请先粘贴密钥', 'err'); return; }
  const idle = btn.textContent;
  btn.disabled = true;
  btn.textContent = '校验中…';
  try{
    const r = await postJSON('/accounts/import/desktop', {keyText: value});
    if(!r.ok){ throw new Error(r.msg || '导入失败'); }
    if(input) input.value = '';
    toast('密钥已导入，正在重新扫描');
    await refreshDesktopScan();
  }catch(e){
    toast('导入失败: ' + e.message, 'err');
    btn.disabled = false;
    btn.textContent = idle;
    const tip = document.getElementById('atrestTip');
    if(tip){
      tip.innerHTML = '导入失败：' + esc(e.message)
        + '<br><span style="color:var(--dim)">密钥必须与桌面客户端 keyblob 里的 '
        + 'protectorKeyId 对得上；贴错或客户端已换钥都会走到这里。</span>';
    }
  }
}

function renderAtrestBar(atrest, list){
  if(!atrest) return '';
  if(atrest.keyCached){
    // 手工导入的密钥会随客户端换钥失效，所以把来路也写出来，便于用户对照。
    const how = atrest.keySource === 'manual' ? '手动导入' : '进程内存回收';
    return '<div class="hint" style="margin:0 0 12px;color:var(--accent2)">解码密钥已就绪（keyId '
      + esc(atrest.keyId || '-') + '，' + how + '），加密凭据可直接导入。</div>';
  }
  if(!list.some(d => d.needsKey)) return '';
  let html = '<div style="background:var(--panel2);border:1px solid var(--line);border-radius:10px;'
    + 'padding:12px 14px;margin-bottom:14px;font-size:13px">'
    + '<b>检测到加密凭据</b><div class="hint" style="margin:6px 0 8px" id="atrestTip">';
  if(atrest.huntSupported){
    html += '桌面客户端把 token 加密存储了，需要从正在运行的客户端进程内存里回收静态密钥'
      + '（只读，密钥只留在网关内存里）。通常几秒钟，内存大时要十几秒。'
      + '<br>回收不到时，也可以在下面手动粘贴密钥。';
  }else{
    html += esc(atrest.huntHint || '当前系统不支持从进程内存回收密钥。')
      + '<br>可以在下面手动粘贴解码密钥，或改用「+ 添加账号 (OAuth)」走浏览器登录。';
  }
  html += '</div>';
  if(atrest.huntSupported){
    html += '<button class="primary mini" onclick="recoverAtrestKey(this)">回收密钥</button>';
  }
  html += '<div style="display:flex;gap:8px;align-items:center;margin-top:10px">'
    + '<input id="adoptKeyInput" class="slot-input" type="password" autocomplete="off"'
    + ' spellcheck="false" placeholder="粘贴解码密钥：64 位 hex，或 44 字符 base64"'
    + ' style="flex:1;max-width:460px">'
    + '<button class="mini" onclick="adoptAtrestKey(this)">导入密钥</button>'
    + '</div>'
    + '<div class="hint" style="margin-top:6px">手工导入的密钥只留在网关内存里，'
    + '不落盘、不写日志；导入时会先用 keyblob 校验，贴错会直接报错。</div>';
  return html + '</div>';
}

function renderDesktopScan(list, atrest){
  const body = document.getElementById('desktopScanBody');
  if(!body) return;
  const valid = list.filter(d => d.valid);
  const bad = list.filter(d => !d.valid);
  const needKey = bad.filter(d => d.needsKey);
  const broken = bad.filter(d => !d.needsKey);
  // 回收只在 Windows 上有；其他平台这一列改成「导入」（手工粘贴）。
  const keyAction = (atrest && atrest.huntSupported) ? '需要先回收密钥' : '需要先导入密钥';
  // Use every account across both realms: the visible list is filtered by the
  // current view, so an earlier import could otherwise look "not imported".
  const already = window.SCAN_POOL && window.SCAN_POOL.size
    ? window.SCAN_POOL
    : new Set((window.ACCOUNTS || []).map(a => a.uid));

  const section = (title, items, cls) => {
    let html = '<div style="margin:14px 0 6px;font-size:12px;font-weight:600" class="' + cls + '">'
      + title + ' <span style="color:var(--dim);font-weight:400">(' + items.length + ')</span></div>';
    if(!items.length){
      html += '<div class="hint" style="margin:4px 0 10px">未检测到该版本账号</div>';
      return html;
    }
    html += '<table><thead><tr><th>账号</th><th>域名</th><th>有效期</th><th></th></tr></thead><tbody>';
    items.forEach(d => {
      const dup = already.has(d.uid);
      // NOTE: never interpolate the path into an inline onclick - JSON.stringify
      // emits double quotes, which would terminate the HTML attribute and make
      // the button silently do nothing. Use a data attribute + delegation.
      const act = dup
        ? '<span class="badge off">已导入</span>'
        : '<button class="primary mini btn-import-desktop"'
          + ' data-path="' + esc(d.path) + '"'
          + ' data-realm="' + esc(d.realm) + '">导入</button>';
      html += '<tr>'
        + '<td><b>' + esc(d.nickname || (d.uid||'').slice(0,8)) + '</b>'
        + '<div class="mono" style="font-size:11px;color:var(--dim)">' + esc((d.uid||'').slice(0,8)) + ' · ' + esc(d.file) + '</div></td>'
        + '<td class="mono" style="font-size:11px">' + esc(d.domain || '-') + '</td>'
        + '<td>' + esc(d.expiresIn || '未知') + '</td>'
        + '<td style="text-align:right">' + act + '</td>'
        + '</tr>';
    });
    html += '</tbody></table>';
    return html;
  };

  let html = renderAtrestBar(atrest, list);
  if(!list.length && !html){
    body.innerHTML = '<p class="hint">未在本机检测到桌面客户端登录凭证。<br>'
      + '请确认已安装并登录 WorkBuddy 客户端后点「重新扫描」。</p>';
    return;
  }
  if(list.length){
    html += section('国际版 (Global) · www.workbuddy.ai',
                    valid.filter(d => d.realm !== 'cn'), '');
    html += section('国内版 (China) · copilot.tencent.com',
                    valid.filter(d => d.realm === 'cn'), '');
  }

  if(needKey.length){
    html += '<div style="margin:14px 0 6px;font-size:12px;font-weight:600;color:var(--warn)">'
      + '待解码的凭证（加密存储）</div>';
    html += '<table><tbody>';
    needKey.forEach(d => {
      html += '<tr><td class="mono" style="font-size:11px">' + esc(d.file) + '</td>'
        + '<td style="color:var(--dim);font-size:12px;text-align:right">' + esc(keyAction) + '</td></tr>';
    });
    html += '</tbody></table>';
  }
  if(broken.length){
    html += '<div style="margin:14px 0 6px;font-size:12px;font-weight:600;color:var(--warn)">无法读取的凭证</div>';
    html += '<table><tbody>';
    broken.forEach(d => {
      html += '<tr><td class="mono" style="font-size:11px">' + esc(d.file) + '</td>'
        + '<td style="color:var(--warn);font-size:12px;text-align:right">' + esc(d.error || '未知原因') + '</td></tr>';
    });
    html += '</tbody></table>';
  }
  if(!html){
    html = '<p class="hint">未在本机检测到桌面客户端登录凭证。<br>'
      + '请确认已安装并登录 WorkBuddy 客户端后点「重新扫描」。</p>';
  }
  body.innerHTML = html;
  // Wire the import buttons through event delegation (see renderDesktopScan).
  body.querySelectorAll('.btn-import-desktop').forEach(btn => {
    btn.addEventListener('click', () => {
      doImportDesktop(btn, btn.getAttribute('data-path'), btn.getAttribute('data-realm'));
    });
  });
}

async function doImportDesktop(btn, path, realm){
  btn.disabled = true;
  btn.textContent = '导入中...';
    try{
    const r = await postJSON('/accounts/import/desktop', {path: path, realm: realm});
    const a = (r.imported || [])[0] || {};
    toast('已导入 ' + (a.nickname || (a.uid||'').slice(0,8)) + ' 到 ' + (a.realm === 'cn' ? '国内版' : '国际版'));
    if(a.uid && window.SCAN_POOL) window.SCAN_POOL.add(a.uid);
    await loadAccounts();
    await refresh();
    try {
      const s = await postJSON('/accounts/import/desktop', {});
      if(s.pool_uids) window.SCAN_POOL = new Set(s.pool_uids);
      renderDesktopScan(s.detected || [], s.atrest || null);
    } catch(e2) { /* keep the refreshed toasts; list will refresh on next scan */ }
    // The scan list is filtered by the current view; tell the user where the
    // account actually landed so this never looks like a no-op.
    if(a.realm && a.realm !== window.VIEW_REALM){
      toast('该账号属于' + (a.realm === 'cn' ? '国内版' : '国际版')
            + '列表，点击上方选项卡即可查看', 'err');
    }
  }catch(e){
    toast('导入失败: ' + e.message, 'err');
    btn.disabled = false;
    btn.textContent = '导入';
  }
}
