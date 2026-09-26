/* Drive the dashboard's matrix filter logic in Node.
 *
 * The filter state lives inside the page script's own scope, so the harness
 * reaches it through accessors defined in that same scope rather than by
 * poking globals. The dashboard itself is loaded from the file next to this
 * test, so the assertions run against the shipped code, not a copy of it.
 *
 * Requires node (no other dependency); the rest of the suite is Python only.
 *
 *   node _test_matrix_filters.js
 */
const path = require('path');
const fs = require('fs');
const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard.html'), 'utf8');
const blocks = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const code = blocks.join('\n');

const els = {};
const mk = (id) => (els[id] = els[id] || {
  id, innerHTML: '', textContent: '', value: '',
  classList: { add(){}, remove(){}, contains(){ return false; } },
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
        fillMatrixFilters,
        setFilters: (a, m) => { matrixAcctFilter = a; matrixModelFilter = m; },
        getFilters: () => [matrixAcctFilter, matrixModelFilter],
      };`)();
} catch (e) {
  console.log('LOAD ERROR:', e.message);
  process.exit(1);
}

const S = (n) => ({ avg: n, p50: n, samples: 1 });
const S3 = (n) => ({ avg: n, p50: n, samples: 3 });
const dsRow = (req, tot, p, c) => ({ requests: req, total_tokens: tot, prompt_tokens: p, completion_tokens: c, reasoning_tokens: 0, cached_tokens: 0 });
const usage = {
  requests: 4, total_tokens: 8300, prompt_tokens: 6600, completion_tokens: 1700, reasoning_tokens: 0,
  by_model: {
    'deepseek-v4.1-flash': dsRow(3, 5800, 4600, 1200),
    'glm-5.3':            dsRow(1, 2500, 2000, 500),
  },
  by_model_realm: {
    'deepseek-v4.1-flash': { intl: dsRow(3, 5800, 4600, 1200) },
    'glm-5.3':            { intl: dsRow(1, 2500, 2000, 500) },
  },
  by_model_acct: {
    'deepseek-v4.1-flash': { intl: { 'acct-A': dsRow(3, 5800, 4600, 1200) } },
    'glm-5.3':            { intl: { 'acct-B': dsRow(1, 2500, 2000, 500) } },
  },
  accounts_map: { 'acct-A': { nickname: 'Hades', realm: 'intl' }, 'acct-B': { nickname: 'lenguedogahz', realm: 'intl' } },
};
const perf = {
  errors: 0, ttft_ms: S(400), tokens_per_sec: S(100), wall_ms: S(1200), cache_hit_pct: { avg: 0, samples: 0 },
  by_model: {
    'deepseek-v4.1-flash': { errors: 0, ttft_ms: S3(400), tokens_per_sec: S3(100), wall_ms: S3(1200), cache_hit_pct: { avg: 0, samples: 0 } },
    'glm-5.3':            { errors: 0, ttft_ms: S(900),  tokens_per_sec: S(50),  wall_ms: S(3000),  cache_hit_pct: { avg: 0, samples: 0 } },
  },
  by_model_realm: {
    'deepseek-v4.1-flash': { intl: { errors: 0, ttft_ms: S3(400), tokens_per_sec: S3(100), wall_ms: S3(1200), cache_hit_pct: { avg: 0, samples: 0 } } },
    'glm-5.3':            { intl: { errors: 0, ttft_ms: S(900),  tokens_per_sec: S(50),  wall_ms: S(3000),  cache_hit_pct: { avg: 0, samples: 0 } } },
  },
  by_model_acct: {},
};

// The alternation must be grouped: writing 筛选结果合计|全部模型合计[...]
// makes the trailing part apply to the second branch only, so the capture is
// undefined whenever the label is "Filtered" and the check fails spuriously.
const summaryReq = (o) => { const m = o.match(/(?:筛选结果合计|全部模型合计)[\s\S]*?<td data-label="请求数">([^<]*)</); return m ? m[1] : null; };
const summaryTok = (o) => { const m = o.match(/<td data-label="总 Token"><b style="color:var\(--accent\)">([^<]*)</); return m ? m[1] : null; };
let pass = 0, fail = 0;
const check = (label, cond, extra) => { if (cond) { pass++; console.log('  [PASS] ' + label); } else { fail++; console.log('  [FAIL] ' + label + (extra ? '  ' + extra : '')); } };

console.log('[1] no filter: full totals');
api.setFilters('', '');
api.fillMatrixFilters(usage);
api.renderPerfMatrix(usage, perf);
let out = document.getElementById('perfMatrix').innerHTML;
check('summary label is Total, not Filtered', out.includes('全部模型合计'));
check('summary shows all 4 requests', summaryReq(out) === '4', summaryReq(out));
check('summary shows all 8,300 tokens', summaryTok(out) === '8,300', summaryTok(out));
check('both models present', out.includes('deepseek-v4.1-flash') && out.includes('glm-5.3'));
check('account dropdown lists both accounts',
      document.getElementById('matrixAcctFilter').innerHTML.includes('acct-A') &&
      document.getElementById('matrixAcctFilter').innerHTML.includes('acct-B'));
check('model dropdown lists both models',
      document.getElementById('matrixModelFilter').innerHTML.includes('glm-5.3'));

console.log();
console.log('[2] filter by model = glm-5.3');
api.setFilters('', 'glm-5.3');
api.renderPerfMatrix(usage, perf);
out = document.getElementById('perfMatrix').innerHTML;
check('only glm is rendered', out.includes('glm-5.3') && !out.includes('deepseek-v4.1-flash'));
check('summary switches to Filtered', out.includes('筛选结果合计'));
check('summary counts only the filtered row (1)', summaryReq(out) === '1', summaryReq(out));
check('summary tokens are the filtered row (2,500)', summaryTok(out) === '2,500', summaryTok(out));

console.log();
console.log('[3] filter by account = acct-A');
api.setFilters('acct-A', '');
api.renderPerfMatrix(usage, perf);
out = document.getElementById('perfMatrix').innerHTML;
check('glm row (acct-B) is dropped', !out.includes('glm-5.3'));
check('summary switches to Filtered', out.includes('筛选结果合计'));
check('summary counts only acct-A rows (3)', summaryReq(out) === '3', summaryReq(out));
check('summary tokens are acct-A only (5,800)', summaryTok(out) === '5,800', summaryTok(out));

console.log();
console.log('[4] filter combination that matches nothing');
api.setFilters('acct-B', 'deepseek-v4.1-flash');
api.renderPerfMatrix(usage, perf);
out = document.getElementById('perfMatrix').innerHTML;
check('empty state shown', out.includes('暂无模型请求'), out.slice(0, 120));

console.log();
console.log('[5] a stale filter is cleared when the range changes the data');
api.setFilters('acct-A', 'glm-5.3');
api.fillMatrixFilters(usage);
const [a, m] = api.getFilters();
check('acct-A kept (still present)', a === 'acct-A', a);
check('glm-5.3 kept (still present)', m === 'glm-5.3', m);
const todayUsage = JSON.parse(JSON.stringify(usage));
todayUsage.by_model = { 'deepseek-v4.1-flash': usage.by_model['deepseek-v4.1-flash'] };
todayUsage.by_model_acct = { 'deepseek-v4.1-flash': usage.by_model_acct['deepseek-v4.1-flash'] };
api.fillMatrixFilters(todayUsage);
const [a2, m2] = api.getFilters();
check('dropped model filter is cleared', m2 === '', m2);
check('still-valid account filter survives', a2 === 'acct-A', a2);

console.log();
console.log('PASS=' + pass + ' FAIL=' + fail);
process.exit(fail ? 1 : 0);
