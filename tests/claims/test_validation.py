"""write_claims refuses values the projection could not use (review M3a, minors 2 and 7)."""

from datetime import datetime, timedelta, timezone

import pytest

from claims import services
from claims.models import Claim, ClaimHistory
from claims.services import MAX_EVENT_KEY, NewClaim, write_claims

pytestmark = pytest.mark.django_db

KEY = "datatourisme:abc"
OCC = f"{KEY}/2026-11-05"
T0 = datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc)
GOOD_LOCATION = {"name": None, "street": None, "postal_code": "26110", "commune": "26220",
                 "commune_name": "Nyons", "lat": None, "lon": None}


@pytest.fixture(autouse=True)
def projected(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(services, "_project", calls.append)
    return calls


@pytest.mark.parametrize("target_type, key, field, value", [
    ("event", KEY, "location", {"postal_code": "26110", "commune": "26220"}),  # no commune_name
    ("event", KEY, "location", {**GOOD_LOCATION, "commune": ""}),
    ("event", KEY, "location", {**GOOD_LOCATION, "lat": "44"}),
    ("event", KEY, "category", "Marché"),
    ("event", KEY, "visibility", "hiden"),
    ("event", KEY, "visibility", {"value": "hiden", "reason": "x"}),
    ("event", KEY, "name", 12),
    ("event", KEY, "source_updated_at", "hier"),
    ("event", KEY, "colour", "red"),
    ("occurrence", OCC, "dates", {"start_date": "2026-11-06", "start_time": None, "end_date": None, "end_time": None}),
    ("occurrence", OCC, "dates", {"start_date": "2026-11-05", "start_time": "25:00", "end_date": None, "end_time": None}),
    ("occurrence", OCC, "dates", "2026-11-05"),
    ("occurrence", OCC, "status", "tentative"),
    ("occurrence", OCC, "previous_start", "2026-11-05T21:00:00"),  # no offset
    ("occurrence", OCC, "previous_start", "le 5"),
    ("occurrence", OCC, "moved_to", "2026-11-12"),
    ("occurrence", OCC, "moved_to", OCC),
    ("occurrence", OCC, "moved_to", "datatourisme:other/2026-11-12"),
    ("occurrence", OCC, "listed", "yes"),
], ids=lambda v: str(v)[:30])
def test_invalid_values_are_refused(target_type: str, key: str, field: str, value: object) -> None:
    with pytest.raises(ValueError):
        write_claims(KEY, [NewClaim(target_type, key, field, value, "moderation", T0)])
    assert not Claim.objects.exists()


@pytest.mark.parametrize("target_type, key, field, value", [
    ("event", KEY, "location", GOOD_LOCATION),
    ("event", KEY, "category", "marche"),
    ("event", KEY, "visibility", "hidden"),
    ("event", KEY, "visibility", {"value": "published"}),
    ("event", KEY, "description", None),
    ("event", KEY, "source_updated_at", "2026-10-24"),
    ("occurrence", OCC, "dates", {"start_date": "2026-11-05", "start_time": "21:00", "end_date": None, "end_time": "01:00"}),
    ("occurrence", OCC, "previous_start", "2026-11-01T21:00:00+01:00"),
    ("occurrence", OCC, "moved_to", f"{KEY}/2026-11-12"),
    ("occurrence", OCC, "listed", False),
], ids=lambda v: str(v)[:30])
def test_valid_values_are_accepted(target_type: str, key: str, field: str, value: object) -> None:
    write_claims(KEY, [NewClaim(target_type, key, field, value, "moderation", T0)])
    assert Claim.objects.get().event_key == KEY


def test_event_key_length_is_capped() -> None:
    long_key = "datatourisme:" + "x" * (MAX_EVENT_KEY - 12)
    with pytest.raises(ValueError):
        write_claims(long_key, [NewClaim("event", long_key, "name", "x", "datatourisme")])
    ok = long_key[:MAX_EVENT_KEY]
    write_claims(ok, [NewClaim("occurrence", f"{ok}/2026-11-05", "listed", True, "datatourisme")])
    assert len(Claim.objects.get().target_key) == MAX_EVENT_KEY + 11


def test_older_write_from_the_same_source_is_ignored(projected: list[str]) -> None:
    write_claims(KEY, [NewClaim("event", KEY, "name", "Nouveau", "datatourisme", T0)])
    write_claims(KEY, [NewClaim("event", KEY, "name", "Ancien", "datatourisme", T0 - timedelta(hours=1))])
    claim = Claim.objects.get()
    assert (claim.value, claim.seen_at) == ("Nouveau", T0)
    assert not ClaimHistory.objects.exists()
    assert projected == [KEY]


def test_values_compare_as_json_text(projected: list[str]) -> None:
    write_claims(KEY, [NewClaim("occurrence", OCC, "listed", True, "datatourisme", T0)])
    # 1 == True in Python, but not in JSON: a change, not a no-op (and then refused as not a bool).
    with pytest.raises(ValueError):
        write_claims(KEY, [NewClaim("occurrence", OCC, "listed", 1, "datatourisme", T0)])
    write_claims(KEY, [NewClaim("event", KEY, "source_url", None, "datatourisme", T0)])
    write_claims(KEY, [NewClaim("event", KEY, "description", "1", "datatourisme", T0)])
    write_claims(KEY, [NewClaim("event", KEY, "description", "1", "datatourisme", T0 + timedelta(days=1))])
    assert projected == [KEY, KEY, KEY]  # the repeated "1" was a no-op
    from claims.services import _same
    assert not _same(1, True) and not _same(1.0, 1) and _same({"a": 1, "b": 2}, {"b": 2, "a": 1})
