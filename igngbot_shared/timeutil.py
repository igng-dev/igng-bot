"""Shared datetime helpers for message_logs UTC storage and Beijing display."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

UTC = timezone.utc
BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
MYSQL_UTC_INIT_COMMAND = "SET time_zone = '+00:00'"


def utc_now() -> datetime:
    """Return a naive UTC datetime suitable for MySQL DATETIME columns."""
    return datetime.now(UTC).replace(tzinfo=None)


def unix_to_utc_naive(timestamp: Any) -> datetime | None:
    """Convert a Unix timestamp (seconds) to naive UTC datetime."""
    if timestamp is None or timestamp == "":
        return None
    try:
        value = float(timestamp)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(value, tz=UTC).replace(tzinfo=None)


def ensure_utc_naive(value: Any) -> datetime | None:
    """Normalize datetime-like values to naive UTC for storage."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC).replace(tzinfo=None)
    if isinstance(value, (int, float)):
        return unix_to_utc_naive(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(UTC).replace(tzinfo=None)


def as_beijing(value: Any) -> datetime | None:
    """Interpret stored naive UTC datetimes and convert them to Beijing time."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(BEIJING_TZ)


def format_beijing(value: Any, fmt: str = "%Y-%m-%d %H:%M:%S", fallback: str = "") -> str:
    """Format a stored UTC datetime as Beijing wall-clock time for AI prompts."""
    beijing = as_beijing(value)
    if beijing is None:
        return fallback if fallback or value in (None, "") else str(value)
    return beijing.strftime(fmt)
