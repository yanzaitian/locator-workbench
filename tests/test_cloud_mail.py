"""Mailbox-free contract tests for the cloud read-only importer."""
import copy
import datetime as dt
import email.policy
from email.message import EmailMessage
import imaplib
import json
from pathlib import Path
import sys
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cloud import mail_import
from cloud.crypto_state import decrypt, encrypt

NOW = dt.datetime(2026, 10, 26, 12, tzinfo=dt.timezone.utc)
SECRET = "synthetic-password-never-persist"
ACCOUNT = "private-account@example.invalid"
CONF = {"enabled": True, "host": "imap.example.invalid", "user": ACCOUNT,
        "password": SECRET, "sender_contains": "tracker@example.invalid",
        "subject_contains": "GPS", "device_id": "phone"}
POINT = {"ts": "2026-10-26T10:00:00Z", "lat": 31.2, "lon": 121.4}


@pytest.fixture(autouse=True)
def no_real_mailbox(monkeypatch):
    monkeypatch.setattr(mail_import.imaplib, "IMAP4_SSL",
                        Mock(side_effect=AssertionError("Real IMAP is forbidden")))


def message(points=None, *, sender="Tracker <tracker@example.invalid>", subject="GPS定位上报",
            text=None, filename="location.json", body="RAW-MAIL-BODY-DO-NOT-PERSIST"):
    msg = EmailMessage(policy=email.policy.SMTP)
    msg["From"] = sender
    msg["To"] = ACCOUNT
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_attachment((json.dumps([POINT] if points is None else points) if text is None else text).encode(),
                       maintype="application", subtype="octet-stream", filename=filename)
    return msg.as_bytes()


def mailbox(messages=None, *, validity=b"55", search_uids=None, sizes=None):
    messages = {1: message()} if messages is None else messages
    conn = Mock(spec=["login", "select", "response", "uid", "logout"])
    conn.login.return_value = ("OK", [b"logged in"])
    conn.select.return_value = ("OK", [str(len(messages)).encode()])
    conn.response.return_value = ("UIDVALIDITY", [validity])
    conn.logout.return_value = ("BYE", [b"bye"])

    def uid(command, *args):
        if command == "search":
            values = sorted(messages) if search_uids is None else search_uids
            return "OK", [b" ".join(str(x).encode() for x in values)]
        assert command == "fetch", "Only search and fetch are allowed"
        number = int(args[0])
        raw = messages[number]
        query = args[1]
        assert "BODY.PEEK[" in query
        assert "BODY[" not in query and "RFC822)" not in query
        if "HEADER.FIELDS" in query:
            data = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
            size = (sizes or {}).get(number, len(raw))
            meta = b"1 (UID %d RFC822.SIZE %d BODY[HEADER.FIELDS (FROM SUBJECT)] {%d}" % (number, size, len(data))
        else:
            data = raw
            meta = b"1 (UID %d BODY[] {%d}" % (number, len(data))
        return "OK", [(meta, data), b")"]

    conn.uid.side_effect = uid
    return conn


def run(conn=None, state=None, config=None, now=NOW):
    conn = mailbox() if conn is None else conn
    factory = Mock(return_value=conn)
    result = mail_import.import_mail({"devices": []} if state is None else state,
                                    CONF if config is None else config,
                                    now=now, connection_factory=factory)
    return result, conn, factory


def body_calls(conn):
    return [c for c in conn.uid.call_args_list if c.args[0] == "fetch" and "HEADER.FIELDS" not in c.args[-1]]


def test_readonly_in_memory_safe_state_and_encryption():
    initial = {"updated": "2026-10-26T11:00:00Z", "devices": [
        {"device_id": "phone", "name": "小米手机", "last_point": dict(POINT), "history": [dict(POINT)]}],
        "history": {"phone": {"started_at": POINT["ts"], "points": [dict(POINT)]}}, "plan": {"schedule": {"enabled": True}}}
    original = copy.deepcopy(initial)
    with patch("builtins.open", side_effect=AssertionError("No filesystem IO")):
        result, conn, factory = run(state=initial)
    assert initial == original
    assert result["devices"][0] == original["devices"][0]
    assert result["history"]["phone"] == initial["history"]["phone"]
    assert result["plan"] == initial["plan"]
    assert result["updated"] == initial["updated"]
    row = result["devices"][1]
    assert row["device_id"] == "mail:phone" and row["source"] == "mail"
    assert row["last_point"]["ts"] == POINT["ts"]
    assert row["last_point"]["gcj_lon"] != POINT["lon"]
    assert row["listed_at"] == row["last_query"] == "2026-10-26T12:00:00Z"
    assert result["mail"]["status"] == "ok" and result["mail"]["last_uid"] == 1
    factory.assert_called_once_with(CONF["host"], 993, timeout=30)
    conn.select.assert_called_once_with("INBOX", readonly=True)
    conn.login.assert_called_once_with(ACCOUNT, SECRET)
    conn.logout.assert_called_once_with()
    assert set(c[0] for c in conn.method_calls) == {"login", "select", "response", "uid", "logout"}
    plain = json.dumps(result)
    for forbidden in (SECRET, ACCOUNT, CONF["host"], "tracker@example.invalid", "GPS定位上报", "RAW-MAIL-BODY", "location.json"):
        assert forbidden not in plain
    encrypted = encrypt(result, "synthetic-state-password")
    assert decrypt(encrypted, "synthetic-state-password") == result
    assert "mail:phone" not in json.dumps(encrypted)


@pytest.mark.parametrize("config", [None, {}, {"enabled": False}, {"imap": {"enabled": False}}])
def test_unconfigured_disabled_is_visible_and_does_not_connect(config):
    factory = Mock(side_effect=AssertionError("Must not connect"))
    result = mail_import.import_mail({}, config, NOW, factory)
    assert result["mail"]["status"] == "disabled"
    assert result["mail"]["message"] == "邮件定位导入未启用"
    factory.assert_not_called()


def test_missing_login_is_friendly_and_does_not_connect():
    result, _, factory = run(config={"enabled": True, "subject_contains": "GPS"})
    assert result["mail"]["status"] == "disabled"
    assert "尚未配置" in result["mail"]["message"]
    assert result["mail"]["error"] is None
    factory.assert_not_called()


@pytest.mark.parametrize("filters", [{}, {"sender_contains": " ", "subject_contains": "\t"},
                                    {"sender_contains": "", "subject_contains": ""}])
def test_no_explicit_filter_refused_before_login(filters):
    config = {k: v for k, v in CONF.items() if not k.endswith("_contains")}
    result, _, factory = run(config=dict(config, **filters))
    assert result["mail"]["error"] == "filter_required"
    factory.assert_not_called()


@pytest.mark.parametrize("key,value", [("max_messages", 0), ("max_messages", 101),
    ("max_messages", True), ("max_message_bytes", 10485761), ("max_attachment_bytes", -1),
    ("max_points", "100"), ("port", 65536), ("device_id", "../phone"),
    ("folder", "INBOX\r\nSTORE"), ("sender_contains", 42), ("enabled", "yes")])
def test_invalid_config_is_safe_and_never_connects(key, value):
    result, _, factory = run(config=dict(CONF, **{key: value}))
    assert result["mail"]["error"] == "invalid_config"
    factory.assert_not_called()


@pytest.mark.parametrize("sender,subject,expected", [
    ("else@example.invalid", "GPS", 0), ("tracker@example.invalid", "Other", 0),
    ("TRACKER@EXAMPLE.INVALID", "gps定位上报", 1)])
def test_filters_are_decoded_case_insensitive_and_combined_with_and(sender, subject, expected):
    result, conn, _ = run(mailbox({1: message(sender=sender, subject=subject)}))
    assert result["mail"]["matched"] == expected
    assert len(body_calls(conn)) == expected
    assert result["mail"]["last_uid"] == 1


@pytest.mark.parametrize("field", ["sender_contains", "subject_contains"])
def test_either_single_filter_is_sufficient(field):
    config = dict(CONF)
    config.pop("subject_contains" if field == "sender_contains" else "sender_contains")
    assert run(config={"imap": config})[0]["mail"]["points"] == 1


def test_message_limit_counts_nonmatching_mail_and_cursor_resumes():
    messages = {1: message(subject="Other"), 2: message(), 3: message()}
    config = dict(CONF, max_messages=2)
    first, conn, _ = run(mailbox(messages), config=config)
    assert first["mail"]["messages"] == 2 and first["mail"]["last_uid"] == 2
    assert len(body_calls(conn)) == 1
    second, conn, _ = run(mailbox(messages), first, config)
    assert second["mail"]["messages"] == 1 and second["mail"]["last_uid"] == 3
    assert conn.uid.call_args_list[0].args == ("search", None, "UID 3:*")
    third, _, _ = run(mailbox(messages), second, config)
    assert third["mail"]["messages"] == 0 and len(third["devices"]) == 1


def test_uidvalidity_reset_reimports_lower_uid():
    previous, _, _ = run(mailbox({100: message()}))
    result, conn, _ = run(mailbox({1: message()}, validity=b"56"), previous)
    assert result["mail"]["last_uid"] == 1 and result["mail"]["uidvalidity"] == "56"
    assert conn.uid.call_args_list[0].args[-1] == "UID 1:*"


@pytest.mark.parametrize("changed", [{"folder": "Reports"}, {"user": "second@example.invalid"},
                                    {"subject_contains": "GPS定位"}, {"device_id": "second"}])
def test_mailbox_filter_or_device_scope_change_resets_cursor(changed):
    first, _, _ = run(mailbox({100: message()}))
    second, _, _ = run(mailbox({1: message()}), first, dict(CONF, **changed))
    assert second["mail"]["scope"] != first["mail"]["scope"]
    assert second["mail"]["last_uid"] == 1
    assert "second@example.invalid" not in json.dumps(second)


@pytest.mark.parametrize("validity", [None, b"", b"0", b"unsafe-account", b"4294967296"])
def test_missing_or_invalid_uidvalidity_fails_closed(validity):
    result, conn, _ = run(mailbox(validity=validity))
    assert result["mail"]["error"] == "uidvalidity_unavailable"
    conn.uid.assert_not_called()
    conn.logout.assert_called_once()


def test_fetch_failure_commits_only_completed_prefix_and_retries():
    messages = {1: message(), 2: message(), 3: message()}
    conn = mailbox(messages)
    original = conn.uid.side_effect
    def failing(command, *args):
        if command == "fetch" and args[0] == "2":
            return "NO", [SECRET.encode()]
        return original(command, *args)
    conn.uid.side_effect = failing
    first, _, _ = run(conn)
    assert first["mail"]["status"] == "error" and first["mail"]["error"] == "fetch_failed"
    assert first["mail"]["last_uid"] == 1 and len(first["devices"]) == 1
    assert not any(c.args[0] == "fetch" and c.args[1] == "3" for c in conn.uid.call_args_list)
    second, _, _ = run(mailbox(messages), first)
    assert second["mail"]["last_uid"] == 3 and second["mail"]["messages"] == 2
    assert SECRET not in json.dumps(first)


@pytest.mark.parametrize("stage,error,category", [("login", imaplib.IMAP4.error(SECRET + ACCOUNT), "imap_error"),
    ("select", OSError(SECRET + ACCOUNT), "connection_failed"),
    ("uid", RuntimeError(SECRET + ACCOUNT), "unexpected_error")])
def test_exception_text_and_dynamic_class_names_never_persist(stage, error, category):
    conn = mailbox()
    getattr(conn, stage).side_effect = error
    result, _, _ = run(conn)
    assert result["mail"]["error"] == category
    assert SECRET not in json.dumps(result) and ACCOUNT not in json.dumps(result)
    conn.logout.assert_called_once()
    unsafe_type = type(SECRET, (Exception,), {})
    conn = mailbox()
    conn.login.side_effect = unsafe_type(ACCOUNT)
    assert run(conn)[0]["mail"]["error"] == "unexpected_error"


def test_factory_error_is_sanitized_and_config_not_mutated():
    config = copy.deepcopy(CONF)
    factory = Mock(side_effect=TimeoutError(SECRET))
    result = mail_import.import_mail({}, config, NOW, factory)
    assert result["mail"]["error"] == "connection_failed"
    assert SECRET not in json.dumps(result) and config == CONF


@pytest.mark.parametrize("stage,category", [("login", "authentication_failed"), ("select", "mailbox_unavailable"),
                                           ("uid", "search_failed")])
def test_protocol_no_responses_use_fixed_categories(stage, category):
    conn = mailbox()
    getattr(conn, stage).side_effect = None
    getattr(conn, stage).return_value = ("NO", [ACCOUNT.encode()])
    result, _, _ = run(conn)
    assert result["mail"]["error"] == category
    assert ACCOUNT not in json.dumps(result)


def test_oversized_message_is_skipped_without_body_fetch():
    conn = mailbox(sizes={1: 5000})
    result, conn, _ = run(conn, config=dict(CONF, max_message_bytes=1000))
    assert result["mail"]["error"] == "message_too_large"
    assert result["mail"]["status"] == "partial" and result["mail"]["last_uid"] == 1
    assert not body_calls(conn)


def test_body_size_lie_is_bounded_and_skipped():
    result, conn, _ = run(mailbox(sizes={1: 20}), config=dict(CONF, max_message_bytes=100))
    assert result["mail"]["error"] == "message_too_large"
    assert body_calls(conn)[0].args[-1] == "(UID BODY.PEEK[]<0.101>)"
    assert result["devices"] == []


def test_truncated_body_is_retried_not_committed():
    result, _, _ = run(mailbox(sizes={1: 5000}))
    assert result["mail"]["error"] == "protocol_error" and result["mail"]["last_uid"] == 0


def test_oversized_attachment_and_point_limit_do_not_import():
    result, _, _ = run(config=dict(CONF, max_attachment_bytes=10))
    assert result["mail"]["error"] == "attachment_too_large" and result["devices"] == []
    result, _, _ = run(mailbox({1: message([POINT, POINT])}), config=dict(CONF, max_points=1))
    assert result["mail"]["error"] == "point_limit" and result["devices"] == []


@pytest.mark.parametrize("point", [dict(POINT, lat=91), dict(POINT, lon=-181), dict(POINT, lat=float("nan")),
    dict(POINT, lon=float("inf")), dict(POINT, ts="bad"), dict(POINT, ts="1969-01-01T00:00:00Z"),
    dict(POINT, ts="2026-10-26T12:06:00Z")])
def test_invalid_position_or_time_is_never_saved(point):
    result, _, _ = run(mailbox({1: message([point])}))
    assert result["devices"] == [] and result["mail"]["last_uid"] == 1
    assert result["mail"]["error"] in {"invalid_location", "invalid_attachment"}
    json.dumps(result, allow_nan=False)


def test_newest_valid_point_wins_and_metadata_is_whitelisted():
    bad = dict(POINT, lon=999)
    old = dict(POINT, ts="2026-10-25T10:00:00Z")
    latest = dict(POINT, source=SECRET, address=ACCOUNT, battery=999, acc=-1, speed=float("inf"))
    first, _, _ = run(mailbox({1: message([latest, bad, old])}))
    point = first["devices"][0]["last_point"]
    assert point["ts"] == POINT["ts"] and point["source"] == "mail"
    assert not {"battery", "acc", "speed", "address"} & point.keys()
    assert first["mail"]["points"] == 2 and first["mail"]["rejected_points"] == 1
    first["devices"][0]["history"] = [copy.deepcopy(point)]
    second, _, _ = run(mailbox({1: message(), 2: message([old])}), first)
    assert second["devices"][0]["last_point"] == point
    assert second["devices"][0]["history"] == [point]
    assert SECRET not in json.dumps(second) and ACCOUNT not in json.dumps(second)


@pytest.mark.parametrize("filename,text", [
    ("p.gpx", '<gpx><trk><trkseg><trkpt lat="31.2" lon="121.4"><time>2026-10-26T10:00:00Z</time></trkpt></trkseg></trk></gpx>'),
    ("p.kml", '<kml><Placemark><TimeStamp><when>2026-10-26T10:00:00Z</when></TimeStamp><Point><coordinates>121.4,31.2,2</coordinates></Point></Placemark></kml>'),
    ("p.jsonl", json.dumps(POINT) + "\n" + json.dumps(dict(POINT, lat=32))),
    ("p.txt", json.dumps(POINT)),
])
def test_existing_pure_parsers_work_in_memory(filename, text):
    result, _, _ = run(mailbox({1: message(text=text, filename=filename)}))
    assert result["mail"]["status"] == "ok" and result["mail"]["points"] >= 1


@pytest.mark.parametrize("text", ['<!DOCTYPE gpx [<!ENTITY x "boom">]><gpx>&x;</gpx>',
                                 '<gpx>broken', '\ufffdnot a location', '{"lat":31}'])
def test_malformed_and_entity_attachments_are_safely_consumed(text):
    result, _, _ = run(mailbox({1: message(text=text, filename="p.xml")}))
    assert result["mail"]["error"] == "invalid_attachment"
    assert result["mail"]["last_uid"] == 1 and result["devices"] == []


def test_binary_non_utf8_is_safely_skipped():
    raw = EmailMessage(policy=email.policy.SMTP)
    raw["From"] = "tracker@example.invalid"
    raw["Subject"] = "GPS"
    raw.set_content("body")
    raw.add_attachment(b"\xff\xfe", maintype="application", subtype="octet-stream", filename="p.json")
    result, _, _ = run(mailbox({1: raw.as_bytes()}))
    assert result["mail"]["error"] == "invalid_attachment" and result["mail"]["last_uid"] == 1


def test_non_location_attachments_ignored():
    result, _, _ = run(mailbox({1: message(filename="p.exe")}))
    assert result["mail"]["status"] == "ok" and result["mail"]["points"] == 0


def test_device_conflict_never_overwrites_existing_device():
    state = {"devices": [{"device_id": "mail:phone", "source": "xiaomi", "last_point": dict(POINT)}]}
    before = copy.deepcopy(state)
    result, _, factory = run(state=state)
    assert result["mail"]["error"] == "device_conflict"
    assert result["devices"] == before["devices"] and state == before
    factory.assert_not_called()


def test_untrusted_old_mail_fields_are_not_copied_and_logout_errors_are_silent():
    state = {"devices": [], "mail": {"error": SECRET, "account": ACCOUNT, "raw": SECRET,
                                    "last_uid": SECRET, "scope": ACCOUNT, "uidvalidity": SECRET}}
    conn = mailbox()
    conn.logout.side_effect = RuntimeError(SECRET)
    result, _, _ = run(conn, state)
    assert result["mail"]["status"] == "ok" and result["mail"]["last_uid"] == 1
    assert SECRET not in json.dumps(result) and ACCOUNT not in json.dumps(result)


def test_wrong_uid_response_cannot_advance_cursor():
    conn = mailbox()
    original = conn.uid.side_effect
    def mismatched(command, *args):
        typ, data = original(command, *args)
        if command == "fetch":
            data[0] = (data[0][0].replace(b"UID 1 ", b"UID 2 "), data[0][1])
        return typ, data
    conn.uid.side_effect = mismatched
    result, _, _ = run(conn)
    assert result["mail"]["error"] == "protocol_error" and result["mail"]["last_uid"] == 0


def test_empty_mailbox_preserves_devices_and_encrypted_cursor():
    first, _, _ = run()
    empty, conn, _ = run(mailbox({}), first)
    assert empty["mail"]["status"] == "ok" and empty["mail"]["messages"] == 0
    assert empty["mail"]["last_uid"] == 1
    expected = copy.deepcopy(first["devices"])
    expected[0]["query_status"] = "未收到新邮件位置（保留历史位置）"
    assert empty["devices"] == expected
    assert not body_calls(conn)


def test_uid_search_is_sorted_deduplicated_and_filters_old_uid_star_behavior():
    first, _, _ = run()
    messages = {1: message(), 2: message(), 3: message()}
    result, conn, _ = run(mailbox(messages, search_uids=[3, 1, 2, 3]), first)
    assert result["mail"]["messages"] == 2 and result["mail"]["last_uid"] == 3
    assert [c.args[1] for c in body_calls(conn)] == ["2", "3"]


@pytest.mark.parametrize("token", [0, 4294967296, "unsafe@example.invalid"])
def test_invalid_search_uid_is_safe_and_cannot_advance_cursor(token):
    result, _, _ = run(mailbox(search_uids=[token]))
    assert result["mail"]["error"] == "protocol_error" and result["mail"]["last_uid"] == 0
    assert "unsafe@example.invalid" not in json.dumps(result)


def test_oversized_headers_skip_message_without_fetching_body():
    result, conn, _ = run(mailbox({1: message(subject="GPS" + "x" * 20000)}))
    assert result["mail"]["error"] == "message_too_large"
    assert result["mail"]["last_uid"] == 1 and not body_calls(conn)


def test_password_rotation_does_not_reset_mailbox_cursor():
    first, _, _ = run(mailbox({100: message()}))
    result, conn, _ = run(mailbox({1: message()}), first, dict(CONF, password="rotated-synthetic-secret"))
    assert result["mail"]["scope"] == first["mail"]["scope"]
    assert result["mail"]["last_uid"] == 100 and result["mail"]["messages"] == 0
    conn.login.assert_called_once_with(ACCOUNT, "rotated-synthetic-secret")


def test_invalid_attachment_does_not_discard_other_valid_attachments():
    raw = EmailMessage(policy=email.policy.SMTP)
    raw["From"] = "tracker@example.invalid"
    raw["Subject"] = "GPS"
    raw.set_content("body")
    raw.add_attachment(b"{bad", maintype="application", subtype="octet-stream", filename="bad.json")
    raw.add_attachment(json.dumps(POINT).encode(), maintype="application", subtype="octet-stream", filename="good.json")
    result, _, _ = run(mailbox({1: raw.as_bytes()}))
    assert result["mail"]["status"] == "partial" and result["mail"]["points"] == 1
    assert result["devices"][0]["last_point"]["ts"] == POINT["ts"]
    assert result["mail"]["last_uid"] == 1


def test_round_point_limit_applies_across_messages():
    result, _, _ = run(mailbox({1: message(), 2: message()}), config=dict(CONF, max_points=1))
    assert result["mail"]["points"] == 1 and result["mail"]["error"] == "point_limit"
    assert result["mail"]["last_uid"] == 2


def test_factory_default_is_mocked_and_no_external_service_is_needed(monkeypatch):
    conn = mailbox()
    mocked_default = Mock(return_value=conn)
    monkeypatch.setattr(mail_import.imaplib, "IMAP4_SSL", mocked_default)
    result = mail_import.import_mail(None, CONF, NOW)
    assert result["mail"]["status"] == "ok"
    mocked_default.assert_called_once_with(CONF["host"], 993, timeout=30)


def test_all_valid_points_from_multiple_attachments_merge_with_existing_history():
    did = "mail:phone"
    older = dict(POINT, ts="2026-08-01T00:00:00Z", source="mail")
    known = dict(POINT, acc=7, source="mail")
    unrelated = {"started_at": "2026-09-01T00:00:00Z", "points": [dict(POINT)]}
    state = {"devices": [], "history": {
        did: {"started_at": "2026-08-02T00:00:00Z", "points": [older, known], "note": "keep"},
        "xiaomi-device": unrelated}}
    original = copy.deepcopy(state)
    earlier = dict(POINT, ts="2026-10-25T09:00:00Z")
    later = dict(POINT, ts="2026-10-26T11:00:00Z")
    raw = EmailMessage(policy=email.policy.SMTP)
    raw["From"] = "tracker@example.invalid"
    raw["Subject"] = "GPS"
    raw.set_content("body")
    for i, points in enumerate(([later, known, earlier], [earlier, later, dict(POINT, lat=999)])):
        raw.add_attachment(json.dumps(points).encode(), maintype="application",
                           subtype="octet-stream", filename=f"part-{i}.json")
    result, _, _ = run(mailbox({1: raw.as_bytes()}), state)
    entry = result["history"][did]
    assert [p["ts"] for p in entry["points"]] == [older["ts"], earlier["ts"], known["ts"], later["ts"]]
    assert entry["points"][2] == known  # First stored metadata wins.
    assert entry["started_at"] == state["history"][did]["started_at"] and entry["note"] == "keep"
    assert result["history"]["xiaomi-device"] == unrelated
    assert result["devices"][0]["last_point"]["ts"] == later["ts"]
    assert result["mail"]["rejected_points"] == 1
    assert state == original
    entry["points"][0]["lat"] = 0
    assert state == original


def test_duplicate_mail_new_uid_and_uidvalidity_replay_do_not_duplicate_history():
    samples = [dict(POINT, ts="2026-10-25T10:00:00Z"), POINT, POINT]
    raw = message(samples)
    first, _, _ = run(mailbox({1: raw}))
    second, _, _ = run(mailbox({1: raw, 2: raw}), first, now=NOW + dt.timedelta(hours=1))
    replay, _, _ = run(mailbox({1: raw}, validity=b"56"), second, now=NOW + dt.timedelta(hours=2))
    assert first["history"] == second["history"] == replay["history"]
    entry = replay["history"]["mail:phone"]
    assert len(entry["points"]) == 2 and entry["started_at"] == "2026-10-26T12:00:00Z"
    assert second["mail"]["last_uid"] == 2 and replay["mail"]["last_uid"] == 1


def test_history_identity_uses_timestamp_and_both_coordinates():
    samples = [POINT, dict(POINT, lat=32), dict(POINT, lon=122),
               dict(POINT, ts="2026-10-26T10:00:01Z"),
               dict(POINT, ts="2026-10-26T18:00:00+08:00", acc=100)]
    result, _, _ = run(mailbox({1: message(samples)}))
    entry = result["history"]["mail:phone"]
    identities = {(p["ts"], p["lat"], p["lon"]) for p in entry["points"]}
    assert len(entry["points"]) == len(identities) == 4
    assert next(p for p in entry["points"] if (p["ts"], p["lat"], p["lon"]) ==
                (POINT["ts"], POINT["lat"], POINT["lon"])).get("acc") is None


def test_subsecond_real_samples_are_retained_and_latest_point_uses_time_order():
    samples = [dict(POINT, ts="2026-10-26T10:00:00.100Z"), POINT,
               dict(POINT, ts="2026-10-26T10:00:00.200Z")]
    first, _, _ = run(mailbox({1: message(samples)}))
    assert len(first["history"]["mail:phone"]["points"]) == 3
    assert first["devices"][0]["last_point"]["ts"] == "2026-10-26T10:00:00.200000Z"
    second, _, _ = run(mailbox({1: message(samples), 2: message([POINT])}), first)
    assert second["history"] == first["history"]
    assert second["devices"][0]["last_point"] == first["devices"][0]["last_point"]


def test_history_merge_deduplicates_existing_equivalent_offset_without_replacing_metadata():
    existing = dict(POINT, ts="2026-10-26T18:00:00+08:00", source="mail", acc=3)
    state = {"devices": [], "history": {"mail:phone": {
        "started_at": "2026-09-01T00:00:00Z", "points": [existing, dict(existing, acc=9)]}}}
    result, _, _ = run(state=state)
    entry = result["history"]["mail:phone"]
    assert entry["points"] == [existing]
    assert entry["started_at"] == "2026-09-01T00:00:00Z"


def test_enrich_receives_all_imported_points_and_owns_month_retention():
    from cloud.history_reports import enrich_state
    samples = [dict(POINT, ts="2026-08-15T10:00:00Z"),
               dict(POINT, ts="2026-09-15T10:00:00Z"),
               dict(POINT, ts="2026-10-10T10:00:00Z"), POINT]
    imported, _, _ = run(mailbox({1: message(samples)}))
    before = copy.deepcopy(imported)
    assert len(imported["history"]["mail:phone"]["points"]) == 4
    enriched = enrich_state(imported, now=NOW)
    assert [p["ts"] for p in enriched["history"]["mail:phone"]["points"]] == [p["ts"] for p in samples[1:]]
    assert imported == before
    encrypted = encrypt(imported, "synthetic-state-password")
    assert decrypt(encrypted, "synthetic-state-password")["history"] == imported["history"]


@pytest.mark.parametrize("history", [[], {"mail:phone": None}, {"mail:phone": {
    "started_at": "2026-10-26T00:00:00Z", "points": [None]}}])
def test_invalid_target_history_is_preserved_with_safe_error_without_advancing(history):
    state = {"devices": [], "history": history}
    before = copy.deepcopy(state)
    result, _, _ = run(state=state)
    assert result["history"] == before["history"] and state == before
    assert result["mail"]["error"] == "invalid_history" and result["mail"]["last_uid"] == 0
    assert result["devices"] == []


@pytest.mark.parametrize("enabled", [True, False])
def test_plan_mail_switch_overrides_legacy_flag_and_reports_safe_status(enabled):
    state = {"devices": [], "plan": {"sources": {"mail": enabled}}}
    config = dict(CONF, enabled=not enabled)
    result, conn, factory = run(state=state, config=config)
    assert result["sources"]["mail"] == {
        "enabled": enabled, "configured": True, "status": "ok" if enabled else "disabled"}
    if enabled:
        assert result["devices"][0]["source"] == "mail"
    else:
        factory.assert_not_called()
    assert SECRET not in json.dumps(result) and ACCOUNT not in json.dumps(result)


def test_enabled_unconfigured_mail_has_explicit_source_status():
    result, _, factory = run(state={"devices": [], "plan": {"sources": {"mail": True}}}, config={})
    assert result["sources"]["mail"] == {
        "enabled": True, "configured": False, "status": "not_configured"}
    factory.assert_not_called()


def test_disabled_mail_resets_stale_success_without_touching_history():
    initial, _, _ = run()
    original = copy.deepcopy(initial)
    initial["plan"] = {"sources": {"mail": False}}
    result, _, factory = run(state=initial)
    assert result["devices"][0]["query_status"] == "邮件导入未启用（保留历史位置）"
    assert result["devices"][0]["last_point"] == original["devices"][0]["last_point"]
    assert result["history"] == original["history"]
    factory.assert_not_called()


@pytest.mark.parametrize("direct_enabled", [False, True])
def test_main_real_mail_import_after_disabled_or_failed_direct(monkeypatch, tmp_path, direct_enabled):
    from cloud import fetch, health_check
    password = "synthetic-mail-pipeline-password"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(fetch.os, "environ", {
        "LOCATOR_PASSWORD": password,
        "XM_PLANS": json.dumps({"sources": {"direct": direct_enabled, "mail": True}}),
        "XM_MAIL_CONFIG": json.dumps(dict(CONF, enabled=False)),
        "XM_TOKEN": "synthetic-token", "XM_UID": "synthetic-uid",
    })
    monkeypatch.setattr(fetch.common, "now_utc", lambda: NOW)
    monkeypatch.setattr(fetch, "load_active", lambda *a: (None, ""))
    client = Mock()
    client.device_list.return_value = []
    factory = Mock(return_value=client)
    monkeypatch.setattr(fetch.xiaomi_cloud, "Client", factory)
    conn = mailbox()
    monkeypatch.setattr(mail_import.imaplib, "IMAP4_SSL", Mock(return_value=conn))
    fetch.main()
    result = decrypt(json.loads((tmp_path / "site" / "state.enc.json").read_text()), password)
    assert result["devices"][0]["device_id"] == "mail:phone"
    assert result["devices"][0]["last_point"]["ts"] == POINT["ts"]
    assert result["history"]["mail:phone"]["points"][0]["ts"] == POINT["ts"]
    assert result["sources"]["mail"] == {"enabled": True, "configured": True, "status": "ok"}
    assert SECRET not in json.dumps(result) and ACCOUNT not in json.dumps(result)
    conn.select.assert_called_once_with("INBOX", readonly=True)
    if direct_enabled:
        with pytest.raises(health_check.HealthCheckError, match="STATE_HAS_ERROR"):
            health_check.validate_state(result)
    else:
        factory.assert_not_called()
        health_check.validate_state(result)


@pytest.mark.parametrize("present", [(), ("XM_TOKEN",), ("XM_UID",)])
def test_mail_still_imports_when_direct_credentials_are_missing(monkeypatch, tmp_path, present, capsys):
    from cloud import fetch, health_check
    password = "synthetic-mail-pipeline-password"
    monkeypatch.chdir(tmp_path)
    env = {
        "LOCATOR_PASSWORD": password,
        "XM_PLANS": json.dumps({"sources": {"direct": True, "mail": True}}),
        "XM_MAIL_CONFIG": json.dumps(CONF),
    }
    env.update({key: "synthetic-value" for key in present})
    monkeypatch.setattr(fetch.os, "environ", env)
    monkeypatch.setattr(fetch.common, "now_utc", lambda: NOW)
    monkeypatch.setattr(fetch, "load_active", lambda *a: (None, ""))
    factory = Mock(side_effect=AssertionError("Missing credentials must not reach Xiaomi"))
    monkeypatch.setattr(fetch.xiaomi_cloud, "Client", factory)
    conn = mailbox()
    monkeypatch.setattr(mail_import.imaplib, "IMAP4_SSL", Mock(return_value=conn))
    fetch.main()
    result = decrypt(json.loads((tmp_path / "site/state.enc.json").read_text()), password)
    factory.assert_not_called()
    assert result["devices"][0]["last_point"]["ts"] == POINT["ts"]
    assert result["sources"]["direct"] == {
        "enabled": True, "status": "error", "error": "missing_required_secrets"}
    assert result["sources"]["mail"]["status"] == "ok"
    output = capsys.readouterr().out
    assert "COLLECTOR_DIRECT=error:missing_required_secrets" in output
    assert "COLLECTOR_MAIL=ok" in output
    assert SECRET not in output and ACCOUNT not in output
    assert result["error"]
    with pytest.raises(health_check.HealthCheckError, match="STATE_HAS_ERROR"):
        health_check.validate_state(result)
    assert SECRET not in json.dumps(result) and ACCOUNT not in json.dumps(result)
