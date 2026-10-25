from datetime import datetime, timezone

import pytest

from agenda.models import Event, Occurrence
from tests.helpers import NYONS, location
from tests.web.jsonld_check import jsonld_problems
from web.jsonld import ldjson_script, occurrence_jsonld

MINIMAL_JSONLD = {
    "@context": "https://schema.org",
    "@type": "Event",
    "name": "Marché de Nyons",
    "startDate": "2026-11-05",
    "eventStatus": "https://schema.org/EventScheduled",
    "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
    "location": {
        "@type": "Place",
        "name": "Nyons",
        "address": {"@type": "PostalAddress", "addressLocality": "Nyons", "postalCode": "26110", "addressCountry": "FR"},
    },
}
UPDATED = datetime(2026, 10, 25, 4, 2, 11, tzinfo=timezone.utc)


def build(status: str = "scheduled", **kw) -> Occurrence:
    """In memory, the minimal occurrence of the spec, or the full one's fields when given."""
    event = Event(
        id="evt_3aLm9x", key="datatourisme:x", slug="marche-de-nyons-abcdefgh", url_key="abcdefgh",
        name=kw.pop("name", "Marché de Nyons"), description=kw.pop("description", None), category="marche",
        commune="26220", location=kw.pop("location", dict(NYONS)), organizer=kw.pop("organizer", None),
        offers=kw.pop("offers", None), visibility="published", shown=True,
        sources=[{"name": "DATAtourisme", "url": None, "licence": "Licence Ouverte 2.0", "updated_at": "2026-10-24T05:00:00+02:00"}],
        updated_at=UPDATED,
    )
    return Occurrence(
        id="occ_7Hk2pQ", key="datatourisme:x/2026-11-05", event=event, start=kw.pop("start", "2026-11-05"),
        end=kw.pop("end", None), status=status, previous_start=kw.pop("previous_start", None), listed=True,
        visibility="published", shown=True, updated_at=UPDATED,
    )


def full() -> Occurrence:
    return build(
        "rescheduled", name="Concert rock", description="Deux groupes locaux, buvette sur place.",
        location=location(name="Salle des fêtes", street="Place de la Mairie", lat=44.3606, lon=5.1406),
        organizer={"name": "Comité des fêtes", "url": None},
        offers={"is_free": False, "price": "8 €", "url": "https://example.org/billets"},
        start="2026-11-14T21:00:00+01:00", end="2026-11-15T01:00:00+01:00", previous_start="2026-11-07T21:00:00+01:00",
    )


def test_minimal_jsonld_equals_spec_example() -> None:
    assert occurrence_jsonld(build()) == MINIMAL_JSONLD


@pytest.mark.parametrize("status, schema_status", [
    ("scheduled", "EventScheduled"), ("rescheduled", "EventRescheduled"),
    ("postponed", "EventPostponed"), ("cancelled", "EventCancelled"),
])
def test_jsonld_required_fields_and_status_mapping(status: str, schema_status: str) -> None:
    occ = build(status, previous_start="2026-11-01" if status == "rescheduled" else None)
    data = occurrence_jsonld(occ)
    assert data["eventStatus"] == f"https://schema.org/{schema_status}"
    assert jsonld_problems(data) == []


def test_previous_start_date_when_rescheduled() -> None:
    data = occurrence_jsonld(full())
    assert data["previousStartDate"] == "2026-11-07T21:00:00+01:00"
    assert (data["startDate"], data["endDate"]) == ("2026-11-14T21:00:00+01:00", "2026-11-15T01:00:00+01:00")
    assert "previousStartDate" not in occurrence_jsonld(build("scheduled", previous_start="2026-11-01"))


def test_full_jsonld_recommended_fields() -> None:
    data = occurrence_jsonld(full())
    assert data["description"] == "Deux groupes locaux, buvette sur place."
    assert data["organizer"] == {"@type": "Organization", "name": "Comité des fêtes"}
    assert data["offers"] == {"@type": "Offer", "url": "https://example.org/billets"}
    assert data["isAccessibleForFree"] is False
    assert data["location"]["name"] == "Salle des fêtes"
    assert data["location"]["address"]["streetAddress"] == "Place de la Mairie"
    free = occurrence_jsonld(build(offers={"is_free": True, "price": None, "url": None}))
    assert free["offers"] == {"@type": "Offer", "price": 0, "priceCurrency": "EUR"} and free["isAccessibleForFree"] is True


def test_no_image_in_jsonld() -> None:
    for occ in (build(), full()):
        assert "image" not in occurrence_jsonld(occ)


def test_ldjson_script_escapes_markup() -> None:
    html = ldjson_script({"name": "</script><script>alert(1)</script> & <!--"})
    assert html.count("</script>") == 1 and "<script>" not in html and "<!--" not in html
    assert html.startswith('<script type="application/ld+json">')
