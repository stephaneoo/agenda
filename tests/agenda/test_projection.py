import random
from datetime import date, datetime, timedelta, timezone

import pytest
from django.forms.models import model_to_dict

from agenda.categories import SLUGS
from agenda.models import CanonicalWriteError, Event, Occurrence, projection_writer
from agenda.projection import project_event, rebuild_all
from claims.keys import event_key_of, occurrence_key
from claims.models import Claim, claims_writer
from claims.services import NewClaim, delete_claim, write_claims
from tests.helpers import T0, dates_value, import_event, location, moderate

pytestmark = pytest.mark.django_db

KEY = "datatourisme:1"
D1, D2, D3 = date(2026, 11, 5), date(2026, 11, 12), date(2026, 11, 19)
T1 = T0 + timedelta(days=1)


def occ(d: date, key: str = KEY) -> Occurrence:
    return Occurrence.objects.get(key=occurrence_key(key, d))


def three_thursdays(**kw) -> None:
    import_event(KEY, dates=[dates_value(d, "08:00", d, "12:30") for d in (D1, D2, D3)], **kw)


def test_categories_are_the_spec_slugs() -> None:
    assert list(SLUGS) == [
        "concert", "spectacle", "cinema", "exposition", "marche", "brocante", "fete",
        "sport-balade", "atelier-conference", "loto-jeux", "autre",
    ]
    assert SLUGS["marche"] == "Marché"


def test_projection_of_an_import() -> None:
    three_thursdays()
    event = Event.objects.get()
    assert event.id.startswith("evt_") and event.key == KEY
    assert (event.name, event.category, event.commune, event.shown) == ("Marché de Nyons", "marche", "26220", True)
    o = occ(D1)
    assert o.id.startswith("occ_")
    assert (o.start, o.end, o.status, o.listed, o.shown) == (
        "2026-11-05T08:00:00+01:00", "2026-11-05T12:30:00+01:00", "scheduled", True, True,
    )
    assert o.start_utc == datetime(2026, 11, 5, 7, 0, tzinfo=timezone.utc)
    assert o.get_absolute_url() == f"/evenements/{event.slug}-2026-11-05/"
    assert event.slug.startswith("marche-de-nyons-")


def test_reimport_unchanged_keeps_ids_and_urls() -> None:
    three_thursdays()
    before = {o.key: (o.id, o.get_absolute_url(), o.updated_at) for o in Occurrence.objects.all()}
    event_before = Event.objects.values_list("id", "slug", "updated_at").get()
    three_thursdays(seen_at=T1)
    rebuild_all()
    assert {o.key: (o.id, o.get_absolute_url(), o.updated_at) for o in Occurrence.objects.all()} == before
    assert Event.objects.values_list("id", "slug", "updated_at").get() == event_before
    # A renamed event keeps its IDs.
    three_thursdays(seen_at=T1, name="Grand marché de Nyons")
    assert {o.key: o.id for o in Occurrence.objects.all()} == {k: v[0] for k, v in before.items()}


def test_cancel_one_occurrence_leaves_others() -> None:
    three_thursdays()
    others = {o.key: model_to_dict(o) | {"updated_at": o.updated_at} for o in (occ(D1), occ(D3))}
    moderate(KEY, "occurrence", occurrence_key(KEY, D2), "status", "cancelled")
    assert (occ(D2).status, occ(D2).shown) == ("cancelled", True)  # cancelled is still listed, as cancelled
    assert {o.key: model_to_dict(o) | {"updated_at": o.updated_at} for o in (occ(D1), occ(D3))} == others


def test_hide_survives_sync_of_changed_record() -> None:
    three_thursdays()
    moderate(KEY, "event", KEY, "visibility", {"value": "hidden", "reason": "doublon"})
    three_thursdays(seen_at=T1, name="Marché de Nyons (nouveau)")
    event = Event.objects.get()
    assert (event.name, event.visibility, event.shown) == ("Marché de Nyons (nouveau)", "hidden", False)
    assert not Occurrence.objects.filter(shown=True).exists()


def test_restore_deletes_moderation_claim_and_withdrawn_stays_withdrawn() -> None:
    three_thursdays()
    moderate(KEY, "event", KEY, "visibility", {"value": "hidden", "reason": "doublon"})
    # While hidden, the source stops listing D3 (a future date).
    write_claims(KEY, [NewClaim("occurrence", occurrence_key(KEY, D3), "listed", False, "datatourisme", T1)])
    delete_claim("event", KEY, "visibility", "moderation")
    assert not Claim.objects.filter(source="moderation").exists()
    assert Event.objects.get().shown is True
    assert (occ(D1).shown, occ(D2).shown) == (True, True)
    assert (occ(D3).listed, occ(D3).shown) == (False, False)


def test_future_date_dropped_is_withdrawn_past_never() -> None:
    past, future = date(2026, 10, 20), date(2026, 11, 20)
    import_event(KEY, dates=[dates_value(past, "10:00"), dates_value(future, "10:00")])
    sync = datetime(2026, 10, 25, 4, 0, tzinfo=timezone.utc)  # after `past`, before `future`
    write_claims(KEY, [
        NewClaim("occurrence", occurrence_key(KEY, d), "listed", False, "datatourisme", sync) for d in (past, future)
    ])
    assert (occ(future).listed, occ(future).shown) == (False, False)
    # Dropped after it happened: still listed, its page stays up.
    assert (occ(past).listed, occ(past).shown) == (True, True)


def test_moved_to_hides_old_and_new_carries_previous_start() -> None:
    three_thursdays()
    new = date(2026, 11, 26)
    new_key = occurrence_key(KEY, new)
    write_claims(KEY, [
        NewClaim("occurrence", new_key, "dates", dates_value(new, "08:00", new, "12:30"), "moderation"),
        NewClaim("occurrence", new_key, "listed", True, "moderation"),
        NewClaim("occurrence", new_key, "status", "rescheduled", "moderation"),
        NewClaim("occurrence", new_key, "previous_start", "2026-11-12T08:00:00+01:00", "moderation"),
        NewClaim("occurrence", occurrence_key(KEY, D2), "moved_to", new_key, "moderation"),
    ])
    old, moved = occ(D2), occ(new)
    assert (old.shown, old.moved_to_id) == (False, moved.id)
    assert old.listed is True  # the source still lists it; moved_to outranks that
    assert (moved.shown, moved.status, moved.previous_start) == (True, "rescheduled", "2026-11-12T08:00:00+01:00")


def test_moderation_date_claim_creates_occurrence() -> None:
    three_thursdays()
    extra = date(2026, 12, 3)
    k = occurrence_key(KEY, extra)
    write_claims(KEY, [
        NewClaim("occurrence", k, "dates", dates_value(extra, "09:00"), "moderation"),
        NewClaim("occurrence", k, "listed", True, "moderation"),
    ])
    o = occ(extra)
    assert (o.start, o.end, o.shown, o.event.key) == ("2026-12-03T09:00:00+01:00", None, True, KEY)


def test_rescheduled_without_previous_start_stays_scheduled() -> None:
    three_thursdays()
    moderate(KEY, "occurrence", occurrence_key(KEY, D1), "status", "rescheduled")
    assert occ(D1).status == "scheduled"  # the API forbids rescheduled with no previous_start


def test_invalid_values_fall_back() -> None:
    import_event(KEY, loc=location(lat=44.36, lon=5.14))
    with pytest.raises(ValueError):
        import_event(KEY, category="nope")
    with claims_writer():
        Claim.objects.filter(field="category").update(value="nope")  # a claim from before validation
    rebuild_all()
    e = Event.objects.get()
    assert e.category == "autre"
    assert e.location["lat"] == 44.36
    import_event("datatourisme:2", name="  ")
    assert Event.objects.get(key="datatourisme:2").shown is False  # a name is required


def test_direct_save_outside_projection_raises() -> None:
    three_thursdays()
    event, o = Event.objects.get(), occ(D1)
    event.name = "changé"
    with pytest.raises(CanonicalWriteError):
        event.save()
    with pytest.raises(CanonicalWriteError):
        o.delete()
    with pytest.raises(CanonicalWriteError):
        Occurrence.objects.update(status="cancelled")
    with pytest.raises(CanonicalWriteError):
        Occurrence.objects.filter(key=o.key).delete()
    with pytest.raises(CanonicalWriteError):
        Event.objects.create(id="evt_x", key="x", slug="x", name="x")
    with pytest.raises(CanonicalWriteError):
        Event.objects.bulk_create([Event(id="evt_y", key="y", slug="y", name="y")])
    with pytest.raises(CanonicalWriteError):
        Occurrence.objects.bulk_update([o], ["status"])
    assert Event.objects.get().name == "Marché de Nyons"
    with projection_writer():
        Occurrence.objects.filter(key=o.key).update(status="cancelled")  # the projection may
    assert occ(D1).status == "cancelled"


def test_sources_lists_external_only_with_record_update_date() -> None:
    three_thursdays(url="https://data.datatourisme.fr/abc", updated_at="2026-10-24T03:00:00Z")
    expected = [{
        "name": "DATAtourisme", "url": "https://data.datatourisme.fr/abc",
        "licence": "Licence Ouverte 2.0", "updated_at": "2026-10-24T05:00:00+02:00",
    }]
    assert Event.objects.get().sources == expected
    moderate(KEY, "event", KEY, "name", "Marché provençal")
    assert Event.objects.get().sources == expected  # moderation is never cited
    # Even when moderation wins every published field, the source that created it stays.
    for field, value in [("category", "marche"), ("location", location()), ("visibility", {"value": "published"})]:
        moderate(KEY, "event", KEY, field, value)
    for d in (D1, D2, D3):
        moderate(KEY, "occurrence", occurrence_key(KEY, d), "dates", dates_value(d))
    assert Event.objects.get().sources == expected


# --- Rebuild equals incremental ---------------------------------------------------------

def snapshot() -> list[dict]:
    rows = [model_to_dict(e, exclude=["updated_at"]) for e in Event.objects.order_by("key")]
    rows += [model_to_dict(o, exclude=["updated_at"]) | {"start_utc": o.start_utc, "end_utc_inclusive": o.end_utc_inclusive}
             for o in Occurrence.objects.order_by("key")]
    return rows


def random_claim(rng: random.Random, keys: list[str], days: list[date], times: list[datetime]) -> NewClaim:
    key = rng.choice(keys)
    source = rng.choice(["datatourisme", "datatourisme", "moderation", "rules", "direct"])
    seen = rng.choice(times)
    if rng.random() < 0.4:
        field, value = rng.choice([
            ("name", rng.choice(["Marché", "Concert", "Loto", ""])),
            ("category", rng.choice(["marche", "concert", "inconnue"])),
            ("description", rng.choice([None, "Texte"])),
            ("location", rng.choice([location(), location("84138", "Valréas", "84600"), {"commune": "26220"}])),
            ("visibility", rng.choice([{"value": "published"}, {"value": "hidden", "reason": "x"}, "pending"])),
            ("organizer", rng.choice([None, {"name": "Comité", "url": None}])),
            ("source_updated_at", rng.choice(["2026-10-24T05:00:00+02:00", "garbage"])),
        ])
        return NewClaim("event", key, field, value, source, seen)
    d = rng.choice(days)
    other = rng.choice(days)
    field, value = rng.choice([
        ("dates", dates_value(d, rng.choice([None, "21:00"]), rng.choice([None, d, d + timedelta(days=3)]), rng.choice([None, "01:00"]))),
        ("listed", rng.choice([True, False])),
        ("listed", True),
        ("status", rng.choice(["scheduled", "cancelled", "postponed", "rescheduled", "bogus"])),
        ("previous_start", "2026-11-01T10:00:00+01:00"),
        ("moved_to", occurrence_key(key, other)),
    ])
    return NewClaim("occurrence", occurrence_key(key, d), field, value, source, seen)


def test_rebuild_equals_incremental() -> None:
    rng = random.Random(20261025)
    keys = ["x:1", "x:12", "src:a/b", "src:a"]
    days = [date(2026, 10, 20), date(2026, 11, 5), date(2026, 11, 6), date(2026, 12, 24)]
    times = [T0, T0, T1, T0 + timedelta(days=30)]
    for case in range(200):
        for _ in range(rng.randint(3, 12)):
            claim = random_claim(rng, keys, days, times)
            existing = list(Claim.objects.all())
            key = event_key_of(claim.target_type, claim.target_key)
            if existing and rng.random() < 0.15:
                c = rng.choice(existing)
                delete_claim(c.target_type, c.target_key, c.field, c.source_id)
                continue
            try:
                write_claims(key, [claim])
            except ValueError:
                # Refused now, but such rows may predate validation: the projection must cope.
                with claims_writer():
                    Claim.objects.update_or_create(
                        target_type=claim.target_type, target_key=claim.target_key, field=claim.field,
                        source_id=claim.source, defaults={"value": claim.value, "seen_at": claim.seen_at, "event_key": key},
                    )
                project_event(key)
        incremental = snapshot()
        with projection_writer():
            Event.objects.all().delete()
        rebuild_all()
        assert snapshot() == incremental, f"case {case}"
        with claims_writer():
            Claim.objects.all().delete()
        rebuild_all()
        assert not Event.objects.exists() and not Occurrence.objects.exists()
