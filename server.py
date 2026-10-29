"""定位工作台 · Web 服务（标准库）。"""
import argparse
import json
import hmac
import os
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import common
import local_auth
import imap_fetch
import ingest
import mailer
import reports
import schedule
import store
import xiaomi_cloud

TEMPLATE_DIR = os.path.join(common.BASE_DIR, "templates")
SYNC_LOCK = threading.Lock()
XIAOMI_AUTH_LOCK = threading.RLock()
AUTO_STATUS = {"running": False, "done": 0, "total": 0, "error": None, "last_run": None}


def _mask(value, keep=4):
    if not value:
        return ""
    s = str(value)
    if len(s) <= keep:
        return "*" * len(s)
    return "*" * (len(s) - keep) + s[-keep:]


def _location_sources(cfg):
    sources = cfg.get("location_sources") or {}
    return {key: sources.get(key, True) for key in ("direct", "mail")}


def sync_all(cfg):
    mail_enabled = _location_sources(cfg)["mail"]
    out = {"inbox": ingest.scan_inbox(cfg, device_id="email") if mail_enabled else 0}
    if mail_enabled and cfg["imap"].get("enabled"):
        out["imap"] = imap_fetch.fetch(cfg)
    return out


def _xiaomi_saved(cfg):
    return bool(store.get_meta(cfg, "xiaomi_token") and store.get_meta(cfg, "xiaomi_uid"))


def _accept_xiaomi(cfg, token, uid, username=None, cookies=None):
    """Validate the candidate before atomically replacing the saved credential pair."""
    with XIAOMI_AUTH_LOCK:
        client = xiaomi_cloud.Client(token, uid, cookies=cookies) if cookies is not None else xiaomi_cloud.Client(token, uid)
        try:
            devices = client.device_list()
            snapshot = client.cookie_snapshot()
            credentials = client.current_credentials()
            if isinstance(snapshot, list) and isinstance(credentials, tuple) and len(credentials) == 2:
                token, uid = credentials
                cookies = snapshot
        finally:
            client.session.close()
        metadata = [xiaomi_cloud.device_meta(device) for device in devices]
        values = [("xiaomi_token", token), ("xiaomi_uid", uid),
                  ("xiaomi_cookies", json.dumps(cookies) if cookies is not None else ""),
                  ("xiaomi_authorized_at", common.utc_iso(common.now_utc()))]
        if username is not None:
            values.append(("xiaomi_user", username))
        conn = store.connect(cfg)
        try:
            with conn:
                conn.executemany(
                    "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    values)
                # Device import and credentials commit together. Reauthorizing must
                # preserve an existing device's polling interval and saved plan.
                conn.executemany(
                    "INSERT INTO devices(device_id,name,mode,interval_minutes,enabled,fid,user_id) "
                    "VALUES(?,?,'cloud',60,1,?,?) ON CONFLICT(device_id) DO UPDATE SET "
                    "name=excluded.name,mode=excluded.mode,fid=excluded.fid,user_id=excluded.user_id",
                    [(meta["device_id"], meta["name"], meta["fid"], meta["user_id"])
                     for meta in metadata])
        finally:
            conn.close()
        return metadata


def _xiaomi_client(cfg):
    with XIAOMI_AUTH_LOCK:
        token = store.get_meta(cfg, "xiaomi_token")
        uid = store.get_meta(cfg, "xiaomi_uid")
        raw = store.get_meta(cfg, "xiaomi_cookies")
        if not raw:
            return xiaomi_cloud.Client(token, uid)
        try:
            cookies = json.loads(raw)
            return xiaomi_cloud.Client(token, uid, cookies=cookies)
        except (ValueError, xiaomi_cloud.XiaomiError):
            # Invalid optional session data must not erase the saved pair.
            return xiaomi_cloud.Client(token, uid)


def _persist_xiaomi_session(cfg, client, expected_token):
    """Persist rotations only if this request still belongs to the current login."""
    with XIAOMI_AUTH_LOCK:
        if store.get_meta(cfg, "xiaomi_token") != expected_token:
            return
        try:
            cookies = client.cookie_snapshot()
            if not isinstance(cookies, list):
                return
        except xiaomi_cloud.XiaomiError:
            return
        values = [("xiaomi_cookies", json.dumps(cookies))]
        try:
            credentials = client.current_credentials()
        except xiaomi_cloud.XiaomiError:
            # A server deletion is meaningful: retain the snapshot without the
            # revoked token, so a new Client cannot resurrect legacy credentials.
            credentials = None
        if isinstance(credentials, tuple) and len(credentials) == 2:
            token, uid = credentials
            values.extend([("xiaomi_token", token), ("xiaomi_uid", uid)])
        conn = store.connect(cfg)
        try:
            with conn:
                conn.executemany(
                    "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    values)
        finally:
            conn.close()


def run_auto_reports(cfg, force=False):
    with SYNC_LOCK:
        if AUTO_STATUS["running"] and not force:
            return
        AUTO_STATUS.update({"running": True, "done": 0, "total": 0, "error": None})
        try:
            generated = reports.auto_generate_missing(cfg, log=lambda m: print("[自动汇报] " + m))
            AUTO_STATUS["done"] = len(generated)
            AUTO_STATUS["total"] = len(generated)
            AUTO_STATUS["last_run"] = common.fmt_local(common.now_utc())
        except Exception as exc:  # noqa: BLE001
            AUTO_STATUS["error"] = "%s: %s" % (type(exc).__name__, exc)
        finally:
            AUTO_STATUS["running"] = False


def _public_url():
    path = os.path.join(common.BASE_DIR, "tools", "tunnel_url.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            url = f.read().strip()
        return url if url.startswith("http") else ""
    except Exception:
        return ""


def _point_json(p):
    lat, lon = p["lat"], p["lon"]
    glat, glon = common.wgs84_to_gcj02(lat, lon)
    return {"ts": p["ts_utc"], "local": common.fmt_local(p["ts_utc"]),
            "lat": round(lat, 6), "lon": round(lon, 6),
            "gcj_lat": round(glat, 6), "gcj_lon": round(glon, 6),
            "acc": p.get("acc"), "battery": p.get("battery"),
            "source": p.get("source"), "device_id": p.get("device_id")}


def build_state(cfg, device=None):
    now = common.now_local()
    sched = store.get_schedule(cfg)
    holidays = store.get_holidays(cfg)
    last = store.last_point(cfg, device)
    today_start = common.utc_iso(now.replace(hour=0, minute=0, second=0, microsecond=0))
    week_start = common.utc_iso(now - __import__("datetime").timedelta(days=7))
    today_pts = store.get_points(cfg, today_start, limit=50000, device_id=device)
    week_pts = store.get_points(cfg, week_start, limit=50000, device_id=device)
    days_map = {}
    for p in week_pts:
        days_map.setdefault(common.to_local(p["ts_utc"]).strftime("%Y-%m-%d"), []).append(p)
    day_rows = []
    for key in sorted(days_map):
        items = days_map[key]
        dist = 0.0
        for i in range(1, len(items)):
            step = common.haversine_m(items[i - 1]["lat"], items[i - 1]["lon"],
                                      items[i]["lat"], items[i]["lon"])
            if step < 2000:
                dist += step
        day_rows.append({"date": key, "points": len(items),
                         "first": common.fmt_local(items[0]["ts_utc"], "%H:%M"),
                         "last": common.fmt_local(items[-1]["ts_utc"], "%H:%M"),
                         "distance_km": round(dist / 1000.0, 2)})
    today_info = schedule.describe_today(sched, holidays)
    nxt = schedule.next_run(sched, holidays)
    devices = store.devices(cfg)
    for d in devices:
        d["last_local"] = common.fmt_local(d.get("last_seen"))
        lp = store.last_point(cfg, d["device_id"])
        d["last_point"] = _point_json(lp) if lp else None
    return {
        "ok": True,
        "server_time": common.fmt_local(common.now_utc()),
        "lan_url": "http://%s:%d/" % (common.lan_ip(), int(cfg["server"].get("port") or 8765)),
        "public_url": _public_url(),
        "token": cfg["access_token"],
        "last": _point_json(last) if last else None,
        "silence_hours": round((common.now_utc() - common.parse_time(last["ts_utc"])).total_seconds() / 3600, 2)
        if last else None,
        "alert_hours": float(cfg["alert"].get("max_silence_hours") or 6),
        "counts": {"today": len(today_pts), "week": len(week_pts), "total": store.total_count(cfg)},
        "days": day_rows,
        "devices": devices,
        "selected_device": device or "",
        "schedule": sched,
        "schedule_updated": store.get_meta(cfg, "schedule_updated"),
        "holidays": holidays,
        "today": today_info,
        "next_run": nxt.strftime("%Y-%m-%d %H:%M") if nxt else None,
        "reports": store.reports_summary(cfg),
        "auto_status": dict(AUTO_STATUS),
        "config": {
            "imap_enabled": bool(cfg["imap"].get("enabled")),
            "imap_user": cfg["imap"].get("user") or "",
            "deepseek_enabled": bool(cfg["deepseek"].get("api_key")),
            "deepseek_model": cfg["deepseek"].get("model") or "",
            "auto_report": bool(cfg["auto_report"].get("enabled", True)),
            "smtp_ready": bool((cfg.get("smtp") or {}).get("user") and (cfg.get("smtp") or {}).get("password")),
            "phone_email": cfg.get("phone_email") or "",
            "data_dir": cfg["data_dir"],
            "config_file": common.CONFIG_PATH,
        },
    }


class Handler(BaseHTTPRequestHandler):
    cfg = None

    def log_message(self, fmt, *args):
        pass

    # ---------- 基础响应 ----------
    def _send(self, body, code=200, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, obj, code=200):
        self._send(obj, code)

    def _body(self):
        if hasattr(self, "_parsed_body"):
            return self._parsed_body
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 <= n <= 2 * 1024 * 1024:
                raise ValueError()
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            if not isinstance(body, dict):
                raise ValueError()
            self._parsed_body = body
            return body
        except (ValueError, UnicodeError):
            raise ValueError("请求须为 JSON 对象且不超过 2MB") from None

    @property
    def auth(self):
        return self.server.local_auth

    def _session(self):
        return self.auth.get_session(self.headers.get("Cookie"))

    def _cookie(self, session=None):
        value = session["id"] if session else ""
        age = 8 * 3600 if session and session["authenticated"] else 600 if session else 0
        secure = "; Secure" if isinstance(self.connection, __import__("ssl").SSLSocket) else ""
        return "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict%s" % (local_auth.COOKIE_NAME, value, age, secure)

    def _origin_ok(self):
        if self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return False
        try:
            parsed = urllib.parse.urlsplit(origin)
            return (parsed.scheme == ("https" if isinstance(self.connection, __import__("ssl").SSLSocket) else "http") and parsed.netloc.lower() ==
                    self.headers.get("Host", "").lower() and not parsed.path and not parsed.query and not parsed.fragment)
        except ValueError:
            return False

    def _write_ok(self, session):
        token = self.headers.get("X-CSRF-Token", "")
        return bool(session and self._origin_ok() and hmac.compare_digest(token.encode("utf-8"), session["csrf"].encode("utf-8")))

    def _local_setup_ok(self):
        try:
            host = urllib.parse.urlsplit("//" + self.headers.get("Host", "")).hostname
            return local_auth.is_loopback(self.client_address[0]) and (host == "localhost" or local_auth.is_loopback(host))
        except ValueError:
            return False

    def _require_login(self, path):
        session = self._session()
        if session and session["authenticated"]:
            return True
        if path in ("/", "/index.html"):
            self._send(b"", 303, extra={"Location": "/login"})
        else:
            self._json({"ok": False, "error": "请先登录工作台"}, 401)
        return False

    def _login_page(self):
        if not self.auth.configured and not self._local_setup_ok():
            return self._json({"ok": False, "error": "仅允许在本机回环地址设置初始密码"}, 403)
        session = self._session() or self.auth.new_session()
        with open(os.path.join(TEMPLATE_DIR, "login.html"), encoding="utf-8") as f:
            page = f.read().replace("__CSRF_TOKEN__", session["csrf"]).replace("__AUTH_MODE__", "login" if self.auth.configured else "setup")
        return self._send(page, ctype="text/html; charset=utf-8", extra={"Set-Cookie": self._cookie(session)})

    def _auth_post(self, path):
        session = self._session()
        if not self._write_ok(session):
            return self._json({"ok": False, "error": "安全校验失败，请刷新页面重试"}, 403)
        if path == "/api/auth/setup":
            if not self._local_setup_ok():
                return self._json({"ok": False, "error": "远程请求不能初始化密码"}, 403)
            password = self._body().get("password")
            with self.auth.lock:
                if self.auth.configured:
                    return self._json({"ok": False, "error": "密码已设置，请登录"}, 409)
                self.auth.set_password(password)
                new_session = self.auth.new_session(True)
        elif path == "/api/auth/login":
            body = self._body()
            new_session, code = self.auth.login(body.get("password"), self.client_address[0])
            if not new_session:
                return self._json({"ok": False, "error": "尝试过于频繁，请稍后再试" if code == 429 else "密码错误或尚未初始化"}, code)
            self.auth.drop_session(session)
        else:
            if not session["authenticated"]:
                return self._json({"ok": False, "error": "请先登录"}, 401)
            if path == "/api/auth/logout":
                self.auth.drop_session(session)
                return self._send({"ok": True}, extra={"Set-Cookie": self._cookie()})
            if path != "/api/auth/password":
                return self._json({"ok": False, "error": "not found"}, 404)
            body = self._body()
            with self.auth.lock:
                _, code = self.auth.login(body.get("current_password"), self.client_address[0])
                if code != 200:
                    return self._json({"ok": False, "error": "当前密码错误或尝试过于频繁"}, code)
                self.auth.set_password(body.get("password"))
                new_session = self.auth.new_session(True)
        return self._send({"ok": True, "csrf": new_session["csrf"]}, extra={"Set-Cookie": self._cookie(new_session)})

    def _sync_xiaomi(self, device_id=None):
        token = store.get_meta(self.cfg, "xiaomi_token")
        uid = store.get_meta(self.cfg, "xiaomi_uid")
        if not token or not uid:
            return {"ok": False, "error": "还没登录小米账号"}
        client = _xiaomi_client(self.cfg)
        expected_token = client.token
        out = {"ok": True, "updated": []}
        try:
            devices = client.device_list()
        except Exception as exc:  # noqa: BLE001
            _persist_xiaomi_session(self.cfg, client, expected_token)
            client.session.close()
            return {"ok": False, "error": "设备列表失败", "diagnostic": xiaomi_cloud.device_list_diagnostic(exc)}
        for d in devices:
            meta = xiaomi_cloud.device_meta(d)
            if device_id and meta["device_id"] != device_id:
                continue
            try:
                st = client.device_status(d, 2 if device_id else 0)
                point = xiaomi_cloud.extract(st) if st else None
                if st:
                    store.update_device(self.cfg, meta["device_id"], **xiaomi_cloud.status_of(st))
                if point:
                    store.insert_points(self.cfg, [point], device_id=meta["device_id"])
                    store.touch_device(self.cfg, meta["device_id"], name=meta["name"],
                                       battery=point.get("battery"), lat=point["lat"], lon=point["lon"],
                                       source="xiaomi-cloud")
                out["updated"].append(meta["device_id"])
            except Exception:  # noqa: BLE001
                continue
        _persist_xiaomi_session(self.cfg, client, expected_token)
        client.session.close()
        return out

    def _token_ok(self, qs):
        token = (qs.get("token", [""])[0] or self.headers.get("X-Token") or "").strip()
        return bool(token) and hmac.compare_digest(token.encode("utf-8"), self.cfg["access_token"].encode("utf-8"))

    # ---------- 路由 ----------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/login":
                return self._login_page()
            if path.startswith("/api/phone/"):
                if not self._token_ok(qs):
                    return self._json({"ok": False, "error": "token 无效"}, 403)
            elif not self._require_login(path):
                return
            if path in ("/api/xiaomi/qr", "/api/xiaomi/qr/poll"):
                return self._json({"ok": False, "error": "请使用 POST 和 CSRF 校验"}, 405)
            if path in ("/", "/index.html"):
                with open(os.path.join(TEMPLATE_DIR, "index.html"), "r", encoding="utf-8") as f:
                    return self._send(f.read().replace("__CSRF_TOKEN__", self._session()["csrf"]), ctype="text/html; charset=utf-8")
            if path == "/favicon.ico":
                return self._send(b"", 204, "image/x-icon")
            if path == "/api/state":
                dev = qs.get("device", [""])[0] or None
                return self._json(build_state(self.cfg, dev))
            if path == "/api/points":
                start = qs.get("start", [None])[0]
                end = qs.get("end", [None])[0]
                limit = int(qs.get("limit", ["2000"])[0])
                dev = qs.get("device", [""])[0] or None
                pts = store.get_points(self.cfg, start, end, limit=limit, device_id=dev)
                return self._json({"ok": True, "points": [_point_json(p) for p in pts]})
            if path == "/api/reports":
                return self._json({"ok": True, "reports": store.reports_summary(self.cfg)})
            if path == "/api/report":
                month = qs.get("month", [""])[0]
                rid = qs.get("id", [""])[0]
                row = store.report_content(self.cfg, month, int(rid) if rid else None)
                if not row:
                    return self._json({"ok": False, "error": "没有该月汇报"}, 404)
                row["versions"] = store.report_versions(self.cfg, month)
                return self._json({"ok": True, **row})
            if path == "/api/report/export":
                month = qs.get("month", [""])[0]
                row = store.report_content(self.cfg, month)
                if not row:
                    return self._json({"ok": False, "error": "没有该月汇报"}, 404)
                fname = "定位工作台-%s.md" % month
                return self._send(row["content"], ctype="text/markdown; charset=utf-8",
                                  extra={"Content-Disposition": "attachment; filename*=UTF-8''%s"
                                         % urllib.parse.quote(fname)})
            if path == "/api/settings":
                c = self.cfg
                return self._json({"ok": True, "settings": {
                    "xiaomi": {"credentials_saved": _xiaomi_saved(c)},
                    "location_sources": _location_sources(c),
                    "imap": {"enabled": c["imap"].get("enabled"), "host": c["imap"].get("host"),
                             "port": c["imap"].get("port"), "user": c["imap"].get("user"),
                             "password_set": bool(c["imap"].get("password")),
                             "folder": c["imap"].get("folder"),
                             "subject_contains": c["imap"].get("subject_contains"),
                             "sender_contains": c["imap"].get("sender_contains")},
                    "deepseek": {"api_key_set": bool(c["deepseek"].get("api_key")),
                                 "api_key_tail": _mask(c["deepseek"].get("api_key"), 4),
                                 "model": c["deepseek"].get("model"),
                                 "base_url": c["deepseek"].get("base_url")},
                    "smtp": {"host": (c.get("smtp") or {}).get("host"),
                             "port": (c.get("smtp") or {}).get("port"),
                             "user": (c.get("smtp") or {}).get("user"),
                             "password_set": bool((c.get("smtp") or {}).get("password"))},
                    "phone_email": c.get("phone_email") or "",
                    "alert": c["alert"],
                    "auto_report": c["auto_report"],
                    "access_token": c["access_token"],
                }})
            # ---------- 手机端 API ----------
            if path == "/api/phone/config":
                if not self._token_ok(qs):
                    return self._json({"ok": False, "error": "token 无效"}, 403)
                sched = store.get_schedule(self.cfg)
                holidays = store.get_holidays(self.cfg)
                nxt = schedule.next_run(sched, holidays)
                return self._json({"ok": True, "server_time": common.fmt_local(common.now_utc()),
                                   "server_utc": common.utc_iso(common.now_utc()),
                                   "schedule": sched, "holidays": holidays,
                                   "schedule_updated": store.get_meta(self.cfg, "schedule_updated"),
                                   "next_run": nxt.strftime("%Y-%m-%d %H:%M") if nxt else None})
            if path == "/api/phone/ping":
                return self._json({"ok": True, "name": "定位工作台", "time": common.fmt_local(common.now_utc())})
            return self._json({"ok": False, "error": "not found"}, 404)
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            return self._json({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}, 500)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            # Consume bounded input before rejecting: Windows may otherwise reset
            # a connection with unread POST bytes before the 401/403 reaches it.
            self.connection.settimeout(10)
            self._body()
            if path.startswith("/api/auth/"):
                return self._auth_post(path)
            if path.startswith("/api/phone/"):
                if not self._token_ok(qs):
                    return self._json({"ok": False, "error": "token 无效"}, 403)
            else:
                if not self._require_login(path):
                    return
                if not self._write_ok(self._session()):
                    return self._json({"ok": False, "error": "安全校验失败，请刷新页面重试"}, 403)
            if path == "/api/schedule":
                body = self._body()
                validated = schedule.validate_schedule(body.get("schedule") or {}, minimum=5)
                sched = store.save_schedule(self.cfg, validated)
                if body.get("holidays") is not None:
                    store.save_holidays(self.cfg, body["holidays"])
                push = None
                if body.get("push"):
                    try:
                        mailer.push_schedule(self.cfg)
                        push = "sent"
                    except Exception as exc:  # noqa: BLE001
                        push = "failed: %s" % exc
                return self._json({"ok": True, "schedule": sched,
                                   "holidays": store.get_holidays(self.cfg), "push": push})
            if path == "/api/report":
                body = self._body()
                month = (body.get("month") or "").strip()
                if not month:
                    return self._json({"ok": False, "error": "缺少月份"}, 400)
                row = reports.generate(self.cfg, month, auto=False)
                return self._json({"ok": True, **{k: row[k] for k in ("id", "month", "source", "error", "auto")},
                                   "content": row["content"]})
            if path == "/api/sync":
                return self._json({"ok": True, "result": sync_all(self.cfg)})
            if path == "/api/auto_report":
                threading.Thread(target=run_auto_reports, args=(self.cfg,), daemon=True).start()
                return self._json({"ok": True, "status": dict(AUTO_STATUS)})
            if path == "/api/xiaomi/check":
                # A live result at checked_at; saving alone never proves connectivity.
                with XIAOMI_AUTH_LOCK:
                    saved = _xiaomi_saved(self.cfg)
                    result = {"ok": False, "credentials_saved": saved, "connected": False}
                    if not saved:
                        return self._json({**result, "diagnostic": "credentials_missing"})
                    client = _xiaomi_client(self.cfg)
                    expected_token = client.token
                    try:
                        devices = client.device_list()
                    except Exception as exc:
                        result["diagnostic"] = xiaomi_cloud.device_list_diagnostic(exc)
                    else:
                        result.update(ok=True, connected=True, devices=len(devices))
                    finally:
                        _persist_xiaomi_session(self.cfg, client, expected_token)
                        client.session.close()
                    result["checked_at"] = common.utc_iso(common.now_utc())
                    return self._json(result)
            if path == "/api/xiaomi/login":
                body = self._body()
                try:
                    token, uid, _header = xiaomi_cloud.login(body.get("username", ""), body.get("password", ""))
                    added = _accept_xiaomi(self.cfg, token, uid, body.get("username", ""))
                except Exception as exc:
                    return self._json({"ok": False, "error": "小米授权验证失败，未替换已保存凭据",
                                       "diagnostic": xiaomi_cloud.device_list_diagnostic(exc)})
                return self._json({"ok": True, "devices": added, "credentials_saved": True,
                                   "connected": True, "checked_at": common.utc_iso(common.now_utc())})
            if path == "/api/xiaomi/qr":
                try:
                    ticket, qr, tips = xiaomi_cloud.qr_start()
                except Exception:
                    return self._json({"ok": False, "error": "小米二维码请求失败，请重试"})
                return self._json({"ok": True, "ticket": ticket, "qr": qr, "tips": tips})
            if path == "/api/xiaomi/cookie":
                body = self._body()
                cookie = body.get("cookie") or ""
                if not isinstance(cookie, str) or "\r" in cookie or "\n" in cookie:
                    return self._json({"ok": False, "error": "Cookie 格式无效"}, 400)
                parsed = {}
                for part in cookie.strip().split(";"):
                    if "=" in part:
                        k, _, v = part.strip().partition("=")
                        if k in ("serviceToken", "userId") and k in parsed:
                            return self._json({"ok": False, "error": "Cookie 包含重复授权字段"}, 400)
                        parsed[k] = v
                token, uid = parsed.get("serviceToken"), parsed.get("userId")
                if not token or not uid:
                    return self._json({"ok": False, "error": "Cookie 里缺少 serviceToken 或 userId"})
                try:
                    devices = _accept_xiaomi(self.cfg, token, uid)
                except Exception as exc:
                    return self._json({"ok": False, "error": "Cookie 验证失败，未替换已保存凭据",
                                       "diagnostic": xiaomi_cloud.device_list_diagnostic(exc)})
                return self._json({"ok": True, "devices": len(devices), "credentials_saved": True,
                                   "connected": True, "checked_at": common.utc_iso(common.now_utc())})
            if path == "/api/xiaomi/qr/poll":
                body = self._body()
                ticket = body.get("ticket", "")
                if not isinstance(ticket, str) or not ticket:
                    return self._json({"ok": False, "status": "缺少 ticket"}, 400)
                try:
                    result, status = xiaomi_cloud.qr_poll(ticket, include_cookies=True)
                    if not result:
                        return self._json({"ok": False, "status": status})
                    token, uid = result[:2]
                    cookies = result[2] if len(result) > 2 else None
                    devices = _accept_xiaomi(self.cfg, token, uid, cookies=cookies)
                except Exception as exc:
                    return self._json({"ok": False, "status": "授权验证失败，未替换已保存凭据",
                                       "diagnostic": xiaomi_cloud.device_list_diagnostic(exc)})
                return self._json({"ok": True, "status": "ok", "devices": len(devices),
                                   "credentials_saved": True, "connected": True,
                                   "checked_at": common.utc_iso(common.now_utc())})
            if path == "/api/xiaomi/sync":
                body = self._body()
                return self._json(self._sync_xiaomi(body.get("device_id")))
            if path == "/api/device/update":
                body = self._body()
                did = body.get("device_id")
                if not did or not any(d["device_id"] == did for d in store.devices(self.cfg)):
                    return self._json({"ok": False, "error": "设备不存在"}, 404)
                fields = {k: body[k] for k in ("enabled", "mode", "interval_minutes", "name", "plan_json") if k in body}
                if "enabled" in fields:
                    if type(fields["enabled"]) is not bool:
                        raise ValueError("enabled 须为布尔值")
                    fields["enabled"] = int(fields["enabled"])
                if "interval_minutes" in fields and (type(fields["interval_minutes"]) is not int or not 5 <= fields["interval_minutes"] <= 10080):
                    raise ValueError("定位间隔须为 5 至 10080 分钟")
                if "mode" in fields and fields["mode"] not in ("cloud", "collector"):
                    raise ValueError("设备模式无效")
                if "plan_json" in fields:
                    fields["plan_json"] = _validated_device_plan(fields["plan_json"])
                store.update_device(self.cfg, did, **fields)
                return self._json({"ok": True})
            if path == "/api/push_schedule":
                try:
                    mailer.push_schedule(self.cfg)
                    return self._json({"ok": True})
                except Exception as exc:  # noqa: BLE001
                    return self._json({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
            if path == "/api/settings":
                body = self._body()
                # Reject sources before mutating any other submitted settings.
                if "location_sources" in body:
                    sources = body["location_sources"]
                    if (not isinstance(sources, dict) or set(sources) != {"direct", "mail"}
                            or any(type(sources[key]) is not bool for key in ("direct", "mail"))):
                        return self._json({"ok": False, "error": "location_sources 须包含 direct、mail 布尔值"}, 400)
                    if not any(sources.values()):
                        return self._json({"ok": False, "error": "至少启用一种定位来源"}, 400)
                else:
                    sources = _location_sources(self.cfg)
                if "imap" in body:
                    for k, v in body["imap"].items():
                        if k == "password" and not v:
                            continue
                        if k in self.cfg["imap"]:
                            self.cfg["imap"][k] = v
                if "deepseek" in body:
                    for k, v in body["deepseek"].items():
                        if k == "api_key" and not v:
                            continue
                        if k in self.cfg["deepseek"]:
                            self.cfg["deepseek"][k] = v
                if "alert" in body:
                    self.cfg["alert"].update(body["alert"])
                if "auto_report" in body:
                    self.cfg["auto_report"].update(body["auto_report"])
                if "smtp" in body:
                    for k, v in body["smtp"].items():
                        if k == "password" and not v:
                            continue
                        self.cfg.setdefault("smtp", {})[k] = v
                if "phone_email" in body:
                    self.cfg["phone_email"] = (body.get("phone_email") or "").strip()
                self.cfg["location_sources"] = dict(sources)
                common.save_config(self.cfg)
                return self._json({"ok": True})
            if path == "/api/token/rotate":
                import uuid
                self.cfg["access_token"] = uuid.uuid4().hex[:16]
                common.save_config(self.cfg)
                return self._json({"ok": True, "token": self.cfg["access_token"]})
            # ---------- 手机端上传 ----------
            if path == "/api/phone/heartbeat":
                if not self._token_ok(qs):
                    return self._json({"ok": False, "error": "token 无效"}, 403)
                body = self._body()
                dev = (body.get("device_id") or "unknown").strip()
                store.touch_device(self.cfg, dev, name=body.get("name"), battery=body.get("battery"),
                                   app_version=body.get("app_version"))
                return self._json({"ok": True, "server_time": common.fmt_local(common.now_utc())})
            if path == "/api/phone/points":
                if not self._token_ok(qs):
                    return self._json({"ok": False, "error": "token 无效"}, 403)
                body = self._body()
                dev = (body.get("device_id") or "unknown").strip()
                pts = body.get("points") or []
                inserted = store.insert_points(self.cfg, pts, device_id=dev)
                last = pts[-1] if pts else {}
                store.touch_device(self.cfg, dev, name=body.get("name"),
                                   battery=last.get("battery"), lat=last.get("lat"),
                                   lon=last.get("lon"), source=last.get("source"),
                                   app_version=body.get("app_version"))
                return self._json({"ok": True, "inserted": inserted, "received": len(pts)})
            return self._json({"ok": False, "error": "not found"}, 404)
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            return self._json({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}, 500)


def _validated_device_plan(value):
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else None
        except ValueError:
            raise ValueError("独立计划须为有效 JSON") from None
    if value is None or value == {}:
        return ""  # store.update_device ignores None; empty text explicitly means inherit.
    if not isinstance(value, dict):
        raise ValueError("独立计划须为 JSON 对象")
    normalized = schedule.validate_schedule(value, minimum=5)
    return json.dumps(normalized, ensure_ascii=False)


def _cloud_tick(cfg):
    """One scheduled pass; separated from the background loop for isolated testing."""
    if not _location_sources(cfg)["direct"]:
        return
    token = store.get_meta(cfg, "xiaomi_token")
    uid = store.get_meta(cfg, "xiaomi_uid")
    if not token or not uid:
        return
    client = _xiaomi_client(cfg)
    expected_token = client.token
    now = common.now_utc()
    global_plan, holidays = store.get_schedule(cfg), store.get_holidays(cfg)
    for d in store.devices(cfg):
        if (d.get("mode") or "") != "cloud":
            continue
        try:
            key = "local_last_query:" + d["device_id"]
            # Query attempts, not point timestamps, control the interval after failures.
            last_query = store.get_meta(cfg, key) or d.get("last_seen")
            if not schedule.device_due(d, global_plan, holidays, last_query, now, minimum=5):
                continue
            store.set_meta(cfg, key, common.utc_iso(now))
            st = client.device_status({"fid": d.get("fid"), "userId": d.get("user_id")}, 0)
            point = xiaomi_cloud.extract(st) if st else None
            if st:
                store.update_device(cfg, d["device_id"], **xiaomi_cloud.status_of(st))
            if point:
                store.insert_points(cfg, [point], device_id=d["device_id"])
                store.touch_device(cfg, d["device_id"], name=d.get("name"),
                                   battery=point.get("battery"), lat=point["lat"],
                                   lon=point["lon"], source="xiaomi-cloud")
        except Exception:
            # One malformed legacy device plan must not stop other devices.
            continue

    _persist_xiaomi_session(cfg, client, expected_token)
    client.session.close()


def _cloud_loop(cfg, stop):
    while not stop.is_set():
        try:
            _cloud_tick(cfg)
        except Exception:
            pass
        stop.wait(120)


def _sync_loop(cfg, stop):
    while not stop.is_set():
        try:
            sync_all(cfg)
        except Exception:
            pass
        not_checked = True
        try:
            run_auto_reports(cfg)
        except Exception:
            pass
        stop.wait(600)


def serve(cfg, open_browser=True):
    cfg.setdefault("location_sources", {"direct": True, "mail": True})
    store.init(cfg)
    Handler.cfg = cfg
    host = cfg["server"].get("host") or "0.0.0.0"
    port = int(cfg["server"].get("port") or 8765)
    auth = local_auth.LocalAuth(cfg, common.save_config)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.local_auth = auth
    stop = threading.Event()
    threading.Thread(target=run_auto_reports, args=(cfg,), daemon=True).start()
    threading.Thread(target=_sync_loop, args=(cfg, stop), daemon=True).start()
    threading.Thread(target=_cloud_loop, args=(cfg, stop), daemon=True).start()
    url = "http://127.0.0.1:%d/" % port
    print("定位工作台 已启动")
    print("  本机访问：%s" % url)
    print("  手机访问：http://%s:%d/（需与电脑同一 Wi-Fi）" % (common.lan_ip(), port))
    print("  数据目录：%s" % cfg["data_dir"])
    print("  配置文　：%s" % common.CONFIG_PATH)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()


def main():
    parser = argparse.ArgumentParser(description="定位工作台")
    parser.add_argument("command", nargs="?", default="serve",
                        choices=["serve", "sync", "report", "auto", "import", "info"])
    parser.add_argument("args", nargs="*")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int)
    ns = parser.parse_args()

    cfg = common.load_config()
    if ns.port:
        cfg["server"]["port"] = ns.port
    store.init(cfg)

    if ns.command == "serve":
        serve(cfg, open_browser=(not ns.no_browser) and bool(cfg["server"].get("open_browser", True)))
    elif ns.command == "sync":
        print(json.dumps(sync_all(cfg), ensure_ascii=False, indent=2))
    elif ns.command == "report":
        month = ns.args[0] if ns.args else (common.now_local().replace(day=1) - __import__("datetime").timedelta(days=1)).strftime("%Y-%m")
        row = reports.generate(cfg, month)
        print("已生成：%s（来源：%s）" % (month, row["source"]))
        if row.get("error"):
            print("DeepSeek 失败：%s" % row["error"])
    elif ns.command == "auto":
        run_auto_reports(cfg)
        print(json.dumps(AUTO_STATUS, ensure_ascii=False, indent=2))
    elif ns.command == "import":
        for target in ns.args:
            try:
                n, total = ingest.import_file(cfg, target)
                print("%s -> %d/%d" % (target, n, total))
            except Exception as exc:  # noqa: BLE001
                print("%s -> 失败：%s" % (target, exc))
    elif ns.command == "info":
        print(json.dumps(build_state(cfg), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
