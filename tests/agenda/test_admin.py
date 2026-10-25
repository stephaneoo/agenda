import re
from datetime import date

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.db import connection

from agenda import admin as agenda_admin
from agenda.models import Event, Occurrence
from agenda.projection import Rebuild
from claims.keys import occurrence_key
from claims.models import Claim, ClaimHistory, Source
from tests.helpers import dates_value, import_event

KEY = "datatourisme:1"
NOV5 = date(2026, 11, 5)


@pytest.fixture
def staff(db, client):
    user = get_user_model().objects.create_superuser("modo", password="mot-de-passe-solide-1")
    client.force_login(user)
    return user


@pytest.fixture
def event(db) -> Event:
    import_event(KEY, dates=[dates_value(NOV5), dates_value(date(2026, 11, 12))])
    return Event.objects.get(key=KEY)


def _inputs(html: str) -> set[str]:
    return set(re.findall(r'<(?:input|select|textarea)[^>]*name="([^"]+)"', html))


def test_admin_changeform_has_no_writable_canonical_fields(staff, event, client) -> None:
    occ = Occurrence.objects.get(key=occurrence_key(KEY, NOV5))
    for model, obj in ((Event, event), (Occurrence, occ)):
        url = f"/admin/agenda/{model._meta.model_name}/{obj.pk}/change/"
        response = client.get(url)
        assert response.status_code == 200
        html = response.content.decode()
        fields = {f.name for f in model._meta.get_fields()} | {f.attname for f in model._meta.concrete_fields}
        assert not _inputs(html) & fields, _inputs(html) & fields
        # Saving is refused, and nothing changed.
        assert client.post(url, {"name": "Piraté"}).status_code == 403
        assert client.get(f"/admin/agenda/{model._meta.model_name}/add/").status_code == 403
        assert client.post(f"/admin/agenda/{model._meta.model_name}/{obj.pk}/delete/", {"post": "yes"}).status_code == 403
    assert Event.objects.get(key=KEY).name == "Marché de Nyons"
    # The claims panel: each field with its source and date.
    html = client.get(f"/admin/agenda/event/{event.pk}/change/").content.decode()
    assert "DATAtourisme" in html and "Marché de Nyons" in html and "2026" in html
    assert Claim not in admin.site._registry and ClaimHistory not in admin.site._registry


def test_moderation_forms_write_moderation_claims(staff, event, client) -> None:
    url = f"/admin/agenda/event/{event.pk}/moderer/"
    assert client.get(url).status_code == 200
    response = client.post(url, {"action": "hide", "reason": ""})
    assert response.status_code == 200 and "obligatoire" in response.content.decode()
    assert client.post(url, {"action": "hide", "reason": "Doublon"}).status_code == 302
    assert not Event.objects.get(key=KEY).shown
    assert client.post(url, {"action": "edit", "field": "category", "value": "inconnue"}).status_code == 200
    assert client.post(url, {"action": "edit", "field": "name", "value": "Marché du jeudi"}).status_code == 302
    assert Event.objects.get(key=KEY).name == "Marché du jeudi"
    occ = Occurrence.objects.get(key=occurrence_key(KEY, NOV5))
    occ_url = f"/admin/agenda/occurrence/{occ.pk}/moderer/"
    assert client.post(occ_url, {"action": "status", "status": "cancelled"}).status_code == 302
    assert client.post(occ_url, {"action": "move", "new_date": "2026-11-26", "new_time": ""}).status_code == 302
    assert Occurrence.objects.get(key=occurrence_key(KEY, date(2026, 11, 26))).status == "rescheduled"
    assert set(Claim.objects.filter(source="moderation").values_list("field", flat=True)) == {
        "visibility", "name", "status", "dates", "listed", "previous_start", "moved_to",
    }


def test_non_staff_cannot_access_admin(db, client, event) -> None:
    user = get_user_model().objects.create_user("lecteur", password="mot-de-passe-solide-1")
    client.force_login(user)
    for url in ("/admin/", f"/admin/agenda/event/{event.pk}/change/", f"/admin/agenda/event/{event.pk}/moderer/"):
        response = client.get(url)
        assert response.status_code == 302 and response["Location"].startswith("/admin/login/"), url
    response = client.post(f"/admin/agenda/event/{event.pk}/moderer/", {"action": "hide", "reason": "x"})
    assert response.status_code == 302 and Event.objects.get(key=KEY).shown


def test_repeated_failed_logins_are_refused(db, client) -> None:
    get_user_model().objects.create_superuser("modo", password="mot-de-passe-solide-1")
    for _ in range(5):
        response = client.post("/admin/login/?next=/admin/", {"username": "modo", "password": "faux"})
        assert response.status_code in (200, 429)
    # Locked out: even the right password is refused now.
    response = client.post("/admin/login/?next=/admin/", {"username": "modo", "password": "mot-de-passe-solide-1"})
    assert response.status_code == 429
    assert client.get("/admin/").status_code == 302


# Real commits, so on_commit runs; the seeded rows (sources) are restored afterwards.
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_source_edit_rebuilds_outside_the_admin_transaction(staff, event, client, monkeypatch) -> None:
    calls = []

    def fake_rebuild():
        calls.append(connection.in_atomic_block)
        return Rebuild(3, ["datatourisme:cassé"])

    monkeypatch.setattr(agenda_admin, "rebuild_all", fake_rebuild)
    source = Source.objects.get(name="datatourisme")
    response = client.post("/admin/claims/source/datatourisme/change/", {
        "label": "DATAtourisme", "priority": "20", "licence": source.licence, "external": "on",
    }, follow=True)
    assert response.status_code == 200
    assert Source.objects.get(name="datatourisme").priority == 20
    assert calls == [False]  # once, after the admin's transaction committed
    html = response.content.decode()
    assert "datatourisme:cassé" in html  # the failed events are shown
    assert client.get("/admin/claims/source/add/").status_code == 403
