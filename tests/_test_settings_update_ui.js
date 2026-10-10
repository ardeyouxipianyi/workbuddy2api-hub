/* 设置页「更新」卡片的契约：只显示结论、只检查，不安装。
 *
 * 这一片是纯前端消费：后端 GET /updates 与 POST /updates/check 已经落地，
 * 面板负责把结论画出来、把两个动作接上去。这里钉住五件事：
 *
 *   1. 三个字段（当前版本 / 最新版本 / 状态）真的被填上，且状态随结论变化；
 *   2. 「立即检查更新」打到 POST /updates/check；
 *   3. **200 不等于成功**：响应体里 ok=false 或 last_error 非空时，卡片与提示
 *      都必须报失败，不能对着一次失败的检查说「已是最新版本」；
 *   4. 「每天自动检查更新」走既有 /settings/save 契约，不用浏览器本地存储；
 *   5. **不下载、不安装、不重启**：安装按钮固定禁用，页面只访问自己网关的
 *      相对路径——浏览器从不直接接触 GitHub，所以也就不存在任何凭证依赖。
 *
 * Run with Node: node tests/_test_settings_update_ui.js
 */
'use strict';

const assert = require('assert');
const {dashboardScript} = require('./_dashboard_source.js');

const script = dashboardScript();

// One shared fake DOM for every dashboard suite: tests/_dom_stub.js.
const dom = require('./_dom_stub.js');
const element = id => dom.byId(id);

/* 每一次请求都记下来：这一片最重要的一条断言就是「面板只跟自己网关说话」。 */
const calls = [];
let updatesPayload = {};
let updatesStatus = 200;
let saveReply = {ok: true};

const SETTINGS = {
  panel_password_is_default: false,
  api_keys: [],
  deleted_api_keys: [],
  limits: {},
  pricing_enabled: false,
  pricing_refresh_minutes: 5,
  auto_switch_product: false,
  daily_chat_web: false,
  local_web_tools: false,
  update_check_enabled: true,
  version: '1.6.17',
  accounts_dir: '/data/accounts',
  usage_dir: '/data/usage',
  settings_file: '/data/settings.json',
};

dom.installDom({
  fetch: (url, options) => {
    const target = String(url);
    calls.push({url: target, method: (options && options.method) || 'GET',
                body: options && options.body});
    if(target === '/settings') {
      return Promise.resolve({status: 200, ok: true,
        json: () => Promise.resolve(SETTINGS), text: () => Promise.resolve('')});
    }
    if(target === '/updates') {
      return Promise.resolve({status: updatesStatus, ok: updatesStatus === 200,
        json: () => Promise.resolve(updatesPayload), text: () => Promise.resolve('')});
    }
    if(target === '/updates/check') {
      return Promise.resolve({status: 200, ok: true,
        json: () => Promise.resolve(updatesPayload),
        text: () => Promise.resolve(JSON.stringify(updatesPayload))});
    }
    if(target === '/settings/save') {
      return Promise.resolve({status: 200, ok: true,
        json: () => Promise.resolve(saveReply),
        text: () => Promise.resolve(JSON.stringify(saveReply))});
    }
    return Promise.resolve({status: 200, ok: true,
      json: () => Promise.resolve({}), text: () => Promise.resolve('{}')});
  },
});

const toasts = [];
let api;
const realLog = console.log;
console.log = () => {};
try {
  api = new Function(script + `
    toast = function(msg, kind){ global.__toasts.push([msg, kind]); };
    return {
      loadSettings: loadSettings,
      loadUpdateStatus: loadUpdateStatus,
      checkUpdateNow: checkUpdateNow,
      saveUpdateCheckEnabled: saveUpdateCheckEnabled,
      applyUpdateStatus: applyUpdateStatus,
    };`)();
} catch(e) {
  console.log = realLog;
  console.error('脚本求值失败:', e.message);
  process.exit(1);
}
global.__toasts = toasts;

const settled = () => new Promise(resolve => process.nextTick(resolve));
const urls = () => calls.map(c => c.method + ' ' + c.url);
const posts = () => calls.filter(c => c.method === 'POST');

(async () => {
  let checks = 0;
  const check = (label, cond, extra) => {
    checks += 1;
    assert.ok(cond, label + (extra !== undefined ? '  [' + extra + ']' : ''));
  };

  // ---- 1. 打开设置页：显示当前版本、最新版本与状态 ----
  updatesPayload = {
    current_version: '1.6.17', latest_version: '1.6.18', update_available: true,
    enabled: true, checking: false,
    last_attempt: '2026-10-09 12:00:00', last_success: '2026-10-09 12:00:00',
    last_error: '', release_url: 'https://github.com/ardeyouxipianyi/workbuddy2api-hub/releases/tag/v1.6.18',
    published_at: '2026-10-09T00:00:00Z',
  };
  calls.length = 0;
  await api.loadSettings(true);
  await settled();

  check('当前版本来自 /updates', element('setUpdateCurrent').textContent === '1.6.17',
        element('setUpdateCurrent').textContent);
  check('最新版本来自 /updates', element('setUpdateLatest').textContent === '1.6.18',
        element('setUpdateLatest').textContent);
  check('状态显示「发现新版本」', element('setUpdateState').textContent === '(发现新版本)',
        element('setUpdateState').textContent);
  check('上次检查时间被显示', element('setUpdateLastCheck').textContent === '2026-10-09 12:00:00',
        element('setUpdateLastCheck').textContent);
  check('自动检查开关跟随后端值', element('setUpdateCheckEnabled').checked === true);
  check('打开设置页只读 /updates，不会自己发起检查',
        urls().indexOf('GET /updates') !== -1 && posts().length === 0, urls().join(', '));

  // ---- 2. 已是最新 / 检查失败两种状态 ----
  api.applyUpdateStatus({current_version: '1.6.18', latest_version: '1.6.18',
                         update_available: false, last_error: ''});
  check('同版本显示「已是最新」', element('setUpdateState').textContent === '(已是最新)',
        element('setUpdateState').textContent);
  api.applyUpdateStatus({current_version: '1.6.17', latest_version: null,
                         update_available: false, last_error: ''});
  check('没查过显示「尚未检查」', element('setUpdateState').textContent === '(尚未检查)',
        element('setUpdateState').textContent);
  api.applyUpdateStatus({current_version: '1.6.17', latest_version: null,
                         update_available: false, last_error: 'HTTP 403'});
  check('后端报错显示「检查失败」', element('setUpdateState').textContent === '(检查失败)',
        element('setUpdateState').textContent);
  check('失败原因原样显示给用户', element('setUpdateError').textContent === '上次检查失败：HTTP 403',
        element('setUpdateError').textContent);

  // ---- 3. 「立即检查更新」打到 POST /updates/check ----
  updatesPayload = {
    current_version: '1.6.17', latest_version: '1.6.19', update_available: true,
    enabled: false, last_error: '', last_attempt: '2026-10-09 13:00:00',
  };
  calls.length = 0;
  const btn = {disabled: false};
  await api.checkUpdateNow(btn);
  await settled();
  check('立即检查走 POST /updates/check', posts().length === 1 && posts()[0].url === '/updates/check',
        urls().join(', '));
  check('检查结果立刻显示出来', element('setUpdateLatest').textContent === '1.6.19',
        element('setUpdateLatest').textContent);
  check('检查结果会更新状态行', element('setUpdateState').textContent === '(发现新版本)',
        element('setUpdateState').textContent);
  check('按钮在检查结束后恢复可用', btn.disabled === false);
  check('自动检查关着也能手动检查（开关未被当作前置条件）',
        posts().filter(c => c.url === '/updates/check').length === 1, urls().join(', '));

  // ---- 4. 200 不等于成功：ok=false / last_error 非空都算检查失败 ----
  // 后端把「检查没跑起来」和「这一次查询失败」都放在 200 的响应体里回来。
  // 按钮必须报失败——以前它对着一次失败的检查说「已是最新版本」。
  toasts.length = 0;
  updatesPayload = {
    ok: true, current_version: '1.6.17', latest_version: null,
    update_available: false, enabled: true, last_error: 'HTTP 403',
    last_attempt: '2026-10-09 14:00:00',
  };
  const failBtn = {disabled: false};
  await api.checkUpdateNow(failBtn);
  await settled();
  check('200 + last_error 显示「检查失败」',
        element('setUpdateState').textContent === '(检查失败)',
        element('setUpdateState').textContent);
  check('200 + last_error 把原因写出来',
        element('setUpdateError').textContent === '上次检查失败：HTTP 403',
        element('setUpdateError').textContent);
  check('200 + last_error 不报「已是最新」',
        !toasts.some(([m]) => String(m).indexOf('已是最新') !== -1),
        JSON.stringify(toasts));
  check('200 + last_error 的提示是失败态',
        toasts.length === 1 && toasts[0][1] === 'bad' &&
        String(toasts[0][0]).indexOf('检查更新失败') === 0, JSON.stringify(toasts));
  check('失败之后按钮仍然恢复可用', failBtn.disabled === false);

  toasts.length = 0;
  updatesPayload = {ok: false, msg: '更新检查未运行'};
  await api.checkUpdateNow({disabled: false});
  await settled();
  check('200 + ok=false 显示「检查失败」',
        element('setUpdateState').textContent === '(检查失败)',
        element('setUpdateState').textContent);
  check('200 + ok=false 用后端的 msg 说明原因',
        element('setUpdateError').textContent === '上次检查失败：更新检查未运行',
        element('setUpdateError').textContent);
  check('200 + ok=false 不报「已是最新」',
        !toasts.some(([m]) => String(m).indexOf('已是最新') !== -1),
        JSON.stringify(toasts));
  check('200 + ok=false 的提示是失败态',
        toasts.length === 1 && toasts[0][1] === 'bad', JSON.stringify(toasts));

  toasts.length = 0;
  updatesPayload = {
    ok: true, current_version: '1.6.17', latest_version: '1.6.17',
    update_available: false, enabled: true, last_error: '',
  };
  await api.checkUpdateNow({disabled: false});
  await settled();
  check('真的没有新版本时才报「已是最新」',
        toasts.length === 1 && toasts[0][1] === 'ok' &&
        String(toasts[0][0]).indexOf('已是最新') !== -1, JSON.stringify(toasts));

  // ---- 5. 开关走既有 settings save 契约 ----
  calls.length = 0;
  element('setUpdateCheckEnabled').checked = true;
  const saveBtn = {disabled: false};
  await api.saveUpdateCheckEnabled(saveBtn);
  await settled();
  const save = posts().filter(c => c.url === '/settings/save');
  check('开关保存走 POST /settings/save', save.length === 1, urls().join(', '));
  check('提交的是 update_check_enabled 布尔值',
        save.length === 1 && JSON.parse(save[0].body).update_check_enabled === true,
        save.length ? save[0].body : '(no save)');
  check('没有走浏览器本地存储（偏好由服务端保存）',
        !calls.some(c => c.url.indexOf('localStorage') !== -1), urls().join(', '));

  // ---- 6. 不下载、不安装、不重启 ----
  const installBtn = element('setUpdateInstallBtn');
  check('安装按钮固定禁用（本版没有安装路径）', installBtn.disabled === true);
  check('安装按钮说明为什么不可用',
        String(installBtn.title).indexOf('尚未提供') !== -1, installBtn.title);
  check('面板从不直接访问 GitHub',
        !calls.some(c => /github\.com|api\.github/i.test(c.url)), urls().join(', '));
  check('面板只访问自己网关的相对路径',
        calls.every(c => c.url.charAt(0) === '/'), urls().join(', '));
  check('没有任何安装/下载/重启端点被调用',
        !calls.some(c => /install|download|restart|reload/i.test(c.url)), urls().join(', '));

  // ---- 7. 读取失败不拖垮设置页的其它区块 ----
  updatesStatus = 500;
  calls.length = 0;
  await api.loadSettings(true);
  await settled();
  check('读不到更新状态时只标注这一块', element('setUpdateState').textContent === '(读取失败)',
        element('setUpdateState').textContent);
  check('其它区块照常填上', element('setVersion').textContent === '1.6.17',
        element('setVersion').textContent);
  check('没有报「读取设置失败」',
        toasts.filter(([m]) => String(m).indexOf('读取设置失败') === 0).length === 0,
        JSON.stringify(toasts));
  updatesStatus = 200;

  console.log = realLog;
  realLog('settings update UI assertions passed (' + checks + ' checks)');
})().catch(e => {
  console.log = realLog;
  console.error(e && e.stack || e);
  process.exit(1);
});
