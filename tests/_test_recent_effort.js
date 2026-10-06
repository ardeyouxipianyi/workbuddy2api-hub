/* The recent-requests table shows the reasoning effort each request ran at.

   record_usage() writes reasoning_effort onto the usage row, /usage/recent hands
   it to the panel, and the model cell renders it as a chip - the table keeps its
   14 columns, because a 15th would not fit and the effort belongs to the model
   that was picked. Rows without the field (older rows, models without reasoning
   controls) must render exactly as before. Run with Node.
*/
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard.html'), 'utf8');
const script = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)]
  .map(match => match[1]).join('\n');

// One persistent element per id, so the rendered table can be read back.
const elements = new Map();
const element = id => {
  if (!elements.has(id)) {
    elements.set(id, {
      id, innerHTML: '', textContent: '', value: '', className: '', style: {},
      classList: {add(){}, remove(){}, toggle(){}, contains(){ return false; }},
      addEventListener(){}, querySelector(){ return null; }, querySelectorAll(){ return []; },
      appendChild(){}, focus(){}, setAttribute(){}, getAttribute(){ return ''; },
    });
  }
  return elements.get(id);
};
global.document = {
  getElementById: element,
  querySelector: () => null, querySelectorAll: () => [],
  addEventListener(){}, createElement: () => element('created'),
  body: element('body'), head: element('head'), documentElement: element('html'),
};
global.window = {addEventListener(){}, location: {href: '', search: ''},
  matchMedia: () => ({matches: false, addEventListener(){}}), ACCOUNTS: []};
global.ACCOUNTS = global.window.ACCOUNTS;
global.localStorage = {getItem(){ return null; }, setItem(){}, removeItem(){}};
global.sessionStorage = global.localStorage;
global.navigator = {userAgent: 'node'};
global.setInterval = () => 0;
global.setTimeout = () => 0;
global.location = {href: '', search: '', hash: ''};
global.alert = () => {};
global.confirm = () => false;

const ROWS = [
  {iso: '2026-10-06T12:00:00', model: 'deepseek-v4.1-flash', stream: true,
   outcome: 'completed', elapsed_ms: 1200, ttft_ms: 800, tokens_per_sec: 100,
   prompt_tokens: 10, completion_tokens: 5, reasoning_tokens: 3, cache_hit_pct: 90,
   total_tokens: 15, credit: 0, account: 'uid-1', reasoning_effort: 'high'},
  {iso: '2026-10-06T12:01:00', model: 'hy3', stream: false, outcome: 'completed',
   elapsed_ms: 900, ttft_ms: null, prompt_tokens: 4, completion_tokens: 2,
   total_tokens: 6, credit: 0, account: 'uid-1'},
];
global.fetch = url => {
  const payload = String(url).includes('/usage/recent')
    ? {rows: ROWS, total: 2, page: 1, total_pages: 1, accounts_map: {}}
    : {rows: [], total: 0, accounts_map: {}, accounts: [], slots: [], data: [],
       results: [], byAccount: []};
  return Promise.resolve({
    status: 200, ok: true,
    json: () => Promise.resolve(payload),
    text: () => Promise.resolve(JSON.stringify(payload)),
  });
};

const api = new Function(script + `
  window.updateUI = updateUI;
  window.toast = toast;
  return { refresh };`)();

(async () => {
  window.VIEW_REALM = 'intl';
  await api.refresh();
  const rendered = element('recent').innerHTML;
  let checks = 0;
  const check = (label, cond, extra) => {
    checks += 1;
    assert.ok(cond, label + (extra ? '  [' + extra + ']' : ''));
  };

  check('the table keeps its 14 columns',
        (rendered.match(/<th>/g) || []).length === 14,
        String((rendered.match(/<th>/g) || []).length));
  check('the effort is rendered for the row that has it',
        rendered.includes('badge-effort') && rendered.includes('>high<'), rendered.slice(0, 300));
  check('the chip sits in the model cell, after the name',
        /data-label="模型">deepseek-v4\.1-flash <span class="badge-effort"/.test(rendered));
  check('the model name is still shown as before',
        rendered.includes('>deepseek-v4.1-flash<') === false &&
        rendered.includes('deepseek-v4.1-flash'));
  check('the row without an effort gets no chip',
        /data-label="模型">hy3<\/td>/.test(rendered));
  check('exactly one chip for two rows',
        (rendered.match(/badge-effort/g) || []).length === 1,
        String((rendered.match(/badge-effort/g) || []).length));

  console.log('recent-requests effort assertions passed (' + checks + ' checks)');
})();
