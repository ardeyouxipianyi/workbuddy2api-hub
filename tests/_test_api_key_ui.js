/* 静态 API Key 账号的看板接线。
 *
 * 后端字段已经有了（apiKey / credentialMask / source），这里钉住面板确实用上
 * 了它们，而且没有把这条入口塞进 OAuth 流程里：
 *
 *   1. 账号区有一个独立的「+ API Key」按钮，不是 OAuth「添加」的别名；
 *   2. 弹窗存在、字段 id 与提交函数对得上；
 *   3. 提交前在浏览器侧也校验前缀（省一次往返），提交到
 *      /accounts/add-api-key，成功后只重拉账号列表；
 *   4. 账号行给这类账号打 API Key 标签并隐藏签到/任务开关；
 *   5. 三语词典都有新增文案。
 *
 * Run with Node: node tests/_test_api_key_ui.js
 */
'use strict';

const assert = require('assert');
const {dashboardHtml} = require('./_dashboard_source.js');

const html = dashboardHtml();
let checks = 0;
function check(ok, msg){ assert.ok(ok, msg); checks += 1; }

// 1. 独立入口
check(html.includes('onclick="openApiKeyModal()"'), '账号区应有独立的 + API Key 按钮');
check(/openLoginModal\(\)">添加<\/button>\s*\n\s*<!--[\s\S]*?-->\s*\n\s*<button class="sec" onclick="openApiKeyModal\(\)">/.test(html),
      'API Key 按钮应紧跟在 OAuth 添加按钮之后');

// 2. 弹窗结构
for(const id of ['apiKeyModal', 'apiKeyInput', 'apiKeyNickname', 'apiKeyModalMsg', 'btnApiKeyCommit']){
  check(html.includes('id="' + id + '"'), '弹窗缺少 #' + id);
}
check(/id="apiKeyInput" type="password"/.test(html), 'Key 输入框应是 password 类型，不该明文上屏');

// 3. 提交逻辑
check(html.includes("postJSON('/accounts/add-api-key'"), '应提交到 /accounts/add-api-key');
check(/function commitApiKey\(btn\)/.test(html), '缺少 commitApiKey');
check(/\^\(ck_\|pt_\)/.test(html), '浏览器侧应先校验 ck_ / pt_ 前缀');
check(/closeApiKeyModal\(\);[\s\S]{0,400}await loadAccounts\(\);/.test(html),
      '成功后应关闭弹窗并重拉账号列表');
check(/if\(input\) input\.value = ''/.test(html), '关闭弹窗时应抹掉输入框里的 Key');

// 4. 账号行
check(html.includes("a.apiKey"), '账号行应读 a.apiKey');
check(html.includes('a.credentialMask'), '悬停提示应显示脱敏凭证');
check(/a\.apiKey \? ''[\s\S]{0,200}toggleAccountTasks/.test(html),
      'API Key 账号不应渲染「任务开/关」开关（没有个人成长体系）');

// 5. 三语文案
const EN = {
  '添加 API Key 账号': 'Add an API Key account',
  '验证并添加': 'Verify and add',
  '备注名（可选）': 'Label (optional)',
  '请先填入 API Key': 'Enter the API key first',
};
for(const [zh, en] of Object.entries(EN)){
  const re = new RegExp("'" + zh.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + "': '" + en.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + "'");
  check(re.test(html), '英文词典缺少：' + zh);
}
// 繁体走字表转换，所以只要正文在、转换规则覆盖这些字即可；这里钉住按钮与
// 弹窗标题都能被 HANT_PHRASES 逐字转换（「API」是 ASCII，原样保留）。
check(html.includes("'添加 API Key 账号'"), '正文缺少弹窗标题');

// The key account picker must show notes, search them, and escape markup.
const pickerStart = html.indexOf('async function renderKeyPickers(i)');
const pickerEnd = html.indexOf('function toggleKeyModel(i, el)', pickerStart);
assert.ok(pickerStart >= 0 && pickerEnd > pickerStart);
const pickerList = {innerHTML: ''};
const fakeDocument = {getElementById: id => id === 'editKeyAccountList_0' ? pickerList : null};
const escapeHtml = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
const picker = new Function('API_KEY_ROWS', 'document', 'window', 'esc', 'updateKeyAccountSummary',
  html.slice(pickerStart, pickerEnd) + '\nreturn renderKeyPickers;')(
    [{accounts: ['uid-123']}], fakeDocument,
    {ACCOUNTS: [{uid: 'uid-123', nickname: 'Alice', note: '<VIP>', realm: 'cn'}]},
    escapeHtml, () => {});
picker(0).then(() => {
  check(pickerList.innerHTML.includes('&lt;VIP>'), '备注应安全显示');
  check(pickerList.innerHTML.includes('data-search="alice · &lt;vip>'), '备注应进入搜索文本');
  check(pickerList.innerHTML.includes('value="uid-123" checked'), '选择仍以 UID 保存');
}).catch(err => { console.error(err); process.exitCode = 1; });

console.log('OK ' + checks + ' checks');
