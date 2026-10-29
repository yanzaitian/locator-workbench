"""月度统计 + DeepSeek 汇报生成（永久保存多版本）。"""
import json
import urllib.request

import common
import schedule
import store

SYSTEM_PROMPT = (
    "你是用户的个人位置数据助理。用户会给你一台手机一个自然月的定位统计 JSON。"
    "请写一份《定位工作台 · 月度汇报》，使用 Markdown，包含："
    "一、本月概况；二、活动规律；三、常去地点；四、离线与异常；五、下月建议。"
    "要求：只依据数据，不编造地址、人名或事件；坐标无法还原为地址时如实说明；"
    "数字必须与数据一致；语气客观简洁，600~900 字。"
)


def aggregate(cfg, month):
    start, end = common.month_range(month)
    pts = store.get_points(cfg, start, end, limit=300000)
    out = {"month": month, "point_count": len(pts), "active_days": 0,
           "first_seen": None, "last_seen": None, "days": [], "top_places": [],
           "longest_gap_hours": 0.0, "night_points": 0, "total_distance_km": 0.0,
           "battery_min": None, "devices": []}
    if not pts:
        return out
    days = {}
    devices = set()
    for p in pts:
        days.setdefault(common.to_local(p["ts_utc"]).strftime("%Y-%m-%d"), []).append(p)
        devices.add(p.get("device_id") or "default")
    total_distance = 0.0
    night = 0
    battery_min = None
    for d in sorted(days):
        items = days[d]
        dist = 0.0
        # Track each device separately within the day; never join device tracks.
        previous_by_device = {}
        for b in items:
            device_id = b.get("device_id") or "default"
            a = previous_by_device.get(device_id)
            previous_by_device[device_id] = b
            if a is not None:
                step = common.haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
                if step < 2000:
                    dist += step
        total_distance += dist
        for p in items:
            if common.to_local(p["ts_utc"]).hour < 6:
                night += 1
            if p.get("battery") is not None:
                battery_min = p["battery"] if battery_min is None else min(battery_min, p["battery"])
        out["days"].append({
            "date": d, "points": len(items),
            "first": common.fmt_local(items[0]["ts_utc"], "%H:%M"),
            "last": common.fmt_local(items[-1]["ts_utc"], "%H:%M"),
            "distance_km": round(dist / 1000.0, 2)})
    gap = 0.0
    for i in range(1, len(pts)):
        t1 = common.parse_time(pts[i - 1]["ts_utc"])
        t2 = common.parse_time(pts[i]["ts_utc"])
        gap = max(gap, (t2 - t1).total_seconds() / 3600.0)
    grid = {}
    for p in pts:
        key = (round(p["lat"], 2), round(p["lon"], 2))
        e = grid.setdefault(key, {"days": set(), "count": 0})
        e["days"].add(common.to_local(p["ts_utc"]).strftime("%Y-%m-%d"))
        e["count"] += 1
    top = sorted(grid.items(), key=lambda kv: (-len(kv[1]["days"]), -kv[1]["count"]))[:8]
    out["top_places"] = [{"lat": k[0], "lon": k[1], "days": len(v["days"]), "points": v["count"]}
                         for k, v in top]
    out["active_days"] = len(days)
    out["first_seen"] = common.fmt_local(pts[0]["ts_utc"])
    out["last_seen"] = common.fmt_local(pts[-1]["ts_utc"])
    out["longest_gap_hours"] = round(gap, 1)
    out["night_points"] = night
    out["total_distance_km"] = round(total_distance / 1000.0, 1)
    out["battery_min"] = battery_min
    out["devices"] = sorted(devices)
    return out


def _call_deepseek(cfg, stats):
    conf = cfg["deepseek"]
    url = conf["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": conf.get("model") or "deepseek-chat",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "统计数据（JSON）：\n" + json.dumps(stats, ensure_ascii=False, indent=2)
                                        + "\n请生成月度汇报。"},
        ],
        "temperature": 0.3, "max_tokens": 2400, "stream": False,
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + conf["api_key"]}, method="POST")
    with urllib.request.urlopen(req, timeout=int(conf.get("timeout_seconds") or 240)) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


def _local(stats):
    lines = ["# %s 定位工作台 · 月度汇报（本地模板）" % stats["month"], ""]
    lines += ["## 一、本月概况", "- 上报点数：%d" % stats["point_count"],
              "- 有数据天数：%d 天" % stats["active_days"],
              "- 首次上报：%s" % (stats["first_seen"] or "—"),
              "- 最近上报：%s" % (stats["last_seen"] or "—"), ""]
    lines += ["## 二、活动规律", "- 累计移动距离：约 %.1f km" % stats["total_distance_km"],
              "- 凌晨 0–6 点上报点数：%d" % stats["night_points"],
              "- 最长上报中断：%.1f 小时" % stats["longest_gap_hours"], ""]
    lines += ["## 三、常去地点（约 1km 网格）"]
    for i, p in enumerate(stats["top_places"], 1):
        lines.append("%d. %.4f, %.4f —— %d 天，%d 点" % (i, p["lat"], p["lon"], p["days"], p["points"]))
    if not stats["top_places"]:
        lines.append("本月无足够数据。")
    lines += ["", "## 四、离线与异常",
              "- 最长中断 %.1f 小时，可结合小米「查找设备」核对当天是否断网或关机。" % stats["longest_gap_hours"]]
    if stats["battery_min"] is not None:
        lines.append("- 记录到的最低电量：%.0f%%" % stats["battery_min"])
    lines += ["", "## 五、下月建议",
              "- 保持手机联网与蓝牙开启，减少上报中断。",
              "- 若中断集中在固定时段，检查省电策略与计划设置。"]
    return "\n".join(lines)


def generate(cfg, month, auto=False):
    stats = aggregate(cfg, month)
    error = None
    if stats["point_count"] == 0:
        content = "# %s 月度汇报\n\n本月没有任何上报数据。请检查手机端采集与上报通道。" % month
        source = "empty"
    elif cfg["deepseek"].get("api_key"):
        try:
            content = _call_deepseek(cfg, stats)
            source = "deepseek"
        except Exception as exc:  # noqa: BLE001
            error = "%s: %s" % (type(exc).__name__, exc)
            content = _local(stats)
            source = "local"
    else:
        content = _local(stats)
        source = "local"
    rid = store.add_report(cfg, month, content, source,
                           model=cfg["deepseek"].get("model", ""), auto=auto)
    return {"id": rid, "month": month, "source": source, "error": error, "content": content, "auto": auto}


def _completed_months(cfg, months):
    current = common.now_local().strftime("%Y-%m")
    return [m for m in months if m < current]


def auto_generate_missing(cfg, log=print):
    """开机后补齐所有缺少汇报的已结束月份（最近的在前面）。"""
    if not cfg["auto_report"].get("enabled", True):
        return []
    months = _completed_months(cfg, store.months_with_data(cfg))
    existing = set(r["month"] for r in store.reports_summary(cfg))
    missing = [m for m in months if m not in existing]
    limit = int(cfg["auto_report"].get("catch_up_months") or 12)
    missing = sorted(missing, reverse=True)[:limit]
    out = []
    for m in missing:
        try:
            log("自动生成 %s 月度汇报…" % m)
            out.append(generate(cfg, m, auto=True))
        except Exception as exc:  # noqa: BLE001
            log("生成 %s 失败：%s" % (m, exc))
    return out
