import {encryptState, decryptState} from './crypto.js';
import {devicePlanSummary, globalScheduleSummary} from './plan-summary.js';

const CACHE_KEY = 'locator-admin-v1';
const DISPATCH_URL = 'https://api.github.com/repos/yanzaitian/locator-workbench/actions/workflows/locator.yml/dispatches';
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const copy = value => structuredClone(value);
const hm = /^(?:[01]\d|2[0-3]):[0-5]\d$/;

// Keep the original schedule, including weekday/holiday rules and future fields.
export function plansFromState(state) {
 const plan = object(state?.plan) ? copy(state.plan) : {};
 plan.schedule ??= {};
 plan.holidays ??= {};
 plan.devices = object(plan.devices) ? plan.devices : {};
 for (const device of state?.devices || []) {
  if (typeof device.device_id === 'string' && !(device.device_id in plan.devices)) {
   plan.devices[device.device_id] = copy(device.settings || {});
  }
 }
 return plan;
}
function ownPlan(settings) {
 let raw = settings.plan_json;
 if (typeof raw === 'string') {
  try { raw = raw.trim() ? JSON.parse(raw) : null; }
  catch { throw Error('独立计划 JSON 无效，请先修复原计划；不会自动覆盖。'); }
 }
 if (raw == null) return null;
 if (!object(raw)) throw Error('独立计划必须为对象；不会自动覆盖。');
 return Object.keys(raw).length ? raw : null;
}
export function draftForDevice(plans, id) {
 const settings = plans.devices[id] || {}, own = ownPlan(settings);
 const global = object(plans.schedule) && Object.keys(plans.schedule).length ? plans.schedule : {default:{start:'00:00',end:'00:00',interval_minutes:10}};
 const slot = (own || global).default || {};
 return {enabled:settings.enabled !== false, inherit:!own,
  start:slot.start || '08:00', end:slot.end || '22:00',
  interval:String(own ? slot.interval_minutes ?? 60 : settings.interval_minutes ?? slot.interval_minutes ?? 60),
  slotChanged:false, inheritChanged:false};
}
export function validateDraft(draft) {
 if (typeof draft.enabled !== 'boolean' || typeof draft.inherit !== 'boolean') throw Error('计划开关格式错误。');
 if (!draft.inherit && (!hm.test(draft.start) || !hm.test(draft.end))) throw Error('开始和结束时间须为有效的 HH:MM。');
 if (!/^\d+$/.test(String(draft.interval)) || !Number.isInteger(Number(draft.interval)) || Number(draft.interval) < 10 || Number(draft.interval) > 10080) {
  throw Error('定位间隔须为 10 至 10080 的整数分钟。');
 }
}
export function buildPlans(base, edits) {
 const plans = copy(base);
 for (const [id, draft] of edits) {
  validateDraft(draft);
  const settings = plans.devices[id] || {};
  const own = ownPlan(settings);
  settings.enabled = draft.enabled;
  if (draft.inherit) {
   if (own) settings.plan_json = null;
   // Preserve an absent interval override when only toggling the device switch.
   if (draft.slotChanged || draft.inheritChanged) settings.interval_minutes = Number(draft.interval);
  } else {
   let schedule = own ? copy(own) : copy(plans.schedule);
   if (!object(schedule) || !Object.keys(schedule).length) schedule = {enabled:true,skip_holidays:false};
   if (!own || draft.slotChanged) {
    schedule.default = {...schedule.default,start:draft.start,end:draft.end,interval_minutes:Number(draft.interval)};
   }
   settings.plan_json = typeof settings.plan_json === 'string' ? JSON.stringify(schedule) : schedule;
  }
  plans.devices[id] = settings;
 }
 return plans;
}
export async function makeDispatchBody(plans, password, baseRevision, requestId = crypto.randomUUID()) {
 const payload = await encryptState({kind:'locator-plans-v1',request_id:requestId,
  created_at:new Date().toISOString(),base_revision:baseRevision || '',plans},password);
 return {requestId,body:{ref:'main',inputs:{operation:'save_plans',payload:JSON.stringify(payload)}}};
}
export function normalizeReports(value, depth = 0) {
 if (depth > 4 || value == null) return [];
 if (Array.isArray(value)) return value.flatMap(item => normalizeReports(item,depth + 1));
 if (!object(value)) return [];
 if ('device_id' in value || 'content' in value) return [value];
 return Object.values(value).flatMap(item => normalizeReports(item,depth + 1));
}
const HTTP_MESSAGES = {401:'GitHub 授权无效或已到期，请重新授权。',403:'GitHub 授权权限不足或请求受限，请检查 Actions 写入权限。',404:'未找到工作流或授权无仓库访问权限。',422:'GitHub 未接受工作流参数，请检查分支与工作流配置。',429:'GitHub 请求过于频繁，请稍后重试。'};
export function httpMessage(status) {
 return `HTTP ${Number.isInteger(status) ? status : 0}：${HTTP_MESSAGES[status] || 'GitHub 请求失败，请稍后重试。'}`;
}

export function createManagement({getState,getPassword,onRefresh,onOpenPlanJSON}) {
 let root, ui, baseline, revision = '', edits = new Map(), token = '', pending = null;
 let generation = 0, busy = false, restoring = false, active = false, editVersion = 0;
 let reportSignature = null, reportCards = new Map(), reportItems = [];
 const valid = version => active && generation === version;
 const el = (tag,text,className) => {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
 };
 function message(text) { if (ui) ui.status.textContent = text; }
 function settingsMessage(text) { if (ui) ui.settingsStatus.textContent = text; }
 function renderPlanSummary() {
  ui.planSummary.replaceChildren();
  const id = ui.device.value;
  if (!id || !baseline?.devices[id]) return;
  const settings = baseline.devices[id], own = ownPlan(settings);
  ui.planSummary.append(el('h3','当前已发布规则'),el('p',devicePlanSummary(settings,baseline.schedule,baseline.holidays),'muted'));
  if (!own) ui.planSummary.append(el('p',globalScheduleSummary(baseline.schedule,baseline.holidays),'muted'));
  const effective = own || baseline.schedule;
  if (object(effective?.days) && Object.keys(effective.days).length) ui.planSummary.append(el('p','存在星期覆盖：修改默认时段不会改变星期专属规则。','warn-text'));
  if (effective?.skip_holidays !== false) ui.planSummary.append(el('p','节假日和调休规则可能覆盖默认时段，请同时核对上述已发布规则。','muted'));
  if (edits.has(id)) ui.planSummary.append(el('p','当前表单有未保存草稿；以上仍为已发布规则。','warn-text'));
  if (own && edits.get(id)?.inherit) ui.planSummary.append(el('p','提交继承全局后，将移除该设备的独立周历及星期覆盖。','warn-text'));
 }
 function clearToken() { token = ''; if (ui) ui.token.value = ''; }
 function field(label,type) {
  const wrap = el('label',null,'management-field'), input = el('input');
  input.type = type; wrap.append(el('span',label),input);
  return {wrap,input};
 }
 function currentDraft() {
  const id = ui.device.value;
  if (!id) return null;
  return edits.get(id) || draftForDevice(baseline,id);
 }
 function loadDevice() {
  try {
   ui.planSummary.replaceChildren();
   const draft = currentDraft(); ui.formError.textContent = '';
   renderPlanSummary();
   ui.enabled.disabled = ui.inherit.disabled = !draft;
   if (!draft) { ui.start.value = ui.end.value = ui.interval.value = ''; return; }
   for (const key of ['enabled','inherit']) ui[key].checked = draft[key];
   for (const key of ['start','end','interval']) ui[key].value = draft[key];
   ui.start.disabled = ui.end.disabled = draft.inherit;
   ui.interval.disabled = false;
  } catch (error) {
   ui.formError.textContent = error.message;
   for (const key of ['enabled','inherit','start','end','interval']) { ui[key].disabled = true; ui[key].value = ''; }
  }
 }
 function record(key) {
  try {
   const draft = copy(currentDraft()); if (!draft) return;
   draft[key] = key === 'enabled' || key === 'inherit' ? ui[key].checked : ui[key].value;
   if (['start','end','interval'].includes(key)) draft.slotChanged = true;
   if (key === 'inherit') draft.inheritChanged = true;
   edits.set(ui.device.value,draft); editVersion++;
   ui.start.disabled = ui.end.disabled = draft.inherit;
   renderPlanSummary();
   message('有未保存的计划修改。');
  } catch { message('原计划格式无效，未修改。'); }
 }
 function acceptState(state) {
  const selected = ui.device.value;
  baseline = plansFromState(state); revision = state.plan_request_id || '';
  ui.device.replaceChildren();
  const names = new Map((state.devices || []).map(d => [d.device_id,d.name || d.device_id]));
  const editable = Object.keys(baseline.devices).filter(id => !id.startsWith('mail:') && !(state.devices || []).some(d => d.device_id === id && d.source === 'mail'));
  for (const id of editable) {
   const option = el('option',names.get(id) || id); option.value = id; ui.device.append(option);
  }
  const ids = editable;
  ui.device.value = ids.includes(selected) ? selected : ids[0] || '';
  loadDevice();
  ui.save.disabled = !ui.device.value || busy;
 }
 function createUI() {
  root.classList.add('management'); root.hidden = false;
  const section = el('section',null,'management-page management-plans');
  section.id = 'view-plans'; section.setAttribute('data-view-panel','plans');
  section.append(el('h2','采集计划'));
  const planTools = el('div',null,'management-actions');
  const exportPlan = el('button','导出计划 JSON','button button-secondary'); exportPlan.type = 'button';
  exportPlan.onclick = () => onOpenPlanJSON?.();
  planTools.append(exportPlan); section.append(planTools);
  const form = el('form',null,'management-form');
  const pick = el('label',null,'management-field'); pick.append(el('span','选择设备'));
  const device = el('select'); device.setAttribute('aria-label','选择设备'); pick.append(device);
  const enabled = field('启用设备','checkbox'), inherit = field('继承全局计划','checkbox');
  const start = field('独立计划开始时间','time'), end = field('独立计划结束时间','time'), interval = field('设备间隔（分钟）','number');
  interval.input.min = '10'; interval.input.max = '10080'; interval.input.step = '1';
  const grid = el('div',null,'management-grid'); grid.append(pick,enabled.wrap,inherit.wrap,start.wrap,end.wrap,interval.wrap);
  const note = el('p','时段按北京时间执行；开始与结束相同表示全天，支持跨午夜。普通表单只修改 default，保留星期覆盖、节假日规则和计划总开关。勾选继承会移除该设备的独立周历；间隔可覆盖全局。','muted');
  const auth = field('GitHub 授权 Token（到期后须重新授权）','password');
  auth.input.setAttribute('aria-label','GitHub 授权 Token');
  auth.input.autocomplete = 'off'; auth.input.spellcheck = false;
  const remember = field('记住授权：仅在本机保存加密密文（默认关闭）','checkbox');
  remember.input.checked = false;
  const restore = el('button','恢复本机加密授权'), forget = el('button','清除本机授权');
  const save = el('button','加密提交计划','primary'), refresh = el('button','刷新发布状态'), reset = el('button','放弃草稿并载入最新计划');
  save.type = 'submit'; for (const button of [restore,forget,refresh,reset]) button.type = 'button';
  const actions = el('div',null,'management-actions'); actions.append(save,refresh,reset);
  const authActions = el('div',null,'management-actions'); authActions.append(restore,forget);
  const status = el('p',null,'management-status'), formError = el('p',null,'warn-text');
  status.setAttribute('role','status'); status.setAttribute('aria-live','polite');
  const planSummary = el('div',null,'plan-rule-summary');
  form.append(grid,planSummary,note,formError,actions,status);
  section.append(form);
  const reports = el('section',null,'management-page management-reports');
  reports.id = 'view-reports'; reports.setAttribute('data-view-panel','reports');
  const settings = el('section',null,'management-page management-settings');
  settings.id = 'view-settings'; settings.setAttribute('data-view-panel','settings');
  settings.append(el('h2','系统设置'));
  const settingsCard = el('div',null,'settings-card');
  settingsCard.append(el('h3','GitHub 计划发布授权'),el('p','授权仅用于本页面向 GitHub 提交加密计划。需要仓库 Actions 写入权限；授权到期须重新输入。访问密码不会存储。'),auth.wrap,remember.wrap,authActions);
  const settingsStatus = el('p',null,'management-status');
  settingsStatus.setAttribute('role','status'); settingsStatus.setAttribute('aria-live','polite');
  settingsCard.append(settingsStatus); settings.append(settingsCard);
  const mail = el('section',null,'device management-mail settings-card');
  settings.append(mail);
  root.replaceChildren(section,reports,settings);
  ui = {section,form,device,enabled:enabled.input,inherit:inherit.input,start:start.input,end:end.input,interval:interval.input,token:auth.input,remember:remember.input,save,refresh,reset,restore,forget,status,settingsStatus,planSummary,formError,reports,mail};
  device.onchange = loadDevice;
  for (const key of ['enabled','inherit','start','end','interval']) ui[key].oninput = () => record(key);
  auth.input.oninput = () => { token = auth.input.value.trim(); };
  remember.input.onchange = () => {
   if (!remember.input.checked) { try { localStorage.removeItem(CACHE_KEY); } catch { settingsMessage('无法清除本机授权，请检查浏览器存储权限。'); } }
  };
  forget.onclick = () => {
   clearToken(); remember.input.checked = false;
   try { localStorage.removeItem(CACHE_KEY); settingsMessage('本机授权已清除。'); } catch { settingsMessage('无法清除本机授权，请检查浏览器存储权限。'); }
  };
  restore.onclick = restoreToken;
  reset.onclick = () => {
   const state = getState(); if (!state || !active || busy) return;
   edits.clear(); editVersion++; acceptState(state); message('已载入最新计划。');
  };
  refresh.onclick = refreshState;
  form.onsubmit = submit;
 }
 async function restoreToken() {
  if (restoring || busy) return;
  const version = generation; let password = getPassword();
  if (!password) { settingsMessage('请先解锁工作台。'); return; }
  restoring = true; const target = ui; target.restore.disabled = true;
  try {
   const raw = localStorage.getItem(CACHE_KEY);
   if (!raw) { settingsMessage('本机没有保存的加密授权。'); return; }
   let restored = await decryptState(JSON.parse(raw),password); password = '';
   if (!valid(version)) { restored = null; return; }
   if (typeof restored?.token !== 'string' || !restored.token.trim()) throw Error();
   token = restored.token.trim(); restored = null; ui.token.value = token; ui.remember.checked = true;
   settingsMessage('已恢复加密授权；授权到期仍须重新授权。');
  } catch { if (valid(version)) settingsMessage('无法恢复授权，请重新输入 Token。'); }
  finally { password = ''; if (valid(version)) { restoring = false; target.restore.disabled = false; } }
 }
 async function refreshState() {
  const version = generation; if (ui.refresh.disabled) return;
  ui.refresh.disabled = true;
  try { await onRefresh?.(); if (valid(version)) render(); }
  catch { if (valid(version)) message('刷新失败，请稍后重试。'); }
  finally { if (valid(version)) ui.refresh.disabled = false; }
 }
 async function submit(event) {
  event.preventDefault(); if (busy || restoring || !active) return;
  const state = getState(); if (!state) return;
  if ((state.plan_request_id || '') !== revision) { message('云端计划版本已变化。草稿已保留，请先放弃草稿并载入最新计划，再重新编辑。'); return; }
  let plans;
  try { plans = buildPlans(baseline,edits); }
  catch (error) { message(error.message); return; }
  let password = getPassword(); const authorization = token || ui.token.value.trim();
  if (!password || !authorization) { password = ''; message('请先解锁并输入 GitHub 授权 Token。'); return; }
  const version = generation, submittedEdit = editVersion, remember = ui.remember.checked;
  busy = true; ui.save.disabled = true; ui.reset.disabled = true;
  message('正在加密并提交计划…');
  try {
   const {requestId,body} = await makeDispatchBody(plans,password,revision);
   if (!valid(version)) return;
   if (remember) {
    const encrypted = await encryptState({token:authorization},password);
    if (!valid(version)) return;
    if (ui.remember.checked) localStorage.setItem(CACHE_KEY,JSON.stringify(encrypted));
   }
   password = '';
   if (!valid(version)) return;
   const response = await fetch(DISPATCH_URL,{method:'POST',headers:{
    Accept:'application/vnd.github+json',Authorization:`Bearer ${authorization}`,
    'Content-Type':'application/json','X-GitHub-Api-Version':'2022-11-28'},body:JSON.stringify(body)});
   if (!valid(version)) return;
   if (response.status !== 204) {
    if (response.status === 401) clearToken();
    message(httpMessage(response.status)); return;
   }
   pending = {requestId,submittedEdit};
   message('已提交，等待云端发布；尚未确认生效。');
  } catch { if (valid(version)) message('加密、存储或网络请求失败，未确认提交，请检查后重试。'); }
  finally { password = ''; if (valid(version)) { busy = false; ui.save.disabled = !ui.device.value; ui.reset.disabled = false; } }
  if (valid(version)) render();
 }
 function reportPreview(report) { return report.status === 'preview' || report.is_final === false || report.is_preview === true; }
 function reportCard(report,name,preview) {
   const card = el('details',null,'device management-report');
   card.append(el('summary',`${report.month || '月份未知'} · ${name} · ${preview ? '本月预览' : '月报'}`));
   card.append(el('span',preview ? '本月预览' : '月报', 'tag'));
   const source = report.ai_status === 'ok' ? '本地统计 + DeepSeek 说明' : ({local:'本地模板',template:'本地模板',deepseek:'DeepSeek'}[report.source] || '未知');
   card.append(el('p',`来源：${source} · 生成时间：${safeDate(report.generated_at)}`,'muted'));
   const content = typeof report.content === 'string' ? report.content : '报告内容暂不可用。';
   const markdown = el('pre',content,'management-markdown');
   markdown.tabIndex = 0; markdown.setAttribute('aria-label',`${report.month || '月份未知'} ${name} 月报内容`);
   card.append(markdown);
   if (report.ai_status === 'ok' && Array.isArray(report.ai_notes)) for (const note of report.ai_notes) { if (typeof note === 'string') card.append(el('p',note,'muted')); }
   if (report.ai_status === 'failed' || report.ai_status === 'invalid_response') card.append(el('p','DeepSeek 说明暂不可用，统计报告已保留。','muted'));
   const download = el('button','下载 Markdown'); download.type = 'button';
   download.onclick = () => {
    if (!active) return;
    const url = URL.createObjectURL(new Blob([content],{type:'text/markdown;charset=utf-8'}));
    const a = el('a'); a.href = url;
    a.download = `${report.month || 'month'}-${report.device_id || 'device'}`.replace(/[^\p{L}\p{N}_.-]/gu,'_') + '.md';
    a.click(); URL.revokeObjectURL(url);
   };
   card.append(download); return card;
 }
 function filterReports() {
  const selected = reportItems.filter(item => (!ui.reportDevice.value || item.device === ui.reportDevice.value) &&
   (!ui.reportMonth.value || item.month === ui.reportMonth.value) && (!ui.reportType.value || item.type === ui.reportType.value));
  ui.reportResults.replaceChildren();
  if (!selected.length) {
   ui.reportResults.append(el('p',reportItems.length ? '没有符合筛选条件的月报。' : '云端刚开始积累定位记录，下月将自动生成月报。','empty')); return;
  }
  let month, group;
  for (const item of selected) {
   if (month !== item.month) {
    month = item.month; group = el('section',null,'report-month-group');
    group.append(el('h3',month || '月份未知')); ui.reportResults.append(group);
   }
   group.append(item.card);
  }
 }
 function renderReports(state) {
  const reports = normalizeReports(state.reports).sort((a,b)=>String(b.month || '').localeCompare(String(a.month || '')));
  const names = new Map((state.devices || []).map(device => [device.device_id,device.name || device.device_id]));
  const signature = JSON.stringify(reports.map(report => [report,names.get(report.device_id)]));
  if (reportSignature === signature) return;
  if (!ui.reportResults) {
   ui.reports.append(el('h2','月报'));
   const filters = el('div',null,'report-filters');
   for (const [key,label] of [['reportDevice','月报设备'],['reportMonth','月报月份'],['reportType','月报类型']]) {
    const wrap = el('label',null,'management-field'), select = el('select');
    select.setAttribute('aria-label',label); wrap.append(el('span',label),select); filters.append(wrap);
    ui[key] = select; select.onchange = filterReports;
   }
   ui.reportResults = el('div'); ui.reports.append(filters,ui.reportResults);
  }
  function options(select, entries) {
   const previous = select.value;
   select.replaceChildren();
   for (const [value,label] of [['','全部'],...entries]) { const option = el('option',label); option.value = value; select.append(option); }
   select.value = entries.some(([value]) => value === previous) ? previous : '';
  }
  options(ui.reportDevice,[...new Set(reports.map(report => report.device_id || ''))].filter(Boolean).map(id => [id,names.get(id) || id]));
  options(ui.reportMonth,[...new Set(reports.map(report => report.month || ''))].filter(Boolean).map(month => [month,month]));
  options(ui.reportType,[['preview','本月预览'],['final','正式月报']]);
  const next = new Map(), counts = new Map();
  reportItems = reports.map(report => {
   const preview = reportPreview(report), name = names.get(report.device_id) || report.device_id || '设备未知';
   const base = JSON.stringify([report.device_id,report.month,preview]), index = counts.get(base) || 0;
   counts.set(base,index + 1);
   const key = base + ':' + index, contentSignature = JSON.stringify([report,name]), previous = reportCards.get(key);
   const card = previous?.signature === contentSignature ? previous.card : reportCard(report,name,preview);
   if (previous && card !== previous.card) card.open = previous.card.open;
   next.set(key,{signature:contentSignature,card});
   return {device:report.device_id || '',month:report.month || '',type:preview ? 'preview' : 'final',card};
  });
  reportCards = next; reportSignature = signature; filterReports();
 }
 function renderMail(state) {
  const mail = object(state.mail) ? state.mail : {};
  const status = {disabled:'未启用',not_configured:'尚未配置',ok:'导入完成',partial:'部分内容已跳过',error:'导入失败'}[mail.status] || '暂无状态';
  ui.mail.replaceChildren(el('h2','邮件导入状态'),el('p',`状态：${status} · 最近检查：${safeDate(mail.checked_at)}`));
  const facts = el('div',null,'facts');
  for (const [key,label] of [['messages','检查邮件'],['matched','匹配邮件'],['attachments','附件'],['points','导入位置'],['skipped','跳过邮件'],['rejected_points','无效位置']]) {
   const count = Number.isSafeInteger(mail[key]) && mail[key] >= 0 ? mail[key] : 0;
   const fact = el('div',null,'fact'); fact.append(el('span',label),el('b',String(count))); facts.append(fact);
  }
  ui.mail.append(facts);
 }
 function render() {
  const container = document.getElementById('management'); if (!container) return;
  const state = getState(); if (!state || !getPassword()) { lock(); return; }
  active = true;
  if (!ui || root !== container) { root = container; createUI(); acceptState(state); }
  if (pending && state.plan_request_id === pending.requestId) {
   const untouched = editVersion === pending.submittedEdit;
   pending = null;
   if (untouched) { edits.clear(); acceptState(state); }
   else { baseline = plansFromState(state); revision = state.plan_request_id || ''; renderPlanSummary(); }
   message(untouched ? '已生效：云端已发布本次计划。' : '已生效：云端已发布上次提交；当前仍有未保存修改。');
  } else if (!edits.size && !busy && !pending) acceptState(state);
  if (edits.size && revision !== (state.plan_request_id || '')) message('云端计划版本已变化，草稿已保留。请先放弃草稿并载入最新计划，再重新编辑。');
  renderReports(state); renderMail(state);
 }
 function lock() {
  generation++; active = false; clearToken();
  if (ui) {
   for (const key of ['start','end','interval','token']) ui[key].value = '';
   ui.enabled.checked = ui.inherit.checked = ui.remember.checked = false;
  }
  const container = root || document.getElementById('management');
  if (container) { container.replaceChildren(); container.hidden = true; }
  ui = null; root = null; baseline = null; revision = ''; edits.clear(); pending = null;
  busy = restoring = false; editVersion = 0;
  reportSignature = null; reportCards.clear(); reportItems = [];
 }
 return {render,lock};
}
function safeDate(value) {
 if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T/.test(value) || !Number.isFinite(Date.parse(value))) return '暂无';
 return new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
}
