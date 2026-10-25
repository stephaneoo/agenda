"""The DATAtourisme sync (spec, DATAtourisme ingestion and Deletion safety).

1. Fetch the rows around the covered area: a bounding box of the covered communes, plus one
   query per covered postcode for rows whose coordinates are missing or wrong.
2. Resolve each row's commune (geo.services.resolve_commune: the point decides, else
   postcode plus name). Rows outside the covered area are left out, and so are points in
   no loaded commune (the area's neighbours are loaded). Rows that can't be decided (no
   usable point and no unique postcode-and-name match, or a point on a border the address
   doesn't settle) are skipped and counted, never guessed, and their dates stay as they were.
3. A row whose payload hash is unchanged writes nothing. Any other row is written as claims,
   one write_claims call (one transaction) per event, as the pages arrive: an update
   applied before a later page fails stays applied.
4. Only once every page has been fetched: withdraw (listed: false) the future occurrences
   the source no longer lists, unless that is more than WITHDRAWAL_THRESHOLD of the area's
   future occurrences, in which case the run is partial and withdraws nothing. An
   occurrence that has ended is never counted and never withdrawn.

Every claim of a run is seen at the run's start, the fetch time; upstream dates are never
used as seen_at. The outcome goes to Source (last_run_at, last_outcome, last_success_at, which
advances for clean and partial runs), to SyncRun, and to the status file.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any

from django.contrib.gis.db.models import Extent
from django.contrib.gis.geos import Point
from django.utils import timezone

from agenda.models import Occurrence
from agenda.timeutil import day_start
from agenda_site import status
from claims.models import Claim, Source, SourceRecord
from claims.services import NewClaim, write_claims
from geo.models import Commune
from geo.services import covered_communes, resolve_commune
from ingest.datatourisme import client, parse
from ingest.models import SyncRun

logger = logging.getLogger(__name__)

SOURCE = parse.SOURCE
JOB = "sync-datatourisme"
WITHDRAWAL_THRESHOLD = 0.20
# Degrees around the covered communes' extent, so a point on the border isn't cut off.
BBOX_MARGIN = 0.01
# Part of the payload hash: bump it when parse or mapping change what a row becomes, so the
# next sync rewrites every record instead of keeping them as unchanged.
PARSER_VERSION = 1
OUTCOME = {"clean": "ok", "partial": "partial", "failed": "failed"}


def area_queries() -> list[dict[str, str]]:
    """The tabular-API filters that cover the area. Empty when no area is loaded."""
    communes = list(covered_communes())
    if not communes:
        return []
    xmin, ymin, xmax, ymax = covered_communes().aggregate(e=Extent("geom"))["e"]
    m = BBOX_MARGIN
    bbox = {
        "Latitude__greater": f"{ymin - m:.5f}", "Latitude__less": f"{ymax + m:.5f}",
        "Longitude__greater": f"{xmin - m:.5f}", "Longitude__less": f"{xmax + m:.5f}",
    }
    postcodes = sorted({pc for c in communes for pc in c.postal_codes})
    # "<postcode>#<name>": a "#" in the filter matches nothing, and names have no digits.
    return [bbox] + [{"Code_postal_et_commune__contains": pc} for pc in postcodes]


def _in_any_commune(lat: float, lon: float) -> bool:
    return Commune.objects.filter(geom__intersects=Point(lon, lat, srid=4326)).exists()


def payload_hash(payload: dict[str, Any], commune: str) -> str:
    text = json.dumps({"parser": PARSER_VERSION, "commune": commune, "row": payload},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def _listed_future(now: datetime) -> dict[str, set[str]]:
    """Event key -> its occurrences this source lists that haven't ended at `now`."""
    listed = {
        target: event
        for event, target, value in Claim.objects.filter(
            source_id=SOURCE, target_type="occurrence", field="listed"
        ).values_list("event_key", "target_key", "value")
        if value is True
    }
    future = Occurrence.objects.filter(key__in=list(listed), end_utc_inclusive__gte=now).values_list("key", flat=True)
    out: dict[str, set[str]] = defaultdict(set)
    for key in future:
        out[listed[key]].add(key)
    return dict(out)


def run(session: Any = None, queries: list[dict[str, str]] | None = None) -> SyncRun:
    """One sync. `session` and `queries` default to the live API and area_queries()."""
    started = timezone.now()
    result = SyncRun.objects.create(source=SOURCE, started_at=started, status="failed")
    try:
        queries = area_queries() if queries is None else queries
        if not queries:
            raise client.FetchError("no covered area loaded (manage.py load_geo)")
        _sync(result, session or client.make_session(), queries, started)
    except client.FetchError as e:
        result.status = "failed"
        result.error = str(e)[:1000]
        logger.error("sync-datatourisme: fetch failed, no withdrawals: %r", str(e))
    except Exception as e:
        result.status = "failed"
        result.error = f"{type(e).__name__}: {e}"[:1000]
        raise
    finally:
        _finish(result, started)
    return result


def _sync(result: SyncRun, session: Any, queries: list[dict[str, str]], started: datetime) -> None:
    covered = {c.insee: c for c in covered_communes()}
    before = _listed_future(started)
    records = {r.external_id: r for r in SourceRecord.objects.filter(source_id=SOURCE)}
    rows_seen: set[str] = set()
    present: set[str] = set()  # events the source still has in (or possibly in) the area
    written: dict[str, set[str]] = {}  # event key -> the occurrence keys it now lists

    for raw in client.iter_rows(session, queries):
        result.fetched_rows += 1
        key, ext_id = parse.event_key(raw), parse.external_id(raw)
        if key is None or ext_id is None:
            result.failed_rows += 1
            continue
        if key in rows_seen:  # the same row from another query
            continue
        rows_seen.add(key)
        p = parse.place(raw)
        insee = resolve_commune(p.lat, p.lon, p.postal_code, p.commune_name)
        if insee is None and p.lat is not None and not _in_any_commune(p.lat, p.lon):
            # Every covered commune and its neighbours are loaded: a point in none of them
            # is outside the area.
            result.out_of_area += 1
            continue
        if insee is None:
            # Not guessed, and not taken as gone either: its dates stay as they were.
            result.unresolved_rows += 1
            present.add(key)
            continue
        if insee not in covered:
            result.out_of_area += 1
            continue
        present.add(key)
        payload = {k: v for k, v in raw.items() if k != "__id"}  # the API's row number
        digest = payload_hash(payload, insee)
        record = records.get(ext_id)
        if record is not None and record.payload_hash == digest:
            result.unchanged += 1
            SourceRecord.objects.filter(pk=record.pk).update(last_seen_sync=started)
            continue
        commune = covered[insee]
        try:
            key, claims = parse.row_to_claims(
                raw, commune=insee, commune_name=commune.name, seen_at=started, postal_codes=commune.postal_codes
            )
            write_claims(key, claims)
        except ValueError as e:  # parse.RowError, or refused by write_claims
            result.failed_rows += 1
            logger.warning("sync-datatourisme: %r skipped: %r", key, str(e))
            continue
        written[key] = {c.target_key for c in claims if c.target_type == "occurrence"}
        updated = parse.updated_at(raw)
        SourceRecord.objects.update_or_create(
            source_id=SOURCE, external_id=ext_id,
            defaults={
                "payload": payload, "payload_hash": digest, "fetched_at": started,
                "updated_at_source": day_start(updated) if updated else None, "last_seen_sync": started,
            },
        )
        if record is None:
            result.added += 1
        else:
            result.updated += 1

    # Every page came: the run is complete, and what's missing is gone at the source.
    drops: dict[str, list[str]] = {}
    for event, occurrences in before.items():
        if event in written:
            gone = occurrences - written[event]
        elif event in present:
            gone = set()
        else:
            gone = occurrences
        if gone:
            drops[event] = sorted(gone)
    total = sum(len(v) for v in before.values())
    count = sum(len(v) for v in drops.values())
    if count and count > WITHDRAWAL_THRESHOLD * total:
        result.status = "partial"
        result.skipped_withdrawals = count
        logger.warning("sync-datatourisme: %d of %d future occurrences would be withdrawn: skipped", count, total)
        return
    result.status = "clean"
    for event, keys in drops.items():
        try:
            write_claims(event, [NewClaim("occurrence", k, "listed", False, SOURCE, started) for k in keys])
        except ValueError as e:
            result.failed_rows += 1
            logger.warning("sync-datatourisme: withdrawing %r failed: %r", event, str(e))
            continue
        result.withdrawn += len(keys)


def _finish(result: SyncRun, started: datetime) -> None:
    result.finished_at = timezone.now()
    result.save()
    outcome = OUTCOME[result.status]
    fields: dict[str, Any] = {"last_run_at": started, "last_outcome": outcome}
    if outcome != "failed":
        fields["last_success_at"] = started
    Source.objects.filter(name=SOURCE).update(**fields)
    try:
        status.record(JOB, outcome, result.summary())
    except (OSError, ValueError):
        logger.exception("sync-datatourisme: could not write the status file")
