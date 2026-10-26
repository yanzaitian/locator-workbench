"""Authenticated, encrypted plan commands for the GitHub Actions runner."""
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common
import schedule
from cloud.crypto_state import encrypt, decrypt

PLAN_PATH = Path("cloud/plans.enc.json")
MAX_PAYLOAD = 100000


class PlanCommandError(ValueError):
    """Fixed messages only; never include supplied plans or ciphertext."""


def load_active(password, previous_password=None, path=None):
    path = Path(path) if path is not None else PLAN_PATH
    if not path.exists():
        return None, ""
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        obj = None
        for candidate in (password, previous_password):
            if not candidate:
                continue
            try:
                obj = decrypt(envelope, candidate)
                break
            except Exception:
                continue
        if not isinstance(obj, dict) or obj.get("kind") != "locator-cloud-plans-v1":
            raise ValueError()
        plans = schedule.validate_cloud_plans(obj["plans"])
        revision = obj["request_id"]
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", revision):
            raise ValueError()
        return plans, revision
    except Exception:
        raise PlanCommandError("saved_plans_unreadable") from None


def apply_payload(payload, password, previous_password=None, path=None, now=None):
    path = Path(path) if path is not None else PLAN_PATH
    now = now or common.now_utc()
    if not isinstance(payload, str) or len(payload.encode("utf-8")) > MAX_PAYLOAD:
        raise PlanCommandError("invalid_command_size")
    try:
        command = decrypt(json.loads(payload), password)
    except Exception:
        raise PlanCommandError("invalid_command_encryption") from None
    try:
        if not isinstance(command, dict) or command.get("kind") != "locator-plans-v1":
            raise ValueError()
        request_id = command["request_id"]
        if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", request_id):
            raise ValueError()
        created = common.parse_time(command["created_at"])
        if not created or created.tzinfo is None:
            raise ValueError()
        elapsed = (now - created).total_seconds()
        if not -300 <= elapsed <= 86400:
            raise ValueError()
        plans = schedule.validate_cloud_plans(command["plans"])
        if len(plans.get("devices", {})) > 100:
            raise ValueError()
        base_revision = command["base_revision"]
        if not isinstance(base_revision, str):
            raise ValueError()
    except Exception:
        raise PlanCommandError("invalid_command_or_plans") from None
    current, revision = load_active(password, previous_password, path)
    if revision == request_id:
        if current != plans:
            raise PlanCommandError("request_id_conflict")
        return request_id
    if base_revision != revision:
        raise PlanCommandError("plan_revision_conflict_refresh_required")
    obj = {"kind": "locator-cloud-plans-v1", "request_id": request_id,
           "updated": common.utc_iso(now), "plans": plans}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(encrypt(obj, password)), encoding="utf-8")
    temporary.replace(path)
    return request_id


def main():
    try:
        apply_payload(os.environ.get("LOCATOR_PLAN_PAYLOAD", ""),
                      os.environ["LOCATOR_PASSWORD"],
                      os.environ.get("LOCATOR_PREVIOUS_PASSWORD"))
    except PlanCommandError as exc:
        print("计划未保存：" + str(exc), file=sys.stderr)
        return 1
    except Exception:
        print("计划未保存：plan_command_failed", file=sys.stderr)
        return 1
    print("已验证并保存加密计划；实际生效状态以发布后的看板为准。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
