'use strict';
// Component logic only: no browser, no rendering, no network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(tag) { this.tagName=tag; this.children=[]; this.listeners={}; this.attrs={}; this.value=''; this.textContent=''; this.disabled=false; this.checked=false; this.className=''; this.style={}; this.dataset={}; this.hidden=false;
    this.classList={add:(s)=>{this.className+=' '+s;},remove:(s)=>{this.className=this.className.split(' ').filter(x=>x!==s).join(' ');},contains:(s)=>this.className.split(' ').includes(s)}; }
  append(...nodes) { nodes.forEach(n=>{n.parent=this;this.children.push(n);}); }
  before(n) { this.parent.append(n); }
  after(n) { this.parent.append(n); }
  replaceChildren(...nodes) { this.children=[]; this.append(...nodes); }
  setAttribute(k,v) { this.attrs[k]=String(v); }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener(k,fn) { (this.listeners[k]??=[]).push(fn); }
  async fire(k) { for(const fn of this.listeners[k]||[]) await fn({preventDefault(){},target:this}); }
  focus() { this.focused=true; }
  scrollIntoView() {}
}
const root=new Element('html');root.dataset={uiLanguage:'zh'};
const nav=new Element('button');nav.id='btnNavSettings';root.append(nav);
const gateway=new Element('div');gateway.id='pageGateway';gateway.className='main-page';root.append(gateway);
const all=()=>{const out=[]; const walk=n=>{out.push(n);n.children.forEach(walk);};walk(root);return out;};
const byId=id=>all().find(x=>x.id===id);
const text=n=>n.textContent+' '+n.children.map(text).join(' ');
let configGate=null, saveGate=null, failConfig=false, failOverview=false, backendURL='http://127.0.0.1:7866';
let inferenceFailure='';
let lang='zh', observer, failModels=false, enabled=true, copies=[], calls=[];
const cfg=()=>({enabled,base_url:backendURL,has_api_key:true,api_key_env:'CODEARTS_API_KEY',timeout_seconds:120});
const context={document:{documentElement:root,head:root,readyState:'complete',createElement:t=>new Element(t),getElementById:byId,querySelectorAll:sel=>all().filter(x=>x.className.split(' ').includes(sel.slice(1)))},
  FileReader:class{readAsDataURL(){this.result='data:image/png;base64,aGVsbG8=';this.onload();}},
  URL,location:{href:'http://localhost/?tab=codearts',origin:'http://localhost'},history:{replaceState(){}},localStorage:{setItem(){}},performance,console,
  navigator:{clipboard:{writeText:async x=>copies.push(x)}},MAIN_TABS:['gateway','settings'],
  MutationObserver:class { constructor(fn){observer=fn;} observe(){} },
  WB_I18N:{current:()=>lang},switchMainTab(){},
  getJSON:async path=>{calls.push(path); if(path==='/codearts/config'){if(failConfig)throw Error('settings offline');return configGate ? new Promise(resolve=>{configGate.resolve=resolve;}) : cfg();} if(path==='/codearts/overview' && failOverview)throw Error('overview offline'); if(path==='/codearts/overview')return {stats:{total:1,healthy:1},accounts:[{uid:'h1',nickname:'<img onerror=boom>',disabled:false}]}; if(path==='/codearts/models'){if(failModels)throw Error('catalog offline');return {data:[{id:'codeart/GLM-5.3',supports_images:false},{id:'codeart/Absent',backend_listed:false},{id:'codeart/vision-real',supports_images:true}]};} if(path==='/codearts/usage')return {requests:8,total_tokens:123};throw Error(path);},
  postJSON:async(path,body)=>{calls.push([path,body]);if(path==='/codearts/config' && saveGate)return new Promise(resolve=>{saveGate.resolve=resolve;});if(path==='/v1/chat/completions' && inferenceFailure)throw Error(inferenceFailure);if(path==='/v1/chat/completions')return {choices:[{message:{content:'OK'}}],usage:{total_tokens:7}};return {ok:true};}
};context.window=context;vm.createContext(context);
vm.runInContext(fs.readFileSync(require.resolve('../dashboard.js'),'utf8'),context);
(async()=>{
  await context.switchMainTab('codearts');
  assert(byId('ca-accounts-table'),'native accounts table');
  assert(byId('ca-models-table'),'native models table');
  assert(text(byId('ca-accounts-table')).includes('<img onerror=boom>'),'nickname is rendered as text');
  assert(!all().some(x=>x.tagName==='img'),'untrusted values cannot create elements');
  const url=byId('ca-base_url');url.value='http://unsaved.example';await url.fire('input');
  const prompt=byId('ca-test-message');prompt.value='do not reset';
  const select=byId('ca-test-model');select.value='codeart/GLM-5.3';await byId('ca-send-test').fire('click');
  assert(text(byId('ca-test-result')).includes('OK'));
  assert(text(byId('ca-models-table')).includes('支持图片'));
  assert(text(byId('ca-models-table')).includes('图片能力未知'));
  const imageURL=byId('ca-image-url'), imageFile=byId('ca-image-file'), imageDetail=byId('ca-image-detail');
  imageURL.value='https://example.test/image.png';imageDetail.value='high';
  const requestCount=()=>calls.filter(c=>Array.isArray(c)&&c[0]==='/v1/chat/completions').length;
  let beforeImage=requestCount();
  inferenceFailure='InferHub.001001020.406 The request model is not multimodal';
  await byId('ca-send-test').fire('click');
  assert.equal(requestCount(),beforeImage+1,'catalog false is advisory; request reaches upstream');
  assert(text(byId('ca-test-result')).includes('InferHub.001001020.406'),'upstream refusal is visible in test result');
  assert(text(byId('ca-image-capability-note')).includes('目录未声明'),'metadata is described as route declaration');
  inferenceFailure='';imageURL.value='';imageFile.files=[{type:'image/png',size:5}];
  await byId('ca-send-test').fire('click');assert.equal(requestCount(),beforeImage+2,'uploaded image allowed despite catalog false');
  assert(text(byId('ca-test-result')).includes('OK'),'successful reply replaces the refusal');
  const preservedModel=calls.filter(c=>Array.isArray(c)&&c[0]==='/v1/chat/completions').at(-1)[1].model;
  assert.equal(preservedModel,'codeart/GLM-5.3','advisory metadata never changes requested model');
  imageFile.files=[];imageURL.value='https://example.test/image.png';
  select.value='codeart/vision-real';await byId('ca-send-test').fire('click');
  let request=calls.filter(c=>Array.isArray(c)&&c[0]==='/v1/chat/completions').at(-1)[1];
  assert.equal(request.model,'codeart/vision-real');assert.equal(request.messages[0].content[1].image_url.detail,'high');
  imageURL.value='file:///private.png';beforeImage=requestCount();await byId('ca-send-test').fire('click');assert.equal(requestCount(),beforeImage);
  imageURL.value='';imageFile.files=[{type:'image/png',size:4*1024*1024+1}];await byId('ca-send-test').fire('click');assert.equal(requestCount(),beforeImage,'oversized file not sent');assert(text(byId('ca-test-result')).includes('4 MiB'),'file validation is visible in result area');
  context.FileReader=class{readAsDataURL(){this.result='data:image/png;base64,aGVsbG8=';this.onload();}};
  imageFile.files=[{type:'image/png',size:5}];await byId('ca-send-test').fire('click');
  request=calls.filter(c=>Array.isArray(c)&&c[0]==='/v1/chat/completions').at(-1)[1];assert.equal(request.messages[0].content[1].image_url.url,'data:image/png;base64,aGVsbG8=');
  let completeFile;
  context.FileReader=class{readAsDataURL(){completeFile=()=>{this.result='data:image/png;base64,aGVsbG8=';this.onload();};}};
  beforeImage=requestCount();const delayedImage=byId('ca-send-test').fire('click');
  backendURL='http://127.0.0.1:9999';await context.switchMainTab('codearts');completeFile();await delayedImage;
  assert.equal(requestCount(),beforeImage,'pending file must not cross an externally changed backend');
  backendURL='http://127.0.0.1:7866';await context.switchMainTab('codearts');
  imageFile.files=[];imageURL.value='';select.value='codeart/GLM-5.3';
  const beforeRefreshResult=text(byId('ca-test-result'));
  failModels=true;await context.switchMainTab('codearts');
  assert.equal(url.value,'http://unsaved.example');assert.equal(prompt.value,'do not reset');assert.equal(select.value,'codeart/GLM-5.3');
  assert.equal(text(byId('ca-test-result')),beforeRefreshResult,'refresh preserves the latest inference result or validation error');
  assert(text(byId('ca-accounts-table')).includes('<img onerror=boom>'),'partial failure retains accounts');
  assert(text(byId('ca-models-error')).includes('catalog offline'),'section failure is visible');
  const beforeLanguage=calls.length;lang='en';observer();assert.equal(calls.length,beforeLanguage,'language does not perform network calls');assert(text(byId('pageCodearts')).includes('Connection settings'));assert.equal(prompt.value,'do not reset');
  lang='zh-Hant';observer();assert(text(byId('pageCodearts')).includes('連線設定'));
  const css=fs.readFileSync(require.resolve('../dashboard.css'),'utf8');assert(!/var\(--(?:border|bg2|text)\)/.test(css));
  assert(!calls.some(c=>Array.isArray(c)&&c[0].includes('/admin/')),'refresh/language never performs admin mutations');
  failOverview=true;await context.switchMainTab('codearts');
  const oldAction=byId('ca-accounts-table').children[1].children[0].children[3].children[0].children[0];
  assert(oldAction.disabled,'stale account actions must be disabled after overview failure');
  backendURL='http://127.0.0.1:9999';await context.switchMainTab('codearts');assert(!text(byId('ca-accounts-table')).includes('<img onerror=boom>'),'new backend cannot inherit previous accounts');
  failOverview=false;backendURL='http://127.0.0.1:7866';await context.switchMainTab('codearts');
  configGate={};const refresh=context.switchMainTab('codearts');url.value='http://edited-during-load.example';await url.fire('input');configGate.resolve(cfg());configGate=null;await refresh;assert.equal(url.value,'http://edited-during-load.example');
  saveGate={};const saving=byId('ca-settings-form').fire('submit');url.value='http://edited-during-save.example';await url.fire('input');const secret=byId('ca-api_key');secret.value='unsaved-private-key';await secret.fire('input');saveGate.resolve({ok:true});saveGate=null;await saving;assert.equal(url.value,'http://edited-during-save.example');assert.equal(secret.value,'unsaved-private-key','saving an older revision must not clear newer credentials');
  url.value='http://successfully-saved.example';await url.fire('input');
  configGate={};const oldRefresh=context.switchMainTab('codearts');
  const saveDuringRefresh=byId('ca-settings-form').fire('submit');await new Promise(setImmediate);
  const oldGate=configGate;configGate=null;failConfig=true;oldGate.resolve(cfg());
  await oldRefresh;await saveDuringRefresh;
  assert.equal(url.value,'http://successfully-saved.example','an older snapshot must not undo a confirmed save even if the next refresh fails');
  failConfig=false;
  enabled=false;await context.switchMainTab('codearts');assert(!text(byId('ca-accounts-table')).includes('<img onerror=boom>'),'disabled bridge clears previous account rows');
  console.log('PASS: native structure, inert metadata, unsaved edits, selection/result retention, partial errors, live language and disabled state');
})().catch(e=>{console.error(e);process.exitCode=1;});
