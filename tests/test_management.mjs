import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {webcrypto} from 'node:crypto';
import {encryptState, decryptState} from '../site/crypto.js';
import {plansFromState,draftForDevice,buildPlans,validateDraft,makeDispatchBody,normalizeReports,httpMessage} from '../site/management.js';

const PASSWORD = 'fake-test-password-123';
const TOKEN = 'fake-github-token-for-tests';
const schedule = {enabled:true,skip_holidays:true,default:{enabled:true,start:'08:00',end:'22:00',interval_minutes:60,extra:'keep'},days:{6:{enabled:true,start:'10:00',end:'18:00',interval_minutes:20}},holiday_workday:{enabled:false,start:'09:00',end:'21:00',interval_minutes:30},future:{keep:true}};
const fixture = () => ({plan_request_id:'revision-1',plan:{schedule:structuredClone(schedule),holidays:{statutory:['2026-10-01'],workdays:[]},future:'untouched'},devices:[{device_id:'phone-1',name:'测试手机',settings:{enabled:true,plan_json:JSON.stringify(schedule),unknown:'keep'}},{device_id:'phone-2',name:'备用手机',settings:{}},{device_id:'mail:label',name:'邮件设备',source:'mail',settings:{}}],reports:[],mail:{}});
const deferred = () => {let resolve,reject; const promise = new Promise((a,b) => {resolve=a;reject=b;});return {promise,resolve,reject};};

class Element {
 constructor(tag='div') { this.tagName=tag;this.children=[];this.attributes={};this.value='';this.textContent='';this.disabled=false;this.checked=false;this.hidden=false;this.className='';this.classList={add:name=>{this.className+=' '+name;}}; }
 append(...nodes) {this.children.push(...nodes);}
 replaceChildren(...nodes) {this.children=nodes;this.textContent='';}
 setAttribute(name,value) {this.attributes[name]=value;}
 click() {return this.onclick?.();}
}
const walk = node => [node,...node.children.flatMap(walk)];
const text = node => [node.textContent,...node.children.map(text)].join(' ');
const source = readFileSync(new URL('../site/management.js',import.meta.url),'utf8').replace(/^import .*;\r?\n/gm,'').replace(/\bexport /g,'');
function harness(options={}) {
 let state = fixture(), password = PASSWORD;
 const root = new Element(), store = new Map(), requests=[];
 const context = vm.createContext({document:{getElementById:id=>id==='management' ? root : null,createElement:tag=>new Element(tag)},
  structuredClone,crypto:webcrypto,Date,Blob,URL,encryptState:options.encrypt || encryptState,decryptState:options.decrypt || decryptState,
  localStorage:{getItem:key=>store.get(key) ?? null,setItem:(key,value)=>store.set(key,value),removeItem:key=>store.delete(key)},
  fetch:async (url,init)=>{requests.push({url,...init});return options.fetch ? options.fetch(url,init) : {status:204};}});
 vm.runInContext(source+'\nglobalThis.api = createManagement({getState:()=>readState(),getPassword:()=>readPassword(),onRefresh:()=>refresh()});',Object.assign(context,{readState:()=>state,readPassword:()=>password,refresh:options.refresh || (async()=>{})}));
 const api = context.api;
 const named = name => {const found=walk(root).find(n=>n.attributes['aria-label']===name || n.tagName==='button'&&n.textContent===name);assert.ok(found,name);return found;};
 const input = label => {const wrap=walk(root).find(n=>n.tagName==='label'&&n.children[0]?.textContent===label);assert.ok(wrap,label);return wrap.children[1];};
 const enter = (node,value) => {if(node.type==='checkbox')node.checked=value;else node.value=value;node.oninput?.();node.onchange?.();};
 const submit = () => walk(root).find(n=>n.tagName==='form').onsubmit({preventDefault(){}});
 api.render();
 return {api,root,store,requests,named,input,enter,submit,get state(){return state;},set state(value){state=value;},set password(value){password=value;},text:()=>text(root)};
}

test('plan construction changes default only, preserving weekdays, holidays, unknown fields and input',()=>{
 const state=fixture(), before=structuredClone(state), plans=plansFromState(state), draft=draftForDevice(plans,'phone-1');
 Object.assign(draft,{start:'22:00',end:'02:00',interval:'25',slotChanged:true});
 const result=buildPlans(plans,new Map([['phone-1',draft]])), own=JSON.parse(result.devices['phone-1'].plan_json);
 assert.deepEqual(own,{...schedule,default:{...schedule.default,start:'22:00',end:'02:00',interval_minutes:25}});
 assert.deepEqual(result.schedule,schedule);assert.deepEqual(result.holidays,state.plan.holidays);
 assert.equal(result.devices['phone-1'].unknown,'keep');assert.deepEqual(state,before);
 assert.equal(result.future,'untouched');assert.deepEqual(result.devices['phone-2'],{});
});
test('enabled-only edit preserves absent inherited interval and independent plan string',()=>{
 const plans=plansFromState(fixture());
 for(const id of ['phone-1','phone-2']) {
  const draft=draftForDevice(plans,id);draft.enabled=false;
  const result=buildPlans(plans,new Map([[id,draft]]));
  assert.equal(result.devices[id].enabled,false);
  assert.equal(result.devices[id].plan_json,plans.devices[id].plan_json);
  assert.equal(Object.hasOwn(result.devices[id],'interval_minutes'),false);
 }
});
test('inheritance is explicit; independent copies all global rules and object plans stay objects',()=>{
 const plans=plansFromState(fixture());
 const inherit=draftForDevice(plans,'phone-1');Object.assign(inherit,{inherit:true,inheritChanged:true});
 assert.equal(buildPlans(plans,new Map([['phone-1',inherit]])).devices['phone-1'].plan_json,null);
 const own=draftForDevice(plans,'phone-2');Object.assign(own,{inherit:false,inheritChanged:true,start:'00:00',end:'00:00',interval:'10'});
 const result=buildPlans(plans,new Map([['phone-2',own]])).devices['phone-2'].plan_json;
 assert.deepEqual(result.days,schedule.days);assert.deepEqual(result.holiday_workday,schedule.holiday_workday);
 assert.equal(result.default.start,'00:00');assert.equal(result.default.end,'00:00');
 plans.devices['phone-1'].plan_json=structuredClone(schedule);
 const draft=draftForDevice(plans,'phone-1');draft.interval='30';draft.slotChanged=true;
 assert.equal(typeof buildPlans(plans,new Map([['phone-1',draft]])).devices['phone-1'].plan_json,'object');
});
test('validation rejects invalid time, fractional/empty/small/large interval and corrupt plan',()=>{
 const draft=draftForDevice(plansFromState(fixture()),'phone-1');
 for(const interval of ['','9','10.1','1e2','Infinity','10081','nope'])assert.throws(()=>validateDraft({...draft,interval}));
 for(const start of ['24:00','1:00','08:99','<script>'])assert.throws(()=>validateDraft({...draft,start}));
 for(const interval of ['10','10080'])validateDraft({...draft,interval,start:'00:00',end:'00:00'});
 const plans=plansFromState(fixture());plans.devices['phone-1'].plan_json='bad-json';
 assert.throws(()=>draftForDevice(plans,'phone-1'),/JSON/);
 assert.throws(()=>buildPlans(plans,new Map([['phone-1',draft]])),/JSON/);
});
test('dispatch body is real authenticated ciphertext with revision, UUID and no plaintext plans/password',async()=>{
 const plans=plansFromState(fixture());
 const {body,requestId}=await makeDispatchBody(plans,PASSWORD,'revision-1');
 assert.deepEqual(Object.keys(body),['ref','inputs']);assert.equal(body.ref,'main');assert.equal(body.inputs.operation,'save_plans');
 const wire=JSON.stringify(body);
 for(const secret of [PASSWORD,'phone-1','revision-1','locator-plans-v1','08:00'])assert.ok(!wire.includes(secret));
 const decoded=await decryptState(JSON.parse(body.inputs.payload),PASSWORD);
 assert.equal(decoded.kind,'locator-plans-v1');assert.equal(decoded.base_revision,'revision-1');assert.equal(decoded.request_id,requestId);
 assert.match(requestId,/^[\da-f]{8}-[\da-f-]{27}$/);assert.ok(Number.isFinite(Date.parse(decoded.created_at)));assert.deepEqual(decoded.plans,plans);
 await assert.rejects(decryptState(JSON.parse(body.inputs.payload),'wrong-password-test'));
 const first=await makeDispatchBody({},PASSWORD,'');
 assert.equal((await decryptState(JSON.parse(first.body.inputs.payload),PASSWORD)).base_revision,'');
});
test('optional mount is safe and API has only render and lock',()=>{
 const context=vm.createContext({document:{getElementById:()=>null},structuredClone});
 vm.runInContext(source+'\nglobalThis.api = createManagement({getState:()=>null,getPassword:()=>null});',context);
 context.api.render();context.api.lock();assert.deepEqual(Object.keys(context.api),['render','lock']);
});
test('accessible controls, real names, and mail devices excluded from plan editing',()=>{
 const h=harness(), options=h.named('选择设备').children;
 assert.equal(options.length,2);assert.equal(options[0].textContent,'测试手机');assert.equal(options[1].textContent,'备用手机');
 assert.equal(h.named('GitHub 授权 Token').type,'password');assert.equal(h.named('记住加密授权').checked,false);assert.ok(h.named('加密提交计划'));
});
test('refresh preserves drafts across device switching and stale revision blocks network',async()=>{
 const h=harness();h.enter(h.input('独立计划开始时间'),'21:00');
 h.enter(h.named('选择设备'),'phone-2');h.enter(h.input('启用设备'),false);
 h.enter(h.named('选择设备'),'phone-1');assert.equal(h.input('独立计划开始时间').value,'21:00');
 h.state={...h.state,updated:'2026-10-26T10:00:00Z'};await h.named('刷新发布状态').click();
 assert.equal(h.input('独立计划开始时间').value,'21:00');
 h.state={...h.state,plan_request_id:'new-cloud-revision'};h.api.render();await h.submit();
 assert.equal(h.requests.length,0);assert.match(h.text(),/版本已变化/);assert.equal(h.input('独立计划开始时间').value,'21:00');
 h.named('放弃草稿并载入最新计划').click();assert.equal(h.input('独立计划开始时间').value,'08:00');
});
test('POST 204 remains pending until exact published request id; token cache defaults off',async()=>{
 const h=harness();h.enter(h.named('GitHub 授权 Token'),TOKEN);h.enter(h.input('设备间隔（分钟）'),'30');await h.submit();
 assert.equal(h.requests.length,1);const req=h.requests[0];assert.match(req.url,/\/repos\/yanzaitian\/locator-workbench\/actions\/workflows\/locator.yml\/dispatches$/);
 assert.equal(req.method,'POST');assert.equal(req.headers.Authorization,`Bearer ${TOKEN}`);
 for(const value of [TOKEN,PASSWORD,'phone-1'])assert.ok(!req.body.includes(value));
 assert.equal(h.store.size,0);assert.match(h.text(),/已提交，等待云端发布/);assert.doesNotMatch(h.text(),/已生效/);
 h.state={...h.state,plan_request_id:'other-request'};h.api.render();assert.doesNotMatch(h.text(),/已生效/);
 const decoded=await decryptState(JSON.parse(JSON.parse(req.body).inputs.payload),PASSWORD);
 h.state={...h.state,plan_request_id:decoded.request_id};h.api.render();assert.match(h.text(),/已生效/);
});
test('HTTP failures never read or echo body; 401 clears in-memory token',async()=>{
 for(const status of [401,403,404,422,429,500,200]) {
  const h=harness({encrypt:async()=>({ciphertext:'fake-only-test'}),fetch:async()=>({status,text(){assert.fail('must not read response body');},json(){assert.fail('must not read response body');}})});
  h.enter(h.named('GitHub 授权 Token'),TOKEN);await h.submit();assert.ok(h.text().includes(httpMessage(status)));assert.doesNotMatch(h.text(),/已生效/);
  if(status===401)assert.equal(h.named('GitHub 授权 Token').value,'');
 }
});
test('encrypted remember can restore; no password/token in storage and forget removes it',async()=>{
 const h=harness();h.enter(h.named('GitHub 授权 Token'),TOKEN);h.enter(h.named('记住加密授权'),true);await h.submit();
 const raw=h.store.get('locator-admin-v1');assert.ok(raw);assert.ok(!raw.includes(TOKEN));assert.ok(!raw.includes(PASSWORD));
 assert.deepEqual(await decryptState(JSON.parse(raw),PASSWORD),{token:TOKEN});
 h.api.lock();assert.equal(h.root.children.length,0);assert.equal(h.root.hidden,true);assert.equal(h.store.get('locator-admin-v1'),raw);
 h.api.render();assert.equal(h.named('GitHub 授权 Token').value,'');await h.named('恢复本机加密授权').click();assert.equal(h.named('GitHub 授权 Token').value,TOKEN);
 h.named('清除本机授权').click();assert.equal(h.store.size,0);assert.equal(h.named('GitHub 授权 Token').value,'');
});
test('lock during payload encryption prevents POST and late status even after re-unlock',async()=>{
 const gate=deferred();const h=harness({encrypt:()=>gate.promise});h.enter(h.named('GitHub 授权 Token'),TOKEN);
 const save=h.submit(), old=h.named('GitHub 授权 Token');h.api.lock();assert.equal(old.value,'');
 h.api.render();gate.resolve({ciphertext:'dummy'});await save;
 assert.equal(h.requests.length,0);assert.equal(h.named('GitHub 授权 Token').value,'');assert.doesNotMatch(h.text(),/已提交/);
});
test('lock while encrypting remembered token writes no cache and sends no request',async()=>{
 const gate=deferred(), started=deferred();let calls=0;
 const h=harness({encrypt:async()=>{if(++calls===1)return {ciphertext:'dummy'};started.resolve();return gate.promise;}});
 h.enter(h.named('GitHub 授权 Token'),TOKEN);h.enter(h.named('记住加密授权'),true);
 const saving=h.submit();await started.promise;h.api.lock();gate.resolve({ciphertext:'encrypted-token'});await saving;
 assert.equal(h.store.size,0);assert.equal(h.requests.length,0);assert.equal(h.root.children.length,0);
});
test('lock during POST success or failure never restores DOM or stale status',async()=>{
 for(const fail of [false,true]) {
  const gate=deferred(), started=deferred();
  const h=harness({encrypt:async()=>({ciphertext:'dummy'}),fetch:()=>{started.resolve();return gate.promise;}});
  h.enter(h.named('GitHub 授权 Token'),TOKEN);const saving=h.submit();await started.promise;
  h.api.lock();if(fail)gate.reject(Error('private server diagnostic'));else gate.resolve({status:204});await saving;
  assert.equal(h.root.children.length,0);assert.equal(h.root.hidden,true);
 }
});
test('lock during restore or refresh does not redisplay token/report; new session ignores old restore',async()=>{
 const gate=deferred();const h=harness({decrypt:()=>gate.promise});h.store.set('locator-admin-v1','{}');
 const restoring=h.named('恢复本机加密授权').click();h.api.lock();h.api.render();gate.resolve({token:TOKEN});await restoring;
 assert.equal(h.named('GitHub 授权 Token').value,'');
 const refreshGate=deferred();const r=harness({refresh:()=>refreshGate.promise});const refreshing=r.named('刷新发布状态').click();r.api.lock();refreshGate.resolve();await refreshing;
 assert.equal(r.root.children.length,0);assert.equal(r.root.hidden,true);
});
test('edits made during POST and acknowledgement remain unsaved and visible',async()=>{
 const gate=deferred(), started=deferred();const h=harness({fetch:()=>{started.resolve();return gate.promise;}});
 h.enter(h.named('GitHub 授权 Token'),TOKEN);h.enter(h.input('设备间隔（分钟）'),'30');const saving=h.submit();await started.promise;
 h.enter(h.input('独立计划开始时间'),'21:00');gate.resolve({status:204});await saving;
 const decoded=await decryptState(JSON.parse(JSON.parse(h.requests[0].body).inputs.payload),PASSWORD);
 h.state={...h.state,plan_request_id:decoded.request_id};h.api.render();assert.equal(h.input('独立计划开始时间').value,'21:00');assert.match(h.text(),/仍有未保存修改/);
});
test('reports show preview/final schema, device names, raw Markdown safely; mail uses fixed whitelist',()=>{
 const h=harness();assert.match(h.text(),/下月将自动生成月报/);
 const markdown='# 月报\n<img src=x onerror=alert(1)> **原文**';
 h.state={...h.state,reports:[{device_id:'phone-1',month:'2026-10',content:markdown,status:'preview',is_final:false,source:'local',generated_at:'2026-10-26T00:00:00Z'},{device_id:'phone-2',month:'2026-09',content:'正式报告',status:'completed',is_final:true,source:'deepseek'}],mail:{status:'ok',points:3,messages:5,account:'private@example.invalid',message:'private-account-message',error:'private-error',scope:'private-scope',checked_at:'private@example.invalid'}};
 h.api.render();assert.match(h.text(),/本月预览/);assert.match(h.text(),/2026-10 · 测试手机/);assert.match(h.text(),/2026-09 · 备用手机/);assert.match(h.text(),/本地模板/);assert.match(h.text(),/DeepSeek/);
 assert.equal(walk(h.root).find(n=>n.tagName==='pre').textContent,markdown);assert.equal(walk(h.root).filter(n=>n.tagName==='img').length,0);
 for(const secret of ['private@example.invalid','private-account-message','private-error','private-scope'])assert.ok(!h.text().includes(secret));
 const nested={'2026-10':{'phone-1':h.state.reports[0]}};assert.equal(normalizeReports(nested).length,1);
 h.state.reports=nested;h.api.render();assert.match(h.text(),/本月预览/);
});
