"""定位工作台 · 公共配置与工具（纯标准库）。"""
import copy
import datetime as dt
import json
import math
import os
import socket
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("WORKBENCH_CONFIG") or os.path.join(BASE_DIR, "config.json")

DEFAULTS = {
    "server": {"host": "0.0.0.0", "port": 8765, "open_browser": True},
    "data_dir": "",
    "access_token": "",
    "imap": {
        "enabled": False,
        "host": "imap.qq.com",
        "port": 993,
        "user": "",
        "password": "",
        "folder": "INBOX",
        "sender_contains": "",
        "subject_contains": "GPS",
        "days_back": 60,
    },
    "deepseek": {
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "timeout_seconds": 240,
    },
    "smtp": {"host": "smtp.qq.com", "port": 465, "user": "", "password": ""},
    "phone_email": "",
    "auto_report": {"enabled": True, "catch_up_months": 12},
    "alert": {"max_silence_hours": 6},
}


def _default_data_dir():
    # 按用户要求：DeepSeek 相关内容全部放在 E:\Deepseek Harness
    preferred = r"E:\Deepseek Harness\定位工作台\data"
    try:
        os.makedirs(preferred, exist_ok=True)
        return preferred
    except Exception:
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(root, "LocatorWorkbench")


def deep_update(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_update(base[k], v)
        else:
            base[k] = v


def _write_default():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["data_dir"] = _default_data_dir()
    cfg["access_token"] = uuid.uuid4().hex[:16]
    os.makedirs(os.path.dirname(CONFIG_PATH) or ".", exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def load_config():
    if not os.path.exists(CONFIG_PATH):
        _write_default()
    cfg = copy.deepcopy(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            deep_update(cfg, json.load(f))
    except Exception:
        pass
    if not cfg.get("access_token"):
        cfg["access_token"] = uuid.uuid4().hex[:16]
        save_config(cfg)
    dd = cfg.get("data_dir") or _default_data_dir()
    cfg["data_dir"] = os.path.abspath(os.path.expanduser(dd))
    for sub in ("inbox", "archive", "reports"):
        os.makedirs(os.path.join(cfg["data_dir"], sub), exist_ok=True)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def db_path(cfg):
    return os.path.join(cfg["data_dir"], "workbench.db")


def inbox_dir(cfg):
    return os.path.join(cfg["data_dir"], "inbox")


def archive_dir(cfg):
    return os.path.join(cfg["data_dir"], "archive")


def reports_dir(cfg):
    return os.path.join(cfg["data_dir"], "reports")


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"
    finally:
        try:
            s.close()
        except Exception:
            pass


# ---------- 时间 ----------
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    LOCAL_TZ = dt.timezone(dt.timedelta(hours=8), "CST")


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def now_local():
    return now_utc().astimezone(LOCAL_TZ)


def utc_iso(value):
    if isinstance(value, str):
        value = parse_time(value)
    if value is None:
        return None
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e11:
            v /= 1000.0
        try:
            return dt.datetime.fromtimestamp(v, dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value).strip()
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        d = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                d = dt.datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        if d is None:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=LOCAL_TZ)
    return d.astimezone(dt.timezone.utc)


def to_local(ts):
    d = parse_time(ts) if not isinstance(ts, dt.datetime) else ts
    if d is None:
        return None
    return d.astimezone(LOCAL_TZ)


def fmt_local(ts, fmt="%Y-%m-%d %H:%M:%S"):
    d = to_local(ts)
    return d.strftime(fmt) if d else ""


def month_range(month):
    y, m = int(month.split("-")[0]), int(month.split("-")[1])
    start = dt.datetime(y, m, 1, tzinfo=LOCAL_TZ)
    end = dt.datetime(y + 1, 1, 1, tzinfo=LOCAL_TZ) if m == 12 else dt.datetime(y, m + 1, 1, tzinfo=LOCAL_TZ)
    return utc_iso(start), utc_iso(end)


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


_A = 6378245.0
_EE = 0.00669342162296594323


def _out_of_china(lat, lon):
    return not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _tl(x, y):
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _tg(x, y):
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lat, lon):
    if _out_of_china(lat, lon):
        return lat, lon
    dlat = _tl(lon - 105.0, lat - 35.0)
    dlon = _tg(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = 1 - _EE * math.sin(radlat) ** 2
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * math.pi)
    dlon = (dlon * 180.0) / (_A / sqrtmagic * math.cos(radlat) * math.pi)
    return lat + dlat, lon + dlon
