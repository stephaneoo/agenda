from datetime import date, time, timedelta

import pytest
from django.contrib.auth import get_user_model

from agenda import moderation, read
from agenda.models import Event, Occurrence
from claims.keys import occurrence_key
from claims.models import Claim
from claims.services import NewClaim, write_claims
from tests.helpers import T0, dates_value, import_event

KEY = "datatourisme:1"
NOV5, NOV12, NOV19, NOV26 = (date(2026, 11, d) for d in (5, 12, 19, 26))


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user("modo", password="x", is_staff=True)


def occ(day: date, key: str = KEY) -> Occurrence:
    return Occurrence.objects.get(key=occurrence_key(key, day))


def test_hide_requires_reason(db, user) -> None:
    import_event(KEY)
    event = Event.objects.get(key=KEY)
    for reason in ("", "   ", None):
        with pytest.raises(ValueError):
            moderation.hide(event, reason, user)
    assert Event.objects.get(key=KEY).shown
    moderation.hide(event, "Doublon", user)
    event.refresh_from_db()
    assert not event.shown and event.visibility == "hidden"
    claim = Claim.objects.get(target_key=KEY, field="visibility", source="moderation")
    assert claim.value == {"value": "hidden", "reason": "Doublon"}
    # A single occurrence can be hidden too, and the others stay.
    import_event("datatourisme:2", dates=[dates_value(NOV5), dates_value(NOV12)])
    moderation.hide(occ(NOV5, "datatourisme:2"), "Annulé sur place", user)
    assert not occ(NOV5, "datatourisme:2").shown and occ(NOV12, "datatourisme:2").shown


def test_restore_removes_override_only(db, user) -> None:
    import_event(KEY, dates=[dates_value(NOV5), dates_value(NOV12)])
    event = Event.objects.get(key=KEY)
    moderation.hide(event, "Spam", user)
    moderation.hide(occ(NOV12), "Spam", user)
    with pytest.raises(ValueError):
        moderation.restore(event, user, "")  # the spec asks for a reason on restore too
    moderation.restore(event, user, "Erreur de modération")
    assert not Claim.objects.filter(target_key=KEY, field="visibility", source="moderation").exists()
    # The source's own claim is untouched, and the occurrence's hide is a separate override.
    assert Claim.objects.get(target_key=KEY, field="visibility", source="datatourisme").value == {"value": "published"}
    assert Event.objects.get(key=KEY).shown and occ(NOV5).shown and not occ(NOV12).shown
    # A restored occurrence that its source has since withdrawn stays withdrawn.
    write_claims(KEY, [NewClaim("occurrence", occurrence_key(KEY, NOV12), "listed", False, "datatourisme", T0 + timedelta(hours=1))])
    moderation.restore(occ(NOV12), user, "Erreur")
    assert not occ(NOV12).shown and not occ(NOV12).listed


def test_edit_field_survives_next_sync(db, user) -> None:
    import_event(KEY, name="Marche de Nyons", description="Avant")
    moderation.edit_field(Event.objects.get(key=KEY), "name", "Marché de Nyons", user)
    assert Event.objects.get(key=KEY).name == "Marché de Nyons"
    # The next sync brings a changed record: our edit stays, the other fields update.
    import_event(KEY, name="MARCHE DE NYONS", description="Après", seen_at=T0 + timedelta(days=1))
    event = Event.objects.get(key=KEY)
    assert (event.name, event.description) == ("Marché de Nyons", "Après")
    with pytest.raises(ValueError):
        moderation.edit_field(event, "category", "inconnue", user)
    with pytest.raises(ValueError):
        moderation.edit_field(event, "source_url", "https://example.org", user)  # not an editable field
    moderation.revert_field(event, "name", user)
    assert Event.objects.get(key=KEY).name == "MARCHE DE NYONS"


def test_set_status_cancelled_single_occurrence(db, user) -> None:
    import_event(KEY, dates=[dates_value(d) for d in (NOV5, NOV12, NOV19)])
    moderation.set_status(occ(NOV12), "cancelled", user)
    assert [occ(d).status for d in (NOV5, NOV12, NOV19)] == ["scheduled", "cancelled", "scheduled"]
    assert occ(NOV12).shown  # a cancelled occurrence is still listed, marked cancelled
    with pytest.raises(ValueError):
        moderation.set_status(occ(NOV5), "rescheduled", user)  # that one comes from move()
    moderation.set_status(occ(NOV12), "scheduled", user)  # back to what the source says
    assert occ(NOV12).status == "scheduled"
    assert not Claim.objects.filter(field="status").exists()


def test_move_to_unlisted_date_creates_and_redirects(db, user, client) -> None:
    import_event(KEY, dates=[dates_value(NOV5, "21:00", None, "23:00")])
    old = occ(NOV5)
    moderation.move(old, NOV26, None, user)
    new = occ(NOV26)
    assert (new.start, new.end) == ("2026-11-26T21:00:00+01:00", "2026-11-26T23:00:00+01:00")
    assert (new.status, new.previous_start, new.shown) == ("rescheduled", "2026-11-05T21:00:00+01:00", True)
    assert read.get_occurrence(old.id) == read.MovedTo(new.id)
    response = client.get(old.get_absolute_url())
    assert response.status_code == 301 and response["Location"] == new.get_absolute_url()
    # A new time moves the start; moving to the same date is refused.
    moderation.move(new, NOV19, time(20, 30), user)
    assert occ(NOV19).start == "2026-11-19T20:30:00+01:00"
    assert read.get_occurrence(old.id) == read.MovedTo(occ(NOV19).id)
    with pytest.raises(ValueError):
        moderation.move(occ(NOV19), NOV19, None, user)
