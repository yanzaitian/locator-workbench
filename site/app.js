import {decryptState} from './crypto.js';
const $=s=>document.querySelector(s);let password='',state=null;
const date=s=>s?new Date(s).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'未知';
const age=s=>s?Math.max(0,Date.now()-Date.parse(s)):Infinity;
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function fact(label,value){return `<div class="fact"><label>${esc(label)}</label><b>${esc(value)}</b></div>`;}
function planText(plan){if(!plan||plan.enabled===false)return '未启用';const start=plan.start||'00:00',end=plan.end||'23:59',interval=plan.interval_minutes||'?';return `${start}-${end}，每 ${interval} 分钟`}
function render(){
 const devices=state.devices||[], stale=age(state.updated)>30*60000, located=devices.filter(d=>d.last_point).length, failures=devices.filter(d=>d.query_status&&/失败|未找到/.test(d.query_status)).length;
 $('#unlock').hidden=true;$('#tools').hidden=false;$('#status').hidden=false;$('#summary').hidden=false;$('#device-section').hidden=false;
 $('#status').textContent='数据更新时间：'+date(state.updated)+(stale?'。超过 30 分钟未更新，请检查 Actions 和凭据。':'')+(state.error?'。'+state.error:'');$('#status').className='status'+(stale||state.error?' warn':'');$('#updated').textContent='最后更新 '+date(state.updated);
 $('#summary').innerHTML=[['设备总数',devices.length],['已有位置',located],['查询异常',failures],['数据状态',stale?'需检查':'正常']].map(x=>`<div class="metric"><strong>${x[1]}</strong><span>${x[0]}</span></div>`).join('');
 $('#cards').replaceChildren();
 for(const d of devices){const pt=d.last_point,missing=d.listed_at!==state.updated,old=pt&&age(pt.ts)>3600000,problem=missing||old||/失败/.test(d.query_status||'');const c=document.createElement('article');c.className='device '+(problem?'warn':'ok');
 const h=document.createElement('h3');h.textContent=d.name||d.device_id;c.append(h);const tag=document.createElement('span');tag.className='tag';tag.textContent=d.online===true||d.online===1?'在线':d.online===false||d.online===0?'离线':'状态未知';c.append(tag);
 const status=d.query_status||'尚未查询';const settings=d.settings||{};const address=d.address||pt?.address||'';c.insertAdjacentHTML('beforeend','<div class="facts">'+fact('本次查询',date(d.last_query))+fact('位置时间',pt?date(pt.ts):'暂无有效位置')+fact('电量',d.battery==null?'未知':d.battery+'%')+fact('精度',pt&&pt.acc!=null?Math.round(pt.acc)+' 米':'未知')+fact('地址',address||'暂无地址')+'</div>');
 const psummary=document.createElement('p');psummary.className='plan-summary';psummary.textContent=`计划：${settings.enabled===false?'已停用':planText(settings.plan_json||state.plan?.schedule)}`;c.append(psummary);
 if(missing){const p=document.createElement('p');p.className='warn-text device-note';p.textContent='本次设备列表未找到，当前显示历史数据';c.append(p)}else if(old){const p=document.createElement('p');p.className='warn-text device-note';p.textContent='位置超过 1 小时，不能视为实时位置';c.append(p)}if(status){const p=document.createElement('p');p.className=/失败/.test(status)?'warn-text device-note':'muted device-note';p.textContent=status;c.append(p)}
 const foot=document.createElement('div');foot.className='device-foot';if(pt){const a=document.createElement('a');a.textContent='打开高德地图';a.target='_blank';a.rel='noopener noreferrer';a.href='https://uri.amap.com/marker?position='+encodeURIComponent(pt.gcj_lon+','+pt.gcj_lat)+'&name='+encodeURIComponent(d.name||d.device_id);foot.append(a)}else foot.append(document.createTextNode('暂无地图位置'));c.append(foot);$('#cards').append(c);}
 if(!devices.length)$('#cards').innerHTML='<div class="empty">暂无设备数据。请先运行一次 GitHub Actions。</div>';
}
async function refresh(){try{const r=await fetch('state.enc.json?t='+Date.now(),{cache:'no-store'});if(!r.ok)throw Error('unavailable');state=await decryptState(await r.json(),password);render();return true}catch(e){$('#status').hidden=false;$('#status').textContent=state?'刷新失败，仍显示上次解锁数据。':'解锁失败：请检查密码，或等待首次云端采集完成。';$('#status').className='status warn';return false;}}
$('#unlock').onsubmit=async e=>{e.preventDefault();const b=e.submitter;b.disabled=true;password=$('#password').value;try{if(await refresh())$('#password').value='';else password='';}finally{b.disabled=false;}};
$('#refresh').onclick=refresh;$('#lock').onclick=()=>{password='';state=null;$('#cards').replaceChildren();$('#summary').hidden=true;$('#device-section').hidden=true;$('#tools').hidden=true;$('#unlock').hidden=false;$('#status').hidden=true;$('#plantext').value='';$('#editor').close();};
$('#plans').onclick=()=>{
 const plan=state.plan||{};
 $('#plantext').value=JSON.stringify({
  schedule:plan.schedule||{},
  holidays:plan.holidays||{},
  devices:Object.fromEntries((state.devices||[]).map(d=>[d.device_id,d.settings||{}]))
 },null,2);
 $('#planerror').textContent='';
 $('#editor').showModal();
};$('#close').onclick=()=>$('#editor').close();
$('#download').onclick=()=>{try{const obj=JSON.parse($('#plantext').value);if(!obj.devices||typeof obj.devices!=='object')throw Error('需要 devices 对象');const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(obj,null,2)],{type:'application/json'}));a.download='cloud-plans.json';a.click();$('#planerror').textContent='已下载，提交工具将进一步校验计划。';}catch(e){$('#planerror').textContent='格式错误：'+e.message;}};
setInterval(()=>{if(password)refresh()},60000);
