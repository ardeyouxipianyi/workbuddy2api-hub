/* 看板刷新的合并语义：撞上正在飞的那一轮时排队补跑，而不是把这次刷新丢掉。
 *
 * 回归背景：刷新函数早先是 `if(REFRESH_BUSY) return;`，切区域/切账号那一下如果
 * 正好撞上 5 秒轮询在飞，这次刷新就没有下文了，只能干等下一个周期——用户看到的是
 * "点了要过几秒才变"。这里把 dashboard.html 里的这段逻辑原样抽出来，在 Node 里
 * 用一个可控的 refreshInner 跑一遍，钉住三条：不丢、只补跑一次、代次必须递增
 * （在飞的那轮据此丢弃过期结果，不会把旧区域的数字画上去）。
 *
 * Run with Node: node tests/_test_dashboard_refresh.js
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard.html'), 'utf8');

const start = html.indexOf('let REFRESH_RUNNING');
const end = html.indexOf('async function refreshInner');
assert.ok(start > 0 && end > start, 'dashboard.html 里找不到刷新逻辑，测试需要更新');

const source = html.slice(start, end);

function makeHarness() {
  const calls = [];
  let release;
  const inner = (gen) => {
    calls.push(gen);
    return new Promise((resolve) => { release = resolve; });
  };
  const factory = new Function('refreshInner', source + '\nreturn refresh;');
  const refresh = factory(inner);
  return {
    refresh,
    calls,
    releaseAll: async () => {
      // 每次挂起的调用都放行，让排队的补跑得以执行
      for (let i = 0; i < 5; i++) {
        const r = release;
        release = null;
        if (r) r();
        await new Promise((res) => setImmediate(res));
      }
    },
  };
}

(async () => {
  // 1) 在飞时再来一次：必须排队补跑，而不是丢弃
  let h = makeHarness();
  const first = h.refresh();          // 起飞并挂住
  await new Promise((res) => setImmediate(res));
  const second = h.refresh();         // 撞上在飞的那轮
  await h.releaseAll();
  await Promise.all([first, second]);
  assert.strictEqual(h.calls.length, 2,
    '撞上在飞的一轮时应补跑一次，实际跑了 ' + h.calls.length + ' 次');
  assert.notStrictEqual(h.calls[0], h.calls[1],
    '补跑必须带上新的代次，否则过期结果无法被识别');

  // 2) 连着撞多次也只补跑一次（防请求堆积）
  h = makeHarness();
  const runs = [h.refresh()];
  await new Promise((res) => setImmediate(res));
  runs.push(h.refresh(), h.refresh(), h.refresh());
  await h.releaseAll();
  await Promise.all(runs);
  assert.strictEqual(h.calls.length, 2,
    '连续多次合并成一次补跑，实际 ' + h.calls.length + ' 次');

  // 3) 空闲时的普通刷新：来一次跑一次，不叠加
  h = makeHarness();
  for (let i = 0; i < 3; i++) {
    const p = h.refresh();
    await h.releaseAll();
    await p;
  }
  assert.strictEqual(h.calls.length, 3, '空闲刷新应逐次执行');

  console.log('dashboard refresh coalescing assertions passed');
})().catch((err) => {
  console.error(err.message);
  process.exit(1);
});
