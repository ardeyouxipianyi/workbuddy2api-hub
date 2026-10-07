/* Pin what the analytics matrix's 用量占比 column is a share *of*.
 *
 * The column used to divide every row by the largest row, so the biggest
 * model always read 100% and everything else was a fraction of it - a
 * relative ranking wearing the label of a share. The contract asserted here
 * is the plain one: each row's own tokens over the total in the summary row,
 * which is also the number the table itself prints as the total.
 *
 * The dashboard is loaded from the file next to this test, so the assertions
 * run against the shipped code, not a copy of it.
 *
 * Requires node (no other dependency); the rest of the suite is Python only.
 *
 *   node _test_usage_share.js
 */
const path = require('path');
const fs = require('fs');
const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard.html'), 'utf8');
const blocks = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const code = blocks.join('\n');

const els = {};
const mk = (id) => (els[id] = els[id] || {
  id, innerHTML: '', textContent: '', value: '',
  classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
  style: {}, children: [], focus(){}, blur(){}, click(){},
  appendChild(c){ this.children.push(c); },
  querySelectorAll(){ return []; }, querySelector(){ return null; },
  addEventListener(){}, setAttribute(){}, getAttribute(){ return ''; },
  insertAdjacentHTML(){}, removeChild(){}, remove(){},
});
global.document = {
  getElementById: (id) => (id ? mk(id) : null),
  querySelectorAll: () => [], querySelector: () => null,
  addEventListener: () => {}, createElement: (t) => mk(t + Math.random()),
  body: mk('body'), head: mk('head'), documentElement: mk('html'),
};
global.window = { addEventListener(){}, location:{ href:'' }, matchMedia: () => ({ matches:false, addEventListener(){} }) };
global.localStorage = { getItem(){return null;}, setItem(){}, removeItem(){} };
global.sessionStorage = global.localStorage;
global.fetch = () => Promise.resolve({ ok:true, status:200, json: () => Promise.resolve({}) });
global.navigator = { userAgent: 'node' };
global.setInterval = () => 0; global.clearInterval = () => {};
global.setTimeout = () => 0; global.clearTimeout = () => {};
global.location = { href: '', search: '', hash: '' };
global.alert = () => {}; global.confirm = () => false;

var api;
try {
  api = new Function(code + `
    ; return {
        renderPerfMatrix,
        setFilters: (a, m) => { matrixAcctFilter = a; matrixModelFilter = m; },
      };`)();
} catch (e) {
  console.log('LOAD ERROR:', e.message);
  process.exit(1);
}

const S = (n) => ({ avg: n, p50: n, samples: 1 });
const dsRow = (tot, p, c) => ({ requests: 1, total_tokens: tot, prompt_tokens: p, completion_tokens: c, reasoning_tokens: 0, cached_tokens: 0 });
const perfRow = { errors: 0, ttft_ms: S(400), tokens_per_sec: S(100), wall_ms: S(1200), cache_hit_pct: { avg: 0, samples: 0 } };

/* Three chat models whose shares of the total are 50 / 30 / 20 - deliberately
 * not 100 / 60 / 40, which is what dividing by the largest row produced. */
const ROWS = { 'model-big': 5000, 'model-mid': 3000, 'model-small': 2000 };

function buildUsage(extra, total){
  const rows = Object.assign({}, ROWS, extra || {});
  const by_model = {};
  const by_model_realm = {};
  const by_model_acct = {};
  Object.keys(rows).forEach(id => {
    const r = dsRow(rows[id], rows[id] - 1000, 1000);
    by_model[id] = r;
    by_model_realm[id] = { intl: r };
    by_model_acct[id] = { intl: { 'acct-A': r } };
  });
  return {
    requests: Object.keys(rows).length,
    total_tokens: total || Object.keys(rows).reduce((s, id) => s + rows[id], 0),
    prompt_tokens: 8000, completion_tokens: 2000, reasoning_tokens: 0,
    by_model, by_model_realm, by_model_acct,
    accounts_map: { 'acct-A': { nickname: 'Hades', realm: 'intl' } },
  };
}

const perf = {
  errors: 0, ttft_ms: S(400), tokens_per_sec: S(100), wall_ms: S(1200), cache_hit_pct: { avg: 0, samples: 0 },
  by_model: Object.fromEntries(Object.keys(ROWS).map(id => [id, perfRow])),
  by_model_realm: Object.fromEntries(Object.keys(ROWS).map(id => [id, { intl: perfRow }])),
  by_model_acct: {},
};

/* One entry per data row, in render order. The summary row has no bar, so
 * matching on the bar element is what separates the two. */
const shareCells = (out) => [...out.matchAll(
  /data-label="用量占比"><span class="bar-fill" style="width:([\d.]+)%"><\/span> <span[^>]*>([\d.]+)%<\/span>/g
)].map(m => ({ bar: Number(m[1]), pct: Number(m[2]) }));
const summaryTok = (out) => { const m = out.match(/(?:筛选结果合计|全部模型合计)[\s\S]*?<td data-label="总 Token">[\s\S]*?>([^<]*)</); return m ? m[1] : null; };
const summaryShare = (out) => { const m = out.match(/data-label="用量占比">([^<]*)</); return m ? m[1] : null; };

let pass = 0, fail = 0;
const check = (label, cond, extra) => { if (cond) { pass++; console.log('  [PASS] ' + label); } else { fail++; console.log('  [FAIL] ' + label + (extra !== undefined ? '  ' + extra : '')); } };

console.log('[1] no filter: each row is its own tokens over the total');
api.setFilters('', '');
api.renderPerfMatrix(buildUsage(), perf);
let out = document.getElementById('perfMatrix').innerHTML;
let cells = shareCells(out);
check('one cell per model', cells.length === 3, cells.length);
check('shares are 50/30/20 of the total', JSON.stringify(cells.map(c => c.pct)) === '[50,30,20]',
      JSON.stringify(cells.map(c => c.pct)));
check('the largest row is not forced to 100%', cells[0].pct !== 100, cells[0].pct);
check('shares add up to the whole', Math.round(cells.reduce((s, c) => s + c.pct, 0) * 10) / 10 === 100,
      cells.reduce((s, c) => s + c.pct, 0));
check('bar width follows the printed share', cells.every(c => c.bar === c.pct), JSON.stringify(cells));
check('summary still prints the total', summaryTok(out) === '10,000', summaryTok(out));
check('summary row is the 100% end of the scale', summaryShare(out) === '100%', summaryShare(out));

console.log();
console.log('[2] a filter re-bases the share on the filtered total');
api.setFilters('', 'model-mid');
api.renderPerfMatrix(buildUsage(), perf);
out = document.getElementById('perfMatrix').innerHTML;
cells = shareCells(out);
check('only the filtered row remains', cells.length === 1, cells.length);
check('it is 100% of what is now shown', cells[0].pct === 100, cells[0].pct);
check('summary is the filtered total', summaryTok(out) === '3,000', summaryTok(out));

console.log();
console.log('[3] tokens the table does not list stay in the denominator');
/* Virtual alias buckets (default-model and friends) are counted in the
 * endpoint total but excluded from the matrix, so the listed rows must not be
 * renormalised to 100% - that would report a share of a number the table
 * never shows. */
api.setFilters('', '');
api.renderPerfMatrix(buildUsage({ 'default-model': 2000 }, 12000), perf);
out = document.getElementById('perfMatrix').innerHTML;
cells = shareCells(out);
check('listed rows are shares of 12,000, not of their own 10,000',
      JSON.stringify(cells.map(c => c.pct)) === '[41.7,25,16.7]', JSON.stringify(cells.map(c => c.pct)));
check('summary prints the full total', summaryTok(out) === '12,000', summaryTok(out));

console.log();
console.log('[4] a lopsided total still resolves the small rows');
/* The real gateway spends almost everything on one model. A whole-percent
 * reading turns the runner-up's millions of tokens into a bare "0%", which
 * reads as "no usage" rather than "a sliver". */
api.setFilters('', '');
api.renderPerfMatrix(buildUsage({ 'model-big': 5700000000, 'model-mid': 8800000, 'model-small': 5000000 }), perf);
out = document.getElementById('perfMatrix').innerHTML;
cells = shareCells(out);
check('the dominant model reads 99.8%, not 100%', cells[0].pct === 99.8, cells[0].pct);
check('the runner-up keeps a visible share', cells[1].pct > 0 && cells[1].pct < 1, cells[1].pct);
check('the smallest row is not a bare zero', cells[2].pct > 0, cells[2].pct);

console.log();
console.log('PASS=' + pass + ' FAIL=' + fail);
process.exit(fail ? 1 : 0);
