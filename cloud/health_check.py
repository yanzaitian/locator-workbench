"""Post-deployment health check for the encrypted locator state."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import TextIO

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_PATH = PROJECT_ROOT / "site" / "state.enc.json"
sys.path.insert(0, str(PROJECT_ROOT))

from cloud.crypto_state import decrypt  # noqa: E402

SUCCESS_MESSAGE = "健康检查成功"
ERROR_PREFIX = "HEALTH_CHECK_FAILED:"


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


def validate_state(state: object) -> None:
    """Validate publication health while allowing devices without a location."""
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


def check_state(path: str | Path = DEFAULT_STATE_PATH, password: str | None = None) -> None:
    """Load, decrypt, and validate the state; raise only safe error codes."""
    validate_state(load_state(path, password))


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
