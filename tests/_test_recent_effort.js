/* The recent-requests table shows the reasoning effort each request ran at.

   record_usage() writes reasoning_effort onto the usage row, /usage/recent hands
   it to the panel, and the table renders it as a chip in its own 推理强度 column
   - the table now carries 15 columns. Rows without the field (older rows, models
   without reasoning controls) show a dash there and otherwise render as before.
   Run with Node.
*/
const assert = require('assert');
const {dashboardScript} = require('./_dashboard_source.js');

const script = dashboardScript();

// One shared fake DOM for every dashboard suite: tests/_dom_stub.js. Elements
// are persistent per id, so the rendered table can be read back.
const dom = require('./_dom_stub.js');
const {window: domWindow} = dom.installDom({
  fetch: url => {
    const payload = String(url).includes('/usage/recent')
      ? {rows: ROWS, total: 2, page: 1, total_pages: 1, accounts_map: {}}
      : {rows: [], total: 0, accounts_map: {}, accounts: [], slots: [], data: [],
         results: [], byAccount: []};
    return Promise.resolve({
      status: 200, ok: true,
      json: () => Promise.resolve(payload),
      text: () => Promise.resolve(JSON.stringify(payload)),
    });
  },
});
domWindow.ACCOUNTS = [];
global.ACCOUNTS = domWindow.ACCOUNTS;

const ROWS = [
  {iso: '2026-10-06T12:00:00', model: 'deepseek-v4.1-flash', stream: true,
   outcome: 'completed', elapsed_ms: 1200, ttft_ms: 800, tokens_per_sec: 100,
   prompt_tokens: 10, completion_tokens: 5, reasoning_tokens: 3, cache_hit_pct: 90,
   total_tokens: 15, credit: 0, account: 'uid-1', reasoning_effort: 'high'},
  {iso: '2026-10-06T12:01:00', model: 'hy3', stream: false, outcome: 'completed',
   elapsed_ms: 900, ttft_ms: null, prompt_tokens: 4, completion_tokens: 2,
   total_tokens: 6, credit: 0, account: 'uid-1'},
];

const api = new Function(script + `
  window.updateUI = updateUI;
  window.toast = toast;
  return { refresh };`)();

(async () => {
  window.VIEW_REALM = 'intl';
  await api.refresh();
  const rendered = dom.byId('recent').innerHTML;
  let checks = 0;
  const check = (label, cond, extra) => {
    checks += 1;
    assert.ok(cond, label + (extra ? '  [' + extra + ']' : ''));
  };

  check('the table keeps its 15 columns',
        (rendered.match(/<th>/g) || []).length === 15,
        String((rendered.match(/<th>/g) || []).length));
  check('the effort is rendered for the row that has it',
        rendered.includes('badge-effort') && rendered.includes('>high<'), rendered.slice(0, 300));
check('the effort has its own column, right after the model',
        /data-label="模型">deepseek-v4\.1-flash<\/td><td data-label="推理强度"><span class="badge-effort">high<\/span><\/td>/.test(rendered),
        rendered.slice(0, 300));
  check('the model cell holds only the model name',
        rendered.includes('data-label="模型">deepseek-v4.1-flash</td>'));
  check('the row without an effort shows a dash, not a chip',
        /data-label="模型">hy3<\/td><td data-label="推理强度"><span style="color:var\(--dim\)">—<\/span><\/td>/.test(rendered),
        rendered.slice(0, 300));
  check('exactly one chip for two rows',
        (rendered.match(/badge-effort/g) || []).length === 1,
        String((rendered.match(/badge-effort/g) || []).length));

  console.log('recent-requests effort assertions passed (' + checks + ' checks)');
})();
