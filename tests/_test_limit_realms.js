/* 限额表的前端契约：全局默认 + 可选分版本。
 *
 * 后端把五条护栏存成「全局默认 + 可选分版本覆盖」，面板这张表必须一一对上：
 * 每条护栏三列输入（全局 / 国际版 / 国内版），id 由 LIMIT_FIELDS 与
 * LIMIT_SCOPES 的名字拼出来；某一版本留空表示继承全局，保存时发 null 让服务端
 * 清掉旧覆盖值；收起「分别设置」再保存，等于把两个覆盖一起清回继承。
 *
 * 这类错名字只会静默失效——输入框填不上、或者发出去的键名对不上，页面上看不
 * 出来。所以这里把两侧的名字和语义都钉住。Run with Node.
 */
const assert = require('assert');
const {dashboardHtml, dashboardScript} = require('./_dashboard_source.js');

const html = dashboardHtml();
const script = dashboardScript();

// One shared fake DOM for every dashboard suite: tests/_dom_stub.js.
const dom = require('./_dom_stub.js');

let sent = null;
dom.installDom({
  fetch: (url, options) => {
    const body = options && options.body ? JSON.parse(options.body) : null;
    if(url === '/settings/save' && body) sent = body;
    const payload = {current: 'intl', accounts: [], slots: [], data: [],
                     results: [], byAccount: []};
    return Promise.resolve({status: 200, ok: true,
      json: () => Promise.resolve(payload),
      text: () => Promise.resolve(JSON.stringify(payload))});
  },
});

const realLog = console.log;
console.log = () => {};
const api = new Function(script + `
  return {
    LIMIT_FIELDS: LIMIT_FIELDS,
    LIMIT_SCOPES: LIMIT_SCOPES,
    limitEl: limitEl,
    applyLimits: applyLimits,
    toggleLimitRealms: toggleLimitRealms,
    saveLimits: saveLimits,
    toggleExpiringWindow: toggleExpiringWindow,
  };`)();

(async () => {
  // 1. 每条护栏 × 每个作用域都在 HTML 里有一个输入框。
  for(const field of api.LIMIT_FIELDS){
    for(const scope of api.LIMIT_SCOPES){
      const id = 'limit' + scope.prefix + field.id;
      assert.ok(html.includes('id="' + id + '"'), '缺少输入框 ' + id);
    }
  }
  assert.equal(api.LIMIT_FIELDS.length, 5, '五条护栏');
  assert.deepStrictEqual(api.LIMIT_SCOPES.map(s => s.scope),
                         ['global', 'intl', 'cn']);

  // 1b. 手机上这张表要跟着看板的「表 → 卡片」走：带上 data-cards 的类，
  //     三个作用域格各有一个 data-label 当行名，否则它会撑出 720px 的下限、
  //     把整个页面拉出横向滚动条。
  assert.ok(/<table class="data-cards limits-cards">/.test(html),
            '限额表必须带 data-cards limits-cards');
  for(const label of ['全局默认', '国际版', '国内版']){
    const hits = html.split('data-label="' + label + '"').length - 1;
    assert.equal(hits, 5, '「' + label + '」格应有 5 个（每条护栏一个），实际 ' + hits);
  }
  assert.ok(/table\.limits-cards td\.limit-name\{display:block/.test(html),
            '护栏名与说明在手机卡片里要占整块');

  // 2. 载入时：全局填进全局列，覆盖值填进对应版本列，留空的写回继承提示。
  api.applyLimits({limits: {
    reserve_credits: {global: 10, intl: 3, cn: null},
    daily_token_limit: {global: 1000, intl: null, cn: null},
    daily_credit_limit: {global: 0, intl: null, cn: null},
    model_daily_token_limit: {global: 0, intl: null, cn: null},
  }});
  assert.equal(api.limitEl('Global', 'Reserve').value, '10');
  assert.equal(api.limitEl('Intl', 'Reserve').value, '3');
  assert.equal(api.limitEl('Cn', 'Reserve').value, '');
  assert.equal(api.limitEl('Cn', 'Reserve').placeholder, '继承 10');
  assert.equal(api.limitEl('Intl', 'DailyToken').placeholder, '继承 1,000');
  assert.equal(dom.byId('setLimitsPerRealm').checked, true, '有覆盖时勾上分版本');
  assert.equal(dom.byId('setReserveState').textContent, '(全局 10 · 国际版 3)');
  assert.equal(dom.byId('setDailyTokenState').textContent, '(全局 1,000)');

  // 3. 没有覆盖时：分版本收起，两个版本列都留空。
  api.applyLimits({limits: {
    reserve_credits: {global: 0, intl: null, cn: null},
  }});
  assert.equal(dom.byId('setLimitsPerRealm').checked, false);
  assert.equal(dom.byId('setLimitsState').textContent, '(全局生效)');
  assert.equal(dom.byId('setReserveState').textContent, '(全部关闭)');

  // 4. 收起分版本时保存：即使版本列里还留着旧数字，也按“继承”发出去。
  dom.byId('setLimitsPerRealm').checked = false;
  api.limitEl('Global', 'Reserve').value = '20';
  api.limitEl('Intl', 'Reserve').value = '5';
  api.limitEl('Cn', 'Reserve').value = '1';
  sent = null;
  await api.saveLimits(null);
  assert.ok(sent && sent.limits, '保存必须发 limits');
  assert.deepStrictEqual(sent.limits.reserve_credits,
                         {global: 20, intl: null, cn: null},
                         '收起分版本 = 两个覆盖一起清回继承');
  assert.deepStrictEqual(Object.keys(sent.limits).sort(), [
    'daily_credit_limit', 'daily_token_limit',
    'expiring_window_days', 'model_daily_token_limit', 'reserve_credits',
  ]);

  // 5. 勾上分版本时保存：填了值的版本发数字，留空的仍发 null。
  dom.byId('setLimitsPerRealm').checked = true;
  api.limitEl('Global', 'Reserve').value = '20';
  api.limitEl('Intl', 'Reserve').value = '5';
  api.limitEl('Cn', 'Reserve').value = '';
  sent = null;
  await api.saveLimits(null);
  assert.deepStrictEqual(sent.limits.reserve_credits,
                         {global: 20, intl: 5, cn: null});

  // 6. 非法输入直接拦下，不发请求。
  sent = null;
  dom.byId('setLimitsPerRealm').checked = false;
  api.limitEl('Global', 'DailyToken').value = 'abc';
  await api.saveLimits(null);
  assert.equal(sent, null, '非法的全局默认不应发请求');

  sent = null;
  dom.byId('setLimitsPerRealm').checked = true;
  api.limitEl('Global', 'DailyToken').value = '10';
  api.limitEl('Cn', 'DailyToken').value = '-1';
  await api.saveLimits(null);
  assert.equal(sent, null, '非法的分版本值不应发请求');

  // 7. 临期优先开关：勾选状态跟着全局窗口值走；点一下在 7 与 0 之间切换并保存。
  api.applyLimits({limits: {expiring_window_days: {global: 7, intl: null, cn: null}}});
  assert.equal(dom.byId('setExpiringWindowToggle').checked, true, '窗口 > 0 时开关应为开');
  api.applyLimits({limits: {expiring_window_days: {global: 0, intl: null, cn: null}}});
  assert.equal(dom.byId('setExpiringWindowToggle').checked, false, '窗口 0 时开关应为关');

  sent = null;
  api.limitEl('Global', 'ExpiringWindow').value = '0';
  await api.toggleExpiringWindow({checked: true, disabled: false});
  assert.deepStrictEqual(sent.limits, {expiring_window_days: {global: 7}},
                         '从关闭打开应写回默认窗口 7');

  sent = null;
  api.limitEl('Global', 'ExpiringWindow').value = '14';
  await api.toggleExpiringWindow({checked: false, disabled: false});
  assert.deepStrictEqual(sent.limits, {expiring_window_days: {global: 0}},
                         '关闭应写 0，不动窗口天数本身');

  // 8. 收起分版本只隐藏两列，不动全局列。
  api.toggleLimitRealms(false);
  api.toggleLimitRealms(true);

  console.log = realLog;
  console.log('limit realm assertions passed');
})().catch(err => {
  console.log = realLog;
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
