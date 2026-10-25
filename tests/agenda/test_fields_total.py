"""agenda.fields.problem() is total over the projection (security review H1).

A seeded random generator (hypothesis isn't a dependency) produces values for every claim
field, valid ones and near misses around each bound. Every value problem() accepts must go
through write_claims and the real projection without raising, and so must deleting the
claims that shadowed it, and a full rebuild.
"""

from __future__ import annotations

import random
import string
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from agenda.fields import EVENT_FIELDS, OCCURRENCE_FIELDS, problem
from agenda.projection import rebuild_all
from claims.keys import occurrence_key
from claims.models import Claim
from claims.services import NewClaim, delete_claim, write_claims

pytestmark = pytest.mark.django_db

SEED = 20261025
CASES = 400
KEY = "datatourisme:fuzz"
NOW = datetime.now(timezone.utc)
YEARS = [1, 1900, 1911, 1912, 1913, 1940, 2026, 2199, 2200, 2201, 9999]
OFFSETS = ["Z", "+00:00", "+01:00", "+02:00", "-14:00", "+14:00", "+14:01", "-23:59", "+05:30", "+01:00:30"]


def text(rng: random.Random, cap: int) -> str:
    n = rng.choice([0, 1, 5, cap - 1, cap, cap + 1])
    alphabet = string.ascii_letters + " éà€\t\n'\"<>&" + rng.choice(["", "\x00", "\x7f", " "])
    return "".join(rng.choice(alphabet) for _ in range(max(n, 0)))


def number(rng: random.Random, bound: int) -> Any:
    return rng.choice([None, 0, bound, -bound, bound + 1, 44.36, 1e308, -1e308, 10**400, float("nan"), True, "44"])


def a_date(rng: random.Random) -> str:
    y = rng.choice(YEARS)
    return f"{y:04d}-{rng.choice([1, 2, 10, 12]):02d}-{rng.choice([1, 25, 28, 31]):02d}"


def a_time(rng: random.Random) -> Any:
    return rng.choice([None, "00:00", "02:30", "21:00", "23:59:59", "24:00", "21:00+02:00", "9:00", "", 2100])


def a_local(rng: random.Random) -> Any:
    d = a_date(rng)
    return rng.choice([None, d, f"{d}T{rng.choice(['00:00:00', '02:30:00', '23:59:59'])}{rng.choice(OFFSETS)}",
                       f"{d} 10:00{rng.choice(OFFSETS)}", "garbage", 12])


def an_url(rng: random.Random) -> Any:
    return rng.choice([None, "https://e.example/" + "x" * rng.choice([0, 1981, 1982, 1983]), "javascript:alert(1)",
                       "https://user:pw@e.example/", "http://[::1", "https://e.example/\x00", 5])


def commune(rng: random.Random) -> Any:
    return rng.choice(["26220", "2A004", "2B033", "123456", "2C004", " 26220 ", "", "ABCDE", "2620", 26220])


def location(rng: random.Random) -> Any:
    loc = {"name": rng.choice([None, text(rng, 300)]), "street": rng.choice([None, text(rng, 300)]),
           "postal_code": rng.choice(["26110", text(rng, 300)]), "commune": commune(rng),
           "commune_name": rng.choice(["Nyons", text(rng, 300)]), "lat": number(rng, 90), "lon": number(rng, 180)}
    if rng.random() < 0.1:
        loc[rng.choice(["extra", "k\x00"])] = text(rng, 300)
    return loc if rng.random() < 0.95 else rng.choice([None, [], "Nyons"])


def event_value(rng: random.Random, field: str) -> Any:
    if field == "name":
        return text(rng, 300)
    if field == "description":
        return rng.choice([None, text(rng, 10_000)])
    if field == "category":
        return rng.choice(["marche", "concert", "autre", "inconnue", ["marche"]])
    if field == "location":
        return location(rng)
    if field == "organizer":
        return rng.choice([None, {"name": text(rng, 300), "url": an_url(rng)}, {"url": None}])
    if field == "offers":
        return rng.choice([None, {"is_free": rng.choice([None, True, False, "oui"]), "price": rng.choice([None, text(rng, 300)]),
                                  "url": an_url(rng)}])
    if field == "visibility":
        return rng.choice(["published", "hidden", {"value": "hidden", "reason": text(rng, 300)}, {"value": ["x"]}])
    if field == "source_url":
        return an_url(rng)
    if field == "source_updated_at":
        return a_local(rng)
    raise AssertionError(field)


def occurrence_value(rng: random.Random, field: str, okey: str, day: str) -> Any:
    if field == "dates":
        end = rng.choice([None, day, a_date(rng)])
        return {"start_date": rng.choice([day, day, a_date(rng)]), "start_time": a_time(rng), "end_date": end,
                "end_time": a_time(rng)}
    if field == "listed":
        return rng.choice([True, False, 1])
    if field == "status":
        return rng.choice(["scheduled", "rescheduled", "postponed", "cancelled", "tentative"])
    if field == "previous_start":
        return a_local(rng)
    if field == "moved_to":
        return occurrence_key(KEY, date.fromisoformat("2026-11-12")) if rng.random() < 0.5 else f"{KEY}/{a_date(rng)}"
    if field == "visibility":
        return rng.choice(["published", "pending", {"value": "hidden", "reason": "x"}])
    raise AssertionError(field)


def random_claim(rng: random.Random) -> NewClaim:
    source = rng.choice(["datatourisme", "moderation", "direct"])
    seen_at = NOW - timedelta(days=rng.choice([0, 1, 365 * 100]))
    if rng.random() < 0.5:
        field = rng.choice(sorted(EVENT_FIELDS))
        return NewClaim("event", KEY, field, event_value(rng, field), source, seen_at)
    day = rng.choice(["2026-11-05", "2026-11-12", "1912-01-01", "2200-12-31"])
    okey = f"{KEY}/{day}"
    field = rng.choice(sorted(OCCURRENCE_FIELDS))
    return NewClaim("occurrence", okey, field, occurrence_value(rng, field, okey, day), source, seen_at)


def test_every_accepted_value_projects() -> None:
    rng = random.Random(SEED)
    accepted = refused = 0
    for case in range(CASES):
        claim = random_claim(rng)
        why = problem(claim.target_type, claim.target_key, claim.field, claim.value)
        if why is not None:
            refused += 1
            with pytest.raises(ValueError):
                write_claims(KEY, [claim])
            continue
        accepted += 1
        write_claims(KEY, [claim])  # must not raise: problem() said it's fine
        if rng.random() < 0.2:
            # Restore: drop a moderation override, so whatever it shadowed now wins.
            for c in Claim.objects.filter(event_key=KEY, source_id="moderation").order_by("?")[:1]:
                delete_claim(c.target_type, c.target_key, c.field, c.source_id)
    rebuild_all()
    # The generator must exercise both sides of the rules, or the test proves nothing.
    assert accepted > CASES // 4 and refused > CASES // 4, (accepted, refused)
