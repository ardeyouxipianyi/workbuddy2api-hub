/* The colour tuner must not save an unreadable theme.
 *
 * Regression: the tuner showed a warning at low contrast but still applied and
 * persisted the value. A custom warn colour at 1.38:1 was therefore accepted;
 * the next page load had no visible warning because the tuner is hidden by
 * default. This executes the helpers from the shipped dashboard so the test
 * covers the browser implementation, including rgba surfaces.
 *
 * Run with Node: node tests/_test_dashboard_theme_contrast.js
 */
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard.html'), 'utf8');
const marker = '  var VARS = [';
const start = html.indexOf(marker);
assert.ok(start > 0, 'the colour tuner block moved; update this test marker');
const parseStart = html.indexOf('  var COLOR = (function(){', start);
const parseEnd = html.indexOf('  })();', parseStart);
assert.ok(parseStart > start && parseEnd > parseStart, 'could not locate the colour helper block');
const source = '(function(){\n' + html.slice(parseStart, parseEnd + '  })();'.length) +
  '\nthis.__COLOR = COLOR;\n}).call(this);';

const sandbox = {isFinite, Math, Number, String, RegExp};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(source, sandbox, {filename: 'dashboard-theme-contrast.js'});
const C = sandbox.__COLOR;

assert.strictEqual(C.parseColor('#fff').length, 4, 'short hex should parse');
assert.strictEqual(Array.from(C.parseColor('#ffffff')).join(','), '255,255,255,1',
  'opaque hex should parse');
assert.strictEqual(Array.from(C.parseColor('rgba(0,0,0,.5)', '#ffffff')).join(','),
  '128,128,128,1', 'translucent colours must be blended over the surface');
assert.strictEqual(C.parseColor('not-a-colour'), null, 'invalid colours must be rejected');
assert.strictEqual(C.parseColor('rgba(0,0,0,2)'), null, 'out-of-range alpha must be rejected');

const pass = C.ensureContrast('#1f1e1b', ['#ffffff', '#efebe0'], '#625f57', 4.5);
assert.strictEqual(pass.adjusted, false, 'a passing colour must be kept as-is');
assert.ok(pass.ratio >= 4.5);

const fixed = C.ensureContrast('#fff1d0', ['#ffffff', '#efebe0'], '#625f57', 4.5);
assert.strictEqual(fixed.adjusted, true, 'a low-contrast colour must be adjusted');
assert.ok(fixed.ratio >= 4.5, `adjusted colour is still too low: ${fixed.ratio}`);
assert.match(fixed.value, /^#[0-9a-f]{6}$/i, 'the adjusted value must be colour-input safe');

const invalid = C.ensureContrast('not-a-colour', ['#211f26', '#141218'], '#e6e1e5', 4.5);
assert.strictEqual(invalid.adjusted, true, 'invalid values must not be applied unchanged');
assert.ok(invalid.ratio >= 4.5, 'invalid values must fall back to a readable colour');

const alpha = C.ensureContrast('rgba(255,255,255,0)', ['#ffffff'], '#625f57', 4.5);
assert.strictEqual(alpha.adjusted, true, 'transparent text must not be accepted');
assert.ok(alpha.ratio >= 4.5, 'transparent text should be replaced by a readable value');

console.log('dashboard theme contrast assertions passed');
