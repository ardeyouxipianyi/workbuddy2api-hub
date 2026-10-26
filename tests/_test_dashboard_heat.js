/* 热力图必须跟着本地日走，而不是只在进页面时取一次。
 *
 * 回归背景（issue #541）：loadStatic 只在页面启动时取一次 366 天序列，
 * 跨过午夜后格子还停在旧的一天；刷新页面后拿到的又是「到当前秒为止」的
 * 窗口，今天后续的用量不在里面。这里把 dashboard.html 里的取数逻辑抽出来，
 * 钉住三条：窗口按本地午夜对齐、60 秒内不重复取、跨天立即重取。
 *
 * Run with Node: node tests/_test_dashboard_heat.js
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const html = require('./_dashboard_source.js').dashboardHtml();
const start = html.indexOf('let heatFetchedAt');
const end = html.indexOf('/* ---------------- KPI ---------------- */');
assert.ok(start > 0 && end > start, 'dashboard.html 里找不到热力图取数逻辑，测试需要更新');
const source = html.slice(start, end);
const RealDate = Date;

function makeHarness(nowMs) {
  const calls = [];
  const state = { heat: null };
  let now = nowMs;
  class FakeDate extends RealDate {
    constructor(value) { super(value === undefined ? now : value); }
    static now() { return now; }
  }
  const api = async (url) => { calls.push(url); return { series: [] }; };
  const buildHeatGrid = () => {};
  const fmtDate = (epoch) => {
    const d = new FakeDate(epoch * 1000);
    return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
  };
  const factory = new Function('api', 'state', 'buildHeatGrid', 'fmtDate', 'Date',
    source + '\nreturn { loadHeat, maybeLoadHeat };');
  const apiObj = factory(api, state, buildHeatGrid, fmtDate, FakeDate);
  return {
    calls, state,
    setNow(value) { now = value; },
    loadHeat: apiObj.loadHeat,
    maybeLoadHeat: apiObj.maybeLoadHeat,
  };
}

(async () => {
  const noon = new RealDate(2026, 9, 21, 12, 0, 0).getTime();
  const h = makeHarness(noon);
  await h.loadHeat();
  assert.strictEqual(h.calls.length, 1, '第一次应取一次热力图');
  const url = new URL(h.calls[0], 'http://x');
  const since = Number(url.searchParams.get('since'));
  const until = Number(url.searchParams.get('until'));
  const midnight = new RealDate(2026, 9, 21, 0, 0, 0).getTime() / 1000;
  assert.strictEqual(since, midnight - 366 * 86400,
    'since 必须对齐本地午夜，而不是 now-366d');
  assert.strictEqual(until, midnight + 86400,
    'until 必须取到明天 0 点，今天的用量才落得进今天的格子');
  assert.strictEqual(url.searchParams.get('bucket_seconds'), '86400');

  await h.maybeLoadHeat();
  assert.strictEqual(h.calls.length, 1, '60 秒内不应重复取');

  h.setNow(noon + 61 * 1000);
  await h.maybeLoadHeat();
  assert.strictEqual(h.calls.length, 2, '超过 60 秒应重取');

  const nextDay = new RealDate(2026, 9, 22, 0, 0, 5).getTime();
  h.setNow(nextDay);
  await h.maybeLoadHeat();
  assert.strictEqual(h.calls.length, 3, '跨天应立即重取');
  const url2 = new URL(h.calls[2], 'http://x');
  assert.strictEqual(Number(url2.searchParams.get('until')),
    new RealDate(2026, 9, 22, 0, 0, 0).getTime() / 1000 + 86400,
    '跨天后 until 应滚到新的明天 0 点');

  console.log('dashboard heat assertions passed (' + h.calls.length + ' fetches)');
})().catch((err) => { console.error(err); process.exit(1); });

