from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from claims.resolve import resolve

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 2, tzinfo=timezone.utc)


@dataclass
class C:
    field: str
    value: Any
    source_name: str
    priority: int
    seen_at: datetime


def test_highest_priority_wins() -> None:
    winners = resolve([
        C("name", "Marché", "datatourisme", 10, T1),
        C("name", "Marché de Nyons", "moderation", 100, T0),
        C("description", "Le jeudi", "datatourisme", 10, T0),
    ])
    assert winners["name"].value == "Marché de Nyons"
    assert winners["name"].source == "moderation"
    assert winners["description"].value == "Le jeudi"
    assert set(winners) == {"name", "description"}


def test_tie_broken_by_newest_seen_at() -> None:
    older = C("name", "Ancien", "a", 50, T0)
    newer = C("name", "Nouveau", "b", 50, T1)
    assert resolve([older, newer])["name"].value == "Nouveau"
    assert resolve([newer, older])["name"].value == "Nouveau"
    # A full tie is still deterministic, whatever the order.
    x, y = C("name", "X", "a", 50, T0), C("name", "Y", "b", 50, T0)
    assert resolve([x, y])["name"].value == resolve([y, x])["name"].value
