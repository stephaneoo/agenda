"""Batch C review: withdrawals that must heal, run lock, refused rows, operator override."""

import json
from datetime import timedelta
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.db import OperationalError, connections

from agenda import moderation, read
from agenda.models import Event, Occurrence
from geo.models import Commune
from geo.services import covered_communes
from ingest import sync
from tests.ingest.conftest import NOW, FakeSession, pages_of, row

pytestmark = pytest.mark.usefixtures("real_geo")
KEY = "datatourisme:00000000-0000-4000-8000-{:012d}"


def run(rows_or_pages, **kw):
    pages = rows_or_pages if rows_or_pages and isinstance(rows_or_pages[0], tuple) else pages_of(rows_or_pages)
    return sync.run(session=FakeSession(pages), queries=[{}], **kw)


def occ(n: int, day: str) -> Occurrence:
    return Occurrence.objects.get(key=f"{KEY.format(n)}/{day}")


def stable(n: int) -> list[dict]:
    return [row(1000 + i, "2026-11-20<->2026-11-20") for i in range(n)]


# --- B1: a withdrawal is never permanent ------------------------------------------------------

def test_row_missing_one_night_comes_back_unchanged_and_is_listed_again(clock) -> None:
    market = row(1, "2026-01-01<->2026-12-31", name="Grand marché")
    others = stable(20)
    run([market, *others])
    clock.now = NOW + timedelta(days=1)
    assert run(others).withdrawn == 1  # a glitch upstream: one of 21 missing
    assert not occ(1, "2026-01-01").shown
    clock.now = NOW + timedelta(days=2)
    result = run([market, *others])  # back, byte for byte
    assert result.unchanged == 21 and result.status == "clean"
    o = occ(1, "2026-01-01")
    assert o.listed and o.shown and isinstance(read.get_occurrence(o.id), Occurrence)


def test_date_dropped_during_a_partial_run_is_withdrawn_by_the_next_clean_one(clock) -> None:
    a = row(1, "2026-11-10<->2026-11-10|2026-11-17<->2026-11-17")
    others = [row(10 + i, "2026-11-12<->2026-11-12") for i in range(4)]
    run([a, *others, *stable(5)])
    clock.now = NOW + timedelta(days=1)
    a2 = row(1, "2026-11-10<->2026-11-10")  # drops 17 Nov
    assert run([a2, *stable(5)]).status == "partial"  # the 4 others missing too: 5 of 11
    clock.now = NOW + timedelta(days=2)
    result = run([a2, *others, *stable(5)])
    assert result.status == "clean" and result.withdrawn == 1 and result.unchanged == 10
    assert not occ(1, "2026-11-17").shown and occ(1, "2026-11-10").shown


def test_date_dropped_during_a_failed_run_is_withdrawn_by_the_next_clean_one(clock) -> None:
    rows = [row(i, "2026-11-10<->2026-11-10") for i in range(30)]
    rows[0] = row(0, "2026-11-10<->2026-11-10|2026-11-17<->2026-11-17")
    run(pages_of(rows, 25))
    clock.now = NOW + timedelta(days=1)
    rows2 = [row(0, "2026-11-10<->2026-11-10"), *rows[1:]]
    assert run([pages_of(rows2, 25)[0], (503, {"errors": []})]).status == "failed"
    clock.now = NOW + timedelta(days=2)
    result = run(pages_of(rows2, 25))
    assert result.status == "clean" and result.unchanged == 30 and result.withdrawn == 1
    assert not occ(0, "2026-11-17").shown


def test_edit_survives_sync_run(clock) -> None:
    run([row(1, name="Nom de la source"), *stable(5)])
    user = get_user_model().objects.create_user("m", is_staff=True)
    moderation.edit_field(Event.objects.get(key=KEY.format(1)), "name", "Nom corrigé", user)
    clock.now = NOW + timedelta(days=1)
    run([row(1, name="Nom changé", Description="Nouvelle description"), *stable(5)])
    event = Event.objects.get(key=KEY.format(1))
    assert (event.name, event.description) == ("Nom corrigé", "Nouvelle description")


# --- Communes ---------------------------------------------------------------------------------

def test_point_outside_every_commune_with_a_covered_address_is_unresolved_not_withdrawn(clock) -> None:
    run([row(1, "2026-11-10<->2026-11-10"), *stable(20)])
    clock.now = NOW + timedelta(days=1)
    swapped = row(1, "2026-11-10<->2026-11-10", lat=5.139799, lon=44.360433)  # address: 26110#Nyons
    zero = row(2, "2026-11-11<->2026-11-11", lat=0.0, lon=0.0, cp="26110#Nulle-Part")
    elsewhere = row(3, "2026-11-11<->2026-11-11", lat=43.0, lon=4.0, cp="13200#Arles")
    result = run([swapped, zero, elsewhere, *stable(20)])
    assert (result.unresolved_rows, result.out_of_area, result.withdrawn) == (2, 1, 0)
    assert occ(1, "2026-11-10").shown


def test_row_that_moves_to_a_non_covered_commune_is_withdrawn(clock) -> None:
    run([row(1, "2026-11-10<->2026-11-10"), *stable(20)])
    clock.now = NOW + timedelta(days=1)
    buis = Commune.objects.get(insee="26063").chef_lieu
    result = run([row(1, "2026-11-10<->2026-11-10", lat=buis.y, lon=buis.x, cp="26170#Buis-les-Baronnies"), *stable(20)])
    assert (result.out_of_area, result.withdrawn) == (1, 1)
    assert not occ(1, "2026-11-10").shown


def test_area_queries_cover_the_area_and_overlapping_queries_are_deduplicated(clock) -> None:
    queries = sync.area_queries()
    bbox, *postcodes = queries
    for c in covered_communes():
        assert float(bbox["Latitude__greater"]) < c.chef_lieu.y < float(bbox["Latitude__less"])
        assert float(bbox["Longitude__greater"]) < c.chef_lieu.x < float(bbox["Longitude__less"])
    expected = sorted({pc for c in covered_communes() for pc in c.postal_codes})
    assert [q["Code_postal_et_commune__contains"] for q in postcodes] == expected
    rows = [row(i, "2026-11-10<->2026-11-10") for i in range(5)]
    session = FakeSession(pages_of(rows))  # every query answers the same rows
    result = sync.run(session=session, queries=queries)
    assert result.fetched_rows == 5 * len(queries) and result.added == 5
    assert len(session.calls) == 2 * len(queries)  # page 1, then page 1 again to check


def test_a_server_capped_page_size_is_followed(clock) -> None:
    rows = [row(i, "2026-11-10<->2026-11-10") for i in range(60)]
    assert run(pages_of(rows, 25)).added == 60  # 200 asked, 25 served


# --- Run outcome --------------------------------------------------------------------------------

def test_second_run_while_one_holds_the_lock_fails_already_running(clock, status_file) -> None:
    other = connections.create_connection("default")
    try:
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s, %s)", [sync.LOCK_CLASS, sync.LOCK_OBJECT])
        result = run(stable(3))
        assert result.status == "failed" and "already running" in result.error
        assert Event.objects.count() == 0
        assert json.loads(status_file.read_text())["jobs"]["sync-datatourisme"]["outcome"] == "failed"
    finally:
        other.close()
    assert run(stable(3)).status == "clean"  # released: the next run goes ahead


def test_rows_refused_by_write_claims_make_the_run_partial(clock) -> None:
    real = sync.write_claims

    def refuse_one(key, claims):
        if key == KEY.format(1):
            raise ValueError("refused")
        return real(key, claims)

    with mock.patch.object(sync, "write_claims", refuse_one):
        result = run([row(1), *stable(3)])
    assert result.status == "partial" and result.refused_rows == 1 and result.withdrawn == 0
    assert "1 refused" in result.summary()


def test_database_down_at_start_still_records_failed(clock, status_file) -> None:
    with mock.patch.object(sync, "_try_lock", side_effect=OperationalError("connection refused")):
        with pytest.raises(OperationalError):
            run(stable(3))
    job = json.loads(status_file.read_text())["jobs"]["sync-datatourisme"]
    assert job["outcome"] == "failed" and "connection refused" in job["summary"]


def test_allow_mass_withdrawal_and_drift_warning(clock) -> None:
    rows = [row(i, "2026-11-10<->2026-11-10") for i in range(10)]
    run(rows)
    clock.now = NOW + timedelta(days=1)
    assert run(rows[:5]).status == "partial"
    result = run(rows[:5], allow_mass_withdrawal=5)
    assert result.status == "clean" and result.withdrawn == 5
    assert "withdrew 50%" in result.summary()
    clock.now = NOW + timedelta(days=2)
    many = [row(100 + i, "2026-11-10<->2026-11-10") for i in range(40)]
    run(rows[:5] + many)
    clock.now = NOW + timedelta(days=3)
    small = run(rows[:4] + many)  # 1 of 45 (2 %): no warning
    assert small.withdrawn == 1 and "withdrew" not in small.summary()


# --- M1: the export replaced during the run ---------------------------------------------------

class SwitchingSession(FakeSession):
    """Serves `v1` pages for the first `switch_after` page requests, then `v2`: the export
    replaced mid-run by one with the same total."""

    def __init__(self, v1, v2, switch_after: int) -> None:
        super().__init__(v1)
        self.v2, self.switch_after = v2, switch_after

    def get(self, url, params=None, **kwargs):
        if len(self.calls) >= self.switch_after:
            self.pages = self.v2
        return super().get(url, params, **kwargs)


def test_same_total_shift_mid_pagination_fails_and_withdraws_nothing(clock) -> None:
    v1 = [row(i, "2026-11-10<->2026-11-10") for i in range(30)]
    run(pages_of(v1, 25))
    clock.now = NOW + timedelta(days=1)
    v2 = v1[1:] + [row(99, "2026-11-10<->2026-11-10")]  # one deleted, one added: same total
    result = sync.run(session=SwitchingSession(pages_of(v1, 25), pages_of(v2, 25), 1), queries=[{}])
    assert result.status == "failed" and result.withdrawn == 0
    assert "changed during" in result.error
    assert all(o.shown for o in Occurrence.objects.all())


def test_export_version_changing_during_the_run_fails(clock) -> None:
    rows = [row(i, "2026-11-10<->2026-11-10") for i in range(10)]
    run(rows)
    clock.now = NOW + timedelta(days=1)
    moved = FakeSession(pages_of(rows[:9]), versions=["2026-10-25T03:47:44+00:00", "t1", "2026-10-26T03:40:00+00:00", "t1"])
    result = sync.run(session=moved, queries=[{}])
    assert result.status == "failed" and result.withdrawn == 0 and "changed during" in result.error
    assert all(o.shown for o in Occurrence.objects.all())
    same = FakeSession(pages_of(rows[:9]), versions=["v", "t", "v", "t"])
    assert sync.run(session=same, queries=[{}]).withdrawn == 1
