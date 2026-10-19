"""Business-timezone calendar days; DST days are 23/25 hours, never fixed UTC offsets."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from igngbot_v4.historian.period import period


def day_period(label, zone, now=None):
    return period('daily', label, zone, now)


def previous_days(label, zone, days):
    """UTC bounds of the `days` calendar days before `label`, oldest first."""
    day = date.fromisoformat(label)
    result = []
    for offset in range(days, 0, -1):
        prior = (day - timedelta(days=offset)).isoformat()
        result.append((prior, *period('daily', prior, zone, datetime.max.replace(tzinfo=timezone.utc))))
    return result


def completed_days(zone, now=None, buffer_seconds=1800, catch_up=3):
    """Most recent completed local days, newest first, after an ingest buffer."""
    local = ((now or datetime.now(timezone.utc)) - timedelta(seconds=buffer_seconds)).astimezone(ZoneInfo(zone))
    today = local.date()
    return [(today - timedelta(days=offset)).isoformat() for offset in range(1, catch_up + 1)]
