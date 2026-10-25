"""Batch C security review: the admin answers loopback only; hostile form input is a form error."""

from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from agenda.models import Event
from tests.helpers import dates_value, import_event

KEY = "datatourisme:1"


@pytest.fixture
def event(db) -> Event:
    import_event(KEY, dates=[dates_value(date(2026, 11, 5))])
    return Event.objects.get(key=KEY)


@pytest.fixture
def modo(db):
    return get_user_model().objects.create_superuser("modo", password="mot-de-passe-solide-1")


@pytest.mark.parametrize("remote", ["203.0.113.7", "", "10.0.0.1", "::ffff:127.0.0.1", "127.0.0.2"])
@pytest.mark.parametrize("path", ["/admin/", "/admin", "/admin/login/", "/admin/agenda/event/{pk}/moderer/"])
def test_admin_is_a_404_off_loopback(event, modo, remote, path) -> None:
    client = Client(REMOTE_ADDR=remote)
    client.force_login(modo)
    url = path.format(pk=event.pk)
    assert client.get(url).status_code == 404
    assert client.post(url, {"action": "hide", "reason": "x"}).status_code == 404
    assert Event.objects.get(key=KEY).visibility == "published"


@pytest.mark.parametrize("remote", ["127.0.0.1", "::1"])
def test_admin_works_from_loopback(event, modo, remote) -> None:
    client = Client(REMOTE_ADDR=remote)
    client.force_login(modo)
    assert client.get("/admin/").status_code == 200
    url = f"/admin/agenda/event/{event.pk}/moderer/"
    assert client.get(url).status_code == 200
    assert client.post(url, {"action": "hide", "reason": "Doublon"}).status_code == 302


def test_public_pages_are_not_affected_off_loopback(db) -> None:
    assert Client(REMOTE_ADDR="203.0.113.7").get("/a-propos/").status_code == 200


@pytest.mark.parametrize("depth", [5000, 900])
def test_deeply_nested_json_in_the_location_edit_is_a_form_error(event, modo, depth) -> None:
    client = Client()
    client.force_login(modo)
    url = f"/admin/agenda/event/{event.pk}/moderer/"
    response = client.post(url, {"action": "edit", "field": "location", "value": "[" * depth + "]" * depth})
    assert response.status_code == 200


def test_problem_is_total_over_deeply_nested_values() -> None:
    from agenda.fields import MAX_DEPTH, problem

    deep: list = []
    for _ in range(50_000):
        deep = [deep]
    assert "nested" in problem("event", KEY, "location", deep)
    assert "nested" in problem("event", KEY, "organizer", {"name": deep})
    shallow: list = []
    for _ in range(MAX_DEPTH - 1):
        shallow = [shallow]
    assert "nested" not in (problem("event", KEY, "location", shallow) or "")
