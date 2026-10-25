"""API and pages, security review L1 (malformed input), L4 (generic 410) and L7 (CORS)."""

from __future__ import annotations

import base64
import json
from datetime import date

import pytest
from django.test import Client

from agenda.models import Event, Occurrence
from claims.keys import occurrence_key
from claims.services import NewClaim, write_claims
from tests.helpers import dates_value, import_event, moderate

pytestmark = pytest.mark.usefixtures("small_geo")
KEY = "datatourisme:marche"
DAYS = [date(2026, 11, 5), date(2026, 11, 12), date(2026, 11, 19)]


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def marche() -> list[Occurrence]:
    import_event(KEY, dates=[dates_value(d, "08:00") for d in DAYS])
    return [Occurrence.objects.get(key=occurrence_key(KEY, d)) for d in DAYS]


def body(response) -> dict:
    return json.loads(response.content)


def cursor(payload: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


# --- L1: malformed input is a 404 or a 422, never a 500 ---------------------------------

@pytest.mark.parametrize("url", [
    "/api/v1/occurrences/occ_%00",
    "/api/v1/occurrences/occ_AAAAAAAAAA%00A",
    "/api/v1/events/evt_%00",
    "/api/v1/events/evt_AAAAAAAAAA%00A",
    "/api/v1/occurrences/evt_AAAAAAAAAAAA",  # an event id is no occurrence id
    "/api/v1/occurrences/" + "occ_" + "A" * 500,
])
def test_malformed_ids_are_404_problems(client: Client, url: str) -> None:
    response = client.get(url)
    assert (response.status_code, response["Content-Type"]) == (404, "application/problem+json")
    assert body(response)["status"] == 404


@pytest.mark.parametrize("query", [
    "commune=%00",
    "commune=26220%00",
    "commune=%00&within=10",
    "commune=" + "2" * 300,
    "cursor=" + cursor({"s": "2026-11-05T07:00:00+00:00", "i": "occ_\x00AAAAAAAAAAA"}),
    "cursor=" + cursor({"s": "2026-11-05T07:00:00+00:00", "i": "occ_AAAAAAAAAAA\x00"}),
    "cursor=" + cursor({"s": "2026-11-05T07:00:00+00:00", "i": "x" * 20}),
    "cursor=" + cursor({"s": "0001-01-01T00:00:00+23:59", "i": "occ_AAAAAAAAAAAA"}),
    "cursor=" + "A" * 513,
    "cursor=%00",
])
def test_malformed_query_is_422_problem(client: Client, query: str) -> None:
    response = client.get(f"/api/v1/occurrences?{query}")
    assert (response.status_code, response["Content-Type"]) == (422, "application/problem+json")
    assert body(response)["status"] == 422


def test_a_valid_cursor_still_pages(client: Client, marche: list[Occurrence]) -> None:
    first = body(client.get("/api/v1/occurrences?from=2026-11-01&to=2026-11-30&limit=1"))
    assert len(first["next"]) <= 512
    second = client.get(f"/api/v1/occurrences?from=2026-11-01&to=2026-11-30&limit=1&cursor={first['next']}")
    assert second.status_code == 200 and body(second)["items"][0]["id"] == marche[1].id


@pytest.mark.parametrize("url", ["/?commune=%00", "/?commune=26220%00&rayon=%00&categorie=%00&periode=%00"])
def test_agenda_page_ignores_malformed_filters(client: Client, url: str) -> None:
    assert client.get(url).status_code == 200


# --- L4: every 410 says the same thing ---------------------------------------------------

def test_api_410_bodies_dont_tell_hidden_from_withdrawn(client: Client, marche: list[Occurrence]) -> None:
    hidden, withdrawn, target = marche
    moderate(KEY, "occurrence", hidden.key, "visibility", {"value": "hidden", "reason": "doublon"})
    write_claims(KEY, [NewClaim("occurrence", withdrawn.key, "listed", False, "datatourisme")])
    responses = [client.get(f"/api/v1/occurrences/{o.id}") for o in (hidden, withdrawn)]
    moderate(KEY, "occurrence", target.key, "visibility", {"value": "pending"})
    responses.append(client.get(f"/api/v1/occurrences/{target.id}"))
    event = Event.objects.get()
    moderate(KEY, "event", KEY, "visibility", {"value": "hidden", "reason": "doublon"})
    responses.append(client.get(f"/api/v1/events/{event.id}"))
    assert {r.status_code for r in responses} == {410}
    bodies = {r.content for r in responses}
    assert len(bodies) == 1, bodies
    text = bodies.pop().decode().lower()
    for word in ("hidden", "withdrawn", "pending", "masqu", "retir", "doublon"):
        assert word not in text


def test_web_410_pages_dont_tell_hidden_from_withdrawn(client: Client, marche: list[Occurrence]) -> None:
    hidden, withdrawn, _ = marche
    moderate(KEY, "occurrence", hidden.key, "visibility", {"value": "hidden", "reason": "doublon"})
    write_claims(KEY, [NewClaim("occurrence", withdrawn.key, "listed", False, "datatourisme")])
    pages = [client.get(o.get_absolute_url()) for o in (hidden, withdrawn)]
    assert [p.status_code for p in pages] == [410, 410]
    assert pages[0].content == pages[1].content
    html = pages[0].content.decode()
    assert "Cette ressource n'est plus disponible" in html
    assert "retiré" not in html.lower() and "publié" not in html.lower()


# --- L7: CORS on the public read API -----------------------------------------------------

@pytest.mark.parametrize("method, url, status", [
    ("get", "/api/v1/categories", 200),
    ("head", "/api/v1/categories", 405),  # django-ninja routes GET only; the header is there all the same
    ("get", "/api/v1/occurrences?within=30", 422),
    ("get", "/api/v1/occurrences/occ_inconnu", 404),
    ("get", "/api/v1/nope", 404),
])
def test_api_get_and_head_allow_any_origin(client: Client, method: str, url: str, status: int) -> None:
    response = getattr(client, method)(url, HTTP_ORIGIN="https://other.example")
    assert response.status_code == status
    assert response["Access-Control-Allow-Origin"] == "*"
    assert not response.has_header("Access-Control-Allow-Credentials")


def test_api_preflight_is_204(client: Client) -> None:
    response = client.options("/api/v1/occurrences", HTTP_ORIGIN="https://other.example",
                              HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET")
    assert response.status_code == 204
    assert response["Access-Control-Allow-Origin"] == "*"
    assert response["Access-Control-Allow-Methods"] == "GET, HEAD, OPTIONS"
    assert int(response["Access-Control-Max-Age"]) > 0
    assert not response.has_header("Access-Control-Allow-Credentials")
    assert response.content == b""


def test_cors_is_for_the_api_only(client: Client) -> None:
    assert not client.get("/").has_header("Access-Control-Allow-Origin")
    assert not client.post("/api/v1/occurrences").has_header("Access-Control-Allow-Origin")
