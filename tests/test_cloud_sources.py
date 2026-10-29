"""Offline regression checks for independent collection sources."""
import copy
import datetime as dt
import json
from pathlib import Path
import socket
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import schedule
from cloud import fetch, health_check, mail_import
from cloud.crypto_state import decrypt, encrypt

NOW = dt.datetime(2026, 10, 27, 4, tzinfo=dt.timezone.utc)
PASSWORD = "synthetic-source-password"
POINT = {"ts": "2026-10-26T04:00:00Z", "lat": 31.2, "lon": 121.5}
HISTORY = {"devices": [{"device_id": "phone", "last_point": POINT,
                        "last_query": POINT["ts"], "query_status": "本次查询失败"}],
           "history": {"phone": {"started_at": POINT["ts"], "points": [POINT]}}}


@pytest.mark.parametrize("sources", [None, [], False, {"direct": 0}, {"mail": "false"},
                                    {"direct": False, "mail": False}, {"other": True}])
def test_invalid_source_plans_are_rejected(sources):
    with pytest.raises(ValueError):
        schedule.validate_cloud_plans({"sources": sources})


@pytest.mark.parametrize("plan,expected", [({}, {"direct": True, "mail": True}),
    ({"sources": {}}, {"direct": True, "mail": True}),
    ({"sources": {"direct": False}}, {"direct": False, "mail": True}),
    ({"sources": {"mail": False}}, {"direct": True, "mail": False})])
def test_source_defaults_and_partial_switches(plan, expected):
    before = copy.deepcopy(plan)
    schedule.validate_cloud_plans(plan)
    assert schedule.cloud_sources(plan) == expected
    assert plan == before


def test_disabled_direct_never_calls_client_and_preserves_history():
    client = Mock()
    before = copy.deepcopy(HISTORY)
    result = fetch.collect(client, HISTORY, {"sources": {"direct": False}}, NOW)
    client.device_list.assert_not_called()
    client.device_status.assert_not_called()
    assert result["devices"][0]["last_point"] == POINT
    assert result["devices"][0]["last_query"] == POINT["ts"]
    assert result["devices"][0]["query_status"] == "直接查询已停用，显示历史位置"
    assert result["history"] == HISTORY["history"] and HISTORY == before
    assert result["sources"]["direct"] == {"enabled": False, "status": "disabled"}
    assert result["plan"]["sources"] == {"direct": False, "mail": True}
    health_check.validate_state(result)


def test_missing_device_does_not_keep_yesterdays_success():
    previous = copy.deepcopy(HISTORY)
    previous["devices"][0]["query_status"] = "查询成功"
    client = Mock()
    client.device_list.return_value = [{"fid": "new", "userId": "u", "model": "Phone"}]
    client.device_status.return_value = {"componentList": []}
    result = fetch.collect(client, previous, now=NOW)
    assert result["devices"][0]["last_point"] == POINT
    assert result["devices"][0]["query_status"] == "本次未列出设备（保留历史位置）"


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(fetch.os, "environ", {"LOCATOR_PASSWORD": PASSWORD})
    monkeypatch.setattr(fetch.common, "now_utc", lambda: NOW)
    monkeypatch.setattr(fetch, "load_active", lambda *a: (None, ""))
    factory = Mock(side_effect=AssertionError("No real Xiaomi client"))
    monkeypatch.setattr(fetch.xiaomi_cloud, "Client", factory)
    forbidden = Mock(side_effect=AssertionError("No real network"))
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(mail_import.imaplib, "IMAP4_SSL", forbidden)
    target = tmp_path / "site" / "state.enc.json"
    return target, factory


def set_sources(direct, mail):
    fetch.os.environ["XM_PLANS"] = json.dumps({"sources": {"direct": direct, "mail": mail}})


def read(target):
    return decrypt(json.loads(target.read_text(encoding="utf-8")), PASSWORD)


def save_history(target):
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(encrypt(HISTORY, PASSWORD)), encoding="utf-8")


def fake_mail(monkeypatch, *, error=None):
    def receive(state, config):
        result = copy.deepcopy(state)
        status = "error" if error else "ok"
        result["mail"] = {"status": status, "error": error}
        result["sources"]["mail"] = {"enabled": True, "configured": True, "status": status}
        if not error:
            result["devices"].append({"device_id": "mail:phone", "source": "mail",
                                      "last_point": POINT, "query_status": "邮件定位导入完成"})
        return result
    spy = Mock(side_effect=receive)
    monkeypatch.setattr(fetch, "import_mail", spy)
    return spy


def test_mail_only_needs_no_xiaomi_secrets_and_exports_sources(app, monkeypatch):
    target, factory = app
    set_sources(False, True)
    save_history(target)
    fake_mail(monkeypatch)
    fetch.main()
    factory.assert_not_called()
    result = read(target)
    assert result["plan"]["sources"] == {"direct": False, "mail": True}
    assert result["devices"][0]["query_status"] == "直接查询已停用，显示历史位置"
    assert result["history"]["phone"]["points"][0]["ts"] == POINT["ts"]
    health_check.validate_state(result)


@pytest.mark.parametrize("with_history", [False, True])
def test_mail_only_unconfigured_is_visible_and_not_healthy(app, with_history):
    target, factory = app
    set_sources(False, True)
    if with_history:
        save_history(target)
        fetch.main()
        result = read(target)
        assert result["sources"]["mail"]["status"] == "not_configured"
        with pytest.raises(health_check.HealthCheckError, match="STATE_HAS_ERROR"):
            health_check.validate_state(result)
    else:
        with pytest.raises(fetch.CollectorError, match="mail_not_configured.*no_usable_location"):
            fetch.main()
        assert not target.exists()
    factory.assert_not_called()


def enable_direct(factory, *, fail=False):
    fetch.os.environ.update(XM_TOKEN="synthetic-token", XM_UID="synthetic-uid")
    client = Mock()
    client.device_list.return_value = []
    if fail:
        client.device_list.side_effect = RuntimeError("private upstream text")
    factory.side_effect = None
    factory.return_value = client
    return client


@pytest.mark.parametrize("fail", [False, True])
def test_first_xiaomi_list_failure_still_imports_mail_and_stays_unhealthy(app, monkeypatch, fail):
    target, factory = app
    set_sources(True, True)
    enable_direct(factory, fail=fail)
    spy = fake_mail(monkeypatch)
    fetch.main()
    spy.assert_called_once()
    result = read(target)
    assert result["devices"][0]["source"] == "mail"
    assert result["sources"]["direct"]["status"] == "error"
    assert "private upstream text" not in json.dumps(result)
    with pytest.raises(health_check.HealthCheckError, match="STATE_HAS_ERROR"):
        health_check.validate_state(result)


def test_both_failures_reach_final_safe_error_without_publishing(app, monkeypatch):
    target, factory = app
    set_sources(True, True)
    enable_direct(factory, fail=True)
    spy = fake_mail(monkeypatch, error="authentication_failed")
    with pytest.raises(fetch.CollectorError) as info:
        fetch.main()
    spy.assert_called_once()
    assert "device_list_unavailable:unexpected_error" in str(info.value)
    assert "mail_import_failed:authentication_failed" in str(info.value)
    assert not target.exists()


def test_mail_error_with_history_is_persisted_and_health_rejects_it(app, monkeypatch):
    target, _ = app
    set_sources(False, True)
    save_history(target)
    fake_mail(monkeypatch, error="authentication_failed")
    fetch.main()
    result = read(target)
    assert "authentication_failed" in result["error"]
    with pytest.raises(health_check.HealthCheckError, match="STATE_HAS_ERROR"):
        health_check.validate_state(result)


@pytest.mark.parametrize("raw,category", [("{bad", "invalid_json"), ("[]", "invalid_schema"),
    ('{"imap": null}', "invalid_schema"), ('{"host": 123}', "invalid_schema"),
    ('{"enabled": "false"}', "invalid_schema"), ('{"port": true}', "invalid_schema")])
def test_bad_mail_config_fails_before_network_and_preserves_ciphertext(app, raw, category):
    target, factory = app
    set_sources(True, True)
    enable_direct(factory)
    save_history(target)
    before = target.read_bytes()
    fetch.os.environ["XM_MAIL_CONFIG"] = raw
    with pytest.raises(fetch.CollectorError, match="^invalid_mail_config:" + category + "$"):
        fetch.main()
    factory.assert_not_called()
    assert target.read_bytes() == before


def test_default_sources_do_not_turn_unconfigured_mail_into_direct_failure(app):
    target, factory = app
    client = enable_direct(factory)
    client.device_list.return_value = [{"fid": "a", "userId": "u", "model": "Phone"}]
    client.device_status.return_value = {"componentList": [{"locationList": [
        {"clientUpdateTime": int(NOW.timestamp() * 1000),
         "gpsInfoTransformed": [{"latitude": 31.2, "longitude": 121.5}]}]}]}
    fetch.main()
    result = read(target)
    assert result["sources"]["mail"] == {"enabled": True, "configured": False, "status": "not_configured"}
    assert result["error"] is None
    health_check.validate_state(result)


def test_disabled_mail_ignores_bad_config_and_never_connects(app):
    target, factory = app
    set_sources(True, False)
    client = enable_direct(factory)
    save_history(target)
    client.device_list.return_value = [{"fid": "a", "userId": "u", "model": "Phone"}]
    client.device_status.return_value = {"componentList": []}
    fetch.os.environ["XM_MAIL_CONFIG"] = "{bad"
    fetch.main()
    assert read(target)["sources"]["mail"]["status"] == "disabled"


def test_direct_enabled_still_requires_both_credentials(app):
    set_sources(True, False)
    with pytest.raises(fetch.CollectorError, match="missing_required_secrets:XM_TOKEN,XM_UID"):
        fetch.main()


@pytest.mark.parametrize("timestamp", [POINT["ts"], "2026-10-25T04:00:00Z"])
def test_direct_same_or_older_point_is_not_reported_as_new_success(monkeypatch, timestamp):
    previous = copy.deepcopy(HISTORY)
    previous["devices"][0]["device_id"] = "xiaomi-a"
    client = Mock()
    client.device_list.return_value = [{"fid": "a", "userId": "u", "model": "Phone"}]
    client.device_status.return_value = {"componentList": []}
    monkeypatch.setattr(fetch.xiaomi_cloud, "extract", lambda status: dict(POINT, ts=timestamp))
    result = fetch.collect(client, previous, now=NOW)
    row = result["devices"][0]
    assert row["last_point"] == POINT
    assert row["query_status"] == "暂无新位置，保留上次位置"
    assert result["history"] == HISTORY["history"]


def test_both_sources_unavailable_preserves_history_and_reports_error(app):
    target, factory = app
    set_sources(True, True)
    save_history(target)
    # Existing history may be republished with an explicit error, never a success.
    fetch.main()
    result = read(target)
    assert result["devices"][0]["last_point"] == POINT
    assert result["sources"]["direct"]["error"] == "missing_required_secrets"
    assert result["sources"]["mail"]["status"] == "not_configured"
    assert result["error"]
    factory.assert_not_called()


def test_missing_credentials_and_no_mail_cannot_publish_empty_first_state(app):
    target, factory = app
    set_sources(True, True)
    with pytest.raises(fetch.CollectorError, match="no_usable_location"):
        fetch.main()
    assert not target.exists()
    factory.assert_not_called()
