"""定位计划：每周每日时段、间隔、节假日跳过。"""
import datetime as dt

import common

DEFAULT_SCHEDULE = {
    "enabled": True,
    "skip_holidays": True,
    # 工作日（周一~周五）：06:00-24:00，每小时一次
    "default": {"enabled": True, "start": "06:00", "end": "23:59", "interval_minutes": 60},
    # 周末（周六日）：00:00-24:00，每 20 分钟一次
    "days": {
        "6": {"enabled": True, "start": "00:00", "end": "23:59", "interval_minutes": 20},
        "7": {"enabled": True, "start": "00:00", "end": "23:59", "interval_minutes": 20},
    },
    "holiday_workday": {"enabled": True, "start": "09:00", "end": "21:00", "interval_minutes": 60},
}

# 国务院公布的法定节假日（含调休工作日）
DEFAULT_HOLIDAYS = {
    "statutory": [
        "2025-01-01",
        "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",
        "2025-02-01", "2025-02-02", "2025-02-03", "2025-02-04",
        "2025-04-04", "2025-04-05", "2025-04-06",
        "2025-05-01", "2025-05-02", "2025-05-03", "2025-05-04", "2025-05-05",
        "2025-05-31", "2025-06-01", "2025-06-02",
        "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-04",
        "2025-10-05", "2025-10-06", "2025-10-07", "2025-10-08",
        "2026-01-01", "2026-01-02", "2026-01-03",
        "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
        "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",
        "2026-04-04", "2026-04-05", "2026-04-06",
        "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
        "2026-06-19", "2026-06-20", "2026-06-21",
        "2026-09-25", "2026-09-26", "2026-09-27",
        "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
        "2026-10-05", "2026-10-06", "2026-10-07",
    ],
    "workdays": [
        "2025-01-26", "2025-02-08", "2025-04-27", "2025-09-28", "2025-10-11",
        "2026-01-04", "2026-02-14", "2026-02-28", "2026-05-09",
        "2026-09-20", "2026-10-10",
    ],
}


def parse_hm(text):
    try:
        h, m = str(text).split(":")[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def fmt_hm(minutes):
    minutes = int(minutes) % (24 * 60)
    return "%02d:%02d" % (minutes // 60, minutes % 60)


def normalize(sched):
    out = {"enabled": bool(sched.get("enabled", True)),
           "skip_holidays": bool(sched.get("skip_holidays", True))}
    for key in ("default", "holiday_workday"):
        plan = sched.get(key) or {}
        out[key] = {
            "enabled": bool(plan.get("enabled", True)),
            "start": str(plan.get("start") or "08:00"),
            "end": str(plan.get("end") or "22:00"),
            "interval_minutes": max(1, int(plan.get("interval_minutes") or 60)),
        }
    out["days"] = {}
    for wd, plan in (sched.get("days") or {}).items():
        if plan:
            out["days"][str(wd)] = {
                "enabled": bool(plan.get("enabled", True)),
                "start": str(plan.get("start") or "08:00"),
                "end": str(plan.get("end") or "22:00"),
                "interval_minutes": max(1, int(plan.get("interval_minutes") or 60)),
            }
    return out


def plan_for(sched, day_local, holidays=None):
    """返回该日期生效的计划，None 表示这一天不定位。"""
    sched = normalize(sched)
    if not sched["enabled"]:
        return None
    holidays = holidays or {}
    iso = day_local.strftime("%Y-%m-%d")
    if sched["skip_holidays"]:
        if iso in set(holidays.get("statutory") or []):
            return None
        if iso in set(holidays.get("workdays") or []):
            p = sched["holiday_workday"]
            return p if p["enabled"] else None
    wd = str(day_local.isoweekday())
    plan = sched["days"].get(wd) or sched["default"]
    return plan if plan and plan["enabled"] else None


def in_window(plan, local_dt):
    if not plan:
        return False
    m = local_dt.hour * 60 + local_dt.minute
    start = parse_hm(plan["start"])
    end = parse_hm(plan["end"])
    if start == end:
        return True
    if start < end:
        return start <= m < end
    return m >= start or m < end


def next_run(sched, holidays, start_local=None, horizon_days=8):
    """从当前时刻起，找下一次落在窗口内且对齐间隔的时间点。"""
    sched = normalize(sched)
    t = (start_local or common.now_local()).replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    limit = t + dt.timedelta(days=horizon_days)
    while t < limit:
        plan = plan_for(sched, t, holidays)
        if plan and in_window(plan, t):
            start_m = parse_hm(plan["start"])
            interval = max(1, int(plan["interval_minutes"]))
            base = t.replace(hour=0, minute=0, second=0, microsecond=0)
            start_dt = base + dt.timedelta(minutes=start_m)
            if start_dt > t:
                start_dt -= dt.timedelta(days=1)
            delta = int((t - start_dt).total_seconds() // 60)
            if delta % interval == 0:
                return t
        t += dt.timedelta(minutes=1)
    return None


def describe_today(sched, holidays):
    now = common.now_local()
    plan = plan_for(sched, now, holidays)
    if not plan:
        reason = "今天不定位（节假日或已关闭）"
        return {"plan": None, "in_window": False, "reason": reason}
    return {
        "plan": plan,
        "in_window": in_window(plan, now),
        "reason": "计划 %s-%s，每 %d 分钟" % (plan["start"], plan["end"], plan["interval_minutes"]),
    }


def validate_schedule(value, minimum=5):
    import re
    if not isinstance(value, dict):
        raise ValueError("计划必须是对象")
    for key in ("enabled", "skip_holidays"):
        if key in value and type(value[key]) is not bool:
            raise ValueError(key + " 必须为布尔值")
    days = value.get("days", {})
    if not isinstance(days, dict) or any(str(k) not in tuple("1234567") for k in days):
        raise ValueError("星期必须为1至7")
    for p in [value.get("default", {}), value.get("holiday_workday", {}), *days.values()]:
        if not isinstance(p, dict):
            raise ValueError("时段必须是对象")
        if "enabled" in p and type(p["enabled"]) is not bool:
            raise ValueError("enabled 必须为布尔值")
        for k in ("start", "end"):
            if k in p and (not isinstance(p[k], str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", p[k])):
                raise ValueError("时间必须为 HH:MM，范围00:00至23:59")
        if "interval_minutes" in p and (type(p["interval_minutes"]) is not int or not minimum <= p["interval_minutes"] <= 10080):
            raise ValueError("定位间隔超出范围")
    return normalize(value)


def _parse_device_plan(raw):
    import json
    if isinstance(raw, str):
        raw = json.loads(raw) if raw.strip() else None
    if raw is not None and not isinstance(raw, dict):
        raise ValueError("设备独立计划必须是对象")
    return raw


def validate_cloud_plans(value):
    if not isinstance(value, dict) or not isinstance(value.get("devices", {}), dict):
        raise ValueError("计划必须包含devices对象")
    if "schedule" in value:
        validate_schedule(value["schedule"], 10)
    for did, device in value.get("devices", {}).items():
        if not isinstance(did, str) or not isinstance(device, dict):
            raise ValueError("设备计划格式错误")
        if "enabled" in device and type(device["enabled"]) is not bool:
            raise ValueError("enabled 必须为布尔值")
        if "interval_minutes" in device and (type(device["interval_minutes"]) is not int or not 10 <= device["interval_minutes"] <= 10080):
            raise ValueError("云端间隔须为10至10080分钟")
        plan = _parse_device_plan(device.get("plan_json"))
        if plan:
            validate_schedule(plan, 10)
    holidays = value.get("holidays", {})
    if not isinstance(holidays, dict):
        raise ValueError("holidays须为对象")
    for key in ("statutory", "workdays"):
        dates = holidays.get(key, [])
        if not isinstance(dates, list):
            raise ValueError("节假日须为日期列表")
        for date in dates:
            dt.date.fromisoformat(date)
    return value


def _device_schedule(device, global_plan):
    """空对象、空 JSON 对象和空字符串均表示继承全局计划。"""
    raw = _parse_device_plan(device.get("plan_json"))
    has_independent = bool(raw)
    return (raw if has_independent else global_plan or {}), has_independent


def device_due(device, global_plan, holidays, last_query=None, now=None, minimum=5):
    """跨午夜窗口归属开始日；独立计划覆盖全局，设备开关始终生效。"""
    if not device.get("enabled", True):
        return False
    sched, has_independent = _device_schedule(device, global_plan)
    now = now or common.now_utc()
    local = common.to_local(now)
    if not sched.get("enabled", True):
        return False
    plan = plan_for(sched, local, holidays)
    active = None
    if plan:
        start, end = parse_hm(plan["start"]), parse_hm(plan["end"])
        minute = local.hour * 60 + local.minute
        if (start == end or start < end and start <= minute < end or start > end and minute >= start):
            active = plan
    previous = plan_for(sched, local - dt.timedelta(days=1), holidays)
    if not active and previous and parse_hm(previous["start"]) > parse_hm(previous["end"]):
        if local.hour * 60 + local.minute < parse_hm(previous["end"]):
            active = previous
    if not active:
        return False
    interval = max(minimum, int(active["interval_minutes"]) if has_independent else int(device.get("interval_minutes") or active["interval_minutes"]))
    last = common.parse_time(last_query) if last_query else None
    return not last or (now - last).total_seconds() >= interval * 60
