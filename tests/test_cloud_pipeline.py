"""Offline integration checks for the Actions collector pipeline.

All files, plans, credentials and locations here are synthetic and temporary.
"""
import copy
import datetime as dt
import json
import socket
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cloud import fetch, plan_store
from cloud.crypto_state import decrypt, encrypt

PASSWORD = "synthetic-pipeline-password"
PREVIOUS_PASSWORD = "synthetic-previous-password"
REVISION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
NOW = dt.datetime(2026, 10, 26, 4, tzinfo=dt.timezone.utc)


def plans():
    return {
        "schedule": {"enabled": True, "skip_holidays": False,
                     "default": {"enabled": True, "start": "00:00",
                                 "end": "00:00", "interval_minutes": 10}},
        "holidays": {"statutory": [], "workdays": []},
        "devices": {
            "offline-device": {"enabled": False, "interval_minutes": 30,
                               "plan_json": {"enabled": True,
                                             "default": {"enabled": True,
                                                         "start": "22:00",
                                                         "end": "02:00",
                                                         "interval_minutes": 20}}},
            "not-listed-device": {"enabled": True, "interval_minutes": 40},
        },
    }


@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    # Replace, rather than inspect, the process environment for this pipeline.
    monkeypatch.setattr(fetch.os, "environ", {
        "LOCATOR_PASSWORD": PASSWORD, "XM_TOKEN": "synthetic-token",
        "XM_UID": "synthetic-uid", "DEEPSEEK_API_KEY": "synthetic-api-key",
        "XM_MAIL_CONFIG": json.dumps({"enabled": True, "host": "mail.invalid"}),
    })

    def forbidden(*args, **kwargs):
        raise AssertionError("Network and real local configuration are forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(fetch.common, "load_config", forbidden)
    monkeypatch.setattr(fetch.common, "now_utc", lambda: NOW)
    events = []
    client = Mock()
    client.device_list.return_value = [{"fid": "a", "userId": "u1", "model": "Phone"}]
    client.device_status.return_value = {
        "fid": "a", "componentList": [{"online": True,
        "locationList": [{"clientUpdateTime": 1792987200000,
                          "gpsInfoTransformed": [{"latitude": 31.2,
                                                   "longitude": 121.5,
                                                   "accuracy": 20}]}]}]}
    factory = Mock(return_value=client)
    monkeypatch.setattr(fetch.xiaomi_cloud, "Client", factory)
    original_collect = fetch.collect
    original_load = plan_store.load_active

    def load(*args):
        events.append("load")
        return original_load(*args)

    def collect(*args):
        events.append("collect")
        return original_collect(*args)

    def mail(state, config):
        events.append("mail")
        assert config == {"enabled": True, "host": "mail.invalid"}
        result = copy.deepcopy(state)
        result["mail"] = {"status": "mock-imported"}
        return result

    def history(state, *, deepseek_key, summarizer):
        events.append("history")
        assert state["mail"] == {"status": "mock-imported"}
        assert deepseek_key == "synthetic-api-key"
        assert summarizer is fetch.summarize_deepseek
        result = copy.deepcopy(state)
        result["history"] = {"mock-device": {"points": [{"ts": "mock-history-point"}]}}
        result["reports"] = {"2026-10": {"content": "mock-summary"}}
        return result

    snapshots = []

    def seal(state, password):
        events.append("encrypt")
        snapshots.append(copy.deepcopy(state))
        return encrypt(state, password)

    monkeypatch.setattr(fetch, "load_active", Mock(side_effect=load))
    monkeypatch.setattr(fetch, "collect", Mock(side_effect=collect))
    monkeypatch.setattr(fetch, "import_mail", Mock(side_effect=mail))
    monkeypatch.setattr(fetch, "enrich_state", Mock(side_effect=history))
    monkeypatch.setattr(fetch, "summarize_deepseek", Mock(side_effect=forbidden))
    monkeypatch.setattr(fetch, "encrypt", Mock(side_effect=seal))
    target = tmp_path / "site" / "state.enc.json"
    target.parent.mkdir()
    return {"root": tmp_path, "target": target, "events": events,
            "snapshots": snapshots, "factory": factory, "client": client}


def save_plan(pipeline, value, password=PASSWORD):
    path = pipeline["root"] / "cloud" / "plans.enc.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(encrypt({"kind": "locator-cloud-plans-v1",
                                       "request_id": REVISION, "plans": value}, password)),
                    encoding="utf-8")


def read_state(pipeline):
    return decrypt(json.loads(pipeline["target"].read_text(encoding="utf-8")), PASSWORD)


@pytest.mark.parametrize("legacy", ["{malformed legacy JSON", json.dumps({"devices": {"legacy": {"enabled": False}}})])
def test_main_prefers_encrypted_plan_and_preserves_full_plan_revision(pipeline, legacy):
    active = plans()
    save_plan(pipeline, active)
    fetch.os.environ["XM_PLANS"] = legacy
    fetch.main()
    state = read_state(pipeline)
    assert state["plan"] == active
    assert state["plan_request_id"] == REVISION
    assert state["plan"]["devices"]["not-listed-device"] == active["devices"]["not-listed-device"]
    assert "legacy" not in state["plan"]["devices"]
    fetch.load_active.assert_called_once_with(PASSWORD, None)
    assert fetch.collect.call_args.args[2] == active
    assert pipeline["events"] == ["load", "collect", "mail", "history", "encrypt"]
    assert pipeline["snapshots"] == [state]
    assert state["mail"]["status"] == "mock-imported"
    assert state["history"]["mock-device"]["points"]
    assert state["reports"]["2026-10"]["content"] == "mock-summary"
    fetch.summarize_deepseek.assert_not_called()
    raw = pipeline["target"].read_text(encoding="utf-8")
    assert "mock-summary" not in raw and "not-listed-device" not in raw
    assert not pipeline["target"].with_suffix(".tmp").exists()


def test_main_falls_back_to_environment_only_without_saved_plan(pipeline):
    active = plans()
    fetch.os.environ["XM_PLANS"] = json.dumps(active)
    fetch.main()
    state = read_state(pipeline)
    assert state["plan"] == active
    assert state["plan_request_id"] == ""
    assert pipeline["events"] == ["load", "collect", "mail", "history", "encrypt"]


def test_main_loads_plan_with_previous_password_and_reencrypts(pipeline):
    save_plan(pipeline, plans(), PREVIOUS_PASSWORD)
    fetch.os.environ["LOCATOR_PREVIOUS_PASSWORD"] = PREVIOUS_PASSWORD
    fetch.os.environ["XM_PLANS"] = "invalid fallback must not be parsed"
    fetch.main()
    assert read_state(pipeline)["plan_request_id"] == REVISION
    fetch.load_active.assert_called_once_with(PASSWORD, PREVIOUS_PASSWORD)
    with pytest.raises(Exception):
        decrypt(json.loads(pipeline["target"].read_text(encoding="utf-8")), PREVIOUS_PASSWORD)


@pytest.mark.parametrize("source", ["invalid-encrypted-plan", "unreadable-encrypted-plan",
                                    "invalid-environment-plan", "malformed-environment-json"])
def test_main_bad_plan_never_overwrites_previous_state(pipeline, source):
    original = encrypt({"devices": [{"device_id": "historical-device",
                                     "last_point": {"ts": "2026-10-25T00:00:00Z"}}]}, PASSWORD)
    before = json.dumps(original, indent=2).encode("utf-8")
    pipeline["target"].write_bytes(before)
    invalid = {"devices": {"bad": {"interval_minutes": 1}}}
    if source == "invalid-encrypted-plan":
        save_plan(pipeline, invalid)
        fetch.os.environ["XM_PLANS"] = json.dumps(plans())
    elif source == "unreadable-encrypted-plan":
        save_plan(pipeline, plans(), "synthetic-unrelated-password")
        fetch.os.environ["XM_PLANS"] = json.dumps(plans())
    else:
        fetch.os.environ["XM_PLANS"] = (json.dumps(invalid) if source == "invalid-environment-plan" else "{bad-json")
    with pytest.raises(fetch.CollectorError, match="^invalid_cloud_plans:"):
        fetch.main()
    assert pipeline["target"].read_bytes() == before
    assert not pipeline["target"].with_suffix(".tmp").exists()
    assert pipeline["events"] == ["load"]
    pipeline["factory"].assert_not_called()
    fetch.collect.assert_not_called()
    fetch.import_mail.assert_not_called()
    fetch.enrich_state.assert_not_called()
    fetch.encrypt.assert_not_called()


@pytest.mark.parametrize("stage", ["import_mail", "enrich_state"])
def test_main_processing_error_preserves_previous_ciphertext(pipeline, stage):
    before = json.dumps(encrypt({"devices": []}, PASSWORD)).encode("utf-8")
    pipeline["target"].write_bytes(before)
    getattr(fetch, stage).side_effect = RuntimeError("synthetic processing failure")
    with pytest.raises(RuntimeError, match="synthetic processing failure"):
        fetch.main()
    assert pipeline["target"].read_bytes() == before
    assert not pipeline["target"].with_suffix(".tmp").exists()
    fetch.encrypt.assert_not_called()
    if stage == "import_mail":
        fetch.enrich_state.assert_not_called()
