/* The agent-config card must send a payload the backend actually accepts.

   Two bugs shipped through a green test suite because nothing exercised the
   real front-end functions:

     1. applyAgent()/restoreAgent() posted {client_id}, while the handler only
        read payload["client"] - every click returned HTTP 400 "client is
        required";
     2. the models list was read from an undeclared identifier
        (AGENTS_MODELS instead of AGENT_MODELS), so the browser raised
        "AGENTS_MODELS is not defined" before the request left the page.

   This suite loads the shipped dashboard.html scripts into a stub DOM, calls
   the real applyAgent()/restoreAgent() and asserts what they post, so a
   renamed field or an undeclared identifier fails here instead of in the
   browser. Run with Node.
*/
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const html = require('./_dashboard_source.js').dashboardHtml();
const script = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)]
  .map(match => match[1]).join('\n');

// ---- stub DOM -------------------------------------------------------------
const values = {
  agentBaseUrl: 'http://127.0.0.1:8788/v1',
  agentKeySelect: '__global',
  agentModelSelect: 'model-a',
};
const elements = new Map();
const element = id => {
  if (!elements.has(id)) {
    elements.set(id, {
      id, innerHTML: '', textContent: '', className: '', dataset: {},
      style: {setProperty(){}},
      classList: {add(){}, remove(){}, toggle(){}, contains(){ return false; }},
      addEventListener(){}, removeEventListener(){}, querySelector(){ return null; },
      querySelectorAll(){ return []; }, appendChild(){}, removeChild(){}, focus(){},
      setAttribute(){}, getAttribute(){ return ''; }, removeAttribute(){},
      contains(){ return false; }, closest(){ return null; },
    });
  }
  const el = elements.get(id);
  if (Object.prototype.hasOwnProperty.call(values, id)) el.value = values[id];
  return el;
};

// 面板用 document 级委托监听复选框，所以假 document 得把监听器存下来，测试才
// 能现场派发一个 change 事件（只认含 model-pick 的那个，别误触发别的委托）。
const documentListeners = {};
const fireDocument = (name, ev) => {
  const handler = (documentListeners[name] || []).find(fn => /model-pick/.test(String(fn)));
  assert.ok(handler, '面板没有注册「.model-pick」的 ' + name + ' 委托监听');
  return handler(ev);
};

global.document = {
  getElementById: element,
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener(name, fn){ (documentListeners[name] || (documentListeners[name] = [])).push(fn); },
  removeEventListener(){},
  createElement: () => element('created'),
  body: element('body'), head: element('head'), documentElement: element('html'),
  readyState: 'complete',
};
global.window = {
  addEventListener(){}, removeEventListener(){},
  location: {href: 'http://127.0.0.1:8788/', search: '', hash: '', origin: 'http://127.0.0.1:8788'},
  matchMedia: () => ({matches: false, addEventListener(){}}),
};
global.localStorage = {getItem(){ return null; }, setItem(){}, removeItem(){}};
global.sessionStorage = global.localStorage;
global.navigator = {userAgent: 'node'};
global.setInterval = () => 0;
global.clearInterval = () => {};
global.setTimeout = () => 0;
global.clearTimeout = () => {};
global.location = global.window.location;
global.alert = () => {};
global.confirm = () => true;
global.console.log = () => {};

// ---- capture every request the page makes ---------------------------------
// 「网关模型清单」的勾选态：一键配置只把勾选中的模型写进客户端。
// configured=false 模拟「服务端还没存过」——老安装升到带勾选这版后的第一屏。
const SELECTED = {models: ['model-a', 'model-b'], configured: true};
// 勾选的默认态读的是 MODELS_DATA；面板里它挂在 window 上，测试的 window 不是真
// 全局，所以先把真全局铺好，免得 loadModels 之外的路径拿到 undefined。
global.MODELS_DATA = [];
const requests = [];
const respond = (url, opts) => {
  const u = String(url);
  const isPost = !!(opts && String(opts.method || '').toUpperCase() === 'POST');
  let payload;
  if (u.includes('/agents/model-selection')) {
    // POST 回声请求体：真服务端会清洗后回一份规范清单，这里让它别把刚发出去的选择改掉。
    payload = isPost && opts.body
      ? {configured: true, selected_models: (JSON.parse(String(opts.body)).models || []).slice()}
      : {configured: SELECTED.configured, selected_models: SELECTED.models.slice()};
  } else if (u.includes('/agents')) {
    payload = {clients: [], keys: [], models: [], gateway: {base_url: 'http://127.0.0.1:8788/v1'}};
  } else {
    payload = {rows: [], total: 0, accounts: [], slots: [], data: [], results: [], byAccount: []};
  }
  return Promise.resolve({
    status: 200, ok: true,
    json: () => Promise.resolve(payload),
    text: () => Promise.resolve(JSON.stringify(payload)),
  });
};
const realFetch = (url, opts) => respond(url, opts);
global.fetch = (url, opts) => {
  requests.push({url: String(url), opts: opts || {}});
  return respond(url, opts);
};

// ---- load the shipped page scripts ---------------------------------------
const api = new Function(script + `
  window.__applyAgent = applyAgent;
  window.__restoreAgent = restoreAgent;
  window.__loadAgents = loadAgents;
  window.__setAgents = function(data, models){ AGENTS_DATA = data; AGENT_MODELS = models; };
  // 直接注入勾选态时三态要一起摆好：configured=false 会让「空集合」被读成
  // 「全都要」，只塞集合会测出一个假绿。
  window.__setSelection = function(list, configured){
    AGENT_MODEL_SELECTION = new Set((list || []).map(String));
    MODEL_SELECTION_CONFIGURED = configured !== false;
    MODEL_SELECTION_SAVED = MODEL_SELECTION_CONFIGURED ? new Set(AGENT_MODEL_SELECTION) : null;
  };
  window.__setModelsData = function(list){ MODELS_DATA = list; };
  return { applyAgent, restoreAgent, loadAgents,
           setAgents: window.__setAgents, setSelection: window.__setSelection,
           setModelsData: window.__setModelsData,
           loadAgentsAvailability, switchMainTab,
           loadAgentModelSelection, applyModelPickAction, isModelPicked,
           syncModelPickBoxes, saveModelSelection,
           selection: function(){ return Array.from(AGENT_MODEL_SELECTION); },
           isConfigured: function(){ return MODEL_SELECTION_CONFIGURED; },
           agentsAvailable: function(){ return AGENTS_AVAILABLE; },
           currentTab: function(){ return currentMainTab; } };`)();

const CLIENTS = [{
  id: 'claude-code', label: 'Claude Code', desc: 'Anthropic CLI',
  protocol: 'anthropic', config_paths: ['/home/u/.claude/settings.json'],
  installed: true, applied: null,
}];
const MODELS = [
  {id: 'model-a', context_window: 131072},
  {id: 'model-b', context_window: 65536},
];

let checks = 0;
const check = (label, cond, extra) => {
  checks += 1;
  assert.ok(cond, label + (extra ? '  [' + extra + ']' : ''));
};

(async () => {
  const seed = () => api.setAgents({
    clients: CLIENTS, keys: [], models: MODELS,
    gateway: {base_url: 'http://127.0.0.1:8788/v1'},
    model_embedding_clients: ['dsh'],
  }, MODELS);
  const select = list => { SELECTED.models = list; api.setSelection(list); };
  seed();
  select(['model-a', 'model-b']);

  // The regression that reached the user: an undeclared identifier inside
  // applyAgent() must not escape as a runtime error.
  requests.length = 0;
  await api.applyAgent('claude-code', null);
  const applyReq = requests.find(r => r.url.indexOf('/agents/apply') !== -1);
  check('applyAgent posts to /agents/apply', !!applyReq,
        JSON.stringify(requests.map(r => r.url)));

  const body = JSON.parse((applyReq && applyReq.opts.body) || '{}');
  check('the payload carries client', body.client === 'claude-code', JSON.stringify(body));
  check('the payload carries client_id too', body.client_id === 'claude-code', JSON.stringify(body));
  check('the payload carries the gateway base_url',
        body.base_url === 'http://127.0.0.1:8788/v1', JSON.stringify(body));
  check('the payload carries a non-empty models list',
        Array.isArray(body.models) && body.models.length === 2, JSON.stringify(body.models));
  check('models are sent as bare ids',
        body.models[0] === 'model-a' && body.models[1] === 'model-b',
        JSON.stringify(body.models));
  check('the payload carries the selected model',
        body.model === 'model-a', JSON.stringify(body.model));
  check('the payload carries the selected key id',
        body.key_id === '__global', JSON.stringify(body.key_id));

  requests.length = 0;
  await api.restoreAgent('claude-code', null);
  const restoreReq = requests.find(r => r.url.indexOf('/agents/restore') !== -1);
  check('restoreAgent posts to /agents/restore', !!restoreReq,
        JSON.stringify(requests.map(r => r.url)));
  const restoreBody = JSON.parse((restoreReq && restoreReq.opts.body) || '{}');
  check('the restore payload carries client',
        restoreBody.client === 'claude-code', JSON.stringify(restoreBody));
  check('the restore payload carries client_id too',
        restoreBody.client_id === 'claude-code', JSON.stringify(restoreBody));

  // 勾选功能带来的新契约：写进客户端的清单 = 勾选集合 ∩ 当前模型目录。
  // 每次 apply 前都要重新喂卡片数据：applyAgent 的 finally 会 loadAgents()，
  // 而第一个 fetch stub 把 clients 返回成空。
  select(['model-b']);
  requests.length = 0;
  seed();
  await api.applyAgent('claude-code', null);
  const partialReq = requests.find(r => r.url.indexOf('/agents/apply') !== -1);
  const partialBody = JSON.parse((partialReq && partialReq.opts.body) || '{}');
  check('只勾选一个时 payload 只带那一个模型',
        Array.isArray(partialBody.models) && partialBody.models.length === 1
          && partialBody.models[0] === 'model-b',
        JSON.stringify(partialBody.models));

  CLIENTS.push({id: 'dsh', label: 'DeepSeek Harness', desc: 'DSH desktop',
                protocol: 'openai', config_paths: ['/home/u/.dsh/settings.yaml'],
                installed: true, applied: null});
  // 先证明 dsh 被认成了客户端：有勾选时它照常发请求（否则下面的"被挡下"可能因别的
  // 早退分支假通过）
  select(['model-a']);
  requests.length = 0;
  seed();
  await api.applyAgent('dsh', null);
  check('有勾选时写模型清单的客户端照常发 /agents/apply',
        !!requests.find(r => r.url.indexOf('/agents/apply') !== -1),
        JSON.stringify(requests.map(r => r.url)));

  // 一个都没勾：把模型清单写进自身配置的客户端（DSH / OpenCode / Crush / ZCode，
  // 由后端 /agents 的 model_embedding_clients 声明）必须被挡下且不发请求；
  // 不读这份清单的 Claude Code 照发，带空数组。
  select([]);
  requests.length = 0;
  seed();
  await api.applyAgent('dsh', null);
  check('空勾选时嵌入模型清单的客户端被挡下，不发 /agents/apply',
        !requests.some(r => r.url.indexOf('/agents/apply') !== -1),
        JSON.stringify(requests.map(r => r.url)));

  requests.length = 0;
  seed();
  await api.applyAgent('claude-code', null);
  const noneReq = requests.find(r => r.url.indexOf('/agents/apply') !== -1);
  const noneBody = JSON.parse((noneReq && noneReq.opts.body) || '{}');
  check('不读模型清单的客户端不受影响，带空清单发出',
        !!noneReq && Array.isArray(noneBody.models) && noneBody.models.length === 0,
        JSON.stringify(noneBody.models));
  CLIENTS.pop();   // 上面临时加的 dsh 卡片不留给后面的渲染断言
  select(['model-a', 'model-b']);

  // ---- 默认态契约：服务端没存过 = 全部勾选 ---------------------------------
  // 勾选这版之前，「一键配置」写的是整份目录。`agent_models` 还没落过时后端回
  // configured=false，面板必须把空集合读成「全都要」——否则老安装升级后第一次点
  // 一键配置会突然被拒绝，而用户一个模型都没动过。
  const MODEL_IDS = MODELS.map(m => m.id);
  api.setModelsData(MODELS.slice());
  SELECTED.configured = false;
  SELECTED.models = [];
  await api.loadAgentModelSelection();
  check('configured=false 时面板记为「没存过」', api.isConfigured() === false);
  check('从未保存 ⇒ 每个模型都算勾选',
        MODEL_IDS.every(id => api.isModelPicked(id)), JSON.stringify(api.selection()));
  api.syncModelPickBoxes();
  check('从未保存 ⇒ 计数写全量',
        /\(共 2 个\) · 已选 2/.test(element('availModelCount').textContent || ''),
        element('availModelCount').textContent);

  // 从全选态里取消一个：必须先把整份清单物化成显式集合，再动这一项。少了这一步，
  // 「取消 model-b」会被存成「只选了 model-a」——语义正好反了。
  requests.length = 0;
  fireDocument('change', {target: {
    dataset: {modelId: 'model-b'}, checked: false,
    classList: {contains: name => name === 'model-pick'},
  }});
  await api.saveModelSelection();
  const materialiseReq = requests.find(r => r.url.indexOf('/agents/model-selection') !== -1);
  const materialiseBody = JSON.parse((materialiseReq && materialiseReq.opts.body) || '{}');
  check('从未保存时取消一项 → 先物化全量再落盘（存成其余全部）',
        Array.isArray(materialiseBody.models) && materialiseBody.models.length === 1
          && materialiseBody.models[0] === 'model-a', JSON.stringify(materialiseBody.models));
  check('落一次盘就转入「已保存」态', api.isConfigured() === true);

  // ---- 表头三个批量动作 ---------------------------------------------------
  // 做成按钮而不是「反选」复选框：反选是动作、不是状态，复选框形态要么得额外记
  // 一份「反选之前的集合」，要么让界面勾选和实际保存的清单分家。
  api.applyModelPickAction('invert');
  check('反选按当前勾选逐项取反', api.selection().join(',') === 'model-b', api.selection().join(','));
  api.applyModelPickAction('all');
  check('全选勾上整份当前清单',
        MODEL_IDS.every(id => api.isModelPicked(id)) && api.selection().length === 2,
        JSON.stringify(api.selection()));
  api.applyModelPickAction('none');
  requests.length = 0;
  await api.saveModelSelection();
  const clearPost = requests.find(r => r.url.indexOf('/agents/model-selection') !== -1);
  const clearBody = JSON.parse((clearPost && clearPost.opts.body) || '{}');
  check('全不选把空清单落盘（后端据此拒绝写空模型清单，而不是写一份空的）',
        Array.isArray(clearBody.models) && clearBody.models.length === 0, JSON.stringify(clearBody));
  api.syncModelPickBoxes();
  check('全不选 ⇒ 计数写 0，一眼看得出没选',
        /已选 0/.test(element('availModelCount').textContent || ''),
        element('availModelCount').textContent);

  SELECTED.configured = true;
  select(['model-a', 'model-b']);

  // The render path must run against a real /agents answer without throwing
  // and must actually fill the card grid and the model picker.
  global.fetch = (url, opts) => {
    if (String(url).indexOf('/agents') !== -1) {
      if (String(url).indexOf('/agents/model-selection') !== -1) {
        const sel = {configured: true, selected_models: SELECTED.models.slice()};
        requests.push({url: String(url), opts: opts || {}});
        return Promise.resolve({status: 200, ok: true,
          json: () => Promise.resolve(sel), text: () => Promise.resolve(JSON.stringify(sel))});
      }
      const payload = {
        clients: CLIENTS.concat([{
          id: 'codex', label: 'Codex CLI', desc: 'OpenAI Codex CLI',
          protocol: 'openai', config_paths: ['/home/u/.codex/config.toml'],
          installed: false, applied: {
            at: '2026-10-09 07:27:30', base_url: 'http://127.0.0.1:8788/v1',
            model: 'model-a', files: 2, external_change: true,
          },
        }]),
        keys: [{id: 'key-1', name: '面板 Key', enabled: true}],
        models: MODELS,
        gateway: {base_url: 'http://127.0.0.1:8788/v1'},
      };
      requests.push({url: String(url), opts: opts || {}});
      return Promise.resolve({
        status: 200, ok: true,
        json: () => Promise.resolve(payload),
        text: () => Promise.resolve(JSON.stringify(payload)),
      });
    }
    return realFetch(url, opts);
  };

  elements.forEach(el => { if (el) el.innerHTML = ''; });
  await api.loadAgents();
  const grid = element('agentCardGrid');
  check('the card grid is rendered', /Claude Code/.test(grid.innerHTML || ''),
        String(grid.innerHTML).slice(0, 160));
  check('a not-installed client is marked as such',
        /Codex CLI/.test(grid.innerHTML || ''), String(grid.innerHTML).slice(0, 160));
  check('an applied client offers the restore button',
        /restoreAgent/.test(grid.innerHTML || ''), String(grid.innerHTML).slice(0, 200));
  check('an externally changed config raises the warning',
        /外部修改|检测到外部修改/.test(grid.innerHTML || ''), String(grid.innerHTML).slice(0, 200));
  const modelSel = element('agentModelSelect');
  check('the model picker is populated',
        /model-a/.test(modelSel.innerHTML || ''), String(modelSel.innerHTML).slice(0, 160));
  const keySel = element('agentKeySelect');
  check('the key picker is populated',
        /key-1/.test(keySel.innerHTML || ''), String(keySel.innerHTML).slice(0, 160));

  // 计数与卡片必须对得上（issue #250）：列表是全部支持的客户端，检测到的只是
  // 其中一部分，所以标题要把两个数字都写出来，而不是只报「已检测到」的那个。
  const detectState = element('agentDetectState');
  check('检测计数同时给出卡片总数与本机检测数',
        /1 \/ 共 2/.test(detectState.textContent || ''), detectState.textContent);

  // An unknown client must not throw before the request is even built.
  requests.length = 0;
  await api.applyAgent('no-such-client', null);
  check('an unknown client is a no-op, not a crash',
        requests.filter(r => r.url.indexOf('/agents/apply') !== -1).length === 0);

  // 服务端形态（看板不在网关本机）时入口整个撤掉（issue #246）：
  // /agents/available 回 enabled:false → Tab 隐藏，?tab=agents 的书签退回网关页。
  global.fetch = (url, opts) => {
    if (String(url).indexOf('/agents/available') !== -1) {
      const payload = {enabled: false};
      return Promise.resolve({
        status: 200, ok: true,
        json: () => Promise.resolve(payload),
        text: () => Promise.resolve(JSON.stringify(payload)),
      });
    }
    return realFetch(url, opts);
  };
  const navAgents = element('btnNavAgents');
  await api.loadAgentsAvailability();
  check('a server-form panel hides the agents tab',
        navAgents.style.display === 'none', JSON.stringify(navAgents.style));
  check('the availability flag flips to false', api.agentsAvailable() === false);
  api.switchMainTab('agents');
  check('a stale agents tab falls back to the gateway page',
        api.currentTab() === 'gateway', api.currentTab());

  process.stdout.write('agent config UI assertions passed (' + checks + ' checks)\n');
})().catch(err => {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
