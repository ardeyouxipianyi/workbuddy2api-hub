/* 「OpenRouter 價估算」列的懸停提示必須把一條請求的價說圓。
 *
 * 這一列顯示的只是一個數，使用者真正要問的是「這個數怎麼來的」：按哪條策略、
 * 三檔單價各多少、按什麼匯率折算、這條價是直接同名命中、人工對映還是剝字尾
 * 繼承來的、落在哪個條件檔、是不是補算。原來的原生 title 只說策略 id，既看
 * 不到價也看不出匹配鏈。
 *
 * 這裡守住四件事：
 *   1. 三種匹配方式各自給出可區分的證據鏈，direct 不冒充 override；
 *   2. 條件檔位那檔的三檔單價，不是基準檔的；
 *   3. 未定價照實說「暫無定價資料」，不顯示 0 或空白；
 *   4. 提示裡的動態文字一律轉義（模型名來自上游，不能當 HTML 拼進去）。
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
  classList: {add(){}, remove(){}, contains(){ return false; }},
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

// 一條真實的直接匹配行：deepseek-v4.1-flash 的三檔價就是它唯一的檔。
const direct = {
  model: 'deepseek-v4.1-flash', cost_cny: 0.001234, cost_band: null,
  cost_source: '09bbbab71283', cost_source_at: 1790908275, cost_backfilled: false,
  cost_rates: {input_cache_hit: 0.02185, input_cache_miss: 0.02185, output: 0.63},
  cost_unit: 1000000, cost_currency: 'USD', cost_usd_cny: 7.1,
  cost_or_id: 'deepseek/deepseek-v4.1-flash', cost_via: 'direct',
  cost_inherited_from: null, cost_override_from: null, cost_band_note: null,
  cost_via_derived: false,
};

// 1. 直接匹配：說清策略、三檔價、匯率，且不冒充人工對映。
{
  const text = api.costTitle(direct);
  assert.ok(text.includes('09bbbab71283'), '策略 id 要在: ' + text);
  assert.ok(text.includes('0.02185') && text.includes('0.63'),
            '三檔單價要在: ' + text);
  assert.ok(text.includes('輸入（快取未命中）') && text.includes('輸入（快取命中）')
            && text.includes('輸出'), '三檔要分別標明: ' + text);
  assert.ok(text.includes('7.10'), '匯率要在: ' + text);
  assert.ok(text.includes('直接匹配'), '要說明是直接匹配: ' + text);
  assert.ok(!text.includes('人工對映'), '直接匹配不得寫成人工對映: ' + text);
  assert.ok(text.includes('deepseek/deepseek-v4.1-flash'), 'or_id 要在: ' + text);
}

// 2. 人工對映：給出 原始 hub 名 → or_id 這條鏈，並標出是對映表命中。
{
  const r = Object.assign({}, direct, {
    model: 'deepseek-v3-1-volc', cost_via: 'override',
    cost_override_from: 'deepseek-v3-1-volc', cost_via_derived: false,
    cost_or_id: 'deepseek/deepseek-chat-v3.1',
  });
  const text = api.costTitle(r);
  assert.ok(text.includes('人工對映表'), '要說明來自對映表: ' + text);
  assert.ok(text.includes('deepseek-v3-1-volc → deepseek/deepseek-chat-v3.1'),
            '要給出原始名 → or_id: ' + text);
  assert.ok(!text.includes('推斷'), '記錄下來的對映不該標成推斷: ' + text);
}

// 3. 老策略行沒有 via 欄位，只能按當前對映表推斷 —— 必須照實標出來。
{
  const r = Object.assign({}, direct, {
    model: 'hy4-preview-f', cost_via: 'override',
    cost_override_from: 'hy4-preview-f', cost_via_derived: true,
    cost_or_id: 'tencent/hy4-preview',
  });
  const text = api.costTitle(r);
  assert.ok(text.includes('推斷'), '推斷來的要標註: ' + text);
  assert.ok(text.includes('hy4-preview-f → tencent/hy4-preview'),
            '推斷也要給出對映鏈: ' + text);
}

// 4. 變體繼承：原始名 → 基準名 → or_id，並標明剝掉的字尾。
{
  const r = Object.assign({}, direct, {
    model: 'deepseek-r1-0528-lkeap', cost_via: 'variant',
    cost_inherited_from: 'deepseek-r1-0528',
    cost_or_id: 'deepseek/deepseek-r1-0528',
  });
  const text = api.costTitle(r);
  assert.ok(text.includes('變體字尾繼承'), '要說明是變體繼承: ' + text);
  assert.ok(text.includes('deepseek-r1-0528-lkeap → deepseek-r1-0528（基準） → deepseek/deepseek-r1-0528'),
            '三段鏈要在: ' + text);
  assert.ok(text.includes('剝掉的字尾：-lkeap'), '剝掉的字尾要在: ' + text);
  assert.equal(api.costVariantSuffix(r), '-lkeap');
  // 基準名不是字首時不能瞎截。
  assert.equal(api.costVariantSuffix({model: 'x-lkeap', cost_inherited_from: 'other'}), '');
}

// 5. 條件檔位：顯示的是這一行實際落的那一檔的價，不是基準價。
{
  const r = Object.assign({}, direct, {
    model: 'hy4-preview-f', cost_band: 1,
    cost_band_note: '每天 16:00–24:00 UTC',
    cost_rates: {input_cache_hit: 0.0378, input_cache_miss: 0.7506, output: 2.2509},
  });
  const text = api.costTitle(r);
  assert.ok(text.includes('第 2 檔'), '檔位序號要在: ' + text);
  assert.ok(text.includes('每天 16:00–24:00 UTC'), '檔位條件要在: ' + text);
  assert.ok(text.includes('0.7506') && text.includes('2.2509'),
            '要顯示該檔的三檔價: ' + text);
  assert.ok(!text.includes('0.02185'), '不得混進別的檔的價: ' + text);
}

// 6. 補算與出廠快照兩條兜底說明還在。
{
  const backfilled = api.costTitle(Object.assign({}, direct, {cost_backfilled: true}));
  assert.ok(backfilled.includes('補算'), '補算標記要在: ' + backfilled);
  const builtin = api.costTitle(Object.assign({}, direct, {cost_source: 'builtin',
    cost_source_at: null}));
  assert.ok(builtin.includes('出廠快照'), '出廠快照依據要在: ' + builtin);
}

// 7. 未定價：照實說，不給 0、不給空白。
{
  const r = Object.assign({}, direct, {cost_cny: null, cost_rates: null,
    cost_unit: null, cost_currency: null, cost_usd_cny: null, cost_or_id: null,
    cost_via: null});
  const text = api.costTitle(r);
  assert.equal(text, '按 OpenRouter 公佈的模型價折算的等價 token 花費\n該模型暫無定價資料',
               '未定價只該有一句說明: ' + text);
  assert.ok(api.costTipHtml(r).includes('該模型暫無定價資料'), '氣泡也要說這句');
  assert.ok(!api.costTipHtml(r).includes('0.00'), '未定價不得顯示 0');
  assert.deepEqual(api.costTipModel(r).rates, [], '未定價沒有三檔價可言');
}

// 8. 快取命中價沒公佈時，說明按未命中價計（計價口徑的兜底）。
{
  const r = Object.assign({}, direct, {
    cost_rates: {input_cache_hit: null, input_cache_miss: 0.3, output: 1.2}});
  const text = api.costTitle(r);
  assert.ok(text.includes('未公佈，按未命中價計'), '要說明兜底口徑: ' + text);
}

// 9. 動態文字一律轉義：模型名來自上游，不能當 HTML 拼進氣泡。
{
  const r = Object.assign({}, direct, {model: '<img src=x onerror=alert(1)>',
    cost_or_id: '<b>or</b>'});
  const out = api.costTipHtml(r);
  assert.ok(!out.includes('<img') && !out.includes('<b>'),
            'HTML 必須被轉義: ' + out);
  assert.ok(out.includes('&lt;b&gt;or&lt;/b&gt;'), '轉義後的文字要還在: ' + out);
}

// 10. 原始碼級：最近請求那一列掛的是 data-cost-key（自繪氣泡），不是原生 title。
{
  const cell = html.match(/<td data-label="OpenRouter 價估算"[^>]*>/g) || [];
  const recent = cell.find(tag => tag.includes('data-cost-key'));
  assert.ok(recent, '最近請求的價估算列要有 data-cost-key: ' + cell.join(' | '));
  assert.ok(!recent.includes('title='), '自繪氣泡不應再掛原生 title: ' + recent);
  assert.ok(/\.cost-cell\{cursor:help\}/.test(html), '懸停列要有手型提示樣式');
}

console.log('pricing tooltip: all checks passed');
