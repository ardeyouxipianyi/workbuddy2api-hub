/* 一键测试：一次看全「账号真不行」和「本机没网」。

   池子出问题时，逐个点「测试」得到的结论常常是错的：本机断网时每个账号都失败，
   看起来像「账号全挂了」；而某个账号只是被本地窗口（熔断 / 降权 / 软限流）关着，
   测试通过本该把它清掉。所以这个套件钉住三件事：

     1. 工具栏按钮 + 结果弹窗真的接上了（结构断言）；
     2. 只发一次请求、带上当前视图的 realm，渲染出「本机连通性 → 汇总 → 每账号一行」，
        网络不通的行标成「本机网络」，其余失败才叫失败；
     3. 账号昵称与回复里的 HTML 被转义（它们是上游数据，不是我们的文案）。

   页面脚本由 tests/_dashboard_sandbox.js 装进显式沙箱（假 DOM + 最小运行环境），
   断言直接打在沙箱暴露出来的函数与 DOM 上。

   Run with Node: node tests/_test_account_test_all.js
*/
'use strict';
const assert = require('assert');
const vm = require('node:vm');
const {dashboardHtml} = require('./_dashboard_source.js');
const {loadDashboard} = require('./_dashboard_sandbox.js');

const html = dashboardHtml();

/* ---- 1. 结构：按钮与弹窗都在页面上 ------------------------------------- */

assert.ok(/onclick="testAccounts\(this\)"[^>]*>一键测试</.test(html),
  '工具栏要有一个跑 testAccounts(this) 的「一键测试」按钮');
assert.ok(/id="testAllModal"/.test(html), '结果弹窗 #testAllModal 必须在页面上');
assert.ok(/id="testAllBody"/.test(html), '结果容器 #testAllBody 必须在页面上');
assert.ok(/onclick="testAccounts\(null\)"[^>]*>重新测试</.test(html),
  '弹窗里要有「重新测试」（不带按钮元素也要能跑）');
assert.ok(/onclick="closeTestAll\(\)"/.test(html), '弹窗要能关掉');

/* ---- 2. 行为：页面脚本 + 假 DOM + 假 fetch ----------------------------- */

let reply = null;
const posts = [];
const toasts = [];

const page = loadDashboard({
  fetch: (url, options) => {
    posts.push({url: String(url), body: options && options.body});
    const body = typeof reply === 'function' ? reply(String(url)) : (reply || {});
    return Promise.resolve({
      status: 200, ok: true,
      json: () => Promise.resolve(body),
      text: () => Promise.resolve(JSON.stringify(body)),
    });
  },
});

// toast 换成记录器：断言用户看到的那句话，而不是断言 DOM 里的浮层。
page.toast = (msg, kind) => { toasts.push({msg: String(msg), kind: kind}); };
assert.strictEqual(typeof page.testAccounts, 'function', 'dashboard 里要有 testAccounts');
assert.strictEqual(typeof page.testAccount, 'function', 'dashboard 里要有 testAccount');

const bodyHtml = () => page.document.getElementById('testAllBody').innerHTML;
const modalShown = () => page.document.getElementById('testAllModal').classList.contains('show');

function check(label, ok, detail) {
  assert.ok(ok, label + (detail === undefined ? '' : '  -> ' + detail));
}

const NET_OK = {realm: 'cn', url: 'https://www.workbuddy.cn', host: 'www.workbuddy.cn',
                ip: '1.2.3.4', ok: true, dns_ms: 4, tcp_ms: 21, error: ''};
const NET_DOWN = {realm: 'cn', url: 'https://www.workbuddy.cn', host: 'www.workbuddy.cn',
                  ip: '', ok: false, dns_ms: null, tcp_ms: null,
                  error: 'DNS 解析失败：gaierror'};

(async () => {
  // 2a. 正常一轮：一次请求、带视图 realm、渲染连通性与每账号结果
  reply = {
    ok: true,
    network: NET_OK,
    summary: {realm: 'cn', model: 'deepseek-v4.1-flash', tested: 2, passed: 1, failed: 1,
              networkDown: false, elapsed_ms: 1234},
    results: [
      {uid: 'cn000001-0000', nickname: '好账号', realm: 'cn', ok: true, elapsed_ms: 900,
       reply: 'Hi!', error: '', network: false},
      {uid: 'cn000002-0000', nickname: '坏账号', realm: 'cn', ok: false, elapsed_ms: 30,
       status: 429, reply: '', error: 'HTTP 429: 频率限制', network: false, fails: 2},
    ],
  };
  const btn = {disabled: false, textContent: '一键测试'};
  page.VIEW_REALM = 'cn';
  posts.length = 0;
  toasts.length = 0;
  await page.testAccounts(btn);

  // 只数 /accounts/test：跑完之后面板还会自己刷新账号列表（/accounts、/usage/…）。
  const testPosts = posts.filter(p => p.url === '/accounts/test');
  check('只发一次测试请求', testPosts.length === 1, JSON.stringify(posts.map(p => p.url)));
  const sent = JSON.parse(testPosts[0].body || '{}');
  check('带上当前视图的 realm', sent.realm === 'cn', JSON.stringify(sent));
  check('不传 uid（传了就退化成单账号测试）', sent.uid === undefined, JSON.stringify(sent));
  check('未选分组时不传 group', sent.group === undefined, JSON.stringify(sent));
  check('弹窗被打开', modalShown() === true);
  const shown = bodyHtml();
  check('渲染本机连通性', shown.includes('本机网络正常') && shown.includes('www.workbuddy.cn'),
    shown.slice(0, 200));
  check('渲染 DNS/TCP 耗时', shown.includes('DNS 4ms') && shown.includes('TCP 21ms'),
    shown.slice(0, 300));
  check('通过的行显示回复', shown.includes('好账号') && shown.includes('Hi!'), shown.slice(0, 400));
  check('失败的行显示原因', shown.includes('坏账号') && shown.includes('HTTP 429'),
    shown.slice(0, 400));
  check('失败的行给出连续失败次数（离熔断还有多远）', shown.includes('连续失败 2 次'),
    shown.slice(0, 400));
  check('按钮复位', btn.disabled === false && btn.textContent === '一键测试', btn.textContent);
  check('toast 报出通过数', toasts.some(t => /一键测试: 1\/2/.test(t.msg)),
    JSON.stringify(toasts));

  // 2a2. 选中分组时，一键测试只作用于该分组
  // ACCOUNT_FILTERS is a top-level const, so it is intentionally not exposed
  // as a window property; evaluate inside the page's own VM context.
  vm.runInContext("ACCOUNT_FILTERS.group = 'Team A'", page);
  posts.length = 0;
  await page.testAccounts(null);
  const grouped = JSON.parse(
    posts.filter(p => p.url === '/accounts/test')[0].body || '{}');
  check('选中分组时带上 group', grouped.group === 'Team A', JSON.stringify(grouped));
  vm.runInContext("ACCOUNT_FILTERS.group = ''", page);

  // 2b. 本机断网：网络行变红、失败行标成「本机网络」而不是账号失败
  reply = {
    ok: true,
    network: NET_DOWN,
    summary: {realm: 'cn', model: 'm', tested: 2, passed: 0, failed: 2,
              networkDown: true, elapsed_ms: 88},
    results: [
      {uid: 'cn000001-0000', nickname: '好账号', realm: 'cn', ok: false, elapsed_ms: 5,
       reply: '', error: '本机网络不可用：DNS 解析失败', network: true},
      {uid: 'cn000002-0000', nickname: '坏账号', realm: 'cn', ok: false, elapsed_ms: 5,
       reply: '', error: '本机网络不可用：DNS 解析失败', network: true},
    ],
  };
  toasts.length = 0;
  await page.testAccounts(null);
  const down = bodyHtml();
  check('网络行报不通', down.includes('本机网络不通'), down.slice(0, 200));
  check('网络行写出原因', down.includes('DNS 解析失败'), down.slice(0, 300));
  check('断网的行不标成账号失败', !down.includes('>失败<'), down.slice(0, 600));
  check('断网的行标成本机网络', (down.match(/本机网络/g) || []).length >= 2, down.slice(0, 600));
  check('断网时 toast 说清是网络', toasts.some(t => t.msg.includes('本机网络不通')),
    JSON.stringify(toasts));
  check('断网时提醒账号未被记账', toasts.some(t => t.msg.includes('未被计入熔断')),
    JSON.stringify(toasts));

  // 2c. 账号数据里的 HTML 必须被转义（昵称与回复都来自上游）
  reply = {
    ok: true, network: NET_OK,
    summary: {realm: 'cn', model: 'm', tested: 1, passed: 1, failed: 0,
              networkDown: false, elapsed_ms: 10},
    results: [{uid: 'cn000003', nickname: '<img src=x onerror=alert(1)>', realm: 'cn',
               ok: true, elapsed_ms: 1, reply: '<b>bold</b>', error: '', network: false}],
  };
  await page.testAccounts(null);
  const escaped = bodyHtml();
  check('昵称里的 HTML 被转义', !escaped.includes('<img'), escaped.slice(0, 400));
  check('回复里的 HTML 被转义', !escaped.includes('<b>bold</b>'), escaped.slice(0, 400));

  // 2d. 单账号测试：本机断网时说网络，别赖账号
  reply = {ok: false, uid: 'cn000001', status: 500, error: '本机网络不可用：DNS 解析失败',
           network: true};
  toasts.length = 0;
  const rowBtn = {disabled: false, textContent: '测试'};
  await page.testAccount('cn000001', rowBtn);
  check('单账号测试说本机网络', toasts.some(t => t.msg.includes('本机网络不可用')),
    JSON.stringify(toasts));
  check('单账号测试不再报「测试失败」', !toasts.some(t => t.msg.includes('测试失败')),
    JSON.stringify(toasts));
  check('单账号按钮复位', rowBtn.disabled === false && rowBtn.textContent === '测试',
    rowBtn.textContent);

  reply = {ok: false, uid: 'cn000002', status: 429, error: 'HTTP 429: 频率限制'};
  toasts.length = 0;
  await page.testAccount('cn000002', rowBtn);
  check('普通失败照旧报测试失败', toasts.some(t => t.msg.includes('测试失败')), JSON.stringify(toasts));

  console.log('account test-all assertions passed');
})().catch((err) => {
  console.error(err.message);
  process.exit(1);
});
