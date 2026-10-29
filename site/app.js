import {createManagement} from './management.js';
import {decryptState} from './crypto.js';
import {devicePlanSummary,globalScheduleSummary} from './plan-summary.js';
const $=s=>document.querySelector(s);
function showView(name){
 const panels=[...(document.querySelectorAll?.('[data-view-panel]')||[])];
 if(!panels.some(panel=>panel.getAttribute('data-view-panel')===name))return;
 for(const panel of panels)panel.hidden=panel.getAttribute('data-view-panel')!==name;
 for(const button of document.querySelectorAll?.('[data-view]')||[]){
  const active=button.getAttribute('data-view')===name;
  button.classList.toggle('active',active);button.setAttribute('aria-current',active?'page':'false');
 }
 currentView=name;
}
for(const button of document.querySelectorAll?.('[data-view]')||[])button.onclick=()=>showView(button.getAttribute('data-view'));
let password='',state=null,sessionVersion=0,requestVersion=0,currentView='overview';
const management=createManagement({getState:()=>state,getPassword:()=>password,onRefresh:refresh,onOpenPlanJSON:openPlanJSON});
const date=s=>{const time=Date.parse(s);return Number.isFinite(time)?new Date(time).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'未知';};
const age=s=>{const time=Date.parse(s);return Number.isFinite(time)?Math.max(0,Date.now()-time):Infinity;};
const ageLabel=s=>{const elapsed=age(s);return Number.isFinite(elapsed)?`${Math.floor(elapsed/60000)} 分钟前`:'时间未知';};
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function fact(label,value){return `<div class="fact${label==='地址'?' fact-wide':''}"><label>${esc(label)}</label><b>${esc(value)}</b></div>`;}
function optionalNode(id) {
 try { return document.getElementById ? (document.getElementById(id) || document.querySelector('#' + id)) : document.querySelector('#' + id); }
 catch { return null; }
}
function statusLabel(value) {
 const map = {disabled:'未启用',not_configured:'未配置',ok:'正常',partial:'部分成功',error:'失败'};
 return map[value] || value || '未知';
}
function renderExtras(stale) {
 const nodes = {
  sources: optionalNode('overview-sources'),
  devices: optionalNode('overview-devices'),
  history: optionalNode('history-content'),
  mail: optionalNode('mail-content'),
  auth: optionalNode('auth-content')
 };
 const sources = state.sources && typeof state.sources === 'object' ? state.sources : {};
 const direct = sources.direct || {}, mail = sources.mail || {};
 if (nodes.sources) {
  const chips = [
   ['小米直连', direct.enabled === false ? '已关闭' : statusLabel(direct.status)],
   ['邮箱导入', mail.enabled === false ? '已关闭' : statusLabel(mail.status)],
   ['数据状态', state.error ? '需检查' : (stale ? '需检查' : '正常')]
  ];
  nodes.sources.innerHTML = chips.map(item => '<span class="status-chip ' + (item[0] === '数据状态' && item[1] !== '正常' ? 'warn' : '') + '">' + esc(item[0]) + '：' + esc(item[1]) + '</span>').join('');
 }
 const devices = state.devices || [];
 if (nodes.devices) {
  nodes.devices.innerHTML = devices.length ? devices.map(d => {
   const pt = d.last_point;
   const cls = d.query_status && /失败/.test(d.query_status) ? 'warn' : '';
   return '<article class="mini-device ' + cls + '"><div class="mini-head"><b>' + esc(d.name || d.device_id) + '</b><span class="tag">' + (d.online === true || d.online === 1 ? '在线' : d.online === false || d.online === 0 ? '离线' : '状态未知') + '</span></div>' +
    '<p>' + esc(pt ? date(pt.ts) : '暂无有效位置') + ' · 电量 ' + esc(d.battery == null ? '未知' : d.battery + '%') + '</p>' +
    '<p class="muted">' + esc(d.query_status || '尚未查询') + '</p></article>';
  }).join('') : '<div class="empty">暂无设备数据。</div>';
 }
 if (nodes.history) {
  const history = state.history && typeof state.history === 'object' ? state.history : {};
  const names = new Map(devices.map(d => [d.device_id, d.name || d.device_id]));
  const blocks = [];
  for (const d of devices) {
   const entry = history[d.device_id] || {};
   const points = Array.isArray(entry.points) ? entry.points : [];
   const recent = points.slice(-20).reverse();
   blocks.push('<section class="history-block"><div class="section-head"><div><h3>' + esc(d.name || d.device_id) + '</h3><p>已保存 ' + points.length + ' 个采样' + (entry.started_at ? ' · 自 ' + esc(date(entry.started_at)) : '') + '</p></div></div>' +
    (recent.length ? '<div class="history-list">' + recent.map(pt => '<div class="history-row"><span>' + esc(date(pt.ts)) + '</span><span>' + esc(pt.gcj_lat != null ? Number(pt.gcj_lat).toFixed(5) + ', ' + Number(pt.gcj_lon).toFixed(5) : '无坐标') + '</span><span>' + esc(pt.acc != null ? Math.round(pt.acc) + ' 米' : '精度未知') + '</span>' + (pt.gcj_lat != null ? '<a href="https://uri.amap.com/marker?position=' + encodeURIComponent(pt.gcj_lon + ',' + pt.gcj_lat) + '&name=' + encodeURIComponent(d.name || d.device_id) + '" target="_blank" rel="noopener noreferrer">地图</a>' : '<span class="muted">—</span>') + '</div>').join('') + '</div>' : '<p class="muted">暂无位置历史；需要成功采集后才会积累。</p>') + '</section>');
  }
  for (const key of Object.keys(history)) {
   if (names.has(key)) continue;
   const entry = history[key] || {}, points = Array.isArray(entry.points) ? entry.points : [];
   if (!points.length) continue;
   blocks.push('<section class="history-block"><h3>' + esc(key.startsWith('mail:') ? '邮件设备 ' + key.slice(5) : key) + '</h3><p class="muted">已保存 ' + points.length + ' 个采样（设备当前未列出）</p></section>');
  }
  nodes.history.innerHTML = blocks.length ? blocks.join('') : '<div class="empty">暂无位置历史。</div>';
 }
 if (nodes.mail) {
  const info = state.mail && typeof state.mail === 'object' ? state.mail : {};
  const status = statusLabel(info.status);
  const facts = [['状态', status], ['最近检查', info.checked_at ? date(info.checked_at) : '暂无'], ['检查邮件', info.messages], ['匹配邮件', info.matched], ['附件', info.attachments], ['导入位置', info.points], ['跳过邮件', info.skipped], ['无效位置', info.rejected_points]];
  nodes.mail.innerHTML = '<section class="auth-card"><h3>邮件导入状态</h3><div class="facts">' + facts.map(f => '<div class="fact"><label>' + esc(f[0]) + '</label><b>' + esc(f[1] == null ? '0' : f[1]) + '</b></div>').join('') + '</div>' +
   (info.status === 'disabled' || info.status === 'not_configured' ? '<p class="warn-text">邮箱来源尚未配置：需要 XM_MAIL_CONFIG Secret，并在“采集计划”中开启邮箱导入。只读导入不会主动命令设备定位。</p>' : '') +
   '<p class="muted">配置项：IMAP 主机与端口、账号、客户端授权码、文件夹、发件人/主题过滤、设备标签、附件格式。授权码只填在 GitHub Secrets，不要发到聊天。</p></section>';
 }
 if (nodes.auth) {
  const mailInfo = state.mail && typeof state.mail === 'object' ? state.mail : {};
  const directStatus = direct.enabled === false ? '已关闭' : statusLabel(direct.status);
  const directError = direct.error ? '（' + esc(direct.error) + '）' : '';
  const deviceLines = devices.map(d => '<li><b>' + esc(d.name || d.device_id) + '</b>：' + esc(d.query_status || '尚未查询') + ' · 最近查询 ' + esc(d.last_query ? date(d.last_query) : '暂无') + ' · 采样 ' + esc(d.last_point ? date(d.last_point.ts) : '暂无') + '</li>').join('');
  nodes.auth.innerHTML = '<section class="auth-card"><h3>小米直连授权</h3><p>状态：' + esc(directStatus) + directError + '</p>' +
   (state.error && /\b401\b/.test(state.error) ? '<p class="warn-text">已发布状态报告小米接口 HTTP 401，历史位置保留但新位置未恢复。请在本机扫码重新授权，并更新 GitHub Secrets 的 XM_TOKEN / XM_UID，再运行 Actions 的 collect。</p>' : '') +
   '<p class="muted">小米直接查询是真实接口查询，不是 AI 或模拟点击；云端会话可能过期，当前未实现自动续期。</p>' +
   '<div class="settings-links"><a href="http://127.0.0.1:8765/" target="_blank" rel="noopener noreferrer">打开本机授权工作台</a><a href="https://i.mi.com/find" target="_blank" rel="noopener noreferrer">小米云服务</a><a href="https://github.com/yanzaitian/locator-workbench/settings/secrets/actions" target="_blank" rel="noopener noreferrer">更新 XM_TOKEN / XM_UID</a></div>' +
   '<ul class="auth-device-list">' + deviceLines + '</ul></section>' +
   '<section class="auth-card"><h3>邮箱导入授权</h3><p>状态：' + esc(statusLabel(mailInfo.status)) + ' · 最近检查 ' + esc(mailInfo.checked_at ? date(mailInfo.checked_at) : '暂无') + '</p><p class="muted">仅读取已收到的定位附件，不主动定位。需要 XM_MAIL_CONFIG Secret。</p></section>' +
   '<section class="auth-card"><h3>GitHub 计划发布授权</h3><p>用途：网页提交采集计划时调用本仓库 Actions，不用于小米查询，也不能替代小米授权。</p><p class="muted">Token 仅在“采集计划”页面输入并临时使用；不在本页展示或保存。需要 Actions 读写与 Metadata 只读权限。</p><a href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noopener noreferrer">创建 GitHub 细粒度 Token</a></section>' +
   '<section class="auth-card"><h3>数据健康</h3><p>最后更新时间：' + esc(state.updated ? date(state.updated) : '未知') + ' · ' + (stale ? '超过 30 分钟未更新，需检查 Actions' : '近期更新') + '</p>' + (state.error ? '<p class="warn-text">' + esc(state.error) + '</p>' : '') + '</section>';
 }
}
function deviceDetails(d, stamp) {
 const pt=d.last_point, details=document.createElement('details');details.className='device-inspector';details.setAttribute('data-device-id',d.device_id);
 const heading=document.createElement('summary');heading.textContent='查看设备详情与采集说明';details.append(heading);
 const body=document.createElement('div');body.className='inspector-body';
 const listed=!!stamp&&d.listed_at===stamp;
 const histEntry=(state&&state.history&&state.history[d.device_id])||{};const histPoints=Array.isArray(histEntry.points)?histEntry.points:[];const historyLabel=histPoints.length?histPoints.length+' 个 · 最近 '+date(histPoints[histPoints.length-1].ts):'暂无采样';
 const rows=[['设备标识',d.device_id],['数据来源',d.device_id.startsWith('mail:')?'邮件导入':'小米云服务'],['设备列表核验',listed?'本次设备列表已确认':'未在本次列表确认，可能为历史数据'],['最近列表确认',date(d.listed_at)],['最近查询尝试',date(d.last_query)],['查询结果',d.query_status||'尚未查询'],['最后位置采样',pt?date(pt.ts):'暂无有效位置'],['位置距今',pt?ageLabel(pt.ts):'未知'],['状态说明','在线状态、电量和地址是最近保存的结果，并非实时连接'],['历史采样',historyLabel]];
 body.insertAdjacentHTML('beforeend','<div class="detail-facts">'+rows.map(([label,value])=>fact(label,value)).join('')+'</div>');
 const guide=document.createElement('p');guide.className='muted';guide.textContent='查询失败时请前往「系统设置 → 小米定位采集授权」。刷新数据只读取已发布结果，不会立即向手机发起定位。所有时间按北京时间显示。';body.append(guide);
 details.append(body);return details;
}
function render(){
 const updatedKnown=Number.isFinite(Date.parse(state.updated)), devices=state.devices||[], stale=!updatedKnown||age(state.updated)>30*60000, located=devices.filter(d=>d.last_point).length, failures=devices.filter(d=>d.query_status&&/失败|未找到/.test(d.query_status)).length;
 $('#unlock').hidden=true;$('#overview').hidden=false;$('#tools').hidden=false;$('#primary-navigation').hidden=false;$('#sidebar').setAttribute('data-locked','false');$('#status').hidden=false;$('#summary').hidden=false;$('#device-section').hidden=false;
 $('#status').textContent='数据更新时间：'+date(state.updated)+(stale?(updatedKnown?'。超过 30 分钟未更新，请检查 Actions。':'。更新时间缺失或格式无效，请检查 Actions。'):'')+(state.error?'。'+state.error:'');$('#status').className='status'+(stale||state.error?' warn':'');$('#updated').textContent=updatedKnown?'最后更新 '+date(state.updated):'最后更新时间未知';
 if(state.error){const help=document.createElement('button');help.type='button';help.className='button button-secondary connection-shortcut';help.textContent='查看连接状态与授权步骤';help.onclick=()=>showView('settings');$('#status').append(help);}
 $('#summary').innerHTML=[['设备总数',devices.length],['已有位置',located],['查询异常',failures],['数据状态',stale||state.error||failures?'需检查':'正常']].map(x=>`<div class="metric"><strong>${x[1]}</strong><span>${x[0]}</span></div>`).join('');
 const scheduleSummary=$('#schedule-summary');scheduleSummary.textContent=globalScheduleSummary(state.plan?.schedule,state.plan?.holidays);scheduleSummary.hidden=false;
 const openDetails=new Set([...(document.querySelectorAll?.('.device-inspector[open]')||[])].map(node=>node.getAttribute('data-device-id')));
 $('#cards').replaceChildren();
 for(const d of devices){const pt=d.last_point,missing=!d.device_id.startsWith('mail:')&&d.listed_at!==state.updated,old=pt&&age(pt.ts)>3600000,problem=missing||old||/失败/.test(d.query_status||'');const c=document.createElement('article');c.className='device '+(problem?'warn':'ok');
 const h=document.createElement('h3');h.textContent=d.name||d.device_id;c.append(h);const tag=document.createElement('span');tag.className='tag';tag.textContent=d.online===true||d.online===1?'在线':d.online===false||d.online===0?'离线':'状态未知';c.append(tag);
 const status=d.query_status||'尚未查询';const address=d.address||pt?.address||'';c.insertAdjacentHTML('beforeend','<div class="facts">'+fact('本次查询',date(d.last_query))+fact('位置时间',pt?date(pt.ts):'暂无有效位置')+fact('电量',d.battery==null?'未知':d.battery+'%')+fact('精度',pt&&pt.acc!=null?Math.round(pt.acc)+' 米':'未知')+fact('地址',address||'暂无地址')+'</div>');
 const psummary=document.createElement('p');psummary.className='plan-summary';psummary.textContent=`计划：${devicePlanSummary(d.settings||{},state.plan?.schedule,state.plan?.holidays)}`;if(psummary.textContent.length>120){const detail=document.createElement('details');detail.className='device-plan-details';const title=document.createElement('summary');title.textContent='独立采集计划 · 查看完整规则';detail.append(title,psummary);c.append(detail);}else c.append(psummary);
 if(missing){const p=document.createElement('p');p.className='warn-text device-note';p.textContent='本次设备列表未找到，当前显示历史数据';c.append(p)}else if(old){const p=document.createElement('p');p.className='warn-text device-note';p.textContent='位置超过 1 小时，不能视为实时位置';c.append(p)}if(status){const p=document.createElement('p');p.className=/失败/.test(status)?'warn-text device-note':'muted device-note';p.textContent=status;c.append(p)}
 const foot=document.createElement('div');foot.className='device-foot';if(pt){const a=document.createElement('a');a.textContent='打开高德地图';a.target='_blank';a.rel='noopener noreferrer';a.href='https://uri.amap.com/marker?position='+encodeURIComponent(pt.gcj_lon+','+pt.gcj_lat)+'&name='+encodeURIComponent(d.name||d.device_id);foot.append(a)}else foot.append(document.createTextNode('暂无地图位置'));const inspector=deviceDetails(d,state.updated);inspector.open=openDetails.has(d.device_id);c.append(foot,inspector);$('#cards').append(c);}
 renderExtras(stale);management.render();showView(currentView);
 if(!devices.length)$('#cards').innerHTML='<div class="empty">暂无设备数据。请先运行一次 GitHub Actions。</div>';
}
function classifyLoadError(error){
 switch(error?.kind){
  case 'network':return '无法连接云端：请检查网络后重试。';
  case 'missing':return '云端尚未生成定位数据：需管理员检查 GitHub Actions 运行结果及小米凭据，采集并发布成功后再重试。';
  case 'http':return `云端读取失败（HTTP ${error.status}）：请稍后重试或检查 Pages 发布状态。`;
  case 'json':return '云端状态文件不是有效 JSON：请检查最近一次 Actions 发布。';
  case 'decrypt':return '无法解密状态：密码不正确，或云端加密数据损坏/版本不兼容。';
  case 'state':return '解密成功但状态结构无效：请检查最近一次云端采集。';
  default:return '读取云端状态失败：请稍后重试。';
 }
}
async function loadState(credential){
 let response;
 try{response=await fetch('state.enc.json?t='+Date.now(),{cache:'no-store'});}catch{throw {kind:'network'};}
 if(response.status===404)throw {kind:'missing'};
 if(!response.ok)throw {kind:'http',status:response.status};
 let envelope;
 try{envelope=await response.json();}catch{throw {kind:'json'};}
 let decoded;
 try{decoded=await decryptState(envelope,credential);}catch{throw {kind:'decrypt'};}
 if(!decoded||typeof decoded!=='object'||!Array.isArray(decoded.devices))throw {kind:'state'};
 return decoded;
}
async function refresh(){
 if(!password)return false;
 const credential=password,session=sessionVersion,request=++requestVersion;
 try{
  const loaded=await loadState(credential);
  if(session!==sessionVersion||request!==requestVersion||credential!==password||!password)return false;
  state=loaded;render();return true;
 }catch(error){
  if(session!==sessionVersion||request!==requestVersion||credential!==password||!password)return false;
  const reason=classifyLoadError(error);
  $('#status').hidden=false;
  $('#status').textContent=state?`刷新失败：${reason}仍显示 ${date(state.updated)} 的旧数据（${ageLabel(state.updated)}）。`:reason;
  $('#status').className='status warn';return false;
 }
}
$('#unlock').onsubmit=async e=>{
 e.preventDefault();const b=e.submitter||$('#unlock button');if(b.disabled)return;
 b.disabled=true;password=$('#password').value;const session=++sessionVersion;
 try{if(await refresh()){if(session===sessionVersion)$('#password').value='';}else if(session===sessionVersion){password='';$('#password').value='';}}
 finally{if(session===sessionVersion)b.disabled=false;}
};
$('#refresh').onclick=refresh;
$('#lock').onclick=()=>{
 management.lock();sessionVersion++;requestVersion++;$('#unlock button').disabled=false;password='';state=null;
 $('#sidebar').setAttribute('data-locked','true');$('#primary-navigation').hidden=true;$('#overview').hidden=true;for(const panel of document.querySelectorAll?.('[data-view-panel]')||[])panel.hidden=true;for(const button of document.querySelectorAll?.('[data-view]')||[]){const active=button.getAttribute('data-view')==='overview';button.classList.toggle('active',active);button.setAttribute('aria-current',active?'page':'false');}currentView='overview';
 $('#cards').replaceChildren();$('#summary').replaceChildren();$('#summary').hidden=true;$('#schedule-summary').textContent='';$('#schedule-summary').hidden=true;$('#device-section').hidden=true;$('#tools').hidden=true;
 $('#updated').textContent='';$('#status').textContent='';$('#status').className='status';$('#status').hidden=true;
 for(const id of ['overview-sources','overview-devices','history-content','mail-content','auth-content']){const node=optionalNode(id);if(node)node.replaceChildren();}
 $('#unlock').hidden=false;$('#password').value='';$('#plantext').value='';$('#planerror').textContent='';if($('#editor').open)$('#editor').close();$('#password').focus();
};
function openPlanJSON(){
 if(!state)return;
 const plan=state.plan||{};
 $('#plantext').value=JSON.stringify({schedule:plan.schedule||{},holidays:plan.holidays||{},devices:Object.fromEntries((state.devices||[]).map(d=>[d.device_id,d.settings||{}]))},null,2);
 $('#planerror').textContent='';$('#editor').showModal();
}
$('#close').onclick=()=>$('#editor').close();
$('#download').onclick=()=>{try{const obj=JSON.parse($('#plantext').value);if(!obj.devices||typeof obj.devices!=='object'||Array.isArray(obj.devices))throw Error('需要 devices 对象');const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(obj,null,2)],{type:'application/json'}));a.download='cloud-plans.json';a.click();$('#planerror').textContent='已下载 JSON；网页不会直接保存到仓库。请使用部署工具提交计划，云端采集发布后才会生效。';}catch(e){$('#planerror').textContent='格式错误：'+e.message;}};
setInterval(()=>{if(password&&state)refresh()},60000);

