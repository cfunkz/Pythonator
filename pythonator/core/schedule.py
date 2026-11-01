"""Run schedules: ``every 15m``, daily times (``09:00, 18:30``) and five-field cron expressions.

Everything works on naive local ``datetime`` values, like cron does. :func:`parse_schedule`
validates the text once; :meth:`Schedule.next_after` is then cheap enough to call every tick.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

_UNITS = {
    "s": 1, "sec": 1, "second": 1,
    "m": 60, "min": 60, "minute": 60,
    "h": 3600, "hr": 3600, "hour": 3600,
    "d": 86400, "day": 86400,
}
_EVERY = re.compile(r"^every\s+(\d+(?:\.\d+)?)\s*([a-z]+)$")
_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")
_MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
_MONTHS = {m: i for i, m in enumerate(("jan feb mar apr may jun jul aug sep oct nov dec").split(), 1)}
_DAYS = {d: i for i, d in enumerate(("sun mon tue wed thu fri sat").split())}
MIN_INTERVAL = 1.0
_SEARCH_LIMIT = 50_000  # cron search steps; plenty for any expression that can match at all


class ScheduleError(ValueError):
    """The schedule text can't be understood."""


class Schedule(Protocol):
    @property
    def text(self) -> str: ...

    def next_after(self, moment: datetime) -> datetime: ...

    def describe(self) -> str: ...


@dataclass(frozen=True, slots=True)
class Interval:
    text: str
    seconds: float

    def next_after(self, moment: datetime) -> datetime:
        return moment + timedelta(seconds=self.seconds)

    def describe(self) -> str:
        return f"every {_fmt_seconds(self.seconds)}"


@dataclass(frozen=True, slots=True)
class Daily:
    text: str
    times: tuple[tuple[int, int], ...]  # sorted (hour, minute)

    def next_after(self, moment: datetime) -> datetime:
        for days in (0, 1):
            day = moment.date() + timedelta(days=days)
            for hour, minute in self.times:
                candidate = datetime(day.year, day.month, day.day, hour, minute)
                if candidate > moment:
                    return candidate
        raise AssertionError("unreachable: a daily schedule always fires within 24 hours")

    def describe(self) -> str:
        return "daily at " + ", ".join(f"{h:02d}:{m:02d}" for h, m in self.times)


@dataclass(frozen=True, slots=True)
class Cron:
    text: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    any_day: bool  # day-of-month field was "*"
    any_weekday: bool  # day-of-week field was "*"

    def _day_matches(self, moment: datetime) -> bool:
        dom = moment.day in self.days
        dow = (moment.weekday() + 1) % 7 in self.weekdays
        if self.any_day or self.any_weekday:
            return dom and dow  # "*" matches everything, so this is just the other field
        return dom or dow  # cron: when both are restricted, either may match

    def next_after(self, moment: datetime) -> datetime:
        t = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(_SEARCH_LIMIT):
            if t.month not in self.months:
                t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
            elif not self._day_matches(t):
                t = t.replace(hour=0, minute=0) + timedelta(days=1)
            elif t.hour not in self.hours:
                t = t.replace(minute=0) + timedelta(hours=1)
            elif t.minute not in self.minutes:
                later = [m for m in self.minutes if m > t.minute]
                t = t.replace(minute=min(later)) if later else t.replace(minute=0) + timedelta(hours=1)
            else:
                return t
        raise ScheduleError(f"“{self.text}” never matches a date")

    def describe(self) -> str:
        return f"cron {self.text}"


def parse_schedule(text: str) -> Schedule | None:
    """Parse ``text``; ``None`` for an empty schedule. Raises :class:`ScheduleError`."""
    raw = " ".join(text.split())
    if not raw:
        return None
    lower = raw.lower()
    if m := _EVERY.match(lower):
        word = m.group(2)
        unit = _UNITS.get(word) or _UNITS.get(word.removesuffix("s"))
        if unit is None:
            raise ScheduleError(f"Unknown unit “{m.group(2)}”. Use s, m, h or d, e.g. every 15m.")
        seconds = float(m.group(1)) * unit
        if seconds < MIN_INTERVAL:
            raise ScheduleError("The interval must be at least 1 second.")
        return Interval(raw, seconds)
    if lower.startswith("daily "):
        lower = lower[6:]
    parts = [p.strip() for p in lower.replace(";", ",").split(",") if p.strip()]
    if parts and all(_TIME.match(p) for p in parts):
        times: set[tuple[int, int]] = set()
        for p in parts:
            hm = _TIME.match(p)
            assert hm is not None
            hour, minute = int(hm.group(1)), int(hm.group(2))
            if hour > 23 or minute > 59:
                raise ScheduleError(f"“{p}” is not a valid time of day.")
            times.add((hour, minute))
        return Daily(raw, tuple(sorted(times)))
    cron = _parse_cron(raw)
    cron.next_after(datetime.now())  # rejects expressions that can never fire (e.g. 30 Feb)
    return cron


def _parse_cron(raw: str) -> Cron:
    expr = _MACROS.get(raw.lower(), raw)
    fields = expr.split()
    if len(fields) != 5:
        raise ScheduleError(
            "Use “every 15m”, times like “09:00, 18:30”, or a cron expression like “*/5 * * * *”."
        )
    minute, hour, dom, month, dow = fields
    weekdays = _parse_field(dow, 0, 7, _DAYS, "day of week")
    if 7 in weekdays:
        weekdays = (weekdays - {7}) | {0}
    return Cron(
        text=raw,
        minutes=_parse_field(minute, 0, 59, {}, "minute"),
        hours=_parse_field(hour, 0, 23, {}, "hour"),
        days=_parse_field(dom, 1, 31, {}, "day of month"),
        months=_parse_field(month, 1, 12, _MONTHS, "month"),
        weekdays=frozenset(weekdays),
        any_day=dom == "*",
        any_weekday=dow == "*",
    )


def _parse_field(text: str, lo: int, hi: int, names: dict[str, int], what: str) -> frozenset[int]:
    def value(token: str) -> int:
        token = token.lower()
        if token in names:
            return names[token]
        if not token.isdigit():
            raise ScheduleError(f"“{token}” is not a valid {what}.")
        n = int(token)
        if not lo <= n <= hi:
            raise ScheduleError(f"{what.capitalize()} {n} is out of range ({lo}-{hi}).")
        return n

    out: set[int] = set()
    for item in text.split(","):
        rng, _, step_text = item.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) == 0:
                raise ScheduleError(f"“{item}” has an invalid step.")
            step = int(step_text)
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            a, _, b = rng.partition("-")
            start, end = value(a), value(b)
            if start > end:
                raise ScheduleError(f"“{rng}” is an empty range.")
        else:
            start = value(rng)
            end = hi if step_text else start
        out.update(range(start, end + 1, step))
    if not out:
        raise ScheduleError(f"Empty {what} field.")
    return frozenset(out)


def _fmt_seconds(seconds: float) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{int(seconds // size)}{unit}"
    return f"{seconds:g}s"


def upcoming(schedule: Schedule, count: int, start: datetime | None = None) -> list[datetime]:
    moment = start or datetime.now()
    out: list[datetime] = []
    for _ in range(count):
        moment = schedule.next_after(moment)
        out.append(moment)
    return out
