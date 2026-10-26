"""In-memory cloud history and monthly reports; persist ONLY via crypto_state.

Integration (owned by fetch's caller)::
    state = enrich_state(collect(...), now=now)
    envelope = encrypt(state, password)

Schema v1:
    history[device_id] = {started_at: UTC ISO, points: [original last_point, ...]}
    reports = [{month, device_id, generated_at, summary, content,
                source: "local", is_final, status, ai_status, ai_notes}, ...]

History retains every valid distinct sample in the previous and current local
calendar months, without downsampling. Reports retain the last 12 closed months
plus a current-month template preview.
The preview is explicitly unfinished and NEVER invokes the AI callback.
Closed months without samples also remain local, avoiding pointless AI costs.
Deduplication uses (UTC timestamp, latitude, longitude), first stored value wins.
A cached last_point is never restamped. Missing devices retain their history.

With explicit now this is deterministic and does not mutate input, access files,
environment, network, local store, config, or email. An OPTIONAL injected
summarizer(summary, api_key) is an explicit effect boundary; it receives
only a detached numeric/time coverage summary (no device ID/name or coordinates).
No callback is invoked without a key. Its only accepted output is
{"observations": ["sample_limits", "collection_gap"]}; these are
validated against facts and rendered locally. Arbitrary AI prose is never saved.
Use summarizer=summarize_deepseek for the provided opt-in HTTP adapter.
Only that adapter performs network I/O; enrich_state never enables it implicitly.
The credential is passed transiently and never included in state or diagnostics.
"""
import copy
import datetime as dt
import json
import math

import requests

import common

REPORT_MONTHS = 12
_POINT_FIELDS = ("ts", "lat", "lon", "acc", "battery", "source", "gcj_lat", "gcj_lon")


def _time(value):
    # Point timestamps must carry their timezone; ambiguous times are not evidence.
    try:
        if isinstance(value, str):
            value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, dt.datetime) or value.tzinfo is None:
            return None
        return value.astimezone(dt.timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _iso(value):
    """UTC ISO, preserving subsecond evidence when supplied."""
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _month_offset(start, offset):
    index = start.year * 12 + start.month - 1 + offset
    year, month = divmod(index, 12)
    return start.replace(year=year, month=month + 1)


def _number(value, low, high):
    return (type(value) in (float, int) and low <= value <= high
            and math.isfinite(value))


def _point(value, start, now):
    if not isinstance(value, dict):
        return None
    stamp = _time(value.get("ts"))
    if stamp is None or not start <= stamp <= now:
        return None
    if not _number(value.get("lat"), -90, 90) or not _number(value.get("lon"), -180, 180):
        return None
    # Keep the existing point vocabulary, never copy arbitrary API payloads.
    point = {key: copy.deepcopy(value[key]) for key in _POINT_FIELDS if key in value}
    point["ts"] = _iso(stamp)
    return point


def _summary(points, started_at, start, end):
    samples = [p for p in points if start <= _time(p["ts"]) < end]
    stamps = [_time(p["ts"]) for p in samples]
    gaps = [(b - a).total_seconds() / 3600 for a, b in zip(stamps, stamps[1:])]
    began = _time(started_at)
    coverage = ("not_started" if began >= end else
                "partial_window" if began > start else "full_window")
    return {
        "timezone": "Asia/Shanghai",
        "window_start": common.utc_iso(start),
        "window_end_exclusive": common.utc_iso(end),
        "accumulation_started_at": started_at,
        "coverage": coverage,
        "data_status": "samples" if samples else "no_samples",
        "point_count": len(samples),
        "active_days": len({common.to_local(s).date() for s in stamps}),
        "sample_start": samples[0]["ts"] if samples else None,
        "sample_end": samples[-1]["ts"] if samples else None,
        "samples_before_accumulation": sum(s < began for s in stamps),
        "longest_sample_gap_hours": round(max(gaps, default=0), 2),
    }


def _template(month, stats, is_final):
    coverage = {
        "not_started": "报告月份尚未开始积累；无法追溯该月完整记录。",
        "partial_window": "月中开始积累，仅覆盖部分月份。",
        "full_window": "积累机制已覆盖月初；不代表持续在线或完整轨迹。",
    }[stats["coverage"]]
    title = "本地模板 · 已完成月" if is_final else "本地模板 · 当月预览，月份未结束"
    lines = [f"# {month} 定位工作台 · 月度汇报（{title}）", "",
             "统计时区：Asia/Shanghai（UTC+8）",
             f"月份范围：[{stats['window_start']}, {stats['window_end_exclusive']})",
             f"开始积累：{stats['accumulation_started_at']}", coverage,
             f"有效样本：{stats['point_count']}；有数据天数：{stats['active_days']}"]
    if stats["point_count"]:
        lines += [f"样本范围：{stats['sample_start']} 至 {stats['sample_end']}",
                  f"相邻样本最大间隔：{stats['longest_sample_gap_hours']} 小时（不等于离线时间）。"]
    elif stats["coverage"] == "not_started":
        lines.append("样本范围：无；该月尚未开始积累，不能认定设备该月无活动。")
    else:
        lines.append("样本范围：无；已开始积累但该月未保存有效样本，不能认定设备无活动。")
    if stats["samples_before_accumulation"]:
        lines.append(f"其中 {stats['samples_before_accumulation']} 个样本来自开始积累前的真实缓存时间，未补造轨迹。")
    if not is_final:
        lines.append("当月尚未结束，以下仅为截至当前已保存样本的模板预览，不调用 AI。")
    lines.append("只统计已保存样本；不推断地址、路线、停留、活动原因或未采样时段。")
    return "\n".join(lines)


def _unique_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("invalid_json")
        value[key] = item
    return value


def _reject_json_constant(value):
    raise ValueError("invalid_json")


def _strict_json(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 65536:
        raise ValueError("invalid_json")
    return json.loads(text, object_pairs_hook=_unique_json_object,
                      parse_constant=_reject_json_constant)


def summarize_deepseek(summary, api_key):
    """Opt-in DeepSeek HTTP callback; pass explicitly to enrich_state.

    Sends only six validated aggregate fields. Unknown input fields (including
    device metadata, timestamps, and coordinates) are never sent. Returns only
    validated observation codes; raises fixed errors without response bodies,
    credential text, or chained network exceptions. No config/env/file access,
    redirects, retries, or fallback requests. Mock requests.post in tests.
    """
    try:
        if (not isinstance(summary, dict) or not isinstance(api_key, str)
                or not api_key.strip() or "\r" in api_key or "\n" in api_key):
            raise ValueError("invalid_input")
        fields = ("point_count", "active_days", "samples_before_accumulation",
                  "longest_sample_gap_hours", "coverage", "data_status")
        safe = {field: summary[field] for field in fields}
        for field in ("point_count", "active_days", "samples_before_accumulation"):
            if type(safe[field]) is not int or safe[field] < 0:
                raise ValueError("invalid_input")
        gap = safe["longest_sample_gap_hours"]
        if (safe["point_count"] == 0
                or not 1 <= safe["active_days"] <= min(31, safe["point_count"])
                or safe["samples_before_accumulation"] > safe["point_count"]
                or type(gap) not in (int, float) or gap < 0 or not math.isfinite(gap)
                or (safe["point_count"] < 2 and gap != 0)
                or safe["coverage"] not in ("not_started", "partial_window", "full_window")
                or safe["data_status"] != "samples"):
            raise ValueError("invalid_input")
        # JSON round-trip also enforces primitive serializability before any I/O.
        safe = json.loads(json.dumps(safe, allow_nan=False))
    except Exception:
        raise ValueError("invalid_deepseek_input") from None
    allowed = ["sample_limits"]
    if gap > 0:
        allowed.append("collection_gap")
    payload = {
        "model": "deepseek-chat",
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": (
                '你只根据汇总统计选择说明代码。只返回 JSON 对象 {"observations": [...]}，'
                "不得包含其他字段、Markdown 或自由文本。observations 必须为无重复的代码数组，"
                "可以为空；允许代码仅为 " + json.dumps(allowed) + "。"
                "sample_limits 表示样本不能还原连续轨迹；collection_gap 仅在样本间隔大于0时使用。"
                "禁止推断或返回设备信息、地址、坐标、人名、行程、离线原因或任何未提供的事实。")},
            {"role": "user", "content": json.dumps(safe, ensure_ascii=False, allow_nan=False)},
        ],
        "temperature": 0, "max_tokens": 128, "stream": False,
    }
    try:
        response = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
            json=payload, timeout=30, allow_redirects=False)
        try:
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("invalid_http_status")
            data = _strict_json(response.text)
        finally:
            response.close()
        if not isinstance(data, dict) or data.get("error") is not None:
            raise ValueError("invalid_response")
        choices = data.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError("invalid_response")
        choice = choices[0]
        if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
            raise ValueError("invalid_response")
        message = choice.get("message")
        if (not isinstance(message, dict) or message.get("role") != "assistant"
                or message.get("refusal") is not None):
            raise ValueError("invalid_response")
        result = _strict_json(message.get("content"))
        if not isinstance(result, dict) or set(result) != {"observations"}:
            raise ValueError("invalid_response")
        codes = result["observations"]
        if (not isinstance(codes, list) or len(codes) > len(allowed)
                or any(not isinstance(code, str) or code not in allowed for code in codes)
                or len(set(codes)) != len(codes)):
            raise ValueError("invalid_response")
        return {"observations": list(codes)}
    except Exception:
        raise RuntimeError("deepseek_summary_failed") from None


def _ai_notes(stats, key, summarizer):
    if not key:
        return "disabled", []
    if not stats["point_count"]:
        return "no_samples", []
    if summarizer is None:
        return "not_configured", []
    allowed = {"sample_limits": "样本只代表已保存的定位记录，不能还原连续轨迹。"}
    if stats["longest_sample_gap_hours"] > 0:
        allowed["collection_gap"] = "记录间存在采样间隔，原因无法从汇总数据判断。"
    try:
        result = summarizer(copy.deepcopy(stats), key)
        if not isinstance(result, dict) or set(result) != {"observations"}:
            return "invalid_response", []
        codes = result["observations"]
        if (not isinstance(codes, list) or len(codes) > len(allowed)
                or any(not isinstance(code, str) or code not in allowed for code in codes)):
            return "invalid_response", []
        return "ok", [allowed[code] for code in dict.fromkeys(codes)]
    except Exception:
        return "failed", []


def enrich_state(state, now=None, deepseek_key=None, *, summarizer=None):
    """Return an independent state with history/reports, ready for encryption.

    now: aware datetime or ISO string; defaults to common.now_utc(). Naive times
    raise a fixed ValueError. deepseek_key alone leaves the template usable;
    supply summarizer explicitly to enable optional AI observations. Existing
    reports remain unchanged unless a late real sample changes their summary.
    AI is attempted once per new/revised CLOSED-month report; there are no
    automatic retries. Current-month previews never call the summarizer.
    """
    instant = _time(common.now_utc() if now is None else now)
    if instant is None:
        raise ValueError("invalid_now")
    if not isinstance(state, dict):
        raise ValueError("invalid_state")
    out = copy.deepcopy(state)
    history = out.setdefault("history", {})
    reports = out.setdefault("reports", [])
    devices = out.get("devices", [])
    if (not isinstance(history, dict) or not isinstance(reports, list)
            or not isinstance(devices, list)):
        raise ValueError("invalid_state_schema")
    report_index = {}
    for item in reports:
        if (not isinstance(item, dict) or not isinstance(item.get("device_id"), str)
                or not isinstance(item.get("month"), str)):
            raise ValueError("invalid_reports_schema")
        report_index.setdefault((item["month"], item["device_id"]), item)
    if any(not isinstance(did, str) or not did for did in history):
        raise ValueError("invalid_history_schema")
    current = common.to_local(instant).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous = _month_offset(current, -1)
    stamp = common.utc_iso(instant)
    rows = {}
    for row in devices:
        if not isinstance(row, dict) or not isinstance(row.get("device_id"), str) or not row["device_id"]:
            raise ValueError("invalid_device_schema")
        rows.setdefault(row["device_id"], []).append(row.get("last_point"))
    for did in sorted(set(history) | set(rows)):
        entry = history.setdefault(did, {"started_at": stamp, "points": []})
        if (not isinstance(did, str) or not isinstance(entry, dict)
                or not isinstance(entry.get("points"), list)
                or _time(entry.get("started_at")) is None):
            raise ValueError("invalid_history_schema")
        unique = {}
        for raw in entry["points"] + rows.get(did, []):
            point = _point(raw, previous, instant)
            if point is not None:
                identity = (_time(point["ts"]), point["lat"], point["lon"])
                unique.setdefault(identity, point)
        entry["points"] = [unique[k] for k in sorted(unique)]
        # Backfill the last completed month on any run; maintain a free preview
        # for the current month from the very first accumulation call.
        for month_start in (previous, current):
            month = month_start.strftime("%Y-%m")
            is_final = month_start < current
            start_iso, end_iso = common.month_range(month)
            stats = _summary(entry["points"], entry["started_at"],
                             _time(start_iso), _time(end_iso))
            existing = report_index.get((month, did))
            if (existing is not None and existing.get("summary") == stats
                    and existing.get("is_final") is is_final):
                continue
            ai_status, notes = (_ai_notes(stats, deepseek_key, summarizer)
                                if is_final else ("preview", []))
            report_index[(month, did)] = {
                "month": month, "device_id": did, "generated_at": stamp,
                "summary": stats, "content": _template(month, stats, is_final),
                "source": "local", "is_final": is_final,
                "status": "completed" if is_final else "preview",
                "ai_status": ai_status, "ai_notes": notes}
    oldest = _month_offset(current, -REPORT_MONTHS).strftime("%Y-%m")
    newest = current.strftime("%Y-%m")
    # Validate month keys before comparing; never touch report contents otherwise.
    retained = []
    for (month, did), item in sorted(report_index.items()):
        try:
            valid = dt.datetime.strptime(month, "%Y-%m").strftime("%Y-%m") == month
        except (ValueError, TypeError):
            valid = False
        if valid and oldest <= month <= newest:
            retained.append(item)
    out["reports"] = retained
    return out
