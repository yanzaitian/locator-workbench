"""Read-only IMAP location import into the *plaintext in-memory* cloud state.

Caller must persist the returned state through cloud.crypto_state.encrypt; this
module never reads/writes files, settings or environment variables.

import_mail(state, config, now=None, connection_factory=None) returns a deep copy.
config is an IMAP mapping (or {"imap": mapping}): enabled, host, user, password,
port=993, folder="INBOX", sender_contains/subject_contains (at least one nonblank;
when both are supplied both must match), device_id="imported" (a local label),
max_messages=20, max_message_bytes=2097152, max_attachment_bytes=524288,
max_points=5000. Limits have hard ceilings. Factory contract matches
imaplib.IMAP4_SSL(host, port, timeout=30). now is an aware datetime.

mail contains only status/message/error, checked_at, counters, scope (an opaque
SHA256 mailbox/filter/device identity), uidvalidity and last_uid. No credentials,
headers, filenames, bodies or exception text are copied into state. Device rows
use source="mail" and device_id="mail:<label>". All valid attachment samples
merge into history[device_id] = {started_at: UTC ISO, points: [...]}, deduplicated
by UTC time/latitude/longitude (first stored value wins). Existing history and
its start time are preserved; month retention/enrichment and scheduling/encryption
belong to the caller.
"""
import copy
import datetime as dt
import email
import email.policy
import hashlib
import imaplib
import json
import math
import re

import common
import ingest

_EXTENSIONS = {"gpx", "kml", "json", "jsonl", "txt", "xml"}
_MESSAGES = {
    "disabled": "邮件定位导入未启用",
    "not_configured": "邮件定位导入尚未配置邮箱登录信息",
    "ok": "邮件定位导入完成",
    "partial": "邮件定位导入完成，部分内容已安全跳过",
    "error": "邮件定位导入失败，保留已有位置和可重试进度",
}


class _Failure(Exception):
    def __init__(self, category):
        self.category = category


def _positive(value, default, ceiling):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
        raise _Failure("invalid_config")
    return value


def _ok(response, category):
    typ, data = response
    if typ not in ("OK", b"OK"):
        raise _Failure(category)
    return data


def _literal(parts, uid, maximum):
    literals = []
    for part in parts or []:
        if isinstance(part, tuple) and len(part) == 2:
            meta, raw = part
            if not isinstance(meta, bytes) or not isinstance(raw, bytes):
                raise _Failure("protocol_error")
            match = re.search(rb"\bUID\s+(\d+)\b", meta, re.I)
            if not match or int(match[1]) != uid:
                raise _Failure("protocol_error")
            literals.append(raw)
    if len(literals) != 1:
        raise _Failure("protocol_error")
    if len(literals[0]) > maximum:
        raise _Failure("message_too_large")
    return literals[0]


def _validity(conn):
    typ, values = conn.response("UIDVALIDITY")
    if typ not in ("UIDVALIDITY", b"UIDVALIDITY") or not values or len(values) != 1:
        raise _Failure("uidvalidity_unavailable")
    raw = values[0]
    if not isinstance(raw, bytes) or not re.fullmatch(rb"[1-9][0-9]{0,9}", raw):
        raise _Failure("uidvalidity_unavailable")
    value = int(raw)
    if value > 4294967295:
        raise _Failure("uidvalidity_unavailable")
    return str(value)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _point(raw, now):
    lat, lon = _number(raw.get("lat")), _number(raw.get("lon"))
    stamp = common.parse_time(raw.get("ts"))
    if (lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180
            or stamp is None or stamp < dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
            or stamp > now + dt.timedelta(minutes=5)):
        return None
    point = {"lat": lat, "lon": lon, "ts": stamp.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"), "source": "mail"}
    point["gcj_lat"], point["gcj_lon"] = common.wgs84_to_gcj02(lat, lon)
    for key in ("alt", "acc", "speed", "battery"):
        value = _number(raw.get(key))
        if value is not None and (key == "alt" or value >= 0) and (key != "battery" or value <= 100):
            point[key] = value
    return point



def _merge_history(state, did, points, stamp):
    """Retain every distinct real sample; leave calendar retention to enrich."""
    history = state.get("history", {})
    if not isinstance(history, dict):
        raise _Failure("invalid_history")
    entry = history.get(did, {"started_at": stamp, "points": []})
    if (not isinstance(entry, dict) or not isinstance(entry.get("points"), list)
            or common.parse_time(entry.get("started_at")) is None):
        raise _Failure("invalid_history")
    unique = {}
    for point in entry["points"] + points:
        if not isinstance(point, dict):
            raise _Failure("invalid_history")
        ts = common.parse_time(point.get("ts"))
        lat, lon = _number(point.get("lat")), _number(point.get("lon"))
        if ts is None or lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise _Failure("invalid_history")
        unique.setdefault((ts, lat, lon), point)
    # Build the entire merge before replacing the entry, preserving existing
    # metadata as well as older samples that enrich may later prune.
    state.setdefault("history", {})[did] = dict(entry, points=[unique[k] for k in sorted(unique)])


def _attachments(msg, conf, now, stats, issue):
    points = []
    for part in msg.walk():
        filename = part.get_filename()
        if not filename or filename.rsplit(".", 1)[-1].lower() not in _EXTENSIONS:
            continue
        if part.is_multipart():
            continue
        try:
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            if len(payload) > conf["max_attachment_bytes"]:
                issue("attachment_too_large")
                continue
            text = payload.decode("utf-8-sig")
            # ElementTree permits internal entities: reject declarations before
            # reusing the existing pure parsers to avoid entity amplification.
            if "<!doctype" in text.lower() or "<!entity" in text.lower():
                issue("invalid_attachment")
                continue
            parsed = ingest.parse_text(text)
            if len(parsed) + stats["points"] > conf["max_points"]:
                issue("point_limit")
                continue
            stats["attachments"] += 1
            valid = []
            for raw in parsed:
                point = _point(raw, now)
                if point is None:
                    stats["rejected_points"] += 1
                    issue("invalid_location")
                else:
                    valid.append(point)
            if not parsed:
                issue("invalid_attachment")
            stats["points"] += len(valid)
            points.extend(valid)
        except Exception:
            issue("invalid_attachment")
    return points


def import_mail(state, config, now=None, connection_factory=None):
    """Return an isolated state copy; all public diagnostics are fixed categories."""
    result = copy.deepcopy(state if state is not None else {"devices": []})
    previous = result.get("mail") or {}
    # Whitelist cursor fields rather than carrying old mail diagnostics/config.
    mail = {"status": "disabled", "message": _MESSAGES["disabled"], "error": None,
            "checked_at": None, "scope": None, "uidvalidity": None, "last_uid": 0,
            "messages": 0, "matched": 0, "attachments": 0, "points": 0,
            "skipped": 0, "rejected_points": 0}
    for key in ("scope", "uidvalidity"):
        value = previous.get(key)
        pattern = r"[0-9a-f]{64}" if key == "scope" else r"[1-9][0-9]{0,9}"
        if isinstance(value, str) and re.fullmatch(pattern, value):
            mail[key] = value
    old_uid = previous.get("last_uid", 0)
    if type(old_uid) is int and 0 <= old_uid <= 4294967295:
        mail["last_uid"] = old_uid
    result["mail"] = mail
    conn = None

    def status(value, error=None):
        mail.update(status=value, message=_MESSAGES[value], error=error)

    def issue(category):
        if mail["error"] is None:
            mail["error"] = category

    try:
        now = common.now_utc() if now is None else now
        if not isinstance(now, dt.datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise _Failure("invalid_config")
        now = now.astimezone(dt.timezone.utc)
        mail["checked_at"] = common.utc_iso(now)
        if config is None:
            return result
        if not isinstance(config, dict):
            raise _Failure("invalid_config")
        conf = config.get("imap", config)
        if not isinstance(conf, dict):
            raise _Failure("invalid_config")
        if conf.get("enabled", False) is False:
            return result
        if conf.get("enabled") is not True:
            raise _Failure("invalid_config")
        conf = dict(conf)
        for key in ("sender_contains", "subject_contains"):
            value = conf.get(key, "")
            if not isinstance(value, str) or len(value) > 512 or "\r" in value or "\n" in value:
                raise _Failure("invalid_config")
            conf[key] = value.strip().casefold()
        if not (conf["sender_contains"] or conf["subject_contains"]):
            raise _Failure("filter_required")
        if not all(conf.get(key) for key in ("host", "user", "password")):
            status("disabled")
            mail["message"] = _MESSAGES["not_configured"]
            return result
        for key in ("host", "user", "password", "folder"):
            value = conf.get(key, "INBOX")
            if not isinstance(value, str) or not value.strip() or "\r" in value or "\n" in value:
                raise _Failure("invalid_config")
        conf.setdefault("folder", "INBOX")
        label = conf.get("device_id", "imported")
        if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", label):
            raise _Failure("invalid_config")
        did = "mail:" + label
        for key, default, ceiling in (("port", 993, 65535), ("max_messages", 20, 100),
                ("max_message_bytes", 2097152, 10485760),
                ("max_attachment_bytes", 524288, 2097152), ("max_points", 5000, 20000)):
            conf[key] = _positive(conf.get(key), default, ceiling)
        rows = result.setdefault("devices", [])
        row = next((r for r in rows if r.get("device_id") == did), None)
        if row is not None and row.get("source") != "mail":
            raise _Failure("device_conflict")
        # Scope changes must not reuse a UID from a different mailbox or filter.
        identity = [conf[k] for k in ("host", "port", "user", "folder", "sender_contains", "subject_contains")]
        scope = hashlib.sha256(json.dumps(identity + [did], ensure_ascii=True).encode()).hexdigest()
        factory = connection_factory if connection_factory is not None else imaplib.IMAP4_SSL
        conn = factory(conf["host"], conf["port"], timeout=30)
        _ok(conn.login(conf["user"], conf["password"]), "authentication_failed")
        _ok(conn.select(conf["folder"], readonly=True), "mailbox_unavailable")
        validity = _validity(conn)
        if mail["scope"] != scope or mail["uidvalidity"] != validity:
            mail["last_uid"] = 0
        mail.update(scope=scope, uidvalidity=validity)
        if mail["last_uid"] == 4294967295:
            status("ok")
            return result
        data = _ok(conn.uid("search", None, "UID %d:*" % (mail["last_uid"] + 1)), "search_failed")
        if not data or not isinstance(data[0], bytes):
            raise _Failure("protocol_error")
        tokens = data[0].split()
        if any(not x.isdigit() or not 1 <= int(x) <= 4294967295 for x in tokens):
            raise _Failure("protocol_error")
        uids = sorted({int(x) for x in tokens if int(x) > mail["last_uid"]})
        for uid in uids[:conf["max_messages"]]:
            mail["messages"] += 1
            parts = _ok(conn.uid("fetch", str(uid),
                "(UID RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)]<0.16385>)"), "fetch_failed")
            # Overlong headers are consumed safely without downloading the body.
            try:
                headers = _literal(parts, uid, 16384)
            except _Failure as exc:
                if exc.category != "message_too_large":
                    raise
                issue("message_too_large")
                mail["skipped"] += 1
                mail["last_uid"] = uid
                continue
            metadata = b" ".join(p[0] for p in parts if isinstance(p, tuple))
            size_match = re.search(rb"\bRFC822.SIZE\s+(\d+)\b", metadata, re.I)
            if not size_match:
                raise _Failure("protocol_error")
            size = int(size_match[1])
            msg = email.message_from_bytes(headers, policy=email.policy.default)
            matched = all(not conf[key] or conf[key] in str(msg.get(header, "")).casefold()
                          for key, header in (("sender_contains", "From"), ("subject_contains", "Subject")))
            if not matched or size > conf["max_message_bytes"]:
                mail["skipped"] += 1
                if matched:
                    issue("message_too_large")
            else:
                mail["matched"] += 1
                limit = conf["max_message_bytes"]
                body = _ok(conn.uid("fetch", str(uid), "(UID BODY.PEEK[]<0.%d>)" % (limit + 1)), "fetch_failed")
                raw = _literal(body, uid, limit + 1)
                if len(raw) > limit:
                    issue("message_too_large")
                    mail["skipped"] += 1
                elif len(raw) != size:
                    raise _Failure("protocol_error")
                else:
                    msg = email.message_from_bytes(raw, policy=email.policy.default)
                    points = _attachments(msg, conf, now, mail, issue)
                    if points:
                        _merge_history(result, did, points, mail["checked_at"])
                        if row is None:
                            row = {"device_id": did, "name": "邮件定位", "source": "mail", "last_point": None}
                            rows.append(row)
                        latest = max(points, key=lambda p: common.parse_time(p["ts"]))
                        old = row.get("last_point")
                        if not old or common.parse_time(latest["ts"]) > common.parse_time(old["ts"]):
                            row["last_point"] = latest
                        row.update(last_query=mail["checked_at"], listed_at=mail["checked_at"], query_status="邮件定位导入完成")
            mail["last_uid"] = uid
        status("partial" if mail["error"] else "ok", mail["error"])
    except _Failure as exc:
        status("error", exc.category)
    except imaplib.IMAP4.error:
        status("error", "imap_error")
    except (OSError, TimeoutError):
        status("error", "connection_failed")
    except Exception:
        status("error", "unexpected_error")
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass
    return result
