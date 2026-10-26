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


def test_main_success_prints_fixed_message_and_allows_missing_location(tmp_path, capsys):
    target = tmp_path / "state.enc.json"
    write_encrypted_state(target, {
        "error": None,
        "devices": [{"device_id": "synthetic-device", "query_status": "暂无新位置"}],
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
