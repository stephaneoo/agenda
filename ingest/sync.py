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
import time
from collections import defaultdict
from datetime import datetime
from typing import Any

from django.conf import settings
from django.contrib.gis.db.models import Extent
from django.contrib.gis.geos import Point
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from agenda.models import Occurrence
from agenda.timeutil import PARIS, day_start
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
# A clean run withdrawing more than this says so in its summary (the MOTD shows it), so a
# slow drift under the threshold is seen.
DRIFT_WARNING = 0.05
# pg advisory lock, one sync at a time: the two-int4 form (class, object). Its key space never
# overlaps the one-bigint form that write_claims' per-event locks use
# (pg_advisory_xact_lock(hashtextextended(key, 0))), so no event can collide with it.
LOCK_CLASS = 0x41474E44  # "AGND"
LOCK_OBJECT = 1  # the DATAtourisme sync
# Degrees around the covered communes' extent, so a point on the border isn't cut off.
BBOX_MARGIN = 0.01
# Part of the payload hash: bump it when parse or mapping change what a row becomes, so the
# next sync rewrites every record instead of keeping them as unchanged.
PARSER_VERSION = 3  # 2: weekday from the name, category name rules; 3: date window and cap, bidi stripped, source_url
OUTCOME = {"clean": "ok", "partial": "partial", "failed": "failed"}


class AlreadyRunning(Exception):
    """Another sync holds the run lock."""


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


def _try_lock() -> bool:
    """One sync at a time: a session-level advisory lock, held until _unlock()."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [LOCK_CLASS, LOCK_OBJECT])
        return bool(cursor.fetchone()[0])


def _unlock() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [LOCK_CLASS, LOCK_OBJECT])


def skipped_last_partial() -> int | None:
    """The withdrawals the last partial run of this source skipped, or None if it skipped none:
    the count an operator must repeat to --allow-mass-withdrawal."""
    last = SyncRun.objects.filter(source=SOURCE, status="partial").order_by("-started_at").first()
    return last.skipped_withdrawals if last and last.skipped_withdrawals else None


def run(
    session: Any = None, queries: list[dict[str, str]] | None = None, *,
    allow_mass_withdrawal: int | None = None, deadline_seconds: float | None = None,
) -> SyncRun:
    """One sync. `session` and `queries` default to the live API and area_queries().

    `allow_mass_withdrawal` = N applies withdrawals past WITHDRAWAL_THRESHOLD, for this run
    only, when this run would withdraw exactly N future occurrences and fetched rows (an
    operator's decision after checking upstream: sync_datatourisme --allow-mass-withdrawal=N,
    N being what the last partial run skipped). Any other count: partial, nothing withdrawn.
    `deadline_seconds` (default settings.SYNC_DEADLINE_SECONDS): past it, the run fails.
    """
    started = timezone.now()
    budget = settings.SYNC_DEADLINE_SECONDS if deadline_seconds is None else deadline_seconds
    deadline = time.monotonic() + budget
    result = SyncRun(source=SOURCE, started_at=started, status="failed")
    locked = False
    try:
        if not _try_lock():
            raise AlreadyRunning("another sync is already running")
        locked = True
        result.save()
        queries = area_queries() if queries is None else queries
        if not queries:
            raise client.FetchError("no covered area loaded (manage.py load_geo)")
        _sync(result, session or client.make_session(), queries, started, allow_mass_withdrawal, deadline)
    except (client.FetchError, AlreadyRunning) as e:
        result.status = "failed"
        result.error = str(e)[:1000]
        logger.error("sync-datatourisme: failed, no withdrawals: %r", str(e))
    except Exception as e:
        result.status = "failed"
        result.error = f"{type(e).__name__}: {e}"[:1000]
        raise
    finally:
        _finish(result, started, record_run=locked)
        if locked:
            try:
                _unlock()
            except DatabaseError:
                logger.exception("sync-datatourisme: could not release the run lock")
    return result


def _store_record(record: SourceRecord | None, ext_id: str, payload: dict, digest: str, raw: dict, started: datetime) -> None:
    with transaction.atomic():
        if record is not None and record.payload_hash == digest:
            SourceRecord.objects.filter(pk=record.pk).update(last_seen_sync=started)
            return
        updated = parse.updated_at(raw)
        SourceRecord.objects.update_or_create(
            source_id=SOURCE, external_id=ext_id,
            defaults={
                "payload": payload, "payload_hash": digest, "fetched_at": started,
                "updated_at_source": day_start(updated) if updated else None, "last_seen_sync": started,
            },
        )


def _sync(
    result: SyncRun, session: Any, queries: list[dict[str, str]], started: datetime,
    allow_mass: int | None, deadline: float | None = None,
) -> None:
    covered = {c.insee: c for c in covered_communes()}
    today = started.astimezone(PARIS).date()  # the run's local date: the parser's date window
    before = _listed_future(started)
    records = {r.external_id: r for r in SourceRecord.objects.filter(source_id=SOURCE)}
    rows_seen: set[str] = set()
    present: set[str] = set()  # events the source still has in (or possibly in) the area
    written: dict[str, set[str]] = {}  # event key -> the occurrence keys it now lists

    version = client.source_version(session, deadline)
    for raw in client.iter_rows(session, queries, deadline=deadline):
        result.fetched_rows += 1
        key, ext_id = parse.event_key(raw), parse.external_id(raw)
        if key is None or ext_id is None:
            result.failed_rows += 1
            continue
        if key in rows_seen:  # the same row from another query
            continue
        rows_seen.add(key)
        p = parse.place(raw)  # an invalid point (missing, out of range, 0,0) is no point
        insee = resolve_commune(p.lat, p.lon, p.postal_code, p.commune_name)
        if insee is None and p.lat is not None and not _in_any_commune(p.lat, p.lon):
            # A point in no loaded commune (the area's neighbours are loaded). If the address
            # says a covered commune, point and address disagree: can't tell, not guessed.
            by_address = resolve_commune(None, None, p.postal_code, p.commune_name)
            if by_address not in covered:
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
        commune = covered[insee]
        try:
            key, claims = parse.row_to_claims(
                raw, commune=insee, commune_name=commune.name, seen_at=started,
                postal_codes=commune.postal_codes, today=today,
            )
        except parse.TooManyDates as e:
            # Refused, not merely unusable: its dates stay as they were, and the run says so.
            result.too_many_dates += 1
            logger.warning("sync-datatourisme: %r refused: %r", key, str(e))
            continue
        except parse.RowError as e:
            result.failed_rows += 1
            logger.warning("sync-datatourisme: %r unusable: %r", key, str(e))
            continue
        # Always written, even when the payload is unchanged: unchanged values are no-ops in
        # write_claims (no history, seen_at kept), and a date an earlier run withdrew, or one
        # a partial or failed run couldn't, gets its listed claim right again.
        # One bad row never stops the run: anything the database or the encoder refuses
        # (client.sanitise should leave nothing of the kind) is that row refused.
        try:
            write_claims(key, claims)
        except (ValueError, DatabaseError, UnicodeError) as e:
            result.refused_rows += 1
            logger.warning("sync-datatourisme: %r refused: %r", key, str(e))
            continue
        written[key] = {c.target_key for c in claims if c.target_type == "occurrence"}
        record = records.get(ext_id)
        try:
            payload = {k: v for k, v in raw.items() if k != "__id"}  # the API's row number
            digest = payload_hash(payload, insee)
            _store_record(record, ext_id, payload, digest, raw, started)
        except (DatabaseError, UnicodeError) as e:
            result.refused_rows += 1
            logger.warning("sync-datatourisme: %r raw record refused: %r", key, str(e))
            continue
        if record is not None and record.payload_hash == digest:
            result.unchanged += 1
        elif record is None:
            result.added += 1
        else:
            result.updated += 1

    if client.source_version(session, deadline) != version:
        raise client.FetchError("the export changed during the run (new version upstream)")
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
    result.status = "partial" if result.refused_rows or result.too_many_dates else "clean"
    if count and count > WITHDRAWAL_THRESHOLD * total:
        allowed = allow_mass is not None and allow_mass == count and result.fetched_rows > 0
        if not allowed:
            result.status = "partial"
            result.skipped_withdrawals = count
            if allow_mass is not None:
                result.warning = f"--allow-mass-withdrawal={allow_mass} refused: {count} to withdraw, {result.fetched_rows} rows fetched"
                logger.warning("sync-datatourisme: %s", result.warning)
            logger.warning("sync-datatourisme: %d of %d future occurrences would be withdrawn: skipped", count, total)
            return
    for event, keys in drops.items():
        try:
            write_claims(event, [NewClaim("occurrence", k, "listed", False, SOURCE, started) for k in keys])
        except ValueError as e:
            result.refused_rows += 1
            result.status = "partial"
            logger.warning("sync-datatourisme: withdrawing %r refused: %r", event, str(e))
            continue
        result.withdrawn += len(keys)
    if total and result.withdrawn > DRIFT_WARNING * total:
        result.warning = f"withdrew {round(100 * result.withdrawn / total)}% of {total} future occurrences"
        logger.warning("sync-datatourisme: %s", result.warning)


def _finish(result: SyncRun, started: datetime, *, record_run: bool) -> None:
    """Record the outcome: SyncRun, Source (unless another run holds the lock: Source's fields
    are its), then the status file, always, even when the database is what failed."""
    result.finished_at = timezone.now()
    outcome = OUTCOME[result.status]
    try:
        result.save()
        if record_run:
            fields: dict[str, Any] = {"last_run_at": started, "last_outcome": outcome}
            if outcome != "failed":
                fields["last_success_at"] = started
            Source.objects.filter(name=SOURCE).update(**fields)
    except DatabaseError:
        logger.exception("sync-datatourisme: could not record the run in the database")
    try:
        status.record(JOB, outcome, result.summary())
    except (OSError, ValueError, RecursionError):
        logger.exception("sync-datatourisme: could not write the status file")
