"""Post-deployment health check for the encrypted locator state."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import sys
from typing import TextIO

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_PATH = PROJECT_ROOT / "site" / "state.enc.json"
sys.path.insert(0, str(PROJECT_ROOT))

from cloud.crypto_state import decrypt  # noqa: E402
import schedule  # noqa: E402

SUCCESS_MESSAGE = "健康检查成功"
ERROR_PREFIX = "HEALTH_CHECK_FAILED:"

# Actions runs every ten minutes, with up to twelve minutes for collection and
# deployment. Allow scheduling/publication slack, but reject a frozen snapshot.
MAX_STATE_AGE = dt.timedelta(minutes=30)
MAX_FUTURE_SKEW = dt.timedelta(minutes=5)
EARLIEST_TIMESTAMP = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


class HealthCheckError(Exception):
    """Failure with a stable, non-sensitive code suitable for CI logs."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def load_state(path: str | Path = DEFAULT_STATE_PATH, password: str | None = None) -> object:
    """Read and decrypt the published state without exposing sensitive errors."""
    password = password if password is not None else os.environ.get("LOCATOR_PASSWORD")
    if not password:
        raise HealthCheckError("PASSWORD_MISSING")

    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise HealthCheckError("STATE_READ_FAILED") from None

    try:
        envelope = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise HealthCheckError("STATE_ENVELOPE_INVALID") from None

    try:
        return decrypt(envelope, password)
    except Exception:
        # Do not pass through crypto/parser exception text: it may reveal data.
        raise HealthCheckError("STATE_DECRYPT_FAILED") from None


def _timestamp(value: object) -> dt.datetime | None:
    """Accept timezone-aware ISO timestamps only, without guessing local time."""
    if not isinstance(value, str):
        return None
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            return None
        stamp = stamp.astimezone(dt.timezone.utc)
    except (ValueError, OverflowError):
        return None
    return stamp if stamp >= EARLIEST_TIMESTAMP else None


def _usable_point(point: object, now: dt.datetime) -> bool:
    if not isinstance(point, dict):
        return False
    for key, limit in (("lat", 90), ("lon", 180)):
        value = point.get(key)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not -limit <= value <= limit):
            return False
    stamp = _timestamp(point.get("ts"))
    return stamp is not None and stamp <= now + MAX_FUTURE_SKEW


def _validate_device_freshness(state: dict, now: dt.datetime) -> None:
    """Reject overdue direct locations only when an explicit cloud plan applies.

    Preserve legacy snapshots without a plan and imported mail history. Give a
    newly opened collection window thirty minutes to obtain its first point.
    A current query/publication timestamp never substitutes for a point's time.
    """
    if "plan" not in state:
        return
    plans = state["plan"]
    try:
        schedule.validate_cloud_plans(plans)
        sources = schedule.cloud_sources(plans)
    except (ValueError, TypeError, OverflowError):
        raise HealthCheckError("STATE_PLAN_INVALID") from None
    if not sources["direct"]:
        return
    global_plan = plans.get("schedule") or {
        "enabled": True, "skip_holidays": False,
        "default": {"enabled": True, "start": "00:00", "end": "00:00",
                    "interval_minutes": 10},
    }
    holidays = plans.get("holidays") or {}
    for device in state["devices"]:
        did = device.get("device_id")
        if device.get("source") == "mail" or (isinstance(did, str) and did.startswith("mail:")):
            continue
        if not isinstance(did, str) or not did:
            raise HealthCheckError("DEVICE_PLAN_INVALID")
        policy = dict(plans.get("devices", {}).get(did, {}), device_id=did)
        # Plans use minute-resolution boundaries. Check the entire grace window,
        # including midnight/holiday transitions, rather than just its endpoints.
        if not all(schedule.device_due(policy, global_plan, holidays, None,
                                       now - dt.timedelta(minutes=minute), minimum=10)
                   for minute in range(31)):
            continue
        point = device.get("last_point")
        if not _usable_point(point, now):
            raise HealthCheckError("DEVICE_LOCATION_MISSING")
        stamp = _timestamp(point["ts"])
        # Use the current effective interval, including independent device plans.
        # Accept the exact interval+grace boundary; reject only once overdue.
        tolerated = stamp + MAX_STATE_AGE + dt.timedelta(microseconds=1)
        if schedule.device_due(policy, global_plan, holidays, tolerated.isoformat(),
                               now, minimum=10):
            raise HealthCheckError("DEVICE_LOCATION_STALE")


def validate_state(state: object, *, runtime: bool = False,
                   now: dt.datetime | None = None) -> None:
    """Check error flags; optionally require recent state and a usable location.

    The default preserves existing structure-only callers. Runtime validation
    bounds the collector's ``updated`` timestamp and active direct-device point
    ages when an explicit plan exists. Disabled/out-of-window devices, legacy
    snapshots and mail imports may retain history. Passing does not prove that
    every device is online or that imported mail is a live location.
    ``now`` may be injected as an aware datetime for offline tests.
    """
    if not isinstance(state, dict):
        raise HealthCheckError("STATE_NOT_OBJECT")

    if state.get("error"):
        raise HealthCheckError("STATE_HAS_ERROR")

    devices = state.get("devices")
    if not isinstance(devices, list) or not devices:
        raise HealthCheckError("DEVICES_MISSING")

    for device in devices:
        if not isinstance(device, dict):
            raise HealthCheckError("DEVICE_INVALID")
        query_status = device.get("query_status")
        if isinstance(query_status, str) and (
            "失败" in query_status or "fail" in query_status.casefold()
        ):
            raise HealthCheckError("DEVICE_QUERY_FAILED")

    if runtime:
        now = dt.datetime.now(dt.timezone.utc) if now is None else now
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        now = now.astimezone(dt.timezone.utc)
        updated = _timestamp(state.get("updated"))
        if updated is None:
            raise HealthCheckError("STATE_UPDATED_INVALID")
        if updated > now + MAX_FUTURE_SKEW:
            raise HealthCheckError("STATE_UPDATED_IN_FUTURE")
        if now - updated > MAX_STATE_AGE:
            raise HealthCheckError("STATE_STALE")
        if not any(_usable_point(device.get("last_point"), now) for device in devices):
            raise HealthCheckError("USABLE_LOCATION_MISSING")
        _validate_device_freshness(state, now)


def check_state(path: str | Path = DEFAULT_STATE_PATH, password: str | None = None, *,
                now: dt.datetime | None = None) -> None:
    """Require recent state and usable points consistent with active direct plans."""
    validate_state(load_state(path, password), runtime=True, now=now)


def main(path: str | Path = DEFAULT_STATE_PATH, password: str | None = None,
         stdout: TextIO | None = None) -> int:
    """CLI entry point; returns a process status and prints only fixed safe text."""
    output = stdout if stdout is not None else sys.stdout
    try:
        check_state(path, password)
    except HealthCheckError as exc:
        print(f"{ERROR_PREFIX}{exc.code}", file=output)
        return 1

    print(SUCCESS_MESSAGE, file=output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
