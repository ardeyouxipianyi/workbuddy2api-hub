/* CodeArts 账号表与启停/删除/签到操作。
 *
 * Run with Node: node tests/_test_codearts_accounts.js
 */
'use strict';
const assert = require('assert');
const {dashboardHtml, dashboardScript} = require('./_dashboard_source.js');
const dom = require('./_dom_stub.js');

const html = dashboardHtml();
const script = dashboardScript();

assert.ok(html.includes('id="codeartsAccounts"'), '缺少 CodeArts 账号表');
assert.ok(/renderCodeartsAccounts/.test(script), '缺少账号表渲染函数');
assert.ok(/codeartsSlotSelectHtml/.test(script), '缺少 CodeArts 出口选择器');
assert.ok(script.includes('/codearts/accounts/proxy'), '缺少出口绑定端点');
assert.ok(script.includes("codeartsAccountAction(\\'delete\\'"),
          '缺少删除操作入口');
assert.ok(/confirm\("删除 CodeArts 账号/.test(script), '删除应有二次确认');

let POSTS = [];
dom.installDom({
  confirm: () => true,
  fetch: (url, opts) => {
    const body = opts && opts.body ? JSON.parse(String(opts.body)) : {};
    if (opts && opts.method === 'POST') POSTS.push({url: String(url), body});
    const payload = {ok: true, message: '已删除 u1', online: true,
                     stats: {total: 2, healthy: 1}, accounts: [], models: []};
    return Promise.resolve({
      status: 200, ok: true,
      json: () => Promise.resolve(payload),
      text: () => Promise.resolve(JSON.stringify(payload)),
    });
  },
});

const realLog = console.log;
console.log = () => {};
let api;
try {
  api = new Function(script + `
    window.updateUI = function(){};
    return {renderCodeartsAccounts: renderCodeartsAccounts,
            codeartsAccountAction: codeartsAccountAction,
            codeartsCheckinAll: codeartsCheckinAll,
            codeartsSetProxy: codeartsSetProxy,
            refreshCodeartsCredits: refreshCodeartsCredits,
            setSlots: function(v){ PROXY_SLOTS = v; }};
  `)();
} catch (e) {
  console.log = realLog;
  console.error('脚本求值失败:', e.message);
  process.exit(1);
}
console.log = realLog;

const box = document.getElementById('codeartsAccounts');
api.setSlots([{id: 'slot-1', name: '美国 住宅', url: 'http://p:1',
               enabled: true}]);
api.renderCodeartsAccounts({
  stats: {total: 2, healthy: 1},
  accounts: [
    {uid: 'u1', nickname: '账号一', disabled: false, cooling: false,
     proxy_slot: 'slot-1', note: '备注一', tasks_enabled: true,
     token_remaining: '1h0m0s', expires_at: '2026-10-31T12:00:00+08:00',
     active_concurrent: 0, max_concurrent: 5, checkin: {ok: true}},
    {uid: 'u2', nickname: '账号二', disabled: true, cooling: false,
     reason: 'manual', token_remaining: '2h0m0s', expires_at: '',
     active_concurrent: 0, max_concurrent: 5, checkin: null},
  ],
});
assert.ok(box.innerHTML.includes('账号一'), '应渲染昵称');
assert.ok(box.innerHTML.includes('账号二'), '应渲染第二个账号');
assert.ok(box.innerHTML.includes('已停用'), '停用账号应显示状态');
assert.ok(box.innerHTML.includes('已签到'), '签到状态应显示');
assert.ok(box.innerHTML.includes("codeartsAccountAction('enable','u2')"),
  '停用账号应显示启用按钮');
assert.ok(box.innerHTML.includes("codeartsAccountAction('disable','u1')"),
  '正常账号应显示停用按钮');
assert.ok(box.innerHTML.includes("codeartsRelogin('u1')"),
  '应显示重新登录按钮');
assert.ok(box.innerHTML.includes("codeartsAccountAction('test','u1')"),
  '应显示单账号测试按钮');
assert.ok(box.innerHTML.includes("codeartsAccountAction('tasks','u1',false)"),
  '应显示任务开关按钮');
assert.ok(box.innerHTML.includes("codeartsEditNote('u1')"),
  '应显示备注按钮');
assert.ok(box.innerHTML.includes("codeartsAccountAction('delete','u1')"),
  '应显示删除按钮');
assert.ok(box.innerHTML.includes('备注一'), '账号表应显示备注');
assert.ok(box.innerHTML.includes('codeartsSetProxy'), '应显示出口选择器');
assert.ok(box.innerHTML.includes('slot-1'), '出口选择器应包含已绑定槽位');
assert.ok(box.innerHTML.includes('美国 住宅'), '出口选择器应显示槽位名称');

// UID 来自服务端/上游，不能直接拼进 inline onclick 的 JS 字符串。HTML 转义
// 不处理单引号，所以这里必须用只允许安全字符的 id 形态。
api.renderCodeartsAccounts({
  stats: {total: 1, healthy: 1},
  accounts: [{uid: "x' onmouseover='alert(1)", nickname: 'evil',
              disabled: false, cooling: false, proxy_slot: '',
              token_remaining: '1h0m0s', expires_at: '',
              active_concurrent: 0, max_concurrent: 5, checkin: null}],
});
const evilHtml = box.innerHTML;
assert.ok(!/onclick="[^"]*x' onmouseover/.test(evilHtml),
  'UID 中的单引号不能进入 inline onclick 的 JS 字符串');
assert.ok(evilHtml.includes("codeartsAccountAction('delete','xonmouseoveralert1')"),
  'UID 应按安全字符集收敛后再拼进处理器');

(async () => {
  await api.codeartsAccountAction('delete', 'u1');
  const hit = POSTS.find(p => p.url.indexOf('/codearts/accounts/delete') >= 0);
  assert.ok(hit, '删除应 POST /codearts/accounts/delete');
  assert.equal(hit.body.uid, 'u1', '删除应带上 uid');

  POSTS = [];
  await api.codeartsSetProxy('u1', 'slot-1', null);
  const bind = POSTS.find(p => p.url.indexOf('/codearts/accounts/proxy') >= 0);
  assert.ok(bind, '出口绑定应 POST /codearts/accounts/proxy');
  assert.equal(bind.body.uid, 'u1', '出口绑定应带上 uid');
  assert.equal(bind.body.slot_id, 'slot-1', '出口绑定应带上 slot_id');

  POSTS = [];
  await api.codeartsAccountAction('tasks', 'u1', false);
  const tasks = POSTS.find(p => p.url.indexOf('/codearts/accounts/tasks') >= 0);
  assert.ok(tasks, '任务开关应 POST /codearts/accounts/tasks');
  assert.equal(tasks.body.enabled, false, '任务开关应带上 enabled');

  POSTS = [];
  await api.codeartsAccountAction('note', 'u1', 'hello');
  const note = POSTS.find(p => p.url.indexOf('/codearts/accounts/note') >= 0);
  assert.ok(note, '备注应 POST /codearts/accounts/note');
  assert.equal(note.body.note, 'hello', '备注应带上 note');

  POSTS = [];
  await api.codeartsAccountAction('test', 'u1');
  const tested = POSTS.find(p => p.url.indexOf('/codearts/accounts/test') >= 0);
  assert.ok(tested, '测试应 POST /codearts/accounts/test');
  assert.equal(tested.body.uid, 'u1', '测试应带上 uid');

  POSTS = [];
  await api.refreshCodeartsCredits(null);
  const credits = POSTS.find(p => p.url.indexOf('/codearts/credits/refresh') >= 0);
  assert.ok(credits, '刷新积分应 POST /codearts/credits/refresh');

  POSTS = [];
  await api.codeartsCheckinAll();
  const checkin = POSTS.find(p => p.url.indexOf('/codearts/accounts/checkin') >= 0);
  assert.ok(checkin, '全员签到应 POST /codearts/accounts/checkin');
  assert.deepEqual(checkin.body, {}, '全员签到不应带 uid');

  console.log('codearts account assertions passed');
})().catch(error => { console.log = realLog; console.error(error); process.exit(1); });
