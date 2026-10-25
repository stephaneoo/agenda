"""The only way to write claims. Every write reprojects the event in the same transaction."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import connection, transaction
from django.utils import timezone

from claims.keys import event_key_of
from claims.models import Claim, ClaimHistory, Source


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


def _lock(event_key: str) -> None:
    """Serialise writers of one event until the transaction ends."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [event_key])


def _json(value: Any) -> Any:
    """The value as the JSON column will give it back, so that equal values compare equal."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as e:
        raise ValueError(f"claim value is not JSON: {e}") from None


def _check(event_key: str, claim: NewClaim, sources: set[str]) -> None:
    owner = event_key_of(claim.target_type, claim.target_key)
    if owner is None:
        raise ValueError(f"malformed target: {claim.target_type} {claim.target_key!r}")
    if owner != event_key:
        raise ValueError(f"target {claim.target_key!r} doesn't belong to event {event_key!r}")
    if claim.source not in sources:
        raise ValueError(f"unknown source {claim.source!r}")
    if not claim.field:
        raise ValueError("empty field name")


def write_claims(event_key: str, claims: list[NewClaim]) -> None:
    """Upsert claims of one event, archive replaced values, and reproject the event.

    A claim whose value is unchanged is left alone, seen_at included. All of it happens in
    one transaction: if the projection fails, nothing is written.
    """
    sources = set(Source.objects.values_list("name", flat=True))
    for claim in claims:
        _check(event_key, claim, sources)
    now = timezone.now()
    with transaction.atomic():
        _lock(event_key)
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
                    target_type=claim.target_type, target_key=claim.target_key, field=claim.field,
                    value=value, source_id=claim.source, seen_at=seen_at,
                )
            elif existing.value == value:
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
    with transaction.atomic():
        _lock(event_key)
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
