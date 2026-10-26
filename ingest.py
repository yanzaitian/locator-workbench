"""解析手机上报的 GPX / KML / JSON / JSONL，写入数据库。"""
import json
import os
import re
import shutil
import time
import xml.etree.ElementTree as ET

import common
import store


def _tag(el):
    return el.tag.split("}")[-1] if isinstance(el.tag, str) else ""


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _child_map(el):
    out = {}
    for ch in el:
        t = _tag(ch).lower()
        if t not in out and ch.text is not None:
            out[t] = ch.text.strip()
    for ch in el.iter():
        t = _tag(ch).lower()
        if t in ("accuracy", "acc", "speed", "vel", "battery") and t not in out and ch.text:
            out[t] = ch.text.strip()
    return out


def parse_gpx(text):
    root = ET.fromstring(text)
    points = []
    for el in root.iter():
        if _tag(el).lower() not in ("trkpt", "wpt", "rtept"):
            continue
        lat, lon = _num(el.attrib.get("lat")), _num(el.attrib.get("lon"))
        if lat is None or lon is None:
            continue
        m = _child_map(el)
        ts = common.parse_time(m.get("time"))
        if ts is None:
            continue
        points.append({"ts": ts, "lat": lat, "lon": lon, "alt": _num(m.get("ele")),
                       "acc": _num(m.get("accuracy") or m.get("acc")),
                       "speed": _num(m.get("speed") or m.get("vel")),
                       "battery": _num(m.get("battery")), "source": "gpx"})
    return points


def parse_kml(text):
    root = ET.fromstring(text)
    points = []
    for track in root.iter():
        if _tag(track).lower() != "track":
            continue
        whens, coords = [], []
        for ch in track:
            t = _tag(ch).lower()
            if t == "when" and ch.text:
                whens.append(ch.text.strip())
            elif t == "coord" and ch.text:
                coords.append(ch.text.strip())
        for w, c in zip(whens, coords):
            parts = re.split(r"[\s,]+", c)
            if len(parts) < 2:
                continue
            lon, lat = _num(parts[0]), _num(parts[1])
            ts = common.parse_time(w)
            if lat is None or lon is None or ts is None:
                continue
            points.append({"ts": ts, "lat": lat, "lon": lon,
                           "alt": _num(parts[2]) if len(parts) > 2 else None, "source": "kml"})
    for pm in root.iter():
        if _tag(pm).lower() != "placemark":
            continue
        when = None
        coord_el = None
        for el in pm.iter():
            t = _tag(el).lower()
            if t == "when" and el.text and when is None:
                when = el.text.strip()
            if t == "coordinates" and el.text and coord_el is None:
                coord_el = el.text.strip()
        if not when or not coord_el:
            continue
        parts = re.split(r"[\s,]+", coord_el)
        if len(parts) < 2:
            continue
        lon, lat = _num(parts[0]), _num(parts[1])
        ts = common.parse_time(when)
        if lat is None or lon is None or ts is None:
            continue
        points.append({"ts": ts, "lat": lat, "lon": lon,
                       "alt": _num(parts[2]) if len(parts) > 2 else None, "source": "kml"})
    return points


def _point_from_dict(d):
    if not isinstance(d, dict):
        return None
    lat = lon = None
    for k in ("lat", "latitude", "Lat", "Latitude"):
        if k in d:
            lat = _num(d[k]); break
    for k in ("lon", "lng", "longitude", "Lon", "Longitude"):
        if k in d:
            lon = _num(d[k]); break
    if lat is None or lon is None:
        return None
    ts = None
    for k in ("ts", "time", "timestamp", "date", "datetime", "Time", "created_at"):
        if k in d:
            ts = common.parse_time(d[k])
            if ts:
                break
    if ts is None:
        return None
    return {"ts": ts, "lat": lat, "lon": lon,
            "alt": _num(d.get("alt") or d.get("ele") or d.get("altitude")),
            "acc": _num(d.get("acc") or d.get("accuracy") or d.get("acc_m")),
            "speed": _num(d.get("speed") or d.get("velocity")),
            "battery": _num(d.get("battery") or d.get("batt")),
            "source": d.get("source") or "json"}


def parse_json(text):
    data = json.loads(text)
    if isinstance(data, dict):
        for key in ("points", "locations", "data", "items", "track"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        return []
    return [p for p in (_point_from_dict(x) for x in data) if p]


def parse_jsonl(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            p = _point_from_dict(json.loads(line))
        except json.JSONDecodeError:
            continue
        if p:
            out.append(p)
    return out


def parse_text(text):
    head = text.lstrip()[:200].lower()
    if "<gpx" in head:
        return parse_gpx(text)
    if "<kml" in head:
        return parse_kml(text)
    if head.startswith("[") or head.startswith("{"):
        try:
            return parse_json(text)
        except json.JSONDecodeError:
            return parse_jsonl(text)
    return parse_jsonl(text)


def import_file(cfg, path, device_id="email"):
    name = os.path.basename(path)
    if name.startswith("dev__"):
        parts = name.split("__", 2)
        if len(parts) >= 2 and parts[1]:
            device_id = parts[1]
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()
    points = parse_text(text)
    if not points:
        raise ValueError("未找到可识别的位置点")
    n = store.insert_points(cfg, points, device_id=device_id)
    return n, len(points)


def move_to_archive(cfg, path, ok=True):
    dest_dir = os.path.join(common.archive_dir(cfg), "ok" if ok else "failed")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, time.strftime("%Y%m%d-%H%M%S-") + os.path.basename(path))
    try:
        shutil.move(path, dest)
    except Exception:
        pass
    return dest


def scan_inbox(cfg, device_id="email"):
    stats = {"files": 0, "inserted": 0, "parsed": 0, "failed": 0}
    d = common.inbox_dir(cfg)
    if not os.path.isdir(d):
        return stats
    for name in sorted(os.listdir(d)):
        path = os.path.join(d, name)
        if not os.path.isfile(path):
            continue
        if os.path.splitext(name)[1].lower() not in (".gpx", ".kml", ".json", ".jsonl", ".txt", ".xml"):
            continue
        stats["files"] += 1
        try:
            inserted, total = import_file(cfg, path, device_id=device_id)
            stats["inserted"] += inserted
            stats["parsed"] += total
            move_to_archive(cfg, path, ok=True)
        except Exception:
            stats["failed"] += 1
            move_to_archive(cfg, path, ok=False)
    return stats
