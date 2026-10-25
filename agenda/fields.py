"""The claim fields the projection understands, and what a valid value of each looks like.

claims.services.write_claims refuses invalid values. The projection also drops invalid
claims before resolving, so a claim stored before validation existed can't win: the next
valid claim does. The one exception is `visibility`, which fails closed: an invalid value
counts as "hidden" rather than being skipped.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit

from agenda.categories import SLUGS
from agenda.periods import OccurrenceDates
from claims.keys import split_occurrence_key

VISIBILITIES = ("published", "pending", "hidden")
STATUSES = ("scheduled", "rescheduled", "postponed", "cancelled")
EVENT_FIELDS = {"name", "description", "category", "location", "organizer", "offers", "visibility",
                "source_url", "source_updated_at"}
OCCURRENCE_FIELDS = {"dates", "listed", "status", "previous_start", "moved_to", "visibility"}
# Fields kept per source instead of resolved by priority.
PER_SOURCE = {"listed", "source_url", "source_updated_at"}

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def local_value(value: Any) -> str | date | datetime | None:
    """A local date ("YYYY-MM-DD") or an RFC 3339 date-time with an offset; None otherwise."""
    if not isinstance(value, str):
        return None
    try:
        if _DATE.match(value):
            return date.fromisoformat(value)
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and "T" in value else None


def safe_url(value: Any) -> str | None:
    """The URL if it is absolute http(s) with a host, else None. Anything else (javascript:,
    data:, relative, scheme-relative) must never become a link."""
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    return url


def visibility_of(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("value")
    return value if value in VISIBILITIES else None


def _str_or_none(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _number_or_none(value: Any) -> bool:
    return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value))


def _location_problem(value: Any) -> str | None:
    if not isinstance(value, dict):
        return "location must be an object"
    for key in ("postal_code", "commune", "commune_name"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            return f"location.{key} is required"
    for key in ("name", "street"):
        if not _str_or_none(value.get(key)):
            return f"location.{key} must be a string or null"
    for key in ("lat", "lon"):
        if not _number_or_none(value.get(key)):
            return f"location.{key} must be a number or null"
    return None


def problem(target_type: str, target_key: str, field: str, value: Any) -> str | None:
    """Why this claim value is invalid, or None when it is fine."""
    if target_type == "event":
        if field not in EVENT_FIELDS:
            return f"unknown event field {field!r}"
        if field == "name":
            return None if isinstance(value, str) else "name must be a string"
        if field == "description":
            return None if _str_or_none(value) else "description must be a string or null"
        if field == "category":
            return None if value in SLUGS else f"unknown category {value!r}"
        if field == "location":
            return _location_problem(value)
        if field == "organizer":
            if value is None:
                return None
            if not isinstance(value, dict) or not isinstance(value.get("name"), str) or not _str_or_none(value.get("url")):
                return "organizer must be {name, url} or null"
            return None
        if field == "offers":
            if value is None:
                return None
            if not isinstance(value, dict) or not (value.get("is_free") is None or isinstance(value["is_free"], bool)) \
                    or not _str_or_none(value.get("price")) or not _str_or_none(value.get("url")):
                return "offers must be {is_free, price, url} or null"
            return None
        if field == "visibility":
            return None if visibility_of(value) else f"unknown visibility {value!r}"
        if field == "source_url":
            return None if _str_or_none(value) else "source_url must be a string or null"
        if field == "source_updated_at":
            return None if value is None or local_value(value) is not None else "source_updated_at is not a local date or date-time"
    parts = split_occurrence_key(target_key)
    if target_type != "occurrence" or parts is None:
        return "malformed target"
    event_key, local_date = parts
    if field not in OCCURRENCE_FIELDS:
        return f"unknown occurrence field {field!r}"
    if field == "dates":
        dates = OccurrenceDates.from_claim(value)
        if dates is None:
            return "malformed dates"
        return None if dates.start_date == local_date else "dates.start_date must be the date in the occurrence key"
    if field == "listed":
        return None if isinstance(value, bool) else "listed must be true or false"
    if field == "status":
        return None if value in STATUSES else f"unknown status {value!r}"
    if field == "previous_start":
        return None if value is None or local_value(value) is not None else "malformed previous_start"
    if field == "moved_to":
        target = split_occurrence_key(value) if isinstance(value, str) else None
        if target is None or target[0] != event_key or value == target_key:
            return "moved_to must be another occurrence key of the same event"
        return None
    if field == "visibility":
        return None if visibility_of(value) else f"unknown visibility {value!r}"
    return None  # pragma: no cover
