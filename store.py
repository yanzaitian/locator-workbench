"""SQLite 存储：位置点、设备、报告、元信息。"""
import json
import sqlite3

import common
import schedule

SCHEMA = """
CREATE TABLE IF NOT EXISTS points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL DEFAULT 'default',
    ts_utc TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    alt REAL, acc REAL, speed REAL, battery REAL,
    source TEXT, raw TEXT,
    UNIQUE(device_id, ts_utc, lat, lon)
);
CREATE INDEX IF NOT EXISTS idx_points_ts ON points(ts_utc);
CREATE INDEX IF NOT EXISTS idx_points_dev ON points(device_id, ts_utc);
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    name TEXT,
    last_seen TEXT,
    last_lat REAL,
    last_lon REAL,
    last_battery REAL,
    last_source TEXT,
    app_version TEXT
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source TEXT,
    model TEXT,
    content TEXT NOT NULL,
    auto INTEGER DEFAULT 0,
    period_start TEXT,
    period_end TEXT
);
CREATE INDEX IF NOT EXISTS idx_reports_month ON reports(month);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def connect(cfg):
    conn = sqlite3.connect(common.db_path(cfg), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init(cfg):
    conn = connect(cfg)
    try:
        conn.executescript(SCHEMA)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(devices)").fetchall()]
        for name, ddl in (("enabled", "INTEGER DEFAULT 1"), ("mode", "TEXT DEFAULT 'collector'"),
                          ("interval_minutes", "INTEGER DEFAULT 60"), ("fid", "TEXT"),
                          ("user_id", "TEXT"), ("plan_json", "TEXT"),
                          ("online", "INTEGER"), ("charging", "INTEGER"), ("address", "TEXT"),
                          ("accuracy", "REAL"), ("status", "TEXT"), ("updated_at", "TEXT")):
            if name not in cols:
                conn.execute("ALTER TABLE devices ADD COLUMN %s %s" % (name, ddl))
        conn.commit()
        row = conn.execute("SELECT v FROM meta WHERE k='seeded'").fetchone()
        if not row:
            conn.execute("INSERT INTO meta(k,v) VALUES('seeded','1')")
            conn.commit()
    finally:
        conn.close()
    get_schedule(cfg)
    get_holidays(cfg)


def get_meta(cfg, key, default=None):
    conn = connect(cfg)
    try:
        row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default
    finally:
        conn.close()


def set_meta(cfg, key, value):
    conn = connect(cfg)
    try:
        conn.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                     (key, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()


def get_schedule(cfg):
    raw = get_meta(cfg, "schedule")
    if not raw:
        sched = schedule.normalize(schedule.DEFAULT_SCHEDULE)
        set_meta(cfg, "schedule", json.dumps(sched, ensure_ascii=False))
        return sched
    try:
        return schedule.normalize(json.loads(raw))
    except Exception:
        return schedule.normalize(schedule.DEFAULT_SCHEDULE)


def save_schedule(cfg, sched):
    sched = schedule.normalize(sched)
    set_meta(cfg, "schedule", json.dumps(sched, ensure_ascii=False))
    set_meta(cfg, "schedule_updated", common.utc_iso(common.now_utc()))
    return sched


def get_holidays(cfg):
    raw = get_meta(cfg, "holidays")
    if not raw:
        set_meta(cfg, "holidays", json.dumps(schedule.DEFAULT_HOLIDAYS, ensure_ascii=False))
        return schedule.DEFAULT_HOLIDAYS
    try:
        data = json.loads(raw)
        data.setdefault("statutory", [])
        data.setdefault("workdays", [])
        return data
    except Exception:
        return schedule.DEFAULT_HOLIDAYS


def save_holidays(cfg, data):
    statutory = sorted(set(x.strip() for x in (data.get("statutory") or []) if x.strip()))
    workdays = sorted(set(x.strip() for x in (data.get("workdays") or []) if x.strip()))
    out = {"statutory": statutory, "workdays": workdays}
    set_meta(cfg, "holidays", json.dumps(out, ensure_ascii=False))
    return out


def insert_points(cfg, points, device_id="default"):
    if not points:
        return 0
    rows = []
    for p in points:
        ts = common.utc_iso(p.get("ts"))
        try:
            lat = float(p.get("lat"))
            lon = float(p.get("lon"))
        except (TypeError, ValueError):
            continue
        if not ts:
            continue
        rows.append((device_id, ts, lat, lon, p.get("alt"), p.get("acc"), p.get("speed"),
                     p.get("battery"), p.get("source"), p.get("raw")))
    if not rows:
        return 0
    conn = connect(cfg)
    try:
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO points(device_id,ts_utc,lat,lon,alt,acc,speed,battery,source,raw) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        return conn.total_changes - before
    finally:
        conn.close()


def touch_device(cfg, device_id, name=None, battery=None, lat=None, lon=None, source=None, app_version=None):
    ts = common.utc_iso(common.now_utc())
    conn = connect(cfg)
    try:
        conn.execute(
            "INSERT INTO devices(device_id,name,last_seen,last_lat,last_lon,last_battery,last_source,app_version) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET "
            "name=COALESCE(excluded.name, devices.name), last_seen=excluded.last_seen, "
            "last_lat=COALESCE(excluded.last_lat, devices.last_lat), "
            "last_lon=COALESCE(excluded.last_lon, devices.last_lon), "
            "last_battery=COALESCE(excluded.last_battery, devices.last_battery), "
            "last_source=COALESCE(excluded.last_source, devices.last_source), "
            "app_version=COALESCE(excluded.app_version, devices.app_version)",
            (device_id, name, ts, lat, lon, battery, source, app_version))
        conn.commit()
    finally:
        conn.close()


def devices(cfg):
    conn = connect(cfg)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM devices ORDER BY last_seen DESC").fetchall()]
    finally:
        conn.close()


def update_device(cfg, device_id, **fields):
    allowed = ("enabled", "mode", "interval_minutes", "name", "plan_json", "fid", "user_id",
               "online", "charging", "address", "accuracy", "status", "updated_at")
    sets, args = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append("%s=?" % k)
            args.append(v)
    if not sets:
        return
    args.append(device_id)
    conn = connect(cfg)
    try:
        conn.execute("UPDATE devices SET " + ", ".join(sets) + " WHERE device_id=?", args)
        conn.commit()
    finally:
        conn.close()


def upsert_device(cfg, device_id, **fields):
    conn = connect(cfg)
    try:
        row = conn.execute("SELECT device_id FROM devices WHERE device_id=?", (device_id,)).fetchone()
        if row:
            conn.execute("UPDATE devices SET name=COALESCE(?,name), mode=COALESCE(?,mode), "
                         "interval_minutes=COALESCE(?,interval_minutes), fid=COALESCE(?,fid), "
                         "user_id=COALESCE(?,user_id) WHERE device_id=?",
                         (fields.get("name"), fields.get("mode"), fields.get("interval_minutes"),
                          fields.get("fid"), fields.get("user_id"), device_id))
        else:
            conn.execute("INSERT INTO devices(device_id,name,mode,interval_minutes,enabled,fid,user_id) "
                         "VALUES(?,?,?,?,1,?,?)",
                         (device_id, fields.get("name"), fields.get("mode") or "cloud",
                          fields.get("interval_minutes") or 60, fields.get("fid"), fields.get("user_id")))
        conn.commit()
    finally:
        conn.close()


def get_points(cfg, start_utc=None, end_utc=None, limit=200000, device_id=None):
    sql = "SELECT * FROM points WHERE 1=1"
    args = []
    if start_utc:
        sql += " AND ts_utc >= ?"
        args.append(start_utc)
    if end_utc:
        sql += " AND ts_utc < ?"
        args.append(end_utc)
    if device_id:
        sql += " AND device_id = ?"
        args.append(device_id)
    sql += " ORDER BY ts_utc ASC LIMIT ?"
    args.append(int(limit))
    conn = connect(cfg)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def last_point(cfg, device_id=None):
    conn = connect(cfg)
    try:
        if device_id:
            row = conn.execute("SELECT * FROM points WHERE device_id=? ORDER BY ts_utc DESC LIMIT 1",
                               (device_id,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM points ORDER BY ts_utc DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def total_count(cfg):
    conn = connect(cfg)
    try:
        return conn.execute("SELECT COUNT(*) AS c FROM points").fetchone()["c"]
    finally:
        conn.close()


def months_with_data(cfg):
    conn = connect(cfg)
    try:
        rows = conn.execute("SELECT ts_utc FROM points").fetchall()
    finally:
        conn.close()
    months = set()
    for r in rows:
        d = common.to_local(r["ts_utc"])
        if d:
            months.add(d.strftime("%Y-%m"))
    return sorted(months)


def add_report(cfg, month, content, source, model="", auto=False, period=None):
    period = period or common.month_range(month)
    conn = connect(cfg)
    try:
        cur = conn.execute(
            "INSERT INTO reports(month,created_at,source,model,content,auto,period_start,period_end) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (month, common.utc_iso(common.now_utc()), source, model, content, 1 if auto else 0,
             period[0], period[1]))
        conn.commit()
        rid = cur.lastrowid
    finally:
        conn.close()
    path = "%s.md" % month
    try:
        with open("%s/%s" % (common.reports_dir(cfg), path), "w", encoding="utf-8") as f:
            f.write(content + "\n")
    except Exception:
        pass
    return rid


def reports_summary(cfg):
    conn = connect(cfg)
    try:
        rows = conn.execute(
            "SELECT month, COUNT(*) AS versions, MAX(created_at) AS latest, MAX(id) AS latest_id, "
            "SUM(auto) AS auto_count FROM reports GROUP BY month ORDER BY month DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def report_versions(cfg, month):
    conn = connect(cfg)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, month, created_at, source, model, auto FROM reports WHERE month=? ORDER BY id DESC",
            (month,)).fetchall()]
    finally:
        conn.close()


def report_content(cfg, month, report_id=None):
    conn = connect(cfg)
    try:
        if report_id:
            row = conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM reports WHERE month=? ORDER BY id DESC LIMIT 1",
                               (month,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def latest_report_month(cfg):
    conn = connect(cfg)
    try:
        row = conn.execute("SELECT month FROM reports ORDER BY month DESC LIMIT 1").fetchone()
        return row["month"] if row else None
    finally:
        conn.close()
