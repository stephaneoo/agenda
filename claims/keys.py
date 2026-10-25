"""Target keys. An event key is the source's own ("datatourisme:<id>"); an occurrence key is
"<event key>/<YYYY-MM-DD>", the occurrence's local start date (spec, Occurrence identity)."""

from __future__ import annotations

import re
from datetime import date

_OCCURRENCE = re.compile(r"^(?P<event>.+)/(?P<date>\d{4}-\d{2}-\d{2})$")


def occurrence_key(event_key: str, local_date: date) -> str:
    return f"{event_key}/{local_date.isoformat()}"


def split_occurrence_key(key: str) -> tuple[str, date] | None:
    """(event key, local date) of an occurrence key, or None if it isn't one."""
    m = _OCCURRENCE.match(key)
    if not m:
        return None
    try:
        return m["event"], date.fromisoformat(m["date"])
    except ValueError:
        return None


def event_key_of(target_type: str, target_key: str) -> str | None:
    """The event a target belongs to, or None for a malformed target."""
    if target_type == "event":
        return target_key if target_key and split_occurrence_key(target_key) is None else None
    if target_type == "occurrence":
        parts = split_occurrence_key(target_key)
        return parts[0] if parts else None
    return None
