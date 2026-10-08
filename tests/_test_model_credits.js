/* The multiplier the panel shows must be the one the gateway fetched.

   renderAvailableModels() used to force "限时免费 0.00x" for three hardcoded ids
   (hy3, hy4-preview-f, intl deepseek-v4.1-flash) no matter what /v1/models
   reported for their credits, and to print a hardcoded 0.79x when a cn model
   carried none - so the panel could keep advertising a price the catalogue no
   longer had. The badge now follows the credits value alone: 0.00 is the free
   one, anything else is shown as fetched, and nothing is invented when the
   value is missing. Run with Node.
*/
const assert = require('assert');
const {dashboardScript} = require('./_dashboard_source.js');

const script = dashboardScript();

// One shared fake DOM for every dashboard suite: tests/_dom_stub.js.
const dom = require('./_dom_stub.js');

// renderAvailableModels() writes into #modelsTable tbody; capture it.
const tbody = dom.makeElement('tbody');
dom.installDom({
  querySelector: {'#modelsTable tbody': tbody},
  fetch: () => {
    const payload = {current: 'intl', accounts: [], slots: [], data: [],
                     results: [], byAccount: []};
    return Promise.resolve({
      status: 200, ok: true,
      json: () => Promise.resolve(payload),
      text: () => Promise.resolve(JSON.stringify(payload)),
    });
  },
});

const api = new Function(script + `
  window.updateUI = updateUI;
  window.toast = toast;
  return { renderAvailableModels };`)();

const creditsCell = model => {
  global.MODELS_DATA = [model];
  window.MODELS_DATA = [model];
  api.renderAvailableModels();
  const m = /data-label="消费倍率">([\s\S]*?)<\/td>/.exec(tbody.innerHTML);
  return m ? m[1] : '';
};

let checks = 0;
const check = (label, cond, extra) => {
  checks += 1;
  assert.ok(cond, label + (extra ? '  [' + extra + ']' : ''));
};

// 1. A free model is recognised by its value, whatever its id.
window.VIEW_REALM = 'intl';
let cell = creditsCell({id: 'space-bunny', credits: 'x0.00'});
check('x0.00 is the free badge', cell.includes('限时免费 0.00x'), cell);
cell = creditsCell({id: 'hy3', credits: 'x0.00'});
check('a pinned id that is still free keeps the free badge',
      cell.includes('限时免费 0.00x'), cell);

// 2. The regression: a pinned id that stopped being free must follow the value.
cell = creditsCell({id: 'hy3', credits: 'x0.50'});
check('hy3 at x0.50 shows 0.50x', cell.includes('0.50x'), cell);
check('hy3 at x0.50 is not advertised as free', !cell.includes('限时免费'), cell);
cell = creditsCell({id: 'hy4-preview-f', credits: 'x0.29'});
check('hy4-preview-f at x0.29 shows 0.29x', cell.includes('0.29x'), cell);
check('hy4-preview-f at x0.29 is not advertised as free',
      !cell.includes('限时免费'), cell);
cell = creditsCell({id: 'deepseek-v4.1-flash', credits: 'x0.11'});
check('intl deepseek at x0.11 shows 0.11x', cell.includes('0.11x'), cell);
check('intl deepseek at x0.11 is not advertised as free',
      !cell.includes('限时免费'), cell);

// 3. cn keeps its night note, but only next to a fetched value.
window.VIEW_REALM = 'cn';
cell = creditsCell({id: 'glm-5.2', credits: 'x0.79'});
check('cn glm-5.2 shows the fetched 0.79x', cell.includes('0.79x'), cell);
check('cn glm-5.2 keeps the night note', cell.includes('(夜间0.5x)'), cell);
cell = creditsCell({id: 'glm-5.2'});
check('cn glm-5.2 without credits shows no number', !cell.includes('0.79x'), cell);
check('cn glm-5.2 without credits falls back to the dash',
      cell.trim() === '-', cell);
cell = creditsCell({id: 'deepseek-v4-pro'});
check('cn deepseek-v4-pro without credits invents nothing',
      !cell.includes('0.79x'), cell);

// 4. No credits at all: a dash, never a guessed number.
window.VIEW_REALM = 'intl';
cell = creditsCell({id: 'gpt-5.5'});
check('a model without credits shows the dash', cell.trim() === '-', cell);
cell = creditsCell({id: 'gpt-5.5', credits: ''});
check('an empty credits string shows the dash', cell.trim() === '-', cell);

// 5. Odd but plausible shapes still follow the value.
cell = creditsCell({id: 'hy3', credits: '  x1.33 '});
check('surrounding spaces are tolerated', cell.includes('1.33x'), cell);
cell = creditsCell({id: 'hy3', credits: 'X0.00'});
check('an upper-case X still reads as free', cell.includes('限时免费'), cell);
cell = creditsCell({id: 'hy3', credits: 0});
check('a numeric 0 still reads as free', cell.includes('限时免费'), cell);
cell = creditsCell({id: 'hy3', credits: 'x0'});
check('a bare x0 still reads as free', cell.includes('限时免费'), cell);
// A non-string value must not take the whole table down: the old code called
// m.credits.startsWith() unguarded, so a numeric credits threw and the table
// came out empty.
cell = creditsCell({id: 'hy3', credits: 0.5});
check('a numeric credits renders instead of throwing', cell.includes('0.5x'), cell);
check('a numeric credits is not advertised as free', !cell.includes('限时免费'), cell);
// Every shape lands in the same multiplier format.
cell = creditsCell({id: 'hy3', credits: '0.50'});
check('a value without the x prefix is shown in the same format',
      cell.includes('0.50x'), cell);
cell = creditsCell({id: 'hy3', credits: 'X1.33'});
check('an upper-case prefix is normalized too', cell.includes('1.33x'), cell);

console.log('model credits assertions passed (' + checks + ' checks)');
