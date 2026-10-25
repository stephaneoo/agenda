from datetime import datetime, timedelta, timezone
from datetime import timezone as dt_timezone

import pytest
from django.db import DataError
from django.utils import timezone as dj_timezone

from claims import services
from claims.models import Claim, ClaimHistory, Source
from claims.services import NewClaim, delete_claim, write_claims

pytestmark = pytest.mark.django_db

KEY = "datatourisme:abc"
T0 = datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def projected(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stands in for agenda.projection.project_event: records the keys it is called with."""
    calls: list[str] = []
    monkeypatch.setattr(services, "_project", calls.append)
    return calls


def name_claim(value: str, seen_at: datetime = T0) -> NewClaim:
    return NewClaim("event", KEY, "name", value, "datatourisme", seen_at)


def test_sources_are_seeded() -> None:
    assert {s.name: (s.priority, s.external) for s in Source.objects.all()} == {
        "moderation": (100, False),
        "rules": (60, False),
        "direct": (50, True),
        "datatourisme": (10, True),
    }
    assert Source.objects.get(name="datatourisme").licence == "Licence Ouverte 2.0"


def test_write_same_value_is_noop_no_history_row(projected: list[str]) -> None:
    write_claims(KEY, [name_claim("Marché de Nyons")])
    write_claims(KEY, [name_claim("Marché de Nyons", T1)])
    claim = Claim.objects.get()
    assert claim.value == "Marché de Nyons"
    assert claim.seen_at == T0  # untouched: a no-op must not change a tie-break
    assert ClaimHistory.objects.count() == 0
    assert projected == [KEY]  # nothing changed the second time, so nothing to project


def test_write_new_value_archives_old(projected: list[str]) -> None:
    write_claims(KEY, [name_claim("Marché")])
    write_claims(KEY, [name_claim("Marché de Nyons", T1)])
    claim = Claim.objects.get()
    assert (claim.value, claim.seen_at) == ("Marché de Nyons", T1)
    (old,) = ClaimHistory.objects.all()
    assert (old.target_key, old.field, old.source_id, old.value, old.seen_at, old.action) == (
        KEY, "name", "datatourisme", "Marché", T0, "replaced",
    )
    assert projected == [KEY, KEY]


def test_write_claims_calls_projection_in_same_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    write_claims(KEY, [name_claim("Marché")])

    def broken(event_key: str) -> None:
        assert Claim.objects.get(field="name").value == "Marché de Nyons"  # sees the new value
        raise RuntimeError("projection failed")

    monkeypatch.setattr(services, "_project", broken)
    with pytest.raises(RuntimeError):
        write_claims(KEY, [name_claim("Marché de Nyons", T1), NewClaim("event", KEY, "category", "marche", "datatourisme")])
    assert Claim.objects.get(field="name").value == "Marché"
    assert not Claim.objects.filter(field="category").exists()
    assert ClaimHistory.objects.count() == 0


def test_delete_claim_archives_and_reprojects(projected: list[str]) -> None:
    occ = f"{KEY}/2026-11-05"
    write_claims(KEY, [NewClaim("occurrence", occ, "status", "cancelled", "moderation")])
    delete_claim("occurrence", occ, "status", "moderation")
    assert not Claim.objects.exists()
    assert ClaimHistory.objects.get().action == "deleted"
    assert projected == [KEY, KEY]
    delete_claim("occurrence", occ, "status", "moderation")  # already gone: nothing to do
    assert projected == [KEY, KEY]


@pytest.mark.parametrize("claim", [
    NewClaim("event", "datatourisme:other", "name", "x", "datatourisme"),
    NewClaim("occurrence", KEY, "status", "cancelled", "moderation"),
    NewClaim("occurrence", f"{KEY}/2026-13-01", "status", "cancelled", "moderation"),
    NewClaim("occurrence", f"datatourisme:abcd/2026-11-05", "status", "cancelled", "moderation"),
    NewClaim("venue", KEY, "name", "x", "datatourisme"),
    NewClaim("event", KEY, "name", "x", "nobody"),
    NewClaim("event", KEY, "name", {1, 2}, "datatourisme"),
])
def test_write_claims_rejects_claims_outside_the_event(claim: NewClaim) -> None:
    with pytest.raises(ValueError):
        write_claims(KEY, [claim])
    assert not Claim.objects.exists()


# --- Security review H1d: ValueError is the only exception callers catch -----------------

@pytest.mark.parametrize("error", [DataError("value too long for type character varying(5)"), OverflowError("date value out of range")])
def test_write_claims_turns_database_and_overflow_errors_into_value_error(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def broken(event_key: str) -> None:
        raise error

    monkeypatch.setattr(services, "_project", broken)
    with pytest.raises(ValueError) as caught:
        write_claims(KEY, [name_claim("Marché")])
    assert caught.value.__cause__ is error
    assert not Claim.objects.exists()  # the transaction rolled back


@pytest.mark.parametrize("error", [DataError("invalid input"), OverflowError("date value out of range")])
def test_delete_claim_turns_database_and_overflow_errors_into_value_error(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    occ = f"{KEY}/2026-11-05"
    write_claims(KEY, [NewClaim("occurrence", occ, "status", "cancelled", "moderation")])

    def broken(event_key: str) -> None:
        raise error

    monkeypatch.setattr(services, "_project", broken)
    with pytest.raises(ValueError) as caught:
        delete_claim("occurrence", occ, "status", "moderation")
    assert caught.value.__cause__ is error
    assert Claim.objects.get().value == "cancelled"  # still there
    assert not ClaimHistory.objects.exists()


# --- Security review M1: a future seen_at would freeze the claim -------------------------

def test_seen_at_in_the_future_is_refused() -> None:
    with pytest.raises(ValueError, match="future"):
        write_claims(KEY, [name_claim("Marché", dj_timezone.now() + services.SEEN_AT_TOLERANCE + timedelta(seconds=30))])
    assert not Claim.objects.exists()


def test_seen_at_within_the_tolerance_is_accepted() -> None:
    seen = dj_timezone.now() + services.SEEN_AT_TOLERANCE - timedelta(seconds=30)
    write_claims(KEY, [name_claim("Marché", seen)])
    assert Claim.objects.get().seen_at == seen
    assert services.SEEN_AT_TOLERANCE == timedelta(minutes=5)


@pytest.mark.parametrize("seen_at", [datetime(2026, 10, 1, 4, 0), datetime(1, 1, 1, tzinfo=dt_timezone.utc), "2026-10-01"])
def test_seen_at_must_be_an_aware_datetime_in_range(seen_at: object) -> None:
    with pytest.raises(ValueError):
        write_claims(KEY, [name_claim("Marché", seen_at)])  # type: ignore[arg-type]
    assert not Claim.objects.exists()
