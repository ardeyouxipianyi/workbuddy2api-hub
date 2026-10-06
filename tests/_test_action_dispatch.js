/* Drive the dashboard's delegated action dispatcher in Node.
 *
 * M4 D3 stage 2 replaced every inline handler with data-action / data-on
 * attributes. This harness records the dispatcher's listeners, fires
 * synthetic events at fake elements and asserts the real page functions run
 * with the right arguments (fetch payloads, state mutations). The page code
 * is loaded from the shipped dashboard.html, not a copy.
 *
 * Run with Node: node tests/_test_action_dispatch.js
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard.html'), 'utf8');
const code = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');

const listeners = {};
const els = {};
function mk(id){
  if (els[id]) return els[id];
  const el = {
    id, innerHTML: '', textContent: '', value: '', checked: false, disabled: false,
    scrollHeight: 100, scrollTop: 0, clientHeight: 100,
    _classes: new Set(), style: {}, children: [],
    focus(){}, blur(){}, click(){},
    appendChild(c){ this.children.push(c); },
    querySelectorAll(){ return []; }, querySelector(){ return null; },
    addEventListener(){}, setAttribute(){}, getAttribute(){ return ''; },
    insertAdjacentHTML(){}, removeChild(){}, remove(){},
  };
  el.classList = {
    add: (c) => el._classes.add(c),
    remove: (c) => el._classes.delete(c),
    contains: (c) => el._classes.has(c),
    toggle: (c, on) => {
      const want = on === undefined ? !el._classes.has(c) : !!on;
      if (want) el._classes.add(c); else el._classes.delete(c);
      return want;
    },
  };
  els[id] = el;
  return el;
}

global.document = {
  getElementById: (id) => (id ? mk(id) : null),
  querySelectorAll: () => [],
  querySelector: () => null,
  addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
  createElement: (t) => mk(t + Math.random()),
  body: mk('body'), head: mk('head'), documentElement: mk('html'),
};
global.window = {
  addEventListener(){}, location:{ href:'' }, matchMedia: () => ({ matches:false, addEventListener(){} }),
  VIEW_REALM: 'intl', ACTIVE_GATEWAY_REALM: 'intl', REALM_FILTER: 'all', __MAIN_TAB__: 'gateway',
  ACCOUNTS: [], SCAN_POOL: [], API_KEY_ROWS: [], MODELS_DATA: [],
  crypto: { randomUUID: () => '00000000-0000-0000-0000-000000000000' },
};
global.localStorage = { getItem(){return null;}, setItem(){}, removeItem(){} };
global.sessionStorage = global.localStorage;
global.navigator = { userAgent: 'node' };
global.setInterval = () => 0; global.clearInterval = () => {};
global.setTimeout = () => 0; global.clearTimeout = () => {};
global.location = { href: '', search: '', hash: '' };
global.alert = () => {}; global.confirm = () => true;

const requests = [];
global.fetch = (url, options) => {
  requests.push({ url: url, options: options || {} });
  return Promise.resolve({
    ok: true, status: 200,
    text: () => Promise.resolve('{}'),
    json: () => Promise.resolve({}),
  });
};

let api;
try {
  api = new Function(code + `
    ; return {
        getRecentLimit: () => RECENT_LIMIT,
        getAdvancedGroups: () => ADVANCED_SETTING_GROUPS,
        getLogLevel: () => logFilterLevel,
        getViewRealm: () => window.VIEW_REALM,
        getProxySlots: () => PROXY_SLOTS,
        setProxySlots: (value) => { PROXY_SLOTS = value; },
        updateUI,
      };`)();
} catch (e) {
  console.log('LOAD ERROR:', e.message);
  process.exit(1);
}

function fakeElement(data, extra){
  const el = Object.assign({
    dataset: Object.assign({}, data), disabled: false, value: '', checked: false,
  }, extra || {});
  el.closest = (sel) => (sel === '[data-action]' ? el : null);
  return el;
}

function dispatch(type, el){
  const fns = listeners[type] || [];
  assert.ok(fns.length > 0, `no ${type} listener registered`);
  for (const fn of fns) fn({ type: type, target: el });
}

global.window.updateUI = api.updateUI;

const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };

(async () => {
  // 1. click -> switchMainTab(arg) toggles the page/button classes.
  dispatch('click', fakeElement({ action: 'switchMainTab', on: 'click', arg: 'analytics' }));
  await flush();
  assert.ok(mk('pageAnalytics').classList.contains('active'), 'analytics page must activate');
  assert.ok(!mk('pageGateway').classList.contains('active'), 'gateway page must deactivate');
  assert.ok(mk('btnNavAnalytics').classList.contains('active'), 'analytics nav must activate');

  // 2. click -> setRecentLimit(arg) updates the state.
  dispatch('click', fakeElement({ action: 'setRecentLimit', on: 'click', arg: '50' }));
  await flush();
  assert.strictEqual(api.getRecentLimit(), 50, 'recent limit must become 50');

  // 3. click -> setLogFilterLevel(arg) updates the level and the button.
  dispatch('click', fakeElement({ action: 'setLogFilterLevel', on: 'click', arg: 'WARN' }));
  await flush();
  assert.strictEqual(api.getLogLevel(), 'WARN', 'log level must become WARN');
  assert.ok(mk('btnLogLvlWarn').classList.contains('active'), 'WARN button must activate');

  // 4. click -> switchViewRealm(arg) switches the visible realm.
  dispatch('click', fakeElement({ action: 'switchViewRealm', on: 'click', arg: 'cn' }));
  await flush();
  assert.strictEqual(api.getViewRealm(), 'cn', 'view realm must become cn');

  // 5. change -> setAccountSlot(uid, value, el) posts the binding.
  requests.length = 0;
  const slotEl = fakeElement({ action: 'setAccountSlot', on: 'change', uid: 'u-1' });
  slotEl.value = 'slot-2';
  dispatch('change', slotEl);
  await flush();
  const slotPost = requests.find(r => r.url === '/accounts/set');
  assert.ok(slotPost, 'slot change must POST /accounts/set');
  assert.deepStrictEqual(JSON.parse(slotPost.options.body), { uid: 'u-1', proxySlot: 'slot-2' });

  // 6. click -> setAccountProduct(uid, arg, el) posts the product.
  requests.length = 0;
  dispatch('click', fakeElement({ action: 'setAccountProduct', on: 'click', uid: 'u-2', arg: 'vscode' }));
  await flush();
  const productPost = requests.find(r => r.url === '/accounts/product');
  assert.ok(productPost, 'product switch must POST /accounts/product');
  assert.deepStrictEqual(JSON.parse(productPost.options.body), { uid: 'u-2', product: 'vscode' });

  // 7. click -> toggleAccount(uid, arg === '1', el) posts the enable flag.
  requests.length = 0;
  dispatch('click', fakeElement({ action: 'toggleAccount', on: 'click', uid: 'u-3', arg: '1' }));
  await flush();
  const togglePost = requests.find(r => r.url === '/accounts/set');
  assert.ok(togglePost, 'account toggle must POST /accounts/set');
  assert.deepStrictEqual(JSON.parse(togglePost.options.body), { uid: 'u-3', enabled: true });

  // 8. input -> proxy slot edits mutate the local slot table.
  api.setProxySlots([{ id: 'slot-1', name: '', url: '', enabled: true }]);
  const nameEl = fakeElement({ action: 'onProxySlotNameInput', on: 'input', arg: '0' });
  nameEl.value = 'edge-a';
  dispatch('input', nameEl);
  const enabledEl = fakeElement({ action: 'onProxySlotEnabledChange', on: 'change', arg: '0' });
  enabledEl.checked = false;
  dispatch('change', enabledEl);
  assert.strictEqual(api.getProxySlots()[0].name, 'edge-a', 'slot name must update');
  assert.strictEqual(api.getProxySlots()[0].enabled, false, 'slot enabled must update');

  // 9. event-type gating: a change-only control must ignore a click event.
  requests.length = 0;
  const gated = fakeElement({ action: 'setAccountSlot', on: 'change', uid: 'u-4' });
  gated.value = 'slot-9';
  dispatch('click', gated);
  await flush();
  assert.strictEqual(requests.length, 0, 'click must not trigger a change-only action');

  // 10. advanced settings schema covers all six backend groups and 49 keys.
  const groups = api.getAdvancedGroups();
  assert.strictEqual(groups.length, 6, 'advanced settings must have six groups');
  assert.strictEqual(groups.reduce((n, g) => n + g.fields.length, 0), 49,
    'advanced settings must expose all 49 backend keys');

  console.log('action dispatch assertions passed (10 checks)');
})().catch(err => {
  console.log('ASSERTION ERROR:', err.message);
  process.exit(1);
});