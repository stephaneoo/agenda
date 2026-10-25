"""The only way to write claims. Every write reprojects the event in the same transaction.

Call write_claims once per event, each in its own transaction: never wrap many calls in one
outer transaction, which would hold every event's lock until it commits.

Exception contract: write_claims and delete_claim raise ValueError for anything wrong with
what they were given, including a value the database or the date arithmetic refuses
(DataError, OverflowError, chained as __cause__). That is the only exception a caller (a
sync, the admin) needs to catch per event; anything else is a bug.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.db import DataError, connection, transaction
from django.utils import timezone

from claims.keys import event_key_of, year_ok
from claims.models import Claim, ClaimHistory, Source


# Occurrence keys add 11 characters ("/YYYY-MM-DD"); Claim.target_key holds 250.
MAX_EVENT_KEY = 200
# A seen_at later than now plus this is refused: the newest seen_at wins ties and an older
# write from the same source is ignored, so a claim from the future would be frozen.
SEEN_AT_TOLERANCE = timedelta(minutes=5)


@dataclass(frozen=True)
class NewClaim:
    target_type: str  # "event" | "occurrence"
    target_key: str
    field: str
    value: Any  # JSON-serialisable
    source: str
    seen_at: datetime | None = None  # default: now


def _project(event_key: str) -> None:
    # Imported here: agenda depends on claims, not the other way round.
    from agenda.projection import project_event

    project_event(event_key)


def lock_event(event_key: str) -> None:
    """Serialise writers of one event until the transaction ends."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [event_key])


def _json(value: Any) -> Any:
    """The value as the JSON column will give it back."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, RecursionError) as e:
        raise ValueError(f"claim value is not JSON: {e}") from None


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _same(a: Any, b: Any) -> bool:
    """Equal as JSON text, so 1, 1.0 and true all differ (Python's == says 1 == True)."""
    return _canonical(a) == _canonical(b)


def _check(event_key: str, claim: NewClaim, sources: set[str], now: datetime) -> None:
    from agenda.fields import problem  # the field rules belong to the projection

    if not event_key or len(event_key) > MAX_EVENT_KEY:
        raise ValueError(f"event key must be 1 to {MAX_EVENT_KEY} characters")
    owner = event_key_of(claim.target_type, claim.target_key)
    if owner is None:
        raise ValueError(f"malformed target: {claim.target_type} {claim.target_key!r}")
    if owner != event_key:
        raise ValueError(f"target {claim.target_key!r} doesn't belong to event {event_key!r}")
    if claim.source not in sources:
        raise ValueError(f"unknown source {claim.source!r}")
    if not claim.field:
        raise ValueError("empty field name")
    if claim.seen_at is not None:
        if not isinstance(claim.seen_at, datetime) or claim.seen_at.utcoffset() is None:
            raise ValueError(f"{claim.target_key} {claim.field}: seen_at must be an aware datetime")
        if not year_ok(claim.seen_at):
            raise ValueError(f"{claim.target_key} {claim.field}: seen_at is out of range")
        if claim.seen_at > now + SEEN_AT_TOLERANCE:
            raise ValueError(f"{claim.target_key} {claim.field}: seen_at is in the future")
    if (why := problem(claim.target_type, claim.target_key, claim.field, _json(claim.value))) is not None:
        raise ValueError(f"{claim.target_key} {claim.field}: {why}")


def write_claims(event_key: str, claims: list[NewClaim]) -> None:
    """Upsert claims of one event, archive replaced values, and reproject the event.

    A claim whose value is unchanged is left alone, seen_at included, and so is a write
    older than what the same source already stated. Invalid values raise ValueError before
    anything is written, and so does a seen_at more than SEEN_AT_TOLERANCE in the future.
    All of it happens in one transaction: if the projection fails, nothing is written.
    """
    sources = set(Source.objects.values_list("name", flat=True))
    now = timezone.now()
    for claim in claims:
        _check(event_key, claim, sources, now)
    try:
        _write(event_key, claims, now)
    except (DataError, OverflowError) as e:
        raise ValueError(f"{event_key!r}: refused by the projection: {e}") from e


def _write(event_key: str, claims: list[NewClaim], now: datetime) -> None:
    with transaction.atomic():
        lock_event(event_key)
        changed = False
        for claim in claims:
            value = _json(claim.value)
            seen_at = claim.seen_at or now
            existing = (
                Claim.objects.select_for_update()
                .filter(target_type=claim.target_type, target_key=claim.target_key, field=claim.field, source_id=claim.source)
                .first()
            )
            if existing is None:
                Claim.objects.create(
                    target_type=claim.target_type, target_key=claim.target_key, event_key=event_key,
                    field=claim.field, value=value, source_id=claim.source, seen_at=seen_at,
                )
            elif _same(existing.value, value) or seen_at < existing.seen_at:
                continue
            else:
                _archive(existing, now, "replaced")
                existing.value, existing.seen_at = value, seen_at
                existing.save(update_fields=["value", "seen_at"])
            changed = True
        if changed:
            _project(event_key)


def delete_claim(target_type: str, target_key: str, field: str, source: str) -> None:
    """Delete one claim (e.g. a moderator's override), archive it, and reproject its event."""
    event_key = event_key_of(target_type, target_key)
    if event_key is None:
        raise ValueError(f"malformed target: {target_type} {target_key!r}")
    try:
        _delete(event_key, target_type, target_key, field, source)
    except (DataError, OverflowError) as e:
        raise ValueError(f"{event_key!r}: refused by the projection: {e}") from e


def _delete(event_key: str, target_type: str, target_key: str, field: str, source: str) -> None:
    with transaction.atomic():
        lock_event(event_key)
        claim = (
            Claim.objects.select_for_update()
            .filter(target_type=target_type, target_key=target_key, field=field, source_id=source)
            .first()
        )
        if claim is None:
            return
        _archive(claim, timezone.now(), "deleted")
        claim.delete()
        _project(event_key)


def _archive(claim: Claim, when: datetime, action: str) -> None:
    ClaimHistory.objects.create(
        target_type=claim.target_type, target_key=claim.target_key, field=claim.field,
        value=claim.value, source_id=claim.source_id, seen_at=claim.seen_at, replaced_at=when, action=action,
    )
