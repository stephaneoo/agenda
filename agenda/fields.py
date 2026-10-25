"""The claim fields the projection understands, and what a valid value of each looks like.

claims.services.write_claims refuses invalid values. The projection also drops invalid
claims before resolving, so a claim stored before validation existed can't win: the next
valid claim does. The one exception is `visibility`, which fails closed: an invalid value
counts as "hidden" rather than being skipped.

problem() is total over what the projection does: any value it accepts, the projection can
cast and store (tests/agenda/test_fields_total.py). That covers the typed columns (the INSEE
commune in varchar(5), dates and times within claims.keys.MIN_YEAR..MAX_YEAR), PostgreSQL's
limits (no NUL in text or jsonb), and the cost of a request (length caps on every string).
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from agenda.categories import SLUGS
from agenda.periods import OccurrenceDates
from agenda.timeutil import PARIS
from claims.keys import split_occurrence_key, year_ok

VISIBILITIES = ("published", "pending", "hidden")
STATUSES = ("scheduled", "rescheduled", "postponed", "cancelled")
EVENT_FIELDS = {"name", "description", "category", "location", "organizer", "offers", "visibility",
                "source_url", "source_updated_at"}
OCCURRENCE_FIELDS = {"dates", "listed", "status", "previous_start", "moved_to", "visibility"}
# Fields kept per source instead of resolved by priority.
PER_SOURCE = {"listed", "source_url", "source_updated_at"}

# Length caps, in characters. A longer value is refused, not truncated.
MAX_NAME = 300
MAX_DESCRIPTION = 10_000
MAX_TEXT = 300  # every other string: location parts, organizer name, price, reason
MAX_URL = 2_000
# The whole value as JSON text, unknown keys included.
MAX_VALUE_JSON = 32_000
# Real UTC offsets stay within ±14:00.
MAX_OFFSET = timedelta(hours=14)

INSEE = re.compile(r"^[0-9][0-9AB][0-9]{3}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def local_value(value: Any, *, paris_offset: bool = False) -> date | datetime | None:
    """A local date ("YYYY-MM-DD") or an RFC 3339 date-time with an offset; None otherwise.

    Years must be within claims.keys.MIN_YEAR..MAX_YEAR, the offset a whole number of minutes
    within ±14:00, and with `paris_offset` the one in force in Europe/Paris at that instant.
    """
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        if _DATE.match(value):
            d = date.fromisoformat(value)
            return d if year_ok(d) else None
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or "T" not in value or not year_ok(parsed):
        return None
    offset = parsed.utcoffset()
    if offset is None or abs(offset) > MAX_OFFSET or offset % timedelta(minutes=1):
        return None
    if paris_offset and parsed.astimezone(PARIS).utcoffset() != offset:
        return None
    return parsed


def safe_url(value: Any) -> str | None:
    """The URL if it is absolute http(s) with a host and no user info, else None. Anything else
    (javascript:, data:, relative, scheme-relative, user@host) must never become a link."""
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or len(url) > MAX_URL or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        return None
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not hostname or "@" in parts.netloc:
        return None
    return url


def visibility_of(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("value")
    return value if isinstance(value, str) and value in VISIBILITIES else None


def _string(value: Any, name: str, cap: int, *, nullable: bool = True) -> str | None:
    """Why `value` isn't a string (or null, if nullable) of at most `cap` characters."""
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        return f"{name} must be a string" + (" or null" if nullable else "")
    if len(value) > cap:
        return f"{name} is longer than {cap} characters"
    return None


def _number(value: Any, name: str, bound: int) -> str | None:
    if value is None:
        return None
    # Compared before any float conversion: an int too large for a float must not raise.
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not -bound <= value <= bound:
        return f"{name} must be a number between -{bound} and {bound}, or null"
    if isinstance(value, float) and not math.isfinite(value):
        return f"{name} must be finite"
    return None


def _location_problem(value: Any) -> str | None:
    if not isinstance(value, dict):
        return "location must be an object"
    for key in ("postal_code", "commune", "commune_name"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            return f"location.{key} is required"
    if not INSEE.match(value["commune"].strip()):
        return "location.commune must be an INSEE code"
    for key in ("postal_code", "commune_name", "name", "street"):
        if why := _string(value.get(key), f"location.{key}", MAX_TEXT):
            return why
    return _number(value.get("lat"), "location.lat", 90) or _number(value.get("lon"), "location.lon", 180)


def _organizer_problem(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return "organizer must be {name, url} or null"
    return _string(value.get("name"), "organizer.name", MAX_TEXT, nullable=False) \
        or _string(value.get("url"), "organizer.url", MAX_URL)


def _offers_problem(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not (value.get("is_free") is None or isinstance(value["is_free"], bool)):
        return "offers must be {is_free, price, url} or null"
    return _string(value.get("price"), "offers.price", MAX_TEXT) or _string(value.get("url"), "offers.url", MAX_URL)


def _visibility_problem(value: Any) -> str | None:
    if not visibility_of(value):
        return f"unknown visibility {value!r}"[:200]
    return _string(value.get("reason"), "visibility.reason", MAX_TEXT) if isinstance(value, dict) else None


def _has_nul(value: Any) -> bool:
    """PostgreSQL text and jsonb can't hold U+0000, in a value or in a key."""
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_has_nul(k) or _has_nul(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_has_nul(v) for v in value)
    return False


MAX_DEPTH = 32  # no valid value nests more than a few levels


def _too_deep(value: Any) -> bool:
    """Nested more than MAX_DEPTH lists or objects, checked without recursion."""
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, (dict, list)):
            if depth >= MAX_DEPTH:
                return True
            stack.extend((v, depth + 1) for v in (item.values() if isinstance(item, dict) else item))
    return False


def _size_problem(value: Any) -> str | None:
    if _too_deep(value):
        return f"value is nested more than {MAX_DEPTH} levels"
    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return "value is not JSON"
    if len(text) > MAX_VALUE_JSON:
        return f"value is longer than {MAX_VALUE_JSON} characters as JSON"
    if _has_nul(value):
        return "value contains a NUL character"
    return None


def problem(target_type: str, target_key: str, field: str, value: Any) -> str | None:
    """Why this claim value is invalid, or None when it is fine."""
    if why := _size_problem(value):
        return why
    if target_type == "event":
        if field not in EVENT_FIELDS:
            return f"unknown event field {field!r}"
        if field == "name":
            return _string(value, "name", MAX_NAME, nullable=False)
        if field == "description":
            return _string(value, "description", MAX_DESCRIPTION)
        if field == "category":
            return None if isinstance(value, str) and value in SLUGS else f"unknown category {value!r}"[:200]
        if field == "location":
            return _location_problem(value)
        if field == "organizer":
            return _organizer_problem(value)
        if field == "offers":
            return _offers_problem(value)
        if field == "visibility":
            return _visibility_problem(value)
        if field == "source_url":
            return _string(value, "source_url", MAX_URL)
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
        return None if isinstance(value, str) and value in STATUSES else f"unknown status {value!r}"[:200]
    if field == "previous_start":
        if value is None or local_value(value, paris_offset=True) is not None:
            return None
        return "previous_start must be a local date, or a date-time with the Europe/Paris offset"
    if field == "moved_to":
        target = split_occurrence_key(value) if isinstance(value, str) else None
        if target is None or target[0] != event_key or value == target_key:
            return "moved_to must be another occurrence key of the same event"
        return None
    if field == "visibility":
        return _visibility_problem(value)
    return None  # pragma: no cover
