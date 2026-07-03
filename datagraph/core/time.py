from __future__ import annotations

from datetime import UTC, datetime


class TimestampValidationError(ValueError):
    pass


def parse_timestamp(value: object, *, field: str = "timestamp") -> tuple[str, int]:
    if not isinstance(value, str):
        raise TimestampValidationError(f"{field} must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TimestampValidationError(f"{field} must be a valid ISO 8601 string") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    utc = parsed.astimezone(UTC)
    timespec = "milliseconds" if utc.microsecond else "seconds"
    timestamp_utc = utc.isoformat(timespec=timespec).replace("+00:00", "Z")
    timestamp_ms = int(utc.timestamp() * 1000)
    return timestamp_utc, timestamp_ms

