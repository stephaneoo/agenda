"""Builders shared by the agenda, api and web tests: claims as a source sync would write them."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from claims.keys import occurrence_key
from claims.services import NewClaim, write_claims

T0 = datetime(2026, 10, 24, 4, 0, tzinfo=timezone.utc)
NYONS = {
    "name": None, "street": None, "postal_code": "26110", "commune": "26220",
    "commune_name": "Nyons", "lat": None, "lon": None,
}


def location(commune: str = "26220", commune_name: str = "Nyons", postal_code: str = "26110", **extra: Any) -> dict:
    return {**NYONS, "commune": commune, "commune_name": commune_name, "postal_code": postal_code, **extra}


def dates_value(start: date, start_time: str | None = None, end: date | None = None, end_time: str | None = None) -> dict:
    return {
        "start_date": start.isoformat(), "start_time": start_time,
        "end_date": end.isoformat() if end else None, "end_time": end_time,
    }


def import_event(
    key: str = "datatourisme:1",
    *,
    name: str = "Marché de Nyons",
    dates: list[dict] | None = None,
    category: str = "marche",
    loc: dict | None = None,
    seen_at: datetime = T0,
    source: str = "datatourisme",
    updated_at: str = "2026-10-24T05:00:00+02:00",
    url: str | None = None,
    **fields: Any,
) -> None:
    """What a sync of one record writes: event fields, then dates and listed: true per occurrence."""
    dates = dates if dates is not None else [dates_value(date(2026, 11, 5))]
    values = {
        "name": name, "category": category, "location": loc or NYONS, "visibility": {"value": "published"},
        "source_updated_at": updated_at, "source_url": url, **fields,
    }
    claims = [NewClaim("event", key, f, v, source, seen_at) for f, v in values.items()]
    for d in dates:
        okey = occurrence_key(key, date.fromisoformat(d["start_date"]))
        claims += [
            NewClaim("occurrence", okey, "dates", d, source, seen_at),
            NewClaim("occurrence", okey, "listed", True, source, seen_at),
        ]
    write_claims(key, claims)


def moderate(key: str, target_type: str, target_key: str, field: str, value: Any, seen_at: datetime | None = None) -> None:
    write_claims(key, [NewClaim(target_type, target_key, field, value, "moderation", seen_at)])
