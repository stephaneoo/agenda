"""Claim resolution: for each field, which claim wins. Pure, no database."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


class ClaimLike(Protocol):
    field: str
    value: Any
    seen_at: datetime

    @property
    def source_name(self) -> str: ...

    @property
    def priority(self) -> int: ...


@dataclass(frozen=True)
class Winner:
    field: str
    value: Any
    source: str
    priority: int
    seen_at: datetime


def _rank(claim: ClaimLike) -> tuple[int, datetime, str]:
    # The source name only breaks a full tie, so the result never depends on input order.
    return (claim.priority, claim.seen_at, claim.source_name)


def resolve(claims: Iterable[ClaimLike]) -> dict[str, Winner]:
    """The winning claim of each field: the highest priority, then the newest seen_at."""
    best: dict[str, ClaimLike] = {}
    for claim in claims:
        current = best.get(claim.field)
        if current is None or _rank(claim) > _rank(current):
            best[claim.field] = claim
    return {
        f: Winner(field=f, value=c.value, source=c.source_name, priority=c.priority, seen_at=c.seen_at)
        for f, c in best.items()
    }
