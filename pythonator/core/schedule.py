"""Schedules for jobs that should run now and then rather than stay up: ``every 15m`` or a cron expression."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from cronsim import CronSim, CronSimError

_EVERY = re.compile(r"every\s+(\d{1,5})\s*(s|sec|m|min|h|hour|d|day)s?", re.IGNORECASE)
_UNITS = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hour": 3600, "d": 86400, "day": 86400}
_MACROS = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *"}


def check(text: str) -> str:
    """The schedule, cleaned; raises ValueError for one that can't be read."""
    text = " ".join(text.split())
    if not text:
        return ""
    next_run(text, datetime.now())
    return text


def next_run(text: str, after: datetime) -> datetime:
    """When the schedule next fires after ``after`` (local time)."""
    if match := _EVERY.fullmatch(text):
        seconds = int(match.group(1)) * _UNITS[match.group(2).lower()]
        if seconds < 10:
            raise ValueError("Run at most every 10 seconds")
        return after + timedelta(seconds=seconds)
    expr = _MACROS.get(text.lower(), text)
    try:
        return next(CronSim(expr, after))
    except (CronSimError, StopIteration) as exc:
        raise ValueError(f"Not a schedule: {text!r} (try 'every 15m' or a cron line like '*/5 * * * *')") from exc
