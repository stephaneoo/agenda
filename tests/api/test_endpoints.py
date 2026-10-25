import json
from datetime import date

import pytest
from django.test import Client
from django.urls import get_resolver

from agenda import read
from agenda.models import Event, Occurrence
from claims.keys import occurrence_key
from claims.models import Source
from claims.services import NewClaim, write_claims
from tests.conformance import occurrence_problems
from tests.helpers import dates_value, import_event, location, moderate
from tools.check_openapi import check_openapi

pytestmark = pytest.mark.usefixtures("small_geo")
NOV = "from=2026-11-01&to=2026-11-30"


class CheckingClient(Client):
    """Every occurrence object the API returns must conform (schema plus the extra rules)."""

    def get(self, *args, **kwargs):
        response = super().get(*args, **kwargs)
        if response.status_code == 200 and response["Content-Type"].startswith("application/json"):
            for obj in _occurrence_objects(args[0] if args else kwargs["path"], json.loads(response.content)):
                assert occurrence_problems(obj) == [], obj
        return response


def _occurrence_objects(url: str, body):
    path = url.split("?")[0]
    if path == "/api/v1/occurrences":
        return body["items"]
    if path.startswith("/api/v1/occurrences/"):
        return [body]
    if path.startswith("/api/v1/events/"):
        return body["occurrences"]
    return []


@pytest.fixture
def client() -> Client:
    return CheckingClient()


@pytest.fixture
def events() -> None:
    import_event("datatourisme:marche", dates=[dates_value(date(2026, 11, d)) for d in (5, 12)])
    import_event(
        "datatourisme:concert", name="Concert rock", category="concert",
        description="Deux groupes locaux, buvette sur place.",
        loc=location(name="Salle des fêtes", street="Place de la Mairie", lat=44.3606, lon=5.1406),
        organizer={"name": "Comité des fêtes", "url": None},
        offers={"is_free": False, "price": "8 €", "url": "https://example.org/billets"},
        url="https://data.datatourisme.fr/abc",
        dates=[dates_value(date(2026, 11, 14), "21:00", None, "01:00")],
    )
    k = occurrence_key("datatourisme:concert", date(2026, 11, 14))
    moderate("datatourisme:concert", "occurrence", k, "status", "rescheduled")
    moderate("datatourisme:concert", "occurrence", k, "previous_start", "2026-11-07T21:00:00+01:00")
    import_event("datatourisme:expo", category="exposition", loc=location("84138", "Valréas", "84600"),
                 dates=[dates_value(date(2026, 10, 1), None, date(2026, 12, 31))])


def get(client: Client, url: str):
    response = client.get(url)
    return response, json.loads(response.content) if response.content else None


def test_occurrences_responses_validate_against_schema(client: Client, events: None) -> None:
    response, body = get(client, f"/api/v1/occurrences?{NOV}")
    assert response.status_code == 200
    assert response["Content-Type"] == "application/json; charset=utf-8"
    assert set(body) == {"items", "next"} and body["next"] is None
    assert len(body["items"]) == 4
    for item in body["items"]:
        assert occurrence_problems(item) == []
        _, single = get(client, f"/api/v1/occurrences/{item['id']}")
        assert single == item
    concert = next(i for i in body["items"] if i["name"] == "Concert rock")
    assert (concert["start"], concert["end"], concert["status"]) == (
        "2026-11-14T21:00:00+01:00", "2026-11-15T01:00:00+01:00", "rescheduled",
    )
    assert concert["url"].startswith("http://testserver/evenements/concert-rock-")
    _, event = get(client, f"/api/v1/events/{concert['event_id']}")
    for occ in event["occurrences"]:
        assert occurrence_problems(occ) == []


def test_pagination_and_filters(client: Client, events: None) -> None:
    _, first = get(client, f"/api/v1/occurrences?{NOV}&limit=3")
    assert len(first["items"]) == 3 and first["next"]
    _, second = get(client, f"/api/v1/occurrences?{NOV}&limit=3&cursor={first['next']}")
    assert len(second["items"]) == 1 and second["next"] is None
    _, cats = get(client, f"/api/v1/occurrences?{NOV}&category=concert&category=exposition")
    assert {i["category"] for i in cats["items"]} == {"concert", "exposition"}
    _, near = get(client, f"/api/v1/occurrences?{NOV}&commune=26220&within=10")
    assert {i["location"]["commune"] for i in near["items"]} == {"26220"}


@pytest.mark.parametrize("query", [
    "within=30",
    "commune=26220&within=61",
    "commune=75056&within=10",
    "from=2026-11-10&to=2026-11-01",
    "from=2026-11-01&to=2027-03-01",
    "limit=201",
    "within=trente&commune=26220",
    "from=2026-13-01",
    "from=2026-11-05T00:00:00",
    "to=20261105",
    "within=1.5&commune=26220",
    "category=inconnue",
    "cursor=nope",
])
def test_422_problem_json(client: Client, query: str) -> None:
    response, body = get(client, f"/api/v1/occurrences?{query}")
    assert response.status_code == 422
    assert response["Content-Type"] == "application/problem+json"
    assert body["status"] == 422 and body["title"] and body["detail"]


def test_get_occurrence_404_410_301(client: Client, events: None) -> None:
    response, body = get(client, "/api/v1/occurrences/occ_inconnu")
    assert (response.status_code, response["Content-Type"], body["status"]) == (404, "application/problem+json", 404)

    hidden = Occurrence.objects.get(key=occurrence_key("datatourisme:marche", date(2026, 11, 12)))
    moderate("datatourisme:marche", "occurrence", hidden.key, "visibility", {"value": "hidden", "reason": "erreur"})
    response, body = get(client, f"/api/v1/occurrences/{hidden.id}")
    assert (response.status_code, body["status"]) == (410, 410)
    withdrawn = occurrence_key("datatourisme:marche", date(2026, 11, 5))
    write_claims("datatourisme:marche", [NewClaim("occurrence", withdrawn, "listed", False, "datatourisme")])
    assert get(client, f"/api/v1/occurrences/{Occurrence.objects.get(key=withdrawn).id}")[0].status_code == 410

    old = Occurrence.objects.get(key=occurrence_key("datatourisme:concert", date(2026, 11, 14)))
    new_key = occurrence_key("datatourisme:concert", date(2026, 11, 21))
    write_claims("datatourisme:concert", [
        NewClaim("occurrence", new_key, "dates", dates_value(date(2026, 11, 21), "21:00"), "moderation"),
        NewClaim("occurrence", new_key, "listed", True, "moderation"),
        NewClaim("occurrence", new_key, "status", "rescheduled", "moderation"),
        NewClaim("occurrence", new_key, "previous_start", old.start, "moderation"),
        NewClaim("occurrence", old.key, "moved_to", new_key, "moderation"),
    ])
    new = Occurrence.objects.get(key=new_key)
    response = client.get(f"/api/v1/occurrences/{old.id}")
    assert response.status_code == 301
    assert response["Location"] == f"/api/v1/occurrences/{new.id}"


def test_event_includes_future_published_occurrences(client: Client, events: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(read, "today", lambda: date(2026, 11, 1))
    event = Event.objects.get(key="datatourisme:marche")
    moderate(event.key, "occurrence", occurrence_key(event.key, date(2026, 11, 12)), "visibility", {"value": "hidden", "reason": "x"})
    response, body = get(client, f"/api/v1/events/{event.id}")
    assert response.status_code == 200
    assert list(body) == ["id", "name", "description", "category", "location", "organizer", "offers", "sources",
                          "updated_at", "occurrences"]
    assert [o["start"] for o in body["occurrences"]] == ["2026-11-05"]
    assert get(client, "/api/v1/events/evt_inconnu")[0].status_code == 404
    moderate(event.key, "event", event.key, "visibility", {"value": "hidden", "reason": "x"})
    assert get(client, f"/api/v1/events/{event.id}")[0].status_code == 410


def test_communes_travel_times_categories_status_shapes(client: Client, events: None) -> None:
    _, communes = get(client, "/api/v1/communes")
    assert communes[0] == {"insee": "26182", "name": "Mirabel-aux-Baronnies", "lat": 44.311, "lon": 5.099}
    assert [c["insee"] for c in communes] == ["26182", "26220", "26377", "84138"]
    _, matrix = get(client, "/api/v1/travel-times")
    assert matrix["version"] == "test-small-1" and matrix["communes"] == ["26182", "26220", "26377", "84138"]
    assert matrix["minutes"][1][0] == 10 and matrix["minutes"][0][2] is None
    _, categories = get(client, "/api/v1/categories")
    assert len(categories) == 11 and categories[4] == {"slug": "marche", "label": "Marché"}
    _, status = get(client, "/api/v1/status")
    assert status == {"last_sync": None, "sources": [{"name": "DATAtourisme", "last_success": None}]}
    from datetime import datetime, timezone
    Source.objects.filter(name="datatourisme").update(
        last_success_at=datetime(2026, 10, 25, 4, 2, 11, tzinfo=timezone.utc),
        last_run_at=datetime(2026, 10, 25, 4, 2, 11, tzinfo=timezone.utc), last_outcome="ok")
    _, status = get(client, "/api/v1/status")
    assert status == {"last_sync": "2026-10-25T05:02:11+01:00",
                      "sources": [{"name": "DATAtourisme", "last_success": "2026-10-25T05:02:11+01:00"}]}


def test_openapi_served_at_v1(client: Client) -> None:
    response, spec = get(client, "/api/v1/openapi.json")
    assert response.status_code == 200
    assert spec["openapi"].startswith("3.1") and spec["info"]["version"] == "1.1.0"
    assert {"/api/v1/occurrences", "/api/v1/occurrences/{id}", "/api/v1/events/{id}", "/api/v1/communes",
            "/api/v1/travel-times", "/api/v1/categories", "/api/v1/status"} <= set(spec["paths"])
    assert check_openapi()


def _paths(resolver, prefix: str = "") -> list[str]:
    out = []
    for p in resolver.url_patterns:
        route = prefix + str(p.pattern)
        out += _paths(p, route) if hasattr(p, "url_patterns") else [route]
    return out


def test_ui_uses_no_private_endpoints() -> None:
    api_routes = [p for p in _paths(get_resolver()) if p.lstrip("^").startswith("api")]
    assert api_routes
    assert all(p.lstrip("^").startswith("api/v1/") for p in api_routes), api_routes


# --- Review fixes ------------------------------------------------------------------------

def test_unsafe_urls_never_reach_the_api(client: Client) -> None:
    import_event("datatourisme:x", url="javascript:alert(1)",
                 organizer={"name": "C", "url": "JaVaScRiPt:alert(1)"},
                 offers={"is_free": True, "price": None, "url": "data:text/html,x"})
    _, body = get(client, f"/api/v1/occurrences?{NOV}")
    (item,) = body["items"]
    assert item["organizer"]["url"] is None and item["offers"]["url"] is None and item["sources"][0]["url"] is None


def test_moved_chain_single_redirect_to_final(client: Client) -> None:
    key = "datatourisme:chain"
    days = [date(2026, 11, d) for d in (5, 12, 19)]
    import_event(key, dates=[dates_value(d, "10:00") for d in days])
    a, b, c = (occurrence_key(key, d) for d in days)
    moderate(key, "occurrence", a, "moved_to", b)
    moderate(key, "occurrence", b, "moved_to", c)
    ids = {k: Occurrence.objects.get(key=k).id for k in (a, b, c)}
    response = client.get(f"/api/v1/occurrences/{ids[a]}")
    assert (response.status_code, response["Location"]) == (301, f"/api/v1/occurrences/{ids[c]}")
    moderate(key, "occurrence", b, "moved_to", a)  # a cycle: both stay where they are
    assert client.get(f"/api/v1/occurrences/{ids[a]}").status_code == 200
    assert client.get(f"/api/v1/occurrences/{ids[b]}").status_code == 200
    moderate(key, "occurrence", b, "moved_to", c)
    moderate(key, "occurrence", c, "visibility", {"value": "hidden", "reason": "x"})
    assert client.get(f"/api/v1/occurrences/{ids[a]}").status_code == 410


@pytest.mark.parametrize("method, url, status", [
    ("get", "/api/v1/occurrences/", 404),
    ("get", "/api/v1/nope", 404),
    ("get", "/api/v1/", 404),
    ("post", "/api/v1/occurrences", 405),
    ("delete", "/api/v1/status", 405),
])
def test_every_api_error_is_problem_json(client: Client, method: str, url: str, status: int) -> None:
    response = getattr(client, method)(url)
    assert response.status_code == status
    assert response["Content-Type"] == "application/problem+json"
    body = json.loads(response.content)
    assert body["status"] == status and body["title"] and body["detail"]


def test_openapi_documents_errors_enums_and_ranges(client: Client) -> None:
    _, spec = get(client, "/api/v1/openapi.json")
    assert spec["info"]["version"] == "1.1.0"
    op = spec["paths"]["/api/v1/occurrences"]["get"]
    assert list(op["responses"]["422"]["content"]) == ["application/problem+json"]
    one_op = spec["paths"]["/api/v1/occurrences/{id}"]["get"]
    assert list(one_op["responses"]["404"]["content"]) == ["application/problem+json"]
    params = {p["name"]: p for p in op["parameters"]}
    within = json.dumps(params["within"]["schema"])
    assert '"minimum": 0' in within and '"maximum": 60' in within
    limit = params["limit"]["schema"]
    assert (limit["minimum"], limit["maximum"]) == (1, 200)
    assert "only" in params["commune"]["description"]
    assert params["from"]["schema"].get("pattern") or "pattern" in json.dumps(params["from"]["schema"])
    schemas = json.dumps(spec["components"]["schemas"])
    assert '"rescheduled"' in schemas and '"sport-balade"' in schemas
