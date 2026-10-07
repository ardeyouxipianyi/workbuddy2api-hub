/* Issue #138: a pricing error must not leave saved keys and later settings blank.
 * Execute the shipped loadSettings() with a fake settings response and DOM.
 * Run: node tests/_test_settings_load.js
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard.html'), 'utf8');
const start = html.indexOf('async function loadSettings(showToast){');
const end = html.indexOf('\nasync function ', start + 1);
assert(start >= 0 && end > start, 'loadSettings must exist');
const source = html.slice(start, end);

async function check(pricingEnabled) {
  const elements = new Map();
  const data = {
    api_keys: [{id: 'saved-key', name: 'Test', masked: 'test******key', enabled: true}],
    deleted_api_keys: [], pricing_enabled: pricingEnabled,
    auto_switch_product: true, daily_chat_web: false, local_web_tools: true,
    version: 'test-version', accounts_dir: '/test/accounts', usage_dir: '/test/usage',
    settings_file: '/test/accounts/settings.json',
  };
  const notices = [];
  let renderCount = 0, priceLoads = 0, appliedPricing;
  const context = {
    window: {},
    document: {getElementById(id) {
      if (!elements.has(id)) elements.set(id, {style: {}, checked: false});
      return elements.get(id);
    }},
    getJSON: async url => { assert.strictEqual(url, '/settings'); return data; },
    applyLimits: () => {},
    applyPricingEnabled: value => { appliedPricing = value; },
    loadPricingStatus: () => { priceLoads++; },
    renderKeyRows: () => { renderCount++; },
    updateUI: () => {}, toast: (message, kind) => notices.push({message, kind}),
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  await context.loadSettings(true);
  assert.deepStrictEqual(notices.map(n => n.kind), ['ok'], 'settings load must complete');
  assert.strictEqual(renderCount, 1, 'saved keys must be rendered');
  assert.strictEqual(context.window.API_KEY_ROWS[0].id, 'saved-key');
  const expectedPricing = pricingEnabled !== false;
  assert.strictEqual(appliedPricing, expectedPricing);
  assert.strictEqual(elements.get('setPricingEnabled').checked, expectedPricing);
  assert.strictEqual(priceLoads, expectedPricing ? 1 : 0);
  assert.strictEqual(elements.get('setAutoSwitch').checked, true);
  assert.strictEqual(elements.get('setDailyChatWeb').checked, false);
  assert.strictEqual(elements.get('setLocalWebTools').checked, true);
  assert.strictEqual(elements.get('setVersion').textContent, data.version);
  assert.strictEqual(elements.get('setAccountsDir').textContent, data.accounts_dir);
  assert.strictEqual(elements.get('setSettingsFile').textContent, data.settings_file);
}

(async () => {
  for (const enabled of [true, false, undefined]) await check(enabled);
  console.log('PASS: settings load renders saved keys and later fields with pricing on, off, or unset');
})().catch(error => { console.error(error); process.exitCode = 1; });
