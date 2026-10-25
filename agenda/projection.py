"""The projection: canonical Event and Occurrence rows as a pure function of the claims.

Claim fields, by target:
- event: name, description, category, location, organizer, offers, visibility, resolved
  by priority; source_url and source_updated_at, kept per source for `sources`.
- occurrence ("<event key>/<YYYY-MM-DD>"): dates (agenda.periods.OccurrenceDates.to_claim),
  status, previous_start, moved_to (another occurrence key of the event) and visibility,
  resolved by priority; listed (true|false), kept per source.

`visibility` is {"value": "published"|"pending"|"hidden", "reason": str}, or the bare value.
With no visibility claim, an event or occurrence is published (post-moderation); an
invalid one counts as hidden. Other invalid claims (agenda.fields) are dropped before
resolution, so the next valid claim wins. URLs other than absolute http(s) become null.
`moved_to` chains are followed to their final target; a cycle of moves is ignored.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from django.db import transaction
from django.utils import timezone

from agenda.categories import DEFAULT, SLUGS
from agenda.fields import PER_SOURCE, STATUSES, local_value, problem, safe_url, visibility_of
from agenda.models import Event, Occurrence, projection_writer
from agenda.periods import OccurrenceDates
from agenda.timeutil import bounds, day_start, format_local, parse, to_local
from claims.keys import split_occurrence_key
from claims.models import Claim, Source
from claims.resolve import resolve
from claims.services import lock_event

logger = logging.getLogger(__name__)
LOCATION_KEYS = ("name", "street", "postal_code", "commune", "commune_name", "lat", "lon")


def _hash(text: str) -> bytes:
    return hashlib.sha256(text.encode()).digest()


def event_id(event_key: str) -> str:
    return "evt_" + base64.urlsafe_b64encode(_hash(event_key)[:9]).decode()


def occurrence_id(occurrence_key: str) -> str:
    """Opaque, stable and URL-safe: a hash of (event key, local date)."""
    return "occ_" + base64.urlsafe_b64encode(_hash(occurrence_key)[:9]).decode()


def url_key(event_key: str) -> str:
    return base64.b32encode(_hash(event_key)[:5]).decode().lower()


def slugify(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:60].strip("-") or "evenement"


# --- Values ------------------------------------------------------------------------------

def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _location(value: Any) -> dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    loc = {k: _text(value.get(k)) for k in ("name", "street", "postal_code", "commune", "commune_name")}
    loc["lat"], loc["lon"] = _number(value.get("lat")), _number(value.get("lon"))
    return {k: loc[k] for k in LOCATION_KEYS}


def _organizer(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not _text(value.get("name")):
        return None
    return {"name": _text(value["name"]), "url": safe_url(value.get("url"))}


def _offers(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    is_free = value.get("is_free")
    offers = {"is_free": is_free if isinstance(is_free, bool) else None,
              "price": _text(value.get("price")), "url": safe_url(value.get("url"))}
    return offers if any(v is not None for v in offers.values()) else None


def _visibility(winner: Any) -> str:
    """No claim: published. A claim with a value we don't know fails closed: hidden."""
    if winner is None:
        return "published"
    return visibility_of(winner.value) or "hidden"


def _local(value: Any) -> str | None:
    """A local date or date-time claim, normalised; None if it isn't one."""
    parsed = local_value(value)
    if parsed is None:
        return None
    return format_local(parsed) if isinstance(parsed, datetime) else parsed.isoformat()


def _before(end: str, start: str) -> bool:
    e, s = parse(end), parse(start)
    if isinstance(e, datetime) and isinstance(s, datetime):
        return e < s
    return (e.date() if isinstance(e, datetime) else e) < (s.date() if isinstance(s, datetime) else s)


def _winner(winners: dict, field: str) -> Any:
    w = winners.get(field)
    return w.value if w else None


# --- Projection --------------------------------------------------------------------------

def _claims_of(event_key: str) -> list[Claim]:
    """The event's usable claims. Invalid ones (e.g. stored before validation) are dropped,
    except visibility, which _visibility turns into hidden."""
    rows = Claim.objects.filter(event_key=event_key).select_related("source")
    return [
        c for c in rows
        if c.field == "visibility" or problem(c.target_type, c.target_key, c.field, c.value) is None
    ]


def _sources(contributors: set[str], claims: list[Claim], sources: dict[str, Source]) -> list[dict]:
    external = {c.source_id for c in claims if sources[c.source_id].external}
    cited = (contributors & external) or external
    meta: dict[str, dict[str, Any]] = defaultdict(dict)
    newest: dict[str, datetime] = {}
    for c in claims:
        if c.source_id in cited:
            newest[c.source_id] = max(newest.get(c.source_id, c.seen_at), c.seen_at)
            if c.target_type == "event" and c.field in ("source_url", "source_updated_at"):
                meta[c.source_id][c.field] = c.value
    out = []
    for name in sorted(cited, key=lambda n: (-sources[n].priority, n)):
        updated = _local(meta[name].get("source_updated_at"))
        if updated and len(updated) == 10:
            updated = format_local(day_start(parse(updated)))
        out.append({
            "name": sources[name].label,
            "url": safe_url(meta[name].get("source_url")),
            "licence": sources[name].licence or "",
            "updated_at": updated or format_local(newest[name]),
        })
    return out


def _occurrence_values(key: str, claims: list[Claim]) -> tuple[dict[str, Any], set[str], str | None]:
    """(column values, contributing sources, moved_to key) of one occurrence."""
    _, local_date = split_occurrence_key(key)
    winners = resolve(c for c in claims if c.field not in PER_SOURCE)
    dates = OccurrenceDates.from_claim(_winner(winners, "dates"))
    if dates is None or dates.start_date != local_date:
        dates = OccurrenceDates(local_date, None, None, None)
    start = to_local(dates.start_date, dates.start_time)
    end = to_local(dates.end_date, dates.end_time) if dates.end_date else None
    if end is not None and _before(end, start):
        end = None  # an end before the start isn't shown
    first, last = bounds(start, end)
    status = _winner(winners, "status")
    status = status if status in STATUSES else "scheduled"
    previous_start = _local(_winner(winners, "previous_start"))
    if status == "rescheduled" and previous_start is None:
        status = "scheduled"  # the API requires previous_start with rescheduled
    # A source that dropped the date only after it had ended doesn't withdraw it.
    # The cutoff is the end as the *current* dates give it: moving the time later on the
    # same day can turn an earlier drop into a withdrawal. Accepted for v1.
    listed = any(c.value is True or (c.value is False and c.seen_at > last) for c in claims if c.field == "listed")
    values = {
        "local_date": local_date, "start": start, "end": end, "status": status,
        "previous_start": previous_start, "listed": listed,
        "visibility": _visibility(winners.get("visibility")),
        "start_utc": first, "end_utc_inclusive": last,
    }
    contributors = {w.source for w in winners.values()} | {c.source_id for c in claims if c.field == "listed" and c.value is True}
    moved = _winner(winners, "moved_to")
    return values, contributors, moved if isinstance(moved, str) and moved != key else None


def _compute(event_key: str, claims: list[Claim]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    sources = {s.name: s for s in Source.objects.all()}
    by_target: dict[str, list[Claim]] = defaultdict(list)
    for c in claims:
        by_target[c.target_key].append(c)
    event_claims = by_target.pop(event_key, [])

    winners = resolve(c for c in event_claims if c.field not in PER_SOURCE)
    contributors = {w.source for w in winners.values()}
    occurrences: dict[str, dict[str, Any]] = {}
    moves: dict[str, str] = {}
    for key, occ_claims in by_target.items():
        values, occ_contributors, moved = _occurrence_values(key, occ_claims)
        occurrences[key] = values
        contributors |= occ_contributors
        if moved:
            moves[key] = moved

    name = _text(_winner(winners, "name")) or ""
    location = _location(_winner(winners, "location"))
    category = _winner(winners, "category")
    visibility = _visibility(winners.get("visibility"))
    cited = _sources(contributors, claims, sources)
    complete = bool(name and location["commune"] and location["postal_code"] and location["commune_name"] and cited)
    event = {
        "name": name,
        "description": _text(_winner(winners, "description")),
        "category": category if category in SLUGS else DEFAULT,
        "commune": location["commune"] or "",
        "location": location,
        "organizer": _organizer(_winner(winners, "organizer")),
        "offers": _offers(_winner(winners, "offers")),
        "visibility": visibility,
        "shown": visibility == "published" and complete,
        "sources": cited,
        "slug": f"{slugify(name)}-{url_key(event_key)}",
        "url_key": url_key(event_key),
    }
    finals = _final_targets(event_key, moves, set(occurrences))
    for key, values in occurrences.items():
        target = finals.get(key)
        values["moved_to_id"] = occurrence_id(target) if target else None
        values["shown"] = bool(
            event["shown"] and values["listed"] and values["visibility"] == "published" and not values["moved_to_id"]
        )
    return event, occurrences


def _final_targets(event_key: str, moves: dict[str, str], known: set[str]) -> dict[str, str]:
    """Each moved occurrence's final target. Moves to an unknown occurrence don't count, and
    every move in a cycle is ignored: its members stay where they are."""
    moves = {k: t for k, t in moves.items() if t in known}
    in_cycle: set[str] = set()
    for start in moves:
        seen: dict[str, int] = {}
        path: list[str] = []
        k = start
        while k in moves and k not in seen:
            seen[k] = len(path)
            path.append(k)
            k = moves[k]
        if k in seen:
            in_cycle |= set(path[seen[k]:])
    if in_cycle:
        logger.warning("projection: %s: moved_to cycle ignored: %s", event_key, ", ".join(sorted(in_cycle)))
    finals = {}
    for start in moves:
        if start in in_cycle:
            continue
        k = start
        while k in moves and k not in in_cycle:
            k = moves[k]
        finals[start] = k
    return finals


def _apply(row: Any, values: dict[str, Any], now: datetime) -> bool:
    changed = [k for k, v in values.items() if getattr(row, k) != v]
    for k in changed:
        setattr(row, k, values[k])
    if changed:
        row.updated_at = now
    return bool(changed)


def project_event(event_key: str) -> None:
    """Recompute the canonical rows of one event and its occurrences from its claims."""
    with transaction.atomic(), projection_writer():
        lock_event(event_key)
        _project(event_key, _claims_of(event_key), timezone.now())


def _project(event_key: str, claims: list[Claim], now: datetime) -> None:
    existing = Event.objects.filter(key=event_key).first()
    if not claims:
        if existing:
            existing.delete()
        return
    values, occurrences = _compute(event_key, claims)
    if existing is None:
        existing = Event(id=event_id(event_key), key=event_key, updated_at=now, **values)
        existing.save(force_insert=True)
    elif _apply(existing, values, now):
        existing.save()
    rows = {o.key: o for o in Occurrence.objects.filter(event=existing)}
    for key, occ_values in sorted(occurrences.items()):
        row = rows.pop(key, None)
        if row is None:
            Occurrence(id=occurrence_id(key), key=key, event=existing, updated_at=now, **occ_values).save(force_insert=True)
        elif _apply(row, occ_values, now):
            row.save()
    for row in rows.values():  # no claims left on it
        row.delete()


def rebuild_all() -> int:
    """Reproject every event from its claims, and drop canonical rows with no claims left.

    One transaction per event, under that event's lock, so the rebuild never holds more than
    one event at a time. Unchanged rows are left as they are, updated_at included. Run it
    after any change to the Source table (priorities, labels, licences). Returns the number
    of event keys with claims.
    """
    keys = set(Claim.objects.exclude(event_key="").values_list("event_key", flat=True).distinct())
    for key in sorted(keys):
        project_event(key)
    for stale in [k for k in Event.objects.values_list("key", flat=True) if k not in keys]:
        # project_event reads the claims again under the lock: if one arrived since, it stays.
        project_event(stale)
    return len(keys)
