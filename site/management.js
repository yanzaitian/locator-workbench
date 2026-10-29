import {encryptState, decryptState} from './crypto.js';
import {devicePlanSummary, globalScheduleSummary} from './plan-summary.js';

const CACHE_KEY = 'locator-admin-v1';
const REPOSITORY_URL = 'https://github.com/yanzaitian/locator-workbench';
const SECRETS_URL = `${REPOSITORY_URL}/settings/secrets/actions`;
const ACTIONS_URL = `${REPOSITORY_URL}/actions/workflows/locator.yml`;
const DISPATCH_URL = 'https://api.github.com/repos/yanzaitian/locator-workbench/actions/workflows/locator.yml/dispatches';
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const copy = value => structuredClone(value);
const hm = /^(?:[01]\d|2[0-3]):[0-5]\d$/;

// Keep the original schedule, including weekday/holiday rules and future fields.
export function plansFromState(state) {
 const plan = object(state?.plan) ? copy(state.plan) : {};
 plan.schedule ??= {};
 plan.holidays ??= {};
 plan.sources = {...(object(plan.sources) ? plan.sources : {}),direct:plan.sources?.direct !== false,mail:plan.sources?.mail !== false};
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
 let sourceEdits = null;
 let reportSignature = null, reportCards = new Map(), reportItems = [];
 const valid = version => active && generation === version;
 const el = (tag,text,className) => {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
 };
 function link(label,href) {
  const node = el('a',label,'settings-link');
  node.href = href; node.target = '_blank'; node.rel = 'noopener noreferrer';
  return node;
 }
 function guide(title,steps) {
  const details = el('details',null,'settings-guide');
  const list = el('ol',null,'settings-guide-steps');
  for (const step of steps) list.append(el('li',step,'settings-guide-step'));
  details.append(el('summary',title,'settings-guide-summary'),list);
  return details;
 }
 function connection(title,description) {
  const card = el('section',null,'settings-card settings-connection');
  card.append(el('h3',title,'settings-connection-title'),el('p',description,'settings-connection-description'));
  return card;
 }
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
   if (!draft) { ui.start.value = ui.end.value = ui.interval.value = ''; ui.start.disabled = ui.end.disabled = ui.interval.disabled = true; return; }
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
  for (const key of ['direct','mail']) ui.sourceInputs[key].checked = (sourceEdits || baseline.sources)[key];
  ui.save.disabled = busy;
 }
 function createUI() {
  root.classList.add('management'); root.hidden = false;
  const section = el('section',null,'management-page management-plans');
  section.id = 'view-plans'; section.setAttribute('data-view-panel','plans');
  section.append(el('h2','采集计划'));
  const planTools = el('div',null,'management-actions');
  const exportPlan = el('button','导出计划 JSON','button button-secondary'); exportPlan.type = 'button';
  exportPlan.onclick = () => {
   // Keep the existing dialog/download flow, but export the complete published plan.
   onOpenPlanJSON?.();
   const editor = document.getElementById('plantext');
   if (editor) editor.value = JSON.stringify(plansFromState(getState()),null,2);
  };
  planTools.append(exportPlan); section.append(planTools);
  const form = el('form',null,'management-form');
  const sources = el('fieldset',null,'collection-sources');
  sources.append(el('legend','获取位置的两种方式'),el('p','两种方式可独立开启或关闭。修改后请加密提交，等待云端发布确认。','muted'));
  const sourceInputs = {}, sourceStatuses = {};
  for (const [key,title,description] of [
   ['direct','定时直接查询小米','由云端按设备计划直接查询小米定位接口，自动更新位置；不是 AI 模拟点击。需要有效的小米会话和可用设备。'],
   ['mail','邮箱导入','通过只读 IMAP 导入已有定位邮件中的定位附件，不向设备发起定位。需要配置邮箱授权及过滤条件；开启开关不代表已配置或已验证。']
  ]) {
   const card = el('div',null,'collection-source');
   const control = field(title,'checkbox');
   control.input.setAttribute('aria-label',title);
   const descriptionId = 'collection-source-' + key;
   const descriptionNode = el('p',description,'muted'); descriptionNode.id = descriptionId;
   control.input.setAttribute('aria-describedby',descriptionId);
   const sourceStatus = el('p',null,'collection-source-status'); sourceStatus.setAttribute('role','status');
   card.append(control.wrap,descriptionNode,sourceStatus); sources.append(card);
   sourceInputs[key] = control.input; sourceStatuses[key] = sourceStatus;
  }
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
  form.append(sources,grid,planSummary,note,formError,actions,status);
  section.append(form);
  const reports = el('section',null,'management-page management-reports');
  reports.id = 'view-reports'; reports.setAttribute('data-view-panel','reports');
  const settings = el('section',null,'management-page management-settings');
  settings.id = 'view-settings'; settings.setAttribute('data-view-panel','settings');
  settings.append(el('h2','系统设置'),el('p','连接中心：三类授权分别配置，不能相互替代。访问密码只用于解锁和加密，不是下列任何服务的授权凭据。','settings-intro'));
  const center = el('div',null,'settings-grid');
  const settingsCard = connection('GitHub 计划发布授权','用于向本仓库提交加密计划。Token 到期影响网页提交计划，不会替代小米会话，也不会直接停止 Actions 自身的定时任务。访问密码不会存储。');
  const githubLinks = el('div',null,'settings-links');
  githubLinks.append(link('创建 GitHub 精细权限 Token','https://github.com/settings/personal-access-tokens/new'),link('打开 GitHub Actions',ACTIONS_URL));
  settingsCard.append(githubLinks,guide('如何授权 GitHub 并确认计划生效',[
   '在 GitHub 创建 Fine-grained personal access token，Resource owner 选择目标仓库所属账号，设置到期日。Repository access 选择 Only select repositories，仅选 yanzaitian/locator-workbench。',
   'Repository permissions 中仅启用 Actions: Read and write；Metadata: Read-only 为必要读取权限。日常提交计划无需 Contents、Workflows 或 Secrets 管理权限。若组织要求审批，须等待批准。',
   '将新 Token 仅填入下方密码输入框。可选“记住授权”，只在本机保存用访问密码加密的密文；重新解锁后点击“恢复本机加密授权”。清除本机授权只移除本机副本，撤销 Token 请到 GitHub。',
   '切换到“采集计划”，编辑后点击“加密提交计划”。HTTP 204 仅表示接收，打开 Actions 等待本次运行完成，再点击“刷新发布状态”，以“已生效”回执确认。',
   'GitHub HTTP 401 请重新创建或输入有效 Token；403 检查仓库访问和 Actions 写入权限。这与小米设备接口的 401 属于不同授权。'
  ]),auth.wrap,remember.wrap,authActions);
  const settingsStatus = el('p',null,'management-status');
  settingsStatus.setAttribute('role','status'); settingsStatus.setAttribute('aria-live','polite');
  settingsCard.append(settingsStatus);
  const xiaomi = connection('小米定位采集授权','云端通过 XM_TOKEN（serviceToken）和 XM_UID（userId）复用小米会话。此静态页面不能直接完成小米扫码登录或写入 GitHub Secrets。');
  const xiaomiStatus = el('p',null,'settings-connection-status');
  const xiaomiLinks = el('div',null,'settings-links');
  xiaomiLinks.append(link('打开本机授权工作台','http://127.0.0.1:8765/'),link('打开小米云服务','https://i.mi.com/find'),link('配置 GitHub Secrets',SECRETS_URL),link('检查采集 Actions',ACTIONS_URL));
  xiaomi.append(xiaomiStatus,el('p','账号信息：未知。已发布状态没有可核验的小米昵称、头像、手机号或完整账号资料；设备名称和历史位置不能证明当前登录账号或会话有效。','settings-account-note'),xiaomiLinks,guide('如何重新授权小米并恢复云端采集',[
   '在运行本项目的电脑上启动 run.bat 或 python server.py，再打开 http://127.0.0.1:8765/，使用本地访问密码登录。首次本地访问需设置密码；它与云端解锁密码分别管理。其他设备上的 127.0.0.1 指向该设备自身，不能连接这台电脑。',
   '进入本地“设备”→“小米账号”→“扫码登录”，用手机“设置 → 小米账号 → 扫一扫”并确认。已有密码登录和备用 Cookie 登录入口；如需 Cookie，必须来自本人刚验证有效的小米会话，仅在本地输入。',
   '本地扫码只更新本机数据库，不会自动更新云端 Secrets，当前没有从本页一键同步的按钮。扫码成功后，在项目根目录双击“复制小米TOKEN.cmd”；工具先验证会话有效，仅将单个 TOKEN 值复制到剪贴板，不在终端输出凭据。验证失败请先重新扫码。',
   '点击“配置 GitHub Secrets”，在 Settings → Secrets and variables → Actions 中用 New repository secret 新建，或用已有条目的 Edit → Update secret 更新 XM_TOKEN，将刚复制的值粘贴并保存。再双击根目录“复制小米UID.cmd”，验证成功后将剪贴板值粘贴到 XM_UID 并保存。两个值须来自同一有效会话；dist/凭据.txt 可能过期，不会自动更新，不要用它覆盖新凭据。',
   '打开本仓库 Actions → 定位工作台 → Run workflow，选择 main，operation 选择 collect，payload 留空。运行后刷新页面，同时核对设备列表、查询结果和实际位置采样时间；页面可打开或 Pages 发布成功不能证明定位成功。',
   '小米 HTTP 401 表示设备接口拒绝当前会话，可能已失效或不被接受。历史位置会保留，不能当作新定位。项目未接入云端自动续期，重新扫码需要人工参与，短暂恢复不能证明长期稳定。'
  ]));
  const mailCard = connection('邮箱导入授权与月报','邮箱授权码用于只读 IMAP 导入定位邮件，与 GitHub Token、小米会话分别管理。在邮箱服务商开启 IMAP 并生成客户端授权码，再配置 XM_MAIL_CONFIG。');
  const mailLinks = el('div',null,'settings-links');
  mailLinks.append(link('设置 XM_MAIL_CONFIG Secret',SECRETS_URL),link('检查邮件导入 Actions',ACTIONS_URL));
  const mail = el('section',null,'device management-mail settings-mail-status');
  mailCard.append(mailLinks,guide('如何启用邮件导入及查看月报',[
   '登录自己的邮箱服务商网页，进入邮箱设置，启用 IMAP 并按服务商要求生成客户端授权码。账号、服务器和授权码仅在 GitHub Secrets 或本地设置中填写，不要发到聊天或写进仓库。',
   '在 GitHub Secrets 新建或更新 XM_MAIL_CONFIG，值为 JSON 对象：enabled 设为 true，填写 host、port（通常 993）、user、password（客户端授权码）、folder（通常 INBOX）及自定义 device_id 标签。',
   '填写 sender_contains 或 subject_contains 中至少一个明确的邮件过滤条件；两个都填写时必须同时匹配。邮件必须包含导入器支持的真实定位内容或附件，仅配置邮箱不会产生位置数据。',
   '在 Actions 手动运行 collect，刷新此页检查“邮件导入状态”、匹配邮件数和导入位置数。未启用、尚未配置、导入完成、部分跳过和导入失败含义不同；导入完成但位置数为 0 不等于已有轨迹。',
   '进入“月报”按设备和月份查看；邮件位置归入独立邮件设备。月报根据真实采样生成，当月为预览，保留最近 12 个已完成月份。未设置 DEEPSEEK_API_KEY 也可用本地模板；该可选 Secret 用于已完成月份的统计说明。'
  ]),el('p','能力范围：云端支持只读邮件导入和网页月报展示，当前 Actions 没有接入 SMTP 月报自动发送。配置 IMAP 不会自动把月报发到邮箱；本地 SMTP 计划推送也不等于云端月报邮件发送。','settings-capability-note'),mail);
  const runtime = connection('运行与安全','GitHub Pages 展示已发布的加密状态，Actions 执行云端采集。以下是当前运行方式与维护步骤。');
  const runtimeLinks = el('div',null,'settings-links');
  runtimeLinks.append(link('查看运行记录',ACTIONS_URL),link('管理运行 Secrets',SECRETS_URL),link('管理或撤销 GitHub Token','https://github.com/settings/personal-access-tokens'));
  runtime.append(runtimeLinks,guide('查看运行机制、锁定与密码轮换步骤',[
   'Pages 由 GitHub 托管，电脑关机后仍可访问已发布页面；云端采集由 Actions 执行，本地扫码和剪贴板工具则需要电脑开机。能访问网页不代表刚获得新位置。',
   'Actions cron 配置每 10 分钟发起一次运行，GitHub 可能排队或延迟，不能保证准确间隔。工作流运行频率与设备实际计划不同：仅对到达采集时段且满足设备间隔的设备请求定位，并遵守星期、节假日和独立计划规则。计划时段及页面时间按北京时间（Asia/Shanghai）理解。',
   '页面“刷新”和“刷新发布状态”只读取已发布状态，不会即时定位，也不会触发 Actions。需要云端重新运行时打开 Actions → Run workflow → operation 选择 collect，再检查实际采样时间。',
   '点击“锁定”会清空当前页面中的访问密码、Token、设备数据、计划草稿和报告缓存，并使在途操作的结果失效。之前选择记住授权的本机加密副本仍保留，可在解锁后恢复；需要移除时点击“清除本机授权”。',
   '浏览器“清除本机授权”只删除当前浏览器的加密副本并清空输入，不等于 GitHub 撤销。需要停用 Token 时点击“管理或撤销 GitHub Token”，在 GitHub 删除或撤销对应授权。',
   '轮换云端访问密码：先在 Actions Secrets 临时添加 LOCATOR_PREVIOUS_PASSWORD（旧密码），再将 LOCATOR_PASSWORD 更新为至少 12 个字符的新密码，运行一次 collect 迁移旧状态；确认成功后用新密码解锁，核对历史和数据。旧密码仅用于本次迁移，不应长期保留。',
   '若已保存加密计划，单次 collect 不会重加密 cloud/plans.enc.json。暂时保留旧密码 Secret，用新密码解锁后进入“采集计划”，输入 GitHub Token，将当前计划“加密提交计划”一次，等待“已生效”，使计划也用新密码保存。随后删除 LOCATOR_PREVIOUS_PASSWORD，再运行 collect 确认不依赖旧密码。密码轮换后，本机旧授权密文需要清除并重新输入、记住。'
  ]));
  center.append(xiaomi,settingsCard,mailCard,runtime); settings.append(center);
  root.replaceChildren(section,reports,settings);
  ui = {section,form,sourceInputs,sourceStatuses,device,enabled:enabled.input,inherit:inherit.input,start:start.input,end:end.input,interval:interval.input,token:auth.input,remember:remember.input,save,refresh,reset,restore,forget,status,settingsStatus,xiaomiStatus,planSummary,formError,reports,mail};
  for (const key of ['direct','mail']) sourceInputs[key].onchange = () => {
   sourceEdits = {...(sourceEdits || baseline.sources),[key]:sourceInputs[key].checked}; editVersion++;
   message('有未保存的来源选择；请加密提交计划。');
  };
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
   edits.clear(); sourceEdits = null; editVersion++; acceptState(state); message('已载入最新计划。');
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
  try { plans = buildPlans(baseline,edits); if (sourceEdits) plans.sources = copy(sourceEdits); }
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
  finally { password = ''; if (valid(version)) { busy = false; ui.save.disabled = false; ui.reset.disabled = false; } }
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
 function renderSources(state) {
  for (const key of ['direct','mail']) {
   const source = object(state.sources?.[key]) ? state.sources[key] : {};
   const published = baseline.sources[key] ? '已开启' : '已关闭';
   const execution = source.enabled === true ? '本轮已启用' : source.enabled === false ? '本轮未启用' : '本轮启用状态未知';
   const status = {disabled:'未启用',not_configured:'尚未配置',ok:key === 'mail' ? '导入完成（以导入位置数为准）' : '本轮检查完成（新位置以设备采样为准）',partial:'部分完成',error:key === 'mail' ? '导入失败' : '查询失败',pending:'等待执行',unknown:'尚未验证',not_verified:'尚未验证'}[source.status] || '尚未验证';
   const configured = key === 'mail' ? (source.configured === true ? ' · 已配置（不代表导入成功）' : source.configured === false ? ' · 尚未配置' : ' · 配置状态未知') : '';
   ui.sourceStatuses[key].textContent = '已发布计划：' + published + ' · ' + execution + configured + ' · 最近结果：' + status;
  }
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
   if (untouched) { edits.clear(); sourceEdits = null; acceptState(state); }
   else { baseline = plansFromState(state); revision = state.plan_request_id || ''; renderPlanSummary(); }
   message(untouched ? '已生效：云端已发布本次计划。' : '已生效：云端已发布上次提交；当前仍有未保存修改。');
  } else if (!edits.size && !sourceEdits && !busy && !pending) acceptState(state);
  if ((edits.size || sourceEdits) && revision !== (state.plan_request_id || '')) message('云端计划版本已变化，草稿已保留。请先放弃草稿并载入最新计划，再重新编辑。');
  ui.xiaomiStatus.textContent = typeof state.error === 'string' && /\b401\b/.test(state.error)
   ? '已发布状态报告：小米接口 HTTP 401，当前展示可能为历史数据。请重新授权并通过新一轮采集核验。'
   : state.error ? '已发布状态存在采集异常；当前小米会话有效性未在此页验证，请检查 Actions 和设备查询结果。'
   : '当前小米会话有效性未在此页验证；请以最新一轮采集的设备查询结果和实际采样时间为准。';
  renderSources(state);
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
  ui = null; root = null; baseline = null; revision = ''; edits.clear(); sourceEdits = null; pending = null;
  busy = restoring = false; editVersion = 0;
  reportSignature = null; reportCards.clear(); reportItems = [];
 }
 return {render,lock};
}
function safeDate(value) {
 if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T/.test(value) || !Number.isFinite(Date.parse(value))) return '暂无';
 return new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
}
