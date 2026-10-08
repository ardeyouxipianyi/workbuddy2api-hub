/* Drive the dashboard's model table with a probed output clamp (M5 E2).
 *
 * scripts/probe_max_tokens.py writes accounts/output_probes.json; the gateway
 * annotates /v1/models entries with output_clamp; the dashboard must show the
 * measured value with a "钳制 N×" note instead of the unprobed spec value.
 *
 * The dashboard itself is loaded from the file next to this test, so the
 * assertions run against the shipped code, not a copy of it.
 *
 * Requires node (no other dependency).
 *
 *   node _test_model_probes.js
 */
const assert = require('assert');
const {dashboardScript} = require('./_dashboard_source.js');

const code = dashboardScript();

// One shared fake DOM for every dashboard suite: tests/_dom_stub.js.
const dom = require('./_dom_stub.js');
const tbody = dom.makeElement('tbody');
dom.installDom({
  querySelector: {'#modelsTable tbody': tbody},
  fetch: () => Promise.resolve({ok: true, status: 200, json: () => Promise.resolve({})}),
});

let api;
try {
  api = new Function(code + `
    ; return { renderAvailableModels };`)();
} catch (e) {
  console.log('LOAD ERROR:', e.message);
  process.exit(1);
}

function render(models){
  tbody.innerHTML = '';
  global.MODELS_DATA = models;
  api.renderAvailableModels();
  return tbody.innerHTML;
}

// 1. The probe measured less than the claimed spec -> warn with the ratio.
let out = render([{ id: 'glm-5.2', context_length: 1000000,
                    max_output_tokens: 131072, output_clamp: 32000 }]);
assert.ok(out.includes('钳制'), 'clamped model must show the 钳制 note: ' + out);
assert.ok(out.includes('4.1×'), 'ratio must be claimed/measured (131072/32000 = 4.1×): ' + out);
assert.ok(out.includes('32K'), 'measured value must be shown: ' + out);
assert.ok(out.includes('⚠'), 'clamped model must carry the warning marker: ' + out);
assert.ok(out.includes('声称 131K'), 'tooltip must carry the claimed value: ' + out);

// 2. The probe says the claimed spec holds -> green check, no clamp note.
out = render([{ id: 'm2', context_length: 1000,
                max_output_tokens: 32000, output_clamp: 32000 }]);
assert.ok(!out.includes('钳制'), 'a non-clamped probe must not warn: ' + out);
assert.ok(out.includes('✓'), 'a non-clamped probe must show the OK marker: ' + out);

// 3. No probe at all -> the plain spec value stays (default behaviour).
out = render([{ id: 'm3', context_length: 1000, max_output_tokens: 32000 }]);
assert.ok(!out.includes('钳制') && !out.includes('⚠'),
          'an unprobed model keeps the plain value: ' + out);
assert.ok(out.includes('32K'), 'an unprobed model still shows its spec value: ' + out);

// 4. Probed but no spec value -> measured value labelled as a measured cap.
out = render([{ id: 'm4', context_length: 1000, output_clamp: 48000 }]);
assert.ok(out.includes('48K'), 'measured-only model must show the measured value: ' + out);
assert.ok(out.includes('实测上限'), 'measured-only model must label the measured cap: ' + out);

console.log('model probe dashboard assertions passed');
