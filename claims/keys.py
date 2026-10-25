"""Target keys. An event key is the source's own ("datatourisme:<id>"); an occurrence key is
"<event key>/<YYYY-MM-DD>", the occurrence's local start date (spec, Occurrence identity)."""

from __future__ import annotations

import re
from datetime import date

_OCCURRENCE = re.compile(r"^(?P<event>.+)/(?P<date>\d{4}-\d{2}-\d{2})$")
# Dates the projection can work with. Before 1912, Paris time was not a whole-minute offset
# of UTC (+00:09:21 until March 1911), which RFC 3339 can't write and the varchar(25) columns
# can't hold; the upper bound keeps every date arithmetic far from date.max.
MIN_YEAR, MAX_YEAR = 1912, 2200
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def year_ok(d: date) -> bool:
    return MIN_YEAR <= d.year <= MAX_YEAR


def occurrence_key(event_key: str, local_date: date) -> str:
    return f"{event_key}/{local_date.isoformat()}"


def split_occurrence_key(key: str) -> tuple[str, date] | None:
    """(event key, local date) of an occurrence key, or None if it isn't one (a date outside
    MIN_YEAR..MAX_YEAR included)."""
    m = _OCCURRENCE.match(key)
    if not m:
        return None
    try:
        d = date.fromisoformat(m["date"])
    except ValueError:
        return None
    return (m["event"], d) if year_ok(d) else None


def event_key_of(target_type: str, target_key: str) -> str | None:
    """The event a target belongs to, or None for a malformed target.

    Keys with control characters (NUL, newlines) are malformed: PostgreSQL can't store a NUL,
    and a newline would forge log lines. An event key never looks like an occurrence key.
    """
    if not isinstance(target_key, str) or not target_key or _CONTROL.search(target_key):
        return None
    if target_type == "event":
        return None if _OCCURRENCE.match(target_key) else target_key
    if target_type == "occurrence":
        parts = split_occurrence_key(target_key)
        return parts[0] if parts else None
    return None
