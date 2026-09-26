/* An account action must not move the panel to the other realm's view.

   国内签到 (doCheckin) is only reachable from the cn view and 国际活跃打卡
   (doDailyChat) only from the intl view. Both used to finish with initRealm(),
   which both snapped VIEW_REALM back to the active gateway exit and re-read the
   ?view= parameter - so with the exit (or a pinned URL) pointing at the other
   realm, the action threw the panel across. Boot-time view selection now lives
   in initRealm() alone; the actions call refreshActiveRealm(). Run with Node.
*/
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, 'dashboard.html'), 'utf8');
const script = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)]
  .map(match => match[1]).join('\n');
const element = () => ({
  innerHTML: '', textContent: '', value: '', style: {},
  classList: {add(){}, remove(){}, contains(){ return false; }},
  addEventListener(){}, querySelector(){ return null; }, querySelectorAll(){ return []; },
  appendChild(){}, focus(){}, setAttribute(){}, getAttribute(){ return ''; },
});
global.document = {
  getElementById: element, querySelector: () => null, querySelectorAll: () => [],
  addEventListener(){}, createElement: element, body: element(), head: element(),
  documentElement: element(),
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
global.confirm = () => false;

// The panel talks to one endpoint shape at a time; answer every URL with a
// superset payload so each consumer finds its own field. `activeRealm` is what
// GET /realm reports, i.e. the gateway exit the panel is currently switched to.
let activeRealm = 'intl';
global.fetch = () => {
  const payload = {current: activeRealm, accounts: [], slots: [], data: [],
                   results: [], byAccount: []};
  return Promise.resolve({
    status: 200, ok: true,
    json: () => Promise.resolve(payload),
    text: () => Promise.resolve(JSON.stringify(payload)),
  });
};

const api = new Function(script + `
  // In the browser these are global-function bindings on window; mirror that so
  // the functions under test can reach updateUI()/toast() from inside the stub.
  window.updateUI = updateUI;
  window.toast = toast;
  return {
    initRealm,
    refreshActiveRealm,
    doCheckin,
    doDailyChat,
    view: () => window.VIEW_REALM,
    active: () => window.ACTIVE_GATEWAY_REALM,
  };`)();

const button = () => ({disabled: false, textContent: '按钮'});
const pin = search => { window.location.search = search; };

(async () => {
  // 1. Boot keeps its old meaning: the view follows the active exit.
  activeRealm = 'intl';
  window.VIEW_REALM = 'cn';
  pin('');
  await api.initRealm();
  assert.equal(window.VIEW_REALM, 'intl', 'boot must snap the view to the active exit');
  assert.equal(window.ACTIVE_GATEWAY_REALM, 'intl');

  // 2. A pinned ?view= still wins at boot.
  activeRealm = 'intl';
  window.VIEW_REALM = 'intl';
  pin('?view=cn');
  await api.initRealm();
  assert.equal(window.VIEW_REALM, 'cn', 'boot must honour ?view=');
  pin('');

  // 3. refreshActiveRealm() refreshes the exit and leaves the view alone.
  activeRealm = 'intl';
  window.VIEW_REALM = 'cn';
  window.ACTIVE_GATEWAY_REALM = 'cn';      // stale local copy of the exit
  await api.refreshActiveRealm();
  assert.equal(window.ACTIVE_GATEWAY_REALM, 'intl', 'the exit is refreshed');
  assert.equal(window.VIEW_REALM, 'cn', 'refreshActiveRealm must not move the view');

  // 4. The reported bug: 签到 from the cn view while the exit is intl.
  activeRealm = 'intl';
  window.VIEW_REALM = 'cn';
  await api.doCheckin(button());
  assert.equal(window.VIEW_REALM, 'cn', 'doCheckin must not jump to the intl view');

  // 5. The symmetric case: 活跃打卡 from the intl view while the exit is cn.
  activeRealm = 'cn';
  window.VIEW_REALM = 'intl';
  await api.doDailyChat(button());
  assert.equal(window.VIEW_REALM, 'intl', 'doDailyChat must not jump to the cn view');

  // 6. A pinned ?view= in the entry URL must not resurface on an action: the
  //    panel is opened as ?view=intl, the user switches to cn by hand (which
  //    does not rewrite the URL), then presses 签到.
  activeRealm = 'intl';
  window.VIEW_REALM = 'cn';
  pin('?view=intl');
  await api.doCheckin(button());
  assert.equal(window.VIEW_REALM, 'cn',
    'doCheckin must keep a manually switched view even when the URL says ?view=intl');

  // 7. ... and the same in reverse.
  activeRealm = 'cn';
  window.VIEW_REALM = 'intl';
  pin('?view=cn');
  await api.doDailyChat(button());
  assert.equal(window.VIEW_REALM, 'intl',
    'doDailyChat must keep a manually switched view even when the URL says ?view=cn');
  pin('');

  console.log('realm view assertions passed');
})().catch(error => { console.error(error); process.exit(1); });
