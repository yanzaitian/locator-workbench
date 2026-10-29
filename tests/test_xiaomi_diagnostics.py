"""Synthetic device-list responses only: no credentials or live requests."""
import copy
import datetime as dt
import json
from pathlib import Path
import re
import runpy
import sys
import traceback
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import xiaomi_cloud
from cloud import fetch

SENSITIVE = "SYNTHETIC_BODY_cookie_token_uid_https://invalid.example/private"
NOW = dt.datetime(2026, 10, 26, 0, 0, tzinfo=dt.timezone.utc)
DEVICE = {"fid": "synthetic-device", "userId": "synthetic-owner"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Live requests are forbidden in diagnostic tests")
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)


@pytest.fixture
def response_client(monkeypatch):
    def make(payload=None, status=200, json_error=None, request_error=None):
        response = Mock()
        response.status_code = status
        response.json = Mock(return_value=payload, side_effect=json_error)
        # These deliberately contain synthetic sensitive text. Diagnostics must
        # never inspect or propagate any of these response attributes.
        response.text = response.url = response.headers = SENSITIVE
        client = xiaomi_cloud.Client("synthetic-token", "synthetic-owner")
        get = Mock(return_value=response, side_effect=request_error)
        monkeypatch.setattr(client.session, "get", get)
        return client, response, get
    return make


def assert_safe(error, expected):
    assert str(error) == expected
    assert xiaomi_cloud.device_list_diagnostic(error) == expected
    assert re.fullmatch(r"(?:http_non_200|non_json|api_rejected|invalid_structure|network_error|empty_list|unexpected_error)(?::http_status=[1-5][0-9]{2})?", str(error))
    assert SENSITIVE not in repr(error)
    formatted = "".join(traceback.format_exception(error))
    assert SENSITIVE not in formatted
    assert error.__suppress_context__


@pytest.mark.parametrize("status", [201, 302, 401, 403, 429, 500, 503])
def test_http_non_200_is_classified_without_parsing_body(response_client, status):
    client, response, _ = response_client(status=status, json_error=ValueError(SENSITIVE))
    with pytest.raises(xiaomi_cloud.DeviceListError) as info:
        client.device_list()
    assert_safe(info.value, f"http_non_200:http_status={status}")
    assert info.value.category == "http_non_200"
    assert info.value.http_status == status
    assert isinstance(info.value, xiaomi_cloud.XiaomiError)
    response.json.assert_not_called()


@pytest.mark.parametrize("status", [None, True, "403", 99, 600, SENSITIVE])
def test_invalid_status_never_enters_diagnostic(response_client, status):
    client, response, _ = response_client(status=status)
    with pytest.raises(xiaomi_cloud.DeviceListError) as info:
        client.device_list()
    assert_safe(info.value, "invalid_structure")
    response.json.assert_not_called()


@pytest.mark.parametrize("error", [ValueError(SENSITIVE), json.JSONDecodeError("invalid", SENSITIVE, 0)])
def test_non_json_never_forwards_decoder_text(response_client, error):
    client, _, _ = response_client(json_error=error)
    with pytest.raises(xiaomi_cloud.DeviceListError) as info:
        client.device_list()
    assert_safe(info.value, "non_json:http_status=200")


@pytest.mark.parametrize("result", ["error", "unauthorized", SENSITIVE])
def test_api_rejection_does_not_include_api_fields(response_client, result):
    client, _, _ = response_client({"result": result, "code": SENSITIVE,
                                    "description": SENSITIVE, "data": SENSITIVE})
    with pytest.raises(xiaomi_cloud.DeviceListError) as info:
        client.device_list()
    assert_safe(info.value, "api_rejected:http_status=200")


@pytest.mark.parametrize("payload", [
    None, [], SENSITIVE, 12, {}, {"result": None}, {"result": 0},
    {"result": "ok"}, {"result": "ok", "data": None},
    {"result": "ok", "data": SENSITIVE},
    {"result": "ok", "data": {}},
    {"result": "ok", "data": {"deviceList": SENSITIVE}},
    {"result": "ok", "data": [SENSITIVE]},
    {"result": "ok", "data": {"deviceList": [None]}},
])
def test_malformed_envelope_and_list_are_structural_failures(response_client, payload):
    client, _, _ = response_client(payload)
    with pytest.raises(xiaomi_cloud.DeviceListError) as info:
        client.device_list()
    assert_safe(info.value, "invalid_structure:http_status=200")


@pytest.mark.parametrize("error_type", [requests.Timeout, requests.ConnectionError, requests.TooManyRedirects])
def test_network_error_discards_request_exception_text(response_client, error_type):
    client, response, _ = response_client(request_error=error_type(SENSITIVE))
    with pytest.raises(xiaomi_cloud.DeviceListError) as info:
        client.device_list()
    assert_safe(info.value, "network_error")
    response.json.assert_not_called()


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("rows", [[], [DEVICE]])
def test_both_supported_success_shapes_are_unchanged(response_client, wrapped, rows):
    payload = {"result": "ok", "data": {"deviceList": rows} if wrapped else rows}
    client, response, get = response_client(payload)
    assert client.device_list() == rows
    get.assert_called_once()
    response.json.assert_called_once_with()


@pytest.mark.parametrize("category,status,expected", [
    (SENSITIVE, SENSITIVE, "unexpected_error"),
    ("http_non_200", "403", "http_non_200"),
    ("http_non_200", True, "http_non_200"),
    ("http_non_200", 600, "http_non_200"),
    ("http_non_200", 403, "http_non_200:http_status=403"),
])
def test_diagnostic_fields_are_revalidated_not_exception_text(category, status, expected):
    error = xiaomi_cloud.DeviceListError("non_json", 200)
    error.category = category
    error.http_status = status
    error.args = (SENSITIVE,)
    assert xiaomi_cloud.device_list_diagnostic(error) == expected


def test_unknown_exception_type_and_text_are_not_forwarded():
    unsafe_class = type(SENSITIVE, (RuntimeError,), {})
    assert xiaomi_cloud.device_list_diagnostic(unsafe_class(SENSITIVE)) == "unexpected_error"


@pytest.mark.parametrize("payload,status,json_error,expected", [
    (None, 403, None, "http_non_200:http_status=403"),
    (None, 200, ValueError(SENSITIVE), "non_json:http_status=200"),
    ({"result": SENSITIVE}, 200, None, "api_rejected:http_status=200"),
    ([], 200, None, "invalid_structure:http_status=200"),
    ({"result": "ok", "data": []}, 200, None, "empty_list"),
])
def test_collect_initial_failure_has_safe_category(response_client, payload, status, json_error, expected):
    client, _, _ = response_client(payload, status, json_error)
    result = fetch.collect(client, now=NOW)
    assert result["sources"]["direct"]["error"] == "device_list_unavailable:" + expected
    assert SENSITIVE not in json.dumps(result)


def test_collect_network_failure_and_unknown_exception_are_fixed(response_client, monkeypatch):
    client, _, _ = response_client(request_error=requests.Timeout(SENSITIVE))
    result = fetch.collect(client, now=NOW)
    assert result["sources"]["direct"]["error"] == "device_list_unavailable:network_error"
    monkeypatch.setattr(client, "device_list", Mock(side_effect=RuntimeError(SENSITIVE)))
    result = fetch.collect(client, now=NOW)
    assert result["sources"]["direct"]["error"] == "device_list_unavailable:unexpected_error"


def test_collect_history_retains_points_and_safe_diagnostic(response_client):
    previous = {"devices": [{"device_id": "synthetic-device", "last_point": {
        "ts": "2026-10-25T00:00:00Z", "lat": 1.0, "lon": 2.0}}]}
    original = copy.deepcopy(previous)
    client, _, _ = response_client(status=429)
    result = fetch.collect(client, previous=previous, now=NOW)
    assert previous == original
    assert result["devices"][0]["last_point"] == original["devices"][0]["last_point"]
    assert "http_non_200:http_status=429" in result["error"]
    assert "http_non_200:http_status=429" in result["devices"][0]["query_status"]
    assert SENSITIVE not in json.dumps(result)


def test_cli_emits_only_safe_failure_and_does_not_publish(response_client, monkeypatch, tmp_path, capsys):
    client, _, _ = response_client({"result": SENSITIVE, "description": SENSITIVE})
    monkeypatch.chdir(tmp_path)
    for name, value in {"LOCATOR_PASSWORD": "synthetic-password-123", "XM_TOKEN": "synthetic-token",
                        "XM_UID": "synthetic-owner"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("XM_PLANS", raising=False)
    monkeypatch.delenv("LOCATOR_PREVIOUS_PASSWORD", raising=False)
    monkeypatch.setattr(xiaomi_cloud, "Client", lambda *args: client)
    with pytest.raises(SystemExit) as info:
        runpy.run_path(str(Path(fetch.__file__).resolve()), run_name="__main__")
    assert info.value.code == 1
    captured = capsys.readouterr()
    assert "device_list_unavailable:api_rejected:http_status=200" in captured.err
    assert SENSITIVE not in captured.out + captured.err
    assert "synthetic-token" not in captured.out + captured.err
    assert "synthetic-owner" not in captured.out + captured.err
    assert not (tmp_path / "site").exists()


@pytest.mark.parametrize("code", [401, 403, 429, 500, 599])
def test_collector_source_log_reports_safe_http_code(capsys, code):
    fetch.emit_source_diagnostics({"sources": {
        "direct": {"status": "error", "error":
                   "device_list_unavailable:http_non_200:http_status=%d" % code},
        "mail": {"status": "not_configured"}},
        "error": SENSITIVE, "devices": [{"device_id": SENSITIVE}]})
    assert capsys.readouterr().out == (
        "COLLECTOR_DIRECT=error:device_list_unavailable:http_non_200:http_status=%d\n"
        "COLLECTOR_MAIL=not_configured\n" % code)


@pytest.mark.parametrize("error", [SENSITIVE,
    "device_list_unavailable:http_non_200:http_status=401\n" + SENSITIVE,
    "device_list_unavailable:http_non_200:http_status=401:" + SENSITIVE,
    "device_list_unavailable:http_non_200:http_status=999",
    {"secret": SENSITIVE}, None])
def test_collector_source_log_does_not_echo_arbitrary_errors(capsys, error):
    fetch.emit_source_diagnostics({"sources": {
        "direct": {"status": "error", "error": error},
        "mail": {"status": "error", "error": SENSITIVE}}})
    assert capsys.readouterr().out == "COLLECTOR_DIRECT=error\nCOLLECTOR_MAIL=error\n"


@pytest.mark.parametrize("sources", [None, SENSITIVE, {"direct": SENSITIVE},
    {"direct": {"status": SENSITIVE}, "mail": {"status": [SENSITIVE]}}])
def test_collector_source_log_unknown_fields_do_not_leak(capsys, sources):
    fetch.emit_source_diagnostics({"sources": sources})
    assert capsys.readouterr().out == "COLLECTOR_DIRECT=unknown\nCOLLECTOR_MAIL=unknown\n"
