"""From a source's periods to occurrence dates (spec, Occurrence identity)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, time, timedelta
from typing import Any


@dataclass(frozen=True)
class Period:
    start_date: date
    end_date: date
    start_time: time | None = None
    end_time: time | None = None
    # date.weekday() values (0 = Monday). Set: one occurrence per matching date.
    # None: a continuous range, one occurrence spanning it.
    weekdays: frozenset[int] | None = None


@dataclass(frozen=True)
class OccurrenceDates:
    start_date: date
    start_time: time | None
    end_date: date | None
    end_time: time | None

    def to_claim(self) -> dict[str, str | None]:
        """The value of an occurrence's `dates` claim."""
        return {
            "start_date": self.start_date.isoformat(),
            "start_time": self.start_time.strftime("%H:%M") if self.start_time else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "end_time": self.end_time.strftime("%H:%M") if self.end_time else None,
        }

    @classmethod
    def from_claim(cls, value: Any) -> OccurrenceDates | None:
        """Parse a `dates` claim; None when it is malformed."""
        if not isinstance(value, dict):
            return None
        try:
            start_date = date.fromisoformat(value["start_date"])
            start_time = time.fromisoformat(value["start_time"]) if value.get("start_time") else None
            end_date = date.fromisoformat(value["end_date"]) if value.get("end_date") else None
            end_time = time.fromisoformat(value["end_time"]) if value.get("end_time") else None
        except (KeyError, TypeError, ValueError):
            return None
        if end_time is not None and end_date is None:
            end_date = _end_date(start_date, start_time, end_time)
        if end_date is not None and end_date < start_date:
            return None
        return cls(start_date, start_time, end_date, end_time)


def _end_date(d: date, start_time: time | None, end_time: time | None) -> date:
    # An end earlier than the start on the same date is the next morning.
    return d + timedelta(days=1) if start_time and end_time and end_time < start_time else d


def _one_day(d: date, start_time: time | None, end_time: time | None) -> OccurrenceDates:
    if end_time is None:
        return OccurrenceDates(d, start_time, None, None)
    return OccurrenceDates(d, start_time, _end_date(d, start_time, end_time), end_time)


def _expand_one(p: Period) -> Iterable[OccurrenceDates]:
    if p.end_date < p.start_date:
        return
    if p.weekdays is not None:
        d = p.start_date
        while d <= p.end_date:
            if d.weekday() in p.weekdays:
                yield _one_day(d, p.start_time, p.end_time)
            d += timedelta(days=1)
    elif p.start_date == p.end_date:
        yield _one_day(p.start_date, p.start_time, p.end_time)
    else:
        yield OccurrenceDates(p.start_date, p.start_time, p.end_date, p.end_time)


def _earliest(o: OccurrenceDates) -> tuple[bool, time]:
    return (o.start_time is None, o.start_time or time())


def expand(periods: list[Period]) -> list[OccurrenceDates]:
    """One occurrence per date, sorted by date. Two showings on the same day are one
    occurrence, the earliest (an accepted v1 limitation)."""
    by_date: dict[date, OccurrenceDates] = {}
    for p in periods:
        for o in _expand_one(p):
            current = by_date.get(o.start_date)
            if current is None or _earliest(o) < _earliest(current):
                by_date[o.start_date] = o
    return [by_date[d] for d in sorted(by_date)]
