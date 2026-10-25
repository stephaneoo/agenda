"""Claim and ClaimHistory are written only by claims.services (security review M2)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from django.db import IntegrityError, transaction

from claims import services
from claims.models import Claim, ClaimHistory, ClaimWriteError, claims_writer
from claims.services import NewClaim, delete_claim, moderation_delete, moderation_write, write_claims

pytestmark = pytest.mark.django_db

KEY = "datatourisme:abc"
OCC = f"{KEY}/2026-11-05"
T0 = datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def projected(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(services, "_project", calls.append)
    return calls


def claim_row(**kw) -> Claim:
    return Claim(**{"target_type": "event", "target_key": KEY, "event_key": KEY, "field": "name", "value": "x",
                    "source_id": "datatourisme", "seen_at": T0} | kw)


def test_every_write_outside_the_services_is_refused() -> None:
    write_claims(KEY, [NewClaim("event", KEY, "name", "Marché", "datatourisme", T0)])
    write_claims(KEY, [NewClaim("event", KEY, "name", "Marché de Nyons", "datatourisme")])
    claim, history = Claim.objects.get(), ClaimHistory.objects.get()
    claim.value = "forgé"
    attempts = [
        lambda: claim.save(),
        lambda: claim.save_base(),  # skips save(); the pre_save signal still fires
        lambda: claim.delete(),
        lambda: Claim.objects.create(**{f: getattr(claim, f) for f in ("target_type", "event_key", "field", "value", "seen_at")},
                                     target_key=KEY, source_id="moderation"),
        lambda: Claim.objects.update(value="forgé"),
        lambda: Claim.objects.filter(pk=claim.pk).delete(),
        lambda: Claim.objects.bulk_create([claim_row(source_id="moderation")]),
        lambda: Claim.objects.bulk_update([claim], ["value"]),
        lambda: Claim.objects.update_or_create(pk=claim.pk, defaults={"value": "forgé"}),
        lambda: history.save(),
        lambda: history.delete(),
        lambda: ClaimHistory.objects.all().delete(),
        lambda: ClaimHistory.objects.update(value="forgé"),
        lambda: claim.source.claim_set.all().delete(),  # related managers use the base manager
    ]
    for attempt in attempts:
        with pytest.raises(ClaimWriteError):
            attempt()
    assert Claim.objects.get().value == "Marché de Nyons"
    assert ClaimHistory.objects.get().value == "Marché"
    assert Claim._meta.base_manager_name == ClaimHistory._meta.base_manager_name == "objects"


def test_the_services_still_write_and_delete() -> None:
    write_claims(KEY, [NewClaim("occurrence", OCC, "status", "cancelled", "moderation", T0)])
    delete_claim("occurrence", OCC, "status", "moderation")
    assert not Claim.objects.exists()
    assert ClaimHistory.objects.get().action == "deleted"


def test_empty_event_key_is_refused_by_the_database() -> None:
    with claims_writer(), pytest.raises(IntegrityError), transaction.atomic():
        claim_row(event_key="").save()


def test_moderation_write_takes_moderation_claims_only(projected: list[str]) -> None:
    with pytest.raises(ValueError, match="source"):
        moderation_write(KEY, [NewClaim("event", KEY, "name", "forgé", "datatourisme", T0)])
    with pytest.raises(ValueError, match="source"):
        write_claims(KEY, [NewClaim("event", KEY, "name", "forgé", "rules", T0)], allowed_sources={"moderation"})
    assert not Claim.objects.exists()
    moderation_write(KEY, [NewClaim("event", KEY, "visibility", {"value": "hidden", "reason": "doublon"}, "moderation", T0)])
    assert Claim.objects.get().source_id == "moderation"


def test_moderation_delete_removes_moderation_claims_only() -> None:
    write_claims(KEY, [NewClaim("event", KEY, "name", "Marché", "datatourisme", T0),
                       NewClaim("event", KEY, "name", "Marché provençal", "moderation", T0)])
    moderation_delete("event", KEY, "name")
    assert list(Claim.objects.values_list("source_id", flat=True)) == ["datatourisme"]
    with pytest.raises(ValueError, match="source"):
        delete_claim("event", KEY, "name", "datatourisme", allowed_sources={"moderation"})
    assert Claim.objects.exists()
