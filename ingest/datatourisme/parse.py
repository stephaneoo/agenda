"""From a row of DATAtourisme's simplified export (data.gouv.fr tabular API) to claims.

Columns used: URI_ID_du_POI (identity and source URL), Nom_du_POI, Description,
Categories_de_POI, Latitude, Longitude, Adresse_postale, Code_postal_et_commune
("<postcode>#<commune name>"), Periodes_regroupees, Date_de_mise_a_jour. The export has no
organizer, prices, status or images; contacts are kept in the raw payload only.

Periodes_regroupees is "<start>[<-><end>]" periods joined by "|", dates only (YYYY-MM-DD):
the export carries no times and no weekdays. A recurrence comes as a list of single days, or
as one long range whose name gives the weekday ("Marché du jeudi"): see weekday_in_name.

Bounds (batch C security review): dates are kept within a window around the run's local
date (WINDOW_BEFORE, WINDOW_AFTER years), and a row that still expands to more than
MAX_OCCURRENCES dates is refused (TooManyDates).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from agenda.fields import MAX_DESCRIPTION, MAX_NAME, MAX_TEXT, safe_url
from agenda.periods import Period, expand
from claims.keys import occurrence_key, year_ok
from claims.services import NewClaim
from geo.services import valid_point
from ingest.datatourisme.mapping import category_for, plain

SOURCE = "datatourisme"
KEY_PREFIX = "datatourisme:"
_PERIOD = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:<->(\d{4}-\d{2}-\d{2}))?$")
_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_POSTCODE = re.compile(r"^\d{5}$")
# Control characters other than tab and newline (C0, DEL, C1); PostgreSQL refuses NUL anyway.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# Bidi embeddings, overrides and isolates, the directional marks, and invisible format
# characters: they can reorder or hide text where it is shown. Removed outright.
_FORMAT = re.compile("[\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c\u200b\u2060\ufeff]")
SOURCE_URL_PREFIX = "https://data.datatourisme.fr/"
WINDOW_BEFORE = 1  # years before the run's date
WINDOW_AFTER = 2  # years after
MAX_OCCURRENCES = 400  # per event, after the window


class RowError(ValueError):
    """A row the sync can't use: no identity, no name or no dates."""


class TooManyDates(RowError):
    """A row whose dates expand to more than MAX_OCCURRENCES occurrences: refused."""


# --- Text ----------------------------------------------------------------------------------

def clean(value: Any, *, one_line: bool = False) -> str | None:
    if not isinstance(value, str):
        return None
    text = _CONTROL.sub(" ", _FORMAT.sub("", value))
    text = " ".join(text.split()) if one_line else "\n".join(" ".join(line.split()) for line in text.splitlines())
    return text.strip() or None


def truncate(text: str | None, cap: int) -> str | None:
    """At most `cap` characters: cut at the last word boundary that fits, then "…"."""
    if text is None or len(text) <= cap:
        return text
    cut = text[: cap - 1]
    space = max(cut.rfind(" "), cut.rfind("\n"))
    if space > cap // 2:
        cut = cut[:space]
    return cut.rstrip(" \n,;:-–") + "…"


# --- Columns -------------------------------------------------------------------------------

def types(raw: Any) -> list[str]:
    """The local names of the type URIs ("https://…/core#Market" is "Market")."""
    if not isinstance(raw, str):
        return []
    return [t for t in (u.strip().rsplit("#", 1)[-1].rsplit("/", 1)[-1] for u in raw.split("|")) if t]


def external_id(row: dict) -> str | None:
    """The record's stable id: the last segment of URI_ID_du_POI."""
    uri = row.get("URI_ID_du_POI")
    if not isinstance(uri, str) or not uri.startswith(("https://", "http://")):
        return None
    last = uri.rstrip("/").rsplit("/", 1)[-1]
    return last if _ID.match(last) else None


def event_key(row: dict) -> str | None:
    ident = external_id(row)
    return KEY_PREFIX + ident if ident else None


def coordinate(value: Any, bound: float) -> float | None:
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and -bound <= value <= bound else None


def postcode_and_name(row: dict) -> tuple[str | None, str | None]:
    """("26110", "Nyons") from "26110#Nyons"."""
    raw = row.get("Code_postal_et_commune")
    if not isinstance(raw, str):
        return None, None
    code, _, name = raw.partition("#")
    code = code.strip()
    return (code if _POSTCODE.match(code) else None), clean(name, one_line=True)


def updated_at(row: dict) -> date | None:
    """Date_de_mise_a_jour, the record's last update at the source."""
    raw = row.get("Date_de_mise_a_jour")
    try:
        d = date.fromisoformat(raw[:10]) if isinstance(raw, str) else None
    except ValueError:
        return None
    return d if d and year_ok(d) else None


DAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
# A range longer than this, whose name gives one weekday, is that weekday every week.
WEEKLY_MIN_DAYS = 30


def weekday_in_name(name: str | None) -> int | None:
    """The weekday (0 = Monday) a name gives unambiguously, or None.

    One weekday only must appear in the name, in one of these forms (case and accents
    ignored): "Mardi : …", "… du jeudi", "… tous les mardi(s)", "… le vendredi". The export
    writes a weekly market as one range over the year; its name is where the weekday is.
    Descriptions are never read: too ambiguous.
    """
    if not name:
        return None
    text = plain(name)
    mentioned = {i for i, d in enumerate(DAYS) if re.search(rf"\b{d}s?\b", text)}
    if len(mentioned) != 1:
        return None
    i = mentioned.pop()
    d = DAYS[i]
    forms = (rf"^\s*{d}\s*:", rf"\bdu {d}\b", rf"\btous les {d}s?\b", rf"\ble {d}\b")
    return i if any(re.search(f, text) for f in forms) else None


def _add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 February
        return d.replace(year=d.year + years, day=28)


def window(today: date) -> tuple[date, date]:
    """The dates a sync keeps, around `today` (the run's local date)."""
    return _add_years(today, -WINDOW_BEFORE), _add_years(today, WINDOW_AFTER)


def periods(raw: Any, name: str | None = None, within: tuple[date, date] | None = None) -> list[Period]:
    """The periods of Periodes_regroupees. Malformed pieces are dropped.

    An end before its start in the same year is a season over the new year that the export
    wrote with the start's year: it ends the next year. Multi-day ranges that overlap or
    touch are merged, so a season split in two isn't two spanning occurrences. A merged
    range over WEEKLY_MIN_DAYS whose `name` gives one weekday (weekday_in_name) becomes
    that weekday each week.

    `within` (first, last), when given: single days outside it are dropped, weekly ranges
    are cut to it, and continuous ranges are dropped when they don't overlap it. A
    continuous range that overlaps is kept whole: it is one occurrence whatever its length,
    and its start date is its identity (cutting it would move its URL every day).
    """
    if not isinstance(raw, str):
        return []
    ranges: list[tuple[date, date]] = []
    for piece in raw.split("|"):
        m = _PERIOD.match(piece.strip())
        if not m:
            continue
        try:
            start = date.fromisoformat(m[1])
            end = date.fromisoformat(m[2] or m[1])
            if end < start and end.year == start.year:
                end = end.replace(year=end.year + 1) if (end.month, end.day) != (2, 29) else date(end.year + 1, 2, 28)
        except ValueError:
            continue
        if end < start or not (year_ok(start) and year_ok(end)):
            continue
        ranges.append((start, end))
    singles = sorted({r for r in ranges if r[0] == r[1]})
    merged: list[list[date]] = []
    for start, end in sorted(r for r in ranges if r[0] != r[1]):
        if merged and start <= merged[-1][1] + timedelta(days=1):
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    weekday = weekday_in_name(name)
    lo, hi = within or (date.min, date.max)
    out = [Period(s, e) for s, e in singles if lo <= s <= hi]
    for s, e in merged:
        if e < lo or s > hi:
            continue
        if weekday is not None and (e - s).days > WEEKLY_MIN_DAYS:
            out.append(Period(max(s, lo), min(e, hi), weekdays=frozenset({weekday})))
        else:
            out.append(Period(s, e))
    return out


# --- Claims --------------------------------------------------------------------------------

@dataclass(frozen=True)
class Place:
    """What commune resolution needs from a row."""

    lat: float | None
    lon: float | None
    postal_code: str | None
    commune_name: str | None


def place(row: dict) -> Place:
    lat, lon = coordinate(row.get("Latitude"), 90), coordinate(row.get("Longitude"), 180)
    if not valid_point(lat, lon):  # missing, out of range, or (0, 0): no point at all
        lat = lon = None
    postal_code, name = postcode_and_name(row)
    return Place(lat, lon, postal_code, name)


def source_url(row: dict) -> str | None:
    """URI_ID_du_POI, only when it is a DATAtourisme https URL."""
    url = safe_url(row.get("URI_ID_du_POI"))
    return url if url and url.startswith(SOURCE_URL_PREFIX) else None


def row_to_claims(
    row: dict, *, commune: str, commune_name: str, seen_at: datetime, today: date,
    postal_codes: list[str] | None = None,
) -> tuple[str, list[NewClaim]]:
    """(event key, claims) for one row in `commune` (an INSEE code, already resolved).

    Event: name, description, category, location, organizer and offers (null: the export
    has none), visibility published, source_url, source_updated_at. Each occurrence: dates
    and listed: true. Every claim is seen at `seen_at`, the sync's fetch time. Strings are
    cut to the claim length caps. Dates are kept within window(today), `today` being the
    run's local date. Raises RowError for a row with no id, name or dates, TooManyDates for
    one with more than MAX_OCCURRENCES of them.
    """
    key = event_key(row)
    if key is None:
        raise RowError("no usable URI_ID_du_POI")
    name = truncate(clean(row.get("Nom_du_POI"), one_line=True), MAX_NAME)
    if not name:
        raise RowError(f"{key!r}: no name")
    occurrences = expand(periods(row.get("Periodes_regroupees"), name, window(today)))
    if not occurrences:
        raise RowError(f"{key!r}: no usable dates in Periodes_regroupees (within {window(today)})")
    if len(occurrences) > MAX_OCCURRENCES:
        raise TooManyDates(f"{key!r}: {len(occurrences)} dates, more than {MAX_OCCURRENCES}")
    p = place(row)
    postal_code = p.postal_code or (postal_codes[0] if postal_codes else None)
    if postal_code is None:
        raise RowError(f"{key!r}: no postcode")
    updated = updated_at(row)
    values = {
        "name": name,
        "description": truncate(clean(row.get("Description")), MAX_DESCRIPTION),
        "category": category_for(types(row.get("Categories_de_POI")), name),
        "location": {
            "name": None,
            "street": truncate(clean(row.get("Adresse_postale"), one_line=True), MAX_TEXT),
            "postal_code": postal_code,
            "commune": commune,
            "commune_name": truncate(commune_name, MAX_TEXT),
            "lat": p.lat,
            "lon": p.lon,
        },
        "organizer": None,
        "offers": None,
        "visibility": {"value": "published"},
        "source_url": source_url(row),
        "source_updated_at": updated.isoformat() if updated else None,
    }
    claims = [NewClaim("event", key, field, value, SOURCE, seen_at) for field, value in values.items()]
    for o in occurrences:
        okey = occurrence_key(key, o.start_date)
        claims += [
            NewClaim("occurrence", okey, "dates", o.to_claim(), SOURCE, seen_at),
            NewClaim("occurrence", okey, "listed", True, SOURCE, seen_at),
        ]
    return key, claims
