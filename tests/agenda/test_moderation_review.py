"""Batch C review: moves, restore without override, the Source admin, the moderators group."""

from datetime import date
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from agenda import moderation, read
from agenda.models import Event, Occurrence
from claims.keys import occurrence_key
from claims.models import Claim, Source
from tests.helpers import dates_value, import_event

KEY = "datatourisme:1"
NOV10, NOV24, NOV26 = date(2026, 11, 10), date(2026, 11, 24), date(2026, 11, 26)


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user("modo", password="x", is_staff=True)


def occ(day: date) -> Occurrence:
    return Occurrence.objects.get(key=occurrence_key(KEY, day))


def test_moving_an_already_moved_occurrence_is_refused_and_moving_its_target_leaves_no_ghost(db, user) -> None:
    import_event(KEY, dates=[dates_value(NOV10)])
    a = occ(NOV10)
    moderation.move(a, NOV24, None, user)
    a.refresh_from_db()
    with pytest.raises(ValueError) as e:
        moderation.move(a, NOV26, None, user)
    assert "24/11/2026" in str(e.value)  # points to the final target
    moderation.move(occ(NOV24), NOV26, None, user)
    b, c = occ(NOV24), occ(NOV26)
    assert not b.shown and c.shown and c.previous_start == "2026-11-10"
    assert read.get_occurrence(a.id) == read.get_occurrence(b.id) == read.MovedTo(c.id)
    # The abandoned target keeps only its redirect.
    left = set(Claim.objects.filter(target_key=b.key, source="moderation").values_list("field", flat=True))
    assert left == {"moved_to"}
    assert [o.key for o in Occurrence.objects.filter(shown=True)] == [c.key]


def test_a_move_is_one_transaction(db, user) -> None:
    import_event(KEY, dates=[dates_value(NOV10)])
    moderation.move(occ(NOV10), NOV24, None, user)
    before = sorted(Claim.objects.values_list("target_key", "field", "source_id"))
    with mock.patch.object(moderation, "moderation_write", side_effect=ValueError("refused")):
        with pytest.raises(ValueError):
            moderation.move(occ(NOV24), NOV26, None, user)
    assert sorted(Claim.objects.values_list("target_key", "field", "source_id")) == before  # nothing deleted


def test_restore_without_override_is_an_error(db, user) -> None:
    import_event(KEY)
    with pytest.raises(ValueError) as e:
        moderation.restore(Event.objects.get(key=KEY), user, "pour voir")
    assert "rien à rétablir" in str(e.value)


@pytest.fixture
def superuser_client(db, client):
    client.force_login(get_user_model().objects.create_superuser("admin", password="mot-de-passe-solide-1"))
    return client


def test_moderation_source_priority_and_external_are_read_only(superuser_client) -> None:
    html = superuser_client.get("/admin/claims/source/moderation/change/").content.decode()
    assert 'name="priority"' not in html and 'name="external"' not in html
    # No other source may reach moderation's priority.
    response = superuser_client.post("/admin/claims/source/datatourisme/change/", {
        "label": "DATAtourisme", "priority": "100", "licence": "Licence Ouverte 2.0", "external": "on",
    })
    assert response.status_code == 200 and "moderation" in response.content.decode()
    assert Source.objects.get(name="datatourisme").priority == 10


def test_moderators_group_can_moderate_and_staff_without_permissions_cannot(db, client) -> None:
    import_event(KEY)
    event = Event.objects.get(key=KEY)
    url = f"/admin/agenda/event/{event.pk}/moderer/"
    group = Group.objects.get(name="Modérateurs")
    perms = set(group.permissions.values_list("content_type__app_label", "codename"))
    assert perms == {
        ("agenda", "view_event"), ("agenda", "change_event"), ("agenda", "view_occurrence"),
        ("agenda", "change_occurrence"), ("claims", "view_source"), ("ingest", "view_syncrun"),
    }
    users = get_user_model().objects
    modo = users.create_user("modo", password="mot-de-passe-solide-1", is_staff=True)
    modo.groups.add(group)
    client.force_login(modo)
    assert client.get(url).status_code == 200
    assert client.post(url, {"action": "hide", "reason": "Doublon"}).status_code == 302
    assert not Event.objects.get(key=KEY).shown
    assert client.get("/admin/auth/user/").status_code == 403
    client.force_login(users.create_user("staff", password="mot-de-passe-solide-1", is_staff=True))
    assert client.get(url).status_code == 403
    assert client.post(url, {"action": "restore", "reason": "x"}).status_code == 403
    assert not Event.objects.get(key=KEY).shown
