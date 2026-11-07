"""Schedules for jobs that should run now and then rather than stay up: ``every 15m`` or a cron expression."""

from __future__ import annotations

import re
from dataclasses import dataclass
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
        if len(expr.split()) != 5:
            # cronsim also reads six fields with seconds first: "* * * * * *" would start it every second.
            raise CronSimError("a cron line has five fields: minute, hour, day of month, month, day of week")
        return next(CronSim(expr, after))
    except (CronSimError, StopIteration) as exc:
        raise ValueError(f"Not a schedule: {text!r} (try 'every 15m' or a cron line like '*/5 * * * *')") from exc


def upgrade(value: object) -> object:
    """A schedule saved before 0.0.18 may have six cron fields, seconds first: drop the seconds, so the service
    still loads and runs at most once a minute (for "0 */5 * * * *", exactly as meant)."""
    if isinstance(value, str) and not _EVERY.fullmatch(text := " ".join(value.split())) \
            and len(fields := text.split()) == 6:
        return " ".join(fields[1:])
    return value


# -- the schedule builder in the service settings --------------------------------------------------------------
#
# A schedule is still saved as text ("every 15m" or a cron line), so a service file reads the same as before. The
# settings show the common shapes as a form (Repeat: every day, at 09:00) and read them back from the text; any
# other cron line is "Custom" and edited as text.

INTERVAL, HOURLY, DAILY, WEEKDAYS, WEEKLY, MONTHLY, CUSTOM = (
    "interval", "hourly", "daily", "weekdays", "weekly", "monthly", "custom")
KINDS = (INTERVAL, HOURLY, DAILY, WEEKDAYS, WEEKLY, MONTHLY, CUSTOM)
DAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")  # cron's 0-6
UNIT_NAMES = {"s": "second", "m": "minute", "h": "hour", "d": "day"}

# One click each in the settings: (chip text, schedule).
PRESETS: tuple[tuple[str, str], ...] = (
    ("Every 5 min", "every 5m"),
    ("Every 15 min", "every 15m"),
    ("Hourly", "0 * * * *"),
    ("Every 6 hours", "every 6h"),
    ("Nightly 03:00", "0 3 * * *"),
    ("Weekdays 09:00", "0 9 * * 1-5"),
    ("Mondays 09:00", "0 9 * * 1"),
    ("Monthly, the 1st", "0 0 1 * *"),
)


@dataclass
class Plan:
    """A schedule as the form shows it. ``to_text`` and ``read`` turn it into the saved text and back."""

    kind: str = INTERVAL
    every: int = 15  # INTERVAL: how many ``unit``
    unit: str = "m"  # s, m, h or d
    hour: int = 9
    minute: int = 0
    weekday: int = 1  # WEEKLY: 0 Sunday … 6 Saturday
    day: int = 1  # MONTHLY: day of the month
    text: str = ""  # CUSTOM: the cron line as written

    def to_text(self) -> str:
        if self.kind == INTERVAL:
            return f"every {self.every}{self.unit}"
        at = f"{self.minute} {self.hour}"
        return {HOURLY: f"{self.minute} * * * *", DAILY: f"{at} * * *", WEEKDAYS: f"{at} * * 1-5",
                WEEKLY: f"{at} * * {self.weekday}", MONTHLY: f"{at} {self.day} * *"}.get(self.kind, self.text)


def _int(text: str, lo: int, hi: int) -> int | None:
    return int(text) if text.isdigit() and lo <= int(text) <= hi else None


def read(text: str) -> Plan:
    """The form for a saved schedule: one of the common shapes, or CUSTOM with the text as it is."""
    text = " ".join(text.split())
    if match := _EVERY.fullmatch(text):
        unit = match.group(2).lower()[0]
        return Plan(INTERVAL, every=int(match.group(1)), unit=unit)
    fields = _MACROS.get(text.lower(), text).split()
    custom = Plan(CUSTOM, text=text)
    if len(fields) != 5:
        return custom
    minute, hour, dom, month, dow = fields
    m = _int(minute, 0, 59)
    if m is None or month != "*":
        return custom
    if hour == "*" and dom == "*" and dow == "*":
        return Plan(HOURLY, minute=m)
    h = _int(hour, 0, 23)
    if h is None:
        return custom
    if dom == "*" and dow == "*":
        return Plan(DAILY, hour=h, minute=m)
    if dom == "*" and dow == "1-5":
        return Plan(WEEKDAYS, hour=h, minute=m)
    if dom == "*" and (wd := _int(dow, 0, 7)) is not None:
        return Plan(WEEKLY, hour=h, minute=m, weekday=wd % 7)
    if dow == "*" and (d := _int(dom, 1, 31)) is not None:
        return Plan(MONTHLY, hour=h, minute=m, day=d)
    return custom


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def describe(text: str) -> str:
    """The schedule in words: "Every day at 09:00". Empty: "Keeps running"."""
    if not text.strip():
        return "Keeps running"
    p = read(text)
    at = f"{p.hour:02d}:{p.minute:02d}"
    if p.kind == INTERVAL:
        name = UNIT_NAMES.get(p.unit, p.unit)
        return f"Every {name}" if p.every == 1 else f"Every {p.every} {name}s"
    if p.kind == HOURLY:
        return "Every hour, on the hour" if p.minute == 0 else f"Every hour at :{p.minute:02d}"
    return {DAILY: f"Every day at {at}", WEEKDAYS: f"Monday to Friday at {at}",
            WEEKLY: f"Every {DAYS[p.weekday]} at {at}",
            MONTHLY: f"On the {_ordinal(p.day)} of every month at {at}"}.get(p.kind, f"Cron: {p.text}")


def upcoming(text: str, after: datetime, count: int = 3) -> list[datetime]:
    """The next ``count`` runs after ``after``, as the supervisor plans them. An interval counts from when the last
    run ends; this assumes each run is short."""
    out: list[datetime] = []
    at = after
    for _ in range(count):
        at = next_run(text, at)
        out.append(at)
    return out
