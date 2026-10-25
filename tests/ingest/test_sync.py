import json
from datetime import timedelta

import pytest
import requests

from agenda import read
from agenda.models import Event, Occurrence
from claims.models import Claim, ClaimHistory, Source
from django.contrib.gis.geos import Point

from geo.models import Commune
from geo.services import covered_communes
from ingest import sync
from ingest.datatourisme import client
from ingest.models import SyncRun
from tests.ingest.conftest import NOW, FakeSession, pages_of, recorded_error, recorded_pages, recorded_rows, row

pytestmark = pytest.mark.usefixtures("real_geo")

KEY = "datatourisme:00000000-0000-4000-8000-{:012d}"


def run(rows_or_pages, **kw) -> SyncRun:
    pages = rows_or_pages if rows_or_pages and isinstance(rows_or_pages[0], tuple) else pages_of(rows_or_pages)
    return sync.run(session=FakeSession(pages), queries=[{}], **kw)


def point(insee: str) -> dict:
    p = Commune.objects.get(insee=insee).chef_lieu
    return {"lat": p.y, "lon": p.x}


def listed(event_n: int, day: str) -> bool:
    return Occurrence.objects.get(key=f"{KEY.format(event_n)}/{day}").listed


def stable(n: int) -> list[dict]:
    """n events, one date each, that every later sync still lists."""
    return [row(1000 + i, "2026-11-20<->2026-11-20") for i in range(n)]


def test_rows_selected_by_commune_not_postcode(clock) -> None:
    covered = set(covered_communes().values_list("insee", flat=True))
    extra = [
        # Postcode 26110 (Nyons'), point in Buis-les-Baronnies, outside the area: not taken.
        row(1, cp="26110#Nyons", **point("26063")),
        # Postcode 26220 is Dieulefit's, and also Nyons' INSEE code: the point decides.
        row(2, cp="26220#Dieulefit", **point("26114")),
        # A postcode no covered commune has, the point in Nyons: taken, in Nyons.
        row(3, cp="26220#Dieulefit", **point("26220")),
    ]
    result = run(pages_of(recorded_rows() + extra))
    assert result.status == "clean"
    communes = dict(Event.objects.values_list("key", "commune"))
    assert set(communes.values()) <= covered
    assert KEY.format(1) not in communes and KEY.format(2) not in communes
    assert communes[KEY.format(3)] == "26220"
    marche = Event.objects.get(name="Marché nyonsais du jeudi")
    assert marche.commune == "26220" and marche.location["commune_name"] == "Nyons"
    # Every recorded row is in a covered commune (they are all 26110, around Nyons).
    assert result.added == len(recorded_rows()) + 1
    assert result.out_of_area == 2 and result.unresolved_rows == 0 and result.failed_rows == 0


def test_commune_resolution_edge_cases(clock) -> None:
    nyons = Commune.objects.get(insee="26220").geom
    lon, lat = nyons[0][0][0]  # a vertex of its boundary
    assert Commune.objects.filter(geom__intersects=Point(lon, lat, srid=4326)).count() > 1
    border = {"lat": lat, "lon": lon}
    rows = [
        # Enclave des Papes: Valréas is in Vaucluse, surrounded by Drôme. The point decides,
        # whatever the address says.
        row(1, cp="26110#Nyons", **point("84138")),
        row(2, cp="84600#Grillon", **point("84053")),
        # A neighbouring commune just outside the area: resolved, then left out, not guessed.
        row(3, cp="26170#Buis-les-Baronnies", **point("26063")),
        # No coordinates: postcode plus commune name.
        row(4, cp="26110#Mirabel-aux-Baronnies", lat=None, lon=None),
        # Coordinates that are no place (0, 0): same fallback.
        row(5, cp="84600#Valréas", lat=0.0, lon=0.0),
        # No coordinates and a name no commune has: skipped and counted.
        row(6, cp="26110#Nulle-Part", lat=None, lon=None),
        # A point in no loaded commune (the Gulf of Lion), address in Nyons: point and
        # address disagree, so it can't be told: skipped, counted, its dates kept.
        row(7, cp="26110#Nyons", lat=43.0, lon=4.0),
        # A postcode alone never decides.
        row(8, cp="26110", lat=None, lon=None),
        # On the border between Nyons and a neighbour: the address breaks the tie, or
        # nothing does.
        row(9, cp="26110#Nyons", **border),
        row(10, cp="26110#Nulle-Part", **border),
    ]
    result = run(rows)
    communes = dict(Event.objects.values_list("key", "commune"))
    assert communes == {
        KEY.format(1): "84138", KEY.format(2): "84053", KEY.format(4): "26182", KEY.format(5): "84138",
        KEY.format(9): "26220",
    }
    assert (result.unresolved_rows, result.out_of_area, result.added) == (4, 1, 5)


def test_unchanged_row_writes_no_claims(clock) -> None:
    first = run(recorded_pages_as_served())
    claims = dict(Claim.objects.values_list("id", "seen_at"))
    history = ClaimHistory.objects.count()
    clock.now = NOW + timedelta(days=1)
    # The next export renumbers rows (__id), which says nothing about the record.
    pages = recorded_pages_as_served()
    for _, body in pages:
        for r in body["data"]:
            r["__id"] += 7
    second = run(pages)
    assert (first.added, second.added, second.updated) == (len(recorded_rows()), 0, 0)
    assert second.unchanged == len(recorded_rows()) and second.status == "clean" and second.withdrawn == 0
    assert dict(Claim.objects.values_list("id", "seen_at")) == claims
    assert ClaimHistory.objects.count() == history


def recorded_pages_as_served() -> list[tuple[int, dict]]:
    return [(200, page) for page in recorded_pages()]


def test_clean_sync_withdraws_missing_future_dates(clock) -> None:
    a = row(1, "2026-10-20<->2026-10-20|2026-11-05<->2026-11-05|2026-11-12<->2026-11-12")
    b = row(2, "2026-11-07<->2026-11-07")
    run([a, b, *stable(20)])
    clock.now = NOW + timedelta(days=1)
    a2 = row(1, "2026-11-12<->2026-11-12")  # drops 20 Oct (over) and 5 Nov (to come)
    result = run([a2, *stable(20)])  # b is gone
    assert result.status == "clean"
    assert (result.withdrawn, result.skipped_withdrawals, result.updated) == (2, 0, 1)
    assert not listed(1, "2026-11-05") and not listed(2, "2026-11-07")
    assert listed(1, "2026-11-12") and listed(1, "2026-10-20")  # a past date is never withdrawn
    gone = Occurrence.objects.get(key=f"{KEY.format(2)}/2026-11-07").id
    assert isinstance(read.get_occurrence(gone), read.Gone)


def test_fetch_error_mid_pagination_withdraws_nothing(clock) -> None:
    rows = [row(i, "2026-11-0{}<->2026-11-0{}".format(1 + i % 8, 1 + i % 8)) for i in range(30)]
    run(pages_of(rows, 25))
    before = Source.objects.get(name="datatourisme").last_success_at
    clock.now = NOW + timedelta(days=1)
    changed = [row(0, "2026-11-01<->2026-11-01", name="Nouveau nom"), *rows[1:20]]  # rows 20-29 are on page 2
    error = recorded_error()
    pages = [pages_of(changed + rows[20:], 25)[0], (error["status"], error["body"])]
    result = run(pages)
    assert result.status == "failed" and result.withdrawn == 0
    assert Event.objects.get(key=KEY.format(0)).name == "Nouveau nom"  # updates already applied stay
    assert all(o.listed for o in Occurrence.objects.all())
    source = Source.objects.get(name="datatourisme")
    assert source.last_success_at == before and source.last_outcome == "failed"
    # A network error is a failed run too.
    assert run([pages[0], requests.ConnectionError("reset")]).status == "failed"
    assert all(o.listed for o in Occurrence.objects.all())


def test_over_20_percent_withdrawals_partial_run_applies_updates_skips_withdrawals(clock) -> None:
    rows = [row(i, "2026-11-10<->2026-11-10") for i in range(10)]
    run(rows)
    clock.now = NOW + timedelta(days=1)
    kept = [row(0, "2026-11-10<->2026-11-10", name="Mis à jour"), *rows[1:7]]  # 3 of 10 dropped: 30 %
    result = run(kept)
    assert result.status == "partial"
    assert (result.withdrawn, result.skipped_withdrawals, result.updated) == (0, 3, 1)
    assert all(o.listed for o in Occurrence.objects.all())
    assert Event.objects.get(key=KEY.format(0)).name == "Mis à jour"
    assert Source.objects.get(name="datatourisme").last_outcome == "partial"


def test_ended_events_not_counted_in_threshold(clock) -> None:
    future = [row(i, "2026-11-10<->2026-11-10") for i in range(10)]
    ended = [row(100 + i, "2026-09-01<->2026-09-01") for i in range(20)]
    run(future + ended)
    clock.now = NOW + timedelta(days=1)
    result = run(future[1:])  # the 20 ended events and one future one are gone
    assert result.status == "clean"
    assert (result.withdrawn, result.skipped_withdrawals) == (1, 0)
    assert not listed(0, "2026-11-10")
    assert all(listed(100 + i, "2026-09-01") for i in range(20))


def test_status_last_success_advances_on_clean_and_partial_only(clock, status_file) -> None:
    rows = [row(i, "2026-11-10<->2026-11-10") for i in range(10)]
    t1 = clock.now
    assert run(rows).status == "clean"
    source = Source.objects.get(name="datatourisme")
    assert (source.last_success_at, source.last_run_at, source.last_outcome) == (t1, t1, "ok")
    assert json.loads(status_file.read_text())["jobs"]["sync-datatourisme"]["outcome"] == "ok"

    clock.now = t2 = t1 + timedelta(days=1)
    assert run(rows[:5]).status == "partial"
    source.refresh_from_db()
    assert (source.last_success_at, source.last_outcome) == (t2, "partial")
    assert read.sync_status()["last_sync"] == t2

    clock.now = t3 = t2 + timedelta(days=1)
    assert run([(503, {"errors": []})]).status == "failed"
    source.refresh_from_db()
    assert (source.last_success_at, source.last_run_at, source.last_outcome) == (t2, t3, "failed")
    job = json.loads(status_file.read_text())["jobs"]["sync-datatourisme"]
    assert job["outcome"] == "failed" and job["last_success"] == t2.isoformat()
    assert SyncRun.objects.count() == 3


def test_iter_rows_refuses_a_total_that_moves_between_pages() -> None:
    rows = [row(i) for i in range(30)]
    first, second = pages_of(rows, 25)
    second[1]["meta"]["total"] = 31
    with pytest.raises(client.FetchError):
        list(client.iter_rows(FakeSession([first, second]), [{}]))
