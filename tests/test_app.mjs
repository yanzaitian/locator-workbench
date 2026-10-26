import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {decryptState as realDecrypt} from '../site/crypto.js';
import {webcrypto} from 'node:crypto';
import * as summaries from '../site/plan-summary.js';

const source=readFileSync(new URL('../site/app.js',import.meta.url),'utf8').replace(/^import .*;\r?\n/gm,'');
class Element{
 constructor(){this.hidden=false;this.textContent='';this.innerHTML='';this.value='';this.children=[];this.disabled=false;this.open=false;}
 append(...children){this.children.push(...children);}
 replaceChildren(...children){this.children=children;this.innerHTML='';this.textContent='';}
 insertAdjacentHTML(_where,value){this.innerHTML+=value;}
 close(){this.open=false;}
 showModal(){this.open=true;}
 click(){this.clicked=true;}
}
function harness({fetch, decrypt=async value=>value}={}){
 const selectors=['unlock','tools','status','summary','device-section','schedule-summary','cards','updated','password','refresh','lock','plans','plantext','planerror','editor','close','download'];
 const elements=Object.fromEntries(selectors.map(key=>['#'+key,new Element()]));elements['#unlock button']=new Element();
 for(const key of ['tools','status','summary','device-section','schedule-summary'])elements['#'+key].hidden=true;
 let timer;
 const context=vm.createContext({createManagement:()=>({render(){},lock(){}}),...summaries,decryptState:decrypt,document:{querySelector:key=>{assert.ok(elements[key],key);return elements[key]},createElement:()=>new Element(),createTextNode:text=>text},fetch:fetch||asyncOk,Date,URL,Blob,structuredClone,setInterval:fn=>{timer=fn;}});
 vm.runInContext(source,context,{filename:'app.js'});
 const get=key=>elements['#'+key];
 return {get,tick:()=>timer(),submit:async(password='fake-test-password')=>{get('password').value=password;return get('unlock').onsubmit({preventDefault(){},submitter:elements['#unlock button']});},button:elements['#unlock button']};
}
const data=(name='假设备')=>({updated:new Date().toISOString(),devices:[{device_id:'fake-id',name,listed_at:new Date().toISOString(),settings:{},query_status:'查询成功',last_point:null}],plan:{schedule:{},holidays:{}}});
const response=(value=data())=>({ok:true,status:200,json:async()=>value});
const asyncOk=async()=>response();
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject};}
function assertLocked(h){
 for(const key of ['tools','summary','device-section','schedule-summary','status'])assert.equal(h.get(key).hidden,true,key);
 assert.equal(h.get('unlock').hidden,false);
 assert.equal(h.get('cards').children.length,0);assert.equal(h.get('summary').innerHTML,'');
 for(const key of ['status','updated','schedule-summary','planerror'])assert.equal(h.get(key).textContent,'',key);
 assert.equal(h.get('plantext').value,'');assert.equal(h.get('password').value,'');
}
test('successful refresh finishing after lock never redisplays decrypted data',async()=>{
 const pending=deferred();let calls=0;
 const h=harness({fetch:()=>++calls===1?asyncOk():pending.promise});
 await h.submit();h.get('plans').onclick();assert.equal(h.get('editor').open,true);
 const refresh=h.get('refresh').onclick();h.get('lock').onclick();assertLocked(h);
 pending.resolve(response(data('过期敏感数据')));await refresh;assertLocked(h);
 assert.equal(h.get('editor').open,false);
});
test('decrypt finishing after lock is ignored, including after re-unlock',async()=>{
 const pending=deferred(),started=deferred();let calls=0;
 const h=harness({decrypt:value=>{if(++calls===2){started.resolve();return pending.promise}return Promise.resolve(value)}});
 await h.submit();const refresh=h.get('refresh').onclick();await started.promise;
 h.get('lock').onclick();await h.submit('another-fake-password');
 pending.resolve(data('旧会话设备'));await refresh;
 assert.equal(h.get('cards').children[0].children[0].textContent,'假设备');
});
test('late request failure after lock does not reveal stale status',async()=>{
 const pending=deferred();let calls=0;
 const h=harness({fetch:()=>++calls===1?asyncOk():pending.promise});await h.submit();
 const refresh=h.get('refresh').onclick();h.get('lock').onclick();pending.reject(Error('dummy network failure'));await refresh;assertLocked(h);
});
test('newer refresh response wins over older response',async()=>{
 const older=deferred(),newer=deferred();let calls=0;
 const h=harness({fetch:()=>++calls===1?asyncOk():calls===2?older.promise:newer.promise});await h.submit();
 const old=h.get('refresh').onclick(),recent=h.get('refresh').onclick();
 newer.resolve(response(data('最新设备')));await recent;older.resolve(response(data('旧设备')));await old;
 assert.equal(h.get('cards').children[0].children[0].textContent,'最新设备');
});
for(const [label,fetch,decrypt,expected] of [
 ['network',async()=>{throw Error('dummy')},undefined,/无法连接云端/],
 ['missing',async()=>({ok:false,status:404}),undefined,/云端尚未生成定位数据.*需管理员检查.*Actions.*小米凭据/],
 ['http',async()=>({ok:false,status:503}),undefined,/HTTP 503/],
 ['json',async()=>({ok:true,status:200,json:async()=>{throw Error('dummy')}}),undefined,/不是有效 JSON/],
 ['decrypt',asyncOk,async()=>{throw Error('dummy')},/无法解密状态/],
 ['structure',async()=>response({devices:'wrong'}),undefined,/状态结构无效/]
])test(`unlock failure distinguishes ${label} and clears entered password`,async()=>{
 const h=harness({fetch,decrypt});await h.submit();assert.match(h.get('status').textContent,expected);
 assert.equal(h.get('password').value,'');assert.equal(h.button.disabled,false);assert.equal(h.get('unlock').hidden,false);
});
test('failed refresh preserves existing cards with an explicit stale-data warning',async()=>{
 let calls=0;const h=harness({fetch:async()=>++calls===1?response():{ok:false,status:503}});await h.submit();
 const card=h.get('cards').children[0];await h.get('refresh').onclick();
 assert.equal(h.get('cards').children[0],card);assert.match(h.get('status').textContent,/HTTP 503.*旧数据/);
});
test('export says submission required, never claims the cloud plan was saved',async()=>{
 const h=harness();await h.submit();h.get('plans').onclick();h.get('download').onclick();
 assert.match(h.get('planerror').textContent,/不会直接保存到仓库.*提交计划/);
 h.get('plantext').value='{"devices":[]}';h.get('download').onclick();assert.match(h.get('planerror').textContent,/格式错误/);
});
test('actual WebCrypto encryption and decryption unlocks and rejects a wrong test password',async()=>{
 const salt=new Uint8Array(16),iv=new Uint8Array(12),password='fake-crypto-password';
 const source=await webcrypto.subtle.importKey('raw',new TextEncoder().encode(password),'PBKDF2',false,['deriveKey']);
 const key=await webcrypto.subtle.deriveKey({name:'PBKDF2',salt,iterations:600000,hash:'SHA-256'},source,{name:'AES-GCM',length:256},false,['encrypt']);
 const cipher=await webcrypto.subtle.encrypt({name:'AES-GCM',iv,additionalData:new TextEncoder().encode('locator-state-v1')},key,new TextEncoder().encode(JSON.stringify(data())));
 const envelope={version:1,iterations:600000,salt:Buffer.from(salt).toString('base64'),nonce:Buffer.from(iv).toString('base64'),ciphertext:Buffer.from(cipher).toString('base64')};
 const h=harness({fetch:async()=>response(envelope),decrypt:realDecrypt});
 await h.submit(password);assert.equal(h.get('unlock').hidden,true);h.get('lock').onclick();await h.submit('wrong-test-password');
 assert.match(h.get('status').textContent,/无法解密状态/);assert.equal(h.get('cards').children.length,0);
});

test('auto refresh waits for initial unlock to complete',async()=>{
 const pending=deferred();let calls=0;
 const h=harness({fetch:()=>{calls++;return pending.promise;}});
 const unlock=h.submit();h.tick();assert.equal(calls,1);
 pending.resolve(response());await unlock;assert.equal(h.get('unlock').hidden,true);
});
test('locking during unlock restores the form and invalidates the pending attempt',async()=>{
 const pending=deferred();let calls=0;
 const h=harness({fetch:()=>++calls===1?pending.promise:asyncOk()});
 const old=h.submit();assert.equal(h.button.disabled,true);h.get('lock').onclick();
 assert.equal(h.button.disabled,false);await h.submit('second-fake-password');
 pending.resolve(response(data('旧会话')));await old;
 assert.equal(h.get('cards').children[0].children[0].textContent,'假设备');
 assert.equal(h.button.disabled,false);
});

test('missing cloud state never attempts JSON parsing or password decryption and can recover',async()=>{
 let calls=0,jsonCalls=0,decryptCalls=0;
 const h=harness({fetch:async()=>++calls===1?{ok:false,status:404,json:async()=>{jsonCalls++;throw Error('must not parse 404')}}:response(),decrypt:async value=>{decryptCalls++;return value;}});
 await h.submit();
 assert.match(h.get('status').textContent,/云端尚未生成定位数据.*需管理员检查.*Actions.*小米凭据/);
 assert.doesNotMatch(h.get('status').textContent,/密码.*错误|无法解密/);
 assert.equal(jsonCalls,0);assert.equal(decryptCalls,0);
 assert.equal(h.get('cards').children.length,0);assert.equal(h.get('unlock').hidden,false);
 await h.submit();assert.equal(decryptCalls,1);assert.equal(h.get('unlock').hidden,true);
});
test('404 during refresh preserves previously decrypted cards with the missing-data warning',async()=>{
 let calls=0;
 const h=harness({fetch:async()=>++calls===1?response():{ok:false,status:404}});
 await h.submit();const card=h.get('cards').children[0];await h.get('refresh').onclick();
 assert.equal(h.get('cards').children[0],card);
 assert.match(h.get('status').textContent,/云端尚未生成定位数据.*小米凭据.*旧数据/);
});
