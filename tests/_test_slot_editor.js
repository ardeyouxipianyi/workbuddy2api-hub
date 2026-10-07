/* The proxy-slot editor must survive the panel's polling (issue #79).
 *
 * loadAccounts() runs every 15 seconds and used to refresh the slot list with
 * it. That refresh replaces the whole list and re-renders the table, so a row
 * the operator had just added with 「+ 添加槽位」 - not saved to the server yet -
 * was wiped before they could type anything into it. The editor now refuses to
 * be refreshed while it holds unsaved changes, and says so in its header.
 *
 * Run with Node: node tests/_test_slot_editor.js
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard.html'), 'utf8');
const script = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)]
  .map(match => match[1]).join('\n');

// Element stubs are cached per id, so text the page writes can be read back.
const elements = new Map();
const element = (id) => {
  let el = elements.get(id);
  if(!el){
    el = {id: id, innerHTML: '', textContent: '', value: '', checked: false, style: {},
          classList: {add(){}, remove(){}, toggle(){}, contains(){ return false; }},
          addEventListener(){}, querySelector(){ return null; }, querySelectorAll(){ return []; },
          appendChild(){}, focus(){}, setAttribute(){}, getAttribute(){ return ''; }};
    elements.set(id, el);
  }
  return el;
};
global.document = {
  getElementById: (id) => element(id),
  querySelector: () => null, querySelectorAll: () => [],
  addEventListener(){}, createElement: () => element('created'), body: element('body'),
  head: element('head'), documentElement: element('html'),
};
global.window = {addEventListener(){}, location: {href: '', search: ''},
  matchMedia: () => ({matches: false, addEventListener(){}})};
global.localStorage = {getItem(){ return null; }, setItem(){}, removeItem(){}};
global.sessionStorage = global.localStorage;
global.navigator = {userAgent: 'node'};
global.setInterval = () => 0;
global.setTimeout = () => 0;
global.location = {href: '', search: '', hash: ''};
global.alert = () => {};
global.confirm = () => true;

// What the server would return for GET /proxy/slots; the test swaps it.
let serverSlots = [];
global.fetch = () => {
  const payload = {current: 'intl', accounts: [], slots: serverSlots, data: [],
                   results: [], byAccount: []};
  return Promise.resolve({status: 200, ok: true,
    json: () => Promise.resolve(payload),
    text: () => Promise.resolve(JSON.stringify(payload))});
};

const api = new Function(script + `
  window.updateUI = updateUI;
  window.toast = toast;
  return {
    loadProxySlots,
    addProxySlotRow,
    saveProxySlots,
    rows: () => PROXY_SLOTS,
    dirty: () => SLOTS_DIRTY,
    state: () => (document.getElementById('slotState') || {}).textContent,
  };`)();

(async () => {
  // 1. A clean editor follows the server.
  serverSlots = [{id: 'a', name: 'a', url: 'http://127.0.0.1:1', enabled: true}];
  await api.loadProxySlots();
  assert.equal(api.rows().length, 1, 'a clean editor loads the server list');

  // 2. Adding a row marks the editor dirty, and the header says so.
  api.addProxySlotRow();
  assert.equal(api.rows().length, 2, 'the new row is in the working copy');
  assert.equal(api.dirty(), true, 'adding a row marks the editor dirty');
  assert.ok(api.state().includes('未保存'), 'the header admits the list is unsaved: ' + api.state());

  // 3. The periodic refresh must not wipe it (this is the reported bug).
  serverSlots = [{id: 'a', name: 'a', url: 'http://127.0.0.1:1', enabled: true},
                 {id: 'b', name: 'b', url: 'http://127.0.0.1:2', enabled: true}];
  await api.loadProxySlots();
  assert.equal(api.rows().length, 2, 'the poll must not replace the working copy');
  // The new row is the one with no id yet - the server rows above all have
  // one - so a poll that replaced the working copy would fail this.
  assert.equal(api.rows()[1].id, '',
               'the row being filled in is still there: ' + JSON.stringify(api.rows()[1]));

  // 4. An explicit refresh still can - the polling path is the only one held.
  await api.loadProxySlots(true);
  assert.equal(api.rows().map(r => r.id).join(','), 'a,b',
               'force reloads from the server: ' + JSON.stringify(api.rows()));

  // 5. Saving clears the flag, so polling resumes afterwards.
  api.addProxySlotRow();
  assert.equal(api.dirty(), true, 'the new row is unsaved again');
  await api.saveProxySlots(null).catch(() => {});
  assert.equal(api.dirty(), false, 'a successful save clears the dirty flag');
  assert.ok(!api.state().includes('未保存'),
            'the header stops saying unsaved: ' + api.state());

  console.log('slot editor assertions passed');
})().catch(error => { console.error(error); process.exit(1); });

