/* 设置页「更新」卡片的契约：显示检查结果，安装必须由用户手动确认。
 *
 * 这一片是纯前端消费：后端 GET /updates、POST /updates/check 与
 * POST /updates/install 已经落地，
 * 面板负责把结论画出来、把两个动作接上去。这里钉住五件事：
 *
 *   1. 三个字段（当前版本 / 最新版本 / 状态）真的被填上，且状态随结论变化；
 *   2. 「立即检查更新」打到 POST /updates/check；
 *   3. **200 不等于成功**：响应体里 ok=false 或 last_error 非空时，卡片与提示
 *      都必须报失败，不能对着一次失败的检查说「已是最新版本」；
 *   4. 「每天自动检查更新」走既有 /settings/save 契约，不用浏览器本地存储；
 *   5. 安装按钮只在已配置更新服务且发现稳定新版本时可用；取消确认不得提交安装；
 *      页面只访问自己网关的相对路径，浏览器从不直接接触 GitHub。
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
let saveStatus = 200;
let confirmCalls = 0;

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
  confirm: () => { confirmCalls += 1; return false; },
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
      return Promise.resolve({status: saveStatus, ok: saveStatus === 200,
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
      installUpdateNow: installUpdateNow,
      saveUpdateCheckEnabled: saveUpdateCheckEnabled,
      applyUpdateStatus: applyUpdateStatus,
      saveDailyChatWebRetry: saveDailyChatWebRetry,
      saveDailyChatWebTuning: saveDailyChatWebTuning,
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
    latest_tag: 'v1.6.18', install_enabled: true,
    installation: {state: 'idle', version: '', message: ''},
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

  // ---- 6. 安装入口受更新状态与人工确认控制 ----
  const installBtn = element('setUpdateInstallBtn');
  updatesPayload = {
    current_version: '1.6.17', latest_version: '1.6.18', update_available: true,
    latest_tag: 'v1.6.18', release_url: 'https://github.com/ardeyouxipianyi/workbuddy2api-hub/releases/tag/v1.6.18',
    install_enabled: true, last_error: '', installation: {state: 'idle'},
  };
  api.applyUpdateStatus(updatesPayload);
  check('有可用稳定更新且更新服务已安装时按钮可用', installBtn.disabled === false);
  api.applyUpdateStatus({...updatesPayload, install_enabled: false});
  check('没有安装服务时按钮保持禁用', installBtn.disabled === true);
  api.applyUpdateStatus({...updatesPayload, installation: {state: 'applying'}});
  check('安装过程中按钮保持禁用', installBtn.disabled === true);
  api.applyUpdateStatus(updatesPayload);
  updatesPayload = {
    current_version: '1.6.18', latest_version: '1.6.18', update_available: false,
    install_enabled: true, last_error: '', installation: {state: 'idle'},
  };
  api.applyUpdateStatus(updatesPayload);
  check('没有新版本时安装按钮禁用', installBtn.disabled === true);
  updatesPayload = {
    current_version: '1.6.17', latest_version: '1.6.18', update_available: true,
    latest_tag: 'v1.6.18', release_url: 'https://github.com/ardeyouxipianyi/workbuddy2api-hub/releases/tag/v1.6.18',
    install_enabled: true, last_error: '', installation: {state: 'idle'},
  };
  api.applyUpdateStatus(updatesPayload);
  calls.length = 0;
  const confirmsBeforeInstall = confirmCalls;
  await api.installUpdateNow(installBtn); // DOM stub declines the confirmation.
  await settled();
  check('安装点击先经过人工确认', confirmCalls === confirmsBeforeInstall + 1);
  check('取消确认不会提交安装请求',
        posts().filter(c => c.url === '/updates/install').length === 0, urls().join(', '));
  check('面板从不直接访问 GitHub',
        !calls.some(c => /github\.com|api\.github/i.test(c.url)), urls().join(', '));
  check('面板只访问自己网关的相对路径',
        calls.every(c => c.url.charAt(0) === '/'), urls().join(', '));

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

  // ---- 8. 每日活跃网页通道的重试开关、尝试次数与首轮等待 ----
  // 开关自动保存（onchange），失败要把勾拨回去：否则界面显示一个没落盘的状态。
  calls.length = 0;
  toasts.length = 0;
  element('setDailyChatWebRetry').checked = false;
  await api.saveDailyChatWebRetry();
  await settled();
  let retrySave = posts().filter(c => c.url === '/settings/save');
  check('重试开关保存走 POST /settings/save', retrySave.length === 1, urls().join(', '));
  check('提交的是 daily_chat_web_retry 布尔值',
        retrySave.length === 1 && JSON.parse(retrySave[0].body).daily_chat_web_retry === false,
        retrySave.length ? retrySave[0].body : '(no save)');
  check('关掉重试的提示是成功态',
        toasts.length === 1 && toasts[0][1] === 'ok', JSON.stringify(toasts));

  // 存失败时把开关拨回原位（checked 恢复成 true）。
  saveStatus = 500;
  calls.length = 0;
  toasts.length = 0;
  element('setDailyChatWebRetry').checked = false;
  await api.saveDailyChatWebRetry();
  await settled();
  saveStatus = 200;
  check('保存失败时开关拨回原位', element('setDailyChatWebRetry').checked === true);
  check('保存失败给出失败提示',
        toasts.length === 1 && toasts[0][1] === 'bad', JSON.stringify(toasts));

  // 次数与首轮等待一次提交；空值/小数/非数字就地拦下，不发请求。
  calls.length = 0;
  toasts.length = 0;
  element('setDailyChatWebAttempts').value = '4';
  element('setDailyChatWebTimeout').value = '45';
  await api.saveDailyChatWebTuning({disabled: false});
  await settled();
  let tuningSave = posts().filter(c => c.url === '/settings/save');
  check('次数与首轮等待一起走 POST /settings/save', tuningSave.length === 1, urls().join(', '));
  check('提交的是整数次数与整数秒',
        tuningSave.length === 1 &&
        JSON.parse(tuningSave[0].body).daily_chat_web_attempts === 4 &&
        JSON.parse(tuningSave[0].body).daily_chat_web_timeout === 45,
        tuningSave.length ? tuningSave[0].body : '(no save)');

  calls.length = 0;
  toasts.length = 0;
  element('setDailyChatWebAttempts').value = '4';
  element('setDailyChatWebTimeout').value = '';
  await api.saveDailyChatWebTuning({disabled: false});
  await settled();
  check('首轮等待空值不发请求', posts().length === 0, urls().join(', '));
  check('首轮等待空值给出失败提示',
        toasts.length === 1 && toasts[0][1] === 'bad', JSON.stringify(toasts));

  calls.length = 0;
  toasts.length = 0;
  element('setDailyChatWebAttempts').value = '';
  element('setDailyChatWebTimeout').value = '120';
  await api.saveDailyChatWebTuning({disabled: false});
  await settled();
  check('次数空值不发请求', posts().length === 0, urls().join(', '));
  check('次数空值给出失败提示',
        toasts.length === 1 && toasts[0][1] === 'bad', JSON.stringify(toasts));

  console.log = realLog;
  realLog('settings update UI assertions passed (' + checks + ' checks)');
})().catch(e => {
  console.log = realLog;
  console.error(e && e.stack || e);
  process.exit(1);
});
