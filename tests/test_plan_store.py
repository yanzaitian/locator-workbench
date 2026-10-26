import datetime as dt
import json
import uuid
import pytest
from cloud.crypto_state import encrypt
from cloud.plan_store import apply_payload, load_active, PlanCommandError

PASSWORD = 'fake-plan-command-password'
NOW = dt.datetime(2026, 10, 26, 7, tzinfo=dt.timezone.utc)
PLANS = {'devices': {'fake-device': {'enabled': True, 'interval_minutes': 10}}}


def payload(**overrides):
    command = {'kind': 'locator-plans-v1', 'request_id': str(uuid.uuid4()),
               'created_at': NOW.isoformat(), 'base_revision': '', 'plans': PLANS}
    command.update(overrides)
    return json.dumps(encrypt(command, PASSWORD)), command['request_id']


def test_roundtrip_ciphertext_and_duplicate_idempotence(tmp_path):
    path = tmp_path / 'plans.enc.json'
    command, revision = payload()
    assert apply_payload(command, PASSWORD, path=path, now=NOW) == revision
    assert load_active(PASSWORD, path=path) == (PLANS, revision)
    raw = path.read_text()
    assert 'fake-device' not in raw and 'devices' not in raw
    assert apply_payload(command, PASSWORD, path=path, now=NOW) == revision
    assert path.read_text() == raw


def test_stale_revision_cannot_overwrite_new_plan(tmp_path):
    path = tmp_path / 'plans.enc.json'
    first, revision = payload()
    apply_payload(first, PASSWORD, path=path, now=NOW)
    before = path.read_bytes()
    stale, _ = payload()
    with pytest.raises(PlanCommandError, match='revision_conflict'):
        apply_payload(stale, PASSWORD, path=path, now=NOW)
    assert path.read_bytes() == before
    next_command, new_revision = payload(base_revision=revision)
    apply_payload(next_command, PASSWORD, path=path, now=NOW)
    assert load_active(PASSWORD, path=path)[1] == new_revision


@pytest.mark.parametrize('change', [
    {'plans': {'devices': {'fake-device': {'interval_minutes': 1}}}},
    {'created_at': '2026-10-20T07:00:00+00:00'},
    {'created_at': '2026-10-26T09:00:00+00:00'},
    {'kind': 'wrong'}, {'base_revision': None}, {'request_id': 'not-a-uuid'},
])
def test_invalid_command_never_writes(tmp_path, change):
    path = tmp_path / 'plans.enc.json'
    command, _ = payload(**change)
    with pytest.raises(PlanCommandError):
        apply_payload(command, PASSWORD, path=path, now=NOW)
    assert not path.exists()


def test_wrong_password_and_tamper_never_write(tmp_path):
    path = tmp_path / 'plans.enc.json'
    command, _ = payload()
    with pytest.raises(PlanCommandError, match='encryption'):
        apply_payload(command, 'another-fake-password', path=path, now=NOW)
    assert not path.exists()


def test_password_rotation_preserves_active_plan(tmp_path):
    path = tmp_path / 'plans.enc.json'
    command, revision = payload()
    apply_payload(command, PASSWORD, path=path, now=NOW)
    assert load_active('new-fake-password', PASSWORD, path=path) == (PLANS, revision)
    with pytest.raises(PlanCommandError, match='unreadable'):
        load_active('new-fake-password', path=path)
