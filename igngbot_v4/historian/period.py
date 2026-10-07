from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def period(kind, label, zone, now=None):
    if kind not in {'daily', 'weekly'}:
        raise ValueError('invalid report kind')
    day = date.fromisoformat(label)
    if day.isoformat() != label:
        raise ValueError('invalid calendar label')
    tz = ZoneInfo(zone)
    if kind == 'weekly' and day.weekday() != 0:
        raise ValueError('weekly label must be Monday')
    end = day + timedelta(days=1 if kind == 'daily' else 7)
    start_at = datetime.combine(day, time(), tz).astimezone(timezone.utc)
    end_at = datetime.combine(end, time(), tz).astimezone(timezone.utc)
    if end_at > (now or datetime.now(timezone.utc)):
        raise ValueError('report period is not complete')
    return start_at.replace(tzinfo=None), end_at.replace(tzinfo=None)


def completed_periods(zone, now=None, buffer_seconds=600):
    local = ((now or datetime.now(timezone.utc)) - timedelta(seconds=buffer_seconds)).astimezone(ZoneInfo(zone))
    today = local.date()
    monday = today - timedelta(days=today.weekday())
    return [('daily', (today - timedelta(days=1)).isoformat()),
            ('weekly', (monday - timedelta(days=7)).isoformat())]
