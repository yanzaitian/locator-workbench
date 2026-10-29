import copy
import datetime as dt
import io
import json
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from cloud import health_check  # noqa: E402
from cloud.crypto_state import encrypt  # noqa: E402

PASSWORD = "synthetic-health-check-password"


def write_encrypted_state(path: Path, state: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(encrypt(state, PASSWORD)), encoding="utf-8")


def test_load_state_reads_and_decrypts_synthetic_file(tmp_path):
    target = tmp_path / "site" / "state.enc.json"
    expected = {"error": None, "devices": [{"device_id": "synthetic-device"}]}
    write_encrypted_state(target, expected)

    assert health_check.load_state(target, PASSWORD) == expected


def test_validate_state_accepts_device_without_location():
    health_check.validate_state({
        "error": None,
        "devices": [{"device_id": "new-or-offline-device", "query_status": "暂无新位置"}],
    })


@pytest.mark.parametrize("state,code", [
    ([], "STATE_NOT_OBJECT"),
    ({"error": "synthetic collection failure", "devices": [{"device_id": "d"}]}, "STATE_HAS_ERROR"),
    ({"error": None, "devices": []}, "DEVICES_MISSING"),
    ({"error": None, "devices": "not-a-list"}, "DEVICES_MISSING"),
    ({"error": None, "devices": [None]}, "DEVICE_INVALID"),
    ({"error": None, "devices": [{"query_status": "本次查询失败（SyntheticError）"}]}, "DEVICE_QUERY_FAILED"),
    ({"error": None, "devices": [{"query_status": "request failed"}]}, "DEVICE_QUERY_FAILED"),
])
def test_validate_state_rejects_unhealthy_structure(state, code):
    with pytest.raises(health_check.HealthCheckError) as exc_info:
        health_check.validate_state(state)
    assert exc_info.value.code == code


def test_main_success_requires_recent_state_and_one_usable_location(tmp_path, capsys):
    target = tmp_path / "state.enc.json"
    write_encrypted_state(target, {
        "error": None,
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(),
        "devices": [
            {"device_id": "synthetic-device", "query_status": "暂无新位置"},
            {"last_point": {"lat": 0, "lon": 0, "ts": "2020-01-01T00:00:00Z"}},
        ],
    })

    assert health_check.main(target, PASSWORD) == 0
    assert capsys.readouterr().out == f"{health_check.SUCCESS_MESSAGE}\n"


@pytest.mark.parametrize("state,expected_code", [
    ({"error": "secret synthetic error text", "devices": [{"device_id": "d"}]}, "STATE_HAS_ERROR"),
    ({"error": None, "devices": [{"device_id": "d", "query_status": "失败: private detail"}]}, "DEVICE_QUERY_FAILED"),
])
def test_main_failure_prints_only_safe_code(tmp_path, capsys, state, expected_code):
    target = tmp_path / "state.enc.json"
    write_encrypted_state(target, state)

    assert health_check.main(target, PASSWORD) == 1
    output = capsys.readouterr().out
    assert output == f"{health_check.ERROR_PREFIX}{expected_code}\n"
    assert "secret synthetic error text" not in output
    assert "private detail" not in output


def test_main_maps_unreadable_state_to_safe_code_without_exception_text(tmp_path):
    output = io.StringIO()

    result = health_check.main(tmp_path / "missing-state.enc.json", PASSWORD, stdout=output)

    assert result == 1
    assert output.getvalue() == f"{health_check.ERROR_PREFIX}STATE_READ_FAILED\n"

@pytest.mark.parametrize("state", [None, "state", 42, {}, {"devices": {}}, {"devices": [False]}])
def test_other_invalid_state_shapes(state):
    with pytest.raises(health_check.HealthCheckError):
        health_check.validate_state(state)


@pytest.mark.parametrize("status", ["查询成功", "等待计划时段或间隔（显示上次位置）", "暂无新位置，保留上次位置"])
def test_normal_statuses_do_not_require_a_fresh_location(status):
    health_check.validate_state({"devices": [{"query_status": status, "last_point": None}]})


@pytest.mark.parametrize("state,code", [
    ({"error": "设备列表获取失败，显示历史数据", "devices": [{"last_point": {"lat": 12.34, "lon": 56.78}}]}, "STATE_HAS_ERROR"),
    ({"devices": [{"query_status": "查询成功"}, {"query_status": "本次查询失败，保留上次位置", "last_point": {"lat": 12.34, "lon": 56.78}}]}, "DEVICE_QUERY_FAILED"),
])
def test_historical_location_never_hides_failure(tmp_path, state, code):
    target = tmp_path / "state.enc.json"
    write_encrypted_state(target, state)
    output = io.StringIO()
    assert health_check.main(target, PASSWORD, stdout=output) == 1
    assert output.getvalue() == f"{health_check.ERROR_PREFIX}{code}\n"


def test_environment_password_is_used(tmp_path, monkeypatch):
    target = tmp_path / "state.enc.json"
    write_encrypted_state(target, {"devices": [{}]})
    monkeypatch.setenv("LOCATOR_PASSWORD", PASSWORD)
    assert health_check.load_state(target) == {"devices": [{}]}


def test_missing_password_does_not_read_file(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCATOR_PASSWORD", raising=False)
    output = io.StringIO()
    assert health_check.main(tmp_path / "absent", stdout=output) == 1
    assert output.getvalue() == "HEALTH_CHECK_FAILED:PASSWORD_MISSING\n"


@pytest.mark.parametrize("raw,code", [
    ("invalid synthetic json", "STATE_ENVELOPE_INVALID"),
    ("null", "STATE_DECRYPT_FAILED"),
    ("[]", "STATE_DECRYPT_FAILED"),
    ("{}", "STATE_DECRYPT_FAILED"),
])
def test_invalid_envelope_outputs_only_code(tmp_path, raw, code):
    target = tmp_path / "state.enc.json"
    target.write_text(raw, encoding="utf-8")
    output = io.StringIO()
    assert health_check.main(target, PASSWORD, stdout=output) == 1
    assert output.getvalue() == f"{health_check.ERROR_PREFIX}{code}\n"


def test_wrong_password_and_tampering_are_safe(tmp_path):
    target = tmp_path / "state.enc.json"
    write_encrypted_state(target, {"devices": [{}]})
    output = io.StringIO()
    assert health_check.main(target, "synthetic-wrong-password", stdout=output) == 1
    assert output.getvalue() == "HEALTH_CHECK_FAILED:STATE_DECRYPT_FAILED\n"
    envelope = json.loads(target.read_text(encoding="utf-8"))
    envelope["ciphertext"] = "AAAA"
    target.write_text(json.dumps(envelope), encoding="utf-8")
    output = io.StringIO()
    assert health_check.main(target, PASSWORD, stdout=output) == 1
    assert output.getvalue() == "HEALTH_CHECK_FAILED:STATE_DECRYPT_FAILED\n"


def test_script_entry_point_from_another_directory(tmp_path):
    import os
    import subprocess

    # Empty synthetic password stops before any attempt to read published data.
    env = dict(os.environ, LOCATOR_PASSWORD="", PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "cloud" / "health_check.py")],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert result.stdout == "HEALTH_CHECK_FAILED:PASSWORD_MISSING\n"
    assert result.stderr == ""

# Runtime checks use a fixed clock and synthetic locations only. Older points
# remain valid by design: publication freshness is not device freshness.
NOW = dt.datetime(2026, 10, 29, 12, tzinfo=dt.timezone.utc)


def runtime_state():
    return {
        "updated": NOW.isoformat(),
        "devices": [{"device_id": "synthetic-device", "last_point": {
            "lat": 12.34, "lon": 56.78, "ts": NOW.isoformat(),
        }}],
    }


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("Health-check tests must not access the network")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


@pytest.mark.parametrize("value", [
    None, "", "private invalid timestamp", True, 1793275200, [], {},
    "2026-10-29", "2026-10-29T12:00:00", "2026-02-30T12:00:00Z",
    "1969-12-31T23:59:59Z", "0001-01-01T00:00:00+14:00",
])
def test_runtime_rejects_invalid_updated(value):
    state = runtime_state()
    state["updated"] = value
    with pytest.raises(health_check.HealthCheckError) as exc:
        health_check.validate_state(state, runtime=True, now=NOW)
    assert exc.value.code == "STATE_UPDATED_INVALID"


@pytest.mark.parametrize("offset,code", [
    (-dt.timedelta(minutes=30), None),
    (-dt.timedelta(minutes=30, microseconds=1), "STATE_STALE"),
    (dt.timedelta(minutes=5), None),
    (dt.timedelta(minutes=5, microseconds=1), "STATE_UPDATED_IN_FUTURE"),
    (-dt.timedelta(days=1), "STATE_STALE"),
    (dt.timedelta(days=1), "STATE_UPDATED_IN_FUTURE"),
])
def test_runtime_updated_age_boundaries(offset, code):
    state = runtime_state()
    state["updated"] = (NOW + offset).isoformat()
    if code is None:
        health_check.validate_state(state, runtime=True, now=NOW)
    else:
        with pytest.raises(health_check.HealthCheckError) as exc:
            health_check.validate_state(state, runtime=True, now=NOW)
        assert exc.value.code == code


@pytest.mark.parametrize("point", [
    None, {}, [], "private malformed point",
    {"lat": 12, "lon": 56},
    {"lat": 12, "lon": 56, "ts": None},
    {"lat": 12, "lon": 56, "ts": "invalid private time"},
    {"lat": 12, "lon": 56, "ts": "2026-10-29T12:00:00"},
    {"lat": 12, "lon": 56, "ts": "1969-12-31T23:59:59Z"},
    {"lat": 12, "lon": 56, "ts": "2026-10-29T12:05:00.000001Z"},
    *[{"lat": lat, "lon": lon, "ts": NOW.isoformat()} for lat, lon in [
        (None, 0), (0, None), (True, 0), (0, False), ("12", 56),
        (12, "56"), (float("nan"), 0), (0, float("nan")),
        (float("inf"), 0), (0, -float("inf")), (91, 0), (0, 181),
        (-91, 0), (0, -181), ([], 0), (0, {}),
    ]],
])
def test_runtime_rejects_no_usable_location(point):
    state = runtime_state()
    state["devices"] = [{"last_point": point}, {"query_status": "查询成功"}]
    with pytest.raises(health_check.HealthCheckError) as exc:
        health_check.validate_state(state, runtime=True, now=NOW)
    assert exc.value.code == "USABLE_LOCATION_MISSING"


def test_runtime_rejects_history_without_a_current_device_point():
    state = runtime_state()
    state["history"] = {"synthetic-device": {"points": [state["devices"][0].pop("last_point")]}}
    with pytest.raises(health_check.HealthCheckError) as exc:
        health_check.validate_state(state, runtime=True, now=NOW)
    assert exc.value.code == "USABLE_LOCATION_MISSING"


@pytest.mark.parametrize("lat,lon", [(0, 0), (-90, -180), (90, 180)])
def test_runtime_accepts_coordinate_boundaries_and_future_tolerance(lat, lon):
    state = runtime_state()
    state["devices"][0]["last_point"] = {
        "lat": lat, "lon": lon, "ts": (NOW + dt.timedelta(minutes=5)).isoformat(),
    }
    health_check.validate_state(state, runtime=True, now=NOW)


def test_runtime_normalizes_timezones_without_mutating_state():
    state = runtime_state()
    state["updated"] = "2026-10-29T20:00:00+08:00"
    state["devices"][0]["last_point"]["ts"] = "2026-10-29T05:00:00-07:00"
    before = copy.deepcopy(state)
    health_check.validate_state(state, runtime=True,
                                now=NOW.astimezone(dt.timezone(dt.timedelta(hours=-7))))
    assert state == before


@pytest.mark.parametrize("row", [
    {"query_status": "等待计划时段或间隔（显示上次位置）"},
    {"settings": {"enabled": False}, "query_status": "直接查询已停用，显示历史位置"},
    {"device_id": "mail:synthetic", "source": "mail", "query_status": "未收到新邮件位置（保留历史位置）"},
    {"query_status": "暂无新位置，保留上次位置"},
])
def test_runtime_allows_old_points_for_standby_disabled_and_mail(row):
    state = runtime_state()
    state["devices"][0].update(row)
    state["devices"][0]["last_point"]["ts"] = "2020-01-01T00:00:00Z"
    state["devices"].insert(0, {"last_point": {"lat": "invalid"}})
    state["devices"].append({"last_point": None})
    health_check.validate_state(state, runtime=True, now=NOW)


@pytest.mark.parametrize("code", ["STATE_HAS_ERROR", "DEVICE_QUERY_FAILED"])
def test_runtime_preserves_failure_flags_even_with_good_location(code):
    state = runtime_state()
    if code == "STATE_HAS_ERROR":
        state["error"] = "private synthetic error"
    else:
        state["devices"].append({"query_status": "request FAILED: private detail"})
    # Error flags must also take priority over stale or malformed timestamps.
    state["updated"] = "invalid"
    with pytest.raises(health_check.HealthCheckError) as exc:
        health_check.validate_state(state, runtime=True, now=NOW)
    assert exc.value.code == code


@pytest.mark.parametrize("updated,point,code", [
    (None, None, "STATE_UPDATED_INVALID"),
    ((NOW - dt.timedelta(hours=1)).isoformat(), None, "STATE_STALE"),
    ((NOW + dt.timedelta(hours=1)).isoformat(), None, "STATE_UPDATED_IN_FUTURE"),
    (NOW.isoformat(), None, "USABLE_LOCATION_MISSING"),
])
def test_check_state_enforces_runtime_checks_on_encrypted_file(tmp_path, updated, point, code):
    state = {"updated": updated, "devices": [{"last_point": point}]}
    # Existing callers retain their structure-only behavior, but CLI cannot opt out.
    health_check.validate_state(state)
    target = tmp_path / "synthetic.enc.json"
    write_encrypted_state(target, state)
    with pytest.raises(health_check.HealthCheckError) as exc:
        health_check.check_state(target, PASSWORD, now=NOW)
    assert exc.value.code == code


def test_check_state_accepts_encrypted_recent_state_with_historical_point(tmp_path):
    state = runtime_state()
    state["devices"][0]["last_point"]["ts"] = "2020-01-01T00:00:00Z"
    target = tmp_path / "synthetic.enc.json"
    write_encrypted_state(target, state)
    health_check.check_state(target, PASSWORD, now=NOW)


@pytest.mark.parametrize("kind,code", [
    ("missing_updated", "STATE_UPDATED_INVALID"),
    ("stale", "STATE_STALE"),
    ("future", "STATE_UPDATED_IN_FUTURE"),
    ("missing_point", "USABLE_LOCATION_MISSING"),
])
def test_main_runtime_failure_prints_only_safe_code(tmp_path, kind, code):
    state = runtime_state()
    state["updated"] = dt.datetime.now(dt.timezone.utc).isoformat()
    if kind == "missing_updated":
        state.pop("updated")
    elif kind == "stale":
        state["updated"] = "2000-01-01T00:00:00Z"
    elif kind == "future":
        state["updated"] = "9998-01-01T00:00:00Z"
    else:
        state["devices"][0]["last_point"]["ts"] = "private invalid point time"
    target = tmp_path / "synthetic.enc.json"
    write_encrypted_state(target, state)
    output = io.StringIO()
    assert health_check.main(target, PASSWORD, stdout=output) == 1
    assert output.getvalue() == f"{health_check.ERROR_PREFIX}{code}\n"
