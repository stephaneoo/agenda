"""The read service layer. The API, the pages, the offline export and the future MCP server
call only this module; it reads only the canonical tables, never the claims."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from django.db.models import DateTimeField, ExpressionWrapper, F, Q, Value

from agenda.categories import SLUGS
from agenda.models import Event, Occurrence
from agenda.timeutil import day_start, format_local, today
from claims.models import Claim, Source
from geo.models import MatrixInfo
from geo.services import covered_communes, reachable

MAX_WITHIN = 60
MAX_SPAN_DAYS = 92
DEFAULT_SPAN_DAYS = 30
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class InvalidQuery(Exception):
    """A request the spec answers with 422."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class NotFound(Exception):
    """No such ID (404)."""


@dataclass(frozen=True)
class Gone:
    """Hidden, pending or withdrawn (410)."""

    id: str


@dataclass(frozen=True)
class MovedTo:
    """Moved by a moderator to another occurrence (301)."""

    new_id: str


@dataclass
class Page:
    items: list[Occurrence]
    next: str | None


@dataclass(frozen=True)
class Query:
    """A validated search: the communes it keeps (None: all) and the local date range."""

    communes: frozenset[str] | None
    date_from: date
    date_to: date
    categories: tuple[str, ...]


def _encode_cursor(occ: Occurrence) -> str:
    raw = json.dumps({"s": occ.start_utc.isoformat(), "i": occ.id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        start = datetime.fromisoformat(raw["s"])
        if start.tzinfo is None or not isinstance(raw["i"], str):
            raise ValueError
        return start, raw["i"]
    except (binascii.Error, ValueError, TypeError, KeyError, UnicodeDecodeError):
        raise InvalidQuery("invalid cursor") from None


def validate_query(
    commune: str | None, within: int | None, date_from: date | None, date_to: date | None, categories: list[str]
) -> Query:
    """The checks of GET /api/v1/occurrences. Raises InvalidQuery for each 422 case."""
    if within is not None and not commune:
        raise InvalidQuery("within requires commune")
    if within is not None and not 0 <= within <= MAX_WITHIN:
        raise InvalidQuery(f"within must be between 0 and {MAX_WITHIN}")
    communes = None
    if commune:
        if not covered_communes().filter(insee=commune).exists():
            raise InvalidQuery(f"commune {commune} is not in the covered area")
        # A commune with no radius keeps that commune only.
        communes = frozenset(reachable(commune, within or 0))
    date_from = date_from or today()
    date_to = date_to or date_from + timedelta(days=DEFAULT_SPAN_DAYS)
    if date_to < date_from:
        raise InvalidQuery("to is before from")
    if (date_to - date_from).days > MAX_SPAN_DAYS:
        raise InvalidQuery(f"the date range spans more than {MAX_SPAN_DAYS} days")
    unknown = sorted(set(categories) - set(SLUGS))
    if unknown:
        raise InvalidQuery(f"unknown category: {', '.join(unknown)}")
    return Query(communes, date_from, date_to, tuple(sorted(set(categories))))


def matching(query: Query):
    """Published occurrences that overlap the range: any part between from 00:00 and to 24:00."""
    qs = Occurrence.objects.filter(
        shown=True,
        start_utc__lt=day_start(query.date_to + timedelta(days=1)),
        end_utc_inclusive__gte=day_start(query.date_from),
    )
    if query.communes is not None:
        qs = qs.filter(event__commune__in=query.communes)
    if query.categories:
        qs = qs.filter(event__category__in=query.categories)
    # Sorting on an expression (start_utc + 0) keeps the planner off the (start_utc, id)
    # index, which would walk every past occurrence; the end_utc_inclusive index then picks
    # the rows still running, so the cost doesn't grow with history. Same order.
    sort_start = ExpressionWrapper(F("start_utc") + Value(timedelta(0)), output_field=DateTimeField())
    return qs.select_related("event").order_by(sort_start, "id")


def search_occurrences(
    commune: str | None,
    within: int | None,
    date_from: date | None,
    date_to: date | None,
    categories: list[str],
    cursor: str | None,
    limit: int = DEFAULT_LIMIT,
) -> Page:
    """One page of matching occurrences, sorted by start then id, with a keyset cursor."""
    if not 1 <= limit <= MAX_LIMIT:
        raise InvalidQuery(f"limit must be between 1 and {MAX_LIMIT}")
    query = validate_query(commune, within, date_from, date_to, categories)
    qs = matching(query)
    if cursor:
        start, last_id = _decode_cursor(cursor)
        qs = qs.filter(Q(start_utc__gt=start) | Q(start_utc=start, id__gt=last_id))
    rows = list(qs[: limit + 1])
    items = rows[:limit]
    return Page(items, _encode_cursor(items[-1]) if len(rows) > limit else None)


def get_occurrence(id: str) -> Occurrence | Gone | MovedTo:
    """A single occurrence, past ones included. Raises NotFound for an unknown ID."""
    occ = Occurrence.objects.select_related("event").filter(id=id).first()
    if occ is None:
        raise NotFound(id)
    if occ.moved_to_id:
        # moved_to_id is already the final target (the projection follows chains); that
        # target's own state decides: a redirect to it, or 410 when it isn't published.
        target_shown = Occurrence.objects.filter(id=occ.moved_to_id).values_list("shown", flat=True).first()
        return MovedTo(occ.moved_to_id) if target_shown else Gone(id)
    return occ if occ.shown else Gone(id)


def find_occurrence(url_key: str, local_date: date) -> Occurrence | Gone | MovedTo:
    """The occurrence of a page URL (/evenements/<slug>-<date>/), by the slug's stable part."""
    occ = (
        Occurrence.objects.filter(event__url_key=url_key, local_date=local_date).order_by("id").values_list("id", flat=True).first()
    )
    if occ is None:
        raise NotFound(f"{url_key} {local_date}")
    return get_occurrence(occ)


def get_event(id: str) -> Event | Gone:
    """The event, with its published occurrences from today onwards in `future_occurrences`."""
    event = Event.objects.filter(id=id).first()
    if event is None:
        raise NotFound(id)
    if not event.shown:
        return Gone(id)
    event.future_occurrences = list(
        event.occurrences.filter(shown=True, end_utc_inclusive__gte=day_start(today())).order_by("start_utc", "id")
    )
    for occ in event.future_occurrences:
        occ.event = event
    return event


def sync_status() -> dict[str, Any]:
    """{"last_sync": the newest successful sync, "sources": [...]}, as aware datetimes.

    Lists the external sources that have run or contributed claims.
    """
    sources = []
    for s in Source.objects.filter(external=True).order_by("-priority", "name"):
        if s.last_run_at is None and not Claim.objects.filter(source=s).exists():
            continue
        sources.append({"name": s.label, "last_success": s.last_success_at, "last_run": s.last_run_at, "last_outcome": s.last_outcome})
    successes = [s["last_success"] for s in sources if s["last_success"]]
    return {"last_sync": max(successes) if successes else None, "sources": sources}


def area_communes() -> list[tuple[str, str]]:
    """(INSEE code, name) of the covered communes, by name."""
    return sorted(covered_communes().values_list("insee", "name"), key=lambda c: (c[1], c[0]))


def default_origin() -> str:
    """The commune the covered area is measured from (Nyons)."""
    info = MatrixInfo.objects.first()
    return info.origin_for_area if info else "26220"


# --- Serialisation (the API's occurrence and event objects) ------------------------------

def _url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


def _event_fields(event: Event) -> dict[str, Any]:
    return {
        "name": event.name,
        "description": event.description,
        "category": event.category,
        "location": dict(event.location),
        "organizer": event.organizer,
        "offers": event.offers,
        "sources": list(event.sources),
    }


def occurrence_dict(occ: Occurrence, base_url: str) -> dict[str, Any]:
    """The spec's occurrence object. `base_url` is the site's absolute root URL."""
    event = occ.event
    fields = _event_fields(event)
    return {
        "id": occ.id,
        "url": _url(base_url, occ.get_absolute_url()),
        "event_id": event.id,
        "name": fields["name"],
        "description": fields["description"],
        "start": occ.start,
        "end": occ.end,
        "timezone": "Europe/Paris",
        "status": occ.status,
        "previous_start": occ.previous_start,
        "category": fields["category"],
        "location": fields["location"],
        "organizer": fields["organizer"],
        "offers": fields["offers"],
        "sources": fields["sources"],
        # The occurrence object changes when either row does.
        "updated_at": format_local(max(occ.updated_at, event.updated_at)),
    }


def event_dict(event: Event, base_url: str) -> dict[str, Any]:
    """The spec's event object; `event` comes from get_event()."""
    return {
        "id": event.id,
        **_event_fields(event),
        "updated_at": format_local(event.updated_at),
        "occurrences": [occurrence_dict(o, base_url) for o in event.future_occurrences],
    }
