/* 「OpenRouter 价估算」列的悬停提示必须把一条请求的价说圆。
 *
 * 这一列显示的只是一个数，用户真正要问的是「这个数怎么来的」：按哪条策略、
 * 三档单价各多少、按什么汇率折算、这条价是直接同名命中、人工映射还是剥后缀
 * 继承来的、落在哪个条件档、是不是补算。原来的原生 title 只说策略 id，既看
 * 不到价也看不出匹配链。
 *
 * 这里守住四件事：
 *   1. 三种匹配方式各自给出可区分的证据链，direct 不冒充 override；
 *   2. 条件档位那档的三档单价，不是基准档的；
 *   3. 未定价照实说「暂无定价数据」，不显示 0 或空白；
 *   4. 提示里的动态文本一律转义（模型名来自上游，不能当 HTML 拼进去）。
 *
 * Run with Node: node tests/_test_pricing_tooltip.js
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard.html'), 'utf8');
const script = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)]
  .map(match => match[1]).join('\n');

const element = (id) => ({
  id: id, innerHTML: '', textContent: '', value: '', checked: false, style: {},
  classList: {add(){}, remove(){}, toggle(){}, contains(){ return false; }},
  addEventListener(){}, querySelector(){ return null; }, querySelectorAll(){ return []; },
  appendChild(){}, focus(){}, setAttribute(){}, getAttribute(){ return ''; },
  getBoundingClientRect(){ return {top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0}; },
  offsetWidth: 0, offsetHeight: 0,
});
global.document = {
  getElementById: (id) => element(id),
  querySelector: () => null, querySelectorAll: () => [],
  addEventListener(){}, createElement: () => element('created'),
  body: element('body'), head: element('head'), documentElement: element('html'),
};
global.window = {addEventListener(){}, location: {href: '', search: ''},
  innerWidth: 1600, innerHeight: 900,
  matchMedia: () => ({matches: false, addEventListener(){}})};
global.localStorage = {getItem(){ return null; }, setItem(){}, removeItem(){}};
global.sessionStorage = global.localStorage;
global.navigator = {userAgent: 'node'};
global.setInterval = () => 0;
global.setTimeout = () => 0;
global.fetch = () => Promise.resolve({status: 200, ok: true,
  json: () => Promise.resolve({}), text: () => Promise.resolve('{}')});

const api = new Function(script + `
  return {costTitle, costTipHtml, costTipModel, costVariantSuffix, fmtRate};`)();

// 一条真实的直接匹配行：deepseek-v4.1-flash 的三档价就是它唯一的档。
const direct = {
  model: 'deepseek-v4.1-flash', cost_cny: 0.001234, cost_band: null,
  cost_source: '09bbbab71283', cost_source_at: 1790908275, cost_backfilled: false,
  cost_rates: {input_cache_hit: 0.02185, input_cache_miss: 0.02185, output: 0.63},
  cost_unit: 1000000, cost_currency: 'USD', cost_usd_cny: 7.1,
  cost_or_id: 'deepseek/deepseek-v4.1-flash', cost_via: 'direct',
  cost_inherited_from: null, cost_override_from: null, cost_band_note: null,
  cost_via_derived: false,
};

// 1. 直接匹配：说清策略、三档价、汇率，且不冒充人工映射。
{
  const text = api.costTitle(direct);
  assert.ok(text.includes('09bbbab71283'), '策略 id 要在: ' + text);
  assert.ok(text.includes('0.02185') && text.includes('0.63'),
            '三档单价要在: ' + text);
  assert.ok(text.includes('输入（缓存未命中）') && text.includes('输入（缓存命中）')
            && text.includes('输出'), '三档要分别标明: ' + text);
  assert.ok(text.includes('7.10'), '汇率要在: ' + text);
  assert.ok(text.includes('直接匹配'), '要说明是直接匹配: ' + text);
  assert.ok(!text.includes('人工映射'), '直接匹配不得写成人工映射: ' + text);
  assert.ok(text.includes('deepseek/deepseek-v4.1-flash'), 'or_id 要在: ' + text);
}

// 2. 人工映射：给出 原始 hub 名 → or_id 这条链，并标出是映射表命中。
{
  const r = Object.assign({}, direct, {
    model: 'deepseek-v3-1-volc', cost_via: 'override',
    cost_override_from: 'deepseek-v3-1-volc', cost_via_derived: false,
    cost_or_id: 'deepseek/deepseek-chat-v3.1',
  });
  const text = api.costTitle(r);
  assert.ok(text.includes('人工映射表'), '要说明来自映射表: ' + text);
  assert.ok(text.includes('deepseek-v3-1-volc → deepseek/deepseek-chat-v3.1'),
            '要给出原始名 → or_id: ' + text);
  assert.ok(!text.includes('推断'), '记录下来的映射不该标成推断: ' + text);
}

// 3. 老策略行没有 via 字段，只能按当前映射表推断 —— 必须照实标出来。
{
  const r = Object.assign({}, direct, {
    model: 'hy4-preview-f', cost_via: 'override',
    cost_override_from: 'hy4-preview-f', cost_via_derived: true,
    cost_or_id: 'tencent/hy4-preview',
  });
  const text = api.costTitle(r);
  assert.ok(text.includes('推断'), '推断来的要标注: ' + text);
  assert.ok(text.includes('hy4-preview-f → tencent/hy4-preview'),
            '推断也要给出映射链: ' + text);
}

// 4. 变体继承：原始名 → 基准名 → or_id，并标明剥掉的后缀。
{
  const r = Object.assign({}, direct, {
    model: 'deepseek-r1-0528-lkeap', cost_via: 'variant',
    cost_inherited_from: 'deepseek-r1-0528',
    cost_or_id: 'deepseek/deepseek-r1-0528',
  });
  const text = api.costTitle(r);
  assert.ok(text.includes('变体后缀继承'), '要说明是变体继承: ' + text);
  assert.ok(text.includes('deepseek-r1-0528-lkeap → deepseek-r1-0528（基准） → deepseek/deepseek-r1-0528'),
            '三段链要在: ' + text);
  assert.ok(text.includes('剥掉的后缀：-lkeap'), '剥掉的后缀要在: ' + text);
  assert.equal(api.costVariantSuffix(r), '-lkeap');
  // 基准名不是前缀时不能瞎截。
  assert.equal(api.costVariantSuffix({model: 'x-lkeap', cost_inherited_from: 'other'}), '');
}

// 5. 条件档位：显示的是这一行实际落的那一档的价，不是基准价。
{
  const r = Object.assign({}, direct, {
    model: 'hy4-preview-f', cost_band: 1,
    cost_band_note: '每天 16:00–24:00 UTC',
    cost_rates: {input_cache_hit: 0.0378, input_cache_miss: 0.7506, output: 2.2509},
  });
  const text = api.costTitle(r);
  assert.ok(text.includes('第 2 档'), '档位序号要在: ' + text);
  assert.ok(text.includes('每天 16:00–24:00 UTC'), '档位条件要在: ' + text);
  assert.ok(text.includes('0.7506') && text.includes('2.2509'),
            '要显示该档的三档价: ' + text);
  assert.ok(!text.includes('0.02185'), '不得混进别的档的价: ' + text);
}

// 6. 补算与出厂快照两条兜底说明还在。
{
  const backfilled = api.costTitle(Object.assign({}, direct, {cost_backfilled: true}));
  assert.ok(backfilled.includes('补算'), '补算标记要在: ' + backfilled);
  const builtin = api.costTitle(Object.assign({}, direct, {cost_source: 'builtin',
    cost_source_at: null}));
  assert.ok(builtin.includes('出厂快照'), '出厂快照依据要在: ' + builtin);
}

// 7. 未定价：照实说，不给 0、不给空白。
{
  const r = Object.assign({}, direct, {cost_cny: null, cost_rates: null,
    cost_unit: null, cost_currency: null, cost_usd_cny: null, cost_or_id: null,
    cost_via: null});
  const text = api.costTitle(r);
  assert.equal(text, '按 OpenRouter 公布的模型价折算的等价 token 花费\n该模型暂无定价数据',
               '未定价只该有一句说明: ' + text);
  assert.ok(api.costTipHtml(r).includes('该模型暂无定价数据'), '气泡也要说这句');
  assert.ok(!api.costTipHtml(r).includes('0.00'), '未定价不得显示 0');
  assert.deepEqual(api.costTipModel(r).rates, [], '未定价没有三档价可言');
}

// 8. 缓存命中价没公布时，说明按未命中价计（计价口径的兜底）。
{
  const r = Object.assign({}, direct, {
    cost_rates: {input_cache_hit: null, input_cache_miss: 0.3, output: 1.2}});
  const text = api.costTitle(r);
  assert.ok(text.includes('未公布，按未命中价计'), '要说明兜底口径: ' + text);
}

// 9. 动态文本一律转义：模型名来自上游，不能当 HTML 拼进气泡。
{
  const r = Object.assign({}, direct, {model: '<img src=x onerror=alert(1)>',
    cost_or_id: '<b>or</b>'});
  const out = api.costTipHtml(r);
  assert.ok(!out.includes('<img') && !out.includes('<b>'),
            'HTML 必须被转义: ' + out);
  assert.ok(out.includes('&lt;b&gt;or&lt;/b&gt;'), '转义后的文本要还在: ' + out);
}

// 10. 源码级：最近请求那一列挂的是 data-cost-key（自绘气泡），不是原生 title。
{
  const cell = html.match(/<td data-label="OpenRouter 价估算"[^>]*>/g) || [];
  const recent = cell.find(tag => tag.includes('data-cost-key'));
  assert.ok(recent, '最近请求的价估算列要有 data-cost-key: ' + cell.join(' | '));
  assert.ok(!recent.includes('title='), '自绘气泡不应再挂原生 title: ' + recent);
  assert.ok(/\.cost-cell\{cursor:help\}/.test(html), '悬停列要有手型提示样式');
}

console.log('pricing tooltip: all checks passed');
