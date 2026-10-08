/* loadSettings 的完整性契约：一次 /settings 往返要把设置页每个区块都填上。
 *
 * loadSettings 是「一个 try 包住全部」的写法：中间任何一行抛异常都会被末尾的
 * catch 吞掉，只弹一条 toast，然后它后面的所有区块一起静默留空——价估算状态、
 * 总开关复选框、API Key 列表、版本号与目录。页面上只看得出「某块没内容」，
 * 看不出是异常；v1.6.14 合并 #130 时冲突处理掉了一行 `const pricingOn = ...`，
 * 正好造成这个后果：API Key 列表整个消失，而「(3 个生效)」的计数照常显示，
 * 于是最容易被当成显示问题而不是报错。
 *
 * 所以这里钉住两件事：不许吞异常，且每个区块都要真的被填上。
 * Run with Node.
 */
const assert = require('assert');
const {dashboardScript} = require('./_dashboard_source.js');

const script = dashboardScript();

// One shared fake DOM for every dashboard suite: tests/_dom_stub.js.
const dom = require('./_dom_stub.js');
dom.installDom();
const element = id => dom.byId(id);

/* 三把 Key：两把跟随面板切换、一把固定国际版出口，其中一把带模型白名单。
   价估算总开关故意设成关，好顺带检查它有没有被写回复选框与 body 类。 */
const SETTINGS = {
  panel_password_is_default: false,
  api_keys: [
    {id: 'k1', name: 'Cursor', realm: '', models: [], enabled: true,
     masked: 'wb-aaaa****1111', created_at: '2026-09-01'},
    {id: 'k2', name: 'DSH', realm: 'intl', models: ['deepseek*'], enabled: true,
     masked: 'wb-bbbb****2222', created_at: '2026-09-02'},
    {id: 'k3', name: '本地测试', realm: 'cn', models: [], enabled: false,
     masked: 'wb-cccc****3333', created_at: '2026-09-03'},
  ],
  deleted_api_keys: [],
  limits: {
    // 国际版单独设过值，所以「分别设置」应处于展开状态。
    reserve_credits: {global: 10, intl: 3, cn: null},
    daily_token_limit: {global: 1000, intl: null, cn: null},
    daily_credit_limit: {global: 0, intl: null, cn: null},
    model_daily_token_limit: {global: 0, intl: null, cn: null},
  },
  pricing_enabled: false,
  pricing_refresh_minutes: 5,
  auto_switch_product: true,
  daily_chat_web: false,
  local_web_tools: true,
  version: 'v1.6.14',
  accounts_dir: '/data/accounts',
  usage_dir: '/data/usage',
  settings_file: '/data/settings.json',
};

global.fetch = (url) => {
  const payload = url === '/settings' ? SETTINGS
    : url === '/pricing' ? {models: 0, policies: 0, current: {}}
    : {current: 'intl', accounts: [], slots: [], data: [], results: [], byAccount: []};
  return Promise.resolve({status: 200, ok: true,
    json: () => Promise.resolve(payload),
    text: () => Promise.resolve(JSON.stringify(payload))});
};

const realLog = console.log;
console.log = () => {};
const toasts = [];
let api;
try {
  api = new Function(script + `
    toast = function(msg, kind){ global.__toasts.push([msg, kind]); };
    return { loadSettings: loadSettings, getRows: () => API_KEY_ROWS };`)();
} catch(e) {
  console.log = realLog;
  console.error('脚本求值失败:', e.message);
  process.exit(1);
}
global.__toasts = toasts;

(async () => {
  await api.loadSettings(true);
  await Promise.resolve();          // 让 loadPricingStatus 这类不 await 的尾巴跑完
  await new Promise(resolve => process.nextTick(resolve));

  console.log = realLog;

  // 1. 一次成功的读取不该报错——异常被 catch 吞掉正是这个 bug 的藏身处。
  const failed = toasts.filter(([msg]) => String(msg).indexOf('读取设置失败') === 0);
  assert.deepStrictEqual(failed, [],
    'loadSettings 抛异常了：' + (failed[0] ? failed[0][0] : ''));

  // 2. API Key 列表：三把 Key 各一张卡片，名字与出口徽章都要在。
  const rows = api.getRows();
  assert.equal(rows.length, 3, 'API_KEY_ROWS 应有 3 行');
  const list = element('keyList').innerHTML;
  // 每张卡片里正好一个 key-card-main；不能拿 class="key-card 去数，
  // 那个前缀会同时命中 key-card-main。
  assert.equal(list.split('class="key-card-main"').length - 1, 3, 'keyList 应有 3 张卡片');
  for(const name of ['Cursor', 'DSH', '本地测试']){
    assert.ok(list.includes(name), '列表里缺少 ' + name);
  }
  assert.ok(list.includes('固定国际版出口'), '固定国际版出口的徽章丢了');
  assert.ok(list.includes('固定国内版出口'), '固定国内版出口的徽章丢了');
  assert.ok(list.includes('限 deepseek*'), '模型白名单徽章丢了');
  assert.ok(list.includes('wb-aaaa****1111'), '掩码后的 Key 没显示');
  assert.equal(element('setKeyState').textContent, '(2 个生效)',
    '计数只算启用中的 Key');

  // 3. 空列表时给的是提示语，不是一片空白。
  assert.ok(element('keyList').innerHTML.length > 0, 'keyList 被留空了');

  // 4. 价估算总开关：复选框要跟着后端值，关掉时 body 挂 pricing-off。
  assert.equal(element('setPricingEnabled').checked, false, '总开关复选框没写回');
  assert.equal(element('setPricingEnabledState').textContent, '(已关闭)');
  assert.ok(document.body.classList.contains('pricing-off'), '关闭时 body 应挂 pricing-off');

  // 5. 异常后面那些区块也得填上：漏填说明执行又提前断了。
  assert.equal(element('setVersion').textContent, 'v1.6.14');
  assert.equal(element('setAccountsDir').textContent, '/data/accounts');
  assert.equal(element('setPricingState').textContent, '(每 5 分钟)');
  assert.equal(element('setAutoSwitch').checked, true);
  assert.equal(element('setDailyChatWeb').checked, false);
  assert.equal(element('setLocalWebTools').checked, true);
  assert.equal(element('setLimitsPerRealm').checked, true, '有分版本覆盖时应勾上');

  realLog('loadSettings 完整性断言通过（14 项）');
})().catch(e => {
  console.log = realLog;
  console.error(e && e.stack || e);
  process.exit(1);
});
