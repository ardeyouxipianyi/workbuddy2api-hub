/* The Token time-series tooltip must survive an empty series.
 *
 *   node tests/_test_timeseries_hover_empty.js
 *
 * "今日" before the first request of the day draws an empty chart: the layout
 * reports n = 0 points and series = []. The hover handler still ran, clamped the
 * index with Math.min(n - 1, idx) -> -1 -> Math.max(0, -1) -> 0, and then read
 * series[0].at off undefined: "TypeError: Cannot read properties of undefined
 * (reading 'at')" on every mousemove over the chart. The panel looked broken
 * exactly when a fresh install has no data to show.
 *
 * The check executes the handler the page actually ships: the block assigned to
 * cv.onmousemove is lifted out of dashboard.html by brace matching and called
 * with an empty series, so a refactor that keeps the bug cannot slip past it.
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard.html'), 'utf8');

/** The source between "cv.onmousemove =" and its matching closing brace. */
function hoverSource() {
  const start = html.indexOf('cv.onmousemove');
  if (start < 0) throw new Error('cv.onmousemove handler not found in dashboard.html');
  const eq = html.indexOf('=', start) + 1;
  const open = html.indexOf('{', eq);
  let depth = 0;
  for (let i = open; i < html.length; i++) {
    const ch = html[i];
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) {
        return html.slice(eq + 1, i + 1).trim().replace(/;+$/, '').trim();
      }
    }
  }
  throw new Error('unterminated cv.onmousemove block');
}

/** Run the shipped handler against one fabricated chart state. */
function callHandler(state) {
  const expr = hoverSource();
  const fn = new Function(
    'seriesLayout', 'cv', 'padL', 'gap', 'n', 'series', 'tip', 'step',
    'fmtDate', 'fmtTime', 'fmtCompact', 'window',
    'return (' + expr + ');'
  );
  const handler = fn(
    state.seriesLayout, state.cv, state.padL, state.gap, state.n, state.series,
    state.tip, state.step,
    (v) => 'D' + v, (v) => 'T' + v, (v) => String(v),
    { innerWidth: 1280 }
  );
  handler({ clientX: 40, clientY: 20 });
}

function emptyState() {
  const tip = { style: { opacity: 0, left: '', top: '' }, innerHTML: '' };
  const cv = { getBoundingClientRect: () => ({ left: 0 }) };
  return {
    seriesLayout: { bars: [], series: [], step: 300, metric: 'total_tokens' },
    cv, padL: 0, gap: 10, n: 0, series: [], tip, step: 300,
  };
}

let failed = 0;
const check = (name, fn) => {
  try { fn(); console.log('  ok   ' + name); }
  catch (err) { failed++; console.log('  FAIL ' + name + ': ' + err.message + '\n' + String(err.stack).split('\n').slice(1,4).join('\n')); }
};

console.log('timeseries hover with an empty series');

check('empty series does not throw', () => {
  callHandler(emptyState());
});

check('empty series leaves the tooltip hidden', () => {
  const state = emptyState();
  callHandler(state);
  assert.notStrictEqual(state.tip.style.opacity, 1,
    'tooltip was shown for a chart with no points');
});

check('a populated series still shows the tooltip', () => {
  const state = emptyState();
  state.series = [{ at: 1700000000, requests: 3, total_tokens: 100, errors: 0, credit: 1 }];
  state.n = 1;
  callHandler(state);
  assert.strictEqual(state.tip.style.opacity, 1, 'tooltip did not open with data present');
  assert.ok(String(state.tip.innerHTML).indexOf('1700000000') >= 0,
    'tooltip body did not carry the timestamp: ' + state.tip.innerHTML);
});

if (failed) {
  console.error('\n' + failed + ' check(s) failed');
  process.exit(1);
}
console.log('\nall checks passed');
