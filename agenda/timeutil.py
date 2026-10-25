"""Local times in Europe/Paris, as the API and JSON-LD write them (RFC 3339 with the offset in
force at that instant, or a bare date when no time is known)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
_TICK = timedelta(microseconds=1)


def _exists(local: datetime) -> bool:
    return local.astimezone(UTC).astimezone(PARIS).replace(tzinfo=None) == local.replace(tzinfo=None)


def local_datetime(d: date, t: time) -> datetime:
    """The instant of a wall-clock time in Paris.

    An ambiguous time (the October change) takes its first instance, fold=0, in summer time.
    A time that doesn't exist (the March change) moves forward to the end of the gap, 03:00.
    """
    wall = datetime.combine(d, t.replace(tzinfo=None, fold=0))
    local = wall.replace(tzinfo=PARIS)
    if _exists(local):
        return local
    # Walk forward to the first whole minute that exists. The gap is an hour at most.
    wall = wall.replace(second=0, microsecond=0)
    for _ in range(24 * 60):
        wall += timedelta(minutes=1)
        if _exists(local := wall.replace(tzinfo=PARIS)):
            return local
    raise ValueError(f"no valid local time after {d} {t}")  # pragma: no cover


def format_local(instant: datetime) -> str:
    """An aware datetime as a Paris local date-time, to the second."""
    return instant.astimezone(PARIS).replace(microsecond=0).isoformat()


def to_local(d: date, t: time | None) -> str:
    return d.isoformat() if t is None else format_local(local_datetime(d, t))


def day_start(d: date) -> datetime:
    """Local midnight, which always exists in Paris (the changes happen at 02:00 and 03:00)."""
    return datetime.combine(d, time()).replace(tzinfo=PARIS)


def parse(value: str) -> date | datetime:
    """A start or end as written by to_local: a date, or an aware datetime."""
    if len(value) == 10:
        return date.fromisoformat(value)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"local date-time without an offset: {value!r}")
    return parsed


def bounds(start: str, end: str | None) -> tuple[datetime, datetime]:
    """(first instant, last instant included) of an occurrence, in UTC.

    A date runs to 24:00 local time inclusive. A date-time end is when it ends; with no end,
    a date-time start is an instant.
    """
    s = parse(start)
    first = day_start(s) if not isinstance(s, datetime) else s
    if end is None:
        last = first if isinstance(s, datetime) else day_start(s + timedelta(days=1)) - _TICK
    else:
        e = parse(end)
        if isinstance(e, datetime):
            # An end at 00:00 doesn't reach into that day.
            last = max(first, e - _TICK)
        else:
            last = day_start(e + timedelta(days=1)) - _TICK
    return first.astimezone(UTC), last.astimezone(UTC)


def today() -> date:
    return datetime.now(PARIS).date()
