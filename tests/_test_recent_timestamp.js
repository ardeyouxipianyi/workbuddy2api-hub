/* The live activity stream stamps each row with the time the request ran.

   It used to print HH:MM:SS and nothing else, so 22:11 yesterday and 22:11
   today were the same string in the same column: scrolling the list gave no
   way to tell which rows were from this visit. Rows from other days now carry
   their month and day; today's rows stay time-only, because those are the ones
   being watched.

   These checks build their fixtures from the clock at run time, so the suite
   does not go stale tomorrow. They read the rendered <span class="t"> cells
   rather than counting them or the columns around them: what this feature owes
   the reader is the stamp on each row, and other markup in the list is free to
   change around it.
   Run with Node.
*/
const assert = require('assert');
const {dashboardScript} = require('./_dashboard_source.js');

const script = dashboardScript().replace(
  'window.wbaLoad = wbaTick;',
  'window.wbaLoad = wbaTick;\n'
  + '  window.__recentProbe = {loadRecent: loadRecent, recentStamp: recentStamp};');

// The analytics block is wrapped in an IIFE, so nothing inside it is reachable
// from a suite. The lines above hang the two functions being tested on window
// at the one place the block already exports something, which is the only seam
// that does not depend on how the block is laid out.
if (!script.includes('__recentProbe')) {
  throw new Error('dashboard.html no longer offers a place to expose loadRecent');
}

// One shared fake DOM for every dashboard suite: tests/_dom_stub.js. Elements
// are persistent per id, so the rendered list can be read back.
const dom = require('./_dom_stub.js');
const {window: domWindow} = dom.installDom({
  fetch: url => {
    const payload = String(url).includes('/usage/recent')
      ? {rows: ROWS, total: ROWS.length, page: 1, total_pages: 1}
      : {rows: [], total: 0, accounts: [], accounts_map: {}, slots: [], data: []};
    return Promise.resolve({
      status: 200, ok: true,
      json: () => Promise.resolve(payload),
      text: () => Promise.resolve(JSON.stringify(payload)),
    });
  },
});
global.state = domWindow.state = {analytics: {accounts: []}};

const pad = v => String(v).padStart(2, '0');
const dayStamp = daysAgo => {
  const d = new Date(Date.now() - daysAgo * 86400000);
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
};

const ROWS = [
  {iso: dayStamp(0) + 'T01:46:17', model: 'deepseek-v4.1-flash',
   outcome: 'completed', total_tokens: 44, elapsed_ms: 1580, account: 'uid-1'},
  {iso: dayStamp(1) + 'T22:11:34', model: 'deepseek-v4.1-flash',
   outcome: 'completed', total_tokens: 95098, elapsed_ms: 2561, account: 'uid-1'},
  {iso: dayStamp(9) + 'T20:33:28', model: 'deepseek-v4.1-flash',
   outcome: 'failed', total_tokens: 0, elapsed_ms: 4066, account: 'uid-1'},
  {iso: '', model: 'deepseek-v4.1-flash', outcome: 'completed',
   total_tokens: 1, elapsed_ms: 100, account: 'uid-1'},
];

const api = new Function(script + `
  return window.__recentProbe;`)();

(async () => {
  await api.loadRecent();
  const rendered = dom.byId('recentList').innerHTML;
  let checks = 0;
  const check = (label, cond, extra) => {
    checks += 1;
    assert.ok(cond, label + (extra ? '  [' + extra + ']' : ''));
  };

  const stamps = [...rendered.matchAll(/<span class="t">([^<]*)<\/span>/g)]
        .map(m => m[1].trim());
  const rowOf = time => rendered.indexOf(time + '</span>') >= 0;

  check('every request gets a time cell', stamps.length === ROWS.length,
        stamps.length + ' cells for ' + ROWS.length + ' rows');

  check("today's row keeps the plain clock, no date in front of it",
        stamps[0] === '01:46:17', stamps[0]);
  check("today's stamp has no date separator",
        stamps[0] && !stamps[0].includes('-'), stamps[0]);

  check("yesterday's row carries its month and day",
        stamps[1] === dayStamp(1).slice(5) + ' 22:11', stamps[1]);
  check("a row from nine days ago carries its month and day too",
        stamps[2] === dayStamp(9).slice(5) + ' 20:33', stamps[2]);

  check('an old row and a current one never print the same stamp',
        new Set([stamps[0], stamps[1], stamps[2]]).size === 3,
        stamps.join(' | '));

  check('the failed row is still flagged as failed',
        rowOf(dayStamp(9).slice(5) + ' 20:33')
        && /<span class="err">\[failed\]<\/span>/.test(rendered), rendered);

  check('a row without a timestamp shows a dash instead of nothing',
        stamps[3] === '—', JSON.stringify(stamps[3]));

  // The helper is the contract too: suites that reuse it get the same rule.
  check('recentStamp stays time-only for today',
        api.recentStamp(dayStamp(0) + 'T12:34:56') === '12:34:56',
        api.recentStamp(dayStamp(0) + 'T12:34:56'));
  check('recentStamp stays time-only when given seconds past midnight',
        api.recentStamp(dayStamp(0) + 'T00:00:00') === '00:00:00',
        api.recentStamp(dayStamp(0) + 'T00:00:00'));
  check('recentStamp refuses to invent a date for a stamp it cannot read',
        api.recentStamp('') === '—' && api.recentStamp(undefined) === '—',
        api.recentStamp('') + ' / ' + api.recentStamp(undefined));

  console.log('recent-requests timestamp assertions passed (' + checks + ' checks)');
})();
