/* Exercise the account row renderer shipped in dashboard.html. Run with Node. */
const assert = require('assert');
const {dashboardScript} = require('./_dashboard_source.js');

const script = dashboardScript();
// One shared fake DOM for every dashboard suite: tests/_dom_stub.js.
const dom = require('./_dom_stub.js');
dom.installDom({
  fetch: () => Promise.resolve({ok: true, status: 200, json: () => Promise.resolve({})}),
});

const {accountRow, coolPills, fmtCoolAt} = new Function(script + `
  return {accountRow, coolPills, fmtCoolAt};`)();
const base = overrides => Object.assign({
  uid: 'uid-cn-0001', nickname: 'synthetic', realm: 'cn', enabled: true,
  source: 'oauth', expiresIn: '24 hours', lastError: '', inCooldown: false,
  cooldownFor: null, machineId: '', credits: null, product: 'cli',
}, overrides);
const until = Math.floor(Date.now() / 1000) + 600;
const d = new Date(until * 1000);
const pad = n => String(n).padStart(2, '0');
const localTime = d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-'
  + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());

assert.equal(coolPills(base({})), '');
assert.equal(coolPills(base({modelCooldowns: []})), '');
assert.equal(fmtCoolAt(until), localTime);
assert.equal(fmtCoolAt('bad-value'), '');

const row = accountRow(base({modelCooldowns: [{model: 'glm-5.3', expiresAt: until}]}));
assert(row.includes('glm-5.3 · ' + localTime + ' 恢复'));
assert(row.includes('时间为本地时间'));
assert(row.includes('>可用</span>')); // The account still serves other models.
assert(!row.includes('冷却 600s'));
const throttled = accountRow(base({lastError: 'HTTP 429 (model throttled)',
  modelCooldowns: [{model: 'glm-5.3', expiresAt: until}]}));
assert(!throttled.includes('HTTP 429 (model throttled)'));
const otherError = accountRow(base({lastError: 'HTTP 401',
  modelCooldowns: [{model: 'glm-5.3', expiresAt: until}]}));
assert(otherError.includes('HTTP 401'));

const multi = accountRow(base({modelCooldowns: [
  {model: 'glm-5.2', expiresAt: until}, {model: 'glm-5.3', expiresAt: until + 60},
]}));
assert(multi.indexOf('glm-5.2') < multi.indexOf('glm-5.3'));
assert.equal((multi.match(/class="cool-pill"/g) || []).length, 2);

const unsafe = accountRow(base({modelCooldowns: [
  {model: '"><img src=x onerror=alert(1)>', expiresAt: until},
  {model: 'invalid', expiresAt: 'bad-value'},
]}));
assert(!unsafe.includes('<img src=x'));
assert(unsafe.includes('&quot;&gt;&lt;img'));
assert(!unsafe.includes('invalid ·'));

console.log('model cooldown dashboard assertions passed');
