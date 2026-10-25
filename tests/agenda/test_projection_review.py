"""Projection fixes from the batch B review."""

import json
from datetime import date, datetime, timezone

import pytest
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext

from agenda import projection
from agenda.models import CanonicalWriteError, Event, Occurrence
from agenda.projection import occurrence_id, project_event, rebuild_all
from claims.keys import occurrence_key
from claims.models import Claim
from claims.services import NewClaim, write_claims
from tests.helpers import T0, dates_value, import_event, location, moderate

pytestmark = pytest.mark.django_db

KEY = "datatourisme:1"
D = [date(2026, 11, d) for d in (5, 12, 19, 26)]


def okey(d: date, key: str = KEY) -> str:
    return occurrence_key(key, d)


def occ(d: date, key: str = KEY) -> Occurrence:
    return Occurrence.objects.get(key=okey(d, key))


def legacy(key: str, target_type: str, target_key: str, field: str, value: object, source: str = "moderation") -> None:
    """A claim written before validation existed, straight into the table, then reprojected."""
    Claim.objects.create(target_type=target_type, target_key=target_key, event_key=key, field=field,
                         value=value, source_id=source, seen_at=T0)
    project_event(key)


# --- M1: only absolute http(s) URLs ------------------------------------------------------

BAD_URLS = ["javascript:alert(1)", "JaVaScRiPt:alert(1)", " javascript:alert(1)", "data:text/html,<b>x</b>",
            "/relative", "relative/path", "//evil.example", "http://", "https:///nohost", "ftp://x.example",
            "http://exa mple.org", "vbscript:x"]


@pytest.mark.parametrize("url", BAD_URLS)
def test_unsafe_urls_become_null(url: str) -> None:
    import_event(KEY, url=url, organizer={"name": "Comité", "url": url}, offers={"is_free": None, "price": "5 €", "url": url})
    e = Event.objects.get()
    assert e.organizer == {"name": "Comité", "url": None}
    assert e.offers == {"is_free": None, "price": "5 €", "url": None}
    assert e.sources[0]["url"] is None


def test_http_urls_are_kept() -> None:
    import_event(KEY, url="HTTPS://Data.Datatourisme.fr/a?b=1", organizer={"name": "C", "url": "http://comite.example/"})
    e = Event.objects.get()
    assert e.sources[0]["url"] == "HTTPS://Data.Datatourisme.fr/a?b=1"
    assert e.organizer["url"] == "http://comite.example/"


# --- M2: moved chains and cycles ---------------------------------------------------------

def three_dates() -> None:
    import_event(KEY, dates=[dates_value(d, "10:00") for d in D[:3]])


def move(src: date, dst: date) -> None:
    moderate(KEY, "occurrence", okey(src), "moved_to", okey(dst))


def test_moved_chain_resolves_to_final_target() -> None:
    three_dates()
    move(D[0], D[1])
    move(D[1], D[2])
    assert occ(D[0]).moved_to_id == occ(D[2]).id
    assert occ(D[1]).moved_to_id == occ(D[2]).id
    assert (occ(D[0]).shown, occ(D[1]).shown, occ(D[2]).shown) == (False, False, True)


def test_moved_cycle_is_ignored(caplog: pytest.LogCaptureFixture) -> None:
    three_dates()
    move(D[0], D[1])
    move(D[1], D[2])
    move(D[2], D[1])  # D1 <-> D2: a cycle
    assert occ(D[1]).moved_to_id is None and occ(D[2]).moved_to_id is None
    assert occ(D[1]).shown and occ(D[2]).shown
    assert occ(D[0]).moved_to_id == occ(D[1]).id  # D0 leads into the cycle: its first member
    assert "cycle" in caplog.text


def test_move_to_unknown_occurrence_is_ignored() -> None:
    three_dates()
    move(D[0], D[3])  # no claims on D[3]
    assert occ(D[0]).moved_to_id is None and occ(D[0]).shown


# --- M3b/c: invalid claims never win ----------------------------------------------------

def test_partial_location_from_moderation_does_not_win() -> None:
    import_event(KEY)
    legacy(KEY, "event", KEY, "location", {"postal_code": "26110", "commune": "26220"})
    e = Event.objects.get()
    assert (e.location["commune_name"], e.shown) == ("Nyons", True)


def test_moderation_dates_for_another_day_does_not_win() -> None:
    import_event(KEY, dates=[dates_value(D[0], "10:00")])
    legacy(KEY, "occurrence", okey(D[0]), "dates", dates_value(D[1], "18:00"))
    assert occ(D[0]).start == "2026-11-05T10:00:00+01:00"


def test_category_label_instead_of_slug_does_not_win() -> None:
    import_event(KEY, category="concert")
    legacy(KEY, "event", KEY, "category", "Marché")
    assert Event.objects.get().category == "concert"


def test_unknown_visibility_hides() -> None:
    import_event(KEY)
    legacy(KEY, "event", KEY, "visibility", "hiden")
    e = Event.objects.get()
    assert (e.visibility, e.shown) == ("hidden", False)
    legacy(KEY, "occurrence", okey(date(2026, 11, 5)), "visibility", {"value": "masqué"})
    assert occ(date(2026, 11, 5)).visibility == "hidden"


# --- M4: rebuild one event per transaction ---------------------------------------------

@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_rebuild_holds_one_event_lock_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    for i in range(4):
        import_event(f"datatourisme:{i}")
    real = projection.project_event
    held: list[int] = []

    def spy(key: str) -> None:
        real(key)
        with connection.cursor() as c:
            c.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()")
            held.append(c.fetchone()[0])

    monkeypatch.setattr(projection, "project_event", spy)
    rebuild_all()
    assert held and max(held) <= 1, held
    assert not connection.in_atomic_block


def test_rebuild_drops_events_without_claims_and_is_idempotent() -> None:
    import_event("datatourisme:a")
    import_event("datatourisme:b")
    Claim.objects.filter(event_key="datatourisme:b").delete()
    rebuild_all()
    assert list(Event.objects.values_list("key", flat=True)) == ["datatourisme:a"]
    before = list(Occurrence.objects.values_list("id", "updated_at"))
    call_command("rebuild_projection")
    assert list(Occurrence.objects.values_list("id", "updated_at")) == before


# --- M5: claims by event_key, no LIKE ----------------------------------------------------

def test_projection_queries_claims_by_equality() -> None:
    import_event(KEY)
    with CaptureQueriesContext(connection) as ctx:
        project_event(KEY)
    assert not [q["sql"] for q in ctx.captured_queries if "LIKE" in q["sql"].upper()]


@pytest.mark.parametrize("a, b", [("x:1", "x:12"), ("src:a", "src:a/b"), ("src:a/b", "src:a/b/c")])
def test_prefix_colliding_keys_stay_distinct(a: str, b: str) -> None:
    for key, name in ((a, "Premier"), (b, "Second")):
        import_event(key, name=name, source="direct")
    assert Event.objects.get(key=a).name == "Premier" and Event.objects.get(key=b).name == "Second"
    assert {o.key for o in Event.objects.get(key=a).occurrences.all()} == {okey(date(2026, 11, 5), a)}
    assert {o.key for o in Event.objects.get(key=b).occurrences.all()} == {okey(date(2026, 11, 5), b)}


# --- Minor 4: no write path around the guard ------------------------------------------

def test_base_manager_and_signals_guard_writes(tmp_path) -> None:
    import_event(KEY)
    o = occ(date(2026, 11, 5))
    assert Occurrence._meta.base_manager_name == "objects" and Event._meta.base_manager_name == "objects"
    with pytest.raises(CanonicalWriteError):
        Occurrence._base_manager.filter(pk=o.pk).update(status="cancelled")
    with pytest.raises(CanonicalWriteError):
        o.save_base()
    with pytest.raises(CanonicalWriteError):
        Event.objects.get().save_base(raw=True)
    fixture = tmp_path / "canonical.json"
    fixture.write_text(json.dumps([{"model": "agenda.event", "pk": "evt_loaded", "fields": {
        "key": "loaded", "slug": "x-aaaaaaaa", "url_key": "aaaaaaaa", "name": "Chargé", "category": "autre",
        "commune": "", "location": {}, "visibility": "published", "shown": True, "sources": [],
        "updated_at": "2026-10-25T00:00:00Z"}}]))
    with pytest.raises(CanonicalWriteError):
        call_command("loaddata", str(fixture), verbosity=0)
    assert not Event.objects.filter(pk="evt_loaded").exists()
