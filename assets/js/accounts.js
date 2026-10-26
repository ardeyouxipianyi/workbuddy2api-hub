/* ------------------------------- accounts ------------------------------- */
let USAGE_BY_ACCOUNT = {};

/* The API sends epoch seconds; display the recovery in the browser's local time. */
function fmtCoolAt(epoch){
  const seconds = Number(epoch);
  if(!Number.isFinite(seconds) || seconds <= 0) return '';
  const d = new Date(seconds * 1000);
  if(!Number.isFinite(d.getTime())) return '';
  const two = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + two(d.getMonth() + 1) + '-' + two(d.getDate())
    + ' ' + two(d.getHours()) + ':' + two(d.getMinutes());
}
// 徽章里放不下完整时间，折算成剩余量：45s / 29m / 2h05m。
function fmtLeft(seconds){
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  if(s < 60) return s + 's';
  const m = Math.floor(s / 60);
  if(m < 60) return m + 'm';
  return Math.floor(m / 60) + 'h' + String(m % 60).padStart(2, '0') + 'm';
}
// 模型冷却 / 达日限额的胶囊：从账号列里拆出来，单独占一整行（colspan 跨满表头
// 列数），文本完整可见、不再被 180px 的账号列切掉。
//
// 胶囊的悬停提示只补「可见文本没说的部分」——原因与影响范围。模型名、撞线 /
// 恢复时刻、本地时间口径都已经画在胶囊上了，再塞进 title 就是同一句话说两遍
// （第一版就是那样：悬停里一半是重复内容，用户明确要求去掉）。
// 用整句常量而不是拼动态值：title 在 i18n 的 ATTRS 里，整句才能被 DICT 精确
// 命中；一旦拼进模型名与时间戳，整串命不中键，只剩逐片段匹配，容易漏译。
const TIP_COOL_HIT = '上游判定该模型额度用满（code 6004）；仅此模型受限，同账号其他模型照常接单';
const TIP_COOL_PLAIN = '上游临时限流（429），到点自动恢复；仅此模型受限，同账号其他模型照常接单';
const TIP_LIMIT = '该模型今日 token 用量达到「按模型每日 Token 限额」；仅此模型受限，同账号其他模型照常接单';
const COOL_ROW_COLS = 11;   // 与 accounts 表头的列数一致
function coolRow(a){
  const list = a && Array.isArray(a.modelCooldowns) ? a.modelCooldowns : [];
  const pills = list.map(c => {
    const at = c && fmtCoolAt(c.expiresAt);
    if(!at) return '';
    // 撞过线（上游 code 6004）的模型带 cappedAt：把撞线时间一并写出来，
    // 两段时间配合就能看出这是窗口配额用满，而不是一次临时限流。
    const hit = c && fmtCoolAt(c.cappedAt);
    return hit
      ? '<span class="cool-pill" title="' + esc(TIP_COOL_HIT) + '">'
        + esc(c.model) + ' · 撞线 ' + hit + ' · 恢复 ' + at + '（时间为本地时间）</span>'
      // 这一支的措辞刻意与 6004 那支错开：用「恢复时刻」而不是「恢复」。因为
      // DICT 里已有一条 ' 恢复': ''（给「剩余用量」的「），额度 X 恢复」用，
      // 那边整词要并进 returns at），胶囊若也用「X 恢复」，英文会被那段规则
      // 先手吃掉，变成「18:04(times are local)」——「back」整个丢了。
      : '<span class="cool-pill" title="' + esc(TIP_COOL_PLAIN) + '">'
        + esc(c.model) + ' · 恢复时刻 ' + at + '（时间为本地时间）</span>';
  }).filter(Boolean);
  if(!pills.length) return '';
  return '<tr class="cool-row"><td colspan="' + COOL_ROW_COLS + '">'
    + '<div class="cool-list">' + pills.join('') + '</div></td></tr>';
}

// 按模型达日限额的清单（与冷却同一行承载）：只影响列出的模型，其他模型照常。
function limitRow(a){
  const limit = Number(a.modelDailyTokenLimit || 0);
  if(limit <= 0 || !a.modelDailyTokens) return '';
  const hit = Object.entries(a.modelDailyTokens)
    .filter(([mid, v]) => Number(v) >= limit);
  if(!hit.length) return '';
  return '<tr class="cool-row"><td colspan="' + COOL_ROW_COLS + '">'
    + '<div class="cool-list">' + hit.map(([mid, v]) =>
        '<span class="cool-pill" title="' + esc(TIP_LIMIT) + '">'
        + esc(mid) + ' · 今日已用 ' + fmt(v)
        + ' / 上限 ' + fmt(limit) + ' token 已达限额（本地 0 点恢复）</span>'
      ).join('') + '</div></td></tr>';
}

// ---- 每日活动（国内签到 / 国际活跃）------------------------------------
// GET /activity/history 是唯一事实来源。账号行上的状态只由「今天已记录的事件」
// 推导：**没有记录不等于失败**，所以缺记录时给的是中性文案而不是红色。账号自带
// 的 lastCheckin / lastDailyChat 只在「今天确实完成过、但这一版还没留下历史行」
// 时补一个中性的「今日已完成」，不据此伪造事件、时间或说明。
//
// 关联键始终是 uid：切区域、刷新账号都只是重画同一份按 uid 建的表，另一个账号的
// 事件不会画到这一行上。
let ACTIVITY_TODAY = {byUid: {}, error: '', loaded: false};
const ACT_FILTER = {range: '7d', uid: '', task: '', result: ''};

const ACTIVITY_TASKS = {checkin: '国内签到', daily_chat: '国际活跃'};
const ACTIVITY_TRIGGERS = {scheduler: '定时', manual: '手动',
                           account_add: '添加账号', account_import: '导入账号'};

function todayStamp(){
  const d = new Date();
  const pad = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
}

// 一个账号今天的活动状态。
// 国际版只有一项：今日活跃（daily_chat 流水或 lastDailyChat）。
// 国内版有两项独立活动：
//   1. Buddy 加油站（checkin 流水或 lastCheckin）
//   2. 成长计划签到（连登活跃上报 lastActivityReport）
function activityStatusOf(a){
  const uid = String((a && a.uid) || '');
  const isCn = (a && a.realm) === 'cn';
  if(!isCn){
    const label = '今日活跃';
    const done = a && a.lastDailyChat;
    const event = ACTIVITY_TODAY.byUid[uid];
    if(event){
      return {kind: event.ok ? 'ok' : 'failed', label: label,
              state: event.ok ? '成功' : '失败',
              at: String(event.ts || '').replace('T', ' ').slice(0, 19),
              message: String(event.message || '')};
    }
    if(done && String(done).startsWith(todayStamp())){
      return {kind: 'done', label: label, state: '已完成', at: '', message: ''};
    }
    return {kind: 'none', label: label, state: '无记录', at: '', message: ''};
  }

  // 国内版：加油站
  const event = ACTIVITY_TODAY.byUid[uid];
  const gasDone = a && a.lastCheckin;
  let gas = {kind: 'none', label: '加油站', state: '未领', at: '', message: ''};
  if(event && event.task === 'checkin'){
    gas = {kind: event.ok ? 'ok' : 'failed', label: '加油站',
           state: event.ok ? '已领' : '失败',
           at: String(event.ts || '').replace('T', ' ').slice(0, 19),
           message: String(event.message || '')};
  } else if(gasDone && String(gasDone).startsWith(todayStamp())){
    gas = {kind: 'done', label: '加油站', state: '已领', at: '', message: ''};
  }

  // 国内版：成长计划连登打卡
  const streakDone = a && a.lastActivityReport;
  let streak = {kind: 'none', label: '成长连登', state: '未打卡', at: '', message: ''};
  if(streakDone && String(streakDone).startsWith(todayStamp())){
    streak = {kind: 'done', label: '成长连登', state: '已打卡', at: String(streakDone), message: '连登活跃已上报'};
  }

  return {isCn: true, gas: gas, streak: streak,
          // 整体是否算已完成（供任务页统计卡片判断）
          kind: (gas.kind === 'ok' || gas.kind === 'done') ? 'done' : 'none'};
}

function activityStatusChip(a){
  const status = activityStatusOf(a);
  const uidAttr = esc(String((a && a.uid) || '').replace(/[^\w.@-]/g, ''));
  if(!status.isCn){
    const cls = status.kind === 'ok' ? 'badge ok'
              : status.kind === 'failed' ? 'badge bad'
              : status.kind === 'done' ? 'badge ok' : 'badge off';
    const tip = status.kind === 'none'
      ? status.label + '：今天还没有记录（没有记录不等于失败）'
      : (status.label + '：' + status.state
         + (status.at ? ' · ' + status.at : '')
         + (status.message ? ' · ' + status.message : ''));
    return '<span class="' + cls + '" style="cursor:pointer" title="' + esc(tip) + '"'
      + ' onclick="openActivityHistory(&quot;' + uidAttr + '&quot;)">'
      + esc(status.label + ' · ' + status.state) + '</span>';
  }

  // 国内版双徽章渲染：主徽章保持「今日签到 · 成功/失败/已完成/无记录」，保证历史兼容；副徽章标明「成长计划」连登状态
  const mainStatus = (status.gas.kind === 'ok') ? {cls: 'badge ok', text: '今日签到 · 成功', tip: status.gas.message}
                   : (status.gas.kind === 'failed') ? {cls: 'badge bad', text: '今日签到 · 失败', tip: status.gas.message}
                   : (status.gas.kind === 'done') ? {cls: 'badge ok', text: '今日签到 · 已完成', tip: ''}
                   : {cls: 'badge off', text: '今日签到 · 无记录', tip: '今天还没有记录'};

  const gasTip = 'Buddy加油站领取：' + (status.gas.state === '已领' ? '今日已领' : status.gas.state)
               + (status.gas.at ? ' · ' + status.gas.at : '')
               + (mainStatus.tip ? ' · ' + mainStatus.tip : '');

  const stkCls = (status.streak.kind === 'done') ? 'badge ok' : 'badge off';
  const stkTip = '成长计划连登活跃：' + status.streak.state
               + (status.streak.at ? ' · ' + status.streak.at : '');

  return '<div style="display:inline-flex;flex-direction:column;gap:4px;align-items:center">'
    + '<span class="' + mainStatus.cls + '" style="cursor:pointer;white-space:nowrap" title="' + esc(gasTip) + '"'
    + ' onclick="openActivityHistory(&quot;' + uidAttr + '&quot;)">'
    + esc('Buddy 加油站 · ' + (status.gas.kind === 'none' ? '未领' : status.gas.state)) + '</span>'
    + '<span class="' + stkCls + '" style="cursor:pointer;white-space:nowrap" title="' + esc(stkTip) + '"'
    + ' onclick="openActivityHistory(&quot;' + uidAttr + '&quot;)">'
    + esc('成长计划 · ' + (status.streak.kind === 'done' ? '已签到' : '未签到')) + '</span>'
    + '</div>'
    + '<span style="display:none">' + esc(mainStatus.text) + '</span>';
}

// 今天的事件，按 uid 建表。rows 是最新在前，所以第一次见到的就是最近一次。
// 读不到历史只是没有状态可显示：绝不能让它把账号区一起带坏。
async function loadActivityToday(){
  try{
    const payload = await getJSON('/activity/history?range=today&limit=200');
    const rows = (payload && Array.isArray(payload.rows)) ? payload.rows : [];
    const byUid = {};
    rows.forEach(row => {
      const uid = String((row && row.uid) || '');
      if(uid && !byUid[uid]) byUid[uid] = row;
    });
    ACTIVITY_TODAY = {byUid: byUid, error: '', loaded: true};
  }catch(e){
    ACTIVITY_TODAY = {byUid: {}, error: (e && e.message) || '读取失败', loaded: true};
  }
}

// 国际版每日活跃的现场面板：时段、下次到点、正在跑的账号与未完成名单。
// 名单与「未活跃打卡一键打卡」同源（pending 由 updateTaskPageCheckinCards 传进来），
// 原因取今天最近一条活跃流水的说明，所以「没跑」和「失败」在界面上分得开。
let DAILY_CHAT_PANEL_CACHE = {at: 0, data: null};
async function fetchDailyChatScheduler(){
  const now = Date.now();
  if(DAILY_CHAT_PANEL_CACHE.data && now - DAILY_CHAT_PANEL_CACHE.at < 10000){
    return DAILY_CHAT_PANEL_CACHE.data;
  }
  try{
    const s = await getJSON('/scheduler');
    const info = (s && s.daily_chat) ? s.daily_chat : null;
    DAILY_CHAT_PANEL_CACHE = {at: now, data: info};
    return info;
  }catch(e){
    return DAILY_CHAT_PANEL_CACHE.data;
  }
}
function fmtDuration(sec){
  const s = Math.max(0, Math.round(Number(sec) || 0));
  if(s < 60) return s + 's';
  const m = Math.floor(s / 60);
  return m + 'm' + String(s % 60).padStart(2, '0') + 's';
}
async function renderDailyChatPanel(pending, intlAccts){
  const slotsEl = document.getElementById('intlDailyChatSlots');
  const liveEl = document.getElementById('intlDailyChatLive');
  const pendingEl = document.getElementById('intlDailyChatPending');
  if(!slotsEl && !liveEl && !pendingEl) return;
  const info = await fetchDailyChatScheduler();
  const total = (intlAccts || []).length;
  const done = total - ((pending || []).length);
  if(slotsEl){
    if(!info || !Array.isArray(info.times) || !info.times.length){
      slotsEl.textContent = '今日时段：未读取到排程（调度器未运行？）';
    }else{
      const label = info.random ? '（每天随机，间隔 12 小时）' : '';
      const next = info.next_slot ? (' · 下次 ' + info.next_slot) : ' · 今日时段已全部触发';
      slotsEl.textContent = '今日时段 ' + info.times.join(' / ') + label + next
        + ' · 完成 ' + done + '/' + total
        + (info.budget_s ? (' · 单账号预算 ' + fmtDuration(info.budget_s)) : ' · 预算不限制');
    }
  }
  if(liveEl){
    const live = (info && Array.isArray(info.live)) ? info.live : [];
    if(!live.length){
      liveEl.textContent = '';
    }else{
      liveEl.innerHTML = live.map(function(item){
        const left = item.remaining_s == null ? '' : (' / 剩余 ' + fmtDuration(item.remaining_s));
        return '<span style="color:var(--accent)">运行中 ' + esc(item.uid8 || '') + ' · 已用 '
          + fmtDuration(item.elapsed_s) + left + '</span>';
      }).join('<br>');
    }
  }
  if(pendingEl){
    const list = pending || [];
    if(!list.length){
      pendingEl.innerHTML = '<span style="color:var(--accent2)">今天全部完成</span>';
      return;
    }
    const rows = list.map(function(a){
      const uid8 = String(a.uid || '').slice(0, 8);
      const nick = a.nickname || '';
      const row = (ACTIVITY_TODAY && ACTIVITY_TODAY.byUid) ? ACTIVITY_TODAY.byUid[a.uid] : null;
      let reason = '未到时段';
      if(row && row.message){
        reason = (row.ok === false ? '失败：' : '') + String(row.message);
      }
      return '<div><span style="color:var(--dim)">·</span> ' + esc(uid8)
        + ' <span style="color:var(--fg)">' + esc(nick) + '</span> '
        + '<span style="color:var(--warn)">' + esc(reason.slice(0, 120)) + '</span></div>';
    }).join('');
    pendingEl.innerHTML = '<div style="color:var(--dim)">未完成 ' + list.length + '</div>' + rows;
  }
}

// 任务与福利页面的今日打卡/签到卡片状态：与网关与账号页面的今日活跃/今日签到完全同一口径
// 统一通过 activityStatusOf(a) 判定（以今天真实流水为准，回落到本日本地时间前缀）。
function updateTaskPageCheckinCards(){
  const allList = window.ACCOUNTS || [];
  const stateEl = document.getElementById("intlTodayActiveState");
  const intlCard = document.getElementById("cardIntlTodayActive");
  if(stateEl){
    // 卡片（与卡片里的数值）都挂上同一个 title，鼠标落在卡片任何位置都能看到名单。
    const setCardTip = tip => {
      stateEl.title = tip;
      if(intlCard) intlCard.title = tip;
    };
    const intlAccts = allList.filter(a => a.realm === 'intl' && taskActive(a));
    if(!intlAccts.length){
      stateEl.textContent = '无国际账号';
      setCardTip('');
    } else {
      const isDone = a => {
        const s = activityStatusOf(a);
        return s.kind === 'ok' || s.kind === 'done';
      };
      const pending = intlAccts.filter(a => !isDone(a));
      const doneCount = intlAccts.length - pending.length;
      if(doneCount === intlAccts.length){
        stateEl.innerHTML = '<span style="color:var(--accent2)">今日已打卡 (' + doneCount + '/' + intlAccts.length + ')</span>';
      } else if(doneCount > 0){
        stateEl.innerHTML = '<span style="color:var(--accent)">部分已打卡 (' + doneCount + '/' + intlAccts.length + ')</span>';
      } else {
        stateEl.innerHTML = '<span style="color:var(--warn)">今日未打卡 (0/' + intlAccts.length + ')</span>';
      }
      // 悬停看未打卡名单：与「未活跃打卡一键打卡」按钮要打的那批同源，看到什么就打什么。
      setCardTip(pending.length ? pendingDailyChatTip(pending) : '国际版账号今天都已打卡');
      renderDailyChatPanel(pending, intlAccts);
    }
  }

  const chkStateEl = document.getElementById("cnTodayCheckinState");
  if(chkStateEl){
    let targetAccts = allList.filter(a => a.realm === 'cn' && taskActive(a));
    if(typeof CURRENT_GROWTH_UID !== 'undefined' && CURRENT_GROWTH_UID && CURRENT_GROWTH_UID !== 'all'){
      targetAccts = targetAccts.filter(a => a.uid === CURRENT_GROWTH_UID);
    }
    if(!targetAccts.length){
      chkStateEl.textContent = '无国内账号';
    } else {
      const isDone = a => {
        const s = activityStatusOf(a);
        return s.kind === 'ok' || s.kind === 'done';
      };
      const doneCount = targetAccts.filter(isDone).length;
      if(doneCount === targetAccts.length){
        chkStateEl.innerHTML = '<span style="color:var(--accent2)">今日已领 (' + doneCount + '/' + targetAccts.length + ')</span>';
      } else if(doneCount > 0){
        chkStateEl.innerHTML = '<span style="color:var(--accent)">部分已领 (' + doneCount + '/' + targetAccts.length + ')</span>';
      } else {
        chkStateEl.innerHTML = '<span style="color:var(--warn)">今日未领 (0/' + targetAccts.length + ')</span>';
      }
    }
  }
}


// 面板拼查询串，筛选在服务端做；写错的取值服务端会回 400，这里原样透出。
function activityFilterQuery(){
  const parts = ['range=' + encodeURIComponent(ACT_FILTER.range || '7d'), 'limit=200'];
  if(ACT_FILTER.uid) parts.push('uid=' + encodeURIComponent(ACT_FILTER.uid));
  if(ACT_FILTER.task) parts.push('task=' + encodeURIComponent(ACT_FILTER.task));
  if(ACT_FILTER.result) parts.push('result=' + encodeURIComponent(ACT_FILTER.result));
  return '/activity/history?' + parts.join('&');
}

function renderActivityFilters(){
  const uidSel = document.getElementById('actUid');
  if(uidSel){
    const accounts = Array.isArray(window.ACCOUNTS) ? window.ACCOUNTS : [];
    uidSel.innerHTML = '<option value="">全部账号</option>' + accounts.map(a =>
      '<option value="' + esc(String(a.uid || '')) + '">'
      + esc((a.nickname || a.uid || '') + (a.realm === 'cn' ? ' · 国内版' : ' · 国际版'))
      + '</option>').join('');
  }
  const bind = (id, value) => {
    const el = document.getElementById(id);
    if(el) el.value = value;
  };
  bind('actRange', ACT_FILTER.range);
  bind('actUid', ACT_FILTER.uid);
  bind('actTask', ACT_FILTER.task);
  bind('actResult', ACT_FILTER.result);
}

function activityHistoryRow(row){
  const uid = String((row && row.uid) || '');
  const nick = String((row && row.nickname) || '') || (uid ? uid.slice(0, 8) : '未知账号');
  const task = ACTIVITY_TASKS[row && row.task] || '—';
  const trigger = ACTIVITY_TRIGGERS[row && row.trigger] || '';
  const message = String((row && row.message) || '');
  return '<tr>'
    + '<td class="mono" data-label="时间">'
    +   esc(String((row && row.ts) || '').replace('T', ' ').slice(0, 19)) + '</td>'
    + '<td data-label="账号">' + esc(nick)
    +   (uid ? '<div class="mono" style="color:var(--dim);font-size:11px">' + esc(uid.slice(0, 8)) + '</div>' : '')
    + '</td>'
    + '<td data-label="任务">' + esc(task)
    +   (trigger ? '<div style="color:var(--dim);font-size:11px">' + esc(trigger) + '</div>' : '')
    + '</td>'
    + '<td data-label="结果">'
    +   (row && row.ok ? '<span class="badge ok">成功</span>' : '<span class="badge bad">失败</span>')
    + '</td>'
    // 上游原文可能很长：在 span 上做行内截断（td 自身不吃 max-width），完整
    // 内容留在 title 里，模态框不会被撑宽。
    + '<td data-label="说明" style="text-align:left">'
    +   '<span style="display:inline-block;max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:bottom"'
    +   ' title="' + esc(message) + '">' + esc(message || '—') + '</span></td>'
    + '</tr>';
}

function renderActivityHistory(payload, error){
  const box = document.getElementById('activityHistoryRows');
  const meta = document.getElementById('activityHistoryMeta');
  if(!box) return;
  if(error){
    box.innerHTML = '<tr><td colspan="5" class="empty">活动记录读取失败：'
      + esc(error) + '</td></tr>';
    if(meta) meta.textContent = '';
    return;
  }
  const rows = (payload && Array.isArray(payload.rows)) ? payload.rows : [];
  const total = (payload && payload.total != null) ? payload.total : rows.length;
  if(meta) meta.textContent = rows.length ? ('显示 ' + rows.length + ' / ' + total + ' 条') : '';
  if(!rows.length){
    box.innerHTML = '<tr><td colspan="5" class="empty">这段时间没有活动记录</td></tr>';
    return;
  }
  box.innerHTML = rows.map(activityHistoryRow).join('');
}

function loadActivityHistory(){
  const box = document.getElementById('activityHistoryRows');
  if(box) box.innerHTML = '<tr><td colspan="5" class="empty">正在读取活动记录…</td></tr>';
  return getJSON(activityFilterQuery()).then(payload => {
    renderActivityHistory(payload, '');
  }).catch(e => {
    renderActivityHistory(null, (e && e.message) || '读取失败');
  });
}

function setActivityFilter(name, value){
  if(!Object.prototype.hasOwnProperty.call(ACT_FILTER, name)) return undefined;
  ACT_FILTER[name] = String(value == null ? '' : value);
  return loadActivityHistory();
}

// uid 为空表示「全部账号」；从账号行的状态徽章点进来时带上该账号，等于预筛选。
function openActivityHistory(uid){
  ACT_FILTER.uid = String(uid || '');
  const mask = document.getElementById('activityHistoryModal');
  if(mask) mask.style.display = 'flex';
  renderActivityFilters();
  return loadActivityHistory();
}

function closeActivityHistory(){
  const mask = document.getElementById('activityHistoryModal');
  if(mask) mask.style.display = 'none';
}

// 账号的手动调度权重提示：与 wb_accounts._pick_weighted_first 的行为一一对应。
const WEIGHT_TIP = '权重 >1 的账号优先接单，多个高权重账号按权重比分流量；它们都接不了单时其他账号照常顶上。已开始的会话保持粘性不换号，权重对新会话生效。1 = 不干预，临期积分优先等自动规则照旧。';

function accountRow(a){
  const realmBadge = a.realm === 'cn'
    ? '<span class="realm-badge-cn">国内版</span>'
    : '<span class="realm-badge-intl">国际版</span>';
  // 移动端卡片视图：版本列与状态列隐藏，徽章改在账号名右侧同行显示（见 ≤640px 块），
  // 桌面表格视图仍用上面的 realmBadge / state 两列，行为不变。
  const realmBadgeMobile = a.realm === 'cn'
    ? '<span class="realm-badge-cn acct-mobile-badge">国内版</span>'
    : '<span class="realm-badge-intl acct-mobile-badge">国际版</span>';
  // Inline handlers embed these in an HTML attribute, so they must survive
  // both the attribute and the quoted JS string. uid comes from an upstream
  // JWT; treat it as untrusted and keep it to a safe charset.
  const uidAttr = esc(String(a.uid || '').replace(/[^\w.@-]/g, ''));
  const realmAttr = esc(String(a.realm || 'intl').replace(/[^\w-]/g, ''));
  const nameAttr = esc(String(a.nickname || '').replace(/["'\\<>]/g, ''));
  const usage = USAGE_BY_ACCOUNT[a.uid] || {};
  const isTokenBad = !!(a.tokenExpired || a.expiresIn === 'expired');
  const state = !a.enabled
              ? (a.tasksEnabled
                 ? '<span class="badge off" title="不接 API 流量，但仍参与签到 / 任务">已停用 · 任务中</span>'
                 : '<span class="badge off">已停用</span>')
              : isTokenBad ? '<span class="badge bad" title="登录凭证已失效（上游返回 401 或已过期），请重新登录">凭证失效</span>'
              : a.reserveBlocked ? '<span class="badge warn">保留积分</span>'
              : a.dailyLimitBlocked ? '<span class="badge warn" title="今日已用 ' + fmt(a.dailyTokensToday || 0) + ' / ' + fmt(a.dailyTokenLimit || 0) + ' token，暂停接单（本地 0 点恢复）">日限额</span>'
              : a.creditLimitReached ? '<span class="badge warn" title="今日已消费 ' + Number(a.dailyCreditsToday || 0).toFixed(2) + ' / ' + (a.dailyCreditLimit || 0) + ' 积分，仅免费模型可用（本地 0 点恢复）">积分限额</span>'
              // 熔断/降权也是"不接单"，但跟上游配额无关：上游连续失败把账号
              // 关进本地保护窗口。少了这两支，账号会被画成绿色「可用」，而池子
              // 其实是空的——生产上就是这么误导排查的。
              : a.breakerFor ? '<span class="badge bad" title="上游连续失败触发熔断，本窗口内不接单；账号与凭证本身没问题，' + fmtLeft(a.breakerFor) + '后自动恢复">熔断 ' + fmtLeft(a.breakerFor) + '</span>'
              : a.degradeFor ? '<span class="badge warn" title="连续未知失败进入降权窗口，' + fmtLeft(a.degradeFor) + '后恢复">降权 ' + fmtLeft(a.degradeFor) + '</span>'
              : a.inCooldown ? '<span class="badge warn"' + (Number(a.softStreak || 0) > 1 ? ' title="连续软限流 ' + Number(a.softStreak) + ' 次，冷却时间按 2 倍递增"' : '') + '>冷却 ' + (a.cooldownFor||0) + 's</span>'
              : a.expiresIn === 'expired' ? '<span class="badge bad">已过期</span>'
              : '<span class="badge ok">可用</span>';
  // 模型冷却 / 达日限额的胶囊不放在账号列里：那里只有 180px，长模型名必然被
  // 省略号切掉。它们改成账号行之后紧跟的一整行（coolRow / limitRow）在
  // </tr> 之后输出，宽度够把话说全。
  // The visible label keeps the exact truncation it had before; the tooltip carries
  // the whole upstream body (lastErrorDetail) so a 429 reason is readable on hover
  // without widening the card.
  // 「到期 358 days」收成「到期：358d」；完整原值（data-tip）保持后端给的原样。
  const expRaw = String(a.expiresIn || '?');
  const expShort = expRaw.replace(/\s*days?$/i, 'd').replace(/\s*hours?$/i, 'h');
  // 报错只画可扫读的短标签「HTTP 429 · code:6004」；完整响应进悬浮气泡
  // （data-tip），行尾的 ⓘ 提示可悬停查看。原样打印整段响应会把账号列
  // 拉宽、也读不出重点。
  const errFull = a.lastErrorDetail || a.lastError || '';
  let err = '';
  // 模型级 429（6004）由 coolRow 画「撞线 / 恢复」；lastError 只是那次的
  // 历史记录，冷却到期后也不能把旧记录再画成账号级错误（#454）。
  if (a.lastError && a.lastError !== 'HTTP 429 (model throttled)') {
    const httpM = String(a.lastError).match(/HTTP\s+(\d{3})/);
    const codeM = String(errFull).match(/"code"\s*:\s*(\d+)/);
    const label = httpM
      ? ('HTTP ' + httpM[1] + (codeM ? ' · code:' + codeM[1] : ''))
      : String(a.lastError).slice(0, 60);
    err = '<div class="acct-err" data-tip="' + esc(errFull) + '">'
        + '<span class="err-label">' + esc(label) + '</span>'
        + '<span class="err-info" aria-hidden="true">ⓘ</span></div>';
  }
  const cred = a.credits;
  let expBadge = '';
  if(cred && cred.earliest_expiring && cred.earliest_expiring.days_left !== undefined && cred.earliest_expiring.days_left !== null){
    const d = Number(cred.earliest_expiring.days_left);
    if(d <= 3 && d >= 0){
      const expEnd = String(cred.earliest_expiring.cycle_end_time || '');
      expBadge = '<span class="badge mini bad" style="font-size:10px;padding:1px 4px" title="最近套餐包将于 ' + esc(expEnd.slice(5,10)) + ' 到期 (剩' + d.toFixed(1) + '天)">临期</span>';
    } else if(d <= 7 && d >= 0){
      const expEnd = String(cred.earliest_expiring.cycle_end_time || '');
      expBadge = '<span class="badge mini warn" style="font-size:10px;padding:1px 4px" title="最近套餐包将于 ' + esc(expEnd.slice(5,10)) + ' 到期 (剩' + Math.ceil(d) + '天)">7天内到期</span>';
    }
  }
  // 账号档位（#505）读自上游计费摘要，包名原样显示（国际版 Free、国内版体验版），
  // 与 workbuddy 官方叫法一致，不自造「免费 / 付费」标签。tier 为空 = 还没读过，
  // 那是「未知」而不是免费，所以不画任何胶囊（企业号与被风控限制的号也是空）。
  //
  // 位置放在「版本」格里、区域徽章的正下方：这一格只有一枚区域徽章（实测 76px
  // 宽、内容约 52px），是全表唯一还有余量又不会被别的元素挤到的地方。积分格
  // 实测只有 120px，加了「临期 / 明细」就顶满；账号格里的次要信息行是 10px 的
  // 可点击链接，胶囊挤在旁边既会被长名单推走、又容易被当成链接的一部分。
  // 行高不受影响：账号格本来就比这一格高。
  const tier = a.tier && typeof a.tier === 'object' ? a.tier : null;
  const tierPkg = tier && tier.package ? String(tier.package) : '';
  let tierBadge = '';
  if(tierPkg){
    const at = tier.checked_at ? new Date(tier.checked_at * 1000).toLocaleString() : '';
    tierBadge = '<span class="badge mini' + (tier.paid === true ? ' ok' : '') + '"'
      + ' title="会员类型: ' + esc(tierPkg) + (at ? ' · 读取于 ' + esc(at) : '') + '">'
      + esc(tierPkg) + '</span>';
  }
  const tierBadgeBlock = tierBadge ? '<div style="margin-top:2px">' + tierBadge + '</div>' : '';
  const credText = cred
    ? ('<div class="credit-stack">'
       + '<div class="credit-main"><b>' + fmt(cred.remain) + '</b>'
       + '<span class="credit-total">/ ' + fmt(cred.size) + '</span></div>'
       + '<div class="credit-meta">' + expBadge
       + '<span class="badge mini" style="cursor:pointer" onclick="openCreditsDetail(&quot;' + uidAttr + '&quot;)" title="点击查看积分与套餐明细">明细</span></div>'
       + '</div>')
    : ('<div class="credit-stack">'
       + '<div class="credit-main"><span style="color:var(--dim)">未查询</span></div>'
       + '<div class="credit-meta"><span class="badge mini" style="cursor:pointer" onclick="openCreditsDetail(&quot;' + uidAttr + '&quot;)" title="点击查询积分明细">查询</span></div>'
       + '</div>');
  // 权重下拉：选项给 1-10；历史值超出范围时不让 select 悄悄跳回最近的档位，
  // 补一个选中项把真实值显示出来。
  const weight = Math.max(1, Math.min(100, Number(a.weight || 1) || 1));
  let weightOpts = '';
  for(let i = 1; i <= 10; i++){
    weightOpts += '<option value="' + i + '"' + (weight === i ? ' selected' : '') + '>' + i + '</option>';
  }
  if(weight > 10) weightOpts += '<option value="' + weight + '" selected>' + weight + '</option>';
  // 人工禁用的模型名单（账号级黑名单，与自动冷却无关）
  const deny = (a.disabledModels || []);
  // 冷却 / 达日限额的胶囊紧跟本账号行另起一整行：账号列只有 180px，长模型名
  // 在那里必然被省略号切掉，整行才放得下完整文本。colspan 跨满表头列数，
  // 所以桌面与窄屏（grid 下 td[colspan] 已跨满 6 栏）都不用再各自的宽度。
  // 有续行时给账号行打 has-alert：两行是同一块内容，中间不该有分割线（见 CSS）。
  const alertRows = coolRow(a) + limitRow(a);
  return '<tr' + (alertRows ? ' class="has-alert"' : '') + '>'
    + '<td style="text-align:center;white-space:nowrap" data-label="版本">' + realmBadge + tierBadgeBlock + '</td>'
    + '<td style="text-align:left" data-label="账号">'
    // 账号名（大一号）+ 备注 chip 同一行；备注从独立一行收进账号后面。
    +   '<div class="acct-name-row">'
    +     '<span class="acct-name" title="' + esc(a.nickname) + '">' + esc(a.nickname) + '</span>'
    // 静态 API Key 账号：没有登录身份，也没有签到/成长体系，标出来省得当成
    // 普通 OAuth 号去点「重新登录」。悬停给出脱敏后的凭证，方便对上是哪一把。
    +     (a.apiKey
          ? '<span class="badge mini" style="background:var(--panel2);color:var(--dim)"'
            + ' title="静态 API Key 账号（' + esc(a.credentialMask || 'ck_***') + '）：不参与刷新 / 签到 / 成长任务，额度走企业口径"'
            + '>API Key</span>'
          : '')
    +     (a.note
          ? '<span class="note-chip has" title="点击编辑备注" onclick="editAccountNote(&quot;' + uidAttr + '&quot;)">' + esc(a.note) + '</span>'
          : '<span class="note-chip" title="点击添加备注，比如「批量」/「自用」" onclick="editAccountNote(&quot;' + uidAttr + '&quot;)">+ 备注</span>')
    // API Key 账号没有个人成长体系，签到/任务开关对它没有意义：上游一律拒绝，
    // 画出来只会让人以为点了能开。
    +     (a.apiKey ? ''
          : '<span class="badge mini ' + (a.tasksEnabled ? 'ok' : 'off') + '" style="cursor:pointer;margin-left:6px" title="参与签到 / 成长任务 / 连登管家 / 猫猫旅行 / 国际版活跃；与 API 取号无关，点一下切换" onclick="toggleAccountTasks(&quot;' + uidAttr + '&quot;,' + (!a.tasksEnabled) + ')">任务' + (a.tasksEnabled ? '开' : '关') + '</span>')
    // 移动端专用：版本 + 状态徽章靠右同行（桌面隐藏，用表格自己的两列）。
    +     '<span class="acct-mobile-badges">' + realmBadgeMobile + state + tierBadge + '</span>'
    +   '</div>'
    // 次要信息收成一行：值截断到几个字符，完整值走悬浮气泡（data-tip）。
    +   '<div class="acct-meta mono">'
    +     '<span data-tip="账号 ID：' + esc(String(a.uid || '')) + '">' + esc(a.source) + '：' + esc(String(a.uid || '').slice(0, 5)) + '…</span>'
    +     ' · <span data-tip="凭证到期：' + esc(expRaw) + '">到期：' + esc(expShort) + '</span>'
    +     (a.machineId ? ' · <span data-tip="设备码：' + esc(String(a.machineId)) + '（由账号 UID 稳定派生的专属设备码，防多号关联风控）">设备码:' + esc(String(a.machineId).slice(0, 4)) + '…</span>' : '')
    +   '</div>'
    + (deny.length
        // 这一行的可见文本没有 ellipsis，名单再长也会换行显示完整，所以悬停里
        // 只留「点击能干什么」，不再把名单重复一遍（重复即噪音）。
        ? '<div style="font-size:11px;color:var(--warn);margin-top:2px;cursor:pointer" title="' + esc('点击编辑该账号禁用的模型（逗号分隔；留空 = 不限制）') + '" onclick="editAccountDisabledModels(&quot;' + uidAttr + '&quot;)">禁用的模型: ' + esc(deny.join(', ')) + '</div>'
        : '<div style="font-size:10px;color:var(--dim);margin-top:2px;cursor:pointer;opacity:.7" title="点击设置该账号禁用的模型：命中的模型不再派给这个号，池子里其他号照常接单" onclick="editAccountDisabledModels(&quot;' + uidAttr + '&quot;)">+ 禁用模型</div>')
    + err + '</td>'
    + '<td style="text-align:center" data-label="状态">' + state + '</td>'
    + '<td style="text-align:center" data-label="今日活动">' + activityStatusChip(a) + '</td>'
    + '<td style="text-align:center" data-label="出口">' + slotSelectHtml(a) + '</td>'
    + '<td style="text-align:center;white-space:nowrap" data-label="权重">'
    +   '<select class="mini-select" title="' + esc(WEIGHT_TIP) + '" onchange="setAccountWeight(&quot;' + uidAttr + '&quot;, this.value, this)">' + weightOpts + '</select></td>'
    + '<td style="text-align:left;white-space:normal" data-label="积分">' + credText + '</td>'
    + '<td style="text-align:right" data-label="请求">' + fmt(usage.requests) + '</td>'
    + '<td style="text-align:right" data-label="总 token" data-tip="' + fmt(usage.total_tokens) + '">' + fmtCompact(usage.total_tokens) + '</td>'
    + '<td style="text-align:right" data-label="缓存" data-tip="' + fmt(usage.cached_tokens) + '">' + fmtCompact(usage.cached_tokens) + '</td>'
    + '<td class="acct-actions" style="text-align:center;white-space:nowrap;vertical-align:middle">'
    + '<div class="acct-actions-col">'
    +   '<div class="acct-actions-row">'
    +     '<button class="sec mini" onclick="toggleAccount(&quot;' + uidAttr + '&quot;,' + (!a.enabled) + ', this)">' + (a.enabled ? '停用' : '启用') + '</button>'
    +     '<button class="sec mini" onclick="testAccount(&quot;' + uidAttr + '&quot;, this)">测试</button>'
    +     (isTokenBad
          ? '<button class="bad mini" onclick="openLoginModal(&quot;' + realmAttr + '&quot;)" title="凭证已失效，点击重新扫码授权并自动更新该账号">重新登录</button>'
          : '<button class="sec mini" onclick="refreshOne(&quot;' + uidAttr + '&quot;, this)">刷新凭证</button>')
    +     '<button class="sec mini" onclick="syncProfile(&quot;' + uidAttr + '&quot;, this)" title="从 Web 控制台同步最新昵称（改名后免重登）">同步昵称</button>'
    +   '</div>'
    +   '<div class="acct-actions-row">'
    +     '<span class="id-btn-label">出站身份：</span>'
    +     '<div class="id-btn-group" title="出站身份：WB = WorkBuddy 独立桌面客户端（默认），VSC = 官方 VSCode 插件，CLI = 官方 CodeBuddy CLI；三者对应不同的出站指纹与配额通道。">'
    +       '<button class="id-btn' + ((!a.product || a.product === 'workbuddy') ? ' active' : '') + '" onclick="setAccountProduct(&quot;' + uidAttr + '&quot;, &quot;workbuddy&quot;, this)">WB</button>'
    +       '<button class="id-btn' + (a.product === 'vscode' ? ' active' : '') + '" onclick="setAccountProduct(&quot;' + uidAttr + '&quot;, &quot;vscode&quot;, this)">VSC</button>'
    +       '<button class="id-btn' + (a.product === 'cli' ? ' active' : '') + '" onclick="setAccountProduct(&quot;' + uidAttr + '&quot;, &quot;cli&quot;, this)">CLI</button>'
    +     '</div>'
    +     '<button class="sec mini" onclick="exportOne(&quot;' + uidAttr + '&quot;)">导出</button>'
    +     '<button class="danger mini" onclick="deleteAccount(&quot;' + uidAttr + '&quot;,&quot;' + nameAttr + '&quot;, this)">删除</button>'
    +   '</div>'
    + '</div>'
    + '</td></tr>'
    + alertRows;
}

// 「!」帮助标记的展开/收起：说明文本只写一份（在 .help-detail 里），
// 点击标记才显示，再点一次收起。
function toggleHelpDetail(id, evt){
  if(evt && evt.stopPropagation) evt.stopPropagation();
  const box = document.getElementById(id);
  if(!box) return;
  const willOpen = box.hasAttribute('hidden');
  if(willOpen) box.removeAttribute('hidden'); else box.setAttribute('hidden', '');
  const mark = document.querySelector('.help-mark[onclick*="' + id + '"]');
  if(mark) mark.classList.toggle('open', willOpen);
}

// 悬停气泡（全局单例）。用自定义 DOM 而不是原生 title：原生 tooltip 由浏览器
// 自己绘制，既截不进图、也没法控制样式与换行。元素只要带 data-tip 就生效，
// 事件走委托，动态重渲染出来的节点不必重新绑定。
let _hoverTip = null;
function moveHoverTip(e){
  if(!_hoverTip) return;
  const pad = 14;
  const w = _hoverTip.offsetWidth, h = _hoverTip.offsetHeight;
  let x = e.clientX + pad, y = e.clientY + pad;
  if(x + w > window.innerWidth - 8) x = Math.max(8, e.clientX - w - pad);
  if(y + h > window.innerHeight - 8) y = Math.max(8, e.clientY - h - pad);
  _hoverTip.style.left = x + 'px';
  _hoverTip.style.top = y + 'px';
}
function ensureHoverTip(){
  if(_hoverTip) return _hoverTip;
  const el = document.createElement('div');
  el.id = 'hoverTip';
  el.style.cssText = 'position:fixed;z-index:9999;display:none;max-width:560px;'
    + 'padding:8px 10px;border-radius:8px;background:var(--panel);border:1px solid var(--line);'
    + 'box-shadow:var(--shadow-sm);font-size:12px;line-height:1.55;color:var(--fg);'
    + 'white-space:pre-wrap;word-break:break-word;pointer-events:none';
  document.body.appendChild(el);
  _hoverTip = el;
  document.addEventListener('mouseover', e => {
    const host = e.target && e.target.closest ? e.target.closest('[data-tip]') : null;
    if(!host) return;
    el.textContent = host.getAttribute('data-tip');
    el.style.display = 'block';
    moveHoverTip(e);
  });
  document.addEventListener('mousemove', e => {
    if(el.style.display === 'block') moveHoverTip(e);
  });
  document.addEventListener('mouseout', e => {
    const host = e.target && e.target.closest ? e.target.closest('[data-tip]') : null;
    if(host) el.style.display = 'none';
  });
  return el;
}

// 当前视图的账号：realm 为空的账号两侧都可见，与账号卡片一直以来的口径一致。
// 全页只留这一处 realm 过滤表达式 —— 账号卡片与「当前禁用账号与模型」都走它，
// 两处不会各写一份、然后各自漂移。视图的真源是 window.VIEW_REALM。
function accountsInView(){
  const view = window.VIEW_REALM || "intl";
  return (window.ACCOUNTS || []).filter(a => !a.realm || a.realm === view);
}

// 当前禁用总览：把散落在账号卡片上的停用/限流状态汇总成一张表。
// 四类来源：①面板手动停用 ②上游账号级冷却 ③上游模型级冷却 ④本地日限额/积分护栏。
// 全部读 window.ACCOUNTS，不额外发请求；数据与账号卡片同源，不会互相矛盾。
// 只描述当前视图（accountsInView）：计数、分组与空状态都从这同一份过滤后的数据
// 算出来，否则切到国内版时会看到国际版的停用状态（issue #31）。
function disabledRows(){
  const rows = [];
  const now = Math.floor(Date.now() / 1000);
  // 日限额与积分护栏按本地自然日恢复，这里取下一个零点。
  const nextMidnight = (() => {
    const d = new Date(); d.setHours(24, 0, 0, 0);
    return Math.floor(d.getTime() / 1000);
  })();
  accountsInView().forEach(a => {
    const who = (a.nickname || '?') + ' (' + String(a.uid || '').slice(0, 8) + ')';
    const realm = a.realm === 'cn' ? '国内版' : '国际版';
    const detail = a.lastErrorDetail || '';
    if(!a.enabled){
      rows.push({who, realm, scope: '账号', kind: '本地停用', manual: true,
                 reason: '面板手动停用，该账号不参与轮询', until: ''});
    }
    if(a.inCooldown){
      const secs = Number(a.cooldownFor || 0);
      const streak = Number(a.softStreak || 0);
      rows.push({who, realm, scope: '账号', kind: '上游限流', detail,
                 reason: (a.lastError || '上游 429')
                       + (streak > 1 ? '（连续软限流 ' + streak + ' 次，冷却按 2 倍递增）' : ''),
                 until: secs > 0 ? fmtCoolAt(now + secs) : ''});
    }
    if(a.breakerFor){
      const secs = Number(a.breakerFor || 0);
      rows.push({who, realm, scope: '账号', kind: '熔断', detail,
                 reason: '上游连续失败触发熔断，本窗口内不接单（账号与凭证本身没问题，恢复后可继续用）',
                 until: secs > 0 ? fmtCoolAt(now + secs) : ''});
    }
    if(a.degradeFor){
      const secs = Number(a.degradeFor || 0);
      rows.push({who, realm, scope: '账号', kind: '降权', detail,
                 reason: '连续未知失败进入降权窗口，优先级被压低',
                 until: secs > 0 ? fmtCoolAt(now + secs) : ''});
    }
    (Array.isArray(a.modelCooldowns) ? a.modelCooldowns : []).forEach(c => {
      if(!c || !c.model) return;
      // 带 cappedAt 的是 6004 撞线（恢复时间来自上游）：撞线时间单独一列。
      const hit = c.cappedAt ? fmtCoolAt(c.cappedAt) : '';
      // 起始时刻：6004 用上游给的撞线时刻；其余 429 用 hub 记下的限流时刻
      // （c.startedAt）。两者都取不到时才留空——不用假值填充表格。
      const started = hit || (c.startedAt ? fmtCoolAt(c.startedAt) : '');
      // 两类模型级限流的原因必须分开写：带 cappedAt 的是上游 6004 配额耗尽
      // （恢复时间以小时计），不带 cappedAt 的只是一次 429 临时限流（60s~10m
      // 的短窗口，到点自动恢复）。共用一句「配额耗尽」会把短时 parking 说成
      // 要等几小时，也让真正该关注的 6004 淹没在里面。
      rows.push({who, realm, scope: c.model, kind: '上游限流', detail, cappedAt: hit,
                 startedAt: started,
                 reason: hit
                   ? '该模型配额耗尽（code 6004），同账号其他模型不受影响'
                   : '该模型被上游临时限流（429），仅此模型受限，到点自动恢复',
                 until: fmtCoolAt(c.expiresAt)});
    });
    if(a.dailyLimitBlocked){
      rows.push({who, realm, scope: '账号', kind: '日限额',
                 reason: '今日 token ' + Number(a.dailyTokensToday || 0).toLocaleString()
                       + ' / 上限 ' + Number(a.dailyTokenLimit || 0).toLocaleString(),
                 until: fmtCoolAt(nextMidnight)});
    }
    if(a.creditLimitReached){
      rows.push({who, realm, scope: '账号', kind: '积分限额',
                 reason: '剩余积分低于保留线', until: fmtCoolAt(nextMidnight)});
    }
    const mcap = Number(a.modelDailyTokenLimit || 0);
    if(mcap > 0 && a.modelDailyTokens){
      Object.entries(a.modelDailyTokens).forEach(([mid, v]) => {
        if(Number(v) >= mcap){
          rows.push({who, realm, scope: mid, kind: '日限额',
                     reason: '今日 token ' + Number(v).toLocaleString() + ' / 上限 ' + mcap.toLocaleString(),
                     until: fmtCoolAt(nextMidnight)});
        }
      });
    }
  });
  return rows;
}

function renderDisabled(){
  const box = document.getElementById('disabledList');
  if(!box) return;
  ensureHoverTip();
  const count = document.getElementById('disabledCount');
  const rows = disabledRows();
  if(count) count.textContent = rows.length ? '(' + rows.length + ')' : '';
  if(!rows.length){
    box.innerHTML = '<div class="legend" style="margin:0"><span>当前没有禁用的账号或模型。</span></div>';
    return;
  }
  const badge = k => k === '上游限流' ? 'warn' : (k === '本地停用' || k === '熔断' ? 'bad' : 's');
  box.innerHTML = '<div class="table-wrap"><table class="data-cards"><thead><tr>'
    + '<th style="text-align:left">账号</th>'
    + '<th style="text-align:center;width:76px">版本</th>'
    + '<th style="text-align:left">范围</th>'
    + '<th style="text-align:center;width:100px">类型</th>'
    + '<th style="text-align:left">原因</th>'
    + '<th style="text-align:center;width:156px">开始时间</th>'
    + '<th style="text-align:center;width:156px">恢复时间</th>'
    + '</tr></thead><tbody>'
    + rows.map(r => '<tr>'
        + '<td style="text-align:left">' + esc(r.who) + '</td>'
        + '<td style="text-align:center">' + esc(r.realm) + '</td>'
        + '<td style="text-align:left" class="mono">' + esc(r.scope) + '</td>'
        + '<td style="text-align:center"><span class="badge ' + badge(r.kind) + '">' + esc(r.kind) + '</span></td>'
        + '<td style="text-align:left"' + (r.detail ? ' data-tip="' + esc(r.detail) + '"' : '') + '>'
          + esc(r.reason) + '</td>'
        // 开始时间：这一行是何时进入该状态的。模型级限流两类都有真实时刻——
        // 6004 是上游给的撞线时刻（cappedAt），临时 429 是 hub 记下的限流时刻
        // （startedAt）；其余行类没有起始时刻，留「—」。
        + '<td style="text-align:center">' + esc(r.startedAt || '—') + '</td>'
        + '<td style="text-align:center">' + (r.manual ? '—（需手动启用）' : (r.until || '—')) + '</td>'
        + '</tr>').join('')
    + '</tbody></table></div>';
  applyTableSort('disabledList');
}

/* ---- 签到与活跃记录（GET /activity/history，issue #176）----
   国内版签到与国际版每日活跃的每一次真实尝试，都由 wb_activity.py 落成一行：
   时间 / uid / 昵称 / 区域 / 任务 / 触发来源 / 结果 / 说明。筛选全部交给服务端
   （range/task/result/uid/limit），它还会校验认不出来的值并回 400——前端不再
   自己过滤一遍，两处各写一份筛选规则迟早会跑偏。
   这份历史是按需读的日志视图，不跟着 15 秒轮询走：轮询会每轮把「加载更多」翻
   出来的页码重置回第一页，操作者正看着的记录会跳走。 */
const ACTIVITY_PAGE = 100;
const ACTIVITY_MAX_LIMIT = 1000;
let ACTIVITY_LIMIT = ACTIVITY_PAGE;
let ACTIVITY_LOADING = false;

const ACTIVITY_TASK_LABEL = {checkin: '国内版签到', daily_chat: '国际版每日活跃'};
const ACTIVITY_TRIGGER_LABEL = {scheduler: '定时', manual: '手动', account_add: '添加账号',
                                account_import: '导入', unknown: '未知'};
const ACTIVITY_REALM_LABEL = {cn: '国内版', intl: '国际版'};

function activityValue(id){
  const el = document.getElementById(id);
  return el ? String(el.value == null ? '' : el.value) : '';
}

function activityQuery(limit){
  const q = new URLSearchParams();
  q.set('range', activityValue('activityRange') || '7d');
  const task = activityValue('activityTask');
  if(task) q.set('task', task);
  const result = activityValue('activityResult');
  if(result) q.set('result', result);
  const uid = activityValue('activityAccount');
  if(uid) q.set('uid', uid);
  q.set('limit', String(limit));
  return '/activity/history?' + q.toString();
}

/* 账号下拉的选项取自当前账号池（/accounts）。账号轮询每 15 秒刷一次窗口，这里
   每次重建选项都要把操作者已选中的 uid 放回去，否则筛选条件会被悄悄清掉。 */
function renderActivityAccounts(){
  const sel = document.getElementById('activityAccount');
  if(!sel) return;
  const keep = String(sel.value || '');
  const opts = ['<option value="">全部账号</option>'];
  (window.ACCOUNTS || []).forEach(a => {
    if(!a || !a.uid) return;
    const name = a.nickname || String(a.uid).slice(0, 8);
    // 昵称是数据不是界面文案，别让 i18n 按汉字逐字转换它。
    opts.push('<option value="' + esc(a.uid) + '" data-no-i18n>' + esc(name) + '</option>');
  });
  sel.innerHTML = opts.join('');
  if(keep) sel.value = keep;
}

/* 时间按记录里写的墙上时间显示（服务端落盘时带本机偏移量），所以这里只做字符串
   截取，不做时区换算：换算一次就等于把运维看到的本地时间再挪一遍。 */
function activityTime(ts){
  const m = String(ts || '').match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  if(!m) return String(ts || '') || '—';
  return m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5];
}

function activityWho(r){
  const nick = String((r && r.nickname) || '').trim();
  if(nick) return nick;
  return (r && r.uid) ? String(r.uid).slice(0, 8) : '—';
}

function renderActivityBoard(data){
  const box = document.getElementById('activityList');
  if(!box) return;
  const rows = (data && data.rows) || [];
  const total = Number((data && data.total) || 0);
  const count = document.getElementById('activityCount');
  if(count) count.textContent = total ? '(共 ' + total + ' 条)' : '';
  const more = document.getElementById('activityMore');
  const info = document.getElementById('activityMoreInfo');
  if(more) more.style.display = (rows.length < total) ? 'flex' : 'none';
  if(info) info.textContent = (rows.length < total)
    ? ('已显示最新 ' + rows.length + ' 条 / 共 ' + total + ' 条') : '';
  if(!rows.length){
    box.innerHTML = '<div class="legend" style="margin:0"><span>这个区间里没有签到或活跃记录。</span></div>';
    return;
  }
  const resultBadge = ok => ok ? '<span class="badge ok">成功</span>'
                               : '<span class="badge bad">失败</span>';
  box.innerHTML = '<table class="data-cards"><thead><tr>'
    + '<th style="text-align:left;width:132px">时间</th>'
    + '<th style="text-align:left">账号</th>'
    + '<th style="text-align:center;width:76px">区域</th>'
    + '<th style="text-align:left;width:132px">任务</th>'
    + '<th style="text-align:center;width:76px">来源</th>'
    + '<th style="text-align:center;width:76px">结果</th>'
    + '<th style="text-align:left">说明</th>'
    + '</tr></thead><tbody>'
    + rows.map(r => '<tr>'
        + '<td class="mono" data-label="时间">' + esc(activityTime(r.ts)) + '</td>'
        + '<td data-label="账号" title="' + esc(r.uid || '') + '">'
          + '<span data-no-i18n>' + esc(activityWho(r)) + '</span></td>'
        + '<td style="text-align:center" data-label="区域">'
          + esc(ACTIVITY_REALM_LABEL[r.realm] || '—') + '</td>'
        + '<td data-label="任务">' + esc(ACTIVITY_TASK_LABEL[r.task] || r.task || '—') + '</td>'
        + '<td style="text-align:center" data-label="来源">'
          + esc(ACTIVITY_TRIGGER_LABEL[r.trigger] || r.trigger || '—') + '</td>'
        + '<td style="text-align:center" data-label="结果">' + resultBadge(r.ok) + '</td>'
        // 上游原文可能很长：在 span 上做行内截断（td 自身不吃 max-width，撑宽的是
        // 整张表），完整内容留在 title 里，表格不会被一条 429 原文拉出横向滚动。
        + '<td data-label="说明" style="text-align:left">'
        +   (r.message
              ? '<span style="display:inline-block;max-width:520px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:bottom" title="' + esc(r.message) + '">' + esc(r.message) + '</span>'
              : '<span style="color:var(--dim)">—</span>')
        + '</td>'
        + '</tr>').join('')
    + '</tbody></table>';
}

async function loadActivityBoard(showToast, more){
  if(ACTIVITY_LOADING) return;
  ACTIVITY_LOADING = true;
  const btn = document.getElementById('btnActivityRefresh');
  if(btn) btn.disabled = true;
  const box = document.getElementById('activityList');
  if(more) ACTIVITY_LIMIT = Math.min(ACTIVITY_MAX_LIMIT, ACTIVITY_LIMIT + ACTIVITY_PAGE);
  else ACTIVITY_LIMIT = ACTIVITY_PAGE;
  if(box && !more) box.innerHTML = '<div class="legend" style="margin:0"><span>加载中…</span></div>';
  try {
    renderActivityAccounts();
    const data = await getJSON(activityQuery(ACTIVITY_LIMIT));
    renderActivityBoard(data);
    if(showToast) toast('签到记录已刷新', 'ok');
  } catch(e) {
    if(box) box.innerHTML = '<div class="legend" style="margin:0"><span>读取签到记录失败: '
      + esc(e && e.message ? e.message : e) + '</span></div>';
    if(showToast) toast('读取签到记录失败: ' + (e && e.message ? e.message : e), 'bad');
  } finally {
    ACTIVITY_LOADING = false;
    if(btn) btn.disabled = false;
  }
}

function renderAccounts(){
  // Same data source as the account cards, so the two views can never disagree.
  renderDisabled();
  if(typeof updateTaskPageCheckinCards === 'function') updateTaskPageCheckinCards();
  const box = document.getElementById('accounts');
  const currentView = (window.VIEW_REALM || "intl");
  let list = accountsInView();
  // 排序：停用的永远沉底（备注/权重再高也一样）→ 备注是启用组内的加分项
  // （有备注的账号只在同权重时排前面，不再全局置顶）→ 权重高的在前 →
  // 同权重保持服务端顺序稳定，表格不乱跳。
  list = list.map((a, i) => [a, i])
    .sort((x, y) => {
      if(x[0].enabled !== y[0].enabled) return x[0].enabled ? -1 : 1;
      const nx = x[0].note ? 0 : 1, ny = y[0].note ? 0 : 1;
      if(nx !== ny) return nx - ny;
      const wx = Number(x[0].weight || 1) || 1, wy = Number(y[0].weight || 1) || 1;
      if(wx !== wy) return wy - wx;
      return x[1] - y[1];
    })
    .map(pair => pair[0]);
  const usable = list.filter(a => a.enabled).length;
  const viewName = currentView === 'cn' ? '国内版' : '国际版';
  document.getElementById('acctCount').textContent =
    list.length ? ('（' + viewName + '共 ' + list.length + ' 个，启用 ' + usable + ' 个）') : '';
  if(!list.length){
    box.innerHTML =
      '<div style="text-align:center;padding:30px 16px">'
      + '<div style="font-size:15px;margin-bottom:8px">当前' + viewName + '还没有账号</div>'
      + '<div style="color:var(--dim);font-size:13px;margin-bottom:18px">'
      +   '请先登录或导入一个 ' + viewName + ' 账号，之后相关请求都会走该账号'
      + '</div>'
      + '<button onclick="openLoginModal()" style="font-size:14px;padding:10px 22px">'
      +   '登录新账号 (OAuth)'
      + '</button>'
      + '</div>';
    return;
  }
  box.innerHTML = '<div class="table-wrap"><table class="data-cards account-cards"><thead><tr>'
    + '<th style="text-align:center;width:76px">版本</th>'
    + '<th style="text-align:left">账号</th>'
    + '<th style="text-align:center;width:84px">状态</th>'
    + '<th style="text-align:center;width:118px">今日活动</th>'
    + '<th style="text-align:center;width:150px">出口</th>'
    + '<th style="text-align:center;width:64px">权重</th>'
    + '<th style="text-align:left;width:120px">积分</th>'
    + '<th style="text-align:right;width:70px">请求</th>'
    + '<th style="text-align:right;width:88px">总 token</th>'
    + '<th style="text-align:right;width:80px">缓存</th>'
    + '<th style="text-align:center;width:232px">操作</th>'
    + '</tr></thead><tbody>' + list.map(accountRow).join('') + '</tbody></table></div>';
  // 表下不再堆两行说明图例（出站身份解释留在每行身份按钮组的悬停提示里，
  // 轮询/粘性说明挂到「账号列表」标题上），表格底部保持干净。
  applyTableSort('accounts');
}

async function loadAccounts(){
  try{
    const [acct, byAcct] = await Promise.all([
      getJSON('/accounts?realm=all'),
      getJSON('/usage/by-account', POLL_READ),
      // 今日活动与账号列表并行取；它自己吞掉失败，读不到历史不该让账号区跟着坏。
      loadActivityToday(),
    ]);
    window.ACCOUNTS = acct.accounts || [];
    USAGE_BY_ACCOUNT = {};
    (byAcct.accounts || []).forEach(a => { USAGE_BY_ACCOUNT[a.account] = a; });
    await loadProxySlots();
    renderAccounts();
    updateUI();
    // 昵称刚到位，补画一次积分扣减历史：它和本请求是并行的，先渲染的那次只有 uid。
    if(LAST_CREDIT_DATA) renderCreditHistory();
    // Auto-fetch credits once if not present
    if(window.ACCOUNTS.length && window.ACCOUNTS.some(a => !a.credits)){
      postJSON('/accounts/credits', {}).then(r => {
        window.ACCOUNTS = r.accounts || ACCOUNTS;
        renderAccounts();
      }).catch(()=>{});
    }
  }catch(e){ toast('账号加载失败: ' + e.message); }
}

async function fetchCredits(btn){
  if(btn){ btn.disabled = true; btn.textContent = '刷新中...'; }
  const currentView = (window.VIEW_REALM || "intl");
  const viewName = currentView === 'cn' ? '国内版' : '国际版';
  try{
    const r = await postJSON('/accounts/credits', {realm: currentView});
    await loadAccounts();
    refresh();
    const count = (r.results || []).length;
    toast('已成功刷新当前' + viewName + ' ' + count + ' 个账号的积分', 'ok');
  }catch(e){
    toast('刷新积分失败: ' + e.message, 'bad');
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '一键刷新积分'; }
  }
}
const queryCredits = fetchCredits;

async function toggleAccount(uid, enable, btn){
  if(btn) btn.disabled = true;
  try{
    await postJSON('/accounts/set', {uid: uid, enabled: enable});
    await loadAccounts();
    toast(enable ? '账号已成功启用' : '账号已停用', 'ok');
  }catch(e){
    toast('操作失败: ' + e.message, 'bad');
  }finally{
    if(btn) btn.disabled = false;
  }
}
async function toggleAccountTasks(uid, enable){
  try{
    await postJSON('/accounts/set', {uid: uid, tasksEnabled: enable});
    await loadAccounts();
    toast(enable ? '该账号已参与任务' : '该账号已停止任务', 'ok');
  }catch(e){
    toast('操作失败: ' + e.message, 'bad');
  }
}

async function setAccountWeight(uid, weight, sel){
  if(sel) sel.disabled = true;
  try{
    await postJSON('/accounts/set', {uid: uid, weight: Number(weight)});
    await loadAccounts();
    toast(Number(weight) > 1 ? '权重已调整：该账号将优先接单' : '权重已恢复默认', 'ok');
  }catch(e){
    toast('权重设置失败: ' + e.message, 'bad');
    renderAccounts();
  }finally{
    if(sel) sel.disabled = false;
  }
}
async function editAccountNote(uid){
  const cur = (window.ACCOUNTS || []).find(a => a.uid === uid);
  const next = prompt('账号备注（留空清除；可用来标记「批量」/「自用」）:', (cur && cur.note) || '');
  if(next === null) return;
  try{
    await postJSON('/accounts/set', {uid: uid, note: next});
    await loadAccounts();
    toast(next.trim() ? '备注已保存' : '备注已清除', 'ok');
  }catch(e){
    toast('备注保存失败: ' + e.message, 'bad');
  }
}

async function editAccountDisabledModels(uid){
  const cur = (window.ACCOUNTS || []).find(a => a.uid === uid);
  if(!cur) return;
  // 按**该账号自己的**区域取模型清单：看板当前视图切到别的区域时，
  // MODELS_DATA 里那份是另一个区域的，用它会给出对不上的选项。
  const realm = cur.realm || window.VIEW_REALM;
  let models = [];
  try{
    const r = await getJSON('/v1/models?realm=' + encodeURIComponent(realm));
    models = (r.data || []).map(m => m.id).filter(Boolean).sort();
  }catch(e){
    toast('取模型清单失败: ' + e.message, 'bad');
    return;
  }
  if(!models.length){ toast('该区域没有可用模型', 'bad'); return; }

  const denied = new Set(cur.disabledModels || []);
  // 面板按「启用的模型」勾选，默认全选 = 不限制；**取消勾选即禁用**。
  const picked = new Set(models.filter(m => !denied.has(m)));
  const nick = esc(cur.nickname || uid.slice(0, 8));

  const mask = document.createElement('div');
  mask.className = 'modal-mask';
  mask.style.display = 'flex';
  mask.innerHTML = '<div class="modal" role="dialog" aria-modal="true" style="width:min(560px,100%)">'
    + '<div class="modal-hd"><h2>启用模型 · ' + nick + '</h2>'
    +   '<button class="modal-x" aria-label="关闭">×</button></div>'
    + '<div class="modal-bd">'
    +   '<div style="font-size:12px;color:var(--dim);line-height:1.7;margin-bottom:10px">'
    +     '取消勾选的模型<b>不再派给这个账号</b>，请求会自动落到池子里其他账号；其余模型不受影响。'
    +     '<br>默认全部勾选 = 不限制。区域 <b>' + esc(realm) + '</b>，共 ' + models.length + ' 个模型。'
    +   '</div>'
    +   '<div id="mdmList" style="max-height:46vh;overflow:auto;border:1px solid var(--line);border-radius:8px;padding:4px 10px"></div>'
    +   '<div style="display:flex;align-items:center;gap:8px;margin-top:12px">'
    +     '<button class="sec" id="mdmAll">全选</button>'
    +     '<button class="sec" id="mdmNone">全不选</button>'
    +     '<span style="flex:1"></span><span id="mdmCount" style="font-size:12px"></span>'
    +   '</div>'
    + '</div>'
    + '<div class="modal-ft">'
    +   '<button class="sec" id="mdmCancel">取消</button>'
    +   '<button id="mdmSave">保存</button>'
    + '</div></div>';

  const listEm = mask.querySelector('#mdmList');
  const countEm = mask.querySelector('#mdmCount');
  listEm.innerHTML = models.map(m =>
    '<label style="display:flex;align-items:center;gap:8px;padding:5px 2px;cursor:pointer;font-size:13px">'
    + '<input type="checkbox" data-model="' + esc(m) + '"' + (picked.has(m) ? ' checked' : '') + '>'
    + '<span class="mono">' + esc(m) + '</span></label>').join('');
  function updateCount(){
    const off = models.length - picked.size;
    countEm.textContent = off ? ('将禁用 ' + off + ' 个') : '不限制';
    countEm.style.color = off ? 'var(--warn)' : 'var(--dim)';
  }
  updateCount();
  // 只更新计数、不重建复选框：重建会把滚动位置和焦点抖掉。
  listEm.addEventListener('change', function(ev){
    const box = ev.target;
    if(!box || box.type !== 'checkbox') return;
    if(box.checked) picked.add(box.dataset.model);
    else picked.delete(box.dataset.model);
    updateCount();
  });
  function setAll(on){
    picked.clear();
    if(on) models.forEach(m => picked.add(m));
    listEm.querySelectorAll('input[type=checkbox]').forEach(b => { b.checked = on; });
    updateCount();
  }
  mask.querySelector('#mdmAll').onclick = function(){ setAll(true); };
  mask.querySelector('#mdmNone').onclick = function(){ setAll(false); };
  function close(){ mask.remove(); }
  mask.querySelector('.modal-x').onclick = close;
  mask.querySelector('#mdmCancel').onclick = close;
  mask.addEventListener('click', function(ev){ if(ev.target === mask) close(); });
  mask.querySelector('#mdmSave').onclick = async function(){
    const denyList = models.filter(m => !picked.has(m));   // 未勾选 = 禁用
    try{
      await postJSON('/accounts/set', {uid: uid, disabledModels: denyList});
      await loadAccounts();
      toast(denyList.length ? ('已禁用 ' + denyList.length + ' 个模型') : '已取消模型禁用', 'ok');
      close();
    }catch(e){
      toast('保存失败: ' + e.message, 'bad');
    }
  };
  document.body.appendChild(mask);
}
async function setAccountProduct(uid, prod, btn){
  const names = {
    'workbuddy': 'WorkBuddy 独立桌面客户端 (WB)',
    'vscode': '官方 VSCode 插件 (VSC)',
    'cli': '官方 CodeBuddy CLI (CLI)'
  };
  const label = names[prod] || prod;
  if(!confirm('将该账号的出站身份切换为 ' + label + '？\n\n不同身分对应不同的出站指纹、端点和配额通道。')) return;
  if(btn){ btn.disabled = true; }
  try {
    const r = await postJSON('/accounts/product', {uid: uid, product: prod});
    toast('已切换为 ' + label, 'ok');
    await loadAccounts();
  } catch(e) {
    toast('切换失败: ' + e.message, 'bad');
  } finally {
    if(btn){ btn.disabled = false; }
  }
}
async function switchProduct(uid, btn){ return setAccountProduct(uid, 'workbuddy', btn); }
// 一键切换所有出站身份：后端 /accounts/product 不带 uid / realm 就是全量
// （含另一个版本），所以这里只负责挑下一个身分、并把当前身分显示在按钮上。
const PRODUCT_ORDER = ['workbuddy', 'vscode', 'cli'];
const PRODUCT_SHORT = {workbuddy: 'WB', vscode: 'VSC', cli: 'CLI'};
function allProductsUniform(){
  const list = window.ACCOUNTS || [];
  if(!list.length) return null;
  const first = list[0].product || 'workbuddy';
  return list.every(a => (a.product || 'workbuddy') === first) ? first : null;
}
function updateProductAllButton(){
  const label = document.getElementById('productAllLabel');
  if(!label) return;
  // 账号还没加载完时保持原样，别把按钮先写成「混合」再改回来。
  if(!(window.ACCOUNTS || []).length) return;
  const cur = allProductsUniform();
  label.textContent = cur ? PRODUCT_SHORT[cur] : '混合';
}
async function cycleAllProducts(btn){
  const list = window.ACCOUNTS || [];
  if(!list.length){ toast('还没有账号', 'bad'); return; }
  const cur = allProductsUniform();
  // cur 为 null（各账号身分不一致）时落到第一项，等于「统一成 WB」。
  const next = PRODUCT_ORDER[(PRODUCT_ORDER.indexOf(cur) + 1) % PRODUCT_ORDER.length];
  const label = PRODUCT_SHORT[next];
  if(!confirm('将把全部账号的出站身份切换为 ' + label + '？（含另一个版本）')) return;
  if(btn) btn.disabled = true;
  try{
    await postJSON('/accounts/product', {product: next});
    await loadAccounts();
    toast('已把全部账号的出站身份切换为 ' + label, 'ok');
  }catch(e){
    toast('切换失败: ' + e.message, 'bad');
  }finally{
    if(btn) btn.disabled = false;
  }
}

async function refreshOne(uid, btn){
  if(btn){ btn.disabled = true; btn.textContent = '刷新中...'; }
  try{
    const r = await postJSON('/accounts/refresh', {uid: uid});
    const ok = (r.results||[])[0];
    toast(ok && ok.ok ? '账号凭证刷新成功' : ('刷新失败: ' + (ok && ok.error || '未知错误')), ok && ok.ok ? 'ok' : 'bad');
    await loadAccounts();
  }catch(e){
    toast('刷新失败: ' + e.message, 'bad');
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '刷新凭证'; }
  }
}
async function syncProfile(uid, btn){
  if(btn){ btn.disabled = true; btn.textContent = '同步中...'; }
  try{
    const r = await postJSON('/accounts/sync-profile', {uid: uid});
    const item = (r.updated || [])[0];
    const bad = (r.failed || [])[0];
    if(item && item.nickname) toast('昵称已同步: ' + item.nickname, 'ok');
    else if(bad) toast('同步失败: ' + bad.error, 'bad');
    else toast('同步完成（上游未返回昵称）', 'warn');
    await loadAccounts();
  }catch(e){
    toast('同步失败: ' + e.message, 'bad');
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '同步昵称'; }
  }
}
async function testAccount(uid, btn){
  if(btn){ btn.disabled = true; btn.textContent = '测试中...'; }
  try{
    const r = await postJSON('/accounts/test', {uid: uid});
    if(r.ok){
      toast('测试通过 (' + r.elapsed_ms + 'ms) [' + r.model + ']: ' + (r.reply || '正常'), 'ok');
    } else if(r.network){
      // 本机断网时上游一个字节都没收到：说清是网络问题，别让人以为账号坏了。
      toast('本机网络不可用: ' + (r.error || 'DNS/路由失败'), 'bad');
    } else {
      toast('测试失败: ' + (r.error || r.status || '异常'), 'bad');
    }
    await loadAccounts();
  }catch(e){
    toast('测试请求失败: ' + e.message, 'bad');
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '测试'; }
  }
}

/* 一键测试：区域内每个已启用账号 + 本机连通性一次看全。
   池子出问题时最需要分辨的就是「账号真不行」与「本机没网 / 只是被本地窗口关着」，
   红点一屏的时候，逐个点测出来的结论往往是错的。 */
function openTestAll(){
  const m = document.getElementById('testAllModal');
  const body = document.getElementById('testAllBody');
  if(body) body.innerHTML = '<p class="hint">正在测试…</p>';
  if(m) m.classList.add('show');
}
function closeTestAll(){
  const m = document.getElementById('testAllModal');
  if(m) m.classList.remove('show');
}

async function testAccounts(btn){
  if(btn){ btn.disabled = true; btn.textContent = '测试中...'; }
  openTestAll();
  try{
    const r = await postJSON('/accounts/test', {realm: window.VIEW_REALM});
    renderTestAll(r);
    const s = r.summary || {};
    if(s.networkDown){
      toast('本机网络不通：' + ((r.network || {}).error || 'DNS/路由失败') + '（账号未被计入熔断）', 'bad');
    } else {
      toast('一键测试: ' + (s.passed || 0) + '/' + (s.tested || 0) + ' 通过 (' + (s.elapsed_ms || 0) + 'ms)',
            (s.failed ? 'warn' : 'ok'));
    }
    await loadAccounts();
  }catch(e){
    const body = document.getElementById('testAllBody');
    if(body) body.innerHTML = '<p class="hint" style="color:var(--warn)">测试请求失败: ' + esc(e.message) + '</p>';
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '一键测试'; }
  }
}

function renderTestAll(r){
  const body = document.getElementById('testAllBody');
  if(!body) return;
  const net = r.network || {};
  const s = r.summary || {};
  const rows = r.results || [];
  let html = '';
  // 本机连通性单独一行放最前面：它不通的话，下面的红点一个都不能算账号的账。
  html += '<div style="background:' + (net.ok ? 'var(--accent2-soft)' : 'var(--bad-soft)')
    + ';border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:12px;font-size:13px">'
    + '<b>' + (net.ok ? '本机网络正常' : '本机网络不通') + '</b>'
    + '<span class="hint" style="margin-left:8px">' + esc(net.host || '')
    + (net.ip ? ' → ' + esc(net.ip) : '')
    + (net.dns_ms !== null && net.dns_ms !== undefined ? ' · DNS ' + net.dns_ms + 'ms' : '')
    + (net.tcp_ms !== null && net.tcp_ms !== undefined ? ' · TCP ' + net.tcp_ms + 'ms' : '')
    + (net.ok ? '' : ' · ' + esc(net.error || ''))
    + '</span></div>';
  html += '<div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:10px;font-size:13px">'
    + '<span>共测 <b>' + esc(String(s.tested || 0)) + '</b></span>'
    + '<span>通过 <b style="color:var(--accent2)">' + esc(String(s.passed || 0)) + '</b></span>'
    + '<span>失败 <b style="color:var(--warn)">' + esc(String(s.failed || 0)) + '</b></span>'
    + '<span class="hint">模型 ' + esc(s.model || '') + ' · 耗时 ' + esc(String(s.elapsed_ms || 0)) + 'ms</span>'
    + '</div>';
  if(!rows.length){
    html += '<p class="hint">该区域没有已启用的账号。</p>';
  } else {
    html += '<div class="table-wrap"><table class="data-cards"><thead><tr>'
      + '<th style="text-align:left">账号</th><th style="text-align:center">结果</th>'
      + '<th style="text-align:center;width:90px">耗时</th>'
      + '<th style="text-align:left">回复 / 原因</th></tr></thead><tbody>';
    rows.forEach(row => {
      // 本机断网的行单独标出来：这不是账号的问题，不该跟真失败画成一个颜色。
      const badge = row.ok ? '<span class="badge ok">通过</span>'
                  : row.network ? '<span class="badge warn">本机网络</span>'
                  : '<span class="badge bad">失败</span>';
      const detail = row.ok ? (row.reply || '正常') : (row.error || '');
      // 失败行带上「连续失败 N 次」：这是熔断的唯一依据，看得见才知道离熔断还有多远。
      const ladder = (!row.ok && Number(row.fails || 0) > 0)
        ? '（连续失败 ' + Number(row.fails) + ' 次）' : '';
      html += '<tr>'
        + '<td style="text-align:left"><b>' + esc(row.nickname || (row.uid || '').slice(0, 8)) + '</b>'
        + '<div class="mono" style="font-size:11px;color:var(--dim)">'
        + esc((row.uid || '').slice(0, 8)) + ' · ' + esc(row.realm === 'cn' ? '国内版' : '国际版') + '</div></td>'
        + '<td style="text-align:center">' + badge + '</td>'
        + '<td style="text-align:center">' + esc(String(row.elapsed_ms || 0)) + 'ms</td>'
        + '<td style="text-align:left;font-size:12px">' + esc(detail + ladder) + '</td>'
        + '</tr>';
    });
    html += '</tbody></table></div>';
  }
  body.innerHTML = html;
}
async function refreshAccounts(btn){
  if(btn){ btn.disabled = true; btn.textContent = '刷新中...'; }
  try{
    const r = await postJSON('/accounts/refresh', {});
    const results = r.results || [];
    const okc = results.filter(x=>x.ok).length;
    const bad = results.filter(x=>!x.ok);
    toast('刷新完成: ' + okc + '/' + results.length, bad.length ? 'warn' : 'ok');
    if(bad.length){
      toast('刷新失败: ' + (bad[0].error || '未知错误') + ' (' + bad.length + ')', 'bad');
    }
    await loadAccounts();
  }catch(e){
    toast('刷新失败: ' + e.message, 'bad');
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '一键刷新凭证'; }
  }
}
async function setAll(enable, btn){
  if(btn) btn.disabled = true;
  try{
    await postJSON('/accounts/set-all', {enabled: enable});
    await loadAccounts();
    toast(enable ? '已全部启用所有账号' : '已全部停用所有账号', 'ok');
  }catch(e){
    toast('批量操作失败: ' + e.message, 'bad');
  }finally{
    if(btn) btn.disabled = false;
  }
}
async function deleteAccount(uid, name, btn){
  if(!confirm("确定删除账号 " + name + "？")) return;
  if(btn) btn.disabled = true;
  try{
    await postJSON('/accounts/delete', {uid: uid});
    await loadAccounts();
    toast('已成功删除账号 ' + name, 'ok');
  }catch(e){
    toast('删除失败: ' + e.message, 'bad');
  }finally{
    if(btn) btn.disabled = false;
  }
}
