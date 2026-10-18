/* 熔断 / 降权必须在看板上看得见——它们也是「不接单」，只是跟上游配额无关。
 *
 * 生产上踩过：上游成片丢连接，账号治理把连续失败 3 次的账号熔断 30 分钟
 * （`note_unknown_failure` → `breaker_until`），几个账号被关掉之后池子就空了，
 * 请求 11ms 就回 503「no usable account」。而面板只画 cooldown / modelCooldowns，
 * 这两种新窗口一个都没有：账号卡片仍是绿色「可用」，「当前禁用总览」也不列它们。
 * 于是「明明有 9 个账号」和「报没有可用账号」在同一屏上互相矛盾。
 *
 * 这里钉住三件事：账号行徽章不再把这类账号画成可用、剩余时间换算正确、禁用总览
 * 把熔断/降权各列一行；顺带回归原有的停用/保留积分/日限额/积分限额/模型冷却。
 *
 * Run with Node: node tests/_test_account_penalty_badges.js
 */
'use strict';
const assert = require('assert');
const {dashboardScript} = require('./_dashboard_source.js');
const dom = require('./_dom_stub.js');

const script = dashboardScript();
dom.installDom({
  fetch: () => Promise.resolve({
    status: 200, ok: true,
    json: () => Promise.resolve({}),
    text: () => Promise.resolve('{}'),
  }),
});

const api = new Function(script + `
  return {
    accountRow: accountRow,
    disabledRows: disabledRows,
    setAccounts: list => { window.ACCOUNTS = list; },
    setView: view => { window.VIEW_REALM = view; },
  };`)();

const NOW = Math.floor(Date.now() / 1000);
const row = extra => api.accountRow(Object.assign(
  {uid: 'acct-0001', nickname: '某账号', realm: 'intl', enabled: true}, extra));

function check(label, ok, detail) {
  assert.ok(ok, label + (detail === undefined ? '' : '  -> ' + detail));
}

/* ---- 1. 账号行：熔断 / 降权不再是绿色「可用」 ------------------------- */

const breaker = row({breakerFor: 1740});
check('熔断账号要有熔断徽章', breaker.includes('熔断'), breaker);
check('熔断账号不能显示可用', !breaker.includes('可用'), breaker);
check('剩余时间要折算（1740s → 29m）', breaker.includes('熔断 29m'), breaker);
check('熔断优先于冷却（两者并存时报熔断）',
  row({breakerFor: 1800, inCooldown: true, cooldownFor: 120}).includes('熔断'), breaker);

const degrade = row({degradeFor: 600});
check('降权账号要有降权徽章', degrade.includes('降权 10m'), degrade);
check('降权账号不能显示可用', !degrade.includes('可用'), degrade);

const cooled = row({inCooldown: true, cooldownFor: 1200, softStreak: 3});
check('冷却徽章保留原有文案', cooled.includes('冷却 1200s'), cooled);
check('连续软限流次数进 tooltip', cooled.includes('连续软限流 3 次'), cooled);
check('只在连踩时才提次数', !row({inCooldown: true, cooldownFor: 60, softStreak: 1})
  .includes('连续软限流'), 'streak=1 不该出现该措辞');

check('小时级剩余时间格式（2h05m 风格）', /降权 1h\d{2}m/.test(row({degradeFor: 6300})),
  row({degradeFor: 6300}));

/* ---- 2. 回归：原有几档不受影响 --------------------------------------- */

check('正常账号仍显示可用', row({}).includes('可用'), row({}));
check('停用优先', row({enabled: false, breakerFor: 600}).includes('已停用'));
check('保留积分优先', row({reserveBlocked: true}).includes('保留积分'));
check('日限额优先', row({dailyLimitBlocked: true}).includes('日限额'));
check('积分限额优先', row({creditLimitReached: true}).includes('积分限额'));
check('模型级冷却照旧出标签',
  row({modelCooldowns: [{model: 'gpt-6-sol', expiresAt: NOW + 900}]}).includes('gpt-6-sol'));

/* ---- 3. 「当前禁用总览」也要列出来 ------------------------------------ */

api.setView('intl');
api.setAccounts([
  {uid: 'intl-brk-0001', nickname: '熔断号', realm: 'intl', enabled: true, breakerFor: 1800},
  {uid: 'intl-deg-0002', nickname: '降权号', realm: 'intl', enabled: true, degradeFor: 600},
  {uid: 'intl-ok-0003', nickname: '正常号', realm: 'intl', enabled: true},
]);
const rows = api.disabledRows();
const kinds = rows.map(r => r.kind);
check('总览要包含熔断', kinds.includes('熔断'), kinds.join(','));
check('总览要包含降权', kinds.includes('降权'), kinds.join(','));
const brk = rows.find(r => r.kind === '熔断');
check('熔断行要写清与账号本身无关', /账号与凭证本身没问题/.test(brk.reason), brk.reason);
check('熔断行给出恢复时间', !!brk.until, brk.until);
check('正常账号不出现', !rows.some(r => r.who.includes('正常号')), JSON.stringify(rows));
check('熔断与降权都只描述当前视图的账号',
  rows.every(r => r.realm === '国际版'), JSON.stringify(rows.map(r => r.realm)));

/* ---- 4. 429 短标签只在限流还开着时显示 -------------------------------- */
// 6004 的恢复时刻一过账号就能接单，标签却还挂着（只能重启清掉）。模型窗口还在
// 时由 pill 表达（不重复画一遍文案）；窗口/冷却都结束以后不再画。issue #454。
const stale429 = row({
  lastError: 'HTTP 429 (model throttled)',
  lastErrorDetail: '{"code":6004,"msg":"usage exceeds frequency limit","requestId":"req-1"}',
});
check('窗口已过的 429 不应再画标签', !stale429.includes('acct-err'), stale429);
check('也不应出现 HTTP 429 文本', !stale429.includes('HTTP 429'), stale429);

const live429 = row({
  lastError: 'HTTP 429 (model throttled)',
  lastErrorDetail: '{"code":6004,"msg":"usage exceeds frequency limit","requestId":"req-1"}',
  modelCooldowns: [{model: 'deepseek-v4.1-flash', expiresAt: NOW + 600}],
});
check('模型窗口还开着时由 pill 表达、不再重复画 429 文案',
  !live429.includes('HTTP 429'), live429);
check('模型窗口本身照旧画出来', live429.includes('deepseek-v4.1-flash'), live429);

const accountLevel429 = row({
  lastError: 'HTTP 429 (model throttled)',
  lastErrorDetail: '{"code":6004,"msg":"usage exceeds frequency limit","requestId":"req-1"}',
  inCooldown: true, cooldownFor: 120,
});
check('账号级冷却还开着时 429 标签要在', accountLevel429.includes('HTTP 429 · code:6004'),
  accountLevel429);
check('标签要带可悬停的完整响应', accountLevel429.includes('data-tip='), accountLevel429);

const authErr = row({lastError: '登录已过期 (HTTP 401)，请重新登录'});
check('与冷却无关的错误照旧常显', authErr.includes('HTTP 401'), authErr);

const plainErr = row({lastError: '登录已过期，请重新登录'});
check('没有 HTTP 状态码的错误也照旧常显', plainErr.includes('登录已过期'), plainErr);

console.log('account penalty badge assertions passed');
