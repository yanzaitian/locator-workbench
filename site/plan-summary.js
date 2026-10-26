const DAY_NAMES={"1":"周一","2":"周二","3":"周三","4":"周四","5":"周五","6":"周六","7":"周日"};
const EMPTY_CLOUD_SCHEDULE={enabled:true,skip_holidays:false,default:{enabled:true,start:'00:00',end:'00:00',interval_minutes:10},days:{},holiday_workday:{enabled:true,start:'00:00',end:'00:00',interval_minutes:10}};
const isObject=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
function parse(value){
 if(typeof value==='string'){if(!value.trim())return null;try{const parsed=JSON.parse(value);return isObject(parsed)?parsed:null}catch{return null}}
 return isObject(value)?value:null;
}
function slot(plan){
 const p=isObject(plan)?plan:{};
 return {enabled:p.enabled!==false,start:p.start||'08:00',end:p.end||'22:00',interval_minutes:Math.max(10,Number(p.interval_minutes||60))};
}
function normalizeSchedule(value,cloudFallback=false){
 const parsed=parse(value);
 if(cloudFallback&&(!parsed||Object.keys(parsed).length===0))return structuredClone(EMPTY_CLOUD_SCHEDULE);
 const source=parsed||{},days={};
 for(const [day,plan] of Object.entries(isObject(source.days)?source.days:{}))if(isObject(plan)&&Object.keys(plan).length)days[String(day)]=slot(plan);
 return {enabled:source.enabled!==false,skip_holidays:source.skip_holidays!==false,default:slot(source.default),days,holiday_workday:slot(source.holiday_workday)};
}
function planFromSetting(settings){
 const raw=settings?.plan_json;
 if(raw==null||typeof raw==='string'&&(!raw.trim()||raw.trim()==='null'))return {plan:null,invalid:false};
 const plan=parse(raw);
 if(!plan)return {plan:null,invalid:true};
 return {plan:Object.keys(plan).length?plan:null,invalid:false};
}
function slotText(plan,label=''){
 if(!plan||plan.enabled===false)return `${label}停用`;
 return `${label}${plan.start}-${plan.end}，每 ${plan.interval_minutes} 分钟`;
}
function weekdayText(schedule){
 return Array.from({length:7},(_,index)=>{
  const key=String(index+1),effective=schedule.days[key]||schedule.default;
  return `${DAY_NAMES[key]} ${slotText(effective)}`;
 }).join('；');
}
function holidayText(schedule,holidays={}){
 if(!schedule.skip_holidays)return '节假日例外关闭';
 const statutory=Array.isArray(holidays?.statutory)?holidays.statutory.length:0;
 const workdays=Array.isArray(holidays?.workdays)?holidays.workdays.length:0;
 return `法定节假日跳过（${statutory}天）；调休工作日${slotText(schedule.holiday_workday)}（${workdays}天）`;
}
export function globalScheduleSummary(globalPlan={},holidays={}){
 const schedule=normalizeSchedule(globalPlan,true);
 if(!schedule.enabled)return '全局周历：计划总开关已停用';
 return `全局周历：${weekdayText(schedule)}；${holidayText(schedule,holidays)}`;
}
export function devicePlanSummary(settings={},globalPlan={},holidays={}){
 if(settings.enabled===false)return '设备已停用';
 const own=planFromSetting(settings);
 if(own.invalid)return '独立计划格式无效';
 if(own.plan){
  const schedule=normalizeSchedule(own.plan);
  if(!schedule.enabled)return '设备独立计划总开关已停用';
  return `设备独立周历：${weekdayText(schedule)}；${holidayText(schedule,holidays)}`;
 }
 const schedule=normalizeSchedule(globalPlan,true);
 if(!schedule.enabled)return '全局计划已停用';
 const override=settings.interval_minutes;
 return override?`继承全局时段；设备间隔覆盖为每 ${Math.max(10,Number(override))} 分钟`:'继承全局时段与间隔';
}
